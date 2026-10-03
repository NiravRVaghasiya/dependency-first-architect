# BUILD PLAN: Customer-support RAG chatbot over help-center docs

**Assumption the phase order depends on:** the bot answers only from public help-center articles. It does not read customer account data, and its only action is handing the conversation to a human. If it must look up orders or issue refunds, the plan changes before Phase 3: it would need an auth model, permission checks at retrieval, and human approval before any action. That would mean re-planning from Step 1.

## 1. Classification and constraints
- **What:** a chat widget on the help center. It answers customer questions only from published help-center articles, with citations, and hands off to a human agent when it can't answer.
- **Type:** AI / RAG, with a simple fixed pipeline rather than an agent. Greenfield service that reads from the help center and creates tickets in the existing ticketing system. Not a small build: outside users see it, customers will type personal data into it, and some decisions are hard to reverse (R3).
- **Dominant constraint:** correctness. Every answer must be grounded in a cited article.
- **Worst failure:** the bot tells a customer about a policy, refund or entitlement that the docs don't contain, and the company is held to it (as in *Moffatt v. Air Canada*, 2024). V3 is the gate that guards against this.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency | Time to first token p95 < 2 s; full answer p95 < 8 s | ASSUMPTION: typical for hosted-LLM chat plus a retrieval step and a check step; product owner to confirm | V0 records the floor; Phase 4 exit check at load |
| Throughput | — | UNKNOWN: peak chats per hour comes from support ops, needed before the Phase 4 load test | Phase 4 load test at 2× peak |
| Availability / SLO | — | UNKNOWN: product owner sets it before Phase 5. The model API, vector store and database in series cap it near 99.7% unless the fallback answer (search results + contact) counts as available | Phase 5 SLIs and burn-rate alerts |
| AI inference cost (per conversation) | — | UNKNOWN: human cost per ticket comes from the support lead, needed before Phase 4 | V5 |
| AI inference cost (daily kill switch) | — | UNKNOWN: daily spend ceiling comes from the product owner / finance, needed before Phase 4. Until then a provider-level hard spend limit applies | Phase 1 exit check |
| Index freshness | Article edits live in ≤ 1 h; unpublished or deleted articles gone in ≤ 15 min | ASSUMPTION: policy changes and legal takedowns must not keep being served; content lead to confirm | Phase 2 exit check |
| Operational complexity | One service, one ingestion worker, one Postgres with pgvector; no self-hosted model | ASSUMPTION: small team | Phase 3 exit check |

**Missing inputs (most plan-changing first):**
1. Whether the bot should ever use account data or take actions. This changes the phase order (see above).
2. The help-center platform and its API (Zendesk Guide, Intercom, Confluence, or a custom CMS), and whether restricted or agent-only articles exist.
3. The ticketing / live-chat system used for handoff.
4. Data residency: are there EU or other regional customers? Is an LLM vendor already approved?
5. From the support lead: chat volume, human cost per ticket, current CSAT, target resolution rate, locales.
6. Who the named reviewers are: privacy/DPO, AppSec, legal, support subject-matter experts (SMEs).

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 skeleton | LLM and embedding API accounts, plus internal vendor approval for non-personal data | organizational | deployed |
| Ingestion (Phase 2) | Read-only help-center API credentials; confirmation of how article visibility is modelled | organizational | built |
| V2 / V3 eval sets | Support lead supplies ≥ 300 real questions (redacted from tickets and search logs) and 2 SMEs to grade | organizational | gates can't run |
| Prompt and answer tuning (Phase 3) | V2 | validation | specified |
| Policy-topic answer scope | Legal and support lead decide which topics are quote-only or handoff-only | decision + organizational | specified (Phase 3 prompts) |
| Handoff | Ticketing API credentials; a queue and routing rule agreed with the support team | organizational | deployed (Phase 1) |
| Any real customer text (agent-assist or canary) | V4: data processing agreement (DPA), privacy review, redaction | risk-security + organizational | exposed (starts Phase 0, lead time) |
| Public widget | V1, V3, web-team embed, CSP review | risk-security | exposed |
| Load beyond canary | Provider rate-limit quota increase (lead time) | organizational | scaled |
| Widening past 25% / GA | V5, which needs the human cost per ticket | economic | scaled / committed |

