"""Scenario 2 -- Xero Projects may not be in the plan, and a project has no unique name.

The Projects API (``projects.xro/2.0``) is a separate product behind the
organisation's plan: an organisation without it answers ``403``. What the
sync does at that moment is the difference between an honest integration
and one that quietly books every wedding with no project while the
dashboard says "Project". And when it *is* available, a project name is not
unique, so an in-flight create is settled the way the QuickBooks GraphQL
path was: look for the name we planted under the contact, and never guess.
"""

from __future__ import annotations

import pytest

from xero_idempotent_sync_proof.client import UnknownOutcome
from xero_idempotent_sync_proof.fake_xero import IDEMPOTENCY_TTL_SECONDS, ROUTE_PROJECT
from xero_idempotent_sync_proof.ledger import (
    PENDING,
    PROJECT_MODE_PROJECTS_API,
    PROJECT_MODE_UNAVAILABLE,
    STEP_PROJECT,
    SYNC_ERROR,
    SYNC_SYNCED,
)
from xero_idempotent_sync_proof.sync import UnresolvableStep, project_name_for


def project_posts(xero) -> list[str]:
    """Every create that reached the project route (replays excluded)."""
    return [r for r in xero.request_log if r.startswith("POST /projects.xro/2.0/Projects key=")]


def test_an_organisation_without_projects_is_flagged_and_still_invoiced(
    xero, engine_factory, client_factory, make_webhook
):
    """The honest path on a 403.

    The wedding still gets its invoice and payments -- in Xero they hang off
    the contact, not the project -- and the ledger row says exactly what did
    not happen. Nobody discovers the missing projects at the end of the
    quarter.
    """
    engine = engine_factory(xero)
    deal = engine.apply(make_webhook())

    assert deal.project_mode == PROJECT_MODE_UNAVAILABLE
    assert deal.xero_project_id is None
    assert deal.sync_status == SYNC_SYNCED
    assert xero.projects == {}

    invoice = client_factory(xero).get_invoice(deal.xero_invoice_id)
    assert invoice["Contact"]["ContactID"] == deal.xero_contact_id, "invoiced on the contact"
    assert invoice["Status"] == "AUTHORISED" and len(invoice["Payments"]) == 1

    assert len(engine.notices) == 1
    assert "Projects API is not available" in engine.notices[0]
    assert PROJECT_MODE_UNAVAILABLE in engine.notices[0]
    assert any(r.endswith("-> 403 (plan)") for r in xero.request_log), "Projects was asked first"


def test_a_403_with_the_projects_scope_missing_is_the_apps_fault_not_the_plans(
    xero_with_projects, engine_factory, make_webhook
):
    """Two things answer 403 on ``projects.xro``: a plan without Projects, and a
    token without the ``projects`` scope. Only the first is "unavailable to
    this organisation". The token response says which scopes were granted;
    when ``projects`` is not among them, recording ``project_mode =
    unavailable`` would bury an authorisation mistake as a plan limit.
    """
    xero_with_projects.grant_scopes(
        "offline_access accounting.contacts accounting.invoices accounting.payments"
    )
    engine = engine_factory(xero_with_projects)

    with pytest.raises(UnresolvableStep) as exc:
        engine.apply(make_webhook())

    assert exc.value.step == STEP_PROJECT
    assert "projects" in exc.value.detail and "scope" in exc.value.detail
    deal = engine.ledger.get_deal("deal-1001")
    assert deal.project_mode is None and deal.sync_status == SYNC_ERROR, "not recorded as unavailable"
    assert xero_with_projects.invoices == {}, "nothing downstream until a human re-authorises"
    assert engine.notices == []


def test_an_organisation_with_projects_gets_a_real_project(
    xero_with_projects, engine_factory, make_webhook
):
    """The same code, a plan with Projects: a real project, and the ledger says so."""
    engine = engine_factory(xero_with_projects)
    deal = engine.apply(make_webhook())

    assert deal.project_mode == PROJECT_MODE_PROJECTS_API
    assert deal.xero_project_id in xero_with_projects.projects
    project = xero_with_projects.projects[deal.xero_project_id]
    assert project["name"] == project_name_for(deal)
    assert project["contactId"] == deal.xero_contact_id
    assert engine.notices == []
    assert len(project_posts(xero_with_projects)) == 1


