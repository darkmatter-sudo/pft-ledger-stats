# txhygiene: how Post Fiat testnet transactions are constructed

`txhygiene/` looks at the *construction* of every validated transaction on the Post Fiat testnet
over a gap-free 24-hour window: whether the client set an expiry (`LastLedgerSequence`) and how much
margin it left, what it paid against the required base fee, which signing algorithm it used,
Sequence vs Ticket use, which optional fields it filled in, and which field sets (client
fingerprints) appear. These are the replay, expiry, sequencing, signing and fee-overpayment risks
that [`txmix/`](../txmix) (types, results, fees, memos), [`txflow/`](../txflow) (proposed ->
validated timing) and [`keyposture/`](../keyposture) (account key settings) do not measure.

It is read-only. The collector talks only to your own node (admin JSON-RPC through `docker exec`)
and its client refuses every method except `server_info`, `ledger` and `fee`. The crosscheck sends
only `server_info` and `tx` to the public RPC. Nothing is submitted or signed.

## Files

| file | what it does |
|---|---|
| `collect_txhygiene.py` | resumable collector: forward-follows validated ledgers (`ledger`, transactions expanded) at <= 1 req/s and writes fsync'd JSONL: `tx.jsonl` (one construction record per tx, fields below), `fees.jsonl` (one `fee` sample per newly validated ledger while caught up), `ledgers.jsonl`, `gaps.jsonl`, `retries.jsonl`, `restarts.jsonl`, plus `state.json` (window). Client-side window end; on resume a torn last line is cut from every file and tx lines of a ledger without its `ledgers.jsonl` line are dropped and fetched again |
| `analyze_txhygiene.py` | report, JSON and 3 charts: expiry margins, fee / required ratio, overpay drops and their concentration by account (Gini, top-1/top-5, Lorenz curve), fee-escalation samples, signing algorithms, Sequence/Ticket and per-account sequence steps, optional fields, field-set client classes |
| `reconcile_txhygiene.py` | part 1: the window alone (every ledger once, no gaps, every tx once, per-ledger `txn_count` == tx lines, `tx_index` 0..n-1); part 2: against an independent `txmix/collect_tx.py` collection of the same window run as a separate process (per-ledger hash and `txn_count`, same tx hashes with the same ledger/index/type/result/fee, txmix-style window totals). `--negative` plants a dropped ledger and a +1-drop fee and must see both |
| `crosscheck_txhygiene.py` | 20 seeded random tx hashes re-fetched from the public RPC (`tx`), rebuilt with the collector's own `tx_row()` and compared in all 21 fields; every mismatch and every retry printed. `--negative` alters every local field and must see 21/21 mismatches per tx. `--offline FILE` re-checks a recorded run with no RPC |
| `resume_drill_txhygiene.sh` | drill on a retained ledger range: reference run vs a run that gets SIGTERM, then SIGKILL, then an injected orphan + torn `tx.jsonl` tail; `tx.jsonl` must be byte-identical to the reference |

Tests: `tests/test_txhygiene_collect.py`, `test_txhygiene_analyze.py` (known-answer fixtures for
every statistics function: quantile, Gini, top share, Lorenz, margin and ratio buckets, fee matching),
`test_txhygiene_reconcile.py`, `test_txhygiene_crosscheck.py`.

## Fields of `tx.jsonl`

