# Plan: Real-Time Card Fraud Detection Pipeline

I've assumed you're the **issuer or processor**: you score each authorization request before it's approved or declined. If you're a merchant or PSP, the design is mostly the same, but the inputs (device, checkout session) and actions (block, 3DS challenge) change. Open questions are at the end.

---

## 1. Requirements

| Concern | Target (assumption, adjust to your numbers) |
|---|---|
| Fraud decision latency | **≤ 50 ms p99** inside an auth flow that has a few hundred ms in total |
| Throughput | Built for 10× normal peak (Black Friday, payroll days) |
| Availability | 99.99%. Scoring must never block the payment |
| Fraud rate | About 0.05–0.2% of transactions, so classes are very imbalanced |
| Success metrics | Fraud caught in dollars, false-decline rate, review-queue volume |

**Key design rule:** the authorization path is a **synchronous request/response call**. Kafka and Flink update features and record events next to it, but are never a blocking step in the decision.

---

## 2. Architecture

```
                       ┌──────────── HOT PATH (synchronous, <50ms) ────────────┐
Card network ─► Auth ─►│ Fraud API ─► Feature fetch ─► Rules + Model ─► Decision│─► Approve / Decline / Step-up
 (ISO 8583)   switch   │   (gRPC)     (online store)    (in-process)    policy │
                       └───────┬───────────────────────────────────────────────┘
                               │ emit auth event + score + decision
                               ▼
                      ┌─────────────────┐     ┌───────────────────┐
                      │ Kafka (events)  │────►│ Flink: streaming  │──► Online feature store
                      └───────┬─────────┘     │ aggregates        │    (Redis/Aerospike/DynamoDB)
                              │               └───────────────────┘
                              ▼
                 Lakehouse / offline store (Iceberg/Delta) ◄── Labels: chargebacks, customer
                              │                                 fraud reports, analyst outcomes
                              ▼
                 Training (point-in-time joins) ─► Model registry ─► Shadow ─► Canary ─► Prod
                              │
                              ▼
                 Monitoring: latency, drift, score distribution, decline rate, fraud bps
```

---

## 3. Components

### a) Ingest and enrichment
- Convert the ISO 8583 message to an internal schema: card token, amount, currency, MCC, merchant ID, POS entry mode, card-present or not, country, and terminal/device/IP for card-not-present.
- **Tokenize the card number (PAN) before it reaches the fraud system** (PCI DSS). This keeps most of the pipeline outside PCI scope. Never store CVV or track data.

### b) Features (where most of the detection power comes from)

| Type | Examples | Computed where |
|---|---|---|
| Velocity | Transaction count and total per card over 1m/10m/1h/24h; distinct merchants per hour; declines in the last hour | Flink sliding windows → online store |
| Behavioral profile | Amount vs. the card's typical amount (z-score); first time at this merchant or MCC; usual hour of day | Daily batch plus streaming updates |
| Geo | Distance and time from last card-present transaction ("impossible travel"); card's country vs. merchant country | On request plus last-location lookup |
| Merchant / terminal | Fraud rate per merchant or MCC; recent fraud spike at the terminal (compromise point) | Batch plus streaming |
| Card-not-present / device | Device fingerprint age, IP risk, email/phone age, shipping ≠ billing address | Vendor signals and internal data |
| Graph (phase 2) | Devices, IPs, or addresses shared across many cards; card-testing rings | Offline graph jobs → online lookups |

- **Define each feature once** (Feast/Tecton or in-house) and use that definition for both serving and training. This prevents the model seeing different values in training than in production.
- **Read-your-writes problem:** the streaming aggregates may not yet include the transaction being scored, or the one just before it. Add the current transaction to the counts in the request path so card-testing bursts aren't missed.
- Fetch budget: one batched read from the online store, about 5–10 ms p99.

### c) Scoring
- **Rules engine:** hard blocks (sanctioned regions, compromised-card lists), policy rules, and fast responses to new attacks. Fraud analysts should be able to change rules without a code deploy, with versioning and a backtest required before rollout.
- **ML model:** a gradient-boosted tree model (LightGBM/XGBoost) is the right first model. It works well on tabular data, runs fast (under 2 ms in-process), and is explainable with SHAP reason codes.
- Later: sequence models over each card's transaction history, and graph neural networks for fraud rings, mainly as offline feature generators.
- Separate models or thresholds for **card-present and card-not-present**, because their fraud patterns differ a lot.

