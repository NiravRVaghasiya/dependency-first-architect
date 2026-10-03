# Dependency-First Architect

A planning skill for coding agents (Claude Code, Cursor, Codex). Ask it to plan, architect, or
sequence a software, infrastructure, or AI build, and it returns a **build plan ordered by what
everything else depends on**: hard-to-reverse decisions settled first, a thin slice running in
production as Phase 0, validation gates for the hypotheses the plan rests on, and security,
observability, reproducibility, and resilience in every phase.

[![CI](https://github.com/NiravRVaghasiya/dependency-first-architect/actions/workflows/ci.yml/badge.svg)](https://github.com/NiravRVaghasiya/dependency-first-architect/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What it is

One instruction file, [`SKILL.md`](SKILL.md), plus reference files it loads on demand. The Cursor
and Codex versions are generated from it, so nothing is maintained twice. A plan has ten sections:
classification and constraints (with labeled budgets), dependency map, Tradeoff gates, walking
skeleton (Phase 0), phases, validation gates, cross-cutting concerns, AI layer, methodology
exceptions, and deliberately deferred work.

> **You:** Plan a customer-support RAG chatbot over our help-center docs.

Two of the six Tradeoff gates that came back in the v2 pilot (Opus 5.5; verbatim, trimmed;
[full plan](eval/results/2026-10-03-pilot-core/generations/P1.dfa.opus-5.5.r01.md)):

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| What gets indexed | R3: a non-public article shown to the public can't be unshown | Index only articles the CMS marks public and published. Filter when indexing, and check visibility again when answering … | The CMS visibility flags are accurate | V1 | Non-public content is needed → retrieval checks the caller's permissions (… login required before Phase 2) |
| Answer policy | R3: answers customers see can become promises with legal liability | Answer only from retrieved passages, with citations. Decline and offer handoff when retrieval is weak … | Strict grounding keeps unsupported claims within V5 and still resolves enough chats | V5 (safety), V6 (usefulness) | V5 fails on a topic → that topic shows article snippets only … |

And the gate that guards the worst failure:

> **V5** · *Hypothesis:* answers state only what the cited passages support, and risky or
> unanswerable questions decline or hand off. · *Threshold:* ≥ 95% of answers fully supported;
> 0 unsupported promises in the policy set … (all ASSUMPTION). Head of Support and legal sign off
> on the policy set. · *If it fails:* failing topics go snippets-only or human-only, then re-run.

## The problem it addresses

Asked to plan a system, language models tend to write a tour of components: build the layers one
at a time, wire them together at the end, add security and monitoring in a final phase, state
tradeoffs without saying what would change them, and present guessed numbers as requirements. The
skill pushes the other way:

1. **Never start work before what blocks it is met.** "It runs" is not validation: a hypothesis
   (fast, safe, correct, or cheap enough) is met only when its validation gate passes.
2. **Keep nothing rigid until it must be**, and scale the plan to the system.
3. **Resolve hard-to-reverse tradeoffs up front** as Tradeoff gates (Decision → Default →
   Assumption → Validation → Flip condition), with effort scaled to reversibility (R1 easy to
   undo, R2 costly, R3 hard).
4. **Order by blast radius:** rework widest first, exposure smallest first (canary, one site).
5. **Thread security, observability, reproducibility, and resilience through every phase.**
6. **Lead with a walking skeleton** in production, deployed, monitored, and rolled back once.
7. **For AI systems, put defenses and budgets before capabilities.**
8. **Treat budgets as constraints.** Each is labeled REQUIREMENT, BASELINE, ASSUMPTION, or
   UNKNOWN, so a guess is never presented as a requirement.
9. **Bend the method only explicitly**, through a numbered methodology exception (rule bypassed,
   why it does not apply, replacement validation, evidence required, when the normal method
   resumes). Brownfield work is the normal method, not an exception.

Dependencies come in seven kinds (structural, runtime, decision, validation, risk/security,
organizational, economic). The rules are in [`SKILL.md`](SKILL.md); worked examples are in
[`reference/validation.md`](reference/validation.md).

## What it does not guarantee

- **A correct architecture.** It shapes how a model plans. The model still supplies the domain
  knowledge and can still be wrong about technologies, limits, and costs.
- **Better systems.** No experiment here has shown that systems built from these plans work
  better; see the evidence below.
- **The same behavior on every model.** With the skill, Haiku 4.5 wrote plans of about 5,300
  words (Opus 5.5: about 3,900), and 13 of its 15 plans printed the private self-check the skill
  says to keep out of the plan (the blinding removed it before judging).
- **Checks that run themselves.** Validation gates name the checks; someone still has to run them.

## Evidence

Every number in the block below is generated from committed run records by
[`eval/dfa_eval/evidence.py`](eval/dfa_eval/evidence.py), and CI fails if the two disagree. What
the measures mean:

- **Adherence** is the skill author's own rubric (v1, /20). It checks what the skill asks for, so
  plans written with the skill are expected to score high by construction. It shows the method is
  followed, nothing more.
- **EQ** (engineering quality, /44) and **EQ-core** (/36, without the two dimensions that overlap
  the method) come from a rubric whose anchors and scoring rules were written by a model session
  that never saw the skill (the maintainer chose the dimension names). LLM judges
  score the *written plan*, not a built system. **Checklist** is coverage of per-prompt lists of
  domain considerations, written the same blind way.
- **Leakage AUC** is how well a judge can tell from the text alone which plans followed a method
  (0.5 = cannot tell, 1.0 = always can). Near 1.0, judges effectively know the condition when they
  score, and their scores may reward the method's recognizable structure.
- Intervals are 95%, across prompts (5 to 8 of them), so they are wide. No p-values are reported.
  Every judge is a Claude model.
- Every committed session ran with the operator's Claude Code user settings, which still apply
  under `--bare`: the built-in **Concise** output style (recorded in every plan and implementer
  transcript; the judges ran with the same settings) and permission mode `auto` (Haiku 4.5:
  `default`). Plan lengths and scores are measured under that style; each run's flags say so.

<!-- BEGIN GENERATED evidence: eval/dfa_eval/evidence.py -->

_Generated from the committed runs by `eval/dfa_eval/evidence.py`; `python eval/run.py check` fails if this block and the records disagree. Cells: mean difference, treatment − control, with its 95% interval (t across prompts for plans; bootstrap over attempts for outcomes)._

**[`2026-10-03-outcomes-webhook-ledger`](eval/results/2026-10-03-outcomes-webhook-ledger/OUTCOMES.md)** — task `eval/outcomes/tasks/webhook-ledger`; planner claude-opus-5-5; implementer claude-sonnet-5-5; 20 complete attempts of 20 planned.

| Treatment − control | n | Round-1 pass rate | Change-request pass rate | Regression pass rate | Rework lines |
|---|---|---|---|---|---|
| dfa-plan − baseline-plan | 5 vs 5 | +0.01 [−0.05, +0.07] | +0.00 [+0.00, +0.00] | +0.01 [−0.05, +0.07] | −12 [−38, +10] |
| baseline-plan − no-plan | 5 vs 5 | +0.09 [−0.09, +0.36] | +0.20 [+0.00, +0.60] | +0.09 [−0.09, +0.36] | +3 [−19, +24] |
| dfa-plan − no-plan | 5 vs 5 | +0.10 [−0.07, +0.38] | +0.20 [+0.00, +0.60] | +0.10 [−0.07, +0.38] | −9 [−34, +11] |
| generic-plan − baseline-plan | 5 vs 5 | +0.03 [−0.02, +0.09] | +0.00 [+0.00, +0.00] | +0.03 [−0.02, +0.07] | −9 [−30, +13] |
| dfa-plan − generic-plan | 5 vs 5 | −0.02 [−0.07, +0.03] | +0.00 [+0.00, +0.00] | −0.02 [−0.07, +0.03] | −4 [−29, +17] |

- caveats, dfa-plan − baseline-plan: n < 10 attempts per arm: descriptive only
- caveats, baseline-plan − no-plan: n < 10 attempts per arm: descriptive only
- caveats, dfa-plan − no-plan: n < 10 attempts per arm: descriptive only
- caveats, generic-plan − baseline-plan: n < 10 attempts per arm: descriptive only
- caveats, dfa-plan − generic-plan: n < 10 attempts per arm: descriptive only
- flag: planner sessions ran with output style 'Concise' and permission mode auto (15) (the operator's Claude Code settings apply under --bare)
- flag: implementer sessions ran with output style 'Concise' and permission mode acceptEdits (40) (the operator's Claude Code settings apply under --bare)
- recorded cost $16.66

**[`2026-10-03-pilot-core`](eval/results/2026-10-03-pilot-core/REPORT.md)** — config `pilot-core`; generator claude-opus-5-5 (effort high); judges claude-sonnet-5-5, claude-fable-5; 5 run(s) per cell configured.

| Treatment − control | Generator | n prompts | Plans | Adherence /20 | EQ /44 | EQ-core /36 | Checklist 0–1 | Words | EQ per 1k tokens |
|---|---|---:|---|---|---|---|---|---|---|
| dfa − baseline | opus-5.5 | 8 | 40 vs 40 | +11.6 [+10.6, +12.6] | +8.2 [+5.3, +11.0] | +6.0 [+3.7, +8.4] | +0.04 [−0.06, +0.13] | +2482 [+2001, +2964] | −7.91 [−11.17, −4.65] |
| dfa − generic-control | opus-5.5 | 8 | 40 vs 40 | +2.3 [+1.0, +3.6] | −1.2 [−1.9, −0.5] | −1.2 [−1.8, −0.7] | −0.05 [−0.07, −0.03] | −4372 [−5316, −3427] | +3.63 [+3.04, +4.22] |
| dfa − dfa-v1 | opus-5.5 | 8 | 40 vs 40 | −0.1 [−0.1, +0.0] | +1.9 [+1.2, +2.5] | +1.6 [+1.0, +2.1] | −0.02 [−0.04, −0.00] | +704 [+354, +1053] | −0.61 [−1.32, +0.10] |
| dfa-v1 − baseline | opus-5.5 | 8 | 40 vs 40 | +11.7 [+10.6, +12.7] | +6.3 [+3.8, +8.8] | +4.5 [+2.5, +6.4] | +0.06 [−0.02, +0.15] | +1778 [+1392, +2165] | −7.31 [−10.60, −4.02] |
| generic-control − baseline | opus-5.5 | 8 | 40 vs 40 | +9.3 [+7.6, +11.0] | +9.3 [+6.5, +12.2] | +7.3 [+4.9, +9.6] | +0.09 [−0.01, +0.18] | +6854 [+5685, +8023] | −11.54 [−14.73, −8.36] |

- caveats, dfa − baseline (opus-5.5): ceiling: 92% of treatment generations at max (Adherence)
- caveats, dfa − generic-control (opus-5.5): ceiling: 92% of treatment generations at max (Adherence)
- caveats, dfa − dfa-v1 (opus-5.5): ceiling: 92% of treatment generations at max (Adherence)
- caveats, dfa-v1 − baseline (opus-5.5): ceiling: 98% of treatment generations at max (Adherence)
- caveats, generic-control − baseline (opus-5.5): ceiling: 18% of treatment generations at max (Adherence); ceiling: 2% of treatment generations at max (Checklist)
- flag: generation sessions ran with output style 'Concise' (160 plans) (the operator's Claude Code settings apply under --bare)
- EQ by judge, dfa − baseline (opus-5.5): fable-5 +6.2 [+3.1, +9.3]; sonnet-5.5 +10.1 [+7.4, +12.8]
- EQ by judge, dfa − generic-control (opus-5.5): fable-5 −0.9 [−1.4, −0.4]; sonnet-5.5 −1.5 [−2.7, −0.2]
- EQ by judge, dfa − dfa-v1 (opus-5.5): fable-5 +1.0 [+0.4, +1.6]; sonnet-5.5 +2.7 [+1.7, +3.7]
- EQ by judge, dfa-v1 − baseline (opus-5.5): fable-5 +5.2 [+2.5, +7.9]; sonnet-5.5 +7.4 [+4.9, +10.0]
- EQ by judge, generic-control − baseline (opus-5.5): fable-5 +7.1 [+4.1, +10.1]; sonnet-5.5 +11.6 [+8.6, +14.6]
- leakage-probe AUC (judge sonnet-5.5, opus-5.5 plans): dfa vs baseline 1.00, dfa vs generic-control 0.99, dfa vs dfa-v1 0.83, dfa-v1 vs baseline 1.00, generic-control vs baseline 1.00
- judge agreement on engineering-quality-v1 (Krippendorff α, 2 coders): 0.12; rank agreement (Spearman) 0.83, with fable-5#1 scoring +6.9 points relative to sonnet-5.5#1
- recorded cost $199.16

**[`2026-10-03-pilot-length`](eval/results/2026-10-03-pilot-length/REPORT.md)** — config `pilot-length`; generator claude-opus-5-5 (effort high); judges claude-sonnet-5-5; 3 run(s) per cell configured.

| Treatment − control | Generator | n prompts | Plans | Adherence /20 | EQ /44 | EQ-core /36 | Checklist 0–1 | Words | EQ per 1k tokens |
|---|---|---:|---|---|---|---|---|---|---|
| dfa-capped − baseline-capped | opus-5.5 | 5 | 15 vs 15 | +11.2 [+9.6, +12.8] | +9.6 [+7.0, +12.2] | +6.3 [+4.1, +8.5] | −0.03 [−0.13, +0.06] | +429 [+117, +741] | +2.08 [−0.18, +4.33] |

- caveats, dfa-capped − baseline-capped (opus-5.5): ceiling: 67% of treatment generations at max (Adherence)
- flag: generation sessions ran with output style 'Concise' (30 plans) (the operator's Claude Code settings apply under --bare)
- leakage-probe AUC (judge sonnet-5.5, opus-5.5 plans): dfa-capped vs baseline-capped 1.00
- recorded cost $7.45

**[`2026-10-03-pilot-models`](eval/results/2026-10-03-pilot-models/REPORT.md)** — config `pilot-models`; generator claude-sonnet-5-5 (effort high), claude-haiku-4-5-20251001 (effort default); judges claude-sonnet-5-5; 3 run(s) per cell configured.

| Treatment − control | Generator | n prompts | Plans | Adherence /20 | EQ /44 | EQ-core /36 | Checklist 0–1 | Words | EQ per 1k tokens |
|---|---|---:|---|---|---|---|---|---|---|
| dfa − baseline | sonnet-5.5 | 5 | 15 vs 15 | +11.7 [+10.2, +13.3] | +10.9 [+6.3, +15.4] | +7.8 [+3.7, +11.9] | +0.05 [−0.11, +0.21] | +1936 [+1300, +2572] | −7.60 [−13.63, −1.57] |
| dfa − baseline | haiku-4.5 | 5 | 15 vs 15 | +13.8 [+10.1, +17.5] | +14.1 [+6.9, +21.4] | +10.1 [+3.5, +16.7] | +0.17 [−0.02, +0.36] | +5046 [+3298, +6794] | −5.06 [−13.86, +3.74] |

- caveats, dfa − baseline (sonnet-5.5): ceiling: 93% of treatment generations at max (Adherence); judge model also generated these plans (Adherence, EQ, EQ-core, Checklist, EQ per 1k tokens)
- caveats, dfa − baseline (haiku-4.5): ceiling: 13% of treatment generations at max (Adherence)
- flag: condition dfa, generator haiku-4.5: 14 of 15 plans could not read the skill's files (all 35 reads of them failed), so they follow SKILL.md alone, not the skill as shipped
- flag: generation sessions ran with output style 'Concise' (60 plans) (the operator's Claude Code settings apply under --bare)
- flag: generation sessions ran in different permission modes: auto (sonnet-5.5 30); default (haiku-4.5 30)
- flag: judge sonnet-5.5 uses the generator's model claude-sonnet-5-5 (generator sonnet-5.5): it judged plans its own model wrote
- leakage-probe AUC (judge sonnet-5.5, sonnet-5.5 plans): dfa vs baseline 1.00
- leakage-probe AUC (judge sonnet-5.5, haiku-4.5 plans): dfa vs baseline 1.00
- recorded cost $11.45

**[`2026-10-03-pilot-models-haiku`](eval/results/2026-10-03-pilot-models-haiku/REPORT.md)** — config `pilot-models-haiku`; generator claude-haiku-4-5-20251001 (effort default); judges claude-sonnet-5-5; 3 run(s) per cell configured.

| Treatment − control | Generator | n prompts | Plans | Adherence /20 | EQ /44 | EQ-core /36 | Checklist 0–1 | Words | EQ per 1k tokens |
|---|---|---:|---|---|---|---|---|---|---|
| dfa − baseline | haiku-4.5 | 5 | 15 vs 15 | +14.5 [+11.9, +17.0] | +14.9 [+7.6, +22.3] | +10.7 [+4.0, +17.3] | +0.25 [+0.04, +0.46] | +3970 [+1839, +6102] | −3.19 [−10.40, +4.02] |

- caveats, dfa − baseline (haiku-4.5): ceiling: 7% of treatment generations at max (Adherence)
- flag: generation sessions ran with output style 'Concise' (30 plans) (the operator's Claude Code settings apply under --bare)
- leakage-probe AUC (judge sonnet-5.5, haiku-4.5 plans): dfa vs baseline 1.00
- recorded cost $5.27

**[`2026-10-03-v1-examples-rejudge`](eval/results/2026-10-03-v1-examples-rejudge/REPORT.md)** — config `v1-examples-rejudge`; generator claude-opus-5-5 (effort max); judges claude-sonnet-5-5, claude-fable-5; 1 run(s) per cell configured.

| Treatment − control | Generator | n prompts | Plans | Adherence /20 | EQ /44 | EQ-core /36 | Checklist 0–1 | Words | EQ per 1k tokens |
|---|---|---:|---|---|---|---|---|---|---|
| dfa-v1 − baseline | opus-5.5-max | 5 | 5 vs 5 | +8.4 [+4.4, +12.4] | +3.4 [+0.9, +5.9] | +2.5 [+0.2, +4.8] | +0.07 [−0.05, +0.19] | +2675 [+1186, +4164] | −8.05 [−18.46, +2.35] |

- caveats, dfa-v1 − baseline (opus-5.5-max): ceiling: 100% of treatment generations at max (Adherence); no variation within prompts: g undefined (Adherence, EQ, EQ-core, Checklist, Words, EQ per 1k tokens); ceiling: 20% of treatment generations at max (Checklist)
- flag: manifest note: Imported, not generated: the plans in examples/ as committed, verified against the SHA-256 recorded in examples/scoring/generation.json at generation time (2026-10-02, model claude-opus-5-5, effort max, skill commit 3ba6b70). Tokens, cost and latency were not recorded in v1.
- flag: small n: 1 run(s) per cell (< 3)
- EQ by judge, dfa-v1 − baseline (opus-5.5-max): fable-5 +2.2 [−0.0, +4.4]; sonnet-5.5 +4.6 [+0.6, +8.6]
- leakage-probe AUC (judge sonnet-5.5, opus-5.5-max plans): dfa-v1 vs baseline 1.00
- judge agreement on engineering-quality-v1 (Krippendorff α, 2 coders): −0.34; rank agreement (Spearman) 0.71, with fable-5#1 scoring +7.2 points relative to sonnet-5.5#1
- recorded cost $7.38
<!-- END GENERATED evidence -->

The v1.0.0 measurement, kept for comparison:

<!-- BEGIN GENERATED score callout: examples/scoring/tally.py -->
> **v1.0.0, methodology adherence.** In Claude Code (same model, the 5 fixed prompts in [`reference/evals.md`](reference/evals.md), one run per arm, 3 judges on the same model, blind to the arm), plans scored **12.8/20 without the skill and 20.0/20 with it (+7.2)** on the skill's own rubric. On a deliberately skill-unfavorable reading (every dispute from an adversarial audit accepted, and the D9 rubric artifact removed), the difference is still **+5.6**. That rubric checks what the skill asks for, and v1's plans self-scored against it before answering, so this shows the method is followed, not that the plans are better engineering. [Scorecard, method and raw scores →](examples/SCORECARD.md)
<!-- END GENERATED -->

### What the evidence supports

The differences and intervals come from the block above; the counts and per-arm figures come
from the runs' `REPORT.md` and `OUTCOMES.md`, which `check` recomputes, but this prose itself is
not compared with them automatically.

- **The method is followed.** Plans written with the skill have its structure: +11.6 adherence
  points of 20 for Opus 5.5 in the core pilot (+11.7 for Sonnet 5.5, +14.5 for Haiku 4.5). This
  is expected by construction, and says nothing about quality. (In `pilot-models` every read of
  the skill's reference files by Haiku 4.5 was refused, so that run's Haiku contrast measures
  `SKILL.md` alone; `pilot-models-haiku` re-ran Haiku with the files readable, and its numbers
  are the ones quoted here.)
- **Judges rate the plans higher than the bare model's, but not higher than those from another
  detailed planning instruction.** In the core pilot (8 prompts × 5 runs), dfa − baseline is
  +8.2 EQ [+5.3, +11.0], and both judges put it above zero. The active control, a generic
  planning skill written independently of this one, scores slightly *higher*: dfa −
  generic-control is −1.2 EQ [−1.9, −0.5], and both judges agree on the sign. So the gain over
  the bare model is not specific to this method; under these judges it is what a detailed
  planning instruction buys.
- **Length goes with it.** Average plan length is 1,452 words for the bare model, 3,935 with the
  skill, and 8,306 with the generic control. Per 1,000 tokens, plans with the skill score lower
  than baseline (−7.9) and higher than the control (+3.6). Capping both arms at 1,500 words
  (pilot-length) did not equalize length: 2 of 15 capped baseline plans (5 within 10% of it) and
  none of the 15 capped skill plans stayed within it, and the skill's averaged 429 words more.
  Under the cap the
  skill still scored +9.6 EQ, so length is not the whole story, but it was not removed either.
- **Domain coverage moves little, except for the smallest model.** On the per-prompt checklists of
  domain considerations, dfa − baseline is +0.04 [−0.06, +0.13] in the core pilot, and the
  interval includes zero for Opus 5.5 and Sonnet 5.5 in every run; for Haiku 4.5 it is +0.25
  [+0.04, +0.46]. The control covers slightly more than the skill (−0.05 [−0.07, −0.03]). For the
  stronger models, the skill changes how a plan is organized, sequenced and validated more than
  what it covers.
- **v2 against v1.** v2 scores +1.9 EQ [+1.2, +2.5] above v1.0.0 on the same prompts, and its
  plans are 704 words longer. Adherence is at its ceiling for both.
- **Judges can tell the conditions apart.** The leakage AUC is 1.00 against the baseline in every
  run, and 0.99 between the skill and the control. Judging was blind to labels, not to style.
  Sonnet 5.5 and Fable 5 rank plans alike (Spearman 0.83 in the core pilot), but Fable scores the
  same plans about 7 points higher.
- **Outcomes: no measurable difference.** On the one outcome task, the arms' round-1 pass-rate
  differences are within ±0.10, with intervals spanning zero. Leaving out four tests that check a
  status-code convention, every attempt given a plan passed every round-1 test, and so did 4 of 5
  without one; the fifth crashed on a bug of its own. A plan raised the implementer's cost per
  attempt by $0.11 (baseline and skill plans) to $0.23 (the control's longer plans). In 6 of
  the 15 attempts given a plan, and none of the 5 without, the code acknowledged malformed events
  and quarantined them, a design those four tests count as failures
  ([details](eval/outcomes/README.md#what-the-first-run-showed-about-the-task-itself)).

None of this shows that systems built with the skill are better. It shows that the method is
followed, and that LLM judges, who can tell which plans followed a method, rate those plans
higher than the bare model's and slightly lower than a longer generic method's.

### What is not validated

- **That this method, rather than any detailed planning instruction, is what helps.** The one
  active control tested scored at least as well on every judged measure except adherence.
- **System-level outcomes** (deployment, operations, incidents, cost in production). Nothing
  here measures them, and the one code-level outcome task did not separate the arms.
- **The newer rules on their own:** dependency kinds, validation gates, labels, reversibility
  tiers and methodology exceptions are measured only as part of the whole skill; no experiment
  removes one at a time.
- **Cursor and Codex.** The adapters are generated and checked, but no committed run used them.
  The [evaluation matrix](docs/evaluation-matrix.md) shows what is *supported*, *tested*, and
  *experimentally evaluated*, per agent and model.
- **Judges outside the Claude family**, and **expert review** of the engineering-quality rubric
  and the checklists.
- **Realistic requests.** The benchmark prompts are one-line requests with no codebase or
  documents around them.

## Install

**Claude Code** (the full skill, for all your projects):

```bash
git clone --depth 1 https://github.com/NiravRVaghasiya/dependency-first-architect ~/.claude/skills/dependency-first-architect
```

**Cursor** (run in your project root):

```bash
mkdir -p .cursor/rules && curl -fsSL https://raw.githubusercontent.com/NiravRVaghasiya/dependency-first-architect/main/adapters/cursor-dependency-first-architect.mdc -o .cursor/rules/dependency-first-architect.mdc
```

**Codex** (run in your project root). This appends the skill to `AGENTS.md`, which Codex reads in
every session (about 16 KB):

```bash
{ printf '\n'; curl -fsSL https://raw.githubusercontent.com/NiravRVaghasiya/dependency-first-architect/main/adapters/AGENTS.md; } >> AGENTS.md
```

**Codex, lazy install** (not yet tested in Codex). About 1 KB stays in `AGENTS.md`; Codex reads
the whole skill, reference files included, only when a request matches:

```bash
mkdir -p .codex && curl -fsSL https://raw.githubusercontent.com/NiravRVaghasiya/dependency-first-architect/main/adapters/codex/dependency-first-architect.md -o .codex/dependency-first-architect.md
{ printf '\n'; curl -fsSL https://raw.githubusercontent.com/NiravRVaghasiya/dependency-first-architect/main/adapters/codex/AGENTS-snippet.md; } >> AGENTS.md
```

Then ask: *"Plan a customer-support RAG chatbot over our help-center docs."* In Claude Code you
can also call it by name: `/dependency-first-architect <your request>`.

- **Pin a version.** These files are instructions your agent follows, so treat an update like a
  dependency upgrade: install from a release tag (`git clone --branch vX.Y.Z`, or `vX.Y.Z` in
  place of `main` in the URLs) and read the diff before you update. See [`SECURITY.md`](SECURITY.md).
- **Update a Codex install.** The commands append, so delete the old block from `AGENTS.md`
  first: everything from the `<!--` line above `BEGIN dependency-first-architect` through
  `<!-- END dependency-first-architect -->`. Then run the install again.

### What loads when

| Agent | Always in context | When the skill fires | On demand |
|---|---|---|---|
| Claude Code | the description (~0.5 KB) | the `SKILL.md` body (~16 KB) and the plan template it loads at once (~6.6 KB) | the worked examples (~11 KB) and the AI layer (~5.5 KB), when a step calls for them |
| Cursor | the description (`alwaysApply: false`) | the procedure (~16 KB), without the reference files | — |
| Codex | the whole procedure (~16 KB), without the reference files | — | — |
| Codex, lazy | a ~1 KB stub | the whole skill with its reference files (~40 KB) | — |

The adapters leave out the steps only Claude Code can follow (loading reference files), and the
evaluation rubric is never loaded while planning.

## Evaluate it yourself

```bash
python -m unittest discover -s tests   # offline, no model calls
python eval/run.py check               # what CI verifies: provenance, every committed run, the numbers in this README
python eval/run.py plan --config eval/experiments/smoke.json   # dry run: what a run would call and cost
python eval/run.py all  --config eval/experiments/smoke.json --run eval/results/<new-run-id>   # about $2
```

Real runs need the `claude` CLI (with `ANTHROPIC_API_KEY`, or a Bedrock or Vertex
configuration) or an OpenAI-compatible endpoint. [`eval/README.md`](eval/README.md) covers:

- **the protocol:** isolated sessions, blinding, randomized judging, the leakage probe, the
  statistics;
- **how to reproduce** a committed run (`eval/experiments/*.json` hold their exact configs);
- **how to interpret** the scores, and the limitations.

The outcome benchmark, which asks whether a plan changes the code built from it, is in
[`eval/outcomes/`](eval/outcomes/README.md). It executes model-written code, so run it in a
container.

## Extend

- **A new adapter:** add a builder in `build.py`, an entry in `eval/agents.json`, a test in
  `tests/test_build.py`, and an install command here. It is listed as *supported* until a
  committed run says more. See [`CONTRIBUTING.md`](CONTRIBUTING.md).
- **A new benchmark prompt:** add it to `eval/benchmark.json`, with a checklist written by a
  session that has not seen the skill ([`eval/rubrics/AUTHORING.md`](eval/rubrics/AUTHORING.md)).
- **A new outcome task:** a brief, starter code, hidden tests by category, reference solutions,
  and single-defect mutants that the tests must catch. See
  [`eval/outcomes/README.md`](eval/outcomes/README.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Project

```
dependency-first-architect/
├── SKILL.md          # canonical source: the method; the frontmatter description is the trigger
├── reference/        # loaded on demand: plan template, worked examples, AI layer, layer-map analogy;
│                     #   evals.md is the v1 adherence rubric (evaluation only, never loaded to plan)
├── adapters/         # GENERATED by build.py: Cursor rule, Codex AGENTS.md, Codex lazy install
├── build.py          # regenerates adapters/ and SHA256SUMS; --check in CI (standard library only)
├── eval/             # evaluation harness, rubrics, conditions, experiments, committed runs
├── examples/         # the v1.0.0 measurement: plans, scorecard, and its harness
├── docs/             # evaluation matrix (generated), layer-map diagram
├── tests/            # offline tests for all of the above
└── VERSION  CHANGELOG.md  SHA256SUMS  SECURITY.md  CONTRIBUTING.md  LICENSE
```

- **Single source of truth.** Change the method in `SKILL.md` or `reference/`, then run
  `python build.py`. Never edit `adapters/`; CI fails on drift.
- **Versioning.** The version in `VERSION` follows semantic versioning and is recorded in
  [`CHANGELOG.md`](CHANGELOG.md): MAJOR when the plan's shape or the procedure changes, MINOR for
  added guidance, PATCH for wording. Releases are signed tags; verify a checkout with
  `python build.py --check` and `sha256sum -c SHA256SUMS`.

![Layer map: build bottom-up. Blueprint (Tradeoff gates), cells (primitives), energy (infra and CI/CD), skeleton (walking skeleton), organs (features), and skin (UI) last, with security, observability, reproducibility and resilience through every layer](docs/layer-map.svg)

The diagram is a teaching analogy ([`reference/layer-map.md`](reference/layer-map.md)). Where it
and engineering disagree, engineering wins.

## License

[MIT](LICENSE)
