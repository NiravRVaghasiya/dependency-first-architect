"""PayCo-Signature verification: `t=<unix seconds>,v1=<hex>`; HMAC-SHA256 over b"<t>." + body."""

import hashlib
import hmac

TOLERANCE_SECONDS = 300


def verify(header, raw_body, secret, now):
    """Return None if the signature is valid and fresh, else a short reason string."""
    if header is None:
        return "missing_signature"
    if isinstance(header, (bytes, bytearray)):
        header = bytes(header).decode("latin-1")
    if not isinstance(header, str) or not header.strip():
        return "missing_signature"

    ts = []
    sigs = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            return "malformed_signature"
        key = key.strip()
        value = value.strip()
        if key == "t":
            ts.append(value)
        elif key == "v1":
            sigs.append(value.lower())
    if len(ts) != 1 or not sigs:
        return "malformed_signature"
    t_str = ts[0]
    if not (t_str.isascii() and t_str.isdigit()):
        return "malformed_signature"

    expected = hmac.new(secret, t_str.encode("ascii") + b"." + bytes(raw_body),
                        hashlib.sha256).hexdigest()
    ok = False
    for sig in sigs:  # no early exit: constant-ish time over all candidates
        if hmac.compare_digest(sig.encode("utf-8"), expected.encode("ascii")):
            ok = True
    if not ok:
        return "bad_signature"

    if abs(now - int(t_str)) > TOLERANCE_SECONDS:
        return "stale_timestamp"
    return None
