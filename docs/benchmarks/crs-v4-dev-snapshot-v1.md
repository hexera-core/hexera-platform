# CRS v4 dev snapshot v1 (PROVISIONAL)

**PROVISIONAL.** 30 dev files and 8 clean-control files from Task 43's 86-file sample. This is not a
benchmark of record. The split is stale: see Caveats (a).

| | |
|---|---|
| Date | 2026-11-23/24 (wave ran 23:32:55Z to 00:32:57Z) |
| Code measured | `79d7ba35993f9569e0ee3dfffe2752da9d38f2b4` on `sdd/snap-v1` = feature-branch tip `5dc953ea` + `5bd7c03d` (two normal WaveDefs `snap_v1_dev` / `snap_v1_clean`, all four arms) + `79d7ba35` (driver and shard plan only; no code) |
| Mesh image | `meshpipeline-mesh:native` = `us-central1-docker.pkg.dev/hexera-dev/mesh/crsv4-mesh@sha256:633e1c3bfd8e2c0fb97b166fa28ff834d0433b84ca8e77c24eb4b00c83ece361` (the same repo digest Task 49 used), local id `sha256:03844d4454ccb620c5e3dce134d5c1f318ba3f509178cb9e7f21b5b6031ec6f4`. Code bind-mounted at `/w`. |
| VM | `dev-crsv4-snap-v1`, c2d-standard-16, STANDARD, `us-central1-b`, Debian 12. Server-side reaper `--max-run-duration=6h --instance-termination-action=DELETE`. Created 23:30:50Z, deleted by me 00:36:19Z. |
| Flags | `--mode arms --h-mm auto --ops 24 --workers 1 --file-usd-cap 0.5 --route openai:gpt-6-astra --wave-type normal`. File wall `--file-timeout-s` default **900 s**. Per-arm `Budget.wall_s` is **600 s**. Task 55/A3 walls are not in this commit. `--ops 24` matches Task 49 for comparability, but **the unit is now APPLIED ops** (Task 53): a `run_stage` macro costs every op it applied. |
| Sharding | dev: 6 shards cut by a cost-balanced LPT plan (`shard_plan_dev6.json`, costs = Task 49's measured walls, input_only + staged + 2×search_det, 900 s for excluded files), 1/3/3/7/8/8 files. clean: 1 shard. Fresh verdict cache `gs://dev-crsv3-transfer-224734058693/eval/snap-v1/verdict-cache`. |
| Inputs | the 38 files from Task 43's `bundle.tar`, staged as `<sha256>/<basename>`. sha256 re-verified: 38 staged, 0 mismatches. |
| Model caps | per-shard `--model-usd-cap`: dev 0.50/1.45/1.45/3.35/3.85/3.85, clean 0.50. **Total $14.95, at most $15.** The `--model-usd-cap` flag lowers each shard's own ledger (`WaveLedger.cap_to`), so the wave cap is the sum of the shard caps. |

Every shard exited 0 (`results/logs/*.rc`). One shard was restarted. The first start of dev6 refused at the
route probe with exit 6: `NoUsableRoute: ... gpt-6-astra: the model saw 'salmon', not red: images are not
understood`. The other six shards passed the same probe in the same minute. I ran dev6 again once, unchanged,
and it passed. The first attempt's provenance and log are in `results/attempt1/`.

## Official reports

| report | exit | verdict |
|---|---|---|
| dev, 30 files (`report.py --wave snap_v1_dev --shards 6 --shard-plan ...`) | **1** | **REFUSED**: `00611852/00611852_673cbf4282be88b3374992cb_step_008.step [input_only]: the arm saw an input with clean=True but the dev split says clean=False`. This is the same near-threshold flip Task 49 saw. Not worked around. |
| dev minus that file, 29 files (sensitivity, **not a split**) | **2** | **UNRELIABLE**: "search_det and search_ai are indistinguishable on all 27 files they share". Here that is the real result, not a harness fault: the AI arm changed no outcome. Paired 27/29 = 93.1% (floor 80%). |
| clean control, 8 files | **0** | OK. Paired 8/8. |

The 30-file numbers below are computed directly from `rows.json` / `EXCLUDED.json`, as Task 49 did.

## Per-arm, dev split (30 files): body-overlap files counted as failures (headline)

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 30 | 28 | 28 | 28 |
| clean (of conclusive) | 1* | 2* | 2* | 2* |
| repaired (unclean in, clean out) | 0 | **1** | **1** | **1** |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes than input | 0 | 1 (`occt/bug32314_corrupted.stp` 1→0) | 0 | 0 |
| more panel passes than input | 0 | 3 (0→1, 0→1, 3→4) | 3 (same files) | 3 (same files) |
| inconclusive rows | 0 | 0 | 0 | 0 |
| excluded (file_timeout 900 s) | 0 | 2 | 2 | 2 |
| model $ (rows) | 0 | 0 | 0 | **$1.0000** |

\* One clean file in every arm is the flipped `00611852_..._step_008` (clean at input in this run, `clean=False`
in the split). The one real repair, in all three repairing arms, is `00614908_a8619514c95972d6221f4629_step_000.step`.

Excluded in staged, search_det and search_ai: `NIST-PMI-STEP-Files/nist_ftc_07_asme1_ap242-e2.stp` and
`abc/0052_00528484_58a256af264a260f6f931a1c_step_003.step`, both `file_timeout: file wall clock 900s exceeded`.
That is the same pair Task 49 lost, minus `bug32314` (see Caveats).

29-file sensitivity (report.py, paired n=27): input_only 0.0%, staged / search_det / search_ai each 3.7% (+1 file).
`deterministic_marginal` is +1 file and `ai_marginal` is +0 files.

## Per-arm, dev split: deep body-overlap files left out of the denominator (owner ruling)

**The depth cut cannot be made from v1's data.** Neither the rows (`triage.valid.hard`) nor the split
(`verdict_summary.hard`) carry an overlap depth or a fidelity bound per finding. They carry only the kind name
`body_overlap`, repeated once per finding. **The depth cut needs the C4 code.**

These 10 dev files have a `body_overlap` finding at input. The split and this run's `input_only` triage agree
on the same 10:

- `00613121/00613121_58c67ddf032617103d59c101_step_003.step`
- `abc/0051_00519727_589dc8aa5abe5e0fe4ada930_step_014.step`
- `abc/0052_00521884_f7d1e4b9658237600ec82613_step_012.step`
- `abc/0052_00528484_58a256af264a260f6f931a1c_step_003.step` (first layer open_shell; also excluded by timeout in 3 arms)
- `abc/0053_00533660_58a4821bab78c2106dbc0ab6_step_014.step`
- `abc/0053_00535066_bb0bc8e7914fdea894ad2ec6_step_009.step`
- `abc/0054_00541957_51229e00ebc25e88ce807b9e_step_000.step`
- `abc/0057_00574245_58b65964d2d73c0f795da35a_step_017.step` (first kind self_intersection)
- `occt/trj3_s1-db-214.stp` (first kind self_intersection)
- `occt/trj4_s1-tu-214.stp`

The table below is the **widest possible** version of the cut: it leaves out all 10 files, whatever their
depth. The ruled table, which leaves out only files deeper than the bound, lies between this one and the
headline.

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 20 | 19 | 19 | 19 |
| clean | 1* | 2* | 2* | 2* |
| repaired | 0 | 1 | 1 | 1 |
| regressions clean→unclean | 0 | 0 | 0 | 0 |
| fewer panel passes | 0 | 1 (bug32314) | 0 | 0 |
| excluded (900 s) | 0 | 1 (ap242-e2) | 1 | 1 |
| model $ | 0 | 0 | 0 | $0.7567 |

All 10 body-overlap files are unclean in every arm. None was repaired.

## Clean control (8 files)

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive | 8 | 8 | 8 | 8 |
| clean | 8 | **7** | 8 | 8 |
| regressions clean→unclean | 0 | **1** | 0 | 0 |
| fewer panel passes | 0 | 1 | 0 | 0 |
| model $ | 0 | 0 | 0 | $0.0440 |

The staged regression is on `00614313/00614313_760137e15b9b924e8339e428_step_003.step`, 4→3 panel passes. Task
49 saw the same regression on the same file. **Lane B's prediction holds.** The `search_det` and `search_ai`
OUTPUT rows for that file are both **clean, 4/4 panel**. `search_ai` spent **$0.0440** on that file (2 settled
calls), and that is the whole clean-control model bill. See Caveats (b).

## AI marginal (search_ai − search_det, per file, paired over 28 files both measured)

- **Files search_ai cleaned that search_det did not: none (0).**
- **Files search_det cleaned that search_ai did not: none (0).**
- Files where the two arms ended at a different panel-pass count: **none (0).**
- The model was called on 26 of 28 dev files: 37 turns, 2 tool errors (3 candidates refused), 0 sites declared
  unfixable. It proposed `run_stage` macros (condition 23, normalise 6, simplify 6, faces 4, edges 3, shells 2,
  bodies 1) and a few ops. Most were declined. The most common decline was `run_stage:condition` →
  `split_angle: invalidated: the result is not BRepCheck-valid`, 11 candidates on 14 files.
- search_ai stops: exhausted 24, patience 2. Two files stopped on the `file_ceiling`: the NIST
  `nist_ctc_01_asme1_rd.stp` ($0.173) and `nist_ctc_01_asme1_ap203.stp` ($0.164). So the per-file ceiling cut
  them well below $0.50, because the next call's reservation would have taken them past it.
- Cheap/full agreement, search_ai, 29-file report: 26.7% over 15 confirmed states. In 11 states the cheap tier
  passed and the full panel failed, and the reverse happened 0 times.

**Net: on this panel the model contributed 0 repairs, at $1.00.**

## Triage (triage.py, dev, exit 0): leads

| arm | what | key | files | of not clean | candidates |
|---|---|---|---|---|---|
| input_only | layer-1 hard finding | body_overlap | 74 | 29 | - |
| search_ai | layer-1 hard finding | body_overlap | 38 | 26 | - |
| search_det | layer-1 hard finding | body_overlap | 38 | 26 | - |
| staged | layer-1 hard finding | body_overlap | 38 | 26 | - |
| search_ai | open site kind | unlocated | 25 | 26 | - |
| search_det | stop reason | exhausted | 25 | 26 | - |
| search_det | open site kind | unlocated | 25 | 26 | - |
| search_ai | stop reason | exhausted | 24 | 26 | - |
| input_only | panel member still failing | gmsh_prod: other | 19 | 29 | - |
| search_ai | panel member still failing | gmsh_prod: other | 18 | 26 | - |
| search_det | panel member still failing | gmsh_prod: other | 18 | 26 | - |
| staged | panel member still failing | gmsh_prod: other | 18 | 26 | - |
| search_ai | op declined | run_stage:condition: ... split_angle: declined: invalidated: the result is not BRepCheck-valid | 14 | 26 | 11 |
| search_ai | op declined | unify_same_domain: no_change | 6 | 26 | 3 |
| search_ai | op declined | run_stage:normalise: ... tighten_tolerances: error: RuntimeError | 6 | 26 | 1 |
| search_ai | op declined | run_stage:simplify: ... unify_same_domain: no_change | 6 | 26 | 2 |
| search_ai | op declined | run_stage:faces: ... to_canonical: no_change: a face is already a Plane | 4 | 26 | 2 |
| search_ai | op declined | run_stage:edges: ... fix_poles: error: ShapeFix_Face.Perform changed nothing | 3 | 26 | 3 |
| search_ai | op invalid | remove_small: targets[0]: F79 is a face, not an any | 2 | 26 | 6 |
| search_det | op invalid | remove_small: targets[0]: F79 is a face, not an any | 2 | 26 | 6 |
| search_det | op declined | unify_same_domain: no_change | 2 | 26 | 2 |
| input_only / search_ai | deviation reason | deviation_exceeds_bound | 1 | - | - |

Read the counts this way: 25 of the 26 unclean search outputs end with an `unlocated` open site, and search_det
ended `exhausted` on 25 of them. The deterministic generator has nothing applicable for a defect it cannot
locate, mostly `gmsh_prod: other` panel failures. The full tables are in `reports/triage_dev.md`. Clean triage
(`triage_clean.md`) has one lead: staged, `gmsh_prod: other`, 1 file.

**ops = 24 (applied):** no row stopped on `ops` (search_det: exhausted 25, wall 1; search_ai: exhausted 24,
patience 2). So this snapshot still cannot tell whether 24 is too low.

## Money and time

- **Ledger: $1.0439** = dev $0.9999 + clean $0.0440, 39 settled calls, 0 open reservations, 0 releases. Dev by
  shard: s1 $0, s2 $0.2843, s3 $0.2219, s4 $0.1720, s5 $0.1727, s6 $0.1490. Rows sum to $1.0000 (dev) and $0.0440
  (clean). Both are far below the $14.95 cap, and no shard cap bound.
- **This is a lower bound.** The route probe (`select_route` → `probe_route`) is not metered against the
  ledger. It ran 8 times: 7 shard starts plus the dev6 retry, one of which failed. Task 50 also measured the
  ledger under-recording by 16-20%. Check the OpenAI dashboard for the true figure.
- **Wall: 60.0 minutes for the wave**, from launch at 23:32:55Z to the ALLDONE sentinel at 00:32:57Z. The
  critical path was dev1, a single file (`nist_ftc_07_asme1_ap242-e2.stp`): input_only took 891 s and three arms
  each hit the 900 s wall. The other dev shards took 42.7-56.6 min, and clean took 5.2 min.
- Per-file arm walls (dev, excluded counted at 900 s), median / mean / p90: input_only 21.9 / 99.7 / 211 s,
  staged 59.6 / 183.8 / 560 s, search_det 35.5 / 168.9 / 586 s, search_ai 60.9 / 170.8 / 588 s. All four arms per
  file: median 203 s, mean 623 s, max 3591 s, total 18,698 s.
- **VM: ~65.5 min** from create to delete, about **$0.82** at ~$0.75/h.

## Caveats

1. **(a) The v1 split is stale.** The v1 split labels were made on the original-STEP form, not on the
   engine's re-export (Lane D4). So "clean" and "first failing layer" in `PROVISIONAL_dev.json` /
   `PROVISIONAL_clean.json` describe a different form of each part from the one the arms judged.
2. **(b) The staged regression on `00614313_760137e15b9b924e8339e428_step_003` is the known bug fixed by Lane
   B5** and is not in this commit. search_ai's **$0.044 on that file is wasted spend, not a model effect**. The
   model was asked to repair the healer's damage to a part that was clean at input. Both search outputs are
   clean (above).
3. **Provisional 30-file split from Task 43.** It is small, and the hash-sampled families are uneven. One file
   (`00611852_..._step_008`) flipped clean between split and run, as it did in Task 49, so the official dev
   report refuses.
4. **The checker is not deterministic from run to run.** Lane D is fixing this. The flip above is one
   instance. Single-file differences between arms or runs should not be read as effects.
5. **Not in this commit:** the Task 52 model throttle and the Task 55/A3 per-file walls. This run used the
   900 s file wall and 600 s arm budget wall.
6. **The route probe is unmetered**, so the ledger $ is a lower bound (see Money). It is also flaky: one of 8
   probes failed its image check (`'salmon', not red`), which refused a whole shard until it was rerun.
7. **Over-wall rows: one row ran past the 900 s file wall and was counted.** Every (file, arm) row of the snapshot
   with `wall_s` > 900, from `results/*/shard-*/rows.jsonl`:

   | wave | file | arm | wall_s | stop reason | recorded as | outcome |
   |---|---|---|---|---|---|---|
   | dev | `occt/bug32314_corrupted.stp` | search_det | 1262.1 | `wall` (its own 600 s budget) | conclusive | not clean, not valid, panel 1/4 (input 1/4) |
   | dev29 | the same row | search_det | 1262.1 | `wall` | conclusive | the same (dev29 shards 1-5 are byte-identical copies of dev's) |

   No other row passes 900 s; the six `file_timeout` exclusions above (two files x staged/search_det/search_ai)
   are excluded correctly. **Cause, found after the run** (Lane A, fixed in `288755c2`): `FileTimeout` was an
   `Exception`, and the search loop's and staged healer's broad `except Exception` handlers ("one broken branch must
   not end the search") swallowed the alarm, spending the one-shot timer; the run then went on to the search's own
   wall and the output verdict. It is now a `BaseException`, and a row that finishes past its wall is excluded
   regardless. On the current tip this row would read as excluded (`file_timeout`); under Lane D4's rule a
   wall stop that did not end clean is unmeasured anyway. Either way it must not be read as a measured failure:
   **search_det's 28 conclusive rows above are 27 measured + this one unmeasured** (its clean count and repairs do
   not change; the row was not clean). The `fewer panel passes` 1 -> 0 on bug32314 is **staged**'s row (612 s,
   inside the wall), not this one.
8. **search_det has changed since Task 49.** It is now 2 clean / 1 repaired, against 1 / 0 then. The new repair
   is `00614908_..._step_000`, the file Task 49 named as the whole footprint of the Lane C "search discards the
   healer's gain" bug (fixed in Task 51). So this change is that fix, not a new capability.
9. **Inputs were not reshaped:** the same 38 bytes as Task 43/49, sha256-verified. The verdict cache was fresh,
   so walls are cold-cache.

## Artifacts

`.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1/`:
- `run_all.sh`: the driver. The dev6 retry was the same `launch` line for shard 6 alone.
- `shard_plan_dev6.json`: the shard plan.
- `results/snap-v1-{dev,clean}/shard-*/`: `rows.json(l)`, `EXCLUDED.json` and `PROVENANCE.json`.
- `results/snap-v1-dev29/`: the 29-file sensitivity copy.
- `results/ledger/*.jsonl`, `results/attempt1/` (dev6's refused first start), and trimmed `results/logs/` (rc, start and end files, sentinels).
- `reports/`: report and triage outputs with their `.rc` files, plus `PROVISIONAL_dev_minus_flipped29.json`.
