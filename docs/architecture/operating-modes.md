# Operating modes

The same code runs in several configurations. This document compares them by consequence, what
changes about behaviour, durability, cost and exposure, rather than by listing values. Exact keys
and defaults are in [configuration.md](../reference/configuration.md).

## Local versus self-hosted

**This repository automates one deployment: the mesh job.** `make mesh-deploy` provisions a private
Cloud Run **Job**, its exchange bucket, a service account and IAM, and nothing else. There is no
tooling here that hosts the API or the worker, and the only Cloud Run manifest is `mesh-job.yaml`.

Running the application somewhere other than a developer's machine is therefore a matter of
configuration, and what the settings let you point at is this:

|  | Local (`make dev-up`) | Self-hosted |
|---|---|---|
| API and worker | Compose services on one machine | the same images, run by whatever you run containers with |
| Database | the `postgres` container | any reachable PostgreSQL |
| Object storage | the `minio` container | any S3-compatible service: the adapter is S3, not GCS |
| Events | the `redis` container | any reachable Redis |
| Identity | self-asserted unless `USER_TOKEN_SECRET` is set | signed, once that secret is set |
| Secrets | `.env` on disk, gitignored | supplied as environment, however your platform does that |

The same images and the same migration baseline serve both. The difference an operator feels most is
enforcement: `ENV=production` refuses to start with a wildcard CORS origin, a missing API key or an
unset database password, and refuses to migrate a database whose host looks like a local stack.

Google Cloud Storage appears in exactly one place, the bucket the worker and the mesh job exchange
workspaces through. It is not an option for the application's own object store.

### What the deployment exposes

Enforcement extends to what is reachable. The interactive API documentation, the OpenAPI schema and
the Prometheus scrape endpoint are **development conveniences**: they are how the API is worked on,
and a hardened deployment does not register them at all. Asking for one there returns an ordinary
404, indistinguishable from any other unknown path, deliberately, because an authentication
challenge would confirm the route exists.

`/health` and `/readyz` are served in every environment. An orchestrator needs them, and they say
nothing but whether the process is alive and ready.

This is a property of the application, not of what sits in front of it. Putting the deployment
behind a proxy or an identity layer is a reasonable thing to do for its own reasons, and changes
none of the above.

Metrics keep being *recorded* in a hardened deployment; only the public scrape URL is absent. Read
them from the process if you collect them.

