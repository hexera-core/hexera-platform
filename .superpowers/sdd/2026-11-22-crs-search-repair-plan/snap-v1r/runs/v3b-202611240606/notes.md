## READ FIRST: v3b (STOPPED EARLY, superseded) is the RELAUNCH of v3 on a FIXED commit, on the v2 SPLITS. Not a rerun of v2, and not the first v3 attempt.

| | v3b (this wave) | v3 first attempt (stopped) | v2 |
|---|---|---|---|
| run commit | `e2b25ddf6496f26add2d23f8a10159661f4a1c95` | `323d659f8037b760a32436b6531f38ee99c2b59e` | `5c43220a4ce4d993c12af1a602ad0d5000fcda31` |
| dev split `PROVISIONAL_dev_v2.json` sha256 | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` | same file | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` |
| clean split `PROVISIONAL_clean_v2.json` sha256 | `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` | same file | `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` |
| wave ids | `snap-v3b-v2splits-{dev,clean}-<ts>` | `snap-v3-v2splits-{dev,clean}-202611240544` | `snap-v2-{dev,clean}-202611240348` |
| how it ended | (this doc) | stopped at 06:03Z: search frontier mis-ordered by the A10 regression | stopped early for v3 |

- **The first v3 attempt () was stopped early because its search arms measured a crippled engine**: the A10 layer-3 regression (first bad , fixed in ) left minus-deviation as the only key on the cheap tier, so the frontier preferred the state that had repaired LESS. That run's doc () keeps its rows as evidence of the bug. **v3b carries the fix.**
- **The body-op ceiling no longer applies.** The first v3 attempt could not end any body op clean (Lane C11's empty compound; netgen_occ ). v3b's commit carries C11's fix (native proof: two overlapping boxes, 4 of 4 members, clean), so a  or  output CAN reach clean here, and the 42 body-overlap dev files are measured without that cap.
- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default.
- v3b's code also carries the **rollback_detail crash fix** (v2 ran WITH that crash): see "Measured (file, arm) pairs: this wave vs v2", which separates pairs v2 lost to harness crashes from this wave's.
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): `4c1fc7d5579d` and `e63710eae340` (477 faces, diagonal 437.112 mm, identical face 9); one defect is double-counted in every dev_v2 rate. **Coincident duplicate faces**: in `7e573047c1a6` faces 108 and 109 are coincident duplicates. The split is unchanged.
- Every row carries `located`; every (file, arm) keeps its delivered BREP; search traces are collected per (split, arm, sha12).
- Image mesh build (toolchain) `1d6ed735`; run commit, bind-mounted, `e2b25ddf6496f26add2d23f8a10159661f4a1c95`.
- **STOPPED EARLY: superseded, not wrong.** v3b is a valid baseline of the engine BEFORE the aimed ops: its commit predates `e23af4b3` (contact proposals made valid; aimed `rebuild_face` / `split_angle`) and `4c74661e` (aimed sliver ops). Contact candidates were all rejected here (`remove_duplicate_faces` proposed without params), so the contact family is unmeasured in this run. The aimed-ops wave is **v3c** at `4c74661eb`.
