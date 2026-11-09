"""analyze_objlife.py: hand-computed census fixture, A->B replay, isolated reconciliation failures."""

import copy
import csv
import json
import os
import sys
from fractions import Fraction

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "objlife"))

import analyze_objlife as A  # noqa: E402

CT = 800_000_000  # snapshot A close time (ripple epoch)
X, Y, Z, W, ISS = "rXowner", "rYowner", "rZowner", "rWdest", "rIssuer"
TYPES = ["Check", "Credential", "DepositPreauth", "Escrow", "NFTokenOffer", "Offer", "PayChannel", "Ticket"]
USD = lambda v: {"currency": "USD", "issuer": ISS, "value": v}  # noqa: E731


def idx(n):
    return f"{n:02X}" * 32


H1, H2, H3 = "A1" * 32, "A2" * 32, "A3" * 32

E1 = {
    "index": idx(1),
    "LedgerEntryType": "Escrow",
    "Account": X,
    "Amount": "1000",
    "Destination": W,
    "CancelAfter": CT + 100,
    "FinishAfter": CT - 10,
    "Flags": 0,
    "PreviousTxnID": "B1" * 32,
    "PreviousTxnLgrSeq": 99,
}
O1 = {
    "index": idx(2),
    "LedgerEntryType": "Offer",
    "Account": X,
    "TakerGets": "500",
    "TakerPays": USD("1.5"),
    "Expiration": CT - 5,
    "Flags": 0,
    "Sequence": 3,
    "PreviousTxnID": "B2" * 32,
    "PreviousTxnLgrSeq": 50,
}
N1 = {
    "index": idx(3),
    "LedgerEntryType": "NFTokenOffer",
    "Owner": Y,
    "Amount": "7",
    "Flags": 1,
    "Expiration": CT + 3600,
    "PreviousTxnID": "B3" * 32,
    "PreviousTxnLgrSeq": 60,
}
N2 = {
    "index": idx(4),
    "LedgerEntryType": "NFTokenOffer",
    "Owner": Y,
    "Amount": "3",
    "Flags": 0,
    "PreviousTxnID": "B4" * 32,
    "PreviousTxnLgrSeq": 1,
}
P1 = {
    "index": idx(5),
    "LedgerEntryType": "PayChannel",
    "Account": Z,
    "Amount": "100",
    "Balance": "40",
    "SettleDelay": 60,
    "Flags": 0,
    "PreviousTxnID": "B5" * 32,
    "PreviousTxnLgrSeq": 98,
}
C1 = {
    "index": idx(6),
    "LedgerEntryType": "Check",
    "Account": X,
    "SendMax": USD("2.25"),
    "Expiration": CT + 40 * 86400,
    "Flags": 0,
    "PreviousTxnID": "B6" * 32,
    "PreviousTxnLgrSeq": 100,
}
T1 = {
    "index": idx(7),
    "LedgerEntryType": "Ticket",
    "Account": Z,
    "TicketSequence": 9,
    "Flags": 0,
    "PreviousTxnID": "B7" * 32,
    "PreviousTxnLgrSeq": 100,
}
A_ROWS = [E1, O1, N1, N2, P1, C1, T1]


def header(ledger, close_time, rows):
    counts = {t: sum(r["LedgerEntryType"] == t for r in rows) for t in TYPES}
    return {
        "format": A.FORMAT,
        "ledger_index": ledger,
        "ledger_hash": f"{ledger:064X}",
        "close_time": close_time,
        "close_time_iso": "x",
        "deferred_rows": len(rows),
        "deferred": {t: {"full_tree": n, "type_filter": n} for t, n in counts.items()},
        "server_state_reserve_inc": 2_000_000,
    }


def tx(h, seq, i, ttype, nodes):
    return {
        "ledger_index": seq,
        "tx_index": i,
        "close_time": 0,
        "tx": {"hash": h, "TransactionType": ttype},
        "meta": {"AffectedNodes": nodes, "TransactionIndex": i},
    }


def fin(o):
    return {k: v for k, v in o.items() if k not in ("index", "LedgerEntryType", *A.THREAD)}


