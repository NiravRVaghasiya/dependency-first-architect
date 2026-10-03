"""Pure projection from an account's stored events to its entries and balance.

The result depends only on which events are present (and the account currency), never on the
order they arrived in, so duplicates, redelivery and reordering cannot change a balance.
"""


def project(records, currency):
    """records: list of dicts with event_id, type, payment_id, amount (unsigned), currency,
    created. currency: the account's currency (fixed by its first payment), or None.

    Returns (entries, status) where entries is the list of applied movements, oldest first, each
    {"event_id","type","amount" (signed),"payment_id","currency","created"}, and status maps
    event_id -> "applied" | "pending" | "held" | "merged"."""
    ordered = sorted(records, key=lambda r: (r["created"], r["event_id"]))
    status = {}
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

    refunded = set()
    for r in ordered:
        if r["type"] != "refund.succeeded":
            continue
        pay = credited.get(r["payment_id"])
        if pay is None:
            status[r["event_id"]] = "pending"
        elif pay["amount"] != r["amount"] or pay["currency"] != r["currency"]:
            status[r["event_id"]] = "held"  # refund_mismatch
        elif r["payment_id"] in refunded:
            status[r["event_id"]] = "merged"
        else:
            refunded.add(r["payment_id"])
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
    return entries, status
