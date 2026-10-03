# BUILD PLAN: Multi-tenant SaaS billing system

The request leaves scope open, so I made the assumptions below (see Missing inputs). Items marked ASSUMPTION are starting values to confirm. They are not requirements.

## 1. Classification and constraints
- **What:** A billing platform for a SaaS vendor's own customers (the tenants). It meters usage, rates it against plans, issues immutable invoices, collects payment through a processor, and keeps a double-entry ledger.
- **Type:** Software, greenfield, **not small**. It handles money and personal data, and it is used by outside tenants.
- **Dominant constraint:** Correctness, meaning money is exact and auditable and tenants are isolated.
- **Worst failure:** Customers are charged the wrong amount (double, wrong, or unreversible), or usage is silently lost or duplicated, and nobody detects it. The second-worst failure is one tenant seeing another's billing data. V2 guards the first and V3 guards the second.

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Money accuracy | Zero ledger invariant violations; every invoice total equals an independent recalculation to the minor unit | ASSUMPTION: rounding and proration rules are to be signed off by Finance | V2, V6 |
| Usage-ingest latency | p95 < 200 ms at projected peak | ASSUMPTION: a typical metering-API target | V0 baseline, V4 |
| Throughput (usage events/s) | — | UNKNOWN: needs projected events per second at 12 months, from the product owner and finance, in Phase 0 | V4 |
| Availability / SLO | — | UNKNOWN: needs the availability the business commits to, from the product owner, before Phase 5. Ledger writes favor correctness over uptime (see §3). | SLIs and burn-rate alerts in Phase 5 |
| RPO / RTO | Ledger RPO ≤ 5 min, RTO ≤ 1 h | ASSUMPTION: managed Postgres multi-AZ with PITR; Finance to confirm | Restore drill in Phase 4 |
| Bill-run duration | — | UNKNOWN: needs tenant count and invoice volume, from the product owner, before Phase 3 hardens | Phase 3 load test |
| Operational complexity | One modular monolith, one worker, one managed Postgres; no self-run cluster | ASSUMPTION: a small team | Phase 5 exit check |

**Missing inputs** (none changes the phase order unless noted):
1. **Who the tenants are.** I assumed the vendor bills its own customers. If the platform bills on behalf of tenants (marketplace or Connect-style), the order changes. That case adds money-transmission, KYC, and payout dependencies before any live money. Confirm this first.
2. The pricing models and the list of plans to support. V1 needs them.
3. Currencies, tax jurisdictions and registrations, and legal invoice-numbering rules. They shape the R3 ledger schema, so they are needed before Phase 1 specifies it. The ledger is multi-currency-ready, with one currency enabled.
4. Projected volume and the availability commitment (both UNKNOWN above).
5. Cloud and stack. I assumed AWS.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Ledger schema and identifiers | Currencies, jurisdictions, and invoice-numbering rules (input 3) | decision / organizational | specified (Phase 1) |
| Rating, proration, and invoicing | V2, plus Finance sign-off on rounding, proration, and credit rules | validation / decision | specified (Phase 3) |
| Any non-internal tenant | V3 | risk-security | exposed |
| Live card charging | V5 (SAQ A confirmed by the processor or a QSA), the processor's live-mode approval, and a DPA with the processor | risk-security / organizational | exposed |
| Live tax calculation | Tax registrations and a tax-provider contract | organizational | deployed in live mode |
| Bill-run and ingest sizing | Volume (UNKNOWN) and V4 | validation | hardened |
| First real invoice and charge | V6, with a named Finance approver | risk-security / organizational | exposed |
| Phase 1 design | V1 (build versus buy) | decision / validation | specified |

Organizational items start in Phase 0 because they have lead time: the processor account, the tax provider, the named Finance and security reviewers, and the DPAs.

No economic dependency binds beyond the cost comparison inside V1. No structural or runtime dependency is surprising.

## 3. Tradeoff gates

