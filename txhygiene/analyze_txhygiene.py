#!/usr/bin/env python3
"""Analyze a txhygiene window: how Post Fiat testnet transactions are constructed.

Reads <data>/tx.jsonl, fees.jsonl, ledgers.jsonl (collect_txhygiene.py) and writes:
  <out>/txhygiene.json   every figure in the report, machine-readable
  <out>/REPORT.md        the report (tables for every chart)
  <out>/*.png            charts (--charts; needs matplotlib, in the repo .venv)

Sections: expiry (LastLedgerSequence margin at inclusion), fees (fee / required base fee, overpay drops and
their concentration by account, fee-escalation samples), signing algorithms, Sequence vs Ticket and
per-account sequence continuity, optional fields, and field-set client fingerprints. Accounts appear only
as the collector's 16-hex HMAC tokens, and the report prints at most an 8-hex prefix.

The statistics helpers (quantile, gini, top_share, lorenz, margin_bucket, ratio_bucket) are pure functions
with known-answer tests in tests/test_txhygiene_analyze.py.
"""

import argparse
import bisect
import json
import os
import sys
from collections import Counter, defaultdict

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
from collect_txhygiene import read_jsonl  # noqa: E402

MARGIN_BUCKETS = [(0, 1, "0-1"), (2, 4, "2-4"), (5, 10, "5-10"), (11, 20, "11-20"), (21, 100, "21-100")]
RATIO_BUCKETS = [(1, 1, "1x (exact)"), (1, 2, ">1-2x"), (2, 10, ">2-10x"), (10, 100, ">10-100x")]
GENERIC = ["Flags", "LastLedgerSequence", "NetworkID", "Memos", "SourceTag", "AccountTxnID", "TicketSequence"]
TF_FULLY_CANONICAL_SIG = 0x80000000
CHART_MAX_MARGIN = 30
SERIES = "#2a78d6"  # reference palette categorical slot 1 (light); every chart is single-series
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3df"


# ---------- pure statistics ----------


def quantile(values, q):
    """Linear-interpolation quantile (numpy 'linear' / Excel PERCENTILE.INC); None for no values."""
    xs = sorted(values)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def gini(values):
    """Gini coefficient of non-negative values (0 = equal, ->1 = one holder); None if the total is 0."""
    xs = sorted(values)
    n, total = len(xs), sum(xs)
    if n == 0 or total == 0:
        return None
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2 * cum) / (n * total) - (n + 1) / n


def top_share(values, k):
    """Share of the total held by the k largest values; None if the total is 0."""
    total = sum(values)
    if total == 0:
        return None
    return sum(sorted(values, reverse=True)[:k]) / total


def lorenz(values):
    """Lorenz curve points [(population share, value share)] from (0, 0) to (1, 1), ascending values."""
    xs = sorted(values)
    total = sum(xs)
    pts, run = [(0.0, 0.0)], 0
    for i, x in enumerate(xs, 1):
        run += x
        pts.append((i / len(xs), run / total if total else 0.0))
    return pts


def margin_bucket(m):
    """LastLedgerSequence margin bucket: '<0', '0-1', '2-4', '5-10', '11-20', '21-100', '>100'."""
    if m < 0:
        return "<0"
    for lo, hi, label in MARGIN_BUCKETS:
        if lo <= m <= hi:
            return label
    return ">100"


def ratio_bucket(r):
    """fee / required bucket: '<1x', '1x (exact)', '>1-2x', '>2-10x', '>10-100x', '>100x'."""
    if r < 1:
        return "<1x"
    if r == 1:
        return "1x (exact)"
    for lo, hi, label in RATIO_BUCKETS[1:]:
        if lo < r <= hi:
            return label
    return ">100x"


def pct(n, d):
    return round(100.0 * n / d, 2) if d else None


# ---------- sections ----------


def required_fee(base, signers):
    """Reference cost of a tx: base fee, times (1 + signers) when multi-signed."""
    return base * (1 + signers)


