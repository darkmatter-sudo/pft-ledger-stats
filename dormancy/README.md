# dormancy/ — account dormancy and last-activity analyzer

**Status: in progress.** Snapshot A is taken; snapshot B and the report follow about 24 h later.
This README describes the method as built so far.

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
