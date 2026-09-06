# GCP deployment, public API and cost telemetry — design

Date: 2026-08-29
Status: proposed
Scope: subsystem **B** (hosted environments + deploy automation) with the cost-telemetry
groundwork folded in. Subsystems **A** (public API key auth) and **C** (release gating) are
specified here to the depth that **B** constrains them; each still gets its own implementation
plan.

## 1. Why this exists

Hexera runs today as a local control plane with one remote tier. `deploy/gcp` provisions the
Cloud Run mesh **Job**, its service account, its GCS exchange bucket and their IAM — and nothing
else. The API, the pipeline worker, PostgreSQL, Redis and MinIO all run on the operator's machine.

To put design partners on the product we need the control plane hosted, an identity system that
issues and revokes credentials per tenant, environments that can be deployed to incrementally,
prod gated behind a release, and enough measurement to know what a run costs.

### Verified starting facts

Each of these was read from the code, not from documentation.

| Fact | Evidence |
|---|---|
| Only the mesh tier deploys | `deploy/gcp/Makefile` header; 16 scripts, all mesh-scoped |
| No programmatic job submission | jobs created only at `agents/intake/approval.py:293` and `api/v1/simulation.py:269` |
| Auth is one shared secret | `api/security.py` — global `MESH_API_KEY` + HMAC `X-User-Sig` over `X-User-Id` |
| Tenancy is real at the data layer | `owner_id` `String(256)`, scoped in SQL on every read; quotas advisory-locked in `job_service.check_quotas` |
| Inference telemetry is built but discarded | `set_inference_telemetry()` is never called in `runtime/composition.py`; only tests bind a sink |
| Reviewer model prices at $0.00 | deliberately absent from `_PRICES` in `adapters/inference_telemetry/pricing.py` — price unconfirmed |
| No infrastructure cost telemetry at all | mesh durations exist only as a capped Redis list for user-facing ETAs |
| Crashed worker loses the job | `task_acks_late=False` + `reap_stalled_jobs` marks it failed after `STALLED_JOB_TIMEOUT_HOURS` (4) and tells the user to resubmit |
| Event streams survive disconnection | monotonic `seq` cursor; client replays from last rendered (`ui/js/realtime/stream.js:27`, `api/v1/ws.py:162-186`) |
| Migrations are already prod-safe | advisory-locked, non-zero exit on failure, three refusals (`runtime/migrate.py`) |
| Object store is MinIO-client-specific | `factory.py` constructs `MinioStore` unconditionally; adapter uses the `minio` package |
| Deploy naming is already env-parameterized | `DEPLOYMENT_ID` prefix, project/region binding, `created`/`reused` dispositions |

## 2. Goals and non-goals

**Goals**

- Two hosted environments (`prod`, `dev`) running the same topology at different sizes.
- Personal developer environments that cost nothing and keep the current fast loop.
- Deploys that are incremental, idempotent, and promote a validated digest rather than rebuilding.
- Prod reachable only from a tagged, gated release.
- Per-tenant API keys with issuance, revocation and rotation.
- Enough cost measurement to price plans and to settle worker placement with data.

**Non-goals for this cycle**

- Metering or billing in the request path. Plans are seat/subscription with soft quotas
  (decided: option **d**). Consumption metering is instrumented but not enforced.
- A deterministic non-conversational job API. The public surface is the conversational one.
- Autoscaling the worker fleet. Launch ships a manually-set size; the servo comes later.
- Spot/preemptible workers. Blocked on resume-on-preemption — see §9.
- A third (staging) environment. `dev` plays that role; adding one later is configuration.

## 3. Decisions

Each decision is recorded with its reasoning so a later reader can tell whether the reason still
holds.

