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

Tests: `tests/test_collect_objlife.py` (macro parsing, rule, committed types.json equals a re-parse
of the source when `POSTFIATD_SRC` is set, count/index disagreement refused, deterministic bytes,
interrupted run + torn tail resumes to the same bytes, ledger-hash drift refused, typecount resume).
