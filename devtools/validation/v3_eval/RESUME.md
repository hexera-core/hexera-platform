# CRS v3 ship: RESUME (single source of truth; every lane appends its status below)

## SUMMARY OF RECORD: three AI waves (owner rules, 2026-11-20)

The headline is CAD-repaired files that newly mesh (gmsh, from the exported STEP), plus files newly broken. Tolerant/fTetWild is never counted. The clean rule is "still meshes AND no new defects"; "changed" is information only. Sample: 120 failing files and 100 clean parts. Each wave has exactly one isolated native crash per arm, on 0059_00597937.

| wave (code) | arm | files fixed | newly broken | clean: still meshes, no new defects | clean changed (info) | cost, failing arm |
|---|---|---|---|---|---|---|
| 1 (b04befe8), PRELIMINARY | det | 1 | 0 | 100/100 | 3 | $0.30 |
| | v3_full | 1 | 0 | 100/100 | 1 | $1.05 |
| | t19_off | 0 | 0 | — | — | $0.84 |
| 2 (df7e1d7a) | det | 1 | 0 | 100/100 | 3 | $0.38 |
| | v3_full | 0 | 0 | 100/100 | 1 | $1.18 |
| | t19_off | 0 | 0 | — | — | $0.91 |
| 3 (17d5f55e, aggressive clone) | det | 1 | 0 | 100/100 | 4 | $0.34 |
| | v3_full | 1 | 0 | 100/100 | 1 | $1.31 |
| | t19_off | 0 | 0 | — | — | $1.10 |

Notes:
- **The one fixed file is always `0060_00608290`, via `fix_small_faces`.**
- **v3 missed it in wave 2.** Region 0 was reverted by the area check, a rounding artefact fixed in 354c7d56 (post-merge). Region 1 was rejected by the reviewer; wave 3's reviewer accepted it (non-determinism, routed to lane E).
- **Regions concluded / asked (v3 failing):** w1 96%, w2 96%, w3 91%. Wave 3 is lower because regions ran out of tool calls under aggressive clone.
- **Runaways** stay at or under about 2%, and they are refused conclusions.
- **Model errors:** wave 1 had 14 invalid_request calls (the 97a9e216 defect, preliminary). Waves 2 and 3 had 0.
- **Appendix only:** the tolerant estimate is 61-68 failing files per arm per wave.
- **Sealed summaries** are in `devtools/validation/v3_eval/supporting/{wave1-b04befe8,wave2-df7e1d7a,wave3-17d5f55e}/`. Transcripts are under `gs://dev-crsv3-transfer-224734058693/eval/supporting/`.

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







## OWNER DECISIONS (via manager, 2026-11-20 ~02:10 PT). These supersede earlier acceptance rules
1. **MERGE PR #494 "when stable":** CI green on a settled HEAD, no lane mid-push, flags off in prod. NOT gated on eval results. Lane C calls "stable" and runs the merge checklist; LEAD approves.
2. **GOAL = maximise files fixed on the corpus.** The T29 "≥4× deterministic" bar is DROPPED everywhere. There is no pass/fail bar; numbers are reported honestly.
3. **CLEAN RULE:** clean parts may change if they still mesh and gain no new defects. This replaces "byte-unchanged". The changed-part count is reported as information.
4. **TOLERANT MESHER:** undecided whether it counts as fixed. Report CAD-repaired (the headline) and tolerant-mesher-meshable (a separate column) and NEVER sum them.
**Owner policy (earlier):** no per-region escalate; aggressive ops allowed on the scratch clone; gates only at delivery; job-level escalation only. Lane A is implementing it, flag-gated, for wave 3.

