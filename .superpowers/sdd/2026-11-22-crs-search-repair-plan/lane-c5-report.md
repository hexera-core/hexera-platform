# Lane C5: the checker locates the failures it used to leave unlocated

## Headline

- **Location only, proved.** The full verdict, uncached, on all 30 smoke dev files, pre-C5 (`152d2d59`) vs post-C5
  (`7567f989`), is **30/30 field-by-field equal**:
  - per member: status, error_kind, min_sicn, elements;
  - per file: clean, tier, inconclusive, valid (ok / hard / soft / solids) and faithful (ok / max_dev_mm / reasons).

  Only `located` changed: a failing member gained locations on **24 of 30** files.
- **The search now has something to aim at.** On the 8 files C4 found with no located site, `search_det` proposes
  candidates on **7 of the 7 measured** (the 8th, 0570551, was still running). Before C5 it proposed 0 ops on every one.
  Across the 13 conclusive not-clean files, ops proposed went **35 -> 149** and verdict candidates **8 -> 38**.
- **Clean count: 0 -> 0.** Nothing new repairs yet. The candidates are now real, but none of them makes these files
  mesh on all four members. That is the next question, and it is a different one from "the search had nothing to try".

## What changed (all merged as b396d645, plus follow-ups on sdd/lane-c)

1. **gmsh.** The 3D back-ends print the failing triangles' NODE ids to C-level stdout/stderr, not to the exception or
   the logger: HXT's `el1 = np = 3 a b c`, and TetGen's `Found two duplicated facets ... [a, b, c] #s`. `mesh_child`
   now captures both descriptors around `generate()` and turns each node into its surface (the box fingerprint
   `find_sites` already translates) plus a point.
2. **low_quality.** A low-quality mesh is located at its 3 worst tets' boundary nodes.
3. **Netgen.** "...could not be built for face N" is located by that face's box. "Meshing failed!" names nothing and
   stays unplaced.
4. **What gmsh cannot locate.** gmsh_default's TetGen PLC prints no point at all, and `getLastEntityError` /
   `getLastNodeError` are empty, so that member alone stays unplaced. On the same files gmsh_prod (HXT) now locates,
   and sites cluster across members.
5. **Coarse fallback.** A failure no member placed becomes a COARSE site: the whole part (its bodies), the members'
   kind, a reason, and the part's small-entity findings. A layer-1 finding naming only a body is coarse too. Det sweeps
   part-wide conditioning (angle 60 / 45, continuity C2, `remove_small` on exactly the reported small entities) ONLY
   when nothing is placed. Coarse sites are not holes for patience. `low_quality` maps to `proximity`.
6. **Locating is best effort.** An exception while locating leaves the answer unlocated and never changes it.
7. **Verdict cache version** bumped (`located` is part of the cached panel result). LEAD-2's merge resolution made it
   `.3`.
8. **Follow-up (this branch, d58d34b5): the owner's overlap ruling.** Every row's triage carries the INPUT's overlap,
   measured by `body_kernel._cut_depth` (the measure `cut_by` declines on), both cuts per pair:
   - `triage.overlap.max_depth_mm`: the worst pair's cheaper cut;
   - `triage.overlap.method`: measured, or mean_thickness;
   - `triage.overlap.bound_mm`: the fidelity bound;
   - `triage.overlap.within_bound`;
   - `triage.overlap.diag_mm`;
   - `triage.overlap.pairs[]`, each with `a_by_b` / `b_by_a` {depth_mm, method, volume_mm3}, `required_mm` and
     `required_method`.

   `null` means the input has no body overlap; `{"error": ...}` means the measurement failed, and the row survives.
   Either cut of the benchmark can be computed from rows.

## Op-cost pins

Task 47's 1-hole cost is unchanged (5 ops, 4 verdict candidates). The 3-hole floor moved from 14 to 13 by ORDER alone:
the coarse `open` site sorts first, so `make_volume` comes before the last `orient_and_close`. The same 14 candidates
run at budget 14. Re-pinned: 12 is not enough, 13 is.

## Verdict equality (30 dev files, VM dev-crsv4-lanec5-vq, crsv4-mesh@sha256:633e1c3b, the same digest as jig)

A VM rather than jig, per the owner rule that multi-file runs go to cloud VMs. Raw:
gs://dev-crsv3-transfer-224734058693/eval/lane-c5v/results/results.tgz

