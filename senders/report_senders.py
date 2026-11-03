#!/usr/bin/env python3
"""REPORT.md and charts for a senders/ collection (matplotlib for the charts, everything else stdlib).

  report_senders.py --data DIR --out-dir senders [--json senders.json] [--seed S] [--n N]

Runs analyze() on DIR (tx.csv, ledgers.csv, gaps.csv), the planted and shuffled controls of
controls_senders.py on the same ledger timeline, and writes:

  REPORT.md       coverage, daily active/new/returning, retention matrix, class shares, regularity
                  and dominant-period statistics, most active senders (truncated), controls
  cohorts.png     active senders per slice, new vs returning
  regularity.png  events vs CV of inter-arrival gaps: real senders and planted controls, class lines
  gaps.png        pooled inter-arrival gaps / each sender's mean gap vs the exponential density

Every number in REPORT.md is computed here from the data; nothing is typed in by hand.
Exit 1 if analyze() finds the collection incomplete (unlisted hole, duplicate ledger, rows !=
txn_count sum) or a control fails.
"""

import argparse
import json
import math
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import controls_senders as ctl  # noqa: E402
from analyze_senders import (  # noqa: E402
    BURSTY_B,
    CLASSES,
    MIN_EVENTS,
    PERIOD_SHARE,
    PERIOD_TOL,
    PERIODIC_CV,
    analyze,
    gap_stats,
    read_csv,
    truncate,
)

# categorical slots 1-4 of the default validated palette, in fixed order
C_REAL, C_PERIODIC, C_POISSON, C_BURST = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, INK2, GRID_C = "#0b0b0b", "#52514e", "#e4e3df"


def pct(x):
    return f"{100 * x:.1f} %"