## LEAD: WAVE 2 LAUNCHED. These are the ARMS OF RECORD (2026-11-20 ~01:30 PT)
- **Launch id 11200927:** 12× c2-standard-8, us-central1-a, 96 shards, all 5 units. Code **df7e1d7a**; image 00ce0156 (toolchain); CAD_REPAIR_TIMEOUT_S=420; REVIEW_OBJECTION=live.
- **Contains:** lane E's keep rule (delivered STEP newly meshes, with a no-op guard, 9ae2e215); lane A's answered tool calls (97a9e216) and measured area/volume delta shown to the author (b8e57fa9); eval posture matching shared dev (d73dcdaa); lane D's role labels (822e4d03) and stored review verdicts (df7e1d7a).
- **Postdates the wave:** 81c4adbf (typing narrow plus a packages-default-to-[] path, host-side safe); 303ae94a (deploy TOURNAMENT_K=3 for shared dev; the eval already runs K=3).
- **Wave 1 (11200820 @ b04befe8) is PRELIMINARY:** 14 invalid_request calls from the unanswered-tool-call defect, the reviewer reachable, the objection posture off.
- **Known open item, which goes to wave 3:** the reviewer rejects face-removing packages with the objection "deviation not measured: no faces" (lane A's hypothesis; lane E is verifying and fixing the evidence).
- **Lane A single-part evidence (0053_00531146 @ 9ae2e215):** review OFF gives a DELIVERED repair (2 ops kept, meshes False→True). Review LIVE gives nothing delivered (1 real reviewer reject, 1 area-cap revert: 64.8 mm² measured vs 0.48 claimed, because the author was shown removed area, not measured delta; fixed in b8e57fa9).
- **Paper statement from lane E:** with review live, the only delivered newly-meshing keep on T6 is rejected, so everything-on measures 0 where review-off measures 1. That is a cost of the reviewer.
- **Lane rule:** run devtools/quality/mypy_ratchet.py plus ruff before every push.

## LEAD NOTE: lane E interim + lane A late fix (2026-11-20)
- **Lane E:** the T29-det supporting run at 120 s attempted 0 operations on all 119 files, so its 0 is a BUDGET fact, not evidence about operations. The wave runs 420 s for every arm.
- **Lane E, first real false rollback:** 0052_00525324 `fix_small_edges` was rolled back as `target_remains` (the kernel small_edge warning is still there), yet the WHOLE PART MESHES after the op.
- **Approved direction (LEAD):** keep it only if the pre-op part had a real gmsh failure AND a definitive post-op whole-part probe meshes. Every cap and check stays, and package review stays on. The keep is recorded honestly as `meshes_after_target_remains` with the unmet claim listed, and counted separately. The package predicate gets the same rule. LEAD reruns the wave at lane E's sha.
- **Lane A 97a9e216** (after the wave commit b04befe8): every native tool call is now answered, so no provider rejects the final round. Evidence from the 8-part sweep says it did not bite, because the model sends one call per round. Lane B greps the collected rows for model-error endings to confirm. 97a9e216 is included in the lane-E rerun.

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
- **00:25 PT 11-20:** Lane B is babysitting the AI wave **11200820** (12x c2d-standard-8, us-central1-b, 96 shards, all 5 units, code b04befe8, 420 s). All 12 VMs are RUNNING. Lane B relaunches failed or stockout shards with `--only`, then runs `collect.py --pull --t6`.
- **00:55 PT:** wave 11200820 is at 92/96 DONE, 0 FAILED. Reviewer reachable in every arm (all det calls ok). 14 `invalid_request` calls (9 v3_full, 5 t19_off) are the 97a9e216 defect. LEAD ruling: wave 1 is PRELIMINARY, sealed with the affected files named; the arms of record come from wave 2 at lane E's sha. Lane B seals wave 1 with `--out devtools/validation/v3_eval/supporting/wave1-b04befe8/`, not the evidence dir, so wave 2 fills that.
- **01:40 PT:** WAVE 1 (11200820) SEALED as PRELIMINARY in `devtools/validation/v3_eval/supporting/wave1-b04befe8/`. Newly meshed: det 1, v3 1, t19_off 0 (all 0060_00608290). 0 broken. Clean 97/100 untouched. Regions about 96% concluded of those asked; runaways are refused conclusions. See its README. Lane B is now watching WAVE 2 **11200927** (12x c2-standard-8, us-central1-a, df7e1d7a): these are the arms of record.

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

- **The admin repair-job page can never show a pipeline run.** `/admin/repair/jobs/{id}` (and the admin console's repair queue) read `CadRepairAttempt` rows. No `src/` code writes them: `RepairJobRepository.record_attempt` has test callers only, and nothing creates `CadRepairJob` rows. So every mesh/crsv3 job's repair is invisible there. Lane D put the investigation view on the run page (`/admin/ops/jobs/{id}/repair-trace`, `/jobs/<id>/why`) instead. Either wire pipeline repairs into `CadRepairAttempt` or retire the page.
- **The gate part wedges after repair: 0 attempts, cancelled at the 40-min cap.** crsv3 job 843249ef-f8b4-4071-983a-d9e550761341 (`abc/00504025_5896ef7054620e0fdd79f59d_step_085.step`, ecf6d72d).
  - The repair ran and its trace was uploaded (region 1 concluded), but the job never reached a mesh attempt and no repair_report was delivered.
  - The same part also ended `cancelled` in the 5d496416 smoke (job 0070038e).
  - Needs a look at what holds a job between repair and the first mesh attempt.

- **A ShapeFix segfault kills the process.** `abc/0059_00597937_2e928f752b8806de5a85c14b_step_000.step` (sha256 fc4e4a440ffd...) segfaults (exit 139) right after OCC "FixShape failed with exception Geom_RectangularTrimmedSurface::U parameters out of range", on both attempts.
  - The eval harness now isolates it, but a production worker running the same repair would die with it.
  - The repair session needs the same out-of-process guard the mesher has (`engines/` native-crash isolation) before CRS repair runs on untrusted uploads.
- **Model-call telemetry labels every call `role=intake`.** Reviewer, investigator and tournament calls cannot be split from `model-calls.jsonl` (wave 11200820: all 4,604 calls were `intake`). Per-role cost and reachability need a real role on each call.
- **`mypy_ratchet.py` fails on macOS with two errors CI never sees.** `os.sched_getaffinity` is Linux-only, so mypy on darwin reports `Module has no attribute "sched_getaffinity"` for `cad/fea_joins.py` and `engines/radius_chords.py`. Measured, not assumed: on `radius_chords.py` the default run reports it twice and `mypy --platform linux` reports it zero times. Both files belong to #465/#475 on main and nothing in this PR touches them. The fix is to pin the ratchet to CI's platform (`--platform linux`, or `platform = linux` in the mypy config); it is main-side tooling, deliberately out of this PR's scope. **Until then: do not "fix" those two, and do not edit either file - the rule for a lane is no NEW mypy error beyond them.**
- **`CAD_REPAIR_TIMEOUT_S` does not bound one part's wall.** `abc/0055_00556585_b7edb4698d5ebd4d0414ee58_step_000.step` (23.7 MB, 3,129 shells) takes 21-36 min in every arm: 2,103 s of repair under a 120 s budget at 0ff8db35, and 1,267-2,151 s per row at b04befe8 with 420 s. Some phase before or outside the clocked loop is unbounded. In production it would hold a worker.

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

## Lane D status (investigator trace visibility), 2026-11-20
- **Why "0 packages" was invisible on crsv3:** in the product the investigator's module `sink` is None, so no product run ever wrote a transcript. Also, `/admin/repair/jobs/{id}` reads `CadRepairAttempt` rows, which no pipeline run writes, so crsv3 runs could never appear there.
- **Eval (b04befe8, ecf6d72d):** every `transcripts.jsonl` line names its file. Rows carry per-region `investigator.outcomes` (ending, edits tried/kept, rollback reasons, package verdict, `why_no_package`). `collect.py` writes `transcripts-<arm>-<kind>.jsonl` and `outcomes-<arm>-<kind>.json`.
  - ecf6d72d (Greptile) rebuilds the outcomes from the transcripts' unelided `turns`. Wave 11200820 (b04befe8) needs no rerun: collect at HEAD reads it correctly.
  - Read the `outcomes-*.json` files, not the shard-time row outcomes.
- **Product (ce4a4478), behind `CAD_REPAIR_INVESTIGATION_TRACE`:** true where crs_v3=live, unset in prod. The repair report gets `measurements.investigation_trace.outcomes`. The redacted, size-capped per-turn JSONL goes to `jobs/<id>/investigation-trace-<sha16>.jsonl`, and its location or upload error stays in the report. `investigator.py` is untouched.
- **Admin (5a68c1ec):** `GET /admin/ops/jobs/{id}/repair-trace`, plus a "Repair investigation" panel on the admin console run page (`/jobs/<id>/why`): an outcome table, with the trace in an expander.
- **Not yet verified deployed.** That needs a crsv3 redeploy (`app` + `fleet`, for worker metadata) at ecf6d72d or later, then one crsv3 job, then reading `/admin/ops/jobs/<id>/repair-trace`.

## LANE E status (keep predicate / target_remains), 2026-11-20
- **Fix 213a7a0c:** an op that the progress rule rolls back as `target_remains` is KEPT, with reason `meshes_after_target_remains`, only when ALL of these hold:
  - the whole pre-op list held a real gmsh failure (an answer, not a timeout or a no-answer);
  - a whole-shape probe of the part AS DELIVERED (STEP written by the promotion writer, then read back) meshes;
  - the rest of the progress rule still passes (new_defect, revealed, an unlocated failure of the aimed code).
  Caps, declines and review are untouched. The unmet claim is reported in `delta.unmet` and `PackageVerdict.unmet_cleared`, never rewritten as cleared. Rows count these keeps as `operations_kept_meshes_after_target_remains`. The package predicate gets the same rule and re-proves the cure at its own boundary. Six guard mutations are each killed.
- **Why STEP:** abc 0052_00525324. `fix_small_edges` cures it by widening tolerances. In memory it meshes 3 times out of 3 and its BREP round-trip meshes, but its STEP round-trip fails again ("segment and facet intersect"). An in-memory proof would promote a file that still does not mesh.
- **Full count. Oracle:** a whole-part gmsh probe after every op. Rules-only, 900 s, review off, all 28 T6 files that attempted ops, 82 op verdicts:
  - 66 `target_remains` where the part still fails. Honest; the 11-19 spec holds.
  - 5 `target_remains` where the part meshes in memory. One is 0052, which the STEP round-trip loses. The other 4 are 0060_00608308, which ALREADY meshed before, so the guard correctly refuses it.
  - 11 kept by the existing predicate.
  - **Result: the corrected predicate adds 0 delivered keeps on this sample.** The door is right, but it is rarely the bottleneck.
- **What review costs.** With review off, 6 of the 28 files end with kept ops: 5 `repaired`, 1 `changed_without_fixing`. Only **1 newly meshes: 0053_00531146** (fix_small_faces + fix_small_edges, overlapping_facets -> meshes; the re-read STEP meshes). In T6, with review live and no reviewer model, that file and 2 others (0052_00523209, 0056_00563150) were lost to "rejected by review". **So the review cost on T6 is 1 newly meshing part, out of 3 files whose kept ops it rejected.** Caveat: the oracle ran at 900 s, against T6's 120 s.
- **Not mine, flagged:** two unit tests fail identically at b04befe8 base:
  - `test_repair_oracle_invariants::test_every_v3_module_is_listed`, because `investigation_trace.py` is unlisted (lane D);
  - `test_repair_whole_shape_declares::test_fix_shape_global_declares_a_rebuild...`, which fails in the full-suite order only.
- **Tools:** `/tmp/laneE/tools/{oracle.py,run_one.sh,roundtrip.py}`; output in `/tmp/laneE/out`.
- **Follow-up (lane A review):** two additions.
  - **No-op guard.** The cure door now also requires the op to have changed the shape. A STEP round-trip alone can move gmsh's answer: on unrepaired 0061_00610830 it fails on different faces. The pass/fail verdict matched in-memory on all 6 parts measured, but a no-op must never be credited for what the export did.
  - **Cost** of each candidate keep, measured on 6 T6 parts of 22 KB to 3.2 MB on a loaded box: STEP round-trip 0.01-3.8 s, plus the delivered whole-shape probe at 1-10 s. The probe is capped by the run's remaining budget (`mesh_probe_budget_s`); the round-trip is not, but it stays under 4 s at 3 MB. It is paid ONLY on a `target_remains` verdict on a part with a real pre-op gmsh failure, and is skipped when a whole-shape re-inspection already failed. Inside the investigator it counts against the region wall, so watch region wall-outs in the next wave.
- **Evidence asymmetry, to state explicitly in the paper:** with review live and no reachable reviewer model, the only delivered newly-meshing keep on T6 (0053_00531146) is rejected. So an everything-on arm measures 0 where a review-off arm measures 1. That is a cost of the reviewer, not proof it is harmless.

### LANE A, the two postures on 0053_00531146 (the first delivered repair)

Measured at 9ae2e215, agentic loop, 420 s, one part. The fix for the third point below is b8e57fa9.

**Review OFF - DELIVERED REPAIR.** 4 regions, 38 tool calls, 28 model calls, 318 s. Region 0 concluded a package (`fix_small_faces@face:1626ed48ae84e5fb`, 6 claimed cleared) and region 2 another (`fix_small_edges@edge:bf80eb813196b6f9`, 3 claimed cleared); BOTH ops kept. File: 2 attempted / 2 kept, 8 `small_edge` + 4 `small_face` repaired, `defects_after` EMPTY, valid either side, `meshes_before` False -> `meshes_after` TRUE, ending `repaired`. `operations_kept_meshes_after_target_remains` is 0, so these are ordinary cleared-target keeps and NOT the mesh-cure door.

**Review LIVE - nothing delivered**, outcome `refused`, stopped `unmeshable`. Region 0's package was reverted by the reviewer ("review: rejected by review"); region 1's was reverted by the CAP, not the reviewer; region 2 concluded `escalate` after 0 tool calls, declining to inspect the region at all. The reviewer verdict is REAL, not a no-answer: 21 model calls that run, 16 the investigator's, 5 others, every one `provider_status` ok with `timed_out` false. So the shipping reviewer rejects the package that, with review off, delivers a meshing part.

**[RETRACTED by lane A, 2026-11-20. The note below is WRONG:** `_warn_unreviewed_closure` only logs a warning, and both postures offer the same catalogue. The 0053_00531146 comparison IS a clean A/B, and the only difference is the reviewer.**]** ~~Note on comparing the two:~~ they do NOT offer the same catalogue. With review off the run fail-closes `close_wire_gaps` ("moves vertices and must not run on customer jobs until the package reviewer is live"), so review-off runs one operation short. It does not affect this part's keeps, but the difference is not the reviewer alone.

**The cap revert, cause (fixed in b8e57fa9).** `FixSmallFaces` reports `area_removed_mm2` - the removed faces' own area - and deliberately no `area_delta_mm2`, which is the only key `caps.Accounting` sums. So the declaration is 0 and `package.evaluate` judges the WHOLE movement against the author's claim. The tool handed the model 0.48387, the model claimed exactly that, and the surface moved 64.8064 by rebuilding the sliver's neighbours: **the author was judged on a number it was never shown.** It is target-dependent, which is why the same op with the same claim was kept in the review-off run and reverted here. The edit result now also carries `measured_area_delta_mm2` and `measured_volume_delta_mm3`, taken from the session's own before/after measures of the clone (no new kernel call), with the prose and the catalogue line saying to claim the measured movement.

**Open, routed to the reviewer-evidence lane.** With the objection posture live the measured objection handed back is `{"field": "deviation_status", "measured": "not measured: no faces"}`. `package_review` reads `deviation_faces == 0` as "no faces", and `fix_small_faces` REMOVES faces - so the reviewer may be rejecting for missing evidence rather than on merit, with an objection naming a field a face-removing package can never satisfy. Hypothesis, mechanism unconfirmed.

## Lane C: owner reporting decisions applied (2026-11-20)

Commits a9530a28, 195a4251, 2763d3a8 plus the prose pass on the papers, evidence README and
RUNBOOK. Four rules, now stated the same way everywhere:

1. **No pass/fail bar.** The T29 "v3 newly meshes >= 4x the deterministic arm" target is dropped,
   not missed and not undefined. No verdict, ratio or acceptance field is computed or rendered.
2. **Headline per arm = files fixed:** failing parts whose CAD-repaired, exported STEP newly meshes
   in the gmsh probe (`\vThree<Prefix>FilesFixed`).
3. **Clean control:** a part is good if it still meshes and gained no new defect
   (`\vThreeCleanStillGood`). Clean parts repair changed (kept op or crash) are information, not a
   failure (`\vThreeCleanChanged`). "Byte-unchanged" is gone.
4. **Tolerant mesher (fTetWild)** is not a fix and not a product fallback: an opt-in diagnostic
   (`--tolerant-delivery`, `harness_argv(tolerant_diagnostic=True)`), shown only in the appendix
   diagnostic table labelled "tolerant-mesher meshable (not counted as CAD repair)".

`results.json` is now **schema_version 2**.

## Lane D, crsv3 proof of the investigation trace (deploy at ecf6d72d, 2026-11-20)
- **(1) The trace lands: PROVED.** Two jobs wrote JSONL to the artifacts bucket:
  - Job 843249ef-f8b4-4071-983a-d9e550761341 wrote `gs://dev-crsv3-artifacts-224734058693/jobs/843249ef-…/investigation-trace-bbf80ccfa77fbe69.jsonl`: 9 lines, region 1 concluded.
  - Job a713057e-18af-4ba3-9e1a-ec0ce41d5ebb wrote `…/jobs/a713057e-…/investigation-trace-f68dc31f63417ba0.jsonl`: 5 lines.
  - Both were checked for keys, tokens and STEP/BREP content and contained none.
- **(2) The report carries the table: PROVED.** Job a713057e succeeded. Its `repair_report-00bcae166619758f.json` has `repair_attempt.measurements.investigation_trace`:
  - `outcomes`: region 15, ended `concluded`, form `escalate`, 0 edits tried, with its why-no-package sentence.
  - `transcript`: object_key, 5 lines, sha256.
  - No `lines` copied into the report, and no reviewer prose.
  - `packages` (the review verdicts) is absent because ecf6d72d predates df7e1d7a.
- **(3) Admin endpoint and page: NOT provable on crsv3.** crsv3 sets no `ADMIN_API_KEY`, so every `/admin/*` route returns a deliberate 404 (checked).
  - The handler was run locally against the exact crsv3 report and trace bytes: it returned region 15 and all 5 lines.
  - LEAD ruling: verify on shared dev after merge (checklist below). No admin secret goes on crsv3.

### Post-merge shared-dev checklist (lane D). FOR THE OWNER: shared dev is human-run
Agents neither submit jobs to shared dev nor read its `admin-api-key`. Steps 1, 3 and 4 are yours. Step 2 is read-only, and lane D can do it once you send the job id.

-1. **Roll the worker fleet (owner runs this).** A merge to main deploys only `images,migrate,console,admin` (`deploy.yml`, the main branch). The worker fleet, which runs the repair node and carries `cad-repair-investigation-trace` in its instance metadata, stays on its previous build: on 2026-11-20 it was still the 11-17 template, with no v3 flags at all. Shared dev therefore runs no v3 repair, and writes no trace, until this runs:
   ```
   gh workflow run deploy.yml --ref main -f fleet=true      # empty slug = shared dev
   gh run watch
   ```
0. **Pre-check that the flag reached the workers** (read-only). This must print `cad-repair-investigation-trace` = `true`:
   ```
   gcloud compute instance-templates list --project hexera-dev --filter="name~^dev-worker" --sort-by=~creationTimestamp --limit 1 --format=json \
     | python3 -c "import json,sys;[print(i['key'],'=',i['value']) for i in json.load(sys.stdin)[0]['properties']['metadata']['items'] if 'investigation' in i['key']]"
   ```
1. **Submit one part** through the shared-dev console, https://dev-console-s7toqcfd3a-uc.a.run.app:
   - Part: `abc/00502475_5895e91efca3da0f6b68639a_step_003.step`, sha256 `81708e24669d635d5f45657ba8fa1730f7880809e504da56f4b3c53153b458cf`. It is in the mesh-fail corpus (on the owner's Mac: `~/Downloads/mesh-fail-corpus-for-areen/abc/`). On crsv3 it repaired, traced and meshed in about 19 min (job a713057e).
   - Chat: "I need a solid volume mesh of this part for a static stress FEA. Use the default settings."
   - Press Proceed on the geometry check, answer "yes" to the confirmation, and wait for a terminal status.
   - Note the job id: it is on the job page and in the admin Activity list.
2. **Read the artifacts** (read-only):
   ```
   J=<job id>
   gcloud storage ls gs://hexera-dev-artifacts-224734058693/jobs/$J/
   gcloud storage cat "$(gcloud storage ls gs://hexera-dev-artifacts-224734058693/jobs/$J/ | grep repair_report)" \
     | python3 -c "import json,sys;r=json.load(sys.stdin);a=r.get('repair_attempt') or {};m=a.get('measurements') or (a.get('detail') or {}).get('measurements') or {};print(json.dumps(m.get('investigation_trace'),indent=1))"
   gcloud storage cat "$(gcloud storage ls gs://hexera-dev-artifacts-224734058693/jobs/$J/ | grep investigation-trace)" | head -20
   ```
   Expect:
   - an `investigation-trace-<sha16>.jsonl`;
   - in the report, `investigation_trace` with `outcomes[]` (region, end, form, edits, `why_no_package`, `packages[]`), `transcript.object_key` pointing at that JSONL, and no `lines` key and no `review_reason` anywhere in it.
3. **The admin API**, with the shared-dev admin key you supply:
   ```
   curl -s -H "X-Admin-Key: <ADMIN_API_KEY>" \
     "https://dev-api-s7toqcfd3a-uc.a.run.app/api/v1/admin/ops/jobs/$J/repair-trace" | python3 -m json.tool | head -60
   ```
   Expect 200 with:
   - `outcomes` matching step 2;
   - `transcript.object_key`;
   - `lines` equal to the JSONL's lines (kinds `driver` / `model` / `call` / `end`; `call` lines with an `edit` carry status, reason and kept);
   - `handback`, only if the mesher handed back.

   A 404 `no investigation trace for this run` means the flag was off on the worker that ran the job (see step 0) or no investigator ran.
4. **The admin page.** Open https://dev-admin-s7toqcfd3a-uc.a.run.app/activity, find the run and click **why**, or go straight to https://dev-admin-s7toqcfd3a-uc.a.run.app/jobs/$J/why. Below the decisions, the **"Repair investigation"** panel ("per region: how it ended, and why no package") shows:
   - one table row per investigated region, with columns Region, Tier, Ending (e.g. `concluded (escalate)`), Tool / model calls, Seconds, Edits tried / kept, Rolled back, and Why no package (a package that was concluded shows "a package was concluded");
   - a "Per-turn trace (N lines, jobs/$J/investigation-trace-….jsonl)" expander holding the JSONL.

   A second panel, "Repair investigation after the mesh failed", appears only if a handback ran. For comparison, on crsv3 this part gave one row: region 15, `concluded (escalate)`, 0 / 0 edits.

## Lane A: every uninterruptible kernel call is bounded (2026-11-20)

Three worker hangs in one night - the clash gap, the investigator's distance, lane H's
`gap_split` - were one bug with three faces: an OCC call that cannot be interrupted, running in
the parent. **A deadline checked BETWEEN calls does not bound the call.** The clock written around
such a call is decoration; on a worker it hangs the job rather than one tool call.

**The standing guard** (`tests/unit/cad/test_repair_occ_bounded_guard.py`) AST-scans
`cad/repair/` for the uninterruptible families and fails on a new call site. Nothing is declared
to it: a module says which function is a child interpreter in its `if __name__ == "__main__"`
block, `bounded_child.KERNELS` is read from source, and child-side is the TRANSITIVE closure over
module-local calls - because the kernel call is rarely in the entry point (lane H's chain is
`_child_main` -> `_work` -> `partners` -> `_distance`). A lane that writes a new child or
registers a new kernel needs no edit here.

It earned its keep three times. Two sites the manual audit missed (`investigator_tools.
_thickness_kernel`, bounded only by `localize._bounded`'s FORK, and `spotlight._normal_of_face`);
and `operations.apply` calling `_common_volume` DIRECTLY while the registry said it was bounded -
**registering a kernel is not bounding it, the call site is.** `KNOWN_UNBOUNDED` and
`KNOWN_DIRECT_CALLS` may only SHRINK; bounding a call means deleting its entry.

**Two rules for whoever bounds the next one.**

1. *A measurement reports "unmeasured" and the caller carries on; an OPERATION must DECLINE.*
   `fragment_bodies` rebuilds two solids on the strength of its answers, so a child that was
   killed gives None and the operation declines - never the best of a partial measurement.
   `PARTNER_UNMEASURED` is deliberately not `None`: no candidate is geometry (`no_partner`), an
   unfinished search is ignorance, and only the second is a reason to stop.
2. *An answer that is only correct by SHARING TOPOLOGY WITH SHAPES THAT STAYED IN THE PARENT
   cannot be computed elsewhere and spliced back by identity.* When the fuse cuts one body and
   leaves the other alone, the conformal face belongs to the UNTOUCHED body - free in-process,
   impossible across a boundary, and splicing only the modified body left ZERO shared faces. Both
   bodies now come back from the child's one builder. Either everything that must share returns
   together, or the operation is not a candidate for bounding.

**Two fixtures too simple to hold the bug.** The symmetric two-box fuse (both bodies cut, images
interchangeable) hid the sharing loss completely, and a single-op equivalence test hid a defect
that only appears across a SEQUENCE (the scan-guard revert, PR #512). A new test that passes
immediately is wrong until it has been seen to fail.

**Measured cost of bounding.** 32 deviation samples on a 2.7 MB part: 0.58 s in the parent
against 2.09 s through a child, of which 0.74 s is interpreter start-up. Paid per tournament fold.
A kept child would take most of it back, which is where `bounded_child`'s dispatcher points.

**Per-body volumes, and why the aggregate lies.** `Measure.solid_volumes` plus `caps.solid_change`
(appeared / disappeared / split, `known: False` when unmeasured), shown to the reviewer and
counted per wave by `collect.body_changes`. It settled 0051_00513292, where a committed
`sew_faces` took solids 7 -> 8 with the volume moving +0.942 mm3: all seven original bodies
survive to four decimal places and a NEW 3601.17 mm3 body appeared. **The aggregate understated
the event about 3800-fold**, because an open shell's faces already contribute a NUMBER to the
volume integral rather than a volume - so the volume-comparison waiver on a solid gain is
load-bearing, not tolerated. No refusal was added: a split has never been observed, and the wave
count is the condition for adding one.

**Open.** Row 1, the contact search, is the last uninterruptible call outside the four
`BRepCheck_Analyzer` sites, which every repair path runs and which is a larger change than any
single distance call. Better location of unplaced defects (`regions_before {'unknown': 628}`) is
the scan-cost lever and has no owner.
