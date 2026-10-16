"""The fake Xero on its own: the documented behaviours it must encode, pinned.

These tests speak raw HTTP so that a defect in ``client.py`` can never hide a
defect in the fake, and vice versa. Verbatim messages come from the official
OpenAPI examples and the idempotency guide; where the fake goes beyond the
documentation, the test says so.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from xero_idempotent_sync_proof.fake_xero import (
    ACCESS_TOKEN_TTL_SECONDS,
    BANK_ACCOUNT_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    IDEMPOTENCY_TTL_SECONDS,
    REFRESH_GRACE_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    ROUTE_CONTACT,
    ROUTE_PAYMENT,
    TENANT_ID,
    FakeXero,
)

TRANSPORT_ERRORS = (urllib.error.URLError, ConnectionError, OSError)


def token_for(xero: FakeXero) -> str:
    """Refresh with the fake's current refresh token and hand back an access token."""
    status, body, _ = refresh(xero, xero.refresh_token)
    assert status == 200, body
    return body["access_token"]


def refresh(xero: FakeXero, refresh_token: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode()
    req = urllib.request.Request(xero.token_url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _send(req)


def call(
    xero: FakeXero,
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    key: str | None = None,
    tenant: str | None = TENANT_ID,
) -> tuple[int, dict[str, Any] | list, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(xero.base_url + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if tenant is not None:
        req.add_header("xero-tenant-id", tenant)
    if key is not None:
        req.add_header("Idempotency-Key", key)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return _send(req)


def _send(req: urllib.request.Request) -> tuple[int, Any, dict[str, str]]:
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, _body(raw), dict(exc.headers)


def _body(raw: str) -> Any:
    """JSON when it is JSON; the text itself otherwise (Xero's 404 is plain text)."""
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return raw


def messages(body: dict[str, Any]) -> list[str]:
    """Every ValidationErrors[].Message in a 400 body, plus the top-level Message."""
    out = [body.get("Message", "")]
    for element in body.get("Elements", []):
        out += [e["Message"] for e in element.get("ValidationErrors", [])]
    return out


def contact(name: str, number: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"Name": name}
    if number is not None:
        body["ContactNumber"] = number
    return {"Contacts": [body]}


def invoice(contact_id: str, number: str, amount: float = 100.0, status: str = "AUTHORISED"):
    return {
        "Invoices": [
            {
                "Type": "ACCREC",
                "Contact": {"ContactID": contact_id},
                "InvoiceNumber": number,
                "Status": status,
                "Date": "2026-10-01",
                "DueDate": "2026-10-31",
                "LineItems": [
                    {"Description": "Deposit", "Quantity": 1, "UnitAmount": amount, "AccountCode": "200"}
                ],
            }
        ]
    }


def payment(invoice_id: str, amount: float, reference: str = "ref") -> dict[str, Any]:
    return {
        "Payments": [
            {
                "Invoice": {"InvoiceID": invoice_id},
                "Account": {"Code": BANK_ACCOUNT_CODE},
                "Date": "2026-10-02",
                "Amount": amount,
                "Reference": reference,
            }
        ]
    }


@pytest.fixture
def token(xero: FakeXero) -> str:
    return token_for(xero)


@pytest.fixture
def authorised_invoice(xero: FakeXero, token: str) -> str:
    """An AUTHORISED invoice for 100.00 under a fresh contact; returns its InvoiceID."""
    _, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Payer"))
    contact_id = body["Contacts"][0]["ContactID"]
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Invoices", token=token, body=invoice(contact_id, "INV-1")
    )
    assert status == 200, body
    return body["Invoices"][0]["InvoiceID"]


# -- tenancy ----------------------------------------------------------------


def test_every_api_call_needs_the_tenant_header(xero, token):
    status, body, _ = call(xero, "GET", "/api.xro/2.0/Organisation", token=token, tenant=None)
    assert status == 403, body
    status, body, _ = call(xero, "GET", "/api.xro/2.0/Organisation", token=token, tenant="nope")
    assert status == 403, body

    status, connections, _ = call(xero, "GET", "/connections", token=token, tenant=None)
    assert status == 200
    assert connections[0]["tenantId"] == TENANT_ID
    assert connections[0]["tenantType"] == "ORGANISATION"

    status, body, headers = call(xero, "GET", "/api.xro/2.0/Organisation", token=token)
    assert status == 200
    assert body["Organisations"][0]["IsDemoCompany"] is True
    assert "X-MinLimit-Remaining" in headers and "X-DayLimit-Remaining" in headers


# -- the idempotency store ----------------------------------------------------


def test_a_key_longer_than_128_characters_is_rejected_verbatim(xero, token):
    key = "k" * 129
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key=key
    )
    assert status == 400
    assert f"Idempotency Key: {key} with length 129 is too long." in messages(body)
    assert xero.contacts == {}


def test_a_replay_returns_the_cached_response_and_creates_nothing(xero, token):
    first = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="k1")
    second = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="k1")

    assert first[0] == second[0] == 200
    assert first[1] == second[1], "body replayed verbatim, same ContactID"
    assert "X-Fake-Replayed" not in first[2]
    assert second[2]["X-Fake-Replayed"] == "true"
    assert len(xero.contacts) == 1
    assert len([r for r in xero.request_log if r.startswith("REPLAY")]) == 1


