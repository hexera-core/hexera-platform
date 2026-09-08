# Admin console: operations, cost, customers and outreach — design

Date: 2026-09-08
Status: proposed
Scope: four sub-projects delivered as four PRs — **Admin-1** (the service, behind IAP),
**Admin-2** (platform ops pages), **Admin-4** (the outreach app, hosted), **Admin-3** (customer
pages). Built in that order; Admin-3 is last because it is the only one blocked on other work.

## 1. Why this exists

`apps/admin-console` is a stub page with a health route, deliberately left undeployed. Everything
we know about the running system is read by hand: fleet state from the Cloud console, spend from
the billing page, customer activity from `psql`, and the design-partner pipeline from a Next.js app
that runs on one laptop and is reachable from nowhere else.

The goal is one authenticated place that answers: what is the fleet doing, what is it costing, who
is using it, and where is the outreach pipeline. Access is a Google identity, not a shared
password.

### Verified starting facts

Read from this repository and from `hexera-core/hexera-ops` at `2026-09-08`, not from
documentation.

| Fact | Evidence |
|---|---|
| The admin console is a stub and is deployed nowhere | `apps/admin-console/src/app/(admin)/page.tsx`; no Cloud Run service; `deploy.sh` has no `admin` component |
| IAP runs directly on Cloud Run, with no load balancer | Google's IAP overview: "Enable IAP directly on your Cloud Run services… protects all ingress paths, including the auto-assigned URL" |
| Core IAP is free; only load balancing is charged | IAP pricing: "…at no charge. However, networking and compute charges apply for required load balancing." We use no load balancer |
| IAP and the Cloud Run invoker policy are separate gates | IAP on a service still bound to `allUsers` protects nothing — the public binding admits the request regardless |
| The IAP service agent needs an explicit invoker grant | `service-<PROJECT_NUMBER>@gcp-sa-iap.iam.gserviceaccount.com` requires `roles/run.invoker` |
| The public console is deliberately the opposite posture | `create-console-service.sh` step 6 binds `allUsers`, because its sign-in page must be reachable to sign in |
| Cloud SQL is private-IP only in both environments | `docs/deployment/environments-and-delivery.md` — prod `10.62.0.3`, no public IP |
| Chats and jobs are already durable and queryable | `chat_sessions`, `simulation_jobs` in `persistence/models.py` |
| Users, orgs and token usage do not exist yet | no `users`/`organizations` tables; `PLANS` is empty; `InferenceCall` is bound to a Redis sink with a 7-day TTL |
| The outreach app is a full Next 16 / React 19 app | `hexera-ops`: 8 pages — analytics, contacts, inbox, login, partners, queue, settings, templates |
| Its schema is 14 tables and has no migration versioning | `lib/db/schema.sql` (334 lines); `scripts/migrate.ts` re-applies `CREATE TABLE IF NOT EXISTS` |
| Its SQLite coupling is confined to one module | exactly one file imports `node:sqlite` (`lib/db/index.ts`), with 9 `db()` call sites |
| It stores Gmail refresh tokens in plaintext | `oauth_tokens.refresh_token TEXT` — long-lived, grants ongoing mailbox access |
| Sending is guarded by a two-part interlock | `DRY_RUN` env var **and** the `live_sending` setting must both agree before a message leaves |
| It has a background sender that is not a web request | `scripts/worker.ts`, run separately from the dashboard |
| Its SMTP probe needs outbound port 25 | `.env.example`: "most home ISPs and every cloud host block [it] by default" |
| Its auth is a shared password, blank by default | `DASHBOARD_PASSWORD`, documented as "fine on localhost" |
| Its runtime dependencies are light and pure-JS | `@anthropic-ai/sdk`, `googleapis`, `next`, `react`, `zod` — no native compilation |

## 2. Goals and non-goals

**Goals**

- One admin surface, reachable only by named Google identities, at no meaningful infrastructure
  cost.
- Fleet and cost visibility for both environments without opening the Cloud console.
- The design-partner pipeline off a single laptop and into a hosted, backed-up database.
- Gmail credentials that survive a database dump without becoming a mailbox compromise.

**Non-goals for this cycle**

- Per-admin permissions. Everyone IAP admits is an admin; distinguishing them is later work.
- Writing to infrastructure from the console. It reads fleet state; it does not scale, drain or
  restart anything.
