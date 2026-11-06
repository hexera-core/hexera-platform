# Defect-driven CAD repair retrieval: literature review and iteration direction

**Date:** 2026-10-29

This review covers the design choices in
`docs/superpowers/specs/2026-10-29-defect-cad-rag-design.md`. It is deliberately scoped to the
lab question: given a located CAD defect, can prior repair outcomes improve the choice of bounded
kernel operation without changing the repair caps or rollback rules?

## Executive direction

Do not start by training a universal CAD embedding or provisioning an approximate-nearest-neighbor
index. The current evidence has 21 direct kept-operation records across the three represented defect
codes. That is enough to validate a data model and an exact-search baseline, but not enough to
distinguish encoder quality from memorization.

The next iteration should instead answer four questions in order:

1. Can the same defect be isolated reproducibly after rigid transforms and STEP round trips?
2. Can a cheap, invariant descriptor retrieve operation-compatible patches across held-out part
   families?
3. When every applicable operation is tried independently on a cloned lab input, does retrieval
   rank kept operations above rolled-back and declined operations?
4. Does that ranking improve the existing planner in shadow before target rebinding or live
   directives are attempted?

The key change is item 3. Historical kept fixes are observational data: the current planner chose
which operation to try. A lab-only action sweep over cloned inputs supplies the missing comparison
cases and turns the corpus into an operation-compatibility dataset. The kernel and existing caps
still produce every label.

## 1. Defect isolation

### What the literature says

Native B-rep methods consistently combine topology and geometry. [BRepNet] uses a small oriented
coedge neighborhood and reports that this local kernel balances recognizable topology against
sensitivity to topological variation. [UV-Net] samples parametric curve and surface geometry while
propagating over a face-adjacency graph. This is stronger evidence for topology-bounded patches than
for Euclidean crops alone: the repair operations act on faces, wires, edges and their incidence.

Mesh and point-cloud work provides the complementary metric view. [Geodesic CNNs] define local
surface patches in geodesic polar coordinates, while [PPF-FoldNet] and [3DSmoothNet] show that
local descriptors need explicit rotation treatment. These methods are useful fallbacks when an
invalid B-rep cannot provide trustworthy adjacency, but discretization should not replace valid
analytic surface and curve data by default.

A newer warning matters directly here: [Learn the Solid, Not the File] reports that B-rep encoders
can fail when the same solid is repartitioned into a different but equivalent B-rep. A defect patch
therefore needs a stability test under import/export and entity splitting, not just a deterministic
walk over one file's entity IDs.

### Direction

Use a hybrid, multi-scale patch:

- level 0: defect-owned entities and the operation's target closure;
- level 1: incident coedges, wires and faces;
- level 2: one additional face-adjacency ring, clipped by a metric radius expressed in local feature
  sizes;
- fallback: sampled oriented points plus analyzer witnesses when topology cannot be traversed.

Persist all three levels rather than choosing one radius now. The first experiment should compare
their stability and retrieval value. A patch is acceptable only if its symbolic signature and
descriptor remain within a declared tolerance after rigid transforms and a STEP write/read cycle.
Do not require construction of a standalone valid sub-shape; invalid source topology may make that
impossible. A patch can be a view over source entities plus sampled geometry.

## 2. Representation and encoder choice

### What the literature says

The strongest native-CAD neural baselines are graph/parametric hybrids. [UV-Net] combines UV grids
with graph convolution; [BRepNet] applies message passing over faces, edges and coedges. Both were
evaluated with datasets many orders of magnitude larger than this repair memory: BRepNet's Fusion
360 segmentation data contains more than 35,000 models, and [ABC] contains one million CAD models.
This makes training either architecture from the current repair labels an invalid first comparison.

Point descriptors are cheaper and provide useful invariance baselines. PPF-FoldNet avoids a fragile
local reference frame by using point-pair features. 3DSmoothNet obtains compact 32-dimensional local
descriptors, but relies on a local reference frame. Multi-view methods can be effective for global
shape recognition, yet they discard exact B-rep incidence and add rendering cost. That is a poor
first trade for tiny, topology-defined defects.

