# Architecture

## The shape
It's **not an "OpenFOAM pipeline"**, and it's not a domain-locked one either: it's a
**capability × purpose** pipeline. An ENGINE is a physical meshing capability (a tool); the
user-declared PURPOSE (`structural` / `external_cfd` / `internal_cfd`) is the workflow. An
engine serves any purpose its DECLARED capabilities can produce from the submitted geometry -
gmsh tetrahedralizes the solid body for structural **and** a supplied fluid domain for CFD -
so the roster is never a "this engine = that domain" lock. Five engines exist today: cfMesh
(`cartesianMesh` 3D + native `cartesian2DMesh` 2D, the fast Cartesian flow engine) and
snappyHexMesh (body-fitted + prism layers, the production 3D flow engine), both producing an
OpenFOAM polyMesh; Gmsh (second-order tets from a closed CAD volume, plane-stress triangles
from a planar face); snappy_multiregion (multi-region snappyHexMesh, a coupled multi-region
OpenFOAM case from a multi-solid assembly, for whatever purpose the user declares: conjugate
heat transfer, multi-material analysis, fluid-structure interaction); and VMTK
(centerline-based, radius-adaptive tets filling a closed lumen surface, vessels, ducts,
manifolds). A sixth drops in as a catalog row + an adapter with no changes to the
orchestration. The flow regime is never inferred from keywords: `flow_topology`
(internal/external) is DERIVED from the declared purpose and travels as a neutral state
field, internal purposes mesh the cavity, external ones build the far-field domain.

```
Browser ──HTTP──> Console (Next.js) ──signed proxy──> API (stateless)
   └───────────── WebSocket, straight to the API ─────────────┘
                                                              │
                                                           enqueue
                                                              ↓
                              Celery worker ──runs──> LangGraph state machine

  intake → engine_select → geometry_admission → builder → executor
         → {classifier → builder}* → reviewer → END
    (any node may set api_failure → node_failure_handler sink; a geometry-admission
     rejection short-circuits builder entirely: the executor reports it)

  after the graph ends, ordinary application code:
    deliver required artifacts → commit terminal status → build final_result
      → persist final_result → publish the rendered message into the Intake conversation
```

## Who speaks to the user
**Intake is the sole user-facing conversational identity.** The user describes the mesh
to Intake, answers its clarifications, selects and confirms an engine, and approves the
canonical requirements. Builder and reviewer are internal workflow agents: they never
converse with the user, never change approved intent, and never decide what the user is
told happened.

**Application code determines terminal truth.** When the graph ends, `pipeline_run`
delivers the required artifacts, commits the terminal status, and calls
[`application/final_result.py`](../../src/meshpipeline/application/final_result.py) to build a
typed `FinalResult` from durable facts, the committed job status, `executor_success`, the
winning attempt's reviewer verdict, the failed engine gate, and the actually-ready Artifact
rows. That record is persisted (schema-versioned, migration `0007`) and rendered into the
user's message by `render_message`. **No model is called for the terminal result**, so it
cannot claim a download that is not ready or a review that did not pass.

The application posts the verified final result back into the Intake conversation. Intake
is not invoked again to reword it, and Intake never judges for itself whether execution
succeeded.

## The front door

The **console** (`apps/console`) is what a user actually reaches. It is a Next.js server on its
own Cloud Run service, and it is the only tier deliberately open to the internet: its Auth.js
session is the gate, so putting Cloud Run's IAM in front of it would block the sign-in page it
exists to serve. The API is not public in the same way, and that difference is stated in each
service's configuration rather than inherited.

Two paths leave the browser, and they are not the same path:

- **HTTP goes through the console.** `/api/v1/*` is proxied server-side. The proxy resolves the
  owner from the session and signs it (`X-User-Id` + an HMAC `X-User-Sig`), so the browser never
  carries a credential and cannot assert an identity of its own. This is what replaces the legacy
  page's `localStorage` identity, which any reader could edit.
