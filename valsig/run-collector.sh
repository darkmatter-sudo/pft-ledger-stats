#!/usr/bin/env bash
# Run `collect_valsig.py record` inside the postfiatd container's network namespace (the admin
# WebSocket listens on the container's own 127.0.0.1:6006, which docker-proxy cannot reach), as an
# unprivileged user: nsenter enters only the net namespace, then drops to RUN_UID/RUN_GID.
# Nothing is executed inside the container itself.
# Usage: sudo RUN_UID=1000 RUN_GID=1000 bash run-collector.sh --out DIR [--log FILE] [--hours 4.5]
set -euo pipefail
CONTAINER="${CONTAINER:-postfiatd}"
: "${RUN_UID:?set RUN_UID}" "${RUN_GID:?set RUN_GID}"
here="$(cd "$(dirname "$0")" && pwd)"
pid="$(docker inspect -f '{{.State.Pid}}' "$CONTAINER")"
[ "$pid" -gt 0 ] || { echo "container $CONTAINER not running" >&2; exit 1; }
exec nsenter -t "$pid" -n --setuid "$RUN_UID" --setgid "$RUN_GID" -- \
  /usr/bin/python3 -I -u "$here/collect_valsig.py" record "$@"
