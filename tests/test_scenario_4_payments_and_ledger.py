"""Scenario 4 -- payments are where duplicates cost money, and the ledger is what the business reads.

Webhooks arrive twice, arrive late, and arrive out of order. Every payment
must land on the same contact and invoice exactly once, and the operational
record must state the balance rather than count it.

Xero gives a payment no unique field. The ``Reference`` we plant is the only
handle, and Xero does not enforce it, so the sync has to *count* the
payments on the invoice carrying it -- and stop when the count is wrong.
"""

from __future__ import annotations

import sqlite3

import pytest

from xero_idempotent_sync_proof.client import UnknownOutcome, XeroRejected
from xero_idempotent_sync_proof.fake_xero import IDEMPOTENCY_TTL_SECONDS, ROUTE_INVOICE, ROUTE_PAYMENT
from xero_idempotent_sync_proof.ledger import (
    PENDING,
    STEP_INVOICE,
    STEP_PAYMENT,
    SYNC_ERROR,
    SYNC_SYNCED,
    idempotency_key_for,
    invoice_number_for,
    payment_reference_for,
)
from xero_idempotent_sync_proof.sync import UnresolvableStep, naive_payment_sync, xero_date_to_iso


def payment_puts(xero) -> list[str]:
    """Every create that reached the payment route (replays excluded)."""
    return [r for r in xero.request_log if r.startswith("PUT /api.xro/2.0/Payments key=")]


def test_duplicate_webhook_delivery_records_one_payment(xero, engine_factory, make_webhook):
    """The CRM delivers the same webhook twice. Xero must see one payment."""
    webhook = make_webhook(amount=2500.0)
    engine = engine_factory(xero)

    first = engine.apply(webhook)
    second = engine_factory(xero).apply(webhook)  # a different worker, same delivery

    assert len(xero.payments) == 1
    assert first.amount_paid == second.amount_paid == 2500.0
    assert second.amount_owed == 10000.0
    assert xero.invoices[second.xero_invoice_id]["AmountDue"] == 10000.0


def test_two_workers_past_the_lookup_at_the_same_moment_book_one_payment(
    xero, engine_factory, make_webhook, tmp_path
):
    """What the six-minute key is actually for.

    Two replicas with their own databases -- a blue/green deploy, a rebuilt
    ledger next to the old one -- receive the same delivery. Both look the
    payment up, both see none, both send. The lookup cannot help here; only
    a key both of them *derive* identically makes the second send a replay.
    """
    webhook = make_webhook(amount=2500.0)
    replica = engine_factory(xero, path=str(tmp_path / "replica.db"))

    def the_other_replica_wins_the_race(seam: str) -> None:
        if seam == "before_payment_send":
            replica.apply(webhook)

    primary = engine_factory(xero, on_seam=the_other_replica_wins_the_race)
    deal = primary.apply(webhook)

    assert len(xero.payments) == 1, "two sends, one payment"
    assert deal.amount_paid == 2500.0 and deal.amount_owed == 10000.0
    ours = {p.xero_payment_id for p in primary.ledger.payments_for(deal.deal_id)}
    theirs = {p.xero_payment_id for p in replica.ledger.payments_for(deal.deal_id)}
    assert ours == theirs == set(xero.payments), "both ledgers bound the same PaymentID"
    key = idempotency_key_for("deal-1001", STEP_PAYMENT, "pay-1")
    assert payment_puts(xero) == [f"PUT /api.xro/2.0/Payments key={key}"]
    assert f"REPLAY PUT /api.xro/2.0/Payments key={key}" in xero.request_log


def test_a_second_payment_hits_the_same_contact_and_invoice(xero, engine_factory, make_webhook):
    """Later payments on the same deal reuse everything and add one Payment."""
    engine = engine_factory(xero)
    first = engine.apply(
        make_webhook(payment_id="pay-1", amount=2500.0, paid_at="2026-09-01T10:00:00Z")
    )
    second = engine.apply(
        make_webhook(payment_id="pay-2", amount=4000.0, paid_at="2026-10-01T10:00:00Z")
    )

    assert (second.xero_contact_id, second.project_mode, second.xero_invoice_id) == (
        first.xero_contact_id,
        first.project_mode,
        first.xero_invoice_id,
    )
    assert len(xero.contacts) == 1 and len(xero.invoices) == 1, "nothing else was created"
    assert len(xero.payments) == 2
    assert second.amount_paid == 6500.0
    assert second.amount_owed == 6000.0, "read back from the invoice, not added up locally"
    assert second.last_payment_at == "2026-10-01T10:00:00Z"
    assert second.sync_status == SYNC_SYNCED
    references = sorted(p["Reference"] for p in xero.payments.values())
    assert references == [
        payment_reference_for("deal-1001", "pay-1"),
        payment_reference_for("deal-1001", "pay-2"),
    ], "each payment carries the reference it can be found by"


