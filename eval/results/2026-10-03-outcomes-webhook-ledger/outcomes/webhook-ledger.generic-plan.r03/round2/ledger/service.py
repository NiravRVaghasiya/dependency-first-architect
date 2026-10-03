"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design (details in NOTES.md): every delivery is processed in ONE store transaction, so the
"seen this event" record, the balance, the entry and any payment/refund bookkeeping commit
together or not at all. A StoreError rolls everything back and we answer 503, so PayCo retries
and nothing is half-applied or wrongly remembered as done.
"""

import hashlib
import hmac
import json
import re

from ledgerkit import StoreError

TOLERANCE_SECONDS = 300

EVENTS = "events"            # event id -> {canon, type, status, payload}
ACCOUNTS = "accounts"        # quoted account id -> {balance, currency, next_seq}
ENTRIES = "entries"          # "<quoted account id>/<seq>" -> entry
PAYMENTS = "payments"        # payment id -> {account_id, amount, currency, event_id, refunded}
                             # (refunded = running total refunded; legacy rows: refunded_by)
PENDING_EVENTS = "pending_refund_events"  # "<quoted payment id>/<quoted event id>" -> refund that
                                          # arrived before its payment (many per payment)
PENDING = "pending_refunds"  # legacy layout: payment id -> one parked refund (read-only now)

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"

_T_RE = re.compile(r"[0-9]{1,15}", re.ASCII)
_V1_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CURRENCY_RE = re.compile(r"[A-Z]{3}", re.ASCII)


class _Invalid(Exception):
    pass


def _reject_dupes(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise _Invalid("duplicate key")
        obj[k] = v
    return obj


def _reject_constant(name):
    raise _Invalid("non-finite number")


def _qkey(account_id):
    # Escape "/" (and everything odd) so one account's entry prefix can't match another's.
    out = []
    for ch in account_id:
        if ch.isascii() and (ch.isalnum() or ch in "_-.:"):
            out.append(ch)
        else:
            out.append("%%%06x" % ord(ch))
    return "".join(out)


def _is_int(x):
    return type(x) is int


def _nonempty_str(x):
    return isinstance(x, str) and x != ""


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret if isinstance(secret, bytes) else str(secret).encode()
        self._clock = clock

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        if not self._signature_ok(raw_body, headers):
            return 401, {"status": "unauthorized"}

        try:
            envelope = self._parse(raw_body)
        except _Invalid as exc:
            return 400, {"status": "bad_request", "reason": str(exc)}

        try:
            with self._store.transaction() as txn:
                return self._process(txn, envelope)
        except StoreError:
            # The transaction rolled back completely; PayCo will redeliver.
            return 503, {"status": "retry"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            acct = txn.get(ACCOUNTS, _qkey(account_id))
        return acct["balance"] if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._store.transaction() as txn:
            rows = txn.scan(ENTRIES, _qkey(account_id) + "/")
        return [value for _, value in rows]  # keys are zero-padded sequence numbers

    # ------------------------------------------------------------------ authentication

    def _signature_ok(self, raw_body, headers):
        values = [v for k, v in (headers or {}).items()
                  if isinstance(k, str) and k.lower() == "payco-signature"]
        if len(values) != 1:
            return False
        value = values[0]
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                return False
        if not isinstance(value, str):
            return False
        ts = []
        sigs = []
        for part in value.split(","):
            name, sep, val = part.strip().partition("=")
            if not sep:
                return False
            if name == "t":
                ts.append(val)
            elif name == "v1":
                sigs.append(val)
        if len(ts) != 1 or not _T_RE.fullmatch(ts[0]) or not sigs:
            return False
        t_str = ts[0]
        expected = hmac.new(self._secret, t_str.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for sig in sigs:
            if _V1_RE.fullmatch(sig) and hmac.compare_digest(sig.encode("ascii"), expected):
                ok = True
        if not ok:
            return False
        return abs(self._clock.now() - int(t_str)) <= TOLERANCE_SECONDS

    # ------------------------------------------------------------------ parsing

    def _parse(self, raw_body):
        try:
            obj = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_reject_dupes,
                             parse_constant=_reject_constant)
        except _Invalid as exc:
            raise _Invalid("invalid JSON: %s" % exc)
        except (ValueError, RecursionError):
            raise _Invalid("invalid JSON")
        if not isinstance(obj, dict):
            raise _Invalid("body must be an object")
        if not _nonempty_str(obj.get("id")):
            raise _Invalid("missing event id")
        if not _nonempty_str(obj.get("type")):
            raise _Invalid("missing event type")
        return obj

    @staticmethod
    def _validate_money_event(envelope):
        """Returns the normalised fields of a payment/refund event, or raises _Invalid."""
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise _Invalid("missing data")
        for field in ("account_id", "payment_id"):
            if not _nonempty_str(data.get(field)):
                raise _Invalid("invalid %s" % field)
        amount = data.get("amount")
        if not _is_int(amount) or amount <= 0:
            raise _Invalid("amount must be a positive integer")
        currency = data.get("currency")
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            raise _Invalid("invalid currency")
        created = envelope.get("created")
        return {
            "event_id": envelope["id"],
            "type": envelope["type"],
            "account_id": data["account_id"],
            "payment_id": data["payment_id"],
            "amount": amount,
            "currency": currency,
            "created": created if _is_int(created) else None,
        }

    # ------------------------------------------------------------------ processing

    def _process(self, txn, envelope):
        event_id = envelope["id"]
        etype = envelope["type"]
        canon = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

        seen = txn.get(EVENTS, event_id)
        if seen is not None:
            if seen["canon"] != canon:
                # Same id, different body: incident. Touch nothing.
                return 409, {"status": "conflict"}
            return 200, {"status": "duplicate"}

        if etype not in (PAYMENT, REFUND):
            # Not handled yet. Acknowledge (so PayCo stops retrying) but keep the payload so it
            # can be replayed when we support the type.
            txn.put(EVENTS, event_id, {"canon": canon, "type": etype, "status": "ignored",
                                       "payload": envelope})
            return 200, {"status": "ignored"}

        try:
            ev = self._validate_money_event(envelope)
        except _Invalid as exc:
            return 400, {"status": "bad_request", "reason": str(exc)}

        if etype == PAYMENT:
            return self._payment(txn, ev, canon, envelope)
        return self._refund(txn, ev, canon, envelope)

    def _record_event(self, txn, ev, canon, envelope, status):
        txn.put(EVENTS, ev["event_id"], {"canon": canon, "type": ev["type"], "status": status,
                                         "payload": envelope})

    def _post(self, txn, ev, signed_amount):
        """Append an entry and move the balance. All checks must already have passed."""
        key = _qkey(ev["account_id"])
        acct = txn.get(ACCOUNTS, key) or {"balance": 0, "currency": ev["currency"], "next_seq": 1}
        seq = acct["next_seq"]
        acct["balance"] += signed_amount
        acct["next_seq"] = seq + 1
        txn.put(ACCOUNTS, key, acct)
        txn.put(ENTRIES, "%s/%012d" % (key, seq), {
            "event_id": ev["event_id"],
            "type": ev["type"],
            "amount": signed_amount,
            "currency": ev["currency"],
            "account_id": ev["account_id"],
            "payment_id": ev["payment_id"],
            "created": ev["created"],
            "seq": seq,
        })

    def _payment(self, txn, ev, canon, envelope):
        if txn.get(PAYMENTS, ev["payment_id"]) is not None:
            return 422, {"status": "rejected", "reason": "payment_id already used"}
        acct = txn.get(ACCOUNTS, _qkey(ev["account_id"]))
        if acct is not None and acct["currency"] != ev["currency"]:
            return 422, {"status": "rejected", "reason": "currency mismatch"}

        self._record_event(txn, ev, canon, envelope, "applied")
        self._post(txn, ev, ev["amount"])
        payment = {"account_id": ev["account_id"], "amount": ev["amount"],
                   "currency": ev["currency"], "event_id": ev["event_id"], "refunded": 0}

        # Refunds that beat their payment are settled now, in the same transaction. Several may
        # be parked; apply them in a deterministic order (created, then event id) as long as the
        # running total stays within the payment's amount.
        parked = []
        pid_prefix = _qkey(ev["payment_id"]) + "/"
        for key, row in txn.scan(PENDING_EVENTS, pid_prefix):
            parked.append((PENDING_EVENTS, key, row))
        legacy = txn.get(PENDING, ev["payment_id"])  # pre-partial-refund layout: one per payment
        if legacy is not None:
            parked.append((PENDING, ev["payment_id"], legacy))
        parked.sort(key=lambda p: (p[2]["created"] if _is_int(p[2].get("created")) else float("inf"),
                                   p[2]["event_id"]))
        for table, key, row in parked:
            if not self._refund_fits(payment, row):
                # Mismatching or over-limit parked refunds stay parked (never applied, never
                # dropped) for finance to look at.
                continue
            refund_ev = {"event_id": row["event_id"], "type": REFUND,
                         "account_id": row["account_id"], "payment_id": ev["payment_id"],
                         "amount": row["amount"], "currency": row["currency"],
                         "created": row["created"]}
            self._post(txn, refund_ev, -row["amount"])
            payment["refunded"] += row["amount"]
            txn.delete(table, key)
        txn.put(PAYMENTS, ev["payment_id"], payment)
        return 200, {"status": "applied"}

    @staticmethod
    def _refunded_total(payment):
        """Total already refunded for a payment. Rows written before partial refunds existed
        carry `refunded_by` (a full refund) instead of a `refunded` running total."""
        if "refunded" in payment:
            return payment["refunded"]
        return payment["amount"] if payment.get("refunded_by") is not None else 0

    @classmethod
    def _refund_fits(cls, payment, refund):
        """True if `refund` belongs to this payment (account, currency) and adding it keeps the
        total refunded within the payment's amount."""
        return (payment["account_id"] == refund["account_id"]
                and payment["currency"] == refund["currency"]
                and cls._refunded_total(payment) + refund["amount"] <= payment["amount"])

    def _refund(self, txn, ev, canon, envelope):
        payment = txn.get(PAYMENTS, ev["payment_id"])
        if payment is None:
            # Payment not seen yet: park this refund (several per payment are allowed now).
            self._record_event(txn, ev, canon, envelope, "pending")
            txn.put(PENDING_EVENTS, _qkey(ev["payment_id"]) + "/" + _qkey(ev["event_id"]), {
                "event_id": ev["event_id"], "account_id": ev["account_id"],
                "amount": ev["amount"], "currency": ev["currency"], "created": ev["created"]})
            return 202, {"status": "pending"}
        # All checks happen before any write, so a 422 leaves the account untouched.
        if payment["account_id"] != ev["account_id"] or payment["currency"] != ev["currency"]:
            return 422, {"status": "rejected", "reason": "refund does not match payment"}
        refunded = self._refunded_total(payment)
        if refunded + ev["amount"] > payment["amount"]:
            return 422, {"status": "rejected", "reason": "refund exceeds payment amount"}
        self._record_event(txn, ev, canon, envelope, "applied")
        self._post(txn, ev, -ev["amount"])
        payment["refunded"] = refunded + ev["amount"]
        payment.pop("refunded_by", None)
        txn.put(PAYMENTS, ev["payment_id"], payment)
        return 200, {"status": "applied"}
