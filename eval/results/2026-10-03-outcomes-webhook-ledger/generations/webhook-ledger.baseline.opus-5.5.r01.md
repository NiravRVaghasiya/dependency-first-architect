I couldn't find the ledgerkit `README.md` the brief mentions. The workspace holds only a placeholder README that says it's intentionally empty, so I don't know what `MemoryStore` offers. That matters a lot, because how to survive a `StoreError` partway through a request depends on it. Below is a full plan with the store-dependent parts marked, and step 0 is to get that document.

# Plan: Ledger service

## 1. Rules the design has to follow

Most of the brief's constraints come down to five rules:

1. **Return 2xx only after the result is fully saved.** PayCo stops retrying after any 2xx. So a 2xx returned before every write has landed means money is lost for good. Every `StoreError` becomes a 5xx so PayCo retries.
2. **Every write must be safe to repeat.** Writes can fail partway and PayCo sends duplicates, so the same request will run against partly-written state. Never do a "balance += amount" step that could run twice.
3. **Never answer 4xx when a retry would give the same result.** A 4xx makes PayCo retry the same bytes for 72 hours and then give up. For a signed, well-formed event we can't apply (wrong currency, mismatched refund), we save it, alert, and return 200. Retrying doesn't help, and dropping it breaks reconciliation.
4. **Always save the raw event before returning 2xx**, including event types we ignore. PayCo won't send them again, and we plan to handle disputes and payouts later. Saving the raw body now lets us process the history later.
5. **Only record event IDs after the signature checks out.** Otherwise an attacker could register an unsigned body under a real event ID. When PayCo's real event arrived, we would see "same ID, different body" and reject it.

## 2. Request pipeline (`handle`)

