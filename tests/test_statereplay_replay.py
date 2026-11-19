"""statereplay/replay.py on a real capture (tests/fixtures/statereplay/F1F3, ledgers 8169668 -> 8169792):
exact replay, and every control (dropped, reordered, altered, missing ledger, chain) failing with its
specific reason."""

import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statereplay"))

import replay as R  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "statereplay", "F1F3")
NUNL = os.path.join(HERE, "fixtures", "statereplay", "nunl.json")


@pytest.fixture()
def world():
    return list(R.load_published(FIX))


def run(w):
    ha, state, hb, target, rows, txs = w
    return R.replay(ha, state, hb, target, rows, txs)


def reason(w):
    with pytest.raises(R.ReplayError) as e:
        diffs, _ = run(w)
        if diffs:
            raise R.ReplayError("FINAL_" + diffs[0]["reason"], index=diffs[0]["index"])
    return e.value.reason


def ledgers_with_tx(w, at_least=1):
    return [r for r in w[5] if len(r["transactions"]) >= at_least]


def account_nodes(tx):
    return [n["ModifiedNode"]["LedgerIndex"] for n in tx["metaData"]["AffectedNodes"] if "ModifiedNode" in n]


def test_real_capture_replays_exactly(world):
    diffs, stats = run(world)
    assert diffs == []
    assert stats["ledgers"] == 8169792 - 8169668 == 124
    assert stats["transactions"] == 39
    assert stats["nodes"] == 78


def test_published_and_raw_forms_share_one_replay():
    # the CLI has one code path: both loaders feed replay(); the published loader is what tests use
    ha, state, hb, target, rows, txs = R.load_published(FIX)
    assert ha["ledger_index"] == 8169668 and hb["ledger_index"] == 8169792
    assert rows[0]["ledger_index"] == 8169668 and rows[-1]["ledger_index"] == 8169792
    assert len(rows) == 125 and len(txs) == 124


def test_dropped_transaction_fails_tx_count(world):
    led = ledgers_with_tx(world)[0]
    led["transactions"].pop()
    assert reason(world) == "TX_COUNT"


def test_dropped_transaction_with_forged_count_fails_on_state(world):
    # drop the first tx of a ledger and forge txn_count + TransactionIndex so the ledger looks complete:
    # the dropped tx's writes are missing, so a later PreviousFields check or the final compare fails
    led = ledgers_with_tx(world)[0]
    n = led["ledger_index"]
    led["transactions"].pop(0)
    for i, t in enumerate(led["transactions"]):
        t["metaData"]["TransactionIndex"] = i
    for r in world[4]:
        if r["ledger_index"] == n:
            r["txn_count"] -= 1
    assert reason(world) in ("PREVIOUS_MISMATCH", "THREAD_MISMATCH", "FINAL_FIELD_MISMATCH")


def same_account_pair(w):
    for led in w[5]:
        txs = led["transactions"]
        for i in range(len(txs)):
            for j in range(i + 1, len(txs)):
                if set(account_nodes(txs[i])) & set(account_nodes(txs[j])):
                    return txs[i], txs[j]
    return None


def test_reordered_transactions_fail(world):
    pair = same_account_pair(world)
    if pair is None:
        # no ledger of the capture writes one object twice: move a later ledger's tx before an earlier
        # one that touches the same object instead (cross-ledger reorder)
        pytest.fail("fixture has no same-ledger pair; extend the fixture")
    a, b = pair
    a["metaData"]["TransactionIndex"], b["metaData"]["TransactionIndex"] = (
        b["metaData"]["TransactionIndex"],
        a["metaData"]["TransactionIndex"],
    )
    assert reason(world) in ("PREVIOUS_MISMATCH", "THREAD_MISMATCH")


def test_reordered_ledgers_fail(world):
    # swap the transaction lists of the first two ledgers with transactions (counts swapped too)
    l1, l2 = ledgers_with_tx(world)[:2]
    l1["transactions"], l2["transactions"] = l2["transactions"], l1["transactions"]
    counts = {r["ledger_index"]: r for r in world[4]}
    c1, c2 = counts[l1["ledger_index"]], counts[l2["ledger_index"]]
    c1["txn_count"], c2["txn_count"] = c2["txn_count"], c1["txn_count"]
    assert reason(world) in ("PREVIOUS_MISMATCH", "THREAD_MISMATCH", "FINAL_FIELD_MISMATCH")


