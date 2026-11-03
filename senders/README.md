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
| `mutants_senders.py` | mutation check: one broken line at a time in a temp copy of the repo; the senders tests must fail for every mutant (34 mutants) |

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
- **Public RPC history.** The public RPC prunes its own history (about 2.5 days were left when this
  window closed). The crosscheck reads its `complete_ledgers` first and checks only the overlap,
  `--margin` ledgers clear of its oldest ledger; whatever falls outside is printed as NOT
  CHECKABLE. For a window longer than the RPC's history, run the crosscheck once during the
  window as well.

## Results

72.5 h window 2026-10-31T06:10:37Z -> 2026-11-03T06:40:37Z on the validator's own node: ledgers
7634323-7720573, **86251 ledgers, 0 gaps, 0 unlisted holes,
0 duplicates, 1 start and 0 restarts** (consecutive-index check in log 06).
Full tables and charts: **[REPORT.md](REPORT.md)** (generated by `report_senders.py`, log 07).

| headline | value |
|---|---|
| transactions (tx.csv rows) = sum of ledger `txn_count` | 29650 = 29650 |
| distinct senders / classifiable (>= 6 events) | 82 / 76 |
| active senders per slice (new, returning) | 47 (47, 0), 53 (20, 33), 50 (15, 35) |
| slice-0 cohort still active in slice 1 / slice 2 | 33 / 15 of 47 |
| classes periodic / bursty / sporadic | 0 / 0 / 82 |
| median CV of inter-arrival gaps (eligible senders) | 1.001 |
| median burstiness B | 0.001 |
| median of per-sender median gap (dominant period) | 181.0 s |
| failed transactions | 33 |
| transaction types | Payment 23246 (78.4 %), NFTokenMint 3229 (10.9 %), NFTokenBurn 3175 (10.7 %) |

**Finding: no sender is periodic or bursty.** Every one of the 76 classifiable senders has a
gap CV near 1 and B near 0, the signature of a Poisson (memoryless) process, and the pooled gaps
divided by each sender's mean follow the exponential density ([gaps.png](gaps.png)). This is not
a blind spot of the rule: the same rule finds 200/200 periodic, 200/200 Poisson and 200/200 burst
senders *planted on this collection's own ledger timeline* (log 08), and placing every real
sender's events on random ledgers of the window changes nothing (eligible=76 real periodic=0 periodic after shuffle=0 real classes={'sporadic': 76} shuffled classes={'sporadic': 76} median CV real=1.001 shuffled=0.997 PASS).
The testnet's steady senders behave like clients that wait a random (exponential) time between
transactions, not like cron jobs. The "top periodic senders" table is therefore empty; REPORT.md
lists the most active senders (addresses truncated) with their statistics instead.

### Public-RPC crosscheck (log 09b), negative (log 10b)

The public RPC had pruned the first part of the window by the time the window closed
(`server_info` complete_ledgers in the log), so the S and L checks cover the overlap only,
kept 2048 ledgers clear of the RPC's oldest ledger:
`RANGE collection 7634323-7720573 public_rpc 7650054-7721611 margin 2048 checked 7652102-7720573 (68472 ledgers, 23605 tx) NOT CHECKABLE 17779 ledgers, 6045 tx`.
That is 68472 of 86251 ledgers (79.4 %) and 23605 of 29650
transactions (79.6 %); ledgers 7634323-7652101 (17779 ledgers, 6045 tx) are
**not checkable** against the public RPC (and no longer on the local node, which keeps ~512-1024
ledgers). The first crosscheck attempt (log 09) ran over the whole window and is published as
found: the senders with early transactions MISMATCHed with `not_in_rpc` only (never
`not_in_local`) and the first ledger sample below the RPC's history failed with lgrNotFound; the
fix (commit message and `crosscheck_senders.py` docstring) limits the checks to the overlap, prints
the RANGE line, and exits 2 if the RPC prunes into the checked span during the run.

- 20/20 senders: sent-transaction sets equal (hash, ledger, type, result, fee), 0 mismatches
- 3/3 daily recounts: tx.csv rows == ledger `txn_count` sums for every slice (whole window)
- 50/50 ledgers: public-RPC `ledger_hash` and transaction count equal
- `SUMMARY ok=73 mismatch=0 negative=False seed=1 rpc_calls=112 retries=0 retries_by_error={}`
- negative control: `SUMMARY ok=0 mismatch=73 negative=True seed=1 rpc_calls=112 retries=0 retries_by_error={}` (every check must MISMATCH, exit 1)