def quartiles(xs):
    s = sorted(xs)
    if not s:
        return (None, None, None)

    def q(p):
        k = (len(s) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    return (q(0.25), q(0.5), q(0.75))


def fmt(v, nd=3):
    return "-" if v is None else f"{v:.{nd}f}"


def planted_points(pool, n, seed):
    """(name, events, cv) for n planted senders per generator on the real ledger timeline."""
    import bisect

    t0, out = pool[0], []
    for name, (gen, _want) in ctl.GENERATORS.items():
        rng = random.Random(f"{seed}:planted:{name}")
        for _ in range(n):
            idx = (bisect.bisect_left(pool, t0 + t) for t in gen(rng))
            st = gap_stats(sorted({pool[i] for i in idx if i < len(pool)}))
            out.append((name, st["n"], st["cv"]))
    return out


def normalized_gaps(txs):
    """Every eligible sender's inter-arrival gaps divided by that sender's mean gap (pooled)."""
    ev = {}
    for r in txs:
        ev.setdefault(r["account"], set()).add(int(r["close_time"]))
    out = []
    for times in ev.values():
        t = sorted(times)
        if len(t) < MIN_EVENTS:
            continue
        g = [b - a for a, b in zip(t, t[1:], strict=False)]
        m = sum(g) / len(g)
        if m > 0:
            out.extend(x / m for x in g)
    return out


def charts(res, txs, pool, out_dir, n, seed):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": INK2,
            "axes.labelcolor": INK,
            "xtick.color": INK2,
            "ytick.color": INK2,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    files = []

    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    days = res["days"]
    xs = [d["slice"] for d in days]
    ret = [d["returning"] for d in days]
    new = [d["new"] for d in days]
    ax.bar(xs, ret, width=0.55, color=C_REAL, edgecolor="white", linewidth=2, label="returning")
    ax.bar(xs, new, width=0.55, bottom=ret, color=C_PERIODIC, edgecolor="white", linewidth=2, label="new")
    for x, r, nw in zip(xs, ret, new, strict=True):
        ax.text(
            x, r + nw + 0.8, f"{r + nw} active ({nw} new)", ha="center", va="bottom", color=INK, fontsize=8
        )
    ax.set_xticks(xs, [f"slice {x}" for x in xs])
    ax.set_ylabel("senders")
    ax.set_ylim(0, max(r + nw for r, nw in zip(ret, new, strict=True)) * 1.18 + 1)
    ax.yaxis.grid(True, color=GRID_C, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", ncol=2)
    ax.set_title("Active senders per 24 h slice", color=INK, loc="left")
    fig.tight_layout()
    files.append("cohorts.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    pts = planted_points(pool, n, seed)
    for name, color, label in (
        ("periodic", C_PERIODIC, "planted periodic"),
        ("poisson", C_POISSON, "planted Poisson"),
        ("burst", C_BURST, "planted burst"),
    ):
        p = [(e, c) for nm, e, c in pts if nm == name and c is not None]
        ax.scatter(
            [e for e, _ in p],
            [c for _, c in p],
            s=14,
            color=color,
            alpha=0.45,
            linewidths=0,
            label=f"{label} (n={len(p)})",
        )
    real = [(s["n"], s["cv"]) for s in res["per_sender"].values() if s["cv"] is not None]
    ax.scatter(
        [e for e, _ in real],
        [c for _, c in real],
        s=30,
        color=C_REAL,
        edgecolors="white",
        linewidths=1,
        label=f"real senders (n={len(real)})",
        zorder=3,
    )
    ax.axhline(PERIODIC_CV, color=INK2, linestyle="--", linewidth=1)
    ax.axhline((1 + BURSTY_B) / (1 - BURSTY_B), color=INK2, linestyle=":", linewidth=1)
    ax.text(
        1.02,
        PERIODIC_CV,
        f"periodic: CV <= {PERIODIC_CV}",
        transform=ax.get_yaxis_transform(),
        va="center",
        fontsize=7,
        color=INK2,
    )
    ax.text(
        1.02,
        (1 + BURSTY_B) / (1 - BURSTY_B),
        f"bursty: B >= {BURSTY_B}",
        transform=ax.get_yaxis_transform(),
        va="center",
        fontsize=7,
        color=INK2,
    )
    ax.axvline(MIN_EVENTS, color=GRID_C, linewidth=1)
    ax.set_xscale("log")
    ax.set_xlabel("events (distinct ledgers sent in), log scale")
    ax.set_ylabel("CV of inter-arrival gaps")
    ax.legend(frameon=False, fontsize=7, loc="upper right")
    ax.set_title("Sender regularity vs planted controls on the same ledger timeline", color=INK, loc="left")
    fig.tight_layout()
    files.append("regularity.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    g = normalized_gaps(txs)
    top = 6.0
    ax.hist(
        [x for x in g if x <= top],
        bins=48,
        range=(0, top),
        density=True,
        color=C_REAL,
        edgecolor="white",
        linewidth=0.6,
        label=f"real gaps / sender mean (n={len(g)})",
    )
    xs2 = [i * top / 200 for i in range(201)]
    ax.plot(xs2, [math.exp(-x) for x in xs2], color=INK, linewidth=2, label="exponential (Poisson) density")
    ax.set_xlabel("inter-arrival gap / that sender's mean gap")
    ax.set_ylabel("density")
    ax.legend(frameon=False)
    ax.set_title("Inter-arrival gaps of eligible senders", color=INK, loc="left")
    fig.tight_layout()
    files.append("gaps.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)
    return files


def build(res, txs, ledgers, pos, planted, neg, files):
    cov, tot, win = res["coverage"], res["totals"], res["window"]
    per = res["per_sender"]
    elig = {a: s for a, s in per.items() if s["n"] >= MIN_EVENTS}
    L = ["# senders: 72 h sender activity report", ""]
    L.append(
        f"Ledgers {cov['first']}-{cov['last']} ({cov['fetched']} fetched, {cov['gaps']} gaps, "
        f"{len(cov['holes'])} unlisted holes, {cov.get('duplicates', 0)} duplicates), "
        f"{win['hours']} h of close time cut into {win['slices']} slices of 24 h (the last one absorbs "
        f"the remainder). Generated by `senders/report_senders.py`; every number below is computed from "
        f"the committed data."
    )
    L += ["", "## Totals", "", "| measure | value |", "|---|---|"]
    L += [
        f"| transactions (tx.csv rows) | {tot['tx']} |",
        f"| sum of ledger `txn_count` | {tot['ledger_txn_count']} |",
        f"| distinct senders | {tot['senders']} |",
        f"| senders with >= {MIN_EVENTS} events (classifiable) | {len(elig)} |",
        f"| fees | {tot['fee_drops']} drops |",
        f"| failed (result != tesSUCCESS) | {tot['failed']} "
        f"({pct(tot['failed'] / tot['tx'] if tot['tx'] else 0)}) |",
    ]
    L += [
        "",
        "## Daily active, new and returning senders",
        "",
        "| slice | active | new | returning | transactions |",
        "|---|---|---|---|---|",
    ]
    L += [
        f"| {d['slice']} | {d['active']} | {d['new']} | {d['returning']} | {d['tx']} |" for d in res["days"]
    ]
    L += ["", '"New" is window-relative: first seen in this window.', "", "![cohorts.png](cohorts.png)"]
    L += [
        "",
        "## Retention (cohort = slice of first appearance)",
        "",
        "| cohort | size | " + " | ".join(f"slice {k}" for k in range(win["slices"])) + " |",
        "|---|---|" + "---|" * win["slices"],
    ]
    for r in res["retention"]:
        cells = [
            "-" if v is None else f"{v} ({pct(v / r['size']) if r['size'] else '-'})" for v in r["active"]
        ]
        L.append(f"| {r['cohort']} | {r['size']} | " + " | ".join(cells) + " |")
    L += [
        "",
        "## Classes",
        "",
        f"Rule (fixed constants in `analyze_senders.py`): *periodic* if n >= {MIN_EVENTS}, "
        f"CV <= {PERIODIC_CV} and >= {PERIOD_SHARE:.0%} of gaps within +-{PERIOD_TOL:.0%} of the median "
        f"gap; *bursty* if burstiness B >= {BURSTY_B}; otherwise *sporadic* (including every sender "
        f"with fewer than {MIN_EVENTS} events).",
        "",
        "| class | senders | share | transactions | share | fees (drops) | share | failed "
        "| share of failures | fail rate |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in CLASSES:
        s = res["shares"][c]
        L.append(
            f"| {c} | {s['senders']} | {pct(s['sender_share'])} | {s['tx']} | {pct(s['tx_share'])} | "
            f"{s['fee_drops']} | {pct(s['fee_share'])} | {s['failed']} | {pct(s['failed_share'])} | "
            f"{s['fail_rate']:.2%} |"
        )
    q_cv = quartiles([s["cv"] for s in elig.values()])
    q_b = quartiles([s["b"] for s in elig.values()])
    q_ps = quartiles([s["period_share"] for s in elig.values()])
    q_med = quartiles([s["median_gap"] for s in elig.values()])
    q_mean = quartiles([s["mean_gap"] for s in elig.values()])
    L += [
        "",
        "## Regularity and dominant period (eligible senders)",
        "",
        "| statistic | q1 | median | q3 |",
        "|---|---|---|---|",
        f"| CV of gaps | {fmt(q_cv[0])} | {fmt(q_cv[1])} | {fmt(q_cv[2])} |",
        f"| burstiness B | {fmt(q_b[0])} | {fmt(q_b[1])} | {fmt(q_b[2])} |",
        f"| period share (gaps within +-{PERIOD_TOL:.0%} of median) | {fmt(q_ps[0])} | {fmt(q_ps[1])} | "
        f"{fmt(q_ps[2])} |",
        f"| dominant period = median gap (s) | {fmt(q_med[0], 1)} | {fmt(q_med[1], 1)} | "
        f"{fmt(q_med[2], 1)} |",
        f"| mean gap (s) | {fmt(q_mean[0], 1)} | {fmt(q_mean[1], 1)} | {fmt(q_mean[2], 1)} |",
    ]
    ratio = quartiles([s["median_gap"] / s["mean_gap"] for s in elig.values() if s["mean_gap"]])
    L += [
        "",
        f"Median gap / mean gap: median {fmt(ratio[1])} (an exponential distribution gives ln 2 = 0.693, "
        "a clock gives 1.0).",
        "",
        "![regularity.png](regularity.png)",
        "",
        "![gaps.png](gaps.png)",
    ]
    L += [
        "",
        "## Most active senders (addresses truncated)",
        "",
        "| sender | tx | events | median gap (s) | CV | B | period share | class | top type | failed |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a, s in sorted(per.items(), key=lambda x: (-x[1]["tx"], x[0]))[:10]:
        L.append(
            f"| {truncate(a)} | {s['tx']} | {s['n']} | {fmt(s['median_gap'], 1)} | {fmt(s['cv'])} | "
            f"{fmt(s['b'])} | {fmt(s['period_share'])} | {s['class']} | {s['top_type']} | {s['failed']} |"
        )
    if res["top_periodic"]:
        L += [
            "",
            "## Top periodic senders",
            "",
            "| sender | tx | events | period (s) | CV | period share |",
            "|---|---|---|---|---|---|",
        ]
        L += [
            f"| {t['account']} | {t['tx']} | {t['events']} | {t['period_s']} | {t['cv']} | "
            f"{t['period_share']} |"
            for t in res["top_periodic"]
        ]
    else:
        L += ["", "Top periodic senders: none (no sender meets the periodic rule)."]
    types = Counter(r["tx_type"] for r in txs)
    L += ["", "## Transaction types", "", "| type | transactions | share |", "|---|---|---|"]
    L += [f"| {t} | {k} | {pct(k / len(txs))} |" for t, k in types.most_common()]
    L += [
        "",
        "## Controls",
        "",
        "| control | expected | correct | classes | result |",
        "|---|---|---|---|---|",
    ]
    for r in pos + planted:
        L.append(
            f"| {r['control']} | {r['expected']} | {r['correct']}/{r['n']} | {r['classes']} | "
            f"{'PASS' if r['ok'] else 'FAIL'} |"
        )
    L += [
        "",
        f"Shuffled-timestamp negative control: {neg['eligible']} eligible senders, real classes "
        f"{neg['real_classes']}, shuffled classes {neg['shuffled_classes']}, "
        "real periodic that stayed periodic "
        f"{neg['shuffled_periodic']}/{neg['real_periodic']}; median CV real {fmt(neg['median_cv_real'])} vs "
        f"shuffled {fmt(neg['median_cv_shuffled'])}: {'PASS' if neg['ok'] else 'FAIL'}.",
    ]
    L.append("")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--json", default=None, help="also write the analyze() result here")
    p.add_argument("--seed", default="senders-controls-1")
    p.add_argument("--n", type=int, default=200, help="senders per positive/planted control")
    p.add_argument("--no-charts", action="store_true")
    a = p.parse_args(argv)
    txs = read_csv(os.path.join(a.data, "tx.csv"))
    ledgers = read_csv(os.path.join(a.data, "ledgers.csv"))
    res = analyze(txs, ledgers, read_csv(os.path.join(a.data, "gaps.csv")))
    cov, tot = res["coverage"], res["totals"]
    bad = bool(cov["holes"] or cov.get("duplicates") or tot["tx"] != tot["ledger_txn_count"])
    pos = []
    for name in ctl.GENERATORS:
        r = ctl.run_positive(name, a.n, a.seed)
        r["ok"] = r["correct"] >= 0.95 * r["n"]
        pos.append(r)
    planted = ctl.run_planted(a.data, a.n, a.seed)
    for r in planted:
        r["ok"] = r["correct"] >= 0.95 * r["n"]
    neg = ctl.run_negative(a.data, a.seed)
    lim = 0.01 * max(neg["eligible"], 1)
    neg["ok"] = neg["shuffled_periodic"] <= lim and neg["shuffled_classes"].get("periodic", 0) <= lim
    pool = sorted({int(r["close_time"]) for r in ledgers})
    files = [] if a.no_charts else charts(res, txs, pool, a.out_dir, min(a.n, 60), a.seed)
    with open(os.path.join(a.out_dir, "REPORT.md"), "w") as fh:
        fh.write(build(res, txs, ledgers, pos, planted, neg, files))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
            fh.write("\n")
    failed = [r["control"] for r in pos + planted if not r["ok"]] + ([] if neg["ok"] else ["shuffled"])
    print(
        f"REPORT {os.path.join(a.out_dir, 'REPORT.md')} charts={files} "
        f"senders={tot['senders']} tx={tot['tx']} "
        f"classes={ {c: res['shares'][c]['senders'] for c in CLASSES} } controls failed={failed} "
        f"incomplete={bad}"
    )
    return 1 if bad or failed else 0


if __name__ == "__main__":
    sys.exit(main())
