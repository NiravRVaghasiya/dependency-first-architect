# Ledger: Architecture and Build Plan

## 1. Summary

- **What:** Ledger is a small Python library (standard library only) that turns PayCo webhook deliveries into per-account balances. Support and Finance can then stop correcting the spreadsheet by hand. It plugs into an HTTP layer that we don't own here.
- **Shape:** One in-process package. A delivery goes through these steps in order: **SignatureVerifier → EventParser → Inbox → Posting handlers**. Everything is written through a **LedgerRepository** wrapped around the ledgerkit store. `balance()` and `entries()` are calculated from stored entries on each call.
- **Key decisions:**
  1. **Every write is idempotent, and every delivery runs the whole pipeline again, including duplicates.** "Seen before" never short-circuits processing. That is how a `StoreError` partway through a request gets repaired: PayCo retries and the retry finishes the work.
  2. **The balance is never stored as a counter.** It is always the sum of entries keyed by `(account_id, event_id)`. Because a counter is never incremented, nothing can be counted twice.
  3. **Out-of-order refunds are stored durably and acknowledged with 2xx.** A refund that arrives before its payment is applied when the payment arrives. We don't depend on PayCo's 72-hour retry window to sort out ordering.
  4. **Anything we can't safely apply is quarantined, not dropped.** That covers currency mismatches, refund/payment mismatches, and malformed but signed payloads. Unhandled event types get a 2xx and their raw body is kept for replay later. The one exception is the case the brief names: **same `id` with a different body returns 409** and is treated as an incident.
  5. **2xx means the event is durable.** We return 2xx only after every effect of the event is written. Store failures return 503 so PayCo retries.
- **First milestone (about 1–1.5 weeks):** signed `payment.succeeded` deliveries flow through to balance and entries on MemoryStore. This includes duplicates, the id-conflict 409, unhandled types, and a fault-injection harness that proves the result is exact under `StoreError` at every write position. It also includes two spikes: the ledgerkit store's actual capabilities, and PayCo's real signing and retry behaviour.
- **Top risks:**
  - The ledgerkit store API is unknown. The README the brief points to isn't in the workspace.
  - PayCo might reuse the original `t` on retries. A transient failure would then turn into permanent loss once 300 seconds pass.
  - Disputes and other unhandled money-moving events will make reconciliation differ from PayCo's statement until we support them.
  - We haven't decided how existing balances get into Ledger.

## 2. Context and Goals

- **Problem:** Balances come from a nightly CSV-to-spreadsheet copy, and support staff fix them by hand. They are up to a day stale, they drift, and they take staff time.
- **Goals:**
  - Each account's balance reflects every successful payment and refund PayCo has delivered.
  - Balances match PayCo's monthly statement exactly, to the minor unit.
  - No event is counted twice, and none is silently lost.
- **Non-goals (for now):**
  - Spending or usage debits from the product.
  - Manual adjustments.
  - Handling disputes or payouts. We store them but don't act on them.
  - The HTTP server, deployment, and monitoring stack. This plan specifies the contract they must meet.
  - Multi-currency accounts.
  - Partial refunds. The brief says refunds are always full.
- **Success measures:**
  - Zero unexplained differences in the monthly reconciliation, starting with the first statement after cutover.
  - Zero manual balance edits after cutover.
  - The nightly script and spreadsheet are retired within two statement cycles.
  - The balance updates within 1 minute of PayCo's delivery at p95 (assumption).

## 3. Drivers, Requirements, and Constraints

**Ranked quality attributes**

| # | Attribute | Measurable target | Why it matters |
|---|---|---|---|
| 1 | **Correctness / data integrity** | Final balances equal the oracle sum for any delivery order, any duplicates, and a `StoreError` at any write. Verified across 0 failures in ≥10,000 randomised runs in CI. Every applied entry traces to exactly one event id. | Finance needs an exact match. Money errors are the whole reason this project exists. |
| 2 | **Security of ingestion** | 100% of unsigned, tampered, or stale (`\|now−t\| > 300`) deliveries rejected before any parsing or write. A same-id/different-body delivery never changes an account and always raises an alert. | Forged credit is real money. The brief requires the incident behaviour. |
| 3 | **Durability of acknowledgement** | A 2xx is returned only after every effect is durably written. RPO for acknowledged events is ≤ the managed database's point-in-time-restore (PITR) granularity (assumption: 5 min). | PayCo stops retrying after a 2xx, so an acknowledged event that isn't durable is lost. |
| 4 | **Evolvability** | A new event type (e.g. `charge.dispute.*`) can be added and back-applied from stored raw bodies in ≤3 days. | The brief says new types are coming. |
| 5 | **Availability / latency** | `handle()` p99 < 1 s. Endpoint availability ≥99% monthly is enough (assumption). | PayCo retries for 72 h, so short outages only delay credit. This attribute ranks last on purpose. |

**Key functional requirements:**
- The `LedgerService(store, secret, clock)` interface, with `handle`, `balance`, and `entries` exactly as specified.
- Header lookup is case-insensitive.
- Payments credit the account. Full refunds debit it.
- An account's currency is fixed by its first payment.
- Unknown types don't fail.

