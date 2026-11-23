# Task 43 report: local re-probe of an 86-file sample, provisional dev split

Measured at code commit `0575cfa5`/`7b19d563` (my driver) on base `0f02d064` (Task 44 and 45 included). Head commit is on `sdd/task-43` (see `git log -1`). Not merged, not pushed. No model called. No bucket object created. One GCP touchpoint, authorised by the coordinator mid-task: `gcloud compute scp/ssh` to the existing VM `dev-crsv4-gate-1` (no VM created by me). The VM vanished at ~18:55 UTC, about 2 hours after creation, before three shards finished their pass-2 cache replay (see Cache).

**Everything below is from a sample of 86 files. It is not the benchmark's data.** Sample is stratified round-robin over diagonal bands, so it deliberately over-represents the tails (median diagonal 226 mm; 7 of 86 above 10 m). Do not read the proportions as corpus proportions.

## Findings (x86 VM run, 86 files, workers=1 per shard, 8 shards in parallel)

Sample by source: abc_fail 50, abc_pass 20, nist 8, occt 8. Each file was run through the cheap tier and then the full four-member panel (I ran full on every file except no_solids/unbounded, which is a superset of the plan's "cheap fails + 10% of passes").

Outcome of the 86:
| outcome | n |
|---|---|
| clean (full tier, certified) | 8 (abc_pass 7, nist 1) |
| failing, layer `panel` | 33 (family `other` 31, `periodic_surface` 2) |
| failing, layer `valid` | 33 (body_overlap 15, open_shell 7, self_intersection 7, brep_invalid 2, round_trip_invalid 2) |
| `no_solids` | 2 (both occt) |
| `unbounded_geometry` | 4 (all abc_fail, 3.46e100 diagonals) |
| not measured: `checker_unavailable` (check_valid exceeded its 300 s) | 3 |
| not measured: `file_timeout` (900 s) | 1 |
| not measured: driver error (see bug 1) | 2 |

Failing set (what `build_splits` sees) = 66. Inconclusive/unmeasured = 6 (7%). That 7% matters: Task 21's rule withholds a number above 10% unmeasured.

Inconvenient findings, in order of how much they change the plan:

1. **The family label is nearly useless for the panel layer.** 31 of 33 panel failures are `other`. Cause (reproduced on two abc_pass files): `gmsh_prod` fails with `HXT 3D mesh failed`, and `failure_family` does not recognise that text. `family_of` labels the panel failure by the FIRST failing member, which is almost always `gmsh_prod`. So stratifying by family collapses the panel layer into one bucket and the other members' reasons (low_quality, overlapping_facets, no_volume, edge_through_face, netgen_meshing_failed) are discarded. Either classify "HXT 3D mesh failed", or label by something other than the first member.
2. **The panel disagrees with itself constantly.** Of 73 files with a panel verdict: 9 all-pass, 13 all-fail, **51 mixed**. Only 12% of failing-panel files fail every member. "Panel fails" is mostly "gmsh_prod fails", which is a single mesher's HXT algorithm.
3. **Half the abc_pass files fail the panel.** 10 of 20 abc_pass fail layer `panel` (and 3 more fail layer 1). abc_pass was defined as passing a gmsh probe; the four-member panel (HXT with FEA sizing, low_quality threshold) is a stricter test. So abc_pass is not "known clean"; only 7/20 certify. The clean split can only be drawn from full-tier certification (it is), and the clean pool is thin: 8 in 86.
4. **Cheap tier misses.** Among 14 files the cheap tier passed and the full tier ran: 6 full-fail (43%). The cheap tier (gmsh default only) is a weak filter. Under the plan's rule (full on only 10% of cheap passes) most of these would be labelled pass without the full panel. Relevant to the "10% sample of passes" design.
5. **Two real bugs in the re-probe path (not fixed, out of scope, stated by evidence):**
   - After a `FileTimeout` (900 s SIGALRM) mid `pool.call`, the pool is left with an unread reply. The NEXT file's `check_valid` then reads it: `KeyError: 'units'` (read_step_xcaf got a stale reply) or `KeyError: 'hard'`. Seen 4 times across the runs, each immediately after a 900 s file. It crashed the whole shard (uncaught) three times on the host. `reprobe`/`run_arm` have no handler, so on the wave one timed-out file would kill its shard, and a stale reply with the right keys would be misattributed silently. My driver catches the exception, rebuilds the pool and records `*_driver_error`, so the sample completed.
   - Inconclusive panels are (correctly) not cached, so they re-mesh on every rerun: the 3 cache misses in pass 2 are all of that kind.
6. **check_valid is slow on a few files.** 3 files exceed the 300 s kernel call limit in layer 1 (diagonals 55 to 125 mm, sizes 1.5 to 4.7 MB) and are `checker_unavailable`; one file, abc_fail 1774 mm, hit the 900 s file clock. These are layer-1 costs, not meshing.
7. `unbounded_geometry` is 4/50 of abc_fail in this sample, but the sample over-weights the largest-diagonal band, so 8% is not an estimate of the corpus rate (Task 40: ~3%).

## Per-file wall-clock on the four-member panel

Native x86 (c2d-standard-16, 8 files in flight, no other load on the VM), n = 73 files with a full-tier row, seconds per file:
- full tier: **median 21.9 s**, mean 82.4, p90 139.6, max 882.6, min 1.3
- cheap tier: median 5.1, mean 25.9, p90 41.7
- cheap + full: **median 31.9 s**, mean 97.9, p90 181.3 (max 891). 73 files, 7,148 s total = 1.99 CPU-hours for 73 files, or **~98 s mean per file**, i.e. ~1.6 min mean, 0.53 min median.
- by source (full tier median): abc_pass 6.1 s (n 20), abc_fail 34.3 (40), occt 16.3 (5), nist 57.8 (8).
- by diagonal band (cheap+full median): 10-100 mm 12.8 s; 100-1000 mm 47.0; 1-10 m 60.0; >10 m 49.0.
- Mesher time itself (per member run, medians): gmsh_default 0.7 s, gmsh_prod 1.0, gmsh_alt 1.8, netgen_occ 2.8. **The panel's median wall is about 6 s of a 22 s median file.** Most of a file's time is layer 1/3 (BRep checks, Booleans, STEP round trip, faithful comparison). The mean is dominated by a handful of files with member timeouts (gmsh_alt up to 575 s, netgen up to 840 s, gmsh_default up to 841 s) and layer-1 kills.

Emulated amd64 on the arm64 Mac (workers=1, 2 shards, same files; host load average was 20 to 80 from other lanes, so the contrast is NOT clean): full-tier median 27.9 s, cheap median 9.4, cheap+full median 39.7, mean 96.8. **Emulation inflated the median by only ~1.3x, not by an order of magnitude**: the absolute figure was not as inflated as assumed. Both runs are dominated by the same few slow files.

For the VM-hour projection: use the **mean** (~98 s/file on the sampled mix, which over-weights large parts), not the median; the tail is the cost. Mean per member-timeout file is ~15 min; 6 of 86 files (7%) hit a 300 s or 900 s wall, and they account for about 60% of the time. A per-file hard cap is the lever.

## Verdict cache hit rate (by mesher invocations)

- Counted by wrapping each member's `run` and `VerdictCache.get/put` (observation only).
- **Pass 1, cold, 86 files:** 151 cache lookups, 0 hits, 149 puts. 368 mesher runs (gmsh_default 149, gmsh_prod 73, gmsh_alt 73, netgen_occ 73). Zero hits is correct: the sample holds no duplicate shas and cheap and full panels are different configs. The two non-puts were inconclusive panels.
- **Pass 2, replay, same files, same cache** (5 of 8 shards finished before the VM disappeared): 88 lookups, **85 hits (96.6%)**, 3 misses, and those 3 meshed 9 member runs. All 3 misses are the inconclusive panels that are never stored. Mesher invocations avoided: 85 panels, 100% of the stored ones. The host's emulated shard 1 agreed independently: 82 lookups, 79 hits, 3 misses.
- So the cache now hits (OCCT header clock fix works) on every conclusive verdict. Not measured: a hit rate on a real wave where duplicates exist across shards. Three shards' pass 2 did not complete (VM deleted).

## build_splits and the split

- Default 300/200/150: **refused**, `failing set has 66 files, need dev 300 + heldout 200`. I changed nothing in `build_splits`.
- This sample supports dev 30 / heldout 25 / clean 8, which is what I drew (`excluded_no_solids` 2, `excluded_unbounded` 4, `shortfall` empty). Clean is the limit: 8 certified files.
- Dev: 30 (abc_fail 19, abc_pass 5, nist 3, occt 3); 14 panel/other, 1 panel/periodic_surface, 7 body_overlap, 3 open_shell, 3 self_intersection, 2 brep_invalid. Heldout 25, clean 8.
- Scale check: a 53,799 minus f360 pool of ~10,887 cleared files with a 66/80 = 82% failing rate would give a failing set far above 500; the sample's failing proportion (66 of 80 measured, and 66 of 86) is higher than a benchmark built around cheap failures would assume. Treat that as an upper bound: the sample is half abc_fail.

Artifacts, in `devtools/validation/corpora/v4/provisional_sample_task43/` (448 KB total, committed), every split file carries a top-level `PROVISIONAL` field and the filename starts `PROVISIONAL_`:
- `PROVISIONAL_dev.json`, `PROVISIONAL_heldout.json`, `PROVISIONAL_clean.json`, `PROVISIONAL_SUMMARY.json`
- `PROVISIONAL_probe_records_x86.jsonl` (86 per-file records, timings, cheap and full rows, mesher counts), `PROVISIONAL_probe_records_emulated_host.jsonl`, `stats_p*_pass*.json` (cache counters).
They are not in `corpora/v4/` proper, so `reprobe.SPLITS_DIR` defaults do not pick them up. Pass them with `--split-file`. **The real splits must come from the full re-probe.**

## What was downloaded

- `abc_0061_step_v00.7z` from archive.nyu.edu, 712,969,275 bytes, to take the 90 abc_pass files for that archive (37 MB extracted).
- `NIST-PMI-STEP-Files.zip` from nist.gov, 13,976,599 bytes (33 nist PMI files; the CTC/FTC archive was not fetched).
- Local copies reused: abc_fail from `/Users/kitts/archductor/mesh-fail-corpus/abc` (sha256 verified against the pool for every sampled file), occt from `/Users/kitts/cadcorpus/occt`. Nothing from the private bucket, nothing authenticated. All under `/Users/kitts/archductor/t43-data/` (outside the repo). f360 never read.
- Files over 20 MB were not sampled (candidates are capped), so the sample is also biased to small-file-size parts.

## Commands (real exit codes)

- `git worktree add -b sdd/task-43 ... c45bfa13`: 0; `git rebase 0f02d064`: 0.
- `local_reprobe_sample.py candidates` (native image, 275 candidates): 0. `... sample` (86 files): 0.
- `... probe` x2 shards on host (emulated): crashed 3 times with `KeyError` (bug 1), resumed with the driver's error handler. Final shard pass completed. Host shard 2 pass 2 was stopped by me with `docker stop` (my own container).
- First VM launch: containers wrote nothing (container uid could not write to a host dir owned by the VM user) and 69 files came back `checker_unavailable` (file modes from a macOS tar). Discarded entirely (`vmout_run1_permdenied`), fixed with chmod, rerun cold: the 86-file results above come from that second VM run.
- `... splits` default counts: exit 0, printed `build_splits REFUSED`. `... splits --dev 30 --heldout 25 --clean 8`: exit 0.
- `scp` of results from the VM: exit 0. The later `ssh` failed with 255; `gcloud compute instances list` shows 0 instances.
- Gates not run: no src files changed, only the new driver in `devtools/validation/v4_eval/local_reprobe_sample.py`. `python -m py_compile`: 0. Ruff/mypy not run on it.
