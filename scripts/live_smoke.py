"""One find-or-create against the Xero demo company, if credentials are present.

This is deliberately tiny. It does not run the suite against Xero and it does
not touch the Projects API. It sends one Contact create with a derived
``Idempotency-Key`` and a derived ``ContactNumber``, sends the identical
request again, and prints whether Xero handed back the same ``ContactID``.
Then it reads the contact back by its ``ContactNumber`` -- the lookup the
whole design rests on once the six-minute key window has closed.

Set these and run it::

    XERO_CLIENT_ID       the app's client id (developer.xero.com)
    XERO_CLIENT_SECRET   the app's client secret
    XERO_REFRESH_TOKEN   a refresh token from your authorisation flow, granted
                         ``offline_access accounting.contacts
                         accounting.settings.read`` (the demo-company check
                         reads ``GET /Organisation``)
    XERO_TENANT_ID       optional; resolved from GET /connections when unset,
                         provided exactly one organisation is connected
    XERO_BASE_URL        optional; default https://api.xero.com
    XERO_IDENTITY_URL    optional; default https://identity.xero.com

    python scripts/live_smoke.py

Without the credentials it prints a skip line and exits 0, so it is safe in CI.

It writes to a **demo company only**: ``GET /Organisation`` must answer
``IsDemoCompany: true``, or the script stops before creating anything. Pass
``--i-know-this-is-not-a-demo`` to override that, knowingly.

The refresh token you pass in is consumed: Xero rotates it on every refresh,
and the rotated one is persisted to ``live_smoke.db`` in the working
directory. Later runs read it from there, and the one you passed in is
``invalid_grant`` thirty minutes after the first refresh.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from xero_idempotent_sync_proof.auth import AuthLockedOut, Authenticator, TokenStore
from xero_idempotent_sync_proof.client import XERO_API_URL, UnknownOutcome, XeroClient, XeroRejected
from xero_idempotent_sync_proof.ledger import STEP_CONTACT, contact_number_for, idempotency_key_for

XERO_IDENTITY_URL = "https://identity.xero.com"
DB_PATH = "live_smoke.db"
REQUIRED = ("XERO_CLIENT_ID", "XERO_CLIENT_SECRET", "XERO_REFRESH_TOKEN")


def env(name: str, default: str = "") -> str:
    # Quotes copied into a .env file arrive as part of the value on some
    # platforms; a client id with quotes in it never authenticates.
    return (os.environ.get(name) or default).replace('"', "")


def resolve_tenant(client: XeroClient) -> str:
    """The one connected organisation's ``tenantId``; refuses to pick among several."""
    connections = [c for c in client.connections() if c.get("tenantType", "ORGANISATION") == "ORGANISATION"]
    if len(connections) != 1:
        names = [f"{c.get('tenantName')!r} ({c.get('tenantId')})" for c in connections]
        raise SystemExit(
            f"{len(connections)} organisations are connected to this app: {names}. "
            "Set XERO_TENANT_ID to the demo company's tenantId."
        )
    return connections[0]["tenantId"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--i-know-this-is-not-a-demo",
        action="store_true",
        help="create the contact even if GET /Organisation does not say IsDemoCompany: true",
    )
    args = parser.parse_args(argv)

    missing = [k for k in REQUIRED if not env(k)]
    if missing:
        print(f"live smoke skipped: {', '.join(missing)} not set")
        return 0

    store = TokenStore(DB_PATH)
    if store.load() is None:
        store.bootstrap(env("XERO_REFRESH_TOKEN"))
    else:
        print(f"using the refresh token persisted in {DB_PATH}; the one in the environment is ignored")
    identity = env("XERO_IDENTITY_URL", XERO_IDENTITY_URL).rstrip("/")
    auth = Authenticator(
        store,
        token_url=f"{identity}/connect/token",
        client_id=env("XERO_CLIENT_ID"),
        client_secret=env("XERO_CLIENT_SECRET"),
    )
    base_url = env("XERO_BASE_URL", XERO_API_URL)

    try:
        auth.refresh()
    except AuthLockedOut as exc:
        print(f"refresh rejected: {exc}")
        print("a human has to re-authorise the app; no retry policy fixes invalid_grant")
        return 1
    print(f"refreshed; the rotated refresh token is persisted in {DB_PATH} (read it back from there next time)")

    tenant_id = env("XERO_TENANT_ID") or resolve_tenant(XeroClient(base_url, "", auth))
    client = XeroClient(base_url, tenant_id, auth)

    organisation = client.organisation()
    is_demo = bool(organisation.get("IsDemoCompany"))
    print(f"organisation {organisation.get('Name')!r} (tenant {tenant_id}): IsDemoCompany={is_demo}")
    if not is_demo and not args.i_know_this_is_not_a_demo:
        print("refusing to create anything in an organisation that is not a demo company")
        print("(pass --i-know-this-is-not-a-demo if you really mean to)")
        return 2

    today = dt.date.today().isoformat()
    deal_id = f"smoke-{today}"
    name = f"xero-idempotent-sync-proof smoke {today}"
    number = contact_number_for(deal_id)
    key = idempotency_key_for(deal_id, STEP_CONTACT, "0")
    body = {"Name": name, "ContactNumber": number}

    try:
        first = client.create_contact(body, idempotency_key=key)
        second = client.create_contact(body, idempotency_key=key)
    except XeroRejected as exc:
        ours = client.get_contact(number) if exc.status == 400 else None
        if ours is not None:
            print(f"400 on {name!r}: already exists as ContactID={ours['ContactID']} carrying ContactNumber {number!r}")
            print("(a previous run today created it; the key has expired, the planted number still settles it)")
            return 0
        print(f"Xero rejected the create: HTTP {exc.status}: {'; '.join(exc.messages) or exc.body[:200]}")
        return 1
    except UnknownOutcome as exc:
        print(f"outcome unknown: {exc}")
        print(f"run again: the lookup by ContactNumber {number!r} settles it either way")
        return 1

    a, b = first.entity, second.entity
    same_id = a["ContactID"] == b["ContactID"]
    same_stamp = a.get("UpdatedDateUTC") == b.get("UpdatedDateUTC")
    print(f"first send  -> ContactID={a['ContactID']} UpdatedDateUTC={a.get('UpdatedDateUTC')}")
    print(f"second send -> ContactID={b['ContactID']} UpdatedDateUTC={b.get('UpdatedDateUTC')} (same Idempotency-Key)")
    if same_id and same_stamp:
        verdict = "CONFIRMED, one contact; the second response repeats the first, UpdatedDateUTC included"
    elif same_id:
        verdict = "one contact, but the second response differs from the first -- not a cached replay"
    else:
        verdict = "NOT honoured, two contacts"
    print(f"Idempotency-Key replay: {verdict}")
    print("(Xero sends no replay marker; identical responses are the only evidence there is)")

    read = client.get_contact(number)
    if read is None:
        print(f"GET /Contacts/{number} -> not found; the planted number cannot be read back")
        return 1
    print(f"GET /Contacts/{number} -> ContactID={read['ContactID']} Name={read['Name']!r}")
    return 0 if same_id and read["ContactID"] == a["ContactID"] else 1


if __name__ == "__main__":
    sys.exit(main())
