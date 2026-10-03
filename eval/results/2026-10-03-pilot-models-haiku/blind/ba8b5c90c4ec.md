Now I'll create the BUILD PLAN for an autonomous agent that triages and resolves GitHub issues, following the dependency-first architect methodology.

---

# BUILD PLAN — Autonomous GitHub Issue Triage and Resolution Agent

## 1. Classification and constraints

**What:** An autonomous AI agent that reads GitHub issues, classifies them by priority and type, suggests resolutions, applies labels/milestones, and optionally creates fix attempts or escalates to humans.

**Type:** AI-agentic system, greenfield, small build (one team, internal-first rollout, no live money or regulated data initially, but touching real external data—GitHub repos and their contents—so exposure controls are critical).

**Dominant constraint:** Correctness (wrong classification or unintended code changes would damage trust in a repo or break real workflows) and cost (unbounded model calls can spend quickly).

**Worst failure:** An agent incorrectly closes or modifies a critical issue without human review, loses a bug report, or creates merge-request conflicts that clog the development workflow.

**Budgets:**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency (triage response) | p95 < 30 s per issue | ASSUMPTION: typical for hosted LLM with retrieval; human review is asynchronous | V0 records floor; Phase 3 canary |
| Throughput (issues processed/hour) | 100 during pilot; 1,000 at scale | ASSUMPTION: no backlog on a typical repo | Phase 2 load test |
| Availability / SLO | Not applicable during pilot; Phase 4 defines SLO if general availability planned | UNKNOWN: depends on rollout scope and business tolerance | Phase 4 gate |
| AI inference cost | < $0.05 per resolved issue (triage + optional fix attempt) | ASSUMPTION: $20/engineer/issue in human time; lower cost per issue than async review | V1 meter on pilot; V4 gate before scaling |
| Model token cap (per issue) | 50k tokens (input + output) | ASSUMPTION: typical GitHub issue context + 2 retrieval passes + 1 fix attempt | V0 enforces hard cap |
| Storage cost | Negligible (no long-term persistent memory planned yet) | ASSUMPTION: ephemeral per-issue context | Phase 1 |
| Operational complexity | One service, one model API key, one vector store (read-only); no self-hosted LLM | ASSUMPTION: two-person team; no on-call except during pilot | Phase 2 |

**Missing inputs:**
- Exact list of GitHub orgs/repos to pilot on (needed Phase 0; assume one internal repo for walkthrough, two pilot repos in Phase 2).
- Human review throughput and approval latency (needed Phase 2 to set timeout-to-escalate; assume 24h mean turnaround).
- Definition of "resolved": does agent close, or only recommend? (Assumed: recommend + require human approval to close or merge).
- Target accuracy for triage (Phase 2 gate; assume 90% precision on labeling, 95% recall on blocking issues).

---

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Fix-attempt recommendations | Prompt injection containment + tool allow-list (only comment, no merge/close without approval) | risk-security | exposed to real repos |
| Fix-attempt recommendations | Cost and latency budget, per-issue token cap | validation | hardened |
| Triage classification | Retrieval index of issue templates / runbooks / known-issues corpus | validation | specified (classification rules need examples) |
| Retrieval index | Chunk schema + metadata (repo, issue type, severity tags) | decision | built |
| Retrieval index | Embedding model pinned in version | decision | specified |
| Agent write access (labels, comments) | Human-in-the-loop gating: approval for close, merge, or major label changes | risk-security | exposed to real repos |
| Triage accuracy | Labeled eval set of 200+ real GitHub issues with ground truth | validation | hardened (rules tuned) |
| General availability scaling | Cost per resolved issue validated on live pilot traffic (>100 issues processed) | economic | committed to |
| Feedback loop (corrections → retuning) | Memory or persistent store of corrections (Phase 3 placeholder; Phase 4 builds it) | decision | scaled |

**No structural, runtime, or organizational dependencies beyond the skeleton setup.**

