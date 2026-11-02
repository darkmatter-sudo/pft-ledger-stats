import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "payflow"))
import collect_payflow as cp  # noqa: E402

BASE_CT = 815000000  # ripple time


def ledger(idx, txs, ct=None):
    return {
        "ledger_index": str(idx),
        "ledger_hash": f"H{idx}",
        "close_time": ct if ct is not None else BASE_CT + (idx - 100) * 4,
        "closed": True,
        "transactions": txs,
    }


def acct_mod(acct, prev, final):
    return {
        "ModifiedNode": {
            "LedgerEntryType": "AccountRoot",
            "FinalFields": {"Account": acct, "Balance": str(final)},
            "PreviousFields": {"Balance": str(prev)},
        }
    }


def pay(
    i, src="rA", dst="rB", amount="1000", delivered="1000", res="tesSUCCESS", fee="12", flags=0, nodes=None
):
    meta = {
        "TransactionIndex": i,
        "TransactionResult": res,
        "AffectedNodes": nodes
        if nodes is not None
        else [
            acct_mod(src, 50000, 50000 - int(fee) - int(delivered)),
            acct_mod(dst, 7000, 7000 + int(delivered)),
        ],
    }
    if delivered is not None:
        meta["delivered_amount"] = delivered
    return {
        "hash": f"T{i}",
        "Account": src,
        "Destination": dst,
        "TransactionType": "Payment",
        "Amount": amount,
        "Fee": fee,
        "Flags": flags,
        "Sequence": 5,
        "metaData": meta,
    }


def other(i, acct="rC", tt="NFTokenMint", fee="10"):
    return {
        "hash": f"O{i}",
        "Account": acct,
        "TransactionType": tt,
        "Fee": fee,
        "Sequence": 3,
        "metaData": {
            "TransactionIndex": i,
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                acct_mod(acct, 900, 900 - int(fee)),
                {"ModifiedNode": {"LedgerEntryType": "NFTokenPage", "FinalFields": {}, "PreviousFields": {}}},
            ],
        },
    }


def test_payment_rows_use_delivered_amount_not_amount():
    # partial payment: Amount 5000 is only the maximum, 1200 delivered
    txs, pays, deltas = cp.ledger_rows(
        ledger(100, [pay(1, amount="5000", delivered="1200", flags=cp.TF_PARTIAL_PAYMENT), other(0)])
    )
    assert [t["tx_index"] for t in txs] == [0, 1]
    assert len(pays) == 1 and pays[0]["delivered_drops"] == 1200 and pays[0]["partial"] == "y"
    assert pays[0]["kind"] == "native" and pays[0]["destination"] == "rB" and pays[0]["delivered_raw"] == ""
    assert [d["tx_index"] for d in deltas] == [0, 1, 1]  # sorted by tx, metadata order kept inside a tx
    assert [d["account"] for d in deltas[1:]] == ["rA", "rB"]
    assert deltas[1]["delta_drops"] == -1212 and deltas[2]["delta_drops"] == 1200


def test_iou_mpt_and_failed_payments():
    iou = {"currency": "USD", "issuer": "rI", "value": "1.5"}
    t = pay(0, amount=iou, delivered=None, nodes=[acct_mod("rA", 100, 88)])
    t["metaData"]["delivered_amount"] = iou
    mpt = pay(
        1, amount={"mpt_issuance_id": "00AB", "value": "7"}, delivered=None, nodes=[acct_mod("rA", 88, 76)]
    )
    mpt["metaData"]["delivered_amount"] = {"mpt_issuance_id": "00AB", "value": "7"}
    failed = pay(2, delivered=None, res="tecPATH_DRY", nodes=[acct_mod("rA", 76, 64)])
    _, pays, deltas = cp.ledger_rows(ledger(100, [t, mpt, failed]))
    assert [p["kind"] for p in pays] == ["iou", "mpt", "native"]
    assert json.loads(pays[0]["delivered_raw"]) == iou and pays[0]["delivered_drops"] == ""
    assert pays[2]["result"] == "tecPATH_DRY" and pays[2]["delivered_drops"] == ""
    assert [d["delta_drops"] for d in deltas] == [-12, -12, -12]


