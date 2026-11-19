"""statereplay/hotspots.py: hotspot metrics on the real capture (F1F3) and, for the branches that capture
does not exercise (DirectoryNode churn, net-unchanged objects, create+delete inside the window), on a
small synthetic window with hand-computed answers. Report rendering is byte-deterministic."""

import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statereplay"))

import extract_replay as X  # noqa: E402
import hotspots as H  # noqa: E402
import replay as R  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "statereplay", "F1F3")


def test_nearest_rank():
    vals = list(range(1, 21))
    assert H.nearest_rank(vals, 50) == 10
    assert H.nearest_rank(vals, 95) == 19
    assert H.nearest_rank(vals, 100) == 20
    assert H.nearest_rank([7], 95) == 7
    assert H.nearest_rank([], 50) is None


def test_real_capture_hotspots_add_up():
    r = H.analyze(FIX)
    hs = r["hotspots"]
    assert r["replay"]["result"] == "EXACT"
    assert hs["metadata_nodes"] == 78
    assert sum(e["nodes_total"] for e in hs["tx_types"].values()) == 78
    assert sum(s["total"] for s in hs["write_share"].values()) == 78
    assert sum(e["transactions"] for e in hs["tx_types"].values()) == r["window"]["transactions"] == 39
    assert (
        hs["tx_types"]["Payment"]["transactions"] == 23
        and hs["tx_types"]["NFTokenBurn"]["transactions"] == 16
    )
    assert hs["write_share"]["NFTokenPage"]["DeletedNode"] == 1
    # 124 ledgers -> 124 short skip-list updates + 1 long one at flag ledger 8169728
    assert hs["derived_writes"] == {"LedgerHashes_skip_list_updates": 125, "NegativeUNL_updates": 0}
    top = hs["most_rewritten_objects"][0]
    assert top["modified"] == 15 and top["object"].startswith("acct_")


def test_render_and_offline_rebuild_are_byte_identical(tmp_path):
    os.makedirs(tmp_path / "1")
    os.makedirs(tmp_path / "2")
    assert H.main(["--data", FIX, "--out", str(tmp_path / "1")]) == 0
    assert H.main(["--data", FIX, "--out", str(tmp_path / "2")]) == 0
    for name in ("REPORT.md", "report.json"):
        assert (tmp_path / "1" / name).read_bytes() == (tmp_path / "2" / name).read_bytes()
    text = (tmp_path / "1" / "REPORT.md").read_text()
    assert "| EXACT | 70 | 69 | 69 | 0 |" in text
    assert not any(X.is_address(w.strip("`|,.()")) for w in text.split())


THREAD0 = {"PreviousTxnID": "P0", "PreviousTxnLgrSeq": 9}


def acct(i, bal):
    return {
        "Account": f"acct_{i}",
        "Balance": str(bal),
        "Flags": 0,
        "LedgerEntryType": "AccountRoot",
        "OwnerCount": 0,
        "Sequence": 1,
        "index": i,
        **THREAD0,
    }


def ff(obj, **changes):
    """FinalFields of obj as metadata carries them: no index/type/thread, with `changes` applied."""
    out = {
        k: v
        for k, v in obj.items()
        if k not in ("index", "LedgerEntryType", "PreviousTxnID", "PreviousTxnLgrSeq")
    }
    out.update(changes)
    return out


def mod(i, etype, final, prev, thread):
    """ModifiedNode of an object last written by transaction `thread` (every synthetic object is threaded)."""
    return {
        "ModifiedNode": {
            "LedgerEntryType": etype,
            "LedgerIndex": i,
            "FinalFields": final,
            "PreviousFields": prev,
            "PreviousTxnID": thread,
            "PreviousTxnLgrSeq": 9 if thread == "P0" else 11,
        }
    }


def tx(h, ttype, sender, ti, nodes):
    return {
        "hash": h,
        "TransactionType": ttype,
        "Account": sender,
        "metaData": {"TransactionIndex": ti, "TransactionResult": "tesSUCCESS", "AffectedNodes": nodes},
    }


