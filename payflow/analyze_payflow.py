#!/usr/bin/env python3
"""Payment graph, concentration, net flows and reward-payer onward flows from a payflow/ collection.

Reads tx.csv / payments.csv / deltas.csv / ledgers.csv / gaps.csv written by collect_payflow.py, plus
the start-state snapshot (state/snapshot.py format) for the opening balances of reward recipients, and
writes one deterministic result as JSON, a Markdown report and two charts.

Counted payments: result tesSUCCESS, native PFT, metadata delivered_amount > 0 (never the tx Amount).
Failed and non-native payments are counted separately and carry no volume.

  graph          directed graph sender -> destination: accounts, distinct edges, degrees, reciprocal
                 pairs, self-payments, sources/sinks/intermediaries, weakly connected components
  concentration  per side (sent, received): Gini, top-1/top-10 share, HHI, accounts for 50 % of volume
  net            per account: received - sent (delivered drops) and fees paid in the window; top gainers
                 and losers. Sum of received - sent over all accounts is 0 by construction (checked).
  onward         what recipients of the reward payer (--payer) did with the PFT it sent them, using a
                 two-pool balance model per recipient (see onward_model). Every metadata balance change of
                 the recipient is replayed in ledger/tx order starting from its snapshot balance, so the
                 model is itself a balance check: before each change, tagged + untagged must equal the
                 metadata's previous balance, and no pool may go negative. Any break is a "model
                 mismatch" and makes the run exit 1.

Two attribution policies are reported, both FIFO over reward lots (oldest receipt spent first):
  reward_first   outflows spend reward lots before any other balance (upper bound of onward flow)
  balance_first  outflows spend the opening balance and non-reward inflows first (lower bound)
An outflow is split into components in this order: delivered amount of the recipient's own Payment (to
its destination), the recipient's own fee, then anything else (non-payment value movements).

Addresses are truncated to first 6 + last 4 characters in the report and JSON. Exit 1 on an unlisted
ledger hole, duplicate ledger, tx rows != ledger txn_count, or any onward model mismatch.
"""

import argparse
import csv
import gzip
import json
import os
import sys
from collections import defaultdict, deque

DROPS = 1_000_000
PAYER = "rwdm72S9YVKkZjeADKU2bbUMuY4vPnSfH7"  # Task Node reward payer (public service account)
POLICIES = ("reward_first", "balance_first")
SINK_LABELS = ("onward", "fee", "other", "retained")


def truncate(addr):
    return addr if len(addr) <= 12 else f"{addr[:6]}...{addr[-4:]}"


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def order_key(r):
    return (int(r["ledger_index"]), int(r["tx_index"]))


def coverage(ledgers, gaps):
    idx = sorted(int(r["ledger_index"]) for r in ledgers)
    gap_idx = sorted(int(r["ledger_index"]) for r in gaps)
    if not idx:
        return {
            "first": None,
            "last": None,
            "fetched": 0,
            "gaps": len(gap_idx),
            "gap_ledgers": gap_idx,
            "holes": [],
            "duplicates": 0,
        }
    have = set(idx) | set(gap_idx)
    holes = [i for i in range(idx[0], idx[-1] + 1) if i not in have]
    return {
        "first": idx[0],
        "last": idx[-1],
        "fetched": len(idx),
        "gaps": len(gap_idx),
        "gap_ledgers": gap_idx,
        "holes": holes,
        "duplicates": len(idx) - len(set(idx)),
    }


def counted(p):
    return p["result"] == "tesSUCCESS" and p["kind"] == "native" and int(p["delivered_drops"] or 0) > 0


def gini(values):
    s = sorted(values)
    n, total = len(s), sum(s)
    if n == 0 or total == 0:
        return 0.0
    return 2.0 * sum((i + 1) * x for i, x in enumerate(s)) / (n * total) - (n + 1) / n


