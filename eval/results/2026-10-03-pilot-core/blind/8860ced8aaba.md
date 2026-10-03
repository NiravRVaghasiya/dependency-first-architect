# Plan: Autonomous GitHub issue triage and resolution agent

**Assumptions this plan rests on (please correct any that are wrong):**
- **A1.** "Resolve" means the agent opens a draft pull request (PR). A human always merges, through the repo's existing branch protection. If you want the agent to merge on its own, the plan needs a new high-risk gate and a phase after Phase 5. If you only want triage, Phases 3 and 4 drop out.
- **A2.** The target repos accept issues from outside users, so all issue text is untrusted. If the repos are internal only, injection risk is lower but the same controls still apply.
- **A3.** The agent runs on infrastructure we operate, installed as a GitHub App.

## 1. Classification and constraints
- **What:** A GitHub App that labels and de-duplicates incoming issues. For issues a maintainer opts in, it writes a fix in an isolated sandbox and opens a draft PR from a dedicated fork.
- **Type:** AI agent plus software. The agent is new, but it plugs into existing repos, CI, branch protection and CODEOWNERS. Not a small build: it reads untrusted outside input, spends money and changes code.
- **Dominant constraint:** Correctness and safety. Every action the agent takes must stay inside fixed limits, even if an injected instruction succeeds.
- **Worst failure:** Text planted in an issue, comment, repo file or tool output makes the agent leak a secret or private code, or get malicious code into a default branch. A leaked secret can't be taken back: GitHub emails comment notifications, and merged code ships.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Webhook acknowledgement | ≤ 10 s | REQUIREMENT (GitHub's webhook delivery timeout) | V0 |
| Triage latency (issue opened → labels applied) | p95 ≤ 10 min | ASSUMPTION (much faster than human triage; compare with the human first-response time measured in Phase 0) | Phase 2 exit |
| Resolve attempt time | hard timeout 45 min per attempt, ≤ 2 attempts per issue | ASSUMPTION (keeps sandbox cost and queue time bounded) | Phase 1 cap test, V3 |
| Throughput | issues per day across target repos | BASELINE (measured from 90 days of API history in Phase 0) | Phase 0 |
| GitHub API usage | stay inside the App's rate limit (5,000 requests/h, more for large orgs) with ≥ 50% headroom | REQUIREMENT (the limit) / ASSUMPTION (the headroom) | Phase 2 exit |
| Availability | no user-facing SLO; 99% of events handled within 1 h, including a reconcile sweep | ASSUMPTION (the fallback is human triage) | Phase 2 exit |
| AI cost per triage | hard cap $0.05 per issue | ASSUMPTION (a short prompt plus retrieval) | V0 baseline, Phase 1 |
| AI cost per resolve attempt | hard cap $3 | ASSUMPTION (a typical coding-agent loop at hosted-model prices) | V3 |
| Cost per merged agent PR | ≤ $15 | ASSUMPTION (a small fraction of one engineer-hour; budget owner to confirm) | V4 |
| Monthly spend cap | UNKNOWN: the budget owner (engineering manager) supplies it before Phase 4 | UNKNOWN | V4 |
| Reviewer burden | median ≤ 15 min of maintainer review per agent PR; ≥ 30% of agent PRs merged | ASSUMPTION (below this the agent costs more review time than it saves) | V4 |
| Storage | traces and transcripts kept 30 days; audit log kept 1 year | ASSUMPTION (the retention policy for private-repo content is UNKNOWN; the security owner supplies it in Phase 1) | Phase 1 |
| Operational complexity | one orchestrator service, a sandbox pool, Postgres and a queue; on-call during business hours by the owning team | ASSUMPTION | Phase 5 exit |