def test_the_same_key_with_a_different_request_is_rejected_verbatim(xero, token):
    call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="k2")
    status, body, headers = call(
        xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana Corrected"), key="k2"
    )
    assert status == 400
    assert "Idempotency Key: k2 is used with a different request." in messages(body)
    assert "X-Fake-Replayed" not in headers
    assert len(xero.contacts) == 1, "and nothing was created or renamed"

    # A different method or URL counts as a different request too.
    status, body, _ = call(
        xero, "POST", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="k2"
    )
    assert status == 400 and "is used with a different request." in messages(body)[0]


def test_a_key_expires_six_minutes_after_the_first_call(xero, token, authorised_invoice):
    """The thesis of the repo: after the window the same key is a brand-new request.

    Shown where it costs money -- a payment, which has no unique field to
    collide on -- so the invoice is simply paid twice.
    """
    body = payment(authorised_invoice, 40.0, "deposit")
    assert call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=body, key="pay")[0] == 200

    xero.advance(IDEMPOTENCY_TTL_SECONDS - 1)
    status, _, headers = call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=body, key="pay")
    assert status == 200 and headers["X-Fake-Replayed"] == "true"
    assert len(xero.payments) == 1

    xero.advance(2)
    status, _, headers = call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=body, key="pay")
    assert status == 200 and "X-Fake-Replayed" not in headers
    assert len(xero.payments) == 2, "processed as a new key: the invoice is paid twice"
    inv = xero.invoices[authorised_invoice]
    assert (inv["AmountPaid"], inv["AmountDue"]) == (80.0, 20.0)


def test_an_error_is_cached_under_the_key_too(xero, token):
    """Fix the cause, resend the same key: still the cached 400. Only a new key gets through."""
    _, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Payer"))
    contact_id = body["Contacts"][0]["ContactID"]
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Invoices", token=token,
        body=invoice(contact_id, "INV-9", status="DRAFT"),
    )
    invoice_id = body["Invoices"][0]["InvoiceID"]

    pay = payment(invoice_id, 50.0)
    status, body, _ = call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=pay, key="p9")
    assert status == 400, "a DRAFT invoice cannot take a payment"
    assert any("AUTHORISED" in m for m in messages(body))

    # The cause goes away...
    status, _, _ = call(
        xero, "POST", f"/api.xro/2.0/Invoices/{invoice_id}", token=token,
        body={"Invoices": [{"Status": "AUTHORISED"}]},
    )
    assert status == 200

    # ...and the same key still answers with the cached 400.
    status, body, headers = call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=pay, key="p9")
    assert status == 400 and headers["X-Fake-Replayed"] == "true"
    assert xero.payments == {}

    status, _, _ = call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=pay, key="p9-new")
    assert status == 200 and len(xero.payments) == 1


