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

## Measuring and looking at an uploaded geometry

Four gates, each off by default. The look needs the measurement; the survey needs the measurement and
its readers.

| Key | Default | Notes |
|---|---|---|
| `GEOMETRY_MEASUREMENT_ENABLED` | `false` | measure an uploaded file and store the report against its sha256 |
| `GEOMETRY_MEASUREMENT_SYNC_MAX_MB` | `4` | under this the measurement runs in the upload request; at or above it a worker takes it |
| `GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS` | `900` | one measurement's deadline |
| `GEOMETRY_REPORT_READERS_ENABLED` | `false` | whether intake, the mesh planner and the admission slot ACT on a stored measurement |
| `GEOMETRY_VISION_ENABLED` | `false` | after the measurement, describe the part from rendered views and store the words in the same row |
| `GEOMETRY_VISION_TIMEOUT_SECONDS` | `180` | one look's deadline, render included |
| `GEOMETRY_VISION_PROVIDER` | `openai` | the provider the look reads with; no key for it means no look, never another provider |
| `GEOMETRY_VISION_MODEL` | `gpt-5.6-luna` | the reader model on that provider |
| `GEOMETRY_SURVEY_ENABLED` | `false` | intake asks the measurement's own questions, stores the answers with who gave them, and the builder gets the survey |
| `GEOMETRY_AGENT_STEP_ENABLED` | `false` | the geometry agent plans the part at submission from the survey and the answers, a question only the plan raises is put once, and the builder gets both write-ups; dead unless `GEOMETRY_SURVEY_ENABLED` is on too |
| `GEOMETRY_AGENT_STEP_PROVIDER` | `deepseek` | which model plans: `deepseek`, `deepinfra`, `anthropic`, `generic`, `auto`, or `reference` for the package's deterministic stand-in. No key for it means no plan, never another provider |
| `GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS` | `300` | one plan's wall clock, and the loop's own budget; past it the step records a failure and the job runs as it does with the step off. 0 means no clock at all |
| `GEOMETRY_AGENT_LEDGER_PATH` | (empty) | a JSONL file the job ledger's rows are also appended to; the durable copy always lives on the survey row |

### What the look is