def test_out_of_order_delivery_ends_in_the_same_state(xero, engine_factory, make_webhook):
    """The second payment's webhook arrives first, then the first, then both again."""
    pay1 = make_webhook(payment_id="pay-1", amount=2500.0, paid_at="2026-09-01T10:00:00Z")
    pay2 = make_webhook(payment_id="pay-2", amount=4000.0, paid_at="2026-10-01T10:00:00Z")

    engine = engine_factory(xero)
    for webhook in (pay2, pay1, pay2, pay1):
        deal = engine.apply(webhook)

    assert len(xero.payments) == 2
    assert deal.amount_paid == 6500.0 and deal.amount_owed == 6000.0
    assert deal.last_payment_at == "2026-10-01T10:00:00Z"
    ids = {p.xero_payment_id for p in engine.ledger.payments_for(deal.deal_id)}
    assert ids == set(xero.payments), "each CRM payment maps to exactly one Xero payment"


def test_naive_payment_retry_after_a_real_kill_pays_the_invoice_twice(
    xero, engine_factory, ledger_factory, client_factory, run_child, make_webhook
):
    """The anti-pattern where it costs money.

    Contact and invoice exist. The payment create commits, the response is
    lost, the worker dies. The retry trusts the derived key -- which would
    have worked for six minutes -- and has no record of the attempt and
    nothing to look up. Seven minutes later there is no name to collide on,
    so the invoice is simply paid twice.
    """
    webhook = make_webhook(amount=2500.0)
    engine = engine_factory(xero)
    deal_id = engine.ledger.record_webhook(webhook)
    engine._ensure_contact(engine.ledger.get_deal(deal_id))
    engine._ensure_project(engine.ledger.get_deal(deal_id))
    engine._ensure_invoice(engine.ledger.get_deal(deal_id))

    xero.drop_response_after_commit(ROUTE_PAYMENT)
    run_child(xero, deal_id, crash_at="-", strategy="naive-payment", payment_id="pay-1")
    assert len(xero.payments) == 1

    xero.advance(IDEMPOTENCY_TTL_SECONDS + 1)  # the redelivery, after the window
    naive_payment_sync(ledger_factory(), client_factory(xero), deal_id, "pay-1")

    invoice = xero.invoices[engine.ledger.get_deal(deal_id).xero_invoice_id]
    assert len(xero.payments) == 2, "one CRM payment, two Xero payments"
    assert invoice["AmountPaid"] == 5000.0, "the books now say the couple paid twice"
    assert invoice["AmountDue"] == 12500.0 - 5000.0
    assert len(payment_puts(xero)) == 2, "same key both times; the second was processed as new"


def invoice_puts(xero) -> list[str]:
    return [r for r in xero.request_log if r.startswith("PUT /api.xro/2.0/Invoices key=")]


