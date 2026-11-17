#!/usr/bin/env python3
"""Read-only AccountRoot snapshot + gap-free transaction capture for acctflags/ (stdlib only).

  snapshot  pins one validated ledger (index + hash) on your own node and, on that ledger hash:
            1. pages the FULL state tree with `ledger_data` (binary=false) and counts every
               LedgerEntryType;
            2. pages `ledger_data type=account` and refuses any count or index-set difference
               against the AccountRoots of the full tree;
            3. writes accounts.jsonl (every AccountRoot, sorted by Account), depositpreauth.jsonl
               (every DepositPreauth object), header.json and counts.json, fsynced, and
               MANIFEST.json (rows + sha256 of every file) last. A run without MANIFEST.json is
               incomplete. Each ledger_data page is kept in pages/ with its marker, so an
               interrupted run resumes on the same pinned hash from the last saved page.

  capture   forward-follows validated ledgers from --start, fetching each ledger once with
            `ledger transactions=true expand=true`, and appends one line per ledger to
            tx.jsonl (all transactions with metadata) and then ledgers.jsonl (index, hash,
            parent_hash, close_time, tx count). Every ledger's parent_hash must equal the hash
            of the ledger before it and its tx count must equal the number of transactions
            returned: a break stops the run (exit 3) instead of recording a gap. A restart
            resumes after the last complete ledgers.jsonl line (torn tails and tx lines without
            a ledgers line are dropped first). --to N stops after ledger N.

Only read-only RPC methods are sent (server_state, ledger, ledger_data). Nothing is signed or
submitted. Every process sends at most --rate requests/s (snapshot default 1.5, capture default
0.5, so both together stay <= 2 req/s). Transient failures are retried with exponential backoff;
each retry is logged as a RETRY line and a call that never answers stops the run (exit 2).
Raw outputs hold real addresses and stay private; extract_flags.py pseudonymizes them.

Usage:
  ./collect_flags.py snapshot --out data/acctflags/A [--ledger N] [--log FILE]
  ./collect_flags.py capture --out data/acctflags/tx --start N [--to M] [--log FILE]
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

FORMAT = "pft-acctflags-snapshot/1"
READ_ONLY = {"server_state", "ledger", "ledger_data"}
TRANSIENT = {"tooBusy", "noNetwork", "noCurrent", "noClosed", "slowDown"}
FILES = ("header.json", "counts.json", "accounts.jsonl", "depositpreauth.jsonl")
STOP = []


class GaveUp(Exception):
    pass


class ChainBreak(Exception):
    pass


class RpcError(Exception):
    """A definite (non-transient) RPC error answer, e.g. lgrNotFound."""

    def __init__(self, error, result):
        super().__init__(error)
        self.error = error
        self.result = result


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def docker_transport(container="postfiatd", port=5005):
    url = f"http://127.0.0.1:{port}/"

    def send(body):
        cmd = [
            "sudo",
            "docker",
            "exec",
            container,
            "curl",
            "-s",
            "-m",
            "30",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            url,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            raise OSError(f"docker exec rc={out.returncode}: {out.stderr.strip()[:200]}")
        return out.stdout

    return send


class Rpc:
    """JSON-RPC client: read-only allowlist, rate limit, logged exponential backoff."""

    def __init__(
        self,
        send,
        log,
        rate=1.5,
        retries=5,
        backoff=1.0,
        sleep=time.sleep,
        clock=time.monotonic,
        allow=READ_ONLY,
    ):
        if rate <= 0 or rate > 2:
            raise ValueError("rate must be in (0, 2] requests/s")
        self.send, self.log, self.retries, self.backoff = send, log, retries, backoff
        self.sleep, self.clock, self.allow = sleep, clock, frozenset(allow)
        self.interval = 1.0 / rate
        self.last = None
        self.calls = 0
        self.retried = 0

    def _throttle(self):
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            self.sleep(self.interval - (now - self.last))
            now = self.clock()
        self.last = now

    def call(self, method, params):
        if method not in self.allow:
            raise ValueError(f"refusing non read-only method {method}")
        body = json.dumps({"method": method, "params": [params]}, sort_keys=True)
        for attempt in range(self.retries + 1):
            self._throttle()
            self.calls += 1
            try:
                result = json.loads(self.send(body))["result"]
                err = result.get("error") if result.get("status") == "error" else None
                if err is None:
                    return result
                if err not in TRANSIENT:
                    raise RpcError(err, result)
                reason = f"error={err}"
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
                reason = f"{type(e).__name__}: {str(e)[:120]}"
            if attempt == self.retries:
                raise GaveUp(f"{method} gave up after {attempt + 1} tries ({reason})")
            delay = self.backoff * (2**attempt)
            self.retried += 1
            self.log(f"RETRY {method} try {attempt + 1}/{self.retries + 1} {reason} sleep={delay:g}s")
            self.sleep(delay)
        raise AssertionError("unreachable")


def dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def write_file(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def jsonl(rows):
    return "".join(dumps(r) + "\n" for r in rows)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def page(rpc, ledger_hash, pagedir, tag, typ=None, limit=2048, log=print):
    """All objects of one ledger (optionally one type). Pages are saved so a rerun resumes."""
    os.makedirs(pagedir, exist_ok=True)
    objs, marker, n = [], None, 0
    while True:
        path = os.path.join(pagedir, f"{tag}-{n:04d}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved["ledger_hash"] != ledger_hash:
                raise SystemExit(f"{path} belongs to ledger {saved['ledger_hash']}, pinned {ledger_hash}")
            state, marker = saved["state"], saved.get("marker")
            log(f"RESUME page {tag}-{n:04d} objects={len(state)}")
        else:
            p = {"ledger_hash": ledger_hash, "binary": False, "limit": limit}
            if typ:
                p["type"] = typ
            if marker is not None:
                p["marker"] = marker
            r = rpc.call("ledger_data", p)
            if r.get("ledger_hash") != ledger_hash:
                raise ValueError(f"ledger_data answered for {r.get('ledger_hash')}, pinned {ledger_hash}")
            state, marker = r.get("state", []), r.get("marker")
            write_file(path, dumps({"ledger_hash": ledger_hash, "state": state, "marker": marker}))
        objs.extend(state)
        n += 1
        if marker is None:
            return objs, n


def snapshot(rpc, out, ledger=None, log=print, limit=2048):
    os.makedirs(out, exist_ok=True)
    if os.path.exists(os.path.join(out, "MANIFEST.json")):
        raise SystemExit(f"{out} already holds a complete snapshot (MANIFEST.json); use a new --out")
    pin_path = os.path.join(out, "pin.json")
    if os.path.exists(pin_path):
        with open(pin_path, encoding="utf-8") as fh:
            led = json.load(fh)
        if ledger is not None and int(ledger) != int(led["ledger_index"]):
            raise SystemExit(f"{out} is pinned to {led['ledger_index']}, not {ledger}")
        log(f"RESUME pinned ledger_index={led['ledger_index']} ledger_hash={led['ledger_hash']}")
    else:
        if ledger is None:
            st = rpc.call("server_state", {})["state"]
            if st.get("server_state") not in ("full", "validating", "proposing"):
                raise SystemExit(f"node not synced: server_state={st.get('server_state')}")
            ledger = st["validated_ledger"]["seq"]
        hdr = rpc.call("ledger", {"ledger_index": int(ledger)})
        if not hdr.get("validated"):
            raise SystemExit(f"ledger {ledger} is not validated on this node")
        led = hdr["ledger"]
        write_file(pin_path, dumps(led) + "\n")
    lh = led["ledger_hash"]
    log(f"PIN ledger_index={led['ledger_index']} ledger_hash={lh} close_time_iso={led.get('close_time_iso')}")

    pagedir = os.path.join(out, "pages")
    full, full_pages = page(rpc, lh, pagedir, "full", limit=limit, log=log)
    by_type = {}
    for o in full:
        by_type[o["LedgerEntryType"]] = by_type.get(o["LedgerEntryType"], 0) + 1
    log(f"FULL objects={len(full)} pages={full_pages} types={dumps(dict(sorted(by_type.items())))}")
    filt, pages = page(rpc, lh, pagedir, "account", "account", limit=limit, log=log)
    a = sorted(o["index"] for o in full if o["LedgerEntryType"] == "AccountRoot")
    b = sorted(o["index"] for o in filt)
    same = a == b and all(o["LedgerEntryType"] == "AccountRoot" for o in filt)
    log(
        f"COUNT AccountRoot full_tree={len(a)} type=account filtered={len(b)} pages={pages} "
        f"index_sets_equal={same}"
    )
    if not same:
        raise SystemExit("full-tree and type=account AccountRoot sets differ: refusing snapshot")
    roots = sorted((o for o in full if o["LedgerEntryType"] == "AccountRoot"), key=lambda o: o["Account"])
    if len({o["Account"] for o in roots}) != len(roots):
        raise SystemExit("duplicate Account in AccountRoot set: refusing snapshot")
    preauth = sorted((o for o in full if o["LedgerEntryType"] == "DepositPreauth"), key=lambda o: o["index"])

    header = {
        "format": FORMAT,
        "ledger_index": int(led["ledger_index"]),
        "ledger_hash": lh,
        "account_hash": led.get("account_hash"),
        "close_time": led.get("close_time"),
        "close_time_iso": led.get("close_time_iso"),
        "parent_hash": led.get("parent_hash"),
    }
    counts = {
        "full_tree_objects": len(full),
        "full_tree_pages": full_pages,
        "full_tree_by_type": dict(sorted(by_type.items())),
        "accountroot_type_filter": len(b),
        "accountroot_type_filter_pages": pages,
        "index_sets_equal": same,
    }
    texts = {
        "header.json": dumps(header) + "\n",
        "counts.json": dumps(counts) + "\n",
        "accounts.jsonl": jsonl(roots),
        "depositpreauth.jsonl": jsonl(preauth),
    }
    for name in FILES:
        write_file(os.path.join(out, name), texts[name])
    manifest = {
        "format": FORMAT,
        "ledger_index": header["ledger_index"],
        "ledger_hash": lh,
        "files": {n: {"rows": texts[n].count("\n"), "sha256": sha256(os.path.join(out, n))} for n in FILES},
        "rpc_calls": rpc.calls,
        "retries": rpc.retried,
    }
    write_file(os.path.join(out, "MANIFEST.json"), json.dumps(manifest, sort_keys=True, indent=1) + "\n")
    for n in FILES:
        log(f"FILE {n} rows={manifest['files'][n]['rows']} sha256={manifest['files'][n]['sha256']}")
    log(
        f"DONE ledger_index={header['ledger_index']} accounts={len(roots)} "
        f"rpc_calls={rpc.calls} retries={rpc.retried}"
    )
    return manifest


def read_lines(path):
    """Complete JSON lines of a file; a torn (unterminated or unparsable) tail is cut off."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        data = fh.read()
    rows, good = [], 0
    for raw in data.split(b"\n")[:-1]:
        try:
            rows.append(json.loads(raw))
        except ValueError:
            break
        good += len(raw) + 1
    if good != len(data):
        with open(path, "r+b") as fh:
            fh.truncate(good)
    return rows


