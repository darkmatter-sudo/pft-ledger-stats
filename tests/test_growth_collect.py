import csv
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "growth"))
import collect_growth as cg  # noqa: E402

BASE_CT = 815000000  # ripple time


def half(prefix, hexdata):
    return hashlib.sha512(prefix + bytes.fromhex(hexdata)).digest()[:32].hex().upper()


def header_hex(idx):
    return (b"HDR" + str(idx).encode()).hex().upper().ljust(236, "0")  # 118 bytes, like a real header


def blob(idx, k, n):
    """n-byte tx blob unique per (ledger, tx)."""
    return (f"{idx:08d}{k:04d}".encode().hex() * 20)[: 2 * n].upper().ljust(2 * n, "A")


def pair(idx, specs, ct=None):
    """specs: list of (blob_bytes, meta_bytes, [(kind, entry_type), ...], tx_type).

    Returns (json_ledger, binary_ledger) that agree the way a real node's two answers do.
    """
    hdr = header_hex(idx)
    btx, jtx = [], []
    for k, (nb, nm, nodes, tt) in enumerate(specs):
        b = blob(idx, k, nb)
        btx.append({"tx_blob": b, "meta": "C" * (2 * nm)})
        jtx.append(
            {
                "hash": half(b"TXN\x00", b),
                "TransactionType": tt,
                "metaData": {
                    "TransactionIndex": k,
                    "TransactionResult": "tesSUCCESS",
                    "AffectedNodes": [{kind: {"LedgerEntryType": et}} for kind, et in nodes],
                },
            }
        )
    j = {
        "ledger_index": str(idx),
        "ledger_hash": half(b"LWR\x00", hdr),
        "close_time": ct if ct is not None else BASE_CT + (idx - 100) * 4,
        "closed": True,
        "transactions": list(reversed(jtx)),
    }  # JSON order != index order on purpose
    return j, {"ledger_data": hdr, "closed": True, "transactions": btx}


SPEC = [
    (182, 322, [("ModifiedNode", "AccountRoot"), ("ModifiedNode", "AccountRoot")], "Payment"),
    (
        200,
        500,
        [
            ("CreatedNode", "AccountRoot"),
            ("ModifiedNode", "AccountRoot"),
            ("DeletedNode", "Offer"),
            ("CreatedNode", "DirectoryNode"),
        ],
        "Payment",
    ),
]


def test_growth_rows_hand_computed():
    led, txs, nodes = cg.growth_rows(*pair(100, SPEC))
    # hand totals: blob 182+200, meta 322+500, header 118, created 2, modified 3, deleted 1
    assert (led["txn_count"], led["header_bytes"], led["tx_blob_bytes"], led["meta_bytes"]) == (
        2,
        118,
        382,
        822,
    )
    assert (led["created"], led["modified"], led["deleted"]) == (2, 3, 1)
    assert [t["tx_index"] for t in txs] == [0, 1]
    assert (txs[1]["blob_bytes"], txs[1]["meta_bytes"], txs[1]["created"], txs[1]["deleted"]) == (
        200,
        500,
        2,
        1,
    )
    assert [(n["tx_index"], n["action"], n["entry_type"]) for n in nodes] == [
        (0, "modified", "AccountRoot"),
        (0, "modified", "AccountRoot"),
        (1, "created", "AccountRoot"),
        (1, "modified", "AccountRoot"),
        (1, "deleted", "Offer"),
        (1, "created", "DirectoryNode"),
    ]


def test_empty_ledger():
    led, txs, nodes = cg.growth_rows(*pair(101, []))
    assert (led["txn_count"], led["tx_blob_bytes"], led["meta_bytes"], led["header_bytes"]) == (0, 0, 0, 118)
    assert txs == [] and nodes == []


