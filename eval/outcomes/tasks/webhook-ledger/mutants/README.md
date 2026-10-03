# Mutants: does each hidden-test category catch the defect it is meant to catch?

Each directory is the round-1 reference (`reference/round1/`) with exactly one deliberate defect.
`tests/test_outcome_benchmark.py` runs the hidden tests against every mutant and requires that it
fails at least one test in its target category and passes every functional test, so the
category measures that defect and not general brokenness. `targets.json` is the machine-readable
version of this table.

| Mutant | Target category | Defect |
|---|---|---|
| `non_idempotent` | idempotency | Never checks whether an event was already applied, so a redelivery moves money again. |
| `memory_dedup` | idempotency | Remembers processed events in the process's memory, not the database, so a redelivery after a restart moves money again. |
| `float_money` | money | Parses `amount` with `float()`: accepts `10.5` and `"2500"`, and rounds large integers. |
| `reserialized_signature` | security | Verifies the HMAC over `json.dumps(json.loads(body))` instead of the exact bytes PayCo signed. |
| `no_replay_window` | security | Ignores the signature timestamp, so a captured delivery can be replayed at any time. |
| `rejects_old_events` | security | Applies the replay window to the event's `created` time instead of the signature time, so a later retry of an older event is rejected for good. |
| `non_atomic` | failure-injection | Marks the event as processed in one transaction and moves the money in a second, so a failed write loses the event. |
| `swallow_store_errors` | failure-injection | Answers 200 when a database write fails, so PayCo never retries the lost delivery. |
| `drops_early_refund` | ordering | Acknowledges a refund that arrives before its payment and drops it. |

These are the classes of defect a plan should prevent by surfacing what the brief implies:
redeliveries need durable idempotency, mid-request write failures need all-or-nothing processing
(acknowledge only what is applied, exactly once), "match to the minor unit" needs integer money,
and the signature covers the exact bytes at the time PayCo sends them.
