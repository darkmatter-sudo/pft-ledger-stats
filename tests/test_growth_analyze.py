"""Hand-computed fixtures for growth/analyze_growth.py and growth/statebytes_growth.py."""

import csv
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "growth"))
import analyze_growth as ag  # noqa: E402
import statebytes_growth as sbg  # noqa: E402


def led(i, ct, txn=0, blob=0, meta=0, c=0, m=0, d=0, src="local"):
    return {
        "ledger_index": str(i),
        "close_time": str(ct),
        "close_time_iso": f"2026-11-04T{(ct // 3600) % 24:02d}:{(ct // 60) % 60:02d}:{ct % 60:02d}Z",
        "txn_count": str(txn),
        "header_bytes": "118",
        "tx_blob_bytes": str(blob),
        "meta_bytes": str(meta),
        "created": str(c),
        "modified": str(m),
        "deleted": str(d),
        "source": src,
    }


def tx(i, ti, typ, blob, meta, c=0, m=0, d=0, result="tesSUCCESS"):
    return {
        "ledger_index": str(i),
        "tx_index": str(ti),
        "tx_type": typ,
        "result": result,
        "blob_bytes": str(blob),
        "meta_bytes": str(meta),
        "created": str(c),
        "modified": str(m),
        "deleted": str(d),
    }


def node(i, ti, action, et):
    return {"ledger_index": str(i), "tx_index": str(ti), "action": action, "entry_type": et}


# Three ledgers 4 s apart: ledger 11 has a Payment (2 modified AccountRoots) and an NFTokenMint
# (1 created NFTokenPage, 1 modified AccountRoot); 10 and 12 are empty.
LEDGERS = [
    led(10, 0),
    led(11, 4, txn=2, blob=300, meta=500, c=1, m=3),
    led(12, 8),
]
TXS = [tx(11, 0, "Payment", 180, 320, m=2), tx(11, 1, "NFTokenMint", 120, 180, c=1, m=1)]
NODES = [
    node(11, 0, "modified", "AccountRoot"),
    node(11, 0, "modified", "AccountRoot"),
    node(11, 1, "created", "NFTokenPage"),
    node(11, 1, "modified", "AccountRoot"),
]


def test_coverage_clean():
    c = ag.coverage(LEDGERS, TXS, NODES, [])
    assert (c["first"], c["last"], c["ledgers"], c["holes"]) == (10, 12, 3, 0)
    assert c["problems"] == []


def test_coverage_unlisted_hole_listed_gap_and_count_mismatch():
    led_rows = [LEDGERS[0], LEDGERS[2]]
    c = ag.coverage(led_rows, [], [], [])
    assert c["holes"] == 1 and any("unlisted holes" in p for p in c["problems"])
    c = ag.coverage(led_rows, [], [], [{"ledger_index": "11"}])
    assert c["problems"] == []
    c = ag.coverage(LEDGERS, TXS[:1], NODES, [])
    assert any("1 tx rows != txn_count 2" in p for p in c["problems"])
    c = ag.coverage(LEDGERS, TXS, NODES[:3], [])
    assert any("3 node rows != 4" in p for p in c["problems"])
    c = ag.coverage(LEDGERS + [LEDGERS[1]], TXS, NODES, [])
    assert any("duplicate ledgers [11]" in p for p in c["problems"])


def test_totals_and_per_day():
    tot, interval, day = ag.per_day(LEDGERS)
    # raw = 3 x 118 header + 300 blob + 500 meta = 1154
    assert tot["raw_bytes"] == 1154 and tot["txns"] == 2
    assert interval == 4.0
    assert day["ledgers"] == 21600.0  # 86400 / 4
    assert day["raw_bytes"] == pytest.approx(1154 * 7200)  # 21600 / 3 ledgers
    assert day["created"] == pytest.approx(7200)


def test_hourly_buckets():
    rows = [led(1, 3599, txn=1, blob=10), led(2, 3600, blob=0), led(3, 3601, txn=2, blob=5, meta=5)]
    h = ag.hourly(rows)
    assert [x["hour"] for x in h] == ["2026-11-04T00:00Z", "2026-11-04T01:00Z"]
    assert (h[0]["ledgers"], h[0]["txns"], h[0]["raw_bytes"]) == (1, 1, 128)
    assert (h[1]["ledgers"], h[1]["txns"], h[1]["raw_bytes"]) == (2, 2, 246)


def test_top_contributors():
    t = ag.top(TXS, NODES)
    assert t["tx_bytes"] == 800
    assert [x["tx_type"] for x in t["tx_types"]] == ["Payment", "NFTokenMint"]
    assert t["tx_types"][0]["byte_share"] == pytest.approx(500 / 800)
    assert t["tx_types"][1]["created"] == 1
    assert t["entries"][0] == {"entry_type": "AccountRoot", "action": "modified", "count": 3}
    assert t["results"] == [{"result": "tesSUCCESS", "count": 2, "share": 1.0}]


def write_snap(path, ledger, types):
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps({"ledger_index": ledger, "ledger_hash": "AB" * 32}) + "\n")
        for n, t in enumerate(types):
            fh.write(json.dumps({"index": f"{n:064X}", "LedgerEntryType": t}) + "\n")