def test_forget_keys_models_the_window_closing_without_touching_the_clock(xero, token):
    call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="k3")
    xero.forget_keys()
    status, body, headers = call(
        xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="k3"
    )
    assert status == 400 and "X-Fake-Replayed" not in headers
    assert "The contact name Ana is already assigned to another contact." in messages(body)[1]


def test_get_requests_ignore_the_key(xero, token):
    status, _, headers = call(xero, "GET", "/api.xro/2.0/Contacts", token=token, key="ignored")
    assert status == 200 and "X-Fake-Replayed" not in headers
    assert xero.idempotency_keys == {}


# -- contacts -------------------------------------------------------------------


def test_contact_name_must_be_unique_verbatim(xero, token):
    call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Buzz Lightyear"))
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Buzz Lightyear")
    )
    assert status == 400
    assert body["Type"] == "ValidationException"
    assert (
        "The contact name Buzz Lightyear is already assigned to another contact. "
        "The contact name must be unique across all active contacts."
    ) in messages(body)
    assert body["Elements"][0]["HasValidationErrors"] is True
    assert len(xero.contacts) == 1


def test_contact_number_must_be_unique_and_is_a_lookup_key(xero, token):
    _, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana", "hs-1"))
    contact_id = body["Contacts"][0]["ContactID"]

    status, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Bea", "hs-1"))
    assert status == 400
    # Assumption, not verified against Xero: the docs say only that a PUT
    # matching an existing ContactNumber "will receive an error". The wording
    # below is the fake's own, and is switchable so the sync can be shown not
    # to depend on it.
    assert "The contact number hs-1 is already assigned to another contact." in messages(body)
    xero.answer_number_collisions_with("A validation exception occurred")
    status, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Bea", "hs-1"))
    assert status == 400 and messages(body) == ["A validation exception occurred"] * 2

    status, body, _ = call(xero, "GET", "/api.xro/2.0/Contacts/hs-1", token=token)
    assert status == 200 and body["Contacts"][0]["ContactID"] == contact_id
    status, body, _ = call(xero, "GET", f"/api.xro/2.0/Contacts/{contact_id}", token=token)
    assert status == 200 and body["Contacts"][0]["ContactNumber"] == "hs-1"
    status, body, headers = call(xero, "GET", "/api.xro/2.0/Contacts/hs-missing", token=token)
    assert status == 404
    # Assumption, modelled on the harder side: Xero's 404 is commonly a plain
    # text body, not JSON. A client must read the status, not the shape.
    assert headers["Content-Type"].startswith("text/plain")
    assert body == "The resource you're looking for cannot be found"

    status, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Cid", "x" * 51))
    assert status == 400, "ContactNumber is capped at 50"


def test_where_name_lookup_is_case_insensitive_and_hides_archived_by_default(xero, token):
    _, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana Perez"))
    contact_id = body["Contacts"][0]["ContactID"]
    where = urllib.parse.quote('Name=="ANA perez"')

    status, body, _ = call(xero, "GET", f"/api.xro/2.0/Contacts?where={where}", token=token)
    assert status == 200 and [c["ContactID"] for c in body["Contacts"]] == [contact_id]

    xero.archive_contact(contact_id)
    _, body, _ = call(xero, "GET", f"/api.xro/2.0/Contacts?where={where}", token=token)
    assert body["Contacts"] == []
    _, body, _ = call(
        xero, "GET", f"/api.xro/2.0/Contacts?where={where}&includeArchived=true", token=token
    )
    assert [c["ContactStatus"] for c in body["Contacts"]] == ["ARCHIVED"]

    # Assumption, not verified against Xero: an archived contact keeps its
    # name reserved. Modelled on the side that makes the client's job harder.
    status, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana Perez"))
    assert status == 400 and "already assigned" in messages(body)[1]


