> **P2** · **without the skill** (cold: plain `claude --bare -p`, no skill, empty directory)  
> Prompt: *"Architect a multi-tenant SaaS billing system."*  
> Generated 2026-10-02 with `claude-opus-5-5` (effort max) in an isolated session. One run per arm, not cherry-picked.  
> Everything below the line is the model's output, verbatim. Judged score: [SCORECARD.md](../SCORECARD.md).

---

# Multi-Tenant SaaS Billing System: Reference Architecture

I didn't find a spec in the workspace, so this design is built on the assumptions in §1. If your numbers are different, the sizing changes but the structure still works.

**Summary:** A **billing core** keeps all financial data. It's one deployable app with separate internal modules, running on Postgres and sharded by customer. A separate, high-volume **metering pipeline** handles usage. The product only ever talks to a cached **entitlements** layer. **Payments and tax** are bought from providers and wrapped in adapters. Correctness comes from four things: finalized invoices never change, every step can safely run twice, a double-entry ledger, and constant reconciliation against providers.

## 1. Assumptions & requirements

- **Who pays:** a B2B SaaS that bills its tenants (orgs/workspaces). If your tenants are instead merchants billing their own customers (billing-as-a-service), see §12.
- **Pricing:** a mix of platform fee, seats, usage, and prepaid credits or commitments. Plus trials, coupons, and negotiated enterprise contracts whose price steps up over time.
- **Scale:** 50k growing to 500k customers · ~2B usage events/day (100k/s at peak) · ~1M invoices/month.
- **Money:** multiple currencies and selling entities (e.g., US Inc. + EU B.V.). US sales tax, EU VAT, GST.
- **Collection:** self-serve (card, direct debit) and sales-led (net-30 invoices paid by wire/ACH).

| Quality | Target |
|---|---|
| Invoice correctness | Every line can be recomputed from its inputs; double-billing is impossible by design |
| Usage ingest | 99.99% available, nothing lost once accepted, each event counted exactly once |
| Entitlement checks | Microseconds locally, p99 < 10 ms remotely. If billing is down, the product keeps working on cached data |
| Billing core | Strongly consistent; no data loss on failover, back up within 1 h |
| Tenant isolation | Enforced by the database, not by developers remembering filters |
| Audit | Full history that can't be edited, kept 7–10 years |

## 2. Design principles

1. **No floats, ever.** Store amounts as integers in the smallest currency unit (cents), plus an ISO-4217 currency code. Currencies differ: JPY has 0 decimals, USD 2, KWD 3. Unit prices are `NUMERIC(38,12)`, because $0.000002/token is a real price. Round once per line, using a documented rounding rule.
2. **Financial records are append-only.** Finalized invoices and ledger entries are never edited or deleted. Mistakes are fixed with new documents: credit notes and reversing entries.
3. **Every operation is safe to repeat.** That covers API writes, usage events, scheduled jobs, payment-provider calls, and webhook handlers.
4. **Prices are versioned, and each subscription is tied to a version.** Changing a price creates a new version. Existing customers keep their old price until you move them on purpose.
5. **Pricing is calculated by a pure function** (no side effects): `rate(usage, price_versions, contract, credits, period) → lines`. The same code produces invoices, previews of the next invoice, quotes, re-pricing, and pricing tests against past usage.
6. **The product never waits on billing for a live request.** Entitlements are pushed to the product and cached there.
7. **The tenant ID comes first** in every primary key, partition key, cache key, and log line.
8. **Reconcile constantly.** Your payment provider, tax engine, and even your own stream processor will disagree with you at some point.

## 3. High-level architecture

