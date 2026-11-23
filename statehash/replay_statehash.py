#!/usr/bin/env python3
"""Rebuild the state SHAMap of snapshot A, replay every ledger A+1..B onto it in binary and check the
root after EVERY ledger against that ledger's header account_hash (stdlib only).

Inputs (raw collector output or the published data/ folder, same layout):
  A/, B/   collect_statehash.py snapshots: header.json + state.jsonl ({index, data} per object)
  cap/     collect_statehash.py capture: ledgers.jsonl (binary header + {tx_blob, meta} per tx)

Steps (the replay rules of statereplay/, applied to binary fields; field codes from definitions.json):
  0. rebuild A from its blobs; root must equal A's header account_hash (ROOT_A)
  for every ledger n = A+1 .. B:
  1. the capture line must exist once and in order (LEDGER_MISSING / LEDGER_ORDER), its header must hash
     to its ledger hash and chain to the previous ledger (HEADER_HASH / CHAIN_BREAK); ledger A's and
     B's hashes must equal the snapshots' pinned hashes (ANCHOR_A_HASH / ANCHOR_B_HASH)
  2. flag ledger (n % 256 == 0): a pending NegativeUNL ValidatorToDisable / ValidatorToReEnable takes
     effect first (Ledger::updateNegativeUNL, no metadata)
  3. transactions in TransactionIndex order 0..k-1 (TX_INDEX_GAP); each AffectedNode is checked against
     the replayed object before it is applied: CREATE_EXISTS, NODE_MISSING, TYPE_MISMATCH,
     PREVIOUS_MISMATCH, UNCHANGED_MISMATCH, THREAD_MISMATCH (see statereplay/README.md)
  4. LedgerHashes skip lists advanced as Ledger::updateSkipList (no metadata)
  5. rebuilt root == ledger n's header account_hash, else ROOT_MISMATCH naming the ledger and the
     object indexes written in it (the first diverging ledger)
  after B: every object must equal snapshot B byte-for-byte (MISSING_IN_REPLAY / EXTRA_IN_REPLAY /
  BLOB_MISMATCH) and B's rebuilt root must equal B's header account_hash (ROOT_B).

Exit 0 only when every check passes; failures print `FAIL <REASON> ...` and exit 1.

Usage:
  ./replay_statehash.py --a DIR --b DIR --cap DIR [--json out.json]
"""

import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_statehash as L  # noqa: E402

SKIP_KEY = bytes.fromhex("B4979A36CDC7F3D3D5C31A4EAE2AC7D7209DDA877588B9AFC66799692AB0D66B")
NUNL_KEY = bytes.fromhex("2E8A59AA9D3B5B186B0B9E0F62E6C02587CA74A4D778938E957B6357D364B244")
PREFIX_LEDGER = b"LWR\x00"
PREFIX_TXID = b"TXN\x00"
# CreatedNode NewFields omit default-valued fields; these are the required zero-default fields per
# type (same table as statereplay/replay.py CREATE_DEFAULTS), written as (field name, byte width)
CREATE_DEFAULTS = {
    "*": [("Flags", 4)],
    "AccountRoot": [("OwnerCount", 4)],
    "Offer": [("OwnerNode", 8), ("BookNode", 8)],
    "NFTokenOffer": [("OwnerNode", 8), ("NFTokenOfferNode", 8)],
    "Check": [("OwnerNode", 8)],
    "Escrow": [("OwnerNode", 8)],
    "PayChannel": [("OwnerNode", 8)],
    "Ticket": [("OwnerNode", 8)],
    "DepositPreauth": [("OwnerNode", 8)],
    "SignerList": [("OwnerNode", 8)],
}


class ReplayError(Exception):
    def __init__(self, reason, ledger=None, tx=None, index=None, detail=""):
        self.reason, self.ledger, self.tx, self.index, self.detail = reason, ledger, tx, index, detail
        super().__init__(f"{reason} ledger={ledger} tx={tx} index={index} {detail}".rstrip())


