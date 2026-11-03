# payflow: payment graph, concentration, net flows and onward flows

`payflow/` shows who pays whom on the Post Fiat testnet over a validated 24-hour window: the
directed payment graph, how concentrated PFT volume is, each account's net flow, and what the
recipients of the Task Node reward payer did with the PFT it sent them (onward flows). Every
balance change is reconciled against two full state snapshots, exact to the drop. It builds on
[`txmix/`](../txmix) (collector plumbing), [`state/`](../state) (snapshots),
[`acctchurn/`](../acctchurn) (snapshot loader, native conservation) and [`rewardflow/`](../rewardflow)
(public-RPC client).

It is read-only. Ledgers come from `collect_payflow.py` on your own node (admin JSON-RPC via
`docker exec`, only `server_info` and `ledger`). `crosscheck_payflow.py` sends only `tx` and `ledger`
to the public RPC (an allowlist in the code). Nothing is submitted or signed. Accounts are truncated
(first 6 + last 4 characters) in the report and JSON.

## Files

| file | what it does |
|---|---|
| `collect_payflow.py` | resumable collector over `ledger` (transactions, expanded): `tx.csv` (every tx with fee and result), `payments.csv` (every Payment, amount from metadata `delivered_amount`, never the tx `Amount`), `deltas.csv` (every AccountRoot balance change in the metadata, Created/Modified/Deleted), `ledgers.csv`, `gaps.csv`, `retries.csv`, `restarts.csv`; client-side window end; torn and orphan rows repaired on resume |
| `analyze_payflow.py` | directed graph (accounts, edges, degrees, reciprocal pairs, sources/sinks, components), Gini / top-1 / top-10 / HHI / accounts-for-half per side, net flows, FIFO reward-payer onward flows under two policies (upper and lower bound), Markdown + JSON + 2 charts; exit 1 on a hole, a duplicate ledger, tx rows != `txn_count`, a non-adjacent snapshot or any balance-model mismatch |
| `reconcile_payflow.py` | snapshot A (ledger before the window) + every delta == snapshot B (last ledger), exact to the drop: C1 coverage/adjacency, C2 burned == fees, C3 escrow/payment-channel holders, C4 every account, C5 per-account delta chains, C6 per-tx value == -fee, C7 per-payment sender/destination deltas; every mismatch line names its cause; `--negative` must fail C2, C4, C6 and C7 |
| `crosscheck_payflow.py` | 20 seeded payments (up to 2 failed ones, the rest uniform over the successful ones) against the public RPC `tx` (ledger, sender, destination, result, fee, delivered amount, per-account balance changes, validated) and each payment's `ledger` (hash, tx count); every retry printed; `--negative` must mismatch every check; `--json` records the RPC answers, `--offline` re-checks such a record against the committed data with no RPC |
| `resume_drill_payflow.sh` | live drill on a retained ledger range: reference run vs a run that gets SIGTERM, then SIGKILL, then an injected torn `deltas.csv` tail and orphan `tx.csv`/`payments.csv` rows of an unfinished ledger; the three data files must be byte-identical to the reference |
| `mutants_payflow.py` | mutation check over all four tools: one broken line at a time in a temp copy of the repo, every mutant must make the payflow tests fail (a hang past `--timeout` counts as caught) |

## Run it

```sh
# snapshot A of the ledger before the window, then 24 h of ledgers from the next one
python3 state/snapshot.py --out data/payflow/snapA
python3 -u payflow/collect_payflow.py --out data/payflow/tx --start <A+1> --hours 24 --log data/payflow/tx/collect.log
# after the window: snapshot B of the collector's last ledger (before online_delete rotates it out)
python3 state/snapshot.py --out data/payflow/snapB --ledger <last ledger in ledgers.csv>
python3 payflow/reconcile_payflow.py data/payflow/snapA/snapshot-<A>.jsonl.gz data/payflow/snapB/snapshot-<B>.jsonl.gz --data data/payflow/tx --json reconcile.json
python3 payflow/reconcile_payflow.py <same args> --negative          # must exit 1
python3 payflow/analyze_payflow.py --data data/payflow/tx --snapshot data/payflow/snapA/snapshot-<A>.jsonl.gz --json payflow.json --out-dir payflow
python3 -u payflow/crosscheck_payflow.py --data data/payflow/tx --json crosscheck.json
python3 -u payflow/crosscheck_payflow.py --data data/payflow/tx --negative --json crosscheck-negative.json   # must exit 1
python3 payflow/crosscheck_payflow.py --data data/payflow/tx --offline crosscheck.json   # no RPC
TERM_AFTER=45 KILL_AFTER=45 payflow/resume_drill_payflow.sh <first> <last> data/payflow/drill
TMPDIR=$HOME/.cache .venv/bin/python payflow/mutants_payflow.py --timeout 90
```

