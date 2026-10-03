# Change request: partial refunds

PayCo now supports partial refunds, and we start issuing them next month.

- A `refund.succeeded` event may now refund part of a payment: `data.amount` is the amount of
  this refund, which can be less than the payment's amount. One payment can have several refunds.
- The total refunded for a payment must never exceed that payment's amount. If a refund would
  push the total past it, reject that delivery with HTTP status 422 and leave the account
  untouched; finance will investigate.
- Everything else in BRIEF.md still applies.
