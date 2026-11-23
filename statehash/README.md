# statehash: state-tree rebuild and per-ledger replay verification

Read-only, stdlib-only proof that the Post Fiat testnet's **state tree** is exactly what the ledger
headers commit to. `hashverify/` rebuilds every ledger hash and transaction tree, but the header's
`account_hash` (the root of the state SHAMap) is hashed into the ledger hash and never rebuilt there.
`statehash/` closes that gap:

1. rebuilds the state SHAMap of two full binary snapshots A and B (~24 h apart) from their object blobs
   and requires each root to equal the same-index header `account_hash` on our own node **and** on the
   public RPC `https://rpc.testnet.postfiat.org/`;
2. replays every validated ledger A+1 .. B in binary (transaction metadata plus the derived skip-list and
   NegativeUNL writes, the rules of `statereplay/`) onto tree A and checks the rebuilt root against
   **every** ledger's header `account_hash`, then requires the result to equal B byte-for-byte;
3. reports the tree's structure (per-type leaves and bytes, inner nodes and fan-out, leaf depth, per-account
   Merkle-proof size), re-rendered byte-identically `--offline` from the committed data.

Status: work in progress (the A -> B capture window runs until ~2026-11-24T06:00Z).

## Files

| file | what it does |
|---|---|
| `lib_statehash.py` | SHAMap: from-scratch root (`root_of`) and incremental map (`SHAMap`: set, delete with collapse, dirty-path re-hash, walk); field-level codec for binary ledger objects and metadata |
| `definitions.json` | the node's own `server_definitions` answer (field/type codes; hash `6141F0BE...170A`, equal on our node and the public RPC). No field code is typed by hand |
| `collect_statehash.py` | read-only collector: `snapshot` (binary `ledger_data` pinned by hash + own and public header), `capture` (every validated ledger: binary header + tx blobs + metadata; tx tree must rebuild to the header's `transaction_hash` before a line is written) |
| `run_window.sh` | the recorded A -> B window: snapshot A, capture to B, snapshot B |
| `replay_statehash.py` | rebuild A, replay A+1 .. B with a root check after every ledger, compare with B |
| `report_statehash.py` | structure reports (`structure.txt/.json`, `structure-types.csv`, `structure-fanout.csv`) |
| `mutants_statehash.py` | mutation harness over all four modules |
| `data/` | published inputs: `A/`, `B/` (`header.json`, `public-header.json`, `MANIFEST.json`, `state.jsonl.gz`), capture |
| `logs/` | recorded runs |

The snapshots are published with their **raw** blobs: every hash in this folder is computed over those exact
bytes, so they cannot be pseudonymized (unlike `statereplay/data/`). They are public ledger data that anyone
can fetch from the public RPC.

## Hashing

```
state leaf   SHA-512Half("MLN\0" || object_blob || index)          index = 32-byte object key
inner node   SHA-512Half("MIN\0" || child_0 || ... || child_15)    empty child = 32 zero bytes
```

A leaf hangs in the inner node at the depth where its key's nibble path stops being shared with any other
key (nibble d = high half of key byte d/2 for even d, low half for odd d). The root is always an inner node;
an empty map hashes to 32 zeros. Deleting a leaf collapses every non-root inner node left with exactly one
leaf and no inner child (the leaf moves up). `root_of` and `SHAMap` are separate implementations; tests
require them to agree on hand-written trees and on 600 random inserts/deletes.

## Replay rules

Same rules as `statereplay/README.md`, applied to binary fields (field values move between metadata and
objects as raw bytes; objects are re-serialized in canonical field order): flag-ledger NegativeUNL changes
first, then transactions in `TransactionIndex` order with every `CreatedNode`/`ModifiedNode`/`DeletedNode`
checked against the replayed object before it is applied, then the `LedgerHashes` skip lists
(`Ledger::updateSkipList`). After each ledger the rebuilt root must equal that ledger's header
`account_hash`; a mismatch stops with `ROOT_MISMATCH`, the ledger and the object indexes written in it.

Failure reasons: `ROOT_A`, `ROOT_B`, `ROOT_MISMATCH`, `LEDGER_MISSING`, `LEDGER_DUPLICATE`, `HEADER_HASH`,
`CHAIN_BREAK`, `ANCHOR_A_HASH`, `ANCHOR_B_HASH`, `TX_INDEX_GAP`, `NODE_KIND`, `CREATE_EXISTS`,
`NODE_MISSING`, `TYPE_MISMATCH`, `PREVIOUS_MISMATCH`, `UNCHANGED_MISMATCH`, `THREAD_MISMATCH`,
`MISSING_IN_REPLAY`, `EXTRA_IN_REPLAY`, `BLOB_MISMATCH`.

## Tests

`tests/test_statehash_*.py`:

- `lib`: hand-computed roots (empty, one leaf, two leaves under a 1-nibble prefix, two leaves under a
  63-nibble prefix, delete-collapse of a lone leaf and of a 63-deep chain, an inner kept when it still has
  two leaves below), random insert/delete vs scratch build, codec round trips;
- `replay`: a real 158-ledger window (published A 8290390 -> recorded fixture snapshot 8290548, stored as a
  diff against A, plus the binary capture between them): every per-ledger root matches; named-reason
  controls for a flipped blob byte (A and B), a missing object, an extra object, wrong header hashes,
  skipping one ledger's metadata, a dropped ledger, and a skip list that is not derived;
- `meta`: each node rule on synthetic binary metadata;
- `report`: hand-computed structure of a synthetic tree, the root gate, byte-identical offline render;
- `collect`: the collector against a fake binary node.

## Usage

```
./collect_statehash.py snapshot --out data/statehash/A --ledger N --log FILE
./collect_statehash.py capture --out data/statehash/cap --start N --to M --log FILE
./replay_statehash.py --a DIR --b DIR --cap DIR [--json out.json]
./report_statehash.py --offline statehash/data --out DIR
python3 statehash/mutants_statehash.py          # from the repo root
```