def test_successful_payment_without_delivered_amount_is_an_error():
    t = pay(0, delivered=None, nodes=[])
    with pytest.raises(ValueError):
        cp.ledger_rows(ledger(100, [t]))
    t["metaData"]["delivered_amount"] = "unavailable"
    with pytest.raises(ValueError):
        cp.ledger_rows(ledger(100, [t]))


def test_delta_nodes_created_deleted_unchanged():
    nodes = [
        {
            "CreatedNode": {
                "LedgerEntryType": "AccountRoot",
                "NewFields": {"Account": "rN", "Balance": "1000000"},
            }
        },
        {
            "DeletedNode": {
                "LedgerEntryType": "AccountRoot",
                "FinalFields": {"Account": "rD", "Balance": "0"},
                "PreviousFields": {"Balance": "4000"},
            }
        },
        {
            "ModifiedNode": {
                "LedgerEntryType": "AccountRoot",
                "FinalFields": {"Account": "rS", "Balance": "10"},
                "PreviousFields": {"Sequence": 4},
            }
        },
        {
            "DeletedNode": {
                "LedgerEntryType": "AccountRoot",
                "FinalFields": {"Account": "rE", "Balance": "33"},
            }
        },
    ]
    d = cp.delta_rows(100, {"hash": "X"}, {"TransactionIndex": 0, "AffectedNodes": nodes})
    assert [(r["account"], r["node"], r["prev_drops"], r["final_drops"], r["delta_drops"]) for r in d] == [
        ("rN", "Created", 0, 1000000, 1000000),
        ("rD", "Deleted", 4000, 0, -4000),
        ("rE", "Deleted", 33, 0, -33),
    ]


def test_meta_variant_missing_and_duplicate_index():
    t = other(0)
    t["meta"] = t.pop("metaData")
    assert cp.ledger_rows(ledger(100, [t]))[0][0]["tx_index"] == 0
    bad = other(0)
    del bad["metaData"]["TransactionIndex"]
    with pytest.raises(ValueError):
        cp.ledger_rows(ledger(100, [bad]))
    with pytest.raises(ValueError):
        cp.ledger_rows(ledger(100, [other(0), pay(0)]))


def test_in_window_boundary():
    end = BASE_CT + cp.RIPPLE_EPOCH
    assert cp.in_window(BASE_CT, end)
    assert not cp.in_window(BASE_CT + 1, end)


def ledger_txs(i):
    """Ledger i holds i % 3 payments and one other tx when i is even."""
    txs = [pay(k, src=f"r{i % 5}", dst=f"r{(i + 1) % 5}") for k in range(i % 3)]
    if i % 2 == 0:
        txs.append(other(i % 3))
    return txs


class FakeRpc:
    """Serves ledgers 100..last; ledger `fail` errors `fail_times` times."""

    def __init__(self, last, fail=None, fail_times=0, kill_after=None):
        self.last, self.fail, self.fail_times, self.kill_after = last, fail, fail_times, kill_after
        self.count = 0

    def __call__(self, *_a, **_k):
        return self

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            return {
                "info": {
                    "validated_ledger": {"seq": self.last},
                    "build_version": "x",
                    "server_state": "full",
                    "complete_ledgers": f"100-{self.last}",
                }
            }
        i = params["ledger_index"]
        if self.kill_after is not None and i > self.kill_after:
            raise KeyboardInterrupt
        if i == self.fail and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        return {"ledger": ledger(i, ledger_txs(i))}


def run(monkeypatch, tmp_path, fake, *args):
    monkeypatch.setattr(cp, "Rpc", fake)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cp, "_stop", False)
    return cp.main(["--out", str(tmp_path), "--no-sudo", *args])


def read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def expected(lo, hi):
    pays = sum(i % 3 for i in range(lo, hi + 1))
    others = sum(1 for i in range(lo, hi + 1) if i % 2 == 0)
    return pays, pays + others, 2 * pays + others


