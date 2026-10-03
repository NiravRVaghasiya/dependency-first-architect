# Real-Time Card Fraud Detection Pipeline: Plan

## 0. Assumptions to confirm first
These choices change the design a lot, so I've stated my defaults:

| Question | Default assumed |
|---|---|
| Who are you? (issuer, acquirer/PSP, or merchant) | Issuer/PSP that scores during authorization |
| Peak volume | About 5–10k TPS at peak, about 1k average |
| Latency budget for fraud scoring | **p99 ≤ 50 ms** inside a roughly 150–300 ms auth SLA |
| Channels | Card-present (CP) and card-not-present (CNP) |
| What happens if scoring fails | Fall back to rules only, then approve with limits (fail-open with limits) |
| Compliance | PCI DSS scope, GDPR/CCPA, and reason codes for declines |

---

## 1. Architecture

```
                    ┌────────────── SYNCHRONOUS (auth path, ≤50ms) ───────────────┐
Card network ──► Auth gateway ──► Fraud Decision Service ──► approve / decline / step-up (3DS/OTP) / review
                                     │   ▲        ▲
                                     │   │        └── Rules engine (hard blocks, allow-lists)
                                     │   └── Model server (GBDT, in-process or gRPC)
                                     │   ▲
                                     │   └── Online feature store (Redis/Aerospike/DynamoDB)
                                     ▼
                    ┌────────────── ASYNCHRONOUS (streaming) ─────────────────────┐
                 Kafka: txn-events, decisions, auth-outcomes
                     │
                     ├──► Flink: streaming features (velocity, windows, geo) ──► online store
                     ├──► Lakehouse (Iceberg/Delta): raw events + decisions + features served
                     ├──► Case management (analyst queue) ──► labels
                     └──► Monitoring (latency, drift, decline rate)

  Labels: chargebacks (30–120 days late), cardholder disputes, analyst verdicts ──► label table
  Offline: point-in-time training sets ──► train/validate ──► registry ──► shadow ──► canary ──► prod
```

**Main principle:** the auth path only reads precomputed state and runs cheap math. Anything heavy, such as aggregations, graph analysis, or retraining, runs off the path.

---

## 2. Features

| Family | Examples | Where computed |
|---|---|---|
| Transaction | amount, MCC, channel, entry mode, currency, cross-border flag | Request payload |
| Velocity | transaction count and total amount per card over 1m/1h/24h/7d; distinct merchants/countries in 24h | Flink, sliding/tumbling windows |
| Behavioral deviation | amount z-score against the card's history; new merchant/MCC/country for this card; time since last transaction | Flink plus card profile |
| Geo | distance and speed since the last CP transaction ("impossible travel"); IP country vs. billing country | Request path plus profile |
| Device/identity (CNP) | device fingerprint age, cards per device, emails per card, BIN/IP risk | Online store |
| Merchant/entity risk | merchant fraud rate, BIN attack indicators, graph features (shared devices/IPs among cards) | Batch/nearline |

**Hard problems to design for:**
- **Freshness gap:** stream features lag by seconds, and card-testing bursts happen faster than that. Fix: keep short-window counters (1m/10m) updated **in the request path** with atomic Redis increments, and use Flink for the longer windows.
- **Train/serve skew:** define each feature once in a feature platform (Feast, Tecton, or in-house) and log the exact feature vector served with every decision. Train on those logged vectors where you can, and use point-in-time joins otherwise.
- **Event time:** use watermarks and handle late or out-of-order events. Deduplicate on transaction ID, because auth retries and reversals happen.

---

## 3. Decisioning

1. **Rules layer:** blocklists, sanctions, hard velocity caps, known attack signatures. Rules are versioned, analysts can deploy them in minutes, and they're tested against replayed traffic before going live.
2. **Model:** a gradient-boosted tree model (LightGBM or XGBoost) as the main model. It's fast (under 2 ms), handles tabular data well, and can be explained with SHAP for reason codes. You can add these later:
   - an anomaly or unsupervised score to catch new patterns
   - graph embeddings for fraud rings
   - a sequence model over the card's transaction history, served from precomputed embeddings
