# Multi-Tenant SaaS Billing System: Architecture

## 0. Scope and assumptions

"Multi-tenant billing" can mean two things:

- **(A) Billing as a platform.** You're building something like Stripe Billing, Chargebee or Orb. Your tenants are businesses, and they use the platform to bill *their* customers.
- **(B) A billing subsystem inside your own SaaS.** Your tenants are your customers, and you bill them.

This design targets **(A)** because it covers (B) as well. For (B), treat yourself as the only tenant and set the tenant's customers equal to your tenants. In case (B), also look hard at buying (Stripe Billing, Orb, Metronome, Chargebee) and building only entitlements and metering. Billing usually looks easy at first and turns out to be much harder.

**Requirements this design must meet:**

- Pricing models: flat recurring, per-seat, usage-based (metered), tiered or volume pricing, hybrid plans, prepaid credits and minimum commitments.
- Lifecycle: trials, upgrades and downgrades with proration, pauses, cancellations and plan versioning (grandfathering).
- Strong correctness: no double charges, no lost usage, invoices that can't be changed after finalization, and a full audit trail.
- Tenant isolation, per-tenant configuration (currency, tax, branding, dunning rules) and protection against noisy neighbors.
- Scale target: about 10k tenants, about 50M end customers, and peaks of about 1M usage events per second.

---

## 1. High-level architecture

```
                    ┌──────────────────────── Edge ────────────────────────┐
 Tenant apps ─────► │ API Gateway (authN, tenant resolution, rate limits) │
 Tenant dashboards  └───┬──────────────┬──────────────┬──────────────┬─────┘
                        │              │              │              │
              ┌─────────▼───┐  ┌───────▼──────┐ ┌─────▼──────┐ ┌─────▼───────┐
              │ Catalog Svc │  │ Subscription │ │  Usage     │ │ Customer /  │
              │ products,   │  │ Svc (state   │ │  Ingest    │ │ Account Svc │
              │ plans,price │  │ machine)     │ │  (high TPS)│ │             │
              └─────────────┘  └──────┬───────┘ └─────┬──────┘ └─────────────┘
                                      │               │ Kafka (partition: tenant+customer)
                                      │        ┌──────▼────────┐
                                      │        │ Metering /    │──► Raw event lake (S3/Iceberg)
                                      │        │ Aggregation   │──► Aggregates (ClickHouse/Pinot)
                                      │        └──────┬────────┘
                              ┌───────▼───────────────▼────────┐
                              │ Billing Orchestrator (Temporal)│  one durable workflow
                              │  per subscription billing cycle│
                              └───────┬───────────────┬────────┘
                         ┌────────────▼──┐      ┌─────▼────────┐     ┌──────────────┐
                         │ Rating Engine │      │ Tax Adapter  │────►│ Avalara/Anrok│
                         │ (pure fn)     │      └──────────────┘     └──────────────┘
                         └──────┬────────┘
                         ┌──────▼────────┐      ┌──────────────┐     ┌──────────────┐
                         │ Invoice Svc   │─────►│ Payments Svc │────►│ PSPs: Stripe,│
                         │ (immutable)   │      │ + Dunning    │◄────│ Adyen (hooks)│
                         └──────┬────────┘      └──────┬───────┘     └──────────────┘
                                │                      │
                         ┌──────▼──────────────────────▼───────┐
                         │      Ledger (double-entry)          │──► RevRec ──► GL export
                         └──────────────────┬──────────────────┘     (NetSuite, etc.)
                                            │ Transactional outbox
                         ┌──────────────────▼──────────────────┐
                         │ Event Bus ─► Webhooks to tenants,   │
                         │ Entitlements Svc, Notifications, BI │
                         └─────────────────────────────────────┘
```

**Guiding principles:**

1. **The ledger is the source of truth for money.** All other money data is either derived from it or a document that points to ledger entries.
2. **Finalized financial documents never change.** Corrections are made with credit notes and adjustments, never by updating rows.
3. **Rating is a pure function:** `(subscription snapshot, price version, usage aggregates, period) → line items`. This makes it deterministic, replayable and easy to test.
4. **Every mutating operation is idempotent,** keyed by `(tenant_id, idempotency_key)`.
5. **Time is an explicit input,** not `now()`. This makes test clocks, backfills and re-rating possible.

