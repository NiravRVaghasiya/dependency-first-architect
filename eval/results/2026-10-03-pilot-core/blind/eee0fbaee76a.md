I've built the plan on four assumptions. If any of them is wrong, the phase order changes, so please check them first:

1. **The bot only answers questions and hands off to a human.** It doesn't look up accounts or take actions like refunds.
2. **Every doc in the help center is public.**
3. **Your help desk doesn't already include an AI agent you pay for.**
4. **You run the existing help-center CMS and help desk, and they stay as they are.**

What changes if each one is false is covered under *Missing inputs*.

# Plan — Customer-support RAG chatbot over help-center docs

## 1. Classification and constraints
- **What:** A chat widget on the help center that answers customer questions using only published help-center articles, cites them, and hands off to a human through the existing help desk.
- **Type:** AI system (RAG) plus software. It's a new service that plugs into an existing help-center CMS and help desk. Not a small build, because outside customers use it and their chat text contains personal data.
- **Dominant constraint:** Correctness. Answers must be backed by the docs.
- **Worst failure:** The bot states a policy or makes a promise (a refund, price, deadline or security step) that the docs don't support, customers act on it, and the company is held to it. In Moffatt v. Air Canada (2024), a tribunal held the airline liable for its chatbot's made-up refund policy. Gate V5 guards this. Close behind: leaking customer personal data or non-public content (V1), and being tricked into abusive or off-brand output (V2).

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency | p95 time to first token ≤ 2 s; p95 full answer ≤ 10 s; hard timeout 20 s, then a fallback that shows relevant articles | ASSUMPTION (typical for live chat; product to confirm) | V3 |
| Throughput | Peak number of chats at once. **UNKNOWN:** support ops to supply current help-center traffic and contact volume; needed by Phase 1 (V3 load test) | UNKNOWN | V3 |
| Availability / SLO | 99.5% per month for the chat API | ASSUMPTION (low-critical: site search and the contact form remain as fallback) | Phase 4 SLO dashboard |
| RPO / RTO | Index: no backup needed, rebuilt from the CMS in ≤ 1 h. Chat transcripts: RPO 24 h | ASSUMPTION (corpus assumed to be a few thousand articles) | Phase 2 exit check |
| Index freshness | Unpublished article gone from answers ≤ 15 min; edits live ≤ 1 h | ASSUMPTION (a withdrawn policy is the worst-failure path) | Phase 2 exit check |
| AI inference cost | Per turn: ≤ 12k input and ≤ 1k output tokens, one generation call. Per session: ≤ 20 turns. Daily spend kill-switch: **UNKNOWN**, finance to supply by Phase 1. Cost per resolved conversation: **UNKNOWN**, set against the cost of a human-handled ticket, which support ops must supply in Phase 0 | ASSUMPTION / UNKNOWN | V3, V6 |
| Operational complexity | 1 chat service, 1 ingest worker, pgvector inside the Postgres you already run; owning team's existing on-call | ASSUMPTION | Phase 2 |

**Missing inputs** (most plan-changing first):
1. **Does your help desk (Zendesk, Intercom, Freshdesk, Salesforce) already include an AI agent?** If yes, the plan starts by evaluating the vendor, and Phases 2–3 become acceptance tests for it instead of things you build.
2. **Are any articles non-public** (internal macros, plan-gated content)? If yes, retrieval has to check who the caller is. That needs login and per-user access checks before Phase 2, as a new decision that is costly to reverse.
3. **Should the bot ever see account data or take actions?** I assumed no (see §10).
4. Which countries and languages you serve. EU users bring GDPR, and possibly the EU AI Act Art. 50 rule that people must be told they're talking to an AI; legal should confirm. I assumed one launch language.
5. Baselines from support ops: contact volume, cost per ticket, CSAT, top contact reasons, and help-center search logs.