3. **Policy layer:** turns a calibrated probability into an action using **expected cost**:
   `decline if p(fraud) × amount × loss_rate > cost_of_false_decline (lost interchange + churn risk)`
   Thresholds are set per segment (CNP vs. CP, MCC, customer tenure). Step-up authentication (3DS/OTP) sits as a middle band between approve and decline.

---

## 4. Labels and training

- **Label sources:** chargebacks (the main source, but 30–120 days late), cardholder fraud claims (faster), and analyst decisions.
- **Label maturity:** only train on transactions old enough for labels to have arrived, for example more than 90 days. Track recent performance with proxy labels such as early disputes.
- **Selection bias:** declined transactions never get a true label. Approve a tiny random sample of low-to-mid-risk would-be declines (with amount caps) as a **control group**, or use reject inference.
- **Class imbalance:** fraud is roughly 0.05–0.2% of transactions. Use class weighting or downsampling, then **recalibrate** so the probabilities are true again (isotonic or Platt scaling).
- **Validation:** out-of-time splits only, never random splits. Grouping by card prevents leakage.
- **Retraining:** start monthly and move to weekly if drift shows up. Rules cover the gap when new attacks appear.

---

## 5. Evaluation and rollout

- **Offline metrics:** PR-AUC, recall at a fixed decline rate, **dollar-weighted** fraud caught, false-positive ratio (declined good transactions per fraud caught), and results by segment.
- **Rollout steps:** replay backtest → **shadow mode** (score everything, act on nothing, 2–4 weeks) → canary at 1–5% of traffic → champion/challenger at full scale.
- **Business KPIs:** fraud loss in basis points of volume, false decline rate, step-up completion rate, analyst queue SLA.

---

## 6. Reliability and operations

- **Latency budget (p99):** feature fetch 10 ms, model 3 ms, rules 2 ms, overhead 5 ms, with 30 ms of headroom.
- **Timeouts and fallback:** a hard timeout on the scorer, then rules only, then default approve under a capped amount. Every fallback is logged and alerted on.
- **Availability:** multi-AZ and active-active across regions. The online store is replicated. Kafka runs with acks=all, and Flink uses exactly-once checkpoints.
- **Monitoring:**
  - p50/p99 latency and timeout rate
  - feature null rate and drift (PSI)
  - score distribution shift
  - **decline rate anomaly alerts**, which catch both attacks and broken models
  - delayed precision and recall once labels mature
- **Kill switches:** per model, per rule, and per segment, with a fast rollback to the previous model version.

---

## 7. Security and compliance

- Tokenize the full card number (PAN) at the edge so the fraud stack sees only tokens. This keeps most of the stack out of PCI scope.
- Encrypt data in transit and at rest. Use role-based access, keep audit logs of every decision with the model, rule, and feature versions, and set retention policies.
- Generate SHAP-based reason codes for each decline, for disputes, regulators, and analysts. Also run fairness checks on proxy attributes.

---

## 8. Phased delivery

| Phase | Weeks | Deliverable |
|---|---|---|
| 1 | 0–6 | Event logging to Kafka and the lakehouse, label pipeline, rules engine on the auth path, latency harness |
| 2 | 6–12 | Flink velocity features, online store, first GBDT model in shadow mode, monitoring dashboards |
| 3 | 12–18 | Canary, then full rollout; cost-based thresholds; step-up band; case management feedback loop |
| 4 | 18+ | Device and graph features, anomaly model, automated retraining, control group for unbiased labels |

---

## Open questions for you
1. Are you an issuer, an acquirer/PSP, or a merchant? This decides which signals you have, such as full card history vs. device and session data.
2. What are your peak TPS and your actual latency budget inside the auth flow?
3. What do you already run (Kafka/Flink vs. Kinesis, cloud provider, any existing rules engine)?
4. How many labels do you have, and how quickly do chargebacks arrive?

Once you answer these, I can turn any section into a detailed design, such as the Flink job topology, the feature schema, or the threshold policy.
