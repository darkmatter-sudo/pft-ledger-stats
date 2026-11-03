#!/usr/bin/env python3
"""Mutation check for senders (collector, analyzer, controls, crosscheck, report): put one broken line in
at a time in a temporary copy of the repo and run the senders tests; every mutant must be CAUGHT
(tests fail). Each mutant's text must occur exactly once, or it is reported NOT APPLIED (counts
as a failure). The unmutated copy is run first as the control and must pass.

Usage: .venv/bin/python senders/mutants_senders.py   (from the repo root; exit 0 = all caught)
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MUTANTS = [
    (
        'M1 collector: window end not enforced client-side',
        'senders/collect_senders.py',
        '    return int(close_time) + RIPPLE_EPOCH <= end_unix\n',
        '    return True\n',
    ),
    (
        'M2 collector: rows not in TransactionIndex order',
        'senders/collect_senders.py',
        '    rows.sort(key=lambda r: r["tx_index"])\n',
        '    pass\n',
    ),
    (
        'M3 collector: orphan tx rows kept on resume',
        'senders/collect_senders.py',
        '        orphans = drop_orphan_tx(tx_csv, done)\n',
        '        orphans = 0\n',
    ),
    (
        'M4 collector: ticket flag inverted',
        'senders/collect_senders.py',
        '"ticket": "y" if "TicketSequence" in tx else "n"',
        '"ticket": "n" if "TicketSequence" in tx else "y"',
    ),
    (
        'M5 collector: failed fetch never recorded as a gap',
        'senders/collect_senders.py',
        '        if err:\n            append_rows(gaps_csv',
        '        if False:\n            append_rows(gaps_csv',
    ),
    (
        'M6 analyzer: MIN_EVENTS not enforced',
        'senders/analyze_senders.py',
        '    if st["n"] < MIN_EVENTS or st["cv"] is None:\n',
        '    if st["cv"] is None:\n',
    ),
    (
        'M7 analyzer: periodic ignores period_share',
        'senders/analyze_senders.py',
        'if st["cv"] <= PERIODIC_CV and st["period_share"] >= PERIOD_SHARE:',
        'if st["cv"] <= PERIODIC_CV:',
    ),
    (
        'M8 analyzer: bursty threshold off by sign',
        'senders/analyze_senders.py',
        'st["b"] >= BURSTY_B:',
        'st["b"] >= -BURSTY_B:',
    ),
    (
        'M9 analyzer: tolerance band ignored',
        'senders/analyze_senders.py',
        'abs(g - med) <= PERIOD_TOL * med',
        'abs(g - med) <= 10 * PERIOD_TOL * med',
    ),
    (
        'M10 analyzer: last slice does not absorb the remainder',
        'senders/analyze_senders.py',
        '        return min((int(ct) - t0) // DAY, nslices - 1)\n',
        '        return (int(ct) - t0) // DAY\n',
    ),
    (
        'M11 analyzer: returning counted as new',
        'senders/analyze_senders.py',
        '        new = sum(1 for a in act if first_slice[a] == s)\n',
        '        new = len(act)\n',
    ),
    (
        'M12 analyzer: retention counts the whole slice, not the cohort',
        'senders/analyze_senders.py',
        '[len(cohort & active.get(k, set())) if k >= c else None',
        '[len(active.get(k, set())) if k >= c else None',
    ),
    (
        'M13 analyzer: one event per tx instead of per ledger',
        'senders/analyze_senders.py',
        '        times = sorted({int(r["close_time"]) for r in rows})\n',
        '        times = sorted(int(r["close_time"]) for r in rows)\n',
    ),
    (
        'M14 analyzer: per-sender failures not counted',
        'senders/analyze_senders.py',
        '        st["failed"] = sum(1 for r in rows if r["result"] != "tesSUCCESS")\n',
        '        st["failed"] = 0\n',
    ),
    (
        'M15 analyzer: unlisted holes accepted',
        'senders/analyze_senders.py',
        '    if cov["holes"] or cov.get("duplicates"):\n',
        '    if False:\n',
    ),
    (
        'M16 analyzer: tx rows vs txn_count not checked',
        'senders/analyze_senders.py',
        '    if tot["tx"] != tot["ledger_txn_count"]:\n',
        '    if False:\n',
    ),
    (
        'M17 analyzer: full addresses in top periodic',
        'senders/analyze_senders.py',
        'return addr if len(addr) <= 12 else f"{addr[:6]}...{addr[-4:]}"',
        'return addr',
    ),
    (
        'M18 crosscheck: window not enforced client-side',
        'senders/crosscheck_senders.py',
        '            if not (lo <= li <= hi):\n',
        '            if False:\n',
    ),
    (
        'M19 crosscheck: received transactions counted as sent',
        'senders/crosscheck_senders.py',
        'if tx.get("Account") != account or not e.get("validated", True):',
        'if not e.get("validated", True):',
    ),
    (
        'M20 crosscheck: paging stops after the first page',
        'senders/crosscheck_senders.py',
        '        if not marker or below:\n',
        '        if True:\n',
    ),
    (
        'M21 crosscheck: --negative leaves sender sets unaltered',
        'senders/crosscheck_senders.py',
        '            want.add(("NEGATIVE-CONTROL", lo, "Payment", "tesSUCCESS", 0))\n',
        '            pass\n',
    ),
    (
        'M22 crosscheck: ledger hash not compared',
        'senders/crosscheck_senders.py',
        'report(led.get("ledger_hash") == r["ledger_hash"] and n == want,',
        'report(n == want,',
    ),
    (
        'M23 crosscheck: daily recount ignores per-ledger tx_rows',
        'senders/crosscheck_senders.py',
        'report(rows_by_slice[s] == want and not per_ledger_bad,',
        'report(rows_by_slice[s] == want,',
    ),
    (
        'M24 controls: shuffled periodic senders never counted',
        'senders/controls_senders.py',
        '                shuf_p += 1\n',
        '                pass\n',
    ),
    (
        'M25 controls: pass threshold ignored',
        'senders/controls_senders.py',
        '        r = run_positive(name, a.n, a.seed)\n        ok = r["correct"] >= a.min_correct * r["n"]\n',
        '        r = run_positive(name, a.n, a.seed)\n        ok = True\n',
    ),
    (
        'M26 controls: planted events not offset to the real window start',
        'senders/controls_senders.py',
        'idx = (bisect.bisect_left(pool, t0 + t) for t in gen(rng))',
        'idx = (bisect.bisect_left(pool, t) for t in gen(rng))',
    ),
    (
        'M27 controls: planted control result ignored',
        'senders/controls_senders.py',
        '            ok = r["correct"] >= a.min_correct * r["n"]\n',
        '            ok = True\n',
    ),
    (
        'M28 controls: shuffled senders that turn periodic not checked',
        'senders/controls_senders.py',
        'ok = r["shuffled_periodic"] <= limit and r["shuffled_classes"].get("periodic", 0) <= limit',
        'ok = r["shuffled_periodic"] <= limit',
    ),
    (
        'M29 report: incomplete collection does not fail the report',
        'senders/report_senders.py',
        '    return 1 if bad or failed else 0\n',
        '    return 1 if failed else 0\n',
    ),
    (
        'M30 report: retention share against the wrong base',
        'senders/report_senders.py',
        "pct(v / r['size'])",
        "pct(v / (r['size'] + 1))",
    ),
    (
        'M31 report: planted control result ignored',
        'senders/report_senders.py',
        '    for r in planted:\n        r["ok"] = r["correct"] >= 0.95 * r["n"]\n',
        '    for r in planted:\n        r["ok"] = True\n',
    ),
]
TIMEOUT = 120
TESTS = ["tests/" + p.name for p in sorted(Path("tests").glob("test_senders_*.py"))]


def pytest(root):
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "timeout", f"tests did not finish in {TIMEOUT} s (hang counts as caught)"
    last = (r.stdout.strip().splitlines() or ["?"])[-1]
    return r.returncode, last


def main():
    src = Path.cwd()
    rc, last = pytest(src)
    print(f"CONTROL unmutated: rc={rc} {last}", flush=True)
    if rc != 0:
        return 1
    bad = 0
    for name, path, old, new in MUTANTS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            shutil.copytree(src, root, ignore=shutil.ignore_patterns(".venv", ".git", "data", "__pycache__"))
            f = root / path
            text = f.read_text()
            if text.count(old) != 1:
                print(f"NOT APPLIED {name} (count {text.count(old)})", flush=True)
                bad += 1
                continue
            f.write_text(text.replace(old, new))
            rc, last = pytest(root)
            caught = rc != 0
            bad += not caught
            print(f"{'CAUGHT' if caught else 'SURVIVED'} {name}: {last}", flush=True)
    print(f"MUTANTS {len(MUTANTS)} caught={len(MUTANTS) - bad} failed={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
