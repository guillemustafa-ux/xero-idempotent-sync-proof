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

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v --log-cli-level=INFO
```

Python 3.10+. No runtime dependencies; `pytest` only to run the proof.
