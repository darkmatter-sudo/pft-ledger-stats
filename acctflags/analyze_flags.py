#!/usr/bin/env python3
"""Account-settings census, A-to-B reconciliation and payment-failure correlation (stdlib + matplotlib).

Reads only the pseudonymized files written by extract_flags.py (acctflags/data/), so the report can be
rebuilt from a public clone with no node and no network:

  census     per-flag account counts and shares at A and B, distinct flag combinations, census
             settings (RegularKey, Domain, ... presence; TransferRate, TickSize set), unknown bits
  reconcile  replays every AccountRoot node of every transaction in A+1..B onto snapshot A, in
             ledger/TransactionIndex order, and compares the result with snapshot B account by
             account. Per flag:  expected_B = A + set_by_tx - cleared_by_tx + created_with - deleted_with
             and the same for every census setting, the account total (A + created - deleted) and the
             DepositPreauth pairs (A + created - deleted). Every disagreement is listed as a mismatch:
             an account whose replayed state differs from B, a ModifiedNode whose PreviousFields Flags
             differs from the replayed flags, a CreatedNode for an existing account, or a Modified /
             DeletedNode for an unknown one. The reconciliation passes only with zero mismatches.
  failures   every Payment with a failing result code, by the destination's replayed state just before
             the payment (missing, RequireDestTag, DepositAuth, DisallowXRP, preauthorized sender); a
             retry is the first later tesSUCCESS Payment from the same sender to the same destination
             at most --retry-window validated ledgers later (default from --config, committed as 100).
             A retry is classed dest_tag_fix (failed without DestinationTag, retry has one),
             preauth_fix (destination preauthorized the sender in between), flag_cleared (destination
             cleared the blocking flag in between) or other. Failures whose window reaches past B are
             counted as censored.

Writes REPORT.md, report.json and two charts (deterministic PNG). `--offline` blocks every socket for
the run, so the rebuild provably used only committed files. Exit 1 if the reconciliation has mismatches.

Usage:
  ./analyze_flags.py --data acctflags/data --config acctflags/report.config.json --out acctflags
      [--retry-window N] [--offline] [--no-charts]
"""

import argparse
import json
import os
import socket
import sys
from collections import Counter

FLAGS = {
    0x00010000: "PasswordSpent",
    0x00020000: "RequireDestTag",
    0x00040000: "RequireAuth",
    0x00080000: "DisallowXRP",
    0x00100000: "DisableMaster",
    0x00200000: "NoFreeze",
    0x00400000: "GlobalFreeze",
    0x00800000: "DefaultRipple",
    0x01000000: "DepositAuth",
    0x02000000: "AMM",
    0x04000000: "DisallowIncomingNFTokenOffer",
    0x08000000: "DisallowIncomingCheck",
    0x10000000: "DisallowIncomingPayChan",
    0x20000000: "DisallowIncomingTrustline",
    0x40000000: "AllowTrustLineLocking",
    0x80000000: "AllowTrustLineClawback",
}
INBOUND = (
    "RequireDestTag",
    "DepositAuth",
    "DisallowXRP",
    "RequireAuth",
    "DisallowIncomingNFTokenOffer",
    "DisallowIncomingCheck",
    "DisallowIncomingPayChan",
    "DisallowIncomingTrustline",
)
SETTINGS = (
    "RegularKey",
    "Domain",
    "EmailHash",
    "MessageKey",
    "NFTokenMinter",
    "WalletLocator",
    "TransferRate",
    "TickSize",
)
FAIL_CODES = (
    "tecDST_TAG_NEEDED",
    "tecNO_PERMISSION",
    "tecNO_DST",
    "tecNO_DST_INSUF_XRP",
    "tecNO_TARGET",
    "tecUNFUNDED_PAYMENT",
    "tecPATH_DRY",
    "tecPATH_PARTIAL",
    "tecNO_AUTH",
    "tecNO_LINE",
)
BLOCKING = {"tecDST_TAG_NEEDED": "RequireDestTag", "tecNO_PERMISSION": "DepositAuth"}


def flag_names(flags):
    names = [n for bit, n in sorted(FLAGS.items()) if flags & bit]
    unknown = flags & ~sum(FLAGS)
    if unknown:
        names.append(f"unknown_0x{unknown:08x}")
    return names


