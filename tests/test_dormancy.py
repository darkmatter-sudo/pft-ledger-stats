"""Tests for dormancy/: bucket boundaries, time-map bounds, Gini, header fetcher resume, transitions."""

import gzip
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dormancy"))

import headers_dormancy as H  # noqa: E402
import lib_dormancy as L  # noqa: E402

DAY, HOUR = L.DAY, L.HOUR


# ---------------------------------------------------------------- buckets


@pytest.mark.parametrize(
    "age,label",
    [
        (0, "<1h"),
        (HOUR - 1, "<1h"),
        (HOUR, "1h-1d"),
        (DAY - 1, "1h-1d"),
        (DAY, "1d-7d"),
        (7 * DAY - 1, "1d-7d"),
        (7 * DAY, "7d-14d"),
        (14 * DAY - 1, "7d-14d"),
        (14 * DAY, ">=14d"),
        (10**9, ">=14d"),
    ],
)
def test_bucket_boundaries(age, label):
    assert L.bucket_of(age) == label


def test_bucket_negative_age_refused():
    with pytest.raises(ValueError):
        L.bucket_of(-1)


def test_interval_buckets():
    assert L.buckets_for_interval(10, 20) == ["<1h"]
    assert L.buckets_for_interval(HOUR - 1, HOUR) == ["<1h", "1h-1d"]
    assert L.buckets_for_interval(HOUR, HOUR) == ["1h-1d"]  # [lo, hi): exactly 1h is 1h-1d
    assert L.buckets_for_interval(15 * DAY, None) == [">=14d"]
    assert L.buckets_for_interval(13 * DAY, None) == ["7d-14d", ">=14d"]
    assert L.buckets_for_interval(0, None) == L.LABELS


# ---------------------------------------------------------------- time map


def tmap():
    return L.TimeMap({100: (1000, "A"), 200: (2000, "B"), 300: (2000, "C")})


def test_timemap_exact_interp_before():
    tm = tmap()
    assert tm.lookup(200) == ("exact", 2000, 2000, 2000)
    assert tm.lookup(150) == ("interp", 1000, 1500.0, 2000)
    assert tm.lookup(250) == ("interp", 2000, 2000.0, 2000)  # equal close times: zero-width bound
    assert tm.lookup(99) == ("before", None, None, 1000)


def test_timemap_never_extrapolates():
    with pytest.raises(ValueError):
        tmap().lookup(301)


def test_timemap_rejects_backwards_times():
    with pytest.raises(ValueError):
        L.TimeMap({1: (10, "a"), 2: (9, "b")})


def test_interp_bound_contains_truth():
    # 'true' close times: irregular steps; the map only knows every 50th ledger
    truth = {}
    t = 0
    for s in range(1, 1001):
        t += 3 + (s * 7919) % 5
        truth[s] = t
    known = {s: (truth[s], str(s)) for s in range(1, 1001, 50)}
    known[1000] = (truth[1000], "end")
    tm = L.TimeMap(known)
    for s in range(1, 1001):
        kind, lo, _pt, hi = tm.lookup(s)
        assert lo <= truth[s] <= hi, (s, kind)


