# Layer map — teaching analogy (reference only)

> **This file is teaching material, not the execution path.** It exists to build intuition for
> *why* dependency-first ordering works. The authoritative build order lives in `SKILL.md`.
> **Wherever this metaphor disagrees with engineering judgment, engineering wins.** Do not plan
> a system by walking this analogy literally.

## Growing a software system like growing a human body

A body is not assembled from the skin inward — the skin is the most visible part and nearly the
last to matter. It grows from dependency outward. Software is the same.

| Body stage | What it is | Software analogue |
|---|---|---|
| **Blueprint (DNA)** | the irreversible instructions every later cell reads | Tradeoff gates — decisions made once, read by everything after |
| **Cells** | the smallest unit that must work before anything larger | the primitives: data model, core service, deploy unit |
| **Energy (metabolism)** | the power/plumbing that keeps cells alive | infra, CI/CD, the deploy + run substrate |
| **Skeleton** | the thin frame that gives the body a shape to hang organs on | the **walking skeleton** — thinnest end-to-end prod path |
| **Organs** | specialized functions, built onto the skeleton | features / services, added in dependency order |
| **Skin** | the visible surface, built last | UI / polish / the demo-friendly layer |
| **Immune + nervous system** | present throughout, in every tissue, from the start | **cross-cutting concerns** — security, observability, resilience, threaded through every phase |

## Why the analogy helps
- **Visibility ≠ priority.** Skin is what you see; the skeleton is what holds you up. Teams
  default to building skin first and are surprised when it collapses.
- **The immune and nervous systems are not a final organ.** They are woven through every
  tissue from the embryo onward — exactly like security and observability. This is the
  intuition behind "observability is day-zero, not last."
- **DNA is fixed early and expensive to change.** That is why irreversible tradeoffs are
  resolved up front, not discovered mid-build.

## Where the analogy breaks (so you don't over-apply it)
- Software can be refactored; you cannot refactor a spine mid-growth. Principle 2 ("keep
  nothing rigid until it must be") has no clean biological parallel — reversibility is a
  software superpower, not a body one.
- Bodies grow on a fixed genetic program; software should defer rigidity and flip decisions
  when a gate's condition triggers. When the metaphor implies "lock it in early," ignore it.
- There is no biological equivalent of "build vs buy" or "hosted API vs self-host." Those are
  pure engineering gates.

Use the picture to remember the *order*. Use `SKILL.md` to actually plan.
