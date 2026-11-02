import copy
import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "payflow"))
import crosscheck_payflow as xc  # noqa: E402

S = "rSENDERxxxxxxxxxxxxxxxxxxxxxxxxxxx"
D = "rDESTxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ISS = "rISSUERxxxxxxxxxxxxxxxxxxxxxxxxxxx"
PAY_FIELDS = [
    "ledger_index",
    "tx_index",
    "tx_hash",
    "account",
    "destination",
    "result",
    "fee_drops",
    "kind",
    "delivered_drops",
    "delivered_raw",
]


def h(i):
    return f"{i:064X}"


def fixture():
    """Local rows and matching public-RPC answers: 6 native successes, 1 IOU, 2 failures, 1 new account."""
    pays, deltas, ledgers, answers, ledger_answers = [], [], [], {}, {}
    specs = [("native", "tesSUCCESS", 1_000_000 + i) for i in range(6)]
    specs += [
        ("iou", "tesSUCCESS", None),
        ("native", "tecUNFUNDED_PAYMENT", None),
        ("native", "tecNO_DST_INSUF_PFT", None),
        ("native", "tesSUCCESS", 30_000_000),
    ]
    for i, (kind, res, amt) in enumerate(specs):
        li, th = 500 + i, h(i)
        ok = res == "tesSUCCESS"
        iou = {"currency": "USD", "issuer": ISS, "value": "5"}
        dst = D if i != 9 else "rNEWACCOUNTxxxxxxxxxxxxxxxxxxxxxxx"
        pays.append(
            {
                "ledger_index": str(li),
                "tx_index": "0",
                "tx_hash": th,
                "account": S,
                "destination": dst,
                "result": res,
                "fee_drops": "12",
                "kind": kind,
                "delivered_drops": str(amt) if ok and kind == "native" else "",
                "delivered_raw": json.dumps(iou, sort_keys=True, separators=(",", ":"))
                if ok and kind == "iou"
                else "",
            }
        )
        nodes = [
            {
                "ModifiedNode": {
                    "LedgerEntryType": "AccountRoot",
                    "FinalFields": {"Account": S, "Balance": str(10**9 - (amt or 0) - 12)},
                    "PreviousFields": {"Balance": str(10**9)},
                }
            }
        ]
        deltas.append(
            {
                "ledger_index": str(li),
                "tx_index": "0",
                "tx_hash": th,
                "account": S,
                "delta_drops": str(-(amt or 0) - 12),
            }
        )
        if ok and kind == "native":
            if i == 9:
                nodes.append(
                    {
                        "CreatedNode": {
                            "LedgerEntryType": "AccountRoot",
                            "NewFields": {"Account": dst, "Balance": str(amt)},
                        }
                    }
                )
            else:
                nodes.append(
                    {
                        "ModifiedNode": {
                            "LedgerEntryType": "AccountRoot",
                            "FinalFields": {"Account": dst, "Balance": str(5 * 10**8 + amt)},
                            "PreviousFields": {"Balance": str(5 * 10**8)},
                        }
                    }
                )
            deltas.append(
                {
                    "ledger_index": str(li),
                    "tx_index": "0",
                    "tx_hash": th,
                    "account": dst,
                    "delta_drops": str(amt),
                }
            )
        meta = {"TransactionResult": res, "TransactionIndex": 0, "AffectedNodes": nodes}
        if ok:
            meta["delivered_amount"] = str(amt) if kind == "native" else iou
        answers[th] = {
            "status": "success",
            "validated": True,
            "ledger_index": li,
            "hash": th,
            "Account": S,
            "Destination": dst,
            "Fee": "12",
            "TransactionType": "Payment",
            "meta": meta,
        }
        ledgers.append({"ledger_index": str(li), "ledger_hash": f"LH{li}", "txn_count": "1"})
        ledger_answers[li] = {"status": "success", "ledger": {"ledger_hash": f"LH{li}", "transactions": [th]}}
    return pays, deltas, ledgers, answers, ledger_answers


