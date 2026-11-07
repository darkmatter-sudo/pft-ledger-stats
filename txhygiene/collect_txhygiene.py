#!/usr/bin/env python3
"""Resumable, read-only transaction-construction hygiene collector (Python 3 stdlib only).

Forward-follows validated ledgers on your own node's admin JSON-RPC through
`docker exec <container> curl` (same client, rate limit and resume rules as
txmix/collect_tx.py and payflow/collect_payflow.py), fetching each ledger once with
`transactions: true, expand: true`. Only read-only methods are sent: server_info, ledger, fee.

Outputs (in --out):
  tx.jsonl      one object per transaction: how it was BUILT, not what it did. Ledger, close time, tx index,
                full tx hash, account as a 16-hex HMAC token (key in <out>/.hmac_key, 0600, never
                published), type, result, fee, Sequence, TicketSequence, LastLedgerSequence and its margin
                at inclusion (LastLedgerSequence - ledger_index), signing key type (from the
                SigningPubKey prefix only: ED = ed25519, 02/03 = secp256k1, empty + Signers = multisig;
                the key itself is never stored), signer count, NetworkID, presence of SourceTag /
                DestinationTag / AccountTxnID, Flags, memo count, and the sorted set of top-level field
                names ("fieldset", the client fingerprint).
  fees.jsonl    one `fee` RPC sample per newly validated ledger seen while caught up: base, median,
                minimum and open-ledger fee (drops), queue size and expected ledger size. Fees are only
                knowable at sample time; the analyzer matches each tx to the newest sample taken before
                its ledger was fetched.
  ledgers.jsonl one object per fetched ledger: hash, close time, txn_count, tx rows written
  gaps.jsonl    ledgers that could not be fetched after retries
  retries.jsonl every failed fetch attempt (ledger, attempt, error), including ones that later worked
  restarts.jsonl one object per process start: kind (start/resume), time, first ledger, lag, torn, orphans
  state.json    window start/end and start seq, so a restart resumes the same window
  collect.log   (--log) start/resume, hourly progress, gaps, final summary

Every append is one write of whole lines followed by fsync. Write order per ledger: tx.jsonl lines, then
the ledgers.jsonl line (fees.jsonl is independent of ledgers). On resume a torn last line is cut from each
file, and tx.jsonl lines after the last ledgers.jsonl/gaps.jsonl ledger are dropped (they are fetched
again), so a SIGKILL at any point cannot duplicate or lose a ledger.
SIGTERM/SIGINT stop cleanly after the current ledger. The window is enforced client-side: no ledger
closed after the window end time is written, whatever the node returns.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "txmix"))
from collect_tx import (  # noqa: E402
    RIPPLE_EPOCH,
    drop_torn_tail,
    hash_account,
    load_key,
    log,
    ripple_iso,
    utcnow,
)

READ_ONLY_METHODS = {"server_info", "ledger", "fee"}
TX_FIELDS = [
    "ledger_index",
    "close_time",
    "tx_index",
    "tx_hash",
    "account",
    "tx_type",
    "result",
    "fee_drops",
    "sequence",
    "ticket_seq",
    "lls",
    "lls_margin",
    "key_type",
    "signers",
    "network_id",
    "source_tag",
    "dest_tag",
    "account_txn_id",
    "flags",
    "memos",
    "fieldset",
]
FEE_FIELDS = [
    "sampled_utc",
    "validated",
    "ledger_current_index",
    "base_fee",
    "median_fee",
    "minimum_fee",
    "open_ledger_fee",
    "current_queue_size",
    "max_queue_size",
    "expected_ledger_size",
    "current_ledger_size",
]
# Fields that are never part of the fingerprint: added by the server (hash, metadata, ledger/date keys of
# some API versions), not by the client that built the tx.
NOT_CLIENT_FIELDS = {
    "hash",
    "metaData",
    "meta",
    "ledger_index",
    "date",
    "inLedger",
    "validated",
    "ctid",
    "DeliverMax",
}

_stop = False


class Rpc:
    """docker exec JSON-RPC client with a request-rate limiter (txmix's Rpc plus the read-only `fee`)."""

    def __init__(self, container, port, rate, docker_cmd):
        self.container = container
        self.url = f"http://127.0.0.1:{port}/"
        self.min_gap = 1.0 / rate
        self.docker_cmd = docker_cmd
        self.last = 0.0
        self.count = 0

    def call(self, method, params=None):
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"refusing non read-only method {method}")
        wait = self.min_gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.count += 1
        body = json.dumps({"method": method, "params": [params or {}]})
        cmd = self.docker_cmd + [
            "exec",
            self.container,
            "curl",
            "-s",
            "-m",
            "15",
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json",
            "-d",
            body,
            self.url,
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if out.returncode != 0:
            raise RuntimeError(f"docker exec rc={out.returncode}: {out.stderr.strip()[:200]}")
        result = json.loads(out.stdout)["result"]
        if result.get("status") == "error":
            raise LookupError(result.get("error", "unknown"))
        return result


def append_jsonl(path, rows):
    """Append rows as JSON lines in one write, then fsync. A kill can only tear the last line."""
    if not rows:
        return
    data = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, "rb") as fh:
        return sum(1 for line in fh if line.strip())


