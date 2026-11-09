#!/usr/bin/env python3
"""Census metrics and A->B lifecycle reconciliation of deferred ledger objects (stdlib only).

    analyze_objlife.py --a A.jsonl --b B.jsonl --tx DIR --types types.json [--json OUT]
                       [--transitions CSV] [--sample K] [--seed S]

Inputs: two collect_objlife.py snapshots (objlife-<ledger>.jsonl, ledgers A < B) and a
watch_objlife.py directory (obj-tx.jsonl + ledgers.csv + gaps.csv) covering ledgers A+1..B.
Nothing is fetched; every number comes from these files.

Per snapshot (exact integers; IOU amounts as exact decimal strings, never floats):
  counts       objects per deferred type
  value        native drops held in or promised by each type: Escrow Amount (locked),
               PayChannel Amount and Amount - Balance (locked, unclaimed), Check SendMax,
               Offer TakerGets/TakerPays, NFTokenOffer Amount split by sell/buy flag;
               IOU sums per (currency, issuer)
  reserve      owner reserve tied up: 1 OwnerCount per object for every deferred type
               (adjustOwnerCount(..., 1, ...) in each create path, see README), x reserve_inc
  owners       per type and overall: owners, top-1/top-5 share of objects, Gini (exact Fraction)
  deadlines    earliest Expiration/CancelAfter vs the ledger close time T (= the next ledger's
               parent close time, which the transactors compare against):
               expired (still in the ledger), <1h, 1h-24h, 1d-7d, 7d-30d, >30d, none.
               Expired = mark <= T, except Escrow CancelAfter and Credential Expiration,
               which lapse only at mark < T (source lines in the README);
               Escrow FinishAfter < T = finishable (counted separately, not expired)
  age          ledgers since PreviousTxnLgrSeq: <1k, 1k-100k, 100k-1M, >=1M

Lifecycle A->B: created, deleted, modified (any field), unchanged, per type, plus the tx types
that caused them.

Reconciliation (retained window: every tx of A+1..B that touched a deferred object):
  completeness  ledgers.csv has exactly one row per ledger A+1..B, gaps.csv has none in range,
                each ledger's obj_tx_count equals its obj-tx.jsonl lines, no tx hash twice.
                If any fails, the reconciliation checks are NOT_CHECKABLE and the exit code is 2.
  R1  replay of A + every CreatedNode/ModifiedNode/DeletedNode in (ledger, TransactionIndex)
      order gives exactly B's index set (missing / unexpected listed)
  R2  every replayed object equals B's object field for field, threading fields aside (created
      objects: B fields that NewFields omits must hold their default value 0 / "0")
  R3  per type: count(B) == count(A) + created - deleted
  R4  every ModifiedNode/DeletedNode hits a live object whose fields match its PreviousFields
      (modified) / FinalFields (deleted), and every CreatedNode an index that is not live
  R5  threading: every B object with PreviousTxnLgrSeq > A names a window tx that touched it at
      that ledger; every B object with PreviousTxnLgrSeq <= A is equal to its A object
      (with R4 passing, R3 is the per-type sum of R1; it is kept as a readable per-type summary)
Sampled: --sample K tx hashes are picked with a fixed --seed from the window txs and listed in
the JSON; crosscheck_objlife.py --txs re-fetches them from the public RPC and compares meta.

Exit: 0 all checks pass, 1 a reconciliation check failed (every residual is listed),
2 incomplete or unreadable input.
"""

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from fractions import Fraction

FORMAT = "pft-objlife-snapshot/1"
THREAD = ("PreviousTxnID", "PreviousTxnLgrSeq")
OWNER_FIELD = {"NFTokenOffer": "Owner", "Credential": "Issuer"}  # every other type: Account
LSF_SELL_NFTOKEN = 0x00000001
LSF_ACCEPTED = 0x00010000
RIPPLE_EPOCH = 946684800
DEADLINE_BUCKETS = ("expired", "<1h", "1h-24h", "1d-7d", "7d-30d", ">30d", "none")
AGE_BUCKETS = ("<1k", "1k-100k", "100k-1M", ">=1M")
DEADLINE_FIELDS = ("Expiration", "CancelAfter")
# Deadlines that lapse only strictly AFTER the mark (postfiatd `after(now, mark)` / `now > exp`);
# every other Expiration/CancelAfter lapses at close time >= mark (hasExpired, PayChan close).
STRICT_DEADLINES = {"Escrow": ("CancelAfter",), "Credential": ("Expiration",)}


class InputError(Exception):
    pass


