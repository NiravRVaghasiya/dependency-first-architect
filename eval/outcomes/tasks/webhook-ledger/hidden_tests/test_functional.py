"""Functional: the basic behavior BRIEF.md describes for well-formed deliveries."""

import unittest

from _support import LedgerTestCase, encode, is_2xx, is_4xx, payment, refund


class Functional(LedgerTestCase):
    def test_payment_credits_the_account(self):
        self.assertTrue(is_2xx(self.deliver(payment("evt_1"))))
        self.assertAccount("acct_1", 2500, [2500])

    def test_payments_accumulate_in_order(self):
        self.deliver(payment("evt_1", payment_id="pay_1", amount=2500))
        self.deliver(payment("evt_2", payment_id="pay_2", amount=700))
        self.assertAccount("acct_1", 3200, [2500, 700])
        entries = self.service.entries("acct_1")
        self.assertEqual([e["event_id"] for e in entries], ["evt_1", "evt_2"])
        self.assertEqual([e["type"] for e in entries], ["payment.succeeded"] * 2)

    def test_full_refund_takes_the_money_back(self):
        self.deliver(payment("evt_1"))
        self.assertTrue(is_2xx(self.deliver(refund("evt_2"))))
        self.assertAccount("acct_1", 0, [2500, -2500])
        self.assertEqual(self.service.entries("acct_1")[1]["type"], "refund.succeeded")

    def test_unknown_account(self):
        self.assertEqual(self.service.balance("acct_never_seen"), 0)
        self.assertEqual(self.service.entries("acct_never_seen"), [])

    def test_accounts_are_independent(self):
        self.deliver(payment("evt_1", account="acct_1", payment_id="pay_1", amount=100))
        self.deliver(payment("evt_2", account="acct_2", payment_id="pay_2", amount=900))
        self.assertAccount("acct_1", 100, [100])
        self.assertAccount("acct_2", 900, [900])

    def test_unhandled_event_type_is_acknowledged_and_ignored(self):
        # PayCo retries anything that is not 2xx for 72 hours; we will never handle this type.
        event = {"id": "evt_d1", "type": "dispute.created", "created": 1699999900,
                 "data": {"account_id": "acct_1", "payment_id": "pay_1", "amount": 2500,
                          "currency": "EUR"}}
        self.assertTrue(is_2xx(self.deliver(event)))
        self.assertAccount("acct_1", 0, [])

    def test_header_name_in_any_case(self):
        status = self.deliver(payment("evt_1"), header_name="payco-signature")
        self.assertTrue(is_2xx(status))
        self.assertAccount("acct_1", 2500, [2500])

    def test_malformed_json_is_rejected(self):
        status = self.deliver(None, raw=b'{"id": "evt_1", "type": ')
        self.assertTrue(is_4xx(status))
        self.assertAccount("acct_1", 0, [])

    def test_missing_field_is_rejected(self):
        event = payment("evt_1")
        del event["data"]["account_id"]
        self.assertTrue(is_4xx(self.deliver(event)))
        self.assertEqual(self.service.balance("acct_1"), 0)


if __name__ == "__main__":
    unittest.main()
