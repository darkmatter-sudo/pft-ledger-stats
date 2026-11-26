# dunl: Dynamic UNL round history and consistency audit

Post Fiat's testnet UNL is chosen by the Dynamic UNL scoring service once a week: it scores the
validators, signs a Validator List (VL), publishes the round's audit-trail bundle, mirrors the VL to
`postfiat.org`, and anchors the round on-chain with a memo transaction. Since protocol v1, validators
also re-run the scoring and commit/reveal their output hashes on-chain, and a sealed convergence
report is anchored too.

`dunl/` checks that all of these public artifacts agree with each other, round by round, and with
what this operator's own two nodes (validator and sentry) actually trust. It also turns the round
history into membership-churn, tenure, score, entry-time and participation tables.

It is read-only and uses public data only. The collector sends HTTP GETs to the scoring API and the
VL mirror, and the JSON-RPC methods `tx`, `server_info` and `ledger` to the public RPC. Nothing is
signed or submitted. `nodes_dunl.sh` runs the admin `validators` command inside the operator's own
node containers. It reads only and never restarts or reconfigures a node.

## What is checked

The sources are `postfiatorg/dynamic-unl-scoring` at `4c3eb28a59fb8e08add0b44f4f24a8c7ff6dac6a`.

| check | what must hold | how |
|---|---|---|
| `vl_decode` | the round's signed VL decodes. The publisher manifest's master signature (Ed25519) and signing signature verify. The blob signature verifies (secp256k1 ECDSA over SHA-512-Half, canonical low-S). Every validator manifest's master key equals its `validation_public_key`, and its master and signing signatures verify | `lib_dunl.py`: pure-stdlib Ed25519 (RFC 8032) and secp256k1 verifiers. The signed manifest data is `MAN\0` plus the STObject without `sfSignature`/`sfMasterSignature`. This is the scheme of `scoring_service/services/vl_generator.py` (`sign_blob`, `parse_manifest`) |
| `sequence` | blob `sequence` == the round's `vl_sequence`, and published sequences step by exactly +1. A round with no VL is WARN if its status is `FAILED`, otherwise FAIL | |
| `validity_window` | `effective < expiration`. Each list stays valid until the next list is effective, so no time is left without a valid list. The latest list is not expired at the snapshot time | |
| `validator_set` | VL validators == `outputs/selected_unl.json` (legacy rounds: `unl.json`). Size <= `unl_max_size` from `/api/scoring/config`. Round N's `inputs/previous_unl.json` == the UNL published by the previous published round | |
| `selection` | replaying the selector on the round's own `outputs/final_scores.json`, `inputs/previous_unl.json` and selector parameters (`runtime/execution_manifest.json`: cutoff, max size, min score gap) reproduces `selected_unl.json` exactly: same UNL, same alternates, same order | `replay_selection` re-implements `scoring_service/services/unl_selector.select_unl` (cutoff, cap on incumbents, open seats to top challengers, a challenger displaces the weakest member only if it beats it by >= min gap) |
| `bundle_hashes` | every `bundle.json` `file_hashes` entry and every `outputs/verification_hashes.json` entry equals the content hash of the served file | the content hash is sha256 of compact sorted-key JSON (`ipfs_publisher._content_hash`) |
| `mirror` | live `/vl.json` == `https://postfiat.org/testnet_vl.json` == the latest round's `signed_validator_list.json`, and `/api/scoring/unl/current` == the latest UNL | |
| `anchor` | the round's `memo_tx_hash` is a validated `tesSUCCESS` tx from `foundation_publisher_address`. Its MemoType equals the memo's `type`. The memo's `final_bundle_cid`, `vl_sequence` and `round_number` match the round record | `onchain_publisher._build_memo_payload` |
| `convergence` | a normal, announced (frozen input package), COMPLETE round after tracking began must not be `not_tracked` (WARN: the API documents `not_tracked` only for override, not-yet-announced or pre-protocol rounds). For each sealed report, the summary (committers, outcomes, levels matched, divergence categories) equals a recount of `participants`. The anchor tx is a publisher memo of type `pf_dynamic_unl_convergence_report_v1` that carries this round and `convergence_bundle_cid`. Every accepted commit and reveal tx is validated `tesSUCCESS` | `onchain_publisher.publish_convergence_report`, `convergence_verification.compare_levels` |
| `nodes` | each own node trusts exactly the latest VL's validators, from the VL publisher key, at the latest sequence. Its quorum lies in [ceil(0.6 n), ceil(0.8 n)]: 80% of the trusted keys, reduced by NegativeUNL to no less than 60%. The two nodes agree | `nodes_dunl.sh` snapshots |

Every result has a reason. FAIL means two public artifacts contradict each other. WARN means a
check could not be completed for a stated, benign cause:
- the round itself declares the cause (`FAILED`, `VL_PUBLISHED_MEMO_FAILED`);
- the round uses the early legacy layout, which has no bundle hashes;
- or the transaction predates the public RPC's retained history (see below).

**Public RPC history.** The public RPC prunes old ledgers. `tx` answers `txnNotFound` for anything
older than its first retained ledger. The collector records `server_info.complete_ledgers` and the
close time of the first retained ledger. A `txnNotFound` tx whose round completed before that time is
reported as WARN "not verifiable (pruned)". A `txnNotFound` tx inside the retained range is a FAIL.

## Files

