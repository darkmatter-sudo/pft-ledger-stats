import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txhygiene"))
import analyze_txhygiene as an  # noqa: E402
import collect_txhygiene as ch  # noqa: E402


def test_quantile_known_answers():
    assert an.quantile([4, 1, 3, 2], 0.5) == 2.5
    assert an.quantile([1, 2, 3, 4], 0.25) == 1.75
    assert an.quantile([1, 2, 3, 4], 0.0) == 1 and an.quantile([1, 2, 3, 4], 1.0) == 4
    assert an.quantile([7], 0.9) == 7
    assert an.quantile([], 0.5) is None


def test_gini_known_answers():
    assert an.gini([1, 1, 1, 1]) == pytest.approx(0.0)
    assert an.gini([90, 10]) == pytest.approx(0.4)  # hand: 2*(1*10+2*90)/(2*100) - 3/2
    assert an.gini([0, 0, 0, 10]) == pytest.approx(0.75)  # hand: 2*40/40 - 5/4
    assert an.gini([0, 0]) is None and an.gini([]) is None


def test_top_share_and_lorenz():
    assert an.top_share([5, 3, 2], 1) == pytest.approx(0.5)
    assert an.top_share([5, 3, 2], 2) == pytest.approx(0.8)
    assert an.top_share([0, 0], 1) is None
    assert an.lorenz([3, 1]) == [(0.0, 0.0), (0.5, 0.25), (1.0, 1.0)]


@pytest.mark.parametrize(
    "m,label",
    [
        (-1, "<0"),
        (0, "0-1"),
        (1, "0-1"),
        (2, "2-4"),
        (4, "2-4"),
        (5, "5-10"),
        (10, "5-10"),
        (11, "11-20"),
        (20, "11-20"),
        (21, "21-100"),
        (100, "21-100"),
        (101, ">100"),
    ],
)
def test_margin_bucket_edges(m, label):
    assert an.margin_bucket(m) == label


@pytest.mark.parametrize(
    "r,label",
    [
        (0.5, "<1x"),
        (1, "1x (exact)"),
        (1.2, ">1-2x"),
        (2, ">1-2x"),
        (2.01, ">2-10x"),
        (10, ">2-10x"),
        (100, ">10-100x"),
        (101, ">100x"),
    ],
)
def test_ratio_bucket_edges(r, label):
    assert an.ratio_bucket(r) == label


def test_required_fee_and_sample_matching():
    assert an.required_fee(10, 0) == 10 and an.required_fee(10, 3) == 40
    samples = [{"validated": v} for v in (5, 7, 9)]
    assert an.fee_for_ledger(samples, 8)["validated"] == 7
    assert an.fee_for_ledger(samples, 7)["validated"] == 5  # sampled while ledger 7 was still open
    assert an.fee_for_ledger(samples, 5) is None
    assert an.fee_for_ledger(samples, 100)["validated"] == 9


def t(
    acct,
    led,
    idx=0,
    fee=10,
    seq=1,
    lls=None,
    kt="ed25519",
    fs="Account+Fee+Sequence",
    ticket=None,
    signers=0,
    tt="Payment",
    flags=0,
    memos=0,
    net=2025,
):
    return {
        "account": acct,
        "ledger_index": led,
        "tx_index": idx,
        "fee_drops": fee,
        "sequence": seq,
        "lls": lls,
        "lls_margin": None if lls is None else lls - led,
        "key_type": kt,
        "fieldset": fs,
        "ticket_seq": ticket,
        "signers": signers,
        "tx_type": tt,
        "result": "tesSUCCESS",
        "flags": flags,
        "memos": memos,
        "network_id": net,
        "source_tag": "n",
        "dest_tag": "n",
        "account_txn_id": "n",
        "tx_hash": f"{acct}{led}{idx}",
    }


def test_expiry_counts_margins_and_missing_lls():
    txs = [t("a", 100, lls=104), t("b", 100, idx=1, lls=120), t("c", 101), t("c", 102)]
    ex = an.expiry(txs)
    assert (ex["with_lls"], ex["no_lls"], ex["no_lls_accounts"], ex["no_lls_pct"]) == (2, 2, 1, 50.0)
    assert ex["margin_values"] == {"4": 1, "20": 1}
    assert ex["buckets"]["2-4"] == 1 and ex["buckets"]["11-20"] == 1 and ex["buckets"][">100"] == 0
    assert ex["margin_quantiles"][0.5] == 12.0


