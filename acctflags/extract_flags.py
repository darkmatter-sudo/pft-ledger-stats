#!/usr/bin/env python3
"""Pseudonymize acctflags raw data into the publishable inputs of analyze_flags.py (stdlib only).

Reads two collect_flags.py snapshots (A, B) and the capture directory, checks them, and writes:

  snapshots.json    per snapshot: ledger index/hash/close time, object counts, raw-file sha256
  A.jsonl, B.jsonl  one row per AccountRoot: {"id", "flags", "settings"} sorted by id
  ledgers.jsonl     ledgers A+1..B: index, hash, parent_hash, close_time, txn_count
  txs.jsonl         every transaction in A+1..B, in ledger/TransactionIndex order, with the fields
                    analyze_flags.py needs: type, result, sender, destination, DestinationTag
                    presence, and every AccountRoot / DepositPreauth node of its metadata

Accounts are never written in clear: an id is the first 12 hex chars of HMAC-SHA256(key, address)
with a random key kept in --key-file (mode 0600, never published). Settings are reduced to what the
census needs: presence of RegularKey, Domain, EmailHash, MessageKey, NFTokenMinter, WalletLocator and
the numeric TransferRate and TickSize (absent = None), so no free-text field is published.

Checks (exit 2 on any failure, nothing written): both snapshots complete (MANIFEST.json present and every
file's sha256 matches), A < B, the capture starts at or before A+1 and reaches B, every ledger in
A+1..B is present exactly once with parent_hash = hash of the ledger before it (A+1's parent is A's
hash, B's hash is snapshot B's hash), and each ledger's txn_count equals its transactions.

Usage:
  ./extract_flags.py data/acctflags/A data/acctflags/B data/acctflags/tx --out acctflags/data \
      [--key-file data/acctflags/.hmac_key]
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys

SETTINGS_PRESENT = ("RegularKey", "Domain", "EmailHash", "MessageKey", "NFTokenMinter", "WalletLocator")
SETTINGS_VALUE = ("TransferRate", "TickSize")


class Invalid(Exception):
    pass


def dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def load_key(path):
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32) + "\n")
    with open(path, encoding="utf-8") as fh:
        return bytes.fromhex(fh.read().strip())


def pseudo(key, address):
    return hmac.new(key, address.encode(), hashlib.sha256).hexdigest()[:12]


def settings_of(fields):
    s = {k: k in fields for k in SETTINGS_PRESENT}
    s.update({k: fields.get(k) for k in SETTINGS_VALUE})
    return s


def root_row(key, obj):
    return {
        "id": pseudo(key, obj["Account"]),
        "flags": int(obj.get("Flags", 0)),
        "settings": settings_of(obj),
    }


def load_snapshot(path):
    with open(os.path.join(path, "MANIFEST.json"), encoding="utf-8") as fh:
        man = json.load(fh)
    for name, meta in man["files"].items():
        with open(os.path.join(path, name), "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != meta["sha256"]:
                raise Invalid(f"{path}/{name} sha256 differs from MANIFEST.json")
    with open(os.path.join(path, "header.json"), encoding="utf-8") as fh:
        header = json.load(fh)
    with open(os.path.join(path, "counts.json"), encoding="utf-8") as fh:
        counts = json.load(fh)
    with open(os.path.join(path, "accounts.jsonl"), encoding="utf-8") as fh:
        roots = [json.loads(line) for line in fh]
    if len(roots) != counts["full_tree_by_type"].get("AccountRoot", 0):
        raise Invalid(f"{path}: accounts.jsonl rows != AccountRoot count")
    return man, header, counts, roots


def load_preauth(path):
    with open(os.path.join(path, "depositpreauth.jsonl"), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    with open(os.path.join(path, "counts.json"), encoding="utf-8") as fh:
        if len(rows) != json.load(fh)["full_tree_by_type"].get("DepositPreauth", 0):
            raise Invalid(f"{path}: depositpreauth.jsonl rows != DepositPreauth count")
    return rows


def node_rows(key, meta):
    """AccountRoot and DepositPreauth nodes of one transaction's metadata, pseudonymized."""
    rows = []
    for wrap in meta.get("AffectedNodes", []):
        ((kind, node),) = wrap.items()
        typ = node.get("LedgerEntryType")
        if typ not in ("AccountRoot", "DepositPreauth"):
            continue
        new = node.get("NewFields") or node.get("FinalFields") or {}
        old = node.get("PreviousFields") or {}
        if typ == "AccountRoot":
            row = {"node": "AccountRoot", "kind": kind, "id": pseudo(key, new["Account"])}
            if kind == "CreatedNode":
                row["after"] = {"flags": int(new.get("Flags", 0)), "settings": settings_of(new)}
            elif kind == "ModifiedNode":
                # Flags is a required field, so a change always lists the old value in PreviousFields.
                # A newly added optional field is NOT listed there, so the settings "before" a
                # modification are taken from the replayed state in analyze_flags.py, not from here.
                row["before_flags"] = int(old.get("Flags", new.get("Flags", 0)))
                row["after"] = {"flags": int(new.get("Flags", 0)), "settings": settings_of(new)}
            else:
                row["before"] = {"flags": int(new.get("Flags", 0)), "settings": settings_of(new)}
        else:
            row = {
                "node": "DepositPreauth",
                "kind": kind,
                "owner": pseudo(key, new["Account"]),
                "authorized": pseudo(key, new["Authorize"]) if "Authorize" in new else None,
            }
        rows.append(row)
    return sorted(rows, key=dumps)