def fee_for_ledger(samples, idx):
    """Newest fee sample taken while ledger idx was the open ledger or earlier (validated < idx)."""
    keys = [s["validated"] for s in samples]
    i = bisect.bisect_left(keys, idx) - 1
    return samples[i] if i >= 0 else None


def expiry(txs):
    with_lls = [t for t in txs if t["lls"] is not None]
    margins = [t["lls_margin"] for t in with_lls]
    by_bucket = Counter(margin_bucket(m) for m in margins)
    by_type = {}
    for tt in sorted({t["tx_type"] for t in txs}):
        sub = [t for t in txs if t["tx_type"] == tt]
        ms = [t["lls_margin"] for t in sub if t["lls"] is not None]
        by_type[tt] = {
            "tx": len(sub),
            "no_lls": len(sub) - len(ms),
            "margin_min": min(ms) if ms else None,
            "margin_median": quantile(ms, 0.5),
            "margin_max": max(ms) if ms else None,
        }
    no_lls_accounts = {t["account"] for t in txs if t["lls"] is None}
    return {
        "tx": len(txs),
        "with_lls": len(with_lls),
        "no_lls": len(txs) - len(with_lls),
        "no_lls_pct": pct(len(txs) - len(with_lls), len(txs)),
        "no_lls_accounts": len(no_lls_accounts),
        "negative_margin": sum(1 for m in margins if m < 0),
        "margin_quantiles": {q: quantile(margins, q) for q in (0.0, 0.1, 0.5, 0.9, 1.0)},
        "margin_values": {str(m): c for m, c in sorted(Counter(margins).items())},
        "buckets": {lb: by_bucket.get(lb, 0) for lb in ["<0"] + [b[2] for b in MARGIN_BUCKETS] + [">100"]},
        "by_type": by_type,
    }


