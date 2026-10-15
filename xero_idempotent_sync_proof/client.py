"""HTTP transport for Xero, with the one distinction that matters.

A call against Xero has three outcomes, not two:

* it **succeeded** and you were told so;
* it **provably failed** -- the server answered with a ``ValidationException``,
  so nothing was created ("The contact name ... is already assigned to
  another contact" is the interesting one);
* the **outcome is unknown** -- the connection died, the request timed out,
  the process was killed. The write may or may not have landed.

Collapsing the third case into the second is the bug, so this transport
keeps them apart at the type level.

Three Xero-specific behaviours are handled here because they are transport
concerns, not business ones:

* every API call carries the ``xero-tenant-id`` header. Xero is
  multi-tenant and the access token alone does not name an organisation;
* every create carries an ``Idempotency-Key`` header. If Xero receives a
  second request with the same key within six minutes, it returns the
  **same response as the original** instead of performing the operation
  again. That makes "resend it" both the question and the fix -- for six
  minutes. What settles the question after that is the sync's business, not
  the transport's;
* a ``429`` is answered by resending the *same* request -- same URL, same
  body, same key -- after the ``Retry-After`` the server asked for. The
  sleeper is injectable so the proof does not sleep.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .auth import Authenticator

XERO_API_URL = "https://api.xero.com"

# A dropped response surfaces as one of these. They do not all descend from
# OSError, so catching OSError alone silently misses the interesting case.
TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    OSError,
)

MUTATING_METHODS = ("POST", "PUT", "PATCH")

# The fake marks an Idempotency-Key replay with this response header so the
# log can say REPLAYED vs CREATED NOW. The real service does not send it;
# nothing in the client *decides* anything based on it.
REPLAY_HEADER = "X-Fake-Replayed"


class UnknownOutcome(Exception):
    """The request may or may not have been applied. Do not retry blindly."""


class XeroRejected(Exception):
    """Xero answered with an error status. Nothing was applied."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body
        self.messages = validation_messages(body)

    def says(self, fragment: str) -> bool:
        """Whether any validation message contains ``fragment``."""
        return any(fragment in m for m in self.messages)


class XeroNotFound(XeroRejected):
    """``404``: the resource named in the URL does not exist."""


class ProjectsApiUnavailable(XeroRejected):
    """The Projects API answered 403: this organisation's plan does not include it."""


def validation_messages(body: str) -> list[str]:
    """Every message in a Xero error body: ``Message``, ``Elements[].ValidationErrors[].Message``,
    ``Detail`` and the Projects API's ``message``."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return [body] if body else []
    if not isinstance(parsed, dict):
        return []
    out: list[str] = []
    for key in ("Message", "Detail", "message", "error_description", "error"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            out.append(value)
    for element in parsed.get("Elements") or []:
        for err in element.get("ValidationErrors") or []:
            if err.get("Message"):
                out.append(err["Message"])
    return out


def where_quote(value: str) -> str:
    """Quote a value for a ``where`` string literal: ``\\`` and ``"`` are escaped.

    Assumption, not verified against Xero: the escape syntax of the filter
    language. The fake accepts exactly this.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class Created:
    entity: dict[str, Any]
    replayed: bool


@dataclass
class _Response:
    status: int
    body: Any
    headers: dict[str, str]


