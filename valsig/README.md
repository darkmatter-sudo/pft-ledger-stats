# valsig: which UNL validators signed each validated ledger, and did it meet quorum

`hashverify/` proves that ledger headers chain and that two sources agree, but its README lists the
gap: "Signatures are not checked ... nothing here proves a hash was signed by a UNL quorum".
`valsig/` closes that gap for a recorded window. It records every validation the operator's own
node receives, as the raw signed `STValidation` blob, and then checks offline, with pure-stdlib
cryptography:

1. the published signed Validator List (VL) is genuine (publisher manifest, blob signature) and is
   the list the node trusts;
2. every validator manifest verifies, so each ephemeral signing key is tied to a UNL master key;
3. every validation decodes, the node's decoded fields equal the signed bytes, and the signature
   verifies;
4. per validated ledger: how many distinct trusted (and not NegativeUNL-disabled) masters signed a
   full validation of exactly that ledger hash, the quorum postfiatd uses, and the margin;
5. a sample of >= 50 recorded ledger hashes equals the public RPC's validated hashes.

It is read-only. The recorder sends only `subscribe`, `server_info`, `validators`, `manifest` and
`ledger_entry` (an allowlist in the code, tested). Nothing is signed or submitted, and the node is
never restarted or reconfigured. External traffic: one HTTPS GET of the published VL at the start
and end of the window, and the sampled public-RPC `ledger` calls.

## Files

| file | what |
|---|---|
| `collect_valsig.py` | `record`: resumable stream recorder -> `seg/NNNNNN.jsonl.gz` (gzip mtime 0) + `state.json`; `vl`: fetch the signed VL |
| `run-collector.sh` | runs `record` inside the node container's **network namespace** (nsenter, net namespace only, unprivileged user); nothing runs inside the container |
| `run_window.sh` | one window: VL at the start, recorder (re-entered after a container restart, resumes), VL at the end |
| `lib_valsig.py` | `STValidation` decoder, signing data, signature check (low-S when fully canonical), postfiatd quorum formula |
| `verify_valsig.py` | `run`: all checks -> `checks.json`, `ledgers.csv`, `validators.csv`, `REPORT.md`; `controls`: the five negative controls |
| `quorum_ref.cpp` | the C++ quorum expression of postfiatd, compiled to make `tests/fixtures/valsig/quorum-cpp.txt` |
| `make_fixture.py` | cuts the pytest fixture (`tests/fixtures/valsig/rec`) out of a real recording |
| `mutants.py` | mutation check: the tests must fail against each of the listed one-line mutants |
| `data/window-01/` | the recorded window (segments, VL files, `rpc-cache.json`); `report/` its results; `logs/` the run logs |

Signature math comes from `dunl/lib_dunl.py` (Ed25519 for master keys, secp256k1 ECDSA over
SHA-512-Half for ephemeral and VL keys). valsig made its secp256k1 verify about 37x faster
(Jacobian coordinates, one joint double-and-add pass); results are unchanged, and a test compares
it with the original affine code.

## Usage

```sh
# a 4.5 h window (the live run used a transient systemd unit with this command, as root)
sudo RUN_UID=$(id -u) RUN_GID=$(id -g) bash valsig/run_window.sh valsig/data/window-01 4.5
# verify (online: fills data/window-01/rpc-cache.json with the public RPC answers; run it right after the window)
python3 valsig/verify_valsig.py run valsig/data/window-01 --out valsig/report
# reproduce offline from the committed data only; must be byte-identical
python3 valsig/verify_valsig.py run valsig/data/window-01 --out /tmp/valsig-check --offline
cmp valsig/report/checks.json /tmp/valsig-check/checks.json
# negative controls on the real recording, tests, mutants
python3 valsig/verify_valsig.py controls valsig/data/window-01
python3 -m pytest -q tests/test_valsig.py
python3 valsig/mutants.py
```

## Method

**Recording.** One JSON object per line (see the docstring of `collect_valsig.py`): `val` holds the
node's decoded fields (`k` signing key, `li`, `lh`, `st` signing time, `fl` flags, `full`, `vh`
validated hash, `mk` master key, `sig`) and `d`, the raw signed blob. A validation is recorded the
first time a (signing key, ledger index, ledger hash) is seen. `ledger` is each validated ledger
from the `ledger` stream. `man` is the node's current manifest for each signing key and trusted
master (asked again when the `manifests` stream reports a new one). `snap` is the `validators`
answer and `q` is the node's `validation_quorum` + `validator_list` + the NegativeUNL entry; both
are polled every 60 s and recorded only when they change. Peer `uri` fields (they carry an IP
address) are dropped before writing. Gaps in the ledger stream are written as `gap` events with a
reason (`stream_skip`, `reconnect`, `restart`) and never filled in. A killed recorder cuts a torn
last line on restart, closes the leftover segment and resumes the same window.

**Signed data.** A validation's signature covers SHA-512-Half of `VAL\0` + the serialized
`STValidation` without `sfSignature` (`STValidation::getSigningHash`). The decoder takes the fields
in canonical (type, field) order, refuses unknown types, truncation and misordered fields, and
cuts out exactly the `sfSignature` bytes.

