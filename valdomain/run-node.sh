#!/usr/bin/env bash
# Run `collect_valdomain.py node` inside the postfiatd container's network namespace (the admin
# JSON-RPC listens on the container's own loopback; from the host the node sees the docker gateway and
# answers public methods only), as an unprivileged user: nsenter enters only the net namespace, then
# drops to RUN_UID/RUN_GID. Nothing is executed inside the container itself. Read-only commands only.
# Usage: sudo RUN_UID=1000 RUN_GID=1000 bash run-node.sh --out DIR
set -euo pipefail
CONTAINER="${CONTAINER:-postfiatd}"
: "${RUN_UID:?set RUN_UID}" "${RUN_GID:?set RUN_GID}"
here="$(cd "$(dirname "$0")" && pwd)"
pid="$(docker inspect -f '{{.State.Pid}}' "$CONTAINER")"
[ "$pid" -gt 0 ] || { echo "container $CONTAINER not running" >&2; exit 1; }
exec nsenter -t "$pid" -n --setuid "$RUN_UID" --setgid "$RUN_GID" -- \
  /usr/bin/python3 -I -u "$here/collect_valdomain.py" node "$@"