def window_txs():
    e2_new = {"Account": X, "Amount": "50", "Destination": W}
    p1_final = dict(fin(P1), Balance="90")
    return [
        tx(
            H1,
            101,
            0,
            "OfferCancel",
            [
                {"DeletedNode": {"LedgerEntryType": "Offer", "LedgerIndex": idx(2), "FinalFields": fin(O1)}},
                {"ModifiedNode": {"LedgerEntryType": "AccountRoot", "LedgerIndex": "FF" * 32}},
            ],
        ),
        tx(
            H2,
            102,
            0,
            "EscrowCreate",
            [{"CreatedNode": {"LedgerEntryType": "Escrow", "LedgerIndex": idx(8), "NewFields": e2_new}}],
        ),
        tx(
            H3,
            102,
            1,
            "PaymentChannelClaim",
            [
                {
                    "ModifiedNode": {
                        "LedgerEntryType": "PayChannel",
                        "LedgerIndex": idx(5),
                        "PreviousFields": {"Balance": "40"},
                        "FinalFields": p1_final,
                        "PreviousTxnID": P1["PreviousTxnID"],
                        "PreviousTxnLgrSeq": 98,
                    }
                }
            ],
        ),
    ]


def b_rows():
    e2 = {
        "index": idx(8),
        "LedgerEntryType": "Escrow",
        "Account": X,
        "Amount": "50",
        "Destination": W,
        "Flags": 0,
        "OwnerNode": "0",
        "PreviousTxnID": H2,
        "PreviousTxnLgrSeq": 102,
    }
    p1 = dict(P1, Balance="90", PreviousTxnID=H3, PreviousTxnLgrSeq=102)
    return [E1, N1, N2, p1, C1, T1, e2]


def write(tmp_path, a_rows=None, b=None, txs=None, ledgers=None, gaps=None, b_seq=103):
    a_rows = A_ROWS if a_rows is None else a_rows
    b = b_rows() if b is None else b
    txs = window_txs() if txs is None else txs
    d = tmp_path / "tx"
    d.mkdir(exist_ok=True)
    pa, pb = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    pa.write_text("".join(json.dumps(r) + "\n" for r in [header(100, CT, a_rows)] + a_rows))
    pb.write_text("".join(json.dumps(r) + "\n" for r in [header(b_seq, CT + 30, b)] + b))
    (d / "obj-tx.jsonl").write_text("".join(json.dumps(x) + "\n" for x in txs))
    if ledgers is None:
        per = {}
        for x in txs:
            per[x["ledger_index"]] = per.get(x["ledger_index"], 0) + 1
        ledgers = [(i, per.get(i, 0)) for i in range(101, b_seq + 3)]  # watcher runs past B
    with open(d / "ledgers.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ledger_index", "obj_tx_count"])
        w.writerows(ledgers)
    with open(d / "gaps.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ledger_index", "error", "logged_at"])
        w.writerows(gaps or [])
    types = tmp_path / "types.json"
    types.write_text(
        json.dumps(
            {
                "entries": [{"name": t, "deferred": True} for t in TYPES]
                + [{"name": "AccountRoot", "deferred": False}]
            }
        )
    )
    return str(pa), str(pb), str(d), str(types)


def run(tmp_path, **kw):
    return A.analyze(*write(tmp_path, **kw), sample=2, seed=5)


def failing(out):
    return out["reconciliation"]["failed"]


# ---- census, every number computed by hand -------------------------------------------------


def test_census_values_by_hand(tmp_path):
    out, rc, _ = run(tmp_path)
    a = out["A"]
    assert a["total"] == 7
    assert a["counts"] == {
        "Check": 1,
        "Credential": 0,
        "DepositPreauth": 0,
        "Escrow": 1,
        "NFTokenOffer": 2,
        "Offer": 1,
        "PayChannel": 1,
        "Ticket": 1,
    }
    assert a["value"]["native_drops"] == {
        "Escrow.Amount": "1000",
        "NFTokenOffer.buy.Amount": "3",
        "NFTokenOffer.sell.Amount": "7",
        "Offer.TakerGets": "500",
        "PayChannel.Amount": "100",
        "PayChannel.unclaimed": "60",
    }
    assert a["value"]["iou"] == {
        "Check.SendMax": {f"USD/{ISS}": "2.25"},
        "Offer.TakerPays": {f"USD/{ISS}": "1.5"},
    }
    assert a["reserve"] == {"reserve_inc_drops": 2_000_000, "owner_count_units": 7, "drops": "14000000"}


