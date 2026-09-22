# CAD repair system before AI meshing

Date: 2026-09-22
Status: designed, awaiting review
Scope: one subsystem reused by the meshing pipeline, a standalone repair product surface, and the test/native harness.

## The problem this solves

Broken or marginal CAD currently reaches engine staging and meshing before the product has a
first-class way to say what is wrong, whether it can be repaired, what changed, and whether the
change made the geometry more meshable. The existing pipeline can reject some measured geometry
before the builder, and each engine can stage the same upload differently, but there is no durable
repair report or reusable product flow.

The repair system must sit before AI meshing without becoming another opaque mesher. It must be
deterministic, bounded by tolerances, auditable, and honest when a geometry needs a human or a
different modeling decision.

## Research baseline

The feature is grounded in kernel and meshing literature rather than model-generated editing.

- OpenCASCADE shape healing is the primary repair authority for STEP/IGES B-rep inputs. It already
  exposes the operations this product needs: wire reordering, small-edge handling, connected-edge
  fixes, curve consistency, degenerated-edge fixes, wire self-intersection handling, gap repair,
  sewing, and explicit control over whether repairs may change tolerance, topology, or geometry.
- Gmsh's OpenCASCADE `HealShapes` path is useful for cross-checking mesh-oriented CAD healing and
  for engine-owned Gmsh preparation, but it is not the product's core repair authority.
- CGAL-style polygon mesh repair is the right reference model for STL/VTP triangle-soup repair:
  orientation, duplicate cleanup, border stitching, non-manifold vertices, degeneracies, hole
  filling, and self-intersection repair. It is not an immediate dependency because licensing and
  packaging need separate approval.
- Recent AI-for-geometry-preparation surveys point to a narrow role for AI: prediction,
  classification, and workflow assistance with measured validation. They do not justify
  autonomous unbounded CAD mutation on the mesh critical path.

## Goals

- Produce a durable, structured CAD repair report before meshing starts.
- Reuse one repair core from both the meshing pipeline and a standalone product surface.
- Preserve existing unit interpretation and content identity rules: a repair never guesses units
  and never mutates the original uploaded source.
- Bound every automatic mutation by a named policy, tolerance cap, and before/after validation.
- Make the meshing integration small: repaired geometry is just the geometry materialized for
  engine staging, with provenance attached.
- Keep the first feature slice reviewable: diagnostics only, no geometry mutation.

## Non-goals

- No AI-authored CAD edits in the initial system.
- No broad defeaturing in the first release. Deleting fillets, logos, tiny holes, and internal
  details is analysis-dependent and belongs behind later policies.
- No new meshing engine. Repair prepares inputs for the engines that already exist.
- No silent source replacement. The original upload remains the durable source of record.
- No CGAL runtime dependency in the first implementation.

## Product contract

The repair core exposes one operation:

```text
RepairRequest -> RepairResult
```

`RepairRequest` contains:

- `source`: a `GeometrySourceRef` plus a `GeometryInterpretationRef`.
- `mode`: `inspect` or `repair`.
- `profile`: `conservative`, `mesh_ready`, or `manual_review`.
- `target`: `standalone` or `meshing`.
- `engine`: optional, present only when the caller wants engine-specific staging checks.

`RepairResult` contains:

- `status`: `clean`, `repairable`, `repaired`, `unrepairable`, or `inconclusive`.
- `input`: source id, sha256, suffix, interpretation id, unit basis, and measured size.
- `output`: optional repaired geometry source/artifact reference.
- `report`: a structured `RepairReport`.
- `policy`: the repair profile and exact tolerance caps used.

`RepairReport` contains:

- Counts and severities for discovered defects.
- Operations attempted, skipped, and completed.
- Whether geometry, topology, or tolerance changed.
- Before/after validity checks.
- Before/after surface/staging checks when an engine was supplied.
- A user-facing summary and an operator-facing diagnostic block.

The report is part of the product surface. It is not a log-only diagnostic.

## Defect taxonomy

The first report vocabulary is deliberately narrow and stable:

