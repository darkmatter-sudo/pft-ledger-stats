import csv
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "payflow"))
import analyze_payflow as ap  # noqa: E402

PAYER = "rPAYERxxxxxxxxxxxxxxxxxxxxxxxxxx"
R1 = "rRECIPIENT1xxxxxxxxxxxxxxxxxxxxx"
R2 = "rRECIPIENT2xxxxxxxxxxxxxxxxxxxxx"
D = "rDESTINATIONxxxxxxxxxxxxxxxxxxxx"
CT0 = 846900000


class World:
    """Builds consistent tx / payments / deltas / ledgers rows, one tx per ledger."""

    def __init__(self, opening):
        self.bal = dict(opening)
        self.txs, self.pays, self.deltas, self.ledgers = [], [], [], []
        self.li = 1000

    def _ledger(self, ntx):
        self.ledgers.append(
            {
                "ledger_index": str(self.li),
                "close_time": str(CT0 + (self.li - 1000) * 60),
                "txn_count": str(ntx),
            }
        )

    def _delta(self, h, acct, d):
        prev = self.bal.get(acct, 0)
        self.bal[acct] = prev + d
        self.deltas.append(
            {
                "ledger_index": str(self.li),
                "tx_index": "0",
                "tx_hash": h,
                "account": acct,
                "node": "Modified",
                "prev_drops": str(prev),
                "final_drops": str(prev + d),
                "delta_drops": str(d),
            }
        )

    def pay(self, src, dst, amt, fee=10, res="tesSUCCESS", kind="native"):
        self.li += 1
        h = f"H{self.li}"
        ok = res == "tesSUCCESS"
        self.txs.append(
            {
                "ledger_index": str(self.li),
                "tx_index": "0",
                "tx_hash": h,
                "account": src,
                "tx_type": "Payment",
                "result": res,
                "fee_drops": str(fee),
            }
        )
        self.pays.append(
            {
                "ledger_index": str(self.li),
                "close_time": str(CT0 + (self.li - 1000) * 60),
                "tx_index": "0",
                "tx_hash": h,
                "account": src,
                "destination": dst,
                "result": res,
                "fee_drops": str(fee),
                "kind": kind,
                "delivered_drops": str(amt) if ok and kind == "native" else "",
                "partial": "n",
            }
        )
        moved = amt if ok and kind == "native" else 0
        self._delta(h, src, -(moved + fee))
        if moved:
            self._delta(h, dst, moved)
        self._ledger(1)
        return h

    def other(self, acct, d, fee=10, tx_type="OfferCreate"):
        self.li += 1
        h = f"H{self.li}"
        self.txs.append(
            {
                "ledger_index": str(self.li),
                "tx_index": "0",
                "tx_hash": h,
                "account": acct,
                "tx_type": tx_type,
                "result": "tesSUCCESS",
                "fee_drops": str(fee),
            }
        )
        self._delta(h, acct, d - fee)
        self._ledger(1)

    def empty(self):
        self.li += 1
        self._ledger(0)

    def run(self, opening, **kw):
        return ap.analyze(self.txs, self.pays, self.deltas, self.ledgers, [], PAYER, opening, **kw)


@pytest.fixture
def fifo_world():
    w = World({PAYER: 10**9, R1: 30, D: 0})
    w.pay(PAYER, R1, 100)  # lot 1 at minute 1
    w.empty()
    w.pay(PAYER, R1, 50)  # lot 2 at minute 3
    w.pay(R1, D, 120)  # minute 4: delivered 120 + fee 10
    return w


def test_fifo_reward_first(fifo_world):
    o = fifo_world.run({R1: 30})["onward"]["reward_first"]
    assert o["received"] == 150 and o["lots"] == 2
    assert (o["onward"], o["fee"], o["other"], o["retained"]) == (120, 10, 0, 20)
    assert o["conserved"] and o["mismatches"] == []
    # 100 from lot 1 held 3 min, 20 from lot 2 held 1 min -> weighted median 180 s
    assert o["hold_median_s"] == 180
    assert o["top_destinations"][0]["drops"] == 120


def test_fifo_balance_first(fifo_world):
    o = fifo_world.run({R1: 30})["onward"]["balance_first"]
    # untagged 30 covers 30 of the 120 delivered, lot 1 the remaining 90, then the fee
    assert (o["onward"], o["fee"], o["other"], o["retained"]) == (90, 10, 0, 50)
    assert o["mismatches"] == []


def test_lower_bound_never_exceeds_upper(fifo_world):
    on = fifo_world.run({R1: 30})["onward"]
    assert on["balance_first"]["onward"] <= on["reward_first"]["onward"]


