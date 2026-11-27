"""Tests for endpoints/: hand-built rounds, negative controls, and the committed real pack."""

import copy
import gzip
import hashlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "endpoints"))

import collect_endpoints as C  # noqa: E402
import lib_endpoints as L  # noqa: E402
import verify_endpoints as V  # noqa: E402

API = "https://scoring.example"
EPOCH = "2026-01-01"
DOC_IP = ["192.0.2.10", "198.51.100.7", "203.0.113.5", "2001:db8::1"]


# ---------- hand-built rounds ----------


def val(key, as_name, country, ip, domain=None, verified=False):
    return {
        "master_key": key,
        "signing_key": "n9" + key,
        "ip": ip,
        "asn": {"asn": 64500, "as_name": as_name} if as_name else None,
        "geolocation": {"country": country} if country else None,
        "domain": domain,
        "domain_verified": verified,
        "server_version": "1.0.9",
    }


ROUND3 = [  # round with block: 2 hetzner (one via cloud ASN name), 1 vultr via "Constant", 2 unresolved
    val("nHA1", "HETZNER-AS, DE", "Germany", DOC_IP[0], "a.example", True),
    val("nHA2", "HETZNER-CLOUD2-AS - Hetzner Online GmbH, DE", "Finland", DOC_IP[1]),
    val("nHA3", "AS-CHOOPA - The Constant Company, LLC, US", "United States", DOC_IP[2], "c.example", True),
    val("nHA4", None, None, DOC_IP[3]),
    val("nHA5", None, None, None),
]
SCORES3 = {"nHA1": 30, "nHA2": 35, "nHA3": 60, "nHA4": 20, "nHA5": 10}


