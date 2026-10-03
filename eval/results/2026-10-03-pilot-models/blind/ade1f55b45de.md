# Customer-Support RAG Chatbot Plan

I don't know your stack, doc volume, or constraints yet, so this plan rests on stated assumptions. The questions at the end are the ones that would change it most.

## 1. Goals and success metrics
Define these before building anything:
- **Deflection rate:** the share of conversations resolved without a human.
- **Answer accuracy and groundedness:** measured on a labeled eval set.
- **Escalation quality:** handoffs to a human that include the conversation context.
- **Guardrail targets:** for example, under 2% unsupported claims and 0 policy or pricing hallucinations.
- **Latency:** p50 under 3s to first token, with streaming.

## 2. Architecture

```
Help-center source (Zendesk/Intercom/HTML/MD/PDF)
   → Ingestion & cleaning → Chunking + metadata → Embeddings → Vector index (+ BM25)
                                                                     ↓
User → Chat UI/API → Query rewrite → Hybrid retrieval → Rerank → LLM (grounded prompt) → Answer + citations
                                                                     ↓
                                              Escalation to human / ticket creation
                                                                     ↓
                                              Logging, feedback, analytics, evals
```

## 3. Phases

**Phase 0: Discovery (about 1 week)**
- Inventory the docs: source system, count, languages, update frequency, and whether any are gated or private.
- Pull 200–500 real past tickets or chat questions. They become your eval set and show where the docs have gaps.
- Decide which topics the bot must never answer alone, such as billing disputes, legal, security incidents, and account deletion.

**Phase 1: Ingestion pipeline**
- Use the help-center API or a crawler for sync. Run incremental updates by webhook or nightly, and handle deletions so stale articles don't linger.
- Clean the content by stripping nav and boilerplate and keeping headings, lists, tables, and code blocks.
- Chunk by heading structure, at roughly 300–600 tokens, with the article title and section path prepended to each chunk. Keep step-by-step procedures intact.
- Store this metadata: URL, title, section, product/plan/version, locale, last-updated date, and audience (admin or end user). You'll use it for filtering and citations.

**Phase 2: Retrieval**
- Start with hybrid search (dense embeddings plus BM25), because support queries often contain exact strings like error codes and feature names.
- Add a cross-encoder reranker over the top 20–50 results, keeping about 5.
- Rewrite follow-up questions into standalone queries using the conversation history.
- Filter by metadata such as product, plan, and language where the user context is known.
- Stack options: pgvector, Qdrant, or OpenSearch for the index. Take the embedding model from a quick bake-off on your own eval set rather than from a leaderboard.

**Phase 3: Generation**
- The system prompt should:
  - answer only from the retrieved context
  - cite sources, with article links
  - say "I'm not sure" when context is insufficient
  - ask a clarifying question when the query is ambiguous
  - match the brand tone
- Use structured output: `{answer, citations[], confidence, needs_handoff}`.
- Add a refusal/handoff path for low retrieval scores, out-of-scope topics, frustrated users, or an explicit request for a human.
- Add a lightweight post-check that each claim is supported by the cited chunks.

**Phase 4: Product integration**
- Embed the chat widget in the help center and app, and optionally in Slack or email.
- Handoff should create a ticket in your helpdesk with the transcript, retrieved articles, and a summary.
- Personalization, such as the user's plan or account status, should come through authenticated tool calls. Don't put it in the vector store.
- Add thumbs up/down and a "didn't answer my question" button.

**Phase 5: Evaluation and launch**
- **Offline:** run the eval set and score retrieval (recall@k, MRR) and answers (faithfulness, correctness, completeness), using LLM-as-judge calibrated against human spot-checks. Run it in CI on every change to prompts, chunking, or models.
- **Red-team:** try prompt injection (including instructions hidden in docs), off-topic requests, PII extraction, and competitor or pricing bait.
- **Rollout:** internal dogfooding, then shadow mode (the bot drafts answers for agents), then 5–10% of traffic, then full launch.

**Phase 6: Operations**
- Dashboards: deflection, CSAT, escalation reasons, no-answer queries, cost per conversation, and latency.
- Send the top unanswered questions to the docs team as content-gap reports. This is often the most valuable output.
- Re-index on doc changes and re-run evals when you change models.

## 4. Key risks and mitigations
| Risk | Mitigation |
|---|---|
| Hallucinated policies or steps | Strict grounding, citations, a verification pass, and handoff on low confidence |
| Stale or conflicting docs | Sync with deletions, "last updated" metadata, and a dedupe/conflict audit |
| Prompt injection via content or user | Treat retrieved text as data, restrict tools, and filter outputs |
| PII leakage | Redact logs, apply retention limits, and give the LLM no access to other users' data |
| Cost and latency | Cache frequent queries, use a smaller model for rewriting, and stream responses |
| Multi-language | Use a multilingual embedding model, or translate at query time |

## 5. Rough team and timeline
A small team (1–2 engineers, 1 support SME, part-time PM) can ship an MVP in **4–6 weeks**. That covers ingestion, hybrid retrieval, grounded answers with citations, handoff, and an eval harness. Production hardening and analytics take another 4–6 weeks.

## 6. Questions that would sharpen this plan
1. Where do the docs live (Zendesk, Intercom, Confluence, Markdown in Git), and about how many articles are there?
2. Which helpdesk and chat channels must it integrate with?
3. Are there languages, compliance needs (SOC 2, GDPR, HIPAA), or data-residency limits that restrict which LLM providers you can use?
4. Does the bot need account-specific answers, such as order status or plan details, or only general documentation answers?
5. Do you have a target deflection rate or budget per conversation?

If you tell me the stack and the answers to those questions, I can turn this into a detailed technical design. I can also draft the system prompt, the eval set format, or the ingestion code. If the docs are in this working directory, give me the path and I'll look at their structure to inform the chunking strategy.
