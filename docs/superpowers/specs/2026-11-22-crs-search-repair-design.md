# CRS search repair (v4): design

Date: 2026-11-22 · Owner: Hexera founder · Repo: hexera-core/hexera-platform-v2 (main at a3225988, prod v0.2.7)
Inputs: `crs-v4/01-clean-step-definition.md`, `crs-v4/02-ai-diagnosis-summary.md`, `crs-v4/03-healing-research-summary.md`.

> **AMENDED 2026-11-24 by owner decision. Read this first: it overrides parts of
> what follows.** `clean` is now **layer 1 valid + layer 2 panel** only. The
> faithful layer is still computed and reported on every row, but it no longer
> gates `clean` and never refuses an edit, so **section 7 items 1-3 and every
> fidelity cap below are superseded**: the deviation bound and per-op deviation
> budget, the area/volume caps, the `fill_loop` 5%-of-diagonal limit, the
> overlap bound on `cut_by`, the no-fuse rule, the `remove_small` width bound
> and the op parameter bounds tied to the deviation budget are **removed, not
> loosened**. Face removal (defeaturing) is allowed. Raymond's
> `cad-simplification` work is ported in (`defeature`, `fuse`, any-depth
> `cut_by`, `trim_unbounded`), attributed, without merging his branch. Gaming is
> **reported**, not capped: a clean output whose volume changed more than 25% or
> whose bbox shrank more than 10% is flagged per arm for the owner to read, and
> still counts as clean. The spend caps, the worker kill and timeout safety and
> the sealed held-out set are unchanged. The architecture below is unchanged -
> staged healer, sites, best-first search, AI proposer, eval - only the goal is.
> The reasoning is in the plan's amendment of 2026-11-24, 02:47 UTC.

## 1. Intent

CRS must output more **clean STEP files**: files a customer can feed into whatever in-house mesher they use. The owner believes the AI part of CRS should help far more than it does, and that the architecture is what stops it.

The diagnosis confirmed this. CRS v3's AI is a selector over the same 13 kernel-healing ops the rules use. It works on kernel-warning clusters, is rewarded for making a defect fingerprint disappear, and is checked by a quick 2D probe that mostly says "pass". On the record benchmark every AI-arm fix came from the deterministic gap split, so the AI's net was +2 −1.

v4 rebuilds the AI part as a **verifier-driven search over a parametric healing-op library**:
- A deterministic staged healer runs first.
- An AI proposer then searches for repairs on what's left, judged by one clean-STEP checker.
- The same checker is the benchmark's scoreboard and the delivery gate.

## 2. Goals, non-goals, anti-goals

**Goals**
1. Maximise clean-STEP gain on a benchmark of files whose input is not clean. Clean means valid + meshes on the full mesher panel + faithful; see `01`.
2. Measure the AI's marginal gain over the deterministic staged healer alone, per file, every run.
3. Reach a first measured result within 3–5 days of starting, built by parallel subagent lanes.
4. Make the AI's action space, observations and reward match the failures, so that a better model or more search budget turns directly into more clean files.

**Non-goals (this sprint)**
- Production routing (triage only sees B-rep evidence), the delivery holes, and turning anything on in prod. These are recorded as follow-ups. v4 stays behind a flag, off in prod.
- Hints / sizing sidecars. Hints are a bonus metric only and never count as clean.
- Learned per-op predictors (phase 3, after data exists).
- A free-form code-writing agent (approach C). It is reserved as a later escape hatch inside the search.

**Anti-goals** (failure even if the numbers rise)
- A file counted clean that isn't clean on the full panel, or that drifts beyond the fidelity bound.
- Any clean control part made unclean.
- Gains that come only from benchmark-specific tuning: they vanish on the held-out split.
- Unbounded kernel calls in the worker process, i.e. a hang that a timeout can't stop.

## 3. Constraints

- Public data only for now: ABC, OCCT dataset, NIST MBE PMI models, Fusion 360 Gallery, CAx-IF where reachable, plus defect injection. Customer CAD comes later.
- Stack: Python, OCP (OCCT 7.8), gmsh 4.15.2. Netgen is to be added. The eval fleet is GCE c2d VMs in hexera-dev, as today.
- The existing safety and kernel plumbing is reused: `bounded_child`, `occ_distance`, `delivery_check`, `deviation`, `caps`, `identity`, `localize`, `mesh_probe`, `mesh_clash`, `gap_split`.
- v3 stays in the tree behind its flags until v4 beats it on the new benchmark; then it is removed in a separate PR.
- Every PR runs the native CAD tier locally before merge. PR CI skips that tier, which is how v0.2.6 broke.

