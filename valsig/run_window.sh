#!/usr/bin/env bash
# One recording window: signed VL at the start, the stream recorder (re-entered after a container
# restart, up to MAX_RESTARTS times; the recording resumes), signed VL at the end.
# Usage (root, e.g. from a transient systemd unit):
#   RUN_UID=1000 RUN_GID=1000 bash valsig/run_window.sh OUT_DIR HOURS
set -uo pipefail
out=${1:?out dir}
hours=${2:?hours}
: "${RUN_UID:?set RUN_UID}" "${RUN_GID:?set RUN_GID}"
here="$(cd "$(dirname "$0")" && pwd)"
MAX_RESTARTS=${MAX_RESTARTS:-20}
as_user() { setpriv --reuid "$RUN_UID" --regid "$RUN_GID" --init-groups -- "$@"; }
as_user mkdir -p "$out"
if [ ! -e "$out/vl-start.json" ]; then
  as_user /usr/bin/python3 -I -u "$here/collect_valsig.py" vl --out "$out" --tag start || exit 3
fi
n=0
while :; do
  bash "$here/run-collector.sh" --out "$out" --log "$out/collect.log" --hours "$hours"
  rc=$?
  echo "$(date -u +%FT%TZ) recorder exit=$rc"
  [ "$rc" = 0 ] && break
  n=$((n + 1))
  [ "$n" -le "$MAX_RESTARTS" ] || { echo "giving up after $n restarts"; exit 4; }
  sleep 15
done
as_user /usr/bin/python3 -I -u "$here/collect_valsig.py" vl --out "$out" --tag end || exit 5
echo "$(date -u +%FT%TZ) window done"