def write(tmp_path, pays, deltas, ledgers):
    for name, rows, fields in (
        ("payments", pays, PAY_FIELDS),
        ("deltas", deltas, ["ledger_index", "tx_index", "tx_hash", "account", "delta_drops"]),
        ("ledgers", ledgers, ["ledger_index", "ledger_hash", "txn_count"]),
    ):
        with open(tmp_path / f"{name}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    return str(tmp_path)


class Fake:
    def __init__(self, answers, ledger_answers, fail_first=0, error="slowDown"):
        self.answers, self.ledger_answers = answers, ledger_answers
        self.fail_first, self.error, self.calls = fail_first, error, 0

    def __call__(self, body):
        self.calls += 1
        if self.calls <= self.fail_first:
            return {"result": {"status": "error", "error": self.error}}
        req = json.loads(body)
        p = req["params"][0]
        if req["method"] == "tx":
            return {"result": self.answers[p["transaction"]]}
        return {"result": self.ledger_answers[p["ledger_index"]]}


def rpc(fake, retries=6):
    return xc.Rpc(
        "http://fake",
        gap=0,
        retries=retries,
        post=fake,
        sleep=lambda s: None,
        retry_errors=xc.RETRY,
        methods=xc.METHODS,
    )


@pytest.fixture
def world(tmp_path):
    pays, deltas, ledgers, answers, lans = fixture()
    return write(tmp_path, pays, deltas, ledgers), answers, lans


def lines_of(data, fake, **kw):
    out = []
    res, rec = xc.run(data, rpc(fake), kw.pop("count", 10), kw.pop("seed", "1"), out=out.append, **kw)
    return res, rec, out


def test_clean_all_ok(world):
    data, answers, lans = world
    res, rec, out = lines_of(data, Fake(answers, lans))
    assert res == {"ok": 20, "mismatch": 0}, out
    assert {r["delivered"] for r in rec["rpc"].values()} >= {
        "",
        '{"currency":"USD","issuer":"' + ISS + '","value":"5"}',
    }


def test_sample_includes_failed_and_is_deterministic(world):
    data, _, _ = world
    recs, _ = xc.local_records(data)
    s1 = xc.sample(recs, 4, "7")
    assert s1 == xc.sample(recs, 4, "7")
    assert sum(recs[x]["result"] != "tesSUCCESS" for x in s1) == 2
    assert len(set(xc.sample(recs, 20, "7"))) == 10  # count capped at the population


@pytest.mark.parametrize("field,value", [("Fee", "13"), ("Destination", S), ("ledger_index", 1)])
def test_tx_field_difference_is_mismatch(world, field, value):
    data, answers, lans = world
    answers = copy.deepcopy(answers)
    answers[h(0)][field] = value
    res, _, out = lines_of(data, Fake(answers, lans))
    assert res["mismatch"] == 1 and any(x.startswith("MISMATCH P") for x in out)


def test_delivered_and_balance_and_validated_differences(world):
    data, answers, lans = world
    for mut in (
        lambda a: a[h(1)]["meta"].update(delivered_amount="1"),
        lambda a: a[h(6)]["meta"]["delivered_amount"].update(value="6"),
        lambda a: a[h(2)]["meta"]["AffectedNodes"][1]["ModifiedNode"]["FinalFields"].update(Balance="1"),
        lambda a: a[h(9)]["meta"]["AffectedNodes"][1]["CreatedNode"]["NewFields"].update(Balance="1"),
        lambda a: a[h(3)].update(validated=False),
        lambda a: a[h(7)]["meta"].update(TransactionResult="tesSUCCESS", delivered_amount="5"),
    ):
        ans = copy.deepcopy(answers)
        mut(ans)
        res, _, out = lines_of(data, Fake(ans, lans))
        assert res["mismatch"] == 1, out


def test_ledger_hash_and_count_difference(world):
    data, answers, lans = world
    for mut in (
        lambda la: la[503]["ledger"].update(ledger_hash="X"),
        lambda la: la[504]["ledger"].update(transactions=[]),
    ):
        la = copy.deepcopy(lans)
        mut(la)
        res, _, out = lines_of(data, Fake(answers, la))
        assert res["mismatch"] == 1 and any(x.startswith("MISMATCH L") for x in out)


def test_api_v2_shape():
    a = fixture()[3][h(0)]
    v2 = {
        "validated": True,
        "ledger_index": a["ledger_index"],
        "meta": a["meta"],
        "tx_json": {k: a[k] for k in ("Account", "Destination", "Fee", "TransactionType")},
    }
    assert xc.rpc_record(v2) == xc.rpc_record(a)


def test_deleted_node_change():
    r = xc.rpc_record(
        {
            "validated": True,
            "ledger_index": 1,
            "Account": S,
            "Fee": "10",
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "AffectedNodes": [
                    {
                        "DeletedNode": {
                            "LedgerEntryType": "AccountRoot",
                            "FinalFields": {"Account": S, "Balance": "0"},
                            "PreviousFields": {"Balance": "40"},
                        }
                    }
                ],
            },
        }
    )
    assert r["balance_changes"] == {S: -40}


