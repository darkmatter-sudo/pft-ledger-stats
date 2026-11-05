#!/usr/bin/env python3
"""Controlled mutation check for growth/ (collector, analyzer, crosscheck, statebytes).

Each mutant puts one broken line into a temporary copy of the repo and runs the growth tests.
Verdicts:
  CAUGHT       pytest exit 1 with at least one failed test (the summary line says "failed")
  TIMEOUT      the suite hung past --timeout; the whole process group is killed (counts as caught)
  SURVIVED     the suite passed with the mutant in place
  BROKEN       any other outcome (collection/import error, usage error): counts as a failure,
               because it proves nothing about the tests
  NOT APPLIED  the mutant's text does not occur exactly once (counts as a failure)

The control (unmutated code) runs from a copy made the same way and in the same place as the
mutants, so a copy that cannot run fails the control instead of making every mutant "caught".

Usage: .venv/bin/python growth/mutants_growth.py [--timeout 120] [--only C7]
       (from the repo root; exit 0 = control passed and every mutant caught)
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COL, ANA = "growth/collect_growth.py", "growth/analyze_growth.py"
XC, SB = "growth/crosscheck_growth.py", "growth/statebytes_growth.py"
MUTANTS = [
    # collector
    (
        "C1 collector: window end not enforced",
        COL,
        "    return int(close_time) + RIPPLE_EPOCH <= end_unix\n",
        "    return True\n",
    ),
    (
        "C2 collector: binary header hash not checked",
        COL,
        '    if sha512half(b"LWR\\x00", bled["ledger_data"]) != jled["ledger_hash"].upper():\n',
        "    if False:\n",
    ),
    (
        "C3 collector: duplicate binary tx accepted",
        COL,
        "        if h in sizes:\n            raise",
        "        if False:\n            raise",
    ),
    (
        "C4 collector: JSON/binary tx count not compared",
        COL,
        "    if len(jtx) != len(sizes):\n",
        "    if False:\n",
    ),
    (
        "C5 collector: JSON tx without binary twin accepted",
        COL,
        "        if h not in sizes:\n            raise",
        "        if False:\n            raise",
    ),
    ("C6 collector: meta bytes = hex length", COL, 'len(t["meta"]) // 2)', 'len(t["meta"]))'),
    (
        "C7 collector: header bytes = hex length",
        COL,
        '"header_bytes": len(bled["ledger_data"]) // 2,',
        '"header_bytes": len(bled["ledger_data"]),',
    ),
    (
        "C8 collector: DeletedNode counted as modified",
        COL,
        '"DeletedNode": "deleted"}',
        '"DeletedNode": "modified"}',
    ),
    (
        "C9 collector: orphan tx/nodes rows kept on resume",
        COL,
        "            k = drop_orphan_tx(path[n], done)\n",
        "            k = 0\n",
    ),
    (
        "C10 collector: torn tail kept on resume",
        COL,
        "            torn = drop_torn_tail(fp)\n",
        "            torn = 0\n",
    ),
    (
        "C11 collector: failed ledger never written to gaps.csv",
        COL,
        '[{"ledger_index": nxt, "error": err, "logged_at": utcnow()}]',
        "[]",
    ),
    (
        "C12 collector: no public-RPC fallback",
        COL,
        '        sources = [("local", rpc)] + ([("public", pub)] if pub else [])\n',
        '        sources = [("local", rpc)]\n',
    ),
    ("C13 collector: public backoff not exponential", COL, "a.backoff * 2 ** (attempt - 1)", "a.backoff"),
    (
        "C14 collector: wrong-ledger answer accepted",
        COL,
        '    if not j.get("closed", True) or int(j["ledger_index"]) != idx:\n',
        "    if False:\n",
    ),
    (
        "C15 collector: resume restarts at window start",
        COL,
        "        nxt = done + 1\n",
        '        nxt = state["start_seq"]\n',
    ),
    # analyzer
    ("A1 analyzer: unlisted holes not reported", ANA, "    if unlisted:\n", "    if False:\n"),
    ("A2 analyzer: duplicate ledgers not reported", ANA, "    if dup:\n", "    if False:\n"),
    (
        "A3 analyzer: tx rows vs txn_count not checked",
        ANA,
        '        if tx_per.get(i, 0) != int(r["txn_count"]):\n',
        "        if False:\n",
    ),
    (
        "A4 analyzer: reconcile ignores deleted objects",
        ANA,
        "        want = ca.get(t, 0) + created.get(t, 0) - deleted.get(t, 0)\n",
        "        want = ca.get(t, 0) + created.get(t, 0)\n",
    ),
    (
        "A5 analyzer: snapshot A adjacency not checked",
        ANA,
        '    if int(ha["ledger_index"]) != first - 1:\n',
        "    if False:\n",
    ),
    (
        "A6 analyzer: mean interval divides by n",
        ANA,
        "    interval = (ct[-1] - ct[0]) / (len(ct) - 1)\n",
        "    interval = (ct[-1] - ct[0]) / len(ct)\n",
    ),
    (
        "A7 analyzer: non-full NuDB samples kept",
        ANA,
        'if r["server_state"] != "full" or (start',
        "if False or (start",
    ),
    (
        "A8 analyzer: NuDB samples outside the window kept",
        ANA,
        "(start is not None and e < start) or (end is not None and e > end)",
        "False",
    ),
    ("A9 analyzer: retained ledgers off by one", ANA, '"retained": hi - lo + 1,', '"retained": hi - lo,'),
    (
        "A10 analyzer: raw projection without state bytes",
        ANA,
        "    raw_proj = state_b + mean_ret * raw_per_ledger\n",
        "    raw_proj = mean_ret * raw_per_ledger\n",
    ),
    (
        "A11 analyzer: low-r2 NuDB fit still used",
        ANA,
        '    if fn and fn["r2"] < MIN_R2:\n',
        "    if False:\n",
    ),
    ("A12 analyzer: retained_max = N", ANA, '"retained_max": 2 * n,', '"retained_max": n,'),
    (
        "A13 analyzer: sizing raw bytes without state",
        ANA,
        '"raw_bytes": state_b + ret * raw_per_ledger,',
        '"raw_bytes": ret * raw_per_ledger,',
    ),
    (
        "A14 analyzer: --check passes with problems",
        ANA,
        'return 0 if same and not res["problems"] else 1',
        "return 0 if same else 1",
    ),
    ("A15 analyzer: r2 always 1", ANA, "    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0\n", "    r2 = 1.0\n"),
    # crosscheck
    (
        "X1 crosscheck: tx rows not compared",
        XC,
        '    if want_tx != {norm(r, TX_FIELDS) for r in got["tx"]}:\n',
        "    if False:\n",
    ),
    (
        "X2 crosscheck: node rows not compared",
        XC,
        "    if want_nodes != Counter(",
        "    if False and want_nodes != Counter(",
    ),
    (
        "X3 crosscheck: negative leaves header_bytes alone",
        XC,
        '        want_led["header_bytes"] = str(int(want_led["header_bytes"]) + 1)\n',
        "        pass\n",
    ),
    (
        "X4 crosscheck: busy ledgers not stratified",
        XC,
        "    pick = rng.sample(busy, min(count // 2, len(busy)))\n",
        "    pick = []\n",
    ),
    ("X5 crosscheck: prune guard off", XC, "        if r_lo2 > c_lo:\n", "        if False:\n"),
    (
        "X6 crosscheck: no refetch on disagreeing answers",
        XC,
        "            if attempt == join_retries:\n",
        "            if True:\n",
    ),
    # statebytes
    (
        "S1 statebytes: first field not checked",
        SB,
        '    if len(hexdata) < 6 or hexdata[:2].upper() != "11":\n',
        "    if len(hexdata) < 6:\n",
    ),
    (
        "S2 statebytes: hash change between pages accepted",
        SB,
        '        elif res["ledger_hash"] != ledger_hash:\n',
        "        elif False:\n",
    ),
    (
        "S3 statebytes: join ignores objects missing in binary",
        SB,
        "    for idx in sorted(set(types) - set(seen)):\n",
        "    for idx in []:\n",
    ),
    (
        "S4 statebytes: join ignores type mismatch",
        SB,
        "        if seen[idx] != types[idx]:\n",
        "        if False:\n",
    ),
    ("S5 statebytes: bytes = hex length", SB, '"bytes": len(h) // 2}', '"bytes": len(h)}'),
]
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_growth_*.py"))]
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
