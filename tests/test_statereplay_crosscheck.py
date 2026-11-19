"""statereplay/crosscheck_replay.py against a fake public node built from the real capture fixture (F1F3):
sample sizes and determinism, rate limit, retry/backoff, give-up, public range, negative control,
record/offline."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statereplay"))
sys.path.insert(0, os.path.join(HERE, "..", "acctflags"))

import collect_flags as C  # noqa: E402
import crosscheck_replay as XC  # noqa: E402
import replay as R  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "statereplay", "F1F3")


def NOSLEEP(s):
    pass


class FakePublic:
    def __init__(self, world, complete="8100000-8200000", fail_first=0, tamper=None):
        self.ha, _, self.hb, target, rows, txs = world
        self.target = dict(target)  # the public node's own copy
        self.rows = {r["ledger_index"]: r for r in rows}
        self.txs = {r["ledger_index"]: [t["hash"] for t in r["transactions"]] for r in txs}
        self.complete, self.fail_first, self.tamper = complete, fail_first, tamper
        self.bodies = []

    def send(self, body):
        self.bodies.append(body)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise OSError("HTTP 503")
        req = json.loads(body)
        m, p = req["method"], req["params"][0]
        if m == "server_info":
            res = {"info": {"complete_ledgers": self.complete, "build_version": "1.0.9"}}
        elif m == "ledger":
            r = self.rows[p["ledger_index"]]
            res = {
                "ledger": {
                    "ledger_hash": r["ledger_hash"],
                    "parent_hash": r["parent_hash"],
                    "transactions": list(reversed(self.txs.get(p["ledger_index"], []))),
                }
            }
        elif m == "ledger_entry":
            if p["index"] not in self.target:
                res = {"status": "error", "error": "entryNotFound"}
            else:
                node = dict(self.target[p["index"]])
                if self.tamper:
                    node = self.tamper(node)
                res = {"node": node}
        else:
            raise AssertionError(m)
        res.setdefault("status", "success")
        return json.dumps({"result": res})


@pytest.fixture()
def world():
    return R.load_published(FIX)


def rpc_for(node, **kw):
    kw.setdefault("sleep", lambda s: None)
    return C.Rpc(node.send, lambda m: None, kw.pop("rate", 2), allow=XC.ALLOW, **kw)


def run(world, node, negative=False, n_ledgers=24, n_objects=64, **kw):
    ha, _, hb, target, rows, txs = world
    lines = []
    summary, ok = XC.run(
        rpc_for(node, **kw), ha, hb, target, rows, txs, n_ledgers, n_objects, negative, out=lines.append
    )
    return summary, ok, lines


def test_object_sample_is_64_and_deterministic(world):
    target = world[3]
    big = {f"{i:064X}": {} for i in range(500)}
    a = XC.draw_objects(big, 64, "seed")
    assert len(a) == 64 == len(set(a)) and a == XC.draw_objects(big, 64, "seed")
    assert a != XC.draw_objects(big, 64, "other")
    assert XC.draw_objects(target, 64, world[2]["ledger_hash"]) == XC.draw_objects(
        target, 64, world[2]["ledger_hash"]
    )
    assert len(XC.draw_objects(target, 64, "x")) == 64  # fixture B has 69 objects


def test_ledger_draw_always_includes_a_and_b():
    d = XC.draw_ledgers(100, 200, 24, "s")
    assert d[0] == 100 and d[-1] == 200 and len(d) == 26 and d == XC.draw_ledgers(100, 200, 24, "s")
    assert XC.draw_ledgers(100, 102, 24, "s") == [100, 101, 102]


def test_positive_run_matches_everything(world):
    node = FakePublic(world)
    summary, ok, lines = run(world, node)
    assert ok and summary["checks"] == 26 + 64 and summary["mismatch"] == 0
    assert lines[-1].endswith("PASS") and "mode=positive" in lines[-1]
    assert sum(1 for line in lines if line.startswith("OBJECT")) == 64


def test_negative_control_mismatches_everything(world):
    summary, ok, lines = run(world, FakePublic(world), negative=True)
    assert ok and summary["match"] == 0 and summary["mismatch"] == summary["checks"] == 90
    assert all(r["fields"] == ["ledger_hash"] for r in summary["ledgers"])
    assert all(r["fields"] in (["Flags"], ["NegativeControl"]) for r in summary["objects"])


def test_tampered_public_object_is_a_mismatch(world):
    def tamper(node):
        if node.get("LedgerEntryType") == "AccountRoot":
            node["Balance"] = "0"
        return node

    summary, ok, _ = run(world, FakePublic(world, tamper=tamper))
    assert not ok
    bad = [r for r in summary["objects"] if r["verdict"] == "MISMATCH"]
    assert bad and all(r["fields"] == ["Balance"] and r["type"] == "AccountRoot" for r in bad)


def test_missing_public_object_is_a_mismatch(world):
    node = FakePublic(world)
    victim = XC.draw_objects(world[3], 64, world[2]["ledger_hash"])[0]
    del node.target[victim]
    summary, ok, _ = run(world, node)
    assert not ok and [r["index"] for r in summary["objects"] if r["verdict"] == "MISMATCH"] == [victim]


def test_tx_hash_set_and_count_are_checked(world):
    node = FakePublic(world)
    n = next(r["ledger_index"] for r in world[5] if r["transactions"])
    node.txs[n] = node.txs[n][:-1]
    ha, _, hb, target, rows, txs = world
    res = XC.check_ledgers(
        rpc_for(node),
        rows,
        {r["ledger_index"]: sorted(t["hash"] for t in r["transactions"]) for r in txs},
        [n],
        False,
        lambda m: None,
    )
    assert res[0]["verdict"] == "MISMATCH" and res[0]["fields"] == ["txn_count", "tx_hashes"]


def test_out_of_public_range_stops_before_any_check(world):
    node = FakePublic(world, complete="8169700-8200000")
    with pytest.raises(C.GaveUp, match="outside the public complete_ledgers"):
        run(world, node)
    assert len(node.bodies) == 1  # only server_info


def test_rpc_rate_limit_spaces_calls(world):
    slept, now = [], [0.0]

    def clock():
        return now[0]

    def sleep(s):
        slept.append(s)
        now[0] += s

    run(world, FakePublic(world), n_ledgers=2, n_objects=2, rate=2, sleep=sleep, clock=clock)
    assert slept and all(abs(s - 0.5) < 1e-9 for s in slept)  # 2 req/s -> 0.5 s apart
    with pytest.raises(ValueError):
        C.Rpc(lambda b: b, print, rate=2.5)


def test_cli_default_rate_is_2(monkeypatch, tmp_path):
    seen = {}

    class Spy(C.Rpc):
        def __init__(self, *a, **kw):
            seen["rate"] = a[2]
            super().__init__(*a, **kw)

    monkeypatch.setattr(C, "Rpc", Spy)
    raw = to_raw(tmp_path)
    XC.main(
        [str(raw / "A"), str(raw / "B"), str(raw / "tx"), "--ledgers", "1", "--objects", "1"],
        transport=FakePublic(R.load_published(FIX)).send,
        sleep=NOSLEEP,
    )
    assert seen["rate"] == 2.0


def test_retry_backoff_is_logged_and_counted(world):
    logs, slept = [], []
    node = FakePublic(world, fail_first=2)
    ha, _, hb, target, rows, txs = world
    rpc = C.Rpc(node.send, logs.append, 2, retries=5, backoff=2.0, sleep=slept.append, allow=XC.ALLOW)
    summary, ok = XC.run(rpc, ha, hb, target, rows, txs, 1, 1, out=lambda m: None)
    assert ok and summary["retries"] == 2
    assert [line.split()[0:4] for line in logs] == [
        ["RETRY", "server_info", "try", "1/6"],
        ["RETRY", "server_info", "try", "2/6"],
    ]
    assert 2.0 in slept and 4.0 in slept


def test_gave_up_exits_2(tmp_path, capsys):
    raw = to_raw(tmp_path)
    node = FakePublic(R.load_published(FIX), fail_first=100)
    rc = XC.main(
        [str(raw / "A"), str(raw / "B"), str(raw / "tx"), "--retries", "1", "--backoff", "0"],
        transport=node.send,
        sleep=NOSLEEP,
    )
    assert rc == 2 and "GAVE_UP" in capsys.readouterr().out


def test_read_only_allowlist():
    rpc = C.Rpc(lambda b: "{}", print, 2, allow=XC.ALLOW)
    for method in ("submit", "sign", "wallet_propose", "account_info"):
        with pytest.raises(ValueError, match="refusing"):
            rpc.call(method, {})


def to_raw(tmp_path):
    """Write the fixture as collector-format raw dirs (A, B snapshots + capture) for the CLI."""
    ha, state, hb, target, rows, txs = R.load_published(FIX)
    for name, h, objs in (("A", ha, state), ("B", hb, target)):
        d = tmp_path / name / "pages"
        os.makedirs(d)
        (tmp_path / name / "header.json").write_text(json.dumps(h))
        (d / "full-0000.json").write_text(
            json.dumps({"ledger_hash": h["ledger_hash"], "state": list(objs.values())})
        )
    os.makedirs(tmp_path / "tx")
    (tmp_path / "tx" / "ledgers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "tx" / "tx.jsonl").write_text("".join(json.dumps(r) + "\n" for r in txs))
    return tmp_path


def test_record_then_offline_gives_same_summary(tmp_path):
    raw = to_raw(tmp_path)
    args = [str(raw / "A"), str(raw / "B"), str(raw / "tx"), "--ledgers", "3", "--objects", "5"]
    node = FakePublic(R.load_published(FIX))
    assert (
        XC.main(
            args + ["--record", str(tmp_path / "rec.json"), "--summary", str(tmp_path / "s1.json")],
            transport=node.send,
            sleep=NOSLEEP,
        )
        == 0
    )
    calls = len(node.bodies)
    assert (
        XC.main(args + ["--offline", str(tmp_path / "rec.json"), "--summary", str(tmp_path / "s2.json")]) == 0
    )
    assert len(node.bodies) == calls  # offline sent nothing
    assert json.load(open(tmp_path / "s1.json")) == json.load(open(tmp_path / "s2.json"))
    assert XC.main(args[:3] + ["--ledgers", "4", "--offline", str(tmp_path / "rec.json")]) == 3


def test_summary_names_no_address(tmp_path):
    raw = to_raw(tmp_path)
    XC.main(
        [str(raw / "A"), str(raw / "B"), str(raw / "tx"), "--summary", str(tmp_path / "s.json")],
        transport=FakePublic(R.load_published(FIX)).send,
        sleep=NOSLEEP,
    )
    s = json.load(open(tmp_path / "s.json"))
    assert set(s["objects"][0]) == {"index", "type", "verdict", "fields"}
    assert "acct_" not in json.dumps(s)
