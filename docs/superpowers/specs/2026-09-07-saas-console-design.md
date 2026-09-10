# SaaS console: deployable consoles, organisations, token metering, React rewrite — design

Date: 2026-09-07
Status: proposed
Scope: four sub-projects delivered as **three PRs** — **A** (console on dev),
**B+D** (organisations, accounts, org-level token metering), **C** (the SaaS console, a full
React rewrite of the browser client). Stripe and any charging model are explicitly out.

## 1. Why this exists

PR #13 created `apps/console` and `apps/admin-console`, moved the legacy browser client into the
public console, and put it behind env-configured email/password sessions. What it did not do is
make either app reachable outside a developer's machine, give the product a tenant boundary
wider than one person, or surface any of the data the product already stores.

The goal of this cycle is a console someone can actually be given a login to: their organisation's
chats and geometry are theirs, they can find yesterday's run without a deep link, and we can see
what their model usage costs us — with the seams for charging left obvious and empty.

Testing happens on the shared dev environment rather than locally. That is a hard requirement, and
it is why **A** ships first: today the console cannot be deployed at all.

### Verified starting facts

Each of these was read from the code in this repository, not from documentation.

| Fact | Evidence |
|---|---|
| Chats, geometry and jobs are already durable and owner-scoped | `persistence/models.py` — `chat_sessions`, `geometry_sources`, `simulation_jobs`, `artifacts`, each with `owner_id String(256)` and an owner index |
| Nothing can list any of it | the only session read is `GET /chat/history/{session_id}`; `api/v1/` has no list route for sessions, jobs or geometry |
| The browser finds a run only by deep link | `?job=<id>` parsed at `main.js:134`, identity from `localStorage.mg_uid` (`api/client.js:16`) |
| Identity can be overridden from the URL | `?user=<owner>` calls `useIdentity()` (`main.js:135`), re-pointing the page at another owner |
| The org seam exists and is unused | `Principal.organization_id` defaults `""` (`contracts/identity.py`); `api_keys.organization_id` is nullable with a comment saying the FK "arrives with the organisations migration" |
| The plan seam exists and is deliberately empty | `PLANS: dict[str, PlanOverride] = {}` (`settings/plans.py`), already wired into the quota gate and the per-plan rate limiter |
| Model calls are fully costed already | `InferenceCall` carries `input_tokens`, `cached_input_tokens`, `output_tokens`, `estimated_cost_usd` (`contracts/inference_telemetry.py`) |
| That telemetry is not durable | bound to `RedisInferenceTelemetrySink` in `composition.py:96`; capped list, 7-day TTL, no owner or org field |
| `user_id` reaches the router but not the record | threaded through `router.py` for Langfuse (`:151`, `:167`), dropped at `_run(route, invoke, *, job_id)` (`:124`) |
| Console auth is env-backed | `CONSOLE_AUTH_USERS` JSON parsed in `lib/auth/credentials.ts`; `owner_id` is the user's email |
| Sign-in has no throttling | flagged on #13; `authorize` calls scrypt for every attempt on a known email |
| The console is not deployed anywhere | no Cloud Run service; `deploy.sh:68` knows `images data storage migrate queue workers` and nothing about `apps/` |
| The node workspace has no CI | `ci.yml` has no pnpm step; the `ui` lane is `make test-ui` (Python + headless Chrome) |
| The legacy UI is duplicated and drifting | `ui/` is still served at `/ui` (`api/app.py:197`); `diff -rq ui apps/console/public/static` already differs in `api/endpoints.js` |
| Only the abandoned copy is tested | `tests/ui/` drives `ui/`, not `apps/console/public/static` |
| The event vocabulary is already portable | `core/events.js` is DOM-free with the renderer injected, by design |
| The mesh surface is decodable typed arrays | base64 `Float32Array`/`Uint32Array` from `render/viewer_pack.py`; patch colours assigned server-side with proven contrast (`render/review_palette.py`) |
| Migrations run before the new image | `deploy.sh` stage 220 (schema) precedes stage 245 (API service) |
| Release images are promoted by digest, never tag | `promote-release.sh`; components today are `mesh` and `app` |

## 2. Goals and non-goals

**Goals**

- The console deployable to `hexera-dev` through the existing `workflow_dispatch`, so all
  subsequent work is exercised on a real deployment.
- A real tenant boundary: `organization_id` populated, backfilled, and the column every
  tenant-scoped read filters on.