def test_altered_final_field_fails(world):
    # raise one Balance in FinalFields: the object's next write (or the final compare) disagrees
    for led in world[5]:
        for t in led["transactions"]:
            for node in t["metaData"]["AffectedNodes"]:
                body = node.get("ModifiedNode")
                if body and "Balance" in body.get("FinalFields", {}):
                    body["FinalFields"]["Balance"] = str(int(body["FinalFields"]["Balance"]) + 1)
                    assert reason(world) in ("PREVIOUS_MISMATCH", "FINAL_FIELD_MISMATCH")
                    return
    pytest.fail("no ModifiedNode with Balance in fixture")


def test_altered_previous_field_fails(world):
    for led in world[5]:
        for t in led["transactions"]:
            for node in t["metaData"]["AffectedNodes"]:
                body = node.get("ModifiedNode")
                if body and body.get("PreviousFields"):
                    k = sorted(body["PreviousFields"])[0]
                    body["PreviousFields"][k] = "altered"
                    assert reason(world) == "PREVIOUS_MISMATCH"
                    return
    pytest.fail("no PreviousFields in fixture")


def test_altered_unchanged_field_fails(world):
    for led in world[5]:
        for t in led["transactions"]:
            for node in t["metaData"]["AffectedNodes"]:
                body = node.get("ModifiedNode")
                if (
                    body
                    and "Flags" in body.get("FinalFields", {})
                    and "Flags" not in body.get("PreviousFields", {})
                ):
                    body["FinalFields"]["Flags"] = body["FinalFields"]["Flags"] ^ 1
                    assert reason(world) == "UNCHANGED_MISMATCH"
                    return
    pytest.fail("no unchanged Flags in fixture")


def test_altered_thread_fails(world):
    for led in world[5]:
        for t in led["transactions"]:
            for node in t["metaData"]["AffectedNodes"]:
                body = node.get("ModifiedNode")
                if body and "PreviousTxnID" in body:
                    body["PreviousTxnID"] = "0" * 64
                    assert reason(world) == "THREAD_MISMATCH"
                    return
    pytest.fail("no threaded ModifiedNode in fixture")


def test_missing_ledger_row_fails(world):
    world[4].pop(10)
    assert reason(world) == "LEDGER_MISSING"


def test_missing_ledger_transactions_fails(world):
    world[5].pop(10)
    assert reason(world) == "LEDGER_TX_MISSING"


def test_duplicate_ledger_fails(world):
    world[4].append(copy.deepcopy(world[4][5]))
    assert reason(world) == "LEDGER_DUPLICATE"
    w2 = list(R.load_published(FIX))
    w2[5].append(copy.deepcopy(w2[5][5]))
    assert reason(w2) == "LEDGER_TX_DUPLICATE"


def test_chain_break_fails(world):
    world[4][20]["parent_hash"] = "F" * 64
    assert reason(world) == "CHAIN_BREAK"


def test_anchor_hashes_fail(world):
    world[4][0]["ledger_hash"] = "A" * 64
    world[4][1]["parent_hash"] = "A" * 64  # keep the chain itself intact
    assert reason(world) == "ANCHOR_A_HASH"
    w2 = list(R.load_published(FIX))
    w2[4][-1]["ledger_hash"] = "B" * 64
    assert reason(w2) == "ANCHOR_B_HASH"


def test_transaction_index_gap_fails(world):
    led = ledgers_with_tx(world)[0]
    led["transactions"][0]["metaData"]["TransactionIndex"] += 100
    assert reason(world) == "TX_INDEX_GAP"


def test_missing_object_fails(world):
    led = ledgers_with_tx(world)[0]
    idx = account_nodes(led["transactions"][0])[0]
    del world[1][idx]
    assert reason(world) == "NODE_MISSING"


def test_create_over_existing_fails(world):
    led = ledgers_with_tx(world)[0]
    t = led["transactions"][0]
    idx = account_nodes(t)[0]
    body = t["metaData"]["AffectedNodes"][0]
    kind = next(iter(body))
    body[kind]["LedgerIndex"] = idx
    t["metaData"]["AffectedNodes"][0] = {
        "CreatedNode": {"LedgerEntryType": "AccountRoot", "LedgerIndex": idx}
    }
    assert reason(world) == "CREATE_EXISTS"


