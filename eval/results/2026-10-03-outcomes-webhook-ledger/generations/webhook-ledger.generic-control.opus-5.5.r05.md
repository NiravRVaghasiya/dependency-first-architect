I've read both reference files and checked the workspace. Here is the plan.

---

# Ledger: Architecture and Build Plan

## 1. Summary

- **What:** Ledger is a small Python library (standard library only) behind our existing HTTP layer. It turns PayCo webhooks into per-account balances that match PayCo's statement to the minor unit. It replaces the nightly CSV-to-spreadsheet script and the hand fixes support does today.
- **Shape:** It runs as one process: `LedgerService.handle()` verifies the signature, parses the event, stores it, and applies it, all inside the request. Only the `LedgerRepository` module talks to the ledgerkit store. There is no queue and no separate service.
- **Key decisions:**
  1. **The rule for 2xx.** We return 2xx only once the event is safely stored (applied, ignored, or parked for a human). For anything else we return a non-2xx code, so PayCo's 72-hour retries act as our recovery mechanism.
  2. **Idempotent replay instead of transactions.** Every write is keyed by the event and computed from it, so running it twice gives the same result. A redelivery re-runs the whole pipeline; it does not just skip. Any partial write left by a `StoreError` is completed by PayCo's next retry.
  3. **Balance is the sum of entries.** There is no separate balance counter that could drift away from the entries.
  4. **Refunds are applied as soon as they arrive,** even before their payment. The balance is a plain sum, so arrival order doesn't change the final number.
  5. **Duplicate detection compares parsed content, not bytes.** Same `id` with the same canonical JSON is a duplicate. Same `id` with different content is a security incident: we return 409 and nothing is written to the account.
- **Milestone 1 (about 1–1.5 engineer-weeks):** two spikes (the ledgerkit store API, and PayCo sandbox delivery behaviour) plus a thin slice. In that slice a real PayCo sandbox `payment.succeeded` reaches `handle()` in staging, shows up in `balance()`, and a fault-injection test proves a `StoreError` at any write point recovers correctly on redelivery.
- **Top risks:** (1) we haven't seen the ledgerkit store API: the brief says "see README.md", but the workspace README doesn't describe it. (2) PayCo may not re-sign retries with a fresh `t`, which would break retries under the 300-second rule. (3) Opening balances for existing customers at cutover. (4) The HTTP layer, which we don't own, might change the body bytes before we verify the signature.

## 2. Context and Goals

- **Problem:** Today balances are copied nightly from PayCo's CSV into a spreadsheet and fixed by hand. They are up to a day stale, nobody can audit the edits, and Finance's monthly reconciliation runs against hand-edited data.
- **Goals:**
  - Each account's balance reflects every PayCo payment and refund within a minute of PayCo's first delivery attempt.
  - Every account matches PayCo's monthly statement exactly. Any difference is explained by an open, visible exception record.
  - No event is lost or applied twice, whether through duplicates, out-of-order delivery, or store failures.
