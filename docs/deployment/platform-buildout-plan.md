# Platform build-out plan

The nine things that need to go up next, each measured against what is actually running today
(see [`gcp-live-inventory.md`](gcp-live-inventory.md), read from `gcloud` on 2026-08-30) and
against what the repository already contains.

This is a scoping document, not a schedule. Each item states where we are, what has to exist,
the decision that has to be made, and what it blocks or is blocked by.

---

## Dependency order

The nine items are not independent. Three of them are foundations that the rest sit on:

```
  9. secrets ──┬──> 1. CI/CD ──┬──> 2. web/API split ──> 3. UI/UX
               │               │
  7. auth ─────┴──> 4. credits │
       │                       │
       └──> 8. admin ──────────┤
                               │
  5. job architecture ─────────┴──> 6. autoscaling groups
```

Suggested build order: **9 → 1 → 7 → 5 → 2 → 6 → 4 → 3 → 8.**

- **9 first** because CI/CD needs somewhere to read deploy credentials from, and because the
  provider API keys currently sitting in plaintext Cloud Run env vars need rotating regardless.
- **1 next** because everything after it wants to ship more than once a day, and today a deploy
  is a human typing a project ID into a prompt.
- **7 before 4** — credits are meaningless without a real identity to charge.
- **5 before 6** — you cannot scale on a queue you have not defined.
- **2 before 3** — refining the UI while it is coupled to the API means every CSS change ships
  a Python image.
- **8 last** because an admin console is a view over 4, 5, 6 and 7. Build it once the things it
  administers exist.

---

## 1. CI/CD and GitHub Actions continuous deployment

### Where we are

`.github/workflows/ci.yml` runs on every push and PR: `make check` (ruff + mypy ratchet + unit
suite) plus the boundary suites, with SHA-pinned actions and `permissions: contents: read`. It
is a good gate and it **deploys nothing**. There is no `deploy.yml`, no environment, no
identity federation, no registry push from CI.

The release machinery already exists locally as four gates in the `Makefile`:

| Gate | Target | Does |
| --- | --- | --- |
| A | `make check-fast` | lint + import sweep + graph wiring + config certification, seconds |
| B | `make check` | the full blocking suite — "may this commit produce a release?" |
| C | `make release-validate` → `make release-publish` | build the images once, validate *those* artifacts, then push the exact validated images and record their registry digests into `deploy/output/release.json` |
| D | `make mesh-preflight` | read-only: may the Gate C image be promoted to this project? |

`deploy/gcp/scripts/deploy.sh` is a 12-stage idempotent provisioner that is **interactive by
default** — it prints a plan and requires the operator to type the exact project ID — with
`DEPLOY_NONINTERACTIVE=1` as the documented automation escape hatch, which then requires every
ambiguous target pinned explicitly. That design is already CI-shaped; nothing calls it from CI.

### What has to exist

- **Workload Identity Federation** between GitHub Actions and `hexera-dev` (and later
  `hexera-prod`). No service-account JSON keys. A dedicated deployer SA per environment with
  the narrow set: `run.admin`, `artifactregistry.writer`, `iam.serviceAccountUser`,
  `compute.instanceAdmin` for the MIG, and nothing else.
- **A `deploy.yml` workflow** that runs Gate C on the merge commit, pushes the validated
  digests, then runs `deploy.sh` with `DEPLOY_NONINTERACTIVE=1` and every target pinned.
- **GitHub Environments** — `dev` auto-deploys on merge to `main`; `prod` requires a manual
  approval. This is where the deploy identity and any remaining config live.
- **Deploy by digest, never by tag.** The live drift already shows why: `deployment.json` names
  `mesh@sha256:e6f8b3f0…` while the job actually runs `sha256:adec28bf…`, and the API runs an
  *untagged* app digest that maps to no release record at all. CI writing `release.json` and
  `deployment.json` back as build outputs removes the class of problem.
- **Alembic in the deploy path.** There is exactly one migration (`0001_schema_baseline`) and
  `runtime/migrate.py` to apply it. The workflow needs a decision on where migrations run —
  a pre-deploy Cloud Run job is the obvious answer — before the schema starts moving, which
  items 4 and 7 will make it do.
