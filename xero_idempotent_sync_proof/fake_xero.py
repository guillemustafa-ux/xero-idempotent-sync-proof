"""A Xero stand-in on a real socket, encoding the documented behaviours.

This is not a generic mock. It models the specific things Xero does that
decide whether an integration is exactly-once or merely usually-once:

* **``Idempotency-Key``** on POST/PUT/PATCH: any string up to 128 characters,
  remembered for **six minutes** from the first call, after which the same
  key "is processed as a new key". A replay returns the cached response --
  **errors included** -- and still counts against the rate limit. The same
  key with a different method, URL or body is a ``400``. Scope is per app.
  The clock is injectable, so a test can watch the window close.
* **Contact ``Name`` unique across active contacts** and **``ContactNumber``
  unique** (API-only, max 50, readable with ``GET /Contacts/{ContactNumber}``),
  both answered with ``400``. The Name message is verbatim from the official
  OpenAPI examples; the ContactNumber message is **not documented** (the docs
  say only that the PUT "will receive an error") and is switchable, so the
  sync can be shown not to depend on its wording. ``where=Name=="..."`` is
  case-insensitive; ``includeArchived=true`` shows archived rows.
* **ACCREC ``InvoiceNumber`` unique** ("Invoice # must be unique.");
  payments only on an ``AUTHORISED`` invoice, never above ``AmountDue``, and
  the payment ``Reference`` is **not** unique -- the invoice's ``Payments[]``
  is the only place to count them.
* **Projects API** behind two gates that both answer ``403``: the token
  lacking the ``projects`` scope, and the organisation's plan not including
  Projects (switchable). The list filters on ``contactID`` and is paged
  (``pageSize`` 1..500, default 50, ``pagination.pageCount`` in the body).
* **OAuth refresh rotation**: every refresh returns a new refresh token; the
  previous one keeps working for a **30-minute grace period**, then
  ``invalid_grant``. A refresh token not used for **60 days** is
  ``invalid_grant`` too. Access tokens live 30 minutes. The token response
  echoes the granted ``scope``.
* **Invoice statuses**: ``DRAFT``/``SUBMITTED``/``AUTHORISED`` on create;
  an ``AUTHORISED`` invoice never goes back to ``DRAFT`` or ``SUBMITTED``;
  ``PAID`` and ``VOIDED`` are final.
* **Rate limiting**: ``429`` with ``X-Rate-Limit-Problem`` and
  ``Retry-After``; ``X-MinLimit-Remaining`` / ``X-DayLimit-Remaining`` on
  every normal response. Armed per test (``throttle_next``); the 60/min and
  5-concurrent limits themselves are **not enforced** by counting.
* **``xero-tenant-id``** required on every API call; ``GET /connections``
  lists the tenant; ``GET /Organisation`` says ``IsDemoCompany: true``.

Faults are armed per request and cut the connection at the exact seam where
the outcome becomes unknowable to the caller.

Where this file goes beyond the documentation it says so inline and errs on
the side of the behaviour that makes the client's job *harder*. Assumptions,
not verified against Xero:

* an **archived** contact keeps its ``Name`` reserved (the message says
  "active contacts"; the harder reading is modelled);
* the wording of the duplicate-``ContactNumber`` 400 (default: the Name
  message's shape; see ``answer_number_collisions_with``);
* ``GET /Contacts/{ContactNumber}`` for a number nobody carries answers
  **404** (the docs say the number is a valid path key, not what a miss
  returns);
* ``where=Name=="..."`` is modelled **case-insensitive only**; the docs say
  the optimised filters are case- *and accent*-insensitive, and the accent
  part is not modelled;
* a quote inside a ``where`` string literal is escaped as ``\\"``;
* **project names are not unique** (a duplicate is simply a second project);
  the docs do not state uniqueness either way, and this is the harder side;
* a 403 from ``projects.xro`` with the ``projects`` scope granted means the
  **plan** lacks Projects; the two 403s differ only in a body the client does
  not rely on;
* ``Idempotency-Key`` is honoured on the Projects API as on Accounting;
* inside the grace period, a refresh with the **retired** token rotates
  again and retires the *newest* token (the docs say the old one "can be
  used" for 30 minutes; what happens to the new one is not stated);
* the exact bodies of the 401, 403 and 429 responses (a JSON problem shape
  here) and of the **404, served as plain text** (``The resource you're
  looking for cannot be found``, the body Xero commonly sends); the client
  branches on the status code for all four and reads a body only on a 400;
* the message for a payment on a non-``AUTHORISED`` invoice and for a
  ``ContactNumber`` over 50 characters (the names are not in the examples);
* a replay returns the original **status code** as well as the body.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, urlparse

from .ledger import IDEMPOTENCY_TTL_SECONDS  # noqa: F401 -- re-exported; one definition

# Credentials the fake token endpoint accepts (Basic auth on the refresh call).
CLIENT_ID = "proof-client-id"
CLIENT_SECRET = "proof-client-secret"
TENANT_ID = "7d1a6f7e-0f1c-4a7a-9d7e-2f4e3b1c9a10"
TENANT_NAME = "Demo Company (proof)"
SCOPES = "offline_access accounting.contacts accounting.invoices accounting.payments projects"

# The one bank account payments can land in, and the one sales account.
BANK_ACCOUNT_CODE = "090"
SALES_ACCOUNT_CODE = "200"

# Documented limits and lifetimes, in seconds (the idempotency TTL is
# imported from the ledger so there is exactly one definition).
IDEMPOTENCY_KEY_MAX_LEN = 128
CONTACT_NUMBER_MAX_LEN = 50
ACCESS_TOKEN_TTL_SECONDS = 30 * 60
REFRESH_TOKEN_TTL_SECONDS = 60 * 86400
REFRESH_GRACE_SECONDS = 30 * 60
MINUTE_LIMIT = 60
DAY_LIMIT = 1000
APP_MINUTE_LIMIT = 10000
# Projects API paging, from the OpenAPI spec: pageSize 1..500, default 50.
PROJECTS_DEFAULT_PAGE_SIZE = 50
PROJECTS_MAX_PAGE_SIZE = 500

# Routes a fault can be armed on.
ROUTE_CONTACT = "contact"
ROUTE_INVOICE = "invoice"
ROUTE_PAYMENT = "payment"
ROUTE_PROJECT = "project"

FAULT_BEFORE_COMMIT = "before_commit"  # nothing was created; safe to retry
FAULT_AFTER_COMMIT = "after_commit"  # created, but the caller never hears so

INVOICE_STATUSES_ON_CREATE = ("DRAFT", "SUBMITTED", "AUTHORISED")

# The body of an Accounting 404: plain text, no JSON (an assumption on the
# harder side; see ``_Handler._send_reply``).
NOT_FOUND_TEXT = "The resource you're looking for cannot be found"

# where=Name=="..." with \" and \\ escapes inside the literal.
_WHERE_NAME_RE = re.compile(r'^\s*Name\s*==\s*"((?:[^"\\]|\\.)*)"\s*$')


def _dotnet_date(now: float) -> str:
    return f"/Date({int(now * 1000)}+0000)/"


def _dotnet_date_from_iso(value: Any) -> Any:
    """Render a ``YYYY-MM-DD`` the caller sent the way Xero's JSON renders dates.

    The official invoice examples show every date as ``/Date(ms+0000)/``.
    Anything that is not an ISO date is echoed as given.
    """
    try:
        day = datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return value
    midnight = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    return _dotnet_date(midnight.timestamp())


def _fingerprint(method: str, path: str, body: bytes) -> str:
    return hashlib.sha256(f"{method} {path}\n".encode() + body).hexdigest()


class _Reply(NamedTuple):
    """What a route handler decided, before the dispatcher sends (or does not)."""

    status: int
    payload: Any
    cut_before: bool = False  # drop the wire; nothing was committed or cached
    cut_after: bool = False  # committed and cached; drop the wire before answering


@dataclass
class _IdempotencyRecord:
    fingerprint: str
    stored_at: float
    status: int
    payload: Any


@dataclass
class XeroState:
    """The service's own storage, plus its configuration."""

    projects_available: bool = False
    clock: Callable[[], float] = time.time
    offset: float = 0.0

    # The bank accounts payments may land in. One by default; removable, so a
    # payment can be a real validation 400 whose cause later goes away.
    bank_account_codes: set[str] = field(default_factory=lambda: {BANK_ACCOUNT_CODE})
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    invoices: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (client_id, key) -> what the first call produced. Scope is per app.
    idempotency: dict[tuple[str, str], _IdempotencyRecord] = field(default_factory=dict)
    # OAuth.
    access_tokens: dict[str, tuple[str, float]] = field(default_factory=dict)  # token -> (client, exp)
    refresh_token: str = field(default_factory=lambda: "rt-" + secrets.token_hex(12))
    # When the current refresh token was issued; None until the fake starts.
    # Documented: a refresh token lives 60 days.
    refresh_token_issued_at: float | None = None
    retired_refresh_tokens: dict[str, float] = field(default_factory=dict)  # token -> retired_at
    # What the authorisation granted; echoed in every token response.
    granted_scopes: str = SCOPES
    # Faults armed for the next matching request, popped as they fire.
    armed: list[tuple[str, str]] = field(default_factory=list)
    throttle_remaining: int = 0
    retry_after: int = 3
    # Armed 5xx answers: (status, how many), served before any route runs.
    server_error_status: int = 503
    server_error_remaining: int = 0
    # None -> the default wording (an assumption; the docs give none).
    number_collision_message: str | None = None
    minute_remaining: int = MINUTE_LIMIT
    day_remaining: int = DAY_LIMIT

    request_log: list[str] = field(default_factory=list)
    _invoice_seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # -- time ---------------------------------------------------------------

    def now(self) -> float:
        return self.clock() + self.offset

    # -- faults -------------------------------------------------------------

    def arm(self, route: str, fault: str) -> None:
        self.armed.append((route, fault))

    def take_fault(self, route: str) -> str | None:
        for i, (target, fault) in enumerate(self.armed):
            if target == route:
                self.armed.pop(i)
                return fault
        return None

    # -- contacts -------------------------------------------------------------

    def contact_by_name(self, name: str, *, include_archived: bool) -> list[dict[str, Any]]:
        wanted = name.casefold()
        return [
            c
            for c in self.contacts.values()
            if c["Name"].casefold() == wanted
            and (include_archived or c["ContactStatus"] == "ACTIVE")
        ]

    def name_taken(self, name: str) -> bool:
        # Assumption (harder side): archived contacts keep the name reserved.
        return bool(self.contact_by_name(name, include_archived=True))

    def contact_by_number(self, number: str) -> dict[str, Any] | None:
        for c in self.contacts.values():
            if c.get("ContactNumber") == number:
                return c
        return None

    def add_contact(self, name: str, contact_number: str | None) -> dict[str, Any]:
        contact = {
            "ContactID": str(uuid.uuid4()),
            "ContactStatus": "ACTIVE",
            "Name": name,
            "IsSupplier": False,
            "IsCustomer": False,
            "UpdatedDateUTC": _dotnet_date(self.now()),
            "HasValidationErrors": False,
        }
        if contact_number is not None:
            contact["ContactNumber"] = contact_number
        self.contacts[contact["ContactID"]] = contact
        return contact

    # -- invoices -------------------------------------------------------------

    def invoice_by_number(self, number: str) -> dict[str, Any] | None:
        for inv in self.invoices.values():
            if inv["InvoiceNumber"] == number:
                return inv
        return None

    def next_invoice_number(self) -> str:
        self._invoice_seq += 1
        return f"INV-{self._invoice_seq:04d}"

    # -- projects -------------------------------------------------------------

    def add_project(self, contact_id: str, name: str) -> dict[str, Any]:
        project = {
            "projectId": str(uuid.uuid4()),
            "contactId": contact_id,
            "name": name,
            "currencyCode": "USD",
            "minutesLogged": 0,
            "status": "INPROGRESS",
        }
        self.projects[project["projectId"]] = project
        return project

    def projects_under(self, contact_id: str | None, name: str | None = None) -> list[dict[str, Any]]:
        return [
            p
            for p in self.projects.values()
            if (contact_id is None or p["contactId"] == contact_id)
            and (name is None or p["name"] == name)
        ]

    # -- OAuth ----------------------------------------------------------------

    def rotate(self) -> str:
        """Retire the current refresh token and issue a new one, good for another 60 days."""
        self.retired_refresh_tokens.setdefault(self.refresh_token, self.now())
        self.refresh_token = "rt-" + secrets.token_hex(12)
        self.refresh_token_issued_at = self.now()
        return self.refresh_token

    def refresh_token_expired(self) -> bool:
        issued = self.refresh_token_issued_at
        return issued is not None and self.now() - issued >= REFRESH_TOKEN_TTL_SECONDS

    def issue_access_token(self, client_id: str) -> str:
        token = "at-" + secrets.token_hex(12)
        self.access_tokens[token] = (client_id, self.now() + ACCESS_TOKEN_TTL_SECONDS)
        return token


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> XeroState:
        return self.server.state  # type: ignore[attr-defined]

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr
        pass

    def _read_body(self) -> bytes:
        # Read once and cache. The request body must be consumed before any
        # early reply (401, 403, 429): closing a socket with unread request
        # bytes makes the OS answer with RST, and on Windows the client then
        # sees ConnectionAbortedError instead of the status we sent.
        if not hasattr(self, "_body_cache"):
            length = int(self.headers.get("Content-Length") or 0)
            self._body_cache = self.rfile.read(length) if length else b""
        return self._body_cache

    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        self._send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", headers)

    def _send_reply(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        """A route's reply, as Xero would shape it: JSON -- except the 404, which is plain text.

        Assumption, modelled on the harder side: Xero's Accounting 404 is
        commonly a text body (``The resource you're looking for cannot be
        found``), not a JSON object. A client that reads the body's shape
        before the status code never learns the resource is absent.
        """
        if status == 404 and payload is None:
            self._send_bytes(status, NOT_FOUND_TEXT.encode("utf-8"), "text/plain; charset=utf-8", headers)
            return
        self._send_json(status, payload, headers)

    def _send_bytes(
        self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cut_the_wire(self) -> None:
        """Drop the connection with no response at all.

        The caller sees a transport error. It cannot distinguish "the request
        never landed" from "the request landed and the answer was lost" --
        which is the entire problem this repo is about.
        """
        self.close_connection = True
        try:
            # Shut the socket down rather than closing ``wfile``: the handler
            # still flushes the writer afterwards, and a closed writer would
            # raise a noisy ValueError in the server thread.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        except OSError:
            pass

    def _envelope(self, **payload: Any) -> dict[str, Any]:
        return {
            "Id": str(uuid.uuid4()),
            "Status": "OK",
            "ProviderName": "xero-idempotent-sync-proof",
            "DateTimeUTC": _dotnet_date(self.state.now()),
            **payload,
        }

    @staticmethod
    def _validation(message: str, element: dict[str, Any] | None = None) -> dict[str, Any]:
        """The documented HTTP 400 shape: an ApiException with ValidationErrors per element."""
        elements = []
        if element is not None:
            elements.append(
                {**element, "HasValidationErrors": True, "ValidationErrors": [{"Message": message}]}
            )
        return {
            "ErrorNumber": 10,
            "Type": "ValidationException",
            "Message": "A validation exception occurred" if element is not None else message,
            "Elements": elements,
        }

    @staticmethod
    def _problem(status: int, title: str, detail: str) -> dict[str, Any]:
        # Assumption: the shape of Xero's 401, 403, 405 and 429 bodies. The
        # client branches on the status code before it reads any of these;
        # the 404 does not come through here at all (it is plain text, see
        # ``_send_reply``).
        return {"Type": None, "Title": title, "Status": status, "Detail": detail,
                "Instance": str(uuid.uuid4())}

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        raw_body = self._read_body()  # drain the request before any early reply
        if method == "POST" and parsed.path == "/connect/token":
            self._token_endpoint()
            return

        state = self.state
        with state._lock:
            client_id = self._authorised()
            if client_id is None:
                return
            if parsed.path == "/connections":
                self._send_json(
                    200,
                    [
                        {
                            "id": "1cea4fdd-cdb9-468d-a63f-2757c6475b67",
                            "authEventId": "b113ffd0-8428-4a0c-a3b6-6c1d549af899",
                            "tenantId": TENANT_ID,
                            "tenantType": "ORGANISATION",
                            "tenantName": TENANT_NAME,
                            "createdDateUtc": "2026-10-15T00:00:00.0000000",
                            "updatedDateUtc": "2026-10-15T00:00:00.0000000",
                        }
                    ],
                )
                return
            if not self._tenant_ok():
                return

            key = self.headers.get("Idempotency-Key")
            mutating = method in ("POST", "PUT", "PATCH")
            if self._throttled(method, key) or self._server_errored(method):
                return
            state.minute_remaining = max(0, state.minute_remaining - 1)
            state.day_remaining = max(0, state.day_remaining - 1)
            rate_headers = {
                "X-MinLimit-Remaining": str(state.minute_remaining),
                "X-DayLimit-Remaining": str(state.day_remaining),
                "X-AppMinLimit-Remaining": str(APP_MINUTE_LIMIT - 1),
            }

            slot: tuple[str, str] | None = None
            fingerprint = ""
            if mutating and key is not None:
                if len(key) > IDEMPOTENCY_KEY_MAX_LEN:
                    state.request_log.append(f"400 key too long {method} {parsed.path}")
                    self._send_json(
                        400,
                        self._validation(
                            f"Idempotency Key: {key} with length {len(key)} is too long."
                        ),
                        rate_headers,
                    )
                    return
                slot = (client_id, key)
                fingerprint = _fingerprint(method, self.path, raw_body)
                record = state.idempotency.get(slot)
                if record is not None and state.now() - record.stored_at >= IDEMPOTENCY_TTL_SECONDS:
                    # The window closed: "processed as a new key".
                    del state.idempotency[slot]
                    record = None
                if record is not None:
                    if record.fingerprint != fingerprint:
                        state.request_log.append(f"400 key reused {method} {parsed.path} key={key}")
                        self._send_json(
                            400,
                            self._validation(
                                f"Idempotency Key: {key} is used with a different request."
                            ),
                            rate_headers,
                        )
                        return
                    # Replay. Note what a faithful implementation does NOT do:
                    # it does not run the operation again. The original
                    # response -- a 400 included -- is returned verbatim.
                    state.request_log.append(f"REPLAY {method} {parsed.path} key={key}")
                    self._send_reply(
                        record.status, record.payload, {**rate_headers, "X-Fake-Replayed": "true"}
                    )
                    return

            state.request_log.append(f"{method} {parsed.path} key={key or '-'}")
            reply = self._dispatch(method, parsed.path, parse_qs(parsed.query), raw_body)
            if reply.cut_before:
                self._cut_the_wire()
                return
            if slot is not None:
                state.idempotency[slot] = _IdempotencyRecord(
                    fingerprint, state.now(), reply.status, reply.payload
                )
            if reply.cut_after:
                # Committed and remembered on this side; the caller never hears.
                self._cut_the_wire()
                return
            self._send_reply(reply.status, reply.payload, rate_headers)

    def _authorised(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        entry = self.state.access_tokens.get(token)
        if entry is not None and entry[1] > self.state.now():
            return entry[0]
        self.state.request_log.append(f"401 {self.command} {urlparse(self.path).path}")
        self._send_json(401, self._problem(401, "Unauthorized", "AuthenticationUnsuccessful"))
        return None

    def _tenant_ok(self) -> bool:
        if self.headers.get("xero-tenant-id") == TENANT_ID:
            return True
        self.state.request_log.append(f"403 tenant {self.command} {urlparse(self.path).path}")
        self._send_json(
            403,
            self._problem(
                403,
                "Forbidden",
                "AuthenticationUnsuccessful: the xero-tenant-id header is missing or "
                "names a tenant this app is not connected to",
            ),
        )
        return False

    def _throttled(self, method: str, key: str | None) -> bool:
        state = self.state
        if state.throttle_remaining <= 0:
            return False
        state.throttle_remaining -= 1
        state.request_log.append(f"429 {method} {urlparse(self.path).path} key={key or '-'}")
        self._send_json(
            429,
            self._problem(429, "Too Many Requests", "Rate limit exceeded: minute"),
            {
                "X-Rate-Limit-Problem": "minute",
                "Retry-After": str(state.retry_after),
                "X-MinLimit-Remaining": "0",
                "X-DayLimit-Remaining": str(state.day_remaining),
            },
        )
        return True

    def _server_errored(self, method: str) -> bool:
        """An armed 5xx: the way a gateway answers, HTML body and all, before any route runs.

        Nothing is committed and nothing is cached under the key. The client
        cannot know that: a 5xx says nothing about the write.
        """
        state = self.state
        if state.server_error_remaining <= 0:
            return False
        state.server_error_remaining -= 1
        status = state.server_error_status
        path = urlparse(self.path).path
        state.request_log.append(f"{status} {method} {path}")
        body = f"<html><body><h1>{status}</h1></body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _dispatch(self, method: str, path: str, query: dict[str, list[str]], raw: bytes) -> _Reply:
        body: Any = {}
        if raw:
            try:
                body = json.loads(raw)
            except ValueError:
                return _Reply(400, self._validation("The request body is not valid JSON"))

        lower = path.lower()
        if lower.startswith("/projects.xro/2.0/projects"):
            return self._projects(method, path, query, body)
        m = re.match(r"^/api\.xro/2\.0/(Organisation|Contacts|Invoices|Payments)(?:/([^/]+))?$", path)
        if not m:
            return _Reply(404, None)
        resource, ident = m.group(1), m.group(2)
        if resource == "Organisation" and method == "GET":
            return _Reply(200, self._envelope(Organisations=[self._organisation()]))
        if resource == "Contacts":
            return self._contacts(method, ident, query, body)
        if resource == "Invoices":
            return self._invoices(method, ident, query, body)
        if resource == "Payments":
            return self._payments(method, ident, body)
        return _Reply(405, self._problem(405, "Method Not Allowed", f"{method} {path}"))

    def _organisation(self) -> dict[str, Any]:
        return {
            "OrganisationID": TENANT_ID,
            "Name": TENANT_NAME,
            "LegalName": TENANT_NAME,
            "OrganisationType": "COMPANY",
            "BaseCurrency": "USD",
            "CountryCode": "US",
            "IsDemoCompany": True,
            "OrganisationStatus": "ACTIVE",
            "Edition": "BUSINESS",
            "Class": "STARTER",
            "ShortCode": "!proof",
        }

    @staticmethod
    def _one(body: Any, collection: str) -> dict[str, Any] | None:
        """One entity per request is modelled: ``{"Contacts": [c]}`` or the bare object."""
        items = body.get(collection) if isinstance(body, dict) and collection in body else [body]
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return None
        return items[0]

    # -- OAuth ------------------------------------------------------------

    def _token_endpoint(self) -> None:
        state = self.state
        form = parse_qs(self._read_body().decode("utf-8"))
        auth = self.headers.get("Authorization") or ""
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        with state._lock:
            state.request_log.append("POST /connect/token")
            if auth != f"Basic {expected}":
                self._send_json(401, {"error": "invalid_client"})
                return
            if (form.get("grant_type") or [""])[0] != "refresh_token":
                self._send_json(400, {"error": "unsupported_grant_type"})
                return
            presented = (form.get("refresh_token") or [""])[0]
            now = state.now()
            if presented == state.refresh_token and state.refresh_token_expired():
                # Sixty days without a refresh: only a human gets the app back in.
                self._send_json(
                    400,
                    {"error": "invalid_grant", "error_description": "refresh token expired"},
                )
                return
            if presented == state.refresh_token:
                pass
            elif presented in state.retired_refresh_tokens and (
                now - state.retired_refresh_tokens[presented] < REFRESH_GRACE_SECONDS
            ):
                # "you can retry using your existing refresh token for a grace
                # period of 30 minutes". The retry rotates again; the grace of
                # the token presented is not extended.
                pass
            else:
                reason = "expired" if presented in state.retired_refresh_tokens else "unknown"
                self._send_json(
                    400,
                    {"error": "invalid_grant", "error_description": f"refresh token {reason}"},
                )
                return
            new_refresh = state.rotate()
            access = state.issue_access_token(CLIENT_ID)
            self._send_json(
                200,
                {
                    "access_token": access,
                    "expires_in": ACCESS_TOKEN_TTL_SECONDS,
                    "token_type": "Bearer",
                    "refresh_token": new_refresh,
                    "scope": state.granted_scopes,
                },
            )

    # -- contacts -----------------------------------------------------------

    def _contacts(self, method: str, ident: str | None, query: dict, body: Any) -> _Reply:
        state = self.state
        if method == "GET":
            if ident is not None:
                contact = state.contacts.get(ident) or state.contact_by_number(ident)
                if contact is None:
                    return _Reply(404, None)
                return _Reply(200, self._envelope(Contacts=[contact]))
            include_archived = (query.get("includeArchived") or ["false"])[0].lower() == "true"
            where = (query.get("where") or [None])[0]
            if where is None:
                rows = [
                    c for c in state.contacts.values()
                    if include_archived or c["ContactStatus"] == "ACTIVE"
                ]
                return _Reply(200, self._envelope(Contacts=rows))
            m = _WHERE_NAME_RE.match(where)
            if not m:
                return _Reply(400, self._validation(f"Invalid where clause: {where}"))
            wanted = re.sub(r'\\(["\\])', r"\1", m.group(1))
            rows = state.contact_by_name(wanted, include_archived=include_archived)
            return _Reply(200, self._envelope(Contacts=rows))

        if ident is not None:
            return _Reply(405, self._problem(405, "Not Modelled", "contact updates"))
        item = self._one(body, "Contacts")
        if item is None:
            return _Reply(400, self._validation("Exactly one Contact per request is modelled"))
        name = str(item.get("Name") or "")
        number = item.get("ContactNumber")
        if not name:
            return _Reply(400, self._validation("Name is required", item))
        if number is not None and len(str(number)) > CONTACT_NUMBER_MAX_LEN:
            # Assumption: the exact message is not in the examples.
            return _Reply(400, self._validation(
                f"The contact number must be {CONTACT_NUMBER_MAX_LEN} characters or less.", item))
        if state.name_taken(name):
            # Definite: nothing was created. Cached under the key all the same.
            return _Reply(400, self._validation(
                f"The contact name {name} is already assigned to another contact. "
                "The contact name must be unique across all active contacts.", item))
        if number is not None and state.contact_by_number(str(number)) is not None:
            # Assumption, not verified against Xero: the docs say only that
            # the PUT "will receive an error". A client must not key on this.
            message = state.number_collision_message or (
                f"The contact number {number} is already assigned to another contact."
            )
            return _Reply(400, self._validation(message, item))

        fault = state.take_fault(ROUTE_CONTACT)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)
        created = state.add_contact(name, str(number) if number is not None else None)
        return _Reply(200, self._envelope(Contacts=[created]), cut_after=fault == FAULT_AFTER_COMMIT)

    # -- invoices -----------------------------------------------------------

    def _invoices(self, method: str, ident: str | None, query: dict, body: Any) -> _Reply:
        state = self.state
        if method == "GET":
            if ident is not None:
                inv = state.invoices.get(ident) or state.invoice_by_number(ident)
                if inv is None:
                    return _Reply(404, None)
                return _Reply(200, self._envelope(Invoices=[inv]))
            rows = list(state.invoices.values())
            if "InvoiceNumbers" in query:
                wanted = set(query["InvoiceNumbers"][0].split(","))
                rows = [i for i in rows if i["InvoiceNumber"] in wanted]
            if "Statuses" in query:
                wanted = set(query["Statuses"][0].split(","))
                rows = [i for i in rows if i["Status"] in wanted]
            if "ContactIDs" in query:
                wanted = set(query["ContactIDs"][0].split(","))
                rows = [i for i in rows if i["Contact"]["ContactID"] in wanted]
            return _Reply(200, self._envelope(Invoices=rows))

        item = self._one(body, "Invoices")
        if item is None:
            return _Reply(400, self._validation("Exactly one Invoice per request is modelled"))

        if ident is not None:
            # POST /Invoices/{id}: only a status change is modelled.
            inv = state.invoices.get(ident) or state.invoice_by_number(ident)
            if inv is None:
                return _Reply(404, None)
            status = item.get("Status", inv["Status"])
            # Documented: DRAFT -> SUBMITTED/AUTHORISED/DELETED, AUTHORISED ->
            # VOIDED. An AUTHORISED invoice never goes back to DRAFT or
            # SUBMITTED; PAID and VOIDED are final.
            backwards = inv["Status"] == "AUTHORISED" and status in ("DRAFT", "SUBMITTED")
            if status not in INVOICE_STATUSES_ON_CREATE or inv["Status"] in ("PAID", "VOIDED") or backwards:
                return _Reply(400, self._validation(
                    f"Invoice status cannot be changed from {inv['Status']} to {status}", item))
            inv["Status"] = status
            inv["UpdatedDateUTC"] = _dotnet_date(state.now())
            return _Reply(200, self._envelope(Invoices=[inv]))

        if item.get("Type") != "ACCREC":
            return _Reply(400, self._validation("Invoice not of valid type for creation", item))
        contact_id = str((item.get("Contact") or {}).get("ContactID") or "")
        contact = state.contacts.get(contact_id)
        if contact is None:
            return _Reply(400, self._validation("The specified contact could not be found", item))
        status = item.get("Status", "DRAFT")
        if status not in INVOICE_STATUSES_ON_CREATE:
            return _Reply(400, self._validation(f"Invalid status for creation: {status}", item))
        number = item.get("InvoiceNumber")
        if number is not None and state.invoice_by_number(str(number)) is not None:
            return _Reply(400, self._validation("Invoice # must be unique.", item))
        lines = []
        total = 0.0
        for line in item.get("LineItems") or []:
            amount = round(float(line.get("Quantity", 1)) * float(line.get("UnitAmount", 0)), 2)
            total = round(total + amount, 2)
            lines.append({
                "LineItemID": str(uuid.uuid4()),
                "Description": line.get("Description", ""),
                "Quantity": float(line.get("Quantity", 1)),
                "UnitAmount": float(line.get("UnitAmount", 0)),
                "AccountCode": str(line.get("AccountCode", SALES_ACCOUNT_CODE)),
                "TaxType": "NONE",
                "TaxAmount": 0.0,
                "LineAmount": amount,
            })

        fault = state.take_fault(ROUTE_INVOICE)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)
        created = {
            "InvoiceID": str(uuid.uuid4()),
            "Type": "ACCREC",
            "InvoiceNumber": str(number) if number is not None else state.next_invoice_number(),
            "Reference": item.get("Reference", ""),
            "Status": status,
            "Contact": {"ContactID": contact_id, "Name": contact["Name"]},
            "Date": item.get("Date", ""),
            "DueDate": item.get("DueDate", ""),
            "LineAmountTypes": "NoTax",
            "LineItems": lines,
            "SubTotal": total,
            "TotalTax": 0.0,
            "Total": total,
            "AmountDue": total,
            "AmountPaid": 0.0,
            "CurrencyCode": "USD",
            "Payments": [],
            "UpdatedDateUTC": _dotnet_date(state.now()),
            "HasValidationErrors": False,
        }
        state.invoices[created["InvoiceID"]] = created
        return _Reply(200, self._envelope(Invoices=[created]), cut_after=fault == FAULT_AFTER_COMMIT)

    # -- payments -----------------------------------------------------------

    def _payments(self, method: str, ident: str | None, body: Any) -> _Reply:
        state = self.state
        if method == "GET":
            if ident is None:
                return _Reply(200, self._envelope(Payments=list(state.payments.values())))
            payment = state.payments.get(ident)
            if payment is None:
                return _Reply(404, None)
            return _Reply(200, self._envelope(Payments=[payment]))
        if ident is not None:
            return _Reply(405, self._problem(405, "Not Modelled", "payment updates"))

        item = self._one(body, "Payments")
        if item is None:
            return _Reply(400, self._validation("Exactly one Payment per request is modelled"))
        ref = item.get("Invoice") or {}
        invoice = state.invoices.get(str(ref.get("InvoiceID") or ""))
        if invoice is None and ref.get("InvoiceNumber"):
            invoice = state.invoice_by_number(str(ref["InvoiceNumber"]))
        if invoice is None:
            return _Reply(400, self._validation("The specified invoice could not be found", item))
        account = item.get("Account") or {}
        code = str(account.get("Code") or account.get("AccountID") or "")
        if code not in state.bank_account_codes:
            return _Reply(400, self._validation("The specified account could not be found", item))
        if invoice["Status"] != "AUTHORISED":
            # Assumption: the exact message is not in the examples.
            return _Reply(400, self._validation(
                f"Payments can only be made against AUTHORISED documents; this invoice is "
                f"{invoice['Status']}", item))
        amount = round(float(item.get("Amount", 0)), 2)
        if amount <= 0:
            return _Reply(400, self._validation("Payment amount must be greater than zero", item))
        if amount > invoice["AmountDue"] + 1e-9:
            return _Reply(400, self._validation(
                "Payment amount exceeds the amount outstanding on this document", item))

        fault = state.take_fault(ROUTE_PAYMENT)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)
        payment = {
            "PaymentID": str(uuid.uuid4()),
            "PaymentType": "ACCRECPAYMENT",
            "Status": "AUTHORISED",
            "Date": _dotnet_date_from_iso(item.get("Date", "")),
            "Amount": amount,
            "Reference": item.get("Reference", ""),
            "IsReconciled": False,
            "Invoice": {"InvoiceID": invoice["InvoiceID"], "InvoiceNumber": invoice["InvoiceNumber"]},
            "Account": {"Code": code},
            "UpdatedDateUTC": _dotnet_date(state.now()),
            "HasValidationErrors": False,
        }
        state.payments[payment["PaymentID"]] = payment
        invoice["AmountPaid"] = round(invoice["AmountPaid"] + amount, 2)
        invoice["AmountDue"] = round(invoice["Total"] - invoice["AmountPaid"], 2)
        invoice["Payments"].append({
            "PaymentID": payment["PaymentID"],
            "Date": payment["Date"],
            "Amount": amount,
            "Reference": payment["Reference"],
            "HasAccount": True,
        })
        if invoice["AmountDue"] == 0:
            invoice["Status"] = "PAID"
        invoice["UpdatedDateUTC"] = _dotnet_date(state.now())
        return _Reply(200, self._envelope(Payments=[payment]), cut_after=fault == FAULT_AFTER_COMMIT)

    # -- projects -----------------------------------------------------------

    def _projects(self, method: str, path: str, query: dict, body: Any) -> _Reply:
        state = self.state
        if "projects" not in state.granted_scopes.split():
            # A token without the scope is 403 too -- and the client cannot
            # tell the two 403s apart by status. Assumption: the body shapes.
            state.request_log.append(f"{method} {path} -> 403 (scope)")
            return _Reply(403, self._problem(
                403, "Forbidden", "AuthorizationUnsuccessful: the token lacks the projects scope"))
        if not state.projects_available:
            state.request_log.append(f"{method} {path} -> 403 (plan)")
            return _Reply(403, self._problem(
                403, "Forbidden",
                "The connected organisation's Xero plan does not include Projects."))
        m = re.match(r"^/projects\.xro/2\.0/[Pp]rojects(?:/([^/]+))?$", path)
        ident = m.group(1) if m else None

        if method == "GET":
            if ident is not None:
                project = state.projects.get(ident)
                if project is None:
                    return _Reply(404, {"message": f"project {ident} not found"})
                return _Reply(200, project)
            # Documented query parameters: ``contactID`` (capital ID; the
            # response body spells it ``contactId``), ``states``, ``page``
            # (default 1) and ``pageSize`` (1..500, default 50). Anything
            # else -- ``contactId`` included -- is not a filter.
            contact_id = (query.get("contactID") or [None])[0]
            items = state.projects_under(contact_id)
            if "states" in query:
                wanted = set(query["states"][0].split(","))
                items = [p for p in items if p["status"] in wanted]
            try:
                page = int((query.get("page") or ["1"])[0])
                page_size = int((query.get("pageSize") or [str(PROJECTS_DEFAULT_PAGE_SIZE)])[0])
            except ValueError:
                return _Reply(400, {"message": "page and pageSize must be integers"})
            if page < 1 or not 1 <= page_size <= PROJECTS_MAX_PAGE_SIZE:
                return _Reply(400, {
                    "message": f"pageSize must be between 1 and {PROJECTS_MAX_PAGE_SIZE}; "
                    "page must be greater than 0"})
            page_count = max(1, -(-len(items) // page_size))
            start = (page - 1) * page_size
            return _Reply(200, {
                "pagination": {
                    "page": page, "pageSize": page_size, "pageCount": page_count,
                    "itemCount": len(items),
                },
                "items": items[start:start + page_size],
            })
        if method != "POST" or ident is not None:
            return _Reply(405, {"message": "only POST /Projects and GET are modelled"})

        contact_id = str(body.get("contactId") or "") if isinstance(body, dict) else ""
        name = str(body.get("name") or "") if isinstance(body, dict) else ""
        if contact_id not in state.contacts or not name:
            # Assumption: the Projects API error shape.
            return _Reply(400, {
                "message": "The request is invalid.",
                "modelState": {
                    "contactId": [] if contact_id in state.contacts else ["contactId is not a known contact"],
                    "name": [] if name else ["name is required"],
                },
            })
        fault = state.take_fault(ROUTE_PROJECT)
        if fault == FAULT_BEFORE_COMMIT:
            return _Reply(0, None, cut_before=True)
        # Names are NOT enforced unique: a duplicate is simply a second project.
        project = state.add_project(contact_id, name)
        return _Reply(201, project, cut_after=fault == FAULT_AFTER_COMMIT)


class FakeXero:
    """A running Xero stand-in on localhost."""

    def __init__(
        self, *, projects_available: bool = False, clock: Callable[[], float] | None = None
    ):
        self.state = XeroState(projects_available=projects_available, clock=clock or time.time)
        self.state.refresh_token_issued_at = self.state.now()  # the authorisation flow just ran
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = self.state  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "FakeXero":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/connect/token"

    @property
    def tenant_id(self) -> str:
        return TENANT_ID

    @property
    def refresh_token(self) -> str:
        """The refresh token the authorisation flow would have handed over."""
        return self.state.refresh_token

    # -- the clock ----------------------------------------------------------

    def now(self) -> float:
        return self.state.now()

    def advance(self, seconds: float) -> None:
        """Move the service's clock forward: keys expire, tokens expire, grace runs out."""
        with self.state._lock:
            self.state.offset += seconds

    # -- fault injection --------------------------------------------------

    def drop_response_after_commit(self, route: str) -> None:
        self.state.arm(route, FAULT_AFTER_COMMIT)

    def drop_response_before_commit(self, route: str) -> None:
        self.state.arm(route, FAULT_BEFORE_COMMIT)

    def throttle_next(self, n: int, *, retry_after: int = 3) -> None:
        with self.state._lock:
            self.state.throttle_remaining = n
            self.state.retry_after = retry_after

    def remove_bank_account(self, code: str) -> None:
        """Payments to ``code`` are a validation 400 until ``add_bank_account`` puts it back."""
        with self.state._lock:
            self.state.bank_account_codes.discard(code)

    def add_bank_account(self, code: str) -> None:
        with self.state._lock:
            self.state.bank_account_codes.add(code)

    def fail_next_with(self, status: int, n: int = 1) -> None:
        """Answer the next ``n`` API calls with a bare 5xx (HTML body), committing nothing."""
        with self.state._lock:
            self.state.server_error_status = status
            self.state.server_error_remaining = n

    def answer_number_collisions_with(self, message: str) -> None:
        """Change the (undocumented) wording of the duplicate-ContactNumber 400."""
        with self.state._lock:
            self.state.number_collision_message = message

    def grant_scopes(self, scopes: str) -> None:
        """What the authorisation granted (space-separated), as the token response will echo it."""
        with self.state._lock:
            self.state.granted_scopes = scopes

    def set_projects_available(self, available: bool) -> None:
        with self.state._lock:
            self.state.projects_available = available

    def rotate_refresh_token_now(self) -> None:
        """Retire the current refresh token, as another instance's refresh would."""
        with self.state._lock:
            self.state.rotate()

    def revoke_access_tokens_now(self) -> None:
        """Every access token expires at once: the next API call gets a 401."""
        with self.state._lock:
            self.state.access_tokens.clear()

    def forget_keys(self) -> None:
        """Model the six-minute window closing for every key, without moving the clock."""
        with self.state._lock:
            self.state.idempotency.clear()

    def plant_contact(self, name: str, contact_number: str | None = None) -> dict[str, Any]:
        """Test-only: a contact that exists without having been created through the API."""
        with self.state._lock:
            return self.state.add_contact(name, contact_number)

    def archive_contact(self, contact_id: str) -> None:
        with self.state._lock:
            self.state.contacts[contact_id]["ContactStatus"] = "ARCHIVED"

    def plant_project(self, contact_id: str, name: str) -> dict[str, Any]:
        """Test-only: a project that exists without having been created through the API."""
        with self.state._lock:
            return self.state.add_project(contact_id, name)

    # -- introspection ----------------------------------------------------

    @property
    def contacts(self) -> dict[str, dict[str, Any]]:
        return self.state.contacts

    @property
    def invoices(self) -> dict[str, dict[str, Any]]:
        return self.state.invoices

    @property
    def payments(self) -> dict[str, dict[str, Any]]:
        return self.state.payments

    @property
    def projects(self) -> dict[str, dict[str, Any]]:
        return self.state.projects

    @property
    def idempotency_keys(self) -> dict[str, float]:
        """Live keys -> when they were stored (expired ones are swept on their next use)."""
        now = self.state.now()
        return {
            key: rec.stored_at
            for (_client, key), rec in self.state.idempotency.items()
            if now - rec.stored_at < IDEMPOTENCY_TTL_SECONDS
        }

    @property
    def request_log(self) -> list[str]:
        return self.state.request_log

    def contacts_named(self, name: str) -> list[dict[str, Any]]:
        return self.state.contact_by_name(name, include_archived=True)
