# BUILD PLAN: Real-time card-transaction fraud detection

## 1. Classification and constraints
- **What:** A scoring pipeline that sits inside card authorization. Each authorization gets a fraud score, reason codes and an action within the auth time budget. A streaming path keeps per-card, per-merchant and per-device state current, and a separate offline loop trains models on labelled outcomes (chargebacks, fraud claims, analyst decisions).
- **Type:** Software, infrastructure and machine learning (a tabular classifier, not an LLM or agent). It is new work added to an existing authorization system. It is not a small build: it handles cardholder data under PCI DSS, it makes money decisions, and it has R3 decisions.
- **Dominant constraint:** Tail latency inside the auth window, with decision correctness close behind. Compliance (PCI DSS, privacy law) adds hard dependencies.
- **Worst failure:** Legitimate cardholders get declined at scale, either because the scorer fails closed (timeout or outage) or because a bad model or threshold declines good traffic. The harm happens within minutes and cannot be undone: stranded cardholders, lost revenue, regulatory attention. The guard is V2.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency (scoring slice of auth processing) | p99 ≤ 50 ms; the auth host gives up at 80 ms and falls back | ASSUMPTION. Basis: issuer processing is typically a few hundred ms inside the network's multi-second response window. Check it against the auth host BASELINE measured in Phase 0 | V5 |
| Throughput | 3× peak authorizations per second, sustained for 1 h | ASSUMPTION for the headroom (basis: seasonal peaks). The peak itself is a BASELINE measured in Phase 0 | V5 |
| Availability | A fraud-service failure causes zero failed authorizations (falls back to the existing decision); decision service SLO 99.95% | Zero failed auths: design requirement drawn from the worst failure. 99.95%: ASSUMPTION | V2 |
| Detection quality | Lift over the current rules at the same decline rate (see V6), with a false-positive ratio no worse than today's rules | Current rules: BASELINE (Phase 2). Lift target: ASSUMPTION ≥ 10% more fraud value caught; the fraud-risk owner confirms it in Phase 1 | V6, V8 |
| RPO / RTO | Decision log RPO ≈ 0. Decision service RTO 5 min, because fallback covers it. Online feature state rebuilt within 1 h by replaying the event stream | ASSUMPTION. Basis: every enforced decision has to be reproducible for disputes and model review | Phase 1, Phase 3 drills |
| Stream freshness | Velocity aggregates lag ≤ 1 s p99 | ASSUMPTION. Basis: card-testing bursts happen at second scale | V5 |
| Storage cost / retention | UNKNOWN. Missing input: how long decision and feature logs must be kept. Supplied by Legal/Compliance; needed in Phase 1 to set topic and table retention | UNKNOWN | Phase 1 |
| Operational complexity | One new 24/7 tier-1 on-call rotation covering the decision service and the stream jobs | ASSUMPTION | Phase 1 runbook drill |

**Missing inputs** (most plan-changing first):
1. **Where we sit in the flow, and inline vs post-auth.** I've assumed we are an issuer or issuer-processor scoring inline in authorization, so a decline is possible. If we are instead a merchant, acquirer or PSP, or post-auth alerting is acceptable, V2 and V5 stop gating exposure, the sync-vs-async row flips, and Phase 4 becomes asynchronous alerts plus card blocks. The plan gets much simpler.
2. **What exists today.** I've assumed (ASSUMED) an auth host, a rules engine already in production, and network risk scores we can use as model inputs (features). If no rules engine exists, there is no current decision to fall back to and V2 has to define a different fallback (approve, or the network's stand-in).
3. **From the auth platform team:** peak TPS, the time slice fraud is allowed, and whether a change slot is available for the integration hook.
4. **From the fraud-risk owner:** lift target and tolerable false-positive or decline rate.
5. **Data access:** at least 12 months (ASSUMPTION, to cover seasonality) of authorization history plus labels, and approval to use it.
6. **Jurisdiction and regime:** whether model-risk guidance such as SR 11-7 applies, and whether GDPR or similar rules on automated decisions apply. Either one adds obligations before enforcement (V7).
7. **Whether a cardholder step-up channel exists** (app push, one-time code, 3DS). Its absence affects the deferred items only.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 hook in the auth host | Auth platform team's change approval and change slot (ASSUMED) | organizational | deployed (Phase 0) |
| Phase 0 service inside the cardholder-data environment (CDE) | Security and PCI scoping of new components; network segment provisioned | organizational | deployed (Phase 0) |
| Production traffic mirror (Phase 1) | V1 (tokenization, no sensitive authentication data, segmentation, telemetry scan) | risk/security | exposed |
| Historical dataset and training (Phases 2 and 4) | Data-access approval; the privacy officer's sign-off in V1 | organizational + risk/security | built (dataset) / committed (training) |
| Schema hardening; feature specification | V3 (identifiers join to labels) | validation | specified / hardened |
| Training on backfilled features | V4 (online and offline features match) | validation | specified |
| Inline call on production traffic | V2 (falls back safely), V5 (latency at load) | risk/security + validation | exposed |
| V5 and V6 thresholds | Peak TPS and auth time budget (auth team); lift target (risk owner) | organizational | V5 and V6 cannot pass until supplied (Phase 1 task) |
| Enforcement (Phase 5) | V6 (model worth running), V7 (model governance and privacy sign-off), fraud-ops analyst staffing, case-management integration | economic + organizational | exposed / committed |
| Measuring fraud caught | Label maturity: chargebacks arrive 30–120 days after the transaction | validation (lead time) | V6 out-of-time test; V8's final reading |
| Widening enforcement | V8 at each stage | risk/security | exposed |

