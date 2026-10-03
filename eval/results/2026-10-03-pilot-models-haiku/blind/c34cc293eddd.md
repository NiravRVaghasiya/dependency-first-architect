Now I'll execute the 10-step procedure to build the plan. Let me work through each step systematically.

---

# BUILD PLAN — Customer-Support RAG Chatbot

## 1. Classification and constraints

**What:** A customer-support chatbot that retrieves help-center documentation and answers questions using retrieval-augmented generation (RAG).

**Type:** AI/agentic + software; greenfield (new service); small build (one team, internal + customer-facing cohort, real data after control gates pass).

**Dominant constraint:** Correctness (hallucinations, false answers harm customer trust and support efficiency) and cost (LLM API calls accrue per turn and per resolved conversation).

**Worst failure:** The bot answers confidently with incorrect information, misleading customers away from correct solutions or into support loops.

**Budgets:**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency (time to first token, p95) | < 3 s | ASSUMPTION: typical for hosted LLM; customer support tolerates moderate delay | V0 (baseline), Phase 3 (target under load) |
| Latency (complete answer, p95) | < 8 s | ASSUMPTION: same basis | V0 (baseline), Phase 3 (target under load) |
| Throughput (concurrent users) | 10–50 concurrent | UNKNOWN: depends on support volume and whether internal staff use it first; validate before Phase 4 | Phase 3 exit check |
| Availability / SLO | 99% (uptime >= 30 min/month down) | ASSUMPTION: best-effort; human fallback available | Phase 4 (attainment) |
| AI inference cost | < $0.10 per resolved conversation | ASSUMPTION: rough parity with human handle time ÷ hourly cost; **must be validated before scaling** | V4 (meter shadow traffic; canary) |
| Storage cost | Negligible (docs + embeddings under 10 GB) | ASSUMPTION: typical help center | V2 (measure after embeddings) |
| Operational complexity | One service, one hosted vector store, one model API account | ASSUMPTION: two-person on-call rotation | Phase 3 exit check |

**Missing inputs:**

1. Help-center docs **format, size, and accessibility** — who owns them, how do we sync? (Answer needed before Phase 1.)
2. **Which LLM to use** (GPT-4, Claude, open-source hosted)? Cost, latency, and hallucination rates differ sharply (Tradeoff gate T1).
3. **Customer data sensitivity** — do docs contain customer names, account IDs, or PII? Data residency? Compliance (GDPR, SOC 2)? (Affects Phase 1 security baseline.)
4. **Support volume and resolution expectations** — what is "resolved"? When does a human take over? (Affects threshold in V4.)
5. **Team access and launch window** — who approves production deployment? Regulatory/legal review needed?

**Assumption for phase order:** Proceeding on the assumption that help-center docs exist, are owned by support/product, and contain no customer PII (only answers to common questions). If docs require HIPAA, PCI DSS, or GDPR consent, add organizational and risk dependencies to Phase 0. If no docs exist yet, Phase 1 shifts to building an indexable Q&A corpus.

---

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Prompt + system instructions | Injection guardrail design + cost cap | Decision + Risk/security | specified |
| Embedding model + chunking strategy | Help-center docs (format, size, content) | Structural + Decision | specified |
| Retrieval index version | Embedding model pinned | Structural | built |
| Cost per resolved conversation measurement | Shadow-traffic run (V4) | Validation + Economic | committed to scaling |
| Customer exposure (canary → full rollout) | Injection suite passes + cost per conversation validated (V4) + availability SLO achieved (V5) | Validation + Risk/security + Economic | exposed |
| Feedback loop tuning | Retrieval quality baseline (V2) + answer eval scores (V3) | Validation + Decision | hardened |

**Organizational:** Legal/product approval for LLM vendor + customer data terms (start early, Phase 1).

**No runtime or structural surprises** (modern LLM APIs and vector stores are plug-in abstractions).

