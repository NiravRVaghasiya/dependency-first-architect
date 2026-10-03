# Plan: Autonomous GitHub Issue Triage and Resolution Agent

## 1. Goals and non-goals

**Goals**
- Triage every new issue: classify, deduplicate, label, prioritize, and ask for missing info.
- Resolve a safe subset automatically by opening a PR with a fix, tests, and an explanation.
- Escalate everything else to humans with useful context.

**Non-goals (v1)**
- Merging without human approval.
- Touching security-sensitive issues, release or CI config, or infrastructure code.
- Large refactors or feature design.

## 2. Architecture

```
GitHub webhook (issues.opened / edited / comment)
        │
   Event queue (dedupe, rate limit, idempotency keys)
        │
   Orchestrator (state machine, per-issue)
   ├── Triage stage
   ├── Reproduction stage
   ├── Fix stage (sandboxed coding agent)
   ├── Verification stage
   └── PR / handoff stage
        │
   Tools: GitHub API, repo index/search, sandbox runner, LLM
        │
   Audit log + metrics store
```

**Key components**
- **Orchestrator**: a durable per-issue state machine (`NEW → TRIAGED → NEEDS_INFO | REPRODUCING → FIXING → VERIFYING → PR_OPEN | ESCALATED`). State is persisted, so it can resume after failures.
- **Sandbox**: an ephemeral container with the repo cloned at the default branch. It has no secrets, network egress limited to package mirrors, and CPU, memory, and time limits.
- **Repo knowledge layer**: a code search index (symbols and embeddings), CODEOWNERS, CONTRIBUTING.md, past issues and PRs, and a per-repo config file (`.github/agent.yml`).
- **GitHub integration**: a GitHub App with least-privilege permissions. It can read contents and issues, write issues and PRs, and push only to `agent/*` branches.

## 3. Pipeline stages

### Stage 1: Triage
1. **Classify** as bug, feature, question, docs, support, spam, or security.
2. **Deduplicate** by embedding similarity against open and recently closed issues. If confidence is high, comment with a link and label `duplicate`. Don't auto-close in v1.
3. **Check completeness** against the issue template: version, steps to reproduce, expected and actual behavior, logs. If anything is missing, post a specific question and set `needs-info`. Re-check after 7 days and then ping the author.
4. **Label and prioritize** by component (from file paths and keywords), severity (crash, data loss, regression, cosmetic), and estimated effort.
5. **Route** by assigning the CODEOWNERS of the likely files.
6. **Decide on autonomy**: compute an *auto-fix eligibility* score (see §5).

### Stage 2: Reproduction
- Generate a failing test or script from the issue text and run it in the sandbox.
- If it can't reproduce after N attempts, post findings and escalate as `cannot-reproduce`. Don't guess at a fix.
- A failing test that captures the bug is required before any fix starts.

### Stage 3: Fix
- A coding agent loop: localize (search, stack traces, `git blame`), edit, run tests, iterate.
- Budget caps: max steps, wall-clock time, tokens, and files touched (e.g. ≤5 files, ≤200 changed lines).
- Follow the repo's style: run the linter and formatter, and read CONTRIBUTING.md.
- Abort and escalate if the fix needs a public API change, a dependency upgrade, a migration, or edits to protected paths.

### Stage 4: Verification
- The new reproduction test must fail before the fix and pass after.
- The full relevant test suite must pass, and the agent must not weaken or delete existing tests.
- Run static analysis, type checks, and a secret scan.
- Self-review pass: a separate LLM call critiques the diff for scope creep, missed edge cases, and unrelated changes.
- Optionally run a mutation or flakiness check by re-running the new test several times.

### Stage 5: PR and handoff
- Open a draft PR from `agent/issue-<n>` that includes `Fixes #n`, root-cause analysis, a summary of the change, test evidence, and known risks or uncertainty.
- Request review from the CODEOWNERS and label the PR `agent-authored`.
- Respond to review comments, with a cap of 3 rounds before escalating.
- A human always does the merge.

