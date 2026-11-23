#!/usr/bin/env python3
"""Read-only binary state snapshot + binary ledger capture for statehash/ (stdlib only).

  snapshot  pins one validated ledger (index + hash) on your own node, pages the FULL state tree
            with `ledger_data binary=true` on that ledger hash (pages saved, so an interrupted run
            resumes on the same pin), and writes header.json (the JSON header of the pinned ledger
            plus its 118-byte binary header), public-header.json (the same-index header from the
            public RPC, fetched once), state.jsonl ({index, data} per object, sorted by index) and
            MANIFEST.json (rows + sha256) last. A run without MANIFEST.json is incomplete.

  capture   forward-follows validated ledgers from --start and appends one line per ledger to
            ledgers.jsonl: index, hash, the 118-byte binary header and every transaction as
            {tx_blob, meta} (`ledger transactions=true expand=true binary=true`). Before a line is
            written: SHA-512Half("LWR\\0" || header) must equal the reported hash, the header's
            sequence must equal the asked index, its parent hash must equal the previous line's
            hash, and the transaction tree rebuilt from the blobs must equal the header's
            transaction_hash (so no transaction can be missing or extra). Any break stops the run
            (exit 3) instead of recording a gap. A restart resumes after the last complete line
            (a torn tail is cut off). --to N stops after ledger N.

Only read-only methods are sent (server_state, ledger, ledger_data); nothing is signed or submitted.
Own node: at most --rate requests/s per process (snapshot 1.5, capture 0.5: <= 2 req/s together),
through the same transport as acctflags/collect_flags.py. Public RPC: one `ledger` call per snapshot.

Usage:
  ./collect_statehash.py snapshot --out data/statehash/A [--ledger N] [--log FILE]
  ./collect_statehash.py capture --out data/statehash/cap --start N [--to M] [--log FILE]
"""

import argparse
import json
import os
import signal
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "acctflags"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hashverify"))
import collect_flags as cf  # noqa: E402
import lib_hashverify as hv  # noqa: E402

FORMAT = "pft-statehash-snapshot/1"
CAPTURE_FORMAT = "pft-statehash-capture/1"
PUBLIC_RPC = "https://rpc.testnet.postfiat.org/"
FILES = ("header.json", "public-header.json", "state.jsonl")
STOP = []


def parse_header(blob):
    """Fields of a 118-byte binary ledger header (rippled addRaw order)."""
    if len(blob) != hv.HEADER_BYTES:
        raise ValueError(f"header is {len(blob)} bytes, expected {hv.HEADER_BYTES}")
    return {
        "ledger_index": int.from_bytes(blob[0:4], "big"),
        "total_coins": int.from_bytes(blob[4:12], "big"),
        "parent_hash": blob[12:44].hex().upper(),
        "transaction_hash": blob[44:76].hex().upper(),
        "account_hash": blob[76:108].hex().upper(),
    }


