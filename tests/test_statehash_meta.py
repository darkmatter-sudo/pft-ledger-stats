"""statehash/replay_statehash.py node rules on synthetic binary metadata: one test per rule and refusal."""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statehash"))

import lib_statehash as L  # noqa: E402
import replay_statehash as R  # noqa: E402

D = L.Defs()
F = {
    n: D.fid(n)
    for n in (
        "LedgerEntryType",
        "Flags",
        "Sequence",
        "Balance",
        "OwnerCount",
        "Account",
        "PreviousTxnID",
        "PreviousTxnLgrSeq",
        "LedgerIndex",
        "TransactionIndex",
        "TransactionResult",
        "AffectedNodes",
        "FinalFields",
        "PreviousFields",
        "NewFields",
        "Domain",
    )
}
NTH = {k: D.fid(k)[1] for k in ("CreatedNode", "ModifiedNode", "DeletedNode")}
KEY = b"\x42" * 32
OLD_TX = b"\x0a" * 32
AR = D.entry_types["AccountRoot"].to_bytes(2, "big")


def u32(v):
    return v.to_bytes(4, "big")


def drops(v):
    return (0x4000000000000000 | v).to_bytes(8, "big")


def account(balance=100, seq=5, owners=0, thread=OLD_TX):
    return {
        F["LedgerEntryType"]: AR,
        F["Flags"]: u32(0),
        F["Sequence"]: u32(seq),
        F["Balance"]: drops(balance),
        F["OwnerCount"]: u32(owners),
        F["Account"]: b"\x14" + b"\x07" * 20,
        F["PreviousTxnID"]: thread,
        F["PreviousTxnLgrSeq"]: u32(9),
    }


def node(kind, final=None, prev=None, new=None, thread=OLD_TX, key=KEY):
    body = {F["LedgerEntryType"]: AR, F["LedgerIndex"]: key}
    if thread is not None and kind != "CreatedNode":
        body[F["PreviousTxnID"]], body[F["PreviousTxnLgrSeq"]] = thread, u32(9)
    for name, fields in (("FinalFields", final), ("PreviousFields", prev), ("NewFields", new)):
        if fields is not None:
            body[F[name]] = L.object_raw(fields)
    return (NTH[kind], body)


def meta(*nodes, index=0):
    return {
        F["TransactionIndex"]: u32(index),
        F["TransactionResult"]: b"\x00",
        F["AffectedNodes"]: L.array_raw(list(nodes)),
    }


def replayer(obj=None):
    return R.Replayer(D, [] if obj is None else [(KEY, L.serialize(obj))])


TX = "AB" * 32


def final_of(obj):
    return {
        k: v
        for k, v in obj.items()
        if k not in (F["LedgerEntryType"], F["PreviousTxnID"], F["PreviousTxnLgrSeq"])
    }


def test_modify_applies_final_fields_and_advances_thread():
    cur = account()
    after = {**cur, F["Balance"]: drops(90), F["Sequence"]: u32(6)}
    r = replayer(cur)
    r.apply_tx(
        meta(node("ModifiedNode", final_of(after), {F["Balance"]: drops(100), F["Sequence"]: u32(5)})), TX, 50
    )
    got = L.parse(r.map.get(KEY))
    assert got[F["Balance"]] == drops(90) and got[F["Sequence"]] == u32(6)
    assert got[F["PreviousTxnID"]] == bytes.fromhex(TX) and got[F["PreviousTxnLgrSeq"]] == u32(50)


def test_modify_removes_field_only_in_previous_and_adds_new_final_field():
    cur = {**account(), F["Domain"]: b"\x02\xaa\xbb"}
    final = final_of(cur)
    del final[F["Domain"]]
    final[F["Sequence"]] = u32(6)
    r = replayer(cur)
    r.apply_tx(
        meta(node("ModifiedNode", final, {F["Domain"]: b"\x02\xaa\xbb", F["Sequence"]: u32(5)})), TX, 50
    )
    got = L.parse(r.map.get(KEY))
    assert F["Domain"] not in got and got[F["Sequence"]] == u32(6)
    # adding a field: in FinalFields, not in PreviousFields, not in the object -> accepted and written
    r2 = replayer(account())
    r2.apply_tx(meta(node("ModifiedNode", {**final_of(account()), F["Domain"]: b"\x01\xcc"}, {})), TX, 50)
    assert L.parse(r2.map.get(KEY))[F["Domain"]] == b"\x01\xcc"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("previous", "PREVIOUS_MISMATCH"),
        ("unchanged", "UNCHANGED_MISMATCH"),
        ("thread", "THREAD_MISMATCH"),
        ("missing", "NODE_MISSING"),
        ("type", "TYPE_MISMATCH"),
    ],
    ids=["previous", "unchanged", "thread", "missing", "type"],
)
def test_modify_refusals_name_their_reason(case, reason):
    cur = account()
    final, prev, thread = final_of({**cur, F["Balance"]: drops(90)}), {F["Balance"]: drops(100)}, OLD_TX
    if case == "previous":
        prev = {F["Balance"]: drops(101)}
    elif case == "unchanged":
        final[F["OwnerCount"]] = u32(3)
    elif case == "thread":
        thread = b"\x0b" * 32
    elif case == "type":
        cur[F["LedgerEntryType"]] = D.entry_types["Offer"].to_bytes(2, "big")
    r = replayer(None if case == "missing" else cur)
    with pytest.raises(R.ReplayError) as e:
        r.apply_tx(meta(node("ModifiedNode", final, prev, thread=thread)), TX, 50)
    assert e.value.reason == reason and e.value.index == KEY.hex().upper()


