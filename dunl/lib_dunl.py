"""dunl/ shared helpers (Python 3 stdlib only): VL + manifest decoding and signature checks.

Pure-Python verifiers, written for auditing public artifacts (no secret keys are ever handled):
  * Ed25519 (RFC 8032 section 5.1.7) for master-key signatures (sfMasterSignature),
  * ECDSA secp256k1 over SHA-512-Half for VL blob signatures and secp256k1 ephemeral keys,
    the scheme of postfiatd/rippled `verify()` (and of scoring_service/services/vl_generator.py
    sign_blob: sha512_half digest, DER, canonical low-S).
Manifests are STObjects; the signed data is HashPrefix::manifest ("MAN\\0") + the serialized
object without sfSignature / sfMasterSignature (rippled Manifest.cpp).
"""

import base64
import hashlib
import json

RIPPLE_EPOCH = 946684800
B58_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
NODE_PUBLIC_PREFIX = 0x1C
MANIFEST_PREFIX = b"MAN\x00"

# STObject field ids (type, field) used by manifests
F_SEQUENCE = (2, 4)
F_PUBLIC_KEY = (7, 1)
F_SIGNING_PUB_KEY = (7, 3)
F_SIGNATURE = (7, 6)
F_DOMAIN = (7, 7)
F_MASTER_SIGNATURE = (7, 18)
SIGNATURE_FIELDS = {F_SIGNATURE, F_MASTER_SIGNATURE}


