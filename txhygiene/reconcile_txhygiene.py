#!/usr/bin/env python3
"""Reconcile the txhygiene window: internal completeness, then against an independent txmix collection.

Part 1, txhygiene data alone (<tx>/ledgers.jsonl, tx.jsonl, gaps.jsonl, state.json):
  every ledger start_seq..last exactly once, no gaps, every tx line once (tx_hash and (ledger, tx_index)),
  no tx line without its ledger line, per ledger txn_count == tx_rows == tx lines and tx_index 0..n-1.
Part 2, against txmix/collect_tx.py run as a separate process over the same window (<twin>/ledgers.csv,
  tx.csv): over the common ledger range, per-ledger ledger_hash and txn_count equal, the same tx hashes
  with the same (ledger, tx_index, type, result, fee), and equal txmix-style window totals (tx count, by
  type, by result, fee sum). Ledgers outside the common range are listed, not compared.

--negative drops the tx lines of one ledger and adds 1 drop to one fee in memory (seeded), and passes only
if both parts report exactly those faults.

Exit 0 when every check passes (or, with --negative, when the planted faults are all reported).
"""

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
from collect_txhygiene import read_jsonl  # noqa: E402


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def internal(ledgers, txs, gaps, start_seq):
    """List of (check, ok, detail) for the txhygiene data alone."""
    out = []
    idx = [r["ledger_index"] for r in ledgers]
    last = idx[-1] if idx else start_seq - 1
    out.append(
        (
            "ledgers start_seq..last exactly once",
            idx == list(range(start_seq, last + 1)),
            f"{len(idx)} ledgers {start_seq}-{last}, {len(idx) - len(set(idx))} duplicate(s), "
            f"{len(set(range(start_seq, last + 1)) - set(idx))} missing",
        )
    )
    out.append(("no gaps recorded", not gaps, f"{len(gaps)} gap line(s)"))
    hashes = Counter(t["tx_hash"] for t in txs)
    keys = Counter((t["ledger_index"], t["tx_index"]) for t in txs)
    out.append(
        (
            "every tx once (hash and (ledger, tx_index))",
            max(hashes.values(), default=1) == 1 and max(keys.values(), default=1) == 1,
            f"{len(txs)} lines, {len(hashes)} hashes, {len(keys)} (ledger, index) keys",
        )
    )
    known = set(idx)
    orphan = [t for t in txs if t["ledger_index"] not in known]
    out.append(("no tx line without its ledger line", not orphan, f"{len(orphan)} orphan line(s)"))
    per = Counter(t["ledger_index"] for t in txs)
    bad = [
        r["ledger_index"]
        for r in ledgers
        if not (r["txn_count"] == r["tx_rows"] == per.get(r["ledger_index"], 0))
    ]
    out.append(
        (
            "per ledger txn_count == tx_rows == tx lines",
            not bad,
            f"{len(bad)} ledger(s) differ{': ' + ','.join(map(str, bad[:10])) if bad else ''}; "
            f"txn_count sum {sum(r['txn_count'] for r in ledgers)}, tx lines {len(txs)}",
        )
    )
    by_ledger = {}
    for t in txs:
        by_ledger.setdefault(t["ledger_index"], []).append(t["tx_index"])
    badidx = [k for k, v in by_ledger.items() if sorted(v) != list(range(len(v)))]
    out.append(("tx_index 0..n-1 in every ledger", not badidx, f"{len(badidx)} ledger(s) with holes"))
    return out


def totals(rows, type_key, result_key, fee_key):
    return {
        "tx": len(rows),
        "by_type": dict(sorted(Counter(r[type_key] for r in rows).items())),
        "by_result": dict(sorted(Counter(r[result_key] for r in rows).items())),
        "fee_sum_drops": sum(int(r[fee_key]) for r in rows),
    }