- Email/password accounts in the database, with the sign-in path hardened.
- Org-level token totals, durable, readable.
- A console with chat history, a geometry library, run history and org settings — the SaaS form
  factor — with the run surface rewritten in React, viewer included.
- The end of the duplicated browser client.

**Non-goals for this cycle**

- Stripe, prices, invoices, or any charging model. **D** produces org-level token counts and
  stops there; how those are charged is a later decision, deliberately not pre-empted here.
- Plan enforcement. `plans.py` starts resolving a plan from the org; `PLANS` stays empty.
- Multi-user organisations in the UI. The schema supports them from day one; the console shows
  one member because that is all a personal org has.
- Deploying `admin-console`. It is a stub page; it earns an image when it administers something.
- OAuth providers. Email/password only, as decided.
- Metering anything other than tokens — no runs, compute-seconds or storage meters.

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | One organisation per user, auto-created; no org UI | Smallest surface that still makes tenancy real. Multi-user lands later without a migration. |
| 2 | `organization_id` becomes the real tenant key, backfilled now | The code's own comments argue that widening a tenant boundary after data accumulates is the expensive migration. Prod has served since 09-03. |
| 3 | `owner_id` is retained as the actor, not removed | Keeps per-seat attribution possible later; re-keying `owner_id` would rewrite live rows and lose who acted. |
| 4 | The product API owns users and organisations; the console calls an internal `/auth/verify` | Identity belongs in the same schema as the columns that scope on it, and migrations stay in alembic. Cost: an endpoint accepting a plaintext password, which must be non-public, rate-limited and never logged. |
| 5 | **D** meters tokens only | Charging model undecided; counting tokens per org is the minimum that makes the next PR a read rather than a migration. |
| 6 | The Postgres telemetry sink batches through a bounded queue | `record()` is synchronous on the hot path of every model call. Losing a metering row is recoverable; stalling inference is not. |
| 7 | **C** is a full React rewrite including the viewer | Chosen over embedding the legacy stage. Accepted risk: the 722-line vtk.js viewer is the largest single piece of work in the cycle. |
| 8 | Three PRs: A, then B+D, then C | **D**'s only real dependency is **B**'s org tables and identity plumbing; merging them avoids a second pass over `identity.py` and `composition.py`. |
| 9 | `/ui` stays alive until **C** | It is the working fallback until the console is proven on dev. **A** stops the drift getting worse by putting the console copy under CI. |
| 10 | `organization_id` ships nullable; `NOT NULL` is a follow-up migration | `deploy.sh` migrates before the new image, so the old revision serves briefly against the new schema. A `NOT NULL` with no default would fail its inserts. |

## 4. A — console on dev

**Image.** A `console` target in the root `Dockerfile` using Next standalone output
(`output: "standalone"`), node runtime, non-root, honouring Cloud Run's `PORT`. `apps/console`
only.

**Release record.** `console` becomes a third component beside `mesh` and `app` in
`devtools/release/record.py`. `release-validate` builds it once and smokes it (container boots,
`/api/internal/health` returns 200); `release-publish` records its digest; `promote-release.sh`
writes `CONSOLE_IMAGE` as a digest reference.

**Deploy stage.** `deploy/gcp/scripts/create-console-service.sh` creates Cloud Run service
`hexera-<env>-console`, digest-pinned, `min-instances=0` on dev. `console` joins `_known` at
`deploy.sh:68`; the stage runs after the API service, because a console that boots before its API
serves errors. An unselected run reports through the existing `skipped` shape.

**Secrets and config.** `create-secrets.sh` gains `AUTH_SECRET` and `CONSOLE_AUTH_USERS`,
delivered as Secret Manager references, never literals. `CONSOLE_AUTH_USERS` is deliberately
short-lived — **B** replaces it with database accounts and deletes it. Env: `HEXERA_API_BASE_URL` (server-side,
for the proxy) and `NEXT_PUBLIC_HEXERA_API_BASE_URL` (public — the WebSocket goes browser→API
directly and does not pass through the Next proxy, so the API stays publicly reachable).

**CI.** A new `web` lane: `pnpm install --frozen-lockfile`, then `typecheck`, `lint`, `test`,
`build`, added to the `ci-gate` aggregate. The `scope` step today derives `run_python` and
`run_images` from a single `docs_only` check; it gains a `run_web` output on the same basis, so
the lane skips a documentation-only change exactly as the others do.

