#!/usr/bin/env python3
"""Controlled mutation check for statehash/ (lib, replay, collector, report, crosscheck).

Each mutant puts one broken line into a temporary copy of the repo and runs the statehash tests
(tests/test_statehash_*.py). Verdicts:
  CAUGHT       pytest exit 1, a "N failed" summary AND at least one FAILED line; the failing test
               names are printed, so a kill by an unrelated test is visible
  TIMEOUT      the suite hung past --timeout; the whole process group is killed (counts as caught)
  SURVIVED     the suite passed with the mutant in place
  BROKEN       any other outcome (collection/import error, usage error): counts as a failure
  NOT APPLIED  the mutant's text does not occur exactly once (counts as a failure)

The control (unmutated code) runs first from a copy made the same way and in the same place as the
mutants and must pass. Every copy gets its own temporary directory. Results are printed as each mutant
finishes. (Engine from statereplay/mutants_replay.py; the copy keeps statehash/data/, which the
tests read, and drops only the repo's top-level raw data/.)

Usage: python3 statehash/mutants_statehash.py [--timeout 120] [--only H3]
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

LIB = "statehash/lib_statehash.py"
REP = "statehash/replay_statehash.py"
COL = "statehash/collect_statehash.py"
RPT = "statehash/report_statehash.py"
XC = "statehash/crosscheck_statehash.py"

MUTANTS = [
    # hashing
    ("H1 leaf prefix wrong", LIB, 'PREFIX_LEAF = b"MLN\\x00"\n', 'PREFIX_LEAF = b"MLN\\x01"\n'),
    ("H2 inner prefix wrong", LIB, 'PREFIX_INNER = b"MIN\\x00"\n', 'PREFIX_INNER = b"MIN\\x01"\n'),
    (
        "H3 leaf hashes key before data",
        LIB,
        "    return sha512half(PREFIX_LEAF, data, index)\n",
        "    return sha512half(PREFIX_LEAF, index, data)\n",
    ),
    (
        "H4 leaf hash omits key",
        LIB,
        "    return sha512half(PREFIX_LEAF, data, index)\n",
        "    return sha512half(PREFIX_LEAF, data)\n",
    ),
    (
        "H5 nibble halves swapped",
        LIB,
        "    return (b >> 4) if depth % 2 == 0 else (b & 0x0F)\n",
        "    return (b & 0x0F) if depth % 2 == 0 else (b >> 4)\n",
    ),
    ("H6 nibble byte index off", LIB, "    b = key[depth // 2]\n", "    b = key[(depth + 1) // 2]\n"),
    (
        "H7 empty map not zero",
        LIB,
        "    if not leaves:\n        return ZERO\n",
        "    if not leaves:\n        return sha512half(PREFIX_INNER, *([ZERO] * 16))\n",
    ),
    ("H8 scratch: lone key not a leaf", LIB, "            elif j - i == 1:\n", "            elif False:\n"),
    (
        "H9 scratch: empty child not zero",
        LIB,
        "                kids.append(ZERO)\n",
        '                kids.append(b"\\x01" * 32)\n',
    ),
    (
        "H10 scratch: inner depth not advanced",
        LIB,
        "                kids.append(inner(i, j, depth + 1))\n",
        "                kids.append(inner(i, j, depth + 2))\n",
    ),
    (
        "H11 inner children reversed",
        LIB,
        "        return sha512half(PREFIX_INNER, *kids)\n",
        "        return sha512half(PREFIX_INNER, *kids[::-1])\n",
    ),
    (
        "H12 incremental empty not zero",
        LIB,
        "        if self.size == 0:\n            return ZERO\n",
        "        if False:\n            return ZERO\n",
    ),
    (
        "H13 incremental inner children reversed",
        LIB,
        "            node.hash = sha512half(PREFIX_INNER, *(ZERO if k is None else self._hash(k) for k in node.kids))\n",
        "            node.hash = sha512half(PREFIX_INNER, *(ZERO if k is None else self._hash(k) for k in node.kids[::-1]))\n",
    ),
    # incremental set / split / delete-collapse
    (
        "C1 set does not invalidate path hash",
        LIB,
        "        while True:\n            node.hash = None\n",
        "        while True:\n            pass\n",
    ),
    (
        "C2 split puts resident leaf at same depth",
        LIB,
        "            split.kids[nibble(child.key, depth + 1)] = child\n",
        "            split.kids[nibble(child.key, depth)] = child\n",
    ),
    ("C3 replace adds a second leaf", LIB, "            if child.key == key:\n", "            if False:\n"),
    (
        "C4 delete does not invalidate path",
        LIB,
        "        for n, _ in path:\n            n.hash = None\n",
        "        for n, _ in path[:1]:\n            n.hash = None\n",
    ),
    (
        "C5 delete never collapses lone leaf",
        LIB,
        "            if len(kids) == 1 and isinstance(kids[0], Leaf):\n",
        "            if False:\n",
    ),
    (
        "C6 delete collapses lone inner child too",
        LIB,
        "            if len(kids) == 1 and isinstance(kids[0], Leaf):\n",
        "            if len(kids) == 1:\n",
    ),
    (
        "C8 collapse stops after one level",
        LIB,
        "        for level in range(len(path) - 1, 0, -1):\n",
        "        for level in range(len(path) - 1, max(0, len(path) - 2), -1):\n",
    ),
    (
        "C9 walk leaf depth off by one",
        LIB,
        '                    yield ("leaf", depth + 1, k.key, k.data)\n',
        '                    yield ("leaf", depth, k.key, k.data)\n',
    ),
    # codec
    (
        "K1 field id nibbles swapped",
        LIB,
        "        return bytes([(t << 4) | n])\n",
        "        return bytes([(n << 4) | t])\n",
    ),
    (
        "K2 VL 2-byte length off",
        LIB,
        "        return 193 + (b0 - 193) * 256 + buf[i + 1], i + 2\n",
        "        return 192 + (b0 - 193) * 256 + buf[i + 1], i + 2\n",
    ),
    ("K3 issued amount read as native", LIB, "        if buf[i] & 0x80:\n", "        if buf[i] & 0x00:\n"),
    (
        "K4 canonical order not enforced",
        LIB,
        '            raise CodecError(f"field {t}.{n} out of canonical order")\n',
        "            pass\n",
    ),
    (
        "K5 serialize keeps insertion order",
        LIB,
        '    return b"".join(field_id_bytes(*fid) + fields[fid] for fid in sorted(fields))\n',
        '    return b"".join(field_id_bytes(*fid) + fields[fid] for fid in fields)\n',
    ),
    # replay
    (
        "R1 root never checked per ledger",
        REP,
        '        if root != header["account_hash"]:\n',
        "        if False:\n",
    ),
    (
        "R2 skip list not advanced",
        REP,
        '        self.advance_skip_lists(seq, header["parent_hash"])\n',
        "        pass\n",
    ),
    (
        "R3 short skip list not trimmed",
        REP,
        "                hashes = hashes[-keep:]\n",
        "                hashes = hashes[-keep - 1 :]\n",
    ),
    (
        "R4 long skip list never written",
        REP,
        "            push(long_skip_key(prev), None)\n",
        "            pass\n",
    ),
    (
        "R5 long skip list off boundary",
        REP,
        "        if prev > 0 and prev % 256 == 0:\n",
        "        if prev > 0 and seq % 256 == 0:\n",
    ),
    (
        "R6 NegativeUNL applied off flag ledgers",
        REP,
        "        if seq % 256 != 0:\n            return None\n        F = self.F\n",
        "        if False:\n            return None\n        F = self.F\n",
    ),
    (
        "R7 NegativeUNL re-enable ignored",
        REP,
        '        new = [(n, e) for n, e in elems if to_re is None or e.get(F["PublicKey"]) != to_re]\n',
        "        new = list(elems)\n",
    ),
    (
        "R8 create defaults dropped",
        REP,
        '            for name, width in CREATE_DEFAULTS["*"] + CREATE_DEFAULTS.get(etype, []):\n',
        "            for name, width in []:\n",
    ),
    (
        "R9 create not threaded",
        REP,
        '            if threaded(etype):\n                new[F["PreviousTxnID"]]',
        '            if False:\n                new[F["PreviousTxnID"]]',
    ),
    (
        "R10 modify not threaded",
        REP,
        '        if threaded(etype) and (F["PreviousTxnID"] in body or F["PreviousTxnID"] in cur):\n',
        "        if False:\n",
    ),
    ("R11 removed fields kept", REP, "                new.pop(k, None)\n", "                pass\n"),
    ("R12 final fields not applied", REP, "        new.update(final)\n", "        pass\n"),
    (
        "R13 previous fields not checked",
        REP,
        '                raise ReplayError("PREVIOUS_MISMATCH", seq, tx_id, idx, self.d.name(k))\n',
        "                pass\n",
    ),
    (
        "R14 unchanged fields not checked",
        REP,
        '                raise ReplayError("UNCHANGED_MISMATCH", seq, tx_id, idx, self.d.name(k))\n',
        "                pass\n",
    ),
    (
        "R15 thread not checked",
        REP,
        '            raise ReplayError("THREAD_MISMATCH", seq, tx_id, idx)\n',
        "            pass\n",
    ),
    (
        "R16 create over existing",
        REP,
        '                raise ReplayError("CREATE_EXISTS", seq, tx_id, idx)\n',
        "                pass\n",
    ),
    (
        "R17 TransactionIndex order ignored",
        REP,
        "        metas.sort(key=lambda m: m[0])\n",
        "        metas.sort(key=lambda m: m[1])\n",
    ),
    ("R18 chain not checked", REP, '            raise ReplayError("CHAIN_BREAK", n)\n', "            pass\n"),
    (
        "R19 header hash not checked",
        REP,
        '            raise ReplayError("HEADER_HASH", n)\n',
        "            pass\n",
    ),
    (
        "R20 missing ledger accepted",
        REP,
        '            raise ReplayError("LEDGER_MISSING", n)\n',
        "            continue\n",
    ),
    ("R21 B blob mismatch ignored", REP, "        elif s != t:\n", "        elif False:\n"),
    ("R22 extra object in replay ignored", REP, "        elif t is None:\n", "        elif False:\n"),
    ("R23 anchor B not checked", REP, '        raise ReplayError("ANCHOR_B_HASH", last)\n', "        pass\n"),
    # collector
    ("S1 capture tx root not checked", COL, '    if root != h["transaction_hash"]:\n', "    if False:\n"),
    (
        "S2 capture parent not checked",
        COL,
        '    if prev_hash is not None and h["parent_hash"] != prev_hash:\n',
        "    if False:\n",
    ),
    (
        "S3 capture header hash not checked",
        COL,
        '    if lh != str(r.get("ledger_hash", "")).upper():\n',
        "    if False:\n",
    ),
    (
        "S4 snapshot duplicate index accepted",
        COL,
        '    if len({r["index"] for r in rows}) != len(rows):\n',
        "    if False:\n",
    ),
    # crosscheck
    ("X1 public fields not compared", XC, '    bad = [k for k, v in own.items() if str(led.get(k, "")).upper() != v]\n', "    bad = []\n"),
    ("X2 unanswered sample counted as match", XC, '        return {"ledger_index": idx, "ok": False, "error": f"{type(e).__name__}: {e}"}\n', '        return {"ledger_index": idx, "ok": True, "error": f"{type(e).__name__}: {e}"}\n'),
    # report
    ("P1 report root gate off", RPT, '    if root != header["account_hash"]:\n', "    if False:\n"),
    (
        "P2 proof bytes omit key",
        RPT,
        "            proof_bytes.append(depth * INNER_BYTES + len(data) + 32)\n",
        "            proof_bytes.append(depth * INNER_BYTES + len(data))\n",
    ),
    (
        "P3 percentile off by one rank",
        RPT,
        "    return sorted_vals[max(1, math.ceil(p / 100 * len(sorted_vals))) - 1]\n",
        "    return sorted_vals[min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)))]\n",
    ),
]

TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_statehash_*.py"))]
SKIP_NAMES = {".venv", ".git", "__pycache__", "logs", ".ruff_cache", "charts", ".pytest_cache"}


def ignore(src_root):
    """copytree filter: SKIP_NAMES and *.png everywhere, `data` only at the repo root (raw, private)."""
    top = os.path.abspath(src_root)

    def fn(d, names):
        out = {n for n in names if n in SKIP_NAMES or n.endswith(".png")}
        if os.path.abspath(d) == top and "data" in names:
            out.add("data")
        return out

    return fn


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
    shutil.copytree(src, root, ignore=ignore(src))
    return root


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--only", default=None, help="run only this mutant id (e.g. H3)")
    a = p.parse_args(argv)
    src = Path.cwd()
    print(f"TESTS {' '.join(TESTS)}", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        root = copy_repo(src, tmp)
        has_a = (root / "statehash" / "data" / "A" / "state.jsonl.gz").exists()
        rc, last, failed = pytest(root, a.timeout)
    print(f"CONTROL unmutated copy: rc={rc} {last} statehash_data_A_copied={has_a}", flush=True)
    if rc != 0 or " passed" not in last or failed or not has_a:
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