- **The event stream does not.** The WebSocket is opened by the browser straight at the API, using
  a short-lived single-use ticket minted over authenticated HTTP. Credentials go in headers, never
  in a URL a proxy or Cloud Run would log. The console is therefore not in the streaming path, and
  a console deploy does not interrupt a running job's event feed.

The console opens no database, no Redis and no object store. Everything it knows, it asks the API
for. That is what lets it scale to zero, stay off the VPC, and deploy on its own schedule — and it
is why a console change can never require a migration.

**Accounts are env-backed today.** `CONSOLE_AUTH_USERS` carries email addresses and scrypt password
hashes, delivered as a Secret Manager reference. It is a launch expedient, not an account system:
there is no signup, no reset, no lockout, and no organisation boundary. Replacing it with database
accounts is the next piece of work, and the tenant columns it will scope on
(`Principal.organization_id`, `api_keys.organization_id`) are already carried and deliberately
unused.

## Three tiers (this is what keeps it sane)
1. **Generic spine** (built once): the graph, the builder agent loop, the reviewer's
   render-and-look, the `run_python` jail, artifact upload, intake. ~Half the system.
2. **Engine pack** (per engine, the heavy unit): system prompt + tool set (catalog) +
   runner (`mesh_engine`) + validator (executor gates) + deliverable. A physical
   capability, not a domain, cfMesh, snappyHexMesh, Gmsh, snappy_multiregion and VMTK today.
3. **Purpose overlay** (cheap): the user-declared PURPOSE owns the boundary-role
   vocabulary and the reviewer's use-case axes (`engines/purposes.py`), the workflow,
   not the engine, drives them, so one engine can serve many purposes.

## The brick contract
Every pipeline stage is `async def node_X(state: dict) -> dict`, reads the shared
`PipelineState`, returns a **partial** update (LangGraph merges it). Adding a stage =
write one such function + wire it in `graph.py`. Routing lives only in `route_after_*`.
Where a node lives: `agents/` if it runs a multi-turn tool loop, else `pipeline/`.

## Key seams
- **Engine selection** (`pipeline/engine_select.py`): DETERMINISTIC, no model call -
  the engine is a direct user input captured at intake (named, or proposed and
  confirmed); this node only resolves edge cases and records provenance. The choice
  flips the builder's prompt + tools + runner (all from the engine's catalog row).
- **Admission** (`engines/base.py::EngineSpec.admit`, ONE method, two evidence
  phases): intake validates the DECLARED evidence (capability × purpose × input_kind,
  dimensionality, patches, engine_params) conversationally; the deterministic
  `pipeline/geometry_admission.py` node re-runs the same method with the MEASURED
  surface evidence added (self-intersection, thinness) before the builder starts. Any
  rejection blocks the build, admission rejects physical impossibility only, never
  quality, budget, or "we haven't tested this".
- **Mesh engine** (`engines/runtime.py`): `get_engine(name)` returns the engine's
  adapter (`engines/<name>/adapter.py`); only adapters touch the concrete runners
  (`engines/cfmesh/cfmesh_runner.py`, `engines/snappy/snappy_runner.py`, …). The
  snappy build is code-driven (`engines/snappy/drivers.py`): a planner LLM
  (`engines/snappy/planner.py`) picks the STRATEGY, the code renders/runs/judges -
  the builder cannot loop or fail to submit.
