# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The current version is in
[`VERSION`](VERSION). `python build.py --check` fails if this file has no entry for it, and
`python build.py --release-tag vX.Y.Z` fails unless that entry has a release date.

## [2.0.0] — Unreleased

Version 2 of the method:

- Work starts only once what blocks it is met, and a hypothesis counts as met only when its
  validation gate passes.
- The effort spent on a decision scales with how costly it is to get wrong.
- Budgets, labels and exceptions are explicit.
- Plans no longer grade themselves.

Plans written with v2 are structured differently from v1 plans: 10 sections, in a new order, with
no score section. This version also adds an evaluation harness that measures more than adherence
to the method. The measurements in `examples/` and `examples/SCORECARD.md` were made with the
v1.0.0 `SKILL.md`. They say nothing about v2.

### Changed

Methodology (`SKILL.md`, `reference/`):

- **Principle 1: what blocks work.** Work never starts before what blocks it is met. "It runs"
  is not validation: anything that rests on an untested hypothesis (fast, safe, correct or cheap
  enough) is met only when its validation gate passes. Until then, dependents may be scaffolded,
  but not specified or hardened.
- **Dependencies are not only code.** There are seven dependency kinds: structural, runtime,
  decision, validation, risk/security, organizational and economic. For each kind, the skill
  states what it blocks (being built, deployed, specified, exposed, or scaled or committed to)
  and when it counts as met. An organizational item blocks the step that needs it. Organizational
  items start first because they have lead time.
- **Validation gates.** Each gate has five fields:
  - hypothesis;
  - method;
  - acceptance threshold;
  - evidence: the artifact the check will produce, never a result;
  - unlocks: what may proceed, and what happens if the gate fails.

  Gates are numbered V0, V1, …, and only these get one:
  - V0, the walking skeleton;
  - R3 decisions whose default rests on an assumption;
  - validation and economic dependencies whose failure would change the plan;
  - the controls that guard exposure of real users, data or money (one must guard the worst
    failure);
  - irreversible operations such as a cutover, a decommissioning or deleting data.

  A check whose failure would change nothing downstream stays a plain exit check.
- **Labels.** Every budget target and every threshold is labeled:
  - REQUIREMENT: stated in the request, a contract or a law;
  - BASELINE: measured on the existing system;
  - ASSUMPTION: a starting value with its basis;
  - UNKNOWN: no number, but the missing input, who supplies it, and the phase that needs it.

  A gate cannot pass on an UNKNOWN threshold.
- **Reversibility tiers.** Each default is tiered by the cost of being wrong, not only by the
  cost of undoing it:
  - R1 (hours to days): a default only, on one line.
  - R2 (weeks, a data migration, or a change coordinated across teams): an assumption and a cheap
    check, passed before dependents are hardened.
  - R3 (months of rework, customer-visible breakage, or a contractual or legal commitment): a
    gate passed before dependents are specified, plus a flip condition. When a requirement fixes
    the default, or it sits far inside known limits, a cited basis replaces the gate.
- **Tradeoff gates** were Decision → Default → Flip condition. They are now Decision →
  Reversibility → Default → Assumption → Validated by → Flip condition. A data-privacy boundary
  is always considered when there is personal or regulated data. Brownfield work adds four
  decisions: migration strategy, coexistence and cutover, rollback, and the system of record.
- **Principle 4: blast radius** now orders exposure as well as rework. Rework still runs widest
  first. Exposure runs smallest first: a rollout, a migration wave or a cutover reaches a canary,
  one site or one tenant first.
- **Walking skeleton.** It is now also rolled back once through the pipeline, and the plan says
  who can reach it (internal or allow-listed traffic until its controls exist). For software you
  do not operate, such as a CLI, a library or an app, production means the released artifact,
  installed the way users install it. Its exit check is V0.
- **Principle 8: budgets.** Latency, throughput, availability/SLO, RPO/RTO, resource use, AI
  inference cost, storage cost and operational complexity, each labeled.
- **Principle 9: methodology exceptions.** A rule that does not fit is bypassed openly, through a
  numbered exception (E1, E2, …). Each exception has five fields: rule bypassed, why it does not
  apply, replacement validation, evidence required, and resumes when. An exception may change the
  order or timing of the work. It never exposes real users, data or money before their controls
  pass. The walking skeleton and V0 are never N/A.