## Method notes

- **Delivered amounts.** A payment's volume is metadata `delivered_amount` of a `tesSUCCESS` native
  payment. The tx `Amount` is only the maximum for partial payments. Failed payments carry their fee
  and no volume, and non-native payments are counted separately with no PFT volume.
- **Conservation.** PFT only leaves circulation as burned fees, so `total_coins(A) - total_coins(B)`
  must equal the sum of fees of every transaction in the window (not only payments). With no escrow or
  payment-channel objects, each tx's AccountRoot deltas must sum to `-fee`. Escrow and payment-channel
  types are exempt from C6 and are instead balanced against the holders' change between the snapshots
  (C3).
- **Path payments.** A payment whose metadata changes a third account's balance is counted as a path
  payment. C7 does not check it, but C4-C6 still do.
- **Onward flows.** Each reward recipient's balance changes are replayed from its snapshot-A balance
  in two pools: reward lots (FIFO) and everything else. `reward_first` spends reward lots first (upper
  bound of onward flow), `balance_first` spends the other pool first (lower bound). Before each change,
  the model must equal the metadata's previous balance, and at the end its final balance. Any break
  makes the analyzer exit 1.
- **Retention.** The node keeps only about 50 minutes of ledgers (`online_delete`). So snapshot B is
  taken right after the window, the resume drill runs on a recent retained range, and
  `crosscheck_payflow.py --offline` re-checks the recorded RPC answers without any node.

## Results

Window: ledgers 7685510-7714380 (28871 ledgers, 24.25 h of close
times, collector window 2026-11-02T01:12:51Z -> 2026-11-03T01:27:51Z), snapshot A = ledger 7685509
(`6F1EA5360BFCB848CEDF95623A066BF0D39C2529F9D9AF0C02D978BFCD77707A`), snapshot B = ledger 7714380 (`FC1903B226981A5A543843640FCDB39A0BF9D22A269B2E0A4DD9EABDBE405148`). Full tables: [REPORT.md](REPORT.md),
machine-readable: [data/payflow.json](data/payflow.json).

| headline | value | source |
|---|---:|---|
| transactions / payments / counted / failed | 10001 / 7957 / 7945 / 12 | `data/payflow.json` `.totals`, `data/tx/*.csv` |
| PFT volume (delivered) | 34,322.686397 | `.totals.volume_drops` |
| fees burned (all tx) | 100124 drops | `.totals.fee_drops`, reconcile C2 |
| accounts / distinct edges / reciprocal pairs | 59 / 1475 / 653 | `.graph` |
| sent: Gini / top-1 / top-10 / accounts for 50 % | 0.44911 / 20.03% / 50.18% / 10 | `.concentration.sent` |
| received: Gini / top-1 / top-10 / accounts for 50 % | 0.44486 / 10.20% / 41.18% / 14 | `.concentration.received` |
| reward payer -> recipients | 6,875.750001 PFT to 13 accounts in 52 payments | `.onward` |
| onward flow, reward_first (upper) / balance_first (lower) | 0.000000 / 0.000000 PFT (fees 48 / 0 drops, model mismatches 0 / 0) | `.onward.*` |

In this window no reward recipient paid any of its reward PFT onward: apart from 48 drops of fees (upper
bound), all 6,875.750001 PFT was still held at ledger 7714380.

### Conservation (exact to the drop)

`SUMMARY ok=11 mismatch=0 causes={} negative=False accounts=4487 fees=100124 burned=100124`

Snapshot A total_coins 99999999968590182 - snapshot B total_coins 99999999968490058 = 100124 drops
burned = 100124 drops of fees over 10001 transactions. 4487 accounts reconciled, 0 unexplained
mismatches (0 mismatch lines). The forced-negative control fails as required:

`SUMMARY ok=7 mismatch=25 causes={'account_balance': 2, 'burn_ne_fees': 1, 'dest_delta': 5, 'payment_delta': 1, 'sender_delta': 5, 'tx_value_not_conserved': 11} negative=True accounts=4487 fees=110125 burned=100124`

### Public-RPC crosscheck, negative, offline

- `SUMMARY ok=40 mismatch=0 negative=False seed=1 rpc_calls=40 retries=0 retries_by_error={}` (20 payments, 2 of them failed `tecUNFUNDED_PAYMENT`)
- `--negative`: `SUMMARY ok=0 mismatch=40 negative=True seed=1 rpc_calls=40 retries=0 retries_by_error={}`
- offline (no RPC): `RESULT PASS offline fails=0 recorded_negative=False`; `RESULT PASS offline fails=0 recorded_negative=True`

### Tests, mutants, resume drill

- `58 passed in 1.44s` (payflow), full suite `423 passed in 17.33s`, `ruff check` + `ruff format --check` clean, shellcheck clean (log 14)
- `MUTANTS 34 caught=34 failed=0` (log 15, per-run timeout 90 s; M16 counts as caught by hang)
- live SIGTERM/SIGKILL resume drill with injected torn tail and orphan rows: `RESULT: PASS` (log 04)

### Data files (SHA-256 in [data/SHA256SUMS](data/SHA256SUMS) and [data/RESULTS.sha256](data/RESULTS.sha256), counts in [data/ROWS.txt](data/ROWS.txt))

| file | rows |
|---|---:|
| `data/tx/deltas.csv` | 17946 rows (excluding header) |
| `data/tx/ledgers.csv` | 28871 rows (excluding header) |
| `data/tx/payments.csv` | 7957 rows (excluding header) |
| `data/tx/restarts.csv` | 1 rows (excluding header) |
| `data/tx/tx.csv` | 10001 rows (excluding header) |
| `data/snapA/snapshot-7685509.jsonl.gz` | 4995 objects (excluding the header line) |
| `data/snapB/snapshot-7714380.jsonl.gz` | 5011 objects (excluding the header line) |

### Logs

| log | what |
|---|---|
| [01](logs/01-window-start.log) | window start, snapshot A, collector launch (commit 8412f59) |
| [02](logs/02-tests-lint-df06600.log) | tests + lint at df06600 |
| [03](logs/03-mutants-df06600-3-not-applied.log) | mutants at df06600: 31/34, 3 NOT APPLIED after `ruff format` (targets fixed in f9efa73) |
| [04](logs/04-resume-drill.log) | live resume drill (SIGTERM, SIGKILL, torn tail + orphan rows) |
| [05](logs/05-snapB-failed-no-dir.log) | snapshot B, FAILED: output dir missing (kept as evidence; retried in 16) |
| [06](logs/06-mutants-f9efa73.log) | mutants at f9efa73: 34/34 |
| [07](logs/07-collector-end-data-copy-manifest.log) | collector end, data copy, SHA256SUMS (last line: 0 non-OK lines, grep exit 1) |
| [08](logs/08-reconcile-and-negative.log) | conservation + `--negative` (the final jq/grep probe read keys that do not exist; log 17 has the correct jq) |
| [09](logs/09-analyze.log) | analyzer: report, JSON, charts |
| [10](logs/10-crosscheck.log) / [11](logs/11-crosscheck-negative.log) / [12](logs/12-offline.log) | crosscheck, negative, offline |
| [13](logs/13-tests-lint-superseded-system-python.log) | SUPERSEDED: system python without matplotlib (payflow 57 passed + 1 chart test skipped; full suite 4 failed, output cut to 3 lines, the 2 visible ones are ModuleNotFoundError in other folders) and 3 files not `ruff format`ted; the same suites pass under `.venv` in 14 |
| [14](logs/14-tests-lint.log) / [15](logs/15-mutants.log) | tests + lint, mutants at b725454 (final code; format-only commit after 13) |
| [16](logs/16-snapB.log) | snapshot B of ledger 7714380 (retry of 05, 1.5 min after the window end) |
| [17](logs/17-manifest.log) | ROWS.txt without header lines, RESULTS.sha256, all 22 checksums OK |

The collector and `state/snapshot.py` are unchanged since the window start (8412f59); the resume drill script is unchanged
since df06600. Published logs: home directory shown as `~` and the local user name as `<user>` (pytest temp paths in 02).

