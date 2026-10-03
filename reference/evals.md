# Methodology-adherence rubric (v1) — test prompts, interpretation

> **What this rubric measures.** Whether a BUILD PLAN *follows the Dependency-First Architect
> methodology*. It was written by the skill's author to check exactly the things the skill asks
> for, so a with-skill plan that scores 20/20 shows the skill was followed. It does **not** show
> that the architecture is correct, feasible, or better than another plan. Engineering quality is
> scored separately, with a rubric that does not use the skill's vocabulary
> ([`eval/rubrics/engineering-quality-v1.json`](../eval/rubrics/engineering-quality-v1.json)),
> and downstream outcomes by the outcome benchmark ([`eval/outcomes/`](../eval/outcomes/)).
> See [`eval/README.md`](../eval/README.md).
>
> This file is evaluation material for maintainers. The skill does not load it while planning.
> Through v1.x it did: Step 8 had the model score its own plan against this rubric and revise
> below 16/20. v2.0.0 removed that self-score, so v1 with-skill plans were written with this
> rubric in context and v2 plans are not; a v1-vs-v2 comparison on this rubric is confounded by
> that change.

Score is **/20**: 10 dimensions, 0–2 points each. The text below is unchanged from v1.0.0, so the
yardstick is the same across versions (the conditions it was applied under are not; see above).
The judge prompt that applies it, with its scoring rules, is in [`eval/rubrics/methodology-adherence-v1.json`](../eval/rubrics/methodology-adherence-v1.json).

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

**Adherence bands:** ≥16 adherent · 12–15 partially adherent · <12 not adherent. (In v1.0.0
these were the skill's ship/revise/re-plan thresholds for its self-score.)

## Fixed test prompts

The five prompts of the original eval, verbatim. They are also the first five prompts of
[`eval/benchmark.json`](../eval/benchmark.json); a test keeps the two in sync.

- **P1 (AI):** "Plan a customer-support RAG chatbot over our help-center docs."
- **P2 (software):** "Architect a multi-tenant SaaS billing system."
- **P3 (infra):** "Sequence building a CI/CD platform for 50 microservices."
- **P4 (AI-agentic):** "Plan an autonomous agent that triages and resolves GitHub issues."
- **P5 (software):** "Order the build of a real-time collaborative document editor."

## Protocol

Plans are generated in isolated sessions, blinded, and scored by independent judge sessions; see
[`eval/README.md`](../eval/README.md) for the harness and how to run it. The v1.0.0 measurement
(one run per arm) and its harness are in [`examples/`](../examples/SCORECARD.md).

## With-skill vs without-skill on this rubric

The delta (with minus without) is the skill's measured effect **on adherence to its own
methodology**. A working skill should show a clear positive delta here; that is a necessary
condition, not evidence of better engineering. Measured v1.0.0 numbers, per dimension, are in
[`examples/SCORECARD.md`](../examples/SCORECARD.md). The recurring gaps without the skill there:
no thin end-to-end slice in production first, reproducible builds and environments mostly
missing, and tradeoffs stated without flip conditions.
