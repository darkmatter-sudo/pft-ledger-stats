import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "objlife"))

import crosscheck_objlife as X  # noqa: E402
import headers_dormancy as H  # noqa: E402

HDR = {
    "ledger_index": 100,
    "ledger_hash": "AA" * 32,
    "account_hash": "BB" * 32,
    "close_time": 777,
    "deferred_rows": 2,
    "deferred": {"Offer": {"rpc": "offer"}, "Escrow": {"rpc": "escrow"}, "Check": {"rpc": "check"}},
}
ROWS = [
    {"index": "01" * 32, "LedgerEntryType": "Offer", "Flags": 0, "Sequence": 5},
    {"index": "02" * 32, "LedgerEntryType": "Escrow", "Flags": 0, "Amount": "10"},
]


def snap(tmp_path, rows=ROWS, header=HDR):
    p = tmp_path / "objlife-100.jsonl"
    p.write_text("".join(json.dumps(x) + "\n" for x in [dict(header, deferred_rows=len(rows))] + rows))
    return str(p)


class Fake:
    """A source holding `objs` at ledger 100. `fail` = {method: [errors to raise first]}."""

    def __init__(self, objs=ROWS, rng="50-200", fail=None, ledger_hash=HDR["ledger_hash"]):
        self.objs, self.rng, self.fail, self.h, self.calls = objs, rng, fail or {}, ledger_hash, []

    def call(self, method, params=None):
        self.calls.append(method)
        q = self.fail.get(method)
        if q:
            e = q.pop(0)
            raise e
        if method == "server_info":
            return {"info": {"complete_ledgers": self.rng}}
        if method == "ledger":
            return {"ledger": {"ledger_hash": self.h, "account_hash": HDR["account_hash"], "close_time": 777}}
        if method == "ledger_data":
            names = {"offer": "Offer", "escrow": "Escrow", "check": "Check"}
            sel = [o for o in self.objs if o["LedgerEntryType"] == names[params["type"]]]
            start = int(params.get("marker") or 0)
            r = {"ledger_hash": self.h, "state": sel[start : start + 1]}
            if start + 1 < len(sel):
                r["marker"] = str(start + 1)
            return r
        if method == "ledger_entry":
            for o in self.objs:
                if o["index"] == params["index"]:
                    return {"node": o}
            raise LookupError("entryNotFound")
        raise AssertionError(method)


def args(**kw):
    d = dict(retries=2, backoff=1.0, absent=3, seed="1", negative=False)
    d.update(kw)
    return argparse.Namespace(**d)


def go(tmp_path, rpc, **kw):
    lines, sleeps = [], []
    rc = X.run(
        rpc,
        snap(tmp_path, **{k: kw.pop(k) for k in ("rows",) if k in kw}),
        args(**kw),
        out=lines.append,
        sleep=sleeps.append,
    )
    return rc, lines, sleeps


def verdicts(lines):
    return [x.split()[0] + " " + x.split()[1] for x in lines if x.startswith(("MATCH", "MISMATCH"))]


def test_all_match(tmp_path):
    rc, lines, _ = go(tmp_path, Fake())
    assert rc == 0
    v = verdicts(lines)
    assert len(v) == 3 + 3 + 2 + 3 and all(x.startswith("MATCH") for x in v)
    assert "MATCH set:Check" in v  # an empty type is still checked


def test_negative_mismatches_every_check_including_empty_and_absent(tmp_path):
    rc, lines, _ = go(tmp_path, Fake(), negative=True)
    v = verdicts(lines)
    assert rc == 1 and len(v) == 11 and all(x.startswith("MISMATCH") for x in v)
    assert "MISMATCH set:Check" in v and "MISMATCH absent:0" in v


def test_missing_and_extra_objects(tmp_path):
    extra = {"index": "03" * 32, "LedgerEntryType": "Check", "Flags": 0}
    rc, lines, _ = go(tmp_path, Fake(objs=ROWS[:1] + [extra]))
    v = verdicts(lines)
    assert rc == 1
    assert "MISMATCH set:Escrow" in v and "MISMATCH set:Check" in v and "MATCH set:Offer" in v
    assert "MISMATCH obj:" + "02" * 32 in v  # entryNotFound for a snapshot object