def test_an_invoice_survives_a_real_kill_between_200_and_persist(
    xero, ledger_factory, engine_factory, run_child, make_webhook
):
    """Killed after Xero created the invoice, before the id was written.

    The intent row says the invoice step was in flight, so recovery asks
    ``GET /Invoices/{InvoiceNumber}`` first and adopts. On the wire: one
    create, ever -- recovery did not resend and lean on "Invoice # must be
    unique." to be told.
    """
    deal_id = ledger_factory().record_webhook(make_webhook(amount=2500.0))

    run_child(xero, deal_id, crash_at="after_invoice_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_INVOICE)
    assert intent is not None and intent.state == PENDING
    assert killed.get_deal(deal_id).xero_invoice_id is None
    assert len(xero.invoices) == 1 and xero.payments == {}

    deal = engine_factory(xero).recover_all().settled[0]

    assert len(xero.invoices) == 1, "adopted, not recreated"
    assert deal.xero_invoice_id == next(iter(xero.invoices))
    assert deal.xero_invoice_number == invoice_number_for(deal_id)
    assert len(xero.payments) == 1 and deal.amount_paid == 2500.0
    assert deal.sync_status == SYNC_SYNCED
    assert len(invoice_puts(xero)) == 1, "looked up by the planted number; never resent"
    assert f"GET /api.xro/2.0/Invoices/{invoice_number_for(deal_id)} key=-" in xero.request_log


def test_a_lost_response_on_the_invoice_create_is_settled_by_its_number(
    xero, engine_factory, make_webhook
):
    """Same hazard, no crash: the process is alive and still cannot tell."""
    webhook = make_webhook(amount=2500.0)
    xero.drop_response_after_commit(ROUTE_INVOICE)
    with pytest.raises(UnknownOutcome):
        engine_factory(xero).apply(webhook)
    assert len(xero.invoices) == 1

    deal = engine_factory(xero).apply(webhook)

    assert len(xero.invoices) == 1 and deal.xero_invoice_id == next(iter(xero.invoices))
    assert deal.sync_status == SYNC_SYNCED and len(xero.payments) == 1
    assert len(invoice_puts(xero)) == 1, "one create on the wire, ever"


def test_an_invoice_with_our_number_on_a_strangers_contact_is_escalated(
    xero, engine_factory, ledger_factory, client_factory, make_webhook
):
    """``InvoiceNumber`` is unique, so the refusal says *an* invoice exists -- not whose.

    Our derived number sits on an invoice that belongs to another contact
    (a hand-keyed invoice, a CRM id reused). Paying our instalment against
    it would book one couple's money on another's account. The sync names
    both contacts and stops; nothing is created and nothing is paid.
    """
    client = client_factory(xero)
    stranger = client.create_contact({"Name": "Someone Else"}, idempotency_key="s").entity
    theirs = client.create_invoice(
        {
            "Type": "ACCREC",
            "Contact": {"ContactID": stranger["ContactID"]},
            "InvoiceNumber": invoice_number_for("deal-1001"),
            "Status": "AUTHORISED",
            "LineItems": [{"Description": "x", "Quantity": 1, "UnitAmount": 50.0, "AccountCode": "200"}],
        },
        idempotency_key="s-inv",
    ).entity

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero).apply(make_webhook(amount=2500.0))

    assert exc.value.step == STEP_INVOICE
    assert theirs["InvoiceID"] in exc.value.detail and stranger["ContactID"] in exc.value.detail
    deal = ledger_factory().get_deal("deal-1001")
    assert deal.xero_contact_id is not None and deal.xero_contact_id in exc.value.detail
    assert deal.xero_invoice_id is None and deal.sync_status == SYNC_ERROR
    assert len(xero.invoices) == 1 and xero.payments == {}, "nothing created, nothing paid"
    assert xero.invoices[theirs["InvoiceID"]]["AmountDue"] == 50.0