---

## 2. Multi-tenancy model

| Concern | Decision |
|---|---|
| **Data isolation** | Shared database and shared schema, with `tenant_id` on every row and Postgres **Row-Level Security** (`SET app.tenant_id` per transaction). The tenant ID comes from the auth token at the gateway and is never taken from the request body. |
| **Scale-out** | **Cell-based architecture.** Each cell is a full stack (DB cluster, Kafka, workers) serving a subset of tenants. A global **tenant directory** maps `tenant_id → cell`. Large or regulated tenants get dedicated cells, using the same code with different placement. |
| **Data residency** | Cells are pinned to regions (EU and US). The tenant chooses a region at signup, and it's stored in the directory. |
| **Noisy neighbors** | Per-tenant rate limits and quotas at the gateway. Fair-share scheduling for billing work: a weighted queue per tenant, so one tenant with 5M subscriptions renewing on the 1st can't starve others. Usage ingestion has per-tenant Kafka quotas. |
| **Encryption** | Envelope encryption with a **per-tenant data key** in KMS for PII fields. Crypto-shredding handles GDPR erasure. |
| **Per-tenant config** | Versioned config: currencies, timezone, invoice numbering scheme, tax provider, PSP credentials (stored in a vault), dunning policy, branding, webhook endpoints. |
| **Tenant-of-tenant** | Two levels: `tenant` (the business) and `customer` (who gets billed). Hierarchies such as parent/child accounts and consolidated billing are modeled on `customer`. |

Why not database-per-tenant by default? At 10k tenants it makes migrations, connection pooling and cross-tenant operations painful. Cells give you most of the isolation benefit and can be adjusted per tenant.

---

## 3. Domain model (core entities)

```
Tenant 1─* Customer 1─* Subscription 1─* SubscriptionItem *─1 PriceVersion *─1 Price *─1 Product
                │                │
                │                └─* SubscriptionPhase (scheduled changes, ramps)
                ├─* PaymentMethod (PSP token only)
                ├─* CreditGrant / Wallet (prepaid credits, commitments)
                └─* Invoice 1─* InvoiceLineItem ──► LedgerTransaction 1─* LedgerEntry
                         └─* Payment / Refund / CreditNote
Meter 1─* UsageEvent (raw) ──► UsageAggregate (per customer, meter, window)
```

The decisions that matter most:

**Money.** Store amounts as `BIGINT` minor units plus an ISO currency code. Store usage quantities and unit prices as `NUMERIC(38,12)`, because sub-cent pricing like $0.0000025 per token is common. Round **once**, at the line-item level, using a rounding mode configured per tenant. Never use floats.

**Price versioning.** A `Price` is a stable identity. A `PriceVersion` is immutable: amount, tiers, billing interval, meter, effective dates. Subscriptions point to a specific version. "Raise prices for new customers only" means publishing a new version. "Migrate existing customers" is an explicit, scheduled bulk operation that leaves an audit record.

**Subscription state machine:**
```
incomplete → trialing → active ⇄ past_due → (unpaid | canceled)
                          ↓  ↑
                        paused
```
Plan changes are stored as **SubscriptionPhases** with effective timestamps. Nothing is overwritten. As a result, the subscription's state "as of time T" can always be reconstructed, which rating, proration and audits all depend on.

**Ledger (double-entry):**
```sql
ledger_entry(
  id, tenant_id, txn_id, account_id,     -- e.g. customer:123:receivable, tenant:revenue:deferred
  direction CHAR(1) CHECK (direction IN ('D','C')),
  amount BIGINT CHECK (amount > 0), currency CHAR(3),
  effective_at TIMESTAMPTZ, created_at TIMESTAMPTZ,
  source_type, source_id                  -- invoice, payment, credit_note, refund...
)
-- invariant: SUM(debits) = SUM(credits) per txn_id, enforced at commit.
-- Append-only: no UPDATE or DELETE grants on this table.
```
Typical accounts per customer: `accounts_receivable`, `credit_balance`, `prepaid_wallet`. Per tenant: `deferred_revenue`, `recognized_revenue`, `tax_payable`, `psp_clearing`. Balances come from entries, with snapshotted rollups for speed.

