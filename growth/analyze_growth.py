#!/usr/bin/env python3
"""Ledger growth, top contributors and online_delete disk sizing from a growth/ collection.

Offline: reads only committed files, never the network.

Inputs
  --data DIR         collect_growth.py output (ledgers.csv, tx.csv, nodes.csv, gaps.csv)
  --snap-a / --snap-b  state/snapshot.py jsonl.gz at window start (first ledger - 1) and window end
                     (last ledger); used for the per-type object count reconciliation
  --statebytes F     statebytes_growth.py JSON (exact serialized state size); give two or more at
                     different ledgers to get a measured state-bytes growth rate
  --nudb F           pft-retention samples.csv (same window, same node): NuDB + SQLite bytes and
                     complete_ledgers per 10-min sample
  --online-delete N  the sampled node's [node_db] online_delete (default 512)

Analyses
  coverage      first..last ledger, holes (exit 1 if a hole is not listed in gaps.csv), duplicate
                ledgers, tx rows == txn_count and node rows == created+modified+deleted per ledger
  hourly        per UTC hour: ledgers, txns, header/tx_blob/meta bytes, created/modified/deleted
  per_day       window totals scaled to 24 h by the measured mean ledger interval
  top           by tx type, by result, by (LedgerEntryType, action): count, bytes, share
  state         snapshot A + created - deleted == snapshot B per LedgerEntryType (exit 1 if not);
                serialized state bytes and its growth per day when two statebytes points exist
  nudb          least-squares fit nudb_bytes = fixed + per_ledger * retained_ledgers over the
                samples (retained = complete_hi - complete_lo + 1), same for SQLite; NuDB overhead
                ratio = measured mean NuDB bytes / raw projection (state bytes + mean retained x raw
                bytes per ledger) for the sampled online_delete
  sizing        for online_delete 256, 512, 2000, 8192, 32768: retained ledgers (N x the measured
                retained/online_delete ratio, min N, max 2N), raw bytes, NuDB estimate from the fit,
                SQLite estimate; full history: growth per day/year and the naive size of all
                ledgers so far at this window's rate (labelled as such)

Raw bytes per ledger = header (118) + tx_blob + metadata bytes, i.e. the serialized ledger content.
NuDB stores SHAMap nodes (tx tree + changed state leaves + inner nodes along their paths), so it
costs more than the raw bytes; the fit measures how much more instead of assuming it.
"""

import argparse
import csv
import gzip
import json
import os
import sys
from collections import Counter, defaultdict

SIZES = (256, 512, 2000, 8192, 32768)
DAY = 86400
MIN_R2 = 0.5  # below this a size-vs-retained fit is not used for projections
RIPPLE_EPOCH = 946684800


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def ints(row, keys):
    return {k: int(row[k]) for k in keys}


BYTE_KEYS = ("header_bytes", "tx_blob_bytes", "meta_bytes")
NODE_KEYS = ("created", "modified", "deleted")


def coverage(ledgers, txs, nodes, gaps):
    idx = [int(r["ledger_index"]) for r in ledgers]
    gap_idx = {int(g["ledger_index"]) for g in gaps}
    problems = []
    dup = [i for i, c in Counter(idx).items() if c > 1]
    if dup:
        problems.append(f"duplicate ledgers {sorted(dup)[:10]}")
    first, last = (min(idx), max(idx)) if idx else (None, None)
    holes = []
    if idx:
        have = set(idx)
        holes = [i for i in range(first, last + 1) if i not in have]
    unlisted = [h for h in holes if h not in gap_idx]
    if unlisted:
        problems.append(f"{len(unlisted)} unlisted holes, first {unlisted[:10]}")
    tx_per = Counter(int(t["ledger_index"]) for t in txs)
    node_per = Counter(int(n["ledger_index"]) for n in nodes)
    for r in ledgers:
        i = int(r["ledger_index"])
        if tx_per.get(i, 0) != int(r["txn_count"]):
            problems.append(f"ledger {i}: {tx_per.get(i, 0)} tx rows != txn_count {r['txn_count']}")
        want = sum(int(r[k]) for k in NODE_KEYS)
        if node_per.get(i, 0) != want:
            problems.append(f"ledger {i}: {node_per.get(i, 0)} node rows != {want}")
    stray = sorted(set(tx_per) - set(idx)) + sorted(set(node_per) - set(idx))
    if stray:
        problems.append(f"tx/node rows for ledgers not in ledgers.csv: {stray[:10]}")
    sources = Counter(r["source"] for r in ledgers)
    return {
        "first": first,
        "last": last,
        "ledgers": len(idx),
        "holes": len(holes),
        "gaps_listed": len(gap_idx),
        "sources": dict(sorted(sources.items())),
        "problems": problems,
    }