```
┌─────────────────┐  ┌──────────────────┐  ┌───────────────────────┐
│ Product services│  │ Customer portal  │  │ Finance console / CPQ │
└──┬───────────▲──┘  └────────┬─────────┘  └───────────┬───────────┘
   │ usage     │ entitlements │                        │
   │ events    │ (cached)     │                        │
┌──▼───────────┴──────────────▼────────────────────────▼───────────┐
│ API gateway: authN · tenant resolution · per-tenant rate limits  │
│ idempotency keys · request audit log                             │
└──┬──────────────────────────┬────────────────────────────────────┘
   │                          │
┌──▼───────────────────┐  ┌───▼────────────────────────────────────┐
│ METERING             │  │ BILLING CORE (modular monolith)        │
│ ingest API → Kafka   │  │ Postgres, sharded by customer_id       │
│ Flink: dedup,        │  │                                        │
│  attribute, aggregate│─►│ Catalog · Accounts · Subscriptions     │
│ → ClickHouse + S3    │  │ Rating · Invoicing · Credits · Ledger  │
└──┬───────────────────┘  │ Dunning · transactional outbox         │
   │                      └─────┬─────────────┬───────────────┬────┘
   │ live usage                 │             │               │ events
   │                     ┌──────▼──────┐ ┌────▼───────┐       │
   │                     │ PAYMENTS    │ │ TAX        │       │
   │                     │ adapter →   │ │ adapter →  │       │
   │                     │ Stripe,     │ │ Avalara,   │       │
   │                     │ Adyen, etc. │ │ Stripe Tax │       │
   │                     └─────────────┘ └────────────┘       │
┌──▼──────────────────────────────────────────────────────────▼────┐
│ ENTITLEMENTS  Redis + SDK caches · p99 < 10 ms · fail-open       │
│ NOTIFICATIONS email · outbound webhooks · invoice PDFs           │
│ ANALYTICS     CDC → warehouse (MRR, AR aging) → ERP/GL · rev-rec │
└──────────────────────────────────────────────────────────────────┘
```

Why it's split this way:
- **The billing core is one app with one database per shard.** Finalizing an invoice, using up credits, posting to the ledger, and emitting the event must all succeed or fail together in one database transaction. If subscriptions, invoices, and the ledger each had their own service and database, every invoice would need cross-service coordination and you'd gain nothing. Keep the internal modules strictly separated so you can pull one out later.
- **Metering is separate** because it behaves very differently: huge volumes of append-only writes, eventual consistency, and analytics-style reads.
- **Entitlements is separate** because it sits in the path of product requests and needs different speed and uptime guarantees.
- **Payments and tax are thin wrappers around providers.** Buy these; don't build them.

## 4. Multi-tenancy

### 4.1 Separate "who uses" from "who pays"

```
Customer ─ root legal payer · isolation + shard key (customer_id)
 ├─ Billing account(s) ─ currency · terms · payment methods
 │   │                   1 by default; N for subsidiaries/cost centers
 │   └─ Subscription(s) ─ item(s) ─ pinned price version + overrides
 ├─ Contracts · commitments · credit grants
 └─ Invoices · payments · ledger journals

Product tenant / workspace ──(effective-dated link)──► Billing account
```

- Usually one workspace maps to one billing account. But an enterprise may want 40 workspaces on one invoice, and workspaces move between accounts after acquisitions or re-orgs.
- **The workspace-to-account link records start and end dates.** Usage belongs to whoever owned the workspace when the event happened, so a move in the middle of a month splits usage correctly.
- **Shard by the top-level `customer_id`.** A customer's whole hierarchy, including its consolidated invoice, then lives on one shard and is billed in one transaction. Moving a customer to a different parent across shards is rare and done offline.

### 4.2 Isolation

| Tier | How | For |
|---|---|---|
| **Pooled** (default) | Shared tables, `customer_id` first in every key, Postgres row-level security (RLS), tokens scoped to one tenant | Almost everyone |
| **Regional cell** | A full copy of the stack per region (US, EU, …); a global directory sends each tenant to its cell | Data-residency rules, limiting the impact of outages |
| **Silo** | A dedicated cell | Regulated or very large tenants (who pay for it) |

Layered protection for the pooled tier:
- The gateway maps each token to a `customer_id`, and every database transaction runs `SET LOCAL app.customer_id`. RLS then hides all other customers' rows, so a missing `WHERE` clause can't leak data.
- Only internal finance roles can see across tenants, through a separate, audited database role.
- Personal data (contacts, addresses, tax IDs) is encrypted in the application with a separate key per tenant, managed through KMS.
- Automated tests try to read another tenant's data through every API.
- Privacy deletion vs. tax law: invoices must be kept by law, and that overrides deletion requests. Delete or anonymize everything else.