def test_where_name_with_a_quote_is_unescaped(xero, token):
    call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact('Liam "Lee" O\'Brien'))
    where = urllib.parse.quote('Name=="Liam \\"Lee\\" O\'Brien"')
    status, body, _ = call(xero, "GET", f"/api.xro/2.0/Contacts?where={where}", token=token)
    assert status == 200 and len(body["Contacts"]) == 1
    status, body, _ = call(xero, "GET", "/api.xro/2.0/Contacts?where=Garbage", token=token)
    assert status == 400


# -- invoices -----------------------------------------------------------------


def test_invoice_number_must_be_unique_verbatim_and_is_filterable(xero, token):
    _, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Payer"))
    contact_id = body["Contacts"][0]["ContactID"]
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Invoices", token=token, body=invoice(contact_id, "HS-1", 250.0)
    )
    assert status == 200
    created = body["Invoices"][0]
    assert created["Status"] == "AUTHORISED"
    assert (created["Total"], created["AmountDue"], created["AmountPaid"]) == (250.0, 250.0, 0.0)
    assert created["Payments"] == []

    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Invoices", token=token, body=invoice(contact_id, "HS-1", 999.0)
    )
    assert status == 400 and "Invoice # must be unique." in messages(body)
    assert len(xero.invoices) == 1

    status, body, _ = call(xero, "GET", "/api.xro/2.0/Invoices?InvoiceNumbers=HS-1,HS-2", token=token)
    assert status == 200 and [i["InvoiceID"] for i in body["Invoices"]] == [created["InvoiceID"]]
    status, body, _ = call(xero, "GET", f"/api.xro/2.0/Invoices/{created['InvoiceID']}", token=token)
    assert status == 200 and body["Invoices"][0]["InvoiceNumber"] == "HS-1"

    bill = invoice(contact_id, "HS-3")
    bill["Invoices"][0]["Type"] = "ACCPAY"
    status, body, _ = call(xero, "PUT", "/api.xro/2.0/Invoices", token=token, body=bill)
    assert status == 400, "only ACCREC is modelled"


# -- payments -----------------------------------------------------------------


def test_payment_rules_and_the_invoice_read_back(xero, token, authorised_invoice):
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 100.01)
    )
    assert status == 400
    assert "Payment amount exceeds the amount outstanding on this document" in messages(body)

    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 40.0, "r")
    )
    assert status == 200
    first = body["Payments"][0]
    assert first["Status"] == "AUTHORISED" and first["Amount"] == 40.0
    # Dates come back in Xero's JSON rendering, not as the ISO string sent.
    assert first["Date"] == "/Date(1790899200000+0000)/", "2026-10-02 at UTC midnight"

    # Reference is NOT unique: a second payment with the same one is accepted.
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 40.0, "r")
    )
    assert status == 200 and body["Payments"][0]["PaymentID"] != first["PaymentID"]

    _, body, _ = call(xero, "GET", f"/api.xro/2.0/Invoices/{authorised_invoice}", token=token)
    inv = body["Invoices"][0]
    assert (inv["AmountPaid"], inv["AmountDue"]) == (80.0, 20.0)
    assert [p["Reference"] for p in inv["Payments"]] == ["r", "r"]
    assert {p["PaymentID"] for p in inv["Payments"]} == set(xero.payments)

    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 20.0)
    )
    assert status == 200
    _, body, _ = call(xero, "GET", f"/api.xro/2.0/Invoices/{authorised_invoice}", token=token)
    assert body["Invoices"][0]["Status"] == "PAID" and body["Invoices"][0]["AmountDue"] == 0.0


# -- projects -----------------------------------------------------------------


