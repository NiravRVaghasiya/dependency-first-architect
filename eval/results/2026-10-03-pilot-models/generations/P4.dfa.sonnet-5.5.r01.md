# BUILD PLAN: Autonomous GitHub issue triage and resolution agent

## 1. Classification and constraints
- **What:** A GitHub App-driven agent that receives issue webhooks, triages each issue (labels, duplicates, owner suggestion), and for eligible issues produces a draft pull request that a human reviews and merges.
- **Type:** AI-agentic with infra. Greenfield service that acts on existing repos. Not a small build: it takes untrusted input, executes code, spends inference money, and writes to source control.
- **Dominant constraint:** correctness and safety. The agent's blast radius must stay bounded.
- **Worst failure:** A crafted issue, comment, or linked file hijacks the agent. It then exfiltrates secrets or code, or lands malicious code in the repo. Possible routes are a merged PR, a CI workflow that runs with secrets, or a sandbox escape.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency (triage) | p95 ≤ 5 min from webhook to comment or label | ASSUMPTION (async advisory work; maintainers tolerate minutes) | Phase 2 exit, baseline from V0 |
| Latency (resolution attempt) | ≤ 30 min wall-clock per attempt, then abort | ASSUMPTION (placeholder until Phase 3 data) | Phase 3 exit |
| Throughput | UNKNOWN: issues/day per repo. Owner: repo owners, or a GitHub API history export by us. Needed in Phase 1 to size queue and daily caps. Design concurrency is 5 runs. | UNKNOWN | Phase 1 |
| Availability / SLO | Best-effort, business hours. A 15-min reconciliation poll recovers missed webhooks. | ASSUMPTION (advisory tool, GitHub remains the system of record) | Phase 2 exit |
| RPO / RTO | RPO is not applicable: run state is rebuildable from GitHub. RTO 4 h. | ASSUMPTION | Phase 2 exit |
| AI inference cost | Skeleton caps: $0.25/run, $20/day. Triage ≤ $0.10/issue. Resolution attempt ≤ $5 hard cap. | ASSUMPTION (placeholders, replaced by V0 baselines) | V0, Phase 1 |
| AI inference cost (per accepted PR) | UNKNOWN. Owner: budget owner or engineering manager. Needed before Phase 4. | UNKNOWN | V5 |
| Resource use | Sandbox 2 vCPU / 4 GB / 30 min per run | ASSUMPTION | V3, Phase 3 |
| Operational complexity | 4 deployables (ingress, worker, sandbox runner, Postgres) plus a queue. Business-hours on-call. | ASSUMPTION | Phase 2 exit |

**Missing inputs** (none change the phase order; each changes thresholds or scope):
- Target repos: public or private, and the author population. I assume one private internal repo first, with org-member authors. Stranger-authored issues on public repos are deferred (§10).
- Model provider and its data-retention terms for source code. I assume a hosted API with zero-retention and no-training terms, to be confirmed in V1.
- Hosting platform. I assume the org's existing container platform and CI.
- Success definition: which issue types count as "agent-attemptable", and the acceptable PR acceptance rate (V5).
- Budget owner's cost ceiling per accepted PR.
- Maintainer time to label the eval sets.
- GitHub org-admin approval to install the App.

## 2. Dependency map

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 skeleton | GitHub App creation and install approval (org admin) and a dedicated test repo. Start first. | organizational | deployed |
| Any real-repo issue text or code reaching the model | V1: security/legal sign-off on the data boundary | organizational / risk-security | exposed |
| Any write beyond comments, and any real-repo exposure | V2: injection containment and permission boundary | risk-security | exposed |
| Any execution of repo code | V3: sandbox isolation | risk-security | deployed |
| Agent PRs on a real repo | Branch protection with required human review, CODEOWNERS covering `.github/`, and CI on agent branches running without secrets. The owning team confirms these. | risk-security / organizational | exposed |
| Auto-applying labels (beyond suggest-only) | V4: triage quality | validation | exposed |
| Resolution beyond the canary repo, and any budget commitment | V5: resolution viability and cost | validation / economic | scaled, committed |
| V4 and V5 | Maintainer-labeled eval sets, plus a human-agreement baseline. Starts in Phase 0 and is required before Phase 2. | organizational | specified (thresholds) |
| V5 threshold and the caps in Phase 1 | Acceptance-rate target and cost ceiling from the product owner and budget owner | organizational / economic | specified |

