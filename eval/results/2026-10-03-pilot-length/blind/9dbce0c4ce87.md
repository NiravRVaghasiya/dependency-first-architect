# BUILD PLAN: Multi-tenant SaaS billing system

## 1. Classification and constraints
- **What:** The billing subsystem of a multi-tenant SaaS. It handles the plan catalog, subscriptions, usage metering, rating, invoices, payments, dunning and finance exports, billing the SaaS's own tenants.
- **Type:** Software. Greenfield. Not a small build, because it moves money and holds personal data.
- **Dominant constraint:** Correctness.
- **Worst failure:** A tenant is charged the wrong amount or charged twice. Close behind: one tenant sees another tenant's billing data.

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Invoice correctness | 0 unexplained ledger-to-processor discrepancies per cycle | ASSUMPTION (the finance standard) | V5, V7 |
| Usage ingest throughput | UNKNOWN: product supplies the peak events/sec and tenant count, needed by Phase 2 | UNKNOWN | V6 |
| Billing API availability | 99.9% monthly | ASSUMPTION (typical internal SLO) | Phase 4 |
| RPO / RTO (ledger) | 0 committed entries lost / 4 h | ASSUMPTION (financial records) | Phase 1 restore drill |
| Ingest cost | UNKNOWN: finance supplies the cost ceiling per million events, needed by Phase 2 | UNKNOWN | V6 |
| Operational complexity | ≤3 deployables, 1 on-call rotation | ASSUMPTION | Phase 4 |
| Card data scope | No card numbers (PAN) stored, processed or logged | REQUIREMENT (PCI DSS scope minimization) | V3 |

**Missing inputs:**
1. Whose billing is this? I assume it bills the SaaS's own tenants. If it is billing-as-a-service for merchant tenants, add a merchant onboarding/KYC phase (e.g. Stripe Connect) before Phase 3, and tenant isolation becomes the worst failure.
2. Pricing models. I assume subscriptions plus metered usage.
3. Tax jurisdictions and currencies. I assume USD and EUR.
4. Revenue-recognition needs.
5. Data residency.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Ledger schema | Finance controller's sign-off on the chart of accounts | Organizational / decision | Specified |
| Phase 0 payment tier | Processor account and test-mode keys | Organizational | Built |
| First live charge | Signed merchant agreement, V2, V3, V7 | Organizational / risk | Exposed |
| Invoicing to external tenants | Signed tax-engine contract, V5 | Organizational / validation | Exposed |
| Usage pipeline design | Peak volume and cost ceiling (UNKNOWN), V6 | Economic / validation | Hardened, committed |
| Any external tenant reaching the portal | V4 isolation | Risk / security | Exposed |
| Invoice retention policy | Legal input on retention period and residency | Organizational | Specified (Phase 4) |

Every dependency kind has at least one instance.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R3: wrong money is unrecoverable harm | Ledger strongly consistent: Postgres, ACID, append-only double-entry. Usage ingest eventually consistent, with a closing cutoff | One primary handles ledger write volume | V1 | V1 fails at the required write rate → shard the ledger by tenant |
| Monolith vs services | R2: splitting later costs weeks | Modular monolith (catalog, subscriptions, rating, ledger, invoicing) plus a separate usage-ingest service | Ingest scales differently from the rest | Phase 2 load test | One module's deploys block the others' releases >2×/month → extract it |
| Sync vs async | R2: retrofitting idempotency is costly | Async charges via a transactional outbox. Idempotency keys on every money operation. Processor webhooks deduplicated | Processor supports idempotency keys | V2 | V2 fails → serialize charges per tenant behind a lock |
| Build vs buy | R3: billing history becomes stuck at the vendor | Buy payments (Stripe or Adyen) and tax (Stripe Tax or Avalara). Build the ledger and rating | Pricing is too custom for off-the-shelf billing products | Product review of the pricing models | Pricing fits Stripe Billing or Chargebee → buy, and cut this plan to an integration |
| Data-privacy boundary | R3: PCI scope and personal-data exposure | Processor tokens only. Billing PII in a separate encrypted table. PII never in logs | Hosted payment fields keep us at SAQ A | V3 | V3 fails → re-scope to SAQ A-EP with added controls |
| Tenancy model | R3: isolation retrofit means months of work | Shared Postgres with `tenant_id` and row-level security (RLS) on every table | No contract requires a dedicated database | V4 | V4 fails, or an enterprise contract requires dedicated storage → cell per tenant |
| Money and identifiers | R3: the format persists in every record | Integer minor units plus ISO currency. Banker's rounding at invoice line level. ULIDs. Prices versioned and never mutated | Finance accepts line-level rounding | V5 (finance sign-off) | Finance requires a different rounding rule → change it before Phase 2 is specified |

