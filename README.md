# pft-ledger-stats

Historical, network-level health analysis of the Post Fiat (PFTL) testnet from your own node:
ledger close intervals, stalls, transactions per ledger/hour, fee burn (`total_coins`), and
proposers vs validation quorum over time.

This is different from a live Prometheus exporter: it records every validated ledger in a
window to plain CSV/JSONL, so the report can be rebuilt and audited later from committed data.

| file | what it does |
|---|---|
| `collect.py` | resumable, read-only collector (stdlib only) |
| `analyze.py` | builds `REPORT.md` + PNG charts in `charts/` from `data/` |
| `crosscheck.py` | compares random collected ledgers against a second node |
| `pftstats/stats.py` | pure statistics functions (unit-tested) |
| `tests/` | `pytest` / `unittest` tests |
| `data/` | raw data of the published run |
| `logs/` | raw logs of the published run |
| `txmix/` | transaction-type mix analyzer, see [`txmix/README.md`](txmix/README.md) |
| `state/` | ledger-state snapshot analyzer (balances, reserves, objects), see [`state/README.md`](state/README.md) |
| `validators/` | per-validator participation analyzer, see [`validators/README.md`](validators/README.md) |
| `rewardflow/` | reward-payer PFT outflow analyzer over the public RPC's history, see [`rewardflow/README.md`](rewardflow/README.md) |
| `nunl/` | NegativeUNL history (UNLModify + NegativeUNL object at every flag ledger) and disable/re-enable risk per 256-ledger window, see [`nunl/README.md`](nunl/README.md) |
| `acctchurn/` | account churn between two state snapshots ~24 h apart: per-type diff, exact native-PFT conservation vs `total_coins` and the fee burn, funding graph of new accounts, see [`acctchurn/README.md`](acctchurn/README.md) |
| `nftflow/` | NFT lifecycle between two state snapshots ~24 h apart: exact supply, owner and issuer-counter reconciliation from a gap-free NFToken* collection, lifecycle stats, public-RPC crosscheck, see [`nftflow/README.md`](nftflow/README.md) |
| `keyposture/` | signing-key posture (master / regular key / multisig / black-holed) of every account at two state snapshots ~24 h apart: exact per-account and per-class reconciliation from a gap-free key-tx collection, weak-config flags, public-RPC crosscheck, see [`keyposture/README.md`](keyposture/README.md) |
| `payflow/` | who pays whom over a 24 h window: directed payment graph, volume concentration, net flows and FIFO onward flows of the reward payer's recipients; snapshot-to-snapshot conservation exact to the drop, public-RPC crosscheck with offline re-check, see [`payflow/README.md`](payflow/README.md) |

## Requirements

- A synced Post Fiat node in Docker (e.g. the official `postfiatd` compose setup), admin RPC on
  port 5005 inside the container. The collector never needs the port exposed: it runs
  `docker exec <container> curl http://127.0.0.1:5005/`.
- Python 3.10+. `collect.py` and `crosscheck.py` use only the standard library.
  `analyze.py` needs `matplotlib`.

## Run it on any node

```bash
git clone https://github.com/darkmatter-sudo/pft-ledger-stats && cd pft-ledger-stats

# 24h collection (default), <= 1 request/second, sudo docker exec into container "postfiatd"
python3 collect.py --out data --log logs/collect.log
#   --container NAME     container name (default postfiatd)
#   --no-sudo            if your user is in the docker group
#   --duration SECONDS   window length, measured from the FIRST start (default 86400)
#   --sample-interval S  validator/quorum sample period (default 300)
#   --start SEQ          first ledger (default: current validated ledger)

# optional: compare 5 random ledgers with a second node you control (reached via ssh alias)
python3 crosscheck.py --ssh my-sentry --remote-container postfiatd-sentry --out data/crosscheck.csv

# report
python3 -m venv .venv && .venv/bin/pip install matplotlib pytest ruff
.venv/bin/python analyze.py --data data --out REPORT.md --charts charts --crosscheck data/crosscheck.csv

# tests + lint
.venv/bin/python -m pytest -q tests && .venv/bin/ruff check .
```

For a 24h unattended run, wrap `collect.py` in a systemd service with `Restart=on-failure`;
it resumes from the last row of `data/ledgers.csv`.

## Method

**Collector.** Only read-only RPC methods are allowed (`server_info`, `ledger`, `validators`;
anything else raises). A rate limiter enforces at least 1 s between requests (`--rate` above 1 is
refused). On start it records `state.json` (start time, start ledger). It then walks validated
ledgers one by one with `ledger {ledger_index, transactions: true, expand: false}` and appends to
`ledgers.csv`:

