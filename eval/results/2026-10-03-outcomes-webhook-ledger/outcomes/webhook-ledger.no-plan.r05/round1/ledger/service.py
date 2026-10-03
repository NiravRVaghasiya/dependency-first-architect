"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import hashlib
import hmac
import json
import threading

from ledgerkit import StoreError

TOLERANCE_SECONDS = 300

T_EVENTS = "events"            # event_id -> {hash, status}
T_ACCOUNTS = "accounts"        # account_id -> {currency, balance, seq}
T_PAYMENTS = "payments"        # payment_id -> {account_id, amount, currency, event_id, refunded}
T_PENDING = "pending_refunds"  # payment_id -> refund event waiting for its payment
T_ENTRIES = "entries"          # "<account_id>/<seq>" -> entry

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"


class _Reject(Exception):
    """Abort the current transaction (nothing is written) and answer with this status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret
        self._clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        if not isinstance(raw_body, (bytes, bytearray)):
            return 400, {"error": "body must be bytes"}
        raw_body = bytes(raw_body)

        # 1. Authenticate before looking at the content.
        err = self._check_signature(raw_body, headers)
        if err is not None:
            return err

        # 2. Parse and validate.
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, {"error": "body is not valid JSON"}
        if not isinstance(event, dict):
            return 400, {"error": "body must be a JSON object"}
        event_id = event.get("id")
        etype = event.get("type")
        if not _nonempty_str(event_id) or not _nonempty_str(etype):
            return 400, {"error": "event id and type are required"}

        if etype not in (PAYMENT, REFUND):
            # New PayCo event types: acknowledge (so PayCo stops retrying) but do nothing.
            return 200, {"status": "ignored", "reason": "unhandled event type"}

        parsed = self._validate(event, etype)
        if isinstance(parsed, tuple):
            return parsed

        digest = hashlib.sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        # 3. Apply atomically: every write happens in one transaction.
        with self._lock:
            try:
                with self._store.transaction() as txn:
                    result = self._apply(txn, event_id, etype, event, parsed, digest)
            except _Reject as r:
                return r.status, {"error": r.message}
            except StoreError:
                # Rolled back entirely; non-2xx so PayCo redelivers.
                return 503, {"error": "temporary storage failure, retry"}
        return 200, result

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
            rows = txn.scan(T_ENTRIES, prefix=account_id + "/")
        out = [v for _, v in rows if v.get("account_id") == account_id]
        out.sort(key=lambda v: v["seq"])
        return out

    # ------------------------------------------------------------------ internals

    def _check_signature(self, raw_body, headers):
        value = None
        for name, v in (headers or {}).items():
            if isinstance(name, str) and name.lower() == "payco-signature":
                value = v
                break
        if not isinstance(value, str):
            return 401, {"error": "missing signature"}
        t_raw = None
        sigs = []
        for part in value.split(","):
            k, _, v = part.strip().partition("=")
            if k == "t":
                t_raw = v
            elif k == "v1":
                sigs.append(v)
        try:
            t = int(t_raw)
        except (TypeError, ValueError):
            return 401, {"error": "malformed signature header"}
        expected = hmac.new(self._secret, str(t).encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for s in sigs:
            try:
                if hmac.compare_digest(s.encode("ascii"), expected):
                    ok = True
            except UnicodeEncodeError:
                pass
        if not ok:
            return 401, {"error": "invalid signature"}
        if abs(self._clock.now() - t) > TOLERANCE_SECONDS:
            return 401, {"error": "timestamp outside tolerance"}
        return None

    def _validate(self, event, etype):
        data = event.get("data")
        if not isinstance(data, dict):
            return 400, {"error": "data must be an object"}
        account_id = data.get("account_id")
        payment_id = data.get("payment_id")
        amount = data.get("amount")
        currency = data.get("currency")
        if not _nonempty_str(account_id):
            return 400, {"error": "account_id required"}
        if not _nonempty_str(payment_id):
            return 400, {"error": "payment_id required"}
        if not _is_int(amount) or amount <= 0:
            return 400, {"error": "amount must be a positive integer"}
        if not _nonempty_str(currency):
            return 400, {"error": "currency required"}
        return {"account_id": account_id, "payment_id": payment_id,
                "amount": amount, "currency": currency, "created": event.get("created")}

    def _apply(self, txn, event_id, etype, event, p, digest):
        existing = txn.get(T_EVENTS, event_id)
        if existing is not None:
            if existing["hash"] != digest:
                # Same id, different body: security incident. Touch nothing.
                raise _Reject(409, "event id reused with a different body")
            return {"status": "duplicate", "event_status": existing["status"]}

        if etype == PAYMENT:
            return self._apply_payment(txn, event_id, p, digest)
        return self._apply_refund(txn, event_id, p, digest)

    def _entry(self, acct, account_id, event_id, etype, signed, p):
        acct["seq"] += 1
        acct["balance"] += signed
        return "%s/%012d" % (account_id, acct["seq"]), {
            "seq": acct["seq"], "account_id": account_id, "event_id": event_id,
            "type": etype, "amount": signed, "currency": p["currency"],
            "payment_id": p["payment_id"], "created": p["created"],
        }

    def _apply_payment(self, txn, event_id, p, digest):
        account_id, payment_id = p["account_id"], p["payment_id"]
        acct = txn.get(T_ACCOUNTS, account_id)
        if acct is None:
            acct = {"currency": p["currency"], "balance": 0, "seq": 0}
        elif acct["currency"] != p["currency"]:
            raise _Reject(422, "currency does not match the account currency")

        if txn.get(T_PAYMENTS, payment_id) is not None:
            # Same payment under a new event id: never credit twice.
            txn.put(T_EVENTS, event_id, {"hash": digest, "status": "ignored_duplicate_payment"})
            return {"status": "ignored", "reason": "payment already recorded"}

        key, entry = self._entry(acct, account_id, event_id, PAYMENT, p["amount"], p)
        txn.put(T_ENTRIES, key, entry)
        payment = {"account_id": account_id, "amount": p["amount"], "currency": p["currency"],
                   "event_id": event_id, "refunded": False}

        # A refund may have arrived before this payment; settle it now if it matches.
        pending = txn.get(T_PENDING, payment_id)
        if (pending is not None and pending["account_id"] == account_id
                and pending["amount"] == p["amount"] and pending["currency"] == p["currency"]):
            rp = {"payment_id": payment_id, "currency": p["currency"],
                  "created": pending["created"]}
            key, entry = self._entry(acct, account_id, pending["event_id"], REFUND,
                                     -pending["amount"], rp)
            txn.put(T_ENTRIES, key, entry)
            txn.put(T_EVENTS, pending["event_id"], {"hash": pending["hash"], "status": "applied"})
            txn.delete(T_PENDING, payment_id)
            payment["refunded"] = True

        txn.put(T_PAYMENTS, payment_id, payment)
        txn.put(T_ACCOUNTS, account_id, acct)
        txn.put(T_EVENTS, event_id, {"hash": digest, "status": "applied"})
        return {"status": "applied"}

    def _apply_refund(self, txn, event_id, p, digest):
        account_id, payment_id = p["account_id"], p["payment_id"]
        payment = txn.get(T_PAYMENTS, payment_id)

        if payment is None:
            # Refund before its payment: hold it; it is applied when the payment arrives.
            if txn.get(T_PENDING, payment_id) is not None:
                txn.put(T_EVENTS, event_id, {"hash": digest, "status": "ignored_duplicate_refund"})
                return {"status": "ignored", "reason": "refund already pending for payment"}
            txn.put(T_PENDING, payment_id, {
                "event_id": event_id, "hash": digest, "account_id": account_id,
                "amount": p["amount"], "currency": p["currency"], "created": p["created"]})
            txn.put(T_EVENTS, event_id, {"hash": digest, "status": "pending"})
            return {"status": "pending", "reason": "payment not yet received"}

        if (payment["account_id"] != account_id or payment["amount"] != p["amount"]
                or payment["currency"] != p["currency"]):
            raise _Reject(422, "refund does not match the original payment")

        if payment["refunded"]:
            txn.put(T_EVENTS, event_id, {"hash": digest, "status": "ignored_duplicate_refund"})
            return {"status": "ignored", "reason": "payment already refunded"}

        acct = txn.get(T_ACCOUNTS, account_id)
        key, entry = self._entry(acct, account_id, event_id, REFUND, -p["amount"], p)
        txn.put(T_ENTRIES, key, entry)
        payment["refunded"] = True
        txn.put(T_PAYMENTS, payment_id, payment)
        txn.put(T_ACCOUNTS, account_id, acct)
        txn.put(T_EVENTS, event_id, {"hash": digest, "status": "applied"})
        return {"status": "applied"}