def test_concentration_by_hand(tmp_path):
    out, _, _ = run(tmp_path)
    allo = out["A"]["owners"]["ALL"]
    # owners X=3 (E1, O1, C1), Y=2, Z=2 -> sorted [2,2,3]: ((-2)*2 + 0*2 + 2*3) / (3*7) = 2/21
    assert allo["owners"] == 3 and allo["top1_objects"] == 3
    assert allo["top1_share"] == "3/7" and allo["top5_share"] == "1"
    assert allo["gini"] == "2/21"
    assert out["A"]["owners"]["NFTokenOffer"]["owners"] == 1


def test_gini_off_centre():
    # a 90/10 split must give 2/5 whatever the input order; swapped weights would give -2/5
    assert A.gini([9, 1]) == A.gini([1, 9]) == Fraction(2, 5)
    assert A.gini([]) == 0 and A.gini([0, 0]) == 0 and A.gini([5, 5, 5]) == 0


def test_deadlines_and_age_by_hand(tmp_path):
    out, _, _ = run(tmp_path)
    d = out["A"]["deadlines"]
    assert d["Offer"]["expired"] == 1  # Expiration CT-5
    assert d["Escrow"]["<1h"] == 1  # CancelAfter CT+100 (FinishAfter does not count as a deadline)
    assert d["NFTokenOffer"]["1h-24h"] == 1 and d["NFTokenOffer"]["none"] == 1  # CT+3600 is not <1h
    assert d["Check"][">30d"] == 1 and d["Ticket"]["none"] == 1 and d["PayChannel"]["none"] == 1
    assert out["A"]["expired_not_removed"] == 1
    assert out["A"]["escrow_finishable"] == 1
    ages = out["A"]["age_ledgers"]
    assert ages["Offer"]["<1k"] == 1 and ages["NFTokenOffer"]["<1k"] == 2  # 100-50; 100-60, 100-1


@pytest.mark.parametrize(
    "left,bucket",
    [
        (-1, "expired"),
        (0, "expired"),
        (1, "<1h"),
        (3599, "<1h"),
        (3600, "1h-24h"),
        (86399, "1h-24h"),
        (86400, "1d-7d"),
        (7 * 86400, "7d-30d"),
        (30 * 86400 - 1, "7d-30d"),
        (30 * 86400, ">30d"),
    ],
)
def test_deadline_bucket_edges(left, bucket):
    assert A.deadline_bucket({"Expiration": 1000 + left}, 1000) == bucket


def test_earliest_deadline_wins():
    assert A.deadline_bucket({"Expiration": 1000 + 90000, "CancelAfter": 1000 + 10}, 1000) == "<1h"


def test_age_bucket_edges():
    assert [
        A.age_bucket({"PreviousTxnLgrSeq": 10**7 - a}, 10**7)
        for a in (999, 1000, 99_999, 100_000, 999_999, 1_000_000)
    ] == ["<1k", "1k-100k", "1k-100k", "100k-1M", "100k-1M", ">=1M"]


def test_credential_owner_follows_accepted_flag():
    c = {"LedgerEntryType": "Credential", "Issuer": "rI", "Subject": "rS", "Flags": 0}
    assert A.owner_of(c) == "rI"
    assert A.owner_of(dict(c, Flags=A.LSF_ACCEPTED)) == "rS"


# ---- A->B replay ---------------------------------------------------------------------------


