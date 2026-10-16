"""The ledger on its own: derived keys, intent-before-send, bindings the schema refuses."""

from __future__ import annotations

import sqlite3

import pytest

from xero_idempotent_sync_proof.ledger import (
    IDEMPOTENCY_KEY_MAX_LEN,
    LEDGER_SCHEMA_VERSION,
    PENDING,
    PROJECT_MODE_PROJECTS_API,
    PROJECT_MODE_UNAVAILABLE,
    REJECTED,
    STEP_CONTACT,
    STEP_INVOICE,
    STEP_PAYMENT,
    SYNC_PENDING,
    Ledger,
    contact_name_for,
    contact_number_for,
    idempotency_key_for,
    invoice_number_for,
    payment_reference_for,
)


def test_the_idempotency_key_is_derived_not_generated():
    """Any process, with no memory of the attempt, computes the same key."""
    first = idempotency_key_for("deal-1001", STEP_CONTACT, "0")
    second = idempotency_key_for("deal-1001", STEP_CONTACT, "0")
    assert first == second
    assert first.startswith("xis-")
    assert len(first) == 44, "xis- plus 40 hex characters"
    assert len(first) <= IDEMPOTENCY_KEY_MAX_LEN


def test_keys_differ_by_deal_step_and_ref():
    keys = {
        idempotency_key_for("deal-1", STEP_CONTACT),
        idempotency_key_for("deal-2", STEP_CONTACT),
        idempotency_key_for("deal-1", STEP_INVOICE),
        idempotency_key_for("deal-1", STEP_CONTACT, "1"),
        idempotency_key_for("deal-1", STEP_PAYMENT, "pay-1"),
        idempotency_key_for("deal-1", STEP_PAYMENT, "pay-2"),
    }
    assert len(keys) == 6


def test_external_keys_are_deterministic_and_within_xero_limits():
    assert contact_number_for("deal-1001") == contact_number_for("deal-1001")
    assert len(contact_number_for("deal-" + "9" * 200)) <= 50, "ContactNumber max 50"
    assert invoice_number_for("deal-1001") == invoice_number_for("deal-1001")
    assert payment_reference_for("deal-1001", "pay-1") != payment_reference_for("deal-1001", "pay-2")
    assert contact_name_for(["Ana Perez", "Tomas Ruiz"], "2027-03-14") == "Ana Perez & Tomas Ruiz"
    assert (
        contact_name_for(["Ana Perez", "Tomas Ruiz"], "2027-03-14", variant=1)
        == "Ana Perez & Tomas Ruiz (2027-03-14)"
    )


def test_begin_step_commits_the_intent_before_anything_is_sent(ledger_factory, make_webhook):
    ledger = ledger_factory()
    deal_id = ledger.record_webhook(make_webhook())

    intent = ledger.begin_step(deal_id, STEP_CONTACT, "0")

    assert intent.state == PENDING
    assert intent.idempotency_key == idempotency_key_for(deal_id, STEP_CONTACT, "0")
    assert intent.remote_id is None

    # A fresh connection -- another process -- sees the row.
    again = ledger_factory().get_intent(deal_id, STEP_CONTACT, "0")
    assert again is not None and again.idempotency_key == intent.idempotency_key


def test_begin_step_twice_keeps_the_first_intent(ledger_factory, make_webhook):
    ledger = ledger_factory()
    deal_id = ledger.record_webhook(make_webhook())
    first = ledger.begin_step(deal_id, STEP_INVOICE)
    ledger.finish_step(deal_id, STEP_INVOICE, "", "inv-remote-1")

    second = ledger.begin_step(deal_id, STEP_INVOICE)

    assert second.idempotency_key == first.idempotency_key
    assert second.state == "done" and second.remote_id == "inv-remote-1"