`ledger_index, ledger_hash, close_time, close_time_iso, close_time_resolution, parent_close_time,
close_flags, txn_count, total_coins`

When it catches up with the validated ledger it polls `server_info` once per second. Every
`--sample-interval` seconds it appends one JSON line to `samples.jsonl`:

`sampled_at, validated_seq, server_state, proposers (last_close.proposers), converge_time_s,
validation_quorum, trusted_validators (count only), peers`

A ledger that still fails after 3 attempts is written to `skips.csv` and logged as `SKIP`, never
silently dropped. On exit (SIGTERM or end of window) it logs a `RANGE first-last ledgers=N
consecutive=yes|no skipped=K skip_list=[...]` line.

**Resume.** After an interruption the collector restarts at `last ledger_index + 1` and walks
forward until it catches up, so no ledger is missed as long as the node still holds it.

**Analyzer.**

- *Close intervals*: `close_time[n] − close_time[n−1]` for consecutive ledgers;
  min / median / p95 / p99 / max (linear-interpolated percentiles) and mean.
- *Stalls*: the literal rule "interval > 3 × median", plus a resolution-aware rule
  "interval > 3 × max(median, close_time_resolution)" (see Limits). Both lists go to `stalls.csv`.
- *Transactions*: per-ledger distribution, per-hour totals (UTC; first/last hour are partial and
  excluded from the per-hour statistics but shown in the table).
- *Fee burn*: transaction fees are destroyed, so `total_coins[first] − total_coins[last]` is the
  PFT burned in the window; also attributed per hour.
- *Proposers vs quorum*: `last_close.proposers` vs `validation_quorum` per sample, and the margin.

**Cross-check.** `crosscheck.py` takes the ledgers that both nodes still hold, picks `--count`
of them with a logged random seed, and requires `ledger_hash` and `close_time` from the CSV, the
local node and the second node to be identical.

## Limits

- **Single-node view.** Everything is what one node saw. `last_close.proposers` is the number of
  proposers in *this node's* last consensus round; a peering problem on your node looks like a
  network problem. The cross-check only proves the recorded ledgers are canonical, not that your
  node's view of proposers is complete.
- **close_time rounding.** `close_time` is rounded to `close_time_resolution` (10 s on testnet
  during the published run). When several ledgers close inside one resolution bucket, each gets
  the previous close time + 1 s, so raw deltas look like 1, 1, 8, 1, 1, 8 … . Individual deltas,
  the median and the literal 3 × median stall rule are therefore dominated by rounding; the mean
  over a long window and per-hour ledger counts are reliable. The resolution-aware rule only
  flags gaps of more than 3 resolution buckets, so it can miss shorter stalls.
- **Short history on light nodes.** A light node keeps only a short ledger history (roughly 500–1,000
  ledgers, well under an hour, on the published node). If the collector is down longer than that, the missing ledgers can't be
  fetched and will appear in `skips.csv`. The cross-check can only sample ledgers both nodes still
  hold, i.e. recent ones.
- **Samples are 5-minute snapshots**, not continuous; short dips in proposers between samples are
  not seen.
- `txn_count` counts all transactions included in the ledger, successful or not.

## Reproduce the report from committed data

```bash
.venv/bin/python analyze.py --data data --out REPORT.md --charts charts --crosscheck data/crosscheck.csv
git diff --stat REPORT.md charts/   # REPORT.md is byte-identical; PNGs may differ in metadata only
```

## Published run (2026-10-21T09:11Z – 2026-10-22T09:11Z)

| file | content |
|---|---|
| `logs/collect.log` | collector log; OS line at top, final `RANGE 7352333-7380888 ledgers=28556 consecutive=yes skipped=0` |
| `logs/crosscheck.log` | 5 random ledgers vs the sentry node: 5/5 hash and close_time matches |
| `logs/analyze-tests.log` | `analyze.py`, `pytest -v` (13 passed) and `ruff check` (clean) |
| `data/` | `ledgers.csv`, `samples.jsonl`, `state.json`, `crosscheck.csv` |
| `REPORT.md`, `charts/`, `stalls.csv` | analyzer output |

The collector ran as a systemd service. The host rebooted at 20:20Z; the log shows the
`STOP (signal)` / interim `RANGE` lines and the `RESUME from ledger 7365587` line, and no ledger was
skipped. The only edit to any published log: home-directory paths in `logs/analyze-tests.log`
(pytest prints `rootdir`) were replaced with `~/` using `sed 's#/home/[a-z0-9_-]*/#~/#g'`.

## Privacy

Published data holds only public chain data (ledger hashes, close times, coin totals, transaction
counts) and counts of validators/peers. The collector does not record node public keys, peer
addresses or host names, and the cross-check output labels the nodes only "local" and "second".

## License

MIT
