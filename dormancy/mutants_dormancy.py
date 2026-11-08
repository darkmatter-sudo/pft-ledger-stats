#!/usr/bin/env python3
"""Controlled mutation check for dormancy/ (lib, header fetcher, anchors, analyzer, crosscheck).

Each mutant puts one broken line into a temporary copy of the repo and runs the dormancy tests
(tests/test_*dormancy*.py). Verdicts:
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
(Harness copied from hashverify/mutants_hashverify.py; the mutant table, test list and the
failed-test listing differ.)

Usage: .venv/bin/python dormancy/mutants_dormancy.py [--timeout 120] [--only L3]
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

LIB = "dormancy/lib_dormancy.py"
HDR = "dormancy/headers_dormancy.py"
ANC = "dormancy/anchors_dormancy.py"
ANA = "dormancy/analyze_dormancy.py"
XC = "dormancy/crosscheck_dormancy.py"

MUTANTS = [
    # lib: buckets and time map
    (
        "L1 bucket upper edge inclusive",
        LIB,
        "if age >= lo and (hi is None or age < hi):",
        "if age >= lo and (hi is None or age <= hi):",
    ),
    (
        "L2 interval misses the bucket its lower bound touches",
        LIB,
        "(age_max is None or age_max >= lo)",
        "(age_max is None or age_max > lo)",
    ),
    (
        "L3 torn headers.jsonl line trusted",
        LIB,
        "        lines = lines[:-1]  # torn last line",
        "        lines = lines  # torn last line",
    ),
    (
        "L4 anchor conflict ignored",
        LIB,
        "                if seq in out and out[seq] != (ct, h):\n",
        "                if False:\n",
    ),
    (
        "L5 backwards close_time accepted",
        LIB,
        "            if self.times[i] < self.times[i - 1]:\n",
        "            if False:\n",
    ),
    (
        "L6 seq above newest header extrapolated",
        LIB,
        "        if i == len(self.seqs):\n            raise",
        "        if False:\n            raise",
    ),
    (
        "L7 'before' rows get the oldest time as lower bound",
        LIB,
        'return ("before", None, None, self.times[0])',
        'return ("before", self.times[0], self.times[0], self.times[0])',
    ),
    (
        "L8 interpolation bound narrowed to the point",
        LIB,
        'return ("interp", t_lo, point, t_hi)',
        'return ("interp", point, point, point)',
    ),
    ("L9 interpolation weights swapped", LIB, "(seq - s_lo) / (s_hi - s_lo)", "(s_hi - seq) / (s_hi - s_lo)"),
    (
        "L10 reserve not capped at balance",
        LIB,
        '"reserve": min(bal, base + inc * oc),',
        '"reserve": base + inc * oc,',
    ),
    ("L11 reserve ignores OwnerCount", LIB, "min(bal, base + inc * oc)", "min(bal, base + inc * 0)"),
    (
        "L12 gini off by one in rank",
        LIB,
        "sum((i + 1) * x for i, x in enumerate(s))",
        "sum(i * x for i, x in enumerate(s))",
    ),
    ("L13 escrow not part of total_coins", LIB, "bal_all + esc + pc == total", "bal_all + pc == total"),
    (
        "L14 untouched-field invariant dropped",
        LIB,
        '                problems.append(f"{acct}: same PreviousTxnID but fields differ")\n',
        "                pass\n",
    ),
    (
        "L15 touched seq window not checked",
        LIB,
        '            if not ledger_a < rb["prev_seq"] <= ledger_b:\n                problems.append(\n',
        "            if False:\n                problems.append(\n",
    ),
    (
        "L16 created accounts not in matrix",
        LIB,
        "            bump(f\"created|{rb['bucket']}\")\n",
        "            pass\n",
    ),
    ("L17 objects_paged check always passes", LIB, 'len(objs) == int(header["objects_paged"]),', "True,"),
    (
        "L18 top-N share uses smallest balances",
        LIB,
        '"top_share": round(sum(sorted(bal, reverse=True)[:top_n])',
        '"top_share": round(sum(sorted(bal)[:top_n])',
    ),
    (
        "L19 accounts_min counts multi-bucket rows",
        LIB,
        'sum(1 for r in rows if r["candidates"] == [label])',
        'sum(1 for r in rows if label in r["candidates"])',
    ),
    # header fetcher
    (
        "H1 torn tail kept on resume",
        HDR,
        '    keep = data[: data.rfind(b"\\n") + 1] if b"\\n" in data else b""\n',
        "    keep = data\n",
    ),
    (
        "H2 already-fetched seqs fetched again",
        HDR,
        "    todo = sorted(seqs - have)\n",
        "    todo = sorted(seqs)\n",
    ),
    (
        "H3 source asked outside complete_ledgers",
        HDR,
        "                if not in_ranges(seq, ranges[name]):\n                    continue\n",
        "                if False:\n                    continue\n",
    ),
    (
        "H4 unvalidated ledger accepted",
        HDR,
        'if not res.get("validated", False) or int(led["ledger_index"]) != seq:',
        'if int(led["ledger_index"]) != seq:',
    ),
    ("H5 SIGTERM flag ignored", HDR, '            if STOP["flag"]:\n', "            if False:\n"),
    (
        "H6 no backoff between retries",
        HDR,
        "            sleep(backoff * (2**attempt))\n",
        "            pass\n",
    ),
    ("H7 write-only methods allowed", HDR, "        if method not in self.allow:\n", "        if False:\n"),
    (
        "H8 no fsync per header",
        HDR,
        '            fh.flush()\n            os.fsync(fh.fileno())\n            stats["fetched"]',
        '            stats["fetched"]',
    ),
    # anchors
    (
        "A1 anchor conflict ignored",
        ANC,
        "            if seq in anchors and anchors[seq] != (ct, h):\n",
        "            if False:\n",
    ),
    (
        "A2 lower bracketing anchor dropped",
        ANC,
        "            if i > 0:\n                keep.add(combined[i - 1])\n",
        "            if False:\n                keep.add(combined[i - 1])\n",
    ),
    ("A3 drill copies counted as anchors", ANC, '        if "/drill" in "/" + n:\n', "        if False:\n"),
    # analyzer
    (
        "N1 anchor vs header disagreement ignored",
        ANA,
        "        if anchors[s] != headers[s]:\n            raise",
        "        if False:\n            raise",
    ),
    (
        "N2 accounts_conserved always passes",
        ANA,
        '"ok": a["accounts"] - deleted + created == b["accounts"],',
        '"ok": True,',
    ),
    (
        "N3 transition checks ignored by the verdict",
        ANA,
        '        ok = ok and all(c["ok"] for c in res["transitions"]["checks"])\n',
        "        ok = ok\n",
    ),
    (
        "N4 snapshot checks ignored by the verdict",
        ANA,
        '    ok = all(c["ok"] for s in res["snapshots"] for c in s["checks"])\n',
        "    ok = True\n",
    ),
    # crosscheck
    (
        "X1 field mismatch never reported",
        XC,
        "        bad = [f for f in FIELDS if exp[f] != got.get(f)]\n",
        "        bad = []\n",
    ),
    (
        "X2 --negative leaves PreviousTxnID unaltered",
        XC,
        '(flip_hex(v) if k == "PreviousTxnID" else v + 1)',
        '(v if k == "PreviousTxnID" else v + 1)',
    ),
    (
        "X3 tx touches_root not checked",
        XC,
        '"touches_root": (not negative, obj["index"] in touched_roots(tx.get("meta", {}))),',
        '"touches_root": (not negative, not negative),',
    ),
    (
        "X4 short sample accepted",
        XC,
        '    if c["accounts"] < count:\n        print(',
        "    if False:\n        print(",
    ),
    ("X5 mismatches exit 0", XC, "    return (1 if mism else 0), c\n", "    return 0, c\n"),
]

TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_*dormancy*.py"))]
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
