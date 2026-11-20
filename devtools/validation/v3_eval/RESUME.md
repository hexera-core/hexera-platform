# CRS v3 ship: RESUME (single source of truth; every lane appends its status below)

## OWNERS (since 2026-11-19 ~22:45 PT)
- **LEAD** (the original chat): integrates, owns this file, launches the AI arms on lane A's GO, resolves conflicts.
- **Lane A, 0-packages fix:** the investigator's 0 tool calls / 0 packages. It has its own worktree, pushes to crs/v3-agentic, and tells LEAD "GO" when a repro yields a package. LEAD's 420 s repro subagent is still running; its result gets posted in LEAD's chat and appended here.
- **Lane B, eval ops (no code edits):** relaunch lost or failed shards, monitor, and collect T29 deterministic.
- **Lane C, PR #494 merge readiness:** unit suite on HEAD, CI, Greptile follow-ups, evidence and whitepaper wiring. It has its own worktree.

## LEAD STATUS 22:45 PT, read this first
- **Launches 11200547/11200548 mostly LOST. Root cause:** the eval VMs run as `224734058693-compute@developer.gserviceaccount.com`, which had only read access to `gs://dev-crsv3-transfer-224734058693`. Every shard ran to exit 0 but no upload succeeded, so the shards wrote FAILED and the VMs self-deleted. **Fixed at 22:40:** granted `roles/storage.objectAdmin` on that one bucket to that SA. It's crsv3-scoped, done by hand, and not in any deploy script yet; lane C should make `launch_fleet.py` preflight check bucket WRITE access, and/or have the deploy grant it.
- **Salvaged** from the last surviving VM: `t29_deterministic` failing shards 12 and 16 of 21, clean shards 3 and 7 of 7 (DONE). Failing shard 20 of 21 shows "running" but its VM is gone, so treat it as lost.
- **Clean-part fetch is COMPLETE:** all 100 parts are under `eval/v3-verdict/inputs/clean`.
- **LEAD decision, lane B: HOLD the deterministic relaunch** until lane A reports the 420 s repro, about 15 min. If the fix is "set CAD_REPAIR_TIMEOUT_S=420 in evalspec for all arms", the deterministic arm must run at the new commit too, so arms stay comparable, and relaunching at 0ff8db35 now would be wasted. If lane A needs longer than about 20 min, relaunch the deterministic arm at 0ff8db35 anyway (`launch_fleet.py ... --only <every t29_deterministic unit not DONE>`), with the 120 s caveat recorded.


## LEAD 420 s REPRO RESULT, for lane A (22:50 PT, at a0d4372b, transcripts in /tmp/diag3 on the Mac)
- **The 420 s timeout fixes the time budget:** regions get 150 s walls, and look, measure and probe succeed.
- **Still 0 packages:** every `edit` is refused. The model always sends all edit keys, e.g. `{"defect_code":"mesh_edge_through_face","fingerprint":"face:00c7…","op":"fragment_bodies","reset":false}`. `_edit_form` (`investigator_tools.py:947-951`) refuses `reset` alongside other keys, so the model retries until the runaway check (3 identical calls) ends the region.
- **Suggested fix:** drop a falsy `reset` (false or null) and empty-string keys before that check; keep refusing `reset:true` combined with a non-empty op or fingerprint; add a test for the `reset:false` + op + fingerprint form.
- **Also needed:** `CAD_REPAIR_TIMEOUT_S=420` for every arm in `evalspec.py`. At 120 s, region 1 still got "only proof time left".
- **Numbers:** region 0 had 8 tool calls (3 edits refused, ended runaway); region 1 had 0 (ended budget_spent); region 2 had 8 (4 edits refused, ended runaway). 12 investigator model calls, 118 s wall.
- **Lane A next:** apply the `_edit_form` fix and the 420 s evalspec change, rerun this repro, and send GO once at least one package is concluded.




