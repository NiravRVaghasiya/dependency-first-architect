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
python package_skill.py                    # dist/: the skill package, release assets and installer, built and checked
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
   only in `adapters/`. An adapter is a single file, so put any instruction that only Claude Code
   can follow, such as loading a reference file, between `<!-- claude-code-only -->` and
   `<!-- /claude-code-only -->`. `build.py` leaves those spans out of the adapters. The Cursor
   rule and the lazy Codex file then append the reference files (`APPENDED_REFERENCES`).
2. **Never hand-edit `adapters/` or `SHA256SUMS`.** After any change to `SKILL.md`, `VERSION` or
   `reference/*.md`, run `python build.py` and commit what it regenerates. `--check` fails until
   you do. After a `VERSION` change, also run `python eval/run.py matrix` (the matrix lists the
   version); `eval/run.py check` fails until you do.
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
6. **The package is an allowlist.** Users get exactly `PACKAGE_FILES` in `package_skill.py`
   ([docs/distribution.md](docs/distribution.md)). If `SKILL.md` names a new reference file, add
   it there; the build fails until you do, and fails if a listed reference is never named. What
   ships is instructions only, and the build's content rules (listed in docs/distribution.md)
   refuse the usual forms of URLs, commands, code, environment variables, credentials and hidden
   text. They are patterns, so review every change to what ships as well. If a rule misreads
   ordinary prose (the word "eval"), reword the prose; do not weaken the rule.

## Adding an adapter

1. **`build.py`:** add a `build_<agent>(name, description, body, lines)` function next to
   `build_cursor` and `build_codex`, and add its output path to `render()`. Every file that
   `render()` returns is automatically compared against the committed copy by `--check` and
   listed in `SHA256SUMS`. If the file carries the whole method, add it to `FULL_ADAPTERS` too,
   so that the sentinels are checked in it (a stub that only points at another file, like the
   lazy Codex `AGENTS-snippet.md`, is not).

   If users append the adapter to a file of their own, keep the BEGIN line and the END marker
   so that an installed copy can be replaced. The installer finds its blocks and files by them.

   **`package_skill.py`:** give the file a release asset name in `ADAPTER_ASSETS`. Adapters are
   release assets of their own, never part of the skill package.
2. **`tests/test_build.py`:** add the file to `FILES` and `GENERATED`, and add a test for its
   format.
3. **`eval/agents.json`:** add the agent (`agent`, `artifact`, `install`, `notes`), then
   regenerate the matrix with `python eval/run.py matrix`. A new agent is listed as
   **Supported**.
4. **The installer and `README.md`:** teach `installer/install.py` the agent's paths, add the
   asset to `INSTALLER_PAYLOAD` in `package_skill.py` and to `PAYLOAD_NAMES` in the installer,
   and test it in `tests/test_installer.py`. Add the agent's `--agent` line to the README
   character for character as in `eval/agents.json` (`tests/test_matrix.py` checks this).
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
3. **Point the README's install commands at the version**: the installer name and download URL in
   both install blocks, every zip name, release link and `git checkout vX.Y.Z`. Run
   `python build.py` first (the adapter banners carry the version), then `python package_skill.py`.
   Put the installer's SHA-256, which it prints on its `Installer SHA-256` line, in both blocks and
   as `Installer SHA-256: <hex>` in the version's CHANGELOG entry. Update `eval/agents.json` to
   match. The installer's bytes depend on `SKILL.md`, `reference/`, `LICENSE`, the adapters,
   `VERSION`, `installer/install.py` and `installer/released-package-sums.txt`, so take the hash
   after the last change to any of them. The README always installs the newest
   version whose CHANGELOG entry has a release date (pre-releases aside; until the first dated
   release from 2.0.1 on, `VERSION`), so this belongs in the release commit, not in the commit
   that bumps `VERSION`; until then it keeps naming the last release.
   `tests/test_matrix.py` checks both files against the CHANGELOG.
4. **Regenerate and commit:** run `python eval/run.py matrix`, then commit. After the release
   is published, add its `dependency-first-architect/` lines from `SHA256SUMS`, under a
   `# vX.Y.Z` heading, to `installer/released-package-sums.txt`, so later installers recognize
   that version's skill folder (`tests/test_installer.py` checks that every dated release is
   there).
5. **Run every check above, plus the release check:**
   ```bash
   python build.py --release-tag vX.Y.Z
   python package_skill.py
   ```
6. **Create a signed tag and push it.** Signing needs a key configured for git. On the tag, CI
   runs the release check again, and the `Release package` workflow
   (`.github/workflows/release.yml`) builds and checks the assets and uploads them as a workflow
   artifact. Nothing is published yet.
   ```bash
   git tag -s vX.Y.Z -m "vX.Y.Z"
   git push origin main vX.Y.Z
   ```
7. **Publish the release.** Either run the `Release package` workflow by hand with the tag and
   `draft_release` ticked, which creates a draft release with the assets and the version's
   CHANGELOG section as notes (add required reviewers to the `release` environment in the
   repository settings to gate that job), or build and upload locally:
   ```bash
   python package_skill.py
   awk -v v="X.Y.Z" 'index($0, "## [" v "]") == 1 {on = 1; next} /^## \[/ {on = 0} on' CHANGELOG.md > release-notes.md
   gh release create vX.Y.Z --draft --verify-tag --title "vX.Y.Z" --notes-file release-notes.md dist/release/*
   ```
   Before publishing the draft, check that its `SHA256SUMS` equals `dist/release/SHA256SUMS` from
   a build at the tag, and that the zip holds only `dependency-first-architect/` and its 6 files.
   Once it is published, run the README's install block in an empty project to confirm the
   download URL and checksum work.
