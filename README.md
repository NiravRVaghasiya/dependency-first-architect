# Your AI plans in layers. This one starts with a thin slice running in production.

**dependency-first-architect** is a planning skill for Claude Code, Cursor, and Codex. Ask it to
plan any software, infra, or AI build and you get a build order: irreversible decisions settled
first, a thin slice running in production as Phase 0, then each layer in dependency order, with
security and observability in every phase.

[![CI](https://github.com/NiravRVaghasiya/dependency-first-architect/actions/workflows/ci.yml/badge.svg)](https://github.com/NiravRVaghasiya/dependency-first-architect/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Demo

<!-- DEMO GIF PLACEHOLDER. Record the request below, save it as docs/demo.gif, and replace this
     comment with: ![Planning a RAG chatbot with dependency-first-architect](docs/demo.gif)
       asciinema rec demo.cast -c 'claude "/dependency-first-architect Plan a customer-support RAG chatbot over our help-center docs."'
       agg demo.cast docs/demo.gif
-->

> **You:** Plan a customer-support RAG chatbot over our help-center docs.

What comes back (real output, trimmed; [full plan](examples/p1-rag-chatbot/with-skill.md)):

> **Sequence:** P0 skeleton → P1 guards, budgets, handoff → P2 retrieval → P3 model access → P4 memory + orchestration → P5 routing → P6 feedback.

| Decision | Default (chosen now) | Flip condition |
|---|---|---|
| (AI) Prompt+RAG vs fine-tune | Prompt + RAG. Docs change weekly; a fine-tuned model holds stale facts and can't cite them | Failures that survive prompt changes are about tone, format or terminology rather than facts … **Never fine-tune for facts** |
| Answer posture | **Accuracy over coverage: cite or abstain.** … *So the human handoff is foundational: it gets built in Phase 1, not as a late feature* | Accuracy holds at target for 4 weeks in a row **and** "I don't know" is the top reason for handoffs → loosen the threshold for low-risk topics only |
| *… 9 more Tradeoff gates* | | |

Phase 0, before any feature work:

> **The one real request:** On the **production** help center, a staff member asks *"How do I reset my password?"* (the widget is shown only to staff via a feature flag). … OpenTelemetry traces with one span per tier … **A smoke eval runs in CI on every deploy:** …

<!-- BEGIN GENERATED score callout: examples/scoring/tally.py -->
> **Measured, not claimed.** In Claude Code, same model, the 5 fixed prompts in [`reference/evals.md`](reference/evals.md), one run per arm, each plan blind-scored by 3 independent judges: **12.8/20 without the skill → 20.0/20 with it (+7.2)**. On a deliberately skill-unfavorable reading (every dispute from an adversarial audit accepted, and the D9 rubric artifact removed), the lift is still **+5.6**. [Scorecard, method and raw scores →](examples/SCORECARD.md)
<!-- END GENERATED -->

## Install

**Claude Code**: all your projects, updates with `git pull`:

```bash
git clone --depth 1 https://github.com/NiravRVaghasiya/dependency-first-architect ~/.claude/skills/dependency-first-architect
```

**Cursor**: run in your project root:

```bash
mkdir -p .cursor/rules && curl -fsSL https://raw.githubusercontent.com/NiravRVaghasiya/dependency-first-architect/main/adapters/cursor-dependency-first-architect.mdc -o .cursor/rules/dependency-first-architect.mdc
```

**Codex**: run in your project root (appends to an existing `AGENTS.md`, or creates one):

```bash
{ printf '\n'; curl -fsSL https://raw.githubusercontent.com/NiravRVaghasiya/dependency-first-architect/main/adapters/AGENTS.md; } >> AGENTS.md
```

Then ask for a plan: *"Plan a customer-support RAG chatbot over our help-center docs."* In Claude
Code you can also call it by name: `/dependency-first-architect <your request>`.

The Claude Code install is the full skill, and it is what the scorecard measured. The Cursor and
Codex adapters carry the same 8-step procedure but not the `reference/` files, so plans there
don't get the exact output template or the self-score against the rubric. To update the Codex
copy, replace the block between its `BEGIN` and `END dependency-first-architect` markers.

## How it orders a build

![Layer map: six layers built bottom-up (Blueprint/Tradeoff gates, Cells/primitives, Energy/infra and CI/CD, Skeleton/walking skeleton, Organs/features, Skin/UI built last), with security, observability, reproducibility and resilience running through every layer](docs/layer-map.svg)

The picture is the teaching analogy from [`reference/layer-map.md`](reference/layer-map.md). The
rules the skill actually enforces:

1. Never specify a component before the thing it depends on is proven.
2. Keep nothing rigid until it must be: scaffold first, harden stable parts later.
3. Resolve irreversible tradeoffs up front as **Tradeoff gates** (default + flip condition).
4. Order work within a phase by **blast radius, widest first**.
5. Thread **security, observability, reproducibility, resilience** through every phase from the
   first commit. Observability is day-zero, not last.
6. Lead with a **walking skeleton**: the thinnest end-to-end path that runs in prod.
7. For AI/agentic systems, add the AI layer: prompt-injection/guardrail defense, cost+latency
   budget, human-in-the-loop gating, then retrieval → model access → memory → orchestration →
   routing → feedback.

In Claude Code, every plan ends with a self-score against the rubric in
[`reference/evals.md`](reference/evals.md). Below 16/20, it revises before answering.

## Proof

- [`examples/`](examples/README.md): the five fixed eval prompts, each answered with and without the
  skill, plus a side-by-side of the biggest gap.
- [`examples/SCORECARD.md`](examples/SCORECARD.md): blind scores, per-dimension breakdown, an
  adversarial audit, method, and threats to validity.
- [`examples/scoring/`](examples/scoring/): the raw judge scores, the per-session protocol log, the
  tally script that produces every score above, and the harness that re-runs the whole eval
  (`run_eval.py`).

## Token model (progressive disclosure)

In Claude Code, three tiers keep the always-on cost low:

- **Always loaded:** the frontmatter `description` only. A few lines, enough to trigger.
- **Loaded when the skill fires:** the `SKILL.md` body, the 8-step procedure.
- **Loaded on demand:** the `reference/*.md` files. Heavy detail (output format, AI-layer
  detail, eval rubric, teaching analogy), pulled in only when a step calls for them.

So routine turns pay only for the description; the full procedure and references load lazily.
Cursor's rule is `alwaysApply: false`, so it also loads only when its description matches. Codex
reads `AGENTS.md` in every session, so there the whole procedure is always in context.

## Single source of truth

The logic lives in **one canonical file: `SKILL.md`.** The Cursor and Codex adapters are
**generated** from it by `build.py`. Each adapter carries a **"GENERATED — do not hand-edit"**
banner, and CI runs `python build.py --check` on every push and pull request. It fails if either
adapter differs from what `SKILL.md` generates. To change behavior, edit `SKILL.md` and rerun the
build. Never edit files in `adapters/` directly.

```
dependency-first-architect/
├── SKILL.md            # canonical source: frontmatter (trigger) + 8-step procedure
├── build.py            # regenerates the two adapters from SKILL.md (stdlib only); --check for CI
├── adapters/           # GENERATED by build.py — do not hand-edit
│   ├── cursor-dependency-first-architect.mdc   # Cursor rule, alwaysApply:false
│   └── AGENTS.md                               # Codex / OpenAI agents
├── reference/
│   ├── plan-template.md   # exact BUILD PLAN output format
│   ├── ai-systems.md      # the AI/agentic layer detail
│   ├── evals.md           # rubric (/20) + test prompts + protocol + baseline
│   └── layer-map.md       # body analogy, teaching-only (engineering wins on conflict)
├── examples/           # with/without-skill plans for the 5 eval prompts + SCORECARD.md
│   └── scoring/        # raw scores, tally.py, run_eval.py
├── docs/layer-map.svg  # the diagram above
├── tests/              # self-test for build.py --check
└── .github/workflows/ci.yml
```

## Build

```bash
python build.py          # regenerate both adapters from SKILL.md
python build.py --check  # verify they're in sync; exit 1 on drift (what CI runs)
```

Both modes check that the shared sentinel ("Tradeoff gates") survives into both adapters. Output
is byte-identical on every OS (LF line endings). No dependencies.

## Usage

Ask the assistant to plan, architect, or sequence a build. Examples:

- "Plan a customer-support RAG chatbot over our help-center docs."
- "Architect a multi-tenant SaaS billing system."
- "Sequence building a CI/CD platform for 50 microservices."

The assistant classifies the system, resolves Tradeoff gates, defines a walking skeleton,
lays out dependency-ordered phases with cross-cutting concerns threaded through each, adds the
AI layer when relevant, lists deferred work, and (in Claude Code) self-scores the plan /20
against `reference/evals.md`.

## License

[MIT](LICENSE)
