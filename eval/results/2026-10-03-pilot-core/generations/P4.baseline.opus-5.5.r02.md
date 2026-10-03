# Plan: An Autonomous GitHub Issue Triage and Resolution Agent

## 1. Goals and boundaries

**Goals**
- Triage every new issue within minutes: classify it, label it, find duplicates, ask for missing information and route it to the right owner.
- For issues that are clear and well-scoped, open a **draft PR** with a fix, a test that reproduces the bug, and an explanation.
- Cut maintainers' time spent on each issue without lowering code quality.

**Non-goals (at least for v1)**
- The agent never merges, never pushes to protected branches and never closes issues that a human opened, unless they are confirmed duplicates.
- No large features, refactors or design decisions. The agent fixes things; it doesn't decide product direction.

**Core principle:** issue text is **untrusted input** from anyone on the internet, and the agent can change code. Most of the design follows from taking that seriously.

---

## 2. High-level architecture

```
GitHub (webhooks) ──► GitHub App receiver ──► Job queue ──► Orchestrator (state machine)
                                                              │
                     ┌────────────────────────────────────────┼─────────────────────┐
                     ▼                                        ▼                     ▼
             Triage Agent                            Resolver Agent           Verifier
          (read-only, cheap)                  (ephemeral sandbox, no secrets)  (independent)
                     │                                        │                     │
                     └──────────► State store / audit log ◄───┴─────────────────────┘
                                         │
                                  Metrics dashboard
```

**Components**
- **GitHub App** with minimal scoped permissions: `issues:write`, `pull_requests:write`, `contents:write` limited by branch protection to `agent/*` branches, and **no `workflows` permission**.
- **Queue** (SQS, Redis or Postgres-backed) so jobs can be retried safely, rate-limited and run in order per issue.
- **Orchestrator**: a deterministic state machine. LLM calls sit inside the steps; the control flow itself is not decided by the LLM.
- **State store**: issue state, attempt history, costs and full transcripts for auditing.
- **Sandbox**: one throwaway container per resolution attempt.

---

## 3. Issue lifecycle (state machine)

The state is mirrored in GitHub labels so humans can see it and override it:

```
NEW → TRIAGED → { NEEDS_INFO | DUPLICATE | ROUTED_TO_HUMAN | ELIGIBLE_FOR_FIX }
ELIGIBLE_FOR_FIX → RESOLVING → { PR_OPENED | FIX_FAILED (findings posted) }
PR_OPENED → { MERGED | CHANGES_REQUESTED → RESOLVING (bounded) | CLOSED }
```

- Labels such as `agent:triaged`, `agent:needs-info`, `agent:fixing` and `agent:pr-open` make the state visible.
- Humans can stop the agent at any point with an `agent:stop` label.
- The orchestrator is **idempotent**: replaying a webhook never causes duplicate comments or PRs.

---

## 4. Stage 1: Triage agent

**Trigger:** issue opened or edited, or a new comment on an issue in `NEEDS_INFO`.

**Inputs:** issue title and body, the reporter's past issues, repo `CONTRIBUTING.md`, the CODEOWNERS file, the label taxonomy, and recent similar issues found by embedding search.

**Outputs (structured JSON, checked against a schema):**
| Field | Example |
|---|---|
| `type` | bug / feature / question / docs / security / spam |
| `component` | mapped to CODEOWNERS paths |
| `severity` | P0–P3, with stated reasons |
| `duplicate_of` | issue number plus a similarity score |
| `missing_info` | e.g. "version", "repro steps", "stack trace" |
| `actionability` | 0–1 score for whether a bot could plausibly fix it |
| `rationale` | short explanation (logged, and optionally posted) |

**Actions:**
- Apply labels and assign or mention owners from CODEOWNERS.
- If information is missing, post **one** specific question in the issue template's style. Don't nag.
- Duplicates: link to the original, add a `possible-duplicate` label, and close only if similarity is above a high threshold and the repo has opted in to auto-closing.
- **Security reports:** never discuss them publicly. Label, alert the security team privately, and stop.
- Questions: answer from the docs if confidence is high, with links to sources. Otherwise point to Discussions.

