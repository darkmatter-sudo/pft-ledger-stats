"""valdomain tests: the signed synthetic fixture (every class, hand-written expectations), the
negative controls, the attestation message and checks, the strict TOML parser (against tomli when
installed), the collector's bounded fetch / resume / privacy logic with stub openers (no network),
and the committed real collection when present."""

import base64
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "valdomain"))
import collect_valdomain as C  # noqa: E402
import lib_valdomain as D  # noqa: E402
import verify_valdomain as V  # noqa: E402

L = D.L
FIXD = os.path.join(ROOT, "tests", "fixtures", "valdomain")
FIX = os.path.join(FIXD, "data")
REAL = os.path.join(ROOT, "valdomain", "data", "run-01")
with open(os.path.join(FIXD, "expected.json")) as _fh:
    EXPECTED = json.load(_fh)
PFT, XRP = D.TOML_PATHS


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    shutil.copytree(FIX, d)
    return str(d)


def by_name(rows):
    return {EXPECTED[r["master"]]["name"]: r for r in rows}


def failed_ids(ck):
    return [r["id"] for r in ck.failed]


def rewrite_jsonl(path, fn):
    lines = C.load_jsonl(path)
    fn(lines)
    with open(path, "w") as fh:
        fh.writelines(json.dumps(e, sort_keys=True) + "\n" for e in lines)


# ---------------------------------------------------------------- fixture: classes and checks


def test_fixture_every_validator_gets_its_hand_written_class():
    ck, rows = V.verify(FIX)
    assert ck.failed == []
    assert len(rows) == len(EXPECTED) == 13
    assert {r["master"]: r["class"] for r in rows} == {k: v["class"] for k, v in EXPECTED.items()}


def test_fixture_counts_cover_every_class():
    _ck, rows = V.verify(FIX)
    assert V.counts(rows) == {
        "verified": 3,
        "bad_attestation": 2,
        "no_attestation": 1,
        "key_not_listed": 2,
        "toml_unparseable": 1,
        "http_error": 2,
        "unreachable": 1,
        "no_domain": 1,
    }


def test_best_of_paths_and_per_path_classes():
    _ck, rows = V.verify(FIX)
    r = by_name(rows)
    assert r["xrponly.example"]["paths"][PFT]["class"] == "http_error"
    assert r["xrponly.example"]["paths"][XRP]["class"] == "verified"
    assert r["xrponly.example"]["class"] == "verified"
    assert r["badatt.example"]["paths"][XRP]["class"] == "unreachable"
    assert r["trunc.example"]["paths"][PFT] == {"class": "http_error", "status": 200, "truncated": True}
    assert r["notlisted.example"]["paths"][PFT]["entries"] == 1
    assert r["notlisted.example"]["paths"][PFT]["matching"] == 0


def test_shared_domain_classifies_each_key_separately():
    _ck, rows = V.verify(FIX)
    r = by_name(rows)
    assert r["shared.example/A"]["class"] == "verified"
    assert r["shared.example/B"]["class"] == "key_not_listed"


def test_crosscheck_states():
    ck, rows = V.verify(FIX)
    r = by_name(rows)
    assert r["noatt.example"]["public"] == "newer_same_domain"
    assert r["httperr.example"]["public"] == "newer_other_domain"
    assert r["verified.example"]["node"] == "same"
    cc = next(x for x in ck.rows if x["id"] == "crosscheck")
    assert cc["ok"] and "25 of 26" in cc["msg"]