def fees(txs, samples):
    base_values = Counter(s["base_fee"] for s in samples)
    rows, unmatched = [], 0
    for t in txs:
        s = fee_for_ledger(samples, t["ledger_index"])
        if s is None:
            unmatched += 1
            continue
        req = required_fee(s["base_fee"], t["signers"])
        rows.append(
            {
                "account": t["account"],
                "fee": t["fee_drops"],
                "required": req,
                "ratio": t["fee_drops"] / req,
                "overpay": max(0, t["fee_drops"] - req),
                "below_open": t["fee_drops"] < s["open_ledger_fee"] * (1 + t["signers"]),
            }
        )
    ratios = [r["ratio"] for r in rows]
    by_bucket = Counter(ratio_bucket(x) for x in ratios)
    per_acct = defaultdict(int)
    for r in rows:
        per_acct[r["account"]] += r["overpay"]
    over = sorted(per_acct.values(), reverse=True)
    escal = [s for s in samples if s["open_ledger_fee"] > s["base_fee"]]
    return {
        "fee_samples": len(samples),
        "base_fee_values": dict(sorted(base_values.items())),
        "matched_tx": len(rows),
        "unmatched_tx": unmatched,
        "fee_quantiles": {q: quantile([r["fee"] for r in rows], q) for q in (0.0, 0.5, 0.9, 0.99, 1.0)},
        "ratio_quantiles": {q: quantile(ratios, q) for q in (0.5, 0.9, 0.99, 1.0)},
        "below_required": sum(1 for x in ratios if x < 1),
        "ratio_buckets": {
            lb: by_bucket.get(lb, 0) for lb in ["<1x"] + [b[2] for b in RATIO_BUCKETS] + [">100x"]
        },
        "overpay_tx": sum(1 for r in rows if r["overpay"] > 0),
        "overpay_drops_total": sum(r["overpay"] for r in rows),
        "fee_drops_total": sum(r["fee"] for r in rows),
        "required_drops_total": sum(r["required"] for r in rows),
        "overpay_accounts": sum(1 for v in over if v > 0),
        "accounts": len(per_acct),
        "overpay_gini": gini(over),
        "overpay_top1_share": top_share(over, 1),
        "overpay_top5_share": top_share(over, 5),
        "overpay_top_accounts": [
            {"account": a[:8], "overpay_drops": v}
            for a, v in sorted(per_acct.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            if v > 0
        ],
        "paid_below_open_ledger_fee": sum(1 for r in rows if r["below_open"]),
        "escalated_samples": len(escal),
        "escalated_samples_pct": pct(len(escal), len(samples)),
        "open_ledger_fee_max": max((s["open_ledger_fee"] for s in samples), default=None),
        "queue_max": max((s["current_queue_size"] for s in samples), default=None),
        "_overpay_by_account": over,
    }


def signing(txs):
    by_tx = Counter(t["key_type"] for t in txs)
    acct_types = defaultdict(set)
    for t in txs:
        acct_types[t["account"]].add(t["key_type"])
    by_acct = Counter("+".join(sorted(v)) for v in acct_types.values())
    signers = Counter(t["signers"] for t in txs if t["key_type"] == "multisig")
    return {
        "by_tx": dict(sorted(by_tx.items())),
        "by_account": dict(sorted(by_acct.items())),
        "accounts": len(acct_types),
        "multisig_signer_counts": dict(sorted(signers.items())),
        "fully_canonical_flag_tx": sum(1 for t in txs if t["flags"] & TF_FULLY_CANONICAL_SIG),
    }


def sequencing(txs):
    """Sequence vs Ticket, and per-account Sequence continuity inside the window (in ledger order)."""
    ticket = [t for t in txs if t["ticket_seq"] is not None]
    seq = [t for t in txs if t["ticket_seq"] is None and t["sequence"] is not None]
    by_acct = defaultdict(list)
    for t in sorted(seq, key=lambda t: (t["ledger_index"], t["tx_index"])):
        by_acct[t["account"]].append(t)
    steps = Counter()
    gap_examples = []
    for a, ts in by_acct.items():
        for prev, cur in zip(ts, ts[1:], strict=False):
            d = cur["sequence"] - prev["sequence"]
            label = "+1" if d == 1 else ("<=0" if d <= 0 else ">+1")
            steps[label] += 1
            if label != "+1" and len(gap_examples) < 10:
                gap_examples.append(
                    {
                        "account": a[:8],
                        "from_ledger": prev["ledger_index"],
                        "to_ledger": cur["ledger_index"],
                        "step": d,
                        "prev_type": prev["tx_type"],
                    }
                )
    same_ledger = Counter()
    for ts in by_acct.values():
        per = Counter(t["ledger_index"] for t in ts)
        for n in per.values():
            same_ledger[min(n, 5)] += 1
    seq_vs_ledger = [t["ledger_index"] - t["sequence"] for t in seq]
    return {
        "ticket_tx": len(ticket),
        "sequence_tx": len(seq),
        "accounts_with_2plus_tx": sum(1 for ts in by_acct.values() if len(ts) > 1),
        "steps": {k: steps.get(k, 0) for k in ("+1", ">+1", "<=0")},
        "gap_examples": gap_examples,
        "tx_per_account_ledger": {
            str(k) + ("+" if k == 5 else ""): v for k, v in sorted(same_ledger.items())
        },
        "ledger_minus_sequence_quantiles": {q: quantile(seq_vs_ledger, q) for q in (0.0, 0.5, 1.0)},
    }


def optional_fields(txs):
    n = len(txs)
    net = Counter(t["network_id"] for t in txs)
    out = {
        "tx": n,
        "network_id_values": {str(k): v for k, v in sorted(net.items(), key=lambda kv: str(kv[0]))},
        "no_network_id": net.get(None, 0),
        "source_tag": sum(1 for t in txs if t["source_tag"] == "y"),
        "dest_tag": sum(1 for t in txs if t["dest_tag"] == "y"),
        "account_txn_id": sum(1 for t in txs if t["account_txn_id"] == "y"),
        "memos": sum(1 for t in txs if t["memos"] > 0),
        "flags_nonzero": sum(1 for t in txs if t["flags"] != 0),
        "flags_field_absent": sum(1 for t in txs if "Flags" not in t["fieldset"].split("+")),
    }
    out["pct"] = {k: pct(v, n) for k, v in out.items() if isinstance(v, int) and k != "tx"}
    return out


def client_class(t):
    """Generic construction signature: which optional fields are present, plus the key type."""
    fs = set(t["fieldset"].split("+"))
    present = [f for f in GENERIC if f in fs]
    return f"{t['key_type']}: " + ("+".join(present) if present else "-")


def fingerprints(txs):
    cls_tx = Counter(client_class(t) for t in txs)
    cls_acct = defaultdict(set)
    for t in txs:
        cls_acct[client_class(t)].add(t["account"])
    acct_classes = defaultdict(set)
    for t in txs:
        acct_classes[t["account"]].add(client_class(t))
    full = Counter((t["tx_type"], t["fieldset"]) for t in txs)
    full_acct = defaultdict(set)
    for t in txs:
        full_acct[(t["tx_type"], t["fieldset"])].add(t["account"])
    return {
        "classes": [
            {"class": c, "tx": n, "accounts": len(cls_acct[c])}
            for c, n in sorted(cls_tx.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "accounts_with_several_classes": sum(1 for v in acct_classes.values() if len(v) > 1),
        "distinct_fieldsets": len(full),
        "fieldsets": [
            {"tx_type": tt, "fieldset": fs, "tx": n, "accounts": len(full_acct[(tt, fs)])}
            for (tt, fs), n in sorted(full.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }


def concentration(txs):
    per = Counter(t["account"] for t in txs)
    v = list(per.values())
    return {
        "accounts": len(per),
        "tx_gini": gini(v),
        "top1_share": top_share(v, 1),
        "top5_share": top_share(v, 5),
    }


def analyze(data):
    txs = read_jsonl(os.path.join(data, "tx.jsonl"))
    samples = sorted(read_jsonl(os.path.join(data, "fees.jsonl")), key=lambda s: s["validated"])
    ledgers = read_jsonl(os.path.join(data, "ledgers.jsonl"))
    with open(os.path.join(data, "state.json")) as fh:
        state = json.load(fh)
    window = {
        "start_utc": state["start_utc"],
        "end_utc": state["end_utc"],
        "first_ledger": ledgers[0]["ledger_index"] if ledgers else None,
        "last_ledger": ledgers[-1]["ledger_index"] if ledgers else None,
        "ledgers": len(ledgers),
        "first_close": ledgers[0]["close_time_iso"] if ledgers else None,
        "last_close": ledgers[-1]["close_time_iso"] if ledgers else None,
        "tx": len(txs),
        "by_type": dict(sorted(Counter(t["tx_type"] for t in txs).items())),
        "by_result": dict(sorted(Counter(t["result"] for t in txs).items())),
    }
    return {
        "window": window,
        "concentration": concentration(txs),
        "expiry": expiry(txs),
        "fees": fees(txs, samples),
        "signing": signing(txs),
        "sequencing": sequencing(txs),
        "optional_fields": optional_fields(txs),
        "fingerprints": fingerprints(txs),
    }


# ---------- output ----------


def fmt(x, nd=3):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))
    return str(x)


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def report(res, charts):
    w, ex, fe, sg, sq, of, fp, co = (
        res[k]
        for k in (
            "window",
            "expiry",
            "fees",
            "signing",
            "sequencing",
            "optional_fields",
            "fingerprints",
            "concentration",
        )
    )
    n = w["tx"]
    L = [
        "# Transaction-construction hygiene, Post Fiat testnet",
        "",
        f"Window {w['start_utc']} -> {w['end_utc']} (collector clock); ledgers {w['first_ledger']}-"
        f"{w['last_ledger']} ({w['ledgers']} ledgers, closes {w['first_close']} .. {w['last_close']}); "
        f"{n} validated transactions from {co['accounts']} accounts. Generated by "
        "`txhygiene/analyze_txhygiene.py` from `txhygiene/data/tx/`; every number below is in "
        "`txhygiene/report/txhygiene.json`.",
        "",
        "Types: "
        + ", ".join(f"{k} {v}" for k, v in w["by_type"].items())
        + ". Results: "
        + ", ".join(f"{k} {v}" for k, v in w["by_result"].items())
        + ". Account activity: top-1 account "
        f"{fmt(100 * (co['top1_share'] or 0), 1)} %, top-5 {fmt(100 * (co['top5_share'] or 0), 1)} % of all "
        f"tx (Gini {fmt(co['tx_gini'])}).",
        "",
        "## 1. Expiry: LastLedgerSequence margin at inclusion",
        "",
        "Margin = `LastLedgerSequence - ledger_index` of the ledger that included the tx: how many more "
        "ledgers the tx could have waited. A tx without LastLedgerSequence never expires; if it is "
        "delayed (queue, low fee, sequence hole) it can apply much later than its sender expects.",
        "",
        f"- with LastLedgerSequence: {ex['with_lls']} / {n}; without: {ex['no_lls']} "
        f"({fmt(ex['no_lls_pct'])} %) from {ex['no_lls_accounts']} account(s); negative margins: "
        f"{ex['negative_margin']}",
        "- margin quantiles (min / p10 / median / p90 / max): "
        + " / ".join(fmt(ex["margin_quantiles"][q]) for q in (0.0, 0.1, 0.5, 0.9, 1.0)),
        "",
        table(["margin (ledgers)", "tx"], list(ex["buckets"].items())),
        "",
        "Exact margins: " + ", ".join(f"{k}: {v}" for k, v in ex["margin_values"].items()) + ".",
        "",
        table(
            ["type", "tx", "no LLS", "min", "median", "max"],
            [
                [k, v["tx"], v["no_lls"], v["margin_min"], v["margin_median"], v["margin_max"]]
                for k, v in ex["by_type"].items()
            ],
        ),
        "",
    ]
    if charts:
        L += ["![LastLedgerSequence margin](margin.png)", ""]
    L += [
        "## 2. Fees: paid vs required, overpay concentration, escalation",
        "",
        "Required = base fee of the newest `fee` sample taken before the tx's ledger closed, times "
        "(1 + signers) for multi-signed tx. Overpay = fee - required (drops). The open-ledger fee is "
        f"sampled once per validated ledger ({fe['fee_samples']} samples).",
        "",
        f"- base fee values seen: {fe['base_fee_values']}; tx matched to a sample {fe['matched_tx']}, "
        f"unmatched {fe['unmatched_tx']}",
        "- fee quantiles in drops (min / median / p90 / p99 / max): "
        + " / ".join(fmt(fe["fee_quantiles"][q]) for q in (0.0, 0.5, 0.9, 0.99, 1.0)),
        "- fee / required (median / p90 / p99 / max): "
        + " / ".join(fmt(fe["ratio_quantiles"][q]) for q in (0.5, 0.9, 0.99, 1.0)),
        f"- below required: {fe['below_required']}; paid below the sampled open-ledger fee: "
        f"{fe['paid_below_open_ledger_fee']}",
        f"- overpaying tx {fe['overpay_tx']}; overpay {fe['overpay_drops_total']} of "
        f"{fe['fee_drops_total']} fee drops (required {fe['required_drops_total']})",
        f"- overpay by account: {fe['overpay_accounts']} of {fe['accounts']} accounts overpay; Gini "
        f"{fmt(fe['overpay_gini'])}, top-1 {fmt(100 * (fe['overpay_top1_share'] or 0), 1)} %, top-5 "
        f"{fmt(100 * (fe['overpay_top5_share'] or 0), 1)} %",
        f"- escalation: open-ledger fee above base in {fe['escalated_samples']} of {fe['fee_samples']} "
        f"samples ({fmt(fe['escalated_samples_pct'])} %); max open-ledger fee {fe['open_ledger_fee_max']} "
        f"drops; max queue {fe['queue_max']}",
        "",
        table(["fee / required", "tx"], list(fe["ratio_buckets"].items())),
        "",
        table(
            ["account (HMAC prefix)", "overpay drops"],
            [[r["account"], r["overpay_drops"]] for r in fe["overpay_top_accounts"]],
        ),
        "",
    ]
    if charts:
        L += ["![Overpay Lorenz curve](overpay_lorenz.png)", ""]
    L += [
        "## 3. Signing algorithms",
        "",
        "From the SigningPubKey prefix only (`ED` = ed25519, `02`/`03` = secp256k1, empty with Signers = "
        "multisig). No public key is stored.",
        "",
        table(["key type", "tx"], list(sg["by_tx"].items())),
        "",
        table(["key types used by an account", "accounts"], list(sg["by_account"].items())),
        "",
        f"Multisig signer counts: {sg['multisig_signer_counts'] or 'none'}. tfFullyCanonicalSig flag set on "
        f"{sg['fully_canonical_flag_tx']} tx (the flag is ignored once RequireFullyCanonicalSig is enabled).",
        "",
        "## 4. Sequences and tickets",
        "",
        f"- Sequence-based tx {sq['sequence_tx']}, Ticket-based tx {sq['ticket_tx']}",
        f"- accounts with 2+ Sequence tx in the window: {sq['accounts_with_2plus_tx']}; steps between "
        f"consecutive tx of one account: +1 {sq['steps']['+1']}, >+1 {sq['steps']['>+1']}, <=0 "
        f"{sq['steps']['<=0']} (a validated tx always consumes exactly its Sequence, so >+1 means the "
        "account used sequences outside these tx types, e.g. TicketCreate, or the window misses a tx)",
        "- tx per (account, ledger): "
        + ", ".join(f"{k}: {v}" for k, v in sq["tx_per_account_ledger"].items()),
        "- ledger_index - Sequence (min / median / max): "
        + " / ".join(fmt(sq["ledger_minus_sequence_quantiles"][q]) for q in (0.0, 0.5, 1.0))
        + " (an account's first Sequence is the ledger index it was created in)",
        "",
    ]
    if sq["gap_examples"]:
        L += [
            table(
                ["account", "from ledger", "to ledger", "step", "previous type"],
                [
                    [g["account"], g["from_ledger"], g["to_ledger"], g["step"], g["prev_type"]]
                    for g in sq["gap_examples"]
                ],
            ),
            "",
        ]
    L += [
        "## 5. Optional fields",
        "",
        table(
            ["field", "tx", "%"],
            [
                [k, of[k], of["pct"][k]]
                for k in (
                    "no_network_id",
                    "source_tag",
                    "dest_tag",
                    "account_txn_id",
                    "memos",
                    "flags_nonzero",
                    "flags_field_absent",
                )
            ],
        ),
        "",
        f"NetworkID values: {of['network_id_values']}.",
        "",
        "## 6. Field-set client fingerprints",
        "",
        "Class = key type + which generic optional fields the client put in the tx (Flags, "
        "LastLedgerSequence, NetworkID, Memos, SourceTag, AccountTxnID, TicketSequence). Server-added keys "
        "(hash, metaData, DeliverMax, ...) are excluded.",
        "",
        table(["class", "tx", "accounts"], [[c["class"], c["tx"], c["accounts"]] for c in fp["classes"]]),
        "",
        f"Accounts seen with more than one class: {fp['accounts_with_several_classes']}. Distinct (type, "
        f"full field set) combinations: {fp['distinct_fieldsets']}:",
        "",
        table(
            ["type", "field set", "tx", "accounts"],
            [
                [f["tx_type"], f["fieldset"].replace("+", " "), f["tx"], f["accounts"]]
                for f in fp["fieldsets"]
            ],
        ),
        "",
    ]
    if charts:
        L += ["![Client classes](classes.png)", ""]
    return "\n".join(L)


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
    ex = res["expiry"]
    mv = {int(k): v for k, v in ex["margin_values"].items()}
    lo = min([0, *mv])
    labels = [str(m) for m in range(lo, CHART_MAX_MARGIN + 1)] + [f">{CHART_MAX_MARGIN}", "no LLS"]
    vals = [mv.get(m, 0) for m in range(lo, CHART_MAX_MARGIN + 1)]
    vals += [sum(v for m, v in mv.items() if m > CHART_MAX_MARGIN), ex["no_lls"]]
    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=120)
    bars = ax.bar(labels, vals, color=SERIES, width=0.7, edgecolor="white", linewidth=1)
    ax.bar_label(bars, labels=[str(v) if v else "" for v in vals], color=INK, fontsize=7, padding=2)
    ax.tick_params(axis="x", labelsize=7, rotation=90)
    style(ax, "LastLedgerSequence margin at inclusion (exact ledgers)", "margin", "transactions")
    fig.tight_layout()
    fig.savefig(os.path.join(out, "margin.png"))
    plt.close(fig)
    files.append("margin.png")

    over = res["fees"]["_overpay_by_account"]
    fig, ax = plt.subplots(figsize=(4.8, 4.4), dpi=120)
    ax.plot([0, 1], [0, 1], color=MUTED, linewidth=1, linestyle="--", label="equal overpay")
    if sum(over):
        pts = lorenz(over)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES, linewidth=2, label="accounts")
    style(
        ax,
        f"Overpay Lorenz curve (Gini {fmt(res['fees']['overpay_gini'])})",
        "share of accounts",
        "share of overpaid drops",
    )
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "overpay_lorenz.png"))
    plt.close(fig)
    files.append("overpay_lorenz.png")

    cls = res["fingerprints"]["classes"][:8][::-1]
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(cls) + 1.2), dpi=120)
    bars = ax.barh(
        [c["class"] for c in cls],
        [c["tx"] for c in cls],
        color=SERIES,
        height=0.6,
        edgecolor="white",
        linewidth=2,
    )
    ax.bar_label(
        bars, labels=[f"{c['tx']} tx / {c['accounts']} acct" for c in cls], color=INK, fontsize=8, padding=3
    )
    style(ax, "Client classes (key type: optional fields present)", "transactions", "")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.tick_params(axis="y", labelsize=7)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "classes.png"))
    plt.close(fig)
    files.append("classes.png")
    return files


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/txhygiene/tx")
    ap.add_argument("--out", default="txhygiene/report")
    ap.add_argument("--charts", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    res = analyze(a.data)
    files = charts(res, a.out) if a.charts else []
    pub = {
        k: ({kk: vv for kk, vv in v.items() if not kk.startswith("_")} if isinstance(v, dict) else v)
        for k, v in res.items()
    }
    with open(os.path.join(a.out, "txhygiene.json"), "w") as fh:
        json.dump(pub, fh, indent=1, sort_keys=True, default=str)
    with open(os.path.join(a.out, "REPORT.md"), "w") as fh:
        fh.write(report(res, bool(files)) + "\n")
    w = res["window"]
    print(
        f"analyzed {w['tx']} tx in {w['ledgers']} ledgers ({w['first_ledger']}-{w['last_ledger']}) -> "
        f"{a.out}/REPORT.md, txhygiene.json{', ' + ', '.join(files) if files else ''}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