def test_a_payment_survives_a_real_kill_between_200_and_persist(
    xero, ledger_factory, engine_factory, run_child, make_webhook
):
    """The same kill, against the design under test. One payment, one id.

    Recovery reads the invoice's ``Payments[]``, finds exactly one with the
    reference it planted, and adopts it. No second send, no replay needed.
    """
    deal_id = ledger_factory().record_webhook(make_webhook(amount=2500.0))

    run_child(xero, deal_id, crash_at="after_payment_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_PAYMENT, "pay-1")
    assert intent is not None and intent.state == PENDING
    assert killed.payments_for(deal_id)[0].xero_payment_id is None
    assert len(xero.payments) == 1

    deal = engine_factory(xero).recover_all().settled[0]

    assert len(xero.payments) == 1, "recovery must not book the payment twice"
    assert deal.amount_paid == 2500.0 and deal.amount_owed == 10000.0
    assert deal.sync_status == SYNC_SYNCED
    assert killed.payments_for(deal_id)[0].xero_payment_id == next(iter(xero.payments))
    assert len(payment_puts(xero)) == 1, "settled by the planted Reference, not by resending"


def test_two_payments_with_our_reference_escalate_instead_of_guessing(
    xero, ledger_factory, engine_factory, client_factory, run_child, make_webhook
):
    """The honest failure, Xero edition.

    ``Reference`` is not unique. A second payment carrying ours exists on the
    invoice -- keyed in by hand, or by an earlier bug. Picking either one is
    a guess with money attached. The sync stops, records both ids, and books
    nothing more.
    """
    deal_id = ledger_factory().record_webhook(make_webhook(amount=2500.0))
    run_child(xero, deal_id, crash_at="after_payment_response")

    deal = ledger_factory().get_deal(deal_id)
    planted = client_factory(xero).create_payment(
        {
            "Invoice": {"InvoiceID": deal.xero_invoice_id},
            "Account": {"Code": "090"},
            "Date": "2026-09-01",
            "Amount": 2500.0,
            "Reference": payment_reference_for(deal_id, "pay-1"),
        },
        idempotency_key="planted-by-hand",
    ).entity
    assert len(xero.payments) == 2

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero).sync(deal_id)

    assert exc.value.step == STEP_PAYMENT
    assert "2 payments" in exc.value.detail
    for payment_id in xero.payments:
        assert payment_id in exc.value.detail, "both ids, for a human"
    assert planted["PaymentID"] in exc.value.detail
    after = ledger_factory()
    assert after.get_deal(deal_id).sync_status == SYNC_ERROR
    assert after.payments_for(deal_id)[0].xero_payment_id is None, "nothing was adopted"
    assert len(xero.payments) == 2, "still two -- we refused to make it three"
    assert len(payment_puts(xero)) == 2, "one from the killed child, one planted; none from recovery"

    report = engine_factory(xero).recover_all()
    assert report.settled == [] and [d.deal_id for d in report.failed] == [deal_id]
    assert planted["PaymentID"] in report.failed[0].last_error
    assert len(xero.payments) == 2 and len(payment_puts(xero)) == 2, "recovery reported; it did not resend"


def test_a_cached_rejection_pins_the_payment_for_at_most_six_minutes(
    xero, engine_factory, make_webhook
):
    """Xero caches errors under the key too -- the second trap of the idempotency store.

    The bank account the payment books to is not in the chart yet, so the
    payment is a real 400. Resend the same key after the accountant adds the
    account and Xero replays the cached 400 for six minutes. The sync does
    **not** mint a fresh key to get around that: a payment is a look-first
    step, and the one derived key is what makes two replicas' sends collapse
    into one. So the deal records the rejection, the step stays pinned on the
    cached 400 for at most the window, and the send after the window goes
    through -- under the same key, exactly once.
    """
    webhook = make_webhook(amount=2500.0)
    xero.remove_bank_account("090")
    key0 = idempotency_key_for("deal-1001", STEP_PAYMENT, "pay-1")

    engine = engine_factory(xero)
    with pytest.raises(XeroRejected) as exc:
        engine.apply(webhook)
    assert exc.value.says("The specified account could not be found")
    deal = engine.ledger.get_deal("deal-1001")
    assert deal.sync_status == SYNC_ERROR and "The specified account could not be found" in deal.last_error
    intent = engine.ledger.get_intent("deal-1001", STEP_PAYMENT, "pay-1")
    assert intent.attempt == 0 and intent.idempotency_key == key0, "a look-first step keeps its key"

    # The accountant adds the account a moment later. The cached 400 is
    # replayed: the payment is pinned, honestly recorded, nothing created.
    xero.add_bank_account("090")
    with pytest.raises(XeroRejected):
        engine_factory(xero).apply(webhook)
    assert len(xero.payments) == 0
    assert any(r.startswith("REPLAY PUT /api.xro/2.0/Payments") for r in xero.request_log), "the cached 400"
    pinned = engine_factory(xero).ledger.get_deal("deal-1001")
    assert pinned.sync_status == SYNC_ERROR and "The specified account could not be found" in pinned.last_error

    # The window closes. Same key, processed as new, one payment.
    xero.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    deal = engine_factory(xero).apply(webhook)

    assert deal.sync_status == SYNC_SYNCED and len(xero.payments) == 1
    assert deal.amount_paid == 2500.0 and deal.last_error is None
    assert {r.split("key=")[1] for r in payment_puts(xero)} == {key0}, "one key, ever, for this instalment"


