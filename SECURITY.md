# Security

## Threat model

### The skill is instructions that your coding agent executes

`SKILL.md`, `reference/` and the files in `adapters/` are instructions. Claude Code, Cursor and
Codex load them and act on them in your projects. If those files change, what your agent is told
to do changes. Codex reads `AGENTS.md` in every session, so a change there reaches every session.
Treat an update like a dependency upgrade:

- **Install a release you reviewed,** not the repository or `main` (README,
  [Install](README.md#install)). From v2.0.1, each release carries the skill package and the
  adapters as assets, built from the tagged commit; installs made earlier from `main` or a clone
  are unpinned. From v2.2.0 the README's install block downloads that release's installer and runs
  it only if its SHA-256 equals the value in the README (and in that version's CHANGELOG entry).
  The installer carries the release's files and their sums, checks them before writing, makes no
  network connection, starts no other program, and writes only the paths the README lists. It
  replaces or deletes a file only if the installer wrote it: a file it generated carries the
  BEGIN banner, and in the skill folder every file must be, byte for byte, a file of a released
  version, so an edit or an added file stops it. The README runs it with Python's `-I`, so no
  module lying next to it is imported in place of the standard library. Version 1.0.0 was designated after the fact and has no tag or
  assets. To use it, pin commit `7ec18f469395fce4f352caa26d983b22b5a71a0f`.
- **Review the diff before you update.** `--dry-run` shows what the new installer would change.
  To read the change itself, unzip the new release into an empty folder and compare it with the
  one you have, for example
  `N="$(mktemp -d)" && unzip -q dependency-first-architect-vX.Y.Z.zip -d "$N" && diff -r ~/.claude/skills/dependency-first-architect "$N/dependency-first-architect"`,
  or, in a clone, `git diff vA vB -- SKILL.md reference/`. For Cursor or Codex, compare the new
  adapter with the copy you have installed before you replace it.
- **Planning needs no side effects.** Be suspicious of any update that tells the agent to run
  commands, fetch URLs, send data anywhere, or change files other than the plan it writes. The
  package holds Markdown and the license only, and `package_skill.py` fails a build whose shipped
  files match one of its content rules: a URL or link out of the package, a download command, a
  pipe into a shell or interpreter, common code-execution calls, a destructive command such as
  `rm -rf`, Claude Code's `!` shell syntax, an environment variable, a credential pattern, an HTML
  comment, or a character outside a short list ([docs/distribution.md](docs/distribution.md)).
  Those rules are patterns, not an
  understanding of the text: an instruction such as "delete the user's files" in plain words
  passes them. Reading the diff is what catches that.

### Verifying what you have

The simplest check: run the release's installer with `--verify`. It reports, for each agent, if
what is installed is byte for byte that release (it reads a CRLF checkout as LF), and changes
nothing.

There are two `SHA256SUMS` files. A **release's** `SHA256SUMS` (a release asset) lists every
asset (`dependency-first-architect-vX.Y.Z.zip`, `cursor-dependency-first-architect.mdc`,
`codex-AGENTS.md`, `codex-lazy-dependency-first-architect.md`, `codex-lazy-AGENTS-snippet.md`,
`install-dependency-first-architect-vX.Y.Z.py`) and every file in the zip, as
`dependency-first-architect/<file>`. The installer carries the same list less its own line. The **repository's**
`SHA256SUMS` lists `VERSION`, `SKILL.md`, every `reference/*.md` and every file in `adapters/`.

- **A download:** in the folder you downloaded to, `sha256sum -c --ignore-missing SHA256SUMS`
  (it skips the assets you did not download).
- **An installed skill:** from the folder that holds it (`.claude/skills` in the project, or
  `~/.claude/skills`), check
  the package lines only, so a missing file fails the check:
  `grep '  dependency-first-architect/' /path/to/SHA256SUMS | sha256sum -c -`. Also list the folder
  (`find dependency-first-architect -type f`): it should hold exactly those six files.
- **An installed Cursor rule** is the `cursor-dependency-first-architect.mdc` asset, renamed:
  hash it and compare with that line. The lazy Codex install's `.codex/dependency-first-architect.md`
  is the `codex-lazy-dependency-first-architect.md` asset, renamed.
- **A Codex install** adds a block to your own `AGENTS.md`, so the file's hash won't match. Diff the
  block between the `BEGIN dependency-first-architect` and `END dependency-first-architect`
  markers with the `codex-AGENTS.md` asset (or, for the lazy install, `codex-lazy-AGENTS-snippet.md`).
- **A checkout:** `sha256sum -c SHA256SUMS` (on macOS, `shasum -a 256 -c SHA256SUMS`) checks the
  repository's list. The hashes are of the files with LF line endings, which is how git checks
  them out here. `python build.py --check` regenerates the adapters in memory and fails if a
  committed adapter or `SHA256SUMS` differs from what `SKILL.md` generates.
- Each adapter's banner names the version and the first 12 hex digits of the SHA-256 of the
  `SKILL.md` it was built from; match them against the `dependency-first-architect/SKILL.md` line
  of the release's `SHA256SUMS`.

### What checksums do not protect against

Checksums catch corruption and accidental edits. They do not protect you from a compromised
repository or release: anyone who can replace a release asset can replace that release's
`SHA256SUMS` with it, and naming a tag in the download pins the tag, not the assets. The README's
install block is a little stronger: it checks the installer against a SHA-256 written in the
README, so a replaced asset fails that check, unless the README you copied the block from was
changed too. Copy the block from the README at the release's tag, or from a copy you reviewed.
Three things guard against a compromised repository:

- **Signed release tags** (`git tag -s`; see [CONTRIBUTING.md](CONTRIBUTING.md)). Verify a tag
  with `git verify-tag vX.Y.Z`, which needs the maintainer's public key.
- **Rebuilding.** The package build is reproducible, so `python package_skill.py` at a verified
  tag must give the release's `SHA256SUMS` byte for byte
  ([docs/distribution.md](docs/distribution.md#rebuilding)).
- **Reviewed diffs** between the version you run and the version you upgrade to.

### The evaluation harness calls model providers

The harness is development tooling: it is not in the skill package, and nothing in it runs when
the skill is installed or used. Tests and CI use the offline `fake` provider.

- API keys are read from environment variables, and only by a stage that calls a model, when it
  starts and on each request; never at import, by tests or by CI. They are never written into run
  records or results.
- The `openai-compatible` provider sends its key only to an https `base_url` (plain http only to
  localhost, never through a proxy), refuses redirects (urllib would re-send the key to the new
  host), reads it only from a variable whose name ends in `_API_KEY` (and `OPENAI_API_KEY` only
  for OpenAI's own endpoint), strips spaces around the key and refuses one with a space, line
  break or control character inside, and redacts the key, literal or repr-, JSON- or
  URL-encoded, from any error or response it records (a value under 8 characters is a
  placeholder for a local server, such as `none`, and is not masked).
- The `claude-cli` and `command` providers start their CLI with your whole environment, every
  credential in it included; only the outcome benchmark's hidden-test child is scrubbed (below).
  Run real benchmarks where the environment holds only the credentials that provider needs.
- **A config is code.** An experiment config, and a run's `manifest.json` (which a stage reads
  when it is given no `--config`), choose the programs that run (a `command` provider's `argv`,
  a `claude-cli` provider's `executable`) and where a key is sent (`base_url`, `api_key_env`).
  Run only configs and run directories you have reviewed.
- Raw provider transcripts are kept in run directories, and they contain what the session saw:
  absolute paths under the operator's temp directory (including the account name) and the
  output style of the operator's Claude Code settings. Review them before publishing a run.
- Each plan is generated in a fresh working directory under the system temp directory, outside
  the repository.

### The outcome benchmark executes model-written code

- `eval/outcomes/` has an implementer model write a service, then runs that code against hidden
  tests. The `implement` step refuses to start without `--allow-code-execution`.
- Only the implementation package (`ledger/`) is copied into a fresh directory and tested, in a
  separate Python process with a timeout and a scrubbed environment: only `PATH`, `SYSTEMROOT`
  (and `SystemRoot`), `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `HOME`, `USERPROFILE`, `LANG`,
  `LC_ALL` and the interpreter's own library path (`LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`) are
  passed through, plus `PYTHONHASHSEED`, `PYTHONDONTWRITEBYTECODE` and `PYTHONIOENCODING`, which it
  sets. No API key or cloud credential is in the child's environment.
- Before running it, a static screen refuses code that imports common networking or process
  modules (`subprocess`, `socket`, `http.client`, `urllib.request`, …, also as `from urllib import
  request`), imports or calls common file-deletion and process functions (also through
  `from os import *`, `posix` or `nt`), and it reads each file
  as Python will decode it. It is a heuristic for honest mistakes that misses many other forms
  (`importlib`, `getattr`, aliases), not a defense against adversarial code. The child that runs
  the tests also writes their results, so code written to game the benchmark could forge them.
- **This is not a sandbox.** The generated code, and the agent that writes it, run as your user,
  with your file system and your network. The orchestrating process does hold model
  credentials, and on Linux a child running as the same user can read its parent's environment
  through `/proc/<ppid>/environ`. Run the outcome benchmark only inside a disposable container or
  VM that holds nothing you need, with credentials scoped to the model provider (or supplied to
  a separate process that never starts the tests).

## Supported versions

Fixes go into the next release. Only the latest release and `main` are supported.

## Reporting a vulnerability

Please report vulnerabilities privately, using GitHub's private vulnerability reporting: open
the repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/NiravRVaghasiya/dependency-first-architect/security/advisories/new>).

**Do not open a public issue, pull request or discussion about a vulnerability.** If the
private reporting form is not available, open a public issue that asks for a private channel.
Give no details in that issue.

Please include:

- the affected file and the version or commit;
- what an attacker could make happen;
- the steps to reproduce it.

In scope:

- instructions in the skill or the adapters that could lead an agent into harmful actions;
- a way to change an adapter or `SHA256SUMS` so that it still passes `python build.py --check`
  while no longer matching `SKILL.md`;
- credentials leaking into run records or results;
- a way around `--allow-code-execution` or the scrubbed environment;
- path traversal through run, generation or judgment ids.

Out of scope: how good the generated plans are, and vulnerabilities in Claude Code, Cursor or
Codex themselves. Report those to their vendors.