### 4.3 Noisy neighbors

- **Ingest:** each tenant gets its own rate limit and burst allowance. Over the limit, the API returns `429` with a `Retry-After` header. The SDK buffers events and retries with the *same* event IDs, which is safe because duplicates are removed.
- **Hot partitions:** the Kafka key is `tenant_id`. The biggest tenants get spread over several keys, `tenant_id#(hash(event_id) mod N)`. A retried event still lands on the same partition, so duplicate removal still works, and the aggregator combines the pieces.
- **Billing runs:** work is shared fairly across tenants. Very large invoices (say, over 100k lines) go to their own worker pool, so one giant customer doesn't delay 10k small ones.
- **API:** per-tenant rate limits. Expensive previews run in the background and are cached.

### 4.4 Per-tenant configuration

Settings are resolved in this order, later ones overriding earlier: platform default → selling entity → plan/segment → account → contract. This covers currency, payment terms, the failed-payment policy, invoice language and template, tax exemptions, PO numbers, and whether billing follows the calendar or the signup date. **Copy the resolved settings onto every document**, so each invoice records the terms it was issued under.

## 5. Domain model

| Area | Entities | Key rule |
|---|---|---|
| Catalog | Product, Price (versioned), Plan (versioned), Meter, Coupon | A version can't change once anything uses it |
| Accounts | Customer, BillingAccount, TenantLink (with start/end dates), SellerEntity | |
| Subscriptions | Subscription, Item, Phase (price steps), Change (scheduled or applied) | A `version` column to catch concurrent edits |
| Usage | UsageEvent, UsageSnapshot | A snapshot is a frozen usage total that invoice lines point to |
| Invoicing | Invoice, Line, CreditNote, InvoiceSequence | Every line points to its `price_version_id` (and `snapshot_id`) |
| Payments | PaymentMethod (provider token only), Payment, Attempt, Refund, Dispute | |
| Credits | CreditGrant (type, priority, expiry), CreditTxn | Promotional ≠ prepaid (see §6.4) |
| Ledger | Journal, Entry, LedgerAccount | Append-only; every journal balances to zero |
| Entitlements | EntitlementSet per tenant (versioned) | Derived and cached; never the source of truth |

Two constraints do a lot of the work:

```sql
-- Tenant key first in every primary key: needed for RLS, Citus sharding, index locality.
CREATE TABLE invoice (
  customer_id     uuid        NOT NULL,
  id              uuid        NOT NULL,
  subscription_id uuid,
  billing_reason  text        NOT NULL,  -- cycle | proration | one_off | manual
  period_start    timestamptz,           -- half-open [start, end), UTC
  period_end      timestamptz,
  status          text        NOT NULL,  -- draft | open | paid | void | uncollectible
  number          text,                  -- assigned at finalization, never before
  currency        char(3)     NOT NULL,
  total           bigint      NOT NULL,  -- minor units
  amount_due      bigint      NOT NULL,
  terms_snapshot  jsonb       NOT NULL,
  CHECK (billing_reason <> 'cycle'
         OR (subscription_id IS NOT NULL AND period_start IS NOT NULL)),
  PRIMARY KEY (customer_id, id)
);

-- Run the billing job once or fifty times: at most one cycle invoice per period.
CREATE UNIQUE INDEX one_cycle_invoice_per_period
  ON invoice (customer_id, subscription_id, period_start)
  WHERE billing_reason = 'cycle';

-- The app must not connect as the table owner (owners skip RLS unless it's FORCEd).
ALTER TABLE invoice ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invoice
  USING (customer_id = current_setting('app.customer_id')::uuid);
```

## 6. Core components

### 6.1 Catalog & pricing

| Model | Example |
|---|---|
| Flat | $500/mo platform fee |
| Per-unit | $20/seat/mo |
| Graduated tiers | First 1M calls at $0.0010, next 9M at $0.0008, … |
| Volume tiers | All units priced at whichever tier the total falls in |
| Package | $10 per 1,000 calls, rounded up |
| Dimensional | Price depends on an event property (`model`, `region`) |
| Commitment + overage | $120k/yr committed spend, used up over the year, extra usage at 20% off list |
| Prepaid credits | Customer buys credits; usage spends them at set rates |

