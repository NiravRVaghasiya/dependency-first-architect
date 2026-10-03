# Plan: Multi-tenant SaaS billing system

**Interpretation I'm building on (not confirmed):** "Tenants" are B2B customers of our platform. Each tenant bills its own customers through subscriptions and metered usage, and we run the ledger and invoicing. A payment provider (PSP) moves the money, so card numbers never touch our systems. If this is only internal billing for one SaaS company's own customers, Phases 1 and 6 shrink and tenant-level isolation drops to customer-level isolation. The phase order stays the same.

## 1. Classification and constraints
- **What:** A platform that meters usage, rates it against versioned plans, keeps an immutable ledger, issues invoices, collects payment through a PSP, and exposes an API and webhooks to tenants.
- **Type:** Software, greenfield, not small (real money, outside users, financial and personal data).
- **Dominant constraint:** Correctness (money and tenant isolation), with compliance (PCI scope, tax and financial records) second.
- **Worst failure:** Customers are charged wrongly or twice, or one tenant's billing data is exposed to another, and nobody notices before money has moved. V1, V2, V4 and V6 guard this.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency | Sync API p95 < 300 ms; usage-event ack p99 < 200 ms | ASSUMPTION: typical for CRUD and ingest APIs; confirm with the product owner | V0 records the floor; V3 |
| Throughput | No number | UNKNOWN: peak usage events/s and tenant count at 12 months; product owner and sales supply before Phase 3 | V3 |
| Availability / SLO | No number | UNKNOWN: the SLA promised to tenants; product owner before Phase 4. Design choice: ingest keeps accepting events when invoicing is down | SLIs and burn-rate alerts in Phase 4 |
| RPO / RTO | RPO ≤ 5 min, RTO ≤ 1 h | ASSUMPTION: basis is point-in-time recovery on a managed Postgres primary; finance and ops confirm before Phase 4 | Restore drill in Phase 4 |
| Correctness | 0 unexplained differences (minor units) between the ledger and PSP settlement | ASSUMPTION: finance to confirm any tolerance | V2, V6 |
| Storage cost | No number | UNKNOWN: event volume × retention. Finance records retention is set by law per jurisdiction, so legal supplies the REQUIREMENT before Phase 3 | Phase 3 exit check |
| Operational complexity | 1 deployable, 1 worker, managed Postgres, managed queue | ASSUMPTION: a small team, which I don't know | Phase 2 exit check |

**Missing inputs** (the first three change the phase order or structure):
1. Who moves the money: PSP as merchant of record, or tenants as sub-merchants (Stripe Connect style). This decides KYC and onboarding lead time. Owner: product and legal.
2. Jurisdictions and currencies, including data residency and tax. Owner: legal and finance.
3. Pricing-model scope: flat, per-seat, metered, tiered, prepaid credits. Owner: product.
4. Peak usage volume (see Budgets).
5. Named approvers: a finance owner for V2 and V6, a security reviewer for V1, and a design-partner tenant for V5.
6. Cloud and identity platform already in use.

## 2. Dependencies

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Schema, keys, API handlers | Tenancy and isolation model (Tradeoffs) plus V1 | decision + risk/security | specified |
| Invoicing, rating, payments | Ledger invariants (V2) | validation | specified |
| Partitioning and queue design | Peak event volume (UNKNOWN) and V3 | validation | specified |
| Public API | Contract sign-off (V5) | decision + risk/security | exposed |
| Live card payments | PSP account and underwriting, PCI SAQ A (V4) | organizational + risk/security | exposed |
| Real invoices and charges | Finance approver, reconciliation (V6), tax registration | organizational + risk/security | exposed |
| Tax calculation | Tax jurisdictions (Missing input 2) and tax vendor contract | organizational | built (real integration) |
| Any logging or tracing | Data classification (no PAN, minimal PII, tenant ID only) | risk/security | built |
| Widening past one tenant | V6 check per cohort | risk/security | exposed |

Organizational items (PSP account, tax vendor, approvers, legal terms and DPA) start in Phase 0 because of lead time. Unconfirmed ones sit under Missing inputs. Economic dependency: pricing of the platform itself is out of scope here. Structural and runtime dependencies follow the phase order.

## 3. Key decisions

