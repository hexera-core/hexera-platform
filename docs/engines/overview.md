# Engines: the capability contract and how to extend it

An **engine** is a physical meshing capability (cfMesh, snappyHexMesh, Gmsh, VMTK, a
multi-region snappy workflow); the user-declared **purpose** (`structural` /
`external_cfd` / `internal_cfd` / …) is the workflow. The same engine serves FEA and CFD
when its declared capabilities allow it.

## The two-state contract

A capability either appears in an engine's spec, meaning production-supported, backed by
a real full-pipeline delivery, and selectable, or it does not exist and admission rejects
the request before the builder. There is no experimental / wrapper-supported /
upstream-supported / hidden middle state anywhere in runtime metadata
(test-enforced: `tests/unit/engines/test_two_state_contract.py`).

* **Supported**: the system runs the request through intake → admission → builder →
  executor → gates → reviewer/metric → deliverable/upload → final result → trace. Every
  registered capability has a delivery-evidence entry in
  [`src/meshpipeline/engines/validation_evidence.json`](../../src/meshpipeline/engines/validation_evidence.json)
  (machine-checked both directions: no capability without evidence, no evidence entry
  without a capability).
* **Rejected**: the selected engine does not support the request; admission (or
  configuration, for geometry-derived facts) rejects it with the reason. No third state.

Two standing rules:

1. A capability the underlying tool lacks must never be simulated by wrapper invention
   (the retired snappy pseudo-2D is the cautionary case: it delivered a real mesh and was
   still wrong, because "snappy meshes 2D" was false).
2. A capability not registered in a spec is simply not a capability: it is rejected, and
   future intent lives in the backlog below, never in the registry.

## Supported capabilities (evidence: `validation_evidence.json`)

| Engine | Capability | Notes |
|---|---|---|
| cfmesh | body-surface → fluid-volume | internal + external 3D; the true-2D engine (external profiles via native `cartesian2DMesh`); 2D internal rejects |
| snappy | body-surface → fluid-volume | body-fitted + prism layers, internal or external, **3D only**; near-wall layer coverage on severe wing-body junctions may sit in the partial band (documented envelope limit) |
| gmsh | solid-body → solid-volume | second-order tet FEA (Abaqus `.inp`) |
| gmsh | fluid-domain → fluid-volume | mesh a supplied fluid domain for CFD |
| gmsh | planar-domain → surface-mesh | 2D plane-stress FEA from a flat face |
| snappy_multiregion | solid-assembly → multiregion-volume | coupled conformal regions (CHT/multi-material); no undeclared region may ship (reconciliation-gated) |
| vmtk | body-surface → fluid-volume; fluid-domain → fluid-volume | radius-adaptive tets with wall layers inside a tubular or branching lumen; a CAD body with declared ports is opened and sized by the engine itself (`engines/vmtk/lumen_staging.py`) |

## Rejected boundaries (enforced in code)

* **snappy**: 3D only, 2D, "2.5D", and thin-slab-as-2D reject at intake (a thin slab
  meshed in full 3D with symmetry patches is declared 3D).
* **cfmesh**: the declared 2D (external profile via `cartesian2DMesh`) and 3D Cartesian
  envelopes only, 2D internal profiles reject with the reason.
* **gmsh**: 3D solid→tet and 2D planar→plane-triangle FEA. Advanced modes (periodic,
  transfinite/structured, quad-recombination, embedded curves/surfaces, shell export) are
  not capabilities, the driver rejects unknown spec keys loudly.
* **vmtk**: a supplied surface must be an open, non-self-intersecting lumen - closed
  shells and self-intersecting surfaces reject up front. A CAD body is different: the
  engine removes the declared port faces itself (real inlet/outlet holes), bounds the
  triangle edges, measures the lumen's local radius at every wall point and sizes the
  cells from it - no centerline seeding is asked of the builder. Ports the user did not
  declare cannot be opened, so an undeclared opening stays wall.
