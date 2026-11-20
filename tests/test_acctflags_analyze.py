"""acctflags extract + analyze on the synthetic world: pseudonyms, window checks, exact reconciliation,
positive controls that must fail, failure/retry matching with the retry window, offline rebuild."""

import json
import os
import shutil
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "acctflags"))

import analyze_flags as AN  # noqa: E402
import extract_flags as X  # noqa: E402
import fixture_acctflags as F  # noqa: E402

CONFIG = os.path.join(HERE, "..", "acctflags", "report.config.json")


@pytest.fixture
def pub(tmp_path):
    return F.make_pub(tmp_path)


def load(pub):
    return AN.load(str(pub))


def rewrite(path, fn):
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    rows = fn(rows)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


# --- extract -------------------------------------------------------------------------------------


def test_extract_publishes_no_address(pub):
    blob = "".join((pub / n).read_text() for n in os.listdir(pub))
    for addr in F.ADDR.values():
        assert addr not in blob
    assert F.pid("Bob") in blob and len(F.pid("Bob")) == 12
    assert X.pseudo(b"other" * 8, F.ADDR["Bob"]) != F.pid("Bob")


def test_extract_rows_and_settings(pub):
    a = {r["id"]: r for r in map(json.loads, (pub / "A.jsonl").read_text().splitlines())}
    assert a[F.pid("Bob")]["flags"] == F.RDT
    assert a[F.pid("Dave")]["settings"]["Domain"] is True and a[F.pid("Dave")]["settings"]["TickSize"] is None
    snaps = json.loads((pub / "snapshots.json").read_text())
    assert snaps["A"]["preauth"] == [] and snaps["B"]["preauth"] == [[F.pid("Carol"), F.pid("Dave")]]
    txs = [json.loads(x) for x in (pub / "txs.jsonl").read_text().splitlines()]
    assert len(txs) == 12 and [(t["ledger_index"], t["tx_index"]) for t in txs] == sorted(
        (t["ledger_index"], t["tx_index"]) for t in txs
    )
    first = txs[0]
    assert first["result"] == "tecDST_TAG_NEEDED" and first["dest_tag"] is False and first["native"] is True
    erin = [n for t in txs for n in t["nodes"] if n.get("id") == F.pid("Erin") and "before_flags" in n]
    assert erin[0]["before_flags"] == F.DXRP and erin[0]["after"]["flags"] == F.DXRP | F.DA


def test_extract_key_file_created_0600(tmp_path):
    k = tmp_path / "key"
    key = X.load_key(str(k))
    assert len(key) == 32 and (os.stat(k).st_mode & 0o777) == 0o600
    assert X.load_key(str(k)) == key


@pytest.mark.parametrize(
    "damage, match",
    [
        ("drop_ledger", "missing from the capture"),
        ("fork", "does not chain"),
        ("count", "txn_count"),
        ("manifest", "sha256 differs"),
        ("dup", "captured twice"),
        ("short", "missing from the capture"),
        ("txindex", "TransactionIndex"),
    ],
    ids=lambda v: v if " " not in v else "",  # ids without spaces, so mutants_flags.py can parse FAILED lines
)
def test_extract_refuses_damaged_input(tmp_path, damage, match):
    a, b, c = F.make_raw(tmp_path)
    if damage == "drop_ledger":
        rewrite(c / "ledgers.jsonl", lambda r: [x for x in r if x["ledger_index"] != 105])
    elif damage == "fork":
        rewrite(
            c / "ledgers.jsonl",
            lambda r: [dict(x, parent_hash="F" * 64) if x["ledger_index"] == 107 else x for x in r],
        )
    elif damage == "count":
        rewrite(
            c / "ledgers.jsonl",
            lambda r: [dict(x, txn_count=9) if x["ledger_index"] == 106 else x for x in r],
        )
    elif damage == "manifest":
        with open(b / "accounts.jsonl", "a") as fh:
            fh.write("{}\n")
    elif damage == "dup":
        rewrite(c / "tx.jsonl", lambda r: r + [r[0]])
    elif damage == "short":
        rewrite(c / "ledgers.jsonl", lambda r: r[:-1])
    elif damage == "txindex":

        def bump(r):
            r[5]["transactions"][1]["metaData"]["TransactionIndex"] = 5
            return r

        rewrite(c / "tx.jsonl", bump)
    with pytest.raises(X.Invalid, match=match):
        X.extract(str(a), str(b), str(c), F.KEY)


