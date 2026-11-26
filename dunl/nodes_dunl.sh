#!/usr/bin/env bash
# Read-only trusted-key snapshot of this operator's own nodes for the dunl audit.
# Runs the admin `validators` command inside each node container (the admin port only answers on
# the container's own loopback) and keeps only: fetch time, quorum, trusted master keys and, per
# publisher list, seq / publisher key / effective / expiration / available / its key list.
# Peer `uri` fields (third-party addresses) are dropped. Writes OUTDIR/<name>.json.
# Usage: sudo bash nodes_dunl.sh OUTDIR [name=container[@ssh-host] ...]
#   default: validator=postfiatd sentry=postfiatd-sentry@relay
set -euo pipefail
OUT=${1:?usage: nodes_dunl.sh OUTDIR [name=container[@ssh-host] ...]}
shift
[ $# -gt 0 ] || set -- validator=postfiatd sentry=postfiatd-sentry@relay
mkdir -p "$OUT"
Q='{"method":"validators"}'
for spec in "$@"; do
  name=${spec%%=*}
  target=${spec#*=}
  container=${target%%@*}
  cmd=(docker exec "$container" timeout 10 curl -s -H 'Content-Type: application/json' -d "$Q" http://127.0.0.1:5005)
  if [[ $target == *@* ]]; then
    host=${target#*@}
    raw=$(sudo -u "${SUDO_USER:-$USER}" ssh -o BatchMode=yes "$host" "$(printf '%q ' "${cmd[@]}")")
  else
    raw=$("${cmd[@]}")
  fi
  [ -n "$raw" ] || { echo "$name: empty validators response" >&2; exit 1; }
  jq -S --arg t "$(date -u +%FT%TZ)" --arg node "$name" '.result | select(.status == "success") | {
      node: $node, t: $t, validation_quorum,
      trusted_validator_keys: (.trusted_validator_keys | sort),
      publisher_lists: [.publisher_lists[]? | {seq, pubkey_publisher, effective, expiration, available,
                        list: (.list | sort)}]}' <<<"$raw" >"$OUT/$name.json.tmp"
  [ -s "$OUT/$name.json.tmp" ] || { rm -f "$OUT/$name.json.tmp"; echo "$name: validators RPC failed" >&2; exit 1; }
  mv "$OUT/$name.json.tmp" "$OUT/$name.json"
  jq -r '"\(.node) \(.t): \(.trusted_validator_keys | length) trusted keys, quorum \(.validation_quorum), lists \([.publisher_lists[] | "seq \(.seq) n \(.list | length)"] | join(","))"' "$OUT/$name.json"
done
