# Eval harness — rubric, test prompts, protocol

Measures whether a produced BUILD PLAN actually follows the skill. Score is **/20**:
10 dimensions, 0–2 points each.

## Rubric (0–2 per dimension)

Scoring per dimension: **0** = absent, **1** = present but weak/partial, **2** = done well.

1. **Dependency ordering** — phases go substrate → upward; nothing specified before its
   dependency is proven.
2. **Walking skeleton first** — a real end-to-end prod path is Phase 0, before features.
3. **Tradeoff gates up front** — irreversible decisions listed with default + flip condition,
   no silent assumptions.
4. **Blast-radius ordering within phases** — widest-impact work precedes leaf work.
5. **Security threaded every phase** — not a trailing step.
6. **Observability from day zero** — present in Phase 0 and every phase after.
7. **Reproducibility threaded** — builds/data/envs are reproducible across phases.
8. **Resilience threaded** — failure handling appears across phases, not only at the end.
9. **AI layer correctness** (AI systems) — defenses+budgets+HITL before capabilities, then
   retrieval → model access → memory → orchestration → routing → feedback.
   *(Non-AI systems: award 2 if the plan correctly declares the AI layer N/A.)*
10. **Deferred work made explicit** — what's not built yet, with a pull-forward condition.

**Thresholds:** >=16 ship. 12–15 revise the weak dimensions. <12 re-plan from Step 1.

## Fixed test prompts

Run the skill against each; score with the rubric.

- **P1 (AI):** "Plan a customer-support RAG chatbot over our help-center docs."
- **P2 (software):** "Architect a multi-tenant SaaS billing system."
- **P3 (infra):** "Sequence building a CI/CD platform for 50 microservices."
- **P4 (AI-agentic):** "Plan an autonomous agent that triages and resolves GitHub issues."
- **P5 (software):** "Order the build of a real-time collaborative document editor."

## Protocol

1. Run the plan generator (with the skill) on a prompt.
2. Score each of the 10 dimensions 0–2; sum to /20.
3. Record the score and the weakest dimension.

## With-skill vs without-skill baseline

For the same prompt, compare:
- **Without skill:** the same model, same prompt, no skill. Measured scores, per dimension, are in
  [`examples/SCORECARD.md`](../examples/SCORECARD.md). The recurring gaps there: no thin
  end-to-end slice in production first, reproducible builds and environments mostly missing, and
  tradeoffs stated without flip conditions.
- **With skill:** dependency-ordered, skeleton-first, gates up front, cross-cutting threaded.
  Target **>=16/20**.

The delta (with minus without) is the skill's measured lift on this rubric. A healthy skill
shows a clear positive delta on every test prompt, especially the AI ones (P1, P4).