@pytest.fixture()
def synthetic(tmp_path):
    """Ledger 11: X pays Y, Y pays X back (both net-unchanged apart from the thread); X places offer O
    (owner dir D +O, new book dir BD) and cancels it (D -O, BD deleted); X cancels its older offer O2
    (in A). Every object is threaded, so "net-unchanged" must ignore PreviousTxnID/LgrSeq.
    Hand-computed answers are asserted below."""
    x, y = acct("XX", 10), acct("YY", 5)
    d = {
        "Flags": 0,
        "Indexes": ["a"],
        "LedgerEntryType": "DirectoryNode",
        "Owner": "acct_XX",
        "RootIndex": "DD",
        "index": "DD",
        **THREAD0,
    }
    o2 = {
        "Account": "acct_XX",
        "BookDirectory": "BD2",
        "BookNode": "0",
        "Flags": 0,
        "LedgerEntryType": "Offer",
        "OwnerNode": "0",
        "index": "O2",
        **THREAD0,
    }
    txs = [
        tx(
            "T0",
            "Payment",
            "acct_XX",
            0,
            [
                mod("XX", "AccountRoot", ff(x, Balance="9"), {"Balance": "10"}, "P0"),
                mod("YY", "AccountRoot", ff(y, Balance="6"), {"Balance": "5"}, "P0"),
            ],
        ),
        tx(
            "T1",
            "Payment",
            "acct_YY",
            1,
            [
                mod("YY", "AccountRoot", ff(y, Balance="5"), {"Balance": "6"}, "T0"),
                mod("XX", "AccountRoot", ff(x, Balance="10"), {"Balance": "9"}, "T0"),
            ],
        ),
        tx(
            "T2",
            "OfferCreate",
            "acct_XX",
            2,
            [
                {
                    "CreatedNode": {
                        "LedgerEntryType": "Offer",
                        "LedgerIndex": "OO",
                        "NewFields": {"Account": "acct_XX", "BookDirectory": "BD"},
                    }
                },
                mod(
                    "DD",
                    "DirectoryNode",
                    {"Flags": 0, "Indexes": ["a", "OO"], "Owner": "acct_XX", "RootIndex": "DD"},
                    {"Indexes": ["a"]},
                    "P0",
                ),
                {
                    "CreatedNode": {
                        "LedgerEntryType": "DirectoryNode",
                        "LedgerIndex": "BD",
                        "NewFields": {"ExchangeRate": "1", "Indexes": ["OO"], "RootIndex": "BD"},
                    }
                },
            ],
        ),
        tx(
            "T3",
            "OfferCancel",
            "acct_XX",
            3,
            [
                {
                    "DeletedNode": {
                        "LedgerEntryType": "Offer",
                        "LedgerIndex": "OO",
                        "FinalFields": {
                            "Account": "acct_XX",
                            "BookDirectory": "BD",
                            "Flags": 0,
                            "OwnerNode": "0",
                            "BookNode": "0",
                        },
                    }
                },
                mod(
                    "DD",
                    "DirectoryNode",
                    {"Flags": 0, "Indexes": ["a"], "Owner": "acct_XX", "RootIndex": "DD"},
                    {"Indexes": ["a", "OO"]},
                    "T2",
                ),
                {
                    "DeletedNode": {
                        "LedgerEntryType": "DirectoryNode",
                        "LedgerIndex": "BD",
                        "FinalFields": {
                            "ExchangeRate": "1",
                            "Flags": 0,
                            "Indexes": ["OO"],
                            "RootIndex": "BD",
                        },
                    }
                },
            ],
        ),
        tx(
            "T4",
            "OfferCancel",
            "acct_XX",
            4,
            [
                {
                    "DeletedNode": {
                        "LedgerEntryType": "Offer",
                        "LedgerIndex": "O2",
                        "FinalFields": ff(o2),
                        "PreviousTxnID": "P0",
                        "PreviousTxnLgrSeq": 9,
                    }
                }
            ],
        ),
    ]
    a_state = {o["index"]: o for o in (x, y, d, o2)}
    rows = [
        {"ledger_index": 10, "ledger_hash": "H10", "parent_hash": "H9", "close_time": 0, "txn_count": 0},
        {"ledger_index": 11, "ledger_hash": "H11", "parent_hash": "H10", "close_time": 3600, "txn_count": 5},
    ]
    tx_rows = [{"ledger_index": 11, "transactions": txs}]
    b_state = copy.deepcopy(a_state)
    ha = {"ledger_index": 10, "ledger_hash": "H10", "parent_hash": "H9", "close_time_iso": "a"}
    hb = {"ledger_index": 11, "ledger_hash": "H11", "parent_hash": "H10", "close_time_iso": "b"}
    R.replay(ha, b_state, hb, {}, rows, copy.deepcopy(tx_rows))
    out = tmp_path / "data"
    os.makedirs(out)
    X.write_gz(str(out / "A.jsonl.gz"), [a_state[i] for i in sorted(a_state)])
    X.write_gz(str(out / "B.jsonl.gz"), [b_state[i] for i in sorted(b_state)])
    X.write_gz(str(out / "ledgers.jsonl.gz"), rows)
    X.write_gz(str(out / "tx.jsonl.gz"), tx_rows)
    with open(out / "snapshots.json", "w") as f:
        json.dump({"A": {"header": ha}, "B": {"header": hb}}, f)
    return str(out)


