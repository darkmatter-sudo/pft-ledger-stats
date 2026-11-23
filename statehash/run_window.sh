#!/usr/bin/env bash
# Recorded A -> B collection window for statehash/ (read-only RPC only).
#   1. snapshot A at START (binary state tree + own and public header)
#   2. capture every validated ledger START..TO (binary header + tx blobs + metadata);
#      a transient give-up (rc 2) resumes up to 5 times, a chain break (rc 3) stops the run
#   3. snapshot B at TO right after the capture reaches it (before the node prunes it)
# Usage: run_window.sh OUTDIR START TO LOGDIR
set -uo pipefail
[ $# -eq 4 ] || { echo "usage: $0 OUTDIR START TO LOGDIR" >&2; exit 64; }
out=$1 start=$2 to=$3 logs=$4
here=$(cd "$(dirname "$0")" && pwd)
col=(python3 -I -u "$here/collect_statehash.py")
mkdir -p "$out" "$logs"
echo "$(date -u +%FT%TZ) WINDOW start=$start to=$to commit=$(git -C "$here" rev-parse HEAD)"

"${col[@]}" snapshot --out "$out/A" --ledger "$start" --log "$logs/snapshotA.log"
rc=$?
echo "$(date -u +%FT%TZ) STEP snapshotA rc=$rc"
[ "$rc" = 0 ] || exit 10

rc=2
for try in 1 2 3 4 5; do
  "${col[@]}" capture --out "$out/cap" --start "$start" --to "$to" --log "$logs/capture.log"
  rc=$?
  echo "$(date -u +%FT%TZ) STEP capture try=$try rc=$rc"
  [ "$rc" = 2 ] || break
  sleep 30
done
[ "$rc" = 0 ] || exit 11

"${col[@]}" snapshot --out "$out/B" --ledger "$to" --log "$logs/snapshotB.log"
rc=$?
echo "$(date -u +%FT%TZ) STEP snapshotB rc=$rc"
[ "$rc" = 0 ] || exit 12
echo "$(date -u +%FT%TZ) WINDOW DONE"
