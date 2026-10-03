"""PayCo webhook signature and timestamp verification (pure functions)."""

import hashlib
import hmac
import re

TOLERANCE_SECONDS = 300
_DIGITS = re.compile(r"[0-9]{1,20}")


def find_header(headers, name):
    """Case-insensitive header lookup; returns a str or None."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if isinstance(key, bytes):
            key = key.decode("latin-1")
        if isinstance(key, str) and key.lower() == wanted:
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            return value if isinstance(value, str) else None
    return None


def parse_signature_header(value):
    """Parse 't=<secs>,v1=<hex>[,v1=<hex>...]'. Returns (t_text, [v1, ...]) or None if malformed.
    Unknown schemes are ignored."""
    if not isinstance(value, str):
        return None
    t_values = []
    v1_values = []
    for part in value.split(","):
        key, sep, val = part.strip().partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if key == "t":
            t_values.append(val)
        elif key == "v1":
            v1_values.append(val)
    if len(t_values) != 1 or not _DIGITS.fullmatch(t_values[0]) or not v1_values:
        return None
    return t_values[0], v1_values


def compute_signature(secret, t_text, raw_body):
    return hmac.new(secret, t_text.encode("ascii") + b"." + raw_body, hashlib.sha256).hexdigest()


def verify(secret, header_value, raw_body, now):
    """True only if the header is well formed, some v1 matches the HMAC of '<t>.' + raw_body,
    and |now - t| <= 300."""
    parsed = parse_signature_header(header_value)
    if parsed is None:
        return False
    t_text, candidates = parsed
    expected = compute_signature(secret, t_text, raw_body).encode("ascii")
    matched = False
    for candidate in candidates:  # no early exit: compare against every candidate
        if hmac.compare_digest(expected, candidate.encode("utf-8")):
            matched = True
    if not matched:
        return False
    return abs(now - int(t_text)) <= TOLERANCE_SECONDS
