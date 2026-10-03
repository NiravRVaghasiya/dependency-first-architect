# BUILD PLAN: Real-time card-transaction fraud detection

**Assumptions** (any of these can be overridden):
- We are the issuer or processor, and we score each authorization inline before it is approved.
- An authorization switch already exists, along with an incumbent control (network scores and/or legacy rules).
- Traffic is a mix of card-present and card-not-present.
- The switch gives us **≤100 ms** to score. This is the first number to confirm in Phase 0.

---

## 1. Classification
- **What:** A service that scores each card authorization for fraud within the auth-path latency budget and returns approve, step-up, review or decline. Behind it sit a streaming feature pipeline, a label (ground-truth) pipeline and a retraining loop.
- **Type:** A mix of software and infra, plus classical ML (gradient-boosted trees). It is **not** generative or agentic AI.
- **Dominant constraint:** **Latency.** It is a hard p99 limit inside the auth path. Correctness (fraud caught vs. good customers declined) and compliance (PCI DSS v4.0, model risk governance) come close behind.
- **Worst failure:** The scorer sits in the auth path, and a bad deploy, bad model or dependency outage causes **mass false declines**, or it **fails open silently** so fraud passes while every dashboard looks green. The design has to make both failures impossible to miss and limited in scope.

## 2. Tradeoff gates (resolved up front)

| Decision | Default (chosen now) | Flip condition |
|---|---|---|
| Consistency vs availability | **Availability on the scoring path.** Feature reads may be up to about 2 s stale (velocity counters are eventually consistent). **Strong consistency for the decision log:** every decision is durably recorded and replayable. | Card-testing or burst attacks start exploiting the staleness window, i.e. measurable losses from transactions that fell inside it. Then switch to single-writer, strongly consistent per-card counters (partitioned by card token). |
| Monolith vs services | **Two deployables, split by latency class.** (1) An inline scoring service: a modular monolith holding rules, model and decision policy. (2) An async streaming and feature pipeline. Nothing finer-grained. | Separate teams need independent release cycles for rules vs. model, or rules evaluation needs to scale independently. |
| Sync vs async | **Sync only for the decision.** Everything else is async through Kafka: feature updates, decision logging, case creation, labels, retraining. | The inline model can't hold p99 within budget at peak. Then keep rules inline and move the model to near-real-time post-auth scoring, which triggers alerts and blocks the card for the *next* transaction. |
| **Failure mode** (fail-open vs fail-closed) | **Fail to the existing fallback.** On timeout or error the switch uses incumbent rules plus network stand-in. If those are also unavailable, approve below a per-MCC amount ceiling and decline above it. Never fail closed globally. | Risk appetite changes (e.g. a regulator or loss event) and someone formally accepts a fail-closed policy for specific segments. |
| Build vs buy | **Buy the infra, build the decisioning.** Managed Kafka (MSK or Confluent), managed Flink, Redis/ElastiCache (or Aerospike), Iceberg on S3 and MLflow. Build features, rules, model and policy in house. Network scores (Visa VAA, Mastercard Decision Intelligence) go in as *features*. | After 6 months of shadow testing, the in-house model doesn't beat the incumbent plus network score at the same decline rate, or the team is too small to run on-call. Then buy a vendor scorer (Featurespace, Feedzai) and keep the substrate. |
| Feature computation model | **One feature definition, streaming first.** The same Flink logic writes the online store and is replayed for offline backfill, so training and serving cannot drift apart. | Replaying history becomes too expensive (e.g. a full backfill takes more than 24 h). Then add a batch path that is checked by an automated online/offline comparison. |
| Model family | **GBDT (LightGBM/XGBoost).** CPU inference under 5 ms, SHAP reason codes, defensible for model-risk review. | A sequence or graph model shows material recall lift at a fixed false-positive rate in shadow testing *and* fits the latency budget. |
| (ML) Hosted model API vs in-process | **In-process inference**, using a Treelite/ONNX artifact embedded in the scorer. There is no network hop and no third-party API in the auth path. | Model size or GPU needs exceed what the scorer pods can hold. Then use a co-located sidecar, still never a remote API. |
| (AI) Prompt+RAG vs fine-tune | **N/A.** No LLM is in the decision path. | Revisit if an LLM analyst copilot is pulled forward (see §7). |
| Data-privacy / PCI boundary | **PAN is tokenized before it reaches the scorer.** The fraud system sees only token, BIN and last-4, which keeps it out of the cardholder-data environment (CDE) where possible. | A feature truly needs raw PAN (rare, since BIN covers most needs). That part then enters PCI scope with a separate CDE segment. |