| Decision | Reversibility | Default | Assumption | Validated by | Revisit trigger |
|---|---|---|---|---|---|
| Tenancy and isolation | R3: customer-visible exposure, and a data migration once data exists | Shared Postgres, `tenant_id` on every row, row-level security (RLS) enforced for the app role, tenant-scoped keys, a `region` attribute per tenant | Residency and contractual needs are met by a shared schema | V1 | V1 fails, or a contract requires a dedicated DB or region: add a silo or cell tier |
| Ledger and data model, identifiers | R3: financial records are legally retained and can't be rewritten | Append-only double-entry ledger; integer minor units plus currency; UUIDv7 IDs; immutable, versioned prices and plans; invoices snapshot their prices; corrections via credit notes; idempotency keys | Finance accepts this chart of accounts and rounding | V2 | V2 fails: redesign before Phase 2 |
| Consistency vs availability | R3: wrong money is harmful | Strong consistency (single-primary Postgres, serializable for balance-affecting writes) for ledger and payment state. Eventual consistency for usage ingest and read models | One primary carries ledger writes; usage, the high-volume part, is kept off the ledger path | V3 (capacity) | V3 fails: partition or stream the ingest path; the ledger stays strongly consistent |
| Public API contract | R3: outside consumers | REST + OpenAPI, `/v1`, tenant-scoped API keys, mandatory `Idempotency-Key`, signed webhooks with versioned events, cursor pagination, additive-only within a version | A design-partner tenant represents typical consumers | V5 | V5 fails: revise before any exposure |
| Data-privacy boundary | R3: exposed personal or card data can't be undone | PSP-hosted fields and tokens only (PCI SAQ A target), no PAN in our systems. End-customer PII minimised to name, email, address, tax ID. Telemetry carries tenant ID and opaque IDs only. Single region, with residency to be confirmed | Hosted fields keep scope at SAQ A | V4 | V4 fails, or a residency requirement appears: re-scope the card flow or add a regional cell |
| Build vs buy | R2: weeks to swap, with a built seam | Build the ledger, rating and invoicing (the differentiator and system of record). Buy payment execution (Stripe or Adyen), tax (Stripe Tax or Avalara), and identity (managed OIDC). The PSP sits behind an interface and holds no source-of-truth state | A PSP's subscription objects would create data gravity; we avoid them | Cheap check: Phase 4 builds the interface and a tested export, passing a second-PSP contract-test stub | Build effort for rating and invoicing exceeds the plan, and an off-the-shelf engine (Lago, Kill Bill) fits the pricing scope: adopt it behind the same ledger |
| Monolith vs services | R2: internal boundary | Modular monolith plus one worker process, with module boundaries per domain (tenancy, ledger, metering, invoicing, payments) | A small team; the modules share one transaction boundary | Phase 2 exit check | Ingest throughput or team count forces splitting metering out |
| Sync vs async | R2 | Sync for API writes and payment-intent creation. Async through a transactional outbox for usage ingest, invoice runs, webhooks in and out, and dunning | Outbox plus a managed queue is enough; the queue is not the system of record (the Postgres event log is) | V3 | V3 fails: a stream platform (Kafka or Kinesis) |

**minor defaults:** language and framework → one typed mainstream stack the team knows; IaC → Terraform; CI → the team's existing CI; observability stack → OpenTelemetry to an existing backend; PDF rendering → an HTML-to-PDF library.
**N/A:** Brownfield migration (greenfield), AI rows (no AI component).

## 4. First end-to-end slice (Phase 0)
- **Real request:** For a designated internal test tenant, `POST /v1/usage-events` (with an `Idempotency-Key`) → `GET /v1/invoices/draft` returns a draft invoice containing one rated line and a $0 total. No money movement, and the PSP is not called.
- **Tiers crossed:** edge/WAF → API (authn, tenant context) → Postgres (tenant-scoped tables, ledger entry) → outbox → worker (rating, draft invoice) → Postgres → read API. The telemetry pipeline sits alongside.
- **Deploy:** IaC and CI/CD build an immutable image from a trunk commit, apply migrations in expand/contract style, then deploy to production through a feature flag. Secrets come from a secret manager.
- **Logs and monitoring:** Structured logs and one trace per request with `tenant_id` and opaque IDs only (no PII, no payloads). Dashboards for RED metrics (rate, errors, duration) and outbox lag. One synthetic probe runs every minute.
- **Rollback:** Redeploy the prior image and flip the flag. Migrations stay backward-compatible, so rollback never needs a schema revert.
- **Who can reach it:** Allow-listed internal IPs and the internal test tenant only, behind the flag. No external tenant or real end-customer data until V1 and V5 pass.

**Exit check (V0):** The request succeeds with one trace across every tier. A deploy and a rollback succeed through the pipeline. An injected failure (worker killed, DB connection dropped) fires an alert. Baselines are recorded for latency and outbox lag at near-zero load.

## 5. Phases

- **Phase 1 — Tenancy, identity, and ledger core** (widest impact first)
  - Unlocks: Every domain module builds on validated isolation and money invariants.
  - Depends on: Phase 0; V0.
  - Tasks:
    1. Tenant model, `tenant_id` and `region` on every table, RLS with a non-bypass app role.
    2. Authn and authz: OIDC for tenant admins, scoped API keys, RBAC, and an append-only audit log.
    3. Ledger: chart of accounts, double-entry postings, idempotency, and the event log.
    4. Plan and price versioning.
    5. Draft the OpenAPI v1 contract (V5 starts).
  - Rollback: Expand/contract migrations and the feature flag; no real data yet.
  - Exit check: V1 and V2 pass.

