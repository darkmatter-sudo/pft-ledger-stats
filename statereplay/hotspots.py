#!/usr/bin/env python3
"""Write-hotspot analysis of a replayed A -> B window, and REPORT.md / report.json (stdlib only).

Runs replay.replay() over the published data/ folder (extract_replay.py output) and, from the same
pass, measures where the ledger's writes go:

  tx types      per TransactionType: transactions, result classes, AffectedNodes per transaction
                (min / p50 / p95 / max / mean, nearest-rank) and the created/modified/deleted split
  objects       most-rewritten objects (ModifiedNode count) and most-rewritten accounts (AccountRoot
                ModifiedNode count, and all nodes written by transactions the account sent)
  directories   DirectoryNode churn: pages created/modified/deleted, owner vs order-book pages,
                Indexes entries added / removed
  net-unchanged objects modified 2+ times whose state at B equals their state at A apart from the
                PreviousTxnID/PreviousTxnLgrSeq thread, and objects created and deleted inside the window
  write share   AffectedNodes by LedgerEntryType and kind, plus the derived (metadata-less) writes:
                LedgerHashes skip lists every ledger and NegativeUNL on flag ledgers

The report is a pure function of data/ (no clock, no network), so an offline rebuild from committed
data is byte-identical. If data/crosscheck-*.json summaries exist (crosscheck_replay.py), they are
included. A replay that is not EXACT is reported as such and the exit code is 1.

Usage:
  ./hotspots.py --data statereplay/data --out statereplay [--top 15]
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import replay  # noqa: E402

KINDS = ("CreatedNode", "ModifiedNode", "DeletedNode")
THREAD = set(replay.THREAD_FIELDS)


def nearest_rank(sorted_vals, q):
    """Nearest-rank percentile of an ascending list (q in 0..100); None for an empty list."""
    if not sorted_vals:
        return None
    k = max(1, -(-len(sorted_vals) * q // 100))
    return sorted_vals[int(k) - 1]


def unthreaded(obj):
    return {k: v for k, v in obj.items() if k not in THREAD}


def label(obj, idx):
    """Report label of an object: AccountRoot -> its (pseudonymous) Account, else type:index-prefix."""
    if obj is None:
        return idx[:16]
    if obj.get("LedgerEntryType") == "AccountRoot" and "Account" in obj:
        return obj["Account"]
    return f"{obj.get('LedgerEntryType')}:{idx[:16]}"


def is_book_dir(obj):
    return obj is not None and "ExchangeRate" in obj


class Hotspots:
    def __init__(self, initial):
        self.initial = {k: dict(v) for k, v in initial.items()}
        self.by_type = {}
        self.mods = {}
        self.acct_mods = {}
        self.sender_nodes = {}
        self.kind_by_entry = {}
        self.created = set()
        self.deleted_after_create = set()
        self.dir = {
            "pages_created": 0,
            "pages_modified": 0,
            "pages_deleted": 0,
            "owner_pages_touched": set(),
            "book_pages_touched": set(),
            "indexes_added": 0,
            "indexes_removed": 0,
        }
        self.etype = {}

    def on_tx(self, ledger, tx, touched):
        t = tx.get("TransactionType", "?")
        result = tx["metaData"].get("TransactionResult", "?")
        e = self.by_type.setdefault(
            t, {"transactions": 0, "results": {}, "nodes": [], "kinds": dict.fromkeys(KINDS, 0)}
        )
        e["transactions"] += 1
        cls = result[:3]
        e["results"][cls] = e["results"].get(cls, 0) + 1
        e["nodes"].append(len(touched))
        sender = tx.get("Account")
        if sender is not None:
            self.sender_nodes[sender] = self.sender_nodes.get(sender, 0) + len(touched)
        for node, (kind, etype, idx) in zip(tx["metaData"]["AffectedNodes"], touched, strict=True):
            e["kinds"][kind] += 1
            k = self.kind_by_entry.setdefault(etype, dict.fromkeys(KINDS, 0))
            k[kind] += 1
            body = node[kind]
            if kind == "CreatedNode":
                self.created.add(idx)
            elif kind == "DeletedNode" and idx in self.created:
                self.deleted_after_create.add(idx)
            if kind == "ModifiedNode":
                self.mods[idx] = self.mods.get(idx, 0) + 1
                if etype == "AccountRoot":
                    acct = body.get("FinalFields", {}).get("Account", idx[:16])
                    self.acct_mods[acct] = self.acct_mods.get(acct, 0) + 1
            if etype == "DirectoryNode":
                self.directory(kind, body, idx)
            self.etype[idx] = etype

    def directory(self, kind, body, idx):
        d = self.dir
        d[
            {
                "CreatedNode": "pages_created",
                "ModifiedNode": "pages_modified",
                "DeletedNode": "pages_deleted",
            }[kind]
        ] += 1
        fields = body.get("FinalFields") or body.get("NewFields") or {}
        (d["book_pages_touched"] if "ExchangeRate" in fields else d["owner_pages_touched"]).add(idx)
        before = set(body.get("PreviousFields", {}).get("Indexes", fields.get("Indexes", [])))
        after = set(fields.get("Indexes", []))
        if kind == "CreatedNode":
            before = set()
        if kind == "DeletedNode":
            after = set()
        d["indexes_added"] += len(after - before)
        d["indexes_removed"] += len(before - after)

    def summary(self, final, stats, top):
        tx_types = {}
        for t, e in sorted(self.by_type.items()):
            vals = sorted(e["nodes"])
            tx_types[t] = {
                "transactions": e["transactions"],
                "results": dict(sorted(e["results"].items())),
                "nodes_total": sum(vals),
                "nodes_min": vals[0],
                "nodes_p50": nearest_rank(vals, 50),
                "nodes_p95": nearest_rank(vals, 95),
                "nodes_max": vals[-1],
                "nodes_mean": round(sum(vals) / len(vals), 3),
                "kinds": e["kinds"],
            }
        objs = sorted(self.mods.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        most_objects = [
            {
                "object": label(final.get(i) or self.initial.get(i), i),
                "type": (final.get(i) or self.initial.get(i) or {}).get("LedgerEntryType"),
                "modified": n,
            }
            for i, n in objs
        ]
        most_accounts = [
            {"account": a, "accountroot_modified": n, "nodes_written_as_sender": self.sender_nodes.get(a, 0)}
            for a, n in sorted(self.acct_mods.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        ]
        most_senders = [
            {"account": a, "nodes_written_as_sender": n}
            for a, n in sorted(self.sender_nodes.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        ]
        net_unchanged = {}
        for i, n in self.mods.items():
            if (
                n >= 2
                and i in self.initial
                and i in final
                and unthreaded(self.initial[i]) == unthreaded(final[i])
            ):
                t = final[i]["LedgerEntryType"]
                net_unchanged.setdefault(t, {"objects": 0, "modifications": 0})
                net_unchanged[t]["objects"] += 1
                net_unchanged[t]["modifications"] += n
        ephemeral = {}
        for i in self.deleted_after_create:
            if i not in final:
                t = self.etype[i]
                ephemeral[t] = ephemeral.get(t, 0) + 1
        meta_nodes = stats["nodes"]
        derived_skip = stats["ledgers"] + sum(
            1 for n in range(stats["first"], stats["last"] + 1) if (n - 1) % 256 == 0
        )
        share = {}
        for etype, kinds in sorted(self.kind_by_entry.items()):
            total = sum(kinds.values())
            share[etype] = dict(
                kinds, total=total, share_pct=round(100 * total / meta_nodes, 2) if meta_nodes else 0
            )
        d = self.dir
        return {
            "tx_types": tx_types,
            "most_rewritten_objects": most_objects,
            "most_rewritten_accounts": most_accounts,
            "top_senders_by_nodes_written": most_senders,
            "directory_churn": {
                "pages_created": d["pages_created"],
                "pages_modified": d["pages_modified"],
                "pages_deleted": d["pages_deleted"],
                "owner_pages_touched": len(d["owner_pages_touched"]),
                "book_pages_touched": len(d["book_pages_touched"]),
                "indexes_added": d["indexes_added"],
                "indexes_removed": d["indexes_removed"],
            },
            "net_unchanged_repeatedly_modified": dict(sorted(net_unchanged.items())),
            "created_and_deleted_in_window": dict(sorted(ephemeral.items())),
            "write_share": share,
            "derived_writes": {
                "LedgerHashes_skip_list_updates": derived_skip,
                "NegativeUNL_updates": len(stats.get("negative_unl", [])),
            },
            "metadata_nodes": meta_nodes,
        }


def analyze(data_dir, top=15):
    ha, state, hb, target, rows, txs = replay.load_published(data_dir)
    h = Hotspots(state)
    diffs, stats = replay.replay(ha, state, hb, target, rows, txs, on_tx=h.on_tx)
    stats = dict(stats, first=ha["ledger_index"] + 1, last=hb["ledger_index"])
    closes = {r["ledger_index"]: r["close_time"] for r in rows}
    out = {
        "window": {
            "A": ha,
            "B": hb,
            "ledgers": stats["ledgers"],
            "transactions": stats["transactions"],
            "hours": round((closes[hb["ledger_index"]] - closes[ha["ledger_index"]]) / 3600, 3),
        },
        "replay": {
            "result": "EXACT" if not diffs else "MISMATCH",
            "objects_A": len(h.initial),
            "objects_B": len(target),
            "objects_replayed": len(state),
            "diffs": diffs,
            "negative_unl_derived": stats.get("negative_unl", []),
        },
        "hotspots": h.summary(state, stats, top),
    }
    out["crosscheck"] = {}
    for name in sorted(os.listdir(data_dir)):
        if name.startswith("crosscheck-") and name.endswith(".json"):
            with open(os.path.join(data_dir, name)) as f:
                out["crosscheck"][name[len("crosscheck-") : -5]] = json.load(f)
    return out


def fmt_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def render(r):
    w, rp, hs = r["window"], r["replay"], r["hotspots"]
    L = ["# statereplay: metadata replay A -> B and write hotspots (Post Fiat testnet)", ""]
    a, b = w["A"], w["B"]
    L.append(
        f"Window: snapshot A ledger {a['ledger_index']} ({a['close_time_iso']}, hash `{a['ledger_hash']}`) "
        f"to snapshot B ledger {b['ledger_index']} ({b['close_time_iso']}, hash `{b['ledger_hash']}`): "
        f"{w['ledgers']} ledgers, {w['hours']} h, {w['transactions']} transactions, "
        f"{hs['metadata_nodes']} metadata AffectedNodes."
    )
    L += ["", "Generated by `hotspots.py` from the committed `data/` only; accounts are HMAC pseudonyms.", ""]
    L += ["## 1. Replay result", ""]
    L.append(
        fmt_table(
            ["result", "objects at A", "objects at B", "objects after replay", "differences"],
            [[rp["result"], rp["objects_A"], rp["objects_B"], rp["objects_replayed"], len(rp["diffs"])]],
        )
    )
    L.append("")
    if rp["negative_unl_derived"]:
        L.append(
            "Derived (metadata-less) NegativeUNL updates on flag ledgers: "
            + ", ".join(f"{n} {a}" for n, a in rp["negative_unl_derived"])
            + "."
        )
        L.append("")
    for d in rp["diffs"][:20]:
        L.append(f"- `{d['index']}` {d['type']} {d['reason']} {','.join(d.get('fields', []))}")
    L += ["## 2. Nodes touched per transaction type", ""]
    rows = []
    for t, e in sorted(hs["tx_types"].items(), key=lambda kv: (-kv[1]["nodes_total"], kv[0])):
        res = " ".join(f"{k}:{v}" for k, v in e["results"].items())
        rows.append(
            [
                t,
                e["transactions"],
                res,
                e["nodes_total"],
                e["nodes_min"],
                e["nodes_p50"],
                e["nodes_p95"],
                e["nodes_max"],
                e["nodes_mean"],
                e["kinds"]["CreatedNode"],
                e["kinds"]["ModifiedNode"],
                e["kinds"]["DeletedNode"],
            ]
        )
    L.append(
        fmt_table(
            [
                "type",
                "txs",
                "results",
                "nodes",
                "min",
                "p50",
                "p95",
                "max",
                "mean",
                "created",
                "modified",
                "deleted",
            ],
            rows,
        )
    )
    L += ["", "## 3. Write share by LedgerEntryType", ""]
    rows = [
        [t, s["total"], s["share_pct"], s["CreatedNode"], s["ModifiedNode"], s["DeletedNode"]]
        for t, s in sorted(hs["write_share"].items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    ]
    L.append(fmt_table(["LedgerEntryType", "nodes", "share %", "created", "modified", "deleted"], rows))
    dw = hs["derived_writes"]
    L += [
        "",
        f"Not in metadata (derived by the replay): {dw['LedgerHashes_skip_list_updates']} LedgerHashes "
        f"skip-list updates, {dw['NegativeUNL_updates']} NegativeUNL update(s).",
        "",
    ]
    L += ["## 4. Most-rewritten objects", ""]
    L.append(
        fmt_table(
            ["object", "type", "ModifiedNode count"],
            [[o["object"], o["type"], o["modified"]] for o in hs["most_rewritten_objects"]],
        )
    )
    L += ["", "## 5. Most-rewritten accounts", ""]
    L.append(
        fmt_table(
            ["account", "AccountRoot modified", "nodes written as sender"],
            [
                [a["account"], a["accountroot_modified"], a["nodes_written_as_sender"]]
                for a in hs["most_rewritten_accounts"]
            ],
        )
    )
    L += ["", "Top senders by nodes written:", ""]
    L.append(
        fmt_table(
            ["account", "nodes written as sender"],
            [[a["account"], a["nodes_written_as_sender"]] for a in hs["top_senders_by_nodes_written"]],
        )
    )
    dc = hs["directory_churn"]
    L += ["", "## 6. DirectoryNode churn", ""]
    L.append(
        fmt_table(
            [
                "pages created",
                "pages modified",
                "pages deleted",
                "owner pages touched",
                "book pages touched",
                "Indexes added",
                "Indexes removed",
            ],
            [
                [
                    dc["pages_created"],
                    dc["pages_modified"],
                    dc["pages_deleted"],
                    dc["owner_pages_touched"],
                    dc["book_pages_touched"],
                    dc["indexes_added"],
                    dc["indexes_removed"],
                ]
            ],
        )
    )
    L += ["", "## 7. Repeatedly modified but net-unchanged", ""]
    nu = hs["net_unchanged_repeatedly_modified"]
    if nu:
        L.append(
            fmt_table(
                ["type", "objects", "modifications"],
                [[t, v["objects"], v["modifications"]] for t, v in nu.items()],
            )
        )
    else:
        L.append("None: every object modified 2+ times ended the window in a different state.")
    eph = hs["created_and_deleted_in_window"]
    L += [
        "",
        "Objects created and deleted inside the window (never visible in either snapshot): "
        + (", ".join(f"{t} {n}" for t, n in eph.items()) if eph else "none")
        + ".",
        "",
    ]
    if r["crosscheck"]:
        L += ["## 8. Public-RPC crosscheck", ""]
        rows = []
        for name, c in r["crosscheck"].items():
            rows.append(
                [name, c.get("mode"), c.get("checks"), c.get("match"), c.get("mismatch"), c.get("retries")]
            )
        L.append(fmt_table(["summary", "mode", "checks", "match", "mismatch", "retries"], rows))
        L.append("")
    return "\n".join(L).rstrip() + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args(argv)
    try:
        r = analyze(a.data, a.top)
    except replay.ReplayError as e:
        print(f"FAIL {e}")
        return 1
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(r, f, indent=1, sort_keys=True)
        f.write("\n")
    with open(os.path.join(a.out, "REPORT.md"), "w") as f:
        f.write(render(r))
    print(
        f"REPLAY {r['replay']['result']} diffs={len(r['replay']['diffs'])} ledgers={r['window']['ledgers']} "
        f"transactions={r['window']['transactions']} nodes={r['hotspots']['metadata_nodes']}"
    )
    print(f"WROTE {os.path.join(a.out, 'REPORT.md')} {os.path.join(a.out, 'report.json')}")
    return 0 if r["replay"]["result"] == "EXACT" else 1


if __name__ == "__main__":
    sys.exit(main())