def against_twin(ledgers, txs, tled, ttx):
    """List of (check, ok, detail) and the common range, comparing with the txmix twin."""
    mine = {r["ledger_index"]: r for r in ledgers}
    twin = {int(r["ledger_index"]): r for r in tled}
    common = sorted(set(mine) & set(twin))
    if not common:
        return [("common ledger range not empty", False, "no common ledgers")], None
    lo, hi = common[0], common[-1]
    out = []
    span = [i for i in range(lo, hi + 1)]
    holes = [i for i in span if i not in mine or i not in twin]
    out.append(
        (
            "both sides hold every ledger of the common range",
            not holes,
            f"common {lo}-{hi} ({len(common)} ledgers); only txhygiene {len(set(mine) - set(twin))}, "
            f"only txmix {len(set(twin) - set(mine))} (outside the common range: not compared)",
        )
    )
    hdiff = [i for i in common if mine[i]["ledger_hash"] != twin[i]["ledger_hash"]]
    cdiff = [i for i in common if mine[i]["txn_count"] != int(twin[i]["txn_count"])]
    out.append(("per-ledger ledger_hash equal", not hdiff, f"{len(hdiff)} differ"))
    out.append(
        (
            "per-ledger txn_count equal",
            not cdiff,
            f"{len(cdiff)} differ{': ' + ','.join(map(str, cdiff[:10])) if cdiff else ''}",
        )
    )
    a = {t["tx_hash"]: t for t in txs if lo <= t["ledger_index"] <= hi}
    b = {r["tx_hash"]: r for r in ttx if lo <= int(r["ledger_index"]) <= hi}
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    out.append(
        (
            "same tx hashes",
            not only_a and not only_b,
            f"{len(a)} vs {len(b)}; only txhygiene {len(only_a)}, only txmix {len(only_b)}",
        )
    )
    fdiff = []
    for h in sorted(set(a) & set(b)):
        x, y = a[h], b[h]
        mine_v = (x["ledger_index"], x["tx_index"], x["tx_type"], x["result"], x["fee_drops"])
        twin_v = (int(y["ledger_index"]), int(y["tx_index"]), y["tx_type"], y["result"], int(y["fee_drops"]))
        if mine_v != twin_v:
            fdiff.append((h, mine_v, twin_v))
    out.append(
        (
            "per tx (ledger, tx_index, type, result, fee) equal",
            not fdiff,
            f"{len(fdiff)} differ"
            + "".join(f"\n      {h[:16]} txhygiene={m} txmix={t}" for h, m, t in fdiff[:10]),
        )
    )
    ta = totals(list(a.values()), "tx_type", "result", "fee_drops")
    tb = totals(list(b.values()), "tx_type", "result", "fee_drops")
    for k in ("tx", "by_type", "by_result", "fee_sum_drops"):
        out.append((f"window total {k} equal", ta[k] == tb[k], f"txhygiene {ta[k]} | txmix {tb[k]}"))
    return out, {"lo": lo, "hi": hi, "ledgers": len(common), "totals": ta}


def plant(ledgers, txs, seed):
    """Negative control: drop all tx lines of one ledger that has txs, add 1 drop to one other tx's fee."""
    rng = random.Random(seed)
    with_tx = sorted({t["ledger_index"] for t in txs})
    drop = rng.choice(with_tx)
    kept = [dict(t) for t in txs if t["ledger_index"] != drop]
    victim = rng.choice(kept)
    victim["fee_drops"] += 1
    return ledgers, kept, drop, victim["tx_hash"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx", default="data/txhygiene/tx", help="txhygiene collector output dir")
    ap.add_argument("--twin", default="data/txhygiene/txmix", help="txmix collect_tx.py output dir")
    ap.add_argument("--negative", action="store_true")
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    ledgers = read_jsonl(os.path.join(a.tx, "ledgers.jsonl"))
    txs = read_jsonl(os.path.join(a.tx, "tx.jsonl"))
    gaps = read_jsonl(os.path.join(a.tx, "gaps.jsonl"))
    with open(os.path.join(a.tx, "state.json")) as fh:
        start_seq = json.load(fh)["start_seq"]
    tled = read_csv(os.path.join(a.twin, "ledgers.csv"))
    ttx = read_csv(os.path.join(a.twin, "tx.csv"))
    planted = None
    if a.negative:
        ledgers, txs, drop, victim = plant(ledgers, txs, a.seed)
        planted = {"dropped_ledger": drop, "fee_plus_one": victim}
        print(f"NEGATIVE: dropped every tx line of ledger {drop}; fee +1 drop on {victim[:16]}")
    print(f"txhygiene: {len(ledgers)} ledgers, {len(txs)} tx lines | ", end="")
    print(f"txmix twin: {len(tled)} ledgers, {len(ttx)} tx")
    res1 = internal(ledgers, txs, gaps, start_seq)
    res2, common = against_twin(ledgers, txs, tled, ttx)
    print("== part 1: txhygiene data alone")
    for name, ok, detail in res1:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    print("== part 2: against the independent txmix collection")
    for name, ok, detail in res2:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    failed = {name for name, ok, _ in res1 + res2 if not ok}
    if a.negative:
        expect = {
            "per ledger txn_count == tx_rows == tx lines",
            "same tx hashes",
            "per tx (ledger, tx_index, type, result, fee) equal",
            "window total tx equal",
            "window total fee_sum_drops equal",
        }
        passed = expect <= failed
        print(f"negative control: expected FAIL {sorted(expect)}; missing {sorted(expect - failed)}")
    else:
        passed = not failed
    n = len(res1) + len(res2)
    what = f"{len(failed)}/{n} checks fail as planted" if a.negative else f"{n - len(failed)}/{n} checks pass"
    print(f"RESULT: {'PASS' if passed else 'FAIL'} ({what})")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(
                {
                    "negative": a.negative,
                    "planted": planted,
                    "checks": [{"check": n, "ok": ok, "detail": d} for n, ok, d in res1 + res2],
                    "common": common,
                    "pass": passed,
                },
                fh,
                indent=1,
            )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
