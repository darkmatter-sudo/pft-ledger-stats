import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "payflow"))
import reconcile_payflow as rp  # noqa: E402

A = "rAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
B = "rBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
C = "rCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
N = "rNEWNEWNEWNEWNEWNEWNEWNEWNEWNEWNEW"
TOTAL = 10_000_000_000


class World:
    """Consistent snapshot A, collection rows and snapshot B; one tx per ledger from ledger 101."""

    def __init__(self, opening):
        self.open = dict(opening)
        self.bal = dict(opening)
        self.escrow_a = self.escrow = 0
        self.burn = 0
        self.txs, self.pays, self.deltas, self.ledgers, self.gaps = [], [], [], [], []
        self.li = 100

    def _delta(self, h, acct, d, node="Modified"):
        prev = self.bal.get(acct, 0)
        self.bal[acct] = prev + d
        self.deltas.append(
            {
                "ledger_index": str(self.li),
                "tx_index": "0",
                "tx_hash": h,
                "account": acct,
                "node": node,
                "prev_drops": str(prev),
                "final_drops": str(prev + d),
                "delta_drops": str(d),
            }
        )

    def _tx(self, acct, tx_type, fee):
        self.li += 1
        h = f"{self.li:064X}"
        self.txs.append(
            {
                "ledger_index": str(self.li),
                "tx_index": "0",
                "tx_hash": h,
                "account": acct,
                "tx_type": tx_type,
                "result": "tesSUCCESS",
                "fee_drops": str(fee),
            }
        )
        self.ledgers.append({"ledger_index": str(self.li), "ledger_hash": f"L{self.li}", "txn_count": "1"})
        self.burn += fee
        return h

    def pay(self, src, dst, amt, fee=10, res="tesSUCCESS"):
        h = self._tx(src, "Payment", fee)
        self.txs[-1]["result"] = res
        ok = res == "tesSUCCESS"
        self.pays.append(
            {
                "ledger_index": str(self.li),
                "tx_index": "0",
                "tx_hash": h,
                "account": src,
                "destination": dst,
                "result": res,
                "fee_drops": str(fee),
                "kind": "native",
                "delivered_drops": str(amt) if ok else "",
                "delivered_raw": "",
            }
        )
        if src == dst:
            self._delta(h, src, -fee)
            return h
        self._delta(h, src, -(amt + fee) if ok else -fee)
        if ok:
            self._delta(h, dst, amt, "Created" if dst not in self.bal else "Modified")
        return h

    def escrow_create(self, src, amt, fee=10):
        h = self._tx(src, "EscrowCreate", fee)
        self._delta(h, src, -(amt + fee))
        self.escrow += amt
        return h

    def delete(self, acct, dst, fee=10):
        h = self._tx(acct, "AccountDelete", fee)
        left = self.bal[acct] - fee
        self._delta(h, acct, -self.bal[acct], "Deleted")
        self._delta(h, dst, left)
        del self.bal[acct]
        return h

    def snaps(self):
        def objs(bal, escrow):
            o = {
                f"{i:064X}": {
                    "LedgerEntryType": "AccountRoot",
                    "Account": a,
                    "Balance": str(b),
                    "index": f"{i:064X}",
                }
                for i, (a, b) in enumerate(sorted(bal.items()))
            }
            if escrow:
                o["E" * 64] = {"LedgerEntryType": "Escrow", "Amount": str(escrow), "index": "E" * 64}
            return o

        ha = {"ledger_index": 100, "ledger_hash": "HA", "total_coins": str(TOTAL)}
        hb = {"ledger_index": self.li, "ledger_hash": "HB", "total_coins": str(TOTAL - self.burn)}
        return ha, objs(self.open, self.escrow_a), hb, objs({a: b for a, b in self.bal.items()}, self.escrow)

    def run(self, negative=False, **over):
        ha, a, hb, b = self.snaps()
        args = dict(
            ha=ha,
            a=a,
            hb=hb,
            b=b,
            txs=self.txs,
            pays=self.pays,
            deltas=self.deltas,
            ledgers=self.ledgers,
            gaps=self.gaps,
        )
        args.update(over)
        return rp.reconcile(
            args["ha"],
            args["a"],
            args["hb"],
            args["b"],
            args["txs"],
            args["pays"],
            args["deltas"],
            args["ledgers"],
            args["gaps"],
            negative,
        )


def world():
    w = World({A: 1_000_000_000, B: 500_000_000, C: 50_000_000})
    w.pay(A, B, 1_000_000)
    w.pay(B, C, 2_500_000)
    w.pay(A, N, 20_000_000)  # creates N
    w.pay(C, A, 999_999_999, res="tecUNFUNDED_PAYMENT")
    w.pay(B, B, 5)  # self-payment: fee only
    return w


def bad_checks(lines):
    return [x.split()[1] for x in lines if x.startswith("MISMATCH")]


def test_clean_world_reconciles():
    lines, res = world().run()
    assert bad_checks(lines) == [], lines
    assert res["mismatch_lines"] == 0 and res["fees_drops"] == res["burned_drops"] == 50
    assert res["payments_checked"] == 3  # failed and self-payment are not checked by C7


def test_balance_off_by_one_drop():
    w = world()
    ha, a, hb, b = w.snaps()
    next(o for o in b.values() if o["Account"] == C)["Balance"] = str(w.bal[C] + 1)
    lines, res = w.run(b=b)
    assert res["mismatch_causes"] == {"account_balance": 2}  # the account line + the C4 total line
    assert any("rCCCCC" in x and "C4" in x for x in lines)


