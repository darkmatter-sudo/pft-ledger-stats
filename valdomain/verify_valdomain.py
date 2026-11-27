#!/usr/bin/env python3
"""Offline verifier for the valdomain audit (Python 3 stdlib only): re-derives every result from a
collect_valdomain.py data directory, with the network disabled (--offline installs a socket guard).

Checks (any failure = internal inconsistency, exit 1):
  sums       SHA256SUMS lists every data file and every hash matches
  vl         publisher manifest + VL blob signature (low-S) verify; the node trusts this publisher key,
             list sequence and exactly these validators; vl-fetch.json matches the file and the site
  manifests  every VL validator manifest verifies (master + signing signature)
  toml       every domain has one cached fetch per TOML path, each body matches its sha256
  crosscheck for every validator, the `manifest` answers of your node and of an independent public node
             verify, name the same master, are not older than the VL's, and at the same sequence carry
             the same domain (a node's own decoded `details.domain` must equal our decode)
Classification (results, not failures): lib_valdomain.CLASSES, best outcome over the TOML paths.
Outputs (--out DIR): results.json, validators.csv, table.md (deterministic: no wall-clock times).
Exit codes: 0 all checks ran and passed (whatever the classes), 1 inconsistency, 2 no data.
Usage: verify_valdomain.py --offline DATA_DIR [--out DIR] | controls --offline DATA_DIR
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import collect_valdomain as C  # noqa: E402
import lib_valdomain as D  # noqa: E402

L = D.L
RIPPLE_EPOCH = L.RIPPLE_EPOCH
SRCS = ("node", "public")


class NoData(Exception):
    pass


def offline_guard():
    def refuse(*_a, **_k):
        raise OSError("network disabled by --offline")

    class NoSocket(socket.socket):
        def __init__(self, *a, **k):
            refuse()

    socket.socket = NoSocket
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def sha256_file(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, cid, ok, msg, **kw):
        self.rows.append({"id": cid, "ok": bool(ok), "msg": msg, **kw})

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]


def load_json(data, name):
    p = os.path.join(data, name)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        return json.loads(fh.read())


def check_sums(data, ck):
    p = os.path.join(data, "SHA256SUMS")
    if not os.path.exists(p):
        ck.add("sums", False, "SHA256SUMS missing")
        return
    listed, bad = {}, []
    with open(p) as fh:
        for line in fh:
            h, rel = line.rstrip("\n").split("  ", 1)
            listed[rel] = h
    present = set()
    for dp, _dn, fn in os.walk(data):
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), data)
            if rel != "SHA256SUMS":
                present.add(rel)
    for rel, h in sorted(listed.items()):
        fp = os.path.join(data, rel)
        if not os.path.exists(fp):
            bad.append(f"{rel}: missing")
        elif sha256_file(fp) != h:
            bad.append(f"{rel}: sha256 differs")
    bad += [f"{rel}: not listed" for rel in sorted(present - set(listed))]
    ck.add(
        "sums", not bad, "; ".join(bad[:5]) or f"{len(listed)} files, every sha256 matches", files=len(listed)
    )


def check_vl(data, ck, node, fetch):
    with open(os.path.join(data, "vl.json"), "rb") as fh:
        raw = fh.read()
    vl = json.loads(raw)
    d = L.decode_vl(vl)
    b = max(d["blobs"], key=lambda x: x["sequence"]) if d["blobs"] else None
    masters = sorted(k.upper() for k, _m in b["validators"]) if b else []
    pl = (node.get("validators", {}).get("publisher_lists") or [{}])[0]
    problems = []
    if L.verify_manifest(d["publisher"]) != (True, True):
        problems.append("publisher manifest signatures do not verify")
    if not b or not b["signature_ok"] or not b["low_s"]:
        problems.append("blob signature invalid or not low-S")
    if d["publisher"]["master"] != (vl.get("public_key") or "").upper():
        problems.append("public_key != publisher manifest master")
    if (pl.get("pubkey_publisher") or "").upper() != d["publisher"]["master"]:
        problems.append("node trusts a different publisher key")
    if b and pl.get("seq") != b["sequence"]:
        problems.append(f"node list seq {pl.get('seq')} != blob sequence {b['sequence']}")
    node_list = sorted(L.node_public_hex(k) for k in pl.get("list") or [])
    if node_list != masters:
        problems.append("node trusted list != VL validators")
    if not fetch or fetch.get("sha256") != hashlib.sha256(raw).hexdigest() or fetch.get("status") != 200:
        problems.append("vl-fetch.json does not match vl.json (sha256/status)")
    elif fetch.get("url") != C.vl_site(node):
        problems.append("VL was not fetched from the node's validator_list_sites uri")
    if (
        b
        and fetch
        and not ((b["effective"] or 0) + RIPPLE_EPOCH <= fetch["t"] < b["expiration"] + RIPPLE_EPOCH)
    ):
        problems.append("VL not valid at fetch time")
    ck.add(
        "vl",
        not problems,
        "; ".join(problems) or "publisher manifest, blob signature (low-S), node trust, fetch record agree",
        sequence=b["sequence"] if b else None,
        validators=len(masters),
        publisher=d["publisher"]["master"],
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    return [(k.upper(), L.parse_manifest(m)) for k, m in b["validators"]] if b else []


def check_manifests(ck, vals):
    bad = []
    for k, m in vals:
        if m["master"] != k or L.verify_manifest(m) != (True, True):
            bad.append(L.node_public_b58(k))
    ck.add("manifests", not bad, f"{len(vals) - len(bad)} of {len(vals)} VL manifests verify", failures=bad)


def load_fetch(data, domain, path):
    p = os.path.join(data, "toml", domain, C.cache_name(path))
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        rec = json.loads(fh.read())
    if "body_b64" in rec:
        rec["body"] = base64.b64decode(rec["body_b64"], validate=True)
    return rec


def classify_all(data, ck, vals):
    rows, problems = [], []
    for k, m in vals:
        dom = m["domain"]
        row = {"master": L.node_public_b58(k), "domain": dom, "manifest_seq": m["sequence"], "paths": {}}
        if not C.valid_domain(dom):
            row["class"] = D.NO_DOMAIN
            rows.append(row)
            continue
        got = []
        for p in D.TOML_PATHS:
            rec = load_fetch(data, dom, p)
            if rec is None:
                problems.append(f"{dom}{p}: no cached fetch")
                continue
            if rec.get("domain") != dom or rec.get("path") != p:
                problems.append(f"{dom}{p}: cache record names {rec.get('domain')}{rec.get('path')}")
                continue
            body = rec.get("body")
            if rec.get("outcome") == "response" and body is not None:
                if hashlib.sha256(body).hexdigest() != rec.get("sha256") or len(body) != rec.get("bytes"):
                    problems.append(f"{dom}{p}: body does not match its sha256/bytes")
                    continue
            try:
                cls, detail = D.classify_fetch(rec, k, dom)
            except ValueError as x:
                problems.append(f"{dom}{p}: {x}")
                continue
            row["paths"][p] = {"class": cls, **detail}
            got.append(cls)
        if len(got) != len(D.TOML_PATHS):
            row["class"] = None
        else:
            row["class"] = D.best(got)
        rows.append(row)
    ck.add(
        "toml",
        not problems,
        "; ".join(problems[:5])
        or f"{len(rows)} validators x {len(D.TOML_PATHS)} TOML paths cached and intact",
        problems=problems,
    )
    return rows


def check_crosscheck(data, ck, vals, rows):
    ans = {}
    p = os.path.join(data, "manifests.jsonl")
    for e in C.load_jsonl(p) if os.path.exists(p) else []:
        ans[(e["src"], e["master"])] = e
    problems = []
    by_master = {r["master"]: r for r in rows}
    for k, m in vals:
        row = by_master[L.node_public_b58(k)]
        for src in SRCS:
            e = ans.get((src, k))
            tag = f"{src} {L.node_public_b58(k)[:10]}"
            if e is None or "answer" not in e:
                problems.append(f"{tag}: no answer" + (f" ({e['error']})" if e else ""))
                row[src] = "missing"
                continue
            a = e["answer"]
            try:
                nm = L.parse_manifest(a.get("manifest") or "")
            except ValueError as x:
                problems.append(f"{tag}: manifest does not decode ({x})")
                row[src] = "bad"
                continue
            det = a.get("details") or {}
            if nm["master"] != k or L.verify_manifest(nm) != (True, True):
                problems.append(f"{tag}: manifest does not verify for this master")
                row[src] = "bad"
            elif det.get("domain") != nm["domain"] or det.get("seq") != nm["sequence"]:
                problems.append(f"{tag}: node's decoded details differ from the manifest blob")
                row[src] = "bad"
            elif nm["sequence"] < m["sequence"]:
                problems.append(f"{tag}: manifest seq {nm['sequence']} older than the VL's {m['sequence']}")
                row[src] = "older"
            elif nm["sequence"] == m["sequence"]:
                if nm["domain"] != m["domain"]:
                    problems.append(
                        f"{tag}: domain {nm['domain']!r} != VL {m['domain']!r} at seq {m['sequence']}"
                    )
                    row[src] = "domain_differs"
                else:
                    row[src] = "same"
            else:
                row[src] = "newer_same_domain" if nm["domain"] == m["domain"] else "newer_other_domain"
    n = sum(1 for r in rows for s in SRCS if r.get(s) in ("same", "newer_same_domain"))
    ck.add(
        "crosscheck",
        not problems,
        "; ".join(problems[:5])
        or f"{n} of {len(rows) * len(SRCS)} node/public manifest answers carry the VL domain",
        problems=problems,
    )


def verify(data):
    """-> (Checks, rows). Raises NoData when the directory holds no collection."""
    node = load_json(data, "node.json")
    if node is None or not os.path.exists(os.path.join(data, "vl.json")):
        raise NoData(f"{data}: node.json / vl.json missing")
    ck = Checks()
    check_sums(data, ck)
    vals = check_vl(data, ck, node, load_json(data, "vl-fetch.json"))
    check_manifests(ck, vals)
    rows = classify_all(data, ck, vals)
    check_crosscheck(data, ck, vals, rows)
    rows.sort(key=lambda r: (r["domain"] or "", r["master"]))
    return ck, rows


def counts(rows):
    out = {c: 0 for c in D.CLASSES + (D.NO_DOMAIN,)}
    for r in rows:
        if r["class"] is not None:
            out[r["class"]] += 1
    return out


def outputs(ck, rows):
    cnt = counts(rows)
    res = {"checks": ck.rows, "counts": cnt, "validators": len(rows), "rows": rows}
    buf = io.StringIO()
    cols = ["key_prefix", "domain", "class"] + [
        f"class{p.replace('/.well-known/', ' ')}" for p in D.TOML_PATHS
    ]
    cols += ["node_manifest", "public_manifest"]
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    md = [
        "| key prefix | domain | class | " + " | ".join(D.TOML_PATHS) + " | node manifest | public manifest |"
    ]
    md.append("|" + "---|" * (len(D.TOML_PATHS) + 5))
    for r in rows:
        per = [r["paths"].get(p, {}).get("class", "-") for p in D.TOML_PATHS]
        vals = [r["master"][:10], r["domain"] or "-", r["class"] or "-"] + per + [r.get(s, "-") for s in SRCS]
        w.writerow(vals)
        md.append("| " + " | ".join(f"`{v}`" if i == 0 else str(v) for i, v in enumerate(vals)) + " |")
    md.append("")
    md.append("| class | validators |")
    md.append("|---|---|")
    md += [f"| {c} | {n} |" for c, n in cnt.items()]
    md.append(f"| total | {len(rows)} |")
    return (
        (json.dumps(res, sort_keys=True, indent=1) + "\n").encode(),
        buf.getvalue().encode(),
        ("\n".join(md) + "\n").encode(),
    )


def run(data, out=None, quiet=False):
    try:
        ck, rows = verify(data)
    except NoData as x:
        print(f"NO DATA: {x}")
        return 2
    js, cs, md = outputs(ck, rows)
    if out:
        for name, b in (("results.json", js), ("validators.csv", cs), ("table.md", md)):
            C.write_atomic(os.path.join(out, name), b)
    if not quiet:
        for r in ck.rows:
            print(f"{'PASS' if r['ok'] else 'FAIL'} {r['id']}: {r['msg']}")
        cnt = counts(rows)
        print("classes: " + " ".join(f"{c}={n}" for c, n in cnt.items()) + f" total={len(rows)}")
    rc = 1 if ck.failed else 0
    print(f"RESULT {'OK' if rc == 0 else 'INCONSISTENT'} exit={rc}")
    return rc


# ---------------------------------------------------------------- negative controls (offline)


def _resum(d):
    C.write_sums(d)


def _first(rows, cls):
    return next((r for r in rows if r["class"] == cls), None)


def _toml_path(d, row, path):
    return os.path.join(d, "toml", row["domain"], C.cache_name(path))


def _edit_rec(fp, fn):
    with open(fp) as fh:
        rec = json.load(fh)
    fn(rec)
    C.write_atomic(fp, C.dump(rec))


def _set_body(rec, body):
    rec["body_b64"] = base64.b64encode(body).decode()
    rec["sha256"] = hashlib.sha256(body).hexdigest()
    rec["bytes"] = len(body)


def _parsed_fetch(rows):
    """-> (row, path) of the first cached 200 body (a fetch that got past http_error)."""
    for r in rows:
        for p in D.TOML_PATHS:
            if r["paths"].get(p, {}).get("class") not in (None, "unreachable", "http_error"):
                return r, p
    raise LookupError("no cached 200 body in this data")


def ctl_file_edit(d, rows):
    """A cached TOML record edited (body + its own sha256), SHA256SUMS not updated -> sums FAIL."""
    r, p = _parsed_fetch(rows)
    _edit_rec(_toml_path(d, r, p), lambda rec: _set_body(rec, b"# x\n" + base64.b64decode(rec["body_b64"])))
    return "sums", None


def ctl_body_hash(d, rows):
    """A cached body changed but its record sha256 kept (SHA256SUMS re-made) -> toml FAIL."""
    r, p = _parsed_fetch(rows)

    def edit(rec):
        rec["body_b64"] = base64.b64encode(b"# x\n" + base64.b64decode(rec["body_b64"])).decode()

    _edit_rec(_toml_path(d, r, p), edit)
    _resum(d)
    return "toml", None


def ctl_attestation_flip(d, rows):
    """One verified attestation with one hex digit changed (re-hashed) -> that validator bad_attestation."""
    r = _first(rows, "verified")
    if r is None:
        return None, "no verified validator in this data"
    for p in D.TOML_PATHS:
        if r["paths"][p]["class"] != "verified":
            continue

        def flip(rec):
            doc = D.parse_toml(base64.b64decode(rec["body_b64"]).decode())
            att = next(e["attestation"] for e in doc["VALIDATORS"] if e.get("public_key") == r["master"])
            new = att[:-1] + ("0" if att[-1] != "0" else "1")
            _set_body(rec, base64.b64decode(rec["body_b64"]).replace(att.encode(), new.encode()))

        _edit_rec(_toml_path(d, r, p), flip)
    _resum(d)
    return None, (r["master"], "bad_attestation")


def ctl_vl_blob(d, rows):
    """VL blob signature byte changed (re-hashed) -> vl FAIL."""
    fp = os.path.join(d, "vl.json")
    with open(fp) as fh:
        vl = json.load(fh)
    key = "blobs_v2" if vl.get("blobs_v2") else None
    tgt = vl[key][0] if key else vl
    s = tgt["signature"]
    tgt["signature"] = s[:-2] + ("00" if s[-2:] != "00" else "01")
    raw = json.dumps(vl).encode()
    C.write_atomic(fp, raw)
    _edit_rec(
        os.path.join(d, "vl-fetch.json"), lambda rec: rec.update(sha256=hashlib.sha256(raw).hexdigest())
    )
    _resum(d)
    return "vl", None


def ctl_public_domain(d, rows):
    """Public node's manifest answer claims another domain in details -> crosscheck FAIL."""
    fp = os.path.join(d, "manifests.jsonl")
    lines = C.load_jsonl(fp)
    e = next(e for e in lines if e["src"] == "public" and "answer" in e)
    e["answer"]["details"]["domain"] = "attacker.example"
    C.write_atomic(fp, "".join(json.dumps(x, sort_keys=True) + "\n" for x in lines).encode())
    _resum(d)
    return "crosscheck", None


