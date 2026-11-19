# statereplay: metadata replay A -> B and write-hotspot analysis

Read-only, stdlib-only proof that transaction metadata explains **every** state change of the Post Fiat
testnet over a ~24 h window of validated ledgers, plus an analysis of where those writes go.

Gap it closes: `acctchurn/` and `acctflags/` diff two snapshots, and `hashverify/` checks the hash chain,
but nothing showed that the metadata of the transactions in between turns snapshot A into snapshot B
exactly. `statereplay/` replays every `CreatedNode` / `ModifiedNode` / `DeletedNode` of every
transaction, in `TransactionIndex` order, onto the full state tree of A and requires the result to equal
the full state tree of B object-for-object and field-for-field.

Results: [REPORT.md](REPORT.md) (generated from the committed `data/` only).

## Files

| file | what it does |
|---|---|
| `replay.py` | the replay; checks window integrity, applies the node rules, derives the metadata-less writes, compares with B |
| `extract_replay.py` | checks both snapshot manifests and the capture, writes the pseudonymized, publishable `data/` (`--subset N` writes a test fixture) |
| `crosscheck_replay.py` | checks sampled window ledgers and 64 random snapshot-B objects against the public RPC |
| `hotspots.py` | replays `data/` and writes the hotspot analysis, `REPORT.md` and `report.json` |
| `mutants_replay.py` | mutation harness over all four modules |
| `data/` | published inputs: `snapshots.json`, `A.jsonl.gz`, `B.jsonl.gz`, `ledgers.jsonl.gz`, `tx.jsonl.gz`, crosscheck summaries |
| `logs/` | the recorded runs (collection, replay, crosscheck, mutation, fresh clone) |

Collection uses the existing read-only client `acctflags/collect_flags.py` unchanged: `snapshot` pins one
validated ledger and pages the full state tree with `ledger_data` (resumable, saved pages), `capture`
follows every validated ledger with `ledger transactions=true expand=true` and stops on any chain break.
Only `server_state`, `ledger`, `ledger_data` are sent to your own node, at most 2 requests/s in total.

## Replay rules

For every ledger A+1 .. B:

1. The ledger row and its transactions must be present exactly once; `parent_hash` must equal the
   previous ledger's hash, ledger A's hash must equal snapshot A's pinned hash and ledger B's hash
   snapshot B's; the transaction count must equal `txn_count`. Failure reasons: `LEDGER_MISSING`,
   `LEDGER_TX_MISSING`, `LEDGER_DUPLICATE`, `LEDGER_TX_DUPLICATE`, `CHAIN_BREAK`, `ANCHOR_A_HASH`,
   `ANCHOR_B_HASH`, `TX_COUNT`.
2. On a flag ledger (index % 256 == 0) a pending NegativeUNL `ValidatorToDisable` / `ValidatorToReEnable`
   takes effect first (`Ledger::updateNegativeUNL`); this writes no metadata.
3. Transactions are applied in `TransactionIndex` order, which must be exactly 0..n-1 (`TX_INDEX_GAP`).
   The `ledger` RPC returns them in hash order, so the capture order is never used. Each node is checked
   against the replayed state *before* it is applied:
   - `CreatedNode`: the object must not exist (`CREATE_EXISTS`); new object = `NewFields` plus the
     required zero-default fields that metadata omits (`Flags`, `OwnerCount`, `OwnerNode`, ...) plus the
     thread (`PreviousTxnID` = this transaction, `PreviousTxnLgrSeq` = this ledger).
   - `ModifiedNode`: the object must exist (`NODE_MISSING`) with the same type (`TYPE_MISMATCH`); every
     `PreviousFields` value must equal the replayed value (`PREVIOUS_MISMATCH`); every `FinalFields` value
     not in `PreviousFields` must be unchanged (`UNCHANGED_MISMATCH`), except a field the transaction
     *adds* (no previous value, so it is not in `PreviousFields`); the node's `PreviousTxnID` must equal
     the replayed thread (`THREAD_MISMATCH`). New object = `FinalFields`, fields only in
     `PreviousFields` removed, thread advanced.
   - `DeletedNode`: same checks on `FinalFields`, then the object is removed.
4. The `LedgerHashes` skip lists are advanced as `Ledger::updateSkipList` does after the transactions:
   the short list gets the parent hash (last 256 kept), and when the parent index is a multiple of 256 the
   long list for `parent >> 16` gets it too. These change every ledger without metadata.

Steps 2 and 4 are **derived** from the hash chain, never exempted: the skip lists and the NegativeUNL are
compared with B like every other object (tests `test_skip_list_is_derived_not_exempted`,
`test_negative_unl_reenable_takes_effect_on_next_flag_ledger`).

