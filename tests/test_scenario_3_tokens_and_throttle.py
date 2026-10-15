"""Scenario 3 -- the two ways Xero stops talking to a correct integration.

Xero rotates the refresh token on every refresh and throttles at 60 calls a
minute per tenant. Both are boring until the day they are not: the first
locks the app out for good if the rotated token was not persisted (after a
30-minute grace), the second turns a retry loop into a duplicate factory if
the retry does not carry the same ``Idempotency-Key``.
"""

from __future__ import annotations

import pytest

from xero_idempotent_sync_proof.auth import (
    AuthLockedOut,
    TokenStore,
    refresh_but_keep_the_old_refresh_token,
)
from xero_idempotent_sync_proof.fake_xero import REFRESH_GRACE_SECONDS
from xero_idempotent_sync_proof.ledger import STEP_CONTACT, idempotency_key_for

CONTACT = {"Name": "Ana Perez & Tomas Ruiz", "ContactNumber": "hs-deal-1001"}
KEY = idempotency_key_for("deal-1001", STEP_CONTACT, "0")


def test_refresh_rotation_persists_the_latest_token_for_a_fresh_process(
    xero, auth_factory, client_factory, db_path
):
    """The refresh token that came back is the only one that still works."""
    first_process = auth_factory(xero)
    original = first_process.store.load().refresh_token

    first_process.refresh()

    stored = TokenStore(db_path).load()
    assert stored.refresh_token != original, "the rotated token replaced the stored one"
    assert stored.refresh_token == xero.refresh_token

    # A brand-new process, hours later, with nothing but the database, and
    # the old refresh token long past its grace.
    xero.revoke_access_tokens_now()
    xero.advance(REFRESH_GRACE_SECONDS + 1)
    created = client_factory(xero).create_contact(CONTACT, idempotency_key=KEY)
    assert created.entity["Name"] == CONTACT["Name"]
    refreshes = [r for r in xero.request_log if r == "POST /connect/token"]
    assert len(refreshes) == 2, "one explicit refresh, one triggered by the fresh process"
    assert len(xero.contacts) == 1


def test_a_store_that_kept_the_old_refresh_token_still_works_inside_the_grace_period(
    xero, auth_factory
):
    """The anti-pattern, caught in time.

    Refresh, take the access token, ignore the rotated refresh token. For 30
    minutes Xero forgives it: the previous token still refreshes. A correct
    ``refresh`` on that stale store rotates again and persists the new one.
    """
    auth = auth_factory(xero)
    original = auth.store.load().refresh_token

    refresh_but_keep_the_old_refresh_token(auth)
    assert auth.store.load().refresh_token == original, "the rotated token was dropped"
    assert xero.refresh_token != original, "and the service already retired the old one"

    xero.advance(REFRESH_GRACE_SECONDS - 1)
    tokens = auth.refresh()
    assert tokens.refresh_token == xero.refresh_token != original
    assert TokenStore(auth.store.path).load().refresh_token == tokens.refresh_token


def test_a_store_that_kept_the_old_refresh_token_is_locked_out_after_the_grace_period(
    xero, auth_factory
):
    """The same anti-pattern, 31 minutes later: ``invalid_grant`` for good.

    No retry policy in the world gets the app back in without a human
    re-authorising it.
    """
    auth = auth_factory(xero)
    original = auth.store.load().refresh_token

    refresh_but_keep_the_old_refresh_token(auth)
    xero.advance(REFRESH_GRACE_SECONDS + 1)

    with pytest.raises(AuthLockedOut) as exc:
        auth.refresh()
    assert "invalid_grant" in str(exc.value)
    assert auth.store.load().refresh_token == original, "nothing new to persist; a human is needed"


def test_an_expired_access_token_is_refreshed_on_401_and_the_call_retried(xero, client_factory):
    """Clocks disagree; the 401 is the truth. One refresh, one retry, no duplicate."""
    client = client_factory(xero)
    client.auth.access_token()  # obtain one
    xero.revoke_access_tokens_now()  # and expire it behind our back

    created = client.create_contact(CONTACT, idempotency_key=KEY)

    assert created.replayed is False
    assert len(xero.contacts) == 1
    refreshes = [r for r in xero.request_log if r == "POST /connect/token"]
    assert len(refreshes) == 2, "bootstrap refresh, then the one forced by the 401"
    assert [r for r in xero.request_log if r.startswith("401")] == ["401 PUT /api.xro/2.0/Contacts"]


def test_a_throttled_create_resends_the_same_key_and_makes_one_contact(xero, client_factory):
    """Two ``429`` in a row mid-create.

    The retry after ``Retry-After`` carries the same ``Idempotency-Key``, so
    whichever of the sends Xero counted, it counted once.
    """
    slept: list[float] = []
    client = client_factory(xero, sleep=slept.append)
    xero.throttle_next(2, retry_after=7)

    created = client.create_contact(CONTACT, idempotency_key=KEY)

    assert created.replayed is False
    assert slept == [7.0, 7.0], "Retry-After honoured, once per 429"
    throttled = [r for r in xero.request_log if r.startswith("429")]
    assert len(throttled) == 2
    keys = {r.split("key=")[1] for r in throttled}
    assert keys == {KEY}, "same key on every send"
    sends = [r for r in xero.request_log if r == f"PUT /api.xro/2.0/Contacts key={KEY}"]
    assert len(sends) == 1, "the throttled attempts never reached the create; the third did"
    assert len(xero.contacts) == 1
