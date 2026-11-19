#!/usr/bin/env python3
"""Replay transaction metadata from snapshot A to snapshot B and require exact state equality (stdlib only).

Inputs are either the raw outputs of the existing read-only client acctflags/collect_flags.py:
  A/, B/      `snapshot` directories (header.json + pages/full-*.json = every ledger object)
  tx/         `capture` directory (ledgers.jsonl + tx.jsonl, expanded transactions with metadata)
or the published, pseudonymized data/ folder written by extract_replay.py (--data): snapshots.json,
A.jsonl.gz, B.jsonl.gz, ledgers.jsonl.gz, tx.jsonl.gz. Both go through the same replay().

For every ledger A+1 .. B, in order:
  1. the ledger must be present, contiguous and chained (parent_hash == hash of the ledger before;
     ledger A's hash == snapshot A's pinned hash, ledger B's hash == snapshot B's pinned hash);
  2. on a flag ledger (index % 256 == 0) a pending NegativeUNL ValidatorToDisable/ToReEnable is
     applied first (Ledger::updateNegativeUNL);
  3. transactions are applied in TransactionIndex order (0..n-1, no gaps or duplicates) and each
     AffectedNode must be consistent with the replayed state before it is applied:
       CreatedNode   object must not exist; new object = NewFields (+ threading fields)
       ModifiedNode  object must exist; every PreviousFields value and every unchanged FinalFields
                     value must equal the replayed value, PreviousTxnID/LgrSeq must equal the
                     replayed thread; new object = FinalFields, fields only in PreviousFields removed
       DeletedNode   object must exist and FinalFields must equal the replayed value; then removed
  4. the LedgerHashes skip lists are advanced exactly as Ledger::updateSkipList does after the
     transactions.
Steps 2 and 4 change state without any metadata, so those objects are DERIVED from the hash chain,
never exempted from the comparison.
After the last ledger the replayed state must equal snapshot B object-for-object and field-for-field.

Every failure is raised as ReplayError(reason, ...) with a specific reason code; the CLI prints
FAIL lines and exits 1. Exit 0 only for an exact match.

Usage:
  ./replay.py --a data/statereplay/A --b data/statereplay/B --tx data/statereplay/tx [--json out.json]
  ./replay.py --data statereplay/data [--json out.json]
"""

import argparse
import glob
import gzip
import hashlib
import json
import os
import sys

SKIP_KEY = "B4979A36CDC7F3D3D5C31A4EAE2AC7D7209DDA877588B9AFC66799692AB0D66B"
NUNL_KEY = "2E8A59AA9D3B5B186B0B9E0F62E6C02587CA74A4D778938E957B6357D364B244"
THREAD_FIELDS = ("PreviousTxnID", "PreviousTxnLgrSeq")
# CreatedNode NewFields omit fields that hold their default value, but ledger_data shows the
# required ones; these are the required zero-default fields per type (postfiatd ledger formats).
CREATE_DEFAULTS = {
    "*": {"Flags": 0},
    "AccountRoot": {"OwnerCount": 0},
    "Offer": {"OwnerNode": "0", "BookNode": "0"},
    "NFTokenOffer": {"OwnerNode": "0", "NFTokenOfferNode": "0"},
    "Check": {"OwnerNode": "0"},
    "Escrow": {"OwnerNode": "0"},
    "PayChannel": {"OwnerNode": "0"},
    "Ticket": {"OwnerNode": "0"},
    "DepositPreauth": {"OwnerNode": "0"},
    "SignerList": {"OwnerNode": "0"},
}


class ReplayError(Exception):
    def __init__(self, reason, ledger=None, tx=None, index=None, detail=""):
        self.reason, self.ledger, self.tx, self.index, self.detail = reason, ledger, tx, index, detail
        super().__init__(f"{reason} ledger={ledger} tx={tx} index={index} {detail}".rstrip())


