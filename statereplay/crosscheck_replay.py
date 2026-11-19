#!/usr/bin/env python3
"""Crosscheck a captured A -> B window and snapshot B against the public RPC (stdlib only).

Two independent checks, both against https://rpc.testnet.postfiat.org/ (not the node that collected):
  ledgers  snapshot A's and B's ledgers plus --ledgers (default 24) ledgers drawn from A+1..B-1 with
           random.Random(seed = A's ledger hash): `ledger ledger_index=N transactions=true` (hashes
           only) must give the captured ledger_hash, parent_hash, transaction count and the exact set
           of transaction hashes
  objects  --objects (default 64) ledger objects drawn from every object of snapshot B with
           random.Random(seed = B's ledger hash): `ledger_entry index=X ledger_hash=<B>` must return
           the snapshot's object field-for-field

The draw is printed and saved in the summary, so a rerun checks the same sample. `server_info` is read
first; a ledger outside the public `complete_ledgers` stops with exit 2 before any check. Calls use the
acctflags Rpc client (read-only allowlist server_info/ledger/ledger_entry, <= --rate requests/s, default
2, logged RETRY lines with exponential backoff; a call that never answers is GAVE_UP, exit 2).
`--negative` alters every expected value (one hex digit of each ledger hash, one field of each object),
so every check must MISMATCH: the control. `--record FILE` saves every answer, `--offline FILE`
replays them with no network (exit 3 on a call that was not recorded). Recordings hold real addresses
and stay private; the --summary JSON names objects only by index and type.

Usage:
  ./crosscheck_replay.py data/statereplay/A data/statereplay/B data/statereplay/tx \
      [--ledgers 24] [--objects 64] [--negative] [--record F | --offline F] [--summary F]
Exit: 0 all MATCH (or, with --negative, all MISMATCH), 1 otherwise, 2 GAVE_UP / out of public range,
3 offline call not recorded.
"""

import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "acctflags"))

import collect_flags as C  # noqa: E402
import crosscheck_flags as XF  # noqa: E402
import replay  # noqa: E402

PUBLIC = XF.PUBLIC
ALLOW = {"server_info", "ledger", "ledger_entry"}


def draw_ledgers(first, last, count, seed):
    inner = list(range(first + 1, last))
    picked = random.Random(seed).sample(inner, min(count, len(inner)))
    return sorted({first, last, *picked})


def draw_objects(target, count, seed):
    return sorted(random.Random(seed).sample(sorted(target), min(count, len(target))))


def flip_hex(h):
    return h[:-1] + ("0" if h[-1] != "0" else "1")


def alter(obj):
    """Negative control: change one field of the expected object (Flags if present, else add one)."""
    o = dict(obj)
    if isinstance(o.get("Flags"), int):
        o["Flags"] = o["Flags"] ^ 1
    else:
        o["NegativeControl"] = 1
    return o


def check_ledgers(rpc, rows, tx_hashes, picked, negative, out):
    by_row = {r["ledger_index"]: r for r in rows}
    results = []
    for n in picked:
        row = by_row[n]
        res = rpc.call("ledger", {"ledger_index": n, "transactions": True, "expand": False})
        led = res["ledger"]
        want_hash = flip_hex(row["ledger_hash"]) if negative else row["ledger_hash"]
        got_tx = sorted(led.get("transactions", []))
        problems = []
        if led.get("ledger_hash") != want_hash:
            problems.append("ledger_hash")
        if led.get("parent_hash") != row["parent_hash"]:
            problems.append("parent_hash")
        if n in tx_hashes:
            if len(got_tx) != row["txn_count"]:
                problems.append("txn_count")
            if got_tx != tx_hashes[n]:
                problems.append("tx_hashes")
        verdict = "MISMATCH" if problems else "MATCH"
        out(f"LEDGER {n} txs={len(got_tx)} {verdict}" + (f" fields={','.join(problems)}" if problems else ""))
        results.append({"ledger_index": n, "txs": len(got_tx), "verdict": verdict, "fields": problems})
    return results


