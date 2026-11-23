"""statehash/report_statehash.py: hand-computed synthetic tree structure, root gate, deterministic files."""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statehash"))

import lib_statehash as L  # noqa: E402
import report_statehash as P  # noqa: E402

DEFS = L.Defs()


def blob(etype, pad):
    """LedgerEntryType + Flags (8 bytes) + optional Blob field, len(pad) bytes in all (header+VL+data)."""
    extra = L.field_id_bytes(7, 3) + bytes([len(pad) - 2]) + bytes(len(pad) - 2) if pad else b""
    return (
        L.field_id_bytes(1, 1)
        + DEFS.entry_types[etype].to_bytes(2, "big")
        + L.field_id_bytes(2, 2)
        + bytes(4)
        + extra
    )


def key(prefix):
    return bytes.fromhex(prefix + "0" * (64 - len(prefix)))


# root: slot 3 -> inner(two AccountRoots in slots 1 and 7), slot 9 -> Offer leaf
ITEMS = [
    (key("31"), blob("AccountRoot", b"")),
    (key("37"), blob("AccountRoot", b"\x00" * 10)),
    (key("90"), blob("Offer", b"\x00" * 3)),
]


def header(items, **kw):
    root = L.root_of({k: L.leaf_hash(k, d) for k, d in items}).hex().upper()
    return {"ledger_index": 7, "ledger_hash": "AB" * 32, "account_hash": root, **kw}


def test_hand_computed_structure():
    s = P.measure(DEFS, header(ITEMS), ITEMS)
    assert s["inner_nodes"] == 2
    assert s["fanout"]["2"] == 2 and sum(s["fanout"].values()) == 2
    assert s["depth"] == {"min": 1, "p50": 2, "p99": 2, "max": 2}
    assert s["types"]["AccountRoot"] == {
        "count": 2,
        "bytes": 8 + 18,
        "max_bytes": 18,
        "max_depth": 2,
        "mean_bytes": 13.0,
    }
    assert s["types"]["Offer"]["count"] == 1 and s["types"]["Offer"]["max_depth"] == 1
    # proofs: both accounts sit below 2 inner nodes -> 2 * 512 + blob + 32-byte key
    assert s["proof"] == {
        "accounts": 2,
        "inner_nodes": {"p50": 2, "p99": 2, "max": 2},
        "bytes": {"p50": 1024 + 8 + 32, "p99": 1024 + 18 + 32, "max": 1024 + 18 + 32},
    }
    assert s["leaves"] == 3 and s["leaf_bytes"] == 8 + 18 + 11


def test_nearest_rank_percentiles():
    v = list(range(1, 101))
    assert (P.pct(v, 50), P.pct(v, 99), P.pct(v, 100)) == (50, 99, 100)
    assert P.pct([5], 99) == 5 and P.pct([], 50) is None


def test_wrong_header_root_refuses_report():
    with pytest.raises(SystemExit, match="FAIL ROOT"):
        P.measure(DEFS, header(ITEMS, account_hash="00" * 32), ITEMS)


def write_snap(d, items):
    os.makedirs(d)
    with open(os.path.join(d, "header.json"), "w") as fh:
        json.dump(header(items), fh)
    with open(os.path.join(d, "state.jsonl"), "w") as fh:
        for k, v in items:
            fh.write(json.dumps({"index": k.hex().upper(), "data": v.hex().upper()}) + "\n")


def test_offline_render_is_byte_identical(tmp_path):
    write_snap(str(tmp_path / "data" / "A"), ITEMS)
    write_snap(str(tmp_path / "data" / "B"), ITEMS[:2])
    for out in ("r1", "r2"):
        assert P.main(["--offline", str(tmp_path / "data"), "--out", str(tmp_path / out)]) == 0
    names = sorted(os.listdir(tmp_path / "r1"))
    assert names == ["structure-fanout.csv", "structure-types.csv", "structure.json", "structure.txt"]
    for n in names:
        assert (tmp_path / "r1" / n).read_bytes() == (tmp_path / "r2" / n).read_bytes()
    rep = json.loads((tmp_path / "r1" / "structure.json").read_text())
    assert sorted(rep["snapshots"]) == ["A", "B"] and rep["snapshots"]["B"]["leaves"] == 2