- **Mesh execution** (`contracts/mesh_execution` + `adapters/mesh_execution/*` +
    `runtime/mesh_runner.py`): engines call the neutral `run_mesh()`, and the composition root
    binds it to the Cloud Run client, always. A Cloud Run Job runs per mesh and GCS carries the
    workspace tars. There is no selector and no fallback: a dispatch failure is reported as a
    remote failure, because a silent fallback hid cloud failures and made cost and behaviour
    depend on an invisible toggle.

    One application topology, one mesh boundary:
    - **The application** (`make dev-up`), a LOCAL control plane (UI/API/Celery/Postgres/Redis/
      MinIO/SearXNG) plus the Cloud Run mesh Job and its GCS exchange. Docker Compose is the
      developer control plane, not a fully offline product; meshing never runs on the developer's
      machine and coworkers do not install OpenFOAM/cfMesh/VMTK. The api and pipeline images carry
      no native toolchain, the separate `mesh` image owns it.
    - **Native tests** (`make test-native-*`), the isolated `mesh` image runs the real engines
      in-process to verify that image. The tiers bind the in-process runner themselves; it is not
      reachable from the application's composition root.
- **State versioning** (`contracts/pipeline_state.STATE_SCHEMA_VERSION`): the worker namespaces each job's
  checkpoint by version, so a deploy that changes the state shape can't resume an
  incompatible checkpoint.
- **Failure taxonomy** (`errors.py`) + **resilience** (`adapters/_shared/resilience.py`):
  system failures (provider down, OOM, dep down) are classified, circuit-broken,
  dead-lettered, and turned into blameless "try again" messages, never conflated with a
  DOMAIN rejection (the mesh/request is genuinely the problem).

## The delivery invariant (the thing we never break)
A mesh reaches the user **only** if: the ENGINE's declared executor gates passed
(patch contract + manifest + its declared solvability check) **and** the reviewer
returned PASS through its engine-declared modality (a *real visual walk* for render-
reviewable meshes; a *measured metric report* for meshes whose quality is numeric -
same agent, same verdict protocol, same bar) **and** the engine's declared deliverable
was produced and uploaded. Otherwise the job fails cleanly, nothing shipped. Enforced
at three layers: routing (`route_after_executor`), the worker success gate
(`verdict==PASS AND executor_success`), and the reviewer (no render where one is
required → system failure, never a fabricated verdict).

## Security: the `run_python` jail: and the foam-dict guard
LLM-authored code runs OS-jailed (`sandbox/sandbox_exec.py`): seccomp BPF blocks the
`socket` syscall (no network, no exfiltration, no internal SSRF), Landlock confines the
filesystem to the job workspace (no cross-tenant reads), rlimits + container `pids` bound
resources, the env is secret-scrubbed, and a static AST scan blocks obvious escapes.
Namespaces are unavailable in the container, so these unprivileged kernel controls are
used instead. Fail-closed when `RUN_PYTHON_REQUIRE_SANDBOX=true`.

