"""Hand-built ledger for the issued/ tests: a fake read-only node and a snapshot writer.

Lines (L = low account, H = high account; Balance is from the low side):
  L1 USD  L=H1 H=G1  +100    LowLimit 1000  HighLimit 0    HighNoRipple           G1 owes H1 100
  L2 USD  L=G1 H=H2  -300    LowLimit 0     HighLimit 200  LowNoRipple+LowFreeze  G1 owes H2 300 (frozen by
                                                                                  G1, over H2's limit 200)
  L3 USD  L=H3 H=G1  0       LowLimit 50    HighLimit 0    -                      zero line, holder H3
  L4 USD  L=G2 H=H1  -40.5   LowLimit 0     HighLimit 100  -                      G2 owes H1 40.5 (same code,
                                                                                  other issuer)
  L5 EURO L=H4 H=G2  +7.25   LowLimit 10    HighLimit 0    HighNoRipple+LowFreeze G2 owes H4 7.25 (hex code,
                                                                                  frozen by the HOLDER)
  L6 USD  L=H1 H=H2  0       LowLimit 0     HighLimit 0    -                      ambiguous (no limits)
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "issued"))

import collect_issued as C  # noqa: E402

EURO = "4555524F00000000000000000000000000000000"
LEDGER_INDEX = 1000
LEDGER_HASH = "AB" * 32
NO_RIPPLE_LOW, NO_RIPPLE_HIGH = 0x00100000, 0x00200000
FREEZE_LOW, FREEZE_HIGH = 0x00400000, 0x00800000


def amt(cur, issuer, v):
    return {"currency": cur, "issuer": issuer, "value": v}


def line(idx, cur, low, high, bal, lowlim, highlim, flags=0):
    return {
        "Balance": amt(cur, "rrrrrrrrrrrrrrrrrrrrBZbvji", bal),
        "Flags": flags,
        "HighLimit": amt(cur, high, highlim),
        "LedgerEntryType": "RippleState",
        "LowLimit": amt(cur, low, lowlim),
        "index": idx,
    }


def lines():
    return [
        line("1" * 64, "USD", "rH1", "rG1", "100", "1000", "0", NO_RIPPLE_HIGH),
        line("2" * 64, "USD", "rG1", "rH2", "-300", "0", "200", NO_RIPPLE_LOW | FREEZE_LOW),
        line("3" * 64, "USD", "rH3", "rG1", "0", "50", "0"),
        line("4" * 64, "USD", "rG2", "rH1", "-40.5", "0", "100"),
        line("5" * 64, EURO, "rH4", "rG2", "7.25", "10", "0", NO_RIPPLE_HIGH | FREEZE_LOW),
        line("6" * 64, "USD", "rH1", "rH2", "0", "0", "0"),
    ]


def roots():
    out = []
    for i, a in enumerate(["rG1", "rG2", "rH1", "rH2", "rH3", "rH4", "rX9"]):
        r = {
            "Account": a,
            "Balance": "1000",
            "Flags": 0,
            "LedgerEntryType": "AccountRoot",
            "index": f"{i + 10:02d}" + "A" * 62,
        }
        if a == "rG2":
            r["Flags"] = 0x00800000 | 0x00040000  # DefaultRipple + RequireAuth
            r["TransferRate"] = 1002000000
            r["Domain"] = "6578616D706C652E6F7267"  # example.org
        out.append(r)
    return out


def objects():
    other = [{"LedgerEntryType": "DirectoryNode", "index": "9" * 64}]
    return sorted(lines() + roots() + other, key=lambda o: o["index"])


GATEWAY = {
    "rG1": {"obligations": {"USD": "100"}, "frozen_balances": {"rH2": [{"currency": "USD", "value": "300"}]}},
    "rG2": {"obligations": {"USD": "40.5", EURO: "7.25"}},
    "rH1": {
        "assets": {
            "rG1": [{"currency": "USD", "value": "100"}],
            "rG2": [{"currency": "USD", "value": "40.5"}],
        }
    },
    "rH2": {"assets": {"rG1": [{"currency": "USD", "value": "300"}]}},
    "rH3": {},
    "rH4": {"assets": {"rG2": [{"currency": EURO, "value": "7.25"}]}},
}


class FakeNode:
    """Answers like postfiatd for one validated ledger; `faults` = exceptions/answers to emit first."""

    def __init__(
        self,
        objs=None,
        gateway=None,
        drop_filtered=None,
        faults=None,
        lgr_range=(900, 2000),
        account_lines=None,
    ):
        self.objs = objs if objs is not None else objects()
        self.gateway = gateway if gateway is not None else GATEWAY
        self.drop_filtered = drop_filtered
        self.faults = list(faults or [])
        self.lgr_range = lgr_range
        self.account_lines = account_lines
        self.methods = []

    def res(self, r):
        return json.dumps(
            {"result": {"status": "success", "ledger_hash": LEDGER_HASH, "ledger_index": LEDGER_INDEX, **r}}
        )

    def err(self, e):
        return json.dumps({"result": {"status": "error", "error": e}})

    def __call__(self, body):
        req = json.loads(body)
        m, p = req["method"], req["params"][0]
        self.methods.append(m)
        if self.faults:
            f = self.faults.pop(0)
            if isinstance(f, Exception):
                raise f
            return f
        if m == "server_state":
            return self.res({"state": {"server_state": "full", "validated_ledger": {"seq": LEDGER_INDEX}}})
        if m == "server_info":
            return self.res({"info": {"complete_ledgers": f"{self.lgr_range[0]}-{self.lgr_range[1]}"}})
        if m == "ledger":
            return self.res(
                {
                    "validated": True,
                    "ledger": {
                        "ledger_index": str(LEDGER_INDEX),
                        "ledger_hash": LEDGER_HASH,
                        "account_hash": "CD" * 32,
                        "close_time": 848000000,
                        "close_time_iso": "2026-11-15T00:00:00Z",
                        "parent_hash": "EF" * 32,
                    },
                }
            )
        if m == "ledger_data":
            start = int(p.get("marker", "0"))
            limit = p.get("limit", 2048)
            chunk = self.objs[start : start + limit]
            typ = {"state": "RippleState", "amm": "AMM"}.get(p.get("type"))
            if typ:
                chunk = [o for o in chunk if o["LedgerEntryType"] == typ and o["index"] != self.drop_filtered]
            r = {"state": chunk}
            if start + limit < len(self.objs):
                r["marker"] = str(start + limit)
            return self.res(r)
        if m == "gateway_balances":
            return self.res({"account": p["account"], **self.gateway.get(p["account"], {})})
        if m == "amm_info":
            return self.err("actNotFound")
        if m == "account_lines":
            if self.account_lines is not None:
                return self.res({"lines": self.account_lines(p)})
            return self.res({"lines": account_lines(self.objs, p["account"], p.get("peer"))})
        return self.err("unknownCmd")


def account_lines(objs, account, peer):
    """account_lines as rippled renders it (balance from `account`'s side)."""
    out = []
    for o in objs:
        if o["LedgerEntryType"] != "RippleState":
            continue
        lo, hi = o["LowLimit"]["issuer"], o["HighLimit"]["issuer"]
        if account not in (lo, hi):
            continue
        me, other = ("Low", "High") if account == lo else ("High", "Low")
        p = o[f"{other}Limit"]["issuer"]
        if peer and p != peer:
            continue
        b = o["Balance"]["value"]
        if me == "High":
            b = b[1:] if b.startswith("-") else ("-" + b if b != "0" else b)
        f = o["Flags"]
        bit = {
            "LowNoRipple": NO_RIPPLE_LOW,
            "HighNoRipple": NO_RIPPLE_HIGH,
            "LowFreeze": FREEZE_LOW,
            "HighFreeze": FREEZE_HIGH,
        }
        x = {
            "account": p,
            "currency": o["Balance"]["currency"],
            "balance": b,
            "limit": o[f"{me}Limit"]["value"],
            "limit_peer": o[f"{other}Limit"]["value"],
            "quality_in": 0,
            "quality_out": 0,
        }
        if f & bit[f"{me}NoRipple"]:
            x["no_ripple"] = True
        if f & bit[f"{other}NoRipple"]:
            x["no_ripple_peer"] = True
        if f & bit[f"{me}Freeze"]:
            x["freeze"] = True
        if f & bit[f"{other}Freeze"]:
            x["freeze_peer"] = True
        out.append(x)
    return out


def make_snapshot(path, node=None, limit=4):
    """Run the real collector against a FakeNode and return the manifest."""
    node = node or FakeNode()
    rpc = C.Rpc(node, lambda m: None, retries=2, backoff=0, sleep=lambda s: None)
    orig = C.page

    def small(rpc_, lh, typ=None, limit_=limit):
        return orig(rpc_, lh, typ, limit_)

    C.page = small
    try:
        return C.snapshot(rpc, str(path), log=lambda m: None)
    finally:
        C.page = orig