## 3. Walking skeleton (Phase 0)

**The one real request:** a live production authorization goes from the auth switch to the scorer. The scorer returns `{decision, score, reason_codes, rule_version, model_version}`. It runs **inline but non-enforcing**: the switch logs our answer and keeps its own decision. This is the only safe way to get the real latency figure on day one.

**Tiers crossed:**
1. **Auth switch**: a new client call with a **30 ms hard timeout** and a circuit breaker, behind a feature flag with a traffic % dial (1% → 100%).
2. **Scoring service** (gRPC, Kubernetes, 2 availability zones), with a minimal canonical request schema v0.
3. **Online store** (Redis): read one feature, the card token's count of transactions in the last hour.
4. **Decision**: one hard-coded velocity rule (e.g. more than 10 auths per hour means "would decline") and a constant model placeholder.
5. **Event bus**: the decision event goes to Kafka through a non-blocking producer with a bounded local buffer, so the hot path never waits on Kafka.
6. **Stream processor**: a Flink job consumes transaction events, updates the Redis velocity counter and sinks decisions to an Iceberg decision log.
7. **Dashboard**: Grafana.

**Deploy:** Terraform for infra. CI builds an image pinned by digest. GitOps (Argo CD) rolls it out. The switch flag starts at 1%.

**Logged and monitored from day zero:**
- An OpenTelemetry trace ID is carried from the switch through the scorer and Redis, into the Kafka header, and on to Flink.
- Metrics: p50/p99/p99.9 latency, timeout rate, would-decline rate, Kafka consumer lag, and Redis error rate.
- Alerts: p99 > 30 ms, timeout rate > 0.1%, would-decline rate moving ±3σ from baseline, consumer lag > 30 s.

**Exit check:** 100% of production auths traverse the path. One transaction can be followed end to end in a single trace, from switch through scorer to Redis to Kafka to Flink to Iceberg. Real p99 is measured and confirmed under budget. Killing the scorer pods causes **zero** change to auth outcomes, which proves the fallback works.

## 4. Phases (ordered by dependency; within each phase, widest blast radius first)

**Phase 1: Data contracts and event substrate**
- Unlocks: every feature, label and model downstream. They all depend on these schemas and entity keys.
- Depends on: Phase 0's proven path (schema v0 and the real event flow).
- Tasks:
  1. **Entity identity model** (widest blast radius): card token, account, customer, merchant ID + MCC, device/fingerprint, and how they join.
  2. Canonical `TransactionEvent`, `DecisionEvent` and `LabelEvent` schemas in Protobuf with a schema registry. Backward-compatibility checks are enforced in CI.
  3. Event-time semantics: timestamps and a late/out-of-order policy (watermarks, e.g. 5 s allowed lateness).
  4. Historical backfill: at least 12 months of auth history loaded into Iceberg in the same schema.
  5. A replay tool that re-emits history through the live topic shape.
- Exit check: a contract test suite runs in CI. A schema-breaking change is rejected automatically. Replaying one day of history reproduces the live decision log byte for byte for the Phase 0 rule.

**Phase 2: Feature platform (online and offline match)**
- Unlocks: a model or rule can only use features that serve identical values online and offline.
- Depends on: Phase 1 (entity keys, event-time semantics, replay).
- Tasks:
  1. **Windowing and aggregation semantics** (widest). Sliding vs. tumbling windows, and the key per window. Every feature inherits these.
  2. Online store SLO: p99 read < 5 ms, sized for 3× peak traffic per second.
  3. Offline point-in-time joins via replay, so no future data leaks into training.
  4. Feature registry: name, owner, definition, version, and whether it is PII.
  5. Individual features (leaf work): velocity by card/merchant/device over 1 m/1 h/24 h; amount z-score against the card's history; time and distance since the last card-present transaction; merchant chargeback rate; new-device flag; network score.
- Exit check: an online/offline comparison job on 1% of transactions shows < 0.5% feature mismatch. Online read p99 < 5 ms at 3× peak in a load test.