def test_field_difference(tmp_path):
    changed = [ROWS[0], dict(ROWS[1], Amount="11")]
    rc, lines, _ = go(tmp_path, Fake(objs=changed))
    assert rc == 1 and any("diff=['Amount']" in x for x in lines)


def test_present_absent_probe_is_mismatch(tmp_path):
    ids = X.absent_ids("1", 1, {o["index"] for o in ROWS})
    rc, lines, _ = go(tmp_path, Fake(objs=ROWS + [{"index": ids[0], "LedgerEntryType": "Ticket"}]))
    assert rc == 1 and "MISMATCH absent:0" in verdicts(lines)


def test_out_of_range_stops_after_server_info(tmp_path):
    rpc = Fake(rng="150-200")
    rc, lines, _ = go(tmp_path, rpc)
    assert rc == 2 and rpc.calls == ["server_info"] and lines[0].startswith("OUT_OF_RANGE")


def test_backoff_logged_then_success(tmp_path):
    rpc = Fake(fail={"ledger": [H.Transient("http 503"), LookupError("lgrNotFound")]})
    rc, lines, sleeps = go(tmp_path, rpc)
    assert rc == 0 and sleeps == [1.0, 2.0]
    r = [x for x in lines if x.startswith("RETRY")]
    assert r == [
        "RETRY ledger try 1/3 error=http 503 sleep=1s",
        "RETRY ledger try 2/3 error=lgrNotFound sleep=2s",
    ]


def test_gave_up_is_exit_2_not_match(tmp_path):
    rpc = Fake(fail={"ledger": [H.Transient("net TimeoutError")] * 3})
    rc, lines, sleeps = go(tmp_path, rpc)
    assert rc == 2 and len(sleeps) == 2 and any(x.startswith("GAVE_UP ledger") for x in lines)
    assert not any("header." in x for x in lines)


def test_other_lookup_error_not_retried(tmp_path):
    rpc = Fake(fail={"ledger_entry": [LookupError("invalidParams")]})
    rc, lines, sleeps = go(tmp_path, rpc)
    assert rc == 1 and sleeps == [] and any("error=invalidParams" in x for x in lines)


def test_record_offline_same_verdicts_and_incomplete(tmp_path):
    rec = tmp_path / "rec.jsonl"
    live = []
    X.run(
        X.Recorder(Fake(fail={"ledger": [H.Transient("http 429")]}), str(rec)),
        snap(tmp_path),
        args(),
        out=live.append,
        sleep=lambda s: None,
    )
    off = []
    rc = X.run(X.Replay(str(rec)), snap(tmp_path), args(), out=off.append, sleep=lambda s: None)
    assert rc == 0 and verdicts(off) == verdicts(live)
    lines = rec.read_text().splitlines()
    rec.write_text("\n".join(lines[:-1]) + "\n")
    try:
        X.run(X.Replay(str(rec)), snap(tmp_path), args(), out=off.append, sleep=lambda s: None)
    except X.NotRecorded:
        pass
    else:
        raise AssertionError("missing call must raise NotRecorded")


def test_snapshot_row_count_checked(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(dict(HDR, deferred_rows=3)) + "\n" + "".join(json.dumps(r) + "\n" for r in ROWS))
    try:
        X.load_snapshot(str(p))
    except ValueError:
        return
    raise AssertionError("row count mismatch not detected")


# ---- sampled tx mode -------------------------------------------------------------------------

TXS = [
    {"ledger_index": 120, "tx_index": 0, "tx": {"hash": "C1" * 32},
     "meta": {"TransactionIndex": 0, "TransactionResult": "tesSUCCESS",
              "AffectedNodes": [{"DeletedNode": {"LedgerEntryType": "Offer", "LedgerIndex": "01" * 32}}]}},
    {"ledger_index": 130, "tx_index": 2, "tx": {"hash": "C2" * 32},
     "meta": {"TransactionIndex": 2, "TransactionResult": "tesSUCCESS",
              "AffectedNodes": [{"CreatedNode": {"LedgerEntryType": "Escrow", "LedgerIndex": "03" * 32}}]}},
]  # fmt: skip


