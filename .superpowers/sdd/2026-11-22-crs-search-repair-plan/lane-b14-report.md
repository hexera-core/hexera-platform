# Lane B14: how to make snapshot v3 finish in about an hour

Design and numbers only. No VM, no model, no launch; nothing in `snap-v1r/` (launch.sh, prep.py, vm_run.sh) is edited
here — the proposed change is a diff at the end, for the snapshot runner to apply.

Measured inputs: v2's own rows (`gs://.../snap-v2-202611240348/partial-sync/out`, 277 rows at 04:40Z), v1's complete
146 rows, `lane-d15-report.md`, the live quota, and the code at `5f1cbeec`.

## 1. The work: about 71 core-hours for the 150 dev files

v1's walls are **obsolete** for planning: C4 (`ad716ef4`) gave the det sweep candidates it never had, so v1's
`search_det` median of 28 s became v2's 921 s. Only v2 measures the engine v3 will run.

Per-arm log-log fits on v2's measured dev rows (`wall_s` against input bytes), then summed over all 150 dev files
with each (file, arm) capped at 1800 s, the worst single arm v2 has actually shown:

| arm | v2 rows fitted | R² | measured median | projected total, 150 files |
|---|---|---|---|---|
| input_only | 43 | 0.34 | 95 s | **4.3 h** |
| staged | 33 | 0.65 | 142 s | **9.8 h** |
| search_det | 24 | 0.52 | 921 s | **28.0 h** |
| search_ai | 17 | 0.67 | 846 s | **29.3 h** |
| **dev total** | | | | **71.4 core-h** (mean 1714 s/file) |
| clean, all 4 arms, 40 files | 160 (complete) | — | 3 s | **0.29 core-h** |

Cross-check: the 17 dev files with all four arms measured average 1719 s, against the model's 1714 s. Two
independent routes to the same number, so I trust ~71 core-h to about ±25%.

**The coordinator's 1.4 rows/min does not extrapolate to 7 h.** The shard plan is longest-first, so every shard is on
its largest file first and the early rate is the worst the wave will show. 71.4 core-h over 26 dev shards is ~2.7 h,
not 6.7 h. v2 is slow, not stuck.

Two facts that bound any schedule:
- **Longest single (file, arm) ≈ 30 min** (v2 max 1784 s; D15 saw 2006 s). The wall is soft, so no scheduler and no
  extra VM can cut it. It only has to be *hidden*, which longest-first does: start it at t=0 and 30 < 60.
- **36 of 150 files** are projected to have an arm over 20 min, **24** to hit the 1800 s cap.

## 2. The ceiling nobody can design around: C2D quota and one VM's size

```
C2D_CPUS us-central1: limit 100, usage 72 (v1r 16 + v2 56), free 28
CPUS     us-central1: limit 200, usage 164   (the regional aggregate, all families)
```

- A single c2d VM under a 100-vCPU quota tops out at **c2d-standard-56**; the next size is 112. **So one VM cannot
  exceed 56 vCPU today, and the 1-hour target is impossible on one VM.** v2 proves it: 56 vCPU is 2.7 h of work.
- v1r and v2 must END before v3 launches, or v3 has 28 vCPU to work with.
- Memory is 4 GB/vCPU. v2 gave each shard 2 vCPU = 8 GB. The largest dev input is 17 MB of STEP.

## 3. The design

**(a) One wave group per arm, each with its own shard count.** Today a shard runs all four arms on each of its files
in sequence, so the slowest shard is a sum of four arms over ~6 files — that is both the 2.7 h and v1r's 3.5 h
straggler. The arms are independent (`arms.run_file` re-derives everything per arm; the search arms run their own
staged healer), so each arm can be its own set of shards, sized to its own work:

| group | work | shards | wall | why that many |
|---|---|---|---|---|
| dev search_ai | 29.3 h | **37** | 47 min | 37 is a hard ceiling, see (d) |
| dev search_det | 28.0 h | 38 | 44 min | no model, so free to scale |
| dev staged | 9.8 h | 14 | 42 min | |
| dev input_only | 4.3 h | 6 | 43 min | |
| clean, all arms | 0.29 h | 2 | 9 min | unchanged |
| **total** | **71.7 h** | **97** | **~48 min** | |

`--arms` already exists and run_arm already accepts a subset of a wave's arms, so no new WaveDefs are needed. Each
group needs its **own `wave_id` and `--n-shards`**, because collect/report read `shard-I-of-N` for one N per wave id.