def test_two_replicas_with_unequal_rejection_counts_still_book_one_payment(
    xero, engine_factory, make_webhook, tmp_path
):
    """Why the payment's key is never bumped: separate ledgers count separately.

    Two replicas with their own databases both hit the cached 400 (the bank
    account is missing) -- the primary twice, the replica once. If a
    rejection bumped a per-ledger attempt counter into the key, the two
    would now derive *different* keys for the same instalment; past the
    window both look first, both see nothing, both send, and neither send
    is a replay of the other: **two payments for one instalment**. With one
    derived key per instalment, the race ends as scenario 4b does -- in a
    replay.
    """
    webhook = make_webhook(amount=2500.0)
    xero.remove_bank_account("090")
    primary_path = str(tmp_path / "primary.db")
    replica_path = str(tmp_path / "replica.db")
    key0 = idempotency_key_for("deal-1001", STEP_PAYMENT, "pay-1")

    for _ in range(2):  # the primary retries while the account is still missing
        with pytest.raises(XeroRejected):
            engine_factory(xero, path=primary_path).apply(webhook)
    with pytest.raises(XeroRejected):  # the replica tries once
        engine_factory(xero, path=replica_path).apply(webhook)
    assert xero.payments == {}

    xero.add_bank_account("090")
    xero.advance(IDEMPOTENCY_TTL_SECONDS + 1)  # the cached 400s expire; the account was fixed "later"
    replica = engine_factory(xero, path=replica_path)

    def the_other_replica_wins_the_race(seam: str) -> None:
        if seam == "before_payment_send":
            replica.apply(webhook)

    deal = engine_factory(xero, path=primary_path, on_seam=the_other_replica_wins_the_race).apply(webhook)

    keys = [r.split("key=")[1] for r in payment_puts(xero)]
    assert len(xero.payments) == 1, f"{len(xero.payments)} payments booked for one instalment; PUT keys {keys}"
    assert set(keys) == {key0}, "every send of this instalment, from either replica, carried the same key"
    assert deal.amount_paid == 2500.0 and deal.sync_status == SYNC_SYNCED
    ours = {p.xero_payment_id for p in engine_factory(xero, path=primary_path).ledger.payments_for("deal-1001")}
    theirs = {p.xero_payment_id for p in replica.ledger.payments_for("deal-1001")}
    assert ours == theirs == set(xero.payments), "both ledgers bound the same PaymentID"


def test_a_strangers_payment_carrying_our_reference_is_not_adopted(
    xero, engine_factory, ledger_factory, client_factory, make_webhook
):
    """``Reference`` is free text. One match is not proof; it must also be our money.

    Someone keyed a 1.00 test payment onto the invoice and pasted our
    reference into it. Counting gives exactly one, and adopting it would mark
    a 2,500 instalment as booked. The sync compares ``Amount`` and ``Date``
    with the CRM instalment and escalates on a mismatch; nothing is created.
    """
    webhook = make_webhook(amount=2500.0, paid_at="2026-09-01T10:00:00Z")
    engine = engine_factory(xero)
    deal_id = engine.ledger.record_webhook(webhook)
    engine._ensure_contact(engine.ledger.get_deal(deal_id))
    engine._ensure_project(engine.ledger.get_deal(deal_id))
    engine._ensure_invoice(engine.ledger.get_deal(deal_id))
    deal = engine.ledger.get_deal(deal_id)
    stranger = client_factory(xero).create_payment(
        {
            "Invoice": {"InvoiceID": deal.xero_invoice_id},
            "Account": {"Code": "090"},
            "Date": "2026-09-01",
            "Amount": 1.0,
            "Reference": payment_reference_for(deal_id, "pay-1"),
        },
        idempotency_key="keyed-in-by-hand",
    ).entity

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero).apply(webhook)

    assert exc.value.step == STEP_PAYMENT
    assert stranger["PaymentID"] in exc.value.detail and "1.0" in exc.value.detail and "2500.0" in exc.value.detail
    after = ledger_factory()
    assert after.get_deal(deal_id).sync_status == SYNC_ERROR
    assert after.payments_for(deal_id)[0].xero_payment_id is None, "not adopted"
    assert len(xero.payments) == 1 and len(payment_puts(xero)) == 1, "and not re-sent either"

    # The same reference with the right amount on the wrong day is a mismatch too.
    xero.payments.clear()
    xero.invoices[deal.xero_invoice_id]["Payments"].clear()
    client_factory(xero).create_payment(
        {
            "Invoice": {"InvoiceID": deal.xero_invoice_id},
            "Account": {"Code": "090"},
            "Date": "2026-09-02",
            "Amount": 2500.0,
            "Reference": payment_reference_for(deal_id, "pay-1"),
        },
        idempotency_key="keyed-in-by-hand-2",
    )
    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero).apply(webhook)
    assert "2026-09-02" in exc.value.detail and "2026-09-01" in exc.value.detail


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/Date(1518685950940+0000)/", "2018-02-15"),  # the official example
        ("/Date(1788220800000+1300)/", "2026-09-01"),  # an offset in the suffix: the day is read in UTC
        ("/Date(-62135596800000+0000)/", "0001-01-01"),  # a negative epoch: .NET's DateTime.MinValue
        ("2026-09-01T10:00:00", "2026-09-01"),
        ("", None),
        ("not-a-date", None),
    ],
)
def test_xero_dates_are_read_including_negative_epochs(value, expected):
    """Xero renders dates as ``/Date(ms+0000)/``; a blank date comes back as a negative epoch.

    ``datetime.fromtimestamp`` refuses negative values on Windows (``OSError``),
    which would have aborted a recovery walk on the first such payment. The
    parser adds the milliseconds to the epoch instead.
    """
    assert xero_date_to_iso(value) == expected


