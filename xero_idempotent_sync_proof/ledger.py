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

# The steps whose key may change after a provable rejection: only those Xero
# refuses to duplicate on a planted unique field, where a second replica on
# another attempt is refused on the number and adopts. A look-first step
# (payment, project) is pinned to attempt 0 for good -- its one derived key
# is what settles two replicas racing -- and the ledger enforces that on
# read and on open, whatever an earlier version left on disk.
BUMP_ATTEMPT_ON_REJECTION = frozenset({STEP_CONTACT, STEP_INVOICE})

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
# Xero remembers an Idempotency-Key for six minutes from the first call. The
# one place this number lives: the transport refuses to wait past it, the
# fake forgets keys at it, the tests step the clock over it.
IDEMPOTENCY_TTL_SECONDS = 6 * 60
# Xero caps ContactNumber at 50 characters.
CONTACT_NUMBER_MAX_LEN = 50

# Schema versions, one per component sharing the SQLite file. Version 1 is
# the first release; version 2 added ``intents.attempt`` (ledger) and
# ``oauth_tokens.scope`` (auth); ledger version 3 pins every look-first
# intent back to attempt 0 (an earlier version bumped payments too). ``CREATE
# TABLE IF NOT EXISTS`` does nothing to a table that already exists, so a
# pre-existing database is upgraded on open instead.
LEDGER_SCHEMA_VERSION = 3
AUTH_SCHEMA_VERSION = 2


