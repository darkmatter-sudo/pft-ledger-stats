#!/bin/bash
# recorded run 11: anchors + analyzer on snapshot A (interim), suite + lint
cd "$(dirname "$0")/pft-ledger-stats" || exit 9
export TZ=UTC
x() { echo "\$ $*"; eval "$*"; local rc=$?; echo "(exit $rc)"; return "$rc"; }
echo "# dormancy run 11 - $(date -u +%FT%TZ)"
echo "# OS: $(. /etc/os-release; echo "$PRETTY_NAME") | python $(.venv/bin/python -V 2>&1 | cut -d' ' -f2) | HEAD $(git rev-parse HEAD)"
x "git status --short -- dormancy tests | grep -v '^?? dormancy/data/' ; true"
x "sha256sum dormancy/*.py tests/test_analyze_dormancy.py tests/test_dormancy.py tests/test_crosscheck_dormancy.py"
x "sha256sum dormancy/data/A/snapshot-7874004.jsonl.gz dormancy/data/headers.jsonl"
x ".venv/bin/python dormancy/anchors_dormancy.py --out dormancy/data/anchors.csv --snapshots dormancy/data/A/snapshot-7874004.jsonl.gz --headers dormancy/data/headers.jsonl"
x "sha256sum dormancy/data/anchors.csv; wc -l dormancy/data/anchors.csv"
x ".venv/bin/python dormancy/analyze_dormancy.py --snap dormancy/data/A/snapshot-7874004.jsonl.gz --anchors dormancy/data/anchors.csv --headers dormancy/data/headers.jsonl --out dormancy/report --charts"
x "cat dormancy/report/REPORT.md"
x ".venv/bin/ruff check . && .venv/bin/ruff format --check dormancy tests/test_analyze_dormancy.py tests/test_dormancy.py tests/test_crosscheck_dormancy.py"
x "timeout 900 nice -n 15 .venv/bin/python -m pytest -q -p no:cacheprovider -rN tests/test_dormancy.py tests/test_crosscheck_dormancy.py tests/test_analyze_dormancy.py"
x "timeout 900 nice -n 15 .venv/bin/python -m pytest -q -p no:cacheprovider -rN"
echo "# end $(date -u +%FT%TZ)"
