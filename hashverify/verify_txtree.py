#!/usr/bin/env python3
"""Transaction-tree verifier: recompute tx IDs and the tx SHAMap root of sampled ledgers.

Read-only (server_info, ledger). Every checked ledger is fetched twice from one source:

  1. `transactions: true, expand: true, binary: true`  -> ledger_data (header bytes), ledger_hash,
     tx_blob + meta of every transaction
  2. `transactions: true`                              -> JSON header fields + the reported tx IDs

and checked without trusting any reported hash:

  header_ok   the 118-byte header serialized from the JSON fields == the binary ledger_data
  hash_ok     SHA-512Half("LWR\\0" || header) == ledger_hash in BOTH answers
  txids_ok    {SHA-512Half("TXN\\0" || tx_blob)} == the reported tx ID set (same count)
  root_ok     the SHAMap root rebuilt from ("SND\\0" leaves, "MIN\\0" inner nodes) == the header's
              transaction_hash (32 zero bytes for a ledger without transactions)

Selection (--count total, seeded with --seed):
  slice   EVERY ledger whose close_time lies in the --slice-seconds (default 3600) before the
          close_time of --slice-end (default: newest validated ledger - --margin), empty or not
  sample  random ledgers from the rest of the source's complete_ledgers (above oldest + --margin)
          until slice + sample = --count
  --range A-B checks every ledger of A..B instead (e.g. your own node's whole history).

Each ledger prints `OK` or `MISMATCH` with the failed checks and, for each, the ledger index plus
reported and computed hashes. Retries as verify_headers.py (one RETRY line each).

--record FILE (.gz ok) / --offline FILE re-run every check from the recorded answers, no RPC.
--tamper N  (with --offline) flips the last byte of the first tx_blob of ledger N (must have
            transactions): a real-data negative control that must print MISMATCH txids,root at N
            and exit 1.
Outputs (--out DIR): txtree.csv (one row per ledger), summary.csv (metric,value).
Exit 0 = every ledger OK, 1 = any mismatch, 2 = RPC failure.
"""

import argparse
import csv
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib_hashverify import ledger_hash, parse_ranges, serialize_header, tx_tree  # noqa: E402
from verify_headers import FIELDS, RETRY, RpcError, make_rpc, opener, out, ranges  # noqa: E402,F401

CSV_FIELDS = [
    "ledger_index",
    "selection",
    "txn_count",
    "close_time",
    "reported_root",
    "computed_root",
    "header_ok",
    "hash_ok",
    "txids_ok",
    "root_ok",
]


def fetch(rpc, idx):
    b = rpc.call("ledger", {"ledger_index": idx, "transactions": True, "expand": True, "binary": True})
    j = rpc.call("ledger", {"ledger_index": idx, "transactions": True})["ledger"]
    if int(j["ledger_index"]) != idx or int(b["ledger_index"]) != idx or not j.get("closed", False):
        raise RpcError(f"ledger {idx}: answers are for {j.get('ledger_index')}/{b.get('ledger_index')}")
    return {
        "ledger_index": idx,
        "binary": {
            "ledger_data": b["ledger"]["ledger_data"],
            "ledger_hash": b["ledger_hash"],
            "transactions": [
                {"tx_blob": t["tx_blob"], "meta": t["meta"]} for t in b["ledger"]["transactions"]
            ],
        },
        "json": {**{f: j[f] for f in FIELDS}, "transactions": list(j["transactions"])},
    }


def check(rec):
    """(row, problems). problems = [(check, reported, computed)]."""
    b, j = rec["binary"], rec["json"]
    probs = []
    hdr = serialize_header(j).hex().upper()
    if hdr != b["ledger_data"].upper():
        probs.append(("header", b["ledger_data"].upper(), hdr))
    lh = ledger_hash(j)
    for name, rep in (("hash", j["ledger_hash"]), ("hash_binary", b["ledger_hash"])):
        if rep.upper() != lh:
            probs.append((name, rep.upper(), lh))
    try:
        root, ids = tx_tree(b["transactions"])
    except ValueError as ex:
        root, ids = f"ERROR:{ex}", []
    want = sorted(t.upper() for t in j["transactions"])
    if sorted(ids) != want or len(ids) != len(want):
        probs.append(("txids", ",".join(want) or "-", ",".join(sorted(ids)) or "-"))
    if root != j["transaction_hash"].upper():
        probs.append(("root", j["transaction_hash"].upper(), root))
    names = {p[0] for p in probs}
    row = {
        "ledger_index": rec["ledger_index"],
        "selection": rec.get("selection", ""),
        "txn_count": len(want),
        "close_time": j["close_time"],
        "reported_root": j["transaction_hash"].upper(),
        "computed_root": root,
        "header_ok": "header" not in names,
        "hash_ok": not names & {"hash", "hash_binary"},
        "txids_ok": "txids" not in names,
        "root_ok": "root" not in names,
    }
    return row, probs


def report(rec, row, probs):
    if probs:
        for name, rep, comp in probs:
            out(f"MISMATCH ledger={row['ledger_index']} check={name} reported={rep} computed={comp}")
    else:
        out(
            f"OK ledger={row['ledger_index']} sel={row['selection']} txs={row['txn_count']} "
            f"root={row['computed_root']}"
        )


