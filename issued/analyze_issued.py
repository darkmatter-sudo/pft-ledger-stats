#!/usr/bin/env python3
"""Offline IOU census, gateway_balances reconciliation and A->B diff over collect_issued.py snapshots.

Reads only committed snapshot directories (no network). Every input file is checked against
MANIFEST.json (rows + sha256) first; a mismatch stops with exit 3. Output is deterministic
(sorted JSON keys, exact decimals, fixed row order), so rebuilding from the same snapshots gives
byte-identical files (`cmp` exit 0).

Sign rules (RippleState.Balance is from the LOW account's side):
  Balance > 0  low holds, high is the issuer, amount = Balance
  Balance < 0  high holds, low is the issuer, amount = -Balance
  Balance = 0  the side with a positive limit is the (would-be) holder; if both or neither side
               has a limit the line is "ambiguous" and is counted under issuer "(ambiguous)".

Census key = (issuer, currency): the same currency code from two issuers is two assets.

gateway_balances model (rippled GatewayBalances.cpp, no hotwallet): for each line of the account,
seen from the account's side, a positive balance is an asset; a negative balance on a line the
account itself froze is a frozen balance; any other negative balance is an obligation, summed
per currency.

Usage:
  ./analyze_issued.py report --a issued/data/A [--b issued/data/B] --out issued/report
Exit: 0 report written and every reconciliation matched, 1 written with mismatches, 3 bad input.
"""

import argparse
import hashlib
import json
import os
import sys
from decimal import ROUND_HALF_EVEN, Decimal

AMBIGUOUS = "(ambiguous)"
Q6 = Decimal("0.000001")
Q2 = Decimal("0.01")

# RippleState flags (rippled LedgerFormats.h)
LSF = {
    "LowReserve": 0x00010000,
    "HighReserve": 0x00020000,
    "LowAuth": 0x00040000,
    "HighAuth": 0x00080000,
    "LowNoRipple": 0x00100000,
    "HighNoRipple": 0x00200000,
    "LowFreeze": 0x00400000,
    "HighFreeze": 0x00800000,
    "AMMNode": 0x01000000,
    "LowDeepFreeze": 0x02000000,
    "HighDeepFreeze": 0x04000000,
}
# AccountRoot flags
ACCT_FLAGS = {
    "PasswordSpent": 0x00010000,
    "RequireDestTag": 0x00020000,
    "RequireAuth": 0x00040000,
    "DisallowXRP": 0x00080000,
    "DisableMaster": 0x00100000,
    "NoFreeze": 0x00200000,
    "GlobalFreeze": 0x00400000,
    "DefaultRipple": 0x00800000,
    "DepositAuth": 0x01000000,
    "AllowTrustLineClawback": 0x80000000,
}
LINE_FIELDS = (
    "Balance",
    "LowLimit",
    "HighLimit",
    "Flags",
    "LowQualityIn",
    "LowQualityOut",
    "HighQualityIn",
    "HighQualityOut",
)
SETTING_FIELDS = ("Flags", "TransferRate", "TickSize", "Domain")


class BadInput(Exception):
    pass


def num(d):
    """Exact decimal as a plain string without exponent or trailing zeros."""
    d = Decimal(d)
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def share(part, total):
    if total == 0:
        return "0"
    return str((Decimal(part) / Decimal(total)).quantize(Q6, rounding=ROUND_HALF_EVEN))


