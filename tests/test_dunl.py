"""dunl/ unit tests: captured public responses (tests/fixtures/dunl, built by dunl/make_fixtures.py)
plus one synthetic mismatch per check. No network, no node."""

import base64
import copy
import hashlib
import json
import os
import sys

import pytest

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "dunl"))
import audit_dunl as A  # noqa: E402
import collect_dunl as C  # noqa: E402
import lib_dunl as L  # noqa: E402

FIX = os.path.join(here, "fixtures", "dunl")


@pytest.fixture(scope="module")
def modern_rows():
    return A.load_rows(os.path.join(FIX, "modern.jsonl.gz"))


@pytest.fixture(scope="module")
def nodes():
    return A.load_nodes(os.path.join(FIX, "nodes"))


@pytest.fixture
def ds(modern_rows):
    return A.dataset(copy.deepcopy(modern_rows))


def by(results, check, scope=None):
    return [r for r in results if r["check"] == check and (scope is None or r["scope"] == scope)]


def statuses(results, check, scope=None):
    return {r["status"] for r in by(results, check, scope)}


def set_memo(tx, payload):
    tx["Memos"][0]["Memo"]["MemoData"] = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode().hex()
    )


# ---------------------------------------------------------------- lib


def test_ed25519_rfc8032_vector1():
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
        "d25bf5f0595bbe24655141438e7a100b"
    )
    assert L.ed25519_verify(pub, b"", sig)
    assert not L.ed25519_verify(pub, b"x", sig)
    assert not L.ed25519_verify(pub, b"", sig[:-1] + bytes([sig[-1] ^ 1]))


def test_node_public_roundtrip_and_checksum():
    k = "ED" + "AB" * 32
    b58 = L.node_public_b58(k)
    assert b58.startswith("n") and L.node_public_hex(b58) == k
    bad = b58[:-1] + ("r" if b58[-1] != "r" else "p")
    with pytest.raises(ValueError):
        L.node_public_hex(bad)


def test_real_vl_signatures_and_tamper(ds):
    vl = A.signed_vl(ds["rounds"][33])
    d = L.decode_vl(vl)
    assert d["blobs"][0]["signature_ok"] and d["blobs"][0]["low_s"]
    assert L.verify_manifest(d["publisher"]) == (True, True)
    raw = base64.b64decode(vl["blobs_v2"][0]["blob"])
    sig = bytes.fromhex(vl["blobs_v2"][0]["signature"])
    assert not L.verify(d["publisher"]["signing"], raw + b" ", sig)
    r, s = L.der_decode(sig)
    assert s <= L._SN // 2


def test_der_rejects_noncanonical():
    with pytest.raises(ValueError):
        L.der_decode(bytes.fromhex("3005020101020101"))  # sequence length != remaining bytes
    with pytest.raises(ValueError):
        L.der_decode(bytes.fromhex("3006020180020101"))  # negative integer (high bit set)
    with pytest.raises(ValueError):
        L.der_decode(bytes.fromhex("300702020001020101"))  # needless leading zero


# ---------------------------------------------------------------- captured data: all checks pass


def test_modern_fixture_passes_except_pruned_history(ds, nodes):
    # the public RPC kept ledgers from 2026-11-23T13:49Z when captured: the round 30-32 anchors and
    # commit/reveal txs (sealed 2026-11-03..18) are unverifiable (WARN), round 33 is fully checked
    res = A.run_checks(ds, nodes)
    assert [r for r in res if r["status"] == "FAIL"] == []
    warn = sorted((r["check"], r["scope"]) for r in res if r["status"] == "WARN")
    assert warn == sorted((c, f"round {n}") for c in ("anchor", "convergence") for n in (30, 31, 32))
    assert all(
        "pruned" in r["reason"] or "retained history" in r["reason"] for r in res if r["status"] == "WARN"
    )
    assert statuses(res, "anchor", "round 33") == {"PASS"}
    assert statuses(res, "convergence", "round 33") == {"PASS"}
    for check in (
        "vl_decode",
        "sequence",
        "validity_window",
        "validator_set",
        "bundle_hashes",
        "mirror",
        "anchor",
        "convergence",
        "nodes",
    ):
        assert by(res, check), check


