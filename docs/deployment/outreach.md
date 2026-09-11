# The outreach engine, hosted

The cold-outreach engine moved out of `hexera-core/hexera-ops` — a laptop-only Next app over a
local SQLite file — and into the admin console. This is what changed, what it costs, and the two
things that still have to be done by hand.

## What it is

Seven pages under `/outreach` in the admin console, a shared library, and a sender that runs as a
scheduled Cloud Run Job. It is **prod-only**: one partner list, one mailbox. A dev copy would
either duplicate the real contacts or sit empty, and neither is worth a second Gmail connection.
`OUTREACH_ENABLED` decides, so the section renders as unavailable everywhere else.

## The interlock

**A message leaves only when both switches agree.** Both default to off.

| Switch | Where | Who can flip it |
|---|---|---|
| `DRY_RUN=false` | the Cloud Run service and job | whoever can deploy |
| `live_sending=true` | the `outreach.settings` table | any admin, from the Settings page |

On a laptop switch 1 lived in `.env.local`. Hosted, the filesystem is ephemeral: that write would
succeed, the page would report sending armed, and the next container would start dry. So it is a
**deployment setting** here and the page refuses to pretend otherwise.

That makes the interlock *stronger*, not weaker: the two switches now need genuinely different
access. Arming a campaign is a database write; allowing the machine to send at all takes a deploy.

With either switch off the engine still verifies, still reads replies, and still renders every due
message so it can be read in the log — but it writes nothing and advances nothing. **A rehearsal
leaves no trace.**

## Why the sender is its own deploy tier

It is the only thing in this repository that can email a stranger, and arming it must never be a
side effect of shipping a page. `DEPLOY_COMPONENTS=outreach` is a separate checkbox, defaulted off
like every other.

Cold outreach from a laptop throttles itself — the machine is closed most of the day, and a mistake
stops when the lid does. A job on a schedule has no such property. Hence: provisioned `DRY_RUN=1`,
`--max-retries 1` (a tick that failed halfway has already sent what it sent; three retries invite a
duplicate), and a weekday business-hours schedule as a coarse guard beside the engine's own
per-campaign send windows.

## The mailbox credential

`outreach.oauth_tokens.refresh_token` is long-lived access to the sending mailbox. It is
**envelope-encrypted with Cloud KMS** before it is written and opened in exactly one place,
`storedToken`. Without that, a database dump is a mailbox compromise rather than a contact-list
leak.

It **fails closed**: with no `OUTREACH_KMS_KEY` configured the console refuses to store a token
rather than writing it in the clear. Values written before this existed — rows imported from the
laptop — carry no prefix, stay readable, and are re-sealed the next time they are saved.

The key is not rotated automatically. Rotating without re-encrypting the stored rows locks the
mailbox out, and re-encryption is a migration, not a schedule.

## The schema

14 tables in an `outreach` **schema**, not an `outreach_` name prefix. The prefix was the first
attempt and it makes every one of the ~100 SQL strings in the ported engine wrong;
`search_path = outreach, public` leaves them correct as written. It is also the privilege boundary
the console's database role is granted on.

Value semantics are preserved exactly — timestamps stay ISO-8601 text, booleans stay 0/1 integers —
because the port's risk was already dominated by turning a synchronous database API asynchronous
across 28 files. Types get modernised in their own revision.

Four pieces of SQL had to change, none of which a typechecker can catch:

| SQLite | Postgres |
|---|---|
| `COLLATE NOCASE` | `LOWER()` |
| `GROUP_CONCAT` | `STRING_AGG` |
| `julianday()` | `EXTRACT(EPOCH FROM …::timestamptz)` |
| `lastInsertRowid` | `RETURNING id` |

## Still to do by hand

**1. The OAuth client.** The engine used a *desktop* client redirecting to `localhost:5789`. Hosted
it needs a **web** client whose redirect URI is the admin console's own origin, set as
`GOOGLE_REDIRECT_URI`. Changing the client type is a Console action; there is no gcloud equivalent.

**2. The data import.** The contact list, templates, campaigns and history live in one SQLite file
on one machine. Moving them is a one-time script, deliberately not a migration — a migration runs
everywhere, and this data exists in exactly one place. It is the last phase and needs that machine.

## Where things are

| | |
|---|---|
| Pages | `apps/admin-console/src/app/(admin)/outreach/` |
| Engine | `apps/admin-console/src/lib/outreach/` |
| Sender | `apps/admin-console/worker/outreach-worker.ts`, bundled to `outreach-worker.js` |
| Schema | `alembic/versions/0005_outreach_schema.py` |
| Provisioning | `deploy/gcp/scripts/create-outreach-worker.sh` |
