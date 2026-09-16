# Identity Platform: initialising it, configuring it, and retiring what it replaced

How the console's sign-in provider gets turned on for a project, what an operator must do by hand
that `gcloud` cannot do for them, where the console's public web configuration comes from, and the
two follow-on operator actions (the `0004` backfill, retiring `console-auth-users`) that this
cycle's design left for deploy time.

Design: [`2026-09-10-signup-and-credits-design.md`](../superpowers/specs/2026-09-10-signup-and-credits-design.md),
§4 (authentication), §5 (schema), §7 (credits), §8 (deployment), §10 (risks).

---

## 1. What Identity Platform holds, and why the API also has a table

Google Cloud Identity Platform holds the credential: email, password hash, `email_verified`, and
the Firebase uid. It sends the verification and password-reset emails, and applies its own
per-account and per-IP throttling. **We never see a password.**

The product API holds the account: `organizations`, `users`, `memberships` and `credit_ledger`
(migration `0003_identity_and_credits`). The console's Firebase Web SDK signs a user in against
Identity Platform and hands the resulting ID token to `POST /auth/session`, which verifies it and
provisions (or looks up) the organisation-side row. Identity Platform never sees `owner_id`,
credits, or which organisation a user belongs to; the API never sees a password. Losing either
side without the other leaves a login with no account, or an account nobody can sign in to.

## 2. One-time initialisation, per project

**Enabling `identitytoolkit.googleapis.com` is not the same as initialising Identity Platform.**
`deploy/gcp/scripts/enable-apis.sh` turns the API on as part of every deploy; that is necessary but
not sufficient. Identity Platform itself — the product, with a configured provider — is a
one-time, per-project action that only the Cloud Console can do. There is no `gcloud` command that
performs it: on the SDK version this repository's tooling was checked against (582.0.0),
`gcloud identity-platform` is not a recognised command group at all, and even
`gcloud alpha identity-platform` requires installing the alpha component first.

**Until this is done, the console renders the sign-up, sign-in and forgot-password pages and every
one of them fails on submit.** The failure is silent at build and deploy time — nothing refuses to
start — and shows up only when a real person tries to use the form. This is exactly the gap
`deploy/gcp/scripts/enable-apis.sh` now reports after it enables the API (see §2a below for why
that note lives there and not in `deploy-preflight.sh`). It is emitted at `info`, not `warn`: the
CLI cannot observe whether the project is initialised, so no correct state can silence it, and a
`warn` on every deploy regardless of whether anything is wrong is what teaches operators to ignore
the ones that mean something.

Do this once per project, before the console is expected to accept a sign-up:

1. Open **`https://console.cloud.google.com/customer-identity/providers?project=<PROJECT_ID>`**
   (substitute `hexera-dev` or `hexera-prod`).
2. If prompted, initialise Identity Platform for the project.
3. Enable the **Email/Password** provider. No other provider is used.

There is nothing else to configure in the console for this cycle — no custom sender domain, no
extra provider, no SMS.

### 2a. Why the automated check lives in `enable-apis.sh`, not `deploy-preflight.sh`

The signup-and-credits design (§8) originally asked for this check in
`deploy/gcp/scripts/deploy-preflight.sh`. That script does not parse:

