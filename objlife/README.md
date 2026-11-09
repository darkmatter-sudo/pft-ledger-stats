# objlife/ — deferred ledger object census and lifecycle (Escrow, Check, PayChannel, Offer, Ticket, …)

**Status: in progress.** Snapshot A is committed (ledger 7907362, 2026-11-09T19:41Z). Snapshot B
(≥ 24 h later), analysis, crosscheck, mutation harness and REPORT.md follow.

Read-only. Nothing is signed or submitted. The only RPC methods sent are `server_state`, `ledger`
and `ledger_data`.

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
