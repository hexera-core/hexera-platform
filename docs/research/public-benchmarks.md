# Public Benchmarks For Mesh And CAD Repair

Status: draft seed, 2026-10-20. This document starts the external benchmark shape for research,
press, and customer-facing claims. It is deliberately stricter than an internal validation run:
anything published from it needs reproducible inputs, redistribution notes, frozen settings, and a
release artifact that another engineer can re-run.

## Inputs And Current Contrast

The CAD repair engine already has a conservative targeted loop: located defects, named entity
targets, per-operation caps, rollback on failed progress, and re-inspection of the written repair.
The corpus runner reports before/after defects by re-reading the output file, and the LLM planner
can run shadow or live behind the same repair session guardrails. The strongest current evidence is
the OCCT/NIST corpus work recorded in the demo package and in `devtools/validation/evidence/`.

The accessible README contrast says the engine repairs unsewn geometry well, but its capability is
narrow: OCCT repaired 430 real defects across 16 parts; NIST repaired none because triage and
review policy kept many files out of the loop; planar fill is limited by the paired-rim guard; and
some operation classes are still unexercised. That maps cleanly to benchmark needs: the benchmark
must separate diagnosis, triage reachability, operation coverage, repair success, and downstream
mesh success instead of collapsing them into one score.

The teammate repository is `hexera-core/hexera-cad-defect-generation` (private; the earlier failure
to find it was a wrong org and name). It generates STEP files with deliberately injected defects:
deterministic OpenCASCADE Boolean cuts/fuses sized to the model diagonal, labelled against a
normalized clean reference, with a blind LLM review loop. Its included campaign ships 50 STEP files
across 18 families with 60 labelled defects. Its core taxonomy is design-intent geometric damage —
`thin_wall`, `extreme_thin_ligament`, `isolated_debris`, `residual_boss`, `short_edge_sliver`,
`micro_slot`, `pin_hole`, `residual_flash`, `internal_void`, `fluid_obstruction` and interface
shifts — while the topology/tolerance defects our repair loop heals (`missing_face`,
`duplicate_face`, `reversed_face`, non-manifold joins, tolerance inflation) are mostly unimplemented
pilots there. Each `answer.json` carries the causal edit, a geometric witness (probe points, volume
delta, topology stats) and a one-sentence `expected_repair`. Integration point: a generated-defect
track with labelled ground truth, scored strictly after the run — answers are never shown to the
engine — while OCCT continues to represent messy real-world translator failures.

A first blind dry run over that campaign (engine commit a044a017, rules planner, host kernel) found
`small_face` on 42/50 files, triaged all 42 to manual cleanup, and attempted zero operations. That
is the measured contrast with the generator's design: their defects surface as sliver/small-face
geometry our triage currently routes away from repair, so triage reachability and a conservative
small-face operation are the first engine gaps this track exposes.

## Shared Publication Rules

Every benchmark row must record corpus provenance, redistribution status, input hash, engine commit,
container image digest, settings, hardware class, timeout, random seed where applicable, and the
release artifact used to run it. The published table can summarize, but the evidence bundle must
include per-case JSON and logs.

Do not publish a headline score over data we cannot redistribute unless the release clearly says
the files are third-party non-redistributable and provides hashes plus acquisition instructions.
OpenCascade data can be used as an internal and possibly externally reproducible benchmark source,
but its per-file bug-report provenance needs a redistribution review before bundling. NIST PMI STEP
files are safer for public redistribution and citation. Generated corpora from
`hexera-cad-defect-generation` should be ours to publish only after license and teammate review.

Baselines must be named and run from frozen artifacts. Minimum baselines are: no repair, current
rules planner, current LLM shadow agreement, live LLM planner where enabled, and at least one
external CAD kernel repair baseline when licensing permits. Mesh baselines must include the current
default engine selection and at least one fixed-engine path per supported mesh engine.

## CAD-Repair Benchmark

This is the cad-repair benchmark track.

### Tracks

1. **OpenCascade real-defect track.** OCCT 7.8.0 STEP corpus, externally fetchable, with hashes and
   no committed CAD files. This is the messy translator-failure track.
