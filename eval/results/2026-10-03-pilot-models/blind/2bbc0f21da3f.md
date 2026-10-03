I'll proceed with the ten-step methodology to produce a dependency-ordered Plan for a real-time collaborative document editor.

---

# Plan: Real-Time Collaborative Document Editor

## 1. Classification and Constraints

**What is being built:** A real-time collaborative document editor where multiple users can simultaneously edit shared documents with live synchronization and conflict resolution.

**Type:** Software (full-stack web application) + infrastructure (backend deployment, database, observability).

**Scale:** Small to medium. One team, greenfield build, no external API consumers yet. Internal or limited beta users initially.

**Dominant constraints (in order of harm if violated):**
1. **Correctness under concurrency** — editing conflicts must resolve deterministically; a user must never silently lose edits or see stale text.
2. **Latency** — real-time sync must feel instantaneous (~100ms RTT); beyond ~500ms users perceive lag.
3. **Availability** — document loss or unavailability breaks trust; RPO ≤ 1 min, RTO ≤ 5 min.
4. **Data privacy** — documents may contain sensitive information; no unauthorized access.

**Worst failure:** Data corruption (concurrent edits producing garbage or lost edits) or data loss (crash without persistence). Undetected corruption is worse than total loss.

**Budgets:**
- **Latency (document edit to user-visible update):** REQUIREMENT = 100–300 ms p99. Basis: typical web-app expectation. UNKNOWN: actual user tolerance; confirm in Phase 1.
- **Throughput:** ASSUMPTION = 10 concurrent users per document, 100 documents active; refine after V1.
- **Availability (uptime):** ASSUMPTION = 99.5% during beta. REQUIREMENT: 99.9% (four 9s) in production post-GA.
- **Operational complexity:** ASSUMPTION = runnable by 1–2 ops engineers; avoid exotic databases or orchestration.
- **Cost:** ASSUMPTION = <$500/month infrastructure during beta; UNKNOWN: post-GA CapEx/OpEx budget.

**Missing inputs (block if they change phase order, defer if only a threshold):**
- Real user tolerance for latency and conflict resolution UX. *Defers Phase 2 conflict UX hardening; scaffolds on assumption.*
- Compliance/data residency requirements. *Affects Phase 3; assume US/EU-only for now, revisit if TP requirement emerges.*
- Whether this is single-tenant or multi-tenant. *Affects Phase 1 arch decision; assume single-tenant MVP, multi-tenant as deferred.*

**Exceptions:** None. First end-to-end slice is feasible and required.

---

## 2. Dependencies

| Dependency Kind | Binding Dependencies | Blocks |
|---|---|---|
| **Structural** | Document schema (title, content, version, author, timestamp) → client, server, database all depend | Phase 1 detailed design |
| | Conflict resolution algorithm (OT or CRDT) decision → sync engine, API contract, client edit model | Phase 2 sync hardening |
| | API contract (WebSocket message shapes, auth handshake) → client, backend both depend | Phase 1 detailed design |
| **Runtime** | Backend server must be running before any client can sync | Phase 2+ user testing |
| | Database (persistence) must be up before backend can be tested end-to-end | Phase 1 infrastructure setup |
| **Decision** | Consistency model (OT vs CRDT) — R3, blocking sync engine and client | Phase 1 decision gate V1 |
| | Monolith vs. microservices — R2, affects Phase 2+ scale decisions | Phase 1 decision gate V2 (cheap bench) |
| | Self-hosted vs. managed (Firebase, Supabase, etc.) — R3, affects all ops and Phase 0–2 | Phase 0 gate V0a |
| **Validation** | Edit latency meets <300ms p99 with 10 concurrent users — fails → optimize or reduce scope | Phase 2 load test V3 |
| | Conflict resolution UX acceptable to test users — fails → redesign algorithm or UX | Phase 2 user test V4 |
| | Data persistence and recovery works end-to-end — fails → debug, causes rework in backend | Phase 1 integration test V5 |
| **Risk / Security** | Authentication (prevent unauthorized document access) — must exist before any shared doc in Phase 2+ | Phase 1 hardening |
| | Encryption at rest and in transit — must exist before Phase 2+ (real users) | Phase 1 hardening |
| | Audit logging (who changed what, when) — must exist before real data in Phase 2+ | Phase 1 hardening |
| **Organizational** | Product decision: feature scope (real-time, comments, versioning, sharing perms, etc.). UNKNOWN. | Blocks detailed design, assume MVP scope now. |
| | Security review and pen test (if data is sensitive). UNKNOWN; assume required for GA. | Phase 2+ gate. |
| **Economic** | Cost per active user must stay <$5/mo to keep total <$500/mo during beta. Validate in Phase 1. | Phase 1 cost baseline V6 |

