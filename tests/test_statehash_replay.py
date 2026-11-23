"""statehash/replay_statehash.py on a real 158-ledger window (published snapshot A 8290390 + recorded fixture
snapshot 8290548 stored as a diff against A + the binary capture between them), plus named-reason controls."""

import copy
import gzip
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statehash"))

import lib_statehash as L  # noqa: E402
import replay_statehash as R  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "statehash")
DATA_A = os.path.join(HERE, "..", "statehash", "data", "A")
DEFS = L.Defs()


def load():
    ha, items_a = R.load_snapshot(DATA_A)
    with open(os.path.join(FIX, "fixB-header.json")) as fh:
        hb = json.load(fh)
    b = dict(items_a)
    for d in R.read_jsonl(os.path.join(FIX, "fixB-diff.jsonl")):
        k = bytes.fromhex(d["index"])
        if d["data"] is None:
            b.pop(k)
        else:
            b[k] = bytes.fromhex(d["data"])
    with gzip.open(os.path.join(FIX, "cap.jsonl.gz"), "rt") as fh:
        rows = [json.loads(x) for x in fh]
    return ha, items_a, hb, sorted(b.items()), rows


BASE = load()


def run(ha=None, a=None, hb=None, b=None, rows=None):
    h_a, i_a, h_b, i_b, r = copy.deepcopy(BASE)
    return R.replay(
        DEFS, ha or h_a, a if a is not None else i_a, hb or h_b, b if b is not None else i_b, rows or r
    )


def reason(**kw):
    with pytest.raises(R.ReplayError) as e:
        run(**kw)
    return e.value


def test_real_window_matches_every_ledger_root_and_b():
    ha, _, hb, _, rows = BASE
    st = run()
    assert st["root_A"] == ha["account_hash"] and st["root_B"] == hb["account_hash"]
    assert st["roots_matched"] == st["ledgers"] == hb["ledger_index"] - ha["ledger_index"] == 158
    assert st["transactions"] == sum(
        len(r["transactions"]) for r in rows if r["ledger_index"] > ha["ledger_index"]
    )
    assert st["objects_B"] == 5334


def test_published_a_matches_own_and_public_header():
    ha, items_a, _, _, _ = BASE
    with open(os.path.join(DATA_A, "public-header.json")) as fh:
        pub = json.load(fh)
    root = L.root_of({k: L.leaf_hash(k, d) for k, d in items_a}).hex().upper()
    assert root == ha["account_hash"] == pub["account_hash"]
    assert pub["ledger_hash"] == ha["ledger_hash"] and pub["validated"] is True


def flip(items, i):
    items = list(items)
    k, d = items[i]
    items[i] = (k, d[:-1] + bytes([d[-1] ^ 0x01]))
    return items


def test_flipped_blob_byte_in_a_fails_root_a():
    assert reason(a=flip(BASE[1], 100)).reason == "ROOT_A"


def test_flipped_blob_byte_in_b_fails_blob_mismatch():
    e = reason(b=flip(BASE[3], 200))
    assert e.reason == "BLOB_MISMATCH" and e.index == BASE[3][200][0].hex().upper()


def test_missing_object_in_a_fails_root_a():
    assert reason(a=BASE[1][:50] + BASE[1][51:]).reason == "ROOT_A"


def test_missing_object_in_b_is_extra_in_replay():
    e = reason(b=BASE[3][:7] + BASE[3][8:])
    assert e.reason == "EXTRA_IN_REPLAY" and e.index == BASE[3][7][0].hex().upper()


def test_extra_object_in_b_is_missing_in_replay():
    extra = (b"\xff" * 32, BASE[3][0][1])
    e = reason(b=BASE[3] + [extra])
    assert e.reason == "MISSING_IN_REPLAY" and e.index == "FF" * 32


def test_wrong_header_hashes_fail_with_named_reason():
    ha, _, hb, _, rows = copy.deepcopy(BASE)
    assert reason(ha={**ha, "account_hash": "00" * 32}).reason == "ROOT_A"
    assert reason(hb={**hb, "account_hash": "00" * 32}).reason == "ROOT_B"
    assert reason(ha={**ha, "ledger_hash": "00" * 32}).reason == "ANCHOR_A_HASH"
    assert reason(hb={**hb, "ledger_hash": "00" * 32}).reason == "ANCHOR_B_HASH"
    bad = copy.deepcopy(rows)
    hdr = bytearray.fromhex(bad[10]["header"])
    hdr[80] ^= 1  # a byte of account_hash: the header no longer hashes to its ledger hash
    bad[10]["header"] = hdr.hex().upper()
    e = reason(rows=bad)
    assert e.reason == "HEADER_HASH" and e.ledger == bad[10]["ledger_index"]


