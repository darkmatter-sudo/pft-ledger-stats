#!/usr/bin/env python3
"""Read-only census snapshot of deferred ledger objects at ONE pinned validated ledger (stdlib only).

Two subcommands:

  types     derive the deferred object types from postfiatd source at the build commit
            (include/xrpl/protocol/detail/ledger_entries.macro) and write types.json with the
            macro line of every type and the reason it counts as deferred.

  snapshot  page the full state tree of one validated ledger with ../state/snapshot.py (pinned
            ledger hash, one fsynced line per page, torn last line cut on restart, resume on the
            same ledger), then
            1. count every deferred type a second way, with `ledger_data type=<rpc name>` on the
               same pinned ledger, and refuse any count or index-set difference
               (typecount.json, written atomically when all types are done; resumed per type);
            2. write objlife-<ledger>.jsonl: line 1 = header (ledger hash, account_hash, close
               time, reserves, counts of every LedgerEntryType, deferred counts, both counts per
               type, source commit + macro sha256), then the deferred objects sorted by index.
               Sorted JSON keys, so the same ledger always gives the same bytes;
            3. fsync the file, then write DONE (sha256 + rows of objlife-<ledger>.jsonl), fsynced.

A deferred type is one whose entry can hold value or a claim that is settled, used or lapses
later: any type with a time-bound field (sfExpiration, sfCancelAfter, sfFinishAfter,
sfSettleDelay), plus Ticket (a reserved future Sequence) and DepositPreauth (a standing
authorization), which the request names explicitly.

Only read-only RPC methods are sent (server_state, ledger, ledger_data). Nothing is signed.

Usage:
  ./collect_objlife.py types --src SRC_DIR --commit SHA --out objlife/data/types.json
  ./collect_objlife.py snapshot --types objlife/data/types.json --out data/objlife/A [--ledger N]
"""

import argparse
import hashlib
import json
import os
import re
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "state"))

import snapshot as S  # noqa: E402

MACRO = "include/xrpl/protocol/detail/ledger_entries.macro"
TIME_FIELDS = ("sfExpiration", "sfCancelAfter", "sfFinishAfter", "sfSettleDelay")
NAMED = {
    "Ticket": "reserved future Sequence (named in request)",
    "DepositPreauth": "standing authorization (named in request)",
}
ENTRY_RE = re.compile(
    r"^LEDGER_ENTRY(?:_DUPLICATE)?\(\s*(\w+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,"
)
FIELD_RE = re.compile(r"\{\s*(sf\w+)\s*,\s*(soe\w+)")
FORMAT = "pft-objlife-snapshot/1"


def parse_macro(text):
    """Every LEDGER_ENTRY in the macro: [{name, code, rpc, line, fields:{sf: soe}}] in file order."""
    entries, cur = [], None
    for n, line in enumerate(text.splitlines(), 1):
        m = ENTRY_RE.match(line)
        if m:
            cur = {
                "lt": m.group(1),
                "code": int(m.group(2), 16),
                "name": m.group(3),
                "rpc": m.group(4),
                "line": n,
                "fields": {},
            }
            entries.append(cur)
        if cur is not None:
            for f in FIELD_RE.finditer(line):
                cur["fields"][f.group(1)] = f.group(2)
    return entries


def classify(entries):
    """Add deferred (bool) and reason to each entry."""
    out = []
    for e in entries:
        tf = [f for f in TIME_FIELDS if f in e["fields"]]
        if tf:
            reason = "time-bound field " + ",".join(tf)
        elif e["name"] in NAMED:
            reason = NAMED[e["name"]]
        else:
            reason = None
        out.append(dict(e, deferred=reason is not None, reason=reason))
    return out


def cmd_types(args):
    path = os.path.join(args.src, MACRO)
    with open(path, "rb") as fh:
        raw = fh.read()
    entries = classify(parse_macro(raw.decode()))
    doc = {
        "commit": args.commit,
        "macro": MACRO,
        "macro_sha256": hashlib.sha256(raw).hexdigest(),
        "rule": "time-bound field (" + ", ".join(TIME_FIELDS) + ") or named: " + ", ".join(sorted(NAMED)),
        "entries": entries,
    }
    S.write_atomic(args.out, json.dumps(doc, sort_keys=True, indent=1) + "\n")
    for e in entries:
        if e["deferred"]:
            print(f"{MACRO}:{e['line']} {e['name']} ({e['rpc']}) deferred: {e['reason']}")
    print(f"{sum(e['deferred'] for e in entries)} deferred of {len(entries)} types -> {args.out}")
    return 0


def load_types(path):
    with open(path) as fh:
        doc = json.load(fh)
    return doc, [e for e in doc["entries"] if e["deferred"]]


