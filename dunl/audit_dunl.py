#!/usr/bin/env python3
"""Dynamic UNL consistency audit + history analysis (Python 3 stdlib only, offline).

Reads the collector cache (cache.jsonl) or its committed pack (cache.jsonl.gz) plus the node
trusted-key snapshots written by nodes_dunl.sh, runs the checks and writes, deterministically
(same inputs -> byte-identical outputs):
  checks.json  every check result {check, scope, status PASS|WARN|FAIL, reason}
  membership.csv tenure.csv scores.csv entry_times.csv participation.csv
  charts/*.svg  REPORT.md  SHA256SUMS
Exit code: 0 no FAIL, 2 at least one FAIL.

Checks (one or more results each):
  selection       replaying unl_selector.select_unl on the round's own final scores, previous_unl and
                  selector parameters reproduces selected_unl.json (UNL + alternates, same order)
  vl_decode       signed VL of each round decodes; publisher manifest master + signing signatures,
                  blob signature (secp256k1 / SHA-512-Half, low-S) and every validator manifest
                  (master key == validation_public_key, master + signing signatures) verify
  sequence        blob sequence == round vl_sequence; published sequences strictly +1
  validity_window effective < expiration; each list valid until the next one is effective;
                  the latest list is not expired at the snapshot time
  validator_set   VL validators == selected_unl.json; size <= unl_max_size; previous_unl input of
                  round N == UNL published by the previous published round
  bundle_hashes   bundle.json file_hashes and outputs/verification_hashes.json match the content
                  hash (sha256 of compact sorted JSON) of every file
  mirror          live /vl.json == mirror VL == latest round's signed VL; /unl/current == latest UNL
  anchor          round memo tx: validated tesSUCCESS from the publisher address, MemoType == memo
                  type field, memo final_bundle_cid / vl_sequence / round_number == round record
  convergence     sealed report: summary == recount of participants; anchor tx memo carries the
                  report's round + convergence_bundle_cid from the publisher; every accepted
                  commit / reveal tx exists, validated tesSUCCESS
  nodes           each own node trusts exactly the latest VL's validators from the VL publisher at
                  the latest sequence; quorum within [ceil(0.6 n), ceil(0.8 n)]; nodes agree
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_dunl as L  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
ROUND_MEMO_TYPES = {"pf_dynamic_unl"}
CONVERGENCE_MEMO_TYPE = "pf_dynamic_unl_convergence_report_v1"
NO_VL_STATUSES = {"FAILED"}


# ---------------------------------------------------------------- loading


def load_rows(path):
    opener = gzip.open if path.endswith(".gz") else open
    rows = {}
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                rows[r["key"]] = r
    return rows


def _json(row):
    if not row or row.get("status") != 200 or row.get("body") is None:
        return None
    return json.loads(row["body"])


def dataset(rows):
    """rows (key -> cache row) -> dict used by checks and analyses."""
    epochs = sorted({k.split("/", 1)[0] for k in rows if k.split("/", 1)[1:2] == ["rounds/0"]})
    if not epochs:
        raise ValueError("no round list in cache")
    ep = epochs[-1]
    rounds = []
    off = 0
    while f"{ep}/rounds/{off}" in rows:
        page = _json(rows[f"{ep}/rounds/{off}"])
        if not page or not page["rounds"]:
            break
        rounds += page["rounds"]
        off += len(page["rounds"])
    rounds.sort(key=lambda r: r["round_number"])
    per = {}
    for r in rounds:
        n = r["round_number"]
        files, hashes = {}, {}
        prefix = f"round/{n}/file/"
        for k, row in rows.items():
            if k.startswith(prefix) and row["status"] == 200:
                f = k[len(prefix) :]
                if row.get("body") is not None:
                    files[f] = json.loads(row["body"])
                    hashes[f] = L.content_hash(files[f])
                else:
                    hashes[f] = row["content_hash"]
        per[n] = {
            "record": r,
            "detail": _json(rows.get(f"round/{n}/detail")),
            "convergence": _json(rows.get(f"round/{n}/convergence")),
            "files": files,
            "hashes": hashes,
        }
    txs = {}
    for k, row in rows.items():
        if k.startswith("tx/"):
            j = _json(row)
            txs[k[3:]] = (j or {}).get("result", {"error": f"http {row['status']}"})
    live = {name: _json(rows.get(f"{ep}/{name}")) for name in ("vl", "config", "health", "unl_current")}
    live["mirror_vl"] = _json(rows.get(f"{ep}/mirror_vl"))
    info = (_json(rows.get(f"{ep}/rpc_server_info")) or {}).get("result", {}).get("info", {})
    first = ((_json(rows.get(f"{ep}/rpc_first_ledger")) or {}).get("result", {}) or {}).get("ledger", {})
    live["rpc_history"] = {
        "complete_ledgers": info.get("complete_ledgers"),
        "first_ledger": int(first["ledger_index"]) if first.get("ledger_index") else None,
        "first_close_time": int(first["close_time"]) if first.get("close_time") is not None else None,
    }
    live["t"] = {name: (rows.get(f"{ep}/{name}") or {}).get("t") for name in ("vl", "mirror_vl")}
    live["raw"] = {name: (rows.get(f"{ep}/{name}") or {}).get("sha256") for name in ("vl", "mirror_vl")}
    return {"epoch": ep, "rounds": per, "txs": txs, "live": live}


def load_nodes(d):
    out = {}
    if d and os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".json"):
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    out[f[:-5]] = json.load(fh)
    return out


# ---------------------------------------------------------------- helpers


def res(check, scope, status, reason):
    return {"check": check, "scope": scope, "status": status, "reason": reason}


def signed_vl(rd):
    return rd["files"].get("outputs/signed_validator_list.json") or rd["files"].get("vl.json")


def selected_unl(rd):
    f = rd["files"].get("outputs/selected_unl.json") or rd["files"].get("unl.json")
    return list(f["unl"]) if f and "unl" in f else None


def published(ds):
    """Rounds that published a signed VL, ascending."""
    return [n for n, rd in sorted(ds["rounds"].items()) if signed_vl(rd) is not None]


def memo_list(tx):
    out = []
    for m in tx.get("Memos") or []:
        mm = m.get("Memo", {})
        try:
            mt = bytes.fromhex(mm.get("MemoType", "")).decode("utf-8")
            md = bytes.fromhex(mm.get("MemoData", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            out.append((None, None))
            continue
        try:
            out.append((mt, json.loads(md)))
        except ValueError:
            out.append((mt, md))
    return out


def tx_ok(tx):
    return (
        bool(tx)
        and tx.get("validated") is True
        and (tx.get("meta") or {}).get("TransactionResult") == "tesSUCCESS"
    )


def tx_state(ds, h, when_iso):
    """-> 'ok' | 'pruned' (txnNotFound and the event predates the public RPC's first retained
    ledger: not verifiable from public RPC) | 'missing' | 'failed'."""
    tx = ds["txs"].get(h)
    if tx_ok(tx):
        return "ok"
    hist = ds["live"].get("rpc_history") or {}
    first = hist.get("first_close_time")
    if tx is not None and tx.get("error") == "txnNotFound":
        if first is not None and when_iso and iso_to_unix(when_iso) - L.RIPPLE_EPOCH < first:
            return "pruned"
        return "missing"
    return "failed" if tx else "missing"


def history_note(ds):
    h = ds["live"].get("rpc_history") or {}
    if h.get("first_close_time") is None:
        return "public RPC history range unknown"
    first = ripple_to_iso(h["first_close_time"])
    return f"public RPC keeps ledgers {h['complete_ledgers']} (first closed {first})"


def ripple_to_iso(sec):
    return datetime.fromtimestamp(sec + L.RIPPLE_EPOCH, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_unix(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


_decoded = {}


def decoded(n, vl):
    key = (n, hashlib.sha256(json.dumps(vl, sort_keys=True).encode()).hexdigest())
    if key not in _decoded:
        d = L.decode_vl(vl)
        vals = []
        for k, m in d["blobs"][0]["validators"] if d["blobs"] else []:
            try:
                mm = L.parse_manifest(m)
                vals.append({"key": k, "master": mm["master"], "sigs": L.verify_manifest(mm), "err": None})
            except ValueError as e:
                vals.append({"key": k, "master": None, "sigs": (False, False), "err": str(e)})
        d["checked"] = vals
        d["publisher_sigs"] = L.verify_manifest(d["publisher"])
        _decoded[key] = d
    return _decoded[key]


# ---------------------------------------------------------------- checks


def check_vl_decode(ds):
    out = []
    for n in published(ds):
        scope = f"round {n}"
        try:
            d = decoded(n, signed_vl(ds["rounds"][n]))
        except (ValueError, KeyError, TypeError) as e:
            out.append(res("vl_decode", scope, FAIL, f"signed VL does not decode: {e}"))
            continue
        bad = []
        if d["public_key"] != d["publisher"]["master"]:
            bad.append("public_key != publisher manifest master key")
        if d["publisher_sigs"] != (True, True):
            bad.append(f"publisher manifest signatures (master, signing) = {d['publisher_sigs']}")
        if len(d["blobs"]) != 1:
            bad.append(f"{len(d['blobs'])} blobs (expected 1)")
        for b in d["blobs"]:
            if not b["signature_ok"]:
                bad.append(f"blob seq {b['sequence']} signature does not verify")
            elif not b["low_s"]:
                bad.append(f"blob seq {b['sequence']} signature not canonical low-S")
        for v in d["checked"]:
            if v["err"]:
                bad.append(f"{v['key'][:12]}.. manifest: {v['err']}")
            elif v["master"] != v["key"]:
                bad.append(f"{v['key'][:12]}.. manifest master key differs")
            elif v["sigs"] != (True, True):
                bad.append(f"{v['key'][:12]}.. manifest signatures (master, signing) = {v['sigs']}")
        if bad:
            out.append(res("vl_decode", scope, FAIL, "; ".join(bad)))
        else:
            nv = len(d["checked"])
            out.append(
                res(
                    "vl_decode",
                    scope,
                    PASS,
                    f"publisher {d['public_key'][:10]}.. manifest seq {d['publisher']['sequence']} sigs ok; "
                    f"blob seq {d['blobs'][0]['sequence']} sig ok (low-S); {nv}/{nv} validator manifests ok",
                )
            )
    return out


def check_sequence(ds):
    out = []
    prev = None
    for n in published(ds):
        rd = ds["rounds"][n]
        want = rd["record"].get("vl_sequence")
        try:
            got = decoded(n, signed_vl(rd))["blobs"][0]["sequence"]
        except (ValueError, KeyError, IndexError, TypeError):
            got = None
        scope = f"round {n}"
        if got != want:
            out.append(res("sequence", scope, FAIL, f"blob sequence {got} != round vl_sequence {want}"))
        elif prev is not None and got != prev[1] + 1:
            st = FAIL if got <= prev[1] else WARN
            out.append(
                res("sequence", scope, st, f"sequence {got} follows {prev[1]} (round {prev[0]}): not +1")
            )
        else:
            after = f" after {prev[1]} (round {prev[0]})" if prev else " (first published)"
            out.append(res("sequence", scope, PASS, f"blob sequence {got} == vl_sequence{after}"))
        if got is not None:
            prev = (n, got)
    for n, rd in sorted(ds["rounds"].items()):
        if signed_vl(rd) is None:
            st = WARN if rd["record"]["status"] in NO_VL_STATUSES else FAIL
            out.append(
                res("sequence", f"round {n}", st, f"status {rd['record']['status']}: no signed VL published")
            )
    return out


def check_validity_window(ds, now_iso):
    out = []
    pub = published(ds)
    blobs = {}
    for n in pub:
        try:
            blobs[n] = decoded(n, signed_vl(ds["rounds"][n]))["blobs"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    for i, n in enumerate(pub):
        b = blobs.get(n)
        if not b:
            continue
        scope = f"round {n}"
        eff, exp = b["effective"], b["expiration"]
        if not (isinstance(eff, int) and isinstance(exp, int) and eff < exp):
            out.append(res("validity_window", scope, FAIL, f"effective {eff} not before expiration {exp}"))
            continue
        nxt = next((m for m in pub[i + 1 :] if m in blobs), None)
        if nxt is not None and exp < blobs[nxt]["effective"]:
            out.append(
                res(
                    "validity_window",
                    scope,
                    FAIL,
                    f"expires {ripple_to_iso(exp)} before round {nxt} list is effective "
                    f"{ripple_to_iso(blobs[nxt]['effective'])}: no valid list in between",
                )
            )
            continue
        tail = (
            f"next list (round {nxt}) effective {ripple_to_iso(blobs[nxt]['effective'])}" if nxt else "latest"
        )
        out.append(
            res(
                "validity_window",
                scope,
                PASS,
                f"effective {ripple_to_iso(eff)} < expiration {ripple_to_iso(exp)}; {tail}",
            )
        )
    if pub and pub[-1] in blobs:
        exp = blobs[pub[-1]]["expiration"]
        now = iso_to_unix(now_iso) - L.RIPPLE_EPOCH
        st = PASS if exp > now else FAIL
        out.append(
            res(
                "validity_window",
                "latest",
                st,
                f"latest list (round {pub[-1]}) expires {ripple_to_iso(exp)}; snapshot {now_iso}: "
                f"{(exp - now) / 86400:.1f} days left",
            )
        )
    return out


def check_validator_set(ds):
    out = []
    cfg = ds["live"].get("config") or {}
    max_size = cfg.get("unl_max_size")
    prev_unl = None
    prev_n = None
    for n in published(ds):
        rd = ds["rounds"][n]
        scope = f"round {n}"
        unl = selected_unl(rd)
        try:
            d = decoded(n, signed_vl(rd))
            vl_keys = sorted(L.node_public_b58(v["key"]) for v in d["checked"])
        except (ValueError, KeyError, TypeError) as e:
            out.append(res("validator_set", scope, FAIL, f"VL keys unreadable: {e}"))
            continue
        bad = []
        if unl is None:
            bad.append("no selected UNL file")
        else:
            if len(set(unl)) != len(unl):
                bad.append("selected UNL has duplicate keys")
            if set(unl) != set(vl_keys):
                only_vl = sorted(set(vl_keys) - set(unl))
                only_unl = sorted(set(unl) - set(vl_keys))
                bad.append(f"VL vs selected UNL differ: only in VL {only_vl}, only in UNL {only_unl}")
        if max_size and len(vl_keys) > max_size:
            bad.append(f"{len(vl_keys)} validators > unl_max_size {max_size}")
        pu = rd["files"].get("inputs/previous_unl.json")
        note = ""
        if pu is not None and prev_unl is not None:
            if set(pu.get("previous_unl", [])) != set(prev_unl):
                bad.append(f"previous_unl input != UNL published by round {prev_n}")
            else:
                note = f"; previous_unl input == round {prev_n} UNL"
        if bad:
            out.append(res("validator_set", scope, FAIL, "; ".join(bad)))
        else:
            out.append(res("validator_set", scope, PASS, f"VL {len(vl_keys)} keys == selected UNL{note}"))
        prev_unl, prev_n = vl_keys, n
    return out


def replay_selection(scores, previous_unl, cutoff, max_size, min_gap):
    """Re-implementation of scoring_service/services/unl_selector.select_unl on (key, score) pairs:
    cutoff, max_size cap on incumbents, open seats to top challengers, min_gap swaps."""
    qualified = sorted([(k, sc) for k, sc in scores if sc >= cutoff], key=lambda v: (-v[1], v[0]))
    if not qualified:
        return [], []
    prev = set(previous_unl or [])
    if not prev:
        return [k for k, _s in qualified[:max_size]], [k for k, _s in qualified[max_size:]]
    inc = [v for v in qualified if v[0] in prev]
    unl, displaced = list(inc[:max_size]), inc[max_size:]
    open_seats = max_size - len(unl)
    remaining = []
    for ch in [v for v in qualified if v[0] not in prev]:
        if open_seats > 0:
            unl.append(ch)
            open_seats -= 1
            continue
        weakest = min(unl, key=lambda v: (v[1], v[0]))
        if ch[1] >= weakest[1] + min_gap:
            unl.remove(weakest)
            unl.append(ch)
            remaining.append(weakest)
        else:
            remaining.append(ch)
    unl.sort(key=lambda v: (-v[1], v[0]))
    alternates = sorted(displaced + remaining, key=lambda v: (-v[1], v[0]))
    return [k for k, _s in unl], [k for k, _s in alternates]


def check_selection(ds):
    """Replay the selector from the round's own published final scores, previous_unl input and
    selector parameters (runtime/execution_manifest.json) and compare with selected_unl.json."""
    out = []
    for n, rd in sorted(ds["rounds"].items()):
        f = rd["files"]
        fs, pu = f.get("outputs/final_scores.json"), f.get("inputs/previous_unl.json")
        params = ((f.get("runtime/execution_manifest.json") or {}).get("code", {}).get("selector") or {}).get(
            "parameters"
        )
        sel = f.get("outputs/selected_unl.json")
        if not (fs and pu is not None and params and sel):
            continue
        scores = [(s["master_key"], s["final_score"]) for s in fs.get("scores", [])]
        unl, alt = replay_selection(
            scores,
            pu.get("previous_unl"),
            params["score_cutoff"],
            params["max_size"],
            params["min_score_gap"],
        )
        p = f"cutoff {params['score_cutoff']}, max {params['max_size']}, gap {params['min_score_gap']}"
        if unl != sel.get("unl") or ("alternates" in sel and alt != sel["alternates"]):
            extra = sorted(set(unl) - set(sel.get("unl", [])))
            miss = sorted(set(sel.get("unl", [])) - set(unl))
            how = "order differs" if not extra and not miss else f"replay adds {extra}, drops {miss}"
            out.append(
                res("selection", f"round {n}", FAIL, f"replayed selection ({p}) != selected_unl: {how}")
            )
        else:
            scored = {k: sc for k, sc in scores}
            held = [k for k in unl if k in set(pu.get("previous_unl") or [])]
            low = min((scored[k] for k in unl), default=None)
            above = sum(1 for k in alt if scored.get(k, -1) > (low if low is not None else 10**9))
            out.append(
                res(
                    "selection",
                    f"round {n}",
                    PASS,
                    f"replay ({p}) == selected_unl ({len(unl)} + {len(alt)} alternates, same order); "
                    f"{len(held)} incumbents kept; {above} alternates score above the lowest selected "
                    f"({low}) but within the min gap",
                )
            )
    return out


def check_bundle_hashes(ds):
    out = []
    for n, rd in sorted(ds["rounds"].items()):
        bundle = rd["files"].get("bundle.json")
        if bundle is None:
            if signed_vl(rd) is not None:
                out.append(
                    res("bundle_hashes", f"round {n}", WARN, "legacy layout: no bundle.json / file_hashes")
                )
            continue
        bad, ok = [], 0
        for f, h in sorted(bundle.get("file_hashes", {}).items()):
            got = rd["hashes"].get(f)
            if got is None:
                bad.append(f"{f} not retrievable")
            elif got != h:
                bad.append(f"{f} content hash {got[:12]}.. != bundle {h[:12]}..")
            else:
                ok += 1
        vh = rd["files"].get("outputs/verification_hashes.json") or {}
        targets = {
            "model_response_hash": "outputs/model_response.json",
            "validator_scores_hash": "outputs/validator_scores.json",
            "final_scores_hash": "outputs/final_scores.json",
            "selected_unl_hash": "outputs/selected_unl.json",
            "signed_validator_list_hash": "outputs/signed_validator_list.json",
        }
        vok = 0
        for name, f in targets.items():
            if name in vh:
                if rd["hashes"].get(f) != vh[name]:
                    bad.append(f"verification_hashes {name} != content hash of {f}")
                else:
                    vok += 1
        if bad:
            out.append(res("bundle_hashes", f"round {n}", FAIL, "; ".join(bad)))
        else:
            out.append(
                res(
                    "bundle_hashes",
                    f"round {n}",
                    PASS,
                    f"{ok}/{len(bundle.get('file_hashes', {}))} file_hashes, "
                    f"{vok}/{len(vh)} verification_hashes",
                )
            )
    return out


def check_mirror(ds):
    out = []
    live = ds["live"]
    pub = published(ds)
    latest = signed_vl(ds["rounds"][pub[-1]]) if pub else None
    vl, mirror = live.get("vl"), live.get("mirror_vl")
    if vl is None or mirror is None:
        out.append(
            res(
                "mirror",
                "vl.json",
                FAIL,
                f"live VL {'ok' if vl else 'missing'}, mirror {'ok' if mirror else 'missing'}",
            )
        )
    else:
        same_bytes = live["raw"]["vl"] == live["raw"]["mirror_vl"]
        if vl != mirror:
            out.append(res("mirror", "vl.json", FAIL, "scoring /vl.json and mirror VL differ"))
        else:
            how = "byte-identical" if same_bytes else "JSON-equal (bytes differ: formatting)"
            out.append(res("mirror", "vl.json", PASS, f"scoring /vl.json == mirror VL ({how})"))
        if latest is not None:
            st = PASS if vl == latest else FAIL
            rel = "==" if vl == latest else "!="
            out.append(
                res("mirror", "latest", st, f"live /vl.json {rel} round {pub[-1]} signed_validator_list")
            )
    cur = live.get("unl_current")
    if cur is not None and pub:
        unl = selected_unl(ds["rounds"][pub[-1]]) or []
        cur_keys = cur.get("unl") if isinstance(cur, dict) else None
        if cur_keys is None:
            out.append(
                res("mirror", "unl/current", WARN, f"/unl/current has no 'unl' list (keys {sorted(cur)})")
            )
        elif set(cur_keys) == set(unl):
            out.append(
                res("mirror", "unl/current", PASS, f"/unl/current == round {pub[-1]} UNL ({len(unl)} keys)")
            )
        else:
            out.append(res("mirror", "unl/current", FAIL, f"/unl/current != round {pub[-1]} UNL"))
    return out


def check_anchor(ds):
    out = []
    publisher = (ds["live"].get("config") or {}).get("foundation_publisher_address")
    for n, rd in sorted(ds["rounds"].items()):
        r = rd["record"]
        scope = f"round {n}"
        h = r.get("memo_tx_hash")
        if not h:
            if signed_vl(rd) is not None:
                out.append(
                    res("anchor", scope, WARN, f"status {r['status']}: VL published without an on-chain memo")
                )
            continue
        tx = ds["txs"].get(h)
        state = tx_state(ds, h, r.get("completed_at"))
        if state == "pruned":
            out.append(
                res(
                    "anchor",
                    scope,
                    WARN,
                    f"memo tx {h[:12]}.. not verifiable: round completed "
                    f"{(r.get('completed_at') or '')[:16]}, before the retained history ({history_note(ds)})",
                )
            )
            continue
        if state != "ok":
            out.append(
                res("anchor", scope, FAIL, f"memo tx {h[:12]}.. {state}: not a validated tesSUCCESS tx")
            )
            continue
        bad = []
        if publisher and tx.get("Account") != publisher:
            bad.append(f"sender {tx.get('Account')} != publisher {publisher}")
        memos = memo_list(tx)
        hit = [(t, d) for t, d in memos if isinstance(d, dict) and d.get("round_number") == n]
        if not hit:
            bad.append(f"no memo for round {n} (memo types {[t for t, _d in memos]})")
        else:
            t, d = hit[0]
            if t != d.get("type"):
                bad.append(f"MemoType {t} != memo type field {d.get('type')}")
            if t not in ROUND_MEMO_TYPES and not str(t).startswith("pf_dynamic_unl"):
                bad.append(f"unexpected memo type {t}")
            if d.get("final_bundle_cid") != r.get("final_bundle_cid"):
                bad.append(f"memo CID {d.get('final_bundle_cid')} != round CID {r.get('final_bundle_cid')}")
            if d.get("vl_sequence") != r.get("vl_sequence"):
                bad.append(f"memo vl_sequence {d.get('vl_sequence')} != {r.get('vl_sequence')}")
        if bad:
            out.append(res("anchor", scope, FAIL, "; ".join(bad)))
        else:
            out.append(
                res(
                    "anchor",
                    scope,
                    PASS,
                    f"tx {h[:12]}.. ledger {tx.get('ledger_index')} {ripple_to_iso(tx['date'])} "
                    f"type {hit[0][0]}: "
                    f"CID, vl_sequence {r.get('vl_sequence')}, round match",
                )
            )
    return out


def check_convergence(ds):
    out = []
    publisher = (ds["live"].get("config") or {}).get("foundation_publisher_address")
    tracked = [
        n
        for n, rd in sorted(ds["rounds"].items())
        if (rd["convergence"] or {}).get("phase") not in (None, "not_tracked")
    ]
    for n, rd in sorted(ds["rounds"].items()):
        c = rd["convergence"]
        scope = f"round {n}"
        r = rd["record"]
        if not c or c.get("phase") == "not_tracked":
            # /convergence documents not_tracked as: override, not-yet-announced or pre-protocol
            announced = bool(r.get("input_package_cid")) and not r.get("override_type")
            if tracked and n > tracked[0] and announced and r.get("status") == "COMPLETE":
                out.append(
                    res(
                        "convergence",
                        scope,
                        WARN,
                        "phase not_tracked although the round is a normal COMPLETE round with a frozen "
                        f"input package ({r.get('input_package_cid')}, frozen "
                        f"{(r.get('input_frozen_at') or '')[:16]}) after convergence tracking began "
                        f"(round {tracked[0]}): no sealed report",
                    )
                )
            continue
        if not c.get("finalized"):
            out.append(res("convergence", scope, WARN, f"phase {c.get('phase')}: not sealed at snapshot"))
            continue
        rep = c.get("report") or {}
        parts = rep.get("participants", [])
        s = rep.get("summary", {})
        bad = []
        if s.get("committers") != len(parts):
            bad.append(f"summary committers {s.get('committers')} != {len(parts)} participants")
        if dict(s.get("outcomes", {})) != dict(Counter(p.get("outcome") for p in parts)):
            bad.append("summary outcomes != recount")
        lv = Counter(x for p in parts for x in (p.get("comparison_levels_matched") or "").split(",") if x)
        if dict(s.get("levels_matched", {})) != dict(lv):
            bad.append(f"summary levels_matched {s.get('levels_matched')} != recount {dict(lv)}")
        dv = Counter(p.get("divergence_category") for p in parts if p.get("divergence_category"))
        if dict(s.get("divergence_categories", {})) != dict(dv):
            bad.append("summary divergence_categories != recount")
        if rep.get("round_number") not in (None, n):
            bad.append(f"report round_number {rep.get('round_number')}")
        when = c.get("sealed_at") or rd["record"].get("completed_at")
        tx = ds["txs"].get(c.get("anchor_tx_hash") or "")
        st_anchor = tx_state(ds, c.get("anchor_tx_hash") or "", when)
        warn = []
        if st_anchor == "pruned":
            warn.append("anchor tx not verifiable (pruned)")
        elif st_anchor != "ok":
            bad.append(f"anchor tx {st_anchor} / not validated tesSUCCESS")
        else:
            if publisher and tx.get("Account") != publisher:
                bad.append(f"anchor sender {tx.get('Account')} != publisher")
            m = [d for t, d in memo_list(tx) if t == CONVERGENCE_MEMO_TYPE and isinstance(d, dict)]
            if not m:
                bad.append("anchor tx has no convergence report memo")
            elif m[0].get("round_number") != n or m[0].get("convergence_bundle_cid") != c.get(
                "convergence_bundle_cid"
            ):
                bad.append("anchor memo round / convergence_bundle_cid != report")
        states = Counter(
            tx_state(ds, p[k], when)
            for p in parts
            for k in ("accepted_commit_tx", "accepted_reveal_tx")
            if p.get(k)
        )
        nbad = sum(v for k, v in states.items() if k not in ("ok", "pruned"))
        if nbad:
            bad.append(f"{nbad} accepted commit/reveal txs not validated tesSUCCESS ({dict(states)})")
        if states.get("pruned"):
            warn.append(f"{states['pruned']} commit/reveal txs not verifiable (pruned)")
        ntx = sum(states.values())
        if bad:
            out.append(res("convergence", scope, FAIL, "; ".join(bad + warn)))
        elif warn:
            out.append(
                res(
                    "convergence",
                    scope,
                    WARN,
                    f"sealed; {len(parts)} participants, summary == recount; {states.get('ok', 0)}/{ntx} "
                    f"commit/reveal txs validated tesSUCCESS; " + "; ".join(warn) + f" ({history_note(ds)})",
                )
            )
        else:
            out.append(
                res(
                    "convergence",
                    scope,
                    PASS,
                    f"sealed; {len(parts)} participants, summary == recount; anchor memo ok; "
                    f"{ntx}/{ntx} commit/reveal txs validated tesSUCCESS",
                )
            )
    return out


def check_nodes(ds, nodes):
    out = []
    pub = published(ds)
    if not pub:
        return [res("nodes", "all", FAIL, "no published VL to compare")]
    d = decoded(pub[-1], signed_vl(ds["rounds"][pub[-1]]))
    keys = sorted(L.node_public_b58(v["key"]) for v in d["checked"])
    seq = d["blobs"][0]["sequence"]
    if not nodes:
        return [res("nodes", "all", FAIL, "no node snapshots")]
    for name, s in sorted(nodes.items()):
        bad = []
        tk = sorted(s.get("trusted_validator_keys", []))
        if tk != keys:
            bad.append(
                f"trusted keys differ from round {pub[-1]} VL: extra {sorted(set(tk) - set(keys))}, "
                f"missing {sorted(set(keys) - set(tk))}"
            )
        pls = s.get("publisher_lists", [])
        pl = [p for p in pls if p.get("pubkey_publisher") == d["public_key"]]
        if not pl:
            bad.append("no publisher list from the VL publisher key")
        else:
            if pl[0].get("seq") != seq:
                bad.append(f"publisher list seq {pl[0].get('seq')} != latest VL seq {seq}")
            if sorted(pl[0].get("list", [])) != keys:
                bad.append("publisher list keys != latest VL")
            if pl[0].get("available") is not True:
                bad.append("publisher list not available")
        if len(pls) != 1:
            bad.append(f"{len(pls)} publisher lists configured (expected 1)")
        n = len(tk)
        q = s.get("validation_quorum")
        lo, hi = math.ceil(0.6 * n), math.ceil(0.8 * n)
        if not (isinstance(q, int) and lo <= q <= hi):
            bad.append(f"quorum {q} outside [{lo}, {hi}] for {n} trusted keys")
        if bad:
            out.append(res("nodes", name, FAIL, "; ".join(bad)))
        else:
            out.append(
                res(
                    "nodes",
                    name,
                    PASS,
                    f"{s.get('t')}: trusts {n} keys == round {pub[-1]} VL seq {seq} from publisher "
                    f"{d['public_key'][:10]}..; quorum {q} in [{lo}, {hi}]",
                )
            )
    names = sorted(nodes)
    for a, b in zip(names, names[1:], strict=False):
        same = sorted(nodes[a].get("trusted_validator_keys", [])) == sorted(
            nodes[b].get("trusted_validator_keys", [])
        )
        sq = nodes[a].get("validation_quorum") == nodes[b].get("validation_quorum")
        st = PASS if same and sq else FAIL
        out.append(
            res(
                "nodes",
                f"{a} vs {b}",
                st,
                f"trusted keys {'equal' if same else 'DIFFER'}, quorum {'equal' if sq else 'DIFFERS'}",
            )
        )
    return out


def run_checks(ds, nodes):
    now = (
        max((s.get("t") for s in nodes.values()), default=None)
        or ds["live"]["t"].get("vl")
        or "1970-01-01T00:00:00Z"
    )
    out = []
    out += check_vl_decode(ds)
    out += check_sequence(ds)
    out += check_validity_window(ds, now)
    out += check_validator_set(ds)
    out += check_selection(ds)
    out += check_bundle_hashes(ds)
    out += check_mirror(ds)
    out += check_anchor(ds)
    out += check_convergence(ds)
    out += check_nodes(ds, nodes)
    return out


# ---------------------------------------------------------------- analyses


def unl_history(ds):
    hist = []
    for n in published(ds):
        rd = ds["rounds"][n]
        unl = selected_unl(rd)
        if unl is None:
            try:
                unl = [L.node_public_b58(v["key"]) for v in decoded(n, signed_vl(rd))["checked"]]
            except (ValueError, KeyError, TypeError):
                continue
        hist.append((n, rd["record"].get("vl_sequence"), sorted(set(unl))))
    return hist


def analyses(ds):
    hist = unl_history(ds)
    membership = []
    prev = None
    for n, seq, unl in hist:
        cur = set(unl)
        added = sorted(cur - prev) if prev is not None else []
        removed = sorted(prev - cur) if prev is not None else []
        membership.append(
            {
                "round": n,
                "vl_sequence": seq,
                "size": len(cur),
                "added": len(added),
                "removed": len(removed),
                "kept": len(cur & prev) if prev is not None else len(cur),
                "added_keys": " ".join(added),
                "removed_keys": " ".join(removed),
            }
        )
        prev = cur
    rounds_all = [n for n, _s, _u in hist]
    tenure = []
    keys = sorted({k for _n, _s, u in hist for k in u})
    scored = Counter()
    for _n, rd in ds["rounds"].items():
        for s in (rd["files"].get("outputs/final_scores.json") or {}).get("scores", []):
            scored[s.get("master_key")] += 1
    for k in keys:
        inr = [n for n, _s, u in hist if k in u]
        best = run = 0
        for n in rounds_all:
            run = run + 1 if n in inr else 0
            best = max(best, run)
        tenure.append(
            {
                "validator": k,
                "first_round": inr[0],
                "last_round": inr[-1],
                "rounds_in_unl": len(inr),
                "share_pct": f"{100 * len(inr) / len(rounds_all):.1f}",
                "longest_streak": best,
                "in_latest": int(k in hist[-1][2]),
                "rounds_scored": scored.get(k, 0),
            }
        )
    tenure.sort(key=lambda t: (-t["rounds_in_unl"], t["validator"]))
    scores = []
    for n, rd in sorted(ds["rounds"].items()):
        fs = rd["files"].get("outputs/final_scores.json")
        if not fs:
            continue
        unl = set(selected_unl(rd) or [])
        for s in sorted(fs.get("scores", []), key=lambda s: s.get("master_key", "")):
            scores.append(
                {
                    "round": n,
                    "validator": s.get("master_key"),
                    "final_score": s.get("final_score"),
                    "model_score": s.get("model_score"),
                    "diversity": s.get("diversity"),
                    "in_unl": int(s.get("master_key") in unl),
                }
            )
    entries = []
    for n in published(ds):
        rd = ds["rounds"][n]
        r = rd["record"]
        try:
            b = decoded(n, signed_vl(rd))["blobs"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        tx = ds["txs"].get(r.get("memo_tx_hash") or "")
        done = iso_to_unix(r["completed_at"]) if r.get("completed_at") else None
        eff_unix = b["effective"] + L.RIPPLE_EPOCH
        anchor_unix = tx["date"] + L.RIPPLE_EPOCH if tx_ok(tx) else None
        entries.append(
            {
                "round": n,
                "vl_sequence": b["sequence"],
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
                "anchor_close_time": ripple_to_iso(tx["date"]) if tx_ok(tx) else "",
                "anchor_ledger": tx.get("ledger_index", "") if tx_ok(tx) else "",
                "effective": ripple_to_iso(b["effective"]),
                "expiration": ripple_to_iso(b["expiration"]),
                "effective_minus_completed_h": f"{(eff_unix - done) / 3600:.2f}" if done else "",
                "effective_minus_anchor_h": f"{(eff_unix - anchor_unix) / 3600:.2f}" if anchor_unix else "",
                "validity_days": f"{(b['expiration'] - b['effective']) / 86400:.1f}",
            }
        )
    participation = []
    for n, rd in sorted(ds["rounds"].items()):
        c = rd["convergence"]
        if not c or not c.get("finalized"):
            continue
        rep = c.get("report") or {}
        s = rep.get("summary", {})
        lv = s.get("levels_matched", {})
        parts = rep.get("participants", [])
        unl = set(selected_unl(rd) or [])
        participation.append(
            {
                "round": n,
                "committers": s.get("committers"),
                "valid": s.get("outcomes", {}).get("valid", 0),
                "not_valid": sum(v for k, v in s.get("outcomes", {}).items() if k != "valid"),
                "raw_matched": lv.get("RAW", 0),
                "parsed_matched": lv.get("PARSED", 0),
                "selected_unl_matched": lv.get("SELECTED_UNL", 0),
                "divergent": sum(s.get("divergence_categories", {}).values()),
                "committers_in_unl": sum(1 for p in parts if p.get("validator_master_key") in unl),
                "unl_size": len(unl),
            }
        )
    return {
        "membership": membership,
        "tenure": tenure,
        "scores": scores,
        "entry_times": entries,
        "participation": participation,
    }


# ---------------------------------------------------------------- output

SURFACE, INK, INK2, GRID, SERIES = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", ("#2a78d6", "#eb6834")


def svg_bars(title, xs, series, ylabel, w=720, h=260):
    """Grouped bar chart (1-2 series) as a deterministic SVG string. series: [(name, values)]."""
    left, right, top, bottom = 48, 12, 34, 34
    pw, ph = w - left - right, h - top - bottom
    ymax = max([v for _n, vals in series for v in vals] + [1])
    step = 10 ** max(0, len(str(int(ymax))) - 1)
    ytop = math.ceil(ymax / step) * step
    if ytop / step < 3:
        step = max(1, step // 2) if step > 1 else 1
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'font-family="system-ui,sans-serif" font-size="11">',
        f'<rect width="{w}" height="{h}" fill="{SURFACE}"/>',
        f'<text x="{left}" y="18" font-size="13" fill="{INK}">{title}</text>',
    ]
    y = 0
    while y <= ytop:
        py = top + ph - ph * y / ytop
        out.append(f'<line x1="{left}" x2="{w - right}" y1="{py:.1f}" y2="{py:.1f}" stroke="{GRID}"/>')
        out.append(f'<text x="{left - 6}" y="{py + 4:.1f}" text-anchor="end" fill="{INK2}">{y}</text>')
        y += step
    slot = pw / max(1, len(xs))
    bw = max(2.0, (slot - 2) / len(series) - 2)
    for i, x in enumerate(xs):
        for j, (_name, vals) in enumerate(series):
            v = vals[i]
            bh = ph * v / ytop
            bx = left + i * slot + 1 + j * (bw + 2)
            out.append(
                f'<rect x="{bx:.1f}" y="{top + ph - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="1.5" '
                f'fill="{SERIES[j]}"><title>{_name} round {x}: {v}</title></rect>'
            )
        if len(xs) <= 40 and (i % 2 == 0 or len(xs) <= 20):
            out.append(
                f'<text x="{left + i * slot + slot / 2:.1f}" y="{h - bottom + 14}" text-anchor="middle" '
                f'fill="{INK2}">{x}</text>'
            )
    out.append(f'<text x="{left + pw / 2:.1f}" y="{h - 4}" text-anchor="middle" fill="{INK2}">round</text>')
    out.append(
        f'<text x="12" y="{top + ph / 2:.1f}" fill="{INK2}" transform="rotate(-90 12 {top + ph / 2:.1f})" '
        f'text-anchor="middle">{ylabel}</text>'
    )
    if len(series) > 1:
        lx = w - right - 200
        for j, (name, _v) in enumerate(series):
            out.append(f'<rect x="{lx + j * 100}" y="9" width="10" height="10" rx="2" fill="{SERIES[j]}"/>')
            out.append(f'<text x="{lx + j * 100 + 14}" y="18" fill="{INK2}">{name}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_csv(path, rows):
    buf = io.StringIO()
    if rows:
        wr = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())


def report_md(ds, checks, an, nodes, src_sha):
    cnt = Counter(c["status"] for c in checks)
    by = {}
    for c in checks:
        by.setdefault(c["check"], Counter())[c["status"]] += 1
    rounds = ds["rounds"]
    pub = published(ds)
    mem = an["membership"]
    ten = an["tenure"]
    part = an["participation"]
    ent = an["entry_times"]
    snaps = ", ".join(f"{k} {v.get('t')}" for k, v in sorted(nodes.items()))
    lines = [
        "# Dynamic UNL round history and consistency audit (Post Fiat testnet)",
        "",
        "Generated by `dunl/audit_dunl.py` from public data only: the scoring service API, the",
        "mirrored VL, public JSON-RPC transactions, and `validators` snapshots of this operator's own two",
        "nodes. Inputs are pinned below; the same inputs reproduce this file byte for byte.",
        "",
        f"- collection epoch: `{ds['epoch']}`; input pack sha256 `{src_sha}`",
        f"- rounds listed: {len(rounds)} (rounds {min(rounds)}..{max(rounds)}); with a signed VL: {len(pub)}",
        f"- transactions checked: {len(ds['txs'])}",
        f"- node snapshots: {snaps}",
        "",
        "## Check results",
        "",
        f"**{cnt.get(PASS, 0)} PASS, {cnt.get(WARN, 0)} WARN, {cnt.get(FAIL, 0)} FAIL** "
        f"({len(checks)} results; full list with reasons in `checks.json`).",
        "",
        "| check | PASS | WARN | FAIL |",
        "|---|---:|---:|---:|",
    ]
    for name in sorted(by):
        lines.append(
            f"| {name} | {by[name].get(PASS, 0)} | {by[name].get(WARN, 0)} | {by[name].get(FAIL, 0)} |"
        )
    lines += ["", "Every non-PASS result:", ""]
    nonpass = [c for c in checks if c["status"] != PASS]
    if not nonpass:
        lines.append("- none")
    for c in nonpass:
        lines.append(f"- **{c['status']}** `{c['check']}` {c['scope']}: {c['reason']}")
    total_added = sum(m["added"] for m in mem[1:])
    total_removed = sum(m["removed"] for m in mem[1:])
    changed = sum(1 for m in mem[1:] if m["added"] or m["removed"])
    sizes = [m["size"] for m in mem]
    lines += [
        "",
        "## Membership and churn",
        "",
        f"UNL size ranged {min(sizes)}..{max(sizes)} (latest {sizes[-1]}). Across {len(mem) - 1} transitions "
        f"the list changed {changed} times: {total_added} additions, {total_removed} removals "
        f"(`membership.csv`).",
        "",
        "![UNL size per round](charts/unl_size.svg)",
        "",
        "![Additions and removals per round](charts/churn.svg)",
        "",
        "## Tenure",
        "",
        f"{len(ten)} distinct validators were on at least one published list; "
        f"{sum(1 for t in ten if t['rounds_in_unl'] == len(mem))} were on every one, "
        f"{sum(1 for t in ten if t['rounds_in_unl'] == 1)} on exactly one (`tenure.csv`). Top 10 by rounds:",
        "",
        "| validator | rounds in UNL | share % | longest streak | first | last | in latest |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for t in ten[:10]:
        lines.append(
            f"| `{t['validator'][:16]}..` | {t['rounds_in_unl']} | {t['share_pct']} | "
            f"{t['longest_streak']} | "
            f"{t['first_round']} | {t['last_round']} | {t['in_latest']} |"
        )
    sc = an["scores"]
    if sc:
        last = max(s["round"] for s in sc)
        cur = [s for s in sc if s["round"] == last]
        inn = sorted(s["final_score"] for s in cur if s["in_unl"])
        out_ = sorted(s["final_score"] for s in cur if not s["in_unl"])
        cutoff = (ds["live"].get("config") or {}).get("unl_score_cutoff")
        lines += [
            "",
            "## Scores",
            "",
            f"`scores.csv` has {len(sc)} per-validator final scores over "
            f"{len({s['round'] for s in sc})} rounds with a final_scores.json. "
            f"Round {last}: {len(cur)} scored, "
            f"{len(inn)} selected (lowest selected score {inn[0] if inn else '-'}), highest unselected "
            f"{out_[-1] if out_ else '-'}; configured score cutoff {cutoff}. An alternate can outscore the "
            "lowest member because a challenger must beat the weakest incumbent by the min score gap; the "
            "`selection` check replays this rule for every round with published scores.",
        ]
    lines += [
        "",
        "## Entry times",
        "",
        "When each list became effective relative to round completion and to its on-chain anchor "
        "(`entry_times.csv`):",
        "",
        "| round | seq | completed | anchor close time | effective | effective - anchor (h) "
        "| validity (days) |",
        "|---:|---:|---|---|---|---:|---:|",
    ]
    for e in ent:
        lines.append(
            f"| {e['round']} | {e['vl_sequence']} | {e['completed_at'][:16] if e['completed_at'] else ''} | "
            f"{e['anchor_close_time'][:16]} | {e['effective'][:16]} | {e['effective_minus_anchor_h']} | "
            f"{e['validity_days']} |"
        )
    lines += ["", "## Participation (commit/reveal convergence)", ""]
    if part:
        lines += [
            f"{len(part)} sealed rounds have a convergence report (`participation.csv`). Committers ranged "
            f"{min(p['committers'] for p in part)}..{max(p['committers'] for p in part)}; "
            "divergent reveals in "
            f"total: {sum(p['divergent'] for p in part)}.",
            "",
            "![Committers per sealed round](charts/participation.svg)",
            "",
            "| round | committers | valid | RAW | PARSED | SELECTED_UNL | committers in UNL / UNL size |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for p in part:
            lines.append(
                f"| {p['round']} | {p['committers']} | {p['valid']} | {p['raw_matched']} | "
                f"{p['parsed_matched']} | "
                f"{p['selected_unl_matched']} | {p['committers_in_unl']} / {p['unl_size']} |"
            )
        lines += [
            "",
            "SELECTED_UNL below PARSED is diagnostic, not divergence: the scoring service compares only",
            "RAW and PARSED for acceptance (`acceptance_levels` in each report).",
        ]
    lines += [
        "",
        "## Own nodes",
        "",
        "| node | snapshot | trusted keys | quorum | publisher list seq |",
        "|---|---|---:|---:|---:|",
    ]
    for k, v in sorted(nodes.items()):
        seqs = ",".join(str(p.get("seq")) for p in v.get("publisher_lists", []))
        lines.append(
            f"| {k} | {v.get('t')} | {len(v.get('trusted_validator_keys', []))} | "
            f"{v.get('validation_quorum')} | {seqs} |"
        )
    lines += ["", "Reproduce: see `dunl/README.md`.", ""]
    return "\n".join(lines)


def write_outputs(out, ds, checks, an, nodes, src_sha):
    os.makedirs(os.path.join(out, "charts"), exist_ok=True)
    with open(os.path.join(out, "checks.json"), "w", encoding="utf-8") as fh:
        json.dump(checks, fh, indent=1, sort_keys=True)
        fh.write("\n")
    for name in ("membership", "tenure", "scores", "entry_times", "participation"):
        write_csv(os.path.join(out, f"{name}.csv"), an[name])
    mem = an["membership"]
    xs = [m["round"] for m in mem]
    charts = {
        "unl_size.svg": svg_bars(
            "UNL size per published round", xs, [("size", [m["size"] for m in mem])], "validators"
        ),
        "churn.svg": svg_bars(
            "UNL additions and removals per round",
            xs,
            [("added", [m["added"] for m in mem]), ("removed", [m["removed"] for m in mem])],
            "validators",
        ),
    }
    part = an["participation"]
    if part:
        charts["participation.svg"] = svg_bars(
            "Commit/reveal committers per sealed round",
            [p["round"] for p in part],
            [("committers", [p["committers"] for p in part])],
            "validators",
        )
    for name, svg in charts.items():
        with open(os.path.join(out, "charts", name), "w", encoding="utf-8") as fh:
            fh.write(svg)
    with open(os.path.join(out, "REPORT.md"), "w", encoding="utf-8") as fh:
        fh.write(report_md(ds, checks, an, nodes, src_sha))
    files = sorted(
        os.path.relpath(os.path.join(d, f), out)
        for d, _s, fs in os.walk(out)
        for f in fs
        if f != "SHA256SUMS"
    )
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        for f in files:
            with open(os.path.join(out, f), "rb") as g:
                fh.write(f"{hashlib.sha256(g.read()).hexdigest()}  {f}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="cache.jsonl or cache.jsonl.gz")
    ap.add_argument("--nodes", required=True, help="directory of nodes_dunl.sh snapshots")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    with open(args.cache, "rb") as fh:
        src_sha = hashlib.sha256(fh.read()).hexdigest()
    ds = dataset(load_rows(args.cache))
    nodes = load_nodes(args.nodes)
    checks = run_checks(ds, nodes)
    an = analyses(ds)
    write_outputs(args.out, ds, checks, an, nodes, src_sha)
    for c in checks:
        print(f"{c['status']} {c['check']} {c['scope']}: {c['reason']}")
    cnt = Counter(c["status"] for c in checks)
    print(
        f"SUMMARY rounds={len(ds['rounds'])} published={len(published(ds))} txs={len(ds['txs'])} "
        f"results={len(checks)} PASS={cnt.get(PASS, 0)} WARN={cnt.get(WARN, 0)} FAIL={cnt.get(FAIL, 0)}"
    )
    return 2 if cnt.get(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