def prompt_for(vals, with_block=True, block=None):
    rows = []
    for i, v in enumerate(sorted(vals, key=lambda x: x["master_key"]), start=1):
        e = {
            "validator_id": f"v{i:03d}",
            "domain": v["domain"],
            "asn": v["asn"],
            "geolocation": v["geolocation"],
        }
        if with_block:
            e["provider_family"] = L.family_for((v["asn"] or {}).get("as_name"))
        rows.append(e)
    text = "Score these.\n\n"
    if with_block:
        blk = block if block is not None else L.concentration(norm(vals))
        text += L.BLOCK_MARK + json.dumps(blk, separators=(",", ":")) + "\n\n"
    text += "VALIDATOR DATA:\n" + json.dumps(rows, separators=(",", ":")) + "\n\nReturn JSON."
    return {"model": "m", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": text}]}


def norm(vals):
    return [
        {"as_name": (v["asn"] or {}).get("as_name"), "country": (v["geolocation"] or {}).get("country")}
        for v in vals
    ]


def diversity_inputs(vals):
    c = L.concentration(norm(vals))
    fam, cty = L.as_table(c["provider_families"], "family"), L.as_table(c["countries"], "country")
    out = []
    for v in sorted(vals, key=lambda x: x["master_key"]):
        f = L.family_for((v["asn"] or {}).get("as_name"))
        ct = (v["geolocation"] or {}).get("country")
        out.append(
            {
                "master_key": v["master_key"],
                "country_validators": cty[ct] if ct else None,
                "provider_validators": fam[f] if f != L.UNKNOWN else None,
            }
        )
    return {"resolved_endpoints": len(vals) - c["unresolved_endpoints"], "validators": out}


def round_docs(n, vals, scores, with_block=True, block=None, version="v12"):
    docs = {
        "inputs/validator_evidence.json": {"round_number": n, "validators": vals},
        "inputs/model_request.json": prompt_for(vals, with_block, block),
        "outputs/validator_scores.json": {
            "validator_scores": [{"master_key": k, "diversity": d, "score": 50} for k, d in scores.items()]
        },
        "runtime/execution_manifest.json": {"code": {"commit": "c" * 40, "prompt": {"version": version}}},
    }
    if with_block:
        docs["inputs/diversity_inputs.json"] = diversity_inputs(vals)
    return docs


def build_world():
    """url -> (status, bytes) for a 4-round history: 1 FAILED, 2 legacy (no bundle), 3 + 4 with inputs."""
    r2 = [val("nHA1", "HETZNER-AS, DE", "Germany", DOC_IP[0]), val("nHA4", "OVH, FR", "France", DOC_IP[3])]
    s2 = {"nHA1": 40, "nHA4": 50}
    r4 = copy.deepcopy(ROUND3)
    r4[3] = val("nHA4", "OVH SAS, FR", "France", DOC_IP[3])  # nHA4 resolves again
    s4 = dict(SCORES3, nHA4=70)
    rounds = {
        1: ("FAILED", None),
        2: ("COMPLETE", round_docs(2, r2, s2, with_block=False, version="v8")),
        3: ("COMPLETE", round_docs(3, ROUND3, SCORES3)),
        4: ("COMPLETE", round_docs(4, r4, s4)),
        5: ("COMPLETE", None),  # legacy layout: no bundle
    }
    world = {}
    listing = [
        {"id": n, "round_number": n, "status": st, "vl_sequence": n - 1, "completed_at": f"t{n}"}
        for n, (st, _) in rounds.items()
    ]
    world[f"{API}/api/scoring/rounds?limit=100&offset=0"] = (200, {"rounds": listing[:3], "total": 5})
    world[f"{API}/api/scoring/rounds?limit=100&offset=3"] = (200, {"rounds": listing[3:], "total": 5})
    for n, (_st, docs) in rounds.items():
        if docs is None:
            world[f"{API}/api/scoring/rounds/{n}/bundle.json"] = (404, {"detail": "not found"})
            continue
        world[f"{API}/api/scoring/rounds/{n}/bundle.json"] = (
            200,
            {
                "file_hashes": {f: L.content_hash(d) for f, d in docs.items()}
                | {"outputs/other.json": "0" * 64}
            },
        )
        for f, d in docs.items():
            world[f"{API}/api/scoring/rounds/{n}/{f}"] = (200, d)
    return {u: (s, json.dumps(b, indent=2).encode()) for u, (s, b) in world.items()}


class FakeNet:
    def __init__(self, world, fail_first=()):
        self.world, self.calls, self.fail = world, [], dict.fromkeys(fail_first, 1)

    def __call__(self, url):
        self.calls.append(url)
        if self.fail.get(url):
            self.fail[url] -= 1
            return 503, b"busy"
        return self.world.get(url, (404, b"{}"))


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 100.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def run_collect(tmp_path, net, clock=None):
    clock = clock or FakeClock()
    cache = C.Cache(str(tmp_path / "cache.jsonl"))
    f = C.Fetcher(cache, gap=1.0, opener=net, sleep=clock.sleep, clock=clock.now)
    args = type("A", (), {"api": API, "epoch": EPOCH})
    rc = C.collect(args, f)
    cache.fh.close()
    return rc, f


@pytest.fixture
def world_pack(tmp_path):
    net = FakeNet(build_world())
    rc, _ = run_collect(tmp_path, net)
    assert rc == 0
    out = tmp_path / "pack.jsonl.gz"
    assert C.pack(str(tmp_path / "cache.jsonl"), str(out), EPOCH) == 0
    return out


def read_pack(p):
    return [json.loads(x) for x in gzip.decompress(p.read_bytes()).decode().splitlines()]


def write_pack(p, rows):
    text = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows)
    p.write_bytes(gzip.compress(text.encode(), mtime=0))


def verify(tmp_path, pack, name="out", valdomain=None):
    out = tmp_path / name
    rc = V.main(
        ["--offline", "--pack", str(pack), "--out", str(out), "--valdomain", str(valdomain or "/nonexistent")]
    )
    res = json.loads((out / "results.json").read_text()) if (out / "results.json").exists() else None
    return rc, res, out


def by_round(res, n):
    return next(x for x in res["rounds"] if x["round"] == n)


# ---------- library ----------