All seven kinds bind. Structural and runtime dependencies are listed only where they surprise: the decision service is a runtime dependency of authorization itself, and V2 neutralizes that.

Dependents of the existing system, found in Phase 0: case management fed by rules-engine alerts (ASSUMED), fraud and regulatory reporting off the rules engine (ASSUMED), and network stand-in processing when the issuer is unreachable (ASSUMED). None of them changes, because the rules engine stays in place throughout.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Sync vs async | R2: the auth integration contract needs coordinated work across teams | Hybrid. A synchronous gRPC score call inside authorization with an 80 ms hard timeout. Kafka carries events, state updates, the decision log and labels asynchronously | We're in the auth path and the fraud slice is ≥ 50 ms (Missing inputs 1 and 3) | V5 | V5 fails after hot-path reduction, or we aren't inline. Then switch to post-auth async alerts plus card blocks |
| Failure behavior (fail open vs closed) | R3: reverting is fast, but even a short time wrong does harm that can't be undone (mass declines, or open season for fraud) | Fall back to the current decision (existing rules plus auth host). Never decline because the scorer timed out or errored | The existing rules are an acceptable floor during outages | V2 | V2 fails. Or attackers are seen exploiting timeouts; then fall back to stricter rules for high-risk merchant categories only |
| Consistency vs availability (online features) | R2: changing which counters sit in the request path means redesigning the store and re-running V4 | Availability. Serve with the best available features (streaming updates, eventual consistency). A few critical counters per card (1 min / 1 h counts) are incremented atomically in the request path. A missing feature becomes a sentinel value the model was trained on | Sub-second staleness doesn't hide card-testing bursts | V5 plus a card-testing replay (Phase 3 exit) | The replay misses bursts. Then move more counters into the request path, within V5's budget |
| Build vs buy | R2: the decision-API seam lets a vendor scorer plug in behind the same contract | Build the pipeline, decision layer and model. Buy or consume network risk scores as features. Keep the existing rules engine as the incumbent baseline (champion) | An in-house model adds lift over rules plus network score | V6 | V6 fails. Then buy vendor scoring (e.g. Featurespace, Feedzai, FICO) behind the same API and keep the pipeline for orchestration |
| Data-privacy boundary | R3: involves cardholder data, PCI scope, and training on personal data | The pipeline sees only a PAN token plus BIN. Sensitive authentication data (CVV, track data, PIN data) is dropped at ingress and never stored. Features are minimized. Everything runs in a segmented CDE zone. Telemetry carries the same classification as the data. Training happens only for an approved purpose in an isolated analytics environment | Tokenization at the auth host is available | V1 | V1 fails, or a reviewer requires otherwise. Then train inside the issuer's existing CDE analytics environment and drop the fields at issue |
| Data model and identifiers | R3: months of labels keyed wrong means relabelling and retraining | A canonical Avro event (mapped from ISO 8583) in a schema registry. Join keys: PAN token + network transaction ID + RRN/STAN + date. A decision ID is stored on the auth record | Chargebacks and claims carry these keys | V3 | V3 fails. Then add the acquirer reference number (ARN) as a key and re-plan the schema before hardening |
| Feature computation (training/serving skew) | R2: changing it means a backfill and a retrain | One Flink job definition runs both live and in backfill mode. Every scored feature vector is also logged (log-and-wait) for future training | Replaying history through Flink reproduces the live values | V4 | V4 fails. Then train only on logged features and wait 8+ weeks for them to accumulate (pushes Phase 4 back) |
| Coexistence and system of record | R2: changes the contract with the auth host and case management | The auth host stays system of record for the auth decision. The existing rules stay as champion and fallback; the new scorer is the challenger. A permanent 2% holdout runs on the champion only | The rules engine stays licensed and running | V2, V8 | Proposal to decommission the rules engine (deferred; needs its own gate) |

