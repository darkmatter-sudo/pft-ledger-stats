#!/usr/bin/env python3
"""Verify a valsig recording: which trusted UNL masters signed each validated ledger, and did each
ledger meet quorum (Python 3 stdlib only; read-only).

  verify_valsig.py run DATA --out DIR [--rpc URL | --offline] [--jobs N] [--sample 60]
  verify_valsig.py controls DATA [--jobs N]

`run` checks, in order (every check is one entry in DIR/checks.json with a status and a reason):
  recording.*    closed gzip segments only, contiguous numbering, no unparsable line, no gap event,
                 consecutive ledger indexes, the window ran its full length (>= --min-hours)
  vl.*           the published signed VL at start and end: publisher manifest (master + signing
                 signature), blob signature, low-S, validity window covers the recording, and it is
                 the list the node trusted (publisher key, sequence, validator set)
  manifests.*    every recorded manifest verifies (master + signing signature); every trusted master
                 has one; the node's manifest is not older than the VL's
  trust.*        every `validators` snapshot: trusted keys == VL validators, signing keys == the keys
                 the verified manifests give
  validations.*  every recorded STValidation decodes, the node's decoded fields equal the signed
                 blob's, the signature verifies (low-S when the fully-canonical flag is set), and the
                 signing key maps to a master through a verified manifest
  quorum.*       the node's quorum equals postfiatd's formula (lib_valsig.expected_quorum) for the
                 trusted set and NegativeUNL; each observed validated ledger has >= quorum distinct
                 trusted, non-NegativeUNL masters with a full, valid validation of that exact hash
  rpc.crosscheck --sample ledgers spread over the window: the public RPC's validated hash equals the
                 recorded one. Answers are cached in DATA/rpc-cache.json; --offline uses only the cache,
                 so a fresh clone rebuilds checks.json byte-identically without network.
A ledger is "observed" when the recorder was connected for the whole time its validations could
arrive: the first ledger of each connection is an "edge" ledger, reported but not graded.

`controls` re-runs the checks on in-memory mutated copies of the recording and exits 0 only if each
of the five negative controls fails as designed (see CONTROLS).
"""

import argparse
import csv
import glob
import gzip
import hashlib
import io
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
sys.path.insert(0, os.path.join(here, "..", "dunl"))
import lib_dunl as L  # noqa: E402
import lib_valsig as V  # noqa: E402

RIPPLE_EPOCH = 946684800
RPC = "https://rpc.testnet.postfiat.org/"


