# Dynamic Mesh Workers Design

## Goal

Evaluate a simple preflight sizing heuristic against the authoritative machine routing and actual
mesh outcomes. The calibration data should help tune capacity for workloads approaching 700
million cells without changing where any mesh runs.

## Decisions

- Worker sizing is internal. This phase adds no customer-facing tier, price, or approval flow.
- Initial selection is deterministic and explainable. It uses evidence already available before
  native meshing: source bytes, surface triangles, requested cell budget, region count, boundary
  layers, and engine.
- CAD file size is one signal, never the sole sizing rule.
- Profiles are ordered `standard`, `high-memory`, `ultramem`, but are analytical labels rather
  than provider or machine types.
- `contracts.mesh_machine` remains the sole runtime authority for Cloud Run/GCE selection and
  capacity escalation. This policy must not duplicate or override it.
- Selection remains in shadow mode until predictions have been compared with actual cells,
  runtime, and the machine selected by the runtime policy.
- Policy is versioned so recorded recommendations remain interpretable as thresholds evolve.

## First Slice

Add a pure policy module containing workload signals and profile selection. It has no provider
imports and does not alter runtime dispatch. Unknown evidence selects the standard profile and is
identified in the decision reasons; invalid negative measurements fail closed at the policy
boundary.

## Later Slices

1. Compare shadow recommendations with `ran_on`, actual cell count, elapsed time, and failures.
2. Calibrate or retire the heuristic from observed data; runtime routing stays in
   `contracts.mesh_machine`.
3. Add memory and artifact measurements when providers can expose them without scanning a
   potentially enormous workspace.

## Shadow Observation Slice

The first integration slice observes the existing Cloud Run provider without changing its
selection or result. It reads only files already declared in `.native_payload`; an undeclared
workspace is not recursively scanned. Recognised geometry files contribute source bytes, binary
STLs contribute their declared triangle counts, and bounded JSON inputs contribute `max_cells`,
`region_count`, `n_layers`, or `boundary_layers` when present.

Immediately before submission, the observer selects a profile. After the provider returns, it
logs one machine-readable record containing the policy version, workload, recommendation,
reasons, elapsed seconds, return code, timeout flag, actual machine decision, and actual cell count
when supplied. An exception is logged by type and re-raised unchanged. Any observation failure is
warning-only and must not prevent or alter the provider call. Memory and returned-artifact
measurements remain for a later provider that can expose them without scanning a potentially
enormous workspace.

## Verification

Unit tests pin threshold boundaries, strongest-signal behavior, engine/layer pressure, unknown
evidence, invalid measurements, actual machine capture, and fail-open observation. Runtime routing
remains unchanged in this slice.
