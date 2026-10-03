# Multi-Tenant SaaS Billing System Architecture

I had no requirements or existing code to work from, so this design rests on some assumptions. Tell me where they're wrong and I'll adjust it.

**Assumptions:** B2B SaaS. Pricing is hybrid (subscription plus usage-based). You use an external payment processor such as Stripe or Adyen rather than acting as a payment facilitator. You need to support thousands to low millions of tenants, and invoices must be auditable.

---

## 1. Key Design Decisions

| Decision | Choice | Why |
|---|---|---|
| Tenancy model | Shared services, with `tenant_id` on every row and Postgres Row-Level Security. Large tenants can move to dedicated schemas or DBs. | Cheap at scale, with an escape hatch for enterprise customers. |
| Source of truth for money | Append-only double-entry ledger | Auditable, reconcilable, and never mutated. |
| Usage ingestion | Event stream (Kafka) plus idempotent writes and a rollup store | Handles high volume and tolerates retries. |
| Billing runs | Deterministic, replayable, and idempotent per `(subscription, period)` | Safe to re-run after failures. |
| Payment processor | Behind an adapter interface | Avoids lock-in and allows multi-PSP. |
| Pricing | Data-driven catalog (plans, prices, tiers) with versioning | Pricing changes don't require deploys. |
| Money | Integer minor units plus currency code, never floats | Avoids rounding errors. |

---

## 2. High-Level Architecture

```
                    ┌──────────────┐
   Customers ──────▶│  API Gateway │◀────── Admin / Finance UI
   (tenants'        │ AuthN/Z, rate│
    apps & users)   │ limit, tenant│
                    │ resolution   │
                    └──────┬───────┘
      ┌─────────────┬──────┴───────┬────────────────┐
      ▼             ▼              ▼                ▼
┌───────────┐ ┌────────────┐ ┌───────────┐  ┌─────────────┐
│ Catalog & │ │Subscription│ │  Usage    │  │  Customer/  │
│ Pricing   │ │ Service    │ │ Ingestion │  │  Account    │
└─────┬─────┘ └─────┬──────┘ └─────┬─────┘  └─────────────┘
      │             │              │ Kafka: usage.events
      │             │              ▼
      │             │        ┌────────────┐
      │             │        │ Metering & │
      │             │        │ Aggregation│
      │             │        └─────┬──────┘
      ▼             ▼              ▼
   ┌──────────────────────────────────────┐
   │   Billing Engine (rating + invoicing)│◀── Scheduler / Temporal
   └───────┬───────────────┬──────────────┘
           ▼               ▼
     ┌──────────┐   ┌──────────────┐     ┌───────────────┐
     │ Invoice  │   │   Ledger     │────▶│ Tax Adapter   │
     │ Service  │   │ (double-entry)│    │ (Avalara etc.)│
     └────┬─────┘   └──────┬───────┘     └───────────────┘
          ▼                ▼
   ┌────────────┐   ┌──────────────┐     ┌───────────────┐
   │ Payments   │──▶│ PSP Adapter  │────▶│ Stripe/Adyen  │
   │ & Dunning  │   │ + Webhooks   │     └───────────────┘
   └────────────┘   └──────────────┘
          │
          ▼
   Notifications · Reporting/Data Warehouse · Audit Log · Rev-Rec export (ERP)
```

**Two levels of "customer":** The platform's tenants are the companies that use your SaaS. If tenants bill their own end customers (a platform or marketplace model), you need a second level: `tenant → customer → subscription`. The design below supports both. In the simple case, the tenant is the billed entity.

---

## 3. Core Services

### 3.1 Catalog & Pricing
- **Entities:** `Product`, `Plan`, `Price` (versioned, with effective dates), `Meter`, `Coupon`/`Discount`.
- **Price models:** flat, per-seat, tiered, volume, graduated, package, and committed-use with overage.
- Prices are **immutable once used**. Changes create a new version, and existing subscriptions stay pinned to their version until migrated.
- Tenant-specific custom pricing uses `PriceOverride` records, which suits enterprise contracts.

### 3.2 Subscription Service
- State machine: `trialing → active → past_due → suspended → canceled` (plus `paused`).
- Supports upgrades and downgrades with **proration**, add-ons, quantity changes, trials, anniversary vs. calendar billing, and scheduled changes.
- Emits domain events (`subscription.changed`, and so on) through a **transactional outbox**, so state and events never diverge.

### 3.3 Usage Ingestion & Metering
- `POST /v1/usage` takes `{tenant_id, customer_id, meter, quantity, timestamp, idempotency_key, dimensions}`.
- Events are validated, deduplicated on `(tenant_id, idempotency_key)`, and published to Kafka, partitioned by `tenant_id`.
- An aggregator (Flink or a Kafka Streams consumer) writes per-period rollups into ClickHouse or TimescaleDB. Raw events go to object storage (Parquet) for replay and dispute resolution.
- **Late events:** a watermark and grace window, plus a adjustment path. After the invoice is finalized, late usage goes onto the next invoice as a line item or a credit note.
- Real-time usage queries and **spend alerts and caps** read from the rollups.