def test_run_exit_0_and_outputs_deterministic(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    assert V.run(FIX, str(a)) == 0
    assert V.run(FIX, str(b)) == 0
    for n in ("results.json", "validators.csv", "table.md"):
        assert (a / n).read_bytes() == (b / n).read_bytes()
    out = capsys.readouterr().out
    assert "RESULT OK exit=0" in out
    assert "verified=3" in out and "no_domain=1" in out


def test_table_has_key_prefix_domain_class():
    _ck, rows = V.verify(FIX)
    _js, cs, md = V.outputs(_ck, rows)
    lines = cs.decode().splitlines()
    assert lines[0].startswith("key_prefix,domain,class,")
    v = next(k for k, e in EXPECTED.items() if e["name"] == "verified.example")
    assert f"{v[:10]},verified.example,verified," in cs.decode()
    assert f"| `{v[:10]}` | verified.example | verified |" in md.decode()
    assert "| total | 13 |" in md.decode()


def test_no_data_exit_2(tmp_path, capsys):
    assert V.run(str(tmp_path)) == 2
    assert "NO DATA" in capsys.readouterr().out


def test_missing_vl_is_no_data(data):
    os.remove(os.path.join(data, "vl.json"))
    with pytest.raises(V.NoData):
        V.verify(data)


# ---------------------------------------------------------------- failures -> exit 1


def test_sums_detect_edit_unlisted_and_missing(data):
    with open(os.path.join(data, "node.json"), "a") as fh:
        fh.write(" ")
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["sums"] and "node.json: sha256 differs" in ck.failed[0]["msg"]
    with open(os.path.join(data, "extra.txt"), "w") as fh:
        fh.write("x")
    C.write_sums(data)
    os.remove(os.path.join(data, "extra.txt"))
    ck, _ = V.verify(data)
    assert "extra.txt: missing" in ck.failed[0]["msg"]
    C.write_sums(data)
    with open(os.path.join(data, "late.txt"), "w") as fh:
        fh.write("x")
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["sums"] and "late.txt: not listed" in ck.failed[0]["msg"]
    os.remove(os.path.join(data, "SHA256SUMS"))
    ck, _ = V.verify(data)
    assert "SHA256SUMS missing" in ck.failed[0]["msg"]


def test_run_exit_1_on_inconsistency(data, capsys):
    with open(os.path.join(data, "node.json"), "a") as fh:
        fh.write(" ")
    assert V.run(data) == 1
    assert "RESULT INCONSISTENT exit=1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "edit,reason",
    [
        (
            lambda n: n["validators"]["publisher_lists"][0].update(pubkey_publisher="ED" + "00" * 32),
            "different publisher",
        ),
        (lambda n: n["validators"]["publisher_lists"][0].update(seq=8), "node list seq 8"),
        (lambda n: n["validators"]["publisher_lists"][0]["list"].pop(), "node trusted list != VL"),
        (
            lambda n: n["validator_list_sites"]["validator_sites"][0].update(
                uri="https://other.example/vl.json"
            ),
            "site",
        ),
    ],
    ids=["publisher", "seq", "list", "site"],
)
def test_vl_node_trust_mismatches(data, edit, reason):
    p = os.path.join(data, "node.json")
    with open(p) as fh:
        node = json.load(fh)
    edit(node)
    C.write_atomic(p, C.dump(node))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["vl"] and reason in ck.failed[0]["msg"]


def test_vl_fetch_record_and_validity(data):
    p = os.path.join(data, "vl-fetch.json")
    with open(p) as fh:
        rec = json.load(fh)
    C.write_atomic(p, C.dump(dict(rec, status=304)))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["vl"] and "vl-fetch.json does not match" in ck.failed[0]["msg"]
    C.write_atomic(p, C.dump(dict(rec, t=rec["t"] + 400 * 86400)))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["vl"] and "not valid at fetch time" in ck.failed[0]["msg"]
    C.write_atomic(p, C.dump(dict(rec, t=rec["t"] - 2 * 86400)))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert "not valid at fetch time" in ck.failed[0]["msg"]


def test_vl_blob_signature_and_publisher_manifest(data):
    p = os.path.join(data, "vl.json")
    with open(p) as fh:
        vl = json.load(fh)
    inner = json.loads(base64.b64decode(vl["blobs_v2"][0]["blob"]))
    inner["validators"] = inner["validators"][:-1]
    vl2 = dict(
        vl, blobs_v2=[dict(vl["blobs_v2"][0], blob=base64.b64encode(json.dumps(inner).encode()).decode())]
    )
    raw = json.dumps(vl2).encode()
    C.write_atomic(p, raw)
    rp = os.path.join(data, "vl-fetch.json")
    with open(rp) as fh:
        rec = json.load(fh)
    C.write_atomic(rp, C.dump(dict(rec, sha256=hashlib.sha256(raw).hexdigest())))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert "vl" in failed_ids(ck) and "blob signature invalid" in ck.failed[0]["msg"]