---

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| **Prompt+RAG vs fine-tune** | R2 (fine-tune ties data + base model; revert by discarding, weeks of eval work) | **Prompt+RAG**: system prompt + retrieval with few-shot examples in context window | LLM context window (4k–128k tokens) is sufficient for help-center use; fine-tune helps only if retrieval noise is very high | V2 (retrieval hit rate) + V3 (answer quality); if hit rate < 0.80 or user satisfaction < 0.85, re-evaluate fine-tune | Retrieval quality plateaus below acceptable, or vendor API becomes unaffordable (cost spike) |
| **Hosted API vs self-host** | R3 (switching to self-hosted model requires infrastructure, scaling logic, and monitoring; data gravity at vendor if large volume) | **Hosted API** (e.g., OpenAI, Anthropic, AWS Bedrock): no infra ops, fast iteration, vendor-managed scaling | Hosted APIs offer sufficient latency/cost/quality for internal + canary; in-house team avoids ML Ops burden early | V0 (latency baseline meets < 3 s p95), Phase 3 (cost + availability at peak) | Inference cost exceeds budget after V4 canary, or availability SLO fails repeatedly; switching to self-host defers 4–6 weeks |
| **Sync retrieval vs async prefetch** | R1 (swappable at API layer) | **Sync retrieval**: retrieve on each query, keep embeddings in vector store; simple, no stale prefetch | System starts small (< 10 concurrent); latency budget is loose enough | V0 exit check (latency < 8 s p95 measured), Phase 3 (load test at peak) | Latency budget fails; shift to cached prefetch or retrieval service; V0 gates escalation |
| **Single model vs routing** | R1 (routing is a config layer) | **Single model**: one LLM endpoint; no routing logic overhead | Support Q&A is homogeneous; one model suffices initially | Phase 1 exit check (eval on 100 diverse customer questions); if quality varies widely by type, gate routing | Answer quality drops sharply for specific question types (e.g. billing vs technical); routing added in Phase 3 |
| **Stateless (per-turn) vs conversation state** | R1 (state layer wraps the core) | **Stateless per-turn** (no multi-turn context in Phase 0–1) | Most help-center answers are independent; conversation state adds complexity and data-retention risk early | Phase 2 exit check (measure multi-turn abandonment); if > 20%, add state in Phase 3 | Follow-up rate > 20% indicates users need context carry-over; add conversation memory |
| **Data residency & tenancy** | R3 (changes data-governance contract) | **Single-tenant (us), unregulated**: docs, index, model calls all in one region (e.g., us-east-1); no per-customer isolation yet | Help-center docs are company property, not customer data; no regulation (GDPR/HIPAA) applies to answers themselves | Legal/product sign-off in Phase 1 (org. dependency); if customer data is present, escalate to compliance review | GDPR/HIPAA applies (customer data in docs or logs); re-scope to per-tenant isolation or data residency gating in Phase 2 |

**R1 defaults:**
- Chunking strategy (fixed-size 500 tokens, overlap 50) → simple, tunable in Phase 2 if needed.
- Alert thresholds (injection attempt rate, cost spike > 2× baseline, latency p95 > 10 s) → chosen empirically from Phase 0.

**N/A:**
- Consistency vs availability: answers are read-only; no consistency hazard.
- Monolith vs services: single chatbot service + vendor APIs; no internal service mesh.
- Build vs buy: buying a pre-trained LLM API + managed vector store (industry standard).

---

## 4. Walking skeleton (Phase 0)

**The one real request:** A customer types *"How do I reset my password?"* into a web chat interface. The bot retrieves the relevant help-center section, generates an answer, and displays it with confidence and latency metrics.

**Every tier it crosses:**

