# Ledger: customer balances from PayCo webhooks

## Context

We sell prepaid credit. Customers top up through our payment provider, PayCo, and spend the
credit in our product. Today a nightly script copies PayCo's CSV export into a spreadsheet and
support staff fix balances by hand. We want a small service, Ledger, that keeps every customer
account's balance current from PayCo's webhooks.

## What PayCo sends

PayCo sends an HTTP POST to our webhook URL for every event on our merchant account. The body is
JSON, for example:

```json
{"id": "evt_1Nq3xKp", "type": "payment.succeeded", "created": 1700000000,
 "data": {"account_id": "acct_42", "payment_id": "pay_9xk", "amount": 2500, "currency": "EUR"}}
```

- `id` identifies the event. A redelivery of the same event has the same `id` and the same body.
- `type` is one of:
  - `payment.succeeded`: the customer paid `amount`; credit it to `account_id`.
  - `refund.succeeded`: PayCo refunded the payment `payment_id` in full, and `amount` equals that
    payment's amount; take it back from `account_id`.
  - Other types: PayCo adds new event types from time to time (disputes, payouts, ...). We do not
    handle any of them yet; we will add them later.
- `created` is when the event happened, in Unix seconds.
- `amount` is an integer number of minor units (cents). `currency` is an ISO 4217 code.

## How PayCo delivers (from PayCo's integration guide)

- **Signatures.** Every delivery carries the header `PayCo-Signature: t=<unix seconds>,v1=<hex>`,
  where `<hex>` is the lowercase hex HMAC-SHA256 of the string `<t>.` followed by the exact request
  body bytes, keyed with our webhook secret. The guide says to reject a delivery whose signature
  does not verify, and one whose `t` is more than 300 seconds away from our clock, so that captured
  requests cannot be replayed.
- **Retries.** If our endpoint does not answer with a 2xx status, PayCo retries the delivery with
  backoff for up to 72 hours. It treats any 2xx as "delivered".
- **Ordering.** PayCo does not guarantee delivery order. A refund can arrive before the payment it
  refers to.
- **Duplicates.** PayCo may deliver the same event more than once, even after it received a 2xx.

## Our side

- The database is a managed network database. Writes occasionally fail partway through a request
  (our client raises `StoreError`). The failure rate is low but not zero.
- Finance reconciles every account against PayCo's monthly statement, and the two must match
  exactly, to the minor unit.
- Each customer account holds a single currency, fixed by its first payment.
- Security: if we ever receive the same event `id` with a different body, treat it as an
  incident: reject that delivery and leave the account untouched.
- Volume is small today (a few thousand events a day) and expected to grow.

## What we need

A Python service, standard library only, with this interface:

```
LedgerService(store, secret, clock)
    store:  our database client, a ledgerkit MemoryStore (see README.md)
    secret: the webhook signing secret, bytes
    clock:  an object whose now() returns the current Unix time in seconds

handle(raw_body: bytes, headers: dict) -> (status: int, body: dict)
    Processes one webhook delivery. Header names may arrive in any letter case. Our HTTP layer
    returns `status` to PayCo as the response code.

balance(account_id) -> int
    The account's current balance in minor units; 0 for an account we have never seen.

entries(account_id) -> list of dict
    The money movements applied to the account, oldest first. Each has at least `event_id`,
    `type` (the event's type, e.g. `payment.succeeded`), and `amount` (signed: positive for
    payments, negative for refunds).
```

The HTTP server, deployment, and monitoring are outside this codebase; a plan may still cover
them.