### 3.4 Billing Engine
- A scheduler (Temporal workflows are a good fit) triggers `BillRun(subscription_id, period)`.
- Steps:
  1. Lock the subscription period (idempotency key = `sub_id:period_start`).
  2. Gather recurring charges, usage rollups, proration adjustments, credits, and discounts.
  3. **Rate** the usage against the pinned price version.
  4. Calculate tax through the tax adapter.
  5. Create a **draft invoice**, then finalize it, which makes it immutable.
  6. Post ledger entries and emit `invoice.finalized`.
- Runs are sharded by tenant to avoid noisy neighbors. Large bill-run days (the 1st of the month) are spread with jitter and per-tenant concurrency limits.

### 3.5 Invoice Service
- Invoices and line items are immutable after finalization.
- Corrections go through **credit notes** or adjustment invoices, never edits.
- Sequential, gap-free invoice numbers **per tenant and legal entity**, which many jurisdictions require. Allocate them at finalization, from a per-tenant counter row inside the transaction.
- PDF rendering is asynchronous, with per-tenant branding and locale, stored in S3 with signed URLs.

### 3.6 Ledger
- Double-entry, append-only: `LedgerEntry(id, tenant_id, account, debit/credit, amount, currency, ref_type, ref_id, created_at)`.
- Accounts include Accounts Receivable, Revenue, Deferred Revenue, Tax Payable, Cash, Credits/Wallet, and Refunds.
- Every money-moving event (invoice, payment, refund, credit, write-off) produces balanced entries in a single transaction.
- Customer balances are derived from the ledger. Projections can be cached.
- Feeds revenue recognition (ASC 606/IFRS 15) and ERP export.

### 3.7 Payments & Dunning
- **PSP adapter** interface: `createCustomer`, `attachPaymentMethod`, `charge`, `refund`, `handleWebhook`.
- Store only PSP tokens, never card data. This keeps you at PCI SAQ-A.
- **Webhooks:** verify the signature, store the raw event, process it idempotently and asynchronously, and tolerate out-of-order delivery by reconciling against the PSP API.
- **Dunning:** a configurable retry schedule per tenant (for example days 1, 3, 5, 7), notifications, grace periods, and then suspend or cancel. Smart retries should consider the decline code.
- Supports ACH/SEPA, invoice-by-wire (manual reconciliation), and prepaid credits or wallets.

### 3.8 Tax & Compliance
- Tax adapter (Avalara, TaxJar, or Stripe Tax) with VAT ID validation and reverse charge for EU B2B.
- Store the tax jurisdiction and rate snapshot on each invoice line.
- Multi-currency: price in a currency and settle in that currency. Store the FX rate snapshot if you report in another.

---

## 4. Data Model (core tables, abridged)

```
tenant(id, name, billing_entity, default_currency, settings_json, status)
customer(id, tenant_id, external_ref, email, tax_id, psp_customer_id, currency)
product / plan / price(id, tenant_id NULL=global, version, model, currency,
                       tiers_json, effective_from, effective_to)
meter(id, tenant_id, key, aggregation[sum|max|unique], unit)
subscription(id, tenant_id, customer_id, status, current_period_start/end,
             billing_anchor, pinned_price_versions)
subscription_item(id, subscription_id, price_id, quantity)
usage_event(tenant_id, idempotency_key, meter_id, customer_id, qty, ts)  -- UNIQUE(tenant_id, idempotency_key)
usage_rollup(tenant_id, customer_id, meter_id, period, qty)
invoice(id, tenant_id, customer_id, number, status, period, subtotal, tax, total,
        currency, finalized_at)
invoice_line(id, invoice_id, type, description, qty, unit_price, amount, tax_detail)
credit_note(...)
payment(id, tenant_id, invoice_id, psp_ref, status, amount, attempt_no)
ledger_entry(...)            -- append-only
outbox(id, tenant_id, type, payload, published_at)
audit_log(actor, tenant_id, action, before, after, ts)
```

**Partitioning:** `usage_event`, `ledger_entry`, and `audit_log` are partitioned by time, with the tenant in the index prefix. Consider hash-sharding by `tenant_id` when a single Postgres primary is no longer enough (Citus is an option).

---

## 5. Multi-Tenancy Concerns

- **Isolation:**
  - Resolve the tenant from the token at the gateway and set `SET app.tenant_id` on each DB session. RLS policies enforce `tenant_id = current_setting('app.tenant_id')`.
  - Treat RLS as a backstop. Application-layer checks and automated cross-tenant tests are also required.
