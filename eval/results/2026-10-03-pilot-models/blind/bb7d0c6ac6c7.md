# BUILD PLAN: CI/CD platform for 50 microservices

## 1. Classification and constraints
- **What:** A shared CI/CD platform: build, test, scan, sign, promote and deploy for 50 microservices, with progressive delivery and rollback.
- **Type:** Infra + software. Brownfield (ASSUMED: 50 services already ship through existing pipelines). Not a small build: many teams, and it holds production deploy rights.
- **Dominant constraint:** Correctness and security of what reaches production, then availability of the deploy path.
- **Worst failure:** A compromised or misconfigured pipeline pushes an untrusted or broken artifact to many services at once. Stolen pipeline credentials and a shared-template bug both lead there. V1 and V2 guard against it.

| Budget | Target | Label | Checked by |
|---|---|---|---|
| PR feedback time p95 | No worse than the old pipeline | BASELINE (measured in Phase 0 through the existing change process) | Phase 0, wave checks in Phase 3 |
| Merge→prod lead time, change-failure rate | No worse than the old pipeline per migrated service | BASELINE (same) | Phase 3 wave exit checks |
| Rollback time | ≤ 5 min from detection | ASSUMPTION: typical for automated canary rollback; confirm with service owners | V0 records the floor; V4 |
| Runner queue time p95 | < 2 min at 2× observed peak concurrency | ASSUMPTION: a developer-wait tolerance to confirm with engineering leads | V3 |
| Platform availability / RTO | No number | UNKNOWN: needs what engineering leadership commits for hotfix capability; required before Phase 2 | Phase 2 break-glass drill |
| CI monthly spend ceiling | No number | UNKNOWN: needs the engineering manager/finance; required before Phase 3 | V3 |
| Operational complexity | Off-the-shelf components only, no bespoke control plane | UNKNOWN: platform team size needed from the eng manager; required before Phase 1 | Phase 2 exit check |

**Missing inputs:**
- Current CI/CD tooling, Git host, and deploy targets. The plan assumes a GitHub-style Git host and Kubernetes for all 50 services. If a sizable share runs on VMs or serverless, the pull-based deploy row (§3) and Phase 2 change.
- Compliance regime (SOC 2, PCI, etc.) and whether any service handles regulated data.
- Number of teams, deploy frequency, and which services are tier-1.
- Whether a change board governs production changes.

## 2. Dependency map
| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 start | Cloud/cluster accounts, a pilot team that agrees, a change-board slot if one exists | organizational | deployed |
| Every wave after the pilot | Service inventory: pipelines, manual steps, cron/batch jobs and shared credentials per service (ASSUMED until Phase 0 confirms) | decision | specified |
| Pipeline templates, admission policy, registry policy | Trust-model default and sign-off (V1) | decision (R3) | specified |
| Any service beyond the pilot | Admission enforcement working (V2) | risk/security | exposed |
| Wave sizes | Runner capacity and cost (V3); spend ceiling supplied | validation + economic | scaled |
| Automatic promotion | Rollback analysis works (V4) | validation | hardened |
| Removing old pipelines | Rehearsal, named approver, go/no-go (V6) | risk/security | committed |
| Migrating services with database changes | Expand/contract migration convention in the template | structural | built |

No runtime dependency is surprising. Brownfield dependencies are ASSUMED until the Phase 0 inventory marks each CONFIRMED.

## 3. Tradeoff gates (resolved up front)
| Decision | Reversibility | Default | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Trust boundary: artifact signing and provenance | R3 (trust boundary; a mistake exposes prod, and changing the root means re-signing everything) | Keyless signing (cosign/Sigstore) with identity bound to the CI OIDC token on protected branches. Kubernetes admission policy (Kyverno) verifies it. No long-lived deploy secrets. | CI issues OIDC tokens; all targets are Kubernetes | V1 (design sign-off), V2 (enforcement) | V1 or V2 fails, or external transparency logs are not permitted → KMS-backed keys |
| Pipeline config contract | R2 (coordinated change across 50 repos) | Versioned reusable templates pinned by tag, two majors supported, service-specific overrides only in a small file | ≤ 10% of a service's config is overrides (ASSUMPTION; checked on the pilot plus 2 diverse services in Phase 1) | Phase 1 exit check | More than ~20% of services need forks → split templates per stack |
| Deploy model | R2 (credential model and manifests) | Pull-based GitOps (Argo CD), manifests in a separate config repo; cluster credentials never leave the cluster | Targets are Kubernetes | Inventory in Phase 0 | > 20% of services are non-Kubernetes → add a push adapter for them |
| CI engine, build vs buy | R2 (the CI system) | Buy: Git host's native CI with self-hosted ephemeral autoscaling runners. No bespoke orchestrator. | Queue time and cost are acceptable | V3 | V3 fails → larger or hosted runners, or a different engine |
| Consistency vs availability | R2 | Fail-closed on signature/policy for deploys, plus an audited break-glass path. Running services do not depend on the CI being up. | Hotfix need can be met by break-glass | Phase 2 drill | Break-glass cannot deploy within the agreed RTO → redesign |
| Progressive delivery | R2 | Argo Rollouts canary with automated metric analysis and auto-rollback | Services expose RED metrics | V4 | V4 fails → manual promotion gates stay |
| Migration: coexistence, cutover, rollback | R2 (coordinated across teams) | Replatform in waves. Each service has exactly one active deploy path, set by a per-service flag. Before the flip, the old pipeline is the system of record for what is deployed; after it, the config repo and cluster are. Rollback is flipping the flag back. | The old pipeline keeps working during migration | Phase 3 wave checks | A wave's change-failure rate is worse than BASELINE → pause and fix before widening |

