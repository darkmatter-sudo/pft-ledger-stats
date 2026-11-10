#!/usr/bin/env python3
"""Cross-check an objlife snapshot against an independent RPC (default: the public testnet RPC).

Read-only (server_info, ledger, ledger_data, ledger_entry, tx). For snapshot ledger L it checks:

  header    `ledger L`: ledger_hash, account_hash, close_time equal the snapshot header
  set:<T>   for every deferred type T: `ledger_data type=<rpc> ledger_index=L` (paged) returns
            exactly the snapshot's index set for T (an absent or extra object is a MISMATCH,
            and a type with 0 objects is checked too)
  obj:<id>  every deferred object: `ledger_entry index=<id> ledger_index=L` equals the snapshot
            object field for field
  absent:k  --absent K indexes that are not in the snapshot (sha256 of seed + k): the source must
            answer entryNotFound

complete_ledgers: server_info is read once; if L is outside the source's range the run stops
with exit 2 and sends nothing else.

Retries: timeouts, HTTP 429/5xx and internal/tooBusy/slowDown are retried with exponential
backoff (`--retries`, `--backoff`); lgrNotFound inside the advertised range too. Every retry is
logged as a RETRY line. A call with no answer after the retries makes that check GAVE_UP (exit 2).

--negative alters every expectation (header hash, close_time+1; one extra fake index per type so
even an empty type fails; one field of every object changed; absent probes expected present).
A working check reports every line MISMATCH and exits 1.

--record F writes every final answer; --offline F replays them with no network and gives the
same verdicts. A call missing from the recording stops the run with exit 3.

Sampled tx mode (--txs ANALYSIS.json --txdir DIR): the tx hashes analyze_objlife.py sampled
(fixed seed) are fetched with `tx` and compared with the watcher's local copy in DIR/obj-tx.jsonl:
ledger_index, validated, TransactionIndex, TransactionResult and the full AffectedNodes list.
A tx whose ledger is outside the source's complete_ledgers is NOT_CHECKABLE (listed, never a
MATCH); if none is checkable the run exits 2. --negative expects TransactionIndex + 1 and
ledger_index + 1, so every tx line must MISMATCH (a txnNotFound is a MISMATCH in both modes).

Window ledger mode (--ledgers DIR --types types.json --from A --to B): checks the watcher's
claim about ledgers A+1..B, including the ones where it kept nothing. From DIR/ledgers.csv it picks
--sample K ledgers that had transactions and --empty E that had none (sha256 of seed + ledger
order, so the pick is fixed by --seed), fetches each with `ledger transactions=true expand=true`
and compares ledger_hash, close_time, txn_count and the sorted hashes of the txs that touch a
deferred type (from types.json) with ledgers.csv and DIR/obj-tx.jsonl. A ledger outside the
source's complete_ledgers is NOT_CHECKABLE; none checkable = exit 2. --negative expects
txn_count + 1 and close_time + 1, so every ledger line must MISMATCH.

Exit 0 all MATCH, 1 any MISMATCH, 2 out of range / gave up, 3 offline recording incomplete.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dormancy"))

import headers_dormancy as H  # noqa: E402

READ_ONLY = {"server_info", "ledger", "ledger_data", "ledger_entry", "tx"}


class Recorder:
    def __init__(self, rpc, path):
        self.rpc, self.fh = rpc, open(path, "w")

    def call(self, method, params=None):
        try:
            res = self.rpc.call(method, params)
        except LookupError as e:
            self._write(method, params, {"error": str(e)})
            raise
        except H.Transient as e:
            self._write(method, params, {"transient": str(e)})
            raise
        self._write(method, params, {"result": res})
        return res

    def _write(self, method, params, ans):
        self.fh.write(json.dumps({"method": method, "params": params or {}, **ans}, sort_keys=True) + "\n")
        self.fh.flush()


class NotRecorded(RuntimeError):
    """A call missing from the --offline recording (never read as a node answer)."""


class Replay:
    """Answers from a --record file. For a repeated call (retries) the LAST recorded answer wins."""

    def __init__(self, path):
        self.answers = {}
        with open(path) as fh:
            for line in fh:
                row = json.loads(line)
                self.answers[self.key(row["method"], row["params"])] = row

    @staticmethod
    def key(method, params):
        return json.dumps([method, params or {}], sort_keys=True)

    def call(self, method, params=None):
        if method not in READ_ONLY:
            raise ValueError(f"refusing non read-only method {method}")
        row = self.answers.get(self.key(method, params))
        if row is None:
            raise NotRecorded(f"not in recording: {method} {params}")
        if "error" in row:
            raise LookupError(row["error"])
        if "transient" in row:
            raise H.Transient(row["transient"])
        return row["result"]


class GaveUp(Exception):
    pass


def ask(rpc, method, params, args, out, retry_errors=("lgrNotFound",), sleep=time.sleep):
    """One call with logged exponential backoff. Raises GaveUp after --retries."""
    for attempt in range(args.retries + 1):
        try:
            try:
                return rpc.call(method, params)
            except LookupError as e:
                if str(e) in retry_errors:
                    raise H.Transient(str(e)) from e
                raise
        except H.Transient as e:
            if attempt == args.retries:
                out(f"GAVE_UP {method} {json.dumps(params, sort_keys=True)} after {attempt + 1} tries: {e}")
                raise GaveUp(str(e)) from e
            delay = args.backoff * (2**attempt)
            out(f"RETRY {method} try {attempt + 1}/{args.retries + 1} error={e} sleep={delay:g}s")
            sleep(delay)
    raise AssertionError("unreachable")


def load_snapshot(path):
    with open(path) as fh:
        lines = [json.loads(x) for x in fh]
    header, rows = lines[0], lines[1:]
    if len(rows) != header["deferred_rows"]:
        raise ValueError(f"{path}: {len(rows)} rows != header deferred_rows {header['deferred_rows']}")
    return header, rows


def absent_ids(seed, k, present):
    out, i = [], 0
    while len(out) < k:
        h = hashlib.sha256(f"objlife-absent:{seed}:{i}".encode()).hexdigest().upper()
        if h not in present:
            out.append(h)
        i += 1
    return out


def flip_hex(h):
    return h[:-1] + ("0" if h[-1] != "0" else "1")


def altered(obj):
    """Same object with one field changed (Flags+1, which every ledger entry has)."""
    o = dict(obj)
    o["Flags"] = int(o.get("Flags", 0)) + 1
    return o


def run(rpc, snap_path, args, out=print, sleep=time.sleep):
    header, rows = load_snapshot(snap_path)
    L = header["ledger_index"]
    try:
        info = ask(rpc, "server_info", {}, args, out, sleep=sleep)
    except GaveUp:
        return 2
    rng = info["info"].get("complete_ledgers", "empty")
    ranges = H.parse_ranges(rng)
    if not any(lo <= L <= hi for lo, hi in ranges):
        out(f"OUT_OF_RANGE ledger {L} not in source complete_ledgers {rng}")
        return 2
    out(f"SOURCE complete_ledgers {rng} | ledger {L} in range | negative={args.negative}")
    neg = args.negative
    results = []

    def verdict(name, ok, detail=""):
        results.append(ok)
        out(f"{'MATCH' if ok else 'MISMATCH'} {name}{(' ' + detail) if detail else ''}")

    gave_up = 0
    try:
        hdr = ask(rpc, "ledger", {"ledger_index": L}, args, out, sleep=sleep)["ledger"]
        exp = {
            "ledger_hash": header["ledger_hash"],
            "account_hash": header["account_hash"],
            "close_time": header["close_time"],
        }
        if neg:
            exp = {
                "ledger_hash": flip_hex(exp["ledger_hash"]),
                "account_hash": flip_hex(exp["account_hash"]),
                "close_time": exp["close_time"] + 1,
            }
        got = {
            "ledger_hash": hdr["ledger_hash"],
            "account_hash": hdr["account_hash"],
            "close_time": int(hdr["close_time"]),
        }
        for k in exp:
            verdict(f"header.{k}", exp[k] == got[k], f"expected={exp[k]} got={got[k]}")
    except GaveUp:
        gave_up += 1

    present = {o["index"] for o in rows}
    for name, t in sorted(header["deferred"].items()):
        expected = sorted(o["index"] for o in rows if o["LedgerEntryType"] == name)
        if neg:
            expected = sorted(expected + absent_ids(f"neg-set-{name}", 1, present))
        got, marker = [], None
        try:
            while True:
                params = {"ledger_index": L, "type": t["rpc"], "binary": False, "limit": 400}
                if marker:
                    params["marker"] = marker
                r = ask(rpc, "ledger_data", params, args, out, sleep=sleep)
                if r["ledger_hash"] != header["ledger_hash"] and not neg:
                    verdict(f"set:{name}", False, f"source ledger_hash {r['ledger_hash']}")
                    break
                got.extend(o["index"] for o in r["state"])
                marker = r.get("marker")
                if not marker:
                    got.sort()
                    missing, extra = sorted(set(expected) - set(got)), sorted(set(got) - set(expected))
                    verdict(
                        f"set:{name}",
                        got == expected,
                        f"expected={len(expected)} got={len(got)} missing={len(missing)} extra={len(extra)}",
                    )
                    break
        except GaveUp:
            gave_up += 1

    for o in rows:
        exp = altered(o) if neg else o
        try:
            r = ask(rpc, "ledger_entry", {"index": o["index"], "ledger_index": L}, args, out, sleep=sleep)
        except GaveUp:
            gave_up += 1
            continue
        except LookupError as e:
            verdict(f"obj:{o['index']}", False, f"{o['LedgerEntryType']} error={e}")
            continue
        node = r["node"]
        diff = sorted(k for k in set(exp) | set(node) if exp.get(k) != node.get(k))
        verdict(f"obj:{o['index']}", not diff, f"{o['LedgerEntryType']} fields={len(exp)} diff={diff}")

    for k, idx in enumerate(absent_ids(args.seed, args.absent, present)):
        try:
            ask(rpc, "ledger_entry", {"index": idx, "ledger_index": L}, args, out, sleep=sleep)
            found = True
        except GaveUp:
            gave_up += 1
            continue
        except LookupError as e:
            if str(e) != "entryNotFound":
                verdict(f"absent:{k}", False, f"{idx} error={e}")
                continue
            found = False
        verdict(
            f"absent:{k}",
            found == neg,
            f"{idx} expected={'present' if neg else 'entryNotFound'} "
            f"got={'present' if found else 'entryNotFound'}",
        )

    n_bad = results.count(False)
    out(f"SUMMARY checks={len(results)} match={results.count(True)} mismatch={n_bad} gave_up={gave_up}")
    if n_bad:
        return 1
    return 2 if gave_up else 0


def run_txs(rpc, analysis_path, txdir, args, out=print, sleep=time.sleep):
    with open(analysis_path) as fh:
        hashes = json.load(fh)["sampled_txs"]["hashes"]
    local = {}
    with open(os.path.join(txdir, "obj-tx.jsonl")) as fh:
        for line in fh:
            x = json.loads(line)
            if x["tx"]["hash"] in hashes:
                local[x["tx"]["hash"]] = x
    missing = sorted(set(hashes) - set(local))
    if missing:
        out(f"LOCAL_MISSING {len(missing)} sampled hashes not in {txdir}/obj-tx.jsonl: {missing[:3]}")
        return 2
    try:
        info = ask(rpc, "server_info", {}, args, out, sleep=sleep)
    except GaveUp:
        return 2
    rng = info["info"].get("complete_ledgers", "empty")
    ranges = H.parse_ranges(rng)
    out(f"SOURCE complete_ledgers {rng} | sampled txs {len(hashes)} | negative={args.negative}")
    results, gave_up, uncheckable = [], 0, 0
    for h in hashes:
        x = local[h]
        if not any(lo <= x["ledger_index"] <= hi for lo, hi in ranges):
            uncheckable += 1
            out(f"NOT_CHECKABLE tx:{h} ledger {x['ledger_index']} outside source complete_ledgers")
            continue
        exp = {
            "ledger_index": x["ledger_index"] + (1 if args.negative else 0),
            "validated": True,
            "TransactionIndex": x["meta"]["TransactionIndex"] + (1 if args.negative else 0),
            "TransactionResult": x["meta"].get("TransactionResult"),
            "AffectedNodes": x["meta"]["AffectedNodes"],
        }
        try:
            r = ask(rpc, "tx", {"transaction": h, "binary": False}, args, out, sleep=sleep)
        except GaveUp:
            gave_up += 1
            continue
        except LookupError as e:
            results.append(False)
            out(f"MISMATCH tx:{h} error={e}")
            continue
        meta = r.get("meta") or {}
        got = {
            "ledger_index": int(r.get("ledger_index", -1)),
            "validated": r.get("validated") is True,
            "TransactionIndex": meta.get("TransactionIndex"),
            "TransactionResult": meta.get("TransactionResult"),
            "AffectedNodes": meta.get("AffectedNodes"),
        }
        diff = sorted(k for k in exp if exp[k] != got[k])
        results.append(not diff)
        out(
            f"{'MISMATCH' if diff else 'MATCH'} tx:{h} ledger {x['ledger_index']} "
            f"nodes={len(exp['AffectedNodes'])} diff={diff}"
        )
    n_bad = results.count(False)
    out(
        f"SUMMARY txs={len(hashes)} checked={len(results)} match={results.count(True)} mismatch={n_bad} "
        f"not_checkable={uncheckable} gave_up={gave_up}"
    )
    if n_bad:
        return 1
    return 2 if gave_up or not results else 0


def pick(rows, k, seed):
    """k rows ordered by sha256(seed:ledger_index) (fixed for a seed), returned in ledger order."""
    key = lambda r: hashlib.sha256(f"{seed}:{r['ledger_index']}".encode()).hexdigest()  # noqa: E731
    return sorted(sorted(rows, key=key)[:k], key=lambda r: int(r["ledger_index"]))


def deferred_hashes(txs, deferred):
    out = []
    for tx in txs:
        meta = tx.get("metaData") or tx.get("meta") or {}
        nodes = [v for n in meta.get("AffectedNodes", []) for v in n.values()]
        if any(v.get("LedgerEntryType") in deferred for v in nodes):
            out.append(tx["hash"])
    return sorted(out)


def run_ledgers(rpc, txdir, types_path, lo_seq, hi_seq, args, out=print, sleep=time.sleep):
    with open(types_path) as fh:
        deferred = {e["name"] for e in json.load(fh)["entries"] if e["deferred"]}
    rows = {}
    with open(os.path.join(txdir, "ledgers.csv")) as fh:
        for r in csv.DictReader(fh):
            if lo_seq < int(r["ledger_index"]) <= hi_seq:
                rows[int(r["ledger_index"])] = r
    if sorted(rows) != list(range(lo_seq + 1, hi_seq + 1)):
        n = hi_seq - lo_seq
        out(f"LOCAL_INCOMPLETE ledgers.csv has {len(rows)} of {n} ledgers in {lo_seq + 1}-{hi_seq}")
        return 2
    local = {}
    with open(os.path.join(txdir, "obj-tx.jsonl")) as fh:
        for line in fh:
            x = json.loads(line)
            local.setdefault(x["ledger_index"], []).append(x["tx"]["hash"])
    busy = [r for r in rows.values() if int(r["txn_count"]) > 0]
    idle = [r for r in rows.values() if int(r["txn_count"]) == 0]
    chosen = sorted(pick(busy, args.sample, args.seed) + pick(idle, args.empty, args.seed),
                    key=lambda r: int(r["ledger_index"]))  # fmt: skip
    try:
        info = ask(rpc, "server_info", {}, args, out, sleep=sleep)
    except GaveUp:
        return 2
    rng = info["info"].get("complete_ledgers", "empty")
    ranges = H.parse_ranges(rng)
    out(
        f"SOURCE complete_ledgers {rng} | window {lo_seq + 1}-{hi_seq} ({len(rows)} ledgers, "
        f"{len(busy)} with txs) | sampled {len(chosen)} | negative={args.negative}"
    )
    results, gave_up, uncheckable = [], 0, 0
    for r in chosen:
        seq = int(r["ledger_index"])
        if not any(lo <= seq <= hi for lo, hi in ranges):
            uncheckable += 1
            out(f"NOT_CHECKABLE ledger:{seq} outside source complete_ledgers")
            continue
        exp = {
            "ledger_hash": r["ledger_hash"],
            "close_time": int(r["close_time"]) + (1 if args.negative else 0),
            "txn_count": int(r["txn_count"]) + (1 if args.negative else 0),
            "deferred_txs": sorted(local.get(seq, [])),
        }
        try:
            led = ask(rpc, "ledger", {"ledger_index": seq, "transactions": True, "expand": True}, args, out,
                      sleep=sleep)["ledger"]  # fmt: skip
        except GaveUp:
            gave_up += 1
            continue
        txs = led.get("transactions") or []
        got = {
            "ledger_hash": led["ledger_hash"],
            "close_time": int(led["close_time"]),
            "txn_count": len(txs),
            "deferred_txs": deferred_hashes(txs, deferred),
        }
        diff = sorted(k for k in exp if exp[k] != got[k])
        results.append(not diff)
        out(
            f"{'MISMATCH' if diff else 'MATCH'} ledger:{seq} txs={got['txn_count']} "
            f"deferred={len(got['deferred_txs'])} diff={diff}"
        )
    n_bad = results.count(False)
    out(
        f"SUMMARY ledgers={len(chosen)} checked={len(results)} match={results.count(True)} mismatch={n_bad} "
        f"not_checkable={uncheckable} gave_up={gave_up}"
    )
    if n_bad:
        return 1
    return 2 if gave_up or len(results) == 0 else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot", nargs="?", help="objlife-<ledger>.jsonl (not with --txs)")
    p.add_argument("--txs", default=None, help="analysis.json: check its sampled tx hashes instead")
    p.add_argument("--txdir", default=None, help="watch_objlife.py directory (with --txs)")
    p.add_argument("--ledgers", default=None, help="watch_objlife.py directory: check sampled window ledgers")
    p.add_argument("--types", default=None, help="types.json (with --ledgers)")
    p.add_argument("--from", dest="from_seq", type=int, default=None, help="ledger A (window is A+1..B)")
    p.add_argument("--to", dest="to_seq", type=int, default=None, help="ledger B")
    p.add_argument("--sample", type=int, default=160, help="window ledgers with txs to check (--ledgers)")
    p.add_argument("--empty", type=int, default=40, help="window ledgers without txs to check (--ledgers)")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--retries", type=int, default=5)
    p.add_argument("--backoff", type=float, default=1.0)
    p.add_argument("--gap", type=float, default=0.25, help="min seconds between requests")
    p.add_argument("--absent", type=int, default=5)
    p.add_argument("--seed", default="1")
    p.add_argument("--negative", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--record", default=None)
    g.add_argument("--offline", default=None)
    args = p.parse_args(argv)
    if args.ledgers:
        if args.snapshot or args.txs or None in (args.types, args.from_seq, args.to_seq):
            p.error("--ledgers needs --types, --from and --to, and no snapshot or --txs")
    elif bool(args.snapshot) == bool(args.txs) or bool(args.txs) != bool(args.txdir):
        p.error("give a snapshot, or --txs with --txdir, or --ledgers")
    if args.offline:
        rpc = Replay(args.offline)
    else:
        rpc = H.HttpRpc(args.rpc, gap=args.gap, allow=READ_ONLY)
        if args.record:
            rpc = Recorder(rpc, args.record)
    try:
        sleep = (lambda s: None) if args.offline else time.sleep
        if args.ledgers:
            return run_ledgers(rpc, args.ledgers, args.types, args.from_seq, args.to_seq, args, sleep=sleep)
        if args.txs:
            return run_txs(rpc, args.txs, args.txdir, args, sleep=sleep)
        return run(rpc, args.snapshot, args, sleep=sleep)
    except NotRecorded as e:
        print(f"OFFLINE_INCOMPLETE {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
