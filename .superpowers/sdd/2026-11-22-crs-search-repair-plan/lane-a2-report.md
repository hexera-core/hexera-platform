# Lane A report, job 2: one read per STEP, one self-intersection per identical solid, input-side work once per search

Branch `sdd/lane-a`, base `eea4a823` (job 1 merged). All measurements native x86 on `dev-crsv4-gate-2`, never emulated.
No model called, no fleet, nothing created on GCP. "Old" below is `eea4a823` (job 1 already in); "new" is this branch.

## What the brief asked, and what the measurements allowed

1. **Read each STEP once per verdict.** Already true after job 1, and now proved by counting kernel calls on 128 real
   search candidates: every candidate's verdict makes exactly 2 `read_step_xcaf` calls (its own STEP, and the round
   trip's re-read, which is a different file and the point of the check); 8 make 3 (the first candidate of each
   file whose judge also reads the original STEP, memoised for the rest). Old and new are identical here, so no change.
2. **Run self-intersection once where the round-tripped shape is provably the same.** It never is. On the 60 files,
   input BREP vs round-tripped BREP: byte-equal 0/60, and not one single solid's exact dump matches either (0 solids
   across 33 multi-solid files). A STEP write/read always perturbs the encoding, so an exact identity never holds,
   and anything weaker (equal within tolerance) is not proof for a Boolean. Both runs stay. The one sound skip I found
   (if the INPUT has `self_intersection`, the round trip's is in `known` and cannot matter: 6/60 files) would serialise
   the round-trip check behind the input check on the other 54, so I did not take it.
   What I did instead with the same idea, exact identity: `_self_intersects` reads nothing but one solid, so it is
   memoised per worker by the solid's exact digest (sha256 of its BinTools serialisation: raw doubles, triangulation
   and normals included; a 1-ulp change moves it, tested natively). It hits where solids really are bit-identical:
   untouched bodies of search candidates, and states reached twice.
3. **Cache input-side work across a file's candidates.** Layer 3's one-side measurements (face keys, area, volume,
   box, body counts) are a pure function of that BREP's bytes, so they are memoised per worker by the file's sha256;
   the original is measured once per worker for every candidate. Also: face keys were computed twice per side per
   call (now once), and the declared-id twin test ran with nothing declared (now skipped; it cannot refuse then).
   Layer 1 has no input-side work for a candidate (it judges the candidate). The original's STEP read and BREP were
   already memoised per pool (job 1).

## Proof of no verdict change

- **60 files, probe case** (dev 30 + heldout 25 + clean 5 of the provisional splits; cheap then full, original ==
  step, `WorkerPool(2)`): full layer-1/3 reports (`dataclasses.asdict` of ValidReport and FaithfulReport) identical
  **120/120**; `reprobe.summarise` identical **119/120**. The one difference is the panel of `occt/bug32314_corrupted.stp`
  (full tier): `gmsh_alt` is `fail/other` in the old run and `crashed/gmsh_crash` in the new. That member flips on
  this file with unchanged code: Task 43 and this run's old tree recorded `fail`, both job-1 runs (old and new trees)
  recorded `crashed`. Layer 1 took 0.0 s on that verdict (memo hit), and nothing here touches meshing. Not my change,
  but it is not byte-identical, so I state it rather than round it.
- **128 real search candidates** (13 files, `search_det` arm, no model; captured once, then replayed through old and
  new: the judge's cheap/full verdict against the judge's own original, plus the declared-edits layer 3 re-run on the
  BREPs, as `stages._judge` / `search._judged` do; fresh pool per file): summaries identical **128/128**, full reports
  (incl. the declared re-run) identical **128/128**. Kinds: 77 staged-healer, 25 search cheap, 26 search full.

## Timings (old and new run CONCURRENTLY, matched shards, so both saw the same load)

gate-2 was shared with other lanes throughout (`laned-m*`, `t49-*`, load avg 10-14 on 16 vCPU). Running old and new
side by side keeps the comparison fair but not quiet; differences under ~10% on one file are noise.

**Per search candidate (128):** verdict median 12.5 -> 12.2 s, mean 16.8 -> 15.0, sum 2144 -> 1913 (-11%).
Layer 1 sum 1309 -> 1075 s (-18%); layer 3 sum 69 -> 47 s (-32%); panel 723 -> 749 (load). Per file:

| file | solids | candidates | layer 1 s/candidate old -> new | layer 3 s/candidate old -> new | verdict s/candidate old -> new |
|---|---|---|---|---|---|
| 0051_00519727_589dc8aa5abe5e0fe4ada930_s | 50 | 11 | 14.8 -> 11.5 | 0.00 -> 0.00 | 39.4 -> 36.6 |
| 0060_00606843_58c2e4ec85eca50f9755deb9_s | 27 | 9 | 17.4 -> 12.1 | 0.00 -> 0.00 | 25.8 -> 20.7 |
| trj4_s1-tu-214.stp | 11 | 10 | 15.3 -> 10.9 | 0.00 -> 0.00 | 19.5 -> 15.3 |
| 0057_00573556_58b60801384ce01021c41311_s | 7 | 10 | 15.1 -> 13.6 | 2.43 -> 1.64 | 23.4 -> 21.3 |
| 0055_00552922_58ad20431b9daa0fd0385375_s | 12 | 12 | 10.9 -> 8.1 | 0.00 -> 0.00 | 12.9 -> 10.3 |
| 0054_00547069_58aaf75dbaa7e90f795564f6_s | 7 | 10 | 12.8 -> 13.8 | 2.85 -> 1.97 | 21.2 -> 21.3 |
| 0052_00521884_f7d1e4b9658237600ec82613_s | 3 | 12 | 9.0 -> 6.9 | 0.00 -> 0.00 | 11.3 -> 9.0 |
| 0053_00534532_58a4bc30176e9a0f88be2c7f_s | 25 | 11 | 7.3 -> 7.3 | 0.00 -> 0.00 | 11.3 -> 11.2 |
| 00616622_94f777d6890fb2a4e48d0a0c_step_0 | 90 | 10 | 7.4 -> 7.2 | 0.00 -> 0.00 | 13.5 -> 14.2 |
| 0061_00615999_4f1d2ad2e4e729a2f1836480_s | 2 | 9 | 7.7 -> 7.1 | 0.80 -> 0.40 | 14.0 -> 13.0 |
| trj3_s1-db-214.stp | 11 | 8 | 8.1 -> 4.4 | 0.00 -> 0.00 | 10.4 -> 6.9 |
| 0057_00573880_417ab441d502eafedde01127_s | 1 | 8 | 3.0 -> 3.0 | 1.17 -> 0.88 | 8.0 -> 8.0 |
| 0060_00608014_3fedd89a1d6b0c6f1c34e1b7_s | 1 | 8 | 0.8 -> 0.3 | 0.00 -> 0.00 | 1.7 -> 1.5 |

Where it pays: multi-solid parts whose ops leave bodies untouched (trj3 -46%, trj4/0060_00606843/0055_00552922
-29% to -31% layer 1). Where it does not: parts whose layer 1 is body-overlap Booleans rather than self-intersection
(the 90-solid file), and single-solid parts (nothing to reuse).

**Per file, probe case (60):** layers 1+3 sum 719 -> 706 s, median 2.87 -> 3.00, i.e. unchanged within noise; layer 3
alone 20 -> 8 s. Expected: after job 1 the probe already measures each file once, and its round trip never matches
the input, so the new memos have nothing to reuse there except layer 3's duplicated face keys. Whole-file median
22.0 -> 21.2 s. Per-file table: `lane-a-work/res/timings_job2_probe.txt`.

## Changes

- `verdict/shape_digest.py` (new): `digest(shape)`, `file_digest(path)`, `WorkerMemo` (per-worker LRU; stores only
  computed values, a raise stores nothing).
- `valid_kernel.py`: `_self_intersects_once` = per-worker memo of `_self_intersects` keyed by the solid's digest.
- `faithful_kernel.py`: per-worker `_Facts` per BREP sha256, each value computed lazily where the plain code computed
  it; only numbers are shared, never OCCT objects (each call still reads its own shapes). Keys once per side; twin
  test skipped when nothing is declared.
- `tests/native/test_native_repair_v4_verdict_digest.py` (new, 4): 1-ulp sensitivity; an untouched body is not
  re-analysed and the report equals a cold one; layer 3 facts reused and the result equals a cold one. These were
  written with the code, not before it (on the old tree they fail at import, which is weaker evidence than a red run).

## Commands (exit codes)

- `PYTHONPATH=src task-21/.venv/bin/python -m pytest tests/unit/cad/repair_v4 -q`: 649 passed, 2 xfailed (xfails are in the merged base), exit 0.
- Native, host-emulated image: valid, faithful, judge_cache, zero_tet, stages, verdict_digest: 67 passed, exit 0.
- ruff check, ruff format --check, mypy on the three src files: clean.
- `make lint` / `make typecheck` / `make check-fast` whole-repo: not run.
- Drivers (outside the repo, `/Users/kitts/archductor/lane-a-work/`): `rt_identity.py`, `capture.py`, `replay.py`,
  `equiv.py`, `run_job2.sh`; raw results in `lane-a-work/res/`.

## Not done / not verified

- 2 of the 15 files picked for capture produced no candidates (one shard's last file was still running when the
  replay set was fixed; one never started); the replay covers 13 files.
- Memo hit rates inside workers were not instrumented; the per-file gains above are the evidence.
- Worker memos live for the worker's life: up to 4096 small entries (self-intersection) and 16 BREP fact sets per
  worker.