def test_full_run_retries_gaps_and_counts(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, FakeRpc(110, fail=104, fail_times=2), "--start", "100", "--stop-at", "110")
    led = read(tmp_path / "ledgers.csv")
    assert [int(r["ledger_index"]) for r in led] == list(range(100, 111))
    pays, txs, deltas = expected(100, 110)
    assert len(read(tmp_path / "payments.csv")) == pays == sum(int(r["pay_rows"]) for r in led)
    assert len(read(tmp_path / "tx.csv")) == txs == sum(int(r["tx_rows"]) for r in led)
    assert len(read(tmp_path / "deltas.csv")) == deltas == sum(int(r["delta_rows"]) for r in led)
    assert [r["attempt"] for r in read(tmp_path / "retries.csv")] == ["1", "2"]
    assert not (tmp_path / "gaps.csv").exists()
    g = tmp_path / "g"
    run(monkeypatch, g, FakeRpc(106, fail=103, fail_times=9), "--start", "100", "--stop-at", "106")
    assert [r["ledger_index"] for r in read(g / "gaps.csv")] == ["103"]
    assert "103" not in [r["ledger_index"] for r in read(g / "ledgers.csv")]
    assert "103" not in [r["ledger_index"] for r in read(g / "payments.csv")]


def test_resume_after_kill_with_orphans_in_every_file_and_torn_line(monkeypatch, tmp_path):
    with pytest.raises(KeyboardInterrupt):
        run(monkeypatch, tmp_path, FakeRpc(115, kill_after=107), "--start", "100", "--stop-at", "115")
    # simulate a kill after all per-ledger rows of 108 but before its ledger row, plus a torn ledger row
    txs, pays, deltas = cp.ledger_rows(ledger(108, ledger_txs(108)))
    cp.append_rows(tmp_path / "tx.csv", cp.TX_FIELDS, txs)
    cp.append_rows(tmp_path / "payments.csv", cp.PAY_FIELDS, pays)
    cp.append_rows(tmp_path / "deltas.csv", cp.DELTA_FIELDS, deltas)
    with open(tmp_path / "ledgers.csv", "a") as fh:
        fh.write("108,H108,8150")
    run(monkeypatch, tmp_path, FakeRpc(115), "--start", "999", "--stop-at", "999")
    led = [int(r["ledger_index"]) for r in read(tmp_path / "ledgers.csv")]
    assert led == list(range(100, 116))
    p, t, d = expected(100, 115)
    rows = {n: read(tmp_path / n) for n in cp.PER_LEDGER}
    assert (len(rows["payments.csv"]), len(rows["tx.csv"]), len(rows["deltas.csv"])) == (p, t, d)
    assert len({(r["ledger_index"], r["tx_index"]) for r in rows["tx.csv"]}) == t
    rs = read(tmp_path / "restarts.csv")
    assert [r["kind"] for r in rs] == ["start", "resume"]
    assert rs[1]["first_ledger"] == "108" and int(rs[1]["torn_bytes"]) > 0
    assert rs[1]["orphan_rows"] == str(len(txs) + len(pays) + len(deltas))
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["start_seq"] == 100 and st["stop_at"] == 115


def test_window_end_enforced_client_side(monkeypatch, tmp_path):
    monkeypatch.setattr(cp.time, "time", lambda: BASE_CT + cp.RIPPLE_EPOCH - 3600 + 20)
    # 1h window ending at BASE_CT+20 (ripple): ledgers 100..105 close at +0..+20, 106 at +24
    run(monkeypatch, tmp_path, FakeRpc(120), "--start", "100", "--hours", "1", "--stop-at", "120")
    led = [int(r["ledger_index"]) for r in read(tmp_path / "ledgers.csv")]
    assert led == list(range(100, 106))


def test_rate_cap(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run(monkeypatch, tmp_path, FakeRpc(101), "--rate", "2")


def test_non_account_root_nodes_ignored():
    nodes = [
        {"ModifiedNode": {"LedgerEntryType": "RippleState",
                          "FinalFields": {"Balance": {"currency": "USD", "issuer": "rI", "value": "1"}},
                          "PreviousFields": {"Balance": {"currency": "USD", "issuer": "rI", "value": "2"}}}},
        {"ModifiedNode": {"LedgerEntryType": "DirectoryNode", "FinalFields": {"Owner": "rA"}}},
        {"CreatedNode": {"LedgerEntryType": "Offer", "NewFields": {"Account": "rA"}}},
        acct_mod("rA", 100, 90),
    ]
    d = cp.delta_rows(100, {"hash": "X"}, {"TransactionIndex": 0, "AffectedNodes": nodes})
    assert [(r["account"], r["delta_drops"]) for r in d] == [("rA", -10)]
