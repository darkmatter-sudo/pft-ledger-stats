import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hashverify"))
import lib_hashverify as lib  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "hashverify")


def vectors():
    with open(os.path.join(FIX, "txtree_vectors.jsonl")) as fh:
        return [json.loads(line) for line in fh]


def by_index(i):
    return next(v for v in vectors() if v["ledger_index"] == i)


def test_real_headers_serialize_to_the_binary_ledger_data():
    for v in vectors():
        hdr = lib.serialize_header(v["json"])
        assert len(hdr) == lib.HEADER_BYTES
        assert hdr.hex().upper() == v["binary"]["ledger_data"].upper()


def test_real_ledger_hashes_recompute():
    for v in vectors():
        assert lib.ledger_hash(v["json"]) == v["json"]["ledger_hash"].upper()
        assert lib.ledger_hash(v["json"]) == v["binary"]["ledger_hash"].upper()


def test_real_tx_ids_and_roots_recompute():
    counts = []
    for v in vectors():
        root, ids = lib.tx_tree(v["binary"]["transactions"])
        assert root == v["json"]["transaction_hash"].upper()
        assert sorted(ids) == sorted(t.upper() for t in v["json"]["transactions"])
        counts.append(len(ids))
    assert 0 in counts and 1 in counts and max(counts) == 5


def test_real_vector_7806332_has_a_depth_two_inner_node():
    """Two of its tx IDs share the first nibble (4A.., 4D..), so the root alone is not enough."""
    _, ids = lib.tx_tree(by_index(7806332)["binary"]["transactions"])
    firsts = [i[0] for i in ids]
    assert any(firsts.count(c) > 1 for c in firsts)


def test_empty_ledger_root_is_zero():
    assert lib.tx_tree([]) == ("0" * 64, [])
    assert by_index(7806708)["json"]["transaction_hash"] == "0" * 64


def test_single_leaf_sits_below_an_inner_root():
    key = bytes([0xA5]) + bytes(31)
    leaf = bytes(range(32))
    kids = [lib.ZERO] * 16
    kids[0xA] = leaf
    assert lib.shamap_root({key: leaf}) == lib.sha512half(b"MIN\x00", *kids)


def test_shared_prefix_builds_nested_inner_nodes():
    k1, k2 = bytes([0x12, 0x30]) + bytes(30), bytes([0x13, 0x40]) + bytes(30)
    k3 = bytes([0x12, 0x50]) + bytes(30)
    l1, l2, l3 = (bytes([n]) * 32 for n in (1, 2, 3))
    d1 = [lib.ZERO] * 16
    d1[3], d1[5] = l1, l3  # third nibble of k1 = 3, of k3 = 5 (both under 1 -> 2)
    n12 = lib.sha512half(b"MIN\x00", *d1)
    d0_1 = [lib.ZERO] * 16
    d0_1[2], d0_1[3] = n12, l2
    n1 = lib.sha512half(b"MIN\x00", *d0_1)
    root = [lib.ZERO] * 16
    root[1] = n1
    assert lib.shamap_root({k1: l1, k2: l2, k3: l3}) == lib.sha512half(b"MIN\x00", *root)


def test_duplicate_key_rejected():
    t = by_index(7806707)["binary"]["transactions"][0]
    with pytest.raises(ValueError, match="duplicate tx ID"):
        lib.tx_tree([t, t])


def test_nibbles_high_then_low():
    key = bytes([0xAB, 0xCD]) + bytes(30)
    assert [lib.nibble(key, d) for d in range(4)] == [0xA, 0xB, 0xC, 0xD]


@pytest.mark.parametrize(
    "n,want",
    [
        (0, "00"),
        (1, "01"),
        (192, "C0"),
        (193, "C100"),
        (194, "C101"),
        (12480, "F0FF"),
        (12481, "F10000"),
        (12482, "F10001"),
        (918744, "FED417"),
    ],
)
def test_vl_prefix_boundaries(n, want):
    assert lib.vl_prefix(n).hex().upper() == want


def test_vl_prefix_rejects_out_of_range():
    with pytest.raises(ValueError):
        lib.vl_prefix(918745)
    with pytest.raises(ValueError):
        lib.vl_prefix(-1)


def test_tx_leaf_layout():
    blob, meta, key = b"\x01" * 200, b"\x02" * 3, bytes([7]) * 32
    want = lib.sha512half(b"SND\x00", b"\xc1\x07", blob, b"\x03", meta, key)
    assert lib.tx_leaf_hash(blob, meta, key) == want


def test_prefixes_are_the_rippled_values():
    assert lib.PREFIX_LEDGER == bytes.fromhex("4C575200")
    assert lib.PREFIX_TXID == bytes.fromhex("54584E00")
    assert lib.PREFIX_TXNODE == bytes.fromhex("534E4400")
    assert lib.PREFIX_INNER == bytes.fromhex("4D494E00")


def test_header_rejects_short_hash():
    led = dict(by_index(7806707)["json"])
    led["account_hash"] = led["account_hash"][:-2]
    with pytest.raises(ValueError, match="account_hash"):
        lib.serialize_header(led)


def test_every_header_field_is_hashed():
    base = by_index(7806707)["json"]
    h0 = lib.ledger_hash(base)
    for f in (
        "ledger_index",
        "total_coins",
        "parent_close_time",
        "close_time",
        "close_time_resolution",
        "close_flags",
    ):
        assert lib.ledger_hash({**base, f: int(base[f]) + 1}) != h0, f
    for f in ("parent_hash", "transaction_hash", "account_hash"):
        v = base[f]
        assert lib.ledger_hash({**base, f: v[:-1] + ("0" if v[-1] != "0" else "1")}) != h0, f


def test_ranges_and_overlap():
    assert lib.parse_ranges("5-9,12,20-30") == [(5, 9), (12, 12), (20, 30)]
    assert lib.parse_ranges("empty") == []
    assert lib.overlap([(1, 100)], [(50, 200)]) == (50, 100)
    assert lib.overlap([(1, 10), (20, 90)], [(5, 95)]) == (20, 90)
    assert lib.overlap([(1, 10)], [(11, 20)]) is None