def test_disagreeing_answers_are_rejected():
    j, b = pair(100, SPEC)
    j["ledger_hash"] = "0" * 64
    with pytest.raises(ValueError, match="header hash"):
        cg.growth_rows(j, b)
    j, b = pair(100, SPEC)
    b["transactions"].pop()
    with pytest.raises(ValueError, match="JSON txs vs 1 binary"):
        cg.growth_rows(j, b)
    j, b = pair(100, SPEC)
    j["transactions"][0]["hash"] = "F" * 64
    with pytest.raises(ValueError, match="no binary twin"):
        cg.growth_rows(j, b)
    j, b = pair(100, SPEC)
    b["transactions"][1] = dict(b["transactions"][0])
    with pytest.raises(ValueError, match="duplicate binary tx"):
        cg.growth_rows(j, b)
    j, b = pair(100, SPEC)
    del j["transactions"][0]["metaData"]["TransactionIndex"]
    with pytest.raises(ValueError, match="no TransactionIndex"):
        cg.growth_rows(j, b)


class OneAnswer:
    """Answers every `ledger` call with the given (json, binary) pair, whatever was asked."""

    def __init__(self, j, b):
        self.j, self.b = j, b

    def call(self, method, params=None):
        return {"ledger": self.b if params.get("binary") else self.j}


def test_fetch_pair_rejects_wrong_or_open_ledger():
    j, b = pair(100, SPEC)
    assert cg.fetch_pair(OneAnswer(j, b), 100)[0] is j
    with pytest.raises(LookupError, match="unexpected ledger 100"):
        cg.fetch_pair(OneAnswer(j, b), 101)
    j = dict(j, closed=False)
    with pytest.raises(LookupError, match="unexpected ledger"):
        cg.fetch_pair(OneAnswer(j, b), 100)


def specs_for(i):
    return SPEC[: i % 3]


class FakeRpc:
    """Ledgers 100..last in both forms; `missing` -> lgrNotFound; `fail` errors `fail_times` times."""

    def __init__(self, last, missing=(), fail=None, fail_times=0, kill_after=None):
        self.last, self.missing, self.fail, self.fail_times = last, set(missing), fail, fail_times
        self.kill_after = kill_after
        self.count = 0
        self.calls = []

    def __call__(self, *_a, **_k):
        return self

    def call(self, method, params=None):
        self.count += 1
        self.calls.append(method)
        if method == "server_info":
            return {
                "info": {
                    "validated_ledger": {"seq": self.last},
                    "build_version": "x",
                    "server_state": "full",
                    "complete_ledgers": f"100-{self.last}",
                }
            }
        i = params["ledger_index"]
        if self.kill_after is not None and i > self.kill_after:
            raise KeyboardInterrupt
        if i in self.missing:
            raise LookupError("lgrNotFound")
        if i == self.fail and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        j, b = pair(i, specs_for(i))
        return {"ledger": b if params.get("binary") else j}


class FakePublic(FakeRpc):
    def call(self, method, params=None):
        if method != "ledger":
            raise ValueError("public: ledger only")
        return super().call(method, params)


def run(monkeypatch, tmp_path, local, *args, public=None):
    monkeypatch.setattr(cg, "Rpc", local)
    monkeypatch.setattr(cg, "PublicRpc", public or FakePublic(10**9, missing=range(0, 10**4)))
    monkeypatch.setattr(cg.time, "sleep", lambda s: None)
    monkeypatch.setattr(cg, "_stop", False)
    return cg.main(["--out", str(tmp_path), "--no-sudo", *args])


def read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_full_run_counts(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path, FakeRpc(110, fail=104, fail_times=2), "--start", "100", "--stop-at", "110")
    led = read(tmp_path / "ledgers.csv")
    assert [int(r["ledger_index"]) for r in led] == list(range(100, 111))
    assert {r["source"] for r in led} == {"local"}
    assert len(read(tmp_path / "tx.csv")) == sum(i % 3 for i in range(100, 111))
    assert sum(int(r["created"]) + int(r["modified"]) + int(r["deleted"]) for r in led) == len(
        read(tmp_path / "nodes.csv")
    )
    assert [(r["source"], r["attempt"]) for r in read(tmp_path / "retries.csv")] == [
        ("local", "1"),
        ("local", "2"),
    ]
    assert not (tmp_path / "gaps.csv").exists()