| Decision | Reversibility | Default | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Tenancy and isolation | R3: tenant data in a shared store can't be re-split cheaply, and a leak can't be undone | Shared Postgres schema. `tenant_id NOT NULL` leads every key and is on every row. Row-level security fails closed. A tenant-routing layer exists from Phase 1 as a seam. | No tenant needs physical isolation or residency. | V3 | V3 fails, or a contract requires dedicated storage or residency. In that case the affected tenant moves to its own database through the routing seam. |
| Ledger model and identifiers | R3: real financial data and audit obligations | Append-only double-entry ledger. Money is integer minor units plus an ISO currency code. IDs are UUIDv7. Corrections are reversing entries, never updates. Finalized invoices are immutable, and credit notes correct them. | The pricing models fit entries plus rating rules. | V2 | V2 fails, or V1 shows a bought engine must be the system of record. |
| Consistency vs availability | R3: it defines the money semantics | Ledger, invoice, and payment state are strongly consistent on a single-region multi-AZ primary, and ledger writes fail rather than diverge. Usage ingest is idempotent (unique on `tenant_id`+`event_key`) so clients can retry safely. | Brief write downtime costs less than a wrong charge. | V2, V4 | The SLO (once supplied) can't be met with one primary. Then add replicas for reads and degraded modes, but never relax ledger writes. |
| Build vs buy: billing engine | R3: invoice history and customer-visible numbering migrate painfully | Build the ledger, rating, and invoicing. Buy payments, tax calculation, and email. | Planned pricing (usage tiers, custom contracts, credits) exceeds what Stripe Billing or Lago express cleanly. | V1 | V1 shows all planned plans fit a bought engine at lower total cost. In that case Phase 1 is replanned around an integration plus the ledger. |
| Payment processor | R3: card tokens have gravity at the vendor | Stripe (or Adyen) behind a `PaymentProvider` interface. Card entry uses the hosted fields only. | The processor contract allows token export to another PCI-compliant processor. | V5, plus a contract clause check in Phase 0 | V5 fails, or the token-export clause is refused. Then choose another processor before Phase 4. |
| Data-privacy boundary | R3: exposed personal data can't be recalled | Card data never touches our systems. Personal data (names, emails, addresses, tax IDs) lives in separate tables, with tax IDs encrypted at column level. The ledger holds only opaque IDs, so erasure doesn't break immutability. Logs and telemetry carry IDs only. | GDPR-style erasure and access requests will apply. | V5 (PAN scan), V3 (telemetry scan) | Legal review requires a regional data split. In that case, treat it as the tenancy flip. |
| Monolith vs services | R2: internal boundaries | Modular monolith plus one worker process | One team | CI module-boundary lint, pass = 0 cross-module table accesses | V4 shows ingest needs independent scaling. Extract ingest. |
| Sync vs async | R2 | Usage is written synchronously to a partitioned Postgres table. Aggregation and bill runs are async jobs with idempotent job keys. Payment state is webhook-driven, with webhooks deduplicated by event ID. | Postgres sustains ingest at 2× peak. | V4 | V4 fails. Add a durable log (Kafka/Kinesis) and an analytical store for usage. |

**R1 defaults:** language → TypeScript (NestJS); cloud → AWS (ECS Fargate, RDS Postgres); IaC → Terraform; CI/CD → GitHub Actions; identity → managed OIDC provider (Cognito/Auth0); telemetry → OpenTelemetry to a managed backend; invoice PDF → server-side renderer.
**N/A:** brownfield migration, since there is no existing billing system (confirm in Missing inputs); AI decisions, since there is no AI component.

## 4. Walking skeleton (Phase 0)
- **The one real request:** An internal "house" tenant authenticates through OIDC and sends `POST /v1/usage-events` with an idempotency key. Rows are written under row-level security. A worker aggregates the usage and builds a draft invoice for a one-line flat-plus-usage plan. Ledger entries are posted. The invoice is finalized and a payment is created at the processor **in test mode** with a test token. The webhook returns, a payment entry is posted, and `GET /v1/invoices/{id}` shows the invoice as paid.
- **Tiers crossed:** Edge/WAF → API → auth → Postgres with RLS → worker → processor sandbox → webhook → API.
- **Deploy:** Terraform and GitHub Actions deploy to the production account, with blue/green on Fargate. Migrations are expand/contract, and the feature flag defaults to off.
- **Logged and monitored:** Structured JSON logs with `trace_id` and `tenant_id`, and no personal data or card data. One OpenTelemetry trace spans every tier. Alarms cover API errors, worker failures, webhook lag, and ledger imbalance. Telemetry that records amounts is classed confidential.
- **Rolled back:** The deploy is rolled back once through the pipeline, including an expand/contract migration step.
- **Who can reach it:** Allow-listed IPs plus an internal OIDC group, behind the flag. No real tenant, and no live-mode processor keys or real money. The payment tier is a disposable sandbox.
- **Also started in Phase 0:** The organizational items in §2. The V1 fit-gap starts here and must finish before Phase 1 specifies the ledger. Volume and currency inputs are collected.