2. **NIST PMI/conformance track.** Public NIST STEP files, cleaner redistribution posture, strong
   for CAD translator diversity and small-feature behaviour.
3. **Generated-defect track.** `hexera-cad-defect-generation` campaign output: labelled defects
   per its taxonomy (thin walls, ligaments, slivers, flash, bosses, pin holes, micro slots,
   internal voids, debris, fluid-domain changes, interface shifts; plus its implemented pilots
   missing_face, duplicate_face, tolerance_inflation). Scoring uses `answer.json` ground truth
   only after the run; the engine sees STEP files alone.
4. **Adversarial safety track.** Bolt holes, paired rims, intentional vents, thin walls, internal
   passages, multi-solid assemblies, and cases where repair should refuse.

### Primary metrics

These are the primary metrics for CAD repair.

- Repair yield: files with fewer error defects after repair, credited only by re-inspection.
- Defect yield: count by defect code removed after writing and re-reading the repaired file.
- Safety refusals: correct refusal rate on intentional openings and unsafe/generated traps.
- Damage rate: any new warning-or-worse defect, solid loss, face loss, cap violation, or unaccounted
  area/volume change.
- Operation coverage: kept, rolled back, declined, and failed per operation.
- Triage reachability: files routed to repair, review, manual cleanup, ask-customer, or no-op.
- Planner value: live/shadow agreement, live repaired-defect delta versus rules-only, and decline
  quality for unsupported operations.
- Downstream utility: repaired CAD reaches mesh staging and produces a valid mesh more often than
  the original.

### Benchmark outputs

For every file, emit `before`, `triage`, `operations`, `refusals`, `after`, `repaired_defects`,
`damage_flags`, `mesh_attempt`, and `artifact_paths`. For press/research tables, publish aggregate
metrics by track and defect class, plus a small case study appendix with images and before/after
inspection snippets.

## Mesh-Engine Benchmark

This is the mesh-engine benchmark track.

### Tracks

1. **Clean geometry track.** Known-valid internal/external CFD setups that should mesh without
   repair; this isolates mesh engine quality from CAD repair.
2. **CAD-repair downstream track.** The same cases before and after CAD repair, measuring whether
   repair creates meshable geometry rather than merely satisfying the B-rep inspector.
3. **Engine-selection track.** Inputs where the expected engine differs: snappy, cfmesh, gmsh, and
   any native engine included in the dispatch registry.
4. **Stress and scale track.** Large assemblies, tiny features, thin gaps, high aspect-ratio
   domains, and tight boundary-layer requirements.

### Primary metrics

These are the primary metrics for mesh generation.

- Mesh success: terminal success with all required deliverables present.
- Mesh quality: max non-orthogonality, skewness, negative volume count, layer completion, boundary
  patch integrity, region labels, and solver-readiness checks.
- Fidelity: surface deviation from source CAD/STL, feature preservation, declared openings kept,
  and no unintended sealed passages.
- Cost: wall time, container/runtime cost where available, CPU/GPU/memory peak, cells per second,
  and retry count.
- Robustness: success under restart, checkpoint resume, worker takeover, and artifact delivery
  verification.
- Human burden: number of clarifications, review items, and manual overrides before success.

### Benchmark outputs

Each run should produce a manifest containing source hash, selected engine, engine version, settings,
mesh statistics, quality criteria, solver-readiness verdict, timings, and links to VTK/OpenFOAM or
other deliverables. Public figures should report both pass rate and quality distribution; a single
pass/fail score hides too much.

## First Implementation Slice

1. Add a benchmark manifest schema shared by `repair_corpus.py` and native mesh runs.
2. Extend the CAD corpus runner to write a publication bundle: row JSON, summary JSON, settings,
   input hashes, and log.
3. Add a mesh benchmark runner that consumes a directory of frozen scenarios and emits the same
   bundle shape.
4. Map the `hexera-cad-defect-generation` answer/manifest layout into the generated-defect track
   scorer: detection, localization, triage reachability and repair scored against `answer.json`
   after the run, never during it.
5. Publish an internal dry run over OpenCascade and NIST first; only after that choose the subset
   safe for external publication.
