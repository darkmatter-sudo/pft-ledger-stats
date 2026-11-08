#!/usr/bin/env python3
"""Resumable, read-only ledger-header fetcher for dormancy/ (Python 3 stdlib only).

Collects the exact close time of every distinct AccountRoot PreviousTxnLgrSeq in the given
snapshots that a source still holds, and appends one header per line to --out (headers.jsonl):
  {"ledger_index", "ledger_hash", "close_time", "parent_close_time", "source"}

Sources, tried in order for each seq: the public RPC (--rpc, HTTPS JSON-RPC) and, unless
--no-local, your own node's admin RPC via `docker exec <container> curl` (state/snapshot.py's
client). A source is asked only for seqs inside its `complete_ledgers` (read once per run with
server_info); a seq no source holds is "pruned" and is left to the time map's bounded
interpolation. Transient errors (timeouts, HTTP 429/5xx, `internal`/`tooBusy`/`noNetwork`) are
retried with exponential backoff (--retries, base --backoff s); `lgrNotFound` inside the
advertised range is counted and the next source is tried.

Resume: each header line is flushed and fsynced before the next request. On start a torn last
line (SIGKILL mid-write) is cut and reported as torn_bytes; seqs already in the file are skipped.
SIGTERM/SIGINT stop after the current line (exit 4).

Only read-only methods are sent: server_info, ledger. Exit 0 done, 4 interrupted, 2 no source.

Usage:
  ./headers_dormancy.py snapA.jsonl.gz [snapB.jsonl.gz] --out data/dormancy/headers.jsonl [--log F]
"""

import argparse
import gzip
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "state"))

READ_ONLY = {"server_info", "ledger"}
TRANSIENT = {"internal", "tooBusy", "noNetwork", "noCurrent", "slowDown"}
STOP = {"flag": False}


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Transient(Exception):
    pass