**Exit check (V0):** The request succeeds with one trace across every tier. A deploy and a rollback succeed through the pipeline. An injected failure (worker killed, webhook dropped) fires an alert. Baselines are recorded: ingest p95 at near-zero load, and the time from usage to paid invoice.

## 5. Phases

- **Phase 1: Tenancy, identity, ledger, API contract** (widest blast radius first)
  - Unlocks: Everything else. Schema and API shape are the costliest things to change.
  - Depends on: Phase 0, V1 and V0, and currency and jurisdiction inputs.
  - Tasks:
    1. Tenant model and routing seam, with row-level security as the default on every table.
    2. Auth: service and user principals, and per-tenant scopes.
    3. Ledger schema and posting API (double-entry, idempotent posting).
    4. Versioned OpenAPI contract with idempotency keys and error semantics.
    5. Audit log.
    6. Run the V2 property tests in CI from the first ledger commit, and the V3 cross-tenant suite in CI from the first endpoint.
  - Rollback: Expand/contract migrations and flag off. No tenant data exists yet.
  - Exit check: V2 and the first pass of V3 run green in CI. CI fails on any table without RLS and `tenant_id`.

- **Phase 2: Metering**
  - Unlocks: Rating on trusted usage.
  - Depends on: Phase 1, and the volume input (UNKNOWN, due in Phase 0).
  - Tasks:
    1. Idempotent ingest API with late and out-of-order handling.
    2. Partitioned usage store.
    3. Aggregation jobs.
    4. Replay tool.
    5. Load test (V4).
  - Rollback: Flag off. Usage is replayable from raw events, and the raw events are retained.
  - Exit check: V4 passes.

- **Phase 3: Catalog, rating, subscriptions, invoicing**
  - Unlocks: Correct invoices in shadow.
  - Depends on: V2, V4, and Finance sign-off on rules.
  - Tasks:
    1. Plan catalog (flat, per-seat, tiered, usage).
    2. Rating engine as pure functions, golden-tested.
    3. Subscription lifecycle: upgrade, downgrade, proration, cancel.
    4. Invoice generation, finalization, and credit notes.
    5. Tax-provider integration in test mode.
    6. Bill-run sizing.
  - Rollback: Invoices stay draft until V6. Drafts can be voided and regenerated.
  - Exit check: Golden scenarios (as many as the plan list requires) reproduce the independent recalculation. The bill-run load test meets a Finance-approved duration once the UNKNOWN is resolved.

- **Phase 4: Payments, dunning, reconciliation**
  - Unlocks: Readiness for live money.
  - Depends on: Phase 3, and V5 (started in Phase 0).
  - Tasks:
    1. `PaymentProvider` adapter (hosted fields, webhooks, refunds).
    2. Dunning and retry schedule.
    3. Daily reconciliation of the ledger against processor reports.
    4. Backup restore drill against RPO and RTO.
    5. PAN scan of logs and databases.
  - Rollback: Processor stays in test mode. Live keys are not provisioned until V5 passes.
  - Exit check: V5 passes. Reconciliation runs with zero unexplained differences on test data. The restore drill meets the RPO and RTO ASSUMPTION.

- **Phase 5: Controlled exposure**
  - Unlocks: General availability.
  - Depends on: V2, V3, V5, V6, and the availability SLO input.
  - Tasks, smallest exposure first:
    1. Shadow bill run: compute invoices for internal tenants and pilot tenants, with no charges and no customer emails.
    2. V6 go/no-go.
    3. One pilot tenant goes live, then about 10, then all, each widening only after the previous step's reconciliation is clean.
    4. SLIs and burn-rate alerts against the SLO.
  - Rollback: Per-tenant flag back to shadow. Refund and credit-note procedure for charges made.
  - **Point of no return:** The first customer-visible invoice and real charge. It is guarded by V6.
  - Exit check: V6 passes, and at least one full live cycle reconciles clean per widening step.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed, and rolled back through every tier in production | Skeleton run | The V0 exit check in §4 | Pipeline run logs and the trace export, kept with the release | Phase 1 | 0 |