def test_clean_window_passes(tmp_path):
    out, rc, rows = run(tmp_path)
    assert rc == 0 and out["reconciliation"]["status"] == "PASS"
    assert out["transitions"]["Offer"] == {"created": 0, "deleted": 1, "modified": 0, "unchanged": 0}
    assert out["transitions"]["Escrow"] == {"created": 1, "deleted": 0, "modified": 0, "unchanged": 1}
    assert out["transitions"]["PayChannel"]["modified"] == 1
    assert out["nodes_replayed"] == 3  # the AccountRoot node is not a deferred type
    assert {(r["type"], r["transition"]) for r in rows} == {
        ("Offer", "deleted"),
        ("Escrow", "created"),
        ("PayChannel", "modified"),
    }
    assert {(c["tx_type"], c["node"]) for c in out["caused_by"]} == {
        ("OfferCancel", "DeletedNode"),
        ("EscrowCreate", "CreatedNode"),
        ("PaymentChannelClaim", "ModifiedNode"),
    }
    assert out["sampled_txs"]["hashes"] == [H2, H3]  # seed 5 skips the first hash
    assert len(out["sampled_txs"]["hashes"]) == 2


def test_txs_after_b_are_ignored(tmp_path):
    late = tx(
        "C9" * 32,
        104,
        0,
        "OfferCancel",
        [{"DeletedNode": {"LedgerEntryType": "Escrow", "LedgerIndex": idx(1), "FinalFields": fin(E1)}}],
    )
    out, rc, _ = run(tmp_path, txs=window_txs() + [late])
    assert rc == 0 and out["window"]["deferred_txs"] == 3


def test_replay_order_is_ledger_then_tx_index(tmp_path):
    # create then delete of the same object inside the window, written out of order
    new = {"Account": X, "TicketSequence": 11}
    cr = tx(
        "D1" * 32,
        101,
        1,
        "TicketCreate",
        [{"CreatedNode": {"LedgerEntryType": "Ticket", "LedgerIndex": idx(9), "NewFields": new}}],
    )
    de = tx(
        "D2" * 32,
        102,
        5,
        "AccountSet",
        [
            {
                "DeletedNode": {
                    "LedgerEntryType": "Ticket",
                    "LedgerIndex": idx(9),
                    "FinalFields": dict(new, Flags=0, OwnerNode="0"),
                }
            }
        ],
    )
    out, rc, _ = run(tmp_path, txs=[de] + window_txs() + [cr])
    assert rc == 0, out["reconciliation"]
    assert out["transitions"]["Ticket"]["created"] == 0  # never in A or B


# ---- isolated reconciliation failures: exactly the named check fails ------------------------


def test_r2_only_field_diff(tmp_path):
    b = b_rows()
    b[3] = dict(b[3], Balance="91")
    out, rc, _ = run(tmp_path, b=b)
    assert rc == 1 and failing(out) == ["R2_fields"]
    assert "Balance: replay '90' != B '91'" in out["reconciliation"]["checks"]["R2_fields"]["residuals"][0]


def test_r2_created_default_rule(tmp_path):
    b = b_rows()
    b[6] = dict(b[6], Flags=0)  # default value omitted from NewFields: fine
    out, rc, _ = run(tmp_path, b=b)
    assert rc == 0
    b[6] = dict(b[6], Flags=5)  # non-default value NewFields did not set
    out, rc, _ = run(tmp_path, b=b)
    assert rc == 1 and failing(out) == ["R2_fields"]


def test_r4_only_previous_fields_disagree(tmp_path):
    t = window_txs()
    t[2]["meta"]["AffectedNodes"][0]["ModifiedNode"]["PreviousFields"] = {"Balance": "41"}
    out, rc, _ = run(tmp_path, txs=t)
    assert rc == 1 and failing(out) == ["R4_node_consistency"]
    assert (
        "PreviousFields.Balance '41' != replay '40'"
        in out["reconciliation"]["checks"]["R4_node_consistency"]["residuals"][0]
    )


def test_r4_only_deleted_final_fields_disagree(tmp_path):
    t = window_txs()
    t[0]["meta"]["AffectedNodes"][0]["DeletedNode"]["FinalFields"] = dict(fin(O1), TakerGets="501")
    out, rc, _ = run(tmp_path, txs=t)
    assert rc == 1 and failing(out) == ["R4_node_consistency"]


