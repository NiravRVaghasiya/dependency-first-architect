"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import hashlib
import hmac
import json
import logging
from urllib.parse import quote

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300

T_EVENTS = "events"          # event_id -> {sha256, type, status, [raw]}
T_ACCOUNTS = "accounts"      # account_id -> {balance, currency, seq}
T_ENTRIES = "entries"        # quote(account_id)/<seq> -> entry dict
T_PAYMENTS = "payments"      # payment_id -> {event_id, account_id, amount, currency, refunded_total}
T_PENDING = "pending_refunds"  # quote(payment_id)/quote(event_id) -> refund that arrived before its payment
T_INCIDENTS = "incidents"    # best-effort audit records

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"


def _resp(status, **body):
    return status, body


def _entry_prefix(account_id):
    return quote(account_id, safe="") + "/"


def _entry_key(account_id, seq):
    return "%s%012d" % (_entry_prefix(account_id), seq)


def _pending_prefix(payment_id):
    return quote(payment_id, safe="") + "/"


def _pending_key(payment_id, event_id):
    return _pending_prefix(payment_id) + quote(event_id, safe="")


def _pending_order(pending):
    created = pending["event"].get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        created = 0
    return (created, pending["event_id"])


def _refunded_total(payment):
    """Total refunded so far. Older records stored a boolean 'refunded' (full refund)."""
    if "refunded_total" in payment:
        return payment["refunded_total"]
    return payment["amount"] if payment.get("refunded") else 0


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
            return _resp(400, error="bad_body")
        raw_body = bytes(raw_body)

        err = self._check_signature(raw_body, headers)
        if err is not None:
            return err

        event = self._parse(raw_body)
        if event is None:
            return _resp(400, error="malformed_event")
        if isinstance(event, tuple):
            return event

        digest = hashlib.sha256(raw_body).hexdigest()
        try:
            with self._store.transaction() as txn:
                result = self._process(txn, event, digest, raw_body)
        except StoreError:
            # The transaction was rolled back as a whole; a non-2xx makes PayCo retry.
            log.warning("store error while handling event %s", event.get("id"))
            return _resp(500, error="store_error")
        if result[0] == 409 and result[1].get("error") == "event_id_conflict":
            self._record_incident(event["id"], digest)
        return result

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            acct = txn.get(T_ACCOUNTS, account_id)
        return acct["balance"] if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._store.transaction() as txn:
            rows = txn.scan(T_ENTRIES, prefix=_entry_prefix(account_id))
        return [value for _key, value in rows]

    # ------------------------------------------------------------------ verification

    def _header(self, headers, name):
        name = name.lower()
        for k, v in (headers or {}).items():
            if isinstance(k, str) and k.lower() == name:
                return v
        return None

    def _check_signature(self, raw_body, headers):
        value = self._header(headers, "PayCo-Signature")
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if not isinstance(value, str):
            return _resp(401, error="missing_signature")
        t_str = None
        sigs = []
        for part in value.split(","):
            k, sep, v = part.partition("=")
            if not sep:
                continue
            k, v = k.strip(), v.strip()
            if k == "t":
                t_str = v
            elif k == "v1":
                sigs.append(v.lower())
        if t_str is None or not sigs:
            return _resp(401, error="invalid_signature")
        try:
            t = int(t_str)
        except ValueError:
            return _resp(401, error="invalid_signature")
        expected = hmac.new(self._secret, t_str.encode("ascii", "replace") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for s in sigs:
            if hmac.compare_digest(expected, s.encode("utf-8")):
                ok = True
        if not ok:
            return _resp(401, error="invalid_signature")
        if abs(self._clock.now() - t) > TOLERANCE_SECONDS:
            return _resp(401, error="stale_timestamp")
        return None

    def _parse(self, raw_body):
        """Returns the event dict, None if malformed, or an error response tuple."""
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict):
            return None
        eid, etype = event.get("id"), event.get("type")
        if not isinstance(eid, str) or not eid or not isinstance(etype, str):
            return None
        if etype in (PAYMENT, REFUND):
            data = event.get("data")
            if not isinstance(data, dict):
                return None
            for f in ("account_id", "payment_id", "currency"):
                if not isinstance(data.get(f), str) or not data[f]:
                    return None
            amt = data.get("amount")
            if not isinstance(amt, int) or isinstance(amt, bool) or amt <= 0:
                return None
        return event

    # ------------------------------------------------------------------ processing

    def _process(self, txn, event, digest, raw_body):
        eid, etype = event["id"], event["type"]

        existing = txn.get(T_EVENTS, eid)
        if existing is not None:
            if existing["sha256"] != digest:
                return _resp(409, error="event_id_conflict")
            return _resp(200, status="duplicate", event_status=existing["status"])

        if etype not in (PAYMENT, REFUND):
            # Not handled yet. Acknowledge (so PayCo stops retrying) but keep the verified raw
            # event so it can be replayed once the type is supported.
            txn.put(T_EVENTS, eid, {"sha256": digest, "type": etype, "status": "unhandled",
                                    "raw": raw_body.decode("utf-8")})
            return _resp(200, status="ignored", reason="unhandled_type")

        data = event["data"]
        if etype == PAYMENT:
            return self._payment(txn, event, data, digest)
        return self._refund(txn, event, data, digest)

    def _append(self, txn, account_id, currency, signed_amount, event, data):
        acct = txn.get(T_ACCOUNTS, account_id) or {"balance": 0, "currency": currency, "seq": 0}
        acct["seq"] += 1
        acct["balance"] += signed_amount
        txn.put(T_ENTRIES, _entry_key(account_id, acct["seq"]), {
            "event_id": event["id"],
            "type": event["type"],
            "amount": signed_amount,
            "account_id": account_id,
            "payment_id": data["payment_id"],
            "currency": currency,
            "created": event.get("created"),
        })
        txn.put(T_ACCOUNTS, account_id, acct)

    def _payment(self, txn, event, data, digest):
        account_id, pid = data["account_id"], data["payment_id"]
        if txn.get(T_PAYMENTS, pid) is not None:
            # Same payment under a different event id: never credit twice.
            return _resp(409, error="duplicate_payment")
        acct = txn.get(T_ACCOUNTS, account_id)
        if acct is not None and acct["currency"] != data["currency"]:
            return _resp(422, error="currency_mismatch")

        self._append(txn, account_id, data["currency"], data["amount"], event, data)
        payment = {"event_id": event["id"], "account_id": account_id, "amount": data["amount"],
                   "currency": data["currency"], "refunded_total": 0}
        txn.put(T_EVENTS, event["id"], {"sha256": digest, "type": PAYMENT, "status": "applied"})

        # Apply refunds that were parked because they arrived before this payment, in a
        # deterministic order (created, then event id) so the outcome does not depend on
        # arrival order. Each is checked against the cap exactly like a live refund.
        applied, rejected = [], []
        prefix = _pending_prefix(pid)
        rows = txn.scan(T_PENDING, prefix=prefix)
        rows.sort(key=lambda kv: _pending_order(kv[1]))
        for key, pending in rows:
            txn.delete(T_PENDING, key)
            ref_rec = txn.get(T_EVENTS, pending["event_id"])
            if (pending["account_id"] != account_id or pending["currency"] != data["currency"]):
                new_status, kind = "rejected_mismatch", "pending_refund_mismatch"
            elif payment["refunded_total"] + pending["amount"] > payment["amount"]:
                new_status, kind = "rejected_exceeds_payment", "pending_refund_exceeds_payment"
            else:
                self._append(txn, account_id, data["currency"], -pending["amount"],
                             pending["event"], pending["event"]["data"])
                payment["refunded_total"] += pending["amount"]
                ref_rec["status"] = "applied"
                txn.put(T_EVENTS, pending["event_id"], ref_rec)
                applied.append(pending["event_id"])
                continue
            ref_rec["status"] = new_status
            txn.put(T_EVENTS, pending["event_id"], ref_rec)
            self._incident_in_txn(txn, pending["event_id"], kind)
            rejected.append(pending["event_id"])
        txn.put(T_PAYMENTS, pid, payment)

        status = "applied"
        if applied:
            status = "applied_with_pending_refund"
        elif rejected:
            status = "applied_refund_rejected"
        return _resp(200, status=status, applied_refunds=applied, rejected_refunds=rejected)

    def _refund(self, txn, event, data, digest):
        account_id, pid = data["account_id"], data["payment_id"]
        payment = txn.get(T_PAYMENTS, pid)
        if payment is None:
            # Refund before its payment: park it, apply when the payment arrives. The cap
            # cannot be checked until the payment's amount is known.
            txn.put(T_PENDING, _pending_key(pid, event["id"]),
                    {"event_id": event["id"], "account_id": account_id,
                     "amount": data["amount"], "currency": data["currency"], "event": event})
            txn.put(T_EVENTS, event["id"], {"sha256": digest, "type": REFUND,
                                            "status": "pending"})
            return _resp(200, status="pending", reason="payment_not_seen_yet")
        if payment["account_id"] != account_id or payment["currency"] != data["currency"]:
            return _resp(422, error="refund_mismatch")
        already = _refunded_total(payment)
        if already + data["amount"] > payment["amount"]:
            # Over-refund: reject, write nothing (finance investigates). The event is not
            # recorded, so PayCo's retries get the same answer.
            log.error("refund %s would exceed payment %s (%d + %d > %d)", event["id"], pid,
                      already, data["amount"], payment["amount"])
            return _resp(422, error="refund_exceeds_payment")
        self._append(txn, account_id, payment["currency"], -data["amount"], event, data)
        payment["refunded_total"] = already + data["amount"]
        payment.pop("refunded", None)
        txn.put(T_PAYMENTS, pid, payment)
        txn.put(T_EVENTS, event["id"], {"sha256": digest, "type": REFUND, "status": "applied"})
        return _resp(200, status="applied")

    # ------------------------------------------------------------------ incidents

    def _incident_in_txn(self, txn, event_id, kind):
        txn.put(T_INCIDENTS, "%s/%s" % (event_id, kind), {"event_id": event_id, "kind": kind})

    def _record_incident(self, event_id, digest):
        # Log only: a rejected delivery performs no database writes at all.
        log.error("security incident: event %s redelivered with a different body (sha256 %s)",
                  event_id, digest)
