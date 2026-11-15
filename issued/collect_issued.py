#!/usr/bin/env python3
"""Read-only issued-asset (IOU) and trust-line snapshot of ONE pinned validated ledger (stdlib only).

  snapshot  pins one validated ledger (index + hash) on your own node and, all on that ledger hash:
            1. pages the FULL state tree with `ledger_data` (binary=false) and counts every
               LedgerEntryType;
            2. pages `ledger_data type=state` (RippleState) and `type=amm` a second time and
               refuses any count or index-set difference against the full tree (counts.json);
            3. keeps every RippleState and AMM object and the AccountRoot of every account that
               is a party to a trust line (issuer settings: flags, TransferRate, TickSize, Domain);
            4. asks `gateway_balances` for every trust-line party and `amm_info` for every
               (issuer, currency) paired with the native asset, and keeps the raw answers;
            5. writes deterministic files (sorted JSON keys, objects sorted by index/account),
               fsyncs them, then writes MANIFEST.json (ledger index/hash, rows and sha256 of
               every file) last. A run without MANIFEST.json is incomplete.

Only read-only RPC methods are sent (server_state, ledger, ledger_data, gateway_balances,
amm_info). Nothing is signed or submitted. Transient failures (docker/curl errors, timeouts,
unparsable answers, tooBusy, noNetwork) are retried with exponential backoff; every retry is
logged as a RETRY line and a call that never answers stops the run (exit 2).

Usage:
  ./collect_issued.py snapshot --out data/issued/A [--ledger N] [--log FILE]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

FORMAT = "pft-issued-snapshot/1"
READ_ONLY = {
    "server_state",
    "server_info",
    "ledger",
    "ledger_data",
    "gateway_balances",
    "amm_info",
    "account_lines",
}
TRANSIENT = {"tooBusy", "noNetwork", "noCurrent", "noClosed", "slowDown"}
NATIVE = {"currency": "XRP"}  # rippled API name of the native asset (PFT on Post Fiat)
GB_FIELDS = ("obligations", "balances", "assets", "frozen_balances", "locked")
FILES = (
    "header.json",
    "counts.json",
    "ripplestate.jsonl",
    "accounts.jsonl",
    "amm.jsonl",
    "gateway_balances.jsonl",
    "amm_info.jsonl",
)


class GaveUp(Exception):
    pass


class RpcError(Exception):
    """A definite (non-transient) RPC error answer, e.g. actNotFound."""

    def __init__(self, error, result):
        super().__init__(error)
        self.error = error
        self.result = result


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def docker_transport(container="postfiatd", port=5005):
    url = f"http://127.0.0.1:{port}/"

    def send(body):
        cmd = [
            "sudo",
            "docker",
            "exec",
            container,
            "curl",
            "-s",
            "-m",
            "30",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            url,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            raise OSError(f"docker exec rc={out.returncode}: {out.stderr.strip()[:200]}")
        return out.stdout

    return send


class Rpc:
    """JSON-RPC client: read-only allowlist, logged exponential backoff on transient failures."""

    def __init__(self, send, log, retries=5, backoff=1.0, sleep=time.sleep):
        self.send, self.log, self.retries, self.backoff, self.sleep = send, log, retries, backoff, sleep
        self.calls = 0
        self.retried = 0

    def call(self, method, params):
        if method not in READ_ONLY:
            raise ValueError(f"refusing non read-only method {method}")
        body = json.dumps({"method": method, "params": [params]}, sort_keys=True)
        for attempt in range(self.retries + 1):
            self.calls += 1
            try:
                result = json.loads(self.send(body))["result"]
                err = result.get("error") if result.get("status") == "error" else None
                if err is None:
                    return result
                if err not in TRANSIENT:
                    raise RpcError(err, result)
                reason = f"error={err}"
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                reason = f"{type(e).__name__}: {str(e)[:120]}"
            if attempt == self.retries:
                raise GaveUp(f"{method} gave up after {attempt + 1} tries ({reason})")
            delay = self.backoff * (2**attempt)
            self.retried += 1
            self.log(f"RETRY {method} try {attempt + 1}/{self.retries + 1} {reason} sleep={delay:g}s")
            self.sleep(delay)
        raise AssertionError("unreachable")


def page(rpc, ledger_hash, typ=None, limit=2048):
    """All objects of one ledger (optionally one type), with the number of pages."""
    objs, marker, pages = [], None, 0
    while True:
        p = {"ledger_hash": ledger_hash, "binary": False, "limit": limit}
        if typ:
            p["type"] = typ
        if marker is not None:
            p["marker"] = marker
        r = rpc.call("ledger_data", p)
        if r.get("ledger_hash") != ledger_hash:
            raise ValueError(f"ledger_data answered for {r.get('ledger_hash')}, pinned {ledger_hash}")
        objs.extend(r.get("state", []))
        pages += 1
        marker = r.get("marker")
        if marker is None:
            return objs, pages


def parties(line):
    return line["LowLimit"]["issuer"], line["HighLimit"]["issuer"]


def dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def jsonl(rows):
    return "".join(dumps(r) + "\n" for r in rows)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def snapshot(rpc, out, ledger=None, log=print):
    os.makedirs(out, exist_ok=True)
    if os.path.exists(os.path.join(out, "MANIFEST.json")):
        raise SystemExit(f"{out} already holds a complete snapshot (MANIFEST.json); use a new --out")
    if ledger is None:
        st = rpc.call("server_state", {})["state"]
        if st.get("server_state") not in ("full", "validating", "proposing"):
            raise SystemExit(f"node not synced: server_state={st.get('server_state')}")
        ledger = st["validated_ledger"]["seq"]
    hdr = rpc.call("ledger", {"ledger_index": int(ledger)})
    if not hdr.get("validated"):
        raise SystemExit(f"ledger {ledger} is not validated on this node")
    led = hdr["ledger"]
    lh = led["ledger_hash"]
    log(f"PIN ledger_index={led['ledger_index']} ledger_hash={lh} close_time_iso={led.get('close_time_iso')}")

    full, full_pages = page(rpc, lh)
    by_type = {}
    for o in full:
        by_type[o["LedgerEntryType"]] = by_type.get(o["LedgerEntryType"], 0) + 1
    log(f"FULL objects={len(full)} pages={full_pages} types={dumps(dict(sorted(by_type.items())))}")

    counts = {
        "full_tree_objects": len(full),
        "full_tree_by_type": dict(sorted(by_type.items())),
        "filtered": {},
    }
    kept = {}
    ok = True
    for typ, name in (("state", "RippleState"), ("amm", "AMM")):
        filt, pages = page(rpc, lh, typ)
        a = sorted(o["index"] for o in full if o["LedgerEntryType"] == name)
        b = sorted(o["index"] for o in filt)
        same = a == b and all(o["LedgerEntryType"] == name for o in filt)
        ok &= same
        counts["filtered"][name] = {
            "rpc_type": typ,
            "full_tree": len(a),
            "type_filter": len(b),
            "pages": pages,
            "index_sets_equal": same,
        }
        log(f"COUNT {name} full_tree={len(a)} type={typ} filtered={len(b)} index_sets_equal={same}")
        kept[name] = sorted((o for o in full if o["LedgerEntryType"] == name), key=lambda o: o["index"])
    if not ok:
        raise SystemExit("full-tree and type-filtered object sets differ: refusing snapshot")

    lines = kept["RippleState"]
    accts = sorted({a for ln in lines for a in parties(ln)})
    roots = {o["Account"]: o for o in full if o["LedgerEntryType"] == "AccountRoot"}
    missing = [a for a in accts if a not in roots]
    if missing:
        raise SystemExit(f"trust-line parties without AccountRoot in the tree: {missing}")

    gbs = []
    for a in accts:
        r = rpc.call("gateway_balances", {"account": a, "ledger_hash": lh})
        if r.get("ledger_hash") != lh:
            raise ValueError(f"gateway_balances {a} answered for {r.get('ledger_hash')}")
        gbs.append({"account": a, **{k: r[k] for k in GB_FIELDS if k in r}})
    log(f"GATEWAY_BALANCES accounts={len(gbs)} with_obligations={sum(1 for g in gbs if 'obligations' in g)}")

    pairs = sorted({(ln["Balance"]["currency"], i) for ln in lines for i in parties(ln)})
    issuers = {g["account"] for g in gbs if "obligations" in g}
    amm_rows = []
    for cur, iss in pairs:
        if iss not in issuers:
            continue
        q = {"asset": NATIVE, "asset2": {"currency": cur, "issuer": iss}, "ledger_hash": lh}
        try:
            r = rpc.call("amm_info", q)
            amm_rows.append({"query": q, "amm": r.get("amm")})
        except RpcError as e:
            amm_rows.append({"query": q, "error": e.error})
    log(f"AMM_INFO queries={len(amm_rows)} found={sum(1 for r in amm_rows if 'amm' in r)}")

    header = {
        "format": FORMAT,
        "ledger_index": int(led["ledger_index"]),
        "ledger_hash": lh,
        "account_hash": led.get("account_hash"),
        "close_time": led.get("close_time"),
        "close_time_iso": led.get("close_time_iso"),
        "parent_hash": led.get("parent_hash"),
    }
    texts = {
        "header.json": dumps(header) + "\n",
        "counts.json": dumps(counts) + "\n",
        "ripplestate.jsonl": jsonl(lines),
        "accounts.jsonl": jsonl(roots[a] for a in accts),
        "amm.jsonl": jsonl(kept["AMM"]),
        "gateway_balances.jsonl": jsonl(gbs),
        "amm_info.jsonl": jsonl(amm_rows),
    }
    for name in FILES:
        write_file(os.path.join(out, name), texts[name])
    manifest = {
        "format": FORMAT,
        "ledger_index": header["ledger_index"],
        "ledger_hash": lh,
        "files": {n: {"rows": texts[n].count("\n"), "sha256": sha256(os.path.join(out, n))} for n in FILES},
        "rpc_calls": rpc.calls,
        "retries": rpc.retried,
    }
    write_file(os.path.join(out, "MANIFEST.json"), json.dumps(manifest, sort_keys=True, indent=1) + "\n")
    for n in FILES:
        log(f"FILE {n} rows={manifest['files'][n]['rows']} sha256={manifest['files'][n]['sha256']}")
    log(
        f"DONE ledger_index={header['ledger_index']} lines={len(lines)} accounts={len(accts)} "
        f"rpc_calls={rpc.calls} retries={rpc.retried}"
    )
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", required=True)
    s.add_argument("--ledger", type=int)
    s.add_argument("--container", default="postfiatd")
    s.add_argument("--retries", type=int, default=5)
    s.add_argument("--backoff", type=float, default=1.0)
    s.add_argument("--log")
    a = ap.parse_args(argv)
    fh = open(a.log, "a", encoding="utf-8") if a.log else None

    def log(msg):
        line = f"{utcnow()} {msg}"
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")
            fh.flush()

    rpc = Rpc(docker_transport(a.container), log, a.retries, a.backoff)
    try:
        snapshot(rpc, a.out, a.ledger, log)
    except GaveUp as e:
        log(f"GAVE_UP {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
