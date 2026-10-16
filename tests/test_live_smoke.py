"""The live smoke script is safe to run anywhere: with no credentials it does nothing."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_without_credentials_the_live_smoke_skips_and_exits_zero(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("XERO_")}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "live_smoke.py")],
        cwd=str(tmp_path),  # so a skipped run never touches a live_smoke.db
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("live smoke skipped: XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_REFRESH_TOKEN not set")
    assert not (tmp_path / "live_smoke.db").exists()