## 2. Dependencies

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 model call | LLM provider account and procurement approval | organizational | built (Phase 0) |
| Phase 0 ingest | Read-only CMS API token | organizational | built (Phase 0) |
| V4, V5 | Labeled set of ≥ 300 real questions, labeled by support staff. Starts in Phase 0 because it takes time | organizational | V4/V5 cannot pass |
| V3 load test | Peak-traffic figure from support ops | organizational | V3 cannot pass |
| V6 | Cost per human ticket, CSAT and contact-rate baselines from support ops and finance | organizational / economic | V6 cannot pass |
| Any customer exposure | Signed data agreement with the LLM provider (no data retention, no training on our data), privacy review, AI-disclosure wording from legal | organizational / risk-security | exposed (V1) |
| Detailed answer-generation design (Phase 3) | Retrieval quality proven | validation | specified (V4) |
| Any customer exposure | Injection containment, cost caps, faithfulness | risk-security | exposed (V2, V3, V5) |
| Handoff ticket creation | Help-desk API write access, plus support leads agreeing which queue and tags handoffs use | organizational | exposed (Phase 4) |
| Widening beyond the canary | Cost per resolved conversation and outcome metrics hold | economic | scaled (V6) |
| Fixing missing-content failures found by V4/V5 | Time from the docs/content team | organizational | hardened (Phase 2–3) |
| Build-vs-buy check | The V4/V5 question set exists | decision | Phase 3 specified |

There are no surprising structural or runtime dependencies.

## 3. Key decisions (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Revisit trigger |
|---|---|---|---|---|---|
| Build vs buy | R2: weeks of re-integration, and transcripts would build up at the vendor | Build a thin RAG service on a hosted model API | No help-desk AI agent you already license meets V4/V5 at a lower total cost | Cheap check at the start of Phase 2: run the V4/V5 question set against a trial of the vendor agent | Vendor passes V5 at ≤ our cost per resolution → buy, and keep V1/V2/V5/V6 as acceptance tests |
| Data-privacy boundary | R3: personal data, once exposed, can't be recalled; transcripts at an LLM vendor are hard to get back | No account data in v1. Personal data is removed before logs and analytics. Raw transcripts stay in our region with 30-day retention (ASSUMPTION). The LLM provider is under a data agreement with no retention and no training. We never train on transcripts | Privacy and legal accept this boundary | V1 | V1 fails, or legal requires the model to run in-region → switch to an in-region cloud endpoint, or stop storing raw transcripts |
| What gets indexed | R3: a non-public article shown to the public can't be unshown | Index only articles the CMS marks public and published. Filter when indexing, and check visibility again when answering. No forum or community content | The CMS visibility flags are accurate | V1 | Non-public content is needed → retrieval checks the caller's permissions (new costly-to-reverse decision; login required before Phase 2) |
| Answer policy (grounded only vs general knowledge) | R3: answers customers see can become promises with legal liability | Answer only from retrieved passages, with citations. Decline and offer handoff when retrieval is weak or a claim isn't supported. For refunds, billing, pricing, legal and security, give a link to the article plus a handoff, never a paraphrased promise | Strict grounding keeps unsupported claims within V5 and still resolves enough chats | V5 (safety), V6 (usefulness) | V5 fails on a topic → that topic shows article snippets only, no generated text. V6 resolution too low → allow more topics, then re-run V5 |
| Index freshness vs availability | R2: users could act on a withdrawn policy | Index updates from CMS webhooks plus a nightly full reconcile. Unpublish events take a priority path. If the index is down, the bot shows search results and the contact form, never an ungrounded answer | Webhooks are reliable enough to meet the freshness target | Phase 2 publish/unpublish test against the freshness budget | Lag exceeds the budget → check each cited article's visibility against the CMS at answer time |
| Hosted API vs self-host | R2: prompts and evals need re-tuning per model; the swap point is built in Phase 0 | Hosted frontier model through an in-region cloud endpoint, behind our own provider interface | A no-retention agreement is available and latency fits the budget | V1 (contract), V3 (latency) | Legal rejects every hosted option, or cost at volume misses the V6 target and self-hosting is cheaper |

**minor defaults:**
- Prompt + RAG instead of fine-tuning (fine-tuning is deferred).
- Monolith instead of services: one chat service and one ingest worker.
- Sync vs async: replies stream back synchronously; ingest runs in the background.
- Vector store: pgvector. With a small corpus, re-embedding takes hours, so the embedding model and chunker are pinned per index version.
- No agent framework, plain code.
- Anonymous access, with per-IP and per-session rate limits.
- Handoff creates a ticket through the help-desk API, only after the user clicks to confirm.

**N/A:** migration strategy, cutover and decommissioning. Nothing is replaced: site search and the contact form stay as the fallback. The CMS stays the source of truth for content (the bot never writes to it), and the help desk stays the source of truth for tickets.

