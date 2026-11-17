"""Synthetic Post Fiat world for the acctflags tests: snapshot A (ledger 100), ledgers 101..110, snapshot B.

Accounts (flags at A): Alice 0, Bob RequireDestTag, Carol DepositAuth, Dave 0 + Domain, Erin DisallowXRP.
  101/0 Payment Alice->Bob, no tag             tecDST_TAG_NEEDED
  102/0 Payment Dave->Carol                     tecNO_PERMISSION
  103/0 Payment Alice->Bob, tag                 tesSUCCESS   (dest_tag_fix, 2 ledgers later)
  104/0 DepositPreauth Carol authorizes Dave    tesSUCCESS
  105/0 Payment Dave->Carol                     tesSUCCESS   (preauth_fix, 3 ledgers later)
  105/1 AccountSet Erin sets DepositAuth        tesSUCCESS
  106/0 Payment Alice->Frank (creates Frank)    tesSUCCESS
  106/1 Payment Erin->Bob, no tag               tecDST_TAG_NEEDED
  107/0 AccountSet Bob clears RequireDestTag    tesSUCCESS
  108/0 Payment Erin->Bob, no tag               tesSUCCESS   (flag_cleared, 2 ledgers later)
  109/0 AccountDelete Dave -> Alice             tesSUCCESS   (Dave deleted, had Domain)
  110/0 Payment Alice->Erin                     tecNO_PERMISSION (no retry; censored)
"""

import copy
import hashlib
import json
import os

RDT, DA, DXRP = 0x00020000, 0x01000000, 0x00080000
A_INDEX, B_INDEX = 100, 110
ADDR = {n: f"r{n}Xq9" for n in ("Alice", "Bob", "Carol", "Dave", "Erin", "Frank")}


def lhash(n):
    return hashlib.sha256(f"ledger{n}".encode()).hexdigest().upper()


def root(name, flags=0, **extra):
    o = {
        "LedgerEntryType": "AccountRoot",
        "Account": ADDR[name],
        "Balance": "100000000",
        "Flags": flags,
        "OwnerCount": 0,
        "Sequence": 10,
        "index": hashlib.sha256(name.encode()).hexdigest().upper(),
    }
    o.update(extra)
    return o


def accounts_a():
    return [
        root("Alice"),
        root("Bob", RDT),
        root("Carol", DA),
        root("Dave", 0, Domain="6578"),
        root("Erin", DXRP),
    ]


def accounts_b():
    return [
        root("Alice", Sequence=14),
        root("Bob", 0, Sequence=11),
        root("Carol", DA, Sequence=11, OwnerCount=1),
        root("Erin", DXRP | DA, Sequence=13),
        root("Frank", 0, Sequence=1),
    ]


def preauth_obj():
    return {
        "LedgerEntryType": "DepositPreauth",
        "Account": ADDR["Carol"],
        "Authorize": ADDR["Dave"],
        "index": "D" * 64,
    }


def mod(name, flags, prev_flags=None, **final):
    ff = {"Account": ADDR[name], "Flags": flags, "Balance": "1", "Sequence": 1}
    ff.update(final)
    node = {"LedgerEntryType": "AccountRoot", "FinalFields": ff, "PreviousFields": {"Balance": "2"}}
    if prev_flags is not None:
        node["PreviousFields"]["Flags"] = prev_flags
    return {"ModifiedNode": node}


def tx(t, sender, result, idx, dest=None, tag=False, nodes=(), **extra):
    o = {"TransactionType": t, "Account": ADDR[sender], "Fee": "10", "hash": f"{t}{sender}{idx}"}
    if dest:
        o["Destination"] = ADDR[dest]
        o["Amount"] = "5"
    if tag:
        o["DestinationTag"] = 7
    o.update(extra)
    o["metaData"] = {"TransactionIndex": idx, "TransactionResult": result, "AffectedNodes": list(nodes)}
    return o


