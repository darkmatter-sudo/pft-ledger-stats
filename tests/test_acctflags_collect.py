"""acctflags/collect_flags.py against a fake node: paging, refusal, resume, rate limit, capture chain."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "acctflags"))

import collect_flags as C  # noqa: E402
import fixture_acctflags as F  # noqa: E402


def quiet(_m):
    pass


def rpc_for(node, **kw):
    kw.setdefault("sleep", lambda s: None)
    return C.Rpc(node.send, quiet, rate=2, **kw)


def test_snapshot_pages_and_manifest(tmp_path):
    node = F.FakeNode()
    man = C.snapshot(rpc_for(node), str(tmp_path / "A"), F.A_INDEX, log=quiet, limit=2)
    assert man["ledger_index"] == F.A_INDEX and man["ledger_hash"] == F.lhash(F.A_INDEX)
    assert man["files"]["accounts.jsonl"]["rows"] == 5
    counts = json.loads((tmp_path / "A" / "counts.json").read_text())
    assert counts["full_tree_by_type"] == {"AccountRoot": 5, "DirectoryNode": 1}
    assert counts["full_tree_pages"] == 3 and counts["accountroot_type_filter_pages"] == 3
    rows = [json.loads(x) for x in (tmp_path / "A" / "accounts.jsonl").read_text().splitlines()]
    assert [r["Account"] for r in rows] == sorted(r["Account"] for r in rows)


def test_snapshot_refuses_type_filter_difference(tmp_path):
    node = F.FakeNode()
    node.drop_type_filter = True
    with pytest.raises(SystemExit, match="differ"):
        C.snapshot(rpc_for(node), str(tmp_path / "A"), F.A_INDEX, log=quiet, limit=2)
    assert not (tmp_path / "A" / "MANIFEST.json").exists()


def test_snapshot_refuses_complete_dir_and_other_ledger(tmp_path):
    node = F.FakeNode()
    C.snapshot(rpc_for(node), str(tmp_path / "A"), F.A_INDEX, log=quiet)
    with pytest.raises(SystemExit, match="already holds"):
        C.snapshot(rpc_for(node), str(tmp_path / "A"), F.A_INDEX, log=quiet)
    os.remove(tmp_path / "A" / "MANIFEST.json")
    with pytest.raises(SystemExit, match="pinned to"):
        C.snapshot(rpc_for(node), str(tmp_path / "A"), F.B_INDEX, log=quiet)


def test_snapshot_resumes_from_saved_pages(tmp_path):
    node = F.FakeNode()
    first = C.snapshot(rpc_for(node), str(tmp_path / "A"), F.A_INDEX, log=quiet, limit=2)
    os.remove(tmp_path / "A" / "MANIFEST.json")
    os.remove(tmp_path / "A" / "pages" / "account-0002.json")
    node2 = F.FakeNode()
    logs = []
    again = C.snapshot(rpc_for(node2), str(tmp_path / "A"), F.A_INDEX, log=logs.append, limit=2)
    assert node2.calls == ["ledger_data"]  # only the missing page; the pin is reused
    assert sum(1 for m in logs if m.startswith("RESUME page")) == 5
    assert again["files"] == first["files"]


def test_snapshot_refuses_page_of_other_ledger(tmp_path):
    node = F.FakeNode()
    C.snapshot(rpc_for(node), str(tmp_path / "A"), F.A_INDEX, log=quiet, limit=2)
    os.remove(tmp_path / "A" / "MANIFEST.json")
    p = tmp_path / "A" / "pages" / "full-0000.json"
    d = json.loads(p.read_text())
    d["ledger_hash"] = F.lhash(5)
    p.write_text(json.dumps(d))
    with pytest.raises(SystemExit, match="belongs to ledger"):
        C.snapshot(rpc_for(F.FakeNode()), str(tmp_path / "A"), F.A_INDEX, log=quiet, limit=2)


def test_snapshot_rejects_unsynced_and_unvalidated(tmp_path):
    class Unsynced(F.FakeNode):
        def send(self, body):
            if json.loads(body)["method"] == "server_state":
                return json.dumps({"result": {"state": {"server_state": "connected"}}})
            return super().send(body)

    with pytest.raises(SystemExit, match="not synced"):
        C.snapshot(rpc_for(Unsynced()), str(tmp_path / "A"), None, log=quiet)

    class Unvalidated(F.FakeNode):
        def send(self, body):
            out = json.loads(super().send(body))
            out["result"]["validated"] = False
            return json.dumps(out)

    with pytest.raises(SystemExit, match="not validated"):
        C.snapshot(rpc_for(Unvalidated()), str(tmp_path / "B"), F.A_INDEX, log=quiet)


def test_ledger_data_for_wrong_hash_refused(tmp_path):
    class Wrong(F.FakeNode):
        def send(self, body):
            out = json.loads(super().send(body))
            if json.loads(body)["method"] == "ledger_data":
                out["result"]["ledger_hash"] = "0" * 64
            return json.dumps(out)

    with pytest.raises(ValueError, match="pinned"):
        C.snapshot(rpc_for(Wrong()), str(tmp_path / "A"), F.A_INDEX, log=quiet)


def test_rpc_refuses_write_methods():
    rpc = rpc_for(F.FakeNode())
    for m in ("submit", "sign", "wallet_propose", "account_info"):
        with pytest.raises(ValueError, match="non read-only"):
            rpc.call(m, {})
    assert C.Rpc(F.FakeNode().send, quiet, allow={"account_info"}).allow == frozenset({"account_info"})


def test_rpc_rate_limit_spaces_calls():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rpc = C.Rpc(F.FakeNode().send, quiet, rate=2, sleep=sleep, clock=lambda: t[0])
    for _ in range(5):
        rpc.call("server_state", {})
    assert t[0] == pytest.approx(2.0)  # 5 calls, 0.5 s apart
    assert slept == [pytest.approx(0.5)] * 4
    for bad in (0, -1, 2.5):
        with pytest.raises(ValueError, match="rate"):
            C.Rpc(F.FakeNode().send, quiet, rate=bad)


def test_rpc_retries_transient_with_logged_backoff():
    node = F.FakeNode()
    node.fail_next = 3
    logs, slept = [], []
    rpc = C.Rpc(node.send, logs.append, rate=2, retries=5, backoff=1.0, sleep=slept.append, clock=lambda: 0.0)
    assert rpc.call("server_state", {})["state"]["server_state"] == "full"
    retry_sleeps = [s for s in slept if s >= 1.0]
    assert retry_sleeps == [1.0, 2.0, 4.0]
    assert [m.split()[0] for m in logs] == ["RETRY"] * 3 and "try 1/6" in logs[0]
    assert rpc.retried == 3 and rpc.calls == 4


def test_rpc_gives_up_and_definite_error_not_retried():
    node = F.FakeNode()
    node.fail_next = 10
    rpc = rpc_for(node, retries=2)
    with pytest.raises(C.GaveUp, match="3 tries"):
        rpc.call("server_state", {})
    node2 = F.FakeNode(validated=50)
    rpc2 = rpc_for(node2, retries=5)
    with pytest.raises(C.RpcError) as e:
        rpc2.call("ledger", {"ledger_index": 60})
    assert e.value.error == "lgrNotFound" and node2.calls == ["ledger"]


def test_rpc_garbage_answer_is_retried():
    answers = iter(["not json", '{"no_result": 1}', '{"result": {"ok": 1}}'])
    rpc = C.Rpc(lambda b: next(answers), quiet, rate=2, sleep=lambda s: None)
    assert rpc.call("server_state", {}) == {"ok": 1}
    assert rpc.retried == 2


def test_capture_window_and_line_alignment(tmp_path):
    node = F.FakeNode()
    last = C.capture(rpc_for(node), str(tmp_path), 101, 110, log=quiet, sleep=lambda s: None)
    assert last == 110
    led = [json.loads(x) for x in (tmp_path / "ledgers.jsonl").read_text().splitlines()]
    txs = [json.loads(x) for x in (tmp_path / "tx.jsonl").read_text().splitlines()]
    assert [r["ledger_index"] for r in led] == list(range(101, 111))
    assert [t["ledger_index"] for t in txs] == list(range(101, 111))
    assert [r["txn_count"] for r in led] == [len(F.window_txs().get(n, [])) for n in range(101, 111)]
    assert led[0]["parent_hash"] == F.lhash(100)


def test_capture_resume_repairs_torn_tail_and_orphans(tmp_path):
    node = F.FakeNode()
    C.capture(rpc_for(node), str(tmp_path), 101, 105, log=quiet, sleep=lambda s: None)
    with open(tmp_path / "tx.jsonl", "a") as fh:
        fh.write(json.dumps({"ledger_index": 106, "transactions": []}) + "\n")  # orphan: no ledgers line
    with open(tmp_path / "ledgers.jsonl", "a") as fh:
        fh.write('{"ledger_index": 106, "ledger_ha')  # torn tail
    logs = []
    node2 = F.FakeNode()
    C.capture(rpc_for(node2), str(tmp_path), 101, 110, log=logs.append, sleep=lambda s: None)
    assert any(m.startswith("REPAIR dropped 1") for m in logs)
    assert any("next=106" in m for m in logs)
    led = [json.loads(x) for x in (tmp_path / "ledgers.jsonl").read_text().splitlines()]
    txs = [json.loads(x) for x in (tmp_path / "tx.jsonl").read_text().splitlines()]
    assert [r["ledger_index"] for r in led] == list(range(101, 111))
    assert [t["ledger_index"] for t in txs] == list(range(101, 111))
    assert node2.calls.count("ledger") == 5


def test_capture_refuses_other_start_and_misaligned_files(tmp_path):
    C.capture(rpc_for(F.FakeNode()), str(tmp_path), 101, 103, log=quiet, sleep=lambda s: None)
    with pytest.raises(SystemExit, match="captures from 101"):
        C.capture(rpc_for(F.FakeNode()), str(tmp_path), 102, 110, log=quiet, sleep=lambda s: None)
    lines = (tmp_path / "ledgers.jsonl").read_text().splitlines()
    (tmp_path / "ledgers.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    with pytest.raises(C.ChainBreak, match="expected 102"):
        C.capture(rpc_for(F.FakeNode()), str(tmp_path), 101, 110, log=quiet, sleep=lambda s: None)


def test_capture_tx_file_without_ledger_rows_is_misaligned(tmp_path):
    C.capture(rpc_for(F.FakeNode()), str(tmp_path), 101, 103, log=quiet, sleep=lambda s: None)
    lines = (tmp_path / "tx.jsonl").read_text().splitlines()
    (tmp_path / "tx.jsonl").write_text(lines[0] + "\n")
    with pytest.raises(C.ChainBreak, match="line-aligned"):
        C.capture(rpc_for(F.FakeNode()), str(tmp_path), 101, 110, log=quiet, sleep=lambda s: None)


def test_capture_chain_break_stops(tmp_path):
    class Forked(F.FakeNode):
        def send(self, body):
            out = json.loads(super().send(body))
            led = out.get("result", {}).get("ledger")
            if led and led["ledger_index"] == "105":
                led["parent_hash"] = "F" * 64
            return json.dumps(out)

    with pytest.raises(C.ChainBreak, match="ledger 105 parent_hash"):
        C.capture(rpc_for(Forked()), str(tmp_path), 101, 110, log=quiet, sleep=lambda s: None)
    led = (tmp_path / "ledgers.jsonl").read_text().splitlines()
    assert len(led) == 4  # 101..104 kept, nothing recorded for the broken ledger


def test_capture_refuses_unexpanded_tx_and_wrong_index(tmp_path):
    class Hashes(F.FakeNode):
        def send(self, body):
            out = json.loads(super().send(body))
            led = out.get("result", {}).get("ledger")
            if led and led.get("transactions"):
                led["transactions"] = ["ABC"]
            return json.dumps(out)

    with pytest.raises(C.ChainBreak, match="expanded"):
        C.capture(rpc_for(Hashes()), str(tmp_path / "a"), 101, 110, log=quiet, sleep=lambda s: None)

    class Shifted(F.FakeNode):
        def send(self, body):
            out = json.loads(super().send(body))
            led = out.get("result", {}).get("ledger")
            if led and led["ledger_index"] == "103":
                led["ledger_index"] = "104"
            return json.dumps(out)

    with pytest.raises(C.ChainBreak, match="asked for ledger 103"):
        C.capture(rpc_for(Shifted()), str(tmp_path / "b"), 101, 110, log=quiet, sleep=lambda s: None)


def test_capture_pruned_validated_ledger_is_a_break(tmp_path):
    class Pruned(F.FakeNode):
        def send(self, body):
            req = json.loads(body)
            if req["method"] == "ledger" and req["params"][0].get("ledger_index") == 102:
                return json.dumps({"result": {"status": "error", "error": "lgrNotFound"}})
            return super().send(body)

    with pytest.raises(C.ChainBreak, match="pruned"):
        C.capture(rpc_for(Pruned()), str(tmp_path), 101, 110, log=quiet, sleep=lambda s: None)


def test_capture_waits_for_validation(tmp_path):
    node = F.FakeNode(validated=103)
    sleeps = []
    last = C.capture(rpc_for(node), str(tmp_path), 101, 110, log=quiet, sleep=sleeps.append, max_idle=3)
    assert last == 103 and sleeps == [2.0, 2.0, 2.0]  # 4th idle poll gives up (max_idle=3)
    assert node.calls.count("ledger") == 3


def test_capture_stops_on_signal(tmp_path):
    node = F.FakeNode()
    C.STOP.append(15)
    try:
        last = C.capture(rpc_for(node), str(tmp_path), 101, 110, log=quiet, sleep=lambda s: None)
    finally:
        C.STOP.clear()
    assert last == 100 and node.calls == []


def test_main_exit_codes(tmp_path, monkeypatch):
    node = F.FakeNode()
    node.fail_next = 100
    monkeypatch.setattr(C, "docker_transport", lambda container: node.send)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    assert C.main(["snapshot", "--out", str(tmp_path / "A"), "--ledger", "100", "--retries", "1"]) == 2

    class Forked(F.FakeNode):
        def send(self, body):
            out = json.loads(super().send(body))
            led = out.get("result", {}).get("ledger")
            if led and led["ledger_index"] == "102":
                led["parent_hash"] = "F" * 64
            return json.dumps(out)

    forked = Forked()
    monkeypatch.setattr(C, "docker_transport", lambda container: forked.send)
    log = tmp_path / "c.log"
    rc = C.main(
        ["capture", "--out", str(tmp_path / "tx"), "--start", "101", "--to", "110", "--log", str(log)]
    )
    assert rc == 3 and "CHAIN_BREAK" in log.read_text()
