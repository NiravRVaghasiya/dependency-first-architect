# BUILD PLAN: Ledger (PayCo webhooks → customer balances)

> Note: the `README.md` in the workspace says the directory is empty. It does not describe `ledgerkit`'s `MemoryStore`. The store's write semantics decide how Ledger writes data safely, so the plan treats them as missing input #1 and checks them first (V1).

## 1. Classification and constraints
- **What:** A Python module (standard library only) that checks PayCo webhook signatures, applies `payment.succeeded` / `refund.succeeded` exactly once to an account ledger that only ever gets new entries added, and returns balances and entries. It runs behind our existing HTTP layer.
- **Type:** Software. Brownfield: it replaces the nightly CSV-to-spreadsheet script and support staff's manual fixes. Not a small build: it handles money, receives traffic from outside, and Finance must reconcile it exactly.
- **Dominant constraint:** Correctness.
- **Worst failure:** A balance that is silently wrong and that we answered with a 2xx, so PayCo never redelivers. Examples: a `StoreError` partway through a request leaves an event marked "done" without its entry (lost credit), or a retry applies the same event twice (double credit). V2 guards this.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Reconciliation accuracy | 0 minor-unit difference per account against PayCo's monthly statement | REQUIREMENT (Finance, brief) | V4, V5 |
| Replay window | Reject if \|now − t\| > 300 s; accept at exactly 300 s | REQUIREMENT (PayCo integration guide) | V3 |
| RPO for acknowledged events | 0: a 2xx is sent only after the movement is durably written | REQUIREMENT (follows from exact reconciliation and PayCo treating 2xx as delivered) | V2 |
| RTO (endpoint outage) | Hard ceiling < 72 h (PayCo's retry window). Working target 4 h, with an alert within 15 min of sustained non-2xx | REQUIREMENT (ceiling, PayCo guide) / ASSUMPTION (4 h, 15 min: well inside the ceiling) | V0, Phase 2 |
| Latency (`handle`) | Must finish within PayCo's delivery timeout. Working target p99 < 1 s | UNKNOWN timeout (from PayCo docs; owner of the PayCo integration; needed in Phase 0) / ASSUMPTION p99 | V0 baseline, V4 |
| Throughput | A few thousand events/day (≈0.05/s average). Design headroom 10 events/s peak | REQUIREMENT (volume, brief) / ASSUMPTION (headroom: ~100× average for bursts and growth) | V4 |
| Storage | About 1 KB of raw body per event, a few MB/day | ASSUMPTION (size of the sample body) | Phase 2 |
| Operational complexity | One module inside the existing HTTP service, the existing managed DB, no queue | ASSUMPTION (PayCo's retries act as the queue; see §3) | V4 |

**Missing inputs** (these would change the plan most):
1. **`ledgerkit` MemoryStore API and failure semantics.** Does it offer atomic multi-key writes, put-if-absent / compare-and-set, and prefix scans? Can a write that raised `StoreError` still have been saved? Is there a fault-injection hook? This decides the write protocol (V1). From the ledgerkit owner, needed at the start of Phase 1.
2. **PayCo facts:** delivery timeout; whether test mode exists; whether each retry is re-signed with a fresh `t` (if not, retries older than 300 s can never verify); published source IPs; whether there is an event-list API. From PayCo docs or the account manager, needed in Phase 0.
3. **Opening state:** does the PayCo export include event IDs and `payment_id`, and how far back does it go? What does the spreadsheet balance include (manual corrections? spend?). From Finance and Support, needed in Phase 2.
4. **Spend.** The interface has no debit for spending. **Assumption:** Ledger holds PayCo-funded credit only, and the product records spend elsewhere. If Ledger must also own spend, the data model changes (an R3 row in §3). Please confirm before Phase 1 specifies it.
5. **Finance decisions and people:** sign-off on parking unmatched refunds and quarantining anomalies (§3); a named Finance approver for cutover (V4, V5); a named security reviewer (V3).

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Write protocol and key layout (Phase 1) | V1: store contract confirmed (missing input 1) | decision / validation | specified |
| Handlers and data model | V2: exactly-once under faults, retries, duplicates, reordering | validation | hardened, exposed to live events |
| Live webhook registration (Phase 2) | V2, plus V3 (signature and replay control reviewed) | risk-security | exposed |
| Phase 0 deploy | Production DB credentials and namespace, data-access approval, webhook secret from the PayCo dashboard (ASSUMED needed) | organizational | deployed |
| Signature check in production | Host clock synced by NTP (more than 300 s of skew rejects every delivery) | runtime | deployed |
| Refund parking and quarantine policy | Finance sign-off (missing input 5); the §3 default stands until then | decision / organizational | hardened |
| Opening-state backfill | PayCo export containing `payment_id` (missing input 3) | organizational | built |
| Support and product reading Ledger balances (Phase 3) | V4, plus the named Finance approver | validation / organizational | exposed |
| Retiring the nightly script | V5 | risk | committed |
| Consumers of the spreadsheet (support tooling, Finance reconciliation, possibly the product's balance reads) | Discovered in Phase 0 | structural (brownfield) | cut over. Support and Finance CONFIRMED (brief); product reads ASSUMED |

No economic dependencies: volume is tiny and the managed DB already exists.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R3: a wrong 2xx causes financial harm that can't be undone | Consistency. Send 2xx only after an idempotent, durable apply. On `StoreError` send 503, on unexpected errors 500, and let PayCo retry | PayCo retries non-2xx for up to 72 h (guide) | V2; basis: 72 h retry REQUIREMENT | V2 fails, or an outage gets near 72 h → add a durable inbox (see sync row) |
| Sync vs async | R2: adding an inbox later is additive | Apply synchronously inside `handle`. PayCo's retries are the queue | p99 of `handle` is well under PayCo's timeout (UNKNOWN, Phase 0) | V0 baseline, V4 | p99 > ½ PayCo timeout, or volume > 10/s → write the verified raw event, return 2xx, apply asynchronously from the inbox |
| Ledger data model | R3: real financial history; migrating it is costly | Entries are only ever added, keyed by `event_id`. Balance = sum of entries, never a counter updated in place. Separate records: event (raw body, SHA-256 fingerprint, state), payment index by `payment_id`, pending refunds by `payment_id`, account currency, quarantine | Summing per account stays cheap at current volume | V2 | V2 fails, or `balance()` p99 on the largest account goes over budget → add a cached balance that can be rebuilt from entries (still not an in-place counter) |
| Idempotency and write order | R3: decides whether a partial failure loses or doubles money | Every write is idempotent and treated as "maybe applied" if it raised. Order: verify → check and claim the event record (fingerprint, `received`) → write entry, indexes and currency idempotently → set `applied` last. A retry finds `received` and finishes the job | The store's single-key writes are atomic (V1) | V1, V2 | V1 shows atomic multi-key commit → one transaction per event. V1 shows single-key writes can tear → re-plan the storage layer |
| Concurrency control | R2: affects deployment, not data | Put-if-absent on the event claim, payment index and account currency | The store has a conditional put (V1) | V1 | No conditional put → a single worker process with a per-account lock, until the store adds one |
| Natural key alongside `event_id` | R3: protects against double credit across backfill and webhooks | At most one credit per `payment_id` and one refund per `payment_id`. A second one with a different `event_id` goes to quarantine | Refunds are always full (brief) | V2 | PayCo starts partial refunds → allow several refunds per payment, capped at the payment amount |
| Refund before its payment | R2: stored pending state that Finance can see | Store as pending under `payment_id`, return 202, apply when the payment is applied. Not in `balance` or `entries` until then | Finance agrees; every refunded payment eventually arrives or is backfilled | Finance sign-off; V4 (age of pending refunds) | Finance wants refunds applied at once, or V4 finds refunds of payments that will never arrive → apply immediately, flagged as unmatched |
| Anomalies (currency ≠ account currency, refund amount ≠ payment amount, refund on another account, second credit or refund for one `payment_id`) | R2 | Return 2xx, quarantine without applying, alert, Finance resolves | A retry cannot fix these, and a 4xx would be dropped by PayCo after 72 h, losing the record | V2 (behaviour); Finance sign-off (policy) | Finance prefers rejection |
| Unknown event types | R2: if dropped now, they can't be replayed when handlers arrive | Verify, check fingerprint, store the raw body, return 200 `ignored`. No entry is written | The brief says these will be handled later | V2 | None expected. Revisit retention if storage grows |
| Same-`id` comparison | R2: false alarms leave a delivery stuck in retries | SHA-256 of the raw body bytes. A different body gets 409, an incident log and alert, and no account writes (REQUIREMENT) | PayCo redelivers byte-identical bodies (brief) | V4 (0 false incidents) | Real redeliveries trigger mismatches → compare canonical JSON (sorted keys) instead |
| Migration, system of record, cutover | R3: Support and Finance depend on balances | Run in parallel. The spreadsheet stays the system of record through Phase 2 while Ledger runs in shadow (nothing reads its balances). Cut over after V4. Keep the script restorable until V5 | Shadow output can be compared with the PayCo export every day | V4, V5 | V4 fails → fix and restart the cycle; the spreadsheet stays the system of record |
| Data-privacy boundary | R2 | Raw bodies live only in the ledger namespace with restricted access. Logs carry `event_id`, `type`, `account_id`, `amount`, `currency`, result and request ID, never the body, signature header or secret | Bodies contain no personal data beyond IDs (UNKNOWN until a live body is inspected in Phase 0) | Phase 0 inspection | Bodies contain personal data → classify, add retention and redaction |

**R1 defaults:**
- Services: one module (`ledger/`) inside the existing HTTP service, no microservices.
- Build vs buy: build. Standard library only is a REQUIREMENT, and the scope is small.
- Status codes: 200 applied / duplicate / ignored / quarantined; 202 parked; 400 signed but malformed body (PayCo's retries give us 72 h to fix the parser); 401 bad or stale signature; 409 id-body conflict; 503 `StoreError`; 500 anything else. Response bodies contain no internal details.
- `entries` order: the order movements were applied, with `created` stored on each entry.
- Signature header: accept any of several `v1=` values; parse header names case-insensitively; compare with `hmac.compare_digest`.
- Payload checks: `amount` must be an `int` > 0 and not a `bool` (Python counts `bool` as an `int`); `currency` must match `^[A-Z]{3}$`.

**N/A:** AI rows (no AI component).

## 4. Walking skeleton (Phase 0)
- **Request:** a PayCo test-mode `payment.succeeded` (2500 EUR to `acct_test`) is POSTed to the production URL `/webhooks/payco`. Response: `200 {"status":"applied"}`. Then an internal ops command `ledger balance acct_test` returns `2500`.
- **Tiers crossed:** PayCo → our HTTP layer → `LedgerService.handle` (signature check written but not yet reviewed; unvalidated write order) → managed DB, in a separate disposable `ledger_shadow` namespace → response → ops command → `balance()`.
- **Deploy, log, monitor, roll back:** deployed through the existing pipeline behind the `ledger_webhook_enabled` flag. One structured log line per delivery with request ID and `event_id`. Counters per result (applied / duplicate / 4xx / 5xx / conflict). Alerts on any 409 and on non-2xx sustained for more than 15 min. Roll back by redeploying the previous version; PayCo's retries cover the gap.
- **Who can reach it:** the endpoint is reachable from the internet (PayCo has to reach it) but is registered for PayCo test-mode events only. Source-IP allow-listing is added if PayCo publishes IP ranges. No person or system reads its balances, so no real money depends on it. If PayCo has no test mode, it takes live deliveries in shadow mode, still into the disposable namespace and still with no consumer.
- **Brownfield in Phase 0:** list everything that reads the spreadsheet or the script's output (CONFIRMED or ASSUMED). Record baselines: manual balance corrections per month, and the current mismatch count at Finance's monthly reconciliation.
- **Exit check (V0):** see §6.

## 5. Phases

- **Phase 1: Correct core (data model, write protocol, trust boundary)**
  - **Unlocks:** live shadow (Phase 2).
  - **Depends on:** V0; missing inputs 1 and 4.
  - **Tasks, widest rework first:**
    1. Store-contract spike (V1).
    2. Key layout and data model (§3).
    3. PayCo simulator for V2: signs deliveries, shuffles, duplicates (including after a 2xx), tampers with bodies, retries every non-2xx like PayCo, and injects `StoreError` at each write position. Built before the handlers.
    4. Write order and concurrency, as V1 decides.
    5. Trust boundary: verify `t.` + raw bytes before any JSON parsing; skew check against the injected `clock`; header parsing; known-answer test vectors (V3).
    6. Payload validation.
    7. Handlers: payment (put-if-absent on currency, `payment_id` uniqueness, then release any pending refund); refund (match / park / quarantine); unknown types; id-body conflict.
    8. `balance()` (0 for an unknown account) and `entries()`.
    9. Status mapping and log/metric hooks.
  - **Rollback:** nothing live depends on it. Revert the commit and wipe the shadow namespace.
  - **Exit check:** V1, V2 and V3 pass.

- **Phase 2: Live shadow, backfill, reconciliation**
  - **Unlocks:** cutover (Phase 3).
  - **Depends on:** Phase 1, V2, V3; missing input 3.
  - **Tasks, smallest exposure first:**
    1. Put the live secret in the secret manager and register the live webhook. Ledger stays shadow-only.
    2. Backfill PayCo history up to a cutoff through the same apply functions, keyed by `payment_id`, so events in the overlap with live webhooks are deduplicated. Every run is written to an audit log.
    3. Daily reconciliation job: per-account diff against the PayCo CSV export, alert if any diff is nonzero.
    4. Ops views: pending refunds by age, quarantine, incidents.
    5. Run V4.
  - **Rollback:** unregister the webhook or turn off the flag. The spreadsheet is still the system of record, so nothing else is affected.
  - **Exit check:** V4 passes. Every quarantined item and every pending refund older than 72 h has a resolution recorded by Finance.

- **Phase 3: Cutover and retiring the script**
  - **Unlocks:** Ledger becomes the system of record for PayCo-funded balances.
  - **Depends on:** V4 and the Finance approver.
  - **Tasks:**
    1. Point Support's read view at Ledger, for one Support pod first and then everyone. The nightly script keeps running in parallel and differences are reported daily.
    2. Point the product's balance reads at Ledger, if Phase 0 confirmed it reads them.
    3. After one more statement cycle, run V5. Then disable the script (keep it restorable) and archive the spreadsheet read-only.
  - **Rollback:** point consumers back at the spreadsheet, which stays current until V5. The point of no return is the script being disabled and manual fixes stopping; V5 guards it.
  - **Exit check:** V5 passes, and Support has made no manual balance fixes for one cycle.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A delivery can be deployed, observed and rolled back through every tier in production | §4 test event; deploy and rollback through the pipeline; force `StoreError` with a fault flag | Event → 200 with one request ID across HTTP layer, service and store. Deploy and rollback succeed. The injected fault gives 503, the alert fires within 15 min, and PayCo's re-signed retry is applied exactly once. p50/p99 latency of `handle` and of store writes recorded as BASELINE (near-zero load floor) | Pipeline run IDs, alert record, log excerpt, `baselines.md` in the repo | Phase 1. If the retry is not re-signed (stale `t`), re-plan the replay-window handling with PayCo | 0 |
| V1 | The store's single-key writes are atomic, and we know whether it has conditional put, multi-key transactions and prefix scans | Spike against `ledgerkit` MemoryStore and the managed DB: fault-inject each operation; check whether a write that raised was still saved | Each operation documented as atomic or not, conditional or not, may-have-saved or not. Signed off by the ledgerkit owner (named in Phase 1) | `docs/store-contract.md` plus spike tests in the repo | Specifying the write order. If it fails, see the flips in §3 (transaction, single writer, or re-plan the storage layer) | 1 |
| V2 | Every 2xx means the movement was applied exactly once, despite faults, duplicates, reordering and tampered redeliveries | Simulator (Phase 1, task 3) compares results with an oracle: (a) one `StoreError` at every write position of every event type, exhaustively; (b) 10,000 random sequences with random fault rates, shuffled order, duplicates and tampering | 0 balance differences from the oracle (REQUIREMENT: exact match). No `event_id` or `payment_id` applied twice. Conflicting events change nothing. Every sequence ends with every legitimate event at 2xx | CI test report and the seeds of any failing runs, kept as CI artifacts | Live shadow. If it fails, fix the write order (§3 flips) and rerun | 1 |
| V3 | Only PayCo-signed deliveries that are fresh (≤ 300 s) reach the ledger | Known-answer vectors: valid; wrong secret; one byte of body changed; skew of ±300 / ±301 s; missing or garbled header; header name in mixed case; several `v1`; non-integer `t`. Code review | All pass (300 s: REQUIREMENT). Pass/fail sign-off by the named security reviewer (UNKNOWN, missing input 5) | Test report plus the signed review in the repo | Registering the live webhook. If it fails, fix and re-review | 1 |
| V4 | Ledger matches PayCo exactly on real traffic | Live shadow with daily reconciliation, plus one full monthly statement | 0 minor-unit differences per account for 30 consecutive days and for one complete statement (REQUIREMENT). 0 false id-body incidents. p99 within the latency row of §1 | Daily diff reports and the statement reconciliation, in Finance's shared drive | Phase 3. If it fails, find the root cause, fix, and restart the 30 days; the flips in §3 apply | 2 |
| V5 | Ledger can be the only system of record | One statement cycle after cutover, with the script running in parallel | 0 differences (REQUIREMENT). Go/no-go by the named Finance approver. Restoring the script rehearsed once | Signed go/no-go and the restore-rehearsal log | Disabling the script. If it fails, keep the parallel run going | 3 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Secret from the secret manager, never logged; signature check in place; test mode only; NTP on hosts | Structured log per delivery; counters per result; alerts on 409, sustained non-2xx, clock skew | Deploy through the pipeline with a pinned Python version; flag-gated | Rollback rehearsed; PayCo's retries cover downtime (V0) |
| 1 | `compare_digest`; verify before parsing; reject `bool` amounts; 409 path makes no writes (V3) | Every result and every anomaly or quarantine reason gets a log code and a counter | Simulator with fixed seeds; failing seeds saved as regression tests (V2) | Idempotent writes, `applied` marker set last (V1, V2) |
| 2 | Live secret, restricted access to the namespace; inspect a live body for personal data; backfill audit log | Daily reconciliation diff alert; alerts on pending-refund age and quarantine count | Backfill script can be rerun safely (keyed by `payment_id`) | Alert if an outage approaches the 72 h ceiling; the reconciliation job catches events PayCo gave up on |
| 3 | Read access for Support limited to the balance view | Daily diff Ledger vs script until V5 | Cutover runbook in the repo | Repointing consumers back is rehearsed; script kept restorable (V5) |

## 8. AI layer
N/A: no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Handlers for disputes, payouts and other types.** Pull forward when the store of ignored events shows the type for our account, or the product needs it. Stored raw events are replayed through the new handler.
- **Async inbox or queue.** Pull forward on the sync row's flip in §3.
- **Cached balance.** Pull forward when `balance()` p99 on the largest account goes over budget.
- **Multiple workers without a conditional put.** Pull forward when one worker can't keep up and V1 found no conditional put.
- **Dual-secret rotation.** Pull forward before the first planned rotation, or immediately on a suspected leak.
- **Recovering missed events through a PayCo event-list API.** Pull forward when reconciliation finds an event PayCo stopped retrying.
- **Spend debits.** Pull forward if missing input 4 says Ledger owns spend. That is an R3 change to the data model.
- **Partial refunds and multi-currency accounts.** Pull forward when PayCo or the product introduces them.
