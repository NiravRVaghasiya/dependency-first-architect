# BUILD PLAN — Ledger (PayCo webhook → customer balances)

> **Note on inputs:** The brief says to see `README.md` for the ledgerkit `MemoryStore`. The `README.md` in this workspace only says the workspace is empty, so the store's API is unknown. It is the first missing input below.

## 1. Classification and constraints
- **What:** A Python service (standard library only) that turns signed PayCo webhooks into exact per-account balances and money-movement entries.
- **Type:** Software. Brownfield: it replaces a nightly job that copies PayCo's CSV export into a spreadsheet, plus manual fixes by support. Not a small build, because it handles real money.
- **Dominant constraint:** Correctness. Balances must match PayCo's statement exactly, to the minor unit, even with duplicates, out-of-order delivery and partial write failures.
- **Worst failure:** PayCo gets a 2xx for an event that was never applied, or was applied twice. The classic case: a `StoreError` hits after the "seen" marker is written but before the money moves. PayCo never resends it, and the balance stays wrong until month-end reconciliation. This is guarded by V1.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Reconciliation difference | 0 minor units per account per statement month | REQUIREMENT (Finance, in the brief) | V3 |
| Signature timestamp tolerance | \|now − t\| ≤ 300 s | REQUIREMENT (PayCo integration guide) | V2 |
| Maximum intake outage before events are lost | < 72 h | REQUIREMENT (PayCo retry window) | V0, Phase 2 drill |
| RTO for intake | ≤ 4 h. Basis: leaves 68 h of PayCo retry margin | ASSUMPTION | Phase 2 drill |
| RPO for acknowledged events | 0. Basis: after a 2xx, PayCo never resends | ASSUMPTION (confirm the managed DB's durability and point-in-time-restore settings) | Phase 2 restore drill |
| Handle latency | p99 under PayCo's response timeout | UNKNOWN: PayCo supplies the timeout (V4); needed in Phase 1 | V4, Phase 1 burst check |
| Throughput (steady) | A few thousand events a day | REQUIREMENT (brief, current volume) | V0 baseline |
| Throughput (backlog after an outage) | Drain about 15k queued retries within 1 h. Basis: about 3 days × about 5k/day | ASSUMPTION | Phase 1 burst check |
| Growth | — | UNKNOWN: product/finance supply a forecast; needed before deferred scaling work is pulled forward | §10 |
| Operational complexity | 1 service instance, 1 managed DB, 1 nightly reconciliation job | ASSUMPTION | Phase 2 |

**Missing inputs** (most plan-changing first):
1. **ledgerkit store API.**
   - Does it support atomic put-if-absent? Multi-key transactions? Listing an account's records?
   - Can a write that raises `StoreError` still have been applied?
   - Does the production DB client share `MemoryStore`'s interface?
   - The answers change the Phase 1 write pattern, not the phase order.
2. **PayCo delivery details** (to be settled by V4):
   - Is each retry re-signed with a fresh `t`? If not, the 300 s rule rejects every retry after 5 minutes.
   - Are redelivered bodies byte-identical?
   - What is the response timeout?
   - Does PayCo auto-disable endpoints that keep failing?
   - Is there a test mode? Published source IPs? An API to re-fetch events?
3. **Which event types move money on PayCo's statement** (for example disputes). Ledger will not handle these yet, so V3 cannot reach 0 difference without counting them. Finance supplies this.
4. **Who reads `balance()`, and how spending is deducted.** The interface covers only PayCo money movements. Product supplies this; Phase 3 needs it.
5. **History:**
   - Does the PayCo export include event or payment ids (for backfill)?
   - What do support's manual fixes consist of?
   - Support and Finance supply this; Phase 2 needs it.
6. **Named approvers:** a security reviewer (V2) and a finance lead (V3, V5).

**Assumption the phase order rests on:** Ledger can run in shadow (receiving live events, read by no one) while the spreadsheet stays the system of record. This holds because nothing reads Ledger today.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 deploy | PayCo dashboard access, a test-mode webhook secret in the secret manager, the managed DB instance (ASSUMED available) | organizational | deployed |
| Phase 1 write pattern | ledgerkit store API docs (missing input 1) | decision / organizational | specified |
| Status mapping, dedup hash, how retries are tolerated | V4 (PayCo delivery contract) | validation | hardened |
| Live merchant webhook registration (Phase 2) | V1, V2, V4; a separate live secret | risk-security | exposed to live events |
| V3 threshold | Finance's list of statement-affecting event types (missing input 3) | organizational | specified |
| Backfill / opening balances | Export format (missing input 5, ASSUMED to contain ids) | decision | specified |
| Consumers reading `balance()` (Phase 3) | V3, plus consumers identified (missing input 4) | validation / organizational | exposed |
| Decommissioning the spreadsheet (Phase 4) | V5 and the finance approver | risk / organizational | committed |
| Running more than 1 instance | A conditional write in the store | decision | scaled (deferred) |

No economic dependencies: standard library only, tiny volume, trivial storage.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R3: a 2xx before the data is durably written loses an event for good | Consistent. Return 2xx only after the commit write succeeds; any `StoreError` returns 503 and PayCo retries | PayCo retries non-2xx for 72 h | Basis: PayCo guide (REQUIREMENT); V1 proves no 2xx without a commit | V4 shows PayCo auto-disables endpoints on 5xx sooner than our RTO, so add redundant intake |
| Ledger data model | R3: money data model once real data exists | Append-only event log keyed by `event_id` (raw body, SHA-256, normalized fields, outcome). Balance and entries are **computed from the log** with no stored counter. Any index records are written first and idempotently; **the event record is written last and is the single commit point** | The store offers an atomic single-key put-if-absent (UNKNOWN until missing input 1) | V1 | V1 fails, or the store has no put-if-absent: serialize all writes through one process lock and read back after each write. If the store has transactions, use them and keep balances computed |
| Refund arrives before its payment | R3: customers see wrong balances; Finance needs exactness | **Park** the refund: store it, return 200, apply it only once a payment with the same `payment_id`, account, currency and amount is committed. A mismatch leaves it unapplied and flagged | The payment arrives within PayCo's 72 h | V1 (reordering); V3 counts parked refunds older than 72 h | Finance wants the debit taken at refund time: apply immediately instead. Because balances are computed, this means recomputing, not migrating data |
| Outcome → HTTP status contract with PayCo | R3: a wrong 2xx silently drops an event | 200 for new / duplicate / unhandled type. 401 for bad or missing signature, or stale `t`. 400 for a signed body that is malformed. **409 for same `id` with a different body** (incident: page on-call, no account writes, original record kept). 422 for business-rule anomalies (currency mismatch, second payment or refund for one `payment_id`, amount ≤ 0 or not an integer), stored durably in quarantine. 503 for `StoreError` | PayCo does not auto-disable endpoints on a stream of 4xx | Basis: PayCo guide + the brief's security rule; V2, V4 | V4 shows auto-disable on 4xx: send 422 cases as 200 with quarantine. Keep 409 and escalate to PayCo |
| Event types we don't handle yet | R3: once acknowledged, an event that wasn't stored is gone unless PayCo can resend it | Verify the signature, store the raw event as `unhandled`, return 200, no balance effect, list it in reconciliation | Raw bodies are safe to store (no card data) | V2 data review | PayCo offers an event re-fetch API (storing becomes optional). Finance names a type that moves money: pull its handler forward (§10) |
| Dedup identity check | R2: re-hash the stored raw bodies | SHA-256 of the exact raw body bytes | Redeliveries are byte-identical (the brief says "same body") | V4 | A genuine redelivery differs only in formatting: hash a canonical JSON form instead |
| Sync vs async | R2: adding a queue means a second durable store | Process synchronously inside the request | One commit write per event; low volume | Phase 1 burst check | p99 handle time above half of PayCo's timeout |
| Concurrency control | R2: moving to conditional writes is a code change | Single instance with a process-wide lock around `handle` | Volume per §1; RTO of 4 h tolerates a single instance | Phase 1 burst check | Need more than 1 instance or stricter high availability: use the store's conditional writes (needs missing input 1) |
| Coexistence / cutover / system of record | R3: Finance and customers depend on balances | Parallel run. The spreadsheet stays the system of record until Phase 3. Data flows one way, PayCo → Ledger. Consumers switch by flag. The point of no return is decommissioning the spreadsheet | Shadow running is harmless (§1) | V3, V5 | V3 fails twice in a row: stop and re-plan the data model |
| Opening balances | R3: the first entries of every account | Backfill historical events from PayCo's export as entries keyed by PayCo's ids, deduplicated against live events | The export includes payment and refund ids (missing input 5) | V3 | The export has no ids: one Finance-signed `opening_balance` entry per account at a cutoff time |
| Data-privacy boundary | R2: re-store only the normalized fields | Store account_id, payment_id, amount, currency and the raw body. Logs carry ids, type, amount and outcome, never bodies, secrets or signatures | Webhook bodies hold no card numbers or personal data | V2 | Bodies contain personal data: store normalized fields plus the hash only |

**R1 defaults:**
- Monolith vs services → one module behind the existing HTTP layer.
- Build vs buy → build (the brief requires standard library only).
- `entries()` order → commit order, with `created` recorded.
- Secret rotation → accept the delivery if any `v1` in the header verifies.

**N/A:** tenancy (one merchant account); AI rows (no AI component).

## 4. Walking skeleton (Phase 0)
- **Request:** a PayCo **test-mode** `payment.succeeded` for `acct_test`. The HTTP layer returns 200, and `balance("acct_test")` returns the amount through an internal ops CLI.
- **Path:** PayCo → TLS ingress → HTTP layer (passes the **raw body bytes** unchanged; enforces a body-size limit) → `LedgerService.handle`:
  - case-insensitive header lookup;
  - parse `t`/`v1`;
  - 300 s check;
  - HMAC with `hmac.compare_digest`;
  - JSON parse;
  - one put-if-absent of the event record.
  
  Then → managed DB → the read path for `balance` and `entries`.
- **Deploy:** a CI pipeline (unit tests + signed test fixtures) builds a versioned artifact on a pinned Python version.
- **Logging:** structured JSON (`event_id`, type, outcome, status, latency). No raw bodies.
- **Metrics:** counters per outcome.
- **Alerts:** on any 5xx, any signature rejection, and a dead-man alert when no events arrive for N hours (N comes from the V0 baseline).
- **Rollback:** redeploy the previous artifact. PayCo's retries cover the gap.
- **Who can reach it:** the test-mode webhook only, plus an IP allow-list if PayCo publishes its ranges. The live merchant webhook is **not registered** (flag `LIVE_INTAKE=off`), and nothing reads the balances.
- **Exit check V0:**
  - the test event succeeds with one trace (by `event_id`) from ingress to DB to read;
  - a deploy and a rollback both go through the pipeline;
  - an injected `StoreError` (fault flag) returns 503, fires an alert, and PayCo's retry succeeds once the flag is cleared;
  - handle latency and the DB write time are recorded as baselines at near-zero load.

## 5. Phases

- **Phase 1 — Correct core** (contracts first, then logic)
  - Unlocks: live intake (Phase 2).
  - Depends on: V0; missing input 1; V4 (starts in Phase 0).
  - Tasks, in order:
    1. Pin the event-record schema and the status contract (§3).
    2. Build the commit-last write path and dedup: an identical body returns the original outcome; a different body returns 409, pages on-call, writes an incident record, leaves the account untouched.
    3. Signed-input checks: reject duplicate JSON keys (`object_pairs_hook`); `amount` must be an `int` and not a `bool`, and > 0; currency must be 3 uppercase letters; reject ambiguous duplicate signature headers.
    4. Computed `balance` and `entries`: payments, matched refunds, parked refunds; currency fixed by the first committed payment; 0 for unknown accounts.
    5. Quarantine records for anomalies.
    6. A FaultyStore test harness plus a property-test generator → V1.
    7. A security test suite → V2.
    8. Burst check: replay 15k signed events.
  - Rollback: redeploy the previous artifact. Test-mode data is disposable.
  - Exit check: V1 and V2 pass, and the burst check meets §1.

- **Phase 2 — Shadow on the live merchant account**
  - Unlocks: evidence for cutover.
  - Depends on: V1, V2, V4.
  - System of record: the spreadsheet. Data flows PayCo → Ledger only.
  - Tasks, in order:
    1. Put the live secret in the secret manager.
    2. Register the live webhook (`LIVE_INTAKE=on`).
    3. Backfill history (per §3).
    4. Nightly reconciliation job: Ledger vs PayCo export, and Ledger vs spreadsheet. The report lists unhandled types, quarantine, and refunds parked longer than 72 h.
    5. Rebuild balances from the event log and compare them to the live values.
    6. Outage drill: 1 h down, then confirm PayCo's retries drain.
    7. Restore drill from point-in-time backup.
    8. Run for at least 1 statement month → V3.
  - Rollback: unregister the webhook; the spreadsheet keeps running.
  - Exit check: V3 passes, both drills meet the §1 RTO/RPO, and nothing is parked for more than 72 h without being explained.

- **Phase 3 — Cutover of readers, smallest exposure first**
  - Unlocks: Ledger becomes the system of record for balances.
  - Depends on: V3; missing input 4.
  - Tasks, in order:
    1. Support staff get read-only access to Ledger alongside the spreadsheet.
    2. Product reads `balance()` for an internal cohort.
    3. Then all accounts, behind a flag.
    4. The spreadsheet keeps being generated as the fallback.
  - Rollback: flip the readers back to the spreadsheet.
  - Exit check: a daily Ledger-vs-spreadsheet comparison shows no unexplained differences for the cohort before each widening.

- **Phase 4 — Decommission the nightly spreadsheet**
  - Depends on: V5.
  - Tasks:
    1. Archive the final spreadsheet with its SHA-256.
    2. Stop the script and revoke its PayCo export credential.
    3. Keep monthly reconciliation as a permanent control.
  - Point of no return: stopping the script and manual fixes. Guarded by V5.
  - Exit check: the first statement month after decommissioning reconciles to 0 difference.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | Ledger can be deployed, observed and rolled back through every tier | §4 | The V0 exit check in §4 | Pipeline run logs, trace export, alert record, baseline sheet in the ops repo | Phase 1; if it fails: fix the pipeline or alerting before any logic | 0 |
| V1 | No sequence of duplicates, reorders and `StoreError`s produces a wrong or doubly-applied balance, or a 2xx without a commit | FaultyStore wraps the store and fails the k-th write (before and after the write applies) for every k on every code path. Seeded generator of sequences with duplicates, reorders and up to 3 faults. A harness retries non-2xx like PayCo. Results are compared to an oracle | 0 mismatches in balance or entries, and 0 2xx without a commit (REQUIREMENT, Finance exactness). Covers exhaustive single faults and at least 10k random sequences | CI test report plus the list of seeds, as a CI artifact | Phase 2; if it fails: apply the data-model flip in §3 | 1 |
| V2 | Only authentic, fresh, consistent deliveries can change an account | Test suite (bad or missing signature, `t` ±301 s and ±299 s, header case variants, multiple `v1`, malformed headers, duplicate JSON keys, bool/float amounts, same id with a different body). Code review of the compare and the parse order. Review of what is logged and stored | All tests pass, and the named security reviewer signs off (UNKNOWN: the security owner names the reviewer before Phase 1 ends) | Test report plus signed review note in the repo `/reviews` | Live registration (Phase 2); if it fails: fix and re-review | 1 |
| V3 | Ledger matches PayCo's statement exactly | Parallel run for at least 1 full statement month. Per-account diff against the statement. Diff against the spreadsheet, attributed to documented manual fixes | 0 minor units per account (REQUIREMENT). Every unhandled-type event and parked refund listed. Finance-lead sign-off (UNKNOWN name) | Monthly reconciliation report plus input file hashes in the finance archive | Phase 3; if it fails: find the root cause, fix, and run a fresh month | Starts in 2, required before 3 |
| V4 | PayCo's delivery behaviour fits our status contract and the 300 s rule | Written confirmation from PayCo, plus a test-mode probe: return 503 for 15 min, then 200, logging each attempt's `t` and body SHA-256; return 4xx for 24 h and watch for auto-disable | Each retry's `t` within 300 s of when it arrives (REQUIREMENT, PayCo guide); bodies byte-identical; no auto-disable within the 4 h RTO (ASSUMPTION, §1); response timeout recorded | Probe log plus PayCo's email, in `/vendor/payco` | Hardening the status map and hash; Phase 2. If it fails: if retries keep the old `t`, escalate to PayCo and do not go live (it conflicts with the security requirement); if bodies differ, use the canonical hash; if PayCo auto-disables, 422 becomes 200 with quarantine | Starts in 0, required before 2 |
| V5 | Spreadsheet can be retired safely | Rehearsal: one month in which support uses only Ledger while the spreadsheet is still generated. Rollback test: re-enable the script | 2 consecutive statement months at 0 difference (ASSUMPTION: catches month-boundary cases). Finance-lead go/no-go | Two reconciliation reports plus a signed go/no-go record | Phase 4; if it fails: stay in Phase 3 | 3 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Test secret in the secret manager; signature + 300 s check; TLS; test mode only | Structured logs by `event_id`; outcome counters; 5xx, signature-failure and dead-man alerts | Pinned Python; standard library only; CI artifact; signed fixtures | 503 on `StoreError`; PayCo retries; rollback rehearsed |
| 1 | Constant-time compare; strict parsing; 409 pages on-call; body-size limit | Metrics for incidents, quarantine and parked refunds | Seeded property tests; FaultyStore in CI | Commit-last idempotent writes (V1); burst check |
| 2 | Separate live secret; access to DB and reports limited to ledger and finance staff; logs classified as internal-financial | Nightly reconciliation report; alert on refunds parked > 72 h | Reports archived with input hashes; balances rebuilt from the log | Outage drill and point-in-time restore drill |
| 3 | Authorization on read paths for consumers | Daily Ledger-vs-spreadsheet comparison per cohort | Flag configuration under version control | Flag-based fallback to the spreadsheet |
| 4 | Revoke the script's export credential | Monthly reconciliation becomes permanent | Final spreadsheet archived with its hash | Runbook: restore from backup plus PayCo export |

## 8. AI layer
N/A: there is no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Handlers for disputes, payouts and other types:** add when Finance names a type that moves money on the statement, or V3 shows an unhandled-type difference. The stored raw events can be replayed then.
- **Stored or snapshot balances:** add when computing `balance()` makes p99 exceed half of PayCo's timeout.
- **Multiple instances / high availability:** add when an availability need exceeds the 4 h RTO, or growth exceeds what one instance handles (needs the store's conditional writes).
- **Async intake queue:** add when sync handling exceeds half of PayCo's timeout.
- **Partial refunds:** add if PayCo starts sending refunds smaller than the payment.
- **Dual-secret rotation tooling:** add after any 409 incident (assume the secret may be compromised) or when a rotation policy is set.
- **Re-fetching missed events from PayCo:** add if V4 shows PayCo has an events API, or reconciliation finds a gap.
- **Spending/debits inside Ledger:** add if Product decides Ledger should track spending as well as PayCo top-ups (missing input 4).
