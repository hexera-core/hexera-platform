# Deterministic CAD healing research: key takeaways (2026-11-22)
Full report is in the conversation (subagent hand-back); this file keeps the decision-relevant core.

- **Root cause of gmsh's "overlapping facets" / "segment-facet" failures:** the surface triangulation self-intersects before tet meshing. Four causes: (1) non-adjacent faces closer than the chord deviation (thin walls, near-coincident faces, non-conformal bodies); (2) face meshes leaving the true surface (SameParameter violations, large edge tolerances, pcurve/3D mismatch); (3) coarse triangles on curved, periodic or C0 faces; (4) true self-intersection or pole collapse.
- **Splits are the best lever.** They add topology, not geometry, so deviation is 0 and they are storable in STEP. Under a mesher-agnostic metric the B-rep itself must survive coarse default sizing. Virtual topology, pinch, compounds and fTetWild only count as diagnostics; a composite must be baked by refitting under the Hausdorff gate.
- **Standard healer order** (ShapeProcess, ACIS, CADfix, SpaceClaim): baseline → normalise (pcurves, SameParameter, tolerances) → wires/edges → faces → shells (sewing ladder, fill) → solids (MakerVolume fallback) → between bodies (General Fuse + CellsBuilder) → simplify (UnifySameDomain, artefact defeature) → mesh conditioning splits (angle, closed, continuity, proximity) → finalise (round trip, deviation, panel). Each stage is transactional.
- **Highest-value OCCT ops to build (15):**
  1. proximity-driven face split (generalised gap split);
  2. angular/periodic split;
  3. continuity split;
  4. inter-body conformalisation (fragment/fuse/cut-by-priority with fuzzy value);
  5. duplicate/coincident face removal;
  6. rebuild a high-tolerance edge as the face-face intersection;
  7. locate and rebuild a self-intersecting face (CheckerSI + MakeFilling);
  8. small edges/slivers within budget;
  9. sewing ladder + MakerVolume;
  10. hole fill, planar then Plate;
  11. BSpline restriction / canonical recognition;
  12. pole and degenerate repair;
  13. UnifySameDomain, ordered so it doesn't undo conditioning splits;
  14. orientation and solid fix;
  15. baked composite refit.
- **Where AI judgement helps:** overlap semantics (duplicate vs fit vs subtract); hole vs missing face; choosing among alternative fixes under the deviation budget; ordering and recovery when stages interact; mapping a mesher error to its causal B-rep entity; knowing when to stop.
- **Where deterministic code wins:** tolerances, all checks, sewing, standard ShapeFix passes, known-criteria splits, deviation measurement.
- **Best architecture:** a verifier-driven best-first/beam search over (op, entities, args). The LLM proposes; learned per-op predictors (Sandia/Cubit style, trained on auto-generated panel labels) rank; the deterministic verifier decides. Outcomes rank lexicographically: valid > panel passes > min SICN > deviation > topology churn.
- **Observations:** structured JSON (global checks, panel results mapped to stable B-rep IDs with coordinates, per-defect k-hop face-graph summary, proximity pairs as distance/h, budget remaining). Images only for intent questions. Keep stable IDs through BRepTools_History.
- **Actions:** typed parametric calls, transactional, each returning a diff (entities changed, deviation used, checks delta, panel delta).
- **Prior art:** no benchmarked learned or agentic healer for real STEP exists (novel). Patterns to borrow: CAD-Assistant (closed-loop tool calls with inspection after each), CADCodeVerify (verify-then-fix), Owen–Shea–Martin, Sandia 2019 (12 Cubit ops + per-op mesh-quality predictors from auto labels).
- **Data:** NIST MBE PMI models (the same part exported through several translators: real cross-kernel variants); Fusion 360 Gallery segmentation set (42.9k STEP, including ones that failed to mesh); MFCAD, FabWave; CAx-IF needs membership. Defect injectors: loose re-approximation, unsewing + perturbation, tolerance inflation, pcurve corruption, seam variants, small entities, face deletion, assembly overlaps, precision truncation, IGES/precision round trips. Calibrate them to the ABC failure histogram.
