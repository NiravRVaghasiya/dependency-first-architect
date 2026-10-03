# Security

## Threat model

### The skill is instructions that your coding agent executes

`SKILL.md`, `reference/` and the files in `adapters/` are instructions. Claude Code, Cursor and
Codex load them and act on them in your projects. If those files change, what your agent is told
to do changes. Codex reads `AGENTS.md` in every session, so a change there reaches every session.
Treat an update like a dependency upgrade:

- **Review the diff before you update.** For a Claude Code install, fetch first and read what
  changed before you pull:
  ```bash
  git -C ~/.claude/skills/dependency-first-architect fetch
  git -C ~/.claude/skills/dependency-first-architect diff HEAD origin/main -- SKILL.md reference/
  ```
  For Cursor or Codex, compare the new adapter with the copy you have installed before you
  replace it.
- **Pin a tag or a commit** rather than tracking `main`. For example, use
  `git clone --branch vX.Y.Z ...`, or put a tag or a full commit SHA in place of `main` in the
  `raw.githubusercontent.com` install URLs. Version 1.0.0 was designated after the fact and has
  no tag. To use it, pin commit `7ec18f469395fce4f352caa26d983b22b5a71a0f`.
- **Planning needs no side effects.** Be suspicious of any update that tells the agent to run
  commands, fetch URLs, send data anywhere, or change files other than the plan it writes.

### Verifying what you have

- The adapters are generated from `SKILL.md` by `build.py`. `python build.py --check` rebuilds
  them in memory. It fails if a committed adapter or `SHA256SUMS` differs from what `SKILL.md`
  generates.
- `SHA256SUMS` lists the SHA-256 of `VERSION`, `SKILL.md`, every `reference/*.md` and every
  adapter file, including the two files of the Codex lazy install. To verify a checkout, run
  `sha256sum -c SHA256SUMS` (on macOS, `shasum -a 256 -c SHA256SUMS`). The hashes are of the
  files with LF line endings, which is how git checks them out in this repository.
- Each adapter's banner names the version and the first 12 hex digits of the SHA-256 of the
  `SKILL.md` it was built from. You can match those against the `SKILL.md` line in the release's
  `SHA256SUMS`.
  - An installed Cursor rule is the adapter file, unchanged. Hash it and compare with the
    `adapters/cursor-dependency-first-architect.mdc` line. The same goes for the lazy Codex
    install's `.codex/dependency-first-architect.md` and the
    `adapters/codex/dependency-first-architect.md` line.
  - The Codex install appends to your own `AGENTS.md`, so the file's hash won't match. Instead,
    diff the block between the `BEGIN dependency-first-architect` and
    `END dependency-first-architect` markers with the release's `adapters/AGENTS.md` (or, for the
    lazy install, `adapters/codex/AGENTS-snippet.md`).

### What checksums do not protect against

Checksums catch corruption and accidental edits. They do not protect you from a compromised
repository or release, because anyone who can change the files can also change `SHA256SUMS`.
Two things guard against that:

- **Signed release tags** (`git tag -s`; see [CONTRIBUTING.md](CONTRIBUTING.md)). Verify a tag
  with `git verify-tag vX.Y.Z`, which needs the maintainer's public key.
- **Reviewed diffs** between the version you run and the version you upgrade to.

### The evaluation harness calls model providers

- API keys are read from environment variables. They are never written into run records or
  results.
- Raw provider transcripts are kept in run directories, and they contain what the session saw:
  absolute paths under the operator's temp directory (including the account name) and the
  output style of the operator's Claude Code settings. Review them before publishing a run.
- Each plan is generated in a fresh working directory under the system temp directory, outside
  the repository.

### The outcome benchmark executes model-written code

- `eval/outcomes/` has an implementer model write a service, then runs that code against hidden
  tests. The `implement` step refuses to start without `--allow-code-execution`.
- Only the implementation package (`ledger/`) is copied into a fresh directory and tested, in a
  separate Python process with a timeout and a scrubbed environment: only `PATH`, `SYSTEMROOT`,
  `WINDIR`, `COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `HOME`, `USERPROFILE`, `LANG`, `LC_ALL` and the
  interpreter's own library path (`LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`) are passed through, so
  no API key or cloud credential is in the child's environment.
- Before running it, a static screen refuses code that imports common networking or process
  modules (`subprocess`, `socket`, `http.client`, `urllib.request`, …) or calls common
  file-deletion and process functions. It is a heuristic for honest mistakes that misses many
  other forms, not a defense against adversarial code.
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