---

## 4. Key flows

### 4.1 Usage metering (the high-throughput path)

1. **Ingest API.** Accepts `POST /usage` in batches. Each event has `{event_id, customer_ref, meter, quantity, timestamp, properties}`. The API validates the schema, checks that the meter exists (from a cached catalog) and writes to Kafka **before** it acknowledges. The partition key is `tenant_id:customer_id`, which keeps each customer's events in order.
2. **Dedup.** Each `(tenant_id, event_id)` is checked against a dedup store: a RocksDB state store in Flink with a TTL equal to the late-arrival window plus a margin. This gives exactly-once *effect* even though delivery is at-least-once.
3. **Two sinks:**
   - **Raw lake** (S3 + Iceberg). An immutable record of every event, used for audits, disputes and re-rating.
   - **Aggregator** (Flink to ClickHouse). Rolls up `SUM`, `MAX`, `COUNT DISTINCT` and `LAST` per `(customer, meter, hour)`. Aggregation types are defined on the Meter.
4. **Late events.** Events are accepted for a configurable grace window after period end (for example 24 to 72 hours) before the invoice is finalized. Events that arrive after finalization go onto the **next** invoice as a dated adjustment line. The finalized invoice is never touched.
5. **Real-time balance.** The same stream updates a running "current period usage" in Redis per customer. This feeds spend alerts, prepaid credit burn-down and hard limits enforced through Entitlements.

### 4.2 Billing cycle (orchestration)

Each subscription runs a **durable workflow** (Temporal, or a DB-backed job table with leases):

```
loop:
  sleep_until(period_end + grace_window)          # durable timer, no cron stampede
  snapshot = subscription_state_as_of(period)     # phases, seats, price versions
  usage    = aggregates(customer, meters, period) # pinned read, recorded on the invoice
  lines    = rate(snapshot, usage, period)        # pure, deterministic
  apply credits / commitments / discounts / minimums
  tax      = tax_adapter.calculate(lines, customer_address)   # result stored on the invoice
  invoice  = create_draft → (optional tenant review window) → finalize
  ledger.post(invoice)                            # AR debit / deferred revenue + tax credit
  outbox.emit(invoice.finalized)
  payments.collect(invoice) if auto_charge
  advance period
```

- **Durable timers instead of a global cron.** "Bill everyone on the 1st" turns into millions of independent timers. They feed a fair-share queue per tenant that workers drain at a controlled rate.
- **Idempotency.** Invoice creation is keyed by `(subscription_id, period_start, period_end)` with a unique constraint, so a retried workflow can't double-bill.
- **Draft window.** Tenants can optionally hold invoices as drafts for N hours to review or add one-off charges before finalization.

### 4.3 Proration and mid-cycle changes

- A change creates a new SubscriptionPhase at time `T`.
- Proration math: `credit(old_price × remaining_fraction) + charge(new_price × remaining_fraction)`. The fraction uses **seconds**, or days if the tenant configures that. Rounding rules are explicit.
- Policy is configurable per tenant and per change: `prorate_now` (invoice immediately), `prorate_next_invoice` or `no_proration`. Downgrades often apply at period end.
- Usage-based items aren't prorated. They're rated by usage within each phase's time window.

### 4.4 Payments and dunning

- **PCI scope.** Card data never touches your servers. Use PSP-hosted fields or Elements so you stay at SAQ-A, and store only PSP tokens. This works per tenant through a PSP-agnostic `PaymentProvider` interface. Tenants can bring their own Stripe or Adyen account (Connect-style), or you act as the merchant of record. That is a large business and legal decision.
- **Payment intent state machine:** `created → processing → succeeded | failed | requires_action (3DS)`. Every PSP call carries an idempotency key derived from `(invoice_id, attempt_no)`.
- **Webhooks from the PSP are the authority.** Store raw payloads, dedupe by PSP event ID and process asynchronously. A **nightly reconciliation job** compares ledger and payments against PSP settlement reports and raises alerts on any mismatch.
- **Dunning.** A per-tenant policy, also run as a workflow. Retries are scheduled sensibly (for example days 1, 3, 5 and 7, avoiding weekends and timed to likely paydays), with network-aware retry codes, card-updater support and escalating email sequences. A grace period comes before entitlements are downgraded. The final action is `cancel` or `mark_uncollectible`, and both post ledger entries for bad debt.
- **Other payment methods:** ACH/SEPA (slow settlement, with return windows modeled), bank transfer with reconciliation by virtual account number, and manual "net 30" invoicing for enterprise customers.

