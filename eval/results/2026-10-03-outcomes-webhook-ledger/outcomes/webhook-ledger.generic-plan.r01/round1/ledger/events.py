"""Strict parsing and validation of PayCo events, and the canonical digest used to decide whether
two deliveries carry "the same body"."""

import hashlib
import json
import re

HANDLED_TYPES = ("payment.succeeded", "refund.succeeded")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class Invalid(Exception):
    pass


def _reject_constant(name):
    raise Invalid("non-finite number %s" % name)


def _no_duplicate_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise Invalid("duplicate key %r" % k)
        obj[k] = v
    return obj


def parse(raw_body):
    """Parse the body into a dict; raise Invalid if it is not a JSON object with a string `id`
    and string `type`."""
    try:
        obj = json.loads(raw_body, parse_constant=_reject_constant,
                         object_pairs_hook=_no_duplicate_keys)
    except Invalid:
        raise
    except (ValueError, RecursionError) as e:
        raise Invalid("body is not valid JSON: %s" % type(e).__name__)
    if not isinstance(obj, dict):
        raise Invalid("body is not a JSON object")
    for field in ("id", "type"):
        if not isinstance(obj.get(field), str) or not obj[field]:
            raise Invalid("%s must be a non-empty string" % field)
    return obj


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate(obj):
    """Validate a parsed event. Unknown types only need `id` and `type` (checked in parse()).
    Returns a normalised dict for handled types (account_id, payment_id, amount, currency,
    created), or None for types we do not handle."""
    if obj["type"] not in HANDLED_TYPES:
        return None
    if not _is_int(obj.get("created")):
        raise Invalid("created must be an integer")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise Invalid("data must be an object")
    for field in ("account_id", "payment_id"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise Invalid("%s must be a non-empty string" % field)
    amount = data.get("amount")
    if not _is_int(amount) or amount <= 0:
        raise Invalid("amount must be a positive integer")
    currency = data.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency):
        raise Invalid("currency must be a 3-letter uppercase ISO 4217 code")
    return {
        "account_id": data["account_id"],
        "payment_id": data["payment_id"],
        "amount": amount,
        "currency": currency,
        "created": obj["created"],
    }


def digest(obj):
    """SHA-256 of the canonical JSON form (sorted keys, no whitespace)."""
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()
