import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txhygiene"))
import collect_txhygiene as ch  # noqa: E402

BASE_CT = 815000000  # ripple time
KEY = b"k" * 32
ED = "ED" + "A" * 64
SECP = "02" + "B" * 64


def ledger(idx, txs, ct=None):
    return {
        "ledger_index": str(idx),
        "ledger_hash": f"H{idx}",
        "close_time": ct if ct is not None else BASE_CT + (idx - 100) * 4,
        "closed": True,
        "transactions": txs,
    }


def tx(i, acct="rA", spk=ED, lls=None, extra=None, res="tesSUCCESS", fee="12"):
    t = {
        "hash": f"T{i}",
        "Account": acct,
        "TransactionType": "Payment",
        "Amount": "1000",
        "DeliverMax": "1000",
        "Destination": "rB",
        "Fee": fee,
        "Flags": 0,
        "Sequence": 5 + i,
        "SigningPubKey": spk,
        "TxnSignature": "AB",
        "metaData": {"TransactionIndex": i, "TransactionResult": res},
    }
    if lls is not None:
        t["LastLedgerSequence"] = lls
    t.update(extra or {})
    return t


def test_key_type_from_prefix_only():
    assert ch.key_type({"SigningPubKey": ED}) == "ed25519"
    assert ch.key_type({"SigningPubKey": "ed" + "a" * 64}) == "ed25519"
    assert ch.key_type({"SigningPubKey": SECP}) == "secp256k1"
    assert ch.key_type({"SigningPubKey": "03" + "C" * 64}) == "secp256k1"
    assert ch.key_type({"SigningPubKey": "", "Signers": [{}, {}]}) == "multisig"
    assert ch.key_type({"SigningPubKey": ""}) == "none"
    assert ch.key_type({}) == "none"
    assert ch.key_type({"SigningPubKey": "04" + "D" * 128}) == "other"
    assert ch.key_type({"SigningPubKey": "ED" + "A" * 10}) == "other"


def test_tx_row_fields_margin_ticket_and_no_raw_key():
    t = tx(
        0,
        lls=1010,
        extra={
            "Sequence": 0,
            "TicketSequence": 77,
            "NetworkID": 2025,
            "SourceTag": 9,
            "DestinationTag": 1,
            "AccountTxnID": "00",
            "Memos": [{"Memo": {}}, {"Memo": {}}],
            "Flags": 2147483648,
        },
    )
    r = ch.tx_row(1000, BASE_CT, t, t["metaData"], KEY)
    assert list(r) == ch.TX_FIELDS
    assert (r["sequence"], r["ticket_seq"], r["lls"], r["lls_margin"]) == (0, 77, 1010, 10)
    assert (r["network_id"], r["source_tag"], r["dest_tag"], r["account_txn_id"]) == (2025, "y", "y", "y")
    assert (r["memos"], r["flags"], r["key_type"], r["signers"]) == (2, 2147483648, "ed25519", 0)
    assert r["account"] == ch.hash_account(KEY, "rA") and len(r["account"]) == 16
    blob = json.dumps(r)
    assert "rA" not in blob.replace(r["account"], "") and ED not in blob
    # fieldset: client fields only, sorted; server-added DeliverMax/hash/metaData excluded
    fs = r["fieldset"].split("+")
    assert fs == sorted(fs)
    assert "DeliverMax" not in fs and "hash" not in fs and "metaData" not in fs
    assert {"LastLedgerSequence", "TicketSequence", "SigningPubKey", "TxnSignature"} <= set(fs)


def test_tx_row_absent_fields_are_null():
    t = tx(0)
    del t["Sequence"]
    r = ch.tx_row(1000, BASE_CT, t, t["metaData"], KEY)
    assert (r["sequence"], r["ticket_seq"], r["lls"], r["lls_margin"], r["network_id"]) == (None,) * 5
    assert (r["source_tag"], r["dest_tag"], r["memos"]) == ("n", "n", 0)


def test_margin_negative_never_happens_but_is_kept():
    # a validated tx always has LastLedgerSequence >= its ledger; the collector records, never "fixes"
    t = tx(0, lls=999)
    assert ch.tx_row(1000, BASE_CT, t, t["metaData"], KEY)["lls_margin"] == -1


def test_ledger_rows_sorted_and_duplicate_index_rejected():
    led = ledger(100, [tx(2), tx(0), tx(1)])
    assert [r["tx_index"] for r in ch.ledger_rows(led, KEY)] == [0, 1, 2]
    with pytest.raises(ValueError):
        ch.ledger_rows(ledger(100, [tx(0), tx(0)]), KEY)
    bad = tx(0)
    bad["metaData"] = {}
    with pytest.raises(ValueError):
        ch.ledger_rows(ledger(100, [bad]), KEY)
    m = tx(0)
    m["meta"] = m.pop("metaData")
    assert len(ch.ledger_rows(ledger(100, [m]), KEY)) == 1