def concentration(vol):
    vals = sorted(vol.values(), reverse=True)
    total = sum(vals)
    if not total:
        return {
            "accounts": len(vals),
            "total_drops": 0,
            "gini": 0.0,
            "top1_share": 0.0,
            "top10_share": 0.0,
            "hhi": 0.0,
            "accounts_for_half": 0,
        }
    c, half = 0, 0
    for i, v in enumerate(vals, 1):
        c += v
        if 2 * c >= total:
            half = i
            break
    return {
        "accounts": len(vals),
        "total_drops": total,
        "gini": round(gini(vals), 6),
        "top1_share": round(vals[0] / total, 6),
        "top10_share": round(sum(vals[:10]) / total, 6),
        "hhi": round(sum((v / total) ** 2 for v in vals), 6),
        "accounts_for_half": half,
    }


def lorenz(vol):
    vals = sorted(vol.values())
    tot = sum(vals) or 1
    xs, ys, c = [0.0], [0.0], 0
    for i, v in enumerate(vals, 1):
        c += v
        xs.append(i / len(vals))
        ys.append(c / tot)
    return xs, ys


def components(nodes, edges):
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    sizes = defaultdict(int)
    for n in nodes:
        sizes[find(n)] += 1
    return sorted(sizes.values(), reverse=True)


