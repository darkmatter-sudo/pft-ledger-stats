import csv
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "senders"))
import crosscheck_senders as cx  # noqa: E402

LO, HI = 100, 104
TX_FIELDS = ["ledger_index", "close_time", "close_time_iso", "tx_index", "tx_hash", "account", "tx_type",
             "result", "fee_drops", "sequence", "ticket"]
LED_FIELDS = ["ledger_index", "ledger_hash", "close_time", "close_time_iso", "txn_count", "tx_rows",
              "fetched_utc"]

# ledger -> [(hash, sender, type, result, fee)]
CHAIN = {
    98: [("OLD1", "rA", "Payment", "tesSUCCESS", 10)],
    100: [("A100", "rA", "Payment", "tesSUCCESS", 10), ("B100", "rB", "Payment", "tesSUCCESS", 10)],
    101: [],
    102: [("A102", "rA", "OfferCreate", "tecUNFUNDED_OFFER", 12)],
    103: [("B103", "rB", "Payment", "tesSUCCESS", 10)],
    104: [("C104", "rC", "AccountSet", "tesSUCCESS", 10)],
    106: [("A106", "rA", "Payment", "tesSUCCESS", 10)],
}
# account_tx also lists transactions where the account is only affected (e.g. a payment destination)
AFFECTS = {"B100": ["rA"], "B103": ["rC"]}


def history(account):
    out = []
    for li in sorted(CHAIN):
        for h, s, tt, res, fee in CHAIN[li]:
            if s == account or account in AFFECTS.get(h, []):
                out.append({"tx": {"hash": h, "Account": s, "TransactionType": tt, "Fee": str(fee),
                                   "ledger_index": li},
                            "meta": {"TransactionResult": res}, "validated": True})
    return out