## LEAD: AI WAVE LAUNCHED (00:xx PT 2026-11-20). User decision: criterion (a), plus lane E in parallel
- **Launch id 11200820:** 12× c2d-standard-8, us-central1-b, 96 shards, all 5 units. Code **b04befe8** contains lane A's c4774a2c (5 investigator fixes), lane C's 9e8db5d8 (per-file crash isolation) and lane D's transcripts plus per-region outcomes; image 00ce0156 (toolchain); CAD_REPAIR_TIMEOUT_S=420 for every arm. Results go to `gs://dev-crsv3-transfer-224734058693/eval/<arm>-b04befe8xxxx/`.
- **Scope:** this measures classification and escalation, not repair. 0 repairs are expected, because no operation is ever kept on this corpus (lane A, 8 parts: 24/25 regions concluded, 22 escalate, 2 mesher_side, 0 packages).
- **Lane B** babysits, relaunches failed shards and collects. **Lane E** (keep predicate / target_remains, incl. "unlocated after op read as still there") lands a fix, then LEAD reruns the wave at that commit.
- Check progress from anywhere: `collect.py --commit $(git rev-parse b04befe8) --status`.

## LEAD RULING ~23:50 PT: AI wave HELD
- Lane A fixed five investigator defects (through c4774a2c): the clock reserve, edit filler, the conclude tool, closing a region without asking, and the conclude schema plus refusal counter. The loop now runs end to end: tool calls happen, operations are tried on the clone, and a conclusion is reached.
- **No package is reachable.** No operation is ever KEPT on this corpus. T6 rules-only had 0 keeps on 90 parts, and the supporting t29-det had 0/119 newly meshed. conclude(package) needs a kept edit.
- **Concrete case:** on a touch-only part the model tried fragment_bodies unprompted. It added 12 faces with common_volume 0, then rolled back as `target_remains` with the same face fingerprints.
- **Ruling:** HOLD the AI wave, per the owner's rule to launch only on a concluded package. A wave would measure 0 repairs in every arm.
- **Lane A next (30 min box):** is the remaining defect a real gmsh failure after fragment (the op doesn't cure it), or only the contact locator re-inferring edge_through_face from bodies that still touch (fix the locator's conformal-pair exclusion)? GO if fixed and a package concludes.
- **Staging:** the branch HEAD is staged (code archive); it will be restaged at the GO sha.

## LEAD RULING 22:55 PT, on lane B's relaunch 11200639
- Lane B relaunched the 24 lost t29_deterministic shards at 0ff8db35 (120 s default timeout) as **11200639** (3× c2d, us-central1-b). This crossed LEAD's hold; neither side is at fault.
- **Ruling: let it run.** It costs about $1. Label its results `t29_deterministic@0ff8db35/120s`: supporting data only, NOT the arm of record.
- **The arm of record** runs at lane A's fixed commit, with `CAD_REPAIR_TIMEOUT_S=420` for every arm, in the single AI wave LEAD launches on GO. A different commit gives a separate results prefix, so nothing collides.
- **Quota:** 11200639 holds 24 C2D vCPU, and the full wave is 96 shards. If 11200639 is still running at GO, the wave goes on c2-standard-8 (C2 quota) or splits across both.
- **Bucket write grant:** before the AI launch, LEAD re-checks that `224734058693-compute@` still has objectAdmin on the crsv3 bucket, because a crsv3 redeploy may reset it. Lane C: make `launch_fleet.py` preflight-check bucket WRITE access.

