"""Hand-computed census, sign, reconciliation and diff tests for issued/analyze_issued.py."""

import copy
import filecmp
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "issued"))

import analyze_issued as A  # noqa: E402
import fixture_issued as F  # noqa: E402


@pytest.fixture
def snap(tmp_path):
    F.make_snapshot(tmp_path / "A")
    return A.load_snapshot(str(tmp_path / "A"))


def by_key(cen):
    return {(c["issuer"], c["currency"]): c for c in cen}


def test_two_sided_signs():
    ls = {ln["index"][0]: ln for ln in F.lines()}
    # L1 Balance +100: low (H1) holds, high (G1) issues
    assert A.classify(ls["1"])[:3] == ("rG1", "rH1", A.Decimal("100"))
    # L2 Balance -300: high (H2) holds, low (G1) issues
    assert A.classify(ls["2"])[:3] == ("rG1", "rH2", A.Decimal("300"))
    # the same line seen from both sides
    assert A.view(ls["2"], "rH2")["balance"] == "300"
    assert A.view(ls["2"], "rG1")["balance"] == "-300"
    assert A.view(ls["1"], "rH1")["balance"] == "100"
    assert A.view(ls["1"], "rG1")["balance"] == "-100"
    assert A.view(ls["2"], "rH2")["limit"] == "200"
    assert A.view(ls["2"], "rH2")["limit_peer"] == "0"


def test_zero_and_ambiguous_lines():
    ls = {ln["index"][0]: ln for ln in F.lines()}
    assert A.classify(ls["3"]) == ("rG1", "rH3", A.Decimal(0), False)  # only H3 has a limit
    assert A.classify(ls["6"]) == (A.AMBIGUOUS, None, A.Decimal(0), True)


def test_freeze_and_noripple_flags_per_side():
    ls = {ln["index"][0]: ln for ln in F.lines()}
    v = A.view(ls["2"], "rH2")
    assert (v["freeze"], v["freeze_peer"], v["no_ripple"], v["no_ripple_peer"]) == (False, True, False, True)
    v = A.view(ls["5"], "rH4")  # holder froze its own (low) side
    assert (v["freeze"], v["freeze_peer"], v["no_ripple"], v["no_ripple_peer"]) == (True, False, False, True)


def test_census_hand_computed(snap):
    c = by_key(A.census(snap))
    g1 = c[("rG1", "USD")]
    assert (g1["lines"], g1["holders"], g1["obligations"]) == (3, 2, "400")
    assert g1["top1_share"] == "0.750000"  # 300 / 400
    assert g1["top10_share"] == "1.000000"
    assert g1["hhi"] == "6250.00"  # (0.75^2 + 0.25^2) * 10000
    assert g1["zero_lines"] == 1
    assert g1["over_limit"] == [{"holder": "rH2", "amount": "300", "limit": "200"}]
    assert (g1["issuer_froze"], g1["holder_froze"]) == (1, 0)
    assert (g1["issuer_side_no_ripple"], g1["holder_side_no_ripple"]) == (2, 0)
    assert [h["holder"] for h in g1["top_holders"]] == ["rH2", "rH1"]


def test_shared_currency_code_kept_apart(snap):
    c = by_key(A.census(snap))
    assert c[("rG2", "USD")]["obligations"] == "40.5"
    assert c[("rG2", "USD")]["hhi"] == "10000.00"
    assert c[("rG1", "USD")]["obligations"] == "400"


def test_hex_currency_and_holder_freeze(snap):
    c = by_key(A.census(snap))
    e = c[("rG2", F.EURO)]
    assert e["currency_name"] == "EURO"
    assert (e["obligations"], e["holder_froze"], e["issuer_froze"], e["issuer_side_no_ripple"]) == (
        "7.25",
        1,
        0,
        1,
    )
    amb = c[(A.AMBIGUOUS, "USD")]
    assert (amb["lines"], amb["zero_lines"], amb["holders"]) == (1, 1, 0)


def test_currency_names():
    assert A.currency_name("USD") == "USD"
    assert A.currency_name(F.EURO) == "EURO"
    assert A.currency_name("03" + "AB" * 19).startswith("LP:")
    raw = "00" * 12 + "FF" * 8
    assert A.currency_name(raw) == raw


def test_rippling_through(snap):
    # H1: L1 low, L4 high, L6 low: own NoRipple clear on all three USD lines; H2: L2 high, L6 high
    assert A.rippling(snap) == [
        {"account": "rH1", "currency": "USD", "rippling_lines": 3},
        {"account": "rH2", "currency": "USD", "rippling_lines": 2},
    ]


