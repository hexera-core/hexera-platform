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
1. Coarse-facet crossing: near-coaxial/near-tangent curved faces with a tiny gap (shaft in bore). Repair: split_aligned (built).
2. True surface crossings / edge through face (meshers report a point). Repairs: imprint/split along the intersection, unify near-coincident faces, refit a thin wedge.
3. Overlapping bodies + self-intersection on the same parts. Repairs: fuse with re-heal, fragment and drop slivers, cut chosen by volume; face rebuild from boundary for a self-intersecting face.
4. Low-quality elements (slivers, narrow faces, tiny edges). Repairs: remove_small at measured width, merge sliver into neighbour, collapse tiny edges.
5. Unknown: AI-classified, reviewed weekly into new classes.

## Measurement (frozen, no new tools)
- The fast loop (dev subset) after each merge batch; full dev_v2 wave every few hours, on the two-region launcher.
- Per class: distinct parts fixed / distinct parts in class. Overall: clean distinct parts, regressions (must be 0), time per file, timeouts (target <=5%).
- Baseline to beat: v4's search_ai (39/102 clean files).

## Owner decisions 2026-11-24 (after the v4 post-mortem)
Full design, post-mortem, prior art and predictions: Claude Doc "CRS v5 - Defect-Class Repair: Design".
- Build plan adds four levers (target 70-80% of dev files clean, v4 search_ai 27%):
  1. Every file finishes: parallel panel members, wall-aware panel start, "checker failed" is an outcome not a defect.
     Large parts get a larger budget: file wall and per-mesher timeout scale with part size, up to 3x base (rule set
     from the paired baseline's wall-vs-size data).
  2. Conformal assembly pass before any class repair (Cubit order): misalignment check, fuzzy General Fuse imprint at a
     tolerance stepped from measured gaps and capped below the smallest real gap, overlap check, Boolean by volume.
  3. Reliable coarse-crossing repair: gap-vs-sag test at shipped settings, shared-angle split with segments from the
     measured cause; extend from coaxial pairs to any close non-adjacent pair.
  4. Sliver merge: thresholds relative to shipped mesh size (e = 10% of it), merge/collapse as real topology edits,
     deviation measured.
- Seven classes (replace the list above): close_faces (was coarse-facet crossing; any close non-adjacent pair the
  facet sag can bridge), overlap_self_intersection, surface_crossing, low_quality, open_shell_volume (open_shell,
  no_volume, open_loop), invalid_brep_tolerance (brep_invalid, pcurve/SameParameter, poles), closed_periodic_face
  (periodic_surface). `checker_failed` (crash/timeout with no location) is an outcome, not a defect. `unknown` = an
  unlocatable failure only; the AI DESCRIBES unknowns for weekly review and never assigns a class.
- Per-defect tournament in the judgement classes (overlap/self-intersection, surface crossing, open shell): <=6
  entrants (rule repairs always, agent <=3 from the class menu with a defect-local brief, retrieval later), heats on
  copies in parallel (applied -> local validity -> local mesh -> rank by fixed, no new failures, deviation, volume),
  $0.15/defect, $1/file. Every match logged; a source that doesn't beat the rule in a class is switched off.
- HORIZONTAL COVERAGE: every known defect kind (v4 layer-1 codes, mesher kinds, prior-art kinds) has a detector, a
  class, a rule repair and an injected test case - before it is seen on dev. Coverage matrix in the design doc. New
  work: detectors for duplicate body, duplicate faces, floating/tiny solid, badly parameterised spline, units/scale;
  ~12 new injectors in devtools/validation/v4_eval/inject.py (shaft-in-bore, thin wall, misalignment, duplicate body,
  self-intersecting face, edge through face, duplicate faces, floating solid, no volume, non-manifold, inside-out,
  unbounded, pole, units); promote the 4 quarantined injectors once a mesher probe fails on them. Each class step
  ships its kinds' detector + injector + repair + test; class gates count injected cases as well as dev parts.
  Every layer-1 code maps to a non-unknown class (test-enforced); boolean_failed is checker_failed.
- TOURNAMENT FOR EVERY DEFECT (supersedes "judgement classes only" and "max 2 repairs per class"): the class sets the
  menu, parameter rules and the rule seed; the rule seed always enters and wins ties, so never worse than the rule.
  Entrant tiers, <=6: rule seed, class menu + parameter variants (free), retrieval (later), agent (only when no free
  entrant wins, or a small random exploration share; $0.15/defect, $1/file). Entrants scale with part size (OOM risk:
  small parts parallel, largest sequential in tier order with early stop).
- Walkthrough decisions: class loop 50% of wall; fallback always runs; max 3 rounds; whole-part close-pair scan;
  thresholds calibrated on the 32 close-face files; units rescale only when header and geometry clearly disagree
  (reported), else report only; units_changed/round_trip_invalid -> invalid_brep_tolerance, orientation_unchecked ->
  checker_failed.
- Build order (13 steps): baseline; one Defect model; tournament runner + applied-or-why + wire the 7 unaimable ops
  (sew, make_volume, orient_and_close, trim_unbounded, tighten_tolerances, rebuild_edge_as_intersection, split_closed)
  and fill_loop's edge aim; every file finishes; quick wins (closed/periodic face, invalid/tolerance); conformal pass;
  close faces; overlap/self-int; surface crossing; low quality; open shell/volume; agent entrants; full wave + freeze.
- Tournament details: rank = defect gone > no new failures on touched bodies > least deviation > least volume change
  > rule seed > cheapest. Defects run one tournament at a time in priority order, each from the previous winner's
  state (parallel only within a tournament). Agent also enters on a 10% exploration share of defects free entrants
  already solved (~$0.02/file).
- GATE CHANGE (owner, 2026-11-25): per-class gates for steps 5-11 are DEFECT-level - high local fix rate on the
  class's dev defects, injected cases clean, zero regressions vs input. Clean FILES are judged only at step 13 (all
  classes + fallback on), because most dev files carry defects from several classes.
