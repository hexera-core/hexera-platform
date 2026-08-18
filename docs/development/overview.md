# Development

Working on the code: environment, test tiers, and the automated gates. (Deployment:
[deployment.md](../deployment/overview.md). Engine extension: [engines.md](../engines/overview.md). Design:
[architecture.md](../architecture/overview.md).)

## Repository map

`src/meshpipeline/` is the product; everything else supports it.

| Path | What lives here |
|---|---|
| `runtime/` | process entry points (`api_server`, `celery_worker`, `run_job`, `mesh_runner`, `migrate`) + `composition.py`, the one place ports are bound to adapters |
| `api/` | the public HTTP/WebSocket boundary (FastAPI `app.py` → `v1/` routers, security, middleware) |
| `application/` | orchestration and terminal truth (`pipeline_run`, `terminal_finalize`, `outbox_publisher`, `final_result`, fencing, artifact policy) |
| `pipeline/` | the LangGraph state machine (`graph.py` + the node functions) |
| `agents/` | the LLM roles: `intake/`, `builder/`, `reviewer/` |
| `engines/` | engine-owned mesh behavior, one folder per engine + the shared contracts (`base.py`, `registry.py`, `dispatch.py`) |
| `contracts/` | ports (interfaces): the seams the app depends on |
| `adapters/` | the implementations behind those ports (model inference, pipeline/mesh execution, object storage, event stream, search) |
| `persistence/` | durable state (SQLAlchemy models, repositories, lease, migration URL) |
| `sandbox/`, `cad/`, `capture/`, `render/`, `settings/` | supporting: LLM code jail + offscreen render, CAD/tessellation, trace capture, viewer packs, config |
| `deploy/` | hosted deployment (`gcp/` scripts + Cloud Run manifests) |
| `tests/` | `unit/` (hermetic) · `integration/` (real PG/Redis/MinIO) · `native/` (mesh image) · `ui/` |

## Reading order (new contributors)

1. [README.md](../../README.md), what it does.
2. [architecture.md](../architecture/overview.md), the design and the seams.
3. `contracts/`, the ports first (`pipeline_execution`, `mesh_execution`, `event_stream`, `object_storage`).
4. `runtime/composition.py`, how ports get concrete adapters.
5. `pipeline/graph.py`, the agent state machine at a glance.
6. one engine end-to-end (e.g. `engines/gmsh/`) + [engines.md](../engines/overview.md).
7. `application/terminal_finalize.py`, the terminal-truth invariant.
8. `application/pipeline_run.py` **last**, the orchestrator (large; read it once the rest is in view).

## Source commentary

Comments and docstrings are read by whoever maintains the file next, without the history that
produced it. They must therefore be **timeless**: a statement about how the system behaves now.

A comment earns its place by explaining something the code cannot say for itself, why it exists, a
non-obvious invariant, who owns a decision, units or coordinate expectations, a trust boundary,
replay or idempotency semantics, an external constraint, or a public contract. A comment that
narrates the line beneath it should be deleted rather than reworded.

Never prefix an explanation with a campaign, checkpoint, phase or audit-row label, `S6:`,
`Phase 2:`, `B-4:`, `[CP3]`. They record when the comment was written, which no maintainer can act
on and which cannot be understood without material that is not in the repository. Write the
sentence instead:

```python
# S6: force ASCII polyMesh so the solvability gate can parse owner/neighbour   ← no
# ASCII polyMesh so the solvability gate can parse owner/neighbour             ← yes
```

Technical identifiers are not provenance and stay: dependency and schema versions, migration
revisions, image digests, RFC/CVE numbers, units, engine names, lint codes and tool directives.
Formal audit documents keep their row identifiers, because there the row *is* the structure.

This is a convention for authors, not a gate: no test enforces comment or header formatting.

## The six "-er" roles (do not confuse them)

| Term | Is | Lives in |
|---|---|---|
| **engine adapter** | the thin `ENGINE` (MeshEngine seam) loaded by `engines.runtime.get_engine` | `engines/<e>/adapter.py` |
| **native engine runner** | the actual mesher (subprocess/native calls) | `engines/<e>/<engine>_runner.py` |
| **model tool** | a tool the LLM may call during an agent turn (write_file, run_mesh, …) | `agents/*/tools.py`, `agent_tools/` |
| **developer tool** | a script a human runs (setup, doctor, release gates), never in a runtime path | `devtools/` |
| **renderer** | offscreen mesh→image backend (PyVista/EGL) | `sandbox/render_adapter.py`, `sandbox/backend.py` |
| **review renderer** | an engine's declaration of what the visual reviewer renders/inspects | `engines/<e>/review_renderer.py` |