def test_extract_refuses_wrong_order_and_wrong_b(tmp_path):
    a, b, c = F.make_raw(tmp_path)
    with pytest.raises(X.Invalid, match="not before"):
        X.extract(str(b), str(a), str(c), F.KEY)
    hdr = json.loads((b / "header.json").read_text())
    hdr["ledger_hash"] = "9" * 64
    (b / "header.json").write_text(json.dumps(hdr))
    man = json.loads((b / "MANIFEST.json").read_text())
    man["files"]["header.json"]["sha256"] = X.hashlib.sha256((b / "header.json").read_bytes()).hexdigest()
    (b / "MANIFEST.json").write_text(json.dumps(man))
    with pytest.raises(X.Invalid, match="not snapshot B"):
        X.extract(str(a), str(b), str(c), F.KEY)


def test_extract_main_exit_2_on_damage(tmp_path, capsys):
    a, b, c = F.make_raw(tmp_path)
    rewrite(c / "ledgers.jsonl", lambda r: r[:3])
    rc = X.main([str(a), str(b), str(c), "--out", str(tmp_path / "o"), "--key-file", str(tmp_path / "k")])
    assert rc == 2 and "INVALID" in capsys.readouterr().err and not (tmp_path / "o").exists()


# --- census + reconciliation ---------------------------------------------------------------------


def test_census_counts(pub):
    d = load(pub)
    ca, cb = AN.census(d["A"]), AN.census(d["B"])
    assert ca["per_flag"] == {"DepositAuth": 1, "DisallowXRP": 1, "RequireDestTag": 1}
    assert cb["per_flag"] == {"DepositAuth": 2, "DisallowXRP": 1}
    assert ca["inbound_any"] == 3 and cb["inbound_any"] == 2
    assert cb["combinations"] == {"(none)": 3, "DepositAuth": 1, "DisallowXRP+DepositAuth": 1}
    assert ca["settings"] == {"Domain": 1} and cb["settings"] == {}
    assert AN.flag_names(0x00000001 | F.RDT) == ["RequireDestTag", "unknown_0x00000001"]


def test_reconcile_exact_terms_zero_mismatch(pub):
    rec = AN.reconcile(load(pub))
    assert rec["mismatches"] == []
    t = {r["name"]: r for r in rec["table"]}
    assert t["RequireDestTag"] | {} == dict(
        t["RequireDestTag"],
        A=1,
        set_by_tx=0,
        cleared_by_tx=1,
        created_with=0,
        deleted_with=0,
        expected_B=0,
        B=0,
    )
    assert (t["DepositAuth"]["A"], t["DepositAuth"]["set_by_tx"], t["DepositAuth"]["B"]) == (1, 1, 2)
    assert (t["Domain"]["A"], t["Domain"]["deleted_with"], t["Domain"]["expected_B"]) == (1, 1, 0)
    assert rec["accounts"] == {"A": 5, "created": 1, "deleted": 1, "B": 5, "expected_B": 5}
    assert rec["preauth"] == {"A": 0, "created": 1, "deleted": 0, "B": 1, "expected_B": 1}
    assert all(r["expected_B"] == r["B"] for r in rec["table"])


def test_positive_control_tampered_b_fails(pub):
    rewrite(pub / "B.jsonl", lambda r: [dict(x, flags=F.RDT) if x["id"] == F.pid("Bob") else x for x in r])
    rec = AN.reconcile(load(pub))
    assert f"account {F.pid('Bob')}: replayed state differs from snapshot B" in rec["mismatches"]
    assert "flag RequireDestTag: expected_B 0 != B 1" in rec["mismatches"]


def test_positive_control_dropped_transaction_fails(pub):
    rewrite(
        pub / "txs.jsonl", lambda r: [x for x in r if x["type"] != "AccountSet" or x["ledger_index"] != 107]
    )
    rec = AN.reconcile(load(pub))
    # Bob's next node (ledger 108) says Flags 0 before, the replay still has RequireDestTag: caught there
    # even though the end state and the per-flag totals happen to agree again
    assert any(
        m.startswith("ledger 108 tx 0: account")
        and "PreviousFields Flags 0x00000000 != replayed 0x00020000" in m
        for m in rec["mismatches"]
    )
    d = load(pub)
    assert any("txn_count" in p for p in AN.check_window(d))


def test_reconcile_detects_bad_previousfields_and_unknown_nodes(pub):
    def bad(rows):
        for t in rows:
            for n in t["nodes"]:
                if n.get("before_flags") == F.DXRP:
                    n["before_flags"] = 0
        return rows

    rewrite(pub / "txs.jsonl", bad)
    rec = AN.reconcile(load(pub))
    assert any("PreviousFields Flags 0x00000000 != replayed 0x00080000" in m for m in rec["mismatches"])


