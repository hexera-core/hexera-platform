# Lane D report: re-probe launch readiness, measurement, wave.py

Branch sdd/lane-d, rebased on the feature tip. No VM launched, no model called.

## Re-probe flow (job 1)
- probe_pool runs the FULL verdict on every file; the cheap-first pass is gone (plan amendment 19:15).
- Inputs staged and sha-verified: 10,887 / 10,887 under gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/inputs; pool.json uploaded (md5 match).
- Two preflight bugs that would have refused the real launch: `ls --recursive` over 10,887 prefixes ran past 600 s (runner limit 120 s; flat glob takes 4.2 s); and the Fusion 360 path heuristic matched ABC id `f36073a...`. Both fixed with tests. preflight.py now passes all 10 checks.
- C2D quota: dev-crsv4-gate-2 (c2d-standard-16) holds 16 of 100. A live 96-shard wave needs it deleted; otherwise 80 shards. Dry runs do not check quota.

## Measurement on the current tip (job 2)
59 diagonal-stratified files, 8 shards pinned to 4 cores' HT pairs (c2d-standard-8 shape), --cpus=1, cold cache, at 5eefb130 (Lane A speed-up in; no verdict/worker_pool change since). Data in devtools/validation/corpora/v4/measure_lane_d_20261123/.
- median 25.5 s, mean 136 s, p90 620 s, max 904 s; file-wall (900 s) timeouts 3/59 = 5.1%.
- 1 vCPU per file holds: cgroup CPU/wall 1.00 (median and max), 1.2 s throttled in 8,023 s; peak memory 2.8 GB per shard.
- Pool-weighted (by file size): 336 vCPU-h = 111 s/file. Simulated 96 shards, cost-balanced: wall 5.2 h (p90 5.6), 54.8 VM-h (p90 56.5), $20.7 (p90 $21.3) at $0.377/VM-h (billing catalog). Hash sharding: 5.5 h, 56.6 VM-h, $21.3. 80 shards cost-balanced: 6.0 h, 53.4 VM-h, $20.1.
- RISK: excluded files (timeouts + check_valid 300 s) are ~8.7% pool-weighted (6 events, wide interval), over collect's 2% limit. Decide before launch: owner-approved --allow-excluded, or longer walls.
- VM shared with two other lanes' containers (load 13-27); our containers were not CPU-starved (CPU/wall 1.00).

## wave.py and cost-balanced shards (job 3)
- Shard plan (evalspec.build_shard_plan, LPT by predicted cost from file size) cut by run_arm --shard-plan, carried by launch_fleet --shard-plan-uri -> v4-shard-plan -> vm_startup.sh, checked by collect/report --shard-plan with a digest in every PROVENANCE.
- wave.py: preflight -> stage -> launch -> watch (relaunch only failed/VM-less shards, deadline = launch + max-run-hours) -> collect -> triage -> report; steps.jsonl mirrored to the bucket. Model cap must equal the ledger's enforced cap; held-out needs its frozen commit.
- Tests: 3-file fake end-to-end through real run_arm arms mode, triage.py and report.py with one shard failing once; real wave.py --dry-run against GCS passes.