- **Phase 2 — Catalog, subscriptions, and invoicing**
  - Unlocks: Flat-fee billing cycles end to end on synthetic data.
  - Depends on: Phase 1; V1, V2.
  - Tasks:
    1. Products, plans, and subscription lifecycle (trial, upgrade, proration, cancel).
    2. Invoice generation with immutable snapshots, gap-free per-tenant numbering, and credit notes.
    3. Tax behind an interface (stub).
    4. PDF and invoice delivery to a sink mailbox.
    5. Property and time-zone tests for proration and billing dates.
  - Rollback: Flag off per module; synthetic data only.
  - Exit check: Operational-complexity budget holds (1 deployable, 1 worker, managed DB and queue); the invoice totals property suite passes.

- **Phase 3 — Usage metering and rating**
  - Unlocks: Metered and tiered pricing, and the capacity decision.
  - Depends on: Phase 2; peak volume and retention inputs (Missing inputs 3, 4); V2.
  - Tasks:
    1. Ingest endpoint: dedupe by event ID, per-tenant quotas, and backpressure.
    2. Aggregation windows and late-event handling.
    3. Rating into invoice lines.
    4. Retention and archival policy.
  - Rollback: Flag off; events remain in the durable event log and can be replayed.
  - Exit check: V3 passes.

- **Phase 4 — Payments, tax, and operational readiness**
  - Unlocks: Live money can be considered. Test-mode only until V4 passes.
  - Depends on: Phase 3; PSP account and tax vendor in hand (organizational); V2.
  - Tasks:
    1. PSP interface and adapter (hosted fields, payment intents with idempotency keys, webhooks with signature checks, duplicates and reordering handled).
    2. Refunds and dunning.
    3. Real tax vendor integration.
    4. Daily reconciliation job: ledger vs PSP settlement.
    5. SLIs, SLOs, and burn-rate alerts, with the SLA input.
    6. Backup restore drill against the RPO/RTO assumption.
    7. Per-charge and daily charge caps.
    8. Tested data export.
  - Rollback: Adapter flag off; the PSP stays in test mode. No live charges exist yet.
  - Exit check: V4 passes; restore drill meets RPO/RTO; the reconciliation job runs green in test mode for 7 consecutive days.

- **Phase 5 — Single-tenant live-money canary** (smallest exposure first)
  - Unlocks: The first real invoices and charges.
  - Depends on: Phase 4; V1, V4, V5, V6; finance approver named; tax registration in place.
  - Tasks:
    1. Expose the API to the design-partner tenant only.
    2. Run shadow invoices for one full billing cycle, compared against the tenant's expected amounts.
    3. On go, enable live charging with low caps.
  - Rollback: Before capture, void invoices and disable charging. After capture, refund and issue credit notes (money that reached a card is the point of no return, mitigated by refunds). The charge caps bound the exposure.
  - Exit check: V6 passes.

- **Phase 6 — Cohort widening and self-serve**
  - Unlocks: General availability.
  - Depends on: Phase 5; V6.
  - Tasks:
    1. Widen from 1 to 5 to 25 tenants, then open onboarding.
    2. Tenant admin console and invoice portal.
    3. Support runbooks.
  - Rollback: Pause onboarding and return to the previous cohort size by flag.
  - Exit check: Each cohort runs a full cycle with 0 unexplained reconciliation differences (ASSUMPTION) and a dispute and error rate within the SLO before the next widening.