def load_snapshot(path):
    """(header, {index: obj}) of one objlife snapshot; raises InputError on any format problem."""
    try:
        with open(path) as fh:
            lines = [json.loads(x) for x in fh]
    except (OSError, ValueError) as e:
        raise InputError(f"{path}: {e}") from e
    if not lines or lines[0].get("format") != FORMAT:
        raise InputError(f"{path}: not a {FORMAT} file")
    header, rows = lines[0], lines[1:]
    if len(rows) != header["deferred_rows"]:
        raise InputError(f"{path}: {len(rows)} rows != header deferred_rows {header['deferred_rows']}")
    objs = {}
    for o in rows:
        if o["index"] in objs:
            raise InputError(f"{path}: index {o['index']} twice")
        if int(o.get("PreviousTxnLgrSeq", 0)) > header["ledger_index"]:
            raise InputError(f"{path}: {o['index']} PreviousTxnLgrSeq after the snapshot ledger")
        objs[o["index"]] = o
    return header, objs


def owner_of(o):
    t = o["LedgerEntryType"]
    if t == "Credential" and o.get("Flags", 0) & LSF_ACCEPTED:
        return o.get("Subject")
    return o.get(OWNER_FIELD.get(t, "Account"))


def is_native(amount):
    return isinstance(amount, str)


def add_amount(native, iou, key, amount):
    """Add one Amount field: drops (str) to native[key], IOU dict to iou[key][(currency, issuer)]."""
    if amount is None:
        return
    if is_native(amount):
        native[key] += int(amount)
    else:
        iou[key][(amount["currency"], amount["issuer"])] += Decimal(amount["value"])


def value_metrics(objs):
    native = Counter()
    iou = defaultdict(lambda: defaultdict(Decimal))
    for o in objs.values():
        t = o["LedgerEntryType"]
        if t == "Escrow":
            add_amount(native, iou, "Escrow.Amount", o.get("Amount"))
        elif t == "PayChannel":
            add_amount(native, iou, "PayChannel.Amount", o.get("Amount"))
            if is_native(o.get("Amount")):
                native["PayChannel.unclaimed"] += int(o["Amount"]) - int(o.get("Balance", "0"))
        elif t == "Check":
            add_amount(native, iou, "Check.SendMax", o.get("SendMax"))
        elif t == "Offer":
            add_amount(native, iou, "Offer.TakerGets", o.get("TakerGets"))
            add_amount(native, iou, "Offer.TakerPays", o.get("TakerPays"))
        elif t == "NFTokenOffer":
            side = "sell" if o.get("Flags", 0) & LSF_SELL_NFTOKEN else "buy"
            add_amount(native, iou, f"NFTokenOffer.{side}.Amount", o.get("Amount"))
    return {
        "native_drops": {k: str(v) for k, v in sorted(native.items())},
        "iou": {
            k: {f"{c}/{i}": format(v, "f") for (c, i), v in sorted(m.items())} for k, m in sorted(iou.items())
        },
    }


def gini(values):
    """Exact Gini of non-negative integers (0 for empty or all-zero input)."""
    xs = sorted(values)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return Fraction(0)
    weighted = sum((2 * (i + 1) - n - 1) * x for i, x in enumerate(xs))
    return Fraction(weighted, n * total)


def concentration(objs):
    def one(group):
        c = Counter(owner_of(o) for o in group)
        n = sum(c.values())
        top = [v for _, v in c.most_common()]
        g = gini(c.values())
        return {
            "objects": n,
            "owners": len(c),
            "top1_objects": top[0] if top else 0,
            "top1_share": str(Fraction(top[0], n)) if n else "0",
            "top5_share": str(Fraction(sum(top[:5]), n)) if n else "0",
            "gini": str(g),
            "gini_float": round(float(g), 6),
        }

    by_type = defaultdict(list)
    for o in objs.values():
        by_type[o["LedgerEntryType"]].append(o)
    out = {t: one(g) for t, g in sorted(by_type.items())}
    out["ALL"] = one(list(objs.values()))
    return out


def is_expired(o, close_time):
    """Expired at the next ledger (its parent close time = this snapshot's close time)."""
    strict = STRICT_DEADLINES.get(o.get("LedgerEntryType"), ())
    for f in DEADLINE_FIELDS:
        if f in o and (o[f] < close_time if f in strict else o[f] <= close_time):
            return True
    return False


def deadline_bucket(o, close_time):
    times = [o[f] for f in DEADLINE_FIELDS if f in o]
    if not times:
        return "none"
    if is_expired(o, close_time):
        return "expired"
    left = min(times) - close_time
    for limit, name in ((3600, "<1h"), (86400, "1h-24h"), (7 * 86400, "1d-7d"), (30 * 86400, "7d-30d")):
        if left < limit:
            return name
    return ">30d"