def test_vl_wrong_public_key_field(data):
    p = os.path.join(data, "vl.json")
    with open(p) as fh:
        vl = json.load(fh)
    raw = json.dumps(dict(vl, public_key="ED" + "11" * 32)).encode()
    C.write_atomic(p, raw)
    rp = os.path.join(data, "vl-fetch.json")
    with open(rp) as fh:
        rec = json.load(fh)
    C.write_atomic(rp, C.dump(dict(rec, sha256=hashlib.sha256(raw).hexdigest())))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["vl"] and "public_key != publisher manifest master" in ck.failed[0]["msg"]


def test_bad_vl_manifest_detected():
    _ck, rows = V.verify(FIX)
    ck = V.Checks()
    with open(os.path.join(FIX, "vl.json")) as fh:
        d = L.decode_vl(json.load(fh))
    vals = [(k.upper(), L.parse_manifest(m)) for k, m in d["blobs"][0]["validators"]]
    k, m = vals[0]
    vals[0] = (k, dict(m, master_signature=bytes(64)))
    V.check_manifests(ck, vals)
    assert failed_ids(ck) == ["manifests"] and ck.failed[0]["failures"] == [L.node_public_b58(k)]
    ck = V.Checks()
    vals[0] = (vals[1][0], m)
    V.check_manifests(ck, vals[:1])
    assert failed_ids(ck) == ["manifests"]


def test_cache_record_naming_other_domain(data):
    p = os.path.join(data, "toml", "verified.example", "pft-ledger.json")
    with open(p) as fh:
        rec = json.load(fh)
    C.write_atomic(p, C.dump(dict(rec, domain="other.example")))
    C.write_sums(data)
    ck, rows = V.verify(data)
    assert failed_ids(ck) == ["toml"] and "cache record names other.example" in ck.failed[0]["msg"]
    assert by_name(rows)["verified.example"]["class"] is None


def test_unknown_outcome_and_200_without_body(data):
    p = os.path.join(data, "toml", "verified.example", "pft-ledger.json")
    with open(p) as fh:
        rec = json.load(fh)
    C.write_atomic(p, C.dump(dict(rec, outcome="maybe")))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["toml"] and "unknown outcome" in ck.failed[0]["msg"]
    rec.pop("body_b64")
    C.write_atomic(p, C.dump(rec))
    C.write_sums(data)
    ck, _ = V.verify(data)
    assert failed_ids(ck) == ["toml"] and "without a cached body" in ck.failed[0]["msg"]


def test_crosscheck_domain_differs_at_same_seq(data):
    with open(os.path.join(FIXD, "conflict.json")) as fh:
        conflict = json.load(fh)

    def swap(lines):
        e = next(e for e in lines if e["src"] == "node" and e["master"] == conflict["master"])
        e["answer"]["manifest"] = conflict["manifest"]
        e["answer"]["details"]["domain"] = "evil.example"

    rewrite_jsonl(os.path.join(data, "manifests.jsonl"), swap)
    C.write_sums(data)
    ck, rows = V.verify(data)
    assert failed_ids(ck) == ["crosscheck"]
    assert "domain 'evil.example' != VL 'verified.example' at seq 3" in ck.failed[0]["msg"]
    assert by_name(rows)["verified.example"]["node"] == "domain_differs"


@pytest.mark.parametrize(
    "edit,state,reason",
    [
        (lambda e: e.pop("answer") and e.update(error="URLError: x"), "missing", "no answer (URLError: x)"),
        (lambda e: e["answer"].update(manifest="!!"), "bad", "does not decode"),
        (lambda e: e["answer"]["details"].update(seq=9), "bad", "decoded details differ"),
    ],
    ids=["no-answer", "undecodable", "details-seq"],
)
def test_crosscheck_answer_problems(data, edit, state, reason):
    tgt = next(k for k, e in EXPECTED.items() if e["name"] == "verified.example")
    hexk = L.node_public_hex(tgt)

    def fn(lines):
        edit(next(e for e in lines if e["src"] == "public" and e["master"] == hexk))

    rewrite_jsonl(os.path.join(data, "manifests.jsonl"), fn)
    C.write_sums(data)
    ck, rows = V.verify(data)
    assert failed_ids(ck) == ["crosscheck"] and reason in ck.failed[0]["msg"]
    assert by_name(rows)["verified.example"]["public"] == state