def test_create_adds_required_zero_defaults_and_thread():
    new = {F["Account"]: b"\x14" + b"\x07" * 20, F["Balance"]: drops(10), F["Sequence"]: u32(50)}
    r = replayer()
    r.apply_tx(meta(node("CreatedNode", new=new)), TX, 50)
    got = L.parse(r.map.get(KEY))
    assert got[F["Flags"]] == u32(0) and got[F["OwnerCount"]] == u32(0)
    assert got[F["PreviousTxnID"]] == bytes.fromhex(TX) and got[F["PreviousTxnLgrSeq"]] == u32(50)
    assert got == {**new, F["LedgerEntryType"]: AR, F["Flags"]: u32(0), F["OwnerCount"]: u32(0),
                   F["PreviousTxnID"]: bytes.fromhex(TX), F["PreviousTxnLgrSeq"]: u32(50)}  # fmt: skip
    with pytest.raises(R.ReplayError, match="CREATE_EXISTS"):
        r.apply_tx(meta(node("CreatedNode", new=new)), TX, 51)


def test_delete_removes_object_and_refuses_missing():
    cur = account()
    r = replayer(cur)
    r.apply_tx(meta(node("DeletedNode", final_of(cur), {})), TX, 50)
    assert r.map.get(KEY) is None and r.map.size == 0
    with pytest.raises(R.ReplayError, match="NODE_MISSING"):
        r.apply_tx(meta(node("DeletedNode", final_of(cur), {})), TX, 51)


def tx_blob(i):
    return "1200" + f"{i:04X}"


def test_ledger_applies_transactions_in_transaction_index_order_not_list_order():
    # two writes to one object: index 0 takes 100 -> 90, index 1 takes 90 -> 80. Pick blobs whose tx ids
    # sort opposite to their TransactionIndex, so sorting by id (or keeping list order) fails.
    ids = {i: L.sha512half(R.PREFIX_TXID, bytes.fromhex(tx_blob(i))).hex().upper() for i in range(40)}
    a, b = next((x, y) for x in ids for y in ids if x != y and ids[x] > ids[y])
    cur = account()
    m0 = meta(
        node("ModifiedNode", final_of({**cur, F["Balance"]: drops(90)}), {F["Balance"]: drops(100)}), index=0
    )
    t0 = bytes.fromhex(ids[a])
    m1 = meta(
        node(
            "ModifiedNode", final_of({**cur, F["Balance"]: drops(80)}), {F["Balance"]: drops(90)}, thread=t0
        ),
        index=1,
    )
    ref = replayer(cur)
    ref.apply_tx(m0, ids[a], 50)
    ref.apply_tx(m1, ids[b], 50)
    ref.advance_skip_lists(50, b"\x11" * 32)
    header = {"parent_hash": b"\x11" * 32, "account_hash": ref.map.root_hash()}
    txs = [  # list order = reverse of TransactionIndex
        {"tx_blob": tx_blob(b), "meta": L.serialize(m1).hex()},
        {"tx_blob": tx_blob(a), "meta": L.serialize(m0).hex()},
    ]
    stats = {"ledgers": 0, "transactions": 0, "nodes": 0, "roots_matched": 0}
    replayer(cur).apply_ledger(50, header, txs, stats)
    assert stats["transactions"] == 2 and stats["roots_matched"] == 1
    gap = [dict(txs[0], meta=L.serialize({**m1, F["TransactionIndex"]: u32(2)}).hex()), txs[1]]
    with pytest.raises(R.ReplayError, match="TX_INDEX_GAP"):
        replayer(cur).apply_ledger(50, header, gap, dict(stats))


def test_short_skip_list_keeps_last_256():
    r = R.Replayer(D, [])
    for n in range(2, 260):
        r.advance_skip_lists(n, n.to_bytes(32, "big"))
    raw = L.parse(r.map.get(R.SKIP_KEY))[D.fid("Hashes")]
    length, j = L.read_vl(raw, 0)
    assert length == 256 * 32
    assert raw[j : j + 32] == (259 - 255).to_bytes(32, "big") and raw[-32:] == (259).to_bytes(32, "big")
