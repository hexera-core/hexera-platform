# Lane B6 report: are C4's four deep body-overlap declines right?

Branch `sdd/lane-b` at the feature tip `d64e343e`; this lane adds no code. Measured on the `jig` context: one labelled container, `owner=lane-b6`, since removed along with `/srv/work/lane-b6`. No model calls, no VM, no local Docker. Evidence is in `lane-b6/`: per-file JSON plus `overlap_probe.py`.

## Answer
**All four declines are right. None of the four comes out clean even when `cut_by` is forced past every guard.** No change to the fidelity accounting would change a dev-set number on these files, so I made none.

They split into two groups:
- **A: duplicate intent.** It belongs with the AI's overlap-intent judgement, or an owner call on duplicate bodies.
- **B, C, D: correctly declined.** These are real interpenetrations 4–16x deeper than the bound.

Two real findings came out of the measurement:
- the kernel's depth estimate overstates the true depth by 5–12x;
- layer 3 accounts for interior removal asymmetrically.

Neither flips any of these four files; both are below.

## Method (`overlap_probe.py`, per file)
1. Read the file as the engine does. For every pair of overlapping bodies, measure:
   - the overlap volume and area;
   - the **mean thickness** 2V/A;
   - the thinnest **axis-aligned** bbox extent;
   - the thinnest **oriented** bbox extent;
   - each body's share of the overlap.
2. The bound is `min(0.1 mm, 0.001 × diagonal)`, as the kernel computes it.
3. **Forced cut_by.** The real op runs through its normal parent path (`Conformalise.apply` → kernel → `apply_history`, so the declared removals are exactly what the search would carry). The only change is that its guards (`fidelity_bound`, `MAX_VOLUME_CHANGE`) are lifted, in-process, for this experiment only. It runs in both keeper directions on up to 3 pairs, and each result gets the real **full** verdict: layer 1, all four meshers, and layer 3 with the declared edits.

## Per file

| File | What it is | Real thickness 2V/A | Kernel "depth" | OBB thinnest | Bound | Forced cut_by → full verdict |
|---|---|---|---|---|---|---|
| A `00613121_…_003` | **Two identical, coincident bodies** (each overlap share 1.00). The input meshes **4/4**; it is unclean only because of layer-1 `body_overlap` | 37.2 mm | 50.8 | 50.8 | 0.1 | Both directions `changes_body_count`: cutting a body by its twin deletes it |
| B `00521884_…_012` | Real interpenetration of B2/B3: 43 mm³, 18% of B2, 3.3% of B3 | **0.45 mm** | 4.6 | 4.6 | 0.084 | Keeper B2: valid, panel unchanged (1/4); **layer 3 FAILS, `deviation_exceeds_bound` 0.546 mm**. Keeper B3: splits B2 (`changes_body_count`) |
| C `00541957_…_000` | Real interpenetration of B1/B4: 4326 mm³, 13.6% of B1 | **1.6 mm** | 18.9 | 18.9 | 0.1 | Keeper B1: valid (`body_overlap` gone), **layer 3 passes (max dev ~0)**, panel **still 1/4**, so **not clean**. Keeper B4: splits B1 |
| D `trj4_s1-tu-214` | An 11-body assembly with 3 overlapping pairs (B8/B10, B8/B11, B9/B11), each ≤6% of the smaller body | **0.39–0.58 mm** | 5.5–10.0 | 1.4–5.7 | 0.1 | 5 of 6 `changes_body_count`; keeper B11 on B8/B11 leaves the part **invalid** (`body_overlap` x2 remain) |

## The three questions

**1. Real depth and volume.** See the table. Every true thickness (2V/A) exceeds the bound: B by 5x, C by 16x, D by 4–6x. A is a whole duplicated body. **The kernel's `depth = max(2V/A, thinnest axis-aligned bbox extent)` overstates by 5–12x on B, C and D.** The bbox term measures the overlap region's extent, not how far a surface would move. It is a measurement artefact, but it **flips no decision here**: the true thickness is still over the bound on all four.

**2. What the deviation measure reports against the actual surface movement of a cut.**
- **B:** layer 3 reports 0.546 mm, which matches the real geometry: the victim's faces are trimmed back by about the overlap thickness (0.45 mm mean).
- **C:** layer 3 reports **about 0 mm**, although the victim's surface moved by up to about 1.6 mm. The cut removed the victim's interior faces **whole**, so they were declared removed and exempt, and the new faces lie exactly on the keeper's existing surface.
- So the same physical operation, removing interpenetrating volume, scores ~0 when the faces inside the keeper go away whole (C) and ~thickness when they are trimmed (B). **The accounting is asymmetric, and on C it is too permissive, not too strict.**

**3. Should the one-sided rule for declared removals apply here?** Design §7.2 names the exempt removals: **duplicate faces, and slivers narrower than min(0.1 mm, 0.1% of the diagonal)**. §7.1 allows `cut_by` "only when the overlap volume is within the fidelity budget". A cut of 0.45–1.6 mm of interpenetrating volume is neither kind of exempt removal, so under the design as written the rule does **not** apply, and the declines follow §7.1. Making interior removal exempt would be a change to the multi-body semantics, which is the owner's call.

## What this means for the benchmark
- **A** is a duplicate-body file that every mesher already accepts. It is "unclean" only because layer 1 flags `body_overlap` on two coincident copies. The design forbids every repair available (`fuse`, deleting a body, changing the body count). Classify it as **correctly declined (duplicate bodies)**, or route it to overlap intent: an AI or owner decision that a duplicate is a duplicate.
- **B, C, D** are **correctly declined**: genuine interpenetrations beyond the bound. Even with the guards lifted, none reaches clean. B fails layer 3, C still fails 3 of 4 meshers, D splits or stays invalid.
- **Effect on dev numbers: 0 files.** This agrees with C4's expectation.

## Recommendations (not implemented; none changes these four)
1. **The kernel's depth measure** should report what moves. Either use the mean thickness 2V/A, or better, the maximum distance from the victim's removed surface to the keeper's surface (the actual movement), instead of the axis-aligned bbox extent. The current measure would wrongly decline a **thin, inclined** overlap whose true thickness is under the bound.
2. **Layer 3's whole-face exemption** lets a removal of interpenetrating volume pass when whole faces go (C) and fail when they are trimmed (B). If the owner ever widens §7.1, the rule should be made symmetric first. Until then it does not matter, because the kernel declines these cuts before layer 3 sees them.
3. **C4's open owner item stands:** a stratum for "overlap deeper than the bound" (and duplicate bodies), so these are reported as declined rather than counted as failures.