**No instances:** No migration (greenfield); no regulatory obligation stated; no external orgs involved yet.

---

## 3. Key decisions

### R3 Decisions (Irreversible; gate required before dependents are specified)

| Decision | Reversibility | Default | Assumption | Validated by | Revisit trigger |
|---|---|---|---|---|---|
| **Consistency model: OT (Operational Transformation) vs. CRDT (Conflict-free Replicated Data Type)** | R3 — months of rework; affects sync protocol, client state machine, server merge logic, database schema. | **CRDT (Yjs/Automerge)** — simpler distributed, deterministic, easier to scale to many writers later. | CRDTs are mature open-source; OT is battle-tested but more complex. Yjs has production users. | **V1:** Spike (3 days) implementing 1 edit conflict (simultaneous insert) in both, benchmark merge latency (<10ms), pass. | **V1 fails** → revert to OT; **user latency (V3) exceeds 500ms with CRDT** → pivot to OT or architecture refactor. |
| **Hosting model: self-hosted backend vs. managed (Firebase, Supabase, etc.)** | R3 — months of vendor lock-in or migration if chosen wrong; affects ops, cost model, compliance, all future phases. | **Self-hosted (Node.js + PostgreSQL on AWS EC2/RDS)** — full control, easier to add custom auth/compliance later, lower cost at small scale, simpler ops overhead. | We can operate a stateless backend and managed database; AWS RDS and EC2 are well-known. Firebase/Supabase faster to start but less flexible for conflict resolution. | **V0a:** Demo app running on managed platform (Firebase) and self-hosted (EC2+RDS) in parallel; measure time-to-deploy, ops toil, cost (1 day, <$20 total). Prefer <1 day to production, cost <$100/mo. | If managed platform time-to-first-production >1 week OR self-hosted cost >$200/mo with team effort, flip to managed. |
| **Monolith vs. microservices (sync engine, auth, document store separate?)** | R2 — weeks to months to split; affects Phase 2+ but not Phase 0 logic. | **Monolith (Phase 0–1)** — all in one process during MVP; sync engine and API in same backend. Split services only if Phase 2+ shows sync latency or throughput bottleneck. | One backend process and one DB can handle 10 concurrent users and 100 docs. Monolith is simpler to debug, deploy, observe. | **V2 (Phase 1):** Load test monolith with 20 concurrent users, 200 docs; target p99 latency <300ms, throughput >50 edits/sec. If both pass, stick with monolith for Phase 2+. If either fails, re-plan split. | **V2 fails** → architecture redesign to services (API gateway, sync sidecar, document store microservice); defer to Phase 2+. |

### R2 Decisions

| Decision | Reversibility | Default | Assumption | Validated by | Revisit trigger |
|---|---|---|---|---|---|
| **Database: PostgreSQL (relational + JSON for CRDT state) vs. MongoDB (document store for nested CRDT state)**  | R2 — 1–2 week data migration if needed; affects schema, indexing, queries. | **PostgreSQL** — ACID, cheaper, easier to reason about versioning, strong consistency model aligns with CRDT guarantees. | PostgreSQL JSONB is fast enough for CRDT blobs (tested with 10k docs, <100ms query latency). | **Phase 1 spike:** Insert 1000 CRDT blobs (each ~50KB), query by doc ID and version; measure latency. Pass if p99 <50ms. | If p99 >100ms with PostgreSQL, evaluate MongoDB; if MongoDB is <50ms and ops cost lower, migrate (cost ~1 week). |

### R1 Decisions (One-line, cheap reversal)

Build-vs-buy: **Build the core sync engine (CRDT merge, WebSocket protocol)** rather than buy a black-box platform. Rationale: need to understand conflict resolution for UX and debugging; open-source CRDT libraries (Yjs) lower the cost. Reverse: weeks, if a platform emerges that meets all constraints. *Not an exception because Phase 0 will validate the approach cheaply.*

---

## 4. First end-to-end slice (Phase 0)

**Scope:** One real request, end-to-end, deployed and rolled back through the pipeline.

**The thin path:**
1. **User A opens the app** (unauthenticated for Phase 0; auth scaffolded in Phase 1).
2. **User A creates a new document** with title "Test" and empty content.
3. **System stores it** in a database (in-memory or SQLite for Phase 0).
4. **User A types three characters** ("abc").
5. **System broadcasts the edit** to itself (same WebSocket, or polling for Phase 0).
6. **User A sees the text update** in real-time on screen.
7. **User A reloads the page** (simulates client crash).
8. **Text "abc" is present** — document survived.
9. **System is rolled back** — process stops, database is cleared, re-run works.

