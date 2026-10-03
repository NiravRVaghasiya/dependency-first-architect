"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design (details in NOTES.md): every delivery is processed in ONE store transaction, so either
everything about an event (event record, entries, payment/refund index, account) is committed
or nothing is, and a StoreError yields 503 (PayCo retries). Balances are computed from entries.
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
SIGNATURE_HEADER = "payco-signature"

T_EVENTS = "events"          # event_id -> event record (commit point, dedup identity)
T_PAYMENTS = "payments"      # payment_id -> committed payment
T_REFUNDS = "refunds"        # "<quoted payment_id>/<quoted event_id>" -> refund (parked / applied);
                             # legacy single-refund records are keyed by the bare payment_id
T_ACCOUNTS = "accounts"      # account_id -> {"currency", "seq"}
T_ENTRIES = "entries"        # "<quoted account>/<seq>" -> entry
T_QUARANTINE = "quarantine"  # event_id -> anomaly
T_INCIDENTS = "incidents"    # "<event_id>/<hash>" -> conflicting delivery

_CURRENCY = re.compile(r"\A[A-Z]{3}\Z")


class _Malformed(Exception):
    pass


def _pairs_hook(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def _reject_constant(name):
    raise ValueError("non-finite number")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _entry_prefix(account_id):
    return quote(account_id, safe="") + "/"


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = bytes(secret)
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
            if not isinstance(raw_body, (bytes, bytearray)):
                return 400, {"error": "body must be bytes"}
            raw_body = bytes(raw_body)

            if not self._authentic(raw_body, headers):
                return 401, {"error": "invalid signature"}

            try:
                event = self._parse(raw_body)
            except _Malformed as exc:
                return 400, {"error": "malformed event", "detail": str(exc)}

            with self._lock:
                try:
                    with self._store.transaction() as txn:
                        status, body = self._process(txn, event, raw_body)
                except StoreError:
                    log.warning("store error while handling event_id=%s", event["id"])
                    return 503, {"error": "storage unavailable, retry"}
            log.info("event_id=%s type=%s status=%s outcome=%s",
                     event["id"], event["type"], status, body.get("outcome", body.get("error")))
            return status, body
        except StoreError:
            return 503, {"error": "storage unavailable, retry"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        return sum(e["amount"] for e in self.entries(account_id))

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        if not isinstance(account_id, str):
            return []
        with self._lock:
            with self._store.transaction() as txn:
                rows = txn.scan(T_ENTRIES, prefix=_entry_prefix(account_id))
        return [value for _, value in rows]

    # ------------------------------------------------------------------ authenticity

    def _authentic(self, raw_body, headers):
        if not isinstance(headers, dict):
            return False
        values = [v for k, v in headers.items()
                  if isinstance(k, str) and k.strip().lower() == SIGNATURE_HEADER]
        if len(values) != 1:  # missing, or ambiguous duplicates
            return False
        value = values[0]
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode("ascii")
            except UnicodeDecodeError:
                return False
        if not isinstance(value, str):
            return False

        t_values, v1_values = [], []
        for part in value.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "t":
                t_values.append(v)
            elif k == "v1":
                v1_values.append(v)
        if len(t_values) != 1 or not v1_values:
            return False
        t_str = t_values[0]
        if not (t_str.isascii() and t_str.isdigit() and len(t_str) <= 15):
            return False

        expected = hmac.new(self._secret, t_str.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for candidate in v1_values:  # accept if any v1 verifies (secret rotation)
            if hmac.compare_digest(expected, candidate.encode("utf-8")):
                ok = True
        if not ok:
            return False
        return abs(self._clock.now() - int(t_str)) <= TOLERANCE_SECONDS

    # ------------------------------------------------------------------ parsing

    def _parse(self, raw_body):
        try:
            doc = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_pairs_hook,
                             parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            raise _Malformed("invalid JSON") from exc
        if not isinstance(doc, dict):
            raise _Malformed("body must be an object")
        event_id, etype = doc.get("id"), doc.get("type")
        if not isinstance(event_id, str) or not event_id:
            raise _Malformed("id must be a non-empty string")
        if not isinstance(etype, str) or not etype:
            raise _Malformed("type must be a non-empty string")
        created = doc.get("created")
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            created = None
        event = {"id": event_id, "type": etype, "created": created, "data": doc.get("data")}
        if etype in ("payment.succeeded", "refund.succeeded"):
            data = doc.get("data")
            if not isinstance(data, dict):
                raise _Malformed("data must be an object")
            for field in ("account_id", "payment_id"):
                if not isinstance(data.get(field), str) or not data[field]:
                    raise _Malformed(field + " must be a non-empty string")
            if not _is_int(data.get("amount")):
                raise _Malformed("amount must be an integer")
            cur = data.get("currency")
            if not isinstance(cur, str) or not _CURRENCY.match(cur):
                raise _Malformed("currency must be 3 uppercase letters")
            event.update(account_id=data["account_id"], payment_id=data["payment_id"],
                         amount=data["amount"], currency=cur)
        return event

    # ------------------------------------------------------------------ processing

    @staticmethod
    def _respond(rec, duplicate):
        body = {"event_id": rec["event_id"], "outcome": rec["outcome"]}
        if rec.get("reason"):
            body["reason"] = rec["reason"]
        if duplicate:
            body["duplicate"] = True
        return rec["status"], body

    def _process(self, txn, event, raw_body):
        digest = hashlib.sha256(raw_body).hexdigest()
        event_id = event["id"]

        existing = txn.get(T_EVENTS, event_id)
        if existing is not None:
            if existing["hash"] == digest:
                return self._respond(existing, True)
            # Same id, different body: security incident. Leave everything else untouched.
            txn.put(T_INCIDENTS, event_id + "/" + digest,
                    {"event_id": event_id, "hash": digest, "original_hash": existing["hash"],
                     "seen_at": self._clock.now()})
            log.error("INCIDENT: event_id=%s redelivered with a different body", event_id)
            return 409, {"event_id": event_id, "outcome": "incident",
                         "error": "event id reused with a different body"}

        rec = {"event_id": event_id, "hash": digest, "body": raw_body.decode("utf-8"),
               "type": event["type"], "created": event["created"], "status": 200,
               "outcome": None, "reason": None,
               "received_at": self._clock.now()}
        for f in ("account_id", "payment_id", "amount", "currency"):
            if f in event:
                rec[f] = event[f]

        if event["type"] == "payment.succeeded":
            self._payment(txn, event, rec)
        elif event["type"] == "refund.succeeded":
            self._refund(txn, event, rec)
        else:
            rec["outcome"] = "unhandled"

        if rec["outcome"] == "quarantined":
            rec["status"] = 422
            txn.put(T_QUARANTINE, event_id, {"event_id": event_id, "reason": rec["reason"],
                                            "type": rec["type"]})
        txn.put(T_EVENTS, event_id, rec)  # the event record
        return self._respond(rec, False)

    def _quarantine(self, rec, reason):
        rec["outcome"] = "quarantined"
        rec["reason"] = reason

    def _add_entry(self, txn, account_id, currency, event_id, etype, amount, payment_id, created):
        acct = txn.get(T_ACCOUNTS, account_id) or {"currency": currency, "seq": 0}
        acct["seq"] += 1
        key = _entry_prefix(account_id) + "%012d" % acct["seq"]
        txn.put(T_ENTRIES, key, {"event_id": event_id, "type": etype, "amount": amount,
                                 "account_id": account_id, "payment_id": payment_id,
                                 "currency": currency, "created": created})
        txn.put(T_ACCOUNTS, account_id, acct)

    def _payment(self, txn, event, rec):
        if event["amount"] <= 0:
            return self._quarantine(rec, "non_positive_amount")
        pid, acct_id, cur = event["payment_id"], event["account_id"], event["currency"]
        if txn.get(T_PAYMENTS, pid) is not None:
            return self._quarantine(rec, "duplicate_payment_id")
        acct = txn.get(T_ACCOUNTS, acct_id)
        if acct is not None and acct["currency"] != cur:
            return self._quarantine(rec, "currency_mismatch")

        self._add_entry(txn, acct_id, cur, event["id"], event["type"], event["amount"], pid,
                        event["created"])
        payment = {"event_id": event["id"], "account_id": acct_id,
                   "amount": event["amount"], "currency": cur, "refunded": 0}
        rec["outcome"] = "applied"

        # Refunds that arrived earlier are waiting for this payment. Apply them in arrival order
        # while the running total stays within the payment amount; quarantine the rest.
        for table, key, parked in self._parked_refunds(txn, pid):
            refund_ev = txn.get(T_EVENTS, parked["event_id"])
            reason = self._refund_problem(payment, payment["refunded"], parked["account_id"],
                                          parked["currency"], parked["amount"])
            if reason is None:
                self._add_entry(txn, acct_id, cur, parked["event_id"], "refund.succeeded",
                                -parked["amount"], pid, refund_ev.get("created"))
                payment["refunded"] += parked["amount"]
                parked["state"] = "applied"
                refund_ev["outcome"] = "applied"
            else:
                # The 200 already sent for the parked delivery cannot be taken back; the event
                # record keeps status 200 (what redeliveries see) but is flagged for finance.
                parked["state"] = "quarantined"
                refund_ev["outcome"] = "quarantined"
                refund_ev["reason"] = reason
                txn.put(T_QUARANTINE, parked["event_id"],
                        {"event_id": parked["event_id"], "reason": reason,
                         "type": "refund.succeeded"})
            txn.put(table, key, parked)
            txn.put(T_EVENTS, parked["event_id"], refund_ev)
        txn.put(T_PAYMENTS, pid, payment)

    @staticmethod
    def _refund_problem(payment, refunded, account_id, currency, amount):
        """None if a refund of `amount` fits the payment, else a quarantine reason."""
        if payment["account_id"] != account_id or payment["currency"] != currency:
            return "refund_does_not_match_payment"
        if refunded + amount > payment["amount"]:
            return "refund_exceeds_payment"
        return None

    @staticmethod
    def _refund_key(payment_id, event_id):
        return quote(payment_id, safe="") + "/" + quote(event_id, safe="")

    def _refund_items(self, txn, payment_id):
        """[(table, key, item)] for every refund stored for the payment, in arrival order."""
        rows = [(T_REFUNDS, k, v) for k, v in
                txn.scan(T_REFUNDS, prefix=quote(payment_id, safe="") + "/")]
        legacy = txn.get(T_REFUNDS, payment_id)  # single-refund schema used before partial refunds
        if legacy is not None:
            legacy.setdefault("n", 0)
            rows.append((T_REFUNDS, payment_id, legacy))
        rows.sort(key=lambda r: (r[2].get("n", 0), r[1]))
        return rows

    def _parked_refunds(self, txn, payment_id):
        return [r for r in self._refund_items(txn, payment_id) if r[2].get("state") == "parked"]

    def _refunded_total(self, txn, payment, payment_id):
        if "refunded" in payment:
            return payment["refunded"]
        # Payment written before partial refunds: derive from the old single-refund record.
        legacy = txn.get(T_REFUNDS, payment_id)
        return legacy["amount"] if legacy is not None and legacy.get("state") == "applied" else 0

    def _refund(self, txn, event, rec):
        if event["amount"] <= 0:
            return self._quarantine(rec, "non_positive_amount")
        pid, acct_id, cur = event["payment_id"], event["account_id"], event["currency"]
        payment = txn.get(T_PAYMENTS, pid)
        refund = {"event_id": event["id"], "account_id": acct_id, "amount": event["amount"],
                  "currency": cur}
        key = self._refund_key(pid, event["id"])
        if payment is None:
            # Refund before its payment: wait for the payment (its total can't be checked yet).
            refund["state"] = "parked"
            refund["n"] = len(self._refund_items(txn, pid)) + 1
            txn.put(T_REFUNDS, key, refund)
            rec["outcome"] = "parked"
            return
        refunded = self._refunded_total(txn, payment, pid)
        reason = self._refund_problem(payment, refunded, acct_id, cur, event["amount"])
        if reason is not None:
            return self._quarantine(rec, reason)
        self._add_entry(txn, acct_id, cur, event["id"], event["type"], -event["amount"], pid,
                        event["created"])
        payment["refunded"] = refunded + event["amount"]
        txn.put(T_PAYMENTS, pid, payment)
        refund["state"] = "applied"
        refund["n"] = len(self._refund_items(txn, pid)) + 1
        txn.put(T_REFUNDS, key, refund)
        rec["outcome"] = "applied"
