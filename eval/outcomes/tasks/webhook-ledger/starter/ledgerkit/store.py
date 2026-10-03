"""A small transactional key-value store: the database client the ledger service runs against.

It stands in for the managed network database described in BRIEF.md. Data lives in named tables
of string keys and JSON-like dict values. All reads and writes happen inside a transaction:

    with store.transaction() as txn:
        account = txn.get("accounts", "acct_1")      # None if absent
        txn.put("accounts", "acct_1", {"balance": 0})
        rows = txn.scan("entries", prefix="acct_1/")  # [(key, value)], sorted by key

A transaction commits when its `with` block exits normally and is rolled back completely (none of
its writes become visible) when anything raises inside the block. Values are deep-copied in and
out, so mutating a returned dict changes nothing until you put() it back.

Writes can fail, as they occasionally do against the real database: put() and delete() raise
StoreError when a fault is armed. `fail_on_write(n)` arms one: the n-th write from now raises, and
the fault then disarms itself. Single-threaded; transactions cannot be nested.
"""

import contextlib
import copy

_DELETED = object()


class StoreError(Exception):
    """A write to the database failed (for example, the network call timed out)."""


class Txn:
    """One transaction: reads see this transaction's own writes; nothing is visible elsewhere
    until it commits."""

    def __init__(self, store):
        self._store = store
        self._pending = {}

    def get(self, table, key):
        """The value stored under `key` in `table`, or None."""
        pending = self._pending.get((table, key))
        if pending is _DELETED:
            return None
        if pending is not None:
            return copy.deepcopy(pending)
        value = self._store._tables.get(table, {}).get(key)
        return copy.deepcopy(value) if value is not None else None

    def put(self, table, key, value):
        """Store a dict under `key` in `table`. May raise StoreError."""
        if not isinstance(key, str):
            raise TypeError("keys must be strings")
        if not isinstance(value, dict):
            raise TypeError("values must be dicts")
        self._store._write()
        self._pending[(table, key)] = copy.deepcopy(value)

    def delete(self, table, key):
        """Remove `key` from `table` (no error if absent). May raise StoreError."""
        self._store._write()
        self._pending[(table, key)] = _DELETED

    def scan(self, table, prefix=""):
        """[(key, value)] for every key in `table` starting with `prefix`, sorted by key."""
        merged = dict(self._store._tables.get(table, {}))
        for (pending_table, key), value in self._pending.items():
            if pending_table == table:
                merged[key] = value
        return [(key, copy.deepcopy(value)) for key, value in sorted(merged.items())
                if value is not _DELETED and key.startswith(prefix)]


class MemoryStore:
    """In-memory tables with transactions and fault injection."""

    def __init__(self):
        self._tables = {}
        self._in_transaction = False
        self._fail_at = None
        self.writes = 0              # every put()/delete() attempted, including failed ones
        self.failures_triggered = 0  # how many armed faults have fired
        self.commits = 0

    def fail_on_write(self, n):
        """Arm a fault: the n-th write from now (n >= 1) raises StoreError, then disarms."""
        if not isinstance(n, int) or n < 1:
            raise ValueError("n must be a positive integer")
        self._fail_at = self.writes + n

    def _write(self):
        self.writes += 1
        if self._fail_at is not None and self.writes == self._fail_at:
            self._fail_at = None
            self.failures_triggered += 1
            raise StoreError("write failed (simulated database error)")

    @contextlib.contextmanager
    def transaction(self):
        """Open a transaction; commit on normal exit, roll back if the block raises."""
        if self._in_transaction:
            raise RuntimeError("transactions cannot be nested")
        self._in_transaction = True
        txn = Txn(self)
        try:
            yield txn
        except BaseException:
            txn._pending.clear()
            raise
        else:
            for (table, key), value in txn._pending.items():
                rows = self._tables.setdefault(table, {})
                if value is _DELETED:
                    rows.pop(key, None)
                else:
                    rows[key] = value
            self.commits += 1
        finally:
            self._in_transaction = False
