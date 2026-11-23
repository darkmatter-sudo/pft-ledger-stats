"""State SHAMap rebuild + binary ledger-object field codec for statehash/ (Python 3 stdlib only).

Hashing (postfiatd 1.0.9 = rippled SHAMap; HashPrefix values from include/xrpl/protocol/HashPrefix.h):

  state leaf   SHA-512Half("MLN\\0" || object_blob || index)        (index = 32-byte object key)
  inner node   SHA-512Half("MIN\\0" || child_0 || ... || child_15)  (empty child = 32 zero bytes)
  placement    a leaf hangs in the inner node at the depth where its key's 4-bit nibble path stops
               being shared with any other key (nibble d = high half of key byte d//2 for even d,
               low half for odd d); the root is always an inner node; an empty map hashes to 32 zeros.
  delete       removing a leaf collapses every inner node (except the root) left holding exactly one
               leaf and no inner child: that leaf moves up into the parent's slot.

`root_of(leaves)` builds the root from scratch (one pass over sorted keys). `SHAMap` keeps the tree and
re-hashes only the dirty path after set/delete; tests require both to agree on every case.

Codec: a ledger object (SLE) blob is a sequence of fields, each a field header (type code and field
code `nth`, 1-3 bytes) and a value whose length follows from its type. Field codes come from the
node's own `server_definitions` answer committed as definitions.json (never typed by hand).
`parse(blob)` returns {(type, nth): raw value bytes} for the top level; nested STObject/STArray values
stay raw, which is all the replay needs (metadata carries whole top-level values).
`serialize(fields)` writes fields in canonical (type, nth) order; parse(serialize(x)) == x and
serialize(parse(blob)) == blob for every canonical blob (tested on every object of a real snapshot).
"""

import hashlib
import json
import os

PREFIX_LEAF = b"MLN\x00"
PREFIX_INNER = b"MIN\x00"
ZERO = bytes(32)
HERE = os.path.dirname(os.path.abspath(__file__))
DEFINITIONS = os.path.join(HERE, "definitions.json")

ST_OBJECT, ST_ARRAY = 14, 15
OBJECT_END, ARRAY_END = (14, 1), (15, 1)
FIXED = {
    1: 2,
    2: 4,
    3: 8,
    4: 16,
    5: 32,
    10: 4,
    11: 8,
    16: 1,
    17: 20,
    20: 12,
    21: 24,
    22: 48,
    23: 64,
    26: 20,
    9: 12,
}
VL_TYPES = {7, 8, 19}


class CodecError(ValueError):
    pass


def sha512half(*parts):
    return hashlib.sha512(b"".join(parts)).digest()[:32]


def leaf_hash(index, data):
    return sha512half(PREFIX_LEAF, data, index)