def window_txs():
    """{ledger: [tx, ...]} for ledgers 101..110 (ledgers without entries are empty)."""
    return {
        101: [tx("Payment", "Alice", "tecDST_TAG_NEEDED", 0, "Bob", nodes=[mod("Alice", 0)])],
        102: [tx("Payment", "Dave", "tecNO_PERMISSION", 0, "Carol", nodes=[mod("Dave", 0, Domain="6578")])],
        103: [
            tx("Payment", "Alice", "tesSUCCESS", 0, "Bob", tag=True, nodes=[mod("Alice", 0), mod("Bob", RDT)])
        ],
        104: [
            tx(
                "DepositPreauth",
                "Carol",
                "tesSUCCESS",
                0,
                nodes=[
                    mod("Carol", DA),
                    {"CreatedNode": {"LedgerEntryType": "DepositPreauth", "NewFields": preauth_obj()}},
                ],
                Authorize=ADDR["Dave"],
            )
        ],
        105: [
            tx(
                "Payment",
                "Dave",
                "tesSUCCESS",
                0,
                "Carol",
                nodes=[mod("Dave", 0, Domain="6578"), mod("Carol", DA)],
            ),
            tx(
                "AccountSet",
                "Erin",
                "tesSUCCESS",
                1,
                nodes=[mod("Erin", DXRP | DA, prev_flags=DXRP)],
                SetFlag=9,
            ),
        ],
        106: [
            tx(
                "Payment",
                "Alice",
                "tesSUCCESS",
                0,
                "Frank",
                nodes=[
                    mod("Alice", 0),
                    {
                        "CreatedNode": {
                            "LedgerEntryType": "AccountRoot",
                            "NewFields": {"Account": ADDR["Frank"], "Balance": "5", "Sequence": 1},
                        }
                    },
                ],
            ),
            tx("Payment", "Erin", "tecDST_TAG_NEEDED", 1, "Bob", nodes=[mod("Erin", DXRP | DA)]),
        ],
        107: [tx("AccountSet", "Bob", "tesSUCCESS", 0, nodes=[mod("Bob", 0, prev_flags=RDT)], ClearFlag=1)],
        108: [tx("Payment", "Erin", "tesSUCCESS", 0, "Bob", nodes=[mod("Erin", DXRP | DA), mod("Bob", 0)])],
        109: [
            tx(
                "AccountDelete",
                "Dave",
                "tesSUCCESS",
                0,
                "Alice",
                nodes=[
                    {
                        "DeletedNode": {
                            "LedgerEntryType": "AccountRoot",
                            "FinalFields": {
                                "Account": ADDR["Dave"],
                                "Flags": 0,
                                "Domain": "6578",
                                "Balance": "0",
                            },
                        }
                    },
                    mod("Alice", 0),
                ],
            )
        ],
        110: [tx("Payment", "Alice", "tecNO_PERMISSION", 0, "Erin", nodes=[mod("Alice", 0)])],
    }


def ledger_header(n, txs=None):
    h = {
        "ledger_index": str(n),
        "ledger_hash": lhash(n),
        "parent_hash": lhash(n - 1),
        "account_hash": "AC" * 32,
        "close_time": 800000000 + 4 * n,
        "close_time_iso": f"2026-11-17T00:{n // 60:02d}:{n % 60:02d}Z",
    }
    if txs is not None:
        h["transactions"] = txs
    return h


class FakeNode:
    """Serves server_state, ledger (headers, transactions+expand) and paged ledger_data."""

    def __init__(self, validated=B_INDEX, state=None):
        self.validated = validated
        self.state = {
            A_INDEX: accounts_a() + [{"LedgerEntryType": "DirectoryNode", "index": "E" * 64}],
            B_INDEX: accounts_b() + [preauth_obj()],
        }
        if state:
            self.state.update(state)
        self.txs = window_txs()
        self.calls = []
        self.fail_next = 0
        self.drop_type_filter = False

    def send(self, body):
        req = json.loads(body)
        method, p = req["method"], req["params"][0]
        self.calls.append(method)
        if self.fail_next:
            self.fail_next -= 1
            return json.dumps({"result": {"status": "error", "error": "tooBusy"}})
        if method == "server_state":
            return json.dumps(
                {"result": {"state": {"server_state": "full", "validated_ledger": {"seq": self.validated}}}}
            )
        if method == "ledger":
            n = int(p.get("ledger_index") or self.index_of(p["ledger_hash"]))
            if n > self.validated:
                return json.dumps({"result": {"status": "error", "error": "lgrNotFound"}})
            txs = copy.deepcopy(self.txs.get(n, [])) if p.get("transactions") else None
            return json.dumps({"result": {"ledger": ledger_header(n, txs), "validated": True}})
        if method == "ledger_data":
            n = self.index_of(p["ledger_hash"])
            objs = sorted(self.state[n], key=lambda o: o["index"])
            if p.get("type") == "account":
                objs = [o for o in objs if o["LedgerEntryType"] == "AccountRoot"]
                if self.drop_type_filter:
                    objs = objs[1:]
            start = int(p.get("marker", 0))
            chunk = objs[start : start + p["limit"]]
            r = {"ledger_hash": p["ledger_hash"], "state": chunk}
            if start + p["limit"] < len(objs):
                r["marker"] = str(start + p["limit"])
            return json.dumps({"result": r})
        raise AssertionError(method)

    @staticmethod
    def index_of(h):
        for n in range(0, 1000):
            if lhash(n) == h:
                return n
        raise AssertionError(h)


def make_raw(tmp, node=None, limit=2):
    """Collect snapshot A, snapshot B and the capture 101..110 with collect_flags.py from a FakeNode."""
    import collect_flags as C

    node = node or FakeNode()
    rpc = C.Rpc(node.send, lambda m: None, rate=2, sleep=lambda s: None)
    C.snapshot(rpc, str(tmp / "A"), A_INDEX, log=lambda m: None, limit=limit)
    C.snapshot(rpc, str(tmp / "B"), B_INDEX, log=lambda m: None, limit=limit)
    C.capture(rpc, str(tmp / "tx"), A_INDEX + 1, B_INDEX, log=lambda m: None, sleep=lambda s: None)
    return tmp / "A", tmp / "B", tmp / "tx"


KEY = b"k" * 32


def make_pub(tmp):
    import extract_flags as X

    a, b, c = make_raw(tmp)
    texts = X.extract(str(a), str(b), str(c), KEY)
    out = tmp / "pub"
    os.makedirs(out, exist_ok=True)
    for name, text in texts.items():
        (out / name).write_text(text)
    return out


def pid(name):
    import extract_flags as X

    return X.pseudo(KEY, ADDR[name])
