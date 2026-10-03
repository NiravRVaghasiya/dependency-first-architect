# Plan: Real-time collaborative document editor

## 1. Classification and constraints
- **What:** A web-based rich-text editor where many users edit the same document at once. Each user sees the others' edits and cursors live, and documents are stored on the server for each workspace (tenant).
- **Type:** Software. Greenfield. Multi-tenant SaaS. Not a small build: it faces outside users, holds customer content, and has several R3 decisions.
- **Dominant constraint:** Correctness. Every copy of a document must end up identical, and an edit the server has acknowledged must never be lost. Latency comes second.
- **Worst failure:** Customers' acknowledged edits are silently lost or their copies drift apart. Users believe their work is saved when it isn't. Guarded by V1 and V2. The second-worst failure, cross-tenant exposure of a document, is guarded by V3.

**Budgets**

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency: local keystroke echo | < 16 ms (one frame). Edits apply locally first | ASSUMPTION: basis is perceptible input lag | Phase 5 exit check |
| Latency: remote edit propagation, same region | p95 < 200 ms, p99 < 500 ms | ASSUMPTION: basis is the usual threshold where collaboration feels live; product to confirm | V4 |
| Throughput: editors per document | 50 concurrent | ASSUMPTION: basis is caps in comparable tools (~100); product to confirm | V4 |
| Throughput: peak concurrent connections | UNKNOWN: product/sales forecast needed by the end of Phase 1 | UNKNOWN | V4 |
| Availability / SLO | 99.9% monthly for the sync tier and the API | ASSUMPTION: basis is standard B2B SaaS tier | Phase 6 SLO alerts |
| RPO / RTO | RPO 0 for acknowledged edits (unacknowledged edits are re-sent from the client buffer); RTO 1 h for a full database restore | ASSUMPTION: basis is the design intent that the server acks only after a durable commit | V2 |
| Revocation latency | A revoked user stops receiving and sending updates within 5 s | ASSUMPTION: product to confirm | V3 |
| Resource use: document size | 1 MB of visible content with 1M operations of history; cold load p95 < 1.5 s on the reference client | ASSUMPTION: basis is long-lived team documents | V5 |
| Storage cost | Stored bytes ≤ 3× visible content after compaction | ASSUMPTION: basis is CRDT deletion-history overhead, kept bounded by compaction | V5 |
| Infrastructure cost per active document | UNKNOWN: product/finance supply it from the pricing model, needed before Phase 4 | UNKNOWN | V5 |
| Operational complexity | 3 services (web/API, sync, worker) plus managed Postgres and Redis; one on-call rotation | ASSUMPTION: basis is team size, not yet confirmed | Phase 6 |

**Missing inputs** (the first three could change the phase order; each assumption is stated with what changes if it is false):
1. **End-to-end encryption required?** Assumed **no**. If yes, the server can no longer read documents. It becomes a relay and blob store, server-side snapshots, compaction and export move to the client, and the trust-boundary row in §3 flips before Phase 1.
2. **Regulated data or data residency (HIPAA, GDPR regions)?** Assumed **no**. If yes, legal obligations (such as a HIPAA business associate agreement) and residency become dependencies that block Phase 0 itself. Per-region "cells" (a separate deployment per region) also move up from §10 into Phase 4.
3. **Standalone product, or embedded in an existing product with its own identity and tenancy?** Assumed **standalone**. If embedded, this becomes brownfield work: the existing identity and tenancy become constraints, Phase 3 shrinks, and Phase 0 must first map what depends on the host system.
4. **Offline editing:** assumed short only (reconnect buffer). Scale and cost targets: see the UNKNOWN rows above.
5. **Unconfirmed organizational inputs:** cloud account and identity provider (IdP) tenant, pen-test vendor, legal counsel for terms of service, privacy policy and data processing agreement (DPA), a design-partner tenant, on-call staffing.

## 2. Dependencies

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| Phase 0 deploy | Cloud account, IdP tenant, domain/TLS (missing input 5) | organizational | built / deployed |
| Freezing the update-log and snapshot storage format; schema v1 | V1 (the engine plus our editor binding converge) | validation | specified |
| Comment anchors, version history, restore | Snapshot model (Phase 2). Comments anchor to CRDT relative positions, not offsets | structural (surprising) | built |
| Hardening how the sync tier routes and fans out | V4 | validation | hardened |
| Any external user, or any real customer document | V2 (durability), V3 (isolation, authorization, no content in telemetry) | risk-security | exposed |
| External beta | Pen-test vendor contract (lead time: start in Phase 1); legal terms, privacy policy and DPA; on-call rotation; design-partner tenant | organizational | exposed |
| Schema changes once outside clients exist | V6 | validation | deployed |
| Capacity commitment and pricing; widening past the design partner | V4, V5, cost target (UNKNOWN, see §1) | economic | committed / scaled |