## 4. Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| A | Patch v3 in place: align clone and delivery gates, failure-site regions, images on, stronger model, op parameters | Fast. Ceiling stays at the 13-op catalogue; expected +5–15 files | Rejected as the core. The gate fix is taken into phase 0 |
| **B** | **Verifier-driven search over a parametric op library**, with a deterministic staged healer first and an AI proposer on the remainder | Bigger build. Removes every cap the diagnosis found; matches the research consensus on pipeline order and the best working AI-on-CAD patterns | **Chosen** |
| C | Code-writing agent: the model writes OCP Python in a sandbox, in a loop with the checker | Most expressive. Hard to make safe, reproducible and cheap, and recipes are hard to reuse | Deferred as an escape-hatch action inside B |

The owner picked B on 2026-11-22.

## 5. Design

### 5.1 Clean-STEP checker (`verdict`)
- One function, `verdict(step_path, original_path) → Verdict`. The benchmark, the search and delivery all call it.
- **Layer 1, valid:** STEP round trip, then BRepCheck_Analyzer, ShapeAnalysis (free bounds, small entities, tolerance stats), BOPAlgo_ArgumentAnalyzer self-intersection, solid closure and orientation (positive volume), pairwise body overlap (Common volume > ε and not conformal), units unchanged.
- **Layer 2, meshable on the panel:** each run is in a killable child with its own wall time. No hints, no fTetWild. Pass = valid tet mesh with min SICN ≥ 0.01. The four panel members:
  1. gmsh with Hexera production FEA settings, extracted from `fea_adapter`/`fea_plan` into a pure config;
  2. gmsh defaults;
  3. gmsh with an alternate algorithm (2D Frontal-Delaunay, 3D Delaunay vs HXT);
  4. Netgen through its OCC interface.
- **Layer 3, faithful:** two-sided sampled surface distance to the original ≤ min(0.1 mm, 0.1% of the diagonal). Area/volume change within caps. Body count preserved. Face removals only as declared slivers.
- **Verdict carries:** per-layer pass/fail; per-mesher error kind, message, located entities and coordinates, min SICN and time; deviation used; a lexicographic score tuple (valid, panel passes, min SICN, −deviation, −topology churn).
- **Cheap tier:** the search also uses a cheap tier that runs layer 1 plus the gmsh defaults run only, to rank candidates. The full panel confirms survivors. The cheap tier's agreement with the full panel is measured and reported.

### 5.2 Benchmark v2
- **Candidate pool:**
  - ABC harvested failures (9,956) and ABC passing samples;
  - OCCT dataset;
  - NIST MBE PMI models (CTC/FTC/STC, one part exported through several translators);
  - Fusion 360 Gallery segmentation STEP files, especially those flagged as unmeshable;
  - injected-defect variants of clean parts (injector list in `03`, calibrated to match the ABC failure histogram).
- **Each candidate input is run through `verdict`.** Failing set = inputs not clean, stratified by first failing layer × gmsh error family × source. Clean control = inputs clean on all layers.
- **Splits:** dev (tuning), held-out (reported, never tuned on), clean control. Proposed sizes: 300 failing dev, 200 failing held-out, 150 clean control. The builder may adjust these to fit compute.
- **Harness:** reuses the v3_eval machinery (stage, launch_fleet, run_arm, collect: commit-pinned, crash-isolated). It adds a per-file wall-clock kill (today `waitpid` has no timeout) and the new verdict scoring.
- **Arms:**
  - `input_only`, the baseline;
  - `staged`, the deterministic pipeline;
  - `search_det`, search with deterministic generators only;
  - `search_ai`, search with deterministic generators plus the AI proposer;
  - v3's `meshability_pass_ai`, run once for comparison.

### 5.3 Workspace and stable identity
- `Workspace` is an immutable-by-convention state: shape plus history. Candidates fork from it cheaply, and every op returns a new state.
- Stable entity IDs come from fingerprints (reuse `identity`) and are tracked across ops through `BRepTools_History` (`Modified`, `Generated`, `IsRemoved`). The proposer can therefore keep referring to "face 41" after a split.
- Each op application yields a **Diff**: entities modified, generated and removed; deviation spent; cheap-verdict delta; time.