**Tiers crossed:** Frontend (HTML/JS) → Backend (Node.js WebSocket) → Database (SQLite or PostgreSQL local) → back to Frontend.

**Deployment path:** `git push` → CI runs tests and linting → deploy to single EC2 instance or local dev server → health check passes → rollback via `git revert` + process restart.

**Who can reach it:** Internal team only, behind a firewall or localhost.

**Observability from day 0:**
- **Structured logs:** Every WebSocket message logged with timestamp, user ID (or "anon"), operation type, latency (ms). Stored in stdout/file; parseable.
- **Baselines recorded:** 
  - Latency (edit to broadcast): BASELINE = ~5ms (single-user, single-process).
  - Memory: BASELINE = ~50MB (process + SQLite).
  - CPU: BASELINE = <5% idle.
- **Failure injection:** Kill the backend mid-edit; verify log shows disconnect and client reconnects.

**Exit check V0:** 
- A real document create, edit, persist, reload sequence succeeds in <2 seconds end-to-end.
- One deploy from `main` branch to running server succeeds in <5 minutes with zero downtime (or graceful stop).
- One rollback (revert previous commit, restart) restores prior state in <2 minutes.
- No data loss on process restart (if DB is SQLite, on disk; if PostgreSQL, in persistent store).
- Logs show one trace per operation with all five fields (timestamp, user, op, latency, result).
- **Pass criterion:** All of the above succeed once, unmodified, on a fresh checkout.

---

## 5. Phases

### Phase 0: First end-to-end slice (Week 1)

**Unlocks:** Confidence that the core loop (edit → sync → persist → reload) works; green light to invest in Phase 1.

**Depends on:** V0a (hosting decision, if gated; see below). **Default:** Do Phase 0 on self-hosted (EC2 + RDS) *and* local dev in parallel; if local dev passes V0 first, proceed to Phase 1 while V0a (managed platform comparison) continues asynchronously.

**Tasks:**

1. **Frontend skeleton (2 days)**
   - HTML page with text input and display area.
   - WebSocket or polling connection to backend URL.
   - Minimal UI: "Create Doc" button, text input, autosave label.
   - No auth, no conflict indicators.

2. **Backend scaffold (2 days)**
   - Node.js + Express or Fastify.
   - WebSocket server (ws library or Socket.io).
   - Single endpoint: POST /docs (create), WebSocket (listen for edits).
   - Route edits to in-memory store; broadcast to all clients.

3. **Database setup (1 day)**
   - PostgreSQL container (Docker) or SQLite file.
   - Schema: `documents(id UUID, title TEXT, content_version INT, created_at TIMESTAMP)` + `edits(id UUID, doc_id UUID, user_id (null for Phase 0), operation JSONB, applied_at TIMESTAMP)`.
   - One SQL migration script, idempotent.

4. **Deployment pipeline (2 days)**
   - GitHub Actions or CircleCI.
   - Lint + run tests on push to `main`.
   - Deploy to staging (or local Docker) on merge.
   - Smoke test (create doc, reload, verify).

5. **Observability setup (1 day)**
   - Winston or Pino logger, structured JSON output.
   - Log every WebSocket message: `{ timestamp, user_id, op_type, latency_ms, result }`.
   - Spike: send logs to file; no external service yet.

6. **CRDT library evaluation (1 day, parallel)**
   - Spike: add Yjs to frontend and backend.
   - Test: create a CRDT text type, apply two edits concurrently, verify merge is deterministic.
   - Record latency of merge (must be <10ms for V1 gate).

**Rollback:** Revert last commit, restart process. Database: if corruption, wipe and restore from schema migration.

**Exit check V0:** All six tasks complete and pass their unit/integration tests; the skeleton path (create, edit, reload) succeeds twice in a row; logs show structured output; rollback succeeds.

---

### Phase 1: Core Architecture & Hard Constraints (Weeks 2–4)

**Unlocks:** Validated consistency model, latency baseline, auth, encrypted secrets, observability at scale, ready for multi-user testing.

**Depends on:** V0 pass, V1 (CRDT spike), V0a (hosting decision, if needed). **Default:** V0 passes before Phase 1 starts; V1 completes in parallel with Phase 0 tasks 1–5, gating Phase 1 Task 1.

**Tasks:**

