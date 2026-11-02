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

Written after the 24-hour window closes, from the committed data and logs.