All seven dependency kinds bind somewhere. Structural and runtime ones are already covered by the phase order.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Build vs buy | R2: weeks; transcripts and workflows would end up at the vendor | Build a thin service on hosted APIs; keep eval sets vendor-neutral | In-house control of grounding and handoff is worth the cost | Cheap check in Phase 3: run the help-center vendor's native AI agent on the same V2/V3 sets | The vendor meets V3 at lower cost per resolved conversation with an acceptable DPA |
| Data-privacy boundary | R3: customer personal data sent to and kept by a third party can't be recalled | Redact PII and card numbers (Luhn-valid PANs) before the LLM call and before logging. Vendor under a DPA with no training on inputs and zero or short retention, in a region matching customers. Transcripts kept 30 days (ASSUMPTION, DPO to confirm). Never train on transcripts | A hosted vendor can meet our privacy terms | V4 | V4 fails, or a residency requirement appears → regional endpoint or self-host |
| Which articles get indexed (corpus trust boundary) | R3: an internal article shown to the public can't be un-shown | Only published, public articles; no community posts or comments. Visibility checked at ingest and filtered again at query time | Visibility metadata in the help-center API is reliable | V2 (zero restricted passages) | Logged-in segment content is needed → filter by user permissions at retrieval (new risk dependency, re-plan) |
| Answer scope on policy and commitments | R3: the company can be held to what the bot says | On policy topics, only paraphrase or quote the cited article with a link. Never grant or promise refunds, credits or exceptions; those requests go to a human | Grounding plus a commitment detector catches this | V3 (legal sign-off) | V3 fails on the policy set → those topics become handoff-only |
| Consistency vs availability | R2: changes ingestion and the degraded path | The index is eventually consistent within the freshness budget, with deletions prioritized. During an LLM or vector-store outage, fall back to keyword search results + contact path | Short staleness is acceptable | Phase 2 freshness check; Phase 1 fault injection | Content lead needs instant consistency on policy pages → synchronous re-index on publish |
| Chunk schema + embedding model | R2: changing it means re-embedding and re-evaluating | Section-level chunks carrying article ID, anchor, locale, visibility, updated_at, index_version. Hybrid search (Postgres full-text + pgvector). Embedding model pinned per index version | Articles are well-sectioned | V2 | V2 fails after re-chunking → change the embedding model or add a reranker |
| Prompt+RAG vs fine-tune | R2: fine-tuning means weeks of data and eval work and ties us to a provider | Prompt+RAG | Docs change often, so grounding beats baked-in knowledge | V2, V3 | V3 fails on tone or format despite V2 passing. Only then fine-tune, and never on transcripts without a separate privacy review (R3) |
| Hosted API vs self-host | R2: hidden behind a model interface from Phase 0 | Hosted API (e.g. Claude through Bedrock or Azure for regional + DPA terms) | V4 can be met by a hosted vendor | V4 | V4 fails on residency or retention → open-weights model in our own cloud |

**R1 defaults (cheap to change, no gate):**
- One service plus an ingestion worker, not microservices.
- Answers are synchronous and streamed; ingestion runs asynchronously (webhooks plus a nightly reconcile).
- pgvector in managed Postgres. Basis: ≤ 100k chunks (ASSUMPTION: ≤ 5k articles), far below pgvector HNSW limits.
- Eval harness: pytest/promptfoo in CI.
- Feature flags with the existing flag service.

**N/A:**
- Migration, cutover and system-of-record rows: new capability. The help center stays the system of record for content (one-way read), and the existing contact form / live chat stays as the fallback.
- Tenancy: single company.

## 4. Walking skeleton (Phase 0)
- **The one request:** an allow-listed staff member asks a real question (e.g. "How do I reset my password?") in the production widget. The answer streams back, grounded in a real public article, with a citation link.
- **Tiers it crosses:**
  - the widget on the help-center site (behind a flag)
  - the chat API (container)
  - retrieval: pgvector over one category (~50 public articles), loaded by a one-shot script
  - the hosted LLM behind a one-function model interface
  - the response renderer
  - the trace store
