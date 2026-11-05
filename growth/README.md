# growth: ledger growth and node storage sizing

Measures how fast a Post Fiat testnet node's data grows over a 24 h validated-ledger window
(transactions, metadata, ledger headers, state objects) and turns it into disk projections for
`online_delete` 256, 512, 2000, 8192, 32768 and full history. The 512 projection is not
assumed: it is fitted to the same node's NuDB/SQLite sizes, sampled every 10 min by
[pft-retention](https://github.com/darkmatter-sudo/postfiat-validator-installer/tree/main/retention) during the window.
Read-only: `server_info`, `ledger` and `ledger_data` only; results in [REPORT.md](REPORT.md).

## Files

| file | what it does |
|---|---|
| `collect_growth.py` | resumable per-ledger collector: binary + JSON `ledger` per ledger, joined by recomputed hashes (`LWR\0` header, `TXN\0` per tx), exact header/tx_blob/meta bytes, every AffectedNodes entry; public-RPC fallback with exponential backoff, gaps/retries/restarts files, torn-line and orphan-row repair on resume, clean SIGTERM stop |
| `resume_drill_growth.sh` | SIGTERM + SIGKILL resume drill with injected torn/orphan rows, byte-compared with an uninterrupted run |
| `snapshot_end.sh` | window-end state snapshot B (from a timer, after the collector exits) via `state/snapshot.py` |
| `statebytes_growth.py`, `statebytes_end.sh` | exact serialized size of every state object (binary `ledger_data`), joined with the JSON snapshot of the same ledger (hash, index set, decoded type) |
| `analyze_growth.py` | coverage, hourly/per-day growth, top contributors, snapshot A + created − deleted = B per type, NuDB/SQLite size-vs-retained fit and overhead ratio, sizing table; `--check` recomputes offline and requires a byte-identical result |
| `crosscheck_growth.py` | recounts sampled ledgers through the public RPC with the collector's own join (half from ledgers with transactions), RANGE/prune guard, RETRY/REFETCH lines, `--record` / `--offline` / `--negative` |
| `charts_growth.py` | `growth_hourly.png`, `growth_nudb_fit.png`, `growth_sizing.png` |
| `mutants_growth.py` | 41 controlled mutants; every one must make a test fail |
| `data/` | the published window: inputs, results, `SHA256SUMS`, `ROWS.txt` |
| `logs/` | redacted execution logs 01-12 |

## Run it

```bash
# 1. start the 24 h window (own node, <= 2 local req/s) and snapshot the ledger before it
python3 growth/collect_growth.py --out data/growth/tx --log data/growth/collect.log --hours 24
python3 state/snapshot.py --out data/growth/snapA --ledger <first-1>
# 2. after the window: snapshot B + exact state bytes of the last ledger (timers in logs 03/07)
growth/snapshot_end.sh data/growth pft-growth-tx && growth/statebytes_end.sh data/growth
# 3. recount 20 ledgers through the public RPC while they are still in its history
.venv/bin/python growth/crosscheck_growth.py --data data/growth/tx --count 20 --seed s --record xc.json
# 4. analyze, then recompute offline from the published files only
python3 growth/analyze_growth.py --data D/tx --snap-a A.jsonl.gz --snap-b B.jsonl.gz \
  --statebytes mid.json --statebytes end.json --nudb nudb-samples.csv --online-delete 512 --out D/final
python3 growth/analyze_growth.py ...same arguments... --check D/final/growth.json
# 5. tests and mutants
.venv/bin/python -m pytest -q tests/test_growth_*.py && .venv/bin/python growth/mutants_growth.py
```

Reproduce the published result offline:
`cd growth/data && sha256sum -c SHA256SUMS`, then step 4 with the paths in
[logs/10-analyze-final.log](logs/10-analyze-final.log).

## Method

- **Raw bytes per ledger** = header (118 B) + tx_blob + metadata bytes, taken from the binary
  answer (hex length / 2), so they are the serialized sizes, not JSON sizes.
- **State bytes** = sum of the serialized size of every object in `ledger_data` at a ledger. Its
  growth rate comes from two exact points (mid-window and window end).
- **Retained ledgers** = `complete_ledgers` span per pft-retention sample. With online_delete N the
  node keeps between N and 2N ledgers (it deletes in rotations); the measured mean ratio
  (1.504 at N = 512) is applied to every N.
- **NuDB fit**: least squares `nudb_bytes = fixed + per_ledger × retained` over the in-window
  samples with `server_state = full`. Not used if r² < 0.5. The same fit for SQLite is reported;
  when it is unusable, SQLite is sized as its measured mean (it did not track retained ledgers).
- **Overhead ratio** = mean measured NuDB bytes / (state bytes + mean retained × raw bytes per
  ledger). NuDB stores SHAMap nodes (inner nodes along every changed path, plus the tx tree), so it
  costs more than the raw content; the ratio measures how much more.
- **Full history**: growth per day at the fitted per-ledger disk cost, and a *naive* size of all
  ledgers since genesis at this window's cost. Both are labelled as extrapolations.

## Caveats

- One node, one 24 h window, one online_delete setting (512). The other settings are
  projections from that fit (linear in retained ledgers), not measurements.
- The per-ledger NuDB cost was measured on a rotating online_delete node; a full-history node
  never deletes and may write more or less per ledger. The full-history numbers are an estimate.
- Testnet load: ~0.35 tx per ledger (Payment, NFTokenMint, NFTokenBurn only). Mainnet-like load
  would change raw bytes per ledger; re-run the window rather than scaling these numbers.
- Public-RPC history is finite: run the crosscheck during or right after the window.