def ctl_missing_fetch(d, rows):
    """One cached TOML fetch removed (re-hashed) -> toml FAIL (collection incomplete)."""
    r = next(r for r in rows if r["domain"] and r["class"] != D.NO_DOMAIN)
    os.remove(_toml_path(d, r, D.TOML_PATHS[0]))
    _resum(d)
    return "toml", None


def ctl_no_data(d, rows):
    """Empty directory -> exit 2."""
    for x in os.listdir(d):
        p = os.path.join(d, x)
        shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    return "nodata", None


CONTROLS = [
    ctl_file_edit,
    ctl_body_hash,
    ctl_attestation_flip,
    ctl_vl_blob,
    ctl_public_domain,
    ctl_missing_fetch,
    ctl_no_data,
]


def run_controls(data, scratch_root=None):
    base_ck, base_rows = verify(data)
    if base_ck.failed:
        print("BASELINE FAIL: controls need a passing data directory")
        return 1
    print(f"baseline: {len(base_rows)} validators, all checks PASS")
    fails = 0
    for fn in CONTROLS:
        tmp = tempfile.mkdtemp(prefix="valdomain-ctl-", dir=scratch_root)
        try:
            d = os.path.join(tmp, "data")
            shutil.copytree(data, d)
            want_fail, want_class = fn(d, base_rows)
            if want_fail is None and want_class is None:
                print(f"SKIP {fn.__name__}: n/a")
                continue
            if isinstance(want_class, str):
                print(f"FAIL {fn.__name__}: cannot run ({want_class})")
                fails += 1
                continue
            if want_fail == "nodata":
                try:
                    verify(d)
                    got = "verified without data"
                except NoData:
                    got = "nodata"
                ok = got == "nodata"
                print(f"{'PASS' if ok else 'FAIL'} {fn.__name__}: {fn.__doc__.strip()} -> {got}")
            elif want_fail:
                ck, _rows = verify(d)
                ids = [r["id"] for r in ck.failed]
                ok = ids == [want_fail]
                why = next((r["msg"] for r in ck.failed), "")
                tag = f"{'PASS' if ok else 'FAIL'} {fn.__name__}: {fn.__doc__.strip()}"
                print(f"{tag} -> failed {ids}: {why[:100]}")
            else:
                ck, rows = verify(d)
                master, cls = want_class
                got = next(r["class"] for r in rows if r["master"] == master)
                ok = not ck.failed and got == cls
                print(
                    f"{'PASS' if ok else 'FAIL'} {fn.__name__}: {fn.__doc__.strip()} -> {master[:10]} {got}"
                )
            fails += 0 if ok else 1
        finally:
            shutil.rmtree(tmp)
    print(f"RESULT controls {len(CONTROLS) - fails}/{len(CONTROLS)} PASS exit={1 if fails else 0}")
    return 1 if fails else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ctl = bool(argv) and argv[0] == "controls"
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true", required=True, help="read the cache only, network off")
    ap.add_argument("data")
    ap.add_argument("--out", default=None)
    ap.add_argument("--scratch", default=None, help="parent dir for control copies (controls only)")
    a = ap.parse_args(argv[1:] if ctl else argv)
    offline_guard()
    if ctl:
        return run_controls(a.data, a.scratch)
    return run(a.data, a.out)


if __name__ == "__main__":
    sys.exit(main())