**Phase 3: Labels and ground truth** (can run in parallel with Phase 2; both depend only on Phase 1)
- Unlocks: training and any honest measurement of fraud caught.
- Depends on: Phase 1 (`LabelEvent` schema, entity keys).
- Tasks:
  1. **Label definition and maturity policy** (widest). What counts as fraud: chargeback reason codes, confirmed customer disputes, analyst dispositions. Chargebacks arrive 30–120 days late, so labels mature at 90 days.
  2. Ingestion from the chargeback, dispute and case systems.
  3. Selection-bias accounting: declined transactions have no outcome, so they are flagged and handled in evaluation.
  4. A labeled training set with known maturity per cohort.
- Exit check: monthly labeled fraud loss reconciles with Finance's reported fraud loss within ±5%.

**Phase 4: Decision policy, fallback chain and rules enforcement**
- Unlocks: the first *enforced* fraud control, with rules before any model.
- Depends on: Phase 0 (latency proven, fallback proven) and Phase 2 (trustworthy features).
- Tasks:
  1. **Decision policy contract** (widest). Score/rule outcomes map to approve, step-up, review or decline. Reason-code taxonomy. Precedence between the network score, rules and model.
  2. Fallback chain is formalized and tested: scorer → incumbent rules → network stand-in → MCC amount ceiling.
  3. Rules engine: rules are versioned config in git, hot-reloaded and signed, with a per-rule kill switch.
  4. Shadow comparison against the incumbent, then **narrow enforcement**: one BIN range or 1% of traffic, with automatic rollback.
  5. Individual rules (leaf work): card-testing patterns, impossible travel, high-risk MCC + new device.
- Exit check: in enforced canary traffic, decline rate and loss rate are no worse than the incumbent, and the rollback flag returns to the incumbent in < 60 s.

**Phase 5: Model v1 (GBDT), shadow then champion/challenger**
- Unlocks: ML-driven risk scores.
- Depends on: Phase 2 (features), Phase 3 (mature labels), Phase 4 (a policy that can use a score).
- Tasks:
  1. **Evaluation metric and threshold policy** (widest, because it defines "better"). Dollar-weighted recall at a fixed false-positive ratio (e.g. 1 false positive per 10 frauds), plus decline rate for good customers.
  2. Reproducible training pipeline: pinned data snapshot (Iceberg snapshot ID), pinned code and environment, seeded runs.
  3. Model registry (MLflow) with signed artifacts and a model card for model-risk review.
  4. In-process serving integration, held under the latency budget.
  5. Shadow scoring on 100% of traffic, then a champion/challenger traffic split.
- Exit check: on matured labels, the model beats rules alone at an equal decline rate. Inline p99 stays within budget at 3× peak. Model-risk sign-off is recorded.

**Phase 6: Human review and customer confirmation**
- Unlocks: "review" and "step-up" outcomes become real. Analyst dispositions become fast labels.
- Depends on: Phase 4 (policy outcomes) and Phase 5 (scores worth reviewing).
- Tasks:
  1. **Disposition schema feeding `LabelEvent`** (widest, because it shapes label quality).
  2. Case queue with SLA and priority (score × amount).
  3. Customer confirmation: 3DS step-up for card-not-present, and push/SMS "was this you?" for post-auth.
  4. Analyst UI (leaf work).
- Exit check: median time from review decision to disposition < SLA. Dispositions show up in the label store within 1 h.

**Phase 7: Monitoring, drift and retraining loop**
- Unlocks: lasting performance as fraudsters adapt.
- Depends on: Phase 5 (a model to monitor) and Phase 6 (fast labels).
- Tasks:
  1. **Performance and drift alerting** (widest): PSI on top features, score distribution, and decline rate by segment (BIN, MCC, country).
  2. Two performance views: early (analyst and customer labels) and mature (chargebacks).
  3. Scheduled retraining. Promotion is **gated by a human** model-risk approval, never automatic.
  4. Attack playbooks: emergency rule push for new patterns.
- Exit check: an injected synthetic drift (a shifted feature distribution) fires an alert within 15 min. A retrained challenger can go from training to approved canary in ≤ 1 day.

**Phase 8: Scale and multi-region hardening** (harden only what has proven to carry load)
- Unlocks: peak-season traffic and region-loss survival.
- Depends on: all prior phases being proven in production.
- Tasks:
  1. **Active-active scoring across 2 regions** (widest), with a cross-region velocity-counter strategy.
  2. Peak load tests at 5× Black Friday traffic.
  3. Scheduled chaos game days.
  4. Cost tuning (leaf work).
