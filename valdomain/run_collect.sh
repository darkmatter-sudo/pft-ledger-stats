#!/usr/bin/env bash
# One recorded valdomain collection on the validator host: node admin read (inside the node's network
# namespace, read-only), VL + manifests + bounded TOML GETs, then the offline verification, the
# negative controls and the report outputs. Re-running resumes: cached files are not fetched again.
# Usage: bash valdomain/run_collect.sh DATA_DIR PRIVATE_FILE   (from the repo root)
set -uo pipefail
DATA="${1:?data dir}"
PRIV="${2:?private reasons file (keep out of the repo)}"
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/.." || exit 1
x() { echo "\$ $*"; ( eval "$*" ) 2>&1; rc=$?; echo "exit=$rc"; return $rc; }
echo "valdomain collection $(date -u +%FT%TZ) repo HEAD $(git rev-parse HEAD)"
x "git status --short | wc -l"
x "sha256sum valdomain/*.py valdomain/*.sh"
mkdir -p "$DATA" "$(dirname "$PRIV")"
x "sudo RUN_UID=$(id -u) RUN_GID=$(id -g) bash valdomain/run-node.sh --out $DATA" || exit 1
x "python3 -I -u valdomain/collect_valdomain.py collect --out $DATA --private $PRIV" || exit 1
x "python3 -I -u valdomain/verify_valdomain.py --offline $DATA --out valdomain/report"
rc=$?
mkdir -p ../scratch/valdomain-ctl
x "python3 -I -u valdomain/verify_valdomain.py controls --offline $DATA --scratch ../scratch/valdomain-ctl"
c=$?
x "cat valdomain/report/table.md"
x "wc -l < $PRIV"
echo "RESULT collect verify=$rc controls=$c"
[ "$rc" = 0 ] && [ "$c" = 0 ]