def test_projects_are_gated_and_names_are_not_unique(xero_factory):
    xero = xero_factory(projects_available=False)
    token = token_for(xero)
    _, body, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Payer"))
    contact_id = body["Contacts"][0]["ContactID"]
    project = {"contactId": contact_id, "name": "Wedding 2027"}

    status, body, _ = call(xero, "POST", "/projects.xro/2.0/Projects", token=token, body=project)
    assert status == 403 and "Projects" in json.dumps(body)
    status, _, _ = call(xero, "GET", f"/projects.xro/2.0/Projects?contactID={contact_id}", token=token)
    assert status == 403

    xero.set_projects_available(True)
    status, body, _ = call(xero, "POST", "/projects.xro/2.0/Projects", token=token, body=project)
    assert status == 201 and body["contactId"] == contact_id and body["name"] == "Wedding 2027"
    status, body, _ = call(xero, "POST", "/projects.xro/2.0/Projects", token=token, body=project)
    assert status == 201, "a duplicate name is accepted (assumption: names are not unique)"
    assert len(xero.projects) == 2

    status, body, _ = call(xero, "GET", f"/projects.xro/2.0/Projects?contactID={contact_id}", token=token)
    assert status == 200 and len(body["items"]) == 2 and body["pagination"]["itemCount"] == 2


def test_the_projects_list_is_paged_and_filters_on_contactID_only(xero_factory):
    """Documented: ``contactID`` (capital ID), ``pageSize`` 1..500 defaulting to 50,
    and a ``pagination`` block with ``pageCount``. The lowercase ``contactId``
    of the response body is not a query parameter and is ignored."""
    xero = xero_factory(projects_available=True)
    token = token_for(xero)
    ours = xero.plant_contact("Ours")["ContactID"]
    theirs = xero.plant_contact("Theirs")["ContactID"]
    for n in range(60):
        xero.plant_project(ours, f"P{n:02d}")
    xero.plant_project(theirs, "Not ours")

    status, body, _ = call(xero, "GET", f"/projects.xro/2.0/Projects?contactID={ours}", token=token)
    assert status == 200 and len(body["items"]) == 50, "50 per page by default"
    assert body["pagination"] == {"page": 1, "pageSize": 50, "pageCount": 2, "itemCount": 60}

    status, body, _ = call(
        xero, "GET", f"/projects.xro/2.0/Projects?contactID={ours}&page=2", token=token
    )
    assert status == 200 and [p["name"] for p in body["items"]] == [f"P{n:02d}" for n in range(50, 60)]

    status, body, _ = call(
        xero, "GET", f"/projects.xro/2.0/Projects?contactID={ours}&pageSize=500", token=token
    )
    assert status == 200 and len(body["items"]) == 60 and body["pagination"]["pageCount"] == 1

    status, body, _ = call(
        xero, "GET", f"/projects.xro/2.0/Projects?contactId={ours}&pageSize=500", token=token
    )
    assert body["pagination"]["itemCount"] == 61, "contactId (lowercase) is not a filter"

    status, _, _ = call(xero, "GET", "/projects.xro/2.0/Projects?pageSize=501", token=token)
    assert status == 400, "pageSize must be between 1 and 500"

    status, body, _ = call(
        xero, "POST", "/projects.xro/2.0/Projects", token=token, body={"contactId": "missing", "name": "x"}
    )
    assert status == 400


# -- OAuth --------------------------------------------------------------------


def test_refresh_rotates_and_the_previous_token_survives_only_the_grace_period(xero):
    original = xero.refresh_token
    status, first, _ = refresh(xero, original)
    assert status == 200 and first["refresh_token"] != original
    assert first["expires_in"] == ACCESS_TOKEN_TTL_SECONDS and first["token_type"] == "Bearer"

    # Inside the grace period the previous token still refreshes (and rotates again).
    xero.advance(REFRESH_GRACE_SECONDS - 1)
    status, second, _ = refresh(xero, original)
    assert status == 200 and second["refresh_token"] not in (original, first["refresh_token"])

    # The grace is measured from the first rotation; it is not extended by the retry.
    xero.advance(2)
    status, body, _ = refresh(xero, original)
    assert status == 400 and body["error"] == "invalid_grant"
    status, body, _ = refresh(xero, "rt-never-issued")
    assert status == 400 and body["error"] == "invalid_grant"
    status, _, _ = refresh(xero, xero.refresh_token)
    assert status == 200, "the current one keeps working"