def last_ledger(path):
    """ledger_index of the last line of ledgers.jsonl/gaps.jsonl, or None."""
    rows = read_jsonl(path)
    return int(rows[-1]["ledger_index"]) if rows else None


def drop_orphans(path, done):
    """Cut trailing tx.jsonl lines whose ledger_index > done (written before a kill, no ledgers line).

    The collector appends a ledger's tx lines before its ledgers.jsonl line, so after a kill between the
    two appends those lines would be written again on resume. Returns the number of lines removed.
    Call after drop_torn_tail (every remaining line must parse).
    """
    if not os.path.exists(path):
        return 0
    with open(path, "rb+") as fh:
        lines = fh.read().split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        keep = len(lines)
        while keep > 0 and int(json.loads(lines[keep - 1])["ledger_index"]) > done:
            keep -= 1
        if keep == len(lines):
            return 0
        fh.truncate(sum(len(x) + 1 for x in lines[:keep]))
        fh.flush()
        os.fsync(fh.fileno())
        return len(lines) - keep


def key_type(tx):
    """ed25519 / secp256k1 / multisig / none / other, from the SigningPubKey prefix only."""
    spk = tx.get("SigningPubKey")
    if spk is None:
        return "none"
    if spk == "":
        return "multisig" if tx.get("Signers") else "none"
    head = spk[:2].upper()
    if head == "ED" and len(spk) == 66:
        return "ed25519"
    if head in ("02", "03") and len(spk) == 66:
        return "secp256k1"
    return "other"


def fieldset(tx):
    return "+".join(sorted(k for k in tx if k not in NOT_CLIENT_FIELDS))


def tx_row(idx, ct, tx, meta, key):
    lls = tx.get("LastLedgerSequence")
    return {
        "ledger_index": idx,
        "close_time": ct,
        "tx_index": int(meta["TransactionIndex"]),
        "tx_hash": tx.get("hash", ""),
        "account": hash_account(key, tx["Account"]) if tx.get("Account") else None,
        "tx_type": tx.get("TransactionType", ""),
        "result": meta.get("TransactionResult", ""),
        "fee_drops": int(tx.get("Fee", 0)),
        "sequence": int(tx["Sequence"]) if "Sequence" in tx else None,
        "ticket_seq": int(tx["TicketSequence"]) if "TicketSequence" in tx else None,
        "lls": int(lls) if lls is not None else None,
        "lls_margin": int(lls) - idx if lls is not None else None,
        "key_type": key_type(tx),
        "signers": len(tx.get("Signers") or []),
        "network_id": int(tx["NetworkID"]) if "NetworkID" in tx else None,
        "source_tag": "y" if "SourceTag" in tx else "n",
        "dest_tag": "y" if "DestinationTag" in tx else "n",
        "account_txn_id": "y" if "AccountTxnID" in tx else "n",
        "flags": int(tx.get("Flags", 0)),
        "memos": len(tx.get("Memos") or []),
        "fieldset": fieldset(tx),
    }