def raw_bytes(r):
    return sum(int(r[k]) for k in BYTE_KEYS)


def hourly(ledgers):
    hours = defaultdict(lambda: {"ledgers": 0, "txns": 0, **{k: 0 for k in BYTE_KEYS + NODE_KEYS}})
    for r in ledgers:
        h = hours[r["close_time_iso"][:13] + ":00Z"]
        h["ledgers"] += 1
        h["txns"] += int(r["txn_count"])
        for k in BYTE_KEYS + NODE_KEYS:
            h[k] += int(r[k])
    out = []
    for hour in sorted(hours):
        h = hours[hour]
        out.append({"hour": hour, **h, "raw_bytes": sum(h[k] for k in BYTE_KEYS)})
    return out


def totals(ledgers):
    t = {"ledgers": len(ledgers), "txns": sum(int(r["txn_count"]) for r in ledgers)}
    for k in BYTE_KEYS + NODE_KEYS:
        t[k] = sum(int(r[k]) for r in ledgers)
    t["raw_bytes"] = sum(t[k] for k in BYTE_KEYS)
    return t


def per_day(ledgers):
    """Totals scaled to 24 h by the mean close interval (first..last close time)."""
    tot = totals(ledgers)
    ct = sorted(int(r["close_time"]) for r in ledgers)
    if len(ct) < 2 or ct[-1] == ct[0]:
        return tot, None, None
    interval = (ct[-1] - ct[0]) / (len(ct) - 1)
    lpd = DAY / interval
    scale = lpd / len(ledgers)
    day = {k: v * scale for k, v in tot.items()}
    day["ledgers"] = lpd
    return tot, interval, day


def top(txs, nodes):
    by_type = defaultdict(lambda: {"count": 0, "blob_bytes": 0, "meta_bytes": 0, "created": 0, "deleted": 0})
    by_result = Counter()
    for t in txs:
        b = by_type[t["tx_type"]]
        b["count"] += 1
        b["blob_bytes"] += int(t["blob_bytes"])
        b["meta_bytes"] += int(t["meta_bytes"])
        b["created"] += int(t["created"])
        b["deleted"] += int(t["deleted"])
        by_result[t["result"]] += 1
    tx_bytes = sum(b["blob_bytes"] + b["meta_bytes"] for b in by_type.values())
    types = []
    for name, b in by_type.items():
        byt = b["blob_bytes"] + b["meta_bytes"]
        share = byt / tx_bytes if tx_bytes else 0.0
        types.append({"tx_type": name, **b, "bytes": byt, "byte_share": share})
    types.sort(key=lambda x: (-x["bytes"], x["tx_type"]))
    ent = Counter((n["entry_type"], n["action"]) for n in nodes)
    entries = [{"entry_type": e, "action": a, "count": c} for (e, a), c in ent.items()]
    entries.sort(key=lambda x: (-x["count"], x["entry_type"], x["action"]))
    n_tx = len(txs)
    results = [{"result": r, "count": c, "share": c / n_tx if n_tx else 0.0} for r, c in by_result.items()]
    results.sort(key=lambda x: (-x["count"], x["result"]))
    return {"tx_types": types, "results": results, "entries": entries, "tx_bytes": tx_bytes}


