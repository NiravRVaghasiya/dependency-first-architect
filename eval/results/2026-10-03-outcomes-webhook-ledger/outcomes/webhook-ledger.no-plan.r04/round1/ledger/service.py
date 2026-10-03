"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""

import hashlib
import hmac
import json
import re
from urllib.parse import quote

from ledgerkit import StoreError

TOLERANCE_SECONDS = 300

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"

# Tables
EVENTS = "events"                  # event_id -> {hash, type, status}
ACCOUNTS = "accounts"              # account_id -> {balance, currency, seq}
ENTRIES = "entries"                # <quoted account_id>/<seq> -> entry
PAYMENTS = "payments"              # payment_id -> {account_id, amount, currency, event_id, refund_event_id}
PENDING_REFUNDS = "pending_refunds"  # payment_id -> refund that arrived before its payment
UNHANDLED = "unhandled_events"     # event_id -> raw event of a type we do not handle yet

_DIGITS = re.compile(r"^[0-9]+$")


class _Reject(Exception):
    """Abort the current transaction (rolling it back) and answer with status/body."""

    def __init__(self, status, body):
        super().__init__(status)
        self.status = status
        self.body = body


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_prefix(account_id):
    return quote(account_id, safe="") + "/"


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
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        if not self._signature_ok(raw_body, headers):
            return 401, {"error": "invalid_signature"}

        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (ValueError, RecursionError):
            return 400, {"error": "invalid_json"}
        if not isinstance(event, dict):
            return 400, {"error": "invalid_event"}
        event_id = event.get("id")
        etype = event.get("type")
        if not isinstance(event_id, str) or not event_id or not isinstance(etype, str):
            return 400, {"error": "invalid_event"}

        digest = hashlib.sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            .encode("ascii")).hexdigest()

        try:
            # One transaction per delivery: the event record, the balance and the ledger entries
            # are committed together or not at all. A StoreError (or a rejection) rolls it back.
            with self._store.transaction() as txn:
                return self._process(txn, event, event_id, etype, digest)
        except _Reject as rej:
            return rej.status, rej.body
        except StoreError:
            # Nothing was committed. Non-2xx makes PayCo retry, and the retry is idempotent.
            return 500, {"error": "store_unavailable"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            account = txn.get(ACCOUNTS, account_id)
        return account["balance"] if account else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._store.transaction() as txn:
            rows = txn.scan(ENTRIES, prefix=_entry_prefix(account_id))
        return [value for _key, value in rows]

    # ------------------------------------------------------------------ internals

    def _signature_ok(self, raw_body, headers):
        header = None
        try:
            for name, value in headers.items():
                if isinstance(name, str) and name.lower() == "payco-signature":
                    header = value
                    break
        except AttributeError:
            return False
        if isinstance(header, bytes):
            header = header.decode("latin-1")
        if not isinstance(header, str):
            return False

        timestamp = None
        candidates = []
        for part in header.split(","):
            key, sep, value = part.strip().partition("=")
            if not sep:
                continue
            if key == "t" and timestamp is None:
                timestamp = value
            elif key == "v1":
                candidates.append(value)
        if timestamp is None or not _DIGITS.match(timestamp) or not candidates:
            return False

        expected = hmac.new(self._secret, timestamp.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        matched = False
        for cand in candidates:
            try:
                cand_bytes = cand.encode("ascii")
            except UnicodeEncodeError:
                continue
            if hmac.compare_digest(cand_bytes, expected):
                matched = True
        if not matched:
            return False
        return abs(self._clock.now() - int(timestamp)) <= TOLERANCE_SECONDS

    def _process(self, txn, event, event_id, etype, digest):
        # Idempotency / tamper check comes first.
        seen = txn.get(EVENTS, event_id)
        if seen is None:
            seen = txn.get(UNHANDLED, event_id)
        if seen is not None:
            if seen["hash"] == digest:
                return 200, {"status": "duplicate", "event_id": event_id}
            # Same id, different body: security incident. Touch nothing.
            raise _Reject(409, {"error": "event_id_conflict"})

        if etype not in (PAYMENT, REFUND):
            # Unknown (future) type: acknowledge so PayCo stops retrying, but keep the event
            # (under a separate table) so it can be handled later; it is not marked as processed.
            txn.put(UNHANDLED, event_id, {"hash": digest, "event": event})
            return 200, {"status": "ignored", "reason": "unhandled_type", "event_id": event_id}

        data = event.get("data")
        if not isinstance(data, dict):
            raise _Reject(400, {"error": "invalid_event"})
        account_id = data.get("account_id")
        payment_id = data.get("payment_id")
        amount = data.get("amount")
        currency = data.get("currency")
        if (not isinstance(account_id, str) or not account_id
                or not isinstance(payment_id, str) or not payment_id
                or not isinstance(currency, str) or not currency
                or not _is_int(amount) or amount <= 0):
            raise _Reject(400, {"error": "invalid_event"})
        created = event.get("created")
        created = created if _is_int(created) else None

        fields = {"event_id": event_id, "type": etype, "payment_id": payment_id,
                  "currency": currency, "created": created}
        if etype == PAYMENT:
            return self._payment(txn, event_id, digest, account_id, payment_id, amount,
                                 currency, fields)
        return self._refund(txn, event_id, digest, account_id, payment_id, amount, currency,
                            fields)

    def _record_event(self, txn, event_id, digest, etype, status):
        txn.put(EVENTS, event_id, {"hash": digest, "type": etype, "status": status})

    def _apply(self, txn, account_id, currency, signed_amount, fields):
        account = txn.get(ACCOUNTS, account_id)
        if account is None:
            account = {"balance": 0, "currency": currency, "seq": 0}
        account["balance"] += signed_amount
        account["seq"] += 1
        entry = dict(fields)
        entry["account_id"] = account_id
        entry["amount"] = signed_amount
        txn.put(ENTRIES, _entry_prefix(account_id) + "%012d" % account["seq"], entry)
        txn.put(ACCOUNTS, account_id, account)

    def _payment(self, txn, event_id, digest, account_id, payment_id, amount, currency, fields):
        existing = txn.get(PAYMENTS, payment_id)
        if existing is not None:
            # Same payment under a different event id: never credit twice.
            if (existing["account_id"], existing["amount"], existing["currency"]) == (
                    account_id, amount, currency):
                self._record_event(txn, event_id, digest, PAYMENT, "ignored_duplicate_payment")
                return 200, {"status": "ignored", "reason": "duplicate_payment",
                             "event_id": event_id}
            raise _Reject(422, {"error": "payment_id_conflict"})

        account = txn.get(ACCOUNTS, account_id)
        if account is not None and account["currency"] != currency:
            raise _Reject(422, {"error": "currency_mismatch"})

        self._apply(txn, account_id, currency, amount, fields)
        payment = {"account_id": account_id, "amount": amount, "currency": currency,
                   "event_id": event_id, "refund_event_id": None}
        self._record_event(txn, event_id, digest, PAYMENT, "applied")

        # A refund may have arrived before this payment; settle it now if it matches.
        pending = txn.get(PENDING_REFUNDS, payment_id)
        if pending is not None and (pending["account_id"], pending["amount"],
                                    pending["currency"]) == (account_id, amount, currency):
            self._apply(txn, account_id, currency, -amount, pending["fields"])
            payment["refund_event_id"] = pending["event_id"]
            refund_event = txn.get(EVENTS, pending["event_id"])
            refund_event["status"] = "applied"
            txn.put(EVENTS, pending["event_id"], refund_event)
            txn.delete(PENDING_REFUNDS, payment_id)
        txn.put(PAYMENTS, payment_id, payment)
        return 200, {"status": "applied", "event_id": event_id}

    def _refund(self, txn, event_id, digest, account_id, payment_id, amount, currency, fields):
        payment = txn.get(PAYMENTS, payment_id)
        if payment is None:
            if txn.get(PENDING_REFUNDS, payment_id) is not None:
                self._record_event(txn, event_id, digest, REFUND, "ignored_duplicate_refund")
                return 200, {"status": "ignored", "reason": "duplicate_refund",
                             "event_id": event_id}
            # Refund before its payment: park it; it is applied when the payment arrives.
            txn.put(PENDING_REFUNDS, payment_id, {
                "event_id": event_id, "account_id": account_id, "amount": amount,
                "currency": currency, "fields": fields})
            self._record_event(txn, event_id, digest, REFUND, "pending_payment")
            return 200, {"status": "pending", "reason": "payment_not_seen", "event_id": event_id}

        if (payment["account_id"], payment["amount"], payment["currency"]) != (
                account_id, amount, currency):
            raise _Reject(422, {"error": "refund_mismatch"})
        if payment["refund_event_id"] is not None:
            self._record_event(txn, event_id, digest, REFUND, "ignored_duplicate_refund")
            return 200, {"status": "ignored", "reason": "duplicate_refund", "event_id": event_id}

        self._apply(txn, account_id, currency, -amount, fields)
        payment["refund_event_id"] = event_id
        txn.put(PAYMENTS, payment_id, payment)
        self._record_event(txn, event_id, digest, REFUND, "applied")
        return 200, {"status": "applied", "event_id": event_id}
