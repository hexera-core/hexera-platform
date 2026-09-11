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

## Connecting the mailbox

On a laptop this was `npm run gmail:auth`: a script that opened a browser, caught the redirect on
`localhost:5789` and wrote the token to SQLite. None of that survives Cloud Run — no browser to
open, no loopback port to catch, no filesystem that persists. Hosted, the consent round trip is two
route handlers:

| | |
|---|---|
| `GET /outreach/auth/start` | issues a random `state`, sets it as an `HttpOnly` cookie scoped to `/outreach/auth`, redirects to Google |
| `GET /outreach/auth/callback` | checks the returned `state` against that cookie, exchanges the code, reads the mailbox address from the token itself, seals and stores it |

Both **require a verified IAP assertion**, the same bar the Fleet page holds VM deletion to —
granting this app send-as rights over a mailbox is a privileged change, not a page view. Neither
route trusts the plain `X-Goog-Authenticated-User-Email` header.

The `state` check is what stops another site from handing a signed-in admin an authorization code
for an **attacker's** mailbox, which the console would otherwise store and start sending from.

The consent URL asks for `access_type=offline` **with** `prompt=consent`. Offline alone is not
enough: Google returns a refresh token only on a consent it considers new, so a mailbox that was
ever connected before comes back with an access token that expires in an hour and nothing to renew
it with. An exchange that yields no refresh token — and where none is already held — is **refused**
rather than stored, because storing it looks like success and then stops working at lunchtime.

The authorization code and both tokens never reach a log line, an audit entry or a redirect
parameter. The audit entry records who connected which mailbox with which scopes.

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
it needs a **web** client. Google shut down the OAuth client admin APIs, so this is a Cloud Console
action with no gcloud equivalent. Three things follow from it:

- Its **authorised redirect URI** must be exactly `https://admin.hexera.ai/outreach/auth/callback`
  — same scheme, no trailing slash. `deploy.yml` already pins this as `google_redirect_uri`.
- Its **client id** goes into `deploy.yml` as `google_client_id`, which is currently empty. It is
  the public half and travels as a plain environment value. Until it is set the Mailbox panel
  reports that it cannot connect rather than offering a button that fails.
- Its **client secret** goes into Secret Manager as `google-client-secret`:

```bash
printf '%s' 'THE-SECRET' | gcloud secrets create google-client-secret \
  --project hexera-prod --data-file=- --replication-policy=automatic
```

`create-admin-service.sh` grants the admin identity `secretAccessor` on it. No secret value ever
appears in `deploy.yml`, and `devtools/quality/check_deploy_secrets.py` is the gate that keeps it
that way.

**2. The data import.** The contact list, templates, campaigns and history live in one SQLite file
on one machine. The importer is written and tested; running it needs that file.

```bash
# a rehearsal first: it reports what it would write and rolls back
node apps/admin-console/import-outreach.js --from ~/hexera-ops/data/outreach.db --dry-run
node apps/admin-console/import-outreach.js --from ~/hexera-ops/data/outreach.db
```

A script and not a migration, deliberately: a migration runs everywhere and describes a change
every environment needs, while this moves data that exists on exactly one machine, once.

It **refuses to run twice**. The failure mode of a re-run is not an error — it is a duplicate
contact list, and a contact enrolled twice is a person emailed twice. `--force` empties the tables
first, for a half-finished import that has to be redone; it truncates rather than merging, because
merging two partial imports is guesswork about which side is right.

**`oauth_tokens` is deliberately not imported.** The token in the laptop's file is plaintext, and
importing it would write plaintext into the shared database — the sealing only happens on save.
Reconnect the mailbox from the Settings page instead: one click, and the token that lands is
sealed.

Identity sequences are reset at the end. Without that the first row written after the import
collides with an id the import already used.

## The database role, and a compromise

The `outreach` schema lives **inside `meshpipeline`** — alembic revision `0005` creates it there —
so outreach reaches the same Cloud SQL instance the rest of the platform does, over the Cloud Run
socket, and `OUTREACH_DB_HOST` doubles as that socket path.

It connects **as `meshpipeline`**, and that is a deliberate compromise rather than an oversight. A
dedicated `outreach` role restricted to its own schema is the right shape. What blocks it today is
that `hexera-prod-pg` has **no public IP**, so there is no path from a laptop or from CI on which to
run the `CREATE ROLE` and the `GRANT`s. Standing one up needs either the Cloud SQL proxy in the
deploy job or a grant step in the migrate job, and neither is a config edit. Until then the
outreach code runs with the same rights as the rest of the platform — the same trust boundary every
other service in this project already sits inside.

Both the console and the sender job get `--network`, `--subnet` and `--vpc-egress
private-ranges-only` alongside `--set-cloudsql-instances`. Against a private-IP instance the
Cloud SQL flag alone is not enough: the connector still needs a route into the VPC, and without it
every tick fails on connection timeout in a way that reads as "the database is down".

## Where things are

| | |
|---|---|
| Pages | `apps/admin-console/src/app/(admin)/outreach/` |
| Engine | `apps/admin-console/src/lib/outreach/` |
| Sender | `apps/admin-console/worker/outreach-worker.ts`, bundled to `outreach-worker.js` |
| Schema | `alembic/versions/0005_outreach_schema.py` |
| Provisioning | `deploy/gcp/scripts/create-outreach-worker.sh` |
