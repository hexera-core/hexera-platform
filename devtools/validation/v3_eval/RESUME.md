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

## Earlier handoff (21:55 PT)

PR #494, branch `crs/v3-agentic`, HEAD **0ff8db35** (pushed). Personal env `crsv3` (GCP hexera-dev). Bucket: `gs://dev-crsv3-transfer-224734058693/eval/`. This file is also at `gs://.../eval/RESUME.md`.

## Launched (detached, self-deleting; nothing depends on the Mac)
- Arm **t29_deterministic** (rules-only plus PACKAGE_REVIEW=live), failing 21 shards plus clean control 7 shards, at code commit **0ff8db350582d7923ce455be4ee04a1db216880d**, image `mesh/app@sha256:00ce0156...` (built from 5d496416).
- Launch IDs **11200547** (1 VM, c2d-standard-8, us-central1-b) and **11200548** (3 VMs, c2-standard-8, us-central1-a). us-central1-a/c/f c2d hit ZONE_RESOURCE stockouts.
- Caveat: the clean-control fetch was ~90/100 parts in GCS at launch (the Mac fetch dies). Clean shards whose inputs are missing end FAILED. Rerun the fetch, then relaunch only those shards.
- Caveat: the eval spec does not set `CAD_REPAIR_TIMEOUT_S`, so runs get the 120 s default, not shared dev's 420. Record this with the result. Rerun with 420 if the comparison needs the shipping posture (see bug below).

## From any machine
```
git clone git@github.com:hexera-core/hexera-platform.git && cd hexera-platform && git checkout crs/v3-agentic
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