- **Non-goals (this plan):** customer spending and debits (see open question Q4); handling disputes, payouts, or other event types (we store them, but don't apply them); the HTTP server, deployment and monitoring stack (we specify the contract only); multi-currency accounts; partial refunds; a manual-adjustment UI.
- **Success measures:** zero unexplained reconciliation differences for 14 straight days in shadow mode, and then at the first monthly statement after cutover. Support's hand fixes to balances drop to zero. Exceptions are triaged within one business day.

## 3. Drivers, Requirements, and Constraints

**Ranked quality attributes**

| # | Attribute | Measurable target | Why it matters |
|---|---|---|---|
| 1 | Financial correctness | Each distinct event's effect is applied at most once and at least once. 0 minor-unit difference per account against PayCo, apart from open exceptions | Finance needs an exact match; double credit is real money lost |
| 2 | Integrity and authenticity | 100% of applied events have a verified HMAC. A conflicting body for a known `id` changes nothing and raises an incident within 5 minutes *(assumed target)* | Forged or changed events would mint credit |
| 3 | Recoverability | RPO = 0 for any event we answered 2xx. RTO under 4 hours *(assumption)*; the hard ceiling is PayCo's 72-hour retry window | Store failures are expected; anything we fail to 2xx must be retried |
| 4 | Operability for a small team | No 3 a.m. pages apart from security incidents. Every non-applied event shows up in an exceptions list | The 72-hour retry window allows business-hours response |
| 5 | Evolvability | A new event type means adding a handler and replaying stored events of that type, in under 1 day *(assumption)* | PayCo adds types; the brief says we'll handle them later |
| – | Latency and scale | p99 `handle()` under 1 second at 10 events/s *(assumption)*. Today's load is about 0.05/s | Comfortably met; not a structural driver |

**Key functional requirements:** signature and timestamp verification; credit `payment.succeeded`; debit `refund.succeeded`, including before its payment; accept and store unknown types; detect duplicates and conflicting bodies; fix currency at the first money movement; the `balance()` and `entries()` reads.

**Constraints:** Python standard library only. The fixed `LedgerService(store, secret, clock)` interface. The store is a ledgerkit `MemoryStore` here, and in production a managed network database that raises `StoreError` partway through requests. HTTP, deployment and monitoring live outside this codebase. *Assumption:* Python 3.10+, 1–2 engineers, no fixed deadline.

**Hard parts**

- **H1 — Exactly-once effect under partial writes, duplicates and concurrency**, on a store whose atomicity features we haven't seen. A wrong write order silently loses or doubles money.
- **H2 — The status-code policy.** A 2xx is a promise to PayCo that it can stop retrying. Returning 2xx too early loses events; returning 4xx too often causes 72 hours of pointless retries.
- **H3 — Out-of-order refunds and refund–payment pairing.** The rules must give the same final state in any arrival order.
- **H4 — Signature verification over exact bytes through an HTTP layer we don't own.** Body re-encoding, header casing, clock skew, and whether retries get a fresh `t` all matter.
- **H5 — Cutover.** Existing customers have balances that never arrive as webhooks.

## 4. Current State

- **Inspected:** the workspace holds only `README.md`, which says the directory is "intentionally empty" with no code, docs, config or data. I found no `ledgerkit/__init__.py` or `ledgerkit.py`. **The brief's pointer to README.md for the `MemoryStore` API can't be followed.** So the store's methods, atomicity guarantees, value types and `StoreError` behaviour are unknown. Spike S1 resolves this, and the design keeps all store-specific code in one module (`ledger/repository.py`).
- **Exists elsewhere (not visible to me):** the nightly script that copies PayCo's CSV export, the support spreadsheet, the HTTP layer, and the CI system. The plan reuses the CSV export as reconciliation input and leaves the spreadsheet running until cutover.
- **Conventions to follow:** stdlib only, so tests use `unittest` and logging uses `logging`. Everything else is a new build; the module layout is proposed in §13.

## 5. Assumptions and Open Questions

**Assumptions**

| # | Assumption | Impact if wrong | How and when validated |
|---|---|---|---|
| A1 | ledgerkit offers at least a single-key conditional insert ("insert if absent") and a prefix or range scan, or multi-key transactions | Without these, concurrent first deliveries can race. We'd need a process lock and a single instance | Spike S1, day 1 of M1 |
| A2 | A `StoreError` may mean "not written" *or* "written, but the reply was lost" | If we assumed "not written", a retry could double-apply. The design already handles both | S1, plus fault tests for both modes |
| A3 | PayCo signs each delivery attempt with a fresh `t` | Otherwise every retry older than 5 minutes fails the timestamp check and retries are useless (see D8) | Spike S2: force a 503 in the sandbox and inspect the retry |
| A4 | Redeliveries carry the same JSON content; byte-identical is not required | If even the content differs, we raise false incidents | S2, comparing redelivered bodies |
| A5 | A refund's currency equals its payment's currency, and full refunds only (one refund per payment) | Pairing rules would mis-flag partial refunds | PayCo docs and S2; revisit if partial refunds are enabled |
| A6 | Ledger records PayCo money movements only; product spending is tracked elsewhere | If spending belongs here, we need a debit API and reconciliation that excludes spend entries | Q4, before M2 |
| A7 | PayCo payloads for payment and refund contain no card data and no personal data beyond `account_id` | Raw-body retention would then need stronger controls | S2 fixtures; check unknown types (disputes) as they arrive |
| A8 | The HTTP layer can hand `handle()` the exact raw body bytes and all headers | Signatures fail for every delivery | T10 in M1, with the HTTP layer owner |
| A9 | PayCo's delivery timeout is at least 10 seconds | Slow store calls look like failures and trigger retries (harmless, but noisy) | S2 / PayCo docs |

**Open questions** (also listed at the end)

| Q | Question | Owner | Default if no answer | Needed by |
|---|---|---|---|---|
| Q1 | ledgerkit `MemoryStore` API and production backend semantics | Platform/DB owner | Design for A1; fall back to an in-process lock | M1 day 1 |
| Q2 | Does PayCo re-sign retries, and are redelivered bodies stable? | Engineer via sandbox, PayCo support | A3/A4 hold | M1 end |
| Q3 | How do existing customers get opening balances? Does PayCo's export include event IDs? | Finance + engineer | Opening-balance entry per account at a cutoff, from PayCo data (not the spreadsheet) | M3 start |
| Q4 | Does Ledger also hold product spending? | Product owner | No (A6) | M2 start |
| Q5 | Does "oldest first" in `entries()` mean event time (`created`) or the order we applied them? Which timestamp does PayCo's statement use? | Finance | Event time, with ties broken by receipt time then `event_id`; reconcile by `created` | M2 |

## 6. Architecture Overview

```mermaid
flowchart LR
  PayCo[PayCo webhooks] -- "HTTPS POST\n(untrusted until HMAC verified)" --> HTTP[HTTP layer\n(outside codebase)]
  HTTP -- "raw bytes + headers" --> SVC
  subgraph Ledger process [Ledger library, in the HTTP layer's process]
    SVC[LedgerService\nhandle / balance / entries] --> SIG[signature]
    SVC --> EVT[events\nparse, validate, canonical hash]
    SVC --> HND[handlers\nregistry by type]
    HND --> REPO[LedgerRepository\nsole writer]
    SVC --> REPO
    OPS[ops (M3)\nreconcile, exceptions,\nrepair sweep, opening import] --> REPO
  end
  REPO -- "ledgerkit client" --> DB[(Managed DB\nvia ledgerkit store)]
  Product[Product / Support] -- "balance(), entries()" --> SVC
  CSV[PayCo CSV export /\nmonthly statement] --> OPS
```

**How the parts fit:** the HTTP layer passes raw bytes and headers to `handle()`. `signature` verifies the HMAC and timestamp. `events` parses the body and computes a canonical hash. `LedgerService` writes the event record first (this record also serves as our inbox and our duplicate check), then dispatches it to a handler by `type`. Handlers make further idempotent writes through `LedgerRepository`. Reads add up the entries. Trust boundaries are: the internet up to the HTTP layer (TLS), unauthenticated bytes up to the end of signature verification, and our network up to the DB.

| Component | Responsibility | Owns data | Interfaces | Tech | Depends on |
|---|---|---|---|---|---|
| HTTP layer | TLS, routing, body size cap, passing raw bytes through | none | HTTPS POST from PayCo; calls `handle()` | existing, not ours | Ledger |
| `ledger.signature` | Parse `PayCo-Signature`, check HMAC-SHA256 and the ±300 s window | none | `verify(headers, raw, secret, now) -> Ok \| Reject(reason)` | `hmac`, `hashlib` | – |
| `ledger.events` | Strict JSON parsing, envelope and field validation, canonical hash | none | `parse(raw) -> Event`, `canonical_hash(obj)` | `json` | – |
| `ledger.service` | Orchestration, status-code policy, catch-all error handling, reads | none (delegates) | `LedgerService` public API | stdlib | all below |
| `ledger.handlers` | Per-type rules: payment, refund, unknown types | none | `HANDLERS: dict[type, fn(event, repo)]` | stdlib | repository |
| `ledger.repository` | Key layout, idempotent writes, scans; **the only importer of ledgerkit** | all stored records (§8) | methods listed in §7 | ledgerkit | store |
| `ledger.ops` (M3) | Reconciliation, exception listing and resolution, repair sweep, opening-balance import | none (writes through the repo) | CLI `python -m ledger.ops ...` | stdlib | repository, CSV |

## 7. Component Details

### LedgerService.handle(): the pipeline and the status rule

Steps, in order:

1. **Size check.** Reject bodies over 256 KiB with 413 *(assumed cap)*.
2. **Signature.** Look up headers without regard to letter case (lower-case the keys once). If two headers differ only in case and have different values, reject with 400. Parse `t=...,v1=...`, accept several `v1` values, and ignore unknown schemes. Compute HMAC-SHA256 over `t_literal + "." + raw_body`, where `t_literal` is the exact `t` string from the header (not re-formatted). Compare with `hmac.compare_digest` and accept if any `v1` matches. Then require `abs(clock.now() - int(t)) <= 300`.
3. **Parse.** `json.loads` the raw bytes as UTF-8, rejecting duplicate keys (`object_pairs_hook`) and `NaN`/`Infinity` (`parse_constant`). `id` must be a non-empty string, or we can't key anything, so 400.
4. **Canonical hash.** SHA-256 of `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)`. Also keep a raw-bytes SHA-256 for forensics.
5. **Claim the event (W1).** Insert `event/{id}` if absent. If it already exists with a different canonical hash, record an incident (best effort), log at CRITICAL, and **return 409**. Nothing else is written. If it exists with the same hash, it's a redelivery: carry on to step 6 anyway (roll-forward).
6. **Dispatch** by `type` to a handler. Every value a handler writes is computed from the stored W1 record (including its `received_at`), never from the current request. That's why a re-run writes the same values.
7. **Record the outcome (W5)** on the event record: `applied`, `ignored` or `exception`.

| Situation | Status | Body `outcome` | Stored? |
|---|---|---|---|
| Missing, malformed, or ambiguous signature header; non-integer `t` | 400 | `bad_signature_header` | no |
| HMAC mismatch | 401 | `invalid_signature` | no |
| `t` outside ±300 s | 401 | `stale_timestamp` | no |
| Body over cap | 413 | `too_large` | no |
| Verified, but not JSON or no `id` | 400 (+ alert) | `malformed` | no; we have no key |
| Applied (first time or roll-forward) | 200 | `applied` | yes |
| Already fully processed, same content | 200 | `duplicate` | yes |
| Unknown `type` | 200 | `ignored` | yes; raw body kept for later replay |
| Handled type failing a field rule or business rule | 200 (+ exception record) | `exception` | yes; parked for a human |
| Same `id`, different content | **409** (+ incident page) | `conflict` | the original is untouched; the attempt goes to the incident log |
| `StoreError` anywhere | 503 | `retry` | partial, completed on redelivery |
| Any other exception (a bug) | 500 | `error` | partial, completed on redelivery after a fix |

The rule behind the table: **a 2xx means "this event is stored and needs nothing more from PayCo".** That's why bad data on a handled type gets 200 and an exception record rather than 422. We keep the raw body, so replaying it after a fix has no 72-hour limit. Relying on PayCo's retries would give us only 72 hours.

`handle()` never raises; its outer `try` maps every error to a status code. `balance()` and `entries()` let `StoreError` propagate to the caller. **They never return 0 on error**, because a wrong balance is worse than an error.

### Handlers

**Validation shared by payment and refund.** `data.account_id` and `data.payment_id` are non-empty strings. `amount` is an `int`, not a `bool`, and greater than 0. `currency` matches `^[A-Z]{3}$`. `created` is an int. Any failure produces an exception record with reason `invalid_event`.

**`payment.succeeded`**, writes in this order:

1. **W2** Insert `pay/{payment_id}/payment = event_id` if absent. If another event already holds it, raise exception `duplicate_payment` and stop.
2. **W3** Insert `acct/{account_id}/currency = currency` if absent. If the stored currency differs, raise exception `currency_mismatch` and stop.
3. **W4** Put `entry/{account_id}/{event_id}` with amount `+amount`.
4. **Advisory check.** If `pay/{payment_id}/refund` exists, compare that refund's account and amount with this payment. On a mismatch, raise an alert record `pair_mismatch`, but don't block.

**`refund.succeeded`** follows the same steps, using `pay/{payment_id}/refund` (exception `duplicate_refund`) and amount `−amount`. A refund can be the first entry on an account; it then fixes the currency, which by A5 is the payment's currency. Mismatched pairs are applied anyway, because PayCo's events define what the statement will show. We only block in two cases: when we couldn't represent the result (mixed currencies), or when the result probably double-counts (a second refund for the same payment).

**Unknown types** write nothing beyond W1 and W5 (`ignored`). When a handler is added later, `ops replay --type X` re-runs those stored events. Redeliveries arriving after that deploy also apply, because duplicates go through the pipeline again.

**Negative balances are allowed and expected.** A refund after the credit was spent, or a refund that arrives before its payment, both go negative. Clamping at zero would break reconciliation.

### LedgerRepository

Methods: `claim_event`, `set_outcome`, `claim_natural_key`, `claim_currency`, `put_entry`, `list_entries(account_id)`, `put_exception_if_absent`, `put_incident_if_absent`, `get_event`, `scan_events(prefix)`.

How these map onto ledgerkit is decided by S1:
- If the store has **multi-key transactions**, run W1–W5 inside one transaction. Keep the idempotent design anyway, because a commit can still fail ambiguously.
- If it has **only conditional insert and scan**, use the design above as written.
- If it has **neither**, put a `threading.Lock` around `handle()`, keep one instance, and escalate (see the R1 row in §16).

**Scaling:** reads add up an account's entries, which is O(n) per read. That's fine up to thousands of entries per account. A cached balance is listed under deferred work.

## 8. Data Design

These are logical keys; S1 maps them onto ledgerkit's model. Every record carries `schema: 1`.

| Key | Value | Written by | Write mode | Purpose |
|---|---|---|---|---|
| `event/{event_id}` | `event_id, type, created, canonical_sha256, raw_sha256, raw_body, received_at, outcome?` | service | insert-if-absent; `outcome` set later | Inbox, duplicate check, incident detection, replay source |
| `pay/{payment_id}/payment` | `event_id` | handlers | insert-if-absent | One payment per payment ID |
| `pay/{payment_id}/refund` | `event_id` | handlers | insert-if-absent | One full refund per payment |
| `acct/{account_id}/currency` | ISO code | handlers | insert-if-absent | Currency fixed at the first money movement |
| `entry/{account_id}/{event_id}` | `event_id, type, amount (signed), currency, payment_id, created, received_at` | handlers | deterministic put | **System of record for balances** |
| `exception/{event_id}` | `reason, details, opened_at, status (open/resolved), resolved_by, note` | handlers, ops | insert-if-absent; status updated by ops | Human work queue |
| `incident/{event_id}/{canonical_sha256}` | `raw_body, received_at` | service | insert-if-absent | Security forensics |

- **Consistency:** each write is atomic on its own key. Correctness across keys comes from write order plus the roll-forward replay, not from transactions. A crash between writes leaves a state that the next run of the same event completes. The worst gap visible to readers is that the currency is set but the entry isn't there yet, which doesn't affect the balance.
- **Access patterns:** look up an event by ID (every delivery); scan entries by account (reads); scan events lacking an outcome (repair sweep, M3); scan exceptions with status open (ops). The prefix layout supports all four.
- **Reads:** `balance(a) = sum(amount over entry/{a}/*)`. `entries(a)` sorts by `(created, received_at, event_id)`, following the Q5 default.
- **Retention:** entries, events and exceptions are kept for at least the financial-records period *(assumed 10 years; Finance to confirm)*. Nothing is ever deleted or updated in place except `outcome` and exception status. Backups follow the managed DB's point-in-time recovery; one restore drill happens before cutover (M4).
- **Classification:** confidential financial data. `raw_body` of unknown types (for example disputes) may contain personal data (A7), so DB access is limited to the Ledger service account. Secrets and signatures are never logged.
- **Schema evolution:** additive fields only, readers ignore unknown fields, and `schema` is bumped for breaking changes, with a one-off rewrite through `ops`. The key layout is the hardest thing in this plan to reverse; fix it in M1.

## 9. Key Flows

**F1: Payment, happy path.** PayCo POSTs `evt_1` (payment, acct_42, 2500 EUR). The signature checks out. W1 claims `event/evt_1`, W2 claims `pay/pay_9xk/payment`, W3 sets acct_42 to EUR, W4 writes an entry of +2500, W5 sets `applied`. We return 200 `applied`, and `balance("acct_42")` is 2500.

**F2: Refund before its payment.** `evt_2` (refund pay_9xk, 2500) arrives first. W1, then W2' claims `pay/pay_9xk/refund`, W3 sets EUR, W4 writes −2500, and we return 200. The balance is −2500. Later `evt_1` arrives: W2 claims `pay/pay_9xk/payment`, W3 sees EUR already set, W4 writes +2500, the advisory pair check passes, and the balance is 0. That's the same final state as the normal order. `entries()` lists the payment first because its `created` is earlier.

**F3 (failure path): `StoreError` after a partial write, then redeliveries.**
1. `evt_1` arrives. W1 and W2 succeed. W3 raises `StoreError`; the write may or may not have committed. We return 503.
2. PayCo retries a few minutes later with a fresh `t` (A3). W1 finds `event/evt_1` with the same hash, so we roll forward. W2 finds its own `event_id`, so we continue. W3 inserts EUR, or finds EUR already there from step 1. W4 and W5 run, and we return 200 `applied`.
3. PayCo sends it once more after the 2xx. W1 is the same, the pipeline re-runs, every write is a no-op, `outcome` is already `applied`, and we return 200 `duplicate`. The balance stays 2500: one entry, because the entry key is the event ID.
4. If our endpoint were down for more than 72 hours after step 1, the M3 repair sweep re-runs events that have no `outcome`.

**F4: Same `id`, different body.** `evt_1` arrives again with a valid signature but `amount: 250000`. W1 finds a different canonical hash. We write an incident record (if that write fails, we only log), log at CRITICAL, page, and return 409. No handler runs, so `acct_42` is untouched. PayCo retries the 409; each retry produces the same result and keeps the alert visible. A valid signature on altered content means either our secret has leaked or PayCo has a bug, so the runbook starts with rotating the secret.

## 10. Key Decisions

| # | Decision | Options considered | Rationale (drivers) | Reversibility | Revisit if |
|---|---|---|---|---|---|
| D1 | 2xx only once the event is stored. Bad data on handled types is parked with 200 | (a) this; (b) 4xx for invalid and unknown types; (c) 200 for everything after the signature check | (b) creates 72-hour retry storms for every new event type and gives only a 72-hour fix window. (c) loses events on store failure. (a) serves #1 and #3 | Easy (status table) | PayCo penalises or disables endpoints that return many errors |
| D2 | Idempotent replay with deterministic keys, not reliance on transactions | (a) this; (b) one multi-key transaction with early return on duplicates | The store's transaction support is unknown (A1). Commits over a network can fail ambiguously (A2). (a) is correct under both outcomes | Hard (key layout) | S1 shows strict serialisable transactions with clear commit results; even then we keep (a) |
| D3 | Balance is the sum of entries | (a) sum when read; (b) a stored counter updated with each entry | (b) can drift on a partial write unless the counter and the entry are updated together, in one transaction | Medium | p99 read latency above 50 ms, or more than 10k entries on one account |
| D4 | Apply refunds as soon as they arrive | (a) apply now; (b) hold until the payment arrives | (a) is order-independent and matches the statement. (b) needs a pending-state machine, and a payment that never arrives leaves the refund stuck | Medium | Product can't tolerate transient negative balances |
| D5 | Detect duplicates by canonical-JSON hash; store the raw hash too | (a) canonical; (b) raw bytes | (b) raises false 409 incidents if PayCo re-serialises bodies. A difference only in formatting doesn't change what the event means, so it isn't a security concern | Easy | S2 shows byte-identical redeliveries, and Security prefers a strict comparison |
| D6 | No retries inside a request; return 503 and let PayCo retry | (a) none; (b) up to 2 retries with backoff inside the request | PayCo's retry gives the same effect for free; inside-request retries add latency against an unknown timeout | Easy | 503 rate above 0.5%, or PayCo backoff too slow for freshness |
| D7 | Process synchronously inside the request; the W1 record is the inbox | (a) synchronous; (b) store, return 200, process with a background worker | At about 0.05 events/s, (b) adds a component and its operations for no driver. The repair sweep covers stuck events | Medium | Sustained more than 50 events/s, or p99 latency targets missed |
| D8 | Enforce the ±300 s window as PayCo's guide says | (a) enforce; (b) skip the window for event IDs we haven't seen | With duplicate detection, a replayed genuine event is harmless, so (b) is safe. But it departs from the guide, so we keep (a) unless S2 shows retries reuse `t` | Easy | S2 shows retries reuse the original `t` |
| D9 | Ship as a library in the HTTP layer's process, one deployable | library vs a separate service | Nothing in the drivers needs separate scaling or deployment | Easy | Another team needs Ledger over the network |

## 11. Cross-Cutting Concerns

- **Security (M1):**
  - **Forgery:** HMAC over the exact bytes, compared with `compare_digest`.
  - **Replay:** the ±300 s window, plus duplicate detection by event ID, which makes replays within the window no-ops.
  - **Changed body:** 409, nothing written to the account, incident page.
  - **Secret leak:** the HTTP layer loads the secret from the secret manager; it is never logged. A rotation runbook covers this (rotating it means constructing `LedgerService` again; supporting two secrets at once is deferred). Reconciliation detects minted credit.
  - **Denial of service:** body size cap; JSON recursion errors mapped to 400.
  - **Information leaks:** response bodies carry only the `outcome` code.
  - **Access:** DB credentials scoped to Ledger.
- **Reliability (M1/M3):**
  - The idempotent pipeline and the status rule (M1).
  - Repair sweep for events without an outcome, run hourly (M3).
  - Store calls rely on ledgerkit's client timeouts (confirmed in S1); the total time inside `handle()` should stay under 5 s.
  - PayCo's backoff provides backpressure.
- **Observability:** one structured JSON log line per delivery (M1). Fields: `event_id, type, account_id, outcome, status, duration_ms, roll_forward(bool)`. Alerts are configured in the monitoring stack (M3), each linked to a runbook section:
  - any `conflict`: page;
  - `invalid_signature` or `stale_timestamp` above 5 in 15 minutes: secret or clock problem, ticket;
  - `retry` (503) rate above 1% over 30 minutes: ticket;
  - any open exception: Finance or support queue;
  - refunds with no payment after 72 hours;
  - a nonzero daily reconciliation difference.
- **Performance and capacity:** a handful of store calls per event, at about 0.05/s today. No load test is needed beyond a local benchmark of 1,000 events through `MemoryStore` (M2) to catch accidental O(n²) behaviour.
- **Cost:** marginal. It runs in the existing process. Storage is about 1–2 KB per event, so roughly 2 GB/year at 3k events/day.
- **Operations (M3):** owned by the backend team that owns the HTTP layer *(assumption)*. Business-hours response, which the 72-hour retry window supports; the only page is `conflict`. `docs/runbook.md` covers each alert, exception triage, secret rotation, replay, and the repair sweep.
- **AI-specific concerns:** not applicable; there are no model components.

## 12. Build Sequence

Team assumption: one Python engineer at full time, plus a few hours from the HTTP layer owner and from Finance. Sizes are rough ranges.

| Milestone | Goal / risk cleared | Scope (excluded) | Exit criteria | Depends on | Size |
|---|---|---|---|---|---|
| **M1 Thin slice + spikes** | H1, H4, A1–A4 | Spikes S1 and S2. Signature, parsing, W1 claim, duplicate and conflict handling, payment handler, unknown types → ignored, 503/500 mapping, reads, fault harness, staging wiring. (Excludes refunds, exceptions tooling, ops) | A sandbox payment reaches `balance()` in staging. A redelivery from the PayCo dashboard returns `duplicate`. The crash-point test passes for every write index in both fault modes. CI is green | S2 sandbox access (**long lead, request day 1**) | 5–8 days |
| **M2 Full rules** | H3, H2 | Refund handler, pairing checks, currency and natural-key exceptions, `invalid_event` parking, `entries()` ordering, permutation and invariant tests, local benchmark | Random permutation and duplication tests of 1–20-event histories (10k seeds) give a state independent of arrival order. All rows of the §7 status table have a test | M1, Q4 and Q5 answered | 3–5 days |
| **M3 Operate and reconcile** | H5, operability | `ledger.ops`: reconcile against the PayCo CSV by `created`, list and resolve exceptions, repair sweep, replay by type, opening-balance import (Q3). Alerts and runbook with the monitoring owner | Reconciliation of last month's PayCo statement, using a historical replay or the opening balance, reports 0 unexplained differences in staging. Each alert fires on an injected fault | M2, Q3 answered | 5–8 days |
| **M4 Shadow, then cutover** | Prove it on live traffic | Production endpoint registered. Ledger runs alongside the spreadsheet; daily three-way diff (Ledger / PayCo CSV / spreadsheet); restore drill; cutover | 14 straight days of 0 unexplained differences against the PayCo CSV. Product and support read from Ledger. First monthly statement matches | M3 | 3–5 weeks calendar, 2–4 days effort |

**Critical path:** S2 sandbox access → T10 staging slice → M4 shadow window (14 days). S1 → T7 → T8 → T9 runs in parallel; T4, T5 and T6 need nothing.

## 13. First Milestone Task Breakdown

Paths are proposed, since the repository is empty. Run tests with `python -m unittest discover -s tests`.

1. **S1 — Spike the ledgerkit store API (time box: half a day, day 1).** Question: does the store offer multi-key transactions, insert-if-absent, prefix or range scans, value types (bytes or str), client timeouts, and what does `StoreError` mean, i.e. can a write commit even though the client gets an error? Deliverable: `docs/adr/0001-store-primitives.md` mapping each repository method to ledgerkit calls. Done when it is reviewed. **Changes the plan if:** there are transactions (wrap W1–W5 in one, as in §7), or there is neither conditional insert nor transactions (in-process lock, single instance, escalate R1).
2. **S2 — Spike PayCo sandbox delivery (time box: 2 days of effort; request access on day 1).** Questions: is `t` fresh on each retry? Are redelivered bodies identical, byte for byte or in content? Can one header carry several `v1` values? What is the delivery timeout? Is there a test-delivery button? Deliverable: captured deliveries (headers and raw body) in `tests/fixtures/payco/` and notes in `docs/adr/0002-payco-delivery.md`. **Changes the plan if:** `t` is reused (revisit D8), or bodies differ in content (escalate to PayCo before M4).
3. **Create the package skeleton:** `ledger/__init__.py` (exporting `LedgerService`), `ledger/{service,signature,events,handlers,repository}.py`, `tests/`, and a CI job running the unittest command on Python 3.10+. Done when CI runs green on a trivial test. *Can run in parallel with 1 and 2.*
4. **Implement `ledger/signature.py`** `verify(headers, raw_body, secret, now)`. Tests in `tests/test_signature.py`:
   - valid signature;
   - wrong secret;
   - one changed body byte;
   - `|Δt|` = 300 accepted and 301 rejected, in both directions;
   - missing header;
   - header-name casing `payco-signature` and `PAYCO-SIGNATURE`;
   - conflicting duplicate headers;
   - several `v1` values;
   - extra whitespace;
   - non-integer `t`;
   - an S2 fixture.

   Done when all pass.
5. **Implement `ledger/events.py`:** strict parsing (duplicate keys, NaN), envelope check, payment and refund field validation (bool is rejected as an amount, so is a float, so is ≤ 0; currency must match the regex), canonical hash. Done when `tests/test_events.py` shows that reordered and reformatted JSON gives the same hash and that a changed amount gives a different one.
6. **Build `tests/fakes.py`:**
   - `FakeClock(now)`;
   - `FaultyStore(inner, fail_on_write=k, mode="before"|"after")`, which raises `StoreError` on the k-th write either before or after passing it to the inner store;
   - `sign(body, secret, t)`.

   Done when a self-test shows that both modes raise and that "after" mode leaves the write in place.
7. **Implement `ledger/repository.py`** using the key layout in §8 and the S1 mapping. It is the only module that imports ledgerkit. Done when `tests/test_repository.py` covers insert-if-absent returning the existing value, and entry scanning.
8. **Implement `ledger/service.py` and the payment and unknown-type parts of `ledger/handlers.py`:** pipeline steps 1–7, the §7 status table rows for M1, roll-forward on duplicates, a catch-all returning 500, `balance()` and `entries()`. Done when `tests/test_service.py` covers: applied, duplicate, conflict (account untouched and incident recorded), unknown type ignored, unseen account balance 0, and an unsigned request leaving the store unchanged.
9. **Write the crash-point test `tests/test_faults.py`.** For a payment-then-duplicate script, take each write index k (1 to N) and each mode. Deliver and expect 503. Redeliver until you get 200. Assert that the store's contents equal a fault-free run and that the balance is 2500 with exactly one entry. Done when it passes for all k. **This test is the main evidence for driver #1.**
10. **Wire up the staging endpoint with the HTTP layer owner.** Write `docs/http-integration.md`. The contract: pass raw body bytes unchanged, pass all headers, cap the body at 256 KiB, build `LedgerService` once with the secret from the secret manager, and return `(status, body)` as JSON. Register the staging URL in the PayCo sandbox. Done when a sandbox payment appears in staging `balance()` and a dashboard redelivery logs `outcome=duplicate`.
11. **Emit the per-delivery structured log line** (§11) using `logging` in `service.py`. Done when the test asserts the fields, including that neither the signature nor the secret ever appears in log output.

Order: 1, 2 and 3 start on day 1, in parallel. 4, 5 and 6 can run in parallel after 3. 7 needs 1. Then 8, then 9 and 11. 10 needs 2 and 8.

## 14. Testing and Validation Strategy

| Risk | Test | When |
|---|---|---|
| H4 signature edge cases | Unit tests and recorded PayCo fixtures | M1, in CI |
| H1 partial writes | Crash-point test at every write index, both fault modes (T9); extended in M2 to refund and exception paths | M1/M2, in CI |
| H3 ordering and duplicates | Seeded random permutations with duplicates and injected faults. Invariants: balance equals the sum of entries; each `event_id` appears at most once; final state is independent of order; a conflicting body never changes `entry/*` | M2, in CI |
| H2 status policy | One test per row of the §7 table | M1/M2, in CI |
| HTTP integration | Staging sandbox deliveries (T10); repeated after every HTTP layer change | M1, before release |
| Reconciliation | `ops reconcile` against last month's PayCo statement in staging | M3 |
| Live correctness | 14-day shadow diff against the PayCo CSV | M4 |

All unit, fault and invariant tests block merges. The staging sandbox check blocks releases. Driver #1's target is checked by the M2 invariants, then the M3 statement reconciliation, then M4 shadow mode.

## 15. Rollout, Migration, and Rollback

1. **Opening balances (M3).** Default per Q3: pick a cutoff time C. Import one `opening_balance` entry per account (synthetic ID `opening:{account}:{C}`), computed from PayCo's export, not the hand-edited spreadsheet. Events with `created < C` are stored with outcome `pre_cutoff` and not applied, so late retries don't double-count. If PayCo's export carries event IDs, import history as normal events instead and drop the cutoff rule. Note that `opening_balance` adds an entry type beyond payment and refund; tell consumers of `entries()`.
2. **Shadow (M4).** Register the production webhook. Ledger ingests live events while the spreadsheet stays authoritative. Run a daily diff of Ledger against the PayCo CSV and the spreadsheet; every difference is explained or fixed.
3. **Cutover.** After 14 clean days, product and support read from `balance()`. The nightly script keeps running for one more full monthly statement.
4. **Rollback.** Point reads back at the spreadsheet. Ledger keeps ingesting, so no data is lost. There is no point of no return until the nightly script and spreadsheet are retired, after the first matching monthly statement.

## 16. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation | Early warning | Owner |
|---|---|---|---|---|---|
| R1: ledgerkit lacks conditional insert and transactions | Med | High | S1 on day 1. Fallback: in-process lock and a single instance; escalate before scaling out | S1 result | Engineer |
| R2: PayCo retries reuse `t` | Low–Med | High | S2 tests it; D8 fallback | Many `stale_timestamp` results on retried IDs | Engineer |
| R3: HTTP layer changes body bytes | Med | High | Contract doc, staging test, alert on signature failures | 100% `invalid_signature` | HTTP owner |
| R4: Wrong opening balances at cutover | Med | High | Source from PayCo, not the spreadsheet; shadow diff | Shadow differences on old accounts | Finance + engineer |
| R5: Exceptions pile up untriaged | Med | Med | Daily exceptions report to Finance; alert on age over 2 days | Open-exception count rising | Finance |
| R6: Webhook secret leaks | Low | High | Secret manager, no logging, rotation runbook, reconciliation | `conflict` incidents; unexplained differences | Security |
| R7: PayCo enables partial refunds | Low | Med | `duplicate_refund` exceptions park them safely; add a handler | `duplicate_refund` exceptions appear | Product |
| R8: Spending turns out to belong in Ledger (Q4) | Med | Med | The `type` field and `source` mapping allow adding it; reconcile only PayCo types | Product asks for a debit API | Product |

## 17. Deferred Work and Future Evolution

- **Cached balance per account**: when D3's revisit condition is hit, and only with a transactional store.
- **Two secrets during rotation**: when the first rotation is planned. This needs a constructor change.
- **Handlers for disputes and payouts**: when Product asks. Add the handler, then run `ops replay --type`.
- **Manual adjustments**: append-only `adjustment` entries with an actor and a reason, through `ops`. Build when support needs a correction path (replaces the spreadsheet fixes).
- **Background processing (D7 option b)**: when volume exceeds 50 events/s.
- **Deliberate debt:** reads add up entries every time; the repair sweep scans all events lacking an outcome (fine below about 1M events).

## 18. Next Steps

1. **Today:** request PayCo sandbox access and a staging webhook secret (critical path for S2 and T10).
2. **Today:** get the real ledgerkit README or source from whoever owns it, and run S1 (task 1).
3. Create the `ledger/` and `tests/` skeleton and the CI job (task 3), then start `signature.py` (task 4).
4. Book 30 minutes with the HTTP layer owner on `docs/http-integration.md` (task 10).
5. Send Q3–Q5 to Finance and Product.

---

**What I did:** read the two reference files, then the workspace. The workspace is only a `README.md` saying it is empty, and there's no `ledgerkit` package in it. So the store API the brief refers to isn't available, and the plan treats it as the first unknown.

**Open questions that would most change this plan**

1. **What can the ledgerkit store do atomically?** (multi-key transactions, insert-if-absent, prefix scans, and whether a `StoreError` can follow a write that actually committed.) This decides how `ledger/repository.py` works and whether more than one instance can run safely. The default is §7's design based on insert-if-absent.
2. **Does PayCo re-sign each retry with a fresh `t`, and do redeliveries keep the same content?** If retries reuse `t`, the 300-second rule makes retries useless and D8 has to change.
3. **How should existing customers' balances be seeded, and does PayCo's export include event IDs?** This decides between a cutoff-plus-opening-balance approach and replaying full history.
4. **Should Ledger also record product spending,** or only PayCo money movements? Spending here would add a write API and change what Finance reconciles.
5. **Does "oldest first" mean event time or the order we applied entries, and which timestamp does PayCo's statement use?** This decides how `entries()` sorts and how as-of-date reconciliation works.