## 4. First end-to-end slice (Phase 0)
- **Real request:** A staff member asks in the live help-center widget, "How do I reset my password?" The answer streams back and cites the password-reset article.
- **Tiers it crosses:**
  1. Widget, behind a feature flag.
  2. Chat API container.
  3. Retrieval from pgvector, loaded with about 20 public articles pulled read-only from the CMS API.
  4. Hosted LLM call. The system prompt is kept separate from user and retrieved text, which go in as clearly marked data, never as instructions.
  5. Citation renderer, which links only to the help-center domain.
  6. Trace.
- **Hard caps already on:** 4k input and 500 output tokens, one call per turn, and a small daily-spend kill-switch (a design parameter).
- **Deploy:** CI builds the image. Model ID, prompt version and index version live in versioned config and are stamped on every trace. Deploys go to production through the pipeline.
- **Logs:** Transcripts are classified confidential, access is restricted, retention is 14 days.
- **Monitoring:** One OpenTelemetry trace covers widget → API → retrieval → LLM. Metrics for latency, tokens, cost and errors. Alert on error rate and LLM failures.
- **Rollback:** Turn off the flag, or redeploy the previous image.
- **Who can reach it:** Staff only, through an SSO allow-list behind the flag. No customer traffic until V1, V2, V3 and V5 pass.
- **Kick off now, because they take time:** provider data agreement, privacy review, labeling the question set, and baselines from support ops and finance.

Exit check (V0): see §6.

## 5. Phases

- **Phase 1 — Defenses and budgets**
  - **Unlocks:** Building capability on a system that is guarded and metered.
  - **Depends on:** V0.
  - **Tasks:**
    1. Indexing filter: public and published articles only, with visibility rechecked when answering.
    2. Personal-data removal in logs and analytics.
    3. Output renderer: help-center link allow-list, no images, no other URLs.
    4. Injection test suite (in user messages and planted in a test article) running in CI.
    5. Jailbreak and abuse filters on input and output.
    6. Token, turn and daily-spend caps, plus per-IP and per-session rate limits.
    7. Timeouts with a fallback that shows relevant articles.
    8. Handoff tool: the system that executes it only creates a ticket after a user click.
    9. AI-disclosure banner.
  - **Rollback:** Revert config and image. Turn the flag off.
  - **Exit check:** V1, V2, and the caps part of V3 pass.

- **Phase 2 — Retrieval**
  - **Unlocks:** Answers grounded in the whole corpus.
  - **Depends on:** Phase 1, and the labeled question set.
  - **Tasks:**
    1. Full ingest pipeline: CMS webhooks, nightly reconcile, priority unpublish path.
    2. Heading-aware chunking.
    3. Hybrid search (keyword BM25 plus embeddings), with a reranker only if V4 needs it.
    4. Versioned index with blue/green alias swap.
    5. Measure existing site search on the same question set, to get a BASELINE for V4.
    6. Build-vs-buy trial check (§3).
    7. Send content gaps to the docs team.
  - **Rollback:** Swap the alias back to the previous index version.
  - **Exit check:**
    - V4 passes.
    - In the publish/unpublish test, an unpublished article is gone ≤ 15 min and an edit is live ≤ 1 h.
    - An index rebuild finishes within the 1 h RTO.

- **Phase 3 — Model access, memory, orchestration**
  - **Unlocks:** An answer pipeline good enough to release.
  - **Depends on:** V4.
  - **Tasks:**
    1. Grounded prompt that requires citations.
    2. Check each claim against its cited passage after generation, and decline if it isn't supported.
    3. Fixed rule that sends policy topics to a link plus handoff.
    4. Provider interface with a pinned model snapshot, retries, and a fallback model selectable by config.
    5. Short-term chat memory: last 6 turns, personal data removed, 30-minute session lifetime.
    6. Rewrite follow-up questions so they search well.
    7. Evals run in CI on every prompt or model change.
    8. Load-test part of V3, with the final prompt size.
  - **Rollback:** Prompts and model are pinned, so revert the config.
  - **Exit check:**
    - V5 passes.
    - The V3 load test passes.
    - Switching to the fallback model is only a config change, and V5 passes on it.