def nibble(key, depth):
    b = key[depth // 2]
    return (b >> 4) if depth % 2 == 0 else (b & 0x0F)


# ---------------------------------------------------------------- SHAMap, from scratch


def root_of(leaves):
    """Root hash of a state map {index(32 bytes): leaf hash(32 bytes)}; 32 zeros when empty."""
    if not leaves:
        return ZERO
    keys = sorted(leaves)

    def inner(lo, hi, depth):
        # keys[lo:hi] share their first `depth` nibbles; split them by nibble `depth`
        kids, i = [], lo
        for slot in range(16):
            j = i
            while j < hi and nibble(keys[j], depth) == slot:
                j += 1
            if j == i:
                kids.append(ZERO)
            elif j - i == 1:
                kids.append(leaves[keys[i]])
            else:
                kids.append(inner(i, j, depth + 1))
            i = j
        return sha512half(PREFIX_INNER, *kids)

    return inner(0, len(keys), 0)


# ---------------------------------------------------------------- SHAMap, incremental


class Leaf:
    __slots__ = ("key", "data", "hash")

    def __init__(self, key, data):
        self.key, self.data, self.hash = key, data, leaf_hash(key, data)


class Inner:
    __slots__ = ("kids", "hash")

    def __init__(self):
        self.kids, self.hash = [None] * 16, None

    def count(self):
        return sum(k is not None for k in self.kids)


class SHAMap:
    def __init__(self, items=()):
        self.root, self.size = Inner(), 0
        for key, data in items:
            self.set(key, data)

    def get(self, key):
        node, depth = self.root, 0
        while isinstance(node, Inner):
            node = node.kids[nibble(key, depth)]
            depth += 1
        return node.data if node is not None and node.key == key else None

    def set(self, key, data):
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        node, depth = self.root, 0
        while True:
            node.hash = None
            slot = nibble(key, depth)
            child = node.kids[slot]
            if child is None:
                node.kids[slot] = Leaf(key, data)
                self.size += 1
                return
            if isinstance(child, Inner):
                node, depth = child, depth + 1
                continue
            if child.key == key:
                node.kids[slot] = Leaf(key, data)
                return
            # split: push the resident leaf one level down and keep descending
            split = Inner()
            split.kids[nibble(child.key, depth + 1)] = child
            node.kids[slot] = split
            node, depth = split, depth + 1

    def delete(self, key):
        path, node, depth = [], self.root, 0
        while isinstance(node, Inner):
            slot = nibble(key, depth)
            path.append((node, slot))
            node = node.kids[slot]
            depth += 1
        if node is None or node.key != key:
            raise KeyError(key.hex().upper())
        parent, slot = path[-1]
        parent.kids[slot] = None
        self.size -= 1
        for n, _ in path:
            n.hash = None
        # collapse from the bottom: an inner (not the root) left with one leaf and no inner child hands
        # the leaf up. A non-root inner always holds >= 2 leaves below it before a delete (set() only
        # creates one to separate two keys), so after one delete it holds >= 1 and never ends up empty.
        for level in range(len(path) - 1, 0, -1):
            n = path[level][0]
            kids = [k for k in n.kids if k is not None]
            if len(kids) == 1 and isinstance(kids[0], Leaf):
                up, up_slot = path[level - 1]
                up.kids[up_slot] = kids[0]
            else:
                break

    def root_hash(self):
        if self.size == 0:
            return ZERO
        return self._hash(self.root)

    def _hash(self, node):
        if isinstance(node, Leaf):
            return node.hash
        if node.hash is None:
            node.hash = sha512half(PREFIX_INNER, *(ZERO if k is None else self._hash(k) for k in node.kids))
        return node.hash

    def walk(self):
        """Yield ("inner", depth, fan_out) and ("leaf", depth, key, data) for every node.
        depth = number of inner nodes from the root down to (and including) the parent."""
        stack = [(self.root, 0)]
        while stack:
            node, depth = stack.pop()
            yield ("inner", depth, node.count())
            for k in node.kids:
                if isinstance(k, Inner):
                    stack.append((k, depth + 1))
                elif k is not None:
                    yield ("leaf", depth + 1, k.key, k.data)


# ---------------------------------------------------------------- definitions + codec


class Defs:
    def __init__(self, path=DEFINITIONS):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        self.hash = d.get("hash")
        self.types = d["TYPES"]
        self.entry_types = {k: v for k, v in d["LEDGER_ENTRY_TYPES"].items() if v >= 0}
        self.entry_names = {v: k for k, v in self.entry_types.items()}
        self.by_name, self.by_id = {}, {}
        for name, info in d["FIELDS"]:
            t = self.types.get(info["type"], -1)
            if t <= 0 or info["nth"] <= 0 or not info.get("isSerialized", True):
                continue
            fid = (t, info["nth"])
            self.by_name[name] = fid
            self.by_id.setdefault(fid, name)

    def fid(self, name):
        return self.by_name[name]

    def name(self, fid):
        return self.by_id.get(fid, f"?{fid[0]}.{fid[1]}")


def read_field_id(buf, i):
    if i >= len(buf):
        raise CodecError("truncated field header")
    b = buf[i]
    t, n = b >> 4, b & 0x0F
    i += 1
    if t == 0 and n == 0:
        t, n = buf[i], buf[i + 1]
        i += 2
    elif t == 0:
        t = buf[i]
        i += 1
    elif n == 0:
        n = buf[i]
        i += 1
    return t, n, i


def field_id_bytes(t, n):
    if t < 16 and n < 16:
        return bytes([(t << 4) | n])
    if t < 16:
        return bytes([t << 4, n])
    if n < 16:
        return bytes([n, t])
    return bytes([0, t, n])


def read_vl(buf, i):
    b0 = buf[i]
    if b0 <= 192:
        return b0, i + 1
    if b0 <= 240:
        return 193 + (b0 - 193) * 256 + buf[i + 1], i + 2
    if b0 <= 254:
        return 12481 + (b0 - 241) * 65536 + buf[i + 1] * 256 + buf[i + 2], i + 3
    raise CodecError(f"bad VL prefix 0x{b0:02X}")


def skip_value(buf, i, t):
    """Index just past the value of type t starting at i."""
    if t in FIXED:
        return i + FIXED[t]
    if t in VL_TYPES:
        n, j = read_vl(buf, i)
        return j + n
    if t == 6:  # Amount: native 8, MPT 33, issued 48
        if buf[i] & 0x80:
            return i + 48
        if buf[i] & 0x20:
            return i + 33
        return i + 8
    if t == ST_OBJECT:
        while True:
            ft, fn, i = read_field_id(buf, i)
            if (ft, fn) == OBJECT_END:
                return i
            i = skip_value(buf, i, ft)
    if t == ST_ARRAY:
        while True:
            ft, fn, i = read_field_id(buf, i)
            if (ft, fn) == ARRAY_END:
                return i
            if ft != ST_OBJECT:
                raise CodecError(f"array element of type {ft}")
            i = skip_value(buf, i, ft)
    if t == 24:  # Issue: currency, + issuer unless native
        return i + (20 if buf[i : i + 20] == bytes(20) else 40)
    if t == 18:  # PathSet
        while True:
            b = buf[i]
            i += 1
            if b == 0x00:
                return i
            if b == 0xFF:
                continue
            i += 20 * (bool(b & 0x01) + bool(b & 0x10) + bool(b & 0x20))
    raise CodecError(f"unsupported field type {t}")


def parse(buf, end_marker=None):
    """{(type, nth): raw value} of the fields in buf (or up to end_marker); raises CodecError."""
    fields, i, last = {}, 0, None
    while i < len(buf):
        t, n, j = read_field_id(buf, i)
        if end_marker is not None and (t, n) == end_marker:
            if j != len(buf):
                raise CodecError("bytes after end marker")
            return fields
        k = skip_value(buf, j, t)
        if k > len(buf):
            raise CodecError(f"field {t}.{n} runs past the end")
        if (t, n) in fields:
            raise CodecError(f"duplicate field {t}.{n}")
        if last is not None and (t, n) < last:
            raise CodecError(f"field {t}.{n} out of canonical order")
        fields[(t, n)] = bytes(buf[j:k])
        last, i = (t, n), k
    if end_marker is not None:
        raise CodecError("missing end marker")
    return fields


def serialize(fields):
    return b"".join(field_id_bytes(*fid) + fields[fid] for fid in sorted(fields))


def object_fields(raw):
    """Fields of a raw STObject value (content + end marker)."""
    return parse(raw, OBJECT_END)


def array_elements(raw):
    """[(nth of element object, {fields})] of a raw STArray value (content + end marker)."""
    out, i = [], 0
    while True:
        t, n, j = read_field_id(raw, i)
        if (t, n) == ARRAY_END:
            if j != len(raw):
                raise CodecError("bytes after array end")
            return out
        if t != ST_OBJECT:
            raise CodecError(f"array element of type {t}")
        k = skip_value(raw, j, t)
        out.append((n, object_fields(raw[j:k])))
        i = k


def object_raw(fields):
    return serialize(fields) + field_id_bytes(*OBJECT_END)


def array_raw(elements):
    return b"".join(field_id_bytes(ST_OBJECT, n) + object_raw(f) for n, f in elements) + field_id_bytes(
        *ARRAY_END
    )


def u16(raw):
    return int.from_bytes(raw, "big")


u32 = u16