def iso(unix):
    return datetime.fromtimestamp(unix, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def b58(hex_key):
    return L.node_public_b58(hex_key)


def hexkey(b58_key):
    try:
        return L.node_public_hex(b58_key)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- loading


class Recording:
    """Every event of a recording directory, in file order, plus the file inventory."""

    def __init__(self, data):
        self.data = data
        self.files = sorted(glob.glob(os.path.join(data, "seg", "[0-9]" * 6 + ".jsonl.gz")))
        self.open_segment = os.path.exists(os.path.join(data, "seg", "current.jsonl"))
        self.events = []
        self.bad_lines = 0
        for p in self.files:
            with gzip.open(p, "rt") as fh:
                for line in fh:
                    try:
                        self.events.append(json.loads(line))
                    except ValueError:
                        self.bad_lines += 1
        with open(os.path.join(data, "state.json")) as fh:
            self.window = json.load(fh)
        self.vl = {}
        for tag in ("start", "end"):
            p = os.path.join(data, f"vl-{tag}.json")
            self.vl[tag] = json.load(open(p)) if os.path.exists(p) else None
        p = os.path.join(data, "vl-fetch.jsonl")
        self.vl_fetch = [json.loads(x) for x in open(p)] if os.path.exists(p) else []

    def of(self, ev):
        return [e for e in self.events if e.get("ev") == ev]

    def inputs(self):
        names = [os.path.join("seg", os.path.basename(p)) for p in self.files]
        names += [
            n
            for n in ("state.json", "vl-start.json", "vl-end.json", "vl-fetch.jsonl", "rpc-cache.json")
            if os.path.exists(os.path.join(self.data, n))
        ]
        return {n: sha256_file(os.path.join(self.data, n)) for n in names}


# ---------------------------------------------------------------- signature work (parallel, cached)


def _sig_job(d):
    try:
        dv = V.decode_validation(d)
    except ValueError as e:
        return d, {"decode": f"decode_error: {e}"}
    ok, reason = V.check_signature(dv)
    dv = dict(dv)
    dv.pop("signed")
    dv["signature"] = dv["signature"].hex().upper()
    dv["sig_ok"], dv["sig_reason"] = ok, reason
    return d, dv


class SigCache:
    def __init__(self, jobs):
        self.jobs = jobs
        self.memo = {}

    def fill(self, blobs):
        todo = sorted({d for d in blobs if d not in self.memo})
        if not todo:
            return
        if self.jobs > 1 and len(todo) > 200:
            with ProcessPoolExecutor(self.jobs) as ex:
                for d, r in ex.map(_sig_job, todo, chunksize=256):
                    self.memo[d] = r
        else:
            for d in todo:
                self.memo[d] = _sig_job(d)[1]

    def get(self, d):
        return self.memo[d]


# ---------------------------------------------------------------- checks


class Checks:
    def __init__(self):
        self.items = []

    def add(self, cid, ok, reason, warn=False, **detail):
        status = "PASS" if ok else ("WARN" if warn else "FAIL")
        self.items.append({"id": cid, "status": status, "reason": reason, **detail})
        return ok

    def status(self, cid):
        return next(c["status"] for c in self.items if c["id"] == cid)


def check_recording(rec, ck, min_hours):
    ck.add(
        "recording.files",
        not rec.open_segment
        and rec.files
        and all(os.path.basename(p) == f"{i + 1:06d}.jsonl.gz" for i, p in enumerate(rec.files)),
        "closed segments numbered 000001.. without holes, no open current.jsonl"
        if not rec.open_segment
        else "open segment current.jsonl present (recorder still running or killed)",
        segments=len(rec.files),
    )
    ck.add(
        "recording.parse", rec.bad_lines == 0, f"{rec.bad_lines} unparsable lines", bad_lines=rec.bad_lines
    )
    ledgers = rec.of("ledger")
    gaps = rec.of("gap")
    idx = [e["li"] for e in ledgers]
    holes = [(a, b) for a, b in zip(idx, idx[1:], strict=False) if b != a + 1]
    ck.add(
        "recording.gaps",
        not gaps and not holes and bool(idx),
        f"{len(gaps)} gap events, {len(holes)} index holes over {len(idx)} ledgers",
        gap_events=len(gaps),
        index_holes=holes[:20],
        ledgers=len(idx),
        first_ledger=idx[0] if idx else None,
        last_ledger=idx[-1] if idx else None,
    )
    stops = rec.of("stop")
    sessions = rec.of("session")
    span = (ledgers[-1]["t"] - ledgers[0]["t"]) if len(ledgers) > 1 else 0
    planned = rec.window["window_end"] - rec.window["window_start"]
    ok = bool(stops) and stops[-1]["reason"] == "window complete" and span >= min_hours * 3600
    ck.add(
        "recording.window",
        ok,
        f"window {iso(rec.window['window_start'])} -> {iso(rec.window['window_end'])}"
        f" ({planned / 3600:.2f} h planned), ledger stream span {span / 3600:.3f} h,"
        f" last stop '{stops[-1]['reason'] if stops else '-'}', minimum {min_hours} h",
        window_start=iso(rec.window["window_start"]),
        window_end=iso(rec.window["window_end"]),
        first_ledger_received=iso(ledgers[0]["t"]) if ledgers else None,
        last_ledger_received=iso(ledgers[-1]["t"]) if ledgers else None,
        span_seconds=round(span, 3),
        sessions=len(sessions),
        connections=len(rec.of("conn")),
        validations=len(rec.of("val")),
    )


def check_vl(rec, ck):
    """-> trusted masters (hex set), VL manifests {master: manifest dict}, publisher key."""
    snaps = rec.of("snap")
    node = snaps[0]["v"]["publisher_lists"][0] if snaps and snaps[0]["v"].get("publisher_lists") else {}
    out = {}
    for tag in ("start", "end"):
        vl = rec.vl[tag]
        cid = f"vl.{tag}"
        if vl is None:
            ck.add(cid, False, f"vl-{tag}.json missing")
            continue
        fetch = [f for f in rec.vl_fetch if f["tag"] == tag]
        sha = sha256_file(os.path.join(rec.data, f"vl-{tag}.json"))
        d = L.decode_vl(vl)
        pub_ok = L.verify_manifest(d["publisher"])
        blobs = d["blobs"]
        b = max(blobs, key=lambda x: x["sequence"]) if blobs else None
        masters = sorted(k.upper() for k, _m in b["validators"]) if b else []
        eff = (b["effective"] or 0) + RIPPLE_EPOCH if b else 0
        exp = (b["expiration"] or 0) + RIPPLE_EPOCH if b else 0
        problems = []
        if not fetch or fetch[-1]["sha256"] != sha:
            problems.append("fetch record sha256 differs from file")
        if pub_ok != (True, True):
            problems.append(f"publisher manifest signatures {pub_ok}")
        if not b or not b["signature_ok"] or not b["low_s"]:
            problems.append("blob signature invalid or not low-S")
        if d["publisher"]["master"] != (vl.get("public_key") or "").upper():
            problems.append("public_key != publisher manifest master")
        if node.get("pubkey_publisher", "").upper() != d["publisher"]["master"]:
            problems.append("node trusts a different publisher key")
        if b and node.get("seq") != b["sequence"]:
            problems.append(f"node list seq {node.get('seq')} != blob sequence {b['sequence']}")
        node_list = sorted(hexkey(k) or k for k in node.get("list", []))
        if node_list != masters:
            problems.append("node trusted list != VL validators")
        if not (eff <= rec.window["window_start"] and exp > rec.window["window_end"]):
            problems.append("validity window does not cover the recording")
        ck.add(
            cid,
            not problems,
            "; ".join(problems)
            or "publisher manifest, blob signature (low-S), validity window and node trust all agree",
            sha256=sha,
            sequence=b["sequence"] if b else None,
            validators=len(masters),
            effective=iso(eff),
            expiration=iso(exp),
            publisher=d["publisher"]["master"],
        )
        out[tag] = (
            set(masters),
            {L.parse_manifest(m)["master"]: L.parse_manifest(m) for _k, m in b["validators"]} if b else {},
        )
    same = (
        "start" in out
        and "end" in out
        and out["start"][0] == out["end"][0]
        and (rec.vl["start"] == rec.vl["end"])
    )
    ck.add(
        "vl.stable",
        same,
        "start and end VL byte-identical (one trusted set for the whole window)"
        if same
        else "VL changed during the window (graded against the start list)",
        warn=True,
    )
    return out.get("start", (set(), {}))


def check_manifests(rec, ck, trusted, vl_manifests):
    """-> {signing key hex: (master hex, seq)} from every verified manifest (highest seq per master)."""
    per_master = {}
    bad = []
    rows = [("vl", m) for m in vl_manifests.values()]
    for e in rec.of("man"):
        try:
            rows.append(("node", L.parse_manifest(e["m"])))
        except ValueError as x:
            bad.append({"req": e["req"], "error": str(x)})
    for src, m in rows:
        ok = L.verify_manifest(m)
        if ok != (True, True):
            bad.append({"master": b58(m["master"]), "src": src, "verify": list(ok)})
            continue
        cur = per_master.get(m["master"])
        if cur is None or m["sequence"] > cur["sequence"]:
            per_master[m["master"]] = m
    node_seq = {}
    for e in rec.of("man"):
        try:
            m = L.parse_manifest(e["m"])
        except ValueError:
            continue
        node_seq[m["master"]] = max(node_seq.get(m["master"], -1), m["sequence"])
    older = [b58(k) for k, m in vl_manifests.items() if node_seq.get(k, -1) < m["sequence"]]
    ck.add(
        "manifests.verify",
        not bad,
        f"{len(rows) - len(bad)} of {len(rows)} manifests verify (master + signing signature)",
        manifests=len(rows),
        failures=bad[:20],
    )
    missing = sorted(b58(k) for k in trusted if k not in per_master or not per_master[k]["signing"])
    ck.add(
        "manifests.trusted",
        not missing and not older,
        f"{len(trusted) - len(missing)} of {len(trusted)} trusted masters have a verified manifest with a"
        f" signing key; node manifest older than the VL's for {len(older)}",
        missing=missing,
        node_older=older,
    )
    return {m["signing"]: (k, m["sequence"]) for k, m in per_master.items() if m["signing"]}


def check_snapshots(rec, ck, trusted, signing_map):
    problems = []
    master_to_signing = {mk: sk for sk, (mk, _s) in signing_map.items()}
    for e in rec.of("snap"):
        v = e["v"]
        tk = sorted(hexkey(k) or k for k in v.get("trusted_validator_keys", []))
        if tk != sorted(trusted):
            problems.append(f"{iso(e['t'])} trusted_validator_keys != VL validators")
        for mk_b58, sk_b58 in (v.get("signing_keys") or {}).items():
            mk = hexkey(mk_b58)
            if mk in trusted and master_to_signing.get(mk) != hexkey(sk_b58):
                problems.append(f"{iso(e['t'])} signing key of {mk_b58} differs from its manifest")
    n = len(rec.of("snap"))
    ck.add(
        "trust.snapshots",
        n > 0 and not problems,
        f"{n} validators snapshots: trusted set and signing keys match the VL and manifests"
        if not problems
        else "; ".join(problems[:10]),
        snapshots=n,
        problems=problems[:20],
    )


def check_validations(rec, ck, sigs, signing_map, trusted):
    """-> list of graded validations: dict(li, lh, master, trusted, full, ok, reason, sk)."""
    vals = rec.of("val")
    sigs.fill(e["d"] for e in vals)
    graded = []
    dec_bad, mism, sig_bad = [], [], []
    classes = {}
    for e in vals:
        r = sigs.get(e["d"])
        if "decode" in r:
            dec_bad.append({"li": e["li"], "k": e["k"], "error": r["decode"]})
            graded.append(
                {
                    "li": e["li"],
                    "lh": e["lh"],
                    "ok": False,
                    "reason": "decode_error",
                    "sk": e["k"],
                    "master": None,
                    "trusted": False,
                    "full": False,
                }
            )
            continue
        sk = hexkey(e["k"])
        diffs = [
            f
            for f, a, b in (
                ("validation_public_key", sk, r["signing_key"]),
                ("ledger_index", e["li"], r["ledger_index"]),
                ("ledger_hash", e["lh"], r["ledger_hash"]),
                ("signing_time", e["st"], r["signing_time"]),
                ("flags", e["fl"], r["flags"]),
                ("full", e["full"], r["full"]),
                ("validated_hash", e.get("vh"), r["validated_hash"]),
                ("signature", e["sig"], r["signature"]),
            )
            if a != b
        ]
        master, _seq = signing_map.get(r["signing_key"], (None, None))
        if e.get("mk") and master and hexkey(e["mk"]) != master:
            diffs.append("master_key")
        if diffs:
            mism.append({"li": e["li"], "k": e["k"], "fields": diffs})
        cls = "trusted" if master in trusted else ("no_manifest" if master is None else "not_in_vl")
        if not r["sig_ok"]:
            sig_bad.append(
                {"li": e["li"], "k": e["k"], "reason": r["sig_reason"], "trusted": cls == "trusted"}
            )
        classes[cls] = classes.get(cls, 0) + 1
        ok = r["sig_ok"] and not diffs
        graded.append(
            {
                "li": e["li"],
                "lh": e["lh"],
                "ok": ok,
                "reason": "ok" if ok else (r["sig_reason"] if not r["sig_ok"] else "field_mismatch"),
                "sk": e["k"],
                "master": master,
                "trusted": cls == "trusted",
                "full": r["full"],
            }
        )
    n = len(vals)
    ck.add(
        "validations.decode",
        not dec_bad,
        f"{n - len(dec_bad)} of {n} STValidation blobs decode",
        validations=n,
        failures=dec_bad[:20],
    )
    ck.add(
        "validations.fields",
        not mism,
        f"{n - len(mism)} of {n}: node-decoded fields == signed blob"
        " (key, index, hash, signing_time, flags, full, validated_hash, signature, master)",
        failures=mism[:20],
        mismatches=len(mism),
    )
    trusted_bad = [x for x in sig_bad if x["trusted"]]
    ck.add(
        "validations.signatures",
        not sig_bad,
        f"{n - len(sig_bad)} of {n} signatures verify ({len(trusted_bad)} invalid from trusted keys)",
        invalid=len(sig_bad),
        failures=sig_bad[:20],
    )
    ck.add(
        "validations.trust",
        classes.get("trusted", 0) > 0,
        "signing key -> verified manifest -> master in the VL: "
        + ", ".join(f"{k} {v}" for k, v in sorted(classes.items())),
        classes=dict(sorted(classes.items())),
    )
    return graded


def quorum_events(rec, trusted):
    out = []
    for e in rec.of("q"):
        nunl = set()
        for x in (e.get("nunl") or {}).get("DisabledValidators") or []:
            k = (x.get("DisabledValidator") or x).get("PublicKey")
            if k:
                nunl.add(k.upper())
        out.append(
            {
                "li": e.get("li") or 0,
                "quorum": e.get("quorum"),
                "nunl": nunl,
                "expected": V.expected_quorum(len(trusted), len(trusted - nunl)),
                "t": e["t"],
                "nunl_known": e.get("nunl") is not None,
            }
        )
    return out


def check_quorum(rec, ck, graded, trusted):
    qs = quorum_events(rec, trusted)
    qbad = [
        {"li": q["li"], "node": q["quorum"], "expected": q["expected"]}
        for q in qs
        if q["quorum"] != q["expected"] or not q["nunl_known"]
    ]
    ck.add(
        "quorum.node",
        bool(qs) and not qbad,
        f"{len(qs)} quorum samples; node quorum == max(ceil(eff*0.67), ceil(n*0.6)) with n={len(trusted)}",
        samples=len(qs),
        values=sorted({q["quorum"] for q in qs if q["quorum"] is not None}),
        nunl_sizes=sorted({len(q["nunl"]) for q in qs}),
        failures=qbad[:20],
    )
    by = {}
    for g in graded:
        by.setdefault(g["li"], []).append(g)
    conn = 0
    first_in_conn = set()
    seen_conn = set()
    for e in rec.events:
        if e.get("ev") == "conn":
            conn += 1
        elif e.get("ev") == "ledger" and conn not in seen_conn:
            seen_conn.add(conn)
            first_in_conn.add(e["li"])
    rows, fails, margins = [], [], []
    for e in rec.of("ledger"):
        li, lh = e["li"], e["lh"]
        # the quorum sample in effect: the last one taken at or before this ledger (else the first)
        q = next((x for x in reversed(qs) if x["li"] <= li), qs[0] if qs else None)
        quorum = q["quorum"] if q else None
        nunl = q["nunl"] if q else set()
        vs = by.get(li, [])
        good = [g for g in vs if g["trusted"] and g["ok"] and g["full"] and g["lh"] == lh]
        signers = {g["master"] for g in good if g["master"] not in nunl}
        excluded = {g["master"] for g in good if g["master"] in nunl}
        other = {g["master"] for g in vs if g["trusted"] and g["ok"] and g["full"] and g["lh"] != lh}
        edge = li in first_in_conn
        met = quorum is not None and len(signers) >= quorum
        margin = len(signers) - quorum if quorum is not None else None
        result = ("edge_met" if met else "edge_short") if edge else ("met" if met else "BELOW_QUORUM")
        if not edge:
            margins.append(margin if margin is not None else -999)
            if not met:
                fails.append({"li": li, "signers": len(signers), "quorum": quorum})
        rows.append(
            {
                "ledger_index": li,
                "ledger_hash": lh,
                "close_time": iso(e["ct"] + RIPPLE_EPOCH) if e.get("ct") is not None else "",
                "txn_count": e.get("txn"),
                "trusted_signers": len(signers),
                "nunl_excluded": len(excluded),
                "quorum": quorum,
                "quorum_expected": q["expected"] if q else None,
                "margin": margin,
                "result": result,
                "trusted_other_hash": len(other),
                "untrusted_validations": sum(1 for g in vs if not g["trusted"]),
                "partial_validations": sum(1 for g in vs if not g["full"]),
                "invalid_validations": sum(1 for g in vs if not g["ok"]),
                "signer_masters": signers,
            }
        )
    graded_n = len(margins)
    ck.add(
        "quorum.ledgers",
        graded_n > 0 and not fails,
        f"{graded_n - len(fails)} of {graded_n} observed ledgers met quorum; margin min"
        f" {min(margins) if margins else '-'}, max {max(margins) if margins else '-'}",
        observed=graded_n,
        below_quorum=len(fails),
        failures=fails[:20],
        margin_min=min(margins) if margins else None,
        margin_max=max(margins) if margins else None,
        margin_histogram={str(m): margins.count(m) for m in sorted(set(margins))},
    )
    edges = [r for r in rows if r["result"].startswith("edge")]
    ck.add(
        "quorum.edges",
        all(r["result"] == "edge_met" for r in edges),
        f"{len(edges)} edge ledgers (first ledger of a connection, not graded):"
        f" {sum(r['result'] == 'edge_met' for r in edges)} met quorum anyway",
        warn=True,
        edge_ledgers=[r["ledger_index"] for r in edges],
    )
    return rows


def check_rpc(rec, ck, rows, args):
    cache_p = os.path.join(rec.data, "rpc-cache.json")
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}
    graded = [r for r in rows if not r["result"].startswith("edge")]
    n = min(args.sample, len(graded))
    pick = [graded[round(i * (len(graded) - 1) / max(n - 1, 1))] for i in range(n)] if n else []
    pick = list({r["ledger_index"]: r for r in pick}.values())
    changed = False
    bad, retries = [], 0
    for r in pick:
        key = str(r["ledger_index"])
        if key not in cache and not args.offline:
            for attempt in range(5):
                try:
                    body = json.dumps(
                        {"method": "ledger", "params": [{"ledger_index": r["ledger_index"]}]}
                    ).encode()
                    req = urllib.request.Request(
                        args.rpc, data=body, headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        res = json.loads(resp.read())["result"]
                    if res.get("status") == "success":
                        cache[key] = {
                            "ledger_hash": res.get("ledger_hash"),
                            "validated": res.get("validated"),
                            "close_time": (res.get("ledger") or {}).get("close_time"),
                        }
                        changed = True
                        break
                except (OSError, ValueError, KeyError):
                    pass
                retries += 1
                time.sleep(2 * (attempt + 1))
        got = cache.get(key)
        if got is None or got["ledger_hash"] != r["ledger_hash"] or got["validated"] is not True:
            bad.append({"li": r["ledger_index"], "recorded": r["ledger_hash"], "rpc": got})
    if changed:
        with open(cache_p + ".tmp", "w") as fh:
            json.dump(dict(sorted(cache.items(), key=lambda kv: int(kv[0]))), fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(cache_p + ".tmp", cache_p)
    ck.add(
        "rpc.crosscheck",
        len(pick) >= min(50, args.sample) and not bad,
        f"{len(pick) - len(bad)} of {len(pick)} sampled ledgers: public RPC validated hash == recorded"
        " (answers kept in rpc-cache.json)",
        sampled=[r["ledger_index"] for r in pick],
        failures=bad[:20],
    )
    return retries


# ---------------------------------------------------------------- run


def verify(rec, args, sigs, edit_vl_manifests=None):
    """All checks except rpc.crosscheck. edit_vl_manifests: hook for the key-absent control only."""
    ck = Checks()
    check_recording(rec, ck, args.min_hours)
    trusted, vl_manifests = check_vl(rec, ck)
    if edit_vl_manifests:
        edit_vl_manifests(vl_manifests)
    signing_map = check_manifests(rec, ck, trusted, vl_manifests)
    check_snapshots(rec, ck, trusted, signing_map)
    graded = check_validations(rec, ck, sigs, signing_map, trusted)
    rows = check_quorum(rec, ck, graded, trusted)
    return ck, graded, rows, trusted, signing_map


def validator_rows(graded, rows, trusted, signing_map):
    observed = {r["ledger_index"] for r in rows if not r["result"].startswith("edge")}
    signed = {}
    for r in rows:
        if r["ledger_index"] in observed:
            for m in r["signer_masters"]:
                signed[m] = signed.get(m, 0) + 1
    per = {}
    for g in graded:
        key = g["master"] or ("sk:" + g["sk"])
        p = per.setdefault(
            key, {"keys": set(), "n": 0, "full": 0, "ok": 0, "bad": 0, "trusted": g["trusted"]}
        )
        p["keys"].add(g["sk"])
        p["n"] += 1
        p["full"] += g["full"]
        p["ok"] += g["ok"]
        p["bad"] += not g["ok"]
    for m in trusted:
        per.setdefault(m, {"keys": set(), "n": 0, "full": 0, "ok": 0, "bad": 0, "trusted": True})
    out = []
    for key, p in per.items():
        master = None if key.startswith("sk:") else key
        out.append(
            {
                "master_key": b58(master) if master else "",
                "signing_keys": ";".join(sorted(p["keys"])),
                "trusted": p["trusted"] or master in trusted,
                "validations": p["n"],
                "full": p["full"],
                "partial": p["n"] - p["full"],
                "valid": p["ok"],
                "invalid": p["bad"],
                "observed_ledgers_signed": signed.get(master, 0) if master else 0,
                "observed_ledgers": len(observed),
                "participation_pct": f"{100 * signed.get(master, 0) / len(observed):.2f}"
                if master and observed
                else "0.00",
            }
        )
    out.sort(
        key=lambda r: (
            not r["trusted"],
            -int(r["observed_ledgers_signed"]),
            r["master_key"],
            r["signing_keys"],
        )
    )
    return out


def write_csv(path, rows, cols):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    with open(path, "w") as fh:
        fh.write(buf.getvalue())


LEDGER_COLS = [
    "ledger_index",
    "ledger_hash",
    "close_time",
    "txn_count",
    "trusted_signers",
    "nunl_excluded",
    "quorum",
    "quorum_expected",
    "margin",
    "result",
    "trusted_other_hash",
    "untrusted_validations",
    "partial_validations",
    "invalid_validations",
]
VALIDATOR_COLS = [
    "master_key",
    "signing_keys",
    "trusted",
    "validations",
    "full",
    "partial",
    "valid",
    "invalid",
    "observed_ledgers_signed",
    "observed_ledgers",
    "participation_pct",
]


def report_md(ck, rows, vrows, inputs):
    by = {c["id"]: c for c in ck.items}
    w = by["recording.window"]
    q = by["quorum.ledgers"]
    lines = [
        "# valsig report: validation signatures and UNL quorum per validated ledger",
        "",
        "Generated by `valsig/verify_valsig.py` from the committed recording; every number below is in"
        " `checks.json`.",
        "",
        "## Window",
        "",
        f"- recording window: {w['window_start']} -> {w['window_end']}",
        f"- ledger stream: {w['first_ledger_received']} -> {w['last_ledger_received']}"
        f" ({w['span_seconds'] / 3600:.3f} h), sessions {w['sessions']}, connections {w['connections']}",
        f"- ledgers {by['recording.gaps']['ledgers']} ({by['recording.gaps']['first_ledger']}.."
        f"{by['recording.gaps']['last_ledger']}), gap events {by['recording.gaps']['gap_events']},"
        f" validations {w['validations']}",
        "",
        "## Checks",
        "",
        "| check | status | result |",
        "|---|---|---|",
    ]
    lines += [f"| `{c['id']}` | {c['status']} | {c['reason']} |" for c in ck.items]
    trusted = [r for r in vrows if r["trusted"]]
    lines += [
        "",
        "## Quorum",
        "",
        f"- observed ledgers {q['observed']}, below quorum {q['below_quorum']},"
        f" margin min {q['margin_min']} max {q['margin_max']}",
        "- margin histogram (signers - quorum: ledgers): "
        + ", ".join(f"{k}: {v}" for k, v in q["margin_histogram"].items()),
        "",
        "## Trusted validators (UNL)",
        "",
        "| master key | observed ledgers signed | participation % | validations | invalid |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| `{r['master_key']}` | {r['observed_ledgers_signed']} / {r['observed_ledgers']} |"
        f" {r['participation_pct']} | {r['validations']} | {r['invalid']} |"
        for r in trusted
    ]
    untrusted = [r for r in vrows if not r["trusted"]]
    lines += [
        "",
        f"Untrusted signers (not in the VL or without a manifest): {len(untrusted)}, "
        f"{sum(r['validations'] for r in untrusted)} validations (recorded and signature-checked, never"
        " counted toward quorum).",
        "",
        "## Inputs (sha256)",
        "",
    ]
    lines += [f"- `{k}` {v}" for k, v in inputs.items()]
    return "\n".join(lines) + "\n"


def cmd_run(args):
    rec = Recording(args.data)
    sigs = SigCache(args.jobs)
    ck, graded, rows, trusted, signing_map = verify(rec, args, sigs)
    retries = check_rpc(rec, ck, rows, args)
    inputs = rec.inputs()
    os.makedirs(args.out, exist_ok=True)
    doc = {
        "schema": 1,
        "inputs": inputs,
        "checks": ck.items,
        "summary": {s: sum(c["status"] == s for c in ck.items) for s in ("PASS", "WARN", "FAIL")},
    }
    with open(os.path.join(args.out, "checks.json"), "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    write_csv(os.path.join(args.out, "ledgers.csv"), rows, LEDGER_COLS)
    vrows = validator_rows(graded, rows, trusted, signing_map)
    write_csv(os.path.join(args.out, "validators.csv"), vrows, VALIDATOR_COLS)
    with open(os.path.join(args.out, "REPORT.md"), "w") as fh:
        fh.write(report_md(ck, rows, vrows, inputs))
    for c in ck.items:
        print(f"CHECK {c['id']} {c['status']} {c['reason']}")
    print(f"MODE {'offline (rpc-cache.json only)' if args.offline else 'online ' + args.rpc}")
    print(
        f"RESULT PASS={doc['summary']['PASS']} WARN={doc['summary']['WARN']} FAIL={doc['summary']['FAIL']}"
        f" rpc_retries={retries}"
    )
    return 1 if doc["summary"]["FAIL"] else 0


# ---------------------------------------------------------------- negative controls


def _first_trusted_val(rec, graded, li=None):
    for e, g in zip(rec.of("val"), graded, strict=True):
        if g["trusted"] and g["ok"] and g["full"] and (li is None or g["li"] == li):
            return e
    raise SystemExit("no trusted validation to mutate")


def _set_field(d_hex, tf, new_value):
    data = bytes.fromhex(d_hex)
    for t, f, s, e, v in V.parse_fields(data):
        if (t, f) == tf:
            head = data[s : e - len(v)]
            return (data[:s] + head + new_value + data[e:]).hex().upper()
    raise ValueError(f"field {tf} not present")


def ctl_flip_bit(rec, graded, rows, signing_map):
    e = _first_trusted_val(rec, graded)
    data = bytearray(bytes.fromhex(e["d"]))
    for t, f, _s, end, _v in V.parse_fields(bytes(data)):
        if (t, f) == (3, 10):  # sfCookie: in the signed data, not decoded into a stream field
            data[end - 1] ^= 0x01
    e["d"] = bytes(data).hex().upper()
    return "validations.signatures", "bad_signature", {"li": e["li"], "k": e["k"]}


def ctl_wrong_key(rec, graded, rows, signing_map):
    e = _first_trusted_val(rec, graded)
    own = hexkey(e["k"])
    other = sorted(sk for sk in signing_map if sk != own)[0]
    e["d"] = _set_field(e["d"], (7, 3), bytes.fromhex(other))
    e["k"] = b58(other)
    e.pop("mk", None)
    return "validations.signatures", "bad_signature", {"li": e["li"], "k": e["k"]}


def ctl_key_absent(rec, graded, rows, signing_map):
    """Remove every manifest of one trusted master (node answers and the VL's copy): its signing key
    can no longer be tied to a master, so the master must show as missing."""
    e = _first_trusted_val(rec, graded)
    master = signing_map[hexkey(e["k"])][0]
    rec.events = [
        x for x in rec.events if not (x.get("ev") == "man" and L.parse_manifest(x["m"])["master"] == master)
    ]
    rec.edit_vl_manifests = lambda vm: vm.pop(master, None)
    return "manifests.trusted", "missing", {"master": b58(master)}


def ctl_tamper_ledger_hash(rec, graded, rows, signing_map):
    e = _first_trusted_val(rec, graded)
    lh = bytearray(bytes.fromhex(e["lh"]))
    lh[0] ^= 0x80
    e["d"] = _set_field(e["d"], (5, 1), bytes(lh))
    e["lh"] = bytes(lh).hex().upper()
    return "validations.signatures", "bad_signature", {"li": e["li"], "k": e["k"]}


def ctl_below_quorum(rec, graded, rows, signing_map):
    row = next(r for r in rows if r["result"] == "met")
    li, keep = row["ledger_index"], row["quorum"] - 1
    masters = sorted(row["signer_masters"])[keep:]
    drop = {sk for sk, (mk, _s) in signing_map.items() if mk in masters}
    rec.events = [
        x for x in rec.events if not (x.get("ev") == "val" and x["li"] == li and hexkey(x["k"]) in drop)
    ]
    return "quorum.ledgers", "below_quorum", {"li": li, "signers": keep, "quorum": row["quorum"]}


CONTROLS = [
    ("flipped_blob_bit", ctl_flip_bit),
    ("wrong_key", ctl_wrong_key),
    ("key_absent_from_manifests", ctl_key_absent),
    ("tampered_ledger_hash", ctl_tamper_ledger_hash),
    ("below_quorum_signers", ctl_below_quorum),
]


def run_control(args, sigs, base, fn):
    rec = Recording(args.data)
    _ck, graded, rows, _t, signing_map = base
    rec.events = json.loads(json.dumps(rec.events))
    cid, reason, where = fn(rec, graded, rows, signing_map)
    ck, *_ = verify(rec, args, sigs, getattr(rec, "edit_vl_manifests", None))
    c = next(x for x in ck.items if x["id"] == cid)
    hit = c["status"] == "FAIL" and (
        (
            reason == "bad_signature"
            and any(
                f["reason"] == "bad_signature" and f["li"] == where["li"] and f["k"] == where["k"]
                for f in c["failures"]
            )
        )
        or (reason == "missing" and where["master"] in c["missing"])
        or (
            reason == "below_quorum"
            and any(f["li"] == where["li"] and f["signers"] == where["signers"] for f in c["failures"])
        )
    )
    others = [x["id"] for x in ck.items if x["status"] == "FAIL" and x["id"] != cid]
    return hit, c, where, others


def cmd_controls(args):
    rec = Recording(args.data)
    sigs = SigCache(args.jobs)
    base = verify(rec, args, sigs)
    base_fail = [c["id"] for c in base[0].items if c["status"] == "FAIL"]
    print(f"BASELINE fail={base_fail or 'none'}")
    if base_fail:
        print("RESULT FAIL baseline must pass before controls mean anything")
        return 1
    failed = 0
    for name, fn in CONTROLS:
        hit, c, where, others = run_control(args, sigs, base, fn)
        print(
            f"CONTROL {name} {'FAILS_AS_DESIGNED' if hit else 'NOT_DETECTED'} check={c['id']} "
            f'status={c["status"]} where={json.dumps(where, sort_keys=True)} reason="{c["reason"]}"'
            f" other_fails={others or 'none'}"
        )
        failed += not hit
    print(
        f"RESULT {'PASS' if not failed else 'FAIL'} controls={len(CONTROLS)}"
        f" detected={len(CONTROLS) - failed}"
    )
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "controls"):
        p = sub.add_parser(name)
        p.add_argument("data")
        p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        p.add_argument("--min-hours", type=float, default=4.0)
        if name == "run":
            p.add_argument("--out", required=True)
            p.add_argument("--rpc", default=RPC)
            p.add_argument("--offline", action="store_true")
            p.add_argument("--sample", type=int, default=60)
    a = ap.parse_args(argv)
    return cmd_run(a) if a.cmd == "run" else cmd_controls(a)


if __name__ == "__main__":
    sys.exit(main())