| Code | Meaning | First action |
| --- | --- | --- |
| `invalid_brep` | OCCT validity check fails | report only |
| `open_shell` | a solid/workflow needs closure but free boundaries remain | report only |
| `wire_gap` | adjacent wire endpoints are disconnected within profile bounds | report only, later gap fix |
| `small_edge` | edge length below profile threshold | report only, later ShapeFix small-edge policy |
| `small_face` | face area below profile threshold | report only, later profile-controlled removal or merge |
| `degenerate_edge` | degenerated or near-zero edge | report only, later ShapeFix degenerated policy |
| `curve_inconsistency` | missing/wrong 2D or 3D curve, same-parameter mismatch | report only, later ShapeFix edge policy |
| `self_intersection` | B-rep wire or staged surface intersects itself | report only |
| `non_manifold_surface` | STL/VTP edge or vertex topology cannot define a manifold surface | report only |
| `duplicate_surface_data` | duplicate points/faces/entities found | report only |
| `engine_staging_failure` | repaired or original geometry cannot be staged for the chosen engine | report only |

The taxonomy separates CAD-kernel facts from surface-mesh facts. A STEP can be valid as a B-rep
and still produce a bad staged surface; the report must show which layer failed.

## Repair profiles

`inspect` mode never mutates geometry and can run in both product surfaces from the first slice.

`repair` mode requires a profile:

- `conservative`: tolerance-only and connectivity repairs within a cap derived from model scale;
  no feature deletion, no hole filling, no face removal.
- `mesh_ready`: conservative repairs plus small-edge/small-face cleanup and sewing when the
  before/after deviation remains under the profile cap.
- `manual_review`: no automatic mutation; returns repair suggestions and measured locations.

The initial implementation creates the profile vocabulary and enforces `inspect` only. Later
implementation tasks turn individual `repair` operations on one at a time.

## Where it fits in the meshing pipeline

Current flow:

```text
upload -> unit interpretation -> execution geometry materialization
       -> engine select -> geometry admission -> builder -> executor
```

Target flow:

```text
upload -> unit interpretation -> execution geometry materialization
       -> CAD repair inspect/repair
       -> engine select -> geometry admission -> builder -> executor
```

The repair node runs after materialization because it needs verified bytes and a verified unit
interpretation. It runs before geometry admission because admission should judge the geometry the
builder will actually stage. For the first implementation, the node records an inspect report and
passes through the original geometry unchanged.

When repair mode is enabled, the node writes repaired bytes to a new durable object and updates the
pipeline geometry handle for this run only. The original `GeometrySource` row remains untouched.
The state carries both identities:

- `geometry`: the effective geometry for engine staging.
- `source_geometry`: the original upload lineage.
- `cad_repair`: the report and output provenance.

The final result can then say whether a mesh used repaired geometry.

## Standalone product surface

The standalone repair product uses the same upload/source records and object store as meshing.

API shape:

```text
POST /api/v1/cad-repair/jobs
GET  /api/v1/cad-repair/jobs/{repair_job_id}
GET  /api/v1/cad-repair/jobs/{repair_job_id}/report
GET  /api/v1/cad-repair/jobs/{repair_job_id}/download
```

The first release may be synchronous for `inspect` if the file is already materialized cheaply, but
the durable shape is a job because repair can become native and long-running. A repair job is not a
simulation job and does not enqueue the AI meshing pipeline.

The console can later expose this as a separate product: upload CAD, inspect/repair, download the
fixed file and report, then optionally start a mesh from the repaired output.

## Harness exposure

The harness should call the same repair core or API-backed job path. It must not grow its own repair
implementation.

Initial harness exposure:

```text
make repair-inspect GEOMETRY=path/to/file.step UNIT=millimetre
```

The target returns the JSON report and exits non-zero only for infrastructure or contract failures.
A geometry with defects is a successful inspection whose report says `repairable` or
`unrepairable`.

Native harness exposure comes later for repair operations that need the mesh image. The normal
developer control plane should not install a second CAD toolchain beyond the existing OCP/PyVista
dependencies.

## Storage and schema

First slice can avoid a migration by storing reports as artifacts or capture payloads only when
called from the meshing path. The standalone product needs durable read models, so it should add:

```sql
cad_repair_jobs(
  id uuid primary key,
  owner_id text not null,
  organization_id text not null,
  source_id uuid not null,
  interpretation_id uuid not null,
  mode text not null,
  profile text not null,
  status text not null,
  report jsonb not null,
  output_object_key text,
  output_sha256 text,
  output_size_bytes bigint,
  created_at timestamptz not null,
  updated_at timestamptz not null
)
```

The output object is not a replacement for `GeometrySource` until the user chooses to mesh from it.
At that point the product records a new `GeometrySource` row for the repaired bytes so the existing
materialization, retention, and source-integrity contracts still apply.

## Engine boundaries

Repair is shared CAD infrastructure. Engine staging remains engine-owned.

