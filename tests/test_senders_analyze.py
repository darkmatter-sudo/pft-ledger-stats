import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "senders"))
import analyze_senders as an  # noqa: E402
import controls_senders as ctl  # noqa: E402

DAY = an.DAY


def row(acct, ct, res="tesSUCCESS", fee=10, tt="Payment", li=None):
    return {"account": acct, "close_time": str(ct), "result": res, "fee_drops": str(fee), "tx_type": tt,
            "ledger_index": str(li if li is not None else ct)}


def ledgers(times, txn=None):
    return [{"ledger_index": str(i), "close_time": str(t), "txn_count": str((txn or {}).get(t, 0))}
            for i, t in enumerate(times, start=1)]


def test_gap_stats_periodic_exact():
    st = an.gap_stats([0, 60, 120, 180, 240, 300])
    assert st["gaps"] == 5 and st["median_gap"] == 60 and st["cv"] == 0 and st["period_share"] == 1
    assert st["b"] == -1


def test_gap_stats_single_event():
    st = an.gap_stats([5])
    assert st["gaps"] == 0 and st["cv"] is None and an.classify(st) == "sporadic"


def test_gap_stats_even_median():
    assert an.gap_stats([0, 10, 30, 60, 100])["median_gap"] == 25


def test_classify_boundaries():
    base = {"n": an.MIN_EVENTS, "cv": an.PERIODIC_CV, "period_share": an.PERIOD_SHARE, "b": 0.0}
    assert an.classify(base) == "periodic"
    assert an.classify({**base, "n": an.MIN_EVENTS - 1}) == "sporadic"
    assert an.classify({**base, "cv": an.PERIODIC_CV + 0.01}) == "sporadic"
    assert an.classify({**base, "period_share": an.PERIOD_SHARE - 0.01}) == "sporadic"
    assert an.classify({**base, "cv": 2.0, "b": an.BURSTY_B}) == "bursty"
    assert an.classify({**base, "cv": 2.0, "b": an.BURSTY_B - 0.01}) == "sporadic"


def test_period_tolerance_edge():
    # gaps 100,100,100,120,80 -> all within +-20 % of median 100
    assert an.gap_stats([0, 100, 200, 300, 420, 500])["period_share"] == 1
    # gaps 100,100,100,121,79 -> 121 and 79 fall outside
    assert an.gap_stats([0, 100, 200, 300, 421, 500])["period_share"] == 0.6


def test_truncate():
    assert an.truncate("rKgWTTN6WcCcGQEUEnPtZkmr8uYpANXha6") == "rKgWTT...Xha6"
    assert an.truncate("short") == "short"


def test_days_new_returning_and_retention():
    # three slices; A every slice, B slices 0 and 2, C only slice 1, D only slice 2
    t = [0, DAY + 5, 2 * DAY + 5, 3 * DAY - 1]
    txs = [row("A", 0), row("A", DAY + 5), row("A", 2 * DAY + 5), row("B", 0), row("B", 3 * DAY - 1),
           row("C", DAY + 5, res="tecX"), row("D", 2 * DAY + 5)]
    res = an.analyze(txs, ledgers(t), [])
    assert res["window"]["slices"] == 3
    assert [(d["active"], d["new"], d["returning"]) for d in res["days"]] == [(2, 2, 0), (2, 1, 1), (3, 1, 2)]
    assert [d["tx"] for d in res["days"]] == [2, 2, 3]
    assert res["retention"][0] == {"cohort": 0, "size": 2, "active": [2, 1, 2]}
    assert res["retention"][1] == {"cohort": 1, "size": 1, "active": [None, 1, 0]}
    assert res["retention"][2] == {"cohort": 2, "size": 1, "active": [None, None, 1]}
    assert res["totals"]["failed"] == 1


def test_last_slice_absorbs_remainder():
    t = [0, int(72.5 * 3600)]
    res = an.analyze([row("A", 0), row("A", t[1])], ledgers(t), [])
    assert res["window"]["slices"] == 3 and [d["tx"] for d in res["days"]] == [1, 0, 1]


