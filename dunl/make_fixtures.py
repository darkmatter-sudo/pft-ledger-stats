#!/usr/bin/env python3
"""Build the dunl pytest fixtures from a real collector cache (captured public responses).

Writes tests/fixtures/dunl/{modern,legacy}.jsonl.gz: the packed rows of a contiguous set of
rounds (modern: 30-33, bundle layout + sealed convergence; legacy: 2-5, early layout, a FAILED
round and a VL_PUBLISHED_MEMO_FAILED round) with their txs, the live documents and the RPC
history rows. The only edit to captured data: the round-list page is reduced to those rounds
(`total` adjusted) so the fixture is self-contained. Also copies the node snapshots.
Usage: python3 dunl/make_fixtures.py --cache data/dunl/cache.jsonl --nodes dunl/data/nodes
"""

import argparse
import gzip
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect_dunl as C  # noqa: E402

SETS = {"modern": range(30, 34), "legacy": range(2, 6)}


def build(rows, keep, epoch):
    out = {}
    page_key = f"{epoch}/rounds/0"
    page = json.loads(rows[page_key]["body"])
    page["rounds"] = [r for r in page["rounds"] if r["round_number"] in keep]
    page["total"] = len(page["rounds"])
    row = dict(rows[page_key])
    row["body"] = json.dumps(page)
    out[page_key] = row
    txs = set()
    for k, r in rows.items():
        parts = k.split("/")
        if k.startswith(f"{epoch}/") and not k.startswith(f"{epoch}/rounds/"):
            out[k] = r
        elif parts[0] == "round" and int(parts[1]) in keep:
            out[k] = r
            if parts[2] == "convergence" and r["status"] == 200:
                c = json.loads(r["body"])
                txs.add(c.get("anchor_tx_hash"))
                for p in (c.get("report") or {}).get("participants", []):
                    txs.update(p.get(x) for x in ("accepted_commit_tx", "accepted_reveal_tx"))
    for r in page["rounds"]:
        txs.add(r.get("memo_tx_hash"))
    for h in txs:
        if h and f"tx/{h}" in rows:
            out[f"tx/{h}"] = rows[f"tx/{h}"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data/dunl/cache.jsonl")
    ap.add_argument("--nodes", default="dunl/data/nodes")
    ap.add_argument("--to", default="tests/fixtures/dunl")
    args = ap.parse_args()
    rows = C.Cache(args.cache).rows
    epoch = sorted({k.split("/", 1)[0] for k in rows if k.endswith("/rounds/0")})[-1]
    os.makedirs(args.to, exist_ok=True)
    for name, keep in SETS.items():
        sub = build(rows, set(keep), epoch)
        tmp = os.path.join(args.to, f"{name}.src.jsonl")
        with open(tmp, "w", encoding="utf-8") as fh:
            for k in sorted(sub):
                fh.write(json.dumps(sub[k], sort_keys=True, separators=(",", ":")) + "\n")
        C.pack(tmp, os.path.join(args.to, f"{name}.jsonl.gz"))
        os.remove(tmp)
        with gzip.open(os.path.join(args.to, f"{name}.jsonl.gz"), "rt") as fh:
            print(f"{name}: rounds {list(keep)} rows {sum(1 for _ in fh)}")
    nd = os.path.join(args.to, "nodes")
    os.makedirs(nd, exist_ok=True)
    for f in sorted(os.listdir(args.nodes)):
        shutil.copy(os.path.join(args.nodes, f), os.path.join(nd, f))
    print(f"nodes: {sorted(os.listdir(nd))}")


if __name__ == "__main__":
    main()