def test_state_reconcile_ok_and_mismatch(tmp_path):
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    write_snap(a, 9, ["AccountRoot", "AccountRoot", "LedgerHashes"])
    write_snap(b, 12, ["AccountRoot", "AccountRoot", "LedgerHashes", "NFTokenPage"])
    st = ag.state_reconcile(str(a), str(b), NODES, 10, 12)
    assert st["problems"] == []
    assert {r["entry_type"]: (r["a"], r["created"], r["b"]) for r in st["types"]}["NFTokenPage"] == (0, 1, 1)
    write_snap(b, 12, ["AccountRoot", "LedgerHashes", "NFTokenPage"])
    st = ag.state_reconcile(str(a), str(b), NODES, 10, 12)
    assert st["problems"] == ["AccountRoot: A 2 + 0 - 0 != B 1"]
    st = ag.state_reconcile(str(a), str(b), NODES, 11, 12)
    assert "snapshot A ledger 9 != first-1 10" in st["problems"]


def test_fit_line_exact_and_degenerate():
    f = ag.fit_line([10, 20, 30], [200, 300, 400])
    assert (
        f["fixed"] == pytest.approx(100)
        and f["per_ledger"] == pytest.approx(10)
        and f["r2"] == pytest.approx(1)
    )
    assert ag.fit_line([5, 5], [1, 2]) is None
    assert ag.fit_line([5], [1]) is None


def sample(epoch, lo, hi, nudb, sqlite, state="full"):
    return {
        "epoch": str(epoch),
        "server_state": state,
        "complete_lo": str(lo),
        "complete_hi": str(hi),
        "nudb_bytes": str(nudb),
        "sqlite_bytes": str(sqlite),
    }


def test_nudb_samples_filter():
    rows = [
        sample(100, 1, 10, 1, 1),
        sample(200, 1, 20, 1, 1, state="syncing"),
        sample(300, 0, 0, 1, 1),
        sample(400, 1, 30, 1, 1),
    ]
    s = ag.nudb_samples(rows, 150, 450)
    assert [x["retained"] for x in s] == [30]
    assert [x["retained"] for x in ag.nudb_samples(rows)] == [10, 30]


def test_nudb_overhead_ratio_hand_computed():
    s = [
        {"epoch": 1, "retained": 10, "nudb": 300, "sqlite": 70},
        {"epoch": 2, "retained": 20, "nudb": 500, "sqlite": 90},
    ]
    nd = ag.nudb(s, 10, state_b=50, raw_per_ledger=5)
    # mean retained 15, mean nudb 400, raw projection 50 + 15 x 5 = 125 -> ratio 3.2
    assert nd["raw_projection_bytes"] == 125
    assert nd["overhead_ratio"] == pytest.approx(3.2)
    assert nd["fit_nudb"]["fixed"] == pytest.approx(100) and nd["fit_nudb"]["per_ledger"] == pytest.approx(20)
    assert nd["retained_ratio"] == {"min": 1.0, "max": 2.0, "mean": 1.5}
    assert ag.nudb([], 512, 1, 1) is None


def test_sizing_with_fit_and_sqlite_fallback():
    nd = {
        "retained_ratio": {"mean": 1.5},
        "fit_nudb": {"fixed": 100.0, "per_ledger": 10.0, "r2": 0.99},
        "fit_sqlite": {"fixed": 5.0, "per_ledger": -1.0, "r2": 0.01},
        "sqlite_bytes": {"mean": 70.0},
    }
    sz = ag.sizing(
        nd, state_b=50, raw_per_ledger=2, ledgers_per_day=1000, last_ledger=10_000, state_growth_day=7
    )
    assert sz["sqlite_mode"] == "measured_mean"
    r = sz["rows"][0]
    assert r["online_delete"] == 256 and r["retained_mean"] == 384
    assert r["nudb_bytes"] == 100 + 10 * 384 and r["nudb_bytes_max"] == 100 + 10 * 512
    assert r["raw_bytes"] == 50 + 2 * 384 and r["sqlite_bytes"] == 70.0
    full = sz["full_history"]
    assert full["raw_bytes_per_day"] == 2 * 1000 + 7
    assert full["disk_bytes_per_day"] == 10 * 1000 + 7  # SQLite per ledger 0 in measured_mean mode
    assert full["naive_all_ledgers_disk_bytes"] == 10 * 10_000


def test_sizing_drops_unusable_nudb_fit():
    nd = {
        "retained_ratio": {"mean": 1.5},
        "fit_nudb": {"fixed": 1.0, "per_ledger": 1.0, "r2": 0.2},
        "fit_sqlite": {"fixed": 5.0, "per_ledger": 1.0, "r2": 0.9},
        "sqlite_bytes": {"mean": 70.0},
    }
    sz = ag.sizing(nd, 50, 2, 1000, 10, None)
    assert sz["rows"][0]["nudb_bytes"] is None and sz["full_history"]["disk_bytes_per_day"] is None
    assert sz["sqlite_mode"] == "fit" and sz["rows"][0]["sqlite_bytes"] == 5 + 384


