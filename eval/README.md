# Evaluation harness

This directory measures what Dependency-First Architect does to a model's plans, and, separately,
whether those plans lead to better code. It is built to make overclaiming hard: every published
number is recomputed from raw, committed records in CI, judges never learn which condition wrote a
plan, and the rubric that the skill was written to satisfy is reported apart from one it never saw.

Standard library only (Python 3.9+). Nothing here runs a model unless you call a stage that does.

## What is measured, and what each number means

| Category | Question | Instrument | Circular? |
|---|---|---|---|
| **A. Methodology adherence** | Did the plan follow the method? | [`rubrics/methodology-adherence-v1.json`](rubrics/methodology-adherence-v1.json): the v1.0.0 rubric, 10 dimensions × 0–2 = /20, LLM-judged | **Yes, by design.** The skill's author wrote it to check what the skill asks for. A high score for a with-skill plan shows the instructions were followed, nothing more. |
| **A′. Template structure** | Does the plan have the v2 sections, tables, IDs, and labels? | [`dfa_eval/lint.py`](dfa_eval/lint.py): 15–16 deterministic checks, 0–1 | **Yes.** It checks the v2 output format. Plans without the skill fail it by construction. |
| **B. Engineering quality** | Is the plan good engineering, whatever produced it? | [`rubrics/engineering-quality-v1.json`](rubrics/engineering-quality-v1.json): 11 dimensions × 0–4 = /44, plus a per-prompt domain checklist (coverage 0–1) and domain traps, LLM-judged | **Mostly not.** Written blind to the skill ([`rubrics/AUTHORING.md`](rubrics/AUTHORING.md)), with no methodology vocabulary. Two dimensions (validation quality, decision reversibility) overlap what v2 instructs; **EQ-core** (/36) drops them. |
| **C. Outcomes** | Does a plan change the code an implementer writes from it? | [`outcomes/`](outcomes/README.md): hidden tests and a change request on a fixed coding task | **No.** Pass/fail tests, validated against reference solutions and mutants. |
| Length and cost | How long and how expensive is each plan? | words, estimated visible tokens, billed tokens, cost, latency | — |

Two derived measures guard against "longer is better": **EQ per 1,000 visible tokens**, and the
**length-capped conditions**, where both arms get the same 1,500-word limit.

Nothing here is a leaderboard. Reports give distributions and differences with intervals, and no
"winner".

## Protocol

**Generation.** Every plan is one isolated `claude --bare -p` session (no CLAUDE.md, hooks or
auto-memory; `--bare` still applies the operator's user settings, so every committed session ran
with Claude Code's built-in `Concise` output style, and the summaries flag the output style and
permission mode each session reported), started in a fresh, empty directory with a hash for a
name and only a
neutral `README.md` that says the directory is empty
([`conditions/neutral-workspace-README.md`](conditions/neutral-workspace-README.md)). A skill
condition loads its skill as a session-only plugin holding `SKILL.md` and exactly the reference
files it names, which the session may read whatever its permission mode (`--add-dir`), and is
invoked as `/<skill-name> <request>`. Each record keeps the model the
provider *actually* used, and the main-loop model must be the requested one: the Claude CLI
silently substitutes another model for an id it does not recognize (observed: `--model
claude-haiku-4-5` ran `global.anthropic.claude-opus-5`), and in the core pilot it once answered a
Fable 5 judge call with Opus 5. Such a call is recorded as an error and never counted. Two more
guards apply to skill conditions, because `--bare` still lists the user's personal skills: a
session that could also see another skill (including a second copy of its own) is recorded as
contaminated, and a session whose skill was not offered and loaded nothing of it is recorded as
an error, not as a treatment plan.

**Conditions** ([`benchmark.json`](benchmark.json)): `baseline` (the request alone), `dfa` (this
repository's skill), `dfa-v1` (the skill exactly as v1.0.0 measured it,
[`conditions/dfa-v1.0.0/`](conditions/dfa-v1.0.0/README.md)), `generic-control` (an active
control: an independently written generic planning skill of similar length,
[`conditions/generic-architect/`](conditions/generic-architect/README.md)), and length-capped
versions of `baseline` and `dfa`.

**Blinding.** Judges see one plan at a time, with tools disabled, under an opaque id, each call in
a fresh empty directory of its own. Any self-score section is stripped (v1 plans print one), and
a score out of 20 left after stripping is reported as residue; the provenance header of imported
plans is stripped too. For the engineering-quality rubric the
methodology's vocabulary is also neutralized ("Tradeoff gates" → "key decisions", "walking
skeleton" → "first end-to-end slice", …; the table is in
[`dfa_eval/blinding.py`](dfa_eval/blinding.py)) identically for every condition. The mapping from
opaque ids to conditions lives in `blind/key.json`, which judges never see. Structure (tables,
section order) still leaks, so a **leakage probe** asks a judge to guess, per plan, whether it
followed a methodology, and the report gives the AUC of those guesses (0.5 = cannot tell, 1.0 =
always can).

