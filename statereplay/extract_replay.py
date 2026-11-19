#!/usr/bin/env python3
"""Write the publishable, pseudonymized inputs of replay.py and hotspots.py (stdlib only).

Reads two acctflags/collect_flags.py snapshots (A, B) and its capture directory, checks them, and writes
to --out (every file gzip with mtime=0 and sorted keys, so the same input gives the same bytes):

  snapshots.json     per snapshot: header (index, hash, parent, close time), object counts by type,
                     sha256 of every raw manifest file
  A.jsonl.gz         every ledger object of snapshot A (full state tree), sorted by index
  B.jsonl.gz         same for snapshot B
  ledgers.jsonl.gz   ledger rows A..B (index, hash, parent_hash, close_time, txn_count)
  tx.jsonl.gz        one line per ledger A+1..B: every transaction reduced to hash, TransactionType,
                     Account and metaData (TransactionIndex, TransactionResult, AffectedNodes)

Pseudonymization is a deterministic substitution applied identically to A, B and the metadata, so the
replay of the published files is the same replay as on the raw files:
  - every string that is a valid classic address (base58check, version 0, 20 bytes) becomes
    "acct_" + first 12 hex chars of HMAC-SHA256(key, address), key in --key-file (0600, private);
  - free-text fields (URI, Domain, Memo*, Data, DIDDocument) become "text_" + sha256(value)[:16].
Object indexes, ledger and transaction hashes stay real (they are needed for the public-RPC crosscheck),
so the pseudonyms keep addresses out of the files and the report, not out of reach of anyone who looks
the indexes up on the public ledger.

Checks (exit 2, nothing written): both snapshot MANIFEST.json present and every listed file's sha256
matches, A < B, and the capture holds every ledger A..B (the chain itself is checked by replay.py).

--subset N writes a test fixture instead: only the objects the window's transactions touch, the
derived skip-list/NegativeUNL objects and N untouched bystanders (tests/fixtures/statereplay/).

Usage:
  ./extract_replay.py data/statereplay/A data/statereplay/B data/statereplay/tx --out statereplay/data \
      [--key-file data/statereplay/.hmac_key] [--subset N]
"""

import argparse
import gzip
import hashlib
import hmac
import json
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import replay  # noqa: E402

B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
FREE_TEXT = ("URI", "Domain", "MemoData", "MemoType", "MemoFormat", "Data", "DIDDocument")
TX_KEEP = ("hash", "TransactionType", "Account")
META_KEEP = ("TransactionIndex", "TransactionResult", "AffectedNodes")


class Invalid(Exception):
    pass


def is_address(s):
    if not (25 <= len(s) <= 35) or s[0] != "r":
        return False
    n = 0
    for ch in s:
        i = B58.find(ch)
        if i < 0:
            return False
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(s) - len(s.lstrip(B58[0]))) + raw
    if len(raw) != 25 or raw[0] != 0:
        return False
    return hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]


class Pseudonymizer:
    def __init__(self, key):
        self.key = key
        self.addresses = 0
        self.texts = 0

    def account(self, addr):
        return "acct_" + hmac.new(self.key, addr.encode(), hashlib.sha256).hexdigest()[:12]

    def walk(self, o, field=None):
        if isinstance(o, dict):
            return {k: self.walk(v, k) for k, v in o.items()}
        if isinstance(o, list):
            return [self.walk(v, field) for v in o]
        if isinstance(o, str):
            if field in FREE_TEXT:
                self.texts += 1
                return "text_" + hashlib.sha256(o.encode()).hexdigest()[:16]
            if o.startswith("r") and is_address(o):
                self.addresses += 1
                return self.account(o)
        return o


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_manifest(d):
    path = os.path.join(d, "MANIFEST.json")
    if not os.path.exists(path):
        raise Invalid(f"{d}: no MANIFEST.json (incomplete snapshot)")
    with open(path) as f:
        man = json.load(f)
    sums = {}
    for name, info in sorted(man["files"].items()):
        got = sha256_file(os.path.join(d, name))
        if got != info["sha256"]:
            raise Invalid(f"{d}/{name}: sha256 {got} != manifest {info['sha256']}")
        sums[name] = got
    return man, sums


