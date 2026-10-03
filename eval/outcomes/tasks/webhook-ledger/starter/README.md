# Ledger service — starter

Implement the service in `ledger/` (start from `ledger/service.py`). The product brief is
`BRIEF.md`. Use only the Python 3 standard library and `ledgerkit`.

## `ledgerkit` (provided; do not modify)

`ledgerkit` is the environment the service runs in.

- `ledgerkit.MemoryStore` — the database client. Tables of string keys and dict values. Every read
  and write happens in a transaction:

  ```python
  with store.transaction() as txn:
      txn.get(table, key)           # -> dict or None
      txn.put(table, key, value)    # value must be a dict; may raise StoreError
      txn.delete(table, key)        # may raise StoreError
      txn.scan(table, prefix="")    # -> [(key, value)] sorted by key
  ```

  A transaction commits when the `with` block exits normally and rolls back entirely if the block
  raises. Transactions cannot be nested. Values are copied in and out.
- `ledgerkit.StoreError` — raised by `put()` or `delete()` when a database write fails. The real
  database does this occasionally.
- `ledgerkit.FixedClock` — a clock; the service reads the time only through `clock.now()` (Unix
  seconds).

## The interface the service must provide

```python
from ledger.service import LedgerService

service = LedgerService(store, secret, clock)   # secret: bytes
status, body = service.handle(raw_body, headers)  # raw_body: bytes, headers: dict; body: dict
service.balance(account_id)                      # -> int, minor units; 0 if unknown
service.entries(account_id)                      # -> [{"event_id", "type", "amount", ...}], oldest first; type = the event type
```

You may add modules inside `ledger/`. Keep the class name, constructor, and method signatures.
