I've read both reference files. The workspace README says the directory is empty apart from itself. There's no `ledgerkit` package next to it either: I tried both `ledgerkit/__init__.py` and `ledgerkit.py` and found neither. The brief says to see README.md for the MemoryStore API, but that information isn't there. The safest way to handle partial writes depends on what the store can do, so the plan below works without relying on any store feature beyond an atomic single-record write, and the first task is a one-day spike to confirm it.

---

# Ledger: Architecture and Build Plan

## 1. Summary

- **What:** Ledger is a stdlib-only Python library (`LedgerService`) that turns signed PayCo webhooks into per-account balances and a list of money movements. It replaces the nightly CSV-to-spreadsheet process. Finance must be able to reconcile it exactly, to the cent, against PayCo's monthly statement.
- **Shape:** one in-process library with five small modules: signature check, event parsing, pure account logic, a repository wrapped around the ledgerkit store, and the service that coordinates them. An HTTP layer that we don't own calls `handle()`. There's no queue and no second service.
- **Key decisions:**
  1. **Every money movement has a fixed key** (`payment:<payment_id>`, `refund:<payment_id>`), and each account's movements, parked refunds and holds live in **one account record that is written in a single put**. The balance is calculated from the movements when read and is never stored as a separate counter. A partial failure or a retry can therefore never double-count or leave the balance and movements disagreeing.
  2. **The event record is written first and marked done last.** Every step between is idempotent (safe to run twice), so any `StoreError` returns 503, PayCo retries, and the retry finishes the work. Ledger never returns 2xx until the effect has been durably written.
  3. **A refund that arrives before its payment is parked** inside the account record and applied when the payment arrives. It is checked against the payment's amount and currency at that point.
  4. **Response codes:** 2xx only means "durably handled, or durably set aside for a human". Problems that a code fix could solve (a malformed payload) return non-2xx, so PayCo's 72 hours of retries act as our buffer. Problems that need a human decision (currency mismatch, a second refund for the same payment) return 200 and are recorded as a **hold**, with an alert.
  5. **Signature check comes before everything else.** A known event `id` with a different body (compared by SHA-256 of the raw bytes) returns 409, records an incident, and changes nothing.
- **First milestone:** a signed sandbox `payment.succeeded` goes from PayCo through `handle()` to the store and shows up in `balance()`/`entries()`. Duplicates and injected `StoreError`s at every write position are proven harmless. Two spikes run alongside: the ledgerkit API and PayCo sandbox behaviour.
- **Top risks:**
  - If the store has no conditional write and more than one process writes, account updates can be silently lost. The mitigation is one Ledger process with per-account locks until the store supports compare-and-set.
  - PayCo might not re-sign retries with a fresh `t`, in which case every retry after 300 s would be rejected.
  - Manual fixes in the spreadsheet that PayCo has no record of would show up as differences at cutover.

## 2. Context and Goals

- **Problem:** balances are copied nightly from PayCo's CSV into a spreadsheet and fixed by hand. They are up to a day old, error-prone, and cost support time.
- **Goals:**
  - Every delivered PayCo payment and refund changes exactly one account exactly once.
  - Balances are current within seconds of delivery.
  - Monthly reconciliation against PayCo's statement matches exactly.
  - Hostile or corrupt input never changes a balance.
- **Non-goals:**
  - Recording customer spend inside the product (not in the brief; see the open questions).
  - Handling disputes, payouts or any other event type. These are stored, not applied.
  - Partial refunds.
  - Currency conversion.
  - Admin UI.
  - The HTTP server, deployment and monitoring stack. This plan says what they need to provide but doesn't build them.
- **Success measures:**
  - 0 unexplained differences at the first two month-end reconciliations after cutover.
  - The nightly script and spreadsheet are retired.
  - Support hours spent on balance fixes drop to about 0.
  - p95 delay from PayCo delivery to updated balance is under 5 s.

## 3. Drivers, Requirements, and Constraints

**Ranked quality attributes**

| # | Attribute | Measurable target | Why it matters |
|---|---|---|---|
| 1 | Financial correctness | Each payment/refund affects its account exactly once whatever the order, duplication or partial failure. 0 unexplained differences at month-end. | Finance reconciles to the minor unit. |
| 2 | Input integrity / security | 100% of unsigned or stale (>300 s) deliveries rejected before any store access. 100% of same-id/different-body deliveries rejected with the account untouched, alert within 5 min *(assumption: 5 min)*. | Ledger holds money balances, and the endpoint is on the internet. |
| 3 | Partial-failure resilience | Any `StoreError` gives a non-2xx response, and state always converges on retry. Never a 2xx before the effect is durable. | Managed DB writes fail partway through; PayCo only retries non-2xx. |
| 4 | Evolvability | Adding a new event type (e.g. disputes), including a backfill from events already received, takes ≤ 2 engineer-days *(assumption)*. | PayCo adds types; the brief says "we will add them later". |
| 5 | Freshness / availability | `handle()` p95 < 250 ms. 99.5% monthly availability is enough *(assumption)*, because PayCo retries for 72 h and so buffers any outage shorter than that. | Customers expect to use credit soon after topping up. Volume is tiny (≈0.05 req/s). |

