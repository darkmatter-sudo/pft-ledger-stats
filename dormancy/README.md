# dormancy/ — account dormancy and last-activity analyzer

**Status: in progress.** Snapshot A is taken; snapshot B and the report follow about 24 h later.
This README describes the method as built so far.

Read-only. For every account on the Post Fiat testnet ledger, it measures how long ago the account
last changed. It then reports how much PFT, how much reserve and how many owned objects sit in each
dormancy bucket, and how accounts move between buckets from snapshot A to snapshot B (~24 h later).
It builds on `state/snapshot.py`, which takes pinned, resumable snapshots of the full state tree.
Nothing is ever signed or submitted. The only RPC methods sent are `server_info`/`server_state`,
`ledger`, `ledger_data` and `account_info`.

| file | what it does |
|---|---|
| `lib_dormancy.py` | time map (exact / interp / before), buckets, per-bucket stats, A→B transitions, exact reconciliation |
| `headers_dormancy.py` | resumable fetcher for the exact close time of every `PreviousTxnLgrSeq` a source still holds (public RPC first, own node second) |
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