def test_access_tokens_expire_after_thirty_minutes_and_can_be_revoked(xero):
    token = token_for(xero)
    assert call(xero, "GET", "/api.xro/2.0/Organisation", token=token)[0] == 200
    xero.advance(ACCESS_TOKEN_TTL_SECONDS + 1)
    assert call(xero, "GET", "/api.xro/2.0/Organisation", token=token)[0] == 401

    token = token_for(xero)
    xero.revoke_access_tokens_now()
    status, body, _ = call(xero, "GET", "/api.xro/2.0/Organisation", token=token)
    assert status == 401 and body["Status"] == 401


def test_the_token_endpoint_checks_the_client_and_the_grant(xero):
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": xero.refresh_token})
    req = urllib.request.Request(xero.token_url, data=data.encode(), method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(b"wrong:creds").decode())
    status, body, _ = _send(req)
    assert status == 401 and body["error"] == "invalid_client"

    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    req = urllib.request.Request(
        xero.token_url, data=urllib.parse.urlencode({"grant_type": "password"}).encode(), method="POST"
    )
    req.add_header("Authorization", f"Basic {basic}")
    status, body, _ = _send(req)
    assert status == 400 and body["error"] == "unsupported_grant_type"


# -- rate limiting --------------------------------------------------------------


def test_throttling_answers_429_with_the_documented_headers_and_replays_count(xero, token):
    xero.throttle_next(2, retry_after=7)
    for _ in range(2):
        status, _, headers = call(
            xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="t1"
        )
        assert status == 429
        assert headers["X-Rate-Limit-Problem"] == "minute" and headers["Retry-After"] == "7"
    assert xero.contacts == {}, "a throttled request never reached the create"

    status, _, before = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="t1")
    assert status == 200
    status, _, after = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="t1")
    assert status == 200 and after["X-Fake-Replayed"] == "true"
    assert int(after["X-MinLimit-Remaining"]) == int(before["X-MinLimit-Remaining"]) - 1

    throttled = [r for r in xero.request_log if r.startswith("429")]
    assert len(throttled) == 2 and all("key=t1" in r for r in throttled)


# -- fault injection -------------------------------------------------------------


def test_drop_after_commit_keeps_the_entity_and_the_key(xero, token):
    xero.drop_response_after_commit(ROUTE_CONTACT)
    with pytest.raises(TRANSPORT_ERRORS):
        call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana", "hs-1"), key="f1")
    assert len(xero.contacts) == 1, "committed on the server side; the caller never heard"

    status, body, headers = call(
        xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana", "hs-1"), key="f1"
    )
    assert status == 200 and headers["X-Fake-Replayed"] == "true"
    assert body["Contacts"][0]["ContactID"] in xero.contacts
    assert len(xero.contacts) == 1


def test_drop_before_commit_leaves_nothing_behind(xero, token, authorised_invoice):
    xero.drop_response_before_commit(ROUTE_PAYMENT)
    with pytest.raises(TRANSPORT_ERRORS):
        call(xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 10.0), key="f2")
    assert xero.payments == {} and xero.idempotency_keys == {}

    status, _, headers = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 10.0), key="f2"
    )
    assert status == 200 and "X-Fake-Replayed" not in headers
    assert len(xero.payments) == 1


