## READ FIRST: the THIRD wave on the v2 SPLITS, at its OWN commit. NOT a rerun of v2.

| | this wave (v3) | v2 |
|---|---|---|
| run commit | `323d659f8037b760a32436b6531f38ee99c2b59e` | `5c43220a4ce4d993c12af1a602ad0d5000fcda31` |
| dev split `PROVISIONAL_dev_v2.json` sha256 | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` |
| clean split `PROVISIONAL_clean_v2.json` sha256 | `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` | `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` |
| WaveDefs | `snap_v2_dev` / `snap_v2_clean` (they name the SPLITS, not the wave) | same |
| wave ids | `snap-v3-v2splits-{dev,clean}-<ts>` (in every PROVENANCE, ledger and row job id) | `snap-v2-{dev,clean}-202611240348` |

- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default (Lane D: ops never bind).
- v3's code carries the **rollback_detail crash fix** (v2 ran WITH that crash), Lane D's gmsh_prod padding fix and the power-of-two quantum fix, aimed and reordered defeature, Lane C's no-op decline, whole-part recognised defeature off by default, per-file search traces, located faces in every row, and every (file, arm)'s delivered BREP.
- **More measured (file, arm) pairs here than in v2 is partly the crash fix, not repair ability**: see "Measured (file, arm) pairs: this wave vs v2".
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): `4c1fc7d5579d` and `e63710eae340` are the same part (477 faces, diagonal 437.112 mm, identical face 9). One defect is double-counted in every dev_v2 rate, v2's and v3's alike. The split is unchanged so v3 matches v2.
- **Coincident duplicate faces** (Lane A, A11): in `7e573047c1a6` faces 108 and 109 are coincident duplicates (identical area 9032.0789 mm2, aspect 230:1); a mesher naming both is naming two copies of one face.
- Image mesh build (toolchain) `1d6ed735`; run commit, bind-mounted, `323d659f8037b760a32436b6531f38ee99c2b59e`.
- **KNOWN CEILING ON THIS COMMIT (Lane C11): no body op can end clean.** Every `cut_by` / `fuse` output carries an empty compound that makes netgen_occ fail (`Bnd_Box is void`); stripped, the same output goes 3/4 -> 4/4 clean. The 42 dev files whose input carries body overlap are capped below clean in every repairing arm; the per-arm tables carry this caveat inline. An overlap-only mini-wave on the fixed commit follows.
- **STOPPED EARLY — the search arms measured a crippled engine.** The A10 layer-3 regression (first bad `ac7fbf52`, fixed `c96fd583`) mis-ordered the search frontier (minus-deviation was the only key on the cheap tier, so the least-repaired state won). This run's rows are kept as evidence of the bug; the comparison the wave exists for is in **v3b** at `e2b25ddf6496`. input_only, staged and the clean controls are unaffected.
