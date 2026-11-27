#!/usr/bin/env python3
"""Mutation check for valsig: each mutant changes one spot of the verifier/recorder; the test suite
(tests/test_valsig.py) must fail against every one of them.

Each mutant runs in its own scratch copy (valsig/, dunl/, txflow/, the tests and fixtures). The
unmutated copy runs first and must pass. A mutant whose `old` text is not found exactly once is NOT
APPLIED and counts as a failure of the harness. CAUGHT needs a non-zero pytest exit with at least one
FAILED/ERROR line; the first failing test ids are printed as the kill reason.
Usage: python3 valsig/mutants.py [--jobs 3] [--timeout 300] [--only NAME,...]
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPY = ["valsig", "dunl", "txflow", "tests/test_valsig.py", "tests/fixtures/valsig"]
LIB, VER, COL = "valsig/lib_valsig.py", "valsig/verify_valsig.py", "valsig/collect_valsig.py"
GOOD = 'good = [g for g in vs if g["trusted"] and g["ok"] and g["full"] and g["lh"] == lh]'

# (name, file, old, new)
MUTANTS = [
    (
        "signed-includes-signature",
        LIB,
        "for t, f, s, e, _v in fields if (t, f) != F_SIGNATURE)",
        "for t, f, s, e, _v in fields if True)",
    ),
    ("wrong-hash-prefix", LIB, 'VALIDATION_PREFIX = b"VAL\\x00"', 'VALIDATION_PREFIX = b"VAL\\x01"'),
    ("no-float32", LIB, 'return struct.unpack("f", struct.pack("f", x))[0]', "return x"),
    ("quorum-80pct", LIB, "f32(0.67)", "f32(0.8)"),
    ("quorum-min", LIB, "    return max(a, b)", "    return min(a, b)"),
    ("no-low-s", LIB, 'and not lib_dunl.der_low_s(dv["signature"])', "and False"),
    ("no-canonical-order", LIB, "if (t, f) <= prev:", "if False:"),
    (
        "nunl-counted",
        VER,
        'signers = {g["master"] for g in good if g["master"] not in nunl}',
        'signers = {g["master"] for g in good}',
    ),
    ("partial-counted", VER, GOOD, GOOD.replace(' and g["full"]', "")),
    ("any-hash-counted", VER, GOOD, GOOD.replace(' and g["lh"] == lh', "")),
    ("invalid-counted", VER, GOOD, GOOD.replace(' and g["ok"]', "")),
    (
        "quorum-strict-gt",
        VER,
        "met = quorum is not None and len(signers) >= quorum",
        "met = quorum is not None and len(signers) > quorum",
    ),
    ("no-edge", VER, "edge = li in first_in_conn", "edge = False"),
    ("gap-events-ignored", VER, "not gaps and not holes and bool(idx)", "not holes and bool(idx)"),
    ("holes-ignored", VER, "if b != a + 1]", "if b < a]"),
    (
        "open-segment-ignored",
        VER,
        '"recording.files",\n        not rec.open_segment\n',
        '"recording.files",\n        True\n',
    ),
    ("window-length-ignored", VER, "and span >= min_hours * 3600", "and True"),
    (
        "vl-blob-sig-ignored",
        VER,
        'if not b or not b["signature_ok"] or not b["low_s"]:',
        'if not b or not b["low_s"]:',
    ),
    (
        "manifest-verify-ignored",
        VER,
        "        if ok != (True, True):\n            bad.append",
        "        if False:\n            bad.append",
    ),
    (
        "manifest-missing-ignored",
        VER,
        'missing = sorted(b58(k) for k in trusted if k not in per_master or not per_master[k]["signing"])',
        "missing = []",
    ),
    ("snapshot-set-ignored", VER, "        if tk != sorted(trusted):", "        if False:"),
    (
        "field-hash-ignored",
        VER,
        '("ledger_hash", e["lh"], r["ledger_hash"])',
        '("ledger_hash", e["lh"], e["lh"])',
    ),
    (
        "trust-by-any-manifest",
        VER,
        'cls = "trusted" if master in trusted else',
        'cls = "trusted" if master is not None else',
    ),
    (
        "node-quorum-ignored",
        VER,
        'if q["quorum"] != q["expected"] or not q["nunl_known"]',
        'if not q["nunl_known"]',
    ),
    ("rpc-hash-ignored", VER, 'or got["ledger_hash"] != r["ledger_hash"]', "or False"),
    ("rpc-sample-floor-ignored", VER, "len(pick) >= min(50, args.sample) and not bad", "not bad"),
    ("controls-baseline-ignored", VER, "    if base_fail:\n        print(", "    if False:\n        print("),
    ("collector-no-dedupe", COL, "            if self.s.has(li, k, lh):", "            if False:"),
    ("collector-no-allowlist", COL, "        if cmd not in ALLOWED_COMMANDS:", "        if False:"),
    ("collector-keeps-uri", COL, 'for k, v in obj.items() if k != "uri"}', "for k, v in obj.items()}"),
    ("collector-gap-reason", COL, '"reason": self.gap_reason or "stream_skip",', '"reason": "stream_skip",'),
    ("collector-gzip-mtime", COL, "compresslevel=9, mtime=0,", "compresslevel=9, mtime=None,"),
    ("collector-no-torn-cut", COL, "        fh.truncate(keep)\n", "        pass\n"),
    ("collector-snap-every-poll", COL, "                if h != self.s.snap_h:", "                if True:"),
    (
        "collector-nunl-error-empty",
        COL,
        '                self.q_parts["nunl"] = None',
        '                self.q_parts["nunl"] = {}',
    ),
    (
        "secp-shamir-skip-pq",
        "dunl/lib_dunl.py",
        "add = {(1, 0): p, (0, 1): q, (1, 1): pq}.get(bits)",
        "add = {(1, 0): p, (0, 1): q, (1, 1): p}.get(bits)",
    ),
]


def make_copy(dst):
    for rel in COPY:
        src = os.path.join(ROOT, rel)
        to = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(to), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, to, ignore=shutil.ignore_patterns("__pycache__", "data", "*.pyc"))
        else:
            shutil.copyfile(src, to)


def run_suite(cwd, timeout):
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        os.path.join(cwd, ".pyt"),
        "tests/test_valsig.py",
    ]
    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return None, "TIMEOUT"
    return p.returncode, out


def one(m, timeout, scratch):
    name, rel, old, new = m
    d = tempfile.mkdtemp(prefix=f"mut-{name}-", dir=scratch)
    try:
        make_copy(d)
        if name != "BASELINE":
            path = os.path.join(d, rel)
            src = open(path).read()
            if src.count(old) != 1:
                return name, "NOT_APPLIED", f"found {src.count(old)} times in {rel}"
            open(path, "w").write(src.replace(old, new))
        rc, out = run_suite(d, timeout)
        if rc is None:
            return name, "CAUGHT" if name != "BASELINE" else "BROKEN", "timeout"
        fails = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)
        summary = (re.findall(r"^.*(?:passed|failed|error).*$", out, re.M) or ["-"])[-1].strip()
        if name == "BASELINE":
            return name, "PASS" if rc == 0 else "BROKEN", summary
        if rc != 0 and fails:
            return name, "CAUGHT", f"{summary} | {', '.join(f.split('::')[-1] for f in fails[:2])}"
        return name, "SURVIVED" if rc == 0 else "BROKEN", summary
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="valsig-mutants-", dir=os.environ.get("TMPDIR"))
    try:
        _name, status, why = one(("BASELINE", None, None, None), a.timeout, scratch)
        print(f"BASELINE {status} {why}", flush=True)
        if status != "PASS":
            print("RESULT FAIL baseline does not pass")
            return 1
        todo = [m for m in MUTANTS if not a.only or m[0] in a.only.split(",")]
        counts = {}
        with ThreadPoolExecutor(a.jobs) as ex:
            futs = [ex.submit(one, m, a.timeout, scratch) for m in todo]
            for f in as_completed(futs):
                name, status, why = f.result()
                counts[status] = counts.get(status, 0) + 1
                print(f"MUTANT {name} {status} {why}", flush=True)
        caught = counts.get("CAUGHT", 0)
        ok = caught == len(todo)
        rest = " ".join(f"{k.lower()}={v}" for k, v in sorted(counts.items()) if k != "CAUGHT")
        print(f"RESULT {'PASS' if ok else 'FAIL'} mutants={len(todo)} caught={caught} {rest}".rstrip())
        return 0 if ok else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
