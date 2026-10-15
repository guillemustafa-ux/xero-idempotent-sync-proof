from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from xero_idempotent_sync_proof.auth import Authenticator, TokenStore
from xero_idempotent_sync_proof.client import XeroClient
from xero_idempotent_sync_proof.fake_xero import CLIENT_ID, CLIENT_SECRET, FakeXero
from xero_idempotent_sync_proof.ledger import Ledger


@pytest.fixture
def xero_factory():
    """A Xero stand-in, with or without the Projects API available to the org."""
    running: list[FakeXero] = []

    def make(*, projects_available: bool = False) -> FakeXero:
        xero = FakeXero(projects_available=projects_available)
        xero.start()
        running.append(xero)
        return xero

    yield make
    for xero in running:
        xero.stop()


@pytest.fixture
def xero(xero_factory) -> FakeXero:
    """The default: an organisation whose plan does not include Projects."""
    return xero_factory()


@pytest.fixture
def xero_with_projects(xero_factory) -> FakeXero:
    return xero_factory(projects_available=True)


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
def auth_factory(db_path):
    """An authenticator over a token store in the same database file.

    The first one for a database seeds it with the refresh token the
    authorisation flow would have produced -- exactly once, so later
    "processes" against the same file find whatever the previous one left.
    The authenticator reads the fake's clock, so advancing the fake advances
    the client's idea of expiry as well.
    """
    opened: list[TokenStore] = []

    def make(xero: FakeXero, path: str | None = None) -> Authenticator:
        store = TokenStore(path or db_path)
        opened.append(store)
        if store.load() is None:
            store.bootstrap(xero.refresh_token)
        return Authenticator(
            store,
            token_url=xero.token_url,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            now=xero.now,
        )

    yield make
    for store in opened:
        store.close()


@pytest.fixture
def client_factory(auth_factory):
    def make(
        xero: FakeXero,
        path: str | None = None,
        *,
        sleep: Callable[[float], None] = lambda _seconds: None,
    ) -> XeroClient:
        # No sleeping in the proof unless a test wants to record the sleeps.
        return XeroClient(
            xero.base_url,
            xero.tenant_id,
            auth_factory(xero, path),
            timeout=5.0,
            sleep=sleep,
        )

    return make


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
