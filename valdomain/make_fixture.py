#!/usr/bin/env python3
"""Build the synthetic, signed valdomain test fixture (tests/fixtures/valdomain/data/).

Dev-only: signing needs the `cryptography` package (python3-cryptography on Ubuntu 22.04); the
verifier and the tests read the committed output with the stdlib only. Keys are derived from fixed
seeds; secp256k1 signatures use cryptography's random nonce, so a re-run changes those bytes (the
fixture is generated once and committed; tests never re-sign). Every domain is under .example
(RFC 2606) and every HTTP answer is synthetic: nothing is fetched.

Validators (EXPECTED below is the hand-written answer the tests compare against):
  verified.example   pft path: valid attestation          xrp path: 404       -> verified
  badatt.example     attestation over another domain      unreachable         -> bad_attestation
  otherkey.example   correct message, signed by another key  404              -> bad_attestation
  noatt.example      entry without attestation            404                 -> no_attestation
  notlisted.example  TOML lists another key only          404                 -> key_not_listed
  unparse.example    200 HTML page                        404                 -> toml_unparseable
  httperr.example    404                                  500                 -> http_error
  unreach.example    unreachable                          unreachable         -> unreachable
  xrponly.example    404                                  valid attestation   -> verified (best of paths)
  trunc.example      200 but cut at the size cap          unreachable         -> http_error
  shared.example     two validators, one file: A attested, B not listed -> verified / key_not_listed
  (no domain)        manifest without sfDomain -> no_domain
Usage: python3 valdomain/make_fixture.py [--out tests/fixtures/valdomain/data]
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import collect_valdomain as C  # noqa: E402
import lib_valdomain as D  # noqa: E402

L = D.L
T0 = 1795000000  # fixed unix time of the synthetic collection (2026-11-18)
SN = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
PFT, XRP = D.TOML_PATHS

EXPECTED = {
    "verified.example": "verified",
    "badatt.example": "bad_attestation",
    "otherkey.example": "bad_attestation",
    "noatt.example": "no_attestation",
    "notlisted.example": "key_not_listed",
    "unparse.example": "toml_unparseable",
    "httperr.example": "http_error",
    "unreach.example": "unreachable",
    "xrponly.example": "verified",
    "trunc.example": "http_error",
    "shared.example/A": "verified",
    "shared.example/B": "key_not_listed",
    "-": "no_domain",
}


def ed_key(name):
    return ed25519.Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(b"valdomain-ed-" + name.encode()).digest()
    )


def ed_pub_hex(k):
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ED" + raw.hex().upper()


def sk_key(name):
    n = int.from_bytes(hashlib.sha256(b"valdomain-sk-" + name.encode()).digest(), "big") % (SN - 1) + 1
    return ec.derive_private_key(n, ec.SECP256K1())


def sk_pub_hex(k):
    return (
        k.public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        .hex()
        .upper()
    )


def sk_sign(k, msg):
    """postfiatd secp256k1 signature: ECDSA over SHA-512-Half(msg), DER, canonical low-S."""
    digest = L.sha512_half(msg)
    der = k.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r, s = utils.decode_dss_signature(der)
    if s > SN // 2:
        s = SN - s
    return utils.encode_dss_signature(r, s)


def sign(key, msg):
    return key.sign(msg) if isinstance(key, ed25519.Ed25519PrivateKey) else sk_sign(key, msg)


def vl_field(t, f, value):
    n = len(value)
    assert n <= 192
    head = bytes([(t << 4) | f]) if f < 16 else bytes([t << 4, f])
    return head + bytes([n]) + value


def manifest(master, signing, seq, domain):
    m_pub = bytes.fromhex(ed_pub_hex(master))
    s_hex = ed_pub_hex(signing) if isinstance(signing, ed25519.Ed25519PrivateKey) else sk_pub_hex(signing)
    seq_f = bytes([0x24]) + seq.to_bytes(4, "big")
    pk = vl_field(7, 1, m_pub)
    spk = vl_field(7, 3, bytes.fromhex(s_hex))
    dom = vl_field(7, 7, domain.encode()) if domain else b""
    signed = L.MANIFEST_PREFIX + seq_f + pk + spk + dom
    sig = sign(signing, signed)
    msig = master.sign(signed)
    blob = seq_f + pk + spk + vl_field(7, 6, sig) + dom + vl_field(7, 18, msig)
    return base64.b64encode(blob).decode()


def attestation(master, domain):
    return master.sign(D.attestation_message(domain, L.node_public_b58(ed_pub_hex(master)))).hex().upper()


def toml(entries):
    out = ["[METADATA]\nmodified = 2026-11-18T00:00:00Z\n"]
    for e in entries:
        out.append("[[VALIDATORS]]")
        out += [f'{k} = "{v}"' for k, v in e.items()]
        out.append("")
    return "\n".join(out).encode()


def response(domain, path, status, body=b"", truncated=False):
    rec = {
        "domain": domain,
        "path": path,
        "url": f"https://{domain}{path}",
        "t": T0 + 60,
        "outcome": "response",
        "status": status,
        "content_type": "text/plain" if status == 200 else "text/html",
        "redirects": [],
        "final_url": None,
        "bytes": len(body),
        "truncated": truncated,
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    if status == 200 and not truncated:
        rec["body_b64"] = base64.b64encode(body).decode()
    return rec


def unreachable(domain, path):
    return {
        "domain": domain,
        "path": path,
        "url": f"https://{domain}{path}",
        "t": T0 + 60,
        "outcome": "unreachable",
    }


def build(out):
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    names = [d for d in EXPECTED]
    masters = {n: ed_key("master-" + n) for n in names}
    signers = {n: (sk_key("sign-" + n) if i % 2 else ed_key("sign-" + n)) for i, n in enumerate(names)}
    domain = {n: (None if n == "-" else n.split("/")[0]) for n in names}
    b58 = {n: L.node_public_b58(ed_pub_hex(masters[n])) for n in names}
    mans = {n: manifest(masters[n], signers[n], 3, domain[n]) for n in names}

    pub_master, pub_sign = ed_key("publisher-master"), sk_key("publisher-sign")
    pub_man = manifest(pub_master, pub_sign, 1, None)
    inner = {
        "sequence": 7,
        "effective": T0 - 86400 - L.RIPPLE_EPOCH,
        "expiration": T0 + 365 * 86400 - L.RIPPLE_EPOCH,
        "validators": [{"validation_public_key": ed_pub_hex(masters[n]), "manifest": mans[n]} for n in names],
    }
    raw_blob = json.dumps(inner, sort_keys=True).encode()
    vl = {
        "public_key": ed_pub_hex(pub_master),
        "manifest": pub_man,
        "version": 2,
        "blobs_v2": [
            {"blob": base64.b64encode(raw_blob).decode(), "signature": sign(pub_sign, raw_blob).hex().upper()}
        ],
    }
    vl_raw = json.dumps(vl, indent=1).encode()
    site = "https://vl.example/testnet_vl.json"
    C.write_atomic(os.path.join(out, "vl.json"), vl_raw)
    C.write_atomic(
        os.path.join(out, "vl-fetch.json"),
        C.dump(
            {
                "url": site,
                "t": T0,
                "status": 200,
                "bytes": len(vl_raw),
                "sha256": hashlib.sha256(vl_raw).hexdigest(),
            }
        ),
    )
    node = {
        "validators": {
            "publisher_lists": [
                {"pubkey_publisher": ed_pub_hex(pub_master), "seq": 7, "list": sorted(b58.values())}
            ]
        },
        "validator_list_sites": {"validator_sites": [{"uri": site, "last_refresh_status": "accepted"}]},
        "server_info": {"build_version": "1.0.9", "server_state": "full"},
        "t": T0,
        "rpc": "http://127.0.0.1:5005/",
    }
    C.write_atomic(os.path.join(out, "node.json"), C.dump(node))

    def ent(n, att=None, **kw):
        e = {"public_key": b58[n]}
        if att is not None:
            e["attestation"] = att
        e.update(kw)
        return e

    other = ed_key("someone-else")
    files = {
        ("verified.example", PFT): response(
            "verified.example",
            PFT,
            200,
            toml(
                [
                    ent(
                        "verified.example",
                        attestation(masters["verified.example"], "verified.example"),
                        network="testnet",
                    )
                ]
            ),
        ),
        ("verified.example", XRP): response("verified.example", XRP, 404, b"<html>404</html>"),
        ("badatt.example", PFT): response(
            "badatt.example",
            PFT,
            200,
            toml([ent("badatt.example", attestation(masters["badatt.example"], "verified.example"))]),
        ),
        ("badatt.example", XRP): unreachable("badatt.example", XRP),
        ("otherkey.example", PFT): response(
            "otherkey.example",
            PFT,
            200,
            toml(
                [
                    ent(
                        "otherkey.example",
                        other.sign(D.attestation_message("otherkey.example", b58["otherkey.example"])).hex(),
                    )
                ]
            ),
        ),
        ("otherkey.example", XRP): response("otherkey.example", XRP, 404, b"nope"),
        ("noatt.example", PFT): response(
            "noatt.example", PFT, 200, toml([ent("noatt.example", network="testnet")])
        ),
        ("noatt.example", XRP): response("noatt.example", XRP, 404, b""),
        ("notlisted.example", PFT): response(
            "notlisted.example",
            PFT,
            200,
            toml([{"public_key": L.node_public_b58(ed_pub_hex(other)), "attestation": "00"}]),
        ),
        ("notlisted.example", XRP): response("notlisted.example", XRP, 404, b""),
        ("unparse.example", PFT): response(
            "unparse.example", PFT, 200, b"<!doctype html><html><body>hi</body></html>\n"
        ),
        ("unparse.example", XRP): response("unparse.example", XRP, 404, b""),
        ("httperr.example", PFT): response("httperr.example", PFT, 404, b"not found"),
        ("httperr.example", XRP): response("httperr.example", XRP, 500, b"oops"),
        ("unreach.example", PFT): unreachable("unreach.example", PFT),
        ("unreach.example", XRP): unreachable("unreach.example", XRP),
        ("xrponly.example", PFT): response("xrponly.example", PFT, 404, b""),
        ("xrponly.example", XRP): response(
            "xrponly.example",
            XRP,
            200,
            toml(
                [ent("xrponly.example", attestation(masters["xrponly.example"], "xrponly.example").lower())]
            ),
        ),
        ("trunc.example", PFT): response("trunc.example", PFT, 200, b"[[VALIDATORS]]\n" * 10, truncated=True),
        ("trunc.example", XRP): unreachable("trunc.example", XRP),
        ("shared.example", PFT): response(
            "shared.example",
            PFT,
            200,
            toml([ent("shared.example/A", attestation(masters["shared.example/A"], "shared.example"))]),
        ),
        ("shared.example", XRP): response("shared.example", XRP, 404, b""),
    }
    for (dom, p), rec in files.items():
        C.write_atomic(os.path.join(out, "toml", dom, C.cache_name(p)), C.dump(rec))

    lines = []
    for src in ("node", "public"):
        for n in names:
            seq, dom, man = 3, domain[n], mans[n]
            if src == "public" and n == "noatt.example":
                seq, man = 4, manifest(masters[n], signers[n], 4, dom)  # rotated since the VL
            if src == "public" and n == "httperr.example":
                seq, dom = 4, "moved.example"
                man = manifest(masters[n], signers[n], 4, dom)  # newer manifest, other domain
            det = {"domain": dom, "seq": seq, "master_key": b58[n], "ephemeral_key": "-"}
            if dom is None:
                det.pop("domain")
            ans = {"details": det, "manifest": man, "requested": b58[n], "status": "success", "error": None}
            lines.append({"src": src, "master": ed_pub_hex(masters[n]), "t": T0 + 30, "answer": ans})
    with open(os.path.join(out, "manifests.jsonl"), "w") as fh:
        fh.writelines(json.dumps(e, sort_keys=True) + "\n" for e in lines)
    C.write_sums(out)
    # a second seq-3 manifest of verified.example's master naming another domain (tests swap it in)
    n = "verified.example"
    conflict = {
        "master": ed_pub_hex(masters[n]),
        "manifest": manifest(masters[n], signers[n], 3, "evil.example"),
    }
    with open(os.path.join(os.path.dirname(out), "conflict.json"), "w") as fh:
        json.dump(conflict, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return {n: b58[n] for n in names}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(here, "..", "tests", "fixtures", "valdomain", "data"))
    a = ap.parse_args(argv)
    keys = build(a.out)
    with open(os.path.join(os.path.dirname(a.out), "expected.json"), "w") as fh:
        json.dump(
            {keys[n]: {"name": n, "class": c} for n, c in EXPECTED.items()}, fh, indent=1, sort_keys=True
        )
        fh.write("\n")
    print(f"wrote {a.out} ({len(keys)} validators)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
