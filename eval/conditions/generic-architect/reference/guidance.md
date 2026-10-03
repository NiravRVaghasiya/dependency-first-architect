# Planning Guidance and Checklists

Use the sections that are relevant. These are thinking aids, not forms to fill in. Skip items that clearly don't apply, and go deeper wherever the hard parts are.

## 1. Discovery questions

Choose the questions whose answers would change the design. If you can't ask, choose a default and record it as an assumption.

- Who are the users, how many are there, and where are they? Is the system internal, B2B, or consumer-facing?
- What is the busiest realistic hour? How much data arrives each day, and how much is kept in total?
- What is the cost of downtime, of losing data, and of returning a wrong answer? These answers set the reliability and correctness targets.
- Is the system single-tenant or multi-tenant? Do tenants need their data isolated?
- What data is regulated or sensitive (personal, health, payment, or confidential data)? Are there residency rules?
- What must the system integrate with? Are those systems reliable and documented, and who owns them?
- What stack, cloud, and tooling are already in use or required by the organisation?
- How many engineers are there, what are their strengths, and how much operational work can they take on?
- Is there a fixed date? What is the smallest release that would still be valuable?
- What has been tried before, and why didn't it work?

## 2. Making quality attributes measurable

| Attribute | Express it as |
|---|---|
| Latency | Percentile at a given load, e.g. p95 < 250 ms at 100 rps |
| Availability | Percentage over a window plus maintenance policy, e.g. 99.9% monthly |
| Recovery | Maximum time to restore and maximum acceptable data loss |
| Throughput | Sustained and peak rates, with expected growth |
| Correctness | Error budget or accuracy threshold on a defined evaluation set |
| Cost | Monthly run cost ceiling, or cost per user, request, or task |
| Security | Named threats that must be mitigated, plus compliance obligations |
| Evolvability | The changes that must be cheap, e.g. "add a new payment provider in under a week" |
| Time to market | The date by which a specific capability must be live |

If a target was invented, label it as an assumption and say how it will be confirmed.

## 3. Decomposition heuristics

- Draw boundaries around business capabilities and the data they own, not around technical layers.
- Things that change together belong together. Things that change for different reasons should be separated.
- Each piece of data should have exactly one writer. Shared writable tables between components are a warning sign.
- Split into separately deployed services only when there is a clear reason: different scaling needs, separate team ownership, different security zones, or different release cadences. Otherwise keep modules inside one deployable unit, with boundaries enforced in code.
- Keep synchronous call chains short. Use asynchronous messaging where work can be delayed or must survive outages. Accept that this brings eventual consistency and harder debugging.
- Wrap each external dependency behind your own interface, so it can be replaced, faked in tests, and protected from failures.
- Check every boundary with one question: can this component be tested and changed without coordinating with the others?

## 4. Data checklist

- Which component is the system of record for each entity?
- Which operations need atomicity, and do they cross component boundaries? If so, plan for idempotent steps, compensating actions, or an outbox pattern for publishing events reliably.
- What are the main read and write paths, and which indexes, caches, or read models do they require?
- How will the schema change without downtime? Use additive changes first, a backfill, a switch of reads, and only then removal of the old structure.
- What are the retention and deletion obligations, including user deletion requests and legal holds?
- What are the backup frequency, the restore targets, and the date of the last tested restore?
- Which fields are sensitive, and are they encrypted, masked in logs, and access-controlled?
- How will production-like test data be produced without copying sensitive data?

## 5. Choosing technology

- Start with what the team already knows and what the organisation already runs.
- Prefer managed services when the team is small or operational appetite is low.
- Before adopting something new, name the driver it satisfies that the existing option can't, and include the cost of learning it and running it.
- Check for limits that could stop you later: licensing, quotas, regional availability, maximum sizes, rate limits, and whether you can get your data out.
- Note any decision that ties you deeply to one vendor, and record what leaving would cost.
- Avoid adopting several new technologies in the same milestone, because it makes failures hard to diagnose.

## 6. Sequencing heuristics

- **First slice:** pick the most representative user scenario and build only enough of each layer to complete it, deployed through the real pipeline to a real environment. Use hard-coded or stubbed pieces freely, but list them.
- **Spikes:** each one has a question, a time box (typically one to five days), something you can examine at the end, and a result that would change the plan. Schedule them before work that depends on their answers.
- **Critical path:** find the longest chain of dependent work and start it first. Look for long-lead items such as vendor access, security reviews, procurement, data-sharing agreements, and quota increases. Request them on day one.
- **Parallel work:** once the interfaces are agreed, independent components can be built at the same time against contracts or fakes.
- **Milestone size:** aim for milestones that take a few weeks at most and end in something you can demonstrate. Long milestones hide risk.
- **Hardening:** don't leave all security, performance, and operational work to a final phase. Build each in when its component is built, then verify it before launch.
- **Order of migrations:** build the new path, run it in parallel or shadow mode, compare results, move traffic over gradually, and only then retire the old path.

## 7. Writing actionable tasks

A good task has a verb, a specific deliverable, a location, and a "done when" condition that someone else can check.

