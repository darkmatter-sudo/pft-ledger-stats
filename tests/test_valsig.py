"""valsig tests: recorded-fixture verification, the five negative controls, decoder edge cases, the
quorum formula, and the recorder's pure logic (fake socket, no network)."""

import copy
import gzip
import json
import os
import random
import shutil
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "valsig"))
sys.path.insert(0, os.path.join(ROOT, "dunl"))
import collect_valsig as C  # noqa: E402
import lib_dunl as L  # noqa: E402
import lib_valsig as V  # noqa: E402
import verify_valsig as VV  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "valsig", "rec")


def args(**kw):
    base = {
        "min_hours": 0.0,
        "jobs": 1,
        "offline": True,
        "sample": 6,
        "rpc": "http://127.0.0.1:9/",
        "out": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(scope="module")
def base():
    rec = VV.Recording(FIX)
    sigs = VV.SigCache(1)
    return rec, sigs, VV.verify(rec, args(), sigs)


def statuses(ck):
    return {c["id"]: c["status"] for c in ck.items}


def check(ck, cid):
    return next(c for c in ck.items if c["id"] == cid)


def fixture_events():
    with gzip.open(os.path.join(FIX, "seg", "000001.jsonl.gz"), "rt") as fh:
        return [json.loads(x) for x in fh]


def copy_fixture(tmp_path, events=None, drop=()):
    d = tmp_path / "rec"
    shutil.copytree(FIX, d)
    for n in drop:
        os.remove(d / n)
    if events is not None:
        with gzip.open(d / "seg" / "000001.jsonl.gz", "wt") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
    return str(d)


# ---------------------------------------------------------------- recorded fixture, end to end


def test_fixture_all_checks_pass(base):
    _rec, _sigs, (ck, graded, rows, trusted, _sm) = base
    st = statuses(ck)
    assert all(v == "PASS" for v in st.values()), st
    assert len(trusted) == 25
    assert len(graded) == 416
    assert check(ck, "validations.trust")["classes"] == {"not_in_vl": 216, "trusted": 200}
    graded_rows = [r for r in rows if r["result"] == "met"]
    assert len(graded_rows) == 7 and all(r["trusted_signers"] == 25 and r["margin"] == 8 for r in graded_rows)
    assert [r["result"] for r in rows][0] == "edge_met"


def test_offline_run_is_byte_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        cmd = ["run", FIX, "--out", str(out), "--offline", "--min-hours", "0", "--sample", "6", "--jobs", "1"]
        assert VV.main(cmd) == 0
    for n in ("checks.json", "ledgers.csv", "validators.csv", "REPORT.md"):
        assert (a / n).read_bytes() == (b / n).read_bytes(), n
    doc = json.loads((a / "checks.json").read_text())
    assert doc["summary"] == {"PASS": 18, "WARN": 0, "FAIL": 0}
    assert "seg/000001.jsonl.gz" in doc["inputs"] and "rpc-cache.json" in doc["inputs"]


def test_offline_without_cache_fails_crosscheck(tmp_path):
    d = copy_fixture(tmp_path, drop=["rpc-cache.json"])
    rc = VV.main(["run", d, "--out", str(tmp_path / "o"), "--offline", "--min-hours", "0", "--sample", "6"])
    doc = json.loads((tmp_path / "o" / "checks.json").read_text())
    assert rc == 1 and check(SimpleNamespace(items=doc["checks"]), "rpc.crosscheck")["status"] == "FAIL"


def test_rpc_hash_mismatch_fails(tmp_path):
    d = copy_fixture(tmp_path)
    p = os.path.join(d, "rpc-cache.json")
    cache = json.load(open(p))
    k = sorted(cache)[0]
    cache[k]["ledger_hash"] = "00" * 32
    json.dump(cache, open(p, "w"))
    VV.main(["run", d, "--out", str(tmp_path / "o"), "--offline", "--min-hours", "0", "--sample", "6"])
    c = [
        x
        for x in json.loads((tmp_path / "o" / "checks.json").read_text())["checks"]
        if x["id"] == "rpc.crosscheck"
    ]
    assert c[0]["status"] == "FAIL" and c[0]["failures"][0]["li"] == int(k)


def test_too_few_samples_fails(tmp_path):
    """Every graded ledger matches in the cache, but 7 samples are below the floor of 50."""
    d = copy_fixture(tmp_path)
    rec = VV.Recording(d)
    ck, _g, rows, _t, _s = VV.verify(rec, args(), VV.SigCache(1))
    cache = {
        str(r["ledger_index"]): {"ledger_hash": r["ledger_hash"], "validated": True, "close_time": 0}
        for r in rows
        if r["result"] == "met"
    }
    json.dump(cache, open(os.path.join(d, "rpc-cache.json"), "w"))
    VV.check_rpc(rec, ck, rows, args(sample=60))
    c = check(ck, "rpc.crosscheck")
    assert c["status"] == "FAIL" and c["failures"] == [] and len(c["sampled"]) == 7


# ---------------------------------------------------------------- the five negative controls


@pytest.mark.parametrize("name", [n for n, _f in VV.CONTROLS])
def test_negative_control_fails_as_designed(base, name):
    _rec, sigs, b = base
    fn = dict(VV.CONTROLS)[name]
    hit, c, where, _others = VV.run_control(args(data=FIX), sigs, b, fn)
    assert hit, (name, c)
    assert c["status"] == "FAIL"


def test_controls_command_passes_on_fixture(capsys):
    assert VV.main(["controls", FIX, "--min-hours", "0", "--jobs", "1"]) == 0
    out = capsys.readouterr().out
    assert out.count("FAILS_AS_DESIGNED") == 5 and "RESULT PASS controls=5 detected=5" in out


def test_controls_refuse_failing_baseline(tmp_path, capsys):
    ev = [e for e in fixture_events() if e["ev"] != "snap"]  # trust.snapshots FAILs with no snapshot
    d = copy_fixture(tmp_path, ev)
    assert VV.main(["controls", d, "--min-hours", "0", "--jobs", "1"]) == 1
    assert "baseline must pass" in capsys.readouterr().out


def test_below_quorum_exact_boundary(base, tmp_path):
    """quorum - 1 signers FAILs (control above); exactly quorum signers still PASSes."""
    _rec, _sigs, (_ck, _g, rows, _t, signing_map) = base
    row = next(r for r in rows if r["result"] == "met")
    masters = sorted(row["signer_masters"])[row["quorum"] :]
    drop = {sk for sk, (mk, _s) in signing_map.items() if mk in masters}
    ev = [
        e
        for e in fixture_events()
        if not (e["ev"] == "val" and e["li"] == row["ledger_index"] and VV.hexkey(e["k"]) in drop)
    ]
    ck, _g2, rows2, _t2, _s2 = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    r2 = next(r for r in rows2 if r["ledger_index"] == row["ledger_index"])
    assert r2["trusted_signers"] == row["quorum"] and r2["margin"] == 0 and r2["result"] == "met"
    assert check(ck, "quorum.ledgers")["status"] == "PASS"


def test_partial_and_wrong_hash_validations_do_not_count(base, tmp_path):
    _rec, _sigs, (_ck, _g, rows, _t, _sm) = base
    row = next(r for r in rows if r["result"] == "met")
    li = row["ledger_index"]
    ev = fixture_events()
    # every trusted validation of this ledger now claims another hash in the stream field only:
    # field mismatch -> invalid, so none counts and the ledger must fall below quorum
    for e in ev:
        if e["ev"] == "val" and e["li"] == li:
            e["lh"] = "11" * 32
    ck, _g2, rows2, _t2, _s2 = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    r2 = next(r for r in rows2 if r["ledger_index"] == li)
    assert r2["trusted_signers"] == 0 and r2["result"] == "BELOW_QUORUM"
    assert check(ck, "validations.fields")["status"] == "FAIL"


@pytest.mark.parametrize(
    "edit,col",
    [
        ({"full": False}, "partial_validations"),
        ({"ok": False, "reason": "bad_signature"}, "invalid_validations"),
        ({"lh": "11" * 32}, "trusted_other_hash"),
    ],
    ids=["partial", "invalid", "other-hash"],
)
def test_only_full_valid_same_hash_validations_count(base, edit, col):
    """Graded validations of one ledger that are partial, invalid or for another hash never count."""
    rec, _sigs, (_ck, graded, rows, trusted, _sm) = base
    li = next(r for r in rows if r["result"] == "met")["ledger_index"]
    g2 = [dict(g, **edit) if g["li"] == li and g["trusted"] else g for g in copy.deepcopy(graded)]
    ck = VV.Checks()
    rows2 = VV.check_quorum(rec, ck, g2, trusted)
    r2 = next(r for r in rows2 if r["ledger_index"] == li)
    assert r2["trusted_signers"] == 0 and r2["result"] == "BELOW_QUORUM"
    assert r2[col] >= 25
    assert check(ck, "quorum.ledgers")["failures"] == [{"li": li, "signers": 0, "quorum": 17}]


def test_negative_unl_lowers_signers_and_quorum(base, tmp_path):
    """Masters in the NegativeUNL are excluded from signers, and quorum is recomputed without them:
    4 of 25 disabled -> 21 signers, quorum 16 (C++ table); a node still claiming 17 is a FAIL."""
    _rec, _sigs, (_ck, _g, _rows, trusted, _sm) = base
    disabled = sorted(trusted)[:4]
    ev = fixture_events()
    for e in ev:
        if e["ev"] == "q":
            e["nunl"] = {"DisabledValidators": [{"DisabledValidator": {"PublicKey": k}} for k in disabled]}
            e["quorum"] = 16
    ck, _g2, rows2, _t2, _s2 = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    met = [r for r in rows2 if r["result"] == "met"]
    assert len(met) == 7
    assert all(r["trusted_signers"] == 21 and r["nunl_excluded"] == 4 and r["margin"] == 5 for r in met)
    assert check(ck, "quorum.node")["status"] == "PASS"
    for e in ev:
        if e["ev"] == "q":
            e["quorum"] = 17
    ck3, *_ = VV.verify(VV.Recording(copy_fixture(tmp_path / "b", ev)), args(), VV.SigCache(1))
    assert check(ck3, "quorum.node")["status"] == "FAIL"
    assert check(ck3, "quorum.node")["failures"][0] == {"li": 8398641, "node": 17, "expected": 16}


# ---------------------------------------------------------------- recording-level failures


def test_gap_event_fails(tmp_path):
    ev = fixture_events()
    i = next(i for i, e in enumerate(ev) if e["ev"] == "ledger")
    ev.insert(i + 1, {"ev": "gap", "from": 1, "to": 1, "count": 1, "reason": "reconnect", "t": ev[i]["t"]})
    ck, *_ = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    assert check(ck, "recording.gaps")["status"] == "FAIL"


def test_index_hole_fails(tmp_path):
    ev = fixture_events()
    li = [e["li"] for e in ev if e["ev"] == "ledger"][3]
    ev = [e for e in ev if not (e["ev"] == "ledger" and e["li"] == li)]
    ck, *_ = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    c = check(ck, "recording.gaps")
    assert c["status"] == "FAIL" and c["index_holes"] == [(li - 1, li + 1)]


def test_open_segment_fails(tmp_path):
    d = copy_fixture(tmp_path)
    open(os.path.join(d, "seg", "current.jsonl"), "w").close()
    ck, *_ = VV.verify(VV.Recording(d), args(), VV.SigCache(1))
    assert check(ck, "recording.files")["status"] == "FAIL"


def test_short_window_fails():
    ck, *_ = VV.verify(VV.Recording(FIX), args(min_hours=4), VV.SigCache(1))
    assert check(ck, "recording.window")["status"] == "FAIL"


def test_missing_vl_fails(tmp_path):
    d = copy_fixture(tmp_path, drop=["vl-end.json"])
    ck, *_ = VV.verify(VV.Recording(d), args(), VV.SigCache(1))
    assert check(ck, "vl.end")["status"] == "FAIL" and check(ck, "vl.start")["status"] == "PASS"


def test_tampered_vl_blob_signature_fails(tmp_path):
    d = copy_fixture(tmp_path)
    p = os.path.join(d, "vl-start.json")
    vl = json.load(open(p))
    sig = bytearray(bytes.fromhex(vl["blobs_v2"][0]["signature"]))
    sig[-1] ^= 1
    vl["blobs_v2"][0]["signature"] = sig.hex().upper()
    json.dump(vl, open(p, "w"))
    ck, *_ = VV.verify(VV.Recording(d), args(), VV.SigCache(1))
    assert check(ck, "vl.start")["status"] == "FAIL"
    assert "blob signature invalid" in check(ck, "vl.start")["reason"]


def test_snapshot_trusting_other_set_fails(tmp_path):
    ev = fixture_events()
    for e in ev:
        if e["ev"] == "snap":
            e["v"]["trusted_validator_keys"] = e["v"]["trusted_validator_keys"][1:]
    ck, *_ = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    assert check(ck, "trust.snapshots")["status"] == "FAIL"


def test_tampered_manifest_fails(tmp_path):
    ev = fixture_events()
    for e in ev:
        if e["ev"] == "man":
            raw = bytearray(L.base64.b64decode(e["m"]))
            raw[-1] ^= 1  # last byte of the master signature
            e["m"] = L.base64.b64encode(bytes(raw)).decode()
            break
    ck, *_ = VV.verify(VV.Recording(copy_fixture(tmp_path, ev)), args(), VV.SigCache(1))
    assert check(ck, "manifests.verify")["status"] == "FAIL"


# ---------------------------------------------------------------- decoder and formula


def first_val():
    return next(e for e in fixture_events() if e["ev"] == "val")


def test_decode_matches_stream_fields():
    e = first_val()
    d = V.decode_validation(e["d"])
    assert d["ledger_index"] == e["li"] and d["ledger_hash"] == e["lh"] and d["signing_time"] == e["st"]
    assert d["flags"] == e["fl"] and d["full"] == e["full"] and d["validated_hash"] == e["vh"]
    assert d["signing_key"] == L.node_public_hex(e["k"]) and d["signature"].hex().upper() == e["sig"]
    assert d["signed"].startswith(b"VAL\x00") and e["sig"].lower() not in d["signed"].hex()
    assert V.check_signature(d) == (True, "ok")


def test_decode_rejects_bad_blobs():
    h = first_val()["d"]
    with pytest.raises(ValueError, match="truncated"):
        V.decode_validation(h[:-10])
    with pytest.raises(ValueError, match="unsupported field type"):
        V.decode_validation("E1" + h)  # type 14 (STObject) is not flat
    with pytest.raises(ValueError, match="canonical order"):
        fields = V.parse_fields(bytes.fromhex(h))
        a, b = fields[0], fields[1]
        raw = bytes.fromhex(h)
        V.decode_validation((raw[b[2] : b[3]] + raw[a[2] : a[3]] + raw[b[3] :]).hex())
    with pytest.raises(ValueError, match="missing sfSignature"):
        raw = bytes.fromhex(h)
        sig = next(f for f in V.parse_fields(raw) if (f[0], f[1]) == (7, 6))
        V.decode_validation((raw[: sig[2]] + raw[sig[3] :]).hex())


def test_high_s_rejected_when_fully_canonical():
    d = V.decode_validation(first_val()["d"])
    r, s = L.der_decode(d["signature"])

    def der(x):
        b = x.to_bytes((x.bit_length() + 8) // 8, "big")
        return b"\x02" + bytes([len(b)]) + b

    body = der(r) + der(L._SN - s)
    d["signature"] = b"\x30" + bytes([len(body)]) + body
    assert d["flags"] & V.VF_FULLY_CANONICAL
    assert V.check_signature(d) == (False, "not_fully_canonical")


@pytest.mark.parametrize(
    "unl,eff,want",
    [
        (25, 25, 17),
        (25, 21, 16),
        (25, 24, 17),
        (25, 23, 16),
        (3, 3, 3),
        (100, 100, 67),
        (100, 50, 61),
        (1, 1, 1),
    ],
)
def test_expected_quorum(unl, eff, want):
    assert V.expected_quorum(unl, eff) == want


def test_expected_quorum_matches_cpp_table():
    """Every (unl 1..120, effective 0..unl) pair against the compiled C++ expression (quorum_ref.cpp):
    float rounding makes ceil(25 * 0.6f) = 16, not 15, so the arithmetic must be single precision."""
    rows = [
        tuple(map(int, x.split()))
        for x in open(os.path.join(ROOT, "tests", "fixtures", "valsig", "quorum-cpp.txt"))
    ]
    assert len(rows) == 7380
    assert [r for r in rows if V.expected_quorum(r[0], r[1]) != r[2]] == []


def test_shamir_matches_affine():
    rnd = random.Random(7)
    for _ in range(20):
        a, b = rnd.randrange(1, L._SN), rnd.randrange(1, L._SN)
        q = L._sk_mul(rnd.randrange(1, L._SN), L._SG)
        assert L._sk_shamir(a, L._SG, b, q) == L._sk_add(L._sk_mul(a, L._SG), L._sk_mul(b, q))
    assert L._sk_shamir(5, L._SG, L._SN - 5, L._SG) is None
    assert L._sk_shamir(3, L._SG, 0, L._SG) == L._sk_mul(3, L._SG)


# ---------------------------------------------------------------- recorder (pure logic + fake socket)


class Rec:
    def __init__(self):
        self.events, self.asked = [], []

    def emit(self, e):
        self.events.append(e)

    def ask(self, cmd, **kw):
        self.asked.append((cmd, kw))


def stream_val(e):
    return {
        "type": "validationReceived",
        "validation_public_key": e["k"],
        "ledger_index": str(e["li"]),
        "ledger_hash": e["lh"],
        "signing_time": e["st"],
        "flags": e["fl"],
        "full": e["full"],
        "validated_hash": e.get("vh"),
        "master_key": e.get("mk"),
        "signature": e["sig"],
        "data": e["d"],
        "cookie": "123",
    }


def test_handler_records_validation_once_and_asks_manifest():
    r = Rec()
    h = C.Handler(C.State(), r.emit, r.ask)
    e = first_val()
    h.on_message(stream_val(e), 1.0)
    h.on_message(stream_val(e), 1.1)
    vals = [x for x in r.events if x["ev"] == "val"]
    assert len(vals) == 1 and h.dups == 1 and vals[0]["d"] == e["d"] and "cookie" not in vals[0]
    assert [a[0] for a in r.asked] == ["manifest"] and r.asked[0][1]["public_key"] == e["k"]


def test_handler_drops_validation_without_data():
    r = Rec()
    h = C.Handler(C.State(), r.emit, r.ask)
    m = stream_val(first_val())
    m.pop("data")
    h.on_message(m, 1.0)
    assert r.events == []


def test_handler_gap_reasons():
    r = Rec()
    st = C.State()
    h = C.Handler(st, r.emit, r.ask)
    led = lambda i: {"type": "ledgerClosed", "ledger_index": i, "ledger_hash": "AB", "ledger_time": 1}  # noqa: E731
    h.on_message(led(10), 1)
    h.on_message(led(12), 2)
    h.on_connect(reconnect=True, t=3, n=2)
    h.on_message(led(15), 4)
    h.on_message(led(15), 5)
    gaps = [(x["from"], x["to"], x["reason"]) for x in r.events if x["ev"] == "gap"]
    assert gaps == [(11, 11, "stream_skip"), (13, 14, "reconnect")]
    assert [x["li"] for x in r.events if x["ev"] == "ledger"] == [10, 12, 15]
    h2 = C.Handler(st, r.emit, r.ask)
    assert h2.gap_reason == "restart"


def test_handler_snapshot_and_quorum_only_on_change():
    r = Rec()
    st = C.State()
    h = C.Handler(st, r.emit, r.ask)
    snap = next(e for e in fixture_events() if e["ev"] == "snap")["v"]
    for _round in range(2):
        h.poll()
        ids = {a[0]: a[1]["id"] for a in r.asked[-3:]}
        v = copy.deepcopy(snap)
        v["publisher_lists"][0]["uri"] = "192.0.2.1:2559"
        h.on_message({"type": "response", "id": ids["validators"], "status": "success", "result": v}, 1)
        h.on_message(
            {
                "type": "response",
                "id": ids["server_info"],
                "status": "success",
                "result": {
                    "info": {
                        "validation_quorum": 17,
                        "validator_list": {"count": 1},
                        "validated_ledger": {"seq": 5},
                    }
                },
            },
            1,
        )
        h.on_message(
            {"type": "response", "id": ids["ledger_entry"], "status": "error", "error": "entryNotFound"}, 1
        )
    snaps = [x for x in r.events if x["ev"] == "snap"]
    qs = [x for x in r.events if x["ev"] == "q"]
    assert len(snaps) == 1 and len(qs) == 1
    assert "192.0.2.1" not in json.dumps(snaps[0]) and qs[0]["nunl"] == {} and qs[0]["quorum"] == 17
    assert sum(1 for a in r.asked if a[0] == "manifest") == 25  # every trusted master, once


def test_handler_nunl_error_is_unknown_not_empty():
    r = Rec()
    h = C.Handler(C.State(), r.emit, r.ask)
    h.poll()
    ids = {a[0]: a[1]["id"] for a in r.asked}
    h.on_message(
        {
            "type": "response",
            "id": ids["server_info"],
            "status": "success",
            "result": {"info": {"validation_quorum": 17, "validated_ledger": {"seq": 5}}},
        },
        1,
    )
    h.on_message({"type": "response", "id": ids["ledger_entry"], "status": "error", "error": "noNetwork"}, 1)
    q = [x for x in r.events if x["ev"] == "q"][0]
    assert q["nunl"] is None


def test_manifest_stream_event_re_asks():
    r = Rec()
    h = C.Handler(C.State(), r.emit, r.ask)
    h.want_manifest("nHX")
    h.on_message({"type": "manifestReceived", "master_key": "nHX", "signing_key": "n9Y", "seq": 2}, 1)
    assert [a[1]["public_key"] for a in r.asked if a[0] == "manifest"] == ["nHX", "nHX"]


def test_allowlist_refuses_other_commands():
    sent = []
    send = C.make_send(SimpleNamespace(send=sent.append))
    send("server_info", id=1)
    for bad in ("submit", "sign", "stop", "ledger_accept", "connect"):
        with pytest.raises(ValueError, match="refusing"):
            send(bad)
    assert len(sent) == 1


def test_segments_torn_tail_close_and_resume(tmp_path):
    out = str(tmp_path)
    s = C.Segments(out)
    s.write({"ev": "ledger", "li": 1, "t": 1})
    s.write({"ev": "val", "li": 1, "k": "a", "lh": "x", "t": 1})
    s.fh.write('{"ev":"ledger","li":2')  # torn line, as a SIGKILL mid-write leaves it
    s.fh.flush()
    s.fh.close()
    s.fh = None
    assert C.drop_torn_tail(s.cur) == len('{"ev":"ledger","li":2')
    with open(s.cur, "rb") as fh:
        body = fh.read()
    assert body.endswith(b"\n") and body.count(b"\n") == 2 and b'"li":2' not in body
    name = s.close_current()
    assert os.path.basename(name) == "000001.jsonl.gz" and not os.path.exists(s.cur)
    with open(name, "rb") as fh:
        assert fh.read(10)[4:8] == b"\x00\x00\x00\x00"  # gzip mtime 0: byte-stable segments
    st = C.State().load(C.Segments(out).lines())
    assert st.last_ledger == 1 and st.vals == 1 and st.has(1, "a", "x") and st.bad_lines == 0
    assert os.path.basename(C.Segments(out).next_name()) == "000002.jsonl.gz"


class FakeWs:
    def __init__(self, msgs, clock):
        self.msgs, self.clock, self.sent = list(msgs), clock, []

    def settimeout(self, t):
        pass

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        if not self.msgs:
            self.clock.t = 10**10  # window over
            return json.dumps({"type": "noop"})
        self.clock.t += 1
        return json.dumps(self.msgs.pop(0))

    def close(self):
        pass


def test_run_end_to_end_with_fake_socket(tmp_path):
    clock = SimpleNamespace(t=1000.0)
    e = first_val()
    msgs = [
        {"type": "ledgerClosed", "ledger_index": e["li"] - 1, "ledger_hash": "AA", "ledger_time": 5},
        stream_val(e),
        {"type": "ledgerClosed", "ledger_index": e["li"], "ledger_hash": e["lh"], "ledger_time": 9},
    ]
    ws = FakeWs(msgs, clock)
    a = SimpleNamespace(
        url="ws://127.0.0.1:6006/",
        out=str(tmp_path),
        log=None,
        hours=1,
        rotate=1000,
        poll=10**9,
        silence=60,
        retry=0,
        max_fail=1,
    )
    C._stop = None
    assert C.run(a, connect=lambda url, timeout: ws, clock=lambda: clock.t) == 0
    assert ws.sent[0] == {"command": "subscribe", "id": "sub", "streams": C.STREAMS}
    assert {m["command"] for m in ws.sent} <= C.ALLOWED_COMMANDS
    segs = C.Segments(str(tmp_path)).closed()
    assert len(segs) == 1 and not os.path.exists(os.path.join(str(tmp_path), "seg", "current.jsonl"))
    evs = [json.loads(x) for x in gzip.open(segs[0], "rt")]
    assert [x["ev"] for x in evs if x["ev"] in ("session", "conn", "ledger", "val", "stop")] == [
        "session",
        "conn",
        "ledger",
        "val",
        "ledger",
        "stop",
    ]
    assert evs[-1]["reason"] == "window complete"
