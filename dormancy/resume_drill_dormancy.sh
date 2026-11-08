#!/usr/bin/env bash
# Torn-tail SIGTERM/SIGKILL resume drill for dormancy/headers_dormancy.py on one snapshot.
#
#   dormancy/resume_drill_dormancy.sh SNAPSHOT.jsonl.gz [WORKDIR]
#
# 1. reference: one uninterrupted fetch of every header the public RPC holds for SNAPSHOT
# 2. drill: same input, SIGTERM after ~TERM_AFTER s (must exit 4 after the current line), resume,
#    SIGKILL after ~KILL_AFTER s, then INJECT the state a SIGKILL mid-write leaves behind whatever byte
#    it landed on: the first CUT bytes of the next reference line, no newline. Resume to the end.
# 3. compare: headers.jsonl byte-identical to the reference, every seq exactly once, the last resume
#    reported torn_bytes == CUT and skipped every complete line, exit codes 4 / 137 / 0, and the public
#    RPC's complete_ledgers did not start after any fetched seq during the drill (else a difference could
#    be pruning, not the fetcher).
# Exit 0 only if every check passes. Public RPC only (--no-local), read-only methods server_info and
# ledger; --item-delay slows each run so the signals land mid-run.
# shellcheck disable=SC2016  # check() bodies are eval'd on purpose, expanded at check time
set -euo pipefail

snap="${1:?usage: resume_drill_dormancy.sh SNAPSHOT.jsonl.gz [WORKDIR]}"
work="${2:-data/dormancy/drill}"
term_after="${TERM_AFTER:-15}"
kill_after="${KILL_AFTER:-15}"
delay="${ITEM_DELAY:-0.3}"
cut="${CUT:-23}"
here="$(cd "$(dirname "$0")" && pwd)"

[ -e "$work" ] && { echo "refusing: $work exists"; exit 2; }
mkdir -p "$work"
ref="$work/ref.jsonl" drl="$work/drill.jsonl"
fetch=(python3 -u "$here/headers_dormancy.py" "$snap" --no-local --item-delay "$delay")
lines() { if [ -f "$1" ]; then wc -l <"$1"; else echo 0; fi; }
lastnl() { if [ ! -s "$1" ] || [ "$(tail -c1 "$1" | od -An -c | tr -d ' ')" = '\n' ]; then echo yes; else echo no; fi; }

echo "== reference run (input $(basename "$snap"))"
rc=0; "${fetch[@]}" --out "$ref" --log "$work/ref.log" >/dev/null || rc=$?
echo "reference: rc $rc, headers $(lines "$ref")"
[ "$rc" -eq 0 ] || { echo "RESULT: FAIL reference run rc $rc"; exit 1; }

echo "== drill run: SIGTERM after ${term_after}s"
"${fetch[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$term_after"
kill -TERM "$pid"
rc_term=0; wait "$pid" || rc_term=$?
echo "after SIGTERM: rc $rc_term, headers $(lines "$drl"), last byte is newline: $(lastnl "$drl")"

echo "== resume 1, SIGKILL after ${kill_after}s"
"${fetch[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$kill_after"
kill -KILL "$pid"
rc_kill=0; wait "$pid" || rc_kill=$?
done_n="$(lines "$drl")"
echo "after SIGKILL: rc $rc_kill, headers $done_n, last byte is newline: $(lastnl "$drl")"
[ "$(lastnl "$drl")" = yes ] || echo "NOTE the SIGKILL itself left a torn line; it is cut on resume 2 together with the injected bytes"

echo "== inject: first $cut bytes of reference line $((done_n + 1)), no newline"
next="$(sed -n "$((done_n + 1))p" "$ref")"
[ -n "$next" ] || { echo "RESULT: FAIL drill finished before the SIGKILL (no next line to tear); raise ITEM_DELAY"; exit 1; }
printf '%s' "${next:0:$cut}" >>"$drl"
torn_expect="$(python3 -c 'import sys; d=open(sys.argv[1],"rb").read(); print(len(d) - d.rfind(b"\n") - 1)' "$drl")"
echo "appended '${next:0:$cut}' -> expect torn_bytes $torn_expect"

echo "== resume 2 to the end"
rc_end=0; "${fetch[@]}" --out "$drl" --log "$work/drill.log" >/dev/null || rc_end=$?
grep -E "complete_ledgers|START|DONE" "$work/drill.log"

fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
seqs() { python3 -c 'import json,sys; [print(json.loads(l)["ledger_index"]) for l in open(sys.argv[1])]' "$1"; }
lastdone() { grep DONE "$work/drill.log" | tail -1 | sed 's/.* DONE //' | python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }
check "exit codes SIGTERM 4 / SIGKILL 137 / final 0 (got $rc_term/$rc_kill/$rc_end)" \
  '[ "$rc_term" -eq 4 ] && [ "$rc_kill" -eq 137 ] && [ "$rc_end" -eq 0 ]'
check "headers.jsonl identical to the reference ($(lines "$ref") lines)" 'cmp -s "$ref" "$drl"'
check "every seq exactly once" '[ -z "$(seqs "$drl" | sort | uniq -d)" ]'
check "last resume torn_bytes == $torn_expect" '[ "$(lastdone torn_bytes)" -eq "$torn_expect" ]'
check "last resume skipped the $done_n complete lines" '[ "$(lastdone already)" -eq "$done_n" ]'
check "last resume fetched only the rest ($(( $(lines "$ref") - done_n )))" \
  '[ "$(lastdone fetched)" -eq "$(( $(lines "$ref") - done_n ))" ]'
check "public complete_ledgers still holds the oldest fetched seq" \
  '[ "$(grep "source public complete_ledgers" "$work/drill.log" | tail -1 | grep -oE "\(([0-9]+)" | head -1 | tr -d "(")" -le "$(seqs "$ref" | sort -n | head -1)" ]'
echo "sha256 ref   $(sha256sum <"$ref" | cut -d' ' -f1)"
echo "sha256 drill $(sha256sum <"$drl" | cut -d' ' -f1)"
[ "$fail" -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit "$fail"
