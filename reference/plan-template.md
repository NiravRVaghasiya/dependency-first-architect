# BUILD PLAN — output format

Emit the plan in exactly these sections, in this order. Keep it concrete: name real
technologies and real checks, not placeholders. Length is not quality: prefer one-line table
cells, cite IDs (V2, Phase 3, E1) instead of restating content, and scale the plan to the system.
A section with nothing load-bearing is one line ("None.", "N/A — <reason>"); a section that a
declared exception makes moot is one line citing it.

---

## 1. Classification and constraints
- **What:** one line on the system.
- **Type:** software / infra / AI-agentic (or a mix); greenfield or brownfield; small build or not.
- **Dominant constraint:** correctness | availability | latency | cost | scale | compliance.
- **Worst failure:** the single failure that would hurt most.

**Budgets** (only the rows that bind; add rows the system needs, such as a cutover freeze window):

| Budget | Target | Label | Checked by |
|---|---|---|---|
| Latency | … | REQUIREMENT / BASELINE / ASSUMPTION / UNKNOWN | V… or Phase … |
| Throughput | … | … | … |
| Availability / SLO | … | … | … |
| RPO / RTO | … | … | … |
| Resource use | … | … | … |
| AI inference cost | … (AI systems only) | … | … |
| Storage cost | … | … | … |
| Operational complexity | … (e.g. services to run, on-call load) | … | … |

An UNKNOWN target has no number: write the missing input, who supplies it, and the phase that
needs it. An ASSUMPTION gives its basis.

**Missing inputs:** the unknowns that would change the plan most, and any assumption the phase
order rests on.

## 2. Dependency map
Only the dependencies that bind and that the phase order does not already show:

| What waits | Depends on | Kind | Blocked from being |
|---|---|---|---|
| … | … | decision / validation / risk-security / organizational / economic (structural or runtime only when surprising) | built / deployed / specified / hardened / exposed / committed |

Then one line naming the kinds with no instance ("No organizational or economic dependencies.").
In brownfield work, mark each dependency CONFIRMED or ASSUMED.

## 3. Tradeoff gates (resolved up front)

| Decision | Reversibility | Default (chosen now) | Assumption | Validated by | Flip condition |
|---|---|---|---|---|---|
| Consistency vs availability | R2/R3 + one-clause reason | … | … | V…, a cheap check, or the cited basis | … (for a gated row: "V… fails, or …") |
| Monolith vs services | … | … | … | … | … |
| Sync vs async | … | … | … | … | … |
| Build vs buy | … | … | … | … | … |
| Data-privacy boundary (personal or regulated data) | … | … | … | … | … |
| <each system-specific R2/R3 decision, e.g. the tenancy model> | … | … | … | … | … |
| (Brownfield) migration strategy, coexistence and cutover, rollback, system of record | … | … | … | … | … |
| (AI) prompt+RAG vs fine-tune | … | … | … | … | … |
| (AI) hosted API vs self-host | … | … | … | … | … |

**R1 defaults:** <decision> → <default>; … (one line; no rows for R1 decisions).
**N/A:** <considered decisions that do not apply> — <reason> (one line).
Omit the (Brownfield) and (AI) rows when they do not apply. Inherited brownfield decisions the
plan will not change are listed once as constraints, not as rows.

## 4. Walking skeleton (Phase 0)
- The one real request and the real response.
- Every tier it crosses.
- How it is deployed, logged, monitored, and rolled back on day zero.
- Who can reach it (internal or allow-listed traffic behind a flag until its controls exist).

If an exception changes Phase 0, say so in one line and cite it (E…).
Exit check (V0): a real request succeeds with one trace across every tier; a deploy and a
rollback succeed through the pipeline; an injected failure fires an alert; the first values of
the measurable budgets are recorded as baselines, not targets.

## 5. Phases (Phase 1 onward; dependency order; within a phase, rework widest first, exposure smallest first)
For each phase:
- **Phase N — <name>**
  - Unlocks: what becomes possible after it.
  - Depends on: earlier phases and V-IDs, by reference.
  - Tasks: in order.
  - Rollback: how this phase is undone, or its point of no return and the gate that guards it.
  - Exit check: its gates pass, plus any observable condition that no gate covers.

## 6. Validation gates

| ID | Hypothesis | Method | Acceptance threshold | Evidence | Unlocks (and if it fails) | Phase |
|---|---|---|---|---|---|---|
| V0 | A change can be deployed, observed, and rolled back through every tier in production | … | the V0 exit check in §4 | … | Phase 1 | 0 |
| V1 | … | … | … (labeled) | the artifact it will produce and where it is kept | … ; if it fails: … | … |

One row per gate the plan cites, and no others. Every threshold carries its label. Evidence names
an artifact the check will produce, never a result. Phase may read "starts in N, required before M".

## 7. Cross-cutting concerns (per phase, from the first commit)

| Phase | Security | Observability | Reproducibility | Resilience |
|---|---|---|---|---|
| 0 | … | … | … | … |

Observability appears in Phase 0. No cell is empty; where a declared exception bypasses one, cite
it (E…).

## 8. AI layer (AI/agentic systems only)

| Sublayer | In this system | Built in | Exit check or V-ID |
|---|---|---|---|
| 1. Prompt-injection / guardrail defense | … | Phase … | … |
| 2. Cost + latency budget | … | … | … |
| 3. Human-in-the-loop gating | … | … | … |
| 4. Retrieval | … | … | … |
| 5. Model access | … | … | … |
| 6. Memory | … | … | … |
| 7. Orchestration | … | … | … |
| 8. Routing | … | … | … |
| 9. Feedback | … | … | … |

A capability sublayer (4–9) this system does not need: "Not needed — <reason>". The defenses
(1–3) are always needed when the system takes untrusted input, spends money, or acts on the
world. For a system with no AI component, the whole section is one line: "N/A — no AI component."

## 9. Methodology exceptions

| ID | Rule bypassed | Why it does not apply | Replacement validation | Evidence required | Resumes when |
|---|---|---|---|---|---|
| E1 | … (principle or step) | … | … (and which risk stays open) | … | … (an observable condition, or "permanent for this system" and why) |

Or the single line "None." An exception with an empty field is a silent skip.

## 10. Deliberately deferred
Bullet list: what you are NOT building yet, and the condition that pulls each item forward.