def test_reconcile_detects_created_existing_and_deleted_unknown(pub):
    def dup(rows):
        for t in rows:
            for n in t["nodes"]:
                if n.get("kind") == "CreatedNode" and n["node"] == "AccountRoot":
                    n["id"] = F.pid("Alice")
                if n.get("kind") == "DeletedNode":
                    n["id"] = "000000000000"
        return rows

    rewrite(pub / "txs.jsonl", dup)
    rec = AN.reconcile(load(pub))
    assert any("CreatedNode for existing account" in m for m in rec["mismatches"])
    assert any("DeletedNode for unknown account" in m for m in rec["mismatches"])
    assert any(m.startswith("accounts: expected_B") for m in rec["mismatches"])


def test_reconcile_detects_preauth_mismatch(pub):
    snaps = json.loads((pub / "snapshots.json").read_text())
    snaps["B"]["preauth"] = []
    (pub / "snapshots.json").write_text(json.dumps(snaps))
    rec = AN.reconcile(load(pub))
    assert "DepositPreauth pairs: replayed set differs from snapshot B" in rec["mismatches"]
    assert "DepositPreauth: expected_B 1 != B 0" in rec["mismatches"]


def test_check_window_detects_gap_and_fork(pub):
    rewrite(pub / "ledgers.jsonl", lambda r: [x for x in r if x["ledger_index"] != 104])
    probs = AN.check_window(load(pub))
    assert any("not exactly 101..110" in p for p in probs) and any("does not chain" in p for p in probs)


# --- failures + retries --------------------------------------------------------------------------


def test_failures_and_fixes_with_window_100(pub):
    fl = AN.failures(load(pub), 100)
    assert fl["payments"] == 8 and fl["failed"] == 4
    assert fl["fixes"] == {"dest_tag_fix": 1, "flag_cleared": 1, "preauth_fix": 1}
    assert fl["censored"] == 1 and fl["not_retried"] == 0
    grid = {(g["result"], g["dest_state"]): g["count"] for g in fl["by_code_and_dest_state"]}
    assert grid == {
        ("tecDST_TAG_NEEDED", "RequireDestTag"): 2,
        ("tecNO_PERMISSION", "DepositAuth"): 1,
        ("tecNO_PERMISSION", "DepositAuth+DisallowXRP"): 1,
    }
    after = {f["fix"]: f["retry_after_ledgers"] for f in fl["listed"] if f["fix"]}
    assert after == {"dest_tag_fix": 2, "preauth_fix": 3, "flag_cleared": 2}


def test_retry_window_is_inclusive_and_enforced(pub):
    d = load(pub)
    w2 = AN.failures(d, 2)
    assert w2["fixes"] == {"dest_tag_fix": 1, "flag_cleared": 1}  # preauth retry is 3 ledgers later
    w1 = AN.failures(d, 1)
    assert w1["retried"] == 0
    with pytest.raises(ValueError):
        AN.failures(d, 0)


def test_not_retried_vs_censored(pub):
    d = load(pub)
    d["txs"] = [t for t in d["txs"] if not (t["ledger_index"] == 103)]
    fl = AN.failures(d, 3)
    first = [f for f in fl["listed"] if f["ledger"] == 101][0]
    assert first["fix"] is None and first["censored"] is False and fl["not_retried"] == 1
    last = [f for f in fl["listed"] if f["ledger"] == 110][0]
    assert last["censored"] is True


def test_retry_must_be_later_and_same_pair(pub):
    d = load(pub)
    for t in d["txs"]:
        if t["ledger_index"] == 103:
            t["destination"] = F.pid("Carol")
    fl = AN.failures(d, 100)
    assert "dest_tag_fix" not in fl["fixes"]


def test_dest_state_missing_and_preauthorized():
    st = {"c": {"flags": F.DA, "settings": {}}}
    assert AN.dest_state(st, set(), "x", "s") == "missing"
    assert AN.dest_state(st, {("c", "s")}, "c", "s") == "DepositAuth+sender_preauthorized"
    assert AN.dest_state({"c": {"flags": 0, "settings": {}}}, set(), "c", "s") == "no_inbound_flag"


# --- report --------------------------------------------------------------------------------------


def test_report_build_deterministic_and_offline(pub, tmp_path, monkeypatch):
    out1, out2 = tmp_path / "r1", tmp_path / "r2"
    assert AN.main(["--data", str(pub), "--config", CONFIG, "--out", str(out1)]) == 0
    # no_network() replaces all three; restore every one, or later tests in the same process lose sockets
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, getattr(socket, name))
    assert AN.main(["--data", str(pub), "--config", CONFIG, "--out", str(out2), "--offline"]) == 0
    with pytest.raises(OSError, match="offline"):
        socket.create_connection(("example.org", 80))
    with pytest.raises(OSError, match="offline"):
        socket.getaddrinfo("example.org", 80)
    monkeypatch.undo()
    assert socket.getaddrinfo("localhost", 80)
    for f in ("REPORT.md", "report.json", "charts/flags.png", "charts/payments.png"):
        assert (out1 / f).read_bytes() == (out2 / f).read_bytes(), f
    text = (out1 / "REPORT.md").read_text()
    assert "Retry window: **100 validated ledgers**" in text and "**Mismatches: 0**" in text
    assert "| dest_tag_fix | 1 |" in text and "| preauth_fix | 1 |" in text