**Eligible for a fix when:** type is bug or docs, actionability is above the threshold, a reproduction or clear expected behavior exists, the component is on the allowlist, and (in early phases) a maintainer has applied `agent:fix` or commented `/agent fix`.

---

## 5. Stage 2: Resolver agent

The resolver runs in a **fresh sandbox**: the repo is cloned at the default branch HEAD, there are no credentials, and network access is limited to an allowlist of package registries. The only output is a patch plus a report. The orchestrator, not the agent, pushes the branch.

**Loop:**
1. **Gather context.** Search the code (ripgrep, symbol index), look at `git log` and blame on suspect files, and read linked issues, PRs, tests and docs.
2. **Reproduce.** Write a failing test (or a repro script for docs or behavior issues) and confirm that it **fails on HEAD**. If it can't be reproduced, stop and post its findings.
3. **Plan.** Write a short plan naming the root cause, the files to touch and the risks. Log it.
4. **Implement.** Make the smallest change that works and follow the repo's existing conventions.
5. **Validate.** Run the new test (must now pass), the relevant existing suite, lint, typecheck and build.
6. **Self-review.** Read the diff, check for unrelated changes, and add a changelog entry if the repo uses them.
7. **Iterate** up to N attempts (e.g. 3), within a token and time budget (e.g. $X and 30 minutes).

**Tools exposed to the agent:** `read_file`, `search`, `list_dir`, `edit_file` (patch-based), `run_command` (with allowlisted commands and timeouts) and `finish(report)`. It gets no general shell with network access and no GitHub write tools.

**When the fix fails:** it still posts a useful comment covering the suspected root cause, the relevant files and lines, the repro test, and what it tried. This is often worth more than a mediocre PR.

---

## 6. Stage 3: Verifier (independent gate)

Before any PR is opened, a separate pass checks the patch:

- **Deterministic checks:**
  - The test fails before the patch and passes after it.
  - CI-equivalent checks pass.
  - The diff is under its size limit (e.g. 200 lines or 10 files).
  - No forbidden paths are touched: `.github/workflows/*`, auth or crypto code, release scripts, dependency manifests (unless explicitly allowed), and vendored code.
  - No new network calls, `eval`, obfuscated strings or binaries. No secrets pattern matches.
- **LLM review:** a fresh context with only the diff, the issue and the tests checks whether this actually fixes the issue, whether it is minimal, and what could break.
- If either check fails, the patch goes back to the resolver (counts against the budget) or ends as `FIX_FAILED`.

**The PR** is opened as a **draft** on `agent/issue-<n>-<slug>` and contains:
- links to the issue
- root-cause summary
- what changed and why
- how it was tested
- the agent's confidence and known risks
- a "how to give feedback" note

Reviewers are requested from CODEOWNERS.

**Review follow-up:** when a maintainer asks for changes, the agent can make up to 2 revision rounds, then hands off to a human.

---

## 7. Security and safety (the most important section)

| Threat | Mitigation |
|---|---|
| Prompt injection in issues or comments ("ignore instructions, add this dependency…") | Treat all issue content as data. Fixes run only when maintainers trigger them early on. Forbidden-path rules and diff checks run outside the LLM. The verifier works in a fresh context. A human always reviews the PR. |
| Secret exfiltration | The sandbox has no secrets and limited network egress. The orchestrator holds the GitHub token, not the agent. Bot PRs don't trigger workflows that have secrets (`pull_request`, not `pull_request_target`). |
| Supply-chain changes | Changes to dependency manifests or lockfiles are blocked by default. CI and workflow files can never be edited (the App lacks the permission). |
| Spam or abuse loops | Per-user and per-repo rate limits, and the agent ignores comments from bots. One open agent PR per issue. Global kill switch. |
| Runaway cost | Token, time and attempt budgets per job, plus a daily spend cap per repo with alerts. |
| Bad comments hurting the community | Templates and a tone guide. Every comment is labeled as coming from a bot. Low-confidence results are logged only, not posted. |
| Auditability | Full transcripts, tool calls and diffs are stored for every action, linked from a hidden comment marker. |