- **Untagged-image cleanup**, as an Artifact Registry policy. One day of builds produced six
  untagged digests and 2.4 GB.

### Decided 2026-08-31

Neither. Dev deploys are **manual and selective**: a `workflow_dispatch` where the operator picks
which stacks go to the shared `hexera-dev` - the API, the worker fleet, the mesh job, migrations -
rather than every merge shipping everything. The recommendation above was auto-on-merge; it was
declined, and the reason is worth keeping: `hexera-dev` is shared, so an automatic deploy on merge
takes a stack out from under whoever is using it, and most merges do not touch most stacks.

Prod is **tag-triggered plus manual approval**, now enforceable rather than aspirational: the
organisation moved to a GitHub Enterprise trial, so the `prod` environment carries a
`required_reviewers` rule and a deployment branch policy restricted to `v*` tags. The earlier note
that the plan's billing tier rejected protection rules (`422 ... billing plan supports the required
reviewers protection rule`) no longer applies, and the `PROD_DEPLOY_ENABLED` repository variable
that stood in for approval is retired - a variable an org admin can flip unreviewed is not an
approval gate.

### Blocks

Items 2, 3 and 8 all ship new deployable units. Doing them by hand does not scale past the
first one.

---

## 2. Separate web from APIs

### Where we are

`ui/` is a vanilla-JS single-page app — `index.html`, hand-written CSS token layers
(`tokens.css` → `theme.css` → `shell/chat/viewer/timeline/result/a11y.css`), `js/` split into
`core`, `shell`, `api`, `realtime`, `render`, `viewer`, and a vendored, checksum-pinned
`vtk.js` under `ui/vendor/` (the release gate verifies those checksums inside the app image).

It is served by the FastAPI app itself via `STATIC_DIR`, out of the same container, on the same
Cloud Run service, behind the same `allUsers` invoker. A CSS change today means rebuilding a
Python image containing OpenFOAM-adjacent dependencies and redeploying the API.

### What has to exist

- **A separate hosting surface for the web app.** Given the UI is static, the cheapest correct
  answer is a GCS bucket behind an external HTTPS load balancer with Cloud CDN, at
  `app.hexera.ai` (or `hexera.ai` directly). Cloud Run for the web tier only becomes worth it
  if we adopt SSR later.
- **A real API origin** at `api.hexera.ai`, mapped to the Cloud Run service. Today the only
  address is the generated `*.run.app` URL.
- **CORS that means something.** `CORS_ORIGINS` is currently `*`. Once web and API have
  distinct origins it becomes the actual allowlist — and the app already enforces a
  non-wildcard `CORS_ORIGINS` when `ENV=production`.
- **A built frontend.** The vendored-and-checksummed `vtk.js` approach should survive any move
  to a bundler; it is what makes the release gate able to assert the browser assets. If a build
  step is introduced, the checksum gate has to move with it.
- **WebSocket routing.** `api/v1/ws.py` drives the live job timeline; the load balancer and any
  CDN in front of the API must not break upgrade requests. Keep WS on the API origin only.

### Decision needed

Bundler or no bundler. Staying dependency-free keeps the checksum gate simple and the UI
loads fine today; item 3 and item 8 will both push toward a component model. Recommend
deciding this *with* item 3, not before it.

### Blocked by

Item 1 (two deploy targets instead of one) and item 9 (a real domain implies real TLS and real
config).

---

## 3. Web UI / UX refinement

### Where we are

The shipped UI works and is tested — `tests/ui/` drives it in real headless Chrome and the
release gate runs 19 UI behaviour tests against the app image. There is a genuine design token
layer and a dedicated `a11y.css`. What it is not is a product surface: there is no account
area, no billing view, no job history beyond the current session, no admin affordance.

### What has to exist

Ordered by what the other items force into existence anyway:

- **Account and session UI** — sign-in, sign-up, password reset, API-key management (item 7).
- **Credit balance and usage** — a running balance, per-job cost, what happens when you run out
  (item 4).
- **Job history** — the `simulation_jobs`, `artifacts` and `chat_sessions` tables already hold
  everything needed; nothing surfaces it as a list you can come back to tomorrow.
- **The waiting experience.** Mesh jobs are budgeted at up to four hours
  (`MESH_TIMEOUT_SECONDS=14400`). The timeline and WS channel exist; what is missing is the
  "close the tab and come back" path — email or in-app notification on completion.
- **Error and quota states** as designed screens, not raw API errors: rate limited
  (`RATE_LIMIT_PER_MINUTE`), too many concurrent jobs (`MAX_CONCURRENT_JOBS`,
  `MAX_JOBS_PER_OWNER`), out of credits, job failed with a `FailedReason`.

### Decision needed

Whether the refinement is a restyle of the current app or a rebuild on a component framework.
That is really a question about how much surface items 4, 7 and 8 add. Recommend prototyping
the account + credits screens first and letting the answer fall out.

---

## 4. Logins and credit allotment

### Where we are

There is no user. `persistence/models.py` defines `geometry_sources`, `simulation_jobs`,
`chat_sessions`, `artifacts`, `terminal_outbox`, `geometry_interpretations`,
`capture_operations`, `artifact_reconciliations`, `source_object_cleanups` and
`native_submission_claims` — and no `users`, no `accounts`, no `credits`, no `api_keys`.

Identity today is an `X-User-Id` header, optionally HMAC-signed with `USER_TOKEN_SECRET`
(`api/security.py`); with the secret unset, as it is in dev, identity is **self-asserted**.
Quotas exist and are enforced per that identity: `RATE_LIMIT_PER_MINUTE=240`,
`MAX_JOBS_PER_OWNER`, `MAX_CONCURRENT_JOBS`. So there is already an "owner" concept threaded
through job ownership — it just has nothing durable behind it.

### What has to exist

- **Schema**: `users`, `accounts` (if teams are ever a thing — decide now, migrating later is
  worse), `api_keys`, `credit_ledger`, and a foreign key from `simulation_jobs` to the owning
  account. Everything the owner column currently implies, made real.
- **A ledger, not a counter.** Append-only entries — grants, holds, debits, refunds — with the
  balance derived. Jobs can run four hours and fail; a naive decrement leaks credits on every
  failure path, and the pipeline has explicit failure and reconciliation paths already
  (`FailedReason`, `artifact_reconciliations`).
- **Hold-then-settle around a job.** Estimate at submit, hold, settle on terminal state, refund
  on infrastructure failure. `NativeSubmissionClaim` and `persistence/lease.py` show the
  codebase already thinks in claims and leases; the credit hold should follow the same shape.
- **A cost model.** Today's real cost drivers are visible in the config: mesh job minutes at
  4 vCPU / 8 GiB, worker VM minutes, and LLM tokens across the intake / planner / builder /
  reviewer / visual-reviewer / search-summarizer agents, each with its own provider, model and
  token budget. Decide whether credits are charged on measured resources or on a flat per-job
  price. Recommend flat per-job tiers publicly, measured internally, so the pricing can move
  without a schema change.
- **Free-tier and abuse limits.** The endpoint is public and every job spends money at
  DeepInfra and DeepSeek. Signup must not equal unlimited spend.

### Blocked by

Item 7 — there is nothing to attach a balance to until logins exist.

---

## 5. Job-based architecture

### Where we are

More of this exists than the list implies, and it is worth being precise about what is missing.

- `PIPELINE_BACKEND` selects `celery` (default) or `deferred`
  (`runtime/composition.py`), with the Celery app and tasks under
  `adapters/pipeline_execution/`.
- Redis is the broker (`hexera-dev-redis`, BASIC tier, 1 GB).
- Job state lives in Postgres: `simulation_jobs` carries `pipeline_dispatch_state`,
  `pipeline_backend`, `pipeline_submitted_at`; `JobRepository.mark_launched` records which
  backend accepted it.
- Leases (`persistence/lease.py`), a `terminal_outbox` for exactly-once terminal delivery,
  `native_submission_claims` for submission idempotency, `worker_lease_seconds` and
  `worker_heartbeat_seconds`, `CELERY_MAX_REDELIVERIES=3`, `STALLED_JOB_TIMEOUT_HOURS`,
  `FAILED_JOB_RETENTION_HOURS=24`, and maintenance tasks for reconciliation.
- Heavy meshing is already offloaded: the API calls the Cloud Run job `dev-mesh`
  (`runtime/mesh_invocation.py`), which trades workspaces through the exchange bucket.

So the durable-job spine is real. What is missing is that it currently runs as **one queue on
one worker**, and the mesh job has **never actually executed** (zero Cloud Run job executions
recorded).

### What has to exist

- **Queue separation by class of work.** Agent/LLM work (I/O-bound, minutes, cheap to retry)
  and mesh/solver work (CPU-bound, hours, expensive) do not belong in one queue with one
  concurrency setting. `CELERY_WORKER_CONCURRENCY=3` is a single number for both today.
- **A queue-depth signal per queue**, which item 6 needs to scale on. The exporter currently
  publishes one scalar.
- **First real mesh job execution.** Everything about the Cloud Run job path is untested in
  the live project. Until one runs end to end, the architecture is a design, not a fact.
- **Redis durability decision.** BASIC tier is a single node with no replica. If the broker is
  the only record of queued work between the Postgres write and the worker pickup, a node loss
  drops jobs. Either move to STANDARD_HA or make Postgres the authority and Redis a pure
  wake-up signal — the outbox and dispatch-state columns suggest the latter is already close
  to true and should just be made explicit.
- **Dead-letter handling.** `CELERY_MAX_REDELIVERIES=3` bounds retries; where a job lands after
  the third failure, and who is told, is undefined.

---

## 6. Autoscaling groups with clear scale-up and scale-down

### Where we are

The MIG `hexera-dev-workers` exists: template `hexera-dev-worker-tpl-1445b8b` (e2-standard-4,
100 GB pd-balanced, Ubuntu 22.04), min 1 / max 5, cooldown 180 s, scaling on the custom gauge
`custom.googleapis.com/hexera/queue_depth` with `utilizationTarget 1.0`. The exporter
(`deploy/gcp/worker/queue_depth_exporter.py`) publishes from the worker every ~30 s against
`gce_instance`. The autoscaler reported `CUSTOM_METRIC_INVALID` right after creation and is now
`ACTIVE` — that was the gap before the first data point, and it self-cleared.

Also live and unmanaged: two hand-made `c2-standard-8` builder VMs running continuously,
reproducible from nothing in `deploy/`, and currently the largest steady cost in the project.

### What has to exist

- **A per-queue scaling signal.** One `queue_depth` gauge cannot drive two pools. Publish
  `queue_depth{queue=...}` and give each pool its own autoscaler.
- **Metric semantics that survive an empty fleet.** The gauge is written *by the workers*, so
  at `minNumReplicas: 0` there is nobody to report a backlog and the fleet never wakes up. This
  is the reason min is 1 today. If scale-to-zero is wanted, the depth must be published by the
  API or by a tiny always-on exporter instead.
- **An explicit scale-down contract.** Mesh work runs for hours; a `REPLACE`/`SUBSTITUTE`
  update policy plus an autoscaler that decides a VM is idle will kill work in flight. Needs:
  drain-before-delete, worker-side graceful shutdown on the GCE preemption/shutdown hook,
  lease-aware deletion, and `autoscalingPolicy.scaleInControl` to bound how fast the fleet can
  shrink.
- **Stated, documented thresholds** rather than a bare `utilizationTarget: 1.0` — target depth
  per worker, cooldown, max scale-in per window, and the max replica count each environment is
  allowed to reach. The max is also the cost ceiling; it should be a deliberate number.
- **Spot instances for the mesh pool**, with the drain path above making preemption safe. That
  is where the cost saving is.
- **A decision on the two builder VMs**: fold into the deploy tooling, or delete them.

### Blocked by

Item 5 — per-queue scaling needs per-queue queues.

---

## 7. Authentication: API keys, and email + password for the website

### Where we are

`api/security.py` verifies an HMAC-SHA256 signature over `X-User-Id` using `USER_TOKEN_SECRET`,
returning 401 for a missing `X-User-Id` or an invalid `X-User-Sig`. When `USER_TOKEN_SECRET` is
empty — the current dev state — the check is skipped entirely and any caller may claim any
identity. `MESH_API_KEY` exists as a separate concept, and `ENV=production` refuses to start
without it, without a non-wildcard `CORS_ORIGINS`, and without `POSTGRES_PASSWORD`. So the
startup policy for a locked-down deployment is already written; it has never been switched on.

There is no user store, no password hashing, no session handling, no key issuance.

### What has to exist

**For the website (email + password):**

- `users` with Argon2id-hashed passwords, email verification, password reset with expiring
  single-use tokens, and login rate limiting distinct from the per-identity API rate limit.
- Session handling. Given the web/API split in item 2, either short-lived JWT access tokens
  plus rotating refresh tokens, or server-side sessions in Redis with a `Secure`, `HttpOnly`,
  `SameSite=Lax` cookie on a shared parent domain. Recommend the cookie-session path — it
  avoids putting a bearer token in browser storage, and Redis is already there.
- Decide up front whether SSO (Google Workspace) is a launch requirement. Adding it later means
  reconciling identities.

**For programmatic access (API keys):**

- Keys as `hx_live_<id>_<secret>` with only a hash stored, shown once at creation, scoped to an
  account, revocable, with `last_used_at` and an optional expiry.
- Middleware resolving both credential types to the same internal principal, so everything
  downstream — quotas, ownership, credits — keeps seeing one identity concept. The existing
  `X-User-Id` owner threading is the seam to build on rather than replace.

**And immediately, regardless of the above:**

- Turn `USER_TOKEN_SECRET` on in dev so identity stops being self-asserted, and stop the API
  being `allUsers`-invokable with `CORS_ORIGINS=*` and `ALLOW_PUBLIC_RAW_TRACE=true` while it
  spends money on LLM calls on behalf of anonymous callers.

### Blocks

Items 4 and 8 both require it.

---

## 8. `admin.hexera.ai` — one console for providers, warm instances, ASGs

### Where we are

Nothing. No DNS zones exist in the project at all (the Cloud DNS API is enabled and unused), no
admin surface, no role concept. Administration today is `gcloud` plus editing ~150 inline
environment variables on a Cloud Run service and redeploying.

### What has to exist

- **A DNS zone** for `hexera.ai` and the three names this plan implies: `app`, `api`, `admin`.
- **A second, separately-deployed surface** at `admin.hexera.ai`, behind IAP or an
  allowlist plus the item-7 login with an admin role. Not a path on the public API — a
  compromise there should not reach the control plane.
- **What it must actually control**, mapped to what currently lives in env vars and `gcloud`:

| Area | Today | In the console |
| --- | --- | --- |
| Model providers | `INTAKE_/PLANNER_/BUILDER_/REVIEWER_/VISUAL_REVIEWER_/SEARCH_SUMMARIZER_*` env vars on the Cloud Run service — provider, model, timeouts, token budgets, concurrency, backoff, standby account | Per-role provider/model selection, live, with an audit trail; `settings/inference_overrides.py` and `settings/routes.py` are the existing seam |
| Warm instances | `minScale: 1` on the API revision; MIG `minNumReplicas: 1` | Warm-pool size per environment, with the cost shown |
| Autoscaling | Autoscaler fields via `gcloud` | Min/max/target/cooldown per pool (item 6) |
| Users and credits | Does not exist | Grant credits, inspect the ledger, suspend an account (items 4, 7) |
| Jobs | Postgres | Inspect, retry, cancel, see the failure reason and artifacts |
| Feature flags | `SOLVABILITY_GATE_ENABLED`, `DOMAIN_EXTENT_GATE_ENABLED`, `WEB_SEARCH_ENABLED`, `DATA_COLLECTION_ENABLED`, `MESH_SCRIPT_SCAN_ENABLED`, `RUN_PYTHON_REQUIRE_SANDBOX` and others, all deploy-time env vars | Runtime toggles where safe, deploy-time where not — and the distinction made explicit |

- **A settings authority decision.** The config system is currently typed, certified at startup
  (`settings/inventory.py`, Gate A's configuration certification) and immutable within a
  revision — which is a real correctness property, not an accident. A console that mutates
  config at runtime weakens it. Recommend a narrow, explicit set of runtime-mutable settings
  stored in Postgres and read through the existing settings layer, with everything else staying
  deploy-time and the console offering "change and redeploy" for those.
- **An audit log** for every admin action: who, what, before, after, when.

### Blocked by

Items 4, 5, 6 and 7 — it is a view over all of them.

---

## 9. Secret and credential storage

### Where we are

**Secret Manager is enabled in `hexera-dev` and contains zero secrets.** Meanwhile, on the
public `hexera-dev-api` Cloud Run service, these are literal plaintext values in the service
spec, readable by anyone with `run.services.get` and echoed into any `gcloud` output or deploy
log that dumps the service:

- `DEEPINFRA_API_KEY`
- `DEEPSEEK_API_KEY`
- `POSTGRES_PASSWORD`
- `MINIO_SECRET_KEY`

`gs://dev-transfer-224734058693/env.txt` and the source tarballs sit in a bucket with no
lifecycle rule and public access prevention merely "inherited". Worker VM metadata carries
`database-url`, `redis-url` and `env-uri` values. Locally, `.env` (17 KB) and `secrets/` are
gitignored and the repo already documents that `deploy/gcp/generated.env` deliberately contains
**secret container names only, never values** — the discipline exists in the deploy tooling and
was lost at the Cloud Run boundary.