def age_bucket(o, ledger):
    age = ledger - int(o.get("PreviousTxnLgrSeq", 0))
    if age < 1000:
        return "<1k"
    if age < 100_000:
        return "1k-100k"
    if age < 1_000_000:
        return "100k-1M"
    return ">=1M"


def census(header, objs, deferred_names):
    ct, ledger = header["close_time"], header["ledger_index"]
    counts = {t: 0 for t in deferred_names}
    deadlines = {t: {b: 0 for b in DEADLINE_BUCKETS} for t in deferred_names}
    ages = {t: {b: 0 for b in AGE_BUCKETS} for t in deferred_names}
    finishable = 0
    for o in objs.values():
        t = o["LedgerEntryType"]
        counts[t] += 1
        deadlines[t][deadline_bucket(o, ct)] += 1
        ages[t][age_bucket(o, ledger)] += 1
        if t == "Escrow" and "FinishAfter" in o and o["FinishAfter"] < ct:
            finishable += 1
    n = len(objs)
    return {
        "ledger_index": ledger,
        "ledger_hash": header["ledger_hash"],
        "close_time_iso": header["close_time_iso"],
        "counts": counts,
        "total": n,
        "value": value_metrics(objs),
        "reserve": {
            "reserve_inc_drops": header["server_state_reserve_inc"],
            "owner_count_units": n,
            "drops": str(n * int(header["server_state_reserve_inc"])),
        },
        "owners": concentration(objs),
        "deadlines": deadlines,
        "expired_not_removed": sum(d["expired"] for d in deadlines.values()),
        "escrow_finishable": finishable,
        "age_ledgers": ages,
    }


def load_window(txdir, a_seq, b_seq):
    """(txs sorted by (ledger, tx_index), completeness problems list)."""
    problems = []
    rows = {}
    with open(os.path.join(txdir, "ledgers.csv")) as fh:
        for r in csv.DictReader(fh):
            i = int(r["ledger_index"])
            if a_seq < i <= b_seq:
                if i in rows:
                    problems.append(f"ledger {i}: two ledgers.csv rows")
                rows[i] = int(r["obj_tx_count"])
    missing = [i for i in range(a_seq + 1, b_seq + 1) if i not in rows]
    if missing:
        problems.append(f"{len(missing)} ledgers missing from ledgers.csv (first {missing[:5]})")
    gp = os.path.join(txdir, "gaps.csv")
    if os.path.exists(gp):
        with open(gp) as fh:
            gaps = [
                int(r["ledger_index"]) for r in csv.DictReader(fh) if a_seq < int(r["ledger_index"]) <= b_seq
            ]
        if gaps:
            problems.append(f"{len(gaps)} gap ledgers in range (first {gaps[:5]})")
    txs, per_ledger, seen = [], Counter(), set()
    with open(os.path.join(txdir, "obj-tx.jsonl")) as fh:
        for line in fh:
            x = json.loads(line)
            if not a_seq < x["ledger_index"] <= b_seq:
                continue
            h = x["tx"]["hash"]
            if h in seen:
                problems.append(f"tx {h} twice")
            seen.add(h)
            per_ledger[x["ledger_index"]] += 1
            txs.append(x)
    for i, n in sorted(rows.items()):
        if per_ledger[i] != n:
            problems.append(f"ledger {i}: obj_tx_count {n} != {per_ledger[i]} obj-tx.jsonl lines")
    txs.sort(key=lambda x: (x["ledger_index"], x["tx_index"]))
    return txs, problems


def is_default(v):
    return v in (0, "0")


def same(cur, f, v):
    """cur[f] == v; a field a CreatedNode omitted (default value) matches 0 / "0"."""
    if f in cur:
        return cur[f] == v
    return "_created" in cur and is_default(v)