def schema_version(conn: sqlite3.Connection, component: str) -> int:
    """The recorded schema version of ``component`` in this file; 0 when none was ever recorded."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version WHERE component = ?", (component,)).fetchone()
    return int(row[0]) if row else 0


def set_schema_version(conn: sqlite3.Connection, component: str, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (component, version) VALUES (?, ?) "
        "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
        (component, version),
    )


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    """``ALTER TABLE ... ADD COLUMN`` when the column is not there yet; a no-op otherwise.

    Idempotent, like everything else here: two processes opening the same
    old file race to the same end state, and the second finds the column.
    """
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in present:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def idempotency_key_for(deal_id: str, step: str, ref: str = "", attempt: int = 0) -> str:
    """Derive the ``Idempotency-Key`` -- deterministically, forever.

    ``"xis-" + sha256(f"{deal_id}:{step}[:{ref}][:attempt{n}]")[:40]`` where
    ``ref`` is the name variant for a contact or the CRM payment id for a
    payment. Any process, at any later time, with no memory of the attempt,
    computes the same one. ``str(uuid4())`` at call time is not an
    idempotency key: every retry carries a fresh id and Xero has nothing to
    recognise.

    ``attempt`` is 0 for the first send, and the key is then exactly what a
    rebuilt ledger re-derives. It is bumped only when Xero **provably
    rejected** the send with a validation ``400`` -- and only for the steps
    Xero refuses to duplicate on a planted unique field (contact, invoice):
    Xero caches that 400 under the key for six minutes, so the corrected send
    needs a key Xero has never seen, and a second replica on another attempt
    is refused on the number and adopts. A look-first step (payment, project)
    keeps attempt 0 forever: its one derived key is what settles two replicas
    racing, and a per-ledger counter in the key would break exactly that.
    """
    material = f"{deal_id}:{step}" + (f":{ref}" if ref else "")
    if attempt:
        material += f":attempt{attempt}"
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


DEALS_COLUMNS = (
    "deal_id",
    "couple_names",
    "wedding_date",
    "contract_total",
    "xero_contact_id",
    "xero_contact_name",
    "xero_contact_number",
    "xero_project_id",
    "project_mode",
    "xero_invoice_id",
    "xero_invoice_number",
    "amount_paid",
    "amount_owed",
    "last_payment_at",
    "sync_status",
    "last_error",
)


def _deals_table_sql(name: str) -> str:
    """The ``deals`` DDL, under the given table name so an upgrade can rebuild it alongside the old one."""
    return f"""
        CREATE TABLE IF NOT EXISTS {name} (
            deal_id              TEXT PRIMARY KEY,
            couple_names         TEXT NOT NULL,
            wedding_date         TEXT NOT NULL,
            contract_total       REAL NOT NULL,
            xero_contact_id      TEXT,
            xero_contact_name    TEXT,
            xero_contact_number  TEXT,
            xero_project_id      TEXT,
            project_mode         TEXT,
            xero_invoice_id      TEXT,
            xero_invoice_number  TEXT,
            amount_paid          REAL NOT NULL DEFAULT 0,
            amount_owed          REAL NOT NULL DEFAULT 0,
            last_payment_at      TEXT,
            sync_status          TEXT NOT NULL,
            last_error           TEXT,
            -- A project id exists exactly when the mode says a real
            -- Project was made. Enforced here, not only in the code. A
            -- CASE, not a chain of ORs: a NULL mode compared with '='
            -- yields NULL, and SQLite lets a NULL CHECK pass.
            CHECK (
                CASE
                    WHEN project_mode IS NULL THEN xero_project_id IS NULL
                    WHEN project_mode = '{PROJECT_MODE_PROJECTS_API}' THEN xero_project_id IS NOT NULL
                    WHEN project_mode = '{PROJECT_MODE_UNAVAILABLE}' THEN xero_project_id IS NULL
                    ELSE 0
                END
            )
        )
        """


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
    attempt: int = 0


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Durability over throughput: a crash must not lose the last commit.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(_deals_table_sql("deals"))
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
                attempt         INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (deal_id, step, ref)
            )
            """
        )
        self._create_binding_indexes()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS payments_xero_uq "
            "ON payments(xero_payment_id) WHERE xero_payment_id IS NOT NULL"
        )
        self._upgrade()

    def _create_binding_indexes(self) -> None:
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

    def _upgrade(self) -> None:
        """Bring a database written by an earlier version up to this schema.

        A fresh file gets the full ``CREATE TABLE`` above and only needs the
        version stamped. A file from version 1 has an ``intents`` table with
        no ``attempt`` column, and ``CREATE TABLE IF NOT EXISTS`` left it
        that way; the column is added here, with the default every old row
        should carry. A file from before version 3 may hold a payment or
        project intent that an earlier version bumped; its stored key is the
        one a replica on a fresh ledger would never derive, so the row is
        pinned back to attempt 0 and the derived key before anything is sent.
        And a ``deals`` table from the first release lacks the CHECK tying
        ``xero_project_id`` to ``project_mode``; SQLite cannot add a
        constraint with ``ALTER TABLE``, so the table is rebuilt.
        """
        version = schema_version(self._conn, "ledger")
        if version < 2:
            add_column_if_missing(self._conn, "intents", "attempt", "INTEGER NOT NULL DEFAULT 0")
        if version < 3:
            self._pin_look_first_intents_to_attempt_zero()
            self._rebuild_deals_if_the_project_check_is_missing()
        set_schema_version(self._conn, "ledger", LEDGER_SCHEMA_VERSION)

    def _rebuild_deals_if_the_project_check_is_missing(self) -> None:
        """Create the current table beside the old one, copy, drop, rename -- in one transaction.

        The stored DDL says whether the CASE constraint is there. Dropping the
        old table drops its indexes with it, so they are recreated on the new
        one. A row that would violate the constraint aborts the whole thing
        and leaves the file as it was: that is a human's question, not one
        to settle by dropping the row.
        """
        stored = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'deals'"
        ).fetchone()
        if stored is None or "CASE" in (stored["sql"] or ""):
            return
        columns = ", ".join(DEALS_COLUMNS)
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(_deals_table_sql("deals_v3"))
            self._conn.execute(f"INSERT INTO deals_v3 ({columns}) SELECT {columns} FROM deals")
            self._conn.execute("DROP TABLE deals")
            self._conn.execute("ALTER TABLE deals_v3 RENAME TO deals")
            self._create_binding_indexes()
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _pin_look_first_intents_to_attempt_zero(self) -> None:
        bumpable = sorted(BUMP_ATTEMPT_ON_REJECTION)
        rows = self._conn.execute(
            f"SELECT deal_id, step, ref FROM intents WHERE attempt > 0 "
            f"AND step NOT IN ({', '.join('?' * len(bumpable))})",
            bumpable,
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "UPDATE intents SET attempt = 0, idempotency_key = ? "
                "WHERE deal_id = ? AND step = ? AND ref = ?",
                (idempotency_key_for(row["deal_id"], row["step"], row["ref"]), *row),
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

        It runs *before* the network call, so a hard kill during the call
        leaves a durable row saying "this step was in flight for this deal,
        with this key". What that row buys, honestly:

        * the next process **looks up before it resends** -- by the planted
          ``ContactNumber`` or ``InvoiceNumber`` -- instead of resending and
          leaning on Xero to refuse the duplicate. With the planted keys in
          place, a sync without this row still ends with one entity: the
          resend is refused, the refusal triggers the lookup. The row saves
          the blind resend on the wire and removes the dependence on Xero's
          refusal; it is not, on its own, what prevents the duplicate;
        * the **variant** a contact settled on (``REJECTED`` for a name a
          stranger holds) and the **attempt** a rejected step is at, so a
          later process derives the right name and the right key without
          re-provoking the 400s that taught the first process;
        * the **mode** the project step chose.

        Where it would be load-bearing on its own is a step with no plantable
        key and a dead key window -- which this design has none of, by
        construction. The second replica racing the first is settled by the
        planted unique number (contact, invoice) or by the one *derived* key
        (payment, project), not by this row (see scenario 4).
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

    def bump_attempt(self, deal_id: str, step: str, ref: str = "") -> Intent:
        """Xero provably rejected this send: derive a fresh key for the next one.

        A validation ``400`` is cached under its key for six minutes, so a
        corrected resend under the same key gets the same 400 back. The
        attempt counter is persisted, so every process *on this ledger*
        derives the same new key; the step stays ``PENDING`` and is looked up
        before the resend like any other in-flight step. Nothing was created
        under the old key. The sync calls this only for the contact and
        invoice steps, where a second replica on another attempt is refused
        on the planted number; never for a payment or a project.
        """
        current = self.get_intent(deal_id, step, ref)
        assert current is not None, "bump_attempt on a step that was never begun"
        attempt = current.attempt + 1
        self._conn.execute(
            "UPDATE intents SET attempt = ?, idempotency_key = ? "
            "WHERE deal_id = ? AND step = ? AND ref = ?",
            (attempt, idempotency_key_for(deal_id, step, ref, attempt), deal_id, step, ref),
        )
        intent = self.get_intent(deal_id, step, ref)
        assert intent is not None
        return intent

    def get_intent(self, deal_id: str, step: str, ref: str = "") -> Intent | None:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE deal_id = ? AND step = ? AND ref = ?",
            (deal_id, step, ref),
        ).fetchone()
        if row is None:
            return None
        intent = Intent(**dict(row))
        if intent.step not in BUMP_ATTEMPT_ON_REJECTION and intent.attempt:
            # A look-first step is on attempt 0 whatever the row says: the
            # derived key is the one every other ledger derives too.
            intent.attempt = 0
            intent.idempotency_key = idempotency_key_for(intent.deal_id, intent.step, intent.ref)
        return intent

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
        """``project_id`` is ``None`` exactly when ``mode`` says the API was unavailable.

        The ``deals`` table's CHECK constraint refuses any other combination.
        """
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
