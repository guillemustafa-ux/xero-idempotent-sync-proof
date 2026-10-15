"""Proof that a CRM -> Xero sync can be made exactly-once across crashes."""

from .auth import AuthLockedOut, Authenticator, TokenStore
from .client import (
    ProjectsApiUnavailable,
    UnknownOutcome,
    XeroClient,
    XeroNotFound,
    XeroRejected,
)
from .fake_xero import FakeXero
from .ledger import (
    Ledger,
    contact_name_for,
    contact_number_for,
    idempotency_key_for,
    invoice_number_for,
    payment_reference_for,
)

__all__ = [
    "AuthLockedOut",
    "Authenticator",
    "TokenStore",
    "ProjectsApiUnavailable",
    "UnknownOutcome",
    "XeroClient",
    "XeroNotFound",
    "XeroRejected",
    "FakeXero",
    "Ledger",
    "contact_name_for",
    "contact_number_for",
    "idempotency_key_for",
    "invoice_number_for",
    "payment_reference_for",
]
