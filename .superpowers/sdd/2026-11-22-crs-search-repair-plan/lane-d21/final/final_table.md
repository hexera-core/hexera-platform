# D21 oracle table - 150 rows for 150 inputs
commit 3669ea7ae12d; netgen: first STEP root, except on its own no-solids read (verdict .7, D19 A); every-root NOT applied
CLEAN below is under FIRST-ROOT netgen (the sweep's commit). The tip (.8) meshes every root: a multi-root winner (flagged MR) may not be clean there. D20: 0 of 92 multi-root INPUTS went clean->unclean; outputs unmeasured.
VMs: us-east1, ephemeral external IPs (no NAT there), self-deleting.

status: {'complete': 56, 'killed': 76, 'staged_clean': 14, 'clean_at_input': 4}

## CLEAN reached by an op: 31 files (24 with an ungamed clean)
- d6d8f9b343 3 with PMI/nist_ctc_04_asme1_ap203.stp | staged []
    [SOLVED by a v4 arm] d1 fill_loop {"continuity": "G0"} -> clean=True passes=4 vol x1 bbox-axis x1
- 71777c28da P-Files/nist_stc_06_asme1_ap242-e3.stp | staged ['gmsh_alt:low_quality']
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 90} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 60} -> clean=True passes=4 vol x1 bbox-axis x1
- cdb59a085a geometry only/nist_ctc_04_asme1_rd.stp | staged None
    [whole-part defeature (OFF in the arms)] d2 defeature+defeature {"groups": [[0], [1], [2, 3, 4], [5, 6, 7], [8], [9], [10], [11], [12] -> clean=True passes=4 vol x1.01 bbox-axis x1
    [stage] d2 defeature+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1.01 bbox-axis x1
    [WALL (det had it; search_det stopped on the wall)] d2 remove_small+split_by_proximity {"face_pairs": [["F483", "F485"], ["F483", "F500"], ["F382", "F544"],  -> clean=True passes=4 vol x1 bbox-axis x1
- 78cbab541a MR 58b7efcfda98a4102b812175_step_008.step | staged ['gmsh_prod:other', 'gmsh_default:low_quality']
    [stage] d2 defeature+run_stage {"stage": "bodies"} -> clean=True passes=4 vol x1.02 bbox-axis x1
    [stage] d2 defeature+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1.02 bbox-axis x1
- bcf201b553 MR d96abb3842cf81ccbe5b1c09_step_009.step | staged ['gmsh_prod:other', 'gmsh_default:low_quality']
    [stage] d2 defeature+run_stage {"stage": "bodies"} -> clean=True passes=4 vol x1.02 bbox-axis x1
    [stage] d2 defeature+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1.02 bbox-axis x1
- 6233fd0122 58c0423eb4ee370fd6253b3d_step_037.step | staged ['gmsh_default:edge_through_face', 'gmsh_prod:other', 'gmsh_alt:no_volume', 'netgen_occ:netgen_meshing_failed']
    [SOLVED by a v4 arm] d2 defeature+defeature {"source": "located"} -> clean=True passes=4 vol x0.624 bbox-axis x1 GAMING ['volume x0.624']
- d2cb9938a2 475d3d5812881065217cd6c1_step_002.step | staged ['gmsh_default:low_quality']
    [SOLVED by a v4 arm] d1 split_by_proximity {"face_pairs": [["F18", "F34"], ["F18", "F35"], ["F6", "F33"], ["F6",  -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_by_proximity+run_stage {"stage": "normalise"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_by_proximity+run_stage {"stage": "simplify"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_by_proximity+run_stage {"stage": "simplify"} -> clean=True passes=4 vol x1 bbox-axis x1
- 75f72ae626 fc467dbe3e5e9c8b99cbae04_step_008.step | staged ['gmsh_default:overlapping_facets', 'gmsh_alt:no_volume']
    [SOLVED by a v4 arm] d2 defeature+defeature {"source": "located"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+defeature {"groups": [[0]], "source": "recognised"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_angle+defeature {"source": "located"} -> clean=True passes=4 vol x1 bbox-axis x1
- 7ab61ea7d6 C Definitions/nist_ftc_10_asme1_rb.stp | staged ['gmsh_default:low_quality']
    [SOLVED by a v4 arm] d1 unify_same_domain {"keep": [], "scope": "targets"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_by_proximity {"face_pairs": [["F673", "F700"], ["F672", "F700"], ["F673", "F701"],  -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], [13, 14, 15, 1 -> clean=True passes=4 vol x1.06 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_angle+defeature {"groups": [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], [13, 14, 15, 1 -> clean=True passes=4 vol x1.06 bbox-axis x1
- d92ecb192f 58c2cd0d85eca50f9755927c_step_144.step | staged ['gmsh_prod:low_quality']
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x1 bbox-axis x1
- d4158ed46a C Definitions/nist_ctc_03_asme1_rc.stp | staged ['gmsh_prod:low_quality', 'gmsh_default:low_quality', 'gmsh_alt:low_quality']
    [SOLVED by a v4 arm] d2 defeature+defeature {"source": "site"} -> clean=True passes=4 vol x1.01 bbox-axis x0.723 GAMING ['bbox axis x0.723']
- 5e30f0903f geometry only/nist_ftc_06_asme1_rd.stp | staged ['gmsh_prod:low_quality', 'gmsh_default:overlapping_facets']
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0], [1, 2, 3], [4], [5], [6], [7], [8], [9], [10], [11],  -> clean=True passes=4 vol x1 bbox-axis x0.917
    [SOLVED by a v4 arm] d2 split_angle+defeature {"groups": [[0], [1, 2, 3], [4], [5], [6, 7, 8, 9], [10], [11], [12],  -> clean=True passes=4 vol x1 bbox-axis x0.917
    [SOLVED by a v4 arm] d2 split_angle+defeature {"groups": [[0], [1, 2, 3], [4], [5], [6, 7, 8, 9, 10, 11], [12], [13] -> clean=True passes=4 vol x1 bbox-axis x0.917
    [SOLVED by a v4 arm] d2 remove_small+defeature {"groups": [[0], [1, 2, 3], [4], [5], [6], [7], [8], [9], [10], [11],  -> clean=True passes=4 vol x1 bbox-axis x0.917
- 448d830578 d89a52b42c828ded4e4cae62_step_028.step | staged ['gmsh_alt:low_quality']
    [SOLVED by a v4 arm] d1 split_by_proximity {"face_pairs": [["F284", "F435"], ["F414", "F415"], ["F414", "F416"],  -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_by_proximity {"face_pairs": [["F284", "F435"], ["F414", "F415"], ["F414", "F416"],  -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_by_proximity {"face_pairs": [["F284", "F435"], ["F414", "F415"], ["F414", "F416"],  -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 60} -> clean=True passes=4 vol x1 bbox-axis x1
- 798565ef85 occt/bm2_ec_leverrod-A.stp | staged ['gmsh_prod:low_quality', 'gmsh_default:low_quality', 'gmsh_alt:low_quality']
    [det had it] d2 split_by_proximity+defeature {"source": "site"} -> clean=True passes=4 vol x0.0583 bbox-axis x0.008 GAMING ['volume x0.0583', 'bbox axis x0.008']
    [det had it] d2 split_by_proximity+defeature {"source": "site"} -> clean=True passes=4 vol x0.0583 bbox-axis x0.008 GAMING ['volume x0.0583', 'bbox axis x0.008']
- 541e13051f MR 90f123a7b7e505aa9a785623_step_000.step | staged ['gmsh_prod:low_quality', 'gmsh_default:low_quality', 'netgen_occ:low_quality']
    [SOLVED by a v4 arm] d2 run_stage+defeature {"source": "site"} -> clean=True passes=4 vol x0.805 bbox-axis x0.333 GAMING ['bbox axis x0.333']
- 54f1189a15 ce82e436f164059a066d0713_step_000.step | staged ['gmsh_prod:low_quality', 'gmsh_default:edge_through_face']
    [whole-part defeature (OFF in the arms)] d1 defeature {"groups": [[0], [1], [2], [3], [4], [5], [6], [7], [8], [9], [10], [1 -> clean=True passes=4 vol x1.09 bbox-axis x1
    [stage] d2 defeature+run_stage {"stage": "shells"} -> clean=True passes=4 vol x1.09 bbox-axis x1
    [stage] d2 defeature+run_stage {"stage": "solids"} -> clean=True passes=4 vol x1.09 bbox-axis x1
    [stage] d2 defeature+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1.09 bbox-axis x1
- 88130bbc32 24ae031415592b9fdd33eb15_step_001.step | staged ['gmsh_prod:low_quality']
    [SOLVED by a v4 arm] d2 defeature+remove_small {"max_width_mm": 792.749999999993} -> clean=True passes=4 vol x1.18 bbox-axis x1
- 532f9baa4a 64293c310e5d6b9a8b377109_step_019.step | staged None
    [det had it] d1 remove_small {"max_width_mm": 11.889342852723075} -> clean=True passes=4 vol x1 bbox-axis x1
    [det had it] d1 defeature {"source": "site"} -> clean=True passes=4 vol x1 bbox-axis x1
    [stage] d2 remove_small+run_stage {"stage": "simplify"} -> clean=True passes=4 vol x1 bbox-axis x1
    [stage] d2 remove_small+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1 bbox-axis x1
- 7613ed549d 3daa0f06888b329e557a6a76_step_000.step | staged ['gmsh_prod:low_quality', 'gmsh_default:low_quality']
    [SOLVED by a v4 arm] d2 run_stage+defeature {"source": "located"} -> clean=True passes=4 vol x0.546 bbox-axis x0.25 GAMING ['volume x0.546', 'bbox axis x0.25']
    [SOLVED by a v4 arm] d2 defeature+defeature {"source": "located"} -> clean=True passes=4 vol x0.546 bbox-axis x0.25 GAMING ['volume x0.546', 'bbox axis x0.25']
- 41808f5985 1519f83024d29ad8f5872d22_step_010.step | staged ['gmsh_default:overlapping_facets', 'gmsh_prod:other', 'gmsh_alt:no_volume', 'netgen_occ:netgen_meshing_failed']
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3]], "source": "recognised"} -> clean=True passes=4 vol x1.01 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]], "source": "re -> clean=True passes=4 vol x1.01 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+run_stage {"stage": "simplify"} -> clean=True passes=4 vol x1.01 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+run_stage {"stage": "condition"} -> clean=True passes=4 vol x1.01 bbox-axis x1
- ddd86130e5 3e7304a7ff86e7407d42d324_step_034.step | staged ['gmsh_default:low_quality']
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x0.904 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"source": "site"} -> clean=True passes=4 vol x0.904 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0], [1], [2], [3]], "source": "recognised"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 remove_small+remove_small {"max_width_mm": 1003.4650543656276} -> clean=True passes=4 vol x1 bbox-axis x1
- e79f4b2cf3 MR f0dda3737ed09100b2283db1_step_000.step | staged []
    [SOLVED by a v4 arm] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B3"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 fuse {"fuzzy_mm": 0.0} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_angle+conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B3"} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_angle+fuse {"fuzzy_mm": 0.0} -> clean=True passes=4 vol x1 bbox-axis x1
- dd9e77d186 3a923e805e65f093905f2748_step_005.step | staged ['netgen_occ:low_quality']
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 60} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 45} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 1 -> clean=True passes=4 vol x1.03 bbox-axis x1
- 1d74529e78 MR 58b5a4ac33248d103c82ca1d_step_000.step | staged []
    [SOLVED by a v4 arm] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B1"} -> clean=True passes=4 vol x0.935 bbox-axis x1
    [SOLVED by a v4 arm] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B2"} -> clean=True passes=4 vol x0.935 bbox-axis x1
    [SOLVED by a v4 arm] d1 fuse {"fuzzy_mm": 0.0} -> clean=True passes=4 vol x0.935 bbox-axis x1
- c79b8deb8e 1519f83024d29ad8f5872d22_step_002.step | staged ['gmsh_default:overlapping_facets', 'gmsh_prod:other', 'gmsh_alt:no_volume', 'netgen_occ:netgen_meshing_failed']
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x1.1 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3]], "source": "recognised"} -> clean=True passes=4 vol x1.01 bbox-axis x1
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]], "source": "re -> clean=True passes=4 vol x1.01 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+run_stage {"stage": "simplify"} -> clean=True passes=4 vol x1.01 bbox-axis x1
- 5efc2c25c2 MR 3df4aeffe01eb64ef9a65e8f_step_019.step | staged ['gmsh_alt:low_quality']
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 1 -> clean=True passes=4 vol x1.04 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_by_proximity+split_by_proximity {"face_pairs": [["F103", "F85"], ["F104", "F85"], ["F131", "F71"], ["F -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_by_proximity+defeature {"groups": [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 1 -> clean=True passes=4 vol x1.04 bbox-axis x1
    [SOLVED by a v4 arm] d2 split_by_proximity+split_angle {"max_deg": 60} -> clean=True passes=4 vol x1 bbox-axis x1
- c7b0fca2f2 f868f0f84bb5ce9c3926e742_step_020.step | staged ['gmsh_prod:low_quality', 'gmsh_default:overlapping_facets', 'gmsh_alt:no_volume']
    [SOLVED by a v4 arm] d1 defeature {"groups": [[0], [1], [2], [3], [4], [5], [6], [7]], "source": "recogn -> clean=True passes=4 vol x1.02 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+defeature {"groups": [[0], [1], [2]], "source": "recognised"} -> clean=True passes=4 vol x1.01 bbox-axis x1
    [SOLVED by a v4 arm] d2 defeature+defeature {"groups": [[0], [1], [2], [3]], "source": "recognised"} -> clean=True passes=4 vol x1.02 bbox-axis x1
    [SOLVED by a v4 arm] d2 remove_small+defeature {"groups": [[0], [1], [2], [3], [4], [5], [6]], "source": "recognised" -> clean=True passes=4 vol x1.02 bbox-axis x1
- 0daa964f71 MR 58c07b8e910b980f78da6060_step_003.step | staged ['gmsh_prod:other', 'gmsh_default:edge_through_face', 'gmsh_alt:low_quality']
    [whole-part defeature (OFF in the arms)] d1 defeature {"groups": [[0], [1], [2], [3], [4], [5], [6], [7], [8], [9], [10], [1 -> clean=True passes=4 vol x3.98 bbox-axis x1 GAMING ['volume x3.98']
    [whole-part defeature (OFF in the arms)] d2 defeature+defeature {"groups": [[0], [1], [2], [3], [4], [5], [6], [7], [8], [9], [10], [1 -> clean=True passes=4 vol x3.98 bbox-axis x1 GAMING ['volume x3.98']
- 2f3034493d 58a469534ed19e0ff68bb62f_step_008.step | staged ['gmsh_prod:low_quality', 'gmsh_default:overlapping_facets', 'netgen_occ:netgen_meshing_failed']
    [SOLVED by a v4 arm] d2 remove_small+defeature {"source": "located"} -> clean=True passes=4 vol x0.983 bbox-axis x0.997
    [SOLVED by a v4 arm] d2 remove_small+defeature {"source": "located"} -> clean=True passes=4 vol x0.983 bbox-axis x0.997
- e647162666 589af7a315a8541030f8f61d_step_012.step | staged ['gmsh_default:low_quality', 'gmsh_alt:low_quality']
    [SOLVED by a v4 arm] d1 remove_small {"max_width_mm": 405.29999999999995} -> clean=True passes=4 vol x1.25 bbox-axis x1 GAMING ['volume x1.25']
    [SOLVED by a v4 arm] d1 remove_small {"max_width_mm": 405.29999999999995} -> clean=True passes=4 vol x1.25 bbox-axis x1 GAMING ['volume x1.25']
    [SOLVED by a v4 arm] d1 defeature {"source": "located"} -> clean=True passes=4 vol x1.68 bbox-axis x1 GAMING ['volume x1.68']
    [SOLVED by a v4 arm] d1 defeature {"source": "site"} -> clean=True passes=4 vol x1.86 bbox-axis x1 GAMING ['volume x1.86']
- 3596803faa 5ee09d41b3e9ec327a3ddc07_step_015.step | staged ['gmsh_alt:low_quality']
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 60} -> clean=True passes=4 vol x1 bbox-axis x1
    [SOLVED by a v4 arm] d1 split_angle {"max_deg": 45} -> clean=True passes=4 vol x1 bbox-axis x1

## PASSES added, not clean: 42 files
- 9c6e77a796 MR staged None -> best [whole-part defeature (OFF in the arms)] d1 defeature {"groups": [[0], [1], [2], [3], [4], [5], [6], [7], [8], [9], [10], [1 -> clean=False passes=2 vol x0.977 bbox-axis x1
- 7e62627465 MR staged None -> best [det had it] d1 split_by_proximity {"face_pairs": [["F350", "F351"]], "target_ratio": 0.125} -> clean=False passes=1 vol x1 bbox-axis x1
- 1b6481c3ec MR staged None -> best [whole-part defeature (OFF in the arms)] d1 defeature {"groups": [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11], [12, 13, 14] -> clean=False passes=1 vol x1.68 bbox-axis x1
- 9ceb64b12b MR staged None -> best [taxonomy (gate)] d1 fuse {"fuzzy_mm": 0.0} -> clean=False passes=2 vol x1 bbox-axis x1
- 3e7c2a1197 MR staged None -> best [det had it] d2 fuse+split_continuity {"order": 2} -> clean=False passes=1 vol x0.000132 bbox-axis x0.239
- bff959ced3 MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 remove_small {"max_width_mm": 3198.3} -> clean=False passes=1 vol x1 bbox-axis x1
- cb2862dd7a MR staged None -> best [taxonomy (gate)] d1 fuse {"fuzzy_mm": 0.0} -> clean=False passes=1 vol x0.998 bbox-axis x1
- d45c510969 MR staged None -> best [det had it] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B1"} -> clean=False passes=1 vol x0.695 bbox-axis x1
- 722c122c9b MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 split_by_proximity {"face_pairs": [["F76", "F80"], ["F59", "F509"], ["F76", "F509"], ["F8 -> clean=False passes=1 vol x1 bbox-axis x1
- f8cbd0194a staged None -> best [det had it] d1 remove_small {"max_width_mm": 827.4142673490118} -> clean=False passes=1 vol x5.53 bbox-axis x1
- b30914ae59 MR staged None -> best [taxonomy (gate)] d1 fuse {"fuzzy_mm": 0.0} -> clean=False passes=1 vol x1 bbox-axis x1
- b26a079a5e staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 split_by_proximity {"face_pairs": [["F237", "F255"], ["F237", "F249"]], "target_ratio": 0 -> clean=False passes=1 vol x1 bbox-axis x1
- f56dd3b77d MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 remove_small {"max_width_mm": 32.514873648989195} -> clean=False passes=1 vol x1 bbox-axis x1
- da386833c1 MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 remove_small {"max_width_mm": 32.514873648989195} -> clean=False passes=1 vol x1 bbox-axis x1
- 37b6afa4cf MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d2 split_angle+split_continuity {"order": 2} -> clean=False passes=1 vol x1 bbox-axis x1
- bbd860fa0f MR staged None -> best [stage] d2 split_angle+run_stage {"stage": "simplify"} -> clean=False passes=1 vol x1 bbox-axis x1
- 985de5aaaa MR staged None -> best [stage] d2 split_angle+run_stage {"stage": "simplify"} -> clean=False passes=1 vol x1 bbox-axis x1
- 4c1fc7d557 MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d2 split_angle+split_continuity {"order": 2} -> clean=False passes=1 vol x1 bbox-axis x1
- f9501251fb MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d2 split_angle+split_continuity {"order": 2} -> clean=False passes=1 vol x1 bbox-axis x1
- e63710eae3 MR staged None -> best [stage] d2 split_angle+run_stage {"stage": "simplify"} -> clean=False passes=1 vol x1 bbox-axis x1
- ce546aef9b MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d2 split_angle+split_continuity {"order": 2} -> clean=False passes=1 vol x1 bbox-axis x1
- cbe8c3abc1 MR staged None -> best [taxonomy (gate)] d2 conformalise+conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B1"} -> clean=False passes=1 vol x1 bbox-axis x1
- fa37bbd567 MR staged None -> best [aim (det had op+rung, other faces)] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B5"} -> clean=False passes=1 vol x0.993 bbox-axis x1
- 7e81aa0fa1 MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 unify_same_domain {"keep": [], "scope": "targets"} -> clean=False passes=1 vol x1 bbox-axis x1
- 6892359075 MR staged 0 -> best [WALL (det had it; search_det stopped on the wall)] d1 split_continuity {"order": 2} -> clean=False passes=1 vol x1 bbox-axis x1
- 80c009cb74 MR staged None -> best [taxonomy (gate)] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B1"} -> clean=False passes=1 vol x1 bbox-axis x1
- 6d1c4eab6f MR staged 1 -> best [WALL (det had it; search_det stopped on the wall)] d2 fuse+conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B12"} -> clean=False passes=1 vol x1 bbox-axis x1
- a2807f9a2f MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 remove_small {"max_width_mm": 58.78133947306135} -> clean=False passes=2 vol x0.549 bbox-axis x0.897
- 3eb61cabb3 MR staged None -> best [aim (det had op+rung, other faces)] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B2"} -> clean=False passes=1 vol x0.998 bbox-axis x1
- b102922dbd MR staged None -> best [WALL (det had it; search_det stopped on the wall)] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B2"} -> clean=False passes=2 vol x0.999 bbox-axis x1
- 7917f3122c MR staged 2 -> best [whole-part defeature (OFF in the arms)] d1 defeature {"groups": [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, 1 -> clean=False passes=4 vol x1 bbox-axis x1
- 8c6a3f2186 MR staged 1 -> best [det had it] d2 defeature+defeature {"source": "located"} -> clean=False passes=2 vol x0.999 bbox-axis x1
- 3e727f1145 MR staged 2 -> best [WALL (det had it; search_det stopped on the wall)] d1 remove_small {"max_width_mm": 75.09292199385247} -> clean=False passes=2 vol x0.622 bbox-axis x1
- a0bc551656 staged None -> best [WALL (det had it; search_det stopped on the wall)] d2 split_closed+to_canonical {"tol_mm": 0.05} -> clean=False passes=2 vol x1 bbox-axis x1
- dc2db5e786 staged 1 -> best [WALL (det had it; search_det stopped on the wall)] d1 defeature {"source": "located"} -> clean=False passes=2 vol x0.972 bbox-axis x0.993
- 75e9d59b46 staged 2 -> best [det had it] d1 defeature {"source": "located"} -> clean=False passes=3 vol x1 bbox-axis x1
- 4f0996fa76 staged None -> best [WALL (det had it; search_det stopped on the wall)] d2 fuse+defeature {"source": "located"} -> clean=False passes=2 vol x0.994 bbox-axis x1
- cfe077313b staged None -> best [taxonomy (gate)] d1 fuse {"fuzzy_mm": 0.0} -> clean=False passes=1 vol x1 bbox-axis x1
- 0df08227f5 MR staged 1 -> best [det had it] d1 defeature {"source": "located"} -> clean=False passes=2 vol x0.737 bbox-axis x1
- e91b70f1e3 MR staged 0 -> best [det had it] d2 defeature+rebuild_edge_as_intersection {} -> clean=False passes=1 vol x0.986 bbox-axis x1
- e357818b22 MR staged None -> best [taxonomy (gate)] d1 conformalise {"fuzzy_mm": 0.0, "mode": "cut_by:B1"} -> clean=False passes=2 vol x1 bbox-axis x1
- b25999e0f8 staged 0 -> best [det had it] d1 defeature {"source": "located"} -> clean=False passes=1 vol x1 bbox-axis x1

## NOTHING TOUCHED: 59 files (2 of them SOLVED by a v4 arm)
- 97314ccbe4 MR complete valid=False failing=[] tried=37 outcomes={'applied': 11, 'declined': 26} stopped_at=None
- 782680a429 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- f027b94e6e MR killed valid=None failing=None tried=None outcomes={'declined': 6, 'applied': 8, 'invalid': 1} stopped_at=depth1 16/119 run_stage
- d0053d0870 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 7918be4052 MR killed valid=None failing=None tried=None outcomes={'declined': 123, 'applied': 22} stopped_at=depth1 146/164 conformalise
- 9d0d49a626 MR killed valid=None failing=None tried=None outcomes={'applied': 9, 'declined': 29, 'failed': 2} stopped_at=depth1 41/214 fuse
- d9b92b7973 MR killed valid=None failing=None tried=None outcomes={'applied': 1} stopped_at=depth1 2/24 fill_loop
- e533d000aa MR killed valid=None failing=None tried=None outcomes={'declined': 211, 'failed': 5, 'applied': 3, 'error': 2, 'invalid': 2} stopped_at=depth2 14/210 run_stage
- 9f6e3964cb killed valid=None failing=None tried=None outcomes={'declined': 24, 'failed': 5, 'applied': 10} stopped_at=depth2 11/26 reapproximate
- 1e6f8d8e11 MR killed valid=None failing=None tried=None outcomes={'applied': 35, 'declined': 387, 'error': 4, 'invalid': 2} stopped_at=depth2 10/210 orient_and_close
- e8ed99fcd8 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 68ef8f5e66 MR killed valid=None failing=None tried=None outcomes={'applied': 45, 'declined': 41, 'failed': 1} stopped_at=depth1 88/164 fuse
- 5f8ac4d73a MR killed valid=None failing=None tried=None outcomes={'applied': 11, 'declined': 15} stopped_at=depth2 1/15 fill_loop
- 6aa493da5b MR killed valid=None failing=None tried=None outcomes={'failed': 3, 'applied': 6, 'error': 1, 'declined': 15} stopped_at=depth1 26/142 reapproximate
- f3878a48db MR killed valid=None failing=None tried=None outcomes={'applied': 40, 'declined': 183, 'failed': 4} stopped_at=depth2 109/124 conformalise
- af9fc48e75 MR killed valid=None failing=None tried=None outcomes={'declined': 20, 'applied': 4} stopped_at=depth1 25/135 reapproximate
- 00911ef0c1 MR killed valid=None failing=None tried=None outcomes={'declined': 29, 'applied': 16, 'failed': 2} stopped_at=depth1 48/125 conformalise
- 0522f19877 killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 09f296a1e4 killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 733142fc27 MR killed valid=None failing=None tried=None outcomes={'applied': 78, 'declined': 66, 'failed': 4} stopped_at=depth2 36/113 conformalise
- 85ab4da1c7 killed valid=None failing=None tried=None outcomes={'declined': 20, 'applied': 19, 'failed': 6} stopped_at=depth2 19/28 run_stage
- 117ecb94ab killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 6109dea12c MR killed valid=None failing=None tried=None outcomes={'declined': 21, 'applied': 11, 'invalid': 1} stopped_at=depth1 34/117 conformalise
- 5accc12fa4 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 3f2c637226 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- f1eef4cd07 killed valid=None failing=None tried=None outcomes={'applied': 24, 'declined': 46, 'failed': 7, 'error': 3} stopped_at=depth2 14/26 defeature
- 5e9f31e673 MR killed valid=None failing=None tried=None outcomes={'applied': 161, 'failed': 5, 'declined': 595} stopped_at=depth2 6/152 defeature
- 0e8e2236c7 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- eda22c2f6e killed valid=None failing=None tried=None outcomes={'declined': 15, 'error': 1, 'applied': 5, 'failed': 1} stopped_at=depth1 23/28 run_stage
- 4edc147fba MR killed valid=None failing=None tried=None outcomes={'declined': 70, 'applied': 133, 'failed': 16} stopped_at=depth2 57/65 conformalise
- 36399c6239 killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 448333aaad MR killed valid=None failing=None tried=None outcomes={'declined': 74, 'applied': 24, 'failed': 9} stopped_at=depth2 36/72 rebuild_face
- eb3bc130c6 killed valid=None failing=None tried=None outcomes={'declined': 22, 'applied': 13, 'failed': 2} stopped_at=depth2 10/34 defeature
- 4be89ef716 MR killed valid=None failing=None tried=None outcomes={'declined': 13, 'applied': 10, 'failed': 2} stopped_at=depth1 26/29 conformalise
- 5abbaf0b55 complete valid=True failing=['gmsh_default:no_volume', 'gmsh_prod:other', 'gmsh_alt:no_volume', 'netgen_occ:netgen_meshing_failed'] tried=69 outcomes={'applied': 32, 'declined': 35, 'failed': 2} stopped_at=None
- f3819472cf MR complete valid=True failing=['gmsh_default:overlapping_facets'] tried=139 outcomes={'declined': 69, 'applied': 70} stopped_at=None
- 01413e3ff0 MR killed valid=None failing=None tried=None outcomes={'declined': 278, 'applied': 449, 'failed': 8} stopped_at=depth2 29/118 run_stage
- 615e4cde05 MR complete valid=True failing=['gmsh_prod:other', 'gmsh_default:edge_through_face'] tried=232 outcomes={'declined': 214, 'applied': 18} stopped_at=None
- b13ba9c6a9 complete valid=True failing=['gmsh_prod:gmsh_crash', 'gmsh_default:overlapping_facets', 'gmsh_alt:no_volume'] tried=158 outcomes={'declined': 89, 'applied': 68, 'failed': 1} stopped_at=None
- 7e573047c1 killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 532be4434c MR complete valid=False failing=[] tried=19 outcomes={'declined': 16, 'applied': 3} stopped_at=None
- 4ef8e443ff MR killed valid=None failing=None tried=None outcomes={'declined': 25, 'applied': 19, 'failed': 6} stopped_at=depth1 51/126 conformalise
- 32ca3e9a6e MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 07edbf934b MR killed valid=None failing=None tried=None outcomes={'declined': 29, 'applied': 27, 'failed': 7} stopped_at=depth1 64/90 fuse
- 96c7bdaf49 MR killed valid=None failing=None tried=None outcomes={'declined': 29, 'applied': 27, 'failed': 7} stopped_at=depth1 64/90 fuse
- 557c3c0563 killed valid=None failing=None tried=None outcomes={'declined': 12, 'applied': 3} stopped_at=depth1 16/26 defeature
- bb0fe0d1c8 MR killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- 67a0c04ed2 MR complete valid=True failing=['gmsh_default:overlapping_facets'] tried=170 outcomes={'declined': 71, 'applied': 99} stopped_at=None
- affa12991b complete valid=False failing=['gmsh_prod:timeout', 'gmsh_default:timeout', 'gmsh_alt:timeout', 'netgen_occ:netgen_meshing_failed'] tried=20 outcomes={'declined': 17, 'failed': 1, 'error': 2} stopped_at=None
- de9ad40118 MR complete valid=True failing=['gmsh_prod:low_quality', 'gmsh_default:overlapping_facets', 'gmsh_alt:low_quality'] tried=227 outcomes={'declined': 114, 'applied': 113} stopped_at=None
- 263d5e2d5b MR complete valid=True failing=['gmsh_prod:other', 'gmsh_default:low_quality', 'gmsh_alt:low_quality'] tried=169 outcomes={'applied': 98, 'declined': 71} stopped_at=None
- 7b13ec12fe complete valid=False failing=['gmsh_default:edge_through_face', 'gmsh_prod:other', 'netgen_occ:netgen_meshing_failed'] tried=991 outcomes={'declined': 794, 'applied': 196, 'failed': 1} stopped_at=None
- 7f07861fcd complete valid=True failing=['gmsh_prod:low_quality', 'gmsh_default:low_quality'] tried=211 outcomes={'declined': 83, 'applied': 118, 'failed': 10} stopped_at=None
- 695d74ca7f killed valid=None failing=None tried=None outcomes={} stopped_at=staged
- bc860ab1a1 complete valid=True failing=['gmsh_prod:other', 'gmsh_default:overlapping_facets', 'gmsh_alt:no_volume'] tried=23 outcomes={'declined': 17, 'applied': 6} stopped_at=None
- 3ca940d41e MR complete valid=True failing=['gmsh_prod:other', 'gmsh_default:overlapping_facets'] tried=181 outcomes={'declined': 91, 'applied': 89, 'failed': 1} stopped_at=None
- 575448ea0b MR complete valid=False failing=['gmsh_prod:low_quality'] tried=818 outcomes={'applied': 187, 'declined': 631} stopped_at=None
- f62ed385ec complete valid=True failing=['gmsh_prod:other', 'gmsh_default:overlapping_facets'] tried=64 outcomes={'declined': 43, 'applied': 19, 'failed': 2} stopped_at=None
- 6946c8aa04 complete valid=True failing=['netgen_occ:netgen_meshing_failed'] tried=51 outcomes={'applied': 25, 'declined': 26} stopped_at=None

## staged alone clean / clean at input: 18
  {'staged_clean': 14, 'clean_at_input': 4}

## ops that reached clean (per winning candidate): {'defeature+defeature': 35, 'defeature': 27, 'defeature+run_stage': 22, 'remove_small+defeature': 11, 'split_angle+defeature': 9, 'split_angle': 8, 'defeature+split_angle': 6, 'split_by_proximity': 5, 'split_by_proximity+defeature': 5, 'run_stage+defeature': 4, 'split_by_proximity+run_stage': 3, 'defeature+split_by_proximity': 3, 'remove_small': 3, 'conformalise': 3, 'remove_small+run_stage': 2, 'split_angle+unify_same_domain': 2, 'fuse': 2, 'defeature+unify_same_domain': 2, 'split_by_proximity+split_angle': 2, 'remove_small+split_angle': 2, 'fill_loop': 1, 'remove_small+split_by_proximity': 1, 'unify_same_domain': 1, 'unify_same_domain+defeature': 1, 'split_by_proximity+unify_same_domain': 1, 'defeature+remove_small': 1, 'remove_small+remove_small': 1, 'split_angle+remove_small': 1, 'split_angle+run_stage': 1, 'split_angle+conformalise': 1, 'split_angle+fuse': 1, 'split_by_proximity+split_by_proximity': 1}
layer-1 losses across all candidates: 146

## WHAT HELD THE SEARCH BACK, per file reaching clean (cheapest explanation): {'SOLVED by a v4 arm': 24, 'WALL (det had it; search_det stopped on the wall)': 2, 'stage': 2, 'det had it': 2, 'whole-part defeature (OFF in the arms)': 1}
