# valdomain: does every validator domain in the Post Fiat testnet VL attest back to its key?

Every validator in the published testnet Validator List (VL) carries a signed manifest, and the
manifest can name a domain (`sfDomain`). A domain is a claim made by the key holder. The claim is
only two-way when the domain publishes a **domain attestation**: a signature by the validator's
master key over `[domain-attestation-blob:<domain>:<master key>]` (validator-keys `set_domain`,
the tool the [Post Fiat validator guide](https://postfiat.org/validator-setup/) uses), listed in a
`[[VALIDATORS]]` entry of a TOML file on that domain.

`valdomain/` checks this for every VL validator and classifies each one. The collector and the
offline verifier are read-only and reproducible, and nothing on the node changes:

1. **VL**: the collector fetches the list from the site your node uses (`validator_list_sites`),
   hashes it, and checks the publisher manifest and the blob signature (low-S). It also checks that
   your node trusts that publisher key, that list sequence and exactly those validators. The
   signature code is the code `valsig/` uses (`valsig/lib_valsig.py` -> `dunl/lib_dunl.py`).
2. **Manifests**: every VL validator manifest must verify (master + signing signature). The domain
   is decoded from `sfDomain`.
3. **Attestation files**: one HTTPS GET per domain for each of two paths, then parse and verify.
4. **Crosscheck**: an independent domain check. For every validator, your own node and the public
   node `rpc.testnet.postfiat.org` each answer `manifest`. Each answer must verify, name the same
   master and not be older than the VL's manifest. At the same sequence it must carry the same
   domain, and the node's decoded `details.domain` must equal our own decode of the blob.

### Paths fetched

The Post Fiat guide (section 8, "Publish Domain Attestation") publishes the proof at
**`/.well-known/pft-ledger.toml`**. The XRPL-standard path is `/.well-known/xrp-ledger.toml`. Both
are fetched, and a validator's class is the best outcome over the two paths. Each path's own class
is reported as well.

## Classes

Listed best first. The verifier takes the best class over the paths.

| class | meaning |
|---|---|
| `verified` | a `[[VALIDATORS]]` entry with `public_key` = the master key (exact string) has an `attestation` that verifies for this domain and key |
| `bad_attestation` | the key is listed with an attestation, but no listed attestation verifies (wrong domain, wrong key, not hex) |
| `no_attestation` | the key is listed, but no entry for it has a non-empty string `attestation` |
| `key_not_listed` | the file parses, but no `[[VALIDATORS]]` entry names this master key |
| `toml_unparseable` | HTTP 200, but the body is not UTF-8 TOML (strict TOML 1.0 parser, `lib_valdomain.parse_toml`) |
| `http_error` | the server answered, but not with HTTP 200, or the body exceeded the size cap |
| `unreachable` | no HTTP answer: DNS, connect, TLS or timeout failure, a non-https or more than 3 redirects, or a name that resolves to a non-public address (never contacted) |
| `no_domain` | the manifest has no valid `sfDomain`, so there is nothing to fetch (0 in the published run) |

## Network footprint (bounded)

- Your own node: `validators`, `validator_list_sites` and `server_info` (admin, read-only, run
  inside the container's network namespace by `run-node.sh`; nothing is executed in the container),
  plus one `manifest` call per validator.
- The VL site: one GET.
- `rpc.testnet.postfiat.org`: one `manifest` call per validator. Failed calls are retried at most 3
  times, and every retry is printed.
- Each validator domain: exactly **one GET per path**, with no retries. It is https only, uses a
  15 s timeout, a 64 KiB body cap and at most 3 https redirects, and every redirect target must also
  resolve to public addresses. The collector checks that the name resolves only to global unicast
  addresses before it connects.
- Everything is resumable: a re-run fetches only what the data directory does not hold yet.

## Privacy and third parties

Published cache records hold only what the classification needs: HTTP status, content type, size,
sha256, redirect hops, and the body of HTTP 200 answers (public TOML files). Low-level failure
reasons, such as DNS or TLS error text, can describe a weakness of someone else's host. They go
only to the `--private` file, which is kept out of the repository and reported privately. No IP
addresses and no response headers other than the content type are stored.

## Files

| file | what |
|---|---|
| `lib_valdomain.py` | attestation message + check, strict TOML parser, per-fetch classification, class ranking |
| `collect_valdomain.py` | `node` (admin read, run via `run-node.sh`) and `collect` (VL, manifests, TOML cache, `SHA256SUMS`) |
| `run-node.sh` | runs `node` inside the node container's network namespace (nsenter, net namespace only, unprivileged user) |
| `run_collect.sh` | one recorded collection: node read, collect, offline verify, controls, table |
| `verify_valdomain.py` | `--offline DATA [--out DIR]`: all checks + classes -> `results.json`, `validators.csv`, `table.md`; `controls --offline DATA`: the 7 negative controls |
| `make_fixture.py` | builds the signed synthetic fixture `tests/fixtures/valdomain/` (dev-only: needs the `cryptography` package; the tests do not) |
| `mutants.py` | mutation check: the tests must fail against each listed one-spot mutant |
| `data/run-01/` | the published collection; `report/` holds its results; `logs/` holds the run logs |
| `REPORT.md` | results of `data/run-01` |

Exit codes of `verify_valdomain.py`: `0` all checks ran and passed, whatever the classes are; `1`
an internal inconsistency (a failed check); `2` no data (`node.json` / `vl.json` missing).
`--offline` is required. It replaces `socket` so that any network use raises.

## Run it

```bash
# collection (validator host; sudo only for nsenter into the node's network namespace)
bash valdomain/run_collect.sh valdomain/data/run-02 ../private/valdomain-reasons.jsonl

# offline re-check of the published collection (any machine, no network, stdlib only)
python3 valdomain/verify_valdomain.py --offline valdomain/data/run-01 --out /tmp/vd
cmp /tmp/vd/results.json valdomain/report/results.json && cmp /tmp/vd/table.md valdomain/report/table.md
python3 valdomain/verify_valdomain.py controls --offline valdomain/data/run-01

# tests + lint + mutants
python3 -m pytest -q tests/test_valdomain.py
ruff check valdomain tests/test_valdomain.py && ruff format --check valdomain tests/test_valdomain.py
python3 valdomain/mutants.py --jobs 3
```

## Negative controls

`controls` copies the data directory, applies one tamper per control and requires the exact
expected failure:

| control | tamper | must give |
|---|---|---|
| `ctl_file_edit` | edit a cached TOML record (body and its own sha256) without updating `SHA256SUMS` | only `sums` fails |
| `ctl_body_hash` | change a cached body but keep its record's sha256 (`SHA256SUMS` re-made) | only `toml` fails |
| `ctl_attestation_flip` | change one hex digit of a verified attestation (record and sums re-made) | no check fails; that validator becomes `bad_attestation` |
| `ctl_vl_blob` | change one byte of the VL blob signature (fetch record and sums re-made) | only `vl` fails |
| `ctl_public_domain` | the public node's answer claims another domain in `details` | only `crosscheck` fails |
| `ctl_missing_fetch` | remove one cached fetch (sums re-made) | only `toml` fails (collection incomplete) |
| `ctl_no_data` | empty directory | exit 2 |

## Limits

- This is a snapshot: one collection (`data/run-01`, 2026-11-27T11:56Z). An attestation proves that
  the master-key holder signed for the domain, and that the domain served that signature over
  verified HTTPS at collection time. It says nothing about who runs the domain or the node.
- The TOML parser is a strict TOML 1.0 parser (stdlib only, because Python 3.10 has no `tomllib`).
  Dates are returned as source text. The tests compare it with `tomli` on valid and invalid
  documents when `tomli` is installed. It is stricter than TOML 1.1, for example on trailing commas
  in inline tables.
- `public_key` is matched as the exact base58 string, which is case-sensitive like the key encoding.