def test_type_mismatch_fails(world):
    led = ledgers_with_tx(world)[0]
    node = led["transactions"][0]["metaData"]["AffectedNodes"][0]
    next(iter(node.values()))["LedgerEntryType"] = "Offer"
    assert reason(world) == "TYPE_MISMATCH"


def test_extra_object_in_b_is_reported(world):
    world[3]["F" * 64] = {"LedgerEntryType": "Ticket", "index": "F" * 64, "Flags": 0}
    diffs, _ = run(world)
    assert [d["reason"] for d in diffs] == ["MISSING_IN_REPLAY"]


def test_skip_list_is_derived_not_exempted(world, monkeypatch):
    # without the derived skip-list update the replay must NOT match B: both skip lists are compared.
    # The window crosses flag ledger 8169728 (= 256 * 31913), so the long list for 8169727 >> 16 changes too
    monkeypatch.setattr(R, "advance_skip_lists", lambda state, seq, parent: None)
    diffs, _ = run(world)
    assert sorted((d["index"], d["reason"], d["fields"]) for d in diffs) == sorted(
        [
            (R.SKIP_KEY, "FIELD_MISMATCH", ["Hashes", "LastLedgerSequence"]),
            (R.long_skip_key(8169728), "FIELD_MISMATCH", ["Hashes", "LastLedgerSequence"]),
        ]
    )


def test_skip_list_keeps_last_256_and_long_list_on_boundary():
    state = {
        R.SKIP_KEY: {
            "Flags": 0,
            "Hashes": [f"{i:064X}" for i in range(256)],
            "LastLedgerSequence": 1,
            "LedgerEntryType": "LedgerHashes",
            "index": R.SKIP_KEY,
        }
    }
    R.advance_skip_lists(state, 513, "E" * 64)  # parent 512 is a multiple of 256
    assert len(state[R.SKIP_KEY]["Hashes"]) == 256
    assert state[R.SKIP_KEY]["Hashes"][-1] == "E" * 64 and state[R.SKIP_KEY]["Hashes"][0] == f"{1:064X}"
    long_key = R.long_skip_key(512)
    assert state[long_key]["Hashes"] == ["E" * 64] and state[long_key]["LastLedgerSequence"] == 512
    R.advance_skip_lists(state, 514, "D" * 64)
    assert state[long_key]["Hashes"] == ["E" * 64]


def test_long_skip_key_matches_live_object():
    # the long skip list for ledgers 8126464.. (8169668 >> 16 == 124) is a real object in the fixture
    _, state, _, _, _, _ = R.load_published(FIX)
    assert R.long_skip_key(8169668) in state
    assert state[R.long_skip_key(8169668)]["LedgerEntryType"] == "LedgerHashes"


def test_negative_unl_reenable_takes_effect_on_next_flag_ledger():
    fx = json.load(open(NUNL))
    state = {R.NUNL_KEY: fx["negative_unl_at_A"]}
    mod = fx["unlmodify_8128256"]
    assert R.advance_negative_unl(state, 8128256) is None  # nothing pending before the UNLModify
    for node in mod["metaData"]["AffectedNodes"]:
        R.apply_node(state, node, mod["hash"], 8128256)
    assert "ValidatorToReEnable" in state[R.NUNL_KEY]
    assert R.advance_negative_unl(state, 8128257) is None  # not a flag ledger
    assert R.advance_negative_unl(state, 8128512) == "erased"
    assert R.NUNL_KEY not in state and fx["present_at_B_8150605"] is False


def test_negative_unl_disable_appends_with_first_ledger():
    state = {
        R.NUNL_KEY: {
            "Flags": 0,
            "LedgerEntryType": "NegativeUNL",
            "index": R.NUNL_KEY,
            "ValidatorToDisable": "ED01",
        }
    }
    assert R.advance_negative_unl(state, 512) == "replaced"
    assert state[R.NUNL_KEY]["DisabledValidators"] == [
        {"DisabledValidator": {"FirstLedgerSequence": 512, "PublicKey": "ED01"}}
    ]
    assert "ValidatorToDisable" not in state[R.NUNL_KEY]