## Environment

One command prepares a fresh clone; it is idempotent and safe to rerun:

```bash
make setup              # host tools, .venv, dependencies, local dirs, images; creates .env if absent
$EDITOR .env            # set DEEPSEEK_API_KEY + DEEPINFRA_API_KEY, and the Cloud Run mesh config
make dev-doctor         # preflight the hybrid config (no services started)
make dev-up             # start the hybrid dev stack (local control plane + Cloud Run mesh)
```

## Hybrid development (the supported developer workflow)

Development is **hybrid**: a LOCAL control plane plus CLOUD native mesh compute.

```
local browser UI → local FastAPI API → local Celery pipeline
    → local PostgreSQL / Redis / MinIO / SearXNG
    → existing Cloud Run native mesh Job
    → GCS mesh workspace exchange → results back into the local stack
```

Docker Compose is the developer **control plane**: it is not a fully offline product, and native
meshing does not run on your machine. You do **not** install OpenFOAM, cfMesh, or VMTK to run the
application: the API and pipeline images deliberately carry no native toolchain, and meshing is
dispatched off-box to the existing Cloud Run mesh Job.

Configure the mesh Job and exchange bucket in `.env` (`GCP_PROJECT_ID`, `GCP_REGION`, `CLOUDRUN_JOB`,
`GCP_MESH_BUCKET`, plus Application Default Credentials or `GOOGLE_APPLICATION_CREDENTIALS`). Then:

```bash
make dev-doctor   # validates host tools, keys, local infra, and the mesh
                  # config, WITHOUT starting anything; prints corrective messages; never prints a
                  # secret. When gcloud is absent it checks config presence and says so plainly.
make dev-up       # starts the local stack; FAILS before start if the cloud-mesh config is missing,
                  # so the stack never looks healthy while unable to dispatch a mesh. Prints the
                  # cloud mesh Job + project. Never builds or needs the mesh image.
make dev-down     # stop the local stack (keeps volumes; never touches cloud or another checkout)
make dev-reset    # destructive: also delete the LOCAL data volumes (never any cloud resource)
```

### Native local execution is a specialist TEST mode: not the application

The separate `mesh` image and `make test-native-smoke` / `test-native-all` / `test-native-terminal`
run the real engines locally for engine verification, deterministic testing, and CI. They are **not**
the normal developer workflow, not the coworker deployment path, and not a second production
architecture. They are the only targets that build the mesh image; `make dev-up` never does, and it
refuses to start without the Cloud Run mesh configuration. The tiers bind the in-process runner
themselves; the application's composition root cannot.

`make setup` (devtools/env/setup.sh) detects the host tools, builds `.venv` from the pinned
files, installs the package editable, prepares the bind-mount dirs, validates Docker and
`.env`, and builds the images. The host-run targets (`make check`, `make test`, `make
lint`, `make typecheck`, `make wheel`) use `.venv` automatically, no `source
.venv/bin/activate`. `make help` lists the common commands; `make help-all` shows the
advanced/internal ones.

`make setup` is the only supported way to build the environment. It is safe to re-run.

### Dependency model

One home, hand-edited, no lock/compile step:

| File | Owns | Installed by |
|---|---|---|
| `requirements/runtime.txt` | the canonical runtime + test pins (fastapi, langgraph, vtk, pytest, …) | the Docker base image, CI, `make setup` |
| `requirements/dev.txt` | the canonical dev/CI toolchain pins (ruff, mypy, build) | CI, `make setup` |
| `pyproject.toml` | package metadata only: **no runtime dependencies** | - |

Both requirement files are **hand-edited and canonical**; nothing in the repository is a
generated lock. `pyproject.toml` deliberately declares no dependencies (the wheel installs
`--no-deps` into an environment built from `requirements/runtime.txt`), so the two can
never contradict. `make dependencies` validates this single-source-of-truth rule
(`devtools/quality/check_dependency_drift.py`): no inline pins in the Dockerfile / CI / Makefile /
setup script, no package pinned in both files, no runtime pins in pyproject. Bump a version
by editing the requirement file directly, then rerun `make setup`.

