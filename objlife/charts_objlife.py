#!/usr/bin/env python3
"""Two REPORT.md charts for objlife/ (matplotlib, in the repo .venv). Reads only files.

    charts_objlife.py --analysis analysis.json --ledgers ledgers.csv --a A --b B --out DIR

  chart-window.png  transactions per UTC hour in the A->B window (ledgers.csv txn_count) with the
                    deferred-object tx count (obj_tx_count) of the same hours
  chart-census.png  deferred objects per type at A and at B (analysis.json counts)

Every number drawn is printed as a CHART line, so the figures can be checked against the data.
"""

import argparse
import csv
import json
import os
from datetime import datetime, timezone

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
S1, S2 = "#2a78d6", "#eb6834"  # categorical slots 1 and 2 (validated pair)


def hourly(path, a, b):
    hours = {}
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if a < int(r["ledger_index"]) <= b:
                h = r["close_time_iso"][:13]
                t = hours.setdefault(h, [0, 0, 0])
                t[0] += 1
                t[1] += int(r["txn_count"])
                t[2] += int(r["obj_tx_count"])
    return sorted(hours.items())


def style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--analysis", required=True)
    p.add_argument("--ledgers", required=True)
    p.add_argument("--a", type=int, required=True)
    p.add_argument("--b", type=int, required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = hourly(args.ledgers, args.a, args.b)
    for h, (n, txs, obj) in rows:
        print(f"CHART window {h}Z ledgers={n} txs={txs} deferred_txs={obj}")
    fig, ax = plt.subplots(figsize=(10, 4), dpi=120, facecolor=SURFACE)
    style(ax)
    xs = list(range(len(rows)))
    ax.bar(xs, [r[1][1] for r in rows], width=0.8, color=S1, label="all transactions")
    ax.plot(xs, [r[1][2] for r in rows], color=S2, linewidth=2, marker="o", markersize=4,
            label="transactions touching a deferred type")  # fmt: skip
    step = max(1, len(rows) // 12)
    ticks = [r[0][5:].replace("T", " ") + "h" for r in rows[::step]]
    ax.set_xticks(xs[::step], ticks, rotation=30, ha="right")
    ax.set_ylabel("transactions per UTC hour", color=MUTED, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    total, obj = sum(r[1][1] for r in rows), sum(r[1][2] for r in rows)
    ax.set_title(
        f"A->B window, ledgers {args.a + 1}-{args.b}: {total} transactions, {obj} touched a deferred object",
        color=INK, fontsize=11, loc="left",
    )  # fmt: skip
    ax.set_ylim(0, max(r[1][1] for r in rows) * 1.22)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK, loc="upper left", ncol=2)
    fig.tight_layout()
    fig.savefig(os.path.join(args.out, "chart-window.png"), facecolor=SURFACE)
    plt.close(fig)

    with open(args.analysis) as fh:
        an = json.load(fh)
    types = sorted(an["A"]["counts"], key=lambda t: (-an["A"]["counts"][t], t))
    ca = [an["A"]["counts"][t] for t in types]
    cb = [an["B"]["counts"][t] for t in types]
    for t, x, y in zip(types, ca, cb, strict=True):
        print(f"CHART census {t} A={x} B={y}")
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120, facecolor=SURFACE)
    style(ax)
    ys = list(range(len(types)))
    when = {k: datetime.strptime(an[k]["close_time_iso"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            for k in ("A", "B")}  # fmt: skip
    la = f"A ledger {an['A']['ledger_index']} ({when['A']:%Y-%m-%d %H:%MZ})"
    lb = f"B ledger {an['B']['ledger_index']} ({when['B']:%Y-%m-%d %H:%MZ})"
    ax.barh([y - 0.2 for y in ys], ca, height=0.36, color=S1, label=la)
    ax.barh([y + 0.2 for y in ys], cb, height=0.36, color=S2, label=lb)
    for y, x1, x2 in zip(ys, ca, cb, strict=True):
        ax.text(x1 + 0.4, y - 0.2, str(x1), va="center", fontsize=8, color=INK)
        ax.text(x2 + 0.4, y + 0.2, str(x2), va="center", fontsize=8, color=INK)
    ax.set_yticks(ys, types)
    ax.invert_yaxis()
    ax.set_xlabel("objects in the validated ledger", color=MUTED, fontsize=9)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    title = "Deferred ledger objects per type (types from postfiatd 32723c15)"
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=8, labelcolor=INK, loc="lower right")
    fig.tight_layout()
    fig.savefig(os.path.join(args.out, "chart-census.png"), facecolor=SURFACE)
    plt.close(fig)
    print("CHART wrote chart-window.png chart-census.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
