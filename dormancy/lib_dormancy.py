"""Shared code for dormancy/: ledger-close-time map, dormancy buckets, per-bucket stats, A->B transitions.

Python 3 stdlib only. Read-only: nothing here signs or submits anything.

Time map. An AccountRoot's PreviousTxnLgrSeq is the ledger of the last transaction that modified it
(its "last activity"). Its close time comes from, in order:
  exact   a ledger header we fetched (headers.jsonl) or that a committed collector run recorded
          (ledgers.csv rows: ledger_index + close_time of a validated ledger). Error 0.
  interp  the seq lies strictly between two known headers lo < seq < hi. Close times never go
          backwards, so the true time is in [t_lo, t_hi]. The point estimate is linear between them;
          the bound is the whole [t_lo, t_hi] interval, never narrower.
  before  the seq is below the oldest known header: the time is <= t_oldest, with no lower bound.
          These accounts get no point estimate; they count in a bucket only when the bucket is
          decided by the upper bound alone (age >= T - t_oldest).
Nothing is ever extrapolated past the newest header: a seq above it is an error (the snapshot's
own header is always in the map).
"""

import bisect
import csv
import glob
import gzip
import json
import os

RIPPLE_EPOCH = 946684800  # 2000-01-01T00:00:00Z

# dormancy buckets by age = snapshot close_time - last-activity close_time, in seconds.
# [lo, hi) ; hi None = open-ended. Labels are what the report prints.
HOUR, DAY = 3600, 86400
BUCKETS = [
    ("<1h", 0, HOUR),
    ("1h-1d", HOUR, DAY),
    ("1d-7d", DAY, 7 * DAY),
    ("7d-14d", 7 * DAY, 14 * DAY),
    (">=14d", 14 * DAY, None),
]
LABELS = [b[0] for b in BUCKETS]


def bucket_of(age):
    """Bucket label of an exact age in seconds (age < 0 is a bug in the caller)."""
    if age < 0:
        raise ValueError(f"negative age {age}")
    for label, lo, hi in BUCKETS:
        if age >= lo and (hi is None or age < hi):
            return label
    raise AssertionError("unreachable")


def buckets_for_interval(age_min, age_max):
    """Every bucket an age in [age_min, age_max] can fall in (age_max None = unbounded)."""
    out = []
    for label, lo, hi in BUCKETS:
        if (hi is None or age_min < hi) and (age_max is None or age_max >= lo):
            out.append(label)
    return out


# ---------------------------------------------------------------- headers / time map