**Meters** turn raw events into billable quantities: `{event_type, filter, aggregation, group_by}`. Aggregations are `count | sum | max | unique_count | latest | time-weighted avg`. The last one is for things like storage, which is billed in GB-months rather than by adding up events. Set an explicit price for each currency rather than converting at today's exchange rate when billing.

### 6.2 Subscriptions

```
incomplete → active (first payment / SCA ok) | expired
trialing   → active (converted) | canceled (trial lapsed)
active     ↔ paused
active     → past_due (renewal payment failed)
past_due   → active (paid) | suspended (dunning exhausted)
suspended  → active (paid) | canceled
any        → canceled, immediately or at period end (terminal)
```

(SCA is the EU's strong customer authentication for card payments. Dunning is the process of chasing failed payments, covered in §6.5.)

Every change is recorded as a `SubscriptionChange` with an `effective_at` time: now, end of period, or a specific date. Changes are logged and can be scheduled. Adding `?preview=true` returns the exact proration lines before anything is saved.

Default rules (each configurable per plan):
- **Upgrade:** takes effect now and is billed now, pro-rated for the rest of the period. This brings in cash and stops people upgrading, using the extra features, then downgrading. Decide whether new features unlock right away or only once payment succeeds.
- **Downgrade:** takes effect at period end, so no refunds are needed.
- **Seats added:** pro-rated and added to the next invoice. **Seats removed:** take effect at renewal.
- **Proration** = `price × time remaining / period length`, measured in seconds or in days. Pick one unit and use it everywhere. Show two lines, a credit for the unused old plan and a charge for the new one, rather than quietly netting them.
- **Price change in the middle of a period:** split the period into segments and price each segment at its own version. Decide explicitly whether tier thresholds carry over between segments.
- **Sales-led deals** come from the quoting tool (CPQ) as contracts: scheduled price steps plus overrides on catalog prices. Each quote line should map 1:1 to a catalog price or override, so quotes and invoices line up.

Calendar traps:
- **Calculate every period from the original billing date**, not from the end of the previous period. Otherwise Jan 31 → Feb 28 → Mar 28 and the date slips forever. The correct sequence is Jan 31 → Feb 28 → Mar 31: use the 31st, or the last day of shorter months.
- Store times in UTC and treat periods as start-inclusive, end-exclusive. If you promise "billed on the 1st," calculate that date in the customer's billing time zone.
- Build **simulated clocks per customer** from day one. That's the only practical way to test a three-year contract with price steps.

### 6.3 Metering pipeline

```
Product SDK ─ batches; retries reuse the same event_id
   │
Ingest API ─ authN · schema check · per-tenant quota · 202 after Kafka ack
   │
Kafka usage.raw ─ RF=3, acks=all, keyed by tenant_id
   │
Flink ─ dedup on (tenant_id, event_id) · attribute to payer via TenantLink
   │    as of event time · match meters
   ├─► ClickHouse raw events + S3/Iceberg archive
   └─► live aggregates → usage dashboards · spend alerts · limit counters

Close job at period_end + grace ─ authoritative query over raw events
   └─► UsageSnapshot (immutable, watermarked) ─► rating ─► invoice lines
```

```
POST /v1/usage/events:batch                    // ≤ 1,000 events per request
{ "events": [{
    "event_id":  "01J9ZK8Q4V7R2XKQ1M3T5W6Y8Z", // client ULID = idempotency key
    "tenant_id": "ws_4821",                     // billing resolves the payer
    "type":      "llm.tokens",
    "timestamp": "2026-10-02T14:03:11.204Z",   // event time, not ingest time
    "properties": { "model": "large", "input": 1830, "output": 412 }
}]}
→ 202 Accepted   // only once the batch is safely stored in Kafka
```

- **"Accepted" means safely stored in Kafka**, not processed. Ingest keeps working when later stages fall behind; a backlog is just a delay.
- **Two layers of duplicate removal make each event count exactly once.** The stream drops duplicates within the retry window (e.g., 48 h). Storage drops duplicates on `event_id` across the whole late-arrival window. ClickHouse only removes duplicates eventually, in the background, so the period-close query uses `FINAL` to force an exact result.
- **Late events:** accept events up to N days old (e.g., 30). A period closes at `period_end + grace`, with the grace set between 1 and 24 h based on how late your producers usually are. Anything arriving after close goes on the next invoice as a visible **prior-period adjustment** line. Never edit a finalized invoice.
- **Two paths, one definition.** The live totals (fast; used for dashboards and limits) and the period-close totals (authoritative; used for invoices) are both built from the same meter definition. A reconciliation job alerts if they drift apart beyond a small tolerance.
- **UsageSnapshot** stores the quantity per meter, dimension, and period, plus a hash of the query and a watermark marking which events were included. That lets you explain and recompute any invoice number during a dispute. The watermark also shows exactly which late events a later adjustment needs to cover.
- **Sizing:** 2B events/day is about 23k/s on average. At ~300 bytes per event that's ~600 GB/day raw, usually 5–10× smaller once compressed. That's a medium-sized Kafka/Flink/ClickHouse setup.

### 6.4 Rating & invoicing

**Order of operations.** Fix it, document it, and always follow it; this is where invoices quietly go wrong:
1. Gross charges: recurring fees (billed in advance), usage (billed in arrears), prorations, one-off charges
2. Discounts (per line first, then per invoice) and *promotional* credits. These reduce the amount that gets taxed.
3. Top-up charge if a minimum commitment wasn't reached
4. Round each line to the smallest currency unit
5. Tax from the tax engine; store its full breakdown exactly as returned
6. *Prepaid* credits (bought credits, balance from credit notes), applied like a payment
7. Amount due → collect payment

Promotional and prepaid credits are taxed and recognized as revenue differently. Model them as separate types from the start.

**Billing run.** A scheduler on each shard looks for `subscription WHERE next_billing_at <= now()` (indexed, with `next_billing_at = period_end + grace`) and queues jobs keyed by `customer_id`. Queuing the same job twice is harmless:

```python
def bill_cycle(customer_id, sub_id, boundary):      # safe to run N times
    with db.tx(), advisory_xact_lock(customer_id):   # serialize per customer
        if invoices.cycle_exists(sub_id, boundary):
            return                                   # already billed → no-op
        sub = subscriptions.as_of(sub_id, boundary)  # phases, overrides
        usage = snapshots.require(sub, sub.period_ending(boundary))
        lines = (recurring_in_advance(sub, boundary)   # next period
                 + usage_in_arrears(sub, usage)        # closed period
                 + pending_prorations(sub)
                 + pending_one_offs(sub))
        draft = rating.price(lines, sub.discounts, sub.contract,
                             credits.promotional(customer_id))
        invoices.insert_draft(draft)       # unique index = last line of defense
        subscriptions.advance_period(sub)  # optimistic version check
        outbox.emit("invoice.drafted", draft.id)
    # invoice.drafted starts the finalize workflow, with no locks held:
    # tax quote (idempotent per invoice) → draft window → finalize()
```

**Finalizing** happens in one transaction: assign the invoice number → set status to `open` → use up credits → post the ledger entries → record `invoice.finalized` in the outbox table so the event is published reliably. The PDF, email, tax filing, and payment follow in the background.

- **Numbering:** assign the number when finalizing, never at draft time, so deleted drafts leave no gaps. Some countries require numbers with no gaps. Use one numbering series per selling entity per shard (`US-S07-2026-004211`). That's allowed where "one or more series" is permitted, as in the EU, and it keeps finalizing on a single shard. Where only one series is allowed, run a small dedicated numbering service.
- **After finalizing**, only the invoice's status can change. Fixes are new documents: credit notes (full or partial, refunded to the card or to the customer's balance) or prior-period adjustments.
- **Draft window:** a short delay before finalizing. It lets integrations add items and lets finance review enterprise invoices.
- **Consolidated invoices** group child accounts' lines under the parent. This stays on one shard because shards are keyed by the top-level customer.
- **Very large customers:** generate lines in chunks, and send a summary invoice with a detailed usage CSV attached.

