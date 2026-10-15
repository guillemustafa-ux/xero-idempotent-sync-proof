"""Scenario 1 -- the gap between "Xero created the contact" and "we wrote down the ContactID".

The process is killed for real (``os._exit`` in a child process, exit 137)
at the exact instruction between the remote commit and the local one. What
is on disk afterwards is all the next run gets.

Two things are Xero-specific here and have no QuickBooks counterpart: the
``Idempotency-Key`` is remembered for six minutes and then "processed as a
new key", and a key replayed with a different body is a loud ``400`` rather
than a silent replay of the original.
"""

from __future__ import annotations

import pytest

from xero_idempotent_sync_proof.client import UnknownOutcome, XeroRejected
from xero_idempotent_sync_proof.fake_xero import IDEMPOTENCY_TTL_SECONDS, ROUTE_CONTACT
from xero_idempotent_sync_proof.ledger import (
    IDEMPOTENCY_KEY_MAX_LEN,
    PENDING,
    REJECTED,
    STEP_CONTACT,
    SYNC_ERROR,
    SYNC_SYNCED,
    contact_name_for,
    contact_number_for,
    idempotency_key_for,
)
from xero_idempotent_sync_proof.sync import (
    UnresolvableStep,
    naive_contact_sync,
    naive_contact_sync_trusting_the_key,
)


def contact_puts(xero) -> list[str]:
    """Every create that reached the contact route (replays excluded)."""
    return [r for r in xero.request_log if r.startswith("PUT /api.xro/2.0/Contacts key=")]


def lookups_by_number(xero, contact_number: str) -> list[str]:
    return [r for r in xero.request_log if r == f"GET /api.xro/2.0/Contacts/{contact_number} key=-"]


def test_naive_retry_after_a_real_kill_makes_a_second_contact(
    xero, ledger_factory, client_factory, run_child, make_webhook
):
    """The anti-pattern, demonstrated rather than asserted.

    Xero commits the contact, the response is lost, the worker dies before
    writing anything down. The retry mints a fresh ``Idempotency-Key`` (so
    Xero has nothing to recognise), sends the same ``Name``, gets the
    "already assigned" ``400``, and handles it the instinctive way: append a
    number.
    """
    deal_id = ledger_factory().record_webhook(make_webhook())

    xero.drop_response_after_commit(ROUTE_CONTACT)
    run_child(xero, deal_id, crash_at="-", strategy="naive-contact")

    # Xero has the contact. The ledger has no idea.
    assert len(xero.contacts) == 1
    assert ledger_factory().get_deal(deal_id).xero_contact_id is None

    # The retry, behaving exactly as written.
    naive_contact_sync(ledger_factory(), client_factory(xero), deal_id)

    names = sorted(c["Name"] for c in xero.contacts.values())
    assert names == ["Ana Perez & Tomas Ruiz", "Ana Perez & Tomas Ruiz (2)"], (
        "this is the bug this repo exists to prevent: one couple, two contacts"
    )


