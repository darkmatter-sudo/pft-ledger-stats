#!/usr/bin/env bash
# Resume drill for senders/collect_senders.py on a fixed, already-validated ledger range.
#
#   senders/resume_drill_senders.sh FIRST LAST [WORKDIR]
#
# 1. reference: one uninterrupted run over FIRST..LAST
# 2. drill: same range, SIGTERM after ~TERM_AFTER s, resume, SIGKILL after ~KILL_AFTER s, then
#    INJECT the two states a SIGKILL can leave behind, whatever byte it landed on: orphan tx.csv
#    rows of the next, unfinished ledger (copied from the reference) and a torn (newline-less)
#    last line in ledgers.csv. Resume to the end.
# 3. compare: tx.csv byte-identical, ledgers.csv identical without fetched_utc,
#    every ledger FIRST..LAST exactly once, gaps.csv absent/empty in both, tx.csv rows ==
#    txn_count sum, restarts.csv records start,resume,resume, and the last resume logged the
#    torn-line cut and the orphan-row drop (so the repair is proven live, not only in
#    tests/test_senders_collect.py)
# Exit 0 only if every check passes. Read-only RPC (server_info, ledger) on your own node.
# shellcheck disable=SC2016  # check() bodies are eval'd on purpose, expanded at check time
set -euo pipefail

first="${1:?usage: resume_drill_senders.sh FIRST LAST [WORKDIR]}"
last="${2:?usage: resume_drill_senders.sh FIRST LAST [WORKDIR]}"
work="${3:-data/senders/drill}"
term_after="${TERM_AFTER:-25}"
kill_after="${KILL_AFTER:-25}"
here="$(cd "$(dirname "$0")" && pwd)"
col=(python3 -u "$here/collect_senders.py" --start "$first" --stop-at "$last")

[ -e "$work" ] && { echo "refusing: $work exists"; exit 2; }
mkdir -p "$work"
ref="$work/ref" drl="$work/drill"

echo "== reference run $first..$last"
"${col[@]}" --out "$ref" --log "$work/ref.log" >/dev/null

echo "== drill run: SIGTERM after ${term_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$term_after"
kill -TERM "$pid"
wait "$pid" || true
echo "after SIGTERM: ledgers $(($(wc -l <"$drl/ledgers.csv") - 1)), last $(tail -1 "$drl/ledgers.csv" | cut -d, -f1)"

echo "== resume 1, SIGKILL after ${kill_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$kill_after"
kill -KILL "$pid"
wait "$pid" || true
done_led="$(tail -1 "$drl/ledgers.csv" | cut -d, -f1)"
echo "after SIGKILL: ledgers $(($(wc -l <"$drl/ledgers.csv") - 1)), last $done_led"
echo "last byte after the SIGKILL: tx.csv $(tail -c1 "$drl/tx.csv" | od -An -tx1 | tr -d ' '), ledgers.csv $(tail -c1 "$drl/ledgers.csv" | od -An -tx1 | tr -d ' ') (0a = ends on a full line)"
[ "$(tail -c1 "$drl/tx.csv" | od -An -tx1 | tr -d ' ')" = 0a ] || { echo "SIGKILL tore tx.csv mid-row: injection would merge rows; re-run the drill"; exit 2; }

echo "== inject: orphan tx.csv rows of the first reference ledger after $done_led with transactions, torn ledgers.csv tail"
orphan="$(awk -F, -v d="$done_led" 'NR>1 && $1+0>d {print $1; exit}' "$ref/tx.csv")"
[ -n "$orphan" ] || { echo "no reference transaction after ledger $done_led to inject"; exit 2; }
inj_rows="$(awk -F, -v o="$orphan" 'NR>1 && $1==o' "$ref/tx.csv" | tee -a "$drl/tx.csv" | wc -l)"
echo "appended $inj_rows tx.csv row(s) of ledger $orphan"
printf '%s,TORN' "$orphan" >>"$drl/ledgers.csv"
echo "appended torn ledgers.csv tail: '$orphan,TORN' (no newline)"

echo "== resume 2 to the end"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null
grep -E "RESUME|STOP|SUMMARY" "$work/drill.log"

fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
strip() { cut -d, -f1-6 "$1"; }  # all but fetched_utc
check "tx.csv identical ($(($(wc -l <"$ref/tx.csv") - 1)) rows)" 'cmp -s "$ref/tx.csv" "$drl/tx.csv"'
check "ledgers.csv identical without fetched_utc" 'diff -q <(strip "$ref/ledgers.csv") <(strip "$drl/ledgers.csv") >/dev/null'
check "every ledger $first..$last exactly once" \
  'diff -q <(seq "$first" "$last") <(tail -n +2 "$drl/ledgers.csv" | cut -d, -f1) >/dev/null'
check "no gaps" '[ ! -s "$ref/gaps.csv" ] && [ ! -s "$drl/gaps.csv" ]'
check "txn_count sum == tx_rows sum == tx.csv rows" \
  '[ "$(tail -n +2 "$drl/ledgers.csv" | cut -d, -f5 | paste -sd+ | bc)" -eq "$(($(wc -l <"$drl/tx.csv") - 1))" ] &&
   [ "$(tail -n +2 "$drl/ledgers.csv" | cut -d, -f6 | paste -sd+ | bc)" -eq "$(($(wc -l <"$drl/tx.csv") - 1))" ]'
check "restarts.csv kinds start,resume,resume" \
  '[ "$(tail -n +2 "$drl/restarts.csv" | cut -d, -f1 | paste -sd,)" = "start,resume,resume" ]'
check "last resume cut the torn ledgers.csv line" 'grep -q "RESUME dropped torn last line of ledgers.csv" "$work/drill.log"'
# csv.writer ends rows with \r\n: strip \r before comparing fields (run 03 failed on "2\r")
check "last resume dropped the $inj_rows injected orphan row(s) (>=: a mid-ledger SIGKILL adds its own)" \
  '[ "$(tail -1 "$drl/restarts.csv" | tr -d "\r" | cut -d, -f7)" -ge "$inj_rows" ] && [ "$inj_rows" -ge 1 ]'
echo "restarts.csv:"; cat "$drl/restarts.csv"
echo "sha256 ref   $(sha256sum <"$ref/tx.csv" | cut -d' ' -f1)"
echo "sha256 drill $(sha256sum <"$drl/tx.csv" | cut -d' ' -f1)"
[ "$fail" -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit "$fail"
