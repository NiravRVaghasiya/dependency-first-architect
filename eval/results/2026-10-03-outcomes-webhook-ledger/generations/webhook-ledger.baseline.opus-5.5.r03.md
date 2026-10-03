# Ledger build plan

I couldn't see the store's API. The brief points to `README.md` for the ledgerkit `MemoryStore`, but the only README in the workspace says the directory is empty. So I don't know whether the store supports transactions, conditional ("put-if-absent") writes, or listing keys by prefix. That decides how we handle `StoreError`, so the first build step is to find out, and Section 4 plans for both cases.

## 1. Rules the design follows

1. **Only answer 2xx once the event is safely saved.** PayCo counts any 2xx as delivered and stops retrying, so a 2xx sent before the write commits means the money is lost for good. If the store raises `StoreError` we return 503 and let PayCo retry.
2. **Once a verified event is saved, answer 2xx, even if we can't apply it.** Retrying won't fix a currency mismatch or an event type we don't handle yet. Reprocessing those is our job, using the saved event. Only three things get a non-2xx: failed authentication, the duplicate-id security incident, and store failures.
3. **Every write must be safe to repeat.** A failure can happen at any point, and a write that raised `StoreError` may still have been saved. A redelivery must finish the work, never apply it twice.
4. **Delivery order and duplicates must not change the result.** Any order of the same events, with any number of duplicates, must end in the same balances.

## 2. Handling one delivery, in order

1. **Read the header.** Look up `PayCo-Signature` ignoring letter case. Parse `t=` (digits only) and every `v1=` value; there may be several while a secret is being rotated. Ignore other schemes. If the header is missing or malformed, return **400**.
2. **Check the signature** before parsing the JSON. Compute the HMAC-SHA256 of `t + "." + raw_body` with the secret and compare it to each `v1` using `hmac.compare_digest`. No match: **401**.
3. **Check the timestamp.** If `abs(clock.now() - t) > 300`, return **401**. A replay inside the 300 seconds does no harm because duplicates are caught in step 6.
4. **Parse the JSON.** Reject duplicate keys (via `object_pairs_hook`) so that two parsers can't read the same signed bytes differently. Unparseable JSON or a missing or non-string `id`: **400**, and alert.
5. **Compute a fingerprint** of the body: a hash of the JSON re-serialised with sorted keys and no spaces. I chose this over hashing the raw bytes because if PayCo ever changes only the formatting, raw bytes would raise a false incident. We would then reject a real event for 72 hours and lose it. Different content still produces a different hash.
6. **Claim the event id.**
   - Already recorded and complete, same fingerprint: **200** `duplicate`, nothing changes.
   - Already recorded, same fingerprint, but marked incomplete (an earlier attempt failed partway): carry on and finish the work.
   - Already recorded with a **different fingerprint**: **409**, raise a security alert, leave the account untouched. This needs a valid signature, so it means either the secret has leaked or PayCo has a bug.
7. **Route by type.**
   - **Unknown type:** save the whole verified event as `unhandled` and return **200**. When we add dispute handling later, we can replay these instead of losing them.
   - **Known type with a bad payload** (amount not a positive integer, including `true` and `25.0`; currency not three capital letters; missing `account_id` or `payment_id`): save as `quarantined`, alert, return **200**.
8. **Apply the event** (next section), mark it complete, return **200** `applied` / `pending` / `quarantined`.
9. **Any `StoreError`** at any step: **503**. Response bodies don't explain why something was rejected.

## 3. Business rules

**Payments**
- The account's currency is set by its first applied payment, using one put-if-absent write so two simultaneous first payments can't both win.
- A payment in a different currency is quarantined with an alert. We can't refuse it at PayCo because the customer has already paid, so finance has to decide what to do.

**Refunds**
- A refund only counts once its payment is on record and matches it: same `account_id`, same `amount` (the brief says refunds are always in full) and same currency.
- A refund that arrives before its payment is saved as `pending`, returns 200, and doesn't change the balance until the payment arrives.
- A refund that doesn't match its payment is quarantined.
- A second refund event (different id) for the same `payment_id` is quarantined. A payment can only be refunded in full once, so this is an anomaly. Finance's reconciliation will show the gap, which is what we want.