def test_durable_sync_survives_a_real_kill_between_200_and_persist(
    xero, ledger_factory, engine_factory, run_child, make_webhook
):
    """The same kill, against the design under test.

    What happens if the process dies after Xero creates the contact but
    before you save its id? The intent row says a send was in flight, so the
    next process asks ``GET /Contacts/{ContactNumber}`` before sending again
    -- and adopts.
    """
    deal_id = ledger_factory().record_webhook(make_webhook())
    number = contact_number_for(deal_id)

    # The remote call succeeds; the worker is killed on the next instruction.
    run_child(xero, deal_id, crash_at="after_contact_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_CONTACT, "0")
    assert intent is not None and intent.state == PENDING, "the intent outlived the process"
    assert killed.get_deal(deal_id).xero_contact_id is None, "the id was never written"
    assert len(xero.contacts) == 1

    # A brand new process, with nothing but the database.
    recovered = engine_factory(xero).recover_all()

    assert len(xero.contacts) == 1, "recovery must not create a second contact"
    assert len(recovered) == 1
    contact = next(iter(xero.contacts.values()))
    assert recovered[0].xero_contact_id == contact["ContactID"]
    assert recovered[0].xero_contact_number == contact["ContactNumber"] == number
    assert recovered[0].sync_status == SYNC_SYNCED

    assert len(contact_puts(xero)) == 1, "the create went out once; recovery asked, it did not resend"
    assert len(lookups_by_number(xero, number)) == 1, "settled by the planted ContactNumber"


def test_lost_response_is_resolved_without_duplicating(xero, engine_factory, make_webhook):
    """Same hazard, no crash: the answer is lost in the network.

    The process is alive and still cannot tell whether the write landed.
    """
    webhook = make_webhook()
    engine = engine_factory(xero)

    xero.drop_response_after_commit(ROUTE_CONTACT)
    with pytest.raises(UnknownOutcome):
        engine.apply(webhook)

    assert len(xero.contacts) == 1, "Xero committed; we simply were not told"
    assert engine.ledger.get_deal(webhook["deal_id"]).xero_contact_id is None

    deal = engine_factory(xero).apply(webhook)

    assert len(xero.contacts) == 1
    assert deal.xero_contact_id == next(iter(xero.contacts))
    assert deal.sync_status == SYNC_SYNCED
    assert len(contact_puts(xero)) == 1


def test_a_create_that_never_landed_is_actually_sent(xero, engine_factory, make_webhook):
    """The mirror case, and the reason "do nothing on doubt" is not the answer.

    The connection died *before* Xero committed. Recovery must create the
    contact, not adopt a contact that does not exist.
    """
    webhook = make_webhook()

    xero.drop_response_before_commit(ROUTE_CONTACT)
    with pytest.raises(UnknownOutcome):
        engine_factory(xero).apply(webhook)

    assert xero.contacts == {}, "nothing was created"

    deal = engine_factory(xero).apply(webhook)

    assert len(xero.contacts) == 1, "recovery created the contact that was missing"
    assert deal.sync_status == SYNC_SYNCED
    assert len(contact_puts(xero)) == 2, "the lookup said absent, so the create was sent again"


def test_two_deals_with_the_same_names_stay_two_contacts(xero, engine_factory, make_webhook):
    """Why the "already assigned" handler must not merge.

    Two couples can share both names. Xero refuses the second ``Name``, the
    holder does not carry this deal's ``ContactNumber``, and the sync
    answers with a *deterministic* variant -- the wedding date -- under a
    key of its own, rather than adopting a stranger or minting "(2)".
    """
    first = make_webhook(deal_id="deal-1", wedding_date="2027-03-14", payment_id="pay-1")
    second = make_webhook(deal_id="deal-2", wedding_date="2027-11-20", payment_id="pay-2")

    engine = engine_factory(xero)
    a = engine.apply(first)
    b = engine.apply(second)

    assert a.xero_contact_name == "Ana Perez & Tomas Ruiz"
    assert b.xero_contact_name == "Ana Perez & Tomas Ruiz (2027-11-20)"
    assert a.xero_contact_id != b.xero_contact_id
    assert len(xero.contacts) == 2, "two real couples stayed two contacts"
    assert len(xero.invoices) == 2 and len(xero.payments) == 2

    rejected = engine.ledger.get_intent("deal-2", STEP_CONTACT, "0")
    assert rejected is not None and rejected.state == REJECTED
    sent = [r.split("key=")[1] for r in contact_puts(xero)]
    assert sent[1:] == [
        idempotency_key_for("deal-2", STEP_CONTACT, "0"),
        idempotency_key_for("deal-2", STEP_CONTACT, "1"),
    ], "each variant goes out under its own derived key"

    # And the variant is stable: a fresh process re-derives the same name.
    again = engine_factory(xero).apply(second)
    assert again.xero_contact_id == b.xero_contact_id
    assert len(xero.contacts) == 2


def test_a_name_with_quotes_is_escaped_in_the_where_clause(xero, engine_factory, make_webhook):
    """``where=Name=="..."`` is a string literal. Get the quoting wrong and the
    lookup returns a parse error instead of the holder.

    The sync only queries by name when it has to escalate, so the holders'
    ids can be handed to a human; both variants are taken by strangers here.
    """
    names = ('Niamh "Nim" O\'Brien', "Sean D'Arcy")
    base = contact_name_for(list(names), "2027-06-06")
    strangers = [
        xero.plant_contact(base),
        xero.plant_contact(contact_name_for(list(names), "2027-06-06", 1)),
    ]

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero).apply(
            make_webhook(deal_id="deal-y", couple_names=names, wedding_date="2027-06-06")
        )

    assert exc.value.step == STEP_CONTACT
    for stranger in strangers:
        assert stranger["ContactID"] in exc.value.detail, "the exact holders, for a human"
    assert len(xero.contacts) == 2, "nothing was created, nothing was adopted"
    assert xero.invoices == {}
    assert engine_factory(xero).ledger.get_deal("deal-y").sync_status == SYNC_ERROR