No runtime or structural dependencies that the phase order doesn't already show.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Data-privacy boundary: hosted model vs self-host | R3 (source code and issue text sent out can't be recalled; contractual) | Hosted API under zero-retention / no-training terms. Only allow-listed private repos go out. Secrets are never put in context. Traces and logs carry the same classification as the data they record and are access-controlled. | The provider's terms satisfy org policy. | V1 | V1 fails, or policy forbids external code egress. Then self-host or a VPC-hosted model, and re-plan Phase 2 onward. |
| Trust boundary: agent identity, permissions, autonomy | R3 (trust boundary; a short time wrong can expose code or secrets) | The GitHub App has minimal permissions. Issues and PRs are write. Contents is write on `agent/*` branches only. It has no workflows, admin, or merge permission. The agent cannot approve PRs. The model never holds a token. An out-of-model tool executor applies writes. A human merges every PR. | Branch protection can be enforced on target repos. | V2 | V2 fails, or protection can't be enforced on a repo. That repo stays suggest-only (comments). |
| Sandbox isolation for code execution | R3 (escape exposes secrets and network) | Ephemeral microVM or gVisor container per run. Read-only repo snapshot and scratch workspace. No credentials. Egress only via an allow-listed proxy for the package mirror and model API. Metadata service blocked. | Hardening at this level contains agent-generated code. | V3 | V3 fails. Then stronger isolation (Firecracker microVM), or resolution is dropped and triage-only ships. |