def test_r5_only_wrong_previous_txn_id(tmp_path):
    b = b_rows()
    b[3] = dict(b[3], PreviousTxnID="EE" * 32)  # seq 102 > A, but not the tx that touched it
    out, rc, _ = run(tmp_path, b=b)
    assert rc == 1 and failing(out) == ["R5_threading"]


def test_r5_only_old_object_rethreaded(tmp_path):
    b = b_rows()
    b[0] = dict(b[0], PreviousTxnLgrSeq=97)  # still <= A, but no longer the A object
    out, rc, _ = run(tmp_path, b=b)
    assert rc == 1 and failing(out) == ["R5_threading"]


def test_r1_r3_missing_create(tmp_path):
    t = window_txs()
    del t[1]  # the EscrowCreate is not in the window file (ledger count fixed to match)
    ledgers = [(101, 1), (102, 1), (103, 0)]
    out, rc, _ = run(tmp_path, txs=t, ledgers=ledgers)
    assert rc == 1
    # B's escrow is unexplained: R1 (missing from replay), R3 (count), R5 (its PreviousTxnID)
    assert failing(out) == ["R1_index_set", "R3_counts", "R5_threading"]
    assert out["reconciliation"]["checks"]["R3_counts"]["residuals"] == [
        "Escrow: B 2 != A 1 + created 0 - deleted 0"
    ]


def test_r3_with_r4_create_of_live(tmp_path):
    # a CreatedNode for an index already live: the set still matches B, but the counts do not
    t = window_txs()
    t[1]["meta"]["AffectedNodes"].append(
        {"CreatedNode": {"LedgerEntryType": "Ticket", "LedgerIndex": idx(7), "NewFields": fin(T1)}}
    )
    b = b_rows()
    b[5] = dict(T1, PreviousTxnID=H2, PreviousTxnLgrSeq=102)
    out, rc, _ = run(tmp_path, txs=t, b=b)
    assert failing(out) == ["R3_counts", "R4_node_consistency"]
    assert (
        "R4 create of live object" in out["reconciliation"]["checks"]["R4_node_consistency"]["residuals"][0]
    )


def test_r4_modify_and_delete_of_absent(tmp_path):
    t = window_txs()
    t[2]["meta"]["AffectedNodes"].append(
        {"ModifiedNode": {"LedgerEntryType": "Check", "LedgerIndex": idx(30), "FinalFields": {"Account": X}}}
    )
    t[2]["meta"]["AffectedNodes"].append(
        {"DeletedNode": {"LedgerEntryType": "Check", "LedgerIndex": idx(31), "FinalFields": {}}}
    )
    out, rc, _ = run(tmp_path, txs=t)
    res = out["reconciliation"]["checks"]["R4_node_consistency"]["residuals"]
    assert any("modify of absent" in r for r in res) and any("delete of absent" in r for r in res)


def test_r1_unexpected_left_over(tmp_path):
    t = window_txs()
    del t[0]  # the OfferCancel is lost: the offer survives the replay but is gone in B
    out, rc, _ = run(tmp_path, txs=t, ledgers=[(101, 0), (102, 2), (103, 0)])
    assert "R1_index_set" in failing(out)
    assert out["reconciliation"]["checks"]["R1_index_set"]["residuals"] == [
        f"unexpected after replay: Offer {idx(2)}"
    ]


# ---- completeness: nothing is reconciled on incomplete data (exit 2) ------------------------


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"ledgers": [(101, 1), (103, 0)]}, "1 ledgers missing"),
        ({"ledgers": [(101, 1), (102, 2), (102, 2), (103, 0)]}, "two ledgers.csv rows"),
        ({"gaps": [(102, "Timeout", "x")]}, "1 gap ledgers in range"),
        ({"ledgers": [(101, 1), (102, 1), (103, 0)]}, "ledger 102: obj_tx_count 1 != 2"),
    ],
)
def test_incomplete_window_not_checkable(tmp_path, kw, needle):
    out, rc, _ = run(tmp_path, **kw)
    assert rc == 2 and out["reconciliation"]["status"] == "NOT_CHECKABLE"
    assert any(needle in p for p in out["window"]["completeness_problems"]), out["window"]
    assert "transitions" not in out


