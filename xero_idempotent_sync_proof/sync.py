"""The sync under test: one "deal paid" webhook, four exactly-once writes against Xero.

One entry point, ``apply(webhook)``, safe to call any number of times, from
any process, in any order of delivery. It decides what is left to do from
durable state, never from memory, and it refuses to guess when Xero gives it
no way to know.

Xero remembers an ``Idempotency-Key`` for six minutes. That settles the
transient retry -- a 429, a connection dropped a moment ago, two workers on
the same delivery -- and nothing else. What settles a write whose fate is
unknown an hour later, or after the ledger was rebuilt, is a value **we
planted** on the entity and can look up:

==============  ==========================  ==================================
The step        Xero offers                 How an in-flight write is settled
==============  ==========================  ==================================
Contact         unique ``Name``; unique     Send first: a refusal is the
                ``ContactNumber`` (ours)    signal to ask. ``GET /Contacts/
                                            {ContactNumber}``: one -> adopt;
                                            none, so the name is a stranger's
                                            -> the date variant, under a key
                                            of its own.
Invoice         unique ``InvoiceNumber``    Send first; "Invoice # must be
                (ours)                      unique." -> look it up by number
                                            and adopt.
Payment         ``Reference`` -- not        Look first, always: count the
                unique                      payments on the invoice carrying
                                            our reference. One -> adopt; none
                                            -> create; more -> stop.
Project         name -- not unique; the     Look first, always, under the
                API may be absent from      contact. A 403 -> ``project_mode
                the plan                    = unavailable`` in the ledger and
                                            a notice. Never silently.
==============  ==========================  ==================================

The rule behind the table: where Xero refuses a duplicate loudly, the
refusal is the lookup trigger; where it does not, look before every send.
And a step whose intent was already in flight when a previous process died
is always looked up before anything is resent, because the key it was sent
under may have expired by now.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import Created, ProjectsApiUnavailable, XeroClient, XeroRejected
from .ledger import (
    PROJECT_MODE_PROJECTS_API,
    PROJECT_MODE_UNAVAILABLE,
    REJECTED,
    STEP_CONTACT,
    STEP_INVOICE,
    STEP_PAYMENT,
    STEP_PROJECT,
    SYNC_ERROR,
    SYNC_SYNCED,
    Deal,
    Intent,
    Ledger,
    Payment,
    contact_name_for,
    contact_number_for,
    idempotency_key_for,
    invoice_number_for,
    payment_reference_for,
)

log = logging.getLogger("xero_sync")

# How many Name variants to try before escalating: the bare names, then the
# names with the wedding date. A third collision is not a naming problem, it
# is a data problem for a human.
MAX_NAME_VARIANTS = 2

# The one sales account and the one bank account this proof books against.
# Chart-of-accounts configuration is out of scope; these match the fake.
SALES_ACCOUNT_CODE = "200"
BANK_ACCOUNT_CODE = "090"

# Xero's refusals, by the fragment the sync keys on. Both the taken-Name and
# the taken-ContactNumber messages contain the first one.
ALREADY_ASSIGNED = "is already assigned to another contact"
INVOICE_NUMBER_TAKEN = "Invoice # must be unique."


class UnresolvableStep(Exception):
    """A step is in flight and Xero offers no safe way to learn its fate.

    Raised instead of guessing. A duplicate contact is a cleanup; a duplicate
    payment is money booked twice. The exception carries the deal, the step
    and the exact ids so a human can settle it.
    """

    def __init__(self, deal_id: str, step: str, detail: str):
        super().__init__(f"deal {deal_id}, step {step}: {detail}")
        self.deal_id = deal_id
        self.step = step
        self.detail = detail


def project_name_for(deal: Deal) -> str:
    """The name planted on the project: derived, so any process finds it."""
    return f"Wedding {deal.wedding_date} - {deal.xero_contact_name}"


@dataclass
class SyncEngine:
    ledger: Ledger
    client: XeroClient
    # Test hook: called at named seams so a scenario can kill the process
    # exactly where it hurts, or run another worker there. Production code
    # passes nothing.
    on_seam: Callable[[str], None] | None = None
    # Anything the business must be told, beyond the ledger row itself.
    notices: list[str] = field(default_factory=list)

    def _seam(self, name: str) -> None:
        if self.on_seam is not None:
            self.on_seam(name)

    def _surface(self, message: str) -> None:
        log.warning(message)
        self.notices.append(message)

    # -- public -----------------------------------------------------------

    def apply(self, webhook: dict[str, Any]) -> Deal:
        """Record one webhook delivery and bring its deal fully into Xero.

        Idempotent by construction: a duplicate delivery, a reordered one, or
        a retry after a crash all end in the same state.
        """
        deal_id = self.ledger.record_webhook(webhook)
        return self.sync(deal_id)

    def sync(self, deal_id: str) -> Deal:
        """Complete whatever is left for one deal, in order, exactly once."""
        try:
            deal = self.ledger.get_deal(deal_id)
            if deal.xero_contact_id is None:
                self._ensure_contact(deal)
            deal = self.ledger.get_deal(deal_id)
            if deal.project_mode is None:
                self._ensure_project(deal)
            deal = self.ledger.get_deal(deal_id)
            if deal.xero_invoice_id is None:
                self._ensure_invoice(deal)
            deal = self.ledger.get_deal(deal_id)
            for payment in self.ledger.payments_for(deal_id):
                if payment.xero_payment_id is None:
                    self._ensure_payment(deal, payment)
            self._read_back_balance(deal)
            self.ledger.set_status(deal_id, SYNC_SYNCED)
        except UnresolvableStep as exc:
            self.ledger.set_status(deal_id, SYNC_ERROR, str(exc))
            raise
        return self.ledger.get_deal(deal_id)

    def recover_all(self) -> list[Deal]:
        """Settle every deal left in doubt by a previous run."""
        return [self.sync(d.deal_id) for d in list(self.ledger.unresolved())]

    # -- contact ----------------------------------------------------------

    def _ensure_contact(self, deal: Deal) -> None:
        number = contact_number_for(deal.deal_id)
        for variant in range(MAX_NAME_VARIANTS):
            name = contact_name_for(deal.couple_names, deal.wedding_date, variant)
            ref = str(variant)
            intent = self.ledger.get_intent(deal.deal_id, STEP_CONTACT, ref)
            if intent is not None and intent.state == REJECTED:
                continue  # settled earlier: that name belongs to someone else
            contact: dict[str, Any] | None = None
            if intent is not None:
                # A send was in flight when a previous process died. Ask
                # before resending: the key it went out under may be gone.
                contact = self._ours_by_number(number)
                log.info(
                    "resolve %s step=contact by ContactNumber %s -> %s",
                    deal.deal_id,
                    number,
                    f"ADOPT (ContactID={contact['ContactID']})" if contact else "PROVEN ABSENT",
                )
            else:
                intent = self._begin(deal.deal_id, STEP_CONTACT, ref)
            if contact is None:
                contact = self._create_contact(intent, name, number)
                if contact is None:
                    self.ledger.reject_step(deal.deal_id, STEP_CONTACT, ref)
                    log.info("Name %r is taken by a stranger -> variant %d", name, variant + 1)
                    continue
            self._seam("after_contact_response")
            self.ledger.finish_step(deal.deal_id, STEP_CONTACT, ref, contact["ContactID"])
            self.ledger.bind_contact(
                deal.deal_id,
                contact["ContactID"],
                contact["Name"],
                contact.get("ContactNumber") or number,
            )
            return
        raise UnresolvableStep(deal.deal_id, STEP_CONTACT, self._contact_holders(deal, number))

    def _ours_by_number(self, contact_number: str) -> dict[str, Any] | None:
        """``GET /Contacts/{ContactNumber}``: the lookup that settles a contact for good.

        The number is derived from the deal and planted on the create, so a
        holder is ours by construction and ``None`` means ours never landed.
        Unlike the Idempotency-Key, it does not expire.
        """
        return self.client.get_contact(contact_number)

    def _create_contact(
        self, intent: Intent, name: str, number: str
    ) -> dict[str, Any] | None:
        """Create (or replay) the contact under its derived key.

        Returns the contact, or ``None`` when the name is held by a stranger.
        The "already assigned" ``400`` is a definite answer, so it is safe to
        act on -- but it is not, on its own, proof that the holder is not
        ours: the planted ``ContactNumber`` settles that.
        """
        try:
            created = self.client.create_contact(
                {"Name": name, "ContactNumber": number}, idempotency_key=intent.idempotency_key
            )
        except XeroRejected as exc:
            if not exc.says(ALREADY_ASSIGNED):
                raise
            ours = self._ours_by_number(number)
            if ours is not None:
                log.info(
                    "resolve %s step=contact -> 400 on %r, holder carries our ContactNumber "
                    "-> ADOPT (ContactID=%s)",
                    intent.deal_id,
                    name,
                    ours["ContactID"],
                )
                return ours
            return None
        self._log_verdict(intent, created, "ContactID")
        return created.entity

    def _contact_holders(self, deal: Deal, number: str) -> str:
        """Who holds each Name variant -- for the human, with the exact ids."""
        holders = {}
        for variant in range(MAX_NAME_VARIANTS):
            name = contact_name_for(deal.couple_names, deal.wedding_date, variant)
            holders[name] = [
                c["ContactID"] for c in self.client.contacts_named(name, include_archived=True)
            ]
        return (
            f"every Name variant is held by a contact that does not carry this deal's "
            f"ContactNumber {number!r}: {holders}"
        )

    # -- project ----------------------------------------------------------

    def _ensure_project(self, deal: Deal) -> None:
        assert deal.xero_contact_id is not None
        name = project_name_for(deal)
        intent = self.ledger.get_intent(deal.deal_id, STEP_PROJECT) or self._begin(
            deal.deal_id, STEP_PROJECT, mode=PROJECT_MODE_PROJECTS_API
        )
        project_id: str | None
        try:
            project = self._settle_project(deal, intent, name)
        except ProjectsApiUnavailable as exc:
            # A definite answer: this organisation's plan has no Projects.
            # Say so where the business reads it. The invoice and payments
            # go on the contact either way -- in Xero they always do.
            self.ledger.set_step_mode(deal.deal_id, STEP_PROJECT, "", PROJECT_MODE_UNAVAILABLE)
            mode = PROJECT_MODE_UNAVAILABLE
            project_id = None
            log.info("project step for %s -> UNAVAILABLE (flagged): %s", deal.deal_id, exc)
            self._surface(
                f"deal {deal.deal_id}: the Projects API is not available to this organisation "
                f"(HTTP {exc.status}), so no Xero Project was created for {name!r}. The "
                f"invoice and payments are booked on the contact; the ledger says "
                f"project_mode={PROJECT_MODE_UNAVAILABLE}."
            )
        else:
            mode = PROJECT_MODE_PROJECTS_API
            project_id = project["projectId"]
            log.info("project step for %s -> PROJECTS API (projectId=%s)", deal.deal_id, project_id)

        self._seam("after_project_response")
        self.ledger.finish_step(deal.deal_id, STEP_PROJECT, "", project_id or "")
        self.ledger.bind_project(deal.deal_id, project_id, mode)

    def _settle_project(self, deal: Deal, intent: Intent, name: str) -> dict[str, Any]:
        created = self.client.create_project(
            deal.xero_contact_id, name, idempotency_key=intent.idempotency_key
        )
        self._log_verdict(intent, created, "projectId")
        return created.entity

    # -- invoice ----------------------------------------------------------

    def _ensure_invoice(self, deal: Deal) -> None:
        number = invoice_number_for(deal.deal_id)
        intent = self.ledger.get_intent(deal.deal_id, STEP_INVOICE) or self._begin(
            deal.deal_id, STEP_INVOICE
        )
        invoice = self._create_invoice(deal, intent, number)
        self._seam("after_invoice_response")
        self.ledger.finish_step(deal.deal_id, STEP_INVOICE, "", invoice["InvoiceID"])
        self.ledger.bind_invoice(deal.deal_id, invoice["InvoiceID"], invoice["InvoiceNumber"])

    def _create_invoice(self, deal: Deal, intent: Intent, number: str) -> dict[str, Any]:
        # Every field is a function of the deal row alone, so the body under
        # this key is the same on every send -- a changed body under the
        # same key is a 400, not a replay.
        body = {
            "Type": "ACCREC",
            "Contact": {"ContactID": deal.xero_contact_id},
            "InvoiceNumber": number,
            "Reference": f"Wedding {deal.wedding_date}",
            # Payments apply only to AUTHORISED invoices.
            "Status": "AUTHORISED",
            "DueDate": deal.wedding_date,
            "LineAmountTypes": "NoTax",
            "LineItems": [
                {
                    "Description": f"Wedding {deal.wedding_date} - contract total",
                    "Quantity": 1,
                    "UnitAmount": deal.contract_total,
                    "AccountCode": SALES_ACCOUNT_CODE,
                }
            ],
        }
        created = self.client.create_invoice(body, idempotency_key=intent.idempotency_key)
        self._log_verdict(intent, created, "InvoiceID")
        return created.entity

    # -- payments ---------------------------------------------------------

    def _ensure_payment(self, deal: Deal, payment: Payment) -> None:
        ref = payment.hubspot_payment_id
        reference = payment_reference_for(deal.deal_id, ref)
        intent = self.ledger.get_intent(deal.deal_id, STEP_PAYMENT, ref) or self._begin(
            deal.deal_id, STEP_PAYMENT, ref
        )
        body = {
            "Invoice": {"InvoiceID": deal.xero_invoice_id},
            "Account": {"Code": BANK_ACCOUNT_CODE},
            "Date": payment.paid_at[:10],
            "Amount": payment.amount,
            "Reference": reference,
        }
        self._seam("before_payment_send")
        created = self.client.create_payment(body, idempotency_key=intent.idempotency_key)
        self._log_verdict(intent, created, "PaymentID")
        entity = created.entity
        self._seam("after_payment_response")
        self.ledger.finish_step(deal.deal_id, STEP_PAYMENT, ref, entity["PaymentID"])
        self.ledger.bind_payment(deal.deal_id, ref, entity["PaymentID"])

    def _read_back_balance(self, deal: Deal) -> None:
        """State the balance from Xero; never add the webhook's amount to a counter.

        The invoice knows what has been applied to it. A local ``+= amount``
        is one more non-idempotent write, and it drifts on the first
        redelivered webhook.
        """
        assert deal.xero_invoice_id is not None
        invoice = self.client.get_invoice(deal.xero_invoice_id)
        assert invoice is not None
        amount_paid = round(float(invoice["AmountPaid"]), 2)
        amount_owed = round(float(invoice["AmountDue"]), 2)
        booked = [p for p in self.ledger.payments_for(deal.deal_id) if p.xero_payment_id]
        last_payment_at = max((p.paid_at for p in booked), default=None) or None
        self.ledger.set_balance(deal.deal_id, amount_paid, amount_owed, last_payment_at)

    # -- helpers ----------------------------------------------------------

    def _begin(self, deal_id: str, step: str, ref: str = "", mode: str | None = None) -> Intent:
        # Durable intent first. If the process dies on the next line, the
        # next run still knows this step was attempted, and with which key.
        intent = self.ledger.begin_step(deal_id, step, ref, mode)
        log.info(
            "intent committed for %s step=%s%s key=%s (durable, pre-send)",
            deal_id,
            step,
            f"[{ref}]" if ref else "",
            intent.idempotency_key,
        )
        return intent

    @staticmethod
    def _log_verdict(intent: Intent, created: Created, id_field: str) -> None:
        verdict = "REPLAYED" if created.replayed else "CREATED NOW"
        log.info(
            "resolve %s step=%s%s key=%s -> %s (%s=%s)",
            intent.deal_id,
            intent.step,
            f"[{intent.ref}]" if intent.ref else "",
            intent.idempotency_key,
            verdict,
            id_field,
            created.entity[id_field],
        )


# -- the anti-patterns, implemented rather than described --------------------


def naive_contact_sync(ledger: Ledger, client: XeroClient, deal_id: str) -> dict[str, Any]:
    """The contact create as it is usually written.

    A fresh key per call (so Xero has nothing to recognise), nothing durable
    before the call, no ``ContactNumber`` planted, and the instinctive
    handler for "already assigned": append a number and try again. After a
    lost response the retry finds the name taken -- by the contact it
    created a moment ago -- and cheerfully makes a second one.
    """
    deal = ledger.get_deal(deal_id)
    base = contact_name_for(deal.couple_names, deal.wedding_date)
    name = base
    for attempt in range(1, 4):
        try:
            created = client.create_contact({"Name": name}, idempotency_key=str(uuid.uuid4()))
        except XeroRejected as exc:
            if not exc.says(ALREADY_ASSIGNED):
                raise
            name = f"{base} ({attempt + 1})"
            continue
        ledger.bind_contact(deal_id, created.entity["ContactID"], name, None)  # too late to be durable
        return created.entity
    raise RuntimeError("could not find a free name")


def naive_contact_sync_trusting_the_key(
    ledger: Ledger, client: XeroClient, deal_id: str
) -> dict[str, Any]:
    """The contact create by someone who read about ``Idempotency-Key`` -- and stopped there.

    The key is derived, so a retry replays and every quick test passes.
    Nothing is planted and nothing is looked up, because the key "handles
    it". It does, for six minutes. The redelivery after that is processed as
    a new request, collides on the name, and gets the "(2)" treatment.
    """
    deal = ledger.get_deal(deal_id)
    base = contact_name_for(deal.couple_names, deal.wedding_date)
    name = base
    for attempt in range(1, 4):
        try:
            created = client.create_contact(
                {"Name": name}, idempotency_key=idempotency_key_for(deal_id, STEP_CONTACT, name)
            )
        except XeroRejected as exc:
            if not exc.says(ALREADY_ASSIGNED):
                raise
            name = f"{base} ({attempt + 1})"
            continue
        ledger.bind_contact(deal_id, created.entity["ContactID"], name, None)
        return created.entity
    raise RuntimeError("could not find a free name")


def naive_payment_sync(
    ledger: Ledger, client: XeroClient, deal_id: str, hubspot_payment_id: str
) -> dict[str, Any]:
    """The payment create that trusts the key and records nothing first.

    Payments have no unique field to collide on, so after the six-minute
    window nothing stops the retry: the invoice is simply paid twice.
    """
    deal = ledger.get_deal(deal_id)
    payment = next(
        p for p in ledger.payments_for(deal_id) if p.hubspot_payment_id == hubspot_payment_id
    )
    created = client.create_payment(
        {
            "Invoice": {"InvoiceID": deal.xero_invoice_id},
            "Account": {"Code": BANK_ACCOUNT_CODE},
            "Date": payment.paid_at[:10],
            "Amount": payment.amount,
            "Reference": payment_reference_for(deal_id, hubspot_payment_id),
        },
        idempotency_key=idempotency_key_for(deal_id, STEP_PAYMENT, hubspot_payment_id),
    )
    ledger.bind_payment(deal_id, hubspot_payment_id, created.entity["PaymentID"])  # too late
    return created.entity
