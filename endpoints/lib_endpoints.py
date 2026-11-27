"""Shared logic for endpoints/ (Python 3 stdlib only).

Re-implements, from the published rules, the Dynamic UNL scoring service's provider-family grouping and
concentration counts (postfiatorg/dynamic-unl-scoring at 4c3eb28a59fb8e08add0b44f4f24a8c7ff6dac6a,
scoring_service/services/provider_families.py lines 19-155), the audit-trail content hash
(scoring_service/services/ipfs_publisher.py lines 124-126) and the concentration block the prompt
builder renders (scoring_service/services/prompt_builder.py lines 85-110). The grouping code is the same
at every scoring commit named by a round's runtime/execution_manifest.json (855fb5c, f0165b5, 7d1be72):
only build_diversity_inputs was added later.
"""

import hashlib
import ipaddress
import json
import re
from collections import Counter

# legal forms and ASN decoration that carry no provider identity
NOISE_TOKENS = frozenset("as asn inc llc ltd gmbh ag sa srl bv co corp corporation company holdings".split())
# corporate families whose ASN names share no common token; needles match whole tokens
FAMILY_RULES = (
    ("hetzner", ("hetzner",)),
    ("vultr", ("vultr", "constant")),
    ("akamai", ("akamai", "linode")),
)
UNKNOWN = "unknown"
_TRAILING_CC = re.compile(r",\s*[A-Za-z]{2}$")
_NON_WORD = re.compile(r"[\W_]+")
_TRAILING_DIGITS = re.compile(r"\d+$")
BLOCK_MARK = "NETWORK CONCENTRATION:\n"


def normalize_provider_name(as_name):
    name = _TRAILING_CC.sub("", as_name.strip())
    if " - " in name:
        name = name.split(" - ", 1)[1]
    name = _NON_WORD.sub(" ", name.lower())
    tokens = []
    for tok in name.split():
        tok = _TRAILING_DIGITS.sub("", tok)
        if tok and tok not in NOISE_TOKENS:
            tokens.append(tok)
    return " ".join(tokens)


def family_for(as_name):
    """ASN name -> provider family; None/empty -> 'unknown' (unresolved endpoint)."""
    if not as_name:
        return UNKNOWN
    norm = normalize_provider_name(as_name)
    if not norm:
        return as_name.strip().lower()
    toks = set(norm.split())
    for fam, needles in FAMILY_RULES:
        if any(n in toks for n in needles):
            return fam
    return norm


def _ranked(counts, key):
    return [{key: k, "validators": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def concentration(validators):
    """validators: iterable of dicts with 'as_name' and 'country' (None when absent).

    Returns the same structure as the published NETWORK CONCENTRATION block.
    """
    fams, ctys, unresolved = Counter(), Counter(), 0
    for v in validators:
        fam = family_for(v.get("as_name"))
        if fam == UNKNOWN:
            unresolved += 1
        else:
            fams[fam] += 1
        if v.get("country"):
            ctys[v["country"]] += 1
    return {
        "provider_families": _ranked(fams, "family"),
        "countries": _ranked(ctys, "country"),
        "unresolved_endpoints": unresolved,
    }


def content_hash(obj):
    """sha256 of compact sorted-key JSON: what bundle.json file_hashes covers."""
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def user_content(model_request):
    for m in (model_request or {}).get("messages") or []:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return None


def extract_block(model_request):
    """The rendered NETWORK CONCENTRATION JSON, or None when the prompt has no block."""
    u = user_content(model_request)
    if u is None or BLOCK_MARK not in u:
        return None
    start = u.index(BLOCK_MARK) + len(BLOCK_MARK)
    end = u.find("\n", start)
    return json.loads(u[start : end if end >= 0 else len(u)])


def prompt_entries(model_request):
    """Per-validator prompt rows (anonymous ids, sorted by master key): [(provider_family, country)]."""
    u = user_content(model_request)
    mark = "VALIDATOR DATA:"
    if u is None or mark not in u:
        return None
    rest = u[u.index(mark) + len(mark) :].lstrip()
    entries, _ = json.JSONDecoder().raw_decode(rest)
    out = []
    for e in entries:
        geo = e.get("geolocation") or {}
        out.append({"provider_family": e.get("provider_family"), "country": geo.get("country")})
    return out


def as_table(block_part, key):
    return {d[key]: d["validators"] for d in block_part}


def diff_counts(mine, theirs, key):
    """[(name, recomputed, published)] for every name whose count differs (None = absent)."""
    a, b = as_table(mine, key), as_table(theirs, key)
    return [(k, a.get(k), b.get(k)) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def hhi(counts):
    """Herfindahl-Hirschman index (0-10000) of a {name: count} table."""
    total = sum(counts.values())
    if not total:
        return None
    return round(sum((c * 10000.0 / total) * (c / total) for c in counts.values()))


_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")


def find_ips(text):
    """Every substring that parses as an IPv4 or IPv6 address (version strings like 1.0.9 do not)."""
    hits = []
    for m in _IPV4.finditer(text):
        try:
            ipaddress.IPv4Address(m.group(0))
            hits.append(m.group(0))
        except ValueError:
            pass
    for m in _IPV6.finditer(text):
        s = m.group(0)
        if s.count(":") < 2 or s.strip(":") == "":
            continue
        try:
            ipaddress.IPv6Address(s)
            hits.append(s)
        except ValueError:
            pass
    return hits
