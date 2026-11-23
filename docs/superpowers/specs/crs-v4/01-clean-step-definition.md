# CRS: definition of a clean STEP (decided 2026-11-22)

## Success metric

The number of output STEPs that are **clean**, where the input was not. A clean input must never come back unclean; that counts as a regression.

## Why

CRS returns CAD that customers feed into their own in-house meshing. "Clean" must therefore be mesher-agnostic: the file has to work in whatever tool the customer uses, not only in Hexera's mesher or with Hexera's hints sidecar. The old metric, the gmsh diag/40 probe producing more than 0 tets, rewarded the wrong thing. On real production jobs, 10 of 12 "failing" benchmark files already meshed.

## A STEP is clean when it passes all three layers

### 1. Valid
- It re-reads after a STEP write/read round trip, and BRepCheck reports it valid on every solid.
- Every body is a closed, manifold, consistently oriented solid with positive volume.
- It has no free edges, no self-intersecting faces or wires, and no small-edge or small-face errors.
- Bodies don't overlap, except where they share faces exactly (conformal contact).
- Units are unchanged from the input.
- Checked with: BRepCheck_Analyzer, ShapeAnalysis, and BOPAlgo_ArgumentAnalyzer on the re-read file.

### 2. Meshable, mesher-agnostic
- It meshes in **every** mesher on a fixed panel, within the normal time limit.
- No tolerant fallback (fTetWild) counts, and no hints sidecar is allowed.
- The worst element quality (min SICN) is above a floor, proposed 0.01.
- Proposed panel, to be finalised:
  - Hexera production FEA settings (gmsh, diag/10 + zones, HXT);
  - gmsh defaults (no size settings);
  - gmsh with a second 3D algorithm (Delaunay vs HXT, Frontal 2D);
  - one non-gmsh mesher reading the B-rep through OCC (candidate: Netgen).

### 3. Faithful to the original
- The maximum sampled surface distance from the original is ≤ min(0.1 mm, 0.1% of the part diagonal).
- The area and volume changes are under set limits (current caps are 2%; to be revisited).
- No body is lost, and no face is lost except a declared sliver below the threshold.

## Consequences
- **The benchmark must be rebuilt** from files whose *input* fails this definition. The current 120-file ABC sample mostly passes layer 2 in production.
- **Hints become a separate bonus metric.** They are never part of "clean".
- **The AI is judged on clean-STEP gain over the deterministic pipeline.**

## Data constraint (2026-11-22)
Public data only until customer CAD is available. Benchmark sources:
- ABC (Onshape-derived; 100k already probed; 9,956 gmsh failures harvested).
- OCCT dataset 7.8.0 (already in the manifest).
- CAx-IF test-round files (the library page returned 403 to unauthenticated fetches; needs a manual download or another route).
- Candidates to add: NIST CAD/MBE PMI test models (CTC/FTC/STC, multi-system STEP exports), Fusion 360 Gallery (a non-Onshape origin), DeepCAD (Onshape again; low diversity value).
- Gap to flag: none of these reproduce customer translation defects well (cross-kernel tolerance mismatch, multi-body assemblies). Mitigation to evaluate: defect injection (the repo already has devtools/validation/defectgen_score.py) and cross-kernel round trips.