**Verification.** Container boot smoke in `release-validate`; the `web` lane; and after deploy, an
HTTP check against the dev URL — unauthenticated `/` redirects to `/sign-in`, the sign-in page
renders, and `/api/internal/health` returns 200 (the same unauthenticated route the boot smoke
already curls). `/readyz` is checked too, but for 401, not 200: it is deliberately session-gated —
it proxies to the product API presenting `MESH_API_KEY` — so on a service that is `allUsers`-
invokable, an unauthenticated 200 there would mean any caller could spend the console's own API
key against the product API. Asserting 401 instead proves the session gate is actually live on the
deployed revision, which the original 200 check never would have.

## 5. B — organisations and accounts

### Schema (`alembic/versions/0003_organizations.py`)

- `organizations` — id, name, slug (unique), created_at.
- `users` — id, email (unique, lowercased), password_hash, name, failed_attempts, locked_until,
  last_login_at, created_at.
- `memberships` — user_id, organization_id, role (`owner`|`member`), unique(user_id,
  organization_id). Present from day one; that is what makes decision 1 free later.
- `organization_id uuid NULL REFERENCES organizations(id)` added to `simulation_jobs`,
  `chat_sessions`, `geometry_sources`, `geometry_interpretations`, `capture_operations`,
  `artifact_reconciliations`, `source_object_cleanups`. `api_keys.organization_id` already exists
  and gains its FK here.
- Composite indexes mirroring today's owner indexes, keyed on `organization_id`.

**Backfill**, in the same migration: one organisation, one user and one membership per distinct
existing `owner_id`; then `organization_id` stamped on every existing row from its `owner_id`.
The column stays nullable (decision 10); a follow-up `0004` adds `NOT NULL` once no writer can
produce one.

### Tenant scoping

`Principal.organization_id` is populated for real, resolved from the membership. A new `org_dep`
joins `owner_dep` in `api/security.py`. Repositories gain an org filter — roughly 50 `owner_id`
sites across `persistence/repositories/` and 12 `owner_dep` route sites.

**The rule: reads scope on `organization_id`; writes stamp both.**

### Console authentication

`POST /auth/verify` on the product API takes email and password and returns
`{user_id, organization_id}` or a single undifferentiated refusal. The console's Auth.js
credentials provider calls it and keeps only the session. `lib/auth/credentials.ts` loses its
env-JSON user list; `CONSOLE_AUTH_USERS` is deleted after a devtool imports its entries.

`POST /auth/register` creates user, organisation and membership in one transaction, gated by
`CONSOLE_SIGNUP_ENABLED` so dev is open and prod stays closed.

### Sign-in hardening

Addresses the finding on #13. Per-email and per-IP attempt windows via the existing
`contracts/rate_limit.incr_window`; lockout after N failures using `failed_attempts` /
`locked_until`; a dummy scrypt derivation on unknown emails so response timing does not reveal
whether an account exists. The verify endpoint never logs the password or the email at any level.

## 6. D — org-level token metering

**Table** `inference_usage` — id, organization_id, owner_id, job_id, role, provider, model,
input_tokens, cached_input_tokens, output_tokens, estimated_cost_usd, occurred_at. One row per
model call. Indexed on `(organization_id, occurred_at)`.

**Plumbing.** `organization_id` and `owner_id` are added to `InferenceCall` and threaded
`_run → execute → _emit`. Every caller already holds `user_id`; the change is mechanical across
three signatures.

**Sink.** `adapters/inference_telemetry/postgres.py` implements the existing
`InferenceTelemetry` Protocol. `composition.py` binds a small fan-out sink so the Redis inspection
feed keeps working unchanged.

**Backpressure** (decision 6). The Postgres sink enqueues onto a bounded queue; a background
flusher inserts in batches on a size or time trigger. On overflow it drops the record and
increments a logged counter. It never blocks `record()`.

**Read surface.** `GET /api/v1/usage` returns org token totals for a period, shown in console org
settings. `plans.py` starts resolving the plan from the organisation rather than the key; `PLANS`
stays empty.

## 7. C — the SaaS console

### New endpoints

All relative to `/api/v1`.

| Route | Returns |
|---|---|
| `GET /chat/sessions` | paginated: id, title, created_at, updated_at, job_id, job status, geometry filename |
| `GET /simulation` | run history, paginated, filterable by status (a collection route beside the existing `GET /simulation/{job_id}`) |
| `GET /geometry` | the model library, carrying `bytes_available` so purged-but-retained rows read correctly |
| `PATCH /chat/sessions/{id}` | archive (`archived_at`) |
| `GET /usage` | from **D** |

