#!/usr/bin/env python3
"""Ledger header hash-chain verifier for two sources (read-only: server_info, ledger).

For every ledger in the overlap of the two sources' complete_ledgers (your own node and the public
RPC), the header is fetched from BOTH sources (`ledger`, transactions false) and checked without
trusting any reported hash:

  HASH_MISMATCH  SHA-512Half("LWR\\0" || header from the JSON fields) != the reported ledger_hash
  CHAIN_BREAK    parent_hash of ledger N != the RECOMPUTED hash of ledger N-1 (same source)
  DISAGREE       the recomputed hash of ledger N differs between the two sources
  GAP            a ledger of the overlap that a source could not serve after all retries

Each finding prints the ledger index and both hashes. A RANGE line prints the overlap; the run
reads server_info again at the end and reports any ledger a source pruned meanwhile as a GAP.

Retries: transport errors and slowDown/tooBusy/lgrNotFound/internal answers are retried with
exponential backoff (one RETRY line each, counted in summary.csv); the public RPC also gets
--gap seconds between requests. Your own node gets --local-gap (default 0).

--record FILE   writes every fetched header (JSON lines) for offline re-runs
--offline FILE  re-runs every check from such a file, with no RPC at all
--tamper S:N:F  (with --offline) changes field F of ledger N from source S before checking
                (total_coins/close_time +1, parent_hash/account_hash/transaction_hash last nibble
                flipped). A real-data negative control: the run must print HASH_MISMATCH at N,
                DISAGREE at N and CHAIN_BREAK at N+1 (parent_hash: CHAIN_BREAK at N), and exit 1.

Outputs (--out DIR): headers.csv (one row per source and ledger) and summary.csv (metric,value).
Exit 0 = full coverage on both sources and no finding, 1 = any finding or gap, 2 = RPC failure.
"""

import argparse
import csv
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "rewardflow"))
from collect_rewardflow import RETRY_ERRORS, Rpc, RpcError  # noqa: E402
from lib_hashverify import ZERO, ledger_hash, overlap, parse_ranges  # noqa: E402

RETRY = RETRY_ERRORS | {"lgrNotFound", "internal"}
METHODS = ("server_info", "ledger")
FIELDS = (
    "ledger_index",
    "ledger_hash",
    "parent_hash",
    "transaction_hash",
    "account_hash",
    "total_coins",
    "parent_close_time",
    "close_time",
    "close_time_resolution",
    "close_flags",
)
SOURCES = ("local", "public")
CSV_FIELDS = [
    "source",
    "ledger_index",
    "reported_hash",
    "computed_hash",
    "hash_ok",
    "parent_ok",
    "agree",
    "parent_hash",
    "prev_computed",
    "other_computed",
    "empty",
    "close_time",
]


def out(msg):
    print(msg, flush=True)


def opener(path, mode):
    if mode == "w":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return gzip.open(path, mode + "t") if path.endswith(".gz") else open(path, mode)


def make_rpc(url, gap):
    return Rpc(url, gap=gap, retries=7, backoff=2.0, retry_errors=RETRY, methods=METHODS)


def ranges(rpc):
    spans = parse_ranges(rpc.call("server_info", {})["info"].get("complete_ledgers", ""))
    if not spans:
        raise RpcError("server_info: no complete_ledgers")
    return spans


def fetch_header(rpc, idx):
    led = rpc.call("ledger", {"ledger_index": idx, "transactions": False})["ledger"]
    if int(led["ledger_index"]) != idx or not led.get("closed", False):
        raise RpcError(f"ledger {idx}: answer is ledger {led.get('ledger_index')} closed={led.get('closed')}")
    return {f: led[f] for f in FIELDS}


def tamper(headers, spec):
    """Apply one --tamper SOURCE:INDEX:FIELD change to the loaded headers (in place)."""
    src, idx, field = spec.split(":")
    h = headers[src][int(idx)]
    if field in ("total_coins", "close_time"):
        h[field] = str(int(h[field]) + 1) if field == "total_coins" else int(h[field]) + 1
    elif field in ("parent_hash", "account_hash", "transaction_hash"):
        v = h[field]
        h[field] = v[:-1] + ("0" if v[-1] != "0" else "1")
    else:
        raise ValueError(f"--tamper: unsupported field {field}")
    return src, int(idx), field


