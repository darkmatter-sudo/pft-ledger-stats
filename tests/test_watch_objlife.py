"""watch_objlife.py: deferred-type filter, ordering, resume with torn tail and orphan lines."""

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "objlife"))

import watch_objlife as W  # noqa: E402

TYPES = {
    "entries": [
        {"name": "Offer", "deferred": True},
        {"name": "Escrow", "deferred": True},
        {"name": "NFTokenPage", "deferred": False},
        {"name": "AccountRoot", "deferred": False},
    ]
}


def node(kind, t):
    return {kind: {"LedgerEntryType": t, "LedgerIndex": "00" * 32}}


def ledger(seq, txs):
    return {
        "ledger_index": str(seq),
        "close_time": 1000 + seq,
        "ledger_hash": f"{seq:064X}",
        "closed": True,
        "transactions": txs,
    }


def t(h, i, *nodes):
    return {
        "hash": h,
        "TransactionType": "X",
        "metaData": {"TransactionIndex": i, "AffectedNodes": list(nodes)},
    }


def setup_module():
    W.DEFERRED.clear()
    W.DEFERRED.update(e["name"] for e in TYPES["entries"] if e["deferred"])


def test_filter_keeps_only_deferred_types_sorted():
    led = ledger(
        10,
        [
            t("B", 2, node("DeletedNode", "Offer"), node("ModifiedNode", "AccountRoot")),
            t("C", 0, node("ModifiedNode", "AccountRoot")),
            t("D", 3, node("ModifiedNode", "NFTokenPage")),  # not a deferred type here
            t("A", 1, node("CreatedNode", "Escrow")),
        ],
    )
    lines, n_all = W.deferred_lines(led)
    assert n_all == 4
    assert [x["tx"]["hash"] for x in lines] == ["A", "B"]
    assert lines[0]["meta"]["AffectedNodes"][0]["CreatedNode"]["LedgerEntryType"] == "Escrow"
    assert "metaData" not in lines[0]["tx"]


class FakeRpc:
    def __init__(self, ledgers, validated):
        self.ledgers, self.validated, self.count = ledgers, validated, 0

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            return {
                "info": {
                    "build_version": "t",
                    "server_state": "full",
                    "complete_ledgers": "1-99",
                    "validated_ledger": {"seq": self.validated},
                }
            }
        if method == "ledger":
            return {"ledger": self.ledgers[params["ledger_index"]]}
        raise AssertionError(method)


LEDGERS = {
    11: ledger(11, [t("H1", 0, node("CreatedNode", "Offer"))]),
    12: ledger(12, []),
    13: ledger(13, [t("H3", 0, node("DeletedNode", "Offer")), t("H4", 1, node("ModifiedNode", "Escrow"))]),
}


def run(tmp_path, monkeypatch, stop_at):
    types = tmp_path / "types.json"
    types.write_text(json.dumps(TYPES))
    monkeypatch.setattr(W, "Rpc", lambda *a: FakeRpc(LEDGERS, 13))
    monkeypatch.setattr(W, "_stop", False)
    out = tmp_path / "tx"
    rc = W.main(
        [
            "--types",
            str(types),
            "--out",
            str(out),
            "--start",
            "11",
            "--stop-at",
            str(stop_at),
            "--log",
            str(tmp_path / "log"),
        ]
    )
    return rc, out


def rows(out):
    with open(out / "ledgers.csv") as fh:
        return [(int(r["ledger_index"]), int(r["obj_tx_count"])) for r in csv.DictReader(fh)]


def test_window_then_resume_after_torn_tail_and_orphans(tmp_path, monkeypatch):
    rc, out = run(tmp_path, monkeypatch, 12)
    assert rc == 0 and rows(out) == [(11, 1), (12, 0)]
    # simulate a SIGKILL during ledger 13: its tx lines written, ledgers.csv row torn
    with open(out / "obj-tx.jsonl", "a") as fh:
        fh.write(json.dumps({"ledger_index": 13, "tx": {"hash": "H3"}}) + "\n")
    with open(out / "ledgers.csv", "a") as fh:
        fh.write("13,ABC")
    st = json.loads((out / "state.json").read_text())
    st["stop_at"] = 13
    (out / "state.json").write_text(json.dumps(st))
    rc, out = run(tmp_path, monkeypatch, 13)
    assert rc == 0 and rows(out) == [(11, 1), (12, 0), (13, 2)]
    hashes = [json.loads(x)["tx"]["hash"] for x in (out / "obj-tx.jsonl").read_text().splitlines()]
    assert hashes == ["H1", "H3", "H4"]  # orphan H3 dropped, then fetched again once
    log = (tmp_path / "log").read_text()
    assert "RESUME dropped torn last line of ledgers.csv" in log
    assert "RESUME dropped 1 obj-tx.jsonl lines after ledger 12" in log


def test_drop_orphan_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("".join(json.dumps({"ledger_index": i}) + "\n" for i in (5, 6, 7, 7)))
    assert W.drop_orphan_lines(str(p), 6) == 2
    assert [json.loads(x)["ledger_index"] for x in p.read_text().splitlines()] == [5, 6]
    assert W.drop_orphan_lines(str(p), 6) == 0
