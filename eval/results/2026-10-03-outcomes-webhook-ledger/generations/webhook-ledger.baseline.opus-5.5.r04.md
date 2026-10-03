# Ledger build plan

**The store's API is missing.** The brief says to see README.md for the ledgerkit `MemoryStore`. The README in the workspace says the folder is intentionally empty and has no code or docs, so I haven't seen what the store can do. The core design depends on that, so step 0 below is to get it. Where the plan depends on it, I give a version for each possible answer.

## 1. Requirements that drive the design

| Fact from the brief | What it means for the build |
|---|---|
| Any 2xx stops retries, and retries last up to 72h | Return 2xx only after everything for the event is safely stored. Return non-2xx only when a retry could actually help or the delivery really must be refused. |
| Duplicates can arrive even after a 2xx | The event `id` is the idempotency key. A second delivery must never change the money. |
| Order isn't guaranteed (a refund can arrive before its payment) | Applying an event can't require the related event to already exist. |
| Store writes can fail partway | We can't rely on several writes landing together. Each write must be safe to repeat, and one write must be the point where the event counts as stored. A retry finishes whatever is left. |
| Finance must match exactly, to the cent | Use integers only, never double-apply, and never silently drop money. Anything we can't apply is stored and raises an alert. |
| Same `id` with a different body is an incident | Store a hash of every event we accept, including types we ignore. A conflicting delivery is rejected and changes nothing. |
| New event types will appear | Accept them with 2xx and **store them**. PayCo won't send them again after a 2xx, so storing them lets us process them later when we add handlers. |

## 2. Steps in `handle()`, in order

1. **Find the signature header in any letter case.** Look up `payco-signature` without regard to case. If it's missing, or appears twice with different values → **400**.
2. **Parse the header.** Split on `,`, then split each part on the first `=`. `t` must be an integer. Collect *every* `v1` value (allowing several makes key rotation possible later) and ignore unknown schemes. If the header is malformed → **400**.
3. **Check the signature before parsing JSON.** Compute `hmac.new(secret, f"{t}.".encode() + raw_body, sha256).hexdigest()` and compare it to each `v1` with `hmac.compare_digest`. If none match → **401** with a generic body, so we don't reveal why it failed.
4. **Check the timestamp.** If `abs(clock.now() - t) > 300` → **401**. A gap of exactly 300 seconds is accepted. The clock value may be a float.
5. **Parse and validate.** Decode as UTF-8, then JSON. `id`, `type` and `created` (an int) are required. For the two money types, `data` also needs:
   - `account_id`: a non-empty string
   - `payment_id`: a string
   - `amount`: an `int` (not a `bool` or float) and greater than 0
   - `currency`: three uppercase letters

   If validation fails → **400** plus an alert. The signature checked out, so this came from PayCo and means our assumptions are wrong. PayCo's retries give us 72h to fix it.
6. **Deduplicate.** Look up the event by `id`.
   - **Hash differs** → **409**, raise a security incident, write nothing.
   - **Hash matches** → finish any steps left over from an earlier failed attempt (see §3), then return **200** `{"result": "duplicate"}`.
7. **Apply the event:**
   - `payment.succeeded` → entry `+amount`.
   - `refund.succeeded` → entry `−amount`, **applied right away even if the payment hasn't arrived yet.** Balances can be negative for a while, and also legitimately (a customer spends credit, then gets a refund). Parking refunds would make `balance` and `entries` disagree with PayCo.
   - Any other type → store the event as `ignored` and return **200** `{"result": "ignored"}`.
8. **Store errors.** Any `StoreError` → **503** so PayCo retries. Any other unexpected exception → **500**, logged with the event id.

The body hash is SHA-256 of the raw bytes. **Open question:** does PayCo resend exactly the same bytes, or might it re-serialize the JSON? If it re-serializes, hash a canonical form instead (`json.dumps(sort_keys=True, separators=(",", ":"))`) and keep the raw-bytes hash for forensics. Otherwise a harmless re-serialization would look like a security incident.

## 3. Storage and the store failure problem

**Records:**
- `event/{id}`: `{body_hash, type, created, received_seq, status: applied|ignored|held, account_id?}`
- `entry/{account_id}/{event_id}`: `{event_id, type, amount (signed), currency, payment_id, created, seq}`
- `account/{account_id}`: `{currency}` and, only if we can update it atomically, `balance`

**The rule that matters:** never keep a read-modify-write balance counter unless it's updated atomically with the entry. If a counter write succeeds and the "event done" write fails, the retry adds the money twice. That mismatch is exactly what finance would catch.

