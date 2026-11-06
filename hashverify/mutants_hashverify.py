#!/usr/bin/env python3
"""Controlled mutation check for hashverify/ (lib, header-chain verifier, tx-tree verifier).

Each mutant puts one broken line into a temporary copy of the repo and runs the hashverify tests
(tests/test_hashverify_*.py, which use real-ledger vectors in tests/fixtures/hashverify/).
Verdicts:
  CAUGHT       pytest exit 1 with at least one failed test (the summary line says "failed")
  TIMEOUT      the suite hung past --timeout; the whole process group is killed (counts as caught)
  SURVIVED     the suite passed with the mutant in place
  BROKEN       any other outcome (collection/import error, usage error): counts as a failure,
               because it proves nothing about the tests
  NOT APPLIED  the mutant's text does not occur exactly once (counts as a failure)

The control (unmutated code) runs from a copy made the same way and in the same place as the
mutants, so a copy that cannot run fails the control instead of making every mutant "caught".
(Harness copied from growth/mutants_growth.py; only the mutant table and test list differ.)

Usage: .venv/bin/python hashverify/mutants_hashverify.py [--timeout 120] [--only L7]
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

LIB, HDR, TXT = "hashverify/lib_hashverify.py", "hashverify/verify_headers.py", "hashverify/verify_txtree.py"
REP = "hashverify/report_hashverify.py"
MUTANTS = [
    # lib: hash prefixes and SHA-512Half
    ("L1 lib: ledger prefix", LIB, 'PREFIX_LEDGER = b"LWR\\x00"', 'PREFIX_LEDGER = b"LGR\\x00"'),
    ("L2 lib: tx ID prefix", LIB, 'PREFIX_TXID = b"TXN\\x00"', 'PREFIX_TXID = b"TXS\\x00"'),
    ("L3 lib: tx leaf prefix", LIB, 'PREFIX_TXNODE = b"SND\\x00"', 'PREFIX_TXNODE = b"TND\\x00"'),
    ("L4 lib: inner node prefix", LIB, 'PREFIX_INNER = b"MIN\\x00"', 'PREFIX_INNER = b"INR\\x00"'),
    ("L5 lib: SHA-512Half = second half", LIB, "digest()[:32]", "digest()[32:]"),
    # lib: header serialization
    ("L6 lib: index/coins little-endian", LIB, 'struct.pack(">IQ"', 'struct.pack("<IQ"'),
    ("L7 lib: close times little-endian", LIB, '            ">IIBB",', '            "<IIBB",'),
    (
        "L8 lib: parent_close_time and close_time swapped",
        LIB,
        '            int(led["parent_close_time"]),\n            int(led["close_time"]),\n',
        '            int(led["close_time"]),\n            int(led["parent_close_time"]),\n',
    ),
    (
        "L9 lib: transaction_hash and account_hash swapped",
        LIB,
        '        + h256(led["transaction_hash"], "transaction_hash")\n'
        '        + h256(led["account_hash"], "account_hash")\n',
        '        + h256(led["account_hash"], "account_hash")\n'
        '        + h256(led["transaction_hash"], "transaction_hash")\n',
    ),
    ("L10 lib: 32-byte hash length not enforced", LIB, "    if len(b) != 32:\n", "    if False:\n"),
    # lib: VL prefix
    ("L11 lib: VL 1-byte limit 191", LIB, "    if n <= 192:\n", "    if n < 192:\n"),
    ("L12 lib: VL 2-byte offset 192", LIB, "        n -= 193\n", "        n -= 192\n"),
    ("L13 lib: VL 2-byte limit 12479", LIB, "    if n <= 12480:\n", "    if n < 12480:\n"),
    ("L14 lib: VL 3-byte offset 12480", LIB, "        n -= 12481\n", "        n -= 12480\n"),
    ("L15 lib: VL 3-byte lead 240", LIB, "bytes([241 + (n >> 16)", "bytes([240 + (n >> 16)"),
    (
        "L16 lib: VL too-large accepted",
        LIB,
        '    raise ValueError(f"VL length {n} too large")\n',
        "    return bytes([254, 0, 0])\n",
    ),
    # lib: leaves and SHAMap
    ("L17 lib: leaf without key", LIB, "vl_prefix(len(meta)), meta, key)", "vl_prefix(len(meta)), meta)"),
    ("L18 lib: leaf without tx VL", LIB, "vl_prefix(len(tx_blob)), tx_blob,", "tx_blob,"),
    ("L19 lib: leaf without meta", LIB, "vl_prefix(len(meta)), meta, key)", "key)"),
    (
        "L20 lib: root may be a leaf",
        LIB,
        "        if len(keys) == 1 and depth > 0:\n",
        "        if len(keys) == 1:\n",
    ),
    (
        "L21 lib: nibbles low then high",
        LIB,
        "    return (b >> 4) if depth % 2 == 0 else (b & 0x0F)\n",
        "    return (b & 0x0F) if depth % 2 == 0 else (b >> 4)\n",
    ),
    (
        "L22 lib: empty child not zero",
        LIB,
        "            return ZERO\n",
        '            return bytes(31) + b"\\x01"\n',
    ),
    ("L23 lib: duplicate tx ID accepted", LIB, "        if k in leaves:\n", "        if False:\n"),
    ("L24 lib: single ledger range parsed as start", LIB, "int(b or a)", "int(a)"),
    (
        "L25 lib: overlap picks the smallest span",
        LIB,
        "hi - lo > best[1] - best[0]",
        "hi - lo < best[1] - best[0]",
    ),
    # header-chain verifier
    (
        "H1 headers: reported hash not checked",
        HDR,
        '            if comp[idx] != h["ledger_hash"].upper():\n',
        "            if False:\n",
    ),
    (
        "H2 headers: chain break not reported",
        HDR,
        "                if not parent_ok:\n",
        "                if False:\n",
    ),
    (
        "H3 headers: parent link always ok",
        HDR,
        '                parent_ok = h["parent_hash"].upper() == comp[idx - 1]\n',
        "                parent_ok = True\n",
    ),
    (
        "H4 headers: disagreement not reported",
        HDR,
        '            if src == "local" and other is not None and not agree:\n',
        "            if False:\n",
    ),
    (
        "H5 headers: gap not reported",
        HDR,
        '                findings.append(("GAP", src, idx, "-", "-"))\n',
        "                pass\n",
    ),
    (
        "H6 headers: lgrNotFound/internal not retried",
        HDR,
        'RETRY = RETRY_ERRORS | {"lgrNotFound", "internal"}',
        "RETRY = RETRY_ERRORS",
    ),
    ("H7 headers: open ledger accepted", HDR, ' or not led.get("closed", False):', ":"),
    ("H8 headers: wrong ledger answer accepted", HDR, 'if int(led["ledger_index"]) != idx or not', "if not"),
    ("H9 headers: tamper total_coins is a no-op", HDR, "str(int(h[field]) + 1)", "str(int(h[field]))"),
    (
        "H10 headers: unservable ledger aborts the run",
        HDR,
        '                out(f"FETCH_FAILED source={src} ledger={idx}: {ex}")\n                continue\n',
        "                raise\n",
    ),
    ("H11 headers: findings exit 0", HDR, "    return 1 if findings else 0\n", "    return 0\n"),
    ("H12 headers: record not written", HDR, "            if record:\n", "            if False:\n"),
    # tx-tree verifier
    (
        "T1 txtree: header bytes not compared",
        TXT,
        '    if hdr != b["ledger_data"].upper():\n',
        "    if False:\n",
    ),
    (
        "T2 txtree: binary ledger_hash not checked",
        TXT,
        '(("hash", j["ledger_hash"]), ("hash_binary", b["ledger_hash"]))',
        '(("hash", j["ledger_hash"]),)',
    ),
    (
        "T3 txtree: tx IDs compared as sets",
        TXT,
        "    if sorted(ids) != want or len(ids) != len(want):\n",
        "    if set(ids) != set(want):\n",
    ),
    (
        "T4 txtree: root not checked",
        TXT,
        '    if root != j["transaction_hash"].upper():\n',
        "    if False:\n",
    ),
    (
        "T5 txtree: slice end exclusive",
        TXT,
        "end_close - ct <= slice_seconds",
        "end_close - ct < slice_seconds",
    ),
    ("T6 txtree: slice not excluded from sample", TXT, "if i not in seen]", "if True]"),
    (
        "T7 txtree: sample ignores slice size",
        TXT,
        "    need = max(0, count - len(seen))\n",
        "    need = count\n",
    ),
    ("T8 txtree: tamper is a no-op", TXT, "^ 0x01", "^ 0x00"),
    ("T9 txtree: mismatch exits 0", TXT, "    return 1 if bad else 0\n", "    return 0\n"),
    (
        "T10 txtree: answers for another ledger accepted",
        TXT,
        'if int(j["ledger_index"]) != idx or int(b["ledger_index"]) != idx or not',
        "if not",
    ),
    (
        "T11 txtree: slice walk never stops",
        TXT,
        '        if got["selection"] == "slice_boundary":\n',
        "        if False:\n",
    ),
    (
        "T12 txtree: record not written",
        TXT,
        "            if rec_fh:\n                rec_fh.write",
        "            if False:\n                rec_fh.write",
    ),
    # report
    ("R1 report: oldest run used", REP, "max(runs)", "min(runs)"),
    ("R2 report: hash findings dropped", REP, '        if r["hash_ok"] != "True":\n', "        if False:\n"),
    (
        "R3 report: parent-link findings dropped",
        REP,
        '        if r["parent_ok"] == "False":\n',
        "        if False:\n",
    ),
    ("R4 report: tx-tree mismatches dropped", REP, "        if failed:\n", "        if False:\n"),
]
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_hashverify_*.py"))]
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
