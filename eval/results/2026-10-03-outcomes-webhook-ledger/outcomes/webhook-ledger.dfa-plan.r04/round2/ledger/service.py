"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Every delivery is processed inside ONE store transaction, so an event's record and all of its
effects commit together or not at all. A StoreError (or any other exception) rolls everything
back and the caller gets 503/500, so PayCo retries and the retry starts from a clean slate.

Tables:
  events      event_id   -> {hash, type, status, reason?, body}
  payments    payment_id -> {event_id, account_id, amount, currency, refunded_total}
  accounts    account_id -> {currency, next_seq}
  entries     "<quoted account_id>/<seq>" -> entry dict (append-only)
  parked      payment_id -> {events: [refunds that arrived before their payment, in arrival order]}
"""

import hashlib
import hmac
import json
import logging
import re
from urllib.parse import quote

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 1024 * 1024

_DIGITS = re.compile(r"[0-9]{1,15}")
_CURRENCY = re.compile(r"[A-Z]{3}")

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"


class _Reject(Exception):
    """Delivery is rejected with this status/body; the transaction is rolled back."""

    def __init__(self, status, body):
        super().__init__(status)
        self.status = status
        self.body = body


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def _entry_prefix(account_id):
    return quote(account_id, safe="") + "/"


def _reject_constant(name):
    raise ValueError("invalid JSON constant " + name)


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = bytes(secret)
        self._clock = clock

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        try:
            if not isinstance(raw_body, (bytes, bytearray)):
                return 400, {"error": "malformed_body"}
            raw_body = bytes(raw_body)
            if len(raw_body) > MAX_BODY_BYTES:
                return 400, {"error": "body_too_large"}

            # 1. Authenticate before looking at the content.
            if not self._signature_ok(raw_body, headers):
                return 400, {"error": "bad_signature"}

            # 2. Parse and validate.
            event = self._parse(raw_body)
            if event is None:
                return 400, {"error": "malformed_body"}

            # 3. Apply atomically.
            digest = hashlib.sha256(raw_body).hexdigest()
            with self._store.transaction() as txn:
                result = self._apply(txn, event, digest, raw_body)
            return 200, result
        except _Reject as r:
            if r.status == 409:
                log.error("event id conflict: same id, different body: %s", r.body.get("event_id"))
            if r.status == 422:
                log.error("refund exceeds payment amount, rejected: %s", r.body.get("event_id"))
            return r.status, r.body
        except StoreError:
            log.warning("store write failed; asking PayCo to retry")
            return 503, {"error": "store_unavailable"}
        except Exception:
            log.exception("unexpected error while handling delivery")
            return 500, {"error": "internal_error"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        return sum(e["amount"] for e in self.entries(account_id))

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        if not isinstance(account_id, str):
            return []
        with self._store.transaction() as txn:
            rows = txn.scan("entries", prefix=_entry_prefix(account_id))
        # Keys end in a zero-padded sequence, so key order is application order.
        return [value for _key, value in rows]

    # ------------------------------------------------------------------ signature

    def _signature_ok(self, raw_body, headers):
        if not isinstance(headers, dict):
            return False
        value = None
        for name, v in headers.items():
            if isinstance(name, str) and name.lower() == "payco-signature":
                value = v
                break
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode("ascii")
            except UnicodeDecodeError:
                return False
        if not isinstance(value, str):
            return False

        timestamps, sigs = [], []
        for part in value.split(","):
            key, sep, val = part.strip().partition("=")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if key == "t":
                timestamps.append(val)
            elif key == "v1":
                sigs.append(val)
        if len(timestamps) != 1 or not sigs:
            return False
        t = timestamps[0]
        if not _DIGITS.fullmatch(t):
            return False
        if abs(self._clock.now() - int(t)) > TOLERANCE_SECONDS:
            return False

        expected = hmac.new(self._secret, t.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for sig in sigs:
            try:
                candidate = sig.encode("ascii")
            except UnicodeEncodeError:
                continue
            if hmac.compare_digest(candidate, expected):
                ok = True
        return ok

    # ------------------------------------------------------------------ parsing

    def _parse(self, raw_body):
        """Returns a normalized event dict, or None if the body is malformed."""
        try:
            doc = json.loads(raw_body.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return None
        if not isinstance(doc, dict):
            return None
        event_id, etype = doc.get("id"), doc.get("type")
        if not _nonempty_str(event_id) or not _nonempty_str(etype):
            return None
        event = {"id": event_id, "type": etype}
        if etype in (PAYMENT, REFUND):
            data = doc.get("data")
            created = doc.get("created")
            if not isinstance(data, dict) or not _is_int(created):
                return None
            account_id, payment_id = data.get("account_id"), data.get("payment_id")
            amount, currency = data.get("amount"), data.get("currency")
            if not _nonempty_str(account_id) or not _nonempty_str(payment_id):
                return None
            if not _is_int(amount) or amount <= 0:
                return None
            if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
                return None
            event.update(created=created, account_id=account_id, payment_id=payment_id,
                         amount=amount, currency=currency)
        return event

    # ------------------------------------------------------------------ applying

    def _apply(self, txn, event, digest, raw_body):
        eid = event["id"]
        existing = txn.get("events", eid)
        if existing is not None:
            if existing["hash"] != digest:
                raise _Reject(409, {"error": "event_conflict", "event_id": eid})
            return {"status": "duplicate", "event_id": eid}

        record = {"hash": digest, "type": event["type"], "status": None,
                  "body": raw_body.decode("utf-8")}

        if event["type"] == PAYMENT:
            status = self._apply_payment(txn, event, record)
        elif event["type"] == REFUND:
            status = self._apply_refund(txn, event, record)
        else:
            status = "ignored"
        record["status"] = status
        txn.put("events", eid, record)
        result = {"status": status, "event_id": eid}
        if "reason" in record:
            result["reason"] = record["reason"]
        return result

    def _quarantine(self, record, reason):
        record["reason"] = reason
        return "quarantined"

    def _append_entry(self, txn, event, sign, source_type):
        account = txn.get("accounts", event["account_id"])
        seq = account["next_seq"]
        account["next_seq"] = seq + 1
        txn.put("accounts", event["account_id"], account)
        txn.put("entries", _entry_prefix(event["account_id"]) + "%012d" % seq, {
            "event_id": event["id"],
            "type": source_type,
            "amount": sign * event["amount"],
            "account_id": event["account_id"],
            "payment_id": event["payment_id"],
            "currency": event["currency"],
            "created": event["created"],
            "seq": seq,
        })

    def _apply_payment(self, txn, event, record):
        pid, acct_id = event["payment_id"], event["account_id"]
        if txn.get("payments", pid) is not None:
            return self._quarantine(record, "payment_id already recorded under another event")
        account = txn.get("accounts", acct_id)
        if account is not None and account["currency"] != event["currency"]:
            return self._quarantine(record, "currency differs from account currency")
        if account is None:
            txn.put("accounts", acct_id, {"currency": event["currency"], "next_seq": 0})

        txn.put("payments", pid, {"event_id": event["id"], "account_id": acct_id,
                                  "amount": event["amount"], "currency": event["currency"],
                                  "refunded_total": 0})
        self._append_entry(txn, event, +1, PAYMENT)

        parked = txn.get("parked", pid)
        if parked is not None:
            txn.delete("parked", pid)
            self._resolve_parked(txn, parked)
        return "applied"

    def _resolve_parked(self, txn, parked):
        """Refunds that arrived first now meet their payment, in arrival order. Each is applied if
        it fits; one that breaks a rule, or would push the total refunded past the payment's
        amount, is quarantined (its delivery was already answered 200, so it cannot be 422'd now)."""
        if "events" in parked:
            refunds = parked["events"]
        else:  # legacy single-refund shape
            refunds = [parked["event"]]
        for refund in refunds:
            pid = refund["payment_id"]
            payment = txn.get("payments", pid)
            reason = self._refund_problem(payment, refund)
            if reason is None and self._refunded_total(payment) + refund["amount"] > payment["amount"]:
                reason = "refund would exceed payment amount"
            rec = txn.get("events", refund["id"])
            if reason is None:
                self._append_entry(txn, refund, -1, REFUND)
                payment["refunded_total"] = self._refunded_total(payment) + refund["amount"]
                txn.put("payments", pid, payment)
                rec["status"] = "applied"
            else:
                rec["status"] = "quarantined"
                rec["reason"] = reason
            txn.put("events", refund["id"], rec)

    @staticmethod
    def _refunded_total(payment):
        if "refunded_total" in payment:
            return payment["refunded_total"]
        return payment["amount"] if payment.get("refunded") else 0  # legacy full-refund flag

    @staticmethod
    def _refund_problem(payment, refund):
        if payment["account_id"] != refund["account_id"]:
            return "refund account differs from payment account"
        if payment["currency"] != refund["currency"]:
            return "refund currency differs from payment currency"
        return None

    def _apply_refund(self, txn, event, record):
        pid = event["payment_id"]
        payment = txn.get("payments", pid)
        if payment is None:
            parked = txn.get("parked", pid)
            if parked is None:
                events = []
            elif "events" in parked:
                events = parked["events"]
            else:
                events = [parked["event"]]
            events.append(event)
            txn.put("parked", pid, {"events": events})
            return "parked"
        reason = self._refund_problem(payment, event)
        if reason is not None:
            return self._quarantine(record, reason)
        total = self._refunded_total(payment)
        if total + event["amount"] > payment["amount"]:
            # Over-refund: reject the delivery; the raised exception rolls the transaction back,
            # so nothing (not even the event record) is written. Finance investigates.
            raise _Reject(422, {"error": "refund_exceeds_payment", "event_id": event["id"],
                                "payment_id": pid})
        self._append_entry(txn, event, -1, REFUND)
        payment["refunded_total"] = total + event["amount"]
        txn.put("payments", pid, payment)
        return "applied"
