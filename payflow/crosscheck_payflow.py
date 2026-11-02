#!/usr/bin/env python3
"""Cross-check a payflow/ collection against the public RPC (read-only: tx, ledger), or offline.

Draws --count payments (seeded: up to 2 non-tesSUCCESS payments, the rest uniform over the successful
ones) and prints one
OK/MISMATCH line per check (accounts and hashes truncated):

  P  `tx` by hash (must be validated): ledger, sender, destination, result, fee, the metadata
     delivered amount (drops for native PFT, compact JSON otherwise, empty if nothing was delivered)
     and the per-account AccountRoot balance changes of its metadata must equal the local
     payments.csv row and the deltas.csv rows of that hash. The RPC side is parsed here, not with
     the collector's code.
  L  `ledger` of the payment's ledger: ledger_hash and transaction count must equal ledgers.csv.

--negative is the control: every expected fee gets +1 drop, every expected delivered amount is
replaced by a sentinel (also when it is empty), and every expected ledger hash is reversed, so
every check must MISMATCH and the run must exit 1.

--json FILE records the sample, the RPC-side records and every verdict. --offline FILE makes no RPC
call: it re-reads the local data, rebuilds the local records for the recorded hashes, re-derives the
seeded sample, and checks that comparing them with the recorded RPC records gives the recorded
verdicts (exit 0 only if the sample, every record and every verdict match).

Retried public-RPC answers (slowDown, lgrNotFound, internal, transport errors) print a RETRY line
and are counted per error in the SUMMARY line, never as mismatches; if they persist the run exits 2.
Exit 0 all OK, 1 any mismatch, 2 RPC failure or unusable input.
"""

import argparse
import csv
import json
import os
import random
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "rewardflow"))
from collect_rewardflow import RETRY_ERRORS, Rpc, RpcError  # noqa: E402

RETRY = RETRY_ERRORS | {"lgrNotFound", "internal", "txnNotFound"}
METHODS = ("tx", "ledger")
SENTINEL = "NEGATIVE-CONTROL"


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def short(s):
    return s if len(s) <= 12 else f"{s[:6]}...{s[-4:]}"


