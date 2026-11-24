# Lane B8 report: the cheap tier was blind to gmsh_prod

Branch `sdd/lane-b`, base `288755c2`, which contains `32402751`. Native measurement and the native gate ran on `jig`, one labelled container at a time (`owner=lane-b8`). No model calls, no VM, no local Docker. Evidence is in `lane-b8/`:
- per-file `*.b8.json`;
- `cheapfull.py`, the native measurement;
- `v1_inputs_agreement.py`, the agreement from v1's recorded full results.

**Hard constraint held.** The full panel (`default_members("full")`), `clean_of` and every full verdict are unchanged, and a test pins the full panel.

## 1. Which member failed, and why the cheap tier missed it
**All of snapshot v1's cheap-pass/full-fail states failed on gmsh_prod alone.** That is the 11 in dev and 1 in clean. They come from three files:

| File | States (v1) | Failing member | Why the cheap tier passed it |
|---|---|---|---|
| `nist_ctc_01_asme1_rd.stp` (AP203 geometry only) | 5 | **gmsh_prod `low_quality`** (min SICN 0.008-0.009 < 0.01), on the INPUT and on every child | gmsh_default meshes it at SICN 0.018-0.021, a pass |
| `nist_ctc_01_asme1_ap203.stp` (AP203 with PMI) | 6 | the same | the same |
| `00614313_…_003` (clean control) | 1 | **gmsh_prod: "HXT 3D mesh failed"** on the pre-B5 staged state (the `condition` stage) | gmsh_default improved, at SICN 0.091 |

**Why.** The cheap tier was layer 1 plus **gmsh_default only**. gmsh_prod (2D Frontal-Delaunay, **3D HXT**, element size a tenth of the diagonal) is the panel member that fails most often: 27 of 38 v1 inputs. On a file where only gmsh_prod fails, the cheap verdict cannot tell anything apart, so the search climbs states the full panel will reject.

Both NIST files fail gmsh_prod already **at input**, and no op the search tried changed that. Every confirmed state on them was therefore a guaranteed miss.

## 2. The fix: the cheap tier meshes gmsh_default AND gmsh_prod
- `verdict.assemble.default_members("cheap")` is now `[GmshDefault(), GmshProd()]`, the full panel's own two members with identical configs.
- A cheap failure is therefore always a full failure. Adding a member can only remove cheap-pass/full-fail disagreements; it can never create cheap-fail/full-pass ones (test-pinned).
- `arms.cheap_full_cells` now counts a cheap PASS as **every** cheap member passing, using the cheap tier's own member count. Before, the rule was `>= 1` passing member, which a two-member tier would have miscounted.
- The cheap verdict still never certifies clean (`clean_of` requires the full tier).
- The verdict cache keys the cheap tier's members (`panel_config_hash`), so old cheap entries simply miss. No version bump is needed, and full-tier entries are untouched.

### Alternatives measured and rejected
- **Adding gmsh_alt or Netgen instead:** neither fixes a single miss, in v1's 38 inputs or natively. Both pass on every miss state.
- **A tighter SICN threshold on gmsh_default:** no threshold separates the cases. gmsh_prod fails on states where gmsh_default reads **0.178** (`00619793`), and fully clean files read gmsh_default **0.021-0.022** (`00611614`, `00616927`). Any threshold that caught the first would fail the second.
- **gmsh_prod instead of gmsh_default:** this misses the gmsh_default-only failure (`00614908`, 1 of 38).

## 3. Agreement before and after (cheap tier vs full panel)

| State set | Before: layer 1 + default | After: layer 1 + default + prod |
|---|---|---|
| v1's 38 inputs (dev + clean; from v1's recorded per-member full results) | 35/38: 9 pass/pass, 26 fail/fail, **3 cheap-pass/full-fail** | **38/38**: 9 pass/pass, 29 fail/fail |
| **Native re-judge on jig**: 22 states over 9 files (inputs, the cheap-only staged state, and the children v1's search_ai tried: unify_same_domain, tighten_tolerances, split_angle where valid, run_stage condition) | **6/22**: 16 cheap-pass/full-fail, every one gmsh_prod | **21/22** |
| v1's confirmed search states (dev 15, clean 1) | dev 4/15, clean 0/1 | **expected 15/15 and 1/1.** Those states are not stored. The native re-judge regenerated the same files' states, and gmsh_prod fails on all of them. The 4 that already agreed were cheap fails, and adding a member cannot flip one |

**The one remaining native miss** is `00611852_…_008`: gmsh_alt fails at min SICN 0.004, against a 0.01 pass line. v1 recorded **this same input as clean**, so this is one mesher flapping at its pass threshold between runs (lane D's panel-reproducibility subject), not a cheap-tier blind spot. Adding gmsh_alt to the cheap tier would not make a flapping verdict reliable.

## 4. Cost per cheap verdict (native, 22 states)

| Member | Mean | Median | Max |
|---|---|---|---|
| gmsh_default | 0.34 s | 0.41 s | 0.49 s |
| **gmsh_prod (added)** | **1.11 s** | 1.58 s | 2.84 s |
| gmsh_alt | 0.67 s | | 1.70 s |
| netgen_occ | 2.53 s | | 5.16 s |

The cheap verdict's meshing goes from **~0.34 s to ~1.45 s per state, about +1.1 s**, against ~4.65 s for a full verdict's four members. Layers 1 and 3 are unchanged by this and were not re-timed; they dominate a verdict's wall time on large files, so the relative increase in a whole cheap verdict is smaller than the meshing ratio. The staged healer's per-stage transactions now also see gmsh_prod, so B5's full-panel confirmation will fall back less often.

## Snapshot impact
- search_ai and search_det **rank** with gmsh_prod visible. On files where gmsh_prod is the only failing member, cheap-pass states now score below states that fix gmsh_prod.
- Expect fewer wasted confirmations, and fewer rounds spent climbing toward panel-rejected states.
- **Clean counts can only move where an op actually fixes gmsh_prod.** On both NIST files no tried op does: gmsh_prod fails at input and after every op.

## Gates
- **New tests in `test_cheap_tier.py`, red on base:**
  - the cheap tier is `[gmsh_default, gmsh_prod]`;
  - it is a strict subset of the full panel with identical configs;
  - the full panel is unchanged;
  - a state where only gmsh_prod fails now scores below one where both pass, and is never clean;
  - the agreement cells count a cheap pass only when every cheap member passes.
- `test_netgen_member.py` updated to the new cheap list.
- **Native on jig:** `pytest tests/native -k repair_v4` gives **223 passed, 1 failed**. The failure is `test_repair_v4_is_callable_from_a_script_without_a_main_guard`, which **fails identically on base** (re-run on jig against `HEAD` without this change).
- **Host:** `pytest tests/unit/cad/repair_v4 tests/unit/dev`, head against base `288755c2`: base 2100 passed / 39 failed; head 2104 passed / 40 failed.
  - The one head-only failure was `test_cheap_full_cells_reads_the_confirm_events`. Its fixture encoded "ONE cheap member passing is a cheap pass", the old one-member assumption; it now uses "every cheap member passes", with the same intent and the same four cells.
  - After that fix, that test and the new tests pass (84 in `test_v4_arms.py` + `test_cheap_tier.py`). The remaining 39 failures are base's own (host /tmp-path).
- ruff on the changed files: clean.
