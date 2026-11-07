#!/usr/bin/env bash
# Resume drill for txhygiene/collect_txhygiene.py on a fixed, already-validated ledger range.
#
#   txhygiene/resume_drill_txhygiene.sh FIRST LAST [WORKDIR]
#
# 1. reference: one uninterrupted run over FIRST..LAST
# 2. drill: same range and HMAC key, SIGTERM after ~TERM_AFTER s, resume, SIGKILL after ~KILL_AFTER s, then
#    INJECT the two states a SIGKILL can leave behind, whatever byte it landed on: orphan tx.jsonl lines of
#    the next, unfinished ledger (copied from the reference) followed by a torn (newline-less) tx.jsonl line.
#    Resume to the end.
# 3. compare: tx.jsonl byte-identical to the reference, ledgers.jsonl identical without fetched_utc, every
#    ledger FIRST..LAST exactly once, no duplicate (ledger, tx_index) or tx_hash, no gaps, tx lines ==
#    txn_count sum, restarts kinds start,resume,resume, the last resume logged the torn-line cut and the
#    orphan drop.
# Exit 0 only if every check passes. Read-only RPC (server_info, ledger) on your own node; --no-fees, because
# fee samples depend on wall-clock time and are not part of the replayed data.
# shellcheck disable=SC2016  # check() bodies are eval'd on purpose, expanded at check time
set -euo pipefail

first="${1:?usage: resume_drill_txhygiene.sh FIRST LAST [WORKDIR]}"
last="${2:?usage: resume_drill_txhygiene.sh FIRST LAST [WORKDIR]}"
work="${3:-data/txhygiene/drill}"
term_after="${TERM_AFTER:-25}"
kill_after="${KILL_AFTER:-25}"
here="$(cd "$(dirname "$0")" && pwd)"

[ -e "$work" ] && { echo "refusing: $work exists"; exit 2; }
mkdir -p "$work"
ref="$work/ref" drl="$work/drill"
col=(python3 -u "$here/collect_txhygiene.py" --start "$first" --stop-at "$last" --no-fees --key-file "$work/.hmac_key")
lines() { if [ -f "$1" ]; then wc -l <"$1"; else echo 0; fi; }
lastled() { tail -1 "$1/ledgers.jsonl" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["ledger_index"])'; }

echo "== reference run $first..$last"
"${col[@]}" --out "$ref" --log "$work/ref.log" >/dev/null
echo "reference: ledgers $(lines "$ref/ledgers.jsonl") tx $(lines "$ref/tx.jsonl")"

echo "== drill run: SIGTERM after ${term_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$term_after"
kill -TERM "$pid"
wait "$pid" || true
echo "after SIGTERM: ledgers $(lines "$drl/ledgers.jsonl"), last $(lastled "$drl")"

echo "== resume 1, SIGKILL after ${kill_after}s"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null &
pid=$!
sleep "$kill_after"
kill -KILL "$pid"
wait "$pid" || true
done_led="$(lastled "$drl")"
echo "after SIGKILL: ledgers $(lines "$drl/ledgers.jsonl"), last $done_led"
for f in tx.jsonl ledgers.jsonl; do
  printf '%s: %s lines, last byte is newline: %s\n' "$f" "$(lines "$drl/$f")" \
    "$([ ! -s "$drl/$f" ] || [ "$(tail -c1 "$drl/$f" | od -An -c | tr -d ' ')" = '\n' ] && echo yes || echo no)"
done

echo "== inject: orphan tx lines of the first reference ledger after $done_led that has txs, then a torn tx.jsonl tail"
orphan="$(python3 -c 'import json,sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r["ledger_index"] > int(sys.argv[2]):
        print(r["ledger_index"]); break' "$ref/tx.jsonl" "$done_led")"
[ -n "$orphan" ] || { echo "no reference tx after ledger $done_led to inject"; exit 2; }
inj_rows="$(python3 -c 'import json,sys
n = 0
with open(sys.argv[2], "a") as out:
    for line in open(sys.argv[1]):
        if json.loads(line)["ledger_index"] == int(sys.argv[3]):
            out.write(line); n += 1
print(n)' "$ref/tx.jsonl" "$drl/tx.jsonl" "$orphan")"
echo "appended $inj_rows tx.jsonl line(s) of ledger $orphan"
printf '{"ledger_index":%s,"close_time":TORN' "$orphan" >>"$drl/tx.jsonl"
echo "appended torn tx.jsonl tail: '{\"ledger_index\":$orphan,\"close_time\":TORN' (no newline)"

echo "== resume 2 to the end"
"${col[@]}" --out "$drl" --log "$work/drill.log" >/dev/null
grep -E "RESUME|STOP|SUMMARY" "$work/drill.log"

fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
strip() { python3 -c 'import json,sys
for line in open(sys.argv[1]):
    r = json.loads(line); r.pop("fetched_utc"); print(json.dumps(r, sort_keys=True))' "$1"; }
check "tx.jsonl identical ($(lines "$ref/tx.jsonl") lines)" 'cmp -s "$ref/tx.jsonl" "$drl/tx.jsonl"'
check "ledgers.jsonl identical without fetched_utc" 'diff -q <(strip "$ref/ledgers.jsonl") <(strip "$drl/ledgers.jsonl") >/dev/null'
check "every ledger $first..$last exactly once" \
  'diff -q <(seq "$first" "$last") <(python3 -c "import json,sys; [print(json.loads(l)[\"ledger_index\"]) for l in open(sys.argv[1])]" "$drl/ledgers.jsonl") >/dev/null'
check "no duplicate (ledger, tx_index) or tx_hash" \
  'python3 -c "import json,sys
r = [json.loads(l) for l in open(sys.argv[1])]
sys.exit(not (len({(x[\"ledger_index\"], x[\"tx_index\"]) for x in r}) == len({x[\"tx_hash\"] for x in r}) == len(r)))" "$drl/tx.jsonl"'
check "no gaps" '[ ! -s "$ref/gaps.jsonl" ] && [ ! -s "$drl/gaps.jsonl" ]'
check "txn_count sum == tx_rows sum == tx.jsonl lines" \
  '[ "$(python3 -c "import json,sys; r=[json.loads(l) for l in open(sys.argv[1])]; print(sum(x[\"txn_count\"] for x in r), sum(x[\"tx_rows\"] for x in r))" "$drl/ledgers.jsonl")" = "$(lines "$drl/tx.jsonl") $(lines "$drl/tx.jsonl")" ]'
check "restarts kinds start,resume,resume" \
  '[ "$(python3 -c "import json,sys; print(\",\".join(json.loads(l)[\"kind\"] for l in open(sys.argv[1])))" "$drl/restarts.jsonl")" = "start,resume,resume" ]'
check "last resume cut the torn tx.jsonl line" 'grep -q "RESUME dropped torn last line of tx.jsonl" "$work/drill.log"'
check "last resume dropped the $inj_rows injected orphan line(s) (>=: a mid-ledger SIGKILL adds its own)" \
  '[ "$(grep -oE "RESUME dropped [0-9]+ tx.jsonl rows" "$work/drill.log" | tail -1 | awk "{print \$3}")" -ge "$inj_rows" ]'
echo "restarts.jsonl:"; cat "$drl/restarts.jsonl"
echo "sha256 tx.jsonl ref   $(sha256sum <"$ref/tx.jsonl" | cut -d' ' -f1)"
echo "sha256 tx.jsonl drill $(sha256sum <"$drl/tx.jsonl" | cut -d' ' -f1)"
[ "$fail" -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit "$fail"
