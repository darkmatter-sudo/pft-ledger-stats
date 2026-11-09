import argparse
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "objlife"))

import collect_objlife as C  # noqa: E402

MACRO = """\
LEDGER_ENTRY(ltCHECK, 0x0043, Check, check, ({
    {sfAccount,              soeREQUIRED},
    {sfExpiration,           soeOPTIONAL},
}))
LEDGER_ENTRY(ltTICKET, 0x0054, Ticket, ticket, ({
    {sfTicketSequence,       soeREQUIRED},
}))
LEDGER_ENTRY(ltACCOUNT_ROOT, 0x0061, AccountRoot, account, ({
    {sfBalance,              soeREQUIRED},
}))
LEDGER_ENTRY_DUPLICATE(ltDEPOSIT_PREAUTH, 0x0070, DepositPreauth, deposit_preauth, ({
    {sfAuthorize,            soeOPTIONAL},
}))
LEDGER_ENTRY(ltESCROW, 0x0075, Escrow, escrow, ({ {sfCancelAfter, soeOPTIONAL}, {sfFinishAfter, soeOPTIONAL},
}))
"""

STATE = {
    "ledger_index": 10,
    "ledger_hash": "H10",
    "account_hash": "AH",
    "total_coins": 1,
    "close_time": 5,
    "close_time_iso": "2000-01-01T00:00:05Z",
    "server_state_reserve_base": 10,
    "server_state_reserve_inc": 2,
    "server_state_validated_seq": 11,
}


def types_doc():
    entries = C.classify(C.parse_macro(MACRO))
    return {"commit": "c0ffee", "macro_sha256": "m", "entries": entries}, [
        e for e in entries if e["deferred"]
    ]


def test_parse_macro_lines_codes_fields():
    e = C.parse_macro(MACRO)
    assert [(x["name"], x["rpc"], x["code"], x["line"]) for x in e] == [
        ("Check", "check", 0x43, 1),
        ("Ticket", "ticket", 0x54, 5),
        ("AccountRoot", "account", 0x61, 8),
        ("DepositPreauth", "deposit_preauth", 0x70, 11),
        ("Escrow", "escrow", 0x75, 14),
    ]
    assert e[0]["fields"] == {"sfAccount": "soeREQUIRED", "sfExpiration": "soeOPTIONAL"}
    # two fields on the entry line itself are read too
    assert set(e[4]["fields"]) == {"sfCancelAfter", "sfFinishAfter"}


def test_classify_rule():
    _, d = types_doc()
    got = {e["name"]: e["reason"] for e in d}
    assert got == {
        "Check": "time-bound field sfExpiration",
        "Ticket": C.NAMED["Ticket"],
        "DepositPreauth": C.NAMED["DepositPreauth"],
        "Escrow": "time-bound field sfCancelAfter,sfFinishAfter",
    }


def test_types_on_real_macro_if_present():
    src = os.environ.get("POSTFIATD_SRC", "")
    p = os.path.join(src, C.MACRO)
    if not os.path.exists(p):
        pytest.skip("set POSTFIATD_SRC to a postfiatd checkout at the build commit")
    with open(os.path.join(HERE, "..", "objlife", "data", "types.json")) as fh:
        committed = json.load(fh)
    with open(p, "rb") as fh:
        raw = fh.read()
    assert hashlib.sha256(raw).hexdigest() == committed["macro_sha256"]
    assert C.classify(C.parse_macro(raw.decode())) == committed["entries"]


def objs():
    return [
        {"index": "B2", "LedgerEntryType": "Check"},
        {"index": "A1", "LedgerEntryType": "AccountRoot"},
        {"index": "A0", "LedgerEntryType": "Check"},
        {"index": "C3", "LedgerEntryType": "Ticket"},
    ]


def test_build_sorts_counts_and_header():
    doc, d = types_doc()
    tc = {"check": ["A0", "B2"], "ticket": ["C3"], "deposit_preauth": [], "escrow": []}
    h, rows = C.build(objs(), STATE, doc, d, tc)
    assert [r["index"] for r in rows] == ["A0", "B2", "C3"]
    assert h["counts_all_types"] == {"Check": 2, "AccountRoot": 1, "Ticket": 1}
    assert h["deferred"]["Check"] == {"rpc": "check", "full_tree": 2, "type_filter": 2}
    assert h["deferred_rows"] == 3 and h["objects_paged"] == 4
    assert "server_state_validated_seq" not in h and h["source_commit"] == "c0ffee"


