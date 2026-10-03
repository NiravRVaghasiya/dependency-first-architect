# Architecture and Build Plan Template

Use the sections below in this order. Headings may be renamed slightly to suit the domain, but keep the order and intent. For small systems, a section may be a single sentence. If a section does not apply, keep its heading and give a one-line reason. Prefer tables where the notes suggest them.

---

## 1. Summary

Five to ten sentences or a tight bullet list covering:

- What is being built and for whom.
- The overall shape of the architecture in one or two sentences.
- The three to five most important decisions.
- What the first milestone delivers.
- The top risks.

Someone who reads only this section should know what is about to happen and why.

## 2. Context and Goals

- **Problem:** what is painful or missing today, and for whom.
- **Goals:** the outcomes the system must achieve, in measurable terms where possible.
- **Non-goals:** things deliberately left out of scope, so nobody assumes they are coming.
- **Success measures:** how you will know it worked after launch, such as adoption, latency, cost, accuracy, or time saved.

## 3. Drivers, Requirements, and Constraints

- **Ranked quality attributes:** a table with the columns *attribute*, *measurable target*, and *why it matters*, in priority order. Mark targets you inferred as assumptions.
- **Key functional requirements:** the capabilities that shape the structure. This is not a full backlog.
- **Constraints:** existing stack, hosting, team size and skills, deadline, budget, compliance, data residency, and mandated vendors.
- **Hard parts:** the few areas that are new, uncertain, dependent on outside parties, or costly to get wrong, each with one sentence on why.

## 4. Current State

What exists today that this work touches: relevant code, services, data stores, infrastructure, and processes. Cite specific files, modules, or systems where you inspected them. Note conventions the plan will follow. For entirely new builds, state what organisational or platform context applies.

## 5. Assumptions and Open Questions

- **Assumptions:** a table with the columns *assumption*, *impact if wrong*, and *how and when it will be validated*.
- **Open questions:** each with the owner or person who can answer it, the default you will use if no answer arrives, and the date or milestone by which it must be resolved.

## 6. Architecture Overview

- A text-based diagram (Mermaid or ASCII) showing components, data stores, external systems, and trust boundaries.
- A short narrative of how the parts fit together.
- A **component table** with the columns *component*, *responsibility*, *owns which data*, *exposes which interfaces*, *technology*, and *key dependencies*.

## 7. Component Details

For each significant component:

- Its responsibility and its boundaries, meaning what it explicitly does not do.
- Its interfaces: whether they are synchronous or asynchronous, the contract format, how they are versioned, and the expected load.
- Its internal structure, where that matters.
- How it fails and how it recovers.
- How it scales, if scale is a driver.

Routine components can be covered in a line or two.

## 8. Data Design

- The main entities and how they relate to each other.
- The system of record for each entity, and which component writes to it.
- Consistency requirements and where transactions are needed.
- Access patterns and the indexing or partitioning they imply.
- Retention, deletion, backup, and restore targets.
- Data classification, covering sensitive fields and how they are protected.
- How schemas will evolve and how migrations will be done.

## 9. Key Flows

Walk through the two to four most important scenarios step by step, from trigger to outcome. Use sequence diagrams or numbered steps. Include at least one failure path, such as a timeout, retry, duplicate, partial failure, or dependency outage, and show how the system behaves.

## 10. Key Decisions

One entry per significant decision, optionally as a table:

- **Decision:** what was decided.
- **Options considered:** at least two realistic ones.
- **Rationale:** why this option, measured against the ranked drivers.
- **Reversibility:** whether it is hard or easy to undo.
- **Revisit if:** the evidence or condition that would reopen the decision.

## 11. Cross-Cutting Concerns

A short subsection for each. Name the concrete mechanism and the milestone in which it is built.

- **Security:** identity, authorisation model, secrets, encryption, input validation, the main threats and their mitigations.
- **Reliability:** availability and recovery targets, redundancy, timeouts, retries, idempotency, backpressure.
- **Observability:** logs, metrics, traces, dashboards, alerts tied to user-facing symptoms.
- **Performance and capacity:** expected load, the bottlenecks you expect, and how they will be load-tested.
- **Cost:** estimated run cost, the main cost drivers, and the controls on spend.
- **Operations:** who owns the system, runbooks, on-call expectations, and how support works.
- **AI-specific concerns, where relevant:** evaluation approach, guardrails, cost and latency budgets, human approval points, and the fallback when the model fails.

## 12. Build Sequence

A milestone table or list. For each milestone:

- **Goal:** the risk it clears or the value it delivers.
- **Scope:** what is included and what is explicitly excluded.
- **Deliverables:** the concrete artefacts, features, or environments it produces.
- **Exit criteria:** checkable conditions that mark it complete.
- **Dependencies:** what must be finished first, and what can run in parallel.
- **Rough size:** a range, with the team assumption behind it.

The first milestone must contain a thin end-to-end slice and the spikes for the highest-risk unknowns. Mark the critical path.

## 13. First Milestone Task Breakdown

A list of concrete tasks. Each one has a verb and a deliverable, its location (module, directory, service, or repository), and a "done when" condition. Note the order and which tasks can run in parallel. Spikes include the question being answered, a time box, and the result that would change the plan.

## 14. Testing and Validation Strategy

- Which kinds of tests cover which risks: unit, contract, integration, end-to-end, load, security, and evaluation sets for AI components.
- The test environments and test data needed.
- What runs in CI and what blocks a release.
- How the measurable targets in Section 3 will actually be verified.

## 15. Rollout, Migration, and Rollback

- How the system reaches users: feature flags, staged or percentage rollout, beta groups.
- For changes to existing systems: how old and new run side by side, the steps for migrating data, and the switchover criteria.
- The rollback procedure for each risky step, and the point after which rolling back is no longer possible.

## 16. Risks and Mitigations

A table with the columns *risk*, *likelihood*, *impact*, *mitigation*, *early warning sign*, and *owner*. Include technical, delivery, dependency, and organisational risks.

## 17. Deferred Work and Future Evolution

- Items deliberately postponed, and what would trigger building each one.
- Ways the architecture is expected to evolve, and the extension points that support it.
- Known shortcuts or technical debt taken on purpose, with a plan to pay each one back.

## 18. Next Steps

Three to seven actions someone can take immediately. These include answering the open questions that block progress and starting the first tasks. Name the specific files, commands, or people where possible.