`title` is derived server-side from `chat_sessions.domain`, falling back to the first user message
and then the filename. No title column: nothing would write it.

Archive rather than delete, because jobs reference sessions and geometry with
`ondelete="RESTRICT"` — these rows are lineage.

### Routes

`/` chat list and new chat · `/c/[sessionId]` the run surface · `/models` · `/runs` ·
`/settings/organization` (members, token usage) · `/sign-in` · `/sign-up`. Lists are server
components fetching through the server-side client; only the run surface is client-side.

### The rewrite

| Legacy | Becomes | Note |
|---|---|---|
| `core/events.js` | `lib/run/events.ts` | Near-verbatim; already DOM-free with the renderer injected |
| `core/state.js` | `useReducer` + context | Six fields; no state library earns its keep |
| `realtime/stream.js` | `useRunStream` hook | Ticket flow unchanged — credentials in headers, never the URL |
| `render/stage.js` (342 ln) | `<ChatMessage> <Brief> <Timeline> <ResultCard>` | Retires hand-rolled `innerHTML` + `esc()` |
| `shell/*` | `<Composer> <ApiStatus>` + a capabilities hook | |
| `viewer/viewer.js` (722 ln) | `<MeshViewer>` on npm `@kitware/vtk.js` | Drops the vendored 2.2 MB UMD blob and `SHA256SUMS` |
| `viewer/dispute.js` | `<DisputeDialog> <AcceptDialog>` | |
| `api/client.js`, `endpoints.js` | `@hexera/api-client` | Added by #13, currently unused by the browser |

Identity stops coming from `localStorage.mg_uid` and starts coming from the session. The
`?user=<owner>` deep-link override is deleted.

Base64 → typed-array decoding stays pure and unit-tested. Patch colours keep coming from the
server's `review_palette` assignment, preserving its proven contrast guarantee.

### Deletions

`ui/`; the `/ui` and `/static` routes in `api/app.py:190-205`; `apps/console/public/static/js`;
the vendored vtk.js; `tests/ui/` (replaced). `STATIC_DIR` and the two documents that describe the
shipped browser client — `docs/architecture/repository-map.md:15` and the `STATIC_DIR` entry in
`docs/reference/configuration.md` — are updated in the same pass. This ends the duplication.

## 8. Testing

| Sub-project | Tests |
|---|---|
| A | container boot smoke in `release-validate`; the `web` CI lane; post-deploy HTTP checks on dev |
| B | migration up/down and a backfill test over seeded multi-owner data; repository tests proving a cross-org read returns nothing; sign-in lockout; unknown-email timing |
| D | tokens land under the correct organisation; queue overflow drops rather than blocks; the fan-out sink still feeds Redis |
| C | vitest for events, decode and reducers (porting the assertions in `tests/ui/test_event_contract.py`); Playwright for sign in, list chats, open a run, replay a finished run, viewer renders a surface |

The `ui` CI lane becomes the Playwright lane in **C**.

## 9. Risks

**The viewer port.** 722 dense lines, and moving from a UMD global to the npm ESM package changes
how vtk.js is imported and bundled. It needs `dynamic(..., { ssr: false })` and its own bundle
budget, or it lands in the main chunk. This is the largest single piece of work in the cycle and
the most likely to slip.

**CORS and origin.** After **A** the console is a different host from the API, and the WebSocket
goes browser→API directly. The API must accept the console origin. Verify on dev early in **C**,
not at the end.

**The backfill.** It runs against live prod data. It must be idempotent and must be rehearsed
against a restored copy of the dev database before it is allowed near prod.

**The plaintext-password endpoint.** `/auth/verify` is the one new surface that would be serious
if exposed. It must be unreachable from the internet, rate-limited independently of the console,
and excluded from request logging.

**`main` has no required checks.** Carried forward from #12: `deploy.yml`'s `await-ci` is still
the only thing between a failing suite and a deployed artifact. Not this cycle's work, but every
PR here rides on it.

## 10. Open questions

1. Whether `hexera-dev` should get a stable custom domain for the console, or run on the generated
   `run.app` URL. Affects the `AUTH_URL`/cookie configuration in **A**.
2. Whether `/auth/verify` should live under `/api/v1` (and be excluded at the edge) or on a
   separate internal port. Decided in **B**; either satisfies decision 4.
3. Session lifetime and whether sign-out should revoke server-side. Today's JWT sessions have
   neither answer.