* **snappy_multiregion**: requires a true multi-solid assembly yielding a clean declared
  region plan; single-solid input and multi-scale assemblies outside the validated
  envelope reject/fail structured.

## Evidence method

The product state is binary; internally, evidence maturity is described by tiers (T1 pure
logic → T5 full-pipeline delivery → T6 re-proven on current code). **A capability may not
register until it has T5 evidence**, a real job through the whole pipeline to a delivered
artifact. The structured record keeps, per capability: tier, delivering job ids, dates,
and a measurable summary.

To produce new evidence: with the stack up (`make dev-up`) and real STEP/VTP files on hand,
drive one case end-to-end through the API (upload → intake conversation → confirm → poll
to terminal) and check: executor gates passed, review verdict PASS, `mesh_bundle`
downloadable. Then add the delivery to `validation_evidence.json`.

### Fixture rule

Synthetic CAD is allowed; toy CAD is not enough. A fixture is valid when it is (1) deterministic,
(2) committed, (3) non-trivial enough to exercise the intended engine path, and (4) run through
the REAL full pipeline. Current committed fixture:
`tests/fixtures/geometry/plate_with_hole_2d.step`, flat plate, central hole and two bolt holes,
the plane-stress stress-concentration benchmark.

### Public validation fixtures (unlicensed upstreams: download-only, do NOT commit)

Neither source repo carries a license, so these stay download-only per machine:

