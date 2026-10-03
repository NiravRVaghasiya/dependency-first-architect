"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import hashlib
import json
import logging
import re
import threading

from ledgerkit import StoreError

from . import signature

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300
PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"
_CURRENCY_RE = re.compile(r"[A-Z]{3}")

# Tables
EVENTS = "events"      # event id -> event record (idempotency + body hash)
ACCOUNTS = "accounts"  # account id -> {currency, balance}
PAYMENTS = "payments"  # payment id -> applied payment (for refund cross-checks)
REFUNDS = "refunds"    # payment id -> {total refunded so far, count, account_id, currency}
ALERTS = "alerts"      # "<event id>/<kind>" -> alert record (for finance / ops)
META = "meta"          # "seq" -> {n}


def _entries_table(account_id):
    # One table per account: avoids key-prefix collisions between account ids.
    return "entries:" + account_id


class _Invalid(Exception):
    pass


def _reject_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _canonical_hash(event):
    canon = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def _refunded_total(row):
    """Total refunded so far for a payment, from its REFUNDS row (None = no refunds yet).
    Rows written before partial refunds hold a single full refund under "amount"."""
    if row is None:
        return 0
    return row["total"] if "total" in row else row["amount"]


def _validate(event):
    """Raise _Invalid unless the event has the fields we need for its type."""
    eid = event.get("id")
    if not isinstance(eid, str) or not eid:
        raise _Invalid("bad id")
    etype = event.get("type")
    if not isinstance(etype, str) or not etype:
        raise _Invalid("bad type")
    if etype not in (PAYMENT, REFUND):
        return  # unknown types are stored verbatim; we don't depend on their shape
    if not _is_int(event.get("created")):
        raise _Invalid("bad created")
    data = event.get("data")
    if not isinstance(data, dict):
        raise _Invalid("bad data")
    acct = data.get("account_id")
    if not isinstance(acct, str) or not acct:
        raise _Invalid("bad account_id")
    pid = data.get("payment_id")
    if not isinstance(pid, str) or not pid:
        raise _Invalid("bad payment_id")
    amount = data.get("amount")
    if not _is_int(amount) or amount <= 0:
        raise _Invalid("bad amount")
    cur = data.get("currency")
    if not isinstance(cur, str) or not _CURRENCY_RE.fullmatch(cur):
        raise _Invalid("bad currency")


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret
        self._clock = clock
        # The store does not allow concurrent/nested transactions on one client.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ webhook

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        event_id = None
        try:
            # 1-3: signature, checked over the raw bytes before any JSON parsing.
            try:
                t, v1s = signature.parse_header(signature.find_header(headers))
            except signature.HeaderError:
                return 400, {"error": "bad signature header"}
            if not isinstance(raw_body, (bytes, bytearray)):
                return 400, {"error": "bad body"}
            raw_body = bytes(raw_body)
            if not signature.verify(self._secret, t, raw_body, v1s):
                return 401, {"error": "unauthorized"}
            # 4: replay window (exactly 300 s is accepted).
            if abs(self._clock.now() - int(t)) > TOLERANCE_SECONDS:
                return 401, {"error": "unauthorized"}

            # 5: parse.
            try:
                event = json.loads(raw_body.decode("utf-8"),
                                   object_pairs_hook=_reject_duplicate_keys)
            except (ValueError, RecursionError):
                event = None
            if not isinstance(event, dict):
                log.error("ALERT: signed delivery with unparseable body")
                return 400, {"error": "invalid event"}
            if isinstance(event.get("id"), str):
                event_id = event["id"]
            body_hash = _canonical_hash(event)

            with self._lock:
                with self._store.transaction() as txn:
                    status, body, alerts = self._process(txn, event, body_hash)
            for alert in alerts:
                log.error("ALERT: %s", alert)
            if status == 409:
                log.critical("SECURITY INCIDENT: event %s redelivered with a different body",
                             event_id)
            elif status == 400:
                log.error("ALERT: signed event %r failed validation: %s", event_id,
                          body.get("detail"))
                body = {"error": "invalid event"}
            return status, body
        except StoreError:
            log.warning("store error while handling event %s", event_id)
            return 503, {"error": "temporarily unavailable"}
        except Exception:
            log.exception("unexpected error handling event %s", event_id)
            return 500, {"error": "internal error"}

    def _process(self, txn, event, body_hash):
        """Runs inside one transaction: everything for the event commits or none of it does.
        Returns (status, body, alerts). Nothing is written before validation/dedup pass."""
        eid = event.get("id")
        if isinstance(eid, str) and eid:
            existing = txn.get(EVENTS, eid)
            if existing is not None:
                if existing.get("body_hash") != body_hash:
                    return 409, {"error": "conflict"}, []
                return 200, {"result": "duplicate"}, []
        try:
            _validate(event)
        except _Invalid as exc:
            return 400, {"error": "invalid event", "detail": str(exc)}, []

        etype = event["type"]
        created = event["created"] if _is_int(event.get("created")) else None
        record = {"body_hash": body_hash, "type": etype, "created": created, "payload": event}

        if etype not in (PAYMENT, REFUND):
            # Stored verbatim so it can be processed once a handler exists.
            seq = self._next_seq(txn)
            record["received_seq"] = seq
            record["status"] = "ignored"
            txn.put(EVENTS, eid, record)
            return 200, {"result": "ignored"}, []

        data = event["data"]
        account_id = data["account_id"]
        payment_id = data["payment_id"]
        amount = data["amount"]
        currency = data["currency"]
        alerts = []
        record["account_id"] = account_id

        account = txn.get(ACCOUNTS, account_id)
        hold_reason = None
        if account is not None and account["currency"] != currency:
            hold_reason = ("currency mismatch: account %s is %s, event %s is %s"
                           % (account_id, account["currency"], eid, currency))

        if hold_reason is not None:
            seq = self._next_seq(txn)
            record["received_seq"] = seq
            record["status"] = "held"
            record["reason"] = hold_reason
            txn.put(EVENTS, eid, record)
            self._alert(txn, eid, "held", hold_reason, alerts)
            return 200, {"result": "held"}, alerts

        payment_row = txn.get(PAYMENTS, payment_id)
        refund_row = txn.get(REFUNDS, payment_id)
        refunded = _refunded_total(refund_row)

        # Partial refunds: the total refunded for a payment may never exceed its amount. If the
        # payment is known and this refund would push the total past it, reject with 422 and write
        # nothing (no event record, no entry, no counters, not even a sequence number). If the
        # payment hasn't arrived yet we can't check, so the refund is applied and the check is
        # made when the payment arrives (below).
        if etype == REFUND and payment_row is not None \
                and refunded + amount > payment_row["amount"]:
            alerts.append("refund %s of %d on payment %s would bring total refunded to %d, over "
                          "the payment amount %d; rejected (422), account %s untouched"
                          % (eid, amount, payment_id, refunded + amount,
                             payment_row["amount"], account_id))
            return 422, {"error": "refund exceeds payment"}, alerts

        seq = self._next_seq(txn)
        record["received_seq"] = seq

        # Cross-check currency/account between a payment and its refunds, whichever came second.
        other = payment_row if etype == REFUND else refund_row
        if other is not None and (other["currency"] != currency
                                  or other["account_id"] != account_id):
            self._alert(txn, eid, "refund_mismatch",
                        "payment %s and its refund(s) disagree on account or currency "
                        "(event %s)" % (payment_id, eid), alerts)
        # A payment arriving after refunds that already exceed it: the money did arrive, so apply
        # it, but tell finance.
        if etype == PAYMENT and payment_row is None and refunded > amount:
            self._alert(txn, eid, "over_refunded",
                        "payment %s (%d) arrived after refunds totalling %d"
                        % (payment_id, amount, refunded), alerts)

        signed = amount if etype == PAYMENT else -amount
        if account is None:
            account = {"currency": currency, "balance": 0}
        account["balance"] += signed

        record["status"] = "applied"
        txn.put(EVENTS, eid, record)
        txn.put(_entries_table(account_id), eid, {
            "event_id": eid, "type": etype, "amount": signed, "currency": currency,
            "account_id": account_id, "payment_id": payment_id, "created": created,
            "seq": seq})
        txn.put(ACCOUNTS, account_id, account)
        if etype == REFUND:
            txn.put(REFUNDS, payment_id, {
                "total": refunded + amount,
                "count": (refund_row.get("count", 1) if refund_row else 0) + 1,
                "last_event_id": eid,
                "currency": refund_row["currency"] if refund_row else currency,
                "account_id": refund_row["account_id"] if refund_row else account_id})
        elif payment_row is None:
            txn.put(PAYMENTS, payment_id, {"event_id": eid, "amount": amount,
                                           "currency": currency, "account_id": account_id})
        return 200, {"result": "applied"}, alerts

    @staticmethod
    def _next_seq(txn):
        row = txn.get(META, "seq")
        n = (row["n"] if row else 0) + 1
        txn.put(META, "seq", {"n": n})
        return n

    @staticmethod
    def _alert(txn, eid, kind, message, alerts):
        txn.put(ALERTS, "%s/%s" % (eid, kind), {"event_id": eid, "kind": kind,
                                                "message": message})
        alerts.append(message)

    # ------------------------------------------------------------------ reads

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
                rows = txn.scan(_entries_table(account_id))
        out = [row for _, row in rows]
        out.sort(key=lambda e: (e["created"], e["seq"]))
        return out