1. **Frontend** (React web chat, or Slack bot): user input box → send to backend.
2. **API Gateway** (edge ingress, auth): validate session, rate-limit, forward to service.
3. **Chatbot Service** (Python/Node): orchestrate retrieval + LLM call.
   - **Vector Store** (Pinecone, Weaviate, or in-process): embed query, retrieve top-5 passages.
   - **LLM API** (OpenAI, Anthropic, AWS Bedrock): system prompt + retrieved docs + user query → answer.
4. **Database** (PostgreSQL or similar): store conversation turn for audit/feedback (read-only on Phase 0, append-only).
5. **Observability** (logs, metrics): trace each step, record latency, cost, and token counts.
6. **Deployment pipeline** (GitHub Actions, GitLab CI, or similar): commit → build → deploy to staging → smoke test → deploy to prod canary (1% traffic).

**Who can reach it:** Internal staff (support team) only, behind a feature flag (`rag-chatbot-alpha`). No customer exposure until controls pass.

**Deploy, log, monitor, rollback on day zero:**

- **Deployment:** Infrastructure-as-code (Terraform, Helm); one-command apply in staging, then canary to prod (1% of chat requests routed to bot; 99% to fallback / human).
- **Logging:** Structured JSON logs (user query, retrieved passages, LLM prompt, answer, latency, token count, model ID, cost estimate). Sent to centralized log store (e.g., Datadog, CloudWatch). **No customer queries logged as plaintext to avoid leaking support data; hash or pseudonymize query if needed.**
- **Monitoring:** Dashboard with latency (p50, p95, p99), throughput, error rate, LLM cost per turn, injection attempt detections.
- **Rollback:** Feature flag off (instant, < 1 min); traffic returns to human-only path. Git revert if code fault.

**Exit check (V0):**

1. A real support query succeeds end-to-end: user input → retrieval → LLM call → answer display, all within 8 s p95. One trace visible in logs across all five tiers.
2. Deploy to canary succeeds through CI/CD; traffic split observed in logs (1% rag, 99% fallback).
3. Rollback (flag off) succeeds; traffic returns to 0% rag within 30 s, no errors.
4. An injected prompt-injection attempt in user input (`ignore previous instructions...`) reaches the LLM but produces no tool calls or exfiltration (hard-coded tool allow-list = empty for Phase 0; injection caught by output parsing).
5. First cost baseline recorded: $X per turn (token count × model rate). Latency baseline: p95 T ms.

---

## 5. Phases (Phase 1 onward; dependency order)

### Phase 1 — Foundation: injection defense, data contract, and retrieval baseline

**Unlocks:** Retrieval quality known (V2); injection suite in CI; production-ready data pipeline.

**Depends on:** Phase 0 (V0 passed); help-center docs format confirmed; LLM vendor contract signed (org. dependency, start early).

**Tasks:**

