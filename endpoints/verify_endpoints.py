#!/usr/bin/env python3
"""Offline verifier and report for endpoints/ (Python 3 stdlib only, no network).

Reads the committed pack (collect_endpoints.py pack) and, per published COMPLETE scoring round:
  * checks every fetched file's content hash against the bundle's published file_hashes,
  * recomputes the provider-family / country / unresolved counts from the round's own validator evidence
    and compares them with the NETWORK CONCENTRATION block of inputs/model_request.json:
    match | mismatch | block_missing,
  * checks each prompt row's provider_family/country against the evidence (prompt rows are sorted by
    master key) and, where published, inputs/diversity_inputs.json against the recount,
  * measures endpoint visibility (resolved / unresolved, key-prefix changes from the previous round),
    family and country concentration (top entries, HHI),
  * tests the validator guide's claim that unresolved validators land at the bottom of the diversity
    range, below even the most crowded visible provider,
and cross-checks round 33's domain_verified against valdomain/report/results.json (VL sequence 31).

Writes results.json, rounds.csv, REPORT.md and SHA256SUMS to --out; same inputs give byte-identical files.
Exit: 0 all checks ran (whatever they found), 1 internal inconsistency in the inputs, 2 no data.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_endpoints as L  # noqa: E402

GUIDE = (
    "postfiatorg/postfiatorg.github.io@8da236f68fb1dbc050fe6463c427f6e0bb5d5d1c "
    "content/validator-setup.md line 580"
)
CROSSCHECK_ROUND = 33
PREFIX = 8
_HEX64 = re.compile(r"[0-9a-f]{64}")


class Inconsistent(Exception):
    pass


def load_pack(path):
    if not os.path.exists(path):
        return None, None
    with open(path, "rb") as fh:
        raw = fh.read()
    text = gzip.decompress(raw).decode("utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return rows, text


def self_check(rows, text):
    """Raise Inconsistent if the pack contradicts itself; returns (meta, rounds)."""
    if not rows or rows[0].get("kind") != "meta":
        raise Inconsistent("first row is not the meta row")
    meta, rounds = rows[0], rows[1:]
    if any(r.get("kind") != "round" for r in rounds):
        raise Inconsistent("non-round row after meta")
    nums = [r["round"] for r in rounds]
    if len(set(nums)) != len(nums):
        raise Inconsistent(f"duplicate round rows: {sorted(n for n, c in Counter(nums).items() if c > 1)}")
    if sorted(nums) != sorted(meta.get("complete_rounds") or []):
        raise Inconsistent("round rows differ from the meta's COMPLETE round list")
    if any(r.get("status") != "COMPLETE" for r in rounds):
        raise Inconsistent("a packed round is not COMPLETE")
    ips = L.find_ips(text)
    if ips:
        raise Inconsistent(f"{len(ips)} IP address(es) in the pack")
    for r in rounds:
        for f, rec in sorted((r.get("files") or {}).items()):
            if rec.get("http") == 200 and not (
                _HEX64.fullmatch(rec.get("raw_sha256") or "")
                and _HEX64.fullmatch(rec.get("content_sha256") or "")
            ):
                raise Inconsistent(f"round {r['round']} {f}: malformed hash record")
        vals = r.get("validators")
        if vals is None:
            continue
        keys = [v["master_key"] for v in vals]
        if keys != sorted(set(keys)):
            raise Inconsistent(f"round {r['round']}: validator rows not sorted/unique")
        for v in vals:
            want = "unresolved" if L.family_for(v.get("as_name")) == L.UNKNOWN else "resolved"
            if v.get("endpoint") != want:
                raise Inconsistent(
                    f"round {r['round']} {v['master_key'][:PREFIX]}: endpoint field != ASN record"
                )
    return meta, sorted(rounds, key=lambda r: r["round"])


def check_files(r):
    bad = []
    for f, rec in sorted((r.get("files") or {}).items()):
        if rec.get("http") != 200:
            bad.append(f"{f}: http {rec.get('http')}")
        elif rec.get("content_sha256") != rec.get("published_sha256"):
            bad.append(f"{f}: content hash != published")
    return {"files": len(r.get("files") or {}), "bad": bad}


def classify_block(r, recount):
    block = r.get("block")
    if block is None:
        why = (
            "no frozen inputs (no validator evidence)"
            if r.get("validators") is None
            else "prompt has no block"
        )
        return {"class": "block_missing", "why": why}
    if recount is None:
        return {"class": "mismatch", "why": "block present but no validator evidence to recount"}
    fam = L.diff_counts(recount["provider_families"], block["provider_families"], "family")
    cty = L.diff_counts(recount["countries"], block["countries"], "country")
    unres = (recount["unresolved_endpoints"], block["unresolved_endpoints"])
    order = recount == block
    if not fam and not cty and unres[0] == unres[1]:
        return {
            "class": "match" if order else "mismatch",
            "why": "" if order else "same counts, different order",
        }
    return {
        "class": "mismatch",
        "families": [list(x) for x in fam],
        "countries": [list(x) for x in cty],
        "unresolved": list(unres),
    }


def check_prompt(r):
    vals, prompt = r.get("validators"), r.get("prompt")
    if vals is None or prompt is None:
        return None
    if len(vals) != len(prompt):
        return {"rows": len(prompt), "evidence": len(vals), "disagree": None}
    bad = 0
    with_family = any(p.get("provider_family") is not None for p in prompt)
    for v, p in zip(vals, prompt, strict=True):
        if with_family and L.family_for(v.get("as_name")) != p.get("provider_family"):
            bad += 1
        elif v.get("country") != p.get("country"):
            bad += 1
    return {"rows": len(prompt), "evidence": len(vals), "family_field": with_family, "disagree": bad}


def check_diversity_inputs(r, recount):
    di, vals = r.get("diversity_inputs"), r.get("validators")
    if di is None or vals is None:
        return None
    fam = L.as_table(recount["provider_families"], "family")
    cty = L.as_table(recount["countries"], "country")
    want = {
        v["master_key"]: (
            cty.get(v["country"]) if v.get("country") else None,
            fam.get(L.family_for(v.get("as_name"))) if v["endpoint"] == "resolved" else None,
        )
        for v in vals
    }
    got = {
        d["master_key"]: (d.get("country_validators"), d.get("provider_validators")) for d in di["validators"]
    }
    bad = sorted(k[:PREFIX] for k in set(want) | set(got) if want.get(k) != got.get(k))
    ok_total = di.get("resolved_endpoints") == len(vals) - recount["unresolved_endpoints"]
    return {"entries": len(got), "disagree": bad, "resolved_endpoints_ok": ok_total}


def rank_test(r, recount):
    vals = r.get("validators")
    if vals is None or not any(v.get("diversity") is not None for v in vals):
        return None
    unres = [v for v in vals if v["endpoint"] == "unresolved" and v.get("diversity") is not None]
    res = [v for v in vals if v["endpoint"] == "resolved" and v.get("diversity") is not None]
    if not unres or not res or not recount["provider_families"]:
        return {"applicable": False, "unresolved": len(unres), "resolved": len(res)}
    top = recount["provider_families"][0]
    top_d = [v["diversity"] for v in res if L.family_for(v.get("as_name")) == top["family"]]
    u_max, r_min, t_min = max(v["diversity"] for v in unres), min(v["diversity"] for v in res), min(top_d)
    at_or_above = sorted(v["master_key"][:PREFIX] for v in unres if v["diversity"] >= t_min)
    return {
        "applicable": True,
        "unresolved": len(unres),
        "resolved": len(res),
        "unresolved_max": u_max,
        "resolved_min": r_min,
        "top_family": top["family"],
        "top_family_validators": top["validators"],
        "top_family_min": t_min,
        "below_all_resolved": u_max < r_min,
        "below_top_family": u_max < t_min,
        "unresolved_at_or_above_top_min": at_or_above,
    }


def visibility(r, prev):
    vals = r.get("validators")
    if vals is None:
        return None
    unres = {v["master_key"] for v in vals if v["endpoint"] == "unresolved"}
    out = {
        "validators": len(vals),
        "resolved": len(vals) - len(unres),
        "unresolved": len(unres),
        "unresolved_address_seen": sum(
            1 for v in vals if v["endpoint"] == "unresolved" and v["address_seen"]
        ),
    }
    if prev is not None:
        pk = {v["master_key"]: v["endpoint"] for v in prev}
        cur = {v["master_key"]: v["endpoint"] for v in vals}

        def pick(cond):
            return sorted(k[:PREFIX] for k in cur if cond(k))

        out["became_unresolved"] = pick(lambda k: pk.get(k) == "resolved" and cur[k] == "unresolved")
        out["became_resolved"] = pick(lambda k: pk.get(k) == "unresolved" and cur[k] == "resolved")
        out["new_unresolved"] = pick(lambda k: k not in pk and cur[k] == "unresolved")
        out["left_set"] = sorted(k[:PREFIX] for k in pk if k not in cur)
    return out


def concentration_stats(recount):
    fam = L.as_table(recount["provider_families"], "family")
    cty = L.as_table(recount["countries"], "country")
    return {
        "top_families": recount["provider_families"][:5],
        "top_countries": recount["countries"][:5],
        "families": len(fam),
        "countries": len(cty),
        "hhi_family": L.hhi(fam),
        "hhi_country": L.hhi(cty),
    }


def crosscheck_valdomain(rounds, path):
    r = next((x for x in rounds if x["round"] == CROSSCHECK_ROUND), None)
    if r is None or r.get("validators") is None or not path or not os.path.exists(path):
        return None
    with open(path) as fh:
        vd = json.load(fh)
    ev = {v["master_key"]: v for v in r["validators"]}
    rows, counts = [], Counter()
    for x in sorted(vd["rows"], key=lambda x: x["master"]):
        v = ev.get(x["master"])
        if v is None:
            res = "not_in_round"
        else:
            same_domain = (v.get("domain") or None) == (x.get("domain") or None)
            agree = bool(v.get("domain_verified")) == (x["class"] == "verified")
            res = "agree" if agree and same_domain else ("domain_differs" if agree else "disagree")
        counts[res] += 1
        rows.append(
            {
                "key": x["master"][:PREFIX],
                "valdomain": x["class"],
                "domain_verified": None if v is None else v.get("domain_verified"),
                "result": res,
            }
        )
    return {
        "round": CROSSCHECK_ROUND,
        "vl_sequence": r.get("vl_sequence"),
        "valdomain_validators": len(vd["rows"]),
        "counts": dict(sorted(counts.items())),
        "rows": rows,
    }


def analyse(meta, rounds, valdomain):
    out, prev = [], None
    for r in rounds:
        vals = r.get("validators")
        recount = L.concentration(vals) if vals is not None else None
        res = {
            "round": r["round"],
            "vl_sequence": r.get("vl_sequence"),
            "prompt_version": r.get("prompt_version"),
            "files": check_files(r),
            "block": classify_block(r, recount),
            "prompt": check_prompt(r),
            "visibility": visibility(r, prev),
        }
        if recount is not None:
            res["diversity_inputs"] = check_diversity_inputs(r, recount)
            res["concentration"] = concentration_stats(recount)
            res["rank"] = rank_test(r, recount)
            prev = vals
        out.append(res)
    return {
        "source": meta.get("source"),
        "epoch": meta.get("epoch"),
        "rounds_listed": meta.get("rounds_listed"),
        "complete_rounds": len(rounds),
        "rounds": out,
        "valdomain_crosscheck": crosscheck_valdomain(rounds, valdomain),
    }


def _yn(b):
    return "yes" if b else "no"


def _keys(keys):
    return ", ".join(keys) if keys else "-"


def render(res):
    rs = res["rounds"]
    classes = Counter(x["block"]["class"] for x in rs)
    hash_bad = [x["round"] for x in rs if x["files"]["bad"]]
    ranked = [x for x in rs if x.get("rank") and x["rank"].get("applicable")]
    fail_top = [x for x in ranked if not x["rank"]["below_top_family"]]
    fail_all = [x for x in ranked if not x["rank"]["below_all_resolved"]]
    with_block = [x["round"] for x in rs if x["block"]["class"] != "block_missing"]
    o = io.StringIO()
    w = o.write

    def row(*cells):
        w("| " + " | ".join(str(c) for c in cells) + " |\n")

    w("# endpoints: Dynamic UNL endpoint visibility and concentration inputs\n\n")
    w("Generated by `verify_endpoints.py --offline` from the committed pack ")
    w(f"(round list epoch {res['epoch']}, {res['rounds_listed']} rounds listed, ")
    w(f"{res['complete_rounds']} COMPLETE, all processed).\n\n")
    w("## Summary\n\n")
    w(f"- NETWORK CONCENTRATION block: {classes.get('match', 0)} match, ")
    w(f"{classes.get('mismatch', 0)} mismatch, {classes.get('block_missing', 0)} block_missing")
    if with_block:
        w(f" (the block is rendered from round {min(with_block)} on)")
    w(".\n")
    w(f"- Published file hashes: {sum(x['files']['files'] for x in rs)} files checked, ")
    w(f"{'all match' if not hash_bad else 'mismatch in rounds ' + ', '.join(map(str, hash_bad))}.\n")
    w(f"- Guide claim ({GUIDE}): unresolved validators below the most crowded visible provider in ")
    w(f"{len(ranked) - len(fail_top)} of {len(ranked)} scored rounds; below every resolved validator in ")
    w(f"{len(ranked) - len(fail_all)} of {len(ranked)}.\n")
    cc = res.get("valdomain_crosscheck")
    if cc:
        w(f"- Round {cc['round']} (VL sequence {cc['vl_sequence']}) `domain_verified` vs valdomain: ")
        w(", ".join(f"{k} {v}" for k, v in cc["counts"].items()) + f" of {cc['valdomain_validators']}.\n")
    w("\n## Per round: block, visibility, concentration\n\n")
    row(
        "round",
        "VL seq",
        "prompt",
        "hashes",
        "block",
        "prompt rows",
        "div. inputs",
        "validators",
        "resolved",
        "unresolved (addr seen)",
        "top family",
        "top country",
        "HHI fam",
        "HHI cty",
    )
    row(
        "---:",
        "---:",
        "---",
        "---",
        "---",
        "---",
        "---",
        "---:",
        "---:",
        "---:",
        "---",
        "---",
        "---:",
        "---:",
    )
    for x in rs:
        v, c, p, d = x.get("visibility"), x.get("concentration"), x.get("prompt"), x.get("diversity_inputs")
        head = (
            x["round"],
            x["vl_sequence"],
            x["prompt_version"] or "-",
            "ok" if not x["files"]["bad"] else "BAD",
            x["block"]["class"],
        )
        if v is None:
            row(*head, *["-"] * 9)
            continue
        prow = "-" if p is None else ("ok" if p["disagree"] == 0 else f"{p['disagree']} differ")
        drow = "-" if d is None else ("ok" if not d["disagree"] and d["resolved_endpoints_ok"] else "differ")
        tf, tc = c["top_families"][0], c["top_countries"][0]
        row(
            *head,
            prow,
            drow,
            v["validators"],
            v["resolved"],
            f"{v['unresolved']} ({v['unresolved_address_seen']})",
            f"{tf['family']} {tf['validators']}",
            f"{tc['country']} {tc['validators']}",
            c["hhi_family"],
            c["hhi_country"],
        )
    mism = [x for x in rs if x["block"]["class"] == "mismatch"]
    w("\n## Block mismatches\n\n")
    if not mism:
        w("None: in every round that renders the block, the recount equals it exactly (families, countries, ")
        w("unresolved count, order).\n")
    for x in mism:
        b = x["block"]
        w(f"- round {x['round']}: families {b.get('families')}, countries {b.get('countries')}, ")
        w(f"unresolved {b.get('unresolved')} {b.get('why', '')}\n")
    w("\n## Endpoint visibility changes (master-key prefixes)\n\n")
    row("round", "became unresolved", "became resolved", "new and unresolved", "left the set")
    row("---:", "---", "---", "---", "---")
    for x in rs:
        v = x.get("visibility")
        if v and "became_unresolved" in v:
            row(
                x["round"],
                _keys(v["became_unresolved"]),
                _keys(v["became_resolved"]),
                _keys(v["new_unresolved"]),
                _keys(v["left_set"]),
            )
    w("\n## Guide claim: unresolved validators rank at the bottom of diversity\n\n")
    w(f'Claim ({GUIDE}): "in published rounds so far, unresolved validators have landed at the bottom ')
    w('of the diversity range, below even the most crowded visible providers." Test per round with ')
    w("published diversity sub-scores (`outputs/validator_scores.json`): highest unresolved score vs the ")
    w("lowest score in the most crowded family (A) and vs the lowest resolved score (B). Ties count as not ")
    w("below.\n\n")
    row(
        "round",
        "prompt",
        "unresolved",
        "max unresolved",
        "top family (n)",
        "min top family",
        "A below top",
        "min resolved",
        "B below all",
        "unresolved >= top min",
    )
    row("---:", "---", "---:", "---:", "---", "---:", "---", "---:", "---", "---")
    for x in ranked:
        k = x["rank"]
        row(
            x["round"],
            x["prompt_version"] or "-",
            k["unresolved"],
            k["unresolved_max"],
            f"{k['top_family']} ({k['top_family_validators']})",
            k["top_family_min"],
            _yn(k["below_top_family"]),
            k["resolved_min"],
            _yn(k["below_all_resolved"]),
            _keys(k["unresolved_at_or_above_top_min"]),
        )
    w(f"\nA holds in {len(ranked) - len(fail_top)} of {len(ranked)} rounds")
    if fail_top:
        w("; it fails in rounds " + ", ".join(str(x["round"]) for x in fail_top))
    w(f". B holds in {len(ranked) - len(fail_all)} of {len(ranked)}.\n")
    if cc:
        w(f"\n## Round {cc['round']} `domain_verified` vs valdomain (VL sequence {cc['vl_sequence']})\n\n")
        row("key", "valdomain class", "round domain_verified", "result")
        row("---", "---", "---", "---")
        for r in cc["rows"]:
            row(r["key"], r["valdomain"], r["domain_verified"], r["result"])
    return o.getvalue()


def write_csv(res):
    o = io.StringIO()
    cw = csv.writer(o, lineterminator="\n")
    cw.writerow(
        [
            "round",
            "vl_sequence",
            "prompt_version",
            "block_class",
            "validators",
            "resolved",
            "unresolved",
            "unresolved_address_seen",
            "families",
            "countries",
            "hhi_family",
            "hhi_country",
            "top_families",
            "top_countries",
        ]
    )
    for x in res["rounds"]:
        v, c = x.get("visibility") or {}, x.get("concentration") or {}
        cw.writerow(
            [
                x["round"],
                x["vl_sequence"],
                x["prompt_version"] or "",
                x["block"]["class"],
                v.get("validators", ""),
                v.get("resolved", ""),
                v.get("unresolved", ""),
                v.get("unresolved_address_seen", ""),
                c.get("families", ""),
                c.get("countries", ""),
                c.get("hhi_family", ""),
                c.get("hhi_country", ""),
                ";".join(f"{e['family']}={e['validators']}" for e in c.get("top_families", [])),
                ";".join(f"{e['country']}={e['validators']}" for e in c.get("top_countries", [])),
            ]
        )
    return o.getvalue()


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true", required=True, help="never touches the network")
    ap.add_argument("--pack", default=os.path.join(here, "data", "pack.jsonl.gz"))
    ap.add_argument(
        "--valdomain", default=os.path.join(os.path.dirname(here), "valdomain", "report", "results.json")
    )
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows, text = load_pack(a.pack)
    if not rows or len(rows) < 2:
        print(f"NO DATA: {a.pack} missing or holds no rounds")
        return 2
    try:
        meta, rounds = self_check(rows, text)
    except Inconsistent as e:
        print(f"INCONSISTENT: {e}")
        return 1
    res = analyse(meta, rounds, a.valdomain)
    files = {
        "results.json": json.dumps(res, sort_keys=True, indent=1, ensure_ascii=False) + "\n",
        "rounds.csv": write_csv(res),
        "REPORT.md": render(res),
    }
    os.makedirs(a.out, exist_ok=True)
    sums = []
    for name, body in sorted(files.items()):
        data = body.encode("utf-8")
        with open(os.path.join(a.out, name), "wb") as fh:
            fh.write(data)
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
    with open(os.path.join(a.out, "SHA256SUMS"), "w") as fh:
        fh.writelines(sums)
    classes = Counter(x["block"]["class"] for x in res["rounds"])
    print(f"pack sha256 {hashlib.sha256(open(a.pack, 'rb').read()).hexdigest()}")
    for x in res["rounds"]:
        k = x.get("rank") or {}
        print(
            f"round {x['round']}: block={x['block']['class']} "
            f"hashes={'ok' if not x['files']['bad'] else 'BAD'} "
            f"unresolved={(x.get('visibility') or {}).get('unresolved', '-')} "
            f"rankA={_yn(k['below_top_family']) if k.get('applicable') else '-'}"
        )
    print(f"RESULT rounds={len(res['rounds'])} " + " ".join(f"{k}={v}" for k, v in sorted(classes.items())))
    for line in sums:
        print(line, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