def test_a_payment_whose_date_cannot_be_read_is_a_mismatch_not_a_pass(
    xero, engine_factory, ledger_factory, client_factory, make_webhook
):
    """One payment carries our reference with the right amount -- and a date nobody can read.

    Skipping the date check when the date is unparseable would adopt it on
    the amount alone. An unreadable date is a mismatch: escalate, name it,
    adopt nothing, send nothing.
    """
    webhook = make_webhook(amount=2500.0, paid_at="2026-09-01T10:00:00Z")
    engine = engine_factory(xero)
    deal_id = engine.ledger.record_webhook(webhook)
    engine._ensure_contact(engine.ledger.get_deal(deal_id))
    engine._ensure_project(engine.ledger.get_deal(deal_id))
    engine._ensure_invoice(engine.ledger.get_deal(deal_id))
    deal = engine.ledger.get_deal(deal_id)
    planted = client_factory(xero).create_payment(
        {
            "Invoice": {"InvoiceID": deal.xero_invoice_id},
            "Account": {"Code": "090"},
            "Date": "2026-09-01",
            "Amount": 2500.0,
            "Reference": payment_reference_for(deal_id, "pay-1"),
        },
        idempotency_key="keyed-in-by-hand",
    ).entity
    for row in xero.invoices[deal.xero_invoice_id]["Payments"]:
        row["Date"] = "/Date(garbage)/"

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero).apply(webhook)

    assert exc.value.step == STEP_PAYMENT and planted["PaymentID"] in exc.value.detail
    assert "garbage" in exc.value.detail and "could not be read" in exc.value.detail
    assert ledger_factory().payments_for(deal_id)[0].xero_payment_id is None, "not adopted"
    assert len(xero.payments) == 1 and len(payment_puts(xero)) == 1, "and not re-sent either"


def test_a_bound_invoice_that_cannot_be_read_back_is_escalated_not_crashed(
    xero, engine_factory, ledger_factory, make_webhook
):
    """The ledger says the invoice exists; Xero answers 404 (voided and purged, wrong tenant, a restore).

    Before: an ``assert invoice is not None`` -- an ``AssertionError`` that no
    handler recorded and that aborted a recovery walk for every deal behind
    it. Now: ``UnresolvableStep`` with the ids, recorded on the row, and the
    walk goes on.
    """
    gone = make_webhook(deal_id="deal-gone", couple_names=("Ana Perez", "Tomas Ruiz"), payment_id="pay-g")
    fine = make_webhook(deal_id="deal-fine", couple_names=("Bea Soto", "Ivo Lund"), payment_id="pay-f")
    engine = engine_factory(xero)
    engine.ledger.record_webhook(gone)
    engine.ledger.record_webhook(fine)
    engine._ensure_contact(engine.ledger.get_deal("deal-gone"))
    engine._ensure_project(engine.ledger.get_deal("deal-gone"))
    engine._ensure_invoice(engine.ledger.get_deal("deal-gone"))
    bound = engine.ledger.get_deal("deal-gone").xero_invoice_id
    del xero.invoices[bound]  # Xero no longer has it

    report = engine_factory(xero).recover_all()

    assert [d.deal_id for d in report.failed] == ["deal-gone"]
    assert bound in report.failed[0].last_error and "cannot be read back" in report.failed[0].last_error
    assert report.failed[0].sync_status == SYNC_ERROR
    assert [d.deal_id for d in report.settled] == ["deal-fine"], "the deal behind it was still settled"
    assert report.settled[0].sync_status == SYNC_SYNCED and len(xero.payments) == 1
    assert ledger_factory().payments_for("deal-gone")[0].xero_payment_id is None


