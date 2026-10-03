"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design (details and decisions in NOTES.md):
  * Each delivery is processed in ONE store transaction, so a StoreError rolls back everything
    (nothing half-applied) and we answer 503 so PayCo retries.
  * Every write is idempotent and a redelivery re-runs the whole pipeline (no short-circuit).
  * The balance is never stored; it is the sum of entries keyed by (account_id, event_id).
"""

import hashlib
import hmac
import json
import logging
import math
import re
import threading

from ledgerkit import StoreError

_log = logging.getLogger("ledger")

TOLERANCE_SECONDS = 300
SIGNATURE_HEADER = "payco-signature"

# Tables.
EVENTS = "events"          # event_id -> raw body, sha256, type, state
INCIDENTS = "incidents"    # (event_id, sha256) -> conflicting raw body
ACCOUNTS = "accounts"      # account_id -> currency (fixed by first applied payment)
PAYMENTS = "payments"      # payment_id -> claim by the payment event that was applied
REFUNDS = "refunds"        # payment_id -> claim by the (single) refund event for that payment
ENTRIES = "entries"        # (account_id, event_id) -> signed money movement
QUARANTINE = "quarantine"  # event_id -> reason

_CURRENCY = re.compile(r"[A-Z]{3}")
_DIGITS = re.compile(r"[0-9]+")
_HEX = re.compile(r"[0-9a-f]+")


def _enc(*parts):
    """Self-delimiting (length-prefixed) key encoding: ids containing any character cannot
    collide, and enc(a) is a prefix of enc(a, b) only."""
    return "".join("%d:%s" % (len(p), p) for p in parts)


# --------------------------------------------------------------------------- signature

def _verify_signature(raw, headers, secret, now):
    value = None
    try:
        items = list(headers.items())
    except AttributeError:
        return False
    for name, val in items:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        if isinstance(name, str) and name.strip().lower() == SIGNATURE_HEADER:
            value = val
            break
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    if not isinstance(value, str):
        return False
    t_values, v1_values = [], []
    for part in value.split(","):
        key, sep, val = part.partition("=")
        if not sep:
            continue
        key, val = key.strip(), val.strip()
        if key == "t":
            t_values.append(val)
        elif key == "v1":
            v1_values.append(val)
    if len(t_values) != 1 or not v1_values or not _DIGITS.fullmatch(t_values[0]):
        return False
    t_str = t_values[0]
    expected = hmac.new(secret, t_str.encode("ascii") + b"." + raw, hashlib.sha256).hexdigest()
    ok = False
    for cand in v1_values:
        if _HEX.fullmatch(cand) and hmac.compare_digest(cand.encode("ascii"),
                                                        expected.encode("ascii")):
            ok = True
    if not ok:
        return False
    return abs(now - int(t_str)) <= TOLERANCE_SECONDS


# --------------------------------------------------------------------------- parsing

def _reject_constant(name):
    raise ValueError("non-finite number")


def _no_duplicate_keys(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _parse(raw):
    try:
        evt = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                         object_pairs_hook=_no_duplicate_keys)
    except Exception:
        return None
    if not isinstance(evt, dict):
        return None
    eid, etype = evt.get("id"), evt.get("type")
    if not (isinstance(eid, str) and eid and isinstance(etype, str) and etype):
        return None
    return evt


def _ident(v):
    return isinstance(v, str) and 0 < len(v) <= 255


def _validate_money_event(evt):
    """Return normalised fields for payment/refund events, or None if invalid."""
    data = evt.get("data")
    if not isinstance(data, dict):
        return None
    account_id, payment_id = data.get("account_id"), data.get("payment_id")
    amount, currency, created = data.get("amount"), data.get("currency"), evt.get("created")
    if not (_ident(account_id) and _ident(payment_id)):
        return None
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None
    if not (isinstance(currency, str) and _CURRENCY.fullmatch(currency)):
        return None
    if isinstance(created, bool) or not isinstance(created, (int, float)) \
            or (isinstance(created, float) and not math.isfinite(created)):
        return None
    return {"account_id": account_id, "payment_id": payment_id, "amount": amount,
            "currency": currency, "created": created}


# --------------------------------------------------------------------------- service

class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        self._store = store
        self._secret = bytes(secret)
        self._clock = clock
        self._lock = threading.RLock()
        self._handlers = {
            "payment.succeeded": self._on_payment,
            "refund.succeeded": self._on_refund,
        }

    # ---- public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        try:
            if isinstance(raw_body, (bytearray, memoryview)):
                raw_body = bytes(raw_body)
            if not isinstance(raw_body, bytes):
                return 400, {"error": "malformed"}
            with self._lock:
                return self._handle(raw_body, headers)
        except StoreError:
            _log.warning("store write failed; asking PayCo to retry")
            return 503, {"error": "store_unavailable"}
        except Exception:
            _log.exception("unexpected error handling delivery")
            return 500, {"error": "internal_error"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        return sum(e["amount"] for e in self._load_entries(account_id))

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        rows = sorted(self._load_entries(account_id),
                      key=lambda e: (e["effective_created"], e["rank"], e["event_id"]))
        return [{k: v for k, v in e.items() if k not in ("rank", "effective_created")}
                for e in rows]

    # ---- reads

    def _load_entries(self, account_id):
        with self._lock, self._store.transaction() as txn:
            return [v for _, v in txn.scan(ENTRIES, _enc(str(account_id)))]

    # ---- pipeline

    def _handle(self, raw, headers):
        if not _verify_signature(raw, headers, self._secret, self._clock.now()):
            _log.warning("rejected delivery: invalid signature")
            return 401, {"error": "invalid_signature"}
        evt = _parse(raw)
        if evt is None:
            return 400, {"error": "malformed"}
        eid, etype = evt["id"], evt["type"]
        digest = hashlib.sha256(raw).hexdigest()

        with self._store.transaction() as txn:
            existing = txn.get(EVENTS, eid)
            if existing is not None and existing.get("sha256") != digest:
                # Same id, different body: incident. Leave accounts untouched.
                _log.error("event_id_conflict event_id=%s stored_sha256=%s new_sha256=%s",
                           eid, existing.get("sha256"), digest)
                try:
                    txn.put(INCIDENTS, _enc(eid, digest),
                            {"event_id": eid, "sha256": digest,
                             "raw_body": raw.decode("utf-8", "replace")})
                except StoreError:
                    pass  # best effort; the 409 and the log line are what matter
                return 409, {"error": "event_id_conflict"}

            duplicate = existing is not None
            if not duplicate:
                txn.put(EVENTS, eid, {"sha256": digest, "type": etype, "state": "received",
                                      "raw_body": raw.decode("utf-8")})
            handler = self._handlers.get(etype)
            if handler is None:
                outcome = "ignored"  # unhandled type: acknowledged, raw body kept in EVENTS
            else:
                fields = _validate_money_event(evt)
                if fields is None:
                    outcome = self._quarantine(txn, eid, "INVALID_PAYLOAD",
                                               "data failed validation")
                else:
                    outcome = handler(txn, eid, fields)
            self._set_state(txn, eid, outcome)

        _log.info("event_id=%s type=%s outcome=%s duplicate=%s", eid, etype, outcome, duplicate)
        if duplicate:
            return 200, {"status": "duplicate", "outcome": outcome, "event_id": eid}
        return 200, {"status": outcome, "event_id": eid}

    # ---- helpers

    @staticmethod
    def _set_state(txn, eid, state):
        rec = txn.get(EVENTS, eid)
        if rec is not None and rec.get("state") != state:
            rec["state"] = state
            txn.put(EVENTS, eid, rec)

    @staticmethod
    def _quarantine(txn, eid, reason, detail=""):
        _log.warning("quarantined event_id=%s reason=%s", eid, reason)
        txn.put(QUARANTINE, eid, {"event_id": eid, "reason": reason, "detail": detail})
        return "quarantined"

    # ---- payment.succeeded

    def _on_payment(self, txn, eid, p):
        claim = txn.get(PAYMENTS, p["payment_id"])
        if claim is not None and claim["event_id"] != eid:
            return self._quarantine(txn, eid, "DUPLICATE_PAYMENT_ID", p["payment_id"])
        account = txn.get(ACCOUNTS, p["account_id"])
        if account is not None and account["currency"] != p["currency"]:
            return self._quarantine(txn, eid, "CURRENCY_MISMATCH", p["account_id"])
        if claim is None:
            claim = {"event_id": eid, "account_id": p["account_id"], "amount": p["amount"],
                     "currency": p["currency"], "created": p["created"]}
            txn.put(PAYMENTS, p["payment_id"], claim)
        if account is None:
            txn.put(ACCOUNTS, p["account_id"],
                    {"currency": p["currency"], "first_event_id": eid})
        txn.put(ENTRIES, _enc(p["account_id"], eid), {
            "event_id": eid, "type": "payment.succeeded", "amount": p["amount"],
            "currency": p["currency"], "payment_id": p["payment_id"],
            "account_id": p["account_id"], "created": p["created"],
            "effective_created": p["created"], "rank": 0})
        # A refund may have arrived first and be parked: apply it now.
        refund = txn.get(REFUNDS, p["payment_id"])
        if refund is not None:
            self._apply_refund(txn, p["payment_id"], refund, claim)
        return "applied"

    # ---- refund.succeeded

    def _on_refund(self, txn, eid, r):
        claim = txn.get(REFUNDS, r["payment_id"])
        if claim is not None and claim["event_id"] != eid:
            return self._quarantine(txn, eid, "DUPLICATE_REFUND", r["payment_id"])
        if claim is None:
            claim = {"event_id": eid, "account_id": r["account_id"], "amount": r["amount"],
                     "currency": r["currency"], "created": r["created"]}
            txn.put(REFUNDS, r["payment_id"], claim)
        payment = txn.get(PAYMENTS, r["payment_id"])
        if payment is None:
            return "pending"  # parked durably; applied when the payment arrives
        return self._apply_refund(txn, r["payment_id"], claim, payment)

    def _apply_refund(self, txn, payment_id, refund, payment):
        """Deterministic and idempotent; shared by the refund and payment sides."""
        reid = refund["event_id"]
        if (refund["account_id"] != payment["account_id"]
                or refund["amount"] != payment["amount"]
                or refund["currency"] != payment["currency"]):
            outcome = self._quarantine(txn, reid, "REFUND_MISMATCH", payment_id)
        else:
            txn.put(ENTRIES, _enc(refund["account_id"], reid), {
                "event_id": reid, "type": "refund.succeeded", "amount": -refund["amount"],
                "currency": refund["currency"], "payment_id": payment_id,
                "account_id": refund["account_id"], "created": refund["created"],
                "effective_created": max(refund["created"], payment["created"]), "rank": 1})
            outcome = "applied"
        self._set_state(txn, reid, outcome)
        return outcome
