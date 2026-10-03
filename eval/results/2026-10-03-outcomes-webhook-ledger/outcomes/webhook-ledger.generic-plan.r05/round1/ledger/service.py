"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design (see NOTES.md): every delivery is processed in ONE store transaction, so a StoreError
rolls everything back and the delivery is answered 503 (PayCo retries). A 2xx is only returned
once the event is durably recorded (applied, ignored, or already seen).
"""

import hashlib
import hmac
import json
import logging
import re
import threading
from urllib.parse import quote

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 256 * 1024

T_EVENTS = "events"          # event_id -> {type, hash, status, reason?, raw}
T_ACCOUNTS = "accounts"      # account_id -> {currency, balance}
T_ENTRIES = "entries"        # quote(account_id)/event_id -> entry
T_PAYMENTS = "payments"      # payment_id -> {event_id, account_id, amount, currency}
T_REFUNDS = "refunds"        # payment_id -> same
T_EXCEPTIONS = "exceptions"  # event_id -> {reason, details}  (for humans)
T_INCIDENTS = "incidents"    # event_id/hash -> {raw}

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class _BadJSON(Exception):
    pass


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise _BadJSON("duplicate key")
        out[k] = v
    return out


def _no_constant(name):
    raise _BadJSON("non-finite number")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _canonical_hash(obj):
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_prefix(account_id):
    return quote(account_id, safe="") + "/"


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret if isinstance(secret, bytes) else str(secret).encode("utf-8")
        self._clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        try:
            with self._lock:
                return self._handle(raw_body, headers)
        except StoreError:
            log.warning("store error while handling delivery; answering 503")
            return 503, {"status": "retry", "error": "store_unavailable"}
        except Exception:  # a bug: never leak, never 2xx
            log.exception("unexpected error while handling delivery")
            return 500, {"status": "error", "error": "internal_error"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            acct = txn.get(T_ACCOUNTS, account_id)
        return int(acct["balance"]) if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._store.transaction() as txn:
            rows = txn.scan(T_ENTRIES, prefix=_entry_prefix(account_id))
        result = [v for _, v in rows]
        # oldest first by event time; payment before refund on a tie; event_id for determinism
        result.sort(key=lambda e: (e["created"], 0 if e["type"] == PAYMENT else 1, e["event_id"]))
        return result

    # ------------------------------------------------------------------ pipeline

    def _handle(self, raw_body, headers):
        if not isinstance(raw_body, (bytes, bytearray, memoryview)):
            return 400, {"status": "rejected", "error": "bad_body"}
        raw_body = bytes(raw_body)
        if len(raw_body) > MAX_BODY_BYTES:
            return 413, {"status": "rejected", "error": "too_large"}

        # 1. authenticate before trusting anything in the body
        err = self._verify_signature(raw_body, headers)
        if err:
            return 401, {"status": "rejected", "error": err}

        # 2. parse
        try:
            text = raw_body.decode("utf-8")
            event = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constant)
        except (UnicodeDecodeError, ValueError, _BadJSON, RecursionError):
            return 400, {"status": "rejected", "error": "malformed_json"}
        if not isinstance(event, dict):
            return 400, {"status": "rejected", "error": "malformed_event"}
        event_id = event.get("id")
        etype = event.get("type")
        if not isinstance(event_id, str) or not event_id or not isinstance(etype, str) or not etype:
            return 400, {"status": "rejected", "error": "malformed_event"}

        digest = _canonical_hash(event)
        with self._store.transaction() as txn:
            result = self._apply(txn, event, event_id, etype, digest, text)

        if result[0] == 409:
            self._record_incident(event_id, digest, text)
        return result

    def _verify_signature(self, raw_body, headers):
        """Returns None if the delivery is authentic and fresh, else an error code."""
        if not isinstance(headers, dict):
            return "missing_signature"
        values = set()
        for name, value in headers.items():
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if not isinstance(name, str) or name.strip().lower() != "payco-signature":
                continue
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            if not isinstance(value, str):
                return "malformed_signature"
            values.add(value)
        if not values:
            return "missing_signature"
        if len(values) > 1:
            return "ambiguous_signature"
        header = values.pop()

        t_values, v1_values = [], []
        for part in header.split(","):
            name, sep, val = part.strip().partition("=")
            if not sep:
                continue
            name, val = name.strip(), val.strip()
            if name == "t":
                t_values.append(val)
            elif name == "v1":
                v1_values.append(val)
        if len(t_values) != 1 or not v1_values:
            return "malformed_signature"
        t_str = t_values[0]
        if not (t_str.isascii() and t_str.isdigit()):
            return "malformed_signature"

        expected = hmac.new(self._secret, t_str.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, v.lower()) for v in v1_values if v.isascii()):
            return "invalid_signature"
        if abs(self._clock.now() - int(t_str)) > TOLERANCE_SECONDS:
            return "stale_timestamp"
        return None

    def _record_incident(self, event_id, digest, text):
        log.critical("SECURITY INCIDENT: event id %s redelivered with a different body", event_id)
        try:
            with self._store.transaction() as txn:
                key = "%s/%s" % (event_id, digest)
                if txn.get(T_INCIDENTS, key) is None:
                    txn.put(T_INCIDENTS, key, {"raw": text, "at": self._clock.now()})
        except Exception:
            log.exception("could not persist incident record")

    def _apply(self, txn, event, event_id, etype, digest, text):
        existing = txn.get(T_EVENTS, event_id)
        if existing is not None:
            if existing.get("hash") != digest:
                return 409, {"status": "conflict", "event_id": event_id}
            if existing.get("status") == "rejected":
                return 422, {"status": "rejected", "event_id": event_id,
                             "error": existing.get("reason")}
            return 200, {"status": "duplicate", "event_id": event_id}

        base = {"type": etype, "hash": digest, "raw": text}

        if etype not in (PAYMENT, REFUND):
            # Unknown/unhandled type: keep it (for later replay) and acknowledge.
            txn.put(T_EVENTS, event_id, dict(base, status="ignored"))
            return 200, {"status": "ignored", "event_id": event_id}

        problem = self._validate(event)
        if problem:
            return 422, {"status": "rejected", "event_id": event_id, "error": problem}

        data = event["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        amount, currency = data["amount"], data["currency"]
        is_refund = etype == REFUND
        own_table = T_REFUNDS if is_refund else T_PAYMENTS
        other_table = T_PAYMENTS if is_refund else T_REFUNDS

        def reject(reason, details):
            txn.put(T_EVENTS, event_id, dict(base, status="rejected", reason=reason))
            txn.put(T_EXCEPTIONS, event_id, {"reason": reason, "details": details, "type": etype})
            return 422, {"status": "rejected", "event_id": event_id, "error": reason}

        if txn.get(own_table, payment_id) is not None:
            return reject("duplicate_refund" if is_refund else "duplicate_payment",
                          "payment_id %s already has a %s from another event"
                          % (payment_id, "refund" if is_refund else "payment"))

        acct = txn.get(T_ACCOUNTS, account_id)
        if acct is not None and acct["currency"] != currency:
            return reject("currency_mismatch",
                          "account %s holds %s, event is %s" % (account_id, acct["currency"], currency))

        signed = -amount if is_refund else amount
        new_balance = (acct["balance"] if acct else 0) + signed
        txn.put(T_ENTRIES, _entry_prefix(account_id) + event_id, {
            "event_id": event_id, "type": etype, "amount": signed, "currency": currency,
            "account_id": account_id, "payment_id": payment_id, "created": event["created"],
        })
        txn.put(T_ACCOUNTS, account_id, {"currency": currency, "balance": new_balance})
        txn.put(own_table, payment_id, {"event_id": event_id, "account_id": account_id,
                                        "amount": amount, "currency": currency})

        other = txn.get(other_table, payment_id)
        if other and (other["account_id"] != account_id or other["amount"] != amount
                      or other["currency"] != currency):
            # applied anyway (PayCo's statement will show both); flag for a human
            txn.put(T_EXCEPTIONS, event_id, {
                "reason": "pair_mismatch", "type": etype,
                "details": "payment and refund for %s disagree (other event %s)"
                           % (payment_id, other["event_id"])})

        txn.put(T_EVENTS, event_id, dict(base, status="applied"))
        return 200, {"status": "applied", "event_id": event_id}

    @staticmethod
    def _validate(event):
        """Returns an error string, or None if a payment/refund event is well formed."""
        data = event.get("data")
        if not isinstance(data, dict):
            return "invalid_data"
        for field in ("account_id", "payment_id"):
            if not isinstance(data.get(field), str) or not data[field]:
                return "invalid_" + field
        if not _is_int(data.get("amount")) or data["amount"] <= 0:
            return "invalid_amount"
        cur = data.get("currency")
        if not isinstance(cur, str) or not _CURRENCY_RE.match(cur):
            return "invalid_currency"
        if not _is_int(event.get("created")):
            return "invalid_created"
        return None
