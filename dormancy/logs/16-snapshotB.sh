#!/bin/bash
# recorded run 16: snapshot B (~24 h after A: ledger 7874004, close 2026-11-08T15:38:40Z),
# B headers, random 200-account crosscheck of B (positive, negative, offline)
cd "$(dirname "$0")/../pft-ledger-stats" || exit 9
export TZ=UTC
x() { echo "\$ $*"; eval "$*"; local rc=$?; echo "(exit $rc)"; return "$rc"; }
target=$(date -u -d '2026-11-09T15:38:45Z' +%s)
while [ "$(date -u +%s)" -lt "$target" ]; do sleep 5; done
uname -a | sed "s/$(uname -n)/<host>/"
grep PRETTY_NAME /etc/os-release
echo "# dormancy run 16 - $(date -u +%FT%TZ) | python $(.venv/bin/python -V 2>&1 | cut -d' ' -f2) | HEAD $(git rev-parse HEAD)"
x "git status --short -- dormancy state tests"
x "mkdir -p data/dormancy/B"
x ".venv/bin/python -u state/snapshot.py --out data/dormancy/B --log data/dormancy/B/snapshot.log"
x "cat data/dormancy/B/snapshot.log"
x "cat data/dormancy/B/state.json"
B=$(ls data/dormancy/B/snapshot-*.jsonl.gz)
x "sha256sum data/dormancy/A/snapshot-7874004.jsonl.gz $B"
x "wc -l data/dormancy/headers.jsonl"
x ".venv/bin/python -u dormancy/headers_dormancy.py data/dormancy/A/snapshot-7874004.jsonl.gz $B --out data/dormancy/headers.jsonl --log data/dormancy/headers-B.log"
x "wc -l data/dormancy/headers.jsonl; sha256sum data/dormancy/headers.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count 200 --seed 1 --record data/dormancy/B/crosscheck-B-random-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count 200 --seed 1 --negative"
x "sha256sum data/dormancy/B/crosscheck-B-random-seed1.jsonl; wc -l data/dormancy/B/crosscheck-B-random-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count 200 --seed 1 --offline data/dormancy/B/crosscheck-B-random-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count 200 --seed 1 --negative --offline data/dormancy/B/crosscheck-B-random-seed1.jsonl"
x ".venv/bin/python -u dormancy/crosscheck_dormancy.py $B --headers data/dormancy/headers.jsonl --count 200 --seed 2 --offline data/dormancy/B/crosscheck-B-random-seed1.jsonl > /dev/null 2> data/dormancy/B/seed2.err"
x "tail -2 data/dormancy/B/seed2.err"
echo "# end $(date -u +%FT%TZ)"
