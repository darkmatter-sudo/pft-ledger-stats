# acctflags: which account settings block inbound payments, and what happens to payments that hit them

`acctflags/` takes two full, validated ledger-state snapshots of the Post Fiat testnet about 24 hours
apart plus a gap-free capture of every transaction in between, and answers:

1. **Census.** For every AccountRoot: which flags are set (RequireDestTag, DepositAuth, DisallowXRP,
   RequireAuth, the four DisallowIncoming* flags, and the rest), their shares, which combinations
   occur, and which settings are present (RegularKey, Domain, EmailHash, MessageKey, NFTokenMinter,
   WalletLocator, TransferRate, TickSize).
2. **Reconciliation.** Replaying the AccountRoot and DepositPreauth nodes of every transaction's
   metadata onto snapshot A must reproduce snapshot B exactly, account by account and per flag:
   `expected_B = A + set_by_tx - cleared_by_tx + created_with - deleted_with`. Every disagreement is
   listed; the published report must say **Mismatches: 0**.
3. **Failures and retries.** Failed Payments grouped by result code and the destination's settings
   just before the payment, and whether the same sender later paid the same destination successfully
   within **100 validated ledgers** (the window is a CLI parameter, pinned to 100 in
   `report.config.json`), with destination-tag fixes, preauthorization fixes and flag clears counted
   separately.

It is read-only. The collector talks only to your own node's admin RPC through `docker exec` (methods
`server_state`, `ledger`, `ledger_data`); the crosscheck talks only to the public RPC
(`server_info`, `ledger`, `account_info`). Both enforce their allowlist in code; nothing is signed or
submitted. Published account ids are HMAC-SHA256 pseudonyms (first 12 hex chars) under a private key.

Results: [REPORT.md](REPORT.md).

## Files

| file | what it does |
|---|---|
| `collect_flags.py snapshot` | pins one validated ledger; pages the full state tree and `ledger_data type=account` on that hash and refuses any AccountRoot count/index-set difference; writes `accounts.jsonl`, `depositpreauth.jsonl`, `header.json`, `counts.json`, then `MANIFEST.json` (rows + sha256) last. Each page is saved with its marker, so an interrupted run resumes on the same pin |
| `collect_flags.py capture` | follows validated ledgers from `--start`, one `ledger transactions+expand` call per ledger; appends `tx.jsonl` then `ledgers.jsonl` (fsync); refuses a parent_hash break, an unexpanded transaction, or a validated ledger the node no longer has (exit 3) instead of recording a gap; on restart it cuts torn tails and orphan tx lines and resumes after the last complete ledger |
| `extract_flags.py` | checks both manifests and the window A+1..B (every ledger once, chained from A's hash to B's hash, tx counts equal) and writes the pseudonymized, publishable `data/` files |
| `analyze_flags.py` | census, reconciliation, failure/retry matching; writes `REPORT.md`, `report.json`, `charts/flags.png`, `charts/payments.png` (deterministic). `--offline` blocks all sockets for the run; exit 1 on any reconciliation mismatch |
| `crosscheck_flags.py` | draws (once, seeded by A's ledger hash) 24 accounts, every non-zero-Flags account first, and compares Flags, Sequence, Balance and settings with public `account_info` pinned to each snapshot's ledger hash; `--negative` flips bit 0 of each expected Flags (every account must MISMATCH); `--record` / `--offline` record and replay the answers; RETRY lines show every retry |
| `mutants_flags.py` | mutation check: one broken line at a time in a temporary copy; the acctflags tests must fail for every mutant |
| `report.config.json` | `{"retry_window_ledgers": 100}` |
| `data/` | the published, pseudonymized inputs of the report (see below) |
| `logs/` | redacted logs of the real runs |

Rate limit: each process sends at most `--rate` requests/s (snapshot 1.5, capture 0.5 by default, so
both together stay at or below 2 req/s; values above 2 are refused).

## Run it

```bash
# 1. start the capture first (it can start before A; only A+1..B is used), detached so it survives
python3 -I acctflags/collect_flags.py capture --out data/acctflags/tx --start <current validated> --log cap.log &
# 2. snapshot A now, crosscheck it while the public node still has the ledger
python3 -I acctflags/collect_flags.py snapshot --out data/acctflags/A
python3 -I acctflags/crosscheck_flags.py data/acctflags/A --sample-file data/acctflags/sample.json \
    --record data/acctflags/xc-A.rec.json --summary acctflags/data/crosscheck-A.json
python3 -I acctflags/crosscheck_flags.py data/acctflags/A --sample-file data/acctflags/sample.json \
    --offline data/acctflags/xc-A.rec.json --negative --summary acctflags/data/crosscheck-A-negative.json  # exit 1
# 3. ~24 h later: snapshot B, crosscheck B the same way, then extract + report
python3 -I acctflags/collect_flags.py snapshot --out data/acctflags/B
python3 -I acctflags/extract_flags.py data/acctflags/A data/acctflags/B data/acctflags/tx --out acctflags/data
.venv/bin/python -I acctflags/analyze_flags.py --data acctflags/data --config acctflags/report.config.json --out acctflags
# rebuild from a public clone (no node, no network):
.venv/bin/python -I acctflags/analyze_flags.py --offline --out /tmp/r && cmp /tmp/r/REPORT.md acctflags/REPORT.md
# tests, lint, mutants
.venv/bin/python -m pytest -q tests/test_acctflags_*.py && .venv/bin/ruff check . && \
    (cd acctflags && ../.venv/bin/ruff check . && ../.venv/bin/ruff format --check .) && \
    .venv/bin/python acctflags/mutants_flags.py
```

`data/acctflags/` (raw snapshots and capture with real addresses, the HMAC key, the sample and the
crosscheck recordings) is private and git-ignored.

## Method notes

- **Why the replay needs both checks.** The per-flag totals alone can agree by accident: dropping the
  transaction that cleared a flag makes the next transaction on that account "clear" it instead.
  `analyze_flags.py` therefore also compares each ModifiedNode's `PreviousFields.Flags` (always listed
  when Flags changed, because Flags is a required field) with the replayed flags, and compares every
  account's replayed state with snapshot B. Tests cover both cases (`test_positive_control_*`).
- **Settings before a modification** cannot be read from metadata: a newly added optional field
  (e.g. Domain) is not listed in PreviousFields. The replay takes them from the replayed state.
- **Retry matching.** A retry is the first later (ledger, TransactionIndex) tesSUCCESS Payment with the
  same sender and destination, at most N validated ledgers after the failure (inclusive). Fix classes:
  `dest_tag_fix` (failure `tecDST_TAG_NEEDED` without a DestinationTag, retry with one),
  `preauth_fix` (failure `tecNO_PERMISSION`, destination created a DepositPreauth for the sender in
  between), `flag_cleared` (destination cleared RequireDestTag / DepositAuth in between), else
  `other`. A failure without a retry whose window reaches past B is `censored`, not "not retried".
- **DepositPreauth** objects that authorize credentials instead of an account are not counted.

## Limits

- One 24-hour window on a test network with few non-default settings; shares are of all AccountRoots.
- Payment failures that never reach a ledger (tem/tef/tel results) are not visible in ledger data.
- The pseudonymous ids are stable within this report only (one key); they cannot be joined with other
  modules' ids.
