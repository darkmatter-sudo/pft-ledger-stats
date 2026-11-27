"""valdomain/ shared helpers (Python 3 stdlib only): domain-attestation messages and checks, a strict
TOML parser for the attestation files, and the per-validator classification.

Signature math and manifest/VL decoding are the code valsig/ uses (valsig/lib_valsig.py ->
dunl/lib_dunl.py: pure-Python Ed25519 + secp256k1, postfiatd verify() semantics).

Domain attestation (validator-keys `set_domain`, the tool the Post Fiat validator guide uses): the
validator's MASTER key signs the bytes
    "[domain-attestation-blob:" + <domain> + ":" + <master key, base58 node public> + "]"
and the operator publishes the hex signature as `attestation = "..."` in a [[VALIDATORS]] entry
whose `public_key` is that master key, at https://<domain>/.well-known/pft-ledger.toml (Post Fiat
guide, section 8). The XRPL-standard path /.well-known/xrp-ledger.toml is fetched as well.
"""

import os
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "valsig"))
import lib_valsig  # noqa: E402

L = lib_valsig.lib_dunl

TOML_PATHS = ("/.well-known/pft-ledger.toml", "/.well-known/xrp-ledger.toml")
# best first: a validator's class is the best outcome over its TOML_PATHS
CLASSES = (
    "verified",
    "bad_attestation",
    "no_attestation",
    "key_not_listed",
    "toml_unparseable",
    "http_error",
    "unreachable",
)
RANK = {c: i for i, c in enumerate(CLASSES)}
NO_DOMAIN = "no_domain"  # manifest carries no sfDomain: nothing to fetch (kept out of CLASSES)


def attestation_message(domain, master_b58):
    return f"[domain-attestation-blob:{domain}:{master_b58}]".encode("utf-8")


def verify_attestation(master_hex, domain, attestation_hex):
    """True only for a hex signature by master_hex over attestation_message(domain, b58(master))."""
    if not isinstance(attestation_hex, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", attestation_hex):
        return False
    msg = attestation_message(domain, L.node_public_b58(master_hex))
    return L.verify(master_hex, msg, bytes.fromhex(attestation_hex))


# ---------------------------------------------------------------- strict TOML (subset of 1.0)


class TomlError(ValueError):
    pass


_BARE = re.compile(r"[A-Za-z0-9_-]+")
_INT = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)")
_HEX = re.compile(r"0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|0o[0-7](?:_?[0-7])*|0b[01](?:_?[01])*")
_FLOAT = re.compile(
    r"[+-]?(?:0|[1-9](?:_?[0-9])*)(?:\.[0-9](?:_?[0-9])*)?(?:[eE][+-]?[0-9](?:_?[0-9])*)?|[+-]?(?:inf|nan)"
)
_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})?)?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d+)?"
)
_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