def setting_on(settings, name):
    v = settings.get(name)
    return v is not None and v is not False


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load(data):
    with open(os.path.join(data, "snapshots.json"), encoding="utf-8") as fh:
        snaps = json.load(fh)
    return {
        "snaps": snaps,
        "A": read_jsonl(os.path.join(data, "A.jsonl")),
        "B": read_jsonl(os.path.join(data, "B.jsonl")),
        "ledgers": read_jsonl(os.path.join(data, "ledgers.jsonl")),
        "txs": read_jsonl(os.path.join(data, "txs.jsonl")),
    }


def check_window(d):
    """The ledger rows must be exactly A+1..B, chained, with tx counts matching txs.jsonl."""
    a, b = d["snaps"]["A"]["ledger_index"], d["snaps"]["B"]["ledger_index"]
    problems = []
    idx = [r["ledger_index"] for r in d["ledgers"]]
    if idx != list(range(a + 1, b + 1)):
        problems.append(f"ledger rows are not exactly {a + 1}..{b}")
    prev = d["snaps"]["A"]["ledger_hash"]
    for r in d["ledgers"]:
        if r["parent_hash"] != prev:
            problems.append(f"ledger {r['ledger_index']} does not chain to its parent")
        prev = r["ledger_hash"]
    if prev != d["snaps"]["B"]["ledger_hash"]:
        problems.append("last ledger is not snapshot B")
    per = Counter(t["ledger_index"] for t in d["txs"])
    for r in d["ledgers"]:
        if per.get(r["ledger_index"], 0) != r["txn_count"]:
            problems.append(
                f"ledger {r['ledger_index']} txn_count {r['txn_count']} != {per.get(r['ledger_index'], 0)}"
            )
    return problems


def census(rows):
    n = len(rows)
    per_flag = Counter()
    combos = Counter()
    settings = Counter()
    for r in rows:
        names = flag_names(r["flags"])
        per_flag.update(names)
        combos["+".join(names) if names else "(none)"] += 1
        settings.update(s for s in SETTINGS if setting_on(r["settings"], s))
    inbound_any = sum(1 for r in rows if set(flag_names(r["flags"])) & set(INBOUND))
    return {
        "accounts": n,
        "per_flag": dict(sorted(per_flag.items())),
        "combinations": dict(sorted(combos.items(), key=lambda kv: (-kv[1], kv[0]))),
        "settings": dict(sorted(settings.items())),
        "inbound_any": inbound_any,
    }


def ordered(txs):
    return sorted(txs, key=lambda t: (t["ledger_index"], t["tx_index"]))


