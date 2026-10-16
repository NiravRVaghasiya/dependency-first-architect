# Distribution: the skill package and the release assets

Users install the skill, not this repository. The repository is the development project: about
4,200 files, most of them evaluation records. A release ships six files for Claude Code and other
agents that load a skill folder, and one generated file per Cursor or standard Codex install (two
for the lazy Codex install).

## What a release holds

`python package_skill.py` builds `dist/` from the checked-out sources:

```text
dist/
├── dependency-first-architect/          the skill package: the folder an agent installs
│   ├── SKILL.md                         the method (canonical; byte for byte as committed)
│   ├── LICENSE                          MIT: copies must carry the notice
│   └── reference/
│       ├── plan-template.md             the exact output format; loaded before planning
│       ├── validation.md                worked examples; loaded at Step 2
│       ├── ai-systems.md                the AI layer; loaded for AI or agentic systems
│       └── layer-map.md                 the teaching analogy; SKILL.md lists it, no step loads it
└── release/                             what a GitHub release publishes, as flat assets
    ├── dependency-first-architect-vX.Y.Z.zip   the package folder, zipped (the folder at the root)
    ├── cursor-dependency-first-architect.mdc   Cursor rule (= adapters/cursor-…mdc)
    ├── codex-AGENTS.md                         Codex block (= adapters/AGENTS.md)
    ├── codex-lazy-dependency-first-architect.md   lazy Codex install (= adapters/codex/…)
    ├── codex-lazy-AGENTS-snippet.md               its AGENTS.md pointer
    └── SHA256SUMS                              every asset, and every file in the zip
```

The package is exactly `PACKAGE_FILES` in `package_skill.py`, an explicit allowlist. The
reference files are the four that `SKILL.md` names, which are also what the evaluation harness
packaged for the measured v2.0.0 skill (`eval/dfa_eval/workspace.py`). `layer-map.md` is
teaching material that no step loads, and it ships because `SKILL.md` lists it among the files
to load on demand, and leaving it out would make that list point at a missing file.

Nothing else ships in the package: no `README.md`, `CHANGELOG.md`, `VERSION`, `SHA256SUMS`,
`build.py`, `package_skill.py`, `adapters/`, `eval/`, `examples/`, `tests/`, `docs/`,
`.github/`, `reference/evals.md` (the v1 scoring rubric, for maintainers), or caches. The adapters
are separate assets because each agent takes a different format, and none of them needs the
others. The version is in the zip's name. To check which version an installed folder came from,
compare its files with the `dependency-first-architect/…` lines of a release's `SHA256SUMS`.

## How it is built and checked

`python package_skill.py` (standard library only; no shell, no network, no generated code). It
checks everything in memory first, writes nothing if any check fails, and exits 1 with one line per
problem:

1. **Allowlist.** Each file in `PACKAGE_FILES` exists and is a regular file inside the
   repository, reached without a symbolic link or (on Windows) a junction.
2. **Frontmatter.** `SKILL.md` starts with frontmatter that holds exactly `name`, `description`
   and `tags`, each on one line, with values YAML reads as text (details below), and `build.py`
   reads the same name and description from it.
3. **Method.** The 16 headings that carry the method are present: core principles, dependency
   kinds, validation gates, brownfield, methodology exceptions, the procedure, and Steps 1–10.
4. **References.** Every `reference/…` path, and every link destination outside code blocks
   (inline, whatever its text holds; a definition, also in a quote or list; an autolink such as
   `<vscode:…>` or an email address; an HTML `href`/`src`), names a file in the package; a link
   with a scheme (a URL, `mailto:`, an editor deep link, a drive letter) or one that leaves the
   package folder fails, and `SKILL.md` names every reference file that ships. Paths resolve the
   same way on every OS.
