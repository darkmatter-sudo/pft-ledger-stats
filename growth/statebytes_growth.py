#!/usr/bin/env python3
"""Exact serialized size of every state object of one validated ledger (read-only).

Pages `ledger_data` with binary=true on your own node's admin JSON-RPC (state/snapshot.py's
docker exec client and read-only allowlist) and records, per object, its index, LedgerEntryType
and serialized byte length (hex length / 2). The type is read from the object's first field
(LedgerEntryType, UInt16 field 1: bytes 0x11 TT TT); codes missing from ENTRY_TYPES are written
as "0xNNNN", never guessed.

--join SNAPSHOT (state/snapshot.py *.jsonl.gz of the SAME ledger) checks the binary page set
against the JSON snapshot: same ledger hash, same index set, and the decoded type equal to the
JSON LedgerEntryType for every object. Any difference is printed and makes the run exit 1, so the
type table is proven on real data, not assumed.

Outputs (in --out):
  statebytes-<L>.csv   index, entry_type, bytes (sorted by index)
  statebytes-<L>.json  ledger index/hash, objects, total bytes, per-type count/bytes, join result
"""

import argparse
import csv
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state"))
from snapshot import Rpc, utcnow  # noqa: E402

# LedgerEntryType codes (rippled ledger_entries.macro; postfiatd keeps the same table).
ENTRY_TYPES = {
    0x0037: "NFTokenOffer",
    0x0043: "Check",
    0x0049: "DID",
    0x004E: "NegativeUNL",
    0x0050: "NFTokenPage",
    0x0053: "SignerList",
    0x0054: "Ticket",
    0x0061: "AccountRoot",
    0x0064: "DirectoryNode",
    0x0066: "Amendments",
    0x0068: "LedgerHashes",
    0x0069: "Bridge",
    0x006F: "Offer",
    0x0070: "DepositPreauth",
    0x0071: "XChainOwnedClaimID",
    0x0072: "RippleState",
    0x0073: "FeeSettings",
    0x0074: "XChainOwnedCreateAccountClaimID",
    0x0075: "Escrow",
    0x0078: "PayChannel",
    0x0079: "AMM",
    0x007E: "MPTokenIssuance",
    0x007F: "MPToken",
    0x0080: "Oracle",
    0x0081: "Credential",
    0x0082: "PermissionedDomain",
}


def entry_type(hexdata):
    """LedgerEntryType from a serialized object: first field must be 0x11 (UInt16, field 1)."""
    if len(hexdata) < 6 or hexdata[:2].upper() != "11":
        raise ValueError(f"object does not start with LedgerEntryType: {hexdata[:6]!r}")
    code = int(hexdata[2:6], 16)
    return ENTRY_TYPES.get(code, f"0x{code:04x}")


def obj_rows(state):
    rows = []
    for o in state:
        h = o["data"]
        if len(h) % 2:
            raise ValueError(f"odd hex length for {o['index']}")
        rows.append({"index": o["index"].upper(), "entry_type": entry_type(h), "bytes": len(h) // 2})
    return rows


def page_ledger(rpc, ledger, limit=2048):
    """All objects of `ledger` (binary). Every page must name the same ledger hash."""
    rows, marker, ledger_hash, pages = [], None, None, 0
    while True:
        params = {"ledger_index": ledger, "binary": True, "limit": limit}
        if marker:
            params["marker"] = marker
        res = rpc.call("ledger_data", params)
        if str(res.get("ledger_index")) != str(ledger):
            raise RuntimeError(f"page answered ledger {res.get('ledger_index')} not {ledger}")
        if ledger_hash is None:
            ledger_hash = res["ledger_hash"]
        elif res["ledger_hash"] != ledger_hash:
            raise RuntimeError("ledger hash changed between pages")
        rows += obj_rows(res.get("state", []))
        pages += 1
        marker = res.get("marker")
        if not marker:
            return ledger_hash, rows, pages


def summarize(rows):
    by_type = {}
    for r in rows:
        t = by_type.setdefault(r["entry_type"], {"count": 0, "bytes": 0})
        t["count"] += 1
        t["bytes"] += r["bytes"]
    return {
        "objects": len(rows),
        "bytes": sum(r["bytes"] for r in rows),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1]["bytes"], kv[0]))),
    }


def read_snapshot(path):
    """(header, {index: LedgerEntryType}) from a state/snapshot.py jsonl.gz."""
    with gzip.open(path, "rt") as fh:
        header = json.loads(fh.readline())
        types = {}
        for line in fh:
            o = json.loads(line)
            types[o["index"].upper()] = o["LedgerEntryType"]
    return header, types


def join_check(ledger_hash, rows, header, types):
    """List of problems between the binary rows and a JSON snapshot of the same ledger."""
    problems = []
    if header.get("ledger_hash", "").upper() != ledger_hash.upper():
        problems.append(f"ledger hash {ledger_hash} != snapshot {header.get('ledger_hash')}")
    seen = {}
    for r in rows:
        if r["index"] in seen:
            problems.append(f"duplicate index {r['index']}")
        seen[r["index"]] = r["entry_type"]
    for idx in sorted(set(types) - set(seen)):
        problems.append(f"missing in binary: {idx}")
    for idx in sorted(set(seen) - set(types)):
        problems.append(f"missing in snapshot: {idx}")
    for idx in sorted(set(seen) & set(types)):
        if seen[idx] != types[idx]:
            problems.append(f"type {idx}: binary {seen[idx]} != json {types[idx]}")
    return problems


def write_outputs(out, ledger, ledger_hash, rows, pages, join):
    os.makedirs(out, exist_ok=True)
    rows = sorted(rows, key=lambda r: r["index"])
    csv_path = os.path.join(out, f"statebytes-{ledger}.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["index", "entry_type", "bytes"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    res = {
        "ledger_index": ledger,
        "ledger_hash": ledger_hash,
        "pages": pages,
        **summarize(rows),
        "join": join,
    }
    with open(os.path.join(out, f"statebytes-{ledger}.json"), "w") as fh:
        json.dump(res, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return res


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True)
    p.add_argument("--ledger", type=int, required=True)
    p.add_argument("--join", default=None, help="state/snapshot.py jsonl.gz of the same ledger")
    p.add_argument("--limit", type=int, default=2048)
    p.add_argument("--rate", type=float, default=2.0)
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    a = p.parse_args(argv)
    rpc = Rpc(a.container, a.port, a.rate)
    print(f"{utcnow()} START statebytes ledger {a.ledger}", flush=True)
    ledger_hash, rows, pages = page_ledger(rpc, a.ledger, a.limit)
    join = None
    if a.join:
        header, types = read_snapshot(a.join)
        problems = join_check(ledger_hash, rows, header, types)
        join = {"snapshot": os.path.basename(a.join), "objects": len(types), "problems": len(problems)}
        for pr in problems[:50]:
            print(f"JOIN {pr}", flush=True)
    res = write_outputs(a.out, a.ledger, ledger_hash, rows, pages, join)
    print(
        f"{utcnow()} DONE ledger {a.ledger} {ledger_hash} objects {res['objects']} bytes {res['bytes']}"
        f" pages {pages} rpc {rpc.count}"
        + (f" join problems {join['problems']}/{join['objects']}" if join else ""),
        flush=True,
    )
    for t, v in res["by_type"].items():
        print(f"  {t:<24} {v['count']:>7} objects {v['bytes']:>10} bytes", flush=True)
    return 1 if join and join["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
