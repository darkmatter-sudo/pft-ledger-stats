#!/usr/bin/env python3
"""Cross-check a dormancy snapshot's last-activity keys against an independent RPC (stdlib only).

Picks --count random AccountRoots (seeded) and asks the source for each one with
`account_info ledger_index=<snapshot ledger>`. It compares Balance, OwnerCount, Sequence,
PreviousTxnID and PreviousTxnLgrSeq with the snapshot. For every sampled account whose
PreviousTxnLgrSeq the source still holds, it also asks `tx <PreviousTxnID>` and checks that
the transaction is validated in exactly that ledger and that its metadata touches the account's
AccountRoot. If the time map has an exact header for that ledger, it also checks that the
transaction's `date` equals the header's close time.

complete_ledgers limits: server_info is read once. If the snapshot ledger is outside the
source's range, the run stops with exit 2 (nothing can be checked). A `tx` check is done only
when PreviousTxnLgrSeq is inside the range; the rest are counted as `tx_out_of_range`.
Transient errors (timeouts, HTTP 429/5xx, internal/tooBusy, and lgrNotFound / txnNotFound inside
the advertised range: the public RPC sometimes answers those and succeeds seconds later) are
retried with exponential backoff (--retries, base --backoff s). An account that still has no
answer after the retries is skipped and replaced by another random account.

--in-range-only samples only accounts whose PreviousTxnLgrSeq the source still holds, so that
every sampled account also gets the tx check (on a pruned source most last-activity ledgers are
older than its oldest ledger).

--negative is the control: every expected value is altered (Balance/OwnerCount/Sequence/
PreviousTxnLgrSeq/date +1, last hex digit of PreviousTxnID changed, and the AccountRoot is
expected to be ABSENT from the tx metadata). A working check must report every compared field
of every account as a MISMATCH and exit 1.

--record F writes every final RPC answer as one JSON line. --offline F re-runs the same check
from such a file with no network access (same seed, same sample, same verdicts).

Only read-only methods are sent: server_info, account_info, tx. Accounts are printed by address
(public ledger data). Exit 0 all match, 1 any mismatch, 2 not enough accounts checked.

Usage:
  ./crosscheck_dormancy.py snap.jsonl.gz --headers data/dormancy/headers.jsonl --count 200 --seed 1 \\
      [--negative] [--record F | --offline F]
"""

import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import headers_dormancy as H  # noqa: E402
import lib_dormancy as L  # noqa: E402

READ_ONLY = {"server_info", "account_info", "tx"}
RETRY_ERRORS = {"lgrNotFound", "txnNotFound"}  # inside the advertised range only
FIELDS = ["Balance", "OwnerCount", "Sequence", "PreviousTxnID", "PreviousTxnLgrSeq"]


class Recorder:
    """Wraps a live rpc; writes every final answer (result or error) to a JSON-lines file."""

    def __init__(self, rpc, path):
        self.rpc, self.fh = rpc, open(path, "w")

    @property
    def count(self):
        return self.rpc.count

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
    """A call missing from the --offline recording (NOT a LookupError: never read as a node answer)."""


class Replay:
    """Answers calls from a --record file; a call that was never recorded is an error."""

    def __init__(self, path):
        self.answers, self.count = {}, 0
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
        self.count += 1
        row = self.answers.get(self.key(method, params))
        if row is None:
            raise NotRecorded(f"not in recording: {method} {params}")
        if "error" in row:
            raise LookupError(row["error"])
        if "transient" in row:
            raise H.Transient(row["transient"])
        return row["result"]


def ask(rpc, method, params, retries, backoff, stats, sleep=time.sleep):
    """One call with backoff; lgrNotFound/txnNotFound count as transient here. None = gave up."""

    def once():
        try:
            return rpc.call(method, params)
        except LookupError as e:
            if str(e) in RETRY_ERRORS:
                raise H.Transient(str(e)) from e
            raise

    try:
        return H.with_retry(once, retries, backoff, stats, sleep=sleep)
    except H.Transient:
        return None


