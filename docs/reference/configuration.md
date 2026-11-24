# Configuration

How this product's configuration works, and how to change it safely. The hand-written sections
below explain the settings you are most likely to touch; they are **not** the complete list. The
complete list is generated, see [the settings roster](#complete-settings-roster) at the end.

## Where settings come from

`src/meshpipeline/settings/inventory.py` is the canonical, machine-readable authority. Every
supported setting has one entry there carrying its name, type, default, description, and three
classifications:

| Classification | Meaning |
|---|---|
| **exposure** `template` | you are expected to set it; it is written into the generated `.env.example` |
| **exposure** `internal` | an advanced control the product supports but does not put in the ordinary template |
| **exposure** `external` | supplied by the platform or a library (a Cloud Run injection, an SDK's own variable), not by editing `.env` |
| **consumer** | who reads it: the application, `docker-compose.yml`, the `Makefile`, or an external SDK |

So **absence from `.env.example` does not mean a name is unsupported**: it means the entry is not
classified `template`. Every entry, whatever its exposure, appears in the generated roster below.

Defaults live in the catalogue, never at the call site. A few are *derived*: they are declared as a
function of another setting (`JOBS_DIR` defaults to `<DATA_ROOT>/jobs`), and runtime resolves them
from whatever `DATA_ROOT` actually is, so overriding the parent moves its children.

The application may not read the environment any other way. A direct `os.getenv`, or a variable
name built at runtime out of provider or model data, fails the configuration certification that
`make check-fast` runs.

Configuration that has been removed is **refused at startup**, before the process does anything,
with the offending variable named and its replacement given. The retired list is generated from the
same authority, see [Removed settings](#removed-settings).

**Required** below means a real value must be supplied before a live job can run.
**Secret** means a real credential: `.env.example` carries only a blank or a local-stack
placeholder for one, and a real value must never be committed.

## Model providers

A dependency that fails repeatedly trips a circuit breaker and the job fails as a system issue.
There is deliberately no fallback model, because a silent substitution changes mesh quality
without telling anyone.

Which provider serves a role is a per-role setting: every role declares twelve of them,
`<ROLE>_PROVIDER` and `<ROLE>_MODEL` among them, and they are listed in `.env.example` and in the
roster at the end of this document. The shipped routes are:

| Role | Prefix | Provider | Model |
|---|---|---|---|
| intake | `INTAKE` | `openai` | `gpt-5.6-luna` |
| search summarizer | `SEARCH_SUMMARIZER` | `openai` | `gpt-5.6-luna` |
| builder | `BUILDER` | `openai` | `gpt-5.6-terra` |
| planner | `PLANNER` | `openai` | `gpt-5.6-terra` |
| visual reviewer | `VISUAL_REVIEWER` | `openai` | `gpt-5.6-terra` |

Each role also owns a circuit breaker, named for the role and never for its vendor: `intake`,
`summarizer`, `builder`, `planner`, `reviewer`. Those names appear in the readiness probe's
`circuits` map, and an open one there names the role whose model is unwell — not the provider.

The tables below are the per-provider connection settings.

### OpenAI: every role

| Key | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | *(blank)* | **Required, secret.** Every shipped route resolves here. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | override only to reach an OpenAI-compatible gateway |

OpenAI is served over the **Responses** API, not chat completions — that is what lets the four
tool-calling roles reason and call tools in the same request. The API rejects `temperature`,
`top_p`, `presence_penalty`, `min_p` and `top_k`, so the sampling settings below are accepted,
recorded, and then dropped from the request with a warning. They are not deleted because a route
pointed back at DeepSeek or DeepInfra still honours every one of them.

### DeepSeek and DeepInfra: supported, unused by the shipped routes

Both remain fully supported: point a role's `<ROLE>_PROVIDER` at either and its key becomes
required. Neither is required to boot a default deployment.

| Key | Default | Notes |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(blank)* | **Secret.** Required only when a route resolves to `deepseek`. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | |
| `DEEPINFRA_API_KEY` | *(blank)* | **Secret.** Required only when a route resolves to `deepinfra`. |
| `DEEPINFRA_BASE_URL` | `https://api.deepinfra.com/v1/openai` | OpenAI-compatible wire protocol |

Only the two keys and the two base URLs belong to those providers. `DEEPSEEK_MODEL` does not,
despite its name — see below.

### Agent models and sampling

Every setting here is **live**, whichever provider serves the role. Two of them carry a provider's
name for historical reasons and are listed here rather than under that provider, because filing a
live setting under a heading the shipped routes do not use is how it gets blanked.

| Key | Default | Notes |
|---|---|---|
| `DEEPSEEK_MODEL` | `gpt-5.6-luna` | **Not a DeepSeek setting.** It is *intake's* model, under a name from when intake could only be DeepSeek, and it is recorded as intake's capture provenance. Blanking it disables nothing — it writes an empty model into every job's record. Keep it equal to `INTAKE_MODEL`, which is what the route reads. Renaming it is a breaking configuration change and is pending. |
| `REVIEWER_MODEL` | `gpt-5.6-terra` | the visual reviewer's model for capture provenance; the route reads `VISUAL_REVIEWER_MODEL`, so keep the two equal. Vision-capable: the reviewer looks at images |
| `BUILDER_MAX_TOKENS` | `16384` | the builder's output ceiling — `max_output_tokens` on a Responses call, `max_tokens` on a chat one |
| `REVIEWER_MAX_TOKENS` | `16384` | the same ceiling for one review round |
| `REVIEWER_TEMPERATURE` | `0.7` | reviewer sampling; dropped by OpenAI, honoured by DeepSeek/DeepInfra |
| `REVIEWER_TOP_P` | `0.8` | dropped by OpenAI |
| `REVIEWER_PRESENCE_PENALTY` | `1.5` | dropped by OpenAI |

`BUILDER_MODEL` and `SEARCH_SUMMARIZER_MODEL` are not listed here: they are the builder's and the
summarizer's *route* settings under their own names, and appear in the route table above.

### Resilience

| Key | Default | Notes |
|---|---|---|
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | consecutive failures before the breaker opens |
| `CIRCUIT_RECOVERY_SECONDS` | `30` | how long it stays open |
| `CIRCUIT_HALF_OPEN_MAX` | `1` | trial calls allowed while recovering |
| `CELERY_MAX_REDELIVERIES` | `3` | a job that keeps crashing the worker is dead-lettered after this many deliveries |

## Mesh compute backend

| Key | Default | Notes |
|---|---|---|
| `GCP_PROJECT_ID` | *(blank)* | required: the project owning the mesh job |
| `GCP_REGION` | `us-central1` | |
| `CLOUDRUN_JOB` | *(blank)* | required: the mesh Job to dispatch |
| `GCP_MESH_BUCKET` | *(blank)* | required: the workspace exchange bucket |
| `GOOGLE_ADC_FILE` | `./secrets/gcp/application_default_credentials.json` | the canonical location of your own credential, resolved from the repository root. A path, never a credential. `secrets/` is excluded from Git and every build context. The stack mounts the file read-only at `/gcp/adc.json` in the worker: the only service that dispatches meshes |
| `OPENFOAM_BASHRC` | `/usr/lib/openfoam/openfoam2412/etc/bashrc` | inside the mesh image |
| `OPENFOAM_COMMAND_TIMEOUT` | `1500` | per OpenFOAM command, in seconds |
| `BUILDER_LOOP_TIMEOUT` | `3600` | must be at least `2 * OPENFOAM_COMMAND_TIMEOUT`; validated at startup |

The relationship in the last row is enforced, not advisory: a builder loop shorter than two mesher
commands would cut a run off mid-mesh.

## Agent round budgets

How many **provider rounds** one agent invocation may take. These are not tool-call budgets, an
agent may issue several tool calls inside one round. The defaults are the shipped values; leave
them alone unless you have measured a reason.

| Key | Default | Notes |
|---|---|---|
| `INTAKE_MAX_ROUNDS` | `20` | per intake conversation turn; must be a positive integer |
| `BUILDER_MAX_ROUNDS` | `60` | a first build attempt |
| `BUILDER_RETRY_MAX_ROUNDS` | `45` | a rebuild: shorter, because a retry starts from a reviewed failure |
| `REVIEWER_MAX_ROUNDS` | `60` | one review invocation |

## Data stores

Every default below is the **declared** one, which is what `.env.example` ships and what a
host-side command (`make check`, `make test`) uses. Inside the Compose stack the four host-shaped
values are overridden with the service names, `postgres`, `redis:6379`, `minio:9000`,
`searxng:8080`, so containers reach each other without you configuring anything.

### PostgreSQL

| Key | Default | Notes |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | the stack overrides this to `postgres` |
| `POSTGRES_PORT` | `5432` | |
| `POSTGRES_DB` | `meshpipeline` | |
| `POSTGRES_USER` | `meshpipeline` | |
| `POSTGRES_PASSWORD` | `localdev` | **Secret.** Fine locally; `ENV=production` requires a real value |
| `DATABASE_URL` | *(blank)* | **Secret.** A full connection string for managed PostgreSQL. When set it **is** the database and every `POSTGRES_*` value above is unused |

### Connection budget

The pool is per process, so the fleet's peak demand is a fan-out sum, not a single number.

| Key | Default | Notes |
|---|---|---|
| `DB_POOL_SIZE` | `5` | |
| `DB_MAX_OVERFLOW` | `5` | |
| `DB_POOL_TIMEOUT` | `30` | |
| `DB_POOL_RECYCLE` | `1800` | |

### Redis

| Key | Default | Notes |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | the stack overrides this to `redis:6379` |
| `REDIS_PASSWORD` | *(blank)* | **Secret.** Set in production and use a credentialed URL |

### Object storage

| Key | Default | Notes |
|---|---|---|
| `MINIO_ENDPOINT` | `localhost:9000` | the stack overrides this to `minio:9000` |
| `MINIO_PUBLIC_ENDPOINT` | `localhost:9000` | the address a **browser** reaches the store on. Signed download URLs are signed for their host, so this - not `MINIO_ENDPOINT` - is what a URL handed to a user is signed with. Blank = same as `MINIO_ENDPOINT` |
| `MINIO_ACCESS_KEY` | `minioadmin` | |
| `MINIO_REGION` | `us-east-1` | signed into every URL as part of the SigV4 credential scope, and passed explicitly so the client never makes a GetBucketLocation call to discover it |
| `MINIO_SECRET_KEY` | `minioadmin` | **Secret.** MinIO's own local default |
| `MINIO_BUCKET` | `mesh-artifacts` | must be lowercase; an uppercase value is rejected and the stack never becomes ready |
| `MINIO_SIGNED_URL_TTL` | `900` | the SHORTEST life of a download link, in seconds. A large file's link lives as long as the file takes to fetch at 1 Mbit/s (a 900 MB deck: 2 h), up to 12 h, so a slow or resumed download is never refused half-way. The console asks for a new link on every Download click |

## Web search

| Key | Default | Notes |
|---|---|---|
| `WEB_SEARCH_ENABLED` | `true` | `false` removes the capability without breaking a run |
| `WEB_SEARCH_PROVIDER` | `searxng` | |
| `WEB_SEARCH_BASE_URL` | `http://localhost:8080` | the stack overrides this to `searxng:8080` |

## Authentication and environment

Hardening is **not** keyed on the literal name `production`. One classifier decides it:
`requires_hardened_runtime()` in `settings/policy.py`. It returns false only for the declared
development names and true for everything else, so `staging`, `prod`, `hosted` and any name it has
never seen are all hardened. An unrecognised environment fails closed rather than being treated as
development.

The declared development names are generated below, from that same authority:

<!-- BEGIN GENERATED DEVELOPMENT ENVIRONMENTS -->

`ci` `dev` `development` `local` `test` `testing`

<!-- END GENERATED DEVELOPMENT ENVIRONMENTS -->

Classification lower-cases the value and does nothing else. `DEV` is therefore the development
environment; `" dev "` with surrounding whitespace is **not**: it is hardened, which is the safe
direction and is deliberate.

In a hardened environment the process refuses to start unless:

| Requirement | Owner |
|---|---|
| `MESH_API_KEY` **and** `USER_TOKEN_SECRET` are both set | `settings/policy.py`, at import |
| `CORS_ORIGINS` is not the wildcard `*` | `runtime/startup.py` |
| a database is configured: **either** `DATABASE_URL` (a managed connection string carrying its own credentials) **or** `POSTGRES_PASSWORD` for the `POSTGRES_*` parts | `runtime/startup.py` |
| every enabled provider route has its credential | `runtime/startup.py`, from the route/provider authority |
| durable graph checkpointing stays on | `settings/policy.py` |

| Key | Default | Notes |
|---|---|---|
| `ENV` | `dev` | any name; only the declared development names above are exempt from hardening |
| `MESH_API_KEY` | *(blank)* | **Secret.** Required in every hardened environment |
| `USER_TOKEN_SECRET` | *(blank)* | **Secret.** Required in every hardened environment: it is not optional there. HMAC-signs `X-User-Id`; without it identity is self-asserted |
| `CORS_ORIGINS` | `*` | must not be `*` in a hardened environment |
| `FIREBASE_PROJECT_ID` | *(blank)* | the Identity Platform project whose ID tokens the console signs in with; deploy scripts default it to `GCP_PROJECT_ID` since Identity Platform lives in the same project |

Development keeps the looser behaviour on purpose: a bare clone runs single-tenant with
self-asserted identity, and the same conditions produce warnings instead of refusals.

The migration wrapper separately refuses to migrate a database whose host looks like the local
Compose stack, so a hosted rollout cannot target a laptop's PostgreSQL.

`USER_TOKEN_SECRET` is the difference between an asserted identity and a proven one. Set it
anywhere the deployment is reachable by someone you do not control.

### The operational surface

The same classifier decides which operational routes the application registers at all. There is no
setting for this: an environment that is hardened for authentication is hardened for its operational
surface, and the two can never disagree.

| Path | Development | Hardened |
|---|---|---|
| `/api/docs`, `/api/redoc` | served | **not registered** |
| `/openapi.json` | served | **not registered** |
| `/metrics` | served | **not registered** |
| `/health`, `/readyz` | served | served |

Hidden means *absent*, not *refused*: the route is never added to the router, so a request for it
gets the same ordinary 404 as any other unknown path. A 401 would be worse than serving the page,
because a challenge confirms that something is there to authenticate against.

Suppressing `/metrics` removes only the scrape endpoint. Instrumentation still runs and still
records every request, so a deployment that wants metrics can read them from the process without
the application publishing a public scrape URL.

`/health` and `/readyz` stay available in every environment. They are what an orchestrator restarts
and routes on, and they report liveness and readiness only.

### Request limits at the edge

`RATE_LIMIT_PER_MINUTE` bounds requests per identity per minute across the HTTP API. Two boundaries
are worth stating because they are easy to assume wrongly:

- **Minting a WebSocket ticket is ordinary API traffic.** `POST /api/v1/ws/ticket` authenticates,
  signs a ticket and reads the database, so it is counted like any other request. It is not exempt.
- **The socket itself is not HTTP traffic.** The limiter returns immediately for any connection
  that is not an HTTP request, so a long-lived stream is never charged per message and cannot be
  throttled mid-session. Session length is bounded by `WS_MAX_SESSION_SECONDS` instead.

Exempt from the limit: `/health`, `/readyz`, `/metrics`, and the `/static` and `/ui` asset trees.
Matching is by path segment, so `/ui/app.js` is exempt and `/uifoo` is not.

## What a live page may show

`PUBLIC_TRACE_MODE` is a **publication** policy: what the live run page is allowed to display. It
is deliberately independent of `DATA_COLLECTION_ENABLED`, which is a **retention** policy.

| Key | Default | Notes |
|---|---|---|
| `PUBLIC_TRACE_MODE` | `raw` | `safe` publishes reasoning and tool activity without content |
| `ALLOW_PUBLIC_RAW_TRACE` | `true` | a second acknowledgement, required before `raw` takes effect |

`raw` additionally publishes provider reasoning, real tool names, arguments, results and the
reviewer's inspection images. Secrets and model or provider identity are redacted in **both**
modes.

## Data collection

One switch, default on. With it on, a run's captured operations are stored per tenant and the
finished run is exported to the local corpus; with it off nothing optional is recorded.

| Key | Default | Notes |
|---|---|---|
| `DATA_COLLECTION_ENABLED` | `true` | the only data-collection setting |
| `DATA_ROOT` | `./data` | operational per-job files; the stack sets `/srv/data` in containers |
| `CORPUS_DIR` | `./data/corpus` | written only while collection is on; **not swept**: see below |

Consequences of each mode: [operating-modes.md](../architecture/operating-modes.md#data-collection).

## Quotas

| Key | Default | Notes |
|---|---|---|
| `MAX_JOBS_PER_OWNER` | `5` | concurrent jobs one owner may hold |
| `MAX_CONCURRENT_JOBS` | `20` | across the deployment |
| `SIGNUP_GRANT_CREDITS` | `100` | credits a newly provisioned organisation is granted once; `0` disables the grant |
| `CREDIT_GATE_ENABLED` | `true` | refuse a new job from an organisation with no paid plan once its balance cannot cover one (its running jobs each hold back `JOB_BASE_CREDITS`). A subscriber is never refused - its overage is metered. Turn off only on a personal dev environment, never on a public deployment |
| `CONSOLE_SIGNUP_ENABLED` | `true` | whether an unknown Identity Platform account may provision itself an organisation on first sign-in; an API setting, not a console one - see [identity-platform.md](../deployment/identity-platform.md#5-opening-and-closing-signup-console_signup_enabled) |
| `CELERY_WORKER_CONCURRENCY` | `2` | pipeline runs per worker process |
| `GEOMETRY_CHECK_WORKER_CONCURRENCY` | `2` | uploads the geometry-check worker draws at once. Compose only: the worker fleet runs two per instance, fixed in `deploy/gcp/worker/startup.sh`. The check has a queue of its own (`geometry_checks`) so an upload is drawn while a mesh runs, never after it |

## Stalled jobs and the maintenance sweep

A job is failed truthfully when nothing will finish it. Two rules, both in
`application/maintenance/cleanup.py`, and either is enough:

| Rule | A job is reaped when | Setting |
|---|---|---|
| the lease | it is `running` and its lease has been expired for a whole further lease period - by default about thirty minutes after the worker's last heartbeat | `WORKER_LEASE_SECONDS` |
| the ceiling | it is `pending`/`queued` and was created, or is `running` without ever having held a lease and started, more than this many hours ago. A running job with a lease is judged by the lease alone: this ceiling (4 h) is shorter than the pipeline deadline (6 h), so it must never apply to a live job | `STALLED_JOB_TIMEOUT_HOURS` |

The lease rule is the one that matters on a worker fleet. When the autoscaler replaces the
instance running a job, the heartbeat stops and nothing takes the job over: a task is acknowledged
on receipt, so it is never redelivered. Without the rule the job sat as `running` for four hours,
and because a running job holds back its base credits (`JOB_BASE_CREDITS`), its organisation was
refused every new job for that long.

A reaped job is marked `failed` and its live stream gets a closing line ("this job stopped
unexpectedly... please resubmit"). A job that reached a real result in the meantime is never
overwritten: the update is a compare-and-set on the stalled states, so the sweep is safe to run
twice and safe to run beside a worker.

Where the schedule runs:

| Stack | Scheduler | Runs |
|---|---|---|
| Compose | the `beat` service enqueues the schedule in `celery_app.py`; `worker-utility` consumes it | the reaper, the upload purge, the orphan reconcile, the failed-workspace purge, the meter sweep |
| Hosted (GCP) | no beat. `<deployment>-maintenance-sweep`, a Cloud Run job on the application image as the API's identity, every ten minutes (`create-maintenance-sweep.sh`, `images` component) | the reaper, the upload purge (`UPLOAD_RETENTION_DAYS`), the orphan reconcile. The meter sweep is its own job. The failed-workspace purge (`FAILED_JOB_RETENTION_HOURS`) is compose-only: on the fleet a workspace is the instance's own disk and goes with the instance |

```bash
gcloud run jobs executions list --job <deployment>-maintenance-sweep --region <region> --project <project>
gcloud logging read 'resource.labels.job_name="<deployment>-maintenance-sweep"' --limit 20 --freshness 1h
```

## Observability

| Key | Default | Notes |
|---|---|---|
| `LOG_FORMAT` | *(blank)* | `json` emits one JSON object per line; blank is human-readable |
| `LOG_LEVEL` | `INFO` | |
| `OTEL_TRACES_ENABLED` | `false` | `true` plus an endpoint ties API request, worker job and each model call into one trace |
| `OTEL_TRACES_EXPORTER` | `otlp` | `console` prints spans to stdout for a quick smoke test |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(blank)* | |
| `OTEL_SERVICE_NAME` | `mesh-api` | |
| `LANGFUSE_PUBLIC_KEY` | *(blank)* | optional; leave blank to disable |
| `LANGFUSE_SECRET_KEY` | *(blank)* | **Secret.** |
| `LANGFUSE_HOST` | *(blank)* | |

## Running it somewhere other than a laptop

Most keys apply unchanged. These are the ones you repoint, and nothing in this repository
automates it, see [operating-modes.md](../architecture/operating-modes.md) for what is and is not deployed
for you:

| Key | Local | Elsewhere |
|---|---|---|
| `POSTGRES_*` | the Compose service | any reachable PostgreSQL, or one `DATABASE_URL` instead |
| `MINIO_*` | the MinIO service | any S3-compatible service; the adapter is S3, not GCS |
| `REDIS_URL` | the Compose service | any reachable Redis |
| `ENV` | `dev` | `production`, which turns on the startup enforcement above |

`DEPLOYMENT_ID` is not one of these: it is read by the deploy scripts to name the cloud mesh
resources, and the application never sees it. Secrets are supplied as environment however your
platform does that; `make mesh-deploy` provisions the mesh job and does not manage application
secrets.

## Removed settings

<!-- BEGIN GENERATED REMOVED SETTINGS -->

<!-- Regenerate: python -m meshpipeline.settings.inventory --removed -->

Setting one of these makes the process refuse to start, naming the variable and its replacement. The configured value is never echoed, because it may be a credential.

| Retired | Refused at startup | Replacement | Why |
|---|---|---|---|
| `DEBUG_ENDPOINTS_ENABLED` | yes | see guidance | deleted with the /api/v1/debug routes it guarded. Nothing consumed them. |
| `EVENTS_LOG_TTL_HOURS` | yes | see guidance | deleted with the sweep it configured, which expired a file nothing wrote any more. |
| `EXPORT_CONVERSATION_DATA` | yes | see guidance | deleted. It gated the whole corpus sample rather than the conversation, and only ever applied behind DATA_COLLECTION_ENABLED. |
| `MESH_BACKEND` | yes | see guidance | deleted. The application always delegates meshing to the Cloud Run mesh job; it never meshes on the machine serving the UI. The native test tiers bind the local runner themselves and need no deployment setting. |
| `MINIO_USE_SSL` | yes | see guidance | deleted. The object store is a service on the local stack, reached over plain HTTP. |
| `OBJECT_STORE_BACKEND` | yes | see guidance | deleted. 'minio' was the only accepted value. The mesh-job exchange on GCS is addressed by the mesh adapter, not by this setting. |
| `OUTPUTS_BASE` | yes | see guidance | retired. The data layout is DATA_ROOT (default ./data) with jobs/<job-id>/ (always) and corpus/<job-id>/ (DATA_COLLECTION_ENABLED only): set DATA_ROOT / JOBS_DIR / CORPUS_DIR. |
| `PLANNER_TIMEOUT_SECONDS` | yes | see guidance | removed. The planner's per-attempt ceiling is PLANNER_TIMEOUT, one of the twelve declared settings of the planner model route. The old name briefly survived as the route's default and now controls nothing, so it is refused rather than read and discarded. |
| `REVIEWER_MAX_TOOL_CALLS` | yes | see guidance | renamed to REVIEWER_MAX_ROUNDS: it limits provider ROUNDS, not tool calls. The old name is not reused: a value set for a round budget must not silently become a tool-call budget. |
| `RUNTIME_DATA_ROOT` | yes | see guidance | retired. The data layout is DATA_ROOT (default ./data) with jobs/<job-id>/ (always) and corpus/<job-id>/ (DATA_COLLECTION_ENABLED only): set DATA_ROOT / JOBS_DIR / CORPUS_DIR. |
| `RUNTIME_SAMPLES_DIR` | yes | see guidance | retired. The data layout is DATA_ROOT (default ./data) with jobs/<job-id>/ (always) and corpus/<job-id>/ (DATA_COLLECTION_ENABLED only): set DATA_ROOT / JOBS_DIR / CORPUS_DIR. |
| `TRACE_CAPTURE_ENABLED` | yes | see guidance | deleted. It gated a trace.jsonl file nothing read, and because it wrapped the durable write it silenced capture entirely: so a deployment that had collection on got no records. DATA_COLLECTION_ENABLED is the one switch. |
| `UPLOADS_BASE` | yes | see guidance | retired. The data layout is DATA_ROOT (default ./data) with jobs/<job-id>/ (always) and corpus/<job-id>/ (DATA_COLLECTION_ENABLED only): set DATA_ROOT / JOBS_DIR / CORPUS_DIR. |
| `UPLOAD_STAGING_ROOT` | yes | see guidance | retired. The data layout is DATA_ROOT (default ./data) with jobs/<job-id>/ (always) and corpus/<job-id>/ (DATA_COLLECTION_ENABLED only): set DATA_ROOT / JOBS_DIR / CORPUS_DIR. |
| `MODEL_BUDGET_<DOMAIN>` (any such name) | yes | `MODEL_DOMAIN_BUDGETS` | the variable NAME carried provider and model data; one budget per record: 'provider:account:model=N', records separated by ';'. |
| `MODEL_PRICE_<PROVIDER>_<MODEL>` (any such name) | yes | `MODEL_PRICE_OVERRIDES` | the variable NAME carried provider and model data; one price per record: 'provider:model=in,out,cached', records separated by ';'. |

<!-- END GENERATED REMOVED SETTINGS -->

### Migrating the two retired override families

Both used to be configured one variable per model or per quota domain, with the provider and model
encoded in the *variable name*. That form is no longer read. Each is now a single setting whose
value carries the records.

**Prices.** `MODEL_PRICE_OVERRIDES` takes records separated by `;`, each `provider:model=in,out,cached`
(US dollars per 1M tokens: input, output, cached input).

```
MODEL_PRICE_OVERRIDES=deepinfra:zai-org/GLM-5.2=0.93,3.00,0.18;deepseek:deepseek-v4-pro=0.435,0.87,0.003625
```

**Budgets.** `MODEL_DOMAIN_BUDGETS` takes records separated by `;`, each `provider:account:model=N`,
where `N` is the number of concurrent calls allowed for that quota domain.

```
MODEL_DOMAIN_BUDGETS=deepinfra:default:zai-org/GLM-5.2=8;deepseek:default:deepseek-v4-pro=16
```

For both settings:

- the provider must be one this product supports; an unsupported provider is refused, and an
  override cannot introduce a new one
- a duplicated key is refused rather than silently taking the last value
- prices must be numeric, finite and non-negative; budgets must be positive whole numbers
- an empty or unset value means "no overrides", which is the default
- both are ordinary operator settings and appear in `.env.example`

A blank line, a trailing `;` or surrounding whitespace is tolerated. Anything else is refused at
startup, naming the setting and what was wrong, never echoing the value.

## Repair operations held behind the package reviewer

`CAD_REPAIR_REBUILD_FACE` (default `false`) admits `rebuild_face`: when gmsh's surface stage
reports overlapping facets on a face, the operation re-trims that face from its own boundary on
the same underlying surface (`BRepBuilderAPI_MakeFace` + `ShapeFix_Face`) and re-stitches it into
its owning body. With the flag on, the rules baseline prefers it for `mesh_overlapping_facets`
and falls back to `fix_shape_global` on the region's next attempt; with it off, the mapping is
unchanged.

**This flag must not be set live on customer jobs until the semantic package reviewer
(`CAD_REPAIR_PACKAGE_REVIEW=live`) is live.** A re-trim can move a feature, and the reviewer is
what replaces the numeric deviation gate for an operation like that: the sampled deviation the
operation records (`deviation_mm`) is evidence handed to the reviewer, never a refusal. The code
does not enforce the coupling - the deployment posture owns it - but startup logs a warning when
`CAD_REPAIR_REBUILD_FACE` is on while `CAD_REPAIR_PACKAGE_REVIEW` is not `live`.

`CAD_REPAIR_CLOSE_WIRE_GAPS` (default `false`) admits `close_wire_gaps`, the first operation
licensed to **move vertices**. Aimed at one face (a `mesh_open_loop` gmsh recorded in its surface
stage, or a kernel `wire_gap`), it merges the disconnected vertices of the face's wires that lie
within the run's tolerance (`ShapeFix_Wire.FixConnected`), then closes the remaining curve gaps
(`FixGaps3d`) on the face and on every neighbour sharing a moved vertex, and re-stitches the
owning body. It measures what it did and declares it: `vertices_moved`, `max_vertex_move_mm`
(the measured largest displacement), and the area change. It declines `gaps_exceed_tolerance`
when the measured gap or the measured move is larger than the run's tolerance, and `not_closed`
when the fixer closed nothing - a no-op is never kept. It is conservative by design: a gap at or
below the face's own vertex tolerance is not a gap, and is declined - a missed repair is preferred
to damage. Curve ends `FixGaps3d` reshapes are bounded by the run's tolerance but not measured,
and the evidence says so. With the flag on, the rules baseline
prefers it for a surface-stage `mesh_open_loop` and falls back to `fix_face_boundary`; a
`wire_gap` still meets `fix_face_boundary` (which moves nothing) first. With the flag off, nothing
changes.

**This flag must not be set live on customer jobs until the semantic package reviewer
(`CAD_REPAIR_PACKAGE_REVIEW=live`) is live.** The move is bounded and measured, but a moved vertex
is a changed part, and the reviewer is what replaces a numeric gate for an operation like that.
Startup logs a warning when `CAD_REPAIR_CLOSE_WIRE_GAPS` is on while `CAD_REPAIR_PACKAGE_REVIEW` is
not `live`.

`CAD_REPAIR_RESOLVE_SUB_MESH_GAP` (default `false`) admits `resolve_sub_mesh_gap`, the answer to a
**sub-mesh-size gap**: two faces of one solid that do not touch, but sit closer together than the
default surface mesh's chord sag, so gmsh's triangles on one pass through the other (an edge
through a face, or overlapping facets). On the real-failure corpus these are almost all designed
thin walls - coaxial cylinders, cones, tori and spheres at round offsets - so the gap is never
closed. Instead the target face and the faces of its solid across the gap are split along their
own iso-curves until a chord across a patch sags less than half the gap, which gives gmsh's default
mesh vertices where the curvature needs them. No surface moves; the boundary edges an iso-curve
ends on are split on their own curve in both faces sharing them. It declares the measured gap,
the mesh size, the faces split, the iso-curves used and the whole part's measured area and volume
change (integration noise, rounded away from zero), and declines `no_sub_mesh_gap`, `flat`,
`too_fine`, `split_failed`, `invalidated` or `geometry_moved` (area or volume beyond noise). A clash
measured at 0 mm is a real intersection and is not aimed at. With the flag on, the rules baseline
prefers it for a gap-located failure and keeps the code's previous choice as its fallback. It
re-partitions the customer's faces, so it is never auto-accepted. With the flag off, nothing
changes.

## The auto-accept tier

`CAD_REPAIR_AUTO_ACCEPT` (default `off`) decides whether a repair package that passed its
predicate may be accepted WITHOUT the model reviewer. `off` is byte-for-byte the prior behaviour:
every such package goes to `CAD_REPAIR_PACKAGE_REVIEW`. `live` adds one tier in front of that
reviewer (`cad/repair/review_tier.py`), and it accepts a package only when ALL of these hold:

- the run declared a CUSTOMER DEVIATION THRESHOLD above zero - without one the posture is inert
  and every package is reviewed, which is the fail-closed direction;
- no kept operation moves vertices: `rebuild_face` and `close_wire_gaps` ride with the reviewer at
  any threshold and under any deviation, by construction in both the deploy expressions and the
  tier;
- nothing was discharged by relabelling and nothing new appeared under the repair;
- the kept operations' declarations cover what was measured, under the same residuals `caps` uses;
- the deviation was MEASURED - not off, not out of time, not failed - and is at or under the
  threshold.

An auto-accept is AUDITED, never silent: the package verdict records `review_tier: "auto"`, the
reason the tier gave and the same clipped evidence the reviewer would have been sent. A package the
tier declines records `review_tier: "reviewer"` beside the reviewer's own verdict. Neither key
appears at all when the review posture is off.

## Attempts bought by what the part costs

`CAD_REPAIR_BUDGET_ATTEMPTS` (default `off`) decides where a region's PACKAGE ALLOWANCE comes
from. `off` is byte-for-byte the prior behaviour: every region of every part gets the one flat
`CAD_REPAIR_REGION_ATTEMPTS`, and the report gains no key. `live` reads the part's expected mesh
cost instead (`cad/repair/grind.ATTEMPTS`, keyed by `grind.tier_of`):

| tier | what it means | packages per region |
|---|---|---|
| `cheap` | gmsh refused in the surface stage, or meshed, on a modest part | 3 |
| `standard` | gmsh built the whole volume before it refused | 6 |
| `heavy` | gmsh never answered, or the part has over 1,000 faces | 10 |
| `unknown` | no face count, so nothing is known | 3 - the cheapest, fail-closed |

The tier is fixed ONCE, from the run's first whole-shape scan and the upload's own face and solid
counts, before any region is authored for: deciding it per attempt would let a package that changed
the face count change the budget it is judged under. An `attempts` argument a caller states
explicitly still wins, and the record says which rule was in force (`attempt_budget: {tier,
attempts, rule}`, under `measurements["regions"]`).

NOTHING ELSE MOVES. `CAD_REPAIR_MAX_OPS`, the run's deadline, each package's own `PackageBudget`
and the two-rejections review escalation are all still outside this allowance and are all still
checked before every attempt, so a heavy part cannot grind past any of them and a rejected package
is never retried until it is accepted. The allowance is read only by the region-scoped driver, so
startup logs a warning when it is `live` while `CAD_REPAIR_REGION_LOOP` is not.

## Replay for agent-driven runs

A rules-only repair is reproduced from its own bytes, and the native determinism tests pin that.
An AGENT-driven one cannot be: an investigator's or a tournament's proposal is not a function of
the part, so re-running the agent proves nothing about the run being reproduced. What is
reproducible is what it DECIDED, so a run with `CAD_REPAIR_REGION_LOOP=live` and at least one agent
posture on (`CAD_REPAIR_INVESTIGATOR` in `shadow`/`live`, or `CAD_REPAIR_TOURNAMENT=live`) records
one entry per COMMITTED package under `measurements["regions"]["packages_replay"]`: the region's
sorted fingerprints, the plan as it was offered, the claim, the run's tolerance and the recorded
review verdict and tier. `cad/repair/replay.py` re-applies those entries in order through a fresh
`RepairSession.try_package`, with the recorded verdict standing in for the review so no model is
called. Each entry's region is found by its fingerprints against a FRESH decomposition, and an
entry with no exact match is recorded as `replay_miss` - never applied to the nearest-looking
region. The key is absent entirely from a rules-only run's report.

## The repair investigator's budget

`CAD_REPAIR_INVESTIGATOR` (default `off`) decides who authors a failing region's target package.
`off` is byte-for-byte the prior behaviour: the investigator modules are never imported and the
single-shot model author (`CAD_REPAIR_LLM_AUTHOR`) is selected exactly as before. `shadow` runs an
investigation beside the driving author, records what it proposed, and executes nothing of it -
and only when the whole investigation fits in the time left, grace included, so a shadow run can
never cost the live repair anything. `live` lets the investigation author the package, which is
still a claim the session proves under the same caps and still goes to the package reviewer. Only
the exact strings `shadow` and `live` turn it on; anything else reads as `off` and says so in the
log. The posture does nothing without `CAD_REPAIR_REGION_LOOP=live` - only the region-scoped driver
reads the author seam - and startup warns when it is asked for without it.

**What an investigation may spend is a per-part compute tier, not a fixed number of attempts.**
The part's tier comes from the kind of mesh failure gmsh reported, the stage it was refused in and
the face count (`cad/repair/grind.py`), and the tier picks one of three budgets - tool calls, model
calls and wall seconds. A part nobody could measure is `unknown`, and `unknown`, like any
unrecognised tier, gets the **smallest** budget: the fail-closed direction is less spend, never
more. The budgets themselves are not settings. They are policy constants moved only by the corpus
gym, because a budget is a number measured offline rather than a deployment knob.

Three settings bound the spend a deployment does own:

- `CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S` (default `20.0`) is the longest one investigator model
  call may take. It is cut further to what is left of the region's wall and of the run, so a
  generous value cannot outlive the run.
- `CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX` (default `48`) is a **run-wide** ceiling across every
  region, shadow or live, above the per-region budgets that bound one region each.
- `CAD_REPAIR_INVESTIGATOR_REGION_SHARE` (default `0.5`) is the fraction of the time left above the
  run's final reserve that one region's investigation may take, so the first failing region can
  never leave the next one with nothing.

An investigation has no clock of its own: it is bounded by the run's one absolute deadline less the
walk's final-verification reserve. It is also forced to conclude while the time its own proof and
review still need is in hand - one scoped mesh probe when `CAD_REPAIR_MESH_PROBE=live`, plus
`CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S` when the reviewer is live - because a package nobody has time
to prove is not a repair.

`CAD_REPAIR_INVESTIGATOR_IMAGES` (default `off`) decides whether the investigator's `look` tool may
carry a rendered image as well as its text observation. It stays `off` everywhere: nothing has ever
sent an image on the repair-review route, and whether that route accepts one is unverified. Unlike
the postures above, it is deliberately **not** plumbed through the deploy path, so no environment
can turn it on until a measured router check says it works.

## Filesystem paths

Path defaults are **relative** and resolved by runtime against the process working directory.
That is what lets one declared value be correct in both places the product runs: a checkout on
your machine, and `WORKDIR /srv` inside the image.

| Key | Default | Resolves to |
|---|---|---|
| `DATA_ROOT` | `./data` | `<checkout>/data` on a host, `/srv/data` in the image |
| `JOBS_DIR` | *derived* `<DATA_ROOT>/jobs` | follows `DATA_ROOT` |
| `CORPUS_DIR` | *derived* `<DATA_ROOT>/corpus` | follows `DATA_ROOT` |
| `WORKSPACE_BASE` | `./workspaces` | `<checkout>/workspaces`, `/srv/workspaces` |
| `STATIC_DIR` | `./ui` | `<checkout>/ui`, `/srv/ui` |

Overriding `DATA_ROOT` moves `JOBS_DIR` and `CORPUS_DIR` with it, because they are declared as
derived from it rather than as their own literal paths. Setting either one explicitly overrides
just that one.

`STATIC_DIR` is the browser client the API serves at `/ui`. The relative default finds the
repository's `ui/` on a host and the installed `/srv/ui` in the image; an explicit override wins
over both. If the resolved directory does not exist the API still starts and still serves its
API: it logs that the directory is missing and leaves `/ui` and `/static` unmounted.

## Complete settings roster

<!-- BEGIN GENERATED SETTINGS ROSTER -->

<!-- Regenerate: python -m meshpipeline.settings.inventory --reference -->

Every supported setting (318 entries). `template` settings are the ones `.env.example` carries; `internal` are advanced controls deliberately kept out of it; `external` are supplied by the platform or a library rather than by editing `.env`.

| Setting | Exposure | Read by | Secret |
|---|---|---|---|
| `OPENAI_API_KEY` | template | app | yes |
| `OPENAI_BASE_URL` | template | app |  |
| `DEEPSEEK_API_KEY` | template | app | yes |
| `DEEPSEEK_BASE_URL` | template | app |  |
| `DEEPINFRA_API_KEY` | template | app | yes |
| `DEEPINFRA_BASE_URL` | template | app |  |
| `BUILDER_MAX_TOKENS` | template | app |  |
| `DEEPSEEK_MODEL` | template | app |  |
| `REVIEWER_MAX_TOKENS` | template | app |  |
| `REVIEWER_MODEL` | template | app |  |
| `REVIEWER_PRESENCE_PENALTY` | template | app |  |
| `REVIEWER_TEMPERATURE` | template | app |  |
| `REVIEWER_TOP_P` | template | app |  |
| `CELERY_MAX_REDELIVERIES` | template | app |  |
| `CIRCUIT_FAILURE_THRESHOLD` | template | app |  |
| `CIRCUIT_HALF_OPEN_MAX` | template | app |  |
| `CIRCUIT_RECOVERY_SECONDS` | template | app |  |
| `CLAIM_WAIT_SECONDS` | template | app |  |
| `BUILDER_LOOP_TIMEOUT` | template | app |  |
| `CLOUDRUN_JOB` | template | app |  |
| `GCP_MESH_BUCKET` | template | app |  |
| `GCP_PROJECT_ID` | template | app |  |
| `GCP_REGION` | template | app |  |
| `GOOGLE_ADC_FILE` | template | compose |  |
| `OPENFOAM_BASHRC` | template | app |  |
| `OPENFOAM_COMMAND_TIMEOUT` | template | app |  |
| `BUILDER_MAX_ROUNDS` | template | app |  |
| `BUILDER_RETRY_MAX_ROUNDS` | template | app |  |
| `INTAKE_MAX_ROUNDS` | template | app |  |
| `REVIEWER_MAX_ROUNDS` | template | app |  |
| `DATABASE_URL` | template | app | yes |
| `POSTGRES_DB` | template | app |  |
| `POSTGRES_HOST` | template | app |  |
| `POSTGRES_PASSWORD` | template | app | yes |
| `POSTGRES_PORT` | template | app |  |
| `POSTGRES_USER` | template | app |  |
| `REDIS_KEY_PREFIX` | template | app |  |
| `REDIS_PASSWORD` | template | compose | yes |
| `REDIS_URL` | template | app |  |
| `MINIO_ACCESS_KEY` | template | app |  |
| `MINIO_BUCKET` | template | app |  |
| `MINIO_ENDPOINT` | template | app |  |
| `MINIO_PUBLIC_ENDPOINT` | template | app |  |
| `MINIO_REGION` | template | app |  |
| `MINIO_SECRET_KEY` | template | app | yes |
| `MINIO_SECURE` | template | app |  |
| `MINIO_SIGNED_URL_TTL` | template | app |  |
| `WEB_SEARCH_BASE_URL` | template | app |  |
| `WEB_SEARCH_ENABLED` | template | app |  |
| `WEB_SEARCH_PROVIDER` | template | app |  |
| `ADMIN_API_KEY` | template | app | yes |
| `CORS_ORIGINS` | template | app |  |
| `ENV` | template | app |  |
| `FIREBASE_PROJECT_ID` | template | app |  |
| `MESH_API_KEY` | template | app | yes |
| `USER_TOKEN_SECRET` | template | app | yes |
| `ALLOW_PUBLIC_RAW_TRACE` | template | app |  |
| `DATA_COLLECTION_ENABLED` | template | app |  |
| `PUBLIC_TRACE_MODE` | template | app |  |
| `LANGFUSE_HOST` | template | app |  |
| `LANGFUSE_PUBLIC_KEY` | template | app |  |
| `LANGFUSE_SECRET_KEY` | template | app | yes |
| `GEOMETRY_CHECK_ENABLED` | template | app |  |
| `GEOMETRY_CHECK_VISION_TIMEOUT_S` | internal | app |  |
| `MESH_PROGRESSIVE_SNAPSHOT_ENABLED` | template | app |  |
| `CAD_INSPECT_MAX_BYTES` | internal | app |  |
| `CAD_INSPECT_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_ALLOW_FACE_REMOVAL` | internal | app |  |
| `CAD_REPAIR_AUTONOMOUS` | internal | app |  |
| `CAD_REPAIR_AUTO_ACCEPT` | internal | app |  |
| `CAD_REPAIR_BUDGET_ATTEMPTS` | internal | app |  |
| `CAD_REPAIR_CLOSE_WIRE_GAPS` | internal | app |  |
| `CAD_REPAIR_ENABLED` | template | app |  |
| `CAD_REPAIR_ESCALATE_REQUIRES_ATTEMPTS` | internal | app |  |
| `CAD_REPAIR_FILL_NONPLANAR_HOLES` | internal | app |  |
| `CAD_REPAIR_FILL_PLANAR_HOLES` | internal | app |  |
| `CAD_REPAIR_FIX_SMALL_FACES` | internal | app |  |
| `CAD_REPAIR_FRAGMENT_BODIES` | internal | app |  |
| `CAD_REPAIR_HEAL_SELF_INTERSECTIONS` | internal | app |  |
| `CAD_REPAIR_INVESTIGATION_TRACE` | internal | app |  |
| `CAD_REPAIR_INVESTIGATOR` | internal | app |  |
| `CAD_REPAIR_INVESTIGATOR_CALL_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_INVESTIGATOR_IMAGES` | internal | app |  |
| `CAD_REPAIR_INVESTIGATOR_MODEL_CALLS_MAX` | internal | app |  |
| `CAD_REPAIR_INVESTIGATOR_REGION_SHARE` | internal | app |  |
| `CAD_REPAIR_LLM_AUTHOR` | internal | app |  |
| `CAD_REPAIR_LLM_AUTHOR_CALLS_MAX` | internal | app |  |
| `CAD_REPAIR_LLM_AUTHOR_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_LLM_PLANNER` | internal | app |  |
| `CAD_REPAIR_LLM_PLANNER_CALLS_MAX` | internal | app |  |
| `CAD_REPAIR_LLM_PLANNER_MIN_CONFIDENCE` | internal | app |  |
| `CAD_REPAIR_LLM_PLANNER_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_MAX_AREA_CHANGE_RATIO` | internal | app |  |
| `CAD_REPAIR_MAX_DEVIATION_RATIO` | internal | app |  |
| `CAD_REPAIR_MAX_HOLE_SPAN_RATIO` | internal | app |  |
| `CAD_REPAIR_MAX_OPS` | internal | app |  |
| `CAD_REPAIR_MAX_TOLERANCE_MM` | internal | app |  |
| `CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO` | internal | app |  |
| `CAD_REPAIR_MEMORY` | internal | app |  |
| `CAD_REPAIR_MESHABILITY_PASS` | internal | app |  |
| `CAD_REPAIR_MESH_HANDBACK` | internal | app |  |
| `CAD_REPAIR_MESH_PROBE` | internal | app |  |
| `CAD_REPAIR_MESH_PROBE_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_PACKAGE_REVIEW` | internal | app |  |
| `CAD_REPAIR_PACKAGE_REVIEW_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_REBUILD_FACE` | internal | app |  |
| `CAD_REPAIR_REGION_ATTEMPTS` | internal | app |  |
| `CAD_REPAIR_REGION_LOOP` | internal | app |  |
| `CAD_REPAIR_RESOLVE_SUB_MESH_GAP` | internal | app |  |
| `CAD_REPAIR_REVIEW_ENABLED` | template | app |  |
| `CAD_REPAIR_REVIEW_LLM_CALLS_MAX` | internal | app |  |
| `CAD_REPAIR_REVIEW_LLM_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_REVIEW_OBJECTION` | internal | app |  |
| `CAD_REPAIR_REVIEW_SAMPLE_SIZE` | internal | app |  |
| `CAD_REPAIR_SLIVER_PRECISION_RATIO` | internal | app |  |
| `CAD_REPAIR_SPLIT_CLOSED_FACES` | internal | app |  |
| `CAD_REPAIR_STALL_LIMIT` | internal | app |  |
| `CAD_REPAIR_STANDALONE_JOBS` | internal | app |  |
| `CAD_REPAIR_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_TOLERANT_PROBE` | internal | app |  |
| `CAD_REPAIR_TOLERANT_PROBE_TIMEOUT_S` | internal | app |  |
| `CAD_REPAIR_TOURNAMENT` | internal | app |  |
| `CAD_REPAIR_TOURNAMENT_K` | internal | app |  |
| `CELERY_WORKER_CONCURRENCY` | template | compose |  |
| `CONSOLE_SIGNUP_ENABLED` | template | app |  |
| `CREDITS_PER_MESH_MINUTE` | template | app |  |
| `CREDIT_GATE_ENABLED` | template | app |  |
| `GEOMETRY_CHECK_WORKER_CONCURRENCY` | template | compose |  |
| `JOB_BASE_CREDITS` | template | app |  |
| `MAX_CONCURRENT_JOBS` | template | app |  |
| `MAX_JOBS_PER_OWNER` | template | app |  |
| `RECONCILE_RETRY_DELAY_SECONDS` | template | app |  |
| `SIGNUP_GRANT_CREDITS` | template | app |  |
| `SIGNUP_GRANT_WITHHELD_DOMAINS` | template | app |  |
| `CONSOLE_BASE_URL` | template | app |  |
| `EXTRA_CREDITS_AMOUNT` | template | app |  |
| `STRIPE_API_KEY` | template | app | yes |
| `STRIPE_PRICE_EXTRA_CREDITS` | template | app |  |
| `STRIPE_PRICE_STARTER` | template | app |  |
| `STRIPE_PRICE_STARTER_OVERAGE` | template | app |  |
| `STRIPE_PRICE_TEAM` | template | app |  |
| `STRIPE_PRICE_TEAM_OVERAGE` | template | app |  |
| `STRIPE_WEBHOOK_SECRET` | template | app | yes |
| `DB_MAX_OVERFLOW` | template | app |  |
| `DB_POOL_RECYCLE` | template | app |  |
| `DB_POOL_SIZE` | template | app |  |
| `DB_POOL_TIMEOUT` | template | app |  |
| `LOG_FORMAT` | template | app |  |
| `LOG_LEVEL` | template | app |  |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | template | sdk |  |
| `OTEL_SERVICE_NAME` | template | app |  |
| `OTEL_TRACES_ENABLED` | template | app |  |
| `OTEL_TRACES_EXPORTER` | template | app |  |
| `CORPUS_DIR` | template | app |  |
| `DATA_ROOT` | template | app |  |
| `JOBS_DIR` | template | app |  |
| `STATIC_DIR` | template | app |  |
| `WORKSPACE_BASE` | template | app |  |
| `EVENT_LOG_TTL_SECONDS` | template | app |  |
| `RATE_LIMIT_ADDRESS_CEILING_PER_MINUTE` | template | app |  |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | template | app |  |
| `RATE_LIMIT_PER_MINUTE` | template | app |  |
| `RATE_LIMIT_TRUSTED_PROXY_HOPS` | template | app |  |
| `WS_MAX_SESSION_SECONDS` | template | app |  |
| `FAILED_JOB_RETENTION_HOURS` | template | app |  |
| `PIPELINE_TOTAL_TIMEOUT_SECONDS` | template | app |  |
| `STALLED_JOB_TIMEOUT_HOURS` | template | app |  |
| `UPLOAD_RETENTION_DAYS` | template | app |  |
| `PIPELINE_BACKEND` | template | app |  |
| `REQUIRE_DURABLE_CHECKPOINTER` | template | app |  |
| `WORKER_HEARTBEAT_SECONDS` | template | app |  |
| `WORKER_IDLE_RETIRE_MINUTES` | template | app |  |
| `WORKER_LEASE_SECONDS` | template | app |  |
| `WORKER_WAKE_MINUTES` | template | app |  |
| `CAD_FIDELITY_RETRY_ENABLED` | template | app |  |
| `CELL_HARD_LIMIT` | template | app |  |
| `CFD_WRAP_ENABLED` | template | app |  |
| `CFD_WRAP_TIME_S` | template | app |  |
| `DOMAIN_EXTENT_GATE_ENABLED` | template | app |  |
| `ENGINE_FALLBACK_ENABLED` | template | app |  |
| `LAYER_CAVEAT_FLOOR_PCT_EXTERNAL` | template | app |  |
| `LAYER_CAVEAT_PATCH_MIN_THICKNESS_PCT` | template | app |  |
| `MESH_SCRIPT_SCAN_ENABLED` | template | app |  |
| `RUN_PYTHON_REQUIRE_SANDBOX` | template | app |  |
| `SOLVABILITY_GATE_ENABLED` | template | app |  |
| `VERDICT_BY_LIMITS` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_BYTES` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_DEPTH` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_ENTRIES` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_FILE_BYTES` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_PATH_LEN` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_RATIO` | template | app |  |
| `WORKSPACE_ARCHIVE_MAX_TOTAL_BYTES` | template | app |  |
| `DISPUTE_MAX_FLAGS` | template | app |  |
| `VIEWER_FINE_FILL` | template | app |  |
| `VIEWER_FLAG_SPAN_FACTOR` | template | app |  |
| `VIEWER_FRAME_FRAC` | template | app |  |
| `VIEWER_GRID_PX` | template | app |  |
| `BUILDER_INFRA_RETRY_BACKOFF_S` | template | app |  |
| `BUILDER_INFRA_RETRY_MAX` | template | app |  |
| `DEEPINFRA_CALL_TIMEOUT` | template | app |  |
| `DEEPINFRA_CONNECT_TIMEOUT` | template | app |  |
| `DEEPINFRA_READ_TIMEOUT` | template | app |  |
| `DEEPINFRA_WRITE_TIMEOUT` | template | app |  |
| `MAX_BUILDER_RETRIES` | template | app |  |
| `MAX_SNAPPY_ATTEMPTS` | template | app |  |
| `REVIEWER_RERUN_BACKOFF_S` | template | app |  |
| `REVIEWER_RERUN_MAX` | template | app |  |
| `TAVILY_API_KEY` | template | app | yes |
| `WEB_SEARCH_MAX_RESULTS` | template | app |  |
| `WEB_SEARCH_TIMEOUT` | template | app |  |
| `BUILDER_AI` | internal | app |  |
| `FEA_AI` | internal | app |  |
| `FEA_MMG` | internal | app |  |
| `VMTK_BIN` | template | app |  |
| `AI_DECISION_CACHE` | internal | app |  |
| `MESH_CFMESH_THREADS` | internal | app |  |
| `MESH_DETERMINISTIC` | internal | app |  |
| `MESH_GMSH_THREADS` | internal | app |  |
| `MESH_SNAPPY_RANKS` | internal | app |  |
| `MESH_THREADS` | internal | app |  |
| `SENTRY_DSN` | template | app | yes |
| `BUILDER_MIN_P` | internal | app |  |
| `BUILDER_TEMPERATURE` | internal | app |  |
| `BUILDER_TOP_P` | internal | app |  |
| `INTAKE_MAX_TOKENS` | internal | app |  |
| `INTAKE_MIN_P` | internal | app |  |
| `INTAKE_TEMPERATURE` | internal | app |  |
| `PLANNER_MAX_TOKENS` | internal | app |  |
| `PLANNER_MIN_P` | internal | app |  |
| `PLANNER_TEMPERATURE` | internal | app |  |
| `PLANNER_TOP_P` | internal | app |  |
| `REVIEWER_TOP_K` | internal | app |  |
| `SEARCH_SUMMARIZER_MAX_TOKENS` | internal | app |  |
| `SEARCH_SUMMARIZER_TEMPERATURE` | internal | app |  |
| `BUILDER_AUTO_SUBMIT_AFTER` | internal | app |  |
| `BUILDER_NULL_CHOICES_SLEEP` | internal | app |  |
| `BUILDER_TOTAL_TIMEOUT_SECONDS` | internal | app |  |
| `INTAKE_GREETING_ON_UPLOAD` | internal | app |  |
| `MAX_TOOL_OUTPUT_CHARS` | internal | app |  |
| `MODEL_DEFAULT_CONCURRENCY_BUDGET` | internal | app |  |
| `PLANNER_TOTAL_TIMEOUT_SECONDS` | internal | app |  |
| `REVIEWER_TOTAL_TIMEOUT_SECONDS` | internal | app |  |
| `SNAPPY_RAZOR_CELL_FACTOR` | internal | app |  |
| `SNAPPY_SEAL_CAVITIES` | internal | app |  |
| `SNAPPY_THIN_AREA_FLOOR` | internal | app |  |
| `SNAPPY_THIN_LAYER_POLICY` | internal | app |  |
| `SNAPPY_THIN_STACK_FACTOR` | internal | app |  |
| `ALEMBIC_CONFIG` | external | app |  |
| `CLOUD_RUN_EXECUTION` | external | app |  |
| `GOOGLE_APPLICATION_CREDENTIALS` | external | app |  |
| `HEXERA_SHELL_RUN_CAP` | external | app |  |
| `HEXERA_SHELL_RUN_END` | external | app |  |
| `MESH_SOURCE_TREE` | external | app |  |
| `PIPELINE_EXECUTION_ID` | external | app |  |
| `PROMETHEUS_MULTIPROC_DIR` | external | app |  |
| `MODEL_DOMAIN_BUDGETS` | template | app |  |
| `MODEL_PRICE_OVERRIDES` | template | app |  |
| `INTAKE_ACCOUNT` | template | app |  |
| `INTAKE_BACKOFF_BASE` | template | app |  |
| `INTAKE_BACKOFF_MAX` | template | app |  |
| `INTAKE_CONCURRENCY_BUDGET` | template | app |  |
| `INTAKE_MAX_ATTEMPTS` | template | app |  |
| `INTAKE_MODEL` | template | app |  |
| `INTAKE_PROVIDER` | template | app |  |
| `INTAKE_QUEUE_DEADLINE` | template | app |  |
| `INTAKE_STANDBY_ACCOUNT` | template | app |  |
| `INTAKE_STANDBY_MODEL` | template | app |  |
| `INTAKE_STANDBY_PROVIDER` | template | app |  |
| `INTAKE_TIMEOUT` | template | app |  |
| `BUILDER_ACCOUNT` | template | app |  |
| `BUILDER_BACKOFF_BASE` | template | app |  |
| `BUILDER_BACKOFF_MAX` | template | app |  |
| `BUILDER_CONCURRENCY_BUDGET` | template | app |  |
| `BUILDER_MAX_ATTEMPTS` | template | app |  |
| `BUILDER_MODEL` | template | app |  |
| `BUILDER_PROVIDER` | template | app |  |
| `BUILDER_QUEUE_DEADLINE` | template | app |  |
| `BUILDER_STANDBY_ACCOUNT` | template | app |  |
| `BUILDER_STANDBY_MODEL` | template | app |  |
| `BUILDER_STANDBY_PROVIDER` | template | app |  |
| `BUILDER_TIMEOUT` | template | app |  |
| `VISUAL_REVIEWER_ACCOUNT` | template | app |  |
| `VISUAL_REVIEWER_BACKOFF_BASE` | template | app |  |
| `VISUAL_REVIEWER_BACKOFF_MAX` | template | app |  |
| `VISUAL_REVIEWER_CONCURRENCY_BUDGET` | template | app |  |
| `VISUAL_REVIEWER_MAX_ATTEMPTS` | template | app |  |
| `VISUAL_REVIEWER_MODEL` | template | app |  |
| `VISUAL_REVIEWER_PROVIDER` | template | app |  |
| `VISUAL_REVIEWER_QUEUE_DEADLINE` | template | app |  |
| `VISUAL_REVIEWER_STANDBY_ACCOUNT` | template | app |  |
| `VISUAL_REVIEWER_STANDBY_MODEL` | template | app |  |
| `VISUAL_REVIEWER_STANDBY_PROVIDER` | template | app |  |
| `VISUAL_REVIEWER_TIMEOUT` | template | app |  |
| `SEARCH_SUMMARIZER_ACCOUNT` | template | app |  |
| `SEARCH_SUMMARIZER_BACKOFF_BASE` | template | app |  |
| `SEARCH_SUMMARIZER_BACKOFF_MAX` | template | app |  |
| `SEARCH_SUMMARIZER_CONCURRENCY_BUDGET` | template | app |  |
| `SEARCH_SUMMARIZER_MAX_ATTEMPTS` | template | app |  |
| `SEARCH_SUMMARIZER_MODEL` | template | app |  |
| `SEARCH_SUMMARIZER_PROVIDER` | template | app |  |
| `SEARCH_SUMMARIZER_QUEUE_DEADLINE` | template | app |  |
| `SEARCH_SUMMARIZER_STANDBY_ACCOUNT` | template | app |  |
| `SEARCH_SUMMARIZER_STANDBY_MODEL` | template | app |  |
| `SEARCH_SUMMARIZER_STANDBY_PROVIDER` | template | app |  |
| `SEARCH_SUMMARIZER_TIMEOUT` | template | app |  |
| `PLANNER_ACCOUNT` | template | app |  |
| `PLANNER_BACKOFF_BASE` | template | app |  |
| `PLANNER_BACKOFF_MAX` | template | app |  |
| `PLANNER_CONCURRENCY_BUDGET` | template | app |  |
| `PLANNER_MAX_ATTEMPTS` | template | app |  |
| `PLANNER_MODEL` | template | app |  |
| `PLANNER_PROVIDER` | template | app |  |
| `PLANNER_QUEUE_DEADLINE` | template | app |  |
| `PLANNER_STANDBY_ACCOUNT` | template | app |  |
| `PLANNER_STANDBY_MODEL` | template | app |  |
| `PLANNER_STANDBY_PROVIDER` | template | app |  |
| `PLANNER_TIMEOUT` | template | app |  |

<!-- END GENERATED SETTINGS ROSTER -->