def reconcile(d):
    state = {r["id"]: {"flags": r["flags"], "settings": r["settings"]} for r in d["A"]}
    preauth = {tuple(p) for p in d["snaps"]["A"]["preauth"]}
    terms = {n: Counter() for n in list(FLAGS.values()) + list(SETTINGS)}
    created = deleted = 0
    pa_created = pa_deleted = 0
    mismatches = []

    def bump(old, new):
        for bit, name in FLAGS.items():
            was, now = bool(old["flags"] & bit), bool(new["flags"] & bit)
            if was != now:
                terms[name]["set_by_tx" if now else "cleared_by_tx"] += 1
        for s in SETTINGS:
            was, now = setting_on(old["settings"], s), setting_on(new["settings"], s)
            if was != now:
                terms[s]["set_by_tx" if now else "cleared_by_tx"] += 1

    empty = {"flags": 0, "settings": {}}
    for t in ordered(d["txs"]):
        where = f"ledger {t['ledger_index']} tx {t['tx_index']}"
        for node in t["nodes"]:
            kind = node["kind"]
            if node["node"] == "DepositPreauth":
                if node["authorized"] is None:
                    continue
                pair = (node["owner"], node["authorized"])
                if kind == "CreatedNode":
                    if pair in preauth:
                        mismatches.append(f"{where}: DepositPreauth {pair[0]}->{pair[1]} created twice")
                    preauth.add(pair)
                    pa_created += 1
                elif kind == "DeletedNode":
                    if pair not in preauth:
                        mismatches.append(f"{where}: DepositPreauth {pair[0]}->{pair[1]} deleted but unknown")
                    preauth.discard(pair)
                    pa_deleted += 1
                continue
            pid = node["id"]
            if kind == "CreatedNode":
                if pid in state:
                    mismatches.append(f"{where}: CreatedNode for existing account {pid}")
                state[pid] = node["after"]
                created += 1
                for name in flag_names(node["after"]["flags"]):
                    terms.setdefault(name, Counter())["created_with"] += 1
                for s in SETTINGS:
                    if setting_on(node["after"]["settings"], s):
                        terms[s]["created_with"] += 1
            elif kind == "ModifiedNode":
                if pid not in state:
                    mismatches.append(f"{where}: ModifiedNode for unknown account {pid}")
                    state[pid] = empty
                if state[pid]["flags"] != node["before_flags"]:
                    mismatches.append(
                        f"{where}: account {pid} PreviousFields Flags 0x{node['before_flags']:08x} != "
                        f"replayed 0x{state[pid]['flags']:08x}"
                    )
                bump(state[pid], node["after"])
                state[pid] = node["after"]
            elif kind == "DeletedNode":
                if pid not in state:
                    mismatches.append(f"{where}: DeletedNode for unknown account {pid}")
                    continue
                gone = state.pop(pid)
                deleted += 1
                for name in flag_names(gone["flags"]):
                    terms.setdefault(name, Counter())["deleted_with"] += 1
                for s in SETTINGS:
                    if setting_on(gone["settings"], s):
                        terms[s]["deleted_with"] += 1

    b_rows = {r["id"]: {"flags": r["flags"], "settings": r["settings"]} for r in d["B"]}
    for pid in sorted(set(state) | set(b_rows)):
        if pid not in b_rows:
            mismatches.append(f"account {pid}: replay has it, snapshot B does not")
        elif pid not in state:
            mismatches.append(f"account {pid}: snapshot B has it, replay does not")
        elif state[pid] != b_rows[pid]:
            mismatches.append(f"account {pid}: replayed state differs from snapshot B")
    ca, cb = census(d["A"]), census(d["B"])

    def count(c, name):
        return c["per_flag"].get(name, 0)

    table = []
    names = sorted(
        set(terms) | set(ca["per_flag"]) | set(cb["per_flag"]), key=lambda n: (n not in FLAGS.values(), n)
    )
    for name in names:
        if name in SETTINGS:
            continue
        tm = terms.get(name, Counter())
        a_n, b_n = count(ca, name), count(cb, name)
        exp = a_n + tm["set_by_tx"] - tm["cleared_by_tx"] + tm["created_with"] - tm["deleted_with"]
        table.append(row(name, "flag", a_n, tm, exp, b_n))
    for s in SETTINGS:
        tm = terms[s]
        a_n, b_n = ca["settings"].get(s, 0), cb["settings"].get(s, 0)
        exp = a_n + tm["set_by_tx"] - tm["cleared_by_tx"] + tm["created_with"] - tm["deleted_with"]
        table.append(row(s, "setting", a_n, tm, exp, b_n))
    acct = {"A": len(d["A"]), "created": created, "deleted": deleted, "B": len(d["B"])}
    acct["expected_B"] = acct["A"] + created - deleted
    pa = {
        "A": len(d["snaps"]["A"]["preauth"]),
        "created": pa_created,
        "deleted": pa_deleted,
        "B": len(d["snaps"]["B"]["preauth"]),
    }
    pa["expected_B"] = pa["A"] + pa_created - pa_deleted
    if {tuple(p) for p in d["snaps"]["B"]["preauth"]} != preauth:
        mismatches.append("DepositPreauth pairs: replayed set differs from snapshot B")
    for r in table:
        if r["expected_B"] != r["B"]:
            mismatches.append(f"{r['kind']} {r['name']}: expected_B {r['expected_B']} != B {r['B']}")
    if acct["expected_B"] != acct["B"]:
        mismatches.append(f"accounts: expected_B {acct['expected_B']} != B {acct['B']}")
    if pa["expected_B"] != pa["B"]:
        mismatches.append(f"DepositPreauth: expected_B {pa['expected_B']} != B {pa['B']}")
    return {"table": table, "accounts": acct, "preauth": pa, "mismatches": mismatches}