One full cycle (billing date the 15th, 1 h grace, 1 h draft window):

```
Oct 15 00:00  period [Sep 15, Oct 15) ends
Oct 15 01:00  grace ends → close job writes UsageSnapshots
              bill_cycle → draft:   fees  [Oct 15, Nov 15) in advance
                                  + usage [Sep 15, Oct 15) in arrears
                                  + proration for seats added Oct 3
              tax quoted on the draft
Oct 15 02:00  finalize → number US-S07-2026-004211, ledger journal posted
              charge (PSP idempotency key inv_789:1) → succeeded → paid
              (declined → past_due → dunning workflow starts)
async         PDF + email · tax transaction committed · rev-rec schedule
```

### 6.5 Payments & dunning

- **Payment-provider (PSP) adapter** with `charge`, `refund`, `attach_method`, and `normalize_webhook`. It routes by region, payment method, and selling entity. For bank transfers, give each customer its own virtual account number so incoming wires match automatically.
- **Card security (PCI):** card numbers never touch your servers. Use the provider's hosted card fields and store only the token, brand, last 4 digits, expiry, and fingerprint. That keeps you in the lightest PCI category (SAQ A).
- **Payment attempt states:** `created → processing → requires_action (3DS/SCA) → succeeded | failed | unknown`. The idempotency key sent to the provider is `invoice_id:attempt_no`.
- **A timeout means "unknown," not "failed."** Ask the provider using the same key. Retrying with a new key is how customers get charged twice.
- **Incoming webhooks** arrive duplicated and out of order. Check the signature, store the raw payload, and drop duplicates by the provider's event ID. Then treat it as a hint: fetch the current object from the provider and only ever move its state forward.
- **Dunning** (chasing failed payments) runs as a long-running workflow, e.g. in Temporal, one per overdue invoice:
  - Retry based on the decline reason. Never retry a card reported stolen.
  - Use card-updater services and network tokens to pick up replaced cards.
  - Send emails and show in-app banners.
  - Escalate in stages: grace (banner) → restricted (read-only) → suspended → canceled and written off.
  - Each stage emits `dunning.stage_changed`, and the entitlements service turns that into product behavior.
  - Enterprise accounts on invoice terms are never auto-suspended. They go to a collections queue showing how overdue they are.