def test_onward_to_payer_and_other_recipient():
    w = World({PAYER: 10**9, R1: 0, R2: 0})
    w.pay(PAYER, R1, 1000)
    w.pay(PAYER, R2, 1000)
    w.pay(R1, R2, 300)
    w.pay(R1, PAYER, 200)
    o = w.run({R1: 0, R2: 0})["onward"]["reward_first"]
    assert o["onward_to_other_recipients"] == 300
    assert o["onward_to_payer"] == 200
    # R2's 300 from R1 is not a reward lot; R2 keeps its own 1000 lot untouched
    assert o["received"] == 2000 and o["retained"] == 2000 - 300 - 200 - 20
    assert o["conserved"]


def test_non_payment_outflow_is_other():
    w = World({PAYER: 10**9, R1: 0})
    w.pay(PAYER, R1, 500)
    w.other(R1, -200)  # e.g. an offer taking 200 drops, plus fee 10
    o = w.run({R1: 0})["onward"]["reward_first"]
    assert (o["onward"], o["fee"], o["other"], o["retained"]) == (0, 10, 200, 290)


def test_prev_balance_mismatch_detected():
    w = World({PAYER: 10**9, R1: 30})
    w.pay(PAYER, R1, 100)
    on = w.run({R1: 31})["onward"]  # wrong opening balance
    for pol in ap.POLICIES:
        assert [m["cause"] for m in on[pol]["mismatches"]] == ["prev_balance"]


def test_missing_opening_balance_detected():
    w = World({PAYER: 10**9, R1: 500})
    w.pay(PAYER, R1, 100)
    w.pay(R1, D, 550)
    on = w.run({})["onward"]  # R1 not in the snapshot: opening assumed 0
    assert on["opening_missing"] == [ap.truncate(R1)]
    assert on["reward_first"]["mismatches"]


def test_failed_and_non_native_excluded():
    w = World({PAYER: 10**9, R1: 0, D: 0})
    w.pay(PAYER, R1, 100)
    w.pay(R1, D, 5000, res="tecUNFUNDED_PAYMENT")
    w.pay(R1, D, 0, kind="iou")
    r = w.run({R1: 0})
    t = r["totals"]
    assert (t["payments"], t["counted"], t["failed"], t["non_native"]) == (3, 1, 1, 1)
    assert t["volume_drops"] == 100
    o = r["onward"]["reward_first"]
    assert (o["onward"], o["fee"]) == (0, 20)  # failed tx still burns its fee
    assert o["mismatches"] == []


def test_graph_metrics():
    w = World({})
    w.pay("rA", "rB", 10)
    w.pay("rB", "rA", 5)
    w.pay("rA", "rC", 7)
    w.pay("rC", "rC", 1)  # self-payment
    w.pay("rX", "rY", 3)  # second component
    g = w.run({})["graph"]
    assert (g["accounts"], g["edges"], g["self_edges"], g["reciprocal_pairs"]) == (5, 5, 1, 1)
    assert (g["components"], g["largest_component"]) == (2, 3)
    assert (g["sources"], g["sinks"], g["intermediaries"]) == (1, 1, 3)
    assert g["out_degree_max"] == 2
    assert g["top_edges"][0] == {"from": "rA", "to": "rB", "payments": 1, "drops": 10}


def test_concentration_and_net():
    w = World({})
    w.pay("rA", "rB", 90)
    w.pay("rC", "rB", 10)
    r = w.run({})
    c = r["concentration"]["sent"]
    assert c["top1_share"] == 0.9 and c["accounts_for_half"] == 1
    assert c["hhi"] == pytest.approx(0.82)
    assert c["gini"] == pytest.approx(0.4)
    assert r["concentration"]["sent"]["accounts"] == 2
    assert r["concentration"]["received"] == {
        "accounts": 1,
        "total_drops": 100,
        "gini": 0.0,
        "top1_share": 1.0,
        "top10_share": 1.0,
        "hhi": 1.0,
        "accounts_for_half": 1,
    }
    assert r["net"]["sum_drops"] == 0
    assert r["net"]["gainers"][0]["net_drops"] == 100
    assert [x["net_drops"] for x in r["net"]["losers"]] == [-90, -10]


def test_coverage_hole_and_duplicate():
    led = [{"ledger_index": i, "close_time": "1", "txn_count": "0"} for i in (10, 11, 13, 13)]
    cov = ap.coverage(led, [{"ledger_index": "14"}])
    assert cov["holes"] == [12] and cov["duplicates"] == 1


def write_csv(path, rows, fields=None):
    fields = fields or (list(rows[0]) if rows else ["ledger_index"])
    with open(path, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=fields)
        wr.writeheader()
        wr.writerows(rows)


def write_world(tmp_path, w, snap_ledger, balances):
    data = tmp_path / "tx"
    data.mkdir()
    write_csv(data / "tx.csv", w.txs)
    write_csv(data / "payments.csv", w.pays)
    write_csv(data / "deltas.csv", w.deltas)
    write_csv(data / "ledgers.csv", w.ledgers)
    write_csv(data / "gaps.csv", [], ["ledger_index"])
    snap = tmp_path / f"snapshot-{snap_ledger}.jsonl.gz"
    with gzip.open(snap, "wt") as fh:
        fh.write(json.dumps({"ledger_index": snap_ledger, "ledger_hash": "LH"}) + "\n")
        for a, b in balances.items():
            fh.write(json.dumps({"LedgerEntryType": "AccountRoot", "Account": a, "Balance": str(b)}) + "\n")
        fh.write(json.dumps({"LedgerEntryType": "RippleState", "Account": R1, "Balance": "999"}) + "\n")
    return data, snap