**Constraints:**
- Python, standard library only. ledgerkit is our own package and is supplied.
- The store is a managed network database whose writes can fail partway through a request.
- Volume is a few thousand events a day, and growing.

**Hard parts:**
1. **Partial writes plus duplicates.** A partly applied event followed by a redelivery is the classic double-credit or lost-credit bug.
2. **Refund before payment.** It needs durable parking, and the payment and refund handlers must find each other even when they run concurrently.
3. **Store semantics we haven't seen.** The correct design depends on whether the store has put-if-absent, multi-key transactions, listing, and thread safety.
4. **PayCo delivery details.** These are external and only documented in prose: whether `t` is refreshed on retry, how the secret is encoded, and whether body bytes are identical on redelivery.

## 4. Current State

- **Workspace:** I read `README.md`. It says the directory is empty apart from that file: "There is no existing code, documentation, configuration, or data here to inspect." **The ledgerkit README the brief points to is not here**, so I couldn't inspect the `MemoryStore` API, `StoreError` semantics, or how to inject faults. Every store-dependent decision below assumes the primitives in A1, and spike T1 checks them first.
- **Processes it replaces:** the nightly PayCo CSV export copied into a spreadsheet, plus manual fixes by support. That process doesn't use webhooks, so Ledger can run alongside it with no interference (see Section 15).
- **Conventions:** none exist, so this plan sets them: package `ledger/`, tests under `tests/` using `unittest`, run with `python -m unittest discover -s tests`.

## 5. Assumptions and Open Questions

**Assumptions**

| # | Assumption | Impact if wrong | Validation |
|---|---|---|---|
| A1 | The ledgerkit store gives per-key atomic `get`/`put`, some atomic put-if-absent or compare-and-set, and a way to list records by account (prefix or range scan). The production client has the same interface as `MemoryStore`. | Without put-if-absent: run a single instance and rely on a process lock (already planned). Without listing: keep a per-account index record updated with compare-and-set. | Spike T1, day 1 of M1 |
| A2 | `StoreError` can occur after a write has actually been applied (an ambiguous commit), not only before it. | None. The design already assumes the worse case. | T1 |
| A3 | PayCo signs each delivery attempt with a fresh `t`. | Any retry after 300 s would always be rejected, so a transient failure becomes permanent loss. Escalate to R2. | Spike T2 (force a retry in PayCo test mode) |
| A4 | The secret is used as PayCo provides it (e.g. the UTF-8 bytes of the `whsec_…` string) and is not base64-decoded. | Every signature fails. | T2: verify real test-mode deliveries |
| A5 | The HTTP layer passes the exact raw body bytes and all headers through to `handle()` unchanged, and caps body size (256 KiB). | Every signature fails, or memory can be exhausted. | Contract test in M3 with the HTTP-layer owner |
| A6 | Ledger's balance is PayCo-funded credit only. Product spending is tracked elsewhere. | The data model needs non-PayCo entry sources and a different reconciliation filter. | Open question Q3 |
| A7 | Python ≥ 3.10 in all environments. | Minor syntax changes. | T3 |
| A8 | One engineer full-time, a reviewer part-time, and the platform/HTTP owner for about 2 days in M3. | Milestone sizes change. | Planning kickoff |
| A9 | Financial records are kept for 7 years. | Retention config changes. | Finance, before M4 |

**Open questions**

| # | Question | Owner | Default if unanswered | Needed by |
|---|---|---|---|---|
| Q1 | Exactly what can the ledgerkit store do: put-if-absent/CAS, multi-key transactions with rollback, listing, thread safety, built-in fault injection? | ledgerkit owner | Assume A1 and add the fallbacks | M1 day 1 |
| Q2 | Does PayCo refresh `t` on retries? Does it disable endpoints after repeated failures? Is there an events API to list or re-fetch events by id? | PayCo integration contact / test mode | Assume A3; no events API | M1 (T2) |
| Q3 | Should product spending or support adjustments ever be posted to Ledger? | Product + Finance | No | Before M2 |
| Q4 | How do pre-existing balances enter Ledger? Does the CSV export include event ids? | Finance | One opening-balance entry per account at cutover | M3 |
| Q5 | Who owns the HTTP layer and on-call? Is a single instance acceptable? | Engineering manager | Single instance; business-hours on-call; security incidents page | M3 |

## 6. Architecture Overview

```
  PayCo ──HTTPS POST (PayCo-Signature, raw JSON)──▶ HTTP layer (platform-owned, outside this repo)
  ═══════════════ trust boundary: internet → our network ═════════════════  │ raw bytes + headers, unmodified
                                                                            ▼
  ┌──────────────────────────── ledger package (in-process) ────────────────────────────┐
  │ LedgerService (facade, process lock)                                                │
  │   handle():  SignatureVerifier ─▶ EventParser ─▶ Inbox ─▶ Posting handlers           │
  │                 (secret)            (strict)     (dedup,    (payment, refund,       │
  │                                                  conflict)  registry by type)       │
  │   balance()/entries(): Projection (sum / sort of entries)                           │
  │                         │                                                           │
  │                 LedgerRepository (key encoding, claims, idempotent puts)            │
  └─────────────────────────┼───────────────────────────────────────────────────────────┘
                            ▼
                ledgerkit store ──▶ managed network DB
  Ops tools (M3): redrive (re-run stored events), reconcile (vs PayCo CSV export) ──▶ LedgerRepository
```