- **Brownfield and migrations** are part of the normal method, not an exception:
  - the skeleton is the thinnest production path of the new capability through the existing
    system, with the old path as the fallback;
  - Phase 0 maps what depends on the system and baselines the live path;
  - each phase names the system of record and its rollback;
  - the point of no return gets a gate.
- **Small builds** (one team, no R3 decision, no exposure to outside users, money or regulated
  data) get three phases or fewer, and one-line sections wherever nothing binds.
- **Procedure: 8 → 10 steps,** one for each output section:
  1. Classification and constraints.
  2. Dependency map (new).
  3. Tradeoff gates.
  4. Walking skeleton.
  5. Phases.
  6. Validation gates (new).
  7. Cross-cutting concerns.
  8. AI layer.
  9. Exceptions and deferred work.
  10. A private self-check, then the plan.
- **No visible self-score.** A plan no longer prints a score, a rubric, or a claim that it
  follows the method. `reference/evals.md` is no longer loaded while planning. For a regulated
  system, the plan maps controls to obligations but never claims compliance.
- **Claude Code-only instructions.** `SKILL.md` marks the instructions that load reference files
  as claude-code-only. In Claude Code, the plan template is loaded when the procedure starts, the
  AI-layer detail at Step 1 for AI systems, and the worked examples at Step 2. The adapters leave
  these instructions out (see below).
- **`reference/plan-template.md`** follows the 10 steps:
  1. Classification and constraints, with a labeled Budgets table and the missing inputs.
  2. Dependency map: only the dependencies that bind and that the phase order does not show.
  3. Tradeoff gates, with Reversibility, Assumption and Validated by columns. R1 defaults and
     N/A decisions take one line each.
  4. Walking skeleton (Phase 0): who can reach it, and the V0 exit check.
  5. Phases, each with its rollback or its point of no return.
  6. Validation gates: the ID, the five fields, and the phase that runs the gate.
  7. Cross-cutting concerns.
  8. AI layer: one row per sublayer, or one N/A line for a system with no AI component.
  9. Methodology exceptions, numbered.
  10. Deliberately deferred.

  The Eval score section is gone.
- **`reference/ai-systems.md`.** An exit check becomes a validation gate when later work depends
  on it. Typical gated checks are injection containment, retrieval quality, and cost per resolved
  conversation (an economic dependency, validated on a canary). Other changes:
  - injection defense is designed for bounded impact and closes exfiltration paths;
  - an approval is bound to the exact action and enforced by the tool executor;
  - retrieval enforces the caller's access;
  - exit checks state their thresholds;
  - fine-tuning is usually R2, and training on customer or personal data is R3.
- **`reference/evals.md`** is renamed the methodology-adherence rubric (v1). It now says what it
  measures (whether the method was followed) and what it does not (whether the plan is good
  engineering). It also warns that comparing v1 and v2 on this rubric is confounded: v1 plans
  were written with the rubric in context, and v2 plans are not. Its ten dimensions and their 0–2
  scoring are unchanged. The old ship/revise/re-plan thresholds are now adherence bands.

Adapters and build:

- The adapters are regenerated from the v2 `SKILL.md`. They leave out its claude-code-only spans,
  because the adapters do not ship `reference/`. `build.py` fails if those markers are unbalanced
  or nested.
- Each adapter banner now names the version, and the SHA-256 (first 12 hex digits) of the
  `SKILL.md` it was built from. It also says that the reference files are not bundled. The BEGIN
  line and the END marker are unchanged, so an installed Codex block can still be replaced between
  them.
- `python build.py --check` now requires three section names to survive into both adapters:
  "Tradeoff gates", "Validation gates" and "Methodology exceptions" (v1 checked only the first).
  It also fails if `SHA256SUMS` is stale, or if this changelog has no entry for `VERSION`.
- The frontmatter parser no longer stops at a `---` inside a value. It also keeps values that
  continue on indented lines (v1 dropped those lines). The Cursor description is quoted whenever
  plain YAML would misread it.
- **Codex lazy install** (`adapters/codex/`): a ~1 KB `AGENTS-snippet.md` block, and
  `dependency-first-architect.md`, which holds the whole skill with the plan template, the
  worked examples and the AI-layer detail appended. Codex reads the full file only when a request
  matches, instead of carrying ~16 KB in every session. Where an appendix disagrees with the
  method, the method wins, as in Claude Code. It has not been tested in Codex yet.