def test_public_fallback_then_gap(monkeypatch, tmp_path):
    pub = FakePublic(10**9, missing={105})
    run(
        monkeypatch,
        tmp_path,
        FakeRpc(106, missing={103, 105}),
        "--start",
        "100",
        "--stop-at",
        "106",
        "--retries",
        "2",
        public=pub,
    )
    led = {int(r["ledger_index"]): r["source"] for r in read(tmp_path / "ledgers.csv")}
    assert led[103] == "public" and led[104] == "local" and 105 not in led
    assert [r["ledger_index"] for r in read(tmp_path / "gaps.csv")] == ["105"]
    rt = [(r["ledger_index"], r["source"], r["attempt"]) for r in read(tmp_path / "retries.csv")]
    assert rt == [
        ("103", "local", "1"),
        ("103", "local", "2"),
        ("105", "local", "1"),
        ("105", "local", "2"),
        ("105", "public", "1"),
        ("105", "public", "2"),
    ]
    assert "server_info" not in pub.calls


def test_backoff_is_exponential(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(cg, "Rpc", FakeRpc(100, missing={100}))
    monkeypatch.setattr(cg, "PublicRpc", lambda url: FakePublic(10**9, missing={100}))
    monkeypatch.setattr(cg.time, "sleep", slept.append)
    monkeypatch.setattr(cg, "_stop", False)
    cg.main(
        [
            "--out",
            str(tmp_path),
            "--no-sudo",
            "--start",
            "100",
            "--stop-at",
            "100",
            "--retries",
            "3",
            "--backoff",
            "1.5",
        ]
    )
    assert slept == [2.0, 2.0, 2.0, 1.5, 3.0, 6.0]


def test_fallback_disabled(monkeypatch, tmp_path):
    run(
        monkeypatch,
        tmp_path,
        FakeRpc(101, missing={100}),
        "--start",
        "100",
        "--stop-at",
        "101",
        "--retries",
        "1",
        "--public-rpc",
        "",
    )
    assert [r["ledger_index"] for r in read(tmp_path / "gaps.csv")] == ["100"]
    assert {r["source"] for r in read(tmp_path / "retries.csv")} == {"local"}


def test_resume_after_kill_with_orphans_and_torn_line(monkeypatch, tmp_path):
    with pytest.raises(KeyboardInterrupt):
        run(monkeypatch, tmp_path, FakeRpc(115, kill_after=107), "--start", "100", "--stop-at", "115")
    # a kill after ledger 108's tx + nodes rows but before its ledgers row, plus a torn nodes line
    _, txs, nodes = cg.growth_rows(*pair(108, SPEC))
    cg.append_rows(tmp_path / "tx.csv", cg.TX_FIELDS, txs)
    cg.append_rows(tmp_path / "nodes.csv", cg.NODE_FIELDS, nodes)
    with open(tmp_path / "nodes.csv", "a") as fh:
        fh.write("108,1,crea")
    run(monkeypatch, tmp_path, FakeRpc(115), "--start", "999", "--stop-at", "999")
    led = [int(r["ledger_index"]) for r in read(tmp_path / "ledgers.csv")]
    assert led == list(range(100, 116))
    txr = read(tmp_path / "tx.csv")
    assert len(txr) == sum(i % 3 for i in range(100, 116))
    assert len({(r["ledger_index"], r["tx_index"]) for r in txr}) == len(txr)
    nodes_total = sum(
        int(r["created"]) + int(r["modified"]) + int(r["deleted"]) for r in read(tmp_path / "ledgers.csv")
    )
    assert len(read(tmp_path / "nodes.csv")) == nodes_total
    rs = read(tmp_path / "restarts.csv")
    assert [r["kind"] for r in rs] == ["start", "resume"]
    assert rs[1]["first_ledger"] == "108" and rs[1]["orphan_rows"] == str(len(txs) + len(nodes))
    assert int(rs[1]["torn_bytes"]) == len("108,1,crea")
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["start_seq"] == 100 and st["stop_at"] == 115


def test_window_end_enforced_client_side(monkeypatch, tmp_path):
    monkeypatch.setattr(cg.time, "time", lambda: BASE_CT + cg.RIPPLE_EPOCH - 3600 + 20)
    run(monkeypatch, tmp_path, FakeRpc(120), "--start", "100", "--hours", "1", "--stop-at", "120")
    assert [int(r["ledger_index"]) for r in read(tmp_path / "ledgers.csv")] == list(range(100, 106))


def test_rate_cap(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run(monkeypatch, tmp_path, FakeRpc(101), "--rate", "2.5")
