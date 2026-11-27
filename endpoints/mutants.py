#!/usr/bin/env python3
"""Mutation check for endpoints: each mutant changes one spot of the library, verifier or collector;
the test suite (tests/test_endpoints.py) must fail against every one of them.

Each mutant runs in its own scratch copy (endpoints/ without data/, report/ and logs/, plus the tests,
so the real-pack test skips there). The unmutated copy runs first and must pass. A mutant whose `old`
text is not found exactly once is NOT_APPLIED and counts as a harness failure. CAUGHT needs a non-zero
pytest exit with at least one FAILED/ERROR line; the first failing test ids are printed as the kill reason.
Usage: TMPDIR=/absolute/dir python3 endpoints/mutants.py [--jobs 3] [--timeout 300] [--only NAME,...]
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
ADDONS = ["endpoints"]
COPY = ADDONS + ["tests/test_endpoints.py"]
LIB, VER, COL = (
    "endpoints/lib_endpoints.py",
    "endpoints/verify_endpoints.py",
    "endpoints/collect_endpoints.py",
)

# (name, file, old, new)
MUTANTS = [
    # library: family grouping, concentration, hashing, IP scan
    ("trailing-cc-kept", LIB, 'name = _TRAILING_CC.sub("", as_name.strip())', "name = as_name.strip()"),
    ("dash-split-off", LIB, 'if " - " in name:', "if False:"),
    ("digits-kept", LIB, 'tok = _TRAILING_DIGITS.sub("", tok)', "tok = tok"),
    ("needle-substring", LIB, "if any(n in toks for n in needles):", "if any(n in norm for n in needles):"),
    ("decoration-unknown", LIB, "return as_name.strip().lower()", "return UNKNOWN"),
    (
        "unknown-counted",
        LIB,
        "if fam == UNKNOWN:\n            unresolved += 1",
        "if fam is None:\n            unresolved += 1",
    ),
    ("rank-ascending", LIB, "key=lambda kv: (-kv[1], kv[0])", "key=lambda kv: (kv[1], kv[0])"),
    ("hhi-not-squared", LIB, "(c * 10000.0 / total) * (c / total)", "(c * 10000.0 / total)"),
    ("ipv4-unvalidated", LIB, "ipaddress.IPv4Address(m.group(0))", "pass"),
    (
        "hash-unsorted",
        LIB,
        'json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)',
        'json.dumps(obj, separators=(",", ":"), default=str)',
    ),
    # verifier
    ("order-ignored", VER, '"match" if order else "mismatch"', '"match"'),
    (
        "unresolved-ignored",
        VER,
        "if not fam and not cty and unres[0] == unres[1]:",
        "if not fam and not cty:",
    ),
    ("tie-is-below", VER, '"below_top_family": u_max < t_min,', '"below_top_family": u_max <= t_min,'),
    (
        "top-is-smallest",
        VER,
        'top = recount["provider_families"][0]',
        'top = recount["provider_families"][-1]',
    ),
    ("hash-unchecked", VER, 'elif rec.get("content_sha256") != rec.get("published_sha256"):', "elif False:"),
    (
        "ip-scan-off",
        VER,
        "    ips = L.find_ips(text)\n    if ips:",
        "    ips = L.find_ips(text)\n    if False:",
    ),
    ("dups-allowed", VER, "if len(set(nums)) != len(nums):", "if False:"),
    ("prompt-country-unchecked", VER, 'elif v.get("country") != p.get("country"):', "elif False:"),
    (
        "became-swapped",
        VER,
        'pk.get(k) == "resolved" and cur[k] == "unresolved"',
        'pk.get(k) == "unresolved" and cur[k] == "resolved"',
    ),
    ("domain-ignored", VER, 'res = "agree" if agree and same_domain else', 'res = "agree" if agree else'),
    ("meta-only-is-data", VER, "if not rows or len(rows) < 2:", "if not rows:"),
    (
        "di-total-unchecked",
        VER,
        'ok_total = di.get("resolved_endpoints") == len(vals) - recount["unresolved_endpoints"]',
        "ok_total = True",
    ),
    # collector
    ("no-pacing", COL, "if wait > 0:", "if False:"),
    (
        "404-not-final",
        COL,
        'return row is not None and row["status"] in (200, 404)',
        'return row is not None and row["status"] in (200,)',
    ),
    ("no-retry", COL, "if status != 0 and status not in RETRY_HTTP:", "if True:"),
    (
        "pack-ip-guard-off",
        COL,
        "    ips = L.find_ips(text)\n    if ips:",
        "    ips = L.find_ips(text)\n    if False:",
    ),
    ("address-copied", COL, '"address_seen": bool(v.get("ip")),', '"address_seen": v.get("ip"),'),
    (
        "failed-rounds-fetched",
        COL,
        'done = [r for r in rounds if r["status"] == "COMPLETE"]',
        "done = rounds",
    ),
    ("pack-mtime-now", COL, "mtime=0, compresslevel=9", "mtime=None, compresslevel=9"),
]


def _ignore(src, names):
    top = os.path.relpath(src, ROOT) in ADDONS
    return [
        n
        for n in names
        if n in ("__pycache__", ".ruff_cache")
        or n.endswith(".pyc")
        or (top and n in ("data", "report", "logs"))
    ]


def make_copy(dst):
    for rel in COPY:
        src = os.path.join(ROOT, rel)
        to = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(to), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, to, ignore=_ignore)
        else:
            shutil.copyfile(src, to)


def run_suite(cwd, timeout):
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        os.path.join(cwd, ".pyt"),
        "tests/test_endpoints.py",
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
            with open(path) as fh:
                src = fh.read()
            if src.count(old) != 1:
                return name, "NOT_APPLIED", f"found {src.count(old)} times in {rel}"
            with open(path, "w") as fh:
                fh.write(src.replace(old, new))
        rc, out = run_suite(d, timeout)
        if rc is None:
            return name, "CAUGHT" if name != "BASELINE" else "BROKEN", "timeout"
        fails = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)
        summary = (re.findall(r"^.*(?:passed|failed|error).*$", out, re.M) or ["-"])[-1].strip()
        if name == "BASELINE":
            return name, "PASS" if rc == 0 else "BROKEN", summary
        if rc != 0 and fails:
            return name, "CAUGHT", f"{summary} | {', '.join(f.split('::')[-1] for f in fails[:3])}"
        return name, "SURVIVED" if rc == 0 else "BROKEN", summary
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="endpoints-mutants-", dir=os.environ.get("TMPDIR"))
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