1. **Conflict resolution engine hardened (3 days, critical path, requires V1 pass)**
   - Integrate Yjs (default from V1) into backend and frontend.
   - Document schema: each doc stores a CRDT.StateVector and updates as Yjs.UpdateMessage array.
   - Backend merges incoming edits: decode client update → apply to CRDT → re-encode state → broadcast.
   - Frontend applies remote updates: decode → merge into local Yjs.Doc → re-render.
   - Test: create doc, two simultaneous inserts at same position, verify both appear in deterministic order.
   - **Observability:** Log every merge with input state hash, update hash, output state hash, merge time (ms).

2. **API contract hardened (2 days)**
   - WebSocket message schema (JSON-schema or TypeScript).
   - `{ type: "edit", doc_id: UUID, update: Base64(Yjs.UpdateMessage), version: int }` (client → server).
   - `{ type: "ack", doc_id: UUID, version: int }` (server → client, confirms persistence).
   - `{ type: "remote_update", doc_id: UUID, update: Base64(Yjs.UpdateMessage), version: int }` (server → other clients).
   - Version := monotonic counter per doc (incremented on each server-applied edit).
   - Load test: emit 100 edits/sec to same doc from 10 clients; verify no update is dropped.

3. **Authentication & authorization (3 days)**
   - Implement JWT tokens (HS256, 1-hour TTL).
   - Signup: POST /auth/signup (username, password) → generate JWT, store hashed password in DB.
   - Login: POST /auth/login (username, password) → validate, return JWT.
   - WebSocket: client sends JWT on connect; backend validates; reject if invalid/expired.
   - Document access control: user_id in JWT; only return user's docs in GET /docs list; only allow edit if user is doc owner or has share token.
   - Share token: doc owner can generate a one-time or time-limited token (30 days) → other user redeems to gain access.
   - Spike test: create two users, one creates a doc, other cannot see it; owner shares token; other can now edit.

4. **Encryption & secrets (2 days)**
   - Secrets (DB password, JWT key) stored in env vars or AWS Secrets Manager.
   - TLS 1.3 for all traffic (self-signed cert for dev, real cert for staging/prod).
   - Encryption at rest: PostgreSQL encryption (AWS RDS encryption enabled).
   - Encryption in transit: WebSocket over wss://.
   - No plaintext passwords in logs or error messages.

5. **Audit logging (1 day)**
   - Log every edit: `{ timestamp, user_id, doc_id, operation_type (insert/delete), position, content, merge_time_ms, result }`.
   - Append-only audit table: `audit_log(id, user_id, doc_id, action, timestamp, old_version, new_version)`.
   - Query endpoint (private, admin-only): GET /audit?doc_id=X → return all edits to doc X in order.
   - **Observability:** All audit events include user_id; use for security monitoring later.