def test_crosscheck_other_master_and_older_and_absent(data):
    names = {e["name"]: L.node_public_hex(k) for k, e in EXPECTED.items()}
    src_line = {}

    def fn(lines):
        a = next(e for e in lines if e["src"] == "node" and e["master"] == names["verified.example"])
        b = next(e for e in lines if e["src"] == "node" and e["master"] == names["badatt.example"])
        src_line["a"] = dict(a["answer"])
        a["answer"] = dict(b["answer"])  # answer for another master
        lines[:] = [
            e for e in lines if not (e["src"] == "public" and e["master"] == names["unreach.example"])
        ]

    rewrite_jsonl(os.path.join(data, "manifests.jsonl"), fn)
    C.write_sums(data)
    ck, rows = V.verify(data)
    msg = ck.failed[0]["msg"]
    assert failed_ids(ck) == ["crosscheck"]
    assert "does not verify for this master" in msg and "no answer" in msg
    r = by_name(rows)
    assert r["verified.example"]["node"] == "bad" and r["unreach.example"]["public"] == "missing"


def test_crosscheck_older_manifest():
    ck = V.Checks()
    with open(os.path.join(FIX, "vl.json")) as fh:
        d = L.decode_vl(json.load(fh))
    vals = [(k.upper(), L.parse_manifest(m)) for k, m in d["blobs"][0]["validators"]]
    k, m = next((k, m) for k, m in vals if m["domain"] == "verified.example")
    rows = [{"master": L.node_public_b58(k)}]
    V.check_crosscheck(FIX, ck, [(k, dict(m, sequence=5))], rows)
    assert failed_ids(ck) == ["crosscheck"] and "seq 3 older than the VL's 5" in ck.failed[0]["msg"]
    assert rows[0]["node"] == "older"


# ---------------------------------------------------------------- negative controls