**R1 defaults:** scanner → Trivy, report-only at first; registry → existing cloud registry with immutable tags; IaC → Terraform; build caching → remote cache; build/queue → async, deploy gated on health.
**N/A:** Monolith vs services: the platform is composed of off-the-shelf parts with no bespoke service. Data-privacy boundary: the platform processes no personal data by design (test stages use synthetic data, ASSUMED). Logs are secret-masked from Phase 0.

## 4. Walking skeleton (Phase 0)
- **Request:** One real commit to one low-criticality, real pilot service goes through the new path to production. The path is: checkout → build → unit test → image push → sign → manifest commit → Argo CD sync → canary → health check → promote. The response is the pilot serving traffic on the new version. One rollback is then done via `git revert` and Argo rollback.
- **Tiers:** Git host, CI runner, registry, config repo, Argo CD, cluster namespace, service, metrics/trace backend.
- **Deployed:** By the pipeline itself. Platform infrastructure is Terraform in Git.
- **Logged and monitored:** OpenTelemetry trace spans per pipeline stage, linked to the deployment. Secret-masked logs. Alerts on pipeline failure and failed sync. A deploy annotation on the pilot's dashboard.
- **Rolled back:** By reverting the config commit. The old pipeline stays the default and the fallback.
- **Who can reach it:** Only the pilot team, through an opt-in flag on that one service. Deploy rights are scoped to the pilot's namespace. Unvalidated defaults, not hardened.
- **Baselines:** Measure the old path's lead time, build time, failure rate and queue time through its change process, and record the new path's first values as floors. Run the service inventory in parallel.

**Exit check (V0):** A real commit deploys with one trace across every tier. One deploy and one rollback succeed through the pipeline. An injected failure (a failing test and a bad image) fires an alert. First budget values are recorded as baselines.

## 5. Phases
- **Phase 1 — Trust and contract foundation** (widest rework first)
  - Unlocks: Onboarding any service beyond the pilot.
  - Depends on: V0, V1.
  - Tasks:
    1. Pipeline template contract, versioned and pinned, with the expand/contract migration convention and a separate migration step.
    2. OIDC workload identity with per-service scoped deploy rights, replacing any shared credentials.
    3. Signing and provenance. Admission policy runs in audit mode, then enforce in staging, then enforce in the pilot namespace.
    4. Secrets-manager integration and log masking.
    5. Registry policy (immutable tags, retention).
    6. Trial the template on 2 diverse services in staging only, no prod exposure.
  - Rollback: Set admission to audit mode. Pin the previous template version.
  - Exit check: V2 passes. The override-size check from §3 holds on the pilot plus the 2 staging services.
- **Phase 2 — Scale the substrate**
  - Unlocks: Wave migration.
  - Depends on: Phase 1, V2; the spend ceiling and RTO inputs supplied.
  - Tasks:
    1. Ephemeral autoscaling runner fleet with caches.
    2. Progressive delivery with automated analysis.
    3. Promotion flow from staging to prod.
    4. Platform SLIs, SLOs and burn-rate alerts.
    5. Break-glass path with audit, and a drill.
    6. Platform backup/restore of config and state.
  - Rollback: Route builds back to the previous runner pool. Manual promotion replaces auto-promote.
  - Exit check: V3 and V4 pass. A break-glass hotfix deploy succeeds within the agreed RTO (an exit check, since nothing downstream changes if it fails).
- **Phase 3 — Migration waves** (exposure smallest first)
  - Unlocks: Full coverage, with old pipelines still able to take over.
  - Depends on: Phase 2, V3, V4.
  - Tasks:
    1. Wave A: 3 low-tier services across different stacks.
    2. Wave B: about 12 services.
    3. Wave C: the remaining non-tier-1 services.
    4. Wave D: tier-1 services, last.
    - For each service: a staging parity run, then the prod flag flip, with the old pipeline kept runnable.
  - Rollback: Per-service flag flip back to the old path. Old pipelines are untouched until Phase 4.
  - Exit check: Each wave is held until change-failure rate and lead time are no worse than BASELINE, with no incident attributed to the platform, before the next wave starts. All 50 services are on the new path.