- **Day-zero safety:** user text and retrieved text go into delimited data blocks, never into instructions. Hard caps: 4k input / 512 output tokens per turn, 5 turns per session, and a provider-account spend limit. Links render only to the help-center domain.
- **Deploy, log, monitor, roll back:**
  - Infrastructure-as-code plus a CI/CD pipeline deploys a pinned image.
  - An OpenTelemetry trace per turn has one span per tier. Each trace records the model ID, prompt version and index version.
  - Dashboards cover latency, errors, tokens and cost; there is an error-rate alert.
  - Rollback means redeploying the previous image through the pipeline. The flag off removes the widget.
- **Who can reach it:** staff allow-list only. No customer data: staff ask only about public docs, so V4 isn't needed yet.
- **Exit check V0** (see §6): a real request succeeds with one trace across every tier; one deploy and one rollback go through the pipeline; an injected LLM timeout shows the fallback message and fires an alert; first latency, token and cost values are recorded as baselines (a floor at near-zero load, not capacity). Current CSAT is also pulled from the support tooling as a BASELINE.

## 5. Phases

**Phase 1: Defenses and budgets**
- **Unlocks:** capability work on a guarded path.
- **Depends on:** V0.
- **Tasks:**
  1. PII and card-number redaction before the LLM call and before logs.
  2. Output renderer: HTML escaped, no images, links only to allow-listed domains.
  3. Input and output filters: jailbreak patterns, off-topic generation ("free LLM proxy" abuse).
  4. Rate limits per IP and per session; bot detection on the widget endpoint.
  5. Per-session cost cap and a daily spend kill switch that degrades to search + contact.
  6. Latency timeout and circuit breaker that serve the degraded answer.
  7. Human handoff. The handoff call is the only tool. The executor, not the model, enforces it, and its only argument is the session ID. It creates a ticket with the redacted transcript, and a "talk to a human" button works even if the LLM is down.
  8. Injection and redaction suites in CI.
- **Rollback:** flags per guard; redeploy the previous image.
- **Exit check:** V1 passes. A runaway session is cut off at its cap and emits the metric. Handoff works with the LLM blocked.

**Phase 2: Retrieval**
- **Unlocks:** prompt and answer work on validated context.
- **Depends on:** Phase 1; help-center API access; labeled questions from the support lead.
- **Tasks:**
  1. Fix the chunk schema and index_version.
  2. Full ingestion with a visibility filter.
  3. Webhook-driven incremental sync, deletions prioritized, plus a nightly full reconcile.
  4. Hybrid search with a locale filter at query time.
  5. Blue/green index behind an alias.
  6. Labeled retrieval set of ≥ 200 questions, redacted.
- **Rollback:** swap the alias back to the previous index version.
- **Exit check:** V2 passes. An unpublished test article disappears from results within 15 minutes and an edit goes live within 1 hour.

**Phase 3: Model access, generation, session memory, orchestration**
- **Unlocks:** exposure to real traffic.
- **Depends on:** V2; legal and support decision on policy-topic scope.
- **Tasks:**
  1. Fixed chain: redact → classify intent (in scope / out of scope / handoff) → rewrite the follow-up query → retrieve → generate structured output (answer + cited chunk IDs) → verify.
  2. The verify step: cited IDs must be in the retrieved set, a support check runs, and a commitment-language detector routes to handoff. If verification fails, the bot abstains and hands off.
  3. Session memory: last 6 turns, server-side, 24 h TTL.
  4. Model interface hardened: retries, structured-output parsing, a fallback model set by config.
  5. Prompts versioned in the repo.
  6. Buy-vs-build cheap check (§3).
  7. V3.
- **Rollback:** pin the previous prompt and model config.
- **Exit check:** V3 passes on the primary model, and on the fallback model over the policy subset.

**Phase 4: Agent-assist, then canary**
- **Unlocks:** the economic proof.
- **Depends on:** V1, V3, V4; quota increase; throughput input.
- **Tasks, smallest exposure first:**
  1. Retention deletion job and widget privacy notice go live.
  2. Load test at 2× peak.
  3. Agent-assist for 2 weeks: real incoming chats; agents see bot drafts, customers don't.
  4. Public canary in the first locale: 1% → 5% → 25%. Each step lasts ≥ 3 days and needs zero confirmed uncited commitments and no rise in the error or handoff-failure rate.
  5. Thumbs and reason capture.
  6. Daily review of a redacted transcript sample.
  7. V5 runs at 25%.
