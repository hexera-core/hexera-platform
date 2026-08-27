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

### DeepSeek: intake and the search summarizer

| Key | Default | Notes |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(blank)* | **Required, secret.** |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | |
| `SEARCH_SUMMARIZER_MODEL` | `deepseek-v4-flash` | distils search results |

### DeepInfra: builder and reviewer

| Key | Default | Notes |
|---|---|---|
| `DEEPINFRA_API_KEY` | *(blank)* | **Required, secret.** |
| `DEEPINFRA_BASE_URL` | `https://api.deepinfra.com/v1/openai` | OpenAI-compatible wire protocol |
| `BUILDER_MODEL` | `zai-org/GLM-5.2` | |
| `REVIEWER_MODEL` | `Qwen/Qwen3-VL-235B-A22B-Thinking` | vision-capable: the reviewer looks at images |
| `BUILDER_MAX_TOKENS` | `16384` | the provider's per-response cap for these models |
| `REVIEWER_MAX_TOKENS` | `16384` | |
| `REVIEWER_TEMPERATURE` | `0.7` | reviewer sampling |
| `REVIEWER_TOP_P` | `0.8` | |
| `REVIEWER_PRESENCE_PENALTY` | `1.5` | |

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
| `MINIO_SIGNED_URL_TTL` | `900` | seconds a download link stays valid |

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
| `CELERY_WORKER_CONCURRENCY` | `2` | pipeline runs per worker process |

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

Every supported setting (203 entries). `template` settings are the ones `.env.example` carries; `internal` are advanced controls deliberately kept out of it; `external` are supplied by the platform or a library rather than by editing `.env`.

| Setting | Exposure | Read by | Secret |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | template | app | yes |
| `DEEPSEEK_BASE_URL` | template | app |  |
| `DEEPSEEK_MODEL` | template | app |  |
| `BUILDER_MAX_TOKENS` | template | app |  |
| `DEEPINFRA_API_KEY` | template | app | yes |
| `DEEPINFRA_BASE_URL` | template | app |  |
| `REVIEWER_MAX_TOKENS` | template | app |  |
| `REVIEWER_MODEL` | template | app |  |
| `REVIEWER_PRESENCE_PENALTY` | template | app |  |
| `REVIEWER_TEMPERATURE` | template | app |  |
| `REVIEWER_TOP_P` | template | app |  |
| `CELERY_MAX_REDELIVERIES` | template | app |  |
| `CIRCUIT_FAILURE_THRESHOLD` | template | app |  |
| `CIRCUIT_HALF_OPEN_MAX` | template | app |  |
| `CIRCUIT_RECOVERY_SECONDS` | template | app |  |
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
| `REDIS_PASSWORD` | template | compose | yes |
| `REDIS_URL` | template | app |  |
| `MINIO_ACCESS_KEY` | template | app |  |
| `MINIO_BUCKET` | template | app |  |
| `MINIO_ENDPOINT` | template | app |  |
| `MINIO_PUBLIC_ENDPOINT` | template | app |  |
| `MINIO_REGION` | template | app |  |
| `MINIO_SECRET_KEY` | template | app | yes |
| `MINIO_SIGNED_URL_TTL` | template | app |  |
| `WEB_SEARCH_BASE_URL` | template | app |  |
| `WEB_SEARCH_ENABLED` | template | app |  |
| `WEB_SEARCH_PROVIDER` | template | app |  |
| `CORS_ORIGINS` | template | app |  |
| `ENV` | template | app |  |
| `MESH_API_KEY` | template | app | yes |
| `USER_TOKEN_SECRET` | template | app | yes |
| `ALLOW_PUBLIC_RAW_TRACE` | template | app |  |
| `DATA_COLLECTION_ENABLED` | template | app |  |
| `PUBLIC_TRACE_MODE` | template | app |  |
| `LANGFUSE_HOST` | template | app |  |
| `LANGFUSE_PUBLIC_KEY` | template | app |  |
| `LANGFUSE_SECRET_KEY` | template | app | yes |
| `CELERY_WORKER_CONCURRENCY` | template | compose |  |
| `MAX_CONCURRENT_JOBS` | template | app |  |
| `MAX_JOBS_PER_OWNER` | template | app |  |
| `RECONCILE_RETRY_DELAY_SECONDS` | template | app |  |
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
| `RATE_LIMIT_PER_MINUTE` | template | app |  |
| `WS_MAX_SESSION_SECONDS` | template | app |  |
| `FAILED_JOB_RETENTION_HOURS` | template | app |  |
| `PIPELINE_TOTAL_TIMEOUT_SECONDS` | template | app |  |
| `STALLED_JOB_TIMEOUT_HOURS` | template | app |  |
| `UPLOAD_RETENTION_DAYS` | template | app |  |
| `PIPELINE_BACKEND` | template | app |  |
| `REQUIRE_DURABLE_CHECKPOINTER` | template | app |  |
| `WORKER_HEARTBEAT_SECONDS` | template | app |  |
| `WORKER_LEASE_SECONDS` | template | app |  |
| `CELL_HARD_LIMIT` | template | app |  |
| `DOMAIN_EXTENT_GATE_ENABLED` | template | app |  |
| `LAYER_CAVEAT_FLOOR_PCT_EXTERNAL` | template | app |  |
| `LAYER_CAVEAT_PATCH_MIN_THICKNESS_PCT` | template | app |  |
| `MESH_SCRIPT_SCAN_ENABLED` | template | app |  |
| `RUN_PYTHON_REQUIRE_SANDBOX` | template | app |  |
| `SOLVABILITY_GATE_ENABLED` | template | app |  |
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
| `DEEPINFRA_CALL_TIMEOUT` | template | app |  |
| `DEEPINFRA_CONNECT_TIMEOUT` | template | app |  |
| `DEEPINFRA_READ_TIMEOUT` | template | app |  |
| `DEEPINFRA_WRITE_TIMEOUT` | template | app |  |
| `MAX_BUILDER_RETRIES` | template | app |  |
| `MAX_SNAPPY_ATTEMPTS` | template | app |  |
| `TAVILY_API_KEY` | template | app | yes |
| `WEB_SEARCH_MAX_RESULTS` | template | app |  |
| `WEB_SEARCH_TIMEOUT` | template | app |  |
| `VMTK_BIN` | template | app |  |
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
| `ALEMBIC_CONFIG` | external | app |  |
| `CLOUD_RUN_EXECUTION` | external | app |  |
| `GOOGLE_APPLICATION_CREDENTIALS` | external | app |  |
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
