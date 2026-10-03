# Plan: Real-Time Card Fraud Detection Pipeline

## 0. Assumptions

You didn't give scale or context, so I assumed the following. Change them and the design changes with them.

| Assumption | Value |
|---|---|
| Role | Issuer, or a processor acting for issuers, that scores transactions **during authorization** |
| Volume | About 3k TPS on average, 15k TPS at peak (holidays) |
| Latency budget | **p99 ≤ 50 ms** for fraud scoring inside a total auth budget of about 150 ms |
| Channels | Card-present (POS) and card-not-present (e-commerce, 3DS) |
| Availability | 99.99%. Scoring must never become the reason an authorization fails |

---

## 1. Architecture overview

There are two paths. The **synchronous path** sits inside authorization and has to be fast and simple. The **asynchronous path** does everything else: heavy aggregation, graph analysis, training and case review.

```
                ┌──────────── SYNCHRONOUS (in auth path, ≤50ms) ────────────┐
Card network →  Auth Switch → Fraud Gateway → Feature Fetch → Rules + ML → Decision
 (ISO 8583)        ▲              │          (online store)    Scoring     Engine
                   │              │                                          │
                   └──── approve / decline / step-up (3DS/OTP) / review ─────┘
                                  │
                                  ▼ (emit event)
                ┌──────────── ASYNCHRONOUS (streaming + batch) ─────────────┐
   Kafka (auth events, decisions, clearing, chargebacks, disputes, logins)
        │
        ├─► Flink: streaming aggregates ──► Online Feature Store (Redis/Aerospike)
        ├─► Graph service: shared devices/emails/IPs ──► features & alerts
        ├─► Case management: analyst queue, customer contact
        └─► Lakehouse (Iceberg/Delta) ──► labeling ──► training ──► model registry
```

---

## 2. Components

### 2.1 Ingestion and the Fraud Gateway
- Takes the auth request from the switch over gRPC or a native ISO 8583 adapter, and normalizes it into a canonical event using Protobuf or Avro with a schema registry.
- **Hard timeout of about 40 ms.** If scoring runs past it, the gateway falls back to **stand-in rules**: a local, in-memory rule set based on amount, MCC and country risk. It never just approves or declines blindly.
- Publishes the raw event and the final decision to Kafka **asynchronously**, so publishing never blocks the response.
- PAN is tokenized at the edge. Nothing after the gateway sees a raw PAN, which keeps PCI scope small.

### 2.2 Feature Store (the hardest part)
Features come in three freshness tiers:

| Tier | Examples | Computed by | Freshness |
|---|---|---|---|
| **Request-time** | amount, MCC, entry mode, CNP flag, local hour, distance from last txn | Inline in the scoring service | 0 ms |
| **Streaming aggregates** | count and sum per card over 1m/1h/24h, distinct merchants in 1h, declines in 10m, card-testing pattern (many small CNP auths) | Flink, sliding or tumbling windows, keyed by card, device, merchant and IP | Under 1 s |
| **Batch profiles** | 90-day spend profile, typical MCCs and geos, merchant fraud rate, customer tenure, graph risk scores | Daily Spark jobs | Hours |

- **Online store:** Redis Cluster or Aerospike with p99 reads under 5 ms. Fetch one entity per lookup key in a single batched call.
- **Velocity read-your-writes problem:** Flink lags by a few hundred ms, so a fast burst of card-testing auths can slip through. To catch it, update hot counters such as `txn_count_card_60s` **synchronously in the gateway** with atomic Redis INCR plus TTL, and let Flink handle the richer windows.
- **Training/serving parity:** define features once (Feast, Tecton or an in-house DSL) and build training sets with **point-in-time correct** joins. Most fraud ML failures I'd expect come from training/serving skew and label leakage, not from the model.

### 2.3 Scoring
- **Rules engine** (e.g. Drools or a custom DSL that analysts can edit with versioning):
  - Hard blocks: known compromised cards, sanctioned regions, merchant blocklist.
  - Fast tactical responses to new attacks, deployable in minutes.
