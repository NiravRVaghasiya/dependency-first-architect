# Plan: Autonomous GitHub Issue Triage and Resolution Agent

## 1. Goals and non-goals

**Goals**
- Label, deduplicate, prioritize, and route new issues within minutes.
- Open high-quality draft PRs for issues that are well-specified and low-risk.
- Ask clarifying questions when an issue is underspecified.

**Non-goals (v1)**
- Merging without human review.
- Touching security-sensitive issues, release or CI config, or secrets.
- Large architectural changes.

## 2. Architecture

```
GitHub webhook (issues, issue_comment, PR review, CI status)
        │
   Event queue (dedupe, rate limit, per-issue lock)
        │
   Orchestrator (state machine per issue)
   ├── Triage stage
   ├── Reproduction stage
   ├── Fix stage
   ├── Verification stage
   └── PR / handoff stage
        │
   Tools: GitHub API, sandboxed repo checkout, code search,
          test runner, linter, embeddings index of issues/docs
        │
   Store: issue state, run logs, metrics (Postgres + object storage)
```

- **Trigger:** a GitHub App, which gives fine-grained permissions and a bot identity. It is more auditable than a PAT.
- **Execution:** each run happens in an ephemeral container with no network access except an allowlist (package mirror, GitHub API, LLM endpoint) and no production secrets.
- **State machine per issue:** `new → triaged → needs-info | reproducing → fixing → verifying → pr-open → done | escalated`. It is persisted so runs can resume and be audited.

## 3. Pipeline stages

### Stage 1: Triage
1. Classify the issue as bug, feature, question, docs, or support. Mark spam or invalid issues.
2. Search for duplicates using embeddings over past issues and PRs. If confidence is high, comment with a link and label it `duplicate`. Never auto-close in v1.
3. Assign labels for component, severity, and difficulty, plus an **agent-eligibility** score.
4. Check completeness: version, repro steps, expected vs. actual behavior. If missing, post a templated clarifying question and set `needs-info`.
5. Suggest an owner from CODEOWNERS and recent git blame.

### Stage 2: Eligibility gate
The agent only proceeds to a fix if all of these hold:
- The issue is a bug, docs change, or small enhancement.
- The issue is not labeled security, breaking-change, or `no-bot`.
- The estimated change is small (e.g., ≤ ~5 files, ≤ ~200 lines).
- The area has test coverage, or a test can be written.
- The confidence score is above a threshold.

Otherwise it escalates with a summary of findings: relevant files, a suspected root cause, and a suggested approach.

### Stage 3: Reproduction
- Build the environment from the repo's documented setup (CI config, Dockerfile, or devcontainer).
- Write a **failing test** that captures the bug. If it can't reproduce the bug within a budget, it comments with what it tried and moves to `needs-info`.

### Stage 4: Fix
- Explore with code search, then plan before editing.
- Make the smallest change that turns the failing test green.
- Follow repo conventions (lint, formatting, CONTRIBUTING.md, commit style).
- Enforce budgets on steps, tokens, wall-clock time, and files touched.

### Stage 5: Verification
- Run the full relevant test suite, linter, type checker, and build.
- Self-review the diff against a checklist: scope creep, unrelated changes, removed tests, weakened assertions, and hardcoded special cases.
- Fail closed. If tests regress or the diff touches forbidden paths (`.github/workflows`, lockfiles, auth code), abort and escalate.

### Stage 6: PR and handoff
- Open a **draft PR** from a bot-owned branch (`agent/issue-123-short-slug`).
- The PR body includes the issue link, root cause, the fix, the tests added, what was and wasn't verified, and confidence level.
- Respond to review comments and CI failures for up to N iterations, then hand off to a human.
- A human always does the final approval and merge.

## 4. Safety and guardrails

| Risk | Mitigation |
|---|---|
| Prompt injection via issue text or comments | Treat all issue content as untrusted data. The agent has no tools that can exfiltrate data, and the network is allowlisted. Sanitize content, and never let issue text alter the agent's permissions or policies. |
| Credential leakage | Short-lived, scoped installation tokens. No secrets in the sandbox. Scan diffs for secrets before pushing. |
| Harmful or wrong changes | Draft PRs only, required human review, protected branches, and a path denylist. |
| Runaway cost or loops | Per-issue and per-day budgets, a max number of iterations, and a global kill switch. |
| Spam or noise | One comment per state transition, edit the existing bot comment instead of adding new ones, and rate limit per repo. |
| Transparency | Bot label on all actions, a clear "automated" disclosure, and a link to the run log. |

## 5. Human-in-the-loop controls
- Per-repo config file (`.github/issue-agent.yml`) for enabled stages, label taxonomy, path allow/denylists, thresholds, and budgets.
- Slash commands in comments: `/agent triage`, `/agent fix`, `/agent stop`, `/agent explain`.
- A maintainer can override any label or decision, and the agent respects that by not re-applying removed labels.
- An escalation queue, which could be a `needs-human` label plus a Slack digest.

## 6. Evaluation

**Offline, before launch**
- Triage: build a labeled set of ~500 historical issues and measure label accuracy, duplicate precision and recall, and priority agreement.
- Fixing: replay closed bug issues at their pre-fix commit and check whether the agent's patch passes the real fix's tests. Use SWE-bench-style harnesses, plus internal repo-specific cases.

**Online, after launch**
- PR acceptance rate, and the share merged with minimal edits.
- Time to first response and time to resolution.
- Maintainer override rate on labels.
- False-positive duplicate rate.
- Cost per resolved issue.
- Regression rate: reverts or follow-up bugs tied to agent PRs.

Set promotion criteria for each autonomy level, for example:
- **Advance from shadow mode to live triage:** label accuracy ≥ 90%.
- **Advance from draft PRs to anything more:** acceptance ≥ 50% and zero reverts over a sustained period.

## 7. Rollout phases

1. **Shadow mode (2–3 wks):** the agent runs on incoming issues and logs what it would do, without posting. Compare against maintainers' actions.
2. **Triage only (2–3 wks):** it applies labels, posts duplicate suggestions, and asks clarifying questions.
3. **Fix with draft PRs (4+ wks):** limited to docs and small bugs in one or two pilot repos, with a maintainer opted in.
4. **Expand:** more repos and issue types, iterate on prompts and tools based on metrics, and add review-comment handling.

## 8. Tech choices (suggested)
- **Agent runtime:** Claude via the Claude Agent SDK, with a tool set limited to read, edit, bash-in-sandbox, and the GitHub API.
- **Infrastructure:** a webhook service (Node or Python) with a queue (SQS or Redis), and containers run on Kubernetes or Firecracker-style sandboxes.
- **Search:** a vector index (pgvector) for issues, docs, and code chunks, plus ripgrep for code.
- **Observability:** structured traces of every tool call and decision per run, with a dashboard and replay ability.

## 9. Open questions
- Which repos and languages are in scope first?
- What's the maintainers' tolerance for bot comments on public issues?
- Are there compliance constraints on sending code to an external LLM?
- Who owns on-call for the agent, and what's the budget?

If you tell me the repo stack and team size, I can turn this into a concrete milestone plan with the config schema and prompts for the triage stage.