**Missing inputs** (most plan-changing first):
1. **Whether the agent may ever merge (A1).** This changes the phase order.
2. **Public or private repos, and who can file issues (A2).**
3. **A signed data-processing agreement with the model provider** covering zero data retention and no training. Private code can't go to the provider without it.
4. **Approval from a GitHub org admin** to install the App and create a fork org. This blocks Phase 0.
5. **Which canary repo to start with, and whether it has tests and maintainers willing to review agent PRs.**
6. **The monthly spend cap.**
7. **A named security reviewer** for V1.

## 2. Dependencies

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Installing the App on the sandbox repo and the fork org | Org admin approval (missing input 4) | organizational | deployed (Phase 0) |
| Any private-repo code or issue text sent to a model | Signed zero-retention agreement (missing input 3) | organizational, risk/security | exposed |
| Real outside-author text reaching an agent that has write tools | V1 | risk/security | exposed (Phase 2 onward) |
| V1 sign-off | Named security reviewer, booked in Phase 0 | organizational | exposed |
| Agent PRs on any repo | That repo's workflow inventory shows no secret-bearing workflows run on fork PRs (`pull_request_target`, self-hosted runners). ASSUMED until checked per repo | risk/security | exposed |
| Auto-applying triage labels | V2 | validation | exposed |
| Detailed design of the resolve loop and choice of coding harness | V3 | decision, validation | specified |
| Phase 4 start | Canary maintainers agree to review agent PRs; monthly spend cap supplied | organizational, economic | exposed, committed |
| Widening past the canary repo | V4 | economic | scaled |
| V2 and V3 datasets | Historical issues with maintainer labels; closed issues with a linked fix PR that adds tests | validation | specified |

Structural and runtime dependencies are already shown by the phase order.

## 3. Key decisions (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Revisit trigger |
|---|---|---|---|---|---|
| Who can merge | R3: merged malicious code ships, and the harm can't be undone | The agent never merges. It opens draft PRs from a fork. A GitHub ruleset requires a CODEOWNER to approve before merge. GitHub enforces this, not the agent | Maintainers will review agent PRs | V1 (attempts to bypass), V4 (review burden) | V4 fails → resolve is switched off and the system stays triage-only. Moving toward autonomous merge needs a new gate (see §10) |
| Trust boundary between credentials and code execution | R3: a leaked App key or secret can't be taken back | Three separate planes. (1) **Control plane**: holds the App key in a secrets manager; never runs code. (2) **Sandbox**: one throwaway gVisor or Firecracker VM per attempt, with no credentials; it reaches the network only through a proxy to a package mirror and to a model gateway that adds the API key. (3) **Publisher**: checks every diff before pushing it to the fork | The chosen harness works with its model access going through the proxy | V1 | V1 finds a decoy secret (honeytoken) leaving the sandbox → cut sandbox network access to none and use vendored dependencies, or drop the in-sandbox harness |
| Where the agent pushes | R2: changing it means reconfiguring CI on every repo | Push to a dedicated fork org. On upstream repos the App can write only issues and PRs; it has read-only access to code | Fork-PR CI runs without secrets and is enough to validate a fix | Phase 0 workflow inventory | Meaningful tests need secrets → a maintainer triggers secret-bearing CI by hand with an `ok-to-test` label |
| Build vs buy | R2: the harness sits behind an interface | Build the control plane (policy, spending limits, audit, triage), which is specific to our org. Choose the resolve harness through a bake-off: a hosted coding agent (e.g. GitHub Copilot coding agent) vs an open-source harness in our sandbox | A hosted agent may not support our network limits, path deny-list and audit | V3 | The hosted agent wins V3 and can meet V1's controls → buy it, and keep only triage and policy in-house |
| Data-privacy boundary | R3: code and text held by a vendor can't be recalled | Hosted API under zero-retention, no-training terms. Secrets and PII are scrubbed from issue text before prompting and from everything the agent posts. Private repos join only after the agreement is signed | The agreement can be obtained | Cited basis: the signed agreement (missing input 3), plus the redaction cases in V1 | No agreement → public repos only, or self-host |
| Scope of triage actions | R2: comments to outside reporters are visible to users | Labels are auto-applied once V2 passes. Closing a duplicate or commenting to the reporter needs a maintainer's approval | Wrong labels are cheap to fix; wrong closes annoy reporters | V2 | V2 fails → suggestions only, on an internal dashboard |
| Prompt + retrieval vs fine-tune | R2 | Prompt plus retrieval (code search, past issues) | Retrieval captures repo conventions well enough | V2, V3 | After retrieval tuning, the eval still shows the same convention errors, and volume justifies the cost of training |
| Hosted API vs self-host | R2 (R3 if data residency applies) | Hosted, behind our own gateway with two providers | Provider terms acceptable; the volume is modest | Cited basis: the agreement; V4 cost | No agreement, or sustained monthly spend above the cost of self-hosting |