class TxFake:
    def __init__(self, txs=TXS, rng="50-200", fail=None):
        self.by = {x["tx"]["hash"]: x for x in txs}
        self.rng, self.fail, self.calls = rng, fail or {}, []

    def call(self, method, params=None):
        self.calls.append(method)
        q = self.fail.get(method)
        if q:
            raise q.pop(0)
        if method == "server_info":
            return {"info": {"complete_ledgers": self.rng}}
        if method == "tx":
            x = self.by.get(params["transaction"])
            if x is None:
                raise LookupError("txnNotFound")
            return {"ledger_index": x["ledger_index"], "validated": True, "meta": x["meta"]}
        raise AssertionError(method)


def go_txs(tmp_path, rpc, local=TXS, hashes=None, **kw):
    d = tmp_path / "tx"
    d.mkdir(exist_ok=True)
    (d / "obj-tx.jsonl").write_text("".join(json.dumps(x) + "\n" for x in local))
    an = tmp_path / "analysis.json"
    an.write_text(json.dumps({"sampled_txs": {"hashes": hashes or [x["tx"]["hash"] for x in TXS]}}))
    lines = []
    rc = X.run_txs(rpc, str(an), str(d), args(**kw), out=lines.append, sleep=lambda s: None)
    return rc, lines


def test_txs_all_match(tmp_path):
    rc, lines = go_txs(tmp_path, TxFake())
    assert rc == 0 and verdicts(lines) == ["MATCH tx:" + "C1" * 32, "MATCH tx:" + "C2" * 32]


def test_txs_negative_every_line_mismatches(tmp_path):
    rc, lines = go_txs(tmp_path, TxFake(), negative=True)
    v = verdicts(lines)
    assert rc == 1 and len(v) == 2 and all(x.startswith("MISMATCH") for x in v)
    assert "diff=['TransactionIndex', 'ledger_index']" in lines[1]


def test_txs_meta_difference(tmp_path):
    other = json.loads(json.dumps(TXS))
    other[1]["meta"]["AffectedNodes"][0]["CreatedNode"]["LedgerIndex"] = "04" * 32
    rc, lines = go_txs(tmp_path, TxFake(txs=other))
    assert rc == 1 and "diff=['AffectedNodes']" in [x for x in lines if "C2C2" in x][0]


def test_txs_not_found_is_mismatch(tmp_path):
    rc, lines = go_txs(tmp_path, TxFake(txs=TXS[:1]))
    assert rc == 1 and any(x.startswith("MISMATCH tx:" + "C2" * 32 + " error=txnNotFound") for x in lines)


def test_txs_outside_retention_not_checkable(tmp_path):
    rc, lines = go_txs(tmp_path, TxFake(rng="125-200"))
    assert rc == 0 and any(x.startswith("NOT_CHECKABLE tx:" + "C1" * 32) for x in lines)
    assert lines[-1].endswith("checked=1 match=1 mismatch=0 not_checkable=1 gave_up=0")
    rc, lines = go_txs(tmp_path, TxFake(rng="140-200"))
    assert rc == 2  # nothing checkable is never a pass


def test_txs_retry_then_gave_up(tmp_path):
    rc, lines = go_txs(tmp_path, TxFake(fail={"tx": [H.Transient("http 503")] * 3}))
    assert rc == 2 and any(x.startswith("RETRY tx try 1/3") for x in lines)
    assert any(x.startswith("GAVE_UP tx") for x in lines)


def test_txs_local_copy_missing(tmp_path):
    rc, lines = go_txs(tmp_path, TxFake(), local=TXS[:1])
    assert rc == 2 and lines[0].startswith("LOCAL_MISSING 1")


def test_txs_record_offline(tmp_path):
    rec = tmp_path / "rec.jsonl"
    r = X.Recorder(TxFake(), str(rec))
    rc1, l1 = go_txs(tmp_path, r)
    r.fh.close()
    rc2, l2 = go_txs(tmp_path, X.Replay(str(rec)))
    assert rc1 == rc2 == 0 and verdicts(l1) == verdicts(l2)


# ---- window ledger mode ----------------------------------------------------------------------

TYPES = {"entries": [{"name": "Offer", "deferred": True}, {"name": "Escrow", "deferred": True},
                     {"name": "AccountRoot", "deferred": False}]}  # fmt: skip


