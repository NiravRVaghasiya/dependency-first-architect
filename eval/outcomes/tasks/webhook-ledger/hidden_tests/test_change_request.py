"""Change request (round 2 only): partial refunds, never refunding more than the payment, and 422
for a refund that would (CHANGE_REQUEST.md). BRIEF.md still applies."""

import unittest

from _support import LedgerTestCase, is_2xx, payment, refund


class ChangeRequest(LedgerTestCase):
    def test_partial_refunds_accumulate(self):
        self.deliver(payment("evt_p", amount=2500))
        self.assertTrue(is_2xx(self.deliver(refund("evt_r1", amount=1000))))
        self.assertTrue(is_2xx(self.deliver(refund("evt_r2", amount=500))))
        self.assertAccount("acct_1", 1000, [2500, -1000, -500])

    def test_refunding_the_remainder_is_allowed(self):
        self.deliver(payment("evt_p", amount=2500))
        self.deliver(refund("evt_r1", amount=2000))
        self.assertTrue(is_2xx(self.deliver(refund("evt_r2", amount=500))))
        self.assertAccount("acct_1", 0, [2500, -2000, -500])

    def test_over_refund_is_rejected_with_422(self):
        self.deliver(payment("evt_p", amount=2500))
        self.deliver(refund("evt_r1", amount=2000))
        self.assertEqual(self.deliver(refund("evt_r2", amount=1000)), 422)
        self.assertAccount("acct_1", 500, [2500, -2000])

    def test_single_refund_larger_than_the_payment_is_rejected_with_422(self):
        self.deliver(payment("evt_p", amount=2500))
        self.assertEqual(self.deliver(refund("evt_r1", amount=3000)), 422)
        self.assertAccount("acct_1", 2500, [2500])

    def test_redelivered_partial_refund_counts_once(self):
        self.deliver(payment("evt_p", amount=2500))
        self.deliver(refund("evt_r1", amount=1000))
        self.later()  # PayCo redelivers later, with a fresh signature
        self.assertTrue(is_2xx(self.deliver(refund("evt_r1", amount=1000))))
        self.assertAccount("acct_1", 1500, [2500, -1000])

    def test_full_refund_still_works(self):
        self.deliver(payment("evt_p", amount=2500))
        self.assertTrue(is_2xx(self.deliver(refund("evt_r1", amount=2500))))
        self.assertAccount("acct_1", 0, [2500, -2500])

    def test_early_partial_refund_is_not_lost_or_doubled(self):
        first = self.deliver(refund("evt_r1", amount=700))
        self.deliver(payment("evt_p", amount=2500))
        self.later()  # PayCo's retry (or redelivery) of the refund comes later, freshly signed
        self.assertTrue(is_2xx(self.deliver(refund("evt_r1", amount=700))))
        if not is_2xx(first):
            self.later()
            self.deliver(refund("evt_r1", amount=700))
        self.assertEqual(self.service.balance("acct_1"), 1800)


if __name__ == "__main__":
    unittest.main()