- Weak: "Implement auth."
- Strong: "Add OIDC login with the organisation's identity provider in `services/api/auth/`. Map provider groups to the `admin` and `member` roles. Done when an integration test logs in a test user from each group and receives the correct permissions."
- Weak: "Set up monitoring."
- Strong: "Emit request count, error rate, and p95 latency per endpoint from the API to the existing metrics stack. Add an alert when the error rate exceeds 2% for 5 minutes. Done when a deliberately injected failure in staging triggers the alert."

Keep each task to a few days of work at most. Split anything larger.

## 8. Security checklist

- Identity: how users and services authenticate, and where sessions or tokens are checked.
- Authorisation: the permission model (roles, attributes, or ownership), enforced on the server side for every request.
- Secrets: stored in a secret manager, never in code or images, and rotatable.
- Encryption in transit everywhere. Encryption at rest for sensitive stores.
- Input validation and output encoding at every trust boundary. Parameterised queries.
- Least privilege for service accounts, infrastructure roles, and AI tool permissions.
- Dependency and image scanning in CI. A plan for applying patches.
- Audit logs for security-relevant actions, protected from tampering.
- The three to five most likely threats for this specific system, each paired with a mitigation.

## 9. Reliability and operations checklist

- A timeout on every network call, retries with backoff and jitter only for idempotent operations, and circuit breaking for fragile dependencies.
- Idempotency keys for operations that may be retried or delivered twice.
- Backpressure and queue limits, plus dead-letter handling and a process for reprocessing failed messages.
- Health checks that reflect whether the system can actually serve requests, not just whether the process is running.
- Alerts based on symptoms users would notice, each linked to a runbook.
- Structured logs with correlation IDs that can be followed across components.
- Deployments that are automated, repeatable, and reversible, with an explicit rollback procedure.
- A capacity plan and a load test at or above the expected peak before launch.
- Clear ownership: who is on call, how incidents are handled, and who approves changes.

## 10. AI and agentic systems checklist

- **Evaluation first:** a representative, versioned evaluation set with clear pass criteria, run automatically whenever prompts, models, or tools change. Include adversarial and edge cases.
- **Simplest design that passes:** start with one model call or a fixed pipeline. Add autonomy, planning loops, or multiple agents only when evaluations show they are needed.
- **Model choice:** weigh quality, latency, cost, context size, and data-handling terms. Keep model access behind an interface so models can be swapped.
- **Prompts and configuration:** under version control, reviewed, and tied to evaluation results.
- **Tools:** narrow, well-described interfaces with validated arguments, least-privilege credentials, and dry-run or confirmation modes for actions that can't be undone.
- **Limits:** a maximum number of steps, a token and spend budget per task, wall-clock timeouts, and loop detection.
- **Human oversight:** approval points before irreversible or high-impact actions, and a clear way to hand off to a person.
- **Untrusted input:** treat retrieved documents, tool outputs, and user content as possible injected instructions. Isolate them, and never let them grant new permissions.
- **Grounding:** for retrieval-based designs, plan how documents are ingested, split into chunks, and refreshed, along with access control on retrieved content and measurement of retrieval quality.
- **Failure handling:** fallbacks for when the model is slow, unavailable, wrong, or refuses to answer, and degraded modes that still work for users.
- **Observability:** log inputs, outputs, tool calls, latency, and cost for each step, with redaction of sensitive data. Sample production traffic back into the evaluation set.
- **Data handling:** what is sent to model providers, how long they keep it, and whether it is allowed to be sent.

## 11. Infrastructure checklist

- All resources defined as code, reviewed, and applied through a pipeline, with no manual changes in production.
- Environments kept as consistent as possible, with the differences between them stated openly.
- Remote infrastructure state with locking. State split so a single change can only affect a limited area.
- Identity, account or project layout, and network design planned first, because they are the hardest to change later.
- Changes promoted from development to staging to production, with automated checks at each stage.
- Quotas, limits, and regional capacity checked early.
- Cost tagging, budgets, and alerts.
- Backup, disaster recovery, and failover procedures, each tested.

## 12. Brownfield and migration checklist

- List what the current system actually does, including undocumented behaviour that users rely on.
- Find a point where requests or data can be split between the old and new systems.
- Decide whether to migrate incrementally, by feature, tenant, or traffic percentage, or all at once, and justify the choice.
- Plan how data stays in sync while both systems run, and how the two will be reconciled.
- Define switchover criteria and the point after which rollback is no longer possible.
- Plan when the old system will be retired. Old systems that are never retired double the operational cost.

## 13. Common failure modes to check against

- The architecture serves a scale or level of flexibility nobody asked for.
- Weeks of foundation work with nothing demonstrable at the end.
- Integration and deployment attempted for the first time near the deadline.
- Several components writing to the same data, so ownership is unclear.
- No plan for schema changes, backfills, or rollback.
- AI behaviour assumed rather than measured.
- Long-lead dependencies discovered late.
- Estimates given as single numbers with no stated assumptions.
- Decisions presented as lists of options with no recommendation.