- **Phase 4 — Decommission old pipelines**
  - Unlocks: A single deploy path.
  - Depends on: Phase 3, V6.
  - Tasks:
    1. Dry run of the removal.
    2. Revoke old deploy credentials.
    3. Disable old jobs.
    4. Archive their config.
  - Rollback: Not reversible once credentials are revoked and jobs deleted. The point of no return is the credential revocation, guarded by V6. The archived config allows a rebuild, not an instant revert.
  - Exit check: V6 passes. No deploys occur via old paths for one full release cycle (ASSUMPTION: 2 weeks).

## 6. Validation gates
| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | §4 skeleton run | The V0 exit check in §4 | Trace export, pipeline run log, alert-fire record, baseline sheet in the platform repo | Phase 1 | 0 |
| V1 | The signing and identity design prevents a deploy from any source other than the protected-branch pipeline | Threat-model review of the design, covering token theft, forked PRs, template injection and runner compromise | Named security reviewer's pass/fail sign-off, with no open high findings | Signed design doc and threat model in the platform repo | Specifying templates, admission policy and registry policy; if it fails: fall back to KMS-backed keys or re-plan | Starts in 0, required before 1 |
| V2 | Admission rejects everything not built by the platform pipeline, without rejecting good deploys | Negative tests: unsigned, wrong identity, tampered digest, unprotected branch, replayed old signature. Then 20 consecutive good pilot deploys. | 100% of ≥ 10 negative cases rejected; 0 false rejects in the 20 (design parameters) | Test-suite run report in CI | Any service beyond the pilot; if it fails: stay at audit mode and fix | 1 |
| V3 | Runner fleet meets queue time and cost at scale | Replay recorded peak merge load at 2× concurrency across all 50 services, for a full working day | p95 queue < 2 min (ASSUMPTION); projected monthly cost ≤ ceiling (UNKNOWN until the eng manager/finance supplies it, before Phase 2) | Load-replay report and cost projection | Wave sizes; if it fails: resize, or flip the CI-engine row (§3) | 2 |
| V4 | Automated analysis catches bad releases and does not block good ones | Inject faults (5xx, latency) into pilot canaries, plus 30 clean deploys | Rollback within 5 min of fault onset (ASSUMPTION); ≤ 1 false rollback in 30 clean deploys (ASSUMPTION) | Rollout analysis logs | Auto-promotion; if it fails: manual promotion gates stay | 2 |
| V6 | Old pipelines can be removed without losing a deploy capability | Rehearsal in a copy of the environment, with an inventory check that no job, cron or batch dependency remains | Zero remaining dependencies in the inventory; named approver (engineering lead) go/no-go; archive restored once in rehearsal | Rehearsal record and approval | Phase 4 execution; if it fails: keep old pipelines and re-plan | Starts in 3, required before 4 |

## 7. Cross-cutting concerns
| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | Pilot-scoped deploy rights, secret masking, allow-listed flag | Traces per pipeline stage, failure alerts, baselines | Platform infrastructure as Terraform in Git | Old pipeline is the fallback; one rollback done |
| 1 | OIDC identity, signing, admission audit→enforce (V2) | Admission decisions logged and alerted | Pinned template versions, immutable image tags | Admission can revert to audit mode |
| 2 | Break-glass is audited and alerted | Platform SLIs/SLOs, burn-rate alerts, queue-time metrics | Runner images built from code and pinned | Autoscaling runners, platform backup/restore, break-glass drill |
| 3 | Per-service least-privilege rights as each service flips | Per-wave dashboards comparing against BASELINE | Config repo is the deploy state of record | Per-service flag rollback; waves paused on regression |
| 4 | Old credentials revoked (V6) | Alert on any deploy via a legacy path | Archived old config | Rehearsed removal, archive for rebuild |

## 8. AI layer
N/A — no AI component.

## 9. Methodology exceptions
None.

## 10. Deliberately deferred
- **Multi-region platform HA:** pulled forward when the RTO input requires it, or when the Phase 2 drill shows a regional outage would block hotfixes.
- **Per-PR preview environments:** pulled forward when teams ask for them after Phase 3 and V3 shows spare runner cost headroom.
- **Blocking vulnerability gates** (scanner starts report-only): pulled forward when a compliance regime requires them, or when the report-only false-positive rate is known.
- **Developer portal / self-service onboarding UI:** pulled forward when onboarding new services after the 50 becomes a recurring load on the platform team.
- **Pipeline chaos testing:** pulled forward after the first platform-attributed incident.