### 4.5 Credit notes, refunds and corrections

- A finalized invoice can be **voided** only if it's unpaid and the jurisdiction allows it. Otherwise it's corrected with a **credit note** that references specific lines.
- A refund is a Payment-reversal record plus ledger entries plus a PSP call. Partial refunds are supported.
- **Re-rating.** If a pricing bug is found, re-run the pure rating function with the corrected inputs, diff the result against what was issued and generate credit notes or adjustment invoices for the deltas, after human approval.

---

## 5. Cross-cutting concerns

**Tax.** Use an adapter interface with providers such as Avalara, Anrok, Stripe Tax, or a simple internal rate table for low-complexity tenants. Store the **full tax calculation response** on the invoice so it can be reproduced. Handle VAT/GST reverse charge, tax-exempt customers (with certificates stored) and e-invoicing mandates (Peppol, Mexico's CFDI, India's GST). These e-invoicing mandates are a growing requirement, so isolate them behind an "invoice delivery" plugin.

**Invoice numbering.** Many jurisdictions require gapless sequences per tenant, and sometimes per legal entity or series. Use a per-tenant sequence row locked at finalization time, not a DB sequence, because DB sequences leave gaps. Drafts get numbers only when they're finalized.

**Revenue recognition.** Turn ledger postings into ASC 606 / IFRS 15 schedules: ratable for subscriptions, point-in-time for usage. Move amounts from deferred to recognized revenue on a schedule, and export journal entries to the tenant's GL (NetSuite, QuickBooks, Xero).

**Entitlements.** Keep this as a separate service. It answers "can customer X use feature Y, and how much is left?" from billing events plus plan definitions. Product code calls it, with local caching and short TTLs, and **never** queries invoices. This decouples product availability from billing outages.

**Outbound webhooks to tenants.** Use a transactional outbox, so the event is written in the same database transaction as the state change. A relay publishes to the bus, and a delivery service sends HMAC-signed payloads with exponential backoff retries for up to about 3 days. Tenants get a replay UI. Event types include `invoice.finalized`, `payment.failed`, `subscription.updated` and similar.

**Audit.** Keep an append-only audit log of who changed what and when, covering both API and dashboard actions, including tenant staff and your support staff. Support access to tenant data goes through a time-boxed impersonation flow that is itself audited.

**Security and compliance.** SOC 2 and PCI SAQ-A to start. Use least-privilege service identities, RLS as defense in depth beneath application-level tenant checks, secrets in a vault, PII minimization, and retention policies (financial records are often kept 7 to 10 years, which conflicts with GDPR erasure; resolve that by pseudonymizing).

---

## 6. Storage choices

| Data | Store | Why |
|---|---|---|
| Catalog, customers, subscriptions, invoices, ledger | **PostgreSQL** per cell (or CockroachDB/Spanner if you need multi-region writes) | ACID, constraints, RLS, a mature tool for money |
| Usage stream | **Kafka** | Ordered per partition, replayable, back-pressure |
| Usage aggregates | **ClickHouse** (or Pinot/Druid) | Fast rollups over billions of rows |
| Raw usage archive | **S3 + Iceberg** | Cheap and immutable, supports re-rating and disputes |
| Real-time counters, caches | **Redis** | Spend limits, entitlement caching |
| Workflows | **Temporal** | Durable timers, retries, visibility into each billing cycle |
| Invoice PDFs | Object storage, rendered asynchronously | Generated from the finalized invoice and hashed for integrity |

---

## 7. Reliability and failure modes