* **vmtk, open aorta lumen** (the VMTK project's own test data):
  ```bash
  curl -L -o aorta-surface-open-ends.stl \
    https://raw.githubusercontent.com/vmtk/vmtk-test-data/master/input/aorta-surface-open-ends.stl
  ```
  594 KB STL; open lumen, 3 boundary profiles, not self-intersecting. Known-good run:
  radius-adaptive fill, `boundary_layers=0`, seeding source `[0]` targets `[1, 2]`
  (layers at `edge_length_factor=0.3` invert tets on this geometry, the gate correctly
  rejects; use fewer/no layers or finer edge length).
* **snappy_multiregion, tube-reactor case**
  (`chemicallyGeeky/MultiRegion_meshing_with_OpenFOAM_ex1`): fluid `gas` + solid `shape`
  (a 1 mm foil). COARSEN before meshing, stock settings build ~30 GB meshes: background
  `(20 20 160)` → `(10 10 80)`, every refinement `level` → `(1 1)` **except the `shape`
  surface (keep `level (6 6)`, the foil cannot castellate coarser)**. Then `blockMesh &&
  surfaceFeatureExtract && snappyHexMesh -overwrite && splitMeshRegions -cellZonesOnly
  -overwrite` and drive `multiregion_runner.check_mesh` / `finalize` / gates on the split
  case (~430k cells).

### Ops note: the remote mesh executor is a SECOND image

Mesh execution runs on a Cloud Run Job (`CLOUDRUN_JOB`) with its own image. Rebuilding the
local worker does **not** update it: after any change to an engine's runner/driver,
rebuild + push the mesh image and update the job, or the two silently drift (a live gmsh
proof once failed a full round on exactly this: the worker authored a correct spec that a
stale remote validator rejected). Build steps: [deployment.md](../deployment/overview.md).

## One folder per engine

An engine's **complete identity** lives in `src/meshpipeline/engines/<name>/`. The
pipeline (intake, builder, executor, reviewer, uploader) is a generic executor of what
the folder declares, adding an engine changes **no shared code**.

```
engines/
  base.py        the contracts: EngineSpec, ParamSpec, GateSpec/GateCtx/run_gates,
                 Criterion, Briefing, Deliverable, RunPolicy, MeshEngine protocol
  registry.py    catalog BY DISCOVERY (every subfolder's spec.SPEC) + helpers
  runtime.py     get_engine(name) → engines/<name>/adapter.ENGINE
  <name>/        one engine
```

| File | What it declares | Consumed by |
|---|---|---|
| `spec.py` | `SPEC = EngineSpec(...)`: descriptor, export_formats, intake_guidance, `intake_params`, `capabilities` (input→output mesh kinds), gates loader, `briefing`, `deliverable`, `run_policy`, `visual_review`, optional hooks. (Region-contract vocabulary lives on `Purpose`: see `engines/purposes.py`.) | everything |
| `pack.py` | builder system prompt + tool-name set | builder |
| `builder_guidance.py` | first-message fragments: geometry line, workflow, contract wording, domain default | builder_messages |
| `criteria.py` | `CRITERIA_ROWS` (machine-checkable bars with citations) + `REVIEW_AXES` (the engine's semantic review rubric) | spec hooks → manifest report, reviewer rubric, run guidance |
| `authoring.py` | flow engines: `AUTHORING_TOOL` (the `configure_mesh` palette), `validate()` (loud rejection of unknown/cross-engine/out-of-range fields), `recommend()` (geometry-derived strategy) | builder via spec hooks |
| `adapter.py` | `ENGINE`: the thin runtime adapter (the MeshEngine seam loaded by `engines/runtime.py`); forwards run/finalize/check_mesh to the native runner | builder tools, executor |
| `<engine>_runner.py` | the NATIVE mesher (`cfmesh_runner.py`, `snappy_runner.py`, `multiregion_runner.py`, `gmsh_runner.py`, `vmtk_runner.py`); `dispatch.py` imports `_run_*_local` from it | dispatch, mesh_runner |
| `gates.py` | executor gates (only when not shared: each flow engine keeps its own `flow_gates.py`) | executor via `spec.gates` |
| `drivers.py` etc. | optional engine-owned machinery (snappy: planner + deterministic drivers) | spec hooks |

**Planned** rows are folders too, containing only `spec.py` with `implemented=False` -
enforced spec-only until they ship.

## CAD handling: the same file, prepared five different ways

An engine does not receive "the geometry". It receives whatever **its own bundle** prepared from the
upload, and the bundles differ enough that the same STEP file reaches two engines as two different
things. `MeshEngine.tessellate_to_stl` is an ENGINE method for exactly this reason, dispatched
through `cad/staging.py` - CAD preparation is engine-owned, like the spec and the runner.

### What the file itself carries

Before any engine is involved, `cad/regions.py` reads what the CAD **distinguishes**: the named
components of an assembly. It uses OpenCASCADE's XDE reader, because the plain `STEPControl_Reader`
returns `OneShape()` with the assembly and its names already dropped - which is why a file that names
its parts and one that does not are otherwise indistinguishable downstream.

This is a fact about the **file**, not about any engine, so it lives in `cad/` and is read once per
upload (cached; a 25 MB STEP costs seconds).

| the file says | consequence |
|---|---|
| several named components | they can become separately named patches, on an engine that carries them |
| one unnamed body | no engine can name parts the file does not contain |

### How each engine consumes it

| engine | path from CAD to mesher | named regions survive because |
|---|---|---|
| **snappy** | STEP → named STL solids → `snappyHexMeshDict` | `geometry { regions {...} }` + per-region `patchInfo`, and one layer entry per region |
| **cfMesh** | STEP → named STL solids → `geom.fms` | `renameBoundary` maps each FMS solid to its own patch |
| **gmsh** | STEP → imported into gmsh's own OCC kernel, **B-rep staged** | physical groups are assigned on CAD topology; no flat surface is involved |
| **vmtk** | a lumen surface (`.vtp`), or a STEP body opened at its declared ports (`lumen_open.vtp` + `vmtk_staging.json`) | `CellEntityIds` carry per-region identity through TetGen: 1 = wall, one id per capped port, bound back to the declared names |
| **snappy_multiregion** | STEP → **volume** regions (fluid/solid) | each region contributes its own named boundary patches |

The two OpenFOAM engines share a shape - named solids in one surface file - and nothing else does.
gmsh never goes through a triangulated surface at all, and vmtk's input is not an assembly.

### The failure this structure prevents

Both OpenFOAM bundles once read the staged surface with `read_stl_triangles`, which dissolves solid
boundaries, and wrote a single wall solid. The named components were destroyed **in surface prep**,
before either mesher could act on them - so a limitation that looked like the mesher's was the
pipeline's own. cfMesh was the worse case: it declared it could deliver several named wall patches
and then silently delivered one.

Two rules follow, and both are load-bearing:

1. **A capability flag describes the whole path.** `supports_multiple_wall_patches` is a claim about
   the mesher, the surface this system prepares for it, AND the dict it authors - not about the
   upstream tool's documented features. Every engine declares it explicitly with the evidence beside
   it; none inherits the default.
2. **Admission asks about the engine and the geometry.** A capable engine is never blocked on a file
   that carries the regions, and no engine can name a part the file does not contain. Unknown
   geometry is admitted rather than refused: silence is not evidence of absence.

## Adding an engine

1. **Create the folder**: `engines/<name>/` with the files above; `SPEC.name` must equal
   the folder name.
2. **Declare, don't wire.** The registry discovers the folder; the intake menu, the
   mesh_engine enum, the boundary-role union, engine_params validation, gate execution,
   review protocol, run/submit policy and the deliverable bundle all derive from the
   spec. There is **no manual registration anywhere**, criteria, rubric, authoring
   palette, gates and any workspace scaffold load lazily from `spec.py`.
3. **Implementation libraries**: machinery used by ONE engine lives in the folder;
   machinery shared by several sits beside the folders as a named module
   (`engines/openfoam_criteria.py` states a solver limit once for both OpenFOAM-family
   engines). No engine imports another engine's package.
4. **The lego rule**: the LLM chooses *strategy values* or authors a *declarative,
   re-runnable spec*; a deterministic renderer emits the engine dict. The model NEVER
   hand-writes engine syntax. LLM-written code is allowed only in the `run_python` jail,
   for computing values, never as the deliverable.
5. **Deliver the capability live**, add its entry to `validation_evidence.json`, and
   **flip `implemented=True`**. That is the release: menus, enum, roles and routing
   unlock automatically (`test_engine_lands_with_zero_edits_elsewhere`).

### The tests that catch a half-built engine

`test_engine_runtime_contract.py::test_the_catalog_and_the_shipped_bundles_agree`
(folder <-> row parity: a row with no package cannot load, a package with no row still
ships in the wheel),
`test_shared_layer_neutrality.py` (shared code holds no engine vocabulary),
`test_engine_catalog.py::test_implemented_rows_declare_the_full_contract`,
`test_data_contract.py` (state/session/corpus threading must be registered; engine params
travel as ONE `engine_params` dict), the manifest registry (`MANIFEST_KEYS`, one uniform
writer), and the two-state suite above.

### What stays global (do NOT copy per engine)

The manifest writer (`engines/manifest.py`), the gate runner (`run_gates`, crash ⇒
SystemFailure, written once), the executor/reviewer nodes, the review-protocol
implementations, intake composition, the graph, and the run_python jail. Variation lives
in declarations, not copies. A spec field earns its place only when shipped engines
*differ* in it or a consumer exists today.

### The four audit seams (why this structure exists)

New-engine bugs cluster in exactly four places: tool gates (`run_policy`), builder
message wording (`briefing`), declared-param threading (`intake_params` →
`engine_params`), and artifact packaging (`deliverable`). Each is a declaration the
engine folder owns, filling the folder IS the end-to-end trace.

## Backlog: not part of the runtime contract

Future intent lives here (and in GitHub issues), never in the capability registry, the
intake menu, or an engine spec. None of these are selectable; requesting them is
rejected: gmsh advanced modes (periodic, embedded curves/surfaces, transfinite, shell
export, quad-dominant); cfmesh 2D internal profiles; snappy_multiregion 19-solid-class
multi-scale assemblies + expected-interface derivation (contact detection); a
cross-format export layer beyond each engine's native writers.
