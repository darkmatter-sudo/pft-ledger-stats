#!/usr/bin/env python3
"""Build the valsig pytest fixture from a real recording directory (captured stream data).

Writes tests/fixtures/valsig/rec/: the first --ledgers validated ledgers of the recording with every
event recorded up to (and including) the last of them, the validations that arrived for those
ledgers, and the recording's own stop event; plus state.json (window cut to the slice), the two
signed VL files and their fetch records, copied unchanged. The only edits: events after the slice
are left out and the window end is moved to the slice end. Public RPC answers for the slice are
added by the first online `verify_valsig.py run` on the fixture (rpc-cache.json).
Usage: python3 valsig/make_fixture.py SOURCE_DIR [--ledgers 8] [--out tests/fixtures/valsig/rec]
"""

import argparse
import glob
import gzip
import json
import os
import shutil


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("--ledgers", type=int, default=8)
    ap.add_argument("--out", default="tests/fixtures/valsig/rec")
    a = ap.parse_args(argv)
    events = []
    for p in sorted(glob.glob(os.path.join(a.source, "seg", "[0-9]" * 6 + ".jsonl.gz"))):
        with gzip.open(p, "rt") as fh:
            events += [json.loads(x) for x in fh]
    ledgers = [e["li"] for e in events if e["ev"] == "ledger"][: a.ledgers]
    last = ledgers[-1]
    cut = next(i for i, e in enumerate(events) if e["ev"] == "ledger" and e["li"] == last)
    keep = [e for e in events[: cut + 1] if e["ev"] != "val" or e["li"] <= last]
    keep += [e for e in events[cut + 1 :] if e["ev"] == "val" and e["li"] <= last]
    stop = [e for e in events if e["ev"] == "stop"][-1]
    keep.append(stop)
    os.makedirs(os.path.join(a.out, "seg"), exist_ok=True)
    with open(os.path.join(a.out, "seg", "000001.jsonl.gz"), "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0, filename="") as gz:
            for e in keep:
                gz.write((json.dumps(e, separators=(",", ":")) + "\n").encode())
    with open(os.path.join(a.source, "state.json")) as fh:
        st = json.load(fh)
    st["window_end"] = int(stop["t"])
    with open(os.path.join(a.out, "state.json"), "w") as fh:
        json.dump(st, fh)
    for n in ("vl-start.json", "vl-end.json", "vl-fetch.jsonl"):
        shutil.copyfile(os.path.join(a.source, n), os.path.join(a.out, n))
    kinds = {}
    for e in keep:
        kinds[e["ev"]] = kinds.get(e["ev"], 0) + 1
    print(f"fixture {a.out}: ledgers {ledgers[0]}..{last}, events {json.dumps(kinds, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