class XeroClient:
    def __init__(
        self,
        base_url: str,
        tenant_id: str,
        auth: Authenticator,
        *,
        timeout: float = 5.0,
        max_throttle_retries: int = 5,
        default_retry_after: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.auth = auth
        self.timeout = timeout
        self.max_throttle_retries = max_throttle_retries
        self.default_retry_after = default_retry_after
        self.sleep = sleep

    # -- transport --------------------------------------------------------

    def _accounting(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}/api.xro/2.0/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _projects(self, path: str = "", query: dict[str, str] | None = None) -> str:
        url = f"{self.base_url}/projects.xro/2.0/Projects"
        if path:
            url += "/" + path.lstrip("/")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Any = None,
        idempotency_key: str | None = None,
        tenant: bool = True,
    ) -> _Response:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        refreshed = False
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", "application/json")
            req.add_header("Authorization", f"Bearer {self.auth.access_token()}")
            if tenant:
                req.add_header("xero-tenant-id", self.tenant_id)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if idempotency_key is not None and method in MUTATING_METHODS:
                req.add_header("Idempotency-Key", idempotency_key)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return _Response(
                        resp.status, json.loads(raw) if raw else {}, dict(resp.headers)
                    )
            except urllib.error.HTTPError as exc:
                # The server spoke. Whatever it says, it is a definite answer.
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 401 and not refreshed:
                    # Access token expired under us. One refresh, one retry.
                    self.auth.refresh()
                    refreshed = True
                    continue
                if exc.code == 429:
                    if attempt >= self.max_throttle_retries:
                        raise XeroRejected(exc.code, raw) from exc
                    # Same URL, same body, same key: if the first send did
                    # land, Xero replays it instead of doubling it.
                    self.sleep(self._retry_after(exc.headers.get("Retry-After")))
                    attempt += 1
                    continue
                if exc.code == 403 and url.startswith(self._projects()):
                    raise ProjectsApiUnavailable(exc.code, raw) from exc
                if exc.code == 404:
                    raise XeroNotFound(exc.code, raw) from exc
                raise XeroRejected(exc.code, raw) from exc
            except TRANSPORT_ERRORS as exc:
                # Nobody spoke. The write's fate is unknowable from here.
                raise UnknownOutcome(f"{method} {url}: {exc}") from exc

    def _retry_after(self, header: str | None) -> float:
        try:
            return float(header) if header is not None else self.default_retry_after
        except ValueError:
            return self.default_retry_after

    def _create(
        self, collection: str, body: dict[str, Any], idempotency_key: str | None
    ) -> Created:
        # ``idempotency_key=None`` exists only so the anti-patterns can send a
        # create with no idempotency at all. The sync never does.
        resp = self._request(
            "PUT", self._accounting(collection), body={collection: [body]},
            idempotency_key=idempotency_key,
        )
        return Created(resp.body[collection][0], resp.headers.get(REPLAY_HEADER) == "true")

    # -- tenancy ------------------------------------------------------------

    def connections(self) -> list[dict[str, Any]]:
        return self._request("GET", f"{self.base_url}/connections", tenant=False).body

    def organisation(self) -> dict[str, Any]:
        return self._request("GET", self._accounting("Organisation")).body["Organisations"][0]

    # -- accounting operations ----------------------------------------------

    def create_contact(self, body: dict[str, Any], *, idempotency_key: str | None) -> Created:
        return self._create("Contacts", body, idempotency_key)

    def create_invoice(self, body: dict[str, Any], *, idempotency_key: str | None) -> Created:
        return self._create("Invoices", body, idempotency_key)

    def create_payment(self, body: dict[str, Any], *, idempotency_key: str | None) -> Created:
        return self._create("Payments", body, idempotency_key)

    def get_contact(self, id_or_number: str) -> dict[str, Any] | None:
        """By ``ContactID`` or by the planted ``ContactNumber``; ``None`` when absent."""
        try:
            resp = self._request("GET", self._accounting(f"Contacts/{id_or_number}"))
        except XeroNotFound:
            return None
        return resp.body["Contacts"][0]

    def contacts_named(self, name: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = {"where": f"Name=={where_quote(name)}"}
        if include_archived:
            # Lookups return only ACTIVE contacts unless asked otherwise.
            query["includeArchived"] = "true"
        return self._request("GET", self._accounting("Contacts", query)).body.get("Contacts", [])

    def get_invoice(self, id_or_number: str) -> dict[str, Any] | None:
        try:
            resp = self._request("GET", self._accounting(f"Invoices/{id_or_number}"))
        except XeroNotFound:
            return None
        return resp.body["Invoices"][0]

    def invoices_numbered(self, invoice_number: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", self._accounting("Invoices", {"InvoiceNumbers": invoice_number})
        ).body.get("Invoices", [])

    # -- Projects -------------------------------------------------------------

    def create_project(
        self, contact_id: str, name: str, *, idempotency_key: str | None
    ) -> Created:
        resp = self._request(
            "POST", self._projects(), body={"contactId": contact_id, "name": name},
            idempotency_key=idempotency_key,
        )
        return Created(resp.body, resp.headers.get(REPLAY_HEADER) == "true")

    def find_projects(self, contact_id: str, name: str) -> list[dict[str, Any]]:
        items = self._request("GET", self._projects(query={"contactId": contact_id})).body.get(
            "items", []
        )
        return [p for p in items if p.get("name") == name]