5. **Content rules,** over every shipped file and adapter: only Markdown and `LICENSE`, UTF-8,
   no `#!` line, only the characters in `TEXT_CHARSET` (printable ASCII, tab, line feed and
   § ± · × – — … → ≠ ≥; so no Unicode tag characters, zero-width or bidirectional controls, or
   no-break spaces), no HTML comment but `build.py`'s two markers (an unclosed `<!--`, `<!…>` and
   `<?…>` count; adapters keep their banner), no Markdown link definition (`[//]: # (…)` is a
   common way to hide a comment), and no match for any rule in `CONTENT_RULES`: a URL or `www.`
   address; a download command
   (`curl`, `wget`, `Invoke-WebRequest`, `iwr`, `git clone`, …); a pipe into a shell or
   interpreter, or `bash -c`, `python -c`, `powershell -enc` and the like; code execution
   (`eval`, `exec`, `subprocess`, `os.system`, `execSync`, script tags and `on…=` handlers);
   a destructive command (`rm -rf`, `Remove-Item -Recurse`, `rmdir /s`);
   Claude Code's `` !`command` `` and ```` ```! ```` shell syntax; an environment variable
   (`$NAME`, `${name}`, `$env:NAME`, `%name%`, `os.environ`, `getenv`, `*_API_KEY`); or a
   credential pattern (AWS, GitHub, GitLab, Slack, Google, Stripe, Hugging Face, npm and `sk-`
   keys, JSON Web Tokens, bearer tokens, private keys, and a key-like name given a value with a
   digit in it). The only exception is the repository URL in the adapters' provenance banner.
   These are patterns: an instruction such as "delete the user's files" in plain words passes
   them, so every change to what ships still needs a review.
6. **Adapters.** The adapters `build.py` renders from these sources equal the committed ones.
7. **Zip.** It holds exactly the package files under the folder `dependency-first-architect/`, as
   plain files with mode 0644 and safe names, and each reads back byte-identical.
8. **File count.** The package holds 6 files; the limit is 49, counting hidden and nested ones.
9. **On disk.** After writing, the output directory holds exactly what was built: no extra,
   missing, changed or linked file or folder.

**Reproducible.** Files are written with LF line endings, as git stores them. The zip is stored
(not compressed, since deflate output differs between zlib builds), with sorted entries, a fixed
1980-01-01 timestamp, and Unix mode 0644 on every OS. The same sources give the same bytes, and
`tests/test_package.py` checks it. To check a release, check out its tag, run
`python package_skill.py`, and compare `dist/release/SHA256SUMS` with the release's.

**Safe to rerun.** The only thing it deletes is its own earlier output: a directory that is empty
or holds the `.package_skill-output` marker, holds no link, and holds nothing a build does not
make (so `--out ~/.claude/skills` never deletes another skill installed there later; the
`.DS_Store`, `Thumbs.db` and `desktop.ini` files an operating system adds are deleted with the
rest). It deletes the marker last, so a
file held open elsewhere leaves a directory the next build still recognizes. It refuses any other
directory, a symbolic link or junction, the repository, its parents, and the home directory.

