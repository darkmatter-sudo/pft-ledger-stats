#!/usr/bin/env bash
# Measure the exact serialized state size at the window-end ledger (snapshot B's ledger) and join it
# with snapshot B. Waits up to 15 min for snapshot_end.sh to finish snapshot B, then runs
# statebytes_growth.py on the same ledger before online_delete removes it.
# Usage: growth/statebytes_end.sh DATA_DIR    (run from the repo root)
set -euo pipefail
data="${1:?usage: statebytes_end.sh DATA_DIR}"
out="$data/statebytes"
mkdir -p "$out"
log="$out/statebytes_end.log"
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$log"; }

snap=""
for _ in $(seq 1 90); do
	if grep -q 'snapshot.py exit 0' "$data/snapB/snapshot_end.log" 2>/dev/null; then
		snap="$(find "$data/snapB" -maxdepth 1 -name 'snapshot-*.jsonl.gz' -print -quit 2>/dev/null || true)"
		[ -n "$snap" ] && break
	fi
	sleep 10
done
if [ -z "$snap" ]; then
	say "FAIL snapshot B not finished in $data/snapB after 15 min"
	exit 2
fi
ledger="$(basename "$snap" .jsonl.gz)"
ledger="${ledger#snapshot-}"
if ! [[ "$ledger" =~ ^[0-9]+$ ]]; then
	say "FAIL cannot read the ledger from $snap"
	exit 2
fi
say "snapshot B $snap ready; statebytes of ledger $ledger"
if python3 -u growth/statebytes_growth.py --out "$out" --ledger "$ledger" --join "$snap" >>"$log" 2>&1; then
	rc=0
else
	rc=$?
fi
say "statebytes_growth.py exit $rc"
exit "$rc"
