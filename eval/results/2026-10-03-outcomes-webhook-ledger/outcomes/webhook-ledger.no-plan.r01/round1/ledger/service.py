"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""


import hashlib
import hmac
import json
import re
from urllib.parse import quote

from ledgerkit import StoreError

TOLERANCE_SECONDS = 300

# Tables
EVENTS = "events"            # event_id -> {hash, status, http_status, type, ...}
ACCOUNTS = "accounts"        # account_id -> {currency, balance}
ENTRIES = "entries"          # <quoted account>/<created>/<event id> -> entry
PAYMENTS = "payments"        # payment_id -> {account_id, amount, currency, refunded}
PENDING = "pending_refunds"  # payment_id -> refund that arrived before its payment

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


class _Reject(Exception):
    """Delivery is bad; nothing recorded."""

    def __init__(self, status, error):
        super().__init__(error)
        self.status = status
        self.error = error


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
        try:
            self._verify_signature(raw_body, headers)
        except _Reject as r:
            return r.status, {"error": r.error}

        try:
            event = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return 400, {"error": "invalid_json"}
        if not isinstance(event, dict) or not _nonempty_str(event.get("id")) \
                or not _nonempty_str(event.get("type")):
            return 400, {"error": "invalid_event"}

        try:
            with self._store.transaction() as txn:
                result = self._process(txn, event)
        except _Reject as r:
            return r.status, {"error": r.error}
        except StoreError:
            # The transaction was rolled back completely. A non-2xx makes PayCo retry.
            return 500, {"error": "temporary_failure"}
        return result

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._store.transaction() as txn:
            acct = txn.get(ACCOUNTS, account_id)
        return acct["balance"] if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        with self._store.transaction() as txn:
            rows = txn.scan(ENTRIES, prefix=self._entry_prefix(account_id))
        return [value for _, value in rows]

    # ------------------------------------------------------------------ signature

    def _verify_signature(self, raw_body, headers):
        header = None
        for name, value in (headers or {}).items():
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            if isinstance(name, str) and name.lower() == "payco-signature":
                if isinstance(value, bytes):
                    value = value.decode("latin-1")
                header = value
                break
        if not isinstance(header, str):
            raise _Reject(401, "missing_signature")
        if not isinstance(raw_body, (bytes, bytearray)):
            raise _Reject(400, "invalid_body")

        t_raw = None
        sigs = []
        for part in header.split(","):
            k, sep, v = part.strip().partition("=")
            if not sep:
                continue
            if k == "t" and t_raw is None:
                t_raw = v
            elif k == "v1":
                sigs.append(v)
        if t_raw is None or not re.fullmatch(r"[0-9]{1,15}", t_raw) or not sigs:
            raise _Reject(401, "malformed_signature")

        expected = hmac.new(self._secret, t_raw.encode("ascii") + b"." + bytes(raw_body),
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for s in sigs:
            try:
                if hmac.compare_digest(s.encode("ascii"), expected):
                    ok = True
            except UnicodeEncodeError:
                pass
        if not ok:
            raise _Reject(401, "bad_signature")
        if abs(self._clock.now() - int(t_raw)) > TOLERANCE_SECONDS:
            raise _Reject(401, "stale_timestamp")

    # ------------------------------------------------------------------ processing

    @staticmethod
    def _entry_prefix(account_id):
        return quote(str(account_id), safe="") + "/"

    @staticmethod
    def _digest(event):
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def _process(self, txn, event):
        event_id = event["id"]
        digest = self._digest(event)

        existing = txn.get(EVENTS, event_id)
        if existing is not None:
            if existing["hash"] != digest:
                # Security incident: same id, different body. Reject, touch nothing.
                return 409, {"error": "event_id_conflict", "event_id": event_id}
            if existing["http_status"] // 100 == 2:
                return 200, {"status": "duplicate", "event_id": event_id,
                             "original_status": existing["status"]}
            return existing["http_status"], {"error": existing["status"], "event_id": event_id}

        etype = event["type"]
        if etype not in ("payment.succeeded", "refund.succeeded"):
            # Not handled yet: acknowledge so PayCo stops retrying; keep the event for later.
            self._record_event(txn, event_id, digest, "ignored", 200, event)
            return 200, {"status": "ignored", "event_id": event_id}

        data = self._validate(event)
        if etype == "payment.succeeded":
            return self._payment(txn, event, data, digest)
        return self._refund(txn, event, data, digest)

    @staticmethod
    def _validate(event):
        data = event.get("data")
        created = event.get("created")
        if not isinstance(data, dict) or not _is_int(created) or not 0 <= created < 10 ** 15:
            raise _Reject(400, "invalid_event")
        amount = data.get("amount")
        if not (_nonempty_str(data.get("account_id")) and _nonempty_str(data.get("payment_id"))
                and _is_int(amount) and amount > 0
                and isinstance(data.get("currency"), str)
                and _CURRENCY_RE.match(data["currency"])):
            raise _Reject(400, "invalid_event")
        return data

    @staticmethod
    def _record_event(txn, event_id, digest, status, http_status, event=None, **extra):
        rec = {"hash": digest, "status": status, "http_status": http_status}
        if event is not None:
            rec["event"] = event
        rec.update(extra)
        txn.put(EVENTS, event_id, rec)

    def _apply(self, txn, event, data, sign):
        """Move money and write the entry. Does not touch events/payments tables."""
        acct_id = data["account_id"]
        acct = txn.get(ACCOUNTS, acct_id) or {"currency": data["currency"], "balance": 0}
        signed = sign * data["amount"]
        acct["balance"] += signed
        txn.put(ACCOUNTS, acct_id, acct)
        key = "%s%015d/%s" % (self._entry_prefix(acct_id), event["created"], event["id"])
        txn.put(ENTRIES, key, {
            "event_id": event["id"], "type": event["type"], "amount": signed,
            "currency": data["currency"], "payment_id": data["payment_id"],
            "account_id": acct_id, "created": event["created"],
        })

    def _reject_recorded(self, txn, event, digest, error):
        self._record_event(txn, event["id"], digest, error, 422)
        return 422, {"error": error, "event_id": event["id"]}

    def _payment(self, txn, event, data, digest):
        event_id = event["id"]
        pid = data["payment_id"]
        if txn.get(PAYMENTS, pid) is not None:
            # Same payment under a different event id: never credit twice.
            self._record_event(txn, event_id, digest, "ignored_duplicate_payment", 200)
            return 200, {"status": "ignored", "event_id": event_id}
        acct = txn.get(ACCOUNTS, data["account_id"])
        if acct is not None and acct["currency"] != data["currency"]:
            return self._reject_recorded(txn, event, digest, "currency_mismatch")

        self._apply(txn, event, data, +1)
        payment = {"account_id": data["account_id"], "amount": data["amount"],
                   "currency": data["currency"], "refunded": False}
        self._record_event(txn, event_id, digest, "applied", 200)

        pending = txn.get(PENDING, pid)
        if pending is not None:
            txn.delete(PENDING, pid)
            pevent = txn.get(EVENTS, pending["event_id"])
            if (pending["account_id"], pending["amount"], pending["currency"]) == \
                    (payment["account_id"], payment["amount"], payment["currency"]):
                revent = {"id": pending["event_id"], "type": "refund.succeeded",
                          "created": pending["created"]}
                rdata = {"account_id": pending["account_id"], "payment_id": pid,
                         "amount": pending["amount"], "currency": pending["currency"]}
                self._apply(txn, revent, rdata, -1)
                payment["refunded"] = True
                pevent["status"] = "applied"
            else:
                pevent["status"] = "rejected_mismatch"
                pevent["http_status"] = 422
            txn.put(EVENTS, pending["event_id"], pevent)
        txn.put(PAYMENTS, pid, payment)
        return 200, {"status": "applied", "event_id": event_id}

    def _refund(self, txn, event, data, digest):
        event_id = event["id"]
        pid = data["payment_id"]
        payment = txn.get(PAYMENTS, pid)
        if payment is None:
            if txn.get(PENDING, pid) is not None:
                self._record_event(txn, event_id, digest, "ignored_duplicate_refund", 200)
                return 200, {"status": "ignored", "event_id": event_id}
            # Refund before its payment: park it durably; it is applied when the payment lands.
            txn.put(PENDING, pid, {"event_id": event_id, "account_id": data["account_id"],
                                   "amount": data["amount"], "currency": data["currency"],
                                   "created": event["created"]})
            self._record_event(txn, event_id, digest, "pending", 200)
            return 200, {"status": "pending", "event_id": event_id}
        if payment["refunded"]:
            self._record_event(txn, event_id, digest, "ignored_duplicate_refund", 200)
            return 200, {"status": "ignored", "event_id": event_id}
        if (payment["account_id"], payment["amount"], payment["currency"]) != \
                (data["account_id"], data["amount"], data["currency"]):
            return self._reject_recorded(txn, event, digest, "refund_mismatch")

        self._apply(txn, event, data, -1)
        payment["refunded"] = True
        txn.put(PAYMENTS, pid, payment)
        self._record_event(txn, event_id, digest, "applied", 200)
        return 200, {"status": "applied", "event_id": event_id}