def replay(a_objs, txs, deferred_names):
    """Apply every deferred-type node of every tx to A. Returns (live, residuals, effects)."""
    live = {k: dict(v) for k, v in a_objs.items()}
    residuals, effects = [], []
    for x in txs:
        h, seq = x["tx"]["hash"], x["ledger_index"]
        ttype = x["tx"].get("TransactionType", "?")
        for node in x["meta"].get("AffectedNodes", []):
            ((kind, n),) = node.items()
            t = n.get("LedgerEntryType")
            if t not in deferred_names:
                continue
            idx = n["LedgerIndex"]
            where = f"{kind} {t} {idx[:12]} tx {h[:12]} ledger {seq}"
            if kind == "CreatedNode":
                if idx in live:
                    residuals.append(f"R4 create of live object: {where}")
                obj = dict(n.get("NewFields") or {}, LedgerEntryType=t, index=idx)
                obj.update(PreviousTxnID=h, PreviousTxnLgrSeq=seq)
                obj["_created"] = sorted(obj)
                live[idx] = obj
            elif kind == "ModifiedNode":
                cur = live.get(idx)
                if cur is None:
                    residuals.append(f"R4 modify of absent object: {where}")
                    cur = {}
                for f, v in (n.get("PreviousFields") or {}).items():
                    if not same(cur, f, v):
                        residuals.append(f"R4 PreviousFields.{f} {v!r} != replay {cur.get(f)!r}: {where}")
                obj = dict(n.get("FinalFields") or {}, LedgerEntryType=t, index=idx)
                obj.update(PreviousTxnID=h, PreviousTxnLgrSeq=seq)
                if "_created" in cur:
                    obj["_created"] = cur["_created"]
                live[idx] = obj
            elif kind == "DeletedNode":
                cur = live.pop(idx, None)
                if cur is None:
                    residuals.append(f"R4 delete of absent object: {where}")
                else:
                    for f, v in (n.get("FinalFields") or {}).items():
                        if f not in THREAD and not same(cur, f, v):
                            residuals.append(f"R4 FinalFields.{f} {v!r} != replay {cur.get(f)!r}: {where}")
            else:
                residuals.append(f"R4 unknown node kind: {where}")
                continue
            effects.append((kind, t, idx, h, seq, ttype))
    return live, residuals, effects


def compare_object(rep, b):
    """Field differences between a replayed object and B's object (default rule for creates)."""
    diffs = []
    created = set(rep.get("_created", ()))
    for f in sorted((set(rep) | set(b)) - {"_created", *THREAD}):
        rv, bv = rep.get(f), b.get(f)
        if rv == bv:
            continue
        if created and f not in rep and is_default(bv):
            continue
        diffs.append(f"{f}: replay {rv!r} != B {bv!r}")
    return diffs


def reconcile(a_hdr, a_objs, b_hdr, b_objs, txs, deferred_names):
    live, residuals, effects = replay(a_objs, txs, deferred_names)
    checks = {}
    missing = sorted(set(b_objs) - set(live))
    unexpected = sorted(set(live) - set(b_objs))
    r1 = [f"missing from replay: {b_objs[i]['LedgerEntryType']} {i}" for i in missing]
    r1 += [f"unexpected after replay: {live[i]['LedgerEntryType']} {i}" for i in unexpected]
    checks["R1_index_set"] = r1
    r2 = []
    for i in sorted(set(b_objs) & set(live)):
        r2 += [f"{b_objs[i]['LedgerEntryType']} {i[:12]} {d}" for d in compare_object(live[i], b_objs[i])]
    checks["R2_fields"] = r2
    created, deleted = Counter(), Counter()
    for kind, t, *_ in effects:
        if kind == "CreatedNode":
            created[t] += 1
        elif kind == "DeletedNode":
            deleted[t] += 1
    ca = Counter(o["LedgerEntryType"] for o in a_objs.values())
    cb = Counter(o["LedgerEntryType"] for o in b_objs.values())
    checks["R3_counts"] = [
        f"{t}: B {cb[t]} != A {ca[t]} + created {created[t]} - deleted {deleted[t]}"
        for t in sorted(deferred_names)
        if cb[t] != ca[t] + created[t] - deleted[t]
    ]
    checks["R4_node_consistency"] = residuals
    touched = {(idx, h, seq) for _, _, idx, h, seq, _ in effects}
    r5 = []
    for i, o in sorted(b_objs.items()):
        seq = int(o.get("PreviousTxnLgrSeq", 0))
        if seq > a_hdr["ledger_index"]:
            if (i, o.get("PreviousTxnID"), seq) not in touched:
                r5.append(
                    f"{o['LedgerEntryType']} {i[:12]}: PreviousTxnID {o.get('PreviousTxnID')} not a window tx"
                )
        elif a_objs.get(i) != o:
            r5.append(
                f"{o['LedgerEntryType']} {i[:12]}: PreviousTxnLgrSeq {seq} <= A but object differs from A"
            )
    checks["R5_threading"] = r5
    return checks, effects