def test_state_bytes_growth():
    pts = [
        {"ledger_index": 200, "objects": 5, "bytes": 1300, "by_type": {}},
        {"ledger_index": 100, "objects": 4, "bytes": 1000, "by_type": {}},
    ]
    sb = ag.state_bytes(pts, interval=4.0)
    assert sb["bytes"] == 1300 and sb["growth_per_ledger"] == 3.0
    assert sb["growth_per_day"] == pytest.approx(3.0 * 21600)
    assert ag.state_bytes(pts[:1], 4.0)["growth_per_day"] is None


def write_dir(d, ledgers, txs, nodes):
    for name, rows in (("ledgers", ledgers), ("tx", txs), ("nodes", nodes)):
        with open(os.path.join(d, f"{name}.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def test_main_offline_check_identical_and_tampered(tmp_path, capsys):
    d = tmp_path / "data"
    d.mkdir()
    write_dir(str(d), LEDGERS, TXS, NODES)
    out = tmp_path / "out"
    assert ag.main(["--data", str(d), "--out", str(out)]) == 0
    rec = out / "growth.json"
    assert ag.main(["--data", str(d), "--out", str(out), "--check", str(rec)]) == 0
    assert "IDENTICAL" in capsys.readouterr().out
    j = json.loads(rec.read_text())
    j["totals"]["raw_bytes"] += 1
    rec.write_text(json.dumps(j, indent=1, sort_keys=True) + "\n")
    assert ag.main(["--data", str(d), "--out", str(out), "--check", str(rec)]) == 1
    assert "DIFFERS" in capsys.readouterr().out


def test_main_exits_1_on_problem(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    write_dir(str(d), [LEDGERS[0], LEDGERS[2]], TXS[:1], NODES[:1])
    assert ag.main(["--data", str(d), "--out", str(tmp_path / "o")]) == 1


# ---- statebytes_growth ----


def test_entry_type_decode():
    assert sbg.entry_type("1100612200000000") == "AccountRoot"
    assert sbg.entry_type("110068") == "LedgerHashes"
    assert sbg.entry_type("111234") == "0x1234"
    with pytest.raises(ValueError):
        sbg.entry_type("220061")
    with pytest.raises(ValueError):
        sbg.entry_type("11")


def test_obj_rows_and_summary():
    rows = sbg.obj_rows([{"index": "aa", "data": "110061" + "00" * 7}, {"index": "BB", "data": "110068ffff"}])
    assert rows == [
        {"index": "AA", "entry_type": "AccountRoot", "bytes": 10},
        {"index": "BB", "entry_type": "LedgerHashes", "bytes": 5},
    ]
    s = sbg.summarize(rows)
    assert s["objects"] == 2 and s["bytes"] == 15
    assert list(s["by_type"]) == ["AccountRoot", "LedgerHashes"]
    with pytest.raises(ValueError):
        sbg.obj_rows([{"index": "CC", "data": "110061f"}])


def test_join_check():
    rows = [
        {"index": "A1", "entry_type": "AccountRoot", "bytes": 1},
        {"index": "B2", "entry_type": "Offer", "bytes": 1},
    ]
    hdr = {"ledger_hash": "ff" * 32}
    assert sbg.join_check("FF" * 32, rows, hdr, {"A1": "AccountRoot", "B2": "Offer"}) == []
    p = sbg.join_check("EE" * 32, rows, hdr, {"A1": "AccountRoot", "C3": "Offer", "B2": "Ticket"})
    assert any(x.startswith("ledger hash") for x in p)
    assert "missing in binary: C3" in p
    assert "type B2: binary Offer != json Ticket" in p
    assert sbg.join_check("FF" * 32, rows + rows[:1], hdr, {"A1": "AccountRoot", "B2": "Offer"}) == [
        "duplicate index A1"
    ]


class PageRpc:
    def __init__(self, pages, ledger=7, hashes=None):
        self.pages, self.ledger, self.hashes, self.n = pages, ledger, hashes, 0

    def call(self, method, params):
        assert method == "ledger_data" and params["binary"] is True
        i = self.n
        self.n += 1
        res = {
            "ledger_index": self.ledger,
            "ledger_hash": (self.hashes or ["H"] * len(self.pages))[i],
            "state": self.pages[i],
        }
        if i + 1 < len(self.pages):
            res["marker"] = f"m{i}"
        return res


def test_page_ledger_pages_and_hash_guard():
    pages = [[{"index": "A", "data": "110061"}], [{"index": "B", "data": "110064"}]]
    h, rows, n = sbg.page_ledger(PageRpc(pages), 7)
    assert (h, n, [r["entry_type"] for r in rows]) == ("H", 2, ["AccountRoot", "DirectoryNode"])
    with pytest.raises(RuntimeError, match="hash changed"):
        sbg.page_ledger(PageRpc(pages, hashes=["H", "X"]), 7)
    with pytest.raises(RuntimeError, match="not 8"):
        sbg.page_ledger(PageRpc(pages), 8)
