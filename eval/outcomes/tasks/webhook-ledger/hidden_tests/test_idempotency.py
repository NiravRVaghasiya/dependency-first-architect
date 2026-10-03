"""Idempotency: BRIEF.md says PayCo may deliver the same event more than once, even after a 2xx,
and finance must reconcile to the minor unit. A redelivery must never move money twice, also
when it arrives after the service restarted."""

import unittest

from _support import LedgerTestCase, is_2xx, payment, refund


class Idempotency(LedgerTestCase):
    def test_redelivered_payment_counts_once(self):
        self.assertTrue(is_2xx(self.deliver(payment("evt_1"))))
        self.later()
        self.assertTrue(is_2xx(self.deliver(payment("evt_1"))))
        self.assertAccount("acct_1", 2500, [2500])

    def test_five_deliveries_count_once(self):
        for _ in range(5):
            self.assertTrue(is_2xx(self.deliver(payment("evt_1"))))
            self.later(60)
        self.assertAccount("acct_1", 2500, [2500])

    def test_redelivery_after_other_events(self):
        self.deliver(payment("evt_1", payment_id="pay_1", amount=2500))
        self.later()
        self.deliver(payment("evt_2", payment_id="pay_2", amount=300))
        self.later()
        self.deliver(payment("evt_1", payment_id="pay_1", amount=2500))
        self.assertAccount("acct_1", 2800, [2500, 300])

    def test_redelivered_refund_counts_once(self):
        self.deliver(payment("evt_1"))
        self.assertTrue(is_2xx(self.deliver(refund("evt_2"))))
        self.later()
        self.assertTrue(is_2xx(self.deliver(refund("evt_2"))))
        self.assertAccount("acct_1", 0, [2500, -2500])

    def test_redelivery_after_a_restart_counts_once(self):
        self.assertTrue(is_2xx(self.deliver(payment("evt_1"))))
        self.restart()
        self.later()
        self.assertTrue(is_2xx(self.deliver(payment("evt_1"))))
        self.assertAccount("acct_1", 2500, [2500])

    def test_redelivered_unhandled_event_is_harmless(self):
        event = {"id": "evt_p1", "type": "payout.paid", "created": 1699999900,
                 "data": {"account_id": "acct_1", "payment_id": "po_1", "amount": 2500,
                          "currency": "EUR"}}
        self.assertTrue(is_2xx(self.deliver(event)))
        self.later()
        self.assertTrue(is_2xx(self.deliver(event)))
        self.assertEqual(self.service.balance("acct_1"), 0)


if __name__ == "__main__":
    unittest.main()
