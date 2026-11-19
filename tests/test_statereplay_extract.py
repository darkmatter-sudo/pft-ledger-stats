"""statereplay/extract_replay.py: address detection, deterministic pseudonymization, gzip determinism and
the refuse-and-write-nothing paths (bad manifest, missing ledgers)."""

import gzip
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "statereplay"))

import extract_replay as X  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "statereplay", "F1F3")
GENESIS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"  # well-known XRPL-format genesis address


def test_alphabet_is_the_xrpl_one():
    assert X.B58 == "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
    assert len(set(X.B58)) == 58


def test_is_address_accepts_valid_and_rejects_corrupt():
    assert X.is_address(GENESIS)
    assert X.is_address("rrrrrrrrrrrrrrrrrrrrrhoLvTp")  # ACCOUNT_ZERO
    assert not X.is_address(GENESIS[:-1] + ("h" if GENESIS[-1] != "h" else "j"))  # checksum
    assert not X.is_address("r0OIl" + GENESIS[5:])  # not in alphabet
    assert not X.is_address("rShort")
    assert not X.is_address("x" + GENESIS[1:])


def test_pseudonymizer_is_deterministic_and_field_aware():
    p = X.Pseudonymizer(b"k" * 32)
    obj = {
        "Account": GENESIS,
        "URI": "68747470",
        "Memos": [{"Memo": {"MemoData": "AA"}}],
        "Balance": "1",
        "Hashes": ["ABC"],
        "Note": GENESIS,
    }
    a, b = p.walk(obj), p.walk(obj)
    assert a == b
    assert a["Account"] == a["Note"] == p.account(GENESIS) and a["Account"].startswith("acct_")
    assert len(a["Account"]) == len("acct_") + 12
    assert a["URI"] == "text_" + hashlib.sha256(b"68747470").hexdigest()[:16]
    assert a["Memos"][0]["Memo"]["MemoData"].startswith("text_")
    assert a["Balance"] == "1" and a["Hashes"] == ["ABC"]
    assert X.Pseudonymizer(b"j" * 32).account(GENESIS) != a["Account"]


def test_fixture_files_hold_no_clear_address_or_free_text():
    for name in ("A.jsonl.gz", "B.jsonl.gz", "tx.jsonl.gz"):
        with gzip.open(os.path.join(FIX, name), "rt") as f:
            for line in f:
                stack = [json.loads(line)]
                while stack:
                    o = stack.pop()
                    if isinstance(o, dict):
                        for k, v in o.items():
                            if k in X.FREE_TEXT and isinstance(v, str):
                                assert v.startswith("text_"), (name, k)
                            stack.append(v)
                    elif isinstance(o, list):
                        stack.extend(o)
                    elif isinstance(o, str):
                        assert not X.is_address(o), name


def test_write_gz_is_byte_deterministic(tmp_path):
    rows = [{"b": 1, "a": [2, 3]}, {"c": "x"}]
    X.write_gz(str(tmp_path / "1.gz"), rows)
    X.write_gz(str(tmp_path / "2.gz"), rows)
    assert (tmp_path / "1.gz").read_bytes() == (tmp_path / "2.gz").read_bytes()
    assert (tmp_path / "1.gz").read_bytes()[4:8] == b"\0\0\0\0"  # gzip mtime field is 0


def make_snapshot(d, ledger, lhash, objects):
    os.makedirs(os.path.join(d, "pages"))
    header = {"ledger_index": ledger, "ledger_hash": lhash, "parent_hash": "0" * 64, "close_time_iso": "t"}
    with open(os.path.join(d, "header.json"), "w") as f:
        json.dump(header, f)
    with open(os.path.join(d, "pages", "full-0000.json"), "w") as f:
        json.dump({"ledger_hash": lhash, "marker": None, "state": objects}, f)
    man = {"files": {"header.json": {"sha256": X.sha256_file(os.path.join(d, "header.json"))}}}
    with open(os.path.join(d, "MANIFEST.json"), "w") as f:
        json.dump(man, f)