All seven kinds bind. Runtime dependencies follow the phase order, and the only surprising structural one is listed above.

## 3. Key decisions (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Revisit trigger |
|---|---|---|---|---|---|
| Merge model: CRDT vs OT | R3: the persisted update format and the client protocol sit inside every stored document and every open client | CRDT: Yjs with y-prosemirror (conflict-free data type; no central server needed to order edits) | Yjs plus our schema always converges, never produces a schema-invalid document, and its deletion history fits V5 | V1, V5 | V1 finds an engine-level divergence that can't be fixed in about 2 weeks, or V5 fails because of deletion-history growth → server-authoritative OT (operational transform: prosemirror-collab steps through a per-document central sequencer) |
| Consistency vs availability | R3: it defines what "saved" means to customers | Edits: local-first, eventually consistent. "Saved" = acknowledged after a durable Postgres commit. Permissions and membership: strongly consistent, checked on connect, with revocation pushed to live sessions | A synchronous commit before the ack fits the latency budget | V2, V4 | V4 shows that committing before the ack breaks the p95 → group commit (50 ms window), still acking only after commit. Never ack before durable |
| Document data model and identifiers | R3: stored customer data | Append-only update log plus periodic snapshots in Postgres (`bytea`). UUIDv7 document IDs. `schema_version` on each document. `tenant_id` on every row | Postgres keeps up with per-document write rates | V2, V5 | V4/V5 show write load above Postgres headroom → log store (object storage plus snapshots) behind the same persistence interface |
| Tenancy and isolation | R3: isolation is hard to retrofit once real data exists | Pooled Postgres (shared by all tenants) with `tenant_id` + row-level security (RLS) from the first table, and tenant-scoped authorization on every WebSocket message | No customer requires dedicated isolation | V3 | V3 fails, or a contract requires dedicated isolation or residency → schema- or database-per-tenant cells |
| Data-privacy / trust boundary | R3: adding E2EE later means re-architecting | The server can read documents (needed for snapshots, compaction, export). TLS in transit, encryption at rest. Document content never goes into logs, traces or error reports | No E2EE requirement (missing input 1) | V3 (canary-string scan plus security sign-off) | E2EE becomes a requirement → client-side encrypted updates and a server-as-relay redesign |
| Monolith vs services | R2: splitting later takes weeks | Modular monolith (web/API) + separate sync service + worker | Long-lived WebSocket connections scale and deploy differently from HTTP traffic | V4 load profile | V4 shows one process handles projected load with ≥ 3× headroom → merge sync into the monolith |
| Sync vs async | R2: protocol and outbox changes cross services | WebSocket for edits and presence. Persistence is synchronous before the ack. Side effects (search, notifications) are asynchronous via a transactional outbox | Side effects can tolerate seconds of lag | Phase 5 exit check | A side effect needs read-your-writes → synchronous call for that effect only |
| Sync-tier fan-out | R2: routing change across sync and load balancer | Document affinity: all connections for one document land on one sync node via consistent hashing at the load balancer. Redis carries presence only | No single document outgrows one node | V4 | V4: a hot document saturates a node, or reconnect after node loss takes > 10 s p95 → Redis pub/sub fan-out across nodes |
| Build vs buy | R2: the Yjs wire protocol is a standard, so the server can be swapped | Adopt open-source and self-host (Yjs, Hocuspocus, TipTap). Not a hosted provider (Liveblocks, Tiptap Cloud) | The libraries stay maintained and MIT-licensed; the team can staff on-call for the sync tier | License review plus a Phase 0 spike | No on-call staffing for the sync tier → hosted provider. That move is R3 (data held at the vendor) and needs a residency and DPA review first |
| Identity | R2: a seam keeps it reversible | Buy an OIDC IdP. Internal `user_id` mapped to the IdP subject (the seam is built in Phase 3) | Standard OIDC is enough; SSO can come later | Phase 3 exit check | An enterprise SSO/SCIM demand that the IdP can't meet → change IdP behind the seam |
| Offline scope | R2: client storage and protocol | Short reconnect buffer: unacknowledged updates kept in IndexedDB and re-sent | Users rarely edit offline for long | Phase 5 exit check | A product requirement for long offline editing, or telemetry showing buffer overflow → offline-first (§10) |

