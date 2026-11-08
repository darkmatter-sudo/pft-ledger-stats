"""Tests for dormancy/crosscheck_dormancy.py: fields, tx checks, ranges, retries, negative, record/offline."""

import copy
import gzip
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dormancy"))

import crosscheck_dormancy as X  # noqa: E402
import headers_dormancy as H  # noqa: E402

SNAP = 1000


def root(i, prev):
    return {
        "Account": f"rAcct{i:03d}",
        "Balance": str(10_000_000 + i),
        "Flags": 0,
        "LedgerEntryType": "AccountRoot",
        "OwnerCount": i % 3,
        "PreviousTxnID": f"{i:064X}",
        "PreviousTxnLgrSeq": prev,
        "Sequence": 5 + i,
        "index": f"{0xA000 + i:064X}",
    }


def fixture(tmp_path, n=6):
    objs = [root(i, 990 if i % 2 else 100) for i in range(n)]  # odd: prev in range, even: pruned
    objs.append({"LedgerEntryType": "FeeSettings", "index": "F" * 64})
    p = tmp_path / "snap.jsonl.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(json.dumps({"ledger_index": SNAP, "ledger_hash": "H" * 64, "close_time": 5000}) + "\n")
        for o in objs:
            fh.write(json.dumps(o) + "\n")
    hp = tmp_path / "headers.jsonl"
    hp.write_text(json.dumps({"ledger_index": 990, "close_time": 4900, "ledger_hash": "X" * 64}) + "\n")
    return str(p), str(hp), [o for o in objs if o["LedgerEntryType"] == "AccountRoot"]


class Fake:
    """Answers like a node holding ledgers 900-1000; `faults` maps (method, key) -> list of errors."""

    def __init__(self, roots, ranges="900-1000", faults=None, edit=None):
        self.roots = {o["Account"]: copy.deepcopy(o) for o in roots}
        self.by_tx = {o["PreviousTxnID"]: o for o in self.roots.values()}
        self.ranges, self.faults, self.count, self.calls = ranges, faults or {}, 0, []
        for acct, fields in (edit or {}).items():
            self.roots[acct].update(fields)

    def call(self, method, params=None):
        self.count += 1
        self.calls.append(method)
        key = (method, (params or {}).get("account") or (params or {}).get("transaction"))
        if self.faults.get(key):
            err = self.faults[key].pop(0)
            if err in H.TRANSIENT or err.startswith("http"):
                raise H.Transient(err)
            raise LookupError(err)
        if method == "server_info":
            return {"info": {"complete_ledgers": self.ranges}}
        if method == "account_info":
            if params["account"] not in self.roots:
                raise LookupError("actNotFound")
            return {"account_data": self.roots[params["account"]], "ledger_index": params["ledger_index"]}
        if method == "tx":
            o = self.by_tx.get(params["transaction"])
            if o is None:
                raise LookupError("txnNotFound")
            meta = {
                "AffectedNodes": [
                    {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "LedgerIndex": o["index"]}}
                ]
            }
            return {"validated": True, "ledger_index": o["PreviousTxnLgrSeq"], "date": 4900, "meta": meta}
        raise AssertionError(method)


def go(snap, hp, rpc, count=6, negative=False):
    out = io.StringIO()
    code, c = X.run(snap, hp, rpc, count, 1, negative, out, retries=3, backoff=0, sleep=lambda s: None)
    return code, c, out.getvalue()