**minor defaults:**
- **Sync vs async:** async. Webhook → queue → workers, because GitHub's 10 s ack requirement forces it.
- **Monolith vs services:** one orchestrator service plus the sandbox pool. The plane split is covered by the trust-boundary row.
- **Consistency vs availability:** consistency first. Postgres is the single source of agent state, with idempotency keys on (delivery ID, issue, action) so no duplicate PRs or comments. Missed webhooks are recovered by a reconcile sweep every 15 min.

**N/A:** none.

## 4. First end-to-end slice (Phase 0)
- **The request:** A team member opens an issue ("fix typo in README") on an internal sandbox repo that has no secrets.
- **The response:** The agent posts suggested labels as a comment and opens one draft PR from the fork.
- **Every tier it crosses:**
  1. GitHub webhook
  2. Receiver: checks the signature, acks within 10 s
  3. Queue
  4. Orchestrator, which writes state to Postgres
  5. Model gateway, with a hard cap of 8k tokens and $0.05 for triage, $3 for resolve
  6. Throwaway sandbox: clone, edit, run tests
  7. Publisher: checks the diff, pushes to the fork, opens the PR
  8. Back to GitHub
- **Treating input as data:** Issue text is passed to the model in a delimited block marked as data, never as instructions.
- **Deploy and rollback:** CI builds an image pinned by digest; Terraform deploys it. Rollback means redeploying the previous digest. A kill switch (a database flag, plus suspending the App) stops everything.
- **Logging and monitoring:** One OpenTelemetry trace per issue across every tier. Token and cost metrics. Alerts on worker errors, queue age and spend rate.
- **Who can reach it:** The App is installed on the sandbox repo and the fork org only. The receiver drops any event whose sender isn't on the team allow-list. A per-repo flag defaults to off.
- **Brownfield inventory:**
  - For each candidate target repo: workflows (`pull_request_target`, self-hosted runners), branch protection, CODEOWNERS and label taxonomy. Each item is marked CONFIRMED or ASSUMED.
  - Baselines: issue volume, and the median time to a human's first response.
  - Start the organizational items: data-processing agreement, security reviewer, canary maintainers.

**Exit check (V0):**
- The skeleton issue produces a comment and a draft PR, with one trace across every tier.
- A deploy and a rollback both succeed through the pipeline.
- An injected model-API 500 and a sandbox timeout each fire an alert.
- First values are recorded as baselines: ack time, triage time, tokens and cost per triage and per resolve, sandbox boot time.

## 5. Phases

