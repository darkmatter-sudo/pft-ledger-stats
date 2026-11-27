#!/usr/bin/env python3
"""Read-only collector for Dynamic UNL endpoint-visibility inputs (Python 3 stdlib only).

`collect` sends HTTP GETs to the scoring service's public API (default
https://scoring-testnet.postfiat.org) and nothing else:
  * /api/scoring/rounds (all pages; a live document, fetched once per --epoch),
  * per COMPLETE round: /api/scoring/rounds/N/bundle.json and, of the files its file_hashes lists,
    inputs/validator_evidence.json, inputs/model_request.json, inputs/diversity_inputs.json,
    outputs/validator_scores.json and runtime/execution_manifest.json.
Round documents are immutable: each key is fetched once (final on HTTP 200 or 404) and later runs
resume from the cache. Requests are spaced by --gap seconds (default 1.0, i.e. at most one request per
second) and retried with exponential backoff on 429/5xx/transport errors.

Raw cache: --out/cache.jsonl, append-only and fsynced, one row per response {key, url, t, status, sha256,
bytes, body}. The raw evidence holds third-party IP addresses: it stays local and is never committed.

`pack` reduces the raw cache to the committed input (gzip mtime 0, sorted keys): per round the raw-file
sha256s, their content hashes next to the published bundle hashes, the concentration block, the prompt's
per-validator (provider_family, country), and per validator only master key, resolution status, whether
an address was seen (boolean), ASN number and name, country, domain + domain_verified and the diversity
sub-score. No address is copied; pack refuses to write if any IP address is left in its output.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_endpoints as L  # noqa: E402

API = "https://scoring-testnet.postfiat.org"
FILES = (
    "inputs/validator_evidence.json",
    "inputs/model_request.json",
    "inputs/diversity_inputs.json",
    "outputs/validator_scores.json",
    "runtime/execution_manifest.json",
)
RETRY_HTTP = {429, 500, 502, 503, 504}
UA = "pft-ledger-stats-endpoints/1 (read-only audit; github.com/darkmatter-sudo/pft-ledger-stats)"


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"{utcnow()} {msg}", flush=True)


class Cache:
    def __init__(self, path):
        self.path, self.rows, self.torn = path, {}, 0
        if os.path.exists(path):
            with open(path, "rb") as fh:
                data = fh.read()
            good = data[: data.rfind(b"\n") + 1] if data and not data.endswith(b"\n") else data
            if good != data:
                self.torn = len(data) - len(good)
                with open(path, "wb") as fh:
                    fh.write(good)
            for line in good.splitlines():
                row = json.loads(line)
                self.rows[row["key"]] = row
        self.fh = open(path, "a", encoding="utf-8")

    def final(self, key):
        row = self.rows.get(key)
        return row is not None and row["status"] in (200, 404)

    def put(self, row):
        self.fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.rows[row["key"]] = row


class Fetcher:
    def __init__(self, cache, gap=1.0, retries=5, backoff=2.0, opener=None, sleep=None, clock=None):
        self.cache, self.gap, self.retries, self.backoff = cache, gap, retries, backoff
        self.opener = opener or self._open
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.last = None
        self.calls = self.retried = self.cached = self.errors = 0

    @staticmethod
    def _open(url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _pace(self):
        if self.last is not None:
            wait = self.gap - (self.clock() - self.last)
            if wait > 0:
                self.sleep(wait)
        self.last = self.clock()

    def fetch(self, key, url):
        if self.cache.final(key):
            self.cached += 1
            return self.cache.rows[key]
        status, raw = 0, b""
        for attempt in range(self.retries + 1):
            self._pace()
            self.calls += 1
            try:
                status, raw = self.opener(url)
                err = None
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                status, raw, err = 0, b"", type(e).__name__
            if status != 0 and status not in RETRY_HTTP:
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
        self.cache.put(row)
        return row


def body_json(row):
    if not row or row["status"] != 200:
        return None
    try:
        return json.loads(row["body"])
    except ValueError:
        return None


def round_list(cache, epoch):
    rounds, offset, total = [], 0, None
    while True:
        page = body_json(cache.rows.get(f"{epoch}/rounds/{offset}"))
        if page is None:
            return None, None
        rounds += page["rounds"]
        offset += len(page["rounds"])
        total = page["total"]
        if not page["rounds"] or offset >= total:
            return rounds, total


def collect(args, fetcher):
    api = args.api.rstrip("/")
    offset = 0
    while True:
        row = fetcher.fetch(
            f"{args.epoch}/rounds/{offset}", f"{api}/api/scoring/rounds?limit=100&offset={offset}"
        )
        page = body_json(row)
        if not page:
            raise SystemExit(f"round list failed at offset {offset}: status {row['status']}")
        offset += len(page["rounds"])
        if not page["rounds"] or offset >= page["total"]:
            break
    rounds, total = round_list(fetcher.cache, args.epoch)
    done = [r for r in rounds if r["status"] == "COMPLETE"]
    log(f"round list: {len(rounds)} rounds (total {total}), COMPLETE {len(done)}")
    for r in sorted(done, key=lambda x: x["round_number"]):
        n = r["round_number"]
        bundle = body_json(
            fetcher.fetch(f"round/{n}/bundle.json", f"{api}/api/scoring/rounds/{n}/bundle.json")
        )
        listed = (bundle or {}).get("file_hashes") or {}
        for f in FILES:
            if f in listed:
                fetcher.fetch(f"round/{n}/file/{f}", f"{api}/api/scoring/rounds/{n}/{urllib.parse.quote(f)}")
        log(
            f"round {n}: bundle={'yes' if bundle else 'no'} files={sum(f in listed for f in FILES)} "
            f"calls={fetcher.calls} cached={fetcher.cached} errors={fetcher.errors}"
        )
    log(
        f"done: calls={fetcher.calls} retried={fetcher.retried} cached={fetcher.cached} "
        f"errors={fetcher.errors}"
    )
    return 0 if fetcher.errors == 0 else 1


def _file_record(row, published):
    rec = {"http": row["status"] if row else None, "published_sha256": published}
    if row:
        rec["raw_sha256"], rec["bytes"] = row["sha256"], row["bytes"]
        j = body_json(row)
        rec["content_sha256"] = L.content_hash(j) if j is not None else None
    return rec


def reduce_round(r, rows):
    n = r["round_number"]
    out = {
        "kind": "round",
        "round": n,
        "status": r["status"],
        "vl_sequence": r.get("vl_sequence"),
        "completed_at": r.get("completed_at"),
    }
    brow = rows.get(f"round/{n}/bundle.json")
    bundle = body_json(brow)
    out["bundle"] = {"http": brow["status"] if brow else None, "raw_sha256": brow["sha256"] if brow else None}
    listed = (bundle or {}).get("file_hashes") or {}
    files, docs = {}, {}
    for f in FILES:
        if f in listed:
            row = rows.get(f"round/{n}/file/{f}")
            files[f] = _file_record(row, listed[f])
            docs[f] = body_json(row)
    out["files"] = files
    man = docs.get("runtime/execution_manifest.json") or {}
    code = man.get("code") or {}
    out["code_commit"] = code.get("commit")
    out["prompt_version"] = (code.get("prompt") or {}).get("version")
    mreq = docs.get("inputs/model_request.json")
    out["block"] = L.extract_block(mreq) if mreq else None
    out["prompt"] = L.prompt_entries(mreq) if mreq else None
    ev = docs.get("inputs/validator_evidence.json")
    scores = docs.get("outputs/validator_scores.json") or {}
    div = {s.get("master_key"): s.get("diversity") for s in scores.get("validator_scores") or []}
    vals = None
    if ev:
        vals = []
        for v in sorted(ev.get("validators") or [], key=lambda x: x["master_key"]):
            asn = v.get("asn") or {}
            geo = v.get("geolocation") or {}
            vals.append(
                {
                    "master_key": v["master_key"],
                    "endpoint": "unresolved" if L.family_for(asn.get("as_name")) == L.UNKNOWN else "resolved",
                    "address_seen": bool(v.get("ip")),
                    "asn": asn.get("asn"),
                    "as_name": asn.get("as_name"),
                    "country": geo.get("country"),
                    "domain": v.get("domain"),
                    "domain_verified": v.get("domain_verified"),
                    "diversity": div.get(v["master_key"]),
                }
            )
    out["validators"] = vals
    out["diversity_inputs"] = docs.get("inputs/diversity_inputs.json")
    return out


def pack(cache_path, out_path, epoch):
    cache = Cache(cache_path)
    rounds, total = round_list(cache, epoch)
    if rounds is None:
        raise SystemExit(f"no round list for epoch {epoch} in {cache_path}")
    meta = {
        "kind": "meta",
        "epoch": epoch,
        "rounds_listed": total,
        "complete_rounds": sorted(r["round_number"] for r in rounds if r["status"] == "COMPLETE"),
        "list_fetched_at": cache.rows[f"{epoch}/rounds/0"]["t"],
        "source": API,
    }
    lines = [meta] + [
        reduce_round(r, cache.rows)
        for r in sorted(rounds, key=lambda x: x["round_number"])
        if r["status"] == "COMPLETE"
    ]
    text = "".join(
        json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for x in lines
    )
    ips = L.find_ips(text)
    if ips:
        raise SystemExit(f"refusing to pack: {len(ips)} IP address(es) left in the reduced output")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(text.encode("utf-8"))
    with open(out_path, "wb") as fh:
        fh.write(buf.getvalue())
    log(
        f"packed {len(lines) - 1} COMPLETE rounds -> {out_path} "
        f"sha256={hashlib.sha256(buf.getvalue()).hexdigest()}"
    )
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--out", required=True)
    c.add_argument("--api", default=API)
    c.add_argument("--epoch", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    c.add_argument("--gap", type=float, default=1.0)
    p = sub.add_parser("pack")
    p.add_argument("--cache", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--epoch", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        if a.gap < 1.0:
            raise SystemExit("--gap below 1.0 s is not allowed (at most one request per second)")
        os.makedirs(a.out, exist_ok=True)
        cache = Cache(os.path.join(a.out, "cache.jsonl"))
        if cache.torn:
            log(f"cut torn last line ({cache.torn} bytes)")
        log(f"collect epoch={a.epoch} api={a.api} gap={a.gap}s cache rows={len(cache.rows)}")
        return collect(a, Fetcher(cache, gap=a.gap))
    return pack(a.cache, a.out, a.epoch)


if __name__ == "__main__":
    sys.exit(main())