The LLM-authored **OpenFOAM dicts** get the same treatment (each OpenFOAM engine carries its
own `engines/<engine>/foam_exec.py`): every foam subprocess runs with the
secret-scrubbed env, and `scan_case_dicts()` refuses to parse a case whose `system/`
files embed `#codeStream`/`#calc`/`#system` (OpenFOAM's compile-and-run-at-parse-time
directives, the jail's side door).

## Agent tools (distinct from mesh engines)

Agent tools are LLM-callable functions; they are not engines, and the ownership rule
keeps the two vocabularies apart:

- Engine-owned meshing vocabulary belongs in `engines/<engine>/`, an engine's
  `authoring.py` / `adapter.py` / `<engine>_runner.py` / `gates.py` / `criteria.py` / `pack.py` / `builder_guidance.py`
  and backend drivers never live in the tools layer. Runtime engine adapters are not
  agent tools.
- Shared agent tools (callable by more than one agent) live in `agent_tools/shared/`.
  `web_search` is the canonical example: both intake and the builder call it, so it
  belongs to neither.
- Agent-specific tool registries may live in `agent_tools/<agent>/` if/when extracted
  (the builder's tool layer lives in `agents/builder/tools/`, split by family -
  `workspace.py`, `geometry.py`, `meshing.py`, `research.py`, with `__init__.py` as the
  assembly point; `configure_mesh` is exposed by the meshing family as an agent tool, but its
  schema/validation/rendering come from `EngineSpec` / `engines/<name>/authoring.py`).

## Layers
The folder tree mirrors the architecture, a folder name answers "what concept owns this file?"

The product is ONE installable distribution, `src/meshpipeline/`, the `meshpipeline` wheel -
from which several runtime images are built. The dependency rule below is enforced by
`tests/unit/hygiene/`, not merely documented.

One deliverable does not come from that wheel: the **console** is a Next.js application built from
its own `Dockerfile` target, on Node, sharing no layer with the Python images. It is a third
release component beside `app` and `mesh`, promoted by digest like the other two.

### The seam (why the tree looks like this)

| dir | role |
|---|---|
| `contracts/` | the PORTS the product depends on: neutral protocols + types, importing nothing above themselves: `event_stream`, `mesh_execution`, `model_inference`, `object_storage`, `pipeline_execution`, `search`, `delivery_guard`, `rate_limit`, `dead_letter`, `mesh_timing`, `training_export`, `pipeline_state`, `review_outcome` |
| `adapters/` | the IMPLEMENTATIONS, one directory per capability: `event_stream/redis`, `mesh_execution/{local,cloud_run_client,gcs_exchange}`, `model_inference/{deepseek,deepinfra,router,messages}`, `object_storage/{minio,factory}`, `pipeline_execution/{celery,celery_app,deferred,maintenance_tasks}`, `search/{searxng,tavily,factory}`, plus `delivery_guard/`, `rate_limit/`, `dead_letter/`, `mesh_timing/` and `_shared/{redis_client,resilience}` |
| `runtime/` | the ONLY composition root: `composition` (settings → concrete adapter) + the thin process entrypoints `api_server`, `celery_worker`, `mesh_runner`, `run_job`, `migrate`, `metrics_server`, `startup`, `logging`, `observability/` |

Product code above the contracts never imports an adapter and never selects a provider;
`runtime/composition.install_adapters()` binds every port once per process.

### The product

| dir | role |
|---|---|
| `agents/` | multi-turn LLM agents, one package each: `intake/`, `builder/`, `reviewer/` (each owns its `settings.py`) |
| `engines/` | one bundle per engine (`cfmesh`, `snappy`, `snappy_multiregion`, `gmsh`, `vmtk`: all implemented; the two-state contract forbids planned/experimental rows), each owning its `spec`, `authoring`, `criteria`, gates, runner, `pack`, `viewer` and `solvability`; the shared framework (`base`, `registry`, `purposes`, `admission`, `quality_criteria`, `dispatch`, `manifest`, `mesh_history`) sits beside them |
| `pipeline/` | single-pass orchestration stages: `graph` (the composition root for nodes), `engine_select`, `geometry_admission`, `executor`, `classifier`, `outcome`, `state_factory`, `data_contract`, `enums` |
| `application/` | neutral use cases: `pipeline_run` (dispatch + the run body), `job_service`, `artifact_uploader`, `maintenance/{cleanup,export}` |
| `api/` | HTTP only, adapter-neutral: `app` (assembly), `security`, `v1/{chat,simulation,upload,ws,client_config,router}`, `middleware/hardening`, `schemas/` |
| `cad/` · `render/` · `sandbox/` | geometry (tessellation, analysis, stl_io, staging, surface checks) · viewer/review artifacts + `review_palette.py` (renderer-owned review presentation) · the LLM code jail (`safe_exec`, `sandbox`, `sandbox_exec`) |
| `capture/` | captured operations and the corpus sample built from them, gated by `DATA_COLLECTION_ENABLED` (default on). Imports none of what it observes |
| `events/` | the closed UI-event vocabulary + channel/key naming: a contract, not a transport |
| `persistence/` · `settings/` · `prompts/` · `agent_tools/` | data access (`models`, `session`, `repositories/`) · owned settings (`env`, `providers`, `runtime`, `policy`) · packaged prompt templates · shared agent tools (`web_search`) |
| `errors.py` · `metrics.py` | failure taxonomy (classification → blameless message → dead-letter) · Prometheus metrics |

### Review presentation is renderer-owned

The mesh manifest carries **engineering semantics only**: patch identifiers and names,
boundary roles, region identities, interface relationships, geometry bounds, mesh
statistics, artifact metadata. It carries no presentation.

Everything the reviewer's evidence looks like is decided by `render/review_palette.py` and
the render backend: categorical patch colours, the reserved defect and selection colours,
contextual (far-field) treatment, background, edge visibility and width, opacity, lighting,
and per-patch camera framing derived from the geometry the renderer actually loaded.

The reason is independence. The reviewer judges a mesh, and the component that built that
mesh must not be able to choose the conditions the judgement happens under, a patch the
colour of the background, two patches the same colour, or a camera pointed away from a
flaw. Colour assignment is a stable hash of the patch identifier, so it is reproducible
across runs and processes and does not shift when patches are added or reordered; every
categorical colour is measured against the review background.

Builder cannot write `mesh_manifest.json`: it is produced by the engine's finalize path
(`engines/manifest.write_manifest`), and no engine prompt requests styling. A manifest that
prescribes review presentation is rejected as not a current manifest.


### Outside the package

| dir | role |
|---|---|
| `apps/` | the Next.js applications, a pnpm workspace: `console/` (the browser front door, its own image and Cloud Run service) and `admin-console/` (a stub, not yet deployed). NOT part of the wheel |
| `packages/` | code shared between those apps: `hexera-api-client/`, the typed product-API client |
| `ui/` | the LEGACY frontend, served by the API at `/ui`. Superseded by `apps/console`, kept as the fallback until the console's React rewrite lands |
| `tests/` | `unit/` (fast, hermetic, heavy deps stubbed) · `integration/` (real deps, no stubs) · `unit/hygiene/` (the executable architecture rules) · `unit/platform/` (provider contracts, both backends) |
| `deploy/` · `alembic/` | image + hosted-deploy documentation and templates (never imported by the package) · database migrations |
| `devtools/` | developer tools, one directory per capability: `env/` (setup, doctor) · `quality/` (the CI-run repository gates) · `release/` (Gate C) |
| `tests/fixtures/external/` | licensed per-machine validation CAD (gitignored). Production code must not depend on it |

**Tests** live under one `tests/` parent split by tier: `tests/unit/`, the fast, hermetic suite (heavy deps stubbed in `tests/unit/conftest.py`), mirroring the app as `tests/unit/{engines,flow,pipeline,api,training,security,worker,infra}/` and `tests/unit/agents/{intake,builder,reviewer}/`; and `tests/integration/`, the live in-container runtime suite (real deps, no stubs, its own conftest). A bare `pytest` runs only `tests/unit` (`testpaths`); integration is opt-in (`make test-integration`).

## Policy versions

Two version fields are fingerprinted into an approved job because the behavior they name is
re-derived AFTER approval and could otherwise change silently between approval and execution. Each
pins that behavior so a change is detected (the recomputed intent stops matching and the run is
refused), not silently re-interpreted.

## fidelity_policy_version

Owner: `pipeline.enums.FIDELITY_POLICY_VERSION` (currently `3tier-v1`).

The mesh-detail tiers (draft, standard, max) become engine-owned soft authoring recommendations at
build time (`EngineSpec.recommend_authoring`), which runs after approval. The version pins the
default tier and the tier meaning into the approved-intent fingerprint.

- Increment when the default tier changes or the qualitative meaning of a tier changes.
- Do not increment when an engine tunes its own numeric recommendation within the same tier meaning.
- An older or unknown version on an approved job is refused at reconstruction (fail closed).

## artifact_policy_version

Owner: `application.artifact_policy.ARTIFACT_POLICY_VERSION` (currently `artifacts-v2`).

Required output classes are derived from the engine and re-verified at reconstruction and terminal
readiness. The version pins what terminal success requires.

- Increment when the required/optional classification changes.
- Do not increment when adding a new optional export.
- An older or unknown version is refused via fingerprint mismatch.

Tests: `tests/unit/application/test_policy_versions.py`,
`tests/unit/application/test_approved_intent_binding.py`.

## The local services

| Service | Role | Notes |
|---|---|---|
| **api** | FastAPI: HTTP, WebSocket, and the legacy `/ui` page | applies migrations on start |
| **worker** | Celery worker running the pipeline | the LangGraph graph executes here |
| **worker-utility** | maintenance work (retention, reconciliation) | separate so a long mesh never blocks it |
| **beat** | scheduled maintenance triggers | |
| **postgres** | durable job, session, artifact and capture state | also the LangGraph checkpointer |
| **redis** | typed event stream, backlog replay, Celery broker | |
| **minio** | object storage for uploads and delivered bundles | S3-compatible; bucket names must be lowercase |
| **searxng** | the web-search backend for the research tool | optional; `WEB_SEARCH_ENABLED=false` disables it |

The console is **not** in that stack. It is a Node application in the pnpm workspace, run on its
own with `pnpm dev:console`, and it talks to whichever API `HEXERA_API_BASE_URL` names — the local
one, or a deployed environment. Nothing in Compose starts it, because nothing in Compose needs it:
the API still serves `/ui`.

### Networking: and why the network has no fixed name

Compose puts every service on one private bridge network that it names after the project:
`<project>_default`, where the project is this directory's name unless `COMPOSE_PROJECT_NAME` says
otherwise. Service DNS is scoped to that network, so `postgres` resolves to *this* project's
database and nothing else.

That scoping is the point. The network deliberately carries no fixed global name, because two
Compose projects that both name their network the same thing are placed on **one** network: their
service names then collide, and a container can reach, and write to, another project's database
while every log line still reads `postgres`. If you run more than one checkout, or a throwaway
stack beside your real one, each gets its own network with no configuration.

If your stack predates this, you may still have a leftover network called `mesh-network`. Nothing
removes it for you. Once your stack has been restarted (`make dev-down && make dev-up`) it is on
the project network, and you can check what is left behind before removing it:

```bash
docker network inspect mesh-network -f '{{range .Containers}}{{.Name}} {{end}}'   # what is still on it
docker network rm mesh-network                                                    # only once that is empty
```

`docker network rm` refuses while any container is still attached, so it cannot take a running
stack down with it.

### The database baseline

The history begins at one baseline revision, `0001_schema_baseline`, which creates the entire
pre-release schema directly: every table, index, constraint, PostgreSQL enum type and the
`chat_sessions` updated-at trigger. Revisions after it are ordinary additive migrations, each
reversing exactly what it created — `0002_api_keys` creates the `api_keys` table. The chain is
linear: one base, one head, no branch and no merge point. It is applied automatically when the
stack starts.

This is a pre-release baseline and it supports **fresh databases only**. No deployment exists whose
data has to survive, so the development-era revisions were deleted rather than superseded and no
upgrade path from them was kept. A database stamped with a revision this build does not ship is
**refused** with an actionable message rather than migrated or restamped. Recreate it against an
empty database.

An existing database holding Hexera-shaped objects but no Alembic history is also refused, before
any DDL runs, rather than adopted.

### Object storage

Uploaded geometry is stored by content: the object key is derived from the file's SHA-256, so the
same bytes uploaded twice resolve to one object. Delivered bundles are stored per job.

This is MinIO, with the credentials in `.env`, reached over plain HTTP on the local stack.

### Redis

Redis carries the typed event stream the browser subscribes to, the backlog that lets a page
reload replay what it missed, and the Celery broker. Event retention is bounded by
`EVENT_LOG_TTL_SECONDS`.

### Search provider

The research tool retrieves through SearXNG and distils results with the summarizer model. It is
enabled by default; `WEB_SEARCH_ENABLED=false` turns the capability off without breaking a run.
