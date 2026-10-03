"""Failure injection: BRIEF.md says database writes occasionally fail partway through a request
and that PayCo retries anything not acknowledged with a 2xx. Whatever the design (one
transaction, retrying inside the request, best-effort extra writes), the invariant is:

- if the service acknowledges a delivery (2xx), the event is already applied exactly once;
- if it does not, nothing of it is visible;
- after PayCo retries, the event is applied exactly once.

Each write position is tested by failing the k-th write the delivery makes.
"""

import unittest

from _support import RETRY_GAP_S, deliver, is_2xx, make, payment, refund, state

MAX_WRITES = 12


class FailureInjection(unittest.TestCase):
    def _check(self, setup, event, expected):
        exercised = 0
        for k in range(1, MAX_WRITES + 1):
            service, store, clock = make()
            setup(service, clock)
            before = state(service)
            store.fail_on_write(k)
            status, _ = deliver(service, event, clock=clock)
            if not store.failures_triggered:
                break  # the delivery made fewer than k writes: every write position is covered
            exercised += 1
            with self.subTest(failing_write=k):
                if is_2xx(status):
                    self.assertEqual(state(service), expected,
                                     "acknowledged, but the event is not applied exactly once")
                else:
                    self.assertEqual(state(service), before,
                                     "not acknowledged, but part of the event is visible")
                clock.advance(RETRY_GAP_S)
                status, _ = deliver(service, event, clock=clock)  # PayCo's retry, database healthy
                self.assertTrue(is_2xx(status))
                self.assertEqual(state(service), expected)
        self.assertGreater(exercised, 0, "the delivery never wrote to the database")

    def test_failed_write_during_a_payment(self):
        self._check(lambda service, clock: None, payment("evt_1"), (2500, [2500]))

    def test_failed_write_during_a_refund(self):
        self._check(lambda service, clock: deliver(service, payment("evt_1"), clock=clock),
                    refund("evt_2"), (0, [2500, -2500]))


if __name__ == "__main__":
    unittest.main()
