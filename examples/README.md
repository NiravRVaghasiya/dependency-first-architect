# Examples: the same prompt, with and without the skill

The five fixed prompts from [`reference/evals.md`](../reference/evals.md). Each was answered once
by `claude-opus-5-5` in a clean, isolated session in Claude Code without the skill, and once with
it. Nothing was edited or cherry-picked: each plan's SHA-256 is recorded, and
`scoring/tally.py --check` fails if a committed plan changes. Each plan was scored by
3 judges who were not told which arm wrote it. The breakdown, method and caveats are in
[SCORECARD.md](SCORECARD.md).

<!-- BEGIN GENERATED examples index: examples/scoring/tally.py -->
| Prompt | Without the skill | With the skill | Delta |
|---|---:|---:|---:|
| **P1** RAG support chatbot | [12/20](p1-rag-chatbot/without-skill.md) | [20/20](p1-rag-chatbot/with-skill.md) | +8 |
| **P2** Multi-tenant SaaS billing | [8/20](p2-saas-billing/without-skill.md) | [20/20](p2-saas-billing/with-skill.md) | +12 |
| **P3** CI/CD for 50 microservices | [17/20](p3-cicd-platform/without-skill.md) | [20/20](p3-cicd-platform/with-skill.md) | +3 |
| **P4** GitHub issue agent | [16/20](p4-github-issue-agent/without-skill.md) | [20/20](p4-github-issue-agent/with-skill.md) | +4 |
| **P5** Collaborative doc editor | [11/20](p5-collab-editor/without-skill.md) | [20/20](p5-collab-editor/with-skill.md) | +9 |
| **Mean** | 12.8 | 20.0 | +7.2 |
<!-- END GENERATED -->

## The biggest gap: multi-tenant SaaS billing

> *"Architect a multi-tenant SaaS billing system."*

Without the skill, the model writes an expert reference architecture: integer money, an
append-only ledger, idempotency everywhere, tenant-first keys. The knowledge is not the problem.
The order is. The answer is organized by component, security gets its own section, the phasing is
a short list near the end, and the word "production" never appears. With the skill, much of the
same knowledge comes back as a build sequence that has a real invoice running in production before
any feature work starts.

| | Without the skill | With the skill |
|---|---|---|
| **Shape** | A component tour: §4 Multi-tenancy, §5 Domain model, §6 Core components (6.1–6.8), §7 Reliability patterns, §8 Scaling, §9 Security & compliance. Phases first appear in §11, "Build vs. buy, and phasing". | 8 Tradeoff gates (plus 2 AI gates marked N/A), then Phase 0, then 9 phases. Each phase states what it *Unlocks*, what it *Depends on*, its *Tasks* and its *Exit check*. |
| **First thing built** | "1. **Foundation:** versioned catalog, flat and per-seat subscriptions, cards, invoices, basic dunning, the tax adapter, entitlements, **and the ledger**." | Phase 0, one real request in prod: "An internal *canary* tenant sends `POST /v1/invoices` with an `Idempotency-Key` header" … "Within 60 s the invoice is `paid`, and a signed `invoice.paid` webhook has reached the canary's receiver." |
| **Observability** | Good alerts, scattered through the component sections (§6.3, §6.5, §6.8) and one §7 table row: "Business-level alerts: billing-run delay, drafts stuck > 2 h, payment success rate, reconciliation mismatches". No phase sets up logs, metrics or traces. | In Phase 0: "That gives **one trace from the POST through Stripe to the outgoing webhook**." … "PagerDuty pages on two canary failures in a row, any ledger imbalance, or a job older than 5 minutes." |
| **Security** | Concrete controls in §4.2 (row-level security, per-tenant KMS keys), §6.5 (hosted card fields) and a standalone §9: "**Card security (PCI DSS):** store tokens only, and scrub card numbers from logs." None is assigned to a phase. | A column in every phase. Phase 0: "TLS-only load balancer with WAF; AWS Secrets Manager; minimal permissions per service; gitleaks and Trivy in CI" … Phase 1: "Forced RLS and tenant-first foreign keys; hashed API keys; 404 rather than 403" |
| **Irreversible decisions** | Mostly stated as fixed design principles: "**No floats, ever.**" "**Financial records are append-only.**" | Each gate has a default and a flip condition. Funds flow: "**We never hold customer money.**" Flips: "If the business decides to earn money on payment volume or run payouts, we become a payment facilitator" |
| **The UI** | "Customer portal" is a box in the architecture diagram; it is in no phase. | Last: "Phase 9: User-facing surfaces (dashboard, hosted invoice page, customer portal, checkout)". "The UI is just another client of it." |
| **What waits** | A few conditions in passing: "only shard (Citus, by `customer_id`) when you need to." | A "Deliberately deferred" list, each with a trigger: "**Temporal:** adopt when timer-driven workflows start causing bugs, or there are more than about 5 hand-written state machines." |

Full plans: [without the skill](p2-saas-billing/without-skill.md) ·
[with the skill](p2-saas-billing/with-skill.md)

## What the plans without the skill already do well

Being fair to the baseline makes the gap easier to trust. Across all five prompts the model,
without the skill, mostly orders work by dependency already (dimension D1 in the scorecard) and
handles failure well (D8). What it misses most often is a thin slice running in production first
(D2), tradeoffs with explicit flip conditions (D3), and reproducibility (D7). Per-dimension numbers
are in [SCORECARD.md](SCORECARD.md).
