# Lane C4: why search_det repaired 0 of 30 in the T2 smoke

## The answer

**`search_det` almost never had anything to try.** On 8 of 13 measured not-clean files, every open site was
`unlocated`, or named no entity. The panel members fail without saying WHERE (`low_quality`, `other`, `no_volume`,
`edge_through_face` with no point, every Netgen failure). A site with no entities gets no deterministic sweep, so the
search ends `exhausted` with 0 candidates. That is the dominant cause. Fixing it is a CHECKER change (locating
failures), so this lane did not implement it; it is the top task below.

The other 5 files had candidates, but none of the candidates could repair them:
- **4 have deep body overlaps**, 4.6 to 50.8 mm against a fidelity bound of 0.08 to 0.1 mm: genuinely interpenetrating
  bodies. No bounded op may repair these under the design's rules (`cut_by` only within fidelity, no `fuse`). Declining
  is the correct outcome.
- **1 is an open shell** whose free loop fills, but `make_volume` finds no closed volume.

The smoke also hid two real bugs and two recording defects (fixed below).

## How this was measured

The smoke's rows predate triage (C2) and C3, so they record no reason. I re-ran the smoke's `search_det` arm on its
exact 30 dev files (Task 43's `PROVISIONAL_dev.json`, inputs from `gs://.../eval/v4-reprobe/inputs`, sha256 verified),
at tip `bbcbed67` plus the C2 and C3 recording, with `--ops 24` and `--h-mm auto` as the smoke used, then ran
`triage.py --per-file`. The run was local and emulated (amd64 on arm64), before the owner's directive moved Docker work
off the Mac. That costs measurement: 25 of 30 files have an answer, and 11 of those were EXCLUDED, mostly emulation
artefacts:
- 3 kernel workers did not start within 30 s;
- 1 shard was OOM-killed at 1.5 GB;
- 4 files hit the 900 s wall;
- 2 `check_valid` timeouts;
- 1 inconclusive panel.

The 13 conclusive not-clean rows are what the ranking stands on. Full per-file evidence is in
`lane-c4/triage.md` and `lane-c4/triage.json`. The 4 overlap files and the open-shell file were then re-run ONE AT A TIME
on the `jig` context with the fixes.

## Causes, ranked by files affected (13 measured not-clean search_det files)

| # | cause | files | status |
|---|---|---|---|
| 1 | **No located failure, so no site entity, so zero candidates.** The gmsh member returns no location for `low_quality` / `no_volume` (judged from the tet list, `mesh_child.judge`), for `other`, and for `edge_through_face` without a point (`mesh_probe.locating_surfaces` returns `()` for it); Netgen never locates. | **8** | TASK (checker) |
| 2 | **Body overlaps the sweep could not touch.** Det proposed only `conformalise(fragment)`, which declines every REAL overlap (`unresolved_overlap`: no keeper), and the search recorded that decline as a DUPLICATE state with no reason. With both fixed, all 4 files decline `cut_by` on `exceeds_fidelity`: overlap depth 4.6 / 5.5-9.5 / 18.9 / 50.8 mm against a bound of 0.084-0.1 mm. | 4 | fixed (decline now recorded with reason; `cut_by` swept). **Expected gain: 0 of 4: these are beyond fidelity by design** |
| 3 | **Surface locations dropped.** A real gmsh member reports `Located.entity` as the face's FINGERPRINT; `find_sites` treated it as a stable id, found it "not live" and dropped it. Its docstring said the member maps tags to stable ids; nothing does, and every unit and native sites test hand-built stable ids, so the real path was never exercised. Separately, exact fingerprint equality fails at half-cell boundaries: 2 of 8 faces of a filleted box. | 1 (the `open_loop` file) | fixed: translation, plus a half-cell-tolerant match |
| 4 | **`remove_small` could never run.** `x-targets.kind == "any"` was compared literally ("F2 is a face, not an any"). The same bug hit `sew` with explicit targets. | 1 | fixed |

The `open_loop` file after the fixes: it now has an `open` site; `fill_loop` is evaluated (score unchanged) and
`make_volume` declines 3x with "no closed volume". It went from 0 candidates to 7, but is still not clean.

## Fixes made (safe: no bound relaxed, no fidelity change, no checker verdict change)

1. `ops/__init__.py` `_check_entity`: `kind "any"` accepts any live entity.
2. `sites.py` `find_sites`: `Located.entity` is translated to live faces through `ws.ids`. Exact match first; on a
   miss, one `sites_geometry` call, and a face also matches the fingerprints its box gives when a coordinate within 1%
   of a cell of a half-cell is rounded the other way. Never further.
3. `search.py`: an op that declines through `diff.params["declined"]` (the body ops) is a DECLINE with its reason, not
   an applied op whose result duplicates the start. This also saves the cheap verdict that was spent on the unchanged
   shape.
4. `search.py` DetGenerator: an `overlap` site sweeps `conformalise(cut_by:<keep>)` both ways on at most 3 body pairs.
   The kernel still refuses an overlap deeper than the bound or a volume change over its cap.
5. Triage recording: `search_triage` now keeps each evaluated candidate's (parent score -> cheap score), up to 8 plus
   the total, and `triage.py --per-file` lists every not-clean search row's evidence.

## Tasks (not done here)

1. **Locate the unlocated panel failures. Expected: up to 8 of 13 files get candidates at all.** This is the only
   lever that touches the majority. Concretely:
   - `low_quality`: report the worst tets' centroids as `xyz` (`mesh_child` already has the qualities). Points resolve to
     faces through `sites_locate_points`. `low_quality` should then map to a conditioning kind (`proximity`/`sliver`),
     not `invalid`.
   - `edge_through_face`: gmsh's message usually carries the point; check why `failure.point` is None on these files.
   - Netgen: report its failing faces (the design asks for them in section 5.6).
   - `no_volume` / `other`: there is no geometry to report. These need a part-wide deterministic sweep for `unlocated`
     sites (conditioning splits at stronger settings, small-entity removal on the soft findings), and that is a search
     design decision.

   All of this touches the checker's `located` output, so a verdict-cache version bump will be needed.
2. **Owner call: deeply overlapping bodies (4 of 13 here, and 10 of the 30 inputs carry `body_overlap`).** They are
   unrepairable within the fidelity bound by design. Either treat overlap deeper than the bound as a DECLINED STRATUM
   (like `face_delete_large`), or revisit the multi-body semantics (assemblies, `fuse` with intent). Today they count as
   failures the search could never have fixed.
3. A whole-part `self_overlap` site (from `self_intersection` whose entities did not resolve, as on `occt/trj3`) names no
   faces, so no sweep applies. Check why layer 1's entity names do not resolve through `ws.indices` there.
4. `make_volume` on the `open_loop` file: after the fill, the shell is still not closed (Task 47's "kernel-discovered"
   case). Look at what else is open.

## Expected effect on dev-set numbers

**From these fixes alone, about 0 files** on the 5 files they target, measured. They are correctness fixes: the search
now sees surface locations, can call `remove_small` and `sew` with targets, and reports body-op declines truthfully.
Task 1 is where repairs can come from.

## Gates

- Unit, on `jig` (`meshpipeline-validation:local`): `tests/unit/cad/repair_v4` + `test_v4_arms` + `test_v4_triage` ->
  802 passed, exit 0.
- Native, on `jig` (`meshpipeline-mesh:native`), the located-fingerprints, det-coverage, sites, waste, patience,
  ops_bodies, ops_shells and open_shell_candidates tests -> 52 passed, 1 skipped, exit 0.
- Mutation:
  - the half-cell match switched off -> the unit test fails, and the native test names exactly F1 and F6 missing (6 of
    8 exact);
  - the fingerprint translation removed -> the unit test fails;
  - the decline handling and the `cut_by` sweep were written test-first and failed before the code.
