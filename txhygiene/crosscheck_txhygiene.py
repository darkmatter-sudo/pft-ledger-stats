#!/usr/bin/env python3
"""Re-fetch random collected transactions from a PUBLIC Post Fiat RPC and compare every field.

For each sampled tx hash in <data>/tx.jsonl, sends `tx` (read-only) to --rpc, rebuilds the row from the
public answer with the collector's own tx_row() and the same HMAC key, and prints every field that
differs. Only ledgers inside the public server's complete_ledgers are sampled; anything outside is
printed as not checkable. Transient answers (slowDown, tooBusy, txnNotFound, lgrNotFound, internal,
transport errors, validated=false) are retried with backoff and every retry is printed; if they persist
the tx counts as FAIL.

--negative alters EVERY field of every sampled local row (null -> a value, ints +1, strings +"x") and
passes only if every field of every tx is reported as a mismatch, which proves the comparison can fail.

--json records each public answer as the rebuilt row (rpc_row); --offline FILE re-compares such a record
against the committed tx.jsonl with no RPC at all.

Exit 0: every sampled tx matched in every field (or, with --negative, every field mismatched).
"""

import argparse
import json
import os
import random
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
sys.path.insert(0, os.path.join(here, "..", "rewardflow"))
from collect_rewardflow import RETRY_ERRORS, Rpc, RpcError  # noqa: E402
from collect_txhygiene import TX_FIELDS, load_key, read_jsonl, tx_row  # noqa: E402

TRANSIENT = set(RETRY_ERRORS) | {"txnNotFound", "lgrNotFound", "internal"}


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def say(msg):
    print(f"{utcnow()} {msg}", flush=True)


def ranges(text):
    """'1-5,7,9-10' -> [(1, 5), (7, 7), (9, 10)]; 'empty' -> []."""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part or part == "empty":
            continue
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def covered(idx, rs):
    return any(lo <= idx <= hi for lo, hi in rs)


def sample(rows, count, seed):
    rng = random.Random(seed)
    return sorted(rng.sample(rows, min(count, len(rows))), key=lambda r: (r["ledger_index"], r["tx_index"]))


def alter(v):
    if v is None:
        return "X"
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1
    return f"{v}x"


def compare(local, remote):
    """[(field, local, remote)] for every TX_FIELDS field that differs."""
    return [(f, local.get(f), remote.get(f)) for f in TX_FIELDS if local.get(f) != remote.get(f)]


def fetch(rpc, h, tries=4, wait=5.0, sleep=time.sleep):
    """Validated public `tx` answer for h, retrying incomplete answers; RpcError if that persists."""
    for attempt in range(1, tries + 1):
        res = rpc.call("tx", {"transaction": h, "binary": False})
        if res.get("validated") is True and "meta" in res and "ledger_index" in res and "date" in res:
            return res
        say(
            f"RETRY tx {h[:16]} attempt {attempt}/{tries}: incomplete answer "
            f"(validated={res.get('validated')})"
        )
        if attempt < tries:
            sleep(wait)
            wait *= 2
    raise RpcError(f"tx {h}: still incomplete after {tries} attempts")


RESPONSE_KEYS = {"status", "warning", "warnings"}  # JSON-RPC envelope of the `tx` answer, not tx fields


def remote_row(res, key):
    tx = {k: v for k, v in res.items() if k not in RESPONSE_KEYS}
    return tx_row(int(res["ledger_index"]), int(res["date"]), tx, res["meta"], key)


