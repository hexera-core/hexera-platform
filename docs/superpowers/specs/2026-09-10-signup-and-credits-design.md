# Sign up, sign in and a signup credit grant — design

Date: 2026-09-10
Status: approved
Scope: sub-project **B** of the SaaS console cycle, with its authentication provider changed from
hand-rolled password verification to Google Cloud Identity Platform, plus the credit *issuance*
half of buildout-plan item 4.

Supersedes: §5 of [`2026-09-07-saas-console-design.md`](2026-09-07-saas-console-design.md). That
section's schema, tenant scoping and backfill survive intact; its `/auth/verify`,
`/auth/register`, `password_hash` and sign-in-hardening paragraphs are replaced by §3 and §4 here.

## 1. Why this exists

The console authenticates against `CONSOLE_AUTH_USERS`: a JSON array of scrypt hashes in an
environment variable, parsed by `apps/console/src/lib/auth/credentials.ts`. There is no way to
create an account. Somebody with a shell and the deploy scripts adds you, or you do not have a
login.

This cycle makes sign-up a thing a stranger can do, gives every new account an organisation and a
starting balance of credits, and populates the tenant boundary the schema has been carrying an
empty column for since `0002_api_keys`.

**Spending is explicitly out of scope.** The ledger is built with the shape that debits, holds and
refunds will need, and nothing in this cycle writes anything but a grant. What a credit buys is
not decided here.

### Verified starting facts

Each read from the code in this repository on 2026-09-10, not from documentation.

| Fact | Evidence |
|---|---|
| Console accounts are an env var | `CONSOLE_AUTH_USERS` JSON parsed in `apps/console/src/lib/auth/credentials.ts`; `owner_id` is the user's email |
| The session is an Auth.js JWT | `apps/console/src/auth.ts` — Credentials provider, `session.strategy: "jwt"`, `subject` from `token.sub` |
| The proxy signs the owner into every API call | `apps/console/src/lib/product-api/proxy.ts` sets `x-user-id`, HMACs it into `x-user-sig`, presents `MESH_API_KEY` |
| Every credential resolves at one seam | `resolve_principal` in `api/security.py`, which its own comment names "THE ONE SEAM" and says will start filling `organization_id` when organisations arrive |
| The org column exists and is always empty | `Principal.organization_id` defaults `""` (`contracts/identity.py`); `api_keys.organization_id` is nullable with no FK |
| There is no user, org, membership or credit table | `alembic/versions/` holds `0001_schema_baseline` and `0002_api_keys` and nothing else |
| Seven tables carry `owner_id` and need an org column | `geometry_sources`, `simulation_jobs`, `chat_sessions`, `geometry_interpretations`, `capture_operations`, `artifact_reconciliations`, `source_object_cleanups` (`persistence/models.py`) |
| The tenant sweep is 50 + 13 sites | `grep -c owner_id persistence/repositories/` → 50; `grep -c owner_dep api/` → 13 |
| No email infrastructure exists | no SMTP, Resend, SendGrid, Postmark or Mailgun reference anywhere outside `sandbox/safe_exec.py` |
| Firebase ID tokens are verifiable with what we already ship | `google-auth==2.35.0` in `requirements/runtime.txt` exposes `google.oauth2.id_token.verify_firebase_token` |
| Identity Platform's API is not yet enabled | `deploy/gcp/scripts/enable-apis.sh` enables nine APIs; `identitytoolkit.googleapis.com` is not among them |
| The plan catalogue is a live, empty seam | `PLANS: dict[str, PlanOverride] = {}` (`settings/plans.py`), already read by the quota gate and the per-plan limiter |
| Migrations run before the new image | `deploy.sh` stage 220 (schema) precedes stage 245 (API service) |
| Console secrets are containers, never literals | `create-secrets.sh` declares `AUTH_SECRET` and `CONSOLE_AUTH_USERS`; `create-console-service.sh` binds them by reference |

## 2. Goals and non-goals

**Goals**

- A stranger can create an account from the console, verify their email, sign in, and reset a
  forgotten password.
- Every account gets exactly one organisation, one membership, and one signup credit grant,
  created atomically and exactly once.
- `organization_id` becomes real: populated on `Principal`, backfilled onto existing rows, and the
  column every tenant-scoped read filters on.
- A readable credit balance.
- `CONSOLE_AUTH_USERS` is deleted.

**Non-goals**

