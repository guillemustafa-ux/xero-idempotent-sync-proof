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
    deal = engine_factory(xero_with_projects).recover_all()[0]

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
        engine_factory(xero_with_projects).recover_all()

    assert exc.value.step == STEP_PROJECT
    assert "2 projects" in exc.value.detail
    for project_id in xero_with_projects.projects:
        assert project_id in exc.value.detail, "both ids, for a human"
    after = ledger_factory().get_deal(deal_id)
    assert after.sync_status == SYNC_ERROR and "refusing to pick one" in after.last_error
    assert after.xero_project_id is None and after.project_mode is None
    assert len(xero_with_projects.projects) == 2, "still two -- we refused to make it three"
    assert xero_with_projects.invoices == {}, "and nothing downstream was booked against a guess"


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
