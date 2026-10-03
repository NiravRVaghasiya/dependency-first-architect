"""PayCo signature verification (pure functions)."""

import hashlib
import hmac
import re

TOLERANCE_SECONDS = 300
_DIGITS = re.compile(r"[0-9]{1,18}\Z")


def find_header(headers, name):
    """Case-insensitive header lookup; returns a str or None."""
    want = name.lower()
    for key, value in (headers or {}).items():
        k = key.decode("latin-1") if isinstance(key, bytes) else key
        if isinstance(k, str) and k.lower() == want:
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            return value if isinstance(value, str) else None
    return None


def parse_signature_header(value):
    """'t=<secs>,v1=<hex>[,v1=<hex>...]' -> (t_str, [v1, ...]) or None if malformed."""
    if not isinstance(value, str):
        return None
    t = None
    sigs = []
    for part in value.split(","):
        key, sep, val = part.strip().partition("=")
        if not sep:
            return None
        key = key.strip()
        val = val.strip()
        if key == "t":
            if t is not None:
                return None
            t = val
        elif key == "v1":
            sigs.append(val)
        # unknown keys (other schemes) are ignored
    if t is None or not _DIGITS.match(t) or not sigs:
        return None
    return t, sigs


def verify(secret, raw_body, header_value, now):
    """True iff the signature header is well formed, one v1 matches the HMAC of '<t>.'+body,
    and |now - t| <= 300 seconds."""
    parsed = parse_signature_header(header_value)
    if parsed is None:
        return False
    t_str, sigs = parsed
    expected = hmac.new(secret, t_str.encode("ascii") + b"." + raw_body,
                        hashlib.sha256).hexdigest().encode("ascii")
    matched = False
    for sig in sigs:
        try:
            if hmac.compare_digest(expected, sig.encode("utf-8")):
                matched = True
        except Exception:
            continue
    if not matched:
        return False
    return abs(now - int(t_str)) <= TOLERANCE_SECONDS
