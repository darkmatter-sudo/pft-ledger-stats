import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hashverify"))
import report_hashverify as rep  # noqa: E402
import verify_headers as vh  # noqa: E402
import verify_txtree as vt  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "hashverify")
HV, TV = os.path.join(FIX, "headers_vectors.jsonl"), os.path.join(FIX, "txtree_vectors.jsonl")


def build(d, capsys):
    vh.main(["--offline", HV, "--out", str(d / "headers-1")])
    vh.main(["--offline", HV, "--out", str(d / "headers-2")])
    vt.main(["--offline", TV, "--out", str(d / "public-3")])
    vh.main(
        [
            "--offline",
            HV,
            "--out",
            str(d / "tamper-1" / "headers-coins"),
            "--tamper",
            "public:7806601:total_coins",
        ]
    )
    vt.main(["--offline", TV, "--out", str(d / "tamper-1" / "txtree-blob"), "--tamper", "7806332"])
    capsys.readouterr()


def test_report_lists_every_control_finding_with_both_hashes(tmp_path, capsys):
    build(tmp_path, capsys)
    assert rep.main(["--data", str(tmp_path), "--out", str(tmp_path)]) == 0
    md = (tmp_path / "REPORT.md").read_text()
    assert "(`headers-2`)" in md and "(`public-3`)" in md and "(`tamper-1`)" in md
    rows = [ln for ln in md.splitlines() if ln.startswith(("| headers-coins |", "| txtree-blob |"))]
    kinds = [
        ln.split("|")[4].strip() for ln in rows if ln.startswith("| headers-coins |") and ln.count("|") > 6
    ]
    assert sorted(kinds) == ["disagree", "hash", "parent link"]
    blob = [ln for ln in rows if ln.startswith("| txtree-blob |") and "7806332" in ln]
    assert len(blob) == 1 and "txids_ok,root_ok" in blob[0]
    for ln in rows:
        if ln.count("|") > 6:
            hexes = [c.strip() for c in ln.split("|") if len(c.strip()) == 64]
            assert len(hexes) == 2 and hexes[0] != hexes[1], ln
    with open(tmp_path / "counts.csv") as fh:
        c = {(r["run"], r["metric"]): r["value"] for r in csv.DictReader(fh)}
    assert c[("headers-2", "findings")] == "0" and c[("public-3", "ledgers_checked")] == "7"
    assert c[("tamper-1/headers-coins", "findings")] == "3" and c[("tamper-1/txtree-blob", "findings")] == "1"


def test_report_needs_runs(tmp_path):
    assert rep.main(["--data", str(tmp_path), "--out", str(tmp_path)]) == 2
