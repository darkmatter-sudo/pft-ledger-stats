import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hashverify"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rewardflow"))
import collect_rewardflow as crf  # noqa: E402
import verify_headers as vh  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "hashverify", "headers_vectors.jsonl")
LO, HI = 7806596, 7806607
MID = 7806601


def summary(d):
    with open(os.path.join(d, "summary.csv")) as fh:
        return {r["metric"]: r["value"] for r in csv.DictReader(fh)}


def run(capsys, *args):
    rc = vh.main(list(args))
    return rc, capsys.readouterr().out


def test_real_chain_passes_offline(tmp_path, capsys):
    rc, out = run(capsys, "--offline", FIX, "--out", str(tmp_path))
    assert rc == 0, out
    s = summary(tmp_path)
    assert s["range_ledgers"] == "12"
    for src in ("local", "public"):
        assert s[f"{src}_covered"] == "12" and s[f"{src}_hash_ok"] == "12"
        assert s[f"{src}_parent_links_checked"] == "11" and s[f"{src}_chain_breaks"] == "0"
    assert s["agree"] == "12" and s["disagree"] == "0"
    assert "RESULT: PASS" in out


def test_tamper_total_coins_is_caught_three_ways(tmp_path, capsys):
    rc, out = run(capsys, "--offline", FIX, "--out", str(tmp_path), "--tamper", f"public:{MID}:total_coins")
    assert rc == 1
    lines = [
        ln for ln in out.splitlines() if ln.split()[0] in ("HASH_MISMATCH", "CHAIN_BREAK", "DISAGREE", "GAP")
    ]
    kinds = sorted((ln.split()[0], ln.split()[2]) for ln in lines)
    assert kinds == [
        ("CHAIN_BREAK", f"ledger={MID + 1}"),
        ("DISAGREE", f"ledger={MID}"),
        ("HASH_MISMATCH", f"ledger={MID}"),
    ]
    hm = next(ln for ln in lines if ln.startswith("HASH_MISMATCH"))
    rep, comp = hm.split("reported=")[1].split(" computed=")
    assert len(rep) == 64 and len(comp) == 64 and rep != comp
    s = summary(tmp_path)
    assert s["public_hash_mismatch"] == "1" and s["public_chain_breaks"] == "1" and s["disagree"] == "1"
    assert s["local_hash_mismatch"] == "0" and s["local_chain_breaks"] == "0"


def test_tamper_parent_hash_breaks_the_chain_at_that_ledger(tmp_path, capsys):
    rc, out = run(capsys, "--offline", FIX, "--out", str(tmp_path), "--tamper", f"local:{MID}:parent_hash")
    assert rc == 1
    assert f"CHAIN_BREAK source=local ledger={MID} " in out
    assert f"HASH_MISMATCH source=local ledger={MID} " in out
    assert f"CHAIN_BREAK source=local ledger={MID + 1} " in out


def test_unsupported_tamper_field(tmp_path):
    with pytest.raises(ValueError):
        vh.main(["--offline", FIX, "--out", str(tmp_path), "--tamper", f"local:{MID}:ledger_hash"])


def test_tamper_needs_offline(tmp_path):
    with pytest.raises(SystemExit):
        vh.main(["--out", str(tmp_path), "--tamper", f"local:{MID}:total_coins"])


def test_missing_ledger_is_a_gap(tmp_path, capsys):
    f = tmp_path / "rec.jsonl"
    with open(FIX) as src, open(f, "w") as dst:
        for line in src:
            r = json.loads(line)
            if not (r["source"] == "public" and int(r["header"]["ledger_index"]) == MID):
                dst.write(line)
    rc, out = run(capsys, "--offline", str(f), "--out", str(tmp_path / "o"))
    assert rc == 1
    assert f"GAP source=public ledger={MID} " in out
    s = summary(tmp_path / "o")
    assert s["public_gaps"] == "1" and s["public_covered"] == "11" and s["local_covered"] == "12"
    assert s["public_parent_links_checked"] == "9"


def test_disagreeing_but_self_consistent_source(tmp_path, capsys):
    """A source whose ledger hashes recompute but differ from the other source (a fork)."""
    f = tmp_path / "rec.jsonl"
    with open(FIX) as src, open(f, "w") as dst:
        for line in src:
            r = json.loads(line)
            h = r["header"]
            if r["source"] == "public" and int(h["ledger_index"]) == HI:
                h["close_time"] = int(h["close_time"]) + 1
                h["ledger_hash"] = vh.ledger_hash(h)
            dst.write(json.dumps(r) + "\n")
    rc, out = run(capsys, "--offline", str(f), "--out", str(tmp_path / "o"))
    assert rc == 1
    assert [ln.split()[0] for ln in out.splitlines() if ln.startswith(("DISAGREE", "HASH_", "CHAIN"))] == [
        "DISAGREE"
    ]