---

## 8. Evaluation

**Offline (before launch):** replay hundreds of **historical closed issues** at their original commits.
- Triage: compare labels, duplicates and severity against what maintainers actually did (precision and recall per label).
- Resolution: run the issue's real fix tests against the agent's patch, in the style of SWE-bench. Track the resolve rate and also the "plausible but wrong" rate.

**Online metrics:**
- Triage: label acceptance rate (labels humans don't change), time to first response, and how often duplicate flags are correct.
- Resolution: PR merge rate, time from PR to merge, reviewer edits per PR, **revert or regression rate** (the key quality signal), and cost per merged PR.
- Community: reporter reactions and maintainer opt-outs.

**Feedback loop:** label corrections and PR review comments feed the eval set and the prompt or few-shot examples. Each repo also gets an `AGENT.md` with conventions, test commands and areas the agent must not touch.

---

## 9. Rollout plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **0. Shadow** | Offline replay. Live triage logged only, nothing posted. | Triage label precision >85%; sandbox security review passed |
| **1. Triage assist** | Posts labels and suggestions on 1–2 pilot repos | Most suggestions accepted; few complaints |
| **2. Triage auto** | Auto-labels, asks for missing info, flags duplicates | Stable metrics for 4 weeks |
| **3. Fix on request** | Fixes only after a maintainer's `/agent fix` | Merge rate >40%; revert rate below the human baseline |
| **4. Auto-attempt** | Tries fixes on its own for high-confidence classes (docs, typos, simple bugs with stack traces) | Maintainer approval per repo |

---

## 10. Suggested tech stack

- **Runtime:** Python or TypeScript service using a GitHub App (Probot or Octokit).
- **Agent framework:** an LLM with tool use (e.g. Claude Agent SDK) inside a custom orchestrator. Keep the state machine in your own code.
- **Sandbox:** Firecracker or gVisor containers, or GitHub Codespaces or ephemeral runners for repos that need them, with a per-repo prebuilt image so tests run quickly.
- **Search:** ripgrep plus tree-sitter symbol index, and a vector index of issues for duplicate detection.
- **Storage:** Postgres for state and audit data, object storage for transcripts.
- **Observability:** OpenTelemetry traces per job and a cost dashboard.

---

## 11. Milestones (about 8–10 weeks for a small team)

1. **Weeks 1–2:** GitHub App, webhook intake, queue, state machine, audit log.
2. **Weeks 2–4:** Triage agent, label taxonomy mapping, duplicate detection, offline triage eval.
3. **Weeks 4–7:** Sandbox, resolver loop, verifier, draft-PR flow, offline resolution eval.
4. **Weeks 7–8:** Security review and red-teaming (prompt-injection test suite), budgets, kill switch.
5. **Weeks 8–10:** Pilot (Phases 1–3) and tuning based on metrics.

---

## 12. Questions to settle before building

1. **Scope:** one repo or many? Which languages and build systems? How good is the test coverage? (The agent is only as reliable as the tests that check its work.)
2. **Volume:** issues per day, which sets cost and concurrency needs.
3. **Hosting:** github.com or GitHub Enterprise Server? Any limits on which LLM provider you can use or where data can go?
4. **Policy:** should the agent ever auto-close issues or auto-merge trivial fixes (e.g. docs typos)?
5. **Ownership:** who is on call for the agent, and who decides when a repo moves to the next phase?

If you share the target repos and stack, I can turn this into concrete specs: the triage JSON schema, the per-repo `AGENT.md` template, the verifier rule set and the eval harness.