```

30 files field-by-field EQUAL (status, error_kind, min_sicn, elements per member; clean, valid, faithful); 0 DIFFER; 0 uncomparable
files where a failing member gained locations: 24
  LOC 11083_58c56432786ec50fe9e0d3ad_step_002.step ['gmsh_alt:0->5', 'gmsh_default:0->3', 'gmsh_prod:0->5']
  LOC 11852_673cbf4282be88b3374992cb_step_008.step ['gmsh_alt:0->5']
  LOC 14908_a8619514c95972d6221f4629_step_000.step ['gmsh_default:0->3']
  LOC 19793_973db45bf9076901a50908bc_step_000.step ['gmsh_prod:0->2']
  LOC AP203 geometry only/nist_ctc_01_asme1_rd.stp ['gmsh_prod:0->5']
  LOC s/AP203 with PMI/nist_ctc_01_asme1_ap203.stp ['gmsh_prod:0->5']
  LOC MI-STEP-Files/nist_ftc_07_asme1_ap242-e2.stp ['gmsh_alt:0->4', 'gmsh_prod:0->4']
  LOC 19158_daf80b2021249a203e938429_step_009.step ['gmsh_prod:0->2']
  LOC 19727_589dc8aa5abe5e0fe4ada930_step_014.step ['gmsh_alt:0->6', 'gmsh_default:0->4']
  LOC 21884_f7d1e4b9658237600ec82613_step_012.step ['gmsh_prod:0->1']
  LOC 28484_58a256af264a260f6f931a1c_step_003.step ['gmsh_prod:0->1']
  LOC 35066_bb0bc8e7914fdea894ad2ec6_step_009.step ['gmsh_prod:0->1']
  LOC 41957_51229e00ebc25e88ce807b9e_step_000.step ['gmsh_alt:0->3', 'gmsh_prod:0->1']
  LOC 47929_58ab27a87442550f6daa230f_step_014.step ['gmsh_prod:0->3']
  LOC 50171_58ac32b095b047102fd8ce8c_step_013.step ['gmsh_prod:0->3']
  LOC 73880_417ab441d502eafedde01127_step_005.step ['gmsh_prod:0->1']
  LOC 74245_58b65964d2d73c0f795da35a_step_017.step ['gmsh_alt:0->5', 'gmsh_prod:0->1']
  LOC 01268_01579e613800b230c1716e80_step_001.step ['gmsh_prod:0->2']
  LOC 04433_58c21d4ea0c27b0f87b161a9_step_000.step ['gmsh_alt:0->5', 'gmsh_default:0->3', 'gmsh_prod:0->3']
  LOC 06843_58c2e4ec85eca50f9755deb9_step_000.step ['gmsh_alt:0->3', 'gmsh_default:0->4']
  LOC 08014_3fedd89a1d6b0c6f1c34e1b7_step_001.step ['netgen_occ:0->1']
  LOC 14327_760137e15b9b924e8339e428_step_017.step ['gmsh_alt:0->3', 'gmsh_prod:0->1']
  LOC 15999_4f1d2ad2e4e729a2f1836480_step_000.step ['gmsh_prod:0->3']
  LOC occt/trj3_s1-db-214.stp ['gmsh_prod:0->1']
```

## search_det before (pre-C5) vs after (C5), 13 conclusive not-clean smoke files (VM dev-crsv4-lanec5-ba)

Sites are counted as located / coarse / unlocated at the search's stop. The "before" column marks non-unlocated sites
with `?`, because pre-C5 traces did not record whether a site named an entity. Outcome is reason and panel in->out.

