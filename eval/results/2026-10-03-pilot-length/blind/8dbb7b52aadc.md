# Multi-Tenant SaaS Billing System: Reference Architecture

**Assumption:** This is a billing platform that serves many tenant businesses, each with its own customers, plans and payment setup (similar to Chargebee or Stripe Billing). The same design works if you're only billing your own SaaS customers: you'd have one tenant, and the tenant-isolation sections become simpler.

---

## 1. Goals and Non-Negotiables

| Requirement | Implication |
|---|---|
| **Correctness over availability** for money | Double-entry ledger, idempotency everywhere, exactly-once *effects* |
| **Auditability** | Records can't be changed after the fact; every change is a new event; full history kept |
| **Tenant isolation** | No data leaks between tenants; one tenant's heavy load can't slow down others |
| **Flexible pricing** | Flat, per-seat, tiered, volume, usage-based, hybrid, prepaid credits |
| **Compliance** | PCI-DSS (keep scope small), SOC 2, GDPR, tax, ASC 606/IFRS 15 revenue recognition |
| **Scale** | Billions of usage events/month, millions of invoices at month-end peaks |

---

## 2. High-Level Architecture

```
            ┌──────────────── API Gateway (authN, tenant resolution, rate limits) ───────────────┐
            │                                                                                     │
  Tenant Apps / SDKs        Admin Portal          Customer Self-Serve Portal         Webhooks Out
            │                                                                                     │
  ┌─────────┴────────┬──────────────┬──────────────┬──────────────┬──────────────┬───────────────┐
  │ Catalog Service  │ Subscription │ Usage        │ Rating &     │ Invoicing    │ Payments &    │
  │ (products/plans/ │ Service      │ Ingestion    │ Pricing      │ Service      │ Dunning       │
  │  prices)         │ (lifecycle)  │ & Metering   │ Engine       │              │ Service       │
  └─────────┬────────┴──────┬───────┴──────┬───────┴──────┬───────┴──────┬───────┴───────┬───────┘
            │               │              │              │              │               │
            └───────────── Event Bus (Kafka; partitioned by tenant_id) ──────────────────┘
                     │                │                 │                 │
               Ledger Service    Entitlements      Tax Service       Revenue Recognition
               (double-entry)    Service           (Avalara/Stripe    & Reporting
                                                    Tax adapter)      (warehouse)
```

---

## 3. Core Domain Model

```
Tenant ─┬─ Customer (Account) ─┬─ PaymentMethod (token only)
        │                      ├─ Subscription ─── SubscriptionItem ─── Price
        │                      ├─ Invoice ─── InvoiceLineItem
        │                      ├─ CreditBalance / Wallet
        │                      └─ UsageRecord (aggregated)
        └─ Product ─── Plan ─── Price (versioned, immutable)
```

Key modeling decisions:
- **Prices can't be edited once created.** A "change" creates a new version, and existing subscriptions keep their version until you migrate them on purpose. That way an old invoice can always be recalculated exactly.
- **Store money as integer minor units plus an ISO currency code.** Never use floats. For fractional usage prices, use decimal with fixed precision.
- **Track time as effective dating.** Every subscription change is a dated *phase* (start, end, items). This makes proration, backdating and scheduled changes deterministic.
- **The invoice is a snapshot.** Once finalized, it can't be changed. Corrections are made through credit notes or a new invoice, never by editing.

---

## 4. Multi-Tenancy Strategy

**Hybrid ("pooled with escape hatch") model:**

- **Default (pooled):** Tenants share databases, and every row carries `tenant_id`. PostgreSQL Row-Level Security enforces separation, with `SET app.tenant_id` set per transaction from the authenticated context. The primary key `(tenant_id, id)` keeps each tenant's data together.
- **Enterprise tier (siloed):** A dedicated schema or database cluster per tenant, for data residency (EU/US), contractual isolation, or very large tenants. A **tenant directory service** maps `tenant_id → shard/cluster/region`.
- **Sharding:** Shard by `tenant_id` (e.g., Citus or app-level). If one tenant gets too big, move it to its own shard.
- **Noisy-neighbor controls:**
  - Rate limits and quotas per tenant at the gateway.
  - Separate Kafka partitions or consumer lanes for heavy tenants.
  - Fair-share scheduling for invoice-run jobs.