def test_the_schema_refuses_a_second_binding(ledger_factory, make_webhook):
    """A last line of defence that does not depend on the code being right.

    One Xero contact, project, invoice or payment binds to at most one local
    thing. If a future change ever tries to bind a second, the database
    rejects it rather than letting the duplicate become someone's
    bookkeeping problem.
    """
    ledger = ledger_factory()
    a = ledger.record_webhook(make_webhook(deal_id="deal-a", payment_id="pay-a"))
    b = ledger.record_webhook(make_webhook(deal_id="deal-b", payment_id="pay-b"))

    ledger.bind_contact(a, "c-41", "Ana Perez & Tomas Ruiz", "hs-deal-a")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_contact(b, "c-41", "Ana Perez & Tomas Ruiz", "hs-deal-b")

    ledger.bind_project(a, "p-55", "projects_api")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_project(b, "p-55", "projects_api")

    ledger.bind_invoice(a, "i-77", "HS-deal-a")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_invoice(b, "i-77", "HS-deal-b")

    ledger.bind_payment(a, "pay-a", "pm-90")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.bind_payment(b, "pay-b", "pm-90")


def test_apply_is_safe_to_call_repeatedly(xero, engine_factory, make_webhook):
    """Idempotence is a property of the operation, not of a retry budget."""
    webhook = make_webhook(amount=2500.0)
    engine = engine_factory(xero)

    first = engine.apply(webhook)
    for _ in range(5):
        again = engine.apply(webhook)
        assert again.xero_invoice_id == first.xero_invoice_id
        assert again.amount_paid == 2500.0
        assert again.amount_owed == 10000.0

    assert (len(xero.contacts), len(xero.invoices), len(xero.payments)) == (1, 1, 1)


def test_a_rebuilt_ledger_after_the_window_adopts_the_invoice_by_number_and_the_payment_by_reference(
    xero, engine_factory, make_webhook, tmp_path
):
    """The strong claim, and the one QuickBooks could not make.

    The ledger is gone *and* the six-minute window is over, so no key
    replays anything. Every step still settles: the contact by its
    ``ContactNumber``, the invoice by "Invoice # must be unique." and a
    lookup by ``InvoiceNumber``, the payment by counting the invoice's
    ``Payments[]`` for the planted ``Reference``. Nothing is created.
    """
    webhook = make_webhook(amount=2500.0)
    first = engine_factory(xero).apply(webhook)
    assert (len(xero.contacts), len(xero.invoices), len(xero.payments)) == (1, 1, 1)

    xero.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    assert xero.idempotency_keys == {}, "no key left to replay"
    rebuilt = engine_factory(xero, path=str(tmp_path / "rebuilt.db")).apply(webhook)

    assert (len(xero.contacts), len(xero.invoices), len(xero.payments)) == (1, 1, 1)
    assert rebuilt.xero_contact_id == first.xero_contact_id
    assert rebuilt.xero_invoice_id == first.xero_invoice_id
    assert rebuilt.xero_invoice_number == invoice_number_for("deal-1001")
    assert rebuilt.amount_paid == 2500.0 and rebuilt.amount_owed == 10000.0
    assert rebuilt.sync_status == SYNC_SYNCED
    unique = [r for r in xero.request_log if r.startswith("PUT /api.xro/2.0/Invoices key=")]
    assert len(unique) == 2, "the invoice create was sent again and refused -- that refusal is the lookup trigger"
    assert len(payment_puts(xero)) == 1, "the payment was never resent: it was counted first"