def ptx(h, *types):
    return {
        "hash": h,
        "metaData": {"AffectedNodes": [{"ModifiedNode": {"LedgerEntryType": t}} for t in types]},
    }


# window A=100 .. B=106; 101 and 105 empty; 103 has one deferred (Offer) tx; 104 touches only AccountRoot
LEDGERS = {
    101: [],
    102: [ptx("D2" * 32, "AccountRoot")],
    103: [ptx("D3" * 32, "AccountRoot"), ptx("E3" * 32, "Offer", "AccountRoot")],
    104: [ptx("D4" * 32, "AccountRoot")],
    105: [],
    106: [ptx("D6" * 32, "AccountRoot", "AccountRoot")],
}
LOCAL_OBJ = [{"ledger_index": 103, "tx": {"hash": "E3" * 32}}]


class LFake:
    def __init__(self, ledgers=LEDGERS, rng="50-200", fail=None, hashes=None, closes=None):
        self.ledgers, self.rng, self.fail, self.calls = ledgers, rng, fail or {}, []
        self.hashes, self.closes = hashes or {}, closes or {}

    def call(self, method, params=None):
        self.calls.append((method, params))
        q = self.fail.get(method)
        if q:
            raise q.pop(0)
        if method == "server_info":
            return {"info": {"complete_ledgers": self.rng}}
        if method == "ledger":
            L = params["ledger_index"]
            assert params["transactions"] is True and params["expand"] is True
            h, ct = self.hashes.get(L, f"{L:064X}"), self.closes.get(L, L * 4)
            return {"ledger": {"ledger_hash": h, "close_time": ct, "transactions": self.ledgers[L]}}
        raise AssertionError(method)


def go_ledgers(tmp_path, rpc, rows=None, local=LOCAL_OBJ, lo=100, hi=106, **kw):
    d = tmp_path / "tx"
    d.mkdir(exist_ok=True)
    rows = rows if rows is not None else [(L, len(t)) for L, t in sorted(LEDGERS.items())] + [(107, 3)]
    (d / "ledgers.csv").write_text(
        "ledger_index,ledger_hash,close_time,close_time_iso,txn_count,obj_tx_count,fetched_utc\n"
        + "".join(f"{L},{L:064X},{L * 4},x,{n},0,x\n" for L, n in rows)
    )
    (d / "obj-tx.jsonl").write_text("".join(json.dumps(x) + "\n" for x in local))
    t = tmp_path / "types.json"
    t.write_text(json.dumps(TYPES))
    kw = dict(dict(sample=10, empty=10), **kw)
    lines = []
    rc = X.run_ledgers(rpc, str(d), str(t), lo, hi, args(**kw), out=lines.append, sleep=lambda s: None)
    return rc, lines


def lverdicts(lines):
    return [x for x in lines if x.startswith(("MATCH", "MISMATCH"))]


def test_ledgers_all_match_and_only_deferred_types_counted(tmp_path):
    rc, lines = go_ledgers(tmp_path, LFake())
    v = lverdicts(lines)
    assert rc == 0 and len(v) == 6 and all(x.startswith("MATCH") for x in v)
    assert "MATCH ledger:103 txs=2 deferred=1 diff=[]" in v
    assert "MATCH ledger:104 txs=1 deferred=0 diff=[]" in v  # AccountRoot is not deferred
    assert "window 101-106 (6 ledgers, 4 with txs) | sampled 6" in lines[0]
    assert lines[-1] == "SUMMARY ledgers=6 checked=6 match=6 mismatch=0 not_checkable=0 gave_up=0"


def test_ledgers_negative_every_line_mismatches(tmp_path):
    rc, lines = go_ledgers(tmp_path, LFake(), negative=True)
    v = lverdicts(lines)
    assert rc == 1 and len(v) == 6 and all(x.endswith("diff=['close_time', 'txn_count']") for x in v)


def test_ledgers_deferred_tx_missed_by_watcher(tmp_path):
    rc, lines = go_ledgers(tmp_path, LFake(), local=[])
    assert rc == 1 and lverdicts(lines) == [
        x if "ledger:103" not in x else "MISMATCH ledger:103 txs=2 deferred=1 diff=['deferred_txs']"
        for x in lverdicts(go_ledgers(tmp_path, LFake())[1])
    ]