Recent sample-efficient CAD work is relevant but does not remove the label problem. [FeatureFox]
shows that enriched graph attributes plus calibrated shallow classifiers can perform strongly with
hundreds rather than thousands of labeled parts. Hundreds is still substantially beyond the 21
direct kept records available here, and its result supports a structured descriptor baseline before
a deep encoder.

### Direction

Start with a versioned 64-128 dimensional descriptor, split into:

- topology: entity counts, valence, boundary components, manifold/open status and adjacency motifs;
- analytic geometry: surface/curve type histograms, continuity, convexity and normal relationships;
- dimensionless measures: length/area/curvature and gap distributions normalized by patch scale;
- absolute facets: millimetre measures, model diagonal, tolerances and unit provenance;
- defect evidence: analyzer measurements already emitted by localization and spotlighting.

Standardize features per defect code and use exact weighted distance. Tune weights only on training
families. Bake off learned encoders later in this order: pretrained/self-supervised B-rep graph,
rotation-invariant point patch, then multi-view. A learned method advances only if it beats the
descriptor on held-out families and CPU wall time.

## 3. Supervision and outcome labels

### What the literature says

The proposed system is a modern instance of case-based process planning. Earlier manufacturing
systems used feature-based indexes to retrieve a prior plan, adapted it, simulated it and repaired
the plan when validation failed. That maps unusually well onto CRS: retrieve a case, bind its target
roles, execute with the kernel, then let caps and re-inspection validate it. The old work also argues
for structured feature indexes rather than undifferentiated whole-part similarity.

Metric learning assumes that positives and negatives express the downstream relation of interest.
Here that relation is not "looks geometrically similar"; it is "the same operation is applicable and
survives the caps." A kept operation is a positive. The same operation rolled back, declined, or
failed on an otherwise comparable defect is a hard negative. Different operations can both be
positive for one patch, so this is multi-label compatibility rather than a single-class problem.

Historical rows are confounded by planner policy. An untried operation is unknown, not negative.
The current 430-defect OCCT headline particularly illustrates the issue: 16 kept operation records
produced many credited defect removals, so per-defect duplication would create false sample size and
leak one repair episode across splits.

### Direction

Create lab labels by independently trying every *applicable* catalogue operation against the same
pre-repair file state. Each attempt runs in a crash-isolated subprocess and is judged by the current
caps, write/read, and re-inspection path. This is bounded experimental exploration, not a customer
runtime behavior.

Store one operation-attempt row and a separate repair-effect row. The latter contains the set of
defect fingerprints removed, retained and introduced. Group all rows from the same source file and
repair episode during splitting. Do not copy one kept file-level operation into hundreds of
nominally independent positive patches.

Only train a projection or contrastive head after each target operation has at least 200 independent
attempts across at least 30 part families, with both positive and negative outcomes. These are
engineering gates, not claims from the literature; they keep the first learned comparison from
being dominated by a few source parts.

## 4. Canonicalization

### What the literature says

Rotation sensitivity is a demonstrated failure mode for 3D descriptors and B-rep encoders.
PPF-FoldNet avoids local-frame dependence; 3DSmoothNet uses a computed frame; [FoV-Net] combines a
local frame with rotation-invariant surrounding context. The approaches show that a local frame is
useful but not universally stable.

CAD adds two ambiguities. Symmetric patches can flip principal axes, and equivalent files can split
or merge faces. Learn the Solid, Not the File explicitly targets both rigid-motion and
repartitioning invariance. Scale normalization is also incomplete for repair: two dimensionlessly
similar gaps at micrometre and metre scales can exercise different kernel tolerances.

### Direction

Make invariant scalars and pairwise relations the baseline. Compute an optional deterministic local
frame for sampled or rendered encoders, but record axis-degeneracy and sign-ambiguity flags and test
all equivalent frame choices during evaluation. Keep both normalized and absolute measurements.
Pose, tessellation density and STEP round-trip perturbations become mandatory invariance tests.

## 5. How retrieval should influence repair

### What the literature says

Case-based planning does not blindly replay a retrieved plan. It retrieves, adapts and simulates
before acceptance. The repair literature gives the reason: local B-rep edits can collapse topology
or violate design intent away from the apparent defect. Work based on design history specifically
argues that B-rep-only correction can unintentionally distort contextual meaning. CRS caps and
global re-inspection already provide the required simulator/judge boundary.