def test_created_node_gets_required_defaults_and_thread():
    state = {}
    node = {
        "CreatedNode": {
            "LedgerEntryType": "AccountRoot",
            "LedgerIndex": "AB",
            "NewFields": {"Account": "acct_x", "Balance": "1", "Sequence": 5},
        }
    }
    R.apply_node(state, node, "TX", 7)
    assert state["AB"] == {
        "Account": "acct_x",
        "Balance": "1",
        "Sequence": 5,
        "Flags": 0,
        "OwnerCount": 0,
        "LedgerEntryType": "AccountRoot",
        "index": "AB",
        "PreviousTxnID": "TX",
        "PreviousTxnLgrSeq": 7,
    }


def test_modified_node_removes_fields_only_in_previous():
    state = {
        "AB": {
            "LedgerEntryType": "AccountRoot",
            "index": "AB",
            "Flags": 0,
            "Domain": "x",
            "Balance": "5",
            "PreviousTxnID": "OLD",
            "PreviousTxnLgrSeq": 1,
        }
    }
    node = {
        "ModifiedNode": {
            "LedgerEntryType": "AccountRoot",
            "LedgerIndex": "AB",
            "FinalFields": {"Flags": 0, "Balance": "4"},
            "PreviousFields": {"Domain": "x", "Balance": "5"},
            "PreviousTxnID": "OLD",
            "PreviousTxnLgrSeq": 1,
        }
    }
    R.apply_node(state, node, "NEW", 2)
    assert state["AB"] == {
        "LedgerEntryType": "AccountRoot",
        "index": "AB",
        "Flags": 0,
        "Balance": "4",
        "PreviousTxnID": "NEW",
        "PreviousTxnLgrSeq": 2,
    }


def test_cli_exit_codes(tmp_path, capsys):
    assert R.main(["--data", FIX, "--json", str(tmp_path / "r.json")]) == 0
    out = capsys.readouterr().out
    assert "RESULT objects_replayed=69 objects_B=69 diffs=0 EXACT" in out
    assert json.load(open(tmp_path / "r.json"))["diffs"] == []
    with pytest.raises(SystemExit):
        R.main(["--data", FIX, "--a", "x"])


def test_threading_covers_every_type_but_ledger_hashes():
    # fixPreviousTxnID is enabled on the testnet: snapshot A (2026-11-19) has PreviousTxnID on every
    # DirectoryNode, so DirectoryNode/NegativeUNL/Amendments/FeeSettings writes advance the thread too
    for t in (
        "AccountRoot",
        "DirectoryNode",
        "NegativeUNL",
        "Amendments",
        "FeeSettings",
        "NFTokenPage",
        "Offer",
    ):
        assert R.threaded(t), t
    assert not R.threaded("LedgerHashes")


def test_directory_node_thread_advances():
    state = {
        "DD": {
            "Flags": 0,
            "Indexes": ["a"],
            "LedgerEntryType": "DirectoryNode",
            "Owner": "acct_x",
            "PreviousTxnID": "OLD",
            "PreviousTxnLgrSeq": 3,
            "RootIndex": "DD",
            "index": "DD",
        }
    }
    node = {
        "ModifiedNode": {
            "LedgerEntryType": "DirectoryNode",
            "LedgerIndex": "DD",
            "FinalFields": {"Flags": 0, "Indexes": ["a", "b"], "Owner": "acct_x", "RootIndex": "DD"},
            "PreviousFields": {"Indexes": ["a"]},
            "PreviousTxnID": "OLD",
            "PreviousTxnLgrSeq": 3,
        }
    }
    R.apply_node(state, node, "NEW", 9)
    assert state["DD"]["Indexes"] == ["a", "b"]
    assert (state["DD"]["PreviousTxnID"], state["DD"]["PreviousTxnLgrSeq"]) == ("NEW", 9)


def test_field_added_by_tx_is_accepted():
    # real shape (NFTokenMint, ledger 8122395): PreviousPageMin appears in FinalFields with empty
    # PreviousFields because the page had no previous value
    idx = "7EE1D64CF6565ECB88BE616073EB676DDBD64DA5FFFFFFFFFFFFFFFFFFFFFFFF"
    prev_txn = "67302FB72FAD88F9453765775BD594D0815D71E798F6324F26595B70D2D8CF5C"
    state = {
        idx: {
            "Flags": 0,
            "LedgerEntryType": "NFTokenPage",
            "NFTokens": [],
            "PreviousTxnID": prev_txn,
            "PreviousTxnLgrSeq": 8121499,
            "index": idx,
        }
    }
    node = {
        "ModifiedNode": {
            "FinalFields": {"Flags": 0, "NFTokens": [], "PreviousPageMin": "AB"},
            "LedgerEntryType": "NFTokenPage",
            "LedgerIndex": idx,
            "PreviousFields": {},
            "PreviousTxnID": prev_txn,
            "PreviousTxnLgrSeq": 8121499,
        }
    }
    R.apply_node(state, node, "TX", 8122395)
    assert state[idx]["PreviousPageMin"] == "AB" and state[idx]["PreviousTxnLgrSeq"] == 8122395