**Phase 1: Defenses (injection containment, spending limits, human approval)**
- **Unlocks:** Real untrusted text can reach the agent (Phase 2 onward).
- **Depends on:** V0.
- **Tasks:**
  1. Lock the App's permissions and repository rulesets (only `agent/*` refs on the fork).
  2. Network proxy for the sandbox, allowing only the package mirror and the model gateway.
  3. Path deny-list for diffs: `.github/**`, CODEOWNERS, CI and build scripts, new dependencies without a maintainer label. Plus diff size caps (≤ 500 lines), no binaries, and a gitleaks secret scan on each diff.
  4. A sanitizer for everything the agent posts: strip links and images not on an allow-list (rendered markdown images are a way to leak data), and scrub secrets and PII.
  5. Plant decoy secrets (honeytokens) in the sandbox environment and in test repos.
  6. Approval enforcement in the executor. Closing an issue or a reporter-facing comment needs an approval tied to that exact action. The resolve trigger is a maintainer-only `agent-ok` label, and the publisher checks that the person who added it has write permission.
  7. Caps: tokens per request, iterations, wall-clock time, spend per issue and per day; the kill switch.
  8. Injection suite (≥ 200 cases) wired into CI.
  9. Red-team session with the named security reviewer.
- **Rollback:** Config and code revert by image digest. No external state changes in this phase.
- **Exit check:** V1 passes. A runaway loop is cut off at its cap and emits a metric.

**Phase 2: Triage (retrieval and model access)**
- **Unlocks:** Labels applied automatically on the canary repo.
- **Depends on:** Phase 1, V1. For a private canary repo, also the signed agreement.
- **Tasks:**
  1. Agree a label taxonomy contract per repo with maintainers. This is the widest rework if wrong, so it goes first.
  2. Retrieval: a per-repo index of past issues (for duplicates) and code search (for component labels). Pin the embedding model version. Never query a private repo's index for a public repo's issue.
  3. Model gateway: second provider as fallback, structured-output parsing.
  4. Run in shadow mode on the canary repo for 2 weeks: results are stored internally only.
  5. Run V2.
  6. Turn on auto-labeling. Duplicate and close suggestions go to a maintainer approval queue.
- **Rollback:** Turn the repo flag off. The audit log records an inverse for every label, so they can be removed in bulk.
- **Exit check:** V2 passes. Triage p95 is measured against its target. GitHub API headroom is ≥ 50%.

**Phase 3: Resolve offline (memory and orchestration)**
- **Unlocks:** Choice of harness; resolve design is fixed.
- **Depends on:** Phase 2, V1. Obtaining the monthly spend cap is also a task here.
- **Tasks:**
  1. Replay dataset: ≥ 50 closed canary-repo issues with a linked fix PR that adds tests. Each is checked out at the commit before the fix, with the fix PR's tests hidden from the agent.
  2. Resolve loop running inside the sandbox: reproduce → locate (using retrieval) → edit → test → self-review → produce a diff. Built behind a harness interface, for both candidates.
  3. Per-issue memory: attempt history and failure reasons. The model doesn't write any memory that carries across issues.
  4. Run V3. Nothing is published in this phase.
- **Rollback:** None needed; nothing is exposed.
- **Exit check:** V3 passes. Every replay run has a full trace, and failed steps show up as a status rather than disappearing.

**Phase 4: Resolve canary**
- **Unlocks:** Evidence on whether the agent is worth its cost.
- **Depends on:** V3. The canary repo's workflow inventory is CONFIRMED. Maintainers have agreed to review. The monthly cap is supplied.
- **Tasks:**
  1. Issues enter resolve only through the `agent-ok` label.
  2. Draft PRs come from the fork. Each is labeled `agent-authored` and its body links the trace ID, prompt version and model ID.
  3. Capture outcomes: merged, closed, review time, review comments.
  4. Run V4.
- **Rollback:** Turn the resolve flag off and bulk-close agent PRs (runbook). Merged code is reverted through the repo's normal process.
- **Point of no return:** Comments already posted publicly. This is why the sanitizer is part of V1.
- **Exit check:** V4 passes.

**Phase 5: Widen, routing and feedback**
- **Unlocks:** More repos and lower cost.
- **Depends on:** V4.
- **Tasks:**
  1. Add repos one at a time. Each needs its workflow inventory CONFIRMED, maintainer consent, and the V4 thresholds re-checked per repo after 4 weeks.
  2. Routing: a cheap model for triage; a strong model for resolve; skip issue classes whose merge rate is below the V4 bar.
  3. Feedback loop: maintainer corrections go into the eval sets only after review.
  4. An eval regression run in CI blocks any prompt or model change that makes results worse.
  5. Re-run the red team every quarter.