@pytest.mark.parametrize(
    "tc",
    [
        {"check": ["A0"], "ticket": ["C3"], "deposit_preauth": [], "escrow": []},  # count differs
        {
            "check": ["A0", "ZZ"],
            "ticket": ["C3"],
            "deposit_preauth": [],
            "escrow": [],
        },  # same count, other index
        {"check": ["A0", "B2"], "ticket": [], "deposit_preauth": [], "escrow": []},  # type filter misses one
        {"check": ["A0", "B2"], "ticket": ["C3"], "deposit_preauth": ["X"], "escrow": []},  # absent locally
    ],
)
def test_build_refuses_count_disagreement(tc):
    doc, d = types_doc()
    with pytest.raises(ValueError):
        C.build(objs(), STATE, doc, d, tc)


def test_write_lines_deterministic(tmp_path):
    doc, d = types_doc()
    tc = {"check": ["A0", "B2"], "ticket": ["C3"], "deposit_preauth": [], "escrow": []}
    h, rows = C.build(objs(), STATE, doc, d, tc)
    a = C.write_lines(str(tmp_path / "a"), h, rows)
    h2, rows2 = C.build(list(reversed(objs())), dict(STATE, server_state_validated_seq=99), doc, d, tc)
    b = C.write_lines(str(tmp_path / "b"), h2, rows2)
    assert a == b == hashlib.sha256((tmp_path / "a").read_bytes()).hexdigest()


class FakeRpc:
    """ledger_data over a fixed object list, pages of `limit`, optional type filter."""

    def __init__(self, objects, ledger_hash="H10", fail_after=None):
        self.objects, self.h, self.count, self.fail_after = objects, ledger_hash, 0, fail_after

    def call(self, method, params=None):
        self.count += 1
        if self.fail_after is not None and self.count > self.fail_after:
            raise RuntimeError("node went away")
        if method == "server_state":
            return {"state": {"validated_ledger": {"seq": 10, "reserve_base": 10, "reserve_inc": 2}}}
        if method == "ledger":
            return {
                "ledger": {
                    "ledger_index": "10",
                    "ledger_hash": "H10",
                    "account_hash": "AH",
                    "total_coins": "1",
                    "close_time": 5,
                    "closed": True,
                }
            }
        assert method == "ledger_data"
        names = {
            "check": "Check",
            "ticket": "Ticket",
            "deposit_preauth": "DepositPreauth",
            "escrow": "Escrow",
        }
        sel = [
            o for o in self.objects if "type" not in params or o["LedgerEntryType"] == names[params["type"]]
        ]
        start = int(params.get("marker") or 0)
        page = sel[start : start + params["limit"]]
        nxt = start + params["limit"]
        r = {"ledger_hash": self.h, "state": page}
        if nxt < len(sel):
            r["marker"] = str(nxt)
        return r


def run_snapshot(tmp_path, rpc, max_pages=None):
    doc, _ = types_doc()
    tpath = tmp_path / "types.json"
    tpath.write_text(json.dumps(doc))
    p = argparse.Namespace(
        types=str(tpath),
        out=str(tmp_path / "out"),
        ledger=None,
        limit=1,
        rate=1000,
        max_pages=max_pages,
        log=None,
        container="x",
        port=1,
        no_sudo=True,
    )
    return C.cmd_snapshot(p, rpc=rpc)


def test_snapshot_end_to_end_and_resume_with_torn_tail(tmp_path):
    rc = run_snapshot(tmp_path, FakeRpc(objs()), max_pages=2)
    assert rc == 4 and not (tmp_path / "out" / "DONE").exists()
    pages = tmp_path / "out" / "pages.jsonl"
    with open(pages, "a") as fh:
        fh.write('{"page": 2, "torn')  # SIGKILL mid-write
    assert run_snapshot(tmp_path, FakeRpc(objs())) == 0
    done = (tmp_path / "out" / "DONE").read_text().split()
    data = (tmp_path / "out" / "objlife-10.jsonl").read_bytes()
    assert done[0] == hashlib.sha256(data).hexdigest() and done[-1] == "3"
    # one uninterrupted run gives the same bytes
    other = tmp_path / "o2"
    other.mkdir()
    assert run_snapshot(other, FakeRpc(objs())) == 0
    assert (other / "out" / "objlife-10.jsonl").read_bytes() == data


def test_snapshot_refuses_other_ledger_hash_on_type_filter(tmp_path):
    class Drift(FakeRpc):
        def call(self, method, params=None):
            r = super().call(method, params)
            if method == "ledger_data" and "type" in params:
                r["ledger_hash"] = "OTHER"
            return r

    with pytest.raises(RuntimeError, match="pinned"):
        run_snapshot(tmp_path, Drift(objs()))


def test_typecount_resume_skips_done_types(tmp_path):
    rpc = FakeRpc(objs())
    assert run_snapshot(tmp_path, rpc) == 0
    (tmp_path / "out" / "DONE").unlink()
    rpc2 = FakeRpc(objs())
    assert run_snapshot(tmp_path, rpc2) == 0
    assert rpc2.count == 0  # snapshot complete and all type counts recorded: no RPC at all
