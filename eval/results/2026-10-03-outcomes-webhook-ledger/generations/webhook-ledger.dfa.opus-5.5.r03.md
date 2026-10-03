# BUILD PLAN: Ledger (customer balances from PayCo webhooks)

## 1. Classification and constraints
- **What:** A Python (standard library only) `LedgerService` that verifies PayCo webhooks and keeps per-account balances and entries from `payment.succeeded` and `refund.succeeded`. It replaces a nightly CSV-to-spreadsheet script and manual balance fixes.
- **Type:** Software. Brownfield: the spreadsheet is the system of record today. Not a small build, because it handles money and takes input from an outside party.
- **Dominant constraint:** Correctness. Balances must match PayCo's statement exactly, to the minor unit.
- **Worst failure:** An account gets credit PayCo never collected, through a forged event or a payment applied twice after a duplicate delivery or a partial write. The customer spends it, and nobody notices until the monthly reconciliation.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Correctness | 0 minor-unit difference per account vs PayCo statement | REQUIREMENT (brief: Finance reconciliation) | V3, V5 |
| Replay window | Reject if \|now − t\| > 300 s | REQUIREMENT (PayCo integration guide) | V2, V4 |
| RPO for acknowledged events | 0: no 2xx until the outcome is committed durably | REQUIREMENT (derived: PayCo treats any 2xx as delivered and will not reliably resend) | V1 |
| RTO | ≤ 4 h | ASSUMPTION. PayCo retries for 72 h (REQUIREMENT ceiling), so 4 h leaves about 68 h of headroom | Phase 3 restore drill |
| Latency | p99 `handle` below PayCo's delivery timeout | UNKNOWN: PayCo's timeout (PayCo docs or support; needed in Phase 2 for the sync/async row). Interim p99 ≤ 1 s (ASSUMPTION) | V0 baseline, Phase 3 exit |
| Throughput | ~5k events/day now; design check at 10× (50k/day) | ASSUMPTION (brief says "a few thousand"; confirm against PayCo export counts in Phase 0). Growth rate UNKNOWN (product owner) | Phase 3 exit |
| Drift detection window | ≤ 1 day (monthly today) | ASSUMPTION (a daily diff against PayCo's daily export is cheap) | Phase 3, V5 |
| Storage | ≤ 10 MB/day of raw event bodies | ASSUMPTION (~1–2 KB/event × 5k) | Phase 3 |
| Operational complexity | One process plus the managed DB; no queue, no cache | ASSUMPTION (team size not given) | Design |

**Missing inputs** (the ones that would change the plan most):
1. **ledgerkit README / MemoryStore API.** It is not in the workspace. I need to know whether the store has put-if-absent, compare-and-set, multi-key transactions or prefix scans, and whether a `StoreError` can be raised *after* the write took effect. These answers decide the data model (V1). The dev lead supplies them before Phase 1.
2. **Does Ledger also record spends?** I assume not: Ledger holds funded credit (payments minus refunds), and spending is recorded elsewhere. If Ledger must take debits, a debit API and its contract become a new phase before Phase 3.
3. **PayCo facts:** whether retries are re-signed with a fresh `t` (V4), the delivery timeout, whether a test mode exists, which fields the historical export has, and whether secret rotation is supported.
4. **Finance facts:** the statement format (gross or net, fees), what the manual spreadsheet fixes represent, and how unhandled event types (disputes) should be treated in reconciliation. These are needed by Phase 3.
5. **Named people:** a security reviewer (V2), the Finance approver (V5, V6), and the on-call owner for incident alerts.
6. **The meaning of "first payment"** for the currency rule. I assume the first payment *applied*, not the earliest by `created`.
7. **What depends on the spreadsheet.** The nightly script and support staff are CONFIRMED by the brief. That the product reads balances from the spreadsheet is ASSUMED; Phase 0 confirms it.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Data model and commit design (Phase 1) | MemoryStore API docs (missing input 1) | organizational / decision | specified |
| Data model hardening, and everything built on it | V1 (single-commit-point atomicity under `StoreError`) | validation | hardened |
| Final timestamp-tolerance design | V4 (PayCo re-signs retries) | validation | specified |
| Live PayCo traffic into Ledger (Phase 3) | V2 (signature, replay and conflict controls) and V3 (order/duplicate/fault correctness) | risk-security | exposed |
| Webhook registration and signing secret | PayCo dashboard admin access | organizational | deployed (Phase 0) |
| V4, and the skeleton's real request | PayCo test mode, or else live events in shadow | organizational | Phase 0 |
| Backfill of historical payments (Phase 3) | PayCo historical export with `payment_id` (data-access approval) | organizational | built |
| Reconciliation spec (Phase 3) | Finance sample statement and its gross/net rules (starts Phase 0) | organizational | specified |
| Product or support reading Ledger balances (Phase 4) | V5 plus Finance sign-off | validation / risk-security | exposed |
| Retiring the nightly script and spreadsheet | V6 plus a named Finance approver | risk-security / organizational | committed |
| Running more than one writer instance | Compare-and-set primitive (V1 result) | decision | deployed (deferred) |

No economic dependency binds: a few thousand events a day is far below any store or compute limit. Brownfield dependents of the spreadsheet: the nightly script and support staff are CONFIRMED; the product reading balances is ASSUMED.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R3: a balance that is wrong even briefly can be spent, and that loss is not recoverable | Consistent: no 2xx until committed; `StoreError` returns 503 and PayCo's retry does the recovery | Store outages are much shorter than 72 h | Cited basis: PayCo's 72 h retry vs a 4 h RTO (ASSUMPTION) | Store outages over 24 h observed → add a durable local inbox in front of the store |
| Ledger data model | R3: reconciliation and every later handler rest on it once real data exists | Append-only: one entry per event; **one conditional write is the commit point** for each event's outcome; balance is the sum of entries; every verified raw body is kept in an inbox keyed by event `id` | The store offers put-if-absent or an atomic multi-key write; a `StoreError` may hide a write that succeeded | V1 | V1 fails → a per-account single-key log with version compare-and-set; if the store has neither primitive, stop and ask for a transactional store |
| Unknown event types | R3: answering 2xx without storing the event loses it for good | Verify, store raw in the inbox, return 200 `{"status":"ignored"}`, replay from the inbox when a handler ships | PayCo stops redelivering after a 2xx | Cited basis: PayCo retry semantics in the guide | Inbox storage cost exceeds budget → keep raw bodies only for types we expect to handle |
| Timestamp tolerance vs 72 h retries | R3: rejecting valid retries loses money events permanently | ±300 s, as the guide says | Each PayCo retry is re-signed with a fresh `t` | V4 | V4 fails → escalate to PayCo; accept a stale `t` only for an `id` already in the inbox (idempotency makes that replay harmless); recover the rest through the daily reconciliation |
| Sync vs async processing | R2: weeks to put a queue in front | Synchronous: verify → commit → respond inside the request | Commit p99 is well under PayCo's timeout (UNKNOWN) | V0 baseline plus the Phase 3 latency check | p99 above 50 % of PayCo's timeout → store in the inbox first, return 2xx, apply asynchronously |
| Refund arriving before its payment | R2: changes stored state and entry order | **Park** the refund (return 200 `parked`) and apply it right after its payment commits. Then check account, amount and currency, and allow at most one refund per `payment_id` | Payments arrive within the retry window, or are in the backfill | V3, plus the Phase 3 metric for parked-refund age | A parked refund whose payment never arrives shows up in shadow → close the backfill gap, or agree a negative-balance policy with Finance |
| Writer concurrency | R2: moving to multiple instances needs a compare-and-set commit | One process with a per-account lock; a sweep releases parked refunds after every payment commit and on a timer | 5k events/day fits easily in one process; PayCo's retries cover downtime | V1 (whether compare-and-set exists) and the Phase 3 load check | Need for high availability or more throughput → a compare-and-set based commit and multiple instances |
| Data-privacy boundary | R2: redacting stored bodies later means a data migration | Raw bodies only in a restricted inbox table; logs carry `event_id`, `account_id`, type, amount and outcome, and never the body, signature or secret; out of PCI scope (PayCo holds card data) | Payloads carry no card numbers and no personal data beyond `account_id` | Phase 3 scan of the stored inbox against PayCo's event catalogue | Personal data found (for example dispute evidence) → field-level redaction before storage, plus a retention policy |
| System of record and cutover | R3: balances customers see, and Finance's records | The spreadsheet stays system of record. Ledger runs in shadow, data flows one way (PayCo → Ledger), then cuts over by cohort; the nightly script stays runnable until V6 | Shadow diffs show where Ledger is wrong | V5, V6 | V5 fails → stay in shadow |
| Opening balances | R2: re-backfilling is days of work | Backfill historical payments and refunds from PayCo's export, keyed by `payment_id`. Do not import the hand-fixed spreadsheet | The export has `payment_id`, `account_id`, amount, currency and the refund link | V5 (backfilled totals vs statement) | The export lacks ids → one opening-balance entry per account from a Finance-signed statement, plus a lookup table for refunds of older payments |

**R1 defaults:**
- Monolith vs services → one library class in one process.
- Build vs buy → build (REQUIREMENT: standard library only).
- Body comparison for conflicts → SHA-256 of canonical JSON (sorted keys, compact form). The raw-bytes hash is also stored for forensics.
- Entry order → commit sequence, with `created` included in each entry.
- Body conflict → 409, write an incident record, alert; the account is not touched (REQUIREMENT).
- Anomalies (currency mismatch, refund amount/account/currency mismatch, second refund or duplicate `payment_id` under a new `id`, a schema violation in a signed body) → keep them in quarantine, never apply them, return 200 `quarantined`, page on-call.

**N/A:** Tenancy model (one merchant account); AI rows (no AI component).

## 4. Walking skeleton (Phase 0)
- **Real request:** a PayCo test-mode `payment.succeeded` for 2500 EUR → 200 `{"status":"applied","event_id":…}`. Then an internal `ledger balance acct_test` returns 2500. If PayCo has no test mode, the skeleton uses one live event in shadow.
- **Tiers:** PayCo → TLS ingress → standard-library HTTP adapter (passes the raw bytes and headers through untouched) → `LedgerService.handle` → managed DB in a shadow namespace → response. Plus an internal read-only CLI that calls `balance` and `entries`.
- **Already present in the skeleton:** a case-insensitive header lookup; signature parsing; the time check uses the injected clock; HMAC over the exact header `t` string plus `.` plus the raw bytes, compared with `hmac.compare_digest`; the signature is checked *before* the JSON is parsed. Event handling is naive: payments only, no deduplication yet. Not hardened.
- **Deployed:** the CI pipeline runs tests and builds a versioned artifact with a pinned Python version, then deploys to production.
- **Logged:** one JSON log line per delivery: request id, `event_id`, type, status, outcome, latency, clock skew (now − t). No body, no signature.
- **Monitored:** alerts on 5xx rate, 401 rate and `StoreError` count.
- **Rolled back:** redeploy the previous artifact. The kill switch is disabling the endpoint in PayCo; the 72 h retry buffer means nothing is lost.
- **Who can reach it:** the URL has to be public for PayCo to reach it, but only verified requests have any effect. It receives test-mode events only, writes only to the shadow namespace, and nothing reads it (not the product, not support).

Exit check (V0): see §6.

## 5. Phases

**Phase 1: Ledger core (data model and idempotency)**
- **Unlocks:** every event handler, and live shadow traffic.
- **Depends on:** V0; MemoryStore API docs.
- **Tasks:**
  1. Read the MemoryStore API.
  2. Build a `FaultyStore` wrapper that raises `StoreError` before, or after, the Nth write.
  3. Run V1 against six scenario scripts: payment, duplicate, refund, refund-first, unknown type, conflict.
  4. Specify the records:
     - inbox (`id` → raw body, canonical hash, raw hash, received time)
     - outcome or entry (`id` → applied, ignored, parked or quarantined, plus the signed amount); this is the single commit write
     - `payment_id` uniqueness for payments and for refunds
  5. Implement the flow in this order: signature → JSON parse → inbox put-if-absent with a body comparison (a different body gives 409 plus an incident) → **"duplicate" is decided from the outcome record, never from the inbox record alone.** An inbox hit without an outcome means a retry after a partial failure, so roll forward. Then commit and respond.
  6. Map any `StoreError` to 503 with no partial effect visible.
  7. `balance` is the sum of committed entries, 0 for an unknown account; `entries` is in commit order, `[]` for an unknown account.
  8. Unknown types are stored and return 200 `ignored`.
- **Rollback:** shadow data only. Wipe the namespace and redeploy the Phase 0 artifact.
- **Exit check:** V1 passes. An unsigned request carrying an existing `id` gets 401 and never touches the inbox, so nobody can plant a fake body under a real event `id`.

**Phase 2: Money semantics**
- **Unlocks:** correct balances for both handled types; readiness for live traffic.
- **Depends on:** Phase 1, V1.
- **Tasks:**
  1. Strict validation:
     - `amount` is an `int`, not a `bool` or `float`, and > 0
     - `currency` matches `^[A-Z]{3}$`
     - ids are non-empty strings; `created` is an `int`
     - a signed body that fails validation goes to quarantine
  2. Currency is fixed by the first applied payment; a payment in another currency goes to quarantine.
  3. Refund handling:
     - payment not yet seen → park
     - payment seen → check account, amount and currency, allow one refund per `payment_id`, then apply a negative entry
     - otherwise → quarantine
     - a refund of a quarantined payment → quarantine
  4. Release parked refunds when the payment commits (roll forward if interrupted), plus a timed sweep; alert when a refund has been parked longer than 96 h.
  5. Reprocess tool: re-runs ignored or quarantined inbox events after a code fix, idempotently.
  6. Parse multiple `v1` values in the signature header.
  7. Run V3, run V2 and get the reviewer's sign-off, and finish V4 (it started in Phase 0).
- **Rollback:** redeploy the Phase 1 artifact; rebuild the shadow state from the inbox.
- **Exit check:** V2, V3 and V4 pass.

**Phase 3: Live shadow, backfill and reconciliation** (system of record is still the spreadsheet; data flows one way, PayCo → Ledger)
- **Unlocks:** evidence for the cutover.
- **Depends on:** V2, V3, V4; the PayCo export; the Finance statement sample.
- **Tasks:**
  1. Register the live endpoint with a live secret.
  2. Run the backfill from PayCo's export, keyed by `payment_id`, so it is idempotent and webhook duplicates of the same events collapse onto it.
  3. Daily reconciliation job: Ledger vs PayCo's daily export (the one the nightly script already pulls) and vs the spreadsheet, per account, with differences sorted into categories.
  4. Monthly reconciliation against the statement, with Finance.
  5. Restore drill to measure RTO.
  6. Replay 10× volume from the inbox against a staging copy to check latency and throughput.
  7. Scan the stored inbox for personal data.
  8. Run V5.
- **Rollback:** disable the live endpoint. Ledger has no consumers yet, so the shadow data can be dropped and re-backfilled.
- **Exit check:**
  - V5 passes
  - RTO ≤ 4 h shown in the drill
  - p99 within the latency budget at 10×
  - rebuilding the state from the inbox gives exactly the live state

**Phase 4: Cutover by cohort, then decommission**
- **Unlocks:** Ledger becomes the system of record.
- **Depends on:** V5.
- **Tasks:**
  1. Product and support read balances from Ledger (read-only credentials) behind a per-cohort flag: internal accounts → 5 % → 25 % → 100 %. Each step needs a clean daily reconciliation.
  2. The spreadsheet becomes read-only.
  3. Run V6, then retire the nightly script.
- **Rollback:** flip the cohort flag back to the spreadsheet path. **The point of no return is retiring the script, guarded by V6.** The spreadsheet is archived, not deleted.
- **Exit check:** V6 passes.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, seen in logs and alerts, and rolled back across every tier in production | A test-mode event end to end; deploy and roll back through CI; inject a `StoreError` through a fault flag | One trace across PayCo → ingress → adapter → service → DB; a deploy and a rollback succeed; the injected `StoreError` gives 503, the alert fires, and PayCo's retry applies the event once; first latency and skew values recorded as BASELINE (a floor at near-zero load) | CI run log, a trace export and the alert record, kept in the repo `evidence/v0/` | Phase 1. If it fails: fix the pipeline before any further work | 0 |
| V1 | A single conditional write can be the commit point, so any `StoreError` leaves the event either not applied or recoverable by retry | `FaultyStore` exhaustive injection: every write index × {fails before the write, fails after the write} × 6 scenarios, retried until 2xx | 0 deviations from the fault-free final state (balances, entries, outcomes): REQUIREMENT (exact match) | Test report plus the scenario scripts in `evidence/v1/` | Hardening the data model, then Phases 2 and 3. If it fails: the data-model flip in §3 | 1 |
| V2 | Only authentic, fresh, consistent deliveries can change state | Vector suite: 1-byte body tamper, `t` tamper, wrong secret, `t` = now ± 300 accepted and ± 301 rejected, missing or malformed header, header-case variants, several `v1` values with one valid, unsigned request carrying an existing `id`, same `id` with a different signed body; code review for `compare_digest` and verify-before-parse; a check that logs contain no secret, body or signature | 100 % of vectors pass; the 409 leaves balances and entries byte-identical; the named security reviewer signs off (name UNKNOWN; the engineering lead supplies it in Phase 0) | Vector suite results plus the signed review in `evidence/v2/` | Live traffic in Phase 3. If it fails: no live registration | 2 |
| V3 | Final state does not depend on delivery order, duplicates or `StoreError` | Seeded property test: random histories (multiple accounts and currencies, refunds, unknown types, conflicts, anomalies), shuffled, each event delivered 1–4 times including after a 2xx, random `StoreError`s, retried until 2xx; compared against an independent oracle | 0 mismatches over 10,000 histories (REQUIREMENT for 0; 10,000 is a design parameter); invariants hold: balance = sum of entries, ≤ 1 entry per `id`, ≤ 1 payment and ≤ 1 refund per `payment_id` | Run report plus failing seeds in `evidence/v3/` | Phase 3. If it fails: fix it and re-run before any live traffic | 2 |
| V4 | PayCo re-signs every retry with a fresh `t` | In test mode, return 503 for one event across retries that span more than 300 s; record the header `t` against our receive clock | Every retry's `t` is within 300 s of when we received it (REQUIREMENT ±300 s from the PayCo guide) | Captured delivery log in `evidence/v4/` | The final tolerance design and Phase 3. If it fails: the tolerance flip in §3 | Starts in 0, required before 3 |
| V5 | Ledger matches PayCo exactly on live data, backfill included | Daily per-account diff against PayCo's daily export for 30 consecutive days, plus one full monthly statement reconciled with Finance | 0 unexplained minor-unit differences (REQUIREMENT). The only allowed explanation is events of unhandled types listed from the inbox, signed off by the Finance approver (name UNKNOWN; Finance supplies it in Phase 0) | Daily diff reports plus the signed monthly reconciliation, kept in Finance-restricted storage | Phase 4. If it fails: stay in shadow, fix, restart the 30-day count; if disputes cause the differences, bring dispute handling forward | 3 |
| V6 | The nightly script can be retired safely | Rehearsal: one monthly close with Ledger as system of record and the script running read-only alongside; restore the script from the archive once | Go/no-go: 0 differences at that close, no quarantined events older than 7 days, restore of the script tested; the Finance approver signs | Rehearsal report, restore log and the signed go/no-go in Finance-restricted storage | Retiring the script. If it fails: keep the script and repeat next month | 4 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Secret held in a secret manager; signature and time check before parsing; shadow namespace; nothing reads the data | JSON log line per delivery (`event_id`, type, outcome, status, latency, skew) tagged as internal financial data; alerts on 5xx, 401 and `StoreError` | Pinned Python version, standard library only, versioned CI artifact; injected clock; signed test fixtures using a synthetic secret | `StoreError` returns 503 so PayCo retries; endpoint kill switch in PayCo |
| 1 | 409 for a body conflict plus an incident record and a page; unsigned requests never write to the inbox | Counters per outcome (applied, duplicate, ignored, conflict); a roll-forward recovery counter | `FaultyStore` and the V1 scenarios checked in and deterministic | A single commit write; roll forward on retry; a `StoreError` is treated as an unknown outcome |
| 2 | Strict schema (rejects `bool` and `float` amounts); multiple `v1` values; V2 review | Count and age gauges for quarantined and parked events, with alerts | Property-test seeds recorded; reprocess tool is deterministic | Parked-refund sweep; reprocess quarantined events after a fix |
| 3 | Live secret issued; inbox access limited to Finance and on-call; personal-data scan | Daily reconciliation report with an alert on any difference; alert when no live delivery arrives for 6 h | Idempotent, rerunnable backfill; rebuild from the inbox equals live state | Restore drill (RTO); load check at 10× |
| 4 | Read-only DB credentials for product and support; balances no longer edited by hand | Per-cohort diff of Ledger vs spreadsheet; support dashboard | Cohort flag configuration under version control | Per-cohort flag rollback; nightly script archived and restorable until V6 |

## 8. AI layer
N/A: no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Handlers for disputes, payouts and other types.** The inbox already keeps them for replay. Pull forward when V5 finds such events changing an account's statement total, or Finance will not accept them as explained differences.
- **Partial refunds.** Pull forward when the quarantine shows a refund amount ≠ its payment amount.
- **Spend or debit API, and manual adjustments.** Pull forward if missing input 2 says Ledger owns spends, or support needs corrections after the cutover.
- **Cached balance** (balance kept as a stored projection instead of summed). Pull forward when `balance` p99 exceeds the latency budget, roughly above 1,000 entries per account.
- **More than one writer, or high availability.** Pull forward when one instance can't meet the 4 h RTO or the throughput budget. Needs compare-and-set (V1).
- **Store-first, apply-later processing.** Pull forward when p99 passes 50 % of PayCo's timeout.
- **Two secrets during rotation.** Pull forward when PayCo schedules a rotation or a 409 incident suggests the secret leaked.
- **PayCo IP allow-list.** Pull forward when PayCo publishes stable sending IP ranges.
