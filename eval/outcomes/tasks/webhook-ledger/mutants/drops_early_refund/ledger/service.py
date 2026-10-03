"""MUTANT `drops_early_refund` (target category: ordering): the round-1 reference with one defect.
Acknowledges and drops a refund that arrives before its payment. The hidden tests must catch it; see mutants/README.md.

Design: verify the signature over the raw bytes, then do everything for one delivery in ONE
transaction, keyed by event id. A database failure rolls the whole delivery back and answers 503,
so PayCo retries it; a redelivery of an event already applied answers 200 and changes nothing.
"""

import hashlib
import hmac
import json

from ledgerkit import StoreError

TOLERANCE_S = 300
HANDLED = ("payment.succeeded", "refund.succeeded")


class Reject(Exception):
    """End the delivery with this status; nothing written so far is kept."""

    def __init__(self, status, error):
        super().__init__(error)
        self.status, self.error = status, error


def _header(headers, name):
    for key, value in (headers or {}).items():
        if isinstance(key, str) and key.lower() == name:
            return value
    return None


def _parse_signature(value):
    """(t, [v1 signatures]) from `t=<int>,v1=<hex>[,v1=...]`, or None if malformed."""
    if not isinstance(value, str):
        return None
    t, signatures = None, []
    for part in value.split(","):
        key, sep, val = part.strip().partition("=")
        if not sep:
            return None
        if key == "t":
            try:
                t = int(val)
            except ValueError:
                return None
        elif key == "v1":
            signatures.append(val)
    return (t, signatures) if t is not None and signatures else None


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class LedgerService:
    def __init__(self, store, secret, clock):
        self.store, self.secret, self.clock = store, bytes(secret), clock

    # --- webhook ---------------------------------------------------------------------------
    def handle(self, raw_body, headers):
        try:
            self._verify(raw_body, headers)
            event = self._parse(raw_body)
            with self.store.transaction() as txn:
                outcome = self._apply(txn, event, hashlib.sha256(raw_body).hexdigest())
            return 200, {"status": outcome}
        except Reject as exc:
            return exc.status, {"error": exc.error}
        except StoreError:
            return 503, {"error": "database unavailable; retry"}

    def _verify(self, raw_body, headers):
        parsed = _parse_signature(_header(headers, "payco-signature"))
        if parsed is None:
            raise Reject(400, "missing or malformed PayCo-Signature header")
        t, signatures = parsed
        expected = hmac.new(self.secret, f"{t}.".encode("ascii") + raw_body,
                            hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in signatures):
            raise Reject(401, "signature does not verify")
        if abs(self.clock.now() - t) > TOLERANCE_S:
            raise Reject(401, "signature timestamp outside the replay window")

    def _parse(self, raw_body):
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Reject(400, "body is not valid JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) \
                or not isinstance(event.get("type"), str):
            raise Reject(400, "event needs a string id and type")
        if event["type"] in HANDLED:
            data = event.get("data")
            if not isinstance(data, dict):
                raise Reject(400, "event needs a data object")
            for field in ("account_id", "payment_id", "currency"):
                if not isinstance(data.get(field), str) or not data[field]:
                    raise Reject(400, f"data.{field} must be a non-empty string")
            if not _is_int(data.get("amount")) or data["amount"] <= 0:
                raise Reject(400, "data.amount must be a positive integer of minor units")
        return event

    def _apply(self, txn, event, body_sha256):
        seen = txn.get("events", event["id"])
        if seen is not None:
            if seen["body_sha256"] != body_sha256:
                raise Reject(409, "event id seen before with a different body")
            return "duplicate"
        if event["type"] not in HANDLED:
            txn.put("events", event["id"], {"body_sha256": body_sha256, "outcome": "ignored"})
            return "ignored"
        data = event["data"]
        if event["type"] == "payment.succeeded":
            self._payment(txn, event, data)
        else:
            self._refund(txn, event, data)
        txn.put("events", event["id"], {"body_sha256": body_sha256, "outcome": "applied"})
        return "applied"

    def _payment(self, txn, event, data):
        account = txn.get("accounts", data["account_id"])
        if account is None:
            account = {"currency": data["currency"], "balance": 0, "seq": 0}
        if account["currency"] != data["currency"]:
            raise Reject(422, "currency differs from the account's currency")
        if txn.get("payments", data["payment_id"]) is not None:
            raise Reject(409, "payment id already recorded by another event")
        txn.put("payments", data["payment_id"], {"account_id": data["account_id"],
                                                 "amount": data["amount"], "refunded": 0})
        self._entry(txn, account, data["account_id"], event, data["amount"])

    def _refund(self, txn, event, data):
        payment = txn.get("payments", data["payment_id"])
        if payment is None:  # MUTANT: drops the early refund and lets it be acknowledged
            return
        if payment["account_id"] != data["account_id"]:
            raise Reject(422, "refund account differs from the payment's account")
        account = txn.get("accounts", data["account_id"])
        if account["currency"] != data["currency"]:
            raise Reject(422, "currency differs from the account's currency")
        self._check_refund(payment, data["amount"])
        payment["refunded"] += data["amount"]
        txn.put("payments", data["payment_id"], payment)
        self._entry(txn, account, data["account_id"], event, -data["amount"])

    def _check_refund(self, payment, amount):
        # Round 1: a refund is always the full payment, once.
        if payment["refunded"] or amount != payment["amount"]:
            raise Reject(422, "a refund must refund the whole payment, once")

    def _entry(self, txn, account, account_id, event, amount):
        account["seq"] += 1
        account["balance"] += amount
        txn.put("accounts", account_id, account)
        txn.put("entries", f"{account_id}/{account['seq']:012d}",
                {"event_id": event["id"], "type": event["type"], "amount": amount,
                 "payment_id": event["data"]["payment_id"], "created": event.get("created")})

    # --- reads -----------------------------------------------------------------------------
    def balance(self, account_id):
        with self.store.transaction() as txn:
            account = txn.get("accounts", account_id)
        return account["balance"] if account else 0

    def entries(self, account_id):
        with self.store.transaction() as txn:
            rows = txn.scan("entries", prefix=f"{account_id}/")
        return [value for _, value in rows]