- **Daily reconciliation:** compare the provider's transaction and payout reports with your payments table and with the ledger's provider-clearing account. Any mismatch alerts finance engineering.

### 6.6 Ledger & revenue recognition

The ledger is double-entry and append-only. Each entry is posted **in the same transaction** as the business event that caused it:

| Event | Debit | Credit |
|---|---|---|
| Annual invoice finalized ($1,200 + $100 tax) | AR 1,300 | Deferred revenue 1,200 · Tax payable 100 |
| Monthly revenue-recognition run | Deferred revenue 100 | Revenue 100 |
| Usage billed in arrears | AR | Revenue · Tax payable |
| Payment succeeds | PSP clearing | AR |
| Provider payout arrives | Bank · Processing fees | PSP clearing |
| Prepaid credits invoiced | AR | Credit liability |
| Credits used | Credit liability | Revenue |
| Credit note | Revenue (or Deferred) · Tax payable | AR (or Customer balance) |
| Write-off | Bad-debt expense | AR |

(AR = accounts receivable, money customers owe you.)

- **Rules checked constantly:** every journal balances to zero, each customer's AR equals their open invoices, and each credit balance equals the sum of its credit transactions.
- **Multiple currencies:** post in both the invoice currency and your accounting currency, using an exchange rate saved at the time. Record the exchange-rate gain or loss when payment arrives.
- **Revenue recognition (ASC 606 / IFRS 15):**
  - Build schedules from invoice lines: subscriptions spread evenly over the period, usage as it's consumed, and expired credits recognized when they lapse.
  - Accrue usage that hasn't been billed yet at month-end, and export journals to the general ledger.
  - For multi-product enterprise contracts that need the price split across products (allocation by standalone selling price), use a dedicated revenue tool (NetSuite ARM, Zuora Revenue, RightRev) instead of building one.

