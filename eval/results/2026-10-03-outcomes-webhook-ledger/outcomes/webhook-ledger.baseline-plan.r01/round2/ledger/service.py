"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design summary (details in NOTES.md): every delivery is processed inside ONE store transaction that
writes the event record, the account balance, the ledger entry and any payment/refund bookkeeping
together. A StoreError therefore rolls everything back and we answer 503, so PayCo retries; a
retry runs against untouched state. Duplicates are detected by event id + body hash.
"""

import hashlib
import hmac
import json
import re
import threading

from ledgerkit import StoreError

SIGNATURE_HEADER = "payco-signature"
TOLERANCE_SECONDS = 300
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_TIMESTAMP_RE = re.compile(r"[0-9]{1,15}")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value):
    return isinstance(value, str) and value != ""


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = bytes(secret)
        self._clock = clock
        self._lock = threading.RLock()

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.

        Status codes: 200 handled (applied, duplicate, pending refund, ignored type, quarantined);
        400 signed but malformed; 401 bad/missing/stale signature; 409 same event id with a
        different body; 422 a refund that would make the total refunded for its payment exceed the
        payment amount (account untouched); 503 database write failed (nothing was changed, PayCo should retry);
        500 unexpected error (also retried by PayCo).
        """
        with self._lock:
            try:
                return self._handle(raw_body, headers)
            except StoreError:
                return 503, {"error": "storage_unavailable"}
            except Exception:
                return 500, {"error": "internal_error"}

    # ------------------------------------------------------------------ request pipeline

    def _handle(self, raw_body, headers):
        if not isinstance(raw_body, (bytes, bytearray)):
            return 400, {"error": "body must be bytes"}
        raw_body = bytes(raw_body)

        problem = self._check_signature(raw_body, headers)
        if problem:
            return 401, {"error": problem}

        # Only signed bodies get past this point; nothing unsigned is ever recorded.
        try:
            event = json.loads(raw_body)
        except (ValueError, RecursionError):
            return 400, {"error": "body is not valid JSON"}
        if not isinstance(event, dict):
            return 400, {"error": "body must be a JSON object"}
        event_id = event.get("id")
        etype = event.get("type")
        if not _nonempty_str(event_id) or not _nonempty_str(etype):
            return 400, {"error": "missing or invalid id/type"}

        sha = hashlib.sha256(raw_body).hexdigest()
        conflict = False
        with self._store.transaction() as txn:
            existing = txn.get("events", event_id)
            if existing is not None:
                if existing.get("sha256") == sha:
                    return 200, {"status": "duplicate", "event_id": event_id,
                                 "original_status": existing.get("status")}
                conflict = True
            else:
                # An over-refund (422) leaves no event row; remember its hash so that reusing
                # the id with a different body is still caught as an incident.
                prior = txn.get("over_refunds", event_id)
                if prior is not None and prior.get("sha256") != sha:
                    conflict = True
                else:
                    result = self._process(txn, event_id, etype, sha, event)
        if conflict:
            self._record_incident(event_id, sha)
            return 409, {"error": "event id reused with a different body", "event_id": event_id}
        code, body = result
        if code == 400:
            self._record_rejected(event_id, sha, body.get("error"))
        elif code == 422:
            self._record_over_refund(event_id, sha, event, body)
        return code, body

    def _check_signature(self, raw_body, headers):
        """None if the delivery is authentic and fresh, otherwise a short reason."""
        value = None
        for name, val in (headers or {}).items():
            if isinstance(name, str) and name.lower() == SIGNATURE_HEADER:
                value = val
                break
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if not isinstance(value, str):
            return "missing signature"
        timestamps, signatures = [], []
        for part in value.split(","):
            name, sep, val = part.strip().partition("=")
            if not sep:
                continue
            if name == "t":
                timestamps.append(val)
            elif name == "v1":
                signatures.append(val)
        if len(timestamps) != 1 or not signatures:
            return "malformed signature header"
        t_text = timestamps[0]
        if not _TIMESTAMP_RE.fullmatch(t_text):
            return "malformed signature timestamp"
        expected = hmac.new(self._secret, t_text.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for sig in signatures:
            if hmac.compare_digest(expected, sig.encode("utf-8", "replace")):
                ok = True
        if not ok:
            return "signature mismatch"
        if abs(self._clock.now() - int(t_text)) > TOLERANCE_SECONDS:
            return "timestamp outside tolerance"
        return None

    @staticmethod
    def _validate(etype, event):
        """Error string if a payment/refund event is malformed, else None."""
        if not _is_int(event.get("created")):
            return "created must be an integer"
        data = event.get("data")
        if not isinstance(data, dict):
            return "data must be an object"
        if not _nonempty_str(data.get("account_id")):
            return "account_id must be a non-empty string"
        if not _nonempty_str(data.get("payment_id")):
            return "payment_id must be a non-empty string"
        if not _is_int(data.get("amount")) or data["amount"] <= 0:
            return "amount must be a positive integer"
        currency = data.get("currency")
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            return "currency must be a 3-letter uppercase code"
        return None

    def _process(self, txn, event_id, etype, sha, event):
        if etype not in ("payment.succeeded", "refund.succeeded"):
            # Not handled yet, but keep the raw event so it can be replayed later.
            self._record_event(txn, event_id, sha, event, "ignored")
            return 200, {"status": "ignored", "event_id": event_id}
        problem = self._validate(etype, event)
        if problem:
            return 400, {"error": problem}
        if etype == "payment.succeeded":
            return self._payment(txn, event_id, sha, event)
        return self._refund(txn, event_id, sha, event)

    # ------------------------------------------------------------------ business rules

    def _payment(self, txn, event_id, sha, event):
        data = event["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        amount, currency = data["amount"], data["currency"]

        if txn.get("payments", payment_id) is not None:
            return self._quarantine(txn, event_id, sha, event,
                                    "payment_id already used by another event")
        account = txn.get("accounts", account_id)
        if account is None:
            account = {"currency": currency, "balance": 0, "seq": 0}
        elif account["currency"] != currency:
            return self._quarantine(txn, event_id, sha, event,
                                    "currency differs from the account's currency")

        self._add_entry(txn, account_id, account, event_id, event["type"], amount, event)
        payment = {"account_id": account_id, "amount": amount, "currency": currency,
                   "event_id": event_id, "refunded_total": 0, "refund_event_ids": []}

        # Refunds may have arrived before this payment; settle them together with the payment,
        # in arrival order. One that does not match the payment's account/currency is
        # quarantined; one that would push the total refunded past the payment amount is
        # rejected (account untouched, recorded in `over_refunds` for finance). PayCo already got
        # a 200 for these, so this delivery's response is unaffected.
        pending = txn.get("pending_refunds", payment_id)
        if pending is not None:
            txn.delete("pending_refunds", payment_id)
            for pend in pending["refunds"]:
                refund_event = txn.get("events", pend["event_id"])
                if pend["account_id"] != account_id or pend["currency"] != currency:
                    refund_event["status"] = "quarantined"
                    refund_event["reason"] = "refund does not match its payment"
                    txn.put("quarantine", pend["event_id"],
                            {"event_id": pend["event_id"], "reason": refund_event["reason"]})
                elif payment["refunded_total"] + pend["amount"] > amount:
                    refund_event["status"] = "rejected"
                    refund_event["reason"] = "refunds would exceed the payment amount"
                    txn.put("over_refunds", pend["event_id"],
                            {"event_id": pend["event_id"], "sha256": refund_event["sha256"],
                             "payment_id": payment_id, "amount": pend["amount"],
                             "payment_amount": amount,
                             "already_refunded": payment["refunded_total"],
                             "reason": refund_event["reason"]})
                else:
                    self._add_entry(txn, account_id, account, pend["event_id"],
                                    "refund.succeeded", -pend["amount"],
                                    {"created": pend["created"],
                                     "data": {"payment_id": payment_id, "currency": currency}})
                    payment["refunded_total"] += pend["amount"]
                    payment["refund_event_ids"].append(pend["event_id"])
                    refund_event["status"] = "applied"
                txn.put("events", pend["event_id"], refund_event)

        txn.put("accounts", account_id, account)
        txn.put("payments", payment_id, payment)
        self._record_event(txn, event_id, sha, event, "applied")
        return 200, {"status": "applied", "event_id": event_id}

    def _refund(self, txn, event_id, sha, event):
        data = event["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        amount, currency = data["amount"], data["currency"]

        payment = txn.get("payments", payment_id)
        if payment is None:
            # Out of order: hold it until the payment arrives. No balance/entry change yet.
            # A payment can have several refunds, so keep a list per payment.
            pending = txn.get("pending_refunds", payment_id) or {"refunds": []}
            pending["refunds"].append(
                {"event_id": event_id, "account_id": account_id, "amount": amount,
                 "currency": currency, "created": event["created"]})
            txn.put("pending_refunds", payment_id, pending)
            self._record_event(txn, event_id, sha, event, "pending")
            return 200, {"status": "pending", "event_id": event_id}

        if payment["account_id"] != account_id or payment["currency"] != currency:
            return self._quarantine(txn, event_id, sha, event,
                                    "refund does not match its payment")
        already = payment["refunded_total"]
        if already + amount > payment["amount"]:
            # Nothing has been written in this transaction: the account stays untouched.
            return 422, {"error": "refunds would exceed the payment amount",
                         "event_id": event_id, "payment_id": payment_id,
                         "payment_amount": payment["amount"], "already_refunded": already}
        account = txn.get("accounts", account_id)
        self._add_entry(txn, account_id, account, event_id, event["type"], -amount, event)
        payment["refunded_total"] = already + amount
        payment["refund_event_ids"].append(event_id)
        txn.put("accounts", account_id, account)
        txn.put("payments", payment_id, payment)
        self._record_event(txn, event_id, sha, event, "applied")
        return 200, {"status": "applied", "event_id": event_id}

    # ------------------------------------------------------------------ storage helpers

    @staticmethod
    def _add_entry(txn, account_id, account, event_id, etype, signed_amount, event):
        account["seq"] += 1
        account["balance"] += signed_amount
        data = event.get("data") or {}
        txn.put("entries", "%s/%012d" % (account_id, account["seq"]), {
            "account_id": account_id,
            "seq": account["seq"],
            "event_id": event_id,
            "type": etype,
            "amount": signed_amount,
            "payment_id": data.get("payment_id"),
            "currency": data.get("currency"),
            "created": event.get("created"),
        })

    def _record_event(self, txn, event_id, sha, event, status, reason=None):
        txn.put("events", event_id, {
            "sha256": sha, "type": event.get("type"), "created": event.get("created"),
            "received_at": self._clock.now(), "status": status, "reason": reason,
            "event": event,
        })

    def _quarantine(self, txn, event_id, sha, event, reason):
        """Signed, well-formed, but cannot be applied (and a retry would not change that): keep it
        for manual review and acknowledge it so PayCo stops retrying."""
        self._record_event(txn, event_id, sha, event, "quarantined", reason)
        txn.put("quarantine", event_id, {"event_id": event_id, "reason": reason, "event": event})
        return 200, {"status": "quarantined", "event_id": event_id, "reason": reason}

    def _record_incident(self, event_id, sha):
        """Best effort: the 409 is returned whether or not this write succeeds."""
        try:
            with self._store.transaction() as txn:
                txn.put("incidents", "%s/%s" % (event_id, sha),
                        {"event_id": event_id, "conflicting_sha256": sha,
                         "detected_at": self._clock.now()})
        except StoreError:
            pass

    def _record_over_refund(self, event_id, sha, event, body):
        """Best effort, for finance: the 422 is returned whether or not this write succeeds. The
        event is deliberately NOT put in `events`, so it is never reported as a duplicate 200."""
        try:
            with self._store.transaction() as txn:
                txn.put("over_refunds", event_id,
                        {"event_id": event_id, "sha256": sha, "event": event,
                         "payment_id": body.get("payment_id"),
                         "payment_amount": body.get("payment_amount"),
                         "already_refunded": body.get("already_refunded"),
                         "reason": body.get("error"), "received_at": self._clock.now()})
        except StoreError:
            pass

    def _record_rejected(self, event_id, sha, reason):
        try:
            with self._store.transaction() as txn:
                txn.put("rejected", sha, {"event_id": event_id, "reason": reason,
                                          "received_at": self._clock.now()})
        except StoreError:
            pass

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._lock, self._store.transaction() as txn:
            account = txn.get("accounts", account_id)
        return account["balance"] if account else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._lock, self._store.transaction() as txn:
            rows = txn.scan("entries", prefix="%s/" % account_id)
        # The prefix could also match an account whose id itself contains "/"; filter exactly.
        return [row for _, row in rows if row.get("account_id") == account_id]