## Lane B (eval ops) status, 2026-11-19 22:50 PT
- Clean-control inputs: 100/100 `.step` present under `eval/v3-verdict/inputs/clean/` (fetch finished; nothing to rerun).
- Launches 11200547/11200548 lost 24 of 28 T29-det shards. Cause: the VMs' compute default SA had only `objectViewer` on the transfer bucket, so every upload (rows sync, rows.json, PROVENANCE, DONE, FAILED) was refused. `objectAdmin` was granted by hand at 22:35 PT; only the last VM alive uploaded after that. Deploy IAM scripts use additive `add-iam-policy-binding`, so a redeploy should not remove the grant. Check the bucket policy before any new launch.
- DONE at 0ff8db35: failing 12/21, 16/21; clean 3/7, 7/7. failing 20/21 has rows.json but no DONE (rerun).
- Relaunched the other 24 as **11200639** (3 x c2d-standard-8, us-central1-b), same commit, image and 120 s timeout.
- Per LEAD ruling 22:55: 11200639 results are `t29_deterministic@0ff8db35/120s`, supporting data only. Lane B monitors and collects them, then babysits the AI wave on GO.
- **23:15 PT, T29-det supporting data SEALED** (`t29_deterministic@0ff8db35/120s`, not the arm of record), under `devtools/validation/v3_eval/supporting/t29-det-0ff8db35-120s/` and `gs://.../eval/supporting/`. Failing: **0/119 newly meshed, 0 newly broken** (meshed 2→2), plus 1 crash. Clean: **100/100 with no kept operation**, 0 broken. 0 model calls; VM cost about $0.13.
- **Segfault:** failing shard 15/21 exits 139 on both attempts (11200639 and 11200655), on `abc/0059_00597937_2e928f752b8806de5a85c14b_step_000.step` (sha256 fc4e4a440ffd…), after OCC FixShape `Geom_RectangularTrimmedSurface::U parameters out of range`. The process dies, so the file gets no row. LEAD routed per-file crash isolation (an outcome=crashed row) to lane C as a blocker for the AI wave. No sample change; shard 15 is not to be relaunched again.
- No eval VMs are running. Lane B is waiting for LEAD's AI-wave launch ids.

## Lane C status (PR #494 merge readiness), 2026-11-20

- **CI:** PR #494 CI fully green at e6b24a6d. Fixed along the way: a mypy error in the tournament, an unpinned clone in this file, a stale publication-authority manifest, and the stale settings roster in `docs/reference/configuration.md`.
- **Greptile:** the 3 `results_to_tex` threads and the 2 `launch_fleet` threads are fixed test-first and resolved.
  - The worst: `from-collect` matched no arm and would have left every number `[PENDING]`.
  - The 2 investigator-clock P1s belong to lane A.
- **Launch preflight:** refuses unless the compute SA holds a replace/delete-capable role on the RESULTS bucket. objectCreator alone is refused.
- **Crash isolation (9e8db5d8):** `--isolate-files` is in HARNESS_FLAGS, so a native crash becomes an `outcome=crashed` row with signal, exit code and stage, and the shard goes on.
  - A crashed file counts in `newly_broken` only if it meshed before; otherwise it goes in the separate `meshing.crashed` count.
  - `run_arm` counts model calls from `model-calls.jsonl`.
  - Proved with a SIGSEGV stub only, not yet on the real file.
- **Evidence:** all 4 PDFs build and none is stale. RUNBOOK §3b has the numbers-to-PDF steps and lists the hand-edited text.
- **PR body:** has the merge checklist: what is measured, T6 shard 4 dropped, images deferred, and the fixes that postdate measured commits.
- **Unit suite:** triage in progress, see the next lane C entry.

## Lane C: unit-suite triage (full `tests/unit`, 6 local shards, macOS; HEAD vs main merge-base a20bc216)

- **Shards:** 68 distinct failures. Each one was rerun at HEAD and at the merge-base.
- **Real regressions, all fixed and pushed:**
  - The settings roster was stale (578a14bd).
  - The supported-route contract lacked the admin standalone route, and the passive-accounting pin still expected state schema 15 instead of 16 (62b9006b). Both contracts were stale; the branch change was intended.
  - The standalone-delivery failure was decided inside the report block, which the wiring test forbids (9aa4be6d).
  - `test_repair_observe` popped `OCP` from `sys.modules` without restoring it. That made `test_repair_whole_shape_declares` fail in suite order only (e16c981a).
  - The publication manifest and the clone pin (earlier).
- **Pre-existing, documented, not touched:** 52 failures that fail the same way at the merge-base. All are macOS/host artifacts:
  - validation-runner and onboarding (`/tmp` workspace rule);
  - idle-env sweep and compose isolation (host tools);
  - assembly files, face names, FEA/CFD/radius-chords (OCP/gmsh on darwin);
  - sandbox seccomp `prctl`, `sched_getaffinity` in the mypy ratchet.
  CI (Linux) has never reported them.
