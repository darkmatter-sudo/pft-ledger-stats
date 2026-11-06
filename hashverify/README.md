# hashverify: ledger hash-chain and transaction-tree verifier

Every other analyzer in this repo (collect.py, txmix, txflow, senders, growth, ...) takes the
ledger and transaction hashes a server reports at face value. `hashverify/` checks them. It
recomputes Post Fiat testnet ledger hashes, parent links, transaction IDs and transaction-tree
(SHAMap) roots from the serialized bytes, then compares them with what two independent sources
report: your own node and the public RPC (`https://rpc.testnet.postfiat.org`).
It is read-only (`server_info` and `ledger` only) and uses only the Python 3 stdlib. Results are
in [REPORT.md](REPORT.md) and [counts.csv](counts.csv).

## Files

| file | what it does |
|---|---|
| `lib_hashverify.py` | serialization and hashing: 118-byte header, SHA-512Half, VL prefix, tx ID, tx leaf, SHAMap root |
| `verify_headers.py` | header hash chain over the whole `complete_ledgers` overlap of both sources: HASH_MISMATCH / CHAIN_BREAK / DISAGREE / GAP, `--record`, `--offline`, `--tamper` |
| `verify_txtree.py` | tx IDs + tx-tree root of sampled ledgers: every ledger of a 1 h slice + a seeded random sample (`--count`), or `--range A-B`; `--record`, `--offline`, `--tamper` |
| `report_hashverify.py` | builds `REPORT.md` and `counts.csv` from the newest run of each stage (no typed numbers) |
| `run_hashverify.sh` | the pipeline stages used for the published runs (`headers`, `public`, `local`, `tamper`, `report`) |
| `mutants_hashverify.py` | 53 controlled mutants over lib, both verifiers and the report; each one must make a test fail |
| `data/<stage>-N/` | per-run outputs: per-ledger CSV, `summary.csv`, the gzipped raw record for offline re-runs |
| `logs/` | execution logs, numbered |
| `../tests/test_hashverify_*.py`, `../tests/fixtures/hashverify/` | 52 tests; offline vectors are real ledgers (7 tx-tree ledgers with 0, 1 and 5 transactions, one with a depth-2 inner node; a 12-ledger header chain from both sources) |

## Run it

```bash
# from the repo root; LOCAL/PUBLIC override the two endpoints
bash hashverify/run_hashverify.sh headers 3     # header chain, own node + public RPC, whole overlap
bash hashverify/run_hashverify.sh public 4      # tx trees on the public RPC: 1 h slice + sample = 2000
bash hashverify/run_hashverify.sh local 5       # tx trees on the own node, every retained ledger
bash hashverify/run_hashverify.sh tamper 7 3 4  # real-data negative controls from the records of runs 3 and 4
bash hashverify/run_hashverify.sh report        # REPORT.md + counts.csv

# offline re-run of a published record (no RPC at all)
python3 hashverify/verify_headers.py --offline hashverify/data/headers-3/headers.jsonl.gz --out /tmp/h
python3 hashverify/verify_txtree.py --offline hashverify/data/public-4/txtree.jsonl.gz --out /tmp/t

# tests and mutants
.venv/bin/python -m pytest -q tests/test_hashverify_*.py && .venv/bin/python hashverify/mutants_hashverify.py
```

Exit codes: 0 = every check passed with full coverage, 1 = any finding (mismatch, break,
disagreement, gap), 2 = RPC failure (`RPC_FAILURE` line). Your own node is reached on its
non-admin JSON-RPC (default `http://127.0.0.1:5005`, no gap between requests). The public RPC gets
`--gap 0.5` s between requests. Transport errors and `slowDown`/`tooBusy`/`lgrNotFound`/`internal`
answers are retried with exponential backoff (2 s, doubling, 7 attempts), with one `RETRY` line
each, and are counted in `summary.csv` (`*_rpc_retries`). A retry is never counted as a mismatch.

## What is recomputed

postfiatd 1.0.9 uses the rippled ledger format. The hash prefixes are the `HashPrefix` values in
rippled's `include/xrpl/protocol/HashPrefix.h`: three ASCII bytes followed by `0x00`. The tests pin
them as hex (`test_prefixes_are_the_rippled_values`), and every real vector reproduces the
server's hashes with them.

| value | computed as |
|---|---|
| ledger hash | SHA-512Half(`LWR\0` ‖ header) |
| header (118 B, big-endian) | `ledger_index` u32 ‖ `total_coins` u64 ‖ `parent_hash` 32 ‖ `transaction_hash` 32 ‖ `account_hash` 32 ‖ `parent_close_time` u32 ‖ `close_time` u32 ‖ `close_time_resolution` u8 ‖ `close_flags` u8 |
| tx ID | SHA-512Half(`TXN\0` ‖ tx_blob) |
| tx leaf | SHA-512Half(`SND\0` ‖ VL(tx_blob) ‖ tx_blob ‖ VL(meta) ‖ meta ‖ tx ID) |
| inner node | SHA-512Half(`MIN\0` ‖ child₀ ‖ … ‖ child₁₅), empty child = 32 zero bytes |
| tx root | radix-16 tree on the tx ID nibbles (high nibble first). The root is always an inner node. A leaf sits at the shallowest depth ≥ 1 where its nibble path is unique. A ledger with no transactions has root = 32 zero bytes |
| VL prefix | 1 byte for n ≤ 192, 2 bytes `193 + (n−193)>>8, (n−193)&0xFF` for n ≤ 12480, 3 bytes `241 + (n−12481)>>16, …` for n ≤ 918744 |

