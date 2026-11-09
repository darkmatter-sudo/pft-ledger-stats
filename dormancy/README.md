# dormancy/ — account dormancy and last-activity analyzer

**Status: done.** Snapshots A (ledger 7874004, 2026-11-08T15:38:40Z) and B (ledger 7902546,
2026-11-09T15:38:50Z, 24 h 00 min 10 s later) are committed with the report: [`report/REPORT.md`](report/REPORT.md).
Results, checks and how to re-run everything offline are at the end of this file.

Read-only. For every account on the Post Fiat testnet ledger, it measures how long ago the account
last changed. It then reports how much PFT, how much reserve and how many owned objects sit in each
dormancy bucket, and how accounts move between buckets from snapshot A to snapshot B (~24 h later).
It builds on `state/snapshot.py`, which takes pinned, resumable snapshots of the full state tree.
Nothing is ever signed or submitted. The only RPC methods sent are `server_info`/`server_state`,
`ledger`, `ledger_data`, `account_info` and `tx`.

| file | what it does |
|---|---|
| `lib_dormancy.py` | time map (exact / interp / before), buckets, per-bucket stats, A→B transitions, exact reconciliation |
| `headers_dormancy.py` | resumable fetcher for the exact close time of every `PreviousTxnLgrSeq` a source still holds (public RPC first, own node second) |
| `crosscheck_dormancy.py` | independent check of the snapshot's last-activity keys against the public RPC (below) |
| `anchors_dormancy.py` | `data/anchors.csv`: exact headers already recorded in this repo's committed `ledgers.csv` files (0 conflicts required), cut to the ones the time map uses |
| `analyze_dormancy.py` | offline report from committed files only: buckets, PFT, reserves, OwnerCount, Gini / top-N, A→B transitions, charts, exact reconciliation (exit 1 on any failed check) |
| `../tests/test_analyze_dormancy.py` | exact PFT formatting, anchor selection keeps every lookup identical, tracked-file filter, conflicts, end-to-end A+B fixture, failing reconciliation |
| `../tests/test_crosscheck_dormancy.py` | crosscheck fields, tx checks, complete_ledgers limits, retries, negative control, record/offline replay |
| `../tests/test_dormancy.py` | bucket boundaries, interpolation bounds, Gini, fetcher resume / torn tail / backoff / ranges, transition invariants |

## Last activity and its time

An `AccountRoot`'s `PreviousTxnLgrSeq` is the ledger of the last transaction that modified it.
Its close time comes from one of three sources:

- **exact**: a validated ledger header, fetched by `headers_dormancy.py` or recorded by an earlier
  collector run in this repo (its `ledgers.csv`). Error 0.
- **interp**: the ledger lies between two known headers `lo < seq < hi`. Close times never go
  backwards, so the true time is in `[t_lo, t_hi]`. The report uses the linear point estimate and
  always prints the whole interval as the error bound.
- **before**: the ledger is older than the oldest known header. The time is `<= t_oldest`, with no
  lower bound. Such an account only gets a bucket that the upper bound alone decides. With the
  last bucket open-ended (`>=14d`), that holds whenever `T_snapshot - t_oldest >= 14 d`.

Nodes prune old headers: the public RPC held about 74k ledgers (~3 days) at snapshot A, and this
box's node holds ~1k. Most accounts' last activity is therefore older than any header a node still
serves. Each bucket is printed with `[min, max]` account counts: min = accounts whose whole
interval lies in that bucket, max = accounts whose interval touches it.

Buckets by age: `<1h`, `1h-1d`, `1d-7d`, `7d-14d`, `>=14d`, all as `[lo, hi)`.

## Anchors

The repo's earlier collectors (`*/data/**/ledgers.csv`, tracked by git) already hold ~240k exact
headers back to ledger 7352333 (2026-10-21). `anchors_dormancy.py` reads only files that
`git ls-files` lists, refuses any seq where two files (or a fetched header) disagree, and with
`--snapshots` keeps just the headers the time map can use: for each AccountRoot
`PreviousTxnLgrSeq` the exact header or its two bracketing neighbours, plus the oldest. A TimeMap
from the kept subset gives the same answer for every one of those seqs as the full set (tested).

## Crosscheck

