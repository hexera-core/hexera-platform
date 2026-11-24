LANE D22/D23 RESULT: fast profile, fresh cache, walls 1200/900, 15 dev files x 2 search arms + clean controls.
BEFORE = your cold baseline (87d6999da, W1 P1 @1.5 vCPU/file). AFTER = 1da1ac06a, W4 P4 @4 vCPU/file. NOT pure D22/D23: the tip also carries C15 (orientation), C16 (caps both tiers), b' and the single-flight cache.

FILE TIMEOUTS: 15 -> 5 (det 8 -> 2, ai 7 -> 3). 33% of rows -> 11%. Target is 5%: not yet.
CLEAN: det 7 -> 7, ai 7 -> 7, NO LOSS. Other exclusions: 0 -> 2, bb0fe0d1c8 both arms: CheckerUnavailable, check_valid exceeded 900 s (layer 1, NOT a mesher). Before, it was a file_timeout in input_verdict; now its input reaches layer 1's 900 s call budget first.
PER PHASE, all dev rows, from PHASE end lines (n ended / total s / mean / max):
- input_verdict 30 / 12117 / 404 / 1200 -> 30 / 2566 / 86 / 901 (-79% total)
- staged_healer 26 / 3506 / 135 / 633 -> 28 / 6325 / 226 / 1198 (UP: more files reach it, and it's the new top killer)
- confirm_staged 22 / 1372 / 62 / 267 -> 26 / 577 / 22 / 179
- search 20 / 10402 / 520 / 1705 -> 26 / 9104 / 350 / 713 (max no longer overruns: the 1705/2248 s overruns are gone)
- output_verdict 15 / 18 -> 23 / 12
PHASE OF DEATH per timed-out (file, arm), before -> after:
- BEFORE 15: input_verdict 4 (32ca3e9a6e x2, bb0fe0d1c8 x2), staged_healer 4 (5accc12fa4 x2, 7e573047c1 x2), confirm_staged 2 (695d74ca7f x2), search 5 (4c1fc7d557 det, 722c122c9b x2, a2807f9a2f x2; overrun to 1368-2248 s).
- AFTER 5: staged_healer 2 (32ca3e9a6e x2, both at 1200.9 s, now past input_verdict), search 3 (5accc12fa4 x2 at 1248/1350 s, 722c122c9b ai at 1206 s).
- Fixed outright: 4c1fc7d557, 695d74ca7f, 7e573047c1, a2807f9a2f, bb0fe0d1c8 (the last moved to a layer-1 exclusion, see above).
SPEND: actual 1 VM x 56 vCPU x 0.76 h = 42.6 vCPU-h -> 3 VMs x 32 x 1.0 h = 96 vCPU-h (2.25x). The plan called it 28.9 -> 81.8.
AT EQUAL VM SPEND: per-file timeouts depend on cores PER FILE, not VM count. Spending only 42.6 vCPU-h at 4 vCPU/file packs fewer files at once, so the same 5 timeouts and the wave takes ~2.25x the wall clock ($1.36 -> ~$3.1 per fast wave). If the wave's wall clock must hold, it's +125% VM spend for 15 -> 5 timeouts.
WHAT REMAINS (next levers):
(1) staged_healer is now the top killer and runs on CHEAP verdicts at P=1 with W idle. Make the healer's transactions use P too (P per call: staged cheap = P), or run its stages' cheap verdicts in parallel.
(2) search at 1200-1350 s on 5accc12fa4 and 722c122c9b: the search wall is 900 s, yet the file ends 300-450 s later. That's input+staged before it, not overrun; A16's wall-aware calls should help.
(3) bb0fe0d1c8: layer-1 check_valid > 900 s; A17's Boolean memo may cover it.
NEXT RUN: the tip, as you said.
