#!/usr/bin/env python3
"""Build dormancy/data/anchors.csv: every exact ledger header (seq, hash, close_time) that a COMMITTED
collector file in this repo already records, so the time map can be rebuilt from the repo alone.

Python 3 stdlib only. Read-only: reads `git ls-files` output and the listed ledgers.csv(.gz) files;
sends nothing to any node. Files without a close_time/ledger_time column are skipped and printed.
Two files that disagree on one seq (hash or close_time) is a hard error (exit 1). Drill copies are
left out (same ledgers as their run). Rows are written sorted by ledger_index with the number of
committed files that hold each header.

The full set is ~240k headers, so with --snapshots only the anchors the time map can use are kept:
for each distinct AccountRoot PreviousTxnLgrSeq (and snapshot ledger) the exact header, or the two
headers that bracket it in (anchors + --headers), plus the oldest anchor. A TimeMap built from the
kept anchors + --headers answers every lookup for those seqs exactly as the full set does
(tests/test_dormancy.py checks this).

Usage:
  ./anchors_dormancy.py [--repo .] [--out dormancy/data/anchors.csv] \
      [--snapshots A.jsonl.gz [B.jsonl.gz] --headers dormancy/data/headers.jsonl]
"""

import argparse
import bisect
import csv
import gzip
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_dormancy as L  # noqa: E402


def tracked_ledger_files(repo):
    out = subprocess.run(
        ["git", "-C", repo, "ls-files", "-z"], check=True, capture_output=True, stdin=subprocess.DEVNULL
    ).stdout.decode()
    names = [n for n in out.split("\0") if n]
    keep = []
    for n in sorted(names):
        base = os.path.basename(n)
        if base not in ("ledgers.csv", "ledgers.csv.gz") and not base.endswith("-ledgers.csv"):
            continue
        if "/drill" in "/" + n:
            continue
        keep.append(n)
    return keep


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", newline="") as fh:
            return fh.read()
    with open(path, newline="") as fh:
        return fh.read()


def collect(repo, files):
    """({seq: (close_time, hash)}, {seq: n_files}, [(file, rows or 'skipped: why')])."""
    anchors, holders, report = {}, {}, []
    for rel in files:
        reader = csv.DictReader(io.StringIO(read_text(os.path.join(repo, rel))))
        cols = reader.fieldnames or []
        tcol = "close_time" if "close_time" in cols else ("ledger_time" if "ledger_time" in cols else None)
        if tcol is None or "ledger_index" not in cols or "ledger_hash" not in cols:
            report.append((rel, "skipped: no ledger_index/ledger_hash/close_time columns"))
            continue
        n = 0
        for row in reader:
            seq, ct, h = int(row["ledger_index"]), int(row[tcol]), row["ledger_hash"].upper()
            if seq in anchors and anchors[seq] != (ct, h):
                raise ValueError(f"anchor conflict at {seq}: {anchors[seq]} vs {(ct, h)} ({rel})")
            anchors[seq] = (ct, h)
            holders[seq] = holders.get(seq, 0) + 1
            n += 1
        report.append((rel, n))
    return anchors, holders, report


def wanted_seqs(snapshots):
    want = set()
    for path in snapshots:
        header, objs = L.load_snapshot(path)
        want.add(int(header["ledger_index"]))
        want.update(int(o["PreviousTxnLgrSeq"]) for o in objs if o["LedgerEntryType"] == "AccountRoot")
    return want


def select(anchors, headers, want):
    """Subset of anchors that keeps every lookup of `want` identical (see module doc)."""
    combined = sorted(set(anchors) | set(headers))
    keep = set()
    for seq in want:
        i = bisect.bisect_left(combined, seq)
        if i < len(combined) and combined[i] == seq:
            keep.add(seq)
        else:
            if i > 0:
                keep.add(combined[i - 1])
            if i < len(combined):
                keep.add(combined[i])
    oldest_anchor = min(anchors)
    keep.add(oldest_anchor)
    return {s: anchors[s] for s in keep if s in anchors}


def write_csv(path, anchors, holders):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ledger_index", "ledger_hash", "close_time", "files"])
        for seq in sorted(anchors):
            ct, h = anchors[seq]
            w.writerow([seq, h, ct, holders[seq]])
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default="dormancy/data/anchors.csv")
    ap.add_argument("--snapshots", nargs="*", default=[])
    ap.add_argument("--headers", help="headers.jsonl used with the anchors (needed with --snapshots)")
    a = ap.parse_args(argv)
    if a.snapshots and not a.headers:
        ap.error("--snapshots needs --headers")
    files = tracked_ledger_files(a.repo)
    try:
        anchors, holders, report = collect(a.repo, files)
    except ValueError as e:
        print(f"ERROR {e}")
        return 1
    for rel, n in report:
        print(f"file {rel}: {n}")
    if not anchors:
        print("ERROR no anchors")
        return 1
    seqs = sorted(anchors)
    nfiles = sum(isinstance(n, int) for _, n in report)
    print(
        f"anchors {len(anchors)} distinct headers from {nfiles} files, 0 conflicts, "
        f"ledgers {seqs[0]}-{seqs[-1]}"
    )
    if a.snapshots:
        headers = L.load_headers_jsonl(a.headers)
        for s in set(anchors) & set(headers):
            if anchors[s] != headers[s]:
                print(f"ERROR anchor {s} {anchors[s]} differs from fetched header {headers[s]}")
                return 1
        want = wanted_seqs(a.snapshots)
        anchors = select(anchors, headers, want)
        print(f"kept {len(anchors)} anchors for {len(want)} wanted seqs ({len(headers)} fetched headers)")
    write_csv(a.out, anchors, holders)
    print(f"wrote {len(anchors)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