### 5.4 Parametric op library
- Typed interface: `Op(name, params_schema, applies_to(site) → bool, apply(ws, targets, params) → (ws', Diff))`.
- Every kernel call runs in a killable child (`bounded_child` allow-list extended); no parent-side kernel work in op bodies.
- Parameters are bounded relative to h (default element size) and to the remaining deviation budget.

| # | Op | OCCT basis | Changes |
|---|---|---|---|
| 1 | `split_by_proximity(face_pairs, target_ratio)`, generalising gap_split | iso-curve/plane sections, BRepAlgoAPI_Splitter | topology |
| 2 | `split_angle(faces, max_deg)` / `split_closed` | ShapeUpgrade_ShapeDivideAngle, ShapeDivideClosed(Edges) | topology |
| 3 | `split_continuity(faces, C1/C2)` | ShapeUpgrade_ShapeDivideContinuity | topology |
| 4 | `conformalise(bodies, fuzzy, mode∈{fragment, fuse, cut_by:id})` | BOPAlgo_Builder (fuzzy, glue) + BOPAlgo_CellsBuilder | geometry + topology |
| 5 | `remove_duplicate_faces(pairs)` | General Fuse + CellsBuilder, or delete and re-sew | topology |
| 6 | `rebuild_edge_as_intersection(edges)` | GeomAPI_IntSS, ShapeFix_Edge, BRepLib::SameParameter | geometry (≤ tol) |
| 7 | `tighten_tolerances(scope)` | ShapeFix_ShapeTolerance, BRepLib::BuildCurves3d, SameParameter | geometry (≤ tol) |
| 8 | `rebuild_face(face, continuity)` for self-intersecting faces | ShapeFix_Wire::FixSelfIntersection → BRepOffsetAPI_MakeFilling | geometry, deviation-gated |
| 9 | `remove_small(entities, max_width)` | ShapeFix_Wireframe::FixSmallEdges, ShapeFix_FixSmallFace, BRepAlgoAPI_Defeaturing | geometry, within budget |
| 10 | `sew(tol_ladder)` + `make_volume` | BRepBuilderAPI_Sewing, BOPAlgo_MakerVolume, ShapeFix_Shell/Solid | topology |
| 11 | `fill_loop(loop, G0/G1)` | BRepBuilderAPI_MakeFace (planar), BRepOffsetAPI_MakeFilling | geometry |
| 12 | `reapproximate(faces, tol, degree)` / `to_canonical` | ShapeCustom_BSplineRestriction, ShapeAnalysis_CanonicalRecognition | geometry, deviation-gated |
| 13 | `fix_poles(faces)` | ShapeFix_Face, ShapeFix_Wire::FixDegenerated | geometry (tiny) |
| 14 | `unify_same_domain(scope, keep)` | ShapeUpgrade_UnifySameDomain (with keep list) | topology |
| 15 | `refit_cluster(faces, tol)`, a baked composite | GeomAPI_PointsToBSplineSurface / MakeFilling with boundary constraints | geometry, deviation-gated |
| — | `orient_and_close` (always run) | ShapeFix_Shell, ShapeFix_Solid, BRepLib::OrientClosedSolid | topology |

v3's 13 ops are mapped into this interface where they overlap; for example, `fix_small_faces` maps to `remove_small`.

### 5.5 Staged deterministic healer
- The stage order follows the cross-vendor consensus (citations [1], [5], [6], [7]):
  0. baseline
  1. normalise
  2. edges/wires
  3. faces
  4. shells (sewing ladder, fill)
  5. solids (MakerVolume fallback)
  6. between bodies (conformalise)
  7. simplify (unify, artefact defeature)
  8. mesh conditioning (angle, closed, continuity and proximity splits)
  9. finalise (round trip, verdict)
- Each stage is transactional: apply on a fork, run the cheap verdict, keep it only if the score didn't regress, otherwise roll back.
- Every stage is also exposed as a macro action, `run_stage(name, params)`, so the search and the AI can call stages with non-default parameters.

### 5.6 Failure sites (the unit of work)
- A **site** is a cluster of B-rep entities implicated by panel failures. Located from:
  - each mesher's error output: gmsh PLC point, surface tags, clash pairs via `mesh_probe`/`mesh_clash`; Netgen failing faces;
  - layer-1 checker findings;
  - proximity pairs below k·h.