**R1 defaults:**
- Consistency vs availability → availability: at-least-once webhooks, idempotent on delivery ID, GitHub as system of record, run records in Postgres.
- Monolith vs services → one orchestrator service plus a separate sandbox runner (isolation forces the split).
- Sync vs async → async queue (GitHub's 10 s webhook timeout is a REQUIREMENT).
- Build vs buy → thin orchestrator on the Claude Agent SDK and the GitHub App API. Flip if a 1-day bake-off of a managed coding agent (e.g. Copilot coding agent) on the Phase 0 eval subset meets the V4/V5 bars at lower cost.
- Prompt+RAG vs fine-tune → prompt+RAG. Fine-tuning on repo or personal data is not planned.
- Retrieval index → ripgrep at a pinned commit SHA for code, plus an embedding index for duplicate issues (embedding model pinned in the index version).

**N/A:** Brownfield migration rows (greenfield service; GitHub stays the system of record and no data moves). Tenancy (single org).

## 4. Walking skeleton (Phase 0)
- **Real request:** A real issue is opened in a dedicated test repo by an allow-listed org member. The agent posts a comment proposing labels, with no code execution and no PR. The real response is that comment, visible on GitHub.
- **Tiers:**
  - GitHub webhook.
  - Ingress (HMAC-verified, acks within 10 s, enqueues on delivery ID).
  - Queue.
  - Worker (issue text passed as delimited data, never as instructions; hard per-run token cap and daily spend cap).
  - Model API.
  - Tool executor.
  - GitHub API comment.
  - Postgres run record.
- **Deploy:** Through CI/CD with infrastructure as code, from the first commit. Images are pinned by digest.
- **Logs and monitoring:** One OpenTelemetry trace keyed by the webhook delivery ID spans every tier. Metrics cover tokens, cost, and latency per run. Alerts cover worker errors, model API failures, and spend above 50% of the daily cap.
- **Rollback:** Redeploy the previous image, plus a kill-switch flag (empty the repo allow-list) that stops all agent activity within one minute.
- **Reachability:** Only GitHub webhooks for the one allow-listed test repo. Only allow-listed authors are processed. Non-test repos are not installed.
- **Also started here:** The organizational items in §2 (App approval, V1 sign-off, eval-set labeling).

**Exit check (V0):** A real issue yields a comment with one trace across every tier. A deploy and a rollback succeed through the pipeline, and the kill switch stops processing. An injected failure (model API returning 500) fires an alert. Cost, token, and latency floors are recorded as baselines.

## 5. Phases

- **Phase 1: Defenses and budgets**
  - Unlocks: Any real-repo exposure. All later capability.
  - Depends on: V0.
  - Tasks, widest blast radius first:
    1. Tool executor as a policy layer: an allow-list of tools and argument validation. Writes are confined to the issue, label, and comment set plus `agent/*` branches.
    2. Trust tiers by author and content source. All issue text, comments, linked files, and tool output are tagged as untrusted data.
    3. Output filters that strip URLs, images, and @mentions from agent comments, which closes GitHub's image-render exfiltration path. There is no outbound tool.
    4. The approval mechanism: a maintainer-applied label or comment, bound to the exact issue and commit SHA, enforced by the executor and logged.
    5. Caps on tokens, iterations, per-run cost, per-repo daily spend, and timeouts, with a degraded "could not complete" comment.
    6. The injection suite in CI and the permission tests (V2).
    7. Replace the placeholder caps with real values once the throughput and cost inputs arrive.
  - Rollback: Kill switch. The executor policy is versioned config.
  - Exit check: V2 passes. A runaway loop is cut off at the cap and emits a cost metric.

- **Phase 2: Retrieval, model access, and triage (smallest exposure first)**
  - Unlocks: Triage on real issues.
  - Depends on: Phase 1, V1 (required before any non-test issue reaches the model), and the labeled triage set.
  - Tasks:
    1. Retrieval: pinned-SHA code search, duplicate-issue index, and CODEOWNERS for owner suggestions.
    2. Model access: provider abstraction, retries, a fallback model, and structured outputs. The eval harness runs in CI.
    3. Short-term run memory only (per-issue run record).
    4. Single-call triage of label, duplicate, and owner suggestions.
    5. Rollout on one internal repo: shadow mode (log only), then suggest-only comments, then auto-label if V4 passes.
    6. Reconciliation poll for missed webhooks.
  - Rollback: Kill switch. Remove agent labels and comments by run ID.
  - Exit check: V4 passes. Retrieval hit rate on the labeled set is reported. Duplicate detection only suggests and never closes. Triage p95 latency meets its budget row.

- **Phase 3: Sandbox and patch generation (no PRs yet)**
  - Unlocks: Code execution and candidate patches, stored as artifacts for offline evaluation.
  - Depends on: Phase 1 and the V3 design.
  - Tasks:
    1. The sandbox runner and its network proxy.
    2. V3 red-team.
    3. The agent loop with tools (search, read, edit, run tests), bounded by caps.
    4. Orchestration kept to a single agent loop, every step traced, failures surfaced rather than swallowed.
    5. Eligibility classifier that marks "agent-attemptable" issues.
    6. Offline runs on the historical eval set.
  - Rollback: Disable the runner. Patches are inert artifacts.
  - Exit check: V3 passes. On the eval set, the share of multi-step runs that complete with full traces is reported, and no run exceeds its caps.

- **Phase 4: Draft PRs on a canary repo**
  - Unlocks: End-to-end resolution with a human merge.
  - Depends on: Phases 2 and 3, V2, V3, the branch protection and CI-without-secrets confirmation (§2), and the V5 thresholds set by their owners.
  - Tasks:
    1. Executor pushes to `agent/*` and opens a draft PR, only after a maintainer approval bound to the issue and SHA.
    2. CI on agent branches runs with no secrets. Workflow files and `.github/` are protected by CODEOWNERS.
    3. Canary on one repo and one maintainer group.
    4. V5 measurement over ≥50 attempts.
  - Rollback: Close PRs, delete `agent/*` branches, revoke the repo allow-list entry. The agent cannot merge, so there is no irreversible step.
  - Exit check: V5 evidence is reviewed. There are zero policy violations in the executor logs.

- **Phase 5: Widen, route, and feed back**
  - Unlocks: More repos and more spend.
  - Depends on: V5 passed.
  - Tasks:
    1. Add repos one at a time, each with its own V1 scope check and protection check.
    2. Routing: a cheaper model for triage and a stronger one for resolution, only if V5 cost demands it and the eval shows quality within margin.
    3. Feedback: maintainer corrections and PR outcomes enter the eval set through reviewer approval only.
  - Rollback: Per-repo allow-list removal.
  - Exit check: Per-repo cost and acceptance rates stay within the V5 bars. The next eval run improves the target metric with no regression elsewhere.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed, and rolled back through every tier in production | Open a real issue in the test repo, deploy, roll back, inject a model API failure | The V0 exit check in §4 | Trace export, pipeline run logs, alert record, baselines sheet, all in the repo's `evidence/` | Phase 1. If it fails, fix the skeleton first. | 0 |
| V1 | Sending issue text and code to the chosen provider meets org data policy | Security/legal review of provider terms (retention, training, sub-processors) and of the data-flow diagram | Named security reviewer's written pass/fail sign-off, and legal sign-off if contract terms are involved | Signed review record, data-flow diagram | Phase 2 and all real-repo exposure. If it fails: self-host flip (§3 row 1). | Starts in 0, required before 2 |
| V2 | A successful injection cannot escape the allow-list, perform an unapproved write, or move data out. The App token cannot exceed its granted scope. | CI injection suite planted in issue title, body, comments, linked files, and tool output. Separate permission tests try to push to `main`, approve a PR, edit workflows, and read secrets using the App token. | Structural checks: 0 escapes across ≥150 cases (ASSUMPTION: 5 channels × 6 attack goals × ≥5 variants). Permission tests: 0 successes (by design, not by filtering). Filter pass rate is tracked but is not the bar. | CI report, test corpus in the repo, permission test log | Real-repo exposure and all writes beyond comments. If it fails: no exposure, fix the executor design. | 1 |
| V3 | Agent-generated code cannot reach credentials, the metadata service, or arbitrary hosts, or exhaust the host | Red-team script and manual review: env and file reads, `169.254.169.254`, egress to arbitrary hosts, writes outside the workspace, fork bomb, oversized output | All attempts blocked or contained within the resource limits. Named security reviewer's pass sign-off. | Red-team log and reviewer sign-off | Any repo code execution (Phase 3 onward). If it fails: stronger isolation, or drop resolution and keep triage-only. | Starts in 3 (design in 1), required before any execution |
| V4 | Triage is accurate enough to apply labels automatically | Run on ≥200 maintainer-labeled historic issues (ASSUMPTION: gives about ±5 pt at 95% near 85% accuracy). Two maintainers also label a 50-issue subset to measure human agreement. | Label accuracy ≥ inter-maintainer agreement on the same subset (BASELINE, measured). Duplicate suggestions precision ≥ 90% (ASSUMPTION: a wrong duplicate is user-visible). | Eval report and labeled sets | Auto-label in Phase 2. If it fails: stay suggest-only. | 2 |
| V5 | Resolution produces PRs that maintainers accept, at a cost the business will pay | Canary over ≥50 attempts on one repo | Accepted-PR share ≥ UNKNOWN (product owner sets it before Phase 4). Cost per accepted PR ≤ UNKNOWN (budget owner). 0 policy violations, 0 PRs touching workflow or secret-bearing files. Net maintainer review time is recorded. | Canary report with per-run cost and outcomes | Phase 5 scaling and budget commitment. If it fails: restrict to triage plus fix hints and re-plan resolution. | 4 |

V5 cannot pass until its two UNKNOWN thresholds are supplied. Getting them is a Phase 1–3 task, not a Phase 4 one.

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | HMAC-verified webhook. Least-privilege App. Secrets in a secret manager. Allow-listed repo and authors. Token and cost cap. | OTel trace per delivery ID. Cost, token, and latency metrics. Error alert. | IaC. Digest-pinned images. Prompts in version control. | Idempotent on delivery ID. Kill switch. Tested rollback. |
| 1 | Executor allow-list. Untrusted-data tagging. Output filters. V2. | Policy-denial and cap-hit metrics. Audit log of every write and approval. | Injection corpus versioned. Policy as config with history. | Timeouts and caps. Degraded "could not complete" comment. |
| 2 | V1 scope enforced per repo. Retrieval limited to repos the App can read. Traces carry the data's classification. | Eval scores and triage accuracy dashboards. Shadow-vs-actual diff. | Pinned model version, embedding model, and index SHA. Eval set versioned. | Reconciliation poll. Fallback model. Retries with backoff. |
| 3 | V3 sandbox. Zero credentials inside. Egress proxy allow-list. | Per-step traces, sandbox resource metrics, egress-denial log. | Sandbox image pinned. Deterministic test commands recorded per run. | Run quotas. Teardown on timeout. Runner failure doesn't affect triage. |
| 4 | Branch protection. CODEOWNERS on `.github/`. CI without secrets. Approval bound to issue and SHA. | PR outcome and cost-per-PR metrics. Alerts on policy violations. | Each PR records model, prompt, and tool versions and the trace ID. | Per-repo kill switch. Branch and PR cleanup script, tested. |
| 5 | V1 and protection checks per added repo. | Per-repo cost and acceptance dashboards. Drift alerts on eval scores. | Routing config and eval runs versioned. Feedback enters only via reviewed PRs to the eval set. | Spend caps per repo. Staged widening, one repo at a time. |

## 8. AI layer

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Prompt-injection / guardrail defense | Executor-enforced allow-list, untrusted-data tagging, output filters, no outbound channel | Skeleton basics in Phase 0, full in Phase 1 | V2 |
| 2. Cost + latency budget | Per-run, per-day, and per-repo caps, iteration limits, timeouts, degraded response | Cap in Phase 0, full in Phase 1 | Runaway loop cut at cap, emits metric. Economics in V5. |
| 3. Human-in-the-loop gating | Approval bound to issue and SHA, enforced by the executor. Human merge required. | Phase 1 (used in Phase 4) | Gated action can't run without approval, and is logged. |
| 4. Retrieval | Pinned-SHA code search, duplicate-issue embeddings, CODEOWNERS. Access limited to App-readable repos. | Phase 2 | Expected sources retrieved at a hit-rate threshold on the labeled set (threshold set in Phase 2, UNKNOWN until the set exists), and no out-of-scope repo content returned |
| 5. Model access | Provider abstraction, retries, fallback, structured outputs | Seam in Phase 0, hardened in Phase 2 | Fallback is a config change, and the eval suite passes on it at the V4 bar |
| 6. Memory | Short-term per-run state only | Phase 2 | Run records resume after worker restart. Long-term memory not needed. |
| 7. Orchestration | Single agent loop with tools | Phase 3 | Traced multi-step completion on the eval set. Failed steps surface. |
| 8. Routing | Conditional: cheap model for triage, stronger for resolution | Phase 5, only if V5 cost requires | Routed quality within margin of the strongest model, at lower cost, both paths logged |
| 9. Feedback | Maintainer corrections and PR outcomes into the eval set via review | Phase 5 | Next eval run improves with no regression elsewhere |

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Autonomous merge or auto-close of issues:** pulled forward only by a new gate with a named approver, after sustained V5-level acceptance and a rollback path.
- **Public repos and stranger-authored issues:** pulled forward when V2 is extended with a stranger-author threat tier and the owning team requests it.
- **Long-term memory:** pulled forward if eval shows cross-issue context lifts resolution quality.
- **Multi-agent orchestration:** pulled forward if single-loop completion on the eval set is below target and failure analysis shows a planning cause.
- **Fine-tuning or self-hosted models:** pulled forward if V1 fails or cost per accepted PR can't meet budget with routing.
- **Multi-tenant or multi-org operation:** pulled forward by a second org requesting it, which re-opens tenancy as an R3 decision.
- **Cross-repo changes:** pulled forward when single-repo acceptance holds and users ask for it.

**Next step:** Start the organizational items now: the GitHub App approval, the V1 review, and the eval-set labeling. They have the longest lead time.