1. **Prompt-injection defense and guardrail design** (Principle 7, AI Defense #1):
   - Separate system prompt (trusted, hardcoded) from user query and retrieved passages (untrusted data).
   - Input filter: block or flag obvious jailbreak patterns (e.g., "ignore instructions", "as an AI", "you are now").
   - Output parsing: constrain LLM response to structured JSON (`{answer: string, confidence: 0–1, sources: [doc_ids]}`). Reject any response with tool calls, URLs, or code execution.
   - Constraint: in Phase 0–1, no tool calls allowed (tool allow-list is empty); reads only.
   - Exit check (hardened in Phase 1): injection suite (50 planted prompts in user input + retrieved docs) runs in CI; zero tool calls or exfiltrations, or injection attempt logged and answer replaced with fallback.

2. **Data contract and retrieval pipeline** (Structural):
   - Confirm help-center docs: source system (Confluence, Zendesk, Notion), update frequency, ownership.
   - Schema: each doc → `{doc_id, title, body, last_updated, source_url, access_control (if any)}`.
   - Chunking: 500-token fixed-size chunks, 50-token overlap, preserve source metadata.
   - Embedding: pin embedding model (OpenAI `text-embedding-3-small` or `sentence-transformers/all-MiniLM-L6-v2`); re-embed only if model changes (R2 decision, gated).
   - Index: Pinecone or Weaviate with versioning; every build produces a versioned snapshot.
   - CI/CD: on doc update, rebuild index (< 5 min), deploy to staging, run V2 eval suite before prod.

3. **Retrieval quality baseline (V2 gate)** (Validation):
   - Curate 100 real customer questions (from support logs or team) with expected answer doc IDs.
   - For each question, retrieve top-5 passages; measure hit rate @5 (passage contains the answer).
   - Acceptance: ≥ 0.80 (80% of questions retrieve a supporting passage).
   - If < 0.80: adjust chunking, re-embed, or re-rank; re-test. If persistent, escalate to Phase 2 (fine-tune or embedding model swap).
   - Evidence: eval report in CI (saved per index version); threshold labeled ASSUMPTION.

4. **Cost per turn baseline** (Measurement, unlocks V4):
   - Meter LLM API costs in Phase 0 (token count × rate).
   - Shadow traffic in Phase 1: replay real customer queries through the bot (no customer sees answers). Measure cost per query (typically $0.001–$0.01 for a few hundred tokens).
   - Project to "cost per resolved conversation" by estimating avg. turns per resolution (e.g., 2 turns). Record as ASSUMPTION + actual measured value.
   - Evidence: cost dashboard export; stored for later gate V4.

**Rollback:** Revert index to prior version; re-enable old retrieval rules if any.

**Exit check:** V2 (retrieval hit rate ≥ 0.80) passes; injection suite runs with zero exfiltration; data pipeline syncs docs on every update with < 5 min latency.

---

### Phase 2 — Prompt tuning and answer quality (V3 gate)

**Unlocks:** Confidence scoring; feedback loop; readiness for small internal rollout.

**Depends on:** Phase 1 (V2 passed, injection defense hardened); injection defense proven (V1 exit check).

**Tasks:**

1. **System prompt and few-shot examples tuning:**
   - Start with a baseline: *"You are a helpful support assistant. Answer using only the provided documentation. If unsure, say so and offer human support contact."*
   - Craft 5–10 few-shot examples (Q&A pairs) from real support tickets, showing desired tone and accuracy.
   - Iteratively test on the 100-question eval set; measure answer quality manually (rubric: correct, confident but wrong, or too vague).
   - Lock prompt version in code (version-control it); log version with every response.

2. **Confidence scoring:**
   - Add a `confidence: 0.0–1.0` field to the LLM response (part of structured JSON).
   - Threshold rule: if confidence < 0.6, append *"I'm not fully confident in this answer. Please contact support."*
   - Baseline: measure what % of answers are low-confidence; target < 20%.

3. **Answer quality gate (V3):**
   - Re-run 100 eval questions with the tuned prompt.
   - Acceptance: ≥ 0.85 of answers are rated "correct" by manual review (2 reviewers, >80% agreement).
   - Evidence: eval report (anonymized question ID, model output, reviewer score); stored in CI artifact.
   - If < 0.85: iterate prompt or gather more examples; re-test.

4. **Observability upgrade (per principle 5):**
   - Log every LLM call: timestamp, user_id (hashed), query (hashed), prompt_version, retrieved_doc_ids, answer_length, confidence, latency, token_count, cost.
   - No plaintext queries or answers in logs (to avoid data leaks).
   - Dashboards: answer quality distribution (% confident, % high-confidence, % marked as needing human escalation).

**Rollback:** Revert prompt version; re-route users to fallback.

**Exit check:** V3 (answer quality ≥ 0.85) passes; confidence distribution baseline recorded; observability logs flowing to centralized store; < 2% of queries error or timeout.

---

### Phase 3 — Cost validation and internal canary (V4 gate)

**Unlocks:** Ready for controlled customer exposure; SLO and runaway-cost controls tuned.

**Depends on:** Phase 2 (V3 passed); operational readiness (Phase 2 exit check).

**Tasks:**

1. **Cost per resolved conversation (V4 gate — economic dependency, R2 validation):**
   - Run canary: 5–10% of internal support staff traffic routed to bot for one week.
   - Meter: cost per query, number of follow-up turns per issue, and cost per resolved ticket (bot handled fully).
   - Acceptance threshold: **UNKNOWN—needs human cost per ticket (support lead input).** Assume $10/hr ÷ 5 tickets/hr = $2 per ticket; target bot cost < $0.20 per ticket (< 10% of human cost) to justify deployment. If unknown, assume ASSUMPTION ($0.15 per resolved conversation based on typical LLM usage) and note it as re-validated in Phase 4.
   - Evidence: cost dashboard export (query count, total cost, avg. cost/query, resolved % estimate).
   - If cost > budget: escalate to cheaper model (e.g., GPT-3.5-turbo instead of GPT-4) or reduce retrieval scope (shorter chunks, fewer passages).

2. **Load test and latency under real concurrency:**
   - Simulate 10–20 concurrent users for 30 min; measure p50, p95, p99 latencies.
   - Acceptance: p95 latency < 8 s (Tradeoff gate T1 default).
   - If latency spikes (e.g., vector store overwhelmed), add caching or switch to a more scalable retrieval backend.
   - Evidence: load test report (concurrent users, latencies, error rate, max memory/CPU).

3. **Cost and latency budgets (defense #2):**
   - Set hard caps: max 2,000 tokens per request, max 5 turns per session (if multi-turn), max $0.50 cost per session.
   - Timeout: 10 s total; if exceeded, return *"Answer generation timed out; please contact support."*
   - Code: LLM call wrapper enforces caps; if exceeded, emit alert and fail gracefully.
   - Exit check: runaway loop (e.g., LLM calling itself recursively) is cut off at token cap and logged.

4. **Availability under peak load:**
   - Measure uptime during canary: target ≥ 99% (< 7 min downtime in one week).
   - Failures: vector store outage, LLM API down, service crash. Each should have a fallback (return human-support contact).
   - Evidence: uptime dashboard (SLI: % of queries served without timeout/error).

**Rollback:** Flag off; traffic back to human staff.

**Exit check:** V4 (cost per resolved conversation validated), load test passes (p95 latency < 8 s), uptime ≥ 99%, zero injection successes in automated suite.

---

### Phase 4 — Customer rollout and feedback loop

**Unlocks:** General availability; feedback-driven tuning; runbook ownership.

**Depends on:** Phase 3 (V4 passed, cost validated); SLO and cost budgets in place; legal/product sign-off (organizational dependency, should be resolved in Phase 1).

**Tasks:**

1. **Phased rollout:**
   - Week 1: 5% of customer chat traffic (one region, one customer segment).
   - Week 2: 20% (expand regions/segments).
   - Week 3–4: 100% (or stop if issues emerge).
   - Runbook: how to roll back, how to escalate to human staff, how to monitor per cohort.

2. **Feedback capture (AI sublayer #9):**
   - Add thumbs-up/thumbs-down on each answer (no context, just binary signal).
   - Optionally, let users correct answers: *"The correct answer is…"* (captures ground truth).
   - Store feedback in database, keyed by query, answer, user_id (hashed), timestamp.
   - Observability: dashboard showing % thumbs-up vs thumbs-down over time.

3. **Feedback integration (after Phase 4):**
   - Weekly: review low-rated answers (thumbs-down > 30%); hand-curate a few into new few-shot examples.
   - Quarterly: batch corrections into a fine-tune dataset (if volume justifies; typically not before Phase 5+).
   - Gate: any prompt or model change re-runs eval suite (V3) before prod deployment.

4. **SLO and runbook:**
   - Define availability SLO: 99% uptime (< 7 min/week).
   - Define escalation: if cost/query doubles or latency p95 > 15 s, auto-disable chatbot, alert on-call, page support lead.
   - On-call rotation: 1 engineer on standby for critical failures.
   - Runbook stored in GitHub/wiki; tested quarterly (principle 5, resilience).

**Rollback:** Feature flag off (instant); traffic to human staff.

**Exit check:** Rollout cohort 1 shows thumbs-up > 0.70, cost per resolved conversation < budget, zero data leaks, SLO attainment ≥ 99% for 7 days. Then proceed to next cohort.

---

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed, and rolled back through every tier in production | Real support query succeeds end-to-end (retrieval + LLM + UI); traffic split to 1% rag / 99% fallback observed in logs; rollback flag-off within 30 s; injected prompt injection produces no tool calls or URLs | (1) Query latency p95 < 8 s; (2) Rollback < 30 s; (3) Zero exfiltration on 5 injected attempts; (4) Baseline costs recorded | Deployment log from CI/CD; trace in centralized logs; rollback timestamp; cost export | Phase 1 (retrieval + injection defense) | 0 |
| V1 | Prompt-injection defense prevents exfiltration and tool abuse | Injection suite (50 payloads planted in user input + retrieved docs + mock tool output) runs in CI; no payload causes tool calls, URL exfiltration, or code execution | 100% of payloads either fail (answer marked unsafe) or produce a benign answer; zero tool calls outside allow-list | CI test output; injection suite logs | Phase 2 (prompt tuning; production readiness); if it fails: code prompt injection filter, re-run before Phase 2 | 1 |
| V2 | For answerable help-center questions, retrieval returns a supporting passage in the top 5 | 100 curated real questions (source: support logs); for each, retrieve top-5 passages; measure hit rate @5 (passage contains correct answer) | Hit rate @5 ≥ 0.80 (ASSUMPTION: 80% retrieval success; if lower, chunking/embedding needs adjustment) | Eval report (per-question result: hit/miss, retrieved doc IDs); stored in CI artifact per index version | Phase 2 (answer tuning); if it fails (< 0.80): adjust chunking strategy, re-embed, or swap embedding model; re-test before Phase 2 | 1 |
| V3 | Tuned prompt produces accurate, confident answers on eval questions | 100 questions + tuned system prompt + few-shot examples; two human reviewers score each answer (rubric: correct / confident but wrong / too vague); measure % correct | ≥ 0.85 of answers rated "correct" by 2 reviewers (>80% inter-rater agreement); confidence < 0.6 on < 20% of answers (ASSUMPTION: typical acceptable vagueness) | Eval report (question ID, model output, reviewer scores, confidence value); stored in CI artifact | Phase 3 (load test + cost validation); if it fails (< 0.85): iterate prompt, gather more examples, re-test | 2 |
| V4 | Answers cost less per resolved conversation than the human handling they replace | Shadow traffic replay + canary (5–10% internal staff, one week): meter cost per query, turns per issue, resolved %; project cost per resolved ticket | Cost per resolved conversation < $0.20 (UNKNOWN baseline: needs human cost per ticket from support lead; assumed $2 per ticket, so bot target < 10% of human cost; re-validate in Phase 4) | Cost dashboard export (query volume, total cost, est. resolved %, cost/resolved); canary metrics | Phase 4 (rollout); if it fails (cost > budget): switch to cheaper model, reduce retrieval scope, or defer chatbot | 3 |
| V5 | System meets availability SLO under real load | Canary week (Phase 3): measure uptime (% of queries served without timeout/error); alert if SLO threshold breached | Uptime ≥ 99% (< 7 min downtime per week) (ASSUMPTION: best-effort; human fallback always available) | Uptime dashboard (SLI: successful queries / total queries); alert logs | Phase 4 (rollout); if it fails (uptime < 99%): identify outage cause, fix, and re-test; or narrow rollout scope | 3 |

---

## 7. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| **0** | Hard-coded tool allow-list (empty); no tool calls. Structured JSON parsing (no arbitrary code). Session auth via API gateway. | Structured JSON logs (query hash, latency, token count, cost, model ID); no plaintext queries. Centralized log store. Dashboard: latency p50/p95/p99, error rate, cost/turn. | Git commit hash logged with every LLM call; prompt version in config (version-controlled). Test queries in CI artifact. | Feature flag for instant rollback. Timeout on all external calls (LLM, vector store) = 10 s. Graceful fallback to human support. |
| **1** | Injection filter + output parsing in code (hardened). Data contract: access control metadata on docs (future enforcement). Hash PII in queries before logging. | Injection attempt counts per query pattern. Retrieval quality metrics (hit rate, latency per index version). Audit log of index builds. | Eval dataset (100 questions, expected answers) version-controlled. Index version tag in CI. | Load-shedding: if LLM latency > 10 s, return fallback. Vector store fallback (e.g., switch to cached index if DB down). |
| **2** | Confidence thresholds enforced in code (not tunable by LLM). Prompt version locked in config. | Confidence distribution (% high/low); answer quality scores per prompt version. Latency per prompt iteration. | Prompt version in Git; eval dataset snapshot per version. Test harness in CI. | Circuit breaker on LLM API (if error rate > 5% in 1 min window, fail-open to human support). Caching of common questions. |
| **3** | Cost cap: max $0.50 per session; token cap: max 2,000 per request. Rate-limit per user (e.g., max 10 queries/min) to prevent abuse. | Cost per query, per session, per user (hashed). Throughput (queries/min) during load test. P99 latency. SLI: uptime (% successful queries). | Load test scenario (10–20 concurrent users, 30 min duration) reproducible in CI. Baseline cost/query stored. | Timeout on all paths: LLM 8 s, vector store 3 s, overall 10 s. If exceeded, graceful fallback. Runaway loop: token cap enforced in wrapper, not in LLM (hardened defense). |
| **4** | Per-user rate-limits (after rollout). Feedback storage: hashed user IDs, no plaintext answers. Data residency check (docs + embeddings in us-east-1; logs may be cross-region). | Feedback dashboard: thumbs-up % per cohort. Cost per user. Latency trend during rollout. Escalation events (cost spike, availability breach). | Rollout playbook: cohort IDs, traffic %, feature flag state, per-cohort metrics. | On-call runbook: escalation thresholds, rollback procedure, who to page. SLO burn-rate alerts (if < 99% uptime, trigger alert). Quarterly runbook drill. |

---

## 8. AI layer

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| **1. Prompt-injection / guardrail defense** | Untrusted input (user query, retrieved docs); hard-coded tool allow-list (empty in Phase 0–1); output parsing to structured JSON; input filter on jailbreak patterns | Phase 1 | V1 (injection suite, zero exfiltration) |
| **2. Cost + latency budget** | Per-request: max 2,000 tokens, max 10 s latency. Per-session: max $0.50 cost, max 5 turns. Hard caps enforced in wrapper. | Phase 3 (hardened in code) | V4 + exit check (runaway loop cut at cap; graceful fallback) |
| **3. Human-in-the-loop gating** | All writes and escalations require human approval (out of scope for Phase 0–4; chatbot reads-only for now). High-risk actions: none gated yet (no writes). Low-risk: reads only, allowed autonomously. | Phase 0 (reads-only design) | Exit check (no tool call succeeds outside allow-list; reads only) |
| **4. Retrieval** | Embedding model: OpenAI `text-embedding-3-small` or OSS equivalent. Chunking: 500-token fixed, 50-token overlap. Vector store: Pinecone or Weaviate. Re-embedding on model change = R2 decision. | Phase 1 | V2 (retrieval hit rate @5 ≥ 0.80) |
| **5. Model access** | Thin wrapper: provider abstraction (swappable LLM), structured output parsing, retry logic (exponential backoff, max 2 retries). Fallback model: GPT-3.5-turbo if GPT-4 fails. | Phase 0 (skeleton calls LLM; hardened in Phase 3) | Exit check (fallback model works in eval; config-swappable) |
| **6. Memory** | Stateless per-turn in Phase 0–2; conversation state optional in Phase 3+ (deferred; gate at Phase 2 exit check: if multi-turn abandonment > 20%, add state). No long-term persistent store of conversations. | Phase 0 (stateless); Phase 4+ (if needed, after V4 passes) | Phase 2 exit check (measure multi-turn abandonment; if < 20%, defer memory) |
| **7. Orchestration** | Single-turn pass-through: retrieve, call LLM, return answer. No loops, no multi-step tasks. | Phase 0 | Exit check (V0: single trace, one LLM call per user query) |
| **8. Routing** | Not needed — single model sufficient for help-center Q&A; no cost-quality routing. | N/A | N/A |
| **9. Feedback** | Binary thumbs-up/thumbs-down + optional correction. Captured but not yet used to retrain (Phase 4 exit check: if > 100 corrections/month, evaluate fine-tune in Phase 5). | Phase 4 (capture); Phase 5+ (closed-loop if needed) | Phase 4 exit check (feedback flowing; weekly review of low-rated answers) |

---

## 9. Methodology exceptions

**None.**

(No feasibility spike needed: RAG over LLM APIs is proven industry practice. Help-center Q&A is not a novel domain. Skeleton runs in production from Phase 0.)

---

## 10. Deliberately deferred

- **Conversation memory (multi-turn context):** Deferred to Phase 3 post-gate. Will be added if Phase 2 exit check shows multi-turn abandonment > 20% (currently assumed < 20%, deferring state complexity).

- **Fine-tuning:** Deferred to Phase 5+. Will be evaluated if (a) V2 retrieval hit rate remains < 0.80 after Phase 1 iteration, **or** (b) feedback volume exceeds 100 corrected answers/month and re-training cost is justified. Fine-tuning is R2; prefer prompt+RAG until this threshold is crossed.

- **Multi-model routing:** Deferred indefinitely. Single model sufficient for help-center domain. Routing adds cost + latency overhead without clear gain; will re-evaluate if answer quality diverges by question type in Phase 4+ feedback.

- **Agent-style tool calls (e.g., create support ticket, escalate to human):** Deferred to Phase 5+. Phase 0–4 is read-only (retrieval + answer only). Tool execution requires human-in-the-loop gating and audit logging; will be gated as a separate phase if requested.

- **Persistent conversation history for analytics:** Deferred to Phase 4+. Phase 0–3 logs are ephemeral or in operational logs. Full conversation history (for user playback, support review) added post-GA if privacy/retention policy is confirmed.

- **Multilingual support:** Deferred indefinitely. Help-center corpus is English-only initially. Will expand if customer demand justifies (estimated effort: 2–3 weeks per language).

---

## Summary

This plan builds a customer-support RAG chatbot in dependency order:

1. **Phase 0** establishes a production walking skeleton with injection defense and cost awareness.
2. **Phase 1** validates retrieval quality and hardens injection defense.
3. **Phase 2** tunes prompts and gates answer quality.
4. **Phase 3** validates cost per resolved conversation and load capacity.
5. **Phase 4** rolls out to customers, captures feedback, and operationalizes.

**Worst-case failure is mitigated first:** injection defense and cost budgets are established in Phase 0 and hardened in Phase 1, before customer exposure. Retrieval quality is gated before prompt tuning. Cost is validated on a canary before general availability.

**Tradeoff decisions are explicit:** prompt+RAG over fine-tune, hosted API over self-host, sync retrieval over async. Each is reversible at the stated tier (R2, R3) and has a flip condition.

**No task precedes what blocks it:** validation gates unlock phases; organizational dependencies (legal, vendor contract) are called out up front to avoid Phase launch delays.
