# AI / agentic layer — detail

Load this when Step 1 classifies the system as AI or agentic. The order below is deliberate:
defenses and budgets are built BEFORE capabilities, because an unguarded or unmetered agent
is a liability the moment it touches real input or real money. Even the walking skeleton already
treats user and retrieved text as data and has a hard token and cost cap; the full defenses come
right after it, before any external exposure.

An exit check below becomes a validation gate (`SKILL.md`) only when later work depends on the
hypothesis it tests — typically injection containment, retrieval quality, and cost per resolved
conversation. The rest stay one-line exit checks. Model quality, cost, and latency thresholds
rarely follow from the request alone: label them ASSUMPTION (with a basis) or UNKNOWN (no number).
A capability sublayer (4–9) the system does not need is one line: "Not needed — <reason>".

## Defenses and budgets (build first)

### 1. Prompt-injection / guardrail defense
Untrusted input (user text, retrieved documents, tool output) can carry instructions. Treat
all of it as data, never as commands. No filter stops every injection, so design for bounded
impact when one succeeds. Concrete moves:
- Separate system/trusted instructions from untrusted content at the API boundary.
- Input and output filters (PII, jailbreak patterns, disallowed content).
- Constrain tool calls to an allow-list; validate arguments before execution.
- Close exfiltration paths: no arbitrary URLs, images, or outbound tools fed by model output.
- Exit check: an injection suite (known strings plus red-team cases, planted in user input,
  retrieved docs, and tool output) runs in CI with its pass rate tracked; and in every case,
  whether or not the injection succeeds, no tool call leaves the allow-list, no write runs
  unapproved, and no data leaves through URLs, images, or outbound tools. The second half holds by
  design, not by filtering.

### 2. Cost + latency budget
An agent loop can spend unbounded tokens and time. Set budgets before capability:
- Per-request token cap, per-session cost cap, max tool-call / reasoning iterations.
- A latency budget with a timeout and a graceful degraded response.
- Record the targets in the plan's Budgets table with their labels. Budget per resolved
  conversation or completed task; meter per request. Cost per resolved conversation is an
  economic dependency: shadow traffic shows only cost per turn, so validate it on a canary.
- Exit check: a runaway loop is cut off at the cap and emits a cost/latency metric.

### 3. Human-in-the-loop gating
Decide which actions require a human before they execute (writes, spend, external messages,
irreversible operations). Low-risk reads run autonomously, unless the agent also has an outbound
channel: reading private data plus any way out is a leak. High-blast-radius actions gate.
- Exit check: a gated action cannot run without an approval bound to that exact action,
  enforced by the tool executor (not the model), and logged.

## Capability order (build after defenses)

### 4. Retrieval
Get the right context in. Chunking, embeddings, index, and relevance evaluation. Pin the
embedding model in the index version: switching it means re-embedding the corpus (R2). For a
permissioned corpus, enforce the caller's access at retrieval (a risk/security dependency).
- Exit check: on a labeled set of real queries, the expected source passages are retrieved at
  the stated hit-rate threshold, and no query returns a passage the caller cannot read.

### 5. Model access
A thin, swappable interface to the model(s): provider abstraction, retries, fallback model,
structured-output parsing. The call seam exists from Phase 0; this layer hardens it.
- Exit check: switching to the fallback model is a config change, and the eval suite passes on it
  at the stated threshold.

### 6. Memory
What persists across turns/sessions: short-term conversation state and long-term store, with
an explicit write policy and a privacy boundary.
- Exit check: relevant prior context is recalled; stale/forbidden data is not written.

### 7. Orchestration
How steps compose: single call, chain, or multi-agent. Keep it the simplest shape that works.
- Exit check: on a labeled task set, the stated share of multi-step tasks completes with each step
  traced; failed steps retry or surface, never vanish.

### 8. Routing
Send each request to the right model/tool/path by difficulty, cost, or capability.
- Exit check: on the eval set, routed quality is within the stated margin of the strongest
  model's, at lower cost, with both paths logged.

### 9. Feedback
Capture outcomes (thumbs, corrections, eval scores) and route them back into prompts,
retrieval tuning, or training data.
- Exit check: corrections enter through review, and the next eval run improves the target metric
  with no regression elsewhere. (Change alone is not improvement; unreviewed feedback is a
  poisoning path.)

## Notes
- Every sublayer still obeys the cross-cutting columns (security, observability,
  reproducibility, resilience). Observability here means per-step traces, token/cost metrics,
  and eval scores — from day zero.
- Prefer prompt+RAG over fine-tuning until a Tradeoff gate's flip condition is met; it is the
  less rigid, more reversible choice (SKILL principle 2). Fine-tuning a model is usually R2;
  training on customer or personal data is R3.
