# Contributing

## Development setup

- You need Python 3.9 or newer. There are no dependencies to install: the build, the checks, the
  tests and the evaluation harness use only the standard library.
- `.gitattributes` gives every file LF line endings on every OS. Generated files and
  `SHA256SUMS` depend on that.
- You do not need a model to develop. The tests and CI use only the offline `fake` provider and
  never call a model. Running the benchmark against real models needs the `claude` CLI, or an
  OpenAI-compatible endpoint with its key in an environment variable, and it costs money.

## The checks

CI runs all of these on Ubuntu and on Windows. Run them before you open a pull request:

```bash
python build.py --check                    # adapters and SHA256SUMS match SKILL.md; CHANGELOG.md has an entry for VERSION
sha256sum -c SHA256SUMS                    # the listed files match their checksums (macOS: shasum -a 256 -c SHA256SUMS)
python examples/scoring/tally.py --check   # the v1.0.0 scorecard matches its raw judge scores
python eval/run.py check                   # committed eval runs: schemas, summaries, reports, matrix, rubric provenance, README evidence block
python -m unittest discover -s tests -v    # self-tests; offline, temp dirs only
```

After you commit a new run under `eval/results/`, regenerate what is derived from the runs:
`python eval/run.py matrix` (the evaluation matrix) and `python eval/run.py evidence` (the
evidence block in `README.md`). `check` fails until both match. Then update by hand what `check`
does not compare: the summary table in the Evidence section of `README.md` and the figures in its
Limits section.

## Rules

1. **`SKILL.md` is canonical.** Change the method there and in `reference/`. Never put a change
   only in `adapters/`. The adapters do not ship `reference/`, so put any instruction that only
   Claude Code can follow, such as loading a reference file, between `<!-- claude-code-only -->`
   and `<!-- /claude-code-only -->`. `build.py` leaves those spans out of the adapters.
2. **Never hand-edit `adapters/` or `SHA256SUMS`.** After any change to `SKILL.md`, `VERSION` or
   `reference/*.md`, run `python build.py` and commit what it regenerates. `--check` fails until
   you do.
3. **Every methodology change needs a CHANGELOG entry and a version bump.** "Methodology change"
   means any change to `SKILL.md` or `reference/`. Bump `VERSION`, then describe the change under
   `## [X.Y.Z] — Unreleased` in `CHANGELOG.md`, saying exactly what changed. Choose the bump by
   its effect on plans:
   - **MAJOR:** the plan's shape or the procedure changes (sections, steps or required fields),
     so plans from different versions can't be compared on structure.
   - **MINOR:** guidance is added or tightened, and the plan's shape stays the same.
   - **PATCH:** wording fixes that should leave plans unchanged.
4. **Claims policy.** `README.md` and `docs/` may state only results that exist as committed
   runs: a run under `eval/results/`, which `python eval/run.py check` verifies, or the v1.0.0
   measurement in `examples/`.
   - Quote numbers from the generated summary and report, with their intervals and caveats.
   - Name the rubric a number comes from. Methodology adherence is not engineering quality, and
     neither is an outcome.
   - Never cite an uncommitted, partial or hand-picked run.
   - No "proves", "best" or "beats".
   - Agent status labels follow the definitions in `docs/evaluation-matrix.md`.
5. **Standard library only, Python 3.9+.** Tests never touch the network or a real model, and
   they write only to temp directories.

## Adding an adapter

1. **`build.py`:** add a `build_<agent>(name, description, body, lines)` function next to
   `build_cursor` and `build_codex`, and add its output path to `render()`. Every file that
   `render()` returns is automatically compared against the committed copy by `--check` and
   listed in `SHA256SUMS`. If the file carries the whole method, add it to `FULL_ADAPTERS` too,
   so that the sentinels are checked in it (a stub that only points at another file, like the
   lazy Codex `AGENTS-snippet.md`, is not).

   If users append the adapter to a file of their own, keep the BEGIN line and the END marker
   so that an installed copy can be replaced.