## 6. Validation checks

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed, and rolled back through every tier in production | Run the skeleton request, deploy, rollback, and fault injection | The V0 exit check in §4 | Pipeline run logs, trace export, alert record, baseline sheet, kept in the repo's ops folder | Phase 1 | 0 |
| V1 | No tenant can read or write another tenant's data | Automated cross-tenant tests over every endpoint × every tenant table (mismatched IDs, forged keys, direct SQL as the app role); RLS verified on non-superuser; adversarial review | 0 cross-tenant successes (design parameter); named security reviewer pass/fail sign-off | Test report in CI, and the signed review | Phase 2 and all exposure; if it fails: isolation model re-planned (silo tier) | 1 |
| V2 | The ledger keeps money correct under retries, duplicates, ordering, and corrections | Property-based tests on ≥ 1M generated sequences (duplicates, out-of-order events, retries, plan changes, rounding); replay from the event log; finance review of the chart of accounts and rounding rules | Every entry balances; invoice total = sum of lines; replay reproduces balances exactly; duplicate requests create no extra postings (design parameters); named finance reviewer sign-off | CI report and the reviewer's sign-off | Phase 2, 3, 4; if it fails: redesign the data model before further work | 1 |
| V3 | The ingest and rating path sustains peak load without losing events or starving tenants | Load test on 12-month data volume, 1 hour sustained, 5% duplicates, one noisy tenant at 50% of load | 2× projected peak (UNKNOWN until supplied; needed before Phase 3); ack p99 < 200 ms (ASSUMPTION); zero lost events by count reconciliation; other tenants' p99 within 1.5× baseline (ASSUMPTION) | Load report stored with the build | Single-Postgres + outbox design confirmed; if it fails: stream platform or partitioning | 3 |
| V4 | Card data stays out of our scope, and payment flows never double-charge or lose state | Data-flow review against SAQ A eligibility (acquirer or QSA); PAN scan of logs, DB, and traces; fault injection in PSP test mode (duplicate, reordered, dropped webhooks; timeouts; retries) across ≥ 10k scenarios (design parameter) | Acquirer or QSA sign-off; 0 PANs found; 0 double charges and 0 ledger/PSP mismatches | SAQ A attestation, scan output, and fault-injection report | Phase 5; if it fails: re-scope the card flow or fix the state machine | starts in 1 (PSP account lead time), required before 5 |
| V5 | The v1 API contract fits real consumers and can stay stable | Contract-first OpenAPI, consumer-driven contract tests, and a design-partner integration in a sandbox | Design-partner engineer and internal API owner pass/fail sign-off; all contract tests green | Signed OpenAPI spec and contract-test report | External exposure in Phase 5; if it fails: revise before exposure | starts in 1, required before 5 |
| V6 | Real invoices and charges are correct and reversible for the first tenant | Shadow invoice run over one full billing cycle, compared with the tenant's expected amounts, then live charges with per-charge and daily caps | 0 unexplained differences over ≥ 1 full cycle (ASSUMPTION; finance to confirm); go/no-go by the named finance approver (Missing input 5); rollback as in Phase 5; point of no return is capture | Shadow comparison report (IDs and totals only, no customer records) and the signed go/no-go | Live charging, then Phase 6; if it fails: stay in shadow and fix | 5 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Allow-listed access, secrets in a manager, least-privilege IAM, WAF | Trace and logs with tenant ID only, RED dashboards, failure alert | IaC, immutable images, pinned dependencies | Flag off, rollback to prior image |
| 1 | RLS, scoped keys, RBAC, audit log, authz tests in CI (V1) | Per-tenant metrics, audit-log alerts | Seeded fixtures, migration tests in CI | Expand/contract migrations, PITR enabled |
| 2 | Signed invoice artifacts, no PII in PDFs' metadata, dependency scanning | Invoice-run metrics and failure alerts | Deterministic invoice generation, golden-file tests | Idempotent invoice runs, resumable |
| 3 | Per-tenant quotas, rate limits, payload validation | Ingest lag, drop and duplicate counters, per-tenant usage | Replayable event log, load-test harness in repo | Backpressure, dead-letter queue, replay |
| 4 | PSP webhook signature checks, hosted fields only, PAN scan in CI | Reconciliation dashboard, payment-state alerts, SLO burn alerts | PSP test-mode fixtures, recorded contract tests | Charge caps, retries with idempotency keys, restore drill |
| 5 | Pen test of the exposed API, key rotation, incident runbook | On-call, error-budget tracking, per-tenant charge anomaly alerts | Signed-off release with pinned versions | Instant charge kill-switch, refund and void runbook |
| 6 | Tenant admin MFA, abuse and fraud limits, periodic access review | Cohort dashboards, support metrics | Cohort rollouts via config, not code | Pause onboarding, cohort rollback, DR exercise |

## 8. AI layer
N/A — no AI component.

## 9. Exceptions
None.

## 10. Deferred
- **Multi-currency and FX:** pulled forward when the first tenant needs a second currency (the ledger stores currency from day one).
- **Dedicated silo or regional cell tenancy:** pulled forward when V1 fails or a contract or residency rule requires it.
- **Revenue recognition (ASC 606 / IFRS 15) reporting:** pulled forward when finance or an auditor requests it.
- **Service split or stream platform:** pulled forward when V3 fails or the team outgrows one deployable.
- **Multiple PSPs or payment methods beyond cards (ACH, SEPA):** pulled forward on a tenant contract or when PSP failover is required.
- **Prepaid credits and complex commitments:** pulled forward when the pricing scope (Missing input 3) includes them.
- **Advanced dunning and churn analytics:** pulled forward when involuntary churn exceeds a target set by the product owner.
- **Data warehouse and tenant-facing analytics:** pulled forward on the first reporting requirement that the read API can't meet.
