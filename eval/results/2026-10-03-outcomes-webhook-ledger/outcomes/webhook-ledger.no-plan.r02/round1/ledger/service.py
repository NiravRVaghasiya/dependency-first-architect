"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import hashlib
import hmac
import json
import logging
import threading
from urllib.parse import quote

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"

# Tables
EVENTS = "events"            # event id -> {digest, status, code, body}
ACCOUNTS = "accounts"        # account id -> {balance, currency, seq}
ENTRIES = "entries"          # quote(account id) + "/" + zero-padded seq -> entry
PAYMENTS = "payments"        # payment id -> {account_id, amount, currency, event_id, refunded_by}
PENDING = "pending_refunds"  # payment id -> refund that arrived before its payment


class _Reject(Exception):
    """Internal: stop processing with an HTTP status; the event is recorded as rejected."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def _entry_prefix(account_id):
    # quote() escapes "/", so one account's prefix can never match another's keys.
    return quote(account_id, safe="") + "/"


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret
        self._clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ signature

    def _verify(self, raw_body, headers):
        """Return None if authentic and fresh, else an error message."""
        header = None
        for name, value in (headers or {}).items():
            if isinstance(name, str) and name.lower() == "payco-signature":
                header = value
                break
        if isinstance(header, bytes):
            try:
                header = header.decode("ascii")
            except UnicodeDecodeError:
                return "malformed signature header"
        if not isinstance(header, str) or not header:
            return "missing signature"
        t = None
        sigs = []
        for part in header.split(","):
            k, sep, v = part.strip().partition("=")
            if not sep:
                continue
            if k == "t" and t is None:
                t = v
            elif k == "v1":
                sigs.append(v)
        if t is None or not t.isascii() or not t.isdigit() or not sigs:
            return "malformed signature header"
        signed = t.encode("ascii") + b"." + raw_body
        expected = hmac.new(self._secret, signed, hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for s in sigs:
            try:
                sb = s.encode("ascii")
            except UnicodeEncodeError:
                continue
            if hmac.compare_digest(sb, expected):
                ok = True
        if not ok:
            return "signature mismatch"
        if abs(self._clock.now() - int(t)) > TOLERANCE_SECONDS:
            return "timestamp outside tolerance"
        return None

    # ------------------------------------------------------------------ handle

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        if not isinstance(raw_body, (bytes, bytearray)):
            return 400, {"error": "body must be bytes"}
        raw_body = bytes(raw_body)

        err = self._verify(raw_body, headers)
        if err:
            return 401, {"error": err}

        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, {"error": "body is not valid JSON"}
        if not isinstance(event, dict):
            return 400, {"error": "body must be a JSON object"}
        event_id, etype = event.get("id"), event.get("type")
        if not _nonempty_str(event_id) or not _nonempty_str(etype):
            return 400, {"error": "event needs string id and type"}

        if etype in (PAYMENT, REFUND):
            err = self._validate(event)
            if err:
                return 400, {"error": err}

        # Semantic digest: a redelivery has the same content; formatting is irrelevant.
        try:
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return 400, {"error": "unserializable event"}
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        try:
            with self._lock:
                with self._store.transaction() as txn:
                    return self._process(txn, event, digest)
        except StoreError:
            # The transaction rolled back completely; PayCo will retry.
            log.warning("store write failed for event %s; asking PayCo to retry", event_id)
            return 500, {"error": "temporary storage failure, retry"}

    @staticmethod
    def _validate(event):
        if not _is_int(event.get("created")):
            return "created must be an integer"
        data = event.get("data")
        if not isinstance(data, dict):
            return "data must be an object"
        for f in ("account_id", "payment_id", "currency"):
            if not _nonempty_str(data.get(f)):
                return "data.%s must be a non-empty string" % f
        if not _is_int(data.get("amount")) or data["amount"] <= 0:
            return "data.amount must be a positive integer"
        return None

    # ------------------------------------------------------------------ processing

    def _process(self, txn, event, digest):
        event_id, etype = event["id"], event["type"]

        prior = txn.get(EVENTS, event_id)
        if prior is not None:
            if prior["digest"] != digest:
                log.error("INCIDENT: event %s redelivered with a different body", event_id)
                return 409, {"error": "event id reused with a different body"}
            body = dict(prior["body"])
            body["duplicate"] = True
            return prior["code"], body

        if etype not in (PAYMENT, REFUND):
            # Unknown type: acknowledge so PayCo stops retrying; no money moves. The record is
            # marked "ignored" so a future handler can find and backfill these.
            return self._record(txn, event_id, digest, etype, "ignored", 200,
                                {"status": "ignored"})

        try:
            if etype == PAYMENT:
                status = self._payment(txn, event)
            else:
                status = self._refund(txn, event)
        except _Reject as r:
            # Discard any partial writes from this attempt by only recording after a clean
            # rejection point (rejections are raised before any write).
            return self._record(txn, event_id, digest, etype, "rejected", r.code,
                                {"error": r.message})
        return self._record(txn, event_id, digest, etype, status, 200, {"status": status})

    @staticmethod
    def _record(txn, event_id, digest, etype, status, code, body):
        txn.put(EVENTS, event_id, {"digest": digest, "type": etype, "status": status,
                                   "code": code, "body": body})
        return code, dict(body)

    @staticmethod
    def _post(txn, account, account_id, event, signed_amount):
        """Append an entry and move the balance (caller puts the account)."""
        account["seq"] += 1
        account["balance"] += signed_amount
        data = event["data"]
        txn.put(ENTRIES, _entry_prefix(account_id) + "%012d" % account["seq"], {
            "event_id": event["id"],
            "type": event["type"],
            "amount": signed_amount,
            "account_id": account_id,
            "payment_id": data["payment_id"],
            "currency": data["currency"],
            "created": event["created"],
        })

    def _payment(self, txn, event):
        data = event["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        if txn.get(PAYMENTS, payment_id) is not None:
            raise _Reject(409, "payment_id already credited by another event")
        account = txn.get(ACCOUNTS, account_id)
        if account is None:
            account = {"balance": 0, "currency": data["currency"], "seq": 0}
        elif account["currency"] != data["currency"]:
            raise _Reject(422, "currency does not match the account currency")

        self._post(txn, account, account_id, event, data["amount"])
        payment = {"account_id": account_id, "amount": data["amount"],
                   "currency": data["currency"], "event_id": event["id"], "refunded_by": None}

        status = "applied"
        pending = txn.get(PENDING, payment_id)
        if pending is not None:
            # The refund arrived first and was held; apply it now, right after the payment.
            refund_event = pending["event"]
            if self._refund_matches(payment, refund_event["data"]):
                self._post(txn, account, account_id, refund_event, -payment["amount"])
                payment["refunded_by"] = refund_event["id"]
                self._set_status(txn, refund_event["id"], "applied")
                status = "applied_with_pending_refund"
            else:
                self._set_status(txn, refund_event["id"], "rejected")
                log.error("held refund %s does not match payment %s; not applied",
                          refund_event["id"], payment_id)
            txn.delete(PENDING, payment_id)

        txn.put(PAYMENTS, payment_id, payment)
        txn.put(ACCOUNTS, account_id, account)
        return status

    def _refund(self, txn, event):
        data = event["data"]
        account_id, payment_id = data["account_id"], data["payment_id"]
        payment = txn.get(PAYMENTS, payment_id)
        if payment is None:
            held = txn.get(PENDING, payment_id)
            if held is not None:
                raise _Reject(409, "another refund for this payment is already held")
            # Hold it; it is applied when the payment arrives. Nothing moves until then.
            txn.put(PENDING, payment_id, {"event": event})
            return "pending"
        if not self._refund_matches(payment, data):
            raise _Reject(422, "refund does not match the payment (account/amount/currency)")
        if payment["refunded_by"] is not None:
            raise _Reject(409, "payment already refunded")
        account = txn.get(ACCOUNTS, account_id)
        self._post(txn, account, account_id, event, -payment["amount"])
        payment["refunded_by"] = event["id"]
        txn.put(PAYMENTS, payment_id, payment)
        txn.put(ACCOUNTS, account_id, account)
        return "applied"

    @staticmethod
    def _refund_matches(payment, data):
        return (data["account_id"] == payment["account_id"]
                and data["amount"] == payment["amount"]
                and data["currency"] == payment["currency"])

    @staticmethod
    def _set_status(txn, event_id, status):
        rec = txn.get(EVENTS, event_id)
        if rec is not None:
            rec["status"] = status
            txn.put(EVENTS, event_id, rec)

    # ------------------------------------------------------------------ queries

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._lock:
            with self._store.transaction() as txn:
                account = txn.get(ACCOUNTS, account_id)
        return account["balance"] if account else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._lock:
            with self._store.transaction() as txn:
                rows = txn.scan(ENTRIES, _entry_prefix(account_id))
        return [v for _, v in rows]
