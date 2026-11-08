"""Tests for dormancy/anchors_dormancy.py and dormancy/analyze_dormancy.py."""

import csv
import gzip
import json
import os
import random
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dormancy"))

import analyze_dormancy as A  # noqa: E402
import anchors_dormancy as N  # noqa: E402
import lib_dormancy as L  # noqa: E402

DAY = L.DAY


def acct(a, bal, seq, txid, oc=0):
    return {
        "LedgerEntryType": "AccountRoot",
        "Account": a,
        "Balance": str(bal),
        "OwnerCount": oc,
        "PreviousTxnLgrSeq": seq,
        "PreviousTxnID": txid,
    }


def write_snap(path, ledger, close, objs, total=None):
    bal = sum(int(o["Balance"]) for o in objs if o["LedgerEntryType"] == "AccountRoot")
    hdr = {
        "ledger_index": ledger,
        "ledger_hash": f"H{ledger}",
        "close_time": close,
        "objects_paged": len(objs),
        "total_coins": bal if total is None else total,
        "server_state_reserve_base": 10,
        "server_state_reserve_inc": 2,
    }
    with gzip.open(path, "wt") as fh:
        fh.write(json.dumps(hdr) + "\n")
        for o in objs:
            fh.write(json.dumps(o) + "\n")
    return str(path)


def write_anchors(path, seqs):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ledger_index", "ledger_hash", "close_time", "files"])
        for s in seqs:
            w.writerow([s, f"H{s}", 10 * s, 1])
    return str(path)


def write_headers(path, seqs):
    with open(path, "w") as fh:
        for s in seqs:
            fh.write(json.dumps({"ledger_index": s, "ledger_hash": f"H{s}", "close_time": 10 * s}) + "\n")
    return str(path)


# ---------------------------------------------------------------- formatting


@pytest.mark.parametrize(
    "drops,text",
    [
        (0, "0.000000"),
        (1, "0.000001"),
        (1_000_000, "1.000000"),
        (99999999967938708, "99,999,999,967.938708"),
        (-1, "-0.000001"),
    ],
)
def test_pft_is_exact(drops, text):
    assert A.pft(drops) == text


@pytest.mark.parametrize(
    "n,label", [(0, "0"), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6-20"), (20, "6-20"), (21, ">20")]
)
def test_owner_count_buckets(n, label):
    assert A.oc_bucket(n) == label


# ---------------------------------------------------------------- anchors


def test_select_keeps_every_lookup_identical():
    rng = random.Random(7)
    anchors = {s: (10 * s, f"H{s}") for s in sorted(rng.sample(range(1000, 50000), 400))}
    headers = {s: (10 * s, f"H{s}") for s in rng.sample(range(40000, 60001), 50)}
    headers[60000] = (600000, "H60000")
    want = set(rng.sample(range(1, 60001), 2000)) | {60000, min(anchors), 999}
    kept = N.select(anchors, headers, want)
    assert len(kept) < len(anchors)
    full, small = L.TimeMap({**anchors, **headers}), L.TimeMap({**kept, **headers})
    for s in want:
        assert full.lookup(s) == small.lookup(s), s
    assert min(anchors) in kept  # oldest anchor always kept ('before' bound)


def test_select_mutant_dropping_upper_neighbour_changes_lookups():
    # control for the test above: keeping only the lower neighbour must change some interp answers
    anchors = {100: (1000, "a"), 200: (2000, "b"), 300: (3000, "c")}
    headers = {400: (4000, "d")}
    kept = {100: anchors[100]}  # what a select() without the upper neighbour would keep for seq 150
    assert L.TimeMap({**anchors, **headers}).lookup(150) != L.TimeMap({**kept, **headers}).lookup(150)


def git_repo(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".gz"):
            with gzip.open(p, "wt") as fh:
                fh.write(text)
        else:
            p.write_text(text)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True, env=env, stdin=subprocess.DEVNULL)
    return str(tmp_path)


