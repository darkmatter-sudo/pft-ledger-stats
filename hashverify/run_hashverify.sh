#!/usr/bin/env bash
# Read-only hashverify pipeline stages (run from the repo root; prints every command it runs).
#   headers  N   header hash-chain, own node + public RPC, whole complete_ledgers overlap
#   public   N   tx trees on the public RPC: 1 h slice + random sample, 2000 ledgers total
#   local    N   tx trees on the own node: every ledger of its complete_ledgers (minus a margin)
#   tamper   N   real-data negative controls (offline, from the records of headers/public)
#   report       REPORT.md + counts.csv from every run directory
# N = run number; outputs go to hashverify/data/<stage>-N/ (record, CSVs).
set -uo pipefail
PY=${PY:-.venv/bin/python}
LOCAL=${LOCAL:-http://127.0.0.1:5005}
PUBLIC=${PUBLIC:-https://rpc.testnet.postfiat.org}
D=hashverify/data

x() {
	echo "\$ $*"
	"$@"
	local rc=$?
	echo "(exit $rc)"
	return "$rc"
}

stage=${1:-}
n=${2:-}
echo "# hashverify $stage $n | $(date -u +%FT%TZ) | $(sed -n 's/^PRETTY_NAME="\(.*\)"$/\1/p' /etc/os-release) | git $(git rev-parse HEAD)"
case "$stage" in
headers)
	x "$PY" -u hashverify/verify_headers.py --local "$LOCAL" --public "$PUBLIC" \
		--out "$D/headers-$n" --record "$D/headers-$n/headers.jsonl.gz"
	;;
public)
	x "$PY" -u hashverify/verify_txtree.py --source "$PUBLIC" --count 2000 --slice-seconds 3600 \
		--out "$D/public-$n" --record "$D/public-$n/txtree.jsonl.gz"
	;;
local)
	span=$(curl -s -m 20 -d '{"method":"server_info","params":[{}]}' "$LOCAL" |
		"$PY" -c 'import json,sys; print(json.load(sys.stdin)["result"]["info"]["complete_ledgers"])')
	lo=${span%-*}
	hi=${span#*-}
	echo "own node complete_ledgers=$span -> checking $((lo + 5))-$((hi - 2))"
	x "$PY" -u hashverify/verify_txtree.py --source "$LOCAL" --gap 0 --range "$((lo + 5))-$((hi - 2))" \
		--out "$D/local-$n" --record "$D/local-$n/txtree.jsonl.gz"
	;;
tamper)
	h=${3:?headers run number} p=${4:?public run number}
	mid=$("$PY" -c 'import csv,sys; r=list(csv.DictReader(open(sys.argv[1]))); print(r[len(r)//2]["ledger_index"])' "$D/headers-$h/headers.csv")
	busy=$("$PY" -c 'import csv,sys; r=[x for x in csv.DictReader(open(sys.argv[1])) if int(x["txn_count"])>0]; print(max(r, key=lambda x: int(x["txn_count"]))["ledger_index"])' "$D/public-$p/txtree.csv")
	echo "controls: header ledger $mid (middle of headers-$h), tx ledger $busy (busiest of public-$p)"
	x "$PY" -u hashverify/verify_headers.py --offline "$D/headers-$h/headers.jsonl.gz" \
		--out "$D/tamper-$n/headers-coins" --tamper "public:$mid:total_coins"
	x "$PY" -u hashverify/verify_headers.py --offline "$D/headers-$h/headers.jsonl.gz" \
		--out "$D/tamper-$n/headers-parent" --tamper "local:$mid:parent_hash"
	x "$PY" -u hashverify/verify_txtree.py --offline "$D/public-$p/txtree.jsonl.gz" \
		--out "$D/tamper-$n/txtree-blob" --tamper "$busy"
	echo "untampered offline re-runs (must PASS):"
	x "$PY" -u hashverify/verify_headers.py --offline "$D/headers-$h/headers.jsonl.gz" --out "$D/tamper-$n/headers-clean"
	x "$PY" -u hashverify/verify_txtree.py --offline "$D/public-$p/txtree.jsonl.gz" --out "$D/tamper-$n/txtree-clean"
	;;
report)
	x "$PY" hashverify/report_hashverify.py --data "$D" --out hashverify
	;;
*)
	echo "usage: $0 headers|public|local|tamper|report N [headers-run public-run]" >&2
	exit 2
	;;
esac