def test_crash_between_create_project_and_persist_adopts_by_planted_name(
    xero_with_projects, ledger_factory, engine_factory, run_child, make_webhook
):
    """A project name is not unique, so a resend would simply make a second one.

    The child is killed after the create returns and before the id is
    written. Recovery looks for the name it planted under the contact,
    finds exactly one, and adopts it. No second project, and no second POST
    on the wire -- the lookup settled it before the key had to.
    """
    deal_id = ledger_factory().record_webhook(make_webhook())

    run_child(xero_with_projects, deal_id, crash_at="after_project_response")

    killed = ledger_factory()
    intent = killed.get_intent(deal_id, STEP_PROJECT)
    assert intent is not None and intent.state == PENDING
    assert intent.mode == PROJECT_MODE_PROJECTS_API
    assert killed.get_deal(deal_id).xero_project_id is None
    assert len(xero_with_projects.projects) == 1

    # Recovery runs after the key has expired, so a resend would NOT be a
    # replay: only the lookup can keep this to one project.
    xero_with_projects.advance(IDEMPOTENCY_TTL_SECONDS + 1)
    deal = engine_factory(xero_with_projects).recover_all().settled[0]

    assert len(xero_with_projects.projects) == 1, "adopted, not recreated"
    assert deal.xero_project_id == next(iter(xero_with_projects.projects))
    assert deal.project_mode == PROJECT_MODE_PROJECTS_API
    assert deal.sync_status == SYNC_SYNCED
    assert len(project_posts(xero_with_projects)) == 1, "the lookup settled it; nothing was resent"


def test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing(
    xero_with_projects, ledger_factory, engine_factory, run_child, make_webhook
):
    """The honest failure.

    A second project with our name already exists under the contact --
    someone created it by hand, or an earlier bug did. Picking either one is
    a guess. The sync stops, records why with both ids, and leaves both
    projects exactly as they were. Nothing downstream is booked.
    """
    deal_id = ledger_factory().record_webhook(make_webhook())
    run_child(xero_with_projects, deal_id, crash_at="after_project_response")

    deal = ledger_factory().get_deal(deal_id)
    xero_with_projects.plant_project(deal.xero_contact_id, project_name_for(deal))
    assert len(xero_with_projects.projects) == 2

    with pytest.raises(UnresolvableStep) as exc:
        engine_factory(xero_with_projects).sync(deal_id)

    assert exc.value.step == STEP_PROJECT
    assert "2 projects" in exc.value.detail
    for project_id in xero_with_projects.projects:
        assert project_id in exc.value.detail, "both ids, for a human"
    after = ledger_factory().get_deal(deal_id)
    assert after.sync_status == SYNC_ERROR and "refusing to pick one" in after.last_error
    assert after.xero_project_id is None and after.project_mode is None
    assert len(xero_with_projects.projects) == 2, "still two -- we refused to make it three"
    assert xero_with_projects.invoices == {}, "and nothing downstream was booked against a guess"

    # Startup recovery reports it instead of raising, and books nothing either.
    report = engine_factory(xero_with_projects).recover_all()
    assert report.settled == [] and [d.deal_id for d in report.failed] == [deal_id]
    assert "refusing to pick one" in report.failed[0].last_error
    assert len(xero_with_projects.projects) == 2 and xero_with_projects.invoices == {}


def test_recovery_settles_the_deals_it_can_and_reports_the_one_it_cannot(
    xero_with_projects, ledger_factory, engine_factory, run_child, make_webhook
):
    """One escalated deal must not hold every other deal hostage.

    Deal A is in doubt and unresolvable (two projects carry its name). Deal B
    was killed after its payment landed. Startup recovery walks both: A is
    recorded with its reason and left alone; B is settled. A recovery that
    stopped at the first exception would leave B's payment unbound forever
    -- and a later redelivery with no intent lookup would be the duplicate.
    """
    first = make_webhook(deal_id="deal-a", couple_names=("Ana Perez", "Tomas Ruiz"), payment_id="pay-a")
    second = make_webhook(deal_id="deal-b", couple_names=("Bea Soto", "Ivo Lund"), payment_id="pay-b")
    ledger = ledger_factory()
    ledger.record_webhook(first)
    ledger.record_webhook(second)

    run_child(xero_with_projects, "deal-a", crash_at="after_project_response")
    deal_a = ledger_factory().get_deal("deal-a")
    xero_with_projects.plant_project(deal_a.xero_contact_id, project_name_for(deal_a))
    run_child(xero_with_projects, "deal-b", crash_at="after_payment_response")
    assert len(xero_with_projects.payments) == 1

    report = engine_factory(xero_with_projects).recover_all()

    assert [d.deal_id for d in report.failed] == ["deal-a"]
    assert report.failed[0].sync_status == SYNC_ERROR
    assert "2 projects" in report.failed[0].last_error and "refusing to pick one" in report.failed[0].last_error
    assert [d.deal_id for d in report.settled] == ["deal-b"]
    assert report.settled[0].sync_status == SYNC_SYNCED
    assert report.settled[0].amount_paid == 2500.0
    assert len(xero_with_projects.payments) == 1, "B's payment adopted, not re-sent"
    assert len(xero_with_projects.projects) == 3, "A's two left alone, B's one created"
    assert ledger_factory().payments_for("deal-b")[0].xero_payment_id == next(iter(xero_with_projects.payments))