- **Rollback:** Turn the flag off per repo or per issue class.
- **Exit check:**
  - Each new repo meets the V4 thresholds.
  - On the eval set, routed quality is within 2 points of the strongest model, at lower cost.
  - On-call load stays inside the operational-complexity budget.

## 6. Validation checks

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | Skeleton issue on the sandbox repo; pipeline deploy and rollback; injected failures | The §4 exit check | Trace export, pipeline run links, alert history, baseline sheet in `gate-evidence/V0/` | Phase 1. If it fails, fix the skeleton; nothing else starts | 0 |
| V1 | Even when an injection succeeds, no secret, private code or unapproved write gets out | Injection suite of ≥ 200 cases planted in issue bodies, comments, repo files, test output and dependency docs; decoy secrets; attempts to bypass approval and the deny-list; runaway-loop test; red-team session | **0** tool calls outside the allow-list, decoy-secret bytes at the proxy or in posted text, pushes outside fork `agent/*` refs, deny-listed paths reaching the fork, non-allow-listed URLs or images posted, or gated actions run without approval. Caps cut runaway loops. Named security reviewer signs off pass/fail. ASSUMPTION: zero tolerance because the worst failure can't be undone; this bars failures of the design controls, while the filters' catch rate is only tracked | CI suite report, proxy logs, red-team report and sign-off ticket in `gate-evidence/V1/` | Phase 2 and all real exposure. If it fails: the trust-boundary flip in §3; no exposure | Reviewer booked in Phase 0; runs in Phase 1; required before Phase 2 |
| V2 | Triage is accurate enough to apply automatically | Replay ≥ 300 historical canary issues against maintainer labels, plus a 2-week live shadow | Label precision ≥ 90% at ≥ 70% coverage (the agent abstains on the rest); duplicate precision ≥ 95%; shadow results within 5 points of replay. ASSUMPTION: about 1 correction in 10 is tolerable; canary maintainers to confirm | Eval report plus a frozen, versioned dataset in `evals/triage/` | Auto-labeling. If it fails: suggestions only | 2 |
| V3 | The agent fixes enough real issues in this repo, at an acceptable cost, to be worth reviewing | Offline replay on ≥ 50 historical issues with hidden tests, for both candidate harnesses, under V1's controls | ≥ 25% of issues resolved (hidden tests pass, no existing test broken); median ≤ $3 per attempt; p95 ≤ 45 min; 0 V1 violations. ASSUMPTION: public benchmark scores overstate results on private repos, so we measure here | Per-harness replay report and traces in `evals/resolve/` | Phase 4 and the harness choice. If it fails: resolve only the issue classes that pass, or stay triage-only with "proposed fix plan" comments | 3 |
| V4 | Agent PRs save more than they cost | Canary run of ≥ 4 weeks and ≥ 40 PRs | ≥ 30% merged with at most minor edits; median review ≤ 15 min; ≤ $15 per merged PR; within the monthly cap (UNKNOWN until supplied, so it must be obtained in Phase 3); 0 security incidents (all ASSUMPTION except the cap). Canary maintainer lead signs off | Outcome-funnel dashboard export plus sign-off in `gate-evidence/V4/` | Phase 5. If it fails: narrow to the classes that pass, or turn resolve off | 4 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Least-privilege App; key in a secrets manager; sender allow-list; issue text treated as data; sandbox has no credentials | One OpenTelemetry trace per issue; token and cost metrics; alerts on errors, queue age and spend | Terraform; images pinned by digest; prompt and model versions in config; every run records prompt hash, model ID and repo SHA | Webhook redelivery plus reconcile sweep; idempotency keys; kill switch; rollback by digest |
| 1 | Network allow-list, deny-list, sanitizer, decoy secrets, approvals enforced by the executor | Blocked actions, network denials and decoy-secret hits page security; audit log records every write and its inverse | Injection and red-team suites versioned and run in CI | Caps cut runaway loops; circuit breaker on provider errors; concurrency limit per repo |
| 2 | Per-repo index isolation; scrubbing before prompts | Triage precision against maintainer corrections; latency p95; API headroom | Embedding model pinned in the index version; frozen labeled set | Fallback provider; degraded mode is "no triage, humans do it"; backoff on GitHub API rate limits |
| 3 | Replay runs under V1's controls without publish credentials; a fresh sandbox for every attempt | Per-step loop traces (tool calls, test runs); cost per attempt | Replay set pinned to issue IDs and the commit before each fix; harness version and model settings recorded | Attempt timeouts; at most 2 attempts; failures shown as a status |
| 4 | Permission check on whoever adds the `agent-ok` label; CODEOWNER review; secret scan on every diff | PR outcome funnel; cost per merged PR | PR body links the trace, prompt and model | Bulk-close runbook; resolve flag per repo; spend cap halts work |
| 5 | Workflow inventory required per new repo; feedback enters only through review; quarterly red team | Dashboards per repo and per class; routing decisions logged | Eval regression in CI gates any prompt or model change | Per-repo budgets and isolation; rollback per wave |