def touched_roots(meta):
    """AccountRoot object indexes in a tx's AffectedNodes."""
    out = set()
    for node in meta.get("AffectedNodes", []):
        (inner,) = node.values()
        if inner.get("LedgerEntryType") == "AccountRoot":
            out.add(inner.get("LedgerIndex"))
    return out


def flip_hex(h):
    return h[:-1] + ("0" if h[-1] != "0" else "1")


def expected_for(obj, negative):
    exp = {
        "Balance": int(obj["Balance"]),
        "OwnerCount": int(obj.get("OwnerCount", 0)),
        "Sequence": int(obj["Sequence"]),
        "PreviousTxnID": obj["PreviousTxnID"],
        "PreviousTxnLgrSeq": int(obj["PreviousTxnLgrSeq"]),
    }
    if negative:
        exp = {k: (flip_hex(v) if k == "PreviousTxnID" else v + 1) for k, v in exp.items()}
    return exp


def got_from(acct):
    return {
        "Balance": int(acct["Balance"]),
        "OwnerCount": int(acct.get("OwnerCount", 0)),
        "Sequence": int(acct["Sequence"]),
        "PreviousTxnID": acct["PreviousTxnID"],
        "PreviousTxnLgrSeq": int(acct["PreviousTxnLgrSeq"]),
    }