class HttpRpc:
    def __init__(self, url, gap=0.25, timeout=20, opener=None, allow=READ_ONLY):
        self.url, self.gap, self.timeout, self.allow = url, gap, timeout, frozenset(allow)
        self.last = 0.0
        self.count = 0
        self.opener = opener or urllib.request.urlopen

    def call(self, method, params=None):
        if method not in self.allow:
            raise ValueError(f"refusing non read-only method {method}")
        wait = self.gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.count += 1
        body = json.dumps({"method": method, "params": [params or {}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read())["result"]
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise Transient(f"http {e.code}") from e
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise Transient(f"net {type(e).__name__}") from e
        if result.get("status") == "error":
            err = result.get("error", "unknown")
            if err in TRANSIENT:
                raise Transient(err)
            raise LookupError(err)
        return result


class LocalRpc:
    """state/snapshot.py's docker-exec client, with its LookupError/RuntimeError mapped like HttpRpc."""

    def __init__(self, container="postfiatd", port=5005):
        from snapshot import Rpc  # state/snapshot.py

        self.rpc = Rpc(container=container, port=port, rate=4.0)

    @property
    def count(self):
        return self.rpc.count

    def call(self, method, params=None):
        if method not in READ_ONLY:
            raise ValueError(f"refusing non read-only method {method}")
        try:
            if method == "server_info":  # snapshot.Rpc allows server_state, which has the same field
                return {"info": self.rpc.call("server_state", params)["state"]}
            return self.rpc.call(method, params)
        except LookupError as e:
            if str(e) in TRANSIENT:
                raise Transient(str(e)) from e
            raise
        except RuntimeError as e:
            raise Transient(str(e)) from e


def parse_ranges(text):
    """'1-5,7,9-10' -> [(1,5),(7,7),(9,10)]; 'empty' -> []."""
    out = []
    if not text or text == "empty":
        return out
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def in_ranges(seq, ranges):
    return any(lo <= seq <= hi for lo, hi in ranges)


def with_retry(fn, retries, backoff, stats, sleep=time.sleep):
    for attempt in range(retries + 1):
        try:
            return fn()
        except Transient as e:
            stats["retries"] = stats.get("retries", 0) + 1
            stats.setdefault("retries_by_error", {})
            stats["retries_by_error"][str(e)] = stats["retries_by_error"].get(str(e), 0) + 1
            if attempt == retries:
                raise
            sleep(backoff * (2**attempt))
    raise AssertionError("unreachable")


def repair(path):
    """Cut a torn last line; return (torn_bytes, set of seqs present)."""
    if not os.path.exists(path):
        return 0, set()
    with open(path, "rb") as fh:
        data = fh.read()
    keep = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
    torn = len(data) - len(keep)
    if torn:
        with open(path, "r+b") as fh:
            fh.truncate(len(keep))
            fh.flush()
            os.fsync(fh.fileno())
    seqs = set()
    for line in keep.split(b"\n"):
        if line.strip():
            seqs.add(int(json.loads(line)["ledger_index"]))
    return torn, seqs


def wanted_seqs(snapshots):
    seqs = set()
    for path in snapshots:
        with gzip.open(path, "rt") as fh:
            header = json.loads(fh.readline())
            seqs.add(int(header["ledger_index"]))
            for line in fh:
                if '"AccountRoot"' in line:
                    seqs.add(int(json.loads(line)["PreviousTxnLgrSeq"]))
    return seqs


def fetch(sources, seqs, out, retries=4, backoff=1.0, logfh=None, sleep=time.sleep, item_delay=0.0):
    """sources: list of (name, rpc). Appends headers for seqs not yet in out. Returns stats dict."""
    torn, have = repair(out)
    stats = {
        "torn_bytes": torn,
        "already": len(seqs & have),
        "fetched": 0,
        "pruned": 0,
        "lgrNotFound": 0,
        "by_source": {},
    }
    ranges = {}
    for name, rpc in sources:
        try:
            info = with_retry(lambda rpc=rpc: rpc.call("server_info"), retries, backoff, stats, sleep)
            ranges[name] = parse_ranges(info["info"].get("complete_ledgers", ""))
        except (Transient, LookupError, OSError) as e:
            ranges[name] = []
            _log(logfh, f"source {name} unavailable: {e}")
        _log(logfh, f"source {name} complete_ledgers {ranges[name]}")
    if not any(ranges.values()):
        stats["error"] = "no source answered server_info"
        return stats
    todo = sorted(seqs - have)
    _log(logfh, f"START want {len(seqs)} have {len(have)} todo {len(todo)} torn_bytes {torn}")
    with open(out, "a") as fh:
        for seq in todo:
            if STOP["flag"]:
                stats["interrupted"] = True
                break
            got = None
            for name, rpc in sources:
                if not in_ranges(seq, ranges[name]):
                    continue
                try:
                    res = with_retry(
                        lambda rpc=rpc, seq=seq: rpc.call("ledger", {"ledger_index": seq}),
                        retries,
                        backoff,
                        stats,
                        sleep,
                    )
                except LookupError:
                    stats["lgrNotFound"] += 1
                    continue
                except Transient:
                    continue
                led = res["ledger"]
                if not res.get("validated", False) or int(led["ledger_index"]) != seq:
                    continue
                got = {
                    "close_time": int(led["close_time"]),
                    "ledger_hash": led["ledger_hash"],
                    "ledger_index": seq,
                    "parent_close_time": int(led["parent_close_time"]),
                    "source": name,
                }
                break
            if got is None:
                stats["pruned"] += 1
                continue
            fh.write(json.dumps(got, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            stats["fetched"] += 1
            stats["by_source"][got["source"]] = stats["by_source"].get(got["source"], 0) + 1
            if item_delay:
                sleep(item_delay)
    _log(logfh, "DONE " + json.dumps(stats, sort_keys=True))
    return stats


def _log(fh, msg):
    line = f"{utcnow()} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def _on_signal(signum, _frame):
    STOP["flag"] = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshots", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--log")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--no-public", action="store_true")
    p.add_argument("--no-local", action="store_true")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--gap", type=float, default=0.25, help="min seconds between public requests")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--backoff", type=float, default=1.0)
    p.add_argument("--item-delay", type=float, default=0.0, help="extra sleep per header (drills)")
    args = p.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    sources = []
    if not args.no_public:
        sources.append(("public", HttpRpc(args.rpc, gap=args.gap)))
    if not args.no_local:
        sources.append(("local", LocalRpc(container=args.container)))
    logfh = open(args.log, "a") if args.log else None
    stats = fetch(
        sources,
        wanted_seqs(args.snapshots),
        args.out,
        args.retries,
        args.backoff,
        logfh,
        item_delay=args.item_delay,
    )
    if "error" in stats:
        return 2
    return 4 if stats.get("interrupted") else 0


if __name__ == "__main__":
    sys.exit(main())
