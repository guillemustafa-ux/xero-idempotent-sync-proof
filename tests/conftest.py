from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from xero_idempotent_sync_proof.ledger import Ledger


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "sync.db")


@pytest.fixture
def ledger_factory(db_path):
    """Opens the ledger fresh each time.

    Every recovery in this suite is driven from a **new** ``Ledger`` object.
    In-memory state is never allowed to help across a crash boundary -- that
    is the whole point.
    """
    opened: list[Ledger] = []

    def make(path: str | None = None) -> Ledger:
        ledger = Ledger(path or db_path)
        opened.append(ledger)
        return ledger

    yield make
    for ledger in opened:
        ledger.close()


@pytest.fixture
def make_webhook():
    """A CRM "deal paid" webhook, as the sync receives it."""

    def make(
        deal_id: str = "deal-1001",
        couple_names: tuple[str, str] = ("Ana Perez", "Tomas Ruiz"),
        wedding_date: str = "2027-03-14",
        contract_total: float = 12500.0,
        payment_id: str = "pay-1",
        amount: float = 2500.0,
        paid_at: str = "2026-09-01T10:00:00Z",
    ) -> dict[str, Any]:
        return {
            "deal_id": deal_id,
            "couple_names": list(couple_names),
            "wedding_date": wedding_date,
            "contract_total": contract_total,
            "payment": {
                "hubspot_payment_id": payment_id,
                "amount": amount,
                "paid_at": paid_at,
            },
        }

    return make
