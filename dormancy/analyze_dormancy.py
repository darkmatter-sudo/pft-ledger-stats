#!/usr/bin/env python3
"""Dormancy report: per-bucket accounts, PFT, reserves, OwnerCount, concentration and A->B transitions.

Python 3 stdlib only (charts need matplotlib, in the repo .venv). Read-only and offline: it reads
committed files only (snapshots, dormancy/data/anchors.csv, dormancy/data/headers.jsonl) and sends
nothing to any node.

Outputs in --out:
  REPORT.md        tables, reconciliation, time-map error bounds, transitions
  dormancy.json    every figure in REPORT.md (integers in drops)
  *.png            charts (--charts)

Exit 0 when every reconciliation check and transition invariant holds, 1 otherwise.

Usage:
  ./analyze_dormancy.py --snap A.jsonl.gz [--snap B.jsonl.gz] --anchors dormancy/data/anchors.csv \\
      --headers dormancy/data/headers.jsonl --out dormancy/report [--charts] [--top 10]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_dormancy as L  # noqa: E402

DROPS = 1_000_000
OC_BUCKETS = [("0", 0, 0), ("1", 1, 1), ("2-5", 2, 5), ("6-20", 6, 20), (">20", 21, None)]
SERIES = "#2a78d6"  # reference palette categorical slot 1 (light); single-series charts
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3df"
SEQ_RAMP = ["#f2f7fd", "#2a78d6", "#0b3a75"]  # sequential: one hue, light -> dark


def iso(ripple):
    return datetime.fromtimestamp(ripple + L.RIPPLE_EPOCH, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pft(drops):
    """Exact decimal PFT string of an integer drop amount (no float rounding)."""
    sign = "-" if drops < 0 else ""
    q, r = divmod(abs(int(drops)), DROPS)
    return f"{sign}{q:,}.{r:06d}"


def short(v):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= div:
            return f"{v / div:.3g}{suf}"
    return f"{v:.3g}"


def build_timemap(anchors_path, headers_path):
    anchors = L.load_anchor_csvs([anchors_path])
    headers = L.load_headers_jsonl(headers_path)
    for s in set(anchors) & set(headers):
        if anchors[s] != headers[s]:
            raise ValueError(f"anchor {s} differs from fetched header")
    merged = {**anchors, **headers}
    return L.TimeMap(merged), len(anchors), len(headers)


def oc_bucket(n):
    for label, lo, hi in OC_BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            return label
    raise AssertionError(n)


def timemap_summary(rows, tmap, header):
    kinds = {k: sum(1 for r in rows if r["kind"] == k) for k in ("exact", "interp", "before")}
    widths = [r["age_max"] - r["age_min"] for r in rows if r["kind"] == "interp"]
    return {
        "kinds": kinds,
        "interp_bound_max_s": max(widths) if widths else 0,
        "interp_bound_median_s": sorted(widths)[len(widths) // 2] if widths else 0,
        "oldest_header": tmap.seqs[0],
        "oldest_header_time": iso(tmap.times[0]),
        "before_age_floor_days": round((int(header["close_time"]) - tmap.times[0]) / L.DAY, 3),
    }


def analyze_snapshot(path, tmap, top_n):
    header, objs = L.load_snapshot(path)
    rows = L.classify_accounts(header, objs, tmap)
    table = L.bucket_table(rows, top_n=top_n)
    checks = L.reconcile(header, objs, rows, table)
    base, inc = L.reserve_params(objs, header)
    oc = {}
    for r in rows:
        k = (r["bucket"], oc_bucket(r["owner_count"]))
        oc[k] = oc.get(k, 0) + 1
    top = sorted(rows, key=lambda r: (-r["balance"], r["account"]))[:top_n]
    esc, pc = L.native_outside_accounts(objs)
    bal = [r["balance"] for r in rows]
    return {
        "file": os.path.basename(path),
        "ledger_index": int(header["ledger_index"]),
        "ledger_hash": header["ledger_hash"],
        "close_time": iso(int(header["close_time"])),
        "objects": len(objs),
        "accounts": len(rows),
        "total_coins": int(header["total_coins"]),
        "balance_drops": sum(bal),
        "escrow_drops": esc,
        "paychan_drops": pc,
        "reserve_base": base,
        "reserve_inc": inc,
        "gini_all": round(L.gini(bal), 6),
        "top_share_all": round(sum(sorted(bal, reverse=True)[:top_n]) / sum(bal), 6) if sum(bal) else 0.0,
        "buckets": table,
        "owner_count": {f"{b}|{o}": n for (b, o), n in sorted(oc.items())},
        "top": [
            {
                "account": r["account"],
                "balance_drops": r["balance"],
                "owner_count": r["owner_count"],
                "bucket": r["bucket"],
                "kind": r["kind"],
            }
            for r in top
        ],
        "timemap": timemap_summary(rows, tmap, header),
        "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks],
        "_rows": rows,
    }


def analyze(snaps, anchors, headers, top_n=10):
    tmap, n_anchor, n_head = build_timemap(anchors, headers)
    res = {"snapshots": [analyze_snapshot(p, tmap, top_n) for p in snaps], "top_n": top_n}
    res["timemap_sources"] = {"anchors": n_anchor, "fetched_headers": n_head, "merged": len(tmap.seqs)}
    if len(snaps) == 2:
        a, b = res["snapshots"]
        if not a["ledger_index"] < b["ledger_index"]:
            raise ValueError("snapshot A must be older than B")
        tr = L.transitions(a["_rows"], b["_rows"], a["ledger_index"], b["ledger_index"])
        created = sum(v for k, v in tr["matrix"].items() if k.startswith("created|"))
        deleted = sum(v for k, v in tr["matrix"].items() if k.endswith("|deleted"))
        tr["created"], tr["deleted"] = created, deleted
        tr["checks"] = [
            {
                "name": "accounts_conserved",
                "ok": a["accounts"] - deleted + created == b["accounts"],
                "detail": f"{a['accounts']} - {deleted} + {created} vs {b['accounts']}",
            },
            {
                "name": "matrix_covers_a",
                "ok": sum(v for k, v in tr["matrix"].items() if not k.startswith("created|"))
                == a["accounts"],
                "detail": "every A account has exactly one A->B cell",
            },
            {
                "name": "touched_plus_untouched",
                "ok": tr["touched"] + tr["untouched"] + deleted == a["accounts"],
                "detail": f"{tr['touched']} + {tr['untouched']} + {deleted} vs {a['accounts']}",
            },
            {"name": "invariants", "ok": not tr["problems"], "detail": f"{len(tr['problems'])} problems"},
        ]
        res["transitions"] = tr
    return res


def all_ok(res):
    ok = all(c["ok"] for s in res["snapshots"] for c in s["checks"])
    if "transitions" in res:
        ok = ok and all(c["ok"] for c in res["transitions"]["checks"])
    return ok


# ---------------------------------------------------------------- output


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def hours(s):
    return f"{s / 3600:.2f} h"


def report(res, files):
    out = ["# Account dormancy report", ""]
    out.append(
        "Generated by `dormancy/analyze_dormancy.py` from committed data only; every figure is in "
        "`dormancy.json`. Amounts are exact (integer drops, 1 PFT = 1,000,000 drops)."
    )
    out.append("")
    out.append("## Snapshots")
    out.append("")
    out.append(
        table(
            ["", "ledger", "ledger hash", "close time", "objects", "accounts", "total_coins (PFT)"],
            [
                [
                    chr(65 + i),
                    s["ledger_index"],
                    f"`{s['ledger_hash']}`",
                    s["close_time"],
                    s["objects"],
                    s["accounts"],
                    pft(s["total_coins"]),
                ]
                for i, s in enumerate(res["snapshots"])
            ],
        )
    )
    src = res["timemap_sources"]
    out += [
        "",
        f"Time map: {src['anchors']} committed anchors + {src['fetched_headers']} fetched headers "
        f"= {src['merged']} exact headers.",
        "",
    ]
    for i, s in enumerate(res["snapshots"]):
        name = chr(65 + i)
        tm = s["timemap"]
        k = tm["kinds"]
        out.append(f"## Snapshot {name} (ledger {s['ledger_index']})")
        out.append("")
        out.append(
            f"Last-activity time source: exact {k['exact']}, interp {k['interp']} (bound width max "
            f"{hours(tm['interp_bound_max_s'])}, median {hours(tm['interp_bound_median_s'])}), before "
            f"{k['before']} (older than the oldest known header {tm['oldest_header']} at "
            f"{tm['oldest_header_time']}, i.e. age >= {tm['before_age_floor_days']} d)."
        )
        out.append("")
        rows = []
        for label in L.LABELS:
            b = s["buckets"][label]
            rows.append(
                [
                    label,
                    b["accounts"],
                    f"[{b['accounts_min']}, {b['accounts_max']}]",
                    f"{b['by_kind']['exact']}/{b['by_kind']['interp']}/{b['by_kind']['before']}",
                    pft(b["balance_drops"]),
                    pft(b["reserve_drops"]),
                    b["owner_count"],
                    b["gini"],
                    b["top_share"],
                ]
            )
        out.append(
            table(
                [
                    "bucket",
                    "accounts",
                    "[min, max]",
                    "exact/interp/before",
                    "balance (PFT)",
                    "reserve (PFT)",
                    "OwnerCount",
                    "Gini",
                    f"top-{res['top_n']} share",
                ],
                rows,
            )
        )
        out.append("")
        out.append(
            "`[min, max]`: accounts whose whole age interval lies in the bucket / that the interval "
            "touches. The point count always lies between them (check `bounds_hold`)."
        )
        out.append("")
        out.append(
            f"All accounts: Gini {s['gini_all']}, top-{res['top_n']} share {s['top_share_all']}. "
            f"Reserve params: base {pft(s['reserve_base'])} + {pft(s['reserve_inc'])} per owned object "
            "(reserve per account = min(balance, base + inc x OwnerCount))."
        )
        out.append("")
        ocs = [o[0] for o in OC_BUCKETS]
        out.append(
            table(
                ["OwnerCount ->", *ocs],
                [[label, *[s["owner_count"].get(f"{label}|{o}", 0) for o in ocs]] for label in L.LABELS],
            )
        )
        out.append("")
        out.append(
            table(
                ["#", "account", "balance (PFT)", "OwnerCount", "bucket", "time source"],
                [
                    [
                        j + 1,
                        f"`{t['account']}`",
                        pft(t["balance_drops"]),
                        t["owner_count"],
                        t["bucket"],
                        t["kind"],
                    ]
                    for j, t in enumerate(s["top"])
                ],
            )
        )
        out.append("")
        out.append("Reconciliation (exact integers):")
        out.append("")
        out.append(
            table(
                ["check", "result", "detail"],
                [[c["name"], "OK" if c["ok"] else "FAIL", c["detail"]] for c in s["checks"]],
            )
        )
        out.append("")
    if "transitions" in res:
        tr = res["transitions"]
        states = [*L.LABELS]
        out.append("## Transitions A -> B")
        out.append("")
        out.append(
            f"Touched (new PreviousTxnID) {tr['touched']}, untouched {tr['untouched']}, "
            f"created {tr['created']}, deleted {tr['deleted']}."
        )
        out.append("")
        head = ["A \\ B", *states, "deleted"]
        rows = [
            [a, *[tr["matrix"].get(f"{a}|{b}", 0) for b in states], tr["matrix"].get(f"{a}|deleted", 0)]
            for a in states
        ]
        rows.append(["created", *[tr["matrix"].get(f"created|{b}", 0) for b in states], ""])
        out.append(table(head, rows))
        out.append("")
        out.append(
            table(
                ["check", "result", "detail"],
                [[c["name"], "OK" if c["ok"] else "FAIL", c["detail"]] for c in tr["checks"]],
            )
        )
        out.append("")
        for p in tr["problems"][:20]:
            out.append(f"- {p}")
    if files:
        out.append("## Charts")
        out.append("")
        out += [f"![{f}]({f})" for f in files]
        out.append("")
    out.append(f"Overall: {'PASS' if all_ok(res) else 'FAIL'}")
    return "\n".join(out)


def charts(res, out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def style(ax, title, xlabel, ylabel):
        ax.set_title(title, loc="left", fontsize=11, color=INK)
        ax.set_xlabel(xlabel, color=MUTED)
        ax.set_ylabel(ylabel, color=MUTED)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.tick_params(colors=MUTED)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)

    files = []
    for i, s in enumerate(res["snapshots"]):
        name = chr(65 + i)
        b = s["buckets"]
        cnt = [b[k]["accounts"] for k in L.LABELS]
        lo = [c - b[k]["accounts_min"] for c, k in zip(cnt, L.LABELS, strict=True)]
        hi = [b[k]["accounts_max"] - c for c, k in zip(cnt, L.LABELS, strict=True)]
        fig, ax = plt.subplots(figsize=(7, 3.6), dpi=120)
        bars = ax.bar(L.LABELS, cnt, color=SERIES, width=0.6, edgecolor="white", linewidth=2)
        ax.errorbar(L.LABELS, cnt, yerr=[lo, hi], fmt="none", ecolor=INK, elinewidth=1, capsize=4)
        ax.bar_label(bars, labels=[str(c) for c in cnt], color=INK, fontsize=8, padding=3)
        ax.set_yscale("log")
        style(
            ax,
            f"Accounts by dormancy, snapshot {name} (whiskers: [min, max])",
            "time since last activity",
            "accounts (log scale)",
        )
        fig.tight_layout()
        f = f"accounts_{name}.png"
        fig.savefig(os.path.join(out, f))
        plt.close(fig)
        files.append(f)

        fig, ax = plt.subplots(figsize=(7, 3.6), dpi=120)
        vals = [b[k]["balance_drops"] / DROPS for k in L.LABELS]
        bars = ax.bar(L.LABELS, vals, color=SERIES, width=0.6, edgecolor="white", linewidth=2)
        tot = sum(vals) or 1
        ax.set_yscale("log")
        ax.bar_label(
            bars,
            labels=[f"{short(v)} ({100 * v / tot:.3g}%)" if v else "0" for v in vals],
            color=INK,
            fontsize=7,
            padding=3,
        )
        style(
            ax, f"PFT held by dormancy bucket, snapshot {name}", "time since last activity", "PFT (log scale)"
        )
        fig.tight_layout()
        f = f"balance_{name}.png"
        fig.savefig(os.path.join(out, f))
        plt.close(fig)
        files.append(f)

    s = res["snapshots"][-1]
    vals = sorted((r["balance"] for r in s["_rows"]), reverse=True)
    tot = sum(vals) or 1
    ks = list(range(1, min(len(vals), 1000) + 1))
    cum, run = [], 0
    for v in vals[: len(ks)]:
        run += v
        cum.append(run / tot)
    fig, ax = plt.subplots(figsize=(6.4, 3.8), dpi=120)
    ax.plot(ks, cum, color=SERIES, linewidth=2)
    ax.set_xscale("log")
    for k in (1, 10, 100):
        if k <= len(cum):
            ax.annotate(
                f"top {k}: {100 * cum[k - 1]:.3f}%",
                (k, cum[k - 1]),
                textcoords="offset points",
                xytext=(6, -12),
                fontsize=8,
                color=INK,
            )
            ax.plot([k], [cum[k - 1]], "o", color=SERIES, markersize=5, markeredgecolor="white")
    letter = chr(64 + len(res["snapshots"]))
    style(
        ax,
        f"Share of PFT held by the k largest accounts, snapshot {letter}",
        "k largest accounts (log scale)",
        "share of all PFT",
    )
    fig.tight_layout()
    fig.savefig(os.path.join(out, "topk_share.png"))
    plt.close(fig)
    files.append("topk_share.png")

    if "transitions" in res:
        from matplotlib.colors import LinearSegmentedColormap, LogNorm

        tr = res["transitions"]
        rows_l = [*L.LABELS, "created"]
        cols_l = [*L.LABELS, "deleted"]
        grid = [[tr["matrix"].get(f"{a}|{b}", 0) for b in cols_l] for a in rows_l]
        fig, ax = plt.subplots(figsize=(6.4, 5), dpi=120)
        cmap = LinearSegmentedColormap.from_list("seq", SEQ_RAMP)
        cmap.set_bad("#ffffff")
        import numpy as np

        arr = np.ma.masked_equal(np.array(grid, dtype=float), 0)
        vmax = max(max(r) for r in grid) or 1
        ax.imshow(arr, cmap=cmap, norm=LogNorm(vmin=1, vmax=max(vmax, 2)))
        for y, r in enumerate(grid):
            for x, v in enumerate(r):
                if v:
                    ax.text(
                        x,
                        y,
                        str(v),
                        ha="center",
                        va="center",
                        fontsize=8,
                        color="white" if v > vmax**0.5 else INK,
                    )
        ax.set_xticks(range(len(cols_l)), cols_l, color=MUTED, fontsize=8)
        ax.set_yticks(range(len(rows_l)), rows_l, color=MUTED, fontsize=8)
        ax.set_title("Accounts moving between buckets, A -> B", loc="left", fontsize=11, color=INK)
        ax.set_xlabel("bucket at B", color=MUTED)
        ax.set_ylabel("bucket at A", color=MUTED)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        fig.tight_layout()
        fig.savefig(os.path.join(out, "transitions.png"))
        plt.close(fig)
        files.append("transitions.png")
    return files


def public(res):
    pub = {k: v for k, v in res.items() if k != "snapshots"}
    pub["snapshots"] = [{k: v for k, v in s.items() if not k.startswith("_")} for s in res["snapshots"]]
    return pub


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snap", action="append", required=True, help="snapshot A, then optionally B")
    ap.add_argument("--anchors", default="dormancy/data/anchors.csv")
    ap.add_argument("--headers", default="dormancy/data/headers.jsonl")
    ap.add_argument("--out", default="dormancy/report")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--charts", action="store_true")
    a = ap.parse_args(argv)
    if len(a.snap) > 2:
        ap.error("at most two snapshots")
    os.makedirs(a.out, exist_ok=True)
    res = analyze(a.snap, a.anchors, a.headers, a.top)
    files = charts(res, a.out) if a.charts else []
    with open(os.path.join(a.out, "dormancy.json"), "w") as fh:
        json.dump(public(res), fh, indent=1, sort_keys=True)
        fh.write("\n")
    with open(os.path.join(a.out, "REPORT.md"), "w") as fh:
        fh.write(report(res, files) + "\n")
    for i, s in enumerate(res["snapshots"]):
        bad = [c["name"] for c in s["checks"] if not c["ok"]]
        print(
            f"snapshot {chr(65 + i)} ledger {s['ledger_index']}: {s['accounts']} accounts, "
            f"checks {len(s['checks']) - len(bad)}/{len(s['checks'])} OK"
            f"{' FAIL ' + ','.join(bad) if bad else ''}"
        )
    if "transitions" in res:
        tr = res["transitions"]
        bad = [c["name"] for c in tr["checks"] if not c["ok"]]
        print(
            f"transitions: touched {tr['touched']} untouched {tr['untouched']} created {tr['created']} "
            f"deleted {tr['deleted']}, checks {len(tr['checks']) - len(bad)}/{len(tr['checks'])} OK"
        )
    ok = all_ok(res)
    print(
        f"RESULT {'PASS' if ok else 'FAIL'} -> {a.out}/REPORT.md, dormancy.json"
        f"{', ' + ', '.join(files) if files else ''}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