- **Encryption:**
  - Per-tenant data keys (envelope encryption via KMS) for sensitive fields.
  - "Crypto-shredding" for GDPR deletion: destroy the key and the data becomes unreadable.
- **Per-tenant configuration:**
  - Currencies, tax settings, invoice templates and numbering sequences.
  - Dunning policies, payment gateway credentials (stored in a secrets vault), and webhook endpoints.

---

## 5. Component Design

### 5.1 Catalog Service
Products, plans, prices, add-ons, coupons and discounts. Price models are expressed as a small **pricing DSL**: tiers, packages, minimums, maximums, and included allowances. The service is read-heavy, so prices are cached aggressively and invalidated by version.

### 5.2 Subscription Service
- **States:** `trialing → active → past_due → paused → canceled`.
- **Lifecycle operations:** upgrades, downgrades, quantity changes, pauses, renewals and cancellations. Each operation is a **command** that produces events (`SubscriptionUpdated`, `PhaseStarted`).
- **Proration:** a pure function of (old phase, new phase, effective time, proration policy). The output is pending invoice items.
- **Billing anchors and cycles:** monthly, annual, calendar-aligned or anniversary. A **scheduler** stores `next_billing_at` in an indexed table, and workers claim due rows with `SELECT … FOR UPDATE SKIP LOCKED`.

### 5.3 Usage Ingestion and Metering (the high-volume path)
1. The ingest API accepts events (`tenant_id, customer_id, meter, quantity, timestamp, idempotency_key`).
2. Events are validated and written to Kafka. The API returns 202 once the write is confirmed.
3. A **deduplication** stage (keyed store such as Redis or RocksDB, with a window of about 7+ days) drops duplicate keys.
4. A **stream aggregator** (Flink or Kafka Streams) rolls usage up per customer, per meter and per billing period. It writes:
   - hourly and daily aggregates to OLTP/ClickHouse
   - raw events to object storage (S3/Parquet) for audit and re-rating
5. **Late events:**
   - Accepted until the period closes, plus a grace window.
   - Arriving after the invoice is finalized, they're added to the next invoice as adjustments.
6. Near-real-time aggregates feed **entitlements and usage alerts**, such as "90% of quota used" or hard caps.

### 5.4 Rating and Pricing Engine
A deterministic, side-effect-free function:

`rate(price_version, usage_aggregate, period, discounts) → line_items`

It's stateless and scales horizontally. The same function serves previews, upcoming-invoice estimates and final invoicing. Because it's pure, **re-rating** (e.g., after fixing a meter bug) is simply running it again over stored aggregates.

### 5.5 Invoicing Service
**Invoice states:** `draft → finalized → paid | void | uncollectible`.

At each billing-cycle boundary:
1. Collect the recurring charges, prorations, rated usage, credits and discounts.
2. Calculate tax through the **Tax Service** adapter (address validation, nexus rules, VAT/GST, reverse charge).
3. Create the invoice in **draft**, with an optional review window for enterprise tenants.
4. **Finalize** it: assign a sequential number per tenant (gap-free when legally required), render the PDF, lock it, and emit `InvoiceFinalized`.

**Invoice runs:** split by tenant and shard, with checkpoints so they can be resumed. Spread the month-end peak by using anniversary billing where possible.

### 5.6 Payments and Dunning
- **Gateway layer:** a payment-orchestration abstraction over Stripe, Adyen, Braintree and others, with per-tenant credentials and routing. It supports cards, ACH/SEPA, wallets and wire transfers (reconciled manually).
- **PCI scope:** card data never reaches your servers. Hosted fields or tokenization put you in **SAQ-A**.
- **Idempotency:** every charge attempt has a key derived from `invoice_id + attempt_no`.
- **Gateway webhooks:** verify signatures, store the raw payload, process asynchronously, and expect events out of order.
- **Dunning:**
  - Configurable retry schedules, with smart retries based on decline codes.
  - Email sequences and card-updater integration.
  - A grace period, then an entitlement downgrade or suspension.
- **Reconciliation job:** compares gateway settlement reports with the ledger every day and raises alerts on mismatches.

### 5.7 Ledger Service (source of financial truth)
- **Double-entry and append-only:** each transaction's entries sum to zero.
- **Accounts:** per customer (Accounts Receivable, credit balance, deferred revenue) and per tenant (revenue, tax payable, gateway clearing).
- **What gets posted:** invoices, payments, refunds, credit notes, chargebacks and write-offs all post journal entries. Balances are derived from entries, never stored as a value that gets edited.
- **Revenue recognition:** consumes ledger events to build ASC 606 schedules (spread annual prepaid revenue over the term).