def test_issuer_settings(snap):
    s = A.issuer_settings(snap["accounts"]["rG2"])
    assert s["flags"] == ["RequireAuth", "DefaultRipple"]
    assert s["transfer_fee_pct"] == "0.200000"
    assert s["domain_text"] == "example.org"


def test_gateway_reconciliation_exact(snap):
    # G1: L1 obligation 100, L2 frozen by G1 -> frozen_balances, not obligations
    assert A.expected_gateway(snap, "rG1") == {
        "obligations": {"USD": "100"},
        "frozen_balances": {"rH2": [{"currency": "USD", "value": "300"}]},
    }
    rows, mism = A.reconcile(snap, A.census(snap))
    assert mism == []
    assert len(rows) == 6 and all(r["match"] for r in rows)


def test_gateway_mismatch_listed(tmp_path):
    gw = copy.deepcopy(F.GATEWAY)
    gw["rG2"]["obligations"]["USD"] = "40.50000000000001"
    F.make_snapshot(tmp_path / "A", F.FakeNode(gateway=gw))
    snap = A.load_snapshot(str(tmp_path / "A"))
    _, mism = A.reconcile(snap, A.census(snap))
    checks = sorted((m["check"], m.get("account", m.get("issuer"))) for m in mism)
    assert checks == [("census_obligations", "rG2"), ("gateway_balances", "rG2")]


def test_gateway_missing_frozen_is_mismatch(tmp_path):
    gw = copy.deepcopy(F.GATEWAY)
    del gw["rG1"]["frozen_balances"]
    F.make_snapshot(tmp_path / "A", F.FakeNode(gateway=gw))
    snap = A.load_snapshot(str(tmp_path / "A"))
    _, mism = A.reconcile(snap, A.census(snap))
    assert {m["check"] for m in mism} == {"gateway_balances", "census_obligations"}


def test_amm_none_is_query_backed(snap):
    m = A.amm_findings(snap)
    assert m["result"] == "none (query-backed)"
    assert (m["amm_info_queries"], m["amm_info_found"], m["amm_info_errors"]) == (3, 0, ["actNotFound"])


def test_manifest_tamper_refused(tmp_path):
    F.make_snapshot(tmp_path / "A")
    p = tmp_path / "A" / "ripplestate.jsonl"
    p.write_text(p.read_text().replace('"100"', '"101"'))
    with pytest.raises(A.BadInput):
        A.load_snapshot(str(tmp_path / "A"))
    assert A.main(["report", "--a", str(tmp_path / "A"), "--out", str(tmp_path / "r")]) == 3


def b_objects():
    objs = [o for o in F.objects() if o["index"] != "3" * 64]
    for o in objs:
        if o["index"] == "1" * 64:
            o["Balance"]["value"] = "150"
        if o.get("Account") == "rG1":
            o["TransferRate"] = 1005000000
    objs.append(F.line("7" * 64, "USD", "rH4", "rG1", "25", "30", "0"))
    return sorted(objs, key=lambda o: o["index"])


B_GATEWAY = {
    **F.GATEWAY,
    "rG1": {"obligations": {"USD": "175"}, "frozen_balances": {"rH2": [{"currency": "USD", "value": "300"}]}},
    "rH1": {
        "assets": {
            "rG1": [{"currency": "USD", "value": "150"}],
            "rG2": [{"currency": "USD", "value": "40.5"}],
        }
    },
    "rH4": {
        "assets": {
            "rG1": [{"currency": "USD", "value": "25"}],
            "rG2": [{"currency": F.EURO, "value": "7.25"}],
        }
    },
}


def test_diff_hand_computed(tmp_path):
    F.make_snapshot(tmp_path / "A")
    nodeb = F.FakeNode(objs=b_objects(), gateway=B_GATEWAY)
    orig = F.LEDGER_INDEX
    F.make_snapshot(tmp_path / "B", nodeb)
    a, b = A.load_snapshot(str(tmp_path / "A")), A.load_snapshot(str(tmp_path / "B"))
    b["header"]["ledger_index"] = orig + 1
    b["header"]["close_time"] += 86400
    d = A.diff(a, b)
    assert [x["index"] for x in d["lines_added"]] == ["7" * 64]
    assert [x["index"] for x in d["lines_removed"]] == ["3" * 64]
    assert d["lines_changed"] == [
        {
            "index": "1" * 64,
            "fields": {
                "Balance": {
                    "a": F.amt("USD", "rrrrrrrrrrrrrrrrrrrrBZbvji", "100"),
                    "b": F.amt("USD", "rrrrrrrrrrrrrrrrrrrrBZbvji", "150"),
                }
            },
        }
    ]
    g1 = [x for x in d["assets"] if (x["issuer"], x["currency"]) == ("rG1", "USD")][0]
    # A: 100 + 300 = 400; B: 150 + 300 + 25 = 475; holders 2 -> 3; lines 3 -> 3 (L3 gone, L7 new)
    assert g1["obligations"] == {"a": "400", "b": "475", "delta": "75"}
    assert g1["holders"] == {"a": "2", "b": "3", "delta": "1"}
    assert g1["lines"] == {"a": "3", "b": "3", "delta": "0"}
    assert d["issuer_settings_changed"] == [
        {"account": "rG1", "fields": {"TransferRate": {"a": None, "b": 1005000000}}}
    ]
    assert d["window_hours"] == "24.00"
    rows, mism = A.reconcile(b, A.census(b))
    assert mism == []