def test_duplicate_tx_hash_not_checkable(tmp_path):
    t = window_txs()
    t.append(copy.deepcopy(t[2]))
    out, rc, _ = run(tmp_path, txs=t, ledgers=[(101, 1), (102, 3), (103, 0)])
    assert rc == 2 and any("twice" in p for p in out["window"]["completeness_problems"])


def test_gap_outside_range_is_ignored(tmp_path):
    out, rc, _ = run(tmp_path, gaps=[(150, "Timeout", "x")])
    assert rc == 0


# ---- input validation and CLI ---------------------------------------------------------------


def test_bad_inputs_exit_2(tmp_path, capsys):
    pa, pb, d, types = write(tmp_path)
    assert A.main(["--a", pb, "--b", pa, "--tx", d, "--types", types]) == 2  # B before A
    assert "is not after A" in capsys.readouterr().out
    lines = open(pa).read().splitlines()
    open(pa, "w").write("\n".join(lines[:-1]) + "\n")  # header says 7 rows, 6 present
    assert A.main(["--a", pa, "--b", pb, "--tx", d, "--types", types]) == 2
    assert "rows != header deferred_rows" in capsys.readouterr().out


def test_duplicate_index_refused(tmp_path):
    pa, pb, d, types = write(tmp_path, a_rows=A_ROWS + [E1])
    with pytest.raises(A.InputError, match="twice"):
        A.load_snapshot(pa)


def test_cli_writes_json_and_transitions(tmp_path, capsys):
    pa, pb, d, types = write(tmp_path)
    j, c = tmp_path / "out.json", tmp_path / "t.csv"
    rc = A.main(
        ["--a", pa, "--b", pb, "--tx", d, "--types", types, "--json", str(j), "--transitions", str(c)]
    )
    assert rc == 0
    text = capsys.readouterr().out
    assert "RECONCILIATION PASS" in text and "R1_index_set OK residuals=0" in text
    assert json.loads(j.read_text())["reconciliation"]["status"] == "PASS"
    assert c.read_text().splitlines()[0] == "index,type,transition" and len(c.read_text().splitlines()) == 4
    # same inputs -> same bytes
    rc = A.main(["--a", pa, "--b", pb, "--tx", d, "--types", types, "--json", str(tmp_path / "o2.json")])
    assert (tmp_path / "o2.json").read_bytes() == j.read_bytes()


def test_cli_failure_prints_residuals(tmp_path, capsys):
    b = b_rows()
    b[3] = dict(b[3], Balance="91")
    pa, pb, d, types = write(tmp_path, b=b)
    assert A.main(["--a", pa, "--b", pb, "--tx", d, "--types", types]) == 1
    text = capsys.readouterr().out
    assert (
        "R2_fields FAIL residuals=1" in text
        and "RESIDUAL PayChannel" in text
        and "RECONCILIATION FAIL" in text
    )


def test_object_threaded_after_its_snapshot_refused(tmp_path):
    pa, *_ = write(tmp_path, a_rows=[dict(E1, PreviousTxnLgrSeq=101)])
    with pytest.raises(A.InputError, match="after the snapshot ledger"):
        A.load_snapshot(pa)


# ---- boundaries from the postfiatd source (see README "Deadline semantics") -----------------


@pytest.mark.parametrize(
    "obj,expired",
    [
        ({"LedgerEntryType": "Offer", "Expiration": 1000}, True),  # hasExpired: T >= mark
        ({"LedgerEntryType": "Check", "Expiration": 1001}, False),
        ({"LedgerEntryType": "PayChannel", "CancelAfter": 1000}, True),  # closeTime >= cancelAfter
        ({"LedgerEntryType": "Escrow", "CancelAfter": 1000}, False),  # after(now, mark): now > mark
        ({"LedgerEntryType": "Escrow", "CancelAfter": 999}, True),
        ({"LedgerEntryType": "Credential", "Expiration": 1000}, False),  # now > exp
        ({"LedgerEntryType": "Credential", "Expiration": 999}, True),
    ],
)
def test_expiry_boundaries(obj, expired):
    assert A.is_expired(obj, 1000) is expired
    assert (A.deadline_bucket(obj, 1000) == "expired") is expired


