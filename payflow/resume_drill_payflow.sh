#!/usr/bin/env bash
# Resume drill for payflow/collect_payflow.py on a fixed, already-validated ledger range.
#
#   payflow/resume_drill_payflow.sh FIRST LAST [WORKDIR]
#
# 1. reference: one uninterrupted run over FIRST..LAST
# 2. drill: same range, SIGTERM after ~TERM_AFTER s, resume, SIGKILL after ~KILL_AFTER s, then
#    INJECT the two states a SIGKILL can leave behind, whatever byte it landed on: a torn (newline-less)
#    last line in deltas.csv and an orphan payments.csv/tx.csv row of the next, unfinished ledger
#    (copied from the reference). Resume to the end.
# 3. compare: tx.csv, payments.csv, deltas.csv byte-identical to the reference, ledgers.csv identical
#    without fetched_utc, every ledger FIRST..LAST exactly once, no gaps, tx.csv rows == txn_count sum,
#    restarts.csv kinds start,resume,resume, the last resume logged the torn-line cut and the orphan drop.
# Exit 0 only if every check passes. Read-only RPC (server_info, ledger) on your own node.
# shellcheck disable=SC2016  # check() bodies are eval'd on purpose, expanded at check time
set -euo pipefail

first="${1:?usage: resume_drill_payflow.sh FIRST LAST [WORKDIR]}"
last="${2:?usage: resume_drill_payflow.sh FIRST LAST [WORKDIR]}"
work="${3:-data/payflow/drill}"
term_after="${TERM_AFTER:-25}"
kill_after="${KILL_AFTER:-25}"
here="$(cd "$(dirname "$0")" && pwd)"
col=(python3 -u "$here/collect_payflow.py" --start "$first" --stop-at "$last")

[ -e "$work" ] && { echo "refusing: $work exists"; exit 2; }
mkdir -p "$work"
ref="$work/ref" drl="$work/drill"
rows() { echo $(($(wc -l <"$1") - 1)); }
lastled() { tail -1 "$1/ledgers.csv" | cut -d, -f1; }

echo "== reference run $first..$last"
"${col[@]}" --out "$ref" --log "$work/ref.log" >/dev/null
echo "reference: ledgers $(rows "$ref/ledgers.csv") tx $(rows "$ref/tx.csv") payments $(rows "$ref/payments.csv") deltas $(rows "$ref/deltas.csv")"

echo "== drill run: SIGTERM after ${term_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$term_after"
kill -TERM "$pid"
wait "$pid" || true
echo "after SIGTERM: ledgers $(rows "$drl/ledgers.csv"), last $(lastled "$drl")"

echo "== resume 1, SIGKILL after ${kill_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$kill_after"
kill -KILL "$pid"
wait "$pid" || true
done_led="$(lastled "$drl")"
echo "after SIGKILL: ledgers $(rows "$drl/ledgers.csv"), last $done_led"
for f in tx.csv payments.csv deltas.csv; do
  printf '%s: %s rows, last byte is newline: %s\n' "$f" "$(rows "$drl/$f")" \
    "$([ "$(tail -c1 "$drl/$f" | od -An -c | tr -d ' ')" = '\n' ] && echo yes || echo no)"
done

echo "== inject: orphan rows of the first reference ledger after $done_led with payments, torn deltas.csv tail"
orphan="$(awk -F, -v d="$done_led" 'NR>1 && $1+0>d {print $1; exit}' "$ref/payments.csv")"
[ -n "$orphan" ] || { echo "no reference payment after ledger $done_led to inject"; exit 2; }
inj_rows=0
for f in tx.csv payments.csv; do
  n="$(awk -F, -v o="$orphan" 'NR>1 && $1==o' "$ref/$f" | tee -a "$drl/$f" | wc -l)"
  inj_rows=$((inj_rows + n))
  echo "appended $n $f row(s) of ledger $orphan"
done
printf '%s,0,TORN' "$orphan" >>"$drl/deltas.csv"
echo "appended torn deltas.csv tail: '$orphan,0,TORN' (no newline)"

echo "== resume 2 to the end"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null
grep -E "RESUME|STOP|SUMMARY" "$work/drill.log"

fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
strip() { cut -d, -f1-8 "$1"; }  # all but fetched_utc
for f in tx.csv payments.csv deltas.csv; do
  check "$f identical ($(rows "$ref/$f") rows)" "cmp -s \"\$ref/$f\" \"\$drl/$f\""
done
check "ledgers.csv identical without fetched_utc" 'diff -q <(strip "$ref/ledgers.csv") <(strip "$drl/ledgers.csv") >/dev/null'
check "every ledger $first..$last exactly once" \
  'diff -q <(seq "$first" "$last") <(tail -n +2 "$drl/ledgers.csv" | cut -d, -f1) >/dev/null'
check "no gaps" '[ ! -s "$ref/gaps.csv" ] && [ ! -s "$drl/gaps.csv" ]'
check "txn_count sum == tx_rows sum == tx.csv rows" \
  '[ "$(tail -n +2 "$drl/ledgers.csv" | cut -d, -f5 | paste -sd+ | bc)" -eq "$(rows "$drl/tx.csv")" ] &&
   [ "$(tail -n +2 "$drl/ledgers.csv" | cut -d, -f6 | paste -sd+ | bc)" -eq "$(rows "$drl/tx.csv")" ]'
check "restarts.csv kinds start,resume,resume" \
  '[ "$(tail -n +2 "$drl/restarts.csv" | cut -d, -f1 | paste -sd,)" = "start,resume,resume" ]'
check "last resume cut the torn deltas.csv line" 'grep -q "RESUME dropped torn last line of deltas.csv" "$work/drill.log"'
check "last resume dropped the $inj_rows injected orphan row(s) (>=: a mid-ledger SIGKILL adds its own)" \
  '[ "$(grep -oE "RESUME dropped [0-9]+ (tx|payments).csv rows" "$work/drill.log" | awk "{s+=\$3} END {print s+0}")" -ge "$inj_rows" ]'
echo "restarts.csv:"; cat "$drl/restarts.csv"
for f in tx.csv payments.csv deltas.csv; do
  echo "sha256 $f ref   $(sha256sum <"$ref/$f" | cut -d' ' -f1)"
  echo "sha256 $f drill $(sha256sum <"$drl/$f" | cut -d' ' -f1)"
done
[ "$fail" -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit "$fail"