**Version A: the store has transactions or atomic multi-key writes.** Write the event record, the entry and the account update in one transaction, on condition that `event/{id}` doesn't exist yet. The balance is a stored counter. This is the simplest option.

**Version B: only single-key writes, ideally with insert-if-absent or compare-and-set, plus a prefix scan.**
- Write `event/{id}` first, with insert-if-absent. That write is the point where the event counts as stored.
- Then write `entry/...` and `account/...`. Their keys are fixed by the event, so writing them again is harmless.
- On a matching duplicate, step 6 checks that these writes exist and makes any that are missing.
- A `StoreError` that comes back *after* the write actually landed is also safe, because writes repeat harmlessly.
- `balance` is the sum of the account's entries. Later, add a snapshot or cache if volume needs it.
- If a crash happens between the event write and the entry write, the balance lags until PayCo retries. PayCo will retry, because we returned 5xx. A nightly sweep for events with no entry covers the case where retries run out.

**Version C: no conditional writes at all.** Version B's dedup only works safely inside one process, under a lock per event id. That has to be written down as a deployment limit: one worker, or a real lock service.

**Concurrency:** two copies of the same event can arrive at the same moment. Insert-if-absent (or a transaction) makes exactly one of them win. The other becomes a duplicate.

## 4. Currency and refund checks

- **Account currency** is set by the first money movement we apply. Refunds share their payment's currency, so a refund that arrives first can set it.
- **Currency mismatch** (a USD payment to a EUR account): we can't add it to the balance, and returning non-2xx only loses it after 72h. **Proposal:** store the event as `held`, return 200, raise an alert, and have finance resolve it.
- **Refund amount ≠ payment amount:** check it whenever both are present, whichever arrives second. Alert if it fails.
- **A second refund event (different `id`) for a `payment_id` that already has one:** "refunded in full" means this shouldn't happen. **Proposal:** hold it and alert instead of applying it.

## 5. Read API

- `balance(id)`: the counter in Version A or the sum in Version B; 0 for an unknown account. If the store fails here, the error is raised to the caller; it doesn't become a webhook status.
- `entries(id)`: sorted by `(created, received_seq)`. **Open question:** "oldest first" could mean by event time or by arrival time. I'm assuming event time.

## 6. Tests (unittest, standard library only)

- **Signatures:** known test vectors, header names in mixed case, several `v1` values, malformed headers, one flipped byte in the body, `t` at ±300 and ±301 seconds.
- **Exhaustive store-failure test:** a wrapper around `MemoryStore` that raises `StoreError` on the k-th write, for every k. After each failure, redeliver until we get 200, then check the final state equals a clean run. Also cover a `StoreError` raised after the write actually landed.
- **Randomized property test:** shuffle a set of payments and refunds, add duplicates and injected failures, keep retrying. Each final balance must equal Σpayments − Σrefunds, and each event must appear exactly once in `entries`.
- **Rules:** a conflicting body leaves the account byte-for-byte unchanged; unknown types are stored and get 200; refund before payment; currency mismatch is held; bool and float amounts are rejected.
- **Concurrency:** many threads delivering the same event, and the same account.

## 7. Build order

0. **Get the ledgerkit README / `MemoryStore` API (blocking).** Choose Version A, B or C.
1. Signature module with its tests.
2. Event parsing and validation.
3. Storage layer, plus the failure-injection wrapper and the exhaustive failure test.
4. `handle()` steps and the status table.
5. `balance` and `entries`.
6. Property and concurrency tests.

## 8. Outside this codebase, but needed

- **Reconciliation job:** nightly and monthly, comparing per-account totals against PayCo's export (the existing CSV). It catches events missed during an outage longer than 72h.
- **Alerts:** 409 incidents; spikes in 401s (attacks, or our clock drifting); 5xx rate; held events; events with no entry.
- **Clock:** run NTP. Drift beyond 300s rejects every delivery.
- **Ask PayCo:** whether each retry is signed with a fresh `t`. If retries keep the original `t`, every retry after 5 minutes fails our check, and the 300s window would have to be rethought.
- **Cutover:** opening balances from the spreadsheet, which includes support's manual fixes. Probably as an `opening_balance` entry type. Also pick a cutover point so no event is counted by both the old script and Ledger.

## Open questions

1. What can `MemoryStore` do (transactions, conditional writes, scans)?
2. Does PayCo resend identical body bytes?
3. Does each retry get a new `t`?
4. Does product spending debit this ledger? If so, reconcile payment and refund totals per account rather than the balance.
5. Currency mismatches and second refunds: hold them, or something else?
6. Should `entries` be ordered by event time or arrival time?
7. How do opening balances get in at cutover?
