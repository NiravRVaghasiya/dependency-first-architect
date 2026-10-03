# Worked examples: dependencies, validation gates, labels, reversibility, budgets, exceptions

Examples for the rules in `SKILL.md`. This file adds no rules of its own. The tables mix different
systems on purpose; they are not one plan.

## 1. Dependency kinds

| Kind | Software | Infra | AI / agentic |
|---|---|---|---|
| Structural | invoicing builds on the ledger schema | clusters build on the network + IAM baseline | the retrieval index builds on the chunk schema + metadata |
| Runtime | the API needs the identity provider up | deploys need DNS and the registry up | the answer path needs the model endpoint and vector store up |
| Decision | keys, schema, and billing wait on the tenancy model | the credential model waits on pull vs push deploys | the data pipeline waits on prompt+RAG vs fine-tune |
| Validation | the partitioning scheme waits on "one primary sustains peak" | the runner-fleet design waits on "hosted runners meet the queue-time target" | answer tuning waits on "retrieval finds the supporting passage" |
| Risk / security | an external beta waits on authorization + tenant isolation | prod admission waits on signed artifacts | agent write access waits on injection containment + a tool allow-list |
| Organizational | live card payments wait on the acquirer confirming PCI scope | the prod cutover waits on a change-board slot | training on customer data waits on a legal review of data use |
| Economic | the pricing model waits on payment fees per transaction | the retention policy waits on storage growth | general availability waits on cost per resolved conversation |

Common misses:

- Treating "the service starts" as evidence that it sustains the load.
- Discovering a lead time (security review, vendor contract, data-processing agreement, production
  account) in launch week — or, worse, in week one of Phase 0.
- Scaling an AI feature before cost per resolved conversation is measured on real traffic.
- Ordering by code imports while a decision is still open, so the code is rewritten when it lands.

## 2. Validation gates

Four independent examples, each from a different system.

| | V1 Capacity (validation) | V2 Retrieval (AI) | V3 PCI scope (risk/security + organizational) | V4 Inference cost (economic) |
|---|---|---|---|---|
| **Hypothesis** | One Postgres primary carries the projected write load at the 12-month data size | For answerable questions, the index returns a supporting passage in the top 5 | Card entry only in the provider's hosted fields keeps our PCI DSS scope at SAQ A (reduced, not zero) | Answers cost less per resolved conversation than the human handling they replace |
| **Method** | Benchmark at 2× projected peak on data sized to the 12-month projection, long enough to include autovacuum and checkpoints | ≥ 200 labeled real questions with known source passages, per index version | Data-flow review against SAQ A eligibility, confirmed with the acquirer or a QSA | Meter cost per turn on shadow traffic, then cost per resolved conversation on a 5% canary for one week |
| **Acceptance threshold** | p99 commit < 50 ms at 2,000 writes/s (ASSUMPTION: projected peak 1,000 writes/s) | hit rate@5 ≥ 0.90 (ASSUMPTION; about ±4 points of sampling error at 200 questions) | Acquirer or QSA sign-off: no card number is stored, processed, or transmitted by our systems; a PAN scan of logs and databases finds none | UNKNOWN: needs the human cost per ticket from the support lead, before Phase 4 |
| **Evidence** | Benchmark report stored with the build | Eval report in CI for each index version | Completed SAQ A and attestation of compliance | Cost dashboard export for the canary week |
| **Unlocks (and if it fails)** | A single-primary data model with sharding deferred; if it fails, the partitioning decision opens | Prompt and answer-format tuning; if it fails, re-chunk or change the embedding model before tuning | Taking live card payments; if it fails, re-scope the card flow | General availability; if it fails, narrow the rollout or route more to humans |

Shadow traffic alone cannot measure cost per resolved conversation: users never see the answers,
so the follow-up turns and resolutions never happen. That is why V4 needs a canary.

A good acceptance threshold:

- is a number with its conditions (load, data shape, duration, sample size) when the property is
  measured, or a named reviewer's pass/fail sign-off on a stated question when it is a design,
  security, or legal judgment;
- carries its label: REQUIREMENT, BASELINE, ASSUMPTION (with its basis), or UNKNOWN (no number);
- can fail within the phase that owns it, and is wider than its own sampling noise;
- has a consumer: if nothing downstream changes when it fails, it is an exit check, not a gate.

Anti-patterns:

- **"Proven because it runs."** Running shows the path exists, not that it is fast, safe,
  correct, or affordable enough.
- **A number labeled UNKNOWN.** "99.9% (UNKNOWN)" is invented precision with a disclaimer. An
  UNKNOWN has no number.
- **Validation theater.** Gates on R1 decisions, gates nobody consumes, or a gate for every AI
  sublayer.
- **Evidence as a result.** "Benchmark showed 42 ms" in a plan written before any benchmark ran.
- **Evidence that leaks.** A parity or benchmark report that copies regulated records into CI;
  store identifiers or counts, not the records.

