# Committed runs

Each directory is one self-contained run of the harness: its config and provenance
(`manifest.json`), every plan exactly as the model returned it, every judgment and probe with the
provider's raw output, the blind copies and key, lint results, the rubric files it was judged
with, records a retry replaced (`superseded/`), and the generated `summary.json` and `REPORT.md`.
Outcome runs also hold `outcomes/` (one record per attempt, the code after each round, and the
pinned task files) and `OUTCOMES.md`.

`python eval/run.py check` (run in CI) validates every record here, re-derives every judgment and
probe from its raw provider output, recomputes every lint record, summary and report
byte-for-byte, and checks outcome attempts against their test rows and code snapshots, so the
numbers in a report cannot drift from the records under it.

| Run | What it asks | Design | Recorded cost |
|---|---|---|---:|
| [`2026-10-03-v1-examples-rejudge`](2026-10-03-v1-examples-rejudge/REPORT.md) | How do the published v1.0.0 plans score with judges and a rubric they were not written for? | The 10 plans in `examples/`, imported byte-for-byte; Sonnet 5.5 on both rubrics, Fable 5 on engineering quality | $7.38 |
| [`2026-10-03-pilot-length`](2026-10-03-pilot-length/REPORT.md) | Does the difference survive a length cap on both arms? | P1–P5 × capped baseline and capped skill × 3 runs; Opus 5.5 plans, Sonnet 5.5 judge | $7.45 |
| [`2026-10-03-pilot-models`](2026-10-03-pilot-models/REPORT.md) | Does it carry over to smaller models? | P1–P5 × baseline and skill × 3 runs, for Sonnet 5.5 and Haiku 4.5; Sonnet 5.5 judge (it also wrote the Sonnet plans). Haiku 4.5's sessions could not read the skill's reference files (flagged), so its arm measures `SKILL.md` alone | $11.45 |
| [`2026-10-03-pilot-models-haiku`](2026-10-03-pilot-models-haiku/REPORT.md) | The Haiku 4.5 cells again, with the skill's files readable | As pilot-models, Haiku 4.5 only, after the harness passed `--add-dir` for the skill | $5.27 |
| [`2026-10-03-pilot-core`](2026-10-03-pilot-core/REPORT.md) | Against the bare model, v1.0.0 and an active control, on 8 prompts | P1–P8 × baseline, skill, v1.0.0 skill, generic control × 5 runs; Opus 5.5 plans; Sonnet 5.5 on both rubrics, Fable 5 on engineering quality | $199.16 |
| [`2026-10-03-outcomes-webhook-ledger`](2026-10-03-outcomes-webhook-ledger/OUTCOMES.md) | Does the plan change the code built from it? | No plan, baseline plan, skill plan, control plan × 5 attempts; Opus 5.5 plans, Sonnet 5.5 implementer, hidden tests and a change request | $16.66 |

Every run's `manifest.json` holds its exact config. The generated runs' configs are also in
[`../experiments/`](../experiments/) and [`../outcomes/outcomes.json`](../outcomes/outcomes.json);
the re-judge's is built by `python eval/run.py import-v1`. A smoke run of
`eval/experiments/smoke.json` ($1.78) tested the harness before these runs and is not committed.

Start with a run's `REPORT.md` (or `OUTCOMES.md`); [`../README.md`](../README.md) explains what
each measure does and does not show.
