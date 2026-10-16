# xero-idempotent-sync-proof

A harness for the gap that quietly double-books money in a CRM-to-accounting
sync: **Xero commits the write, and your process dies before it can write
down the id.** Applied to one concrete flow -- a "deal paid" webhook that
must become a Xero Contact, a Project, an Invoice and one Payment per
instalment -- with runnable evidence that no step is performed twice across
a real process kill.

Sibling of [qbo-idempotent-sync-proof](https://github.com/guillemustafa-ux/qbo-idempotent-sync-proof),
which proves the same thing against QuickBooks Online. The design differs
where Xero differs: Xero's `Idempotency-Key` is a **6-minute dedupe**, not a
durable replay, so the planted external keys (`ContactNumber`,
`InvoiceNumber`, payment `Reference`) are what make recovery possible with
nothing but the database.

Work in progress: this README is a stub until the suite is complete.

## Scope, honestly (notes to be folded into the full README)

- **The duplicate-`ContactNumber` message is not documented.** Xero's docs say
  only that a PUT matching an existing `ContactNumber` "will receive an error".
  The fake's wording is an assumption and is switchable; the sync never keys on
  it -- any 400 from the contact create is followed by `GET /Contacts/{ContactNumber}`.
- **What the durable intent row buys.** With the external keys planted, a sync
  *without* the intent row still ends with one entity: the resend is refused on
  the planted number and the refusal triggers the lookup. The row saves that
  blind resend, removes the dependence on Xero refusing, and keeps the settled
  name variant, the attempt counter and the project mode. The planted keys and
  the look-first rule are what prevent the duplicate; the derived key is what
  settles two replicas racing. `scripts/verify_the_suite_bites.py` says so for
  the `no-durable-intent` mutation: it is caught by request counts on the fake's
  wire and by the intent row the crash tests read back, not by a duplicate.
- **Assumptions, not verified against Xero** (each modelled on the side that
  makes the client's job harder): a 403 from `projects.xro` with the `projects`
  scope granted means the plan lacks Projects (without the scope, the sync
  escalates instead of recording `unavailable`); project names are not unique;
  a refresh with the retired token inside the grace period retires the newest
  token; `GET /Contacts/{ContactNumber}` for an unknown number is a 404; `where`
  is modelled case-insensitive only, the documented accent-insensitivity is not.
- **The fake does not count requests against the 60/min or 5-concurrent
  limits**; throttling is armed per test.
- **One token store per refresh token.** Replicas with separate stores and one
  shared refresh token lock each other out thirty minutes after one rotates;
  `auth.py` has no lock or compare-and-swap. Share the store or serialise
  rotation.
- **A payment adopted by `Reference` must also match `Amount` and `Date`**;
  a stranger's payment quoting our reference is escalated.
- **A 5xx, a non-Xero body, or throttling that would outlast the six-minute key
  window are all `UnknownOutcome`**: the sync looks up before it resends.

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v --log-cli-level=INFO
```

Python 3.10+. No runtime dependencies; `pytest` only to run the proof.