- Kernel warnings are attached to a site only when they lie inside it. No rule forces attempts on warnings outside sites.
- Each site record holds: kind, entities, coordinates, which meshers fail there, distance/h, local k-hop face-graph summary (surface types, areas, min widths, angular span, continuity, edge tolerance vs actual gap, dihedral angles), and a rendered view.

### 5.7 Search driver
- **Best-first search per file over Workspace states,** ranked by the verdict score tuple. Start state: the output of the staged healer.
- **Candidate generators:**
  - deterministic generators per site kind (e.g. a proximity site proposes `split_by_proximity` at ratios {0.5, 0.25, 0.12});
  - the AI proposer.
- **Candidate evaluation:**
  - Candidates are evaluated in parallel on forks (process pool, N workers ≈ cores) with the cheap verdict.
  - The best K are confirmed with the full panel.
- **Budgets per file:** wall clock, op applications, model calls and model $. Configurable; eval defaults set by the builder within the cost ceiling in §7.
- **Stop when:** clean on the full panel, the budget is spent, or no candidate improves the score for M rounds.
- **Output:** the best clean state, otherwise the best-scoring state flagged not clean, plus a full trace.

### 5.8 AI proposer
- **A loop, not one-shot.** One conversation per file, persisting across sites and rounds.
- **Each turn it receives:**
  - the current best state's verdict summary;
  - open sites (§5.6), with images on;
  - the op catalogue with parameter schemas and bounds;
  - Diffs and verdict deltas of its previous candidates;
  - remaining budgets.
- **It returns** 1–n typed candidates, `{op | run_stage, targets (stable IDs), params, rationale}`. It may also declare a site unfixable within the fidelity budget, with a reason.
- **Judgement it owns:** overlap intent (duplicate vs fit vs subtract), hole vs missing face, choice between alternative fixes under the deviation budget, ordering and recovery when stages interact, mapping a mesher error to its causal entity, and when to stop. These are the areas the research identifies as genuinely needing judgement.
- **Model:** the strongest available model on the repair route, replacing `gpt-5.6-luna`, with tool use and images. Vendor and per-file cost ceiling are owner decisions (§7). **Settled 2026-11-23: the strongest OpenAI model on the existing `openai-api-key` route, not Claude; the Anthropic adapter built for this is parked unmerged.**
- **Malformed calls** are rejected by schema with a structured error, not counted as a refusal streak. v3's runaway and filler-key failures came from loose transport.

### 5.9 Delivery gate
- A state that passes the full verdict on the scratch copy ships.
- No op-answers-defect gate sits between a measured clean result and delivery. This fixes v3's clone/delivery asymmetry, where 11 files had a package refused.
- The LLM package reviewer stays available as an optional extra gate, off in the eval by default, so its effect can be measured separately.

### 5.10 Recipe memory (phase 3)
- Stores verified sequences keyed by failure signature: site kind × local geometry features × which meshers fail.
- Retrieval seeds the proposer and the deterministic generators.
- Written only from clean outcomes on the dev split, never from held-out files.

### 5.11 Integration
- New flag `CAD_REPAIR_ENGINE ∈ {v3, search}`, default v3, off in prod.
- `repair_step_file` dispatches to the search engine when it is set to `search`, so the pipeline contract is unchanged.
- Old v3 modules are untouched until v4 wins on held-out; then a removal PR follows.

## 6. Delivery plan (lanes and days)

| Day | Lanes (parallel subagents; each has a test-first scope and a merge gate) |
|---|---|
| 1 | **L0 checker** (5.1), **L1 benchmark** (5.2: pool fetch, injectors, verdict re-probe launched on the fleet), **L2 workspace + identity + Diff** (5.3), **L-gate** (port the v3 clone/delivery fix for an early v3 number) |
| 2 | **L3 ops 1–8**, **L4 ops 9–15 + orient**, **L5 staged healer** (5.5) on L2. First `staged` arm wave on dev once L1 has the failing set |
| 3 | **L6 failure sites + observations** (5.6), **L7 search driver** (5.7), **L8 AI proposer** (5.8). First `search_det` and `search_ai` waves |
| 4–5 | Tuning waves on dev; one held-out report; write-up |
| later | Recipe memory, learned per-op predictors, production routing |

**Merge gate per lane:** unit tests, native CAD tier run locally, no new failures on clean controls, and wave results attached to the PR.