def load_key(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def write_gz(path, rows):
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            for r in rows:
                gz.write((json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n").encode())


def slim(tx):
    out = {k: tx[k] for k in TX_KEEP if k in tx}
    out["metaData"] = {k: tx["metaData"][k] for k in META_KEEP if k in tx["metaData"]}
    return out


def subset_indexes(state, target, txs, first, last, bystanders):
    """Index set of a small test fixture: every object any transaction of the window touches, the
    skip lists and NegativeUNL (derived writes), plus `bystanders` untouched objects picked by
    sha256(index) order, so the subset is deterministic."""
    keep = {replay.SKIP_KEY, replay.NUNL_KEY}
    keep |= {replay.long_skip_key(n - 1) for n in range(first, last + 1) if (n - 1) % 256 == 0}
    for r in txs:
        for t in r["transactions"]:
            for node in t["metaData"]["AffectedNodes"]:
                keep.add(next(iter(node.values()))["LedgerIndex"])
    rest = sorted(set(state) - keep, key=lambda i: hashlib.sha256(i.encode()).hexdigest())
    return keep | set(rest[:bystanders])


def extract(a_dir, b_dir, txdir, out, key, subset=None):
    man_a, sums_a = check_manifest(a_dir)
    man_b, sums_b = check_manifest(b_dir)
    ha, state, hb, target, rows, txs = replay.load_raw(a_dir, b_dir, txdir)
    if not ha["ledger_index"] < hb["ledger_index"]:
        raise Invalid(f"A {ha['ledger_index']} is not before B {hb['ledger_index']}")
    have = {r["ledger_index"] for r in rows}
    missing = [n for n in range(ha["ledger_index"], hb["ledger_index"] + 1) if n not in have]
    if missing:
        raise Invalid(f"capture lacks {len(missing)} ledgers of A..B, first {missing[0]}")
    if subset is not None:
        keep = subset_indexes(state, target, txs, ha["ledger_index"] + 1, hb["ledger_index"], subset)
        state = {i: o for i, o in state.items() if i in keep}
        target = {i: o for i, o in target.items() if i in keep}
    p = Pseudonymizer(key)
    snaps = {}
    for name, header, objs, sums in (("A", ha, state, sums_a), ("B", hb, target, sums_b)):
        counts = {}
        for o in objs.values():
            counts[o["LedgerEntryType"]] = counts.get(o["LedgerEntryType"], 0) + 1
        snaps[name] = {
            "header": {
                k: header[k] for k in ("ledger_index", "ledger_hash", "parent_hash", "close_time_iso")
            },
            "objects": len(objs),
            "by_type": dict(sorted(counts.items())),
            "raw_sha256": sums,
        }
    os.makedirs(out, exist_ok=True)
    write_gz(os.path.join(out, "A.jsonl.gz"), (p.walk(state[i]) for i in sorted(state)))
    write_gz(os.path.join(out, "B.jsonl.gz"), (p.walk(target[i]) for i in sorted(target)))
    keep = ("ledger_index", "ledger_hash", "parent_hash", "close_time", "txn_count")
    write_gz(
        os.path.join(out, "ledgers.jsonl.gz"),
        ({k: r[k] for k in keep} for r in sorted(rows, key=lambda r: r["ledger_index"])),
    )
    tx_rows = []
    for r in sorted(txs, key=lambda r: r["ledger_index"]):
        ordered = sorted(r["transactions"], key=lambda t: t["metaData"]["TransactionIndex"])
        tx_rows.append(
            {"ledger_index": r["ledger_index"], "transactions": [p.walk(slim(t)) for t in ordered]}
        )
    write_gz(os.path.join(out, "tx.jsonl.gz"), tx_rows)
    with open(os.path.join(out, "snapshots.json"), "w") as f:
        json.dump(snaps, f, indent=1, sort_keys=True)
        f.write("\n")
    files = {}
    for name in sorted(os.listdir(out)):
        files[name] = sha256_file(os.path.join(out, name))
    return snaps, files, p


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("tx")
    ap.add_argument("--out", required=True)
    ap.add_argument("--key-file", default="data/statereplay/.hmac_key")
    ap.add_argument(
        "--subset",
        type=int,
        metavar="N",
        help="test fixture: keep only touched + derived objects and N untouched bystanders",
    )
    a = ap.parse_args(argv)
    try:
        snaps, files, p = extract(a.a, a.b, a.tx, a.out, load_key(a.key_file), a.subset)
    except (Invalid, replay.ReplayError) as e:
        print(f"INVALID {e}")
        return 2
    for name in ("A", "B"):
        h = snaps[name]["header"]
        objects = snaps[name]["objects"]
        print(f"SNAPSHOT {name} ledger={h['ledger_index']} hash={h['ledger_hash']} objects={objects}")
    print(f"PSEUDONYMIZED addresses={p.addresses} free_text={p.texts}")
    for name, digest in files.items():
        print(f"FILE {name} sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
