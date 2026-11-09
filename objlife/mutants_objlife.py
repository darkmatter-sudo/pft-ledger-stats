#!/usr/bin/env python3
"""Controlled mutation check for objlife/ (collector, watcher, analyzer, crosscheck).

Each mutant puts one broken line into a temporary copy of the repo and runs the objlife tests
(tests/test_*objlife*.py). Verdicts:
  CAUGHT       pytest exit 1 with at least one failed test (the summary line says "failed");
               the failing test names are printed, so a kill by an unrelated test is visible
  TIMEOUT      the suite hung past --timeout; the whole process group is killed (counts as caught)
  SURVIVED     the suite passed with the mutant in place
  BROKEN       any other outcome (collection/import error, usage error): counts as a failure,
               because it proves nothing about the tests
  NOT APPLIED  the mutant's text does not occur exactly once (counts as a failure)

The control (unmutated code) runs from a copy made the same way and in the same place as the
mutants, so a copy that cannot run fails the control instead of making every mutant "caught".
Every copy gets its own temporary directory (no shared scratch between runs).
(Harness copied from dormancy/mutants_dormancy.py; the mutant table and test list differ.)

Usage: .venv/bin/python objlife/mutants_objlife.py [--timeout 120] [--only N3]
       (from the repo root; exit 0 = control passed and every mutant caught)
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COL = "objlife/collect_objlife.py"
WAT = "objlife/watch_objlife.py"
ANA = "objlife/analyze_objlife.py"
XC = "objlife/crosscheck_objlife.py"

MUTANTS = [
    # collector
    (
        "C1 time-bound fields ignored",
        COL,
        '        tf = [f for f in TIME_FIELDS if f in e["fields"]]\n',
        "        tf = []\n",
    ),
    (
        "C2 full-tree vs type-filter disagreement accepted",
        COL,
        "        if local != filt:\n",
        "        if False:\n",
    ),
    (
        "C3 typecount.json from another ledger trusted",
        COL,
        '            if tc.get("ledger_hash") != state["ledger_hash"]:\n',
        "            if False:\n",
    ),
    # watcher
    (
        "W1 deferred filter inverted",
        WAT,
        '            if v.get("LedgerEntryType") in DEFERRED:\n',
        '            if v.get("LedgerEntryType") not in DEFERRED:\n',
    ),
    (
        "W2 orphan lines kept on resume",
        WAT,
        "        while keep > 0 and json.loads",
        "        while False and json.loads",
    ),
    (
        "W3 txs not sorted by TransactionIndex",
        WAT,
        '    out.sort(key=lambda r: r["tx_index"])\n',
        "    pass\n",
    ),
    # analyzer: input validation
    ("A1 duplicate index accepted", ANA, '        if o["index"] in objs:\n', "        if False:\n"),
    (
        "A2 object threaded after its snapshot accepted",
        ANA,
        '        if int(o.get("PreviousTxnLgrSeq", 0)) > header["ledger_index"]:\n',
        "        if False:\n",
    ),
    (
        "A3 B before A accepted",
        ANA,
        '    if b_hdr["ledger_index"] <= a_hdr["ledger_index"]:\n',
        "    if False:\n",
    ),
    # analyzer: completeness
    (
        "A4 ledger B left out of the window",
        ANA,
        "            if a_seq < i <= b_seq:\n",
        "            if a_seq < i < b_seq:\n",
    ),
    (
        "A5 txs after B replayed",
        ANA,
        '            if not a_seq < x["ledger_index"] <= b_seq:\n',
        '            if not a_seq < x["ledger_index"]:\n',
    ),
    (
        "A6 gaps ignored",
        ANA,
        '            problems.append(f"{len(gaps)} gap ledgers in range (first {gaps[:5]})")\n',
        "            pass\n",
    ),
    ("A7 per-ledger tx count not checked", ANA, "        if per_ledger[i] != n:\n", "        if False:\n"),
    (
        "A8 duplicate tx hash accepted",
        ANA,
        '                problems.append(f"tx {h} twice")\n',
        "                pass\n",
    ),
    (
        "A9 duplicate ledgers.csv row accepted",
        ANA,
        '                    problems.append(f"ledger {i}: two ledgers.csv rows")\n',
        "                    pass\n",
    ),
    ("A10 incomplete window still reconciled", ANA, "        return out, 2, []\n", "        pass\n"),
    # analyzer: replay and checks
    (
        "A11 replay order ignores TransactionIndex",
        ANA,
        '    txs.sort(key=lambda x: (x["ledger_index"], x["tx_index"]))\n',
        '    txs.sort(key=lambda x: x["ledger_index"])\n',
    ),
    (
        "A12 create of a live object not flagged",
        ANA,
        '                    residuals.append(f"R4 create of live object: {where}")\n',
        "                    pass\n",
    ),
    (
        "A13 modify of an absent object not flagged",
        ANA,
        '                    residuals.append(f"R4 modify of absent object: {where}")\n',
        "                    pass\n",
    ),
    (
        "A14 delete of an absent object not flagged",
        ANA,
        '                    residuals.append(f"R4 delete of absent object: {where}")\n',
        "                    pass\n",
    ),
    (
        "A15 PreviousFields not checked",
        ANA,
        "                    if not same(cur, f, v):\n",
        "                    if False:\n",
    ),
    (
        "A16 DeletedNode FinalFields not checked",
        ANA,
        "                        if f not in THREAD and not same(cur, f, v):\n",
        "                        if False:\n",
    ),
    (
        "A17 created-default rule accepts any value (node checks)",
        ANA,
        '    return "_created" in cur and is_default(v)\n',
        '    return "_created" in cur\n',
    ),
    (
        "A18 created-default rule accepts any value (R2)",
        ANA,
        "        if created and f not in rep and is_default(bv):\n",
        "        if created and f not in rep:\n",
    ),
    ("A19 R2 compares threading fields", ANA, '- {"_created", *THREAD}):', '- {"_created"}):'),
    (
        "A20 R1 missing objects not reported",
        ANA,
        "    missing = sorted(set(b_objs) - set(live))\n",
        "    missing = []\n",
    ),
    (
        "A21 R1 unexpected objects not reported",
        ANA,
        "    unexpected = sorted(set(live) - set(b_objs))\n",
        "    unexpected = []\n",
    ),
    (
        "A22 R3 ignores deletions",
        ANA,
        "        if cb[t] != ca[t] + created[t] - deleted[t]\n",
        "        if cb[t] != ca[t] + created[t]\n",
    ),
    (
        "A23 R5 window-tx threading not checked",
        ANA,
        '            if (i, o.get("PreviousTxnID"), seq) not in touched:\n',
        "            if False:\n",
    ),
    (
        "A24 R5 old-object equality not checked",
        ANA,
        "        elif a_objs.get(i) != o:\n",
        "        elif False:\n",
    ),
    (
        "A25 verdict always PASS",
        ANA,
        '        "status": "FAIL" if failed else "PASS",\n',
        '        "status": "PASS",\n',
    ),
    (
        "A26 failed reconciliation exits 0",
        ANA,
        "    return out, (1 if failed else 0), rows\n",
        "    return out, 0, rows\n",
    ),
    ("A27 sample seed ignored", ANA, "    rng = random.Random(seed)\n", "    rng = random.Random(1)\n"),
    # analyzer: census numbers
    ("A28 unclaimed = Amount", ANA, 'int(o["Amount"]) - int(o.get("Balance", "0"))', 'int(o["Amount"])'),
    (
        "A29 NFT sell/buy swapped",
        ANA,
        'side = "sell" if o.get("Flags", 0) & LSF_SELL_NFTOKEN else "buy"',
        'side = "buy" if o.get("Flags", 0) & LSF_SELL_NFTOKEN else "sell"',
    ),
    (
        "A30 IOU summed through float",
        ANA,
        '+= Decimal(amount["value"])',
        '+= Decimal(float(amount["value"]))',
    ),
    (
        "A31 native summed through float",
        ANA,
        "        native[key] += int(amount)\n",
        "        native[key] += int(float(amount))\n",
    ),
    ("A32 Gini rank off by one", ANA, "(2 * (i + 1) - n - 1) * x", "(2 * i - n - 1) * x"),
    ("A33 top-5 is top-4", ANA, "sum(top[:5])", "sum(top[:4])"),
    (
        "A34 NFTokenOffer owner field lost",
        ANA,
        '{"NFTokenOffer": "Owner", "Credential": "Issuer"}',
        '{"Credential": "Issuer"}',
    ),
    (
        "A35 accepted Credential stays with issuer",
        ANA,
        '    if t == "Credential" and o.get("Flags", 0) & LSF_ACCEPTED:\n',
        "    if False:\n",
    ),
    (
        "A36 strict deadlines treated inclusive",
        ANA,
        "(o[f] < close_time if f in strict else o[f] <= close_time)",
        "o[f] <= close_time",
    ),
    (
        "A37 inclusive deadlines treated strict",
        ANA,
        "(o[f] < close_time if f in strict else o[f] <= close_time)",
        "o[f] < close_time",
    ),
    ("A38 bucket edge inclusive", ANA, "        if left < limit:\n", "        if left <= limit:\n"),
    (
        "A39 latest deadline used",
        ANA,
        "    left = min(times) - close_time\n",
        "    left = max(times) - close_time\n",
    ),
    (
        "A40 FinishAfter counted as a deadline",
        ANA,
        'DEADLINE_FIELDS = ("Expiration", "CancelAfter")',
        'DEADLINE_FIELDS = ("Expiration", "CancelAfter", "FinishAfter")',
    ),
    ("A41 finishable at FinishAfter == T", ANA, 'o["FinishAfter"] < ct:', 'o["FinishAfter"] <= ct:'),
    (
        "A42 reserve from reserve_base",
        ANA,
        '"drops": str(n * int(header["server_state_reserve_inc"])),',
        '"drops": str(n * int(header.get("server_state_reserve_base", 0))),',
    ),
    ("A43 age bucket edge inclusive", ANA, "    if age < 1000:\n", "    if age <= 1000:\n"),
    # crosscheck: snapshot mode
    (
        "X1 out-of-range ledger checked",
        XC,
        "    if not any(lo <= L <= hi for lo, hi in ranges):\n",
        "    if False:\n",
    ),
    (
        "X2 negative leaves empty types matching",
        XC,
        '            expected = sorted(expected + absent_ids(f"neg-set-{name}", 1, present))\n',
        "            expected = expected\n",
    ),
    ("X3 absent probes ignore --negative", XC, "            found == neg,\n", "            not found,\n"),
    (
        "X4 object field diff never reported",
        XC,
        "        diff = sorted(k for k in set(exp) | set(node) if exp.get(k) != node.get(k))\n",
        "        diff = []\n",
    ),
    (
        "X5 no exponential backoff",
        XC,
        "            delay = args.backoff * (2**attempt)\n",
        "            delay = args.backoff\n",
    ),
    # crosscheck: sampled tx mode
    (
        "X6 tx retention ignored",
        XC,
        '        if not any(lo <= x["ledger_index"] <= hi for lo, hi in ranges):\n',
        "        if False:\n",
    ),
    (
        "X7 negative leaves TransactionIndex",
        XC,
        '            "TransactionIndex": x["meta"]["TransactionIndex"] + (1 if args.negative else 0),\n',
        '            "TransactionIndex": x["meta"]["TransactionIndex"],\n',
    ),
    (
        "X8 AffectedNodes not compared",
        XC,
        '            "AffectedNodes": meta.get("AffectedNodes"),\n',
        '            "AffectedNodes": x["meta"]["AffectedNodes"],\n',
    ),
    (
        "X9 nothing checkable passes",
        XC,
        "    return 2 if gave_up or not results else 0\n",
        "    return 2 if gave_up else 0\n",
    ),
    (
        "X10 txnNotFound read as MATCH",
        XC,
        '            out(f"MISMATCH tx:{h} error={e}")\n',
        '            out(f"MATCH tx:{h} error={e}")\n',
    ),
]

TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_*objlife*.py"))]
IGNORE = shutil.ignore_patterns(".venv", ".git", "data", "__pycache__", "logs", ".ruff_cache", "*.png")


def pytest(root, timeout):
    """(verdict-input, summary line). Kills the whole process group on timeout."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return "timeout", f"tests did not finish in {timeout} s"
    last = (out.strip().splitlines() or ["?"])[-1]
    failed = sorted(set(re.findall(r"^FAILED (\S+?)(?: - |$)", out, re.M)))
    if failed:
        last += " | " + ", ".join(f.split("::")[-1] for f in failed[:4]) + (" ..." if len(failed) > 4 else "")
    return proc.returncode, last


