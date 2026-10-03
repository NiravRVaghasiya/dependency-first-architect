# BUILD PLAN — output format

Emit the plan in exactly these sections, in this order. Keep it concrete; name real
technologies and real checks, not placeholders.

---

## 1. Classification
- **What:** one line on the system.
- **Type:** software / infra / AI-agentic (or mix).
- **Dominant constraint:** correctness | latency | cost | scale | compliance.
- **Worst failure:** the single failure that would hurt most.

## 2. Tradeoff gates (resolved up front)
A table of irreversible decisions. Each row:

| Decision | Default (chosen now) | Flip condition |
|---|---|---|
| Consistency vs availability | … | … |
| Monolith vs services | … | … |
| Sync vs async | … | … |
| Build vs buy | … | … |
| (AI) prompt+RAG vs fine-tune | … | … |
| (AI) hosted API vs self-host | … | … |

No silent assumptions. If a gate doesn't apply, write "N/A — <reason>".

## 3. Walking skeleton (Phase 0)
The thinnest end-to-end path that runs in production:
- The one real request and the real response.
- Every tier it crosses.
- How it is deployed, logged, and monitored on day zero.
Exit check: a real request returns a real response in prod, with a trace and a metric visible.

## 4. Phases (ordered by dependency; within a phase, widest blast radius first)
For each phase:
- **Phase N — <name>**
  - Unlocks: what becomes possible after it.
  - Depends on: the proven thing it builds on.
  - Tasks: ordered widest-blast-radius → narrowest.
  - Exit check: the observable condition that proves the phase is done.

## 5. Cross-cutting concerns (per phase, from the first commit)
A table with one row per phase and columns for the concrete move made in each:

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | … | … | … | … |

Observability appears in Phase 0. None of these columns may be empty.

## 6. AI layer (AI/agentic systems only)
Defenses/budgets first, then capability order:
1. Prompt-injection / guardrail defense
2. Cost + latency budget
3. Human-in-the-loop gating
4. Retrieval
5. Model access
6. Memory
7. Orchestration
8. Routing
9. Feedback

For each: what it is in THIS system, and its exit check. Omit this section for non-AI systems.

## 7. Deliberately deferred
Bullet list: what you are NOT building yet, and the condition that pulls each item forward.

## 8. Eval score
Report the self-score against `reference/evals.md` as **/20**, with a one-line note per
rubric dimension. If below 16, revise above and re-score before delivering.
