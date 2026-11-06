# Literature Review For A Repair, Mesh And Solve Feedback Loop

Status: draft seed, 2026-11-05. This document reviews the external literature behind a proposed
harness architecture: repair geometry, mesh the repair, run isolated CFD/FEA on the sessions that
came out defective, then use that result to drive another repair pass. It is research input only.
It does not specify the loop, and it deliberately does not answer the open decisions listed at the
end - those are the user's to make.

Every number below was checked against a primary source where one could be retrieved. Claims that
could not be verified that way are marked inline as UNVERIFIED and collected in
[Verification Notes](#verification-notes). The review was written because the architecture is new to
this repository; the repair surface it would sit on is already documented as one surface with a
deterministic portion and a model-directed portion, not two engines, and nothing here proposes
changing that.

## 1. LLM Loops Need An External Verifier

The premise behind the proposed architecture is that LLMs do well in loops and this harness has no
loop yet. The literature supports that premise with one hard condition attached: the loop needs a
grader the model does not control.

`SELF-REFINE` is the baseline pattern - the same LLM generates an output, gives feedback on it, and
refines, iteratively. Verified from the paper: it requires no supervised training data, no
additional training and no reinforcement learning; it was evaluated on 7 diverse tasks with GPT-3.5
and GPT-4; outputs improved by about 20% absolute on average across tasks, with a stated range of
5-40% absolute, and code generation improved by up to 13% absolute.

The counterweight is Huang et al., `Large Language Models Cannot Self-Correct Reasoning Yet`
(ICLR 2024). Verified from the paper's tables:

- Intrinsic self-correction, no oracle (Table 3): GPT-3.5 on CommonSenseQA falls 75.8 to 38.1 after
  one round; GPT-4 on GSM8K falls 95.5 to 91.5 to 89.0 over two rounds.
- Table 4: Llama-2 on GSM8K falls 62.0 to 43.5 to 36.5 over two rounds, and on CommonSenseQA 64.0
  to 37.5 to 36.5.
- With oracle labels (Table 2) the same setup looks good - GPT-3.5 on GSM8K rises 75.9 to 84.3 - and
  the paper's point is that those improvements "vanish when oracle labels are not available",
  because the oracle is what decides when to stop.
- The paper also attributes multi-agent debate gains to self-consistency rather than
  self-correction: on GSM8K, self-consistency scores 85.3 at 6 responses and 88.2 at 9, against
  83.2 and 83.0 for debate.

Read together: a refinement loop is worth building when the feedback is external and objective, and
is actively harmful when the model grades itself. Code-generation loops work because execution is
that external grader. In the proposed architecture, the CFD/FEA run is the analogue of execution.
That is the strongest argument for step 3 existing at all.

A caveat on the title of the Huang paper. A secondary source argues the correct reading is "cannot
be prompted into self-correction" rather than "cannot learn to self-correct", citing a later
RL-trained self-correction result (SCoRe). That reframing and the SCoRe figures are UNVERIFIED.

## 2. Goal-Oriented Mesh Adaptation Already Is This Loop

The physics side of the proposed architecture has a mature, 25-year-old equivalent, and it is not an
LLM. Adjoint-based, goal-oriented error estimation and mesh adaptation measures error in a chosen
output functional - drag, lift, a stress at a point - and refines where it matters for that output.
The error estimate falls out of the adjoint solve as a by-product.

A 2021 comparative review of error estimators for anisotropic mesh adaptation, run through a unified
solver and adaptation code so the comparison is fair, reports that goal-oriented methods typically
reach higher accuracy in fewer adaptation steps on coarser meshes, and that feature-based indicators
can over-refine and are generally outperformed when the aim is a specific target quantity. Those
findings are taken from the review's abstract and summary material and are UNVERIFIED against the
full text.

The field's own account of why this matters is stronger. Verified from the abstract of
`Grid Quality and Resolution Issues from the Drag Prediction Workshop Series` (Mavriplis, Vassberg,
Tinoco, Mani, Brodersen, Eisfeld, Wahls, Morrison, Zickuhr, Levy, Murayama): across three AIAA Drag
Prediction Workshops, "an emerging consensus from the three workshop series has been the
identification of spatial discretization errors as a dominant error source in absolute as well as
incremental drag prediction". Discretization error, not element prettiness, is what the workshop
series found dominating the answer.

The learned variant is worth knowing because it shows what a loop over mesh refinement costs.
Adaptive Swarm Mesh Refinement (ASMR / ASMR++, Freymuth et al., KIT) formulates AMR as a system of
collaborating homogeneous agents that iteratively split, which permits a spatial reward focused on
reducing the maximum mesh element error. Verified from the arXiv v2 abstract (29 Jan 2026): ASMR
outperforms heuristic approaches and learned baselines, matches the performance of expensive
error-based oracle AMR strategies, generalizes across domains at inference, and produces meshes
that simulate up to two orders of magnitude faster than uniform refinement in demanding settings.
Verified from the body: the local reward for an element is 0 if no refinement is made, and the
reward depends on a fine-grained reference mesh whose computation is expensive. The paper also
states plainly that ML-based AMR methods "currently only scale to simple problems". The widely
quoted 30-fold speedup belongs to the earlier NeurIPS 2023 version and is UNVERIFIED here.

The implication for the proposed architecture is structural rather than prescriptive: the
mesh-and-resolve portion of the loop has a deterministic, well-studied answer, so whatever an LLM
adds is unlikely to be found there.

## 3. Repair And Robust Meshing

The repair side has one result that matters more than the rest, because it supplies a measurable
quantity connecting a repair to geometric fidelity.

`Fast Tetrahedral Meshing in the Wild` (fTetWild, Hu et al.) converts triangle soups into
tetrahedral meshes. Verified from the paper:

- It takes a 3D triangle soup and makes no assumptions on the input, allowing gaps and
  self-intersections.
- It has two user parameters: target edge length and envelope size epsilon. "The epsilon-envelope
  represents the maximal deviation from the" input.
- Output elements are non-inverted, that is, of positive volume, checked with exact predicates, and
  boundary faces approximate the input soup within the user-defined epsilon-envelope. The algorithm
  "always guarantees to generate a valid tetrahedral mesh with floating point vertex positions".
- Experiments use envelope size epsilon = 10^-3 d and target edge length d/20, "where d is the
  diagonal's length of" the bounding box of the input mesh. The commonly quoted default of
  bounding-box-diagonal over 1000 is therefore the experimental setting, stated as 10^-3 d.
- Non-manifold output is handled explicitly: the algorithm identifies non-manifold edges and splits
  them, then duplicates every non-manifold vertex to ensure a global manifold output, using the
  algorithm of Attene et al. 2009.
- The cost trade-off is explicit: checking envelope containment by sampling has a cost that grows
  quadratically as the envelope shrinks, so running time increases sharply for small envelopes.
- One honest limitation, from the abstract: unlike its predecessor TetWild, the method "no longer
  guarantees that all input triangles are present in the output mesh".

The classical CAD repair and defeaturing line is adjacent and less well covered by retrievable
primaries. An extended, tolerant B-rep representation that retains unsuppressed geometry so that
existing repair and meshing algorithms still apply to models with small gaps and overlaps; a Sandia
report by Owen and Shea using machine learning to predict the effect of defeaturing on tetrahedral
mesh quality, trained on automatically generated FEA meshes; and vendor topological reconstruction
that keeps a solid watertight after feature removal (CADfix, COMSOL CAD Import) are all relevant,
and all UNVERIFIED - only search-result abstracts and marketing copy were retrieved. The Owen and
Shea work is the closest external precedent for predicting downstream mesh success from a repair
decision, and is worth a proper read before any spec.

A workflow distinction from the Ansys/NASA meshing material is worth carrying: a watertight workflow
treats clean geometry as a prerequisite, while a fault-tolerant workflow wraps dirty or faceted
geometry instead. These are two different contracts between repair and meshing. UNVERIFIED beyond
presentation slides.

## 4. Domain Agents For CFD, FEA And CAD

The proposed architecture sits in a crowded 2025-2026 space. All titles below were verified to exist
at the cited arXiv identifiers.

### CFD

- `MetaOpenFOAM` (arXiv 2407.21320). Verified: a multi-agent framework reaching a pass rate per
  test of 85% on eight CFD simulation tasks, at an average of $0.22 per test case, with an
  executability score of 3.6. The most useful detail for a loop design is its ablation: removing
  the Reviewer agent dropped the pass rate from 85% to 27.5%, and without the Reviewer no
  iterations occurred at all. The verifier is the load-bearing component, not the orchestration.
- `MetaOpenFOAM 2.0` (arXiv 2502.00498). Verified: chain-of-thought decomposition and iterative
  verification, reaching an executability score of 6.3/7 and a pass rate of 86.9%, at an average of
  $0.15 per case. The claim circulating that version 1 scored 0% on this benchmark is UNVERIFIED.
- `Foam-Agent 2.0` (arXiv 2509.18178). Verified: on a benchmark of 110 simulation tasks, an 88.2%
  executable success rate with Claude 3.5 Sonnet, against 55.5% for MetaOpenFOAM and 37.3% for a
  third baseline. Success is measured as the percentage of cases that ran successfully through the
  framework given the prompt. An earlier reported figure of 83.6% is UNVERIFIED. Note that "ran
  successfully" is a solvability metric, not an accuracy metric.

### FEA

- `ALL-FEM` (arXiv 2603.21011). Verified: fine-tuned LLMs for FEniCS code generation, a corpus of
  1000+ verified scripts built from 500+ curated expert codes, models from 3B to 120B parameters,
  evaluated on 39 benchmarks. Embedded in a multi-agent workflow with runtime feedback, the best
  fine-tuned model (GPT OSS 120B) reaches code-level success of 71.79%, outperforming a non-agentic
  deployment of GPT 5 Thinking. The paper's framing is explicit that conventional LLMs "cannot
  close the loop from problem statement to a verified solution".
- `VFEAgent` (arXiv 2605.28978), a multimodal agent framework for end-to-end automated FEA;
  `PDE-Agents` (arXiv 2606.07850), an LLM-orchestrated multi-agent framework for automated finite
  element simulations with knowledge-graph-augmented reasoning; and a multi-AI-agent framework for
  end-to-end FEA in solid mechanics (arXiv 2606.00138). Titles and identifiers verified; the
  specific performance and design claims attributed to them are UNVERIFIED.

### CAD And Geometry

- `Embodied CAD` (arXiv 2606.31252). Verified from the abstract: rather than generating a complete
  script in one pass, the agent iteratively selects actions from a stratified L0-L4 CAD skill
  library, resolves them into typed geometric operations, executes them in a CAD backend, and uses
  solver feedback to plan, repair and learn. It combines action-grammar constraints, deterministic
  parameter resolution, and solver-derived rewards for supervised warm-up and GRPO-style
  refinement. Its metrics are solver-aligned: executable rate, skill accuracy, operation-family
  accuracy, exact policy accuracy, task completion success. This is the closest structural match to
  the proposed loop, and its grader is a geometric kernel, not a physics solver.
- `TraceCAD` (arXiv 2608.03062). Verified from the abstract: a recovery layer that links requested
  features, modeling steps, failure evidence and candidate outcomes as persistent state; it
  diagnoses likely faulty operations, searches bounded edits in their dependency regions, validates
  candidates through execution and preservation checks, and retains successful and failed repair
  outcomes in reusable skill memory. Ablations on DeepCAD-derived benchmarks: removing persistent
  state nearly halves the recovery score; removing localized search more than doubles geometric
  regression and doubles code-agent invocations. This is the only retrieved work that treats repair
  attribution across attempts as the problem.
- `AgentsCAD` (arXiv 2607.02448) and `CADIR` (arXiv 2608.00891). Titles verified; claims
  UNVERIFIED.

Across all of these, the loop closes on whether the setup runs and is kernel-valid. None of the
retrieved work closes a loop on repair quality judged by solver output error. That gap, and the
attribution problem behind it - getting a solver error to point back at a specific repair action,
when an adjoint gives sensitivity with respect to the mesh and the design variables rather than with
respect to "I sealed this hole by extending that face" - is the part the literature does not supply.

## 5. Cost: Surrogates And Sample Efficiency

Nobody retrieved here runs full-fidelity physics inside the inner loop.

Verified from `Sample-Efficient and Surrogate-Based Design Optimization of Underwater Vehicle Hulls`
(arXiv 2304.12420): Bayesian optimization is used for sample efficiency but "solves one specific
problem and struggles with transferability"; of the methods considered, Bayesian
Optimization-Lower Confidence Bound (BO-LCB) was the most sample-efficient with the best convergence
behaviour; a DNN surrogate predicted drag with a mean absolute percentage error of 1.85%; and
combining these gives "a two-orders-of-magnitude speedup (with comparable accuracy) for the design
optimization process when the surrogate model is used". The paper notes a CFD evaluation takes
10-20 minutes against roughly 0.002 s for the surrogate, and states elsewhere that the surrogate can
solve design optimization problems four orders of magnitude faster than using RANS. It uses
OpenFOAM, FreeCAD and Docker, and the code is open source.

Related and weaker: an AutoML-GA study reporting that conventional genetic and response-surface
methods can take months to find optimal engine designs, motivating active learning to cut the number
of CFD runs; and vendor guidance that a surrogate is only valid inside the range of its training
data, so the practice is to explore with the surrogate and validate final candidates with full CFD
or FEA. Both UNVERIFIED.

The relevant structural point is that every retrieved simulation-in-the-loop system has a cost
ladder of some kind, and the expensive solve is reserved for candidates that already survived
something cheaper.

## 6. Loop Failure Modes

This cluster turned out to be the most directly applicable, and the strongest single citation for
the whole architecture.

`When Do Agent Loops Mistake Stagnation for Progress?` (arXiv 2607.25152) names the failure the
"progress mirage" and measures it. Verified from the abstract:

- The testbed holds the agent and its tool surface fixed and manipulates only the information-channel
  type of the evaluator that gates the loop. A world-state oracle, unfakeable in principle, is
  enforced by container and network isolation and verified at every run.
- Across 54 cycles a frontier agent claimed improvement every time, yet 56 percent had a measured
  delta of zero or below. Self-report was uninformative, the self-verdict gate degenerated into
  accept-all, and it eroded the best deployed state it had reached by 19 percent.
- Even the strongest in-band judge - reading the full artifact text, the change diff and its own
  verdict history - accepted cycles of which 44 percent were real-world regressions, and rejected
  38 percent of real improvements. The preregistered hypothesis that a strong judge closes the gap
  was rejected.
- On a boundary task whose success specification is verifiable from the artifact itself, the mirage
  vanished to zero and the gap collapsed within the registered threshold. The gap depends on where
  the success signal resides.
- A sign-only variant returning only the acceptance verdict kept real-world output similar to full
  feedback (110.0 versus 113.0), locating the benefit in the gate's grounding rather than in
  feedback content.
- Conclusion as stated: for open-ended objectives whose success signal lives outside the transcript,
  scaling up the judge is not enough, and out-of-band evaluation with real-world access is a
  structural requirement.

`Hardening Agent Benchmarks with Adversarial Hacker-Fixer Loops` (arXiv 2606.08960) covers the
verifier side. Verified from the abstract: an audit of 1,968 tasks across five terminal-agent
benchmarks found 323 (16%) hackable by frontier models given only the task description. The
hacker-fixer loop alternates a hacker that tries to pass the verifier without solving the task, a
fixer that patches the verifier to reject each discovered exploit, and a solver that confirms the
patched verifier still admits legitimate solutions. On KernelBench the loop drives attack success
from 62% to 0% on a held-out corpus of publicly reported exploits, and weaker agents in the loop can
defend against stronger hackers (Gemini 3 Flash's loop drives Gemini 3.1 Pro and Claude Opus 4.7
from 76% and 61% to 0% on KernelBench, and Gemini 3.1 Pro from 39% to 17% on Terminal Bench across
77 tasks). The released artifact is Terminal Wrench, 323 hackable environments and 3,632 hack
trajectories.

Three further papers in this cluster exist at their cited identifiers -
`LLMs Gaming Verifiers: RLVR can Lead to Reward Hacking` (arXiv 2604.15149),
`Reward Hacking in the Era of Large Models` (arXiv 2604.13602), and
`Before the Model Learns the Bug: Fuzzing RLVR Verifiers` (arXiv 2606.01066) - but their contents
are UNVERIFIED. The practitioner guidance that the grader should be kept out of the agent's reach
and that held-out checks should be used is consistent with the two verified papers, but as a source
it is a blog post and UNVERIFIED.

No retrieved source offers a principled diminishing-returns stopping rule for iterative refinement.
The retrievable state of the art is an externally grounded check plus a retry budget.

## What Decides The Quality Of A Mesh

This was the question that prompted the deepest search, and the literature's answer is that there is
no single quantity, and that the cheap, geometry-only quantities are weak predictors of accuracy.

### Three Different Things Wear The Name

Validity is binary: inverted or degenerate elements, non-manifold edges, non-positive volumes, an
open surface. Element shape and conditioning is geometric, per-element and cheap: aspect ratio,
skewness, non-orthogonality, warpage, taper, condition number. Discretization error in a quantity of
interest is the thing an engineer actually cares about, and it needs a solve, often several. Most
tooling reports the second and calls it quality.

### The Evidence That Shape Metrics Do Not Predict Accuracy

The best single statement is Knupp's own, verified from the abstract of `Remarks on Mesh Quality`
(SAND2007-8128C, 45th AIAA Aerospace Sciences Meeting, 2007):

> Various aspects of mesh quality are surveyed to clarify the disconnect between the traditional
> uses of mesh quality metrics within industry and the fact that quality ultimately depends on the
> solution to the physical problem. Truncation error analysis for finite difference methods reveals
> no clear connection to most traditional mesh quality metrics. Finite element bounds to the
> interpolation error can be shown, in some cases, to be related to known quality metrics such as
> the condition number. On the other hand, the use of quality metrics that do not take solution
> characteristics into account can be valid in certain circumstances, primarily as a means of
> automatically detecting defective meshes. The use of such metrics when applied to simulations for
> which quality is highly-dependent on the physical solution is clearly inappropriate.

The same paper closes by advocating "explicitly-referenced" quality metrics as a way to bridge a
priori and solution-dependent metrics, and verified body text identifies the condition number as an
important example of an explicitly-referenced algebraic metric, notes that the relevant bound "goes
to infinity as the element becomes degenerate due to some internal angle approaching zero or pi",
and criticises the arbitrariness of rejection thresholds: the common practice of rejecting any
element whose metric falls below some threshold T has, in the paper's reading, no justification, and
the lack of justified threshold values is listed as open work.

Shewchuk's `What Is a Good Linear Finite Element?` (11th International Meshing Roundtable, 2002)
supplies the theory, verified from the paper and its slides:

- Quality measures disagree with each other: "small angles are bad for matrix conditioning but not
  for interpolation or discretization".
- "there are circumstances wherein interpolation, conditioning, and discretization error are each
  best served by elements of different aspect ratios or" orientations.
- Equilateral is not a universal ideal: there are circumstances "where a triangle with a 179.9
  degree angle performs excellently, and an equilateral triangle dismally", and equilateral elements
  "may sharply increase lambda-max for anisotropic PDEs".
- The slides state the ideal element for the stiffness matrix depends on the anisotropy "of the PDE,
  not the solution", and advise choosing "small discretization error over interpolation".

Lowrie et al., `A priori mesh quality metric error analysis applied to a high-order finite element
method` (Journal of Computational Physics 230, 2011) is often cited as the null result, and the
citation needs care. Verified: the sentence "Many metrics do not show a strong global correlation to
solution accuracy, mainly because of the variety of problems that have to be considered and
complexity of correlating them to solution error" appears in their introduction and cites Knupp and
Berzins, so it is their summary of prior work, not their own finding. Their own result is more
favourable to metrics: using a chi-square-style goodness-of-fit statistic against three test
problems (advection, Poisson, linearized MHD), "for all but the skew deformation type the solution
error norm has the highest probability of following the Knupp metric for all test problems", while
"in the skew case the stiffness matrix condition number has the lowest" statistic; and relative to a
baseline value of about 0.5 for a metric with no relationship, "the metrics with v2 < 0.25 provide
statistically significant correlation to solution error". The paper's stated goal was a "universal
metric" across equation systems, and its framing notes that a priori metrics depend solely on mesh
geometry and are independent of the equations being solved, which is what makes correlation hard.

Gao et al., `Evaluating Hex-mesh Quality Metrics via Correlation Analysis` (Computer Graphics Forum,
SGP 2017) classifies metrics into groups by correlation and reports that conditioning-based measures
track elliptic-PDE solution quality more closely than others. Verified from the authors' own
presentation slides: a positive minimum scaled Jacobian is a hard requirement for conducting
PDE-based simulation, and the paper's question is whether, once that minimum is met, the scaled
Jacobian is still the most effective indicator; the correlation study separates
application-independent from application-dependent correlation, with the latter measured on elliptic
PDE solves. The precise abstract wording about conditioning-based measures is abstract-level only
and the full text is UNVERIFIED.

The frequently repeated trio of null results - Dannenhoffer finding little if any correlation
between grid metrics and solution accuracy, Roy's counterexample where a 2D Burgers solution on an
adapted mesh of widely varying skew and aspect ratio has much less discretization error than on a
mesh of perfect squares, and McDaniel finding correlation with the caveat that a well-resolved mesh
of poor quality can still give a good answer - is UNVERIFIED. These were attributed to Knupp's
`Remarks on Mesh Quality` by a secondary source, and a text search of the retrieved primary found
none of those three names. If they matter to a decision, they need to be traced to the correct
primary first.

### What Differs Between CFD And FEA

The difference is structural, not cosmetic. Finite-volume CFD accumulates error through fluxes
across faces, so its metrics are face-based: non-orthogonality, the angle between the face normal
and the line joining cell centres, which corrupts gradient and Laplacian terms and is partly
correctable numerically; and skewness, the offset of the face intersection from the face centre.
Alongside those sit requirements that are not shape at all - wall-normal spacing expressed as y+,
growth rate, cells across a feature. And extreme anisotropy is deliberate: wall-normal stretching is
the point of a boundary-layer mesh.

Verified from the OpenFOAM source
(`src/OpenFOAM/meshes/primitiveMesh/primitiveMeshCheck/primitiveMeshCheck.C`, OpenFOAM-10), the
built-in defaults are:

```
Foam::scalar Foam::primitiveMesh::closedThreshold_  = 1.0e-6;
Foam::scalar Foam::primitiveMesh::aspectThreshold_  = 1000;
Foam::scalar Foam::primitiveMesh::nonOrthThreshold_ = 70;    // deg
Foam::scalar Foam::primitiveMesh::skewThreshold_    = 4;
```

Each is settable at runtime through `setClosedThreshold`, `setAspectThreshold`,
`setNonOrthThreshold` and the skew equivalent. Community recommendations are far stricter and
mutually inconsistent - skewness limits of 2, 4, 0.95 and 0.2 all appear in training material and
blogs - and all of those stricter figures are UNVERIFIED. One portability trap is worth recording:
OpenFOAM's definition of skewness differs from other packages, so values are not comparable across
tools. That claim is UNVERIFIED but is repeated in the OpenFOAM user-manual material. The claim that
no numerical corrector exists for skewness, unlike non-orthogonality, is also UNVERIFIED.

Element-based FEA accumulates error through the stiffness matrix, so its metrics are element-based:
the sign of the Jacobian for inversion, the scaled Jacobian, warpage, taper and distortion.
Aspect-ratio tolerance is much tighter, because there is no boundary layer to justify extreme
stretching. Ansys documentation gives a determinant above 0.3 as acceptable for most solvers and
scales warpage from 0 to 90 degrees; both figures are vendor documentation and UNVERIFIED against
any independent source.

One trap here is verified, and the earlier informal account of it in chat was imprecise.
`Mesh Quality Metrics for Isogeometric Bernstein-Bezier Discretizations` (arXiv 1810.06975) states
that the scaled Jacobian "is equal to one for a linear finite element", so it carries no information
about a linear element's distortion at all; and separately that for curvilinear elements it can be
actively misleading - the paper exhibits a highly distorted cubic Bernstein-Bezier triangle with a
scaled Jacobian of exactly 1, and remarks that "the most commonly used quality metric for
curvilinear elements cannot distinguish between this highly skewed element and a purely linear
triangle". It also notes families of meshes with scaled Jacobian bounded away from zero that still
show sub-optimal convergence rates. The pathological skewed example is the curvilinear one, not a
linear triangle.

The net asymmetry: extreme anisotropy is a feature in CFD and usually a defect in FEA, which is an
argument against any single scalar spanning both.

### Does It Change By Industry

The underlying mathematics does not move. Thresholds and resolution recipes do, and they are scoped
to a solver class rather than universal.

Aerospace external aerodynamics is the most codified case. Verified in full from the
`1st AIAA CFD High Lift Prediction Workshop Gridding Guidelines`, 11 June 2009:

- Initial spacing normal to all viscous walls, at Re = 4.3M based on CREF = 39.6 inches: coarse
  y+ ~ 1.0 (dy = 0.00020"), medium y+ ~ 2/3 (0.00013"), fine y+ ~ 4/9 (0.00009"), extra-fine
  y+ ~ 8/27 (0.00006").
- At least 2 cell layers of constant spacing normal to viscous walls.
- Total grid size to grow ~3X between each grid level for grid-convergence cases; for structured
  meshes, ~1.5X in each coordinate direction.
- Grid-convergence cases must maintain the same grid family between levels - same stretching
  factors, same topology.
- Growth rate in the viscous layer (GR1) below 1.25 for all grids, plus a region of constant
  spacing (GR2 = 1.0) to capture the wake from upstream elements, that region being roughly 2X the
  slat/flap gap at the wing root.
- Farfield at ~100 CREF for all grid levels.
- Medium grids: chordwise spacing at leading and trailing edge ~0.1% local device chord; spanwise
  spacing ~0.1% local semispan at root and tip; cell size near body nose and tail ~2.0% CREF.
- Trailing-edge base: minimum 4 cells across for coarse, 6 for medium, 9 for fine, 14 for
  extra-fine.
- Separate conventions for vertex-based and cell-centred unstructured solvers, with tetrahedral
  cell-centred meshes expected at roughly 1/3 of the listed point counts.

The scope limit on all of that is stated by the workshop series itself. Verified from
`HLPW-4/GMGW-3: Overview and Workshop Summary` (Rumsey, NASA): "These RANS-based guidelines were not
necessarily appropriate for the HO, ADAPT, HRLES, or WMLESLB TFGs", referring to the higher-order,
adaptive, hybrid RANS/LES and wall-modelled LES task groups, which went on to explore and refine
their own best practices during workshop preparation. The most mature guideline set in the field is
explicitly scoped to fixed-grid RANS.

Cross-industry verification practice answers the question differently again. Roache's Grid
Convergence Index, as used under ASME V&V 20 practice, makes the quality number a property of a mesh
family rather than of a mesh. From a retrieved application paper applying GCI to patient-specific
aorta CFD (arXiv 2309.05509): the safety factor "was set by Roache as 3.0 for two different grid
spacings, and 1.25" for three or more; and "the apparent convergence condition must be within the
monotonic convergence range to use the generalized Richardson extrapolation method", with a separate
check for whether numerical convergence is within the asymptotic range. That paper's own results
illustrate the fragility: tetrahedral meshes showed non-monotonic convergence or divergence at
points that had to be discarded, while polyhedral and poly-hexacore triplets reached the asymptotic
range with monotonic convergence. The safety-factor values are verified only through this secondary
source; the ASME V&V 20 standard text itself was not retrieved, and the often-quoted recommended
refinement ratio of about 1.3 is UNVERIFIED.

Other sectors - biomedical patient-specific flow, structural stress concentration, electronics
cooling, turbomachinery - differ mainly in which output functional matters and where the thresholds
sit, not in the structure of the problem.

### What The Literature Constrains

Stated as constraints the review found, not as design decisions:

- A refinement loop needs a grader the agent does not control, and the measured cost of getting this
  wrong is large (arXiv 2607.25152; Huang et al.).
- Geometry-only element metrics are defensible as automatic detectors of defective meshes and are
  explicitly described as inappropriate as a proxy for solution quality where quality depends on the
  physical solution (Knupp, `Remarks on Mesh Quality`).
- No single mesh-quality scalar is physics-agnostic; interpolation, conditioning and discretization
  error can each prefer different elements (Shewchuk).
- A quality number that satisfies verification practice is a property of a mesh family and costs at
  least three solves plus a monotonicity check (GCI under ASME V&V 20 practice).
- The expensive solve is reserved for survivors of something cheaper in every retrieved
  simulation-in-the-loop system.
- Thresholds are solver- and solver-class-specific, and definitions differ between packages, so
  numeric limits are not portable.
- Nothing retrieved closes a loop from solver output error back to a geometry repair action. That
  attribution step is unsolved in the retrieved literature.

## Verification Notes

Verified against a retrieved primary source: Huang et al. tables; `SELF-REFINE` abstract and body;
fTetWild parameters, guarantees, non-manifold handling and cost trade-off; Shewchuk paper text and
slides; Knupp `Remarks on Mesh Quality` abstract and body; Lowrie et al. attribution, method and
conclusions; the isogeometric scaled-Jacobian results; OpenFOAM `primitiveMeshCheck.C` default
thresholds; the HLPW-1 gridding guidelines in full; the HLPW-4/GMGW-3 scope statement; the DPW grid
quality abstract; MetaOpenFOAM, MetaOpenFOAM 2.0 and Foam-Agent 2.0 headline figures and the
MetaOpenFOAM Reviewer ablation; ASMR++ abstract and reward/reference-mesh details; the underwater
hull abstract; ALL-FEM, Embodied CAD, TraceCAD, the progress-mirage paper and the hacker-fixer paper
abstracts. The existence and exact titles of all fifteen cited arXiv identifiers were checked
individually.

Could not be verified, and marked UNVERIFIED above:

- The 2021 comparative review of anisotropic error estimators - abstract and secondary summaries
  only.
- The ASMR 30-fold speedup from the NeurIPS 2023 version; the retrievable v2 figure is two orders
  of magnitude.
- Gao et al. full text; verified only through the authors' slides and the published abstract.
- The Dannenhoffer, Roy Burgers-equation and McDaniel null results - attributed to Knupp by a
  secondary source, absent from the retrieved primary, primary not located.
- ASME V&V 20 standard text; GCI safety factors verified only via a secondary application paper; the
  recommended refinement ratio of ~1.3 not verified at all.
- NASA CFD Vision 2030's identification of meshing and adaptivity as bottlenecks.
- Owen and Shea's ML defeaturing report; the extended tolerant B-rep paper; Berzins 1999.
- All vendor claims: Ansys determinant 0.3 and warpage scale, CADfix, COMSOL, Cadence, SimScale, and
  the watertight versus fault-tolerant workflow distinction.
- Community OpenFOAM skewness and aspect-ratio recommendations; the differing skewness definition
  across packages; the absence of a numerical corrector for skewness.
- VFEAgent, PDE-Agents, the solid-mechanics multi-agent framework, AgentsCAD and CADIR beyond their
  titles.
- `LLMs Gaming Verifiers`, `Reward Hacking in the Era of Large Models` and the RLVR verifier-fuzzing
  paper beyond their titles.
- MetaOpenFOAM 2.0's reported 0% for version 1; Foam-Agent's earlier 83.6% figure.
- The AutoML-GA engine-design study.
- The SCoRe reframing of the Huang result.
- Secondhand Self-Refine figures of 8.7 and 13.9 points from a vibe-coding survey; superseded here
  by the primary paper's own ~20% average, 5-40% range and up-to-13% code figure.
- Knupp's SIAM `Algebraic Mesh Quality Metrics` and the Target-Matrix Paradigm reports. OSTI
  returned HTTP 429 for both. The claims that the element condition number metric is equivalent to
  the mean ratio metric, and that the condition number measures distance to the set of degenerate
  elements, are therefore UNVERIFIED, although the retrieved `Remarks on Mesh Quality` does
  independently describe the condition number as an explicitly-referenced metric whose interpolation
  bound diverges as an element degenerates.

Two corrections to statements made while discussing this material in chat, recorded because they
change what the sources support. First, Lowrie et al.'s "no strong global correlation" sentence is
their summary of Knupp and Berzins, not their own finding; their own result is that Knupp's metrics
and the stiffness-matrix condition number did correlate significantly on their test problems.
Second, the scaled-Jacobian pathology is that the metric is identically one for linear elements and
so uninformative there, while the highly skewed element with a scaled Jacobian of one that the
isogeometric paper exhibits is a cubic curvilinear element.

## Open Decisions For The User

These are the decisions the review surfaced and deliberately does not make. They are listed in the
order the literature suggests they bind, not in order of importance.

1. **What counts as a defective session.** Step 3 of the proposed architecture runs isolated
   CFD/FEA on "the defective sessions", and the review found no external definition that transfers.
   Candidate signals sit at three different costs and three different levels of trust: validity
   failures, geometry-only element metrics, and discretization error in an output functional. Knupp
   is explicit that the middle layer is defensible for automatically detecting defective meshes and
   inappropriate as a stand-in for solution quality, which makes this a choice about which layer
   defines "defective" rather than a free parameter.

2. **Automated or human-gated.** The progress-mirage result makes this a question about where the
   gate's success signal lives rather than about autonomy: an in-band judge reading artifacts and
   diffs accepted 44 percent real regressions and rejected 38 percent real improvements, while a
   gate grounded outside the transcript closed the gap. Whether this harness has access to such an
   out-of-band signal, and whether a human supplies it, is undecided.

3. **Termination criteria.** No retrieved source offers a principled diminishing-returns stopping
   rule. The retrievable options are an externally grounded check plus a retry budget, or the
   verification-practice route where convergence is judged over a mesh family with a monotonicity
   and asymptotic-range check. The second is defensible and expensive; the first is cheap and
   unprincipled. Undecided.

4. **Cost per iteration.** Every retrieved simulation-in-the-loop system reserves the expensive
   solve for candidates that survived something cheaper, and the numbers available span a wide
   range: $0.15 to $0.22 per case for the OpenFOAM agent frameworks, 10-20 minutes per CFD
   evaluation against 0.002 s for a trained surrogate in the hull study, at least three solves for a
   GCI-style convergence claim, and a fTetWild envelope cost that grows quadratically as the
   envelope shrinks. What budget one iteration of this loop is allowed, and what "isolated" means
   geometrically for step 3, are undecided.

Two further questions came up in discussion and are recorded here without answers, because they
change what the four decisions above mean: which output functional the loop would be trying to
improve, since an adjoint-based error estimate requires one and "higher fidelity" is not a
functional; and which solvers the harness must drive, since adjoint support is what determines
whether localized error attribution is available at all.

## Sources

Retrieved and verified:

- Madaan et al., `SELF-REFINE: Iterative Refinement with Self-Feedback` -
  https://arxiv.org/abs/2303.17651
- Huang et al., `Large Language Models Cannot Self-Correct Reasoning Yet` (ICLR 2024) -
  https://arxiv.org/abs/2310.01798 (tables read via https://ar5iv.labs.arxiv.org/html/2310.01798)
- Hu et al., `Fast Tetrahedral Meshing in the Wild` -
  https://arxiv.org/abs/1908.03581 ; project page https://wildmeshing.github.io/ftetwild/
- Shewchuk, `What Is a Good Linear Finite Element? Interpolation, Conditioning, Anisotropy, and
  Quality Measures` (11th International Meshing Roundtable, 2002) -
  https://people.eecs.berkeley.edu/~jrs/papers/elemj.pdf ; slides
  https://people.eecs.berkeley.edu/~jrs/papers/elemtalk.pdf
- Knupp, `Remarks on Mesh Quality` (SAND2007-8128C, 45th AIAA ASM, 2007) -
  https://www.osti.gov/servlets/purl/1146104
- Lowrie et al., `A priori mesh quality metric error analysis applied to a high-order finite element
  method` (J. Comput. Phys. 230, 2011) -
  https://www.aa.washington.edu/sites/aa/files/research/cpdlab/docs/Lowrie_JCP2011.pdf
- `Mesh Quality Metrics for Isogeometric Bernstein-Bezier Discretizations` -
  https://arxiv.org/abs/1810.06975
- OpenFOAM `primitiveMeshCheck.C` (OpenFOAM-10), default check thresholds -
  https://github.com/OpenFOAM/OpenFOAM-10/blob/master/src/OpenFOAM/meshes/primitiveMesh/primitiveMeshCheck/primitiveMeshCheck.C
- `1st AIAA CFD High Lift Prediction Workshop Gridding Guidelines`, 11 June 2009 - original host
  hiliftpw.larc.nasa.gov now redirects; retrieved via
  https://web.archive.org/web/2018/https://hiliftpw.larc.nasa.gov/Workshop1/GriddingGuidelinesHiLiftPW1-11JUN09.pdf
- Rumsey, `HLPW-4/GMGW-3: Overview and Workshop Summary` -
  https://ntrs.nasa.gov/api/citations/20220005271/downloads/hlpwsummary_paper_09.pdf
- Mavriplis et al., `Grid Quality and Resolution Issues from the Drag Prediction Workshop Series` -
  https://ntrs.nasa.gov/api/citations/20080008566/downloads/20080008566.pdf
- Freymuth et al., `Adaptive Swarm Mesh Refinement using Deep Reinforcement Learning with Local
  Rewards` (ASMR++) - https://arxiv.org/abs/2406.08440
- Chen et al., `MetaOpenFOAM` - https://arxiv.org/abs/2407.21320 ; `MetaOpenFOAM 2.0` -
  https://arxiv.org/abs/2502.00498
- Yue et al., `Foam-Agent 2.0` - https://arxiv.org/abs/2509.18178 ; code
  https://github.com/csml-rpi/Foam-Agent
- `ALL-FEM: Agentic Large Language models Fine-tuned for Finite Element Methods` -
  https://arxiv.org/abs/2603.21011
- `Embodied CAD: Solver-Grounded LLM Agents for Parametric B-Rep Assembly Modeling` -
  https://arxiv.org/abs/2606.31252
- `TraceCAD: Trace-Guided Repair for Agentic CAD Generation` - https://arxiv.org/abs/2608.03062
- `When Do Agent Loops Mistake Stagnation for Progress?` - https://arxiv.org/abs/2607.25152
- `Hardening Agent Benchmarks with Adversarial Hacker-Fixer Loops` -
  https://arxiv.org/abs/2606.08960
- `Sample-Efficient and Surrogate-Based Design Optimization of Underwater Vehicle Hulls` -
  https://arxiv.org/abs/2304.12420
- `Evaluating Uncertainties in CFD Simulations of Patient-Specific Aorta Models using Grid
  Convergence Index Method` - https://arxiv.org/abs/2309.05509 (secondary source for GCI safety
  factors)

Cited but not verified against a primary source, listed so the next reader knows what still needs
pulling:

- Gao et al., `Evaluating Hex-mesh Quality Metrics via Correlation Analysis` (CGF / SGP 2017) -
  https://doi.org/10.1111/cgf.13249 (paywalled; authors' slides at
  https://www2.cs.uh.edu/~chengu/Publications/HexMesh/eval/HexEval_SGP17.pdf ; code
  https://github.com/gaoxifeng/Evaluation_code_SGP2017)
- `A review and comparison of error estimators for anisotropic mesh adaptation for flow simulations`
  - https://www.sciencedirect.com/science/article/abs/pii/S0045793021003601
- Knupp, `Algebraic Mesh Quality Metrics` (SIAM J. Sci. Comput.) -
  https://www.osti.gov/servlets/purl/754328 and `Formulation of a Target-Matrix Paradigm for Mesh
  Optimization` - https://www.osti.gov/servlets/purl/1143308 (both returned HTTP 429)
- Owen and Shea, `CAD Defeaturing Using Machine Learning` -
  https://www.osti.gov/servlets/purl/1640901
- `Automatic surface repairing, defeaturing and meshing algorithms based on an extended B-rep` -
  https://www.researchgate.net/publication/275673435
- Chitta et al., `A Guided, Fluent Meshing Workflow for the Aerospace and ...` (NASA AMS 2021) -
  https://www.nas.nasa.gov/assets/nas/pdf/ams/2021/AMS_20210817_Chitta.pdf
- `VFEAgent` https://arxiv.org/abs/2605.28978 ; `PDE-Agents` https://arxiv.org/abs/2606.07850 ;
  solid-mechanics multi-agent FEA https://arxiv.org/abs/2606.00138 ; `AgentsCAD`
  https://arxiv.org/abs/2607.02448 ; `CADIR` https://arxiv.org/abs/2608.00891
- `LLMs Gaming Verifiers: RLVR can Lead to Reward Hacking` https://arxiv.org/abs/2604.15149 ;
  `Reward Hacking in the Era of Large Models` https://arxiv.org/abs/2604.13602 ;
  `Before the Model Learns the Bug: Fuzzing RLVR Verifiers` https://arxiv.org/abs/2606.01066
- `Application of an automated machine learning-genetic algorithm (AutoML-GA) ...` -
  https://arxiv.org/abs/2101.02653
- Ansys pre-mesh quality documentation (determinant, warpage) -
  https://ansyshelp.ansys.com/public/views/secured/corp/v251/en/icm_user/iuser_premeshqual.html
