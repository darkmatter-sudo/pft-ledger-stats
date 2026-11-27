#!/usr/bin/env python3
"""Read-only collector for the valdomain audit (Python 3 stdlib only). Two subcommands:

`node --out DIR` (run inside the postfiatd container's network namespace by run-node.sh, as an
unprivileged user): asks your own node's admin JSON-RPC (container loopback 127.0.0.1:5005) for
`validators`, `validator_list_sites` and `server_info` and writes DIR/node.json. Read-only commands.

`collect --out DIR` (on the host): resumable; every step skips what DIR already holds.
  1. VL: GETs the site the node fetches its list from (node.json validator_list_sites) once and saves
     the raw bytes as DIR/vl.json + DIR/vl-fetch.json (url, time, HTTP status, sha256).
  2. Manifests: one `manifest` request per VL master key to your node (--node-rpc) and one to an
     independent public node (--public-rpc), saved to DIR/manifests.jsonl (retries print).
  3. TOML: for each distinct domain in the VL manifests, ONE GET per path in lib_valdomain.TOML_PATHS
     (https only, --timeout s, body capped at --max-bytes, <= 3 https redirects, no retries; hosts that
     resolve to a non-public address are not contacted). Cached as DIR/toml/<domain>/<name>.json.
  4. DIR/SHA256SUMS over every data file.

Published cache records hold only what classification needs (status, content type, size, sha256,
the body of 200 answers). Low-level failure reasons (DNS, TLS, connect) can describe a third party's
host weaknesses, so they go only to --private FILE (keep it out of the repo).
Exit codes: 0 done, 2 node.json missing.
"""

import argparse
import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import lib_valdomain as D  # noqa: E402

L = D.L
UA = "pft-ledger-stats-valdomain/1 (read-only domain attestation audit)"
VL_URL = "https://postfiat.org/testnet_vl.json"
PUBLIC_RPC = "https://rpc.testnet.postfiat.org/"
NODE_RPC = "http://127.0.0.1:5005/"
ADMIN_COMMANDS = ("validators", "validator_list_sites", "server_info")
HOST_RE = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
MAX_REDIRECTS = 3


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def dump(obj):
    return (json.dumps(obj, sort_keys=True, indent=1) + "\n").encode()


def rpc_call(url, method, params=None, timeout=20, opener=urllib.request.urlopen):
    body = json.dumps({"method": method, "params": [params or {}]}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA}
    )
    with opener(req, timeout=timeout) as r:
        return json.loads(r.read())["result"]


def valid_domain(domain):
    return isinstance(domain, str) and HOST_RE.fullmatch(domain) is not None


def public_host(host, resolver=socket.getaddrinfo):
    """-> (ok, reason). ok only if every address the name resolves to is a global unicast address."""
    try:
        infos = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as x:
        return False, f"dns: {x}"
    addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    if not addrs:
        return False, "dns: no address"
    if any(not a.is_global or a.is_multicast for a in addrs):
        return False, "resolves to a non-public address"
    return True, ""


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, checker):
        super().__init__()
        self.checker = checker
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or not u.hostname:
            raise urllib.error.URLError(f"redirect to non-https {u.scheme}")
        if len(self.hops) >= MAX_REDIRECTS:
            raise urllib.error.URLError("too many redirects")
        ok, why = self.checker(u.hostname)
        if not ok:
            raise urllib.error.URLError(f"redirect target: {why}")
        self.hops.append({"status": code, "to": newurl})
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_capped(r, cap):
    data = r.read(cap + 1)
    return data[:cap], len(data) > cap