def test_fee_row_parses_string_drops():
    f = {
        "drops": {"base_fee": "10", "median_fee": "5000", "minimum_fee": "10", "open_ledger_fee": "256"},
        "ledger_current_index": 7,
        "current_queue_size": "3",
        "max_queue_size": "2000",
        "expected_ledger_size": "32",
        "current_ledger_size": "40",
    }
    r = ch.fee_row(6, f)
    assert list(r) == ch.FEE_FIELDS
    assert (r["base_fee"], r["open_ledger_fee"], r["current_queue_size"], r["current_ledger_size"]) == (
        10,
        256,
        3,
        40,
    )


def test_in_window_boundary():
    end = BASE_CT + ch.RIPPLE_EPOCH
    assert ch.in_window(BASE_CT, end)
    assert not ch.in_window(BASE_CT + 1, end)


def test_append_jsonl_and_drop_orphans(tmp_path):
    p = str(tmp_path / "t.jsonl")
    ch.append_jsonl(p, [{"ledger_index": 1}, {"ledger_index": 2}])
    ch.append_jsonl(p, [])
    ch.append_jsonl(p, [{"ledger_index": 3}, {"ledger_index": 3}])
    assert ch.count_lines(p) == 4 and ch.last_ledger(p) == 3
    assert ch.drop_orphans(p, 3) == 0
    assert ch.drop_orphans(p, 1) == 3
    assert ch.read_jsonl(p) == [{"ledger_index": 1}]
    assert ch.drop_orphans(p, 0) == 1 and ch.read_jsonl(p) == []
    assert ch.drop_orphans(str(tmp_path / "missing"), 5) == 0
    assert ch.last_ledger(str(tmp_path / "missing")) is None


def ledger_txs(i):
    """Ledger i holds i % 3 txs, alternating ed25519 / secp256k1 signers."""
    return [tx(k, acct=f"r{i % 5}", spk=ED if k % 2 else SECP, lls=i + 4 + k) for k in range(i % 3)]


class FakeRpc:
    """Serves ledgers 100..last; ledger `fail` errors `fail_times` times; fee answers count."""

    def __init__(self, last, fail=None, fail_times=0, kill_after=None, grow=None):
        self.last, self.fail, self.fail_times, self.kill_after = last, fail, fail_times, kill_after
        self.grow = grow  # validated advances by one per server_info call up to this ledger
        self.count = 0
        self.fees = 0

    def __call__(self, *_a, **_k):
        return self

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            if self.grow is not None and self.count > 1 and self.last < self.grow:
                self.last += 1
            return {
                "info": {
                    "validated_ledger": {"seq": self.last},
                    "build_version": "x",
                    "server_state": "full",
                    "complete_ledgers": f"100-{self.last}",
                }
            }
        if method == "fee":
            self.fees += 1
            return {
                "drops": {
                    "base_fee": "10",
                    "median_fee": "5000",
                    "minimum_fee": "10",
                    "open_ledger_fee": "10",
                },
                "ledger_current_index": self.last + 1,
            }
        i = params["ledger_index"]
        if self.kill_after is not None and i > self.kill_after:
            raise KeyboardInterrupt
        if i == self.fail and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        return {"ledger": ledger(i, ledger_txs(i))}


def run(monkeypatch, tmp_path, fake, *args):
    monkeypatch.setattr(ch, "Rpc", fake)
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)
    monkeypatch.setattr(ch, "_stop", False)
    return ch.main(["--out", str(tmp_path), "--no-sudo", "--key-file", str(tmp_path / "k"), *args])


def expected(lo, hi):
    return sum(i % 3 for i in range(lo, hi + 1))


def test_full_run_retries_gaps_and_counts(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, FakeRpc(110, fail=104, fail_times=2), "--start", "100", "--stop-at", "110")
    led = ch.read_jsonl(tmp_path / "ledgers.jsonl")
    assert [r["ledger_index"] for r in led] == list(range(100, 111))
    txs = ch.read_jsonl(tmp_path / "tx.jsonl")
    assert (
        len(txs) == expected(100, 110) == sum(r["tx_rows"] for r in led) == sum(r["txn_count"] for r in led)
    )
    assert [r["attempt"] for r in ch.read_jsonl(tmp_path / "retries.jsonl")] == [1, 2]
    assert not (tmp_path / "gaps.jsonl").exists()
    assert {r["key_type"] for r in txs} == {"ed25519", "secp256k1"}
    assert oct(os.stat(tmp_path / "k").st_mode & 0o777) == "0o600"
    g = tmp_path / "g"
    run(monkeypatch, g, FakeRpc(106, fail=103, fail_times=9), "--start", "100", "--stop-at", "106")
    assert [r["ledger_index"] for r in ch.read_jsonl(g / "gaps.jsonl")] == [103]
    assert 103 not in [r["ledger_index"] for r in ch.read_jsonl(g / "ledgers.jsonl")]
    assert 103 not in [r["ledger_index"] for r in ch.read_jsonl(g / "tx.jsonl")]