def test_legacy_fixture_expected_warns(nodes):
    ds = A.dataset(A.load_rows(os.path.join(FIX, "legacy.jsonl.gz")))
    res = A.run_checks(ds, nodes)
    assert statuses(res, "sequence", "round 2") == {"WARN"}  # FAILED round, no VL
    assert statuses(res, "anchor", "round 3") == {"WARN"}  # VL_PUBLISHED_MEMO_FAILED
    assert statuses(res, "bundle_hashes", "round 4") == {"WARN"}  # legacy layout
    assert statuses(res, "anchor", "round 4") == {"WARN"}  # pruned from the public RPC
    assert statuses(res, "vl_decode") == {"PASS"}
    assert statuses(res, "validator_set") == {"PASS"}


# ---------------------------------------------------------------- one synthetic mismatch per check


def test_vl_decode_detects_bad_blob_signature(ds, nodes):
    vl = ds["rounds"][32]["files"]["outputs/signed_validator_list.json"]
    s = vl["blobs_v2"][0]["signature"]
    vl["blobs_v2"][0]["signature"] = s[:-4] + ("0000" if s[-4:] != "0000" else "1111")
    res = A.run_checks(ds, nodes)
    assert statuses(res, "vl_decode", "round 32") == {"FAIL"}
    assert statuses(res, "vl_decode", "round 33") == {"PASS"}


def test_vl_decode_detects_swapped_validator_key(ds, nodes):
    vl = ds["rounds"][33]["files"]["outputs/signed_validator_list.json"]
    blob = json.loads(base64.b64decode(vl["blobs_v2"][0]["blob"]))
    a, b = blob["validators"][0], blob["validators"][1]
    a["manifest"], b["manifest"] = b["manifest"], a["manifest"]
    vl["blobs_v2"][0]["blob"] = base64.b64encode(json.dumps(blob, separators=(",", ":")).encode()).decode()
    reason = by(A.run_checks(ds, nodes), "vl_decode", "round 33")[0]
    assert reason["status"] == "FAIL"
    assert "signature does not verify" in reason["reason"] and "master key differs" in reason["reason"]


def test_sequence_detects_record_mismatch_and_gap(ds, nodes):
    ds["rounds"][31]["record"]["vl_sequence"] = 99
    res = A.run_checks(ds, nodes)
    assert "!= round vl_sequence 99" in by(res, "sequence", "round 31")[0]["reason"]
    assert statuses(res, "sequence", "round 31") == {"FAIL"}


def test_sequence_detects_missing_round(ds, nodes):
    del ds["rounds"][32]["files"]["outputs/signed_validator_list.json"]
    res = A.run_checks(ds, nodes)
    assert statuses(res, "sequence", "round 32") == {"FAIL"}  # COMPLETE round without a VL
    assert statuses(res, "sequence", "round 33") == {"WARN"}  # 31 follows 29: not +1


def _reblob(vl, **kw):
    blob = json.loads(base64.b64decode(vl["blobs_v2"][0]["blob"]))
    blob.update(kw)
    vl["blobs_v2"][0]["blob"] = base64.b64encode(json.dumps(blob, separators=(",", ":")).encode()).decode()
    return blob


def test_validity_window_detects_gap(ds, nodes):
    vl31 = ds["rounds"][31]["files"]["outputs/signed_validator_list.json"]
    eff32 = L.decode_vl(ds["rounds"][32]["files"]["outputs/signed_validator_list.json"])["blobs"][0][
        "effective"
    ]
    _reblob(vl31, expiration=eff32 - 60)
    res = A.run_checks(ds, nodes)
    r = by(res, "validity_window", "round 31")[0]
    assert r["status"] == "FAIL" and "no valid list in between" in r["reason"]


def test_validity_window_detects_inverted_and_expired(ds, nodes):
    vl30 = ds["rounds"][30]["files"]["outputs/signed_validator_list.json"]
    blob = json.loads(base64.b64decode(vl30["blobs_v2"][0]["blob"]))
    _reblob(vl30, expiration=blob["effective"] - 1)
    assert statuses(A.run_checks(ds, nodes), "validity_window", "round 30") == {"FAIL"}
    res = A.check_validity_window(ds, "2099-01-01T00:00:00Z")
    assert {r["status"] for r in res if r["scope"] == "latest"} == {"FAIL"}