## Test tiers

Each tier runs as its **own pytest session**. They must never share a process, the fast
tier installs framework stubs (`langgraph`, `celery`) into `sys.modules`, and those are
process-global. If the integration tier were collected in the same process it would
import the stub instead of the real package and skip silently, a false "green". So
`tests/integration/conftest.py` **fails loudly** if it detects a stub in the process, and
the `make` targets always invoke each tier separately.

There is deliberately **no shared `tests/conftest.py`**: `tests/unit/conftest.py` owns
the stubs and the hermetic composition; `tests/integration/conftest.py` owns nothing but
the contamination guard.

| tier | command | dependencies | real framework packages? | permitted skips | "green" means |
|---|---|---|---|---|---|
| **fast** | `make test-fast` | requirements only, package installed | **No**: langgraph/celery stubbed; DB/Redis/object-store faked by the unit conftest | an SDK genuinely absent from the env (e.g. `gmsh`, `pyvista` on a thin venv) skips with a named `could not import …`: it RUNS where the SDK is installed (CI) | every hermetic test passes; the only skips are named missing SDKs |
| **integration** | `make test-integration` | a running compose stack (`make dev-up`); after changing source, `make rebuild` so the RUNNING containers carry the stamp the tier checks | **Yes**: real langgraph/pyvista/celery + real Postgres + Redis + MinIO, inside the worker container | a missing service is a hard **failure**. What may skip is what the worker image or the machine does not carry: licensed CAD, the repository files the image does not ship (`.env.example`, the publication manifest), a Docker socket the container has no access to, and MinIO identities nobody provisioned (`MINIO_READONLY_KEY`, `MINIO_NODELETE_KEY`). Every skip names its reason under `-rs` | real graph + surface metrics + real-PG/Redis/MinIO tests pass; **stub contamination and a missing required service are hard failures, never a skip** |
| **container-smoke** | `make test-container-smoke` | Docker (builds the `pipeline` image); **no** Postgres/Redis/MinIO, run with `--network none` | **Yes**: the image ships them | the one licensed-CAD skip | the installed distribution imports + reports the version `src/meshpipeline/__init__.py` declares + ships exactly the prompts `REQUIRED_PROMPTS` declares and no closer asset + celery entrypoint imports, and the `hermetic`-marked integration subset (graph wiring, surface metrics, no service) passes. Makes **no** integration-coverage claim |
| **container-integration** (alias: **container**) | `make test-container-integration` | Docker builds the `pipeline` image, then provisions + health-checks real Postgres + Redis + MinIO on a throwaway network (`tests/integration/run_in_container.sh`) | **Yes**: the image ships them | the same skips the integration tier permits; a missing service is a hard **failure** | the FULL integration tier passes against the image's real deps and three real services; the stack is torn down afterwards. This is the local equivalent of CI's BLOCKING integration job |
| **external-fixtures** | `make test-external-fixtures` | licensed CAD under `tests/fixtures/external/` (gitignored; per machine) | as marked | fixture absence is **reported** (`-rs` shows every skip + reason), never counted as ordinary coverage | licensed-CAD tests pass where the CAD is present; absence is said plainly |
| **ui** | `make test-ui` | a **real headless Chrome** (`CHROME_BIN`, or Chrome/Chromium on PATH) + the ASGI app on a loopback port; no other service | **Yes**: the browser is the runtime under test | **none: a missing browser FAILS the gate**, with installation instructions. Skipping would let a release pass with its browser validation absent | the live page BOOTS: every module parses, every asset it asks for is served, nothing throws, the dialog opens and closes from the keyboard, credentials stay out of the socket URL, the mesh viewer loads its vendored bundle and explains a surface it cannot fetch, and delivered text cannot become markup in the DOM |
| **all** | `make test-all` | the union, as separate sessions | - | - | every tier green in its own session |

### Where the tiers run when the host cannot run them

The table above assumes `make setup` prepared a `.venv` on a **cp311** interpreter, `make check`
and `make test` pick it up automatically. On a host with a different Python, or none, the honest
answer is not "environmental failure": it is that the tier was never given a place to run. Thirteen
modules under `tests/unit/dev`, `tests/unit/deploy`, `tests/unit/hygiene` and `tests/unit/settings`
read tracked repository files (`Makefile`, `Dockerfile`, `docker-compose.yml`, `devtools/`,
`docs/`, `.env.example`), and the pipeline image does not ship them, so running the unit tier
inside the worker container produced 13 collection errors and executed none of them.

