"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design (see NOTES.md): every delivery is processed in ONE store transaction, so a StoreError
anywhere rolls the whole delivery back and we answer 503; PayCo's retry then starts from a clean
state. Movements are keyed by payment_id, so a payment or refund can never be applied twice.
"""

import hashlib
import hmac
import json
import logging
import re
import threading

from ledgerkit import StoreError

log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300

# Tables
EVENTS = "events"            # event_id -> {sha256, type, outcome, received_at}
ACCOUNTS = "accounts"        # account_id -> {currency, balance, seq}
ENTRIES = "entries"          # "<len>:<account_id>/<seq>" -> entry
PAYMENTS = "payments"        # payment_id -> {state: applied|held, account_id, amount, currency,
                             #                event_id, refunded (total refunded so far)}
REFUNDS = "refunds"          # legacy (full refunds only): payment_id -> {event_id}; no longer written
PARKED = "parked_refunds"    # "<len>:<payment_id>/<event_id>" -> refund waiting for its payment
HOLDS = "holds"              # event_id -> anomaly needing a human decision

_T_RE = re.compile(r"[0-9]{1,15}")
_CUR_RE = re.compile(r"[A-Z]{3}")


class _Rejected(Exception):
    """A delivery refused with an HTTP status; always raised before the first write."""

    def __init__(self, status, error):
        Exception.__init__(self, error)
        self.status = status
        self.error = error


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def _entry_prefix(account_id):
    # Length prefix makes prefixes unambiguous even if ids contain "/".
    return "%d:%s/" % (len(account_id), account_id)


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret if isinstance(secret, bytes) else str(secret).encode("utf-8")
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
            return self._handle(raw_body, headers)
        except StoreError:
            log.warning("store write failed; delivery rolled back, asking PayCo to retry")
            return 503, {"status": "unavailable", "error": "temporary storage failure, retry"}
        except Exception:  # never leak a traceback to the caller; non-2xx so PayCo retries
            log.exception("unexpected error while handling delivery")
            return 500, {"status": "error", "error": "internal error"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._lock:
            with self._store.transaction() as txn:
                acct = txn.get(ACCOUNTS, account_id)
        return acct["balance"] if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._lock:
            with self._store.transaction() as txn:
                rows = txn.scan(ENTRIES, prefix=_entry_prefix(account_id))
        return [v for _, v in rows]  # zero-padded seq => key order is application order

    def holds(self):
        """Anomalies recorded for human review: {event_id: {...}} (not part of the brief)."""
        with self._lock:
            with self._store.transaction() as txn:
                return dict(txn.scan(HOLDS))

    # ------------------------------------------------------------------ signature

    @staticmethod
    def _header(headers, name):
        found = None
        for k, v in headers.items():
            if isinstance(k, bytes):
                k = k.decode("latin-1")
            if isinstance(k, str) and k.lower() == name:
                if isinstance(v, bytes):
                    v = v.decode("latin-1")
                if found is not None and found != v:
                    return None  # ambiguous duplicate header: reject
                found = v
        return found if isinstance(found, str) else None

    def _signature_ok(self, raw_body, headers):
        value = self._header(headers, "payco-signature")
        if value is None:
            return False
        t = None
        sigs = []
        for part in value.split(","):
            k, sep, v = part.strip().partition("=")
            if not sep:
                continue
            if k == "t":
                if t is not None:
                    return False
                t = v
            elif k == "v1":
                sigs.append(v)
        if t is None or not sigs or not _T_RE.fullmatch(t):
            return False
        if abs(self._clock.now() - int(t)) > TOLERANCE_SECONDS:
            return False
        expected = hmac.new(self._secret, t.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for s in sigs:
            try:
                sb = s.encode("ascii")
            except UnicodeEncodeError:
                continue
            if hmac.compare_digest(sb, expected):
                ok = True
        return ok

    # ------------------------------------------------------------------ handling

    def _handle(self, raw_body, headers):
        if isinstance(raw_body, (bytearray, memoryview)):
            raw_body = bytes(raw_body)
        if not isinstance(raw_body, bytes) or not isinstance(headers, dict):
            return 400, {"status": "rejected", "error": "malformed request"}

        # 1. Authenticate before looking at anything else (and before touching the store).
        if not self._signature_ok(raw_body, headers):
            return 401, {"status": "rejected", "error": "invalid signature"}

        # 2. Envelope.
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return 400, {"status": "rejected", "error": "body is not valid JSON"}
        if not isinstance(event, dict) or not _nonempty_str(event.get("id")) \
                or not isinstance(event.get("type"), str):
            return 400, {"status": "rejected", "error": "missing or invalid id/type"}

        digest = hashlib.sha256(raw_body).hexdigest()
        with self._lock:
            with self._store.transaction() as txn:
                return self._process(txn, event, digest)

    def _process(self, txn, event, digest):
        """Runs inside one transaction; any exception rolls everything back."""
        event_id = event["id"]
        etype = event["type"]
        now = self._clock.now()

        seen = txn.get(EVENTS, event_id)
        if seen is not None:
            if seen["sha256"] != digest:
                log.critical("INCIDENT: event %s redelivered with a different body; rejected",
                             event_id)
                return 409, {"status": "conflict", "event_id": event_id,
                             "error": "event id reused with a different body"}
            return 200, {"status": "duplicate", "event_id": event_id, "outcome": seen["outcome"]}

        extra = {}
        if etype in ("payment.succeeded", "refund.succeeded"):
            ev, err = self._validate(event)
            if err:
                return 422, {"status": "rejected", "event_id": event_id, "error": err}
            try:
                if etype == "payment.succeeded":
                    outcome, extra = self._payment(txn, ev, now)
                else:
                    outcome, extra = self._refund(txn, ev, now)
            except _Rejected as r:
                # Raised before any write: nothing is stored, not even the event record.
                log.warning("event %s rejected with %d: %s", event_id, r.status, r.error)
                return r.status, {"status": "rejected", "event_id": event_id, "error": r.error}
        else:
            outcome = "ignored"  # unknown types: acknowledge (2xx) so PayCo stops retrying

        txn.put(EVENTS, event_id, {"sha256": digest, "type": etype, "outcome": outcome,
                                   "received_at": now})
        body = {"status": outcome, "event_id": event_id}
        body.update(extra)
        return 200, body

    @staticmethod
    def _validate(event):
        data = event.get("data")
        if not isinstance(data, dict):
            return None, "data must be an object"
        if not _is_int(event.get("created")):
            return None, "created must be an integer"
        for f in ("account_id", "payment_id"):
            if not _nonempty_str(data.get(f)):
                return None, "%s must be a non-empty string" % f
        amount = data.get("amount")
        if not _is_int(amount) or amount <= 0:
            return None, "amount must be a positive integer"
        currency = data.get("currency")
        if event["type"] == "payment.succeeded":
            if not isinstance(currency, str) or not _CUR_RE.fullmatch(currency):
                return None, "currency must be a 3-letter uppercase code"
        elif currency is not None and (not isinstance(currency, str)
                                       or not _CUR_RE.fullmatch(currency)):
            return None, "currency must be a 3-letter uppercase code"
        return {"event_id": event["id"], "type": event["type"], "created": event["created"],
                "account_id": data["account_id"], "payment_id": data["payment_id"],
                "amount": amount, "currency": currency}, None

    # -- helpers that write

    @staticmethod
    def _hold(txn, ev, reason, now):
        txn.put(HOLDS, ev["event_id"], {"reason": reason, "type": ev["type"],
                                        "account_id": ev["account_id"],
                                        "payment_id": ev["payment_id"], "amount": ev["amount"],
                                        "currency": ev["currency"], "created": ev["created"],
                                        "held_at": now})
        log.warning("hold %s on event %s", reason, ev["event_id"])

    @staticmethod
    def _append(txn, account, ev, signed_amount, now):
        """Add a movement to `account` (dict, mutated; caller puts it)."""
        account["seq"] += 1
        account["balance"] += signed_amount
        txn.put(ENTRIES, _entry_prefix(ev["account_id"]) + "%012d" % account["seq"], {
            "event_id": ev["event_id"], "type": ev["type"], "amount": signed_amount,
            "currency": account["currency"], "account_id": ev["account_id"],
            "payment_id": ev["payment_id"], "created": ev["created"], "applied_at": now,
            "seq": account["seq"]})

    @staticmethod
    def _refund_problem(payment, ev):
        """Reasons (other than over-refunding) why a refund needs a human decision."""
        if payment["state"] != "applied":
            return "refund_of_held_payment"
        if payment["account_id"] != ev["account_id"]:
            return "refund_account_mismatch"
        if ev["currency"] is not None and ev["currency"] != payment["currency"]:
            return "refund_currency_mismatch"
        return None

    @staticmethod
    def _refunded(txn, pid, payment):
        """Total refunded so far for the payment. Records written before partial refunds existed
        have no "refunded" field; for them a refund in the legacy REFUNDS table meant a full one."""
        if "refunded" in payment:
            return payment["refunded"]
        return payment["amount"] if txn.get(REFUNDS, pid) is not None else 0

    @staticmethod
    def _parked_key(pid, event_id):
        return _entry_prefix(pid) + event_id

    @staticmethod
    def _parked_for(txn, pid):
        """All refunds parked for payment `pid`, as [(table key, refund)] in a deterministic order
        (by the refund's `created`, then event id), independent of delivery order."""
        found = list(txn.scan(PARKED, prefix=_entry_prefix(pid)))
        legacy = txn.get(PARKED, pid)  # old layout: one parked refund, keyed by payment_id alone
        if legacy is not None and legacy.get("payment_id") == pid:
            found.append((pid, legacy))
        found.sort(key=lambda kv: (kv[1]["created"], kv[1]["event_id"]))
        return found

    def _payment(self, txn, ev, now):
        pid = ev["payment_id"]
        if txn.get(PAYMENTS, pid) is not None:
            self._hold(txn, ev, "duplicate_payment", now)
            return "held", {"reason": "duplicate_payment"}

        account = txn.get(ACCOUNTS, ev["account_id"]) or {"currency": None, "balance": 0, "seq": 0}
        record = {"account_id": ev["account_id"], "amount": ev["amount"],
                  "currency": ev["currency"], "event_id": ev["event_id"]}
        if account["currency"] is not None and account["currency"] != ev["currency"]:
            txn.put(PAYMENTS, pid, dict(record, state="held"))
            self._hold(txn, ev, "currency_mismatch", now)
            return "held", {"reason": "currency_mismatch"}

        account["currency"] = ev["currency"]
        self._append(txn, account, ev, ev["amount"], now)
        payment = dict(record, state="applied", refunded=0)
        released = []

        # Refunds may have arrived first; settle them now, in the same transaction, oldest first.
        # The payment itself is legitimate, so a parked refund that would push the total past the
        # payment's amount cannot be rejected with 422 (it was already acknowledged); it is held
        # for finance instead and the balance is untouched by it.
        for key, pev in self._parked_for(txn, pid):
            pev = dict(pev)
            problem = self._refund_problem(payment, pev)
            if problem is None and payment["refunded"] + pev["amount"] > payment["amount"]:
                problem = "refund_exceeds_payment"
            if problem:
                self._hold(txn, pev, problem, now)
                refund_outcome = "held"
            else:
                self._append(txn, account, pev, -pev["amount"], now)
                payment["refunded"] += pev["amount"]
                refund_outcome = "applied"
            txn.delete(PARKED, key)
            rec = txn.get(EVENTS, pev["event_id"])
            if rec is not None:
                rec["outcome"] = refund_outcome
                txn.put(EVENTS, pev["event_id"], rec)
            released.append({"event_id": pev["event_id"], "outcome": refund_outcome})

        txn.put(PAYMENTS, pid, payment)
        txn.put(ACCOUNTS, ev["account_id"], account)
        return "applied", ({"released_refunds": released} if released else {})

    def _refund(self, txn, ev, now):
        """One payment may have several (partial) refunds; their total may never exceed the
        payment's amount. Every check happens before the first write, so a rejection (422)
        leaves the store untouched."""
        pid = ev["payment_id"]
        payment = txn.get(PAYMENTS, pid)
        if payment is None:
            # Refund before its payment: do not touch the balance; wait for the payment.
            txn.put(PARKED, self._parked_key(pid, ev["event_id"]), ev)
            return "parked", {}

        refunded = self._refunded(txn, pid, payment)
        if refunded + ev["amount"] > payment["amount"]:
            raise _Rejected(422, "refund of %d would exceed payment %s (amount %d, already "
                            "refunded %d)" % (ev["amount"], pid, payment["amount"], refunded))

        problem = self._refund_problem(payment, ev)
        if problem:
            self._hold(txn, ev, problem, now)
            return "held", {"reason": problem}

        account = txn.get(ACCOUNTS, ev["account_id"])
        self._append(txn, account, ev, -ev["amount"], now)
        txn.put(ACCOUNTS, ev["account_id"], account)
        payment["refunded"] = refunded + ev["amount"]
        txn.put(PAYMENTS, pid, payment)
        return "applied", {}
