import copy
import csv
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hashverify"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rewardflow"))
import collect_rewardflow as crf  # noqa: E402
import verify_txtree as vt  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "hashverify", "txtree_vectors.jsonl")
BUSY = 7806332  # 5 transactions, two tx IDs share the first nibble


def vectors():
    with open(FIX) as fh:
        return [json.loads(line) for line in fh]


def vec(i):
    return copy.deepcopy(next(v for v in vectors() if v["ledger_index"] == i))


def summary(d):
    with open(os.path.join(d, "summary.csv")) as fh:
        return {r["metric"]: r["value"] for r in csv.DictReader(fh)}


def checks(rec):
    return [p[0] for p in vt.check(rec)[1]]


def test_real_vectors_pass_offline(tmp_path, capsys):
    rc = vt.main(["--offline", FIX, "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    s = summary(tmp_path)
    assert s["ledgers_checked"] == "7" and s["mismatched_ledgers"] == "0"
    assert s["transactions"] == "26" and s["max_txn_count"] == "5" and s["empty_ledgers"] == "1"
    assert out.count("OK ledger=") == 7


def test_tamper_control_is_caught_with_both_hashes(tmp_path, capsys):
    rc = vt.main(["--offline", FIX, "--out", str(tmp_path), "--tamper", str(BUSY)])
    out = capsys.readouterr().out
    assert rc == 1
    bad = [ln for ln in out.splitlines() if ln.startswith("MISMATCH")]
    assert [ln.split()[2] for ln in bad] == ["check=txids", "check=root"]
    assert all(f"ledger={BUSY} " in ln for ln in bad)
    rep, comp = bad[1].split("reported=")[1].split(" computed=")
    assert rep == vec(BUSY)["json"]["transaction_hash"] and len(comp) == 64 and comp != rep
    assert summary(tmp_path)["mismatched_ledgers"] == "1"
    assert out.count("OK ledger=") == 6


def test_tamper_needs_a_ledger_with_transactions(tmp_path):
    with pytest.raises(ValueError, match="no transactions"):
        vt.main(["--offline", FIX, "--out", str(tmp_path), "--tamper", "7806708"])
    with pytest.raises(SystemExit):
        vt.main(["--out", str(tmp_path), "--tamper", str(BUSY)])


def test_header_bytes_must_match_json_fields():
    r = vec(BUSY)
    d = r["binary"]["ledger_data"]
    r["binary"]["ledger_data"] = d[:-2] + ("01" if d[-2:] != "01" else "02")
    assert checks(r) == ["header"]


def test_both_reported_ledger_hashes_are_checked():
    r = vec(BUSY)
    r["binary"]["ledger_hash"] = "0" * 64
    assert checks(r) == ["hash_binary"]
    r = vec(BUSY)
    r["json"]["ledger_hash"] = "0" * 64
    assert checks(r) == ["hash"]


def test_reported_tx_id_list_must_match_exactly():
    r = vec(BUSY)
    r["json"]["transactions"] = r["json"]["transactions"][:-1]
    assert checks(r) == ["txids"]
    r = vec(BUSY)
    r["json"]["transactions"] = r["json"]["transactions"] + r["json"]["transactions"][:1]
    assert checks(r) == ["txids"]


def test_missing_binary_tx_changes_root():
    r = vec(BUSY)
    r["binary"]["transactions"] = r["binary"]["transactions"][1:]
    assert checks(r) == ["txids", "root"]


def test_meta_is_part_of_the_leaf():
    r = vec(BUSY)
    m = r["binary"]["transactions"][0]["meta"]
    r["binary"]["transactions"][0]["meta"] = m[:-2] + ("00" if m[-2:] != "00" else "01")
    assert checks(r) == ["root"]


def test_duplicate_binary_tx_is_a_root_problem_not_a_crash():
    r = vec(7806707)
    r["binary"]["transactions"] = r["binary"]["transactions"] * 2
    assert "root" in checks(r)


def test_row_flags():
    row, probs = vt.check(vec(BUSY))
    assert probs == [] and row["txn_count"] == 5
    assert all(row[c] is True for c in ("header_ok", "hash_ok", "txids_ok", "root_ok"))


# --- live path with a stub transport ---------------------------------------------------------


def make_ledgers(lo, hi, close0=847000000, step=4):
    """Synthetic but self-consistent ledgers lo..hi built from the real vectors' transactions."""
    base = vectors()
    out = {}
    for n, i in enumerate(range(lo, hi + 1)):
        v = copy.deepcopy(base[n % len(base)])
        v["ledger_index"] = i
        j = v["json"]
        j["ledger_index"] = str(i)
        j["close_time"] = close0 + step * (i - lo)
        j["ledger_hash"] = vt.ledger_hash(j)
        v["binary"]["ledger_hash"] = j["ledger_hash"]
        v["binary"]["ledger_data"] = vt.serialize_header(j).hex().upper()
        out[i] = v
    return out


def stub_post(ledgers, spans, fail=None):
    fail = {k: list(v) for k, v in (fail or {}).items()}

    def post(body):
        req = json.loads(body)
        method, params = req["method"], req["params"][0]
        if method == "server_info":
            return {"result": {"status": "success", "info": {"complete_ledgers": spans}}}
        idx = params["ledger_index"]
        if fail.get(idx):
            return {"result": {"status": "error", "error": fail[idx].pop(0)}}
        v = ledgers[idx]
        if params.get("binary"):
            return {
                "result": {
                    "status": "success",
                    "ledger_index": idx,
                    "ledger_hash": v["binary"]["ledger_hash"],
                    "ledger": {
                        "closed": True,
                        "ledger_data": v["binary"]["ledger_data"],
                        "transactions": v["binary"]["transactions"],
                    },
                }
            }
        assert params.get("expand") is not True
        return {"result": {"status": "success", "ledger": {**v["json"], "closed": True}}}

    return post


def patch(monkeypatch, post, made):
    def make(url, gap):
        rpc = crf.Rpc(
            url,
            gap=gap,
            retries=4,
            backoff=0.01,
            post=post,
            sleep=lambda s: None,
            retry_errors=vt.RETRY,
            methods=("server_info", "ledger"),
        )
        made.append(rpc)
        return rpc

    monkeypatch.setattr(vt, "make_rpc", make)


def rows(d):
    with open(os.path.join(d, "txtree.csv")) as fh:
        return list(csv.DictReader(fh))


def test_live_slice_then_sample(tmp_path, capsys, monkeypatch):
    lo, hi = 1000, 1300
    led = make_ledgers(lo, hi)
    patch(monkeypatch, stub_post(led, f"{lo}-{hi}", fail={1290: ["lgrNotFound"]}), [])
    rec = tmp_path / "rec.jsonl.gz"
    argv = [
        "--source",
        "S",
        "--count",
        "60",
        "--slice-seconds",
        "80",
        "--margin",
        "5",
        "--out",
        str(tmp_path / "o"),
        "--record",
        str(rec),
    ]
    rc = vt.main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    # slice end = 1295; close_time steps 4 s, so 1275..1295 lie within 80 s (21 ledgers),
    # 1274 is the first one outside it: fetched, checked, labelled slice_boundary, walk stops
    assert "SLICE 1275-1295 (21 ledgers" in out
    r = rows(tmp_path / "o")
    sel = {int(x["ledger_index"]): x["selection"] for x in r}
    assert [i for i, s in sel.items() if s == "slice"] == list(range(1275, 1296))
    assert [i for i, s in sel.items() if s == "slice_boundary"] == [1274]
    sample = [i for i, s in sel.items() if s == "sample"]
    assert len(sample) == 60 - 21 and all(lo + 5 <= i <= hi - 5 for i in sample)
    assert not set(sample) & set(range(1275, 1296))
    s = summary(tmp_path / "o")
    assert s["slice_ledgers"] == "21" and s["sample_ledgers"] == "39" and s["rpc_retries"] == "1"
    with gzip.open(rec, "rt") as fh:
        assert sum(1 for _ in fh) == 61
    # same seed -> same sample
    vt.main(argv[:-4] + ["--out", str(tmp_path / "o2")])
    capsys.readouterr()
    assert [x["ledger_index"] for x in rows(tmp_path / "o2")] == [x["ledger_index"] for x in r]


def test_live_range_mode_and_mismatch(tmp_path, capsys, monkeypatch):
    led = make_ledgers(500, 509)
    led[503]["json"]["transaction_hash"] = "F" * 64  # reported root no longer matches the txs
    patch(monkeypatch, stub_post(led, "500-509"), [])
    rc = vt.main(["--source", "S", "--range", "500-509", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISMATCH ledger=503 check=root reported=" + "F" * 64 in out
    assert summary(tmp_path)["range_ledgers"] == "10"


def test_live_persistent_error_exits_2(tmp_path, capsys, monkeypatch):
    led = make_ledgers(500, 509)
    patch(monkeypatch, stub_post(led, "500-509", fail={505: ["tooBusy"] * 9}), [])
    rc = vt.main(["--source", "S", "--range", "500-509", "--out", str(tmp_path)])
    assert rc == 2 and "RPC_FAILURE" in capsys.readouterr().out


def test_fetch_rejects_answers_for_another_ledger(monkeypatch):
    led = make_ledgers(500, 501)
    post = stub_post(led, "500-501")

    def wrong(body):
        req = json.loads(body)
        if req["method"] == "ledger":
            req["params"][0]["ledger_index"] = 501
        return post(json.dumps(req).encode())

    rpc = crf.Rpc("S", post=wrong, sleep=lambda s: None, methods=("server_info", "ledger"))
    with pytest.raises(crf.RpcError, match="answers are for"):
        vt.fetch(rpc, 500)
