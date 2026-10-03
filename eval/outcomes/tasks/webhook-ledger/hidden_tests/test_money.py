"""Money: BRIEF.md says amounts are integer minor units, finance reconciles to the minor unit, and
each account holds the single currency of its first payment."""

import unittest

from _support import LedgerTestCase, is_2xx, payment


class Money(LedgerTestCase):
    def test_fractional_amount_is_rejected(self):
        status = self.deliver(payment("evt_1", amount=10.5))
        self.assertFalse(is_2xx(status))
        self.assertAccount("acct_1", 0, [])

    def test_string_amount_is_rejected(self):
        status = self.deliver(payment("evt_1", amount="2500"))
        self.assertFalse(is_2xx(status))
        self.assertAccount("acct_1", 0, [])

    def test_large_amounts_stay_exact(self):
        big = 2 ** 53 + 1  # the first integer a float cannot hold exactly
        self.assertTrue(is_2xx(self.deliver(payment("evt_1", amount=big))))
        self.deliver(payment("evt_2", payment_id="pay_2", amount=1))
        self.assertEqual(self.service.balance("acct_1"), big + 1)
        self.assertIsInstance(self.service.balance("acct_1"), int)

    def test_balance_is_an_integer(self):
        self.deliver(payment("evt_1", amount=2500))
        self.assertIsInstance(self.service.balance("acct_1"), int)
        self.assertTrue(all(isinstance(e["amount"], int) for e in self.service.entries("acct_1")))

    def test_payment_in_another_currency_does_not_change_the_account(self):
        self.deliver(payment("evt_1", currency="EUR", amount=2500))
        self.deliver(payment("evt_2", payment_id="pay_2", currency="USD", amount=9999))
        self.assertAccount("acct_1", 2500, [2500])


if __name__ == "__main__":
    unittest.main()