- The skill packaged for an evaluation run contains exactly the files its `SKILL.md` names; the
  harness no longer special-cases any file.

### Added

- **`reference/validation.md`:** worked examples for the rules in `SKILL.md`: dependency kinds,
  validation gates, labels, reversibility, budgets and methodology exceptions. It adds no rules
  of its own.
- **Evaluation harness** (`eval/`, run with `python eval/run.py`; standard library only):
  - Stages: generate → blind → judge → probe → lint → aggregate → report.
  - Providers: `claude-cli`, `openai-compatible`, `command` (evaluates the Cursor and Codex
    adapters as installed instruction files), and an offline `fake` provider for tests and CI.
  - Each run directory keeps:
    - a manifest (the config, the skill version and file hashes, the repo commit, the platform);
    - every raw provider output;
    - blinded copies of the plans, with a sealed key;
    - schema-validated judgments;
    - a leakage probe, which asks whether a judge can tell which plans followed a method;
    - a deterministic check of each plan's structure;
    - `summary.json`: descriptive statistics, t and cluster-bootstrap confidence intervals,
      effect sizes (paired d_z, and Hedges' g and Cliff's delta computed within prompts), judge
      agreement (Krippendorff's α and rank consistency), per-judge contrasts, leakage AUC per
      judge and generator, length and cost;
    - a snapshot of the rubrics it was judged with;
    - a `REPORT.md` generated from `summary.json`.
  - Generation guards: a plan is recorded as an error, not scored, when the CLI substituted
    another model, when a skill condition's skill was not offered to the model, or when the
    session could also see other installed skills (contamination).
  - `python eval/run.py check` re-validates every committed run: every judgment and probe
    against its re-parsed raw output, every lint record against a fresh lint, the run's rubric
    copies against the hashes its judgments recorded, raw outputs against the records that
    explain them, and outcome attempts against their per-test rows and code snapshots. It
    recomputes every summary, report, the matrix and the README evidence block, rejects result
    files outside a committed run, and checks P1–P5 in every experiment config and run.
  - Retries never overwrite: a replaced record moves to `superseded/` with its raw output and
    still counts in the cost cap and the summary's cost.
  - Headline judge metrics are complete-case (a plan counts only if every configured judge
    scored it), and judge and probe records carry the SHA-256 of the blind copy they saw.
  - A skill session may read the skill's files whatever its permission mode (`--add-dir`); a
    skill plan whose every read of the skill's files failed is recorded as an error, not as a
    treatment plan. Summaries flag such plans in earlier runs, and the output style and
    permission mode each session reported (user settings still apply under `--bare`); the
    matrix does not count a run whose skill plans could not read the skill as an evaluation.
  - `python eval/run.py import-v1` re-judges the v1.0.0 example plans, byte-for-byte as
    generated, with the v2 judges and rubrics.
  - `python eval/run.py evidence` writes the evidence block in `README.md` from the committed
    runs, and `check` fails if the block is stale.
- **Experiments** (`eval/experiments/`): the exact configs of the committed runs.
  - `smoke`: P1, baseline and dfa, one run each.
  - `pilot-core`: P1–P8 × baseline, dfa, dfa-v1 and generic-control × 5 runs, Opus 5.5; judged
    by Sonnet 5.5 on both rubrics and by Fable 5 on engineering quality.
  - `pilot-length`: P1–P5 with every plan capped at 1,500 words, 3 runs.
  - `pilot-models`: Sonnet 5.5 and Haiku 4.5 as generators, 3 runs.
  - `pilot-models-haiku`: the Haiku 4.5 cells again, after `pilot-models` showed that Haiku's
    sessions could not read the skill's reference files.
- **The v1.0.0 skill as a condition** (`eval/conditions/dfa-v1.0.0/`): the exact files at commit
  3ba6b70, which the v1.0.0 scorecard measured, with their checksums, so v1 and v2 can be
  compared in one run. (At 7ec18f4 only the baseline paragraph of `reference/evals.md` differs.)
- **Benchmark `core-v2`** (`eval/benchmark.json`):
  - 8 prompts: P1–P5 from v1.0.0 verbatim, plus P6 hospital scheduling cloud migration, P7
    photo-renaming CLI and P8 card fraud detection.
  - 5 conditions: baseline, dfa, generic-control, and baseline and dfa capped at 1,500 words.
  - 5 runs per cell, scored by two judge models, with a cost cap.
- **Active control** (`eval/conditions/generic-architect/`): a generic architecture-planning
  skill of comparable length. It was written by an isolated model session that never saw this
  repository.
- **Rubrics** (`eval/rubrics/`):
  - `methodology-adherence-v1`: the v1.0.0 /20 rubric, unchanged.
  - `engineering-quality-v1`: 11 dimensions, each scored 0–4. The maintainer chose the dimension
    names. The anchors and scoring rules were written by a model session that could not read the
    skill, and they give no credit for any method's vocabulary. The two dimensions that overlap
    the method are flagged and left out of the EQ-core total.
  - Per-prompt checklists of domain considerations and traps for P1–P8.
  - The authoring sessions the rubric and checklists were copied from (`eval/rubrics/authoring/`,
    `AUTHORING.md`).
- **Outcome benchmark** (`eval/outcomes/`) asks whether a plan changes what gets built:
  - The task is `webhook-ledger`, a payment-webhook ledger service described in a product brief.
  - A planner model writes a plan from the brief.
  - An implementer model then builds the service with no plan, with a baseline plan, with a dfa
    plan, or with a plan from the active-control skill.
  - Hidden tests score functional behavior, idempotency, ordering, failure injection, security
    and money handling.
  - A change request (partial refunds) follows, and the round-1 tests are rerun as regression
    tests.
  - Reference solutions and single-defect mutants check that the tests tell good code from bad.
  - Model-written code runs only with `--allow-code-execution`. Only the implementation package
    is copied out and tested, after a static screen, in a subprocess with a scrubbed environment
    and a timeout. This is not a sandbox (see `SECURITY.md`).
  - A run pins the task's files and the runner (`outcomes/task-files.json`); `implement` and
    `check` refuse a run whose task has changed since, so results from two test versions cannot
    mix, and `implement` refuses a config other than the one the run was planned with. Each run
    keeps a copy of the runner it used (`outcomes/runner.py`), which `check` verifies, so the
    shared runner can evolve. The summary lists which hidden tests failed, per arm, and the
    planner's and implementer's session settings.
  - The test child keeps the interpreter's library path (`LD_LIBRARY_PATH`,
    `DYLD_LIBRARY_PATH`): a Python built as a shared library, such as actions/setup-python's on
    Linux, could not start without it, which failed every outcome test in CI on Ubuntu.