def run(
    snapshot,
    headers_path,
    rpc,
    count,
    seed,
    negative,
    out,
    retries=4,
    backoff=1.0,
    sleep=time.sleep,
    in_range_only=False,
):
    header, objs = L.load_snapshot(snapshot)
    seq_snap = int(header["ledger_index"])
    exact = {s: t for s, (t, _h) in L.load_headers_jsonl(headers_path).items()} if headers_path else {}
    stats = {}

    info = ask(rpc, "server_info", {}, retries, backoff, stats, sleep=sleep)
    if info is None:
        print("server_info: no answer", file=out)
        return 2, {}
    ranges = H.parse_ranges(info["info"].get("complete_ledgers", "empty"))
    print(f"source complete_ledgers {ranges}", file=out)
    print(
        f"snapshot ledger {seq_snap} {header['ledger_hash']} mode {'NEGATIVE' if negative else 'positive'}"
        f" seed {seed} count {count}",
        file=out,
    )
    if not H.in_ranges(seq_snap, ranges):
        print(f"snapshot ledger {seq_snap} outside source complete_ledgers: cannot check", file=out)
        return 2, {"out_of_range": True}

    roots = sorted((o for o in objs if o["LedgerEntryType"] == "AccountRoot"), key=lambda o: o["index"])
    if in_range_only:  # sample only accounts whose last tx the source still holds (tx check on each)
        roots = [o for o in roots if H.in_ranges(int(o["PreviousTxnLgrSeq"]), ranges)]
        print(f"--in-range-only: {len(roots)} AccountRoots with PreviousTxnLgrSeq inside the range", file=out)
    order = list(range(len(roots)))
    random.Random(seed).shuffle(order)

    c = {
        "accounts": 0,
        "skipped": 0,
        "fields_ok": 0,
        "fields_mismatch": 0,
        "tx_checked": 0,
        "tx_out_of_range": 0,
        "tx_ok": 0,
        "tx_mismatch": 0,
        "tx_skipped": 0,
        "date_checked": 0,
    }
    print(" #   account                             field              expected -> got   result", file=out)
    for i in order:
        if c["accounts"] >= count:
            break
        obj = roots[i]
        params = {"account": obj["Account"], "ledger_index": seq_snap}
        try:
            ans = ask(rpc, "account_info", params, retries, backoff, stats, sleep=sleep)
        except LookupError as e:  # e.g. actNotFound: the account is in the snapshot at this ledger
            ans = {"error": str(e)}
        if ans is None:
            c["skipped"] += 1
            print(
                f"    {obj['Account']} account_info gave no answer after retries: SKIP (replaced)", file=out
            )
            continue
        c["accounts"] += 1
        n = c["accounts"]
        exp = expected_for(obj, negative)
        got = got_from(ans["account_data"]) if "account_data" in ans else {}
        bad = [f for f in FIELDS if exp[f] != got.get(f)]
        c["fields_ok"] += len(FIELDS) - len(bad)
        c["fields_mismatch"] += len(bad)
        line = "MATCH" if not bad else "MISMATCH " + ",".join(bad)
        if "error" in ans:
            line += f" ({ans['error']})"
        print(f"{n:3d} {obj['Account']:<35} acct  {len(FIELDS)} fields {line}", file=out)

        prev = int(obj["PreviousTxnLgrSeq"])
        if not H.in_ranges(prev, ranges):
            c["tx_out_of_range"] += 1
            continue
        try:
            tx = ask(rpc, "tx", {"transaction": obj["PreviousTxnID"]}, retries, backoff, stats, sleep=sleep)
        except LookupError as e:
            tx = {"error": str(e)}
        if tx is None:
            c["tx_skipped"] += 1
            print(f"{n:3d} {obj['Account']:<35} tx    no answer after retries: SKIP", file=out)
            continue
        c["tx_checked"] += 1
        checks = {
            "validated": (not negative, bool(tx.get("validated"))),
            "ledger_index": (prev + (1 if negative else 0), int(tx.get("ledger_index", -1))),
            "touches_root": (not negative, obj["index"] in touched_roots(tx.get("meta", {}))),
        }
        if prev in exact:
            c["date_checked"] += 1
            checks["date"] = (exact[prev] + (1 if negative else 0), int(tx.get("date", -1)))
        tbad = [k for k, (e, g) in checks.items() if e != g]
        c["tx_ok" if not tbad else "tx_mismatch"] += 1
        detail = " ".join(f"{k}={g}" for k, (_e, g) in checks.items())
        verdict = "MATCH" if not tbad else "MISMATCH " + ",".join(tbad)
        print(
            f"{n:3d} {obj['Account']:<35} tx    {detail} {verdict}",
            file=out,
        )

    c["retries"] = stats.get("retries", 0)
    c["retries_by_error"] = stats.get("retries_by_error", {})
    c["rpc_calls"] = rpc.count
    print("SUMMARY " + json.dumps(c, sort_keys=True), file=out)
    if c["accounts"] < count:
        print(f"RESULT: only {c['accounts']}/{count} accounts checked -> exit 2", file=out)
        return 2, c
    mism = c["fields_mismatch"] + c["tx_mismatch"]
    if negative:
        want = c["accounts"] * len(FIELDS)
        ok = c["fields_mismatch"] == want and c["tx_mismatch"] == c["tx_checked"]
        print(
            f"RESULT: negative control {'PASS' if ok else 'FAIL'}:"
            f" field mismatches {c['fields_mismatch']}/{want},"
            f" tx mismatches {c['tx_mismatch']}/{c['tx_checked']}, OK lines {c['fields_ok'] + c['tx_ok']}"
            f" -> exit {1 if mism else 0}",
            file=out,
        )
    else:
        print(
            f"RESULT: {'all match' if not mism else f'{mism} mismatches'} -> exit {1 if mism else 0}",
            file=out,
        )
    return (1 if mism else 0), c


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot")
    p.add_argument("--headers", help="headers.jsonl (exact close times for the tx date check)")
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--negative", action="store_true")
    p.add_argument("--in-range-only", action="store_true", help="sample only accounts whose last tx is held")
    p.add_argument("--rpc", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.25, help="min seconds between requests")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--backoff", type=float, default=1.0)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--record", help="write every final RPC answer here (JSON lines)")
    g.add_argument("--offline", help="replay a --record file; no network")
    a = p.parse_args(argv)
    if a.offline:
        rpc = Replay(a.offline)
        a.backoff = 0.0  # replayed retries need no wait
    else:
        rpc = H.HttpRpc(a.rpc, gap=a.gap, allow=READ_ONLY)
        if a.record:
            rpc = Recorder(rpc, a.record)
    mode = "offline " + os.path.basename(a.offline) if a.offline else "live"
    print(f"{H.utcnow()} crosscheck_dormancy {mode}")
    code, _ = run(
        a.snapshot,
        a.headers,
        rpc,
        a.count,
        a.seed,
        a.negative,
        sys.stdout,
        retries=a.retries,
        backoff=a.backoff,
        in_range_only=a.in_range_only,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
