#!/usr/bin/env python3
"""Mutation check for payflow (collector, analyzer, reconcile, crosscheck): put one broken line in at
a time in a temporary copy of the repo and run the payflow tests; every mutant must be CAUGHT (tests
fail, or hang past --timeout seconds). Each mutant's text must occur exactly once, or it is reported
NOT APPLIED (counts as a failure). The unmutated copy is run first as the control and must pass.

Usage: .venv/bin/python payflow/mutants_payflow.py [--timeout 120] [--only M7]
       (from the repo root; exit 0 = all caught)
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COL, ANA = "payflow/collect_payflow.py", "payflow/analyze_payflow.py"
REC, XC = "payflow/reconcile_payflow.py", "payflow/crosscheck_payflow.py"
MUTANTS = [
    # collector
    (
        "M1 collector: window end not enforced",
        COL,
        "    return int(close_time) + RIPPLE_EPOCH <= end_unix\n",
        "    return True\n",
    ),
    (
        "M2 collector: tx Amount used instead of delivered_amount",
        COL,
        '    delivered = meta.get("delivered_amount", meta.get("DeliveredAmount"))\n',
        '    delivered = tx.get("Amount")\n',
    ),
    (
        "M3 collector: partial-payment flag ignored",
        COL,
        '"partial": "y" if int(tx.get("Flags", 0)) & TF_PARTIAL_PAYMENT else "n"',
        '"partial": "n"',
    ),
    (
        "M4 collector: Modified node without Balance change written",
        COL,
        '            if "Balance" not in prev:\n                continue\n',
        '            if "Balance" not in prev:\n                prev = {"Balance": final.get("Balance", 0)}\n',  # noqa: E501
    ),
    (
        "M5 collector: Deleted node final balance kept",
        COL,
        '            p, f = int(prev.get("Balance", final.get("Balance", 0))), 0\n',
        '            p, f = int(prev.get("Balance", final.get("Balance", 0))), int(final.get("Balance", 0))\n',  # noqa: E501
    ),
    (
        "M6 collector: non-AccountRoot nodes counted",
        COL,
        '        if body.get("LedgerEntryType") != "AccountRoot":\n            continue\n        final =',
        "        if False:\n            continue\n        final =",
    ),
    (
        "M7 collector: orphan per-ledger rows kept on resume",
        COL,
        "            n = drop_orphan_tx(path, done)\n",
        "            n = 0\n",
    ),
    (
        "M8 collector: torn tail kept on resume",
        COL,
        "            torn = drop_torn_tail(path)\n",
        "            torn = 0\n",
    ),
    (
        "M9 collector: failed fetch never recorded as a gap",
        COL,
        "        if err:\n            append_rows(gaps_csv",
        "        if False:\n            append_rows(gaps_csv",
    ),
    (
        "M10 collector: rows not sorted by TransactionIndex",
        COL,
        '    txs.sort(key=lambda r: r["tx_index"])\n',
        "    pass\n",
    ),
    # analyzer
    (
        "M11 analyzer: failed payments counted",
        ANA,
        '    return p["result"] == "tesSUCCESS" and p["kind"] == "native" and int(p["delivered_drops"] or 0) > 0\n',  # noqa: E501
        '    return p["kind"] == "native" and int(p["delivered_drops"] or 0) > 0\n',
    ),
    (
        "M12 analyzer: gini off by one rank",
        ANA,
        "sum((i + 1) * x for i, x in enumerate(s))",
        "sum(i * x for i, x in enumerate(s))",
    ),
    (
        "M13 analyzer: half-volume threshold uses >",
        ANA,
        "        if 2 * c >= total:\n",
        "        if 2 * c > total:\n",
    ),
    (
        "M14 analyzer: reward lot not capped at delta",
        ANA,
        '                tag = min(int(p["delivered_drops"]), delta)\n',
        '                tag = int(p["delivered_drops"])\n',
    ),
    (
        "M15 analyzer: balance_first spends lots first",
        ANA,
        '            if policy == "balance_first":\n',
        '            if policy == "never":\n',
    ),
    (
        "M16 analyzer: lots LIFO instead of FIFO",
        ANA,
        "                take = min(lots[0][0], amt)\n                lots[0][0] -= take\n",
        "                take = min(lots[-1][0], amt)\n                lots[-1][0] -= take\n",
    ),
    (
        "M17 analyzer: prev-balance break not reported",
        ANA,
        "        if prev != have:\n",
        "        if False:\n",
    ),
    (
        "M18 analyzer: fee component not attributed",
        ANA,
        "        if fee[0] == acct:\n",
        "        if fee[0] == None:\n",
    ),
    (
        "M19 analyzer: final balance check dropped",
        ANA,
        '    if deltas and res["final_model"] != res["final_metadata"]:\n',
        "    if False:\n",
    ),
    # reconcile
    (
        "M20 reconcile: burn compared with fees + 1",
        REC,
        '    report(burned == fees, "C2"',
        '    report(burned == fees + 1 or True, "C2"',
    ),
    (
        "M21 reconcile: created account opening not 0",
        REC,
        "        start, end = bal_a.get(acct, 0), bal_b.get(acct, 0)\n",
        "        start, end = bal_a.get(acct, 1), bal_b.get(acct, 0)\n",
    ),
    (
        "M22 reconcile: accounts only from snapshot A",
        REC,
        "    accounts = sorted(set(bal_a) | set(bal_b) | set(per_acct))\n",
        "    accounts = sorted(set(bal_a))\n",
    ),
    (
        "M23 reconcile: chain start not checked",
        REC,
        "        prev = start\n        for d in rows:\n",
        '        prev = int(rows[0]["prev_drops"]) if rows else start\n        for d in rows:\n',
    ),
    (
        "M24 reconcile: tx conservation ignores fee",
        REC,
        '        want = -(int(t["fee_drops"]) + fee_bump)\n',
        '        want = by_tx[t["tx_hash"]]\n',
    ),
    (
        "M25 reconcile: escrow holder change ignored",
        REC,
        '    report(held_change == into_holders, "C3"',
        '    report(True, "C3"',
    ),
    (
        "M26 reconcile: sender delta check dropped",
        REC,
        '            (send == -(delivered + int(p["fee_drops"])), "sender_delta",',
        '            (True, "sender_delta",',
    ),
    (
        "M27 reconcile: snapshot adjacency not checked",
        REC,
        'int(ha["ledger_index"]) == lo - 1 and',
        "True and",
    ),
    ("M28 reconcile: negative leaves snapshot B alone", REC, "        bal_b[first] += 1\n", "        pass\n"),
    # crosscheck
    (
        "M29 crosscheck: balance changes not compared",
        XC,
        '"delivered",\n                         "balance_changes") if want[k] != got[k]]',
        '"delivered",\n                         ) if want[k] != got[k]]',
    ),
    (
        "M30 crosscheck: unvalidated answer accepted",
        XC,
        '    if not got.get("validated"):\n',
        "    if False:\n",
    ),
    (
        "M31 crosscheck: negative keeps empty delivered",
        XC,
        '        want["delivered"] = SENTINEL\n',
        '        want["delivered"] = rec["delivered"] or ""\n',
    ),
    (
        "M32 crosscheck: offline ignores recorded sample",
        XC,
        '    check(picks == rec["sample"],',
        "    check(True,",
    ),
    (
        "M33 crosscheck: failed payments not stratified",
        XC,
        "    picks = rng.sample(bad, min(failed, len(bad), count))\n",
        "    picks = []\n",
    ),
    (
        "M34 crosscheck: Created node balance ignored",
        XC,
        '                changes[new["Account"]] += int(new.get("Balance", 0))\n',
        "                pass\n",
    ),
]
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_payflow_*.py"))]


def pytest(root, timeout):
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout", f"tests did not finish in {timeout} s (hang counts as caught)"
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    return r.returncode, last


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--only", default=None, help="run only this mutant id (e.g. M7)")
    a = p.parse_args(argv)
    src = Path.cwd()
    rc, last = pytest(src, a.timeout)
    print(f"CONTROL unmutated: rc={rc} {last}", flush=True)
    if rc != 0:
        return 1
    todo = [m for m in MUTANTS if a.only is None or m[0].split()[0] == a.only]
    bad = 0
    for name, path, old, new in todo:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            shutil.copytree(
                src, root, ignore=shutil.ignore_patterns(".venv", ".git", "data", "__pycache__", "logs")
            )
            f = root / path
            text = f.read_text()
            if text.count(old) != 1:
                print(f"NOT APPLIED {name} (count {text.count(old)})", flush=True)
                bad += 1
                continue
            f.write_text(text.replace(old, new))
            rc, last = pytest(root, a.timeout)
            caught = rc != 0
            bad += not caught
            print(
                f"{'CAUGHT' if caught else 'SURVIVED'} {name}: {last} ({time.monotonic() - t0:.1f}s)",
                flush=True,
            )
    print(f"MUTANTS {len(todo)} caught={len(todo) - bad} failed={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