def read_jsonl(path):
    """Rows of path, or of path + ".gz" when only the compressed copy exists (published data/)."""
    if not os.path.exists(path) and os.path.exists(path + ".gz"):
        with gzip.open(path + ".gz", "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load_snapshot(d):
    with open(os.path.join(d, "header.json"), encoding="utf-8") as fh:
        hdr = json.load(fh)
    items = [
        (bytes.fromhex(r["index"]), bytes.fromhex(r["data"]))
        for r in read_jsonl(os.path.join(d, "state.jsonl"))
    ]
    return hdr, items


def header_fields(blob):
    return {
        "ledger_index": int.from_bytes(blob[0:4], "big"),
        "parent_hash": blob[12:44],
        "account_hash": blob[76:108],
    }


class Replayer:
    def __init__(self, defs, items):
        self.d = defs
        self.map = L.SHAMap(items)
        f = defs.fid
        self.F = {
            n: f(n)
            for n in (
                "LedgerEntryType",
                "LedgerIndex",
                "PreviousTxnID",
                "PreviousTxnLgrSeq",
                "TransactionIndex",
                "AffectedNodes",
                "NewFields",
                "FinalFields",
                "PreviousFields",
                "Flags",
                "Hashes",
                "LastLedgerSequence",
                "DisabledValidators",
                "DisabledValidator",
                "ValidatorToDisable",
                "ValidatorToReEnable",
                "PublicKey",
                "FirstLedgerSequence",
            )
        }
        self.kinds = {
            f("CreatedNode")[1]: "CreatedNode",
            f("ModifiedNode")[1]: "ModifiedNode",
            f("DeletedNode")[1]: "DeletedNode",
        }
        self.thread = (self.F["PreviousTxnID"], self.F["PreviousTxnLgrSeq"])
        self.written = []

    def obj(self, key):
        blob = self.map.get(key)
        return None if blob is None else L.parse(blob)

    def put(self, key, fields):
        self.map.set(key, L.serialize(fields))
        self.written.append(key)

    def drop(self, key):
        self.map.delete(key)
        self.written.append(key)

    def etype_name(self, raw):
        return self.d.entry_names.get(L.u16(raw), f"?{L.u16(raw)}")

    # ---------------------------------------------------------- per-ledger derived writes

    def advance_skip_lists(self, seq, parent_hash):
        F, prev = self.F, seq - 1
        lh_type = self.d.entry_types["LedgerHashes"].to_bytes(2, "big")

        def push(key, keep):
            cur = self.obj(key)
            if cur is None:
                cur = {F["LedgerEntryType"]: lh_type, F["Flags"]: bytes(4)}
                hashes = []
            else:
                raw = cur.get(F["Hashes"], b"\x00")
                n, j = L.read_vl(raw, 0)
                hashes = [raw[j + i : j + i + 32] for i in range(0, n, 32)]
            hashes.append(parent_hash)
            if keep:
                hashes = hashes[-keep:]
            body = b"".join(hashes)
            cur[F["Hashes"]] = vl(len(body)) + body
            cur[F["LastLedgerSequence"]] = prev.to_bytes(4, "big")
            self.put(key, cur)

        if prev > 0 and prev % 256 == 0:
            push(long_skip_key(prev), None)
        push(SKIP_KEY, 256)

    def advance_negative_unl(self, seq):
        if seq % 256 != 0:
            return None
        F = self.F
        cur = self.obj(NUNL_KEY)
        if cur is None:
            return None
        to_dis, to_re = cur.get(F["ValidatorToDisable"]), cur.get(F["ValidatorToReEnable"])
        if to_dis is None and to_re is None:
            return None
        elems = L.array_elements(cur[F["DisabledValidators"]]) if F["DisabledValidators"] in cur else []
        new = [(n, e) for n, e in elems if to_re is None or e.get(F["PublicKey"]) != to_re]
        if to_dis is not None:
            new.append(
                (
                    F["DisabledValidator"][1],
                    {F["FirstLedgerSequence"]: seq.to_bytes(4, "big"), F["PublicKey"]: to_dis},
                )
            )
        if not new:
            self.drop(NUNL_KEY)
            return "erased"
        cur.pop(F["ValidatorToDisable"], None)
        cur.pop(F["ValidatorToReEnable"], None)
        cur[F["DisabledValidators"]] = L.array_raw(new)
        self.put(NUNL_KEY, cur)
        return "replaced"

    # ---------------------------------------------------------- metadata

    def apply_node(self, kind, body, tx_id, seq):
        F = self.F
        key = body[F["LedgerIndex"]]
        et_raw = body[F["LedgerEntryType"]]
        etype = self.etype_name(et_raw)
        idx = key.hex().upper()
        cur = self.obj(key)
        if kind == "CreatedNode":
            if cur is not None:
                raise ReplayError("CREATE_EXISTS", seq, tx_id, idx)
            new = {F["LedgerEntryType"]: et_raw}
            for name, width in CREATE_DEFAULTS["*"] + CREATE_DEFAULTS.get(etype, []):
                new[self.d.fid(name)] = bytes(width)
            if F["NewFields"] in body:
                new.update(L.object_fields(body[F["NewFields"]]))
            if threaded(etype):
                new[F["PreviousTxnID"]], new[F["PreviousTxnLgrSeq"]] = (
                    tx_id_bytes(tx_id),
                    seq.to_bytes(4, "big"),
                )
            self.put(key, new)
            return
        if cur is None:
            raise ReplayError("NODE_MISSING", seq, tx_id, idx, kind)
        if cur.get(F["LedgerEntryType"]) != et_raw:
            raise ReplayError("TYPE_MISMATCH", seq, tx_id, idx)
        final = L.object_fields(body[F["FinalFields"]]) if F["FinalFields"] in body else {}
        prevf = L.object_fields(body[F["PreviousFields"]]) if F["PreviousFields"] in body else {}
        for k, v in prevf.items():
            if cur.get(k) != v:
                raise ReplayError("PREVIOUS_MISMATCH", seq, tx_id, idx, self.d.name(k))
        for k, v in final.items():
            if k in self.thread:
                continue
            if k not in prevf and k in cur and cur[k] != v:
                raise ReplayError("UNCHANGED_MISMATCH", seq, tx_id, idx, self.d.name(k))
        if F["PreviousTxnID"] in body and cur.get(F["PreviousTxnID"]) != body[F["PreviousTxnID"]]:
            raise ReplayError("THREAD_MISMATCH", seq, tx_id, idx)
        if kind == "DeletedNode":
            self.drop(key)
            return
        new = dict(cur)
        for k in prevf:
            if k not in final:
                new.pop(k, None)
        new.update(final)
        if threaded(etype) and (F["PreviousTxnID"] in body or F["PreviousTxnID"] in cur):
            new[F["PreviousTxnID"]], new[F["PreviousTxnLgrSeq"]] = tx_id_bytes(tx_id), seq.to_bytes(4, "big")
        self.put(key, new)

    def apply_tx(self, meta, tx_id, seq):
        F = self.F
        nodes = 0
        for nth, body in L.array_elements(meta[F["AffectedNodes"]]):
            kind = self.kinds.get(nth)
            if kind is None:
                raise ReplayError("NODE_KIND", seq, tx_id, detail=str(nth))
            self.apply_node(kind, body, tx_id, seq)
            nodes += 1
        return nodes

    def apply_ledger(self, seq, header, txs, stats):
        F = self.F
        self.written = []
        nunl = self.advance_negative_unl(seq)
        if nunl:
            stats.setdefault("negative_unl", []).append([seq, nunl])
        metas = []
        for t in txs:
            blob = bytes.fromhex(t["tx_blob"])
            meta = L.parse(bytes.fromhex(t["meta"]))
            metas.append(
                (L.u32(meta[F["TransactionIndex"]]), L.sha512half(PREFIX_TXID, blob).hex().upper(), meta)
            )
        metas.sort(key=lambda m: m[0])
        if [m[0] for m in metas] != list(range(len(metas))):
            raise ReplayError("TX_INDEX_GAP", seq)
        for _, tx_id, meta in metas:
            stats["nodes"] += self.apply_tx(meta, tx_id, seq)
            stats["transactions"] += 1
        self.advance_skip_lists(seq, header["parent_hash"])
        root = self.map.root_hash()
        if root != header["account_hash"]:
            written = sorted({k.hex().upper() for k in self.written})
            raise ReplayError(
                "ROOT_MISMATCH",
                seq,
                detail=f"rebuilt={root.hex().upper()} header={header['account_hash'].hex().upper()} "
                f"written={len(written)} first_written={written[:8]}",
            )
        stats["ledgers"] += 1
        stats["roots_matched"] += 1


def vl(n):
    if n <= 192:
        return bytes([n])
    if n <= 12480:
        n -= 193
        return bytes([193 + (n >> 8), n & 0xFF])
    n -= 12481
    return bytes([241 + (n >> 16), (n >> 8) & 0xFF, n & 0xFF])


def tx_id_bytes(tx_id):
    return bytes.fromhex(tx_id)


def long_skip_key(prev):
    return L.sha512half((0x0073).to_bytes(2, "big") + (prev >> 16).to_bytes(4, "big"))


def threaded(etype):
    """Every type except LedgerHashes threads PreviousTxnID (fixPreviousTxnID; see statereplay/)."""
    return etype != "LedgerHashes"


def replay(defs, ha, items_a, hb, items_b, cap_rows, on_ledger=None):
    """Returns stats; raises ReplayError on the first failure."""
    r = Replayer(defs, items_a)
    root_a = r.map.root_hash().hex().upper()
    if root_a != ha["account_hash"]:
        raise ReplayError(
            "ROOT_A", ha["ledger_index"], detail=f"rebuilt={root_a} header={ha['account_hash']}"
        )
    first, last = ha["ledger_index"] + 1, hb["ledger_index"]
    if last < first:
        raise ReplayError("WINDOW_EMPTY", detail=f"A={first - 1} B={last}")
    by_n = {}
    for row in cap_rows:
        n = row["ledger_index"]
        if n in by_n:
            raise ReplayError("LEDGER_DUPLICATE", n)
        by_n[n] = row
    if first - 1 not in by_n:
        raise ReplayError("LEDGER_MISSING", first - 1)
    if by_n[first - 1]["ledger_hash"] != ha["ledger_hash"]:
        raise ReplayError("ANCHOR_A_HASH", first - 1)
    prev_hash = bytes.fromhex(ha["ledger_hash"])
    stats = {"ledgers": 0, "transactions": 0, "nodes": 0, "roots_matched": 0, "root_A": root_a}
    for n in range(first, last + 1):
        row = by_n.get(n)
        if row is None:
            raise ReplayError("LEDGER_MISSING", n)
        blob = bytes.fromhex(row["header"])
        h = header_fields(blob)
        lh = L.sha512half(PREFIX_LEDGER, blob)
        if h["ledger_index"] != n or lh.hex().upper() != row["ledger_hash"]:
            raise ReplayError("HEADER_HASH", n)
        if h["parent_hash"] != prev_hash:
            raise ReplayError("CHAIN_BREAK", n)
        r.apply_ledger(n, h, row["transactions"], stats)
        if on_ledger:
            on_ledger(n, r, stats)
        prev_hash = lh
    if prev_hash.hex().upper() != hb["ledger_hash"]:
        raise ReplayError("ANCHOR_B_HASH", last)
    target = dict(items_b)
    diffs = []
    for k in sorted(set(target) | {key for key, _ in iter_map(r.map)}):
        s, t = r.map.get(k), target.get(k)
        if s is None:
            diffs.append(("MISSING_IN_REPLAY", k.hex().upper()))
        elif t is None:
            diffs.append(("EXTRA_IN_REPLAY", k.hex().upper()))
        elif s != t:
            diffs.append(("BLOB_MISMATCH", k.hex().upper()))
    if diffs:
        raise ReplayError(diffs[0][0], last, index=diffs[0][1], detail=f"diffs={len(diffs)}")
    root_b = L.root_of({k: L.leaf_hash(k, v) for k, v in items_b}).hex().upper()
    if root_b != hb["account_hash"] or r.map.root_hash().hex().upper() != root_b:
        raise ReplayError("ROOT_B", last, detail=f"rebuilt={root_b} header={hb['account_hash']}")
    stats["root_B"] = root_b
    stats["objects_B"] = len(target)
    return stats


def iter_map(m):
    for node in m.walk():
        if node[0] == "leaf":
            yield node[2], node[3]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--cap", required=True)
    ap.add_argument(
        "--defs", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "definitions.json")
    )
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    defs = L.Defs(a.defs)
    ha, items_a = load_snapshot(a.a)
    hb, items_b = load_snapshot(a.b)
    rows = read_jsonl(os.path.join(a.cap, "ledgers.jsonl"))
    print(
        f"INPUT A={ha['ledger_index']} B={hb['ledger_index']} capture_rows={len(rows)} "
        f"definitions={defs.hash}"
    )
    try:
        stats = replay(defs, ha, items_a, hb, items_b, rows)
    except ReplayError as e:
        print(f"FAIL {e}")
        out = {
            "ok": False,
            "reason": e.reason,
            "ledger": e.ledger,
            "tx": e.tx,
            "index": e.index,
            "detail": e.detail,
        }
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(out, fh, sort_keys=True, indent=1)
                fh.write("\n")
        return 1
    print(
        f"ROOTS root_A={stats['root_A']} root_B={stats['root_B']} "
        f"per_ledger_roots_matched={stats['roots_matched']}"
        f"/{hb['ledger_index'] - ha['ledger_index']}"
    )
    print(
        f"RESULT PASS ledgers={stats['ledgers']} transactions={stats['transactions']} nodes={stats['nodes']} "
        f"objects_B={stats['objects_B']} negative_unl={stats.get('negative_unl', [])}"
    )
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump({"ok": True, **stats}, fh, sort_keys=True, indent=1)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
