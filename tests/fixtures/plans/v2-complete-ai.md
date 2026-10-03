# BUILD PLAN — Customer-support RAG chatbot over the help-center docs

## 1. Classification and constraints
- **What:** a chatbot that answers customer questions from the public help-center articles and hands off to a human agent when it cannot cite one.
- **Type:** AI-agentic plus software; greenfield; not a small build (it reaches outside users).
- **Dominant constraint:** correctness.
- **Worst failure:** the bot states an invented refund policy to a customer as fact.

Storage cost does not bind (a few thousand articles), so it has no row.

**Budgets** (only the rows that bind):

| Budget | Target | Label | Checked by |
|:---|:---|:---|:---|
| Latency | Time to first token p95 < 2 s \| p99 < 4 s | ASSUMPTION: typical for hosted-LLM chat; confirm with the product owner | V0 records the floor; Phase 3 exit check |
| Throughput | 20 conversations per minute at peak | **BASELINE**: today's chat volume from the support dashboard | Phase 3 load test |
| Availability / SLO | — | UNKNOWN: needs the support SLA (support lead, before Phase 3) | Phase 3 |
| AI inference cost | … | *UNKNOWN*: needs the human cost per ticket (support lead, before Phase 3) | [V3](#6-validation-gates) |
| Operational complexity | One service and one managed vector store | [REQUIREMENT](#1-classification-and-constraints): a two-person team, stated in the request | Phase 2 exit check |

**Missing inputs:** the support SLA and the human cost per ticket, both from the support lead.

## 2. Dependency map

What waits | Depends on | Kind | Blocked from being
---|---|---|---
Answer tuning | Retrieval finds the supporting passage (V2) | validation | specified
Beta exposure | Injection containment and the handoff path (V1) | risk-security | exposed
General availability | Cost per resolved conversation (V3) | economic | scaled
Indexing internal articles | Legal review of data use | organizational | built

No structural or runtime dependency beyond what the phase order shows.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|:---:|---|---|---|---|
| Prompt+RAG vs fine-tune | R2: weeks of data and eval work | Prompt + RAG over the help center | Answers are mostly factual lookups | **V2** | V2 fails after two re-chunking rounds |
| Hosted API vs self-host | R2 — a provider switch is an adapter change plus an eval re-run | Hosted API behind a `ModelClient` interface | Provider terms allow support transcripts | A one-day spike on 50 questions | A data-residency demand appears |
| Data-privacy boundary | *R3*: exposed personal data cannot be recalled | Index public articles only; scrub PII from logs | No customer data is needed to answer | [V1](#6-validation-gates) | V1 fails, or a use case needs account data |
| Answer posture | R3: a wrong answer cannot be unsent | Cite or abstain; hand off on low retrieval confidence | Customers accept a handoff | Cited basis: support policy requires no answer over a wrong one | Handoff rate above the staffing limit for 2 weeks |
| Log schema | R2: SIEM rules will parse it | JSON lines keyed by `trace_id` and `route|tier` | Two consumers at most | A cheap check: the SIEM rule test passes | A third consumer needs a new field |
| N/A — consistency vs availability | — | — | — | — | — |

**R1 defaults:** test runner → pytest; widget styling → the help-center theme.
**N/A:** monolith vs services — one service; build vs buy for the vector store — a managed store behind an interface is cheap to replace.

## 4. Walking skeleton (Phase 0)
- The one real request: a staff member asks "How do I reset my password?" in the production widget and gets a cited answer.
- Tiers: widget → API → retriever → model API → trace store.
- Deployed by the pipeline with a one-command rollback; logged with one trace per request; an alert on any 5xx.
- Who can reach it: staff only, behind a feature flag.

The request the skeleton sends:

```http
POST /chat HTTP/1.1
# this line is inside a code block, not a heading
| Step | Note |
|---|---|
| 1 | not a table either |
```

Exit check (V0): a real request succeeds with one trace across every tier; a deploy and a rollback succeed through the pipeline; an injected failure fires an alert; the first latency and cost values are recorded as baselines, not targets.

## 5. Phases (Phase 1 onward; dependency order)

### Phase 1 — Guardrails, budgets, handoff
- Unlocks: an allow-listed beta cohort.
- Depends on: Phase 0 and V0.
- Tasks: retrieved text handled as data; an output filter and a tool allow-list; token and cost caps per turn; the human handoff.
- Rollback: the feature flag off.
- Exit check: V1 passes.

### Phase 2 — Retrieval
- Unlocks: answer tuning.
- Depends on: Phase 1.
- Tasks: chunking, hybrid search, the labeled question set.
- Rollback: the previous index version stays warm.
- Exit check: V2 passes.

### Phase 3 — Model access and launch
- Unlocks: general availability.
- Depends on: V2 and V3.
- Tasks: the provider adapter; a 5% canary; the widget for all visitors.
- Rollback: route every conversation to humans.
- Exit check: V3 passes and p95 latency stays inside its budget.

## 6. Validation gates

One row per gate cited above, and no others. Each threshold carries its label.

~~~text
# a fenced line that looks like a heading must not end this section
~~~

The evidence is stored with the build that produced it.

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| **V0** | A change can be deployed, observed, and rolled back through every tier in production | Deploy, roll back, and inject a failure | the V0 exit check in §4 | Pipeline run log and the alert record | Phase 1 | 0 |
| V1 | Injection attempts cannot make the bot reveal internal articles or act outside its allow-list | A red-team suite of 200 attacks run in CI | Zero leaks in 200 attempts (ASSUMPTION: suite size from the security team's guidance) | Red-team report per release | Beta exposure; if it fails: block the beta and harden the filters | 1 |
| V2 | For answerable questions the index returns a supporting passage in the top 5 | 200 labeled questions per index version, each scored `hit|miss` | hit rate@5 ≥ 0.90 (ASSUMPTION: about ±4 points of sampling error at 200 questions) | Eval report per index version | Answer tuning; if it fails: re-chunk or change the embedding model | starts in 1, required before 3 |
| V3 | Cost per resolved conversation is below the human cost per ticket | Meter cost on a 5% canary for one week | UNKNOWN: needs the human cost per ticket from the support lead before Phase 3 | Cost dashboard export<br>for the canary week | General availability; if it fails: narrow the rollout | 3 |

## 7. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Staff-only flag; secrets in the vault | One trace per request; 5xx alert | Pinned dependencies; infrastructure as code | Rollback through the pipeline |
| 1 | Output filter; tool allow-list | Blocked-attack counter | Red-team suite in CI | Handoff when the model API is down |
| 2 | Public articles only; PII scrubbed | Retrieval hit-rate dashboard | Versioned index builds | Previous index kept warm |
| 3 | Rate limits per visitor | Cost per conversation metric | Prompt versions in git (`a \| b` diffs reviewed) | Circuit breaker to the human queue |

## 8. AI layer (AI/agentic systems only)

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Prompt-injection / guardrail defense | Retrieved text treated as data; output filter | Phase 1 | V1 |
| 2. Cost + latency budget | Token cap per turn; daily cost cap | Phase 1 | V3 |
| 3. Human-in-the-loop gating | Handoff to a human agent on low confidence | Phase 1 | Phase 1 exit check |
| 4. Retrieval | Hybrid search over public articles | Phase 2 | V2 |
| 5. Model access | Hosted API behind an interface | Phase 3 | Phase 3 exit check |
| 6. Memory | Not needed — single-session answers | — | — |
| 7. Orchestration | Not needed — one retrieval step | — | — |
| 8. Routing | Not needed — one model | — | — |
| 9. Feedback | Thumbs up or down into the question set | Phase 3 | Phase 3 exit check |

## 9. Methodology exceptions

None.

## 10. Deliberately deferred
- Multilingual answers: pulled forward when non-English tickets exceed 10% of volume.
- Account-specific answers: pulled forward after a privacy review approves account data access.
