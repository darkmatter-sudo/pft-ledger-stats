# objlife/ — deferred ledger object census and lifecycle (Escrow, Check, PayChannel, Offer, Ticket, …)

**Status: in progress.** Snapshot A is committed (ledger 7907362, 2026-11-09T19:41Z). The A->B
transaction watcher runs from ledger A+1; snapshot B (≥ 24 h after A, ~2026-11-10T19:41Z), the
A->B run of the analyzer, the B crosschecks and REPORT.md follow. Analyzer, sampled-tx crosscheck
and mutation harness are in place and tested on fixtures.

Read-only. Nothing is signed or submitted. The only RPC methods sent are `server_state`,
`server_info`, `ledger` and `ledger_data` (local node), plus `ledger_entry` and `tx` (public RPC
crosscheck).

## Which types count as deferred

`collect_objlife.py types` reads `include/xrpl/protocol/detail/ledger_entries.macro` from postfiatd
source at the running build's commit (`postfiatd --version` in the container:
`32723c15e686a2088f2c83017a6ff909d93822cd`, 1.0.9) and writes `data/types.json` with every entry
type, its macro line and fields. A type is deferred if it has a time-bound field (`sfExpiration`,
`sfCancelAfter`, `sfFinishAfter`, `sfSettleDelay`), or is Ticket (reserved future Sequence) or
DepositPreauth (standing authorization). At that commit: 8 of 31 types (`logs/01-types.log`):
NFTokenOffer, Check, Ticket, Offer, DepositPreauth, Escrow, PayChannel, Credential.

```bash
git init src && cd src && git remote add origin https://github.com/postfiatorg/postfiatd.git
git fetch --depth 1 origin 32723c15e686a2088f2c83017a6ff909d93822cd && git checkout FETCH_HEAD && cd ..
python3 -I objlife/collect_objlife.py types --src src --commit 32723c15e686a2088f2c83017a6ff909d93822cd \
  --out objlife/data/types.json
```

## Snapshot

`collect_objlife.py snapshot` pages the full state tree of one pinned validated ledger with
`../state/snapshot.py` (one fsynced line per page, torn last line cut on restart, resume on the same
ledger hash). It then counts every deferred type again with `ledger_data type=<rpc name>` on the
same ledger and stops on any count or index difference. It writes `objlife-<ledger>.jsonl` (header +
deferred objects sorted by index, deterministic bytes), fsyncs it, then writes `DONE` with its
sha256 and row count.

```bash
python3 -I objlife/collect_objlife.py snapshot --types objlife/data/types.json --out data/objlife/A \
  --log data/objlife/A/snapshot.log
```

Snapshot A (`logs/02-snapshotA.log`): 5117 objects; deferred rows 48 = NFTokenOffer 45 + Offer 3,
every other deferred type 0, and the full tree and the type filter agree for all 8 types.

## Crosscheck (public RPC)

`crosscheck_objlife.py objlife-<L>.jsonl` asks `https://rpc.testnet.postfiat.org` (read-only:
`server_info`, `ledger`, `ledger_data`, `ledger_entry`) for: the ledger header (hash, account_hash,
close_time); for every deferred type, the full index set via `ledger_data type=<rpc>` (missing and
extra objects are both MISMATCH, empty types are checked too); every snapshot object field for field
via `ledger_entry`; and `--absent K` seeded indexes not in the snapshot, which must be
`entryNotFound`. `server_info` is read once and a ledger outside `complete_ledgers` stops with exit 2.
Transient errors (timeouts, HTTP 429/5xx, `tooBusy`, `lgrNotFound` inside the range) are retried with
exponential backoff, each retry logged as a `RETRY` line; a call that never answers is `GAVE_UP`
(exit 2), never a MATCH. `--negative` changes every expectation (each type also gets one fake index
and each absent probe is expected present) so every check must MISMATCH. `--record`/`--offline`
replay the answers with no network; a call missing from the recording exits 3.

Snapshot A, live (`logs/04`): 64 checks (3 header, 8 types, 48 objects, 5 absent), 64 MATCH, 0
retries needed, rc 0. Negative (`logs/05`): 64 MISMATCH, rc 1. Offline (`logs/06`): same 64 verdicts
as live, negative offline 64 MISMATCH, and the recording minus its last line exits 3.

Tests: `tests/test_crosscheck_objlife.py` (all match, negative covers empty types and absent probes,
missing/extra objects, field diff, present absent-probe, out of range stops after `server_info`,
logged backoff, give-up is exit 2, non-transient errors not retried, record/offline + incomplete
recording, snapshot row count). `tests/test_collect_objlife.py` (macro parsing, rule, committed types.json equals a re-parse
of the source when `POSTFIATD_SRC` is set, count/index disagreement refused, deterministic bytes,
interrupted run + torn tail resumes to the same bytes, ledger-hash drift refused, typecount resume).

## A->B transaction watcher

The local node keeps only ~512-1024 ledgers (`online_delete=512`), so the transactions between A and
B cannot be fetched after the fact. `watch_objlife.py` (adapted from `nftflow/collect_nft.py`, same
resume rules) follows validated ledgers from A+1 with `ledger transactions=true expand=true` (≤ 1
request/s) and keeps every transaction whose metadata creates, modifies or deletes an object of a
deferred type (`types.json`), with full metadata. Per ledger it writes the tx lines first, then one
`ledgers.csv` row (`obj_tx_count`); on restart a torn last line is cut and tx lines after the last
`ledgers.csv` row are dropped and fetched again. Started 2026-11-09T20:09:23Z at ledger 7907363
(ledger A+1 was still retained), as a transient systemd unit, with a `pft-maint` data-collection
lock so no planned node restart lands in the window.