class Handler(BaseHTTPRequestHandler):
    page = 2
    fail_first = 0
    seen = 0
    complete = ["1-1000"]  # server_info complete_ledgers per call; the last one repeats
    infos = 0

    @classmethod
    def oldest(cls):
        return int(cls.complete[min(cls.infos, len(cls.complete)) - 1].split("-")[0]) if cls.infos else 1

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        p = body["params"][0]
        Handler.seen += 1
        if Handler.seen <= Handler.fail_first:
            res = {"status": "error", "error": "slowDown"}
        elif body["method"] == "server_info":
            res = {"status": "success", "info": {"complete_ledgers": Handler.complete[
                min(Handler.infos, len(Handler.complete) - 1)]}}
            Handler.infos += 1
        elif body["method"] == "account_tx":
            # ignores ledger_index_min/max like the public RPC, but knows nothing it has pruned
            txs = [e for e in history(p["account"]) if e["tx"]["ledger_index"] >= Handler.oldest()]
            if not p.get("forward"):
                txs = txs[::-1]
            start = p.get("marker", 0)
            res = {"status": "success", "transactions": txs[start:start + Handler.page]}
            if start + Handler.page < len(txs):
                res["marker"] = start + Handler.page
        elif body["method"] == "ledger" and p["ledger_index"] < Handler.oldest():
            res = {"status": "error", "error": "lgrNotFound"}
        elif body["method"] == "ledger":
            li = p["ledger_index"]
            res = {"status": "success", "ledger": {"ledger_hash": f"H{li}",
                                                   "transactions": [t[0] for t in CHAIN.get(li, [])]}}
        else:
            res = {"status": "error", "error": "unknownCmd"}
        data = json.dumps({"result": res}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Handler.seen, Handler.fail_first, Handler.page = 0, 0, 2
    Handler.complete, Handler.infos = ["1-1000"], 0
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def write(path, fields, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def data(tmp_path):
    txs, leds = [], []
    for li in range(LO, HI + 1):
        rows = CHAIN.get(li, [])
        for i, (h, s, tt, res, fee) in enumerate(rows):
            txs.append({"ledger_index": li, "close_time": li * 4, "close_time_iso": "", "tx_index": i,
                        "tx_hash": h, "account": s, "tx_type": tt, "result": res, "fee_drops": fee,
                        "sequence": 1, "ticket": "n"})
        leds.append({"ledger_index": li, "ledger_hash": f"H{li}", "close_time": li * 4, "close_time_iso": "",
                     "txn_count": len(rows), "tx_rows": len(rows), "fetched_utc": ""})
    write(tmp_path / "tx.csv", TX_FIELDS, txs)
    write(tmp_path / "ledgers.csv", LED_FIELDS, leds)
    return tmp_path


def args(url, data, *extra):
    base = ["--data", str(data), "--rpc", url, "--gap", "0", "--count", "3", "--ledgers", "5"]
    return [*base, "--margin", "0", *extra]


def test_all_ok_against_window_ignoring_server(server, data, capsys):
    assert cx.main(args(server, data)) == 0
    out = capsys.readouterr().out
    assert out.count("OK      ") == 3 + 1 + 5 and "MISMATCH" not in out
    assert "S rA local=2 rpc=2" in out  # OLD1 (98) and A106 (106) dropped, B100 (rB) not counted for rA
    assert "S rC local=1 rpc=1" in out  # B103 affects rC but was sent by rB


def test_negative_control_every_check_mismatches(server, data, capsys):
    assert cx.main(args(server, data, "--negative")) == 1
    out = capsys.readouterr().out
    assert "OK      " not in out and out.count("MISMATCH") == 9


def test_detects_missing_local_row(server, data, capsys):
    rows = list(csv.DictReader(open(data / "tx.csv")))
    write(data / "tx.csv", TX_FIELDS, [r for r in rows if r["tx_hash"] != "A102"])
    assert cx.main(args(server, data)) == 1
    out = capsys.readouterr().out
    assert "MISMATCH S rA local=1 rpc=2" in out and "not_in_local=['A102']" in out
    assert "MISMATCH D slice 0 tx_rows=4 header_txn_count=5" in out


def test_detects_wrong_field_and_hash(server, data, capsys):
    rows = list(csv.DictReader(open(data / "tx.csv")))
    for r in rows:
        if r["tx_hash"] == "B103":
            r["result"] = "tecX"
    write(data / "tx.csv", TX_FIELDS, rows)
    leds = list(csv.DictReader(open(data / "ledgers.csv")))
    leds[1]["ledger_hash"] = "WRONG"
    write(data / "ledgers.csv", LED_FIELDS, leds)
    assert cx.main(args(server, data)) == 1
    out = capsys.readouterr().out
    assert "MISMATCH S rB" in out and "MISMATCH L 101 hash_equal=False" in out


def test_tx_rows_ne_txn_count_flagged(server, data, capsys):
    leds = list(csv.DictReader(open(data / "ledgers.csv")))
    leds[0]["tx_rows"] = "1"
    write(data / "ledgers.csv", LED_FIELDS, leds)
    assert cx.main(args(server, data)) == 1
    assert "ledgers_with_tx_rows_ne_txn_count=1" in capsys.readouterr().out


def test_newest_first_stops_below_window(server, data):
    rpc = cx.Rpc(server, gap=0, retry_errors=cx.RETRY, methods=cx.METHODS)
    Handler.page = 1
    got, pages, outside = cx.rpc_sent(rpc, "rA", LO, HI, limit=1)
    assert {g[0] for g in got} == {"A100", "A102"}
    # rA history newest first: A106, A102, B100, A100, OLD1 -> stops on the page with OLD1
    assert pages == 5 and outside == 2


def fast_rpc(monkeypatch, retries=6):
    real = cx.Rpc
    monkeypatch.setattr(cx, "Rpc", lambda *a, **k: real(*a, **{**k, "retries": retries, "backoff": 0}))


def test_retries_counted_not_mismatched(server, data, capsys, monkeypatch):
    fast_rpc(monkeypatch)
    Handler.fail_first = 2
    assert cx.main(args(server, data)) == 0
    out = capsys.readouterr().out
    assert "retries=2 retries_by_error={'slowDown': 2}" in out


def test_persistent_rpc_failure_exits_2(server, data, capsys, monkeypatch):
    fast_rpc(monkeypatch, retries=2)
    Handler.fail_first = 10**6
    assert cx.main(args(server, data)) == 2
    assert "RPC FAILURE" in capsys.readouterr().out


def test_checks_only_the_overlap_with_the_rpc_history(server, data, capsys, monkeypatch):
    fast_rpc(monkeypatch, retries=1)
    Handler.complete = ["102-1000"]
    assert cx.main(args(server, data)) == 0
    out = capsys.readouterr().out
    assert "checked 102-104 (3 ledgers, 3 tx) NOT CHECKABLE 2 ledgers, 2 tx" in out
    assert "S rA local=1 rpc=1" in out  # A100 is outside the RPC's history, A102 is checked
    assert "L 100 " not in out and "L 101 " not in out and out.count("L 10") == 3
    assert "MISMATCH" not in out and "RANGE end: public_rpc oldest 102" in out


def test_margin_keeps_clear_of_the_oldest_rpc_ledger(server, data, capsys):
    Handler.complete = ["100-1000"]
    assert cx.main(args(server, data)[:-2] + ["--margin", "2"]) == 0
    assert "margin 2 checked 102-104" in capsys.readouterr().out


def test_rpc_pruned_into_checked_span_exits_2(server, data, capsys):
    Handler.complete = ["100-1000", "103-1000"]
    assert cx.main(args(server, data)) == 2
    assert "pruned into the checked span" in capsys.readouterr().out


def test_no_overlap_exits_2(server, data, capsys):
    Handler.complete = ["200-1000"]
    assert cx.main(args(server, data)) == 2
    assert "no overlap" in capsys.readouterr().out
