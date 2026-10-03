"""Event parsing, fingerprinting and field validation (pure functions)."""

import hashlib
import json
import re

_CURRENCY = re.compile(r"[A-Z]{3}\Z")


class BadBody(Exception):
    """The body is not an acceptable event envelope (-> HTTP 400)."""


def _no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise BadBody("duplicate key in JSON object")
        obj[key] = value
    return obj


def _reject_constant(name):
    raise BadBody("non-standard JSON constant")


def parse_event(raw_body):
    """Parse and check the envelope: id (non-empty str), type (str), data (object)."""
    if not isinstance(raw_body, (bytes, bytearray)):
        raise BadBody("body must be bytes")
    try:
        event = json.loads(bytes(raw_body).decode("utf-8"),
                           object_pairs_hook=_no_duplicates,
                           parse_constant=_reject_constant)
    except BadBody:
        raise
    except (ValueError, RecursionError):
        raise BadBody("body is not valid JSON")
    if not isinstance(event, dict):
        raise BadBody("body must be a JSON object")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise BadBody("missing event id")
    if not isinstance(event.get("type"), str):
        raise BadBody("missing event type")
    if not isinstance(event.get("data"), dict):
        raise BadBody("missing event data")
    return event


def fingerprint(event):
    """SHA-256 of the canonical JSON of the parsed event (insensitive to whitespace/key order)."""
    canon = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def validate_money_event(data):
    """Return None if the data of a payment/refund is valid, else a reason string."""
    if not _nonempty_str(data.get("account_id")):
        return "invalid account_id"
    if not _nonempty_str(data.get("payment_id")):
        return "invalid payment_id"
    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return "amount must be a positive integer"
    currency = data.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        return "invalid currency"
    return None