def test_controls_all_pass_on_fixture(tmp_path, capsys):
    assert V.run_controls(FIX, str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "RESULT controls 7/7 PASS exit=0" in out
    assert out.count("\nPASS ctl_") == 7


def test_controls_refuse_failing_baseline(data, tmp_path, capsys):
    with open(os.path.join(data, "node.json"), "a") as fh:
        fh.write(" ")
    assert V.run_controls(data, str(tmp_path)) == 1
    assert "BASELINE FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------- attestation


def test_attestation_message_bytes():
    assert D.attestation_message("a.example", "nHKEY") == b"[domain-attestation-blob:a.example:nHKEY]"


def _verified_entry():
    with open(os.path.join(FIX, "toml", "verified.example", "pft-ledger.json")) as fh:
        rec = json.load(fh)
    doc = D.parse_toml(base64.b64decode(rec["body_b64"]).decode())
    e = doc["VALIDATORS"][0]
    return L.node_public_hex(e["public_key"]), e["attestation"]


def test_verify_attestation_accepts_only_exact_domain_key_and_hex():
    k, att = _verified_entry()
    assert D.verify_attestation(k, "verified.example", att)
    assert D.verify_attestation(k, "verified.example", att.lower())
    assert not D.verify_attestation(k, "Verified.example", att)
    assert not D.verify_attestation(k, "verified.example.", att)
    other = next(L.node_public_hex(x) for x, e in EXPECTED.items() if e["name"] == "badatt.example")
    assert not D.verify_attestation(other, "verified.example", att)
    for bad in ("", "0", att[:-1], att + "zz", None, 12, att[:-2]):
        assert not D.verify_attestation(k, "verified.example", bad)


def _rec(status=200, body=b"", **kw):
    r = {"outcome": "response", "status": status, "truncated": False, "body": body}
    r.update(kw)
    return r


def test_classify_fetch_branches():
    k, att = _verified_entry()
    kb = L.node_public_b58(k)
    good = f'[[VALIDATORS]]\npublic_key = "{kb}"\nattestation = "{att}"\n'.encode()
    assert D.classify_fetch(_rec(body=good), k, "verified.example")[0] == "verified"
    assert D.classify_fetch(_rec(body=good), k, "other.example")[0] == "bad_attestation"
    two = good + f'[[VALIDATORS]]\npublic_key = "{kb}"\nattestation = "00"\n'.encode()
    assert D.classify_fetch(_rec(body=two), k, "verified.example")[0] == "verified"
    noatt = f'[[VALIDATORS]]\npublic_key = "{kb}"\nattestation = ""\n'.encode()
    assert D.classify_fetch(_rec(body=noatt), k, "verified.example")[0] == "no_attestation"
    numatt = f'[[VALIDATORS]]\npublic_key = "{kb}"\nattestation = 5\n'.encode()
    assert D.classify_fetch(_rec(body=numatt), k, "verified.example")[0] == "no_attestation"
    assert D.classify_fetch(_rec(body=b'[VALIDATORS]\npublic_key = "x"\n'), k, "d")[0] == "key_not_listed"
    assert D.classify_fetch(_rec(body=b"title = 1\n"), k, "d") == (
        "key_not_listed",
        {"entries": 0, "matching": 0},
    )
    assert D.classify_fetch(_rec(body=good.lower()), k, "verified.example")[0] == "key_not_listed"
    assert D.classify_fetch(_rec(body=b"\xff\xfe"), k, "d")[0] == "toml_unparseable"
    assert D.classify_fetch(_rec(body=b"a = = 1"), k, "d")[0] == "toml_unparseable"
    assert D.classify_fetch(_rec(301), k, "d") == ("http_error", {"status": 301, "truncated": False})
    assert D.classify_fetch(_rec(200, good, truncated=True), k, "d")[0] == "http_error"
    assert D.classify_fetch({"outcome": "unreachable"}, k, "d") == ("unreachable", {})
    with pytest.raises(ValueError):
        D.classify_fetch({"outcome": "x"}, k, "d")


def test_best_rank_order():
    assert D.CLASSES[0] == "verified" and D.CLASSES[-1] == "unreachable"
    assert D.best(["unreachable", "http_error"]) == "http_error"
    assert D.best(["key_not_listed", "no_attestation"]) == "no_attestation"
    assert D.best(["bad_attestation", "verified"]) == "verified"
    assert D.best(["toml_unparseable", "http_error"]) == "toml_unparseable"


# ---------------------------------------------------------------- TOML parser

TOML_OK = [
    '[[VALIDATORS]]\npublic_key = "nHabc"\nattestation = "AB12"\n',
    '# c\n[METADATA]\nmodified = 2021-01-01T00:00:00Z\n\n[[VALIDATORS]]\npublic_key="x" # hi\nnetwork = "t"\n'
    "[[VALIDATORS]]\npublic_key = 'lit'\n[[PRINCIPALS]]\nname = \"a\"\n",
    'a.b.c = 1\na.d = [1, 2,\n 3, ]\ne = { x = 1, y.z = "q" }\n'
    "f = \"\"\"\nml\\\n  cont\"\"\"\ng = '''raw\\n'''\n"
    "h = 0x1F\ni = 1_000\nj = 3.5e2\nk = -inf\nl = true\nm = false\nn = 0o17\no = 0b101\n",
    '[server]\nport = 1\n[server.sub]\nk="v"\n[[arr]]\nx=1\n[arr.sub]\ny=2\n[[arr]]\nx=2\n',
    '"quoted key" = "\\u00e9\\t\\"q\\""\n\'lit key\' = 1\n',
    '\r\n[[VALIDATORS]]\r\npublic_key = "x"\r\n',
    'nested = [[1, 2], ["a"], []]\nempty = {}\nml = """a""b"""\n',
]
TOML_BAD = [
    "a = 1\na = 2\n",
    "[t]\n[t]\n",
    "x = \n",
    '[[V]]\npublic_key = "a\n',
    "a = [1 2]\n",
    "x = 1 y\n",
    "<html>\n",
    "[a]\nb=1\n[a.b]\n",
    "a = 1\n[[a]]\n",
    "a = [1]\n[[a]]\n",
    'k = "\\q"\n',
    'k = "\\uD800"\n',
    "k = 01\n",
    "e = {x = 1}\n[e]\n",
    '"""k""" = 1\n',
    "k = 'a\nb'\n",
    "[a\n",
    "[[a]\n",
    'k = """open\n',
    "k = '''open\n",
]


def _norm(o):
    import datetime as dt

    if isinstance(o, dict):
        return {k: _norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_norm(v) for v in o]
    if isinstance(o, (dt.datetime, dt.date, dt.time)):
        return "<dt>"
    if isinstance(o, str) and D._DATE.fullmatch(o):
        return "<dt>"
    return o


@pytest.mark.parametrize("i", range(len(TOML_OK)), ids=[f"ok{i}" for i in range(len(TOML_OK))])
def test_toml_parses_like_tomli(i):
    got = D.parse_toml(TOML_OK[i])
    tomli = pytest.importorskip("tomli")
    assert _norm(got) == _norm(tomli.loads(TOML_OK[i]))


def test_toml_values_without_tomli():
    d = D.parse_toml(TOML_OK[2])
    assert d["a"] == {"b": {"c": 1}, "d": [1, 2, 3]}
    assert d["e"] == {"x": 1, "y": {"z": "q"}}
    assert d["f"] == "mlcont" and d["g"] == "raw\\n"
    assert (d["h"], d["i"], d["j"], d["k"], d["n"], d["o"]) == (31, 1000, 350.0, float("-inf"), 15, 5)
    assert d["l"] is True and d["m"] is False
    assert D.parse_toml(TOML_OK[3])["arr"] == [{"x": 1, "sub": {"y": 2}}, {"x": 2}]
    assert D.parse_toml(TOML_OK[4]) == {"quoted key": 'é\t"q"', "lit key": 1}


@pytest.mark.parametrize("i", range(len(TOML_BAD)), ids=[f"bad{i}" for i in range(len(TOML_BAD))])
def test_toml_rejects_invalid(i):
    with pytest.raises(D.TomlError):
        D.parse_toml(TOML_BAD[i])
    tomli = pytest.importorskip("tomli")
    with pytest.raises(tomli.TOMLDecodeError):
        tomli.loads(TOML_BAD[i])


def test_toml_not_text():
    with pytest.raises(D.TomlError):
        D.parse_toml(b"a = 1")


# ---------------------------------------------------------------- collector (stubbed, no network)


class FakeResp(io.BytesIO):
    def __init__(self, body, status=200, url=None, ctype="text/plain"):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, timeout))
        return self.fn(req)


