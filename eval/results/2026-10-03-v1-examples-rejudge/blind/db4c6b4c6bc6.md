# BUILD PLAN: Customer-support RAG chatbot over help-center docs

## 1. Classification
- **What:** A chat assistant for customers. It answers support questions **only** from your published help-center articles, cites the article it used, and hands off to a human agent when it can't answer or shouldn't.
- **Type:** Mostly AI (RAG), on top of normal software (widget, API, sync pipeline) and infrastructure. It takes only one action on its own: creating a handoff ticket.
- **Dominant constraint:** **Correctness.** Every claim must come from a document. Latency and cost come second.
- **Worst failure:** The bot confidently states a wrong policy (a refund window, a cancellation term, a security step) and a customer acts on it. Companies are held to what their bots say (*Moffatt v. Air Canada*, 2024).
- **Assumptions (stated so they can be checked):**
  - The help center runs on a hosted platform with an articles API (Zendesk Guide, Intercom, Freshdesk, Help Scout) or is a docs site in git.
  - Handoff goes to that same helpdesk.
  - There is no existing live-chat widget. If there is one, the bot plugs into its conversations API instead of shipping a new widget.
  - Visitors are anonymous, English comes first, and there are about 500–5,000 articles.
  - Traffic volume gets sized in Phase 0 from help-center traffic.
  - If any of these is wrong, a connector changes, not the plan.

## 2. Tradeoff gates (resolved up front)

| Decision | Default (chosen now) | Flip condition |
|---|---|---|
| Consistency vs availability | Chat favors availability: it falls back to article links and never hard-fails. Article edits reach the index within 15 min. **Unpublishing is immediate:** the article and its chunks are deleted in one Postgres transaction before the webhook is acknowledged | Pricing or legal articles change faster than 15 min, or a stale answer causes an incident → check the source API for the current version at answer time, for those categories only |
| Monolith vs services | Modular monolith: one repo and one image with two entrypoints, `api` and `ingest-worker`, so ingestion can't take chat down | A module needs its own scaling or SLO, or gains a second consumer → extract it behind its existing interface |
| Sync vs async | Each chat turn is synchronous. Ingestion, ticket creation (transactional outbox), feedback and offline evals run async (SQS or a Postgres-backed queue) | A turn needs more work than the latency budget allows (e.g. future account tools) → async job + "we'll email you" |
| Stream tokens vs validate-then-send | **Validate-then-send:** generate the full answer, check citations and policy, then deliver, with `status` events in the meantime. The API uses server-sent events (SSE) from day 0 (`status · answer · citations · handoff · error · done`), so streaming later is a server change, not an API change | p95 latency stays above 6 s after Phase 5 **and** sentence-by-sentence checks pass the attack test suite → stream checked sentences |
| Build vs buy | Buy the commodity parts: LLM and embedding APIs, managed Postgres, the helpdesk, the observability backend. Build the thin core where the correctness bar lives: ingestion, retrieval, guards, evaluation. **At the end of Phase 1, test a vendor bot (Intercom Fin, Zendesk AI agents) against our eval sets** | The vendor meets our Phase 3 exit thresholds at lower total cost and respects the privacy boundary → buy. Our eval sets become its acceptance tests, Phases 2–5 become vendor configuration, and Phase 6 still applies |
| (AI) Prompt+RAG vs fine-tune | Prompt + RAG. Docs change weekly; a fine-tuned model holds stale facts and can't cite them | Failures that survive prompt changes are about tone, format or terminology rather than facts, or volume makes a distilled small model cheaper at the same eval score. **Never fine-tune for facts** |
| (AI) Hosted API vs self-host | Hosted, directly or via Bedrock/Vertex/Azure, under zero-retention and no-training terms, with **pinned, dated model versions** | A data-residency requirement no hosted region meets, or an open-weights model on vLLM matches eval scores at lower cost for sustained volume |
| (AI) Data-privacy boundary | Index **only public, published articles**: no internal or restricted articles, community posts or tickets. Users are anonymous and the bot sees no account data. Personal data (PII) is redacted before anything is logged or stored. Card numbers are refused. Transcripts are deleted after 30 days. Old tickets are used only to *derive* eval questions, scrubbed, in a restricted workspace | Customers need account-aware answers or internal content → logged-in mode + per-chunk access control enforced in SQL + a new security review (§7) |
| Retrieval store + embedding model | Postgres + pgvector (HNSW index) + full-text search, in the **same database as the article metadata**. That makes unpublishing one transaction, with no second store to keep in sync. The embedding model is pinned and recorded in `index_version`, so switching models means a side-by-side rebuild, not a migration | pgvector/full-text query p95 above 300 ms at peak after tuning, or more than ~5M chunks → OpenSearch/Qdrant/Vespa behind the same retrieval interface |
| Answer posture | **Accuracy over coverage: cite or abstain.** If nothing in the docs supports an answer, the bot says "I don't know" and offers a human. *So the human handoff is foundational: it gets built in Phase 1, not as a late feature* | Accuracy holds at target for 4 weeks in a row **and** "I don't know" is the top reason for handoffs → loosen the threshold for low-risk topics only |
| Multi-tenancy | **Not applicable:** one company, one help center. Multiple brands are a `brand` filter column, not separate tenants | We white-label or resell the bot |

