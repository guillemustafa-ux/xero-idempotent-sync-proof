"""The operational ledger, and the durable intent that has to exist before any send.

One deal becomes four kinds of write against Xero: a Contact, a Project (or
its honest absence), an Invoice and one Payment per CRM payment. Each of
those has the same gap -- Xero commits, the process dies before the id is
written down -- so each one gets the same treatment:

* the intent to send a step is committed to SQLite **before** the first byte
  leaves, so a process killed mid-call wakes up knowing a call was in flight;
* the ``Idempotency-Key`` is **derived** from the deal, the step and the
  payment, not generated at call time -- a key that changes on retry is not
  an idempotency key at all;
* the **external keys Xero lets us plant** -- ``ContactNumber``,
  ``InvoiceNumber`` and the payment ``Reference`` -- are derived the same
  way. This matters more here than with QuickBooks Online: Xero remembers an
  ``Idempotency-Key`` for **six minutes**, so a key alone settles only the
  transient retry. Whatever can be looked up by a planted value is what makes
  recovery possible with nothing but the database;
* ``synchronous=FULL`` so "committed" means committed, not "in the OS page
  cache when the power went out";
* unique indexes on every remote binding, so a second contact, project,
  invoice or payment for the same local thing is rejected by the database
  even if the code is wrong.

The ``deals`` table is the operational record the business reads (it stands
in for a spreadsheet or a no-code base): what was created, what is owed, and
-- explicitly -- whether the "project" is a real Xero Project or was
unavailable to this organisation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

# Steps, in the order they are applied.
STEP_CONTACT = "contact"
STEP_PROJECT = "project"
STEP_INVOICE = "invoice"
STEP_PAYMENT = "payment"

# Intent lifecycle.
PENDING = "pending"  # sent, or about to be; fate unknown
DONE = "done"  # remote id known and persisted
REJECTED = "rejected"  # the server answered with a definite no; nothing exists

# Deal sync status.
SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_ERROR = "error"

# How the "project" was materialised. Never left blank once a project id is
# bound: the business must be able to tell a real Project from a stand-in.
PROJECT_MODE_PROJECTS_API = "projects_api"
PROJECT_MODE_UNAVAILABLE = "unavailable"
PROJECT_MODES = (PROJECT_MODE_PROJECTS_API, PROJECT_MODE_UNAVAILABLE)

# Xero caps Idempotency-Key at 128 characters; "xis-" + 40 hex = 44.
IDEMPOTENCY_KEY_MAX_LEN = 128
# Xero caps ContactNumber at 50 characters.
CONTACT_NUMBER_MAX_LEN = 50


def idempotency_key_for(deal_id: str, step: str, ref: str = "") -> str:
    """Derive the ``Idempotency-Key`` -- deterministically, forever.

    ``"xis-" + sha256(f"{deal_id}:{step}[:{ref}]")[:40]`` where ``ref`` is
    the name variant for a contact or the CRM payment id for a payment. Any
    process, at any later time, with no memory of the attempt, computes the
    same one. ``str(uuid4())`` at call time is not an idempotency key: every
    retry carries a fresh id and Xero has nothing to recognise.
    """
    material = f"{deal_id}:{step}" + (f":{ref}" if ref else "")
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"xis-{digest[:40]}"


def contact_number_for(deal_id: str) -> str:
    """The ``ContactNumber`` planted on the contact we create.

    It is the external-key slot Xero offers (API-only, unique, max 50,
    readable with ``GET /Contacts/{ContactNumber}``), so a later lookup can
    tell "ours" from "a stranger with the same name" without guessing. Long
    deal ids are hashed to stay within the limit.
    """
    number = f"hs-deal-{deal_id}"
    if len(number) > CONTACT_NUMBER_MAX_LEN:
        number = "hs-deal-" + hashlib.sha256(deal_id.encode()).hexdigest()[:40]
    return number


def invoice_number_for(deal_id: str) -> str:
    """The ACCREC ``InvoiceNumber``: unique in Xero, so it doubles as the lookup key."""
    return f"HS-{deal_id}"


def payment_reference_for(deal_id: str, hubspot_payment_id: str) -> str:
    """The ``Reference`` planted on a payment.

    Xero does **not** enforce uniqueness on it, which is exactly why the
    sync must count matches on the invoice's ``Payments[]`` rather than
    assume one.
    """
    return f"hubspot_payment_id={hubspot_payment_id} deal={deal_id}"


def contact_name_for(couple_names: list[str], wedding_date: str, variant: int = 0) -> str:
    """The consistent contact ``Name`` for a couple, plus a deterministic tiebreak.

    Xero requires ``Name`` to be unique across active contacts. Two different
    couples can share both names, so the second variant carries the wedding
    date. The suffix is derived, not counted, so every process picks the
    same one.
    """
    base = " & ".join(couple_names)
    return base if variant == 0 else f"{base} ({wedding_date})"


@dataclass
class Deal:
    deal_id: str
    couple_names: list[str]
    wedding_date: str
    contract_total: float
    xero_contact_id: str | None
    xero_contact_name: str | None
    xero_contact_number: str | None
    xero_project_id: str | None
    project_mode: str | None
    xero_invoice_id: str | None
    xero_invoice_number: str | None
    amount_paid: float
    amount_owed: float
    last_payment_at: str | None
    sync_status: str
    last_error: str | None


@dataclass
class Payment:
    deal_id: str
    hubspot_payment_id: str
    amount: float
    paid_at: str
    xero_payment_id: str | None


@dataclass
class Intent:
    deal_id: str
    step: str
    ref: str
    idempotency_key: str
    mode: str | None
    state: str
    remote_id: str | None


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Durability over throughput: a crash must not lose the last commit.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS deals (
                deal_id              TEXT PRIMARY KEY,
                couple_names         TEXT NOT NULL,
                wedding_date         TEXT NOT NULL,
                contract_total       REAL NOT NULL,
                xero_contact_id      TEXT,
                xero_contact_name    TEXT,
                xero_contact_number  TEXT,
                xero_project_id      TEXT,
                project_mode         TEXT CHECK (project_mode IS NULL OR project_mode IN
                                     ('{PROJECT_MODE_PROJECTS_API}', '{PROJECT_MODE_UNAVAILABLE}')),
                xero_invoice_id      TEXT,
                xero_invoice_number  TEXT,
                amount_paid          REAL NOT NULL DEFAULT 0,
                amount_owed          REAL NOT NULL DEFAULT 0,
                last_payment_at      TEXT,
                sync_status          TEXT NOT NULL,
                last_error           TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                deal_id            TEXT NOT NULL,
                hubspot_payment_id TEXT NOT NULL,
                amount             REAL NOT NULL,
                paid_at            TEXT NOT NULL,
                xero_payment_id    TEXT,
                PRIMARY KEY (deal_id, hubspot_payment_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intents (
                deal_id         TEXT NOT NULL,
                step            TEXT NOT NULL,
                ref             TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL,
                mode            TEXT,
                state           TEXT NOT NULL,
                remote_id       TEXT,
                PRIMARY KEY (deal_id, step, ref)
            )
            """
        )
        # One Xero entity binds to at most one local thing. The database
        # refuses a second binding even if the code is wrong.
        for name, column in (
            ("deals_contact_uq", "xero_contact_id"),
            ("deals_project_uq", "xero_project_id"),
            ("deals_invoice_uq", "xero_invoice_id"),
        ):
            self._conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                f"ON deals({column}) WHERE {column} IS NOT NULL"
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS payments_xero_uq "
            "ON payments(xero_payment_id) WHERE xero_payment_id IS NOT NULL"
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- webhook intake ---------------------------------------------------

    def record_webhook(self, webhook: dict[str, Any]) -> str:
        """Record a "deal paid" webhook. Safe for duplicates and reordering.

        The deal row and the payment row are both ``INSERT OR IGNORE``: a
        second delivery of the same payment changes nothing, and a payment
        arriving before an earlier one simply lands next to it.
        """
        deal_id = str(webhook["deal_id"])
        self._conn.execute(
            "INSERT OR IGNORE INTO deals (deal_id, couple_names, wedding_date, "
            "contract_total, amount_owed, sync_status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                deal_id,
                json.dumps(list(webhook["couple_names"])),
                webhook["wedding_date"],
                float(webhook["contract_total"]),
                float(webhook["contract_total"]),
                SYNC_PENDING,
            ),
        )
        payment = webhook.get("payment")
        if payment:
            self._conn.execute(
                "INSERT OR IGNORE INTO payments (deal_id, hubspot_payment_id, "
                "amount, paid_at) VALUES (?, ?, ?, ?)",
                (
                    deal_id,
                    str(payment["hubspot_payment_id"]),
                    float(payment["amount"]),
                    payment.get("paid_at", ""),
                ),
            )
            # A new payment means the deal has work to do again.
            self._conn.execute(
                "UPDATE deals SET sync_status = ? WHERE deal_id = ? AND sync_status = ?",
                (SYNC_PENDING, deal_id, SYNC_SYNCED),
            )
        return deal_id

    # -- intents ----------------------------------------------------------

    def begin_step(
        self, deal_id: str, step: str, ref: str = "", mode: str | None = None
    ) -> Intent:
        """Commit the intent to send one step, and return the key for it.

        This is the load-bearing line of the whole repo. It runs *before* the
        network call, so a hard kill during the call leaves a durable row
        saying "this step was in flight for this deal, with this key".
        """
        key = idempotency_key_for(deal_id, step, ref)
        assert len(key) <= IDEMPOTENCY_KEY_MAX_LEN
        self._conn.execute(
            "INSERT OR IGNORE INTO intents (deal_id, step, ref, idempotency_key, mode, state) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (deal_id, step, ref, key, mode, PENDING),
        )
        intent = self.get_intent(deal_id, step, ref)
        assert intent is not None
        return intent

    def set_step_mode(self, deal_id: str, step: str, ref: str, mode: str) -> Intent:
        """Record a change of strategy for a pending step, before acting on it."""
        self._conn.execute(
            "INSERT INTO intents (deal_id, step, ref, idempotency_key, mode, state) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id, step, ref) DO UPDATE SET mode = excluded.mode",
            (deal_id, step, ref, idempotency_key_for(deal_id, step, ref), mode, PENDING),
        )
        intent = self.get_intent(deal_id, step, ref)
        assert intent is not None
        return intent

    def finish_step(self, deal_id: str, step: str, ref: str, remote_id: str) -> None:
        self._conn.execute(
            "INSERT INTO intents (deal_id, step, ref, idempotency_key, state, remote_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id, step, ref) DO UPDATE SET state = excluded.state, "
            "remote_id = excluded.remote_id",
            (deal_id, step, ref, idempotency_key_for(deal_id, step, ref), DONE, remote_id),
        )

    def reject_step(self, deal_id: str, step: str, ref: str) -> None:
        """The server said no, definitively. Nothing exists under this intent."""
        self._conn.execute(
            "UPDATE intents SET state = ? WHERE deal_id = ? AND step = ? AND ref = ?",
            (REJECTED, deal_id, step, ref),
        )

    def get_intent(self, deal_id: str, step: str, ref: str = "") -> Intent | None:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE deal_id = ? AND step = ? AND ref = ?",
            (deal_id, step, ref),
        ).fetchone()
        return Intent(**dict(row)) if row else None

    # -- bindings ---------------------------------------------------------

    def bind_contact(
        self, deal_id: str, contact_id: str, name: str, contact_number: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE deals SET xero_contact_id = ?, xero_contact_name = ?, "
            "xero_contact_number = ? WHERE deal_id = ?",
            (contact_id, name, contact_number, deal_id),
        )

    def bind_project(self, deal_id: str, project_id: str | None, mode: str) -> None:
        """``project_id`` is ``None`` exactly when ``mode`` says the API was unavailable."""
        self._conn.execute(
            "UPDATE deals SET xero_project_id = ?, project_mode = ? WHERE deal_id = ?",
            (project_id, mode, deal_id),
        )

    def bind_invoice(self, deal_id: str, invoice_id: str, invoice_number: str) -> None:
        self._conn.execute(
            "UPDATE deals SET xero_invoice_id = ?, xero_invoice_number = ? WHERE deal_id = ?",
            (invoice_id, invoice_number, deal_id),
        )

    def bind_payment(self, deal_id: str, hubspot_payment_id: str, xero_payment_id: str) -> None:
        self._conn.execute(
            "UPDATE payments SET xero_payment_id = ? "
            "WHERE deal_id = ? AND hubspot_payment_id = ?",
            (xero_payment_id, deal_id, hubspot_payment_id),
        )

    def set_balance(
        self, deal_id: str, amount_paid: float, amount_owed: float, last_payment_at: str | None
    ) -> None:
        """Absolute, not incremental.

        A counter you bump by the webhook's amount is itself a non-idempotent
        write: replay the webhook and the total drifts. The numbers here come
        from reading the invoice back (``AmountPaid`` and ``AmountDue``), and
        are stated, not added.
        """
        self._conn.execute(
            "UPDATE deals SET amount_paid = ?, amount_owed = ?, last_payment_at = ? "
            "WHERE deal_id = ?",
            (amount_paid, amount_owed, last_payment_at, deal_id),
        )

    def set_status(self, deal_id: str, status: str, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE deals SET sync_status = ?, last_error = ? WHERE deal_id = ?",
            (status, last_error, deal_id),
        )

    # -- reads ------------------------------------------------------------

    def get_deal(self, deal_id: str) -> Deal:
        row = self._conn.execute(
            "SELECT * FROM deals WHERE deal_id = ?", (deal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(deal_id)
        data = dict(row)
        data["couple_names"] = json.loads(data["couple_names"])
        return Deal(**data)

    def payments_for(self, deal_id: str) -> list[Payment]:
        rows = self._conn.execute(
            "SELECT * FROM payments WHERE deal_id = ? ORDER BY paid_at, hubspot_payment_id",
            (deal_id,),
        ).fetchall()
        return [Payment(**dict(r)) for r in rows]

    def unresolved(self) -> Iterator[Deal]:
        """Deals whose remote state is in doubt, for startup recovery."""
        rows = self._conn.execute(
            "SELECT deal_id FROM deals WHERE sync_status != ?", (SYNC_SYNCED,)
        ).fetchall()
        return (self.get_deal(r["deal_id"]) for r in rows)