- **Rollback:** the kill switch returns to search + contact form. Changing the cohort is a flag change.
- **Exit check:** V5 passes. Latency p95 is within budget at load.

**Phase 5: Feedback loop and GA**
- **Unlocks:** general availability and continuous improvement.
- **Depends on:** V5; SLO target from the product owner.
- **Tasks:**
  1. Feedback loop: reviewed corrections become eval cases (no automatic changes to prompts); content-gap reports go to the docs team.
  2. Widen 50% → 100%.
  3. SLIs and burn-rate alerts; on-call runbook.
  4. Each additional locale passes its own V2/V3 run before being exposed.
- **Rollback:** flag back to the last cohort.
- **Exit check:** the first eval run after reviewed feedback improves the target metric with no regression elsewhere; 30 days of SLO attainment are recorded.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | Staff request, deploy, rollback, injected LLM timeout | The §4 exit check | Trace link, pipeline run records, alert record, `docs/baselines/v0.md` | Phase 1; if it fails, fix the pipeline before any feature work | 0 |
| V1 | Injection and abuse impact is bounded by design | CI suite of ≥ 150 cases: in user input, planted in a test article in a *staging* index, and in handoff text; off-topic abuse prompts; AppSec review | 100% of cases: no link outside the allow-list, no image or HTML, no tool except handoff, handoff argument is only the session ID (ASSUMPTION: code-enforced, so any miss is a bug). Filter neutralization ≥ 0.95 and off-topic refusal ≥ 0.95 (ASSUMPTION). AppSec reviewer pass/fail sign-off | CI report per build; signed AppSec review record | Public exposure in Phase 4; if it fails, tighten renderer or executor and re-review before Phase 4 | Runs in 1; re-run every build; required before 4 |
| V2 | Retrieval finds the supporting passage and never a restricted one | ≥ 200 labeled real questions per index version, plus a probe set targeting restricted, draft and wrong-locale articles | Hit rate@5 ≥ 0.85 (ASSUMPTION; ±5 points at n=200). Zero restricted, unpublished or wrong-locale passages (ASSUMPTION: R3 trust boundary) | Eval report per index_version in CI artifacts | Phase 3 prompt work; if it fails, re-chunk, add a reranker, then change the embedding model (§3); flag content gaps | 2 |
| V3 | Answers are grounded and never invent commitments (guards the worst failure) | 200 general questions plus a ≥ 100-question policy set (refunds, pricing, terms; ≥ 30 unanswerable or "promise me" cases), graded blind by 2 support SMEs; legal review of the policy set | ≥ 0.95 of answers fully supported by their citations (ASSUMPTION). Zero uncited commitments (ASSUMPTION; zero in 100 bounds the rate at ≤ 3% at 95% confidence, so canary review continues). Correct abstain or handoff on ≥ 0.90 of unanswerables (ASSUMPTION). Legal pass/fail sign-off | Graded eval sheet per prompt + model version; legal sign-off record | Phase 4; if it fails, make the failing topics handoff-only (§3) or add a stricter verifier, then re-run | 3 |
| V4 | Customer text can flow to the vendor and logs lawfully and safely | DPA and vendor security review; data-flow review; redaction suite of ≥ 500 seeded synthetic PII strings; card-number scan of log and trace stores after agent-assist day 1 | DPO / privacy counsel sign-off covering: no vendor training, retention terms, region, 30-day transcript retention, widget notice. 100% of Luhn-valid card numbers redacted; ≥ 0.95 recall on emails and phones (ASSUMPTION). Card-number scan finds none | Signed DPA, privacy review record, CI redaction report, scan output (counts only) | Any real customer text (Phase 4); if it fails, regional endpoint or self-host (§3) | Starts in 0; required before 4 |
| V5 | The bot resolves conversations for less than human handling, without hurting satisfaction | 25% canary in the first locale for ≥ 2 weeks against the non-bot control. "Resolved" = no handoff, no ticket from the same visitor within 72 h, not thumbs-down | Cost per resolved conversation below human cost per ticket (UNKNOWN: support lead, before Phase 4). Resolution rate (UNKNOWN: support lead target). CSAT not below the BASELINE from Phase 0 | Canary dashboard export plus cost report | Widening and GA (Phase 5); if it fails, narrow topics, send more to humans, use a cheaper model, or take the buy flip | 4 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Staff allow-list + flag; secrets in a vault; text treated as data; token caps; provider spend limit; traces classified confidential | OTel trace per turn across all tiers; latency, token and cost metrics; error alert | Infrastructure-as-code; pinned image; model, prompt and index version on every trace | LLM timeout → static fallback; pipeline rollback |
| 1 | Redaction before LLM and logs; link allow-list renderer; rate limits; executor-enforced handoff | Metrics on guard trips, redaction counts (counts, not content) and cap hits | Injection and redaction suites versioned in CI | Circuit breaker; spend kill switch; handoff works with the LLM down |
| 2 | Visibility filter at ingest and at query; read-only ingest credential | Sync lag, indexed and deleted counts, retrieval scores per trace | index_version = corpus snapshot + chunker + embedding model; rebuild-from-scratch script | Blue/green alias; nightly reconcile; keyword-only search if the embedding API is down |
| 3 | Citation verifier; commitment detector → handoff; reviewed prompt changes | Spans per chain step; eval scores per prompt version | Prompts and eval sets (redacted) in the repo; model ID pinned | Fallback model by config; retries with backoff; verify fails → abstain + handoff |
| 4 | V4 in place; CSP; penetration test of the public endpoint; retention deletion job | Canary vs control: resolution, handoff, CSAT, cost per resolved conversation; reviewed transcript sample | Cohort assignment logged; config snapshot per canary step | Kill switch; load test at 2× peak; quota headroom |
| 5 | Feedback only through a reviewed queue (blocks poisoning); new red-team cases added to V1; audit of retention deletion | SLIs + burn-rate alerts; eval-drift alerts; content-gap report | Each accepted correction becomes an eval case with its source recorded | On-call runbook; provider-outage drill |

