"""OAuth 2.0 against Xero, with the one rule that locks apps out when missed.

Xero's refresh token **rotates on every refresh**: each call to the token
endpoint returns a *new* refresh token and retires the one you presented.
The retired token keeps working for a **30-minute grace period** -- so that
an app which lost the response can retry -- and is then ``invalid_grant``
forever. An app that keeps using the refresh token it was first handed
works for half an hour, then needs a human to re-authorise it. The fix is
not clever: persist the refresh token that came back, every time, before
doing anything else with the response.

The access token itself lives 30 minutes. Expiry is handled both proactively
(clock) and reactively (a 401 from the API triggers one refresh and one
retry), because the two clocks are never quite the same.
"""

from __future__ import annotations

import base64
import http.client
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

XERO_TOKEN_URL = "https://identity.xero.com/connect/token"

ACCESS_TOKEN_LIFETIME = 30 * 60  # documented: 30 minutes
REFRESH_TOKEN_LIFETIME = 60 * 86400  # documented: 60 days

TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    OSError,
)


class AuthLockedOut(Exception):
    """The refresh token was rejected. Only a human re-authorisation fixes this."""


class AuthNotBootstrapped(Exception):
    """No refresh token has ever been stored. Run the authorisation flow first."""


@dataclass
class Tokens:
    access_token: str | None
    access_expires_at: float
    refresh_token: str
    refresh_expires_at: float
    # The space-separated scopes the token response said were granted; None
    # until the first refresh has been seen.
    scope: str | None = None


class TokenStore:
    """The persisted tokens, in the same SQLite file as the ledger.

    Kept in its own table and its own connection so the token lifecycle does
    not depend on which deal is being synced.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                access_token       TEXT,
                access_expires_at  REAL NOT NULL,
                refresh_token      TEXT NOT NULL,
                refresh_expires_at REAL NOT NULL,
                scope              TEXT
            )
            """
        )

    def close(self) -> None:
        self._conn.close()

    def load(self) -> Tokens | None:
        row = self._conn.execute("SELECT * FROM oauth_tokens WHERE id = 1").fetchone()
        if row is None:
            return None
        data = dict(row)
        data.pop("id")
        return Tokens(**data)

    def save(self, tokens: Tokens) -> None:
        self._conn.execute(
            "INSERT INTO oauth_tokens (id, access_token, access_expires_at, "
            "refresh_token, refresh_expires_at, scope) VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET access_token = excluded.access_token, "
            "access_expires_at = excluded.access_expires_at, "
            "refresh_token = excluded.refresh_token, "
            "refresh_expires_at = excluded.refresh_expires_at, "
            "scope = excluded.scope",
            (
                tokens.access_token,
                tokens.access_expires_at,
                tokens.refresh_token,
                tokens.refresh_expires_at,
                tokens.scope,
            ),
        )

    def bootstrap(self, refresh_token: str, refresh_expires_at: float = 0.0) -> None:
        """Seed the store with the refresh token from the authorisation flow."""
        self.save(Tokens(None, 0.0, refresh_token, refresh_expires_at))


class Authenticator:
    def __init__(
        self,
        store: TokenStore,
        *,
        token_url: str = XERO_TOKEN_URL,
        client_id: str,
        client_secret: str,
        timeout: float = 5.0,
        now: Callable[[], float] = time.time,
        skew: float = 60.0,
    ):
        self.store = store
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.now = now
        self.skew = skew

    def access_token(self) -> str:
        tokens = self.store.load()
        if tokens is None:
            raise AuthNotBootstrapped()
        if tokens.access_token is None or tokens.access_expires_at <= self.now() + self.skew:
            tokens = self.refresh()
        assert tokens.access_token is not None
        return tokens.access_token

    def granted_scopes(self) -> set[str] | None:
        """The scopes the last token response said were granted; ``None`` if never seen.

        A 403 from an API that needs a scope the app was not granted looks
        exactly like a 403 from a plan that lacks the product. This is how
        the sync tells them apart.
        """
        tokens = self.store.load()
        if tokens is None or tokens.scope is None:
            return None
        return set(tokens.scope.split())

    def refresh(self) -> Tokens:
        """Exchange the stored refresh token for a new pair, and persist BOTH.

        The response's ``refresh_token`` replaces the stored one every time.
        Xero does not merely fail to promise it is unchanged: it documents
        that it is not.
        """
        current = self.store.load()
        if current is None:
            raise AuthNotBootstrapped()
        payload = self._post_refresh(current.refresh_token)
        issued_at = self.now()
        tokens = Tokens(
            access_token=payload["access_token"],
            access_expires_at=issued_at + float(payload.get("expires_in", ACCESS_TOKEN_LIFETIME)),
            refresh_token=payload["refresh_token"],
            refresh_expires_at=issued_at + REFRESH_TOKEN_LIFETIME,
            scope=payload.get("scope") or current.scope,
        )
        self.store.save(tokens)
        return tokens

    def _post_refresh(self, refresh_token: str) -> dict:
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        ).encode()
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(self.token_url, data=body, method="POST")
        req.add_header("Authorization", f"Basic {basic}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            if exc.code in (400, 401):
                raise AuthLockedOut(f"refresh rejected (HTTP {exc.code}): {raw}") from exc
            raise
        except TRANSPORT_ERRORS as exc:
            raise ConnectionError(f"token endpoint unreachable: {exc}") from exc


# -- the anti-pattern, implemented rather than described --------------------


def refresh_but_keep_the_old_refresh_token(auth: Authenticator) -> Tokens:
    """The refresh that works for thirty minutes.

    It takes the new access token and leaves the stored refresh token as it
    was -- which is what happens when the code was written against a provider
    whose refresh tokens never rotate, or when only ``access_token`` is
    mapped into the config. Xero forgives it for the length of the grace
    period; after that the stored token is dead.
    """
    current = auth.store.load()
    if current is None:
        raise AuthNotBootstrapped()
    payload = auth._post_refresh(current.refresh_token)
    issued_at = auth.now()
    tokens = Tokens(
        access_token=payload["access_token"],
        access_expires_at=issued_at + float(payload.get("expires_in", ACCESS_TOKEN_LIFETIME)),
        refresh_token=current.refresh_token,  # the bug: the rotated one is dropped
        refresh_expires_at=current.refresh_expires_at,
    )
    auth.store.save(tokens)
    return tokens
