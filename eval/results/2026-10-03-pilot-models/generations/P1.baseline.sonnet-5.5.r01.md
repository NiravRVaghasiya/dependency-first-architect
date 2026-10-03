# Customer-Support RAG Chatbot Plan

I don't know your stack, doc volume, or constraints yet, so this plan uses stated assumptions. The open questions at the end are the ones most likely to change it.

## 1. Goals and success criteria
- **Primary goal:** deflect routine tickets with accurate, cited answers.
- **Metrics to set targets for:**
  - Deflection or resolution rate
  - Answer groundedness (no unsupported claims)
  - Retrieval hit rate@k
  - Escalation rate and CSAT
  - p95 latency (aim for under 5s to the first token)
  - Cost per conversation
- **Non-goals for v1:** taking account actions (refunds, password resets) and handling multi-turn troubleshooting flows. Add these in a later phase.

## 2. Architecture

```
Help-center source → Ingest/Clean → Chunk → Embed → Vector + keyword index
                                                         ↓
User → Guardrails → Query rewrite → Hybrid retrieve → Rerank → LLM (grounded) → Answer + citations
                                                         ↓
                                   Low confidence / sensitive → Human handoff
```

## 3. Ingestion
- **Source:** use the help-center API or export (Zendesk, Intercom, Confluence, or HTML crawl). Avoid scraping if an API exists.
- **Cleaning:** strip nav, footers, and boilerplate. Keep headings, lists, tables, and code blocks.
- **Metadata per chunk:** article URL, title, section heading path, last-updated date, product or plan, locale, and audience (admin or end user).
- **Freshness:** run incremental sync (webhook or nightly). Re-embed only changed articles and delete removed ones so stale answers don't persist.

## 4. Chunking
- Split by heading structure first, then by size (~300–500 tokens, 10–15% overlap).
- Prepend the article title and heading path to each chunk so it carries context.
- Keep procedures (step lists) intact. Don't split a numbered list across chunks.

## 5. Retrieval
- **Hybrid search:** combine dense embeddings with BM25. Support content has many exact terms like error codes, feature names, and SKUs.
- **Query rewriting:** condense the chat history into a standalone query.
- **Pipeline:** retrieve the top ~20–30, rerank with a cross-encoder, and pass the top 4–6 to the LLM.
- **Metadata filters:** apply locale, plan, and product filters when they're known.

## 6. Generation
- **Prompt rules:**
  - Answer only from the provided context.
  - Say "I don't know" and offer handoff when the context is insufficient.
  - Cite sources with links.
  - Keep a consistent tone.
  - Ask one clarifying question when the query is ambiguous.
- **Output:** a short answer, steps if relevant, and a "Sources" list.
- **Model choice:** start with a mid-tier model and measure. Move to a larger one only if evals show a gap.

## 7. Guardrails and escalation
- **Prompt injection:** treat retrieved text and user input as untrusted data, not instructions.
- **PII:** redact PII in logs.
- **Out-of-scope topics:** handle legal, medical, billing disputes, and security incidents with a canned response or routing.
- **Handoff triggers:**
  - Low retrieval score
  - User frustration or an explicit request for a human
  - Repeated failed turns
  - Sensitive categories
- **Handoff payload:** pass the transcript and retrieved articles to the agent so the customer doesn't have to repeat themselves.

## 8. Evaluation (build before launch)
- **Golden set:** 150–300 real questions from past tickets and search logs, with reference answers and expected source articles. Include unanswerable and adversarial cases.
- **Retrieval metrics:** recall@k and MRR.
- **Generation metrics:** faithfulness, correctness, and citation accuracy, using an LLM judge calibrated against human review of a sample.
- **CI regression:** run the eval on every change to chunking, prompts, models, or indexes.
- **Online metrics:** thumbs up/down, escalation rate, and repeat contact within 7 days.

## 9. Stack options
| Layer | Options |
|---|---|
| Vector store | pgvector (if you already use Postgres), Qdrant, Pinecone, OpenSearch (gives hybrid search in one place) |
| Embeddings | Any current hosted embedding model. Choose on multilingual needs and cost. |
| Reranker | Cohere Rerank or an open cross-encoder (e.g., bge-reranker) |
| Orchestration | Plain code or a thin framework. Avoid heavy abstractions for v1. |
| Observability | Trace every step (query, chunks, scores, answer), e.g. Langfuse or LangSmith |

## 10. Phased rollout
1. **Weeks 1–2:** Ingest the docs, build a baseline (chunk, embed, retrieve, answer), and assemble the golden set.
2. **Weeks 3–4:** Add hybrid search, reranking, and query rewriting. Tune against evals and add guardrails.
3. **Weeks 5–6:** Internal pilot with support agents as a copilot. Collect feedback and fix doc gaps.
4. **Weeks 7–8:** Limited customer launch (10–20% of traffic) with handoff. Monitor, then ramp up.
5. **Later:** Add authenticated, account-aware answers and tool actions. Build a content-gap feedback loop that reports unanswered questions to the docs team.

## 11. Key risks
- **Hallucination or wrong answers:** mitigate with strict grounding, citations, abstention, and evals.
- **Stale content:** mitigate with incremental sync and showing last-updated dates.
- **Poor docs:** retrieval can't fix missing or contradictory articles. Budget time for doc cleanup.
- **Cost and latency:** mitigate with caching, a smaller model for rewriting, and limiting context size.

## Open questions
1. Where do the docs live (Zendesk, Intercom, Confluence, custom site), and roughly how many articles?
2. Which channels does it serve (web widget, in-app, Slack, email drafts)? Is it customer-facing or an agent copilot first?
3. Do you need multiple languages?
4. Are there compliance constraints (data residency, PII, approved LLM vendors)?
5. Should answers depend on who the user is (plan, account state), or only on public docs?
6. Which helpdesk handles escalation?
7. What's your budget and latency target?

If you answer these, I can turn this into a concrete tech design with a schema, prompt templates, and an eval harness. If the docs are in a local folder, send me the path and I can look at their structure to refine the chunking strategy.