### What has to exist

1. **Rotate the four exposed credentials.** They have been in a world-readable-by-project
   service spec; treat them as disclosed.
2. **Move them into Secret Manager** and reference them from Cloud Run as secret env vars or
   mounted volumes, so the value never appears in the service spec, in `gcloud` output, or in a
   deploy log.
3. **Grant `secretmanager.secretAccessor` per secret**, to the specific runtime SA that needs
   it. Not project-wide. `dev-mesh` currently holds no project-level roles at all — keep that
   property.
4. **Database credentials via IAM auth** rather than a static password, once the runtime SAs
   are sorted. Removes the longest-lived secret in the system.
5. **Take secrets out of instance metadata.** Worker metadata should carry a secret *name*,
   with the startup script fetching the value under the instance's own identity.
6. **Clean up `dev-transfer-…`** — `env.txt` and the source tarballs should not be sitting in a
   bucket with no lifecycle policy.
7. **A rotation policy** with an owner and an interval, and CI wired to fail on a plaintext
   secret in a deploy spec — the rule is only real if something enforces it.

### Blocks

Item 1, which needs a credential path that is not a JSON key in a GitHub secret. Do this first.

---

## Decisions

Answered 2026-08-31. Each is recorded with what it changes, because several of them move work
that the items above had scoped differently.

