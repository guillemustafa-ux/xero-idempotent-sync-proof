"""Break the implementation on purpose, and check the suite notices.

A green suite proves nothing on its own -- tests that assert only what the
code already does are green forever, including after someone deletes the part
that mattered. So this script reintroduces, one at a time, the seven defects
this design exists to prevent, and reports which tests catch each one.

Every mutation is applied to a real source file, the suite is run against it,
and the file is restored afterwards (even if the run is interrupted). The
working tree must be clean before the run -- a mutation and an uncommitted
edit would be indistinguishable -- and is checked clean again at the end, so
"restored" is verified rather than assumed.

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
    # How the suite catches it, honestly: by a visible duplicate, by a request
    # that should not be on the wire, or only by looking at the ledger.
    evidence: str = ""


MUTATIONS = [
    Mutation(
        name="random-idempotency-key",
        defect="Generate the Idempotency-Key at call time instead of deriving it "
        "from the deal and the step, so a process with no memory of the first "
        "attempt -- or a second replica -- sends a key Xero has never seen.",
        path=PKG / "ledger.py",
        old='    material = f"{deal_id}:{step}" + (f":{ref}" if ref else "")\n'
        "    if attempt:\n"
        '        material += f":attempt{attempt}"\n'
        "    digest = hashlib.sha256(material.encode()).hexdigest()\n"
        '    return f"xis-{digest[:40]}"',
        new="    import uuid\n\n"
        '    return f"xis-{uuid.uuid4().hex[:40]}"',
        evidence="A visible duplicate: two replicas past the lookup both send, and "
        "the second is no longer a replay (scenario 4), plus the keys asserted on "
        "the wire in scenarios 1 and 3.",
    ),
    Mutation(
        name="no-durable-intent",
        defect="Send the step without recording the intent first, so a process "
        "killed mid-call wakes up believing nothing was ever tried.",
        evidence="NOT a visible duplicate. With the external keys planted, recovery "
        "without the row resends, Xero refuses on the planted ContactNumber or "
        "InvoiceNumber, and the refusal triggers the same lookup -- one entity "
        "either way. What fires, honestly: the crash tests of scenarios 1, 2 and 4 "
        "fail on their FIRST assertion, that the killed child left an intent row "
        "(their wire counts never run); the ledger tests fail on the missing row; "
        "and the wire checks that do fire are scenario 3's 5xx and throttle-past-"
        "the-window tests, where the next run must look up by ContactNumber before "
        "it resends. The payment crash test's PUT count could not catch this on "
        "its own: payments look first anyway. The row buys the lookup-before-"
        "resend, the settled name variant, the attempt and the project mode; it "
        "is not what prevents the duplicate here.",
        path=PKG / "ledger.py",
        old="        self._conn.execute(\n"
        '            "INSERT OR IGNORE INTO intents (deal_id, step, ref, idempotency_key, mode, state) "\n'
        '            "VALUES (?, ?, ?, ?, ?, ?)",\n'
        "            (deal_id, step, ref, key, mode, PENDING),\n"
        "        )\n"
        "        intent = self.get_intent(deal_id, step, ref)\n"
        "        assert intent is not None\n"
        "        return intent",
        new="        return Intent(deal_id, step, ref, key, mode, PENDING, None, 0)",
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
        evidence="A visible duplicate: a dated twin of the couple after the window "
        "closes (scenario 1), and the rebuilt ledger after the window (scenarios 1 "
        "and 4).",
    ),
]


def working_tree_dirty() -> str:
    """``git status --porcelain`` for the repo: empty when clean."""
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


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
    dirty = working_tree_dirty()
    if dirty:
        print("the working tree must be clean before mutating (commit or stash first):")
        print(dirty)
        return 1

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
        # ``newline="\n"``: on Windows the default would restore the file
        # with CRLF endings, and the repo pins LF.
        try:
            mutation.path.write_text(
                original.replace(mutation.old, mutation.new, 1), encoding="utf-8", newline="\n"
            )
            code, failed = run_suite()
        finally:
            mutation.path.write_text(original, encoding="utf-8", newline="\n")

        print(f"\n[{mutation.name}]")
        print(f"  defect: {mutation.defect}")
        if mutation.evidence:
            print(f"  how it is caught: {mutation.evidence}")
        if code == 0:
            print("  CAUGHT BY: nothing -- the suite does not cover this defect")
            survived.append(mutation.name)
        else:
            for name in failed:
                print(f"  caught by: {name}")

    print()
    dirty = working_tree_dirty()
    if dirty:
        print("the source files were NOT restored cleanly:")
        print(dirty)
        return 1
    if survived:
        print(f"{len(survived)} mutation(s) survived: {', '.join(survived)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations were caught; the suite bites (working tree clean again)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