| Step | Check | Result if it fails |
|---|---|---|
| 1 | Find `PayCo-Signature` without regard to letter case. Parse `t=` and one or more `v1=` values. | 401 |
| 2 | `t` is an integer and `abs(clock.now() - t) <= 300` | 401 (stale) |
| 3 | HMAC-SHA256(secret, `f"{t}.".encode() + raw_body`). Compare with `hmac.compare_digest` against each `v1`; accept if any matches. | 401 |
| 4 | Body is valid JSON with a string `id`, a string `type` and an integer `created` | 400 + alert (signed but malformed means a PayCo bug, and retries won't fix it) |
| 5 | Look up `id` in the event registry and compare sha256(raw_body) | Same hash and done: **200, nothing changes**. Same hash but unfinished: carry on and finish it (§4). **Different hash: 409, record an incident, touch nothing.** |
| 6 | Route by `type`: payment, refund, or anything else | Other types: save as `ignored` and return 200 |
| 7 | Check the fields. `amount` must be an `int` and not a `bool` (in Python `True` counts as an int); payments need `amount > 0`. `currency` must match `^[A-Z]{3}$`. `account_id` and `payment_id` must be non-empty strings. | Save as `quarantined`, alert, return 200 |
| 8 | Apply the business rules (§3) and commit (§4) | `StoreError` → 503 |

The whole handler is wrapped so that any unexpected exception returns 500, never 2xx.

## 3. Business rules

**Payment**
- First payment on an account sets the account's currency. A later payment in another currency is quarantined, never mixed into the balance. Whether to reject or convert is a question for Finance.
- If a different event already used the same `payment_id`, quarantine it. Deduplicating by event ID alone would credit the customer twice.
- Otherwise add an entry of `+amount`. If a refund for this payment is waiting (see below), apply it straight after, in the same commit.

**Refund**
- Find the payment by `payment_id`.
  - **Payment not seen yet (out of order):** save the refund as *pending* on the account and return 200. It is not in `entries` and doesn't change the balance yet. When the payment arrives, both are applied together, so the customer never briefly holds credit for a refunded payment, and the balance never shows a temporary negative.
  - **Payment found:** check that `account_id`, `currency` and `amount` all match the payment, and that the payment hasn't already been refunded. If any check fails, quarantine it. A second refund event for the same payment must not take the money twice. If all pass, add an entry of `-amount` and mark the payment refunded.
- A refund can push the balance below zero if the customer already spent the credit. That is correct for reconciliation, so I wouldn't stop it at zero. Product should confirm.

**Entry order:** "Oldest first" means the order we applied them in, numbered with an increasing sequence. Each entry also stores `created`, so reports can sort by PayCo time. I'd confirm this reading with Finance.

## 4. Storage and surviving partial failures (depends on the store's API)

**Data model, with each account as one record:**
- `event:{id}` holds `{body_sha256, raw_body, type, created, received_at, status}`. Status is one of `received`, `applied`, `pending`, `ignored`, `quarantined` or `conflict`.
- `account:{id}` holds `{currency, balance, seq, entries[], payments{payment_id: {...refunded_by}}, pending_refunds{}, applied_event_ids}`.
- `incidents` and `quarantine` records for alerts and review.

Every event that moves money changes **exactly one account**, because a refund must match its payment's account. So all money changes for an event fit in one account-record write, and `balance` is saved alongside `entries` in that same write.

**Write order, with a plan for each failure point:**
1. Write `event:{id}` with status `received`, only if it doesn't exist yet. If it fails, return 503; nothing has changed.
2. Write the updated `account` record, with this event ID added to `applied_event_ids`. If it fails, return 503. On retry, step 5 of §2 finds the event `received`, the account doesn't list the event, so we apply it.
3. Update `event:{id}` to its final status. If it fails, return 503. On retry, the account already lists the event, so we only finish this step and don't apply the money again.

**What I need from ledgerkit's README:**
- Are single-key writes atomic? If one `put` can be partly written, this design needs changing.
- Is there a conditional write (put only if absent, or compare-and-set by version)? Without one, two workers can race. For now I'd use a lock per account inside the process. Before running more than one worker we'll need compare-and-set, or a store-side transaction.
- Is there a multi-key transaction? If so, steps 1–3 become a single commit, which is simpler.
- Are there size limits on a record? The `entries[]` list keeps growing. At a few thousand events a day this is fine for a long time. Later, entries move to their own keys with a balance snapshot.

**Recovery sweep:** a job finds events stuck in `received` for more than N minutes and finishes them the same way a retry would. This covers the case where PayCo's retry never arrives.

## 5. Status codes

| Situation | Code |
|---|---|
| Applied, duplicate, pending refund, ignored type, quarantined | 200 |
| Malformed JSON or missing envelope fields | 400 |
| Missing or bad signature, or stale timestamp | 401 |
| Same ID with a different body | 409 |
| `StoreError` | 503 |
| Unexpected exception | 500 |

## 6. Tests (standard library `unittest`)

- **Signatures:** valid, wrong secret, tampered body, header in mixed case, several `v1` values, `t` at exactly ±300 and ±301, `t` not a number, header missing.
- **Idempotency:** the same delivery 1, 2 and 10 times gives the same balance and entries. Same ID with a changed body gives 409 and nothing changes. An unsigned body sent under a real ID doesn't block the real event later.
- **Out of order:** refund then payment ends with the same balance as payment then refund. Entries are payment then refund.
- **Rejected cases:** refund with the wrong amount, wrong account or wrong currency; double refund; repeated `payment_id`; currency change; `amount` given as `true`, `25.0`, `"2500"`, 0 or negative; unknown type saved and returning 200.
- **Fault injection:** a wrapper store that raises `StoreError` on the k-th write. For every k, deliver, then retry until 2xx. The final state must equal one clean delivery. This is the test that matters most for reconciliation.
- **Randomized test:** a seeded generator of payments and refunds across accounts, with deliveries shuffled, duplicated and given random faults. Balances must equal the expected sum.
- **Concurrency:** threads delivering the same event, and a payment and its refund at the same time.

## 7. Build order

0. Get the ledgerkit README and settle the questions in §4.
1. Signature checks and envelope parsing, with tests.
2. Event registry, deduplication and conflict detection.
3. Payments and the account record; `balance` and `entries`.
4. Refunds, including pending refunds and the matching checks.
5. Quarantine and incident records.
6. Fault-injection and randomized tests, then fixes.
7. Recovery sweep and the reconciliation export.

## 8. Outside this codebase, but needed

- **HTTP layer:** pass the exact raw body bytes to `handle`; re-encoding them breaks the signature. Pass headers unchanged and keep the request timeout short.
- **Clock:** servers must keep accurate time (NTP), or the 300-second window will reject real deliveries.
- **Secret:** keep it in a secrets manager. Accepting several `v1` values lets us rotate it; consider letting `LedgerService` take a list of secrets.
- **Monitoring and alerts:** 401/409/5xx rates, any incident, any quarantined event, pending refunds older than 72 hours, and events stuck in `received`.
- **Reconciliation:** a daily diff of our entries against PayCo's CSV export, not only the monthly one. If we're down for more than 72 hours PayCo stops retrying and events are lost. The CSV, or a PayCo events API if there is one, is the way to backfill them.

## 9. Questions for the business

1. **Retry signatures:** does PayCo sign each retry with a fresh `t`? If not, every retry after 5 minutes fails the 300-second check.
2. **Currency mismatch:** for a payment in a different currency from the account, quarantine and handle by hand, or something else?
3. **Negative balances:** may a refund take a balance below zero?
4. **Pending refunds:** should a pending refund show up anywhere customers or support can see?
5. **Entry order:** should `entries` be ordered by when we applied them or by PayCo's `created` time?
