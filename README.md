# xero-idempotent-sync-proof

[![proof](https://github.com/guillemustafa-ux/xero-idempotent-sync-proof/actions/workflows/ci.yml/badge.svg)](https://github.com/guillemustafa-ux/xero-idempotent-sync-proof/actions/workflows/ci.yml)

A harness for the gap that quietly double-books money in a CRM-to-accounting
sync: **Xero commits the write, and your process dies before it can write
down the id.** Applied to one concrete flow -- a "deal paid" webhook that
must become a Xero Contact, a Project, an Invoice and one Payment per
instalment -- with runnable evidence that:

1. **no step is performed twice** across a real process kill, including the
   case where the outcome of the call is genuinely unknowable -- and
   including the retry that arrives **after Xero has forgotten the
   `Idempotency-Key`**, which it does six minutes after the first call;
2. **two couples who share both names stay two contacts**, and a redelivered
   webhook never creates a second Payment;
3. the sync takes the **honest path on Xero Projects** -- a separate product
   the organisation's plan may not include -- and says so in the operational
   record instead of silently degrading;
4. it **refuses to guess** when Xero offers no way to settle the question --
   two payments carrying our reference, two projects carrying our name, an
   invoice carrying our number on somebody else's contact -- and escalates
   with the exact ids instead.

You can verify all of it in about two minutes, on your machine, with **no
Xero account and no network access**. There are no runtime dependencies:
the fake Xero, the OAuth client, the transport and the ledger are standard
library, so nothing here can be attributed to a framework being clever.

This is the sibling of [qbo-idempotent-sync-proof](https://github.com/guillemustafa-ux/qbo-idempotent-sync-proof),
which proves the same thing against QuickBooks Online, and of
[idempotent-writes-proof](https://github.com/guillemustafa-ux/idempotent-writes-proof),
which proves the general mechanism against a generic third-party API. This
repo applies it to the specific things Xero does -- and the one thing Xero
does differently from QBO changes the design.

## The failure, precisely

A call to Xero has three outcomes, not two:

* it **succeeded** and you were told so;
* it **provably failed** -- Xero answered with a `ValidationException`, so
  nothing was created. `The contact name ... is already assigned to another
  contact` is the interesting one;
* the **outcome is unknown** -- the connection died, the request timed out,
  the container was evicted mid-flight, a gateway answered 502.

Almost every client library collapses the third case into the second, and
the natural `except ConnectionError: retry` treats "it never landed" and "it
landed and I wasn't told" identically. One of those two retries is a
duplicate. For a Contact that costs a cleanup; for a Payment it is the
couple's deposit booked twice.

```
    intent committed              ─┐
                                   │   ← a crash here is free
    PUT /Payments                  ─┤   Idempotency-Key: derived; Xero keeps it for 6 minutes
                                   │   ← a crash here books the payment twice on a retry after the window
    200 { Payments: [{ PaymentID }] } ─┤
                                   │   ← a crash here books the payment twice on a retry after the window
    save xero_payment_id           ─┘
```

## What Xero gives you decides the strategy

The central fact, from Xero's own
[idempotency guide](https://developer.xero.com/documentation/guides/idempotent-requests/idempotency/):
idempotency keys "are stored for 6 minutes from the time of the first call".
Within that window a reused key returns the cached response; after it, the
same key "is processed as a new key". Errors are cached under the key too,
and the same key with a different request is a `400`. So Xero's
`Idempotency-Key` is a **six-minute dedupe, not a durable replay**. It
settles a 429 retry, a connection that dropped a moment ago, two workers on
the same delivery -- and nothing that happens an hour later, or after the
ledger was rebuilt from a backup. What settles those is a value **we
planted** on the entity and can look up for as long as the entity exists.

| The step | Xero offers | How an in-flight write is settled |
|---|---|---|
| Contact | `Idempotency-Key` (6 min) + a unique, API-only [`ContactNumber`](https://developer.xero.com/documentation/api/accounting/contacts) we plant | The key covers the transient retry only. The durable settle is `GET /Contacts/{ContactNumber}` with the derived number: one -> adopt; none -> ours never landed. A `400` on a `Name` held by a stranger -> a deterministic variant (the wedding date), under a derived key of its own. |
| Invoice | `Idempotency-Key` + a unique [`InvoiceNumber`](https://developer.xero.com/documentation/api/accounting/invoices) we plant | Derived `InvoiceNumber`. "Invoice # must be unique." means it landed: look it up by number and adopt -- after checking it sits on our contact. |
| Payment | `Idempotency-Key` only; the [`Reference`](https://developer.xero.com/documentation/api/accounting/payments) is **not** unique | Plant the derived `Reference` and **look first, always**: count the invoice's `Payments[]` carrying it. Exactly one, with our `Amount` and `Date` -> adopt; zero -> create; more than one -> **escalate**, never pick. |
| Project | `Idempotency-Key`; name uniqueness not documented; the [Projects API](https://developer.xero.com/documentation/api/projects/projects) may be absent from the plan | Planted name under the contact, interrogate-first through every page of the list. A `403` with the `projects` scope granted -> ledger `project_mode = unavailable`, surfaced, never silent. A `403` without the scope -> the app's fault; escalate. |
| any step after the six-minute window with no planted key | nothing | **Nothing can settle it.** Stop and escalate. This design has no such step, by construction. |

The last row is the honest one, and the reason every entity here carries
something derived from the deal. A key the service forgets in six minutes
recovers no information an hour later; a number the service enforces as
unique does.

## The design under test

* **Intent before send -- and what it buys here, stated plainly.** Each
  step's key is committed to SQLite (WAL, `synchronous=FULL`) *before* the
  first byte leaves. With the external keys planted, a sync *without* that
  row still ends with one entity: the blind resend is refused on the planted
  `ContactNumber` or `InvoiceNumber`, and the refusal triggers the same
  lookup. What the row buys is the lookup **before** the resend (one create
  on the wire, ever, in the crash tests), independence from Xero refusing,
  and the settled name variant, the attempt counter and the project mode,
  so a later process derives the right name and the right key without
  re-provoking the 400s that taught the first one. The planted keys and the
  look-first rule are what prevent the duplicate, not the row.
* **Two replicas racing are settled step by step, and the guarantee
  differs.** Separate ledgers, the same delivery, both past the lookup:
  the **contact and invoice** are settled by the planted unique
  `ContactNumber` / `InvoiceNumber` -- the second send is refused and
  adopts the first, whatever key it carried. The **payment and project**
  are settled by the **one derived key**, inside the six-minute window, and
  by nothing after it. What this design does not close: replica A sends at
  minute 0, replica B looks up at 6:01 before A's payment is visible and
  sends -- a window of seconds against an immediate lookup, and nothing
  Xero offers closes it.
* **The key is derived, not generated.**
  `"xis-" + sha256(f"{deal_id}:{step}[:{ref}]")[:40]` -- 44 characters,
  under Xero's limit of 128 -- so any process, at any later time, with no
  memory of the attempt, computes the same one. `uuid4()` at call time is
  not an idempotency key: every retry carries a fresh one and Xero has
  nothing to recognise. One rule on top, and it is **not the same for every
  step**: a **validation `400` is cached under its key for six minutes**.
  For the contact and invoice -- where Xero refuses a duplicate on the
  planted number anyway -- a provable rejection bumps the step's persisted
  `attempt` and the next send derives `...:attempt{n}`, a key Xero has
  never seen; a second replica on another attempt is refused on the number
  and adopts. For the **payment and project the key is never changed**: a
  per-ledger counter in the key would make two replicas send two keys for
  one instalment and book it twice (scenario 4p). A cached 400 pins those
  steps for at most six minutes instead, with the reason on the deal row;
  the send after the window goes out under the same key and is processed
  as new. Attempt 0 stays exactly what a rebuilt ledger re-derives.
* **The planted keys are derived the same way.** `ContactNumber`
  `hs-deal-{deal_id}` (hashed when over Xero's 50 characters),
  `InvoiceNumber` `HS-{deal_id}`, payment `Reference`
  `hubspot_payment_id=... deal=...`.
* **A consistent Name, and a deterministic tiebreak.** The contact is
  `"Ana Perez & Tomas Ruiz"`; on the "already assigned" `400` the sync first
  asks whether a contact carries this deal's `ContactNumber`. If so it is
  ours and is adopted. If not, the holder is a stranger and the next variant
  is `"Ana Perez & Tomas Ruiz (2027-11-20)"` -- the wedding date, not a
  counter, so every process picks the same one, under a key of its own.
* **Interrogate first.** Where Xero refuses a duplicate loudly (`Name`,
  `ContactNumber`, `InvoiceNumber`), send first and treat the refusal as the
  lookup trigger. Where it does not (payment `Reference`, project name),
  look before **every** send. And a step whose intent was already in flight
  when a previous process died is **always** looked up before anything is
  resent, because the key it went out under may be gone.
* **Read back to state the balance.** `amount_paid` and `amount_owed` come
  from the invoice's `AmountPaid` and `AmountDue`, never from `+= amount`. A
  counter you bump is itself a non-idempotent write.
* **The refresh token that came back is the only one that will still work in
  30 minutes.** Xero's refresh token
  [rotates on every refresh](https://developer.xero.com/documentation/best-practices/data-integrity/managing-tokens);
  the previous one survives a 30-minute grace period and is then
  `invalid_grant` for good, so the store is overwritten on every refresh.
  Known limit: **one token store per refresh token.** Replicas with separate
  stores and one shared token lock each other out thirty minutes after one
  of them rotates; `auth.py` has no lock or compare-and-swap. Share the
  store, or serialise rotation.
* **429 means resend the same request -- within the window.** A throttled
  create is retried after the `Retry-After` Xero asked for, with the same
  URL, body and key. The 429s themselves put nothing in Xero; the hazard is
  an *earlier* send under the same key (a lost response, a killed process)
  whose six minutes the accumulated wait does not measure and may outlive
  -- a resend after it closes is processed as new. So if honouring the next
  `Retry-After` would cross six minutes the transport stops with
  `ThrottledPastWindow` (an `UnknownOutcome`) and the sync looks up before
  it sends.
* **The status code is read before the body.** A 401 refreshes once; a 404
  is "absent" -- Xero's 404 is commonly **plain text**, and a client that
  demanded a JSON body first would never learn that the planted number is
  free; a 429 is throttling; a Projects 403 is the product gate. Only the
  remaining 4xx (the 400 whose messages the sync reads) must carry a Xero
  JSON body to count as a definite "no". **A 5xx, or a 400-class answer
  with no Xero body, is `UnknownOutcome`**, not a rejection: a gateway can
  answer 502 after handing the request on.
* **A Projects `403` is checked against the granted scopes** before it is
  read as "the plan has no Projects". The scopes are a claim inside the
  access-token JWT (the token response has no `scope` field in the docs);
  a token missing the `projects` scope is the app's mistake, escalated,
  never filed as a plan limit -- and when the scopes cannot be read at
  all, the 403 is escalated too, because it is then evidence of nothing.
* **Unique indexes on every remote binding** -- `xero_contact_id`,
  `xero_project_id`, `xero_invoice_id`, `xero_payment_id` -- and a `CHECK`
  tying `xero_project_id` to `project_mode` (an id exists exactly when the
  mode says a real Project was made), so a future change that lies about a
  project or binds a second Xero entity to one local thing is rejected by
  the database rather than by a code review. A `schema_version` table
  upgrades a database written by an earlier version on open, because
  `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists:
  a missing column is added (`ALTER TABLE ... ADD COLUMN`); a `deals` table
  whose stored DDL lacks the `CHECK` is rebuilt (create beside, copy, drop,
  rename, indexes back, in one transaction), because SQLite cannot add a
  constraint in place; and a payment or project intent that an earlier
  version bumped is pinned back to attempt 0 and its derived key.
* **Recovery walks every deal, whatever blew up.** One deal's escalation,
  rejection, unknown outcome -- or an exception nobody wrote a handler for
  -- is recorded on that deal's row and the walk continues; only an
  authentication lockout stops it, because every deal behind would fail
  the same way.

## Verify it in two minutes

```bash
git clone https://github.com/guillemustafa-ux/xero-idempotent-sync-proof
cd xero-idempotent-sync-proof
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v --log-cli-level=INFO
```

The suite runs the sync against a **fake Xero** that speaks real HTTP over a
real socket and encodes the documented behaviours above -- with an
injectable clock, so a test can watch the six-minute window close -- and
injects faults at the exact seam where money gets lost. The crash scenarios
are not mocked: a **child process is killed with `os._exit`** mid-step, so
no stack unwinds, no `finally` runs and no buffer flushes -- exactly what an
evicted container leaves behind. Recovery then runs in a fresh process with
nothing but the database.

```text
tests/test_scenario_1_contact_exactly_once.py
  test_naive_retry_after_a_real_kill_makes_a_second_contact PASSED

  resolve deal-1001 step=contact by ContactNumber hs-deal-deal-1001 -> ADOPT (ContactID=47356f79…bd45a)
  intent committed for deal-1001 step=project key=xis-559b1ffb…76550 (durable, pre-send)
  project step for deal-1001 -> UNAVAILABLE (flagged): HTTP 403: {… "Detail": "The connected
    organisation's Xero plan does not include Projects." …}
  deal deal-1001: the Projects API is not available to this organisation (HTTP 403), so no Xero
    Project was created for 'Wedding 2027-03-14 - Ana Perez & Tomas Ruiz'. The invoice and payments
    are booked on the contact; the ledger says project_mode=unavailable.
  intent committed for deal-1001 step=invoice key=xis-7caa7003…b40a6 (durable, pre-send)
  resolve deal-1001 step=invoice key=xis-7caa7003…b40a6 -> CREATED NOW (InvoiceID=3f623fe0…fa5b3)
  intent committed for deal-1001 step=payment[pay-1] key=xis-e5724cf0…4ee55 (durable, pre-send)
  resolve deal-1001 step=payment[pay-1] by Reference -> PROVEN ABSENT
  resolve deal-1001 step=payment[pay-1] key=xis-e5724cf0…4ee55 -> CREATED NOW (PaymentID=bfb5bbc7…e1b24)
  test_durable_sync_survives_a_real_kill_between_200_and_persist PASSED

  intent committed for deal-2 step=contact[0] key=xis-4a5a5bd7…02d73 (durable, pre-send)
  Name 'Ana Perez & Tomas Ruiz' is taken by a stranger -> variant 1
  intent committed for deal-2 step=contact[1] key=xis-cd550e69…f4e02 (durable, pre-send)
  resolve deal-2 step=contact[1] key=xis-cd550e69…f4e02 -> CREATED NOW (ContactID=a9a164d5…3c37c)
  test_two_deals_with_the_same_names_stay_two_contacts PASSED

  intent committed for deal-1001 step=contact[0] key=xis-a4d4bad6…ab76c (durable, pre-send)
  resolve deal-1001 step=contact -> 400 on 'Ana Perez & Tomas Ruiz', holder carries our ContactNumber
    -> ADOPT (ContactID=0bdcf20d…b7f08)
  test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing[after-the-window] PASSED

tests/test_scenario_2_projects_honest_path.py
  resolve deal-1001 step=project by planted name -> PROVEN ABSENT
  resolve deal-1001 step=project key=xis-559b1ffb…76550 -> CREATED NOW (projectId=88010778…88412)
  project step for deal-1001 -> PROJECTS API (projectId=88010778…88412)
  test_an_organisation_with_projects_gets_a_real_project PASSED

  resolve deal-1001 step=project by planted name -> ADOPT (projectId=0ca8855a…3e4de)
  project step for deal-1001 -> PROJECTS API (projectId=0ca8855a…3e4de)
  test_crash_between_create_project_and_persist_adopts_by_planted_name PASSED

tests/test_scenario_4_payments_and_ledger.py
  resolve deal-1001 step=payment[pay-1] by Reference -> PROVEN ABSENT
  intent committed for deal-1001 step=contact[0] key=xis-a4d4bad6…ab76c (durable, pre-send)
  resolve deal-1001 step=contact[0] key=xis-a4d4bad6…ab76c -> REPLAYED (ContactID=cddbc24a…8d99f)
  resolve deal-1001 step=invoice key=xis-7caa7003…b40a6 -> REPLAYED (InvoiceID=6d92c55c…516a3b9)
  resolve deal-1001 step=payment[pay-1] key=xis-e5724cf0…4ee55 -> REPLAYED (PaymentID=95414622…a4adc4)
  test_two_workers_past_the_lookup_at_the_same_moment_book_one_payment PASSED

102 passed
```

Note the verdicts: **ADOPT**, **PROVEN ABSENT**, **CREATED NOW**,
**REPLAYED**. Same sync code, same recovery path, different conclusions --
one entity at Xero either way. In the crash test the first line of the
recovering process is a lookup, not a send: the intent row left by the
killed child is what makes it ask. In the rebuilt-ledger test there is no
row and no key, so the resend goes out, is refused on the name, and the
refusal is answered by the lookup. In the two-workers test the lookup
cannot help -- both replicas saw nothing -- and only the key both of them
*derived* makes the second send a replay; which is why that key is never
changed for a payment, not even after a cached 400. (The verdict word `REPLAYED` comes
from a response header the *fake* adds on a replay; the real service
returns the identical body with no marker. Nothing in the sync decides
anything based on it. It exists so the log can tell you what happened.)

### What each scenario proves

| # | Test | Cut point | Proven |
|---|---|---|---|
| 1a | `test_naive_retry_after_a_real_kill_makes_a_second_contact` | Xero commits, response lost, worker killed | **the anti-pattern, demonstrated**: a fresh key per call, the retry gets the "already assigned" `400`, appends "(2)", and one couple becomes two contacts |
| 1b | `test_durable_sync_survives_a_real_kill_between_200_and_persist` | `os._exit` **between** the 200 and the local write | the intent row outlived the process; recovery asks `GET /Contacts/{ContactNumber}` and adopts; one create on the wire, one contact |
| 1c | `test_lost_response_is_resolved_without_duplicating` | connection cut after Xero commits | the process survives and still cannot tell -- the next run looks up by the planted number; one create, one contact |
| 1d | `test_a_create_that_never_landed_is_actually_sent` | connection cut **before** Xero commits | the mirror case: the lookup proves absence and recovery **does** create -- "do nothing on doubt" is not the answer either |
| 1e | `test_two_deals_with_the_same_names_stay_two_contacts` | -- | the `400` on `Name` answered with the date variant under its own derived key; two couples, two contacts, two invoices; a fresh process re-derives the same variant |
| 1f | `test_a_name_with_quotes_is_escaped_in_the_where_clause` | both name variants held by strangers | `Niamh "Nim" O'Brien` reaches `where=Name=="..."` escaped, so the escalation names the exact holders instead of hitting a parse error |
| 1g | `test_replaying_a_key_with_a_corrected_body_is_rejected_loudly` | -- | **the opposite of QBO**: the same key with a corrected body is a `400` "is used with a different request" and creates nothing; the sync's variant path never trips it, because every body under a key is a pure function of the deal |
| 1h | `test_after_the_window_the_key_is_new_and_only_the_contact_number_settles_it` | response lost; the redelivery arrives 6 min + 1 s later | **six minutes is the whole promise**: the key-trusting sync replays inside the window and mints a "(2)" twin after it, with a 200 on every call; the durable sync settles by `ContactNumber` |
| 1i | `test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing[inside-the-window]` | ledger wiped, webhook replayed | a process with no memory computes the same keys and numbers; contact and invoice are replayed; nothing is created twice |
| 1j | `test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing[after-the-window]` | ledger wiped, window closed | no key left to replay; the resend is refused on the name and the refusal triggers the lookup by number; nothing is created |
| 1k | `test_a_number_collision_is_settled_by_the_lookup_whatever_the_message_says` | -- | the duplicate-`ContactNumber` wording is undocumented, so **any** `400` from the create is followed by the lookup by number; a holder with a different name and a generic message is still adopted |
| 1l | `test_a_holder_carrying_our_contact_number_is_adopted_not_suffixed` | -- | "already assigned" is not proof of a stranger: the holder carries our number, so it is adopted, not given a date suffix |
| 1m | `test_a_plain_text_404_still_proves_the_number_absent` | a stranger holds the name; the lookup by number is a `404` with a **plain-text** body | the status is the answer: `None`, the date variant is created; a client demanding a JSON body first would have left this deal "unknown" forever |
| 2a | `test_an_organisation_without_projects_is_flagged_and_still_invoiced` | Projects answers `403` | no project is created, the ledger says `unavailable`, a notice is surfaced, and the invoice and payment are still booked on the contact |
| 2b | `test_a_403_with_the_projects_scope_missing_is_the_apps_fault_not_the_plans` | `403`, token without the `projects` scope | escalated with the granted scopes named; **not** recorded as `unavailable`; nothing downstream until a human re-authorises |
| 2b' | `test_a_403_with_the_granted_scopes_unknown_is_escalated_not_filed_as_the_plan` | `403`, and the scopes cannot be read (no decodable JWT, no `scope` field) | escalated: a 403 whose cause cannot be told is not evidence about the plan; never `unavailable` |
| 2c | `test_an_organisation_with_projects_gets_a_real_project` | -- | the same code on a plan with Projects creates a real project and records `projects_api` |
| 2d | `test_crash_between_create_project_and_persist_adopts_by_planted_name` | `os._exit` after the POST returns; recovery after the window | no key to replay and no unique name to collide on; the lookup by planted name finds exactly one and adopts it; one POST on the wire |
| 2e | `test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing` | same, plus a second project planted | the sync stops with both ids, books nothing downstream, and still two projects, not three; startup recovery reports it instead of raising |
| 2f | `test_recovery_settles_the_deals_it_can_and_reports_the_one_it_cannot` | deal A unresolvable, deal B killed after its payment landed | recovery walks both: A is reported with its reason, B's payment is adopted, not resent |
| 2f' | `test_recovery_records_an_unexpected_exception_on_its_deal_and_walks_on` | deal A blows up on an exception with no handler | recorded as `RuntimeError: ...` on A's row, B settled; an authentication lockout, by contrast, propagates |
| 2g | `test_recovery_finds_the_planted_project_behind_hundreds_of_others` | 520 other projects under the contact; crash; window closed | the list is paged (50 by default, 500 at most): `pageSize=500`, `contactID` filter, every page `pagination.pageCount` announces; adopted from page 2 |
| 2h | `test_the_lookup_matches_the_planted_name_exactly_and_ignores_the_contacts_other_projects` | -- | the contact's other projects, a near-miss name and a stranger's project with our exact name are all left alone |
| 2i | `test_a_lost_response_on_the_project_create_is_settled_by_the_name_lookup` | connection cut after Xero commits | one POST, ever; the name lookup adopts before the key has to replay |
| 2j | `test_the_projects_lookup_spells_the_documented_contactID_on_the_wire` | -- | every list request carried `contactID` (the documented spelling) and `pageSize=500`, pinned on the fake's wire, because a wrong spelling would be hidden by the client's own filter |
| 2k | `test_the_projects_lookup_filters_by_contact_itself_when_the_service_does_not` | the fake ignores `contactID` and returns every project | a stranger's project with our exact name is on the page and is not matched; reverting either guard alone fails exactly one of 2j/2k |
| 3a | `test_refresh_rotation_persists_the_latest_token_for_a_fresh_process` | -- | after a rotation and past the grace period, a brand-new process authenticates from the store alone |
| 3b | `test_a_store_that_kept_the_old_refresh_token_still_works_inside_the_grace_period` | -- | **the anti-pattern, caught in time**: 29 minutes after dropping the rotated token the stale store still refreshes, and a correct refresh repairs it |
| 3c | `test_a_store_that_kept_the_old_refresh_token_is_locked_out_after_the_grace_period` | -- | **the anti-pattern, 31 minutes later**: `invalid_grant` for good; only a human re-authorisation fixes it |
| 3c' | `test_granted_scopes_are_read_from_the_access_token_jwt_not_from_the_response` | -- | the scopes come from the access token's `scope` claim; an opaque token with a `scope` field in the response still works; an opaque token and nothing else -> `None`, "unknown", never an empty set |
| 3c'' | `test_a_token_store_from_before_the_scope_column_is_upgraded_on_open` | an `oauth_tokens` table without `scope` | opened, upgraded, the next refresh writes and reads the column |
| 3d | `test_an_expired_access_token_is_refreshed_on_401_and_the_call_retried` | access token revoked behind the client | one refresh, one retry, one contact |
| 3e | `test_a_throttled_create_resends_the_same_key_and_makes_one_contact` | two `429` in a row | `Retry-After` honoured once per 429; every send carried the same key; one contact |
| 3f | `test_a_5xx_is_an_unknown_outcome_not_a_rejection` | `503` on the create | `UnknownOutcome`, not `XeroRejected`; recorded on the deal; the next run looks up by the planted number before it sends |
| 3f' | `test_a_400_without_a_xero_body_is_unknown_and_a_plain_404_is_absent` | a `400` with a gateway's HTML body; a `404` with a plain-text body | the first is `UnknownOutcome` (not written by Xero); the second is `None` from the lookup, never "unknown" |
| 3g | `test_throttling_that_would_outlast_the_key_window_stops_and_asks` | two `429` with `Retry-After: 200` | the first wait fits, the second would cross 360 s; the transport stops with `ThrottledPastWindow` -- for the sake of an *earlier* send under the key, not this one -- and the sync looks up first |
| 4a | `test_duplicate_webhook_delivery_records_one_payment` | -- | the same webhook twice, from two workers: one Payment, balance read back |
| 4b | `test_two_workers_past_the_lookup_at_the_same_moment_book_one_payment` | a second replica with its own database sends at the `before_payment_send` seam | **what the six-minute key is for**: both looked up and saw nothing; only the key both derived makes the second send a replay; one Payment, both ledgers bound to it |
| 4c | `test_a_second_payment_hits_the_same_contact_and_invoice` | -- | later instalments reuse everything and add one Payment; `amount_owed` comes from the invoice |
| 4d | `test_out_of_order_delivery_ends_in_the_same_state` | -- | second payment first, then the first, then both again: two Payments, right balance |
| 4e | `test_naive_payment_retry_after_a_real_kill_pays_the_invoice_twice` | Payment committed, response lost, worker killed; redelivery after the window | **the anti-pattern where it costs money**: the derived key would have worked for six minutes; with nothing recorded and nothing to collide on, the invoice is simply paid twice |
| 4f | `test_an_invoice_survives_a_real_kill_between_200_and_persist` | `os._exit` after the invoice 200 | recovery asks `GET /Invoices/{InvoiceNumber}` first and adopts; one create on the wire |
| 4g | `test_a_lost_response_on_the_invoice_create_is_settled_by_its_number` | connection cut after Xero commits | same hazard, no crash: one create, ever |
| 4h | `test_an_invoice_with_our_number_on_a_strangers_contact_is_escalated` | -- | "Invoice # must be unique." says *an* invoice exists, not whose; one on another contact is named with both contact ids and nothing is paid against it |
| 4i | `test_a_payment_survives_a_real_kill_between_200_and_persist` | `os._exit` after the payment 200 | recovery counts the invoice's `Payments[]` carrying our `Reference`, finds one, adopts; no resend |
| 4j | `test_two_payments_with_our_reference_escalate_instead_of_guessing` | same, plus a second payment planted with our reference | the sync stops with both ids, adopts nothing, resends nothing; recovery reports it |
| 4k | `test_a_cached_rejection_pins_the_payment_for_at_most_six_minutes` | the bank account is missing, so the payment is a real `400` | **the second trap of the key store**: Xero caches errors too, and the sync does *not* mint a fresh key to dodge it -- the deal records the rejection, the step stays pinned on the cached 400 for at most the window, and the send after it goes out under the same key, once |
| 4l | `test_a_strangers_payment_carrying_our_reference_is_not_adopted` | -- | one match is not proof: a 1.00 test payment quoting our reference, or the right amount on the wrong day, is escalated, never adopted |
| 4l' | `test_a_payment_whose_date_cannot_be_read_is_a_mismatch_not_a_pass` | the one match has the right amount and a date nobody can parse | a mismatch, not a check skipped: escalated with the raw date, nothing adopted, nothing sent |
| 4l'' | `test_a_bound_invoice_that_cannot_be_read_back_is_escalated_not_crashed` | the ledger has the invoice id, Xero answers `404` | `UnresolvableStep` with the ids on that deal's row; the deal behind it is still settled (before: an `AssertionError` that aborted the walk) |
| 4l''' | `test_xero_dates_are_read_including_negative_epochs` | -- | `/Date(ms+0000)/` is read as epoch + milliseconds, so .NET's `DateTime.MinValue` (`-62135596800000`) is a date and not the `OSError` Windows raises from `fromtimestamp` |
| 4m | `test_the_schema_refuses_a_second_binding` | -- | the database rejects a second contact, project, invoice or payment binding independently of the code |
| 4n | `test_apply_is_safe_to_call_repeatedly` | -- | idempotence is a property of the operation, not of a retry budget |
| 4o | `test_a_rebuilt_ledger_after_the_window_adopts_the_invoice_by_number_and_the_payment_by_reference` | ledger gone **and** window closed | **the strong claim, which QBO could not make**: no key replays anything, and every step still settles by its planted value; nothing is created, and the payment is never resent |
| 4p | `test_two_replicas_with_unequal_rejection_counts_still_book_one_payment` | two replicas with their own databases hit the cached `400` -- the primary twice, the replica once; account fixed; window closed; seam race | **why the payment's key is never bumped**: with a per-ledger attempt in the key the two would send two keys and book the instalment twice (the reviewer's reproduction: PUT keys `key0, key1, key1, key2`, two payments); with one derived key the race ends in a replay, one payment, both ledgers bound to it |

The remaining tests are the fake's own (27 in `tests/test_fake_xero.py`,
over raw HTTP, pinning the verbatim error messages, the six-minute expiry,
the cached error, the different-request `400`, the plain-text `404`, the
JWT-shaped token, the grace period and the paging), the ledger's (13,
including the upgrade of a version-1 file: the added column and the
rebuilt `deals` table), the crash child's (2) and the live smoke's skip
path (1).

## Proof that the suite bites

A green suite proves nothing on its own -- tests that assert only what the
code already does stay green after someone deletes the part that mattered.
So the repo ships the check, and CI runs it:

```bash
python scripts/verify_the_suite_bites.py
```

It refuses to run on a dirty tree, reintroduces each defect into the real
source, runs the suite, reports what caught it, restores the file, and
verifies the tree is clean again:

```text
baseline: PASS

[random-idempotency-key]
  defect: Generate the Idempotency-Key at call time instead of deriving it from the deal and
          the step, so a process with no memory of the first attempt -- or a second replica --
          sends a key Xero has never seen.
  how it is caught: A visible duplicate: two replicas past the lookup both send, and the second is no
          longer a replay (scenario 4), plus the keys asserted on the wire in scenarios 1 and
          3.
  caught by: test_a_cached_rejection_pins_the_payment_for_at_most_six_minutes
  caught by: test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing[inside-the-window]
  caught by: test_a_rejected_step_derives_a_fresh_key_from_its_attempt
  caught by: test_after_the_window_the_key_is_new_and_only_the_contact_number_settles_it
  caught by: test_begin_step_commits_the_intent_before_anything_is_sent
  caught by: test_replaying_a_key_with_a_corrected_body_is_rejected_loudly
  caught by: test_the_idempotency_key_is_derived_not_generated
  caught by: test_two_deals_with_the_same_names_stay_two_contacts
  caught by: test_two_replicas_with_unequal_rejection_counts_still_book_one_payment
  caught by: test_two_workers_past_the_lookup_at_the_same_moment_book_one_payment

[no-durable-intent]
  defect: Send the step without recording the intent first, so a process killed mid-call wakes
          up believing nothing was ever tried.
  how it is caught: NOT a visible duplicate. With the external keys planted, recovery without the row
          resends, Xero refuses on the planted ContactNumber or InvoiceNumber, and the refusal
          triggers the same lookup -- one entity either way. What fires, honestly: the crash
          tests of scenarios 1, 2 and 4 fail on their FIRST assertion, that the killed child
          left an intent row (their wire counts never run); the ledger tests fail on the
          missing row; and the wire checks that do fire are scenario 3's 5xx and throttle-
          past-the-window tests, where the next run must look up by ContactNumber before it
          resends. The payment crash test's PUT count could not catch this on its own:
          payments look first anyway. The row buys the lookup-before-resend, the settled name
          variant, the attempt and the project mode; it is not what prevents the duplicate
          here.
  caught by: test_a_5xx_is_an_unknown_outcome_not_a_rejection
  caught by: test_a_cached_rejection_pins_the_payment_for_at_most_six_minutes
  caught by: test_a_payment_survives_a_real_kill_between_200_and_persist
  caught by: test_a_rejected_step_derives_a_fresh_key_from_its_attempt
  caught by: test_an_invoice_survives_a_real_kill_between_200_and_persist
  caught by: test_begin_step_commits_the_intent_before_anything_is_sent
  caught by: test_begin_step_twice_keeps_the_first_intent
  caught by: test_crash_between_create_project_and_persist_adopts_by_planted_name
  caught by: test_durable_sync_survives_a_real_kill_between_200_and_persist
  caught by: test_reject_step_records_a_definite_no
  caught by: test_throttling_that_would_outlast_the_key_window_stops_and_asks
  caught by: test_two_deals_with_the_same_names_stay_two_contacts

[blind-retry-on-doubt]
  defect: Resolve an unknown project outcome by assuming it never landed -- the instinctive
          fix, and the one that doubles the project, because a project name is not unique and
          the key is gone after six minutes.
  caught by: test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing[inside-the-window]
  caught by: test_crash_between_create_project_and_persist_adopts_by_planted_name
  caught by: test_recovery_finds_the_planted_project_behind_hundreds_of_others
  caught by: test_recovery_settles_the_deals_it_can_and_reports_the_one_it_cannot
  caught by: test_the_lookup_matches_the_planted_name_exactly_and_ignores_the_contacts_other_projects
  caught by: test_the_projects_lookup_filters_by_contact_itself_when_the_service_does_not
  caught by: test_two_projects_with_the_same_planted_name_escalate_instead_of_guessing

[silent-project-fallback]
  defect: When the Projects API answers 403, record the deal as if a real project had been
          created, so the business reads 'project' where there is none.
  caught by: test_an_organisation_without_projects_is_flagged_and_still_invoiced
  caught by: test_durable_sync_survives_a_real_kill_between_200_and_persist
  caught by: test_a_rebuilt_ledger_after_the_window_adopts_the_invoice_by_number_and_the_payment_by_reference
  caught by: test_two_workers_past_the_lookup_at_the_same_moment_book_one_payment
  … 32 tests in all: every test whose deal meets the 403 (see the note below)

[stale-refresh-token]
  defect: After a refresh, keep the stored refresh token instead of the one that came back, so
          the app is locked out once the 30-minute grace period ends.
  caught by: test_a_store_that_kept_the_old_refresh_token_still_works_inside_the_grace_period
  caught by: test_refresh_rotation_persists_the_latest_token_for_a_fresh_process

[increment-amount-paid]
  defect: Compute amount_paid by adding the recorded payments to the previous total instead of
          reading AmountPaid/AmountDue back from the invoice, so every redelivered webhook or
          repeated apply drifts the balance.
  caught by: test_a_second_payment_hits_the_same_contact_and_invoice
  caught by: test_apply_is_safe_to_call_repeatedly
  caught by: test_duplicate_webhook_delivery_records_one_payment
  caught by: test_out_of_order_delivery_ends_in_the_same_state

[trust-the-idempotency-window]
  defect: Skip the lookup by the planted ContactNumber and rely on the Idempotency-Key alone
          -- correct for six minutes, after which the same key is processed as new and the
          retry mints a dated twin.
  how it is caught: A visible duplicate: a dated twin of the couple after the window closes (scenario
          1), and the rebuilt ledger after the window (scenarios 1 and 4).
  caught by: test_a_5xx_is_an_unknown_outcome_not_a_rejection
  caught by: test_a_holder_carrying_our_contact_number_is_adopted_not_suffixed
  caught by: test_a_number_collision_is_settled_by_the_lookup_whatever_the_message_says
  caught by: test_a_plain_text_404_still_proves_the_number_absent
  caught by: test_a_rebuilt_ledger_after_the_window_adopts_the_invoice_by_number_and_the_payment_by_reference
  caught by: test_a_rebuilt_ledger_re_derives_every_key_and_number_and_creates_nothing[after-the-window]
  caught by: test_after_the_window_the_key_is_new_and_only_the_contact_number_settles_it
  caught by: test_durable_sync_survives_a_real_kill_between_200_and_persist
  caught by: test_throttling_that_would_outlast_the_key_window_stops_and_asks

all 7 mutations were caught; the suite bites (working tree clean again)
```

Three of those results say something true rather than something flattering:

* **`no-durable-intent` doubles nothing.** With the external keys planted,
  a recovery that forgot it was mid-call resends, Xero refuses on the
  planted `ContactNumber` or `InvoiceNumber`, and the refusal triggers the
  same lookup -- one entity either way. What actually fires, read off the
  list above: the crash tests (1b, 2d, 4f, 4i) fail on their **first**
  assertion, that the killed child left an intent row -- their wire counts
  never run; 1e and 4k fail reading the intent row back; the ledger tests
  fail on the missing row; and the two wire checks that do fire are 3f and
  3g, where the next run must look up by `ContactNumber` before it resends.
  The payment crash test's PUT count could not catch this on its own,
  because payments look first anyway. **No test finds a duplicate.** The row
  buys the lookup-before-resend, the settled name variant, the attempt and
  the project mode; it is not what prevents the duplicate here. Correct
  behaviour, said out loud, not a coverage gap.
* **`trust-the-idempotency-window` is the Xero-specific one.** Skip the
  lookup by `ContactNumber` and rely on the key, and every test that
  finishes inside six minutes with nothing in doubt stays green. The
  visible duplicate -- the dated twin -- appears only where the clock moves
  past the window: the redelivery seven minutes later (1h) and the rebuilt
  ledger after the window (1j, 4o). The other six catches are the lookup's
  absence on the wire or an adoption that did not happen (1b, 1k, 1l, 1m,
  3f, 3g), not a duplicate. That is the difference from QBO in one
  mutation: there a replayable `requestid` made the key alone sufficient;
  here it is sufficient for six minutes.
* **`silent-project-fallback` is caught 32 times, and mostly by the
  database.** Recording `projects_api` with no project id violates the
  `CHECK` on the `deals` table, so every deal that meets the 403 fails on
  the write, not on an assertion about the notice. Only
  `test_an_organisation_without_projects_is_flagged_and_still_invoiced`
  asserts the honest path itself; the other 31 are the schema refusing to
  store a project that does not exist. That is the constraint doing the job
  it was added for -- and a reminder that a mutation which also lies to the
  database is the easy one to catch.

## Projects, honestly

This is the part a demo skips and a quarter-end discovers.

* **Xero Projects is a separate product behind the organisation's plan**,
  with its own API (`https://api.xero.com/projects.xro/2.0/Projects`) and
  its own scope, `projects`
  ([docs](https://developer.xero.com/documentation/api/projects/projects)).
  Which plans include it, and whether the free demo company does, is **not
  verified** here; the fake makes it switchable and the default
  organisation does not have it.
* **An organisation without it answers `403`.** So does a token that was not
  granted the `projects` scope, and the two look the same on the wire. The
  sync reads the granted scopes off the access token's JWT `scope` claim
  first (the token response carries no `scope` field): without `projects`,
  the `403` is the app's authorisation mistake and is escalated; with it,
  the `403` is read as the plan (an assumption, see below) and recorded as
  `project_mode = unavailable`, with a notice; and when the scopes cannot
  be read at all, the `403` is escalated, because it is then evidence of
  nothing.
* **A project name is not unique** (the docs do not say either way; the
  harder reading is modelled), so an in-flight create cannot be settled by
  resending. It is settled the way the QBO GraphQL path was: look for the
  name we planted under the contact, through every page, and never guess
  between two.

So a private CRM-to-Xero sync has exactly two honest options:

1. **Run on a plan that includes Projects**, with the `projects` scope
   granted, and create real Projects. The `xero_with_projects` fixture
   models that path.
2. **Accept that there is no project**, book the invoice and payments on the
   contact -- in Xero they hang off the contact regardless -- and make sure
   every record and every report says `unavailable`, so nobody reads
   "Project" off a dashboard and looks for it in Xero. The default fixture
   models that path, and the ledger field, the `CHECK` constraint and the
   surfaced notice are the whole point of it.

What is not an option is the third thing, which is what the
`silent-project-fallback` mutation does: record a project that was never
made, and let the difference be found by whoever opens Xero Projects first.

## Layout

```
xero_idempotent_sync_proof/
  ledger.py        # SQLite operational record: intent-before-send, derived keys and
                   # planted numbers, attempt counter (contact/invoice only), unique bindings,
                   # project_mode CHECK, schema_version upgrade of older files
  auth.py          # OAuth 2.0 refresh with rotation, persisted every time; granted scopes
                   # read from the access token's JWT claim
  client.py        # transport that keeps "failed" and "unknown" apart; xero-tenant-id and
                   # Idempotency-Key on every create; status before body (plain-text 404 =
                   # absent); 429 -> same request within the window; 401 -> refresh once;
                   # 5xx -> unknown; paged Projects lookup
  sync.py          # the step machine: contact -> project -> invoice -> payments -> read-back;
                   # interrogate-first recovery that walks on past any exception; the honest
                   # project path; the anti-patterns
  fake_xero.py     # Xero on a socket: six-minute key store with a clock, cached errors,
                   # different-request 400, unique Name/ContactNumber/InvoiceNumber, plain-text
                   # 404, payments on AUTHORISED only, gated and paged Projects, JWT-shaped
                   # tokens, rotation with grace, 429, faults
  _crash_child.py  # a worker killed for real with os._exit, mid-step
scripts/
  verify_the_suite_bites.py   # mutation harness (7 defects)
  live_smoke.py               # one find-or-create against the demo company, if configured
tests/
  conftest.py
  test_ledger.py
  test_fake_xero.py
  test_crash_child.py
  test_live_smoke.py
  test_scenario_1_contact_exactly_once.py
  test_scenario_2_projects_honest_path.py
  test_scenario_3_tokens_and_throttle.py
  test_scenario_4_payments_and_ledger.py
```

Python 3.10+. No runtime dependencies; `pytest` only to run the proof.

## Scope, honestly

The suite runs against a **local fake** that encodes the behaviours Xero
documents, linked below. It has **not yet been run against a Xero
organisation**, demo or otherwise, and this repo makes no claim of
production use. Specifically, the fake models these documented facts:

* `Idempotency-Key` on `POST`/`PUT`/`PATCH`: any string up to 128
  characters, stored for 6 minutes from the first call, the cached response
  replayed within that window, errors cached too, replays counted against
  the rate limit, the same key with a different request -> `400`
  "Idempotency Key: ... is used with a different request", scoped per app.
  [Idempotent requests](https://developer.xero.com/documentation/guides/idempotent-requests/idempotency/)
* Contact `Name` unique across active contacts -> `400` "The contact name
  ... is already assigned to another contact. The contact name must be
  unique across all active contacts."; `ContactNumber` API-only, unique, max
  50, readable with `GET /Contacts/{ContactNumber}`; `where=Name=="..."`
  and `includeArchived=true`.
  [Contacts](https://developer.xero.com/documentation/api/accounting/contacts)
* ACCREC `InvoiceNumber` unique -> "Invoice # must be unique."; `Reference`
  not unique; `DRAFT`/`SUBMITTED`/`AUTHORISED` on create, `AUTHORISED` never
  back to `DRAFT`; `AmountPaid`, `AmountDue` and `Payments[]` on the
  invoice read-back.
  [Invoices](https://developer.xero.com/documentation/api/accounting/invoices)
* A Payment applies to an `AUTHORISED` invoice: `Invoice.InvoiceID`,
  `Account.Code`, `Date`, `Amount` not above the amount outstanding,
  `Reference`.
  [Payments](https://developer.xero.com/documentation/api/accounting/payments)
* Projects: `POST` with `contactId` and `name`; `GET ?contactID=` paged at
  50 by default and 500 at most, with `pagination.pageCount`; scope
  `projects`.
  [Projects](https://developer.xero.com/documentation/api/projects/projects)
* OAuth 2.0: `POST https://identity.xero.com/connect/token`; the response
  carries `access_token`, `expires_in`, `token_type`, `refresh_token` (and
  `id_token` with OpenID) -- no `scope` field; the access token is a JWT
  that can be decoded; access token 30 minutes; refresh token 60 days,
  rotating on every refresh, the previous one valid for a 30-minute grace
  period; `xero-tenant-id` on every API call, resolved from
  `GET https://api.xero.com/connections`; `offline_access` for a refresh
  token.
  [Auth flow](https://developer.xero.com/documentation/guides/oauth2/auth-flow),
  [managing tokens](https://developer.xero.com/documentation/best-practices/data-integrity/managing-tokens)
* Rate limits: 60 calls per minute per tenant, 5 concurrent, 1,000 per day
  on the Starter plan (5,000 on higher plans), 10,000 per minute per app;
  `429` with `X-Rate-Limit-Problem` and `Retry-After`;
  `X-MinLimit-Remaining` and `X-DayLimit-Remaining` on every response.
  [Limits](https://developer.xero.com/documentation/guides/oauth2/limits)
* Scopes for an app created since 2026-03-02 (granular): `offline_access
  accounting.contacts accounting.invoices accounting.payments
  accounting.settings.read projects` -- `accounting.settings.read` is what
  `GET /Organisation` needs, and the `IsDemoCompany` guard of the live
  smoke reads it.
  [Scopes](https://developer.xero.com/documentation/guides/oauth2/scopes)
* A free Xero account comes with a demo company, which `GET /Organisation`
  reports as `IsDemoCompany: true`; its data is reset 28 days after
  creation.
  [Development accounts](https://developer.xero.com/documentation/development-accounts)

And it goes **beyond the documentation** in these places, each modelled on
the side that makes the client's job harder, and each worth checking against
the demo company before relying on it:

* an **archived** contact keeps its `Name` reserved (the message says
  "active contacts"; the harder reading is modelled);
* the wording of the duplicate-`ContactNumber` `400` (the docs say only that
  the PUT "will receive an error"); the fake's wording is switchable and the
  sync never keys on it;
* `GET /Contacts/{ContactNumber}` for a number nobody carries answers
  **404** (the docs say the number is a valid path key, not what a miss
  returns), and that 404 is served as **plain text** -- "The resource
  you're looking for cannot be found", the body Xero commonly sends -- so
  the client is shown to read the status, not the shape; anything other
  than a 404 is raised, never read as absent;
* the access token's granted scopes are a **`scope` claim, an array of
  strings**, in the JWT (the docs say the token is a decodable JWT and that
  the token response has no `scope` field; the claim's name and shape
  follow the IdentityServer convention and are not spelled out); when it
  cannot be read, the sync treats the scopes as unknown, not as none;
* `where=Name=="..."` is modelled **case-insensitive only**; the documented
  accent-insensitivity is not modelled;
* a quote inside a `where` string literal is escaped as `\"` (the escape
  syntax of the filter language is not documented);
* **project names are not unique** -- a duplicate is simply a second project;
* a `403` from `projects.xro` with the `projects` scope granted means the
  **plan** lacks Projects; the two 403s differ only in a body the client
  does not rely on;
* `Idempotency-Key` is honoured on the Projects API as it is on Accounting;
* inside the grace period, a refresh with the **retired** token rotates
  again and retires the *newest* token (the docs say the old one can be
  used for 30 minutes; what happens to the new one is not stated);
* the exact bodies of the `401`, `403` and `429` responses (a JSON problem
  shape here), and of the Projects API's `400`; the client branches on the
  status code for the first three and reads a body only on a `400`;
* the message for a payment on a non-`AUTHORISED` invoice and for a
  `ContactNumber` over 50 characters (neither is in the examples);
* a replay returns the original **status code** as well as the body.

Also deliberately not modelled: the 60-per-minute and 5-concurrent limits
themselves (throttling is armed per test, not counted), and the daily
limit. Deliberately not covered: the CRM side, webhook signature
verification, tax, items and chart-of-accounts configuration beyond one
sales account and one bank account, multi-currency, batch endpoints and
queue semantics -- those are engineering, not proof.

### LIVE

To run the one thing that can be run live without a paid plan -- a Contact
find-or-create with an `Idempotency-Key` and a planted `ContactNumber` --
against the Xero demo company:

1. A [Xero developer account](https://developer.xero.com/documentation/development-accounts)
   (free; new apps start on the Starter plan with no payment method) and the
   demo company that comes with it. The demo company resets 28 days after
   creation, and the app has to be reconnected after a reset.
2. An app with the scopes `offline_access accounting.contacts
   accounting.settings.read` (the demo-company check reads
   `GET /Organisation`), and a refresh token from your authorisation flow.
3. Environment variables: `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`,
   `XERO_REFRESH_TOKEN`; optionally `XERO_TENANT_ID` (otherwise resolved
   from `GET /connections`, provided exactly one organisation is
   connected), `XERO_BASE_URL` (default `https://api.xero.com`) and
   `XERO_IDENTITY_URL` (default `https://identity.xero.com`).
4. `python scripts/live_smoke.py` -- it refreshes (and persists the rotated
   refresh token to `live_smoke.db`), reads `GET /Organisation` and
   **refuses to continue unless `IsDemoCompany` is true** (override with
   `--i-know-this-is-not-a-demo`), creates one contact with a derived
   `Idempotency-Key` and `ContactNumber`, sends the identical request again,
   prints whether the two `ContactID`s match and whether the second response
   repeats the first (`UpdatedDateUTC` included -- Xero sends no replay
   marker, so identical responses are the only evidence), and finally reads
   the contact back by `GET /Contacts/{ContactNumber}`. Without the
   variables it prints a skip line and exits 0. Read the refresh token back
   from `live_smoke.db` for the next run: the one you passed in is dead
   thirty minutes after the first refresh.

A second run on the same day finds the name taken, looks the contact up by
its planted number, and reports the `ContactID` it already has -- the
six-minute key is gone by then, and the number is what settles it. The
Projects step is not exercised live: whether the demo company has Projects
is not verified, and the proof of that path is the fake's switchable gate.
