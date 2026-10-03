# Outcome benchmark: does a better plan produce better code?

Plan-scoring rubrics judge text. This benchmark checks something a rubric cannot: whether the
code an implementer writes *from a plan* is more correct, and cheaper to change, when the plan came
from Dependency-First Architect than when it came from the bare model or from no plan at all.

## The task: `webhook-ledger`

A small Python service that keeps prepaid-credit balances in sync with a payment provider's
webhooks ([`tasks/webhook-ledger/brief.md`](tasks/webhook-ledger/brief.md)). The brief states
**facts** about the environment and leaves the **implications** unstated:

| The brief says | What correct code must do | Hidden-test category |
|---|---|---|
| PayCo may deliver an event more than once, even after a 2xx; finance reconciles to the minor unit | apply each event exactly once, also after a restart | `idempotency` |
| A refund can arrive before its payment; PayCo retries anything not answered with a 2xx | never lose or double-apply an early refund | `ordering` |
| Database writes occasionally fail partway through a request | acknowledge (2xx) only what is applied, exactly once; otherwise leave no partial effect | `failure-injection` |
| The signature is an HMAC over the exact body bytes; reject `t` more than 300 s off | verify the raw bytes; enforce the window on the signature time, both ways; reject a reused id with a new body | `security` |
| Amounts are integer minor units; each account holds one currency | reject fractional and string amounts; keep big amounts exact; never mix currencies | `money` |
| — | the basic credit / refund / read behavior | `functional` |
| Round 2 ([`CHANGE_REQUEST.md`](tasks/webhook-ledger/CHANGE_REQUEST.md)): partial refunds, never above the payment, 422 otherwise | the new behavior, without breaking round 1 | `change-request` (+ every round-1 test as regression) |

A plan that surfaces those implications should lead to code that passes more hidden tests. That
is the hypothesis being tested, not an assumption the benchmark makes.

## How an attempt runs

For each arm — `no-plan`, `baseline-plan` (a plan from the bare model), `dfa-plan` (a plan from the
skill), and optionally `generic-plan` (the active-control skill) — and each repetition:

1. **Plan.** The planner model writes a plan for the brief under the arm's condition (no plan for
   `no-plan`).
2. **Implement (round 1).** A fixed implementer model, the same for every arm, gets a sandbox with
   the starter code, `BRIEF.md`, and (except `no-plan`) the plan as `PLAN.md`. Its instructions are
   identical across arms except for one clause pointing at `PLAN.md`. The claude-cli implementer
   has file tools only (Read, Write, Edit), so it can read and write files but cannot run code or
   ask questions; an agent behind the `command` provider runs with whatever tools it has.
3. **Test.** `runner.py` runs the hidden tests against the code, in a child process with a
   scrubbed environment and a timeout.
4. **Change (round 2).** The implementer gets `CHANGE_REQUEST.md` and updates its own code.
5. **Test again**, all categories, and measure the rework: files changed and lines added and
   removed between the round-1 and round-2 code.

Recorded per attempt: pass/total per category and round, the rework diff, and the implementer's
tokens, cost, latency, and turns. The implementer never sees the hidden tests, the reference
solutions, or which arm it is in. Arms are interleaved by repetition, so a cost cap cannot starve
one arm. An attempt re-run with `--retry-failed` keeps its earlier record, code and raw output
under `superseded/`, counted in the cost and listed in the summary's flags.

## Validating the benchmark itself

`tests/test_outcome_benchmark.py` (runs in CI, no model calls) shows the hidden tests measure
what they claim:

- the round-1 reference passes every round-1 test, and the round-2 reference every test;
- the round-1 reference fails `change-request`, so round 2 requires new work;
- the starter stub passes nothing;
- each of 9 single-defect [mutants](tasks/webhook-ledger/mutants/README.md) fails its target
  category and passes every functional test;
- a legitimate alternative design (retrying a failed write inside the request) passes every
  test, so the tests check invariants, not one implementation;
- the runner tests only the implementation package (files elsewhere in the sandbox cannot
  shadow hidden tests), uses its own copy of the environment (`ledgerkit`), times out hung
  code, reports import errors as test errors, refuses code that imports common networking or
  process modules or calls common file-deletion functions (a heuristic screen for honest
  mistakes that misses many other forms; not a sandbox), and strips credentials from the
  environment;
- the brief never names the remedies (idempotency, atomic transactions, ...).

## What the first run showed about the task itself

The first committed run
([`2026-10-03-outcomes-webhook-ledger`](../results/2026-10-03-outcomes-webhook-ledger/OUTCOMES.md);
Opus 5.5 plans, Sonnet 5.5 implementer, 5 attempts per arm) exposed two limits of this task
version:

- **Four tests check a status-code convention, not correctness.**
  `test_missing_field_is_rejected`, `test_fractional_amount_is_rejected`,
  `test_string_amount_is_rejected` and `test_malformed_json_is_rejected` require a non-2xx
  response to a malformed event. Answering 2xx and quarantining the event without applying it
  leaves the ledger just as correct, and the brief does not rule it out (PayCo retries anything
  not answered with a 2xx, so some plans chose to stop the retries). In that run, 6 of 15
  attempts given a plan, and 0 of 5 without one, took that design. Every test they failed was one
  of these four, so the round-1 differences between the plan arms come from this convention.
- **Ceiling.** Leaving those four tests out, all 15 attempts given a plan passed every round-1
  test, and so did 4 of the 5 without one (the fifth crashed on a bug of its own and passed
  11 of 35). The task does not separate plans at this implementer's capability; a harder task
  (more implications left unstated), a weaker implementer, or several tasks are needed before a
  difference between plan arms could show up.

Task files are pinned per run (`outcomes/task-files.json`), so the fix (accept either a rejection
or an acknowledged, unapplied, recorded event) belongs in a new task id, not in this one.

## What it does not measure

- **System-level outcomes.** Deployment, observability, operations, and organizational work —
  much of what the skill plans — are out of scope for a single-module coding task.
- **Generality.** One task, one domain (payments). A small effect here, or none, says little about
  other systems; a large one would still need replication on other tasks.
- **Iterative development.** The implementer cannot run code, so this is closer to "write it from
  the spec" than to an agent that runs its own tests. It isolates the plan's information content
  at the cost of realism.
- **Contamination.** The hidden tests and references are public in this repository. A future model
  trained on them could pass without a plan. Rotate or extend the task before trusting results
  from models released after this commit.

## Safety: this runs model-written code

`python eval/run.py outcomes implement` executes code written by a model. It refuses to run
without `--allow-code-execution`. Each test run happens in a child Python process started with
`-s -B` (no user site-packages, no bytecode files), from a fresh directory holding only the
implementation package, the hidden tests and the provided `ledgerkit`, with a scrubbed environment (no API keys, tokens, or cloud credentials)
and a timeout. That is not a sandbox: the code can still read and write your files and use the
network. Run it inside a container or a throwaway VM. See [`SECURITY.md`](../../SECURITY.md).
(`runner.py`'s docstring still says `-I`; the file is pinned by the committed run, so the wording
is corrected here instead.)

## Running it

See [`eval/README.md`](../README.md#outcome-benchmark) for the commands, the config
([`outcomes.json`](outcomes.json)), cost, and how results are reported.