- **Host-load flakes, pass on rerun:** gate D idempotence, `test_mesh_vm_cleanup` (60 s limit at load average 145), the consolidated matrix.
- **Not proved here:** the Linux run of the unit tier. PR CI skips it, so the main-push run is the first Linux proof.

## Product follow-ups (not for this PR)

- **A ShapeFix segfault kills the process.** `abc/0059_00597937_2e928f752b8806de5a85c14b_step_000.step` (sha256 fc4e4a440ffd...) segfaults (exit 139) right after OCC "FixShape failed with exception Geom_RectangularTrimmedSurface::U parameters out of range", on both attempts.
  - The eval harness now isolates it, but a production worker running the same repair would die with it.
  - The repair session needs the same out-of-process guard the mesher has (`engines/` native-crash isolation) before CRS repair runs on untrusted uploads.

## Earlier handoff (21:55 PT)

PR #494, branch `crs/v3-agentic`, HEAD **0ff8db35** (pushed). Personal env `crsv3` (GCP hexera-dev). Bucket: `gs://dev-crsv3-transfer-224734058693/eval/`. This file is also at `gs://.../eval/RESUME.md`.

## Launched (detached, self-deleting; nothing depends on the Mac)
- Arm **t29_deterministic** (rules-only plus PACKAGE_REVIEW=live), failing 21 shards plus clean control 7 shards, at code commit **0ff8db350582d7923ce455be4ee04a1db216880d**, image `mesh/app@sha256:00ce0156...` (built from 5d496416).
- Launch IDs **11200547** (1 VM, c2d-standard-8, us-central1-b) and **11200548** (3 VMs, c2-standard-8, us-central1-a). us-central1-a/c/f c2d hit ZONE_RESOURCE stockouts.
- Caveat: the clean-control fetch was ~90/100 parts in GCS at launch (the Mac fetch dies). Clean shards whose inputs are missing end FAILED. Rerun the fetch, then relaunch only those shards.
- Caveat: the eval spec does not set `CAD_REPAIR_TIMEOUT_S`, so runs get the 120 s default, not shared dev's 420. Record this with the result. Rerun with 420 if the comparison needs the shipping posture (see bug below).

## From any machine
```
git clone --branch crs/v3-agentic git@github.com:hexera-core/hexera-platform.git && cd hexera-platform
gcloud auth login && gcloud config set project hexera-dev     # needs access to the crsv3 bucket + compute
python -m venv .venv && .venv/bin/pip install -e .              # or make setup
C=0ff8db350582d7923ce455be4ee04a1db216880d
gcloud compute instances list --project hexera-dev --filter="labels.purpose=v3-eval"     # VMs alive
PYTHONPATH=$PWD/src .venv/bin/python devtools/validation/v3_eval/collect.py --commit $C --status
```
Collect refuses until EVERY planned unit (all arms × kinds) exists. Interim per-shard rows are under `gs://.../eval/t29_deterministic-0ff8db350582/{failing,clean}/shard-I-of-N/rows.json`.
Relaunch missing or failed shards (it skips DONE and running ones; scope with `--only arm:kind:I/N,...`; on stockout try `--zone us-central1-b` or `--machine-type c2-standard-8`):
```
PYTHONPATH=$PWD/src .venv/bin/python devtools/validation/v3_eval/launch_fleet.py \
  --image us-central1-docker.pkg.dev/hexera-dev/mesh/app@sha256:00ce015614daaccebfb7169ac24b2766f0069eeb5570fb5d4bb4b5518a597832 \
  --commit $C --image-commit 5d496416e3433ce7df626a07bc441f2459d34e49 --zone us-central1-a --machine-type c2-standard-8 --only "<units>"
```
Clean fetch (finish it): `PYTHONPATH=$PWD/src .venv/bin/python devtools/validation/v3_eval/clean_control.py fetch --work /tmp/v3-clean --gcs gs://dev-crsv3-transfer-224734058693/eval/v3-verdict/inputs/clean`. It is hash-checked; the slow part is about 6 min per ABC chunk.

