# Outreach into the admin console (Admin-4) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Move the cold-outreach engine from a laptop-only Next app (`hexera-core/hexera-ops`) into the admin console, hosted, IAP-gated, with its data in Postgres and its sender running as a scheduled Cloud Run Job.

**Architecture:** The 14 SQLite tables become `outreach_*` tables on the product's single alembic chain. `lib/db/index.ts` — the one module that touches SQLite — is reimplemented over `pg`, which turns its six synchronous functions asynchronous and ripples an `await` through every call site. The seven pages mount under the console's Outreach section; the login page is deleted because IAP is the gate. Gmail tokens are envelope-encrypted with Cloud KMS before they are written.

**Tech Stack:** Next 16 App Router, React 19, TypeScript 6, `pg`, `@google-cloud/kms`, `googleapis`, `@anthropic-ai/sdk`, `zod`, alembic, Cloud Run Jobs, Cloud Scheduler.

**Spec:** `docs/superpowers/specs/2026-09-08-admin-console-design.md` §6 (Admin-4). Read it first; this plan implements it and records where it departs.

## Measured starting facts

Read from a tarball of `hexera-core/hexera-ops` at `2026-09-02`, and from this repo, on 2026-09-10.

| Fact | Number |
|---|---|
| TypeScript/TSX/SQL to port | ~15,500 lines |
| `app/` (7 pages + actions) | 2,991 lines, 11 files |
| `lib/` (engine, mail, ingest, prospect, verify, analytics, inbox, core) | 8,082 lines, 34 files |
| `scripts/` (worker, migrate, gmail-auth, ingest, seed, enrich, prospect, verify, …) | 1,677 lines, 13 files |
| `components/` | 1,675 lines, 7 files |
| Tables | 14 |
| Files touching the DB API | 28 |
| Alembic head to build on | `0002_api_keys` |

**The DB API is narrow and entirely synchronous** — `all`, `get`, `run`, `scalar`, `transaction`, plus `db`, `migrate`, `closeDb`. That narrowness is what makes this tractable; the synchrony is what makes it expensive. `pg` is async, so every one of those becomes a promise and 28 files change shape even where their SQL does not.

## Global Constraints

- **Sending stays off until a human turns it on.** The `DRY_RUN` env var and the `live_sending` setting must BOTH agree before a message leaves, and both default to off after the move. A hosted sender that starts unattended can burn the domain's reputation before anyone notices.
- **No plaintext refresh token is ever written.** A test asserts it.
- **Outreach is prod-only.** Dev's console renders the section unavailable. One partner list, one mailbox.
- **The SMTP probe stays disabled.** Cloud Run blocks outbound port 25, so every result would be "unknown".
- **Nothing is hard-deleted.** Contacts are suppressed, enrollments stopped. Preserve that.
- **One alembic chain, linear.** `lib/db/schema.sql` and `scripts/migrate.ts` are deleted; alembic becomes the authority.
- **Tables are prefixed `outreach_`.** `events`, `settings`, `contacts` and `templates` are too generic for a shared namespace.

---

## Phase 1 — Schema

- [x] **1.1** Write `alembic/versions/0003_outreach_schema.py` creating all 14 tables as `outreach_*`, `down_revision = "0002_api_keys"`.
- [x] **1.2** ~~Translate SQLite types~~ **DEPARTED, deliberately.** Only `INTEGER PRIMARY KEY AUTOINCREMENT` → `BigInteger` identity, because that one has no Postgres spelling. Timestamps stay `TEXT` holding ISO-8601 and booleans stay `INTEGER` holding 0/1.

  The reason is that this port's risk is already dominated by turning a synchronous database API asynchronous across 28 files. Adding a representation change on top interleaves two classes of breakage, and the failure it produces — a `Date` where a string was expected, rendering as `Invalid Date` — is exactly the kind that survives review and reaches production. ISO-8601 sorts lexically, so `<`, `>` and `ORDER BY` mean the same thing on `TEXT` in Postgres as they did in SQLite; nothing behaves differently. Types get modernised in their own revision once the engine change has settled, where the diff is about types and the tests can be about types.
- [x] **1.3** Carry every index, including the partial unique on `contacts(email_normalized) WHERE email_normalized IS NOT NULL` — many rows legitimately have no email and NULLs must not collide.
- [x] **1.4** Carry foreign keys. SQLite had `PRAGMA foreign_keys=ON`; Postgres enforces them always, so a migration that drops one changes behaviour silently.
- [x] **1.5** Verified against a real Postgres 16: upgrade → downgrade → re-upgrade. 14 tables, 46 indexes, 13 foreign keys, 1 view; downgrade leaves the baseline's 12 tables untouched. Two NULL-email contacts admitted, a duplicate rejected by `ix_outreach_contacts_email`, and a suppression survived deletion of its contact with `contact_id` set to NULL. Hermetic invariant tests live in `tests/unit/migrations/test_outreach_schema_revision.py`; the round trip itself is not in CI because it needs a live database.
- [x] **1.6** Commit.

## Phase 2 — The database layer

**Phase 1 is complete.**