def test_shares_and_top_periodic():
    per = [row("rPERIODIC00000000000000000001", 600 * k, fee=12) for k in range(10)]
    burst = [row("BURST", 10000 * (k // 5) + (k % 5) * 4, res="tecFAIL") for k in range(20)]
    one = [row("ONE", 5)]
    res = an.analyze(per + burst + one, ledgers([0, 6000, 40000]), [])
    sh = res["shares"]
    assert (sh["periodic"]["senders"], sh["bursty"]["senders"], sh["sporadic"]["senders"]) == (1, 1, 1)
    assert sh["periodic"]["tx"] == 10 and sh["periodic"]["fee_drops"] == 120
    b = sh["bursty"]
    assert b["failed"] == 20 and b["failed_share"] == 1 and b["fail_rate"] == 1
    assert abs(sum(s["tx_share"] for s in sh.values()) - 1) < 1e-12
    top = res["top_periodic"]
    assert top[0]["account"] == "rPERIO...0001" and top[0]["period_s"] == 600 and top[0]["tx"] == 10


def test_events_are_distinct_ledgers():
    # two txs in one ledger are one event: a sender with 6 txs in 3 ledgers is not eligible
    txs = [row("A", t) for t in (0, 0, 60, 60, 120, 120)]
    res = an.analyze(txs, ledgers([0, 60, 120]), [])
    assert res["per_sender"]["A"]["n"] == 3 and res["per_sender"]["A"]["tx"] == 6


def test_coverage_holes_and_gaps():
    led = [{"ledger_index": str(i), "close_time": str(i), "txn_count": "0"} for i in (10, 11, 13, 15)]
    cov = an.coverage(led, [{"ledger_index": "12"}])
    assert cov["holes"] == [14] and cov["gaps"] == 1 and cov["duplicates"] == 0
    cov = an.coverage(led + led[:1], [])
    assert cov["duplicates"] == 1


def write_csv(path, fields, rows):
    import csv
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_main_exit_codes(tmp_path, capsys):
    led = [{"ledger_index": i, "close_time": i * 4, "txn_count": 1} for i in range(1, 6)]
    tx = [{"ledger_index": i, "close_time": i * 4, "account": "A", "result": "tesSUCCESS", "fee_drops": 10,
           "tx_type": "Payment"} for i in range(1, 6)]
    write_csv(tmp_path / "ledgers.csv", list(led[0]), led)
    write_csv(tmp_path / "tx.csv", list(tx[0]), tx)
    assert an.main(["--data", str(tmp_path), "--json", str(tmp_path / "r.json")]) == 0
    assert (tmp_path / "r.json").exists()
    write_csv(tmp_path / "tx.csv", list(tx[0]), tx[:4])  # one tx row missing vs txn_count
    assert an.main(["--data", str(tmp_path)]) == 1
    capsys.readouterr()
    # unlisted hole at ledger 3 (its tx row gone too, so rows == txn_count and only the hole check fires)
    write_csv(tmp_path / "tx.csv", list(tx[0]), tx[:2] + tx[3:])
    write_csv(tmp_path / "ledgers.csv", list(led[0]), led[:2] + led[3:])
    assert an.main(["--data", str(tmp_path)]) == 1
    assert "INCOMPLETE: 1 unlisted holes" in capsys.readouterr().out
    # the same ledger listed in gaps.csv is a recorded gap, not a hole
    gap = {"ledger_index": 3, "error": "x", "logged_at": ""}
    write_csv(tmp_path / "gaps.csv", list(gap), [gap])
    assert an.main(["--data", str(tmp_path)]) == 0


def test_controls_pass_and_are_deterministic():
    for name in ctl.GENERATORS:
        r1 = ctl.run_positive(name, 100, "t")
        assert r1 == ctl.run_positive(name, 100, "t")
        assert r1["correct"] >= 95, r1


def test_negative_control_destroys_schedule(tmp_path):
    pool = list(range(0, 72 * 3600, 4))
    led = [{"ledger_index": i, "close_time": t, "txn_count": 0} for i, t in enumerate(pool)]
    tx = [{"ledger_index": 0, "close_time": 600 * k, "account": f"P{a}", "result": "tesSUCCESS",
           "fee_drops": 10, "tx_type": "Payment"} for a in range(30) for k in range(400)]
    write_csv(tmp_path / "ledgers.csv", list(led[0]), led)
    write_csv(tmp_path / "tx.csv", list(tx[0]), tx)
    r = ctl.run_negative(str(tmp_path), "t")
    assert {k: r[k] for k in ("control", "eligible", "real_periodic", "shuffled_periodic")} == {
        "control": "shuffled", "eligible": 30, "real_periodic": 30, "shuffled_periodic": 0}
    assert r["real_classes"] == {"periodic": 30}
    assert r["shuffled_classes"].get("periodic", 0) == 0 and sum(r["shuffled_classes"].values()) == 30
    assert r["median_cv_real"] == 0.0 and 0.8 < r["median_cv_shuffled"] < 1.2
    assert ctl.main(["--n", "50", "--data", str(tmp_path)]) == 0


def test_controls_fail_when_rule_broken(monkeypatch):
    monkeypatch.setattr(ctl, "classify", lambda st: "sporadic")
    assert ctl.main(["--n", "20"]) == 1


def test_negative_control_fails_when_schedule_survives(tmp_path):
    # every ledger of the window is on a 600 s clock, so any sample of it stays periodic
    pool = list(range(0, 72 * 3600, 600))
    led = [{"ledger_index": i, "close_time": t, "txn_count": 0} for i, t in enumerate(pool)]
    tx = [{"ledger_index": i, "close_time": t, "account": "P", "result": "tesSUCCESS", "fee_drops": 10,
           "tx_type": "Payment"} for i, t in enumerate(pool)]
    write_csv(tmp_path / "ledgers.csv", list(led[0]), led)
    write_csv(tmp_path / "tx.csv", list(tx[0]), tx)
    r = ctl.run_negative(str(tmp_path), "t")
    assert r["real_periodic"] == 1 and r["shuffled_periodic"] == 1
    assert ctl.main(["--n", "20", "--data", str(tmp_path)]) == 1


def irregular_ledgers(tmp_path, t0=800_000_000, hours=72.5, seed=7):
    # real-like ledger timeline: close times 3..5 s apart, starting far from 0
    import random

    rng, t, led = random.Random(seed), t0, []
    while t < t0 + hours * 3600:
        led.append({"ledger_index": len(led) + 1, "close_time": t, "txn_count": 0})
        t += rng.choice((3, 4, 4, 5))
    write_csv(tmp_path / "ledgers.csv", list(led[0]), led)
    return led


def test_planted_controls_on_real_timeline(tmp_path):
    irregular_ledgers(tmp_path)
    res = ctl.run_planted(str(tmp_path), 60, "t")
    assert [r["control"] for r in res] == ["planted-periodic", "planted-poisson", "planted-burst"]
    for r in res:
        assert r["correct"] >= 57, r
    assert res == ctl.run_planted(str(tmp_path), 60, "t")


def test_planted_controls_fail_when_rule_broken(tmp_path, monkeypatch):
    irregular_ledgers(tmp_path)
    fields = ["ledger_index", "close_time", "account", "result", "fee_drops", "tx_type"]
    write_csv(tmp_path / "tx.csv", fields,
              [{"ledger_index": 1, "close_time": 800_000_000, "account": "A", "result": "tesSUCCESS",
                "fee_drops": 10, "tx_type": "Payment"}])
    assert ctl.main(["--n", "30", "--data", str(tmp_path)]) == 0
    real = ctl.classify
    monkeypatch.setattr(ctl, "classify", lambda st: "bursty" if real(st) == "periodic" else real(st))
    monkeypatch.setattr(ctl, "run_positive", lambda name, n, seed: {
        "control": name, "expected": "x", "n": n, "correct": n, "classes": {}})
    assert ctl.main(["--n", "30", "--data", str(tmp_path)]) == 1


def test_negative_control_fails_when_any_shuffled_sender_is_periodic(tmp_path, monkeypatch):
    irregular_ledgers(tmp_path, hours=6)
    tx = [{"ledger_index": 1, "close_time": 800_000_000 + 37 * k, "account": "S", "result": "tesSUCCESS",
           "fee_drops": 10, "tx_type": "Payment"} for k in range(50)]
    write_csv(tmp_path / "tx.csv", list(tx[0]), tx)
    # inverted rule: the real 37 s clock (CV 0) reads sporadic, its shuffled copy (CV > 0) periodic
    monkeypatch.setattr(ctl, "classify", lambda st: "periodic" if st["cv"] > 0 else "sporadic")
    r = ctl.run_negative(str(tmp_path), "t")
    assert r["real_periodic"] == 0 and r["shuffled_classes"] == {"periodic": 1}
    monkeypatch.setattr(ctl, "run_positive", lambda name, n, seed: {
        "control": name, "expected": "x", "n": n, "correct": n, "classes": {}})
    monkeypatch.setattr(ctl, "run_planted", lambda data, n, seed: [])
    assert ctl.main(["--n", "10", "--data", str(tmp_path)]) == 1