## AI arms NOT launched: the 0-packages bug
- **Symptom:** the live investigator makes 0 tool calls and concludes `budget_spent` in every region, so 0 packages, and T19/T29-agentic would measure nothing.
- **9ecb677c** (Greptile fix "investigations spend reserved verification time") made unstated clocks use share 0.5 and proof reserve `min(60, L/2)+20`. Since `share*L` is below that reserve for every L, `must_conclude` fired before the first tool call. Reproduced locally at 859cbf7c: 0 tool calls in 3 regions.
- **0ff8db35** fixes the arithmetic in both clocks (`investigator._share_wall`, `InvestigationClock.region_wall`): the wall is the proof reserve plus share × the time above it. It has table tests.
- **But a repro at 0ff8db35 still gives 0 tool calls.** Region 0 got a 20.7 s wall, i.e. no time left above the proof reserve. Cause: the 120 s default `CAD_REPAIR_TIMEOUT_S` leaves about 21 s after the final reserve (60 s probe + 10 s slack + 5 s margin), and the proof reserve alone is 30 s or more.
- **Leading hypothesis:** the run is simply too short. Production dev uses 420 (`deploy.yml:1613`), and the eval spec never sets it.
- **Next diagnostic:** rerun the same local repro with `CAD_REPAIR_TIMEOUT_S=420` and check the transcript for tool calls >0, a concluded package and its fate. Repro command and env: see the diagnosis notes in this file's history. It is `repair_corpus.py <abc/00504025_5896ef7054620e0fdd79f59d_step_085.step> --mesh-probe --force-repair --transcripts ...` with run_arm's model wiring and the key from secret `openai-api-key`.
- **If 420 works:** add `CAD_REPAIR_TIMEOUT_S=420` to every arm's env in `devtools/validation/v3_eval/evalspec.py`. Run it for every arm so they stay comparable, and that means relaunching t29_deterministic too. Commit, restage (`stage.py --commit <new>`), launch all units.
- **Second hypothesis**, if 420 still gives 0: the proof reserve is too large relative to the region share. Check `investigator_budget.proof_reserve_s` and `region_loop.final_reserve_s`.

## Launch T19 + T29-agentic once fixed
`launch_fleet.py ... --commit <fixed> --only "v3_full:failing:1/32,...,v3_full:clean:1/7..7/7,t19_tournament_off:failing:1/29..29/29"`. Omit `--only` to launch every not-done unit (stage first). Ceiling 100 vCPU; C2D quota 100, C2 quota 100; stockouts are common in us-central1-a/c/f.

## After results land
`collect.py --commit <C> --pull --t6` (seals bundles plus results.json) → `results_to_tex.py from-collect ...` → `render` → `render_pr_section.py` → rebuild PDFs (see RUNBOOK.md) → commit, push → CI green → the owner merges. Then verify on shared dev, then `make destroy-env SLUG=crsv3` (collect first; it deletes the transfer bucket).

## Other state
- T6 shard 4: **dropped.** T6 is the rules-only local baseline on 90 files (`eval/t6-rules-only-0bf78625/`).
- Greptile: 21 comments, 20 fixed, 1 deferred (images), all resolved. 0ff8db35 itself is a fix to a Greptile fix and has not been reviewed.
- Unit suite: the 6 local shards died with the Mac. PR CI skips unit/integration by design; run `make test-fast` on a Linux box, or rely on post-merge main CI.
- Pending crsv3 gate jobs 50da0655 / 81fd1b1a ran at 5d496416 (pre-fix); ignore their repair outcome.

## LANE A status (0-packages), 2026-11-20
- **Three causes, all fixed and pushed. Each fix has a test that failed before it.**
  1. **99b5d4a2:** a region's wall was capped at the tier budget's bare wall. CHEAP's 60 s is less than the 80 s proof reserve (60 s scoped probe + 20 s review), so every cheap region concluded before its first tool call, at any timeout. Both clocks now add the proof reserve on top of the budget wall. The tournament now rehearses only the time above the proof reserve (Greptile P1 x2, resolved).
  2. **dfbf024e:** the strict tool transport sends every declared field, so every `edit` arrived with `reset:false` and every reset with `""` fillers, and `_edit_form` refused all of them as a mixture. A local 420 s repro at 0ff8db35 showed 14 tool calls, all 4 edits refused, and a runaway. Fillers now count as unset.
  3. **e6b24a6d:** evalspec gives every arm `CAD_REPAIR_TIMEOUT_S=420`, so t29_deterministic must relaunch at the new commit to stay comparable.
