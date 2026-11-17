"""acctflags/crosscheck_flags.py vs a fake public node: match, negative control, actNotFound, replay."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "acctflags"))

import collect_flags as C  # noqa: E402
import crosscheck_flags as XC  # noqa: E402
import extract_flags as X  # noqa: E402
import fixture_acctflags as F  # noqa: E402


class Public:
    """Answers server_info, ledger and account_info from the fixture's snapshot B state."""

    def __init__(self, complete="1-1000", accounts=None):
        self.complete = complete
        self.accounts = {o["Account"]: o for o in (accounts or F.accounts_b())}
        self.calls = []

    def send(self, body):
        req = json.loads(body)
        m, p = req["method"], req["params"][0]
        self.calls.append(m)
        if m == "server_info":
            return json.dumps({"result": {"info": {"complete_ledgers": self.complete}}})
        if m == "ledger":
            n = F.FakeNode.index_of(p["ledger_hash"])
            return json.dumps({"result": {"ledger": F.ledger_header(n), "validated": True}})
        if m == "account_info":
            o = self.accounts.get(p["account"])
            if o is None:
                return json.dumps({"result": {"status": "error", "error": "actNotFound"}})
            return json.dumps({"result": {"account_data": o, "ledger_hash": p["ledger_hash"]}})
        raise AssertionError(m)


def rpc(send):
    return C.Rpc(send, lambda m: None, rate=2, sleep=lambda s: None, allow=XC.ALLOW)


@pytest.fixture
def snap_b(tmp_path):
    _a, b, _c = F.make_raw(tmp_path)
    return b


def test_all_match_and_absent_account(snap_b):
    _m, hdr, _c, roots = X.load_snapshot(str(snap_b))
    addrs = sorted(F.ADDR.values())  # includes Dave, deleted before B
    out = []
    res = XC.run(rpc(Public().send), hdr, roots, addrs, F.KEY, out=out.append)
    assert all(r["ok"] for r in res) and len(res) == 7
    dave = [r for r in res if r.get("id") == F.pid("Dave")][0]
    assert dave["in_snapshot"] is False
    assert any("actNotFound" in line for line in out)
    assert not any(a in "\n".join(out) for a in F.ADDR.values())
    assert out[-1].startswith("SUMMARY ledger=110 accounts=6 checks=7 match=7 mismatch=0")


def test_negative_control_every_account_mismatches(snap_b):
    _m, hdr, _c, roots = X.load_snapshot(str(snap_b))
    addrs = [o["Account"] for o in roots]
    res = XC.run(rpc(Public().send), hdr, roots, addrs, F.KEY, negative=True, out=lambda m: None)
    acc = [r for r in res if r["check"] == "account"]
    assert len(acc) == 5 and not any(r["ok"] for r in acc)


def test_field_difference_is_mismatch(snap_b):
    _m, hdr, _c, roots = X.load_snapshot(str(snap_b))
    changed = [dict(o, Domain="AA") if o["Account"] == F.ADDR["Bob"] else o for o in F.accounts_b()]
    res = XC.run(rpc(Public(accounts=changed).send), hdr, roots, [F.ADDR["Bob"]], F.KEY, out=lambda m: None)
    assert [r["ok"] for r in res] == [True, False]


def test_header_mismatch(snap_b):
    _m, hdr, _c, roots = X.load_snapshot(str(snap_b))
    hdr = dict(hdr, ledger_index=111)
    res = XC.run(rpc(Public().send), hdr, roots, [], F.KEY, out=lambda m: None)
    assert res == [{"check": "header", "ok": False}]


def test_out_of_public_range_gives_up(snap_b):
    _m, hdr, _c, roots = X.load_snapshot(str(snap_b))
    with pytest.raises(C.GaveUp, match="outside public"):
        XC.run(rpc(Public(complete="200-300").send), hdr, roots, [], F.KEY, out=lambda m: None)
    assert XC.in_range("1-5,9,20-30", 9) and XC.in_range("1-5,9,20-30", 25) and not XC.in_range("empty", 3)


def test_draw_takes_flagged_first_and_is_seeded():
    roots = F.accounts_a() + [F.root("Frank")]
    one = XC.draw(roots, 4, "seed")
    assert one == XC.draw(roots, 4, "seed") and len(one) == 4
    flagged = {F.ADDR[n] for n in ("Bob", "Carol")}  # 2 of the 3 flagged fit into half of 4
    assert len(flagged & set(one)) + (F.ADDR["Erin"] in one) >= 2
    assert set(XC.draw(roots, 100, "x")) == {o["Account"] for o in roots}


def test_main_record_offline_and_not_recorded(snap_b, tmp_path, monkeypatch, capsys):
    pub = Public()
    monkeypatch.setattr(XC, "http_transport", lambda: pub.send)
    key = tmp_path / "key"
    sample = tmp_path / "sample.json"
    rec = tmp_path / "rec.json"
    summ = tmp_path / "sum.json"
    base = [str(snap_b), "--sample-file", str(sample), "--key-file", str(key)]
    assert XC.main(base + ["--count", "4", "--record", str(rec), "--summary", str(summ)]) == 0
    assert (os.stat(sample).st_mode & 0o777) == 0o600 and len(json.load(open(sample))["addresses"]) == 4
    assert json.load(open(summ))["negative"] is False
    n_live = len(pub.calls)
    assert XC.main(base + ["--offline", str(rec)]) == 0 and len(pub.calls) == n_live
    assert XC.main(base + ["--offline", str(rec), "--negative"]) == 1
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps(["rNotRecorded"]))
    assert XC.main(base + ["--offline", str(rec), "--extra", str(extra)]) == 3
    assert "NOT_RECORDED" in capsys.readouterr().out


def test_main_gave_up_exit_2(snap_b, tmp_path, monkeypatch):
    monkeypatch.setattr(XC, "http_transport", lambda: Public(complete="500-600").send)
    rc = XC.main([str(snap_b), "--sample-file", str(tmp_path / "s"), "--key-file", str(tmp_path / "k")])
    assert rc == 2


def test_allowlist_blocks_other_methods():
    r = rpc(Public().send)
    with pytest.raises(ValueError):
        r.call("ledger_data", {})
    with pytest.raises(ValueError):
        r.call("submit", {})
