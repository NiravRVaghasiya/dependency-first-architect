"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md.

Design (details in NOTES.md): every delivery is handled in ONE store transaction, so the inbox
record, the outcome and the ledger entries commit together or not at all. A StoreError rolls the
whole transaction back and is answered with 503, so PayCo's retry re-does the event cleanly.

Tables:
  inbox        event id -> {hash, raw, type, outcome, reason, ...}   (one row per verified event id)
  accounts     account id -> {currency, seq}
  entries      "<quoted account>/<seq>" -> entry dict                (append-only)
  payments     payment id -> {event_id, account_id, amount, currency}  (applied payments)
  refunds      payment id -> {total, last_event_id}                   (total refunded so far)
  bad_payments payment id -> {event_id}                               (quarantined payments)
  parked       "<quoted payment>/<quoted event>" -> refund fields     (refunds awaiting payment)
  incidents    "<event id>/<hash>" -> {...}                           (same id, different body)
"""

import base64
import hashlib
import hmac
import json
import re
from urllib.parse import quote

from ledgerkit import StoreError

TOLERANCE_SECONDS = 300
SIGNATURE_HEADER = "payco-signature"
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_DIGITS_RE = re.compile(r"[0-9]{1,15}")
_HEX_RE = re.compile(r"[0-9a-f]{64}")

PAYMENT = "payment.succeeded"
REFUND = "refund.succeeded"


class _Reject(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body


def _unauthorized():
    return _Reject(401, {"status": "rejected", "error": "invalid signature"})


def _reject_non_finite(_constant):
    raise ValueError("non-finite number")


def _canonical_hash(obj):
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_id(value):
    return isinstance(value, str) and value != ""


def _q(part):
    return quote(part, safe="")


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        self._store = store
        self._secret = bytes(secret)
        self._clock = clock

    # ------------------------------------------------------------------ public API

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.
        """
        try:
            if isinstance(raw_body, str):
                raw_body = raw_body.encode("utf-8")
            raw_body = bytes(raw_body)
            self._verify(raw_body, headers)          # before anything is parsed or stored
            event = self._parse(raw_body)
            with self._store.transaction() as txn:
                result = self._process(txn, event, raw_body)
            return result
        except _Reject as rej:
            return rej.status, rej.body
        except StoreError:
            # The transaction rolled back completely; nothing of this delivery is visible.
            return 503, {"status": "error", "error": "storage unavailable, retry"}

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        return sum(e["amount"] for e in self.entries(account_id))

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        if not isinstance(account_id, str):
            return []
        with self._store.transaction() as txn:
            rows = txn.scan("entries", _q(account_id) + "/")
        return [value for _key, value in rows]

    # ------------------------------------------------------------------ verification

    def _header(self, headers, name):
        found = None
        for key, value in (headers or {}).items():
            if isinstance(key, bytes):
                key = key.decode("latin-1")
            if isinstance(key, str) and key.strip().lower() == name:
                if isinstance(value, bytes):
                    value = value.decode("latin-1")
                if isinstance(value, str):
                    found = value
        return found

    def _verify(self, raw_body, headers):
        header = self._header(headers, SIGNATURE_HEADER)
        if header is None:
            raise _unauthorized()
        t_values = []
        v1_values = []
        for part in header.split(","):
            name, sep, value = part.partition("=")
            if not sep:
                continue
            name, value = name.strip(), value.strip()
            if name == "t":
                t_values.append(value)
            elif name == "v1":
                v1_values.append(value)
        if len(t_values) != 1 or not v1_values or not _DIGITS_RE.fullmatch(t_values[0]):
            raise _unauthorized()
        t_text = t_values[0]
        expected = hmac.new(self._secret, t_text.encode("ascii") + b"." + raw_body,
                            hashlib.sha256).hexdigest().encode("ascii")
        ok = False
        for candidate in v1_values:
            try:
                cand = candidate.encode("ascii")
            except UnicodeEncodeError:
                continue
            if hmac.compare_digest(cand, expected):
                ok = True
        if not ok:
            raise _unauthorized()
        if abs(self._clock.now() - int(t_text)) > TOLERANCE_SECONDS:
            raise _unauthorized()

    def _parse(self, raw_body):
        try:
            event = json.loads(raw_body, parse_constant=_reject_non_finite)
        except (ValueError, RecursionError):
            raise _Reject(400, {"status": "rejected", "error": "body is not valid JSON"})
        if not isinstance(event, dict) or not _is_id(event.get("id")):
            raise _Reject(400, {"status": "rejected", "error": "missing event id"})
        return event

    # ------------------------------------------------------------------ processing

    def _process(self, txn, event, raw_body):
        eid = event["id"]
        digest = _canonical_hash(event)
        record = txn.get("inbox", eid)

        if record is not None:
            if record["hash"] != digest:
                # Same event id, different body: incident. Reject, leave accounts untouched.
                key = eid + "/" + digest
                if txn.get("incidents", key) is None:
                    txn.put("incidents", key, {
                        "event_id": eid, "hash": digest, "original_hash": record["hash"],
                        "raw": base64.b64encode(raw_body).decode("ascii"),
                        "received": int(self._clock.now()),
                    })
                return 409, {"status": "conflict", "event_id": eid,
                             "error": "event id already seen with a different body"}
            if record["outcome"] in ("parked", "rejected"):
                # Roll forward: the payment may have arrived since (parked); a rejected
                # over-refund is re-evaluated and normally stays rejected.
                self._settle_parked_refund(txn, record["fields"], record)
                record = txn.get("inbox", eid)
            if record["outcome"] == "rejected":
                return 422, {"status": "rejected", "event_id": eid, "error": record["reason"]}
            return 200, {"status": "duplicate", "event_id": eid, "outcome": record["outcome"]}

        record = {
            "hash": digest,
            "raw_hash": hashlib.sha256(raw_body).hexdigest(),
            "raw": base64.b64encode(raw_body).decode("ascii"),
            "type": event.get("type") if isinstance(event.get("type"), str) else None,
            "received": int(self._clock.now()),
            "outcome": None,
            "reason": None,
        }
        etype = event.get("type")
        if not isinstance(etype, str) or etype == "":
            return self._finish(txn, eid, record, "quarantined", "missing or invalid type")
        if etype not in (PAYMENT, REFUND):
            return self._finish(txn, eid, record, "ignored", "unhandled event type")

        fields, problem = self._validate(event)
        if problem:
            if etype == PAYMENT:
                self._mark_bad_payment(txn, event)
            return self._finish(txn, eid, record, "quarantined", problem)

        if etype == PAYMENT:
            return self._apply_payment(txn, eid, record, fields)
        return self._apply_refund(txn, eid, record, fields)

    def _validate(self, event):
        data = event.get("data")
        if not isinstance(data, dict):
            return None, "data is not an object"
        created = event.get("created")
        if not _is_int(created):
            return None, "created is not an integer"
        for name in ("account_id", "payment_id"):
            if not _is_id(data.get(name)):
                return None, name + " is missing or not a string"
        amount = data.get("amount")
        if not _is_int(amount) or amount <= 0:
            return None, "amount is not a positive integer"
        currency = data.get("currency")
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            return None, "currency is not an ISO 4217 code"
        return {
            "event_id": event["id"], "type": event["type"], "created": created,
            "account_id": data["account_id"], "payment_id": data["payment_id"],
            "amount": amount, "currency": currency,
        }, None

    def _finish(self, txn, eid, record, outcome, reason=None):
        record["outcome"] = outcome
        record["reason"] = reason
        txn.put("inbox", eid, record)
        body = {"status": outcome, "event_id": eid}
        if outcome == "rejected":
            body["error"] = reason
            return 422, body
        if reason and outcome == "quarantined":
            body["reason"] = reason
        return 200, body

    def _append_entry(self, txn, fields, signed_amount, currency):
        account = txn.get("accounts", fields["account_id"]) or {"currency": currency, "seq": 0}
        account["seq"] += 1
        txn.put("accounts", fields["account_id"], account)
        txn.put("entries", "%s/%012d" % (_q(fields["account_id"]), account["seq"]), {
            "event_id": fields["event_id"], "type": fields["type"], "amount": signed_amount,
            "currency": currency, "payment_id": fields["payment_id"],
            "created": fields["created"], "account_id": fields["account_id"],
            "seq": account["seq"],
        })

    # payments ----------------------------------------------------------------------

    def _mark_bad_payment(self, txn, event):
        data = event.get("data")
        pid = data.get("payment_id") if isinstance(data, dict) else None
        if _is_id(pid) and txn.get("payments", pid) is None and txn.get("bad_payments", pid) is None:
            txn.put("bad_payments", pid, {"event_id": event["id"]})
            self._release_parked(txn, pid)

    def _apply_payment(self, txn, eid, record, f):
        if txn.get("payments", f["payment_id"]) is not None:
            return self._finish(txn, eid, record, "quarantined",
                                "payment_id already used by another event")
        account = txn.get("accounts", f["account_id"])
        if account is not None and account["currency"] != f["currency"]:
            if txn.get("bad_payments", f["payment_id"]) is None:
                txn.put("bad_payments", f["payment_id"], {"event_id": eid})
            result = self._finish(txn, eid, record, "quarantined",
                                  "currency differs from the account currency")
            self._release_parked(txn, f["payment_id"])
            return result
        self._append_entry(txn, f, f["amount"], f["currency"])
        txn.put("payments", f["payment_id"], {
            "event_id": eid, "account_id": f["account_id"],
            "amount": f["amount"], "currency": f["currency"]})
        result = self._finish(txn, eid, record, "applied")
        self._release_parked(txn, f["payment_id"])
        return result

    # refunds -----------------------------------------------------------------------

    def _refund_verdict(self, txn, f):
        """('apply'|'park'|'quarantine'|'reject', reason) for a valid refund, given current state.

        Partial refunds: several refunds may apply to one payment as long as their total never
        exceeds the payment's amount; a refund that would exceed it is 'reject' (HTTP 422)."""
        payment = txn.get("payments", f["payment_id"])
        if payment is None:
            if txn.get("bad_payments", f["payment_id"]) is not None:
                return "quarantine", "refund of a quarantined payment"
            return "park", None
        if payment["account_id"] != f["account_id"] or payment["currency"] != f["currency"]:
            return "quarantine", "refund does not match its payment"
        return self._cap_verdict(txn, payment, f)

    def _refunded_total(self, txn, payment_id, payment):
        row = txn.get("refunds", payment_id)
        if row is None:
            return 0
        # Rows written before partial refunds existed had no "total"; those were full refunds.
        return row["total"] if "total" in row else payment["amount"]

    def _cap_verdict(self, txn, payment, f):
        if self._refunded_total(txn, f["payment_id"], payment) + f["amount"] > payment["amount"]:
            return "reject", "refund would exceed the payment amount"
        return "apply", None

    def _apply_refund_entry(self, txn, f):
        payment = txn.get("payments", f["payment_id"])
        total = self._refunded_total(txn, f["payment_id"], payment) + f["amount"]
        self._append_entry(txn, f, -f["amount"], f["currency"])
        txn.put("refunds", f["payment_id"], {"total": total, "last_event_id": f["event_id"]})

    def _apply_refund(self, txn, eid, record, f):
        verdict, reason = self._refund_verdict(txn, f)
        if verdict == "park":
            record["fields"] = f
            txn.put("parked", "%s/%s" % (_q(f["payment_id"]), _q(eid)), f)
            return self._finish(txn, eid, record, "parked")
        if verdict == "quarantine":
            return self._finish(txn, eid, record, "quarantined", reason)
        if verdict == "reject":
            # Over-refund: no entry, no change to the refunded total. The inbox row keeps the
            # evidence for finance; the delivery is answered 422.
            record["fields"] = f
            return self._finish(txn, eid, record, "rejected", reason)
        self._apply_refund_entry(txn, f)
        return self._finish(txn, eid, record, "applied")

    def _settle_parked_refund(self, txn, f, record):
        """Re-evaluate a parked (or previously rejected) refund already in the inbox; update its
        outcome. A rejected refund stays rejected unless the state now lets it apply."""
        was_parked = record["outcome"] == "parked"
        verdict, reason = self._refund_verdict(txn, f)
        if verdict == "park":
            return
        if verdict == "reject" and not was_parked:
            return  # still rejected; nothing to write
        if verdict == "apply":
            self._apply_refund_entry(txn, f)
            record["outcome"], record["reason"] = "applied", None
        elif verdict == "reject":
            record["outcome"], record["reason"] = "rejected", reason
        else:
            record["outcome"], record["reason"] = "quarantined", reason
        txn.put("inbox", f["event_id"], record)
        if was_parked:
            txn.delete("parked", "%s/%s" % (_q(f["payment_id"]), _q(f["event_id"])))

    def _release_parked(self, txn, payment_id):
        # Deterministic order (event time, then id) so the outcome does not depend on key order.
        parked = txn.scan("parked", _q(payment_id) + "/")
        parked.sort(key=lambda kv: (kv[1]["created"], kv[1]["event_id"]))
        for _key, f in parked:
            record = txn.get("inbox", f["event_id"])
            if record is not None and record["outcome"] == "parked":
                self._settle_parked_refund(txn, f, record)
            else:
                txn.delete("parked", _key)