def verdict(rc, last):
    if rc == "timeout":
        return "TIMEOUT"
    if rc == 0:
        return "SURVIVED"
    if rc == 1 and " failed" in last:
        return "CAUGHT"
    return "BROKEN"


def copy_repo(src, tmp):
    root = Path(tmp) / "repo"
    shutil.copytree(src, root, ignore=IGNORE)
    return root


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--only", default=None, help="run only this mutant id (e.g. C7)")
    a = p.parse_args(argv)
    src = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        rc, last = pytest(copy_repo(src, tmp), a.timeout)
    print(f"CONTROL unmutated copy: rc={rc} {last}", flush=True)
    if rc != 0 or " passed" not in last:
        print("RESULT: FAIL control did not pass")
        return 1
    todo = [m for m in MUTANTS if a.only is None or m[0].split()[0] == a.only]
    counts = {}
    for name, path, old, new in todo:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_repo(src, tmp)
            f = root / path
            text = f.read_text()
            if text.count(old) != 1:
                v, last = "NOT APPLIED", f"count {text.count(old)}"
            else:
                f.write_text(text.replace(old, new))
                rc, last = pytest(root, a.timeout)
                v = verdict(rc, last)
        counts[v] = counts.get(v, 0) + 1
        print(f"{v} {name}: {last} ({time.monotonic() - t0:.1f}s)", flush=True)
    caught = counts.get("CAUGHT", 0) + counts.get("TIMEOUT", 0)
    bad = len(todo) - caught
    print(f"MUTANTS {len(todo)} caught={caught} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"RESULT: {'PASS' if bad == 0 else 'FAIL'} ({bad} not caught)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