With `GEOMETRY_VISION_ENABLED` on, a background task renders seventeen views of the measured file (the
merged `geometry_agent.vision.look` builds them the same way the agent's own `look_at_views` does),
asks a vision model what it is looking at, and stores the answer in the same `geometry_measurements`
row. It needs `GEOMETRY_MEASUREMENT_ENABLED` as well: there is no row for a look to attach to without a
measurement, and the views are labelled with the openings the measurement found.

The reader is named, never discovered: `GEOMETRY_VISION_PROVIDER` at `GEOMETRY_VISION_MODEL`,
`openai` and `gpt-5.6-luna` by default. The package's own `auto` provider takes Anthropic, then OpenAI,
then DeepInfra, and this template carries only a DeepInfra key, so left to itself it would read every
part with a model the look was never measured with. With no key for the named provider the row
records a look that was not attempted and says why; it never falls through to another provider.

It produces no number. Every digit is removed from the description before it is stored, so nothing
in it can be mistaken for a measurement, and the measurement package's own trust tiers decide which
fields may reach a model at all. What the vision model says about defects, about which opening is an
inlet, about the orientation and about the symmetry is kept in the row for a person to read and is
not put in front of the mesh planner or the intake conversation. The first has never once been
right; the other three the measurement already has exactly.

What it said AT a measured place (a letter the renderer drew, the end of a passage the stop detector
found) reaches both the planner, as `at_places`, and the intake conversation, where the place is
given in millimetres and the words are marked as the model's. Counted over the four cached corpus
draws of the shipped prompt, attached to this platform's own measurement of each of the 322 parts:
2,084 placed findings a draw in the planner's block, and 0 in the intake prompt before this branch
against 2,074, 2,075, 2,077 and 2,076 after it, the rest being places the look declined to read,
which are named once rather than shown as findings.

### What it costs

The figures below were measured on 2026-09-15 on the nine-view look this branch was first written
against, with `gpt-5.6-luna` on OpenAI, on seven corpus export parts. **The shipped look renders
seventeen views, and these have not been measured again for it**: read them as the nine-view look's.
18.2 to 29.1 seconds of wall clock including the render, 7,306 to 7,350 prompt tokens and 1,301 to
2,565 completion tokens, of which 1,024 to 2,049 were reasoning tokens. Seven looks, seven succeeded.

**The dollar figure is not stated here because it has not been confirmed.** `openai:gpt-5.6-luna` is
not in `adapters/inference_telemetry/pricing.py`, and that module's own rule is that an unconfirmed
price is left out rather than guessed. The token counts above are what a rate multiplies: at a rate
of *(in, out)* dollars per million tokens the cost of one look is `7.317 * in / 1000 + 1.894 * out /
1000` dollars at the nine-view medians (7,317 prompt tokens and 1,894 completion tokens).

The seventeen-view look, measured once so far: on 2026-09-18, through this platform's own path
(`look_at_local_file` with the named reader), on ONE part, `venturi_orifice_001`, four draws of
`gpt-5.6-luna`:

| draw | seconds | prompt tokens | completion tokens | of which reasoning |
|---|---:|---:|---:|---:|
| 1 | 25.8 | 13,377 | 1,279 | 888 |
| 2 | 23.9 | 13,377 | 886 | 512 |
| 3 | 20.0 | 13,377 | 809 | 461 |
| 4 | 21.4 | 13,377 | 823 | 512 |

The wall clock was taken while a test suite ran on the same machine. One part is not a range for
the product; the prompt token count is the one number here that does not move between draws.

A second job against the same upload costs nothing at all: the look is stored with the measurement
and keyed the same way, so the task reads one row and stops, having fetched no bytes, rendered
nothing and called no provider.

The stored look no longer carries the measurement package's trust table. The tiers reach every model
through `vision.trust.for_model` at the point of use, off the live ledger, so a stored copy would
outlive the next demotion. The 5.1 to 5.6 KB the nine-view figures included for it is not in a row
written now. No new column and no migration for the look: `document` is already JSONB and already has
a `look` key.

### When the provider is down

Nothing happens, and nothing breaks. A provider error, a missing key, a reply that is not JSON, a
file this image cannot render and a look that outlives its deadline are all recorded as a look that
did not work. The measurement row keeps the measurement it already had, the mesh planner's block
carries no look key, intake renders no look lines, and the conversation and the mesh are exactly what
they are with `GEOMETRY_VISION_ENABLED` off. A queue with no worker draining it is the same answer.
The upload is never affected in any case: the look is always a queued task and never runs in a
request.

### The survey, and the order of the chain

`GEOMETRY_SURVEY_ENABLED` turns on the chain in this order, and the order is the design:

| step | who | on this platform |
|---|---|---|
| 1 | intake | the customer says what the part is for; intake calls `survey_the_part` with the purpose and their words quoted |
| 2 | measure | the bytes were read once at upload; the stored reading is now composed FOR that purpose, those words and any ports they named, by the package's own `report_measured`, in milliseconds |
| 3 | look | queued now, with the purpose and representation step 2 decided, and no longer at upload |
| 4 | intake | the package's own questions (`contract.asking.questions_from`) are put, and intake asks nothing of its own about which opening is which; `answer_survey_question` stores each answer with who gave it |
| 5 | geometry | with `GEOMETRY_AGENT_STEP_ENABLED` off, the builder's own planner decides what to do and is handed the survey as facts it may not re-decide. With it on, the geometry agent plans first (below) |
| 6 | intake | the budget trade, and only it, once every step-4 question is settled. With the step on there is a THIRD intake after it, for the question only a plan can raise |
| 7 | builder | the survey rides in `metrics["geometry_agent"]["survey"]`, after the request cut, checked by the package's own validator |

Why intake comes first, measured rather than argued: `ahmed_variant_001` is a bluff body with four
measured openings. Composed for the purpose the upload assumes it is asked which opening is the
inlet, twice; composed for what the customer said it is asked nothing. Over all 322 corpus export
parts, each measured through this platform's own upload call on 2026-09-18 and composed with its
brief's purpose, words and ports (no model involved, so every figure is deterministic):

| | composed at upload, for the assumed purpose | composed after intake |
|---|---:|---:|
| geometry questions put at step 4 | 622, on 311 parts | 151, on 151 parts |
| external parts asked which opening is the inlet | 50 of 61 | 0 of 61 |
| parts whose representation the customer's words change | | 86 |
| `customer_cell_cap` set | 0 | 322 |
| budget trades put at step 6 | | 39 |
| survey reaching the planner, passing the package's validator | 0 | 322, at most 1,620 characters |

Every one of the 151 remaining questions is the one where the brief names fewer ports than the file
has mouths. On most of those parts the extra mouths are flange shoulders the package's own catalog
already calls wall, so the count is an upper bound on what a better question list would ask. The
322 of 322 on `customer_cell_cap` rests on one budget sentence repeated across the corpus briefs.

A default is not an answer. A customer who lets a default stand is stored as `default_taken`, the
question stays open, and the builder is told it is unsettled. `submit_requirements` refuses an inlet
or outlet on a mouth the customer did not name, so no guessed role reaches `port_declaration`. A cell
budget the customer confirmed in the trade holds `max_cells` to it; one they only wrote is shown to
the planner as `customer_cell_cap` and holds nothing.

The survey is stored in `geometry_surveys` (migration 0004), one row per upload, keyed by the file's
sha256, so an answer can be bound to the part it was given about long after the conversation.

### The geometry agent's step

`GEOMETRY_AGENT_STEP_ENABLED` puts steps 5 and 6 of the chain on this platform. It is off by default
and dead unless the survey is on: every gate under it is read together, so setting one flag cannot arm
a chain whose earlier steps are off. Nothing below happens until it is set, which the repository gate
`devtools/quality/check_vision_off_is_byte_identical.py` proves by hashing what the intake model and the
mesh planner are handed in this checkout and in `3ba42c0`, the commit before the step existed.

| | with the step off | with the step on |
|---|---|---|
| step 5 | nothing runs | at `submit_requirements`, `geometry_step.at_submission` runs the package's own chain (`chain.job.plan`) on the stored survey and the customer's answers, once per set of answers. It decides the flow patches, where the cells go, and what that costs by the builder's own sizing (`tools.estimate_builder_cells`, never `tools.refined_cell_estimate`) |
| step 6 | the survey's own budget trade, and nothing after it | a THIRD intake, for a question that did not exist at step 4. The canonical one is the budget trade against what THIS PLAN costs; `contract.intake.binds_late` refuses any topic the survey already had. Put ONCE, with a default, and a default that stands is not a confirmation |
| step 7 | the builder gets the survey in the typed block | the builder gets the geometry agent's write-up in front of the request and, in the typed block after the request cut, intake's write-up, the survey, the flow patches and the plan's envelope |

**It waits for the customer.** The geometry agent plans from their answers, so step 5 does not run while
the survey still has a question to put, its own budget trade included: confirming a budget composes the
measurement again, and a plan made before that answer describes a job that no longer exists. A question
that was put and skipped, or left to its default, is not one that is waiting.

**It fails open, and it says why.** No model configured, the loop out of time, a plan the checker sends
back, a contract refusal, a survey the platform cannot compose again from its own stored inputs: each is
stored on the survey row as `status: failed` with the sentence, logged, and the job then runs exactly as
it does with the switch off. No third question, the builder's request and typed block untouched. The
builder's own read says the same sentence again when it falls back.

**Where it runs and why there.** At `submit_requirements`, not in the pipeline graph: the graph runs
after the customer has approved and left, and no node there can put a question to anybody. The
submission is the last moment the customer is still in the conversation and the first moment the engine
and the fidelity are settled. The plan is made there, once, and the builder reads it off the row.

**Measured** on 2026-09-22, deterministically, with `GEOMETRY_AGENT_STEP_PROVIDER=reference` (the
package's own stand-in policy, so no model was called and every figure reproduces from the bytes):
49 real corpus parts measured through this platform's own `measure_local_file`, surveyed, answered and
planned, then handed to the builder through `cad.regions.planner_inputs_for_state`, the call the snappy
driver makes. `devtools/quality/run_geometry_step_on_corpus.py` is the harness.

| | |
|---|---|
| planned | 49 of 49, every one `submitted` |
| reached the builder with the plan | 49 of 49 |
| representations covered | `wall_shell` 21, `external` 13, `fluid_domain` 13, `annular_fluid` 2; `unknown` on its own run, which is the only way this corpus reaches that word |
| the typed block carried the survey, intake's write-up, the flow patches and the plan's envelope | 49 of 49 |
| the envelope's source | `tools.estimate_builder_cells` on all 49, never `tools.refined_cell_estimate` |
| the ledger's stages | the same seven on all 49: brief, survey, look, uncertainty, intake, plan, handover |
| the customer's brief survived the planner's 2,000-character read | 46 of 49; on the other 3 the last 31 to 100 characters are the physics tail, which the assembly reads as having no reader |
| one plan's wall clock | median 0.21 s, longest 4.13 s, with the stand-in policy and no model call |

The look did not run in any of these: `measure_local_file` stores `not_attempted` and step 3 is a
worker, so the closed ends only the look names are absent from every row above.

**The third intake did not fire on any of the 49**, and the reason is worth knowing before turning the
step on: on 39 the plan's envelope fit the stated budget, and on the other 10 the survey had already
put its own budget question, which `contract.intake.binds_late` says is the end of it. The question
exists exactly where the plan's envelope is over a budget the measurement's own forecast was under, and
every brief in this export states two million cells where the whole corpus forecasts less. On a
customer budget inside that window it fires and runs to the end: on `manifold_001` with 750,000 stated,
the plan costs 987,828, the question is put once with "hold 750,000" as its default, and the answer
becomes `customer_cell_cap` in the builder's typed block and the mesh's own ceiling.

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

Every supported setting (217 entries). `template` settings are the ones `.env.example` carries; `internal` are advanced controls deliberately kept out of it; `external` are supplied by the platform or a library rather than by editing `.env`.

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
| `MINIO_SECURE` | template | app |  |
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
| `GEOMETRY_AGENT_LEDGER_PATH` | template | app |  |
| `GEOMETRY_AGENT_STEP_ENABLED` | template | app |  |
| `GEOMETRY_AGENT_STEP_PROVIDER` | template | app |  |
| `GEOMETRY_AGENT_STEP_TIMEOUT_SECONDS` | template | app |  |
| `GEOMETRY_MEASUREMENT_ENABLED` | template | app |  |
| `GEOMETRY_MEASUREMENT_SYNC_MAX_MB` | template | app |  |
| `GEOMETRY_MEASUREMENT_TIMEOUT_SECONDS` | template | app |  |
| `GEOMETRY_REPORT_READERS_ENABLED` | template | app |  |
| `GEOMETRY_SURVEY_ENABLED` | template | app |  |
| `GEOMETRY_VISION_ENABLED` | template | app |  |
| `GEOMETRY_VISION_MODEL` | template | app |  |
| `GEOMETRY_VISION_PROVIDER` | template | app |  |
| `GEOMETRY_VISION_TIMEOUT_SECONDS` | template | app |  |
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
