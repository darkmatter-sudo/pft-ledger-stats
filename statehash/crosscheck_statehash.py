#!/usr/bin/env python3
"""Public-RPC crosscheck of the statehash/ capture (stdlib only, read-only).

Samples --n captured ledgers (deterministic: seeded with snapshot A's ledger hash, plus the first and last
captured ledger) and asks https://rpc.testnet.postfiat.org/ for each one's header (`ledger`, JSON). For
each sample the public ledger_hash, account_hash, parent_hash and transaction_hash must equal the fields of
our captured binary header. Also re-asks the public RPC for snapshot A's (and B's, when present) header.
At most --rate requests/s (default 0.5); transient failures retry with logged backoff.

Exit 0 only if every sample was answered and matched; mismatches print `FAIL <index> <field>`.

Usage:
  ./crosscheck_statehash.py --cap DIR --a DIR [--b DIR] [--n 64] [--json out.json]
"""

import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_statehash as C  # noqa: E402
import replay_statehash as R  # noqa: E402

cf = C.cf


def own_fields(row):
    h = C.parse_header(bytes.fromhex(row["header"]))
    return {
        "ledger_hash": row["ledger_hash"],
        "account_hash": h["account_hash"],
        "parent_hash": h["parent_hash"],
        "transaction_hash": h["transaction_hash"],
    }


def check(rpc, idx, own):
    try:
        r = rpc.call("ledger", {"ledger_index": int(idx)})
    except (cf.GaveUp, cf.RpcError) as e:
        return {"ledger_index": idx, "ok": False, "error": f"{type(e).__name__}: {e}"}
    led = r.get("ledger", {})
    bad = [k for k, v in own.items() if str(led.get(k, "")).upper() != v]
    if not r.get("validated"):
        bad.append("validated")
    return {"ledger_index": idx, "ok": not bad, "mismatch": bad}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cap", required=True)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b")
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--rate", type=float, default=0.5)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    rows = R.read_jsonl(os.path.join(a.cap, "ledgers.jsonl"))
    by_n = {r["ledger_index"]: r for r in rows}
    ha, _ = R.load_snapshot(a.a)
    idxs = sorted(by_n)
    rng = random.Random(ha["ledger_hash"])
    sample = sorted({idxs[0], idxs[-1], *rng.sample(idxs, min(a.n, len(idxs)))})
    rpc = cf.Rpc(
        C.https_transport(), lambda m: print(cf.utcnow(), m, flush=True), a.rate, 5, 2.0, sleep=time.sleep
    )
    print(
        f"CROSSCHECK source={C.PUBLIC_RPC} captured={len(rows)} "
        f"range={idxs[0]}-{idxs[-1]} samples={len(sample)}"
    )
    results = []
    for idx in sample:
        res = check(rpc, idx, own_fields(by_n[idx]))
        results.append(res)
        if not res["ok"]:
            print(f"FAIL {idx} {res.get('mismatch') or res.get('error')}")
    snaps = [("A", a.a)] + ([("B", a.b)] if a.b else [])
    for tag, d in snaps:
        h, _ = R.load_snapshot(d)
        own = {k: h[k] for k in ("ledger_hash", "account_hash", "parent_hash", "transaction_hash")}
        res = check(rpc, h["ledger_index"], own)
        res["snapshot"] = tag
        results.append(res)
        print(f"SNAPSHOT {tag} {h['ledger_index']} account_hash={h['account_hash']} public_match={res['ok']}")
        if not res["ok"]:
            print(f"FAIL snapshot {tag} {res.get('mismatch') or res.get('error')}")
    ok = sum(r["ok"] for r in results)
    print(
        f"RESULT {'PASS' if ok == len(results) else 'FAIL'} matched={ok}/{len(results)} "
        f"rpc_calls={rpc.calls} retries={rpc.retried}"
    )
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"source": C.PUBLIC_RPC, "results": results}, fh, sort_keys=True, indent=1)
            fh.write("\n")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
