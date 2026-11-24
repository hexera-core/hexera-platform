# CRS v5: defect-class repair (owner-approved redesign, 2026-11-24)

## Why
v4's general search over ~20 operations stopped scaling. A sweep with up to 2 hours and 3 steps per file found zero extra clean files. Most of the last day's bugs were in the machinery around repair (harness, caches, bookkeeping, agent hand-offs), not in geometry. The real progress came from diagnosing specific defects and writing one targeted repair for each. Example: crossing facets on a near-coaxial shaft and bore, fixed by the aligned split. v5 is shaped like Raymond's simplification work: recognise, repair with a dedicated operation, verify cheap-first, keep or revert.

## Goal and rules (unchanged owner decisions)
- Clean = valid + meshes on all four meshers with their shipped settings (prod gmsh, default gmsh, alt gmsh, Netgen). Faithfulness is reported, never gating. Never tune the meshers to make files pass.
- Safety guards stay: catastrophic-loss refusal (<10% of a body's volume, or a lost body), degenerate-Boolean and inside-out refusal, gaming flags (>25% volume, >10% bbox shrink) reported.
- No merge to main, no deploy, no prod flags. Held-out split sealed; one run only, after a freeze tag.
- Feature first: no full test suites before merges; tiny targeted tests only.
- Count DISTINCT PARTS, not files (dev_v2 contains duplicate geometries).

## Pipeline (one path)
1. Read the input and run the staged healer (as in v4, with today's time fixes).
2. DIAGNOSE: one full check. Every failure becomes a defect with a CLASS, a LOCATION (faces/bodies/points, from layer-1 findings and the meshers' parsed output) and a CAUSE measurement (gap, clash depth, angle, segment count...).
3. For each defect in priority order (validity first, then mesh), apply the class's DEDICATED REPAIR, with parameters computed from the measurement. Verify LOCALLY (touched bodies/region only). Keep it if fixed and nothing new broke; otherwise try the class's next repair (max 2), then mark the defect unrepaired with a reason.
4. One full four-mesher check at the end. Re-diagnose and loop, max 3 rounds.
5. Fallback: if the file isn't clean and time remains, run the v4 AI search (search_ai) from the best state, so v5 is never worse than v4's best.
6. Deliver through the safety guards. Write a readable trace: defect -> cause -> repair -> result.

## AI role (narrow)
- Classify defects the rules can't classify (with a short description), and choose between a class's repairs when the rule is ambiguous. No open-ended search except inside the fallback.
- Cap: $1 per file.

## Defect classes (ordered by distinct parts blocked; each gets an owner)
1. Coarse-facet crossing: near-coaxial/near-tangent curved faces with a tiny gap (shaft in bore). Repair: split_aligned (built). Owner: Lane C.
2. True surface crossings / edge through face (meshers report a point). Repairs: imprint/split along the intersection, unify near-coincident faces, refit a thin wedge. Owner: Lane C (after 1) or Lane D.
3. Overlapping bodies + self-intersection on the same parts. Repairs: fuse with re-heal, fragment and drop slivers, cut chosen by volume; face rebuild from boundary for a self-intersecting face. Owner: Lane A + B jointly (one boundary agreed up front).
4. Low-quality elements (slivers, narrow faces, tiny edges). Repairs: remove_small at measured width, merge sliver into neighbour, collapse tiny edges. Owner: later.
5. Unknown: AI-classified, reviewed weekly into new classes.

## Measurement (frozen, no new tools)
- The fast loop (dev subset) after each merge batch; full dev_v2 wave every few hours, on the two-region launcher.
- Per class: distinct parts fixed / distinct parts in class. Overall: clean distinct parts, regressions (must be 0), time per file, timeouts (target <=5%).
- Baseline to beat: v4's search_ai (39/102 clean files).

## Team
- One lead (integrator, sole merger, reports to coordinator in plain prose).
- Three lanes, one per class group. Lanes message only the lead; always `archductor session send ... --immediate`; never use interactive user questions.