| Failure | Mitigation |
|---|---|
| Duplicate usage events | Dedup on `(tenant, event_id)` within the window, and an idempotent aggregation design |
| Worker crashes mid-invoice | Workflow resumes, and a unique key on `(subscription, period)` prevents duplicates |
| PSP timeout ("did it charge?") | Idempotency key on retry. Query the PSP before re-attempting. Webhooks plus reconciliation settle the truth |
| Tax provider down | Retry with backoff, then hold the invoice in draft. Never finalize with guessed tax unless the tenant opts into a fallback |
| Load spike at month start | Durable timers, fair-share queues per tenant, autoscaled workers, plus a nudge to tenants to use anniversary billing |
| Kafka or aggregator lag | Billing waits on an aggregator **watermark** for the period. An invoice isn't finalized until the watermark passes `period_end + grace` |
| Bad price config | Price versions are validated at publish time, with a preview invoice in the dashboard and shadow rating of the new version before activation |
| Cross-tenant data leak | RLS plus tenant-scoped repositories in code, plus automated tests that try cross-tenant access on every endpoint |

**Invariants to monitor continuously, with pages on violation:**

- Ledger debits equal credits.
- `invoice.total = Σ line items + tax − credits`.
- Customer AR balance equals open invoices minus unapplied payments.
- Every `succeeded` PSP charge maps to exactly one ledger payment.

---

## 8. Testing strategy

- **Test clocks.** Tenants and your QA team create a sandbox clock and advance time to simulate a year of renewals, trials and dunning in minutes. This works because time is an explicit input.
- **Property-based tests** on the rating and proration functions. Examples: proration credit plus charge for a full period equals the full price, and a change followed by its reversal nets to zero.
- **Golden invoice fixtures** for every pricing model, compared in CI.
- **Shadow billing** for migrations: run the new engine alongside the old one and diff every invoice before cutover.
- **Chaos tests** that kill workers mid-workflow and inject PSP timeouts, then assert the invariants still hold.

---

## 9. API surface (sketch)

```
POST   /v1/customers
POST   /v1/products, /v1/prices            (create a new PriceVersion; immutable)
POST   /v1/subscriptions                   {customer, items[], trial_end, billing_anchor}
POST   /v1/subscriptions/{id}/changes      {items[], effective_at, proration_behavior}
POST   /v1/usage                           batch, Idempotency-Key, returns 202
GET    /v1/customers/{id}/upcoming_invoice preview (rates without persisting)
POST   /v1/invoices/{id}/finalize | /void | /pay
POST   /v1/credit_notes
GET    /v1/customers/{id}/balance
Webhooks: subscription.*, invoice.*, payment.*, usage.threshold_reached
```
Every write takes an `Idempotency-Key` header. Store each response for 24 hours or more, keyed by `(tenant, key)`, and replay it on retry.

---

## 10. Phased delivery

1. **MVP (one cell, about 3 to 4 months, small team):** catalog with versioning, flat and per-seat subscriptions, the invoice state machine, the ledger, a single PSP (Stripe), basic dunning, outbox webhooks, RLS isolation, test clocks.
2. **Usage-based billing:** the Kafka/Flink/ClickHouse metering pipeline, tiered and volume rating, prepaid credits, spend alerts, an entitlements service.
3. **Enterprise:** commitments and minimums, ramp deals, consolidated or parent billing, manual invoicing and ACH, a tax provider integration, revenue recognition and GL export.
4. **Scale and global:** multiple cells, regional residency, dedicated cells, multiple PSPs, e-invoicing mandates, self-serve re-rating tools.

---

### Decisions to make early (they're expensive to change later)

1. **Merchant of record or tenant-owned PSP accounts.** This affects legal exposure, tax liability and money flows.
2. **Ledger-first design.** Retrofitting double-entry later is very painful.
3. **Immutable price versions and subscription phases.** Without them you can't reliably reproduce past invoices.
4. **Integer minor units with explicit rounding at line level,** and high-precision decimals for unit prices.
5. **Cell-based tenancy from day one,** even with a single cell, so `tenant → cell` routing already exists when you need it.

To tailor this further, the most useful details would be: which of (A) or (B) you're building, your main pricing model (seats or usage), your expected event volume, and whether you need to be merchant of record. Some of those answers could remove large parts of this design, such as the metering pipeline or the cells.