**R1 defaults:** Language: Kotlin. CI: GitHub Actions. Queue: SQS. Dashboards: Grafana. PDF rendering: a server-side template.
**N/A:** Brownfield and AI rows, because this is greenfield with no AI component.

## 4. Walking skeleton (Phase 0)
- **Request:** An internal test tenant on a flat $1 plan records one usage event, then closes the billing period. The response is an invoice, a payment confirmation and an email.
- **Tiers:** API gateway with tenant authentication → usage ingest → billing monolith → Postgres ledger → outbox → processor → webhook handler → notification service.
- **Day zero:**
  - Deployed by the CI pipeline with blue/green deploys.
  - Structured logs and OpenTelemetry traces carry `tenant_id` and `invoice_id`, never PII.
  - Alerts on webhook failures and on a ledger that doesn't balance.
  - Rollback is a blue/green switch.
- **Who can reach it:** Internal tenant only, behind a flag. The payment tier uses processor **test mode** from the production deployment, so no real money moves before V2, V3 and V7.
- **Exit check:** V0.

## 5. Phases

**Phase 1: Ledger and tenancy core**
- **Unlocks:** Every money-writing module.
- **Depends on:** V0. Finance sign-off on the chart of accounts.
- **Tasks:**
  1. Double-entry schema and invariants.
  2. RLS on every table.
  3. Money and ULID types.
  4. Idempotency-key table.
  5. Outbox.
  6. Point-in-time recovery and a restore drill.
- **Rollback:** No external data exists yet, so drop and re-migrate.
- **Exit check:** V1 and V4 pass (the latter on internal data). The restore drill meets RPO and RTO.

**Phase 2: Catalog, subscriptions and rating**
- **Unlocks:** Correct invoices.
- **Depends on:** Phase 1. Volume and cost inputs (§1).
- **Tasks:**
  1. Versioned price catalog.
  2. Subscription lifecycle, including proration.
  3. Usage aggregation with a late-event cutoff.
  4. Rating engine.
  5. Tax-engine integration in sandbox.
- **Rollback:** Feature-flag each module. Data is still internal.
- **Exit check:** V5 and V6 pass.

**Phase 3: Payments, dunning and reconciliation**
- **Unlocks:** Real money.
- **Depends on:** Phase 2, V2, V3, a signed merchant agreement.
- **Tasks:**
  1. Hosted payment fields.
  2. Live processor keys.
  3. Webhook deduplication.
  4. Retry and dunning schedules.
  5. Refunds and credit notes.
  6. Daily reconciliation of the ledger against processor payouts.
- **Rollout:** Live charges go to the internal tenant first, then one design-partner tenant.
- **Point of no return:** The first live charge, guarded by V7. Refunds are the compensation path.
- **Exit check:** V7 passes.

**Phase 4: Invoicing, portal and rollout**
- **Unlocks:** All tenants.
- **Depends on:** Phase 3. V4 re-run plus a penetration test.
- **Tasks:**
  1. Invoice PDFs and retention, per legal input.
  2. Tenant billing portal.
  3. General-ledger export.
  4. Rollout waves of 5% → 25% → 100% of tenants, each wave gated on a clean reconciliation cycle.
