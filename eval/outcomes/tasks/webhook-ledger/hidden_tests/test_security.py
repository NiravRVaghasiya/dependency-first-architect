"""Security: BRIEF.md's signature scheme (HMAC over `<t>.` plus the exact body bytes), its
300-second replay window, and its rule for an event id that arrives with a different body."""

import unittest

from _support import NOW, LedgerTestCase, encode, is_2xx, is_4xx, payment, sign


class Security(LedgerTestCase):
    def test_wrong_signature_is_rejected(self):
        raw = encode(payment("evt_1"))
        forged = sign(raw, NOW, secret=b"not-our-secret")
        status, _ = self.service.handle(raw, {"PayCo-Signature": forged})
        self.assertTrue(is_4xx(status))
        self.assertEqual(self.service.balance("acct_1"), 0)

    def test_tampered_body_is_rejected(self):
        event = payment("evt_1", amount=100)
        signature = sign(encode(event))
        event["data"]["amount"] = 100000
        status, _ = self.service.handle(encode(event), {"PayCo-Signature": signature})
        self.assertTrue(is_4xx(status))
        self.assertEqual(self.service.balance("acct_1"), 0)

    def test_missing_signature_is_rejected(self):
        status, _ = self.service.handle(encode(payment("evt_1")), {})
        self.assertTrue(is_4xx(status))
        self.assertEqual(self.service.balance("acct_1"), 0)

    def test_malformed_signature_header_is_rejected(self):
        status, _ = self.service.handle(encode(payment("evt_1")), {"PayCo-Signature": "garbage"})
        self.assertTrue(is_4xx(status))
        self.assertEqual(self.service.balance("acct_1"), 0)

    def test_signature_covers_the_exact_bytes(self):
        # Valid JSON with unusual spacing and key order, signed exactly as sent.
        raw = (b'{ "type":"payment.succeeded" ,"data":{"currency":"EUR","amount":2500,'
               b'"payment_id":"pay_1","account_id":"acct_1"},  "created":1699999940,'
               b'"id":"evt_1" }')
        status, _ = self.service.handle(raw, {"PayCo-Signature": sign(raw)})
        self.assertTrue(is_2xx(status))
        self.assertEqual(self.service.balance("acct_1"), 2500)

    def test_stale_delivery_is_rejected(self):
        self.assertTrue(is_4xx(self.deliver(payment("evt_1"), t=NOW - 301)))
        self.assertEqual(self.service.balance("acct_1"), 0)

    def test_future_dated_delivery_is_rejected(self):
        self.assertTrue(is_4xx(self.deliver(payment("evt_1"), t=NOW + 301)))
        self.assertEqual(self.service.balance("acct_1"), 0)

    def test_deliveries_inside_the_window_are_accepted(self):
        self.assertTrue(is_2xx(self.deliver(payment("evt_1", payment_id="pay_1"), t=NOW - 299)))
        self.assertTrue(is_2xx(self.deliver(payment("evt_2", payment_id="pay_2"), t=NOW + 299)))
        self.assertEqual(self.service.balance("acct_1"), 5000)

    def test_the_window_uses_the_signature_time_not_the_event_time(self):
        # A retry hours after the event happened is signed when PayCo sends it, so it is fresh.
        old = payment("evt_1", created=NOW - 3 * 3600)
        self.assertTrue(is_2xx(self.deliver(old)))
        self.assertEqual(self.service.balance("acct_1"), 2500)

    def test_same_id_with_a_different_body_is_rejected(self):
        self.deliver(payment("evt_1", amount=2500))
        status = self.deliver(payment("evt_1", amount=9900))
        self.assertTrue(is_4xx(status))
        self.assertAccount("acct_1", 2500, [2500])


if __name__ == "__main__":
    unittest.main()