def test_report_window_override_and_mismatch_exit(pub, tmp_path):
    out = tmp_path / "r"
    assert (
        AN.main(
            ["--data", str(pub), "--config", CONFIG, "--out", str(out), "--retry-window", "2", "--no-charts"]
        )
        == 0
    )
    assert "Retry window: **2 validated ledgers**" in (out / "REPORT.md").read_text()
    assert json.loads((out / "report.json").read_text())["failures"]["window"] == 2
    rewrite(pub / "B.jsonl", lambda r: r[1:])
    assert AN.main(["--data", str(pub), "--config", CONFIG, "--out", str(out), "--no-charts"]) == 1
    assert "**Mismatches: 0**" not in (out / "REPORT.md").read_text()


def test_committed_config_pins_100():
    with open(CONFIG) as fh:
        assert json.load(fh) == {"retry_window_ledgers": 100}


def test_report_crosscheck_section(pub, tmp_path):
    for k, neg in (("A", False), ("B", False), ("A", True), ("B", True)):
        res = [{"check": "header", "ok": not neg}] + [
            {"check": "account", "id": f"{i:012x}", "ok": not neg, "in_snapshot": True} for i in range(20)
        ]
        name = f"crosscheck-{k}{'-negative' if neg else ''}.json"
        (pub / name).write_text(json.dumps({"ledger_index": 100, "negative": neg, "results": res}))
    out = tmp_path / "r"
    AN.main(["--data", str(pub), "--config", CONFIG, "--out", str(out), "--no-charts"])
    text = (out / "REPORT.md").read_text()
    assert "| A | 100 | 20 | 21 | 21 | 0 | 21 of 20 |" in text


def test_malformed_published_input(pub, tmp_path):
    (pub / "txs.jsonl").write_text("{not json\n")
    with pytest.raises(ValueError):
        AN.load(str(pub))
    shutil.rmtree(pub)
    with pytest.raises(FileNotFoundError):
        AN.load(str(pub))


def test_created_account_with_flag_is_counted(pub):
    """An account created WITH a flag: created_with must carry it, or expected_B misses it."""
    new_id = "f00f00f00f00"

    def add_tx(rows):
        rows.append(
            {
                "ledger_index": 110,
                "tx_index": 1,
                "type": "Payment",
                "result": "tesSUCCESS",
                "sender": F.pid("Alice"),
                "destination": new_id,
                "dest_tag": False,
                "native": True,
                "nodes": [
                    {
                        "node": "AccountRoot",
                        "kind": "CreatedNode",
                        "id": new_id,
                        "after": {"flags": F.RDT, "settings": {}},
                    }
                ],
            }
        )
        return rows

    rewrite(pub / "txs.jsonl", add_tx)
    rewrite(
        pub / "B.jsonl",
        lambda r: sorted(r + [{"id": new_id, "flags": F.RDT, "settings": {}}], key=lambda x: x["id"]),
    )
    rec = AN.reconcile(load(pub))
    t = {r["name"]: r for r in rec["table"]}
    assert t["RequireDestTag"]["created_with"] == 1 and t["RequireDestTag"]["expected_B"] == 1
    assert rec["mismatches"] == []


def test_earlier_success_is_not_a_retry(pub):
    """A success for the same pair BEFORE the failure (same window) must not be matched as its retry."""
    d = load(pub)
    early = {
        "ledger_index": 105,
        "tx_index": 2,
        "type": "Payment",
        "result": "tesSUCCESS",
        "sender": F.pid("Erin"),
        "destination": F.pid("Bob"),
        "dest_tag": True,
        "native": True,
        "nodes": [],
    }
    d["txs"].append(early)
    fl = AN.failures(d, 100)
    erin = [f for f in fl["listed"] if f["ledger"] == 106][0]
    assert erin["retry_ledger"] == 108 and erin["retry_after_ledgers"] == 2 and erin["fix"] == "flag_cleared"


def test_build_reports_window_gap_as_mismatch(pub, tmp_path):
    rewrite(pub / "ledgers.jsonl", lambda r: [x for x in r if x["ledger_index"] != 102])
    r = AN.build(load(pub), 100)
    assert any("not exactly 101..110" in m for m in r["reconcile"]["mismatches"])
    out = tmp_path / "r"
    assert AN.main(["--data", str(pub), "--config", CONFIG, "--out", str(out), "--no-charts"]) == 1
