> **P1** · **without the skill** (cold: plain `claude --bare -p`, no skill, empty directory)  
> Prompt: *"Plan a customer-support RAG chatbot over our help-center docs."*  
> Generated 2026-10-02 with `claude-opus-5-5` (effort max) in an isolated session. One run per arm, not cherry-picked.  
> Everything below the line is the model's output, verbatim. Judged score: [SCORECARD.md](../SCORECARD.md).

---

# Customer-support RAG chatbot over help-center docs: plan

I couldn't find an existing project in the workspace: no README, package or Python dependency files, `docs/`, `src/`, `data/` or `.git`. So this plan uses common defaults. The questions at the end are the ones whose answers would change it.

**Assumptions:**
- 100–5,000 articles on a help-center platform with an API (Zendesk, Intercom, Freshdesk…) or Markdown files in git.
- One main language.
- A chat widget on your website.
- An existing ticketing or live-chat tool to hand conversations off to.
- Version 1 answers questions from the docs only. It can't look up or change customer accounts.

## 1. Goal and success metrics

The bot answers how-to, troubleshooting and policy questions **only from your docs**, with links to the articles it used. When the docs don't have the answer, it hands the customer to a human smoothly.

| Metric | Starting target | How to measure |
|---|---|---|
| Deflection (tickets avoided) | Better than a control group | A/B test: tickets filed within 72h by users who saw the bot vs. users who didn't |
| Correctness | ≥ 90% | A test set of real questions, scored by an LLM grader that has been checked against human labels |
| Grounded in the docs | ≥ 95% of claims supported | The grader checks each claim against the articles that were retrieved |
| Correctly says "not covered" and hands off | ≥ 90% | Test-set questions the docs can't answer |
| Customer satisfaction (CSAT) | Not below current level | Survey in the widget |
| Latency | First words in < 2s typically, < 4s for slow cases | Request logs |

A conversation that ended without escalation hasn't necessarily been resolved. Someone who gives up and emails you looks the same in the logs. Only the A/B control group measures real deflection.

## 2. Before building (week 0)

- **Coverage audit:** Label about 200 recent tickets as *answerable from current docs*, *needs account data or an action*, or *missing from the docs*. This sets your upper limit: if only 35% are answerable from docs, the bot can't deflect more than 35%. The audit also gives you a first list of missing docs and the start of your test set.
- **Build or buy:** The AI agents built into helpdesk platforms, and specialist vendors, can be live in days and are often priced per resolved conversation. Building makes sense if you need control over the model, the data or cost at high volume, product-specific logic (plans, versions, platforms), or your own UI. The rest of this plan assumes you build. Sections 5 and 7 apply either way.

## 3. Architecture

```
INGESTION (async)
help-center API ─► sync ─► HTML→Markdown ─► section chunks ─► embed ─► Postgres
 (webhooks + nightly reconcile, incl. deletes)     (pgvector + full-text)

QUERY (every message)
message + context (page, locale, plan, platform)
 ├─► router (small model, runs in parallel)
 │     human request / sensitive topic ─────────────────────► HANDOFF
 ▼
rewrite into standalone query (skip on first turn)
 ▼
hybrid search: keyword + vector ─► RRF ─► top 50   (filter: locale, visibility)
 ▼
rerank ─► top 5–8 sections ─► expand to parent articles
 ▼
answerability gate ── weak ─► ask once to clarify, or "not covered" + human
 ▼
generate (mid-tier LLM, streamed, cites doc ids)
 ▼
checks: citations in retrieved set · no commitments
 ▼
answer + source links + feedback + "Talk to a human"
```

**Ingestion (loading the docs)**
- **Use the platform's API, not a web scraper.** For each article, store its ID, URL, section anchors, category, labels, locale, last-updated time and visibility (who is allowed to see it). Index only public articles, or store the permissions and filter on them. Leaking internal-only articles is the most common failure here.
- **Sync on publish events plus a nightly full check.** Sync whenever an article is published (if the platform sends webhooks). The nightly check also **removes** archived or unpublished articles. Old articles left in the index lead to confident wrong answers.
- **Clean the HTML carefully.** Keep headings, numbered steps, tables and image alt text. Remove navigation, "related articles" and feedback widgets.
- **Later (v2):** have a vision model describe key screenshots, since many steps only appear in images.
- **Flag near-duplicate or conflicting articles** to the docs team.
- **Leave community-forum posts out of v1.** Nobody has checked them, and users can plant instructions in them that the model might follow (prompt injection).

