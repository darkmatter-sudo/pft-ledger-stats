# issued/ — issued-asset (IOU) and trust-line census, Post Fiat testnet

**Status: complete.** Snapshot A (ledger 8059029, 2026-11-15T03:10:51Z) and snapshot B (ledger
8087575, 2026-11-16T03:11:01Z, 24.00 h later) are committed with their public-RPC crosschecks, the
A->B diff in `report/`, the mutation run and the fresh-clone verification log.

Read-only. Nothing is signed or submitted. Local node methods: `server_state`, `ledger`,
`ledger_data`, `gateway_balances`, `amm_info` (admin JSON-RPC through `docker exec`, never exposed).
Public RPC (`https://rpc.testnet.postfiat.org`): `server_info`, `ledger`, `ledger_data`,
`gateway_balances`, `account_lines`. Python 3 stdlib only. Any other method is refused before it is sent.

## Files

| file | what |
|---|---|
| `collect_issued.py` | snapshot of one pinned validated ledger from your own node |
| `crosscheck_issued.py` | the same ledger checked against public RPC (`--record`, `--offline`, `--negative`) |
| `analyze_issued.py` | offline census, gateway reconciliation and A->B diff (deterministic) |
| `mutants_issued.py` | mutation check of the three scripts against `tests/test_issued_*.py` |
| `data/A/`, `data/B/` | committed raw snapshots (`MANIFEST.json`: ledger index/hash, rows and sha256 per file) |
| `data/crosscheck/` | recorded public RPC answers (offline replay) |
| `report/` | `REPORT.md` + `report.json` |
| `logs/` | raw run logs |

## Snapshot (collect_issued.py)

```bash
python3 -I issued/collect_issued.py snapshot --out issued/data/A
```

Pins the validated ledger (index + hash) and then, all by `ledger_hash`:

1. pages the full state tree (`ledger_data`, binary=false) and counts every LedgerEntryType;
2. pages `ledger_data type=state` and `type=amm` again and stops (no MANIFEST) if the index sets
   differ from the full tree (`counts.json`);
3. keeps every RippleState and AMM object and the AccountRoot of every trust-line party;
4. asks `gateway_balances` for every trust-line party and `amm_info` for every (issuer, currency)
   with outstanding obligations paired with the native asset;
5. writes the files with sorted keys and rows sorted by index/account, fsyncs them, and writes
   `MANIFEST.json` last.

Transient failures (docker/curl errors, timeouts, unparsable answers, `tooBusy`, `noNetwork`) are
retried with exponential backoff; each retry is a `RETRY` line; a call that never answers is `GAVE_UP`
(exit 2). Definite errors (`actNotFound`) are not retried.

## Census rules (analyze_issued.py)

`RippleState.Balance` is from the low account's side:

| Balance | holder | issuer | amount |
|---|---|---|---|
| > 0 | low | high | Balance |
| < 0 | high | low | -Balance |
| = 0 | the side with a positive limit | the other side | 0 |
| = 0, both or no limits | — | `(ambiguous)` | 0 |