## 7. Decisions needed from the owner (recommended defaults; the plan proceeds on these unless changed)
1. **Multi-body semantics.**
   - Default: a multi-body part is clean when the panel meshes it **as one conformal mesh** (gmsh fragments the bodies; Netgen gets the glued compound). Touching bodies must share faces after the STEP round trip, so the output is written as one compound with shared faces.
   - Body count is preserved.
   - `fuse` is not allowed without an explicit customer intent signal.
   - `cut_by` is allowed only when the overlap volume is within the fidelity budget.
   - *Why it matters:* this decides how the checker drives each mesher, whether ops 4–5 can produce clean files, and whether the assembly injector is worth building.
2. **Fidelity for intentional adds and removes.**
   - Default: the distance is **one-sided, original → repaired** for every face the repair declares added (fills, refits). Declared removals (duplicate faces, slivers narrower than min(0.1 mm, 0.1% of the diagonal)) are exempt but counted.
   - Everything else uses the two-sided bound.
   - A hole fill is limited to loops whose span is ≤ 5% of the diagonal.
   - For injected files, "the original" is the **damaged input**, so we never use knowledge the product wouldn't have. The clean source is used only to label the defect.
3. **Which layer-1 findings fail "clean".**
   - Default hard fails: BRepCheck invalid, open or non-manifold shell, non-positive volume, self-intersection, non-conformal body overlap, units changed, a STEP round trip that doesn't re-read valid.
   - Soft findings are reported but don't fail clean: small edges/faces, large tolerances. Real damage from those shows up in layer 2.
   - The failing set therefore never fills with cosmetic-only files.
4. **Model and cost.**
   - Default: Claude (strongest available) on the repair route, with images. **Decided 2026-11-23: OpenAI instead — the strongest model on the existing `openai-api-key` route, with tool use and image input; no Anthropic key or adapter is introduced, and the ledger must price the chosen model correctly.**
   - Per-file ceiling: $1.00 for `search_ai` in the eval, with model spend and wall time reported per file.
   - Compute: up to ~600 c2d VM-hours for the re-probe plus waves this sprint.
   - The re-probe runs the cheap tier on the full pool first; the full panel then runs only on cheap-tier failures and a 10% sample of passes (to measure agreement).
5. **Assembly structure, names, colours, PMI.**
   - Default: **preserve body and face names, colours and assembly structure** (customers set boundary conditions by name). I/O moves to STEPCAFControl/XCAF, labels are carried through BRepTools_History, and layer 1 checks they survive.
   - PMI is carried where present but not checked this sprint.

**Settled by the designer (builder may refine):**
- **Kernel calls run in a warm worker pool.** Workers are spawned fresh (not forked), each call has a time limit, and a timed-out worker is SIGKILLed and replaced. This satisfies the no-unkillable-hang anti-goal at about 10× the throughput of one interpreter per call. Shapes cross as BREP, with stable IDs in a JSON sidecar.
- **Win condition and headline numbers:**
  - The headline is **clean-STEP gain on held-out**.
  - The AI marginal is `search_ai` − `search_det`, at equal wall-clock and op budgets. The model budget is extra and reported.
  - The deterministic marginal is `staged` − `input_only`.
  - v3 is removed when the best v4 arm beats `v3 meshability_pass_ai` on held-out clean gain with zero clean-control regressions.
  - A regression is any clean input made unclean, or any output that passes **fewer panel members** than its input.
  - When nothing comes out clean, the delivered state is the best-scoring state only if it passes at least as many panel members as the input; otherwise the input is passed through.

8. **Benchmark distribution. Settled 2026-11-22: internal only.** Corpus files, injected derivatives
   and per-file results stay in the private eval bucket; any external write-up carries aggregate
   numbers plus names and hashes only. This removes the ABC/OCCT redistribution question from the
   critical path - it remains an open owner item should external distribution ever be wanted, and
   the OCCT licence text is still marked UNVERIFIED. The "an injected variant is governed by its
   clean source's terms" invariant stands regardless of distribution, because it is what makes the
   provenance of a derived file answerable at all.

## 8. Left to the builder
Exact panel wall times; cheap-tier composition; split sizes within compute; search hyperparameters (K, N workers, M rounds); site clustering thresholds; injector calibration; the Netgen integration route; how the production FEA config is extracted.