def test_recovery_finds_the_planted_project_behind_hundreds_of_others(
    xero_with_projects, ledger_factory, engine_factory, run_child, make_webhook
):
    """The Projects list is paged: 50 per page by default, 500 at most.

    A busy contact -- a venue with hundreds of events -- pushes our project
    off the first page. A lookup that reads one page sees nothing, concludes
    "never landed", and creates a second project. The sync must ask for
    ``pageSize=500``, filter by ``contactID`` (the documented spelling), and
    walk every page ``pagination.pageCount`` announces.
    """
    engine = engine_factory(xero_with_projects)
    deal_id = engine.ledger.record_webhook(make_webhook())
    engine._ensure_contact(engine.ledger.get_deal(deal_id))
    contact_id = engine.ledger.get_deal(deal_id).xero_contact_id
    for n in range(520):  # more than one page even at the maximum page size
        xero_with_projects.plant_project(contact_id, f"Engagement shoot {n:03d}")

    run_child(xero_with_projects, deal_id, crash_at="after_project_response")
    assert len(xero_with_projects.projects) == 521
    ours = [p for p in xero_with_projects.projects.values() if p["name"] != f"Engagement shoot {0:03d}"
            and not p["name"].startswith("Engagement shoot")]
    assert len(ours) == 1

    xero_with_projects.advance(IDEMPOTENCY_TTL_SECONDS + 1)  # no key left to replay
    deal = engine_factory(xero_with_projects).recover_all().settled[0]

    assert len(xero_with_projects.projects) == 521, "adopted from page 2, not recreated"
    assert deal.xero_project_id == ours[0]["projectId"]
    assert deal.sync_status == SYNC_SYNCED
    assert len(project_posts(xero_with_projects)) == 1
    pages = [r for r in xero_with_projects.request_log if r.startswith("GET /projects.xro/2.0/Projects")]
    assert len(pages) >= 2, "page 1 did not have it; page 2 was asked for"


def test_the_lookup_matches_the_planted_name_exactly_and_ignores_the_contacts_other_projects(
    xero_with_projects, client_factory, engine_factory, make_webhook
):
    """A contact's other projects are not ours, and neither is a stranger's project with our name."""
    engine = engine_factory(xero_with_projects)
    deal_id = engine.ledger.record_webhook(make_webhook())
    engine._ensure_contact(engine.ledger.get_deal(deal_id))
    deal = engine.ledger.get_deal(deal_id)
    name = project_name_for(deal)
    xero_with_projects.plant_project(deal.xero_contact_id, "Engagement shoot")
    xero_with_projects.plant_project(deal.xero_contact_id, name + " (rehearsal)")
    stranger = xero_with_projects.plant_contact("Someone Else")
    xero_with_projects.plant_project(stranger["ContactID"], name)
    ours = xero_with_projects.plant_project(deal.xero_contact_id, name)

    found = client_factory(xero_with_projects).find_projects(deal.xero_contact_id, name)
    assert [p["projectId"] for p in found] == [ours["projectId"]]

    synced = engine.sync(deal_id)
    assert synced.xero_project_id == ours["projectId"], "adopted the one with our exact name"
    assert len(xero_with_projects.projects) == 4, "nothing created"


def test_a_lost_response_on_the_project_create_is_settled_by_the_name_lookup(
    xero_with_projects, engine_factory, make_webhook
):
    """Same hazard, no crash: Xero committed the project and the answer was lost.

    The process is alive and cannot tell. The next attempt looks the name up
    first, finds the one project, and adopts it; the key never has to replay.
    """
    webhook = make_webhook()
    engine = engine_factory(xero_with_projects)

    xero_with_projects.drop_response_after_commit(ROUTE_PROJECT)
    with pytest.raises(UnknownOutcome):
        engine.apply(webhook)
    assert len(xero_with_projects.projects) == 1, "committed; the caller was never told"
    assert engine.ledger.get_deal(webhook["deal_id"]).project_mode is None

    deal = engine_factory(xero_with_projects).apply(webhook)

    assert len(xero_with_projects.projects) == 1
    assert deal.xero_project_id == next(iter(xero_with_projects.projects))
    assert deal.project_mode == PROJECT_MODE_PROJECTS_API
    assert deal.sync_status == SYNC_SYNCED
    assert len(project_posts(xero_with_projects)) == 1, "one create on the wire, ever"