def tx_row(key, ledger_index, tx):
    meta = tx["metaData"]
    row = {
        "ledger_index": ledger_index,
        "tx_index": int(meta["TransactionIndex"]),
        "type": tx["TransactionType"],
        "result": meta["TransactionResult"],
        "sender": pseudo(key, tx["Account"]),
        "nodes": node_rows(key, meta),
    }
    if "Destination" in tx:
        row["destination"] = pseudo(key, tx["Destination"])
        row["dest_tag"] = "DestinationTag" in tx
    if tx["TransactionType"] == "Payment":
        row["native"] = isinstance(tx.get("Amount"), str)
    return row


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def extract(snap_a, snap_b, capdir, key):
    man_a, hdr_a, cnt_a, roots_a = load_snapshot(snap_a)
    man_b, hdr_b, cnt_b, roots_b = load_snapshot(snap_b)
    a, b = hdr_a["ledger_index"], hdr_b["ledger_index"]
    if a >= b:
        raise Invalid(f"snapshot A {a} is not before snapshot B {b}")
    ledgers = {r["ledger_index"]: r for r in read_jsonl(os.path.join(capdir, "ledgers.jsonl"))}
    blocks = {}
    for r in read_jsonl(os.path.join(capdir, "tx.jsonl")):
        if r["ledger_index"] in blocks:
            raise Invalid(f"ledger {r['ledger_index']} captured twice")
        blocks[r["ledger_index"]] = r["transactions"]
    prev = hdr_a["ledger_hash"]
    out_ledgers, out_txs = [], []
    for n in range(a + 1, b + 1):
        if n not in ledgers or n not in blocks:
            raise Invalid(f"ledger {n} missing from the capture")
        row = ledgers[n]
        if row["parent_hash"] != prev:
            raise Invalid(f"ledger {n} parent_hash does not chain to ledger {n - 1}")
        if row["txn_count"] != len(blocks[n]):
            raise Invalid(f"ledger {n} txn_count {row['txn_count']} != {len(blocks[n])} transactions")
        prev = row["ledger_hash"]
        out_ledgers.append(
            {k: row[k] for k in ("ledger_index", "ledger_hash", "parent_hash", "close_time", "txn_count")}
        )
        txs = sorted((tx_row(key, n, t) for t in blocks[n]), key=lambda t: t["tx_index"])
        if [t["tx_index"] for t in txs] != list(range(len(txs))):
            raise Invalid(f"ledger {n} TransactionIndex values are not 0..{len(txs) - 1}")
        out_txs.extend(txs)
    if prev != hdr_b["ledger_hash"]:
        raise Invalid(f"ledger {b} in the capture is not snapshot B's ledger hash")

    def snap_meta(man, hdr, cnt, path):
        pairs = sorted(
            [pseudo(key, o["Account"]), pseudo(key, o["Authorize"])]
            for o in load_preauth(path)
            if "Authorize" in o
        )
        return {
            "preauth": pairs,
            "ledger_index": hdr["ledger_index"],
            "ledger_hash": hdr["ledger_hash"],
            "close_time_iso": hdr["close_time_iso"],
            "counts": cnt,
            "raw_sha256": {n: m["sha256"] for n, m in sorted(man["files"].items())},
        }

    return {
        "snapshots.json": json.dumps(
            {"A": snap_meta(man_a, hdr_a, cnt_a, snap_a), "B": snap_meta(man_b, hdr_b, cnt_b, snap_b)},
            sort_keys=True,
            indent=1,
        )
        + "\n",
        "A.jsonl": "".join(
            dumps(r) + "\n" for r in sorted((root_row(key, o) for o in roots_a), key=lambda r: r["id"])
        ),
        "B.jsonl": "".join(
            dumps(r) + "\n" for r in sorted((root_row(key, o) for o in roots_b), key=lambda r: r["id"])
        ),
        "ledgers.jsonl": "".join(dumps(r) + "\n" for r in out_ledgers),
        "txs.jsonl": "".join(dumps(r) + "\n" for r in out_txs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snap_a")
    ap.add_argument("snap_b")
    ap.add_argument("capture")
    ap.add_argument("--out", required=True)
    ap.add_argument("--key-file", default="data/acctflags/.hmac_key")
    a = ap.parse_args(argv)
    key = load_key(a.key_file)
    try:
        texts = extract(a.snap_a, a.snap_b, a.capture, key)
    except (Invalid, OSError, KeyError, ValueError) as e:
        print(f"INVALID {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    os.makedirs(a.out, exist_ok=True)
    for name, text in texts.items():
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
        sha = hashlib.sha256(text.encode()).hexdigest()
        print(f"WROTE {name} rows={text.count(chr(10))} sha256={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