def test_negative_every_check_mismatches(world):
    data, answers, lans = world
    res, rec, out = lines_of(data, Fake(answers, lans), negative=True)
    assert res == {"ok": 0, "mismatch": 20}, out
    assert any(rec["rpc"][x]["delivered"] == "" for x in rec["sample"])  # the empty-delivered case too


def test_retry_counted_not_mismatch_and_persisting_error_raises(world):
    data, answers, lans = world
    r = rpc(Fake(answers, lans, fail_first=2))
    res, _ = xc.run(data, r, 10, "1", out=lambda s: None)
    assert res["mismatch"] == 0 and r.retried == 2 and r.retry_kinds == {"slowDown": 2}
    with pytest.raises(xc.RpcError):
        xc.run(
            data,
            rpc(Fake(answers, lans, fail_first=99, error="lgrNotFound"), retries=3),
            10,
            "1",
            out=lambda s: None,
        )


def test_offline_pass_and_tamper(world, tmp_path, capsys):
    data, answers, lans = world
    for negative in (False, True):
        _, rec, _ = lines_of(data, Fake(answers, lans), negative=negative)
        assert xc.offline(data, rec, out=lambda s: None) == 0
    _, rec, _ = lines_of(data, Fake(answers, lans))
    bad = copy.deepcopy(rec)
    bad["rpc"][rec["sample"][0]]["fee_drops"] += 1
    assert xc.offline(data, bad, out=lambda s: None) == 1
    bad = copy.deepcopy(rec)
    bad["sample"] = bad["sample"][::-1]
    assert xc.offline(data, bad, out=lambda s: None) >= 1
    bad = copy.deepcopy(rec)
    bad["verdicts"].pop(next(iter(bad["verdicts"])))
    assert xc.offline(data, bad, out=lambda s: None) >= 1
    bad = copy.deepcopy(rec)
    bad["rpc_ledgers"][next(iter(bad["rpc_ledgers"]))]["txn_count"] = 9
    assert xc.offline(data, bad, out=lambda s: None) == 1
    p = tmp_path / "rec.json"
    p.write_text(json.dumps(rec))
    assert xc.main(["--data", data, "--offline", str(p)]) == 0
    assert "RESULT PASS offline fails=0" in capsys.readouterr().out


def test_negative_alters_delivered_on_every_payment(world):
    data, answers, lans = world
    _, _, out = lines_of(data, Fake(answers, lans), negative=True)
    p_lines = [x for x in out if x.startswith("MISMATCH P")]
    assert len(p_lines) == 10 and all("'delivered'" in x and "'fee_drops'" in x for x in p_lines)
