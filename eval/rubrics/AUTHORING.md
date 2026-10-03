# How the rubrics and checklists were authored

Two rubrics score every plan. They answer different questions, and they were written in
different ways on purpose.

| Rubric | Question | Written by | Scale |
|---|---|---|---|
| [`methodology-adherence-v1.json`](methodology-adherence-v1.json) | Does the plan follow the Dependency-First Architect method? | The skill's author (v1.0.0, unchanged) | 10 dimensions × 0–2 = /20 |
| [`engineering-quality-v1.json`](engineering-quality-v1.json) | Is the plan good engineering, whatever method produced it? | A model session that never saw the skill (below) | 11 dimensions × 0–4 = /44, plus a per-request checklist and traps |

## Methodology adherence: circular by design

The adherence rubric checks exactly what `SKILL.md` asks for. A plan written with the skill is
*expected* to score near 20/20; that shows the instructions were followed, not that the plan is
good. Its text is byte-identical to the v1.0.0 judge prompt in
[`examples/scoring/run_eval.py`](../../examples/scoring/run_eval.py), so scores stay comparable
with the published v1.0.0 measurement; `python eval/run.py check` fails if the two drift apart.

## Engineering quality: written blind to the skill

To keep the skill from defining its own yardstick, the engineering-quality rubric and the domain
checklists were written by isolated model sessions that had no access to `SKILL.md`,
`reference/`, `examples/` or `adapters/`:

- `claude --bare -p` (no CLAUDE.md, memory, hooks, or settings), all tools disabled, empty working
  directory, structured JSON output. Model `claude-opus-5-5`, effort `high`, 2026-10-02.
- The prompts named the 11 dimensions (chosen by the maintainer), asked for anchors and scoring
  rules that are **methodology-neutral** (no credit for structure, templates, section names or any
  named method's vocabulary), and asked for per-request checklists of domain considerations and
  traps.
- The full prompts, output schemas, session metadata (model actually used, cost, duration) and the
  verbatim structured output are in [`authoring/`](authoring/). The rubric dimensions, anchors,
  scoring rules and every checklist in [`checklists/`](checklists/) are copied from those records
  unchanged; `python eval/run.py check` fails if they differ.

What the maintainer added: the dimension names, the judge prompt template
(`judge_template`, which contains no methodology vocabulary), and the `overlaps_methodology` flag.

### Known overlap with the skill

Two of the 11 dimensions — **Validation quality** and **Decision reversibility** — are things that
v2 of the skill explicitly instructs (validation gates, reversibility tiers). They are flagged
`overlaps_methodology: true`, and every report also shows **EQ-core**, the total without them, so
a reader can see how much of any difference comes from those two.

### Limits of this provenance

- "Blind to the skill" means the authoring session could not read it. The author is still a
  Claude model, the same family as the generators and judges, so it may share their notion of
  what a good plan contains.
- The checklists are drafts by a model, not by domain experts. They have not been reviewed by
  practitioners in billing, CI/CD, healthcare, payments, or collaborative editing. Treat
  checklist coverage as provisional until they are.
- A rubric is not an outcome. Plan scores are judgments of text; see
  [`../outcomes/`](../outcomes/) for the outcome benchmark.