def builder(fn, sink):
    def build(*handlers):
        o = FakeOpener(fn)
        sink.append(o)
        return o

    return build


def ok_checker(host):
    return True, ""


def test_fetch_toml_200_and_cap():
    built = []
    rec, why = C.fetch_toml(
        "a.example", PFT, 5, 10, ok_checker, builder(lambda r: FakeResp(b"0123456789", url=r.full_url), built)
    )
    assert why is None and rec["outcome"] == "response" and rec["status"] == 200 and not rec["truncated"]
    assert base64.b64decode(rec["body_b64"]) == b"0123456789" and rec["bytes"] == 10
    assert rec["sha256"] == hashlib.sha256(b"0123456789").hexdigest() and rec["final_url"] is None
    assert built[0].calls == [("https://a.example/.well-known/pft-ledger.toml", 5)]
    rec, _ = C.fetch_toml("a.example", PFT, 5, 9, ok_checker, builder(lambda r: FakeResp(b"0123456789"), []))
    assert rec["truncated"] and rec["bytes"] == 9 and "body_b64" not in rec


def test_fetch_toml_http_error_keeps_no_body():
    def fn(r):
        raise urllib.error.HTTPError(
            r.full_url, 404, "nf", {"Content-Type": "text/html"}, io.BytesIO(b"<h1>")
        )

    rec, why = C.fetch_toml("a.example", XRP, 5, 100, ok_checker, builder(fn, []))
    assert why is None and rec["status"] == 404 and rec["bytes"] == 4 and "body_b64" not in rec


