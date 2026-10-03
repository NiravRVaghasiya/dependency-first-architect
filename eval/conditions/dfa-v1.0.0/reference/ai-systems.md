# AI / agentic layer — detail

Load this when Step 1 classifies the system as AI or agentic. The order below is deliberate:
defenses and budgets are built BEFORE capabilities, because an unguarded or unmetered agent
is a liability the moment it touches real input or real money.

## Defenses and budgets (build first)

### 1. Prompt-injection / guardrail defense
Untrusted input (user text, retrieved documents, tool output) can carry instructions. Treat
all of it as data, never as commands. Concrete moves:
- Separate system/trusted instructions from untrusted content at the API boundary.
- Input and output filters (PII, jailbreak patterns, disallowed content).
- Constrain tool calls to an allow-list; validate arguments before execution.
- Exit check: a known injection string in a retrieved doc does not change agent behavior.

### 2. Cost + latency budget
An agent loop can spend unbounded tokens and time. Set budgets before capability:
- Per-request token cap, per-session cost cap, max tool-call / reasoning iterations.
- A latency budget with a timeout and a graceful degraded response.
- Exit check: a runaway loop is cut off at the cap and emits a cost/latency metric.

### 3. Human-in-the-loop gating
Decide which actions require a human before they execute (writes, spend, external messages,
irreversible operations). Low-risk reads run autonomously; high-blast-radius actions gate.
- Exit check: a gated action pauses for approval and is auditable.

## Capability order (build after defenses)

### 4. Retrieval
Get the right context in. Chunking, embeddings, index, and relevance evaluation.
- Exit check: for a known query, the expected source passages are retrieved.

### 5. Model access
A thin, swappable interface to the model(s): provider abstraction, retries, fallback model,
structured-output parsing.
- Exit check: the model can be swapped via config without touching call sites.

### 6. Memory
What persists across turns/sessions: short-term conversation state and long-term store, with
an explicit write policy and a privacy boundary.
- Exit check: relevant prior context is recalled; stale/forbidden data is not written.

### 7. Orchestration
How steps compose: single call, chain, or multi-agent. Keep it the simplest shape that works.
- Exit check: a multi-step task completes end-to-end with each step traced.

### 8. Routing
Send each request to the right model/tool/path by difficulty, cost, or capability.
- Exit check: cheap requests take the cheap path; hard ones escalate, both logged.

### 9. Feedback
Capture outcomes (thumbs, corrections, eval scores) and route them back into prompts,
retrieval tuning, or training data.
- Exit check: a captured correction measurably changes a later response or an eval metric.

## Notes
- Every sublayer still obeys the cross-cutting columns (security, observability,
  reproducibility, resilience). Observability here means per-step traces, token/cost metrics,
  and eval scores — from day zero.
- Prefer prompt+RAG over fine-tuning until a Tradeoff gate's flip condition is met; it is the
  less rigid, more reversible choice (SKILL principle 2).