- **Phase 4 — Staged exposure and feedback**
  - **Unlocks:** Real customer value.
  - **Depends on:** V1, V2, V3, V5, the signed data agreement, and handoff queues agreed with support.
  - **Tasks:**
    1. Roll out in steps: all staff → 1% of sessions in one language → 5% with a holdout group (V6 runs here) → 25% → 100%. Each step needs one clean week: no Sev-1/2 incidents, cost and SLO alerts quiet, and the sampled faithfulness score within V5.
    2. Daily automated faithfulness check on 2% of chats, plus a weekly human review.
    3. Collect feedback (§8).
  - **Rollback:** Turning off the flag brings back search and the contact form. Rollback triggers automatically on spend or faithfulness alerts. There's no point of no return.
  - **Exit check:** V6 passes before going past 5%. The SLO holds at 100% for 30 days.

## 6. Validation checks

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | Staff question end to end. Deploy and roll back through CI. Revoke the LLM key in a test to inject a failure | One trace across all tiers; deploy and rollback succeed; the alert fires and the widget falls back to search; first latency, token and cost baselines recorded (a near-zero-load floor, not capacity) | Trace ID, pipeline run IDs, alert record, baseline sheet in the repo `/ops/baselines` | Phase 1. If it fails: fix the pipeline before anything else | 0 |
| V1 | No non-public content or customer personal data crosses the agreed boundary | (a) Compare every indexed doc ID with the CMS list of public and published articles, and test that unpublishing removes an article. (b) Push seeded synthetic personal data through the logging pipeline. (c) Check the contract. (d) Privacy review | 0 non-public docs indexed (design bar); personal-data removal catches ≥ 99% of ≥ 500 seeded items (ASSUMPTION); data agreement signed with no retention and no training; named privacy lead signs off | Audit diff report, redaction test report in CI artifacts, contract ref, privacy review ticket | Customer exposure. If it fails: hold exposure and flip the privacy row | 1 |
| V2 | Injected instructions can't cause harm, even when one gets past the filters | ≥ 200 injection and jailbreak cases (public attack collections plus red-team) in user messages and in a test article, run in CI on every prompt or model change. Design review of every way data could leave | 100%: no link outside the allow-list rendered, no ticket created without a user click, no data from another session (guaranteed by design). ≥ 95% of jailbreak attempts end in a refusal or an on-topic answer (ASSUMPTION). Named security reviewer signs off | Versioned suite results in CI, design-review doc | Customer exposure. If it fails: tighten the renderer and filters, then re-run | 1 |
| V3 | The caps limit worst-case spend, and latency stays in budget at peak | Phase 1: runaway tests (huge input, endless follow-ups, scripted abuse from one IP). Phase 3: load test at 2× peak traffic for 30 min with the real model and final prompt | Every over-cap request is cut off and logged; rate limits hold; p95 time to first token ≤ 2 s and p95 full answer ≤ 10 s (ASSUMPTION); 100% of timeouts return the fallback. Peak figure is UNKNOWN until support ops supplies it | Runaway test log, load-test report in `/perf` | Phase 4. If it fails: shrink context or move to a faster model (flips the hosted-API row, or brings routing forward) | Starts in 1, required before 4 |
| V4 | Retrieval finds the right article for real customer questions | ≥ 300 real questions from search logs and ticket subjects, sampled by top contact reasons, with the expected article labeled by support staff. Plus ≥ 50 questions the docs can't answer | Right article in the top 5 for ≥ 90% of answerable questions (ASSUMPTION), and at least as good as existing site search on the same set (BASELINE). ≥ 80% of unanswerable questions score below the decline threshold (ASSUMPTION) | Eval report with index version and embedding model ID in `/evals/results` | Phase 3 detailed design. If it fails: tune chunking and search weights, or add a reranker. Missing-content failures go to the docs team. Still failing → snippets-only answers | 2 |
| V5 | Answers state only what the cited passages support, and risky or unanswerable questions decline or hand off. Guards the worst failure | Full pipeline on the V4 set, plus ≥ 100 policy questions (refunds, billing, pricing, legal, security, account deletion), plus "promise me…" attacks. An LLM judge checks each claim and is calibrated against 150 human-graded answers | ≥ 95% of answers fully supported; 0 unsupported promises in the policy set; ≥ 90% decline or handoff on unanswerable questions; judge agrees with humans ≥ 90% (all ASSUMPTION). Head of Support and legal sign off on the policy set | Graded eval report and sign-off record in `/evals/results` | Customer exposure. If it fails: failing topics go snippets-only or human-only, then re-run | 3 |
| V6 | The bot resolves enough chats at acceptable cost, without hurting CSAT or flooding agents | Canary on 5% of sessions in one language for ≥ 2 weeks and ≥ 1,000 chats (ASSUMPTION), against a holdout group. "Resolved" means no ticket within 72 h and no escalation | Cost per resolved chat ≤ target, which is UNKNOWN until support ops and finance supply cost per ticket in Phase 0. Ticket rate no higher than the holdout's, and CSAT not significantly lower (ASSUMPTION). Agents rate handoffs as usable | Experiment report and dashboard snapshot in `/experiments` | Rollout to 25% and then 100%. If it fails: stay at canary and tune, fall back to agent-assist, or flip build-vs-buy | 4 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Secrets in a vault; staff-only flag; user and retrieved text passed as data; outbound traffic only to the LLM provider | End-to-end OTel trace; token, cost and latency metrics; error alert; transcripts classified confidential | Infrastructure as code; model, prompt and index versions stamped on each trace | Timeout falls back to search; flag works as a kill switch |
| 1 | Injection suite in CI, link allow-list renderer, personal-data removal, rate limits, CSP | Guardrail-trigger and spend dashboards; daily spend alert | Guardrail config and test suite versioned | Spend kill-switch; fallback mode when the provider is down |
| 2 | Public-only indexing filter; read-only ingest token | Freshness-lag metric, ingest-failure alert, retrieval scores logged | Index versioned with embedding model and chunker; question set versioned | Nightly reconcile; alias rollback; rebuild from the CMS |
| 3 | Claim checking, policy-topic routing, no secrets in prompts | Sampled faithfulness score; eval in CI | Prompts in git; model snapshot pinned; eval run IDs recorded | Fallback model; retries with backoff; decline when unsure |
| 4 | AI disclosure; abuse monitoring; runbook for harmful output | SLO, resolution, CSAT and cost dashboards; daily sampling | Experiment assignments logged; feedback reviewed before it changes anything | Automatic rollback at each step; flag-off brings back the old path |