**Chunking: search small pieces, give the model whole articles**
- Split articles at H2/H3 headings into chunks of about 200–800 tokens. Never split a numbered procedure or a table.
- Start each chunk with `Article title > Section` (plus product or plan) before indexing it. A chunk that only says "Click **Save**" can't be found on its own.
- Search over sections, but give the model the **whole article** when it fits a budget of about 8k tokens (most help articles are under 2k). Otherwise give the section plus the sections around it. That way procedures don't lose steps.

**Retrieval (finding the right articles)**
- **Use both keyword and vector search.** Keyword search catches error codes, button labels and feature names. Vector (meaning-based) search catches rephrasings ("can't get in" → "reset password"). Merge the two result lists with reciprocal rank fusion (RRF), then reorder the top results with a reranking model.
- **Tune the "weak match" cutoff on the test set**, not by gut feel.
- **Locale is a hard filter**, falling back to the default language if an article isn't translated.
- **Plan and platform raise an article's rank** rather than filtering. The bot also states requirements, e.g. "Requires the Business plan."
- **You don't need a separate vector database.** A help center is only thousands of chunks, so Postgres with the pgvector extension is enough.
- **If retrieval misses**, try having an LLM add a short context line to each chunk ("contextual retrieval") before anything more complex.

**Generation (writing the answer)**
- **Use a fixed pipeline in v1:** always retrieve, then answer. It's predictable, cheap and easy to test. Only let the model run its own searches ("agentic" retrieval) if tests show multi-part questions failing.
- **Use two model sizes.** A mid-size model writes answers. A small, fast model handles routing and query rewriting. Stream the answer as it's written, and use the provider's prompt caching for the fixed system prompt.
- **Validate citations.** Each document goes into the prompt with an ID, and the model cites IDs like `[doc:123#section]`. As the answer streams, the server turns citations into links and removes any ID it didn't actually retrieve.
- **Check for commitments last.** After the answer is complete, check it for promises (refunds, credits, dates). If it fails, replace it with a safe fallback message and offer a human.

**Handoff to a human**
- Hand off **immediately** when the user asks or the topic is sensitive (security incident, legal threat, safety). Never make users fight the bot.
- For account questions ("why was I charged twice?"), give the relevant policy from the docs, then offer a human. The bot can't see accounts in v1.
- Offer a human after two unhelpful answers in a row, repeated rephrasing, or visible frustration.
- The ticket includes the transcript, a two-line summary, what the user wanted, the articles shown and the user's plan, so customers never have to repeat themselves.
- Respect business hours: live chat when agents are available, otherwise a ticket. Promised reply times come from your settings, never from the model.

## 4. Behavior rules (core of the system prompt)

1. Answer only from the provided docs. If they don't cover the question, say so plainly and offer a human. Never fill gaps from general knowledge, especially prices, limits, timelines or policies.
2. Never make commitments: refunds, credits, exceptions or dates. (In *Moffatt v. Air Canada* (2024), a tribunal held the airline liable for a refund policy its chatbot made up.)
3. State plan, platform or version requirements whenever the docs do.
4. If a question is ambiguous, ask **one** clarifying question. Otherwise answer and state the assumption.
5. Format: a direct answer, then numbered steps using the exact button and menu names, then sources. Keep it chat-length, not article-length.
6. Reply in the user's language, politely decline off-topic requests, and say clearly that it's an AI.

Keep prompts in version control. For every conversation turn, log the rewritten query, the retrieved IDs and scores, the prompt, index and model versions, token counts, latency and user feedback.

## 5. Testing: start in week 1, before the bot exists

Build a test set of 200–300 cases from real tickets, with personal data removed:

| Type of question | Share | Passes if the bot… |
|---|---|---|
| Answerable how-to or troubleshooting | ~50% | Answers correctly and cites the right article |
| Not covered by the docs | ~15% | Says it's not covered and offers a human |
| Account-specific or needs an action | ~10% | Gives general guidance, then hands off |
| Ambiguous | ~10% | Asks one good clarifying question |
| Follow-up in a longer conversation | ~10% | Understands the follow-up and answers correctly |
| Adversarial (prompt injection, pressure for refunds, off-topic) | ~5% | Makes no commitments and stays on topic |