| file | sites located/coarse/unlocated (before -> after) | ops proposed (before -> after) | verdict candidates (before -> after) | outcome before | outcome after | ops after |
|---|---|---|---|---|---|---|
| 13121_58c67ddf032617103d59c101_step_003.step | 1?/0/0 -> 0/1/0 | 4 -> 14 | 0 -> 3 | exhausted 4->4 | exhausted 4->4 | conformalise 8, split_angle 4, split_continuity 2 |
| AP203 geometry only/nist_ctc_01_asme1_rd.stp | 0?/0/1 -> 1/0/0 | 0 -> 12 | 0 -> 4 | exhausted 3->3 | exhausted 3->3 | split_angle 6, split_by_proximity 6 |
| s/AP203 with PMI/nist_ctc_01_asme1_ap203.stp | 0?/0/1 -> 1/0/0 | 0 -> 12 | 0 -> 4 | exhausted 3->3 | exhausted 3->3 | split_angle 6, split_by_proximity 6 |
| 19158_daf80b2021249a203e938429_step_009.step | 0?/0/1 -> 1/1/0 | 0 -> 4 | 0 -> 2 | exhausted 0->1 | exhausted 0->1 | orient_and_close 2, split_continuity 2 |
| 21884_f7d1e4b9658237600ec82613_step_012.step | 1?/0/1 -> 1/1/0 | 4 -> 7 | 0 -> 0 | exhausted 1->1 | exhausted 1->1 | conformalise 6, remove_duplicate_faces 1 |
| 26988_58a1d33fd3d9a2108b54be6d_step_000.step | 0?/0/1 -> 0/1/0 | 0 -> 4 | 0 -> 1 | exhausted 1->1 | exhausted 1->1 | orient_and_close 1, split_angle 2, split_continuity 1 |
| 41957_51229e00ebc25e88ce807b9e_step_000.step | 1?/0/1 -> 1/1/1 | 4 -> 8 | 0 -> 1 | exhausted 1->1 | exhausted 1->1 | conformalise 6, orient_and_close 1, remove_duplicate_faces 1 |
| 47929_58ab27a87442550f6daa230f_step_014.step | 0?/0/1 -> 1/0/0 | 0 -> 21 | 0 -> 2 | exhausted 1->1 | exhausted 1->1 | orient_and_close 3, rebuild_edge_as_intersection 6, split_continuity 6, to_canonical 6 |
| 50171_58ac32b095b047102fd8ce8c_step_013.step | 0?/0/1 -> 1/1/0 | 0 -> 3 | 0 -> 1 | exhausted 0->0 | exhausted 0->0 | orient_and_close 1, split_continuity 2 |
| 70551_59995fb3cd27b48fe0957ae2_step_007.step | 0?/0/1 -> - | 0 -> - | 0 -> - | exhausted 1->1 | EXCLUDED ? | - |
| 08014_3fedd89a1d6b0c6f1c34e1b7_step_001.step | 1?/0/1 -> 1/1/0 | 7 -> 11 | 4 -> 7 | exhausted 0->0 | exhausted 0->0 | fill_loop 1, make_volume 4, orient_and_close 6 |
| occt/trj3_s1-db-214.stp | 1?/0/1 -> 1/1/0 | 0 -> 30 | 0 -> 9 | exhausted 1->1 | ops 1->1 | conformalise 8, rebuild_face 8, refit_cluster 2, split_angle 12 |
| occt/trj4_s1-tu-214.stp | 1?/0/1 -> 0/2/0 | 16 -> 23 | 4 -> 4 | exhausted 1->1 | exhausted 1->1 | conformalise 16, remove_duplicate_faces 1, remove_small 3, split_angle 2, split_continuity 1 |

TOTAL ops proposed 35 -> 149; verdict candidates 8 -> 38; clean 0 -> 0

0570551 was still on its last file when this was written; its row follows separately. Raw:
gs://dev-crsv3-transfer-224734058693/eval/lane-c5/results/ (uploaded by the VM when it finishes).

## What this says

Locating was the right lever for "the search has nothing to try": that cause is gone. Clean stays 0 because the
candidates available on these files (angle and continuity splits, proximity splits, orient, conformalise, edge
rebuilds) do not make all four members mesh. Several of these files have deep body overlaps (C4) or a gmsh_prod
quality floor that splits do not lift. The next diagnostic is per candidate: which member still fails after each
evaluated candidate. The triage deltas and per-file evidence carry that.

## Gates

On jig (meshpipeline-mesh:native), after rebasing onto 3661d8cf:
- `tests/unit/cad/repair_v4`, `test_v4_arms`, `test_v4_triage`, `test_v4_report` and the new native
  `test_native_repair_v4_overlap_depths` -> 1005 passed.
- Earlier, on the C5 commits: native panel / netgen / sites / located_fingerprints / det_coverage / waste / patience /
  end_to_end / zero_tet / judge_cache / open_shell / ops_shells -> 73 passed, 3 skipped, 1 failed
  (`test_repair_v4_is_callable_from_a_script_without_a_main_guard`; Task 47's report lists it as failing on base too,
  not re-run at base here).
- ruff and mypy are clean on every changed file.