- [ ] **2.1** Add `pg` and `@types/pg`. Reimplement `lib/db/index.ts` as `apps/admin-console/src/lib/outreach/db.ts` over a `pg.Pool`, same five-function shape, all returning promises.
- [ ] **2.2** Rewrite placeholders: SQLite `?` → Postgres `$1..$n`. Do it in the wrapper, not at 100+ call sites.
- [ ] **2.3** `run()` returned `lastInsertRowid`. Postgres has no equivalent — append `RETURNING id` and return that. Every INSERT whose id is used must be found and changed; a wrapper that returns 0 silently breaks enrollment.
- [ ] **2.4** `transaction()` becomes async and must take a dedicated client from the pool. Running a transaction on the pool interleaves statements from other requests and the failure is intermittent.
- [ ] **2.5** Timestamps: SQLite stored ISO strings so lexical order equalled chronological order and plain `<`/`>`/`ORDER BY` worked. With real `timestamptz` that still holds, but any SQL doing string comparison on a date must be found.
- [ ] **2.6** Tests against a real Postgres for each function, plus one asserting `?`→`$n` rewriting handles a literal `?` inside a string.
- [ ] **2.7** Commit.

## Phase 3 — Call sites

- [ ] **3.1** Port `lib/core`, `lib/analytics`, `lib/inbox`, `lib/ingest`, `lib/prospect`, `lib/verify` to `await` the new API.
- [ ] **3.2** Port `lib/engine` — `scheduler`, `enroll`, `dispatch`, `sender`, `state`. This is the sending path; treat it as the highest-risk file set.
- [ ] **3.3** Port `lib/mail` — `gmail`, `mime`, `render`.
- [ ] **3.4** Typecheck is the gate: a missed `await` on a promise-returning call is a type error, not a runtime surprise. Do not loosen types to move faster.
- [ ] **3.5** Commit per area.

## Phase 4 — Credentials

- [ ] **4.1** Add `@google-cloud/kms`. Envelope-encrypt `oauth_tokens.access_token` and `refresh_token` before write, decrypt on read.
- [ ] **4.2** Provision the KMS keyring and key in `deploy/gcp/scripts/`; grant the admin identity `cloudkms.cryptoKeyEncrypterDecrypter` and nothing wider.
- [ ] **4.3** Test asserting no plaintext refresh token is ever persisted.
- [ ] **4.4** `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `APOLLO_API_KEY`, `VERIFIER_API_KEY`, `ANTHROPIC_API_KEY` become Secret Manager references following the `<SETTING>_SECRET` convention. `devtools/quality/check_deploy_secrets.py` is the gate.
- [ ] **4.5** OAuth: `GOOGLE_REDIRECT_URI` is a desktop flow at `localhost:5789`. It becomes a web flow on the admin console's own origin, and the OAuth client type changes with it.
- [ ] **4.6** Commit.

## Phase 5 — Pages

- [ ] **5.1** Mount analytics, contacts, inbox, partners, queue, settings, templates under `(admin)/outreach/`.
- [ ] **5.2** Delete `app/login/page.tsx` and `DASHBOARD_PASSWORD`. IAP is the gate; an app password behind an identity gate is a second thing to leak and rotate.
- [ ] **5.3** Any mutation reachable by POST verifies the IAP assertion, exactly as the Fleet controls do — reuse `verifyIapActor`.
- [ ] **5.4** Port `components/` and fold the outreach CSS into the console's own, keeping the existing class conventions.
- [ ] **5.5** `sections.ts`: Outreach becomes available in prod and stays unavailable in dev.
- [ ] **5.6** Commit per page.

## Phase 6 — The sender

- [ ] **6.1** `scripts/worker.ts` becomes a Cloud Run Job on a schedule, modelled on `create-queue-depth-publisher.sh`. It shares the image and the database and serves no traffic.
- [ ] **6.2** Preserve the two-part interlock exactly. Test: a message cannot send unless `DRY_RUN` is off AND `live_sending` is on.
- [ ] **6.3** Both default to off after the move. Turning sending on is a deliberate act in the hosted environment, not a state inherited from a laptop.
- [ ] **6.4** Commit.

## Phase 7 — Infrastructure

- [ ] **7.1** `create-admin-service.sh` gains VPC egress and a Cloud SQL connection. Its header currently states it has neither and why; that comment is the thing to update, not delete.
- [ ] **7.2** A dedicated Postgres role with write on `outreach_*` and read-only elsewhere. The credential is a Secret Manager reference.
- [ ] **7.3** Fake-gcloud stage tests in `tests/unit/deploy/`.
- [ ] **7.4** Commit.

## Phase 8 — Data

- [ ] **8.1** A one-time import from the local `data/outreach.db` into Postgres. Kept as a script, not a migration — it moves data that exists on exactly one machine.
- [ ] **8.2** Verify row counts per table match before and after.
- [ ] **8.3** Commit.

## Definition of done

- [ ] `pnpm typecheck`, `lint`, `test`, `build` clean
- [ ] `pytest tests/unit/deploy` passes, including the new stage tests
- [ ] Migration up and down clean against a real Postgres
- [ ] No plaintext refresh token is persisted — asserted by test
- [ ] Sending refused unless both switches agree — asserted by test
- [ ] Row counts match after import
- [ ] Dev's console renders Outreach unavailable; prod's renders it