---

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Prompt+RAG vs fine-tune (retrieval or custom model) | R2: weeks to re-index or re-train | Prompt+RAG: read-only retrieval over issue templates / runbooks, no fine-tuning yet | Retrieval quality is the blocker, not model capability; cost per issue is high enough to justify RAG | V1 (retrieval hit-rate@5 ≥ 0.85 on labeled test set) | "V1 fails, or cost per issue stays > $0.10 despite optimization" → reconsider fine-tune or cheaper model |
| Hosted API vs self-host | R3: switching providers or standing down a cluster is weeks of integration | Hosted API (OpenAI or Anthropic) with fallback model | Operational complexity and latency for a two-person team favor managed; private data is internal only (no PII yet) | Cost + latency budget; Phase 2 exit check | "Cost per issue > $0.07 and infra budget freed" → self-host cheaper model |
| Build vs buy (use GitHub's native automation or custom agent) | R1: agent can be deprecated in weeks if GitHub's tools advance | Build custom agent: GitHub's native actions lack multi-step reasoning and cost awareness | More control over triage logic and cost; GitHub Actions cannot reason over repo context or enforce token budgets | V0 shows end-to-end inference loop and cost tracking | "GitHub releases a reasoning system that is cheaper and faster" → migrate or shut down |
| Agent-created PRs (fix attempts) vs recommendation-only | R3: allowing writes changes the trust model and audit trail | Recommendation-only in Phase 0–2; agent comments a fix suggestion but never pushes | Safer during pilot; human always merges or closes | V2 (no unintended merges or closed issues on pilot); Phase 3 gate before auto-create-PR | "V2 passes, code churn is low, 95% fix quality on sampled corrections" → Phase 3 enables limited auto-PRs |
| Retrieval corpus: static snapshot vs live issue feed | R2: switching to live feed means schema change and real-time sync logic | Static snapshot: index only the GitHub org's public templates, docs, and issue templates (e.g., bug report format); refresh weekly | Keeps retrieval deterministic and cheap; live feed is noisy and costlier | V0 + Phase 1 design | "Pilot shows > 20% stale hits on live issues" → live feed + CDC (change-data-capture) |
| Triage output: labels only vs labels + priority priority + assignment | R1: output schema can change per issue type | Labels + severity tags + suggested assignee (no auto-assign yet) | Gives engineers richer context; suggests assignee from git-blame history, not AI reasoning | V2 (assignee suggestions verified against git blame; no false positives > 10%) | "False positives > 15% or too noisy in practice" → revert to labels-only |

**R1 defaults:** Retry strategy for model API → exponential backoff, 3 attempts, fallback to cache or human escalation; Classification schema version control → one-line config, not code.

**N/A:** Data-privacy boundary (no PII in issues yet, internal pilot; Phase 4 assesses if customer issues are in scope and adds GDPR gate if so); tenancy model (single org, no multi-tenant concerns until Phase 4).

---

## 4. Walking skeleton (Phase 0)

**One real request:**  
A GitHub webhook fires on a new issue in an internal test repo. The agent:
1. Fetches the issue text (title, body, labels, repo context).
2. Queries the retrieval index with the issue title.
3. Calls the model (Claude or GPT) with the issue + top-3 retrieved docs.
4. Parses the model's output: suggested labels, severity, confidence score, and a short explanation.
5. Posts a comment (never modifies labels or closes) with the suggestion.
6. Logs the full trace and inference cost.
7. Records the issue ID, suggestion, and human feedback placeholder.

**Tiers crossed:**
- GitHub API (webhook ingress, fetch issue, post comment).
- Retrieval service (vector index, fetch top-3 docs).
- Model API (LLM inference).
- Logging (structured logs, cost tracking).
- Monitoring (latency, error rate, token budget).

**Deployment:**
- Code: Lambda function (or small container); deployed via GitHub Actions on commit to main.
- Configuration: GitHub Actions secret (model API key), environment variable (vector index URL).
- Rollback: Kill the webhook subscription; previous agent state (human review only) resumes.

**Who can reach it:**  
Internal GitHub org, one test repo, behind a feature flag (`AGENT_ENABLED_REPOS`). Issue creator and maintainers can see the agent comment; no external traffic until Phase 2.

**Exit check (V0):**
- Real issue created → agent posts a comment with a triage suggestion within 30 s.
- Logs show one trace: webhook → fetch → retrieval → inference → comment (each step logged, no failures).
- Deploy succeeds in < 5 min; rollback (revert Lambda) succeeds in < 2 min.
- Injected failure (model API times out) fires an alert (CloudWatch or equivalent).
- Baselines recorded: latency p50, model tokens per issue, retrieval latency, comment post time.

---

## 5. Phases

### Phase 1 — Foundations and safety guardrails

**Unlocks:** Pilot triage on real issues (Phase 2).

**Depends on:** V0 (skeleton deployed and monitored).

**Tasks:**

1. **Build prompt-injection and guardrail defense.**
   - Prompt template: system instructions (role, task, guardrails) separated from untrusted content (issue text, retrieved docs).
   - Input validation: reject issues with known injection patterns (e.g., "Ignore above instructions") before sending to model. Log and skip.
   - Output validation: parse model response; constrain to allowed labels, severity levels (low/medium/high/critical), and confidence scores. Reject or escalate if output breaks schema.
   - Tool allow-list: agent can only comment and fetch; no label changes, no issue closes, no PR creation.

2. **Implement cost and latency budget.**
   - Per-issue token cap: 50k (input: issue + docs ≈ 15k; output: reasoning + classification ≈ 2k; buffer for retries).
   - Per-request timeout: 40 s (p95 target 30 s; leaves 10 s headroom before client timeout).
   - Hard stop: if either cap is hit, emit a degraded response ("Could not classify; escalating to human") and log.
   - Meter: track tokens and inference cost per issue; aggregate daily cost.

3. **Design and build retrieval index.**
   - Corpus: GitHub org's issue templates, CONTRIBUTING.md, README.md, known-issues list (curated, human-reviewed runbooks).
   - Chunk schema: 256-token chunks with metadata (file path, issue type tag, severity level, last updated).
   - Embedding model: pin to one version (e.g., OpenAI's `text-embedding-3-small` or Anthropic's embeddings; lock version in config).
   - Index: Pinecone, Weaviate, or similar; read-only access from agent Lambda.
   - Refresh: weekly batch (GitHub → ETL → index update); backfill on first deploy.

4. **Set up human-in-the-loop gating for writes.**
   - Agent can only post comments (no label changes, no closes, no auto-PRs).
   - Comments include: triage suggestion, confidence score, and a link to human review UI (future; Phase 2 placeholder).
   - All agent actions logged: issue ID, suggestion, timestamp, cost, confidence.

5. **Observability, reproducibility, and resilience.**
   - Structured logs (JSON): issue ID, suggestion, confidence, retrieval hit rate, model latency, total cost.
   - Tracing: one trace ID per issue, linked through GitHub API calls, retrieval queries, and LLM requests.
   - Metrics: latency (p50, p95, p99), error rate, token usage, inference cost, retrieval hit rate.
   - Reproducibility: log all model inputs (issue text, retrieved docs); allow re-run with same model version.
   - Resilience: retry model API 3 times with exponential backoff; degrade to "needs human review" if all fail.
   - Dry-run mode: optional env var to log suggestions without posting comments (for testing).

**Rollback:** Revert Lambda code; disable webhook. Agent comments remain on issues (informational, no action taken). Return to Phase 0 (human-only review) or stop.

**Exit check:**
- Injection suite (known patterns + red-team strings) runs in local test; no injection causes an unapproved action.
- Cost and latency budgets enforced: runaway issue processed in ≤ 40 s and ≤ 50k tokens; metrics logged.
- Retrieval index built and populated; top-3 results on 10 seed queries are human-verified as relevant.
- Human-in-the-loop hardened: comments posted, labels unchanged, no auto-closes or auto-merges.

### Phase 2 — Pilot triage accuracy and cost validation

**Unlocks:** Rollout to more repos (Phase 3); optional fix-attempt recommendations (Phase 3 gate).

**Depends on:** Phase 1; V0, V1 (retrieval validation gate), V2 (triage accuracy and cost gate).

**Tasks:**

1. **Expand to two pilot repos** (internal; no external users).
   - Repos selected: one application, one library; mix of bug reports, feature requests, and documentation issues.
   - Webhook enabled for both repos.
   - Agent suggestions appear as comments; no label changes or closes yet.

2. **Build triage accuracy eval set.**
   - Collect 200+ issues from pilot repos covering: bug, feature, documentation, question, chore.
   - For each issue: human labels (ground truth): (a) issue type (5 classes), (b) priority (critical/high/medium/low), (c) suggested assignee.
   - Store eval set in version control (anonymized issue IDs, text, no personal data).

3. **Tune retrieval and prompts.**
   - Run triage on eval set; compare agent suggestions to ground truth.
   - Measure: precision, recall, F1 per label class; confidence calibration (does confidence correlate with correctness?).
   - If hit-rate < 0.85 (V1): re-chunk corpus, update embedding model version, or add more templates to index.
   - If classification precision < 0.90: iteratively refine prompt (examples in system message, better task framing).
   - Re-run eval after each change; track improvements in CI.

4. **Meter cost and latency on live traffic.**
   - Instrument agent: log cost per issue (input + output tokens × rate).
   - Collect latency distribution (p50, p95, p99).
   - Run for ≥ 2 weeks; process ≥ 100 issues.
   - Compute: mean cost per issue, cost per resolved issue (if humans use agent suggestions to close/resolve), latency percentiles.

5. **Human review workflow (async, no blocking).**
   - Engineers in pilot repos can see agent suggestions as comments; no expectation to act.
   - Collect feedback: thumbs up/down on each suggestion, or manual corrections (new label, reassignment).
   - Log feedback for Phase 3 feedback loop.

6. **Security and observability hardening.**
   - Review logs for injection attempts or anomalies (e.g., issues with unusually long bodies, non-text attachments).
   - Validate: no unintended label changes, no closes, no merges.
   - Alert on: model API errors > 5%, inference cost > $1/issue, latency p95 > 60 s.

**Rollback:** Disable webhooks on pilot repos; keep Phase 1 running on internal test repo or stop.

**Exit check:**
- V1: Retrieval hit-rate@5 ≥ 0.85 on labeled eval set (or re-plan retrieval in Phase 2).
- V2: Triage classification precision ≥ 0.90, recall ≥ 0.85 on eval set; cost per issue ≤ $0.05 (or re-tune, narrow scope, or stop if unachievable).
- Latency p95 ≤ 30 s on live traffic (or increase timeout, optimize model call, or accept degraded performance).
- ≥ 100 issues processed; no security incidents (injections, unintended closes, etc.).

### Phase 3 — Fix-attempt recommendations and approval workflow

**Unlocks:** Broader rollout and optional auto-PR creation (Phase 4 gate).

**Depends on:** Phase 2; V2, V3 (fix-attempt quality and human approval flow), V4 (cost per resolved issue on canary).

**Tasks:**

1. **Extend model inference to suggest fixes (read-only analysis).**
   - For bug reports: agent retrieves relevant code sections (from repo checkout), parses stack traces, suggests root cause and fix strategy.
   - For documentation: agent suggests updates to docs or comments.
   - Output: structured suggestion (file, line range, proposed change, confidence, reasoning).
   - Constraint: agent never modifies code or docs; only comments a suggestion.

2. **Build human-approval workflow.**
   - Add a button or checkbox in comment (optional GitHub UI or external review page): engineer can accept, reject, or request revision.
   - Accepted suggestions are logged (ground truth for feedback loop Phase 4).
   - Rejected suggestions: log reason (if provided) for prompt tuning.

3. **Canary rollout: cost and quality validation (V4).**
   - Enable fix suggestions on 5% of pilot repos' issues for 1 week.
   - Measure: cost per issue, quality (% accepted, % correct after merge), engineer satisfaction (NPS, optional).
   - If cost per resolved issue < $0.07 and acceptance rate > 70%: proceed to Phase 4. Otherwise, narrow scope or stop.

4. **Expand to internal organization (all repos, behind feature flag).**
   - Rollout to all internal repos in the GitHub org.
   - Triage + fix suggestions enabled; no auto-writes.
   - Monitor: daily cost, accuracy (sampled manual review), error rate.

5. **Operational readiness.**
   - On-call playbook: model API outage, injection attack, runaway costs, high error rate.
   - Alerts tuned: cost spike, latency p95 > 60 s, errors > 5%, budget exhausted.
   - Daily cost tracking dashboard.

**Rollback:** Disable agent in feature flag; all issues revert to human triage. Keep cost metrics for analysis.

**Exit check:**
- V3: On 20 sampled fix suggestions, ≥ 15 are rated as "helpful" or "correct" by engineers (≥ 75% quality bar).
- V4: Canary week shows cost per resolved issue ≤ $0.07 and acceptance rate ≥ 70% (or narrow scope, optimize, or stop).
- No injection attacks, unintended closes, or code breaks during Phase 3.
- On-call runbook tested (no major gaps).

### Phase 4 — Auto-merge and external exposure (dependent on V4 gate, Phase 3 exit)

**Unlocks:** Public repo pilot or general availability (if approved).

**Depends on:** Phase 3; V4 (cost per resolved issue on canary).

**Tasks:**

1. **Optional: Enable auto-PR creation for fix suggestions (if V4 passes and org approves).**
   - Agent creates a PR (never auto-merges) with the suggested fix and a link to the issue.
   - PR description includes: agent confidence score, reasoning, link to suggested-by comment.
   - Engineers review and merge manually.
   - Constraint: auto-PR only for documentation and low-risk changes (e.g., typos, config); complex fixes stay as suggestions.

2. **Feedback loop: corrections and retuning.**
   - Collect feedback from Phase 3 pilot: approved fixes, rejected fixes, manual corrections.
   - Feed corrections into prompt tuning (in-context learning) for next phase; no fine-tuning yet (R2 decision deferred to Phase 5).
   - Re-eval: triage accuracy on new eval set; acceptance rate trend.

3. **Scale cost tracking and budget enforcement.**
   - Per-team or per-repo cost caps (optional; Phase 4 design task).
   - Org-wide cost dashboard; monthly reporting to stakeholders.

4. **Pilot on external repo or open-source project (optional, if approved).**
   - Choose a small, low-risk OSS repo where agent suggestions are read-only (comments only, no writes or auto-PRs).
   - Enable triage; monitor for false positives, security issues, brand impact.
   - Metrics: suggestion quality, maintainer sentiment, cost per issue.

5. **Organizational readiness: legal and data-protection review.**
   - If pilot involves external repos or customer data: review GDPR, data retention, data usage with legal.
   - Clarify: are suggested code samples owned by the org or covered by repo license?
   - Document: agent does not learn from code (no fine-tuning on private data); suggestions are per-issue, not retained for training.

**Rollback:** Revert to Phase 3 (read-only suggestions, no auto-PR). Keep feedback data for analysis.

**Exit check:**
- V4 baseline from Phase 3 held or improved: cost per resolved issue, acceptance rate.
- Feedback loop integrates corrections; no regression in triage accuracy.
- If external pilot enabled: no security incidents, no brand issues, maintainer feedback positive.
- Legal review completed (if applicable); data retention and usage policy documented.

---

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A real issue can be ingested, classified, and result posted through every tier in production; deployment and rollback succeed | Deploy Lambda + webhook to test repo; create issue; verify comment appears; inject model timeout; verify alert fires; kill webhook and verify no more comments | Real issue → comment ≤ 30 s; deploy ≤ 5 min; rollback ≤ 2 min; alert fires on model timeout; latency/token/cost baselines recorded (no target, floor only) | Deployment log, issue comment, CloudWatch metrics, alert trigger screenshot | Phase 1; if it fails: debug infrastructure or adjust targets and retest | 0 |
| V1 | The retrieval index returns a supporting document for answerable issues ≥ 85% of the time | Query index on 200 labeled issues with known relevant docs; record hit@5 (top 5 results include ground truth) and hit@1 (top 1 result) | Hit-rate@5 ≥ 0.85 (ASSUMPTION: 85% is acceptable for retrieval; sampling error ±4 points at 200 samples; if lower, re-chunk or update embedding model) | Eval report (CSV: issue ID, query, top-3 results, ground truth, hit) stored in CI artifacts | Phase 2 triage tuning; if it fails: re-chunk corpus, swap embedding model, or expand corpus and retry in Phase 2 | 1 |
| V2 | Triage suggestions (issue type, priority, suggested assignee) are correct ≥ 90% of the time (precision) and catch ≥ 85% of true issues (recall) | Run agent on 200-issue eval set; compare suggestions to ground truth; compute precision, recall, F1 per class and per issue | Precision ≥ 0.90 (weighted across types); recall ≥ 0.85; no label with F1 < 0.75; confidence calibration ≥ 0.80 (Brier score); cost per issue ≤ $0.05 on real issues | Eval report (CSV: issue ID, ground truth labels, suggested labels, confidence, cost) + confusion matrix per label; stored in CI | Phase 3 fix recommendations + canary; if it fails: iterate on prompt, retrieval tuning, or eval set quality; re-run Phase 2 until V2 passes | 2 |
| V3 | Suggested fixes are correct and helpful ≥ 75% of the time (as rated by engineers who implement them) | Run fix-suggestion agent on 20 issues from Phase 3 pilot; collect engineer ratings (helpful/not helpful, correct/incorrect, usable/needs revision) on each suggestion | ≥ 15 of 20 suggestions rated as "helpful or correct" (75%; ASSUMPTION: acceptable bar for experimental feature) | Feedback form responses (issue ID, suggestion ID, rating, comments), stored in spreadsheet or database | Phase 4 auto-PR + external pilot (or narrow Phase 3 to triage-only); if it fails: continue Phase 3 with triage-only, gather more feedback, and retry V3 in next cycle | 3 |
| V4 | Cost per resolved issue on pilot canary is sustainable compared to human handling | Meter cost per issue and per resolution on 5% canary for 1 week (≥ 50 resolved issues); compare to baseline human cost (support lead provides per-ticket cost) | Cost per resolved issue < support-provided baseline OR cost per issue ≤ $0.07 (ASSUMPTION, pending support cost input; UNKNOWN if not provided by Phase 3) | Cost dashboard export (date, issue ID, agent cost, human resolution or follow-up, final cost) | Phase 4 scaling + external pilot; if it fails: narrow rollout (fewer issue types, lower complexity), optimize model use (cheaper model, shorter context), or stop scaling and revert to internal-only | 3, required before 4 |

---

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| **0** | Prompt template hardened: system instructions separated from issue text; tool allow-list (comment only); no auto-write execution tested | Structured logging (JSON): issue ID, suggestion, confidence, retrieval latency, model latency, token usage, cost; trace ID per issue | Log all model inputs (issue + retrieval results); store in versioned config; dry-run mode available | Model API retry (3 attempts, exponential backoff); degraded response if budget exhausted; rollback: kill webhook |
| **1** | Input validation on issue text (reject known injection patterns); output schema validation (allowed labels, priority levels); audit log of all actions | Per-step metrics (retrieval hit rate, model latency p50/p95, cost per step); daily dashboard (volume, cost, errors); alerts on budget exceeded | Same as Phase 0 + log each prompt version with git commit; eval set versioned | Hard token cap enforced (50k per issue); timeout 40 s; fallback to "needs human review" message |
| **2** | Injection testing continues (red-team patterns in pilot issues); no unintended label changes or closes monitored in logs | Pilot repo logs: daily cost per issue, acceptance rate (% approved suggestions), accuracy (sampled manual review, confusion matrix); NPS survey optional | Eval set version-controlled; replay pilot with same model version produces same suggestions (determinism check) | Same as Phase 1 + broader monitoring (cross-repo error rate, daily cost budget alerts) |
| **3** | Approval flow audited: all PR suggestions logged with engineer approval/rejection; no unintended auto-merges (enforcement in tool executor, not model); code review process unchanged for human reviewers | Feedback loop metrics: corrections per issue, re-tuning count; acceptance rate trend per label type; cost per resolved issue (new metric) | Same as Phase 2 + log feedback (approved/rejected suggestions) for retraining audit trail | Broader resilience: regional failover (if external model API down, use fallback model or human escalation); cost runaway: per-org or per-repo cap + alert |
| **4** | External repo pilot: no auto-PR on customer code; all suggestions read-only until org trust builds; data-protection review: clarify agent does not learn from external code | Public dashboard (optional): suggestion acceptance rate, cost per external issue; feedback sentiment; no PII leaked in logs (anonymize if external data involved) | External pilot: reproducibility across orgs (same model version, same corpus); eval set for new issue types (feature requests vs bug fixes) | Permanent fallback: human triage resume if agent disabled; graceful degradation if model API unavailable; cost cap per external org if multi-org support added |

---

## 8. AI layer

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| **1. Prompt-injection / guardrail defense** | Issue text and retrieved docs are untrusted inputs; system instructions separated; tool allow-list (comment only); output schema validation (allowed labels, priority levels, confidence score range); no arbitrary code execution | Phase 1 | Injection suite (known patterns + red-team strings) runs in CI; no unintended action triggered; all outputs conform to schema; tool executor rejects writes |
| **2. Cost + latency budget** | Per-issue token cap (50k); per-request timeout (40 s); hard stop at budget; cost metered per issue and aggregated daily | Phase 1 (enforced in skeleton); Phase 2 (monitored on live traffic) | V0 baseline recorded; Phase 2 live traffic < $0.05/issue; Phase 3 canary < $0.07/issue (V4) |
| **3. Human-in-the-loop gating** | Triage suggestions read-only (comments); fix suggestions require engineer approval before any action; no auto-closes or auto-merges until Phase 4 gate passes; all actions logged | Phase 1 (hardened in Phase 2) | V2: engineers can approve/reject suggestions; V3: 75% acceptance rate on fix suggestions; V4 prerequisite before auto-PR |
| **4. Retrieval** | Index of GitHub org templates, runbooks, known-issues (static snapshot, weekly refresh); chunk schema with metadata; embedding model pinned; read-only access | Phase 1 (built and populated); Phase 2 (tuned on eval set) | V1: hit-rate@5 ≥ 0.85 on 200 labeled issues |
| **5. Model access** | Hosted LLM API (Claude or GPT); abstraction layer (provider swap possible); retries with exponential backoff; fallback model optional (not yet implemented, Phase 4 placeholder) | Phase 0 (skeleton) | Switching to fallback model is a config change; eval suite runs on both models at Phase 4 if fallback enabled; V0 exit check confirms inference works |
| **6. Memory** | None yet. Per-issue context only (issue text + retrieval results); no conversation history, no user session persistence | Not built in pilot (Phase 0–3) | N/A for Phase 0–3; Phase 4 design task if multi-turn interactions needed (deferred) |
| **7. Orchestration** | Single-turn: fetch issue → retrieve docs → call model → parse output → post comment. Optional multi-step in Phase 3: retrieve code → analyze stack trace → suggest fix (simple chain) | Phase 0 (single-turn); Phase 3 (optional multi-step for fix suggestions) | V0 exit: comment posts successfully; V2 exit: fix suggestions are coherent and actionable; no failed steps vanish |
| **8. Routing** | None needed. All issues routed to one model; fallback model optional (future) | Not needed — one model per phase | N/A — single model per phase; if multiple model strategies in Phase 4, add routing |
| **9. Feedback** | Corrections from engineer approvals (accepted/rejected suggestions) logged; not yet fed back into model tuning (Phase 4 placeholder); no automated retraining | Phase 3: feedback collection (approval/rejection); Phase 4: optional in-context example updates (no fine-tuning yet) | Phase 3 exit: engineers rate suggestions; Phase 4 design: corrections flow into prompt updates, next eval improves target metric with no regression |

---

## 9. Methodology exceptions

| ID | Rule bypassed | Why it does not apply | Replacement validation | Evidence required | Resumes when |
|---|---|---|---|---|---|
| E1 | Walking skeleton first (principle 6); V0 assumes retrieval and model are available | Feasibility risk: unknown if retrieval accuracy or model quality sufficient to triage issues correctly | Two-week spike (Phase 0.5, parallel to setup): run agent on 50 internal issues offline (no webhook, no posting); measure triage accuracy (F1 ≥ 0.70), latency (p95 ≤ 60 s), cost (< $0.10/issue); if ≥ 70% feasible, proceed to V0; else re-scope or stop | Spike report: accuracy per issue type, latency distribution, cost per issue, failure modes | The spike passes; if spike fails, re-plan agent scope (e.g., triage-only, narrower issue types) |
| None | — | — | — | — | — |

---

## 10. Deliberately deferred

- **Multi-turn conversation / session memory:** Phase 4 gate. If engineers want follow-up questions ("What version of X?") or conversation history, build persistent memory (short-term per session, long-term per issue) after Phase 3 exit. Trigger: feedback indicates multi-turn interactions needed.
- **Fine-tuning on private data:** R2/R3 decision deferred to Phase 4. If prompt+RAG plateaus (V2 or V3 fails repeatedly), consider fine-tuning on labeled org issues (weeks of work, vendor lock-in risk, training data governance). Trigger: triage accuracy not improving, cost per issue not decreasing, and org commits to long-term investment.
- **Auto-PR creation:** Phase 4 gate (V4 cost + quality), not enabled in Phase 0–3. Trigger: V4 passes, V3 quality ≥ 75%, org approves. If V4 fails, stay read-only (suggestions, no writes).
- **External repository pilot:** Phase 4 gate. Trigger: internal pilot successful (cost stable, quality good, engineers trust it), org approves external rollout, legal review completed. Risk: external issues may have different patterns; start with one OSS repo, read-only only.
- **Cost allocation per team:** Phase 4 design. If org grows or multi-team usage is high, implement per-team cost tracking and budgets. Trigger: Phase 3 cross-org usage, cost > $500/month, or org requests chargeback.
- **Alternative models (open-source, self-hosted):** R3 decision deferred. If hosted LLM cost or latency becomes unacceptable (Phase 2/3 findings), evaluate self-hosting (Llama, Mixtral) or cheaper API. Trigger: cost per issue > $0.10 or latency p95 > 60 s for 2 weeks.

---