def https_transport(url=PUBLIC_RPC, timeout=30):
    def send(body):
        req = urllib.request.Request(url, data=body.encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()

    return send


def page_binary(rpc, ledger_hash, pagedir, limit=2048, log=print):
    os.makedirs(pagedir, exist_ok=True)
    objs, marker, n = [], None, 0
    while True:
        path = os.path.join(pagedir, f"full-{n:04d}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved["ledger_hash"] != ledger_hash:
                raise SystemExit(f"{path} belongs to ledger {saved['ledger_hash']}, pinned {ledger_hash}")
            state, marker = saved["state"], saved.get("marker")
            log(f"RESUME page full-{n:04d} objects={len(state)}")
        else:
            p = {"ledger_hash": ledger_hash, "binary": True, "limit": limit}
            if marker is not None:
                p["marker"] = marker
            r = rpc.call("ledger_data", p)
            if r.get("ledger_hash") != ledger_hash:
                raise ValueError(f"ledger_data answered for {r.get('ledger_hash')}, pinned {ledger_hash}")
            state, marker = r.get("state", []), r.get("marker")
            cf.write_file(path, cf.dumps({"ledger_hash": ledger_hash, "state": state, "marker": marker}))
        objs.extend(state)
        n += 1
        if marker is None:
            return objs, n


def snapshot(rpc, out, ledger=None, log=print, limit=2048, public=None):
    os.makedirs(out, exist_ok=True)
    if os.path.exists(os.path.join(out, "MANIFEST.json")):
        raise SystemExit(f"{out} already holds a complete snapshot (MANIFEST.json); use a new --out")
    pin_path = os.path.join(out, "pin.json")
    if os.path.exists(pin_path):
        with open(pin_path, encoding="utf-8") as fh:
            pin = json.load(fh)
        if ledger is not None and int(ledger) != int(pin["ledger_index"]):
            raise SystemExit(f"{out} is pinned to {pin['ledger_index']}, not {ledger}")
        log(f"RESUME pinned ledger_index={pin['ledger_index']} ledger_hash={pin['ledger_hash']}")
    else:
        if ledger is None:
            st = rpc.call("server_state", {})["state"]
            if st.get("server_state") not in ("full", "validating", "proposing"):
                raise SystemExit(f"node not synced: server_state={st.get('server_state')}")
            ledger = st["validated_ledger"]["seq"]
        j = rpc.call("ledger", {"ledger_index": int(ledger)})
        b = rpc.call("ledger", {"ledger_index": int(ledger), "binary": True})
        if not (j.get("validated") and b.get("validated")):
            raise SystemExit(f"ledger {ledger} is not validated on this node")
        led = j["ledger"]
        blob = bytes.fromhex(b["ledger"]["ledger_data"])
        if hv.sha512half(hv.PREFIX_LEDGER, blob).hex().upper() != led["ledger_hash"]:
            raise SystemExit("binary header does not hash to the JSON ledger_hash")
        if hv.serialize_header(led) != blob:
            raise SystemExit("JSON header fields do not serialize to the binary header")
        pin = {
            "ledger_index": int(led["ledger_index"]),
            "ledger_hash": led["ledger_hash"],
            "account_hash": led["account_hash"],
            "parent_hash": led["parent_hash"],
            "transaction_hash": led["transaction_hash"],
            "close_time": led.get("close_time"),
            "close_time_iso": led.get("close_time_iso"),
            "header_hex": blob.hex().upper(),
        }
        cf.write_file(pin_path, cf.dumps(pin) + "\n")
    lh = pin["ledger_hash"]
    log(
        f"PIN ledger_index={pin['ledger_index']} ledger_hash={lh} account_hash={pin['account_hash']} "
        f"close_time_iso={pin.get('close_time_iso')}"
    )
    objs, pages = page_binary(rpc, lh, os.path.join(out, "pages"), limit=limit, log=log)
    rows = sorted(
        ({"index": o["index"].upper(), "data": o["data"].upper()} for o in objs), key=lambda r: r["index"]
    )
    if len({r["index"] for r in rows}) != len(rows):
        raise SystemExit("duplicate index in ledger_data pages: refusing snapshot")
    log(f"FULL objects={len(rows)} pages={pages}")

    pub = {"source": PUBLIC_RPC, "ledger_index": pin["ledger_index"], "fetched": cf.utcnow()}
    if public is not None:
        try:
            r = public.call("ledger", {"ledger_index": pin["ledger_index"]})
            pl = r["ledger"]
            pub.update(
                validated=bool(r.get("validated")),
                ledger_hash=pl.get("ledger_hash"),
                account_hash=pl.get("account_hash"),
                parent_hash=pl.get("parent_hash"),
                transaction_hash=pl.get("transaction_hash"),
            )
        except (cf.GaveUp, cf.RpcError, KeyError) as e:
            pub["error"] = f"{type(e).__name__}: {e}"
    log(f"PUBLIC {cf.dumps(pub)}")
    texts = {
        "header.json": cf.dumps({"format": FORMAT, **pin}) + "\n",
        "public-header.json": cf.dumps(pub) + "\n",
        "state.jsonl": cf.jsonl(rows),
    }
    for name in FILES:
        cf.write_file(os.path.join(out, name), texts[name])
    manifest = {
        "format": FORMAT,
        "ledger_index": pin["ledger_index"],
        "ledger_hash": lh,
        "account_hash": pin["account_hash"],
        "objects": len(rows),
        "files": {
            n: {"rows": texts[n].count("\n"), "sha256": cf.sha256(os.path.join(out, n))} for n in FILES
        },
        "rpc_calls": rpc.calls,
        "retries": rpc.retried,
    }
    cf.write_file(os.path.join(out, "MANIFEST.json"), json.dumps(manifest, sort_keys=True, indent=1) + "\n")
    for n in FILES:
        log(f"FILE {n} rows={manifest['files'][n]['rows']} sha256={manifest['files'][n]['sha256']}")
    log(
        f"DONE ledger_index={pin['ledger_index']} objects={len(rows)} "
        f"rpc_calls={rpc.calls} retries={rpc.retried}"
    )
    return manifest


def check_ledger(n, r, prev_hash):
    """Capture row from a binary `ledger` answer for index n, or raise ChainBreak."""
    led = r["ledger"]
    blob = bytes.fromhex(led["ledger_data"])
    h = parse_header(blob)
    lh = hv.sha512half(hv.PREFIX_LEDGER, blob).hex().upper()
    if lh != str(r.get("ledger_hash", "")).upper():
        raise cf.ChainBreak(f"ledger {n}: header hashes to {lh}, reported {r.get('ledger_hash')}")
    if h["ledger_index"] != n:
        raise cf.ChainBreak(f"asked for ledger {n}, header says {h['ledger_index']}")
    if prev_hash is not None and h["parent_hash"] != prev_hash:
        raise cf.ChainBreak(f"ledger {n} parent_hash {h['parent_hash']} != hash of {n - 1} {prev_hash}")
    txs = led.get("transactions", [])
    if any(not isinstance(t, dict) or "tx_blob" not in t or "meta" not in t for t in txs):
        raise cf.ChainBreak(f"ledger {n}: transaction without binary blob/metadata")
    root, _ = hv.tx_tree(txs)
    if root != h["transaction_hash"]:
        raise cf.ChainBreak(
            f"ledger {n}: rebuilt tx root {root} != header transaction_hash {h['transaction_hash']}"
        )
    return {
        "ledger_index": n,
        "ledger_hash": lh,
        "header": blob.hex().upper(),
        "transactions": [{"tx_blob": t["tx_blob"].upper(), "meta": t["meta"].upper()} for t in txs],
    }


def capture(rpc, out, start, to=None, log=print, poll=2.0, sleep=time.sleep, max_idle=None):
    os.makedirs(out, exist_ok=True)
    state_path = os.path.join(out, "state.json")
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            st = json.load(fh)
        if st["start"] != start:
            raise SystemExit(f"{out} captures from {st['start']}, not {start}")
    else:
        cf.write_file(
            state_path, cf.dumps({"format": CAPTURE_FORMAT, "start": start, "created": cf.utcnow()}) + "\n"
        )
    lpath = os.path.join(out, "ledgers.jsonl")
    rows = cf.read_lines(lpath)
    for i, row in enumerate(rows):
        if row["ledger_index"] != start + i:
            raise cf.ChainBreak(f"ledgers.jsonl row {i} is {row['ledger_index']}, expected {start + i}")
    n = rows[-1]["ledger_index"] + 1 if rows else start
    prev_hash = rows[-1]["ledger_hash"] if rows else None
    del rows
    log(f"CAPTURE start={start} next={n} to={to}")
    validated, idle, t_hour, txs = 0, 0, time.monotonic(), 0
    while not STOP and (to is None or n <= to):
        if n > validated:
            validated = rpc.call("server_state", {})["state"].get("validated_ledger", {}).get("seq", 0)
            if n > validated:
                idle += 1
                if max_idle is not None and idle > max_idle:
                    break
                sleep(poll)
                continue
        try:
            r = rpc.call("ledger", {"ledger_index": n, "transactions": True, "expand": True, "binary": True})
        except cf.RpcError as e:
            if e.error == "lgrNotFound" and n <= validated:
                raise cf.ChainBreak(f"ledger {n} is validated but no longer on this node (pruned?)") from e
            raise
        if not r.get("validated"):
            sleep(poll)
            continue
        idle = 0
        row = check_ledger(n, r, prev_hash)
        cf.append(lpath, row)
        prev_hash = row["ledger_hash"]
        txs += len(row["transactions"])
        if time.monotonic() - t_hour >= 3600:
            log(
                f"PROGRESS ledger={n} validated={validated} txs={txs} "
                f"rpc_calls={rpc.calls} retries={rpc.retried}"
            )
            t_hour = time.monotonic()
        n += 1
    log(f"STOPPED last={n - 1} txs_this_run={txs} rpc_calls={rpc.calls} retries={rpc.retried}")
    return n - 1


def _handle(signum, _frame):
    STOP.append(signum)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--out", required=True)
    s.add_argument("--ledger", type=int)
    s.add_argument("--rate", type=float, default=1.5)
    s.add_argument("--no-public", action="store_true", help="skip the public-RPC header fetch")
    c = sub.add_parser("capture")
    c.add_argument("--out", required=True)
    c.add_argument("--start", type=int, required=True)
    c.add_argument("--to", type=int)
    c.add_argument("--rate", type=float, default=0.5)
    for p in (s, c):
        p.add_argument("--container", default="postfiatd")
        p.add_argument("--retries", type=int, default=5)
        p.add_argument("--backoff", type=float, default=1.0)
        p.add_argument("--log")
    a = ap.parse_args(argv)
    fh = open(a.log, "a", encoding="utf-8") if a.log else None

    def log(msg):
        line = f"{cf.utcnow()} {msg}"
        print(line, flush=True)
        if fh:
            fh.write(line + "\n")
            fh.flush()

    rpc = cf.Rpc(cf.docker_transport(a.container), log, a.rate, a.retries, a.backoff)
    try:
        if a.cmd == "snapshot":
            public = None if a.no_public else cf.Rpc(https_transport(), log, 0.5, a.retries, 2.0)
            snapshot(rpc, a.out, a.ledger, log, public=public)
        else:
            signal.signal(signal.SIGTERM, _handle)
            signal.signal(signal.SIGINT, _handle)
            capture(rpc, a.out, a.start, a.to, log)
    except cf.GaveUp as e:
        log(f"GAVE_UP {e}")
        return 2
    except cf.ChainBreak as e:
        log(f"CHAIN_BREAK {e}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