The ranking means that if a step can't be confirmed as durable, Ledger answers 503 and leaves PayCo to retry. It never answers 200 hoping the write landed.

**Key functional requirements:** `LedgerService(store, secret, clock)`, `handle()`, `balance()`, `entries()` exactly as specified. Header names are case-insensitive. Unknown event types are accepted and stored. Each account's currency is fixed by its first applied payment.

**Constraints:**
- Python standard library only.
- The store is a ledgerkit `MemoryStore`, standing in for a managed network DB.
- The HTTP layer, deployment and monitoring are outside this codebase.
- Team size, skills and deadline weren't given. I've assumed 1–2 Python engineers.

**Hard parts**

1. **Exactly-once effects when writes fail partway, including "ambiguous" failures.** A write that raised `StoreError` may still have committed. Wrong handling here means real money is double-counted or lost.
2. **Store primitives are unknown.** Whether atomic puts, compare-and-set (CAS) or scans exist decides the concurrency model, and the API couldn't be inspected.
3. **Out-of-order refunds.** These need parking state, a check against the payment, and must still be correct at cutover for payments that were made before Ledger existed.
4. **PayCo behaviour we can't see:** whether retries are re-signed, whether redeliveries are byte-identical, and what fields a refund payload carries. These depend on an outside party and can only be confirmed in the sandbox.
5. **Cutover reconciliation.** The spreadsheet contains hand edits that PayCo may know nothing about.

## 4. Current State

- **Repository:** empty. `README.md` reads "This directory is intentionally empty… There is no existing code, documentation, configuration, or data here to inspect." The brief says README.md describes ledgerkit's `MemoryStore`, but it doesn't, and no `ledgerkit` package is present. **Nothing about the store API has been confirmed.** Task T1 resolves this.
- **Outside the repo:**
  - PayCo merchant account. A sandbox is assumed to exist.
  - Nightly CSV export script and the support-maintained spreadsheet.
  - A managed network DB.
  - An HTTP layer of unknown framework that will host the webhook URL.
- **Conventions:** there are none to follow, so this plan proposes them:
  - Python ≥ 3.10 *(assumption)*.
  - Package `ledger/`.
  - `unittest` tests in `tests/`, run with `python -m unittest discover -s tests`.
  - No runtime dependencies.

## 5. Assumptions and Open Questions

**Assumptions**

| # | Assumption | Impact if wrong | Validated how / when |
|---|---|---|---|
| A1 | A single-record put in ledgerkit is atomic (all or nothing, though it may be ambiguous whether it committed). | The single-record design fails. We'd have to switch to append-only per-movement records plus a scan. | T1 spike, M1 day 1. |
| A2 | The store **may not** offer CAS or transactions. | If it does, we use CAS (better) and drop the single-process limit. | T1. |
| A3 | PayCo re-signs every retry with a fresh `t`. | Every retry after 300 s gets a 401 and events are lost after 72 h. We would have to escalate with PayCo. | T2 sandbox: force a 500 and inspect the retry headers. |
| A4 | Redeliveries are byte-identical, as the brief says. | Harmless false 409s, which still page someone. If so, switch to hashing canonical JSON. | T2: compare captured redeliveries. |
| A5 | A refund payload has `account_id`, `payment_id`, `amount`, and maybe `currency`. | If `account_id` is missing, parking needs a global index keyed by `payment_id`. | T2. |
| A6 | PayCo payloads contain no cardholder data. | Ledger would fall into PCI scope, and raw-body storage would have to change. | T2 plus PayCo docs. |
| A7 | The HTTP layer passes raw body bytes through unmodified (no JSON re-serialisation) and limits bodies to 64 KB. | The HMAC check fails on every request, or memory can be abused. | M1 T10 in staging. |
| A8 | The PayCo export includes `payment_id`s and the full history. | The cutover import can't match old refunds, and they stay parked. | T2 / M4 prep. |
| A9 | Python ≥ 3.10. One process is enough for years (≈0.05 req/s now; 100× growth is about 5 req/s). | Low. | T3. |

**Open questions**