class _Parser:
    def __init__(self, text):
        self.t = text
        self.i = 0
        self.root = {}
        self.cur = self.root
        self.defined = set()  # ids of tables opened by a [header] or created by dotted keys
        self.inline = set()  # ids of inline tables / static arrays (closed for extension)
        self.aot = set()  # ids of arrays of tables

    def err(self, msg):
        line = self.t.count("\n", 0, self.i) + 1
        raise TomlError(f"line {line}: {msg}")

    def peek(self, k=1):
        return self.t[self.i : self.i + k]

    def ws(self):
        while self.peek() in (" ", "\t") and self.peek():
            self.i += 1

    def comment(self):
        if self.peek() == "#":
            while self.peek() and self.peek() != "\n":
                c = self.peek()
                if c != "\t" and (ord(c) < 0x20 or ord(c) == 0x7F) and c != "\r":
                    self.err("control character in comment")
                self.i += 1

    def eol(self):
        self.ws()
        self.comment()
        if self.peek(2) == "\r\n":
            self.i += 2
        elif self.peek() == "\n":
            self.i += 1
        elif self.peek():
            self.err(f"expected end of line, got {self.peek()!r}")

    def blank(self):
        """Skip whitespace, newlines and comments (inside arrays)."""
        while True:
            self.ws()
            self.comment()
            if self.peek(2) == "\r\n":
                self.i += 2
            elif self.peek() == "\n":
                self.i += 1
            else:
                return

    def parse(self):
        while True:
            self.blank()
            if not self.peek():
                return self.root
            if self.peek(2) == "[[":
                self.i += 2
                self.ws()
                path = self.key()
                self.ws()
                if self.peek(2) != "]]":
                    self.err("expected ]]")
                self.i += 2
                self.open_aot(path)
            elif self.peek() == "[":
                self.i += 1
                self.ws()
                path = self.key()
                self.ws()
                if self.peek() != "]":
                    self.err("expected ]")
                self.i += 1
                self.open_table(path)
            else:
                self.keyval(self.cur)
            self.eol()

    def walk(self, path, create_from):
        d = create_from
        for k in path:
            if k not in d:
                d[k] = {}
                self.defined.add(id(d[k]))
            v = d[k]
            if isinstance(v, list):
                if id(v) not in self.aot or not v:
                    self.err(f"key {k!r} is a static array, not a table")
                v = v[-1]
            if not isinstance(v, dict) or id(v) in self.inline:
                self.err(f"key {k!r} is not an extendable table")
            d = v
        return d

    def open_table(self, path):
        parent = self.walk(path[:-1], self.root)
        k = path[-1]
        if k in parent:
            v = parent[k]
            if not isinstance(v, dict) or id(v) in self.inline or ("hdr", id(v)) in self.defined:
                self.err(f"table {'.'.join(path)!r} defined twice")
        else:
            v = parent[k] = {}
        self.defined.add(("hdr", id(v)))
        self.cur = v

    def open_aot(self, path):
        parent = self.walk(path[:-1], self.root)
        k = path[-1]
        if k not in parent:
            parent[k] = []
            self.aot.add(id(parent[k]))
        arr = parent[k]
        if not isinstance(arr, list) or id(arr) not in self.aot:
            self.err(f"{'.'.join(path)!r} is not an array of tables")
        arr.append({})
        self.cur = arr[-1]

    def key(self):
        parts = [self.simple_key()]
        while True:
            self.ws()
            if self.peek() != ".":
                return parts
            self.i += 1
            self.ws()
            parts.append(self.simple_key())

    def simple_key(self):
        c = self.peek()
        if c == '"':
            if self.peek(3) == '"""':
                self.err("multi-line string as key")
            return self.basic_string()
        if c == "'":
            if self.peek(3) == "'''":
                self.err("multi-line string as key")
            return self.literal_string()
        m = _BARE.match(self.t, self.i)
        if not m:
            self.err("expected a key")
        self.i = m.end()
        return m.group(0)

    def keyval(self, table):
        path = self.key()
        self.ws()
        if self.peek() != "=":
            self.err("expected =")
        self.i += 1
        self.ws()
        val = self.value()
        d = table
        for k in path[:-1]:
            if k not in d:
                d[k] = {}
                self.defined.add(id(d[k]))
            d = d[k]
            hdr = ("hdr", id(d)) in self.defined and d is not table
            if not isinstance(d, dict) or id(d) in self.inline or hdr:
                self.err(f"dotted key {k!r} extends a defined table or value")
        if path[-1] in d:
            self.err(f"duplicate key {'.'.join(path)!r}")
        d[path[-1]] = val

    def value(self):
        c = self.peek()
        if c == '"':
            return self.ml_basic() if self.peek(3) == '"""' else self.basic_string()
        if c == "'":
            return self.ml_literal() if self.peek(3) == "'''" else self.literal_string()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        for word, v in (("true", True), ("false", False)):
            if self.t.startswith(word, self.i) and not _BARE.match(self.t, self.i + len(word)):
                self.i += len(word)
                return v
        m = _DATE.match(self.t, self.i)
        if m and not re.match(r"[0-9A-Za-z_.:+-]", self.t[m.end() : m.end() + 1]):
            self.i = m.end()
            return m.group(0)
        tok = self.token()
        for rx, base in ((_HEX, 0), (_INT, 10)):
            if rx.fullmatch(tok):
                self.i += len(tok)
                return int(tok.replace("_", ""), base)
        if tok and _FLOAT.fullmatch(tok):
            self.i += len(tok)
            return float(tok.replace("_", ""))
        self.err(f"invalid value {tok or c!r}")

    def token(self):
        m = re.compile(r"[0-9A-Za-z_.+-]+").match(self.t, self.i)
        return m.group(0) if m else ""

    def _char_ok(self, c, multiline):
        o = ord(c)
        if c == "\t" or (multiline and c in "\r\n"):
            return True
        return not (o < 0x20 or o == 0x7F)

    def escape(self):
        self.i += 1
        c = self.peek()
        if c in _ESC:
            self.i += 1
            return _ESC[c]
        if c in ("u", "U"):
            n = 4 if c == "u" else 8
            h = self.t[self.i + 1 : self.i + 1 + n]
            if not re.fullmatch(r"[0-9A-Fa-f]{%d}" % n, h):
                self.err("bad unicode escape")
            cp = int(h, 16)
            if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                self.err("escape is not a unicode scalar value")
            self.i += 1 + n
            return chr(cp)
        self.err(f"bad escape \\{c}")

    def basic_string(self):
        self.i += 1
        out = []
        while True:
            c = self.peek()
            if not c or c == "\n":
                self.err("unterminated string")
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                out.append(self.escape())
                continue
            if not self._char_ok(c, False):
                self.err("control character in string")
            out.append(c)
            self.i += 1

    def literal_string(self):
        self.i += 1
        j = self.i
        while True:
            c = self.peek()
            if not c or c == "\n":
                self.err("unterminated literal string")
            if c == "'":
                s = self.t[j : self.i]
                self.i += 1
                return s
            if not self._char_ok(c, False):
                self.err("control character in string")
            self.i += 1

    def _ml_start(self):
        self.i += 3
        if self.peek(2) == "\r\n":
            self.i += 2
        elif self.peek() == "\n":
            self.i += 1

    def ml_basic(self):
        self._ml_start()
        out = []
        while True:
            if not self.peek():
                self.err("unterminated multi-line string")
            if self.peek(3) == '"""':
                n = 3
                while self.peek(n + 1) == '"' * (n + 1) and n < 5:
                    n += 1
                out.append('"' * (n - 3))
                self.i += n
                return "".join(out)
            c = self.peek()
            if c == "\\":
                m = re.compile(r"\\[ \t]*\r?\n").match(self.t, self.i)
                if m:
                    self.i = m.end()
                    while self.peek() in (" ", "\t", "\n", "\r") and self.peek():
                        self.i += 1
                    continue
                out.append(self.escape())
                continue
            if not self._char_ok(c, True):
                self.err("control character in string")
            out.append(c)
            self.i += 1

    def ml_literal(self):
        self._ml_start()
        j = self.i
        while True:
            if not self.peek():
                self.err("unterminated multi-line literal string")
            if self.peek(3) == "'''":
                n = 3
                while self.peek(n + 1) == "'" * (n + 1) and n < 5:
                    n += 1
                s = self.t[j : self.i] + "'" * (n - 3)
                self.i += n
                return s
            if not self._char_ok(self.peek(), True):
                self.err("control character in string")
            self.i += 1

    def array(self):
        self.i += 1
        out = []
        while True:
            self.blank()
            if self.peek() == "]":
                self.i += 1
                break
            out.append(self.value())
            self.blank()
            if self.peek() == ",":
                self.i += 1
                continue
            if self.peek() != "]":
                self.err("expected , or ] in array")
        self.inline.add(id(out))
        return out

    def inline_table(self):
        self.i += 1
        d = {}
        self.inline.add(id(d))
        self.ws()
        if self.peek() == "}":
            self.i += 1
            return d
        while True:
            self.ws()
            saved = self.inline
            self.inline = set(saved) - {id(d)}
            self.keyval(d)
            self.inline = saved
            self.ws()
            if self.peek() == "}":
                self.i += 1
                return d
            if self.peek() != ",":
                self.err("expected , or } in inline table")
            self.i += 1