def select(rpc, count, seed, slice_seconds, slice_end, margin, emit):
    """Yield (idx, selection) with the slice walked backwards, then the random sample."""
    spans = ranges(rpc)
    lo, hi = max(spans, key=lambda s: s[1] - s[0])
    end = slice_end or hi - margin
    end_close = None
    idx, seen = end, set()
    while idx >= lo + margin:

        def label(rec, end_close=end_close):
            ct = int(rec["json"]["close_time"])
            return "slice" if end_close is None or end_close - ct <= slice_seconds else "slice_boundary"

        got = emit(idx, label)
        if end_close is None:
            end_close = int(got["json"]["close_time"])
        if got["selection"] == "slice_boundary":
            break
        seen.add(idx)
        idx -= 1
    out(f"SLICE {idx + 1}-{end} ({len(seen)} ledgers, {slice_seconds}s before close_time {end_close})")
    pool = [i for i in range(lo + margin, hi - margin + 1) if i not in seen]
    need = max(0, count - len(seen))
    rng = random.Random(seed)
    picks = sorted(rng.sample(pool, min(need, len(pool))))
    out(f"SAMPLE {len(picks)} random ledgers from {lo + margin}-{hi - margin} (seed {seed})")
    for i in picks:
        emit(i, "sample")


def summarize(rows, runtime, rpc):
    def n(pred):
        return sum(1 for r in rows if pred(r))

    s = [
        ("ledgers_checked", len(rows)),
        ("slice_ledgers", n(lambda r: r["selection"] == "slice")),
        ("slice_nonempty", n(lambda r: r["selection"] == "slice" and r["txn_count"] > 0)),
        ("sample_ledgers", n(lambda r: r["selection"] == "sample")),
        ("range_ledgers", n(lambda r: r["selection"] == "range")),
        ("nonempty_ledgers", n(lambda r: r["txn_count"] > 0)),
        ("empty_ledgers", n(lambda r: r["txn_count"] == 0)),
        ("transactions", sum(r["txn_count"] for r in rows)),
        ("max_txn_count", max((r["txn_count"] for r in rows), default=0)),
    ]
    for c in ("header_ok", "hash_ok", "txids_ok", "root_ok"):
        s.append((f"{c}_false", n(lambda r, c=c: not r[c])))
    s.append(
        (
            "mismatched_ledgers",
            n(lambda r: not (r["header_ok"] and r["hash_ok"] and r["txids_ok"] and r["root_ok"])),
        )
    )
    if rows:
        s += [
            ("first_ledger", min(r["ledger_index"] for r in rows)),
            ("last_ledger", max(r["ledger_index"] for r in rows)),
        ]
    s.append(("runtime_s", f"{runtime:.1f}"))
    if rpc is not None:
        s += [("rpc_calls", rpc.calls), ("rpc_retries", rpc.retried)]
    return s


def write_outputs(d, rows, summary):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "txtree.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["ledger_index"]))
    with open(os.path.join(d, "summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "value"])
        w.writerows(summary)


def tamper(recs, idx):
    rec = next((r for r in recs if r["ledger_index"] == idx), None)
    if rec is None or not rec["binary"]["transactions"]:
        raise ValueError(f"--tamper {idx}: ledger not recorded or has no transactions")
    t = rec["binary"]["transactions"][0]
    last = int(t["tx_blob"][-2:], 16) ^ 0x01
    t["tx_blob"] = t["tx_blob"][:-2] + f"{last:02X}"
    out(f"TAMPER ledger={idx} first tx_blob last byte flipped (negative control)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5, help="seconds between requests")
    p.add_argument("--count", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20261106)
    p.add_argument("--slice-seconds", type=int, default=3600)
    p.add_argument("--slice-end", type=int)
    p.add_argument("--margin", type=int, default=20)
    p.add_argument("--range", dest="span")
    p.add_argument("--out", required=True)
    p.add_argument("--record")
    p.add_argument("--offline")
    p.add_argument("--tamper", type=int, action="append", default=[])
    a = p.parse_args(argv)
    if a.tamper and not a.offline:
        p.error("--tamper only works with --offline (it never changes live data)")
    t0 = time.monotonic()
    rows, bad, rpc = [], 0, None

    def take(rec):
        nonlocal bad
        row, probs = check(rec)
        rows.append(row)
        bad += bool(probs)
        report(rec, row, probs)

    if a.offline:
        with opener(a.offline, "r") as fh:
            recs = [json.loads(line) for line in fh]
        out(f"OFFLINE {a.offline}: {len(recs)} ledgers")
        for idx in a.tamper:
            tamper(recs, idx)
        for rec in recs:
            take(rec)
    else:
        rpc = make_rpc(a.source, a.gap)
        rec_fh = opener(a.record, "w") if a.record else None

        def emit(idx, sel):
            rec = fetch(rpc, idx)
            rec["selection"] = sel(rec) if callable(sel) else sel
            if rec_fh:
                rec_fh.write(json.dumps(rec, sort_keys=True) + "\n")
            take(rec)
            return rec

        try:
            out(f"SOURCE {a.source} complete_ledgers={ranges(rpc)}")
            if a.span:
                ((lo, hi),) = parse_ranges(a.span)
                out(f"RANGE {lo}-{hi} ({hi - lo + 1} ledgers, every ledger)")
                for i in range(lo, hi + 1):
                    emit(i, "range")
            else:
                select(rpc, a.count, a.seed, a.slice_seconds, a.slice_end, a.margin, emit)
            out(f"SOURCE_END complete_ledgers={ranges(rpc)}")
        except RpcError as ex:
            out(f"RPC_FAILURE {ex}")
            return 2
        finally:
            if rec_fh:
                rec_fh.close()
    summary = summarize(rows, time.monotonic() - t0, rpc)
    write_outputs(a.out, rows, summary)
    for k, v in summary:
        out(f"SUMMARY {k}={v}")
    out(f"RESULT: {'PASS' if not bad else 'FAIL'} ({bad} mismatched ledgers)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