def test_a_rejected_step_derives_a_fresh_key_from_its_attempt(ledger_factory, make_webhook):
    """Xero caches a 400 under its key for six minutes. The fix must not wait for it.

    Attempt 0 derives the key the rebuilt-ledger guarantee relies on. A
    send-first step (contact, invoice) that Xero provably rejected bumps a
    persisted attempt counter, and the next send goes out under
    ``{deal}:{step}:{ref}:attempt{n}`` -- a key Xero has never seen, so the
    corrected send is processed, not replayed. (The sync never does this for
    a payment or a project; see scenario 4.)
    """
    ledger = ledger_factory()
    deal_id = ledger.record_webhook(make_webhook())
    first = ledger.begin_step(deal_id, STEP_INVOICE)
    assert first.attempt == 0
    assert first.idempotency_key == idempotency_key_for(deal_id, STEP_INVOICE)
    assert idempotency_key_for(deal_id, STEP_INVOICE, attempt=0) == first.idempotency_key

    bumped = ledger.bump_attempt(deal_id, STEP_INVOICE)

    assert bumped.attempt == 1 and bumped.state == PENDING
    assert bumped.idempotency_key == idempotency_key_for(deal_id, STEP_INVOICE, attempt=1)
    assert bumped.idempotency_key != first.idempotency_key
    again = ledger_factory().get_intent(deal_id, STEP_INVOICE)
    assert again.attempt == 1 and again.idempotency_key == bumped.idempotency_key, "persisted"
    assert ledger.begin_step(deal_id, STEP_INVOICE).idempotency_key == bumped.idempotency_key, (
        "begin_step after a bump hands back the bumped key, never attempt 0 again"
    )


def test_reject_step_records_a_definite_no(ledger_factory, make_webhook):
    ledger = ledger_factory()
    deal_id = ledger.record_webhook(make_webhook())
    ledger.begin_step(deal_id, STEP_CONTACT, "0")
    ledger.reject_step(deal_id, STEP_CONTACT, "0")
    assert ledger.get_intent(deal_id, STEP_CONTACT, "0").state == REJECTED


def test_record_webhook_is_safe_for_duplicates_and_reordering(ledger_factory, make_webhook):
    ledger = ledger_factory()
    ledger.record_webhook(make_webhook(payment_id="pay-2", amount=1000.0, paid_at="2026-10-01"))
    ledger.record_webhook(make_webhook(payment_id="pay-1", amount=2500.0, paid_at="2026-09-01"))
    ledger.record_webhook(make_webhook(payment_id="pay-1", amount=2500.0, paid_at="2026-09-01"))

    payments = ledger.payments_for("deal-1001")
    assert [p.hubspot_payment_id for p in payments] == ["pay-1", "pay-2"]
    deal = ledger.get_deal("deal-1001")
    assert deal.sync_status == SYNC_PENDING
    assert deal.amount_owed == 12500.0 and deal.amount_paid == 0.0


def test_the_schema_refuses_a_second_binding(ledger_factory, make_webhook):
    """One Xero entity binds to at most one local thing, whatever the code does."""
    ledger = ledger_factory()
    ledger.record_webhook(make_webhook(deal_id="deal-1", payment_id="pay-1"))
    ledger.record_webhook(make_webhook(deal_id="deal-2", payment_id="pay-2"))

    ledger.bind_contact("deal-1", "c-1", "Ana Perez & Tomas Ruiz", "hs-deal-1")
    ledger.bind_project("deal-1", "p-1", PROJECT_MODE_PROJECTS_API)
    ledger.bind_invoice("deal-1", "i-1", "INV-deal-1")
    ledger.bind_payment("deal-1", "pay-1", "pm-1")

    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_contact("deal-2", "c-1", "Someone Else", "hs-deal-2")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_project("deal-2", "p-1", PROJECT_MODE_PROJECTS_API)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_invoice("deal-2", "i-1", "INV-deal-2")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_payment("deal-2", "pay-2", "pm-1")


