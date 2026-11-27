#!/usr/bin/env python3
"""Resumable, read-only validation-signature recorder (Python 3 stdlib only).

Subscribes to the `validations`, `manifests` and `ledger` streams of your own node's loopback admin
WebSocket and records, for every distinct validation, the raw signed STValidation (`data`) next to
the fields the node decoded from it. It also records the signing-key manifests (`manifest`), the
node's trusted list (`validators`), its quorum (`server_info`) and the NegativeUNL ledger entry
(`ledger_entry`). Only the read-only commands in ALLOWED_COMMANDS are ever sent. Run it inside the
node container's network namespace (run-collector.sh), because the admin port listens on the
container's own loopback.

`vl` subcommand (run on the host, not in the namespace): fetch the published signed VL over HTTPS
into DIR/vl-<tag>.json (the window runner takes one at the start and one at the end).

Recording: DIR/seg/NNNNNN.jsonl.gz finished segments (gzip mtime 0) and DIR/seg/current.jsonl, the
open segment. A segment is closed every --rotate ledgers and at stop. On start, a torn last line of
current.jsonl is cut and the leftover segment is closed first, so a kill loses at most one line.
One JSON object per line, `ev` is one of
  session  collector (re)start: {start, resumed, last_ledger, torn_bytes, bad_lines, window_end, seg}
  conn     connected + subscribed: {n = connection number in this session, t}; the verifier counts
           a ledger as observed only inside a single connection (see verify_valsig.py)
  ledger   validated ledger closed: {li, lh, ct, txn, t}
  val      first sighting of (signing key, ledger index, ledger hash):
           {li, k, lh, st, fl, full, vh?, mk?, sig, d, t}; d = the raw signed STValidation (hex)
  manev    manifests stream event, as sent: {mk, sk, seq, t}
  man      `manifest` answer: {req, mk?, m (base64 manifest), seq?, t}
  snap     `validators` result (only when it changed; peer `uri` fields dropped): {h, v, t}
  q        server_info quorum / validator_list + NegativeUNL entry (only when changed):
           {quorum, vlist, nunl, li, t}
  gap      ledgers the ledger stream never delivered: {from, to, count, reason, t}
           reason = stream_skip (same connection) | reconnect | restart (collector was down)
  stop     clean stop: {t, reason}
`t` = this host's UTC clock (unix seconds) when the message was read.
"""

import argparse
import glob
import gzip
import hashlib
import json
import os
import signal
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "txflow"))
from wsclient import WsClient  # noqa: E402

