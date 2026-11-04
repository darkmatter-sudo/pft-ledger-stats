#!/usr/bin/env python3
"""Recount random ledgers of a growth/ collection through the public RPC (read-only: server_info, ledger).

Every sampled ledger is fetched again from the public RPC (binary and JSON, the same two calls as
the collector) and turned into rows by the collector's own join (`growth_rows`: LWR header hash
and TXN hashes must agree). The recount must equal the collection exactly:
  ledger row: hash, close time, txn_count, header/tx_blob/meta bytes, created/modified/deleted
  tx rows:    (ledger, tx index, hash, type, result, blob/meta bytes, node counts) as a set
  node rows:  (ledger, tx index, action, LedgerEntryType) as a multiset
Each ledger prints one OK/MISMATCH line naming the fields that differ.

Sample: --count ledgers (seeded), half drawn from ledgers with at least one transaction and the
rest from all remaining ledgers, so the size fields of real transactions are covered too (most
testnet ledgers are empty).

Range: the public RPC keeps only part of the history. server_info's complete_ledgers is read
first; only the overlap of the collection with it, starting --margin ledgers above the RPC's
oldest ledger, is sampled. A RANGE line prints the checked and the NOT CHECKABLE span; server_info
is read again at the end and the run exits 2 if the RPC pruned into the checked span meanwhile.

Retries: transport errors, slowDown/tooBusy/lgrNotFound/internal answers are retried with
exponential backoff (one RETRY line each), and a public answer whose binary and JSON halves
disagree is fetched again up to --join-retries times (one REFETCH line each). Retries are never
mismatches; if they persist the run exits 2.

--record FILE  writes every recount (rows + sha256 of both raw answers) as JSON.
--offline FILE  re-runs the comparison from such a file against the CSVs, with no RPC at all.
--negative     control: every expected ledger row gets header_bytes+1, and every expected tx set and
               node multiset one extra row, so every ledger must MISMATCH (differ=header_bytes,tx_rows,
               node_rows) and the run must exit 1.
Exit 0 all OK, 1 any mismatch, 2 RPC failure.
"""

import argparse
import csv
import hashlib
import json
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "rewardflow"))
from collect_growth import LEDGER_FIELDS, NODE_FIELDS, TX_FIELDS, growth_rows  # noqa: E402
from collect_rewardflow import RETRY_ERRORS, Rpc, RpcError  # noqa: E402

RETRY = RETRY_ERRORS | {"lgrNotFound", "internal"}
METHODS = ("server_info", "ledger")
LED_CMP = [f for f in LEDGER_FIELDS if f not in ("source", "fetched_utc")]


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def norm(row, fields):
    return tuple(str(row[f]) for f in fields)


def rpc_range(rpc, hi):
    """(first, last) of the public RPC's complete_ledgers range that contains `hi` (or the newest)."""
    spans = []
    for part in str(rpc.call("server_info", {})["info"].get("complete_ledgers", "")).split(","):
        a, _, b = part.strip().partition("-")
        if a.isdigit():
            spans.append((int(a), int(b or a)))
    if not spans:
        raise RpcError("server_info: no complete_ledgers")
    return next((sp for sp in spans if sp[0] <= hi <= sp[1]), max(spans))


def raw_sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recount(rpc, idx, join_retries, out):
    """Public-RPC rows for one ledger, joined like the collector does."""
    base = {"ledger_index": idx, "transactions": True, "expand": True}
    for attempt in range(1, join_retries + 1):
        b = rpc.call("ledger", {**base, "binary": True})["ledger"]
        j = rpc.call("ledger", base)["ledger"]
        try:
            if int(j["ledger_index"]) != idx or not j.get("closed", True):
                raise ValueError(
                    f"ledger {idx}: answer is ledger {j.get('ledger_index')} closed={j.get('closed')}"
                )
            led, txs, nodes = growth_rows(j, b)
        except (ValueError, KeyError) as ex:
            if attempt == join_retries:
                raise RpcError(
                    f"ledger {idx}: public answers still disagree after {attempt} fetches: {ex}"
                ) from ex
            out(f"REFETCH ledger {idx} attempt {attempt}/{join_retries}: {ex}")
            continue
        return {
            "ledger": led,
            "tx": txs,
            "nodes": nodes,
            "raw_sha256": {"binary": raw_sha(b), "json": raw_sha(j)},
        }
    raise AssertionError("unreachable")


