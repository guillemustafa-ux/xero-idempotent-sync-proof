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
from xero_idempotent_sync_proof.client import UnknownOutcome, XeroRejected
from xero_idempotent_sync_proof.fake_xero import REFRESH_GRACE_SECONDS
from xero_idempotent_sync_proof.ledger import (
    IDEMPOTENCY_TTL_SECONDS,
    STEP_CONTACT,
    SYNC_ERROR,
    SYNC_SYNCED,
    contact_number_for,
    idempotency_key_for,
)

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


def test_granted_scopes_are_read_from_the_access_token_jwt_not_from_the_response(
    xero, auth_factory, monkeypatch
):
    """The token response has no ``scope`` field; the access token is a JWT with a ``scope`` claim.

    Three tokens, in order of what the app can learn: a JWT with the claim
    (the documented shape) -> the set; an opaque token with a ``scope``
    field in the response (not documented, but harmless to read) -> the set;
    an opaque token and nothing else -> ``None``, "unknown", never an empty
    set that reads as "nothing granted".
    """
    auth = auth_factory(xero)
    assert auth.granted_scopes() is None, "nothing has been seen yet"

    tokens = auth.refresh()
    assert tokens.access_token.count(".") == 2, "the fake issues a JWT"
    assert auth.granted_scopes() == {
        "offline_access", "accounting.contacts", "accounting.invoices", "accounting.payments",
        "accounting.settings.read", "projects",
    }

    def opaque(payload_scope: str | None):
        def post(_refresh_token: str) -> dict:
            payload = {"access_token": "opaque-" + "x" * 20, "expires_in": 1800,
                       "token_type": "Bearer", "refresh_token": "rt-next"}
            if payload_scope is not None:
                payload["scope"] = payload_scope
            return payload
        return post

    monkeypatch.setattr(auth, "_post_refresh", opaque("offline_access accounting.contacts"))
    auth.refresh()
    assert auth.granted_scopes() == {"offline_access", "accounting.contacts"}, "the response field, when present"

    monkeypatch.setattr(auth, "_post_refresh", opaque(None))
    auth.refresh()
    assert auth.granted_scopes() is None, "unknown, not empty"


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


def contact_puts(xero) -> list[str]:
    return [r for r in xero.request_log if r.startswith("PUT /api.xro/2.0/Contacts key=")]


def test_a_5xx_is_an_unknown_outcome_not_a_rejection(xero, engine_factory, make_webhook):
    """A 503 -- or any answer that is not a Xero body -- says nothing about the write.

    A gateway can answer 502 after the request was handed on; a 500 can come
    back after the row was committed. Reading it as "rejected, nothing
    happened" is the same bug as reading a dropped connection that way. The
    transport keeps it ``UnknownOutcome``, the deal row records it, and the
    next run looks up by the planted number before sending again.
    """
    webhook = make_webhook()
    xero.fail_next_with(503)

    with pytest.raises(UnknownOutcome) as exc:
        engine_factory(xero).apply(webhook)
    assert "503" in str(exc.value) and not isinstance(exc.value, XeroRejected)
    assert xero.contacts == {}, "this 503 happened to land before anything was committed"
    doubtful = engine_factory(xero).ledger.get_deal("deal-1001")
    assert doubtful.sync_status == SYNC_ERROR and "UnknownOutcome" in doubtful.last_error

    deal = engine_factory(xero).apply(webhook)

    assert len(xero.contacts) == 1 and deal.sync_status == SYNC_SYNCED
    number = contact_number_for("deal-1001")
    assert f"GET /api.xro/2.0/Contacts/{number} key=-" in xero.request_log, "looked up before resending"
    assert len(contact_puts(xero)) == 1, "the 503 never reached the route; the second send did"


def test_a_400_without_a_xero_body_is_unknown_and_a_plain_404_is_absent(xero, client_factory):
    """The body's shape is read only where the sync reads messages: the 400.

    A 400 whose body is a gateway's HTML page was not written by Xero and is
    no verdict on the write -> ``UnknownOutcome``. A 404 whose body is plain
    text, which is how Xero commonly answers a miss, is still a 404 ->
    ``None`` from the lookup, never "unknown".
    """
    client = client_factory(xero)
    xero.fail_next_with(400)
    with pytest.raises(UnknownOutcome) as exc:
        client.create_contact(CONTACT, idempotency_key=KEY)
    assert "400" in str(exc.value) and not isinstance(exc.value, XeroRejected)
    assert xero.contacts == {}

    assert client.get_contact("hs-deal-nobody") is None, "a plain-text 404 means absent"
    assert client.get_invoice("HS-nobody") is None


def test_throttling_that_would_outlast_the_key_window_stops_and_asks(
    xero, client_factory, engine_factory, make_webhook
):
    """``Retry-After`` is honoured -- up to the point where honouring it crosses six minutes.

    Two 429s with ``Retry-After: 200`` add up to 400 seconds of waiting. A
    send after that is "processed as a new key": if an earlier send under
    this key did land, the resend is the duplicate. So the transport stops
    at the window's edge and raises ``UnknownOutcome``; the sync then does
    what it does with every unknown outcome -- it looks up first.
    """
    slept: list[float] = []
    client = client_factory(xero, sleep=slept.append)
    xero.throttle_next(2, retry_after=200)

    with pytest.raises(UnknownOutcome) as exc:
        client.create_contact(CONTACT, idempotency_key=KEY)

    assert slept == [200.0], "the first wait fit; the second would have crossed the window"
    assert str(IDEMPOTENCY_TTL_SECONDS) in str(exc.value)
    assert xero.contacts == {}
    throttled = [r for r in xero.request_log if r.startswith("429")]
    assert len(throttled) == 2 and all(f"key={KEY}" in r for r in throttled)

    # The same thing through the sync: it records the doubt, and the next run
    # asks by the planted number before it sends.
    webhook = make_webhook(deal_id="deal-429")
    xero.throttle_next(2, retry_after=200)
    with pytest.raises(UnknownOutcome):
        engine_factory(xero).apply(webhook)
    deal = engine_factory(xero).apply(webhook)
    assert deal.sync_status == SYNC_SYNCED and len(xero.contacts) == 1
    assert f"GET /api.xro/2.0/Contacts/{contact_number_for('deal-429')} key=-" in xero.request_log
