#!/usr/bin/env python3
"""Mutation check for valdomain: each mutant changes one spot of the library, verifier or collector;
the test suite (tests/test_valdomain.py) must fail against every one of them.

Each mutant runs in its own scratch copy (valdomain/, valsig/, dunl/, the tests and fixtures; the
add-ons' own data/ folders are left out, so the real-collection test skips there). The unmutated copy
runs first and must pass. A mutant whose `old` text is not found exactly once is NOT_APPLIED and
counts as a harness failure. CAUGHT needs a non-zero pytest exit with at least one FAILED/ERROR line;
the first failing test ids are printed as the kill reason.
Usage: python3 valdomain/mutants.py [--jobs 3] [--timeout 300] [--only NAME,...]
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
ADDONS = ["valdomain", "valsig", "dunl"]
COPY = ADDONS + ["tests/test_valdomain.py", "tests/fixtures/valdomain"]
LIB, VER, COL = (
    "valdomain/lib_valdomain.py",
    "valdomain/verify_valdomain.py",
    "valdomain/collect_valdomain.py",
)

# (name, file, old, new)
MUTANTS = [
    # library: attestation + classification
    (
        "att-message-order",
        LIB,
        'f"[domain-attestation-blob:{domain}:{master_b58}]"',
        'f"[domain-attestation-blob:{master_b58}:{domain}]"',
    ),
    (
        "att-hex-unchecked",
        LIB,
        'or not re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", attestation_hex):',
        ":",
    ),
    (
        "best-is-worst",
        LIB,
        "return min(classes, key=lambda c: RANK[c])",
        "return max(classes, key=lambda c: RANK[c])",
    ),
    (
        "key-match-ignores-case",
        LIB,
        'mine = [e for e in entries if e.get("public_key") == master_b58]',
        'mine = [e for e in entries if str(e.get("public_key")).lower() == master_b58.lower()]',
    ),
    (
        "empty-attestation-counts",
        LIB,
        'isinstance(e.get("attestation"), str) and e["attestation"]]',
        'isinstance(e.get("attestation"), str)]',
    ),
    (
        "truncated-accepted",
        LIB,
        'if rec.get("status") != 200 or rec.get("truncated"):',
        'if rec.get("status") != 200:',
    ),
    (
        "non-200-parsed",
        LIB,
        'if rec.get("status") != 200 or rec.get("truncated"):',
        'if rec.get("truncated"):',
    ),
    (
        "all-attestations-needed",
        LIB,
        "if any(verify_attestation(master_hex, domain, a) for a in atts):",
        "if all(verify_attestation(master_hex, domain, a) for a in atts):",
    ),
    ("toml-duplicate-key-ok", LIB, "        if path[-1] in d:\n", "        if False:\n"),
    (
        "toml-table-twice-ok",
        LIB,
        'or ("hdr", id(v)) in self.defined:',
        "or False:",
    ),
    ("toml-bad-escape-ok", LIB, '        self.err(f"bad escape \\\\{c}")', "        return c"),
    # verifier
    ("sums-unlisted-ignored", VER, "for rel in sorted(present - set(listed))]", "for rel in []]"),
    ("vl-node-list-ignored", VER, "    if node_list != masters:", "    if False:"),
    ("vl-seq-ignored", VER, 'if b and pl.get("seq") != b["sequence"]:', "if False:"),
    (
        "vl-validity-ignored",
        VER,
        'and not ((b["effective"] or 0) + RIPPLE_EPOCH <= fetch["t"] < b["expiration"] + RIPPLE_EPOCH)',
        "and False",
    ),
    ("vl-site-ignored", VER, 'elif fetch.get("url") != C.vl_site(node):', "elif False:"),
    (
        "body-hash-ignored",
        VER,
        'if hashlib.sha256(body).hexdigest() != rec.get("sha256") or len(body) != rec.get("bytes"):',
        "if False:",
    ),
    ("cache-name-ignored", VER, 'if rec.get("domain") != dom or rec.get("path") != p:', "if False:"),
    ("cross-older-ok", VER, 'elif nm["sequence"] < m["sequence"]:', "elif False:"),
    (
        "cross-domain-ignored",
        VER,
        '                if nm["domain"] != m["domain"]:',
        "                if False:",
    ),
    (
        "cross-details-ignored",
        VER,
        'elif det.get("domain") != nm["domain"] or det.get("seq") != nm["sequence"]:',
        "elif False:",
    ),
    ("cross-missing-ok", VER, 'problems.append(f"{tag}: no answer"', '(f"{tag}: no answer"'),
    ("exit-always-0", VER, "rc = 1 if ck.failed else 0", "rc = 0"),
    (
        "nodata-exit-0",
        VER,
        '        print(f"NO DATA: {x}")\n        return 2',
        '        print(f"NO DATA: {x}")\n        return 0',
    ),
    ("controls-baseline-ignored", VER, "    if base_ck.failed:", "    if False:"),
    # collector
    ("redirect-to-http", COL, 'if u.scheme != "https" or not u.hostname:', "if not u.hostname:"),
    ("redirect-unlimited", COL, "if len(self.hops) >= MAX_REDIRECTS:", "if False:"),
    ("size-cap-off", COL, "return data[:cap], len(data) > cap", "return data, False"),
    (
        "reason-published",
        COL,
        'rec.update(outcome="unreachable", redirects=red.hops)',
        'rec.update(outcome="unreachable", redirects=red.hops, reason=str(x))',
    ),
    ("private-address-ok", COL, "if any(not a.is_global or a.is_multicast for a in addrs):", "if False:"),
    (
        "toml-no-resume",
        COL,
        "                if os.path.exists(dst):\n                    cached += 1",
        "                if False:\n                    cached += 1",
    ),
    (
        "checker-skipped",
        COL,
        '    if not ok:\n        rec.update(outcome="unreachable")',
        '    if False:\n        rec.update(outcome="unreachable")',
    ),
    ("manifest-no-resume", COL, "                if (src, mk) in have:", "                if False:"),
    ("invalid-domain-fetched", COL, "            if not valid_domain(dom):", "            if False:"),
    (
        "server-info-unfiltered",
        COL,
        "            res = {\n                k: info.get(k)",
        "            res = info or {\n                k: info.get(k)",
    ),
    (
        "peer-uri-kept",
        COL,
        '        if m == "validators":\n            res = strip_uri(res)',
        "        if False:\n            res = strip_uri(res)",
    ),
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
        "tests/test_valdomain.py",
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
    scratch = tempfile.mkdtemp(prefix="valdomain-mutants-", dir=os.environ.get("TMPDIR"))
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