def check_objects(rpc, header, target, picked, negative, out):
    results = []
    for idx in picked:
        want = alter(target[idx]) if negative else target[idx]
        try:
            res = rpc.call("ledger_entry", {"index": idx, "ledger_hash": header["ledger_hash"]})
            got = res.get("node")
        except C.RpcError as e:
            got, res = None, {"error": str(e)}
        fields = sorted(k for k in set(want) | set(got or {}) if (got or {}).get(k) != want.get(k))
        verdict = "MATCH" if got == want else "MISMATCH"
        out(
            f"OBJECT {idx} {want['LedgerEntryType']} {verdict}"
            + (f" fields={','.join(fields)}" if fields else "")
        )
        results.append({"index": idx, "type": want["LedgerEntryType"], "verdict": verdict, "fields": fields})
    return results


def run(rpc, ha, hb, target, rows, txs, n_ledgers, n_objects, negative=False, out=print):
    info = rpc.call("server_info", {})["info"]
    complete = info.get("complete_ledgers", "")
    out(f"PUBLIC complete_ledgers={complete} build={info.get('build_version')}")
    for n in (ha["ledger_index"], hb["ledger_index"]):
        if not XF.in_range(complete, n):
            raise C.GaveUp(f"ledger {n} is outside the public complete_ledgers {complete}")
    tx_hashes = {r["ledger_index"]: sorted(t["hash"] for t in r["transactions"]) for r in txs}
    lp = draw_ledgers(ha["ledger_index"], hb["ledger_index"], n_ledgers, ha["ledger_hash"])
    op = draw_objects(target, n_objects, hb["ledger_hash"])
    out(f"DRAW ledgers={len(lp)} objects={len(op)} seedA={ha['ledger_hash']} seedB={hb['ledger_hash']}")
    lres = check_ledgers(rpc, rows, tx_hashes, lp, negative, out)
    ores = check_objects(rpc, hb, target, op, negative, out)
    allres = lres + ores
    match = sum(r["verdict"] == "MATCH" for r in allres)
    summary = {
        "mode": "negative" if negative else "positive",
        "public_complete_ledgers": complete,
        "A": ha["ledger_index"],
        "B": hb["ledger_index"],
        "checks": len(allres),
        "match": match,
        "mismatch": len(allres) - match,
        "ledgers": lres,
        "objects": ores,
        "object_types": dict(
            sorted({t: sum(r["type"] == t for r in ores) for t in {r["type"] for r in ores}}.items())
        ),
        "rpc_calls": rpc.calls,
        "retries": rpc.retried,
    }
    ok = summary["mismatch"] == 0 if not negative else summary["match"] == 0
    out(
        f"RESULT mode={summary['mode']} checks={summary['checks']} match={match} "
        f"mismatch={summary['mismatch']} rpc_calls={rpc.calls} retries={rpc.retried} "
        f"{'PASS' if ok else 'FAIL'}"
    )
    return summary, ok


def main(argv=None, transport=None, sleep=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("tx")
    ap.add_argument("--ledgers", type=int, default=24)
    ap.add_argument("--objects", type=int, default=64)
    ap.add_argument("--rate", type=float, default=2.0)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--backoff", type=float, default=2.0)
    ap.add_argument("--negative", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--record")
    g.add_argument("--offline")
    ap.add_argument("--summary")
    a = ap.parse_args(argv)
    send = transport or XF.http_transport()
    if a.offline:
        send = XF.replay(a.offline)
    elif a.record:
        send = XF.recording(send, a.record)
    if sleep is None:
        sleep = (lambda s: None) if a.offline else C.time.sleep
    rpc = C.Rpc(send, print, a.rate, a.retries, a.backoff, sleep=sleep, allow=ALLOW)
    ha, _, hb, target, rows, txs = replay.load_raw(a.a, a.b, a.tx)
    try:
        summary, ok = run(rpc, ha, hb, target, rows, txs, a.ledgers, a.objects, a.negative)
    except C.GaveUp as e:
        print(f"GAVE_UP {e}")
        return 2
    except XF.NotRecorded as e:
        print(f"NOT_RECORDED {str(e)[:200]}")
        return 3
    if a.summary:
        with open(a.summary, "w") as f:
            json.dump(summary, f, indent=1, sort_keys=True)
            f.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
