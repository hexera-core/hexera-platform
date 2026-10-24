# Dynamic Mesh Workers Design

## Goal

Route each mesh to the smallest internal worker profile likely to complete it, while preserving
the requested mesh fidelity and allowing one bounded automatic escalation after a capacity
failure. The eventual large tier must support workloads approaching 700 million cells.

## Decisions

- Worker sizing is internal. This phase adds no customer-facing tier, price, or approval flow.
- Initial selection is deterministic and explainable. It uses evidence already available before
  native meshing: source bytes, surface triangles, requested cell budget, region count, boundary
  layers, and engine.
- CAD file size is one signal, never the sole sizing rule.
- Profiles are ordered `standard`, `high-memory`, `ultramem`.
- Capacity failures may move a run up exactly one profile once. Geometry, validation, and other
  semantic failures do not escalate compute.
- Escalation preserves requested fidelity; it never silently coarsens the mesh.
- Selection begins in shadow mode. The current Cloud Run executor remains authoritative until
  predictions have been compared with actual cells, memory, runtime, and artifact size.
- Policy is versioned so recorded recommendations remain interpretable as thresholds evolve.

## First Slice

Add a pure policy module containing workload signals, profile selection, and one-step escalation.
It has no provider imports and does not alter runtime dispatch. Unknown evidence selects the
standard profile and is identified in the decision reasons; invalid negative measurements fail
closed at the policy boundary.

## Later Slices

1. Record shadow recommendations beside actual execution measurements.
2. Replace the global cell ceiling with the selected profile's capacity envelope.
3. Add remote artifact references so large meshes are not returned through the worker filesystem.
4. Add a high-memory executor and an ultramem Compute Engine executor behind `MeshExecutor`.
5. Enable profile routing, then bounded automatic escalation, after calibration.

## Shadow Observation Slice

The first integration slice observes the existing Cloud Run provider without changing its
selection or result. It reads only files already declared in `.native_payload`; an undeclared
workspace is not recursively scanned. Recognised geometry files contribute source bytes, binary
STLs contribute their declared triangle counts, and bounded JSON inputs contribute `max_cells`,
`region_count`, `n_layers`, or `boundary_layers` when present.

Immediately before submission, the observer selects a profile. After the provider returns, it
logs one machine-readable record containing the policy version, workload, recommendation,
reasons, elapsed seconds, return code, timeout flag, and actual cell count when supplied. An
exception is logged by type and re-raised unchanged. Any observation failure is warning-only and
must not prevent or alter the provider call. Memory and returned-artifact measurements remain for
a later provider that can expose them without scanning a potentially enormous workspace.

## Verification

Unit tests pin threshold boundaries, strongest-signal behavior, engine/layer pressure, unknown
evidence, invalid measurements, eligible escalation, ineligible failures, and the one-retry cap.
Runtime routing remains unchanged in this slice.
