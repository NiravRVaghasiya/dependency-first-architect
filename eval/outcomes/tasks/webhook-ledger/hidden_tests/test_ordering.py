"""Ordering: BRIEF.md says delivery order is not guaranteed and a refund can arrive before its
payment. Whatever the service answers, simulating PayCo (which redelivers, later, anything not
acknowledged with a 2xx, and may redeliver anyway) must end with the right balance."""

import unittest

from _support import LedgerTestCase, is_2xx, payment, refund


class Ordering(LedgerTestCase):
    def test_refund_before_its_payment_is_not_lost_or_doubled(self):
        first = self.deliver(refund("evt_r"))
        self.later()
        self.assertTrue(is_2xx(self.deliver(payment("evt_p"))))
        self.later()
        # PayCo redelivers the refund: always if the first answer was not 2xx, and possibly anyway.
        self.assertTrue(is_2xx(self.deliver(refund("evt_r"))))
        if not is_2xx(first):
            self.later()
            self.deliver(refund("evt_r"))
        self.assertEqual(self.service.balance("acct_1"), 0)
        amounts = sorted(e["amount"] for e in self.service.entries("acct_1"))
        self.assertEqual(amounts, [-2500, 2500])

    def test_payments_delivered_out_of_order_both_count(self):
        self.deliver(payment("evt_2", payment_id="pay_2", amount=400, created=1699999950))
        self.later()
        self.deliver(payment("evt_1", payment_id="pay_1", amount=600, created=1699999900))
        self.assertEqual(self.service.balance("acct_1"), 1000)
        self.assertEqual(sorted(e["amount"] for e in self.service.entries("acct_1")), [400, 600])

    def test_early_refund_for_one_payment_does_not_touch_another(self):
        self.deliver(payment("evt_a", payment_id="pay_a", amount=1000))
        first = self.deliver(refund("evt_rb", payment_id="pay_b", amount=500))
        self.later()
        self.deliver(payment("evt_b", payment_id="pay_b", amount=500))
        self.later()
        self.deliver(refund("evt_rb", payment_id="pay_b", amount=500))
        if not is_2xx(first):
            self.later()
            self.deliver(refund("evt_rb", payment_id="pay_b", amount=500))
        self.assertEqual(self.service.balance("acct_1"), 1000)


if __name__ == "__main__":
    unittest.main()