**minor defaults:** TypeScript for client and sync; React + TipTap; managed Postgres and Redis (RDS / ElastiCache or equivalent); ECS or Kubernetes; GitHub Actions; Terraform; OpenTelemetry + Grafana stack; OpenFeature flags; Sentry for client errors, with content scrubbing.
**N/A:** Migration, cutover and system-of-record rows: greenfield. Prompt+RAG vs fine-tune and hosted vs self-hosted model: no AI component.

## 4. First end-to-end slice (Phase 0)
- **Request:** Staff user A signs in through OIDC, creates a document and types "hello". Staff user B, in a second browser, sees it live. After the sync node restarts, a reload shows "hello" loaded from Postgres.
- **Tiers:**
  - Browser: React + TipTap + Yjs.
  - CDN for static assets.
  - API: OIDC callback, document metadata, a short-lived WebSocket token.
  - Load balancer with WebSocket support.
  - Sync service: one Hocuspocus node.
  - Postgres: metadata and update log.
  - IdP.
- **Deploy:** GitHub Actions builds images pinned by digest, deploys to staging, then to production. Infrastructure is provisioned with Terraform. Rolling deploys drain WebSocket connections and clients reconnect.
- **Logs:** Structured JSON with no document content.
- **Tracing:** One OpenTelemetry trace spans browser → API → WebSocket → Postgres; trace context rides as metadata on WebSocket messages.
- **Monitoring:**
  - Dashboards for connections, propagation latency and persist errors.
  - Alerts on persist failures and WebSocket error rate.
  - A synthetic probe edits a canary document every minute and measures propagation.
- **Rollback:** Redeploy the previous image digest. Database migrations only add things (no destructive changes).
- **Who can reach it:** Only allow-listed staff accounts, behind a feature flag in both the IdP and the app. Internal throwaway documents only, no customer data. The skeleton runs on unvalidated defaults and is not hardened.

Exit check: V0.

## 5. Phases

- **Phase 1: Document model and convergence**
  - Unlocks: A frozen storage contract and schema v1 that everything else is specified against.
  - Depends on: V0.
  - Tasks, in order:
    1. Tenancy contract: `tenant_id` + RLS on every table.
    2. Update-log and snapshot schema, with `schema_version`.
    3. ProseMirror schema v1.
    4. Fuzz harness for V1, plus server-side validation that rejects malformed or schema-breaking updates.
    5. Production check: clients report a state-vector hash so divergence shows up as a metric and an alert.
    6. Get the peak-connection forecast and the cost target from product/finance.
    7. Sign the pen-test vendor and start the legal review (both have lead time).
  - Rollback: Only internal throwaway documents exist, so drop and re-create them. This is why contracts are fixed before exposure.
  - Exit check: V1 passes; the divergence metric is live.
- **Phase 2: Durable persistence and recovery**
  - Unlocks: Edits that can be trusted as saved; restore after corruption.
  - Depends on: Phase 1, V1.
  - Tasks:
    1. Ack-after-commit protocol, with idempotent apply of updates.
    2. Client IndexedDB buffer that re-sends unacknowledged updates.
    3. Snapshots and compaction in the worker.
    4. Point-in-time recovery (PITR) and encrypted backups.
    5. Per-document version snapshots and restore.
    6. Chaos harness and restore drill.
  - Rollback: Redeploy; rebuild any document from its log (internal data only).
  - Exit check: V2 passes.
- **Phase 3: Identity, authorization, tenancy enforcement** (can run in parallel with Phase 2; depends only on Phase 1)
  - Unlocks: Access control that is safe for outside users.
  - Depends on: Phase 1.
  - Tasks:
    1. Mapping from IdP subject to internal user.
    2. Workspaces and roles: owner, editor, commenter, viewer.
    3. Authorization on every WebSocket message (the server drops updates from viewers).
    4. Revocation pushed to live sessions.
    5. Scoped, expiring share links.
    6. Rate limits.
    7. Metadata-only audit log.
    8. Authorization matrix tests and canary-string scan of telemetry.
    9. Security review.
  - Rollback: Policy changes are versioned and revertible. It fails closed: when in doubt, deny.
  - Exit check: V3's automated parts pass. The pen test runs against the Phase 5 build.
- **Phase 4: Scale-out and performance**
  - Unlocks: Multiple sync nodes; capacity and cost numbers.
  - Depends on: Phases 2 and 3; the inputs gathered in Phase 1.
  - Tasks:
    1. Consistent-hash document affinity at the load balancer.
    2. Presence over Redis.
    3. Rebalance and reconnect when a node is lost.
    4. Limits on document and update size.
    5. Load tests (k6 with recorded editing traces).
    6. Large-document and compaction benchmark.
  - Rollback: Collapse to one sync node; Redis presence is optional.
  - Exit check: V4 and V5 pass, or their flips are taken and re-run.