SHA-512Half means the first 32 bytes of SHA-512.

Checks per ledger:

* **verify_headers.py.** For each source: the recomputed hash must equal the reported
  `ledger_hash` (HASH_MISMATCH). `parent_hash` of N must equal the **recomputed** hash of N−1
  (CHAIN_BREAK). Across sources, the recomputed hash of N must be the same on both (DISAGREE). A
  ledger in the overlap that a source can't serve is a GAP. Each finding line prints the ledger
  index and both hashes.
* **verify_txtree.py.** The header serialized from the JSON fields must equal the binary
  `ledger_data`. The LWR hash must equal `ledger_hash` in both the binary and the JSON answer. The
  recomputed tx ID multiset must equal the reported tx list. The rebuilt root must equal the
  header's `transaction_hash`. `MISMATCH ledger=N check=… reported=… computed=…` names each failed
  check.

Negative controls (`--tamper`, offline only, never live data) change one field in a **real
recorded** ledger. `total_coins +1` must give HASH_MISMATCH and DISAGREE at N plus CHAIN_BREAK at
N+1. A flipped `parent_hash` nibble must give CHAIN_BREAK at N. A flipped byte in a real
`tx_blob` must give `txids` + `root` MISMATCH at N. Each control must exit 1.

## Trust limits

* **Signatures are not checked.** No validations are checked, so nothing here proves a hash was
  signed by a UNL quorum. A source that makes up an entire self-consistent chain passes every
  in-source check. It is caught only by DISAGREE against the other source, which needs the two
  sources to be independent: here, my own node (peered over the overlay) and the foundation's
  public RPC.
* **`account_hash` (the state tree) is hashed into the header but not rebuilt.** That would need
  every state object of every ledger (full `ledger_data` paging per ledger), so a wrong state root inside a correctly-chained header is out of scope.
* **Tx trees use one source per ledger.** The public sample is checked on the public RPC only, and
  the own-node range on the own node only. The tx root is still covered across sources, because
  `transaction_hash` is part of the header and the header hash is compared across sources over the
  overlap.
* **Metadata is hashed as bytes, not interpreted.** A different but well-formed meta gives a
  different root, which is what the check needs. Its contents are not validated.
* **Coverage is limited to what the sources retain.** Header-chain coverage is the overlap of the
  two `complete_ledgers` ranges. The own node keeps about 1,000 ledgers (`online_delete=512`), so
  the overlap is about one hour of history. The tx-tree sample covers the public RPC's retained
  range. Older history can't be checked from these sources.

## Results (2026-11-06, pft-agent-01)

All numbers are read from [counts.csv](counts.csv) / the logs; details in [REPORT.md](REPORT.md).

| run | log | result |
|---|---|---|
| header chain, both sources, whole overlap 7806086-7807080 | [03](logs/03-headers.log) | 995 ledgers; own node 995/995 hashes OK, 994 parent links, 0 breaks, 0 gaps; public RPC 995/995 OK, 994 links, 0 breaks, 0 gaps; sources agree on 995, disagree on 0; 1 own-node lgrNotFound retry, 0 public retries; 1376.6 s |
| tx trees, public RPC: 1 h slice + seeded sample | [04](logs/04-public.log) | 2001 ledgers (1193 slice, 222 of them non-empty, + 807 sampled, ledgers 7750154-7807516); 384 non-empty, 700 transactions; 0 mismatched ledgers; 0 retries; 5415.8 s |
| tx trees, own node: every retained ledger | [05](logs/05-local.log) | 987 ledgers (7806092-7807078), 184 non-empty, 316 transactions; 0 mismatched; 1 lgrNotFound retry |
| real-data negative controls | [07](logs/07-tamper.log) | every tampered control exits 1 with the expected findings (ledger index + both hashes); both untampered offline re-runs exit 0 |
| tests, ruff, shellcheck | [01](logs/01-checks.log) | `533 passed in 28.41s` (whole repo), ruff + ruff format + shellcheck clean |
| mutants | [02](logs/02-mutants.log) | 53 mutants, caught=53, 0 survived |
| report | [08](logs/08-report.log) | REPORT.md + counts.csv |

Logs 01-05 ran at f99ae9743629dee83435e0f6c294319028a75c03; 07-08 at e1d682b292d6771f791ca5e2481d9c438aa40154,
which changes only `run_hashverify.sh` (the tamper stage's choice of control ledger). Tamper run
[06](logs/06-tamper-superseded.log) is superseded: `headers.csv` lists the own-node rows, then the public rows, so
`r[len(r)//2]` picked the first ledger of the range (7806086), whose own parent link is never checked. Its controls
were still all detected; run 07 uses the middle own-node ledger (7806583) so both links of the tampered ledger are
exercised. Its output directory is not published.
