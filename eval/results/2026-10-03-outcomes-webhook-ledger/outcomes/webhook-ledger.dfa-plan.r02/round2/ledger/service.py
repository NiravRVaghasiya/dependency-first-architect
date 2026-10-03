"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import base64
import hashlib
import hmac
import json
import logging
import re
from urllib.parse import quote

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300
PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"
KNOWN_TYPES = (PAYMENT, REFUND)

_T_RE = re.compile(r"[0-9]{1,15}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

# Tables
EVENTS = "events"            # event_id -> fingerprint, state, raw body
ACCOUNTS = "accounts"        # quoted account_id -> currency, next_seq
ENTRIES = "entries"          # quoted account_id/seq -> entry (append-only)
PAYMENTS = "payments"        # payment_id -> credited payment, total refunded so far
PENDING = "pending_refunds"  # payment_id -> refunds (in arrival order) that arrived before it
QUARANTINE = "quarantine"    # event_id -> reason (not applied; for Finance)
REJECTED = "rejected"        # event_id -> over-refund (422, not applied; for Finance)


def _akey(account_id):
    # No "/" survives quoting, so one account's prefix can never match another's entries.
    return quote(account_id, safe="")


def _parse_signature(value):
    """-> (t_string, [v1 hex strings]) or None if malformed."""
    t = None
    sigs = []
    for part in value.split(","):
        k, _, v = part.strip().partition("=")
        k = k.strip()
        v = v.strip()
        if k == "t":
            if t is not None:
                return None
            t = v
        elif k == "v1":
            sigs.append(v)
    if t is None or not _T_RE.fullmatch(t) or not sigs:
        return None
    return t, sigs


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        self._clock = clock

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        try:
            return self._handle(raw_body, headers)
        except StoreError:
            # The whole event is one transaction, so nothing was saved. PayCo will retry.
            log.warning("ledger: store error, asking PayCo to retry")
            return 503, {"status": "unavailable"}
        except Exception:
            log.exception("ledger: unexpected error")
            return 500, {"status": "error"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        return sum(e["amount"] for e in self._load_entries(account_id))

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        return self._load_entries(account_id)

    # ------------------------------------------------------------------ reads

    def _load_entries(self, account_id):
        if not isinstance(account_id, str):
            return []
        with self._store.transaction() as txn:
            rows = txn.scan(ENTRIES, prefix=_akey(account_id) + "/")
        return [value for _, value in rows]

    # ------------------------------------------------------------------ trust boundary

    def _header(self, headers, name):
        if not isinstance(headers, dict):
            return None
        for k, v in headers.items():
            if isinstance(k, bytes):
                k = k.decode("latin-1")
            if isinstance(k, str) and k.lower() == name:
                if isinstance(v, bytes):
                    v = v.decode("latin-1")
                return v if isinstance(v, str) else None
        return None

    def _verify(self, body, headers):
        """True only for a correctly signed, fresh delivery. Runs before any JSON parsing."""
        header = self._header(headers, "payco-signature")
        if header is None:
            return False
        parsed = _parse_signature(header)
        if parsed is None:
            return False
        t, sigs = parsed
        expected = hmac.new(self._secret, t.encode("ascii") + b"." + body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for s in sigs:
            if hmac.compare_digest(s.encode("utf-8"), expected):
                ok = True
        if not ok:
            return False
        return abs(self._clock.now() - int(t)) <= TOLERANCE_SECONDS

    # ------------------------------------------------------------------ handle

    def _handle(self, raw_body, headers):
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        body = bytes(raw_body)

        if not self._verify(body, headers):
            return 401, {"status": "invalid_signature"}

        try:
            event = json.loads(body)
        except Exception:
            return 400, {"status": "malformed"}
        if (not isinstance(event, dict) or not isinstance(event.get("id"), str)
                or not event["id"] or not isinstance(event.get("type"), str)):
            return 400, {"status": "malformed"}

        fingerprint = hashlib.sha256(body).hexdigest()
        # One transaction per event: all writes commit together or (on StoreError) none do.
        with self._store.transaction() as txn:
            return self._process(txn, event, body, fingerprint)

    def _process(self, txn, event, body, fingerprint):
        eid = event["id"]
        existing = txn.get(EVENTS, eid)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                log.error("ledger: INCIDENT event id %s redelivered with a different body", eid)
                return 409, {"status": "conflict"}
            return self._replay_response(existing)

        etype = event["type"]
        rec = {"fingerprint": fingerprint, "type": etype, "state": "received",
               "raw_b64": base64.b64encode(body).decode("ascii")}

        if etype not in KNOWN_TYPES:
            # Keep the verified raw event so it can be replayed when a handler exists.
            rec["state"] = "ignored"
            txn.put(EVENTS, eid, rec)
            return 200, {"status": "ignored"}

        ev = self._validate(event)
        if ev is None:
            return 400, {"status": "malformed"}

        if etype == PAYMENT:
            return self._payment(txn, ev, rec)
        return self._refund(txn, ev, rec)

    @staticmethod
    def _replay_response(rec):
        state = rec["state"]
        if state == "applied":
            return 200, {"status": "duplicate"}
        if state == "parked":
            return 202, {"status": "parked"}
        if state == "rejected":
            return 422, {"status": "refund_exceeds_payment"}
        return 200, {"status": state}  # quarantined / ignored

    @staticmethod
    def _validate(event):
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        account_id, payment_id = data.get("account_id"), data.get("payment_id")
        amount, currency = data.get("amount"), data.get("currency")
        created = event.get("created")
        if not (isinstance(account_id, str) and account_id):
            return None
        if not (isinstance(payment_id, str) and payment_id):
            return None
        if not _is_int(amount) or amount <= 0:
            return None
        if not (isinstance(currency, str) and _CURRENCY_RE.fullmatch(currency)):
            return None
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            return None
        return {"event_id": event["id"], "type": event["type"], "created": created,
                "account_id": account_id, "payment_id": payment_id,
                "amount": amount, "currency": currency}

    # ------------------------------------------------------------------ handlers

    def _finish(self, txn, ev, rec, state, reason=None):
        rec["state"] = state
        if state == "rejected":
            # Over-refund: nothing touches the account; Finance investigates from this record.
            rec["reason"] = reason
            txn.put(REJECTED, ev["event_id"], {
                "reason": reason, "type": ev["type"], "account_id": ev["account_id"],
                "payment_id": ev["payment_id"], "amount": ev["amount"],
                "currency": ev["currency"]})
            log.error("ledger: rejected event %s (%s)", ev["event_id"], reason)
        elif reason:
            rec["reason"] = reason
            txn.put(QUARANTINE, ev["event_id"], {
                "reason": reason, "type": ev["type"], "account_id": ev["account_id"],
                "payment_id": ev["payment_id"], "amount": ev["amount"],
                "currency": ev["currency"]})
            log.error("ledger: quarantined event %s (%s)", ev["event_id"], reason)
        txn.put(EVENTS, ev["event_id"], rec)

    def _append(self, txn, acct, ev, signed_amount):
        """Append one entry for ev to the account (acct is its record, updated in place)."""
        key = _akey(ev["account_id"])
        seq = acct["next_seq"]
        txn.put(ENTRIES, "%s/%012d" % (key, seq), {
            "event_id": ev["event_id"], "type": ev["type"], "amount": signed_amount,
            "currency": ev["currency"], "created": ev["created"],
            "payment_id": ev["payment_id"], "account_id": ev["account_id"]})
        acct["next_seq"] = seq + 1
        txn.put(ACCOUNTS, key, acct)

    def _payment(self, txn, ev, rec):
        pid = ev["payment_id"]
        if txn.get(PAYMENTS, pid) is not None:
            self._finish(txn, ev, rec, "quarantined", "second_credit_for_payment_id")
            return 200, {"status": "quarantined"}

        key = _akey(ev["account_id"])
        acct = txn.get(ACCOUNTS, key)
        if acct is None:
            acct = {"currency": ev["currency"], "next_seq": 0}
        elif acct["currency"] != ev["currency"]:
            self._finish(txn, ev, rec, "quarantined", "currency_mismatch")
            return 200, {"status": "quarantined"}

        self._append(txn, acct, ev, ev["amount"])
        payment = {"event_id": ev["event_id"], "account_id": ev["account_id"],
                   "amount": ev["amount"], "currency": ev["currency"],
                   "refunded": 0, "refund_event_ids": []}
        self._finish(txn, ev, rec, "applied")

        # Release refunds that arrived before this payment, in arrival order. Each one is
        # checked against the running total exactly like a refund that arrives later.
        pending = txn.get(PENDING, pid)
        if pending is not None:
            txn.delete(PENDING, pid)
            for rev in (pending.get("evs") or [pending["ev"]]):
                rrec = txn.get(EVENTS, rev["event_id"])
                if not self._refund_matches(payment, rev):
                    self._finish(txn, rev, rrec, "quarantined", "refund_mismatch")
                elif self._refunded(payment) + rev["amount"] > payment["amount"]:
                    # Its own delivery already got 202; its retries will get 422.
                    self._finish(txn, rev, rrec, "rejected", "refund_exceeds_payment")
                else:
                    self._apply_refund(txn, acct, payment, rev)
                    self._finish(txn, rev, rrec, "applied")
        txn.put(PAYMENTS, pid, payment)
        return 200, {"status": "applied"}

    @staticmethod
    def _refund_matches(payment, rev):
        # Same account and currency as the payment. The amount may be a part of the payment's.
        return (payment["account_id"] == rev["account_id"]
                and payment["currency"] == rev["currency"])

    @staticmethod
    def _refunded(payment):
        """Total already refunded for the payment (minor units)."""
        if "refunded" in payment:
            return payment["refunded"]
        # Record written before partial refunds: a refund then always meant the full amount.
        return payment["amount"] if payment.get("refund_event_id") else 0

    def _apply_refund(self, txn, acct, payment, rev):
        self._append(txn, acct, rev, -rev["amount"])
        payment["refunded"] = self._refunded(payment) + rev["amount"]
        payment.setdefault("refund_event_ids", []).append(rev["event_id"])

    def _refund(self, txn, ev, rec):
        pid = ev["payment_id"]
        payment = txn.get(PAYMENTS, pid)
        if payment is None:
            # Refund before its payment: park it (several may be parked for one payment);
            # each is checked and applied when the payment arrives.
            pending = txn.get(PENDING, pid)
            evs = (pending.get("evs") or [pending["ev"]]) if pending is not None else []
            evs.append(ev)
            txn.put(PENDING, pid, {"evs": evs})
            self._finish(txn, ev, rec, "parked")
            return 202, {"status": "parked"}

        if not self._refund_matches(payment, ev):
            self._finish(txn, ev, rec, "quarantined", "refund_mismatch")
            return 200, {"status": "quarantined"}

        # The total refunded must never exceed the payment: reject, touch no account data.
        if self._refunded(payment) + ev["amount"] > payment["amount"]:
            self._finish(txn, ev, rec, "rejected", "refund_exceeds_payment")
            return 422, {"status": "refund_exceeds_payment"}

        acct = txn.get(ACCOUNTS, _akey(ev["account_id"]))
        self._apply_refund(txn, acct, payment, ev)
        txn.put(PAYMENTS, pid, payment)
        self._finish(txn, ev, rec, "applied")
        return 200, {"status": "applied"}
