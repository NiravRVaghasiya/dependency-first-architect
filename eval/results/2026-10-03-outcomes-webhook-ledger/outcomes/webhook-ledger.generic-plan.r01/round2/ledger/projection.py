"""Pure projection from an account's stored events to its entries and balance.

The result depends only on which events are present (and the account currency), never on the
order they arrived in, so duplicates, redelivery and reordering cannot change a balance.
"""


REFUND_EXCEEDS = "refund_exceeds_payment"


def project(records, currency):
    """records: list of dicts with event_id, type, payment_id, amount (unsigned), currency,
    created. currency: the account's currency (fixed by its first payment), or None.

    Returns (entries, status) where entries is the list of applied movements, oldest first, each
    {"event_id","type","amount" (signed),"payment_id","currency","created"}, and status maps
    event_id -> "applied" | "pending" | "held" | "merged"."""
    entries, status, _ = project_detailed(records, currency)
    return entries, status


def project_detailed(records, currency):
    """Like project(), but returns (entries, status, reasons); reasons maps the event_id of each
    held event to why it is held (e.g. "refund_exceeds_payment")."""
    ordered = sorted(records, key=lambda r: (r["created"], r["event_id"]))
    status = {}
    reasons = {}
    credited = {}  # payment_id -> record
    applied = []

    for r in ordered:
        if r["type"] != "payment.succeeded":
            continue
        if currency is not None and r["currency"] != currency:
            status[r["event_id"]] = "held"  # currency_mismatch
            continue
        prior = credited.get(r["payment_id"])
        if prior is None:
            credited[r["payment_id"]] = r
            status[r["event_id"]] = "applied"
            applied.append((r["created"], 0, r["event_id"], r, r["amount"]))
        elif prior["amount"] == r["amount"] and prior["currency"] == r["currency"]:
            status[r["event_id"]] = "merged"  # same payment seen under another event id
        else:
            status[r["event_id"]] = "held"  # payment_id_conflict

    # Partial refunds: a payment may have several refunds; their total may never exceed the
    # payment's amount. Refunds are considered in (created, event_id) order and one that would
    # push the running total past the payment amount is held (and not counted), so the outcome
    # is independent of arrival order. Each refund has its own event id, so there is no merging.
    refunded = {}  # payment_id -> total refunded so far
    for r in ordered:
        if r["type"] != "refund.succeeded":
            continue
        pay = credited.get(r["payment_id"])
        if pay is None:
            status[r["event_id"]] = "pending"
        elif pay["currency"] != r["currency"]:
            status[r["event_id"]] = "held"
            reasons[r["event_id"]] = "refund_currency_mismatch"
        elif refunded.get(r["payment_id"], 0) + r["amount"] > pay["amount"]:
            status[r["event_id"]] = "held"
            reasons[r["event_id"]] = REFUND_EXCEEDS
        else:
            refunded[r["payment_id"]] = refunded.get(r["payment_id"], 0) + r["amount"]
            status[r["event_id"]] = "applied"
            applied.append((max(r["created"], pay["created"]), 1, r["event_id"], r, -r["amount"]))

    applied.sort(key=lambda a: a[:3])
    entries = [{
        "event_id": r["event_id"],
        "type": r["type"],
        "amount": signed,
        "payment_id": r["payment_id"],
        "currency": r["currency"],
        "created": when,
    } for when, _, _, r, signed in applied]
    return entries, status, reasons
