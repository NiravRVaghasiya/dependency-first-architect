"""Ledger service: ingests PayCo webhook deliveries and keeps account balances. See BRIEF.md."""

import logging

from ledgerkit import StoreError

from . import events
from .projection import project
from .signature import verify

log = logging.getLogger("ledger")

MAX_BODY = 64 * 1024
EVENTS = "events"            # event_id -> stored event (write-once)
ACCT_EVENTS = "acct_events"  # "<account>\0<event_id>" -> handled movement (write-once)
ACCOUNTS = "accounts"        # account_id -> {"currency"}
INCIDENTS = "incidents"      # "<event_id>/<digest>" -> evidence


class LedgerService:
    def __init__(self, store, secret, clock):
        """store: a ledgerkit.MemoryStore; secret: the PayCo webhook signing secret (bytes);
        clock: an object whose now() returns the current Unix time in seconds."""
        self._store = store
        self._secret = secret
        self._clock = clock

    # ------------------------------------------------------------------ webhook

    def handle(self, raw_body, headers):
        """Process one webhook delivery.

        raw_body: the request body exactly as received (bytes). headers: a dict of header name ->
        value; names may arrive in any letter case. Returns (status_code, body_dict); the HTTP
        layer sends status_code back to PayCo.

        2xx is returned only when the event is durably stored (or is an exact duplicate of one
        that is). Everything is written in a single transaction, so a StoreError rolls back the
        whole delivery and PayCo's retry starts from a clean slate.
        """
        try:
            return self._handle(raw_body, headers)
        except StoreError:
            return 503, {"error": "store_unavailable"}
        except Exception:
            log.exception("unexpected error handling delivery")
            return 500, {"error": "internal_error"}

    def _handle(self, raw_body, headers):
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        raw_body = bytes(raw_body)
        lowered = {}
        for k, v in (headers or {}).items():
            name = k.decode("latin-1") if isinstance(k, (bytes, bytearray)) else str(k)
            lowered[name.lower()] = v

        now = self._clock.now()
        if len(raw_body) > MAX_BODY:
            return 413, {"error": "body_too_large"}

        # Authenticate before looking inside the body.
        reason = verify(lowered.get("payco-signature"), raw_body, self._secret, now)
        if reason is not None:
            log.warning("rejected delivery: %s", reason)
            return 401, {"error": reason}

        try:
            obj = events.parse(raw_body)
        except events.Invalid as e:
            return 400, {"error": "invalid_payload", "detail": str(e)}
        event_id = obj["id"]
        dig = events.digest(obj)

        conflict = None
        with self._store.transaction() as txn:
            existing = txn.get(EVENTS, event_id)
            if existing is not None:
                if existing["digest"] != dig:
                    conflict = existing["digest"]
                    result = None
                else:
                    result = (200, {"status": "duplicate", "event_id": event_id})
            else:
                try:
                    fields = events.validate(obj)
                except events.Invalid as e:
                    return 400, {"error": "invalid_payload", "detail": str(e)}
                record = {
                    "event_id": event_id,
                    "type": obj["type"],
                    "digest": dig,
                    "raw_body": raw_body.decode("utf-8", errors="replace"),
                    "received_at": now,
                    "schema_version": 1,
                }
                outcome = "ignored"
                if fields is not None:
                    account_id = fields["account_id"]
                    record["account_id"] = account_id
                    record["created"] = fields["created"]
                    txn.put(EVENTS, event_id, record)
                    txn.put(ACCT_EVENTS, _akey(account_id, event_id), {
                        "account_id": account_id,
                        "event_id": event_id,
                        "type": obj["type"],
                        "payment_id": fields["payment_id"],
                        "amount": fields["amount"],
                        "currency": fields["currency"],
                        "created": fields["created"],
                    })
                    acct = txn.get(ACCOUNTS, account_id)
                    if acct is None and obj["type"] == "payment.succeeded":
                        # The account's currency is fixed by its first payment.
                        txn.put(ACCOUNTS, account_id, {"currency": fields["currency"]})
                    try:
                        _, status = self._load(txn, account_id)
                        outcome = status.get(event_id, "accepted")
                    except Exception:
                        outcome = "accepted"
                else:
                    txn.put(EVENTS, event_id, record)
                result = (200, {"status": outcome, "event_id": event_id})

        if conflict is not None:
            return self._incident(event_id, conflict, dig, raw_body, now)
        return result

    def _incident(self, event_id, existing_digest, new_digest, raw_body, now):
        """Same event id, different body: reject, leave accounts untouched, flag loudly."""
        log.error("security_incident: event id %s redelivered with a different body "
                  "(stored digest %s, received digest %s)", event_id, existing_digest, new_digest)
        try:  # best effort evidence; never affects the response
            with self._store.transaction() as txn:
                txn.put(INCIDENTS, "%s/%s" % (event_id, new_digest), {
                    "event_id": event_id,
                    "existing_digest": existing_digest,
                    "digest": new_digest,
                    "received_at": now,
                    "raw_body": raw_body.decode("utf-8", errors="replace"),
                })
        except StoreError:
            pass
        return 409, {"error": "event_id_conflict", "event_id": event_id}

    # -------------------------------------------------------------------- reads

    def _load(self, txn, account_id):
        acct = txn.get(ACCOUNTS, account_id)
        currency = acct["currency"] if acct else None
        prefix = account_id + "\x00"
        records = [v for _, v in txn.scan(ACCT_EVENTS, prefix=prefix)
                   if v.get("account_id") == account_id]
        return project(records, currency)

    def _project(self, account_id):
        with self._store.transaction() as txn:
            return self._load(txn, account_id)

    def balance(self, account_id):
        """The account's current balance in minor units (int); 0 for an unknown account."""
        entries, _ = self._project(account_id)
        return sum(e["amount"] for e in entries)

    def entries(self, account_id):
        """The money movements applied to the account, oldest first: a list of dicts, each with
        at least "event_id", "type" (the event's type) and "amount" (signed int: + payments,
        - refunds)."""
        entries, _ = self._project(account_id)
        return entries

    def pending(self, account_id):
        """Event ids of refunds waiting for their payment (ops aid)."""
        _, status = self._project(account_id)
        return sorted(e for e, s in status.items() if s == "pending")

    def held(self, account_id):
        """Event ids of anomalous events held for a human (ops aid)."""
        _, status = self._project(account_id)
        return sorted(e for e, s in status.items() if s == "held")


def _akey(account_id, event_id):
    return "%s\x00%s" % (account_id, event_id)