- Replacing the Cloud console for billing analysis. The cost pages answer "roughly what, roughly
  where", not chargeback.
- Prod outreach. The outreach app is a single hosted instance, not a per-environment tier.
- Any change to the public console's posture. It stays `allUsers`-invokable.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | IAP directly on Cloud Run, no load balancer | The only thing IAP charges for is load balancing. Direct mode is free and protects the auto-assigned URL too. |
| 2 | The admin console takes a direct Postgres connection | Admin-4's logic is TypeScript. Reaching it through the Python API would mean rewriting it or building a relay API that only relays. |
| 3 | Outreach tables live in the same database and the same public schema, on the one alembic chain | Chosen by the user over a separate schema, database or instance. One migration authority, one connection. |
| 4 | Outreach tables are prefixed `outreach_` | Their current names include `events`, `settings`, `contacts` and `templates` — generic enough to collide with a future product table in a shared namespace. |
| 5 | Gmail tokens are envelope-encrypted with Cloud KMS before storage | A refresh token is long-lived mailbox access. Secret Manager is the wrong tool because the app *writes* these on every refresh. |
| 6 | No app-level auth in the admin console | IAP is the gate. An app password behind an identity gate is a second thing to leak and rotate. |
| 7 | The outreach sender runs as a Cloud Run Job, not in the web service | It is a background loop, not a request. Cloud Run scales the web service to zero; a sender living there would send only while someone had the page open. |
| 8 | Sending defaults to off after the move, and the two-part interlock is preserved | A hosted sender that starts unattended can burn the domain's reputation before anyone notices. |
| 9 | Admin-4 ships before Admin-3 | Admin-3 has nothing to read until B+D land; Admin-4 has no such dependency. |
| 10 | Build order Admin-1 → 2 → 4 → 3 | Admin-1 is the access boundary everything else sits behind. |

## 4. Admin-1 — the service, behind IAP

**Image and release.** A fourth release component `admin` beside `mesh`, `app` and `console`: a
Dockerfile target, built and smoked once by Gate C, published by digest, promoted as `ADMIN_IMAGE`.
The console's path exactly.

**Service.** `deploy/gcp/scripts/create-admin-service.sh`, modelled on `create-console-service.sh`
with four deliberate differences:

- `--no-allow-unauthenticated`, never the `allUsers` binding.
- `--iap`.
- The IAP service agent granted `roles/run.invoker`.
- VPC egress, because it opens Cloud SQL — which the public console does not.

A new `admin` value in `DEPLOY_COMPONENTS`, its stage after the API, stating its skip when
unselected.

**Database identity.** A dedicated Postgres role with write on `outreach_*` and read-only
elsewhere. The credential is a Secret Manager reference like every other.

**Shell.** A sidebar over five sections — Fleet, Costs, Activity, Customers, Outreach. Customers
renders an explicit "arrives with accounts and metering" state rather than an empty page.

**Verification.** The post-deploy step asserts what
[`docs/deployment/admin-console-access.md`](../../deployment/admin-console-access.md) §6 specifies:
an anonymous request must not return 200, and no `allUsers` binding survives. A 200 there is the
one failure that makes the whole exercise pointless.

## 5. Admin-2 — platform ops

| Page | Source |
|---|---|
| Fleet | Compute MIG API for `<env>-workers` — target size, per-instance status; Cloud Run for api/console revisions |
| Warm starts | `min-instances` per Cloud Run service and MIG target size, dev and prod side by side |
| Activity | `chat_sessions` and `simulation_jobs`, read directly |
| Costs | see below |

Infrastructure facts are read from the GCP APIs with the admin service account. They are not routed
through the product API, which has no business knowing about managed instance groups.

**Costs, honestly.** The Cloud Billing API returns budgets and forecasts, not per-service spend over
time. A real breakdown needs a **BigQuery billing export**, which is a project-level setting that
accumulates only from the day it is enabled and cannot be backfilled. Whether it is on is
**unverified** — see open question 1. If it is off, the first version shows current-month totals
from the Billing API and says so on the page, and the breakdown arrives once the export has run.
The page must not imply history it does not have.

## 6. Admin-4 — outreach, hosted

**Schema.** One alembic migration creates the 14 tables as `outreach_*`. `lib/db/schema.sql` and
`scripts/migrate.ts` are deleted; alembic becomes the authority, and the chain stays linear.