CI runs it on Ubuntu and Windows. `.github/workflows/release.yml` runs it on a release tag,
with every other CI check (on Ubuntu; the tag's CI run covers Windows), and uploads the assets as
a workflow artifact. Publishing stays manual
(see [CONTRIBUTING.md](../CONTRIBUTING.md#release-process)).

## Frontmatter

```yaml
---
name: dependency-first-architect
description: Use when a user asks to plan, architect, sequence, or order the building or migration of any software, infrastructure, or AI/agentic system, new or already running. Produces a BUILD PLAN ordered by dependency and blast radius — not by what is most visible or exciting. …
tags: [architecture, planning, dependencies, validation, sequencing, walking-skeleton, risk-management, migration, ai-systems]
---
```

- `name`: lowercase letters, digits and single hyphens, at most 64 characters, no reserved word
  (`anthropic`, `claude`), and the package folder's name.
- `description`: what the skill does and when to use it, at most 1,024 characters, with no `<`
  or `>`, written as one plain YAML line. A folded `>-` block, quotes, comments and continuation
  lines are refused, because that is where YAML parsers and the line-based readers some skill
  hosts use start to disagree. PyYAML and `build.py` read the same 503-character value.
- `tags`: a one-line list of 1–10 lowercase hyphenated tags, one space after each comma.
- No value may be one YAML reads as a boolean, null, number or date (`yes`, `off`, `null`,
  `2026`, `2026-10-16`).

**Compatibility.** The submission platform this package targets requires `tags`. `tags` is not
one of the fields the Agent Skills format defines (`name`, `description`, `license`,
`allowed-tools`, `metadata`, `compatibility`). Claude Code ignores frontmatter keys it does not
use. Validators that allow only those fields reject it. Anthropic's skill-creator plugin
(`scripts/quick_validate.py`) reports "Unexpected key(s) in SKILL.md frontmatter: tags" for this
package, and passes it once `tags` is removed. A host that applies the same rule to uploads would
reject this zip; that has not been tested. For such a host, the frontmatter would have to change:
dropping `tags`, or moving them under `metadata`, also means changing `FRONTMATTER_KEYS` and
`read_frontmatter()` in `package_skill.py`, which require `tags` and refuse other keys today. A
user can instead delete the `tags` line from an installed copy, after checking the download; the
edited `SKILL.md` then no longer matches its line in the release's `SHA256SUMS`.

## Review of a skill-submission scan

A skill-submission scan flagged the items below, from the whole repository. It reported them by
keyword, so each was traced to its source line. The `eval` matches came from two files that ship
(`reference/ai-systems.md` and `reference/validation.md`) and were reworded; every other finding
was in files that do not ship. The validator itself was not available to rerun. `package_skill.py`'s content rules
cover its categories for everything that ships, and `tests/test_package.py` plants examples of
each category to show that the rules fire, and ordinary planning prose to show that it passes.

| Reported | What it is | Verdict | Resolution |
|---|---|---|---|
| Dangerous code: `curl` in `README.md` | The install commands downloaded the Cursor and Codex adapters from `main` with `curl -fsSL … -o file`, and one appended the download to `AGENTS.md`, which Codex obeys in every session. No pipe into a shell, but unpinned and unchecked instructions for an agent. | Real supply-chain weakness; not remote code execution | The README is not in the package. It now installs a release asset of a stated version into an empty folder, checked against that release's `SHA256SUMS`, with no `curl`, `wget` or raw-content URL, and no clone into the skills folder (`tests/test_matrix.py` checks this). |
| Dangerous code: `eval` in the Codex adapter | The word "eval", as in "eval suite" and "eval set" (model evaluation), in the appendices `build.py` copies from `reference/ai-systems.md` and `reference/validation.md`. No code. | False positive | Reworded to "evaluation" in the 7 places (no change in meaning). The adapters are separate assets, not part of the skill package. |
| Dangerous code: `eval(repo_root` in `eval/dfa_eval/cli.py` | The function `_load_run_eval(repo_root)`, not the built-in `eval`. It did, however, execute `examples/scoring/run_eval.py` with `importlib` (`exec_module`) on every `eval/run.py check`, only to read three string constants. | Keyword false positive; the execution was real, but of a fixed repository file | Replaced by `_v1_harness_constants()`, which parses the file with `ast` and accepts only string literals (or a list of them) assigned once at top level, and no other binding of those names anywhere (a star import included), so nothing runs. Tests show it equals the imported values and refuses anything else. Not in the package. |
| Exposed secret: the GitHub URL in `build.py` | `REPO_URL = "https://github.com/NiravRVaghasiya/dependency-first-architect"`: a public repository URL, with no user, token or query, printed in the adapters' banner so an installed copy can be traced. | False positive | Kept. A scan of every tracked file, `eval/results/` included, and of all 14 commits on every branch, the public `main` included, found no credential. Nothing to rotate. Not in the package. |
| Environment variables: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | The names of the variables the optional benchmark uses: in `eval/README.md`, the `examples/scoring/run_eval.py` docstring, `eval/outcomes/runner.py` (in the list of variables it strips) and `providers.py` (the default name). No values. | Not a leak; real hardening gaps in the optional benchmark | The `openai-compatible` provider now sends its key only to https (or localhost, never through a proxy), never follows a redirect, reads only a `*_API_KEY` variable and `OPENAI_API_KEY` only for OpenAI's endpoint, refuses keys a header cannot carry, and redacts the key from what it records. `eval/README.md` documents when keys are read, where they go, and the costs. Nothing in the package reads a variable. |
| Network access: URLs | Documentation links (badges, README, SECURITY, CHANGELOG). The only network code is in the optional benchmark: the providers call the `claude` CLI, another agent CLI, or a Chat Completions endpoint (default `https://api.openai.com/v1`), and only when a stage that calls a model runs. The tests, `build.py`, `package_skill.py` and the other CI checks make no network call. | Links: not network access. Benchmark calls: by design, opt-in | The package holds no URL. The benchmark's endpoints and when they are called are documented in `eval/README.md`. |
| More than 50 files | The repository holds about 4,200 files. | Real | The package holds 6, and the build fails at 50 or more. |

## Rebuilding

The build is reproducible, so a rebuild at a verified tag is an independent check of a release:

```bash
git clone https://github.com/NiravRVaghasiya/dependency-first-architect dfa-source
cd dfa-source
git verify-tag vX.Y.Z         # needs the maintainer's public key
git checkout vX.Y.Z           # the release to reproduce (from v2.0.1 on)
python package_skill.py       # writes dist/; Python 3.9+, nothing to install
gh release download vX.Y.Z --repo NiravRVaghasiya/dependency-first-architect --dir published
diff -r published dist/release && echo "every asset of the release matches its tag"
```