def test_b_not_after_a_refused(tmp_path):
    F.make_snapshot(tmp_path / "A")
    F.make_snapshot(tmp_path / "B")
    assert (
        A.main(
            ["report", "--a", str(tmp_path / "A"), "--b", str(tmp_path / "B"), "--out", str(tmp_path / "r")]
        )
        == 3
    )


def test_report_deterministic_and_exit_codes(tmp_path):
    F.make_snapshot(tmp_path / "A")
    assert A.main(["report", "--a", str(tmp_path / "A"), "--out", str(tmp_path / "r1")]) == 0
    assert A.main(["report", "--a", str(tmp_path / "A"), "--out", str(tmp_path / "r2")]) == 0
    for n in ("report.json", "REPORT.md"):
        assert filecmp.cmp(tmp_path / "r1" / n, tmp_path / "r2" / n, shallow=False)
    doc = json.loads((tmp_path / "r1" / "report.json").read_text())
    assert doc["A"]["mismatches"] == []
    gw = copy.deepcopy(F.GATEWAY)
    gw["rH3"] = {"assets": {"rG1": [{"currency": "USD", "value": "1"}]}}
    F.make_snapshot(tmp_path / "X", F.FakeNode(gateway=gw))
    assert A.main(["report", "--a", str(tmp_path / "X"), "--out", str(tmp_path / "r3")]) == 1
    assert "Mismatches: 1." in (tmp_path / "r3" / "REPORT.md").read_text()


def test_zero_line_high_side_limit_is_holder():
    ln = F.line("A" * 64, "USD", "rG1", "rH9", "0", "0", "25")
    assert A.classify(ln) == ("rG1", "rH9", A.Decimal(0), False)


def test_balance_equal_to_limit_is_not_over_limit(tmp_path):
    objs = F.objects()
    for o in objs:
        if o["index"] == "1" * 64:
            o["LowLimit"]["value"] = "100"  # H1 holds exactly its limit
    F.make_snapshot(tmp_path / "A", F.FakeNode(objs=objs))
    c = by_key(A.census(A.load_snapshot(str(tmp_path / "A"))))
    assert [r["holder"] for r in c[("rG1", "USD")]["over_limit"]] == ["rH2"]


def test_object_count_disagreement_listed(snap):
    snap["counts"]["filtered"]["AMM"]["index_sets_equal"] = False
    _, mism = A.reconcile(snap, A.census(snap))
    assert [(m["check"], m["type"]) for m in mism] == [("object_count", "AMM")]
    snap["counts"]["filtered"]["AMM"]["index_sets_equal"] = True
    snap["counts"]["filtered"]["RippleState"]["type_filter"] = 5
    _, mism = A.reconcile(snap, A.census(snap))
    assert [(m["check"], m["type"]) for m in mism] == [("object_count", "RippleState")]


def test_lp_token_line_means_pools_present(tmp_path):
    lp = "03" + "AB" * 19
    objs = F.objects() + [F.line("B" * 64, lp, "rH1", "rG1", "5", "0", "0")]
    objs.sort(key=lambda o: o["index"])
    gw = copy.deepcopy(F.GATEWAY)
    gw["rG1"]["obligations"][lp] = "5"
    gw["rH1"]["assets"]["rG1"].append({"currency": lp, "value": "5"})
    F.make_snapshot(tmp_path / "A", F.FakeNode(objs=objs, gateway=gw))
    snap = A.load_snapshot(str(tmp_path / "A"))
    m = A.amm_findings(snap)
    assert (m["lp_token_lines"], m["result"]) == (1, "pools present")
    assert A.reconcile(snap, A.census(snap))[1] == []