def compare(local_led, local_tx, local_nodes, got, negative=False):
    """Field names that differ between the collected rows and a recount (empty list = equal)."""
    want_led = dict(local_led)
    want_tx = {norm(r, TX_FIELDS) for r in local_tx}
    want_nodes = Counter(norm(r, NODE_FIELDS) for r in local_nodes)
    if negative:
        want_led["header_bytes"] = str(int(want_led["header_bytes"]) + 1)
        want_tx.add(("NEGATIVE-CONTROL",) * len(TX_FIELDS))
        want_nodes[("NEGATIVE-CONTROL",) * len(NODE_FIELDS)] += 1
    diff = [f for f in LED_CMP if str(want_led[f]) != str(got["ledger"][f])]
    if want_tx != {norm(r, TX_FIELDS) for r in got["tx"]}:
        diff.append("tx_rows")
    if want_nodes != Counter(norm(r, NODE_FIELDS) for r in got["nodes"]):
        diff.append("node_rows")
    return diff


def sample(ledgers, lo, hi, count, seed):
    rng = random.Random(seed)
    span = [r for r in ledgers if lo <= int(r["ledger_index"]) <= hi]
    busy = [r for r in span if int(r["txn_count"]) > 0]
    pick = rng.sample(busy, min(count // 2, len(busy)))
    chosen = {r["ledger_index"] for r in pick}
    rest = [r for r in span if r["ledger_index"] not in chosen]
    pick += rng.sample(rest, min(count - len(pick), len(rest)))
    return sorted(pick, key=lambda r: int(r["ledger_index"])), len(busy), len(span)


def run(
    data,
    count,
    seed,
    negative=False,
    rpc=None,
    offline=None,
    record=None,
    margin=256,
    join_retries=3,
    out=print,
):
    ledgers = read_csv(os.path.join(data, "ledgers.csv"))
    txs = read_csv(os.path.join(data, "tx.csv"))
    nodes = read_csv(os.path.join(data, "nodes.csv"))
    # a live collector may be appending: a torn last ledgers.csv row has missing fields (its tx/nodes rows,
    # written first, are complete for every whole ledgers.csv row)
    torn = [r for r in ledgers if None in r.values() or None in r]
    ledgers = [r for r in ledgers if not (None in r.values() or None in r)]
    if torn:
        out(f"NOTE skipped {len(torn)} incomplete ledgers.csv row(s) (collector still writing)")
    if not ledgers:
        raise SystemExit(f"no ledgers.csv rows in {data}")
    by_led = {r["ledger_index"]: r for r in ledgers}
    tx_of, nodes_of = {}, {}
    for r in txs:
        tx_of.setdefault(r["ledger_index"], []).append(r)
    for r in nodes:
        nodes_of.setdefault(r["ledger_index"], []).append(r)
    lo = min(int(r["ledger_index"]) for r in ledgers)
    hi = max(int(r["ledger_index"]) for r in ledgers)
    res = {"ok": 0, "mismatch": 0}

    if offline:
        with open(offline) as fh:
            rec = json.load(fh)
        out(
            f"OFFLINE {os.path.basename(offline)}: {len(rec['ledgers'])} recounts "
            f"recorded {rec['recorded_utc']} "
            f"(seed {rec['seed']}, checked {rec['range']['checked']}), no RPC"
        )
        items = [(k, rec["ledgers"][k]) for k in sorted(rec["ledgers"], key=int)]
    else:
        r_lo, r_hi = rpc_range(rpc, hi)
        c_lo, c_hi = max(lo, r_lo + margin), min(hi, r_hi)
        if c_lo > c_hi:
            raise RpcError(f"no overlap: collection {lo}-{hi}, public RPC {r_lo}-{r_hi}, margin {margin}")
        pick, n_busy, n_span = sample(ledgers, c_lo, c_hi, count, seed)
        n_out = len(ledgers) - n_span
        out(
            f"RANGE collection {lo}-{hi} public_rpc {r_lo}-{r_hi} margin {margin} checked {c_lo}-{c_hi} "
            f"({n_span} ledgers, {n_busy} with tx) NOT CHECKABLE {n_out} ledgers"
        )
        out(
            f"SAMPLE seed {seed}: {len(pick)} ledgers "
            f"({sum(1 for r in pick if int(r['txn_count']) > 0)} with tx): "
            + " ".join(r["ledger_index"] for r in pick)
        )
        items = []
        for r in pick:
            items.append((r["ledger_index"], recount(rpc, int(r["ledger_index"]), join_retries, out)))
        rec = {
            "recorded_utc": None,
            "seed": seed,
            "range": {"collection": [lo, hi], "rpc": [r_lo, r_hi], "checked": [c_lo, c_hi]},
            "ledgers": {k: v for k, v in items},
        }

    for k, got in items:
        if k not in by_led:
            res["mismatch"] += 1
            out(f"MISMATCH {k} not in ledgers.csv")
            continue
        led = {f: by_led[k][f] for f in LED_CMP}
        diff = compare(led, tx_of.get(k, []), nodes_of.get(k, []), got, negative)
        res["ok" if not diff else "mismatch"] += 1
        out(
            f"{'OK      ' if not diff else 'MISMATCH'} {k} txn_count={got['ledger']['txn_count']} "
            f"header={got['ledger']['header_bytes']} blob={got['ledger']['tx_blob_bytes']} "
            f"meta={got['ledger']['meta_bytes']} nodes={len(got['nodes'])}"
            + (f" differ={','.join(diff)}" if diff else "")
        )

    if not offline:
        r_lo2, _ = rpc_range(rpc, hi)
        out(f"RANGE end: public_rpc oldest {r_lo2} (start {r_lo}), checked span starts {c_lo}")
        if r_lo2 > c_lo:
            raise RpcError(f"public RPC pruned into the checked span during the run ({r_lo2} > {c_lo})")
        if record:
            from datetime import datetime, timezone

            rec["recorded_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(record, "w") as fh:
                json.dump(rec, fh, indent=1, sort_keys=True)
                fh.write("\n")
            out(f"RECORD {len(items)} recounts -> {record}")
    return res


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="data/growth/tx")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--seed", default="1")
    p.add_argument("--negative", action="store_true")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5, help="seconds between public RPC requests")
    p.add_argument("--margin", type=int, default=256, help="ledgers kept clear of the RPC's oldest ledger")
    p.add_argument("--join-retries", type=int, default=3)
    p.add_argument("--record", default=None, help="write the recounts to this JSON file")
    p.add_argument("--offline", default=None, help="compare a --record file with the CSVs, no RPC")
    a = p.parse_args(argv)
    rpc = None if a.offline else Rpc(a.rpc, gap=a.gap, retry_errors=RETRY, methods=METHODS)
    try:
        res = run(
            a.data,
            a.count,
            a.seed,
            a.negative,
            rpc=rpc,
            offline=a.offline,
            record=a.record,
            margin=a.margin,
            join_retries=a.join_retries,
        )
    except RpcError as e:
        print(f"RPC FAILURE {e}", flush=True)
        return 2
    calls = (
        f"rpc_calls={rpc.calls} retries={rpc.retried} "
        f"retries_by_error={dict(sorted(rpc.retry_kinds.items()))}"
        if rpc
        else "rpc_calls=0 (offline)"
    )
    print(
        f"SUMMARY ok={res['ok']} mismatch={res['mismatch']} negative={a.negative} "
        f"seed={'recorded' if a.offline else a.seed} {calls}",
        flush=True,
    )
    return 1 if res["mismatch"] else 0


if __name__ == "__main__":
    sys.exit(main())