```
$ bash -n deploy/gcp/scripts/deploy-preflight.sh
deploy/gcp/scripts/deploy-preflight.sh: line 251: unexpected EOF while looking for matching `)'
deploy/gcp/scripts/deploy-preflight.sh: line 337: syntax error: unexpected end of file
```

**This is a pre-existing defect, unrelated to this plan**, present since `d8811ec` ("Stand up
hexera-dev on GCP", #5). It is not a logic bug: several of the script's
`$("${PY}" - <<'PY' ... PY)` blocks — inline Python invoked from inside a command substitution —
trip a long-standing parser defect in bash below 4.x, where a heredoc body nested inside `$(...)`
can desynchronise the substitution's own quote/paren tracking. It is confirmed present with
content as small as a single embedded regex literal, and confirmed **absent** under bash 5
(`docker run --rm bash:5 bash -n deploy/gcp/scripts/deploy-preflight.sh` parses cleanly). macOS
ships bash 3.2.57 as `/bin/bash` (frozen there since Snow Leopard over the GPLv3 relicensing), so
this breaks for exactly the operators most likely to run `make mesh-preflight` from their own
laptop, while it would parse fine on a Linux CI runner.

Appending a check to a file that fails to parse produces a check that never runs, on the same
shell that fails to parse it — the whole file fails as one unit; bash refuses to run any of a
script with a syntax error anywhere in it, reached or not. Fixing the underlying defect would mean
rewriting several inline-Python delivery mechanisms in a file neither this task nor its author
built, purely to work around an ancient shell's parser — a wider, riskier change than this task's
scope, on a script that gates real deployment promotion decisions. **That fix is left for its own,
separate piece of work.**

The check instead lives in `deploy/gcp/scripts/enable-apis.sh`, which parses cleanly under both
bash versions and already runs on every deploy (`deploy.sh` calls it directly, both from
`make deploy` and from `.github/workflows/deploy.yml`) — unlike `deploy-preflight.sh`, which is
wired only to the standalone `make mesh-preflight` gate and is not part of the deploy path at all.
It is read-only exactly as the design asked: it enables the API, and separately reports (never
provisions) that initialisation cannot be confirmed automatically, with the console URL from §2.
It reports at `info` — see §2 for why a standing note about a manual step is not a `warn`.

## 3. Where the console's web configuration comes from

The console reads three settings client-side to talk to Identity Platform directly from the
browser:

- `NEXT_PUBLIC_FIREBASE_API_KEY`
- `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`
- `NEXT_PUBLIC_FIREBASE_PROJECT_ID`

**All three are public by design**, `NEXT_PUBLIC_*` in Next.js meaning "shipped in the client
bundle, readable by anyone who opens the page." This is not a leak: a Firebase web API key is not a
secret credential — it identifies which project a request is for, and every security decision
(who may sign in, what a token proves) is enforced server-side, by Identity Platform itself and by
the API's `POST /auth/session` verification. They are delivered as plain environment variables to
the console's Cloud Run service, never as Secret Manager references — there is nothing in them for
a reference to protect.

The API side verifies ID tokens against `FIREBASE_PROJECT_ID` (§5 below): the two must name the
same Identity Platform project, or every token the console mints will fail verification with a
wrong-audience error.

## 4. The sender domain

Verification and password-reset emails send from **`<project>.firebaseapp.com`** — Identity
Platform's default sender — until a custom domain is configured. **Configuring a custom sender
domain is out of scope for this cycle.** Users will see mail from
`hexera-dev.firebaseapp.com` / `hexera-prod.firebaseapp.com` rather than from a `hexera.ai`
address; this is a known, accepted limitation, not a defect to chase during this deploy.

## 5. Opening and closing signup: `CONSOLE_SIGNUP_ENABLED`

`CONSOLE_SIGNUP_ENABLED` (default `true`) gates whether an ID token for a uid the API has never
seen provisions a new organisation, or is refused.

**It is an API setting, passed to the API service by `deploy/gcp/scripts/create-api-service.sh`,
never a console one.** This is deliberate: the console can only ever decline to *render* the
`/sign-up` page. Anyone can create an Identity Platform account directly against the project's
public web API key (§3 — it is public by design) and present the resulting ID token to
`POST /auth/session` without ever loading the console's sign-up form. Only a check at the point
that provisions an organisation is a real gate. The console reads the same value through
`GET /api/v1/client-config` so its own sign-up page and the API's actual behaviour cannot disagree.

To close signup on a running deployment, set `CONSOLE_SIGNUP_ENABLED=false` in the deployment
environment and redeploy the API. Existing accounts continue to sign in; only provisioning a new
one is refused.

## 6. The signup credit grant: `SIGNUP_GRANT_CREDITS`

`SIGNUP_GRANT_CREDITS` (default `100`) is the number of credits granted, once, to a newly
provisioned organisation, at the same time `POST /auth/session` creates it. It is passed to the API
service the same way as `CONSOLE_SIGNUP_ENABLED`, above.

**Nothing in this cycle spends a credit.** The ledger (`credit_ledger`) and `GET /api/v1/credits`
exist so a balance can be issued and read; debiting against usage is future work. Setting
`SIGNUP_GRANT_CREDITS=0` disables the grant without a code change — new organisations are still
created normally, just with a starting balance of zero.

## 7. The `0004` backfill

`alembic/versions/0004_tenant_columns.py` does two things in one revision: it adds a nullable
`organization_id` column (plus an index and an FK) to seven existing tables, and it backfills that
column — and mints one organisation, user and membership per distinct pre-existing `owner_id` — in
the same migration run.

**It builds seven indexes without `CREATE INDEX CONCURRENTLY`, on live tables:**

```
geometry_sources, simulation_jobs, chat_sessions, geometry_interpretations,
capture_operations, artifact_reconciliations, source_object_cleanups
```

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction, and every migration in this repository
runs inside one (alembic's default, relied on elsewhere for atomicity). Each of the seven
`CREATE INDEX` statements therefore takes a write lock on its table for the duration of that
index's build. **Before running `0004` against prod, check row counts on the largest of the seven**
— on a table with a non-trivial number of rows this is a real write stall, not a formality, and it
should be sized and, if necessary, scheduled for a quiet period rather than discovered live.

**It is idempotent** — re-running it changes nothing, verified by running the backfill twice
against seeded multi-owner data and comparing counts — but idempotent is not the same as safe to
run unrehearsed. Rehearse it in this order:

1. Restore a copy of the **dev** database (not dev itself) from a recent backup.
2. Run `0004` against the restored copy. Confirm: one organisation/user/membership per distinct
   `owner_id` that existed before, every one of the seven tables' rows stamped with the right
   `organization_id`, and `api_keys.organization_id` stamped and FK'd.
3. Run it a second time against the same restored copy. Confirm the counts from step 2 are
   unchanged — this is the idempotency check, and it should be a comparison of actual numbers, not
   an assumption.
4. Only then run it against **dev** for real.
5. Only after dev has run it and been observed working does it go anywhere near **prod** — sized
   and scheduled per the write-stall note above.

### 7a. Before `0005` adds `NOT NULL`

`organization_id` ships nullable and a follow-up `0005` closes it (design decision 10). **Re-run
`0004`'s stamping `UPDATE`s immediately before adding the constraint, and check that nothing is
left unstamped afterwards.** The backfill is a one-shot: it stamps the rows that existed when it
ran, and nothing re-stamps a row written later. Two things can produce an unstamped row after it:

- the deliberate window between stage 220 (migrate) and stage 245 (deploy the API), where the old
  revision serves against the new schema and writes rows naming no organisation — this is why the
  column is nullable in the first place;
- a transient failure of the organisation lookup in `api/security.py::_organization_for`. It fails
  open to owner scope rather than refusing a request whose caller is already proven, so any write
  in that request is stamped with the owner alone. It logs at `error` for exactly this reason.
  Search for `could not resolve an organisation for` to find out whether it has happened.

Either leaves rows that `0005` will refuse, and — more importantly — rows that no org-scoped read
can see. The repair is the stamping half of `0004`, which is idempotent and safe to run on its own:

```sql
-- for each of the seven tenant tables, and then api_keys
UPDATE <table> SET organization_id = g.id
FROM organizations g
WHERE g.slug = 'backfill-' || md5(lower(<table>.owner_id))
  AND <table>.organization_id IS NULL
  AND <table>.owner_id IS NOT NULL AND <table>.owner_id <> '';
```

A row whose `owner_id` post-dates the backfill has no `backfill-` organisation to match, so check
for survivors (`SELECT count(*) ... WHERE organization_id IS NULL`) before adding the constraint
and mint the missing organisations rather than letting `0005` fail.

## 8. Retiring `console-auth-users`

`CONSOLE_AUTH_USERS` and its backing secret, `console-auth-users`, are gone from the code
(`create-secrets.sh`, `create-console-service.sh`, `apps/console/.env.example`) as of this cycle's
earlier tasks. The secret container itself may still exist in Secret Manager from before this
change shipped — nothing in this deploy deletes it automatically, and that is deliberate.

**Retiring it is an operator action, taken after the fact, once the new console revision is
confirmed serving sign-in through Identity Platform:**

```bash
gcloud secrets delete console-auth-users --project <PROJECT_ID>
```

Run this only once the new revision is live and verified — never before, and never as a step a
provisioning script performs automatically. The old secret, while it still exists, is the rollback:
if the new console revision misbehaves, the previous revision (or a redeploy pinned to the old
image) can still read it. A script that deleted it on its next run would remove the rollback at
exactly the moment a bad deploy might need it.
