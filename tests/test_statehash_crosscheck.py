"""statehash/crosscheck_statehash.py vs fake public nodes: match, a changed header field, dead endpoint."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "statehash"))

import crosscheck_statehash as X  # noqa: E402
import test_statehash_collect as T  # noqa: E402

S = T.S


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Rate-limit and backoff sleeps are real time.sleep calls: make them instant."""
    monkeypatch.setattr(X.time, "sleep", lambda s: None)


def setup(tmp_path):
    node = T.FakeNode(top=T.START + 20)
    S.capture(T.rpc_for(node), str(tmp_path / "cap"), T.START, to=T.START + 20, log=T.quiet)
    S.snapshot(T.rpc_for(node), str(tmp_path / "A"), T.START, log=T.quiet)
    return node


def run(tmp_path, monkeypatch, send, n=5):
    monkeypatch.setattr(X.C, "https_transport", lambda: send)
    out = tmp_path / "x.json"
    rc = X.main(
        [
            "--cap",
            str(tmp_path / "cap"),
            "--a",
            str(tmp_path / "A"),
            "--n",
            str(n),
            "--rate",
            "2",
            "--json",
            str(out),
        ]
    )
    return rc, json.loads(out.read_text())


def test_all_samples_match(tmp_path, monkeypatch, capsys):
    node = setup(tmp_path)
    rc, rep = run(tmp_path, monkeypatch, node.send)
    assert rc == 0 and all(r["ok"] for r in rep["results"])
    idxs = [r["ledger_index"] for r in rep["results"] if "snapshot" not in r]
    assert T.START in idxs and T.START + 20 in idxs and len(idxs) >= 5
    assert "RESULT PASS" in capsys.readouterr().out


def test_changed_public_account_hash_fails_naming_field(tmp_path, monkeypatch, capsys):
    node = setup(tmp_path)

    def send(body):
        r = json.loads(node.send(body))
        if r["result"].get("ledger", {}).get("ledger_index") == T.START + 20:
            r["result"]["ledger"]["account_hash"] = "00" * 32
        return json.dumps(r)

    rc, rep = run(tmp_path, monkeypatch, send)
    assert rc == 1
    bad = [r for r in rep["results"] if not r["ok"]]
    assert [(r["ledger_index"], r["mismatch"]) for r in bad] == [(T.START + 20, ["account_hash"])]
    assert f"FAIL {T.START + 20} ['account_hash']" in capsys.readouterr().out


def test_dead_endpoint_fails_closed(tmp_path, monkeypatch):
    setup(tmp_path)

    def dead(_body):
        raise OSError("connection refused")

    rc, rep = run(tmp_path, monkeypatch, dead, n=2)
    assert rc == 1 and not any(r["ok"] for r in rep["results"])
    assert all("GaveUp" in r["error"] for r in rep["results"])