**How it fits together.**
1. `handle()` checks the signature against the raw bytes before doing anything else.
2. It parses only enough to get `id` and `type`.
3. The Inbox records the raw body and its SHA-256 under the event id, or finds the existing record. A hash mismatch is the incident case.
4. The handler for the event type then applies its effects with idempotent, keyed writes: claims (put-if-absent) and entries (deterministic key and value).
5. Reads calculate the result from entries.

Nothing is ever incremented, so running any delivery again, in whole or in part, converges on the same state.

| Component | Responsibility | Owns data | Interfaces | Tech | Depends on |
|---|---|---|---|---|---|
| LedgerService | Public facade. Maps outcomes to HTTP status. Holds the process lock. | none | `handle`, `balance`, `entries` (sync) | stdlib | all below |
| SignatureVerifier | Parse `PayCo-Signature`, check HMAC and the ±300 s window | none | `verify(raw, headers, secret, now)` raises `SignatureError` | `hmac`, `hashlib` | clock |
| EventParser | Strict JSON parsing. Validates the envelope; validates `data` for known types. | none | `parse_envelope(raw)`, `validate_payment/refund(evt)` | `json` | none |
| Inbox | Event dedup, id/body conflict detection, event state, incident records | `event`, `incident` | `record(id, hash, raw)`, `mark(id, state)` | stdlib | Repository |
| Posting handlers | Apply payment and refund effects. Park, quarantine. | `account`, `payment_claim`, `refund_claim`, `entry`, `quarantine` | `apply(evt) -> outcome`; registry `{type: handler}` | stdlib | Repository |
| Projection | Balance = sum of entries; ordered entry list | none (read-only) | `balance(acct)`, `entries(acct)` | stdlib | Repository |
| LedgerRepository | The only code that touches the store: key encoding and the store primitives | the physical layout | internal methods | ledgerkit | store |
| Ops tools (M3) | Redrive stuck or ignored events; daily reconciliation against PayCo CSV | reports only | CLI `python -m ledger.tools.…` | stdlib | Repository, service |

## 7. Component Details

**SignatureVerifier**
- Finds the header with a case-insensitive key match. Splits on `,` and each part on the first `=`, trimming whitespace. Requires exactly one integer `t` and at least one `v1`. If there are several `v1` values, any match passes; this allows PayCo-side secret rotation.
- Computes `hmac.new(secret, f"{t}.".encode() + raw_body, sha256).hexdigest()` and compares with `hmac.compare_digest`.
- Rejects when `abs(clock.now() - t) > 300`. Exactly 300 is accepted.
- Never parses the body.
- On any failure it returns 401 with a reason code that is logged but not returned in detail.
- It is stateless and fails closed.

**EventParser**
- Uses `json.loads` with `parse_constant` set to reject `NaN`/`Infinity`, and `object_pairs_hook` set to reject duplicate keys.
- Envelope rules: `id` and `type` must be non-empty strings, and `created` must be an int.
- Validation for known types:
  - `account_id` and `payment_id` must be non-empty strings of at most 255 characters.
  - `amount` must be an `int` but not a `bool`, and greater than 0. `2500.0` is rejected.
  - `currency` must match `^[A-Z]{3}$`.
- If the envelope fails, we have no id, so the result is 400. If a known type's `data` fails, the event already has an id, so it is quarantined (see Decision D6).

**Inbox**
- `record` does a put-if-absent of `{v:1, body_sha256, raw_body, type, received_at, state:"received"}`, then reads the record back.
  - Same hash: carry on. This is a duplicate or a resumed attempt, and processing runs again.
  - Different hash: write an `incident` record keyed by `(id, new_hash)` on a best-effort basis, log at ERROR, and return 409.
- `mark` overwrites `state` with one of `applied`, `ignored`, `pending`, or `quarantined`. State is for observability and redrive only. Correctness never relies on it.

**Posting handlers.** These are the hard part. Each step is idempotent, and a `StoreError` at any step returns 503.

*Payment `p` (event `e`):*
1. `claim_currency(account, currency)` does put-if-absent and reads back. If a different currency comes back, quarantine `CURRENCY_MISMATCH` and stop.
2. `claim_payment(payment_id → e, account, amount, currency)` does put-if-absent and reads back. If a different event holds the claim, quarantine `DUPLICATE_PAYMENT_ID` and stop.
3. `put_entry((account, e), {type, amount:+a, currency, created, payment_id})` writes the same key and value on every retry.
4. **Then** read `refund_claim(payment_id)`. If one is present, run the *refund-apply* routine for that refund.
5. Return `applied`.