## 4. Safety and guardrails

| Risk | Mitigation |
|---|---|
| Prompt injection via issue text or comments | Treat issue content as untrusted data. Give the agent no secrets. Keep tool permissions fixed regardless of the text. Have the agent ignore instructions found in issues. |
| Malicious code execution | Ephemeral sandbox with no credentials and restricted egress. Never run fork or untrusted CI with privileged tokens. |
| Security vulnerabilities | Classifier routes suspected security issues to a private human channel. The agent doesn't comment publicly or attempt a fix. |
| Bad or over-broad fixes | Diff-size limits, protected-path denylist, required failing-then-passing test, human merge. |
| Spam or abuse loops | Per-repo and per-user rate limits. Ignore the agent's own comments and bots. Circuit breaker if the PR rejection rate spikes. |
| Cost runaway | Per-issue token and time budgets, plus a global daily cap. |
| Overconfident replies | The agent states its uncertainty and never claims unverified results. |

## 5. Eligibility and confidence gating

Auto-fix is attempted only if **all** of these hold:
- The issue is classified as a bug with confidence ≥ 0.8.
- It is reproduced by an automated test.
- The likely change is small and in non-protected paths.
- The repo has a runnable test suite that passes on the base branch.

Otherwise the agent triages only and escalates. Thresholds are configurable per repo.

## 6. Configuration (`.github/agent.yml`)

```yaml
mode: triage_only | suggest | autofix_draft_pr
protected_paths: ["infra/**", ".github/workflows/**", "**/auth/**"]
labels: {bug: ..., needs-info: ...}
limits: {max_files: 5, max_lines: 200, max_minutes: 30}
test_command: "make test"
escalation: {team: "@org/maintainers"}
```

## 7. Evaluation

- **Offline benchmark**: replay historical closed issues (and SWE-bench-style tasks from the repo's own history). Measure the following:
  - Triage label accuracy and duplicate precision/recall.
  - Reproduction rate.
  - Fix success, defined as hidden tests passing and matching the human fix's behavior.
- **Online metrics**:
  - Time to first response and time to triage.
  - PR acceptance rate, review rounds, and revert rate.
  - Maintainer-overridden labels.
  - Cost per resolved issue.
- **Shadow mode** first: the agent produces output privately and maintainers compare it to what they did.

## 8. Rollout phases

1. **Phase 0, shadow (2–3 wks)**: triage output goes to a private log only. Tune the classifier and dedupe.
2. **Phase 1, triage live**: labels, needs-info questions, and duplicate comments on one or two repos.
3. **Phase 2, suggest**: post a proposed root cause and patch as a comment or gist, with no PR.
4. **Phase 3, draft PRs**: auto-fix for eligible bugs, with human review.
5. **Phase 4, expand**: more repos and issue types, with the thresholds tuned from the metrics.

Move to the next phase only when the exit criteria are met. For example, label accuracy ≥90% and PR acceptance ≥50% with zero security incidents.

## 9. Tech choices (suggested)

- **Runtime**: a Python or TypeScript service on a queue (SQS, Pub/Sub, or Temporal for durable workflows).
- **LLM**: a strong coding model for the fix stage and a cheaper one for classification and dedupe.
- **Sandbox**: Docker with gVisor or Firecracker.
- **Storage**: Postgres for state and audit, and a vector store for issue and code embeddings.
- **Observability**: structured traces per issue, including every tool call and prompt, for debugging and audit.

## 10. Open questions

- Which repos and languages come first? That determines the sandbox images and test tooling.
- Is public commenting by a bot acceptable to the maintainers, or should output start as private suggestions?
- What are the compliance or licensing constraints on sending code to an external LLM?
- Who owns on-call for agent misbehavior, and how is it disabled (a kill switch per repo)?

If you tell me the target repo, language, and preferred stack, I can turn this into a concrete implementation breakdown with milestones and an initial prompt and tool design.