def test_fetch_toml_unreachable_reason_is_private():
    def fn(r):
        raise urllib.error.URLError("certificate verify failed: some detail")

    rec, why = C.fetch_toml("a.example", PFT, 5, 100, ok_checker, builder(fn, []))
    assert rec["outcome"] == "unreachable" and "certificate" in why
    assert "certificate" not in json.dumps(rec) and set(rec) == {
        "domain",
        "path",
        "url",
        "t",
        "outcome",
        "redirects",
    }


def test_fetch_toml_non_public_host_never_contacted():
    built = []
    rec, why = C.fetch_toml(
        "a.example", PFT, 5, 100, lambda h: (False, "resolves to a non-public address"), builder(None, built)
    )
    assert rec["outcome"] == "unreachable" and why == "resolves to a non-public address" and built == []


def test_public_host_rules():
    def res(*addrs):
        return lambda h, p, type=None: [(None, None, None, None, (a, 443)) for a in addrs]

    assert C.public_host("x", res("93.184.216.34")) == (True, "")
    for a in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1", "::1", "fd00::1", "100.64.0.1"):
        assert C.public_host("x", res("93.184.216.34", a))[0] is False, a
    assert C.public_host("x", res())[0] is False

    def boom(*a, **k):
        raise socket.gaierror("no such host")

    ok, why = C.public_host("x", boom)
    assert not ok and why.startswith("dns:")


def test_redirect_rules():
    r = C._Redirects(ok_checker)
    req = urllib.request.Request("https://a.example/x")
    for i in range(C.MAX_REDIRECTS):
        r.redirect_request(req, None, 301, "m", {}, f"https://a.example/{i}")
    with pytest.raises(urllib.error.URLError, match="too many"):
        r.redirect_request(req, None, 301, "m", {}, "https://a.example/z")
    with pytest.raises(urllib.error.URLError, match="non-https"):
        C._Redirects(ok_checker).redirect_request(req, None, 301, "m", {}, "http://a.example/")
    bad = C._Redirects(lambda h: (False, "non-public"))
    with pytest.raises(urllib.error.URLError, match="redirect target"):
        bad.redirect_request(req, None, 302, "m", {}, "https://internal.example/")


def test_valid_domain():
    for d in ("a.example", "pft.x-y.io", "x1.github.io"):
        assert C.valid_domain(d)
    for d in (
        None,
        "",
        "localhost",
        "1.2.3.4",
        "a/b.example",
        "../x.example",
        "a.example:80",
        "A.example",
        "a..b",
        "-a.io",
    ):
        assert not C.valid_domain(d), d