**Judging.** Judge prompts are separate from generation prompts and never mention conditions,
methodologies, or other plans. The order of judge calls is shuffled with a seed derived from the
run seed. Each response must validate against a schema built from the rubric (every dimension
exactly once, scores in range, checklist and trap ids exact) or it is retried, then recorded as
invalid and excluded. Records written since the field was added carry the SHA-256 of the blind
copy they scored, so a judgment of a copy that has since changed is excluded and redone (most
records in the committed runs predate it; their blind copies are checked against the key). Judges can be different models from the
generator, and a judge can be limited to some rubrics (`rubrics` in its config): the core pilot
uses Sonnet 5.5 on both rubrics and Fable 5 on engineering quality only. All are Claude models:
there is no cross-family judge in the published runs, because no other provider was available
when they were made.

**Statistics** ([`dfa_eval/stats.py`](dfa_eval/stats.py), exact arithmetic so results are
byte-identical across platforms and Python versions). The unit of generalization is the prompt:
contrasts report per-prompt differences of cell means, their mean with a Student-t 95% interval
across prompts (df = prompts − 1), the paired effect size d_z, Hedges' g and Cliff's delta
computed within prompts (so unequal cell sizes cannot flip their sign), and a two-stage cluster
bootstrap interval (prompts, then generations within each). The headline judged metrics are
complete-case: a plan counts only if every judge configured for that rubric scored it, so one
judge failing more often on one arm cannot shift that arm's mean (excluded plans are flagged per
arm). Per-judge EQ contrasts (`eq_total@<judge>`) use everything that judge scored. Judge
agreement is Krippendorff's alpha (interval), plus rank consistency (Spearman) and the mean
offset between each pair of judges, because a judge that scores everything higher but in the
same order lowers alpha without disagreeing about which plan is better. There are no p-values.
Automatic notes flag few prompts, ceiling effects, zero variance, incomplete judging, and judges
that are also the generator.

**Provenance.** A run directory is self-contained: `manifest.json` (config, hashes of every skill
and condition file, versions), every generation's text and raw provider output, the blind copies
and key, every judgment and probe with raw output, lint results, the rubric files it was judged
with, and the generated `summary.json` and `REPORT.md`. A record that a retry replaces is never
overwritten: it moves, with its raw output, to `superseded/` (numbered), still counts toward the
cost cap and the summary's cost, and is never used in a metric. The summary also discloses
planned plans that have no record, notes in the manifest (such as a config replaced with
`--force-config`), and any condition whose plans loaded different versions of the same file.

`python eval/run.py check` (in CI) verifies that every committed run's numbers follow from its
records:

- every record validates, and every id, hash and file reference holds;
- every judgment's and probe's answer equals what its raw provider output says, re-parsed;
- every lint record equals a fresh lint of its plan;
- the run's rubric copies are the rubrics its judgments recorded;
- every raw output in `raw/` belongs to a record;
- outcome attempts: test counts follow from the per-test rows, and file lists and code hashes
  from the code snapshots;
- `summary.json` and `REPORT.md` (or the outcome summary and report) are recomputed
  byte-for-byte, and so are the evaluation matrix and the README's evidence block;
- result files outside a committed run (no `manifest.json`) are an error.

It also checks that the rubrics, checklists and control skill still equal their blind-authored
records, that the `dfa-v1` files hash to the digests pinned for commit 3ba6b70, and that P1–P5
are the original prompts in the benchmark, every experiment config, and every committed run.
Records and raw files are not signed, so this catches inconsistent and accidental edits, not a
deliberate forgery of a whole run.

## Running it