def transitions(a_objs, b_objs, effects, deferred_names):
    per = {t: {"created": 0, "deleted": 0, "modified": 0, "unchanged": 0} for t in sorted(deferred_names)}
    rows = []
    for i in sorted(set(a_objs) | set(b_objs)):
        a, b = a_objs.get(i), b_objs.get(i)
        t = (a or b)["LedgerEntryType"]
        if a is None:
            kind = "created"
        elif b is None:
            kind = "deleted"
        elif a != b:
            kind = "modified"
        else:
            kind = "unchanged"
        per[t][kind] += 1
        if kind != "unchanged":
            rows.append({"index": i, "type": t, "transition": kind})
    by_tx = Counter((ttype, kind, t) for kind, t, _, _, _, ttype in effects)
    caused_by = [
        {"tx_type": k[0], "node": k[1], "object_type": k[2], "n": v} for k, v in sorted(by_tx.items())
    ]
    return per, rows, caused_by


def sample_txs(txs, k, seed):
    hashes = sorted({x["tx"]["hash"] for x in txs})
    rng = random.Random(seed)
    return sorted(rng.sample(hashes, min(k, len(hashes))))


def analyze(a_path, b_path, txdir, types_path, sample=10, seed=1):
    with open(types_path) as fh:
        deferred_names = sorted(e["name"] for e in json.load(fh)["entries"] if e["deferred"])
    a_hdr, a_objs = load_snapshot(a_path)
    b_hdr, b_objs = load_snapshot(b_path)
    if b_hdr["ledger_index"] <= a_hdr["ledger_index"]:
        raise InputError(f"B {b_hdr['ledger_index']} is not after A {a_hdr['ledger_index']}")
    for h in (a_hdr, b_hdr):
        bad = sorted(t for t in h["deferred"] if t not in deferred_names)
        if bad:
            raise InputError(f"snapshot {h['ledger_index']} has types not deferred in types.json: {bad}")
    txs, problems = load_window(txdir, a_hdr["ledger_index"], b_hdr["ledger_index"])
    out = {
        "format": "pft-objlife-analysis/1",
        "A": census(a_hdr, a_objs, deferred_names),
        "B": census(b_hdr, b_objs, deferred_names),
        "window": {
            "ledgers": b_hdr["ledger_index"] - a_hdr["ledger_index"],
            "seconds": b_hdr["close_time"] - a_hdr["close_time"],
            "deferred_txs": len(txs),
            "completeness_problems": problems,
        },
    }
    if problems:
        out["reconciliation"] = {"status": "NOT_CHECKABLE", "checks": {}}
        return out, 2, []
    checks, effects = reconcile(a_hdr, a_objs, b_hdr, b_objs, txs, deferred_names)
    per, rows, caused_by = transitions(a_objs, b_objs, effects, deferred_names)
    out["transitions"] = per
    out["caused_by"] = caused_by
    out["nodes_replayed"] = len(effects)
    failed = sorted(k for k, v in checks.items() if v)
    out["reconciliation"] = {
        "status": "FAIL" if failed else "PASS",
        "failed": failed,
        "checks": {k: {"ok": not v, "residuals": v} for k, v in sorted(checks.items())},
    }
    out["sampled_txs"] = {"seed": seed, "k": sample, "hashes": sample_txs(txs, sample, seed)}
    return out, (1 if failed else 0), rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--tx", required=True)
    p.add_argument("--types", required=True)
    p.add_argument("--json", default=None)
    p.add_argument("--transitions", default=None, help="CSV of every non-unchanged object")
    p.add_argument("--sample", type=int, default=10)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args(argv)
    try:
        out, rc, rows = analyze(a.a, a.b, a.tx, a.types, a.sample, a.seed)
    except (InputError, OSError, KeyError, ValueError) as e:
        print(f"ERROR {type(e).__name__}: {e}")
        return 2
    text = json.dumps(out, indent=1, sort_keys=True) + "\n"
    if a.json:
        with open(a.json, "w") as fh:
            fh.write(text)
    if a.transitions:
        with open(a.transitions, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["index", "type", "transition"], lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    for side in ("A", "B"):
        c = out[side]
        print(
            f"{side} ledger {c['ledger_index']} {c['close_time_iso']}: {c['total']} deferred objects "
            f"{ {k: v for k, v in c['counts'].items() if v} } reserve {c['reserve']['drops']} drops, "
            f"expired_not_removed {c['expired_not_removed']}"
        )
    w = out["window"]
    print(f"window {w['ledgers']} ledgers / {w['seconds']} s, {w['deferred_txs']} deferred txs")
    for prob in w["completeness_problems"]:
        print(f"INCOMPLETE {prob}")
    rec = out["reconciliation"]
    for k, v in sorted(rec["checks"].items()):
        print(f"{k} {'OK' if v['ok'] else 'FAIL'} residuals={len(v['residuals'])}")
        for r in v["residuals"]:
            print(f"  RESIDUAL {r}")
    print(f"RECONCILIATION {rec['status']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