def type_count(rpc, state, rpc_name, limit=2048):
    """Indexes of all objects of one type on the pinned ledger, via ledger_data type filter."""
    idx, marker = [], None
    while True:
        params = {"ledger_index": state["ledger_index"], "binary": False, "limit": limit, "type": rpc_name}
        if marker:
            params["marker"] = marker
        r = rpc.call("ledger_data", params)
        if r["ledger_hash"] != state["ledger_hash"]:
            raise RuntimeError(f"type {rpc_name}: ledger hash {r['ledger_hash']} != pinned")
        idx.extend(o["index"] for o in r["state"])
        marker = r.get("marker")
        if not marker:
            return sorted(idx)


def build(objects, state, types_doc, deferred, typecounts):
    """(header, rows) for objlife-<ledger>.jsonl; raises on any count disagreement."""
    by_name = {e["name"]: e for e in deferred}
    all_counts, rows = {}, []
    for o in objects:
        t = o["LedgerEntryType"]
        all_counts[t] = all_counts.get(t, 0) + 1
        if t in by_name:
            rows.append(o)
    rows.sort(key=lambda o: o["index"])
    per_type = {}
    for name, e in sorted(by_name.items()):
        local = sorted(o["index"] for o in rows if o["LedgerEntryType"] == name)
        filt = typecounts[e["rpc"]]
        if local != filt:
            raise ValueError(
                f"{name}: full-tree {len(local)} objects != type filter {len(filt)} (or different indexes)"
            )
        per_type[name] = {"rpc": e["rpc"], "full_tree": len(local), "type_filter": len(filt)}
    header = {k: v for k, v in state.items() if k != "server_state_validated_seq"}
    header.update(
        format=FORMAT,
        objects_paged=len(objects),
        counts_all_types=all_counts,
        deferred=per_type,
        deferred_rows=len(rows),
        source_commit=types_doc["commit"],
        macro_sha256=types_doc["macro_sha256"],
    )
    return header, rows


def write_lines(path, header, rows):
    data = "".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n" for x in [header] + rows)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return hashlib.sha256(data.encode()).hexdigest()


def cmd_snapshot(args, rpc=None):
    types_doc, deferred = load_types(args.types)
    rpc = rpc or S.Rpc(
        args.container, args.port, args.rate, ["docker"] if args.no_sudo else ["sudo", "docker"]
    )
    logfh = open(args.log, "a") if args.log else None
    try:
        path, _ = S.collect(rpc, args.out, args.ledger, args.limit, 0.0, logfh, args.max_pages)
        if not path:
            return 4
        with open(os.path.join(args.out, "state.json")) as fh:
            state = json.load(fh)
        objects = S.repair_pages(os.path.join(args.out, "pages.jsonl"))[0]
        objects = [o for p in objects for o in p["state"]]
        tpath = os.path.join(args.out, "typecount.json")
        tc = {}
        if os.path.exists(tpath):
            with open(tpath) as fh:
                tc = json.load(fh)
            if tc.get("ledger_hash") != state["ledger_hash"]:
                raise ValueError("typecount.json is from another ledger")
        counts = tc.get("types", {})
        for e in deferred:
            if e["rpc"] in counts:
                continue
            if S._stop:
                return 4
            try:
                counts[e["rpc"]] = type_count(rpc, state, e["rpc"], args.limit)
            except LookupError as err:
                S.log(logfh, f"ABORT type {e['rpc']}: {err}")
                return 3
            S.write_atomic(
                tpath,
                json.dumps({"ledger_hash": state["ledger_hash"], "types": counts}, sort_keys=True) + "\n",
            )
            S.log(logfh, f"typecount {e['rpc']} {len(counts[e['rpc']])}")
        header, rows = build(objects, state, types_doc, deferred, counts)
        name = f"objlife-{state['ledger_index']}.jsonl"
        digest = write_lines(os.path.join(args.out, name), header, rows)
        S.write_atomic(os.path.join(args.out, "DONE"), f"{digest}  {name}\nrows {len(rows)}\n")
        S.log(
            logfh,
            f"OBJLIFE {name} rows {len(rows)} sha256 {digest} | "
            + " ".join(f"{k}={v['full_tree']}/{v['type_filter']}" for k, v in header["deferred"].items()),
        )
        return 0
    finally:
        if logfh:
            logfh.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("types")
    t.add_argument("--src", required=True, help="postfiatd source checkout at the build commit")
    t.add_argument("--commit", required=True)
    t.add_argument("--out", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--types", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--ledger", type=int, default=None)
    s.add_argument("--limit", type=int, default=2048)
    s.add_argument("--rate", type=float, default=2.0)
    s.add_argument("--max-pages", type=int, default=None, help="stop after N pages (resume drills)")
    s.add_argument("--log", default=None)
    s.add_argument("--container", default="postfiatd")
    s.add_argument("--port", type=int, default=5005)
    s.add_argument("--no-sudo", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "types":
        return cmd_types(args)
    signal.signal(signal.SIGTERM, S._on_signal)
    signal.signal(signal.SIGINT, S._on_signal)
    return cmd_snapshot(args)


if __name__ == "__main__":
    sys.exit(main())