*Refund `r`:*
1. `claim_refund(payment_id → r)` does put-if-absent and reads back. If a different refund holds the claim, quarantine `DUPLICATE_REFUND`. Refunds are full, so a second refund for the same payment is impossible.
2. **Then** read `payment_claim(payment_id)`. If there is none, return `pending` (2xx). If there is one, run *refund-apply*.

*Refund-apply* is shared by both sides, deterministic, and idempotent.
- Check `refund.account_id == payment.account_id`, `amount ==`, and `currency ==`. If any check fails, quarantine `REFUND_MISMATCH`.
- Otherwise `put_entry((account, r), {type:"refund.succeeded", amount:−a, …})`, and mark `r` as applied.

Both handlers follow the same rule: write your own claim, then read the other's. With a linearizable store, at least one of two concurrent handlers sees the other, so a refund cannot stay stuck in pending. Applying it twice writes the same entry key.

*Unhandled types* do nothing and return `ignored` (2xx). The raw body is already in the Inbox.

**Projection**
- `balance` is the sum of `amount` over the account's entries. An account we've never seen returns 0.
- `entries` sorts by `(effective_created, kind_rank, event_id)`:
  - `effective_created` for a refund is `max(refund.created, payment.created)`.
  - `kind_rank` puts payment before refund.
- The output is deterministic for any arrival order.

**LedgerService**
- Holds one `threading.Lock` around the write part of `handle()` (see D8).
- Status mapping: `200 {"status": applied|duplicate|ignored|pending|quarantined, "event_id"}`, `400 malformed`, `401 invalid_signature`, `409 event_id_conflict`, `503` for `StoreError`, and `500` for unexpected exceptions. Every non-2xx makes PayCo retry, so a bug becomes a retry rather than lost money.
- Response bodies never echo internals.
- "Duplicate" is reported when the Inbox record already existed with the same hash *and* reprocessing found nothing new. It is still a full re-run.

**Failure and scale.**
- Every failure leaves only idempotent writes behind, and the next delivery or redrive completes them.
- A delivery takes about 6–10 store round trips (~100 ms at 10 ms each), against a load of about 0.05 events/s on average. Serialised under the lock, capacity is about 10 events/s, which is about 200 times today's average.

## 8. Data Design

| Record | Key | Value (all carry `v:1`) | Writer | Write mode |
|---|---|---|---|---|
| event | `event_id` | body_sha256, raw_body, type, received_at, state | Inbox | put-if-absent; `state` overwrite |
| incident | `(event_id, body_sha256)` | raw_body, received_at | Inbox | put |
| account | `account_id` | currency, first_event_id | Posting | put-if-absent |
| payment_claim | `payment_id` | event_id, account_id, amount, currency, created | Posting | put-if-absent |
| refund_claim | `payment_id` | refund event_id | Posting | put-if-absent |
| entry | `(account_id, event_id)` | type, amount (signed int), currency, created, payment_id | Posting | deterministic put |
| quarantine | `event_id` | reason code, detail | Posting | put |

- **System of record:** PayCo owns the money facts. Ledger's `event` records are our durable copy, and every other record can in principle be rebuilt from them by redrive.
- **Consistency:** single-key atomic writes only. Order of operations and idempotency replace transactions (D2). If T1 finds multi-key transactions with rollback, we wrap each handler's writes in one transaction as defence in depth, and keep the idempotent design because commits can still be ambiguous.
- **Access patterns:**
  - get event by id
  - get claims by `account_id` and `payment_id`
  - list entries by `account_id`, which needs a prefix or range scan, or a per-account index (A1)
  - list events by state, for redrive
- **Key encoding:** tuple keys if the store supports them. Otherwise a length-prefixed encoding, so an id containing `:` cannot collide with another key.
- **Retention and backups:** keep everything for 7 years (A9). Use the managed database's PITR. A restore loses acknowledged events after the restore point. The daily reconciliation detects them, and a backfill brings them back (M4/deferred).
- **Classification:** the data is confidential financial data: account ids, payment ids, amounts. Card data isn't expected. If PayCo adds personal data to bodies, `raw_body` holds it, so review this when new types are adopted. The secret is never stored.
- **Schema evolution:** changes are additive only. Readers tolerate missing optional fields, and the `v` field lets a later version migrate when records are read.
- **Volume:** about 1 KB per event across all records, so about 3 MB a day today and roughly 1 GB a year.

## 9. Key Flows

**F1: Payment, happy path.** A valid signature and a parsed envelope lead to the Inbox writing the new event record. The payment handler then:
1. claims currency EUR for `acct_42`
2. claims `pay_9xk`
3. puts entry `(acct_42, evt_1Nq3xKp, +2500)`
4. finds no refund claim

The Inbox marks the event `applied` and the response is `200 applied`. `balance("acct_42") == 2500`.

