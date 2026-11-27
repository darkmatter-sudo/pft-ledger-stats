"""valsig/ shared helpers (Python 3 stdlib only): STValidation decoding, signature and trust checks,
quorum arithmetic. Signature math is reused from dunl/lib_dunl.py (pure-Python secp256k1/Ed25519).

A validation is an STObject. Its signing hash is SHA-512-Half of HashPrefix::validation ("VAL\\0")
followed by the serialized object without sfSignature (rippled STValidation::getSigningHash ->
STObject::getSigningHash; sfSignature is the only non-signing field). lib_dunl.verify() hashes the
message for secp256k1 keys and signs it directly for Ed25519 keys, like postfiatd's verify().
"""

import math
import os
import struct
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "dunl"))
import lib_dunl  # noqa: E402

VALIDATION_PREFIX = b"VAL\x00"

# (type, field) ids from postfiatd include/xrpl/protocol/detail/sfields.macro
FIELDS = {
    (2, 2): "Flags",
    (2, 6): "LedgerSequence",
    (2, 7): "CloseTime",
    (2, 9): "SigningTime",
    (2, 24): "LoadFee",
    (2, 31): "ReserveBase",
    (2, 32): "ReserveIncrement",
    (3, 5): "BaseFee",
    (3, 10): "Cookie",
    (3, 11): "ServerVersion",
    (5, 1): "LedgerHash",
    (5, 23): "ConsensusHash",
    (5, 25): "ValidatedHash",
    (6, 22): "BaseFeeDrops",
    (6, 23): "ReserveBaseDrops",
    (6, 24): "ReserveIncrementDrops",
    (7, 3): "SigningPubKey",
    (7, 6): "Signature",
    (19, 3): "Amendments",
}
F_SIGNATURE = (7, 6)
FIXED = {1: 2, 2: 4, 3: 8, 4: 16, 5: 32, 16: 1, 17: 20}
VF_FULL = 0x00000001
VF_FULLY_CANONICAL = 0x80000000


def _vl_len(data, i):
    if i >= len(data):
        raise ValueError("truncated VL length")
    n = data[i]
    i += 1
    if n > 240:
        if i + 1 >= len(data):
            raise ValueError("truncated VL length")
        n = 12481 + (n - 241) * 65536 + data[i] * 256 + data[i + 1]
        i += 2
    elif n > 192:
        if i >= len(data):
            raise ValueError("truncated VL length")
        n = 193 + (n - 193) * 256 + data[i]
        i += 1
    return n, i


def parse_fields(data):
    """Flat STObject -> [(type, field, start, end, value bytes)]. Raises ValueError on unknown
    types, truncation, or fields out of canonical (type, field) order."""
    out = []
    i = 0
    prev = (0, 0)
    while i < len(data):
        start = i
        b = data[i]
        i += 1
        t, f = b >> 4, b & 0x0F
        if t == 0:
            if i >= len(data):
                raise ValueError("truncated field id")
            t = data[i]
            i += 1
        if f == 0:
            if i >= len(data):
                raise ValueError("truncated field id")
            f = data[i]
            i += 1
        if t in FIXED:
            n = FIXED[t]
        elif t == 6:
            if i >= len(data):
                raise ValueError("truncated Amount")
            n = 8 if not data[i] & 0x80 else 48  # native drops vs IOU amount
        elif t in (7, 8, 19):
            n, i = _vl_len(data, i)
        else:
            raise ValueError(f"unsupported field type {t}")
        if i + n > len(data):
            raise ValueError(f"truncated field ({t},{f})")
        if (t, f) <= prev:
            raise ValueError(f"field ({t},{f}) out of canonical order")
        prev = (t, f)
        out.append((t, f, start, i + n, data[i : i + n]))
        i += n
    return out


def decode_validation(hex_data):
    """Raw STValidation hex -> dict with the decoded fields, `signed` (the exact bytes the signature
    covers, prefix included) and `signature` (bytes). Raises ValueError on malformed input."""
    data = bytes.fromhex(hex_data)
    fields = parse_fields(data)
    got = {}
    for t, f, _s, _e, v in fields:
        name = FIELDS.get((t, f), f"F{t}_{f}")
        got[name] = v
    for need in ("Flags", "LedgerSequence", "SigningTime", "LedgerHash", "SigningPubKey", "Signature"):
        if need not in got:
            raise ValueError(f"missing sf{need}")
    signed = VALIDATION_PREFIX + b"".join(data[s:e] for t, f, s, e, _v in fields if (t, f) != F_SIGNATURE)
    u32 = lambda b: int.from_bytes(b, "big")  # noqa: E731
    out = {
        "flags": u32(got["Flags"]),
        "ledger_index": u32(got["LedgerSequence"]),
        "signing_time": u32(got["SigningTime"]),
        "ledger_hash": got["LedgerHash"].hex().upper(),
        "signing_key": got["SigningPubKey"].hex().upper(),
        "signature": got["Signature"],
        "validated_hash": got["ValidatedHash"].hex().upper() if "ValidatedHash" in got else None,
        "fields": sorted(n for n in got),
        "signed": signed,
    }
    out["full"] = bool(out["flags"] & VF_FULL)
    return out


def check_signature(dv):
    """-> (ok, reason). Fully-canonical flag requires a low-S DER signature, as postfiatd does."""
    if dv["flags"] & VF_FULLY_CANONICAL and not lib_dunl.der_low_s(dv["signature"]):
        return False, "not_fully_canonical"
    try:
        ok = lib_dunl.verify(dv["signing_key"], dv["signed"], dv["signature"])
    except ValueError:
        ok = False
    return (True, "ok") if ok else (False, "bad_signature")


def f32(x):
    """Round to IEEE-754 single precision (postfiatd multiplies by 0.67f / 0.6f)."""
    return struct.unpack("f", struct.pack("f", x))[0]


def expected_quorum(unl_size, effective_size):
    """postfiatd ValidatorList::calculateQuorum (no command-line quorum, lists available):
    max(ceil(effective * 0.67f), ceil(unl * 0.6f)), computed in float like the C++ code."""
    a = math.ceil(f32(f32(float(effective_size)) * f32(0.67)))
    b = math.ceil(f32(f32(float(unl_size)) * f32(0.6)))
    return max(a, b)