# --- live path with a stub transport ---------------------------------------------------------


def stub_post(headers, spans, fail=None):
    """post(body) for crf.Rpc serving recorded headers; fail = {(index): [errors to return first]}."""
    fail = {k: list(v) for k, v in (fail or {}).items()}

    def post(body):
        req = json.loads(body)
        method, params = req["method"], req["params"][0]
        if method == "server_info":
            return {"result": {"status": "success", "info": {"complete_ledgers": spans}}}
        assert method == "ledger" and params["transactions"] is False
        idx = params["ledger_index"]
        if fail.get(idx):
            return {"result": {"status": "error", "error": fail[idx].pop(0)}}
        if idx not in headers:
            return {"result": {"status": "error", "error": "lgrNotFound"}}
        return {"result": {"status": "success", "ledger": {**headers[idx], "closed": True}}}

    return post


def load_fix():
    return vh.load(FIX)


def patch_rpcs(monkeypatch, posts, calls):
    def make(url, gap):
        rpc = crf.Rpc(
            url,
            gap=gap,
            retries=4,
            backoff=0.01,
            post=posts[url],
            sleep=lambda s: None,
            retry_errors=vh.RETRY,
            methods=vh.METHODS,
        )
        calls.append(rpc)
        return rpc

    monkeypatch.setattr(vh, "make_rpc", make)


def test_live_overlap_fetch_and_retry(tmp_path, capsys, monkeypatch):
    h = load_fix()
    calls = []
    posts = {
        "L": stub_post(h["local"], f"{LO}-{HI}"),
        "P": stub_post(h["public"], f"7700000-{HI + 50}", fail={MID: ["slowDown", "lgrNotFound"]}),
    }
    patch_rpcs(monkeypatch, posts, calls)
    rec = tmp_path / "rec.jsonl.gz"
    rc, out = run(capsys, "--local", "L", "--public", "P", "--out", str(tmp_path / "o"), "--record", str(rec))
    assert rc == 0, out
    assert f"overlap={LO}-{HI} checked={LO}-{HI} (12)" in out
    assert out.count("RETRY ledger") == 2
    s = summary(tmp_path / "o")
    assert s["public_rpc_retries"] == "2" and s["local_rpc_retries"] == "0"
    rc2, out2 = run(capsys, "--offline", str(rec), "--out", str(tmp_path / "o2"))
    assert rc2 == 0 and "local 12 public 12 headers" in out2


def test_live_rpc_failure_exits_2(tmp_path, capsys, monkeypatch):
    h = load_fix()
    posts = {"L": stub_post(h["local"], f"{LO}-{HI}"), "P": stub_post(h["public"], "1-5")}
    patch_rpcs(monkeypatch, posts, [])
    rc, out = run(capsys, "--local", "L", "--public", "P", "--out", str(tmp_path))
    assert rc == 2 and "RANGE no overlap" in out


def test_live_unservable_ledger_is_a_gap(tmp_path, capsys, monkeypatch):
    h = load_fix()
    pub = dict(h["public"])
    del pub[MID]
    posts = {"L": stub_post(h["local"], f"{LO}-{HI}"), "P": stub_post(pub, f"{LO}-{HI}")}
    patch_rpcs(monkeypatch, posts, [])
    rc, out = run(capsys, "--local", "L", "--public", "P", "--out", str(tmp_path))
    assert rc == 1
    assert f"FETCH_FAILED source=public ledger={MID}" in out and f"GAP source=public ledger={MID} " in out


def test_fetch_rejects_wrong_or_open_ledger():
    h = load_fix()["local"]

    def post(body):
        return {"result": {"status": "success", "ledger": {**h[LO], "closed": True}}}

    rpc = crf.Rpc("x", post=post, sleep=lambda s: None, methods=vh.METHODS)
    with pytest.raises(crf.RpcError, match="answer is ledger"):
        vh.fetch_header(rpc, LO + 1)

    def post_open(body):
        return {"result": {"status": "success", "ledger": {**h[LO], "closed": False}}}

    rpc = crf.Rpc("x", post=post_open, sleep=lambda s: None, methods=vh.METHODS)
    with pytest.raises(crf.RpcError):
        vh.fetch_header(rpc, LO)


def test_only_read_only_methods():
    assert set(vh.METHODS) == {"server_info", "ledger"}
    assert {"lgrNotFound", "internal", "slowDown", "tooBusy"} <= vh.RETRY