## 9. Risks
- **Netgen-on-OCC integration** may be slow. Fallback: ship the panel with three gmsh members first and add Netgen when it is ready, reporting both.
- **Cheap tier disagreeing with the full panel** would misrank candidates. Agreement is measured every wave.
- **Benchmark re-probe compute or time** may be too high. Mitigation: start the re-probe on day 1 on the fleet; begin dev waves on the first 300 confirmed failures.
- **Parallel lanes regressing each other**, as in v3. Mitigation: the merge gate above, one integration owner, and the native tier run before every merge.
- **The AI still adds little** even with the right actions. That would itself be a clear, measured answer, and the staged healer plus search keeps its gains either way.

## References
[1] OCCT Shape Healing user guide (ShapeAnalysis/ShapeFix/ShapeUpgrade/ShapeProcess) — https://occt3d.com/dev/doc/overview/html/occt_user_guides__shape_healing.html
[2] gmsh reference manual (OCC healing options, healShapes, fragment, defeature, setCompound) — https://gmsh.info/doc/texinfo/gmsh.html
[3] ANSYS Meshing: virtual topology — https://ansyshelp.ansys.com/public/Views/Secured/corp/v261/en/wb_msh/msh_vtintro.html ; pinch — https://ansyshelp.ansys.com/public/views/secured/corp/v252/en/wb_msh/msh_pinch_usage.html ; auto defeaturing — https://ansyshelp.ansys.com/public/Views/Secured/corp/v251/en/wb_msh/msh_auto_defeat.html
[4] Sandia Cubit cleanup: small details — https://www.sandia.gov/files/cubit/15.9/help_manual/WebHelp/item/clean_up/small_details.htm ; tweak remove topology — https://sandia.gov/files/cubit/15.9/help_manual/WebHelp/geometry/cleanup_and_defeaturing/tweaking_geometry/tweak_remove_topology.htm
[5] Spatial (ACIS) healing phases — https://blog.spatial.com/healing-as-an-essential-function
[6] CADfix overview — https://iti-global.com/media/fzgjudfz/cadfix_overview.pdf ; https://info.iti-global.com/cadfix
[7] SpaceClaim repair tools — https://innovationspace.ansys.com/knowledge/?p=160693
[8] Jackson, B-rep modelling with local tolerances (1995) — https://ftp1.cs.wisc.edu/pub/users/prem/jackson-SM-95.pdf
[9] Gu et al., CAD interoperability errors (JCISE 2001) — https://researchr.org/publication/GuCCBJ01
[10] Barequet & Kumar, Repairing CAD models (1997) — https://dblp.uni-trier.de/rec/conf/visualization/BarequetK97.html
[11] Bischoff & Kobbelt, Structure preserving CAD model repair (2005) — https://people.eecs.berkeley.edu/~sequin/CS284/PAPERS/Bischoff_nurbs_repair1.pdf
[12] Attene, Campen, Kobbelt, Polygon mesh repairing (ACM CSUR 2013) — https://doi.org/10.1145/2431211.2431214
[13] Thakur, Banerjee, Gupta, CAD simplification survey (CAD 2009) — https://umiacs.umd.edu/node/18807
[14] Sheffer et al., Virtual topology operators for meshing (IJCGA 2000) — https://cris.huji.ac.il/en/publications/virtual-topology-operators-for-meshing/
[15] Owen, Shea, Martin, CAD defeaturing using machine learning (Sandia 2019) — https://www.osti.gov/servlets/purl/1640901
[16] Owen et al., AI methods for geometry preparation and mesh generation survey — https://arxiv.org/abs/2512.23719
[17] CAD-Assistant (ICCV 2025) — https://arxiv.org/abs/2412.13810
[18] CADCodeVerify (ICLR 2025) — https://arxiv.org/abs/2410.05340
[19] Huang et al., B-rep Boolean result repair (2023) — https://arxiv.org/abs/2310.10351
[20] BRepNet — https://arxiv.org/abs/2104.00706 ; UV-Net — https://arxiv.org/abs/2006.10211 ; HoLa — https://arxiv.org/abs/2504.14257
[21] NIST MBE PMI test models — https://www.nist.gov/el/systems-integration-division-73400/mbe-pmi-validation-and-conformance-testing/download-free-cad
[22] Fusion 360 Gallery dataset — https://github.com/AutodeskAILab/Fusion360GalleryDataset
[23] MBx-IF (formerly CAx-IF) — https://www.mbx-if.org/what_we_do.php