- **Phase 5: Editor features and collaboration UX** (the visible part, built last)
  - Unlocks: A product worth a beta.
  - Depends on: V1, Phase 2 snapshots, Phase 3 roles.
  - Tasks:
    1. Schema-evolution protocol: version negotiation, forced reload, preserving unknown nodes (V6).
    2. Presence cursors.
    3. Comments anchored to relative positions.
    4. Version history UI and restore.
    5. Paste sanitization and CSP.
    6. Markdown/HTML export.
    7. Outbox events.
  - Rollback: A feature flag per feature.
  - Exit check: V6 passes; local echo is under 16 ms on the reference client.
- **Phase 6: Staged exposure**
  - Unlocks: Real customers.
  - Depends on: V2, V3 (including pen test and sign-off), V4, V6; legal terms, privacy policy and DPA; on-call in place.
  - Tasks:
    1. SLO burn-rate alerts and runbooks.
    2. Kill switch: per-tenant read-only mode plus export.
    3. One design-partner tenant.
    4. After 7 days within SLO with no divergence alerts, widen to 10% of new tenants, then 50%, then GA. Each step waits for the same check. Widening past the design partner also needs V5.
  - **Point of no return:** Once outside customers hold documents, the storage format and schema are committed. This is guarded by V1, V2 and V6.
  - Rollback: Put the cohort's tenants into read-only mode with export, and stop new sign-ups.
  - Exit check: Each widening step meets its 7-day SLO.

## 6. Validation checks

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed and rolled back through every tier in production | The §4 request, plus a pipeline deploy and rollback, plus an injected persist failure | The V0 exit check in §4; first latency and connection numbers recorded as a near-zero-load floor | `ops/evidence/V0/`: trace ID, pipeline run links, alert record, baseline numbers | Phase 1; if it fails, fix the pipeline or telemetry before any other work | 0 |
| V1 | Yjs plus our binding and schema always converges to a valid document | fast-check property fuzz: 2–20 in-process clients making random edits (insert, delete, format, split, join, paste, undo); messages reordered, duplicated, dropped and re-sent; network partitions; a check that snapshot plus log replays to the live state | 0 divergent final states and 0 schema-invalid documents over ≥ 1M operations in ≥ 10k sessions, plus a 24 h soak (ASSUMPTION: zero tolerance follows from the worst failure; sample size chosen to hit rare interleavings) | Fuzz report as a CI artifact; failing-seed corpus in `test/fuzz/` | Freezes the storage contract and schema v1 (Phases 2 and 5); if it fails, fix the binding, or flip the merge-model row to OT | 1 |
| V2 | An acknowledged update is never lost, and every document can be restored | Chaos test in a production-like environment: 50 clients per document; kill -9 of sync nodes, Postgres failover, partitions, disk-full; compare the client-side ledger of acknowledged updates with the persisted log; restore drill from PITR to a new instance, then replay and compare | 0 acknowledged updates lost over ≥ 500 injected faults (RPO 0, ASSUMPTION); unacknowledged edits re-sent and converged; full restore within 1 h with checksums matching (RTO, ASSUMPTION) | `ops/evidence/V2/`: chaos run report, restore-drill log | Phase 6 exposure and hardening of persistence; if it fails, move the ack point (wait for a synchronous replica) and re-plan Phase 2 | 2 |
| V3 | No one reads or writes a document they are not authorized for (across tenants, roles, live sessions after revocation), and no document content leaks into telemetry | Automated matrix: every HTTP endpoint × every WebSocket message type × role × tenant; live revocation test; canary strings placed in documents and scanned for in logs, traces and Sentry; external pen test; security reviewer sign-off | 0 unauthorized reads or writes with 100% coverage of endpoints and message types (ASSUMPTION: zero tolerance); revocation effective within 5 s (ASSUMPTION); 0 canary strings found; no open critical or high pen-test findings, and pass/fail sign-off by the named security reviewer (to be named, missing input 5) | Matrix report as a CI artifact; pen-test report and sign-off record in the security repo | Phase 6 external exposure; if it fails, block exposure; if pooled isolation can't pass, flip the tenancy row | Starts in 3; pen test on the Phase 5 build; required before 6 |
| V4 | The sync tier meets the latency budget at target concurrency using document affinity | k6 WebSocket load test with recorded editing traces: 50 editors on one document; a mix of many documents up to 2× projected peak connections; a node killed mid-run; 30 min sustained | Propagation p95 < 200 ms and p99 < 500 ms (ASSUMPTION); at 50 editors per document (ASSUMPTION) and 2× peak connections (peak is UNKNOWN, obtained in Phase 1); reconnect and resync within 10 s p95 after node loss (ASSUMPTION) | `perf/evidence/V4/`: k6 reports, dashboard exports | Hardening the routing topology; widening past the design partner; if it fails, flip the fan-out row or the monolith row | 4 |
| V5 | CRDT history storage and load cost stay bounded | Heavy-edit documents (1 MB visible content, 1M-operation history): cold load time, sizes before and after compaction, byte-equality of compacted state, cost model | Cold load p95 < 1.5 s on the reference client; ≤ 3× stored overhead; compacted state byte-equal to the original (all ASSUMPTION); cost per active document ≤ target (UNKNOWN from finance, obtained in Phase 1) | `perf/evidence/V5/`: benchmark report, cost sheet | Committing capacity and pricing; widening past the design partner; if it fails, tune Yjs garbage collection, move history to object storage, or flip to OT | 4 |
| V6 | Schema changes roll out safely while older clients are connected | Mixed-version fuzz: clients on schema vN and vN+1 editing the same document, with new node types added | 0 lost or corrupted content over ≥ 100k mixed-version operations (ASSUMPTION); incompatible clients blocked from writing and prompted to reload on their next reconnect | CI report in `test/fuzz/schema/` | Any schema change after the first outside tenant (Phase 6 onward); if it fails, freeze the schema and force a reload on every schema change | 5 |

