import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txhygiene"))
import collect_txhygiene as ch  # noqa: E402
import crosscheck_txhygiene as xc  # noqa: E402

KEY = b"k" * 32


def public_answer(validated=True):
    return {
        "Account": "rA",
        "TransactionType": "Payment",
        "Amount": "5",
        "DeliverMax": "5",
        "Destination": "rB",
        "Fee": "12",
        "Flags": 0,
        "Sequence": 9,
        "LastLedgerSequence": 120,
        "NetworkID": 2025,
        "SigningPubKey": "ED" + "A" * 64,
        "TxnSignature": "AB",
        "hash": "H1",
        "ctid": "C0",
        "date": 815000000,
        "inLedger": 100,
        "ledger_index": 100,
        "validated": validated,
        "status": "success",
        "meta": {"TransactionIndex": 3, "TransactionResult": "tesSUCCESS"},
    }


def local_row():
    a = public_answer()
    tx = {k: v for k, v in a.items() if k not in ("status", "meta", "ctid", "date", "inLedger", "validated")}
    tx["ledger_index"] = "100"  # the ledger RPC's expanded tx carries no ledger_index; harmless either way
    return ch.tx_row(100, 815000000, tx, a["meta"], KEY)


def test_ranges_and_covered():
    rs = xc.ranges("5-9,12,20-21")
    assert rs == [(5, 9), (12, 12), (20, 21)]
    assert xc.covered(5, rs) and xc.covered(12, rs) and xc.covered(21, rs)
    assert not xc.covered(10, rs) and not xc.covered(4, rs)
    assert xc.ranges("empty") == [] and xc.ranges(None) == []


def test_remote_row_strips_response_envelope_and_matches_local():
    r = xc.remote_row(public_answer(), KEY)
    assert "status" not in r["fieldset"] and "ctid" not in r["fieldset"]
    assert xc.compare(local_row(), r) == []


def test_compare_reports_each_differing_field():
    loc, rem = local_row(), xc.remote_row(public_answer(), KEY)
    loc["fee_drops"] = 13
    loc["lls_margin"] = None
    assert [f for f, _, _ in xc.compare(loc, rem)] == ["fee_drops", "lls_margin"]


def test_alter_changes_every_kind_including_null():
    for v in (None, 0, 7, "", "abc", "n"):
        assert xc.alter(v) != v
    row = local_row()
    neg = {f: xc.alter(v) for f, v in row.items()}
    assert len(xc.compare(neg, xc.remote_row(public_answer(), KEY))) == len(ch.TX_FIELDS)


def test_sample_is_seeded_and_sorted():
    rows = [{"ledger_index": i // 2, "tx_index": i % 2, "tx_hash": str(i)} for i in range(50)]
    a, b = xc.sample(rows, 20, 1), xc.sample(rows, 20, 1)
    assert a == b and len(a) == 20
    assert a == sorted(a, key=lambda r: (r["ledger_index"], r["tx_index"]))
    assert len(xc.sample(rows[:3], 20, 1)) == 3


class FakeRpc:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def call(self, method, params):
        self.calls += 1
        return self.answers.pop(0)


def test_fetch_retries_incomplete_then_succeeds_or_fails():
    waits = []
    rpc = FakeRpc([public_answer(validated=False), public_answer()])
    assert xc.fetch(rpc, "H1", sleep=waits.append)["validated"] is True
    assert rpc.calls == 2 and waits == [5.0]
    rpc = FakeRpc([public_answer(validated=False)] * 4)
    with pytest.raises(xc.RpcError):
        xc.fetch(rpc, "H1", tries=4, sleep=waits.append)
    no_meta = public_answer()
    del no_meta["meta"]
    with pytest.raises(xc.RpcError):
        xc.fetch(FakeRpc([no_meta]), "H1", tries=1, sleep=waits.append)


def test_offline_rechecks_recorded_rows(tmp_path):
    row = local_row()
    ch.append_jsonl(tmp_path / "tx.jsonl", [row])
    rec = {
        "count": 1,
        "negative": False,
        "results": [{"tx_hash": row["tx_hash"], "ledger_index": 100, "rpc_row": dict(row)}],
    }
    (tmp_path / "r.json").write_text(json.dumps(rec))
    assert xc.main(["--data", str(tmp_path), "--offline", str(tmp_path / "r.json")]) == 0
    rec["results"][0]["rpc_row"]["fee_drops"] = 999
    (tmp_path / "r.json").write_text(json.dumps(rec))
    assert xc.main(["--data", str(tmp_path), "--offline", str(tmp_path / "r.json")]) == 1
    rec["results"][0]["rpc_row"] = None
    (tmp_path / "r.json").write_text(json.dumps(rec))
    assert xc.main(["--data", str(tmp_path), "--offline", str(tmp_path / "r.json")]) == 1