def ledger_rows(ledger, key):
    """tx rows of one expanded ledger, sorted by TransactionIndex (which must be unique)."""
    idx = int(ledger["ledger_index"])
    ct = int(ledger["close_time"])
    rows = []
    for tx in ledger.get("transactions") or []:
        meta = tx.get("metaData") or tx.get("meta") or {}
        if "TransactionIndex" not in meta:
            raise ValueError(f"ledger {idx}: tx {tx.get('hash', '?')} has no TransactionIndex")
        rows.append(tx_row(idx, ct, tx, meta, key))
    if len({r["tx_index"] for r in rows}) != len(rows):
        raise ValueError(f"ledger {idx}: duplicate TransactionIndex")
    rows.sort(key=lambda r: r["tx_index"])
    return rows


def fee_row(validated, fee):
    d = fee.get("drops") or {}
    return {
        "sampled_utc": utcnow(),
        "validated": validated,
        "ledger_current_index": int(fee.get("ledger_current_index", 0)),
        "base_fee": int(d["base_fee"]),
        "median_fee": int(d["median_fee"]),
        "minimum_fee": int(d["minimum_fee"]),
        "open_ledger_fee": int(d["open_ledger_fee"]),
        "current_queue_size": int(fee.get("current_queue_size", 0)),
        "max_queue_size": int(fee.get("max_queue_size", 0)),
        "expected_ledger_size": int(fee.get("expected_ledger_size", 0)),
        "current_ledger_size": int(fee.get("current_ledger_size", 0)),
    }


def in_window(close_time, end_unix):
    """Client-side window rule: a ledger belongs to the window iff it closed at or before the end."""
    return int(close_time) + RIPPLE_EPOCH <= end_unix


