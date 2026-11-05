import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "growth"))
sys.path.insert(0, os.path.dirname(__file__))
import collect_growth as cg  # noqa: E402
import crosscheck_growth as xc  # noqa: E402
from test_growth_collect import SPEC, pair  # noqa: E402

FIRST, LAST = 100, 119


def specs_for(i):
    """Every third ledger carries SPEC's two transactions (6 node rows), the rest are empty."""
    return SPEC if i % 3 == 0 else []


def write_collection(d, rows_of=None):
    """Write ledgers/tx/nodes.csv for FIRST..LAST exactly the way the collector does."""
    rows_of = rows_of or (lambda i: cg.growth_rows(*pair(i, specs_for(i))))
    led_rows, tx_rows, node_rows = [], [], []
    for i in range(FIRST, LAST + 1):
        led, txs, nodes = rows_of(i)
        led_rows.append({**led, "source": "local", "fetched_utc": "2026-11-04T00:00:00Z"})
        tx_rows += txs
        node_rows += nodes
    for name, fields, rows in (
        ("ledgers", cg.LEDGER_FIELDS, led_rows),
        ("tx", cg.TX_FIELDS, tx_rows),
        ("nodes", cg.NODE_FIELDS, node_rows),
    ):
        with open(os.path.join(d, f"{name}.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)


class FakeRpc:
    """Public RPC stand-in: server_info ranges in order (last one repeats), ledger answers from pair()."""

    def __init__(self, ranges=("1-1000",), bad_join=None, answers=None):
        self.ranges = list(ranges)
        self.bad_join = dict(bad_join or {})  # ledger -> number of disagreeing answer pairs first
        self.answers = answers or (lambda i: pair(i, specs_for(i)))
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "server_info":
            r = self.ranges.pop(0) if len(self.ranges) > 1 else self.ranges[0]
            return {"info": {"complete_ledgers": r}}
        assert method == "ledger"
        i = params["ledger_index"]
        j, b = self.answers(i)
        if self.bad_join.get(i, 0) > 0:
            if params.get("binary"):
                return {"ledger": {**b, "ledger_data": b["ledger_data"][:-2] + "FF"}}
            self.bad_join[i] -= 1
        return {"ledger": b if params.get("binary") else j}


def run(tmp_path, rpc=None, **kw):
    lines = []
    res = xc.run(str(tmp_path), kw.pop("count", 10), "s1", rpc=rpc, out=lines.append, margin=0, **kw)
    return res, lines


def test_equal_collection_is_all_ok_and_stratified(tmp_path):
    write_collection(tmp_path)
    res, lines = run(tmp_path, FakeRpc())
    assert res == {"ok": 10, "mismatch": 0}
    sample = next(x for x in lines if x.startswith("SAMPLE"))
    picked = [int(x) for x in sample.rsplit("): ", 1)[1].split()]
    busy = [i for i in range(FIRST, LAST + 1) if i % 3 == 0]  # 102, 105, ..., 117: 6 of 20
    assert len(picked) == 10 and len(set(picked)) == 10
    assert sum(1 for i in picked if i in busy) >= 5  # count // 2 drawn from ledgers with transactions
    assert all(x.startswith("OK") for x in lines if x.startswith(("OK", "MISMATCH")))


def test_hand_computed_ok_line(tmp_path):
    write_collection(tmp_path)
    _, lines = run(tmp_path, FakeRpc(), count=20)
    line = next(x for x in lines if x.startswith("OK       102 "))
    # SPEC by hand: 2 tx, header 118, blob 182+200, meta 322+500, 2+4 node rows
    assert line == "OK       102 txn_count=2 header=118 blob=382 meta=822 nodes=6"


@pytest.mark.parametrize(
    "field,mutate",
    [
        ("meta_bytes", lambda led, txs, nodes: led.update(meta_bytes=led["meta_bytes"] + 1)),
        ("ledger_hash", lambda led, txs, nodes: led.update(ledger_hash="AB" * 32)),
        ("tx_rows", lambda led, txs, nodes: txs[0].update(result="tecPATH_DRY")),
        ("node_rows", lambda led, txs, nodes: nodes[0].update(entry_type="Offer")),
        ("node_rows", lambda led, txs, nodes: nodes.append(dict(nodes[0]))),
    ],
)
def test_each_difference_is_named(tmp_path, field, mutate):
    def rows_of(i):
        led, txs, nodes = cg.growth_rows(*pair(i, specs_for(i)))
        if i == 102:
            mutate(led, txs, nodes)
        return led, txs, nodes

    write_collection(tmp_path, rows_of)
    res, lines = run(tmp_path, FakeRpc(), count=20)
    assert res == {"ok": 19, "mismatch": 1}
    bad = next(x for x in lines if x.startswith("MISMATCH"))
    assert bad.startswith("MISMATCH 102 ") and bad.endswith(f"differ={field}")


def test_negative_control_mismatches_every_ledger_on_all_three(tmp_path):
    write_collection(tmp_path)
    res, lines = run(tmp_path, FakeRpc(), count=10, negative=True)
    assert res == {"ok": 0, "mismatch": 10}
    assert all(x.endswith("differ=header_bytes,tx_rows,node_rows") for x in lines if x.startswith("MISMATCH"))


def test_record_then_offline_without_rpc(tmp_path):
    write_collection(tmp_path)
    rec = tmp_path / "rec.json"
    res, lines = run(tmp_path, FakeRpc(), count=6, record=str(rec))
    assert res["ok"] == 6
    data = json.loads(rec.read_text())
    assert len(data["ledgers"]) == 6 and all(
        len(v["raw_sha256"]["json"]) == 64 for v in data["ledgers"].values()
    )
    res2, lines2 = run(tmp_path, None, offline=str(rec))
    assert res2 == {"ok": 6, "mismatch": 0}
    assert lines2[0].startswith("OFFLINE rec.json: 6 recounts")
    # offline also catches a CSV changed after the recording
    with open(tmp_path / "tx.csv") as fh:
        body = fh.read()
    (tmp_path / "tx.csv").write_text(body.replace("tesSUCCESS", "tecFAILED"))
    res3, _ = run(tmp_path, None, offline=str(rec))
    n_busy = sum(1 for v in data["ledgers"].values() if v["ledger"]["txn_count"] > 0)
    assert n_busy >= 3 and res3 == {"ok": 6 - n_busy, "mismatch": n_busy}


def test_refetch_on_disagreeing_public_answers(tmp_path):
    write_collection(tmp_path)
    res, lines = run(tmp_path, FakeRpc(bad_join={102: 2}), count=20)
    assert res == {"ok": 20, "mismatch": 0}
    assert sum(1 for x in lines if x.startswith("REFETCH ledger 102")) == 2


def test_persistent_disagreement_is_rpc_failure_not_mismatch(tmp_path):
    write_collection(tmp_path)
    with pytest.raises(xc.RpcError, match="still disagree after 3"):
        run(tmp_path, FakeRpc(bad_join={102: 5}), count=20)


def test_range_overlap_and_not_checkable(tmp_path):
    write_collection(tmp_path)
    res, lines = run(tmp_path, FakeRpc(ranges=("110-5000",)), count=20)
    rng = next(x for x in lines if x.startswith("RANGE collection"))
    assert "checked 110-119 (10 ledgers" in rng and "NOT CHECKABLE 10 ledgers" in rng
    assert res["ok"] == 10
    assert all(int(x.split()[1]) >= 110 for x in lines if x.startswith("OK"))


def test_no_overlap_and_pruned_during_run(tmp_path):
    write_collection(tmp_path)
    with pytest.raises(xc.RpcError, match="no overlap"):
        run(tmp_path, FakeRpc(ranges=("500-900",)))
    with pytest.raises(xc.RpcError, match="pruned into the checked span"):
        run(tmp_path, FakeRpc(ranges=("1-1000", "105-1000")))


def test_torn_last_row_of_a_live_collection_is_skipped(tmp_path):
    write_collection(tmp_path)
    with open(tmp_path / "ledgers.csv", "a") as fh:
        fh.write("120,ABCD")  # collector mid-write
    res, lines = run(tmp_path, FakeRpc(), count=20)
    assert "NOTE skipped 1 incomplete ledgers.csv row(s) (collector still writing)" in lines
    assert res == {"ok": 20, "mismatch": 0}


def test_only_read_only_methods():
    assert set(xc.METHODS) == {"server_info", "ledger"}


def test_retry_with_exponential_backoff_is_printed_and_counted(tmp_path, capsys):
    """The real public-RPC client (no network): lgrNotFound twice, then a transport error, then OK."""
    import urllib.error

    write_collection(tmp_path)
    fake = FakeRpc()
    failures = {"lgrNotFound": 2, "transport": 1}
    sleeps = []

    def post(body):
        req = json.loads(body)
        params = req["params"][0]
        if req["method"] == "ledger" and params["ledger_index"] == 102 and params.get("binary"):
            if failures["lgrNotFound"]:
                failures["lgrNotFound"] -= 1
                return {"result": {"status": "error", "error": "lgrNotFound"}}
            if failures["transport"]:
                failures["transport"] -= 1
                raise urllib.error.URLError("connection reset")
        return {"result": {"status": "success", **fake.call(req["method"], params)}}

    rpc = xc.Rpc(
        "https://rpc.invalid",
        gap=0,
        retry_errors=xc.RETRY,
        methods=xc.METHODS,
        post=post,
        sleep=sleeps.append,
    )
    res, _ = run(tmp_path, rpc, count=20)
    assert res == {"ok": 20, "mismatch": 0}
    retry = [x for x in capsys.readouterr().out.splitlines() if " RETRY ledger " in x]
    assert [x.split("RETRY ", 1)[1] for x in retry] == [
        "ledger attempt 1/6 error=lgrNotFound backoff=2.0s",
        "ledger attempt 2/6 error=lgrNotFound backoff=4.0s",
        "ledger attempt 3/6 error=URLError: <urlopen error connection reset> backoff=8.0s",
    ]
    assert [s for s in sleeps if s] == [2.0, 4.0, 8.0]
    assert rpc.retry_kinds == {"lgrNotFound": 2, "transport": 1}
