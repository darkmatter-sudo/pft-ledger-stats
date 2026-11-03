import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "senders"))
import report_senders as rep  # noqa: E402

TX = ["ledger_index", "close_time", "account", "result", "fee_drops", "tx_type"]
LED = ["ledger_index", "close_time", "txn_count"]


def write(path, fields, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def fixture(d, drop_row=False):
    # 72.5 h of ledgers every 4 s; sender A sends every 600 s (periodic), B every ~irregular step
    t0 = 800_000_000
    led, tx = [], []
    for i, t in enumerate(range(t0, t0 + int(72.5 * 3600), 4), start=1):
        rows = []
        if (t - t0) % 600 == 0:
            rows.append(
                {
                    "ledger_index": i,
                    "close_time": t,
                    "account": "rAAAAAAAAAAAAAAAAAA",
                    "result": "tesSUCCESS",
                    "fee_drops": 12,
                    "tx_type": "Payment",
                }
            )
        if (t - t0) % 7777 == 0 or (t - t0) % 5003 == 0:
            rows.append(
                {
                    "ledger_index": i,
                    "close_time": t,
                    "account": "rBBBBBBBBBBBBBBBBBB",
                    "result": "tecFAIL",
                    "fee_drops": 10,
                    "tx_type": "NFTokenMint",
                }
            )
        led.append({"ledger_index": i, "close_time": t, "txn_count": len(rows)})
        tx.extend(rows)
    if drop_row:
        tx.pop()
    write(os.path.join(d, "ledgers.csv"), LED, led)
    write(os.path.join(d, "tx.csv"), TX, tx)
    return tx


def test_report_numbers_come_from_the_data(tmp_path):
    tx = fixture(tmp_path)
    assert (
        rep.main(
            [
                "--data",
                str(tmp_path),
                "--out-dir",
                str(tmp_path),
                "--n",
                "40",
                "--no-charts",
                "--json",
                str(tmp_path / "s.json"),
            ]
        )
        == 0
    )
    md = (tmp_path / "REPORT.md").read_text()
    assert f"| transactions (tx.csv rows) | {len(tx)} |" in md
    assert f"| sum of ledger `txn_count` | {len(tx)} |" in md
    assert "| distinct senders | 2 |" in md
    assert "| periodic | 1 | 50.0 % |" in md
    nb = sum(1 for r in tx if r["account"].startswith("rB"))
    assert f"| failed (result != tesSUCCESS) | {nb} " in md
    assert "| rAAAAA...AAAA | " in md and "rAAAAAAAAAAAAAAAAAA" not in md
    assert "| 0 | 2 | 2 (100.0 %) | 2 (100.0 %) | 2 (100.0 %) |" in md
    assert "planted-periodic | periodic | 40/40" in md
    assert (tmp_path / "s.json").exists()


def test_report_fails_on_incomplete_collection(tmp_path):
    fixture(tmp_path, drop_row=True)
    assert rep.main(["--data", str(tmp_path), "--out-dir", str(tmp_path), "--n", "20", "--no-charts"]) == 1


def test_report_fails_when_a_control_fails(tmp_path, monkeypatch):
    fixture(tmp_path)
    monkeypatch.setattr(
        rep.ctl,
        "run_planted",
        lambda data, n, seed: [
            {"control": "planted-periodic", "expected": "periodic", "n": n, "correct": 0, "classes": {}}
        ],
    )
    assert rep.main(["--data", str(tmp_path), "--out-dir", str(tmp_path), "--n", "20", "--no-charts"]) == 1
    assert "| planted-periodic | periodic | 0/20 | {} | FAIL |" in (tmp_path / "REPORT.md").read_text()


def test_normalized_gaps_mean_one():
    tx = [{"account": "A", "close_time": str(t)} for t in (0, 10, 30, 60, 100, 150, 210)]
    g = rep.normalized_gaps(tx)
    assert len(g) == 6 and abs(sum(g) / len(g) - 1) < 1e-9


def test_charts_written(tmp_path):
    import pytest

    pytest.importorskip("matplotlib")
    fixture(tmp_path)
    assert rep.main(["--data", str(tmp_path), "--out-dir", str(tmp_path), "--n", "20"]) == 0
    for f in ("cohorts.png", "regularity.png", "gaps.png"):
        assert (tmp_path / f).stat().st_size > 5000
