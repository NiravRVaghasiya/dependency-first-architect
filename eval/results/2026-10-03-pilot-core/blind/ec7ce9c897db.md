# Plan: An Autonomous GitHub Issue Triage and Resolution Agent

## 1. Goals and non-goals

**Goals**
- Reply to new issues within minutes: classify them, label them, find duplicates and ask for missing information.
- Open **draft PRs** that fix the issues where it's likely to succeed: small bugs, docs fixes, simple dependency bumps.
- Lighten maintainers' workload without making them review bad PRs.

**Non-goals (v1)**
- Merging code on its own.
- Large features, architecture changes, or issues involving security or private disclosures.
- Changing CI/CD, secrets or release workflows.

## 2. High-level architecture

```
GitHub App (webhooks + scheduled sweep)
        │
        ▼
  Event Router ──► Job Queue ──► Orchestrator (per-issue state machine)
                                     │
        ┌────────────────┬───────────┼──────────────┬────────────────┐
        ▼                ▼           ▼              ▼                ▼
   Triage Agent    Dedup Service  Resolvability  Resolver Agent   Feedback Handler
   (classify,      (embeddings    Gate           (sandboxed       (/agent commands,
    label, ask)     over issues)  (go/no-go)      repro→fix→test)  PR review comments)
                                                     │
                                                     ▼
                                          Ephemeral sandbox container
                                          (repo clone, no secrets,
                                           restricted network)
```

**Components**
- **GitHub App:** Uses scoped permissions: read/write on issues and PRs, write on contents only for `agent/*` branches, and no access to workflows or admin settings.
- **Orchestrator:** Keeps a durable state machine per issue in Postgres. Each event is processed only once, so retries are safe.
- **LLM layer:** Uses a tool-calling model. A cheaper model handles classification and a stronger model handles code changes.
- **Per-repo config** (`.github/issue-agent.yml`): label taxonomy, areas where the agent may or may not make changes, test commands, autonomy level, and limits on cost and number of PRs.

## 3. Issue lifecycle (state machine)

```
NEW → TRIAGED → { NEEDS_INFO | DUPLICATE | WONT_ATTEMPT | QUEUED_FOR_FIX }
QUEUED_FOR_FIX → REPRODUCING → FIXING → VERIFYING → PR_OPEN → { MERGED | REVISING | ABANDONED }
```
Any state can move to `ESCALATED` (hand off to a human with a summary).

## 4. Stage details

### 4.1 Triage (runs on every issue)
1. **Normalize:** Parse the title, body, templates, stack traces, versions and attachments.
2. **Classify:** Type (bug, feature, question, docs, support, spam), severity, component (from file and path hints plus CODEOWNERS), and confidence.
3. **Dedup:** Run an embedding search over open and recently closed issues, then have the LLM confirm the match. Above a threshold, it comments with a link to the likely duplicate. It never auto-closes in v1.
4. **Completeness check:** If repro steps, versions or logs are missing, post *one* specific follow-up question. Don't use a generic template.
5. **Route:** Apply labels, suggest an assignee from CODEOWNERS, and request a reviewer for high severity.
6. **Output:** A structured JSON record kept for audit and evaluation, plus one concise comment that says it came from the bot.

### 4.2 Resolvability gate
The gate scores each issue on whether a fix is worth attempting. It goes ahead only if:
- the type is a bug or docs issue,
- the repro is clear or a stack trace points to code,
- the estimated change touches fewer than about 3 files and is in an allowed area,
- the repo has a working test command,
- the issue has no security or breaking-change labels,
- the issue is under budget.

Otherwise it marks the issue `WONT_ATTEMPT` and gives a reason, which is useful data in itself. In early phases, a maintainer label such as `agent:fix` is also required.

### 4.3 Resolver loop (in a sandbox)
1. **Setup:** Create a fresh container, shallow-clone the repo at the default branch and install dependencies from a cached image.
2. **Localize:** Search the code (ripgrep), map stack-trace frames to files, check `git log`/blame on suspect files, and read the related tests.
3. **Reproduce first:** Write a failing test, or a repro script for docs or behavior issues. If it can't reproduce the bug, it stops and comments with what it tried.
4. **Fix:** Make the smallest change that turns the failing test green.
5. **Verify:** Run the full or affected test suite, the linter, the type checker and the formatter. If anything fails, go back to the fix step, up to N iterations or the token/time limit.
6. **Self-review:** A separate review pass checks the diff for scope creep, unrelated edits, deleted tests, weakened assertions and hard-coded special cases.
7. **Open a draft PR** on `agent/issue-<n>` with:
   - root-cause analysis,
   - what changed and why,
   - test evidence,
   - risks and confidence,
   - `Fixes #n`.