- **Evaluation matrix** (`docs/evaluation-matrix.md`), generated from `eval/agents.json` and the
  committed runs. Each agent and model is marked Supported, Tested or Experimentally evaluated,
  following definitions stated in the matrix.
- **Provenance and release:**
  - `VERSION`.
  - `SHA256SUMS` for `VERSION`, `SKILL.md`, `reference/*.md` and every adapter file, checked
    with `sha256sum -c SHA256SUMS`.
  - `python build.py --release-tag vX.Y.Z`.
  - This changelog, `SECURITY.md` and `CONTRIBUTING.md`.
  - CI now also runs `sha256sum -c SHA256SUMS` and `python eval/run.py check`, plus the release
    check on tag pushes.

### Removed

- The self-score. In v1.0.0, Step 8 scored every plan /20 against `reference/evals.md` and
  revised any plan that scored below 16, and the plan template ended with an "Eval score"
  section. Both are gone.
- `reference/evals.md` from the skill's reference files. The skill no longer loads it.

## [1.0.0] — 2026-10-02

Retroactively designated: the state at commit 7ec18f4 (untagged).

- `SKILL.md`: seven core principles and an 8-step procedure that ends in a /20 self-score
  against `reference/evals.md`.
- `reference/`: the plan template, the AI-layer detail, the rubric with its test prompts, and
  the layer-map analogy.
- Cursor and Codex adapters, generated from `SKILL.md` by `build.py`. CI fails if they drift.
- `examples/`: the five fixed prompts answered with and without the skill in Claude Code, one run
  per arm. Each plan was blind-scored by three judges. Includes `SCORECARD.md` and, in
  `examples/scoring/`, the raw scores, the tally script and the harness.
- MIT license.

[2.0.0]: https://github.com/NiravRVaghasiya/dependency-first-architect/compare/7ec18f469395fce4f352caa26d983b22b5a71a0f...HEAD
[1.0.0]: https://github.com/NiravRVaghasiya/dependency-first-architect/tree/7ec18f469395fce4f352caa26d983b22b5a71a0f