@pytest.fixture()
def raw(tmp_path):
    obj = {"Account": GENESIS, "Balance": "10", "Flags": 0, "LedgerEntryType": "AccountRoot", "index": "AA"}
    make_snapshot(str(tmp_path / "A"), 10, "H10", [obj])
    # ledger 11's skip list holds its parent's hash (derived by the replay, so B must have it)
    skip = {
        "Flags": 0,
        "Hashes": ["H10"],
        "LastLedgerSequence": 10,
        "LedgerEntryType": "LedgerHashes",
        "index": "B4979A36CDC7F3D3D5C31A4EAE2AC7D7209DDA877588B9AFC66799692AB0D66B",
    }
    make_snapshot(str(tmp_path / "B"), 11, "H11", [obj, skip])
    os.makedirs(tmp_path / "tx")
    with open(tmp_path / "tx" / "ledgers.jsonl", "w") as f:
        for n, h, p in ((10, "H10", "H9"), (11, "H11", "H10")):
            f.write(
                json.dumps(
                    {"ledger_index": n, "ledger_hash": h, "parent_hash": p, "close_time": n, "txn_count": 0}
                )
                + "\n"
            )
    with open(tmp_path / "tx" / "tx.jsonl", "w") as f:
        f.write(json.dumps({"ledger_index": 11, "transactions": []}) + "\n")
    return tmp_path


def test_extract_writes_and_replays(raw):
    out = raw / "out"
    snaps, files, p = X.extract(str(raw / "A"), str(raw / "B"), str(raw / "tx"), str(out), b"k" * 32)
    assert sorted(files) == ["A.jsonl.gz", "B.jsonl.gz", "ledgers.jsonl.gz", "snapshots.json", "tx.jsonl.gz"]
    assert p.addresses == 2 and snaps["A"]["objects"] == 1
    again = X.extract(str(raw / "A"), str(raw / "B"), str(raw / "tx"), str(raw / "out2"), b"k" * 32)[1]
    assert again == files  # same input, same bytes
    import replay

    w = replay.load_published(str(out))
    diffs, stats = replay.replay(*w)
    assert diffs == [] and stats["ledgers"] == 1


def test_manifest_mismatch_refuses_and_writes_nothing(raw):
    with open(raw / "A" / "header.json", "a") as f:
        f.write(" ")
    with pytest.raises(X.Invalid, match="sha256"):
        X.extract(str(raw / "A"), str(raw / "B"), str(raw / "tx"), str(raw / "out"), b"k" * 32)
    assert not (raw / "out").exists()


def test_missing_manifest_refuses_and_writes_nothing(raw):
    os.remove(raw / "B" / "MANIFEST.json")
    with pytest.raises(X.Invalid, match="incomplete"):
        X.extract(str(raw / "A"), str(raw / "B"), str(raw / "tx"), str(raw / "out"), b"k" * 32)
    assert not (raw / "out").exists()


def test_missing_ledger_refuses_and_writes_nothing(raw, capsys):
    with open(raw / "tx" / "ledgers.jsonl") as f:
        first = f.readline()
    with open(raw / "tx" / "ledgers.jsonl", "w") as f:
        f.write(first)
    rc = X.main(
        [
            str(raw / "A"),
            str(raw / "B"),
            str(raw / "tx"),
            "--out",
            str(raw / "out"),
            "--key-file",
            str(raw / "key"),
        ]
    )
    assert rc == 2 and "INVALID capture lacks 1 ledgers" in capsys.readouterr().out
    assert not (raw / "out").exists()


def test_key_file_is_created_private_and_reused(tmp_path):
    k = tmp_path / "key"
    a = X.load_key(str(k))
    assert oct(os.stat(k).st_mode & 0o777) == "0o600" and len(a) == 32
    assert X.load_key(str(k)) == a


def test_subset_keeps_touched_derived_and_bystanders():
    import replay

    ha, state, hb, target, rows, txs = replay.load_published(FIX)
    keep = X.subset_indexes(state, target, txs, ha["ledger_index"] + 1, hb["ledger_index"], 3)
    touched = {
        next(iter(n.values()))["LedgerIndex"]
        for r in txs
        for t in r["transactions"]
        for n in t["metaData"]["AffectedNodes"]
    }
    assert touched <= keep and replay.SKIP_KEY in keep and replay.long_skip_key(8169728) in keep
    assert len(keep - touched - {replay.SKIP_KEY, replay.NUNL_KEY, replay.long_skip_key(8169728)}) == 3
