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