| field | meaning |
|---|---|
| `ledger_index`, `close_time`, `tx_index`, `tx_hash` | where the tx was validated (close time in Ripple epoch seconds) |
| `account` | 16-hex HMAC-SHA256 token of the sender, keyed with a random local key (`.hmac_key`, 0600, never published); the report prints only 8-hex prefixes |
| `tx_type`, `result`, `fee_drops` | as validated |
| `sequence`, `ticket_seq` | `Sequence` and `TicketSequence` (null when absent; a Ticket tx has Sequence 0) |
| `lls`, `lls_margin` | `LastLedgerSequence` and `LastLedgerSequence - ledger_index` (null when absent) |
| `key_type` | from the `SigningPubKey` prefix only: `ED` + 64 hex = ed25519, `02`/`03` + 64 hex = secp256k1, empty with `Signers` = multisig; the key itself is not stored |
| `signers` | number of `Signers` |
| `network_id` | `NetworkID` (null when absent) |
| `source_tag`, `dest_tag`, `account_txn_id` | `y`/`n`: field present |
| `flags`, `memos` | `Flags` value (0 when absent; see `fieldset` for presence), number of memos |
| `fieldset` | sorted top-level field names the client put in the tx, joined by `+`; server-added keys (`hash`, `metaData`, `DeliverMax`, `ctid`, `date`, `inLedger`, `ledger_index`, `validated`) are excluded |

`fees.jsonl`: `validated` (ledger validated when sampled), `ledger_current_index`, `base_fee`,
`median_fee`, `minimum_fee`, `open_ledger_fee` (drops), queue and ledger sizes.

## Method notes and limitations

- **Validated ledgers only.** Transactions that expired, were dropped from the queue or never left
  the client are invisible; so a "0 transactions without LastLedgerSequence" figure describes what
  got validated, not every client. Margins are measured at *inclusion*; the margin the client set at
  submit time is that plus the ledgers it waited, which the ledger does not record.
- **Required fee** = the base fee of the newest `fee` sample taken before the tx's ledger closed,
  times (1 + signers) for multi-signed tx. Special-cost tx types (e.g. AccountDelete) would show as
  "below required"; the report lists that count. The open-ledger fee is a per-ledger sample of what
  a new tx would have needed at that moment, not the fee the sender saw.
- **Client classes** are field-presence patterns, not software names; two libraries with the same
  defaults look the same.
- **Sequence steps** are measured between an account's consecutive Sequence-based tx inside the
  window; a step above +1 means sequences were used by something else (TicketCreate consumes several).
- **Retention.** The node keeps ~512-1024 ledgers (`online_delete`), so the resume drill and the live
  interrupts ran during the window; the public-RPC crosscheck uses only ledgers inside the public
  server's `complete_ledgers` and prints how many rows were not checkable.

## Redaction

No address, public key or signature is stored. Accounts are keyed HMAC tokens; transaction hashes
are public identifiers and are kept so every row can be re-fetched. Published logs are run from the
repo with relative paths; they are checked for home paths, user names, local time offsets and IP
addresses before commit.

## Run it

```sh
python3 -u txhygiene/collect_txhygiene.py --out data/txhygiene/tx --log data/txhygiene/tx/collect.log --hours 24.25
python3 -u txmix/collect_tx.py --out data/txhygiene/txmix --log data/txhygiene/txmix/collect.log --start <same first ledger> --hours 24.25
# during the window (needs retained ledgers):
TERM_AFTER=25 KILL_AFTER=25 bash txhygiene/resume_drill_txhygiene.sh <first> <last> data/txhygiene/drill
# after the window:
python3 txhygiene/reconcile_txhygiene.py --tx data/txhygiene/tx --twin data/txhygiene/txmix
python3 txhygiene/reconcile_txhygiene.py --tx data/txhygiene/tx --twin data/txhygiene/txmix --negative
python3 -u txhygiene/crosscheck_txhygiene.py --data data/txhygiene/tx --json crosscheck.json
python3 -u txhygiene/crosscheck_txhygiene.py --data data/txhygiene/tx --negative --json crosscheck-negative.json
python3 txhygiene/crosscheck_txhygiene.py --data data/txhygiene/tx --offline crosscheck.json   # no RPC
.venv/bin/python txhygiene/analyze_txhygiene.py --data data/txhygiene/tx --out txhygiene/report --charts
```

## Live run