def snapshot_counts(path):
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        c = Counter(json.loads(line)["LedgerEntryType"] for line in fh)
    return header, c


def state_reconcile(snap_a, snap_b, nodes, first, last):
    """Snapshot A (first-1) + created - deleted over first..last must equal snapshot B (last)."""
    ha, ca = snapshot_counts(snap_a)
    hb, cb = snapshot_counts(snap_b)
    problems = []
    if int(ha["ledger_index"]) != first - 1:
        problems.append(f"snapshot A ledger {ha['ledger_index']} != first-1 {first - 1}")
    if int(hb["ledger_index"]) != last:
        problems.append(f"snapshot B ledger {hb['ledger_index']} != last {last}")
    created, deleted = Counter(), Counter()
    for n in nodes:
        if first <= int(n["ledger_index"]) <= last:
            if n["action"] == "created":
                created[n["entry_type"]] += 1
            elif n["action"] == "deleted":
                deleted[n["entry_type"]] += 1
    rows = []
    for t in sorted(set(ca) | set(cb) | set(created) | set(deleted)):
        want = ca.get(t, 0) + created.get(t, 0) - deleted.get(t, 0)
        ok = want == cb.get(t, 0)
        if not ok:
            problems.append(
                f"{t}: A {ca.get(t, 0)} + {created.get(t, 0)} - {deleted.get(t, 0)} != B {cb.get(t, 0)}"
            )
        rows.append(
            {
                "entry_type": t,
                "a": ca.get(t, 0),
                "created": created.get(t, 0),
                "deleted": deleted.get(t, 0),
                "b": cb.get(t, 0),
                "ok": ok,
            }
        )
    return {
        "a_ledger": int(ha["ledger_index"]),
        "b_ledger": int(hb["ledger_index"]),
        "a_objects": sum(ca.values()),
        "b_objects": sum(cb.values()),
        "types": rows,
        "problems": problems,
    }


def state_bytes(points, interval):
    """Latest exact state size and, with >= 2 points, its measured growth per day."""
    pts = sorted(points, key=lambda p: p["ledger_index"])
    last = pts[-1]
    res = {
        "points": [
            {"ledger_index": p["ledger_index"], "objects": p["objects"], "bytes": p["bytes"]} for p in pts
        ],
        "bytes": last["bytes"],
        "objects": last["objects"],
        "by_type": last["by_type"],
        "growth_per_ledger": None,
        "growth_per_day": None,
    }
    if len(pts) >= 2 and pts[-1]["ledger_index"] > pts[0]["ledger_index"]:
        g = (pts[-1]["bytes"] - pts[0]["bytes"]) / (pts[-1]["ledger_index"] - pts[0]["ledger_index"])
        res["growth_per_ledger"] = g
        if interval:
            res["growth_per_day"] = g * DAY / interval
    return res


