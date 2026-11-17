#!/usr/bin/env python3
"""Controlled mutation check for acctflags/ (collector, extract, analyzer, crosscheck).

Each mutant puts one broken line into a temporary copy of the repo and runs the issued tests
(tests/test_acctflags_*.py). Verdicts:
  CAUGHT       pytest exit 1, a "N failed" summary AND at least one FAILED line; the failing test
               names are printed, so a kill by an unrelated test is visible
  TIMEOUT      the suite hung past --timeout; the whole process group is killed (counts as caught)
  SURVIVED     the suite passed with the mutant in place
  BROKEN       any other outcome (collection/import error, usage error): counts as a failure,
               because it proves nothing about the tests
  NOT APPLIED  the mutant's text does not occur exactly once (counts as a failure)

The control (unmutated code) runs first from a copy made the same way and in the same place as the
mutants and must pass, so a copy that cannot run fails the control instead of making every mutant
"caught". Every copy gets its own temporary directory (no shared scratch between runs).
(Harness copied from issued/mutants_issued.py; the mutant table and test list differ.)

Usage: python3 acctflags/mutants_flags.py [--timeout 120] [--only A7]
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

COL = "acctflags/collect_flags.py"
EXT = "acctflags/extract_flags.py"
ANA = "acctflags/analyze_flags.py"
XC = "acctflags/crosscheck_flags.py"

MUTANTS = [
    # collector
    ("C1 write method allowed", COL, "        if method not in self.allow:\n", "        if False:\n"),
    (
        "C2 one try fewer",
        COL,
        "        for attempt in range(self.retries + 1):\n",
        "        for attempt in range(self.retries):\n",
    ),
    (
        "C3 definite errors retried",
        COL,
        "                if err not in TRANSIENT:\n",
        "                if False:\n",
    ),
    (
        "C4 constant backoff",
        COL,
        "            delay = self.backoff * (2**attempt)\n",
        "            delay = self.backoff\n",
    ),
    (
        "C5 no rate limit",
        COL,
        "        if self.last is not None and now - self.last < self.interval:\n",
        "        if False:\n",
    ),
    ("C6 rate above 2 req/s accepted", COL, "        if rate <= 0 or rate > 2:\n", "        if rate <= 0:\n"),
    (
        "C7 ledger_data pin not enforced",
        COL,
        '            if r.get("ledger_hash") != ledger_hash:\n',
        "            if False:\n",
    ),
    ("C8 type filter difference accepted", COL, "    if not same:\n", "    if False:\n"),
    (
        "C9 saved page of other ledger reused",
        COL,
        '            if saved["ledger_hash"] != ledger_hash:\n',
        "            if False:\n",
    ),
    (
        "C10 parent_hash chain not checked",
        COL,
        '        if prev_hash is not None and led["parent_hash"] != prev_hash:\n',
        "        if False:\n",
    ),
    (
        "C11 unexpanded tx accepted",
        COL,
        '        if any(not isinstance(t, dict) or "metaData" not in t for t in txs):\n',
        "        if False:\n",
    ),
    (
        "C12 orphan tx lines kept",
        COL,
        '    keep = [t for t in txs if t["ledger_index"] <= done]\n',
        "    keep = list(txs)\n",
    ),
    ("C13 torn tail kept", COL, "    if good != len(data):\n", "    if False:\n"),
    (
        "C14 pruned ledger skipped silently",
        COL,
        '            if e.error == "lgrNotFound" and n <= validated:\n',
        "            if False:\n",
    ),
    (
        "C15 resume restarts at last ledger",
        COL,
        '    n = last["ledger_index"] + 1 if last else start\n',
        '    n = last["ledger_index"] if last else start\n',
    ),
    (
        "C16 unvalidated snapshot ledger accepted",
        COL,
        '        if not hdr.get("validated"):\n',
        "        if False:\n",
    ),
    # extract
    ("X1 chain gap accepted", EXT, '        if row["parent_hash"] != prev:\n', "        if False:\n"),
    (
        "X2 txn_count not checked",
        EXT,
        '        if row["txn_count"] != len(blocks[n]):\n',
        "        if False:\n",
    ),
    (
        "X3 manifest sha not checked",
        EXT,
        '            if hashlib.sha256(fh.read()).hexdigest() != meta["sha256"]:\n',
        "            if False:\n",
    ),
    ("X4 B hash not checked", EXT, '    if prev != hdr_b["ledger_hash"]:\n', "    if False:\n"),
    (
        "X5 before_flags from FinalFields",
        EXT,
        '                row["before_flags"] = int(old.get("Flags", new.get("Flags", 0)))\n',
        '                row["before_flags"] = int(new.get("Flags", 0))\n',
    ),
    (
        "X6 dest tag presence lost",
        EXT,
        '        row["dest_tag"] = "DestinationTag" in tx\n',
        '        row["dest_tag"] = False\n',
    ),
    ("X7 duplicate ledger accepted", EXT, '        if r["ledger_index"] in blocks:\n', "        if False:\n"),
    # analyze
    (
        "A1 set_by_tx not counted",
        ANA,
        '                terms[name]["set_by_tx" if now else "cleared_by_tx"] += 1\n',
        '                terms[name]["cleared_by_tx"] += 1\n',
    ),
    (
        "A2 per-account compare skipped",
        ANA,
        "        elif state[pid] != b_rows[pid]:\n",
        "        elif False:\n",
    ),
    (
        "A3 PreviousFields check skipped",
        ANA,
        '                if state[pid]["flags"] != node["before_flags"]:\n',
        "                if False:\n",
    ),
    (
        "A4 deleted accounts not removed",
        ANA,
        "                gone = state.pop(pid)\n",
        "                gone = state[pid]\n",
    ),
    (
        "A5 created-with flags ignored",
        ANA,
        '                    terms.setdefault(name, Counter())["created_with"] += 1\n',
        "                    pass\n",
    ),
    (
        "A6 retry window exclusive",
        ANA,
        '            if (p["ledger"], p["tx"]) > key and p["ledger"] - f["ledger"] <= window:\n',
        '            if (p["ledger"], p["tx"]) > key and p["ledger"] - f["ledger"] < window:\n',
    ),
    (
        "A7 retry may precede failure",
        ANA,
        '            if (p["ledger"], p["tx"]) > key and p["ledger"] - f["ledger"] <= window:\n',
        '            if p["ledger"] - f["ledger"] <= window:\n',
    ),
    (
        "A8 dest tag fix not required",
        ANA,
        '            if f["result"] == "tecDST_TAG_NEEDED" and not f["dest_tag"] and retry["dest_tag"]:\n',
        '            if f["result"] == "tecDST_TAG_NEEDED":\n',
    ),
    (
        "A9 preauth pair reversed",
        ANA,
        '                e[2] == "preauth" and e[3] == (f["dest"], f["sender"]) for e in span\n',
        '                e[2] == "preauth" and e[3] == (f["sender"], f["dest"]) for e in span\n',
    ),
    (
        "A10 censoring ignored",
        ANA,
        '                "censored": retry is None and f["ledger"] + window > b,\n',
        '                "censored": False,\n',
    ),
    ("A11 window check skipped", ANA, "    problems = check_window(d)\n", "    problems = []\n"),
    (
        "A12 offline does not block sockets",
        ANA,
        "    if a.offline:\n        no_network()\n",
        "    if a.offline:\n        pass\n",
    ),
    ("A13 mismatch exit 0", ANA, "    return 1 if n else 0\n", "    return 0\n"),
    (
        "A14 config window ignored",
        ANA,
        '    window = a.retry_window if a.retry_window is not None else cfg["retry_window_ledgers"]\n',
        "    window = a.retry_window or 50\n",
    ),
    (
        "A15 preauth totals not compared",
        ANA,
        '    if {tuple(p) for p in d["snaps"]["B"]["preauth"]} != preauth:\n',
        "    if False:\n",
    ),
    # crosscheck
    ("K1 negative control is a no-op", XC, '            exp["Flags"] ^= 1\n', "            pass\n"),
    (
        "K2 actNotFound treated as error",
        XC,
        '            got = None if e.error == "actNotFound" else {"error": e.error}\n',
        '            got = {"error": e.error}\n',
    ),
    ("K3 public range not enforced", XC, "    if not inside:\n", "    if False:\n"),
    (
        "K4 header not compared",
        XC,
        '    ok = led["ledger_hash"] == lh and int(led["ledger_index"]) == li\n',
        "    ok = True\n",
    ),
    ("K5 settings not compared", XC, '        "settings": X.settings_of(o),\n', '        "settings": {},\n'),
]

TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_acctflags_*.py"))]
IGNORE = shutil.ignore_patterns(
    ".venv", ".git", "data", "__pycache__", "logs", ".ruff_cache", "*.png", "charts"
)


def pytest(root, timeout):
    """(rc or 'timeout', summary line, failed test names). Kills the whole process group on timeout."""
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
        return "timeout", f"tests did not finish in {timeout} s", []
    last = (out.strip().splitlines() or ["?"])[-1]
    failed = sorted(set(re.findall(r"^FAILED (\S+?)(?: - |$)", out, re.M)))
    return proc.returncode, last, failed


def verdict(rc, last, failed):
    if rc == "timeout":
        return "TIMEOUT"
    if rc == 0:
        return "SURVIVED"
    if rc == 1 and re.search(r"\b\d+ failed\b", last) and failed:
        return "CAUGHT"
    return "BROKEN"


def copy_repo(src, tmp):
    root = Path(tmp) / "repo"
    shutil.copytree(src, root, ignore=IGNORE)
    return root


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--only", default=None, help="run only this mutant id (e.g. A7)")
    a = p.parse_args(argv)
    src = Path.cwd()
    print(f"TESTS {' '.join(TESTS)}", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        rc, last, failed = pytest(copy_repo(src, tmp), a.timeout)
    print(f"CONTROL unmutated copy: rc={rc} {last}", flush=True)
    if rc != 0 or " passed" not in last or failed:
        print("RESULT: FAIL control did not pass")
        return 1
    todo = [m for m in MUTANTS if a.only is None or m[0].split()[0] == a.only]
    counts = {}
    for name, path, old, new in todo:
        t0 = time.monotonic()
        failed = []
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_repo(src, tmp)
            f = root / path
            text = f.read_text()
            if text.count(old) != 1:
                v, last = "NOT APPLIED", f"count {text.count(old)}"
            else:
                f.write_text(text.replace(old, new))
                rc, last, failed = pytest(root, a.timeout)
                v = verdict(rc, last, failed)
        counts[v] = counts.get(v, 0) + 1
        kills = ", ".join(x.split("::")[-1] for x in failed[:4]) + (" ..." if len(failed) > 4 else "")
        print(
            f"{v} {name}: {last}{' | ' + kills if kills else ''} ({time.monotonic() - t0:.1f}s)", flush=True
        )
    caught = counts.get("CAUGHT", 0) + counts.get("TIMEOUT", 0)
    bad = len(todo) - caught
    print(f"MUTANTS {len(todo)} caught={caught} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"RESULT: {'PASS' if bad == 0 else 'FAIL'} ({bad} not caught)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