| # | Decision | Reasoning |
|---|---|---|
| D1 | Two GCP projects: `hexera-prod`, `hexera-dev` | The boundary that cannot be retrofitted cheaply is prod's IAM/quota/billing. A third project to test a path `dev` already exercises is cost without coverage. |
| D2 | Personal dev = `DEPLOYMENT_ID` prefix inside `hexera-dev` | The prefix model already exists and is tested. Project-per-developer needs project-creation rights and fans image pulls across registries. |
| D3 | Workers on a GCE MIG (topology **B**) | Chosen by the user over all-Cloud-Run. Cheaper per vCPU-hour, no request/lifetime model against a 6-hour job, and real local disk — which removes Cloud Run's in-memory-filesystem problem against a 4 GB expanded-archive ceiling. |
| D4 | API on Cloud Run | Stateless and request-shaped. Disconnects are already survivable via the `seq` replay cursor, so the request-duration cap is not a constraint. |
| D5 | Standard VMs, never Spot, until resume-on-preemption exists | A preemption costs a user their run and stalls up to 4h before the reaper reports it. See §9. |
| D6 | Fixed MIG size at launch; autoscaling deferred | Queue-depth autoscaling needs a custom metric exporter. A settable target size already satisfies "tune the numbers easily". |
| D7 | Beat replaced by Cloud Scheduler → Cloud Run Jobs | Removes a long-lived container that only waits; makes each sweep independently observable and retryable. |
| D8 | One Artifact Registry, in `hexera-prod`, read-only to `hexera-dev` | One place a digest exists means no copy step can silently substitute different bytes for the ones Gate C validated. |
| D9 | Object store: spike S3-interop first, default to a native GCS adapter | `MinioStore` uses the `minio` client, not boto3. Interop is plausible but unproven at multipart, presigning and `bucket_exists`. |
| D10 | Deploy order is API-then-MIG | Only the API entrypoint migrates. Rolling workers first runs them against an un-upgraded schema. |

## 4. Environment topology

| Environment | Project | `DEPLOYMENT_ID` | Sizing |
|---|---|---|---|
| Production | `hexera-prod` | `prod` | API `min-instances=1`; MIG target set from measured load |
| Shared dev | `hexera-dev` | `dev` | API `min-instances=0`; MIG target 0–1 |
| Personal | `hexera-dev` | `dev-<name>` | mesh job only; control plane local |

Resource names derive from the prefix, exactly as `bootstrap-env.sh` does today:
`${DEPLOYMENT_ID}-api`, `${DEPLOYMENT_ID}-worker-mig`, `${DEPLOYMENT_ID}-mesh`,
`${DEPLOYMENT_ID}-exchange-${PROJECT_NUMBER}`.

`generated.env` remains per-environment, gitignored, bound to its project and region, and carries
Secret Manager container names rather than secret values. The existing stale-state guard — which
refuses to deploy a `generated.env` produced for a different project — becomes more valuable with
two projects, not less.

### Per-environment components

| Component | Placement | Scale dial |
|---|---|---|
| API | Cloud Run service, Direct VPC egress | `min`/`max-instances` |
| Worker | Regional MIG, COS VMs running the `pipeline` image | MIG target size |
| Maintenance sweeps | Cloud Scheduler → Cloud Run Jobs | schedule |
| Database | Cloud SQL for PostgreSQL, private IP | tier |
| Events/broker | Memorystore for Redis, private IP | tier |
| Object storage | see §5 | — |
| Mesh executor | Cloud Run Job (unchanged) | `MESH_CPU`, `MESH_MEMORY` |
| Network | one VPC per project | — |

Cloud SQL and Memorystore on private IP put the MIG in the VPC naturally and require Direct VPC
egress for Cloud Run. That is one more resource per environment for the deploy scripts to
provision and for the doctor to verify.

Worker concurrency is set to 1 per instance. This is a choice, not the status quo: the `pipeline`
image's own `CMD` uses `--concurrency 1`, but compose starts it with `CELERY_WORKER_CONCURRENCY`,
which defaults to 2. Fixing the hosted fleet at 1 makes capacity a pure function of instance count,
keeps a job's cost attributable to a whole instance (which §8 depends on), and stops one stuck job
from occupying a slot a second job on the same VM would need.

## 5. Object storage

`build_object_store()` has one branch and constructs `MinioStore`; the adapter is written against
the `minio` Python client. Three candidates:

1. **GCS via S3-interoperability + HMAC keys.** No adapter change if the client is compatible.
   Unproven at multipart upload, presigned GET through `MINIO_PUBLIC_ENDPOINT`, and
   `bucket_exists`/`make_bucket`.
2. **Self-run MinIO** per environment on a VM with a persistent disk. Zero compatibility risk,
   adapter untouched, but introduces a stateful service with backup and restore obligations.
3. **Native GCS adapter** behind the existing `ObjectStore` contract. Bounded work — the contract
   is clean and the factory is one function — and unifies the system on one storage technology,
   since `gcs_exchange.py` already speaks GCS natively.

