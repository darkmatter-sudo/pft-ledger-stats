#!/usr/bin/env python3
"""Crosscheck sampled AccountRoots of one collect_flags.py snapshot against the public RPC (stdlib).

All checks are pinned to the snapshot's ledger hash:
  header   `ledger`: ledger_index and ledger_hash equal the snapshot header
  account  `account_info` of every sampled address: Flags, Sequence, Balance and the census settings
           (extract_flags.settings_of) equal the snapshot's AccountRoot; an address that is not in the
           snapshot must answer actNotFound

The sample is drawn once and saved (real addresses, private) to --sample-file; later runs (snapshot B)
reuse it and may add --extra addresses (e.g. accounts that changed in the window). A new draw takes up
to half of --count from accounts with a non-zero Flags (all of them if they fit), fills that half with
accounts that have any census setting, and draws the rest from all accounts, with random.Random(seed)
where seed = snapshot A's ledger hash.

`server_info` is read first; a ledger outside the public `complete_ledgers` stops with exit 2. Transient
errors are retried with logged backoff (RETRY lines); a call that never answers is GAVE_UP (exit 2).
`--negative` flips bit 0 of every expected Flags value, so every account check must MISMATCH: the
control. `--record FILE` saves every answer (private: it holds addresses), `--offline FILE` replays them
with no network (exit 3 on a call that was not recorded). Output names accounts only by their
pseudonymous id; `--summary FILE` writes the per-account verdicts as JSON for analyze_flags.py.

Usage:
  ./crosscheck_flags.py data/acctflags/A --sample-file data/acctflags/sample.json [--count 24]
      [--extra FILE] [--negative] [--record F | --offline F] [--summary F]
Exit: 0 all MATCH, 1 any MISMATCH, 2 GAVE_UP or ledger out of public range, 3 offline call not recorded.
"""

import argparse
import json
import os
import random
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collect_flags as C  # noqa: E402
import extract_flags as X  # noqa: E402

PUBLIC = "https://rpc.testnet.postfiat.org/"
ALLOW = {"server_info", "ledger", "account_info"}


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


def in_range(text, n):
    for part in text.split(","):
        part = part.strip()
        if not part or part == "empty":
            continue
        lo, _, hi = part.partition("-")
        if int(lo) <= n <= int(hi or lo):
            return True
    return False


def has_posture(o):
    s = X.settings_of(o)
    return int(o.get("Flags", 0)) != 0 or any(v not in (False, None) for v in s.values())


def draw(roots, count, seed):
    rng = random.Random(seed)
    flagged = sorted(o["Account"] for o in roots if int(o.get("Flags", 0)) != 0)
    picked = rng.sample(flagged, min(len(flagged), count // 2))
    posture = sorted(o["Account"] for o in roots if has_posture(o) and o["Account"] not in set(picked))
    picked += rng.sample(posture, min(len(posture), count // 2 - len(picked)))
    rest = sorted(o["Account"] for o in roots if o["Account"] not in set(picked))
    picked += rng.sample(rest, min(len(rest), count - len(picked)))
    return sorted(picked)


def view(o):
    return {
        "Flags": int(o.get("Flags", 0)),
        "Sequence": o.get("Sequence"),
        "Balance": o.get("Balance"),
        "settings": X.settings_of(o),
    }


def run(rpc, header, roots, addresses, key, negative=False, out=print):
    lh, li = header["ledger_hash"], header["ledger_index"]
    by_addr = {o["Account"]: o for o in roots}
    results = []
    info = rpc.call("server_info", {})["info"]
    rng = info.get("complete_ledgers", "")
    inside = in_range(rng, li)
    out(f"PUBLIC complete_ledgers={rng} ledger={li} inside={inside}")
    if not inside:
        raise C.GaveUp(f"ledger {li} outside public complete_ledgers {rng}")
    led = rpc.call("ledger", {"ledger_hash": lh})["ledger"]
    ok = led["ledger_hash"] == lh and int(led["ledger_index"]) == li
    results.append({"check": "header", "ok": ok})
    out(
        f"{'MATCH' if ok else 'MISMATCH'} header ledger_index={led['ledger_index']} "
        f"ledger_hash={led['ledger_hash']}"
    )
    for addr in addresses:
        pid = X.pseudo(key, addr)
        exp = view(by_addr[addr]) if addr in by_addr else None
        if exp is not None and negative:
            exp["Flags"] ^= 1
        try:
            got = view(rpc.call("account_info", {"account": addr, "ledger_hash": lh})["account_data"])
        except C.RpcError as e:
            got = None if e.error == "actNotFound" else {"error": e.error}
        ok = got == exp
        results.append({"check": "account", "id": pid, "ok": ok, "in_snapshot": exp is not None})
        shown = "actNotFound" if got is None else f"flags=0x{got.get('Flags', 0):08x}"
        out(
            f"{'MATCH' if ok else 'MISMATCH'} account id={pid} {shown} expected_in_snapshot={exp is not None}"
        )
    n_bad = sum(1 for r in results if not r["ok"])
    n_acc = sum(1 for r in results if r["check"] == "account")
    out(
        f"SUMMARY ledger={li} accounts={n_acc} checks={len(results)} match={len(results) - n_bad} "
        f"mismatch={n_bad} rpc_calls={rpc.calls} retries={rpc.retried} negative={negative}"
    )
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot")
    ap.add_argument("--sample-file", required=True)
    ap.add_argument("--count", type=int, default=24)
    ap.add_argument("--extra")
    ap.add_argument("--negative", action="store_true")
    ap.add_argument("--key-file", default="data/acctflags/.hmac_key")
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--summary")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--record")
    g.add_argument("--offline")
    a = ap.parse_args(argv)
    _man, header, _cnt, roots = X.load_snapshot(a.snapshot)
    key = X.load_key(a.key_file)
    if os.path.exists(a.sample_file):
        with open(a.sample_file, encoding="utf-8") as fh:
            sample = json.load(fh)
    else:
        sample = {"seed": header["ledger_hash"], "addresses": draw(roots, a.count, header["ledger_hash"])}
        fd = os.open(a.sample_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(sample, fh, sort_keys=True, indent=1)
    addresses = list(sample["addresses"])
    if a.extra:
        with open(a.extra, encoding="utf-8") as fh:
            addresses += [x for x in json.load(fh) if x not in addresses]
    send = replay(a.offline) if a.offline else http_transport()
    if a.record:
        send = recording(send, a.record)
    rpc = C.Rpc(send, lambda m: print(f"{C.utcnow()} {m}", flush=True), a.rate, a.retries, allow=ALLOW)
    try:
        results = run(rpc, header, roots, addresses, key, a.negative)
    except NotRecorded as e:
        print(f"NOT_RECORDED {str(e)[:120]}")
        return 3
    except C.GaveUp as e:
        print(f"GAVE_UP {e}")
        return 2
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as fh:
            json.dump(
                {"ledger_index": header["ledger_index"], "negative": a.negative, "results": results},
                fh,
                sort_keys=True,
                indent=1,
            )
            fh.write("\n")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