**(b) Longest-first on v2's per-arm walls.** `evalspec.build_shard_plan` is already LPT, so the planner's algorithm
is right; its *costs* are wrong. `prep.py` feeds it v1's all-four-arm seconds, and only for files where all four arms
are present (17 of 150 in v2's partial rows). Per-arm costs from v2 use 117 measured rows instead of 17, and each
group is then packed by its own arm's cost. Diff in section 5.

**(c) Four VMs, 194 vCPU, one machine type.** 97 shards × 2 vCPU = 194: `3 × c2d-standard-56 + 1 × c2d-standard-32`
= 200 vCPU. Keep every VM the same family — the rows carry `machine_type`, and mixing c2d with n2/c3 to dodge the
C2D sub-quota would make `wall_s` incomparable between files in the same benchmark.

**(d) search_ai cannot exceed 37 shards** without an explicit override. `model_throttle.wave_max_in_flight`
('openai:gpt-6-astra', share 0.05) = **37**; `for_shard` raises `ThrottleConfigError` as soon as
`37 // n_shards < 1`, i.e. at 38 (lane B11). So 37 shards (47 min) is the floor for search_ai unless the launch
passes `--allow-in-flight-floor`, which is recorded in provenance and means up to N calls in flight against a
37-call ceiling. **My recommendation: stay at 37 and let search_ai set the wave's 48-minute wall.** If the
coordinator wants under 45 min, that is a deliberate route-limit decision, not a scheduling one.

## 4. The numbers to approve before launch

| | VMs | vCPU | shards | run wall | total wall | VM-h | $ at $0.047/vCPU-h | $ at $0.38/vCPU-h |
|---|---|---|---|---|---|---|---|---|
| **recommended** (needs quota 200) | 4 | 194/200 | 97 | 48 min | **~62 min** | 4.6 | **$10.8** | $87 |
| fallback A, today's quota, 2 vCPU/shard | 2 | 100 | 50 | 93 min | ~107 min | 3.6 | $8.4 | $68 |
| fallback B, today's quota, 1 vCPU/shard | 2 | 97 | 97 | 48 min × penalty | unknown | ~3.7 | $8.6 | $70 |
| v2 today, for comparison | 1 | 56 | 28 | ~2.7 h | ~2.9 h | 2.9 | $6.8 | $55 |

Total wall = 4 min VM create and image pull + 1 min staging (v2 staged 190 files in **13 s**) + run + ~5 min
report/triage + margin.

**The $/vCPU-h discrepancy is yours to settle.** The v2 profile records `VM_USD_PER_H=2.63` for c2d-standard-56,
which is **$0.047/vCPU-h**; the brief says ~$0.38/vCPU-h, 8× higher. Either way this is far inside
`MAX_VM_HOURS = 600` and nowhere near binding; the ledger is model spend only ($3.14 of $49.88 on v2) and the arm
split does not change it.

**Quota.** The recommended plan needs **C2D_CPUS raised from 100 to 200 in us-central1**, requested now so it is in
place before v1r and v2 release their 72. Nothing else in the plan needs an approval I can see.

**Fallback B needs a 10-minute pilot first.** 1 vCPU per shard halves each shard's core and its RAM (4 GB).
Both the contention penalty and the OOM risk on the 17 MB parts are unmeasured, so B is only honest after a pilot
of the ~6 largest files at 1 vCPU. I would not launch a 150-file wave on an unmeasured penalty.

## 5. The diff I am asking for (prep.py, the planner's cost source)

Per-arm costs and a plan per group, instead of one plan on four-arm costs from a stale snapshot. It also reads
`rows.partial.jsonl`, so a wave that has not finished can still inform the next one.

```diff
--- a/.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/prep.py
+++ b/.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1r/prep.py
@@
-def prior_costs(root: Path | None, wall: float) -> dict[str, float]:
-    """sha256 -> all-four-arm seconds measured in an earlier snapshot (an excluded arm counted at THIS wall)."""
+def prior_costs_per_arm(root: Path | None, wall: float) -> dict[str, dict[str, float]]:
+    """sha256 -> {arm: measured seconds} from an earlier snapshot (an excluded arm counted at THIS wall).
+
+    PER ARM, and per arm INDEPENDENTLY: a file is useful for packing search_det even when its search_ai row is
+    missing. `rows.partial.jsonl` is read too, so an unfinished wave still informs the next one (B14)."""
     if root is None or not root.exists():
         return {}
     per: dict[str, dict[str, float]] = defaultdict(dict)
-    for f in root.glob("*/shard-*/rows.json"):
-        for r in json.loads(f.read_text()):
-            per[r["sha256"]][r["arm"]] = float(r["wall_s"])
+    for f in root.glob("*/shard-*/rows.json"):
+        for r in json.loads(f.read_text()):
+            per[r["sha256"]][r["arm"]] = float(r["wall_s"])
+    for f in root.glob("*/shard-*/rows.partial.jsonl"):
+        for line in f.read_text().splitlines():
+            if line.strip():
+                r = json.loads(line)
+                per[r["sha256"]].setdefault(r["arm"], float(r["wall_s"]))
     for f in root.glob("*/shard-*/EXCLUDED.json"):
         for e in json.loads(f.read_text()):
-            per[e["sha256"]][e["arm"]] = wall
-    return {s: sum(a.values()) for s, a in per.items() if set(ARMS4) <= set(a)}
+            per[e["sha256"]].setdefault(e["arm"], wall)
+    return dict(per)
```

Then, where the plan is built, one plan per group instead of one per split. `GROUPS` is the arm split of section 3;
`--group-shards` replaces `--dev-shards` for dev:

```diff
+#: (group name, the arms it runs, the split it runs on). One shard plan, one wave id and one --n-shards EACH.
+GROUPS = (("dev_ai", ("search_ai",), "dev"), ("dev_det", ("search_det",), "dev"),
+          ("dev_staged", ("staged",), "dev"), ("dev_input", ("input_only",), "dev"),
+          ("clean", ARMS4, "clean"))
@@
-    for k in ("dev", "clean"):
+    for group, arms, k in GROUPS:
         costs, source = {}, {"prior": 0, "size_model": 0}
         for f in files[k]:
             s = f["sha256"]
-            if s in prior:
-                costs[s] = prior[s]
-                source["prior"] += 1
+            measured = [prior[s][a] for a in arms if s in prior and a in prior[s]]
+            if len(measured) == len(arms):
+                costs[s] = sum(measured)          # this GROUP's arms only
+                source["prior"] += 1
             else:
-                costs[s] = (k_dev * ev.predicted_cost_s(staged[s][0])) if k == "dev" else clean_default
+                # the size model, scaled by this group's share of the four-arm calibration
+                costs[s] = (k_dev * SHARE[group] * ev.predicted_cost_s(staged[s][0])) if k == "dev" \
+                    else clean_default
                 ...
-        n = min(shards[k], len(files[k]))
+        n = min(shards[group], len(files[k]))
+        if "search_ai" in arms:                   # lane B11: 37 // n < 1 is refused at n >= 38
+            n = min(n, ev_throttle_ceiling(a.route))
```

`SHARE` is each group's measured fraction of the four-arm total (input_only 0.06, staged 0.14, search_det 0.39,
search_ai 0.41 from section 1), so a file with no prior row is still packed sensibly.

