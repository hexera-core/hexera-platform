# Lane A report, job 3: the 1/120 mismatch, measured walls, image provenance

Branch `sdd/lane-a`, on `db58dbf6`. No model called, no fleet launched. Compute: gate-2 had been deleted, so with the
owner's approval I created `dev-crsv4-lanea-measure` (n2-standard-16, N2 quota so the wave's C2D stays free), measured
on it, and deleted it (~2.5 h). After the owner's 22:58 directive no container ran on local Docker; the native tier ran
on the `jig` context (one container, `/srv/work/lane-a`, removed afterwards).

**Overlap with Task 55.** Task 55 (merged `7ae1a689` while I worked) already raised the walls (1800 s per file, 900 s
per `check_valid` call, one constant shared with the engine) and added the `excluded_stratum` to collect and report. I
had built 2a/2b in parallel; I DROPPED my version rather than land a second design (kept on local branch
`lane-a-job3-backup`, not pushed). What this branch adds instead: the uncensored measurement Task 55 could not make
(it settles their open 1.3%-4.8% range), the mismatch diagnosis, and 2c.

## 1. The 1/120 verdict mismatch: wall-proximity nondeterminism, handed to Lane D

`occt/bug32314_corrupted.stp` (sha256 63d79ed9...), `gmsh_alt`. Run alone 8 times (native x86, `--cpus=1`, the real
member): every run first times out at the 300 s member budget; the 2x retry then ends at 750-840 s as **timeout**
(`timeout_twice`) x2, **fail/other** x3, or **crashed/gmsh_crash** (SIGSEGV) x3. Where gmsh is when the retry's clock
runs out decides the label, so it depends on CPU speed. Not a near-threshold SICN; it is the "corrupted file flipping
mesher-crash against mesher-failure" source amendment 5 names. `gmsh_alt` never passes, so `first_failing_layer` and
the failing-member count are stable; only that member's status/kind string flips. Handed to Lane D (session 173) with
the data and two possible fixes (one outcome class for anything after a first timeout, or label it by the timeout).

## 2a. The walls, measured uncensored: Task 55's walls do not reach 2%

Every exclusion in Lane D's and Task 43's data was censored (">900 s", ">300 s"). I re-ran all 10 wall-excluded files
with the file wall off and the layer 1/3 call wall at 3600 s, as the re-probe runs them (`WorkerPool(1)`, `--cpus=1`,
full verdict), plus 4 calibration files Lane D had timed on c2d (n2 took 1.08-1.49x their c2d time, mean ~1.27x).

| file (source sample) | was | uncensored on n2 (s): total / layer 1 / panel | check_valid calls (s) | result |
|---|---|---|---|---|
| occt/bug25013_25013face.stp (laneD) | file_timeout: file wall clock 900s | 1374 / 0 / 1373 | 0, 0 | invalid: no_solids,open_shell |
| 89be67146dd4070b440cd7aa_step_000.step (laneD) | file_timeout: file wall clock 900s | 1449 / 272 / 1177 | 133, 136 | invalid: body_overlap,self_intersection |
| 0d88f2ca9d27ef64ad0bd5f5_step_001.step (laneD) | file_timeout: file wall clock 900s | 1007 / 77 / 930 | 29, 34 | INCONCLUSIVE panel (retry spent) |
| 0e7ee8b9dcc7d552bf34ee08_step_000.step (laneD) | checker_unavailable: check_valid:  | not finished: a check_valid call ran past 3600 s | >3600 | still excluded |
| 589e508f97e5700f6ce1ce8c_step_001.step (laneD) | checker_unavailable: check_valid:  | not finished: a check_valid call ran past 3600 s | >3600 | still excluded |
| 58be4fff1c3df610237a50be_step_000.step (laneD) | calibration | 40 / 23 / 16 | 11, 9 | valid |
| 92e34291f8dc9d46cdf65cb9_step_007.step (laneD) | calibration | 50 / 15 / 35 | 6, 6 | invalid: body_overlap |
| 58c269759d8770102ad0d951_step_001.step (laneD) | calibration | 83 / 11 / 72 | 5, 6 | invalid: body_overlap |
| a100614698e973a4f1f07a25_step_007.step (laneD) | calibration | 108 / 1 / 107 | 0, 0 | valid |
| 58ad10a89420f7103b1bfecf_step_001.step (t43) | checker_unavailable: check_valid:  | 2509 / 2382 / 128 | 1382, 990 | invalid: body_overlap |
| occt/trj6_pm4-ec-203.stp (t43) | file_timeout: file wall clock 900s | 1062 / 169 / 892 | 79, 84 | invalid: body_overlap,self_intersection |
| 796e5a4b1bfb1f59da2e443d_step_000.step (t43) | file_timeout: file wall clock 900s | 1592 / 272 / 1320 | 122, 116 | INCONCLUSIVE panel (retry spent) |
| 58a2c9cc159cfb0fe677e9ab_step_023.step (t43) | checker_unavailable: check_valid:  | not finished: a check_valid call ran past 3600 s | >3600 | still excluded |
| 58a325b2e7998b0fd81515cf_step_000.step (t43) | checker_unavailable: check_valid:  | 2247 / 2212 / 34 | 985, 1216 | invalid: body_overlap |