- **ML model:**
  - Gradient-boosted trees (LightGBM/XGBoost), served in-process via ONNX or Treelite. That takes about 1–3 ms, with no network hop.
  - Separate models or calibration per segment (CP vs. CNP, consumer vs. commercial), because fraud patterns differ a lot between them.
  - Optional later: a sequence model (transformer or GRU over the card's last N transactions), with embeddings computed asynchronously and cached.
- **Output:** a calibrated probability, the top reason codes from SHAP (precomputed or TreeSHAP), and the model version.

### 2.4 Decision Engine
- Converts the score, rule hits and context into an action using **cost-aware thresholds**:
  - Expected loss = P(fraud) × amount.
  - Compare it with the cost of a false decline: lost interchange, customer churn and support calls.
- Possible actions: **Approve**, **Decline**, **Step-up** (3DS challenge, OTP, push to the app), **Approve + queue for review**, and **Approve + alert the customer**.
- Thresholds are config, not code. They are versioned, A/B testable and set per segment.

### 2.5 Case management and the feedback loop
- An analyst UI shows the transaction, the card's timeline, linked entities from the graph, and the reason codes.
- Customer confirmations ("was this you?" via SMS or the app) are the **fastest labels**, arriving in minutes.
- Chargebacks and disputes are the **authoritative labels**, but they arrive 30–90 days later.
- Labels are written back to the lakehouse with event timestamps for training.

---

## 3. ML lifecycle

| Concern | Approach |
|---|---|
| **Label delay** | Train on data older than about 60 days for mature labels. Add confirmed-fraud labels from the fast channels to recent windows. Consider a label-maturity weighting scheme |
| **Class imbalance** (~0.05–0.2% fraud) | Downsample negatives, then re-calibrate (Platt or isotonic) on an unsampled holdout |
| **Selection bias** | Declined transactions have no outcome label. Keep a small **random exploration holdout**, e.g. 0.1% of low-risk declines approved with monitoring, or use reject inference |
| **Evaluation** | Out-of-time validation only, never a random split. Metrics: recall at a fixed false-positive rate, $-weighted recall, PR-AUC |
| **Retraining** | Weekly or biweekly scheduled retrains, plus a retrain when drift is detected |
| **Deployment** | Registry → **shadow mode** (score without acting for 1–2 weeks) → canary at 5% → full rollout. Instant rollback through config |

---

## 4. Non-functional requirements

**Latency budget (p99):**
| Step | Budget |
|---|---|
| Gateway and parsing | 3 ms |
| Feature fetch | 8 ms |
| Rules | 3 ms |
| Model plus explanation | 5 ms |
| Decision and response | 2 ms |
| **Headroom** | **about 20 ms** |

**Resilience:**
- Multi-region active-active setup.
- Stand-in rules if the feature store or model is unavailable.
- Circuit breakers on every dependency.
- Kafka with RF=3 and idempotent producers.
- Flink exactly-once checkpoints. Redis counters tolerate a small amount of double counting.

**Security and compliance:**
- PCI DSS: tokenized PAN, HSM-managed keys, network segmentation.
- GDPR/CCPA data retention policies.
- Model risk management (SR 11-7 style): documentation, explainability, bias review.
- An audit log of every decision, recording the features, model and rule versions.

**Observability:**
- Latency histograms for each stage.
- Fallback rate.
- Score distribution and feature drift (PSI).
- Approval rate and decline rate by segment.
- Fraud basis points.
- Alerting on sudden shifts, since a shift can mean an attack *or* a broken feature.

---

## 5. Business KPIs
- **Fraud loss in bps** of transaction volume.
- **False positive ratio**: false declines per fraud caught.
- **Approval rate** and customer friction (step-up rate, 3DS abandonment).
- **Analyst efficiency**: cases per hour and precision of the review queue.
- **Time to respond** to a new attack pattern, with a target under 1 hour using rules.

---

## 6. Rollout plan

| Phase | Weeks | Deliverables |
|---|---|---|
| **1. Foundation** | 0–6 | Canonical event schema, Kafka, gateway with stand-in rules, lakehouse ingestion of auth, clearing and chargebacks |
| **2. Rules + features** | 6–12 | Flink streaming aggregates, online store, rules engine, analyst case tool v1. Go live with rules |
| **3. ML v1** | 12–20 | Labeling pipeline, point-in-time training sets, GBDT model, shadow mode, then canary, then live |
| **4. Optimization** | 20–28 | Cost-based thresholds, step-up actions, A/B framework, drift monitoring, exploration holdout |
| **5. Advanced** | 28+ | Graph features (rings, mule accounts), sequence models, device fingerprinting, consortium data |

---

## 7. Main risks
1. **Feature skew between training and serving.** Mitigation: one shared feature definition and logging of the features actually served, used for training.
2. **Label leakage**, for example features computed with information from after the transaction. Mitigation: strict point-in-time joins.
3. **Feedback loops**: the model only learns from transactions it approved. Mitigation: exploration holdout.
4. **Attackers adapting.** Mitigation: fast rule deployment and frequent retraining.
5. **Hot keys** on a heavily used merchant or a card-testing BIN range overloading Redis shards. Mitigation: key salting and local caching for merchant-level features.

---

**Questions that would sharpen the plan:**
- Are you an issuer, an acquirer or a PSP/merchant? This changes the available signals and actions. For example, acquirers can't step up the cardholder in the same way.
- What are your real TPS and latency budget?
- Do you have historical labeled data, and how far back?
- What's your existing stack (cloud vs. on-prem, any Kafka or Flink already in place)?
