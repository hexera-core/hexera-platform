## READ FIRST: v3c is the FIRST wave with the AIMED OPS, on the v2 SPLITS. Not a rerun of v2, v3 or v3b.

| | v3c (this wave) | v3b (stopped, superseded) | v3 (stopped, regression) | v2 |
|---|---|---|---|---|
| run commit | `4c74661ebafc6e50872f6bc4253635b9f79300aa` | `e2b25ddf6496f26add2d23f8a10159661f4a1c95` | `323d659f8037b760a32436b6531f38ee99c2b59e` | `5c43220a4ce4d993c12af1a602ad0d5000fcda31` |
| dev / clean split sha256 | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` / `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` | same files | same files | `94e83d177fcd8522d9aabd8185b74affaf00e47b7be54a57e2077e300d3791a8` / `afbb72fe3b6291a689eeaa87a068ccd68e99200045a85eabec1602253bb7eb29` |
| wave ids | `snap-v3c-v2splits-{dev,clean}-<ts>` | `snap-v3b-v2splits-*-202611240606` | `snap-v3-v2splits-*-202611240544` | `snap-v2-*-202611240348` |
| what it can measure | aimed ops: contact family, slivers, rebuild_face / split_angle at named faces | engine before aimed ops (contact candidates all rejected) | crippled search (A10 regression) + body-op ceiling | crash + regression + ceiling |

- **What v3c adds over v3b:** `e23af4b3` (Lane A): the contact row builders now give `remove_duplicate_faces` its params, so contact candidates validate (contacts are 18 of 35 located failures); `rebuild_face` and `split_angle` are aimed at the faces the mesher named. `4c74661e` (Lane D, D18): `unify_same_domain` / `remove_small` aimed at the elongated face.
- Also on this commit: the A10 layer-3 regression fix (`c96fd583`), Lane C11's empty-compound fix (a body op CAN end clean), the rollback_detail crash fix, `located` in every row, the delivered BREP per (file, arm), search traces.
- Clean is the NEW definition (valid + panel); faithfulness reported, never gating. Op budget = the run commit's own default.
- **The dev_v2 split carries ONE PART TWICE** (Lane A, A11): `4c1fc7d5579d` and `e63710eae340`; one defect is double-counted in every dev_v2 rate. **Coincident duplicate faces** in `7e573047c1a6` (faces 108 and 109). The split is unchanged.
- "Measured (file, arm) pairs: this wave vs v2" separates pairs v2 lost to harness crashes from this wave's.
- Image mesh build (toolchain) `1d6ed735`; run commit, bind-mounted, `4c74661ebafc6e50872f6bc4253635b9f79300aa`.
- **STOPPED EARLY - superseded by v4, not wrong.** v3c is the **last pre-anytime baseline**: a wall-stopped search is recorded unmeasured here (v4 delivers and verdicts the best state). It was stopped to release 56 C2D vCPUs that v4, the oracle sweep and the fast loop need.