def row(name, kind, a_n, tm, exp, b_n):
    return {
        "name": name,
        "kind": kind,
        "A": a_n,
        "set_by_tx": tm["set_by_tx"],
        "cleared_by_tx": tm["cleared_by_tx"],
        "created_with": tm["created_with"],
        "deleted_with": tm["deleted_with"],
        "expected_B": exp,
        "B": b_n,
    }


def dest_state(state, preauth, dest, sender):
    if dest not in state:
        return "missing"
    names = set(flag_names(state[dest]["flags"]))
    parts = [n for n in ("RequireDestTag", "DepositAuth", "DisallowXRP") if n in names]
    if "DepositAuth" in names and (dest, sender) in preauth:
        parts.append("sender_preauthorized")
    return "+".join(parts) if parts else "no_inbound_flag"


def failures(d, window):
    """Failed payments by destination state, and their retries within `window` validated ledgers."""
    if window < 1:
        raise ValueError("retry window must be >= 1 ledger")
    b = d["snaps"]["B"]["ledger_index"]
    state = {r["id"]: {"flags": r["flags"], "settings": r["settings"]} for r in d["A"]}
    preauth = {tuple(p) for p in d["snaps"]["A"]["preauth"]}
    events = []  # (ledger, tx, kind, payload) in replay order
    fails = []
    pays = []
    results = Counter()
    for t in ordered(d["txs"]):
        if t["type"] == "Payment":
            results[t["result"]] += 1
            info = {
                "ledger": t["ledger_index"],
                "tx": t["tx_index"],
                "sender": t["sender"],
                "dest": t.get("destination"),
                "dest_tag": t.get("dest_tag", False),
                "result": t["result"],
                "dest_state": dest_state(state, preauth, t.get("destination"), t["sender"]),
            }
            pays.append(info)
            if t["result"] != "tesSUCCESS":
                fails.append(info)
        for node in t["nodes"]:
            if node["node"] == "DepositPreauth":
                if node["authorized"] is None:
                    continue
                pair = (node["owner"], node["authorized"])
                if node["kind"] == "CreatedNode":
                    preauth.add(pair)
                    events.append((t["ledger_index"], t["tx_index"], "preauth", pair))
                elif node["kind"] == "DeletedNode":
                    preauth.discard(pair)
            elif node["kind"] == "DeletedNode":
                state.pop(node["id"], None)
            else:
                old = state.get(node["id"], {"flags": 0})["flags"]
                state[node["id"]] = node["after"]
                for bit, name in FLAGS.items():
                    if old & bit and not node["after"]["flags"] & bit:
                        events.append((t["ledger_index"], t["tx_index"], "cleared", (node["id"], name)))

    by_pair = {}
    for p in pays:
        if p["result"] == "tesSUCCESS":
            by_pair.setdefault((p["sender"], p["dest"]), []).append(p)
    out = []
    for f in fails:
        key = (f["ledger"], f["tx"])
        retry = None
        for p in by_pair.get((f["sender"], f["dest"]), []):
            if (p["ledger"], p["tx"]) > key and p["ledger"] - f["ledger"] <= window:
                retry = p
                break
        fix = None
        if retry is not None:
            span = [e for e in events if key < (e[0], e[1]) < (retry["ledger"], retry["tx"])]
            blocking = BLOCKING.get(f["result"])
            if f["result"] == "tecDST_TAG_NEEDED" and not f["dest_tag"] and retry["dest_tag"]:
                fix = "dest_tag_fix"
            elif f["result"] == "tecNO_PERMISSION" and any(
                e[2] == "preauth" and e[3] == (f["dest"], f["sender"]) for e in span
            ):
                fix = "preauth_fix"
            elif blocking and any(e[2] == "cleared" and e[3] == (f["dest"], blocking) for e in span):
                fix = "flag_cleared"
            else:
                fix = "other"
        out.append(
            {
                **f,
                "retry_ledger": retry["ledger"] if retry else None,
                "retry_after_ledgers": retry["ledger"] - f["ledger"] if retry else None,
                "fix": fix,
                "censored": retry is None and f["ledger"] + window > b,
            }
        )
    grid = Counter((f["result"], f["dest_state"]) for f in out)
    return {
        "window": window,
        "payments": len(pays),
        "results": dict(sorted(results.items())),
        "failed": len(out),
        "by_code_and_dest_state": [
            {"result": k[0], "dest_state": k[1], "count": v} for k, v in sorted(grid.items())
        ],
        "retried": sum(1 for f in out if f["fix"]),
        "fixes": dict(sorted(Counter(f["fix"] for f in out if f["fix"]).items())),
        "censored": sum(1 for f in out if f["censored"]),
        "not_retried": sum(1 for f in out if not f["fix"] and not f["censored"]),
        "listed": out,
        "hourly": hourly(d, pays),
    }


