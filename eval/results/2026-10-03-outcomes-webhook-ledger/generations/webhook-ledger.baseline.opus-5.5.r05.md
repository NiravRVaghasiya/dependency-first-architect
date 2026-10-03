# Ledger build plan

**There is no store documentation in the workspace.** The brief points to `README.md` for the `ledgerkit MemoryStore` API, but the README here only says the directory is empty. So I don't know what the store can do: whether it has transactions, conditional writes ("put if absent" or compare-and-set), or key scans, or whether a write that raises `StoreError` may still have been saved. That decides how we stay correct when writes fail partway through, so checking it is step 0. The design below works with very little from the store (atomic single-key writes plus put-if-absent). If the store has real transactions, parts of it get simpler.

## 1. Core design

**The event log is the source of truth. Balances are worked out from it, not kept as a separately updated number.**

- Every signed delivery we accept is saved once, under its event `id`, along with a fingerprint of its body.
- Money movements ("entries") are saved with keys built from the event id, so writing one twice has no effect.
- `balance(account)` is the sum of that account's entries. If the store has transactions, we can keep a running balance updated in the same transaction. Without them, a separate counter is where partial failures and races would cause drift, so we skip it.
- Every step can be safely re-run. If any write raises `StoreError`, we return 503, PayCo retries, and the retry finishes the work. No partial state can survive that a retry can't complete.

This turns three of the brief's problems (duplicates, retries, failed writes) into a single mechanism.

## 2. Request pipeline (`handle`)

1. **Find the signature header** without regard to letter case. Parse `t=` and every `v1=` (more than one allows for secret rotation). Missing or malformed → **400**.
2. **Verify the signature.** Compute HMAC-SHA256 over `t` exactly as sent, plus `.`, plus the raw body bytes. Never re-serialize the body, and use the timestamp string as received rather than a reformatted number. Compare with `hmac.compare_digest`. Mismatch → **401**.
3. **Check freshness:** if `abs(clock.now() - t) > 300` → **401**. Exactly 300 seconds is accepted.
4. **Parse the JSON** and check the envelope: `id` and `type` must be strings. Failure → **400**.
5. **Deduplicate** by looking up the event record for `id`:
   - Same id with a different fingerprint → **409**, log a security incident, write nothing.
   - Already finished → **200** `{"result": "duplicate"}`.
   - Saved but not finished (an earlier attempt hit `StoreError`) → continue from step 7.
6. **Save the event record** with status `received`, using put-if-absent. If another delivery saved it first, re-read it and return to step 5.
7. **Dispatch by `type`:**
   - Unknown type → mark `ignored` and return **200**. The raw event stays stored so we can process it later when we add that type.
   - `payment.succeeded` or `refund.succeeded` → validate and apply (section 3).
8. **Mark the event finished** and return **200**.

Any `StoreError` at any step → **503**.

### Response codes

| Situation | Status | Why |
|---|---|---|
| Applied / duplicate / ignored type / refund held | 200 | PayCo stops retrying |
| Bad header or body | 400 | Not a valid delivery |
| Bad signature or stale `t` | 401 | Required by PayCo's guide |
| Same id, different body | 409 | Required by the brief; alert |
| Signed but can't be applied (see 3.3) | 200 + quarantine | Retrying won't fix it; a person must |
| `StoreError` | 503 | Retry finishes the work |

## 3. Money rules

### 3.1 Payments

- Validate the fields. `amount` must be a positive `int`, and `bool` must be rejected explicitly because Python treats `True` as an int. `currency` must be three uppercase letters. `account_id` and `payment_id` must be non-empty strings.
- Set the account's currency with put-if-absent, so the first payment fixes it. If the currency differs from the account's → quarantine.
- Write the payment's `payment_id → event_id` record, then the entry (+amount).
- Then check for a held refund on this `payment_id` and apply it.

### 3.2 Refunds (which may arrive before their payment)

