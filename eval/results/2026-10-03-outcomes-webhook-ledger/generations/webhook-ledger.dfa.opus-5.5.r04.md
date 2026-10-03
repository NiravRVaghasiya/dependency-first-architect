# BUILD PLAN — Ledger (PayCo webhooks → customer balances)

## 1. Classification and constraints
- **What:** A Python service (standard library only) that checks each PayCo webhook, applies each payment or refund to its account exactly once, and serves `balance()` and `entries()`.
- **Type:** Software. Brownfield: it replaces a nightly CSV → spreadsheet copy plus hand fixes by support. Not a small build, because it handles money.
- **Dominant constraint:** Correctness. Finance must match PayCo's monthly statement exactly, to the minor unit.
- **Worst failure:** Money applied twice or lost without anyone noticing, which breaks reconciliation. Causes:
  - a duplicate delivery, or a retry after a write that failed partway, gets applied twice;
  - an event is marked "processed" before its effect is saved, so the retry skips it;
  - a refund arrives before its payment and gets stuck;
  - a forged request credits an account.

  V4 guards it, backed by V3 and V5.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Reconciliation accuracy | Ledger matches the PayCo statement for every account, to the minor unit | REQUIREMENT (Finance, brief) | V6 |
| Exactly-once application | 0 events applied twice, 0 events lost | REQUIREMENT (follows from exact match) | V4, V5 |
| Signature time window | \|now − t\| ≤ 300 s | REQUIREMENT (PayCo integration guide) | V3 |
| Host clock skew | ≤ 5 s against NTP | ASSUMPTION (well inside the 300 s window) | Phase 2 alert |
| Latency (`handle` p99) | ≤ 2 s and below PayCo's delivery timeout | ASSUMPTION. PayCo's timeout is UNKNOWN: PayCo guide or support supplies it; Phase 0 needs it | V0 baseline, Phase 2 load check |
| Throughput | A few thousand events/day, growing. Design check at 50k/day with bursts of 20 req/s | REQUIREMENT (brief) for today; ASSUMPTION (10× headroom) for the check | Phase 2 load check |
| Event intake RPO | An outage shorter than 72 h loses no events | REQUIREMENT (PayCo 72 h retry window) | Phase 2 outage runbook |
| Outage detection | Alert within 15 min of sustained 5xx | ASSUMPTION (leaves almost all of the 72 h window to recover) | V0 |
| Stored-data RPO/RTO | UNKNOWN: the platform team supplies the managed-DB backup/PITR settings; needed before Phase 3, when Ledger becomes the system of record | — | Phase 2 restore drill |
| Parked-refund age | ≤ 72 h | ASSUMPTION (equals PayCo's retry window) | Phase 2 alert |
| Operational complexity | One stateless service plus the existing managed DB; no queue | ASSUMPTION | Sync vs async row |

**Missing inputs** (most plan-changing first):
1. **ledgerkit `MemoryStore` / managed-DB client API.** The brief points to README.md, but the workspace README has no ledgerkit docs. Unknowns:
   - whether the store has transactions or a conditional insert / compare-and-set;
   - whether reads are linearizable;
   - whether a `StoreError` can be raised *after* a write was saved.

   *Assumption:* per-key get/put plus put-if-absent, and a `StoreError` can mean "maybe saved". The data model is designed for that worst case, and V1 settles it.
2. **How the product reads balances, and whether spending debits this ledger.** *Assumption:* spending is tracked elsewhere, and Ledger holds the PayCo-funded balance (payments minus refunds), which is what the statement can match. If Ledger must also record spending, the interface changes; ask before Phase 1 is specified.
3. **PayCo facts:**
   - Is there a test mode with its own secret?
   - Are retries re-signed with a fresh `t`, and is the body byte-identical? (V2)
   - What is the delivery timeout?
   - Can a second live endpoint be registered (needed for shadow mode)?
   - Does the historical export carry event IDs? (needed for backfill)
4. **Finance's statement format and scope.** If it includes disputes, fees or payouts, the event types we ignore will break the exact match (see §10).
5. **Smaller choices, decided by default:**
   - `entries()` order is "oldest first" in the order entries were applied, not by `created`.
   - "First payment fixes the currency" means the first payment *applied*.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Data model and write sequence (Phase 1) | Store semantics (V1); ledgerkit docs (missing input 1) | decision / validation | specified |
| `StoreError` → 503 policy; byte-identity conflict check | PayCo redelivery contract (V2) | validation | specified |
| Live webhook registration (Phase 2) | V3 signatures, V4 exactly-once | risk / security | exposed (real money data) |
| Phase 0 deploy | PayCo test-mode account, test secret in secrets manager, endpoint URL (PayCo dashboard admin) | organizational (ASSUMED available) | deployed |
| Live shadow endpoint | A second live webhook endpoint at PayCo, plus the live secret | organizational (ASSUMED) | deployed |
| Backfill of history | PayCo historical export with payment and refund IDs (Finance / PayCo admin) | organizational | built |
| V6 | Statement format, plus a Finance reviewer named for sign-off | organizational | V6 cannot run without them |
| Product reads Ledger (Phase 3) | V5, V6; product integration point (missing input 2) | risk / security | exposed |
| Handlers for disputes and other types | Whether the statement includes them (missing input 4) | validation | Phase 3 cutover is blocked if they are needed |
| Retiring script and spreadsheet | V7, plus approval by the Finance lead | organizational / risk | committed |
| Current dependents of the spreadsheet (product? support tooling?) | Phase 0 inventory | structural (ASSUMED: product and support read it) | Phase 3 specified |

No economic dependency binds at this volume.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R3: a wrong 2xx loses money for good | Consistency first. Reply 2xx only after the effect is saved; any `StoreError` or unexpected exception → 503/500 so PayCo retries | PayCo re-signs retries with a fresh `t`; DB outages stay well under 72 h | V2, V4 | **V2 fails:** keep the 503 policy, but accept a stale `t` for an event whose HMAC verifies and whose body hash was already stored when it first arrived inside the window (replay is harmless because writes are idempotent). Or DB outages approach 72 h → add a durable inbox |
| Data model and idempotency keys | R3: identifiers and money records are hard to migrate once real data exists | Append-only entries, unique on `event_id`; event record = `id` → SHA-256 of raw body + status + raw body; payment index unique on `payment_id`; `balance` = sum of entries | The store has put-if-absent (or transactions) and linearizable reads | V1, V4 | **V1 fails:** with no conditional write, run one writer instance with a per-account lock. With transactions, put the whole write sequence in one transaction |
| How to detect "same event, different body" | R2: a wrong check makes legitimate redeliveries get 409 | Compare SHA-256 of the raw body bytes | Redeliveries are byte-identical (brief: "same body") | V2 | V2 shows byte differences with the same meaning → compare canonical JSON |
| Refund arriving before its payment | R2: the parked set would need migrating; no money lost either way | Accept with 2xx and park the refund keyed by `payment_id`; the payment handler applies it. Each side writes, then re-checks the other, so a race still resolves | The payment arrives within 72 h; refunds of pre-Ledger payments are covered by backfill | V4, plus the parked-age alert | Parked refunds older than 72 h keep happening, or Finance wants refunds shown on arrival → apply immediately and allow a negative balance |
| Signed but rule-breaking events (currency mismatch, refund ≠ payment amount or account, second refund for one payment, new event ID for a known `payment_id`) | R2: changes the status map PayCo sees | Quarantine record + alert + 200; nothing applied; released or dismissed by review in the ops tool | Retrying cannot fix them; people resolve them | Finance sign-off on the status map (Phase 1) | Finance wants these visible as failures in PayCo → 422 |
| Sync vs async | R2: adding a queue changes the write path | Apply synchronously inside the request | DB latency stays well below PayCo's timeout at this volume | V0 baseline, Phase 2 load check | p99 above 50% of PayCo's timeout, or volume above 50k/day → verify, store raw body, reply 2xx, apply asynchronously |
| Brownfield coexistence and system of record | R2: two systems run in parallel until V7 | Spreadsheet stays the system of record through Phase 2. Ledger runs in shadow, then takes over by cohort behind a flag. The script keeps running until V7 | The script can keep running in parallel | V6, V7 | Shadow differences cannot be explained → stay in shadow |
| Data classification | R2: logs and stored bodies are hard to clean up later | Bodies and entries are confidential financial data, kept in the DB only. Logs carry event ID, type, account ID and outcome, never the secret or the signature header | Payloads hold IDs and amounts, no card data | Phase 1 log review | A new event type carries personal data → redact before storing |

**R1 defaults:**
- Monolith vs services: one module behind one HTTP handler.
- Build vs buy: build (stdlib-only is a REQUIREMENT).
- Balance: sum on read; a cache is deferred.
- Bad, missing or stale signature → 400.
- Unknown event types: verify, record raw body and hash, reply 200, no balance change.

**N/A:** tenancy (one merchant account); AI rows (no AI component).

## 4. Walking skeleton (Phase 0)
- **Request:** PayCo test mode sends one `payment.succeeded` for `acct_test_1` (2500 EUR). The flow:
  1. The HTTP layer passes raw bytes and headers to `handle()`.
  2. `handle()` checks the signature (case-insensitive header lookup, `hmac.compare_digest`, 300 s window), parses the body and writes one entry.
  3. It returns `200 {"status":"applied"}`.
  4. An ops CLI call to `balance("acct_test_1")` returns 2500.
- **Tiers:** PayCo test mode → TLS ingress → HTTP layer → `LedgerService` → ledgerkit client → managed DB (separate namespace); read path: ops CLI → `balance()`.
- **Deploy and rollback:**
  - CI runs the tests on a pinned Python version, builds a versioned artifact and deploys it.
  - Rollback = redeploy the previous artifact. Schema changes are additive only.
- **Logs and alerts:**
  - One structured log line per delivery: trace ID, event ID, type, account, outcome, status, latency.
  - Alerts: 5xx sustained for 15 min; any 409; signature failures above a threshold.
- **Who can reach it:** The URL is public (PayCo has to reach it), but only the **test-mode** secret is configured. No live webhook is registered and nothing reads Ledger. This skeleton has no idempotency yet, which is fine on test data.
- **Exit check V0:**
  - a test-mode event shows one trace from ingress to DB and back through the CLI read;
  - a deploy and a rollback both succeed through the pipeline;
  - an injected `StoreError` returns 503, PayCo retries, and the alert fires;
  - p50/p99 latency is recorded as a baseline.
- **Also starts in Phase 0 (they take time):**
  - get the ledgerkit docs, PayCo answers, Finance statement sample and the export;
  - list what reads the spreadsheet today (each item CONFIRMED or ASSUMED).

## 5. Phases

- **Phase 1 — Core correctness (sandbox and MemoryStore only)**
  - Unlocks: a specified, tested `handle`, `balance` and `entries`.
  - Depends on: V0.
  - Tasks:
    1. Store spike, V1 (affects the most later work).
    2. Specify the data model: events, entries, payments index, account currency, parked refunds, quarantine.
    3. Sandbox redelivery checks, V2.
    4. Signature verifier, V3:
       - multiple `v1` values allowed;
       - non-integer `t` rejected;
       - body size cap;
       - verify before parsing.
    5. Validation, then the status map:
       - strict JSON ints (no bool or float), amount > 0, currency `^[A-Z]{3}$`, required fields present;
       - 200 for applied, duplicate, parked, ignored or quarantined;
       - 400 for a bad signature or malformed body; 409 for a conflict (incident alert, account untouched); 503 for `StoreError`; 500 for anything unexpected.
    6. Idempotent write order: event record (put-if-absent; conflict check) → business checks → payment index → entry (put-if-absent on `event_id`) → apply any parked refund → mark event done. A "done" marker is written only last, so a retry always finishes the remaining steps.
    7. Refund rules: same account, same currency, amount equal to the payment's, at most one refund per payment; otherwise park or quarantine.
    8. Unknown types are recorded and ignored.
    9. `entries()` ordered by application sequence.
    10. Fault and ordering property suite, V4.
    11. Finance signs off the status map.
  - Rollback: code only; nothing live.
  - Exit check: V1–V4 pass.

- **Phase 2 — Live shadow on the managed DB**
  - Unlocks: Ledger balances computed from live events while the spreadsheet remains the system of record.
  - Depends on: Phase 1, V2, V3, V4.
  - Tasks:
    1. Concurrency test on the real DB, V5.
    2. Load check at the throughput budget.
    3. Register the live endpoint with the live secret (shadow mode).
    4. Backfill history from the PayCo export through the same internal apply path:
       - synthetic `event_id` `backfill:<payment_id>` unless the export has real IDs;
       - entries tagged `source=backfill`;
       - overlap with live events is deduplicated by the unique `payment_id`;
       - refunds parked for old payments are applied as the backfill fills them in.
    5. Daily diff of Ledger against PayCo's daily export, per account.
    6. Alerts for parked-refund age, quarantine count and the first event of any unknown type.
    7. Ops CLI to release or dismiss quarantined events, with an audit record.
    8. Restore drill, which sets the stored-data RPO/RTO.
    9. Monthly reconciliation, V6.
  - Rollback: unregister the endpoint and drop the Ledger data; nothing depends on it yet.
  - Exit check: V5 and V6 pass; the restore drill meets the RPO/RTO once its UNKNOWN is supplied.

- **Phase 3 — Cutover**
  - Unlocks: the product and support read balances from Ledger.
  - Depends on: V5, V6, missing input 2.
  - Tasks:
    1. Per-cohort read flag in the product, in order: internal accounts → 5% → 25% → 100%. Each step widens only after a week with a zero daily diff.
    2. Support's hand fixes are replaced by the quarantine workflow.
    3. The script keeps running for comparison.
    4. Decommission go/no-go, V7.
  - Rollback: flip the cohort flag back to the spreadsheet path, until V7.
  - **Point of no return:** the spreadsheet stops being maintained, guarded by V7. It is archived read-only, never deleted.
  - Exit check: V7 passes and every cohort is on Ledger.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | §4 exit check with PayCo test-mode events | The §4 exit check | Trace screenshot, pipeline run IDs, alert record and latency baseline in the runbook repo | Phase 1 | 0 |
| V1 | The store offers put-if-absent (or transactions) and linearizable reads, and we know what a `StoreError` means | Spike against MemoryStore and the managed-DB client: conditional writes, read-after-write, and faults injected after a write is saved | A primitive is confirmed for each step of the write order (design check, signed off by the eng owner) | Spike notes and test script in `docs/spikes/store.md` | Data model specified; fails → single writer with a per-account lock, or one transaction | 1 |
| V2 | PayCo re-signs retries with a fresh `t`, and redelivered bodies are byte-identical | Force a 503 in the sandbox; capture ≥5 retries and a manual resend | 100% of retries have a fresh `t` and an identical body hash | Captured deliveries stored as test fixtures | 503 and raw-hash policy kept; fails → flips in the §3 rows | starts 0, required before 2 |
| V3 | Only genuine, recent PayCo deliveries are accepted | Test matrix: PayCo/sandbox vectors; tampered body; wrong secret; t ± 301 s; missing or malformed header; header case variants; multiple `v1` values. Then a security review | 100% of the matrix behaves as expected, and the security reviewer (to be named) signs off | Test report in CI artifacts and a review record | Live endpoint (Phase 2); fails → fix and re-run, Phase 2 waits | 1, required before 2 |
| V4 | Under any order, duplicates and partial failures, retrying until 2xx converges to the fault-free result | Seeded property tests on a fault-injecting store wrapper. Raise `StoreError` at every write position, both "not saved" and "saved, then raised". Shuffle orderings, include refund-before-payment, add duplicates and parallel duplicates | 0 divergences in balance or entries across every single-fault position plus 10,000 random sequences; balance = sum of entries; at most one entry per event ID (REQUIREMENT: exact match) | CI test report with seeds | Live shadow (Phase 2); fails → fix the write order before going live | 1, required before 2 |
| V5 | The exactly-once guarantees hold under real concurrency on the managed DB | 2+ instances; 1,000 events, each delivered 3× at once with faults injected | 0 double or missing entries | Load-test report in `docs/tests/` | Cutover (Phase 3); fails → single-writer flip (§3) | 2 |
| V6 | Ledger matches PayCo for every account | One full monthly statement, plus daily diffs over that month | Exact match to the minor unit for 100% of accounts (REQUIREMENT); every daily diff root-caused; no unresolved quarantined event or parked refund; the Finance lead (to be named) signs off | Reconciliation reports archived per month | Phase 3; fails → stay in shadow; if disputes or fees caused it, pull their handlers forward | 2 |
| V7 | The spreadsheet can stop being maintained safely | Rehearsal: one month with the spreadsheet frozen read-only and Ledger serving all cohorts; flag rollback rehearsed | Zero diff for the month, rollback done within 1 h, approved by the Finance lead and the eng owner | Go/no-go record and rollback drill log | Retire script and spreadsheet updates; fails → keep running both | 3 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Test secret in a secrets manager; signature check and TLS from the first commit | Structured log per delivery, status metrics, alerts on 5xx, 409 and signature failures | Pinned Python version in CI, stdlib only, versioned artifact, schema in code | 503 on `StoreError` lets PayCo retry; rollback rehearsed |
| 1 | V3 test matrix, body size cap, 409 incident path; secret and signature never logged | One outcome per delivery (applied, duplicate, parked, ignored, quarantined, conflict) as metrics | Seeded property tests; sandbox captures kept as fixtures | Fault-injecting store wrapper (V4); idempotent write order |
| 2 | Separate live secret; rotation runbook; least-privilege DB roles; stored bodies classified | Daily diff dashboard; parked-age, quarantine and unknown-type alerts; clock-skew metric | Backfill script can be re-run safely; reconciliation reports archived | V5; restore drill; outage runbook for the 72 h window |
| 3 | Product gets read-only access; audit trail for quarantine releases | Diff and read latency per cohort | Flag configuration versioned | Per-cohort rollback flag; script runs until V7 |

## 8. AI layer
N/A — no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Handlers for disputes, payouts and other new types.** Pulled forward when the first unknown-type event shows up in shadow, or when the statement includes them (V6). Stored raw bodies let us replay the history once a handler exists.
- **Async inbox or queue.** Pulled forward when p99 exceeds 50% of PayCo's timeout, volume passes 50k/day, or V2 fails.
- **Cached balance.** Pulled forward when an account passes about 10k entries, or `balance()` p95 exceeds 100 ms.
- **Signing with two secrets during rotation.** Before the first planned rotation, or immediately after any 409 incident.
- **Spending (debit) entries in Ledger.** Pulled forward if missing input 2 says Ledger owns spending; that is an interface change.
- **Manual adjustment entries.** Pulled forward if releasing quarantined events or Finance corrections need them.
- **Partial refunds.** Pulled forward when quarantines for "refund ≠ payment amount" show up, which would mean PayCo changed its behaviour.
- **Deleting spreadsheet history.** Not planned; it stays archived after V7.