| Question | Owner | Default if unanswered | Needed by |
|---|---|---|---|
| Q1. What is ledgerkit's exact API: atomic put? CAS/version? key scan or prefix list? multi-key transaction? Does it return copies or live references? | Whoever owns ledgerkit / the DB | get/put only, atomic single-record put, no scan. Run one process with in-process locks. | M1 day 1 |
| Q2. PayCo: re-signing on retry, byte-identical redelivery, refund fields, request timeout, multiple `v1` during secret rotation. | PayCo integration contact | A3–A5 | M1 end |
| Q3. Finance: hold-and-alert for anomalies (currency mismatch, a second payment or refund for the same `payment_id`), or apply anyway? | Finance lead | Hold and alert, balance untouched | M2 start |
| Q4. Does the spreadsheet contain deliberate manual credits or adjustments that PayCo doesn't know about? | Support lead + Finance | Treated as differences for Finance to decide; adjustment entry type deferred | M4 start |
| Q5. Will product spend be recorded in Ledger later? | Product owner | No. Entries cover PayCo movements only. | M3 (affects the record layout) |

## 6. Architecture Overview

```
          Internet (untrusted)              |            Our network (trusted)
                                            |
 PayCo ──HTTPS POST──►  HTTP layer (not ours; TLS, ≤64 KB body, raw bytes, all headers)
                                            |        │ handle(raw_body, headers)
                                            |        ▼
                                            |  ┌──────────── ledger (one Python package, one process) ───────────┐
                                            |  │ service.LedgerService                                            │
                                            |  │   1 signature.verify ──► 2 events.parse ──► 3 repository: event  │
                                            |  │                                               record (pending)   │
                                            |  │   4 accounts.apply_* (pure) ──► 5 repository: account record put │
                                            |  │   6 repository: event record done ──► (status, body)             │
                                            |  │ balance()/entries() ──► repository.get_account                   │
                                            |  └────────────────────────────┬─────────────────────────────────────┘
                                            |                               ▼
                                            |                ledgerkit store (MemoryStore → managed DB)
                                            |   tools/ (M3–M4): reconcile.py, report.py, import_history.py
  Secret manager ──► webhook secret (bytes) ─┘ injected at construction
```

The HTTP layer hands over untouched bytes and headers. `LedgerService` checks the signature before reading anything, parses the body, and claims the event `id` by writing an event record. It then runs a **pure** function that takes the current account record and the event and returns the new record and an outcome. The account record is written once, then the event is marked done. All store access goes through `repository`, which is the only module that knows about ledgerkit.

**Component table**

| Component | Responsibility | Owns data | Interfaces exposed | Tech | Depends on |
|---|---|---|---|---|---|
| HTTP layer (external) | TLS, routing, body limit, pass raw bytes through | — | POST /webhooks/payco | Not ours | `LedgerService` |
| `ledger/signature.py` | Parse `PayCo-Signature`, check HMAC and the 300 s window | — | `verify(headers, raw_body, secret, now) -> None \| reason` | `hmac`, `hashlib` | — |
| `ledger/events.py` | JSON parsing, envelope and payload checks, typed events | — | `parse(raw_body) -> Envelope`; `PaymentSucceeded`, `RefundSucceeded`, `Unhandled` | `json`, `dataclasses` | — |
| `ledger/accounts.py` | Domain rules: apply payment or refund, parking, holds, currency lock | — (pure) | `apply_payment(rec, evt, now)`, `apply_refund(rec, evt, now)` → `(new_rec, outcome)` | stdlib | — |
| `ledger/repository.py` | Key layout, serialisation, copying, concurrency (CAS or locks) | EventRecord, AccountRecord, IncidentRecord | `get_event`, `insert_event`, `mark_event`, `get_account`, `put_account`, `put_incident` | stdlib | ledgerkit |
| `ledger/service.py` | Coordination, status mapping, logging | — | `LedgerService.handle / balance / entries` | `logging` | all above |
| `tools/*` (M3–M4) | Reconciliation, hold and parked reports, history import | — (writes only through `repository`) | CLI | stdlib | `repository` |

## 7. Component Details