Every stage reads and writes one run directory and can be resumed; `--max-cost-usd` caps a stage
(spending stops when the run's recorded cost reaches the cap), and `--retry-failed` redoes
failed calls (the failed records are kept under `superseded/`). A large experiment is easier to
run stage by stage than with `all`: each stage picks up where the records leave off.

```bash
python -m unittest discover -s tests   # offline: the harness, statistics, benchmark (no model calls)
python eval/run.py check               # what CI verifies

python eval/run.py plan --config eval/experiments/pilot-core.json      # dry run: matrix and call counts
python eval/run.py all  --config eval/experiments/pilot-core.json --run eval/results/<run-id>
# or stage by stage: generate, blind, judge, probe, lint, aggregate, report
```

Requirements for real runs: the `claude` CLI on PATH, authenticated with `ANTHROPIC_API_KEY` or a
Bedrock/Vertex configuration (`--bare` does not use a claude.ai login). Other providers:
`openai-compatible` (any Chat Completions endpoint; supports seeds) and `command` (wraps another
agent CLI, e.g. Codex or Cursor, installing the real adapter file into the workspace; see the
`CommandProvider` docstring in [`dfa_eval/providers.py`](dfa_eval/providers.py)). Seeds are
derived per generation and passed to providers that honor them; the Claude CLI does not, and
records say so (`seed_honored: false`).

### Re-judging the published v1.0.0 plans

```bash
python eval/run.py import-v1 --run eval/results/<run-id>   # imports examples/, verified by hash
python eval/run.py blind --run ... ; judge ; probe ; lint ; aggregate ; report
```

### Outcome benchmark

```bash
python eval/run.py outcomes plan      --config eval/outcomes/outcomes.json --run eval/results/<id>
python eval/run.py outcomes implement --config eval/outcomes/outcomes.json --run eval/results/<id> --allow-code-execution
python eval/run.py outcomes aggregate --run eval/results/<id>
python eval/run.py outcomes report    --run eval/results/<id>
```

`implement` executes model-written code; run it in a container or VM
([`outcomes/README.md`](outcomes/README.md#safety-this-runs-model-written-code)).

## Reproducing published results

Committed runs live in [`results/`](results/README.md). To check that their numbers follow from
their raw records: `python eval/run.py check`. To re-run an experiment from scratch, run its
config (`manifest.json` → `config_file`) into a new run directory and compare the two `REPORT.md`
files. Expect differences: model APIs are not deterministic, judge scores vary between calls, and
models change over time. A replication should land inside the published intervals more often than
not; it will not be byte-identical.

## Interpreting scores

- **Adherence near 20/20 for plans written with the skill is the expected result, not evidence.**
  It shows the model followed the instructions. The interesting numbers are the baseline's
  adherence (what the model does unprompted) and everything in categories B and C.
- **An engineering-quality difference is an LLM's judgment of text.** Read it with the judge
  agreement (alpha and rank consistency), the per-judge contrasts, the leakage AUC (if judges
  can tell the conditions apart, their scores may reflect it), EQ-core (without the two
  overlapping dimensions), and the length measures. In every committed run so far the leakage
  AUC against the baseline is 1.00 (0.99 between the skill and the control, 0.83 between v2 and
  v1): judges can tell which plans followed a method, so no EQ difference here is blind in the
  sense that matters.
- **Hedges' g and Cliff's delta are within-prompt.** When plans of one condition vary little on a
  prompt (adherence near its ceiling), g becomes very large; read the raw difference and its
  interval first.
- **Intervals are across prompts.** With 5–8 prompts they are wide. A positive mean difference
  whose interval includes 0 is not a demonstrated effect.
- **The active control matters most for specificity.** `dfa` vs `generic-control` asks whether the
  effect is this method's or any long planning instruction's.
- **Outcomes are the only results not judged by a model.** They come from one task in one domain
  and say nothing about deployment, operations, or organizational work.

## Extending

- **A benchmark prompt:** add it to `benchmark.json` (or an experiment config) with a domain
  checklist under `rubrics/checklists/`. Author the checklist blind to the skill (the prompt and
  procedure in [`rubrics/AUTHORING.md`](rubrics/AUTHORING.md)), commit the authoring record, and
  let `check` verify the copy.
- **A condition:** add a skill directory under `conditions/` (with a README saying where it came
  from) and an entry in the config.
- **An agent or model:** a new provider in `dfa_eval/providers.py`, or a `command` provider config
  that installs the agent's adapter. Add the agent to [`agents.json`](agents.json); its evaluation
  status in [`../docs/evaluation-matrix.md`](../docs/evaluation-matrix.md) is computed from
  committed runs, never declared by hand.
- **An outcome task:** a new directory under `outcomes/tasks/` with a brief, starter code, hidden
  tests by category, reference solutions, and mutants, validated the way
  `tests/test_outcome_benchmark.py` validates `webhook-ledger`.

## Limitations

- **Same-family judges.** Every judge and every blind author so far is a Claude model, and in
  `pilot-models` the Sonnet 5.5 generator's plans were judged by Sonnet 5.5 itself.
- **Arm identifiability.** The probe's AUC against the baseline has been 1.00 in every committed
  run. Vocabulary neutralization does not hide the method; its structure survives blinding.
- **Judge disagreement in level.** Sonnet 5.5 and Fable 5 order plans similarly but differ by
  several points on the same plans (alpha −0.34 in the v1 re-judge, +0.12 in the core pilot), so
  absolute EQ values are judge-specific.
- **Small samples.** One to five runs per cell and five to eight prompts give wide intervals.
- **Rubric provenance.** The engineering-quality rubric and checklists were written by a model,
  not by domain experts, and have not been validated against expert judgment.
- **One outcome task**, at its ceiling for the implementer used, with four tests that check a
  status-code convention ([`outcomes/README.md`](outcomes/README.md)); single-shot
  implementation without running code; public hidden tests.
- **Prompt set.** Eight short, one-line requests. Real planning requests carry context the
  benchmark does not have.
- **Moving tools.** The Claude CLI updates itself: the v1.0.0 plans were generated with 2.1.287
  and the v2 runs with 2.1.288. A run records the CLI version when it starts, not per call.
- **Operator settings.** Every committed session ran with the operator's user settings (the
  `Concise` output style; permission mode `auto`, or `default` for Haiku 4.5, which in
  `pilot-models` refused every read of the skill's reference files, so that run's Haiku arm
  measures `SKILL.md` alone; `pilot-models-haiku` re-ran it with the files readable). A model the
  settings name as a fallback answered one judge call in the core pilot; the model check caught
  it.