def offline(record_path, tx_path):
    """Re-compare every recorded public row with the committed local row of the same hash."""
    with open(record_path) as fh:
        rec = json.load(fh)
    local = {r["tx_hash"]: r for r in read_jsonl(tx_path)}
    ok = bad = 0
    for res in rec["results"]:
        h, row = res["tx_hash"], res.get("rpc_row")
        if row is None or h not in local:
            bad += 1
            say(f"FAIL {h}: {'no recorded rpc_row' if row is None else 'not in tx.jsonl'}")
            continue
        diffs = compare(local[h], row)
        for f, lv, rv in diffs:
            say(f"MISMATCH {h[:16]} {f}: local={lv!r} recorded rpc={rv!r}")
        ok += not diffs
        bad += bool(diffs)
        say(
            f"{'PASS' if not diffs else 'FAIL'} {h} ledger {res['ledger_index']}: "
            f"{'OK' if not diffs else str(len(diffs)) + ' fields differ'} (offline)"
        )
    passed = bad == 0 and ok == rec["count"] and not rec["negative"]
    say(
        f"OFFLINE SUMMARY {ok}/{rec['count']} recorded tx match tx.jsonl in all {len(TX_FIELDS)} fields -> "
        f"{'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/txhygiene/tx", help="collector output dir (tx.jsonl, .hmac_key)")
    ap.add_argument("--key-file", default=None, help="HMAC key (default <data>/.hmac_key)")
    ap.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--max-ledger", type=int, default=None, help="sample only up to this ledger")
    ap.add_argument("--negative", action="store_true")
    ap.add_argument("--json", default=None, help="write the result as JSON here")
    ap.add_argument("--offline", default=None, help="re-check a --json record against the data, no RPC")
    a = ap.parse_args(argv)
    if a.offline:
        return offline(a.offline, os.path.join(a.data, "tx.jsonl"))

    key = load_key(a.key_file or os.path.join(a.data, ".hmac_key"))
    rows = read_jsonl(os.path.join(a.data, "tx.jsonl"))
    rpc = Rpc(a.rpc, gap=0.5, retries=6, backoff=2.0, retry_errors=TRANSIENT, methods={"server_info", "tx"})
    info = rpc.call("server_info", {})["info"]
    rs = ranges(info.get("complete_ledgers"))
    say(
        f"public RPC {a.rpc}: build {info.get('build_version')} "
        f"complete_ledgers {info.get('complete_ledgers')}"
    )
    if a.max_ledger is not None:
        rows = [r for r in rows if r["ledger_index"] <= a.max_ledger]
    pool = [r for r in rows if covered(r["ledger_index"], rs)]
    if rows:
        lo, hi = rows[0]["ledger_index"], rows[-1]["ledger_index"]
        say(
            f"local tx rows {len(rows)} in ledgers {lo}-{hi}; checkable on the public RPC {len(pool)}; "
            f"not checkable {len(rows) - len(pool)}"
        )
    picked = sample(pool, a.count, a.seed)
    say(
        f"sampled {len(picked)} tx (seed {a.seed})"
        f"{' NEGATIVE: every local field altered' if a.negative else ''}"
    )

    results, ok, failed = [], 0, 0
    for r in picked:
        local = {f: alter(v) for f, v in r.items()} if a.negative else r
        h = r["tx_hash"]
        try:
            remote = remote_row(fetch(rpc, h), key)
        except RpcError as e:
            failed += 1
            say(f"FAIL {h} ledger {r['ledger_index']}: {e}")
            results.append({"tx_hash": h, "ledger_index": r["ledger_index"], "error": str(e)})
            continue
        diffs = compare(local, remote)
        for f, lv, rv in diffs:
            say(f"MISMATCH {h[:16]} ledger {r['ledger_index']} {f}: local={lv!r} rpc={rv!r}")
        good = len(diffs) == len(TX_FIELDS) if a.negative else not diffs
        ok += good
        failed += not good
        verdict = "OK" if not diffs else f"{len(diffs)}/{len(TX_FIELDS)} fields differ"
        say(f"{'PASS' if good else 'FAIL'} {h} ledger {r['ledger_index']} {r['tx_type']}: {verdict}")
        results.append(
            {
                "tx_hash": h,
                "ledger_index": r["ledger_index"],
                "fields_compared": len(TX_FIELDS),
                "mismatches": [{"field": f, "local": lv, "rpc": rv} for f, lv, rv in diffs],
                "rpc_row": remote,
            }
        )
    passed = failed == 0 and len(picked) == a.count
    say(
        f"SUMMARY sampled {len(picked)}/{a.count} pass {ok} fail {failed} fields/tx {len(TX_FIELDS)} "
        f"public calls {rpc.calls} retried {rpc.retried} {dict(rpc.retry_kinds)} -> "
        f"{'PASS' if passed else 'FAIL'}"
    )
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(
                {
                    "rpc": a.rpc,
                    "public_complete_ledgers": info.get("complete_ledgers"),
                    "seed": a.seed,
                    "negative": a.negative,
                    "count": a.count,
                    "pass": passed,
                    "results": results,
                },
                fh,
                indent=1,
            )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