def compact(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def local_records(data):
    pays = read_csv(os.path.join(data, "payments.csv"))
    deltas = read_csv(os.path.join(data, "deltas.csv"))
    ledgers = {r["ledger_index"]: r for r in read_csv(os.path.join(data, "ledgers.csv"))}
    by_tx = defaultdict(lambda: defaultdict(int))
    for d in deltas:
        by_tx[d["tx_hash"]][d["account"]] += int(d["delta_drops"])
    recs = {}
    for p in pays:
        delivered = p["delivered_drops"] or p["delivered_raw"]
        recs[p["tx_hash"]] = {
            "ledger_index": int(p["ledger_index"]),
            "account": p["account"],
            "destination": p["destination"],
            "result": p["result"],
            "fee_drops": int(p["fee_drops"]),
            "delivered": delivered,
            "balance_changes": {a: v for a, v in sorted(by_tx[p["tx_hash"]].items()) if v},
        }
    return recs, ledgers


def sample(recs, count, seed, failed=2):
    """Seeded sample of `count` payments: up to `failed` non-tesSUCCESS ones (so the empty-delivered path is
    always crosschecked when the window has any), the rest uniform over the successful ones."""
    rng = random.Random(seed)
    bad = sorted(h for h, r in recs.items() if r["result"] != "tesSUCCESS")
    good = sorted(h for h, r in recs.items() if r["result"] == "tesSUCCESS")
    picks = rng.sample(bad, min(failed, len(bad), count))
    return picks + rng.sample(good, min(count - len(picks), len(good)))


def rpc_record(res):
    """Normalise a public-RPC `tx` answer (API v1 or v2 shape) into the local record layout."""
    tx = res.get("tx_json") or res
    meta = res.get("meta") or res.get("metaData") or {}
    ok = meta.get("TransactionResult") == "tesSUCCESS"
    d = meta.get("delivered_amount", meta.get("DeliveredAmount"))
    if not ok or d in (None, "unavailable"):
        delivered = ""
    elif isinstance(d, str):
        delivered = str(int(d))
    else:
        delivered = compact(d)
    changes = defaultdict(int)
    for node in meta.get("AffectedNodes") or []:
        for kind, body in node.items():
            if body.get("LedgerEntryType") != "AccountRoot":
                continue
            fin, prev, new = (
                body.get("FinalFields") or {},
                body.get("PreviousFields") or {},
                body.get("NewFields") or {},
            )
            if kind == "CreatedNode":
                changes[new["Account"]] += int(new.get("Balance", 0))
            elif kind == "DeletedNode":
                changes[fin["Account"]] -= int(prev.get("Balance", fin.get("Balance", 0)))
            elif "Balance" in prev:
                changes[fin["Account"]] += int(fin["Balance"]) - int(prev["Balance"])
    return {
        "validated": bool(res.get("validated")),
        "ledger_index": int(res.get("ledger_index", tx.get("ledger_index", 0))),
        "account": tx.get("Account", ""),
        "destination": tx.get("Destination", ""),
        "result": meta.get("TransactionResult", ""),
        "fee_drops": int(tx.get("Fee", 0)),
        "delivered": delivered,
        "balance_changes": {a: v for a, v in sorted(changes.items()) if v},
    }


def expected(rec, negative):
    want = dict(rec)
    if negative:
        want["fee_drops"] = rec["fee_drops"] + 1
        want["delivered"] = SENTINEL
    return want


def compare_p(h, want, got):
    diffs = [
        k
        for k in (
            "ledger_index",
            "account",
            "destination",
            "result",
            "fee_drops",
            "delivered",
            "balance_changes",
        )
        if want[k] != got[k]
    ]
    if not got.get("validated"):
        diffs.append("validated")
    line = (
        f"P {short(h)} ledger={want['ledger_index']} {short(want['account'])}->{short(want['destination'])} "
        f"result={got['result']} fee={got['fee_drops']} delivered={got['delivered'] or '-'} "
        f"accounts_changed={len(got['balance_changes'])}" + (f" differs={diffs}" if diffs else "")
    )
    return not diffs, line


def compare_l(li, ledger_row, got, negative):
    want_hash = ledger_row["ledger_hash"][::-1] if negative else ledger_row["ledger_hash"]
    ok = got["ledger_hash"] == want_hash and got["txn_count"] == int(ledger_row["txn_count"])
    return ok, (
        f"L {li} hash_equal={got['ledger_hash'] == want_hash} local_txn_count={ledger_row['txn_count']} "
        f"rpc_txn_count={got['txn_count']}"
    )


def run(data, rpc, count, seed, negative=False, out=print):
    recs, ledgers = local_records(data)
    picks = sample(recs, count, seed)
    res = {"ok": 0, "mismatch": 0}
    rec_out = {
        "seed": seed,
        "count": count,
        "negative": negative,
        "sample": picks,
        "rpc": {},
        "rpc_ledgers": {},
        "verdicts": {},
    }

    def report(key, ok, line):
        res["ok" if ok else "mismatch"] += 1
        rec_out["verdicts"][key] = ok
        out(f"{'OK      ' if ok else 'MISMATCH'} {line}")

    for h in picks:
        got = rpc_record(rpc.call("tx", {"transaction": h, "binary": False}))
        rec_out["rpc"][h] = got
        ok, line = compare_p(h, expected(recs[h], negative), got)
        report(f"P:{h}", ok, line)
    for li in sorted({str(recs[h]["ledger_index"]) for h in picks}, key=int):
        led = rpc.call("ledger", {"ledger_index": int(li), "transactions": True})["ledger"]
        got = {"ledger_hash": led.get("ledger_hash", ""), "txn_count": len(led.get("transactions") or [])}
        rec_out["rpc_ledgers"][li] = got
        ok, line = compare_l(li, ledgers[li], got, negative)
        report(f"L:{li}", ok, line)
    return res, rec_out


def offline(data, rec, out=print):
    """Re-check a recorded --json result against the local data, without RPC. Returns failure count."""
    recs, ledgers = local_records(data)
    fails = 0

    def check(ok, line):
        nonlocal fails
        fails += 0 if ok else 1
        out(f"{'OK      ' if ok else 'FAIL    '} {line}")

    picks = sample(recs, rec["count"], rec["seed"])
    check(picks == rec["sample"], f"sample seed={rec['seed']} count={len(picks)} equals recorded sample")
    for h in rec["sample"]:
        if h not in recs:
            check(False, f"P {short(h)} not in local payments.csv")
            continue
        ok, line = compare_p(h, expected(recs[h], rec["negative"]), rec["rpc"][h])
        check(
            ok == rec["verdicts"].get(f"P:{h}"), f"verdict {'OK' if ok else 'MISMATCH'} == recorded: {line}"
        )
    for li, got in sorted(rec["rpc_ledgers"].items(), key=lambda kv: int(kv[0])):
        ok, line = compare_l(li, ledgers[li], got, rec["negative"])
        check(
            ok == rec["verdicts"].get(f"L:{li}"), f"verdict {'OK' if ok else 'MISMATCH'} == recorded: {line}"
        )
    want_keys = {f"P:{h}" for h in rec["sample"]} | {f"L:{li}" for li in rec["rpc_ledgers"]}
    check(set(rec["verdicts"]) == want_keys, f"recorded verdicts cover exactly {len(want_keys)} checks")
    return fails


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/payflow/tx")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--seed", default="1")
    p.add_argument("--negative", action="store_true")
    p.add_argument("--json", default=None, help="record sample, RPC records and verdicts here")
    p.add_argument("--offline", metavar="JSON", default=None, help="no RPC: re-check a recorded --json file")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5, help="seconds between public RPC requests")
    a = p.parse_args(argv)
    if a.offline:
        try:
            with open(a.offline) as fh:
                rec = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"INPUT ERROR {e}")
            return 2
        fails = offline(a.data, rec)
        print(
            f"RESULT {'PASS' if fails == 0 else 'FAIL'} offline fails={fails} "
            f"recorded_negative={rec['negative']}"
        )
        return 1 if fails else 0
    rpc = Rpc(a.rpc, gap=a.gap, retry_errors=RETRY, methods=METHODS)
    try:
        res, rec = run(a.data, rpc, a.count, a.seed, a.negative)
    except RpcError as e:
        print(f"RPC FAILURE {e}")
        return 2
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(rec, fh, indent=1, sort_keys=True)
            fh.write("\n")
    print(
        f"SUMMARY ok={res['ok']} mismatch={res['mismatch']} negative={a.negative} seed={a.seed} "
        f"rpc_calls={rpc.calls} retries={rpc.retried} "
        f"retries_by_error={dict(sorted(rpc.retry_kinds.items()))}"
    )
    return 1 if res["mismatch"] else 0


if __name__ == "__main__":
    sys.exit(main())