def load_headers_jsonl(path):
    """{seq: (close_time, ledger_hash)} from headers.jsonl; skips a torn last line (no newline)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "rb") as fh:
        data = fh.read()
    lines = data.split(b"\n")
    if not data.endswith(b"\n"):
        lines = lines[:-1]  # torn last line (killed mid-write): not trusted
    for raw in lines:
        if not raw.strip():
            continue
        row = json.loads(raw)
        out[int(row["ledger_index"])] = (int(row["close_time"]), row["ledger_hash"])
    return out


def load_anchor_csvs(paths):
    """{seq: (close_time, ledger_hash)} from collector ledgers.csv files (ledger_index, ledger_hash,
    close_time or ledger_time columns). Two files that disagree on one seq is a hard error."""
    out = {}
    for path in paths:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                ct = row["close_time"] if "close_time" in row else row["ledger_time"]  # validators/ name
                seq, ct, h = int(row["ledger_index"]), int(ct), row["ledger_hash"]
                if seq in out and out[seq] != (ct, h):
                    raise ValueError(f"anchor conflict at {seq}: {out[seq]} vs {(ct, h)} ({path})")
                out[seq] = (ct, h)
    return out


def default_anchor_paths(repo):
    """Committed ledgers.csv files of earlier collector runs, drill copies excluded (same ledgers)."""
    paths = sorted(glob.glob(os.path.join(repo, "data", "**", "ledgers.csv"), recursive=True))
    return [p for p in paths if "/drill" not in p.replace(os.sep, "/")]


class TimeMap:
    """Ledger seq -> close-time bound, from exact headers only (see module doc)."""

    def __init__(self, headers):
        if not headers:
            raise ValueError("empty time map")
        self.headers = dict(headers)
        self.seqs = sorted(self.headers)
        self.times = [self.headers[s][0] for s in self.seqs]
        # close times must be non-decreasing in seq, else the bounds below are not bounds
        for i in range(1, len(self.seqs)):
            if self.times[i] < self.times[i - 1]:
                raise ValueError(f"close_time goes backwards between {self.seqs[i - 1]} and {self.seqs[i]}")

    def lookup(self, seq):
        """(kind, t_min, t_point, t_max) in ripple-epoch seconds; t_min/t_point None for 'before'."""
        i = bisect.bisect_left(self.seqs, seq)
        if i < len(self.seqs) and self.seqs[i] == seq:
            t = self.times[i]
            return ("exact", t, t, t)
        if i == len(self.seqs):
            raise ValueError(f"seq {seq} is above the newest known header {self.seqs[-1]}")
        if i == 0:
            return ("before", None, None, self.times[0])
        s_lo, s_hi = self.seqs[i - 1], self.seqs[i]
        t_lo, t_hi = self.times[i - 1], self.times[i]
        point = t_lo + (t_hi - t_lo) * (seq - s_lo) / (s_hi - s_lo)
        return ("interp", t_lo, point, t_hi)


# ---------------------------------------------------------------- snapshots


def load_snapshot(path):
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        objs = [json.loads(line) for line in fh if line.strip()]
    return header, objs


def reserve_params(objs, header):
    fees = [o for o in objs if o["LedgerEntryType"] == "FeeSettings"]
    if fees:
        f = fees[0]
        return int(f.get("ReserveBaseDrops", f.get("ReserveBase"))), int(
            f.get("ReserveIncrementDrops", f.get("ReserveIncrement"))
        )
    return int(header["server_state_reserve_base"]), int(header["server_state_reserve_inc"])


def native_outside_accounts(objs):
    """Native drops held by Escrow and PayChannel objects (part of total_coins, not of any Balance)."""
    esc = sum(
        int(o["Amount"]) for o in objs if o["LedgerEntryType"] == "Escrow" and isinstance(o["Amount"], str)
    )
    pc = sum(
        int(o["Amount"]) - int(o["Balance"])
        for o in objs
        if o["LedgerEntryType"] == "PayChannel" and isinstance(o["Amount"], str)
    )
    return esc, pc


def classify_accounts(header, objs, tmap):
    """One row per AccountRoot: address, balance, owner_count, reserve, last-activity seq/txid,
    time-map kind, age bounds (seconds, age_max None = unbounded), point age, bucket set, bucket."""
    t_snap = int(header["close_time"])
    base, inc = reserve_params(objs, header)
    rows = []
    for o in objs:
        if o["LedgerEntryType"] != "AccountRoot":
            continue
        seq = int(o["PreviousTxnLgrSeq"])
        if seq > int(header["ledger_index"]):
            raise ValueError(f"{o['Account']} PreviousTxnLgrSeq {seq} after snapshot ledger")
        kind, t_min, t_point, t_max = tmap.lookup(seq)
        age_min = t_snap - t_max
        age_max = None if t_min is None else t_snap - t_min
        age_point = None if t_point is None else t_snap - t_point
        cands = buckets_for_interval(age_min, age_max)
        if age_point is not None:
            bucket = bucket_of(max(0.0, age_point))
        elif len(cands) == 1:
            bucket = cands[0]
        else:
            bucket = None  # undecidable 'before' row: cannot happen with the open last bucket
        bal, oc = int(o["Balance"]), int(o.get("OwnerCount", 0))
        rows.append(
            {
                "account": o["Account"],
                "balance": bal,
                "owner_count": oc,
                "reserve": min(bal, base + inc * oc),
                "prev_seq": seq,
                "prev_txid": o["PreviousTxnID"],
                "kind": kind,
                "age_min": age_min,
                "age_max": age_max,
                "age_point": age_point,
                "bucket": bucket,
                "candidates": cands,
            }
        )
    return rows


# ---------------------------------------------------------------- stats


def gini(values):
    """Gini coefficient of non-negative integers, computed in exact fractions then rounded."""
    from fractions import Fraction

    s = sorted(values)
    n, total = len(s), sum(s)
    if n == 0 or total == 0:
        return 0.0
    g = Fraction(2 * sum((i + 1) * x for i, x in enumerate(s)), n * total) - Fraction(n + 1, n)
    return float(g)


def bucket_table(rows, top_n=10):
    """Per bucket: accounts, balance, reserve, owner_count (integers), gini, top-N share, plus the
    count bounds [min, max] that follow from the time-map intervals."""
    out = {}
    for label in LABELS:
        sel = [r for r in rows if r["bucket"] == label]
        bal = [r["balance"] for r in sel]
        tot = sum(bal)
        out[label] = {
            "accounts": len(sel),
            "balance_drops": tot,
            "reserve_drops": sum(r["reserve"] for r in sel),
            "owner_count": sum(r["owner_count"] for r in sel),
            "gini": round(gini(bal), 6),
            "top_share": round(sum(sorted(bal, reverse=True)[:top_n]) / tot, 6) if tot else 0.0,
            "accounts_min": sum(1 for r in rows if r["candidates"] == [label]),
            "accounts_max": sum(1 for r in rows if label in r["candidates"]),
            "by_kind": {k: sum(1 for r in sel if r["kind"] == k) for k in ("exact", "interp", "before")},
        }
    return out


def transitions(rows_a, rows_b, ledger_a, ledger_b):
    """A->B matrix keyed 'from|to' with 'created' / 'deleted' as the extra states, plus invariants:
    an account untouched between A and B (same PreviousTxnID) must be byte-identical in the fields we
    use, and a touched one must have its new PreviousTxnLgrSeq in (ledger_a, ledger_b]."""
    a = {r["account"]: r for r in rows_a}
    b = {r["account"]: r for r in rows_b}
    matrix = {}
    problems = []
    untouched = touched = 0

    def bump(k):
        matrix[k] = matrix.get(k, 0) + 1

    for acct, ra in a.items():
        rb = b.get(acct)
        if rb is None:
            bump(f"{ra['bucket']}|deleted")
            continue
        bump(f"{ra['bucket']}|{rb['bucket']}")
        if ra["prev_txid"] == rb["prev_txid"]:
            untouched += 1
            if (ra["prev_seq"], ra["balance"], ra["owner_count"]) != (
                rb["prev_seq"],
                rb["balance"],
                rb["owner_count"],
            ):
                problems.append(f"{acct}: same PreviousTxnID but fields differ")
        else:
            touched += 1
            if not ledger_a < rb["prev_seq"] <= ledger_b:
                problems.append(
                    f"{acct}: new PreviousTxnLgrSeq {rb['prev_seq']} outside ({ledger_a}, {ledger_b}]"
                )
    for acct, rb in b.items():
        if acct not in a:
            bump(f"created|{rb['bucket']}")
            if not ledger_a < rb["prev_seq"] <= ledger_b:
                problems.append(f"{acct}: created but PreviousTxnLgrSeq {rb['prev_seq']} not in window")
    return {"matrix": matrix, "untouched": untouched, "touched": touched, "problems": problems}


def reconcile(header, objs, rows, table):
    """Exact integer checks; returns list of (name, ok, detail)."""
    bal_all = sum(r["balance"] for r in rows)
    bal_buckets = sum(v["balance_drops"] for v in table.values())
    n_acct = sum(1 for o in objs if o["LedgerEntryType"] == "AccountRoot")
    esc, pc = native_outside_accounts(objs)
    total = int(header["total_coins"])
    checks = [
        (
            "objects_paged",
            len(objs) == int(header["objects_paged"]),
            f"{len(objs)} vs {header['objects_paged']}",
        ),
        (
            "accounts_in_buckets",
            sum(v["accounts"] for v in table.values()) == n_acct == len(rows),
            f"{sum(v['accounts'] for v in table.values())} vs {n_acct}",
        ),
        ("balance_in_buckets", bal_buckets == bal_all, f"{bal_buckets} vs {bal_all}"),
        (
            "reserve_in_buckets",
            sum(v["reserve_drops"] for v in table.values()) == sum(r["reserve"] for r in rows),
            "sum of bucket reserves = sum of account reserves",
        ),
        (
            "owner_count_in_buckets",
            sum(v["owner_count"] for v in table.values()) == sum(r["owner_count"] for r in rows),
            "owner counts",
        ),
        (
            "total_coins",
            bal_all + esc + pc == total,
            f"balances {bal_all} + escrow {esc} + paychan {pc} vs {total}",
        ),
        ("no_unbucketed", all(r["bucket"] is not None for r in rows), "every account has a bucket"),
        (
            "bounds_hold",
            all(v["accounts_min"] <= v["accounts"] <= v["accounts_max"] for v in table.values()),
            "min <= point <= max per bucket",
        ),
    ]
    return checks
