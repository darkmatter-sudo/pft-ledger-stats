"""issued/collect_issued.py against a fake node: paging, reconciliation refusal, retries, determinism."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "issued"))

import collect_issued as C  # noqa: E402
import fixture_issued as F  # noqa: E402


def test_snapshot_files_and_manifest(tmp_path):
    node = F.FakeNode()
    man = F.make_snapshot(tmp_path / "A", node, limit=4)
    assert man["ledger_hash"] == F.LEDGER_HASH and man["ledger_index"] == F.LEDGER_INDEX
    rows = {n: m["rows"] for n, m in man["files"].items()}
    assert rows["ripplestate.jsonl"] == 6
    assert rows["accounts.jsonl"] == 6  # every line party, not rX9
    assert rows["gateway_balances.jsonl"] == 6
    assert rows["amm.jsonl"] == 0
    assert rows["amm_info.jsonl"] == 3  # (G1,USD) (G2,USD) (G2,EURO): issuers with obligations only
    counts = json.loads((tmp_path / "A" / "counts.json").read_text())
    assert counts["full_tree_by_type"] == {"AccountRoot": 7, "DirectoryNode": 1, "RippleState": 6}
    assert counts["filtered"]["RippleState"]["index_sets_equal"] is True
    assert counts["filtered"]["RippleState"]["pages"] == 4  # 14 objects / limit 4
    assert set(node.methods) <= C.READ_ONLY


def test_bytes_independent_of_page_size(tmp_path):
    """Object files are byte-identical for any page size; counts.json differs only in the page counts."""
    F.make_snapshot(tmp_path / "a", limit=3)
    F.make_snapshot(tmp_path / "b", limit=100)
    for n in C.FILES:
        if n == "counts.json":
            ca = json.loads((tmp_path / "a" / n).read_text())
            cb = json.loads((tmp_path / "b" / n).read_text())
            assert [ca["filtered"][t].pop("pages") for t in ("AMM", "RippleState")] == [5, 5]
            assert [cb["filtered"][t].pop("pages") for t in ("AMM", "RippleState")] == [1, 1]
            assert ca == cb
        else:
            assert (tmp_path / "a" / n).read_bytes() == (tmp_path / "b" / n).read_bytes(), n


def test_type_filter_difference_refused_no_manifest(tmp_path):
    node = F.FakeNode(drop_filtered="2" * 64)
    with pytest.raises(SystemExit, match="differ"):
        F.make_snapshot(tmp_path / "A", node)
    assert not (tmp_path / "A" / "MANIFEST.json").exists()
    assert "gateway_balances" not in node.methods


def test_existing_snapshot_not_overwritten(tmp_path):
    F.make_snapshot(tmp_path / "A")
    node = F.FakeNode()
    with pytest.raises(SystemExit, match="already holds"):
        F.make_snapshot(tmp_path / "A", node)
    assert node.methods == []


def test_unsynced_node_refused(tmp_path):
    node = F.FakeNode(
        faults=[json.dumps({"result": {"status": "success", "state": {"server_state": "syncing"}}})]
    )
    with pytest.raises(SystemExit, match="not synced"):
        F.make_snapshot(tmp_path / "A", node)


def test_retry_logged_then_success():
    logs = []
    node = F.FakeNode(
        faults=[OSError("docker exec rc=1"), json.dumps({"result": {"status": "error", "error": "tooBusy"}})]
    )
    slept = []
    rpc = C.Rpc(node, logs.append, retries=3, backoff=0.5, sleep=slept.append)
    assert rpc.call("server_state", {})["state"]["server_state"] == "full"
    assert slept == [0.5, 1.0]
    assert logs == [
        "RETRY server_state try 1/4 OSError: docker exec rc=1 sleep=0.5s",
        "RETRY server_state try 2/4 error=tooBusy sleep=1s",
    ]
    assert (rpc.calls, rpc.retried) == (3, 2)


def test_gave_up_after_retries():
    node = F.FakeNode(faults=["not json"] * 3)
    rpc = C.Rpc(node, lambda m: None, retries=2, backoff=0, sleep=lambda s: None)
    with pytest.raises(C.GaveUp):
        rpc.call("ledger", {})
    assert rpc.calls == 3


def test_definite_error_not_retried():
    node = F.FakeNode()
    rpc = C.Rpc(node, lambda m: None, retries=5, backoff=0, sleep=lambda s: None)
    with pytest.raises(C.RpcError) as e:
        rpc.call("amm_info", {})
    assert e.value.error == "actNotFound" and rpc.calls == 1


def test_write_methods_refused():
    node = F.FakeNode()
    rpc = C.Rpc(node, lambda m: None)
    for m in ("submit", "sign", "wallet_propose", "ledger_accept"):
        with pytest.raises(ValueError):
            rpc.call(m, {})
    assert node.methods == []


def test_ledger_hash_pin_enforced(tmp_path):
    node = F.FakeNode()
    real = node.res

    def other(r):
        s = json.loads(real(r))
        if "state" in s["result"] and isinstance(s["result"]["state"], list):
            s["result"]["ledger_hash"] = "00" * 32
        return json.dumps(s)

    node.res = other
    with pytest.raises(ValueError, match="pinned"):
        F.make_snapshot(tmp_path / "A", node)
