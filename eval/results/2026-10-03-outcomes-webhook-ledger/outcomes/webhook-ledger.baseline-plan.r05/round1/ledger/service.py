"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import hashlib
import hmac
import json
import logging
import re
from urllib.parse import quote

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300
SIGNATURE_HEADER = "payco-signature"

# Tables
EVENTS = "events"          # event_id -> event record (fingerprint, status, raw body, ...)
PAYMENTS = "payments"      # payment_id -> applied payment (+ refund_event_id once refunded)
HELD = "held_refunds"      # payment_id -> refund that arrived before its payment
ACCOUNTS = "accounts"      # account_id -> {currency, balance, seq}
ENTRIES = "entries"        # <quoted account_id>/<seq> -> entry

_DIGITS = re.compile(r"[0-9]+")
_CURRENCY = re.compile(r"[A-Z]{3}")
_HEX = re.compile(r"[0-9a-fA-F]+")


def _ok(result):
    return 200, {"result": result}


def _is_amount(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _is_text(v):
    return isinstance(v, str) and v != ""


def _fingerprint(event):
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _entry_key(account_id, seq):
    return "%s/%012d" % (quote(account_id, safe=""), seq)


class _Rejected(Exception):
    """Internal: abort the transaction (nothing is written) and answer with this response."""

    def __init__(self, status, body):
        self.status = status
        self.body = body


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret
        self._clock = clock

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        if not isinstance(raw_body, (bytes, bytearray)):
            return 400, {"error": "body must be bytes"}
        raw_body = bytes(raw_body)

        # 1. Signature header
        parsed = self._parse_signature(headers)
        if parsed is None:
            return 400, {"error": "missing or malformed signature header"}
        t_text, candidates = parsed

        # 2. Signature check (over t exactly as sent + "." + raw bytes)
        expected = hmac.new(self._secret, t_text.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        verified = False
        for cand in candidates:
            if hmac.compare_digest(cand.lower().encode("ascii"), expected):
                verified = True
        if not verified:
            return 401, {"error": "bad signature"}

        # 3. Freshness (exactly 300 s is accepted)
        if abs(self._clock.now() - int(t_text)) > TOLERANCE_SECONDS:
            return 401, {"error": "stale timestamp"}

        # 4. Envelope
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except ValueError:
            return 400, {"error": "body is not valid JSON"}
        if not isinstance(event, dict):
            return 400, {"error": "body must be a JSON object"}
        event_id, etype = event.get("id"), event.get("type")
        if not _is_text(event_id) or not isinstance(etype, str):
            return 400, {"error": "id and type must be strings"}
        try:
            fingerprint = _fingerprint(event)
        except (TypeError, ValueError):
            return 400, {"error": "body is not valid JSON"}

        # 5-8. Everything else happens in ONE transaction: any StoreError rolls back all of it,
        # so a failed attempt leaves no trace and PayCo's retry starts clean.
        try:
            with self._store.transaction() as txn:
                return self._process(txn, event, event_id, etype, fingerprint, raw_body)
        except _Rejected as r:
            return r.status, r.body
        except StoreError:
            return 503, {"error": "storage failure, retry"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        if not isinstance(account_id, str):
            return 0
        with self._store.transaction() as txn:
            acct = txn.get(ACCOUNTS, account_id)
        return acct["balance"] if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        if not isinstance(account_id, str):
            return []
        with self._store.transaction() as txn:
            rows = txn.scan(ENTRIES, prefix=quote(account_id, safe="") + "/")
        return [value for _, value in rows]

    # Read-only helpers for Finance / operators.

    def held_refunds(self):
        """Refunds waiting for their payment: list of dicts."""
        with self._store.transaction() as txn:
            return [v for _, v in txn.scan(HELD)]

    def quarantined_events(self):
        """Signed events that could not be applied: list of {event_id, type, reason, raw}."""
        with self._store.transaction() as txn:
            return [dict(v, event_id=k) for k, v in txn.scan(EVENTS)
                    if v.get("status") == "quarantined"]

    # ------------------------------------------------------------------ signature

    def _parse_signature(self, headers):
        value = None
        try:
            items = list(headers.items())
        except AttributeError:
            return None
        for name, v in items:
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(name, str) and name.strip().lower() == SIGNATURE_HEADER:
                if isinstance(v, bytes):
                    v = v.decode("latin-1")
                value = v
                break
        if not isinstance(value, str):
            return None
        t_values, v1_values = [], []
        for part in value.split(","):
            key, sep, val = part.strip().partition("=")
            if not sep:
                return None
            key, val = key.strip(), val.strip()
            if key == "t":
                t_values.append(val)
            elif key == "v1":
                v1_values.append(val)
            # unknown schemes (e.g. v0) are ignored
        if len(t_values) != 1 or not _DIGITS.fullmatch(t_values[0]) or not v1_values:
            return None
        if not all(_HEX.fullmatch(v) for v in v1_values):
            return None
        return t_values[0], v1_values

    # ------------------------------------------------------------------ processing

    def _process(self, txn, event, event_id, etype, fingerprint, raw_body):
        existing = txn.get(EVENTS, event_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                log.error("SECURITY INCIDENT: event %s redelivered with a different body",
                          event_id)
                # Nothing has been written in this transaction.
                raise _Rejected(409, {"error": "event id reused with a different body"})
            return _ok("duplicate")

        record = {
            "fingerprint": fingerprint,
            "type": etype,
            "status": "received",
            "raw": raw_body.decode("utf-8"),
        }

        if etype == "payment.succeeded":
            result = self._payment(txn, event, event_id, record)
        elif etype == "refund.succeeded":
            result = self._refund(txn, event, event_id, record)
        else:
            # Unknown type: remember it (raw body kept) but change nothing else.
            record["status"] = "ignored"
            result = "ignored"
        txn.put(EVENTS, event_id, record)
        return _ok(result)

    @staticmethod
    def _quarantine(record, reason):
        record["status"] = "quarantined"
        record["reason"] = reason
        log.error("QUARANTINED event: %s", reason)
        return "quarantined"

    @staticmethod
    def _fields(event):
        """Validated (account_id, payment_id, amount, currency, created) or an error string."""
        data = event.get("data")
        if not isinstance(data, dict):
            return "data missing or not an object"
        account_id, payment_id = data.get("account_id"), data.get("payment_id")
        amount, currency = data.get("amount"), data.get("currency")
        if not _is_text(account_id):
            return "invalid account_id"
        if not _is_text(payment_id):
            return "invalid payment_id"
        if not _is_amount(amount):
            return "amount must be a positive integer"
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            return "invalid currency"
        created = event.get("created")
        if isinstance(created, bool) or not isinstance(created, int):
            created = None
        return account_id, payment_id, amount, currency, created

    def _add_entry(self, txn, account_id, currency, event_id, etype, amount, payment_id, created):
        acct = txn.get(ACCOUNTS, account_id) or {"currency": currency, "balance": 0, "seq": 0}
        acct["seq"] += 1
        acct["balance"] += amount
        txn.put(ENTRIES, _entry_key(account_id, acct["seq"]), {
            "event_id": event_id, "type": etype, "amount": amount, "currency": currency,
            "account_id": account_id, "payment_id": payment_id, "created": created,
            "seq": acct["seq"],
        })
        txn.put(ACCOUNTS, account_id, acct)

    def _payment(self, txn, event, event_id, record):
        f = self._fields(event)
        if isinstance(f, str):
            return self._quarantine(record, f)
        account_id, payment_id, amount, currency, created = f

        if txn.get(PAYMENTS, payment_id) is not None:
            return self._quarantine(
                record, "payment_id %s already credited by another event" % payment_id)
        acct = txn.get(ACCOUNTS, account_id)
        if acct is not None and acct["currency"] != currency:
            return self._quarantine(
                record, "currency %s differs from account currency %s" % (currency, acct["currency"]))

        self._add_entry(txn, account_id, currency, event_id, "payment.succeeded", amount,
                        payment_id, created)
        payment = {"event_id": event_id, "account_id": account_id, "amount": amount,
                   "currency": currency, "refund_event_id": None}

        # A refund may have arrived first and been held.
        held = txn.get(HELD, payment_id)
        if held is not None:
            txn.delete(HELD, payment_id)
            held_event = txn.get(EVENTS, held["event_id"])
            if (held["account_id"], held["amount"], held["currency"]) == (
                    account_id, amount, currency):
                self._add_entry(txn, account_id, currency, held["event_id"], "refund.succeeded",
                                -amount, payment_id, held["created"])
                payment["refund_event_id"] = held["event_id"]
                held_event["status"] = "applied"
            else:
                self._quarantine(held_event, "held refund does not match payment %s" % payment_id)
            txn.put(EVENTS, held["event_id"], held_event)
        txn.put(PAYMENTS, payment_id, payment)
        record["status"] = "applied"
        return "applied"

    def _refund(self, txn, event, event_id, record):
        f = self._fields(event)
        if isinstance(f, str):
            return self._quarantine(record, f)
        account_id, payment_id, amount, currency, created = f

        payment = txn.get(PAYMENTS, payment_id)
        if payment is None:
            if txn.get(HELD, payment_id) is not None:
                return self._quarantine(
                    record, "second refund held for payment %s" % payment_id)
            txn.put(HELD, payment_id, {
                "event_id": event_id, "payment_id": payment_id, "account_id": account_id,
                "amount": amount, "currency": currency, "created": created})
            record["status"] = "held"
            return "held"

        if (payment["account_id"], payment["amount"], payment["currency"]) != (
                account_id, amount, currency):
            return self._quarantine(record, "refund does not match payment %s" % payment_id)
        if payment["refund_event_id"] is not None:
            return self._quarantine(
                record, "payment %s already refunded by %s" % (payment_id, payment["refund_event_id"]))
        self._add_entry(txn, account_id, currency, event_id, "refund.succeeded", -amount,
                        payment_id, created)
        payment["refund_event_id"] = event_id
        txn.put(PAYMENTS, payment_id, payment)
        record["status"] = "applied"
        return "applied"