`make validate TIER=<tier>` (`devtools/validation/run.sh`) closes that gap. It runs the tests in the
**`validation` image target**, the `pipeline` image plus `requirements/dev.txt`, `make`, `git`, the
Docker CLI and a real Chrome.

The checkout is mounted **read-only** at `/src` and then **copied** into a task workspace the run
owns, where it builds its own virtualenv from the image's interpreter and installs the copied
package into it. Two consequences worth knowing:

- **Your host `.venv` is never used, and never touched.** The supported virtualenv directories
  (`.venv`, `venv`, `env`) are excluded from the copy, so a host environment built on a different
  Python cannot leak into the run. The tier is analysed on the interpreter the image ships.
- **Your working tree is what runs**: tracked and untracked changes alike, `.git` included. It is
  not an export of the last commit, so you can validate what you are actually editing.

Each tier is a separate, copy-pasteable command:

```bash
make validate TIER=collect
make validate TIER=unit
make validate TIER=ui
make validate TIER=integration
make validate TIER=native
make validate TIER=release
```

`collect`, `unit` and `ui` run inside the validation image; `unit` additionally needs the Docker
socket. `integration`, `native` and `release` drive Docker from the host. **None of them needs
cloud access**, `native` runs the engines in-process inside the mesh image to verify that image,
which is *not* the path a user job takes.

| `TIER=` | what runs | why it is separate |

|---|---|---|
| `collect` | `--collect-only` over every tier | one collection error fails it; import health is a contract of its own |
| `unit` | `tests/unit`, with the Docker socket | `tests/unit/deploy` and `tests/unit/dev` shell out to `docker compose config` and `docker info`; **without the socket they skip**, and a skipped compose-parity contract reads exactly like a proven one |
| `ui` | `tests/ui` in the image's Chrome | the browser is the runtime under test; the image carries one so the gate cannot be skipped for its absence |
| `integration` / `native` / `release` | the existing host runners, unchanged | these provision databases, buckets and images on the host daemon, so they keep their own authority rather than re-entering it from a container |

Every containerised tier writes a JUnit record to `RESULTS_DIR` and the runner **fails if the tier
collected zero tests** or skipped anything outside its policy. A summary line cannot distinguish
"everything passed" from "nothing ran"; the record can.

The `validation` target derives **from** `pipeline` and is never a base for it, so no test tooling
can reach a released image.

**Markers** (defined in `pyproject.toml`): `external_fixture`, needs licensed CAD from
`tests/fixtures/external/`; excluded from `test-fast` and `make check` (the difference
between a missing installable dependency, an honest SDK skip that runs in CI, and
missing non-redistributable DATA a clean clone can never have). `container`, needs the
real runtime image or native tooling (OpenFOAM/vmtk). `ui`, its own tier, above.

**What a green fast tier does NOT prove:** it stubs the graph framework and fakes the
stores, so it exercises product LOGIC, not real component interaction. That is the
deliberate trade (speed for hermetic isolation), and it is why the integration/container
tiers exist and must actually run somewhere with the real dependencies.

## Developer tools (`devtools/`)

