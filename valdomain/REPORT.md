# valdomain report: domain attestations of the Post Fiat testnet VL (run-01)

Collection `data/run-01`: 2026-11-27T11:56:51Z to 11:57:33Z, from the validator host (postfiatd 1.0.9,
`server_state` full), with the run log in `logs/01-collect.log`. Every number below comes from
`report/results.json`. Re-derive it offline with:

```bash
python3 valdomain/verify_valdomain.py --offline valdomain/data/run-01 --out /tmp/vd
cmp /tmp/vd/results.json valdomain/report/results.json && cmp /tmp/vd/table.md valdomain/report/table.md
jq '.counts' valdomain/report/results.json
```

## Result

**All 25 validators of VL sequence 31 are `verified`.** Every domain publishes a valid master-key
attestation at `/.well-known/pft-ledger.toml`, the path in the Post Fiat guide. Every check passed:
`sums`, `vl`, `manifests`, `toml` and `crosscheck`, with exit 0.

| check | result |
|---|---|
| `sums` | 54 files listed in `SHA256SUMS`, every sha256 matches |
| `vl` | VL sequence 31, 25 validators, publisher `ED3F1E0DA736FCF99BE2880A60DBD470715C0E04DD793FB862236B070571FC09E2`. Publisher manifest and blob signature (low-S) verify. The node trusts this publisher, sequence 31 and exactly these 25 masters. Fetched from the node's `validator_list_sites` uri `https://postfiat.org/testnet_vl.json` (HTTP 200, 14779 B, sha256 `5c6e03b7b054cc6921a650736cc950119d66c66c63d479b11fbc507c277ad688`), valid 2026-11-24T20:11:38Z to 2028-04-07T16:15:57Z |
| `manifests` | 25 of 25 VL manifests verify (master + signing signature). 25 of 25 carry `sfDomain`; the 25 domains are distinct |
| `toml` | 25 domains x 2 paths = 50 cached fetches, each body matches its sha256 |
| `crosscheck` | 50 of 50 `manifest` answers (25 from the own node, 25 from `rpc.testnet.postfiat.org`) verify for the same master at the VL's sequence and carry the VL's domain; 0 retries were needed |

Per-class counts (`jq '.counts' report/results.json`):

| class | validators |
|---|---|
| verified | 25 |
| bad_attestation | 0 |
| no_attestation | 0 |
| key_not_listed | 0 |
| toml_unparseable | 0 |
| http_error | 0 |
| unreachable | 0 |
| no_domain | 0 |
| total | 25 |

Per path:

| path | verified | http_error | other classes |
|---|---|---|---|
| `/.well-known/pft-ledger.toml` (Post Fiat guide) | 25 | 0 | 0 |
| `/.well-known/xrp-ledger.toml` (XRPL standard) | 6 | 19 | 0 |

The 19 `http_error` answers on the XRPL path break down as 17 HTTP 404, 1 HTTP 502, and 1 HTTP 200
whose body (an HTML page) exceeded the 64 KiB cap. Because every validator is verified on the Post
Fiat path, none of these changes a class. Re-derive the breakdown with
`for f in valdomain/data/run-01/toml/*/xrp-ledger.json; do jq -r '[.status,.truncated]|@tsv' $f; done | sort | uniq -c`.

## Independent VL-domain crosscheck

The domain in the VL is checked against two sources that do not depend on the VL file:

1. **The domain itself** binds back to the key: 25 of 25 attestations verify, so each domain
   publishes a signature that only the master key named in the VL could make.
2. **Two nodes' live manifest stores** (queried over RPC, not read from the VL file): the own node
   and Post Fiat's public node `rpc.testnet.postfiat.org` each returned the manifest for every master. 50 of 50 verify, have
   the VL's sequence and carry the same domain. Each node's decoded `details.domain` also equals our
   own decode of the manifest blob (`node_manifest` / `public_manifest` = `same` in every row below).

## Validators

Key prefix = the first 10 characters of the base58 master key. The per-path columns are each path's
own class.

| key prefix | domain | class | /.well-known/pft-ledger.toml | /.well-known/xrp-ledger.toml | node manifest | public manifest |
|---|---|---|---|---|---|---|
| `nHBAToXoTH` | app.w.ai | verified | verified | http_error | same | same |
| `nHUWciHX8W` | auri0x.io | verified | verified | http_error | same | same |
| `nHBmj7zUY2` | dex3333.github.io | verified | verified | http_error | same | same |
| `nHUvKQbxx1` | hamedminasli.duckdns.org | verified | verified | http_error | same | same |
| `nHBb2uHHY8` | iopo545454-del.github.io | verified | verified | http_error | same | same |
| `nHBM2nzq3p` | jollydinger.com | verified | verified | verified | same | same |
| `nHUCEXpC5L` | lc66validator.postfiatcn.org | verified | verified | http_error | same | same |
| `nHUatddiVB` | local-maxi.github.io | verified | verified | http_error | same | same |
| `nHUwdHGzvT` | parshant.com | verified | verified | http_error | same | same |
| `nHUUXMXfPE` | pft.akirax.xyz | verified | verified | verified | same | same |
| `nHByMXejvH` | pft.bigwoodnode.com | verified | verified | http_error | same | same |
| `nHBcnQZ9a9` | pft.g.money | verified | verified | verified | same | same |
| `nHBYKjxjbz` | pft.hit0ri.xyz | verified | verified | http_error | same | same |
| `nHB6Zc7mhr` | pft.permanentupperclass.com | verified | verified | http_error | same | same |
| `nHUT8QPaTE` | pft.thedarkmatter.xyz | verified | verified | http_error | same | same |
| `nHUdwzTWTQ` | pft.whiteguy.eu | verified | verified | http_error | same | same |
| `nHUzc5Xzsm` | pft.wizbubba.xyz | verified | verified | http_error | same | same |
| `nHBVTAUpHE` | pftmeech.xyz | verified | verified | http_error | same | same |
| `nHDfSHLutR` | postfiat.live | verified | verified | http_error | same | same |
| `nHBxxNuWsJ` | postfiat.maro.dpdns.org | verified | verified | http_error | same | same |
| `nHDw5erSdz` | postfiatd.devflows.dedyn.io | verified | verified | http_error | same | same |
| `nHBWFVzxVY` | qwerky2233.github.io | verified | verified | http_error | same | same |
| `nHBcLEB4S6` | sendoeth.com | verified | verified | verified | same | same |
| `nHD3sPmhNt` | snakespartan.pftoligarchy.com | verified | verified | verified | same | same |
| `nHU74qX4tC` | validator.pftperry.com | verified | verified | verified | same | same |

## Notes

- No third-party host security observations came up in this collection: the private failure-reason
  file has 0 lines, because no fetch was unreachable.
- The TOML file contents are public (they are what the domains publish). They are kept in
  `data/run-01/toml/` so that the offline verifier can re-derive every class.