STREAMS = ["validations", "manifests", "ledger"]
ALLOWED_COMMANDS = {"subscribe", "server_info", "validators", "manifest", "ledger_entry"}
# keylet::negativeUNL(): sha512Half(0x004E 'N') - fixed index of the NegativeUNL singleton
NEGUNL_INDEX = "2E8A59AA9D3B5B186B0B9E0F62E6C02587CA74A4D778938E957B6357D364B244"
KEEP_LEDGERS = 2000
VL_URL = "https://postfiat.org/testnet_vl.json"
_stop = None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def drop_torn_tail(path):
    """Cut a partial last line. Returns the number of bytes removed."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0
    with open(path, "rb+") as fh:
        data = fh.read()
        if data.endswith(b"\n"):
            return 0
        keep = data.rfind(b"\n") + 1
        fh.truncate(keep)
        return len(data) - keep


def strip_uri(obj):
    """Drop every `uri` key (the peer a list was received from carries its IP address)."""
    if isinstance(obj, dict):
        return {k: strip_uri(v) for k, v in obj.items() if k != "uri"}
    if isinstance(obj, list):
        return [strip_uri(v) for v in obj]
    return obj


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Segments:
    """Gzip segment store: closed NNNNNN.jsonl.gz files + one open current.jsonl."""

    def __init__(self, out):
        self.dir = os.path.join(out, "seg")
        os.makedirs(self.dir, exist_ok=True)
        self.cur = os.path.join(self.dir, "current.jsonl")
        self.fh = None
        self.ledgers_in_cur = 0

    def closed(self):
        return sorted(glob.glob(os.path.join(self.dir, "[0-9]" * 6 + ".jsonl.gz")))

    def next_name(self):
        c = self.closed()
        n = int(os.path.basename(c[-1])[:6]) + 1 if c else 1
        return os.path.join(self.dir, f"{n:06d}.jsonl.gz")

    def close_current(self):
        """Compress current.jsonl into the next segment (atomic rename), then remove it."""
        if self.fh:
            self.fh.close()
            self.fh = None
        if not os.path.exists(self.cur):
            return None
        if os.path.getsize(self.cur) == 0:
            os.remove(self.cur)
            return None
        dst = self.next_name()
        tmp = dst + ".tmp"
        with open(self.cur, "rb") as src, open(tmp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    gz.write(b)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, dst)
        os.remove(self.cur)
        self.ledgers_in_cur = 0
        return dst

    def lines(self):
        """Every recorded line, oldest first (closed segments, then the open one)."""
        for p in self.closed():
            with gzip.open(p, "rt") as fh:
                yield from fh
        if os.path.exists(self.cur):
            with open(self.cur) as fh:
                yield from fh

    def write(self, e):
        if self.fh is None:
            self.fh = open(self.cur, "a")
        self.fh.write(json.dumps(e, separators=(",", ":")) + "\n")
        self.fh.flush()
        if e["ev"] != "val":
            os.fsync(self.fh.fileno())
        if e["ev"] == "ledger":
            self.ledgers_in_cur += 1


class State:
    """In-memory view rebuilt from the recording: recent validation keys, last ledger, last snapshots."""

    def __init__(self, keep=KEEP_LEDGERS):
        self.keep = keep
        self.seen = {}  # ledger index -> set of (key, ledger hash)
        self.last_ledger = None
        self.vals = 0
        self.bad_lines = 0
        self.snap_h = None
        self.q_h = None
        self.man_have = set()  # keys a `man` answer exists for

    def load(self, lines):
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                self.bad_lines += 1
                continue
            self.apply(e)
        return self

    def has(self, li, k, lh):
        return (k, lh) in self.seen.get(li, ())

    def apply(self, e):
        ev = e.get("ev")
        if ev == "val":
            self.seen.setdefault(e["li"], set()).add((e["k"], e["lh"]))
            self.vals += 1
            self._prune(e["li"])
        elif ev == "ledger":
            self.last_ledger = max(self.last_ledger or 0, e["li"])
            self._prune(e["li"])
        elif ev == "snap":
            self.snap_h = e["h"]
        elif ev == "q":
            self.q_h = digest({k: e.get(k) for k in ("quorum", "vlist", "nunl")})
        elif ev == "man":
            self.man_have.add(e["req"])

    def _prune(self, li):
        if len(self.seen) > self.keep * 2:
            floor = li - self.keep
            for old in [x for x in self.seen if x < floor]:
                del self.seen[old]


class Handler:
    """Turns stream messages and command answers into events (pure logic; I/O goes through emit/ask)."""

    def __init__(self, state, emit, ask):
        self.s = state
        self.emit = emit
        self.ask = ask  # ask(command, **kw) -> sends one allowed command; answer comes back via on_message
        self.gap_reason = "restart" if state.last_ledger else None
        self.dups = 0
        self.asked = set()
        self.pending = {}  # request id -> (kind, extra)
        self.q_parts = {}
        self.n = 0

    def _event(self, e):
        self.s.apply(e)
        self.emit(e)

    def _req(self, kind, cmd, extra=None, **kw):
        self.n += 1
        rid = f"{kind}-{self.n}"
        self.pending[rid] = (kind, extra)
        self.ask(cmd, id=rid, **kw)

    def want_manifest(self, key):
        if key and key not in self.asked:
            self.asked.add(key)
            self._req("man", "manifest", extra=key, public_key=key)

    def poll(self):
        """Periodic snapshot: trusted list, quorum, NegativeUNL (events only when they changed)."""
        self.q_parts = {}
        self._req("snap", "validators")
        self._req("si", "server_info")
        self._req("nunl", "ledger_entry", index=NEGUNL_INDEX, ledger_index="validated")

    def on_connect(self, reconnect, t, n):
        if reconnect:
            self.gap_reason = "reconnect"
        self.pending = {}
        self.asked = set(self.s.man_have)
        self._event({"ev": "conn", "n": n, "t": round(t, 3)})

    def on_response(self, m, t):
        rid = m.get("id")
        if rid not in self.pending:
            return
        kind, extra = self.pending.pop(rid)
        res = m.get("result") or {}
        ok = m.get("status") == "success"
        if kind == "man":
            if ok and res.get("manifest"):
                e = {"ev": "man", "req": extra, "m": res["manifest"], "t": round(t, 3)}
                d = res.get("details") or {}
                if d.get("master_key"):
                    e["mk"] = d["master_key"]
                if "seq" in d:
                    e["seq"] = d["seq"]
                self._event(e)
            return
        if kind == "snap":
            if ok:
                v = strip_uri(res)
                h = digest(v)
                if h != self.s.snap_h:
                    self._event({"ev": "snap", "h": h, "v": v, "t": round(t, 3)})
                for mk in v.get("trusted_validator_keys") or []:
                    self.want_manifest(mk)
            return
        if kind == "si":
            info = res.get("info") or {}
            self.q_parts["quorum"] = info.get("validation_quorum") if ok else None
            self.q_parts["vlist"] = info.get("validator_list") if ok else None
            self.q_parts["li"] = (info.get("validated_ledger") or {}).get("seq") if ok else None
        elif kind == "nunl":
            if ok:
                self.q_parts["nunl"] = res.get("node") or {}
            elif (m.get("error") or res.get("error")) == "entryNotFound":
                self.q_parts["nunl"] = {}
            else:
                self.q_parts["nunl"] = None
        if {"quorum", "vlist", "li", "nunl"} <= set(self.q_parts):
            q = {k: self.q_parts[k] for k in ("quorum", "vlist", "nunl")}
            if digest(q) != self.s.q_h:
                self._event({"ev": "q", **q, "li": self.q_parts["li"], "t": round(t, 3)})
            self.q_parts = {}

    def on_message(self, m, t):
        typ = m.get("type")
        if typ == "response":
            self.on_response(m, t)
        elif typ == "validationReceived":
            k, lh, d = m.get("validation_public_key"), m.get("ledger_hash"), m.get("data")
            try:
                li = int(m["ledger_index"])
            except (KeyError, TypeError, ValueError):
                return
            if not k or not lh or not d:
                return
            if self.s.has(li, k, lh):
                self.dups += 1
                return
            e = {
                "ev": "val",
                "li": li,
                "k": k,
                "lh": lh,
                "st": m.get("signing_time"),
                "fl": m.get("flags"),
                "full": bool(m.get("full")),
            }
            if m.get("validated_hash"):
                e["vh"] = m["validated_hash"]
            if m.get("master_key"):
                e["mk"] = m["master_key"]
            e.update({"sig": m.get("signature"), "d": d, "t": round(t, 3)})
            self._event(e)
            self.want_manifest(k)
        elif typ == "manifestReceived":
            self._event(
                {
                    "ev": "manev",
                    "mk": m.get("master_key"),
                    "sk": m.get("signing_key"),
                    "seq": m.get("seq"),
                    "t": round(t, 3),
                }
            )
            # a new manifest replaces the old one: ask again for the master key's current manifest
            mk = m.get("master_key")
            self.asked.discard(mk)
            self.want_manifest(mk)
        elif typ == "ledgerClosed":
            idx = int(m["ledger_index"])
            last = self.s.last_ledger
            if last is not None and idx <= last:
                return
            if last is not None and idx > last + 1:
                self._event(
                    {
                        "ev": "gap",
                        "from": last + 1,
                        "to": idx - 1,
                        "count": idx - 1 - last,
                        "reason": self.gap_reason or "stream_skip",
                        "t": round(t, 3),
                    }
                )
            self.gap_reason = None
            self._event(
                {
                    "ev": "ledger",
                    "li": idx,
                    "lh": m.get("ledger_hash"),
                    "ct": m.get("ledger_time"),
                    "txn": m.get("txn_count"),
                    "t": round(t, 3),
                }
            )


def log(fh, msg):
    line = f"{iso(time.time())} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def load_window(out, hours):
    p = os.path.join(out, "state.json")
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh), True
    now = int(time.time())
    st = {"window_start": now, "window_end": now + int(hours * 3600)}
    with open(p + ".tmp", "w") as fh:
        json.dump(st, fh)
    os.replace(p + ".tmp", p)
    return st, False


def make_send(ws):
    def send(cmd, **kw):
        if cmd not in ALLOWED_COMMANDS:
            raise ValueError(f"refusing command {cmd}")
        ws.send(json.dumps({"command": cmd, **kw}))

    return send


def run(args, connect=WsClient, clock=time.time):
    global _stop
    os.makedirs(args.out, exist_ok=True)
    lf = open(args.log, "a") if args.log else None
    segs = Segments(args.out)
    torn = drop_torn_tail(segs.cur)
    leftover = segs.close_current()
    state = State().load(segs.lines())
    win, resumed = load_window(args.out, args.hours)
    holder = {}

    def ask(cmd, **kw):
        holder["send"](cmd, **kw)

    h = Handler(state, segs.write, ask)
    segs.write(
        {
            "ev": "session",
            "start": round(clock(), 3),
            "resumed": resumed,
            "last_ledger": state.last_ledger,
            "torn_bytes": torn,
            "bad_lines": state.bad_lines,
            "window_end": win["window_end"],
            "seg": len(segs.closed()),
        }
    )
    log(
        lf,
        f"{'resume' if resumed else 'start'} window {iso(win['window_start'])} -> {iso(win['window_end'])}"
        f" last_ledger={state.last_ledger} vals={state.vals} torn_bytes={torn} bad_lines={state.bad_lines}"
        f" closed_leftover={os.path.basename(leftover) if leftover else '-'} pid={os.getpid()}",
    )
    if clock() >= win["window_end"]:
        segs.write({"ev": "stop", "t": round(clock(), 3), "reason": "window already complete"})
        segs.close_current()
        log(lf, "window already complete, nothing to do")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", "SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", "SIGINT"))
    connects, fails, next_hour = 0, 0, clock() + 3600
    while _stop is None and clock() < win["window_end"]:
        try:
            ws = connect(args.url, timeout=10)
            ws.settimeout(args.silence)
            holder["send"] = make_send(ws)
            h.on_connect(reconnect=connects > 0, t=clock(), n=connects + 1)
            connects += 1
            fails = 0
            holder["send"]("subscribe", id="sub", streams=STREAMS)
            h.poll()
            next_poll = clock() + args.poll
            log(lf, f"connected ({connects}) {args.url}, subscribed {','.join(STREAMS)}")
            while _stop is None and clock() < win["window_end"]:
                raw = ws.recv()
                t = clock()
                m = json.loads(raw)
                if m.get("type") == "response" and m.get("status") != "success" and m.get("id") == "sub":
                    raise ConnectionError(f"subscribe refused: {m.get('error')}")
                h.on_message(m, t)
                if t >= next_poll:
                    next_poll = t + args.poll
                    h.poll()
                if segs.ledgers_in_cur >= args.rotate:
                    name = segs.close_current()
                    log(lf, f"segment {os.path.basename(name)} closed last_ledger={state.last_ledger}")
                if t >= next_hour:
                    next_hour += 3600
                    log(
                        lf,
                        f"progress last_ledger={state.last_ledger} vals={state.vals} dups_dropped={h.dups}"
                        f" manifests={len(state.man_have)} segments={len(segs.closed())}",
                    )
            ws.close()
        except (OSError, ConnectionError, ValueError, socket.timeout) as e:
            log(lf, f"stream error: {type(e).__name__}: {e}; reconnect in {args.retry}s")
            fails += 1
            if fails >= args.max_fail:
                segs.write({"ev": "stop", "t": round(clock(), 3), "reason": f"{fails} failed connects"})
                segs.close_current()
                log(
                    lf, f"exit after {fails} failed connects (supervisor restarts with the current namespace)"
                )
                return 2
            for _ in range(args.retry):
                if _stop is not None:
                    break
                time.sleep(1)
    reason = _stop or "window complete"
    segs.write({"ev": "stop", "t": round(clock(), 3), "reason": reason})
    name = segs.close_current()
    log(
        lf,
        f"stop ({reason}) last_ledger={state.last_ledger} vals={state.vals} dups_dropped={h.dups}"
        f" manifests={len(state.man_have)} last_segment={os.path.basename(name) if name else '-'}",
    )
    return 0


def fetch_vl(args, opener=urllib.request.urlopen):
    """Save the published signed VL as DIR/vl-<tag>.json (raw bytes as served) + a fetch record."""
    os.makedirs(args.out, exist_ok=True)
    req = urllib.request.Request(args.vl_url, headers={"User-Agent": "pft-ledger-stats-valsig"})
    with opener(req, timeout=60) as r:
        body = r.read()
        status = getattr(r, "status", 200)
    json.loads(body)  # must be JSON
    dst = os.path.join(args.out, f"vl-{args.tag}.json")
    with open(dst + ".tmp", "wb") as fh:
        fh.write(body)
    os.replace(dst + ".tmp", dst)
    rec = {
        "tag": args.tag,
        "url": args.vl_url,
        "status": status,
        "t": round(time.time(), 3),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }
    with open(os.path.join(args.out, "vl-fetch.jsonl"), "a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    print(
        f"{iso(rec['t'])} vl {args.tag}: {args.vl_url} -> {os.path.basename(dst)}"
        f" status={status} bytes={len(body)}"
        f" sha256={rec['sha256']}",
        flush=True,
    )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record the streams (inside the node's network namespace)")
    r.add_argument("--url", default="ws://127.0.0.1:6006/")
    r.add_argument("--out", required=True)
    r.add_argument("--log")
    r.add_argument("--hours", type=float, default=4.5)
    r.add_argument("--rotate", type=int, default=300, help="close a gzip segment every N ledgers")
    r.add_argument("--poll", type=int, default=60, help="trusted list / quorum / NegativeUNL every N s")
    r.add_argument("--silence", type=int, default=60, help="reconnect after N s without any message")
    r.add_argument("--retry", type=int, default=10)
    r.add_argument("--max-fail", type=int, default=3, help="exit 2 after N connection failures in a row")
    v = sub.add_parser("vl", help="fetch the published signed VL (on the host)")
    v.add_argument("--out", required=True)
    v.add_argument("--tag", required=True, choices=["start", "end"])
    v.add_argument("--vl-url", default=VL_URL)
    a = ap.parse_args(argv)
    return run(a) if a.cmd == "record" else fetch_vl(a)


if __name__ == "__main__":
    sys.exit(main())