`vm_run.sh` then launches one container per (group, shard) with `--arms <the group's arms> --wave-id <per group>
--n-shards <the group's n>`, and `post.py` needs to accept several (wave_id, n) pairs per split instead of one, or be
run once per group with the reports merged. Both are the runner's files, so I have written no code for them.

## 6. What I did NOT propose, and why

- **A hard search wall.** Cutting the soft wall to a real one would cut the search arms from 57 core-h to perhaps 25
  and make the whole thing easy. It also changes how many expansions every file gets, so it changes the benchmark's
  results, not just its speed. D15 already has it in front of the owner; it is not a throughput decision.
- **Dropping an arm or files.** The comparison is the point of the snapshot.
- **A measurable but unproven lever: the arms repeat each other's work.** Every arm re-judges the input, and both
  search arms re-run the staged healer, so the input verdict is paid up to 4× and the staged healer up to 3×. If the
  shared verdict cache never hits across arms, that is up to **32 of the 71 core-h**; if it hits, it is already
  counted in my fit and there is nothing to win. The rows carry no cache hit/miss counter and I could not settle it
  from the 465 cached step digests in v2's bucket. **One counter in a 10-minute probe would decide it**, and it is
  worth doing before v4 — a 45% cut would beat every scheduling change in this report. I did not assume it in any
  number above.