Against Task 55's walls (1800 s file, 900 s call), c2d time ~= n2 / 1.27:

- **Rescued:** the files that were merely slow and end in a verdict: `bug25013` (~1,082 s), `89be67` (~1,141 s),
  `trj6` (~836 s).
- **Still excluded, and why:**
  - one solid whose `BOPAlgo_ArgumentAnalyzer` self-intersection runs past an hour (3 files: a single `check_valid`
    call was killed at 3600 s on two and ran past 70 min on the third; on the 24 MB one, 122 of 177 solids took
    <=12 s each and the 123rd never finished). No affordable wall fixes this.
  - a panel whose ONE timeout retry per file is spent: a second member's timeout is then `infrastructure` and the
    verdict inconclusive (2 files, `0d88f2` and `796e5a4b`, both former 900 s file-wall victims; on a faster c2d core
    they might not time out).
  - two Task 43 files whose `check_valid` calls take ~960-1,090 s each on c2d: just over the 900 s call wall (a
    1,200 s call wall would take them, at ~2x 1,200 s of a file wall).
  - the 2,000 body-pair overlap cap (1 file; a `ValueError`, not a wall).

**Measured excluded share at Task 55's walls** (Lane D's 59 files, pool-weighted by file-size band against the 10,887
staged inputs, the method that reproduces Lane D's 8.7%): **~5.6% if the two retry-spent files pass on c2d, ~6.6% if
not**, against Task 55's projected 1.3%-4.8%. Both samples together, unweighted: 11/145 before; 6-8/145 after. **Not
under 2%**: the wave will end at collect exit 2, which Task 55 already states is the correct outcome.

What would get under 2%, each a decision outside this lane: (a) a second panel retry, or classifying a
retry-spent timeout as the file's (panel design; Lane D / coordinator); (b) a per-solid self-intersection budget that
marks the solid unmeasured: it does NOT lower the rate (the file is still unmeasured), it only makes it cheap;
(c) raising `MAX_OVERLAP_PAIRS` after measuring per-pair cost; (d) the owner's `--allow-excluded` with the named
stratum. Raising the call wall to 1,200 s rescues the two Task 43 files at ~+1-2% pool cost.

**A note on Task 55's design** (not changed here): the 900 s call wall is a single constant that the ENGINE also
uses, so a search candidate's verdict can now block 900 s per layer-1 call instead of 300 s. My dropped version kept
the engine at 300 s and passed the re-probe's wall as an explicit `call_timeout_s`; worth a look if search wall time
matters.

## 2b. The excluded stratum

Delivered by Task 55 (collect `excluded_stratum` with every name and reason, printed every run; report.py's own
stratum block). Not duplicated. Two things my dropped version had that Task 55's does not, if the coordinator wants
them: the stratum carried into every split file (so report.py and triage.py can name the RE-PROBE's excluded files,
not only an arm wave's), and a refusal when a split holds a file the re-probe excluded.

## 2c. Image provenance

The pushed `crsv4-mesh:native` (sha256:633e1c3b...) has no `org.opencontainers.image.revision` label (read from the
registry; it carries only `MESH_SOURCE_TREE`, a working-tree digest). Now: `make mesh-image` stamps that label with the
HEAD sha of a clean tree ("dirty" otherwise); `image_build.py` builds, pushes, resolves the digest and reads the label
back, refusing a dirty tree, a failed build/push, or a mismatch; preflight's new `image_commit_label` check reads the
label from the registry (`docker buildx imagetools inspect`, no pull) and refuses a missing label, a mismatch, or an
abbreviated `--image-commit`; the v4 launcher feeds `--image-commit` to it and records the verified link on every VM
(`v4-image-commit-link`); `wave.py` passes it and refuses a short one before any cloud call. **The current pin is
unlabelled, so a launch against it is now refused: rebuild with `image_build.py` on an x86 VM first.** I did not
build or push an image.

## Tests (exit codes)

- `PYTHONPATH=src task-21/.venv/bin/python -m pytest tests/unit/cad/repair_v4 tests/unit/dev -k "repair_v4 or v4"`:
  1486 passed, exit 0, on the rebuilt branch.
- New for 2c: preflight label check (healthy, missing, mismatched, abbreviated, unreadable registry, multi-platform,
  stamp), launcher records/refuses (proved to fail on the pre-change launcher), wave refusal and argv, image_build
  (clean, dirty, mislabelled, failed build/push, Makefile stamp).
- Native (`docker --context jig`, before the branch was rebuilt; the verdict package is untouched by this branch):
  67 passed, exit 0.
- ruff check clean; mypy clean on preflight.py and image_build.py. Whole-repo make lint/typecheck/check-fast: not run.

Raw data: `/Users/kitts/archductor/lane-a-work/walls/` and `lane-a-work/flip_vm/`.