def test_validator_set_detects_unl_mismatch(ds, nodes):
    ds["rounds"][33]["files"]["outputs/selected_unl.json"]["unl"].pop()
    assert statuses(A.run_checks(ds, nodes), "validator_set", "round 33") == {"FAIL"}


def test_validator_set_detects_previous_unl_mismatch(ds, nodes):
    pu = ds["rounds"][32]["files"]["inputs/previous_unl.json"]["previous_unl"]
    pu[0] = L.node_public_b58("ED" + "00" * 32)
    r = by(A.run_checks(ds, nodes), "validator_set", "round 32")[0]
    assert r["status"] == "FAIL" and "previous_unl" in r["reason"]


def test_validator_set_detects_oversize(ds, nodes):
    ds["live"]["config"]["unl_max_size"] = 3
    assert statuses(A.run_checks(ds, nodes), "validator_set") == {"FAIL"}


def test_selection_replay_matches_and_detects_score_change(ds, nodes):
    assert statuses(A.run_checks(ds, nodes), "selection") == {"PASS"}
    assert {r["scope"] for r in by(A.run_checks(ds, nodes), "selection")} == {
        f"round {n}" for n in (30, 31, 32, 33)
    }
    sel = ds["rounds"][33]["files"]["outputs/selected_unl.json"]
    challenger = sel["alternates"][0]
    for s in ds["rounds"][33]["files"]["outputs/final_scores.json"]["scores"]:
        if s["master_key"] == challenger:
            s["final_score"] = 100  # now beats the weakest incumbent by >= min gap: must be swapped in
    r = by(A.run_checks(ds, nodes), "selection", "round 33")[0]
    assert r["status"] == "FAIL" and challenger in r["reason"]


def test_replay_selection_rules():
    prev = ["a", "b"]
    # incumbent b (80) keeps its seat against c (82) with gap 3, loses against d (90)
    assert A.replay_selection([("a", 90), ("b", 80), ("c", 82)], prev, 40, 2, 3) == (["a", "b"], ["c"])
    assert A.replay_selection([("a", 90), ("b", 80), ("d", 90)], prev, 40, 2, 3) == (["a", "d"], ["b"])
    # first round: plain top-N; cutoff drops low scores
    assert A.replay_selection([("a", 50), ("b", 30)], [], 40, 5, 3) == (["a"], [])


def test_bundle_hashes_detect_changed_file(ds, nodes):
    ds["rounds"][31]["hashes"]["outputs/final_scores.json"] = "00" * 32
    r = by(A.run_checks(ds, nodes), "bundle_hashes", "round 31")[0]
    assert r["status"] == "FAIL" and "final_scores" in r["reason"] and "verification_hashes" in r["reason"]


def test_bundle_hashes_detect_hash_only_mismatch(ds, nodes):
    ds["rounds"][30]["hashes"]["raw/vhs_validators.json"] = "11" * 32
    assert statuses(A.run_checks(ds, nodes), "bundle_hashes", "round 30") == {"FAIL"}


def test_mirror_detects_divergent_mirror(ds, nodes):
    ds["live"]["mirror_vl"]["blobs_v2"][0]["signature"] = "00"
    assert statuses(A.run_checks(ds, nodes), "mirror", "vl.json") == {"FAIL"}


def test_mirror_detects_stale_live_vl(ds, nodes):
    ds["live"]["vl"] = copy.deepcopy(ds["rounds"][32]["files"]["outputs/signed_validator_list.json"])
    ds["live"]["mirror_vl"] = copy.deepcopy(ds["live"]["vl"])
    assert statuses(A.run_checks(ds, nodes), "mirror", "latest") == {"FAIL"}


def test_mirror_detects_unl_current_mismatch(ds, nodes):
    ds["live"]["unl_current"]["unl"] = ds["live"]["unl_current"]["unl"][1:]
    assert statuses(A.run_checks(ds, nodes), "mirror", "unl/current") == {"FAIL"}


def test_anchor_detects_wrong_cid(ds, nodes):
    h = ds["rounds"][33]["record"]["memo_tx_hash"]
    tx = ds["txs"][h]
    (_t, d) = A.memo_list(tx)[0]
    d["final_bundle_cid"] = "QmWrong"
    set_memo(tx, d)
    r = by(A.run_checks(ds, nodes), "anchor", "round 33")[0]
    assert r["status"] == "FAIL" and "memo CID QmWrong" in r["reason"]