## 8. AI layer

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Prompt-injection / guardrail defense | Untrusted text is passed as data; link allow-list; no images or other URLs; jailbreak filters | Phase 0 (basic split), Phase 1 | V2 |
| 2. Cost + latency budget | Token, turn and daily caps; rate limits; timeout with fallback | Phase 0 (caps), Phase 1 | V3 |
| 3. Human-in-the-loop gating | The only write is a handoff ticket, created only after a user click and enforced by the code that runs it, not the model. No refunds or account actions. Feedback is reviewed by people | Phase 1 | V2 (no ticket without a click) |
| 4. Retrieval | Hybrid search over public articles; versioned index | Phase 2 | V4 |
| 5. Model access | Provider interface, pinned snapshot, fallback model by config | Phase 0 (interface), Phase 3 | Phase 3 exit check (V5 passes on the fallback model) |
| 6. Memory | Short-term only: last 6 turns, personal data removed, 30-minute lifetime. Not needed long-term (§10) | Phase 3 | Follow-up questions keep context in the V5 run |
| 7. Orchestration | One chain: rewrite follow-up → search → generate with citations → check claims | Phase 3 | V5 |
| 8. Routing | Model routing isn't needed (one model). The fixed policy-topic rule is part of V5 | — | — |
| 9. Feedback | Thumbs, handoff reasons and unanswered questions go to a weekly triage. Results become docs-team fixes and new eval cases, after review, never applied automatically | Phase 4 | Next eval run improves the target metric with no regression |

## 9. Exceptions
None.

## 10. Deferred
- **Account-aware answers (order status, plan details):** when V6 passes and product asks for it. It needs login, per-user access checks, and a new privacy decision that is costly to reverse.
- **Actions such as refunds or plan changes:** only with a human-approval step bound to the exact action, after account-aware answers exist.
- **Indexing forum or community content:** when V2 covers user-written content and V1 covers who wrote it.
- **More languages:** when V6 passes in the first language. Each language needs its own V4/V5 question set.
- **Model routing (small model / large model):** when cost per resolution misses the V6 target while a cheaper model still passes V5.
- **Fine-tuning:** when V5 fails for reasons prompting and retrieval can't fix. Never on transcripts without a new privacy decision.
- **Long-term memory across sessions:** when repeat-contact data shows users lose context between sessions.
- **Agent-assist mode (drafts for human agents):** brought forward if V5 or V6 fail. It's the lower-risk fallback.
- **Voice or other channels:** after the SLO holds at 100% web rollout.
