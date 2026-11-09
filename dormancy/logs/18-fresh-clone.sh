#!/bin/bash
# recorded run 18: verification from a fresh anonymous public clone at the pinned commit
# usage: SHA=<commit> bash task_7f884c52-18-fresh-clone.sh   (python deps from the existing repo .venv: PY)
: "${SHA:?commit}" "${PY:?python with matplotlib/pytest}"
export TZ=UTC GIT_TERMINAL_PROMPT=0
unset GH_TOKEN GITHUB_TOKEN
x() { echo "\$ $*"; eval "$*"; local rc=$?; echo "(exit $rc)"; return "$rc"; }
W=$(mktemp -d "${TEST_TMPDIR:-$HOME/.cache}/dormancy-verify.XXXXXX")
cd "$W" || exit 9
grep PRETTY_NAME /etc/os-release
echo "# dormancy run 18 - $(date -u +%FT%TZ) | python $("$PY" -V 2>&1 | cut -d' ' -f2) (repo .venv interpreter, outside the clone)"
x "git -c credential.helper= clone -q https://github.com/darkmatter-sudo/pft-ledger-stats.git clone"
cd clone || exit 9
x "git checkout -q $SHA && git rev-parse HEAD"
x "sha256sum dormancy/*.py dormancy/*.sh tests/test_dormancy.py tests/test_crosscheck_dormancy.py tests/test_analyze_dormancy.py"
x "(cd dormancy/data && sha256sum -c SHA256SUMS)"
x "cat dormancy/data/ROWS.txt"
x "\$PY dormancy/analyze_dormancy.py --snap dormancy/data/A/snapshot-7874004.jsonl.gz --snap dormancy/data/B/snapshot-7902546.jsonl.gz --anchors dormancy/data/anchors.csv --headers dormancy/data/headers.jsonl --out ../rerun --charts"
x "for f in REPORT.md dormancy.json accounts_A.png balance_A.png accounts_B.png balance_B.png topk_share.png transitions.png; do cmp ../rerun/\$f dormancy/report/\$f && echo \"identical \$f\"; done"
for s in A B; do
  snap=$(ls dormancy/data/$s/snapshot-*.jsonl.gz)
  x "\$PY dormancy/crosscheck_dormancy.py $snap --headers dormancy/data/headers.jsonl --count 200 --seed 1 --offline dormancy/data/$s/crosscheck-$s-random-seed1.jsonl > ../p.out; rc=\$?; tail -2 ../p.out; echo crosscheck_rc=\$rc"
  x "\$PY dormancy/crosscheck_dormancy.py $snap --headers dormancy/data/headers.jsonl --count 200 --seed 1 --negative --offline dormancy/data/$s/crosscheck-$s-random-seed1.jsonl > ../n.out; rc=\$?; tail -1 ../n.out; echo crosscheck_rc=\$rc"
done
x "\$PY dormancy/crosscheck_dormancy.py dormancy/data/A/snapshot-7874004.jsonl.gz --headers dormancy/data/headers.jsonl --count 89 --seed 1 --in-range-only --offline dormancy/data/A/crosscheck-A-inrange89-seed1.jsonl > ../a.out; rc=\$?; tail -1 ../a.out; echo crosscheck_rc=\$rc"
x "\$PY dormancy/crosscheck_dormancy.py dormancy/data/B/snapshot-7902546.jsonl.gz --headers dormancy/data/headers.jsonl --count 75 --seed 1 --in-range-only --offline dormancy/data/B/crosscheck-B-inrange75-seed1.jsonl > ../b.out; rc=\$?; tail -1 ../b.out; echo crosscheck_rc=\$rc"
x "TMPDIR=\$HOME/.cache timeout 600 \$PY -m pytest -q -p no:cacheprovider tests/test_dormancy.py tests/test_crosscheck_dormancy.py tests/test_analyze_dormancy.py 2>&1 | tail -1"
cd / && rm -rf "$W"
echo "# end $(date -u +%FT%TZ)"
