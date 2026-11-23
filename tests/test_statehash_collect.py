"""statehash/collect_statehash.py vs a fake binary node: snapshot pin/paging/resume, capture chain checks."""

import json
import os
import struct
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statehash"))

import collect_statehash as S  # noqa: E402

cf, hv = S.cf, S.hv
START = 1000


def quiet(_m):
    pass


def header(n, parent, tx_root=bytes(32), acct=b"\x11" * 32):
    return (
        struct.pack(">IQ", n, 99) + parent + tx_root + acct + struct.pack(">IIBB", 10 * n, 10 * n + 3, 10, 0)
    )


class FakeNode:
    """Ledgers START.. with one transaction in every odd ledger; a 5-object state tree."""

    def __init__(self, top=START + 5):
        self.top, self.blobs, self.txs, self.calls = top, {}, {}, []
        parent = b"\x00" * 32
        for n in range(START - 1, top + 1):
            txs = [{"tx_blob": f"12000022{n:08X}", "meta": f"201C{n:08X}E1"}] if n % 2 else []
            root = bytes.fromhex(hv.tx_tree(txs)[0])
            blob = header(n, parent, root)
            self.blobs[n], self.txs[n] = blob, txs
            parent = hv.sha512half(hv.PREFIX_LEDGER, blob)
        self.state = [{"index": f"{i:064X}", "data": f"1100{i:04X}"} for i in (5, 1, 4, 2, 3)]

    def lhash(self, n):
        return hv.sha512half(hv.PREFIX_LEDGER, self.blobs[n]).hex().upper()

    def json_header(self, n):
        h = S.parse_header(self.blobs[n])
        b = self.blobs[n]
        return {
            **{k: v for k, v in h.items() if k != "total_coins"},
            "total_coins": str(h["total_coins"]),
            "ledger_hash": self.lhash(n),
            "parent_close_time": struct.unpack(">I", b[108:112])[0],
            "close_time": struct.unpack(">I", b[112:116])[0],
            "close_time_resolution": b[116],
            "close_flags": b[117],
        }

    def send(self, body):
        req = json.loads(body)
        m, p = req["method"], req["params"][0]
        self.calls.append(m)
        if m == "server_state":
            res = {"state": {"server_state": "full", "validated_ledger": {"seq": self.top}}}
        elif m == "ledger":
            n = int(p["ledger_index"])
            if n > self.top:
                res = {"status": "error", "error": "lgrNotFound"}
            elif p.get("binary"):
                led = {"closed": True, "ledger_data": self.blobs[n].hex().upper()}
                if p.get("transactions"):
                    led["transactions"] = [dict(t) for t in self.txs[n]]
                res = {"validated": True, "ledger": led, "ledger_hash": self.lhash(n), "ledger_index": n}
            else:
                res = {"validated": True, "ledger": self.json_header(n)}
        elif m == "ledger_data":
            st = sorted(self.state, key=lambda o: o["index"])
            i = int(p.get("marker", 0))
            lim = p["limit"]
            res = {"ledger_hash": p["ledger_hash"], "state": st[i : i + lim]}
            if i + lim < len(st):
                res["marker"] = str(i + lim)
        else:
            raise AssertionError(m)
        return json.dumps({"result": res})


def rpc_for(node):
    return cf.Rpc(node.send, quiet, rate=2, sleep=lambda s: None)


def test_parse_header_round_trips_serialize_header():
    node = FakeNode()
    assert hv.serialize_header(node.json_header(START)) == node.blobs[START]
    with pytest.raises(ValueError, match="117 bytes"):
        S.parse_header(node.blobs[START][:-1])


def test_snapshot_pages_sorts_and_records_public_header(tmp_path):
    node, pub = FakeNode(), FakeNode()
    man = S.snapshot(rpc_for(node), str(tmp_path / "A"), START, log=quiet, limit=2, public=rpc_for(pub))
    assert man["objects"] == 5 and man["ledger_hash"] == node.lhash(START)
    rows = [json.loads(x) for x in (tmp_path / "A" / "state.jsonl").read_text().splitlines()]
    assert [r["index"] for r in rows] == sorted(r["index"] for r in rows)
    assert len(os.listdir(tmp_path / "A" / "pages")) == 3
    p = json.loads((tmp_path / "A" / "public-header.json").read_text())
    assert p["account_hash"] == man["account_hash"] and p["validated"] is True