### d) Decision policy
- The output is an action, not just a score: **approve / step-up (3DS, OTP, push notification) / decline / approve-and-queue-for-review**.
- Thresholds vary by segment (amount, MCC, customer tenure) and are tuned to an expected-cost function:
  `cost = fraud loss × P(fraud) vs. lost interchange + customer churn × P(false decline)`
- Each decision returns reason codes for analysts and customer service.

### e) Failure handling
- Hard timeout around the fraud call (for example 40 ms).
- If it times out, fall back to a rules-only decision or a default stand-in policy. For example, fail open below an amount threshold and step up above it.
- Circuit breakers, multi-AZ deployment, and per-region online stores. Load-test at 10× peak.

---

## 4. Labels and feedback loop (the hard part)
- **Label sources:** chargebacks with fraud reason codes (arrive 30–90+ days later), cardholder fraud claims (days), and analyst outcomes on reviewed cases (hours).
- **Label maturity:** train only on transactions old enough for their labels to have arrived, or model the delay. Otherwise recent fraud looks like legitimate traffic.
- **Selection bias:** declined transactions never get a fraud label. Mitigate with a small controlled exploration group (randomly approve a tiny fraction of low-amount, borderline declines, within a loss budget) or with reject-inference methods.
- **Fraud-ring tagging:** when one transaction is confirmed fraud, also label the related transactions on that card in the same time window.
- **Retraining:** weekly or monthly on a schedule, plus triggered retraining when drift alerts fire. Rules handle new attacks in the meantime.

---

## 5. Evaluation and rollout
- **Offline metrics:** precision-recall AUC (not ROC), dollar-weighted recall at a fixed false-positive rate, and decline rate by segment. Validate on later time periods than training (out-of-time), never a random split.
- **Rollout:** shadow mode (score but don't act) → champion/challenger on 5–10% of traffic → gradual ramp, with automatic rollback if the decline rate or latency breaches limits.
- **Model governance:** model cards, documented validation, approval workflow, and reproducible training. Regulators expect this (e.g., SR 11-7 model risk guidance in the US).

---

## 6. Monitoring
- **System:** p50/p99 latency per stage, timeout and fallback rate, consumer lag, online-store hit rate.
- **Model:** score distribution, feature drift (PSI), share of missing features, approve/decline/step-up rates by segment.
- **Business:** fraud losses in basis points, false-decline rate (from customer complaints and step-up pass rates), review-queue SLA.
- **Alerts:** sudden spikes such as card-testing bursts at one merchant or BIN range, which should feed straight into rule updates.

---

## 7. Phased delivery

| Phase | Scope | Rough duration |
|---|---|---|
| 0 | Event schema, tokenization, Kafka, lakehouse, label ingestion, baseline rules | 4–6 weeks |
| 1 | Flink velocity features, online store, Fraud API with rules, latency SLOs met | 6–8 weeks |
| 2 | First gradient-boosted model in shadow → champion/challenger; decision policy; reason codes; analyst case tool | 6–8 weeks |
| 3 | Step-up integration (3DS, OTP), exploration group for unbiased labels, drift monitoring and automated retraining | 6 weeks |
| 4 | Graph/fraud-ring features, sequence models, consortium/network risk signals | Ongoing |

---

## 8. Technology choices (swappable)
- **Streaming:** Kafka (or Kinesis/Pub/Sub) with Flink for stateful windows.
- **Online store:** Redis Enterprise, Aerospike, or DynamoDB. Offline: Iceberg/Delta on object storage.
- **Serving:** Go/Java/Rust gRPC service with the model embedded (ONNX/Treelite), not a remote model server on the hot path.
- **Machine-learning workflow:** MLflow registry, Airflow/Dagster pipelines, a feature platform (Feast/Tecton).

---

## Open questions that would change the design
1. **Issuer, acquirer/PSP, or merchant?** This decides which signals you have and which actions you can take.
2. Transaction volume (average and peak), and the latency budget the auth switch actually allows.
3. Mix of card-present and card-not-present (e-commerce) transactions.
4. Do historical labels exist, how many, and how reliable are chargeback reason codes?
5. Build or buy? Vendor scoring (Featurespace, Feedzai, network-provided scores) could be used as features or as a starting baseline.
6. Regulatory footprint: PCI scope, GDPR/data residency, explainability requirements.

If you can answer these, I can turn this into a sized design with capacity numbers, a cost estimate, and a first feature list.