def sha512_half(data):
    return hashlib.sha512(data).digest()[:32]


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def content_hash(obj):
    """scoring_service ipfs_publisher._content_hash: sha256 of compact sorted-key JSON."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- base58 node public keys


def b58encode(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58_ALPHABET[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return B58_ALPHABET[0] * pad + out


def b58decode(text):
    n = 0
    for ch in text:
        i = B58_ALPHABET.find(ch)
        if i < 0:
            raise ValueError(f"bad base58 character {ch!r}")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip(B58_ALPHABET[0]))
    return b"\x00" * pad + body


def node_public_b58(key_hex):
    """hex 33-byte public key -> nH... (TokenType::NodePublic) string."""
    raw = bytes([NODE_PUBLIC_PREFIX]) + bytes.fromhex(key_hex)
    check = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    return b58encode(raw + check)


def node_public_hex(b58):
    """nH... -> hex 33-byte key; raises ValueError on a bad prefix, length or checksum."""
    raw = b58decode(b58)
    if len(raw) != 38 or raw[0] != NODE_PUBLIC_PREFIX:
        raise ValueError(f"not a node public key: {b58}")
    body, check = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != check:
        raise ValueError(f"bad checksum: {b58}")
    return body[1:].hex().upper()


# ---------------------------------------------------------------- STObject (manifest) parsing


def parse_stobject(data):
    """Return [(type, field, start, end, value_bytes_or_int)] for a flat STObject of the types
    manifests use (UInt16, UInt32, VL blobs). Raises ValueError on anything else / truncation."""
    out = []
    i = 0
    while i < len(data):
        start = i
        b = data[i]
        i += 1
        t, f = b >> 4, b & 0x0F
        if t == 0:
            t = data[i]
            i += 1
        if f == 0:
            f = data[i]
            i += 1
        if t == 1:
            if i + 2 > len(data):
                raise ValueError("truncated UInt16")
            val = int.from_bytes(data[i : i + 2], "big")
            i += 2
        elif t == 2:
            if i + 4 > len(data):
                raise ValueError("truncated UInt32")
            val = int.from_bytes(data[i : i + 4], "big")
            i += 4
        elif t == 7:
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
            if i + n > len(data):
                raise ValueError("truncated VL blob")
            val = data[i : i + n]
            i += n
        else:
            raise ValueError(f"unsupported field type {t} in manifest")
        out.append((t, f, start, i, val))
    return out


def parse_manifest(b64):
    """Decode a base64 manifest -> dict(master, signing, sequence, domain, signature,
    master_signature, signed_data). Keys are upper hex; signed_data is bytes."""
    data = base64.b64decode(b64, validate=True)
    fields = parse_stobject(data)
    got = {(t, f): v for t, f, _s, _e, v in fields}
    signed = MANIFEST_PREFIX + b"".join(
        data[s:e] for t, f, s, e, _v in fields if (t, f) not in SIGNATURE_FIELDS
    )
    master = got.get(F_PUBLIC_KEY)
    if master is None:
        raise ValueError("manifest has no sfPublicKey")
    signing = got.get(F_SIGNING_PUB_KEY)
    return {
        "master": master.hex().upper(),
        "signing": signing.hex().upper() if signing is not None else None,
        "sequence": got.get(F_SEQUENCE),
        "domain": got[F_DOMAIN].decode("utf-8", "replace") if F_DOMAIN in got else None,
        "signature": got.get(F_SIGNATURE),
        "master_signature": got.get(F_MASTER_SIGNATURE),
        "signed_data": signed,
    }


def verify_manifest(m):
    """-> (master_ok, signing_ok). The revocation manifest (seq 0xFFFFFFFF) has no signing key."""
    master_ok = bool(m["master_signature"]) and verify(m["master"], m["signed_data"], m["master_signature"])
    if m["signing"] is None:
        return master_ok, m["sequence"] == 0xFFFFFFFF
    signing_ok = bool(m["signature"]) and verify(m["signing"], m["signed_data"], m["signature"])
    return master_ok, signing_ok


def decode_vl(vl):
    """Signed VL v2 JSON -> dict(publisher (manifest dict), blobs: [dict(sequence, effective,
    expiration, validators: [(validation_public_key, manifest_b64)], signature_ok, raw)])."""
    publisher = parse_manifest(vl["manifest"])
    blobs = []
    for b in vl.get("blobs_v2") or (
        [{"blob": vl["blob"], "signature": vl["signature"]}] if "blob" in vl else []
    ):
        raw = base64.b64decode(b["blob"], validate=True)
        inner = json.loads(raw)
        sig_ok = bool(publisher["signing"]) and verify(
            publisher["signing"], raw, bytes.fromhex(b["signature"])
        )
        blobs.append(
            {
                "sequence": inner.get("sequence"),
                "effective": inner.get("effective"),
                "expiration": inner.get("expiration"),
                "validators": [
                    (v.get("validation_public_key"), v.get("manifest")) for v in inner.get("validators", [])
                ],
                "signature_ok": sig_ok,
                "low_s": der_low_s(bytes.fromhex(b["signature"])),
                "raw_sha256": sha256_hex(raw),
            }
        )
    return {
        "publisher": publisher,
        "public_key": vl.get("public_key"),
        "version": vl.get("version"),
        "blobs": blobs,
    }


_verified = {}


def verify(key_hex, message, signature):
    """postfiatd verify(): ed25519 keys (ED prefix) sign the message, secp256k1 its SHA-512-Half.
    Results are memoized on (key, sha256(message), signature): the pure-Python math is slow and the
    same manifests recur in every round."""
    memo = (key_hex, hashlib.sha256(message).digest(), bytes(signature))
    if memo not in _verified:
        _verified[memo] = _verify(key_hex, message, signature)
    return _verified[memo]


def _verify(key_hex, message, signature):
    key = bytes.fromhex(key_hex)
    if len(key) == 33 and key[0] == 0xED:
        return ed25519_verify(key[1:], message, signature)
    if len(key) == 33 and key[0] in (2, 3):
        return secp256k1_verify(key, sha512_half(message), signature)
    return False


# ---------------------------------------------------------------- Ed25519 (RFC 8032)

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _ed_add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = 2 * t1 * t2 * _D % _P
    D = 2 * z1 * z2 % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _ed_mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        s >>= 1
    return q


def _ed_eq(a, b):
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _ed_decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        if sign:
            return None
        x = 0
    else:
        x = pow(x2, (_P + 3) // 8, _P)
        if (x * x - x2) % _P:
            x = x * _I % _P
        if (x * x - x2) % _P:
            return None
        if (x & 1) != sign:
            x = _P - x
    return (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = _ed_decompress(_GY.to_bytes(32, "little"))


def ed25519_verify(pub, msg, sig):
    if len(pub) != 32 or len(sig) != 64:
        return False
    a = _ed_decompress(pub)
    r = _ed_decompress(sig[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % _L
    return _ed_eq(_ed_mul(s, _G), _ed_add(r, _ed_mul(h, a)))


# ---------------------------------------------------------------- secp256k1 ECDSA

_SP = 2**256 - 2**32 - 977
_SN = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SG = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _sk_add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0]:
        if (a[1] + b[1]) % _SP == 0:
            return None
        m = 3 * a[0] * a[0] * pow(2 * a[1], _SP - 2, _SP) % _SP
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], _SP - 2, _SP) % _SP
    x = (m * m - a[0] - b[0]) % _SP
    return (x, (m * (a[0] - x) - a[1]) % _SP)


def _sk_mul(k, p):
    q = None
    while k:
        if k & 1:
            q = _sk_add(q, p)
        p = _sk_add(p, p)
        k >>= 1
    return q


def _sk_decompress(key):
    if len(key) != 33 or key[0] not in (2, 3):
        return None
    x = int.from_bytes(key[1:], "big")
    if x >= _SP:
        return None
    y2 = (pow(x, 3, _SP) + 7) % _SP
    y = pow(y2, (_SP + 1) // 4, _SP)
    if y * y % _SP != y2:
        return None
    if (y & 1) != (key[0] & 1):
        y = _SP - y
    return (x, y)


def der_decode(sig):
    """Strict DER ECDSA signature -> (r, s); raises ValueError."""
    if len(sig) < 8 or sig[0] != 0x30 or sig[1] != len(sig) - 2:
        raise ValueError("bad DER sequence")
    out = []
    i = 2
    for _ in range(2):
        if i + 2 > len(sig) or sig[i] != 0x02:
            raise ValueError("bad DER integer")
        n = sig[i + 1]
        v = sig[i + 2 : i + 2 + n]
        if n == 0 or len(v) != n or v[0] & 0x80 or (n > 1 and v[0] == 0 and not v[1] & 0x80):
            raise ValueError("non-canonical DER integer")
        out.append(int.from_bytes(v, "big"))
        i += 2 + n
    if i != len(sig):
        raise ValueError("trailing DER bytes")
    return out[0], out[1]


def der_low_s(sig):
    try:
        _r, s = der_decode(sig)
    except ValueError:
        return False
    return s <= _SN // 2


def secp256k1_verify(key, digest, sig):
    q = _sk_decompress(key)
    if q is None:
        return False
    try:
        r, s = der_decode(sig)
    except ValueError:
        return False
    if not (0 < r < _SN and 0 < s < _SN):
        return False
    e = int.from_bytes(digest, "big") % _SN
    w = pow(s, _SN - 2, _SN)
    pt = _sk_shamir(e * w % _SN, _SG, r * w % _SN, q)
    return pt is not None and pt[0] % _SN == r


# Jacobian coordinates (X, Y, Z) <-> affine (X/Z^2, Y/Z^3); one inversion per verify instead of
# one per point operation. Same results as _sk_add/_sk_mul (tests/test_dunl.py compares them).


def _jac_double(p):
    x, y, z = p
    if y == 0 or z == 0:
        return (1, 1, 0)
    yy = y * y % _SP
    s = 4 * x * yy % _SP
    m = 3 * x * x % _SP  # curve a = 0
    x3 = (m * m - 2 * s) % _SP
    y3 = (m * (s - x3) - 8 * yy * yy) % _SP
    return (x3, y3, 2 * y * z % _SP)


def _jac_add_affine(p, q):
    """Jacobian p + affine q."""
    x1, y1, z1 = p
    if z1 == 0:
        return (q[0], q[1], 1)
    z1z1 = z1 * z1 % _SP
    u2 = q[0] * z1z1 % _SP
    s2 = q[1] * z1 * z1z1 % _SP
    h = (u2 - x1) % _SP
    r = (s2 - y1) % _SP
    if h == 0:
        return _jac_double(p) if r == 0 else (1, 1, 0)
    hh = h * h % _SP
    hhh = h * hh % _SP
    v = x1 * hh % _SP
    x3 = (r * r - hhh - 2 * v) % _SP
    y3 = (r * (v - x3) - y1 * hhh) % _SP
    return (x3, y3, z1 * h % _SP)


def _sk_shamir(a, p, b, q):
    """a*p + b*q (affine in, affine out, None = infinity), one joint double-and-add pass."""
    pq = _sk_add(p, q)
    acc = (1, 1, 0)
    for i in range(max(a.bit_length(), b.bit_length()) - 1, -1, -1):
        acc = _jac_double(acc)
        bits = ((a >> i) & 1, (b >> i) & 1)
        add = {(1, 0): p, (0, 1): q, (1, 1): pq}.get(bits)
        if add is not None:
            acc = _jac_add_affine(acc, add)
    x, y, z = acc
    if z == 0:
        return None
    zi = pow(z, _SP - 2, _SP)
    zi2 = zi * zi % _SP
    return (x * zi2 % _SP, y * zi2 * zi % _SP)