- LOSS GUARD on self-intersecting solids (owner, 2026-11-25, option A): the rule (keep >= 10% of part and of each
  body) is unchanged; only the MEASUREMENT changes. When layer 1 says an input body self-intersects, its kept volume
  is not taken from Common(body, output) (OCCT Booleans return ~0 on self-interfering arguments - b25999e0f8be:
  "kept 0.0" on a 1e-5 change); instead tessellate both and compare volumes plus interior point sampling. Built in
  step 8.
- UNITS (owner, 2026-11-25, option A): detect and REPORT a suspected unit mismatch only; never rescale. The `rescale`
  op leaves every menu (injected wrong-unit parts mesh clean anyway; a wrong rescale silently corrupts real size).
  Supersedes the earlier "rescale only on clear header mismatch" rule.
- LOSS GUARD COST (owner, 2026-11-26): the guard never gets a timeout that lets a repair through unmeasured; it is
  made CHEAP instead (changed bodies only, per-file cached input data, exact screens before any Boolean). The 10%
  rule is unchanged. Every phase of a file must finish within a bounded share of the file wall ("everything resolves
  in reasonable time").
- Round 2 (2026-11-25/26) added: facet_crossing_local class (+ split_turn, cut_knife), rebuild_boundary_edge,
  rebuild_seam, robust Booleans + drop_sheet (owner-approved), narrow-region low_quality, fallback guaranteed
  search_ai's whole search wall, streaming event log + events_query.py.
- GAP CLOSING (owner, 2026-11-26): v5 MAY close a gap between two faces/bodies when the gap is smaller than the
  fidelity bound (snap/glue the faces so they touch or share topology). Moved distance reported as deviation; loss
  guard and gaming flags still apply. Supersedes the conformal pass's "fuzzy capped below the smallest real gap".
  Evidence: 44 of 93 unclean round-3 files have a face pair at a median 1.1% of the element size; splits cannot fix it.
- DEFERRED: local re-surfacing (approximating patch) of unrepairable regions; revisit only after the full paired wave.

## Build
- One chat builds v5 end to end, one class at a time, in small reviewable commits. No lanes, no lead/merger, no agent hand-offs (owner, 2026-11-24: the multi-lane loop produced bloat and duplicate machinery).
- One `Defect` model and one class list; nothing lands that duplicates an existing v4/v5 mechanism.