def test_skipping_one_ledgers_metadata_names_that_ledger():
    rows = copy.deepcopy(BASE[4])
    i = next(i for i, r in enumerate(rows) if r["transactions"] and i > 0)
    rows[i]["transactions"] = []
    e = reason(rows=rows)
    assert e.reason == "ROOT_MISMATCH" and e.ledger == rows[i]["ledger_index"]
    assert "written=1" in e.detail  # only the skip list was written


def test_dropped_ledger_row_is_ledger_missing():
    rows = copy.deepcopy(BASE[4])
    gone = rows.pop(20)
    assert reason(rows=rows).reason == "LEDGER_MISSING" and gone["ledger_index"]


def test_skip_list_is_derived_not_exempted(monkeypatch):
    monkeypatch.setattr(R.Replayer, "advance_skip_lists", lambda self, seq, parent: None)
    e = reason()
    assert e.reason == "ROOT_MISMATCH" and e.ledger == BASE[0]["ledger_index"] + 1


def test_negative_unl_disable_and_reenable_on_flag_ledger():
    F = {
        n: DEFS.fid(n)
        for n in (
            "LedgerEntryType",
            "Flags",
            "DisabledValidators",
            "ValidatorToDisable",
            "ValidatorToReEnable",
            "PublicKey",
            "FirstLedgerSequence",
        )
    }
    dv = DEFS.fid("DisabledValidator")[1]
    k1, k2 = b"\x21" + b"\x02" * 33, b"\x21" + b"\x03" * 33  # VL 33 + key
    obj = {
        F["LedgerEntryType"]: DEFS.entry_types["NegativeUNL"].to_bytes(2, "big"),
        F["Flags"]: bytes(4),
        F["DisabledValidators"]: L.array_raw(
            [(dv, {F["FirstLedgerSequence"]: (256).to_bytes(4, "big"), F["PublicKey"]: k1})]
        ),
        F["ValidatorToDisable"]: k2,
        F["ValidatorToReEnable"]: k1,
    }
    r = R.Replayer(DEFS, [(R.NUNL_KEY, L.serialize(obj))])
    assert r.advance_negative_unl(511) is None  # not a flag ledger
    assert r.advance_negative_unl(512) == "replaced"
    got = L.parse(r.map.get(R.NUNL_KEY))
    assert F["ValidatorToDisable"] not in got and F["ValidatorToReEnable"] not in got
    assert L.array_elements(got[F["DisabledValidators"]]) == [
        (dv, {F["FirstLedgerSequence"]: (512).to_bytes(4, "big"), F["PublicKey"]: k2})
    ]
    obj2 = dict(got)
    obj2[F["ValidatorToReEnable"]] = k2
    r2 = R.Replayer(DEFS, [(R.NUNL_KEY, L.serialize(obj2))])
    assert r2.advance_negative_unl(768) == "erased" and r2.map.get(R.NUNL_KEY) is None


def test_long_skip_list_created_on_256_boundary():
    r = R.Replayer(DEFS, [])
    r.advance_skip_lists(257, b"\xaa" * 32)
    long_key = R.long_skip_key(256)
    got = L.parse(r.map.get(long_key))
    assert got[DEFS.fid("Hashes")] == bytes([32]) + b"\xaa" * 32
    assert got[DEFS.fid("LastLedgerSequence")] == (256).to_bytes(4, "big")
    assert r.map.get(R.SKIP_KEY) is not None and r.map.size == 2
    r.advance_skip_lists(258, b"\xbb" * 32)
    assert r.map.size == 2  # 257 is no boundary: only the short list moves


def test_self_consistent_header_with_wrong_parent_is_chain_break():
    rows = copy.deepcopy(BASE[4])
    i = 30
    hdr = bytearray.fromhex(rows[i]["header"])
    hdr[12] ^= 1  # parent_hash byte; re-hash so HEADER_HASH passes and only the chain is wrong
    rows[i]["header"] = hdr.hex().upper()
    rows[i]["ledger_hash"] = L.sha512half(R.PREFIX_LEDGER, bytes(hdr)).hex().upper()
    e = reason(rows=rows)
    assert e.reason == "CHAIN_BREAK" and e.ledger == rows[i]["ledger_index"]