def test_snapshot_resumes_and_refuses_other_ledger(tmp_path):
    node = FakeNode()
    S.snapshot(rpc_for(node), str(tmp_path / "A"), START, log=quiet, limit=2)
    with pytest.raises(SystemExit, match="already holds"):
        S.snapshot(rpc_for(node), str(tmp_path / "A"), START, log=quiet)
    os.remove(tmp_path / "A" / "MANIFEST.json")
    with pytest.raises(SystemExit, match="pinned to"):
        S.snapshot(rpc_for(node), str(tmp_path / "A"), START + 1, log=quiet)
    node.calls.clear()
    S.snapshot(rpc_for(node), str(tmp_path / "A"), None, log=quiet, limit=2)
    assert "ledger_data" not in node.calls  # every page came from disk


def test_snapshot_refuses_duplicate_index(tmp_path):
    node = FakeNode()
    node.state.append(dict(node.state[0]))
    with pytest.raises(SystemExit, match="duplicate index"):
        S.snapshot(rpc_for(node), str(tmp_path / "A"), START, log=quiet, limit=2)


def test_capture_records_every_ledger_and_resumes_after_torn_tail(tmp_path):
    node = FakeNode()
    out = str(tmp_path / "cap")
    assert S.capture(rpc_for(node), out, START, to=START + 3, log=quiet) == START + 3
    path = tmp_path / "cap" / "ledgers.jsonl"
    with open(path, "a") as fh:
        fh.write('{"ledger_index": 10')  # torn tail
    assert S.capture(rpc_for(node), out, START, to=START + 5, log=quiet) == START + 5
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["ledger_index"] for r in rows] == list(range(START, START + 6))
    assert sum(len(r["transactions"]) for r in rows) == 3
    assert all(S.parse_header(bytes.fromhex(r["header"]))["ledger_index"] == r["ledger_index"] for r in rows)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("drop_tx", "rebuilt tx root"),
        ("extra_tx", "rebuilt tx root"),
        ("flip_meta", "rebuilt tx root"),
        ("wrong_hash", "header hashes to"),
        ("wrong_index", "header says"),
        ("no_meta", "without binary blob"),
    ],
    ids=["drop-tx", "extra-tx", "flip-meta", "wrong-hash", "wrong-index", "no-meta"],
)
def test_check_ledger_refuses_with_named_reason(fault, reason):
    node = FakeNode()
    n = START + 1
    r = json.loads(
        node.send(
            json.dumps(
                {"method": "ledger", "params": [{"ledger_index": n, "binary": True, "transactions": True}]}
            )
        )
    )["result"]
    txs = r["ledger"]["transactions"]
    if fault == "drop_tx":
        txs.clear()
    elif fault == "extra_tx":
        txs.append({"tx_blob": "1200", "meta": "E1"})
    elif fault == "flip_meta":
        txs[0]["meta"] = txs[0]["meta"][:-2] + "E2"
    elif fault == "wrong_hash":
        r["ledger_hash"] = "00" * 32
    elif fault == "wrong_index":
        r["ledger"]["ledger_data"] = node.blobs[n + 2].hex()
        r["ledger_hash"] = node.lhash(n + 2)
    elif fault == "no_meta":
        del txs[0]["meta"]
    with pytest.raises(cf.ChainBreak, match=reason):
        S.check_ledger(n, r, node.lhash(n - 1))


def test_check_ledger_refuses_parent_break():
    node = FakeNode()
    r = json.loads(
        node.send(
            json.dumps(
                {
                    "method": "ledger",
                    "params": [{"ledger_index": START + 2, "binary": True, "transactions": True}],
                }
            )
        )
    )["result"]
    with pytest.raises(cf.ChainBreak, match="parent_hash"):
        S.check_ledger(START + 2, r, node.lhash(START))
    assert S.check_ledger(START + 2, r, node.lhash(START + 1))["ledger_index"] == START + 2


def test_capture_stops_on_pruned_validated_ledger(tmp_path):
    node = FakeNode()
    del node.blobs[START + 2]
    orig = node.send

    def send(body):
        p = json.loads(body)["params"][0]
        if json.loads(body)["method"] == "ledger" and p.get("ledger_index") == START + 2:
            return json.dumps({"result": {"status": "error", "error": "lgrNotFound"}})
        return orig(body)

    rpc = cf.Rpc(send, quiet, rate=2, sleep=lambda s: None)
    with pytest.raises(cf.ChainBreak, match="pruned"):
        S.capture(rpc, str(tmp_path / "cap"), START, to=START + 4, log=quiet)
    rows = (tmp_path / "cap" / "ledgers.jsonl").read_text().splitlines()
    assert len(rows) == 2