def args(out, **kw):
    from types import SimpleNamespace

    base = dict(out=out, vl_url=None, node_rpc="n", public_rpc="p", timeout=1.0, max_bytes=1000, private=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_step_toml_one_get_per_path_resume_and_private(tmp_path):
    calls = []

    def fetch(dom, p, timeout, cap):
        calls.append((dom, p))
        if dom == "down.example":
            return {"domain": dom, "path": p, "url": "u", "t": 1, "outcome": "unreachable"}, "TLS detail"
        return {
            "domain": dom,
            "path": p,
            "url": "u",
            "t": 1,
            "outcome": "response",
            "status": 404,
            "bytes": 0,
        }, None

    priv = tmp_path / "private.jsonl"
    a = args(str(tmp_path / "d"), private=str(priv))
    C.step_toml(a, {"a.example", "down.example", "localhost"}, fetch)
    assert sorted(calls) == sorted((d, p) for d in ("a.example", "down.example") for p in D.TOML_PATHS)
    C.step_toml(a, {"a.example", "down.example"}, fetch)
    assert len(calls) == 4  # resumed: nothing re-fetched
    lines = [json.loads(x) for x in priv.read_text().splitlines()]
    assert {(x["domain"], x["reason"]) for x in lines} == {("down.example", "TLS detail")} and len(lines) == 2
    for p in (tmp_path / "d" / "toml").rglob("*.json"):
        assert "TLS detail" not in p.read_text()


def test_step_manifests_resume_and_retry(tmp_path):
    seen = []

    def call(url, method, params):
        seen.append((url, params["public_key"]))
        if url == "p" and len([s for s in seen if s[0] == "p"]) == 1:
            raise OSError("flaky")
        return {
            "details": {"domain": "d"},
            "manifest": "m",
            "requested": params["public_key"],
            "status": "ok",
        }

    ks = ["ED" + "01" * 32, "ED" + "02" * 32]
    a = args(str(tmp_path))
    C.step_manifests(a, ks, call, sleep=lambda s: None)
    lines = C.load_jsonl(str(tmp_path / "manifests.jsonl"))
    assert len(lines) == 4 and all("answer" in e for e in lines) and len(seen) == 5
    C.step_manifests(a, ks, call, sleep=lambda s: None)
    assert len(seen) == 5


def test_step_manifests_records_error_after_3_attempts(tmp_path):
    def call(url, method, params):
        raise OSError("down")

    C.step_manifests(args(str(tmp_path)), ["ED" + "01" * 32], call, sleep=lambda s: None)
    lines = C.load_jsonl(str(tmp_path / "manifests.jsonl"))
    assert [e["error"] for e in lines] == ["OSError: down", "OSError: down"]


def test_load_jsonl_drops_torn_tail(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_bytes(b'{"a": 1}\n{"b": ')
    assert C.load_jsonl(str(p)) == [{"a": 1}]


def test_step_vl_uses_node_site_and_caches(tmp_path):
    got = []

    def opener(req, timeout=None):
        got.append(req.full_url)
        return FakeResp(b'{"x": 1}')

    node = {"validator_list_sites": {"validator_sites": [{"uri": "https://vl.example/l.json"}]}}
    a = args(str(tmp_path))
    C.step_vl(a, node, opener)
    C.step_vl(a, node, opener)
    assert got == ["https://vl.example/l.json"]
    with open(tmp_path / "vl-fetch.json") as fh:
        rec = json.load(fh)
    assert rec["sha256"] == hashlib.sha256(b'{"x": 1}').hexdigest() and rec["status"] == 200
    assert C.vl_site({"validator_list_sites": {"validator_sites": [{"uri": "a"}, {"uri": "b"}]}}) is None
    with pytest.raises(SystemExit):
        C.step_vl(args(str(tmp_path / "n")), {}, opener)


def test_cmd_node_reads_admin_commands(tmp_path):
    asked = []

    def call(url, m):
        asked.append(m)
        if m == "server_info":
            return {"info": {"build_version": "1.0.9", "server_state": "full", "pubkey_node": "secret-ish"}}
        if m == "validators":
            return {"publisher_lists": [{"pubkey_publisher": "ED", "seq": 1, "list": ["a"]}]}
        return {"validator_sites": []}

    assert C.cmd_node(args(str(tmp_path), rpc="r"), call) == 0
    assert asked == ["validators", "validator_list_sites", "server_info"]
    node = json.loads((tmp_path / "node.json").read_text())
    assert node["server_info"]["build_version"] == "1.0.9" and "pubkey_node" not in json.dumps(node)


def test_cmd_collect_needs_node_json(tmp_path):
    assert C.cmd_collect(args(str(tmp_path))) == 2


def test_write_sums_round_trip(data):
    ck = V.Checks()
    V.check_sums(data, ck)
    assert not ck.failed and ck.rows[0]["files"] == 26


def test_offline_guard_blocks_network(monkeypatch):
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, getattr(socket, name))
    V.offline_guard()
    with pytest.raises(OSError, match="network disabled"):
        socket.create_connection(("example.com", 443))
    with pytest.raises(OSError, match="network disabled"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(OSError, match="network disabled"):
        socket.socket()


def test_main_requires_offline(capsys):
    with pytest.raises(SystemExit):
        V.main([FIX])


# ---------------------------------------------------------------- the committed real collection


@pytest.mark.skipif(not os.path.isdir(REAL), reason="real collection not committed yet")
def test_real_collection_matches_committed_results(tmp_path):
    ck, rows = V.verify(REAL)
    assert ck.failed == []
    assert all(r["class"] in D.CLASSES + (D.NO_DOMAIN,) for r in rows)
    js, cs, md = V.outputs(ck, rows)
    rep = os.path.join(ROOT, "valdomain", "report")
    with open(os.path.join(rep, "results.json"), "rb") as fh:
        assert fh.read() == js
    with open(os.path.join(rep, "table.md"), "rb") as fh:
        assert fh.read() == md
