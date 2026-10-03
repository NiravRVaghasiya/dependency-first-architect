# Condition `dfa-v1`: the skill exactly as v1.0.0 measured it

`SKILL.md` and `reference/` here are byte-for-byte the files at commit `3ba6b70`, the version
the v1.0.0 scorecard ([`examples/SCORECARD.md`](../../../examples/SCORECARD.md)) measured.
[`SHA256SUMS`](SHA256SUMS) records their hashes at that commit, and `python eval/run.py check`
fails if any file differs.

They exist so that v1 and v2 can be compared in the same harness, on the same prompts, models,
and judges. Two things differ between the versions at once, and a comparison cannot separate
them:

- the methodology (v2 adds dependency kinds, validation gates, labels, reversibility tiers,
  brownfield and exception rules, and a new output template);
- the evaluation conditions: v1's Step 8 loads `reference/evals.md` — the methodology-adherence
  rubric the judges use — and revises its plan until it self-scores at least 16/20. v2 never loads
  the rubric and prints no score.

So a v1-vs-v2 difference on the adherence rubric is confounded by v1 having that rubric in
context. The engineering-quality rubric was never in either version's context.

This directory is evaluation material, not an install target. Do not edit it.