def test_positive_all_match(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    code, c, text = go(snap, hp, Fake(roots))
    assert code == 0, text
    assert c["accounts"] == 6 and c["fields_mismatch"] == 0 and c["fields_ok"] == 30
    assert c["tx_checked"] == 3 and c["tx_ok"] == 3 and c["tx_out_of_range"] == 3 and c["date_checked"] == 3
    assert "RESULT: all match -> exit 0" in text


def test_each_field_mismatch_is_named(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    for field, val in [
        ("Balance", "1"),
        ("OwnerCount", 9),
        ("Sequence", 1),
        ("PreviousTxnID", "0" * 64),
        ("PreviousTxnLgrSeq", 1),
    ]:
        code, c, text = go(snap, hp, Fake(roots, edit={"rAcct002": {field: val}}))
        assert code == 1 and c["fields_mismatch"] == 1, (field, text)
        assert f"MISMATCH {field}" in text


def test_missing_owner_count_means_zero(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    rpc = Fake(roots)
    del rpc.roots["rAcct000"]["OwnerCount"]  # OwnerCount 0 in the snapshot, absent in the answer
    assert go(snap, hp, rpc)[0] == 0


def test_tx_wrong_ledger_wrong_date_and_untouched_root(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    for patch, name in [
        (lambda r: r.update(ledger_index=989), "ledger_index"),
        (lambda r: r.update(date=4901), "date"),
        (lambda r: r.update(validated=False), "validated"),
        (lambda r: r["meta"]["AffectedNodes"].clear(), "touches_root"),
    ]:
        rpc = Fake(roots)
        orig = rpc.call

        def call(m, p=None, orig=orig, patch=patch):
            r = orig(m, p)
            if m == "tx" and p["transaction"] == f"{1:064X}":
                patch(r)
            return r

        rpc.call = call
        code, c, text = go(snap, hp, rpc)
        assert code == 1 and c["tx_mismatch"] == 1, (name, text)
        assert f"MISMATCH {name}" in text


def test_act_not_found_is_a_mismatch_not_a_crash(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    rpc = Fake(roots)
    del rpc.roots["rAcct003"]
    code, c, text = go(snap, hp, rpc)
    assert code == 1 and c["fields_mismatch"] == 5 and "(actNotFound)" in text


def test_snapshot_outside_complete_ledgers_exits_2(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    rpc = Fake(roots, ranges="1001-2000")
    code, c, text = go(snap, hp, rpc)
    assert code == 2 and c == {"out_of_range": True}
    assert rpc.calls == ["server_info"]  # nothing else asked


def test_tx_only_inside_range(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    rpc = Fake(roots, ranges="995-1000")  # prev 990 now pruned too
    code, c, _ = go(snap, hp, rpc)
    assert code == 0 and c["tx_checked"] == 0 and c["tx_out_of_range"] == 6 and "tx" not in rpc.calls


def test_transient_and_in_range_not_found_are_retried(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    faults = {("account_info", "rAcct001"): ["lgrNotFound", "internal"], ("tx", f"{1:064X}"): ["txnNotFound"]}
    code, c, _ = go(snap, hp, Fake(roots, faults=faults))
    assert code == 0 and c["retries"] == 3
    assert c["retries_by_error"] == {"lgrNotFound": 1, "internal": 1, "txnNotFound": 1}


def test_gave_up_account_is_replaced(tmp_path):
    snap, hp, roots = fixture(tmp_path, n=8)
    faults = {("account_info", o["Account"]): ["tooBusy"] * 10 for o in roots[:1]}
    code, c, text = go(snap, hp, Fake(roots, faults=faults), count=6)
    assert code == 0 and c["accounts"] == 6 and c["skipped"] == 1 and "SKIP (replaced)" in text


def test_not_enough_accounts_exit_2(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    code, c, _ = go(snap, hp, Fake(roots), count=7)
    assert code == 2 and c["accounts"] == 6


def test_negative_every_field_and_tx_mismatch(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    code, c, text = go(snap, hp, Fake(roots), negative=True)
    assert code == 1
    assert c["fields_mismatch"] == 30 and c["fields_ok"] == 0
    assert c["tx_mismatch"] == 3 and c["tx_ok"] == 0
    assert "negative control PASS" in text and "OK lines 0" in text
    for line in text.splitlines():  # every compared tx check named, incl. the absent-root case
        if " tx    " in line:
            assert "MISMATCH validated,ledger_index,touches_root,date" in line, line


def test_negative_owner_count_absent_still_mismatches(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    for o in roots:
        o.pop("OwnerCount")
    rpc = Fake(roots)
    with gzip.open(snap, "wt") as fh:  # rewrite the snapshot without OwnerCount
        fh.write(json.dumps({"ledger_index": SNAP, "ledger_hash": "H" * 64, "close_time": 5000}) + "\n")
        for o in roots:
            fh.write(json.dumps(o) + "\n")
    _code, c, _ = go(snap, hp, rpc, negative=True)
    assert c["fields_mismatch"] == 30


def test_record_then_offline_replays_same_verdicts(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    faults = {("account_info", "rAcct001"): ["lgrNotFound"]}
    rec_path = tmp_path / "rec.jsonl"
    live = X.Recorder(Fake(roots, faults=faults, edit={"rAcct004": {"Balance": "7"}}), str(rec_path))
    code_l, c_l, text_l = go(snap, hp, live)
    live.fh.close()
    off = X.Replay(str(rec_path))
    code_o, c_o, text_o = go(snap, hp, off)
    assert code_l == code_o == 1
    drop = ("rpc_calls", "retries", "retries_by_error")
    assert {k: v for k, v in c_l.items() if k not in drop} == {k: v for k, v in c_o.items() if k not in drop}
    assert [ln for ln in text_l.splitlines() if "SUMMARY" not in ln] == [
        ln for ln in text_o.splitlines() if "SUMMARY" not in ln
    ]


def test_offline_unrecorded_call_fails_loudly(tmp_path):
    snap, hp, roots = fixture(tmp_path)
    rec_path = tmp_path / "rec.jsonl"
    rec_path.write_text(
        json.dumps(
            {"method": "server_info", "params": {}, "result": {"info": {"complete_ledgers": "900-1000"}}}
        )
        + "\n"
    )
    try:
        go(snap, hp, X.Replay(str(rec_path)))
    except X.NotRecorded as e:
        assert "not in recording" in str(e)
    else:
        raise AssertionError("expected NotRecorded")


def test_read_only_allowlists():
    for rpc in (H.HttpRpc("http://127.0.0.1:9", allow=X.READ_ONLY), X.Replay(os.devnull)):
        for m in ("submit", "sign", "wallet_propose", "ledger_accept"):
            try:
                rpc.call(m, {})
            except ValueError as e:
                assert "refusing" in str(e)
            else:
                raise AssertionError(m)
    assert X.READ_ONLY == {"server_info", "account_info", "tx"}


def test_in_range_only_samples_held_ledgers(tmp_path):
    snap, hp, roots = fixture(tmp_path, n=8)
    out = io.StringIO()
    code, c = X.run(snap, hp, Fake(roots), 4, 1, False, out, retries=0, backoff=0, in_range_only=True)
    assert code == 0 and c["accounts"] == 4 and c["tx_checked"] == 4 and c["tx_out_of_range"] == 0
    assert "--in-range-only: 4 AccountRoots" in out.getvalue()
    code, c = X.run(
        snap, hp, Fake(roots), 5, 1, False, io.StringIO(), retries=0, backoff=0, in_range_only=True
    )
    assert code == 2 and c["accounts"] == 4  # fewer held accounts than asked: not enough