def test_synthetic_directory_churn(synthetic):
    dc = H.analyze(synthetic)["hotspots"]["directory_churn"]
    assert dc == {
        "pages_created": 1,
        "pages_modified": 2,
        "pages_deleted": 1,
        "owner_pages_touched": 1,
        "book_pages_touched": 1,
        "indexes_added": 2,
        "indexes_removed": 2,
    }


def test_synthetic_net_unchanged_and_ephemeral(synthetic):
    r = H.analyze(synthetic)
    assert r["replay"]["result"] == "EXACT"
    hs = r["hotspots"]
    # the thread did move: XX ends the window written by T1, not by its A-time P0
    assert H.unthreaded({"a": 1, "PreviousTxnID": "x", "PreviousTxnLgrSeq": 2}) == {"a": 1}
    assert hs["net_unchanged_repeatedly_modified"] == {
        "AccountRoot": {"objects": 2, "modifications": 4},
        "DirectoryNode": {"objects": 1, "modifications": 2},
    }
    # O2 is deleted but existed at A, so it is not "created and deleted in the window"
    assert hs["created_and_deleted_in_window"] == {"DirectoryNode": 1, "Offer": 1}


def test_synthetic_tx_types_accounts_and_share(synthetic):
    r = H.analyze(synthetic)
    hs = r["hotspots"]
    assert r["window"]["hours"] == 1.0
    assert hs["tx_types"]["Payment"]["nodes_total"] == 4 and hs["tx_types"]["OfferCreate"]["kinds"] == {
        "CreatedNode": 2,
        "ModifiedNode": 1,
        "DeletedNode": 0,
    }
    assert hs["most_rewritten_accounts"] == [
        {"account": "acct_XX", "accountroot_modified": 2, "nodes_written_as_sender": 9},
        {"account": "acct_YY", "accountroot_modified": 2, "nodes_written_as_sender": 2},
    ]
    assert (
        hs["write_share"]["DirectoryNode"]["total"] == 4
        and hs["write_share"]["DirectoryNode"]["share_pct"] == 36.36
    )
    assert hs["write_share"]["Offer"] == {
        "CreatedNode": 1,
        "ModifiedNode": 0,
        "DeletedNode": 2,
        "total": 3,
        "share_pct": 27.27,
    }


def test_mismatch_is_reported_and_exits_1(synthetic, tmp_path):
    rows = [json.loads(line) for line in __import__("gzip").open(os.path.join(synthetic, "B.jsonl.gz"), "rt")]
    rows[0]["Balance"] = "11"
    X.write_gz(os.path.join(synthetic, "B.jsonl.gz"), rows)
    assert H.main(["--data", synthetic, "--out", str(tmp_path)]) == 1
    assert "| MISMATCH |" in (tmp_path / "REPORT.md").read_text()


def test_replay_failure_exits_1_and_writes_nothing(synthetic, tmp_path, capsys):
    os.remove(os.path.join(synthetic, "tx.jsonl.gz"))
    X.write_gz(os.path.join(synthetic, "tx.jsonl.gz"), [])
    out = tmp_path / "o"
    os.makedirs(out)
    assert H.main(["--data", synthetic, "--out", str(out)]) == 1
    assert "FAIL LEDGER_TX_MISSING" in capsys.readouterr().out
    assert os.listdir(out) == []


def test_p95_and_p50_on_a_spread_distribution():
    # 20 transactions touching 1..20 nodes: nearest-rank p50 = 10, p95 = 19 (p90 would be 18)
    h = H.Hotspots({})
    for n in range(1, 21):
        nodes = [
            {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "LedgerIndex": f"I{n}_{k}"}} for k in range(n)
        ]
        t = {
            "TransactionType": "Batchy",
            "Account": "acct_a",
            "metaData": {"TransactionResult": "tesSUCCESS", "AffectedNodes": nodes},
        }
        h.on_tx(1, t, [("ModifiedNode", "AccountRoot", f"I{n}_{k}") for k in range(n)])
    e = h.summary({}, {"nodes": 210, "ledgers": 1, "first": 2, "last": 2}, 5)["tx_types"]["Batchy"]
    assert (e["nodes_min"], e["nodes_p50"], e["nodes_p95"], e["nodes_max"], e["nodes_mean"]) == (
        1,
        10,
        19,
        20,
        10.5,
    )