```bash
python3 -u objlife/watch_objlife.py --types objlife/data/types.json --out data/objlife/tx \
  --log data/objlife/tx/collect.log --start 7907363 --hours 25
```

Tests: `tests/test_watch_objlife.py` (filter keeps only deferred types and sorts by
TransactionIndex; a run, then a resume after a torn `ledgers.csv` row and an orphan tx line gives
each ledger exactly once).

## Analysis and reconciliation

`analyze_objlife.py --a A.jsonl --b B.jsonl --tx DIR --types types.json --json OUT --transitions CSV`
reads only files. Per snapshot: objects per type; native value per type in exact integer drops
(Escrow `Amount` and PayChannel `Amount - Balance` are locked; Check `SendMax`, Offer
`TakerGets`/`TakerPays` and NFTokenOffer `Amount` by sell/buy flag are promised, not locked); IOU
sums per currency/issuer as exact decimals; owner reserve; owner concentration (owners, top-1 and
top-5 share, exact Gini); deadline buckets; expired-but-not-removed objects; finishable escrows;
age in ledgers since `PreviousTxnLgrSeq`. A->B: created / deleted / modified / unchanged per type and
the tx types that caused them.

Reconciliation replays every deferred-type node of every window tx in (ledger, TransactionIndex)
order over A. R1 the replay gives exactly B's index set; R2 every object equals B field for field
(threading aside; fields a CreatedNode omits must be default 0/"0" in B); R3 per type B = A +
created - deleted; R4 every Modified/DeletedNode hits a live object whose PreviousFields /
FinalFields match the replay, and every CreatedNode a free index; R5 every B object touched after A
names a window tx that touched it at that ledger, and every older one equals its A object. First,
completeness: one `ledgers.csv` row per ledger A+1..B, no gaps, per-ledger tx counts match, no tx
twice; if any fails, nothing is reconciled (NOT_CHECKABLE, exit 2). Exit 1 lists every residual.

Sampled reconciliation: the analyzer picks `--sample K` window tx hashes with a fixed `--seed`;
`crosscheck_objlife.py --txs analysis.json --txdir DIR` fetches each from the public RPC with `tx`
and compares ledger_index, validated, TransactionIndex, TransactionResult and the full
AffectedNodes list with the watcher's copy. A tx outside the public RPC's `complete_ledgers` is
`NOT_CHECKABLE` (listed, never a MATCH; none checkable = exit 2). `--negative` expects
TransactionIndex + 1 and ledger_index + 1, so every tx line must MISMATCH.

### Owner reserve (postfiatd 32723c15)

Every deferred object adds exactly 1 to its owner's OwnerCount when created, so the reserve tied up
is objects x `reserve_inc` (from `server_state` at the snapshot): `Escrow.cpp:610`,
`CreateCheck.cpp:233`, `PayChan.cpp:324`, `CreateOffer.cpp:859`, `CreateTicket.cpp:138` (adds
`ticketCount`, one per Ticket object), `DepositPreauth.cpp:206,270`, `NFTokenUtils.cpp:1091`
(NFTokenOffer), `Credentials.cpp:168` (issuer; moved to the subject on accept,
`Credentials.cpp:369-370`), all under `src/xrpld/app/tx/detail/`.

### Deadline semantics (postfiatd 32723c15)

A snapshot's close time T is the next ledger's parent close time, which the transactors compare
against (files under `src/xrpld/app/tx/detail/` unless a path is given). Expired = mark <= T for `hasExpired` (`src/libxrpl/ledger/View.cpp:175-181`: Check
`CashCheck.cpp:117`, NFTokenOffer `NFTokenAcceptOffer.cpp:75`), Offer (`OfferStream.cpp:259-260`)
and PayChannel Expiration/CancelAfter (`PayChan.cpp:368-371`). Escrow CancelAfter uses
`after(now, mark)` = now > mark (`View.cpp:3956-3959`, `Escrow.cpp:1055`), and Credential
Expiration `now > exp` (`src/libxrpl/ledger/CredentialHelpers.cpp:35-38`): these lapse only at mark
< T. An Escrow is finishable once FinishAfter < T (`Escrow.cpp:1051`); that is not a deadline.
`tests/test_analyze_objlife.py::test_expiry_boundaries` pins each boundary.

Tests: `tests/test_analyze_objlife.py`: one hand-computed fixture (7 objects, 3 window txs) for
every census number (drops, IOU decimals, reserve, Gini 2/21, top shares, buckets), bucket edges,
an off-centre Gini (90/10 = 2/5), exact decimals (0.1 + 0.2 = 0.3, 2^53 + 1 drops), replay order
across and within ledgers, and isolated failures: each reconciliation check has an input where it
fails and the others pass (R2, R4, R5), R1 with R3 for a lost create, R3 with R4 for a create of a
live object (R3 can't fail alone: with R4 passing it is the per-type sum of R1); every completeness
problem gives NOT_CHECKABLE.

## Mutation check

`objlife/mutants_objlife.py` (harness from `dormancy/mutants_dormancy.py`) runs the unmutated
control from a temp copy first, then each mutant in its own temp copy with a per-run timeout that
kills the process group; CAUGHT needs a failed test (names printed), anything else is BROKEN.
59 mutants over the collector, watcher, analyzer and crosscheck.

```bash
TMPDIR=$HOME/.cache .venv/bin/python objlife/mutants_objlife.py --timeout 120
```
