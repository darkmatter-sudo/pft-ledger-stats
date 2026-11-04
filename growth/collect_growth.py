#!/usr/bin/env python3
"""Resumable, read-only ledger-growth collector (Python 3 stdlib only).

Forward-follows validated ledgers on your own node's admin JSON-RPC through
`docker exec <container> curl` (same client and resume rules as txmix/collect_tx.py and
senders/collect_senders.py). Every ledger is fetched twice:

  1. `transactions: true, expand: true, binary: true`  -> exact serialized sizes: ledger header
     bytes, each tx_blob and each metadata blob (hex length / 2)
  2. `transactions: true, expand: true`                -> tx type, result, TransactionIndex and
     every AffectedNodes entry (Created/Modified/DeletedNode + LedgerEntryType)

The two answers are joined, not trusted blindly: the ledger hash is recomputed from the binary
header (SHA-512Half of "LWR\\0" + header) and must equal the JSON ledger_hash, and every binary
transaction's hash (SHA-512Half of "TXN\\0" + tx_blob) must match exactly one JSON transaction
(same count, same set). A mismatch is a failed attempt.

RPC fallback: if the local node fails a ledger on every retry (e.g. lgrNotFound after
online_delete), the same two calls go to the public RPC (--public-rpc, retried with exponential
backoff). The source of each ledger is recorded; a ledger neither source can serve is a gap.

Outputs (in --out):
  ledgers.csv   one row per ledger: hash, close time, txn_count, header/tx_blob/meta bytes,
                created/modified/deleted node counts, source (local|public), fetched_utc
  tx.csv        one row per transaction: ledger, tx index, hash, type, result, blob/meta bytes,
                created/modified/deleted counts
  nodes.csv     one row per AffectedNodes entry: ledger, tx index, action, LedgerEntryType
  gaps.csv      ledgers no source could serve
  retries.csv   every failed attempt (ledger, source, attempt, error), including ones that later worked
  restarts.csv  one row per process start: kind (start/resume), lag, torn bytes, orphan rows
  state.json    window start/end and start seq, so a restart resumes the same window
  collect.log   (--log) start/resume, hourly progress, gaps, final summary

Write order per ledger: tx.csv, nodes.csv, then the ledgers.csv row. On resume a torn last line
is cut from every file, and tx.csv/nodes.csv rows after the last ledgers.csv/gaps.csv ledger are
dropped (they are fetched again), so a SIGKILL at any point cannot duplicate or lose a ledger.
SIGTERM/SIGINT stop cleanly after the current ledger. No ledger closed after the window end is
written, whatever the node returns.
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "txmix"))
from collect_tx import (  # noqa: E402
    RIPPLE_EPOCH,
    Rpc,
    append_rows,
    count_rows,
    drop_orphan_tx,
    drop_torn_tail,
    last_index,
    log,
    ripple_iso,
    utcnow,
)

LEDGER_FIELDS = [
    "ledger_index",
    "ledger_hash",
    "close_time",
    "close_time_iso",
    "txn_count",
    "header_bytes",
    "tx_blob_bytes",
    "meta_bytes",
    "created",
    "modified",
    "deleted",
    "source",
    "fetched_utc",
]
TX_FIELDS = [
    "ledger_index",
    "tx_index",
    "tx_hash",
    "tx_type",
    "result",
    "blob_bytes",
    "meta_bytes",
    "created",
    "modified",
    "deleted",
]
NODE_FIELDS = ["ledger_index", "tx_index", "action", "entry_type"]
GAP_FIELDS = ["ledger_index", "error", "logged_at"]
RETRY_FIELDS = ["ledger_index", "source", "attempt", "error", "logged_at"]
RESTART_FIELDS = ["kind", "at_utc", "first_ledger", "validated", "lag", "torn_bytes", "orphan_rows"]
ACTIONS = {"CreatedNode": "created", "ModifiedNode": "modified", "DeletedNode": "deleted"}

_stop = False


def sha512half(prefix, hexdata):
    return hashlib.sha512(prefix + bytes.fromhex(hexdata)).digest()[:32].hex().upper()


def growth_rows(jled, bled):
    """Join the JSON and binary answers for one ledger.

    Returns (ledger_row, tx_rows, node_rows). Raises ValueError if the two answers disagree.
    """
    idx = int(jled["ledger_index"])
    if sha512half(b"LWR\x00", bled["ledger_data"]) != jled["ledger_hash"].upper():
        raise ValueError(f"ledger {idx}: binary header hash != JSON ledger_hash")
    sizes = {}
    for t in bled.get("transactions") or []:
        h = sha512half(b"TXN\x00", t["tx_blob"])
        if h in sizes:
            raise ValueError(f"ledger {idx}: duplicate binary tx {h}")
        sizes[h] = (len(t["tx_blob"]) // 2, len(t["meta"]) // 2)
    jtx = jled.get("transactions") or []
    if len(jtx) != len(sizes):
        raise ValueError(f"ledger {idx}: {len(jtx)} JSON txs vs {len(sizes)} binary txs")
    tx_rows, node_rows = [], []
    for tx in jtx:
        h = tx.get("hash", "").upper()
        if h not in sizes:
            raise ValueError(f"ledger {idx}: JSON tx {h} has no binary twin")
        meta = tx.get("metaData") or tx.get("meta") or {}
        if "TransactionIndex" not in meta:
            raise ValueError(f"ledger {idx}: tx {h} has no TransactionIndex")
        ti = int(meta["TransactionIndex"])
        counts = {"created": 0, "modified": 0, "deleted": 0}
        for node in meta.get("AffectedNodes") or []:
            ((kind, body),) = node.items()
            action = ACTIONS[kind]
            counts[action] += 1
            node_rows.append(
                {
                    "ledger_index": idx,
                    "tx_index": ti,
                    "action": action,
                    "entry_type": body.get("LedgerEntryType", ""),
                }
            )
        blob, mb = sizes[h]
        tx_rows.append(
            {
                "ledger_index": idx,
                "tx_index": ti,
                "tx_hash": h,
                "tx_type": tx.get("TransactionType", ""),
                "result": meta.get("TransactionResult", ""),
                "blob_bytes": blob,
                "meta_bytes": mb,
                **counts,
            }
        )
    tx_rows.sort(key=lambda r: r["tx_index"])
    node_rows.sort(key=lambda r: r["tx_index"])
    ct = int(jled["close_time"])
    led = {
        "ledger_index": idx,
        "ledger_hash": jled["ledger_hash"].upper(),
        "close_time": ct,
        "close_time_iso": ripple_iso(ct),
        "txn_count": len(tx_rows),
        "header_bytes": len(bled["ledger_data"]) // 2,
        "tx_blob_bytes": sum(r["blob_bytes"] for r in tx_rows),
        "meta_bytes": sum(r["meta_bytes"] for r in tx_rows),
        "created": sum(r["created"] for r in tx_rows),
        "modified": sum(r["modified"] for r in tx_rows),
        "deleted": sum(r["deleted"] for r in tx_rows),
    }
    return led, tx_rows, node_rows


def in_window(close_time, end_unix):
    """Client-side window rule: a ledger belongs to the window iff it closed at or before the end."""
    return int(close_time) + RIPPLE_EPOCH <= end_unix


class PublicRpc:
    """Fallback JSON-RPC client for the public endpoint (read-only `ledger` only)."""

    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout
        self.count = 0

    def call(self, method, params=None):
        if method != "ledger":
            raise ValueError(f"refusing method {method} on the public RPC")
        self.count += 1
        body = json.dumps({"method": method, "params": [params or {}]}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            result = json.loads(r.read())["result"]
        if result.get("status") == "error":
            raise LookupError(result.get("error", "unknown"))
        return result


def fetch_pair(rpc, idx):
    """Both answers for one ledger from one source; validates closed + index."""
    base = {"ledger_index": idx, "transactions": True, "expand": True}
    b = rpc.call("ledger", {**base, "binary": True})["ledger"]
    j = rpc.call("ledger", base)["ledger"]
    if not j.get("closed", True) or int(j["ledger_index"]) != idx:
        raise LookupError(f"unexpected ledger {j.get('ledger_index')}")
    return j, b


def _handle(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/growth/run")
    p.add_argument("--log", default=None, help="append log lines to this file too")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument(
        "--rate",
        type=float,
        default=2.0,
        help="max local requests/second (default 2 = one ledger/s, two calls each; max 2)",
    )
    p.add_argument("--hours", type=float, default=24.0, help="window length from first start")
    p.add_argument("--start", type=int, default=None, help="first ledger (default: current validated)")
    p.add_argument("--stop-at", type=int, default=None, help="last ledger to fetch, then exit")
    p.add_argument("--retries", type=int, default=3, help="attempts per source")
    p.add_argument(
        "--public-rpc",
        default="https://rpc.testnet.postfiat.org",
        help="fallback endpoint ('' disables the fallback)",
    )
    p.add_argument("--backoff", type=float, default=2.0, help="first public-RPC backoff in seconds")
    p.add_argument("--no-sudo", action="store_true", help="call docker without sudo")
    a = p.parse_args(argv)

    if a.rate > 2.0:
        p.error("--rate above 2 req/s is not allowed")
    os.makedirs(a.out, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    lf = open(a.log, "a") if a.log else None
    rpc = Rpc(a.container, a.port, a.rate, ["docker"] if a.no_sudo else ["sudo", "docker"])
    pub = PublicRpc(a.public_rpc) if a.public_rpc else None

    path = {
        n: os.path.join(a.out, f"{n}.csv") for n in ("ledgers", "tx", "nodes", "gaps", "retries", "restarts")
    }
    state_path = os.path.join(a.out, "state.json")

    info = rpc.call("server_info")["info"]
    log(
        lf,
        f"node: build {info.get('build_version')} state {info.get('server_state')} "
        f"complete_ledgers {info.get('complete_ledgers')}",
    )
    validated = int(info["validated_ledger"]["seq"])

    state = {}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
    torn_total = orphans = 0
    if state:
        for fp in path.values():
            torn = drop_torn_tail(fp)
            torn_total += torn
            if torn:
                log(lf, f"RESUME dropped torn last line of {os.path.basename(fp)} ({torn} bytes)")
        done = max(
            x
            for x in (last_index(path["ledgers"]), last_index(path["gaps"]), state["start_seq"] - 1)
            if x is not None
        )
        for n in ("tx", "nodes"):
            k = drop_orphan_tx(path[n], done)
            orphans += k
            if k:
                log(lf, f"RESUME dropped {k} {n}.csv rows after ledger {done} (no ledgers.csv row)")
        nxt = done + 1
        kind = "resume"
        log(
            lf,
            f"RESUME at ledger {nxt} (window {state['start_utc']} -> {state['end_utc']}, "
            f"start seq {state['start_seq']}, behind by {validated - nxt + 1})",
        )
    else:
        nxt = a.start if a.start is not None else validated
        now = time.time()
        state = {
            "start": int(now),
            "end": int(now + a.hours * 3600),
            "start_seq": nxt,
            "start_utc": utcnow(),
            "stop_at": a.stop_at,
            "end_utc": datetime.fromtimestamp(now + a.hours * 3600, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=1)
        kind = "start"
        log(
            lf,
            f"WINDOW START {state['start_utc']} -> {state['end_utc']} at ledger {nxt}, "
            f"stop_at {a.stop_at}, local rate <= {a.rate} req/s, fallback {a.public_rpc or 'off'}",
        )
    append_rows(
        path["restarts"],
        RESTART_FIELDS,
        [
            {
                "kind": kind,
                "at_utc": utcnow(),
                "first_ledger": nxt,
                "validated": validated,
                "lag": validated - nxt + 1,
                "torn_bytes": torn_total,
                "orphan_rows": orphans,
            }
        ],
    )
    stop_at = state.get("stop_at")

    def progress():
        return (
            f"ledgers={count_rows(path['ledgers'])} tx={count_rows(path['tx'])} "
            f"nodes={count_rows(path['nodes'])} gaps={count_rows(path['gaps'])} "
            f"retries={count_rows(path['retries'])} local_requests={rpc.count} "
            f"public_requests={pub.count if pub else 0}"
        )

    reason = None
    next_progress = time.monotonic() + 3600
    while not _stop and (stop_at is None or nxt <= stop_at):
        if time.monotonic() >= next_progress:
            log(lf, f"PROGRESS last={nxt - 1} lag={validated - nxt + 1} {progress()}")
            next_progress = time.monotonic() + 3600
        if nxt > validated:
            if time.time() > state["end"] + 30:
                reason = "window end reached"
                break
            try:
                validated = int(rpc.call("server_info")["info"]["validated_ledger"]["seq"])
            except Exception as e:
                log(lf, f"WARN server_info failed: {e}")
            if nxt > validated:
                time.sleep(1.0)
            continue
        got = err = None
        sources = [("local", rpc)] + ([("public", pub)] if pub else [])
        for src, client in sources:
            for attempt in range(1, a.retries + 1):
                try:
                    j, b = fetch_pair(client, nxt)
                    got = (src, *growth_rows(j, b))
                    break
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    append_rows(
                        path["retries"],
                        RETRY_FIELDS,
                        [
                            {
                                "ledger_index": nxt,
                                "source": src,
                                "attempt": attempt,
                                "error": err,
                                "logged_at": utcnow(),
                            }
                        ],
                    )
                    log(lf, f"WARN ledger {nxt} {src} attempt {attempt}/{a.retries}: {err}")
                    time.sleep(2.0 if src == "local" else a.backoff * 2 ** (attempt - 1))
            if got:
                if src != "local":
                    log(lf, f"FALLBACK ledger {nxt} served by {src}")
                break
        if got:
            src, led, tx_rows, node_rows = got
            if not in_window(led["close_time"], state["end"]):
                reason = f"window end reached (ledger {nxt} closed {led['close_time_iso']}, not written)"
                break
            append_rows(path["tx"], TX_FIELDS, tx_rows)
            append_rows(path["nodes"], NODE_FIELDS, node_rows)
            append_rows(path["ledgers"], LEDGER_FIELDS, [{**led, "source": src, "fetched_utc": utcnow()}])
        else:
            append_rows(
                path["gaps"], GAP_FIELDS, [{"ledger_index": nxt, "error": err, "logged_at": utcnow()}]
            )
            log(lf, f"GAP ledger {nxt}: {err}")
        nxt += 1

    if reason is None:
        reason = "signal" if _stop else "stop_at reached"
    log(lf, f"STOP ({reason})")
    log(
        lf,
        f"SUMMARY window {state['start_utc']} -> {state['end_utc']} ledgers {state['start_seq']}-{nxt - 1} "
        f"{progress()} restarts={count_rows(path['restarts'])}",
    )
    if lf:
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
