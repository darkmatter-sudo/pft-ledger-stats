#!/usr/bin/env python3
"""Controlled mutation check for issued/ (collector, analyzer, crosscheck).

Each mutant puts one broken line into a temporary copy of the repo and runs the issued tests
(tests/test_issued_*.py). Verdicts:
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
(Harness adapted from objlife/mutants_objlife.py; the mutant table and test list differ.)

Usage: python3 issued/mutants_issued.py [--timeout 120] [--only A7]
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

COL = "issued/collect_issued.py"
ANA = "issued/analyze_issued.py"
XC = "issued/crosscheck_issued.py"

MUTANTS = [
    # collector
    ("C1 write method allowed", COL, "        if method not in READ_ONLY:\n", "        if False:\n"),
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
        "C5 ledger pin not enforced",
        COL,
        '        if r.get("ledger_hash") != ledger_hash:\n',
        "        if False:\n",
    ),
    (
        "C6 existing snapshot overwritten",
        COL,
        '    if os.path.exists(os.path.join(out, "MANIFEST.json")):\n',
        "    if False:\n",
    ),
    (
        "C7 index sets not compared",
        COL,
        '        same = a == b and all(o["LedgerEntryType"] == name for o in filt)\n',
        "        same = len(a) == len(a)\n",
    ),
    ("C8 differing sets accepted", COL, "    if not ok:\n", "    if False:\n"),
    ("C9 amm_info for every party", COL, "        if iss not in issuers:\n", "        if False:\n"),
    (
        "C10 sync state not checked",
        COL,
        '        if st.get("server_state") not in ("full", "validating", "proposing"):\n',
        "        if False:\n",
    ),
    # analyzer
    (
        "A1 positive balance: wrong issuer",
        ANA,
        "        return high, low, bal, False\n",
        "        return low, high, bal, False\n",
    ),
    (
        "A2 negative balance: sign kept",
        ANA,
        "        return low, high, -bal, False\n",
        "        return low, high, bal, False\n",
    ),
    (
        "A3 zero line: low-limit holder flipped",
        ANA,
        "        return high, low, Decimal(0), False\n",
        "        return low, high, Decimal(0), False\n",
    ),
    (
        "A4 zero line: high-limit holder dropped",
        ANA,
        "    if hi_lim > 0 and lo_lim == 0:\n",
        "    if False:\n",
    ),
    (
        "A5 view balance from low side only",
        ANA,
        '        "balance": num(bal if me == "Low" else -bal),\n',
        '        "balance": num(bal),\n',
    ),
    ("A6 zero balances count as holders", ANA, "        if amt > 0:\n", "        if amt >= 0:\n"),
    ("A7 over-limit uses >=", ANA, "        if amt > limit:\n", "        if amt >= limit:\n"),
    (
        "A8 issuer freeze read from holder side",
        ANA,
        '        a["issuer_froze"] += flag(line, f"{isd}Freeze")\n',
        '        a["issuer_froze"] += flag(line, f"{hsd}Freeze")\n',
    ),
    (
        "A9 holder freeze read from issuer side",
        ANA,
        '        a["holder_froze"] += flag(line, f"{hsd}Freeze")\n',
        '        a["holder_froze"] += flag(line, f"{isd}Freeze")\n',
    ),
    (
        "A10 issuer NoRipple from holder side",
        ANA,
        '        a["issuer_side_no_ripple"] += flag(line, f"{isd}NoRipple")\n',
        '        a["issuer_side_no_ripple"] += flag(line, f"{hsd}NoRipple")\n',
    ),
    (
        "A11 top holders ascending",
        ANA,
        '        bal = sorted(a.pop("holders").items(), key=lambda kv: (-kv[1], kv[0]))\n',
        '        bal = sorted(a.pop("holders").items(), key=lambda kv: (kv[1], kv[0]))\n',
    ),
    (
        "A12 HHI without squares",
        ANA,
        "        hhi = sum(((v / total) ** 2 for _, v in bal), Decimal(0)) * 10000 if total else Decimal(0)\n",
        "        hhi = sum(((v / total) for _, v in bal), Decimal(0)) * 10000 if total else Decimal(0)\n",
    ),
    (
        "A13 rippling threshold 1",
        ANA,
        "in sorted(acc.items()) if n >= 2]\n",
        "in sorted(acc.items()) if n >= 1]\n",
    ),
    (
        "A14 frozen lines counted as obligations",
        ANA,
        '        elif b < 0 and v["freeze"]:\n',
        "        elif False:\n",
    ),
    ("A15 gateway mismatch not listed", ANA, "        if not ok:\n", "        if False:\n"),
    (
        "A16 census obligations not reconciled",
        ANA,
        '        if tot != Decimal(c["obligations"]):\n',
        "        if False:\n",
    ),
    (
        "A17 object-count disagreement ignored",
        ANA,
        '        if not f["index_sets_equal"] or f["full_tree"] != f["type_filter"]:\n',
        "        if False:\n",
    ),
    (
        "A18 LP-token lines not detected",
        ANA,
        '    lp_lines = [ln["index"] for ln in snap["lines"] if ln["Balance"]["currency"].startswith("03")]\n',
        "    lp_lines = []\n",
    ),
    (
        "A19 manifest not verified",
        ANA,
        '        if hashlib.sha256(data).hexdigest() != meta["sha256"] or data.count(b"\\n") != meta["rows"]:\n',
        "        if False:\n",
    ),
    (
        "A20 B before A accepted",
        ANA,
        '        if b["header"]["ledger_index"] <= a["header"]["ledger_index"]:\n',
        "        if False:\n",
    ),
    (
        "A21 hex currency not decoded",
        ANA,
        "    if raw and all(32 <= c < 127 for c in raw):\n",
        "    if False:\n",
    ),
    ("A22 exit 0 despite mismatches", ANA, "    return 1 if mism else 0\n", "    return 0\n"),
    (
        "A23 transfer fee off by 10x",
        ANA,
        '        s["transfer_fee_pct"] = str(((Decimal(root["TransferRate"]) - 10**9) / 10**7).quantize(Q6))\n',
        '        s["transfer_fee_pct"] = str(((Decimal(root["TransferRate"]) - 10**9) / 10**8).quantize(Q6))\n',
    ),
    (
        "A24 changed lines not diffed",
        ANA,
        "        if ch:\n            changed.append",
        "        if False:\n            changed.append",
    ),
    (
        "A25 top10 = top1",
        ANA,
        '        a["top10_share"] = share(sum((v for _, v in bal[:10]), Decimal(0)), total) if bal else "0"\n',
        '        a["top10_share"] = share(bal[0][1], total) if bal else "0"\n',
    ),
    # crosscheck
    ("X1 out-of-range ledger accepted", XC, "    if not inside:\n", "    if False:\n"),
    (
        "X2 lgrNotFound never retried",
        XC,
        '                if e.error != "lgrNotFound" or attempt == 3:\n',
        "                if True:\n",
    ),
    (
        "X3 header always MATCH",
        XC,
        '        verdict(f"header.{f}", got == want, f"got={got} want={want}")\n',
        '        verdict(f"header.{f}", True, f"got={got} want={want}")\n',
    ),
    (
        "X4 extra public objects ignored",
        XC,
        "            not missing and not extra,\n",
        "            not missing,\n",
    ),
    (
        "X5 objects not compared",
        XC,
        '            verdict(f"{typ}.object {i}", g[i] == w)\n',
        '            verdict(f"{typ}.object {i}", True)\n',
    ),
    (
        "X6 gateway not compared",
        XC,
        '        verdict(f"gateway_balances {acct}", gotn == w)\n',
        '        verdict(f"gateway_balances {acct}", True)\n',
    ),
    ("X7 account_lines fields not compared", XC, "            ok = gv == want\n", "            ok = True\n"),
    ("X8 unseeded sample", XC, "    rnd = random.Random(lh)\n", "    rnd = random.Random()\n"),
    ("X9 exit 0 on mismatch", XC, "    return 1 if bad else 0\n", "    return 0\n"),
    (
        "X10 negative control alters nothing in sets",
        XC,
        '            want[flip_hex("0" * 64)] = {"index": "fake"}\n',
        "            pass\n",
    ),
]

TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_issued_*.py"))]
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