def test_anchor_detects_wrong_sender_and_type(ds, nodes):
    h = ds["rounds"][33]["record"]["memo_tx_hash"]
    ds["txs"][h]["Account"] = "rNotThePublisher"
    ds["txs"][h]["Memos"][0]["Memo"]["MemoType"] = b"other".hex()
    r = by(A.run_checks(ds, nodes), "anchor", "round 33")[0]
    assert r["status"] == "FAIL" and "sender" in r["reason"] and "MemoType other" in r["reason"]


def test_anchor_missing_recent_tx_fails_but_pruned_warns(ds, nodes):
    h = ds["rounds"][33]["record"]["memo_tx_hash"]
    ds["txs"][h] = {"error": "txnNotFound", "status": "error"}
    assert statuses(A.run_checks(ds, nodes), "anchor", "round 33") == {"FAIL"}
    ds["live"]["rpc_history"]["first_close_time"] = 10**9  # 2031: every round predates retained history
    assert statuses(A.run_checks(ds, nodes), "anchor", "round 33") == {"WARN"}


def test_anchor_failed_tx_result(ds, nodes):
    h = ds["rounds"][33]["record"]["memo_tx_hash"]
    ds["txs"][h]["meta"]["TransactionResult"] = "tecNO_DST"
    assert statuses(A.run_checks(ds, nodes), "anchor", "round 33") == {"FAIL"}


def test_convergence_detects_summary_mismatch(ds, nodes):
    rep = ds["rounds"][33]["convergence"]["report"]
    rep["summary"]["committers"] += 1
    rep["summary"]["levels_matched"]["RAW"] -= 1
    r = by(A.run_checks(ds, nodes), "convergence", "round 33")[0]
    assert r["status"] == "FAIL" and "committers" in r["reason"] and "levels_matched" in r["reason"]


def test_convergence_detects_bad_anchor_memo_and_missing_reveal(ds, nodes):
    c = ds["rounds"][33]["convergence"]
    c["convergence_bundle_cid"] = "QmOther"
    p = c["report"]["participants"][0]
    ds["txs"][p["accepted_reveal_tx"]] = {"error": "txnNotFound", "status": "error"}
    r = by(A.run_checks(ds, nodes), "convergence", "round 33")[0]
    assert r["status"] == "FAIL" and "convergence_bundle_cid" in r["reason"] and "1 accepted" in r["reason"]


def test_convergence_flags_untracked_announced_round(ds, nodes):
    ds["rounds"][32]["convergence"] = {"round_number": 32, "phase": "not_tracked", "finalized": False}
    r = by(A.run_checks(ds, nodes), "convergence", "round 32")[0]
    assert r["status"] == "WARN" and "not_tracked although" in r["reason"]
    ds["rounds"][32]["record"]["override_type"] = "manual"
    assert by(A.run_checks(ds, nodes), "convergence", "round 32") == []  # documented not_tracked case


def test_nodes_detect_untrusted_key_and_disagreement(ds, nodes):
    n = copy.deepcopy(nodes)
    n["sentry"]["trusted_validator_keys"] = n["sentry"]["trusted_validator_keys"][1:]
    res = A.run_checks(ds, n)
    assert statuses(res, "nodes", "sentry") == {"FAIL"}
    assert statuses(res, "nodes", "validator") == {"PASS"}
    assert statuses(res, "nodes", "sentry vs validator") == {"FAIL"}


def test_nodes_detect_stale_list_and_bad_quorum(ds, nodes):
    n = copy.deepcopy(nodes)
    n["validator"]["publisher_lists"][0]["seq"] -= 1
    n["validator"]["validation_quorum"] = 3
    r = by(A.run_checks(ds, n), "nodes", "validator")[0]
    assert r["status"] == "FAIL" and "seq" in r["reason"] and "quorum 3 outside" in r["reason"]


# ---------------------------------------------------------------- analyses + outputs


def test_analyses_membership_and_tenure(ds):
    an = A.analyses(ds)
    assert [m["round"] for m in an["membership"]] == [30, 31, 32, 33]
    for prev, m in zip(an["membership"], an["membership"][1:], strict=False):
        assert m["size"] == prev["size"] + m["added"] - m["removed"]
        assert m["kept"] == prev["size"] - m["removed"]
    assert all(1 <= t["rounds_in_unl"] <= 4 for t in an["tenure"])
    assert {p["round"] for p in an["participation"]} == {30, 31, 32, 33}
    assert [e["round"] for e in an["entry_times"] if e["anchor_close_time"]] == [33]


