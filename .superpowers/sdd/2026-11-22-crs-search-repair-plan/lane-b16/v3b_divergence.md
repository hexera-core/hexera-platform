# B16: search_ai against search_det, per paired file

Wave snap-v3b-202611240606 on e2b25ddf (the repaired frontier + C11), PARTIAL: dev has 10 search_ai and 17 search_det rows so far; clean is complete at 40 each.

Paired files (both arms conclusive, both traces present): **45**. Files where an AI-proposed op is on the delivered path: **0**.

| file | start same | diverge at | det took | ai took | det had ai's op | better end | det clean | ai clean | AI candidates |
|---|---|---|---|---|---|---|---|---|---|
| ffd12b0ffcc8c80c_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| e9012a817ddde919_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 79fc27bf121c4ec9_step_000.step | yes | never | - | - (-) | - | equal | False | False | ACCESS 7, NO-OP(declined) 3, DUPLICATE 1 |
| 7a695984d76da473_step_005.step | yes | never | - | - (-) | - | equal | True | True | none |
| 49d1a6af3497f12c_step_006.step | yes | never | - | - (-) | - | equal | True | True | none |
| d9904386d953c95a_step_001.step | yes | never | - | - (-) | - | equal | True | True | none |
| 9ffd2eb17e6eb519_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 4ed19e0ff68bb62f_step_008.step | yes | never | - | - (-) | - | equal | True | True | JUDGEMENT 2 |
| edc90e268161977c_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 7a983345462cdf84_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| fc0a8b7a0b297dad_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 4eca45b53e26a6ba_step_003.step | yes | never | - | - (-) | - | equal | True | True | none |
| 89ce5e53d0f17031_step_011.step | yes | never | - | - (-) | - | equal | True | True | none |
| 33eeb1d44e68f20a_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 4e4b4f49b1f4da50_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 4cd9630fcbd7e5de_step_001.step | yes | never | - | - (-) | - | equal | True | True | none |
| a359350f72d61437_step_010.step | yes | never | - | - (-) | - | equal | True | True | none |
| 0c6d3ae6919b5acc_step_008.step | yes | never | - | - (-) | - | equal | True | True | none |
| d1f1d24023e136c8_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 4a1c28eff4462339_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| b52d5eab89fd80bb_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| b021c4b8a4175c11_step_001.step | yes | never | - | - (-) | - | equal | True | True | none |
| 37e1351054364cae_step_005.step | yes | never | - | - (-) | - | equal | True | True | none |
| 98e973a4f1f07a25_step_003.step | yes | never | - | - (-) | - | equal | True | True | none |
| c4a89913e7f34ffc_step_001.step | yes | never | - | - (-) | - | equal | True | True | none |
| be88c66b7d2ae3db_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 4477f9a84d0a86f3_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| e800be28a82d1ced_step_001.step | yes | never | - | - (-) | - | equal | True | True | none |
| 6e0f274a15e4f30a_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 332d01e2abd5d392_step_002.step | yes | never | - | - (-) | - | equal | True | True | none |
| a45b8411f42b0522_step_006.step | yes | never | - | - (-) | - | equal | True | True | none |
| 1f8ae92e51b809be_step_003.step | yes | never | - | - (-) | - | equal | True | True | none |
| 7e68f10fff6d11c4_step_003.step | yes | never | - | - (-) | - | equal | True | True | none |
| 7e864b0f65045295_step_068.step | yes | never | - | - (-) | - | equal | True | True | none |
| b8d85e130613221d_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| db1ce506dd250224_step_002.step | yes | never | - | - (-) | - | equal | True | True | none |
| tions/nist_ftc_11_asme1_rb.stp | yes | never | - | - (-) | - | equal | True | True | none |
| d674155a87d8208f_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| 345b0064a51a8387_step_009.step | yes | never | - | - (-) | - | equal | True | True | none |
| a359350f72d6458a_step_000.step | yes | never | - | - (-) | - | equal | True | True | none |
| b8d61e0f97572aa8_step_032.step | yes | never | - | - (-) | - | equal | True | True | none |
| 98bdbe89669a4f49_step_000.step | yes | never | - | - (-) | - | equal | False | False | DUPLICATE 3, ACCESS 1, JUDGEMENT 1, NO-OP(declined) 1 |
| 5dabbc2bbc26c4f6_step_001.step | yes | never | - | - (-) | - | equal | True | True | none |
| 22115e0f6fe05a45_step_002.step | yes | never | - | - (-) | - | equal | True | True | none |
| 716d73109155df3d_step_020.step | yes | never | - | - (-) | - | equal | True | True | none |

## Aggregate

- better end: {'equal': 45}
- clean pairs (det,ai): {'det=True,ai=True': 43, 'det=False,ai=False': 2}
- first divergence depth: {'none': 45}
- AI candidate verdicts: {'ACCESS': 8, 'NO-OP(declined)': 4, 'DUPLICATE': 4, 'JUDGEMENT': 3}  (ACCESS = det never named that op at that state; JUDGEMENT = det named it there too and the search took something else first)
- ops ONLY the AI proposed: {'conformalise': 1, 'fuse': 1, 'remove_small': 1, 'run_stage': 1, 'split_angle': 1, 'split_by_proximity': 1, 'split_closed': 1, 'unify_same_domain': 1, 'reapproximate': 1, 'rebuild_edge_as_intersection': 1}
- ops only det proposed inside the AI arm: {'defeature': 2, 'orient_and_close': 2, 'rebuild_face': 2, 'split_continuity': 1}
- stop reasons det: {'clean': 43, 'patience': 2}; ai: {'clean': 43, 'patience': 2}

Not paired: 12 - 37b6afa4cf69 one arm only; 3e7c2a1197c3 one arm only; 4edc147fba72 one arm only; 5abbaf0b5508 inconclusive: search_ai; 5e9f31e673fd inconclusive: search_det,search_ai; 615e4cde0518 inconclusive: search_ai; a2807f9a2fef one arm only; bc860ab1a1e1 one arm only; bcf201b55323 inconclusive: search_ai; cbe8c3abc11c inconclusive: search_det,search_ai; e357818b22f8 one arm only; fa37bbd5673b one arm only
