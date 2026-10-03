"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Every delivery is processed in ONE store transaction, so a StoreError (raised by put) rolls back
everything and we answer 503; PayCo then redelivers and the event is processed from scratch.
Once the transaction commits the event is fully applied, so a redelivery is a pure no-op.

Tables:
  events          event_id -> {fingerprint, type, outcome, reason, received_at, event}
  payments        payment_id -> {event_id, account_id, amount, currency, refunded}
                  (applied payments; "refunded" = total of applied refunds, always <= amount)
  pending_refunds "<q(payment_id)>/<event_id>" -> {payment_id}             (refund before payment)
  accounts        account_id -> {currency, balance}
  ledger          "<q(account_id)>/<event_id>" -> entry dict
"""

import hashlib
import json
import logging
import math
import re
from urllib.parse import quote

from ledgerkit import StoreError

from . import signature

log = logging.getLogger("ledger")

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"
_CURRENCY = re.compile(r"[A-Z]{3}")


class _BadJSON(ValueError):
    pass


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise _BadJSON("duplicate key")
        obj[key] = value
    return obj


def _reject_constant(name):
    raise _BadJSON("non-finite number")


def _parse(raw_body):
    text = raw_body.decode("utf-8")
    return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)


def _fingerprint(obj):
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value):
    return isinstance(value, str) and value != ""


def _payload_problem(obj):
    """Why a payment/refund event cannot be applied, or None if its payload is well formed."""
    data = obj.get("data")
    if not isinstance(data, dict):
        return "bad_data"
    if not _is_nonempty_str(data.get("account_id")):
        return "bad_account_id"
    if not _is_nonempty_str(data.get("payment_id")):
        return "bad_payment_id"
    amount = data.get("amount")
    if not _is_int(amount) or amount <= 0:
        return "bad_amount"
    currency = data.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        return "bad_currency"
    created = obj.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return "bad_created"
    if isinstance(created, float) and not math.isfinite(created):
        return "bad_created"
    return None


def _q(text):
    return quote(text, safe="")


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        self._clock = clock

    # ------------------------------------------------------------------ handle

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        raw_body = bytes(raw_body)

        header = signature.find_header(headers, "PayCo-Signature")
        if header is None or not signature.verify(self._secret, header, raw_body, self._clock.now()):
            return 401, {"error": "unauthorized"}

        try:
            obj = _parse(raw_body)
        except (ValueError, RecursionError):  # includes UnicodeDecodeError, JSONDecodeError
            log.warning("signed delivery with unparseable body")
            return 400, {"error": "bad_request"}
        if (not isinstance(obj, dict) or not _is_nonempty_str(obj.get("id"))
                or not isinstance(obj.get("type"), str)):
            log.warning("signed delivery without usable id/type")
            return 400, {"error": "bad_request"}

        try:
            fingerprint = _fingerprint(obj)
        except (ValueError, TypeError, RecursionError):
            return 400, {"error": "bad_request"}

        alerts = []
        try:
            with self._store.transaction() as txn:
                result = self._ingest(txn, obj, fingerprint, alerts)
        except StoreError:
            log.error("store write failed for event %s; asking PayCo to retry", obj["id"])
            return 503, {"error": "unavailable"}

        for alert in alerts:
            log.warning(alert)
        status, body = result
        return status, body

    def _ingest(self, txn, obj, fingerprint, alerts):
        event_id = obj["id"]
        existing = txn.get("events", event_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                alerts.append("SECURITY: event %s redelivered with a different body" % event_id)
                return 409, {"error": "conflict"}
            if existing["outcome"] == "rejected":
                return 422, {"error": "unprocessable"}  # still rejected; nothing changes
            return 200, {"status": "duplicate"}

        record = {
            "fingerprint": fingerprint,
            "type": obj["type"],
            "outcome": None,
            "reason": None,
            "received_at": self._clock.now(),
            "event": obj,  # the whole verified event, so unhandled/quarantined ones can be replayed
        }

        if obj["type"] not in (PAYMENT, REFUND):
            record["outcome"] = "unhandled"
            txn.put("events", event_id, record)
            return 200, {"status": "unhandled"}

        problem = _payload_problem(obj)
        if problem is not None:
            return self._quarantine(txn, event_id, record, problem, alerts)

        if obj["type"] == PAYMENT:
            return self._payment(txn, event_id, record, alerts)
        return self._refund(txn, event_id, record, alerts)

    def _quarantine(self, txn, event_id, record, reason, alerts):
        record["outcome"] = "quarantined"
        record["reason"] = reason
        txn.put("events", event_id, record)
        alerts.append("QUARANTINED event %s: %s" % (event_id, reason))
        return 200, {"status": "quarantined"}

    # ---------------------------------------------------------------- payments

    def _payment(self, txn, event_id, record, alerts):
        data = record["event"]["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        amount, currency = data["amount"], data["currency"]

        if txn.get("payments", payment_id) is not None:
            return self._quarantine(txn, event_id, record, "duplicate_payment_id", alerts)
        account = txn.get("accounts", account_id)
        if account is None:
            account = {"currency": currency, "balance": 0}  # currency fixed by first payment
        elif account["currency"] != currency:
            return self._quarantine(txn, event_id, record, "currency_mismatch", alerts)

        account["balance"] += amount
        self._append_entry(txn, account_id, event_id, record, amount)
        record["outcome"] = "applied"
        txn.put("events", event_id, record)

        # Refunds that arrived before this payment can be settled now, in the same transaction.
        # They are settled in (created, event_id) order so the result does not depend on the order
        # in which they arrived; any that would push the total past the payment are quarantined
        # (their delivery was already answered 200 "pending", so 422 is no longer possible).
        waiting = []
        for key, _ in list(txn.scan("pending_refunds", prefix=_q(payment_id) + "/")):
            refund_id = key.split("/", 1)[1]
            refund_record = txn.get("events", refund_id)
            waiting.append((refund_record["event"]["created"], refund_id, key, refund_record))
        waiting.sort(key=lambda w: (w[0], w[1]))

        refunded = 0
        for _created, refund_id, key, refund_record in waiting:
            refund_data = refund_record["event"]["data"]
            refund_amount = refund_data["amount"]
            reason = None
            if (refund_data["account_id"] != account_id or refund_data["currency"] != currency):
                reason = "refund_mismatch"
            elif refunded + refund_amount > amount:
                reason = "refund_exceeds_payment"
            if reason is None:
                refunded += refund_amount
                account["balance"] -= refund_amount
                self._append_entry(txn, account_id, refund_id, refund_record, -refund_amount)
                refund_record["outcome"] = "applied"
            else:
                refund_record["outcome"] = "quarantined"
                refund_record["reason"] = reason
                alerts.append("QUARANTINED event %s: %s" % (refund_id, reason))
            txn.put("events", refund_id, refund_record)
            txn.delete("pending_refunds", key)

        txn.put("payments", payment_id, {"event_id": event_id, "account_id": account_id,
                                         "amount": amount, "currency": currency,
                                         "refunded": refunded})
        txn.put("accounts", account_id, account)
        return 200, {"status": "applied"}

    # ----------------------------------------------------------------- refunds

    def _refund(self, txn, event_id, record, alerts):
        data = record["event"]["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        amount, currency = data["amount"], data["currency"]

        payment = txn.get("payments", payment_id)
        if payment is None:
            record["outcome"] = "pending"
            txn.put("events", event_id, record)
            txn.put("pending_refunds", _q(payment_id) + "/" + event_id, {"payment_id": payment_id})
            return 200, {"status": "pending"}

        if payment["account_id"] != account_id or payment["currency"] != currency:
            return self._quarantine(txn, event_id, record, "refund_mismatch", alerts)
        if payment["refunded"] + amount > payment["amount"]:
            # Partial refunds are allowed, but the total must never exceed the payment.
            # Reject (422) and leave the account untouched; keep the event for finance.
            record["outcome"] = "rejected"
            record["reason"] = "refund_exceeds_payment"
            txn.put("events", event_id, record)
            alerts.append("REJECTED event %s: refunds would exceed payment %s"
                          % (event_id, payment_id))
            return 422, {"error": "unprocessable"}

        account = txn.get("accounts", account_id)
        account["balance"] -= amount
        self._append_entry(txn, account_id, event_id, record, -amount)
        payment["refunded"] += amount
        txn.put("payments", payment_id, payment)
        record["outcome"] = "applied"
        txn.put("events", event_id, record)
        txn.put("accounts", account_id, account)
        return 200, {"status": "applied"}

    def _append_entry(self, txn, account_id, event_id, record, signed_amount):
        obj = record["event"]
        data = obj["data"]
        txn.put("ledger", _q(account_id) + "/" + event_id, {
            "event_id": event_id,
            "type": obj["type"],
            "amount": signed_amount,
            "currency": data["currency"],
            "account_id": account_id,
            "payment_id": data["payment_id"],
            "created": obj["created"],
            "received_at": record["received_at"],
        })

    # ------------------------------------------------------------------- reads

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            account = txn.get("accounts", account_id)
        return account["balance"] if account is not None else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds). Ordered by (created, event_id), so arrival order does not matter."""
        with self._store.transaction() as txn:
            rows = txn.scan("ledger", prefix=_q(account_id) + "/")
        result = [value for _, value in rows]
        result.sort(key=lambda e: (e["created"], e["event_id"]))
        return result