def hourly(d, pays):
    close = {r["ledger_index"]: r["close_time"] for r in d["ledgers"]}
    t0 = close[min(close)] if close else 0
    hours = Counter()
    for p in pays:
        if p["ledger"] not in close:  # a missing ledger row is already a window mismatch
            continue
        h = (close[p["ledger"]] - t0) // 3600
        hours[(h, p["result"] == "tesSUCCESS")] += 1
    top = max((h for h, _ in hours), default=-1)
    return [{"hour": h, "ok": hours[(h, True)], "failed": hours[(h, False)]} for h in range(top + 1)]


def build(d, window):
    problems = check_window(d)
    rec = reconcile(d)
    rec["mismatches"] = problems + rec["mismatches"]
    return {
        "snapshots": {
            k: {x: d["snaps"][k][x] for x in ("ledger_index", "ledger_hash", "close_time_iso", "raw_sha256")}
            for k in ("A", "B")
        },
        "window_ledgers": len(d["ledgers"]),
        "transactions": len(d["txs"]),
        "census": {"A": census(d["A"]), "B": census(d["B"])},
        "reconcile": rec,
        "failures": failures(d, window),
    }


def pct(n, total):
    return f"{100.0 * n / total:.2f}%" if total else "n/a"


def render(r, crosscheck=None):
    a, b = r["snapshots"]["A"], r["snapshots"]["B"]
    ca, cb = r["census"]["A"], r["census"]["B"]
    rec, fl = r["reconcile"], r["failures"]
    L = []
    w = L.append
    w("# acctflags: account settings that block inbound payments, Post Fiat testnet")
    w("")
    w(f"Snapshot A: ledger {a['ledger_index']} ({a['close_time_iso']}), hash `{a['ledger_hash']}`  ")
    w(f"Snapshot B: ledger {b['ledger_index']} ({b['close_time_iso']}), hash `{b['ledger_hash']}`  ")
    w(f"Window: {r['window_ledgers']} validated ledgers A+1..B, {r['transactions']} transactions, gap-free ")
    w("(every ledger chained by parent_hash, tx counts equal).  ")
    w(f"Retry window: **{fl['window']} validated ledgers** (report.config.json).")
    w("")
    w("Account ids are the first 12 hex chars of HMAC-SHA256(private key, address).")
    w("")
    w("## 1. Census")
    w("")
    w("| | A | B |")
    w("|---|---:|---:|")
    w(f"| AccountRoots | {ca['accounts']} | {cb['accounts']} |")
    w(
        f"| any inbound-blocking flag | {ca['inbound_any']} ({pct(ca['inbound_any'], ca['accounts'])}) | "
        f"{cb['inbound_any']} ({pct(cb['inbound_any'], cb['accounts'])}) |"
    )
    w("")
    w("Per flag (accounts with the flag set; share of all AccountRoots):")
    w("")
    w("| flag | inbound-blocking | A | share A | B | share B |")
    w("|---|:---:|---:|---:|---:|---:|")
    names = [n for n in FLAGS.values()] + sorted(
        n for n in set(ca["per_flag"]) | set(cb["per_flag"]) if n.startswith("unknown")
    )
    for n in names:
        x, y = ca["per_flag"].get(n, 0), cb["per_flag"].get(n, 0)
        w(
            f"| {n} | {'yes' if n in INBOUND else ''} | {x} | {pct(x, ca['accounts'])} | {y} | "
            f"{pct(y, cb['accounts'])} |"
        )
    w("")
    w("Flag combinations (distinct Flags sets):")
    w("")
    w("| combination | A | B |")
    w("|---|---:|---:|")
    for k in sorted(set(ca["combinations"]) | set(cb["combinations"]), key=lambda k: (k != "(none)", k)):
        w(f"| {k} | {ca['combinations'].get(k, 0)} | {cb['combinations'].get(k, 0)} |")
    w("")
    w("Settings (accounts with the field present):")
    w("")
    w("| setting | A | B |")
    w("|---|---:|---:|")
    for s in SETTINGS:
        w(f"| {s} | {ca['settings'].get(s, 0)} | {cb['settings'].get(s, 0)} |")
    w("")
    w("![flags](charts/flags.png)")
    w("")
    w("## 2. Reconciliation A -> B from transaction metadata")
    w("")
    w("expected_B = A + set_by_tx - cleared_by_tx + created_with - deleted_with")
    w("")
    w("| kind | name | A | set_by_tx | cleared_by_tx | created_with | deleted_with | expected_B | B | diff |")
    w("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t in rec["table"]:
        w(
            f"| {t['kind']} | {t['name']} | {t['A']} | {t['set_by_tx']} | {t['cleared_by_tx']} | "
            f"{t['created_with']} | {t['deleted_with']} | {t['expected_B']} | {t['B']} | "
            f"{t['B'] - t['expected_B']} |"
        )
    ac, pa = rec["accounts"], rec["preauth"]
    w(
        f"| total | AccountRoots | {ac['A']} | | | {ac['created']} | {ac['deleted']} | {ac['expected_B']} | "
        f"{ac['B']} | {ac['B'] - ac['expected_B']} |"
    )
    w(
        f"| total | DepositPreauth pairs | {pa['A']} | | | {pa['created']} | {pa['deleted']} | "
        f"{pa['expected_B']} | {pa['B']} | {pa['B'] - pa['expected_B']} |"
    )
    w("")
    w(
        f"**Mismatches: {len(rec['mismatches'])}** "
        "(per-account replay vs snapshot B, PreviousFields checks, totals)"
    )
    w("")
    for m in rec["mismatches"]:
        w(f"- {m}")
    if rec["mismatches"]:
        w("")
    w("## 3. Payment failures by destination settings")
    w("")
    w(f"Payments in the window: {fl['payments']}; failed: {fl['failed']}.")
    w("")
    w("| result | count |")
    w("|---|---:|")
    for k, v in fl["results"].items():
        w(f"| {k} | {v} |")
    w("")
    w("Failed payments by result code and the destination's state just before the payment:")
    w("")
    w("| result | destination state | count |")
    w("|---|---|---:|")
    for g in fl["by_code_and_dest_state"]:
        w(f"| {g['result']} | {g['dest_state']} | {g['count']} |")
    if not fl["by_code_and_dest_state"]:
        w("| (none) | | 0 |")
    w("")
    w(
        f"Retries (first later tesSUCCESS Payment, same sender -> same destination, "
        f"<= {fl['window']} validated ledgers): retried {fl['retried']}, not retried {fl['not_retried']}, "
        f"censored (window reaches past B) {fl['censored']}."
    )
    w("")
    w("| fix | count |")
    w("|---|---:|")
    for k in ("dest_tag_fix", "preauth_fix", "flag_cleared", "other"):
        w(f"| {k} | {fl['fixes'].get(k, 0)} |")
    w("")
    if fl["listed"]:
        w("| ledger | sender | destination | result | dest state | tag | retry after (ledgers) | fix |")
        w("|---:|---|---|---|---|:---:|---:|---|")
        for f in fl["listed"]:
            after = "" if f["retry_after_ledgers"] is None else f["retry_after_ledgers"]
            w(
                f"| {f['ledger']} | {f['sender']} | {f['dest']} | {f['result']} | {f['dest_state']} | "
                f"{'y' if f['dest_tag'] else 'n'} | {after} "
                f"| {f['fix'] or ('censored' if f['censored'] else 'none')} |"
            )
        w("")
    w("![payments](charts/payments.png)")
    w("")
    if crosscheck:
        w("## 4. Public RPC crosscheck")
        w("")
        w("| snapshot | ledger | accounts | checks | match | mismatch | negative control mismatches |")
        w("|---|---:|---:|---:|---:|---:|---:|")
        for k in ("A", "B"):
            c = crosscheck.get(k)
            if not c:
                continue
            res, neg = c["positive"]["results"], c["negative"]["results"]
            n_acc = sum(1 for x in res if x["check"] == "account")
            bad = sum(1 for x in res if not x["ok"])
            li = c["positive"]["ledger_index"]
            w(
                f"| {k} | {li} | {n_acc} | {len(res)} | {len(res) - bad} | {bad} | "
                f"{sum(1 for x in neg if not x['ok'])} of {sum(1 for x in neg if x['check'] == 'account')} |"
            )
        w("")
    return "\n".join(L) + "\n"


def charts(r, outdir):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["svg.hashsalt"] = "acctflags"
    os.makedirs(os.path.join(outdir, "charts"), exist_ok=True)
    ca, cb = r["census"]["A"], r["census"]["B"]
    names = list(FLAGS.values())
    fig, ax = plt.subplots(figsize=(8, 5), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    ys = range(len(names))
    ax.barh(
        [y + 0.2 for y in ys],
        [ca["per_flag"].get(n, 0) for n in names],
        height=0.4,
        color="#2a78d6",
        label="A",
    )
    ax.barh(
        [y - 0.2 for y in ys],
        [cb["per_flag"].get(n, 0) for n in names],
        height=0.4,
        color="#e3a33b",
        label="B",
    )
    ax.set_yticks(list(ys), [n + (" *" if n in INBOUND else "") for n in names], fontsize=7)
    ax.set_xlabel(
        f"accounts with flag set (of {ca['accounts']} at A, {cb['accounts']} at B); * = blocks inbound"
    )
    ax.set_title("AccountRoot flags at snapshots A and B", fontsize=10, loc="left")
    ax.grid(axis="x", color="#e6e5e1", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "charts", "flags.png"), dpi=100, metadata={"Software": None})
    plt.close(fig)

    hours = r["failures"]["hourly"]
    fig, ax = plt.subplots(figsize=(8, 3.2), facecolor="#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    xs = [h["hour"] for h in hours]
    ax.bar(xs, [h["ok"] for h in hours], color="#2a78d6", label="tesSUCCESS")
    ax.bar(xs, [h["failed"] for h in hours], bottom=[h["ok"] for h in hours], color="#e34948", label="failed")
    ax.set_xlabel("hour since snapshot A")
    ax.set_ylabel("payments")
    ax.set_title("Payments per hour in the window", fontsize=10, loc="left")
    ax.grid(axis="y", color="#e6e5e1", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "charts", "payments.png"), dpi=100, metadata={"Software": None})
    plt.close(fig)


def no_network():
    def refuse(*_a, **_k):
        raise OSError("--offline: network access refused")

    socket.socket = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def load_crosscheck(data):
    out = {}
    for k in ("A", "B"):
        pos, neg = (
            os.path.join(data, f"crosscheck-{k}.json"),
            os.path.join(data, f"crosscheck-{k}-negative.json"),
        )
        if os.path.exists(pos) and os.path.exists(neg):
            with open(pos, encoding="utf-8") as fh, open(neg, encoding="utf-8") as fn:
                out[k] = {"positive": json.load(fh), "negative": json.load(fn)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="acctflags/data")
    ap.add_argument("--config", default="acctflags/report.config.json")
    ap.add_argument("--out", default="acctflags")
    ap.add_argument("--retry-window", type=int)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-charts", action="store_true")
    a = ap.parse_args(argv)
    if a.offline:
        no_network()
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    window = a.retry_window if a.retry_window is not None else cfg["retry_window_ledgers"]
    r = build(load(a.data), window)
    text = render(r, load_crosscheck(a.data))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(a.out, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(r, fh, sort_keys=True, indent=1)
        fh.write("\n")
    if not a.no_charts:
        charts(r, a.out)
    n = len(r["reconcile"]["mismatches"])
    print(
        f"RECONCILE mismatches={n} accounts A={r['census']['A']['accounts']} "
        f"B={r['census']['B']['accounts']} "
        f"window_ledgers={r['window_ledgers']} txs={r['transactions']} payments={r['failures']['payments']} "
        f"failed={r['failures']['failed']} retried={r['failures']['retried']} retry_window={window}"
    )
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