def test_main_end_to_end(tmp_path, fifo_world, capsys):
    data, snap = write_world(tmp_path, fifo_world, 1000, {PAYER: 10**9, R1: 30})
    out = tmp_path / "report"
    rc = ap.main(
        [
            "--data",
            str(data),
            "--snapshot",
            str(snap),
            "--payer",
            PAYER,
            "--json",
            str(tmp_path / "p.json"),
            "--out-dir",
            str(out),
            "--no-charts",
        ]
    )
    assert rc == 0
    res = json.loads((tmp_path / "p.json").read_text())
    assert res["onward"]["reward_first"]["onward"] == 120
    assert res["snapshot"] == {"ledger_index": 1000, "ledger_hash": "LH", "adjacent": True}
    assert "_lorenz" not in res
    rep = (out / "REPORT.md").read_text()
    blob = rep + (tmp_path / "p.json").read_text()
    for full in (PAYER, R1, D):
        assert full not in blob
    assert ap.truncate(R1) in rep
    assert "onward reward_first: recipients=1 received=150 onward=120" in capsys.readouterr().out


def test_main_wrong_opening_exits_1(tmp_path, fifo_world, capsys):
    data, snap = write_world(tmp_path, fifo_world, 1000, {PAYER: 10**9, R1: 29})
    rc = ap.main(["--data", str(data), "--snapshot", str(snap), "--payer", PAYER])
    assert rc == 1
    assert "ONWARD MODEL" in capsys.readouterr().out


def test_main_non_adjacent_snapshot_exits_1(tmp_path, fifo_world, capsys):
    data, snap = write_world(tmp_path, fifo_world, 999, {PAYER: 10**9, R1: 30})
    assert ap.main(["--data", str(data), "--snapshot", str(snap), "--payer", PAYER]) == 1
    assert "SNAPSHOT" in capsys.readouterr().out


def test_main_txn_count_mismatch_exits_1(tmp_path, fifo_world, capsys):
    fifo_world.ledgers[0]["txn_count"] = "2"
    data, snap = write_world(tmp_path, fifo_world, 1000, {PAYER: 10**9, R1: 30})
    assert ap.main(["--data", str(data), "--snapshot", str(snap), "--payer", PAYER]) == 1
    assert "txn_count" in capsys.readouterr().out


def test_charts_written(tmp_path, fifo_world):
    pytest.importorskip("matplotlib")
    data, snap = write_world(tmp_path, fifo_world, 1000, {PAYER: 10**9, R1: 30})
    out = tmp_path / "r"
    rc = ap.main(["--data", str(data), "--snapshot", str(snap), "--payer", PAYER, "--out-dir", str(out)])
    assert rc == 0
    assert (out / "lorenz.png").stat().st_size > 0 and (out / "onward.png").stat().st_size > 0
    assert "![onward.png](onward.png)" in (out / "REPORT.md").read_text()


def test_failed_row_with_amount_not_counted():
    # the collector never writes a delivered amount for a failed payment; the analyzer must not trust that
    assert not ap.counted({"result": "tecPATH_PARTIAL", "kind": "native", "delivered_drops": "5"})
    assert ap.counted({"result": "tesSUCCESS", "kind": "native", "delivered_drops": "5"})


def test_accounts_for_half_boundary():
    assert ap.concentration({"a": 50, "b": 30, "c": 20})["accounts_for_half"] == 1  # exactly 50 % counts
    assert ap.concentration({"a": 49, "b": 31, "c": 20})["accounts_for_half"] == 2


def _row(li, h, prev, delta, final=None):
    return {
        "ledger_index": str(li),
        "tx_index": "0",
        "tx_hash": h,
        "close_time": str(CT0 + li),
        "prev_drops": str(prev),
        "delta_drops": str(delta),
        "final_drops": str(prev + delta if final is None else final),
    }


def test_reward_lot_capped_at_balance_delta():
    p = {
        "account": PAYER,
        "destination": R1,
        "result": "tesSUCCESS",
        "kind": "native",
        "delivered_drops": "1000",
    }
    r = ap.onward_model(R1, 0, [_row(1, "H1", 0, 600)], {"H1": p}, {}, PAYER, "reward_first")
    assert r["received"] == 600 and r["retained"] == 600 and r["mismatches"] == []


def test_final_balance_break_detected():
    r = ap.onward_model(R1, 100, [_row(1, "H1", 100, 50, final=151)], {}, {}, PAYER, "reward_first")
    assert [m["cause"] for m in r["mismatches"]] == ["final_balance"]
