import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "txhygiene"))
import reconcile_txhygiene as rc  # noqa: E402


def data(n=6):
    """Ledgers 100..100+n-1, ledger i holds i % 3 txs."""
    ledgers, txs = [], []
    for i in range(100, 100 + n):
        k = i % 3
        ledgers.append({"ledger_index": i, "ledger_hash": f"H{i}", "txn_count": k, "tx_rows": k})
        for j in range(k):
            txs.append(
                {
                    "ledger_index": i,
                    "tx_index": j,
                    "tx_hash": f"T{i}-{j}",
                    "tx_type": "Payment",
                    "result": "tesSUCCESS",
                    "fee_drops": 10 + j,
                }
            )
    return ledgers, txs


def twin(ledgers, txs):
    tled = [
        {
            "ledger_index": str(r["ledger_index"]),
            "ledger_hash": r["ledger_hash"],
            "txn_count": str(r["txn_count"]),
        }
        for r in ledgers
    ]
    ttx = [
        {
            "ledger_index": str(t["ledger_index"]),
            "tx_index": str(t["tx_index"]),
            "tx_hash": t["tx_hash"],
            "tx_type": t["tx_type"],
            "result": t["result"],
            "fee_drops": str(t["fee_drops"]),
        }
        for t in txs
    ]
    return tled, ttx


def failed(results):
    return {name for name, ok, _ in results if not ok}


def test_clean_data_passes_both_parts():
    ledgers, txs = data()
    assert failed(rc.internal(ledgers, txs, [], 100)) == set()
    res, common = rc.against_twin(ledgers, txs, *twin(ledgers, txs))
    assert failed(res) == set()
    assert (common["lo"], common["hi"], common["ledgers"]) == (100, 105, 6)
    assert common["totals"]["tx"] == len(txs) and common["totals"]["fee_sum_drops"] == sum(
        t["fee_drops"] for t in txs
    )


def test_internal_detects_each_fault():
    ledgers, txs = data()
    assert "ledgers start_seq..last exactly once" in failed(rc.internal(ledgers[1:], txs, [], 100))
    assert "ledgers start_seq..last exactly once" in failed(rc.internal(ledgers + ledgers[-1:], txs, [], 100))
    assert "no gaps recorded" in failed(rc.internal(ledgers, txs, [{"ledger_index": 99}], 100))
    assert "every tx once (hash and (ledger, tx_index))" in failed(
        rc.internal(ledgers, txs + txs[:1], [], 100)
    )
    orphan = {"ledger_index": 999, "tx_index": 0, "tx_hash": "X", "fee_drops": 1}
    assert "no tx line without its ledger line" in failed(rc.internal(ledgers, txs + [orphan], [], 100))
    short = [t for t in txs if t["tx_hash"] != "T101-0"]
    f = failed(rc.internal(ledgers, short, [], 100))
    assert "per ledger txn_count == tx_rows == tx lines" in f and "tx_index 0..n-1 in every ledger" in f


def test_twin_detects_hash_count_tx_and_total_differences():
    ledgers, txs = data()
    tled, ttx = twin(ledgers, txs)
    tled[2]["ledger_hash"] = "BAD"
    tled[4]["txn_count"] = "9"
    ttx[0]["fee_drops"] = "999"
    ttx.append(
        {
            "ledger_index": "104",
            "tx_index": "5",
            "tx_hash": "EXTRA",
            "tx_type": "AccountSet",
            "result": "tesSUCCESS",
            "fee_drops": "10",
        }
    )
    f = failed(rc.against_twin(ledgers, txs, tled, ttx)[0])
    assert {
        "per-ledger ledger_hash equal",
        "per-ledger txn_count equal",
        "same tx hashes",
        "per tx (ledger, tx_index, type, result, fee) equal",
        "window total tx equal",
        "window total by_type equal",
        "window total fee_sum_drops equal",
    } <= f


def test_twin_compares_only_the_common_range():
    ledgers, txs = data(8)
    tled, ttx = twin(ledgers[2:], [t for t in txs if t["ledger_index"] >= 102])
    res, common = rc.against_twin(ledgers, txs, tled, ttx)
    assert failed(res) == set() and (common["lo"], common["hi"]) == (102, 107)
    assert failed(rc.against_twin(ledgers, txs, [], [])[0]) == {"common ledger range not empty"}


def test_plant_drops_one_ledger_and_bumps_one_fee():
    ledgers, txs = data(30)
    _, kept, drop, victim = rc.plant(ledgers, txs, 1)
    assert all(t["ledger_index"] != drop for t in kept) and any(t["ledger_index"] == drop for t in txs)
    bumped = [t for t in kept if t["tx_hash"] == victim][0]
    orig = [t for t in txs if t["tx_hash"] == victim][0]
    assert bumped["fee_drops"] == orig["fee_drops"] + 1
