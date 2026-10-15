"""A worker that is killed for real, mid-sync.

Mocking a crash with ``raise`` proves less than it looks like: the stack
unwinds, ``finally`` blocks run, buffers flush, and destructors get their say.
None of that happens when a container is evicted, a deploy restarts the pod,
or someone closes the laptop.

So this runs in its own process and calls ``os._exit``, which returns to the
operating system immediately -- no unwinding, no flushing, no cleanup. The
parent process is then left with exactly what a real crash leaves: whatever
made it to disk before the kill, and a Xero organisation whose state nobody
recorded.

Run as::

    python -m xero_idempotent_sync_proof._crash_child --db X --base-url Y \\
        --tenant-id T --deal-id Z --crash-at after_contact_response
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from .auth import Authenticator, TokenStore
from .client import UnknownOutcome, XeroClient
from .fake_xero import CLIENT_ID, CLIENT_SECRET
from .ledger import Ledger
from .sync import SyncEngine, naive_contact_sync, naive_payment_sync

# The exit code the child uses for the scripted death, so the parent can
# assert the kill was ours and not an unnoticed traceback.
CRASH_EXIT_CODE = 137
# Anything else that blows up exits with this code and a traceback on stderr.
# A test that asserts 137 must never be satisfied by a typo.
UNEXPECTED_EXIT_CODE = 3


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:  # noqa: BLE001 -- reported, with a code of its own
        traceback.print_exc()
        return UNEXPECTED_EXIT_CODE


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--crash-at", required=True)
    parser.add_argument("--strategy", default="durable")
    parser.add_argument("--payment-id", default="")
    args = parser.parse_args(argv)

    ledger = Ledger(args.db)
    auth = Authenticator(
        TokenStore(args.db),
        token_url=f"{args.base_url}/connect/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )
    client = XeroClient(args.base_url, args.tenant_id, auth, sleep=lambda _: None)

    def kill_here(seam: str) -> None:
        if seam == args.crash_at:
            sys.stdout.write(f"[child] dying at seam '{seam}'\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)

    if args.strategy.startswith("naive"):
        # The naive paths have no seams to arm, so the kill is scheduled by
        # the server instead: it commits, then drops the connection, and the
        # child dies on the resulting transport error before persisting. Only
        # that transport error is the scripted death; anything else is a bug
        # in the test or the code and exits through ``main``'s handler.
        try:
            if args.strategy == "naive-contact":
                naive_contact_sync(ledger, client, args.deal_id)
            else:
                naive_payment_sync(ledger, client, args.deal_id, args.payment_id)
        except UnknownOutcome as exc:
            sys.stdout.write(f"[child] died on {type(exc).__name__}\n")
            sys.stdout.flush()
            os._exit(CRASH_EXIT_CODE)
        return 0

    SyncEngine(ledger=ledger, client=client, on_seam=kill_here).sync(args.deal_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