**Driver.** `node:sqlite` → `pg`. Confined to `lib/db/index.ts` and its 9 call sites. SQLite's
synchronous API becomes async, so the call sites change shape even where the SQL does not.

**Credentials.** `oauth_tokens.access_token` / `refresh_token` are envelope-encrypted with a Cloud
KMS key before they are written. A test asserts no plaintext refresh token is ever persisted.
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `APOLLO_API_KEY`, `VERIFIER_API_KEY` and
`ANTHROPIC_API_KEY` become Secret Manager references.

**OAuth flow.** `GOOGLE_REDIRECT_URI` is a desktop flow pointing at `localhost:5789`. Hosted, it
becomes a web flow with a redirect URI on the admin console's own origin, and the OAuth client type
changes accordingly.

**The sender.** `scripts/worker.ts` becomes a Cloud Run Job on a schedule, like the existing
queue-depth publisher. It shares the image and the database; it serves no traffic.

**Sending safety.** The `DRY_RUN` + `live_sending` interlock is preserved exactly, and both default
to "off" after the move. Turning sending on is a deliberate act in the hosted environment, not a
state inherited from a laptop.

**SMTP probe.** Stays disabled. Cloud Run blocks outbound port 25, so every result would be
"unknown"; `VERIFIER_PROVIDER` is the hosted path if verification is wanted.

**Auth.** `app/login/page.tsx` and `DASHBOARD_PASSWORD` are deleted. IAP replaces them.

**Data move.** A one-time import from the local `data/outreach.db` into Postgres, run once and kept
as a script rather than a migration — it moves data that exists on exactly one machine.

**Pages.** analytics, contacts, inbox, partners, queue, settings, templates, mounted under the
shell's Outreach section.

**Which environment.** Outreach is a single instance and it lives in **prod**, because the
design-partner list and the sending mailbox are real, not sample data, and a dev copy would either
duplicate them or sit empty. The dev admin console therefore renders the Outreach section as
unavailable rather than pointing at prod's tables. This is the one place the two admin consoles
deliberately differ.

## 7. Admin-3 — customers

Signups, per-organisation usage and token spend, read from B+D's `users`, `organizations`,
`memberships` and `inference_usage`. Thin reads over the same connection; the work is presentation,
not plumbing. Ships when those tables exist.

## 8. Testing

| Sub-project | Tests |
|---|---|
| Admin-1 | fake-gcloud stage tests as `tests/unit/deploy/` already does: IAP flag set, no `allUsers` binding, IAP service agent granted, refusal before mutation when a declared secret is absent. Post-deploy: anonymous request is not 200 |
| Admin-2 | fake GCP responses per page; a cost page that renders honestly when no billing export exists |
| Admin-4 | migration up/down; a round-trip test per ported query; an encryption test asserting no plaintext refresh token is written; an interlock test asserting a message cannot send unless both switches agree |
| Admin-3 | cross-org reads return nothing |

## 9. Risks

**The invoker binding.** IAP on a service that still carries `allUsers` protects nothing, and the
symptom is silence — the page loads, so it looks fine. This is the single failure most likely to go
unnoticed, which is why it is an automated post-deploy assertion rather than a step in a runbook.

**A hosted sender.** Cold email from a laptop that is closed most of the day is self-limiting. A
hosted worker on a schedule is not. Decision 8 exists for this, and the interlock must survive the
port intact.

**Gmail tokens.** Even encrypted, hosting them widens the blast radius of a database compromise
from "the contact list" to "the mailbox". KMS makes it a two-grant problem rather than a one-dump
problem; it does not make it free.

**Reading product tables from a second application.** A product migration that renames a column
Admin-2 reads will break the admin console, and nothing in the product's own tests will notice.
The read-only role limits the damage to a broken page, not corrupt data.

**Cost data may be thinner than expected** — see §5 and open question 1.

## 10. Open questions

1. Is a BigQuery billing export enabled on either project? It cannot be backfilled, so if the
   answer is no, enabling it now is worth doing before Admin-2 is built, independent of this work.
2. Which Google identity or group holds admin access, and is it the same for dev and prod? The
   access guide recommends a group; the membership is a decision.
3. Does the outreach app move to a dedicated sending mailbox as part of the hosting move, or keep
   whichever account is currently authorised?