Threading: the `fixPreviousTxnID` amendment is enabled on the testnet (checked with `feature` on
2026-11-19; every DirectoryNode in snapshot A carries `PreviousTxnID`), so every type except
`LedgerHashes` is threaded, including DirectoryNode, NegativeUNL, Amendments and FeeSettings.

After the last ledger, any object missing, extra or different is a diff (`MISSING_IN_REPLAY`,
`EXTRA_IN_REPLAY`, `FIELD_MISMATCH` with the field names). Exit 0 only for zero diffs.

## Hotspot analysis (`hotspots.py`)

From the same replay pass: AffectedNodes per transaction type (min / p50 / p95 / max / mean,
nearest-rank, and the created/modified/deleted split), the most-rewritten objects and accounts, top senders
by nodes written, DirectoryNode churn (pages created/modified/deleted, owner vs order-book pages,
`Indexes` entries added/removed), objects modified 2+ times that end the window net-unchanged (ignoring
the thread), objects created and deleted inside the window, and write share by `LedgerEntryType`
including the derived skip-list/NegativeUNL writes.

## Public-RPC crosscheck (`crosscheck_replay.py`)

Against `https://rpc.testnet.postfiat.org/`, not the collecting node: ledger A, ledger B and 24 ledgers
drawn from the window (`random.Random(A's hash)`) must have the captured hash, parent hash, transaction
count and transaction-hash set; 64 objects drawn from snapshot B (`random.Random(B's hash)`) must equal
`ledger_entry index=X ledger_hash=B` field-for-field. Read-only allowlist `server_info` / `ledger` /
`ledger_entry`, at most 2 requests/s, retries with logged exponential backoff (`RETRY` lines). The
`--negative` run alters every expected value and must mismatch every check. `--record` / `--offline`
store and replay the answers (recordings hold real addresses and stay private).

## Run it

```sh
# 1. collect (your own node, read-only)
python3 -I acctflags/collect_flags.py capture --out data/statereplay/tx --start <A-15> --rate 0.5 &
python3 -I acctflags/collect_flags.py snapshot --out data/statereplay/A
# ... ~24 h later
python3 -I acctflags/collect_flags.py snapshot --out data/statereplay/B   # then stop the capture
# 2. replay the raw capture, crosscheck, extract the publishable data
python3 -I statereplay/replay.py --a data/statereplay/A --b data/statereplay/B --tx data/statereplay/tx
python3 -I statereplay/crosscheck_replay.py data/statereplay/A data/statereplay/B data/statereplay/tx \
    --record data/statereplay/xc.rec.json --summary statereplay/data/crosscheck-B.json
python3 -I statereplay/crosscheck_replay.py data/statereplay/A data/statereplay/B data/statereplay/tx \
    --negative --offline data/statereplay/xc.rec.json --summary statereplay/data/crosscheck-B-negative.json
python3 -I statereplay/extract_replay.py data/statereplay/A data/statereplay/B data/statereplay/tx --out statereplay/data
# 3. report
python3 -I statereplay/hotspots.py --data statereplay/data --out statereplay
```

Offline rebuild from a clone (no node, no network):

```sh
python3 -I statereplay/replay.py --data statereplay/data
mkdir -p /tmp/sr && python3 -I statereplay/hotspots.py --data statereplay/data --out /tmp/sr
cmp /tmp/sr/REPORT.md statereplay/REPORT.md && cmp /tmp/sr/report.json statereplay/report.json
```

Tests and mutation check (from the repo root):

```sh
python3 -m pytest -q tests/test_statereplay_*.py
python3 statereplay/mutants_replay.py
```

The tests run on a real capture (`tests/fixtures/statereplay/F1F3`: ledgers 8169668 -> 8169792, every
object those 39 transactions touch, both skip lists and 40 untouched objects, from two real snapshots)
and real NegativeUNL objects (`nunl.json`); branches that capture does not reach (DirectoryNode churn,
net-unchanged objects) use a small synthetic window with hand-computed answers.

## Privacy and limits

- Published files and the report carry no address in clear: every valid classic address becomes
  `acct_` + 12 hex of HMAC-SHA256 under a private key, free-text fields (`URI`, `Domain`, `Memo*`, ...)
  become `text_` + sha256 prefix. The substitution is deterministic, so the published replay is the same
  replay. Object indexes and ledger/transaction hashes stay real for the crosscheck, so the pseudonyms keep
  addresses out of the files, not out of reach of someone who looks indexes up on the public ledger.
- Published transactions are reduced to `hash`, `TransactionType`, `Account` and the metadata
  (`TransactionIndex`, `TransactionResult`, `AffectedNodes`); signatures and other fields are dropped.
- Transactions that never reach a validated ledger (`tem`/`tef`/`ter`) leave no metadata and are out of scope.
- The `CREATE_DEFAULTS` table covers the types seen on the testnet; a new type with an omitted required
  default would show up as a `FIELD_MISMATCH` on that object, never as a silent pass.