## 8. AI layer

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Injection and guardrail defense | Text marked as data; three-plane split; network allow-list; deny-list; output sanitizer | Phase 0 (basics), Phase 1 | V1 |
| 2. Cost and latency budget | Caps on tokens, iterations, wall-clock time, spend per issue and per day; kill switch | Phase 0 (hard caps), Phase 1 | Phase 1 cap test; V3 and V4 (cost) |
| 3. Human approval | Resolve starts only from the maintainer `agent-ok` label; closing an issue or replying to the reporter needs approval; merging always needs a CODEOWNER | Phase 1 | V1 (attempts to bypass) |
| 4. Retrieval | Per-repo index of past issues and code search; embedding model pinned | Phase 2 | V2 |
| 5. Model access | Gateway with two providers, structured output, key injection | Phase 0 (basic call), Phase 2 | Switching to the fallback is a config change, and V2's thresholds still hold on it |
| 6. Memory | Per-issue attempt state in Postgres only | Phase 3 | Prior attempts are recalled on retry; nothing carries across issues |
| 7. Orchestration | A single-agent loop inside the sandbox (reproduce → locate → edit → test → self-review), through the harness interface | Phase 3 | V3 |
| 8. Routing | Cheap model vs strong model; skip low-yield issue classes | Phase 5 | Phase 5 routing exit check |
| 9. Feedback | PR outcomes and maintainer corrections, entering eval sets only after review | Capture in Phase 4; loop in Phase 5 | The next eval improves with no regressions elsewhere |

## 9. Exceptions
None.

## 10. Deferred
- **Autonomous merge:** pulled forward only by an explicit request, plus a new high-risk gate. That gate would need sustained V4 merge rates for a narrow issue class (docs, dependency bumps) and security sign-off.
- **Closing duplicates or stale issues without approval:** when duplicate precision stays ≥ 98% over 3 months of approval-queue decisions.
- **The agent iterating on PR review comments:** after V4 passes and canary reviewers ask for it.
- **Resolving issues without a maintainer opting each one in:** when per-class merge rates exceed the V4 bar for 2 consecutive months.
- **Long-term memory across issues (learned repo conventions):** when eval failures show the same convention errors repeatedly. Any memory writes would be reviewed.
- **Fine-tuning or self-hosting:** per the revisit triggers in §3.
- **Multi-agent orchestration:** when V3 failures are dominated by multi-file or multi-step issues that the single loop can't handle.
- **Serving other orgs (multi-tenant):** if the agent is offered outside our org. That would need a tenancy-isolation gate.