@pytest.mark.parametrize(
    "name,family",
    [
        ("CONTABO-40021 - Contabo Inc., US", "contabo"),
        ("CONTABO, DE", "contabo"),
        ("CHERRYSERVERS4-AS, LT", "cherryservers"),
        ("AS-CHOOPA - The Constant Company, LLC, US", "vultr"),
        ("HETZNER-CLOUD2-AS - Hetzner Online GmbH, DE", "hetzner"),
        ("LINODE-AP Linode, LLC, US", "akamai"),
        ("OVH SAS, FR", "ovh sas"),
        ("AS12876", "as12876"),
        ("Constantine Telecom, DZ", "constantine telecom"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_family_for(name, family):
    assert L.family_for(name) == family


def test_concentration_order_and_unresolved():
    c = L.concentration(norm(ROUND3))
    assert c["provider_families"] == [
        {"family": "hetzner", "validators": 2},
        {"family": "vultr", "validators": 1},
    ]
    assert c["countries"] == [
        {"country": "Finland", "validators": 1},
        {"country": "Germany", "validators": 1},
        {"country": "United States", "validators": 1},
    ]
    assert c["unresolved_endpoints"] == 2


def test_hhi():
    assert L.hhi({"a": 1}) == 10000
    assert L.hhi({"a": 1, "b": 1}) == 5000
    assert L.hhi({"a": 3, "b": 1}) == 6250
    assert L.hhi({}) is None


def test_find_ips_hits_and_non_hits():
    assert L.find_ips("x 192.0.2.10 y 2001:db8::1 z") == ["192.0.2.10", "2001:db8::1"]
    assert L.find_ips('"server_version":"1.0.9" t=16:11:18 2026-11-24T16:11:18.56+00:00 999.1.1.1') == []


def test_content_hash_is_canonical():
    assert L.content_hash({"b": 1, "a": [1, 2]}) == hashlib.sha256(b'{"a":[1,2],"b":1}').hexdigest()


def test_extract_block_and_prompt_rows():
    mr = prompt_for(ROUND3)
    assert L.extract_block(mr) == L.concentration(norm(ROUND3))
    rows = L.prompt_entries(mr)
    assert [r["provider_family"] for r in rows] == ["hetzner", "hetzner", "vultr", "unknown", "unknown"]
    assert L.extract_block(prompt_for(ROUND3, with_block=False)) is None


# ---------- collector ----------


def test_collect_fetches_each_document_once_and_paces(tmp_path):
    net, clock = FakeNet(build_world()), FakeClock()
    rc, f = run_collect(tmp_path, net, clock)
    assert rc == 0
    assert len(net.calls) == len(set(net.calls))
    assert all(s <= 1.0 + 1e-9 for s in clock.sleeps)
    assert len(clock.sleeps) == len(net.calls) - 1  # every request after the first waits for the gap
    assert f"{API}/api/scoring/rounds/1/bundle.json" not in net.calls  # FAILED round skipped
    assert not any("outputs/other.json" in u for u in net.calls)  # only the needed files
    rc, f2 = run_collect(tmp_path, FakeNet(build_world()))
    assert rc == 0 and f2.calls == 0 and f2.cached == f.calls  # resume: nothing refetched


def test_collect_retries_5xx(tmp_path):
    url = f"{API}/api/scoring/rounds/3/inputs/model_request.json"
    net = FakeNet(build_world(), fail_first=[url])
    rc, f = run_collect(tmp_path, net)
    assert rc == 0 and f.retried == 1 and net.calls.count(url) == 2


def test_collect_cli_rejects_fast_gap(tmp_path):
    with pytest.raises(SystemExit, match="one request per second"):
        C.main(["collect", "--out", str(tmp_path), "--gap", "0.5"])


def test_torn_cache_line_is_cut(tmp_path):
    p = tmp_path / "cache.jsonl"
    p.write_text('{"key":"a","status":200,"body":"{}"}\n{"key":"b","sta')
    c = C.Cache(str(p))
    assert list(c.rows) == ["a"] and c.torn > 0


def test_pack_strips_addresses(world_pack, tmp_path):
    raw = (tmp_path / "cache.jsonl").read_text()
    assert all(ip in raw for ip in DOC_IP)  # the raw cache does hold them
    text = gzip.decompress(world_pack.read_bytes()).decode()
    assert L.find_ips(text) == []
    r3 = [r for r in read_pack(world_pack) if r.get("round") == 3][0]
    assert [v["address_seen"] for v in r3["validators"]] == [True, True, True, True, False]
    assert gzip.decompress(world_pack.read_bytes()) and world_pack.read_bytes()[4:8] == b"\0\0\0\0"  # mtime 0


def test_pack_refuses_leftover_ip(tmp_path):
    world = build_world()
    url = f"{API}/api/scoring/rounds/3/inputs/validator_evidence.json"
    doc = json.loads(world[url][1])
    doc["validators"][0]["asn"]["as_name"] = "EDGE 192.0.2.99, US"
    world[url] = (200, json.dumps(doc).encode())
    bundle_url = f"{API}/api/scoring/rounds/3/bundle.json"
    b = json.loads(world[bundle_url][1])
    b["file_hashes"]["inputs/validator_evidence.json"] = L.content_hash(doc)
    world[bundle_url] = (200, json.dumps(b).encode())
    run_collect(tmp_path, FakeNet(world))
    with pytest.raises(SystemExit, match="IP address"):
        C.pack(str(tmp_path / "cache.jsonl"), str(tmp_path / "p.gz"), EPOCH)


def test_pack_is_deterministic(world_pack, tmp_path):
    again = tmp_path / "again.gz"
    C.pack(str(tmp_path / "cache.jsonl"), str(again), EPOCH)
    assert again.read_bytes() == world_pack.read_bytes()


# ---------- verifier: positive ----------


def test_verify_world(world_pack, tmp_path):
    rc, res, out = verify(tmp_path, world_pack)
    assert rc == 0
    assert [x["round"] for x in res["rounds"]] == [2, 3, 4, 5]
    assert [by_round(res, n)["block"]["class"] for n in (2, 3, 4, 5)] == [
        "block_missing",
        "match",
        "match",
        "block_missing",
    ]
    assert by_round(res, 2)["block"]["why"] == "prompt has no block"
    assert by_round(res, 5)["block"]["why"] == "no frozen inputs (no validator evidence)"
    assert all(not x["files"]["bad"] for x in res["rounds"])
    assert by_round(res, 3)["prompt"]["disagree"] == 0
    assert by_round(res, 3)["diversity_inputs"]["disagree"] == []
    v3 = by_round(res, 3)["visibility"]
    assert (v3["resolved"], v3["unresolved"], v3["unresolved_address_seen"]) == (3, 2, 1)
    assert v3["became_unresolved"] == ["nHA4"] and v3["new_unresolved"] == ["nHA5"]
    assert v3["left_set"] == []
    assert by_round(res, 4)["visibility"]["became_resolved"] == ["nHA4"]
    sums = (out / "SHA256SUMS").read_text().splitlines()
    assert [s.split()[1] for s in sums] == ["REPORT.md", "results.json", "rounds.csv"]


def test_rank_test_holds_and_fails(world_pack, tmp_path):
    rc, res, _ = verify(tmp_path, world_pack)
    k3 = by_round(res, 3)["rank"]
    assert (k3["unresolved_max"], k3["top_family"], k3["top_family_min"], k3["resolved_min"]) == (
        20,
        "hetzner",
        30,
        30,
    )
    assert k3["below_top_family"] and k3["below_all_resolved"]
    k4 = by_round(res, 4)["rank"]  # nHA5 is the only unresolved (10): holds
    assert k4["below_top_family"] and k4["unresolved"] == 1
    k2 = by_round(res, 2)["rank"]
    assert k2 == {"applicable": False, "unresolved": 0, "resolved": 2}


def test_rank_test_tie_is_not_below(world_pack, tmp_path):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    for v in r3["validators"]:
        if v["master_key"] == "nHA5":
            v["diversity"] = 30
    write_pack(world_pack, rows)
    _, res, _ = verify(tmp_path, world_pack)
    k = by_round(res, 3)["rank"]
    assert not k["below_top_family"] and not k["below_all_resolved"]
    assert k["unresolved_at_or_above_top_min"] == ["nHA5"]


def test_verify_is_byte_identical(world_pack, tmp_path):
    verify(tmp_path, world_pack, "a")
    verify(tmp_path, world_pack, "b")
    for f in ("REPORT.md", "results.json", "rounds.csv", "SHA256SUMS"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()


def test_valdomain_crosscheck(world_pack, tmp_path):
    rows = read_pack(world_pack)
    for r in rows:
        if r.get("round") == 3:
            r["round"] = 33
    rows[0]["complete_rounds"] = sorted(r["round"] for r in rows[1:])
    write_pack(world_pack, rows)
    vd = tmp_path / "vd.json"
    vd.write_text(
        json.dumps(
            {
                "validators": [
                    {"master": "nHA1", "domain": "a.example", "class": "verified"},
                    {"master": "nHA3", "domain": "c.example", "class": "bad_attestation"},
                    {"master": "nHA2", "domain": None, "class": "no_domain"},
                    {"master": "nHZZ", "domain": "z.example", "class": "verified"},
                ]
            }
        )
    )
    rc, res, _ = verify(tmp_path, world_pack, valdomain=vd)
    cc = res["valdomain_crosscheck"]
    assert rc == 0 and cc["round"] == 33
    assert cc["counts"] == {"agree": 2, "disagree": 1, "not_in_round": 1}
    assert [r["result"] for r in cc["rows"]] == ["agree", "agree", "disagree", "not_in_round"]


# ---------- verifier: negative controls ----------


def test_tampered_block_is_mismatch(world_pack, tmp_path):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    r3["block"]["provider_families"][0]["validators"] = 3
    r3["block"]["unresolved_endpoints"] = 1
    write_pack(world_pack, rows)
    rc, res, out = verify(tmp_path, world_pack)
    b = by_round(res, 3)["block"]
    assert rc == 0 and b["class"] == "mismatch"
    assert b["families"] == [["hetzner", 2, 3]] and b["unresolved"] == [2, 1]
    assert "round 3: families [['hetzner', 2, 3]]" in (out / "REPORT.md").read_text()


def test_reordered_block_is_mismatch(world_pack, tmp_path):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    r3["block"]["countries"].reverse()
    write_pack(world_pack, rows)
    _, res, _ = verify(tmp_path, world_pack)
    assert by_round(res, 3)["block"] == {"class": "mismatch", "why": "same counts, different order"}


def test_block_without_evidence_is_mismatch(world_pack, tmp_path):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    r3["validators"] = None
    write_pack(world_pack, rows)
    _, res, _ = verify(tmp_path, world_pack)
    assert by_round(res, 3)["block"]["class"] == "mismatch"


def test_hash_mismatch_and_http_error_reported(world_pack, tmp_path):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    r3["files"]["inputs/model_request.json"]["published_sha256"] = "f" * 64
    r3["files"]["outputs/validator_scores.json"]["http"] = 500
    write_pack(world_pack, rows)
    rc, res, _ = verify(tmp_path, world_pack)
    assert rc == 0
    assert by_round(res, 3)["files"]["bad"] == [
        "inputs/model_request.json: content hash != published",
        "outputs/validator_scores.json: http 500",
    ]


def test_prompt_and_diversity_inputs_disagreement(world_pack, tmp_path):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    r3["prompt"][0]["provider_family"] = "contabo"
    r3["prompt"][2]["country"] = "Canada"
    r3["diversity_inputs"]["validators"][1]["provider_validators"] = 9
    r3["diversity_inputs"]["resolved_endpoints"] = 5
    write_pack(world_pack, rows)
    _, res, _ = verify(tmp_path, world_pack)
    assert by_round(res, 3)["prompt"]["disagree"] == 2
    d = by_round(res, 3)["diversity_inputs"]
    assert d["disagree"] == ["nHA2"] and d["resolved_endpoints_ok"] is False


def test_ip_in_pack_is_internal_inconsistency(world_pack, tmp_path):
    rows = read_pack(world_pack)
    rows[2]["validators"][0]["domain"] = "203.0.113.9"
    write_pack(world_pack, rows)
    rc, res, _ = verify(tmp_path, world_pack)
    assert rc == 1 and res is None


@pytest.mark.parametrize(
    "breakage", ["dup", "missing_round", "endpoint_flag", "unsorted", "bad_hash", "no_meta"]
)
def test_internal_inconsistency_exit_1(world_pack, tmp_path, breakage):
    rows = read_pack(world_pack)
    r3 = [r for r in rows if r.get("round") == 3][0]
    if breakage == "dup":
        rows.append(copy.deepcopy(r3))
    elif breakage == "missing_round":
        rows = [r for r in rows if r.get("round") != 4]
    elif breakage == "endpoint_flag":
        r3["validators"][3]["endpoint"] = "resolved"
    elif breakage == "unsorted":
        r3["validators"].reverse()
    elif breakage == "bad_hash":
        r3["files"]["inputs/model_request.json"]["content_sha256"] = "xyz"
    elif breakage == "no_meta":
        rows = rows[1:]
    write_pack(world_pack, rows)
    rc, _, _ = verify(tmp_path, world_pack)
    assert rc == 1


def test_no_data_exit_2(tmp_path):
    rc, _, _ = verify(tmp_path, tmp_path / "absent.gz")
    assert rc == 2
    empty = tmp_path / "empty.gz"
    write_pack(empty, [{"kind": "meta", "complete_rounds": []}])
    rc, _, _ = verify(tmp_path, empty)
    assert rc == 2


# ---------- the committed real pack ----------

REAL = os.path.join(ROOT, "endpoints", "data", "pack.jsonl.gz")
REPORT = os.path.join(ROOT, "endpoints", "report")


@pytest.mark.skipif(not os.path.exists(REAL), reason="no committed pack in this tree")
def test_real_pack_reproduces_committed_report(tmp_path):
    rc, res, out = verify(tmp_path, REAL, valdomain=os.path.join(ROOT, "valdomain", "report", "results.json"))
    assert rc == 0
    rows, text = V.load_pack(REAL)
    assert L.find_ips(text) == []
    assert [x["round"] for x in res["rounds"]] == rows[0]["complete_rounds"]
    for f in ("REPORT.md", "results.json", "rounds.csv", "SHA256SUMS"):
        assert (out / f).read_bytes() == open(os.path.join(REPORT, f), "rb").read(), f
