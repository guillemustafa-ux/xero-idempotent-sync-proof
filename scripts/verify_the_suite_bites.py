"""Break the implementation on purpose, and check the suite notices.

A green suite proves nothing on its own -- tests that assert only what the
code already does are green forever, including after someone deletes the part
that mattered. So this script reintroduces, one at a time, the seven defects
this design exists to prevent, and reports which tests catch each one.

Every mutation is applied to a real source file, the suite is run against it,
and the file is restored afterwards (even if the run is interrupted).

    python scripts/verify_the_suite_bites.py
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "xero_idempotent_sync_proof"


@dataclass
class Mutation:
    name: str
    defect: str
    path: Path
    old: str
    new: str


MUTATIONS = [
    Mutation(
        name="random-idempotency-key",
        defect="Generate the Idempotency-Key at call time instead of deriving it "
        "from the deal and the step, so a process with no memory of the first "
        "attempt -- or a second replica -- sends a key Xero has never seen.",
        path=PKG / "ledger.py",
        old='    material = f"{deal_id}:{step}" + (f":{ref}" if ref else "")\n'
        "    digest = hashlib.sha256(material.encode()).hexdigest()\n"
        '    return f"xis-{digest[:40]}"',
        new="    import uuid\n\n"
        '    return f"xis-{uuid.uuid4().hex[:40]}"',
    ),
    Mutation(
        name="no-durable-intent",
        defect="Send the step without recording the intent first, so a process "
        "killed mid-call wakes up believing nothing was ever tried.",
        path=PKG / "ledger.py",
        old="        self._conn.execute(\n"
        '            "INSERT OR IGNORE INTO intents (deal_id, step, ref, idempotency_key, mode, state) "\n'
        '            "VALUES (?, ?, ?, ?, ?, ?)",\n'
        "            (deal_id, step, ref, key, mode, PENDING),\n"
        "        )\n"
        "        intent = self.get_intent(deal_id, step, ref)\n"
        "        assert intent is not None\n"
        "        return intent",
        new="        return Intent(deal_id, step, ref, key, mode, PENDING, None)",
    ),
    Mutation(
        name="blind-retry-on-doubt",
        defect="Resolve an unknown project outcome by assuming it never landed -- "
        "the instinctive fix, and the one that doubles the project, because a "
        "project name is not unique and the key is gone after six minutes.",
        path=PKG / "sync.py",
        old="        found = self.client.find_projects(deal.xero_contact_id, name)",
        new="        found = []  # assume it never landed",
    ),
    Mutation(
        name="silent-project-fallback",
        defect="When the Projects API answers 403, record the deal as if a real "
        "project had been created, so the business reads 'project' where there "
        "is none.",
        path=PKG / "sync.py",
        old="            mode = PROJECT_MODE_UNAVAILABLE\n"
        "            project_id = None",
        new="            mode = PROJECT_MODE_PROJECTS_API\n"
        "            project_id = None",
    ),
    Mutation(
        name="stale-refresh-token",
        defect="After a refresh, keep the stored refresh token instead of the one "
        "that came back, so the app is locked out once the 30-minute grace "
        "period ends.",
        path=PKG / "auth.py",
        old='            refresh_token=payload["refresh_token"],\n'
        "            refresh_expires_at=issued_at + REFRESH_TOKEN_LIFETIME,",
        new="            refresh_token=current.refresh_token,\n"
        "            refresh_expires_at=current.refresh_expires_at,",
    ),
    Mutation(
        name="increment-amount-paid",
        defect="Compute amount_paid by adding the recorded payments to the "
        "previous total instead of reading AmountPaid/AmountDue back from the "
        "invoice, so every redelivered webhook or repeated apply drifts the balance.",
        path=PKG / "sync.py",
        old='        amount_paid = round(float(invoice["AmountPaid"]), 2)\n'
        '        amount_owed = round(float(invoice["AmountDue"]), 2)',
        new="        amount_paid = round(\n"
        "            deal.amount_paid\n"
        "            + sum(p.amount for p in self.ledger.payments_for(deal.deal_id)),\n"
        "            2,\n"
        "        )\n"
        "        amount_owed = round(deal.contract_total - amount_paid, 2)",
    ),
    Mutation(
        name="trust-the-idempotency-window",
        defect="Skip the lookup by the planted ContactNumber and rely on the "
        "Idempotency-Key alone -- correct for six minutes, after which the "
        "same key is processed as new and the retry mints a dated twin.",
        path=PKG / "sync.py",
        old="        return self.client.get_contact(contact_number)",
        new="        return None  # the key will have replayed it",
    ),
]


def run_suite() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    failed = sorted(
        {
            line.split("::")[1].split(" ")[0]
            for line in proc.stdout.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        }
    )
    return proc.returncode, failed


def main() -> int:
    baseline_code, baseline_failed = run_suite()
    print(f"baseline: {'PASS' if baseline_code == 0 else 'FAIL'}")
    if baseline_code != 0:
        print("the suite must be green before mutating:", baseline_failed)
        return 1

    survived: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"\n!! {mutation.name}: anchor not found in {mutation.path.name}")
            return 1
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8"
            )
            code, failed = run_suite()
        finally:
            mutation.path.write_text(original, encoding="utf-8")

        print(f"\n[{mutation.name}]")
        print(f"  defect: {mutation.defect}")
        if code == 0:
            print("  CAUGHT BY: nothing -- the suite does not cover this defect")
            survived.append(mutation.name)
        else:
            for name in failed:
                print(f"  caught by: {name}")

    print()
    if survived:
        print(f"{len(survived)} mutation(s) survived: {', '.join(survived)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations were caught; the suite bites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