- Exit check: losing a region drops auth throughput by 0% and fraud-control coverage stays ≥ 99%.

## 5. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | PAN tokenized before scorer; mTLS between switch and scorer; secrets in KMS/Vault; scorer on its own network segment outside CDE | End-to-end OTel trace; latency, timeout, decision and lag metrics; alerts | Terraform + GitOps; image pinned by digest; flag config in git | 30 ms timeout + circuit breaker; non-enforcing; pod-kill test proves zero impact on auths |
| 1 | Schema fields tagged by PII class; registry write access restricted; tokens only in the lake | Schema-violation and dead-letter-queue counters; contract-test results in CI | Versioned schemas; byte-identical replay of history | Dead-letter queue for malformed events; replay supports recovery |
| 2 | PII features access-controlled in the registry; no raw PAN in any feature | Online/offline mismatch metric; feature freshness and read latency | One definition feeds online and offline; backfill by replay | Missing feature → documented default value plus a counter; Redis replicas across AZs |
| 3 | Label sources reached through least-privilege service accounts; dispute data retention policy | Label arrival lag and maturity dashboards; Finance reconciliation report | Labeled sets pinned to Iceberg snapshot IDs | Late or duplicate labels handled idempotently (upsert by transaction ID + source) |
| 4 | Rules signed, reviewed via pull request, two-person approval to enforce; audit log of every rule change | Hit rate and decline contribution per rule; canary vs. control dashboards | Rules versioned in git; rule_version stamped on every decision | Per-rule kill switch; automatic rollback; fallback chain tested in CI |
| 5 | Signed model artifacts verified at load; training data access audited; model card | Score distribution, shadow-vs-champion deltas, inference latency | Pinned data snapshot, code commit, environment lock and seed; registry lineage | Model fails to load → stay on previous version; timeout → rules only |
| 6 | Analyst RBAC; PAN masked in UI; every analyst action audited; 3DS/OTP anti-phishing copy | Queue depth, SLA breaches, disposition mix | Disposition schema versioned; decisions replayable to the queue | Queue backpressure; cases not reviewed in time fall back to policy defaults |
| 7 | Promotion requires human model-risk approval; emergency rule push needs break-glass approval with audit | PSI drift, segment decline-rate alarms, early vs. mature performance | Every retrain reproducible from registry lineage | Automatic revert to the last approved model on a performance alarm |
| 8 | Per-region KMS keys; cross-region traffic encrypted; annual PCI scope review | Per-region SLOs; global dashboard; game-day reports | Identical infra modules per region | Active-active failover; regional chaos drills; peak load proven |

## 6. AI layer
**N/A as defined (no LLM, no agent).** This is a classical ML system, so the LLM-specific sublayers do not apply: prompt-injection defense, retrieval, LLM memory, orchestration and routing. Their ML equivalents are already in the plan:
- Adversarial-input defense: tokenization, feature-validation defaults, rule kill switches (Phases 1, 2, 4).
- Latency and cost budget: the 30 ms gate in Phase 0.
- Human-in-the-loop: analyst review (Phase 6) and human model promotion (Phase 7).
- Model access: in-process, swappable artifact (Phase 5).
- Feedback: labels and retraining (Phases 3 and 7).

This section becomes active if the LLM analyst copilot in §7 is pulled forward.

## 7. Deliberately deferred
- **Graph and fraud-ring features (or a GNN).** Pull forward when ring or mule fraud exceeds about 15% of losses.
- **Deep sequence models.** Pull forward when GBDT recall plateaus across two retrains and a shadow test shows material lift within budget.
- **Online or continuous learning.** Pull forward when attack adaptation outpaces the daily retrain (loss spikes before the next model ships).
- **Strongly consistent per-card counters.** Pull forward on the consistency gate's flip condition.
- **Bought feature-store product (Feast/Tecton).** Pull forward when there are more than about 100 features or more than one team producing features.
- **Self-serve rules UI for fraud ops.** Pull forward when rule changes exceed about 10 per week and pull-request review becomes the bottleneck.
- **Exploration holdout** (letting a tiny, capped sample of would-be declines through to measure true outcomes). Pull forward when selection bias measurably distorts evaluation, and only with risk sign-off.
- **LLM analyst copilot** (case summarization). Pull forward when analyst throughput is the binding constraint. This would activate the full AI layer in §6.
- **More than 2 regions, and acquirer/merchant-side fraud.** Pull forward on new market entry or a product-scope change.