### 5.8 Entitlements Service
Answers "Can customer X use feature Y / consume Z more units?" with low latency (Redis-backed and event-driven). This keeps product feature-gating separate from billing internals.

---

## 6. Consistency and Reliability Patterns

- **Transactional outbox:** each service writes its state change and its outgoing event in one database transaction. A relay then publishes to Kafka, so no event is lost and none is "phantom" (published for a change that never committed).
- **Idempotency keys** on every mutating API call. Responses are stored for 24–72 hours and replayed on retry.
- **Sagas** for multi-step flows (e.g., upgrade → prorate → invoice → charge → grant entitlement), with compensations such as a credit note or refund.
- **Exactly-once effects:** at-least-once delivery plus idempotent consumers (dedupe on event ID per consumer).
- **Optimistic concurrency** (version columns) on subscriptions, to prevent lost updates when changes arrive at the same moment.
- **Clock discipline:** the server decides billing time. Store everything in UTC, and convert to the tenant's or customer's time zone only for cycle-boundary calculations.

---

## 7. Data Stores

| Data | Store |
|---|---|
| Catalog, subscriptions, invoices, ledger | PostgreSQL (sharded, RLS, PITR backups) |
| Raw usage events | Kafka → S3/Parquet (immutable, can be replayed) |
| Usage aggregates / analytics | ClickHouse or Druid |
| Dedupe, entitlements cache, rate limits | Redis |
| PDFs, exports | Object storage with per-tenant prefixes |
| Reporting / BI / revenue recognition | Warehouse (Snowflake/BigQuery) via CDC |

---

## 8. Security and Compliance

- **Authentication:** OAuth2/API keys scoped to a tenant. Tenant context comes from the token, *never* from the request body. Fine-grained RBAC for tenant admins.
- **Secrets:** gateway credentials kept in Vault/KMS, and rotated.
- **Audit:** every admin and API action written to a tamper-evident audit log (hash-chained).
- **Data residency:** run regional cells (EU/US). A tenant is pinned to one region, and global services hold only routing metadata.
- **Testing isolation:** automated tests that attempt cross-tenant access, run in CI.

---

## 9. Observability and Operations

- **Per-tenant metrics:** ingest lag, invoice-run duration, payment success rate, dunning recovery rate and webhook delivery success.
- **Financial invariants run as continuous checks:**
  - Ledger entries sum to zero.
  - Invoice total = sum of lines + tax − discounts.
  - Accounts Receivable matches open invoices.
- **Webhooks out:** signed, retried with exponential backoff, with a dead-letter queue and a replay UI for tenants.
- **Test clocks:** simulated time per sandbox tenant, so tenants (and your own QA) can test renewals, trials and dunning without waiting.
- **Sandbox and live environments** per tenant, completely separate.

---

## 10. Scaling Hotspots and Mitigations

| Hotspot | Mitigation |
|---|---|
| Usage ingest bursts | Kafka buffering, horizontal ingest pods, backpressure via 429 |
| Month-end invoice runs | Anniversary billing, sharded job queues, precomputed usage aggregates |
| Large enterprise tenant | Dedicated shard/cell, dedicated consumer group |
| Gateway outages | Circuit breakers, queue charges for retry, failover routing |
| Re-rating at scale | Batch recompute from the Parquet event log on Spark |

---

## 11. Phased Delivery

1. **MVP:**
   - Catalog, subscriptions (flat/per-seat), invoicing, a single gateway, basic dunning and the ledger.
   - Pooled tenancy with RLS.
2. **Usage-based:**
   - Metering pipeline, rating engine, entitlements, usage alerts.
3. **Enterprise:**
   - Multiple gateways, tax engine, revenue recognition, siloed tenants, regional cells, quotes and contracts.
4. **Advanced:**
   - Prepaid credits/wallets, marketplace payouts, ML-based smart retries, pricing experiments.

**Build vs. buy:** use vendors for tax (Avalara), card processing and possibly revenue recognition. Build the catalog, subscriptions, metering, rating, invoicing and ledger, because that's where you differentiate and where the tenant-specific requirements live.