def test_outputs_deterministic(tmp_path, modern_rows, nodes):
    outs = []
    for i in range(2):
        d = tmp_path / f"o{i}"
        ds = A.dataset(copy.deepcopy(modern_rows))
        A.write_outputs(str(d), ds, A.run_checks(ds, nodes), A.analyses(ds), nodes, "x" * 64)
        outs.append((d / "SHA256SUMS").read_text())
    assert outs[0] == outs[1]
    assert "REPORT.md" in outs[0] and "charts/churn.svg" in outs[0]


# ---------------------------------------------------------------- collector


class FakeNet:
    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def __call__(self, url, body):
        self.seen.append(url)
        a = self.answers[url]
        r = a.pop(0) if isinstance(a, list) else a
        if isinstance(r, Exception):
            raise r
        return r


def test_fetcher_retries_then_caches(tmp_path):
    cache = C.Cache(str(tmp_path / "c.jsonl"))
    net = FakeNet({"u": [(503, b"busy"), OSError("reset"), (200, b'{"a":1}')]})
    f = C.Fetcher(cache, gap=0, opener=net, sleep=lambda s: None)
    row = f.fetch("k", "u")
    assert row["status"] == 200 and f.retried == 2 and row["sha256"] == hashlib.sha256(b'{"a":1}').hexdigest()
    assert f.fetch("k", "u")["status"] == 200 and len(net.seen) == 3 and f.cached == 1
    again = C.Fetcher(C.Cache(str(tmp_path / "c.jsonl")), gap=0, opener=net, sleep=lambda s: None)
    again.fetch("k", "u")
    assert len(net.seen) == 3  # resumed from disk


def test_fetcher_gives_up_and_retry_if(tmp_path):
    cache = C.Cache(str(tmp_path / "c.jsonl"))
    net = FakeNet({"u": (502, b""), "v": (200, b'{"phase":"commit","finalized":false}')})
    f = C.Fetcher(cache, gap=0, retries=2, opener=net, sleep=lambda s: None)
    assert f.fetch("k", "u")["status"] == 502 and f.errors == 1 and not cache.final("k")
    f.fetch("c", "v", retry_if=C._conv_live)
    assert cache.rows["c"]["retry"] and not cache.final("c")


def test_cache_cuts_torn_line(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_bytes(b'{"key":"a","status":200,"body":"{}"}\n{"key":"b","sta')
    c = C.Cache(str(p))
    assert list(c.rows) == ["a"] and c.torn == len(b'{"key":"b","sta')
    assert p.read_bytes().endswith(b"}\n")


def test_tx_not_final():
    assert C._tx_not_final({"result": {"status": "success", "validated": False}})
    assert not C._tx_not_final({"result": {"status": "success", "validated": True}})
    assert not C._tx_not_final({"result": {"status": "error", "error": "txnNotFound"}})
    assert C._tx_not_final({"result": {"status": "error", "error": "tooBusy"}})


def test_pack_deterministic_and_trims(tmp_path):
    src = tmp_path / "c.jsonl"
    rows = [
        {
            "key": "round/9/file/raw/x.json",
            "status": 200,
            "body": '{"b": 2, "a": 1}',
            "t": "T",
            "sha256": "s",
        },
        {"key": "round/9/file/bundle.json", "status": 200, "body": "{}", "t": "T", "sha256": "s"},
        {"key": "e/vl", "status": 503, "body": "", "t": "T", "sha256": "s"},
    ]
    src.write_text("".join(json.dumps(r) + "\n" for r in rows))
    C.pack(str(src), str(tmp_path / "a.gz"))
    C.pack(str(src), str(tmp_path / "b.gz"))
    assert (tmp_path / "a.gz").read_bytes() == (tmp_path / "b.gz").read_bytes()
    got = {r["key"]: r for r in A.load_rows(str(tmp_path / "a.gz")).values()}
    assert got["round/9/file/raw/x.json"]["body"] is None
    assert got["round/9/file/raw/x.json"]["content_hash"] == L.content_hash({"a": 1, "b": 2})
    assert got["round/9/file/bundle.json"]["body"] == "{}" and "e/vl" not in got