HDR = "ledger_index,ledger_hash,close_time\n"


def test_tracked_files_and_collect(tmp_path):
    repo = git_repo(
        tmp_path,
        {
            "a/data/ledgers.csv": HDR + "10,h10,100\n11,h11,110\n",
            "b/data/ledgers.csv.gz": HDR + "11,H11,110\n12,H12,120\n",
            "c/data/fees-ledgers.csv": "ledger_index,ledger_hash,fee_drops\n13,H13,1\n",
            "c/data/drill-1/ledgers.csv": HDR + "99,BAD,1\n",
            "d/other.csv": HDR + "98,BAD,1\n",
        },
    )
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked" / "ledgers.csv").write_text(HDR + "97,BAD,1\n")
    files = N.tracked_ledger_files(repo)
    assert files == ["a/data/ledgers.csv", "b/data/ledgers.csv.gz", "c/data/fees-ledgers.csv"]
    anchors, holders, report = N.collect(repo, files)
    assert anchors == {10: (100, "H10"), 11: (110, "H11"), 12: (120, "H12")}  # hash case folded
    assert holders[11] == 2
    assert report[2][1].startswith("skipped")


def test_collect_conflict_is_an_error(tmp_path):
    repo = git_repo(tmp_path, {"a/ledgers.csv": HDR + "10,H10,100\n", "b/ledgers.csv": HDR + "10,H10,101\n"})
    with pytest.raises(ValueError, match="anchor conflict at 10"):
        N.collect(repo, N.tracked_ledger_files(repo))


# ---------------------------------------------------------------- analyzer end to end


def fixture(tmp_path, b_objs=None):
    # ledger n closes at 10*n s. Anchors 1000..1100 (sparse), headers near the snapshots.
    anchors = write_anchors(tmp_path / "anchors.csv", [1000, 1050, 1100])
    a_led, b_led = 200000, 208640  # B is 86400 s after A
    headers = write_headers(tmp_path / "headers.jsonl", [199000, 199990, a_led, 208500, b_led])
    a_objs = [
        acct("rOld", 5_000_000, 900, "T0"),  # before the oldest anchor: >=14d by upper bound
        acct("rInterp", 40, 1075, "T1", oc=2),  # interp between 1050 and 1100
        acct("rDay", 30, 199000, "T2", oc=1),  # 10000 s -> 1h-1d
        acct("rNow", 20, 199990, "T3", oc=25),  # 100 s -> <1h, reserve capped at balance
        acct("rGone", 10, 199990, "T4"),
    ]
    a = write_snap(tmp_path / "A.jsonl.gz", a_led, 10 * a_led, a_objs)
    if b_objs is None:
        b_objs = [
            acct("rOld", 5_000_000, 900, "T0"),
            acct("rInterp", 40, 1075, "T1", oc=2),
            acct("rDay", 31, 208500, "U2", oc=1),  # touched inside (A, B]
            acct("rNow", 20, 199990, "T3", oc=25),  # untouched, ages from <1h to 1d-7d
            acct("rNew", 9, b_led, "U5"),  # created
        ]
    b = write_snap(tmp_path / "B.jsonl.gz", b_led, 10 * b_led, b_objs)
    return a, b, anchors, headers