def test_window_empty_and_unknown_node_kind_fail(world):
    ha, state, hb, target, rows, txs = world
    with pytest.raises(R.ReplayError) as e:
        R.replay(hb, state, ha, target, rows, txs)
    assert e.value.reason == "WINDOW_EMPTY"
    led = ledgers_with_tx(world)[0]
    node = led["transactions"][0]["metaData"]["AffectedNodes"]
    node[0] = {"RenamedNode": next(iter(node[0].values()))}
    assert reason(world) == "NODE_KIND"


def raw_snapshot(tmp_path, pages, header_hash="H"):
    os.makedirs(tmp_path / "pages")
    (tmp_path / "header.json").write_text(json.dumps({"ledger_index": 1, "ledger_hash": header_hash}))
    for i, (lhash, objs) in enumerate(pages):
        (tmp_path / "pages" / f"full-{i:04d}.json").write_text(
            json.dumps({"ledger_hash": lhash, "state": objs})
        )
    return str(tmp_path)


def test_snapshot_loader_refuses_bad_pages(tmp_path):
    o = {"index": "AA", "LedgerEntryType": "AccountRoot"}
    with pytest.raises(R.ReplayError) as e:
        R.load_snapshot(raw_snapshot(tmp_path / "1", [("H", [o]), ("OTHER", [])]))
    assert e.value.reason == "SNAPSHOT_PAGE_HASH"
    with pytest.raises(R.ReplayError) as e:
        R.load_snapshot(raw_snapshot(tmp_path / "2", [("H", [o]), ("H", [o])]))
    assert e.value.reason == "SNAPSHOT_DUPLICATE"
    with pytest.raises(R.ReplayError) as e:
        R.load_snapshot(raw_snapshot(tmp_path / "3", []))
    assert e.value.reason == "SNAPSHOT_EMPTY"
    assert R.load_snapshot(raw_snapshot(tmp_path / "4", [("H", [o])]))[1] == {"AA": o}


def test_cli_reports_fail_reason(tmp_path, capsys):
    raw = tmp_path / "raw"
    ha, state, hb, target, rows, txs = R.load_published(FIX)
    for name, h, objs in (("A", ha, state), ("B", hb, target)):
        os.makedirs(raw / name / "pages")
        (raw / name / "header.json").write_text(json.dumps(h))
        (raw / name / "pages" / "full-0000.json").write_text(
            json.dumps({"ledger_hash": h["ledger_hash"], "state": list(objs.values())})
        )
    os.makedirs(raw / "tx")
    rows[30]["parent_hash"] = "0" * 64
    (raw / "tx" / "ledgers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (raw / "tx" / "tx.jsonl").write_text("".join(json.dumps(r) + "\n" for r in txs))
    rc = R.main(["--a", str(raw / "A"), "--b", str(raw / "B"), "--tx", str(raw / "tx")])
    assert rc == 1 and capsys.readouterr().out.startswith(
        f"FAIL CHAIN_BREAK ledger={rows[30]['ledger_index']}"
    )


def test_replay_derives_pending_negative_unl_on_flag_ledger(world):
    # put the real 2026-11-17 NegativeUNL (one disabled validator) into A with its re-enable pending, as
    # UNLModify left it: the window's flag ledger 8169728 must erase it, so the replay still equals B
    fx = json.load(open(NUNL))
    obj = dict(fx["negative_unl_at_A"])
    obj["ValidatorToReEnable"] = obj["DisabledValidators"][0]["DisabledValidator"]["PublicKey"]
    world[1][R.NUNL_KEY] = obj
    diffs, stats = run(world)
    assert diffs == [] and stats["negative_unl"] == [[8169728, "erased"]]
