#!/usr/bin/env python3
"""Controlled mutation check for statereplay/ (replay, extract, hotspots, crosscheck).

Each mutant puts one broken line into a temporary copy of the repo and runs the statereplay tests
(tests/test_statereplay_*.py). Verdicts:
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
(Harness engine copied from acctflags/mutants_flags.py; the mutant table and test list differ.)

Usage: python3 statereplay/mutants_replay.py [--timeout 120] [--only R7]
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

REP = "statereplay/replay.py"
EXT = "statereplay/extract_replay.py"
HOT = "statereplay/hotspots.py"
XC = "statereplay/crosscheck_replay.py"

MUTANTS = [
    # replay: window integrity
    ("R1 duplicate ledger row accepted", REP, "            if n in by_row:\n", "            if False:\n"),
    ("R2 duplicate tx line accepted", REP, "            if n in by_tx:\n", "            if False:\n"),
    ("R3 missing ledger row accepted", REP, "        if n not in by_row:\n", "        if False:\n"),
    ("R4 missing tx line accepted", REP, "        if n not in by_tx:\n", "        if False:\n"),
    (
        "R5 anchor A not checked",
        REP,
        '        if n - 1 == ha["ledger_index"] and prev["ledger_hash"] != ha["ledger_hash"]:\n',
        "        if False:\n",
    ),
    (
        "R6 chain not checked",
        REP,
        '        if row["parent_hash"] != prev["ledger_hash"]:\n',
        "        if False:\n",
    ),
    ("R7 tx count not checked", REP, '        if row["txn_count"] != len(ltxs):\n', "        if False:\n"),
    (
        "R8 TransactionIndex gaps accepted",
        REP,
        '        if [t["metaData"]["TransactionIndex"] for t in ordered] != list(range(len(ltxs))):\n',
        "        if False:\n",
    ),
    (
        "R9 capture order instead of TransactionIndex order",
        REP,
        '        ordered = sorted(ltxs, key=lambda t: t["metaData"]["TransactionIndex"])\n',
        "        ordered = list(ltxs)\n",
    ),
    (
        "R10 anchor B not checked",
        REP,
        '        if n == last and row["ledger_hash"] != hb["ledger_hash"]:\n',
        "        if False:\n",
    ),
    ("R11 empty window accepted", REP, "    if last < first:\n", "    if False:\n"),
    # replay: derived writes
    ("R12 no long skip list", REP, "    if prev > 0 and prev % 256 == 0:\n", "    if False:\n"),
    (
        "R13 short skip list not capped",
        REP,
        '    obj["Hashes"] = hashes[-256:]\n',
        '    obj["Hashes"] = hashes\n',
    ),
    (
        "R14 short skip list LastLedgerSequence off by one",
        REP,
        '    obj["Hashes"] = hashes[-256:]\n    obj["LastLedgerSequence"] = prev\n',
        '    obj["Hashes"] = hashes[-256:]\n    obj["LastLedgerSequence"] = seq\n',
    ),
    ("R15 long skip key wrong group", REP, "(prev >> 16).to_bytes(4", "(prev >> 8).to_bytes(4"),
    (
        "R16 skip list before transactions",
        REP,
        '        advance_skip_lists(state, n, row["parent_hash"])\n        stats["ledgers"] += 1\n',
        '        stats["ledgers"] += 1\n',
    ),
    ("R17 NegativeUNL on any ledger", REP, "    if seq % 256 != 0:\n", "    if False:\n"),
    (
        "R18 NegativeUNL re-enable ignored",
        REP,
        '        if to_re is not None and v["DisabledValidator"]["PublicKey"] == to_re:\n',
        "        if False:\n",
    ),
    (
        "R19 NegativeUNL disable wrong first ledger",
        REP,
        '"FirstLedgerSequence": seq,',
        '"FirstLedgerSequence": seq - 1,',
    ),
    ("R20 NegativeUNL never erased", REP, "    if not new:\n", "    if False:\n"),
    ("R21 NegativeUNL pending kept", REP, '    obj.pop("ValidatorToDisable", None)\n', "    pass\n"),
    (
        "R22 NegativeUNL not applied in replay",
        REP,
        "        nunl = advance_negative_unl(state, n)\n",
        "        nunl = None\n",
    ),
    # replay: node rules
    (
        "R23 create over existing allowed",
        REP,
        '        if cur is not None:\n            raise ReplayError("CREATE_EXISTS"',
        '        if False:\n            raise ReplayError("CREATE_EXISTS"',
    ),
    ("R24 no create defaults", REP, "        obj.update(CREATE_DEFAULTS.get(etype, {}))\n", "        pass\n"),
    (
        "R25 created node not threaded",
        REP,
        '        if threaded(etype):\n            obj["PreviousTxnID"]',
        '        if False:\n            obj["PreviousTxnID"]',
    ),
    (
        "R26 missing node ignored",
        REP,
        '    if cur is None:\n        raise ReplayError("NODE_MISSING"',
        '    if cur is None:\n        cur = {}\n    if False:\n        raise ReplayError("NODE_MISSING"',
    ),
    ("R27 type not checked", REP, '    if cur.get("LedgerEntryType") != etype:\n', "    if False:\n"),
    (
        "R28 PreviousFields not checked",
        REP,
        '        if cur.get(k) != v:\n            raise ReplayError("PREVIOUS_MISMATCH"',
        '        if False:\n            raise ReplayError("PREVIOUS_MISMATCH"',
    ),
    (
        "R29 unchanged FinalFields not checked",
        REP,
        "        if k not in prevf and k in cur and cur[k] != v:\n",
        "        if False:\n",
    ),
    (
        "R30 added field rejected",
        REP,
        "        if k not in prevf and k in cur and cur[k] != v:\n",
        "        if k not in prevf and cur.get(k) != v:\n",
    ),
    (
        "R31 thread not checked",
        REP,
        '    if "PreviousTxnID" in body and cur.get("PreviousTxnID") != body["PreviousTxnID"]:\n',
        "    if False:\n",
    ),
    ("R32 deleted node kept", REP, "        del state[idx]\n", "        pass\n"),
    ("R33 unknown node kind applied", REP, '    if kind != "ModifiedNode":\n', "    if False:\n"),
    ("R34 removed fields kept", REP, "            new.pop(k, None)\n", "            pass\n"),
    (
        "R35 modified thread not advanced",
        REP,
        '        new["PreviousTxnID"], new["PreviousTxnLgrSeq"] = tx_hash, seq\n',
        "        pass\n",
    ),
    (
        "R36 DirectoryNode not threaded",
        REP,
        '    return etype != "LedgerHashes"\n',
        '    return etype not in ("LedgerHashes", "DirectoryNode")\n',
    ),
    ("R37 compare ignores field differences", REP, "        elif s != t:\n", "        elif False:\n"),
    (
        "R38 snapshot page of another ledger accepted",
        REP,
        '        if page.get("ledger_hash") != header["ledger_hash"]:\n',
        "        if False:\n",
    ),
    # extract
    (
        "X1 checksum not verified",
        EXT,
        "    return hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]\n",
        "    return True\n",
    ),
    ("X2 free text published", EXT, "            if field in FREE_TEXT:\n", "            if False:\n"),
    (
        "X3 addresses published",
        EXT,
        '            if o.startswith("r") and is_address(o):\n',
        "            if False:\n",
    ),
    ("X4 manifest mismatch accepted", EXT, '        if got != info["sha256"]:\n', "        if False:\n"),
    ("X5 missing manifest accepted", EXT, "    if not os.path.exists(path):\n", "    if False:\n"),
    ("X6 gzip timestamp kept", EXT, "mtime=0)", "mtime=None)"),
    ("X7 missing ledgers accepted", EXT, "    if missing:\n", "    if False:\n"),
    (
        "X8 key file world-readable",
        EXT,
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
        "os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)",
    ),
    ("X9 subset drops bystanders", EXT, "    return keep | set(rest[:bystanders])\n", "    return keep\n"),
    # hotspots
    ("H1 p95 is p90", HOT, "nearest_rank(vals, 95)", "nearest_rank(vals, 90)"),
    (
        "H2 nearest rank off by one",
        HOT,
        "    return sorted_vals[int(k) - 1]\n",
        "    return sorted_vals[min(int(k), len(sorted_vals) - 1)]\n",
    ),
    (
        "H3 net-unchanged includes thread",
        HOT,
        "    return {k: v for k, v in obj.items() if k not in THREAD}\n",
        "    return dict(obj)\n",
    ),
    ("H4 book dirs counted as owner dirs", HOT, '"ExchangeRate" in fields else', "False else"),
    (
        "H5 created page counts previous",
        HOT,
        '        if kind == "CreatedNode":\n            before = set()\n',
        "        if False:\n            before = set()\n",
    ),
    (
        "H6 deleted page keeps indexes",
        HOT,
        '        if kind == "DeletedNode":\n            after = set()\n',
        "        if False:\n            after = set()\n",
    ),
    (
        "H7 ephemeral ignores create",
        HOT,
        '            elif kind == "DeletedNode" and idx in self.created:\n',
        '            elif kind == "DeletedNode":\n',
    ),
    (
        "H8 long skip updates not counted",
        HOT,
        '            1 for n in range(stats["first"], stats["last"] + 1) if (n - 1) % 256 == 0\n',
        '            0 for n in range(stats["first"], stats["last"] + 1) if (n - 1) % 256 == 0\n',
    ),
    (
        "H9 mismatch reported as exact",
        HOT,
        '"result": "EXACT" if not diffs else "MISMATCH",',
        '"result": "EXACT",',
    ),
    # crosscheck
    (
        "K1 ledger hash not compared",
        XC,
        '        if led.get("ledger_hash") != want_hash:\n',
        "        if False:\n",
    ),
    (
        "K2 tx count not compared",
        XC,
        '            if len(got_tx) != row["txn_count"]:\n',
        "            if False:\n",
    ),
    ("K3 tx hashes not compared", XC, "            if got_tx != tx_hashes[n]:\n", "            if False:\n"),
    (
        "K4 object not compared",
        XC,
        '        verdict = "MATCH" if got == want else "MISMATCH"\n',
        '        verdict = "MATCH"\n',
    ),
    ("K5 public range not enforced", XC, "        if not XF.in_range(complete, n):\n", "        if False:\n"),
    (
        "K6 negative control not altered",
        XC,
        "        want = alter(target[idx]) if negative else target[idx]\n",
        "        want = target[idx]\n",
    ),
    (
        "K7 A and B not always drawn",
        XC,
        "    return sorted({first, last, *picked})\n",
        "    return sorted(picked)\n",
    ),
    (
        "K8 object sample unseeded",
        XC,
        "    return sorted(random.Random(seed).sample(",
        "    return sorted(random.Random().sample(",
    ),
    (
        "K9 negative run passes on matches",
        XC,
        '    ok = summary["mismatch"] == 0 if not negative else summary["match"] == 0\n',
        '    ok = summary["mismatch"] == 0\n',
    ),
]

TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_statereplay_*.py"))]
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