Per (issuer, currency) (a currency code from two issuers is two assets): lines, holders (amount > 0),
obligations (exact decimal sum), top1 / top10 share, HHI (sum of squared shares x 10000), zero lines,
over-limit lines (amount > holder's limit), freeze flags set by the issuer / by the holder, NoRipple on
the issuer / holder side. Hex currency codes are decoded when printable ASCII; `03...` codes are AMM
LP tokens. Issuer settings: AccountRoot flags, TransferRate (fee %), TickSize, Domain. Rippling-through
accounts: an account with >= 2 lines of one currency whose own side has NoRipple clear.
AMM: AMM objects in the full tree and by type filter, LP-token lines, and the `amm_info` answers;
none of them present gives `none (query-backed)`.

### gateway_balances reconciliation

For every trust-line party the expected `gateway_balances` answer is derived from the RippleState
objects (rippled GatewayBalances model, no hotwallet): from the account's side a positive balance is
an asset, a negative balance on a line the account itself froze is a frozen balance, any other
negative balance is an obligation summed per currency. It must equal the recorded answer exactly
(Decimal, not float). The census obligations per (issuer, currency) must also equal gateway
obligations + frozen balances. Object counts: full tree = type filter for RippleState and AMM, and
the snapshot rows = the full-tree count. Every difference is listed under Mismatches and the run exits 1.

```bash
python3 -I issued/analyze_issued.py report --a issued/data/A [--b issued/data/B] --out issued/report
```

## Public crosscheck (crosscheck_issued.py)

At the pinned ledger hash: ledger header fields; the RippleState and AMM index sets (missing and extra
both MISMATCH) and every object field for field; `gateway_balances` of every party = the local answer;
`account_lines account=X peer=Y` for a seeded random sample of lines (seed = ledger hash; `--sample K`,
default 20, so with fewer lines every line is checked) from a randomly drawn side, compared with the
view derived from the RippleState object (balance sign, limits, NoRipple, freeze, auth). A ledger
outside the public `complete_ledgers` exits 2; `lgrNotFound` inside the range is retried and logged.
`--negative` alters every expectation, so every check must MISMATCH (the control).

```bash
python3 -I issued/crosscheck_issued.py issued/data/A --record issued/data/crosscheck/A-public.json
python3 -I issued/crosscheck_issued.py issued/data/A --offline issued/data/crosscheck/A-public.json
```

## Tests and mutation check

```bash
TMPDIR=$HOME/.cache python3 -m pytest -q tests/test_issued_*.py
ruff check issued tests/test_issued_*.py tests/fixture_issued.py
TMPDIR=$HOME/.cache python3 issued/mutants_issued.py
```

`tests/fixture_issued.py` is a hand-built ledger (docstring table): two-sided signs, an issuer freeze
(goes to `frozen_balances`), a holder freeze, NoRipple on either side, a zero line, an ambiguous zero
line, `USD` from two issuers, a hex currency (`EURO`), an over-limit line, issuer settings
(DefaultRipple, RequireAuth, TransferRate, Domain). The expected numbers in the tests are computed by
hand in comments. The collector and crosscheck tests run the real code against a fake node.

## Results

Snapshot A, ledger 8059029 (`logs/01-snapshotA.log`): 5199 objects in the full tree, 4 RippleState
(full tree 4 = type filter 4, index sets equal), 0 AMM (0 = 0). One asset: DLK issued by
`rhaQYQ11ckRQmg9axsE1SBjqgWXPKhgteE`, 4 holders x 1000000, obligations 4000000, top1 0.25, HHI 2500,
issuer Flags 0 (no DefaultRipple, so NoRipple is set on the issuer side of all 4 lines), no freezes,
no zero or over-limit lines, no rippling-through accounts. AMM: none (query-backed: 0 AMM objects,
0 LP-token lines, `amm_info` DLK/native `actNotFound`). gateway_balances 5/5 accounts exact.

Public crosscheck of A (`logs/02`): 19 checks, 19 MATCH (4 header, 1+4 RippleState, 1 AMM set,
5 gateway_balances, 4 of 4 lines via `account_lines`), 0 retries. Offline replay 19 MATCH; negative
control 19 MISMATCH, exit 1 (`logs/03`).

Mutation run (`logs/04`): control 40 passed, 45 mutants, 45 caught. Report A and offline rebuild:
`cmp` exit 0 for both files (`logs/05`). Retry path on the real docker exec transport (`logs/07`):
3 logged RETRY lines with 0.5/1/2 s backoff, then GAVE_UP exit 2 and no files written. The live
snapshot and crosscheck runs needed 0 retries.

Snapshot B, ledger 8087575 (`logs/08-snapshotB.log`): 5219 objects, 4 RippleState (4 = 4, index sets
equal), 0 AMM (0 = 0), gateway_balances 5/5 exact, 0 retries. `ripplestate.jsonl`, `accounts.jsonl`
and `gateway_balances.jsonl` are byte-identical to A (same sha256 in both manifests). Public crosscheck
of B (`logs/09`): 19 checks, 19 MATCH, 0 retries; offline replay 19 MATCH, negative control 19
MISMATCH, exit 1 (`logs/10`).

A -> B (`report/REPORT.md`, `logs/11`): lines added 0, removed 0, changed 0; issuer-setting changes 0;
DLK obligations 4000000 -> 4000000, holders 4 -> 4; AMM none at both ledgers. Object-type count
changes: AccountRoot 4669 -> 4687, LedgerHashes 124 -> 125, NFTokenPage 299 -> 300. Report A+B and
offline rebuild: `cmp` exit 0 for both files.