def median(vals):
    s = sorted(vals)
    if not s:
        return None
    m = len(s)
    return s[m // 2] if m % 2 else (s[m // 2 - 1] + s[m // 2]) / 2


def weighted_median(pairs):
    """Median of values weighted by drops: pairs of (value, weight)."""
    s = sorted(pairs)
    tot = sum(w for _, w in s)
    if not tot:
        return None
    c = 0
    for v, w in s:
        c += w
        if 2 * c >= tot:
            return v
    return s[-1][0]


def graph(pays):
    edges = defaultdict(lambda: [0, 0])  # (src, dst) -> [count, drops]
    for p in pays:
        e = edges[(p["account"], p["destination"])]
        e[0] += 1
        e[1] += int(p["delivered_drops"])
    senders = {a for a, _ in edges}
    receivers = {b for _, b in edges}
    nodes = senders | receivers
    out_deg, in_deg = defaultdict(int), defaultdict(int)
    for a, b in edges:
        if a != b:
            out_deg[a] += 1
            in_deg[b] += 1
    pairs = {(a, b) for a, b in edges if a != b}
    recip = sum(1 for a, b in pairs if (b, a) in pairs and a < b)
    comps = components(nodes, list(edges))
    n = len(nodes)
    top_edges = sorted(edges.items(), key=lambda kv: (-kv[1][1], kv[0]))[:10]
    return {
        "accounts": n,
        "senders": len(senders),
        "receivers": len(receivers),
        "edges": len(edges),
        "self_edges": sum(1 for a, b in edges if a == b),
        "density": round(len(pairs) / (n * (n - 1)), 8) if n > 1 else 0.0,
        "reciprocal_pairs": recip,
        "sources": len(senders - receivers),
        "sinks": len(receivers - senders),
        "intermediaries": len(senders & receivers),
        "out_degree_max": max(out_deg.values(), default=0),
        "in_degree_max": max(in_deg.values(), default=0),
        "out_degree_median": median([out_deg[a] for a in senders]),
        "in_degree_median": median([in_deg[b] for b in receivers]),
        "components": len(comps),
        "largest_component": comps[0] if comps else 0,
        "component_sizes_top5": comps[:5],
        "top_edges": [
            {"from": truncate(a), "to": truncate(b), "payments": c, "drops": d}
            for (a, b), (c, d) in top_edges
        ],
    }


def snapshot_balances(path, accounts):
    """Balance (drops) at the snapshot ledger for the given accounts; absent accounts are not returned."""
    want, out, header = set(accounts), {}, None
    with gzip.open(path, "rt") as fh:
        for line in fh:
            o = json.loads(line)
            if header is None:
                header = o
                continue
            if o.get("LedgerEntryType") == "AccountRoot" and o.get("Account") in want:
                out[o["Account"]] = int(o["Balance"])
    return header, out


def onward_model(acct, opening, deltas, pay_by_hash, fee_by_hash, payer, policy):
    """Replay one recipient's balance changes with a tagged (reward lots, FIFO) and an untagged pool.

    Returns totals (drops) of reward PFT received, spent per component, retained at the end, per-
    destination onward amounts, hold times (seconds, weighted by drops) and the list of mismatches.
    """
    lots = deque()  # [drops, close_time]
    untagged = opening if opening is not None else 0
    res = {
        "received": 0,
        "lots": 0,
        "onward": 0,
        "fee": 0,
        "other": 0,
        "to": defaultdict(int),
        "holds": [],
        "mismatches": [],
    }
    for d in deltas:
        prev, delta, h = int(d["prev_drops"]), int(d["delta_drops"]), d["tx_hash"]
        ct = int(d["close_time"])
        have = untagged + sum(x[0] for x in lots)
        if prev != have:
            res["mismatches"].append(
                {
                    "ledger": int(d["ledger_index"]),
                    "tx": h,
                    "cause": "prev_balance",
                    "metadata_prev": prev,
                    "model": have,
                }
            )
            untagged += prev - have  # resync so one break is reported once, not on every later row
            if untagged < 0:
                lots.clear()
                untagged = prev
        p = pay_by_hash.get(h)
        if delta > 0:
            if p is not None and p["account"] == payer and p["destination"] == acct and counted(p):
                tag = min(int(p["delivered_drops"]), delta)
                lots.append([tag, ct])
                res["received"] += tag
                res["lots"] += 1
                untagged += delta - tag
            else:
                untagged += delta
            continue
        if delta == 0:
            continue
        out = -delta
        parts = []
        if p is not None and p["account"] == acct and counted(p) and p["destination"] != acct:
            parts.append(("onward", p["destination"], min(int(p["delivered_drops"]), out)))
        fee = fee_by_hash.get(h, (None, 0))
        if fee[0] == acct:
            parts.append(("fee", None, min(fee[1], out - sum(x[2] for x in parts))))
        rest = out - sum(x[2] for x in parts)
        if rest > 0:
            parts.append(("other", None, rest))
        for kind, dest, amt in parts:
            if policy == "balance_first":
                take = min(untagged, amt)
                untagged -= take
                amt -= take
            while amt > 0 and lots:
                take = min(lots[0][0], amt)
                lots[0][0] -= take
                amt -= take
                res[kind] += take
                if kind == "onward":
                    res["to"][dest] += take
                    res["holds"].append((ct - lots[0][1], take))
                if lots[0][0] == 0:
                    lots.popleft()
            if amt > 0:
                if amt > untagged:
                    res["mismatches"].append(
                        {
                            "ledger": int(d["ledger_index"]),
                            "tx": h,
                            "cause": "negative_pool",
                            "short_drops": amt - untagged,
                        }
                    )
                    untagged = 0
                else:
                    untagged -= amt
    res["retained"] = sum(x[0] for x in lots)
    res["final_model"] = res["retained"] + untagged
    res["final_metadata"] = int(deltas[-1]["final_drops"]) if deltas else opening
    if deltas and res["final_model"] != res["final_metadata"]:
        res["mismatches"].append(
            {
                "ledger": int(deltas[-1]["ledger_index"]),
                "tx": deltas[-1]["tx_hash"],
                "cause": "final_balance",
                "metadata_final": res["final_metadata"],
                "model": res["final_model"],
            }
        )
    return res


def onward(pays, deltas, txs, ledgers, payer, opening, top_n=10):
    ct_by_ledger = {int(r["ledger_index"]): int(r["close_time"]) for r in ledgers}
    recipients = sorted(
        {p["destination"] for p in pays if p["account"] == payer and p["destination"] != payer}
    )
    pay_by_hash = {p["tx_hash"]: p for p in pays}
    fee_by_hash = {t["tx_hash"]: (t["account"], int(t["fee_drops"])) for t in txs}
    by_acct = defaultdict(list)
    want = set(recipients)
    for d in sorted(deltas, key=order_key):
        if d["account"] in want:
            by_acct[d["account"]].append(dict(d, close_time=ct_by_ledger.get(int(d["ledger_index"]), 0)))
    out = {
        "payer": truncate(payer),
        "recipients": len(recipients),
        "opening_missing": sorted(truncate(a) for a in recipients if a not in opening),
    }
    rset = set(recipients)
    for pol in POLICIES:
        agg = {"received": 0, "lots": 0, "onward": 0, "fee": 0, "other": 0, "retained": 0}
        to, holds, mism, per = defaultdict(int), [], [], []
        for a in recipients:
            r = onward_model(a, opening.get(a), by_acct[a], pay_by_hash, fee_by_hash, payer, pol)
            for k in agg:
                agg[k] += r[k]
            for dest, v in r["to"].items():
                to[dest] += v
            holds += r["holds"]
            mism += [dict(m, account=truncate(a)) for m in r["mismatches"]]
            per.append(
                {
                    "account": truncate(a),
                    "received": r["received"],
                    "onward": r["onward"],
                    "fee": r["fee"],
                    "other": r["other"],
                    "retained": r["retained"],
                    "destinations": len(r["to"]),
                }
            )
        rec = agg["received"] or 1
        dests = sorted(to.items(), key=lambda kv: (-kv[1], kv[0]))
        out[pol] = {
            **agg,
            "shares": {k: round(agg[k] / rec, 6) for k in SINK_LABELS},
            "onward_to_payer": to.get(payer, 0),
            "onward_to_other_recipients": sum(v for d, v in to.items() if d in rset),
            "onward_destinations": len(to),
            "hold_median_s": weighted_median(holds),
            "top_destinations": [
                {"account": truncate(d), "drops": v, "is_payer": d == payer, "is_recipient": d in rset}
                for d, v in dests[:top_n]
            ],
            "top_recipients": sorted(per, key=lambda x: (-x["received"], x["account"]))[:top_n],
            "conserved": agg["received"] == agg["onward"] + agg["fee"] + agg["other"] + agg["retained"],
            "mismatches": mism,
        }
    return out


def analyze(txs, pays, deltas, ledgers, gaps, payer=PAYER, opening=None, top_n=10):
    cov = coverage(ledgers, gaps)
    ok = [p for p in pays if counted(p)]
    sent, recv = defaultdict(int), defaultdict(int)
    for p in ok:
        sent[p["account"]] += int(p["delivered_drops"])
        recv[p["destination"]] += int(p["delivered_drops"])
    fees = defaultdict(int)
    for t in txs:
        fees[t["account"]] += int(t["fee_drops"])
    accts = set(sent) | set(recv)
    net = {a: recv.get(a, 0) - sent.get(a, 0) for a in accts}
    ranked = sorted(net.items(), key=lambda kv: (-kv[1], kv[0]))

    def row(a, v):
        return {
            "account": truncate(a),
            "net_drops": v,
            "received": recv.get(a, 0),
            "sent": sent.get(a, 0),
            "fees": fees.get(a, 0),
        }

    t0 = min((int(r["close_time"]) for r in ledgers), default=0)
    t1 = max((int(r["close_time"]) for r in ledgers), default=0)
    hourly = defaultdict(lambda: [0, 0])
    ct_by_ledger = {int(r["ledger_index"]): int(r["close_time"]) for r in ledgers}
    for p in ok:
        h = (ct_by_ledger.get(int(p["ledger_index"]), int(p["close_time"])) - t0) // 3600
        hourly[h][0] += 1
        hourly[h][1] += int(p["delivered_drops"])
    payer_out = [p for p in ok if p["account"] == payer]
    total = sum(sent.values())
    return {
        "coverage": cov,
        "window": {"first_close": t0, "last_close": t1, "hours": round((t1 - t0) / 3600, 3)},
        "totals": {
            "tx": len(txs),
            "ledger_txn_count": sum(int(r["txn_count"]) for r in ledgers),
            "payments": len(pays),
            "counted": len(ok),
            "failed": sum(1 for p in pays if p["result"] != "tesSUCCESS"),
            "non_native": sum(1 for p in pays if p["result"] == "tesSUCCESS" and p["kind"] != "native"),
            "zero_delivered": sum(
                1
                for p in pays
                if p["result"] == "tesSUCCESS"
                and p["kind"] == "native"
                and int(p["delivered_drops"] or 0) == 0
            ),
            "partial": sum(1 for p in ok if p["partial"] == "y"),
            "volume_drops": total,
            "fee_drops": sum(fees.values()),
            "payment_fee_drops": sum(int(p["fee_drops"]) for p in pays),
        },
        "graph": graph(ok),
        "concentration": {"sent": concentration(sent), "received": concentration(recv)},
        "net": {
            "sum_drops": sum(net.values()),
            "gainers": [row(a, v) for a, v in ranked[:top_n] if v > 0],
            "losers": [row(a, v) for a, v in reversed(ranked[-top_n:]) if v < 0],
        },
        "top_senders": [
            {"account": truncate(a), "drops": v}
            for a, v in sorted(sent.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        ],
        "top_receivers": [
            {"account": truncate(a), "drops": v}
            for a, v in sorted(recv.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
        ],
        "hourly": [{"hour": h, "payments": hourly[h][0], "drops": hourly[h][1]} for h in sorted(hourly)],
        "payer": {
            "account": truncate(payer),
            "payments": len(payer_out),
            "drops": sum(int(p["delivered_drops"]) for p in payer_out),
            "share_of_volume": round(sum(int(p["delivered_drops"]) for p in payer_out) / total, 6)
            if total
            else 0.0,
        },
        "onward": onward(ok, deltas, txs, ledgers, payer, opening or {}, top_n),
        "_lorenz": {"sent": lorenz(sent), "received": lorenz(recv)},
    }


def charts(res, out_dir):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    files = []
    fig, ax = plt.subplots(figsize=(4.8, 4.8))
    for side, color in (("sent", "#3b6ea5"), ("received", "#c0712b")):
        xs, ys = res["_lorenz"][side]
        ax.plot(xs, ys, color=color, label=f"{side} (Gini {res['concentration'][side]['gini']:.3f})")
    ax.plot([0, 1], [0, 1], color="#999", linestyle="--", label="equal")
    ax.set_xlabel("share of accounts")
    ax.set_ylabel("share of PFT volume")
    ax.set_title("Payment volume concentration (Lorenz)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    files.append("lorenz.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.2, 3.9))
    on = res["onward"]
    colors = {"onward": "#3b6ea5", "fee": "#999999", "other": "#7a5195", "retained": "#c0712b"}
    for i, pol in enumerate(POLICIES):
        left = 0.0
        for k in SINK_LABELS:
            v = on[pol][k] / DROPS
            ax.barh(i, v, left=left, color=colors[k], label=k if i == 0 else None)
            left += v
    ax.set_yticks(range(len(POLICIES)), [p.replace("_", "-") for p in POLICIES])
    ax.set_xlabel("PFT received from the reward payer")
    ax.set_title("Fate of reward-payer PFT by end of window (FIFO lots)")
    ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=len(SINK_LABELS), fontsize=8, frameon=False
    )
    fig.tight_layout()
    files.append("onward.png")
    fig.savefig(os.path.join(out_dir, files[-1]), dpi=110)
    plt.close(fig)
    return files


def pft(drops):
    return f"{drops / DROPS:,.6f}"


def pct(x):
    return f"{100 * x:.2f} %"


def render(res, chart_files):
    cov, tot, g, on = res["coverage"], res["totals"], res["graph"], res["onward"]
    L = [
        "# payflow report: payment graph and reward-payer onward flows",
        "",
        f"Ledgers {cov['first']}-{cov['last']} ({cov['fetched']} fetched, {cov['gaps']} gaps, "
        f"{len(cov['holes'])} unlisted holes), {res['window']['hours']} h of close times. "
        "Counted payments: tesSUCCESS, native PFT, metadata delivered_amount > 0. "
        "Amounts in PFT (1 PFT = 1,000,000 drops); addresses truncated.",
        "",
        "## Totals",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| transactions (tx.csv rows / ledger txn_count sum) | {tot['tx']} / {tot['ledger_txn_count']} |",
        f"| payments (all results) | {tot['payments']} |",
        f"| counted payments | {tot['counted']} |",
        f"| failed payments | {tot['failed']} |",
        f"| non-native / zero-delivered / partial | {tot['non_native']} / {tot['zero_delivered']} / "
        f"{tot['partial']} |",
        f"| PFT volume (delivered) | {pft(tot['volume_drops'])} |",
        f"| fees, all tx / payments only | {pft(tot['fee_drops'])} / {pft(tot['payment_fee_drops'])} |",
        f"| reward payer {res['payer']['account']}: payments, PFT, share of volume | "
        f"{res['payer']['payments']}, {pft(res['payer']['drops'])}, {pct(res['payer']['share_of_volume'])} |",
        "",
        "## Graph",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| accounts (senders / receivers) | {g['accounts']} ({g['senders']} / {g['receivers']}) |",
        f"| distinct edges (self-payments) | {g['edges']} ({g['self_edges']}) |",
        f"| density | {g['density']} |",
        f"| reciprocal pairs | {g['reciprocal_pairs']} |",
        f"| sources / sinks / intermediaries | {g['sources']} / {g['sinks']} / {g['intermediaries']} |",
        f"| out-degree max / median | {g['out_degree_max']} / {g['out_degree_median']} |",
        f"| in-degree max / median | {g['in_degree_max']} / {g['in_degree_median']} |",
        f"| weakly connected components (largest; top 5 sizes) | {g['components']} "
        f"({g['largest_component']}; {', '.join(map(str, g['component_sizes_top5']))}) |",
        "",
        "Top edges by volume:",
        "",
        "| from | to | payments | PFT |",
        "|---|---|---:|---:|",
    ]
    L += [f"| `{e['from']}` | `{e['to']}` | {e['payments']} | {pft(e['drops'])} |" for e in g["top_edges"]]
    L += [
        "",
        "## Concentration",
        "",
        "| side | accounts | Gini | top-1 | top-10 | HHI | accounts for 50 % |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for side, c in res["concentration"].items():
        L.append(
            f"| {side} | {c['accounts']} | {c['gini']:.4f} | {pct(c['top1_share'])} | "
            f"{pct(c['top10_share'])} | {c['hhi']:.4f} | {c['accounts_for_half']} |"
        )
    L += [
        "",
        "## Net flows (received - sent, delivered PFT)",
        "",
        f"Sum over all accounts: {res['net']['sum_drops']} drops (0 by construction).",
        "",
        "| account | net PFT | received | sent | fees |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in res["net"]["gainers"] + res["net"]["losers"]:
        L.append(
            f"| `{r['account']}` | {pft(r['net_drops'])} | {pft(r['received'])} | {pft(r['sent'])} | "
            f"{pft(r['fees'])} |"
        )
    L += [
        "",
        "## Reward-payer onward flows",
        "",
        f"{on['recipients']} accounts received PFT from {on['payer']}. "
        "Each recipient's balance is replayed from its snapshot balance through every metadata "
        "balance change; reward receipts are FIFO lots. "
        "`reward_first` spends lots before other balance (upper bound of onward flow), `balance_first` "
        "spends them last (lower bound).",
        "",
        "| policy | received | onward | fees | other | retained | to payer | to other recipients | "
        "destinations | median hold | mismatches |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for pol in POLICIES:
        o = on[pol]
        hold = "-" if o["hold_median_s"] is None else f"{o['hold_median_s'] / 60:.1f} min"
        L.append(
            f"| {pol} | {pft(o['received'])} | {pft(o['onward'])} ({pct(o['shares']['onward'])}) | "
            f"{pft(o['fee'])} | {pft(o['other'])} | "
            f"{pft(o['retained'])} ({pct(o['shares']['retained'])}) | "
            f"{pft(o['onward_to_payer'])} | {pft(o['onward_to_other_recipients'])} | "
            f"{o['onward_destinations']} | {hold} | {len(o['mismatches'])} |"
        )
    L += [
        "",
        "Top onward destinations (reward_first):",
        "",
        "| account | PFT | payer | recipient |",
        "|---|---:|---|---|",
    ]
    L += [
        f"| `{d['account']}` | {pft(d['drops'])} | {'yes' if d['is_payer'] else ''} | "
        f"{'yes' if d['is_recipient'] else ''} |"
        for d in on["reward_first"]["top_destinations"]
    ]
    if on["opening_missing"]:
        L += [
            "",
            f"Recipients absent from the start snapshot (opening balance 0, created in window): "
            f"{len(on['opening_missing'])}.",
        ]
    if chart_files:
        L += ["", "## Charts", ""] + [f"![{f}]({f})" for f in chart_files]
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/payflow/tx")
    p.add_argument(
        "--snapshot",
        required=True,
        help="start-state snapshot-<ledger>.jsonl.gz (ledger = first collected - 1)",
    )
    p.add_argument("--payer", default=PAYER)
    p.add_argument("--json", default=None)
    p.add_argument("--out-dir", default=None, help="write REPORT.md (+ charts unless --no-charts) here")
    p.add_argument("--no-charts", action="store_true")
    p.add_argument("--top", type=int, default=10)
    a = p.parse_args(argv)
    rd = {
        n: read_csv(os.path.join(a.data, f"{n}.csv")) for n in ("tx", "payments", "deltas", "ledgers", "gaps")
    }
    recipients = {r["destination"] for r in rd["payments"] if r["account"] == a.payer}
    header, opening = snapshot_balances(a.snapshot, recipients)
    res = analyze(rd["tx"], rd["payments"], rd["deltas"], rd["ledgers"], rd["gaps"], a.payer, opening, a.top)
    cov, tot = res["coverage"], res["totals"]
    snap_ok = cov["first"] is None or header.get("ledger_index") == cov["first"] - 1
    res["snapshot"] = {
        "ledger_index": header.get("ledger_index"),
        "ledger_hash": header.get("ledger_hash"),
        "adjacent": snap_ok,
    }
    chart_files = []
    if a.out_dir:
        os.makedirs(a.out_dir, exist_ok=True)
        if not a.no_charts:
            chart_files = charts(res, a.out_dir)
        with open(os.path.join(a.out_dir, "REPORT.md"), "w") as fh:
            fh.write(render(res, chart_files))
    lor = res.pop("_lorenz")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
            fh.write("\n")
    res["_lorenz"] = lor
    g, on = res["graph"], res["onward"]
    print(
        f"coverage ledgers {cov['first']}-{cov['last']} fetched={cov['fetched']} gaps={cov['gaps']} "
        f"holes={len(cov['holes'])} duplicates={cov['duplicates']}; snapshot {header.get('ledger_index')} "
        f"adjacent={snap_ok}"
    )
    print(
        f"payments={tot['payments']} counted={tot['counted']} failed={tot['failed']} "
        f"volume={tot['volume_drops']} drops fees={tot['fee_drops']} drops"
    )
    print(
        f"graph accounts={g['accounts']} edges={g['edges']} reciprocal={g['reciprocal_pairs']} "
        f"components={g['components']} largest={g['largest_component']}"
    )
    for side, c in res["concentration"].items():
        print(
            f"concentration {side}: gini={c['gini']} top1={c['top1_share']} top10={c['top10_share']} "
            f"hhi={c['hhi']} half={c['accounts_for_half']}"
        )
    print(f"net sum={res['net']['sum_drops']}")
    bad = 0
    for pol in POLICIES:
        o = on[pol]
        bad += len(o["mismatches"]) + (not o["conserved"])
        print(
            f"onward {pol}: recipients={on['recipients']} received={o['received']} onward={o['onward']} "
            f"fee={o['fee']} other={o['other']} retained={o['retained']} conserved={o['conserved']} "
            f"mismatches={len(o['mismatches'])}"
        )
        for m in o["mismatches"]:
            print(f"  MISMATCH {json.dumps(m, sort_keys=True)}")
    rc = 0
    if cov["holes"] or cov["duplicates"]:
        print(f"INCOMPLETE: {len(cov['holes'])} unlisted holes, {cov['duplicates']} duplicate ledgers")
        rc = 1
    if tot["tx"] != tot["ledger_txn_count"]:
        print(f"INCOMPLETE: tx rows {tot['tx']} != ledger txn_count sum {tot['ledger_txn_count']}")
        rc = 1
    if not snap_ok:
        print("SNAPSHOT: start snapshot is not the ledger before the first collected ledger")
        rc = 1
    if bad:
        print(f"ONWARD MODEL: {bad} mismatches")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