The remaining risk is target adaptation. A nearest case can recommend the correct operation but
have no unambiguous target-role binding in the query patch. Operation ranking and target rebinding
should therefore be evaluated separately.

### Direction

Use retrieval in three increasingly strong modes:

1. **Evidence only:** report nearest kept and failed cases and whether the existing planner agrees.
2. **Candidate reranker:** reorder only operations already considered applicable by deterministic
   rules; do not import targets or parameters.
3. **Directive proposal:** bind relative target roles and parameters, dropping any ambiguous case.

The first live experiment should be the candidate reranker, not direct directives. It can test the
value of memory without conflating operation selection with geometric correspondence. Retrieve
contraindications as well as successful cases, and abstain when neighbors disagree or lie beyond a
calibrated distance.

## 6. Store and retrieval system

### What the literature and implementation guidance say

At the current and near-term corpus sizes, exact search is simpler and gives a recall ceiling for
every approximate method. pgvector's own guidance recommends monitoring approximate recall against
exact search. It also notes that filtered HNSW scans may need iterative scanning; this matters
because every query here filters by defect code, encoder version, scope and often scale band.

HNSW is not free storage. One million 768-dimensional `float4` vectors consume about 3.07 GB before
row and index overhead. pgvector supports smaller representations and reranking, but the actual HNSW
index size and build memory must be measured on the deployed extension version.

### Direction

Use an in-process exact matrix for Phase 1 and retain vectors in versioned artifact files beside the
corpus records. Probe Postgres + pgvector for schema and query compatibility, but do not make it a
Phase-1 dependency. Move to exact pgvector queries when durable shared access is needed. Add HNSW
only after a measured corpus size or latency threshold is crossed; compare every ANN configuration
with exact top-k recall.

Prefer relational columns for high-selectivity facets and JSON only for evolving evidence. If HNSW
is introduced, test partitioning or partial indexes by defect/encoder against iterative scans. The
shared `db-g1-small` instance is suitable for an extension probe, not a million-vector benchmark.

## 7. Evaluation and acceptance

### What the literature says

ABC emphasizes ground-truth parametric geometry, resampling at different resolutions, and dataset
variation. CAD retrieval and feature-recognition work commonly benefits from synthetic scale, but
the repair question is narrower: the retrieved operation must remain valid after kernel execution.
Embedding retrieval metrics are therefore intermediate measures, not the promotion criterion.

Equivalent patches from one source part are highly correlated. Random patch splits would reward
memorizing family geometry, and operation sweeps create multiple rows from the same starting state.
The split unit must be the source/reference family, with all derived defects and attempts on one
side.

### Direction

Report four layers separately:

1. **Isolation stability:** patch extraction success and descriptor drift under pose, tessellation
   and STEP round trips.
2. **Retrieval:** operation-compatible recall@k, MRR, calibration and abstention coverage, grouped by
   defect code and held-out family.
3. **Playability:** applicable-operation rate and unambiguous target-role binding rate.
4. **Repair value:** defect yield, damage, refusal quality, runtime and cost against rules-only.

For Phase 1, require at least 100 files and report bootstrap confidence intervals over source
families. Do not set a universal recall threshold before the action sweep reveals class balance.
Advance the reranker only when its lower confidence bound beats rules-only operation top-1 accuracy
on at least one adequately supported defect code and does not worsen damage or refusal metrics.

## Recommended iteration sequence

### Iteration A: make the unit of evidence honest

Add per-defect patch records, operation-attempt records, repair-effect sets, crash isolation and
family-grouped manifests. Re-express existing OCCT and defectgen evidence without multiplying
file-level outcomes into patch labels.

### Iteration B: prove patch stability

Implement the three-level hybrid patch and descriptor. Run rigid-transform, tessellation and STEP
round-trip metamorphic tests. Stop and revise isolation if equivalent inputs do not retrieve one
another reliably.

### Iteration C: generate operation compatibility

On cloned lab inputs, sweep applicable operations independently under existing caps. Prioritize
`open_shell` and `small_edge`, where the catalogue has direct successful operations. Use generated
`small_face` design-intent cases as negative and abstention controls until a repair operation is
shown to preserve their intended geometry.