def load_snapshot(d):
    with open(os.path.join(d, "header.json")) as f:
        header = json.load(f)
    state = {}
    pages = sorted(glob.glob(os.path.join(d, "pages", "full-*.json")))
    if not pages:
        raise ReplayError("SNAPSHOT_EMPTY", detail=d)
    for p in pages:
        with open(p) as f:
            page = json.load(f)
        if page.get("ledger_hash") != header["ledger_hash"]:
            raise ReplayError("SNAPSHOT_PAGE_HASH", index=os.path.basename(p))
        for obj in page["state"]:
            if obj["index"] in state:
                raise ReplayError("SNAPSHOT_DUPLICATE", index=obj["index"])
            state[obj["index"]] = obj
    return header, state


def read_jsonl(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            yield json.loads(line)


def load_raw(a_dir, b_dir, txdir):
    ha, state = load_snapshot(a_dir)
    hb, target = load_snapshot(b_dir)
    first, last = ha["ledger_index"], hb["ledger_index"]
    rows = [r for r in read_jsonl(os.path.join(txdir, "ledgers.jsonl")) if first <= r["ledger_index"] <= last]
    txs = [r for r in read_jsonl(os.path.join(txdir, "tx.jsonl")) if first < r["ledger_index"] <= last]
    return ha, state, hb, target, rows, txs


def load_published(d):
    with open(os.path.join(d, "snapshots.json")) as f:
        snaps = json.load(f)
    state = {o["index"]: o for o in read_jsonl(os.path.join(d, "A.jsonl.gz"))}
    target = {o["index"]: o for o in read_jsonl(os.path.join(d, "B.jsonl.gz"))}
    rows = list(read_jsonl(os.path.join(d, "ledgers.jsonl.gz")))
    txs = list(read_jsonl(os.path.join(d, "tx.jsonl.gz")))
    return snaps["A"]["header"], state, snaps["B"]["header"], target, rows, txs


def window(rows, txs, first, last):
    """Yield (previous ledger row, ledger row, transactions) for first..last. Ledger rows and tx lines
    are matched by ledger_index, never by position; each must appear exactly once."""
    by_row, by_tx = {}, {}
    for r in rows:
        n = r["ledger_index"]
        if first - 1 <= n <= last:
            if n in by_row:
                raise ReplayError("LEDGER_DUPLICATE", ledger=n)
            by_row[n] = r
    for r in txs:
        n = r["ledger_index"]
        if first <= n <= last:
            if n in by_tx:
                raise ReplayError("LEDGER_TX_DUPLICATE", ledger=n)
            by_tx[n] = r["transactions"]
    for n in range(first - 1, last + 1):
        if n not in by_row:
            raise ReplayError("LEDGER_MISSING", ledger=n)
    for n in range(first, last + 1):
        if n not in by_tx:
            raise ReplayError("LEDGER_TX_MISSING", ledger=n)
        yield by_row[n - 1], by_row[n], by_tx[n]


def advance_skip_lists(state, seq, parent_hash):
    """postfiatd Ledger::updateSkipList for the ledger with sequence `seq` (parent = seq-1)."""
    prev = seq - 1
    if prev > 0 and prev % 256 == 0:
        key = long_skip_key(prev)
        obj = state.get(key)
        if obj is None:
            obj = {"Flags": 0, "Hashes": [], "LedgerEntryType": "LedgerHashes", "index": key}
            state[key] = obj
        obj["Hashes"] = obj["Hashes"] + [parent_hash]
        obj["LastLedgerSequence"] = prev
    obj = state.get(SKIP_KEY)
    if obj is None:
        obj = {"Flags": 0, "Hashes": [], "LedgerEntryType": "LedgerHashes", "index": SKIP_KEY}
        state[SKIP_KEY] = obj
    hashes = obj["Hashes"] + [parent_hash]
    obj["Hashes"] = hashes[-256:]
    obj["LastLedgerSequence"] = prev


def advance_negative_unl(state, seq):
    """postfiatd Ledger::updateNegativeUNL on a flag ledger (seq % 256 == 0), before transactions:
    the pending ValidatorToDisable / ValidatorToReEnable set by the previous flag ledger's UNLModify
    take effect with no metadata. Returns the action taken (for the log), or None."""
    if seq % 256 != 0:
        return None
    obj = state.get(NUNL_KEY)
    if obj is None:
        return None
    to_dis, to_re = obj.get("ValidatorToDisable"), obj.get("ValidatorToReEnable")
    if to_dis is None and to_re is None:
        return None
    new = []
    for v in obj.get("DisabledValidators", []):
        if to_re is not None and v["DisabledValidator"]["PublicKey"] == to_re:
            continue
        new.append(v)
    if to_dis is not None:
        new.append({"DisabledValidator": {"FirstLedgerSequence": seq, "PublicKey": to_dis}})
    if not new:
        del state[NUNL_KEY]
        return "erased"
    obj = dict(obj)
    obj["DisabledValidators"] = new
    obj.pop("ValidatorToDisable", None)
    obj.pop("ValidatorToReEnable", None)
    state[NUNL_KEY] = obj
    return "replaced"


def long_skip_key(prev):
    # keylet::skip(ledgerIndex): sha512Half(LedgerSpace 's' (0x0073), uint32(ledgerIndex >> 16))
    data = (0x0073).to_bytes(2, "big") + (prev >> 16).to_bytes(4, "big")
    return hashlib.sha512(data).digest()[:32].hex().upper()


def apply_node(state, node, tx_hash, seq):
    ((kind, body),) = node.items()
    idx = body["LedgerIndex"]
    etype = body["LedgerEntryType"]
    cur = state.get(idx)
    if kind == "CreatedNode":
        if cur is not None:
            raise ReplayError("CREATE_EXISTS", seq, tx_hash, idx)
        obj = dict(CREATE_DEFAULTS["*"])
        obj.update(CREATE_DEFAULTS.get(etype, {}))
        obj.update(body.get("NewFields", {}))
        obj["LedgerEntryType"] = etype
        obj["index"] = idx
        if threaded(etype):
            obj["PreviousTxnID"], obj["PreviousTxnLgrSeq"] = tx_hash, seq
        state[idx] = obj
        return kind, etype, idx
    if cur is None:
        raise ReplayError("NODE_MISSING", seq, tx_hash, idx, kind)
    if cur.get("LedgerEntryType") != etype:
        raise ReplayError("TYPE_MISMATCH", seq, tx_hash, idx, f"{cur.get('LedgerEntryType')}!={etype}")
    final = body.get("FinalFields", {})
    prevf = body.get("PreviousFields", {})
    for k, v in prevf.items():
        if cur.get(k) != v:
            raise ReplayError("PREVIOUS_MISMATCH", seq, tx_hash, idx, k)
    for k, v in final.items():
        if k in THREAD_FIELDS:
            continue
        # a field ADDED by this transaction is in FinalFields but not in PreviousFields (there was no
        # previous value); a field present before and absent from PreviousFields must be unchanged
        if k not in prevf and k in cur and cur[k] != v:
            raise ReplayError("UNCHANGED_MISMATCH", seq, tx_hash, idx, k)
    if "PreviousTxnID" in body and cur.get("PreviousTxnID") != body["PreviousTxnID"]:
        raise ReplayError("THREAD_MISMATCH", seq, tx_hash, idx)
    if kind == "DeletedNode":
        del state[idx]
        return kind, etype, idx
    if kind != "ModifiedNode":
        raise ReplayError("NODE_KIND", seq, tx_hash, idx, kind)
    new = dict(cur)
    for k in prevf:
        if k not in final:
            new.pop(k, None)
    new.update(final)
    if threaded(etype) and ("PreviousTxnID" in body or "PreviousTxnID" in cur):
        new["PreviousTxnID"], new["PreviousTxnLgrSeq"] = tx_hash, seq
    state[idx] = new
    return kind, etype, idx


def threaded(etype):
    """Types whose PreviousTxnID/PreviousTxnLgrSeq thread is advanced by every write. With the
    fixPreviousTxnID amendment (enabled on the Post Fiat testnet, `feature` 2026-11-19) that is every
    type except LedgerHashes, including DirectoryNode, NegativeUNL, Amendments and FeeSettings."""
    return etype != "LedgerHashes"


def replay(ha, state, hb, target, rows, txs, on_tx=None):
    """Replay A -> B in place on `state`; returns (diffs against target, stats). Raises ReplayError."""
    first, last = ha["ledger_index"] + 1, hb["ledger_index"]
    if last < first:
        raise ReplayError("WINDOW_EMPTY", detail=f"A={first - 1} B={last}")
    stats = {"ledgers": 0, "transactions": 0, "nodes": 0}
    for prev, row, ltxs in window(rows, txs, first, last):
        n = row["ledger_index"]
        if n - 1 == ha["ledger_index"] and prev["ledger_hash"] != ha["ledger_hash"]:
            raise ReplayError("ANCHOR_A_HASH", ledger=n - 1)
        if row["parent_hash"] != prev["ledger_hash"]:
            raise ReplayError("CHAIN_BREAK", ledger=n)
        if row["txn_count"] != len(ltxs):
            raise ReplayError("TX_COUNT", ledger=n, detail=f"{row['txn_count']}!={len(ltxs)}")
        nunl = advance_negative_unl(state, n)
        if nunl:
            stats.setdefault("negative_unl", []).append([n, nunl])
        ordered = sorted(ltxs, key=lambda t: t["metaData"]["TransactionIndex"])
        if [t["metaData"]["TransactionIndex"] for t in ordered] != list(range(len(ltxs))):
            raise ReplayError("TX_INDEX_GAP", ledger=n)
        for t in ordered:
            touched = []
            for node in t["metaData"]["AffectedNodes"]:
                touched.append(apply_node(state, node, t["hash"], n))
                stats["nodes"] += 1
            stats["transactions"] += 1
            if on_tx:
                on_tx(n, t, touched)
        # Ledger::updateSkipList runs after the transactions are applied
        advance_skip_lists(state, n, row["parent_hash"])
        stats["ledgers"] += 1
        if n == last and row["ledger_hash"] != hb["ledger_hash"]:
            raise ReplayError("ANCHOR_B_HASH", ledger=n)
    return compare(state, target), stats


def compare(state, target):
    diffs = []
    for idx in sorted(set(state) | set(target)):
        s, t = state.get(idx), target.get(idx)
        if s is None:
            diffs.append({"index": idx, "reason": "MISSING_IN_REPLAY", "type": t["LedgerEntryType"]})
        elif t is None:
            diffs.append({"index": idx, "reason": "EXTRA_IN_REPLAY", "type": s["LedgerEntryType"]})
        elif s != t:
            fields = sorted(k for k in set(s) | set(t) if s.get(k) != t.get(k))
            diffs.append(
                {"index": idx, "reason": "FIELD_MISMATCH", "type": t["LedgerEntryType"], "fields": fields}
            )
    return diffs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--a")
    ap.add_argument("--b")
    ap.add_argument("--tx")
    ap.add_argument("--data", help="published data/ folder (extract_replay.py output)")
    ap.add_argument("--json", help="write stats + diffs as JSON")
    a = ap.parse_args(argv)
    if bool(a.data) == bool(a.a or a.b or a.tx) or (not a.data and not (a.a and a.b and a.tx)):
        ap.error("give either --data or all of --a --b --tx")
    try:
        ha, state, hb, target, rows, txs = load_published(a.data) if a.data else load_raw(a.a, a.b, a.tx)
        diffs, stats = replay(ha, state, hb, target, rows, txs)
    except ReplayError as e:
        print(f"FAIL {e}")
        return 1
    print(
        f"WINDOW A={ha['ledger_index']} B={hb['ledger_index']} ledgers={stats['ledgers']} "
        f"transactions={stats['transactions']} nodes={stats['nodes']}"
    )
    for n, action in stats.get("negative_unl", []):
        print(f"DERIVED NegativeUNL ledger={n} {action}")
    for d in diffs[:50]:
        print("DIFF", json.dumps(d, sort_keys=True))
    print(
        f"RESULT objects_replayed={len(state)} objects_B={len(target)} diffs={len(diffs)} "
        f"{'EXACT' if not diffs else 'MISMATCH'}"
    )
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"stats": stats, "diffs": diffs}, f, indent=1, sort_keys=True)
            f.write("\n")
    return 0 if not diffs else 1


if __name__ == "__main__":
    sys.exit(main())