def fetch_toml(domain, path, timeout, cap, checker=public_host, build_opener=urllib.request.build_opener):
    """ONE GET of https://domain/path -> (published record, private reason or None)."""
    url = f"https://{domain}{path}"
    rec = {"domain": domain, "path": path, "url": url, "t": round(time.time(), 3)}
    ok, why = checker(domain)
    if not ok:
        rec.update(outcome="unreachable")
        return rec, why
    red = _Redirects(checker)
    opener = build_opener(red, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/plain, */*"})
    try:
        with opener.open(req, timeout=timeout) as r:
            body, cut = _read_capped(r, cap)
            status, ctype, final = r.status, r.headers.get("Content-Type"), r.geturl()
    except urllib.error.HTTPError as e:
        try:
            body, cut = _read_capped(e, cap)
        except (OSError, http.client.HTTPException):
            body, cut = b"", False
        status, ctype, final = e.code, e.headers.get("Content-Type") if e.headers else None, e.geturl()
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as x:
        rec.update(outcome="unreachable", redirects=red.hops)
        return rec, f"{type(x).__name__}: {getattr(x, 'reason', x)}"
    rec.update(
        outcome="response",
        status=status,
        content_type=ctype,
        redirects=red.hops,
        final_url=final if final != url else None,
        bytes=len(body),
        truncated=cut,
        sha256=sha256_bytes(body),
    )
    if status == 200 and not cut:
        rec["body_b64"] = base64.b64encode(body).decode()
    return rec, None


def cache_name(path):
    return os.path.basename(path).replace(".toml", "") + ".json"


def load_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "rb") as fh:
        for raw in fh:
            if raw.endswith(b"\n"):
                out.append(json.loads(raw))
    return out


def write_sums(out):
    rows = []
    for dp, _dn, fn in os.walk(out):
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, out)
            if rel == "SHA256SUMS" or rel.endswith(".tmp"):
                continue
            with open(p, "rb") as fh:
                rows.append(f"{sha256_bytes(fh.read())}  {rel}")
    write_atomic(
        os.path.join(out, "SHA256SUMS"), ("\n".join(sorted(rows, key=lambda r: r[66:])) + "\n").encode()
    )
    return len(rows)


def say(msg):
    print(f"{iso(time.time())} {msg}", flush=True)


def cmd_node(a, call=rpc_call):
    got = {}
    for m in ADMIN_COMMANDS:
        res = call(a.rpc, m)
        if m == "server_info":
            info = res.get("info", {})
            res = {
                k: info.get(k)
                for k in ("build_version", "server_state", "validator_list", "validated_ledger")
            }
        got[m] = res
    got["t"] = round(time.time(), 3)
    got["rpc"] = a.rpc
    write_atomic(os.path.join(a.out, "node.json"), dump(got))
    pl = (got["validators"].get("publisher_lists") or [{}])[0]
    n = len(pl.get("list") or [])
    say(f"node.json: publisher {pl.get('pubkey_publisher')} seq {pl.get('seq')} list {n}")
    return 0


def vl_site(node):
    sites = (node.get("validator_list_sites") or {}).get("validator_sites") or []
    uris = [s.get("uri") for s in sites if s.get("uri")]
    return uris[0] if len(uris) == 1 else None


def step_vl(a, node, opener=urllib.request.urlopen):
    dst = os.path.join(a.out, "vl.json")
    if os.path.exists(dst):
        say("vl.json cached")
        return
    url = a.vl_url or vl_site(node)
    if not url:
        raise SystemExit("node.json does not name exactly one validator_list_sites uri; pass --vl-url")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener(req, timeout=a.timeout) as r:
        raw, status = r.read(), r.status
    write_atomic(dst, raw)
    rec = {
        "url": url,
        "t": round(time.time(), 3),
        "status": status,
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
    }
    write_atomic(os.path.join(a.out, "vl-fetch.json"), dump(rec))
    say(f"vl.json: {url} HTTP {status} {len(raw)} B sha256 {rec['sha256']}")


def vl_validators(out):
    with open(os.path.join(out, "vl.json"), "rb") as fh:
        d = L.decode_vl(json.loads(fh.read()))
    b = max(d["blobs"], key=lambda x: x["sequence"])
    return d, b, [(k.upper(), L.parse_manifest(m)) for k, m in b["validators"]]


def step_manifests(a, masters, call=rpc_call, sleep=time.sleep):
    path = os.path.join(a.out, "manifests.jsonl")
    have = {(e["src"], e["master"]) for e in load_jsonl(path)}
    n = 0
    with open(path, "a") as fh:
        for src, url in (("node", a.node_rpc), ("public", a.public_rpc)):
            for mk in masters:
                if (src, mk) in have:
                    continue
                key = L.node_public_b58(mk)
                ans, err = None, None
                for attempt in range(1, 4):
                    try:
                        ans = call(url, "manifest", {"public_key": key})
                        err = None
                        break
                    except (OSError, ValueError, KeyError, http.client.HTTPException) as x:
                        err = f"{type(x).__name__}: {x}"
                        say(f"manifest {src} {key[:8]} attempt {attempt} failed: {err}")
                        sleep(2 * attempt)
                e = {"src": src, "master": mk, "t": round(time.time(), 3)}
                if ans is not None:
                    e["answer"] = {
                        k: ans.get(k) for k in ("details", "manifest", "requested", "status", "error")
                    }
                else:
                    e["error"] = err
                fh.write(json.dumps(e, sort_keys=True) + "\n")
                fh.flush()
                n += 1
    say(f"manifests.jsonl: {n} new answers ({len(have)} cached)")


def step_toml(a, domains, fetch=fetch_toml):
    new = cached = 0
    priv = open(a.private, "a") if a.private else None
    try:
        for dom in sorted(domains):
            if not valid_domain(dom):
                say(f"{dom!r}: not a valid DNS name, not fetched")
                continue
            for p in D.TOML_PATHS:
                dst = os.path.join(a.out, "toml", dom, cache_name(p))
                if os.path.exists(dst):
                    cached += 1
                    continue
                rec, why = fetch(dom, p, a.timeout, a.max_bytes)
                write_atomic(dst, dump(rec))
                new += 1
                if why and priv:
                    priv.write(json.dumps({"domain": dom, "path": p, "t": rec["t"], "reason": why}) + "\n")
                    priv.flush()
                say(
                    f"GET https://{dom}{p} -> {rec['outcome']}"
                    + (f" HTTP {rec['status']} {rec['bytes']} B" if rec["outcome"] == "response" else "")
                    + (f" redirects={len(rec['redirects'])}" if rec.get("redirects") else "")
                )
    finally:
        if priv:
            priv.close()
    say(f"toml: {new} fetched, {cached} cached")


def cmd_collect(a):
    np = os.path.join(a.out, "node.json")
    if not os.path.exists(np):
        print(f"{np} missing: run run-node.sh first", file=sys.stderr)
        return 2
    with open(np) as fh:
        node = json.load(fh)
    step_vl(a, node)
    _d, b, vals = vl_validators(a.out)
    n = sum(1 for _k, m in vals if m["domain"])
    say(f"VL sequence {b['sequence']}: {len(vals)} validators, {n} with sfDomain")
    step_manifests(a, [k for k, _m in vals])
    step_toml(a, {m["domain"] for _k, m in vals if m["domain"]})
    say(f"SHA256SUMS: {write_sums(a.out)} files")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("node", help="admin read of your own node (inside its network namespace)")
    n.add_argument("--out", required=True)
    n.add_argument("--rpc", default=NODE_RPC)
    c = sub.add_parser("collect", help="VL, manifests and attestation TOML files (on the host)")
    c.add_argument("--out", required=True)
    c.add_argument("--vl-url", default=None)
    c.add_argument("--node-rpc", default=NODE_RPC)
    c.add_argument("--public-rpc", default=PUBLIC_RPC)
    c.add_argument("--timeout", type=float, default=15.0)
    c.add_argument("--max-bytes", type=int, default=65536)
    c.add_argument("--private", default=None, help="append low-level failure reasons here (never publish)")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    return cmd_node(a) if a.cmd == "node" else cmd_collect(a)


if __name__ == "__main__":
    sys.exit(main())