| file | what it does |
|---|---|
| `lib_dunl.py` | base58 node keys, STObject manifest parser, VL decoder, Ed25519 + secp256k1 verifiers |
| `collect_dunl.py` | `collect` is a resumable, rate-limited collector (0.5 s gap; exponential backoff on 429/5xx/transport errors). Its cache `data/dunl/cache.jsonl` holds one row per response: key, URL, UTC fetch time, HTTP status, sha256 and size of the raw body, and the body. `pack` writes the deterministic committed subset |
| `nodes_dunl.sh` | trusted-key snapshot of the own validator and sentry (peer `uri` fields dropped) |
| `audit_dunl.py` | offline checks, analyses, CSVs, SVG charts, `REPORT.md` and `SHA256SUMS`. Same inputs give byte-identical outputs |
| `make_fixtures.py` | builds `tests/fixtures/dunl/` from a real cache (rounds 30-33 and 2-5) |
| `data/cache.jsonl.gz` | committed input pack. The bodies of large evidence/raw inputs, which also hold third-party network details, are replaced by their content hash. That hash is what `file_hashes` covers |
| `data/nodes/*.json` | committed node snapshots |
| `report/` | `REPORT.md`, `checks.json`, `membership.csv`, `tenure.csv`, `scores.csv`, `entry_times.csv`, `participation.csv`, `charts/*.svg`, `SHA256SUMS` |
| `logs/` | recorded runs (collection, node snapshots, audit, tests, fresh clone) |

Tests: `tests/test_dunl.py`. They use the captured responses plus one synthetic mismatch per check.

## Results (collection 2026-11-26, rounds 2..33)

Full report: [`report/REPORT.md`](report/REPORT.md). Every result with its reason: [`report/checks.json`](report/checks.json).

**234 results: 178 PASS, 56 WARN, 0 FAIL.** No two public artifacts contradict each other:
- every one of the 31 signed VLs verifies cryptographically: the publisher manifest, the blob, and every validator manifest;
- sequences run 1..31 with no gap;
- the validity windows overlap without a gap;
- each VL equals its selected UNL and the next round's `previous_unl`;
- replaying the selector reproduces all 18 rounds with published scores exactly (UNL and alternates, in order);
- every bundle and verification hash matches;
- the live VL, the `postfiat.org` mirror and round 33 are byte-identical;
- the round 33 anchor and its 34 commit/reveal txs check out on-chain;
- both own nodes trust exactly the 25 round-33 keys at sequence 31.

The 56 WARN, by cause:

| WARN | count | cause |
|---|---:|---|
| `anchor` rounds 4-32, `convergence` rounds 12-28, 30-32 | 29 + 20 | the tx predates the public RPC's retained history (ledgers 8300055+, first closed 2026-11-23T14:02Z): not verifiable from public RPC. The convergence summaries of these rounds still equal the recount of their participants |
| `bundle_hashes` rounds 3-6 | 4 | early legacy layout without `bundle.json` |
| `sequence` round 2 | 1 | round `FAILED`, no VL (declared by the round itself) |
| `anchor` round 3 | 1 | round `VL_PUBLISHED_MEMO_FAILED` (declared by the round itself) |
| `convergence` round 29 | 1 | `/rounds/29/convergence` answers `not_tracked`, but round 29 is a normal `COMPLETE` round with a frozen input package (announced) between sealed rounds 28 and 30. The endpoint documents `not_tracked` only for override, not-yet-announced or pre-protocol rounds |

Recorded runs (`logs/`):

| log | what |
|---|---|
| `01-collect-full.log` | full collection into a fresh cache at `dec9a5f`: 32 rounds, 1,279 requests, 0 retries, 0 errors, 737 txs |
| `02-tests.log` | `pytest -v tests/test_dunl.py` (38 passed, 0 failed), `ruff check`, `ruff format --check`, `shellcheck` |
| `03-nodes.log` | trusted-key snapshots of the own validator and sentry |
| `04-pack-audit.log` | pack of the recorded cache, input sha256s, full audit output (every result) and the report `SHA256SUMS` |
| `05-fresh-clone.log` | anonymous fresh clone: tests, lint, offline audit, regenerated report byte-identical to the committed one |

Log 01 ran at `dec9a5f`. The next commit, `f064c75`, changed only `pack` (it trims `server_info`), the tests and the fixtures. Logs 02-04 ran at `f064c75`.

## Reproduce

From a fresh clone. Steps 1-4 re-verify the committed report offline; step 5 collects anew.

```bash
git clone https://github.com/darkmatter-sudo/pft-ledger-stats && cd pft-ledger-stats
# 1. tests
python3 -m pytest -q tests/test_dunl.py
# 2. re-run the audit on the committed inputs
python3 -I dunl/audit_dunl.py --cache dunl/data/cache.jsonl.gz --nodes dunl/data/nodes --out /tmp/dunl-report
# 3. the regenerated files are byte-identical to the committed report
(cd /tmp/dunl-report && sha256sum -c "$OLDPWD/dunl/report/SHA256SUMS") && cmp /tmp/dunl-report/SHA256SUMS dunl/report/SHA256SUMS
# 4. lint
ruff check dunl tests/test_dunl.py && ruff format --check dunl tests/test_dunl.py && shellcheck dunl/nodes_dunl.sh
# 5. collect anew (~20 min, about 1,300 requests) and audit
python3 -I dunl/collect_dunl.py collect --out data/dunl
sudo bash dunl/nodes_dunl.sh data/dunl/nodes            # your own nodes; edit the name=container[@ssh-host] args
python3 -I dunl/audit_dunl.py --cache data/dunl/cache.jsonl --nodes data/dunl/nodes --out data/dunl/report
```

The audit exits 0 when there is no FAIL and 2 otherwise.