1. **`hexera-prod` is stood up on the first release.** Not before. Until then dev keeps its
   current posture, which means the "public, wide open, spends real money" properties in item 7
   are a dev-only concession with an expiry date rather than a permanent state.
2. **Teams / organisations, not single-tenant users.** The tenant boundary therefore has to be
   in the schema before data accumulates. `owner_id` stays authoritative for now and is not
   re-threaded across the codebase in one move; new tables carry an organisation column from the
   start, and credential resolution goes through a single named seam that widens from "an owner"
   to "an owner within an organisation" when the organisations tables land.
3. **Pricing is measured resource cost**, not flat per-job tiers. This promotes cost telemetry
   from an observability nicety to a billing input: the per-call `InferenceCall` record already
   carries tokens and an estimated cost but is discarded because no sink is bound, and the
   reviewer's model is absent from the price table and meters at $0.00. Both are now correctness
   bugs in the revenue path, not gaps in a dashboard. Mesh vCPU-seconds and worker occupancy,
   which nothing captures today, become required rather than desirable.
4. **Scale-to-zero in dev, a warm pool in prod.** This breaks item 6's current arrangement: the
   queue-depth metric is published by the workers themselves, so at zero instances there is
   nobody left to report the depth that would cause a scale-up. The publisher has to move off
   the fleet — a scheduled job is the obvious home — before dev can scale to zero.
5. **The frontend migrates to Next.js.** So item 2's "bundler or no bundler" is settled by the
   framework, and item 3 is a rebuild rather than a restyle. The vendored, checksum-pinned
   `vtk.js` under `ui/vendor/` and the release gate that verifies those checksums inside the app
   image both need a new home in that world.
6. **SSO comes later.** Email + password first. Item 7 should still model identity so that a
   later Google Workspace identity can be reconciled onto an existing account rather than
   creating a second one.