def parse_toml(text):
    """TOML text -> dict. Dates/times are returned as their source text. Raises TomlError."""
    if not isinstance(text, str):
        raise TomlError("not text")
    return _Parser(text).parse()


# ---------------------------------------------------------------- classification


def classify_fetch(rec, master_hex, domain):
    """One cached fetch record (see collect_valdomain.fetch_toml) -> (class, detail dict)."""
    if rec.get("outcome") == "unreachable":
        return "unreachable", {}
    if rec.get("outcome") != "response":
        raise ValueError(f"fetch record has unknown outcome {rec.get('outcome')!r}")
    if rec.get("status") != 200 or rec.get("truncated"):
        return "http_error", {"status": rec.get("status"), "truncated": bool(rec.get("truncated"))}
    body = rec.get("body")
    if body is None:
        raise ValueError("200 response without a cached body")
    try:
        doc = parse_toml(body.decode("utf-8"))
    except (UnicodeDecodeError, TomlError) as x:
        return "toml_unparseable", {"error": str(x)[:120]}
    vals = doc.get("VALIDATORS")
    entries = [v for v in vals if isinstance(v, dict)] if isinstance(vals, list) else []
    master_b58 = L.node_public_b58(master_hex)
    mine = [e for e in entries if e.get("public_key") == master_b58]
    detail = {"entries": len(entries), "matching": len(mine)}
    if not mine:
        return "key_not_listed", detail
    atts = [e["attestation"] for e in mine if isinstance(e.get("attestation"), str) and e["attestation"]]
    if not atts:
        return "no_attestation", detail
    if any(verify_attestation(master_hex, domain, a) for a in atts):
        return "verified", detail
    return "bad_attestation", detail


def best(classes):
    return min(classes, key=lambda c: RANK[c])