- **Score retrieval separately.** Check whether the right article appears in the top 5 results. Most "the AI got it wrong" bugs are actually retrieval bugs.
- **Score the answers** on correctness, grounding in the docs, accurate citations, missed handoffs and unnecessary handoffs. Before trusting an LLM grader, check its scores against about 50 cases labeled by people.
- **Run the tests automatically** on every change to prompts, models, chunking or embeddings, and block changes that make scores worse. Pick one tool, for example Ragas, DeepEval, promptfoo, or the datasets feature in Langfuse, Braintrust or LangSmith.
- **After launch:** track thumbs up/down, "Did this solve it?", handoffs after an answer, and tickets filed within 72 hours. Each week, review about 50 random conversations plus every thumbs-down, and add the failures to the test set.
- **Missing-docs report:** each week, group the questions the bot couldn't answer and send them to the docs team. This is often the most valuable thing the project produces.

## 6. Security and privacy

- Mask emails, phone numbers and card numbers before anything is logged, and never store card numbers. Set how long transcripts are kept (e.g. 30–90 days).
- Get a data processing agreement (DPA) from the LLM vendor that guarantees your data isn't stored or used for training. Add data-residency terms if you need them.
- v1 can't take actions, so prompt injection mostly means tricking it into promising something or saying something embarrassing. Rules 1–2 and the final check cover that. Keep secrets out of prompts.
- Rate-limit per session and IP address, cap conversation length, and cap daily spending.
- Some jurisdictions legally require telling users they're talking to an AI. Check with your legal team.

## 7. Rollout

| Phase | Time | Done when |
|---|---|---|
| 0. Coverage audit, test set, choose the stack | 1–2 wk | Upper limit known, ≥ 200 test cases |
| 1. Doc loading, search, test harness | 2 wk | Right article in the top 5 for ≥ 90% of answerable questions |
| 2. Answer generation, safeguards, handoff, logging | 2–3 wk | Test targets met, security review passed |
| 3. Agent assist: the bot drafts replies for your support agents inside the helpdesk | 2 wk | Agents find most drafts usable with light edits |
| 4. Customer beta: 5–10% of traffic or one product area, with a control group | 2–4 wk | Fewer tickets, CSAT at least as good as the control group |
| 5. Full launch, plus the weekly review | ongoing | — |

That's about 2 months to a customer beta with 1–2 engineers, plus a part-time support-operations owner and a docs writer. Phase 3 is the cheapest way to reduce risk: the bot sees real questions, customers never see its answers, and every agent edit becomes a labeled test case.

## 8. Default tech stack (swap freely)

| Part | Default | Alternatives |
|---|---|---|
| Storage and search | Postgres + pgvector + built-in full-text search | OpenSearch/Elasticsearch, Qdrant, Pinecone |
| Embeddings | Hosted API (OpenAI, Voyage, Cohere) | bge-m3, self-hosted (multilingual) |
| Reranker | Cohere or Voyage rerank | bge-reranker, self-hosted |
| LLM | Mid-size model for answers, small model for routing and rewriting | Pick by running the test set on 2–3 candidates |
| Orchestration | Your own thin code (easiest to debug) | LlamaIndex, LangGraph, Haystack |
| API and UI | FastAPI or Node with streaming responses, embeddable widget | Your helpdesk's messenger SDK |
| Logging and tracing | Langfuse (open source) | LangSmith, Braintrust, Arize Phoenix |

**Cost:** each message uses about 5–10k input tokens and about 300 output tokens. That's a few cents per conversation with a mid-size model, and less with prompt caching. That's much cheaper than having a person handle the ticket.

## Questions that would change the plan
1. **Docs platform:** Zendesk, Intercom, Freshdesk, Confluence, Markdown in git? This decides how you connect, sync and handle permissions.
2. **Size:** how many articles, in how many languages?
3. **Placement:** help center, inside your product, or an existing chat messenger? Are users logged in? That decides whether answers can depend on the user's plan.
4. **Handoff:** which ticketing or live-chat tool, and what hours are agents available?
5. **Scope:** docs-only, or will you soon need account lookups or actions (order status, refunds)? Those mean the bot calling your systems, user authentication, and much more security work.
6. **Constraints:** cloud and programming language, approved LLM vendors, data residency, expected volume. And is buying still an option?

Once you've answered these, I can draft the connector for your platform, the chunker, the system prompt and the test-set template. If you put a sample export of a few articles in the workspace, I'll fit the cleaning and chunking rules to your actual HTML. I can only read files here, so I'll post the code in chat for you to save.
