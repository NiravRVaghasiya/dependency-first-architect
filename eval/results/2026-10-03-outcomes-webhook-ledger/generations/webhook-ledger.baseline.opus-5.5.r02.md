# Build plan: Ledger (PayCo webhooks → account balances)

The workspace is empty. The only file is a README that says there's nothing here, so the "ledgerkit MemoryStore (see README.md)" the brief points to isn't documented anywhere I can see. I don't know the store's API, and that matters more than anything else in this plan, because the store is what stops a write that fails partway from corrupting balances. Step 0 is to get that API. Below I state the minimum the design needs from the store and how the plan changes depending on what it offers.

---

## 1. Design decisions

### 1.1 Order of checks in `handle()`
1. **Read headers ignoring case.** Lowercase the header names, then read `paycosignature`. If it's missing, return **401**.
2. **Check the signature on the raw bytes, before parsing anything.**
   - Parse `t=…,v1=…` into key/value pairs. Accept more than one `v1` (secret rotation) and ignore keys we don't recognise. A malformed header or a `t` that isn't an integer gets **401**.
   - Compute `HMAC-SHA256(secret, f"{t}.".encode() + raw_body)` and compare it to each `v1` with `hmac.compare_digest`. No match gets **401**.
   - If `abs(clock.now() - t) > 300`, return **401**. Exactly 300 seconds is accepted, which is how I read "more than 300". `clock.now()` might return a float, so the comparison has to handle that.
3. **Parse and validate the body.** Reject duplicate JSON keys using `object_pairs_hook`, because they make "same body" ambiguous. Require `id` (a non-empty string), `type` (a string), `created` (an int) and `data` (an object). Malformed input gets **400**.
4. **Check for a duplicate or conflicting `id`** (see 1.2).
5. **Dispatch on `type`.** Handle payment or refund. Anything else is recorded and ignored.

Validation that applies to every type runs before the dedup check. Validation of payment and refund fields runs inside the handler, so that a new event type with a different `data` shape still gets recorded.

### 1.2 Idempotency and the "same id, different body" incident
- **Every** signed, well-formed event is recorded in a global event index: `event_id → {fingerprint, status, first_seen}`. Unknown types are included. That way a conflicting body is caught for them too, and stored events can be replayed when we add handlers for disputes and so on.
- **Fingerprint** = SHA-256 of canonical JSON (`sort_keys=True`, compact separators), not of the raw bytes. Hashing raw bytes would raise a false incident, and skip a real refund, if PayCo ever re-serialises the body with different whitespace or key order. Comparing parsed content is just as safe, since a body that means the same thing can't be used for an attack.
- Same `id` with the same fingerprint returns **200** `{"status": "duplicate"}` and touches nothing.
- Same `id` with a different fingerprint returns **409** `{"status": "conflict"}`. No account is touched, the incident is recorded (both fingerprints and the time) and an alert-level log is written. PayCo will keep retrying the 409 for 72 hours. That's acceptable, and each retry repeats the alert, which is useful.

### 1.3 Status codes, and why most outcomes are 2xx
PayCo only distinguishes 2xx from everything else. Anything that isn't 2xx gets retried for 72 hours and is then dropped. The rule: **return 2xx once the event is safely stored, even if we chose not to apply it.** Return non-2xx only when a retry might help, or when the brief requires rejection.

| Situation | Status |
|---|---|
| Applied payment or refund | 200 `applied` |
| Duplicate | 200 `duplicate` |
| Unknown type (recorded, no money moved) | 200 `ignored` |
| Refund arrived before its payment (parked) | 200 `pending` |
| Signed but fails business rules (see 1.5) | 200 `flagged`, recorded for review |
| Bad, missing or stale signature | 401 |
| Malformed body | 400 |
| Same id, different body | 409 |
| `StoreError` or any unexpected exception | 500, so PayCo retries |

### 1.4 Out-of-order refunds: park them, don't apply them early
If a refund arrives and its `payment_id` hasn't been seen yet, it is stored as **pending** on its account. It is not applied yet. When the payment arrives, the payment and the pending refund are applied **in one atomic write**, payment first.
- Why park instead of applying straight away: before the payment arrives the account has no currency, and we can't check the refund's amount, account or currency against a payment we don't have yet.
- It's safe: the account is never credited with a payment that has already been refunded, and the final balance is the same either way.
- `entries()` order = order of application, kept as a per-account sequence number. Parked refunds therefore always come straight after their payment. Each entry also stores `created`, so finance can sort by event time if they want to.
- A refund that is still pending after N days (e.g. 7) goes on a report. It means the payment event never arrived.

### 1.5 Business rules (each failure is flagged with 200, never applied)
- **Payment:** `amount` must be an `int` but not a `bool` (in Python, `True` counts as an int), and greater than 0. `currency` must be 3 uppercase letters. `account_id` and `payment_id` must be non-empty strings. The first payment that is *applied* sets the account's currency. A later payment in a different currency is flagged.
- **A second payment event with a `payment_id` we already have** (different event id) is flagged, not credited twice.
- **Refund:** it must match the payment's `account_id`, its `amount` and its currency exactly. A second refund for the same `payment_id` is flagged, because a payment can only be fully refunded once. The refund entry's `amount` is negative.
- **Negative balances are allowed.** A customer can spend credit and then get a refund, and the balance has to match PayCo's statement. Whether product needs to do anything about negative balances is a separate question (see section 4).

### 1.6 Surviving `StoreError` (writes that fail partway)
The goal is that **any sequence of failures, followed by PayCo's retries, ends in exactly the correct state.** Two techniques get there:

