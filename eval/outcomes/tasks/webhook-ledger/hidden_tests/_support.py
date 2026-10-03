"""Shared helpers for the hidden tests: build a service, sign and deliver events the way PayCo
does (BRIEF.md). Every expectation in the tests follows from BRIEF.md or CHANGE_REQUEST.md.

Deliveries are signed with the clock's current time, as PayCo signs each delivery when it sends
it. A PayCo retry or redelivery therefore comes later, with a fresh signature: tests advance the
clock (`later()`) before redelivering, rather than replaying the same bytes at the same instant.
"""

import hashlib
import hmac
import json
import unittest

from ledgerkit import FixedClock, MemoryStore
from ledger.service import LedgerService

SECRET = b"whsec_9f8e7d6c5b4a"
NOW = 1_700_000_000
RETRY_GAP_S = 240  # a PayCo retry arrives minutes later; inside the 300 s window either way


def make():
    """(service, store, clock) with a fresh store and a clock at NOW."""
    store, clock = MemoryStore(), FixedClock(NOW)
    return LedgerService(store, SECRET, clock), store, clock


def encode(event):
    """The body bytes PayCo would send for `event` (json.dumps with default separators)."""
    return json.dumps(event).encode("utf-8")


def sign(raw, t=NOW, secret=SECRET):
    digest = hmac.new(secret, f"{t}.".encode("ascii") + raw, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def deliver(service, event, t=None, header_name="PayCo-Signature", raw=None, clock=None):
    """Sign and deliver `event` (or the exact bytes `raw`); returns (status, body). The signature
    time is `t`, else the clock's current time, else NOW."""
    raw = encode(event) if raw is None else raw
    if t is None:
        t = int(clock.now()) if clock is not None else NOW
    return service.handle(raw, {header_name: sign(raw, t)})


def payment(event_id, account="acct_1", payment_id="pay_1", amount=2500, currency="EUR",
            created=NOW - 60):
    return {"id": event_id, "type": "payment.succeeded", "created": created,
            "data": {"account_id": account, "payment_id": payment_id, "amount": amount,
                     "currency": currency}}


def refund(event_id, account="acct_1", payment_id="pay_1", amount=2500, currency="EUR",
           created=NOW - 30):
    return {"id": event_id, "type": "refund.succeeded", "created": created,
            "data": {"account_id": account, "payment_id": payment_id, "amount": amount,
                     "currency": currency}}


def is_2xx(status):
    return isinstance(status, int) and 200 <= status < 300


def is_4xx(status):
    return isinstance(status, int) and 400 <= status < 500


def state(service, account="acct_1"):
    """What finance can see for an account: (balance, amounts of its entries in order)."""
    return service.balance(account), [e["amount"] for e in service.entries(account)]


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.service, self.store, self.clock = make()

    def deliver(self, event, **kwargs):
        kwargs.setdefault("clock", self.clock)
        result = deliver(self.service, event, **kwargs)
        self.assertIsInstance(result, tuple, "handle() must return (status, body)")
        self.assertEqual(len(result), 2, "handle() must return (status, body)")
        status, body = result
        self.assertIsInstance(status, int)
        self.assertIsInstance(body, dict)
        return status

    def later(self, seconds=RETRY_GAP_S):
        """Let time pass before the next delivery, as between PayCo's retries."""
        self.clock.advance(seconds)

    def restart(self):
        """A new service process on the same database (a deploy or crash between deliveries)."""
        self.service = LedgerService(self.store, SECRET, self.clock)

    def assertAccount(self, account, balance, amounts):
        self.assertEqual(self.service.balance(account), balance)
        self.assertEqual([e["amount"] for e in self.service.entries(account)], amounts)