**`signature`**
- Header lookup lowercases the dict keys. A missing header, or two keys that differ only in case, gives a rejection.
- Split the value on `,`, then on the first `=`. Collect `t` (must match `^\d+$`; don't use bare `int()`, which accepts `+1`, ` 1` and `1_0`) and every `v1`. Ignore any other scheme.
- Reject if `abs(clock.now() - int(t)) > 300`. Exactly 300 is allowed.
- Compute `expected = hmac.new(secret, t_raw.encode() + b"." + raw_body, sha256).hexdigest()` and accept if `hmac.compare_digest` matches any `v1`. Use the `t` string **as received**, not a reformatted copy.
- Pure, with no access to the store.

**`events`**
- Runs only after the signature has passed.
- Envelope: `id` is a non-empty str, `type` is a str, `created` is an int, `data` is a dict.
- For handled types:
  - `account_id` and `payment_id` are non-empty strings.
  - `amount` is an `int`, not a `bool` and not a float, and greater than 0.
  - `currency` matches `^[A-Z]{3}$`. It's required on payments and optional on refunds until A5 is confirmed.
- Returns `Unhandled(type)` for anything else.

**`accounts`** (the core; no I/O)
- `apply_payment`:
  - If `payment:<pid>` already exists with the same `event_id` (and body hash), do nothing.
  - If it exists with `source=import` and the same amount and currency, do nothing; the outcome is `already_imported`.
  - Otherwise, if it exists, hold with `duplicate_payment`.
  - If the record has no currency, set it. If the currency differs, hold with `currency_mismatch`.
  - Otherwise append `+amount`. If `parked_refunds[pid]` exists, check it (same amount; currency missing or equal). Append `-amount` if it passes, or move it to `held` if not.
- `apply_refund`:
  - If `refund:<pid>` exists or `parked_refunds[pid]` exists: with the same `event_id`, do nothing; with a different one, hold with `duplicate_refund`.
  - If `payment:<pid>` exists: check amount and currency, then append `-amount` or hold.
  - If the payment is in `held`: hold with `refund_of_held_payment`.
  - Otherwise park it.
- Holds are stored in `held[event_id]`, which is idempotent.
- A negative balance is allowed and never clamped.

**`repository`**
- Each read returns a deep copy (a JSON round-trip), so callers can't alter an in-memory `MemoryStore` by accident.
- Concurrency mode, chosen in T1:
  - **(a) CAS available:** `put_account(rec, expected_version)`. On conflict, re-read and re-apply up to 3 times, then 503.
  - **(b) No CAS:** a `threading.Lock` per `account_id` and per `event_id`, held across read-modify-write, plus a hard rule of **one Ledger process**.
- Failure behaviour: `StoreError` propagates to `service`.

**`service`**
- Maps outcomes to statuses:

| Outcome | Status | `status` field |
|---|---|---|
| Signature or timestamp failure | 401 | `unauthorized` (no detail) |
| Bad JSON or envelope | 400 | |
| Payload fails checks | 422 | (event stays `pending`) |
| Same id, different body | 409 | `conflict` |
| Applied / parked / ignored / held / duplicate | 200 | the outcome |
| `StoreError` | 503 | |
| Any other exception | 500 | (logged with traceback) |

- `balance()` and `entries()` let `StoreError` propagate rather than returning a misleading 0.

**Scaling:** not a driver. Mode (b) handles more than 100× today's volume in one process. Move to mode (a) when more than one instance is needed.

## 8. Data Design

| Record | Key | Fields | Writer |
|---|---|---|---|
| EventRecord | `event:<event_id>` | `event_id`, `body_sha256`, `raw_body` (UTF-8 text), `type`, `created`, `received_at`, `state` (`pending`\|`done`), `outcome`, `last_error` | `service` via `repository` |
| AccountRecord | `account:<account_id>` | `account_id`, `currency` (null until first payment), `version`, `entries[]`, `parked_refunds{payment_id→…}`, `held{event_id→{type, reason, payment_id, amount, currency}}` | `service` via `repository` |
| Entry (inside AccountRecord) | `key` = `payment:<pid>` \| `refund:<pid>` | `event_id`, `type`, `amount` (signed), `currency`, `payment_id`, `created`, `applied_at`, `body_sha256`, `source` (`webhook`\|`import`) | `accounts` |
| IncidentRecord | `incident:<event_id>:<sha256>` | `event_id`, `original_sha256`, `received_sha256`, `raw_body`, `received_at` | `service` |

- **System of record:**
  - The AccountRecord is the system of record for balances. `balance = sum(e.amount for e in entries)`, so it can't drift.
  - EventRecords hold the full raw history. That allows an account to be rebuilt by replaying events in `received_at` order, and lets future handlers backfill event types that were previously ignored.
- **Consistency:**
  - All money effects for one event touch exactly one AccountRecord in one put.
  - No cross-record transaction is needed. The event record's `pending → done` only controls whether work gets replayed, and the replay is idempotent.
- **"Oldest first"** in `entries()` means the order movements were applied to the account (list order). `created` is included so consumers can sort differently if they need to.
- **Access patterns:** point reads by key only. A scan (for reports and backfill) depends on what T1 finds. Without one, reports in M3 use a key list kept by `repository`.
- **Size:** about 300 bytes per entry. An account with 1,000 movements is about 300 KB. Revisit if any account goes over 2,000 entries.
- **Retention:** keep everything for 10 years *(assumption: financial record retention; Finance to confirm in M3)*. Backups come from the managed DB. A test restore plus a rebuild-from-events runs before cutover (M4).
- **Classification:** account and payment IDs and amounts are internal-confidential financial data, with no card data assumed (A6). The webhook secret is the most sensitive item and is held only in the secret manager and in memory.
- **Schema evolution:** add fields only, and readers default missing fields. Each record carries `schema: 1`. A breaking change is handled by a lazy upgrade on read, then a backfill tool.

## 9. Key Flows

**F1: Payment, happy path.** Signature passes → JSON parses → `event:evt_1` is absent, so it's inserted as pending → under the `acct_42` lock (or CAS), read the account (absent, so a new record) → `apply_payment` sets EUR and appends `payment:pay_9xk +2500` → put the account → mark the event done (`applied`) → 200. `balance("acct_42") == 2500`.

**F2: Refund before payment.**
1. The refund `evt_2` arrives. `payment:pay_9xk` isn't in `acct_42`, so it goes into `parked_refunds[pay_9xk]` → 200 `parked`. Balance is 0 and there are no entries.
2. The payment `evt_1` arrives. It appends +2500, finds the parked refund, checks amount and currency, appends `refund:pay_9xk -2500` (`event_id=evt_2`), and removes the parked refund, all in **one put**.
3. Result: balance 0, two entries, and no window where the payment shows without its refund.

**F3: Failure path, `StoreError` partway through (the case the design exists for).** Writes per delivery are W1 (event pending), W2 (account), W3 (event done).

| Fails at | State left behind | PayCo retry does | Final |
|---|---|---|---|
| W1 (did or didn't commit) | Nothing, or a pending event | Insert, or find pending with the same hash → continue | Correct |
| W2 (did or didn't commit) | Account unchanged, or already updated | `apply_*` sees the movement key with the same `event_id` → no change; otherwise applies | Correct, applied once |
| W3 | Account updated, event pending | W2 does nothing, W3 succeeds → 200 | Correct |

Each case returns 503 to PayCo. The only visible effect is that the balance may already include the movement before PayCo hears 200, which is the correct end state anyway.

**F4: Same id, different body.** Signature passes (the sender has the secret, or PayCo has a bug) → `event:evt_1` exists and its `body_sha256` differs → write IncidentRecord (best effort) → log at CRITICAL → 409. The account and the original EventRecord aren't touched. PayCo keeps retrying, which keeps the alert firing until someone acts. An *unsigned* conflicting body gets 401 at step 1 and never reaches the incident logic, so attackers can't flood the incident queue.

## 10. Key Decisions

| # | Decision | Options considered | Rationale (drivers) | Reversibility | Revisit if |
|---|---|---|---|---|---|
| D1 | One AccountRecord holds entries, parked refunds and holds, written in one put. Balance is calculated on read. | (a) This. (b) Balance counter plus a separate entries table. (c) Append-only entry records plus a scan. | #1 and #3: (b) needs multi-key transactions to avoid drift; (c) needs scan and has no atomic parking. (a) needs only an atomic single put (A1). | Hard (data model) | T1 shows puts aren't atomic, or accounts exceed 2,000 entries |
| D2 | Movement keys are `payment:<pid>` / `refund:<pid>`, not event id. | Key by event id only | #1: also blocks a second event for the same payment and makes cutover overlap with imported history safe | Hard | PayCo introduces partial or multiple refunds per payment |
| D3 | Write the event record first, idempotent steps in between, mark done last. Replay on retry. | Mark done first; single transaction | #3: marking done first loses the effect on failure. Transactions may not exist. | Easy | Store offers transactions → wrap W1–W3 in one (keep the idempotency anyway) |
| D4 | Park early refunds; apply and check when the payment arrives. | Apply immediately (balance temporarily negative, no check) | #1 and #2: checks amount and currency and keeps the "currency fixed by first payment" rule clean. Cutover gap handled by the history import (D8). | Medium | Finance prefers a negative balance that is visible immediately |
| D5 | Response policy: non-2xx if a code fix could help, 200 + hold if a human must decide. Unknown types get 200 and are stored. | Reject anomalies with 4xx | #1 and #4: 4xx anomalies would retry for 72 h and then be dropped by PayCo. Holds are durable and visible. | Easy | Finance wants anomalies to show as failures in PayCo's dashboard |
| D6 | Conflicts are detected by SHA-256 of the raw bytes, after the signature check. | Hash of canonical JSON | #2: literal reading of the brief and conservative. A false positive costs a page, not money. | Easy | T2 finds redeliveries aren't byte-identical |
| D7 | Concurrency: CAS if the store has it, otherwise one process with per-account and per-event locks. | Multi-process with no CAS (unsafe); external lock service | #1: lost updates are silent money loss. Volume doesn't need more than one process, and PayCo's 72 h retries cover single-instance downtime. | Easy | Need for a second instance, or T1 finds CAS |
| D8 | Cutover state is rebuilt from PayCo history; the spreadsheet is used only for comparison. | Opening balances from the spreadsheet | #1: matches the statement by construction, and lets old refunds find their payments | Medium | Q4 shows legitimate manual credits → add an `adjustment` entry type |

## 11. Cross-Cutting Concerns

**Security** (M1 unless noted). Threats and mitigations:

1. **Forged deliveries.** HMAC-SHA256 with `compare_digest`, checked before parsing or touching the store.
2. **Replay.** The 300 s window, plus deduplication by event id inside the window.
3. **Same-id tampering or secret compromise.** 409, incident record, page. The runbook treats it as possible secret exposure and rotates the secret.
4. **Secret leakage.** The secret is injected from the secret manager. It and the signature header are never logged. Tests assert this.
5. **Resource abuse.** 64 KB body limit and edge rate limit in the HTTP layer (A7). Unsigned requests cost one HMAC.

Secret rotation (M3): during PayCo's dual-signing window (Q2) we accept any matching `v1`. Supporting two secrets on our side is deferred, because the interface takes a single `secret`.

**Reliability** (M1–M2). Idempotency is defined in D1–D3. There is no retry inside a request; PayCo's retries do that job. CAS conflicts get 3 attempts. The health check (provided by the HTTP layer) should do one store read. Recovery is by managed DB restore, then rebuilding accounts from EventRecords (`tools/rebuild.py`, M3).

**Observability** (M1 logging, M3 alerts):
- One JSON log line per delivery with `event_id`, `type`, `account_id`, `outcome`, `status`, `latency_ms`, and the first 12 characters of `body_sha256`.
- Counters by outcome.
- Alerts, through the external monitoring stack:

| Condition | Action |
|---|---|
| Any 409 | Page |
| 401 rate > 5 in 10 min | Ticket (clock skew, secret misconfiguration, or probing) |
| 503 rate > 2% over 15 min | Page in business hours |
| Any hold | Finance ticket |
| Parked refund older than 72 h | Finance ticket (the payment is probably pre-cutover or went to a different account) |

**Performance:** about 3 store operations per delivery and well under 250 ms p95. The M2 test harness includes 10k deliveries in one run as a smoke load test. No dedicated load test is needed at this volume.

**Cost:** one small process and the existing managed DB; storage is a few MB a day. Effectively nothing extra.

**Operations** (M3): the building team owns it. Business-hours response is enough for everything except 409s, because PayCo buffers 72 h. Runbooks cover 409 incidents, 401 spikes, `StoreError` surges, clearing holds, stale parked refunds, and rebuilding an account.

**AI-specific:** not applicable; there are no model components.

## 12. Build Sequence

Team assumption: 1–2 Python engineers, a Finance contact for about 2 hours a week, and PayCo sandbox access. Sizes are rough ranges, not commitments.

| Milestone | Goal / scope | Exit criteria | Depends on | Size |
|---|---|---|---|---|
| **M1: Thin slice and spikes** | Signed sandbox payment → balance. Duplicates, unknown types, 409 conflict, `StoreError` → 503. Spikes S1 (store) and S2 (PayCo). *Excludes refunds and holds.* | Sandbox payment shows in `balance()`. Fault-injection test passes for each write position, in both failed and ambiguous-commit modes. Known signature test vector from a real sandbox delivery passes. | — | 5–8 eng-days |
| **M2: Correctness core** | Refunds and parking, holds, currency lock, concurrency mode, a randomised model-based test harness | Harness: 500 seeds × random event sets with shuffled order, duplicates, injected faults and conflicting bodies. Final state equals the reference model every time. Thread test (mode b) shows no lost updates. | M1, Q3 | 5–8 eng-days |
| **M3: Operability and reconciliation** | `tools/reconcile.py` (Ledger vs PayCo statement CSV), `tools/report.py` (holds, stale parked), `tools/rebuild.py`, alert specs, runbooks, record retention confirmed | Reconcile runs against a sandbox month with 0 differences. Each alert fires in staging. | M2 | 4–6 eng-days |
| **M4: Cutover** | `tools/import_history.py`, shadow run alongside the spreadsheet, switch consumers over, retire the nightly script | One full month-end reconciliation matches exactly. Spreadsheet differences explained by Finance (Q4). Restore and rebuild drill done. | M3, Q4 | 3–5 eng-days + ≥1 month-end of calendar time |

**Critical path:** PayCo sandbox access (an outside party; request it on day 1) → S2 → M1 end-to-end → M2 harness → M4 month-end shadow, which is fixed calendar time. S1 → repository → service is the internal critical path.

## 13. First Milestone Task Breakdown

Day 1, in parallel: T1, T2 (send the access request first), T3.

| # | Task | Location | Done when |
|---|---|---|---|
| T1 | **Spike S1 (≤1 day):** find the ledgerkit API and classify: atomic single put? CAS/version? scan? transactions? copy or reference on read? What does `StoreError` look like after a write that committed? | `docs/store-capabilities.md` | Doc answers each question with a code snippet; concurrency mode (a) or (b) recorded. **Changes the plan if:** puts aren't atomic (switch D1 to option c) or transactions exist (simplify D3). |
| T2 | **Spike S2 (≤2 days, start day 1):** register a sandbox webhook to a tunnel, capture deliveries. Force a 500 and inspect the retry's `t` and body bytes; capture a refund payload; note PayCo's timeout and rotation behaviour. | `tests/fixtures/payco/*.json` (body + headers), `docs/payco-behaviour.md` | A3–A6 each marked confirmed or refuted. **Changes the plan if:** retries keep the old `t` (escalate to PayCo; consider a wider window only with their guidance) or refunds lack `account_id` (add a global park index). |
| T3 | Scaffold the package and CI | `ledger/__init__.py`, `pyproject.toml` (no deps, `requires-python>=3.10`), `tests/`, CI job running `python -m unittest discover -s tests` | CI green on an empty test; a test asserting no non-stdlib imports passes. |
| T4 | Implement `verify()` | `ledger/signature.py`, `tests/test_signature.py` | Tests cover: valid; wrong secret; tampered body; header-name case variants; missing, duplicate or malformed header; non-digit `t`; drift of ±300 accepted and ±301 rejected; multiple `v1`; the S2 fixture vector. |
| T5 | Implement `parse()` and the typed events | `ledger/events.py`, `tests/test_events.py` | Rejects bad JSON, `amount` as `True`, `2500.0`, `"2500"`, `0`, negative, bad currency. Unknown type gives `Unhandled`. |
| T6 | Implement the repository with the §8 key layout and the T1 concurrency mode | `ledger/repository.py`, `tests/test_repository.py` | Round-trip tests; changing a returned record doesn't change the store; in mode (b) the lock helpers are tested with threads. |
| T7 | Implement `apply_payment` (currency lock; parked-refund branch stubbed until M2) | `ledger/accounts.py`, `tests/test_accounts.py` | Pure tests: new account, repeat payment does nothing, `duplicate_payment` hold, `currency_mismatch` hold. |
| T8 | Implement `LedgerService` for payments, unknown types, duplicates, 409, status mapping, JSON log line | `ledger/service.py`, `tests/test_service.py` | Every row of the §7 status table has a test; `balance()` is 0 and `entries()` is `[]` for unknown accounts; secret never appears in logs (checked with `assertLogs`). |
| T9 | Build the fault-injection store and partial-failure tests | `tests/faults.py` (`FlakyStore`: raise `StoreError` before *or after* the Nth write), `tests/test_partial_failure.py` | For N ∈ {1, 2, 3} × both modes: retry until 200 gives balance == amount, exactly 1 entry, and no 2xx ever returned with the effect missing. |
| T10 | Run the slice end to end | Staging HTTP layer, or `devtools/devserver.py` (stdlib `http.server`, dev only, never deployed) | Sandbox payment shows in `balance()`; a redelivery from PayCo's dashboard returns `duplicate`; A7 (raw bytes) confirmed. |

Order: T1 → T6. T2 → (T4 finished, T10). T3 → T4, T5, T7 in parallel → T8 → T9 → T10.

## 14. Testing and Validation Strategy

- **Unit tests:** signature, parsing, the pure `accounts` rules (most of the rule edge cases live here).
- **Partial-failure tests** (T9): every write position, in both "failed" and "committed then raised" modes.
- **Model-based randomised harness** (M2, the release gate):
  - A seeded `random` generator produces accounts, payments, refunds and unknown events.
  - Each run shuffles delivery order, injects duplicates, conflicting bodies and `StoreError`s, and retries until 2xx, the same way PayCo does.
  - It asserts that balances and entries equal a simple reference model, that conflicts never change state, and that holds match the anomalies that were planted.
- **Contract fixtures:** real sandbox deliveries from T2, so we aren't only testing our HMAC against itself.
- **Concurrency test:** 8 threads delivering interleaved events for the same account (mode b), or forced CAS conflicts (mode a).
- **Reconciliation test** (M3): a synthetic statement CSV run against the ledger output.
- **CI and release blocking:** all of the above in CI. Any harness divergence or partial-failure failure blocks release.
- **How driver 1 is verified in production:** month-end reconciliation during the M4 shadow run.

## 15. Rollout, Migration, and Rollback

1. Build and deploy Ledger against live webhooks. Nothing reads it yet, and the spreadsheet process carries on unchanged. No existing system consumes webhooks, so the two run side by side naturally.
2. Run `import_history.py` to bring in PayCo's history as `source=import` entries. Overlap with live webhooks is safe because of the D2 keys.
3. Compare daily: Ledger, the spreadsheet and the PayCo export. Finance categorises each difference (Q4).
4. After one month-end reconciliation matches exactly, switch balance consumers to Ledger.
5. Retire the nightly script one more month-end later.

**Rollback:** until step 5, point consumers back at the spreadsheet; Ledger keeps receiving. A bad deploy of Ledger is rolled back to the previous version, and PayCo retries anything that got a non-2xx in between. **No return after** step 5, once nobody maintains the spreadsheet any more.

## 16. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation | Early warning | Owner |
|---|---|---|---|---|---|
| Lost update from concurrent writers with no CAS | Med | High (silent money loss) | D7: one process with locks, or CAS. The deployment config enforces replicas = 1. | Reconcile difference; >1 instance in deploy config | Tech lead |
| PayCo retries not re-signed | Low–Med | High | Confirm in T2; escalate with PayCo | Retries returning 401 in sandbox | Tech lead |
| HTTP layer changes the body bytes | Med | High (all 401s) | A7 checked in T10; contract test in the HTTP layer | 401 on 100% of requests | HTTP layer owner |
| Spreadsheet has untracked manual credits | Med | Med | Q4, shadow comparison, possible `adjustment` type | Unexplained differences at step 3 | Finance |
| Refund points at a different account than its payment | Low | Low–Med | Stale-parked alert at 72 h | Parked refunds older than 72 h | Finance |
| ledgerkit puts aren't atomic | Low | High (redesign) | T1 on day 1, before any repository code | T1 result | Tech lead |
| False 409s from re-serialised redeliveries | Low | Low (pages, no money) | D6 revisit trigger | 409s in sandbox | Tech lead |

## 17. Deferred Work and Future Evolution

**Deferred, with the trigger for each:**
- Dispute and payout handlers: when the business needs them. Backfill from stored `ignored` EventRecords.
- Two-secret rotation: first planned rotation.
- Admin tool to resolve holds: more than about 5 holds a month.
- Cached balance field: if accounts exceed 2,000 entries.
- Multi-instance: needs CAS (mode a).
- `adjustment` entry type: Q4.
- Spend entries: Q5. This would add more writers to the account record, so CAS becomes required.

**Extension points:**
- Handlers are registered in `service` per event type.
- `accounts` holds pure per-type functions.
- `repository` is the only place that knows about ledgerkit.

**Debt taken on deliberately:** the single-process limit in mode (b). Paid off by moving to CAS once the store supports it.

## 18. Next Steps

1. **Today:** request PayCo sandbox access and a sandbox webhook secret, which is the long-lead item. Ask the ledgerkit/DB owner for the API docs (Q1).
2. Start T1: write `docs/store-capabilities.md` from the ledgerkit source or docs.
3. Start T3: create `ledger/`, `tests/`, `pyproject.toml` and the CI job running `python -m unittest discover -s tests`.
4. Book 30 minutes with Finance on Q3 (hold vs apply) and Q4 (manual credits).
5. Start T4 and T5 against the brief's example payload. Add the sandbox fixture vectors when T2 delivers them.

---

These five open questions would change the plan most:

1. **What does ledgerkit's `MemoryStore` support?** Is a single put atomic, is there compare-and-set, scans, transactions? The README the brief points to has nothing about it. The answer decides between one process with locks and a CAS-based design, and whether the data model holds at all.
2. **Does PayCo re-sign each retry with a fresh `t`, and are redeliveries byte-identical?** If retries aren't re-signed, the 300 s rule rejects every retry after five minutes.
3. **For anomalies** (currency mismatch, a second refund for the same payment), **does Finance want them held for review or applied?** I've defaulted to holding them.
4. **Does the spreadsheet contain deliberate manual credits that PayCo doesn't know about?** If so, Ledger needs an adjustment entry type before cutover.
5. **Will customer spend be recorded in Ledger later?** That would add a second writer to each account and make compare-and-set mandatory.