**Plan:** run a half-day spike against (1) before implementation. If it passes all three risk
areas, take it. Otherwise implement (3). Option (2) is the fallback only if both are blocked.
The decision must be made before prod holds user data; migrating object storage afterwards means
moving objects and rewriting `storage_key` values.

## 6. Release gating and promotion

Gates A–D already exist and are digest-based. Promotion extends them rather than replacing them.

```
Gate A  check-fast          seconds, no services
Gate B  check               ruff + mypy ratchet + deps + hermetic suite + UI
Gate C  release-validate    build images ONCE from this commit, validate THOSE artifacts
        release-publish     push the exact validated images; record immutable digests
Gate D  mesh-preflight      read-only: may this image be promoted here?
        ↓
   deploy to dev from the published digest
        ↓
   soak
        ↓
   promote the SAME digest to prod   ← requires an annotated tag per tag_policy.py
```

Properties this preserves:

- **Deploy never builds.** `promote-release.sh` already reads digests from
  `deploy/output/release.json` and refuses an empty `MESH_IMAGE` rather than promoting something
  unvalidated. Application images follow the same rule.
- **Rollback is redeploying the previous digest** — which works only because there is one registry
  (D8) and the digest is immutable.
- **Prod requires a tag.** `tag_policy.py` enforces annotated, immutable, clean-tree,
  exact-commit. Prod deploy asserts the deploying commit is tagged; dev has no such requirement.
- **Deploy order is API-then-MIG** (D10), enforced in the deploy script.

Migrations need no new machinery. `runtime/migrate.py` is advisory-locked so concurrent API
instances contend safely, exits non-zero on failure so the previous revision keeps serving, and
refuses an unmanaged schema, an unshipped revision, or an incomplete one.

## 7. Public API: keys, tenancy and plans

The public surface is the existing conversational API — `POST /chat/message`,
`GET /chat/history/{id}`, `POST /upload/step-file`, `GET /simulations/{id}`, `/surface`,
`POST /simulations/{id}/dispute`, `POST /ws/ticket`, `WS /ws/{id}/stream`. No new product surface;
what changes is how a caller proves who they are.

### Key model

New table `api_keys`:

| Column | Purpose |
|---|---|
| `id` | primary key |
| `owner_id` | the tenant this key authenticates as; maps onto existing `owner_id` scoping |
| `name` | human label for the key |
| `key_prefix` | leading public segment, indexed, used to locate the row |
| `key_hash` | SHA-256 of the secret; the secret itself is shown once at creation and never stored |
| `plan` | drives quotas and limits |
| `created_at`, `last_used_at` | lifecycle and staleness |
| `revoked_at`, `expires_at` | revocation and expiry, both nullable |