| log | what |
|---|---|
| `logs/01-window-start.log` | both collectors started at ledger 7829493 (2026-11-07T02:14:20Z, 24.25 h), pft-maint data-collection lock |
| `logs/02-tests-lint.log` | collector tests, full suite, ruff, shellcheck |
| `logs/03-resume-drill.log` | resume drill on ledgers 7829305-7829425: PASS 9/9; negative control (collector copy whose resume skips the orphan drop) FAIL 4/9 as expected |
| `logs/04-live-interrupts.log` | the live 24 h collector itself: SIGTERM, resume, SIGKILL, resume; window contiguous afterwards |
| `logs/05-window-end-reconcile.log` | both STOP lines (exit 0), `restarts.jsonl` (start + 2 resumes), reconciliation PASS 15/15, negative control: all planted faults reported |
| `logs/06-public-crosscheck.log` | 20 random tx re-fetched from the public RPC: 20/20 match in all 21 fields, 0 retries; negative 20/20 with 21/21 fields differing; offline re-check PASS |
| `logs/07-analyze.log` | input sha256 and the analyzer run that wrote `report/` |
| `logs/08-publish-copy.log` | window data copied to `data/` without the HMAC key (11 files, sha256-identical); report, JSON and reconciliation re-run from the published copy (first re-run omitted `--charts`, so `REPORT.md` differed only by the 3 image lines; re-run with `--charts` identical) |
| `logs/09-margin-overpay-crosstab.log` | the long-margin and overpaying tx are the same 66 tx |
| `logs/10-tests-lint.log` | 56 txhygiene tests, full suite 589 passed, ruff, shellcheck |

`data/tx/` (collector output: `tx.jsonl`, `fees.jsonl`, `ledgers.jsonl`, `restarts.jsonl`, `state.json`,
`collect.log`), `data/txmix/` (the independent twin collection), `data/reconcile.json`,
`data/crosscheck.json` (the public answers, for `--offline`) and `data/crosscheck-negative.json`.
Re-run the report with `.venv/bin/python txhygiene/analyze_txhygiene.py --data txhygiene/data/tx --out /tmp/r --charts`.

## Results (2026-11-07T02:14:20Z -> 2026-11-08T02:29:20Z)

Full report: [`report/REPORT.md`](report/REPORT.md). 28854 ledgers (7829493-7858346), 9943 validated
tx from 53 accounts (Payment 8023, NFTokenMint 1033, NFTokenBurn 887; 3 `tecUNFUNDED_PAYMENT`).

- **Expiry.** Every tx set `LastLedgerSequence` (0 of 9943 without). 9877 tx were included with 17-19
  ledgers to spare (about 1 minute at the window's ~3.0 s per ledger); 66 tx had 118-119. No
  negative margins. On this window no validated tx could have been replayed late by a missing expiry.
- **Fees.** Base fee 10 drops in all 28702 samples; the open-ledger fee never rose above base and the
  queue was always empty. 9877 tx paid exactly 10 drops; 66 tx paid 12 (1.2x). Total overpay 132 of
  99562 fee drops (0.13 %), from 6 of 53 accounts; one account paid 92.4 % of it (Gini 0.973).
  0 tx paid below the required fee.
- **Two construction families.** The 66 long-margin tx and the 66 overpaying tx are the same tx
  (`logs/09`): one family with fee 12, margin ~118 and an explicit `Flags` field (1 ed25519 account
  sending Payments with Memos, 61 tx; 5 secp256k1 accounts minting one NFT each with `TransferFee`).
  The other 9877 tx from 47 accounts have fee 10, margin 17-19 and no `Flags` field. No account was
  seen in both families.
- **Signing.** ed25519 9938 tx / 48 accounts, secp256k1 5 tx / 5 accounts; no multisig.
- **Sequences.** All Sequence-based, 0 Ticket tx; 9890 steps between an account's consecutive tx,
  all +1 (no sequence holes, no reuse); at most 1 tx per account per ledger.
- **Optional fields.** Every tx carries `NetworkID` 2025; no `SourceTag`, `DestinationTag` or
  `AccountTxnID` anywhere; Memos on 61 tx (one account).

What this does not show: transactions that were never validated (see limitations), and which
software built each family. Field patterns are not library names.