def resume_capture(out, start, log=print):
    """Last complete ledger row (or None) after repairing torn tails and orphan tx lines."""
    lpath, tpath = os.path.join(out, "ledgers.jsonl"), os.path.join(out, "tx.jsonl")
    ledgers = read_lines(lpath)
    txs = read_lines(tpath)
    for i, row in enumerate(ledgers):
        if row["ledger_index"] != start + i:
            raise ChainBreak(f"ledgers.jsonl row {i} is {row['ledger_index']}, expected {start + i}")
    done = ledgers[-1]["ledger_index"] if ledgers else start - 1
    keep = [t for t in txs if t["ledger_index"] <= done]
    if len(keep) != len(txs):
        write_file(tpath, jsonl(keep))
        log(f"REPAIR dropped {len(txs) - len(keep)} tx line(s) without a ledgers line")
    aligned = len(keep) == len(ledgers) and all(
        t["ledger_index"] == r["ledger_index"] for t, r in zip(keep, ledgers, strict=True)
    )
    if not aligned:
        raise ChainBreak("tx.jsonl and ledgers.jsonl are not line-aligned")
    return ledgers[-1] if ledgers else None


def append(path, row):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(dumps(row) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def fetch_ledger(rpc, n):
    r = rpc.call("ledger", {"ledger_index": n, "transactions": True, "expand": True})
    if not r.get("validated"):
        return None
    return r["ledger"]


def capture(rpc, out, start, to=None, log=print, poll=2.0, sleep=time.sleep, max_idle=None):
    os.makedirs(out, exist_ok=True)
    state_path = os.path.join(out, "state.json")
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            st = json.load(fh)
        if st["start"] != start:
            raise SystemExit(f"{out} captures from {st['start']}, not {start}")
    else:
        write_file(state_path, dumps({"start": start, "created": utcnow()}) + "\n")
    last = resume_capture(out, start, log)
    n = last["ledger_index"] + 1 if last else start
    prev_hash = last["ledger_hash"] if last else None
    log(f"CAPTURE start={start} next={n} to={to}")
    validated, idle, t_hour = 0, 0, time.monotonic()
    while not STOP and (to is None or n <= to):
        if n > validated:
            validated = rpc.call("server_state", {})["state"].get("validated_ledger", {}).get("seq", 0)
            if n > validated:
                idle += 1
                if max_idle is not None and idle > max_idle:
                    break
                sleep(poll)
                continue
        try:
            led = fetch_ledger(rpc, n)
        except RpcError as e:
            if e.error == "lgrNotFound" and n <= validated:
                raise ChainBreak(f"ledger {n} is validated but no longer on this node (pruned?)") from e
            raise
        if led is None:
            sleep(poll)
            continue
        idle = 0
        txs = led.get("transactions", [])
        if prev_hash is not None and led["parent_hash"] != prev_hash:
            raise ChainBreak(f"ledger {n} parent_hash {led['parent_hash']} != hash of {n - 1} {prev_hash}")
        if any(not isinstance(t, dict) or "metaData" not in t for t in txs):
            raise ChainBreak(f"ledger {n}: transaction without expanded metadata")
        row = {
            "ledger_index": int(led["ledger_index"]),
            "ledger_hash": led["ledger_hash"],
            "parent_hash": led["parent_hash"],
            "close_time": led.get("close_time"),
            "close_time_iso": led.get("close_time_iso"),
            "txn_count": len(txs),
        }
        if row["ledger_index"] != n:
            raise ChainBreak(f"asked for ledger {n}, got {row['ledger_index']}")
        append(os.path.join(out, "tx.jsonl"), {"ledger_index": n, "transactions": txs})
        append(os.path.join(out, "ledgers.jsonl"), row)
        prev_hash = row["ledger_hash"]
        if time.monotonic() - t_hour >= 3600:
            log(f"PROGRESS ledger={n} validated={validated} rpc_calls={rpc.calls} retries={rpc.retried}")
            t_hour = time.monotonic()
        n += 1
    log(f"STOPPED last={n - 1} rpc_calls={rpc.calls} retries={rpc.retried}")
    return n - 1


def _handle(signum, _frame):
    STOP.append(signum)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", required=True)
    s.add_argument("--ledger", type=int)
    s.add_argument("--rate", type=float, default=1.5)
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    c.add_argument("--start", type=int, required=True)
    c.add_argument("--to", type=int)
    c.add_argument("--rate", type=float, default=0.5)
    for p in (s, c):
        p.add_argument("--container", default="postfiatd")
        p.add_argument("--retries", type=int, default=5)
        p.add_argument("--backoff", type=float, default=1.0)
        p.add_argument("--log")
    a = ap.parse_args(argv)
    fh = open(a.log, "a", encoding="utf-8") if a.log else None

    def log(msg):
        line = f"{utcnow()} {msg}"
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")
            fh.flush()

    rpc = Rpc(docker_transport(a.container), log, a.rate, a.retries, a.backoff)
    try:
        if a.cmd == "snapshot":
            snapshot(rpc, a.out, a.ledger, log)
        else:
            signal.signal(signal.SIGTERM, _handle)
            signal.signal(signal.SIGINT, _handle)
            capture(rpc, a.out, a.start, a.to, log)
    except GaveUp as e:
        log(f"GAVE_UP {e}")
        return 2
    except ChainBreak as e:
        log(f"CHAIN_BREAK {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