def test_ledgers_isolated_hash_close_count_differences(tmp_path):
    rc, lines = go_ledgers(tmp_path, LFake(hashes={102: "FF" * 32}))
    assert rc == 1 and [x for x in lverdicts(lines) if x.startswith("MISMATCH")] == [
        "MISMATCH ledger:102 txs=1 deferred=0 diff=['ledger_hash']"
    ]
    rc, lines = go_ledgers(tmp_path, LFake(closes={105: 1}))
    assert rc == 1 and [x for x in lverdicts(lines) if x.startswith("MISMATCH")] == [
        "MISMATCH ledger:105 txs=0 deferred=0 diff=['close_time']"
    ]
    more = {**LEDGERS, 106: LEDGERS[106] + [ptx("D7" * 32, "AccountRoot")]}
    rc, lines = go_ledgers(tmp_path, LFake(ledgers=more))
    assert rc == 1 and [x for x in lverdicts(lines) if x.startswith("MISMATCH")] == [
        "MISMATCH ledger:106 txs=2 deferred=0 diff=['txn_count']"
    ]


def test_ledgers_outside_retention_not_checkable(tmp_path):
    rc, lines = go_ledgers(tmp_path, LFake(rng="104-200"))
    assert rc == 0 and sum(x.startswith("NOT_CHECKABLE ledger:") for x in lines) == 3
    assert lines[-1].endswith("checked=3 match=3 mismatch=0 not_checkable=3 gave_up=0")
    rpc = LFake(rng="150-200")
    rc, lines = go_ledgers(tmp_path, rpc)
    assert rc == 2  # nothing checkable is never a pass
    assert [c[0] for c in rpc.calls] == ["server_info"]


def test_ledgers_local_gap_refused_and_rows_outside_window_ignored(tmp_path):
    rows = [(L, len(t)) for L, t in sorted(LEDGERS.items()) if L != 104]
    rpc = LFake()
    rc, lines = go_ledgers(tmp_path, rpc, rows=rows)
    assert (
        rc == 2
        and lines == ["LOCAL_INCOMPLETE ledgers.csv has 5 of 6 ledgers in 101-106"]
        and rpc.calls == []
    )
    rc, lines = go_ledgers(
        tmp_path, LFake(), rows=[(100, 9)] + [(L, len(t)) for L, t in sorted(LEDGERS.items())]
    )
    assert rc == 0 and len(lverdicts(lines)) == 6  # A itself and B+1 are not window ledgers


def test_ledgers_pick_is_seeded_and_split_busy_idle(tmp_path):
    rows = [{"ledger_index": str(i)} for i in range(1000)]
    a, b, c = X.pick(rows, 5, "1"), X.pick(rows, 5, "1"), X.pick(rows, 5, "2")
    assert a == b and a != c and len(a) == 5
    assert [r["ledger_index"] for r in a] == sorted((r["ledger_index"] for r in a), key=int)
    rc, lines = go_ledgers(tmp_path, LFake(), sample=2, empty=1)
    v = lverdicts(lines)
    idle = [x for x in v if " txs=0 " in x]
    assert rc == 0 and len(v) == 3 and len(idle) == 1
    rc2, lines2 = go_ledgers(tmp_path, LFake(), sample=2, empty=1)
    assert lverdicts(lines2) == v


def test_ledgers_retry_then_gave_up(tmp_path):
    rc, lines = go_ledgers(tmp_path, LFake(fail={"ledger": [H.Transient("http 503")] * 3}))
    assert rc == 2 and any(x.startswith("RETRY ledger try 1/3") for x in lines)
    assert any(x.startswith("GAVE_UP ledger") for x in lines) and "gave_up=1" in lines[-1]


def test_ledgers_record_offline(tmp_path):
    rec = tmp_path / "rec.jsonl"
    r = X.Recorder(LFake(), str(rec))
    rc1, l1 = go_ledgers(tmp_path, r)
    r.fh.close()
    rc2, l2 = go_ledgers(tmp_path, X.Replay(str(rec)))
    assert rc1 == rc2 == 0 and lverdicts(l1) == lverdicts(l2)
