# senders: sender activity cohorts and periodic-sender analysis

`senders/` measures how Post Fiat testnet accounts *send* transactions over a 72-hour window:
who is active each day, who is new and who comes back, how long cohorts stay active, and whether
each sender transacts on a schedule (periodic), in bursts (bursty) or irregularly (sporadic),
with the transaction, fee and failure share of each class. It sits between
[`txmix/`](../txmix) (window statistics, hashed accounts) and [`acctchurn/`](../acctchurn)
(state diffs between two snapshots).

It is read-only. Transactions come from `collect_senders.py` on your own node (admin JSON-RPC via
`docker exec`, only `server_info` and `ledger`). `crosscheck_senders.py` sends only `account_tx`
and `ledger` to the public RPC (an allowlist in the code). Nothing is submitted or signed.

## Files

| file | what it does |
|---|---|
| `collect_senders.py` | resumable collector: follows validated ledgers from `--start` for `--hours` (default 72), one `tx.csv` row per transaction (ledger, close time, index, hash, sender, type, result, fee, sequence, ticket flag), one `ledgers.csv` row per ledger (`txn_count`, `tx_rows`), `gaps.csv` for ledgers it could not fetch, `retries.csv` for every failed attempt, `restarts.csv` for every start/resume; a ledger that closed after the window end is never written (client-side window); a torn or orphan row is repaired on resume, so SIGTERM or SIGKILL cannot duplicate or lose a ledger |
| `resume_drill_senders.sh` | resume drill on a fixed ledger range: an uninterrupted reference run vs a run that gets SIGTERM and then SIGKILL and is resumed each time; after the SIGKILL it injects orphan `tx.csv` rows of the next ledger and a torn `ledgers.csv` tail, so the repair is proven live; `tx.csv` byte-identical, every ledger once, no gaps, `txn_count` = `tx_rows` = `tx.csv` rows, `restarts.csv` = start,resume,resume, torn cut + orphan drop logged |
| `analyze_senders.py` | daily active / new / returning senders, retention matrix, per-sender inter-arrival statistics, periodic/bursty/sporadic classes, class shares of senders, transactions, fees and failures, top periodic senders (truncated); exits 1 on an unlisted hole, a duplicate ledger or `tx.csv` rows != `txn_count` sum |
| `controls_senders.py` | seeded synthetic periodic, Poisson and burst senders (must classify periodic / sporadic / bursty, >= 95 %), the same generators *planted* on the real ledger timeline of the collection (every event moved to the next real close time, >= 95 %), and a shuffled-timestamp negative control on the real data (real periodic senders must not stay periodic, no shuffled sender may become periodic; class counts and median CV real vs shuffled are printed) |
| `report_senders.py` | runs the analyzer and the controls on one collection and writes `REPORT.md`, `cohorts.png`, `regularity.png` and `gaps.png`; every number in the report is computed from the data; exits 1 on an incomplete collection or a failed control |
| `crosscheck_senders.py` | public-RPC checks: 20 random senders' sent-transaction sets via `account_tx` (window enforced client-side), per-slice recount against ledger `txn_count`, sampled ledger hashes and transaction counts; `--negative` alters every expected value and must exit 1 |
| `mutants_senders.py` | mutation check: one broken line at a time in a temp copy of the repo; the senders tests must fail for every mutant (31 mutants) |

## Run it

```bash
senders/collect_senders.py --out data/senders/tx --log data/senders/tx/collect.log --hours 72.5
senders/resume_drill_senders.sh <first> <last> data/senders/drill-01      # already-validated range
senders/analyze_senders.py --data data/senders/tx --json senders.json
senders/controls_senders.py --data data/senders/tx
senders/report_senders.py --data data/senders/tx --out-dir senders --json senders.json   # REPORT.md + charts
senders/crosscheck_senders.py --data data/senders/tx --count 20 --ledgers 50 --seed 1
senders/crosscheck_senders.py --data data/senders/tx --count 20 --ledgers 50 --seed 1 --negative  # exit 1
.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/python senders/mutants_senders.py
```

## Method notes

- **One event per ledger.** A sender's activity times are the distinct close times of the ledgers
  it sent in; several transactions in one ledger are one event (close times are rounded to the
  network's close-time resolution, so order inside one ledger says nothing about scheduling).
- **Slices, not calendar days.** The window is cut into 24 h slices from the first ledger's close
  time; the slice count is the window length rounded to whole days and the last slice absorbs
  the remainder (72.5 h gives 3 slices, the last 24.5 h). "New" means first seen *in the window*:
  an account that last sent before the window starts counts as new in slice 0.
- **Classes.** For a sender with n >= 6 events and inter-arrival gaps g:
  CV = sd(g)/mean(g), burstiness B = (sd - mean)/(sd + mean) (B is -1 for a perfect clock, ~0
  for a Poisson process, -> 1 for bursts), period share = share of gaps within +-20 % of the
  median gap.
  *periodic*: CV <= 0.5 and period share >= 0.6. *bursty*: B >= 0.25 (CV >= 1.67).
  *sporadic*: everything else, including senders with fewer than 6 events.
  The thresholds sit between the clusters of the three synthetic controls (clock with +-5 %
  jitter, Poisson, bursts of 5-15 events) and are checked on every run by `controls_senders.py`;
  the shuffled-timestamp control shows a schedule found in real data disappears when the same
  number of events is placed on random ledgers of the window.
- **Public RPC window.** The public RPC returns `account_tx` entries outside
  `ledger_index_min/max`, so the crosscheck pages newest-first, drops every entry outside the
  collection range and stops after the first page that reaches below it; entries where the
  account only *received* (another sender's tx) are dropped too.

## Results

Collection in progress (window 2026-10-31T06:10:37Z -> 2026-11-03T06:40:37Z). REPORT.md, the data
CSVs and the logs are published when the window ends.