- **Rollback:** The flag removes a wave from the new billing path. Issued invoices are corrected only by credit notes, never edited.
- **Exit check:** Two consecutive clean cycles at 100%. The 99.9% availability SLO is held for 30 days.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | The skeleton run in §4 | The four-part check: request succeeds with one trace; deploy and rollback succeed; injected failure fires an alert; baselines recorded | Trace ID, pipeline run log, alert record, baseline sheet in the repo `/evidence/v0` | Phase 1 (fix the tier that broke) | 0 |
| V1 | The ledger never goes out of balance under concurrency | Property-based tests with 50 concurrent writers | 1M random transactions, 0 imbalances, ≥200 writes/s (ASSUMPTION: 10× expected) | Test report and invariant-check output in CI artifacts | Phase 2 (if it fails: shard by tenant, §3) | 1 |
| V2 | No tenant is ever charged twice | Fault injection: duplicate webhooks, crashes after the processor call, retries | 10,000 scenarios, 0 duplicate or missing charges (REQUIREMENT: worst failure) | Chaos test report and processor test-mode logs in `/evidence/v2` | Live keys in Phase 3 (if it fails: per-tenant lock, §3) | Starts 1, required before 3 |
| V3 | Card data never enters our systems | Data-flow review plus a scanner that looks for PANs in logs and databases | Security lead and QSA (PCI assessor) sign SAQ A scope. Scanner finds 0 PANs | Signed scope memo and scanner report in the compliance store | Phase 3 exposure (if it fails: SAQ A-EP controls) | Starts 0, required before 3 |
| V4 | No tenant can read or write another tenant's data | RLS tests plus cross-tenant fuzzing of every endpoint. Penetration test before Phase 4 | 0 cross-tenant reads or writes. Penetration tester signs off with no high findings | Fuzz report and penetration-test report in the security store | External tenants (if it fails: cell per tenant, §3) | 1, re-run 4 |
| V5 | Rating and rounding match finance's expectation | A finance-supplied golden dataset of pricing, proration, tax and currency cases | 100% match on ≥500 cases (ASSUMPTION). Finance controller signs off | Diff report and sign-off in `/evidence/v5` | Phase 3 (if it fails: fix the rounding or proration model) | 2 |
| V6 | Ingest handles peak load within budget | Load test at 3× peak, run for 1 h | Peak and cost ceiling are UNKNOWN until product and finance supply them (§1). 0 events lost | Load-test dashboard export and cost report | Hardening the ingest design (if it fails: batch at the edge, or pre-aggregate on the client) | 2 |
| V7 | Live money flows reconcile | One full live cycle for the internal tenant and the design partner. Refund rehearsal | 0 unexplained discrepancies. Refund round-trip works. Finance approver gives go/no-go | Reconciliation report, refund receipt, signed go/no-go | Rollout in Phase 4 (if it fails: halt live charges, refund, re-plan Phase 3) | 3 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Secrets in a secrets manager. Processor test keys only. Tenant authentication at the gateway | Traces, structured logs and alerts with PII redaction | Infrastructure as code (IaC) and pinned builds | Blue/green rollback |
| 1 | RLS. PII table encrypted with KMS | Ledger-balance metric and alert | Versioned migrations | Point-in-time recovery and restore drill |
| 2 | Catalog edits audit-logged and need two approvers | Ingest lag and dropped-event metrics | Deterministic rating replay from raw events | Ingest backpressure and dead-letter queue (DLQ) |
| 3 | Live keys scoped and rotated. Webhook signatures verified | Reconciliation dashboard and alert on any discrepancy | Idempotent replay of the outbox | Circuit breaker on the processor. Retries with jitter |
| 4 | Portal role-based access control (RBAC). Penetration test (V4) | SLO dashboards for each rollout wave | Invoice regeneration is byte-identical | Wave rollback flag. Failover drill |

## 8. AI layer
N/A — no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Revenue-recognition engine (ASC 606):** pull forward when finance needs automated deferred-revenue schedules.
- **Dedicated per-tenant database cells:** pull forward when an enterprise contract requires them, or V4 fails.
- **A second payment processor:** pull forward when processor availability or approval rates breach the SLO.
- **Currencies beyond USD and EUR:** pull forward when sales commits to a new market.
- **Self-serve pricing editor:** pull forward when catalog changes exceed what engineering can handle, roughly a weekly cadence.
- **Billing-as-a-service for merchants:** pull forward if missing input 1 resolves that way, which adds the KYC phase (§1).