def _handle(signum, _frame):
    global _stop
    _stop = True


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/txhygiene/tx")
    p.add_argument("--log", default=None, help="append log lines to this file too")
    p.add_argument("--key-file", default=None, help="HMAC key file (default <out>/.hmac_key, 0600)")
    p.add_argument("--container", default="postfiatd")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--rate", type=float, default=1.0, help="max requests/second (default 1, max 1)")
    p.add_argument("--hours", type=float, default=24.0, help="window length from first start")
    p.add_argument("--start", type=int, default=None, help="first ledger (default: current validated)")
    p.add_argument("--stop-at", type=int, default=None, help="last ledger to fetch, then exit")
    p.add_argument("--no-fees", action="store_true", help="do not sample the fee RPC (resume drills)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--no-sudo", action="store_true", help="call docker without sudo")
    a = p.parse_args(argv)

    if a.rate > 1.0:
        p.error("--rate above 1 req/s is not allowed")
    os.makedirs(a.out, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    lf = open(a.log, "a") if a.log else None
    key = load_key(a.key_file or os.path.join(a.out, ".hmac_key"))
    rpc = Rpc(a.container, a.port, a.rate, ["docker"] if a.no_sudo else ["sudo", "docker"])

    tx_f = os.path.join(a.out, "tx.jsonl")
    fees_f = os.path.join(a.out, "fees.jsonl")
    ledgers_f = os.path.join(a.out, "ledgers.jsonl")
    gaps_f = os.path.join(a.out, "gaps.jsonl")
    retries_f = os.path.join(a.out, "retries.jsonl")
    restarts_f = os.path.join(a.out, "restarts.jsonl")
    state_path = os.path.join(a.out, "state.json")

    info = rpc.call("server_info")["info"]
    log(
        lf,
        f"node: build {info.get('build_version')} state {info.get('server_state')} "
        f"complete_ledgers {info.get('complete_ledgers')}",
    )
    validated = int(info["validated_ledger"]["seq"])

    state = {}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
    torn_total = orphans = 0
    if state:
        for path in (tx_f, fees_f, ledgers_f, gaps_f, retries_f, restarts_f):
            torn = drop_torn_tail(path)
            torn_total += torn
            if torn:
                log(lf, f"RESUME dropped torn last line of {os.path.basename(path)} ({torn} bytes)")
        done = max(
            x for x in (last_ledger(ledgers_f), last_ledger(gaps_f), state["start_seq"] - 1) if x is not None
        )
        orphans = drop_orphans(tx_f, done)
        if orphans:
            log(lf, f"RESUME dropped {orphans} tx.jsonl rows after ledger {done} (no ledgers.jsonl row)")
        nxt = done + 1
        kind = "resume"
        log(
            lf,
            f"RESUME at ledger {nxt} (window {state['start_utc']} -> {state['end_utc']}, "
            f"start seq {state['start_seq']}, behind by {validated - nxt + 1})",
        )
    else:
        nxt = a.start if a.start is not None else validated
        now = time.time()
        state = {
            "start": int(now),
            "end": int(now + a.hours * 3600),
            "start_seq": nxt,
            "start_utc": utcnow(),
            "stop_at": a.stop_at,
            "end_utc": datetime.fromtimestamp(now + a.hours * 3600, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        with open(state_path, "w") as fh:
            json.dump(state, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        kind = "start"
        log(
            lf,
            f"WINDOW START {state['start_utc']} -> {state['end_utc']} at ledger {nxt}, "
            f"stop_at {a.stop_at}, rate <= {a.rate} req/s",
        )
    append_jsonl(
        restarts_f,
        [
            {
                "kind": kind,
                "at_utc": utcnow(),
                "first_ledger": nxt,
                "validated": validated,
                "lag": validated - nxt + 1,
                "torn_bytes": torn_total,
                "orphan_rows": orphans,
            }
        ],
    )
    stop_at = state.get("stop_at")

    def counts():
        return (
            f"ledgers={count_lines(ledgers_f)} tx={count_lines(tx_f)} fees={count_lines(fees_f)} "
            f"gaps={count_lines(gaps_f)} retries={count_lines(retries_f)}"
        )

    reason = None
    sampled_for = None
    next_progress = time.monotonic() + 3600
    while not _stop and (stop_at is None or nxt <= stop_at):
        if time.monotonic() >= next_progress:
            log(lf, f"PROGRESS last={nxt - 1} lag={validated - nxt + 1} {counts()} requests={rpc.count}")
            next_progress = time.monotonic() + 3600
        if nxt > validated:
            if time.time() > state["end"] + 30:
                reason = "window end reached"
                break
            if not a.no_fees and sampled_for != validated:
                try:
                    append_jsonl(fees_f, [fee_row(validated, rpc.call("fee"))])
                except Exception as e:
                    log(lf, f"WARN fee sample failed: {type(e).__name__}: {e}")
                sampled_for = validated
            try:
                validated = int(rpc.call("server_info")["info"]["validated_ledger"]["seq"])
            except Exception as e:
                log(lf, f"WARN server_info failed: {e}")
            if nxt > validated:
                time.sleep(1.0)
            continue
        err = None
        for attempt in range(1, a.retries + 1):
            try:
                led = rpc.call("ledger", {"ledger_index": nxt, "transactions": True, "expand": True})[
                    "ledger"
                ]
                if not led.get("closed", True) or int(led["ledger_index"]) != nxt:
                    raise LookupError(f"unexpected ledger {led.get('ledger_index')}")
                ct = int(led["close_time"])
                if not in_window(ct, state["end"]):
                    reason = f"window end reached (ledger {nxt} closed {ripple_iso(ct)}, not written)"
                    break
                rows = ledger_rows(led, key)
                append_jsonl(tx_f, rows)
                append_jsonl(
                    ledgers_f,
                    [
                        {
                            "ledger_index": nxt,
                            "ledger_hash": led["ledger_hash"],
                            "close_time": ct,
                            "close_time_iso": ripple_iso(ct),
                            "txn_count": len(led.get("transactions") or []),
                            "tx_rows": len(rows),
                            "fetched_utc": utcnow(),
                        }
                    ],
                )
                err = None
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                append_jsonl(
                    retries_f,
                    [{"ledger_index": nxt, "attempt": attempt, "error": err, "logged_at": utcnow()}],
                )
                log(lf, f"WARN ledger {nxt} attempt {attempt}/{a.retries}: {err}")
                time.sleep(2.0)
        if reason:
            break
        if err:
            append_jsonl(gaps_f, [{"ledger_index": nxt, "error": err, "logged_at": utcnow()}])
            log(lf, f"GAP ledger {nxt}: {err}")
        nxt += 1

    if reason is None:
        reason = "signal" if _stop else "stop_at reached"
    log(lf, f"STOP ({reason}) requests this run={rpc.count}")
    log(
        lf,
        f"SUMMARY window {state['start_utc']} -> {state['end_utc']} "
        f"ledgers {state['start_seq']}-{nxt - 1} {counts()} restarts={count_lines(restarts_f)}",
    )
    if lf:
        lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