def currency_name(code):
    """3-char codes as is; 40-hex codes decoded to ASCII when printable, LP tokens (0x03) marked."""
    if len(code) != 40:
        return code
    if code.startswith("03"):
        return f"LP:{code}"
    raw = bytes.fromhex(code).rstrip(b"\x00")
    if raw and all(32 <= c < 127 for c in raw):
        return raw.decode("ascii")
    return code


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def load_snapshot(d):
    mpath = os.path.join(d, "MANIFEST.json")
    if not os.path.exists(mpath):
        raise BadInput(f"{d}: no MANIFEST.json (incomplete snapshot)")
    with open(mpath, encoding="utf-8") as fh:
        man = json.load(fh)
    for name, meta in sorted(man["files"].items()):
        p = os.path.join(d, name)
        with open(p, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != meta["sha256"] or data.count(b"\n") != meta["rows"]:
            raise BadInput(f"{p}: sha256/rows differ from MANIFEST.json")
    with open(os.path.join(d, "header.json"), encoding="utf-8") as fh:
        header = json.load(fh)
    with open(os.path.join(d, "counts.json"), encoding="utf-8") as fh:
        counts = json.load(fh)
    if header["ledger_hash"] != man["ledger_hash"]:
        raise BadInput(f"{d}: header and manifest ledger_hash differ")
    return {
        "dir": d,
        "manifest": man,
        "header": header,
        "counts": counts,
        "lines": read_jsonl(os.path.join(d, "ripplestate.jsonl")),
        "accounts": {a["Account"]: a for a in read_jsonl(os.path.join(d, "accounts.jsonl"))},
        "amm": read_jsonl(os.path.join(d, "amm.jsonl")),
        "gateway_balances": {g["account"]: g for g in read_jsonl(os.path.join(d, "gateway_balances.jsonl"))},
        "amm_info": read_jsonl(os.path.join(d, "amm_info.jsonl")),
    }


def flag(line, name):
    return bool(line.get("Flags", 0) & LSF[name])


def side_of(line, account):
    if line["LowLimit"]["issuer"] == account:
        return "Low"
    if line["HighLimit"]["issuer"] == account:
        return "High"
    raise ValueError(f"{account} is not a party of line {line['index']}")


def view(line, account):
    """The line as account_lines shows it to `account` (balance from that account's side)."""
    me = side_of(line, account)
    peer = "High" if me == "Low" else "Low"
    bal = Decimal(line["Balance"]["value"])
    return {
        "account": line[f"{peer}Limit"]["issuer"],
        "currency": line["Balance"]["currency"],
        "balance": num(bal if me == "Low" else -bal),
        "limit": num(line[f"{me}Limit"]["value"]),
        "limit_peer": num(line[f"{peer}Limit"]["value"]),
        "no_ripple": flag(line, f"{me}NoRipple"),
        "no_ripple_peer": flag(line, f"{peer}NoRipple"),
        "freeze": flag(line, f"{me}Freeze"),
        "freeze_peer": flag(line, f"{peer}Freeze"),
        "authorized": flag(line, f"{me}Auth"),
        "peer_authorized": flag(line, f"{peer}Auth"),
    }


def classify(line):
    """(issuer, holder, amount>=0, ambiguous) with the sign rules of the module docstring."""
    low, high = line["LowLimit"]["issuer"], line["HighLimit"]["issuer"]
    bal = Decimal(line["Balance"]["value"])
    if bal > 0:
        return high, low, bal, False
    if bal < 0:
        return low, high, -bal, False
    lo_lim, hi_lim = Decimal(line["LowLimit"]["value"]), Decimal(line["HighLimit"]["value"])
    if lo_lim > 0 and hi_lim == 0:
        return high, low, Decimal(0), False
    if hi_lim > 0 and lo_lim == 0:
        return low, high, Decimal(0), False
    return AMBIGUOUS, None, Decimal(0), True


def acct_flags(flags):
    return [n for n, v in ACCT_FLAGS.items() if flags & v]


def issuer_settings(root):
    s = {"flags": acct_flags(root.get("Flags", 0)), "Flags": root.get("Flags", 0)}
    if "TransferRate" in root:
        s["TransferRate"] = root["TransferRate"]
        s["transfer_fee_pct"] = str(((Decimal(root["TransferRate"]) - 10**9) / 10**7).quantize(Q6))
    if "TickSize" in root:
        s["TickSize"] = root["TickSize"]
    if "Domain" in root:
        s["Domain"] = root["Domain"]
        try:
            s["domain_text"] = bytes.fromhex(root["Domain"]).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            s["domain_text"] = None
    return s


def census(snap):
    assets = {}
    for line in snap["lines"]:
        issuer, holder, amt, amb = classify(line)
        cur = line["Balance"]["currency"]
        a = assets.setdefault(
            (issuer, cur),
            {
                "issuer": issuer,
                "currency": cur,
                "currency_name": currency_name(cur),
                "lines": 0,
                "holders": {},
                "zero_lines": 0,
                "over_limit": [],
                "issuer_froze": 0,
                "holder_froze": 0,
                "deep_frozen": 0,
                "issuer_side_no_ripple": 0,
                "holder_side_no_ripple": 0,
                "issuer_authorized": 0,
                "amm_node": 0,
            },
        )
        a["lines"] += 1
        if amt == 0:
            a["zero_lines"] += 1
        if amb:
            continue
        isd, hsd = side_of(line, issuer), side_of(line, holder)
        if amt > 0:
            a["holders"][holder] = a["holders"].get(holder, Decimal(0)) + amt
        limit = Decimal(line[f"{hsd}Limit"]["value"])
        if amt > limit:
            a["over_limit"].append({"holder": holder, "amount": num(amt), "limit": num(limit)})
        a["issuer_froze"] += flag(line, f"{isd}Freeze")
        a["holder_froze"] += flag(line, f"{hsd}Freeze")
        a["deep_frozen"] += flag(line, f"{isd}DeepFreeze") or flag(line, f"{hsd}DeepFreeze")
        a["issuer_side_no_ripple"] += flag(line, f"{isd}NoRipple")
        a["holder_side_no_ripple"] += flag(line, f"{hsd}NoRipple")
        a["issuer_authorized"] += flag(line, f"{isd}Auth")
        a["amm_node"] += flag(line, "AMMNode")
    out = []
    for key in sorted(assets, key=lambda k: (k[0], k[1])):
        a = assets[key]
        bal = sorted(a.pop("holders").items(), key=lambda kv: (-kv[1], kv[0]))
        total = sum((v for _, v in bal), Decimal(0))
        a["holders"] = len(bal)
        a["obligations"] = num(total)
        a["top1_share"] = share(bal[0][1], total) if bal else "0"
        a["top10_share"] = share(sum((v for _, v in bal[:10]), Decimal(0)), total) if bal else "0"
        hhi = sum(((v / total) ** 2 for _, v in bal), Decimal(0)) * 10000 if total else Decimal(0)
        a["hhi"] = str(hhi.quantize(Q2, rounding=ROUND_HALF_EVEN))
        a["top_holders"] = [{"holder": h, "amount": num(v)} for h, v in bal[:10]]
        a["over_limit"].sort(key=lambda r: r["holder"])
        out.append(a)
    return out


def rippling(snap):
    """Accounts with >= 2 lines of one currency whose own side has NoRipple clear (value can pass through)."""
    acc = {}
    for line in snap["lines"]:
        for s in ("Low", "High"):
            if not flag(line, f"{s}NoRipple"):
                k = (line[f"{s}Limit"]["issuer"], line["Balance"]["currency"])
                acc[k] = acc.get(k, 0) + 1
    return [{"account": a, "currency": c, "rippling_lines": n} for (a, c), n in sorted(acc.items()) if n >= 2]


def expected_gateway(snap, account):
    obligations, assets, frozen = {}, {}, {}
    for line in snap["lines"]:
        try:
            v = view(line, account)
        except ValueError:
            continue
        b = Decimal(v["balance"])
        if b > 0:
            assets.setdefault(v["account"], []).append({"currency": v["currency"], "value": num(b)})
        elif b < 0 and v["freeze"]:
            frozen.setdefault(v["account"], []).append({"currency": v["currency"], "value": num(-b)})
        elif b < 0:
            obligations[v["currency"]] = obligations.get(v["currency"], Decimal(0)) - b
    exp = {}
    if obligations:
        exp["obligations"] = {c: num(v) for c, v in sorted(obligations.items())}
    if assets:
        exp["assets"] = {
            k: sorted(v, key=lambda r: (r["currency"], Decimal(r["value"]))) for k, v in assets.items()
        }
    if frozen:
        exp["frozen_balances"] = {
            k: sorted(v, key=lambda r: (r["currency"], Decimal(r["value"]))) for k, v in frozen.items()
        }
    return exp


def norm_gateway(g):
    """gateway_balances answer in the same exact-decimal shape as expected_gateway."""
    out = {}
    if g.get("obligations"):
        out["obligations"] = {c: num(v) for c, v in sorted(g["obligations"].items())}
    for k in ("assets", "frozen_balances", "balances"):
        if g.get(k):
            out[k] = {
                a: sorted(
                    ({"currency": r["currency"], "value": num(r["value"])} for r in rows),
                    key=lambda r: (r["currency"], Decimal(r["value"])),
                )
                for a, rows in g[k].items()
            }
    if g.get("locked"):
        out["locked"] = {c: num(v) for c, v in sorted(g["locked"].items())}
    return out


def reconcile(snap, cen):
    rows, mismatches = [], []
    accounts = sorted(
        {a for ln in snap["lines"] for a in (ln["LowLimit"]["issuer"], ln["HighLimit"]["issuer"])}
    )
    for a in accounts:
        if a not in snap["gateway_balances"]:
            mismatches.append(
                {"check": "gateway_balances", "account": a, "detail": "no gateway_balances answer"}
            )
            continue
        exp, got = expected_gateway(snap, a), norm_gateway(snap["gateway_balances"][a])
        ok = exp == got
        rows.append({"account": a, "match": ok, "expected": exp, "gateway_balances": got})
        if not ok:
            mismatches.append({"check": "gateway_balances", "account": a, "expected": exp, "got": got})
    # census obligations (all non-zero holder balances) vs gateway obligations + frozen per issuer
    for c in cen:
        if c["issuer"] == AMBIGUOUS:
            continue
        g = norm_gateway(snap["gateway_balances"].get(c["issuer"], {}))
        tot = Decimal(g.get("obligations", {}).get(c["currency"], "0"))
        for rows_ in g.get("frozen_balances", {}).values():
            tot += sum((Decimal(r["value"]) for r in rows_ if r["currency"] == c["currency"]), Decimal(0))
        if tot != Decimal(c["obligations"]):
            mismatches.append(
                {
                    "check": "census_obligations",
                    "issuer": c["issuer"],
                    "currency": c["currency"],
                    "census": c["obligations"],
                    "gateway": num(tot),
                }
            )
    cnt = snap["counts"]
    for name, f in sorted(cnt["filtered"].items()):
        if not f["index_sets_equal"] or f["full_tree"] != f["type_filter"]:
            mismatches.append({"check": "object_count", "type": name, **f})
    if cnt["full_tree_by_type"].get("RippleState", 0) != len(snap["lines"]):
        mismatches.append(
            {"check": "object_count", "type": "RippleState", "detail": "snapshot rows != full tree"}
        )
    if cnt["full_tree_by_type"].get("AMM", 0) != len(snap["amm"]):
        mismatches.append({"check": "object_count", "type": "AMM", "detail": "snapshot rows != full tree"})
    return rows, mismatches


def amm_findings(snap):
    lp_lines = [ln["index"] for ln in snap["lines"] if ln["Balance"]["currency"].startswith("03")]
    found = [r for r in snap["amm_info"] if "amm" in r]
    return {
        "amm_objects_full_tree": snap["counts"]["full_tree_by_type"].get("AMM", 0),
        "amm_objects_type_filter": snap["counts"]["filtered"]["AMM"]["type_filter"],
        "lp_token_lines": len(lp_lines),
        "amm_info_queries": len(snap["amm_info"]),
        "amm_info_found": len(found),
        "amm_info_errors": sorted({r.get("error") for r in snap["amm_info"] if "error" in r}),
        "result": "pools present" if (snap["amm"] or found or lp_lines) else "none (query-backed)",
    }


def snapshot_report(snap):
    cen = census(snap)
    rec_rows, mism = reconcile(snap, cen)
    issuers = sorted({c["issuer"] for c in cen if c["issuer"] != AMBIGUOUS})
    return {
        "ledger_index": snap["header"]["ledger_index"],
        "ledger_hash": snap["header"]["ledger_hash"],
        "close_time_iso": snap["header"]["close_time_iso"],
        "trust_lines": len(snap["lines"]),
        "census": cen,
        "issuer_settings": {i: issuer_settings(snap["accounts"][i]) for i in issuers},
        "rippling_through": rippling(snap),
        "amm": amm_findings(snap),
        "object_counts": snap["counts"],
        "gateway_reconciliation": rec_rows,
        "mismatches": mism,
    }


def diff(a, b):
    la = {ln["index"]: ln for ln in a["lines"]}
    lb = {ln["index"]: ln for ln in b["lines"]}
    added = [
        {"index": i, "currency": lb[i]["Balance"]["currency"], "parties": parties(lb[i])}
        for i in sorted(lb.keys() - la.keys())
    ]
    removed = [
        {"index": i, "currency": la[i]["Balance"]["currency"], "parties": parties(la[i])}
        for i in sorted(la.keys() - lb.keys())
    ]
    changed = []
    for i in sorted(la.keys() & lb.keys()):
        ch = {f: {"a": la[i].get(f), "b": lb[i].get(f)} for f in LINE_FIELDS if la[i].get(f) != lb[i].get(f)}
        if ch:
            changed.append({"index": i, "fields": ch})
    ca = {(c["issuer"], c["currency"]): c for c in census(a)}
    cb = {(c["issuer"], c["currency"]): c for c in census(b)}
    assets = []
    for k in sorted(ca.keys() | cb.keys()):
        x, y = ca.get(k), cb.get(k)
        row = {
            "issuer": k[0],
            "currency": k[1],
            "status": "new" if x is None else "gone" if y is None else "kept",
        }
        for f in ("obligations", "holders", "lines"):
            va = Decimal(x[f]) if x else Decimal(0)
            vb = Decimal(y[f]) if y else Decimal(0)
            row[f] = {"a": num(va), "b": num(vb), "delta": num(vb - va)}
        assets.append(row)
    sa, sb = a["accounts"], b["accounts"]
    settings = []
    for acct in sorted(sa.keys() & sb.keys()):
        ch = {
            f: {"a": sa[acct].get(f), "b": sb[acct].get(f)}
            for f in SETTING_FIELDS
            if sa[acct].get(f) != sb[acct].get(f)
        }
        if ch:
            settings.append({"account": acct, "fields": ch})
    ta, tb = a["counts"]["full_tree_by_type"], b["counts"]["full_tree_by_type"]
    hours = (Decimal(b["header"]["close_time"]) - Decimal(a["header"]["close_time"])) / 3600
    return {
        "window_hours": str(hours.quantize(Q2, rounding=ROUND_HALF_EVEN)),
        "a": {
            "ledger_index": a["header"]["ledger_index"],
            "ledger_hash": a["header"]["ledger_hash"],
            "close_time_iso": a["header"]["close_time_iso"],
        },
        "b": {
            "ledger_index": b["header"]["ledger_index"],
            "ledger_hash": b["header"]["ledger_hash"],
            "close_time_iso": b["header"]["close_time_iso"],
        },
        "lines_added": added,
        "lines_removed": removed,
        "lines_changed": changed,
        "assets": assets,
        "issuer_settings_changed": settings,
        "accounts_only_a": sorted(sa.keys() - sb.keys()),
        "accounts_only_b": sorted(sb.keys() - sa.keys()),
        "object_types": {
            t: {"a": ta.get(t, 0), "b": tb.get(t, 0), "delta": tb.get(t, 0) - ta.get(t, 0)}
            for t in sorted(ta.keys() | tb.keys())
        },
    }


def parties(line):
    return [line["LowLimit"]["issuer"], line["HighLimit"]["issuer"]]


def md_snapshot(tag, r):
    out = [
        f"## Snapshot {tag}: ledger {r['ledger_index']} ({r['close_time_iso']})",
        "",
        f"Ledger hash `{r['ledger_hash']}`; trust lines {r['trust_lines']}; "
        f"full-tree objects {r['object_counts']['full_tree_objects']}.",
        "",
        "| issuer | currency | lines | holders | obligations | top1 | top10 | HHI | zero | over-limit "
        "| issuer froze | holder froze | issuer NoRipple | holder NoRipple |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in r["census"]:
        out.append(
            f"| `{c['issuer']}` | {c['currency_name']} | {c['lines']} | {c['holders']} | {c['obligations']} "
            f"| {c['top1_share']} | {c['top10_share']} | {c['hhi']} | {c['zero_lines']} "
            f"| {len(c['over_limit'])} | {c['issuer_froze']} | {c['holder_froze']} "
            f"| {c['issuer_side_no_ripple']} | {c['holder_side_no_ripple']} |"
        )
    out += ["", "Issuer settings:", ""]
    for i, s in sorted(r["issuer_settings"].items()):
        extra = "".join(f", {k}={s[k]}" for k in ("TransferRate", "TickSize", "domain_text") if k in s)
        out.append(f"- `{i}`: Flags={s['Flags']} [{', '.join(s['flags']) or 'none'}]{extra}")
    rt = r["rippling_through"]
    out += ["", f"Rippling-through accounts (>= 2 lines of one currency, own NoRipple clear): {len(rt)}"]
    out += [f"- `{x['account']}` {x['currency']}: {x['rippling_lines']} lines" for x in rt]
    m = r["amm"]
    out += [
        "",
        f"AMM: {m['result']} (AMM objects full tree {m['amm_objects_full_tree']}, type filter "
        f"{m['amm_objects_type_filter']}, LP-token lines {m['lp_token_lines']}, amm_info queries "
        f"{m['amm_info_queries']} found {m['amm_info_found']}, errors {m['amm_info_errors']}).",
    ]
    ok = sum(1 for x in r["gateway_reconciliation"] if x["match"])
    out += [
        "",
        f"gateway_balances reconciliation: {ok}/{len(r['gateway_reconciliation'])} accounts exact; "
        f"object-count checks: "
        + ", ".join(
            f"{n} full tree {f['full_tree']} = type filter {f['type_filter']} (index sets equal "
            f"{f['index_sets_equal']})"
            for n, f in sorted(r["object_counts"]["filtered"].items())
        )
        + ".",
        f"Mismatches: {len(r['mismatches'])}.",
    ]
    out += [f"- {json.dumps(x, sort_keys=True)}" for x in r["mismatches"]]
    return out


def md_diff(d):
    out = [
        f"## A -> B: ledger {d['a']['ledger_index']} -> {d['b']['ledger_index']} "
        f"({d['a']['close_time_iso']} -> {d['b']['close_time_iso']}, {d['window_hours']} h)",
        "",
        f"Lines added {len(d['lines_added'])}, removed {len(d['lines_removed'])}, changed "
        f"{len(d['lines_changed'])}; issuer-setting changes {len(d['issuer_settings_changed'])}.",
        "",
        "| issuer | currency | status | obligations A | obligations B | delta | holders A | holders B |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for x in d["assets"]:
        out.append(
            f"| `{x['issuer']}` | {x['currency']} | {x['status']} | {x['obligations']['a']} "
            f"| {x['obligations']['b']} | {x['obligations']['delta']} | {x['holders']['a']} "
            f"| {x['holders']['b']} |"
        )
    for k in ("lines_added", "lines_removed", "lines_changed", "issuer_settings_changed"):
        for x in d[k]:
            out.append(f"- {k}: {json.dumps(x, sort_keys=True)}")
    moved = {t: v for t, v in d["object_types"].items() if v["delta"]}
    out += [
        "",
        "Object-type count changes: "
        + (", ".join(f"{t} {v['a']} -> {v['b']}" for t, v in moved.items()) or "none"),
    ]
    return out


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def report(a_dir, b_dir, out):
    os.makedirs(out, exist_ok=True)
    a = load_snapshot(a_dir)
    ra = snapshot_report(a)
    doc = {"format": "pft-issued-report/1", "A": ra}
    md = [
        "# Issued assets (IOU) and trust lines: census",
        "",
        "Generated by `issued/analyze_issued.py` from committed snapshots; offline and deterministic.",
        "",
    ]
    md += md_snapshot("A", ra)
    mism = len(ra["mismatches"])
    if b_dir:
        b = load_snapshot(b_dir)
        if b["header"]["ledger_index"] <= a["header"]["ledger_index"]:
            raise BadInput(
                f"B ledger {b['header']['ledger_index']} is not after A {a['header']['ledger_index']}"
            )
        rb = snapshot_report(b)
        doc["B"] = rb
        doc["diff"] = diff(a, b)
        md += [""] + md_snapshot("B", rb) + [""] + md_diff(doc["diff"])
        mism += len(rb["mismatches"])
    write(os.path.join(out, "report.json"), json.dumps(doc, sort_keys=True, indent=1) + "\n")
    write(os.path.join(out, "REPORT.md"), "\n".join(md) + "\n")
    return mism


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--a", required=True)
    r.add_argument("--b")
    r.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        mism = report(args.a, args.b, args.out)
    except BadInput as e:
        print(f"BAD_INPUT {e}", file=sys.stderr)
        return 3
    print(f"REPORT {os.path.join(args.out, 'REPORT.md')} mismatches={mism}")
    return 1 if mism else 0


if __name__ == "__main__":
    sys.exit(main())