- If the payment is already known:
  - Check that `account_id`, `amount` and `currency` match the payment. Any mismatch → quarantine.
  - Claim the payment's refund slot with put-if-absent (`payment_id → refund event id`). If a different refund already holds it, that's a second "full" refund → quarantine.
  - Write the entry (−amount).
- If the payment isn't known yet, **hold the refund** under its `payment_id`, return 200, and leave the balance alone. When the payment arrives, both apply and the net effect is zero.
  - I chose holding over applying the refund at once (which would make the balance go negative for a while). Holding lets us check the refund against the payment, and avoids taking away credit the customer was never given.
- **Race to handle:** a refund being held while its payment is applied at the same moment. Both sides check again after writing: after holding, look for the payment; after the payment, look for a held refund. A background job that retries held refunds is the backstop.

### 3.3 Quarantine

Some deliveries are signed (so they really came from PayCo) but can't be applied: wrong currency, a refund that doesn't match its payment, a double refund, or missing fields on a type we handle. These are saved with a reason, raise an alert, and get a 2xx. A non-2xx would only cause 72 hours of retries that can't succeed. The 409 for a reused id is the exception, because the brief says to reject it.

### 3.4 Entries

Each entry holds `event_id`, `type`, a signed `amount`, `currency`, `payment_id`, `created`, and a sequence number. `entries()` returns them in the order applied. A test enforces that `balance()` always equals the sum of `entries()`.

## 4. Build order

0. **Pin down the store API** (the ledgerkit docs or source). Then write a wrapper that can inject `StoreError` before or after any chosen write, including "raised but the write was actually saved."
1. **Signature module:** headers in any case, several `v1` values, whitespace, tolerance edges (±300 and ±301 seconds), body bytes changed by one byte, non-numeric `t`.
2. **Event log and dedupe:** duplicate → 200 with no change; conflicting body → 409 with the account untouched, including when the conflict arrives while the first delivery is only half processed.
3. **Payments, then refunds** (holding, matching, quarantine), plus `balance` and `entries`.
4. **Failure tests:** for each write position, inject a failure, redeliver, and check the final state matches a run with no failures.
5. **Property test:** random event histories that are shuffled, duplicated and failure-injected must always give the same balances as a clean in-order run. This is the main protection for exact reconciliation.
6. **Reconciliation export:** per-account, per-month totals by `created`, plus lists of held refunds and quarantined events, for Finance to compare with PayCo's statement.

## 5. Outside this codebase (still needed)

- **HTTP layer:** pass the raw body bytes through untouched, set a size limit, respond quickly.
- **Clock sync (NTP)** on the host. If the clock drifts, every delivery gets a 401. Alert on a jump in 401s.
- **Alerts** on 409s, quarantined events, held refunds older than a day, and the 503 rate.
- **Secret rotation:** accept two secrets during the changeover.
- **Cutover from the spreadsheet:**
  - Import opening balances as an `opening_balance` entry type.
  - Import historical payments from PayCo's export, so refunds of payments made before go-live can be matched instead of being held forever.
  - Choose a cutover timestamp so no event is counted by both the nightly script and the webhook.
- **Growth:** summing entries per account is fine at a few thousand events a day. Later we can add balance snapshots with a "covered up to" marker. Running several instances needs the store's conditional writes; an in-process lock won't do.

## 6. Questions to settle before building

1. **Store API** (above): transactions or conditional writes, and can a failed write still have been saved?
2. **Does PayCo re-sign each retry with a fresh `t`?** If retries keep the original timestamp, every retry after 5 minutes fails the 300-second check, and an outage would lose events. We need PayCo to confirm.
3. **"Same body": byte-for-byte, or the same JSON?** I plan to fingerprint a canonical form of the JSON so a reformatted redelivery doesn't trigger a false incident, and to store the raw bytes too. Byte-for-byte is stricter but riskier.
4. **Quarantine with 200 instead of rejecting** for signed events we can't apply: is that acceptable?
5. **Spend before refund:** a refund can push a balance negative if the credit was already spent. That's a business decision, not something the ledger can solve.
6. **Order of `entries()`:** order applied (my plan) or by `created`?
