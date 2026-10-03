# Active control: `architecture-planner`

A generic architecture-planning skill used as the **active control** condition
(`generic-control` in [`eval/benchmark.json`](../../benchmark.json)). It answers the question:
*is any difference between the skill and the bare baseline specific to Dependency-First
Architect, or would any long, careful planning instruction produce it?*

## How it was written

By an isolated model session that never saw this repository: `claude --bare -p`, model
`claude-opus-5-5`, effort `high`, all tools disabled, empty working directory, 2026-10-02. The
prompt asked for the best general instructions a principal engineer would give for producing an
architecture and build plan, explicitly *without* referencing or imitating any named methodology,
at a length comparable to what the treatment loads, and without any self-scoring.

The prompt, session metadata, and the verbatim output are in
[`authoring-session.json`](authoring-session.json). `SKILL.md`, `reference/document-template.md`
and `reference/guidance.md` are that output, unedited; `python eval/run.py check` fails if they
differ.

| Condition | Instruction files loaded | Words |
|---|---|---|
| `dfa` (non-AI prompt) | `SKILL.md`, `reference/plan-template.md`, `reference/validation.md` | ≈ 4,400 |
| `dfa` (AI prompt) | the above + `reference/ai-systems.md` | ≈ 5,000 |
| `generic-control` | `SKILL.md`, `reference/document-template.md`, `reference/guidance.md` | ≈ 5,500 |

(Words counted on the files at the time of writing; each run's manifest records the exact files
and hashes the sessions loaded.)

## What it is not

It is not a straw man and not a placebo: it is a strong, credible alternative. Its author was free
to recommend practices that overlap with Dependency-First Architect (it mentions end-to-end
paths, reversibility and exit criteria), and no overlap was edited out. A small difference
between `dfa` and `generic-control` is therefore a meaningful result, not a failure of the
control.
