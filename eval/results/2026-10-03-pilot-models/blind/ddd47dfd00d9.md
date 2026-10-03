# Build sequence: CI/CD platform for 50 microservices

The guiding principle is to prove each layer on a few services before scaling to all 50. Each phase ends with something usable.

## Phase 0: Foundations (weeks 1–2)
1. **Inventory the 50 services.** Record language, build tool, runtime, data stores, owners, and deploy targets. Group them into 3–5 archetypes, such as Java/Spring, Node, Python, and Go.
2. **Pick pilot services.** Choose 3–4 that cover the archetypes, with one low-risk and one moderately critical.
3. **Make the core decisions:**
   - Source control and branching model (trunk-based is preferred).
   - CI engine (GitHub Actions, GitLab CI, Jenkins, or Tekton).
   - Deploy model (GitOps with Argo CD or Flux, versus push-based).
   - Target platform (Kubernetes, ECS, or something else).
4. **Define success metrics.** Use the DORA metrics: lead time, deploy frequency, change failure rate, and MTTR. Take a baseline now.

## Phase 1: Platform substrate (weeks 2–4)
1. Set up the **container registry and artifact repository** (images, packages, and Helm charts).
2. Build **infrastructure as code** for clusters and environments (dev, staging, prod) using Terraform or similar.
3. Set up **identity and secrets**: a secrets manager, OIDC federation for CI (no long-lived credentials), and RBAC.
4. Set up **basic observability**: logs, metrics, and traces. You need this before automating deploys so you can see failures.

## Phase 2: Continuous integration (weeks 4–6)
1. Build **reusable pipeline templates** (shared workflows or libraries) per archetype. Services should consume them, not copy them.
2. Standardize the stages: build → unit test → lint → package → image build → push.
3. Add **fast-feedback gates**: dependency caching, parallel tests, and a PR-time target of under 10 minutes.
4. Add **versioning and immutable artifacts**: semantic versioning or commit SHA tags, with an SBOM and image signing.
5. **Onboard the pilot services.**

## Phase 3: Security and quality gates (weeks 6–8)
1. Add SAST, dependency scanning (SCA), secret scanning, and container image scanning.
2. Add policy as code (OPA/Kyverno) for admission and compliance.
3. Set severity thresholds. Start in **warn-only mode**, then move to blocking once noise is tuned.
4. Set up test-quality gates: coverage thresholds and contract tests (Pact), which matter a lot with 50 services.

## Phase 4: Continuous delivery (weeks 8–11)
1. Adopt **GitOps**: config repos per environment, with Argo CD or Flux reconciling them.
2. Standardize deployment packaging (shared Helm chart or Kustomize base).
3. Build **automated promotion**: dev (auto) → staging (auto, with integration and smoke tests) → prod (gated or auto).
4. Add **progressive delivery**: canary or blue/green with Argo Rollouts or Flagger, with automated rollback on SLO breach.
5. Define **database migration handling** (expand/contract pattern) and feature flags.
6. Deploy the pilot services to production through the full path.

## Phase 5: Pilot review and hardening (weeks 11–12)
1. Compare the results against the baseline metrics.
2. Fix template gaps, slow stages, and flaky tests.
3. Write the **golden path documentation** and a service onboarding checklist.
4. Run a **rollback and disaster recovery drill**.

## Phase 6: Scale onboarding (weeks 12–24)
1. Migrate services in **waves of 8–10**, ordered by archetype and risk: low-criticality first, then core services last.
2. Provide a **scaffolding tool** (a template repo or an Backstage software template) so onboarding takes hours, not days.
3. Handle **cross-service concerns**:
   - Dependency ordering and shared libraries.
   - Monorepo versus polyrepo change detection, if applicable.
   - Environment capacity and ephemeral preview environments.
4. Decommission legacy pipelines after each wave.

## Phase 7: Optimization and governance (ongoing)
- **Cost and speed:** autoscaling runners, build caching, and test selection.
- **Developer portal** (Backstage) showing service catalog, pipeline status, and ownership.
- **Deployment observability:** DORA dashboards, deploy markers on graphs, and alerting tied to releases.
- **Platform operations:** treat the platform as a product, with an owning team, SLOs, a roadmap, and a feedback channel.
- **Continuous compliance:** audit trails, approvals, and periodic policy review.

## Key dependencies
- Observability must come before automated rollback.
- Templates must come before mass onboarding.
- Security gates should start in warn mode before they block.
- The pilot must succeed before the waves start.

## Common pitfalls
- Onboarding all 50 services before the templates are stable.
- Letting each team customize pipelines, which leads to drift.
- Blocking security gates on day one, which causes revolt and workarounds.
- Neglecting flaky tests, which destroys trust in the pipeline.
- Skipping a platform team or owner.

I can go deeper on any phase or tailor this to your stack if you tell me your cloud, language mix, and current tooling. I can also put it into a Gantt-style timeline or a team-staffing plan.