The repair core may ask an engine to stage or measure the effective geometry as a validation step,
but it does not import engine internals directly. It uses the same `prepare_surface(...,
engine=name)` seam already used by geometry admission.

No engine capability claim changes merely because repair exists. An engine that cannot support an
input kind still rejects it. Repair can make bad geometry cleaner; it cannot create a supported
capability that the engine does not declare.

## Failure semantics

- A repair infrastructure failure is a system failure and must not be reported as bad geometry.
- A defect report is not a failure. It is the product doing useful work.
- An automatic repair that worsens validity, increases deviation beyond profile caps, loses units,
  drops named regions, or breaks engine staging is rejected and the original geometry remains the
  effective geometry.
- A standalone repair job may finish as `unrepairable` with HTTP 200 on read. The job succeeded;
  the geometry could not be safely fixed automatically.

## User-facing copy

The product should not say "fixed" unless it has a repaired output and a passing after-check.
Preferred language:

- `No repair needed.`
- `Repair recommended before meshing.`
- `Repaired geometry is ready for meshing.`
- `Automatic repair was not safe for this geometry.`
- `Inspection could not complete because a service dependency failed.`

## Implementation slices

### Slice 1: Diagnostics-only repair core

- Add repair contracts and `inspect` mode.
- Read STEP/IGES through OCP and report validity/tolerance/topology findings that are available
  without mutation.
- Read STL/VTP through existing PyVista helpers and report surface defects already supported by the
  codebase: self-intersection when available, triangle count, bounds, and basic manifold-like
  counts.
- Add unit tests around deterministic reports and taxonomy.

### Slice 2: Pipeline inspect node

- Add a graph node after execution geometry materialization and before geometry admission.
- Record inspect report in pipeline state.
- Do not alter geometry.
- Add unit routing tests and a focused integration test proving the builder still receives the
  original geometry.

### Slice 3: Standalone inspect product

- Add durable `cad_repair_jobs` table and repository.
- Add API routes for repair job create/read/report.
- Support `inspect` mode only.
- Add API and integration tests for tenancy, storage dependency failures, and report reads.

### Slice 4: Conservative B-rep repair

- Add OpenCASCADE ShapeFix/Sewing-backed repair operations behind `profile=conservative`.
- Emit repaired bytes as a separate object.
- Reject repairs that fail after-checks or exceed deviation/tolerance caps.
- Add native/container tests with synthetic fixtures for small gaps, disconnected wire endpoints,
  and small edges.

### Slice 5: Mesh-path opt-in

- Allow a meshing run to use conservative repair output as the effective geometry only when the
  repair result is `repaired` and the chosen engine staging check passes.
- Add final-result provenance and user-visible messaging.
- Add an end-to-end integration test with a geometry that fails inspection before repair and passes
  staging after repair.

## Verification strategy

Each slice must have its own proof:

- Unit tests for report schema, status transitions, and deterministic classification.
- CAD unit tests with synthetic STEP fixtures created by the existing OCP fixture style.
- Integration tests for object storage, database persistence, tenancy, and API boundaries.
- Native/container tests only when the implementation uses real native repair or engine staging.
- Regression tests that original uploads are never overwritten and repaired outputs carry new
  content identity.

The strongest acceptance signal is not "repair ran"; it is "the repaired geometry passes the
same downstream staging/admission checks the original failed, within a recorded deviation cap."

## Risks and controls

| Risk | Control |
| --- | --- |
| Repair changes engineering intent | Start inspect-only; require profiles, caps, before/after checks, and report changed geometry/topology/tolerance separately |
| Repair hides an engine limitation | Engine admission remains authoritative; repair never changes engine capabilities |
| Tolerance inflation makes a model valid but physically wrong | Record tolerance deltas; cap by model scale; reject over-cap repairs |
| Standalone product and meshing path drift | One repair core; harness and API call the same contracts |
| Triangle-soup repair needs stronger algorithms than PyVista | Defer CGAL-style repair behind dependency/licensing review |
| Named CAD regions disappear | Region preservation is an after-check before repaired geometry can enter meshing |

## Deferred decisions

- Whether CGAL can be shipped, isolated in a native worker, or used only as a research reference.
- Whether repaired geometry should be exported in the original CAD format or in a normalized STEP
  representation. Initial repaired B-rep output should be STEP because the upload path already
  accepts it and it preserves CAD topology better than STL.
- Whether the console repair product is a separate top-level page or an upload preflight inside
  the existing simulation flow.
- Which repair operations graduate from `conservative` to default-on before meshing.