One request limit covers the HTTP API, per identity per minute. Minting a WebSocket ticket is
ordinary API traffic and is counted like any other request: it signs a ticket and reads the
database. The stream it opens is not: a connection that is not an HTTP request never reaches the
limiter, so a long session is never throttled part-way through. See
[configuration.md](../reference/configuration.md#request-limits-at-the-edge).

## Mesh execution

Every mesh the application runs is dispatched to the Cloud Run mesh job. There is no setting that
moves it, and no fallback: the machine serving the UI never meshes.

- The workspace is exchanged through a GCS bucket, so no filesystem is shared between the caller
  and the mesher.
- The toolchain image is built and maintained once, not on every developer's machine.
- Requires `GCP_PROJECT_ID`, `CLOUDRUN_JOB` and `GCP_MESH_BUCKET`. `make dev-up` and
  `make dev-doctor` refuse to start without them, so the stack never appears healthy while unable
  to dispatch a mesh.
- Failure modes are remote: a job that cannot start, a workspace that cannot be fetched, or a
  mesher that exceeded its wall clock. Each is reported as itself.

The `mesh` image carries OpenFOAM, cfMesh, Gmsh and VMTK, and the native test tiers
(`make test-native-*`) run the engines in-process inside it to verify that image. Those tiers bind
the in-process runner themselves; nothing in the application can reach it.


## Pipeline execution

The pipeline graph runs in a Celery worker by default. Dispatch is a declared seam
(`contracts/pipeline_execution.py`), so how a run is launched is a composition decision rather
than something the pipeline knows about.

- **Celery (local and hosted)**: the API enqueues; a worker executes. The API returns as soon as
  the job is durable, and the browser follows the event stream.
- **Deferred**: a run can be launched later against the same durable record. Everything the
  pipeline needs is in the database and the object store, so a run does not depend on the process
  that accepted it still existing.

A worker that loses ownership mid-run is fenced: it stops before any terminal side effect rather
than racing the generation that superseded it. See [architecture/overview.md](overview.md#key-seams).

## Optional services

| Service | Off means | Setting |
|---|---|---|
| Web search | the research tool is unavailable; runs proceed without it | `WEB_SEARCH_ENABLED=false` |
| OpenTelemetry | no distributed traces; logs are unaffected | `OTEL_TRACES_ENABLED` unset |
| Langfuse | no model-call tracing to Langfuse | leave the three keys blank |
| Sentry | no error reporting | leave `SENTRY_DSN` unset |

None of these is required for a mesh to be produced. Turning one off removes a capability; it does
not degrade mesh quality, and no run silently substitutes something else.

## Data collection

Two independent switches are often confused, so they are stated together here.

- **`DATA_COLLECTION_ENABLED`** is a **retention** policy: whether finished runs are exported to
  the corpus for training and evaluation.
- **`PUBLIC_TRACE_MODE`** is a **publication** policy: what the live run page may display.

Neither affects the other.

### Collection disabled

- No `capture_operations` rows, no corpus directory, no startup write probe, no export task, and no
  intake training metadata on the session.
- Everything a run needs is still written: the job and its final result, artifacts, the chat
  session and its messages, the approval snapshot, and the viewer preview under `DATA_ROOT/jobs`.
  Chat, page reload, approval and dispatch behave identically.

### Collection enabled (default)

- A run's captured operations are stored in `capture_operations`, scoped to the owning tenant; a
  record that cannot name its tenant is dropped rather than filed under a default owner.
- The finished run is exported to `CORPUS_DIR/<job-id>/`: the event projection, per-agent episodes,
  the workspace files each attempt saw, the delivered mesh bundle and the reviewer's renders.
- Environment secrets and key-shaped strings are redacted on the way in. Payloads are stored whole -
  full conversation turns, prompts and model responses included. Treat the corpus as sensitive.
- Capture is fail-open: a capture failure logs a warning and never fails a mesh job. A run's
  success must not depend on whether we managed to record it. The cost of that is real, so it is
  worth saying plainly: a run can succeed while retaining nothing, and the only sign is the
  warning. `capture: no tenant scope bound` means records were written before the claim that
  names the tenant they belong to, and were dropped rather than filed under a guess.
- **The corpus is operator-managed.** Nothing expires it. Delete samples you no longer want.

Visible messages and the prompts actually sent to models are captured. Provider-internal reasoning
is not. Nothing leaves this machine either way: the corpus is local files and a local database.

### What the live page shows

| `PUBLIC_TRACE_MODE` | The page shows |
|---|---|
| `safe` | reasoning and tool activity, without content |
| `raw` (default) | additionally: provider reasoning, real tool names, arguments, results, and the reviewer's inspection images |

In `raw`, reasoning appears **while the round is still thinking**, not only once it ends: the
builder, the planner and the reviewer stream, so their card fills in as the text arrives. Intake
does not stream, so its reasoning is whatever the round reports at its close. The plan is
published in the builder's lane rather than one of its own, because it is a round of the run the
builder is doing and not a separate agent. A round is never failed or delayed by this: if the
trace cannot publish, the round continues and only the live text is lost, which is why a missing
card is a trace fault and never a meshing one.

`raw` requires `ALLOW_PUBLIC_RAW_TRACE=true` as a second, deliberate acknowledgement. Secrets and
model or provider identity are redacted in both modes.

## Hosted resource modes

`make mesh-deploy` works against either a blank project or one that already holds a deployment.

- **Fresh resources**: the scripts create the mesh job, its exchange bucket, the registry
  repository, the service account and the IAM bindings, under the names your `.env` declares.
  Nothing is inherited from another organisation, because no identifier is baked into the code.
- **Existing resources**: each resource is judged on its own. One this deployment created is
  updated in place; one you supplied is validated and **left exactly as it is**, never
  reconfigured. Reruns are idempotent and duplicate nothing.

`make mesh-doctor` reports which state a project is in, and the exact next command, without
printing any value. Details: [deployment/overview.md](../deployment/overview.md).