- **In flight:** crsv3 redeploy at e6b24a6d (run 44691183384), then the 2-file gate repro. A local repro is also running. GO follows once a package is concluded.

### LANE A, second pass: two more causes, and the wall behind them

- **bc88c9f9:** `conclude` was never DECLARED as a function. A native-tool model concludes by calling a tool, so the only route to a conclusion was to abandon the tool transport and write the prose protocol's JSON - which the live model never did (16 tool calls over 3 regions, 0 conclusions). `conclude` is now declared; a turn where only a conclusion can still be used declares it alone; and a region that has spent its tool calls is ASKED to conclude once before it is closed, instead of having its kept edits thrown away.
- **c4774a2c:** the schema declared the three conclusion forms as optional properties with no constraint, so the model hedged and sent `escalate` AND `mesher_side` together on 11 straight turns, each refused. The forms are now declared exclusive (`oneOf`, one key per branch). Those 11 turns also spent the whole model-call budget because the refused-in-a-row counter was cleared by every reply that PARSED, before `_conclude` could refuse it; only an ACCEPTED conclusion clears it now, so a repeated refusal trips the runaway guard at 3.
- **THE WALL, for whoever owns the next lane.** No package is reachable on this corpus by any arm, and it is not the investigator's doing: `conclude(package=...)` is refused without a kept edit, and NO OPERATION IS EVER KEPT. The T6 rules-only run of 90 parts (`eval-results/t6-rules-only-0bf78625/rows-{1,2,3}.json`) meshed 0 parts after repair and rolled back every operation it ran - heal_self_intersections 15, fix_small_faces 14, fix_small_edges 10, sew_faces 2, fragment_bodies 2, close_wire_gaps 1, fix_face_boundary 1, fix_shape_global 1, split_closed_faces 1, zero keeps. On the gate part the model tried `fragment_bodies` on both located faces unprompted; it ran (faces_added 12, bodies 2, common_volume 0) and rolled back with `target_remains`, both fingerprints remaining. The op adds the interface but the original face keeps its fingerprint, so the predicate cannot see it cleared. That is the `target_remains` family (see the 2026-11-19 diagnosis spec), and the keep predicate is the thing to own next.

### LANE A, the fragment_bodies question answered (for the predicate lane)

Measured directly on the gate part (`00504025_..._step_085`), the operation applied outside the loop with the flags on and a WHOLE-PART probe either side:

- Before: 4 located `mesh_edge_through_face`, faces 15/19/96/98.
- The op runs: faces 307 -> 315, 12 added, solids unchanged, area and volume delta 0, common volume 0.
- After: the shared-solid-pair set is `[(1, 2)]`, so the contact locator's "already conformal" exclusion FIRES - the fuse really did give the pair a conformal interface.
- And the part still does not mesh: `PLC Error: A segment and a facet intersect at point`, reached in 15.9 s against a 600 s budget. **Not a timeout, and not a wrong-tool mislabel: `fragment_bodies` genuinely does not cure this part.**

The part worth owning: after the op the failure is **unlocated** (fingerprint `""`, index 0) precisely BECAUSE the exclusion fires - no face can be named any more. The re-inspection therefore clears nothing, and the predicate reads the original fingerprints as still present. The live edit's `remaining: [both fingerprints]` is not the same defect re-found on those faces; it is "no longer measurable" recorded as "still there". A predicate that distinguished those two would not keep this operation either (the part still fails), but it would stop reporting `target_remains` for operations that did change the part.

Loop proof at c4774a2c, 8 corpus parts: 18 region investigations, 17 concluded (16 `escalate`, 1 `mesher_side`), 1 runaway, 0 packages - consistent with the wall above, since `conclude(package)` needs a kept edit. Before these fixes the same runs concluded nothing at all.