Format: `hx_live_<id>_<secret>`, as implemented — the id is 12 base62 characters (no `_`, so the
public part can be split off unambiguously even though the secret's alphabet contains one) and the
secret is `secrets.token_urlsafe(32)`. An earlier draft of this section said `hxa_<env>_<…>`; the
build-out plan's spelling won, and this records the one that shipped. Verification looks the row up by prefix, then compares
the hash with `hmac.compare_digest` — the same constant-time discipline `api/security.py` already
uses. Presented as `Authorization: Bearer <key>`.

**Why this maps cleanly:** `owner_id` is already `String(256)` and already scoped in SQL on every
query, with `test_owner_isolation_matrix.py` covering it. A key resolves to an `owner_id` and
everything downstream is unchanged. No data-model migration for tenancy itself.

**Rotation** is per-key: an owner may hold several live keys, so issuing a new one and revoking the
old is a zero-downtime operation. This is the concrete improvement over `USER_TOKEN_SECRET`, whose
rotation invalidates every user id simultaneously.

### Plans

`MAX_JOBS_PER_OWNER`, `MAX_CONCURRENT_JOBS` and the existing Redis rate limiter are global
environment values today. They become per-plan lookups with the current values as defaults, so
behaviour is unchanged for anyone without a plan. `check_quotas` already serializes count-then-create
under a per-owner advisory lock, so per-plan limits inherit that correctness.

### Migration of the existing auth

The HMAC `X-User-Id`/`X-User-Sig` path stays until the browser session is migrated onto keys.
The global `MESH_API_KEY` is retired once per-key auth is enforced; keeping both would leave a
shared secret that grants access without an owner.

## 8. Cost telemetry

Three measurements, one output.

**Model cost — mostly built.** `InferenceCall` already carries `job_id`, `role`, provider, model,
input/cached/output tokens, `estimated_cost_usd`, latency, TTFT, queue wait, attempts and failover,
and the router builds one per call. Binding a durable sink in `runtime/composition.py` — writing to
a Postgres table keyed on `job_id` — turns on per-run model cost immediately. The sink must stay
fail-open, matching the contract's existing behaviour.

**Blocker: the reviewer prices at $0.00.** Its model is deliberately absent from `_PRICES` because
the price could not be confirmed. The reviewer is the image-heavy role, so every cost figure
understates the most expensive agent until this is filled in. One entry in the table, or one
`MODEL_PRICE_OVERRIDES` value. This gates any use of cost data for pricing.

**Mesh cost — new.** The Cloud Run Job execution's duration multiplied by `MESH_CPU` gives vCPU-
seconds per mesh. `cloud_run_client` already holds the execution reference; the duration needs
recording against the job.

**Worker occupancy — new.** Wall-clock seconds a job held a worker instance, which at concurrency 1
is the instance's whole cost for that period.

**Output:** a per-job cost record — model USD, mesh vCPU-seconds, worker occupancy seconds. That
prices plans, and it converts "Cloud Run or VMs" from an argument into arithmetic. It is also the
input to the deferred decisions in §9.

## 9. Deferred work, with its trigger

| Deferred | Blocked on | Trigger to revisit |
|---|---|---|
| Spot/preemptible workers | resume-on-preemption built atop the existing checkpointer and fence | when worker cost is a material share of the bill |
| MIG autoscaling on queue depth | a custom metric exporter | when manual size changes become frequent |
| Consumption metering / billing | §8 telemetry plus a pricing decision | when plans stop covering costs |
| A staging environment | nothing; configuration only | when `dev` can no longer be disrupted freely |
| Deterministic job-submission API | product decision | if users ask for pipeline/CI integration |

**On Spot specifically.** A MIG makes Spot trivially available and it is roughly 60–70% cheaper,
which is exactly why the constraint is recorded here rather than left for a reader to rediscover.
With `task_acks_late=False`, a preempted worker's task is not redelivered; the job sits in
`running` until the reaper fails it after `STALLED_JOB_TIMEOUT_HOURS` and tells the user to
resubmit. The checkpointer, the execution fence and the crash-takeover path exist, so automatic
resume is buildable — it is simply not on the launch path.

## 10. Verification

| Area | How it is verified |
|---|---|
| Deploy scripts | extend `deploy/gcp` `make check` (already `bash -n` + shellcheck + manifest/lifecycle parse) to the new manifests |
| Environment health | per-environment `deploy-doctor` covering VPC egress, Cloud SQL and Memorystore reachability, registry read access |
| Key lifecycle | unit tests for issue, verify, reject-revoked, reject-expired, constant-time compare |
| Tenancy | `test_owner_isolation_matrix.py` extended to key-resolved owners |
| Telemetry binding | a composition test asserting a sink is bound — this is the exact defect live today, and it should not be able to regress silently |
| Plans | unit tests that a plan's limits override the global defaults and that absent plans keep current behaviour |
| Deploy ordering | the deploy script asserts the API revision is serving the new digest before the MIG rolls |

## 11. Phasing

| Phase | Content | Depends on |
|---|---|---|
| 0 | Bind the telemetry sink; fix the reviewer price; add the composition test | nothing |
| 1 | Object-store spike; decide and implement per §5 | nothing |
| 2 | `hexera-dev` end to end: VPC, Cloud SQL, Memorystore, API service, worker MIG, Scheduler jobs | 1 |
| 3 | API keys, plans, retire `MESH_API_KEY` | 2 |
| 4 | `hexera-prod` + release gating per §6 | 2, 3 |
| 5 | Cost review from real data; decide autoscaling and Spot | 0, 4 |

Phase 0 has no infrastructure dependency and produces the data every later decision needs. It goes
first for that reason, not because it is easy.

## 12. Open questions

1. **Reviewer model price.** Needs confirmation from the provider's catalogue. Blocks §8.
2. **Region.** `GCP_REGION` defaults to `us-central1`. Design-partner locality and any data-residency
   commitment should settle this before Cloud SQL is created, since moving it later means a migration.
3. **Cloud SQL sizing and HA.** Deliberately unspecified — it should follow the first real load
   measurement rather than a guess, and it is a dial rather than a topology choice.