def test_project_mode_and_project_id_agree_or_the_schema_refuses(ledger_factory, make_webhook):
    """A project id exists exactly when the mode says a real Project was made.

    ``unavailable`` with an id, ``projects_api`` without one, or a mode outside
    the two: the database refuses each, so the business can never read
    "project" where there is none, or "none" where there is one.
    """
    ledger = ledger_factory()
    ledger.record_webhook(make_webhook())

    ledger.bind_project("deal-1001", None, PROJECT_MODE_UNAVAILABLE)
    assert ledger.get_deal("deal-1001").project_mode == PROJECT_MODE_UNAVAILABLE
    ledger.bind_project("deal-1001", "p-1", PROJECT_MODE_PROJECTS_API)
    assert ledger.get_deal("deal-1001").xero_project_id == "p-1"

    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_project("deal-1001", "p-2", PROJECT_MODE_UNAVAILABLE)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_project("deal-1001", None, PROJECT_MODE_PROJECTS_API)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_project("deal-1001", "p-2", "")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_project("deal-1001", "p-2", None)
    assert ledger.get_deal("deal-1001").xero_project_id == "p-1", "every refusal left the row as it was"


def test_a_database_from_before_the_attempt_column_is_upgraded_on_open(db_path, ledger_factory):
    """``CREATE TABLE IF NOT EXISTS`` does not add a column to a table that already exists.

    A ledger written by the first version has an ``intents`` table without
    ``attempt`` and no ``schema_version`` table. Opening it must add the
    column (with its default), stamp the version, and leave the rows usable
    -- not fail on the first ``SELECT *``.
    """
    v1 = sqlite3.connect(db_path)
    v1.executescript(
        """
        CREATE TABLE deals (
            deal_id TEXT PRIMARY KEY, couple_names TEXT NOT NULL, wedding_date TEXT NOT NULL,
            contract_total REAL NOT NULL, xero_contact_id TEXT, xero_contact_name TEXT,
            xero_contact_number TEXT, xero_project_id TEXT, project_mode TEXT, xero_invoice_id TEXT,
            xero_invoice_number TEXT, amount_paid REAL NOT NULL DEFAULT 0,
            amount_owed REAL NOT NULL DEFAULT 0, last_payment_at TEXT, sync_status TEXT NOT NULL,
            last_error TEXT
        );
        CREATE TABLE payments (
            deal_id TEXT NOT NULL, hubspot_payment_id TEXT NOT NULL, amount REAL NOT NULL,
            paid_at TEXT NOT NULL, xero_payment_id TEXT, PRIMARY KEY (deal_id, hubspot_payment_id)
        );
        CREATE TABLE intents (
            deal_id TEXT NOT NULL, step TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL, mode TEXT, state TEXT NOT NULL, remote_id TEXT,
            PRIMARY KEY (deal_id, step, ref)
        );
        INSERT INTO deals (deal_id, couple_names, wedding_date, contract_total, sync_status)
            VALUES ('deal-old', '["A", "B"]', '2027-01-01', 100.0, 'pending');
        INSERT INTO intents (deal_id, step, ref, idempotency_key, state)
            VALUES ('deal-old', 'invoice', '', 'xis-old', 'pending');
        """
    )
    v1.commit()
    v1.close()

    ledger = ledger_factory()

    intent = ledger.get_intent("deal-old", STEP_INVOICE)
    assert intent is not None and intent.attempt == 0 and intent.idempotency_key == "xis-old"
    assert ledger.bump_attempt("deal-old", STEP_INVOICE).attempt == 1
    columns = {r[1] for r in ledger._conn.execute("PRAGMA table_info(intents)")}
    assert "attempt" in columns
    assert ledger._conn.execute(
        "SELECT version FROM schema_version WHERE component = 'ledger'"
    ).fetchone()[0] == LEDGER_SCHEMA_VERSION
    assert ledger_factory().get_intent("deal-old", STEP_INVOICE).attempt == 1, "a second open changes nothing"


def test_balance_is_stated_not_added(ledger_factory, make_webhook):
    ledger = ledger_factory()
    ledger.record_webhook(make_webhook())
    ledger.set_balance("deal-1001", 2500.0, 10000.0, "2026-09-01")
    ledger.set_balance("deal-1001", 2500.0, 10000.0, "2026-09-01")
    deal = ledger.get_deal("deal-1001")
    assert (deal.amount_paid, deal.amount_owed) == (2500.0, 10000.0)