def test_a_server_error_is_not_a_xero_body_and_commits_nothing(xero, token):
    """``fail_next_with``: a 5xx with an HTML body, the way a gateway answers, before any route runs."""
    xero.fail_next_with(503)
    req = urllib.request.Request(
        xero.base_url + "/api.xro/2.0/Contacts", data=json.dumps(contact("Ana")).encode(), method="PUT"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("xero-tenant-id", TENANT_ID)
    req.add_header("Content-Type", "application/json")
    req.add_header("Idempotency-Key", "s1")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 503
    assert exc.value.headers.get("Content-Type", "").startswith("text/html")
    assert xero.contacts == {} and xero.idempotency_keys == {}

    status, _, _ = call(xero, "PUT", "/api.xro/2.0/Contacts", token=token, body=contact("Ana"), key="s1")
    assert status == 200 and len(xero.contacts) == 1, "only the one armed answer fails"


def test_a_payment_to_an_unknown_bank_account_is_refused_until_the_account_exists(
    xero, token, authorised_invoice
):
    """``remove_bank_account`` / ``add_bank_account``: a validation 400 whose cause can go away."""
    xero.remove_bank_account(BANK_ACCOUNT_CODE)
    status, body, _ = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 10.0), key="b1"
    )
    assert status == 400 and "The specified account could not be found" in messages(body)
    assert xero.payments == {}

    xero.add_bank_account(BANK_ACCOUNT_CODE)
    status, _, headers = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 10.0), key="b1"
    )
    assert status == 400 and headers["X-Fake-Replayed"] == "true", "same key: the cached 400, cause or no cause"
    status, _, _ = call(
        xero, "PUT", "/api.xro/2.0/Payments", token=token, body=payment(authorised_invoice, 10.0), key="b2"
    )
    assert status == 200 and len(xero.payments) == 1


def jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a JWT-shaped token, signature unchecked (the fake does not sign)."""
    header, payload, _signature = token.split(".")
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_the_token_carries_the_granted_scopes_and_projects_needs_its_own(xero_factory):
    """``grant_scopes``: without ``projects`` in the token, projects.xro is 403 whatever the plan.

    Documented: the token response carries ``access_token``, ``expires_in``,
    ``token_type``, ``refresh_token`` (and ``id_token`` with OpenID) -- no
    ``scope`` field -- and the access token is a JWT that can be decoded. The
    granted scopes are a claim inside it (``scope``, an array), which is the
    only place the fake puts them.
    """
    xero = xero_factory(projects_available=True)
    xero.grant_scopes("offline_access accounting.contacts accounting.invoices accounting.payments")
    status, body, _ = refresh(xero, xero.refresh_token)
    assert status == 200 and "scope" not in body, "the documented response has no scope field"
    assert set(body) == {"access_token", "expires_in", "token_type", "refresh_token"}
    token = body["access_token"]
    claims = jwt_claims(token)
    assert claims["scope"] == ["offline_access", "accounting.contacts", "accounting.invoices", "accounting.payments"]
    assert claims["exp"] > claims["iat"] and claims["client_id"] == CLIENT_ID
    status, body, _ = call(xero, "GET", "/projects.xro/2.0/Projects", token=token)
    assert status == 403 and "scope" in json.dumps(body)
    status, _, _ = call(xero, "GET", "/api.xro/2.0/Organisation", token=token)
    assert status == 200, "the accounting scopes still work"


def test_a_refresh_token_dies_after_sixty_days_even_if_it_was_never_rotated(xero):
    """Documented: refresh tokens live 60 days. An app idle that long re-authorises by hand."""
    xero.advance(REFRESH_TOKEN_TTL_SECONDS - 1)
    status, body, _ = refresh(xero, xero.refresh_token)
    assert status == 200, "one second to spare"
    fresh = body["refresh_token"]

    xero.advance(REFRESH_TOKEN_TTL_SECONDS - 1)
    status, body, _ = refresh(xero, fresh)
    assert status == 200, "the rotated token got its own sixty days"

    xero.advance(REFRESH_TOKEN_TTL_SECONDS + 1)
    status, body, _ = refresh(xero, xero.refresh_token)
    assert status == 400 and body["error"] == "invalid_grant" and "expired" in body["error_description"]


def test_an_authorised_invoice_cannot_go_back_to_draft(xero, token, authorised_invoice):
    for status_back in ("DRAFT", "SUBMITTED"):
        status, body, _ = call(
            xero, "POST", f"/api.xro/2.0/Invoices/{authorised_invoice}", token=token,
            body={"Invoices": [{"Status": status_back}]},
        )
        assert status == 400, status_back
        assert any("AUTHORISED" in m and status_back in m for m in messages(body))
    assert xero.invoices[authorised_invoice]["Status"] == "AUTHORISED"
