"""Proof that a CRM -> Xero sync can be made exactly-once across crashes."""

from .ledger import (
    Ledger,
    contact_name_for,
    contact_number_for,
    idempotency_key_for,
    invoice_number_for,
    payment_reference_for,
)

__all__ = [
    "Ledger",
    "contact_name_for",
    "contact_number_for",
    "idempotency_key_for",
    "invoice_number_for",
    "payment_reference_for",
]
