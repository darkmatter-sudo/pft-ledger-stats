"""statehash/lib_statehash.py: hand-computed SHAMap roots, incremental == from-scratch, codec round trips."""

import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statehash"))

import lib_statehash as L  # noqa: E402

Z = bytes(32)


def H(*parts):
    import hashlib

    return hashlib.sha512(b"".join(parts)).digest()[:32]


def inner(slots):
    """Hand-written inner node: MIN prefix + 16 child hashes, zeros where empty."""
    return H(b"MIN\x00", *(slots.get(i, Z) for i in range(16)))


def leaf(key, data):
    return H(b"MLN\x00", data, key)


def key(hexprefix, fill="0"):
    return bytes.fromhex(hexprefix + fill * (64 - len(hexprefix)))


def both(items):
    """Root from scratch and from the incremental map (inserted in shuffled order); must agree."""
    a = L.root_of({k: L.leaf_hash(k, d) for k, d in items})
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)
    b = L.SHAMap(shuffled).root_hash()
    assert a == b
    return a


def test_empty_map_is_zero():
    assert both([]) == Z
    assert L.SHAMap().root_hash() == Z


def test_one_leaf_sits_in_root_slot_of_its_first_nibble():
    k = key("A1")
    assert both([(k, b"\x11\x00\x61")]) == inner({0xA: leaf(k, b"\x11\x00\x61")})


def test_two_leaves_sharing_one_nibble_get_one_inner_below_root():
    a, b = key("31"), key("37")
    want = inner({3: inner({1: leaf(a, b"a"), 7: leaf(b, b"b")})})
    assert both([(a, b"a"), (b, b"b")]) == want


def test_two_leaves_sharing_63_nibbles_get_a_63_deep_chain():
    a = key("5" * 63 + "2")
    b = key("5" * 63 + "C")
    h = inner({2: leaf(a, b"a"), 0xC: leaf(b, b"b")})  # inner at depth 63
    for _depth in range(62, -1, -1):  # depths 62..0 each hold only the chain child in slot 5
        h = inner({5: h})
    assert both([(a, b"a"), (b, b"b")]) == h


def test_delete_collapses_lone_leaf_up_to_root():
    a, b, c = key("31"), key("37"), key("90")
    m = L.SHAMap([(a, b"a"), (b, b"b"), (c, b"c")])
    assert m.root_hash() == inner({3: inner({1: leaf(a, b"a"), 7: leaf(b, b"b")}), 9: leaf(c, b"c")})
    m.delete(b)
    assert m.root_hash() == inner({3: leaf(a, b"a"), 9: leaf(c, b"c")})
    m.delete(a)
    m.delete(c)
    assert m.root_hash() == Z and m.size == 0


def test_delete_collapses_whole_63_deep_chain():
    a, b = key("5" * 63 + "2"), key("5" * 63 + "C")
    m = L.SHAMap([(a, b"a"), (b, b"b")])
    m.delete(a)
    assert m.root_hash() == inner({5: leaf(b, b"b")})
    assert [n[0] for n in m.walk()] == ["inner", "leaf"]


def test_delete_keeps_inner_with_two_leaves_below():
    a, b, c = key("311"), key("317"), key("37")
    m = L.SHAMap([(a, b"a"), (b, b"b"), (c, b"c")])
    m.delete(c)
    assert m.root_hash() == inner({3: inner({1: inner({1: leaf(a, b"a"), 7: leaf(b, b"b")})})})


def test_delete_missing_key_raises():
    m = L.SHAMap([(key("31"), b"a")])
    with pytest.raises(KeyError):
        m.delete(key("32"))


def test_replace_changes_only_that_leaf():
    a, c = key("31"), key("90")
    m = L.SHAMap([(a, b"a"), (c, b"c")])
    m.set(a, b"A")
    assert m.root_hash() == inner({3: leaf(a, b"A"), 9: leaf(c, b"c")})
    assert m.get(a) == b"A" and m.size == 2


def test_random_inserts_and_deletes_agree_with_scratch_build():
    rng = random.Random(42)
    live, m = {}, L.SHAMap()
    for step in range(600):
        if live and rng.random() < 0.4:
            k = rng.choice(sorted(live))
            m.delete(k)
            del live[k]
        else:
            k = bytes([rng.choice([0x10, 0x11, 0xF0])]) + bytes(rng.randrange(4) for _ in range(31))
            live[k] = bytes([step % 256])
            m.set(k, live[k])
        assert m.root_hash() == L.root_of({k: L.leaf_hash(k, d) for k, d in live.items()}), step


def test_leaf_hash_prefix_and_order():
    k = key("AB")
    assert L.leaf_hash(k, b"xy") == H(b"MLN\x00", b"xy", k) != H(b"MLN\x00", k, b"xy")


# ---------------------------------------------------------------- codec


def test_field_id_round_trip_all_widths():
    for t, n in [(1, 1), (2, 28), (14, 1), (16, 3), (19, 1), (2, 2), (24, 20), (25, 1)]:
        raw = L.field_id_bytes(t, n)
        assert L.read_field_id(raw, 0) == (t, n, len(raw))
    assert L.field_id_bytes(16, 3) == bytes([0x03, 16])
    assert L.field_id_bytes(2, 28) == bytes([0x20, 28])


def test_parse_serialize_round_trip_and_canonical_order():
    flags = L.field_id_bytes(2, 2) + b"\x00\x00\x00\x01"
    etype = L.field_id_bytes(1, 1) + b"\x00\x61"
    acct = L.field_id_bytes(8, 1) + b"\x14" + b"\x22" * 20
    blob = etype + flags + acct
    f = L.parse(blob)
    assert f == {(1, 1): b"\x00\x61", (2, 2): b"\x00\x00\x00\x01", (8, 1): b"\x14" + b"\x22" * 20}
    assert L.serialize(f) == blob
    with pytest.raises(L.CodecError, match="canonical order"):
        L.parse(flags + etype)
    with pytest.raises(L.CodecError, match="duplicate"):
        L.parse(etype + etype)
    with pytest.raises(L.CodecError, match="past the end"):
        L.parse(blob[:-1])


def test_amount_widths():
    native = L.field_id_bytes(6, 1) + bytes([0x40]) + bytes(7)
    issued = L.field_id_bytes(6, 2) + bytes([0xD4]) + bytes(47)
    assert L.parse(native + issued) == {(6, 1): native[1:], (6, 2): issued[1:]}


def test_array_and_object_round_trip():
    elems = [(19, {(2, 26): b"\x00\x00\x01\x00", (7, 1): b"\x21" + b"\x02" * 33})]
    raw = L.array_raw(elems)
    assert L.array_elements(raw) == elems
    blob = L.field_id_bytes(15, 17) + raw
    assert L.serialize(L.parse(blob)) == blob