- Spending credits. No debits, no holds, no settle, no refunds, no enforcement, no balance gate on
  job submission. The ledger supports all of it and this cycle writes none of it.
- Pricing. What a credit is worth stays undecided; buildout-plan decision 3 (measured resource
  cost) is neither implemented nor contradicted here.
- Plan enforcement. `PLANS` stays empty; the organisation becomes where a plan would be resolved
  from.
- Multi-user organisations in the UI. `memberships` supports them from day one; a personal org
  shows one member.
- Changing your email address. See decision 4 — the UI does not offer it this cycle.
- SSO. Identity Platform makes Google sign-in a provider toggle; turning it on is a later decision.
- `admin-console`. Unchanged.
- The React rewrite (sub-project **C**). The legacy static client keeps working; only the
  authentication pages are new.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Google Cloud Identity Platform** holds credentials, not us | Sign-up, verification email, password reset, lockout and enumeration protection arrive finished. It removes the need to choose an email provider, which nothing in this repository currently has. |
| 2 | The API verifies ID tokens with `google-auth`, not `firebase-admin` | `google-auth==2.35.0` is already a runtime dependency and exposes `verify_firebase_token`. Zero new Python dependencies, and no service-account key — verification is against Google's public certs. |
| 3 | The product API still owns `users`, `organizations`, `memberships` and the ledger | Identity Platform owns the *credential*; the tenant record belongs in the same schema as the columns that scope on it, and its migrations belong in alembic. |
| 4 | `owner_id` stays the lowercased email; it is not re-keyed to the Firebase uid | Live rows are keyed by email today. Re-keying rewrites production data and loses continuity for existing accounts. Cost, accepted: an email change would drift `owner_id`, so the UI does not offer one this cycle. |
| 5 | Auth.js stays, wrapping Firebase | The Credentials provider's `authorize` changes; `auth()`, `ownerIdFromSession` and the whole proxy path do not. Firebase session cookies would be a larger diff for no gain here. |
| 6 | One organisation per user, auto-created, no org UI | Carried unchanged from the 09-07 design, decision 1. |
| 7 | The ledger is append-only with a derived balance | A counter leaks credits on every failure path, and the pipeline has several. Grants, debits, holds and refunds are all rows; the balance is `SUM(amount)`. |
| 8 | Credits are whole integers of an undenominated unit | Recording a dollar figure would pre-empt a pricing decision nobody has made. An integer count does not. |
| 9 | Backfilled users link by email on first sign-up | Otherwise a returning owner signs up and lands in a second, empty organisation while their jobs and geometry sit in the first. |
| 10 | `organization_id` ships nullable; `NOT NULL` is a follow-up `0005` | Carried unchanged from the 09-07 design, decision 10. `deploy.sh` migrates before the new image, so the old revision briefly serves against the new schema and its inserts must not fail. |

## 4. Authentication

### What Identity Platform holds

Email, password, `email_verified`, and the uid. It sends the verification and password-reset
emails. It applies its own per-account and per-IP throttling. It answers "does this email exist?"
with its own enumeration protection, which is why the finding on #13 — that `authorize` ran scrypt
for every attempt on a known email and so leaked account existence through response timing — has
no equivalent here. We never see a password.

### The console

Firebase Web SDK on the client drives three pages:

| Page | Calls |
|---|---|
| `/sign-up` | `createUserWithEmailAndPassword`, then `sendEmailVerification` |
| `/sign-in` | `signInWithEmailAndPassword` |
| `/forgot-password` | `sendPasswordResetEmail` |

