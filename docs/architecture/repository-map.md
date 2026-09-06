# Repository map

Which area owns what, and which way dependencies point. This is an orientation document, not an
inventory: individual modules carry their own architectural headers.

## Top level

| Path | Owns |
|---|---|
| `src/meshpipeline/` | the product |
| `tests/` | the test suite, in tiers (`unit`, `integration`, `native`, `ui`) |
| `devtools/` | tools that create, inspect or validate this software, never anything a user's job depends on |
| `deploy/` | deployment automation and container entrypoints |
| `alembic/` | the migration authority: a linear chain from `0001_schema_baseline` |
| `ui/` | the browser client shipped by the API |
| `docs/` | this manual |
| `requirements/` | the pinned dependency sets |

## Inside `src/meshpipeline/`

Dependencies point **inward**: adapters depend on contracts, never the reverse. The rule that
keeps this true is that a contract declares a Protocol and a process-wide binding, and the
composition root is the only place a concrete adapter is attached to it.

| Package | Owns | Depends on |
|---|---|---|
| `contracts/` | the seams: Protocols, typed values and the vocabulary shared across layers | nothing above it |
| `runtime/` | process entry points (`api_server`, `celery_worker`, `run_job`, `mesh_runner`, `migrate`) and `composition.py`, the one place ports are bound to adapters | everything |
| `api/` | the public HTTP and WebSocket boundary: `app.py`, the `v1/` routers, security and middleware | application, contracts |
| `application/` | use cases that coordinate persistence, storage and dispatch for a job | contracts, persistence |
| `pipeline/` | the LangGraph graph, its state and the data contract that governs it | agents, engines, contracts |
| `agents/` | the model-driven roles, and the shared loop they all run through | contracts, engines |
| `agent_tools/` | tools more than one agent uses; an agent's own tools stay in its package | contracts |
| `prompts/` | the shipped prompt text, one directory per role | nothing |
| `engines/` | the engine contract, the registry, and one directory per engine | contracts |
| `sandbox/` | rendering and the confinement fence every artifact passes | contracts |
| `cad/` | geometry inspection, unit evidence and staging | contracts |
| `persistence/` | the ORM models, repositories and the durable lease | contracts |
| `adapters/` | concrete implementations of the contracts (Redis, object stores, providers, execution backends) | contracts |
| `capture/` | training and diagnostic capture | contracts |
| `events/` | the closed public event vocabulary | nothing |
| `settings/` | configuration declarations and the inventory that generates `.env.example` | nothing |
| `trace/` | span-shaped tracing and its sanitizer | contracts |
| `render/` | viewer payloads and mesh facts | contracts |

## Extension points

| To add | Extend | Without touching |
|---|---|---|
| a meshing engine | a directory under `engines/` with a `spec.py` | any shared code: the registry discovers it |
| an object store, event stream, model provider or execution backend | an adapter implementing the contract, bound in `composition.py` | any caller |
| an agent capability | a tool in that agent's tool package | the shared loop |
| a public event | the vocabulary in `events/` and the data contract | the transport |

## Runtime roots

Five processes start the product. Everything else is reached from one of them:

| Entry point | Runs |
|---|---|
| `runtime/api_server.py` | the FastAPI application |
| `runtime/celery_worker.py` | the pipeline worker |
| `runtime/run_job.py` | one pipeline run, directly |
| `runtime/mesh_runner.py` | the mesher, in the mesh image |
| `runtime/migrate.py` | the advisory-locked migration, on container start |

`runtime/composition.py` is what makes these processes differ: the same contracts are bound to
different adapters depending on how the deployment is configured.

## Boundaries worth knowing

- **The API never imports a mesher.** It reads the engine catalog to show a menu; importing gmsh,
  VTK or OpenFOAM to do that would make the API process heavy and fragile. `engines/runtime.py` is
  the single place a bundle is imported by name.
- **The pipeline never touches a shared filesystem.** Everything a run needs is in the database
  and the object store, which is what lets the API and the worker run on different machines.
- **Nothing under `devtools/` is imported by `src/meshpipeline/`**: shipped in the wheel, or
  present in a runtime image.
