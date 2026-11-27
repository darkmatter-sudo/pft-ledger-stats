# endpoints: Dynamic UNL endpoint visibility and concentration inputs

The Dynamic UNL scoring service scores each validator's infrastructure diversity from its resolved public
endpoint: provider family (grouped ASN names) and country. Since prompt v9 the prompt carries precomputed
`NETWORK CONCENTRATION` counts, and the validator guide tells operators to pick providers with few
validators in that block. `endpoints/` checks that block independently for every published COMPLETE round:

- it recomputes the provider-family / country / unresolved counts from the round's own
  `inputs/validator_evidence.json` and compares them with the block in `inputs/model_request.json`
  (`match`, `mismatch` or `block_missing`). It also checks each prompt row's `provider_family`/country
  against the evidence, and `inputs/diversity_inputs.json` against the recount where a round publishes it;
- it checks every fetched file's content hash against the round's published `bundle.json` `file_hashes`;
- it measures endpoint visibility per round: resolved / unresolved, unresolved validators whose address
  was seen but did not resolve to an ASN, and which master-key prefixes changed state from the previous
  round. It also reports family and country concentration (top 5 each, HHI);
- it tests the guide's claim (postfiatorg/postfiatorg.github.io@8da236f, `content/validator-setup.md`
  line 580) that unresolved validators land at the bottom of the diversity range, below even the most
  crowded visible provider. It uses the published diversity sub-scores in `outputs/validator_scores.json`;
- it cross-checks round 33's `domain_verified` (VL sequence 31) against [`valdomain/`](../valdomain/README.md)'s
  independent attestation audit of the same list (`valdomain/report/results.json`).

The family rules re-implement `scoring_service/services/provider_families.py` of
`postfiatorg/dynamic-unl-scoring` at `4c3eb28a59fb8e08add0b44f4f24a8c7ff6dac6a`. The grouping code
(name normalization, the hetzner/vultr/akamai rules, the own-family fallback) is identical at every scoring
commit named by a round's `runtime/execution_manifest.json`, so one implementation serves all rounds.

## Network footprint (bounded)

`collect` sends plain HTTP GETs to `https://scoring-testnet.postfiat.org` only. It reads the round list
(one live document, fetched once per `--epoch`), then for each COMPLETE round `bundle.json` and the five
files above if the bundle lists them. That is 146 requests for 30 rounds. Requests are spaced 1.0 s
apart. `--gap` below 1.0 is refused. Every round document is fetched once: a key is final on HTTP 200
or 404, and a re-run resumes from the cache without refetching. Retries on 429/5xx use exponential
backoff. Nothing touches a node: no `postfiatd` command, change or restart on any host.

## Privacy and third parties

The raw evidence holds other operators' IP addresses. The raw cache (`data/endpoints/cache.jsonl`) stays
local and is git-ignored. `pack` writes the committed input `endpoints/data/pack.jsonl.gz` with, per
round: the raw sha256, size and content hash of each fetched file next to the published hash, the
concentration block, the prompt's per-row `(provider_family, country)`, and per validator only:

| field | why it is kept |
|---|---|
| `master_key` | public validator key; needed for key-prefix changes, the rank test and the valdomain crosscheck |
| `endpoint` (`resolved`/`unresolved`), `address_seen` (boolean) | resolution status. The address itself is never copied |
| `asn`, `as_name`, `country` | the ASN record and geolocation the counts are computed from |
| `domain`, `domain_verified` | the round 33 crosscheck |
| `diversity` | published diversity sub-score, for the rank test |

`pack` refuses to write if any IPv4/IPv6 address is left in its output. `verify_endpoints.py` treats an
address in the pack as an internal inconsistency (exit 1), and a test re-scans the committed pack.
Reports name validators only by 8-character master-key prefixes.

## Files

| file | what it does |
|---|---|
| `lib_endpoints.py` | family grouping, concentration counts, content hash, block / prompt-row extraction, HHI, IP scanner |
| `collect_endpoints.py` | `collect` (rate-limited, resumable raw cache) and `pack` (committed IP-free input, gzip mtime 0) |
| `verify_endpoints.py` | `--offline` checks and analyses; writes `results.json`, `rounds.csv`, `REPORT.md`, `SHA256SUMS`. Same inputs give byte-identical files. Exit 0 all checks ran (whatever they found), 1 internal inconsistency in the pack, 2 no data |
| `mutants.py` | 29 one-line mutants of the library, verifier and collector; the test suite must catch each one |
| `data/pack.jsonl.gz` | committed input |
| `report/` | committed output |
| `logs/` | recorded runs |

Tests: [`tests/test_endpoints.py`](../tests/test_endpoints.py). They use hand-built rounds (documentation-range
addresses), a fake network and clock, negative controls, and a check that the committed pack regenerates
the committed report byte for byte.

## Run it

```bash
git clone https://github.com/darkmatter-sudo/pft-ledger-stats && cd pft-ledger-stats
python3 -m pytest -q tests/test_endpoints.py
ruff check endpoints tests/test_endpoints.py && ruff format --check endpoints tests/test_endpoints.py
sha256sum endpoints/data/pack.jsonl.gz
# offline re-verification (works without network, e.g. inside `unshare -rn`)
python3 -I endpoints/verify_endpoints.py --offline --out /tmp/ep-report
(cd /tmp/ep-report && sha256sum -c "$OLDPWD/endpoints/report/SHA256SUMS") && cmp /tmp/ep-report/SHA256SUMS endpoints/report/SHA256SUMS
# mutants (absolute TMPDIR)
TMPDIR=/tmp python3 -I endpoints/mutants.py --jobs 3
# collect anew (~3 min) and pack
python3 -I endpoints/collect_endpoints.py collect --out data/endpoints --epoch "$(date -u +%F)"
python3 -I endpoints/collect_endpoints.py pack --cache data/endpoints/cache.jsonl --out /tmp/pack.jsonl.gz --epoch "$(date -u +%F)"
```

## Results (collection 2026-11-27, rounds 4-33)

Full report: [`report/REPORT.md`](report/REPORT.md). The round list has 32 rounds: 30 COMPLETE (4-33), round 2
`FAILED`, round 3 `VL_PUBLISHED_MEMO_FAILED`. All 30 COMPLETE rounds are processed.

- **Block: 17 match, 0 mismatch, 13 block_missing.** Rounds 17-33 render the block (prompt v9 and later).
  In all 17 the recount from the evidence equals it exactly: every family count, every country count,
  the unresolved count and the order. Rounds 7-16 have frozen inputs but no block (prompts v5-v8).
  Rounds 4-6 use the legacy layout with no frozen inputs.
- **Hashes:** 115 fetched files, all equal to the published `file_hashes`.
- **Visibility:** round 33 has 66 validators, 52 resolved and 14 unresolved. The largest family is
  hetzner with 16. See the per-round table for counts, prefix changes and HHI.
- **Guide claim:** unresolved validators were below the most crowded visible family in 19 of 27 scored
  rounds (7-33). It fails in rounds 8, 20-23, 25, 26 and 29. In 20-23, 25 and 26 one unresolved
  validator scored 60-85 while hetzner's lowest was 30-45. In 8 and 29 the highest unresolved score
  ties the lowest hetzner score. The claim holds in every round from 30 to 33.
- **Round 33 vs valdomain:** `domain_verified` agrees with valdomain's class for 25 of 25 VL validators.

## Limits

The checks cover what the published inputs say, not whether the scoring service's ASN and geolocation
lookups were right. Rounds before v9 have no block to compare: they count as `block_missing`, and their
visibility and rank numbers come from the evidence alone.