**(a) Keep everything about one account in one record**, written in a single write: `{currency, balance, seq, entries[], applied_event_ids, payments{payment_id → {amount, currency, refunded}}, pending_refunds{payment_id → event}}`. Applying a payment and its parked refund is then one write, and `balance == sum(entries)` stays true by construction.

**(b) Order the writes across keys so that a retry finishes the job:**
1. Write the event index record (`status: received`, fingerprint).
2. Read, change and write the account record. Its `applied_event_ids` is what makes this step idempotent.
3. Set the event index status to `applied` / `pending` / `flagged` / `ignored`.

- If the request crashes after step 1, the retry finds the same fingerprint with status `received` and carries on with step 2.
- If it crashes after step 2, the retry sees that the account already contains the event id, skips the money change and finishes step 3.
- If `StoreError` was raised even though the write actually went through (an ambiguous commit), the retry handles it the same way, because every step re-reads before it writes.
- Any `StoreError` returns 500. A small bounded retry inside the request (say 2 attempts) is optional. PayCo's retries are the real recovery mechanism.

**Concurrency.** A payment and its refund, or two copies of the same event, can arrive at the same time. A plain read-then-write would lose one of the updates. What I need from the store, in order of preference:
1. Transactions across several keys. If we have these, use them and make the design simpler.
2. Compare-and-swap or versioned writes on a single key. Then retry the read-change-write on a version conflict. The event index write in step 1 must be "insert only if absent".
3. Only plain get and put. Then use a per-account `threading.Lock` plus a lock for the event index. **This only works with one process.** That is fine at a few thousand events a day, but it has to be documented as a hard limit before we scale out.

### 1.7 Reading balances and entries
`balance()` reads the account record and returns 0 if it doesn't exist. `entries()` returns copies of the entry dicts, each with `event_id`, `type`, `amount` (signed), `currency`, `payment_id`, `created`, `seq` and `applied_at`.

---

## 2. Code layout (standard library only)
- `ledger/signature.py`: header parsing and verification. Pure functions, easy to test.
- `ledger/events.py`: parsing, the duplicate-key guard, canonical fingerprint, validation.
- `ledger/store_adapter.py`: the only module that talks to `MemoryStore`. It hides the get/put/CAS details so the rest of the code doesn't depend on them.
- `ledger/service.py`: `LedgerService` (dispatch and the write ordering from 1.6).
- `ledger/apply.py`: pure functions of the form `(account_record, event) → new_record | Flag`. The money logic stays free of I/O.
- `tests/`

## 3. Tests
- **Signatures:** valid; tampered body; wrong secret; header in any letter case; missing or malformed header; multiple `v1` values; skew of exactly ±300 (accepted) and ±301 (rejected), in both directions.
- **Idempotency:** a duplicate after 200; a duplicate whose JSON is re-serialised differently is still treated as a duplicate; same id with a changed amount or account returns 409, leaves the account untouched and records an incident; a conflict on an unknown-type id.
- **Ordering:** refund before payment; refund only (stays pending, balance 0); double refund; refund with mismatched amount, account or currency.
- **Validation:** bool, float, zero or negative amount; bad currency; a second currency on an account; a reused `payment_id`.
- **Fault injection (the most important tests):** a store wrapper that raises `StoreError` at every write point, in two variants: the write didn't happen, and the write happened but the error was raised anyway. After each fault, redeliver until we get 200, then check the final state equals the state with no faults.
- **Randomised runs:** generate payments and refunds, shuffle them, duplicate some, inject faults, retry until done. Check `balance == sum(entries)`, that every balance matches a simple reference model, and that no `event_id` appears twice in any account.
- **Concurrency:** use threads to deliver the same event and payment/refund pairs at the same time.

## 4. Open questions for stakeholders
1. **The `MemoryStore` API.** Does it have transactions, CAS or versioning? Do failed writes leave a partial result or none at all? This decides which option in 1.6 we use.
2. **Do PayCo's retries come with a fresh `t`?** If a retry reuses the original timestamp, the 300-second check rejects every retry after 5 minutes and we'd silently lose events. This has to be confirmed with PayCo or tested in their sandbox.
3. **Disputes and other unhandled types move money.** Until we support them, balances **will not match** the monthly statement whenever a dispute happens. Finance needs to know this. The events are stored, so we can replay them once a handler exists.
4. What should `entries()` be sorted by: application order (what I propose) or `created`?
5. Is the currency "fixed by its first payment" the first payment we *apply* (what I propose) or the earliest by `created`?
6. Product policy on negative balances, and who works through flagged events and pending refunds.

## 5. Outside the codebase (recommendations)
- The HTTP layer must pass the **exact raw body bytes** to `handle()` without parsing them first. It should cap the body size and answer quickly.
- Alert on any 409, any flagged event, a sustained 500 rate, and refunds that stay pending.
- Store the secret in a secret manager. Rotation already works because multiple `v1` values are accepted.
- **Reconcile daily, not monthly,** against PayCo's export or API. If the store is down for more than 72 hours, PayCo stops retrying and those events are gone; a daily reconciliation would catch that.
- **For growth:** one record per account with every entry in it grows without limit. Moving to a transactional database (an entries table with a unique `event_id` and account rows locked during updates) removes that problem and the single-process limit. Keeping storage behind `store_adapter.py` keeps that migration small.

## 6. Order of work
0. Get the ledgerkit API and answer questions 1 and 2.
1. Signature checking and event parsing, with their tests.
2. The pure apply logic with the reference model and randomised tests.
3. The service, the store adapter and the write ordering, then fault-injection and concurrency tests.
4. Recording and reporting of incidents, flags and pending refunds.
5. Hand over the integration notes for the HTTP layer and monitoring.
