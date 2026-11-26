#!/usr/bin/env python3
"""Resumable, read-only Dynamic UNL round collector (Python 3 stdlib only).

Fetches, from public endpoints only:
  * the scoring service's public API (default https://scoring-testnet.postfiat.org):
    /api/scoring/rounds (all pages), per round: /rounds/N, /rounds/N/convergence, the audit-trail
    bundle /rounds/N/bundle.json and every file it lists in file_hashes (or, for early rounds
    without a bundle, the legacy unl.json / vl.json), plus /vl.json, /api/scoring/config,
    /api/scoring/health, /api/scoring/unl/current, /api/scoring/convergence/current,
  * the mirrored VL (default https://postfiat.org/testnet_vl.json),
  * every transaction those documents name (round memo_tx_hash, convergence anchor_tx_hash,
    participants' accepted commit / reveal txs) from a public JSON-RPC (methods tx, server_info, ledger).

Cache: --out/cache.jsonl, append-only, fsynced, one JSON object per response:
  {key, url, t (UTC fetch time), status (HTTP code, or 0 for a transport error after all
   retries), sha256 (of the raw body bytes), bytes, body (utf-8 text)}
Immutable keys (round documents, sealed convergence, txs) are fetched once; a key is final once
it has a 200 or 404. "Live" keys (round list, current VL, mirror, config, health, current UNL /
convergence) are prefixed with --epoch (default: today's UTC date) and fetched once per epoch.
A torn last line (kill during a write) is cut on start. Requests are spaced by --gap seconds and
retried with exponential backoff on 429/5xx/transport errors.

`pack` writes the committed, deterministic subset (gzip mtime 0): bodies needed by the audit are
kept; bodies of large evidence/raw inputs (which also hold third-party network details) are
replaced by their canonical content hash, which is what bundle file_hashes covers; the public
RPC's server_info is reduced to complete_ledgers (`trimmed`). `sha256` always hashes the raw body.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_dunl as L  # noqa: E402

API = "https://scoring-testnet.postfiat.org"
MIRROR = "https://postfiat.org/testnet_vl.json"
RPC = "https://rpc.testnet.postfiat.org"
LEGACY_FILES = ("unl.json", "vl.json")
LIVE_PATHS = {
    "vl": "/vl.json",
    "config": "/api/scoring/config",
    "health": "/api/scoring/health",
    "unl_current": "/api/scoring/unl/current",
    "convergence_current": "/api/scoring/convergence/current",
}
# bodies kept in the committed pack (everything else -> content hash only)
KEEP_FILES = {
    "bundle.json",
    "unl.json",
    "vl.json",
    "inputs/previous_unl.json",
    "outputs/final_scores.json",
    "outputs/selected_unl.json",
    "outputs/signed_validator_list.json",
    "outputs/verification_hashes.json",
    "runtime/execution_manifest.json",
}
RETRY_HTTP = {429, 500, 502, 503, 504}
UA = "pft-ledger-stats-dunl/1 (read-only audit; github.com/darkmatter-sudo/pft-ledger-stats)"


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"{utcnow()} {msg}", flush=True)


class Cache:
    def __init__(self, path):
        self.path = path
        self.rows = {}
        self.torn = 0
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read()
            good = data
            if data and not data.endswith(b"\n"):
                good = data[: data.rfind(b"\n") + 1]
                self.torn = len(data) - len(good)
                with open(path, "wb") as fh:
                    fh.write(good)
            for line in good.splitlines():
                row = json.loads(line)
                self.rows[row["key"]] = row
        self.fh = open(path, "a", encoding="utf-8")

    def final(self, key):
        row = self.rows.get(key)
        return row is not None and row["status"] in (200, 404) and not row.get("retry")

    def get(self, key):
        row = self.rows.get(key)
        return row if row and row["status"] == 200 else None

    def put(self, row):
        self.fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.rows[row["key"]] = row


class Fetcher:
    def __init__(self, cache, gap=0.5, retries=5, backoff=2.0, opener=None, sleep=None):
        self.cache, self.gap, self.retries, self.backoff = cache, gap, retries, backoff
        self.opener = opener or self._open
        self.sleep = sleep or time.sleep
        self.last = 0.0
        self.calls = self.retried = self.cached = self.errors = 0

    @staticmethod
    def _open(url, body):
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def fetch(self, key, url, body=None, retry_if=None):
        """retry_if(parsed_json) -> True marks a 200 as not final (refetched on the next run)."""
        if self.cache.final(key):
            self.cached += 1
            return self.cache.rows[key]
        status, raw = 0, b""
        for attempt in range(self.retries + 1):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                self.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            try:
                status, raw = self.opener(url, body)
                err = None
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, raw, err = 0, b"", f"{type(e).__name__}: {e}"
            if status not in RETRY_HTTP and status != 0:
                break
            if attempt < self.retries:
                self.retried += 1
                delay = self.backoff * (2**attempt)
                log(f"retry {attempt + 1}/{self.retries} {key} status={status} {err or ''} in {delay:.0f}s")
                self.sleep(delay)
        if status not in (200, 404):
            self.errors += 1
        row = {
            "key": key,
            "url": url,
            "t": utcnow(),
            "status": status,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "body": raw.decode("utf-8", "replace"),
        }
        if retry_if and status == 200 and retry_if(body_json(row)):
            row["retry"] = True
        self.cache.put(row)
        return row


def body_json(row):
    if not row or row["status"] != 200:
        return None
    try:
        return json.loads(row["body"])
    except ValueError:
        return None


def rpc_tx(fetcher, rpc, tx_hash):
    body = json.dumps({"method": "tx", "params": [{"transaction": tx_hash, "binary": False}]}).encode()
    # a not-yet-validated tx or a transient RPC error is re-fetched on the next run
    row = fetcher.fetch(f"tx/{tx_hash}", rpc, body, retry_if=_tx_not_final)
    return (body_json(row) or {}).get("result", {})


def _tx_not_final(j):
    res = (j or {}).get("result", {})
    if res.get("error") == "txnNotFound":
        return False
    return res.get("status") != "success" or not res.get("validated")


def _rpc_error(j):
    return (j or {}).get("result", {}).get("status") != "success"


def _conv_live(j):
    """A convergence view that is still a live tally (not sealed, not not_tracked)."""
    return bool(j) and not j.get("finalized") and j.get("phase") != "not_tracked"


def collect(args, fetcher):
    api = args.api.rstrip("/")
    ep = args.epoch
    rounds = []
    offset = 0
    while True:
        row = fetcher.fetch(f"{ep}/rounds/{offset}", f"{api}/api/scoring/rounds?limit=100&offset={offset}")
        page = body_json(row)
        if not page:
            raise SystemExit(f"round list failed at offset {offset}: status {row['status']}")
        rounds += page["rounds"]
        offset += len(page["rounds"])
        if not page["rounds"] or offset >= page["total"]:
            break
    log(f"round list: {len(rounds)} rounds (total {page['total']})")
    for name, path in sorted(LIVE_PATHS.items()):
        fetcher.fetch(f"{ep}/{name}", api + path)
    fetcher.fetch(f"{ep}/mirror_vl", args.mirror)

    txs = []
    for r in sorted(rounds, key=lambda x: x["round_number"]):
        n = r["round_number"]
        fetcher.fetch(f"round/{n}/detail", f"{api}/api/scoring/rounds/{r['id']}")
        conv = body_json(
            fetcher.fetch(
                f"round/{n}/convergence", f"{api}/api/scoring/rounds/{n}/convergence", retry_if=_conv_live
            )
        )
        bundle = body_json(
            fetcher.fetch(f"round/{n}/file/bundle.json", f"{api}/api/scoring/rounds/{n}/bundle.json")
        )
        files = sorted((bundle or {}).get("file_hashes", {})) if bundle else list(LEGACY_FILES)
        for f in files:
            fetcher.fetch(f"round/{n}/file/{f}", f"{api}/api/scoring/rounds/{n}/{urllib.parse.quote(f)}")
        if r.get("memo_tx_hash"):
            txs.append(r["memo_tx_hash"])
        if conv:
            if conv.get("anchor_tx_hash"):
                txs.append(conv["anchor_tx_hash"])
            for p in (conv.get("report") or {}).get("participants", []):
                txs += [p[k] for k in ("accepted_commit_tx", "accepted_reveal_tx") if p.get(k)]
        log(f"round {n}: status={r['status']} files={len(files)} calls={fetcher.calls} hit={fetcher.cached}")
    # the public RPC keeps a limited history: record its range and the close time of its first
    # ledger so the audit can tell a pruned tx (unverifiable) from a missing one
    info = body_json(
        fetcher.fetch(f"{ep}/rpc_server_info", args.rpc, json.dumps({"method": "server_info"}).encode())
    )
    cl = ((info or {}).get("result", {}).get("info", {}) or {}).get("complete_ledgers", "")
    first = cl.split(",")[0].split("-")[0] if cl and cl != "empty" else ""
    if first.isdigit():
        # the very first retained ledger can be pruned between the two calls (lgrNotFound): step
        # forward a little; a later boundary only widens the "pruned" window by minutes
        for step in (0, 16, 256, 1024):
            q = {"method": "ledger", "params": [{"ledger_index": int(first) + step, "transactions": False}]}
            row = fetcher.fetch(
                f"{ep}/rpc_first_ledger", args.rpc, json.dumps(q).encode(), retry_if=_rpc_error
            )
            if not row.get("retry"):
                break
    log(f"rpc complete_ledgers {cl or '?'}")
    calls0 = fetcher.calls
    for i, h in enumerate(dict.fromkeys(txs)):
        rpc_tx(fetcher, args.rpc, h)
        if (i + 1) % 50 == 0 and fetcher.calls > calls0:
            log(f"txs {i + 1}/{len(set(txs))} calls={fetcher.calls}")
    log(f"txs: {len(set(txs))} unique")
    return rounds


def pack(cache_path, out_path):
    """Deterministic committed subset of the cache (latest row per key, sorted by key)."""
    rows = Cache(cache_path).rows
    kept = trimmed = 0
    lines = []
    for key in sorted(rows):
        row = dict(rows[key])
        if row["status"] not in (200, 404) or row.get("retry"):
            continue
        parts = key.split("/", 3)
        if key.endswith("/rpc_server_info") and row["status"] == 200:
            # the audit needs only complete_ledgers; drop the public server's host details
            info = json.loads(row["body"]).get("result", {}).get("info", {})
            row["body"] = json.dumps({"result": {"info": {"complete_ledgers": info.get("complete_ledgers")}}})
            row["trimmed"] = True
            kept += 1
        elif (
            parts[0] == "round" and parts[2] == "file" and parts[3] not in KEEP_FILES and row["status"] == 200
        ):
            row["content_hash"] = L.content_hash(json.loads(row["body"]))
            row["body"] = None
            trimmed += 1
        else:
            kept += 1
        lines.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    data = ("\n".join(lines) + "\n").encode()
    with open(out_path + ".tmp", "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(data)
    os.replace(out_path + ".tmp", out_path)
    log(f"pack: {len(lines)} rows ({kept} with body, {trimmed} hash-only) -> {out_path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--out", default="data/dunl")
    c.add_argument("--api", default=API)
    c.add_argument("--mirror", default=MIRROR)
    c.add_argument("--rpc", default=RPC)
    c.add_argument("--epoch", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    c.add_argument("--gap", type=float, default=0.5)
    p = sub.add_parser("pack")
    p.add_argument("--cache", default="data/dunl/cache.jsonl")
    p.add_argument("--to", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "pack":
        pack(args.cache, args.to)
        return 0
    os.makedirs(args.out, exist_ok=True)
    cache = Cache(os.path.join(args.out, "cache.jsonl"))
    log(
        f"collect start epoch={args.epoch} api={args.api} rpc={args.rpc} cached_keys={len(cache.rows)} "
        f"torn_bytes={cache.torn} gap={args.gap}s"
    )
    fetcher = Fetcher(cache, gap=args.gap)
    rounds = collect(args, fetcher)
    log(
        f"collect done rounds={len(rounds)} calls={fetcher.calls} cached={fetcher.cached} "
        f"retried={fetcher.retried} errors={fetcher.errors} keys={len(cache.rows)}"
    )
    return 1 if fetcher.errors else 0


if __name__ == "__main__":
    sys.exit(main())