`crosscheck_dormancy.py` picks `--count` random AccountRoots (seeded) and asks the public RPC
for each with `account_info ledger_index=<snapshot ledger>`. It compares Balance, OwnerCount,
Sequence, PreviousTxnID and PreviousTxnLgrSeq. If the source still holds the account's
PreviousTxnLgrSeq, it also asks `tx <PreviousTxnID>` and checks four things: the tx is validated,
it is in exactly that ledger, its metadata touches this AccountRoot, and its `date` equals the
exact header close time (when the time map has that header).

- **complete_ledgers limits**: `server_info` is read once. A snapshot ledger outside the range
  gives exit 2 with no further calls. A tx check runs only for ledgers inside the range; the rest
  are counted as `tx_out_of_range`. `--in-range-only` samples only accounts whose last tx is held,
  so each of them gets the tx check.
- **backoff**: timeouts, HTTP 429/5xx, `internal`/`tooBusy`, and `lgrNotFound`/`txnNotFound`
  inside the advertised range are retried with exponential backoff. An account with no answer
  after the retries is skipped and replaced. `actNotFound` counts as a mismatch, since the account
  exists in the snapshot at that ledger.
- **--negative**: every expected value is altered (+1, last PreviousTxnID hex digit changed, and
  the AccountRoot is expected to be absent from the tx metadata). A working check reports every
  compared field as a MISMATCH and exits 1.
- **--record / --offline**: `--record F` writes every final RPC answer as one JSON line;
  `--offline F` re-runs the same seed from that file with no network and gives the same verdicts.
  A call that is not in the recording stops the run with exit 3 (it is never read as a node answer).

Exit 0 all match, 1 any mismatch, 2 not enough accounts checked, 3 offline recording incomplete.

## Mutation check

```
.venv/bin/python dormancy/mutants_dormancy.py [--timeout 120] [--only L9]
```

Puts one broken line at a time (39 mutants: lib, header fetcher, anchors, analyzer, crosscheck)
into a temporary copy of the repo and runs `tests/test_*dormancy*.py`. The unmutated copy runs
first as the control, made the same way and in the same place, and must pass. Each copy gets its
own temporary directory. A mutant counts as CAUGHT only if pytest exits 1 with failed tests, and
the failing test names are printed so you can check the kill came from the right test. TIMEOUT
also counts as caught. SURVIVED, BROKEN (collection/import error) and NOT APPLIED (the mutant's
text is not found exactly once) count as failures. Exit 0 = control passed and every mutant caught.

## Resume drill (header fetcher)

```
dormancy/resume_drill_dormancy.sh dormancy/data/A/snapshot-<ledger>.jsonl.gz data/dormancy/drill-1
```

Makes one uninterrupted reference fetch. Then the same fetch is stopped with SIGTERM (must exit
4 after the current line), resumed, and SIGKILLed. Next the drill appends the first `CUT` bytes
of the next reference line with no newline: the torn line a SIGKILL mid-write leaves. A SIGKILL
that lands between writes leaves no torn line, so the drill injects one every time. Finally it
resumes to the end. To pass, headers.jsonl must be byte-identical to the reference with every
seq exactly once. The last resume must report `torn_bytes` equal to the injected length and skip
every complete line. Exit codes must be 4 / 137 / 0, and the public `complete_ledgers` must
still hold the oldest fetched seq. The drill uses public RPC only (`server_info`, `ledger`),
and `TERM_AFTER`, `KILL_AFTER`, `ITEM_DELAY` and `CUT` tune the timing.

## Snapshot source

Both snapshots were paged by `state/snapshot.py` (unchanged) from this box's own node: it pins the
validated ledger on start and only that node serves `ledger_data` for a whole recent ledger. The
public RPC (`rpc.testnet.postfiat.org`) is the second, independent source: `headers_dormancy.py`
asks it first for close times (own node second), and `crosscheck_dormancy.py` checks the snapshot
against it at the same ledger. `snapshot.py` itself has no public-RPC paging mode.

## Results (A = ledger 7874004, B = ledger 7902546)

Full tables and charts: [`report/REPORT.md`](report/REPORT.md); every figure: `report/dormancy.json`.

- Accounts: A 4573, B 4586. A→B: 47 touched (new `PreviousTxnID`), 4526 untouched, 13 created,
  0 deleted; no account in the `>=14d` bucket at A was active again by B.