def test_anchor_conflict_is_an_error(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("ledger_index,ledger_hash,close_time\n5,AA,100\n")
    b.write_text("ledger_index,ledger_hash,ledger_time\n5,AA,101\n")
    with pytest.raises(ValueError):
        L.load_anchor_csvs([str(a), str(b)])
    b.write_text("ledger_index,ledger_hash,ledger_time\n5,AA,100\n6,BB,104\n")
    assert L.load_anchor_csvs([str(a), str(b)]) == {5: (100, "AA"), 6: (104, "BB")}


def test_headers_jsonl_ignores_torn_tail(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text('{"ledger_index": 1, "close_time": 5, "ledger_hash": "X"}\n{"ledger_index": 2, "clo')
    assert L.load_headers_jsonl(str(p)) == {1: (5, "X")}


# ---------------------------------------------------------------- gini


def test_gini_known_values():
    assert L.gini([]) == 0.0
    assert L.gini([0, 0]) == 0.0
    assert L.gini([5, 5, 5, 5]) == 0.0
    assert L.gini([0, 0, 0, 10]) == pytest.approx(0.75)  # (n-1)/n
    assert L.gini([1, 2, 3, 4]) == pytest.approx(0.25)


# ---------------------------------------------------------------- snapshots / reconcile / transitions


def acct(a, bal, seq, txid, oc=0):
    return {
        "LedgerEntryType": "AccountRoot",
        "Account": a,
        "Balance": str(bal),
        "OwnerCount": oc,
        "PreviousTxnLgrSeq": seq,
        "PreviousTxnID": txid,
    }


def header(ledger, close, objs, total=None):
    bal = sum(int(o["Balance"]) for o in objs if o["LedgerEntryType"] == "AccountRoot")
    return {
        "ledger_index": ledger,
        "close_time": close,
        "objects_paged": len(objs),
        "total_coins": bal if total is None else total,
        "server_state_reserve_base": 10,
        "server_state_reserve_inc": 2,
    }


def fixture_maps():
    # ledger n closes at 10*n seconds, known exactly for 1000..20000, nothing older
    return L.TimeMap({s: (10 * s, f"H{s}") for s in range(1000, 20001)})


def test_classify_and_reconcile():
    tm = fixture_maps()
    objs = [
        acct("rA", 100, 20000, "T1", oc=3),  # exact
        acct("rB", 5, 19000, "T2", oc=10),  # exact, reserve capped at balance
        acct("rC", 7, 900, "T3"),  # before the map: bucket from the upper bound only
        {"LedgerEntryType": "FeeSettings", "ReserveBase": 10, "ReserveIncrement": 2},
        {"LedgerEntryType": "Escrow", "Amount": "50"},
        {"LedgerEntryType": "PayChannel", "Amount": "30", "Balance": "10"},
    ]
    hdr = header(20000, 200000 + 14 * DAY, objs, total=100 + 5 + 7 + 50 + 20)
    # make rC's upper bound (t=10000) >= 14d old at snapshot time
    tm = L.TimeMap({**tm.headers, 20001: (200000 + 14 * DAY, "S")})
    hdr["ledger_index"] = 20001
    rows = L.classify_accounts(hdr, objs, tm)
    by = {r["account"]: r for r in rows}
    assert by["rC"]["kind"] == "before" and by["rC"]["bucket"] == ">=14d"
    assert by["rB"]["reserve"] == 5  # min(balance, 10 + 2*10)
    assert by["rA"]["reserve"] == 16
    t = L.bucket_table(rows)
    checks = dict((n, ok) for n, ok, _ in L.reconcile(hdr, objs, rows, t))
    assert all(checks.values()), checks
    hdr["total_coins"] += 1
    assert not dict((n, ok) for n, ok, _ in L.reconcile(hdr, objs, rows, t))["total_coins"]


def test_prev_seq_after_snapshot_refused():
    tm = fixture_maps()
    objs = [acct("rA", 1, 20000, "T")]
    hdr = header(19999, 199990, objs)
    with pytest.raises(ValueError):
        L.classify_accounts(hdr, objs, tm)


def rows_for(objs, ledger):
    tm = fixture_maps()
    hdr = header(ledger, 10 * ledger, objs)
    return L.classify_accounts(hdr, objs, tm)


def test_transitions_matrix_and_invariants():
    a = rows_for([acct("r1", 10, 1000, "X1"), acct("r2", 10, 19000, "X2"), acct("r3", 4, 1500, "X3")], 19500)
    b = rows_for([acct("r1", 10, 1000, "X1"), acct("r2", 12, 19900, "Y2"), acct("r4", 9, 19800, "Y4")], 20000)
    tr = L.transitions(a, b, 19500, 20000)
    assert tr["problems"] == []
    assert tr["untouched"] == 1 and tr["touched"] == 1
    # ledger n closes at 10*n: r1 age 185000 s / 190000 s, r2 5000 s -> 1000 s, r3 deleted, r4 2000 s
    assert tr["matrix"]["1d-7d|1d-7d"] == 1
    assert tr["matrix"]["1h-1d|<1h"] == 1
    assert tr["matrix"]["1d-7d|deleted"] == 1
    assert tr["matrix"]["created|<1h"] == 1
    assert sum(v for k, v in tr["matrix"].items() if k.endswith("|deleted")) == 1
    assert sum(v for k, v in tr["matrix"].items() if k.startswith("created|")) == 1
    assert sum(tr["matrix"].values()) == 4  # 3 from A + 1 created


def test_transitions_catch_inconsistent_accounts():
    a = rows_for([acct("r1", 10, 1000, "X1"), acct("r2", 10, 1000, "X2")], 19500)
    b = rows_for([acct("r1", 11, 1000, "X1"), acct("r2", 10, 19400, "Z2"), acct("r5", 1, 1200, "Q")], 20000)
    probs = L.transitions(a, b, 19500, 20000)["problems"]
    assert len(probs) == 3
    assert any("same PreviousTxnID" in p for p in probs)
    assert any("outside" in p for p in probs)
    assert any("created" in p for p in probs)


# ---------------------------------------------------------------- header fetcher


class FakeRpc:
    def __init__(self, ranges, missing=(), transient_first=0, close=lambda s: 10 * s):
        self.ranges, self.missing, self.transient_left, self.close = (
            ranges,
            set(missing),
            transient_first,
            close,
        )
        self.count = 0
        self.asked = []

    def call(self, method, params=None):
        self.count += 1
        if method == "server_info":
            return {"info": {"complete_ledgers": self.ranges}}
        assert method == "ledger"
        if self.transient_left:
            self.transient_left -= 1
            raise H.Transient("tooBusy")
        s = params["ledger_index"]
        self.asked.append(s)
        if s in self.missing:
            raise LookupError("lgrNotFound")
        return {
            "validated": True,
            "ledger": {
                "ledger_index": str(s),
                "ledger_hash": f"H{s}",
                "close_time": self.close(s),
                "parent_close_time": self.close(s) - 3,
            },
        }


def nosleep(_s):
    pass


def test_parse_ranges():
    assert H.parse_ranges("1-5,7,9-10") == [(1, 5), (7, 7), (9, 10)]
    assert H.parse_ranges("empty") == []


def test_fetch_sources_ranges_and_fallback(tmp_path):
    out = str(tmp_path / "h.jsonl")
    pub = FakeRpc("100-200", missing={150})
    loc = FakeRpc("140-300")
    st = H.fetch([("public", pub), ("local", loc)], {50, 120, 150, 250}, out, sleep=nosleep)
    assert st["fetched"] == 3 and st["pruned"] == 1 and st["lgrNotFound"] == 1
    assert st["by_source"] == {"public": 1, "local": 2}
    assert 50 not in pub.asked and 50 not in loc.asked  # never asked outside complete_ledgers
    assert set(L.load_headers_jsonl(out)) == {120, 150, 250}


def test_fetch_retries_transient_with_backoff(tmp_path):
    slept = []
    st = H.fetch(
        [("public", FakeRpc("1-10", transient_first=2))],
        {5},
        str(tmp_path / "h"),
        retries=3,
        backoff=0.5,
        sleep=slept.append,
    )
    assert st["fetched"] == 1 and st["retries"] == 2 and slept == [0.5, 1.0]


def test_fetch_gives_up_after_retries(tmp_path):
    st = H.fetch(
        [("public", FakeRpc("1-10", transient_first=9))],
        {5},
        str(tmp_path / "h"),
        retries=2,
        backoff=0,
        sleep=nosleep,
    )
    assert st["fetched"] == 0 and st["pruned"] == 1


def test_fetch_resume_cuts_torn_tail_and_skips_done(tmp_path):
    out = tmp_path / "h.jsonl"
    full = str(tmp_path / "ref.jsonl")
    H.fetch([("public", FakeRpc("1-10"))], {1, 2, 3, 4}, full, sleep=nosleep)
    lines = open(full).read().splitlines(keepends=True)
    out.write_text(lines[0] + lines[1] + lines[2][:7])  # SIGKILL mid-line 3
    rpc = FakeRpc("1-10")
    st = H.fetch([("public", rpc)], {1, 2, 3, 4}, str(out), sleep=nosleep)
    assert st["torn_bytes"] == 7 and st["already"] == 2 and rpc.asked == [3, 4]
    assert out.read_bytes() == open(full, "rb").read()


def test_fetch_stop_flag(tmp_path):
    H.STOP["flag"] = True
    try:
        st = H.fetch([("public", FakeRpc("1-10"))], {1, 2}, str(tmp_path / "h"), sleep=nosleep)
    finally:
        H.STOP["flag"] = False
    assert st.get("interrupted") and st["fetched"] == 0


def test_http_rpc_refuses_write_methods():
    with pytest.raises(ValueError):
        H.HttpRpc("https://rpc.invalid").call("submit", {})


def test_wanted_seqs(tmp_path):
    p = tmp_path / "s.jsonl.gz"
    objs = [acct("rA", 1, 7, "T"), {"LedgerEntryType": "DirectoryNode", "PreviousTxnLgrSeq": 99}]
    with gzip.open(p, "wt") as fh:
        fh.write(json.dumps(header(10, 100, objs)) + "\n")
        for o in objs:
            fh.write(json.dumps(o) + "\n")
    assert H.wanted_seqs([str(p)]) == {7, 10}