`devtools/` holds what a developer runs to create, inspect, validate, or maintain this
software, never anything a user's job depends on. One directory per capability; see
[Developer tools](#developer-tools) below for the boundary and the canonical commands.

| group | holds | run by |
|---|---|---|
| `env/` | `setup.sh`, `doctor.sh` | `make setup`, `make dev-doctor` |
| `quality/` | `check_dependency_drift.py`, `mypy_ratchet.py` + `mypy_baseline.txt` | `make check`, CI |
| `release/` | `validate.sh`, `publish.sh`, `record.py` | `make release-validate`, `make release-publish`, the deploy scripts |

The two quality gates are the ones a machine runs and a human reads only when they fail:

| gate | enforces |
|---|---|
| `devtools/quality/mypy_ratchet.py` | exit 0 while the known baseline of pre-existing type errors holds; FAIL on any NEW one, on a baseline entry whose file no longer exists (a rename without re-keying), and `--update` refuses in an environment missing a canonical dependency (mypy sees fewer errors there: a baseline rewritten in a thin env breaks CI) |
| `devtools/quality/mypy_baseline.txt` | data for the ratchet: the accepted pre-existing errors, normalized (path, code, message); canonical to the FULL dependency set |
| `devtools/quality/check_dependency_drift.py` | one dependency source of truth: no inline pins in CI/Makefile/Dockerfile, no package pinned twice, no runtime pins in pyproject |

Nothing in CI or `make` may depend on the hand-run groups. A test harness is not a
developer tool: it belongs with its tier (`tests/native/run_tier.sh`,
`tests/integration/run_in_container.sh`). Something that fits no rule (a one-off migration
helper, a scratch script) does not belong in the repository at all.

## CI

`.github/workflows/ci.yml` runs `make check`, the architecture/hygiene suite, the
provider contracts (both backends of every seam), the mypy ratchet and the
dependency-drift check, all blocking, then builds and smokes all three image targets.
Actions are SHA-pinned; the workflow token is read-only; Dependabot watches pip and the
pinned actions weekly.

## Developer tools

Software used to **create, inspect, validate, or maintain this software**, never anything a
user's job depends on. Nothing here is imported by `src/meshpipeline/`, shipped in a wheel, or
present in a runtime image.

| group | what it owns | canonical command |
|---|---|---|
| `env/` | host-tool detection, `.venv`, local dirs, `.env`, images (`setup.sh`); read-only preflight of the hybrid dev config (`doctor.sh`) | `make setup` · `make dev-doctor` |
| `quality/` | the repository gates CI enforces: one dependency source of truth, and the mypy ratchet with its `mypy_baseline.txt` | `make dependencies` · `make typecheck` (both inside `make check`) |
| `release/` | Gate C: prove a commit is releasable (`validate.sh`), push what was proven (`publish.sh`), and the release-record schema both share (`record.py`, also read by the deploy scripts) | `make release-validate` · `make release-publish` |

All three are wired into `make` and CI. A script no gate runs does not belong here.

## What belongs elsewhere

| | goes to |
|---|---|
| a harness that builds an image, provisions services, and runs one test tier | that tier: `tests/native/run_tier.sh`, `tests/native/run_terminal_matrix.sh`, `tests/integration/run_in_container.sh` |
| test data and fixtures | `tests/fixtures/` |
| provisioning, promoting, or diagnosing the cloud mesh deployment (Gates D and E) | `deploy/gcp/scripts/` |
| anything a running job needs | `src/meshpipeline/` |

A one-off migration helper or a scratch script belongs in no directory here; it belongs
outside the repository.

## Geometry fixtures

Deterministic, license-free synthetic CAD/surfaces used by the native engine tests
(`tests/native/`) and a few unit tests. They live in `tests/fixtures/geometry/` and are committed:
the tests read them and never regenerate them.

| Fixture | What it is |
|---|---|
| `plate_with_hole_2d.step` | flat plate, central hole + two bolt holes: the gmsh plane-stress benchmark |
| `cht_enclosing_2region.step` | 2-solid CHT assembly (fluid box with a cubic cavity + the cube that fills it); conformal interface, fluid bbox == assembly bbox so no undeclared background region forms |
| `vessel_tube_open.vtp` | smooth open single-lumen tube (`R_TUBE` much smaller than `R_ARC`, so no self-intersection), consistent outward normals: the vmtk vascular input |

Changing one means committing the replacement geometry.

## Licensed external fixtures (not committed)

Some validation tests use real third-party geometry whose redistribution rights are not confirmed.
These are never committed, never baked into an image, and never uploaded in a build context. A fresh
clone does not have them and that is the intended state.

Supply them per machine under `tests/fixtures/external/geometry/`:

- `elbow90_fluid.step` (gmsh/cfmesh/snappy runs)
- `Naca0012.STEP`, `DPW4-CRM.stp` (external aero)
- `model.vtp` (vmtk vascular input)

Tests needing them carry the `external_fixture` marker; the fast suite deselects them and
`make test-external-fixtures` reports absence loudly rather than skipping silently. Production runtime
code must never reference this directory (enforced by
`tests/unit/hygiene/test_no_external_fixture_dependency.py`). Download-only public fixtures (aorta STL,
tube-reactor case) are documented in the engine docs.