**Trust chain.** VL publisher manifest (master Ed25519 + signing key signatures) -> VL blob
signature (secp256k1, low-S) -> VL validator masters == the node's trusted list and publisher key
and sequence -> each master's manifest (highest verified sequence from the node or the VL; the
node's must not be older) -> signing key -> validation signature. A signing key without a verified
manifest is `no_manifest`; one whose master is not in the VL is `not_in_vl`. Neither counts toward
quorum (they are still decoded and signature-checked).

**Quorum.** postfiatd `ValidatorList::calculateQuorum` uses a 67% quorum with a 60% floor:
`max(ceil(effective * 0.67f), ceil(unl * 0.6f))`, where `effective` = trusted masters not in the
NegativeUNL. The multiplication is in single precision, which matters: `25 * 0.6f` is slightly
above 15, so the floor is 16, not 15. `expected_quorum` reproduces this and is tested against the
compiled C++ expression for every (unl 1..120, effective 0..unl). A ledger meets quorum when the
distinct trusted, non-disabled masters with a full, valid validation of exactly its hash number at
least the node's quorum. Validations from NegativeUNL masters are reported as `nunl_excluded`.
The node's own quorum must equal the formula at every `q` sample.

**Observed ledgers.** Validations for a ledger arrive before and just after it closes. The first
ledger of each connection may have lost validations sent before the subscription, so it is an
`edge` ledger: reported, not graded. With no reconnects, the only edge ledger is the first one.

**Public RPC crosscheck.** `--sample` (default 60) graded ledgers spread evenly over the window.
The public RPC must report each as `validated` with the recorded hash. Answers are kept in
`rpc-cache.json`, and `--offline` reads only that file, so a fresh clone rebuilds every output
byte for byte. `checks.json` holds no run timestamps and no mode-dependent text.

**Negative controls** (`controls`, on in-memory copies of the real recording; each must FAIL the
named check at the mutated item):

| control | mutation | must fail |
|---|---|---|
| `flipped_blob_bit` | one bit of `sfCookie` inside the signed blob | `validations.signatures` (bad_signature at that key/ledger) |
| `wrong_key` | `sfSigningPubKey` replaced by another trusted validator's signing key | `validations.signatures` |
| `key_absent_from_manifests` | every manifest of one trusted master removed (node + VL copy) | `manifests.trusted` (master missing) |
| `tampered_ledger_hash` | `sfLedgerHash` changed in the blob (and the stream field to match) | `validations.signatures` |
| `below_quorum_signers` | a graded ledger's trusted validations cut to quorum - 1 | `quorum.ledgers` (that ledger below quorum) |

The baseline must pass first, otherwise `controls` exits 1. The tests also cover the boundary
(exactly quorum signers still passes), partial and other-hash validations, NegativeUNL accounting,
gap/hole/open-segment/short-window failures, VL and manifest tampering, and the recorder's dedupe,
gap reasons, allowlist, `uri` stripping and torn-tail resume.

## Results: window-01 (2026-11-27T00:49:58Z -> 05:19:58Z)

All numbers come from `report/checks.json` (built from the committed recording); `report/REPORT.md`
has the full tables.

* Recording: 4.499 h of ledger stream, 1 session, 1 connection, 18 closed segments, **0 gap events,
  0 index holes** over 5356 ledgers (8398802..8404157), 278501 validations, 0 unparsable lines.
* VL: start and end copies byte-identical (sha256 `5c6e03b7...7ad688`), both verify; 25 trusted
  masters, all with a verified manifest; 102 of 102 manifests verify.
* Validations: 278501 of 278501 decode, match the node's fields and verify. 133894 are from trusted
  signing keys; 144607 are from 27 signers outside the VL (checked, never counted).
* Quorum: node quorum 17 (n=25, none NegativeUNL-disabled) == the formula. **5355 of 5355** graded
  ledgers met quorum, margin min 7, max 8 (8: 5346 ledgers, 7: 9 ledgers). 24 masters signed every
  graded ledger; one signed 5346 of 5355 (99.83 %). The one edge ledger also met quorum.
* Public RPC: 60 of 60 sampled ledgers validated with the recorded hash.
* Negative controls: 5 of 5 fail the named check at the mutated item. Tests: 48 passed, ruff and
  shellcheck clean. Mutants: 36 of 36 caught.

Run logs are in `logs/` (window, online verify, controls, tests and lint, mutants, offline rebuild,
fresh public clone).

## Limits

* **Single vantage point.** Only validations that reached this node are seen. A validator that
  signed but whose validation never arrived counts as not signing. The quorum result is a lower
  bound on what the network had.
* **Window only.** Ledgers before and after the recording are not covered. Public RPC history is
  about 3 days, so the crosscheck must run soon after the window.
* **Trust root.** The VL publisher key is taken from the node's configuration (`validators` answer)
  and the published VL. valsig proves the node trusted a list signed by that key, not that the key
  is the right one.