def verify(headers, lo, hi):
    """Findings + per-row data. headers = {source: {index: header dict}}."""
    findings, rows, computed = [], [], {}
    for src in SOURCES:
        got = headers.get(src, {})
        comp = computed[src] = {}
        for idx in range(lo, hi + 1):
            h = got.get(idx)
            if h is None:
                findings.append(("GAP", src, idx, "-", "-"))
                continue
            comp[idx] = ledger_hash(h)
            if comp[idx] != h["ledger_hash"].upper():
                findings.append(("HASH_MISMATCH", src, idx, h["ledger_hash"].upper(), comp[idx]))
    for src in SOURCES:
        got, comp = headers.get(src, {}), computed[src]
        for idx in range(lo, hi + 1):
            h = got.get(idx)
            if h is None:
                continue
            parent_ok = ""
            if idx - 1 in comp:
                parent_ok = h["parent_hash"].upper() == comp[idx - 1]
                if not parent_ok:
                    findings.append(("CHAIN_BREAK", src, idx, h["parent_hash"].upper(), comp[idx - 1]))
            other = computed["public" if src == "local" else "local"].get(idx)
            agree = "" if other is None else other == comp[idx]
            if src == "local" and other is not None and not agree:
                findings.append(("DISAGREE", "both", idx, comp[idx], other))
            rows.append(
                {
                    "source": src,
                    "ledger_index": idx,
                    "reported_hash": h["ledger_hash"].upper(),
                    "computed_hash": comp[idx],
                    "hash_ok": comp[idx] == h["ledger_hash"].upper(),
                    "parent_ok": parent_ok,
                    "agree": agree,
                    "parent_hash": h["parent_hash"].upper(),
                    "prev_computed": comp.get(idx - 1, ""),
                    "other_computed": other or "",
                    "empty": h["transaction_hash"].upper() == ZERO.hex().upper(),
                    "close_time": h["close_time"],
                }
            )
    return findings, rows


def print_finding(f):
    kind, src, idx, a, b = f
    labels = {
        "HASH_MISMATCH": ("reported", "computed"),
        "CHAIN_BREAK": ("parent_hash", "computed_prev"),
        "DISAGREE": ("local", "public"),
        "GAP": ("reported", "computed"),
    }[kind]
    out(f"{kind} source={src} ledger={idx} {labels[0]}={a} {labels[1]}={b}")


def write_outputs(d, rows, summary):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "headers.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(d, "summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "value"])
        w.writerows(summary)


def summarize(findings, rows, lo, hi, runtime, rpcs):
    kinds = {}
    for f in findings:
        key = (f[0], f[1])
        kinds[key] = kinds.get(key, 0) + 1
    n = hi - lo + 1
    s = [("range_first", lo), ("range_last", hi), ("range_ledgers", n)]
    for src in SOURCES:
        mine = [r for r in rows if r["source"] == src]
        s += [
            (f"{src}_covered", len(mine)),
            (f"{src}_hash_ok", sum(1 for r in mine if r["hash_ok"])),
            (f"{src}_hash_mismatch", kinds.get(("HASH_MISMATCH", src), 0)),
            (f"{src}_parent_links_checked", sum(1 for r in mine if r["parent_ok"] != "")),
            (f"{src}_chain_breaks", kinds.get(("CHAIN_BREAK", src), 0)),
            (f"{src}_gaps", kinds.get(("GAP", src), 0)),
            (f"{src}_empty_ledgers", sum(1 for r in mine if r["empty"])),
        ]
    s += [
        ("agree", sum(1 for r in rows if r["source"] == "local" and r["agree"] is True)),
        ("disagree", kinds.get(("DISAGREE", "both"), 0)),
        ("findings", len(findings)),
        ("runtime_s", f"{runtime:.1f}"),
    ]
    for src, rpc in rpcs.items():
        s += [(f"{src}_rpc_calls", rpc.calls), (f"{src}_rpc_retries", rpc.retried)]
    return s