def test_replaying_a_key_with_a_corrected_body_is_rejected_loudly(
    xero, client_factory, engine_factory, make_webhook
):
    """The loud one -- the opposite of QuickBooks Online.

    QBO replays the original response and discards the corrected body with a
    200. Xero answers "Idempotency Key ... is used with a different request",
    a definite ``400``, and creates nothing. Loud is better, but it means a
    sync must never reuse a key for a body it has changed: every body the
    sync can send under a key is a pure function of the deal and the step,
    and a name variant carries a key of its own.
    """
    client = client_factory(xero)
    key = idempotency_key_for("deal-typo", STEP_CONTACT, "0")

    created = client.create_contact(
        {"Name": "Ana Perez & Tomas Ruis", "ContactNumber": "hs-deal-typo"}, idempotency_key=key
    )
    with pytest.raises(XeroRejected) as exc:
        client.create_contact(
            {"Name": "Ana Perez & Tomas Ruiz", "ContactNumber": "hs-deal-typo"}, idempotency_key=key
        )

    assert exc.value.status == 400 and exc.value.says("is used with a different request")
    assert [c["Name"] for c in xero.contacts.values()] == ["Ana Perez & Tomas Ruis"]
    assert created.replayed is False

    # The sync's variant path: a stranger holds the base name, so the sync
    # sends the date variant -- under the variant's key, never the base key.
    xero.plant_contact("Ana Perez & Tomas Ruiz")
    deal = engine_factory(xero).apply(make_webhook())

    assert deal.xero_contact_name == "Ana Perez & Tomas Ruiz (2027-03-14)"
    keys = [r.split("key=")[1] for r in contact_puts(xero) if "deal-typo" not in r]
    assert keys[-2:] == [
        idempotency_key_for("deal-1001", STEP_CONTACT, "0"),
        idempotency_key_for("deal-1001", STEP_CONTACT, "1"),
    ]
    reused = [r for r in xero.request_log if r.startswith("400 key reused")]
    assert len(reused) == 1 and reused[0].endswith(f"key={key}"), (
        "the only different-request 400 is the one this test provoked; the sync never trips it"
    )