**R1 defaults:** service shape → one decision-service deployable (features, model and policy in one process, with no extra network hops on the hot path), plus separate Flink jobs and offline training; model family → gradient-boosted trees (LightGBM), served in-process via ONNX or Treelite on CPU; online store → Redis Cluster or Aerospike; streaming → Kafka plus Flink (or the organization's existing bus); deploy target → Kubernetes in the CDE segment.
**N/A:** prompt+RAG vs fine-tune (no LLM); hosted vs self-hosted model API (the model runs in-process inside the CDE, with no external inference); migration strategy (nothing is migrated; the system is added alongside).

## 4. Walking skeleton (Phase 0)
- **Request:** A real authorization on an **issuer test-BIN card** (no cardholder account) at a test merchant goes through the production auth host. The auth host calls the decision service, which returns `{decision_id, score, action=APPROVE, reason=SKELETON, model_version=stub}`. The auth host logs the response next to its own decision and **ignores it**. The real response to the network is the auth host's normal approval.
- **Tiers:** auth host hook (behind a flag, allow-listed to test BINs) → gRPC over mTLS → decision service (stub score: a constant plus one real feature read) → online store (one counter, incremented atomically) → Kafka decision topic → decision-log sink (Iceberg table) → back to the auth host.
- **Day zero:**
  - Deployed by CI/CD (signed image, Helm canary) into the CDE segment.
  - The auth host's trace ID is propagated through OpenTelemetry across every tier.
  - Prometheus metrics: request rate, errors, latency, timeout rate.
  - An alert fires when timeouts exceed 1% over 5 min.
  - Rollback is `helm rollback`, or the auth-host flag off, which stops the call entirely.
- **Also in Phase 0:**
  - Record a BASELINE of auth host processing latency and peak TPS, taken through the auth team's own change process.
  - Confirm or refute the ASSUMED dependents (§2).
  - Open the organizational items with lead time: auth-team change slot, PCI scoping, data-access request, privacy officer review, model-governance intake, fraud-ops staffing.
- **Who can reach it:** only allow-listed test BINs behind the flag. No cardholder traffic until V1 passes.

Exit check (V0): a real test-BIN authorization succeeds with one trace across every tier. A deploy and a rollback succeed through the pipeline. An injected decision-service kill fires the alert, and the auth host still answers through its existing path. First latency values are recorded as near-zero-load baselines.

## 5. Phases

- **Phase 1: Contracts, privacy boundary and fail-safe guardrails**
  - Unlocks: production traffic mirrored into the pipeline; inline calls made safe.
  - Depends on: V0. V1 must pass before the mirror is turned on.
  - Tasks:
    1. Canonical transaction event and decision API contract (score, action list, reason codes, model and policy version, decision ID), keyed as in §3, with compatibility checks in CI.
    2. Tokenization and dropping of sensitive authentication data at ingress; field classification in the registry.
    3. In the auth host client: fall back to the existing decision on timeout or error, and an allow-list of actions (this phase allows APPROVE only).
    4. A decline-rate circuit breaker that forces champion-only.
    5. Two-person approval on any threshold change or enforcement toggle, enforced by the config pipeline rather than the model.
    6. V1 review and data-loss-prevention scan.
    7. Asynchronous mirror of 100% of production auth events to Kafka, with no response path.
    8. V2 fault injection.
    9. Collect from the owners: peak TPS, fraud time slice, lift target, false-positive tolerance, retention.
  - Rollback: mirror producer flag off; schema versions stay backward compatible.
  - Exit check: V1 and V2 pass; the mirror runs 7 days with consumer lag inside V5's freshness target; on-call runbook drill done.

- **Phase 2: Historical data and labels**
  - Unlocks: a trustworthy training set and the champion BASELINE.
  - Depends on: Phase 1 contract; V1 (privacy officer sign-off); data-access approval.
  - Tasks:
    1. Ingest ≥ 12 months of authorization history.
    2. Ingest labels: chargebacks with fraud reason codes, fraud claims, analyst dispositions.
    3. Run V3, then harden the schema.
    4. Measure the champion on mature labels (decline rate, fraud value caught, false-positive ratio) and record them as BASELINE.
  - Rollback: revert dataset snapshots; no production effect.
  - Exit check: V3 passes; BASELINE metrics published to the fraud-risk owner.

- **Phase 3: Features (online lookups and streaming state)**
  - Unlocks: a model-ready feature vector at production latency.
  - Depends on: V3; the Phase 1 mirror.
  - Tasks:
    1. Flink streaming aggregates (card, merchant and device velocity; card behavior profile) feeding the online store.
    2. Daily batch features (card tenure, customer profile, merchant risk).
    3. The request-path counters from §3.
    4. The same Flink job run in backfill mode over history.
    5. V4.
    6. V5 load test with full features and a dummy model.
    7. Card-testing attack replay.
  - Rollback: the decision service pins the previous feature-set version.
  - Exit check: V4 and V5 pass; the replay flags injected card-testing bursts; online state rebuilt from Kafka replay within the RTO.

- **Phase 4: Model and inline shadow**
  - Unlocks: evidence that the model is worth enforcing.
  - Depends on: V1 (training), V2, V4, V5.
  - Tasks:
    1. Train the gradient-boosted model on the backfill with an out-of-time split; reason codes from top SHAP contributions (the features that pushed the score most).
    2. Signed model registered in the registry.
    3. Offline backtest (V6a).
    4. Deploy the model in-process.
    5. Turn on the inline synchronous call in shadow mode on 1%, then 10%, then 100% of production auths. Decisions are logged and ignored.
    6. At least 2 weeks of shadow running (V6b).
    7. Submit the model package to V7.
  - Rollback: inline flag off, back to mirror only.
  - Exit check: V6 passes; shadow p99 holds V5's target in production.

- **Phase 5: Enforcement canary with the analyst loop**
  - Unlocks: real fraud reduction.
  - Depends on: V2, V6, V7; fraud-ops staffing; case-management integration.
  - Tasks:
    1. Policy layer combining rules and model thresholds into an action from the allowed list: APPROVE, APPROVE+CASE, or DECLINE+CASE. Permanent card blocks stay analyst-only.
    2. Case queue in case management. If the queue overflows, transactions are approved with an alert, never declined.
    3. Randomized enforcement on 1%, then 5%, 25% and 100% of **one portfolio** (chosen with fraud-ops), then the other portfolios, keeping the 2% holdout.
    4. V8 at every stage.
  - Rollback: flag per stage back to champion-only, plus the automatic breaker. No point of no return, because the champion stays.
  - Exit check: V8 passes at 100% of the first portfolio.

- **Phase 6: Feedback and retraining**
  - Unlocks: keeping performance up as attackers adapt.
  - Depends on: Phase 5 (V8 at ≥ 25%); the Phase 2 label pipeline.
  - Tasks:
    1. Label review against poisoning: "friendly fraud" (cardholders disputing purchases they made), insider label edits.
    2. Drift monitors: PSI (population stability index) per feature and on the score.
    3. Monthly retrain producing a challenger that goes through shadow, then V6 criteria, then the V8 canary path. Never promoted automatically.
  - Rollback: registry pointer back to the previous model.
  - Exit check: one retrained challenger promoted end to end through that path.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | §4 skeleton run | The V0 exit check in §4 | Trace export, CI/CD run logs, alert record, baseline sheet in the repo `/evidence/v0` | Phase 1 | 0 |
| V1 | Pipeline components handling production cardholder data keep the privacy boundary in §3 | Data-flow review; segmentation test; DLP regex and Luhn scan of every sink (Kafka, Iceberg, logs, traces, metrics labels) over test-BIN and synthetic traffic; re-scan after 24 h of mirror | Sign-off by a named PCI QSA or CISO delegate, and by the privacy officer for processing and training purpose; zero PAN or sensitive-auth-data matches in either scan | Signed review records, segmentation test report, DLP scan reports in the GRC tool | Mirror (Phase 1) and training (Phases 2 and 4). If it fails: redesign the data flow per §3's flip | Starts 0, required before Phase 1 mirror |
| V2 | No failure of the pipeline causes a decline the existing system wouldn't have made, and a runaway decline rate is cut off | Fault injection in perf, then on production test BINs: kill the service, add 200 ms latency, always-decline model output, feature store outage, Kafka outage | In 100% of ≥ 1,000 injections per fault type, the auth host returns the champion decision inside its timeout. The breaker trips to champion-only within 60 s when the decline rate exceeds 2× BASELINE over 5 min (ASSUMPTION; basis: well above normal daily swings). Page within 2 min | Chaos run reports and breaker event logs in `/evidence/v2` | Inline shadow (Phase 4) and enforcement (Phase 5). If it fails: stay mirror-only and redesign the auth client | 1 |
| V3 | The chosen identifiers join decisions and transactions to fraud labels | Replay 6 months of authorizations plus chargebacks and claims through the join | ≥ 99.5% of fraud-coded chargebacks and claims join to exactly one transaction (ASSUMPTION; basis: unjoined labels bias training toward fraud types that are easy to join) | Join-rate report by label source in the data-quality store | Schema hardening and the Phase 2 dataset. If it fails: add ARN and re-plan the schema | 2 |
| V4 | Backfilled features equal the features served live | One production day: compare the Flink backfill with the logged served vectors | ≥ 99.9% of values match; counts exactly, floats to 1e-6 relative (ASSUMPTION) | Parity report per feature | Training on the backfill. If it fails: log-and-wait only, Phase 4 slips ≥ 8 weeks | 3 |
| V5 | The decision service with full features meets latency at peak, and the streams stay fresh | Load test in a production-sized perf env with replayed tokenized traffic, then confirmed during Phase 4 shadow | p99 ≤ 50 ms and stream lag ≤ 1 s p99 at 3× BASELINE peak TPS for 1 h (ASSUMPTION; §1). Thresholds confirmed by the auth team in Phase 1 | Load-test report and Grafana snapshots | Inline shadow. If it fails: trim the hot path; if it still fails, flip to async (§3) | 3 |
| V6 | The model adds enough lift to justify enforcing it inline | (a) Out-of-time backtest on the latest 3 matured months (labels ≥ 90 days old); (b) ≥ 2 weeks of inline shadow | At equal decline rate, ≥ 10% more fraud value caught than champion BASELINE (ASSUMPTION; risk owner confirms in Phase 1); false-positive ratio ≤ BASELINE; PSI < 0.1 between backtest and live scores | Backtest notebook output with data snapshot ID; shadow comparison report in the model registry | Phase 5; commitment to build. If it fails: buy vendor scoring (§3) | 4 |
| V7 | The model and decision policy may make automated declines on real cardholders | Model governance package: documentation, a check that no protected attributes or proxies are used, a decline-rate disparity check by region, reason codes; privacy officer check on automated-decision obligations for the confirmed jurisdiction | Pass/fail sign-off by a named model-governance reviewer and the privacy officer | Signed validation report in the model inventory | Phase 5. If it fails: fix what was raised, re-run V6 if the model changes | Starts 2, required before 5 |
| V8 | Enforcement cuts fraud without unacceptable harm to cardholders | Randomized enforcement vs the 2% champion holdout at each stage | Each stage runs ≥ 7 days. Extra decline rate ≤ +0.2 percentage points over control (ASSUMPTION). False-positive ratio (analyst-confirmed) ≤ BASELINE. Cardholder complaint contacts per 10k auths ≤ control +10% (ASSUMPTION). Fraud measured from claims; re-confirmed against chargebacks at 90 days | Per-stage canary report with sign-off by fraud-ops and the risk owner | Next stage. If it fails: roll the stage back by flag and retune thresholds | 5 |

## 7. Cross-cutting concerns (per phase)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Test BINs only; tokenized PAN; mTLS; secrets in Vault; CDE segment; least-privilege IAM | OpenTelemetry trace from the auth host; request-rate/error/latency metrics; timeout alert; decision log | Terraform, image digests, signed artifacts, everything through CI/CD | 80 ms hard timeout; flag off; rollback rehearsed |
| 1 | Sensitive auth data rejected at ingress; field classification; V1 DLP covers telemetry too; two-person approval on enforcement config | Decision and decline rates by action; breaker state; mirror lag | Schemas and API under compatibility tests | Fallback to champion and breaker (V2); Kafka `acks=all` plus local disk buffer |
| 2 | Role-based access to the analytics env; purpose-bound access; audited reads | Join rate (V3), null rates, label lag | Iceberg snapshots with lineage; dataset IDs pinned | Idempotent, re-runnable backfill |
| 3 | Store encrypted; no raw identifiers in features; input bounds on attacker-controlled fields (amount, merchant text) | Freshness, null rate and skew per feature (V4) | Feature definitions as versioned code | Store replica; degraded-feature mode; state-rebuild drill |
| 4 | Signed models; registry approval; isolated training env | Score distribution, PSI, shadow-vs-champion agreement | Each run pins snapshot ID, commit, seed and library versions | Champion stays warm; model rollback via registry pointer |
| 5 | Action allow-list enforced by the executor; analyst access control; two-person threshold changes | Canary-vs-control dashboards; complaint contacts; queue depth | Policy and thresholds as versioned config; every decision records model and policy version | Flag per stage; breaker; queue overflow approves with alert, never declines |
| 6 | Label review against poisoning; audited label edits | Drift alerts; performance by cohort; label maturity | Reproducible retrain pipeline; champion/challenger history | No auto-promotion; one-step rollback to the previous model |

## 8. AI layer (ML scorer, no LLM)

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Guardrail defense | Attackers control transaction fields and probe thresholds. Defenses: schema validation, bounded features, actions limited to the allowed list, breaker. The model only outputs a score | Phase 1 (bounds in Phase 3) | V2 |
| 2. Cost + latency budget | Hard timeout, CPU budget per score, scorer kept in-process | Phase 1 (budget validated in Phase 3) | V5 |
| 3. Human-in-the-loop gating | Two-person approval on thresholds and enforcement (Phase 1); analyst-only permanent blocks and case queue (Phase 5) | Phases 1 and 5 | V7, V8 |
| 4. Retrieval | Online feature lookup (batch profiles, merchant risk, network score) | Phase 3 | V4 |
| 5. Model access | Gradient-boosted model in-process via ONNX/Treelite; champion rules as fallback | Phase 4 | V6 |
| 6. Memory | Streaming per-card, per-merchant and per-device velocity and profile state | Phase 3 | V5 plus card-testing replay |
| 7. Orchestration | Policy layer: rules plus score mapped to an action | Phase 5 | V8 |
| 8. Routing | Not needed: one general model. Deferred (§10) | n/a | n/a |
| 9. Feedback | Reviewed labels, drift monitoring, gated retrain | Phase 6 | Phase 6 exit |

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Step-up actions** (app push, one-time code, 3DS challenge): pull forward once a cardholder contact channel is confirmed and V8 has passed at 100% of the first portfolio.
- **Segment-specific models and routing** (card-not-present vs card-present, debit vs credit): pull forward when a segment's backtest shows the general model ≥ 10% below a segment model.
- **Graph or ring features** (common point of compromise, mule networks): pull forward when fraud-ops asks for them or losses concentrate in linked accounts.
- **Sequence or deep models, GPU serving:** pull forward when gradient-boosted performance plateaus across two retrains and V5 has latency headroom.
- **Decommissioning the rules engine** (this is the point of no return): pull forward when it is proposed after ≥ 2 quarters at 100% enforcement. It needs a new gate: a rehearsed champion-off run, a named approver, and a defined way back.
- **Multi-region active-active decision service:** pull forward when the auth host itself goes multi-region or the 99.95% SLO is missed.
- **Cardholder-facing decline explanations:** pull forward if V7 finds a jurisdictional obligation.
- **LLM analyst copilot:** pull forward when analyst queue time becomes the bottleneck. It would need its own prompt-injection, cost and approval defenses before use.

PCI DSS v4.0 mapping:
- Requirement 3 (no sensitive authentication data after authorization, protecting stored account data) → V1 and the ingress drop.
- Requirement 1 (segmentation) → the CDE zone and V1's segmentation test.
- Requirement 6 (secure development) → CI/CD with signed artifacts.
- Requirement 10 (logging) → decision log and audited access.

These controls address those obligations. This mapping does not mean the system is compliant; that is for the QSA to decide.