def fetch_all(rpcs, lo, hi, record):
    headers = {src: {} for src in SOURCES}
    for src in SOURCES:
        t0 = time.monotonic()
        for idx in range(lo, hi + 1):
            try:
                h = fetch_header(rpcs[src], idx)
            except RpcError as ex:
                out(f"FETCH_FAILED source={src} ledger={idx}: {ex}")
                continue
            headers[src][idx] = h
            if record:
                record.write(json.dumps({"source": src, "header": h}, sort_keys=True) + "\n")
            if (idx - lo + 1) % 100 == 0:
                out(f"PROGRESS source={src} {idx - lo + 1}/{hi - lo + 1}")
        out(f"FETCHED source={src} {len(headers[src])}/{hi - lo + 1} in {time.monotonic() - t0:.1f}s")
    return headers


def load(path):
    headers = {src: {} for src in SOURCES}
    with opener(path, "r") as fh:
        for line in fh:
            r = json.loads(line)
            headers[r["source"]][int(r["header"]["ledger_index"])] = r["header"]
    return headers


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local", default="http://127.0.0.1:5005")
    p.add_argument("--public", default="https://rpc.testnet.postfiat.org")
    p.add_argument("--gap", type=float, default=0.5, help="seconds between public RPC requests")
    p.add_argument("--local-gap", type=float, default=0.0)
    p.add_argument("--first", type=int, help="clamp the overlap (default: the whole overlap)")
    p.add_argument("--last", type=int)
    p.add_argument("--out", required=True)
    p.add_argument("--record")
    p.add_argument("--offline")
    p.add_argument("--tamper", action="append", default=[])
    a = p.parse_args(argv)
    t0 = time.monotonic()
    if a.tamper and not a.offline:
        p.error("--tamper only works with --offline (it never changes live data)")
    rpcs = {}
    if a.offline:
        headers = load(a.offline)
        idx_all = set(headers["local"]) | set(headers["public"])
        lo, hi = a.first or min(idx_all), a.last or max(idx_all)
        out(f"OFFLINE {a.offline}: local {len(headers['local'])} public {len(headers['public'])} headers")
        for spec in a.tamper:
            src, idx, field = tamper(headers, spec)
            out(f"TAMPER source={src} ledger={idx} field={field} (negative control)")
    else:
        rpcs = {"local": make_rpc(a.local, a.local_gap), "public": make_rpc(a.public, a.gap)}
        try:
            r_loc, r_pub = ranges(rpcs["local"]), ranges(rpcs["public"])
            ov = overlap(r_loc, r_pub)
            if ov is None:
                out(f"RANGE no overlap: local {r_loc} public {r_pub}")
                return 2
            lo, hi = max(ov[0], a.first or ov[0]), min(ov[1], a.last or ov[1])
            out(
                f"RANGE local={r_loc} public={r_pub} overlap={ov[0]}-{ov[1]} "
                f"checked={lo}-{hi} ({hi - lo + 1})"
            )
            rec = opener(a.record, "w") if a.record else None
            try:
                headers = fetch_all(rpcs, lo, hi, rec)
            finally:
                if rec:
                    rec.close()
            out(f"RANGE_END local={ranges(rpcs['local'])} public={ranges(rpcs['public'])}")
        except RpcError as ex:
            out(f"RPC_FAILURE {ex}")
            return 2
    findings, rows = verify(headers, lo, hi)
    for f in findings:
        print_finding(f)
    summary = summarize(findings, rows, lo, hi, time.monotonic() - t0, rpcs)
    write_outputs(a.out, rows, summary)
    for k, v in summary:
        out(f"SUMMARY {k}={v}")
    out(f"RESULT: {'PASS' if not findings else 'FAIL'} ({len(findings)} findings)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