- Time sources (B): exact 214, interp 152 (bound width up to 151.95 h), before 4220. 4220 of
  4586 accounts last changed before the oldest header any source or committed file holds
  (ledger 7352333, 2026-10-21T09:11:50Z), so they are `>=14d` by their upper bound alone.
- `7d-14d` is the only bucket whose count depends on interpolation: A [40, 140], B [57, 157].
  The `>=14d` bucket is [4257, 4357] in both snapshots.
- Concentration: Gini 0.999702 and top-10 share 0.994837 for all accounts at A and at B. They
  differ only past the 6th decimal (recomputed independently: A 0.999701531 / 0.994836834,
  B 0.999702366 / 0.994836541); one account holds 96,999,999,999.999880 PFT.
- Reconciliation, both snapshots, exact integers: objects 5106/5113 paged = in file, every
  account in exactly one bucket, bucket balances = total_coins (A 99999999967938708, B
  99999999967839710 drops; escrow 0, paychan 0), bucket reserves and OwnerCounts = account sums,
  min <= point <= max per bucket. Transition checks: 4573 - 0 + 13 = 4586, every A account in one
  cell, 47 + 4526 + 0 = 4573, 0 invariant problems.

## Checks

| check | A | B | log |
|---|---|---|---|
| random crosscheck, 200 accounts, seed 1 | 1000/1000 fields OK, 3 tx OK | 1000/1000 fields OK, 5/5 tx OK | `06` / `16` |
| same, `--negative` | 1000/1000 fields + 3/3 tx MISMATCH, exit 1 | 1000/1000 + 5/5 MISMATCH, exit 1 | `07` / `16` |
| same, `--offline` from the recording | same verdicts, exit 0; negative exit 1 | same verdicts, exit 0; negative exit 1 | `08` / `16` |
| in-range crosscheck (every held account, tx + date check) | 89/89: 445 fields, 89 tx, 89 dates OK | 75/75: 375 fields, 75 tx, 75 dates OK | `09` / `17` |
| in-range `--negative` (live and offline) | 445/445 + 89/89 MISMATCH | 375/375 + 75/75 MISMATCH | `09` / `17` |
| `--offline` with an unrecorded seed (2) | rc 1 (before the exit-3 fix, log 08/09 NOTE) | exit 3 | `09` / `16` |
| reconciliation + transitions | 8/8 OK | 8/8 OK, transitions 4/4 OK | `17` |
| header fetcher resume drill (SIGTERM/SIGKILL + injected 23-byte torn tail) | PASS 7/7, rc 4/137/0 | | `13` |
| mutation harness | control 76 passed, 39/39 CAUGHT | | `14` |
| tests / lint | dormancy 76 passed, full repo suite 665 passed, ruff check + format, shellcheck clean | | `17` |

The live SIGKILL in the drill landed between two writes (no torn line of its own); the torn
tail is the injected one, every run. Log 05 is superseded (code changed after it); logs 08/09
carry NOTE lines where a printed `(exit 0)` was `tail`'s status, not the tool's. In published
logs the home directory is shown as `~` and the user name as `<user>`.

## Reproduce offline

Logs ran against a scratch copy under `data/dormancy/` (gitignored); the committed copies are
the same bytes under `dormancy/data/` (`SHA256SUMS`, row counts in `ROWS.txt`).

```
cd dormancy/data && sha256sum -c SHA256SUMS && cd ../..
.venv/bin/python dormancy/analyze_dormancy.py --snap dormancy/data/A/snapshot-7874004.jsonl.gz \
  --snap dormancy/data/B/snapshot-7902546.jsonl.gz --anchors dormancy/data/anchors.csv \
  --headers dormancy/data/headers.jsonl --out /some/dir --charts   # byte-identical to report/
.venv/bin/python dormancy/crosscheck_dormancy.py dormancy/data/B/snapshot-7902546.jsonl.gz \
  --headers dormancy/data/headers.jsonl --count 200 --seed 1 --offline dormancy/data/B/crosscheck-B-random-seed1.jsonl
.venv/bin/python -m pytest -q tests/test_dormancy.py tests/test_crosscheck_dormancy.py tests/test_analyze_dormancy.py
```
