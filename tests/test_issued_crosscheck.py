"""issued/crosscheck_issued.py against a fake public node: MATCH, negative control, tampering, replay."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "issued"))

import analyze_issued as A  # noqa: E402
import collect_issued as C  # noqa: E402
import crosscheck_issued as X  # noqa: E402
import fixture_issued as F  # noqa: E402


@pytest.fixture
def snap(tmp_path):
    F.make_snapshot(tmp_path / "A")
    return A.load_snapshot(str(tmp_path / "A"))


def go(snap, node, sample=20, negative=False):
    out = []
    rpc = C.Rpc(node, out.append, retries=2, backoff=0, sleep=lambda s: None)
    bad = X.run(rpc, snap, sample, negative, out.append)
    return bad, out


def verdicts(out):
    return [x.split(" ")[0] for x in out if x.startswith(("MATCH", "MISMATCH"))]


def test_all_match(snap):
    bad, out = go(snap, F.FakeNode())
    v = verdicts(out)
    # 4 header + state set + 6 lines + amm set + 6 gateway + 6 sampled lines
    assert bad == 0 and v == ["MATCH"] * 24
    assert "SAMPLE seed=ledger_hash k=6 of 6 lines" in out


def test_negative_control_every_check_mismatches(snap):
    bad, out = go(snap, F.FakeNode(), negative=True)
    v = verdicts(out)
    assert v == ["MISMATCH"] * len(v) and bad == len(v) == 24


def test_tampered_public_line_and_extra_line(snap):
    objs = F.objects()
    for o in objs:
        if o["index"] == "4" * 64:
            o["Flags"] = F.FREEZE_LOW
    objs.append(F.line("8" * 64, "USD", "rH3", "rG2", "1", "5", "0"))
    objs.sort(key=lambda o: o["index"])
    bad, out = go(snap, F.FakeNode(objs=objs))
    mism = [x for x in out if x.startswith("MISMATCH")]
    assert any(x.startswith("MISMATCH state.index_set") and "extra=1" in x for x in mism)
    assert any(x == "MISMATCH state.object " + "4" * 64 for x in mism)
    assert any(x.startswith("MISMATCH account_lines") and "USD balance=" in x for x in mism)


def test_account_lines_balance_sign_checked(snap):
    def flipped(p):
        rows = F.account_lines(F.objects(), p["account"], p.get("peer"))
        for r in rows:
            if r["balance"] != "0":
                r["balance"] = r["balance"][1:] if r["balance"].startswith("-") else "-" + r["balance"]
        return rows

    bad, out = go(snap, F.FakeNode(account_lines=flipped))
    al = [x for x in out if " account_lines " in x or x.split(" ")[1] == "account_lines"]
    # 4 of the 6 lines have a non-zero balance
    assert sum(x.startswith("MISMATCH") for x in al) == 4


def test_sample_is_seeded_subset(snap):
    _, out1 = go(snap, F.FakeNode(), sample=2)
    _, out2 = go(snap, F.FakeNode(), sample=2)
    a1 = [x for x in out1 if "account_lines" in x]
    assert len(a1) == 2 and a1 == [x for x in out2 if "account_lines" in x]


def test_out_of_public_range_gives_up(snap):
    with pytest.raises(C.GaveUp, match="outside"):
        go(snap, F.FakeNode(lgr_range=(1001, 2000)))


def test_lgrnotfound_inside_range_retried(snap):
    node = F.FakeNode()
    real = node.__call__
    state = {"n": 0}

    def flaky(body):
        if json.loads(body)["method"] == "ledger" and state["n"] < 2:
            state["n"] += 1
            return node.err("lgrNotFound")
        return real(body)

    out = []
    rpc = C.Rpc(flaky, out.append, retries=2, backoff=0, sleep=lambda s: None)
    assert X.run(rpc, snap, 20, False, out.append) == 0
    assert sum(x.startswith("RETRY ledger lgrNotFound") for x in out) == 2


def test_record_then_offline_replay(tmp_path, snap):
    rec = tmp_path / "rec.json"
    out = []
    rpc = C.Rpc(X.recording(F.FakeNode(), str(rec)), out.append, sleep=lambda s: None)
    assert X.run(rpc, snap, 20, False, out.append) == 0
    out2 = []
    rpc2 = C.Rpc(X.replay(str(rec)), out2.append, sleep=lambda s: None)
    assert X.run(rpc2, snap, 20, False, out2.append) == 0
    assert verdicts(out) == verdicts(out2)
    rpc3 = C.Rpc(X.replay(str(rec)), out2.append, sleep=lambda s: None)
    with pytest.raises(X.NotRecorded):
        rpc3.call("ledger", {"ledger_index": 1})


def test_main_exit_codes(tmp_path, snap):
    rec = tmp_path / "rec.json"
    rpc = C.Rpc(X.recording(F.FakeNode(), str(rec)), lambda m: None, sleep=lambda s: None)
    X.run(rpc, snap, 20, False, lambda m: None)
    d = snap["dir"]
    assert X.main([d, "--offline", str(rec)]) == 0
    assert X.main([d, "--offline", str(rec), "--negative"]) == 1
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert X.main([d, "--offline", str(empty)]) == 3