### 6.7 Tax

- **An adapter over Avalara, Vertex, Stripe Tax, or Anrok.**
  - Each product has a tax code, because whether SaaS is taxable varies by US state.
  - The selling entity determines where you're registered and owe tax.
  - Customer location comes from the billing address, plus supporting evidence such as IP address and card country for EU VAT.
- **EU business customers:** validate VAT IDs against the EU's VIES service, apply reverse charge (the customer accounts for the VAT), and keep the validation evidence.
- **Lifecycle:** get a quote when drafting, re-quote if anything changed, then **commit** the transaction after finalizing (safe to repeat per invoice). Reverse it on void or credit note. Store the full breakdown on the invoice and never recalculate tax on old invoices.
- **If the tax engine is down,** invoices wait in draft. Never finalize without tax.
- **Mandatory e-invoicing** is spreading: Italy, Poland, Belgium, France, and more. Put invoice delivery behind its own adapter (PDF/email, the Peppol network, government clearance APIs). In some countries an invoice isn't legally issued until the tax authority accepts it, so add a `cleared` state.

### 6.8 Entitlements: the product's only interface to billing

```jsonc
// materialized per tenant, versioned, pushed on every change
{ "tenant_id": "ws_4821", "version": 912,
  "state": "active",                       // active | restricted | suspended
  "features": { "sso": true, "audit_log": false },
  "limits":   { "seats": 50, "api_calls_month": 10000000 } }
```

- **How they're built:** from the plan template, add-ons, contract overrides, trial status, and dunning stage. They're recalculated whenever a relevant event arrives, served from Redis, and pushed to an SDK that caches them in memory, so a check makes no network call.
- **Feature keys, not plan names:** product code asks `can(tenant, "sso")`, never `if plan == "pro"`. Repackaging plans then becomes a catalog change, not a code deploy.
- **Limits:** use live counters from the metering stream. Soft limits send a warning or bill the overage, and can overshoot slightly because the counters lag a little. Hard limits, like prepaid balance or spending caps, use atomic counters checked on every request.
- **If billing is unreachable, keep using the last known state** and alert when it gets stale. Never lock out a paying customer because billing is down.

## 7. Reliability patterns at a glance

| Risk | Pattern |
|---|---|
| Data saved but the event never published | Write the event to an outbox table in the same transaction, then relay it to Kafka (Debezium or a poller) |
| Client retries a write | `Idempotency-Key` table: `(customer_id, key) → request hash + response` |
| Duplicate usage events | Remove duplicates by `event_id` in the stream and in storage |
| Double billing | Unique index on (subscription, period) for cycle invoices |
| Two edits to one customer at once | Per-customer lock or queue, plus version checks on rows |
| Multi-step calls to outside systems | Long-running workflows for payment, dunning, tax filing, e-invoice clearance |
| Duplicate or out-of-order webhooks | Drop duplicates by provider ID, re-fetch the object, only move state forward |
| Systems quietly drifting apart | Nightly checks: live vs. close-time usage, invoices vs. ledger, payments vs. provider, entitlements vs. subscriptions |
| Failures nobody notices | Business-level alerts: billing-run delay, drafts stuck > 2 h, payment success rate, reconciliation mismatches |
| Pricing bug found later | Re-price in preview mode, compare, then issue credit notes or adjustments; never edit invoices |

## 8. Scaling

- **Billing core:** 50k customers fit on one well-sized Postgres. Design keys for sharding from day one, but only shard (Citus, by `customer_id`) when you need to. Portal reads go to read replicas. All analytics (MRR/ARR changes, overdue AR, cohorts) run in the warehouse, fed by change-data-capture, never on the main database.
- **Billing-day spikes:** billing on each customer's signup date spreads the load naturally. Calendar billing ("everyone on the 1st") and annual renewals don't. Advance fees don't depend on usage, so **draft them days early**; only the usage lines have to wait for the period to close.
- **Metering** scales by adding Kafka partitions, Flink workers, and ClickHouse shards.
- **Entitlements** checks are local. Redis only handles pushing updates out and serving cold starts.