| V1 | The planned pricing cannot be met by a bought engine at comparable total cost | Fit-gap of the planned plans against Stripe Billing and Lago, with a 3-year cost comparison | Named sign-off by the product owner and Finance on build versus buy. The plan list is UNKNOWN until supplied, so it is needed in Phase 0. | Fit-gap matrix and cost sheet in the repo | Phase 1 build-path; if it fails, replan around the bought engine | Starts and ends in 0 |
| V2 | The ledger cannot lose, duplicate, or misstate money under retries, concurrency, and replays | Property-based tests over randomized postings with duplicate requests, concurrent posts, and replayed webhooks. Then golden-scenario replay against an independent recalculation. | Zero invariant violations (debits = credits per currency; no updates or deletes; idempotent re-posts) over 1,000,000 generated operations, and 100% of golden scenarios match to the minor unit. These are design parameters, not measured baselines. | Test reports stored in CI artifacts | Phase 3 spec and any live money; if it fails, fix the model, and the R3 schema is reopened | Starts in 1, required before 3 |
| V3 | A tenant cannot read or write another tenant's data by any path | Cross-tenant automated suite over every endpoint, job, cache, and export. Then a scan of logs and telemetry for other tenants' identifiers. Then an independent penetration test on the isolation paths. | 0 cross-tenant reads or writes across the suite. A named security reviewer's pass/fail sign-off. | Suite report and the reviewer's sign-off in the security folder | Any non-internal tenant; if it fails, fix and re-test, and the dedicated-storage flip is considered | Starts in 1, required before 5 |
| V4 | Postgres ingest sustains 2× projected peak with duplicates and late events, and replay reproduces identical totals | Load test at 2× peak on 12-month-sized data, long enough to span autovacuum and checkpoints. Then replay from raw events. | Zero lost events, aggregates identical after replay, and p95 < 200 ms (ASSUMPTION). The peak itself is UNKNOWN, so it is a prerequisite input. | Benchmark report stored with the build | Phase 3 sizing; if it fails, add the durable log and analytical store (§3) | 2 |
| V5 | Hosted card entry keeps us at SAQ A and no card number exists in our systems | Data-flow review by the processor or a QSA. Then a PAN scan of logs, databases, and backups. | Written confirmation of SAQ A eligibility and a PAN scan that finds none | Completed SAQ A and scan report | Live-mode keys and live charging; if it fails, re-scope the card flow | Starts in 0, required before 5 |
| V6 | The first real invoices are correct, and we can reverse a mistake | Shadow bill run for a full cycle over internal and pilot tenants, compared against an independent recalculation by Finance. Rehearse refund and credit-note reversal. | 100% of invoices match to the minor unit (ASSUMPTION; rounding rules from Finance). The named Finance approver signs the go/no-go. | Shadow-run comparison report with counts and IDs only (no customer records) | Pilot, then widening; if it fails, stay in shadow and fix | 5 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | OIDC, allow-list, flag off, sandbox-only payments, secrets in a manager, no personal data or card data in logs | Trace, logs, alarms, V0 baselines, telemetry classed confidential | Terraform, pinned images, one-command deploy | Rollback drill, expand/contract migrations |
| 1 | RLS fail-closed, CI guard on tenant columns, scoped API credentials, immutable audit log | Per-tenant metrics, ledger-imbalance alarm | Migrations in CI, schema snapshots | Idempotent posting, PITR enabled |
| 2 | Ingest authentication and per-tenant rate limits | Ingest lag, duplicate, and late-event counters | Raw events retained, replay tool | Retry-safe ingest, backlog alarms |
| 3 | Finalized invoices immutable, plus approval for manual adjustments | Rating traces, bill-run progress, failure counts | Golden fixtures, versioned pricing rules | Idempotent job keys, resumable bill runs |
| 4 | Hosted fields only, webhook signature checks, PAN scan, key rotation | Webhook lag, reconciliation differences | Processor contract tests, pinned API version | Webhook dedupe and replay, restore drill |
| 5 | V3 and V5 verified before exposure, least-privilege operators, access logs | SLIs, burn-rate alerts, on-call runbooks | Release tagged per tenant wave | Per-tenant flag back to shadow, refund and credit-note runbook |

## 8. AI layer
N/A — no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- Dedicated per-tenant databases. Pulled forward by a contract requiring isolation or residency (the §3 tenancy flip).
- Durable log and analytical store for usage. Pulled forward if V4 fails.
- Extracting services from the monolith. Pulled forward if V4 or ownership boundaries demand independent scaling.
- Multi-currency enablement and FX. The schema is ready. Pulled forward when the first non-base-currency tenant signs.
- Revenue recognition and tax filing. Pulled forward when Finance needs them before the first audit.
- Customer self-serve portal beyond hosted invoices. Pulled forward when support volume justifies it.
- Billing on behalf of tenants (marketplace payouts). Pulled forward only if Missing input 1 turns out the other way, and it then needs its own plan.
- Multi-region active-active. Pulled forward if the SLO, once supplied, cannot be met from a single region.