def fit_line(xs, ys):
    """Least squares y = a + b x; returns a, b, r2 (None if x has no spread)."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return {"fixed": a, "per_ledger": b, "r2": r2}


def nudb_samples(rows, start=None, end=None):
    out = []
    for r in rows:
        e = int(r["epoch"])
        if r["server_state"] != "full" or (start is not None and e < start) or (end is not None and e > end):
            continue
        lo, hi = int(r["complete_lo"]), int(r["complete_hi"])
        if lo <= 0 or hi < lo:
            continue
        out.append(
            {
                "epoch": e,
                "retained": hi - lo + 1,
                "nudb": int(r["nudb_bytes"]),
                "sqlite": int(r["sqlite_bytes"]),
            }
        )
    return out


def nudb(samples, online_delete, state_b, raw_per_ledger):
    if not samples:
        return None
    ret = [s["retained"] for s in samples]
    nb = [s["nudb"] for s in samples]
    sq = [s["sqlite"] for s in samples]
    mean_ret = sum(ret) / len(ret)
    mean_nudb = sum(nb) / len(nb)
    raw_proj = state_b + mean_ret * raw_per_ledger
    return {
        "samples": len(samples),
        "online_delete": online_delete,
        "retained": {"min": min(ret), "max": max(ret), "mean": mean_ret},
        "retained_ratio": {
            "min": min(ret) / online_delete,
            "max": max(ret) / online_delete,
            "mean": mean_ret / online_delete,
        },
        "nudb_bytes": {"min": min(nb), "max": max(nb), "mean": mean_nudb},
        "sqlite_bytes": {"min": min(sq), "max": max(sq), "mean": sum(sq) / len(sq)},
        "raw_projection_bytes": raw_proj,
        "overhead_ratio": mean_nudb / raw_proj if raw_proj else None,
        "fit_nudb": fit_line(ret, nb),
        "fit_sqlite": fit_line(ret, sq),
    }


def sizing(nd, state_b, raw_per_ledger, ledgers_per_day, last_ledger, state_growth_day):
    ratio = nd["retained_ratio"]["mean"] if nd else 1.5
    fn = nd["fit_nudb"] if nd else None
    fs = nd["fit_sqlite"] if nd else None
    if fn and fn["r2"] < MIN_R2:
        fn = None
    # SQLite (ledger/transaction db + WAL) need not scale with retained ledgers; with no usable
    # fit, size it as the measured mean, constant across online_delete (labelled in the output).
    sqlite_mode = "fit" if fs and fs["r2"] >= MIN_R2 else "measured_mean"
    if sqlite_mode == "measured_mean":
        fs = {"fixed": nd["sqlite_bytes"]["mean"], "per_ledger": 0.0} if nd else None
    rows = []
    for n in SIZES:
        ret = n * ratio
        row = {
            "online_delete": n,
            "retained_min": n,
            "retained_mean": ret,
            "retained_max": 2 * n,
            "raw_bytes": state_b + ret * raw_per_ledger,
            "raw_bytes_max": state_b + 2 * n * raw_per_ledger,
            "nudb_bytes": fn["fixed"] + fn["per_ledger"] * ret if fn else None,
            "nudb_bytes_max": fn["fixed"] + fn["per_ledger"] * 2 * n if fn else None,
            "sqlite_bytes": fs["fixed"] + fs["per_ledger"] * ret if fs else None,
        }
        rows.append(row)
    per_ledger_disk = fn["per_ledger"] + (fs["per_ledger"] if fs else 0) if fn else None
    full = {
        "raw_bytes_per_day": raw_per_ledger * ledgers_per_day + (state_growth_day or 0),
        "disk_bytes_per_day": per_ledger_disk * ledgers_per_day + (state_growth_day or 0)
        if per_ledger_disk is not None
        else None,
        "naive_all_ledgers_disk_bytes": per_ledger_disk * last_ledger
        if per_ledger_disk is not None
        else None,
        "note": "naive_all_ledgers_*: every ledger since genesis assumed to cost what this window's cost",
    }
    full["raw_bytes_per_year"] = full["raw_bytes_per_day"] * 365
    full["disk_bytes_per_year"] = full["disk_bytes_per_day"] * 365 if full["disk_bytes_per_day"] else None
    return {"assumed_retained_ratio": ratio, "sqlite_mode": sqlite_mode, "rows": rows, "full_history": full}


def analyze(
    ledgers, txs, nodes, gaps, snap_a=None, snap_b=None, statebytes=(), nudb_rows=(), online_delete=512
):
    cov = coverage(ledgers, txs, nodes, gaps)
    tot, interval, day = per_day(ledgers)
    raw_per_ledger = tot["raw_bytes"] / tot["ledgers"] if tot["ledgers"] else 0.0
    res = {
        "coverage": cov,
        "totals": tot,
        "mean_interval_s": interval,
        "per_day": day,
        "raw_bytes_per_ledger": raw_per_ledger,
        "hourly": hourly(ledgers),
        "top": top(txs, nodes),
        "state": None,
        "state_bytes": None,
        "nudb": None,
        "sizing": None,
        "problems": list(cov["problems"]),
    }
    if snap_a and snap_b and cov["first"] is not None:
        st = state_reconcile(snap_a, snap_b, nodes, cov["first"], cov["last"])
        res["state"] = st
        res["problems"] += st["problems"]
    if statebytes:
        sb = state_bytes(list(statebytes), interval)
        res["state_bytes"] = sb
        ctimes = [int(r["close_time"]) for r in ledgers]
        start = min(ctimes) + RIPPLE_EPOCH if ctimes else None
        end = max(ctimes) + RIPPLE_EPOCH if ctimes else None
        res["nudb"] = nudb(nudb_samples(nudb_rows, start, end), online_delete, sb["bytes"], raw_per_ledger)
        res["sizing"] = sizing(
            res["nudb"],
            sb["bytes"],
            raw_per_ledger,
            day["ledgers"] if day else 0,
            cov["last"] or 0,
            sb["growth_per_day"],
        )
    return res


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def round_floats(o, nd=6):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: round_floats(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [round_floats(v, nd) for v in o]
    return o


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", required=True)
    p.add_argument("--snap-a")
    p.add_argument("--snap-b")
    p.add_argument("--statebytes", action="append", default=[])
    p.add_argument("--nudb")
    p.add_argument("--online-delete", type=int, default=512)
    p.add_argument("--out", required=True)
    p.add_argument("--check", help="recorded growth.json: recompute and require an identical result")
    a = p.parse_args(argv)
    d = a.data
    sb = []
    for f in a.statebytes:
        with open(f) as fh:
            sb.append(json.load(fh))
    res = round_floats(
        analyze(
            read_csv(os.path.join(d, "ledgers.csv")),
            read_csv(os.path.join(d, "tx.csv")),
            read_csv(os.path.join(d, "nodes.csv")),
            read_csv(os.path.join(d, "gaps.csv")),
            a.snap_a,
            a.snap_b,
            sb,
            read_csv(a.nudb) if a.nudb else [],
            a.online_delete,
        )
    )
    text = json.dumps(res, indent=1, sort_keys=True) + "\n"
    if a.check:
        with open(a.check) as fh:
            same = fh.read() == text
        print(
            f"CHECK recomputed result {'IDENTICAL to' if same else 'DIFFERS from'}",
            os.path.basename(a.check),
        )
        return 0 if same and not res["problems"] else 1
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "growth.json"), "w") as fh:
        fh.write(text)
    hk = ["hour", "ledgers", "txns", *BYTE_KEYS, "raw_bytes", *NODE_KEYS]
    write_csv(os.path.join(a.out, "hourly.csv"), res["hourly"], hk)
    if res["sizing"]:
        write_csv(os.path.join(a.out, "sizing.csv"), res["sizing"]["rows"], list(res["sizing"]["rows"][0]))
    c = res["coverage"]
    print(f"ledgers {c['first']}..{c['last']} n={c['ledgers']} holes={c['holes']} sources={c['sources']}")
    print(
        f"txns {res['totals']['txns']} raw bytes {res['totals']['raw_bytes']}",
        f"per ledger {res['raw_bytes_per_ledger']}",
    )
    if res["nudb"]:
        nd = res["nudb"]
        print(f"nudb samples {nd['samples']} retained {nd['retained']} overhead ratio {nd['overhead_ratio']}")
        print(f"fit nudb {nd['fit_nudb']} fit sqlite {nd['fit_sqlite']}")
    for pr in res["problems"]:
        print(f"PROBLEM {pr}")
    print(f"RESULT {'FAIL' if res['problems'] else 'OK'} problems={len(res['problems'])}")
    return 1 if res["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