Each ends by handing the resulting **ID token** to `signIn("credentials", { idToken })`. The
provider's `authorize` posts it to the product API and keeps `{ owner_id, organization_id,
email_verified }` in the Auth.js JWT. `ownerIdFromSession` continues to return the email, so
`proxy.ts` and every existing `/api/v1` call are unchanged.

An unverified email may sign in and is shown a persistent banner with a resend control. It is not
blocked, because blocking on delivery of an email through a `firebaseapp.com` sender is a support
burden with no security benefit at this stage — nothing costly is gated on it yet. When spending
lands, gating the *grant* on verification is the natural place, and this design leaves
`email_verified_at` populated so that choice stays available.

Firebase web configuration (`NEXT_PUBLIC_FIREBASE_API_KEY`, `_AUTH_DOMAIN`, `_PROJECT_ID`) is
public by design and is delivered as plain environment, not as Secret Manager references.

### The product API

One new endpoint, `POST /auth/session`, mounted outside `/api/v1` and reachable only by a caller
presenting `MESH_API_KEY` — the console already holds it. It is excluded from request-body
logging.

Given an ID token it:

1. Verifies signature, expiry and audience against the GCP project, using
   `google.oauth2.id_token.verify_firebase_token`. Google's signing certificates are cached with a
   TTL and the verification runs in a threadpool, because `google-auth`'s transport is synchronous
   and this route is not.
2. Resolves the user: by `firebase_uid`, else by normalised email — attaching the uid to that row
   when it matches a backfilled account (decision 9).
3. On a genuinely new uid, in **one transaction**: inserts `users`, `organizations`,
   `memberships` and one `credit_ledger` grant.
4. Updates `last_login_at`, and `email_verified_at` the first time the token asserts it.
5. Returns `{ user_id, owner_id, organization_id, email_verified }`.

It is idempotent under concurrent calls: `users.firebase_uid` is unique, and the insert path
handles the conflict by re-reading rather than failing. The grant is bound to organisation
creation inside that same transaction, so it cannot happen twice.

`CONSOLE_SIGNUP_ENABLED` gates whether a token for an unknown uid provisions or is refused. It is
an **API** setting, not a console one, declared alongside `SIGNUP_GRANT_CREDITS` in
`settings/policy.py` and `settings/inventory.py` (kind `bool`, default `true`). Putting it on the
API is what makes it a real gate: the console can only ever decline to *show* the sign-up form,
whereas anyone can create an Identity Platform account directly against the project's public web
API key and present the resulting token. The console reads the same value through
`GET /api/v1/client-config` to decide whether to render `/sign-up`, so the two cannot disagree.

## 5. Schema — `0003_identity_and_credits.py` and `0004_tenant_columns.py`

Two revisions, not one. `0003` creates the four new tables, which reference nothing existing and
so are reversible on their own terms exactly as `0002` was. `0004` adds `organization_id` to the
existing tables and runs the backfill, which is the only part that touches live rows. Splitting
them means the risky half can be reviewed, rehearsed and rolled back without the safe half moving,
and `0003` can ship while `0004` is still being rehearsed. The `NOT NULL` follow-up becomes `0005`.

### `0003` — the new tables

- **`organizations`** — id, name, slug (unique), created_at.
- **`users`** — id, `firebase_uid` (unique, nullable for backfilled rows), email (unique,
  lowercased), name, `email_verified_at`, `last_login_at`, created_at. **No `password_hash`,
  `failed_attempts` or `locked_until`** — decision 1 moved all three out of our schema.
- **`memberships`** — user_id, organization_id, role (`owner`|`member`), unique(user_id,
  organization_id).
- **`credit_ledger`** — id, organization_id FK, `entry_type` (`grant`|`debit`|`refund`), `amount`
  BigInteger, `reason` String, `created_at`. Indexed on `(organization_id, created_at)`.
  Append-only: no update or delete path is written, and the repository exposes none.
### `0004` — the tenant columns and the backfill

- `organization_id uuid NULL REFERENCES organizations(id)` added to the seven tables named in §1.
  `api_keys.organization_id` gains the FK its `0002` comment promised.
- Composite indexes mirroring today's owner indexes, keyed on `organization_id`.

**Backfill**, in this revision: one organisation, one user and one membership per distinct
existing `owner_id`; then `organization_id` stamped on every existing row from its `owner_id`.
Backfilled users get `firebase_uid = NULL` and **no credit grant** — a grant is for a new signup,
and issuing one here would silently hand balances to accounts that predate the feature. The
migration is idempotent and must be rehearsed against a restored copy of the dev database.

## 6. Tenant scoping

`Principal.organization_id` is populated for real. For a bearer key it comes from the row, which
already carries the column. For the console's signed-header credential it is resolved from
`owner_id` through `memberships`, which costs one indexed read per request; the resolution lives
behind a single function so a cache can be added later without touching call sites.

A new `org_dep` joins `owner_dep` in `api/security.py`. The repositories gain an org filter across
their 50 `owner_id` sites and the 13 `owner_dep` route sites are updated with them.

**The rule: reads scope on `organization_id`; writes stamp both.** A read whose principal has no
organisation — which no post-backfill caller has — falls back to `owner_id` alone rather than
returning nothing, so a half-migrated deployment degrades to today's behaviour instead of looking
like data loss.

## 7. Credits

`application/credit_service.py`, mirroring the shape of `api_key_service.py`:

- `grant(db, *, organization_id, amount, reason)` — appends one row.
- `balance(db, *, organization_id)` — `SUM(amount)`, `0` for an organisation with no rows.

The signup amount is a new certified setting `SIGNUP_GRANT_CREDITS`, declared in
`settings/policy.py` and `settings/inventory.py` (kind `int`, default `100`, exposure `template`),
so Gate A's configuration certification covers it like every other knob. Setting it to `0`
disables the grant without a code change.

`GET /api/v1/credits` returns `{ balance }` for the principal's organisation. The console renders
it as a header chip beside the existing API-status chip.

Nothing debits. That is the whole of item 4 this cycle delivers.

## 8. Deployment

- `identitytoolkit.googleapis.com` added to `enable-apis.sh`.
- **Identity Platform must be initialised once per project** (`hexera-dev`, later `hexera-prod`).
  This is a console step that `gcloud` does not cover; `deploy-preflight.sh` gains a read-only
  check that reports it as absent rather than letting the console fail at first sign-up.
- Console env: `NEXT_PUBLIC_FIREBASE_API_KEY`, `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN`,
  `NEXT_PUBLIC_FIREBASE_PROJECT_ID`. API env: `FIREBASE_PROJECT_ID` (the audience it verifies
  against), `CONSOLE_SIGNUP_ENABLED` and `SIGNUP_GRANT_CREDITS`.
- `CONSOLE_AUTH_USERS` is removed from `create-secrets.sh`, from `create-console-service.sh`, from
  `apps/console/.env.example` and from the repository. `apps/console/src/lib/auth/hash-password.cli.ts`
  and the `auth:hash` script are deleted with it.
- Verification and reset emails send from `<project>.firebaseapp.com` until a custom sender domain
  is configured. Configuring one is not in this cycle.

## 9. Testing

| Area | Tests |
|---|---|
| Migration | up/down; backfill over seeded multi-owner data; re-running the backfill changes nothing |
| Tenant scoping | a cross-org read returns nothing; a principal with no organisation still reads its own rows |
| `/auth/session` | a new uid provisions exactly one org, membership and grant; a repeat call provisions nothing further; concurrent first calls yield one organisation; a backfilled email links rather than duplicating; an expired, wrong-audience or forged token is refused; `CONSOLE_SIGNUP_ENABLED=0` refuses an unknown uid and still admits a known one |
| Credits | balance is the sum of the ledger; an org with no rows reads `0`; the grant honours `SIGNUP_GRANT_CREDITS`, including `0` |
| Console | `authorize` maps a verified token to a session and a refusal to `null`; the sign-up, sign-in and reset pages render and submit; the `web` CI lane (typecheck, lint, test, build) |

Firebase itself is not under test. The console tests stub the Web SDK and the API tests inject a
verifier, so neither suite needs a live Identity Platform project.

## 10. Risks

**The backfill runs against live prod data.** Idempotent by construction and rehearsed against a
restored copy before it goes near prod, per the 09-07 design's own risk register.

**`owner_id` remains the email** (decision 4). If an email change ships before `owner_id` is
re-keyed, that account's history detaches. The mitigation is that the UI offers no email change;
the durable fix is a later re-keying migration, and `users.id` exists so that migration has a
stable target.

**One extra read per signed-header request** for the organisation lookup. Indexed and behind a
single function, but it is on the hot path of every console call and should be measured on dev
before prod.

**Identity Platform initialisation is manual.** A project where it was never initialised gets a
console that renders sign-up and fails on submit. The preflight check exists to make that loud.

**`main` has no required checks.** Carried forward from #12 and #13: `deploy.yml`'s `await-ci` is
still the only thing between a failing suite and a deployed artifact.

## 11. Open questions

1. Whether prod ships with `CONSOLE_SIGNUP_ENABLED=0` and accounts created by hand, or open from
   the first release. Affects nothing in the code — it is a deploy-time value — but it should be a
   deliberate choice rather than a default.
2. Whether the signup grant should later be gated on email verification. Deliberately left open;
   `email_verified_at` is populated so it stays a one-line change.
3. What a credit is worth. Out of scope by construction, and the ledger's `amount` is an integer so
   the answer can arrive without a migration.