**How the balance is worked out**
- The balance is computed when read, from the saved records: matched payments minus matched refunds. There is no separate running total.
- The reason: if a refund and its payment arrive at the same time, each request can miss the other, and a "refund waiting for payment" step that runs on write would leave the refund stuck. Computing at read time avoids that and needs no locks.
- `entries()` lists only applied movements (payments, and refunds whose payment exists). They are sorted by `(created, event_id)`, so the order doesn't depend on arrival order. This needs confirming (Section 7).
- The tests check that `balance(a) == sum(e["amount"] for e in entries(a))`.
- Each entry has `event_id`, `type`, signed `amount`, `currency`, `payment_id`, `created` and `received_at`, so finance can match against PayCo's statement line by line.

## 4. Data model and partial failures

Records, keyed so that every write is safe to repeat:
- `event/{id}`: fingerprint, parsed fields, outcome (`applied`, `pending`, `quarantined`, `unhandled`), and a `complete` flag
- `payment/{payment_id}` → event id (put-if-absent)
- `refund/{payment_id}` → event id (put-if-absent; a different id already there means quarantine)
- `account/{id}/currency` (put-if-absent)
- `account/{id}/event/{event_id}`: an index used to compute the balance

What we build depends on the store:
- **If `MemoryStore` has transactions:** do all the writes for one event in a single transaction. Then a `StoreError` means nothing was saved, and we return 503.
- **If it only has single-key writes:** write `event/{id}` first, marked incomplete. Then write the other records, each safe to repeat. Mark it complete last. A redelivery that finds an incomplete record redoes the remaining steps (rule 3).
- **If it has no conditional writes:** run one process that writes for each account at a time. That's fine at a few thousand events a day, but it limits how far we can scale, so we should push for conditional writes.
- **When volume grows:** keep a stored running balance, updated in the same transaction as each entry, so reads don't add up every record.

## 5. Build order

1. Read the real ledgerkit API and choose the matching branch in Section 4.
2. Signature and timestamp checking as a pure function, tested on its own: wrong letter case, several `v1` values, a tampered body, `t` exactly 300 and 301 seconds off in both directions.
3. Event parsing, payload checks and the fingerprint.
4. Saving and claiming events, including duplicate and incident handling.
5. Payments, refunds, currency rules and the balance/entries reads.
6. Tests, standard library only (`unittest`, seeded `random`):
   - **Fault injection:** wrap the store so the Nth write raises `StoreError`, for every N. Then redeliver. The final state must match a run with no faults. This includes a variant where the write that raised was actually saved.
   - **Order and duplicates:** random orderings of an event set with duplicates added must always give the same balances and entries.
   - **Specific cases:** refund before payment; refund with a wrong amount, account or currency; second refund for the same payment; currency mismatch; unknown type; same id with a different body (409, nothing changes); unknown account returns 0.

## 6. Outside this codebase (HTTP, deployment, monitoring)

- **HTTP layer:** pass the raw body bytes through untouched (no JSON middleware, no re-encoding), set a body size limit, and keep headers.
- **Clock:** keep it synced with NTP. With a 300-second window, drift causes 401s, though PayCo's retries give us time to fix it.
- **Alerts:** any 409; spikes in 401 or 5xx; anything quarantined; refunds pending for more than 24 hours; unhandled types (as a count, not paging anyone).
- **Daily reconciliation:** compare our entries against PayCo's export or API. This catches events lost when an outage lasts longer than PayCo's 72-hour retry window.
- **Secret rotation:** accept two secrets at once during a rotation.

## 7. Questions to settle before go-live

1. **Does PayCo re-sign retries with a new `t`?** If retries keep the original timestamp, every retry after 5 minutes fails our check and the event is lost, which undoes the retry-based recovery above. This is the most important question.
2. **Moving off the spreadsheet.** Today's balances include manual fixes. Refunds after go-live for payments made before go-live will stay pending forever unless we first import past payments from PayCo's export, plus a starting balance for each account.
3. **Spending.** Ledger has no spend operation, so `balance()` is really top-ups minus refunds. Who records spending, and what happens when a refund lands after the credit was spent (a negative balance)?
4. **Policies:** what to do with a payment in the wrong currency, with a second refund for the same payment, and whether `entries()` should be ordered by `created` or by arrival.
5. **Payment id uniqueness:** can PayCo send two different event ids for the same `payment_id`? The plan removes duplicates by event id only, as the brief says.