## 3. Reversibility

| Tier | Examples |
|---|---|
| **R1** | A library behind an interface, internal naming, log message wording, UI styling, the test runner |
| **R2** | Queue technology (R3 if it is the system of record and consumers rely on replay), ORM, an internal service boundary, cache topology, the CI system, a structured log schema once alerts or SIEM rules parse it, the UI framework, fine-tuning a model (weeks of data and eval work; a hosted fine-tune is tied to one provider's base model) |
| **R3** | The data model and identifiers once real data exists, tenancy and isolation, the consistency model, a public API with consumers you cannot update in lockstep, trust boundaries, data residency, data gravity at a vendor, training on customer or personal data (removable only by retraining) |

Cost of being wrong: making a storage bucket public is one click to undo, but the exposure cannot
be undone, so the change is gated like R3. A cutover with rehearsed reverse replication is cheap to
undo, but the hour it runs wrong can lose or double-book records, so it is gated as an
irreversible operation.

Basis instead of a gate: "One Postgres primary at about 50 writes/s, roughly 100× below a single
primary's limits" needs no benchmark gate; "standard practice" alone is not a basis.

Seams that turn R3 into R2 when the plan builds them: expand/contract schema migrations; an
interface in front of the vendor's API plus a tested export of your data in an open format; the
raw event log, with a retention policy and per-subject keys if it holds personal data; dual-write
through an outbox or change data capture, with a backfill and a reconciliation check; a tenant ID
on every row from day one.

## 4. Budgets

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency | Time to first token p95 < 2 s; complete answer p95 < 6 s | ASSUMPTION: typical for hosted-LLM chat; confirm with the product owner | V0 records the floor; Phase 3 exit check at peak |
| Throughput | 50 requests/s at peak | REQUIREMENT: stated in the request | Phase 2 load test |
| Availability / SLO | — | UNKNOWN: needs the availability the business commits to (product owner, before Phase 4); a model API, vector store, and database in series at 99.9% each cap it near 99.7% unless fallback answers count as available | SLIs and burn-rate alerts at Phase 4; attainment over the first 30 days |
| AI inference cost | — | UNKNOWN: needs the human cost per ticket (support lead, before Phase 4) | V4 |
| Operational complexity | One service and one managed database; no self-hosted cluster | ASSUMPTION: a two-person team | Phase 3 exit check |

Rows that do not bind are left out (here, storage cost). Unknowns are fine; fabricated certainty
is not. The walking skeleton measures the first real values, and later gates check against them.

## 5. Methodology exceptions

| ID | Rule bypassed | Why it does not apply | Replacement validation | Evidence required | Resumes when |
|---|---|---|---|---|---|
| E1 | Walking skeleton first (principle 6) | Feasibility is the main risk: nobody knows whether a model can extract invoice fields accurately enough | Two-week offline spike on 300 labeled invoices covering the vendor and layout mix; proves extraction quality only, so integration and deployment risk stay open | Spike report: accuracy per critical field (total, due date, bank details) and the share of invoices needing no correction (thresholds ASSUMPTION) | The spike passes and Phase 0 builds the production skeleton; if it fails, re-scope or stop |
| E2 | Skeleton runs in production (principle 6) | Certified medical device: nothing reaches production before clearance | Skeleton runs in a production-equivalent environment on target hardware, under design controls | Verification records traceable into the submission | Permanent for the certified scope: every release passes change control, and a significant change needs re-submission unless an approved change-control plan covers it; uncertified back-office tools follow the normal order |
| E3 | Dependency order (principle 1) | A regulatory date fixes when the reporting feature ships, before its data source is validated | Build behind a flag against the source's contract, with contract tests, and keep an interim manual report that meets the date | Contract tests pass against the real source, and report totals reconcile with the system of record | The source's validation gate passes; the feature is re-checked before exposure |
| E4 | Cross-cutting moves in every phase (principle 5) | Throwaway prototype answering one product question in two weeks | Containment instead of hardening: synthetic or consented data only, no production credentials or network path, a separate account, a teardown date | The decision record, and the teardown done or the re-plan started | The prototype is chosen to become a product: re-plan from Step 1 |

Not exceptions:

- "N/A — <reason>" for something that does not exist in this system.
- Brownfield work. Example of the normal method: a CI/CD platform for 50 services takes one real
  service through the new platform to production in Phase 0, with its old pipeline kept as the
  fallback, and migrates the rest in waves.
- Reading principle 6 for software you do not operate: a CLI's skeleton is the released package,
  installed the way users install it, renaming a real folder in dry-run mode (writes run on a
  disposable copy until the undo journal's gate passes).
- Data-protection regimes (HIPAA, PCI DSS, GDPR). They add risk/security and organizational
  dependencies, such as a business associate agreement before patient data reaches a new
  vendor, and the skeleton still runs in production on a designated test record.