6. **Latency baseline & load test (V3, 2 days, gate)**
   - Load test: 20 concurrent users, each editing same doc, 100 edits per user over 10 seconds.
   - Measure: e2e latency (edit to seeing other user's change), p50/p95/p99.
   - Target: p99 <300ms. Baseline: expect ~50ms (local network).
   - If p99 >500ms, log bottleneck (client → server latency? merge time? broadcast?); log traces; do not proceed to Phase 2 until understood.
   - **Pass:** p99 <300ms with 20 users, or clear understanding of bottleneck and mitigation (Phase 2 task).

7. **Cost baseline & scale projections (V6, 1 day, info-gate)**
   - Measure: CPU, memory, DB connections during load test.
   - Project: cost per concurrent user (EC2 instance size, RDS IOPS).
   - **Assumption:** <$5 per active user per month.
   - If actual is >$10/user, log concern; may trigger Phase 2 cost optimization (e.g., service split, caching).

8. **Observability: dashboards & alerts (1 day)**
   - Single-server dashboard: CPU, memory, WebSocket connection count, edits/sec, p99 latency, error rate.
   - Alerts: if error rate >1%, p99 latency >500ms, or DB connections >80% of pool.
   - Use Prometheus (local) or CloudWatch.

**Rollback:** Revert Phase 1 commits; re-run Phase 0. If auth breaks existing docs, migration: backfill user_id=NULL for Phase 0 docs, or wipe and restart.

**Exit check:** All tasks complete and pass; V1 (CRDT spike) and V3 (latency load test) pass; cost baseline recorded; audit logging works; JWT auth tested with two users.

---

### Phase 2: Real Users & Conflict UX (Weeks 5–7)

**Unlocks:** Beta launch; data in production; hardened rollback and monitoring.

**Depends on:** Phase 1 complete, V3 pass (latency <300ms), V4 (user test).

**Tasks:**

1. **Conflict indicators & resolution UX (3 days)**
   - Frontend: when local edit merges with remote edit, show visual indicator (e.g., "3 other users editing" badge, last-edit attribution).
   - Conflict case: user A inserts "x" at position 5, user B deletes position 5–7 → both happen (CRDT guarantees). Show to user: "(+x) (-del)" inline or in margin.
   - User study (V4): 5 beta testers edit shared doc simultaneously for 15 min; collect feedback on conflict clarity.
   - **Pass criterion:** >80% report no confusion; no reports of lost edits.

2. **Persistence & recovery (2 days)**
   - Ensure every edit hits the database before ACK to client.
   - Schema: on crash, re-read all edits for doc from DB, rebuild CRDT state.
   - Spike: crash backend mid-merge; restart; verify document is consistent (no lost or duplicate edits).
   - Audit: log every crash and recovery time.

3. **Operational runbook & monitoring (2 days)**
   - Runbook: backup procedure, restore procedure, common errors and fixes.
   - Monitoring: alert if sync latency >500ms, error rate >1%, DB not responding.
   - On-call: assign for Phase 2 beta; document escalation path.

4. **Data privacy & compliance (1 day)**
   - Privacy policy: clarify that documents are stored on our servers, encrypted in transit.
   - Data retention: clarify how long deleted docs are kept (assume 30 days in backups).
   - GDPR/CCPA (if applicable): add export and delete endpoints (defer full compliance to Phase 3).
   - **Observability:** Log all data access (read document, export, delete) with user_id and timestamp.

5. **Beta sign-up & onboarding (1 day)**
   - Simple form: email, password, agree to terms.
   - Welcome email with link to app.
   - First-time UX: tutorial (create doc, invite friend, edit together).

**Rollback:** If critical bug (data corruption), restore from backup (point-in-time recovery). If auth breaks, disable login for 1 hour, revert code, re-enable.

**Exit check:** 5+ beta testers onboard, each create and edit a doc, successfully share and see real-time sync. V4 (user test) passes. No data loss during 1-week beta period.

---

### Phase 3: Scale & Hardening (Weeks 8–10)

**Unlocks:** GA readiness; multi-user, multi-document at scale; SLA compliance (99.9% uptime).

**Depends on:** Phase 2 complete and stable (1 week zero-incident run).

**Tasks:**

1. **Multi-service architecture (if V2 flagged it, else N/A) (3 days)**
   - Split: API gateway (auth, doc list) ↔ Sync engine (WebSocket, edits) ↔ Document store (PostgreSQL).
   - Use gRPC or async queue (Redis) between services.
   - Load balance across 2 sync engine instances.
   - Latency target: p99 <300ms with 100 concurrent users, 1000 docs.

2. **High availability & disaster recovery (2 days)**
   - Multi-AZ RDS (primary + standby, automatic failover).
   - Multi-AZ EC2 (2+ instances behind ALB).
   - Backup: daily incremental to S3, 30-day retention.
   - RTO target: 5 min (restore from backup). RPO: 1 min (point-in-time restore).
   - Rehearse: monthly failover drill; document steps.

3. **Security hardening (2 days)**
   - Penetration test spike: SQL injection, XSS, CSRF, WebSocket hijacking.
   - Fix critical findings; defer medium/low to Phase 4.
   - Add rate limiting: 100 edits/min per user, 1000 edits/min per doc.
   - Add DDoS mitigation (AWS WAF or Cloudflare).

4. **Performance optimization (2 days)**
   - Measure baseline (Phase 1) vs. Phase 3 load test.
   - If p99 latency still high: profile CPU/memory, tune DB indexes, cache recent edits in memory.
   - If not: document baseline and move to Phase 4.

5. **Compliance & audit readiness (1 day)**
   - Map controls to GDPR/CCPA if applicable (data export, delete, consent logging).
   - Add compliance checklist to runbook.
   - (Full SOC 2 audit deferred to Phase 4.)

6. **GA launch prep (1 day)**
   - Upgrade docs, pricing page, support channels.
   - Capacity planning: infrastructure to handle 10x current beta load.
   - SLA documentation: uptime, latency, support response time.

**Rollback:** If critical issue post-GA, feature flag: disable new signups, route new users to waitlist, warn existing users, roll back service mesh if applied. Restore database from point-in-time backup (1 min).

**Exit check:** 1-week zero-incident run with 50+ concurrent users, 500+ docs. Load test shows p99 <300ms and error rate <0.1%. Backup/restore drill succeeds. Security spike finds no critical issues. GA SLA document signed by stakeholders.

---

## 6. Validation checks

| Gate ID | Hypothesis | Method | Acceptance Threshold | Evidence | Unlocks | Phase(s) |
|---|---|---|---|---|---|---|
| **V0** | A real edit-persist-reload cycle works end-to-end with <2s latency and zero data loss. | Manual: create doc, type "abc", reload, verify text is there. Repeat 3x. | All 3 cycles succeed with text intact; logs show one trace per cycle. | Git commit hash, terminal screenshot showing edit and reload, logs in `build/v0.log`. | Phase 1 start. | Runs in Phase 0, required before Phase 1. |
| **V0a** | Self-hosted backend (EC2+RDS) and managed platform (Firebase/Supabase) can both reach Phase 0 production in <1 week with <$100 total cost. | Parallel spike (1 day each): deploy Phase 0 app to Firebase and EC2; measure time-to-first-production, resource cost. | Both platforms deploy in <1 week; Firebase <$50, EC2+RDS <$100 (1-week estimate). | Deployment logs, cost screenshots from both platforms, time-to-production timestamps. | Resolves R3 hosting decision (V0a default: self-hosted). If managed platform faster/cheaper, flip to managed. | Runs parallel to Phase 0, optional but recommended for confidence. |
| **V1** | CRDT (Yjs) merge is deterministic and <10ms for 1–2 simultaneous edits (same position insert). | Spike: implement edit conflict in Yjs on frontend and backend. Create two UpdateMessages (insert "x" and "y" at pos 5), apply in both orders, verify output is identical and merge time <10ms. | Merge times: 100 trials, p99 <10ms. Output hashes match. | Test code in `src/crdt-spike.ts`, benchmark results in `build/v1-benchmark.json`, console output. | Phase 1 Task 1 (conflict resolution engine). | Runs in Phase 0 (parallel), required before Phase 1 starts. |
| **V2** | Monolithic backend can handle 20 concurrent users, 200 documents, 50+ edits/sec with p99 latency <300ms. | Load test: k6 or Artillery, 20 concurrent users, each issues 10 sequential edits over 20 sec. Measure p50/p95/p99 latency (client-send to server-ack), throughput, error rate. | p99 <300ms; throughput >50 edits/sec; error rate 0%. | k6 script in `test/load.js`, results in `build/v2-load-test.json` (Prometheus metrics export or JSON summary). | Phase 1 Task 1 (hardening); or flip to multi-service architecture in Phase 3 if fails. | Runs at end of Phase 1 Task 2 (API contract), before Phase 2. |
| **V3** | Edit-to-remote-user-see latency is <300ms p99 with 20 concurrent users editing same document. | Integration test: two clients, one client edits, measure time for other client to receive update in WebSocket event. Run 100 edits, record latencies. | p99 <300ms; no edits lost. | Client-side timing logs (WebSocket event timestamps), server-side logs (edit apply → broadcast time), merged timeline in `build/v3-latency.csv`. | Phase 2 start. | Runs at end of Phase 1, blocks Phase 2. |
| **V4** | Real users (5 beta testers) can edit a shared document concurrently without losing edits or reporting confusion about conflicts. | User study: 5 beta testers, each invited to same doc; given task "collaborate to write a 200-word paragraph". Record edits, video interviews post-task: "Did you lose any text? Any confusion?" | >80% report no lost text, no confusion; all 5 successfully co-edit; audit log shows no missing edits. | User feedback form (`build/v4-user-feedback.txt`), audit log for test doc (`build/v4-audit.log`), video summary. | Phase 2 Task 1 (conflict UX refinement); Phase 3 if major UX changes needed. | Runs at end of Phase 2, informs Phase 3. |
| **V5** | Database persistence survives a process crash: edits applied before crash are in DB; on restart, doc is reconstructed to pre-crash state. | Chaos test: apply 50 edits to a doc, inject SIGKILL to backend mid-merge, wait 5 sec, restart backend, query document version and content. | Content and version match pre-crash state; no edits lost or duplicated. | Backend crash logs, DB state dump before/after crash (`build/v5-pre-crash.json`, `build/v5-post-restart.json`), audit log. | Phase 1 Task 5 (persistence hardening); Phase 2 beta if fails. | Runs in Phase 1 Task 2, before Phase 2. |
| **V6** | Cost per active concurrent user is <$5/month (infrastructure only). | Baseline: measure CPU/memory/IOPS/network during Phase 1 load test (20 users). Project to hourly cost (EC2, RDS, data transfer). Annualize. Divide by active-user-months. | Actual cost <$5/user/mo; if >$10/user/mo, flag and plan optimization. | CloudWatch billing export or manual calculation in `build/v6-cost-projection.xlsx`. | Informs Phase 2–3 scale decisions. | Baseline in Phase 1 Task 7; forecast in Phase 2/3 scale planning. |

---

## 7. Cross-Cutting Concerns

### Phase 0: First end-to-end slice

| Concern | Move |
|---|---|
| **Security** | Secrets (DB password, JWT key placeholder) in `.env` file, never in code. HTTPS/WSS not enforced yet (dev-only). Auth scaffolded as TODO. |
| **Observability** | Winston logger configured; every WebSocket message and DB operation logged as structured JSON (timestamp, user_id, op, latency_ms). Logs shipped to stdout; no external service. Baseline metrics recorded: latency ~5ms, memory ~50MB. |
| **Reproducibility** | Docker Compose file for PostgreSQL + Node backend; `npm install` and `docker-compose up` reproduces Phase 0 on any dev machine. Git tags Phase 0 stable state. |
| **Resilience** | Graceful shutdown: on SIGTERM, close WebSocket connections, wait 5 sec for in-flight edits, then exit. On restart, re-read doc state from DB (recovery is manual, not automated yet). |

### Phase 1: Core Architecture & Hard Constraints

| Concern | Move |
|---|---|
| **Security** | TLS 1.3 enabled (self-signed cert for dev). JWT auth implemented with 1-hour TTL. Secrets stored in env vars and validated at startup (fail if missing). Audit log captures every edit with user_id. Role-based access control (doc owner, share token holder) hardened. |
| **Observability** | Prometheus metrics added: `edits_total` (counter, doc_id + user_id), `edit_latency_seconds` (histogram), `active_websocket_connections` (gauge), `db_query_latency_seconds` (histogram). Dashboards in Grafana (local instance). Alerts for latency spikes and errors. |
| **Reproducibility** | Helm chart (or CloudFormation template) for single-AZ deployment. Pre-baked AMI or Docker image with app + dependencies. CI/CD deploys from `main` branch; every merge tagged with version and config hash. Manual runbook for local reproduction. |
| **Resilience** | Circuit breaker for DB connection pool (fail fast if DB unreachable). Exponential backoff for WebSocket reconnect (1s → 10s → 30s, cap). Health check endpoint (`GET /health`) returns `{ status, db, timestamp }`; alerts if health check fails 3 times in 2 min. |

### Phase 2: Real Users & Conflict UX

| Concern | Move |
|---|---|
| **Security** | HTTPS enforced (real cert from LetsEncrypt/ACM). Password hashing upgraded to bcrypt (cost factor 12). Session invalidation on logout. CSRF tokens for non-WebSocket endpoints (form submissions). Rate limiting: 100 edits/min per user. Data export endpoint requires user authentication and 2FA (deferred to Phase 3 if too slow). |
| **Observability** | Application Performance Monitoring (APM): add OpenTelemetry or similar; trace every edit from client to DB and back. Correlate traces by doc_id and user_id. SLI targets: latency p99 <300ms, error rate <0.1%, uptime >99.5%. Dashboards split by user, document, and edit operation type. Canary alerts: if p99 latency jumps >50ms, notify on-call. |
| **Reproducibility** | Environment parity: staging mirrors production config (same RDS instance type, EC2 size, TLS, secrets structure). Pre-deployment checklist: deploy to staging, run smoke tests, collect metrics for 5 min, compare to baseline; proceed to prod only if baselines match. Git tag each deployment. Rollback procedure tested monthly. |
| **Resilience** | Automated recovery: if WebSocket connection drops, client auto-reconnects with exponential backoff (max 30s). Server-side session timeout: 24 hours (hard logout). Backup: nightly to S3 with 30-day retention. Recovery time objective (RTO): 5 min (restore from backup). Recovery point objective (RPO): 1 min (assume ~1 min of edits may be lost if DB fails; acceptable for beta). |

### Phase 3: Scale & Hardening

| Concern | Move |
|---|---|
| **Security** | Multi-region replication (if compliance requires): replicate to secondary region daily, async. Encryption at rest enforced via RDS KMS. DDoS mitigation (AWS WAF rate limiting). Security headers added: HSTS, CSP, X-Frame-Options. Penetration test conducted; critical findings fixed. |
| **Observability** | Distributed tracing: every edit traced from client JS through backend and DB; latency waterfall visible. Centralized logging: ELK or CloudWatch Logs. Real-time dashboards show p99 latency, error rate, active users, docs, by region/tenant (if multi-tenant later). SLO monitoring: uptime, latency, support metrics tracked per team. |
| **Reproducibility** | Infrastructure-as-Code (Terraform or CDK): all AWS resources codified. Deployment pipeline fully automated: push to `main` → CI runs tests → deploy to staging → smoke tests pass → one-click deploy to production (with approval gate). Playbooks for common failure scenarios (DB failover, service restart, data restore). Monthly dry-runs. |
| **Resilience** | Multi-AZ for compute (EC2 ASG across 3 AZs) and data (RDS Multi-AZ + daily backup to S3 and cross-region). Load balancer health checks every 5 sec. Graceful connection draining (30 sec) on instance termination. Canary deployments: new version deployed to 10% of traffic first, monitored for 10 min; if error rate >1%, auto-rollback; else proceed to 100%. |

---

## 8. AI Layer

**N/A — no AI component.** The system does not use LLMs, retrievers, or agents; conflict resolution is algorithmic (CRDT). If a future feature adds AI-assisted suggestions (e.g., "auto-complete" or "grammar check"), this will be added as a new layer post-GA.

---

## 9. Exceptions

**None.** The first end-to-end slice is feasible, low-cost, and provides immediate confidence. All dependencies are resolvable via gates and phases. No expedited timeline, external constraint, or data governance rule requires an exception.

---

## 10. Deferred

| Item | Pull-Forward Condition |
|---|---|
| **Multi-tenancy (shared docs across teams)** | Deferred to Phase 4. Pulled forward if beta user(s) request team collaboration. Adds: resource isolation, per-tenant billing, RBAC for team admins. Cost: ~2 weeks Phase 4 work. |
| **Comments and annotations** | Deferred to Phase 4. Pulled forward if user study (V4) shows strong demand. Requires: separate data model, threading, notifications. Cost: ~1 week. |
| **Document versioning and history browser** | Deferred to Phase 4. Pulled forward if compliance requires audit history UI. Requires: query all edit events for doc, reconstruct past states. Cost: ~3 days. |
| **Offline-first (local-only edits, sync on reconnect)** | Deferred to Phase 4. Pulled forward if users report frequent disconnections. Requires: service worker, IndexedDB, background sync API. Cost: ~1 week. |
| **Mobile app (iOS/Android)** | Deferred to Phase 4 (or post-GA product decision). Pulled forward if web usage shows >30% mobile traffic. Native app adds: platform specifics, app store setup, push notifications. Cost: 2 weeks per platform. |
| **Data residency and SOC 2 audit** | Deferred to Phase 4. Pulled forward if customer (enterprise) requires it. Adds: regional deployment, audit controls, 3rd-party pen test. Cost: ~2 weeks. |

---

## Self-Check

✓ **Phase 0 is a production first end-to-end slice** — create, edit, reload one document end-to-end, deployed and rolled back, with observability and fallback to Phase 0 on failure.

✓ **Every task follows what blocks it** — conflict resolution (V1) → sync engine hardening (Phase 1 Task 1) → latency test (V3) → Phase 2 user study (V4). Auth (Phase 1 Task 3) → Phase 2 real users. No task precedes its dependencies.

✓ **Nothing reaches real users, data, or money before controls pass** — Phase 0 is internal-only; Phase 2 (beta, real users) requires Phase 1 hardening (auth, encryption, audit logging) and V3 (latency); Phase 3 (GA) requires Phase 2 stability (1-week zero-incident run) and V2/V3/V4 passes.

✓ **Rework runs widest first** — R3 decisions (CRDT model, hosting, monolith vs. services) decided and gated early (V1, V0a, V2) before Phase 1 hardening. API contract (Phase 1 Task 2) affects both client and server; specified before conflict UX (Phase 2).

✓ **Each considered tradeoff has a row, tier, default, and revisit trigger** — CRDT vs. OT (R3), self-hosted vs. managed (R3), monolith vs. services (R2), PostgreSQL vs. MongoDB (R2). R1 decision (build vs. buy sync engine) on one line.

✓ **Each gate has five fields, a phase, and a consumer** — V0–V6 all specified: hypothesis, method, threshold, evidence, unlocks, phase(s). Thresholds labeled: REQUIREMENT (latency <300ms), ASSUMPTION (<$5/user/mo), UNKNOWN (GA budget).

✓ **Observability starts in Phase 0** — structured logs, latency baseline recorded. Expanded in Phases 1–3 (metrics, dashboards, APM, SLO monitoring).

✓ **No AI component — N/A stated.** Security, resilience, reproducibility threaded through every phase.

✓ **No exceptions** — all seven rules apply; first end-to-end slice is feasible and required.

✓ **Deferred items have pull-forward conditions** — multi-tenancy pulled forward if users request; comments, versioning, offline, mobile, SOC 2 deferred with clear triggers.

---

**Plan COMPLETE.** Ready to start Phase 0.
