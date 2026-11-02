#!/usr/bin/env python3
"""Reconcile two state snapshots against a payflow/ collection, exact to the drop (offline).

Inputs: snapshot A (state/snapshot.py) of ledger S-1, snapshot B of the collection's last ledger L,
and the collection (--data) covering S..L with no holes or gaps. Every check prints one
OK/MISMATCH line; every mismatch line carries a cause:

  C1 coverage      ledgers S..L each fetched once, no gaps.csv rows, tx rows == txn_count per
                   ledger; snapshot A is ledger S-1 and snapshot B is ledger L (cause: snapshot_not_adjacent,
                   hole, gap, duplicate, txn_count)
  C2 burn          total_coins(A) - total_coins(B) == sum fee_drops of tx.csv (cause: burn_ne_fees)
  C3 holders       native held by Escrow + PayChannel objects in A and B; their change must equal the
                   AccountRoot deltas of the tx types that move PFT into or out of them (cause:
                   non_account_holder)
  C4 accounts      per AccountRoot: balance(A) + sum delta_drops == balance(B); created accounts start
                   at 0, deleted ones end at 0 (cause: account_balance, per account)
  C5 chains        per account, the delta rows in (ledger, tx) order form a chain: the first prev_drops
                   is balance(A) and each prev_drops is the previous final_drops (cause: chain_break)
  C6 transactions  per tx: sum of its AccountRoot deltas == -fee_drops, unless it is one of the escrow /
                   payment-channel types (counted in C3) (cause: tx_value_not_conserved)
  C7 payments      per counted native payment (tesSUCCESS, delivered > 0, sender != destination):
                   destination delta == delivered_drops and sender delta == -(delivered + fee) in the
                   same tx (cause: dest_delta, sender_delta; payments whose metadata also touches other
                   accounts' balances are reported as cause path_payment and checked only by C4-C6)

--negative is the control: it adds 1 drop to snapshot B's first AccountRoot balance and to every
expected payment delivery and tx fee, so C2, C4, C6 and C7 must MISMATCH and the run must exit 1.
Exit 0 if every check is OK, 1 on any mismatch, 2 on unusable input. --json writes the result.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "acctchurn"))
from diff_churn import SnapshotError, load, native_held  # noqa: E402

HOLDER_TYPES = {
    "EscrowCreate",
    "EscrowFinish",
    "EscrowCancel",
    "PaymentChannelCreate",
    "PaymentChannelFund",
    "PaymentChannelClaim",
}


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def balances(objs):
    return {
        o["Account"]: int(o["Balance"]) for o in objs.values() if o.get("LedgerEntryType") == "AccountRoot"
    }


def reconcile(ha, a, hb, b, txs, pays, deltas, ledgers, gaps, negative=False):
    lines, causes = [], defaultdict(int)

    def report(ok, check, line, cause=None):
        lines.append(f"{'OK      ' if ok else 'MISMATCH'} {check} {line}" + ("" if ok else f" cause={cause}"))
        if not ok:
            causes[cause] += 1

    bal_a, bal_b = balances(a), balances(b)
    if negative and bal_b:
        first = sorted(bal_b)[0]
        bal_b[first] += 1
    fee_bump = 1 if negative else 0

    # C1 coverage
    idx = [int(r["ledger_index"]) for r in ledgers]
    lo, hi = (min(idx), max(idx)) if idx else (None, None)
    report(
        bool(idx) and int(ha["ledger_index"]) == lo - 1 and int(hb["ledger_index"]) == hi,
        "C1",
        f"snapshots A={ha['ledger_index']} B={hb['ledger_index']} collection={lo}..{hi}",
        "snapshot_not_adjacent",
    )
    if idx:
        holes = sorted(set(range(lo, hi + 1)) - set(idx))
        report(not holes, "C1", f"holes={len(holes)} first={holes[:3]}", "hole")
        report(len(idx) == len(set(idx)), "C1", f"duplicate_ledgers={len(idx) - len(set(idx))}", "duplicate")
    report(not gaps, "C1", f"gaps={len(gaps)}", "gap")
    rows_per = defaultdict(int)
    for r in txs:
        rows_per[int(r["ledger_index"])] += 1
    bad = [r["ledger_index"] for r in ledgers if rows_per[int(r["ledger_index"])] != int(r["txn_count"])]
    report(not bad, "C1", f"ledgers_with_tx_rows_ne_txn_count={len(bad)} tx_rows={len(txs)}", "txn_count")

    # C2 burn
    fees = sum(int(r["fee_drops"]) + fee_bump for r in txs)
    burned = int(ha["total_coins"]) - int(hb["total_coins"])
    report(burned == fees, "C2", f"burned={burned} fees={fees} txs={len(txs)}", "burn_ne_fees")

    # C3 escrow / payment-channel holders
    na, nb = native_held(a), native_held(b)
    held_change = (nb["escrow"] + nb["paychannel"]) - (na["escrow"] + na["paychannel"])
    by_tx = defaultdict(int)
    for d in deltas:
        by_tx[d["tx_hash"]] += int(d["delta_drops"])
    holder_txs = [t for t in txs if t["tx_type"] in HOLDER_TYPES]
    into_holders = -sum(by_tx[t["tx_hash"]] + int(t["fee_drops"]) for t in holder_txs)
    report(
        held_change == into_holders,
        "C3",
        f"escrow+paychannel A={na['escrow'] + na['paychannel']} B={nb['escrow'] + nb['paychannel']} "
        f"change={held_change} from_holder_txs={into_holders} holder_txs={len(holder_txs)}",
        "non_account_holder",
    )

    # C4 accounts and C5 chains
    order = sorted(deltas, key=lambda d: (int(d["ledger_index"]), int(d["tx_index"])))
    per_acct = defaultdict(list)
    for d in order:
        per_acct[d["account"]].append(d)
    accounts = sorted(set(bal_a) | set(bal_b) | set(per_acct))
    acct_bad = chain_bad = 0
    for acct in accounts:
        rows = per_acct.get(acct, [])
        start, end = bal_a.get(acct, 0), bal_b.get(acct, 0)
        got = start + sum(int(d["delta_drops"]) for d in rows)
        if got != end:
            acct_bad += 1
            report(
                False,
                "C4",
                f"{acct[:6]}...{acct[-4:]} A={start} +deltas={got - start} B={end}",
                "account_balance",
            )
        prev = start
        for d in rows:
            if int(d["prev_drops"]) != prev:
                chain_bad += 1
                report(
                    False,
                    "C5",
                    f"{acct[:6]}...{acct[-4:]} ledger={d['ledger_index']} tx={d['tx_index']} "
                    f"prev_drops={d['prev_drops']} expected={prev}",
                    "chain_break",
                )
                break
            prev = int(d["final_drops"])
    report(
        acct_bad == 0,
        "C4",
        f"accounts={len(accounts)} in_A={len(bal_a)} in_B={len(bal_b)} with_deltas={len(per_acct)} "
        f"balance_mismatches={acct_bad}",
        "account_balance",
    )
    report(
        chain_bad == 0,
        "C5",
        f"chains={len(per_acct)} delta_rows={len(deltas)} breaks={chain_bad}",
        "chain_break",
    )

    # C6 transactions
    tx_bad = 0
    for t in txs:
        if t["tx_type"] in HOLDER_TYPES:
            continue
        want = -(int(t["fee_drops"]) + fee_bump)
        if by_tx[t["tx_hash"]] != want:
            tx_bad += 1
            if tx_bad <= 10:
                report(
                    False,
                    "C6",
                    f"{t['tx_hash'][:12]} {t['tx_type']} deltas={by_tx[t['tx_hash']]} -fee={want}",
                    "tx_value_not_conserved",
                )
    report(
        tx_bad == 0,
        "C6",
        f"txs={len(txs) - len(holder_txs)} not_conserved={tx_bad}",
        "tx_value_not_conserved",
    )

    # C7 payments
    rows_by_tx = defaultdict(list)
    for d in deltas:
        rows_by_tx[d["tx_hash"]].append(d)
    checked = path = pay_bad = 0
    for p in pays:
        if p["result"] != "tesSUCCESS" or p["kind"] != "native" or not p["delivered_drops"]:
            continue
        if int(p["delivered_drops"]) <= 0 or p["account"] == p["destination"]:
            continue
        rows = rows_by_tx[p["tx_hash"]]
        touched = {d["account"] for d in rows}
        if touched - {p["account"], p["destination"]}:
            path += 1
            continue
        checked += 1
        delivered = int(p["delivered_drops"]) + fee_bump
        dest = sum(int(d["delta_drops"]) for d in rows if d["account"] == p["destination"])
        send = sum(int(d["delta_drops"]) for d in rows if d["account"] == p["account"])
        for ok, cause, line in (
            (dest == delivered, "dest_delta", f"dest_delta={dest} delivered={delivered}"),
            (
                send == -(delivered + int(p["fee_drops"])),
                "sender_delta",
                f"sender_delta={send} -(delivered+fee)={-(delivered + int(p['fee_drops']))}",
            ),
        ):
            if not ok:
                pay_bad += 1
                if pay_bad <= 10:
                    report(False, "C7", f"{p['tx_hash'][:12]} {line}", cause)
    report(
        pay_bad == 0,
        "C7",
        f"payments_checked={checked} path_payments={path} mismatches={pay_bad}",
        "payment_delta",
    )

    ok = sum(1 for x in lines if x.startswith("OK"))
    result = {
        "snapshot_a": {
            "ledger_index": ha["ledger_index"],
            "ledger_hash": ha["ledger_hash"],
            "total_coins": ha["total_coins"],
        },
        "snapshot_b": {
            "ledger_index": hb["ledger_index"],
            "ledger_hash": hb["ledger_hash"],
            "total_coins": hb["total_coins"],
        },
        "ledgers": {"first": lo, "last": hi, "count": len(idx)},
        "rows": {"tx": len(txs), "payments": len(pays), "deltas": len(deltas)},
        "fees_drops": fees,
        "burned_drops": burned,
        "accounts": len(accounts),
        "payments_checked": checked,
        "path_payments": path,
        "ok_lines": ok,
        "mismatch_lines": len(lines) - ok,
        "mismatch_causes": dict(sorted(causes.items())),
        "negative": negative,
    }
    return lines, result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snap_a")
    p.add_argument("snap_b")
    p.add_argument("--data", default="data/payflow/tx")
    p.add_argument("--negative", action="store_true")
    p.add_argument("--json", default=None)
    a = p.parse_args(argv)
    try:
        ha, oa = load(a.snap_a)
        hb, ob = load(a.snap_b)
    except (SnapshotError, OSError, ValueError) as e:
        print(f"INPUT ERROR {e}")
        return 2
    data = {
        n: read_csv(os.path.join(a.data, f"{n}.csv")) for n in ("tx", "payments", "deltas", "ledgers", "gaps")
    }
    if not data["ledgers"]:
        print(f"INPUT ERROR no ledgers.csv rows in {a.data}")
        return 2
    lines, res = reconcile(
        ha,
        oa,
        hb,
        ob,
        data["tx"],
        data["payments"],
        data["deltas"],
        data["ledgers"],
        data["gaps"],
        a.negative,
    )
    for line in lines:
        print(line)
    print(
        f"SUMMARY ok={res['ok_lines']} mismatch={res['mismatch_lines']} causes={res['mismatch_causes']} "
        f"negative={a.negative} accounts={res['accounts']} fees={res['fees_drops']} "
        f"burned={res['burned_drops']}"
    )
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
            fh.write("\n")
    return 1 if res["mismatch_lines"] else 0


if __name__ == "__main__":
    sys.exit(main())