def test_resume_after_kill_with_orphans_and_torn_lines(monkeypatch, tmp_path):
    with pytest.raises(KeyboardInterrupt):
        run(monkeypatch, tmp_path, FakeRpc(115, kill_after=106), "--start", "100", "--stop-at", "115")
    # simulate a kill after the tx lines of 107 but before its ledger line, plus torn lines in both files
    rows = ch.ledger_rows(ledger(107, ledger_txs(107)), KEY)
    assert rows
    ch.append_jsonl(tmp_path / "tx.jsonl", rows)
    with open(tmp_path / "tx.jsonl", "a") as fh:
        fh.write('{"ledger_index":108,"tx_ind')
    with open(tmp_path / "ledgers.jsonl", "a") as fh:
        fh.write('{"ledger_index":107,"ledg')
    run(monkeypatch, tmp_path, FakeRpc(115), "--start", "999", "--stop-at", "999")
    led = [r["ledger_index"] for r in ch.read_jsonl(tmp_path / "ledgers.jsonl")]
    assert led == list(range(100, 116))
    txs = ch.read_jsonl(tmp_path / "tx.jsonl")
    assert len(txs) == expected(100, 115)
    assert len({(r["ledger_index"], r["tx_index"]) for r in txs}) == len(txs)
    rs = ch.read_jsonl(tmp_path / "restarts.jsonl")
    assert [r["kind"] for r in rs] == ["start", "resume"]
    assert rs[1]["first_ledger"] == 107 and rs[1]["torn_bytes"] > 0
    assert rs[1]["orphan_rows"] == len(rows)
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["start_seq"] == 100 and st["stop_at"] == 115


def test_resume_is_byte_identical_to_uninterrupted(monkeypatch, tmp_path):
    ref, drl = tmp_path / "ref", tmp_path / "drl"
    run(monkeypatch, ref, FakeRpc(112), "--start", "100", "--stop-at", "112", "--no-fees")
    same_key = ("--key-file", str(tmp_path / "k"))  # run() names <out>/k; the last --key-file wins
    os.replace(ref / "k", tmp_path / "k")
    ref2 = tmp_path / "ref2"
    run(monkeypatch, ref2, FakeRpc(112), "--start", "100", "--stop-at", "112", "--no-fees", *same_key)
    with pytest.raises(KeyboardInterrupt):
        run(
            monkeypatch,
            drl,
            FakeRpc(112, kill_after=105),
            "--start",
            "100",
            "--stop-at",
            "112",
            "--no-fees",
            *same_key,
        )
    assert (drl / "tx.jsonl").read_bytes() != (ref2 / "tx.jsonl").read_bytes()
    run(monkeypatch, drl, FakeRpc(112), "--no-fees", *same_key)
    assert (ref2 / "tx.jsonl").read_bytes() == (drl / "tx.jsonl").read_bytes()


def test_fee_sampled_once_per_new_validated_ledger(monkeypatch, tmp_path):
    fake = FakeRpc(100, grow=104)
    run(monkeypatch, tmp_path, fake, "--start", "100", "--stop-at", "104")
    fees = ch.read_jsonl(tmp_path / "fees.jsonl")
    assert fake.fees == len(fees) >= 1
    assert len({r["validated"] for r in fees}) == len(fees)  # never twice for one validated ledger
    n = tmp_path / "nf"
    fake2 = FakeRpc(100, grow=104)
    run(monkeypatch, n, fake2, "--start", "100", "--stop-at", "104", "--no-fees")
    assert fake2.fees == 0 and not (n / "fees.jsonl").exists()


def test_window_end_enforced_client_side(monkeypatch, tmp_path):
    monkeypatch.setattr(ch.time, "time", lambda: BASE_CT + ch.RIPPLE_EPOCH - 3600 + 20)
    # 1h window ending at BASE_CT+20 (ripple): ledgers 100..105 close at +0..+20, 106 at +24
    run(monkeypatch, tmp_path, FakeRpc(120), "--start", "100", "--hours", "1", "--stop-at", "120")
    led = [r["ledger_index"] for r in ch.read_jsonl(tmp_path / "ledgers.jsonl")]
    assert led == list(range(100, 106))


def test_rate_cap(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run(monkeypatch, tmp_path, FakeRpc(101), "--rate", "2")


def test_rpc_refuses_write_methods():
    r = ch.Rpc("c", 5005, 1.0, ["false"])
    for m in ("submit", "sign", "ledger_accept", "stop"):
        with pytest.raises(ValueError):
            r.call(m)
    assert r.count == 0