**F2: Refund before payment, including the race.**
1. Refund `evt_R` arrives first. It claims refund on `pay_9xk`, finds no payment claim, and returns `200 pending`. The balance is unchanged at 0, which is correct because nothing was credited.
2. Payment `evt_P` arrives an hour later. It works through currency claim, payment claim, and entry +2500, then reads the refund claim and finds `evt_R`.
3. *Refund-apply* validates the refund (same account, amount, and currency) and puts entry `(acct_42, evt_R, −2500)`. It marks `evt_R` applied and `evt_P` applied, and returns 200.
4. The final balance is 0. `entries` lists +2500, then −2500.

If the two arrive at the same moment on different instances, the write-then-read rule ensures at least one side applies the refund. If both do, they write the same key.

**F3: StoreError partway, then redelivery (failure path).**
1. A payment's Inbox write and currency claim succeed. `put_entry` raises `StoreError`, perhaps after the write actually landed. The response is `503`.
2. PayCo retries with backoff. The Inbox record exists with the same hash, so processing runs again.
3. The currency claim reads back EUR, which matches. The payment claim reads back as held by this event, so processing continues. `put_entry` writes the same key and value, and the event is marked applied.
4. The response is `200`, and the entry exists exactly once.

A short-circuit design ("seen, so return 200") would have left the account permanently uncredited here. The M1 fault harness is built specifically to catch that bug. If PayCo gives up after 72 h, the M3 redrive job re-runs any event stuck in `received` for more than 10 minutes, using the stored raw body.

**F4: Same id, different body (incident).** The signature is valid and the id is already stored, but the hash differs. The Inbox writes `incident(evt_id, new_hash)` on a best-effort basis and logs `event_id_conflict` at ERROR with both hashes. The response is `409`, and no Posting handler runs. PayCo will keep retrying for 72 h, so alerts are grouped by event id. The security runbook covers checking the secret, rotating it if needed, and contacting PayCo.

**F5: Bad signature or stale `t`.** The response is `401` before the body is parsed and before any store access. If the secret is misconfigured, PayCo's 72 h of retries gives us time to fix it. The alert in Section 11 fires within 15 minutes.

## 10. Key Decisions

