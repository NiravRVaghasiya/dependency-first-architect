# BUILD PLAN: Ledger (PayCo webhook balances)

> The workspace has no code apart from a README, and that README says the directory is empty. The `ledgerkit` README that the brief points to, which describes the `MemoryStore`, isn't in the workspace. So the store's contract is the main missing input. The plan assumes a storage design and checks it at V1 before anything that depends on it is designed in detail.

## 1. Classification and constraints
- **What:** A Python service using only the standard library. It turns PayCo webhooks into a per-account ledger of signed movements and derives each balance from them. It replaces the nightly CSV-to-spreadsheet job and manual fixes.
- **Type:** Software. Brownfield: the spreadsheet is the current system of record. Not a small build, because it handles money and its numbers are reconciled to the minor unit.
- **Dominant constraint:** Correctness, with exactly-once effect on the balance under retries, duplicates, out-of-order delivery and partial write failures.
- **Worst failure:** A balance that silently drifts from PayCo's. This can happen three ways: we return 2xx for an event that never committed (lost credit), we apply one event twice, or a forged or tampered event changes a balance.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Correctness | 0 minor-unit difference per account vs PayCo's monthly statement | REQUIREMENT (brief: Finance) | V3, V5 |
| Signature freshness | Accept only if \|clock.now() − t\| ≤ 300 s; `t` comes from the header, never from `created` | REQUIREMENT (PayCo guide) | V4 |
| Latency (`handle`) | p99 ≤ 1 s working target. The hard ceiling is PayCo's delivery timeout. | ASSUMPTION (a delivery costs ~3–5 store round trips). The ceiling is UNKNOWN: the integrating engineer gets PayCo's timeout from PayCo's guide or support, needed by Phase 2. | V0 baseline, Phase 2 load test |
| Throughput | A few thousand events a day on average. Peak burst rate not known. | Average: BASELINE (brief; confirm from PayCo export counts in Phase 0). Peak: UNKNOWN, derived from export timestamps in Phase 0. | Phase 2 load test at 10× peak |
| RPO | 0 for any event we answered 2xx | REQUIREMENT (follows from exact reconciliation plus "any 2xx = delivered"). It depends on the managed database's commit durability, which is UNKNOWN: the platform owner supplies it in Phase 0. | V1, Phase 2 restore rehearsal |
| RTO | ≤ 24 h. The hard limit is 72 h, after which PayCo stops retrying and events are lost. | ASSUMPTION (24 h leaves 48 h margin inside PayCo's 72 h REQUIREMENT window) | Phase 2 outage drill and restore rehearsal |
| Clock skew | ≤ 1 s, kept by NTP | ASSUMPTION (far inside the 300 s window) | Alert from Phase 0 |
| Operational complexity | One stateless service, the existing managed database, one daily reconciliation job. No new queue or datastore. | ASSUMPTION | Phase 2 review |

**Missing inputs** (none of these change the phase order; each assumption is stated with what changes if it's false):
1. **ledgerkit / `MemoryStore` API.** Does it offer atomic put-if-absent or multi-key transactions? Can it scan keys by prefix? What does `StoreError` mean: did the write not happen, or might it have landed anyway? *Assumption:* put-if-absent and prefix scan exist, and a write that raised may or may not have landed. *If false:* see Tradeoff rows 2 and 3, settled at V1.
2. **Does PayCo send a fresh `t` on each retry?** If retries reuse the first attempt's `t`, the 300 s rule rejects every retry after 5 minutes. Every transient 503 would then become a permanently lost event. *Assumption:* `t` is fresh on each attempt. This is checked in Phase 0 by forcing a 503. *If false:* escalate to PayCo before Phase 2, because the guide's two rules conflict.
3. **What Finance's reconciliation compares**, and whether disputes or payouts change PayCo's statement for an account. Resolved at V2.
4. **Existing balances and the spreadsheet's manual fixes.** If any fixes have no matching PayCo movement, Ledger needs an adjustment entry type, which belongs in the Phase 1 data model.
5. **Who reads balances today** (support, Finance, the product's spend path), and where spending is deducted. The brief's interface has no spend entries.
6. **PayCo operational facts:** whether a test mode exists, the delivery timeout, whether PayCo disables endpoints that keep failing, and whether a merchant can register several endpoints.
7. **Named owners:** the HTTP/deploy/monitoring layer (outside this codebase), the Finance lead, the security reviewer.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 skeleton | PayCo test-mode account and test webhook secret in a secret manager; database credentials; an owner for the HTTP and deploy layer | organizational | built / deployed |
| Phase 1 data model and write order | V1 (store contract) | decision / validation | specified |
| Phase 1 rules: refund pairing, anomalies, entry order | V2 (Finance sign-off) | decision | specified |
| Tolerance logic and the retry-based resilience design | Missing input 2 (`t` on retries), observed in Phase 0 | validation | hardened |
| Sync design | PayCo delivery timeout (UNKNOWN) | validation | hardened (Phase 2) |
| Registering the live merchant webhook | V3, V4 | risk / security | exposed (live data) |
| Backfill design | PayCo export format check (Phase 0); support's inventory of manual fixes | organizational / decision | specified |
| Consumers reading balances from Ledger | V5 | validation | exposed (money) |
| Stopping the spreadsheet process | V6 | risk / security | committed |
| Cutover | Things that depend on the spreadsheet: support staff (CONFIRMED), Finance reconciliation (CONFIRMED), the nightly CSV script (CONFIRMED), the product's balance reads (ASSUMED) | structural (brownfield) | exposed |

No economic dependencies: at the stated volume, compute is trivial and raw bodies add about 1 MB a day.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R3: an event we answered 2xx but never committed is lost money | Answer 2xx **only after the commit write is durable**. Any `StoreError` or unexpected exception returns 503, so PayCo retries. Availability comes from PayCo's 72 h retries, not from accepting writes we can't commit. | PayCo retries every non-2xx for 72 h, and our RTO is far shorter | Basis: Finance's exact-match REQUIREMENT plus PayCo's 72 h guarantee. Plus the Phase 2 outage drill. | Never answer before committing. If outages near 72 h, add a durable front inbox (deferred). |
| Data model | R3: data model and identifiers, once real data exists | Records are append-only, one per event, keyed by PayCo `event_id`; this record is the **commit point**. Index keys (`acct/{account}/{event}`, `pay/{payment_id}/{event}`) are written **before** it. Reads join the indexes to the records and check they agree. `balance` and `entries` are **derived on read**; no balance counter is stored. | Accounts have few events, so deriving on read fits the latency budget | V1, V3 | If V1 finds multi-key transactions, use one transaction instead. If derived reads go over budget, add a snapshot with a high-water mark. |
| Atomicity and concurrency primitive | R3: decides whether duplicates or races can double-apply | Put-if-absent on the commit record, prefix scan for the indexes. Concurrent `handle()` calls across processes are allowed. | The ledgerkit store and the managed database have these primitives with the same semantics | V1 | If V1 fails, run a single writer (one worker, serialized deliveries) or use transactions. |
| Refund arriving before its payment | R2: Finance sees the effect, but because balances are derived, changing the rule needs no data migration | A refund counts only when its payment is present with the same account, currency and amount, and it is the first refund for that payment. Until then it is stored as "pending" and left out of `balance` and `entries`. The account's currency is the currency of its earliest-`created` payment, so the result doesn't depend on arrival order. | Finance wants balances never to reflect a refund for a payment we haven't seen. If the payment never arrives, PayCo's net and ours are both 0. | V2 | V2 fails: Finance wants refunds applied on arrival, so change the derivation rule. |
| Signed events we can't process (unknown type, malformed, currency or amount mismatch, second credit for a `payment_id`) | R3: a non-2xx only postpones the loss, because PayCo drops the event after 72 h | Store the event durably, answer 200 with `ignored` or `quarantined`, and leave it out of the balance. Anomalies raise an alert. Stored events can be replayed when new handlers ship. | Retrying an identical body never changes the outcome | Basis: the PayCo guide (redeliveries carry the same body). Anomaly rules signed off at V2. | PayCo offers an event re-fetch API and Finance accepts relying on it. |
| `entries()` order | R2: it's a contract with consumers | Sort by (`created`, `event_id`), the business-time order that reconciliation uses | Consumers want business time, not arrival order | V2 | A consumer needs arrival order: add `applied_at` and a second accessor. |
| Sync vs async | R2: switching later is a code change, since raw events are already stored | Apply synchronously inside `handle()` | p99 stays well under PayCo's timeout | Load test at 10× peak in Phase 2 | p99 above 50% of PayCo's timeout, or volume growth, moves the work to an inbox and worker. |
| Historical backfill | R2: rerunnable, but it shapes identifiers | Import past payments and refunds one movement at a time from PayCo's export, using IDs like `import:{payment_id}`. The rule "one credit per `payment_id`" removes overlap with webhooks. | The export has `payment_id`, account, amount, currency and created time for every movement | Inspect one month's export in Phase 0 | If the export lacks `payment_id`: one opening-balance entry per account at a cutover timestamp, and refunds of older payments matched by hand. |
| Coexistence, cutover and system of record | R3: decommissioning the spreadsheet is the point of no return | Ledger runs in shadow on live webhooks while the spreadsheet stays the system of record (data flows PayCo → both). Then consumers move over cohort by cohort. The spreadsheet runs in parallel for one more statement cycle. | Shadow diffs converge to 0 | V5, V6 | V5 shows a diff nobody can explain: stay in shadow. |
| Data-privacy boundary | R2: retention and encryption can change later; whatever has leaked can't be recalled | Store verified raw bodies, which incident comparison and replay need. Logs carry IDs, type, amount and outcome, never bodies, the secret or the signature. | PayCo bodies carry no card or personal data beyond opaque IDs, so no PCI scope | Phase 0 review of PayCo's event schema | If bodies carry card or personal data: store only a hash plus the extracted fields, encrypt at rest, and set a retention limit. |

**R1 defaults:** build vs buy → build (standard library only is a REQUIREMENT); monolith vs services → one module behind one HTTP endpoint; detecting a duplicate with a different body → compare SHA-256 of the raw bytes (the raw body is stored, so switching to canonical JSON later is cheap); webhook secret → one secret, rotation deferred.
**N/A:** tenancy (one merchant account); AI rows (no AI component).

## 4. Walking skeleton (Phase 0)
- **Request:** PayCo sends a test-mode `payment.succeeded` delivery. The response is `200 {"status":"applied"}`. An operator then reads `balance(acct)` and sees the amount.
- **Tiers:** PayCo → HTTPS endpoint `POST /webhooks/payco` (the HTTP layer from the other codebase) → `LedgerService.handle` → managed database (a test namespace) → internal read of `balance` and `entries`.
- **Skeleton scope:** Find headers regardless of letter case. Verify HMAC over `f"{t}.".encode() + raw_body` with `hmac.compare_digest`, and check the 300 s window against `clock.now()`. Parse the JSON. Append a record naively; the correct design arrives in Phase 1. Other event types get 200 `ignored`. `StoreError` gets 503.
- **Deploy, log, monitor, roll back:** The artifact is built by the pipeline with a pinned Python version. Each delivery writes one structured log line, using `event_id` as the trace ID through the HTTP log, `handle` and the store write. Metrics count outcomes by status. Alerts fire on any 5xx and on a spike of signature failures. Deploy version N+1, then roll back to N once.
- **Lead-time work started now:** org items (§2), V1 (reading the store docs and the spike), V2 (Finance session), one month of PayCo export (format check and peak rate), PayCo schema review (privacy row).
- **Who can reach it:** The URL has to be internet-reachable for PayCo. Only test-mode deliveries signed with the test secret get past verification. The live secret is not deployed, and nobody reads these balances.

**Exit check (V0):** see §6. It also records two observations: whether `t` changes on retries (force a 503, then inspect the retry), and PayCo's retry interval.

## 5. Phases

- **Phase 1: Core ledger correctness**
  - **Unlocks:** live shadow traffic.
  - **Depends on:** V0, V1, V2.
  - **Tasks** (widest rework first):
    1. **Key schema and write order** (from V1). `evt/{event_id}` holds `{sha256, raw, type, created, account_id, payment_id, amount, currency, received_at, status}`. The index keys are written first and the commit record last, with put-if-absent. Reads ignore index keys that don't match their record.
    2. **Response contract:**

       | Status | When |
       |---|---|
       | 200 `applied` / `duplicate` / `pending` / `ignored` / `quarantined` | Normal outcomes |
       | 401 | Signature header missing or garbled, bad signature, or stale `t`. Not recorded; only body hash and length are logged. |
       | 409 | Same ID with a different body. No writes; page security. |
       | 503 | `StoreError` |
       | 500 | Any other exception. `handle()` never raises to the HTTP layer. |

    3. **Pipeline order:** header → signature over the raw bytes → freshness → parse → schema check → look up the ID (duplicate or incident) → write indexes → commit. The schema check covers:
       - `amount` is an `int`, not a `bool` or `float`, and is > 0
       - `currency` matches `^[A-Z]{3}$`
       - IDs are non-empty strings
       - `created` is an int

       A duplicate with the same body returns 200 without writing.
    4. **Derivation rules** (V2): the account's currency, one credit per `payment_id`, refund pairing, `entries` sorted by (`created`, `event_id`), and functions that list anomalies and pending refunds.
    5. **V3 test harness and V4 security vectors in CI;** the security review happens here.
  - **Rollback:** Code only, with no live data yet. Revert in the pipeline and wipe the test namespace.
  - **Exit check:** V3 and V4 pass.

- **Phase 2: Live shadow, backfill, reconciliation**
  - **Unlocks:** cutover.
  - **Depends on:** Phase 1, V3, V4.
  - **Tasks:**
    1. Load test at 10× peak on staging and record p99 (sync row).
    2. Create a fresh production namespace, deploy the live secret, and register the live endpoint. The spreadsheet stays the system of record.
    3. Backfill up to registration time plus an overlap window. The import is idempotent and rerunnable.
    4. Run a daily diff against the PayCo CSV export the nightly script already pulls.
    5. Sweep for:
       - refunds still pending after 72 h (the payment will never arrive by webhook)
       - accounts with more than one currency
       - counts of anomalies and of unknown event types, by type
    6. Outage drill: return 503 by flag for one hour, then confirm full recovery with 0 diff.
    7. Restore-rehearsal of the database, measuring RTO.
  - **Rollback:** Unregister the webhook or turn the flag off. The spreadsheet is untouched, and the store can be rebuilt from PayCo's export through the backfill path.
  - **Exit check:** V5 passes; p99 and RTO are within §1.

- **Phase 3: Cutover**
  - **Unlocks:** retiring the manual process.
  - **Depends on:** V5.
  - **Tasks:**
    1. Confirm the list of consumers (missing input 5).
    2. Switch reads behind a flag in widening cohorts: internal accounts → 10% → 100%. The nightly spreadsheet keeps running alongside.
    3. Finance runs one monthly reconciliation from Ledger.
    4. V6.
    5. Stop the nightly script and manual edits, and archive the spreadsheet read-only.
  - **Rollback:** Before V6, flip the flag back to the spreadsheet. Stopping the spreadsheet is the point of no return, guarded by V6. After it, the remaining recovery path is the archived spreadsheet plus backfill from PayCo.
  - **Exit check:** V6 passes; each cohort shows 0 diff before the next widens.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | Send a PayCo test delivery end to end; deploy N+1 and roll back to N; inject a `StoreError`; take baselines | The §4 exit check: one trace across all tiers, deploy and rollback succeed, the injected failure alerts, and baselines are recorded (p50/p99, store round trip, observed `StoreError` rate, `t` on retry) | Pipeline run log, a trace screenshot, the alert record, `baselines.md` in the repo | Phase 1 | 0 |
| V1 | The store and the managed database provide atomic put-if-absent (or transactions) and prefix scan, and their `StoreError` semantics are known and match `MemoryStore` | Read the ledgerkit docs. Spike: 1,000 concurrent put-if-absent races on one key, plus `StoreError` injection with read-back, run against `MemoryStore` and the staging database. | 1,000/1,000 races have exactly one winner; prefix scan returns every key written; the tech lead signs off on the semantics note | `docs/store-contract.md` and the spike output stored as a CI artifact | Detailed design of the Phase 1 data model. If it fails: single-writer or transactions (rows 2 and 3). | Starts in 0, required before 1 |
| V2 | Finance's reconciliation is per account, Σ payments − Σ refunds by `created` month, and Finance accepts the pending-refund, anomaly and entry-order rules, plus disputes being out of scope | Written rule spec walked through against a sample PayCo statement | The Finance lead signs a pass | The signed spec in `docs/` | Phase 1 rules. If it fails: change the derivation rules, or pull dispute handling into Phase 1. | Starts in 0, required before 1 |
| V3 | No mix of duplicates, reordering, `StoreError`s or concurrency can lose or double-apply an event (this guards the worst failure) | A golden model, a pure function over the set of distinct events, is compared with the service. Random event sets include payments, refunds, unknown types, anomalies and same-ID-different-body cases, delivered shuffled and duplicated. A fault-injecting wrapper raises `StoreError` before *and* after the k-th write. Every non-2xx is retried. Every write index is tested exhaustively with a single fault. Concurrent threads also run. | 0 divergences across all single-fault cases and 10,000 seeded random schedules; never a 2xx without the committed record; a 409 changes nothing (REQUIREMENT, brief) | CI test report; failing seeds archived in the repo | Phase 2. If it fails: fix and rerun. If the cause is the store's semantics, revisit the V1 flip. | 1 |
| V4 | Only correctly signed, fresh deliveries change state, and same-ID-different-body pages and changes nothing | Test vectors: PayCo's documented sample signature, one changed body byte, wrong secret, `t` at ±300 and ±301, missing or garbled header, mixed-case header names, several `v1` values, a replay inside 300 s (expect `duplicate`). Then a security review. | Every vector passes (300 s is a REQUIREMENT from the PayCo guide); the named security reviewer passes it | Test report and review record | Registering the live endpoint (Phase 2). If it fails: no live registration. | 1 |
| V5 | Shadow balances equal PayCo's for every account | Daily diff against PayCo's CSV export and the spreadsheet; one full monthly statement; the outage drill | 0 minor-unit diff for every account (REQUIREMENT, Finance) over ≥ 30 consecutive days and one full statement (ASSUMPTION: one statement cycle); every diff in that period root-caused; the drill recovers with 0 diff | Archived daily diff reports; the monthly reconciliation signed by Finance | Phase 3. If it fails: stay in shadow, fix, restart the window. | 2 |
| V6 | Stopping the spreadsheet process is safe (point of no return) | Rehearse the cutover runbook on a staging copy; switch the flag back to the spreadsheet once; check each cohort | V5 passed; ≥ 7 days at 0 diff per cohort; rollback rehearsed; the Finance lead and support lead approve | Signed go/no-go record and rehearsal log | Decommissioning. If it fails: keep running in parallel. | 3 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | HMAC over the raw bytes with constant-time compare; 300 s window; test secret held in a secret manager; logs never contain the secret, signature or body | One structured line per delivery (`event_id`, type, outcome, status, latency, store error); alerts on 5xx, signature-failure spikes and clock skew | Pinned Python; CI check that only the standard library is imported; versioned artifact; recorded PayCo test deliveries kept as fixtures | `StoreError` → 503, never 2xx; one rollback done; PayCo retry behaviour observed |
| 1 | Incident path (409 plus page, deduplicated by event ID); strict schema checks; review at V4 | Per-outcome metrics; counters for anomalies and pending refunds; a log line whenever a read finds an index that doesn't match its record | Seeded fault and permutation harness in CI; golden model kept in the repo | Commit record written last; index writes idempotent; concurrency tests; every exception mapped to a status |
| 2 | Live secret, with a rotation runbook triggered by any incident; least-privilege store credentials; retention rule for raw bodies | Daily reconciliation diff alert; alert on refunds pending > 72 h; alerts on multiple currencies and unknown types; dashboard of PayCo delivery failures | Backfill and reconciliation scripts are idempotent and rerunnable; their outputs archived | Outage drill; database restore rehearsal that measures RTO; the store can be rebuilt from PayCo's export |
| 3 | Read access to balances scoped per consumer; the old spreadsheet archived read-only | Per-cohort dashboard comparing Ledger with the spreadsheet | Cutover runbook rehearsed (V6) | Flag back to the spreadsheet until V6; the parallel run continues for one more statement cycle |

## 8. AI layer
N/A: no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Disputes, payouts and other event types.** They are already stored, so they can be replayed. Pull forward when V2 says they change account balances, or when the unknown-type counter shows them arriving.
- **Partial refunds.** Pull forward on the first refund whose amount doesn't match its payment (anomaly alert), or when the product turns partial refunds on.
- **Two secrets active at once, for rotation.** Pull forward before the first planned rotation or on any incident from §3.
- **Async inbox and worker.** Pull forward when p99 exceeds 50% of PayCo's timeout, or when volume growth makes the Phase 2 load test fail.
- **Balance snapshot with a high-water mark.** Pull forward when derived reads exceed the latency budget.
- **Manual adjustment entry type.** Pull forward if support's inventory (missing input 4) finds fixes with no matching PayCo movement.
- **Spend entries, so Ledger becomes the spendable balance.** Pull forward when the product decides to read its spendable balance from Ledger (missing input 5).
- **High availability across zones or regions.** Pull forward if the Phase 2 drill or restore rehearsal takes longer than the 24 h RTO.
- **Raw-body archival or expiry.** Pull forward if the privacy review or storage growth needs it.
