"""PayCo-Signature header handling: lookup, parsing and verification."""

import hashlib
import hmac
import re

HEADER_NAME = "payco-signature"
_T_RE = re.compile(r"[0-9]{1,15}")
_HEX_RE = re.compile(r"[0-9a-fA-F]+")


class HeaderError(Exception):
    """The signature header is missing, ambiguous or malformed (-> 400)."""


def _text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("latin-1")
    return value


def find_header(headers, name=HEADER_NAME):
    """Case-insensitive header lookup. Raises HeaderError if absent or given conflicting values."""
    found = set()
    for key, value in (headers or {}).items():
        key = _text(key)
        if isinstance(key, str) and key.lower() == name:
            value = _text(value)
            if not isinstance(value, str):
                raise HeaderError("malformed signature header")
            found.add(value)
    if not found:
        raise HeaderError("missing signature header")
    if len(found) > 1:
        raise HeaderError("conflicting signature headers")
    return found.pop()


def parse_header(value):
    """Return (t_string, [v1 hex values]). Unknown schemes are ignored."""
    t = None
    v1s = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, val = part.partition("=")
        if not sep:
            raise HeaderError("malformed signature header")
        name, val = name.strip(), val.strip()
        if name == "t":
            if t is not None or not _T_RE.fullmatch(val):
                raise HeaderError("malformed signature header")
            t = val
        elif name == "v1":
            if not _HEX_RE.fullmatch(val):
                raise HeaderError("malformed signature header")
            v1s.append(val.lower())
    if t is None or not v1s:
        raise HeaderError("malformed signature header")
    return t, v1s


def verify(secret, t, raw_body, v1s):
    """True if any candidate equals HMAC-SHA256(secret, "<t>." + raw_body) (constant time)."""
    expected = hmac.new(secret, t.encode("ascii") + b"." + raw_body, hashlib.sha256).hexdigest()
    expected = expected.encode("ascii")
    ok = False
    for candidate in v1s:
        if hmac.compare_digest(expected, candidate.encode("ascii")):
            ok = True
    return ok