### Tests, mutants, resume drill

- tests: 43 passed (log 11: senders analyze/collect/crosscheck/report suites, fake RPC server,
  `ruff check` and `shellcheck` clean)
- mutants: 34 mutants, 34 caught, 0 failed, unmutated control passes (log 12)
- resume drill (log 13, ledgers 7721467-7721666): SIGTERM, resume, SIGKILL, then injected
  orphan tx.csv rows + a torn ledgers.csv tail, resume: the resume dropped the torn line and 5
  orphan rows, tx.csv byte-identical to the reference, 8 PASS / 0 FAIL. Log 03 is the first
  recorded run of the injecting drill: the repair worked but its checker read `2\r` from the CRLF
  `restarts.csv` and printed FAIL; fixed in the checker and re-run as log 13. Log 01 is the
  original drill from the window start (no injection).
- code versions: logs 03 and 06-08 ran at 2bf8d0fb62c68e81f8a5a175c84286e793d9ec12, logs 09b-13 at
  330af7dd3f98630eaed3d954daec44aaa6604750; between the two only `crosscheck_senders.py`,
  `mutants_senders.py`, `tests/test_senders_crosscheck.py` and this README changed
  (`git diff --stat 2bf8d0f 330af7d -- senders/analyze_senders.py senders/report_senders.py
  senders/controls_senders.py senders/collect_senders.py senders/resume_drill_senders.sh` is empty).
  Logs 02, 04, 05 and 14 are development runs, published as they were.

### Data files (SHA-256 in [data/SHA256SUMS](data/SHA256SUMS) and [data/RESULTS.sha256](data/RESULTS.sha256), counts in [data/ROWS.txt](data/ROWS.txt))

| file | rows | sha256 |
|---|---|---|
| data/tx/ledgers.csv | 86251 | `a917cd786924453f98798338a2bebee6228d121e26f0f4d01414c3f033a55c00` |
| data/tx/restarts.csv | 1 | `eedeae661cfe64c222dea7d8646f1f3c593a5e93961720b3d52e180f261bdcc6` |
| data/tx/tx.csv | 29650 | `e3a83fb3be35913cf8ada28fe96318ded82d377fc5ae5f85535e484f3231ed6f` |
| REPORT.md | - | `40b3931f0ecb90327d68d13a0caccce8a698a7f4b9ae86cd97b8884b8ed4b985` |
| cohorts.png | - | `0f00f07cc039c34d37f8d9d401e2fb43d774fe447d8c567466af98f36a449972` |
| regularity.png | - | `9322217f20cd3abd81897dd2625768cd4509a03d1212f57ead2caaefbf59d7ee` |
| gaps.png | - | `fa841e0227e41955ad82c45505f85e567e6750e071959dfbc7930f12003dab2c` |
| data/senders.json | - | `e4233ae9780aa2cad561b8e1ba60939e3cc24737843586c24c26a806a027035c` |

### Logs

- [logs/01-resume-drill-window-start.log](logs/01-resume-drill-window-start.log)
- [logs/02-dev-analyze.log](logs/02-dev-analyze.log)
- [logs/03-resume-drill-inject-checker-cr-fail.log](logs/03-resume-drill-inject-checker-cr-fail.log)
- [logs/04-dev-controls.log](logs/04-dev-controls.log)
- [logs/05-dev-mutants.log](logs/05-dev-mutants.log)
- [logs/06-data-manifest.log](logs/06-data-manifest.log)
- [logs/07-analyze-report.log](logs/07-analyze-report.log)
- [logs/08-controls.log](logs/08-controls.log)
- [logs/09-crosscheck-full-window-rpc-pruned.log](logs/09-crosscheck-full-window-rpc-pruned.log)
- [logs/09b-crosscheck.log](logs/09b-crosscheck.log)
- [logs/10a-crosscheck-negative-stopped.log](logs/10a-crosscheck-negative-stopped.log)
- [logs/10b-crosscheck-negative.log](logs/10b-crosscheck-negative.log)
- [logs/11-tests-lint.log](logs/11-tests-lint.log)
- [logs/12-mutants.log](logs/12-mutants.log)
- [logs/13-resume-drill.log](logs/13-resume-drill.log)
- [logs/14-dev-mutants.log](logs/14-dev-mutants.log)
