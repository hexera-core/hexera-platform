# Task 28 report

Base 6427d8de, branch sdd/task-28. Head commit: see `git log -1` (the report commit sits on top of d63ef997 + the test_sites fix).

## COST NOTE (prominent)
The full tier now runs FOUR meshers per verdict. Measured natively (meshpipeline-mesh:native, amd64 under emulation, 10 mm box, warm pool, run_panel over default_members("full")):
gmsh_prod 0.9-1.2s, gmsh_default 0.8-1.0s, gmsh_alt 1.0-1.3s, netgen_occ 1.3-1.5s; total 4.5-4.6s. Netgen is the single slowest member even on a trivial box, i.e. ~30% of the panel's time; on real parts expect the full-tier per-file minutes to rise roughly by one member's share (about +1/3 over the three-gmsh panel). The re-probe VM-hour projection must be re-based on a four-member panel; a box is not representative of real files and I did not measure real ones.
Worst-case wall clock per file is (n+2)T with n=4 = 6T (was 5T for three): a genuine hang costs T then 2T for the one shared retry, plus one T for each of the other three members. At MEMBER_TIMEOUT_S = 300 s that is 1800 s (30 min) pool time worst case per file, vs 1500 s. A per-file budget must be >= 6T plus valid/faithful layers, or the budget must shrink T.

## Changes
1. assemble.default_members("full") = [GmshProd, GmshDefault, GmshAlt, NetgenOCC]; "cheap" stays [GmshDefault].
   Test `test_the_full_tier_contains_a_non_gmsh_member...` asserts by member module (non-gmsh modules == [NetgenOCC]), exact type list, cheap tier unchanged, four distinct configs, hash stable. Not a count test.
   Nothing else hard-coded three (grepped tests/src for GmshAlt, default_members, three-element lists, 5T): no other test needed changing. panel_config_hash already iterates members; existing test shows four configs are distinct, hash differs from the 3-member panel, and changes for each Netgen setting. NetgenOCC().config is present (constructed with no args like gmsh members).
2. Pool-level single timeout with spent budget on `run`'s branch: `test_a_single_pool_timeout_with_the_retry_spent_is_infrastructure_on_the_run_branch` (POOL_TIMEOUT under retry_budget(0) -> error_kind "infrastructure", cause "timeout_retry_spent").
3. argv: `_run_netgen` tested with monkeypatched netgen_python/subprocess.run: argv ends ["10.0","glue"]; with GLUE_TOUCHING_SOLIDS=False and MAXH_DIVISOR=25 ends ["25.0","noglue"].
4. Double-SIGKILL Netgen -> "netgen_crash" (cause kept: sigkill_twice); assertion is now the literal string. Infrastructure kinds (single kill) unchanged.
Minor: test_sites builds the real Verdict via `_real_verdict` (explicit None, False, "cheap"; no production defaults added); native version test asserts both wheels (netgen-mesher, netgen-occt via importlib.metadata inside netgen-python) == NETGEN_PIN; SIGTERM (-15) case -> crashed/infrastructure/signal_15.

## Mutations (each run in test_netgen_member.py, then restored)
- M1 run timeout branch -> constant "netgen_timeout": broke test_a_single_pool_timeout_with_the_retry_spent_is_infrastructure_on_the_run_branch (1 failed).
- M2 drop divisor/glue argv: broke test_the_size_divisor_and_glue_are_passed... and test_turning_glue_off_and_resizing... (2 failed).
- M3 full tier: NetgenOCC replaced by a second GmshAlt (4 members, no Netgen): broke test_the_full_tier_contains_a_non_gmsh_member... (count would have passed).
- M4 SIGKILL kind back to panel constant (gmsh_crash): broke test_a_sigkill_once_is_infrastructure_twice_is_the_files.

## Unexpected
My own first run had a name clash in test_sites (`_verdict` helper shadowed an existing `_verdict`; 42 RecursionError failures). Fixed by renaming to `_real_verdict` before the final runs; not a product issue. Adding Netgen to the full tier broke no existing test.

## Id diff (tests/unit/cad/repair_v4, native image, clone with no v4 in path)
Base 6427d8de: 392 passed, 0 failed. Final head: 397 passed, 0 failed (+5 new tests).
FAILED/ERROR ids sorted: base.ids empty, head.ids empty. `comm -23 head base` (new failures): empty. `comm -13 head base` (fixed on head): empty. `-rs` shows 0 skips on both.
Native tier (tests/native/test_native_repair_v4_netgen.py + _stages.py, meshpipeline-mesh:native): 15 passed, 0 skips, 38 s; slowest: stages unsewn box setup 5.9 s, netgen box setup 5.6 s, face-sharing boxes 3.0 s.