def test_fees_overpay_concentration_hand_computed():
    samples = [
        {"validated": 99, "base_fee": 10, "open_ledger_fee": 10, "current_queue_size": 0},
        {"validated": 100, "base_fee": 10, "open_ledger_fee": 50, "current_queue_size": 4},
    ]
    txs = [
        t("a", 100, fee=100),
        t("b", 100, idx=1, fee=20),
        t("c", 101, fee=40, signers=3),
        t("d", 101, fee=10),
    ]
    fe = an.fees(txs, samples)
    # a: 90 over (ratio 10), b: 10 over (ratio 2), c: 40 = 10*(1+3) exact, d: exact
    assert fe["overpay_drops_total"] == 100 and fe["overpay_tx"] == 2
    assert fe["ratio_buckets"]["1x (exact)"] == 2 and fe["ratio_buckets"][">1-2x"] == 1
    assert fe["ratio_buckets"][">2-10x"] == 1
    # hand: sorted [0, 0, 10, 90], 2*(3*10+4*90)/(4*100) - 5/4 = 0.7
    assert fe["overpay_gini"] == pytest.approx(0.7)
    assert fe["overpay_top1_share"] == pytest.approx(0.9)
    # ledger 101 matches the sample with open-ledger fee 50 (x4 for c): c pays 40 < 200, d pays 10 < 50
    assert fe["paid_below_open_ledger_fee"] == 2
    assert fe["escalated_samples"] == 1 and fe["queue_max"] == 4
    assert [r["account"] for r in fe["overpay_top_accounts"]] == ["a", "b"]


def test_sequencing_steps():
    txs = [
        t("a", 100, seq=5),
        t("a", 101, seq=6),
        t("a", 105, seq=9),
        t("b", 100, idx=1, seq=3),
        t("b", 100, idx=2, seq=3),
        t("c", 100, idx=3, seq=0, ticket=12),
    ]
    sq = an.sequencing(txs)
    assert (sq["ticket_tx"], sq["sequence_tx"]) == (1, 5)
    assert sq["steps"] == {"+1": 1, ">+1": 1, "<=0": 1}
    assert sq["tx_per_account_ledger"] == {"1": 3, "2": 1}


def test_signing_optional_fields_and_classes():
    txs = [
        t("a", 100, fs="Account+Fee+Flags+LastLedgerSequence+NetworkID", flags=0x80000000),
        t("a", 101, kt="secp256k1", fs="Account+Fee"),
        t("b", 101, idx=1, kt="multisig", signers=2, fs="Account+Fee+Memos", memos=1, net=None),
    ]
    sg = an.signing(txs)
    assert sg["by_tx"] == {"ed25519": 1, "multisig": 1, "secp256k1": 1}
    assert sg["by_account"] == {"ed25519+secp256k1": 1, "multisig": 1}
    assert sg["multisig_signer_counts"] == {2: 1} and sg["fully_canonical_flag_tx"] == 1
    of = an.optional_fields(txs)
    assert (of["no_network_id"], of["memos"], of["flags_nonzero"], of["flags_field_absent"]) == (1, 1, 1, 2)
    fp = an.fingerprints(txs)
    assert {c["class"] for c in fp["classes"]} == {
        "ed25519: Flags+LastLedgerSequence+NetworkID",
        "secp256k1: -",
        "multisig: Memos",
    }
    assert fp["accounts_with_several_classes"] == 1


def test_end_to_end_report_uses_only_short_tokens(tmp_path):
    key = b"k" * 32
    acct = ch.hash_account(key, "rSecretAccount")
    txs = [
        dict(t(acct, 100, lls=118), tx_hash="H1"),
        dict(t(acct, 101, seq=2, fee=12, lls=119), tx_hash="H2"),
    ]
    ch.append_jsonl(tmp_path / "tx.jsonl", txs)
    ch.append_jsonl(
        tmp_path / "fees.jsonl",
        [{"validated": 99, "base_fee": 10, "open_ledger_fee": 10, "current_queue_size": 0}],
    )
    ch.append_jsonl(
        tmp_path / "ledgers.jsonl",
        [{"ledger_index": i, "close_time_iso": f"T{i}", "txn_count": 1, "tx_rows": 1} for i in (100, 101)],
    )
    (tmp_path / "state.json").write_text(json.dumps({"start_utc": "S", "end_utc": "E", "start_seq": 100}))
    out = tmp_path / "out"
    assert an.main(["--data", str(tmp_path), "--out", str(out)]) == 0
    rep = (out / "REPORT.md").read_text()
    js = (out / "txhygiene.json").read_text()
    assert "Exact margins: 18: 2." in rep and "overpay 2 of 22 fee drops" in rep
    assert acct not in rep and acct not in js and acct[:8] in rep
    assert "_overpay_by_account" not in js
