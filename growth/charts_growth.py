#!/usr/bin/env python3
"""Charts for a growth/ result (offline; needs matplotlib, run with .venv/bin/python).

  growth_hourly.png   serialized ledger bytes (header + tx_blob + metadata) per UTC hour
  growth_nudb_fit.png NuDB bytes vs retained ledgers for the in-window pft-retention samples,
                      with the analyzer's least-squares fit
  growth_sizing.png   projected NuDB bytes per online_delete (mean retained; whisker to 2N)

Usage: growth/charts_growth.py --result OUT/growth.json --data DATA --nudb samples.csv --out DIR
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_growth as ag  # noqa: E402

C1, C2 = "#2a78d6", "#eb6834"
INK, INK2, GRID_C = "#0b0b0b", "#52514e", "#e4e3df"


def mib(b):
    return b / 2**20


def charts(res, samples, out_dir):
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

    hours = res["hourly"]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    xs = range(len(hours))
    ax.bar(xs, [h["raw_bytes"] / 1024 for h in hours], width=0.8, color=C1, edgecolor="white", linewidth=2)
    ticks = [i for i in xs if i % 3 == 0]
    ax.set_xticks(ticks, [hours[i]["hour"][5:13].replace("T", " ") + "h" for i in ticks], rotation=45)
    ax.set_ylabel("KiB per hour")
    ax.yaxis.grid(True, color=GRID_C, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Serialized ledger bytes per UTC hour (first and last hour partial)", color=INK, loc="left")
    fig.tight_layout()
    files.append("growth_hourly.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)

    nd = res["nudb"]
    if nd and samples:
        fig, ax = plt.subplots(figsize=(6.4, 3.6))
        ax.scatter(
            [s["retained"] for s in samples],
            [mib(s["nudb"]) for s in samples],
            s=18,
            color=C1,
            edgecolor="white",
            linewidth=1,
            label=f"pft-retention samples (n={len(samples)})",
        )
        f = nd["fit_nudb"]
        if f:
            lo, hi = nd["retained"]["min"], nd["retained"]["max"]
            ax.plot(
                [lo, hi],
                [mib(f["fixed"] + f["per_ledger"] * lo), mib(f["fixed"] + f["per_ledger"] * hi)],
                color=C2,
                linewidth=2,
                label=f"fit: {f['per_ledger']:.0f} B per retained ledger, r2 {f['r2']:.3f}",
            )
        ax.set_xlabel("retained ledgers (complete_ledgers span)")
        ax.set_ylabel("NuDB MiB")
        ax.yaxis.grid(True, color=GRID_C, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.legend(frameon=False, loc="upper left")
        ax.set_title(
            f"NuDB size vs retained ledgers (online_delete {nd['online_delete']})", color=INK, loc="left"
        )
        fig.tight_layout()
        files.append("growth_nudb_fit.png")
        fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
        plt.close(fig)

    sz = res["sizing"]
    if sz and sz["rows"][0]["nudb_bytes"] is not None:
        rows = sz["rows"]
        fig, ax = plt.subplots(figsize=(6.4, 3.6))
        xs = range(len(rows))
        mean = [mib(r["nudb_bytes"]) for r in rows]
        top = [mib(r["nudb_bytes_max"]) for r in rows]
        ax.bar(xs, mean, width=0.55, color=C1, edgecolor="white", linewidth=2)
        ax.errorbar(
            xs,
            mean,
            yerr=[[0] * len(rows), [t - m for t, m in zip(top, mean, strict=True)]],
            fmt="none",
            ecolor=INK2,
            capsize=4,
            linewidth=1,
        )
        for x, m, t in zip(xs, mean, top, strict=True):
            ax.text(x, t, f"{m:.0f} MiB\n(max {t:.0f})", ha="center", va="bottom", color=INK, fontsize=8)
        ax.set_xticks(list(xs), [str(r["online_delete"]) for r in rows])
        ax.set_xlabel("online_delete (ledgers)")
        ax.set_ylabel("projected NuDB MiB")
        ax.set_yscale("log")
        ax.set_ylim(top=max(top) * 3)  # room for the labels above the tallest whisker
        ax.yaxis.grid(True, color=GRID_C, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_title(
            "Projected NuDB size at this window's rate (whisker: 2N retained)", color=INK, loc="left"
        )
        fig.tight_layout()
        files.append("growth_sizing.png")
        fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
        plt.close(fig)
    return files


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--result", required=True)
    p.add_argument("--data", required=True, help="collector dir (window start/end for the NuDB samples)")
    p.add_argument("--nudb", default=None)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    with open(a.result) as fh:
        res = json.load(fh)
    ct = [int(r["close_time"]) for r in ag.read_csv(os.path.join(a.data, "ledgers.csv"))]
    samples = []
    if a.nudb and ct:
        samples = ag.nudb_samples(ag.read_csv(a.nudb), min(ct) + ag.RIPPLE_EPOCH, max(ct) + ag.RIPPLE_EPOCH)
    if res["nudb"] and len(samples) != res["nudb"]["samples"]:
        print(f"FAIL {len(samples)} samples in window != {res['nudb']['samples']} in the result")
        return 1
    os.makedirs(a.out, exist_ok=True)
    for f in charts(res, samples, a.out):
        print(f"wrote {os.path.join(a.out, f)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