def test_created_account_absent_in_a():
    w = world()
    ha, a, hb, b = w.snaps()
    assert N not in {o["Account"] for o in a.values()}
    assert bad_checks(w.run()[0]) == []
    b = {k: o for k, o in b.items() if o.get("Account") != N}  # created but missing in B
    assert "C4" in bad_checks(w.run(b=b)[0])


def test_deleted_account_absent_in_b():
    w = world()
    w.delete(C, A)
    ha, a, hb, b = w.snaps()
    assert C not in {o.get("Account") for o in b.values()}
    assert bad_checks(w.run()[0]) == []
    b[f"{99:064X}"] = {"LedgerEntryType": "AccountRoot", "Account": C, "Balance": "1", "index": f"{99:064X}"}
    assert "C4" in bad_checks(w.run(b=b)[0])


def test_chain_break_detected():
    w = world()
    w.deltas[1] = dict(w.deltas[1], prev_drops=str(int(w.deltas[1]["prev_drops"]) + 7))
    assert "C5" in bad_checks(w.run()[0])


def test_burn_ne_fees():
    w = world()
    ha, a, hb, b = w.snaps()
    hb = dict(hb, total_coins=str(int(hb["total_coins"]) - 1))
    _, res = w.run(hb=hb)
    assert res["mismatch_causes"] == {"burn_ne_fees": 1}


def test_tx_value_not_conserved():
    w = world()
    d = w.deltas[0]
    w.deltas[0] = dict(
        d, delta_drops=str(int(d["delta_drops"]) + 3), final_drops=str(int(d["final_drops"]) + 3)
    )
    lines, res = w.run()
    assert "tx_value_not_conserved" in res["mismatch_causes"]


def test_payment_dest_delta_ne_delivered():
    w = world()
    w.pays[0] = dict(w.pays[0], delivered_drops=str(int(w.pays[0]["delivered_drops"]) - 1))
    _, res = w.run()
    assert res["mismatch_causes"] == {"dest_delta": 1, "payment_delta": 1, "sender_delta": 1}


def test_path_payment_counted_not_checked():
    w = world()
    h = w.pay(A, B, 1000)
    w._delta(h, C, 0)  # a third account appears in the metadata
    lines, res = w.run()
    assert res["path_payments"] == 1 and bad_checks(lines) == []


def test_non_adjacent_snapshot():
    w = world()
    ha, a, hb, b = w.snaps()
    assert "C1" in bad_checks(w.run(ha=dict(ha, ledger_index=99))[0])
    assert "C1" in bad_checks(w.run(hb=dict(hb, ledger_index=w.li - 1))[0])


def test_hole_gap_duplicate_txn_count():
    w = world()
    assert "C1" in bad_checks(w.run(ledgers=w.ledgers[:2] + w.ledgers[3:])[0])  # hole
    assert "C1" in bad_checks(w.run(gaps=[{"ledger_index": "103"}])[0])
    assert "C1" in bad_checks(w.run(ledgers=w.ledgers + [w.ledgers[0]])[0])
    led = [dict(r) for r in w.ledgers]
    led[0]["txn_count"] = "2"
    lines = w.run(ledgers=led)[0]
    assert any("C1 ledgers_with_tx_rows_ne_txn_count=1" in x for x in lines)


def test_escrow_holder_reconciles_and_missing_escrow_detected():
    w = world()
    w.escrow_create(A, 7_000_000)
    lines, res = w.run()
    assert bad_checks(lines) == [], lines
    ha, a, hb, b = w.snaps()
    b = {k: o for k, o in b.items() if o["LedgerEntryType"] != "Escrow"}
    assert "C3" in bad_checks(w.run(b=b)[0])


def test_negative_control_mismatches_every_value_check():
    lines, res = world().run(negative=True)
    assert set(bad_checks(lines)) == {"C2", "C4", "C6", "C7"}
    assert not any(x.startswith("OK") and x.split()[1] in ("C2", "C4", "C6", "C7") for x in lines)


def write_snap(path, header, objs):
    h = dict(header, format="pft-state-snapshot/1", objects_paged=len(objs))
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps(h) + "\n")
        for o in objs.values():
            fh.write(json.dumps(o) + "\n")


def write_csv(path, rows):
    import csv

    with open(path, "w", newline="") as fh:
        if rows:
            wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)


def test_main_end_to_end(tmp_path, capsys):
    w = world()
    ha, a, hb, b = w.snaps()
    write_snap(tmp_path / "a.jsonl.gz", ha, a)
    write_snap(tmp_path / "b.jsonl.gz", hb, b)
    for name, rows in (("tx", w.txs), ("payments", w.pays), ("deltas", w.deltas), ("ledgers", w.ledgers)):
        write_csv(tmp_path / f"{name}.csv", rows)
    args = [str(tmp_path / "a.jsonl.gz"), str(tmp_path / "b.jsonl.gz"), "--data", str(tmp_path)]
    assert rp.main(args + ["--json", str(tmp_path / "r.json")]) == 0
    assert "SUMMARY ok=11 mismatch=0" in capsys.readouterr().out
    assert json.loads((tmp_path / "r.json").read_text())["accounts"] == 4
    assert rp.main(args + ["--negative"]) == 1
    (tmp_path / "bad.jsonl.gz").write_bytes(gzip.compress(b"not json\n"))
    assert (
        rp.main([str(tmp_path / "bad.jsonl.gz"), str(tmp_path / "b.jsonl.gz"), "--data", str(tmp_path)]) == 2
    )