def test_escrow_finishable_strictly_after(tmp_path):
    e_eq = dict(E1, index=idx(20), FinishAfter=CT)  # after(now, FinishAfter) is false at now == mark
    e_lt = dict(E1, index=idx(21), FinishAfter=CT - 1)
    out, _, _ = run(tmp_path, a_rows=[e_eq, e_lt], b=[e_eq, e_lt], txs=[])
    assert out["A"]["escrow_finishable"] == 1


def test_iou_sums_are_exact_decimals():
    native, iou = A.Counter(), A.defaultdict(lambda: A.defaultdict(A.Decimal))
    for v in ("0.1", "0.2"):
        A.add_amount(native, iou, "k", {"currency": "USD", "issuer": ISS, "value": v})
    assert format(iou["k"][("USD", ISS)], "f") == "0.3"  # float would give 0.30000000000000004
    A.add_amount(native, iou, "k", "9007199254740993")  # 2^53 + 1 stays exact
    assert native["k"] == 9007199254740993


def test_owner_fields():
    assert A.owner_of(N1) == Y and A.owner_of(E1) == X and A.owner_of(T1) == Z


def test_top5_share_counts_five_owners():
    objs = {idx(k): {"LedgerEntryType": "Ticket", "Account": f"r{k}"} for k in range(6)}
    objs[idx(9)] = {"LedgerEntryType": "Ticket", "Account": "r0"}
    c = A.concentration(objs)["ALL"]
    # counts r0=2, r1..r5=1 -> top5 = 2+1+1+1+1 = 6 of 7
    # Gini weights -5,-3,-1,1,3,5: (-5-3-1+1+3+5*2)/(6*7) = 5/42
    assert c["top5_share"] == "6/7" and c["top1_share"] == "2/7" and c["owners"] == 6
    assert c["gini"] == str(A.gini([1, 1, 1, 1, 1, 2])) == "5/42"


def test_same_ledger_order_by_tx_index(tmp_path):
    new = {"Account": X, "TicketSequence": 12}
    cr = tx(
        "D3" * 32,
        102,
        3,
        "TicketCreate",
        [{"CreatedNode": {"LedgerEntryType": "Ticket", "LedgerIndex": idx(10), "NewFields": new}}],
    )
    de = tx(
        "D4" * 32,
        102,
        4,
        "AccountSet",
        [{"DeletedNode": {"LedgerEntryType": "Ticket", "LedgerIndex": idx(10), "FinalFields": new}}],
    )
    out, rc, _ = run(tmp_path, txs=window_txs() + [de, cr])  # delete written before create
    assert rc == 0, out["reconciliation"]


def test_r4_created_then_deleted_with_non_default_field(tmp_path):
    # NewFields omitted Flags (so it was 0); a later DeletedNode claiming Flags 5 is inconsistent
    new = {"Account": X, "TicketSequence": 13}
    cr = tx(
        "D5" * 32,
        101,
        2,
        "TicketCreate",
        [{"CreatedNode": {"LedgerEntryType": "Ticket", "LedgerIndex": idx(11), "NewFields": new}}],
    )
    de = tx(
        "D6" * 32,
        102,
        6,
        "AccountSet",
        [
            {
                "DeletedNode": {
                    "LedgerEntryType": "Ticket",
                    "LedgerIndex": idx(11),
                    "FinalFields": dict(new, Flags=5),
                }
            }
        ],
    )
    out, rc, _ = run(tmp_path, txs=window_txs() + [cr, de])
    assert rc == 1 and failing(out) == ["R4_node_consistency"]
    assert (
        "FinalFields.Flags 5 != replay None"
        in out["reconciliation"]["checks"]["R4_node_consistency"]["residuals"][0]
    )