def test_analyze_two_snapshots_reconciles(tmp_path):
    a, b, anchors, headers = fixture(tmp_path)
    res = A.analyze([a, b], anchors, headers, top_n=2)
    sa, sb = res["snapshots"]
    assert A.all_ok(res)
    assert sa["timemap"]["kinds"] == {"exact": 3, "interp": 1, "before": 1}
    assert sa["buckets"]["<1h"]["accounts"] == 2 and sa["buckets"]["1h-1d"]["accounts"] == 1
    assert sa["buckets"][">=14d"]["accounts"] == 2  # rOld (before) + rInterp (interp, ~23 d old)
    assert sa["buckets"]["<1h"]["reserve_drops"] == 20 + 10  # rNow capped at 20, rGone base 10
    assert sa["owner_count"]["<1h|>20"] == 1
    assert [t["account"] for t in sa["top"]] == ["rOld", "rInterp"]
    tr = res["transitions"]
    assert (tr["touched"], tr["untouched"], tr["created"], tr["deleted"]) == (1, 3, 1, 1)
    assert tr["matrix"]["<1h|1d-7d"] == 1 and tr["matrix"]["<1h|deleted"] == 1
    assert tr["matrix"]["1h-1d|<1h"] == 1 and tr["matrix"]["created|<1h"] == 1
    assert all(c["ok"] for c in tr["checks"])


def test_analyze_flags_inconsistent_b(tmp_path):
    bad = [
        acct("rOld", 5_000_001, 900, "T0"),  # same PreviousTxnID but balance changed
        acct("rInterp", 40, 1075, "T1", oc=2),
        acct("rDay", 30, 199000, "T2", oc=1),
        acct("rNow", 20, 199990, "T3", oc=25),
        acct("rGone", 10, 199990, "T4"),
    ]
    a, b, anchors, headers = fixture(tmp_path, b_objs=bad)
    res = A.analyze([a, b], anchors, headers)
    assert not A.all_ok(res)
    assert [c["name"] for c in res["transitions"]["checks"] if not c["ok"]] == ["invariants"]


def test_main_writes_outputs_and_fails_on_total_coins(tmp_path):
    a, b, anchors, headers = fixture(tmp_path)
    out = tmp_path / "rep"
    assert (
        A.main(["--snap", a, "--snap", b, "--anchors", anchors, "--headers", headers, "--out", str(out)]) == 0
    )
    data = json.loads((out / "dormancy.json").read_text())
    assert "_rows" not in data["snapshots"][0]
    assert "Overall: PASS" in (out / "REPORT.md").read_text()
    # a snapshot whose total_coins is off by one drop must fail the run
    hdr_objs = [acct("rX", 5, 199990, "Z")]
    a2 = write_snap(tmp_path / "A2.jsonl.gz", 200000, 2000000, hdr_objs, total=6)
    assert A.main(["--snap", a2, "--anchors", anchors, "--headers", headers, "--out", str(out)]) == 1
    assert "| total_coins | FAIL |" in (out / "REPORT.md").read_text()


def test_snapshot_order_enforced(tmp_path):
    a, b, anchors, headers = fixture(tmp_path)
    with pytest.raises(ValueError, match="older than B"):
        A.analyze([b, a], anchors, headers)


def test_anchor_header_disagreement_refused(tmp_path):
    a, _, anchors, _ = fixture(tmp_path)
    with open(tmp_path / "h2.jsonl", "w") as fh:
        fh.write(json.dumps({"ledger_index": 1050, "ledger_hash": "H1050", "close_time": 10501}) + "\n")
        fh.write(json.dumps({"ledger_index": 200000, "ledger_hash": "H200000", "close_time": 2000000}) + "\n")
    with pytest.raises(ValueError, match="differs"):
        A.analyze([a], anchors, str(tmp_path / "h2.jsonl"))


def test_charts_written(tmp_path):
    pytest.importorskip("matplotlib")
    a, b, anchors, headers = fixture(tmp_path)
    out = tmp_path / "rep"
    rc = A.main(
        ["--snap", a, "--snap", b, "--anchors", anchors, "--headers", headers, "--out", str(out), "--charts"]
    )
    assert rc == 0
    names = {
        "accounts_A.png",
        "balance_A.png",
        "accounts_B.png",
        "balance_B.png",
        "topk_share.png",
        "transitions.png",
    }
    assert names <= set(os.listdir(out))
    assert all((out / n).stat().st_size > 1000 for n in names)
    assert all(f"![{n}]({n})" in (out / "REPORT.md").read_text() for n in names)