### Iteration D: exact retrieval and shadow reranking

Run exact kNN with the descriptor, retrieve both success and failure cases, and calibrate an abstain
threshold on training families. Shadow a candidate reranker against the deterministic planner.

### Iteration E: encoder bake-off

Only after the label gate, compare a pretrained B-rep graph encoder and a rotation-invariant point
patch encoder with the descriptor. Multi-view remains a fallback for patches whose analytic or
topological representation cannot be extracted. Keep exact retrieval as the oracle while evaluating
any ANN store.

## Sources

- [BRepNet]: J. G. Lambourne et al., "BRepNet: A Topological Message Passing System for Solid
  Models," CVPR 2021.
- [UV-Net]: P. K. Jayaraman et al., "UV-Net: Learning From Boundary Representations," CVPR 2021.
- [ABC]: S. Koch et al., "ABC: A Big CAD Model Dataset for Geometric Deep Learning," CVPR 2019.
- [Geodesic CNNs]: J. Masci et al., "Geodesic Convolutional Neural Networks on Riemannian
  Manifolds," ICCV Workshops 2015.
- [PPF-FoldNet]: H. Deng, T. Birdal and S. Ilic, "PPF-FoldNet: Unsupervised Learning of Rotation
  Invariant 3D Local Descriptors," ECCV 2018.
- [3DSmoothNet]: Z. Gojcic et al., "The Perfect Match: 3D Point Cloud Matching With Smoothed
  Densities," CVPR 2019.
- [FeatureFox]: B. Fuchs et al., "FeatureFox: Sample-Efficient Panoptic Graph Segmentation for
  Machining Feature Recognition in B-Rep 3D-CAD Models," 2026 preprint.
- [FoV-Net]: M. Ballegeer and D. F. Benoit, "FoV-Net: Rotation-Invariant CAD B-rep Learning via
  Field-of-View Ray Casting," 2026 preprint.
- [Learn the Solid, Not the File]: H. Jiang et al., "Learn the Solid, Not the File: Canonical Inputs
  for Neural Networks on CAD Boundary Representations," 2026 preprint.
- H.-C. Chang et al., "Indexing and retrieval in machining process planning using case-based
  reasoning," *Artificial Intelligence in Engineering* 14(1), 2000.
  https://doi.org/10.1016/S0954-1810(99)00027-8
- J. Yang and S. Han, "Repairing CAD model errors based on the design history," *Computer-Aided
  Design* 38(6), 2006. https://doi.org/10.1016/j.cad.2006.02.007
- pgvector documentation and exact/approximate recall guidance.
  https://github.com/pgvector/pgvector
- PostgreSQL, "pgvector 0.8.0 Released," including iterative scans for filtered ANN queries.
  https://www.postgresql.org/about/news/pgvector-080-released-2952/

[BRepNet]: https://openaccess.thecvf.com/content/CVPR2021/html/Lambourne_BRepNet_A_Topological_Message_Passing_System_for_Solid_Models_CVPR_2021_paper.html
[UV-Net]: https://openaccess.thecvf.com/content/CVPR2021/html/Jayaraman_UV-Net_Learning_From_Boundary_Representations_CVPR_2021_paper.html
[ABC]: https://openaccess.thecvf.com/content_CVPR_2019/html/Koch_ABC_A_Big_CAD_Model_Dataset_for_Geometric_Deep_Learning_CVPR_2019_paper.html
[Geodesic CNNs]: https://openaccess.thecvf.com/content_iccv_2015_workshops/w22/html/Masci_Geodesic_Convolutional_Neural_ICCV_2015_paper.html
[PPF-FoldNet]: https://openaccess.thecvf.com/content_ECCV_2018/html/Haowen_Deng_PPF-FoldNet_Unsupervised_Learning_ECCV_2018_paper.html
[3DSmoothNet]: https://openaccess.thecvf.com/content_CVPR_2019/html/Gojcic_The_Perfect_Match_3D_Point_Cloud_Matching_With_Smoothed_Densities_CVPR_2019_paper.html
[FeatureFox]: https://arxiv.org/abs/2604.26770
[FoV-Net]: https://arxiv.org/abs/2602.24084
[Learn the Solid, Not the File]: https://arxiv.org/abs/2609.11573