2. **`tests/test_build.py`:** add the file to `FILES` and `GENERATED`, and add a test for its
   format.
3. **`eval/agents.json`:** add the agent (`agent`, `artifact`, `install`, `notes`), then
   regenerate the matrix with `python eval/run.py matrix`. A new agent is listed as
   **Supported**.
4. **`README.md`:** add the install command, character for character as in `eval/agents.json`
   (`tests/test_matrix.py` checks this).
5. **Moving past Supported:** run the benchmark with a `command` provider that installs the
   adapter file verbatim (`options.adapter_file`, `options.instructions_path`; see
   `eval/README.md`), then commit the run.

## Adding a benchmark prompt

1. **Add the prompt to `eval/benchmark.json`** with `id`, `title`, `kind` (`AI` or `non-AI`),
   `request`, `checklist` and `tags`. Keep P1–P5 byte-identical to `reference/evals.md`.
2. **Write its checklist blind to the skill**, as described in `eval/rubrics/AUTHORING.md`. Use an
   isolated session that cannot read `SKILL.md`, `reference/`, `examples/` or `adapters/`.
   - Commit the prompt, the output schema, the session metadata and the verbatim output under
     `eval/rubrics/authoring/`.
   - Copy the checklist, unchanged, to `eval/rubrics/checklists/<id>.json`, with a matching
     `prompt_id`.

   `python eval/run.py check` fails if the copy differs from the recorded output.
3. **Start a new run directory.** A changed benchmark is a new experiment. `generate` refuses
   to resume a run whose config hash has changed.

## Adding an outcome task

1. **Create `eval/outcomes/tasks/<id>/`** with:
   - **`task.json`** (`dfa-eval/outcome-task@1`): the planner request, the implementer prompt and
     the change-request prompt, the category of each hidden test module, the modules for each
     round, and the test timeout.
   - **`brief.md`:** facts about the environment. Never the implications that the hidden tests
     check. Every tested behavior must follow unambiguously from the brief.
   - **`starter/`:** the provided environment and the API stubs the implementer starts from.
   - **`hidden_tests/`:** one module per category.
   - **`CHANGE_REQUEST.md`:** the round-2 change.
   - **`reference/round1/` and `reference/round2/`:** solutions. Round 1 passes every round-1
     module and fails the change request. Round 2 passes everything.
   - **`mutants/<name>/`:** copies of the round-1 solution, each with exactly one defect.
   - **`mutants/README.md`:** maps each mutant to the category it targets.
2. **Add tests** (see `tests/test_outcome_benchmark.py`):
   - the reference solutions pass;
   - the starter fails;
   - every mutant fails its target category and passes every functional test.
3. **Point `eval/outcomes/outcomes.json`**, or a new config, at the task.

## Release process

1. **Set `VERSION` to the release version**, if it does not hold it already.
2. **Update `CHANGELOG.md`:** replace `Unreleased` with the release date
   (`## [X.Y.Z] — YYYY-MM-DD`), and point the version's link at the tag
   (`.../compare/<previous>...vX.Y.Z`).
3. **Regenerate and commit:** run `python build.py` (the adapter banners carry the version),
   then commit.
4. **Run every check above, plus the release check:**
   ```bash
   python build.py --release-tag vX.Y.Z
   ```
5. **Create a signed tag and push it.** Signing needs a key configured for git. CI runs the
   release check again on the tag.
   ```bash
   git tag -s vX.Y.Z -m "vX.Y.Z"
   git push origin main vX.Y.Z
   ```
6. **Publish the release** with `SHA256SUMS` attached, using the version's CHANGELOG section as
   the release notes:
   ```bash
   awk -v v="X.Y.Z" 'index($0, "## [" v "]") == 1 {on = 1; next} /^## \[/ {on = 0} on' CHANGELOG.md > release-notes.md
   gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --notes-file release-notes.md SHA256SUMS
   ```