- **Noisy neighbors:** per-tenant rate limits, Kafka quotas, per-tenant concurrency on bill runs, and fair-queue scheduling.
- **Tiered tenancy:** the default pool is shared. Premium tenants get dedicated DB and Kafka partitions, routed through a tenant-directory service.
- **Per-tenant configuration:** currency, tax settings, invoice templates, dunning policy, payment gateway credentials (encrypted, KMS envelope encryption), and custom domains.
- **Encryption and residency:** per-tenant data keys where needed, and regional deployments (EU/US) for data residency, with the tenant directory pinning each tenant to a region.
- **RBAC:** roles such as owner, finance, developer, and read-only. Audit all admin actions. Support impersonation for support staff, with logging.

---

## 6. Correctness & Reliability Patterns

1. **Idempotency everywhere:** API `Idempotency-Key` headers, usage dedupe, bill-run keys, and PSP charge keys derived from `invoice_id:attempt`.
2. **Transactional outbox and inbox:** this gives effectively-once processing on top of at-least-once delivery.
3. **Sagas via Temporal:** the sequence invoice → charge → ledger → notify is durable, with compensations.
4. **Reconciliation jobs (daily):**
   - Ledger vs. PSP settlements/payouts.
   - Usage events vs. rollups.
   - Invoice totals vs. the sum of lines.
   - Alert on any drift.
5. **Immutability:** finalized invoices, ledger entries, and raw usage are never updated. Corrections are new records.
6. **Backfill and replay:** because raw usage is retained and pricing is versioned, you can **re-rate** any period in a sandbox and diff it against production before fixing billing bugs.
7. **Time handling:** store UTC and record the tenant's billing timezone. Define period boundaries explicitly, and test month-end, leap-day, and DST cases.

---

## 7. APIs & Integration

- **REST + webhooks** for tenants: `/v1/customers`, `/subscriptions`, `/usage`, `/invoices`, `/credit-notes`, `/payment-methods`, `/portal-sessions`.
- **Outbound webhooks:** `invoice.finalized`, `payment.failed`, `subscription.canceled`, and so on. Signed, retried with exponential backoff, and with a replay UI.
- **Customer portal:** update payment method, view invoices and usage, and change plan.
- **Exports:** ERP (NetSuite, QuickBooks), the data warehouse (CDC to Snowflake or BigQuery), and tax reports.

---

## 8. Tech Stack (one reasonable option)

| Concern | Choice |
|---|---|
| Services | Go, Java/Kotlin, or TypeScript (NestJS) on Kubernetes |
| OLTP | PostgreSQL (RLS, partitioning) |
| Events | Kafka (or Redpanda) |
| Usage analytics | ClickHouse |
| Workflows | Temporal |
| Cache / limits | Redis |
| Object storage | S3 (invoices, raw usage) |
| Secrets/keys | Vault or KMS |
| Observability | OpenTelemetry, Prometheus/Grafana, with `tenant_id` as a trace attribute |

**Build vs. buy:** if your pricing is simple, Stripe Billing, Chargebee, or Lago (open source) can replace much of 3.1–3.5. Build custom when you need complex usage rating, enterprise contracts, or control over the ledger. A hybrid is common: keep your own metering, rating, and ledger, and use the PSP only for payment collection.

---

## 9. Security & Compliance

- PCI: tokenize through the PSP, so card data never touches your systems.
- SOC 2 controls: audit logs, change management, least-privilege access, and encryption at rest and in transit.
- GDPR: PII is isolated in the customer tables, with a deletion/anonymization path that preserves financial records (retained for the legal period, typically 7 years).
- Webhook signature verification, API key hashing, and scoped keys.

---

## 10. Phased Rollout

1. **MVP:** catalog, flat and per-seat subscriptions, Stripe adapter, invoices, basic ledger, webhooks, RLS.
2. **Usage billing:** metering pipeline, rollups, tiered and graduated pricing, spend alerts.
3. **Finance-grade:** credit notes, tax engine, dunning, reconciliation, ERP export, revenue recognition.
4. **Enterprise:** custom contracts and overrides, committed-use drawdown, multi-entity, regional cells, dedicated-tenant tiers.

---

## 11. Open Questions That Would Change the Design

1. Do tenants bill *their own* customers (platform model), or do you only bill tenants?
2. What's the expected usage-event volume (events per second), and do you need real-time balances or caps?
3. Which regions and currencies, and what tax complexity (US sales tax vs. EU VAT)?
4. Do you need invoicing by wire or PO for enterprise customers, or only card payments?
5. Is revenue recognition in scope, or will finance handle it in an ERP?
6. Build vs. buy preference and team size?

I can go deeper on any part: the SQL schema and RLS policies, the usage-aggregation pipeline, the bill-run workflow, proration math, or an OpenAPI spec for the public API.
