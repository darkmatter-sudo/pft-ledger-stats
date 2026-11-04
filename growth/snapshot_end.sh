#!/usr/bin/env bash
# Take the window-end state snapshot (snapshot B) for a growth collection run.
# Waits until the collector unit has exited, reads the last ledger of ledgers.csv and pages that
# ledger's full state with state/snapshot.py before online_delete removes it (~50 min on a
# 512-ledger node). Meant to run from a timer a few minutes after the window end.
# Usage: growth/snapshot_end.sh DATA_DIR [UNIT]    (run from the repo root)
set -euo pipefail
data="${1:?usage: snapshot_end.sh DATA_DIR [UNIT]}"
unit="${2:-pft-growth-tx}"
out="$data/snapB"
mkdir -p "$out"
log="$out/snapshot_end.log"
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$log"; }

for _ in $(seq 1 60); do
	systemctl is-active --quiet "$unit" || break
	sleep 10
done
if systemctl is-active --quiet "$unit"; then
	say "FAIL collector unit $unit still active after 10 min"
	exit 2
fi
last=""
[ -s "$data/tx/ledgers.csv" ] && last="$(tail -n 1 "$data/tx/ledgers.csv" | cut -d, -f1)"
if ! [[ "$last" =~ ^[0-9]+$ ]]; then
	say "FAIL no last ledger in $data/tx/ledgers.csv"
	exit 2
fi
say "collector $unit inactive; snapshot B of last window ledger $last"
if python3 -u state/snapshot.py --out "$out" --ledger "$last" --log "$out/snapshot.log" >>"$log" 2>&1; then
	rc=0
else
	rc=$?
fi
say "snapshot.py exit $rc"
exit "$rc"