## 8. AI layer

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Prompt-injection / guardrail defense | User text untrusted; docs treated as data; renderer with link allow-list; handoff the only tool | 0 (data blocks), 1 | V1 |
| 2. Cost + latency budget | Token caps per turn and session; daily kill switch; timeout + degraded answer | 0 (caps), 1 | Phase 1 exit check; V5 |
| 3. Human-in-the-loop gating | Bot never commits to anything; refund, exception and account requests plus failed checks go to a human; always-on "talk to a human" | 1, 3 | V3; Phase 1 exit check |
| 4. Retrieval | Hybrid pgvector + full-text; public articles only; freshness sync | 2 | V2 |
| 5. Model access | Hosted LLM behind an interface; fallback model by config; structured output | 0 (interface), 3 | Phase 3 exit check (V3 on the fallback model) |
| 6. Memory | Session-only, last 6 turns, 24 h TTL; no memory across sessions | 3 | Follow-up questions resolve in V3 set; nothing persists after the TTL |
| 7. Orchestration | Fixed chain, not an agent | 3 | V3; every step traced |
| 8. Routing | Not needed: one model, plus a fallback for outages | — | — |
| 9. Feedback | Thumbs, agent corrections and handoff outcomes → reviewed queue → eval set and content gaps | Capture in 4, loop in 5 | Phase 5 exit check |

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Answers using account data, or actions like refunds and order status.** Pulled forward when product requires them; that means re-planning from Step 1 with auth, retrieval authorization and approval gates.
- **Content for logged-in customer segments.** Pulled forward when a segment's articles are needed; it requires a permission filter at retrieval and a new gate.
- **Model routing / multiple models.** Pulled forward if V5 fails on cost or volume grows past provider quotas.
- **Memory across sessions.** Pulled forward when repeat users need earlier context and a privacy review approves it.
- **Fine-tuning.** Pulled forward only on the §3 flip condition.
- **Locales beyond the first.** Each needs its own labeled set passing V2 and V3.
- **Self-hosted model.** Pulled forward if V4 fails or a residency requirement appears.
- **Multi-region high availability.** Pulled forward if the SLO the product owner sets is above what one region plus the fallback answer can meet.
- **Email and voice channels, or agent-assist as its own product.** Pulled forward on demand or on Phase 4 agent-assist results.