### 4.4 Feedback handling
- Maintainers can use `/agent revise <instructions>`, `/agent abandon` and `/agent retry`.
- PR review comments go back into the resolver loop.
- The agent never force-pushes over human commits.

## 5. Safety and guardrails (the most important section)

| Risk | Mitigation |
|---|---|
| **Prompt injection via issue text** | Treat all issue and comment content as untrusted data, never as instructions. The tools available don't depend on what the issue says. The sandbox has no credentials, and outbound network access is limited to package registries. |
| Leaking secrets | No secrets inside the sandbox. A separate service holding the GitHub token pushes the branch and opens the PR. Diffs and logs are scanned for secrets before posting. |
| Malicious repro code | Runs in an ephemeral container (e.g. gVisor/Firecracker) with CPU, memory and time limits, and is destroyed after the job. |
| Bad or overreaching changes | Path allow and deny lists (e.g. no `.github/workflows`, lockfiles only on explicit request), a maximum diff size, deleting tests is forbidden, PRs are always drafts and merging is always done by a human. |
| Spam or cost runaway | Per-repo, per-day and per-issue limits on tokens, runs and PRs; per-user rate limits; a global kill switch. |
| Noise | Comment budget (max 1 triage comment plus 1 follow-up per issue), confidence thresholds, and an `agent:ignore` label to opt out. |
| Accountability | Full audit log: prompts, tool calls, diffs and decisions, linked to the issue. |

## 6. Tooling the agent gets
- `search_code`, `read_file`, `list_dir`, `git_log`, `git_blame`
- `edit_file` (patch-based), `run_command` (allowlisted: test, lint, build)
- `search_issues` (dedup), `get_issue_thread`
- `post_comment`, `add_labels`, `open_draft_pr`: these run through the orchestrator, never directly from the sandbox.

## 7. Evaluation
- **Offline benchmark:** Mine your own closed issues that have linked merged PRs. Check out the parent commit, run the agent, and judge it by the hidden tests from the real PR (SWE-bench style). Run this on every prompt or model change.
- **Triage metrics:** Label precision and recall against what maintainers later relabel, duplicate precision, and how often the follow-up questions get useful answers.
- **Resolution metrics:** Repro success rate, PR acceptance (merged without major rewrite), time to merge, revert rate, and cost per merged PR.
- **Health metrics:** Maintainer override rate, `agent:ignore` usage, and survey or 👍/👎 reactions on bot comments.

## 8. Rollout phases
1. **Shadow (2–4 weeks):** Triage and fix attempts run, but results go only to an internal dashboard. Compare them with what humans actually did.
2. **Triage live:** Labels, duplicate suggestions and info requests are posted. No PRs yet.
3. **Opt-in fixes:** Draft PRs only when a maintainer adds `agent:fix`.
4. **Auto-attempt:** The gate picks issues on its own within the per-repo limits. Expand the allowed issue types based on measured acceptance rates.

## 9. Suggested build order
1. GitHub App, webhook router, queue, state machine and audit log
2. Triage agent, dedup index and repo config
3. Sandbox runner with the allowlisted tools
4. Resolver loop (repro → fix → verify → self-review) and draft PR
5. Offline eval harness, built alongside step 4 rather than after
6. Feedback commands, dashboards, limits and kill switch

## 10. Open questions
- Which repos or languages come first? This affects sandbox images and test commands.
- Is it one org-wide GitHub App, or installed per repo?
- Model and hosting limits: self-hosted versus API, and data-residency rules for private code.
- Who owns the agent's PRs for review, and what service level is expected?
- What acceptance rate counts as success before moving to phase 4?

This is a general plan: I didn't look at any code or repos in the working directory. If you share the target repo(s) or stack, I can make it concrete, with config schemas, prompts for each stage and a sandbox image spec.
