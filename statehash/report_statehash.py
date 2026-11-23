#!/usr/bin/env python3
"""State-tree structure report for statehash/ snapshots (stdlib only, deterministic output).

For each snapshot (A, B) the tree is rebuilt from its blobs (root must equal the header account_hash,
else exit 1) and measured:
  leaves     per LedgerEntryType: object count, serialized bytes (sum, mean, max) and deepest leaf
  inner      inner-node count and fan-out histogram (number of non-empty children, 1..16)
  depth      leaf depth = inner nodes from the root down to the leaf's parent: min / p50 / p99 / max
  proof      per AccountRoot: Merkle proof size = inner nodes on its path (p50/p99/max) and proof bytes
             = 512 per inner node (16 x 32-byte child hashes, the full inner-node form) + blob + 32-byte key
Percentiles are nearest-rank (p = value at rank ceil(p/100 * n) of the sorted list).

Outputs (--out DIR): structure.txt, structure.json, structure-types.csv, structure-fanout.csv. The files
depend only on the snapshot inputs, so `--offline` over the committed data/ re-renders them byte-identically
(compare with `cmp`).

Usage:
  ./report_statehash.py --snap A=DIR --snap B=DIR --out DIR
  ./report_statehash.py --offline statehash/data --out DIR      (= --snap A=data/A --snap B=data/B)
"""

import argparse
import csv
import io
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_statehash as L  # noqa: E402
import replay_statehash as R  # noqa: E402

INNER_BYTES = 16 * 32


def pct(sorted_vals, p):
    if not sorted_vals:
        return None
    return sorted_vals[max(1, math.ceil(p / 100 * len(sorted_vals))) - 1]


def measure(defs, header, items):
    m = L.SHAMap(items)
    root = m.root_hash().hex().upper()
    if root != header["account_hash"]:
        raise SystemExit(
            f"FAIL ROOT ledger={header['ledger_index']} rebuilt={root} header={header['account_hash']}"
        )
    types, fan, depths, proof_nodes, proof_bytes = {}, [0] * 17, [], [], []
    inner = 0
    et_fid = defs.fid("LedgerEntryType")
    acct_code = defs.entry_types["AccountRoot"]
    for node in m.walk():
        if node[0] == "inner":
            inner += 1
            fan[node[2]] += 1
            continue
        _, depth, _key, data = node
        code = L.u16(L.parse(data)[et_fid])
        name = defs.entry_names.get(code, f"?{code}")
        t = types.setdefault(name, {"count": 0, "bytes": 0, "max_bytes": 0, "max_depth": 0})
        t["max_depth"] = max(t["max_depth"], depth)
        t["count"] += 1
        t["bytes"] += len(data)
        t["max_bytes"] = max(t["max_bytes"], len(data))
        depths.append(depth)
        if code == acct_code:
            proof_nodes.append(depth)
            proof_bytes.append(depth * INNER_BYTES + len(data) + 32)
    depths.sort()
    proof_nodes.sort()
    proof_bytes.sort()
    for t in types.values():
        t["mean_bytes"] = round(t["bytes"] / t["count"], 1)
    return {
        "ledger_index": header["ledger_index"],
        "ledger_hash": header["ledger_hash"],
        "account_hash": header["account_hash"],
        "root_rebuilt": root,
        "leaves": len(items),
        "leaf_bytes": sum(len(d) for _, d in items),
        "types": dict(sorted(types.items())),
        "inner_nodes": inner,
        "fanout": {str(i): fan[i] for i in range(1, 17)},
        "depth": {"min": depths[0], "p50": pct(depths, 50), "p99": pct(depths, 99), "max": depths[-1]},
        "proof": {
            "accounts": len(proof_nodes),
            "inner_nodes": {"p50": pct(proof_nodes, 50), "p99": pct(proof_nodes, 99), "max": proof_nodes[-1]},
            "bytes": {"p50": pct(proof_bytes, 50), "p99": pct(proof_bytes, 99), "max": proof_bytes[-1]},
        },
    }


def render_text(rep):
    out = []
    for tag, s in rep["snapshots"].items():
        out.append(f"== snapshot {tag}: ledger {s['ledger_index']} {s['ledger_hash']}")
        out.append(f"root rebuilt {s['root_rebuilt']} == header account_hash {s['account_hash']}")
        out.append(f"leaves {s['leaves']}  leaf bytes {s['leaf_bytes']}  inner nodes {s['inner_nodes']}")
        out.append(f"{'type':<16}{'count':>8}{'bytes':>10}{'mean':>8}{'max':>6}{'depth':>7}")
        for name, t in s["types"].items():
            out.append(
                f"{name:<16}{t['count']:>8}{t['bytes']:>10}{t['mean_bytes']:>8}{t['max_bytes']:>6}{t['max_depth']:>7}"
            )
        out.append(
            "fan-out (children: inner nodes) " + " ".join(f"{k}:{v}" for k, v in s["fanout"].items() if v)
        )
        d = s["depth"]
        out.append(f"leaf depth min {d['min']} p50 {d['p50']} p99 {d['p99']} max {d['max']}")
        p = s["proof"]
        out.append(
            f"AccountRoot proofs ({p['accounts']}): inner nodes p50 {p['inner_nodes']['p50']} "
            f"p99 {p['inner_nodes']['p99']} max {p['inner_nodes']['max']}; bytes p50 {p['bytes']['p50']} "
            f"p99 {p['bytes']['p99']} max {p['bytes']['max']}"
        )
        out.append("")
    return "\n".join(out)


def render_csv(rows, header):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def build(defs, snaps):
    rep = {"definitions": defs.hash, "proof_inner_node_bytes": INNER_BYTES, "snapshots": {}}
    for tag, d in snaps:
        header, items = R.load_snapshot(d)
        rep["snapshots"][tag] = measure(defs, header, items)
    types = [
        [tag, name, t["count"], t["bytes"], t["mean_bytes"], t["max_bytes"], t["max_depth"]]
        for tag, s in rep["snapshots"].items()
        for name, t in s["types"].items()
    ]
    fan = [[tag, k, v] for tag, s in rep["snapshots"].items() for k, v in s["fanout"].items()]
    return {
        "structure.json": json.dumps(rep, sort_keys=True, indent=1) + "\n",
        "structure.txt": render_text(rep),
        "structure-types.csv": render_csv(
            types, ["snapshot", "type", "count", "bytes", "mean_bytes", "max_bytes", "max_depth"]
        ),
        "structure-fanout.csv": render_csv(fan, ["snapshot", "children", "inner_nodes"]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snap", action="append", default=[], help="TAG=DIR (repeat)")
    ap.add_argument("--offline", help="published data/ folder: uses DATA/A and DATA/B")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    snaps = [tuple(s.split("=", 1)) for s in a.snap]
    if a.offline:
        snaps = [
            (t, os.path.join(a.offline, t)) for t in ("A", "B") if os.path.isdir(os.path.join(a.offline, t))
        ]
    if not snaps:
        ap.error("no snapshots given")
    files = build(L.Defs(), snaps)
    os.makedirs(a.out, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"WROTE {name} bytes={len(text.encode())}")
    sys.stdout.write(files["structure.txt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