def test_after_the_window_the_key_is_new_and_only_the_contact_number_settles_it(
    xero, ledger_factory, client_factory, engine_factory, make_webhook
):
    """Six minutes is the whole promise of the key.

    A sync that trusts the key alone passes every quick test: the retry a
    moment later is replayed. The redelivery seven minutes later is
    "processed as a new key", collides on the ``Name``, and is handled the
    instinctive way. The durable sync asks for the ``ContactNumber`` it
    planted, which does not expire.
    """
    # -- the sync that trusts the key --------------------------------------
    naive = make_webhook(deal_id="deal-naive", couple_names=("Bea Soto", "Ivo Lund"))
    ledger = ledger_factory()
    ledger.record_webhook(naive)
    client = client_factory(xero)

    xero.drop_response_after_commit(ROUTE_CONTACT)
    with pytest.raises(UnknownOutcome):
        naive_contact_sync_trusting_the_key(ledger, client, "deal-naive")
    assert len(xero.contacts) == 1, "committed; the caller was never told"

    # A retry inside the window: the key replays, and it looks right.
    naive_contact_sync_trusting_the_key(ledger, client, "deal-naive")
    assert len(xero.contacts) == 1
    assert any(r.startswith("REPLAY PUT /api.xro/2.0/Contacts") for r in xero.request_log)

    # A redelivery seven minutes later: same key, "processed as a new key".
    xero.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    naive_contact_sync_trusting_the_key(ledger, client, "deal-naive")
    assert sorted(c["Name"] for c in xero.contacts.values()) == [
        "Bea Soto & Ivo Lund",
        "Bea Soto & Ivo Lund (2)",
    ], "one couple, two contacts, and every call returned a 200"

    # -- the durable sync, same kill, same wait -------------------------------
    webhook = make_webhook()
    xero.drop_response_after_commit(ROUTE_CONTACT)
    with pytest.raises(UnknownOutcome):
        engine_factory(xero).apply(webhook)
    key = idempotency_key_for("deal-1001", STEP_CONTACT, "0")
    assert key in xero.idempotency_keys

    xero.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    assert key not in xero.idempotency_keys, "the window closed"

    deal = engine_factory(xero).apply(webhook)

    ours = [c for c in xero.contacts.values() if c["Name"] == "Ana Perez & Tomas Ruiz"]
    assert len(ours) == 1 and deal.xero_contact_id == ours[0]["ContactID"]
    assert deal.sync_status == SYNC_SYNCED
    assert len(xero.contacts) == 3, "two from the naive sync, one from the durable one"
    assert len(lookups_by_number(xero, contact_number_for("deal-1001"))) == 1


def test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing(
    xero, engine_factory, make_webhook, tmp_path
):
    """Why the keys and the planted numbers are derived and not merely persisted.

    Lose the ledger -- a restore from an old backup, a rebuild from the
    webhook log -- and replay the webhook against an empty database. A
    process with no memory of the first run computes the same keys and the
    same ``ContactNumber`` and ``InvoiceNumber``, and nothing is created
    twice.
    """
    webhook = make_webhook()
    first = engine_factory(xero).apply(webhook)
    assert (len(xero.contacts), len(xero.invoices), len(xero.payments)) == (1, 1, 1)

    rebuilt = engine_factory(xero, path=str(tmp_path / "rebuilt.db")).apply(webhook)

    assert (len(xero.contacts), len(xero.invoices), len(xero.payments)) == (1, 1, 1)
    assert rebuilt.xero_contact_id == first.xero_contact_id
    assert rebuilt.xero_contact_number == first.xero_contact_number == contact_number_for("deal-1001")
    assert rebuilt.xero_invoice_id == first.xero_invoice_id
    assert rebuilt.amount_paid == 2500.0 and rebuilt.amount_owed == 10000.0
    assert len(idempotency_key_for(webhook["deal_id"], "payment", "pay-1")) <= IDEMPOTENCY_KEY_MAX_LEN


def test_a_holder_carrying_our_contact_number_is_adopted_not_suffixed(
    xero, engine_factory, make_webhook
):
    """The "already assigned" 400 is not, on its own, proof of a stranger.

    The holder of the name carries the ``ContactNumber`` we derive for this
    deal -- created by an earlier run whose ledger is gone, or by hand from
    the same CRM id. It is ours. Adopt it; do not make a dated twin.
    """
    planted = xero.plant_contact("Ana Perez & Tomas Ruiz", contact_number_for("deal-1001"))

    deal = engine_factory(xero).apply(make_webhook())

    assert deal.xero_contact_id == planted["ContactID"]
    assert deal.xero_contact_name == "Ana Perez & Tomas Ruiz", "no date suffix"
    assert len(xero.contacts) == 1
    assert deal.sync_status == SYNC_SYNCED
