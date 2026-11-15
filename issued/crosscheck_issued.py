#!/usr/bin/env python3
"""Crosscheck one collect_issued.py snapshot against the public Post Fiat testnet RPC (read-only, stdlib).

Checks, all at the snapshot's pinned ledger hash:
  header     `ledger`: ledger_index, ledger_hash, account_hash, close_time equal the snapshot header
  lines      `ledger_data type=state`: the RippleState index set equals the snapshot (missing and extra
             are both MISMATCH) and every object is equal field for field
  amm        `ledger_data type=amm`: the AMM index set equals the snapshot (an empty set is checked too)
  gateway    `gateway_balances` of every trust-line party equals the local node's recorded answer
  sample     `account_lines account=X peer=Y` for --sample K lines drawn with a seeded RNG (seed = the
             ledger hash, so the draw is reproducible; K >= lines checks every line), from a randomly
             drawn side: the one returned line must equal the view derived from the RippleState object
             (balance sign, limits, NoRipple, freeze, auth flags)

`server_info` is read first; a ledger outside the public `complete_ledgers` stops with exit 2. Transient
errors are retried with logged exponential backoff (RETRY lines, see collect_issued.Rpc), and also
`lgrNotFound` for a ledger inside the advertised range; a call with no answer is GAVE_UP (exit 2).
`--negative` alters every expectation (flipped hash digit, a fake extra index per object set, an altered
field per object and per gateway answer, a flipped balance sign per sampled line), so every check must
MISMATCH: the control. `--record FILE` saves every answer, `--offline FILE` replays them with no network
(a call missing from the recording exits 3).

Usage:
  ./crosscheck_issued.py issued/data/A [--sample 20] [--negative] [--record F | --offline F]
Exit: 0 all MATCH, 1 any MISMATCH, 2 GAVE_UP or ledger out of public range, 3 offline call not recorded.
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analyze_issued as A  # noqa: E402
import collect_issued as C  # noqa: E402

PUBLIC = "https://rpc.testnet.postfiat.org/"


class NotRecorded(Exception):
    pass


def http_transport(url=PUBLIC, timeout=30):
    def send(body):
        req = urllib.request.Request(url, data=body.encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            raise OSError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise OSError(str(e)) from e

    return send


def recording(send, path):
    book = {}

    def rec(body):
        ans = send(body)
        book[body] = ans
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(book, fh, sort_keys=True, indent=0)
            fh.write("\n")
        return ans

    return rec


def replay(path):
    with open(path, encoding="utf-8") as fh:
        book = json.load(fh)

    def send(body):
        if body not in book:
            raise NotRecorded(body)
        return book[body]

    return send


def flip_hex(h):
    return h[:-1] + ("0" if h[-1] != "0" else "1")


def strip(o):
    return {k: v for k, v in o.items() if k not in ("ledger_hash", "ledger_index", "validated", "status")}


def run(rpc, snap, sample, negative=False, out=print):
    hdr = snap["header"]
    lh = hdr["ledger_hash"]
    results = []

    def verdict(name, ok, detail=""):
        v = "MATCH" if ok else "MISMATCH"
        results.append(v)
        out(f"{v} {name}{(' ' + detail) if detail else ''}")

    info = rpc.call("server_info", {})["info"]
    rng_txt = info.get("complete_ledgers", "")
    inside = any(
        lo <= hdr["ledger_index"] <= hi
        for lo, hi in (
            tuple(map(int, p.split("-"))) if "-" in p else (int(p), int(p))
            for p in rng_txt.split(",")
            if p.strip() and p.strip() != "empty"
        )
    )
    out(f"PUBLIC complete_ledgers={rng_txt} ledger={hdr['ledger_index']} inside={inside}")
    if not inside:
        raise C.GaveUp(f"ledger {hdr['ledger_index']} outside public complete_ledgers {rng_txt}")

    def ask(method, params):
        for attempt in range(4):
            try:
                return rpc.call(method, params)
            except C.RpcError as e:
                if e.error != "lgrNotFound" or attempt == 3:
                    raise
                out(f"RETRY {method} lgrNotFound inside public range try {attempt + 1}/4 sleep={2**attempt}s")
                rpc.sleep(2**attempt)
        raise AssertionError("unreachable")

    led = ask("ledger", {"ledger_hash": lh})["ledger"]
    exp_hash = flip_hex(lh) if negative else lh
    for f, want in (
        ("ledger_index", hdr["ledger_index"]),
        ("ledger_hash", exp_hash),
        ("account_hash", hdr["account_hash"]),
        ("close_time", hdr["close_time"]),
    ):
        got = int(led[f]) if f == "ledger_index" else led[f]
        if negative and f != "ledger_hash":
            want = flip_hex(want) if isinstance(want, str) else want + 1
        verdict(f"header.{f}", got == want, f"got={got} want={want}")

    for typ, rows in (("state", snap["lines"]), ("amm", snap["amm"])):
        got, _ = C.page(rpc, lh, typ)
        g = {o["index"]: o for o in got}
        want = {o["index"]: o for o in rows}
        if negative:
            want[flip_hex("0" * 64)] = {"index": "fake"}
        missing, extra = sorted(want.keys() - g.keys()), sorted(g.keys() - want.keys())
        verdict(
            f"{typ}.index_set",
            not missing and not extra,
            f"public={len(g)} snapshot={len(want)} missing={len(missing)} extra={len(extra)}",
        )
        for i in sorted(want.keys() & g.keys()):
            w = dict(want[i])
            if negative:
                w["Flags"] = w.get("Flags", 0) ^ 1
            verdict(f"{typ}.object {i}", g[i] == w)

    for acct, rec in sorted(snap["gateway_balances"].items()):
        got = ask("gateway_balances", {"account": acct, "ledger_hash": lh})
        gotn = {"account": acct, **{k: got[k] for k in C.GB_FIELDS if k in got}}
        w = dict(rec)
        if negative:
            w["account"] = acct + "x"
        verdict(f"gateway_balances {acct}", gotn == w)

    rnd = random.Random(lh)
    lines = sorted(snap["lines"], key=lambda o: o["index"])
    picks = rnd.sample(lines, min(sample, len(lines)))
    out(f"SAMPLE seed=ledger_hash k={len(picks)} of {len(lines)} lines")
    for ln in sorted(picks, key=lambda o: o["index"]):
        me = rnd.choice(A.parties(ln))
        peer = [p for p in A.parties(ln) if p != me][0]
        want = A.view(ln, me)
        if negative:
            want["balance"] = A.num(-A.Decimal(want["balance"])) if want["balance"] != "0" else "-1"
        got = ask("account_lines", {"account": me, "peer": peer, "ledger_hash": lh})["lines"]
        mine = [x for x in got if x["currency"] == want["currency"]]
        ok = len(mine) == 1
        if ok:
            x = mine[0]
            gv = {
                "account": x["account"],
                "currency": x["currency"],
                "balance": A.num(x["balance"]),
                "limit": A.num(x["limit"]),
                "limit_peer": A.num(x["limit_peer"]),
                "no_ripple": bool(x.get("no_ripple", False)),
                "no_ripple_peer": bool(x.get("no_ripple_peer", False)),
                "freeze": bool(x.get("freeze", False)),
                "freeze_peer": bool(x.get("freeze_peer", False)),
                "authorized": bool(x.get("authorized", False)),
                "peer_authorized": bool(x.get("peer_authorized", False)),
            }
            ok = gv == want
            detail = (
                "" if ok else f"got={json.dumps(gv, sort_keys=True)} want={json.dumps(want, sort_keys=True)}"
            )
        else:
            detail = f"public returned {len(mine)} lines for {want['currency']}"
        verdict(f"account_lines {me} peer={peer} {want['currency']} balance={want['balance']}", ok, detail)

    n_ok, n_bad = results.count("MATCH"), results.count("MISMATCH")
    out(
        f"RESULT checks={len(results)} match={n_ok} mismatch={n_bad} "
        f"rpc_calls={rpc.calls} retries={rpc.retried}"
        f"{' NEGATIVE' if negative else ''}"
    )
    return n_bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot")
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--negative", action="store_true")
    ap.add_argument("--rpc", default=PUBLIC)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--backoff", type=float, default=1.0)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--record")
    g.add_argument("--offline")
    a = ap.parse_args(argv)
    try:
        snap = A.load_snapshot(a.snapshot)
    except A.BadInput as e:
        print(f"BAD_INPUT {e}")
        return 3

    def out(msg):
        print(f"{C.utcnow()} {msg}", flush=True)

    if a.offline:
        send = replay(a.offline)
    else:
        send = http_transport(a.rpc)
        if a.record:
            send = recording(send, a.record)
    rpc = C.Rpc(send, out, a.retries, a.backoff, sleep=(lambda s: None) if a.offline else time.sleep)
    try:
        bad = run(rpc, snap, a.sample, a.negative, out)
    except NotRecorded as e:
        out(f"NOT_RECORDED {e}")
        return 3
    except C.GaveUp as e:
        out(f"GAVE_UP {e}")
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
