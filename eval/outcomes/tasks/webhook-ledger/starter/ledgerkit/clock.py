"""A controllable clock. The service reads the time only through `clock.now()`."""


class FixedClock:
    """A clock that stands still until told to move. now() returns Unix seconds (float)."""

    def __init__(self, now=1_700_000_000.0):
        self._now = float(now)

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds

    def set(self, now):
        self._now = float(now)
