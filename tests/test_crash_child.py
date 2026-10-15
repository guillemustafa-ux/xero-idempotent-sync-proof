"""The crash child itself: a scripted death and an unexpected exception must not look alike."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from xero_idempotent_sync_proof._crash_child import CRASH_EXIT_CODE, UNEXPECTED_EXIT_CODE


def test_the_fake_clock_only_moves_on_advance(xero):
    """``TTL - 1`` means exactly that: no wall-clock second can creep in between two calls."""
    before = xero.now()
    time.sleep(0.05)
    assert xero.now() == before
    xero.advance(1.5)
    assert xero.now() == before + 1.5


def test_an_unexpected_exception_in_the_child_has_its_own_exit_code(xero, db_path, auth_factory):
    """A naive run that blows up on a KeyError is not "the scripted death".

    If the child answered 137 to every exception, a test asserting the kill
    would pass on a typo. The unexpected path prints the traceback and exits
    with a code of its own.
    """
    auth_factory(xero)
    for strategy in ("naive-contact", "durable"):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "xero_idempotent_sync_proof._crash_child",
                "--db", db_path,
                "--base-url", xero.base_url,
                "--tenant-id", xero.tenant_id,
                "--deal-id", "deal-that-was-never-recorded",
                "--crash-at", "-",
                "--strategy", strategy,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert proc.returncode == UNEXPECTED_EXIT_CODE != CRASH_EXIT_CODE, (strategy, proc.stdout)
        assert "KeyError" in proc.stderr, strategy