## 7. Cross-cutting concerns

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | TLS everywhere, OIDC, staff allow-list behind a flag, secrets manager, WebSocket origin check, no customer data | One trace browser → database; content-free logs; synthetic canary-document probe; first alerts | Terraform, lockfiles, image digests, migrations run in CI | Rollback by previous digest; add-only migrations; client reconnect with backoff |
| 1 | RLS on `tenant_id` from the first table; server rejects malformed or oversized updates | State-vector-hash divergence metric and alert | Fuzz seeds recorded; failing runs replay deterministically | Schema validation on the server rejects updates that would make a document invalid |
| 2 | Encrypted backups; restore access limited to named roles | Metric comparing acknowledged with persisted updates; persist latency; compaction lag; backup-failure alerts | Scripted restore drill | PITR; client re-send buffer; idempotent apply; version snapshots |
| 3 | Per-message authorization, revocation, expiring share links, rate limits, audit log | Authorization-denial metrics; metadata-only audit events | Authorization policy as code, with a test matrix | IdP outage: existing sessions run until their token expires; new connections fail closed |
| 4 | Limits on connections and document size against abuse and DoS | Per-node connections; per-document fan-out p95; hot-document detection | Versioned k6 scripts; load environment matches production | Node-loss rebalance; graceful WebSocket drain on deploy |
| 5 | Paste and HTML sanitization; CSP; comment content escaped | Sentry with content scrubbing; usage metrics per feature flag | Schema versions in the repo, with mixed-version tests | Forced reload on incompatible schema; feature kill flags |
| 6 | Pen-test findings closed; incident-response runbook; DPA in place | SLO burn-rate alerts; per-tenant dashboards | Tagged releases; evidence archived per widening step | Canary by tenant; read-only kill switch; restore drill repeated; on-call |

## 8. AI layer
N/A: no AI component.

## 9. Exceptions
None.

## 10. Deferred
Each item is built only when its condition is met:
- **E2EE:** a customer or regulatory requirement (this redesigns the trust boundary in §3).
- **Long offline-first editing:** a product requirement, or telemetry showing the reconnect buffer overflowing.
- **Hard purge of documents and tenant erasure** (only soft delete with retention until then): the first legal erasure obligation or a contract term. It will come with its own gate: a rehearsal on a copy, a named approver, and a declared point of no return.
- **Multi-region cells and data residency:** a residency contract, or V4 latency misses for remote regions.
- **Dedicated per-tenant isolation:** a V3 flip or a contract term.
- **Full-text search, notifications, webhooks, public API** (all built off the Phase 5 outbox): customer demand. A public API becomes an R3 contract when it is built.
- **Native mobile clients; suggestion mode / track changes; Word / Google Docs import:** validated demand after Phase 6.
- **AI writing assistance:** a product request. Prompt-injection defense, a cost cap and human review would come first.
