"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Every delivery is applied inside ONE store transaction, so it either commits completely or not
at all. A StoreError (or any other exception) rolls the transaction back and the caller gets a
non-2xx status, so PayCo retries. A 2xx is only returned after the transaction committed.
"""

import base64
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

T_EVENTS = "events"            # event_id -> record (commit/dedupe record for every accepted event)
T_ACCOUNTS = "accounts"        # account_id -> {"balance", "currency"}
T_PAYMENTS = "payments"        # payment_id -> credited payment (+ which refund took it back)
T_PENDING = "pending_refunds"  # quote(payment_id)/created/event_id -> refund waiting for its payment
T_ENTRIES = "entries"          # quote(account)/created/quote(event_id) -> money movement
T_MALFORMED = "malformed"      # sha256(body) -> signed bodies we could not even identify

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"

_CURRENCY = re.compile(r"[A-Z]{3}")
_DIGITS = re.compile(r"[0-9]{1,15}")
_HEX = re.compile(r"[0-9a-f]+")


def _q(s):
    return quote(s, safe="")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


class _Conflict(Exception):
    """Same event id, different body."""


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        if isinstance(secret, str):
            secret = secret.encode()
        self._store = store
        self._secret = bytes(secret)
        self._clock = clock
        # MemoryStore is single-threaded and refuses nested/overlapping transactions.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ handle

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo. Never raises.
        """
        try:
            return self._handle(raw_body, headers)
        except StoreError:
            log.warning("store error while handling delivery; answering 503")
            return 503, {"error": "store unavailable, retry"}
        except Exception:  # noqa: BLE001 - handle() must never raise to the HTTP layer
            log.exception("unexpected error while handling delivery; answering 500")
            return 500, {"error": "internal error, retry"}

    def _handle(self, raw_body, headers):
        if isinstance(raw_body, (bytearray, memoryview)):
            raw_body = bytes(raw_body)
        if not isinstance(raw_body, bytes):
            return 400, {"error": "body must be bytes"}

        # 1. authenticate: signature over the raw bytes, then freshness. Nothing is parsed or
        #    stored before this passes.
        err = self._verify(raw_body, headers)
        if err is not None:
            return 401, {"error": err}

        # 2. parse
        sha = hashlib.sha256(raw_body).hexdigest()
        now = int(self._clock.now())
        try:
            ev = json.loads(raw_body)
        except Exception:  # noqa: BLE001 - bad UTF-8, bad JSON, recursion...
            ev = None
        if not isinstance(ev, dict) or not _nonempty_str(ev.get("id")) \
                or not _nonempty_str(ev.get("type")):
            return self._store_malformed(raw_body, sha, now)

        # 3. look up / apply, all in one transaction
        try:
            with self._lock, self._store.transaction() as txn:
                status, extra = self._apply(txn, ev, sha, raw_body, now)
        except _Conflict:
            log.error("INCIDENT: event %s redelivered with a different body", ev["id"])
            return 409, {"error": "event id reused with a different body", "event_id": ev["id"]}
        body = {"status": status, "event_id": ev["id"]}
        body.update(extra)
        log.info("event=%s type=%s outcome=%s", ev["id"], ev["type"], status)
        # "rejected" (refund would exceed its payment) is the only outcome that is not a 2xx.
        return (422 if status == "rejected" else 200), body

    # --------------------------------------------------------------- signature

    def _verify(self, raw_body, headers):
        value = None
        try:
            for name, v in headers.items():
                if isinstance(name, str) and name.lower() == "payco-signature":
                    value = v
                    break
        except AttributeError:
            return "bad headers"
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                return "bad signature header"
        if not isinstance(value, str) or not value:
            return "missing signature header"

        t_vals, sigs = [], []
        for part in value.split(","):
            k, sep, v = part.strip().partition("=")
            if not sep:
                return "bad signature header"
            if k == "t":
                t_vals.append(v)
            elif k == "v1":
                sigs.append(v)
        if len(t_vals) != 1 or not _DIGITS.fullmatch(t_vals[0]) or not sigs:
            return "bad signature header"
        t = t_vals[0]

        expected = hmac.new(self._secret, t.encode() + b"." + raw_body, hashlib.sha256) \
            .hexdigest().encode()
        ok = False
        for s in sigs:
            if not _HEX.fullmatch(s):
                continue
            if hmac.compare_digest(s.encode(), expected):
                ok = True
        if not ok:
            return "signature mismatch"

        # Freshness uses the signed header t, never the body's "created".
        if abs(self._clock.now() - int(t)) > TOLERANCE_SECONDS:
            return "timestamp outside tolerance"
        return None

    # ------------------------------------------------------------------- apply

    def _store_malformed(self, raw_body, sha, now):
        """Signed but unidentifiable body: retrying the same bytes can never help, so keep it
        for inspection and answer 200 (quarantined)."""
        with self._lock, self._store.transaction() as txn:
            if txn.get(T_MALFORMED, sha) is None:
                txn.put(T_MALFORMED, sha, {
                    "raw": base64.b64encode(raw_body).decode(), "received_at": now})
        log.error("quarantined signed delivery without usable id/type sha256=%s", sha)
        return 200, {"status": "quarantined", "reason": "unidentifiable event"}

    def _apply(self, txn, ev, sha, raw_body, now):
        eid = ev["id"]
        existing = txn.get(T_EVENTS, eid)
        if existing is not None:
            if existing.get("sha256") == sha:
                if existing.get("status") == "rejected":
                    # Same refusal as the first time: never a 2xx for an over-refund.
                    return "rejected", {"reason": existing.get("reason")}
                return "duplicate", {}
            raise _Conflict()

        etype = ev["type"]
        created = ev.get("created")
        rec = {
            "sha256": sha,
            "raw": base64.b64encode(raw_body).decode(),
            "type": etype,
            "created": created if _is_int(created) else None,
            "received_at": now,
            "status": "ignored",
            "reason": None,
        }

        if etype not in (PAYMENT, REFUND):
            txn.put(T_EVENTS, eid, rec)
            return "ignored", {}

        fields, reason = self._validate(ev)
        if fields is None:
            rec["status"], rec["reason"] = "quarantined", reason
            txn.put(T_EVENTS, eid, rec)
            log.error("quarantined event %s: %s", eid, reason)
            return "quarantined", {"reason": reason}
        rec.update(account_id=fields["account_id"], payment_id=fields["payment_id"],
                   amount=fields["amount"], currency=fields["currency"])

        if etype == PAYMENT:
            status, reason = self._apply_payment(txn, eid, fields)
        else:
            status, reason = self._apply_refund(txn, eid, fields)
        rec["status"], rec["reason"] = status, reason
        txn.put(T_EVENTS, eid, rec)
        if status in ("quarantined", "rejected"):
            log.error("%s event %s: %s", status, eid, reason)
        return status, ({"reason": reason} if reason else {})

    @staticmethod
    def _validate(ev):
        created = ev.get("created")
        data = ev.get("data")
        if not _is_int(created) or created < 0:
            return None, "invalid created"
        if not isinstance(data, dict):
            return None, "invalid data"
        for k in ("account_id", "payment_id"):
            if not _nonempty_str(data.get(k)):
                return None, "invalid " + k
        amount = data.get("amount")
        if not _is_int(amount) or amount <= 0:
            return None, "invalid amount"
        currency = data.get("currency")
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            return None, "invalid currency"
        return {"type": ev["type"], "created": created, "account_id": data["account_id"],
                "payment_id": data["payment_id"], "amount": amount, "currency": currency}, None

    def _credit(self, txn, eid, f, signed_amount):
        """Record one money movement and move the account balance, in the caller's txn."""
        acct = txn.get(T_ACCOUNTS, f["account_id"]) or {"balance": 0, "currency": f["currency"]}
        acct["balance"] += signed_amount
        txn.put(T_ACCOUNTS, f["account_id"], acct)
        key = "%s/%020d/%s" % (_q(f["account_id"]), f["created"], _q(eid))
        txn.put(T_ENTRIES, key, {
            "event_id": eid, "type": f["type"], "amount": signed_amount,
            "created": f["created"], "payment_id": f["payment_id"],
            "currency": f["currency"], "account_id": f["account_id"]})

    def _apply_payment(self, txn, eid, f):
        if txn.get(T_PAYMENTS, f["payment_id"]) is not None:
            return "quarantined", "second credit for payment_id"
        acct = txn.get(T_ACCOUNTS, f["account_id"])
        if acct is not None and acct["currency"] != f["currency"]:
            return "quarantined", "currency differs from account currency"

        self._credit(txn, eid, f, f["amount"])
        pay = {"event_id": eid, "account_id": f["account_id"], "amount": f["amount"],
               "currency": f["currency"], "refunded": 0}
        txn.put(T_PAYMENTS, f["payment_id"], pay)

        # Refunds that arrived before this payment can be settled now (oldest first).
        for key, pr in txn.scan(T_PENDING, _q(f["payment_id"]) + "/"):
            status, reason = self._settle_refund(txn, pay, f["payment_id"], pr)
            txn.delete(T_PENDING, key)
            rrec = txn.get(T_EVENTS, pr["event_id"])
            if rrec is not None:
                rrec["status"], rrec["reason"] = status, reason
                txn.put(T_EVENTS, pr["event_id"], rrec)
            if status in ("quarantined", "rejected"):
                log.error("%s pending refund %s: %s", status, pr["event_id"], reason)
        return "applied", None

    def _apply_refund(self, txn, eid, f):
        pay = txn.get(T_PAYMENTS, f["payment_id"])
        if pay is None:
            key = "%s/%020d/%s" % (_q(f["payment_id"]), f["created"], _q(eid))
            txn.put(T_PENDING, key, dict(f, event_id=eid))
            return "pending", None
        return self._settle_refund(txn, pay, f["payment_id"], dict(f, event_id=eid))

    def _settle_refund(self, txn, pay, payment_id, r):
        """Apply one (possibly partial) refund against its payment, in the caller's txn.
        Returns "applied", "quarantined" (account/currency mismatch) or "rejected" (the running
        total of refunds would exceed the payment's amount; nothing is written to the account)."""
        if r["account_id"] != pay["account_id"]:
            return "quarantined", "refund account differs from payment account"
        if r["currency"] != pay["currency"]:
            return "quarantined", "refund currency differs from payment currency"
        refunded = pay.get("refunded", 0)
        if refunded + r["amount"] > pay["amount"]:
            return "rejected", "refund would exceed payment amount"
        self._credit(txn, r["event_id"], r, -r["amount"])
        pay["refunded"] = refunded + r["amount"]
        txn.put(T_PAYMENTS, payment_id, pay)
        return "applied", None

    # ------------------------------------------------------------------- reads

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        with self._lock, self._store.transaction() as txn:
            acct = txn.get(T_ACCOUNTS, account_id)
        return int(acct["balance"]) if acct else 0

    def entries(self, account_id):
        """The money movements applied to the account, oldest first (by event `created`, then
        event id): a list of dicts, each with at least "event_id", "type" (the event's type) and
        "amount" (signed int: + payments, - refunds)."""
        with self._lock, self._store.transaction() as txn:
            rows = txn.scan(T_ENTRIES, _q(account_id) + "/")
        return [v for _, v in rows]
