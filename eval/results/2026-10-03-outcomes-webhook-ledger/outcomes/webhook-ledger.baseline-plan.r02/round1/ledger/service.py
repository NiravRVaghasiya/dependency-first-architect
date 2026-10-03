"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import logging
from urllib.parse import quote

from ledgerkit import StoreError

from . import events, signature

log = logging.getLogger("ledger")

# Tables
EVENTS = "events"            # event_id -> {fingerprint, type, status, reason, first_seen, ...}
ACCOUNTS = "accounts"        # quoted account_id -> {currency, balance, seq}
ENTRIES = "entries"          # "<quoted account>/<seq:012d>" -> entry
PAYMENTS = "payments"        # payment_id -> {account_id, amount, currency, event_id, refunded}
PENDING = "pending_refunds"  # payment_id -> refund waiting for its payment
INCIDENTS = "incidents"      # "<event_id>/<fingerprint>" -> conflicting delivery record


def _akey(account_id):
    return quote(str(account_id), safe="")


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        self._store = store
        self._secret = secret
        self._clock = clock

    # ------------------------------------------------------------------ handle

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        try:
            now = self._clock.now()
            header = signature.find_header(headers, "PayCo-Signature")
            if header is None:
                return 401, {"status": "rejected", "error": "missing signature"}
            if not isinstance(raw_body, (bytes, bytearray)):
                return 400, {"status": "rejected", "error": "invalid body"}
            raw_body = bytes(raw_body)
            if not signature.verify(self._secret, raw_body, header, now):
                return 401, {"status": "rejected", "error": "invalid signature"}
            try:
                event = events.parse_event(raw_body)
            except events.BadBody as exc:
                return 400, {"status": "rejected", "error": str(exc)}

            # One transaction per delivery: the event record, the account and every entry are
            # committed together or not at all, so a StoreError leaves nothing half-applied and
            # PayCo's retry (we answer 500) starts from a clean slate.
            with self._store.transaction() as txn:
                result = self._process(txn, event, events.fingerprint(event), now)
            return result
        except StoreError:
            log.warning("store write failed; asking PayCo to retry")
            return 500, {"status": "error", "error": "storage failure, retry"}
        except Exception:
            log.exception("unexpected error handling webhook")
            return 500, {"status": "error", "error": "internal error, retry"}

    def _process(self, txn, event, fp, now):
        event_id = event["id"]
        existing = txn.get(EVENTS, event_id)
        if existing is not None:
            if existing["fingerprint"] == fp:
                return 200, {"status": "duplicate"}
            log.error("SECURITY INCIDENT: event %s redelivered with a different body", event_id)
            txn.put(INCIDENTS, "%s/%s" % (event_id, fp), {
                "event_id": event_id,
                "original_fingerprint": existing["fingerprint"],
                "conflicting_fingerprint": fp,
                "detected_at": now,
            })
            return 409, {"status": "conflict",
                         "error": "event id reused with a different body"}

        etype = event["type"]
        created = event.get("created")
        if isinstance(created, bool) or not isinstance(created, int):
            created = None
        record = {"fingerprint": fp, "type": etype, "first_seen": now, "created": created,
                  "account_id": event["data"].get("account_id")
                  if isinstance(event["data"].get("account_id"), str) else None}

        if etype == "payment.succeeded":
            status, reason = self._payment(txn, event, created, now)
        elif etype == "refund.succeeded":
            status, reason = self._refund(txn, event, created, now)
        else:
            status, reason = "ignored", "unhandled event type"
        record["status"] = status
        if reason:
            record["reason"] = reason
        txn.put(EVENTS, event_id, record)
        if status == "flagged":
            log.warning("event %s flagged: %s", event_id, reason)
        return 200, {"status": status}

    # ------------------------------------------------------------------ money logic

    def _load_account(self, txn, account_id):
        acct = txn.get(ACCOUNTS, _akey(account_id))
        return acct if acct is not None else {"currency": None, "balance": 0, "seq": 0}

    def _append(self, txn, account_id, acct, entry):
        """Add one entry to the account (in memory + entries table)."""
        acct["seq"] += 1
        acct["balance"] += entry["amount"]
        entry = dict(entry, account_id=account_id, seq=acct["seq"])
        txn.put(ENTRIES, "%s/%012d" % (_akey(account_id), acct["seq"]), entry)

    def _payment(self, txn, event, created, now):
        data = event["data"]
        problem = events.validate_money_event(data)
        if problem:
            return "flagged", problem
        account_id, pid = data["account_id"], data["payment_id"]
        amount, currency = data["amount"], data["currency"]

        if txn.get(PAYMENTS, pid) is not None:
            return "flagged", "payment_id already recorded"
        acct = self._load_account(txn, account_id)
        if acct["currency"] is not None and acct["currency"] != currency:
            return "flagged", "currency differs from account currency"
        acct["currency"] = currency

        self._append(txn, account_id, acct, {
            "event_id": event["id"], "type": "payment.succeeded", "amount": amount,
            "currency": currency, "payment_id": pid, "created": created, "applied_at": now})
        payment = {"account_id": account_id, "amount": amount, "currency": currency,
                   "event_id": event["id"], "refunded": False}

        # A refund that arrived earlier is applied right after the payment, in this same
        # transaction.
        pending = txn.get(PENDING, pid)
        if pending is not None:
            refund_rec = txn.get(EVENTS, pending["event_id"]) or {}
            problem = self._refund_mismatch(payment, pending)
            if problem:
                refund_rec.update(status="flagged", reason=problem)
                log.warning("pending refund %s flagged: %s", pending["event_id"], problem)
            else:
                self._append(txn, account_id, acct, {
                    "event_id": pending["event_id"], "type": "refund.succeeded",
                    "amount": -amount, "currency": currency, "payment_id": pid,
                    "created": pending.get("created"), "applied_at": now})
                payment["refunded"] = True
                refund_rec.update(status="applied")
                refund_rec.pop("reason", None)
            txn.put(EVENTS, pending["event_id"], refund_rec)
            txn.delete(PENDING, pid)

        txn.put(ACCOUNTS, _akey(account_id), acct)
        txn.put(PAYMENTS, pid, payment)
        return "applied", None

    @staticmethod
    def _refund_mismatch(payment, refund):
        if refund["account_id"] != payment["account_id"]:
            return "refund account differs from payment account"
        if refund["amount"] != payment["amount"]:
            return "refund amount differs from payment amount"
        if refund["currency"] != payment["currency"]:
            return "refund currency differs from payment currency"
        return None

    def _refund(self, txn, event, created, now):
        data = event["data"]
        problem = events.validate_money_event(data)
        if problem:
            return "flagged", problem
        account_id, pid = data["account_id"], data["payment_id"]
        refund = {"event_id": event["id"], "account_id": account_id, "amount": data["amount"],
                  "currency": data["currency"], "created": created, "received_at": now}

        payment = txn.get(PAYMENTS, pid)
        if payment is None:
            if txn.get(PENDING, pid) is not None:
                return "flagged", "another refund for this payment is already pending"
            txn.put(PENDING, pid, refund)  # parked until the payment arrives
            return "pending", "payment not seen yet"
        if payment["refunded"]:
            return "flagged", "payment already refunded"
        problem = self._refund_mismatch(payment, refund)
        if problem:
            return "flagged", problem

        acct = self._load_account(txn, account_id)
        self._append(txn, account_id, acct, {
            "event_id": event["id"], "type": "refund.succeeded", "amount": -data["amount"],
            "currency": data["currency"], "payment_id": pid, "created": created,
            "applied_at": now})
        payment["refunded"] = True
        txn.put(ACCOUNTS, _akey(account_id), acct)
        txn.put(PAYMENTS, pid, payment)
        return "applied", None

    # ------------------------------------------------------------------ queries

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            acct = txn.get(ACCOUNTS, _akey(account_id))
        return acct["balance"] if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._store.transaction() as txn:
            rows = txn.scan(ENTRIES, prefix=_akey(account_id) + "/")
        return [value for _key, value in rows]