| # | Decision | Options considered | Rationale (drivers) | Reversibility | Revisit if |
|---|---|---|---|---|---|
| D1 | Balance is calculated from entries on every read | (a) stored counter, incremented; (b) counter recalculated from entries; (c) **calculated on read** | (a) double-counts on retry (driver 1). (b) races under concurrency. (c) can't drift, and costs about one scan per read at our volume. | Hard (data model). We can add a materialised snapshot later without changing the model. | `balance()` p95 > 50 ms, or any account has more than 10k entries |
| D2 | Idempotent keyed writes, and every delivery runs the whole pipeline again | (a) dedup marker first, then short-circuit; (b) one multi-key transaction; (c) **idempotent steps, full re-run** | (a) loses money after a partial write. (b) depends on an unknown store feature and still has ambiguous commits. (c) is correct on the weakest store. | Hard | None foreseen. (b) is added as defence in depth if available. |
| D3 | Refund before payment: park durably and return 2xx | (a) return 5xx and let PayCo retry; (b) apply immediately, allowing negative balance; (c) **park, apply when the payment arrives** | (a) loses the refund if the payment is more than 72 h late, and creates retry noise. (b) can't validate the refund and shows customers negative balances. (c) is durable and validated. | Medium | PayCo guarantees ordering |
| D4 | Unhandled types: return 200 and keep the raw body | (a) 4xx/5xx; (b) 200 and discard; (c) **200 and store** | (a) means 72 h of retries and then loss. (c) lets us back-apply when handlers ship (driver 4). | Easy | Storage cost matters (it won't) |
| D5 | "Same body" means identical raw bytes (SHA-256) | (a) **raw bytes**; (b) canonicalised JSON | Matches the brief's wording and what the signature covers. It's strict, so any false positive shows up loudly. | Easy | Shadow mode shows legitimate redeliveries whose bytes differ |
| D6 | Anomalies (currency mismatch, refund mismatch, duplicate payment or refund, invalid `data`) are quarantined with 2xx and an alert | (a) 4xx, relying on PayCo retries; (b) **quarantine** | A retry never fixes these. (a) eventually drops them silently. (b) keeps them durable and visible, and reconciliation can explain every difference. | Easy | Finance wants retries kept as a holding pattern |
| D7 | Status codes: 401 signature, 400 no envelope, 409 conflict, 503 store, 500 bug | Single 400 for everything | Distinct codes make PayCo's dashboard and our metrics diagnostic. PayCo only cares about 2xx vs non-2xx. | Easy | none |
| D8 | One active instance plus an in-process lock around writes; put-if-absent claims for safety across instances | (a) multi-instance from day 1; (b) **single instance and lock** | PayCo's 72 h retries make availability the lowest-ranked driver. The lock removes a whole class of races. | Easy | Sustained > 2 events/s, or an HA requirement |
| D9 | Entry order is `created`, with refund after its payment | Order of application (needs a store sequence) | Deterministic for any arrival order, with no store feature required. | Easy (it's calculated) | Support wants arrival order |
| D10 | Currency is fixed by the first payment *applied*, using a first-writer-wins claim | Earliest by `created` | Currency must never change after the fact, and a late-arriving older payment would otherwise flip it. | Medium | Finance disagrees |

## 11. Cross-Cutting Concerns

**Security (M1, with M3 for secrets and ops)**

| Threat | Mitigation |
|---|---|
| Forged webhook crediting money | HMAC-SHA256 over `t.` + raw bytes, `compare_digest`, checked before parsing and before any store access |
| Replay of a captured request | ±300 s window. Inside the window, a replay is a duplicate and has no effect (D2). |
| Same id with a different body (leaked secret or PayCo fault) | 409, incident record, page to security (F4) |
| Secret leakage | Secret injected from the secret manager as bytes. Never logged; a test checks the logs. Rotation runbook in M3. Support for multiple secrets is deferred. |
| Oversized or malicious bodies; key injection | HTTP layer caps the body at 256 KiB (A5). Strict JSON, ids limited to 255 characters, collision-safe key encoding. |

Read APIs (`balance`, `entries`) are in-process. Callers authorise users at their own boundary.

**Reliability.**
- The core invariant is that 2xx means durable.
- Idempotency comes from D1 and D2. Redrive (M3) covers events PayCo stops retrying.
- Timeouts: we recommend the HTTP layer applies a 10 s request deadline (assumption, below PayCo's timeout). Store client timeouts come from ledgerkit (checked in T1).
- No retries happen inside `handle()`. PayCo is the retry loop, so there's no amplification.

**Observability.**
- *M1:* one structured log line per delivery: `event_id`, `type`, `account_id`, `outcome`, `status`, `duration_ms`, `store_ops`, `error_class`. Uses `logging` with the logger name `ledger`.
- *M3:* counters calculated from those logs, plus these alerts:
  - any 409 pages security
  - any 401s for more than 15 minutes page on-call, because that pattern looks like a misconfigured secret or HTTP layer
  - 503/500 rate above 5% over 15 minutes raises a ticket
  - a new quarantine raises a ticket
  - a refund pending for more than 24 h raises a ticket
  - no deliveries for 3 h raises a ticket, since we expect about 2 a minute
  - any difference in the daily reconciliation raises a ticket

**Performance and capacity.** Load is trivial. Bottlenecks are store round trips and the lock (about 10 events/s). M3 includes a load test that replays 10× peak (assumption: 1 event/s) against the staging store.

**Cost.** One small instance plus about 1 GB a year of database rows, so negligible. There's nothing to control.

**Operations.**
- Owner: the team that builds it (Q5).
- Because PayCo retries for 72 h, business-hours on-call is enough. Only security incidents and failing signatures page out of hours.
- Runbooks (M3): incident (409); secret rotation; quarantine triage; stuck pending refund; redrive; restore and backfill.

**AI-specific concerns:** not applicable. The system has no AI components.

## 12. Build Sequence

Team assumption: one Python engineer, a part-time reviewer, and about 2 days from the HTTP-layer owner in M3. Sizes are rough guides.

| Milestone | Goal | Scope / excluded | Exit criteria | Depends on | Size |
|---|---|---|---|---|---|
| **M1: Payments, exact under faults** | Clear hard parts 1, 3, and 4 | Signature, parsing, Inbox, payment handler, unhandled types, 409, 503, projection, fault harness, both spikes. *Excludes* refunds and ops tools. | Section 13 tasks done. CI green. The harness has 0 failures over 2,000 seeds and **fails** against a deliberately broken short-circuit variant. Real PayCo test-mode fixtures verify. | none | 5–8 days |
| **M2: Refunds and anomalies** | Clear hard part 2 | Refund handler, parking, the payment-side check, currency claim enforcement, quarantine reasons, ordering of entries. Harness extended with shuffled refunds and concurrency (threads). | Randomised harness with refunds, ≥10,000 runs, 0 failures. A threaded race test (refund and payment at the same time, 1,000 iterations) leaves nothing stuck in pending. Every D6 case has a test. | M1 | 4–6 days |
| **M3: Running in staging** | Integration with real infrastructure and ops | HTTP-layer contract test (raw bytes, headers, size cap), staging deploy receiving PayCo test-mode traffic, alerts, `redrive` and `reconcile` tools, runbooks, load test | A test-mode payment and refund show up correctly in staging. An injected `StoreError` in staging is recovered by PayCo's retry. Every alert fires on an injected fault. Reconcile against a sample CSV produces a correct difference report. | M2; HTTP owner (can run in parallel from M1) | 6–10 days |
| **M4: Shadow and cutover** | Prove the exact match against a real statement | Production webhook live in shadow mode; opening balances (Q4); daily reconcile; one full monthly statement; cutover | The monthly statement matches with 0 unexplained differences. Daily reconcile is clean for 2 weeks. Finance signs off. | M3, Q4 | 2–4 days of effort, about 5–6 weeks elapsed |

**Critical path:** T1 (store spike) → T6 (repository) → T7 (service) → T8 (harness) → M2 → M3 staging → M4 statement cycle. The statement cycle is the longest item, so start M4's shadow run as early as M3 allows. **Long lead, request on day 1:** PayCo test-mode access and a webhook secret, a staging database, and the HTTP owner's time.

## 13. First Milestone Task Breakdown

1. **T1 Spike: ledgerkit store capabilities** (time box: 0.5 day; starts day 1).
   - Question: which primitives exist (put-if-absent/CAS, transactions with rollback, prefix listing, thread safety, built-in fault injection), and can `StoreError` fire after a write has landed?
   - Deliverables: `docs/store-notes.md`, and `tests/test_store_contract.py` pinning every behaviour we rely on.
   - Changes the plan if: there is no put-if-absent (rely on D8's lock and a single instance as a documented hard limit); there is no listing (add a per-account index record with CAS); there are transactions (add them as defence in depth).
2. **T2 Spike: PayCo real deliveries** (time box: 1 day, in parallel with T1; needs test-mode access).
   - Write `tools/capture_server.py`, a dev-only receiver built on `http.server` that saves raw body bytes and headers. Expose it through a tunnel and trigger a payment and a refund. Return 500 once to force a retry.
   - Answer: is `t` fresh on the retry? Are the body bytes identical? What is the secret's encoding? Any quirks in the header format?
   - Save the results to `tests/fixtures/payco/`.
   - Changes the plan if: `t` isn't refreshed (raise R2 immediately), or bodies differ in bytes (revisit D5).
3. **T3 Create the skeleton and CI.** Add `ledger/__init__.py` exporting `LedgerService`, a `tests/` directory, and a CI job running `python -m unittest discover -s tests` on Python 3.10 (A7). Done when CI is green with one smoke test.
4. **T4 Implement `ledger/signature.py`.** Done when tests in `tests/test_signature.py` cover:
   - valid signature
   - wrong secret
   - a one-byte change to the body
   - `t` = now ± 300 accepted, ± 301 rejected
   - header names `payco-signature` and `PAYCO-SIGNATURE`
   - missing header, missing `t` or `v1`, non-integer `t`, non-hex value
   - multiple `v1` values
   - T2's fixtures verifying

   Can run in parallel with T5 and T6.
5. **T5 Implement `ledger/events.py`.** Done when tests reject `true`, `2500.0`, `0`, and `-5` as amounts; reject `NaN`, duplicate keys, missing fields, and `eur`; and pass unknown types through untouched.
6. **T6 Implement `ledger/repository.py`** (after T1). It provides key encoding, `record_event`, `claim_currency`, `claim_payment`, `claim_refund` (stubbed until M2), `put_entry`, `list_entries`, `set_state`, and `put_quarantine`. Done when the unit tests on MemoryStore pass, and a test with ids containing separator characters shows no collisions.
7. **T7 Implement `ledger/service.py` and `ledger/handlers.py`.** Covers the payment handler, the unhandled-type handler, Inbox conflict handling, status mapping, the lock, and `balance`/`entries`. Done when:
   - the brief's example produces balance 2500 and one entry
   - a redelivery returns 200 with no change
   - the same id with one changed byte returns 409, the account is unchanged, and an incident record exists
   - `dispute.created` returns 200 with no entry and its raw body stored
   - an unknown account has balance 0 and `[]` entries
8. **T8 Build the fault harness.**
   - `tests/fakes.py`: `FakeClock`, a `sign()` helper, and `FaultyStore`, which raises `StoreError` before *or after* the Nth write.
   - `tests/test_faults.py`, deterministic part: for every write position k in a payment, inject a failure, expect 503, redeliver, and assert the state is identical to a run with no faults.
   - Randomised part: 2,000 seeds, each with 50 events, with duplicates, shuffled order, and a 5% fault rate. Redeliver until 2xx, then compare with the oracle.
   - Done when it passes **and** a flag that turns on the short-circuit bug makes it fail.
9. **T9 Add the structured log line.** Done when a test asserts the fields are present and that neither the secret nor the signature appears in any log output.

Order: T1, T2, and T3 on day 1. T4 and T5 run in parallel with T6. T7 follows. T8 starts its fakes alongside T6 and finishes after T7. T9 goes with T7.

## 14. Testing and Validation Strategy

| Risk | Test type | Where |
|---|---|---|
| Forgery, replay, header quirks | Unit tests plus real fixtures | `test_signature.py` (M1) |
| Partial writes, duplicates, ordering | Deterministic fault-position tests plus a randomised oracle harness (stdlib `random`, fixed seeds) | `test_faults.py` (M1, extended in M2) |
| Refund/payment races | Threaded stress test | M2 |
| Assumptions about store behaviour | Store contract tests, run against MemoryStore in CI and against the staging DB in M3 | `test_store_contract.py` |
| HTTP layer changing bytes | Contract test that posts the fixtures through the real HTTP layer | M3 |
| Exact match with Finance | Daily `reconcile` plus the monthly statement during shadow | M3/M4 |

**Blocks a release:** all unit and harness tests, the contract tests, and a seed count ≥ 2,000 per CI run (10,000 nightly). Driver 1 is verified by the harness and then by the M4 statement. Driver 2 by the signature and conflict tests. Driver 3 by fault injection in staging. Driver 5 by the M3 load test.

## 15. Rollout, Migration, and Rollback

- **Side by side:** the existing CSV-to-spreadsheet process doesn't use webhooks, so Ledger receives production webhooks in shadow mode with no interference. Nobody reads its balances yet.
- **Opening balances (Q4):**
  - Default: at cutover, add one `ledger.opening_balance` entry per account, taken from PayCo's statement up to a date at the end of a month. Webhook events created before that date are excluded from the reconciliation scope.
  - Better, if the CSV or a PayCo events API provides event ids: backfill the historical events through `reprocess`. The Inbox dedup then handles any overlap automatically.
- **Switchover criteria:** a clean daily reconcile for 2 weeks, plus one monthly statement with 0 unexplained differences and Finance sign-off. After that, support reads balances from Ledger and stops editing the spreadsheet.
- **Rollback:**
  - The store is append-only, so rolling back code is always safe.
  - Before cutover, rollback means continuing to use the spreadsheet.
  - **Point of no return:** when support stops maintaining the spreadsheet. Keep the nightly script running read-only for one more statement cycle afterwards, then retire it.

## 16. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation | Early warning | Owner |
|---|---|---|---|---|---|
| R1: The ledgerkit store lacks put-if-absent, listing, or thread safety | Med | High | T1 on day 1; fallbacks (D8 lock, CAS index) | T1 findings | Engineer |
| R2: PayCo reuses `t` on retries, so retries after 300 s always fail | Low–Med | High | T2 checks this. If confirmed: rely on redrive from the stored body (step 1 is the first write) and daily reconcile plus backfill for failures before step 1. Ask PayCo. | T2 retry capture | Engineer + PayCo contact |
| R3: Disputes or other unhandled money events make reconciliation differ | High (eventually) | Med | Brief Finance. Reconcile reports ignored events by type. Prioritise dispute handling when the first one appears. | `ignored` count for money-moving types | Finance + Engineer |
| R4: HTTP layer re-encodes the body, so every signature fails | Med | High (no credits) | A5 contract, M3 contract test, 401 alert | 401 spike in staging | HTTP owner |
| R5: Legitimate redeliveries differ in bytes, causing false 409s | Low | Med | D5 is strict and loud. Watch during shadow. | Any 409 in shadow | Engineer |
| R6: Opening-balance approach not agreed in time | Med | Med (delays cutover) | Q4 raised now. Default is an opening entry. | No answer by M3 | Finance |
| R7: Scope creep into product spending | Med | High (model change) | Q3 answered before M2. Kept as a non-goal. | Requests from the product team | Eng manager |

## 17. Deferred Work and Future Evolution

- **Handlers for disputes and payouts.** Trigger: the first such event, or a Finance request. Extension point: the handler registry plus `reprocess` over stored `ignored` events.
- **Multiple secrets for rotation.** Trigger: the first rotation. `LedgerService` gets an optional `secrets=[…]` argument.
- **A materialised balance snapshot (with CAS on a version).** Trigger: D1's revisit condition.
- **Multiple instances.** Trigger: D8's revisit condition. Requires put-if-absent confirmed from T1.
- **Backfill from the PayCo API or CSV,** for restores and opening balances. Trigger: Q4 answer, or the first restore.
- **Deliberate shortcuts:**
  - The global lock: pay it back by moving to locks per account, or none, once store atomics are proven.
  - Scan-on-read: pay it back with the snapshot above.

## 18. Next Steps

1. Ask the ledgerkit owner for the store README and API (Q1), then run **T1** and commit `tests/test_store_contract.py`.
2. Request PayCo test-mode access and a test webhook secret today. It's on the critical path for **T2**.
3. Create `ledger/` and `tests/`, and wire `python -m unittest discover -s tests` into CI (**T3**).
4. Start **T4** (`ledger/signature.py`) and **T5** (`ledger/events.py`) in parallel; neither needs store answers.
5. Send Finance Q3 (spending and adjustments) and Q4 (opening balances), and warn them about R3 (disputes).

---

**Open questions that would most change the plan**

1. **What does the ledgerkit store actually provide?** The README it refers to isn't in the workspace. The concurrency model and the per-account listing both depend on whether it has put-if-absent/CAS, multi-key transactions, prefix listing, and thread safety.
2. **Does PayCo re-sign each retry with a fresh `t`?** If it doesn't, the 300 s rule turns every transient `StoreError` into permanent loss for events that failed before their first write. We would need a different recovery path.
3. **Will product spending or support adjustments ever be posted to Ledger?** If yes, the entry model, the reconciliation filter, and the concurrency needs all change before M2.
4. **How do today's balances get into Ledger?** Do PayCo's exports include event ids? That decides between a clean backfill and opening-balance entries, and sets the cutover date.
5. **Is a single instance with business-hours on-call acceptable?** That's the basis for D8 and the alerting plan. If the answer is no, multi-instance work moves into M3.