## 9. Security & compliance

- **Card security (PCI DSS):** store tokens only, and scrub card numbers from logs.
- **SOC 2 / SOX:**
  - A log of every change that can't be edited (who, before and after, why).
  - A second approver for credits and refunds above a threshold.
  - Separation of duties: whoever edits pricing can't approve write-offs.
  - Regular access reviews.
- **Encryption:** TLS everywhere, KMS for data at rest, separate keys per tenant for personal data.
- **Residency and privacy:** regional cells, data export on request, and deletion within the limits of legal retention rules.
- **Secrets:** provider API keys and webhook-signing secrets live in a vault and are rotated.

## 10. Reference stack

| Layer | Pick | Alternatives |
|---|---|---|
| Billing core database | PostgreSQL (+ Citus when sharding) | CockroachDB, Spanner |
| Language | Kotlin/Java (`BigDecimal`), Go (`shopspring/decimal`) | Anything with a real decimal type |
| Event bus | Kafka | Redpanda, Kinesis, Pub/Sub |
| Stream processing | Flink | Kafka Streams; ClickHouse materialized views at smaller scale |
| Usage store | ClickHouse + S3/Iceberg | Druid, Pinot |
| Workflows | Temporal | AWS Step Functions |
| Payments | Stripe, Adyen, GoCardless | Braintree, Checkout.com |
| Tax | Avalara, Stripe Tax, Anrok | Vertex |
| Analytics | Debezium change-data-capture → Snowflake/BigQuery | Databricks |
| General ledger / revenue recognition | NetSuite (+ ARM) | Zuora Revenue, RightRev |

## 11. Build vs. buy, and phasing

- **Always buy** payment processing (including card storage) and tax calculation and filing.
- **Seriously consider buying the billing engine itself:**
  - General: Stripe Billing, Chargebee, Recurly, Zuora.
  - Usage-heavy pricing: Orb, Metronome.
  - Open source: Lago, Kill Bill.
  - Metering only: OpenMeter.

  Build your own if pricing flexibility is a competitive advantage, or if per-transaction fees hurt at your volume.
- **You'll own some parts either way:** the workspace-to-payer mapping (§4.1), sending usage with stable event IDs, entitlements (§6.8), reconciliation (§7), and the finance data pipeline.

If you build, do it in phases:
1. **Foundation:** versioned catalog, flat and per-seat subscriptions, cards, invoices, basic dunning, the tax adapter, entitlements, **and the ledger**. Adding a ledger later is painful.
2. **Usage:** metering pipeline, usage pricing, next-invoice previews, credits, spending alerts.
3. **Enterprise:** contracts, commitments, and price steps; account hierarchies and consolidated invoices; invoice terms with bank reconciliation; multiple selling entities; revenue recognition and ERP export.
4. **Global:** regional cells, the silo tier, e-invoicing channels, testing pricing changes against past usage.

Moving off an existing billing system? **Run both in parallel** for 2–3 billing cycles and compare every invoice to the cent before switching over.

## 12. Variant: billing-as-a-service (tenants are merchants)

Add a level above the customer, `Merchant → Customer → Billing account → …`, and put `merchant_id` first in every key. Shard on `(merchant_id, customer_id)` so small merchants stay on one shard and large ones spread across several. On top of that:
- **Per merchant:** catalog, tax registrations, invoice numbering and branding, webhook endpoints, scoped API keys, and strict **test/live separation**.
- **Payment connections:** either the merchant's own provider credentials stored in a vault, or you act as the payment facilitator with sub-accounts for each merchant.
- **Isolation becomes a contractual promise:** RLS is mandatory, each merchant gets its own encryption keys, every resource has quotas, and merchants can see their own audit logs.
- **Noisy neighbors become a serious threat.** A merchant billing 5M customers on the 1st needs fair scheduling or its own cell.
- **Extensibility:** custom fields, outgoing webhooks, and possibly sandboxed (WASM) pricing hooks.

---

If you share your actual pricing model, volumes, and whether you're leaning toward building or buying, I can go deeper on any piece: the full schema, the Flink/ClickHouse setup, or the dunning workflow.
