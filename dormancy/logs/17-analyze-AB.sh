#!/bin/bash
# recorded run 17: in-range crosscheck of B (every sampled account gets the tx check),
# anchors over A+B, A->B analysis + charts, lint, suite
cd "$(dirname "$0")/../pft-ledger-stats" || exit 9
export TZ=UTC
x() { echo "\$ $*"; eval "$*"; local rc=$?; echo "(exit $rc)"; return "$rc"; }
: "${N:?N = in-range sample size}"
B=$(ls data/dormancy/B/snapshot-*.jsonl.gz)
grep PRETTY_NAME /etc/os-release
echo "# dormancy run 17 - $(date -u +%FT%TZ) | python $(.venv/bin/python -V 2>&1 | cut -d' ' -f2) | HEAD $(git rev-parse HEAD)"
x "git status --short -- dormancy state tests"
x "sha256sum dormancy/*.py dormancy/*.sh tests/test_analyze_dormancy.py tests/test_dormancy.py tests/test_crosscheck_dormancy.py"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count $N --seed 1 --in-range-only --record data/dormancy/B/crosscheck-B-inrange$N-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count $N --seed 1 --in-range-only --negative"
x "sha256sum data/dormancy/B/crosscheck-B-inrange$N-seed1.jsonl; wc -l data/dormancy/B/crosscheck-B-inrange$N-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count $N --seed 1 --in-range-only --offline data/dormancy/B/crosscheck-B-inrange$N-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count $N --seed 1 --in-range-only --negative --offline data/dormancy/B/crosscheck-B-inrange$N-seed1.jsonl"
x "mkdir -p dormancy/data/B && cp $B dormancy/data/B/ && cp data/dormancy/headers.jsonl dormancy/data/headers.jsonl"
BD="dormancy/data/B/$(basename "$B")"
x "sha256sum dormancy/data/A/snapshot-7874004.jsonl.gz $BD dormancy/data/headers.jsonl"
x ".venv/bin/python dormancy/anchors_dormancy.py --out dormancy/data/anchors.csv --snapshots dormancy/data/A/snapshot-7874004.jsonl.gz $BD --headers dormancy/data/headers.jsonl"
x "sha256sum dormancy/data/anchors.csv; wc -l dormancy/data/anchors.csv"
x "rm -f dormancy/report/*.png dormancy/report/REPORT.md dormancy/report/dormancy.json"
x ".venv/bin/python dormancy/analyze_dormancy.py --snap dormancy/data/A/snapshot-7874004.jsonl.gz --snap $BD --anchors dormancy/data/anchors.csv --headers dormancy/data/headers.jsonl --out dormancy/report --charts"
x "ls -l dormancy/report; sha256sum dormancy/report/*"
x "cat dormancy/report/REPORT.md"
x ".venv/bin/ruff check . && .venv/bin/ruff format --check dormancy tests/test_analyze_dormancy.py tests/test_dormancy.py tests/test_crosscheck_dormancy.py"
x "shellcheck dormancy/resume_drill_dormancy.sh"
x "TMPDIR=\$HOME/.cache timeout 900 nice -n 15 .venv/bin/python -m pytest -q -p no:cacheprovider -rN tests/test_dormancy.py tests/test_crosscheck_dormancy.py tests/test_analyze_dormancy.py"
x "TMPDIR=\$HOME/.cache timeout 1500 nice -n 15 .venv/bin/python -m pytest -q -p no:cacheprovider -rN"
echo "# end $(date -u +%FT%TZ)"