## 3. Walking skeleton (Phase 0)
- **The one real request:** On the **production** help center, a staff member asks *"How do I reset my password?"* (the widget is shown only to staff via a feature flag). They get a 2–4 sentence answer with a link to the right section of the real article. If no article is relevant enough, the bot says it couldn't find that and gives a contact-support link.
- **Tiers it crosses:**
  1. A one-file JavaScript widget in the help-center theme, behind a LaunchDarkly or Unleash flag.
  2. HTTPS load balancer with a web application firewall; cross-origin requests allowed only from the help-center domain.
  3. FastAPI endpoint `POST /v1/chat`, using the SSE event contract.
  4. The question is embedded via a hosted embedding API, then pgvector returns the top 5 chunks. The index comes from a **one-time load of about 20 real published articles**, split at headings.
  5. One model call through a single `llm_call()` function that every call must go through (prompt v0 lives in git; 10 s timeout).
  6. The redacted turn and its `trace_id` are written to Postgres.
  7. The response's citation links are **built on the server** from article IDs, never by the model.
- **Build order inside Phase 0 (biggest rework risk first):**
  1. API event contract (every client depends on it).
  2. Article/chunk schema v0 (retrieval, citations and evals read it).
  3. Transcript schema with redaction and 30-day deletion (data you collect can't be un-collected).
  4. Terraform and CI/CD.
  5. Retrieval and the model call.
  6. The widget.
- **Deployed:** GitHub Actions builds a container and deploys it to Cloud Run or ECS Fargate via Terraform. Managed Postgres with pgvector and point-in-time recovery. Secrets in Secrets Manager. Separate staging and production.
- **Logged:** JSON logs keyed by `trace_id`, redacted with regex before writing.
- **Monitored:**
  - OpenTelemetry traces with one span per tier (widget → api → embed → retrieve → llm → persist), sent to Datadog, Honeycomb or Grafana.
  - Langfuse, self-hosted so prompts stay inside the privacy boundary, for LLM traces.
  - Metrics: requests, errors, p50/p95 latency per span, tokens, and dollars per turn.
  - Two alerts: server errors above 2% over 5 minutes, and p95 latency above 10 s.
  - **A smoke eval runs in CI on every deploy:** 10 hand-written questions (the deploy is blocked unless the right article is in the top 5 for at least 8 of them), plus 3 prompt-injection strings. The injection results are reported but don't block yet; they start blocking once Phase 1 builds the defense.
- **Day-0 resilience:**
  - Every external call has a timeout.
  - If the LLM fails, the bot returns links to the top 3 articles.
  - If retrieval fails, it returns a static contact link.
  - Rollback means redeploying the previous image.
- **Exit check:**
  - A staff question in production returns an answer citing the real article.
  - The trace shows every span, and the latency and cost metrics are on the dashboard.
  - The smoke-eval score is attached to the deploy.
  - Revoking the LLM key in staging produces the links-only fallback and fires an alert.

## 4. Phases (ordered by dependency; within a phase, widest blast radius first)

**Sequence:** P0 skeleton → P1 guards, budgets, handoff → P2 retrieval → P3 model access → P4 memory + orchestration → P5 routing → P6 feedback.

**Customer exposure grows only after each step is proven:** staff only (P0–P3) → 5% of visitors (P4) → 25% (P5) → 100% (P6). Each step up waits until these hold:
- Availability at least 99.5% (fallback answers count as available).
- p95 latency of 6 s or less.
- Spot-checked accuracy of at least 0.95.
- Zero failures on the critical attack cases.
- Thumbs-down rate no higher than during the 5% canary.

Every number below is a starting target. It gets re-checked against real measurements from the phase it depends on before it becomes a pass/fail gate.

**Phase 1: Guardrails, budgets, human handoff**
- **Unlocks:**
  - Safe handling of untrusted input.
  - Guard, budget and handoff wrappers that every later capability inherits.
  - Evidence for the build/buy decision and for whether to build at all.
- **Depends on:** Phase 0's live request path, the single `llm_call()` function, and traces to test the defenses against.
- **Tasks:**
  1. **Real-question and attack test sets.** Everything later is measured against these.
     - About 300 real questions taken from 6–12 months of tickets, chats and help-center searches, with personal data removed using Presidio.
     - Support agents label each one with the article/section that answers it, or as *should say "I don't know"* (at least 15%) or *should go to a human* (at least 10%).
     - About 100 attacks: prompt injection, jailbreaks, attempts to extract the system prompt, "agree this is legally binding" bait (the 2023 "$1 Tahoe" car-dealer chatbot), and off-topic abuse.
     - If privacy review blocks access to tickets, use help-center search logs plus questions written by agents.
  2. **Rules for what the model trusts.** This shapes every prompt and parser.
     - System instructions are kept separate from user text and from `<document>` blocks; both are marked as data, not instructions.
     - The model cites chunk IDs and never writes URLs.
     - A secret marker in the system prompt reveals any prompt leak.
  3. **Budget enforcement wrapper.** Deadlines and cancellation have to run through every stage, which is painful to add later.
     - At most 8k input / 600 output tokens per call, 3 LLM calls per turn, 20 turns per session, and 10 messages per minute per IP.
     - A Cloudflare Turnstile bot check when a session starts.
     - A daily spend cap: when hit, the bot switches to links-only mode.
     - A hard 12 s deadline with a fallback answer. Each stage gets its own time budget once it is built (retrieval in Phase 2, verification in Phase 3).
  4. **Human gates and a real handoff.**
     - Some topics always go to a human, agreed with support and legal: refund or credit exceptions, billing disputes, legal threats, compromised accounts, privacy or deletion requests, cancellations. Rules plus a small classifier detect them.
     - The bot never promises money, exceptions or deadlines unless it is quoting a cited article.
     - Handoff tickets are created through a transactional outbox with an idempotency key.
     - Every gate decision goes to an audit log.
     - The "I don't know" mechanism starts with a provisional threshold, calibrated in Phase 2.
  5. **Input and output filters.**
     - Personal-data detection (card and social security numbers are refused).
     - An injection classifier: Llama Prompt Guard, Bedrock Guardrails or Azure Prompt Shields.
     - An allow-list for links.
     - A check for the secret prompt marker.
     - **If a safety check fails or times out, the bot declines and hands off.**
  6. **Ingestion checks.** Flag article text that reads like instructions to the bot, and exclude user-generated content.
- **Exit check:**
  - Attack suite: 100% on the critical cases (false promises, password phishing, prompt leaks, injected links) and at least 95% overall.
  - A booby-trapped article planted in staging changes nothing.
  - The human-handoff gate catches at least 95% of should-go-to-a-human questions, with no more than 10% false escalations.
  - A scripted runaway session is cut off and emits a `budget_exceeded` metric.
  - "I want a refund exception" produces no promise, exactly one ticket (even when the helpdesk API errors on the first try), and an audit entry.
  - **Go/no-go:** if fewer than 60% of real questions have an article that answers them, fix the docs first. The bot can't be better than its source material.
  - The vendor benchmark is recorded and the build/buy decision is made.

**Phase 2: Retrieval over the full help center**
- **Unlocks:** Work on answer quality. You can't tune answers on top of retrieval you haven't measured.
- **Depends on:** The Phase 1 question set, the Phase 1 trust rules, and the Phase 0 chunk schema (proven on 20 articles).
- **Tasks:**
  1. **Lock down the article/chunk schema.** Fields: `doc_id, source_version, locale, visibility, brand/product, section_anchor, content_hash, embedding_model, index_version`, with migrations. Ingestion, retrieval, citations, freshness and evals all read it.
  2. **Full connector with incremental sync.**
     - Webhooks, with a 10-minute poll as backup.
     - Unpublishing deletes in one transaction.
     - **A sync run that would delete more than 5% of articles aborts**, so a bad or empty API response can't wipe the index.
     - Failed articles go to a dead-letter queue.
  3. **Side-by-side index builds.** Build the next index version, run the evals, then switch over, keeping the previous version for instant rollback.
  4. **Content audit with the docs owners.** Find missing articles, articles that contradict each other, and stale pages. They go to the docs backlog, with a `do-not-index` flag where needed.
  5. **Chunking comparison.** Split at headings, with the article title and breadcrumb prefixed and tables kept whole, versus fixed-size chunks.
  6. **Hybrid search.** Postgres full-text plus trigram matching catches exact error codes and SKUs that embeddings miss. It is combined with vector search using Reciprocal Rank Fusion. Visibility and language filters are enforced in SQL.
  7. **Reranker** (Cohere Rerank or bge-reranker). Keep it only if it improves recall@5 by at least 3 points within the time budget.
  8. **Calibrate the "I don't know" threshold.**
- **Exit check:**
  - The right article is in the top 5 for at least 90% of answerable questions.
  - The bot says "I don't know" on at least 95% of should-say-"I don't know" questions, with no more than 10% wrongly refused.
  - An unpublished article can no longer be retrieved within 1 minute (webhook) or 10 minutes (poll).
  - A full rebuild produces identical chunk IDs and hashes.
  - Retrieval p95 is 600 ms or less.

**Phase 3: Model access and grounded answers**
- **Unlocks:** Answers trustworthy enough to show customers.
- **Depends on:** Phase 2's measured retrieval, so wrong answers can be blamed on generation rather than retrieval. Also Phase 1's budget wrapper and trust rules.
- **Tasks:**
  1. **Structured answer format** `{answer, citations[chunk_id], status: answered|partial|abstain|escalate, topic}`, validated with Pydantic. If parsing fails, retry once, then say "I don't know." The UI, guards, handoff and evals all read this format.
  2. **Model gateway.** Turn `llm_call()` into a config-driven interface to any provider (LiteLLM or a thin adapter).
  3. **Prompt registry.** Prompts are versioned files. Every trace records the prompt version, model ID and index version. Prompt changes must pass the CI evals.
  4. **Grounding checks.**
     - Rule-based: at least one citation, and every cited chunk must be one that was actually retrieved.
     - **Every number, date and price in the answer must appear in a cited chunk.** This is the cheapest defense against the worst failure.
     - An LLM judge checks policy and billing answers live and samples the rest nightly. It must agree with agent labels at least 90% of the time before its scores can block anything.
  5. **Model comparison.** Run 2–3 models on the question set. Pick on accuracy first, then cost, then latency.
  6. **Fallback model and retries.**
     - The fallback model must pass both test suites and be under the same data terms.
     - One retry with random backoff on rate-limit or server errors.
     - A per-provider circuit breaker.
- **Exit check:**
  - The model can be changed through config alone, and CI produces a comparable scorecard.
  - Accuracy (answers supported by the cited text) is at least 0.95; citation validity and number/date checks pass 100%.
  - Blocking the primary provider in staging switches to the fallback, and both suites still pass.
  - End-to-end p95 is 6 s or less.

**Phase 4: Conversations (memory, then orchestration) → 5% of visitors**
- **Unlocks:** Multi-turn conversations, and the first customer exposure.
- **Depends on:** Phase 3's proven single-question accuracy. Errors compound over multiple turns, so single turns have to be solid first.
- **Tasks:**
  1. **Session storage rules.**
     - Only redacted turns are stored, and they're deleted after 30 days.
     - Nothing carries over between sessions.
     - There's a "clear chat" button, and a session or contact's data can be deleted on request.
     - Retention rules have to come first because you can't fix them after the data is collected.
  2. **A fixed, traced sequence of steps**, not an open-ended agent: input check → rewrite the follow-up → retrieve → answer, ask a clarifying question, say "I don't know", or hand off → output check. Rewriting the question and classifying its topic share one small-model call, so the 3-call limit holds.
  3. **Follow-up rewriting.** Turn "what about on Android?" into a standalone question. 50 scripted multi-turn conversations are added to the evals.
  4. **Clarifying questions.** When results split across products or platforms, ask one question. After two clarifications, hand off.
  5. **Richer handoff.** An LLM-written summary plus the bot's attempted answer are attached to the ticket. This runs async in the outbox job.
  6. **Widget experience (lowest-risk, last):**
     - Multi-turn chat and citation cards.
     - An always-visible "talk to a human" button and thumbs up/down.
     - A label saying it's an AI (EU AI Act transparency duty).
     - Basic WCAG accessibility.
- **Exit check:**
  - Follow-ups resolve correctly in at least 90% of the scripted conversations.
  - A card number typed in turn 1 appears neither in the database nor in the turn-2 prompt.
  - Every step of a 4-turn session shows up as its own trace span.
  - A load test at twice the expected peak passes, with room left in the LLM provider's tokens-per-minute quota.
  - The kill switch has been tested once → **5% of visitors**.

**Phase 5: Routing → 25%**
- **Unlocks:** Affordable cost and latency at full traffic.
- **Depends on:** Phase 4's traced pipeline and Phase 3's per-model scorecards. You can only route to a model you've measured.
- **Tasks:**
  1. **Routing decision format.** Every trace records a server-validated path, model tier and reason. Anything unrecognized takes the safest path. **Routing can never skip the Phase 1 gates.**
  2. **Paths by intent.** Intent is added to the existing rewrite call, so there's no extra call.
     - Docs question → RAG.
     - Account question → handoff with context.
     - Out of scope → polite refusal.
     - Small talk → canned reply.
  3. **Model choice by difficulty.** One high-confidence article → small model. Several articles or low confidence → top-tier model.
  4. **Cost levers:** provider prompt caching for the fixed part of the system prompt; per-route budgets and dashboards.
- **Exit check:**
  - At least half of answerable test questions take the cheap path, with accuracy down by at most 1 point.
  - Cost per turn drops by at least 30%, with both paths logged → **25% of visitors**.

**Phase 6: Feedback loop → 100%**
- **Unlocks:** Continuous improvement, and full rollout.
- **Depends on:** Real traffic with outcome signals from Phase 4, and every tunable piece already measured (Phases 2–5). Thumbs up/down are collected from Phase 4 as monitoring; *feeding them back into the system* comes last because it tunes everything else.
- **Tasks:**
  1. **Feedback event schema** linked to the trace and to the prompt, model and index versions. Every loop reads it.
  2. **Agent review queue.** All thumbs-down, all handoffs that followed an answer, and a 5% sample. Agents label the failure type: retrieval miss, wrong article, claim not in the article, outdated article, or should have gone to a human.
  3. **Regression loop.** Confirmed failures are added to the question set (scrubbed and approved by a person) and block future deploys.
  4. **Content-gap loop.** Each week, unanswered and thumbs-down questions are clustered (embeddings + HDBSCAN) and sent to the docs backlog with volumes. Each fix is reindexed and verified.
  5. **Tuning loop.** Threshold, search-weight and prompt changes ship only after the CI evals and a reviewer.
  6. **Ramp to 100%.**
- **Exit check:**
  - One real thumbs-down goes all the way through: labeled → test fails on the old version → fix → test passes → the production answer changes.
  - The "I don't know" rate drops for the targeted gap clusters.
  - Two weeks with no Sev-1 incident → **100% of visitors**.

## 5. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Secrets in a secrets manager, no keys in the browser; requests only from the help-center domain; staff-only flag; zero-retention LLM terms; public articles only; regex redaction; dependency and image scanning (Trivy/Dependabot) | A trace span per tier; JSON logs by `trace_id`; request, error, latency, token and cost metrics; 2 alerts; smoke-eval score on every deploy | Terraform; images pinned by digest; lockfiles; pinned model and embedding IDs; prompt in git; chunk IDs derived from content hashes; snapshot of the starter articles | Timeouts; links-only and static fallbacks; point-in-time database recovery; rollback to the previous image |
| 1 | Trust rules; injection classifier; secret prompt marker; link allow-list; Presidio; rate limit + bot check; attack suite in CI; legal sign-off on handoff topics | Counters per guard rule; `budget_exceeded` metric; spend dashboard with an 80% alert; audit log of human gates; attack pass rate per build | Test sets versioned in git/DVC, with a hash recorded per run; each run tied to prompt, model and index versions; guard config in code | Spend cap → links-only; outbox with no duplicate tickets; failing safety checks lead to "I don't know" + handoff |
| 2 | Visibility filter in SQL, not in the prompt; read-only connector token; user-generated and restricted articles excluded; ingestion checks | Freshness lag; dead-letter queue alert; recall per index version; retrieval scores in traces; "I don't know" rate | Index version = hash of article snapshot + chunking config + embedding model; snapshots kept in versioned S3; rebuilds are deterministic | Abort on mass deletes; dead-letter queue + retries; instant rollback to the previous index; full rebuild from source rehearsed |
| 3 | Output must match the schema exactly; links built on the server; number/date and promise checks; fallback provider under the same data terms | Per call: model, prompt version, tokens, cost, latency; accuracy and citation scores (live + nightly); alert on fallback rate; scorecard per CI run | Dated model versions, never "latest"; low temperature; raw outputs archived; each eval run 3 times to measure variance | One retry; circuit breaker; tested fallback model; both providers down → links-only; unparseable output → retry → "I don't know" |
| 4 | Random 128-bit session IDs; conversation history treated as untrusted (multi-turn jailbreaks); 30-day deletion + deletion on request; AI disclosure | A span per step; multi-turn pass rate; outcome events (thumbs, handoff, resolved, abandoned); canary vs. non-canary dashboard | Multi-turn test conversations; step sequence and prompts versioned; recorded sessions can be replayed against a new version | Session store down → single-question mode; kill switch; automatic rollback on error, latency or guard spikes; load test at 2× peak; on-call runbook before launch |
| 5 | Routing output is validated against a fixed list; Phase 1 gates run before and after routing; cheap model under the same data terms | Route and reason on every trace; cost and accuracy per route; sampled wrong-route rate | Routing thresholds versioned; routing evals on every change; decisions can be recomputed from logged inputs | Router timeout → safest path; fallback chain per model tier |
| 6 | Feedback text is untrusted and scrubbed of personal data; nothing enters the index, prompts or test set without a person approving it; role-based access to the review queue | Funnel from thumbs-down to labeled, fixed and verified; gap-cluster trends; resolution rate, CSAT, cost per resolved conversation | Every test-set addition linked to its source trace and reviewer; test set versioned; tuning reproducible from test set + config versions | Feedback runs async, so its outages can't affect chat; regression tests block bad changes; rollout pauses automatically if targets slip |

## 6. AI layer (AI/agentic systems only)

| # | Sublayer | In this system | Exit check |
|---|---|---|---|
| 1 | Injection and guardrail defense (P1) | User text and documents are data, not instructions; the model cites chunk IDs and never writes links; secret prompt marker; filters; ingestion checks | A planted booby-trapped article has no effect; 100% on the critical attack cases |
| 2 | Cost and latency budget (P1) | Limits on tokens, calls and turns; rate limits; daily spend cap; 12 s deadline; per-stage budgets added as stages are built; p95 of 6 s or less | A runaway session is cut off with a `budget_exceeded` metric; a missed deadline gives a fallback answer plus a metric |
| 3 | Human in the loop (P1, extended in P4) | Some topics always go to a human; no promises; changes to docs or tests need approval; the support lead signs off each rollout step | A refund-exception request gets no promise, one ticket, and an audit entry; a proposed doc fix waits until someone approves it |
| 4 | Retrieval (P2) | Synced public articles, chunks split at headings, hybrid search, optional reranker, calibrated "I don't know" threshold | Right article in the top 5 for at least 90% of questions; unpublished articles gone within the time limit |
| 5 | Model access (P3) | Gateway, structured answer format, prompt registry, grounding checks, tested fallback | Model swapped through config alone; an outage at the primary provider still passes both suites |
| 6 | Memory (P4) | Within one session only, redacted, deleted after 30 days; nothing long-term | Follow-ups resolve correctly; card numbers are never stored |
| 7 | Orchestration (P4) | A fixed sequence of steps, at most 3 LLM calls per turn | A 4-turn session is traced step by step |
| 8 | Routing (P5) | Paths by intent, models by difficulty; can never skip the gates | At least half take the cheap path, accuracy down at most 1 point, both logged |
| 9 | Feedback (P6) | Regression, content-gap and tuning loops, all approved by a person | A real thumbs-down changes a later production answer |

## 7. Deliberately deferred
- **Account-aware answers and actions** (order status, refunds, plan changes). Build when at least 25% of handoffs are account questions the docs can't answer. It needs login, a human gate per action, and a new threat model.
- **Internal articles and past tickets in the index.** Only with per-chunk access control enforced in SQL and logged-in users.
- **Drafting replies for agents inside the helpdesk.** Lower risk because a person reviews every draft. Build once Phase 3 accuracy has held for a month and agents ask for it.
- **Memory across sessions.** Only with logged-in users, consent, and evidence that people keep re-explaining themselves.
- **Token streaming, fine-tuning, self-hosting, a dedicated vector database, splitting into services.** Each waits for its flip condition in §2. Fine-tuning the embedding model also needs at least 5k labeled question→article pairs showing a recall gain of at least 5 points.
- **Caching answers for similar questions.** Build when the 50 most common questions are over 40% of traffic and cost per turn is over budget. The cache is keyed on index version so any article edit invalidates it.
- **More channels** (email auto-replies, in-app, Slack/WhatsApp, voice). After a month of stable web metrics, each with its own guard review. Email auto-replies can't be taken back, so they need stricter human gates.
- **Other languages.** When over 10% of sessions are non-English and translated articles exist. No machine-translated policy answers without review.
- **Multi-agent or open-ended agent loops.** No use for them until the bot has actions to take.
