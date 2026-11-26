# Why the AI CRS adds +1–2 files: diagnosis summary (2026-11-22)
Source: the record benchmark (bench-record-242c2710) AI arm, 568 investigator regions over 103 files; code at a3225988; lane chats. Raw data: scratchpad/benchrec/.

## Measured
- All 10 AI-arm fixes came from resolve_sub_mesh_gap. The AI's marginal over the pass alone is +2/−1.
- Region endings: 74% not_reached, 78 budget_spent, 40 attempted_none_held, 12 package, 8 mesher_side, 9 runaway.
- Edits: 783 total. 607 rolled back (329 target_remains, 165 not_isolated), 101 refused, 37 kept.
- Model: gpt-5.6-luna, about 1.9 s per call, $1.71 for the whole arm (about $0.014 per file). 71% of reached-region time went to tools.
- Images were off: the model asked for a view 148 times and got "off" 143 times.
- The scoped tier-1 probe passed 138/149 times; the whole-part tier-2 probe failed 64/82. The cheap signal mostly lies.

## Root causes, ranked
1. **Clone and delivery disagree.** Clone-freedom lets an edit cure the part on the scratch copy. Then validate_package (clone=False) refuses it with op_does_not_answer. 17 files had a mesh-curing clone edit; 11 had their package refused; 7 ended unmeshable. Cheapest win (3–7 files).
2. **The action space doesn't match the failures.** Edits are only {op, fingerprint}, with no parameters and no geometry authoring, over the same 13-op kernel catalogue the rules use. There is no op for unlocated self-overlap (33 files), clash faces outside every region (13), real intersections the kernel can't see (16), folds, or gaps beyond the split. Blocks about 60–80 files.
3. **The observations and reward are wrong.** The model sees kernel defect records, not gmsh failure geometry, adjacency, or feature size vs element size. The keep test asks whether the fingerprint is gone, not whether the part meshes. Amplifies cause 2.
4. **The unit of work is wrong.** Regions are kernel-defect box clusters (up to 314 fingerprints). The untried-defect rule forces aims at warnings and contradicts the prompt; 46 of 78 budget_spent regions were blocked by it.
5. **The AI is boxed in structurally.** The pass takes the one working op first, the tournament ties go to rules, and the catalogue and gates are the same. The marginal is capped at about 1–3.
6. **No learning and a weak model.** Each region starts a blank conversation, memory is not wired, review reasons are withheld, and the model is small (runaways, malformed edits).
7. **The objective and corpus were mismatched** (diag/40 probe; hint sizing not credited).

## Requirements for an AI that helps (against clean STEP)
1. The reward is the metric: valid + meshes on the panel + faithful, measured on the exported STEP, and the model sees the result after every candidate.
2. What passes on the clone ships, subject only to the safety gates (no catalogue-advice gate).
3. The unit of work is a panel failure site mapped to faces and edges; kernel warnings matter only at those sites.
4. Remodelling ops with model-chosen parameters (defeature, merge or replace coincident faces, thicken or collapse thin walls, refit a self-overlapping surface, split at a chosen density, unify same-domain faces, re-fragment), or a constrained geometry scripting surface. Fidelity, not op identity, bounds what's allowed.
5. The model sees the failure: mesher messages with coordinates, clash triangles, distance and thickness vs element size, the adjacency graph, surface types, and rendered views.
6. Budget follows failure sites; candidates run in parallel on independent clones; use a stronger model.
7. Deterministic code goes first; the AI owns the remainder, and candidates compete on the measured result.
8. Learning: within a file across sites, plus a verified recipe memory keyed by failure signature, plus structured review reasons.
9. The corpus is defined by the metric.
