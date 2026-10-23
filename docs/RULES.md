# The rulebook

Every decision a mesh run makes on its own is made by a written rule. Each rule has a stable ID,
and each decision the run records in its "why" record names the rule behind it
(`contracts/decision_record.py`). This file is the one home for those rules.

Rulebook version: **13**

How the book is kept honest:

- A rule's ID never changes and is never reused. A rule that stops applying is marked `retired`,
  not deleted.
- **In force** means the code makes this decision today. **Planned** means the ID is reserved
  for a later PR (named in brackets) and no code may cite it yet.
- `tests/unit/hygiene/test_rulebook.py` checks, on every PR, that every rule ID the code cites is
  in force here, that every in-force rule here is cited by the code, and that the version above
  matches `RULES_VERSION` in `contracts/decision_record.py`.
- Changing what a rule decides means changing this book in the same PR and raising the version.

Each decision in the record also says where it came from:

| Source | Meaning |
|---|---|
| `rule` | The code decided by the rule, from measured or declared facts. |
| `user` | The user decided (named an engine, or confirmed one). The rule says why that stands. |
| `ai-lever` | The AI proposed one approved lever, inside its limits (not used yet: AI levers are off). |
| `guard-clock` | A clock decided. These are being removed (D3); each one is listed so a run that differs here is explained. |

---

## ENG: which engine meshes the run

| ID | Status | Rule |
|---|---|---|
| ENG-1 | in force | **Only engines that can take the case are ranked.** An engine is a candidate only if its declared capability covers the flow, the file's form, the number of separate passages and the layers asked for. |
| ENG-2 | in force | **Home turf first.** An engine that declares the measured shape class its home (`EngineSpec.home_classes`: VMTK for tubes and branching passages) comes first, unless its own turf rows mark the case a weak spot or outside. Two home engines rank between themselves by ENG-4. |
| ENG-3 | in force | **The lab record overrides home turf only when it clearly wins:** the challenger has at least 20 runs on at least 8 different shapes of this class, no quality fault, and a 95% lower bound above the home engine's 95% upper bound. A home engine whose own record of 20+ runs here shows a quality fault also loses first place. A home engine never tried on the class keeps it ("home turf unproven"). |
| ENG-4 | in force | **The best lab record on shapes like this one comes first.** The run's geometry is measured, and engines are ranked by the Wilson 95% lower bound of their pass rate on its shape class (`engines/fitness.py`), each run short of 20 counted as a miss, so 7 of 8 never beats 14 of 19. The table is dev-side runs only, versioned (`docs/engines/fitness-changelog.md`). An engine with a quality fault ranks after the sound ones; an engine whose declared turf puts the shape outside is never recommended. Ties go to fewer broken meshes, then fewer bad cells, then the declared turf, layers and ladder order. Time never breaks a tie. |
| ENG-5 | in force | **The fallback ladder walks the same ranking**, after the approved engine, minus the engines already tried and the engines that would refuse the case. With nothing measured, the same rules rank the engines on their record over the whole flow, never a fixed order. |
| ENG-6 | in force | **The user's pick stands.** An engine the user named is never switched on its own; another engine is only offered. A dispute keeps the engine of the mesh it disputes. |
| ENG-7 | in force | **An engine that reads the upload's own format comes first**, measured or not: a conversion loses what the file describes (an ECXML's parts, materials and powers). |
| ENG-8 | in force | **Nothing measured, nothing named: the declared default.** When the run's geometry could not be measured, a run nobody chose an engine for takes the engine the declarations name for its flow (or the flow-only order). |
| ENG-9 | in force | **The user confirmed an engine the measured ranking did not put first** (for example, the ranking was not ready while the user was asked). The confirmed engine stands. This is a gap D1 closes: the proposal should always be the ranked one. |

## SW: whether a run moves to another engine after a failed attempt

Decided by `pipeline/engine_fallback.decide`. The run moves only on measured facts, never on a
review. The order the run moves in is ENG-5.

| ID | Status | Rule |
|---|---|---|
| SW-1 | in force | **Move on: the mesh came out broken** (checkMesh's fatal defects). The next engine in the ladder runs at once. |
| SW-2 | in force | **Move on: the engine could not build a mesh** (it crashed, stopped early or refused the input). |
| SW-3 | in force | **Move on: the same measured failure repeated** on this engine. |
| SW-4 | in force | **Move on: the shared attempts go to the untried engine** when a retry here would leave it no attempt of its own. |
| SW-5 | in force | **Stay: a measured check failed, and this engine's own levers are tried first**, because a retry here still leaves every untried engine an attempt. |
| SW-6 | in force | **Stay: a retry here fits** and still leaves every untried engine an attempt. |
| SW-7 | in force | **Stay: a review never moves the run.** The mesh passed every measured check. |
| SW-8 | in force | **Stay: the engine is not ours to change** (the user named it, or a dispute; see ENG-6). |
| SW-9 | in force | **Stay: another engine cannot change this failure** (the request, the approval or a region split), or the mesh is sound and only short of one of our own bars: its own resolution ladder answers it (LAD-cfd-1), never a switch. |
| SW-10 | in force | **Stay: no untried engine delivers the approved mesh** (same boundaries, units, cell kind and layers). |
| SW-11 | in force | **Stay: the untried engines would refuse** this many separate passages in one job. |
| SW-12 | in force | **Stay: no regular attempt is left.** |
| SW-13 | in force | **Stay: too little time left** for the next engine's run. A clock decides this one (source `guard-clock`); D3 removes it. |
| SW-14 | in force | **Stay: the fallback ladder is switched off** by a setting. |
| SW-15 | in force | **No decision: the last attempt did not fail.** |
| SW-16 | in force | **Move on, to another kind of mesh: the system's own pick made no mesh, or a broken one** (a time-out, a crash, a stop before meshing, broken cells), and no untried engine delivers the same kind. The next engine in the ranking takes the run when the only differences are the mesh's file format and cell type: never fewer layers, a worse wall fit, a lost requirement or a setting. The user is told what changes, as it happens and beside the delivered mesh. Never for an engine the user named (ENG-6). |

## DEL: which valid mesh a run delivers

When a run built more than one valid mesh, `application/review_delivery.rank_key` ranks them by a
fixed order of measured facts. The first fact that separates the winner from the runner-up is the
rule. An invalid mesh is never delivered.

| ID | Status | Rule |
|---|---|---|
| DEL-0 | in force | **The only valid mesh** the run built is the one delivered. |
| DEL-1 | in force | **A mesh the review confirmed is the wrong problem ranks last** (a cited, inspected wrong-problem finding, not a concern). Shadow period only: once `VERDICT_BY_LIMITS` is on, a review ranks nothing and this position never separates two meshes. |
| DEL-2 | in force | **A mesh that passes every fixed quality limit beats one that does not.** |
| DEL-3 | in force | **Fewer failed quality checks wins.** |
| DEL-4 | in force | **Meeting more of the approved plan wins** (layers on the wall, wake refinement, stated sizes). |
| DEL-5 | in force | **Better worst cells win**, compared to the whole degree of non-orthogonality. |
| DEL-6 | in force | **On a measured tie, the mesh whose review passed wins.** Shadow period only: once `VERDICT_BY_LIMITS` is on, a review ranks nothing and a tie on everything measured goes to the earlier attempt (DEL-10). |
| DEL-7 | in force | **Lower worst non-orthogonality wins** (exact value). |
| DEL-8 | in force | **Lower worst skewness wins.** |
| DEL-9 | in force | **More wall covered by prism layers wins.** |
| DEL-10 | in force | **The earlier attempt wins** when everything measured is the same. |

## VER: pass, delivered with concerns, or fail

Decided by `contracts/decision_record.verdict` from measured numbers alone, read off the delivered
mesh by `engines/verdict_limits.py`. Every finished run's why record carries one `verdict` entry:
the outcome, the VER rule, and every row below with its measured number, its limit and its
source, so the verdict replays from the record. Nothing the AI reviewer wrote is an input.

**The AI reviewer only adds notes.** It still looks at the pictures. Its points are shown to the
customer under "Reviewer notes", each one labelled "Reviewer note". It cannot change a status,
start a rebuild, a bonus attempt or a retry, rank one mesh over another, or fail a job as the
wrong problem. A user's own dispute ("rebuild it") is the user's decision and still rebuilds.

**Shadow first.** The setting `VERDICT_BY_LIMITS` (off by default) switches the limits verdict on.
While it is off, the review-led verdict still decides, and the limits verdict is worked out on
every run beside it: one `verdict shadow:` log line (limits outcome, rule, review-led outcome,
whether they agree, the limits missed) and the `verdict` entry in the why record.

| ID | Status | Rule |
|---|---|---|
| VER-1 | in force | **Fail: no valid mesh.** The executor's gates refused the last attempt, or the mesh's own numbers fail one of the engine's gating rows (its `criteria.py` rows marked gating: checkMesh fatal defects, wall captured, ports captured, the quality floors, regions and interfaces). An invalid mesh is never delivered. The resolution floor, the share of skewed faces and the one-job cell limit are not validity: on a sound mesh they are notes (LAD-cfd-3, LAD-cfd-4). |
| VER-2 | in force | **Delivered with concerns:** the mesh is valid but misses at least one production limit (table below). Each one is listed as measured vs limit. |
| VER-3 | in force | **Clean pass:** valid, and every production limit that applies is met (a limit whose number was not measured is listed as unmeasured and decides nothing). |
| VER-4 | in force | **Wrong problem, by measured checks only.** The run failed at a gate that measures one of the ways a mesh can be a different problem than the one confirmed (table below). The gate's own measured account is what the user reads. |

### VER-2 production limits, per engine and purpose

| Limit | Engines | Applies to | Limit value | Source |
|---|---|---|---|---|
| Worst face non-orthogonality | snappy, cfMesh, multi-region | every flow | at most 70 degrees | checkMesh warns above 70 |
| Worst face non-orthogonality | gmsh | external and internal flow (not FEA) | at most 85 degrees | gmsh's own gating bar, so a mesh past it never validates |
| Share of skewed faces | snappy, cfMesh | every flow | at most 0.05% | checkMesh's skewness bars as a share of all faces; a production limit on a sound mesh, never a validity bar (cfMesh often does not report it) |
| Wall covered by prism layers | snappy, cfMesh, multi-region, VMTK | when the approved plan builds layers | at least 80% | lab history: below it wall shear and y+ are unreliable on a material share of the wall |
| Layers built short of the layers asked | cfMesh | when the approved plan builds layers | none short | the layer census (layers in the meshDict vs the request) |
| Connected cell regions | snappy, cfMesh, gmsh, VMTK | every flow | at most 1 | checkMesh's region count: a second region is cut off from the flow |
| Cells across the narrowest passage | snappy, cfMesh, gmsh, VMTK | internal flow | at least the resolution floor (12) | the resolution floor's own reading; narrow side passages it set aside are listed here |
| Stated requirements met | every engine | every flow | no near-miss | the executor's measured requirement checks (`requirement_caveats`) |
| A believable size for what it is | every engine | every flow | no size concern | the unit-plausibility check on the delivered body |
| The engine's own declared advisory lists | snap-grid | every flow | empty | `engines/snapgrid/criteria.py` (thin layers short, resolution short) |
| Worst scaled Jacobian, over the whole curved element | every FEA adapter (gmsh today) | a mesh built from an FEA plan | at least 0.2 for straight (4-node) tetrahedra; at least 0.01 for curved (10-node) ones | Cubit/Verdict's acceptable range for straight tetrahedra. A curved element's figure reads its curving as well as its shape, and none of the version-1 solvers checks it (they check validity), so a curved mesh is noted only under 0.01 (Areen, 22 Oct) |
| Worst Jacobian ratio across an element's nodes | every FEA adapter | a mesh built from an FEA plan | at most 30 | Ansys MAPDL's corner-node warning |
| Worst aspect ratio (1 is a perfect element) | every FEA adapter | a mesh built from an FEA plan | at most 20 | Ansys MAPDL's aspect-ratio warning |
| Angles between the faces of an element | every FEA adapter | a mesh built from an FEA plan | between 5 and 170 degrees | Hexera's internal gate (design section 9) |
| Mesh zones short of the FEA plan's rules | every FEA adapter | a mesh built from an FEA plan | none | `engines/fea_checks.py`: elements per quarter-turn and through the wall at each hot spot |

The FEA rows are the strictest limit among the version-1 solvers (Abaqus/CalculiX, Nastran, Ansys;
`engines/fea_quality.LIMITS`, read by `engines/verdict_limits.fea_limits`), measured on the solver's
own element (`engines/fea_quality.py`). For an FEA mesh, VER-1 also refuses any inverted element
(a Jacobian not above zero anywhere in the curved element) and any degenerate one: a VALID MESH is the only thing that fails an FEA run (Areen, 21 Oct). The rows above, the quality note floor (SICN 0.1 for straight elements, 0.01 for curved ones) and the elements through each wall are notes on the delivery, never a failure.

The table is `engines/verdict_limits.PRODUCTION_LIMITS`. A limit names engines and flows, never a
file, a part or a model. Changing a number here means changing the table, this book and the
version in the same PR.

### VER-4 measured checks

| Way the mesh is the wrong problem | Measured by | Today |
|---|---|---|
| The wrong side of the body was meshed | `port_areas`, `region_contract` | gates |
| Part of the confirmed geometry is missing | `file_fidelity` | gate (snap-grid); CAD fidelity checks to come (#225) |
| The wrong scale | the believable-size row of VER-2 | a concern, not a failure |
| The flow setup contradicts what was confirmed | `domain_extent`, `port_areas` | gates |
| A boundary contradicts what was confirmed | `patch_contract`, `boundary_types`, `port_areas` | gates |
| No flow path from inlet to outlet | none yet | no measured check: a review claim is a note, and the shadow log lists it |
| The meshed surface differs from the CAD | `file_fidelity` | gate (snap-grid); CAD fidelity checks to come (#225) |
| Disconnected junk regions | `regions_split` (multi-region); the regions row of VER-2 | gate; a concern on one-region engines |
| A stated requirement is absent | the requirement rows of VER-2 | a concern |

The table is `contracts/decision_record.WRONG_PROBLEM_CHECKS`.

## PLAN-cfd: the flow mesh's sizing, on every flow engine

The sizing rules every flow engine (snappyHexMesh, cfMesh, VMTK, gmsh) follows for a CFD mesh,
from the part's own measured passages - never from a file or a part name
(`engines/resolution_policy.py`, `engines/gap_sizing.py`).

| ID | Status | Rule |
|---|---|---|
| PLAN-cfd-1 | in force | **Cells from the measured narrowest gap, locally.** Every passage the flow goes through gets at least 13 cells across its own width, refined only where it is narrow: snappyHexMesh and cfMesh refine boxes and distance bands round the narrow spots of the staged wall (`engines/gap_sizing`, the cost checked against the cell limit before meshing); VMTK sizes every edge from the local radius (2 / `edge_length_factor` across); gmsh sizes from the local radius field. The plan is written beside the case (`gap_sizing.json`) and cited in the why record. |
| PLAN-cfd-2 | in force | **Every opening, at least 8 cells across its own width.** A port narrower than 8 wall cells gets its own finer cells at the port only (snappyHexMesh: the port's refinement level; cfMesh: a local refinement on the port's surface). Under about 4 the octree seals a port over and it comes back with no faces. |
| PLAN-cfd-3 | in force | **The big-machine hook.** When the cells the sizing rule needs are over one job's cell limit, the plan records `route_big` (and the why record cites it). The big-machine runner is not built yet: until it is, the run is meshed as the limit holds, and the delivered note says so in plain numbers (the cells the full rule needs against the one-job limit). |

## LAD-cfd: the resolution ladder, and what a sound mesh becomes

A mesh is SOUND when every blocking gate passed and the trial solve converged: no fatal checkMesh
defect, no negative volume, every port with faces, the right region, the domain asked for. The
resolution floor (12 cells across the narrowest 5% of the wall), the share of skewed faces and the
one-job cell limit are our own bars, not validity: on a sound mesh they are notes, declared as
non-blocking gates (`GateSpec.blocking=False`). `engines/resolution_policy.py` holds the rules.

| ID | Status | Rule |
|---|---|---|
| LAD-cfd-1 | in force | **The ladder's next rung, never an identical re-run.** A sound mesh short of the resolution floor sends the next attempt up one rung on the same engine: the cells-across target at the narrow spots is multiplied by 1.5 (rung 1), then 2 (rung 2), or by what the measured shortfall needs ((floor + 1) / measured) when that is more, never past 3. snappyHexMesh also refines where the mesh measured short (its narrow retry); cfMesh detects and sizes its narrow-spot boxes that much finer; VMTK divides its edge factor by it, and its budget fit may coarsen the edge back only to the rung's own cells across (a rung that cannot fit one job is not started, and the sound mesh stands); gmsh sizes its passage field by it. The rung is written into the attempt's workspace (`resolution_ladder.json`), so the case the mesher gets is never the one it already ran (the rerun guard hashes it). No rung when there is no room left to refine: the mesh already has 90% of the cell limit, or the attempt's gap plan was already held by the limit (`limited`, `over_limit`, `accepted_fewer`) with its budget the whole one-job limit - the next plan has the same cells to share and shares them the same way (lab: 7 of 7 such rungs rebuilt the same mesh). A held plan under a smaller budget keeps its rung: the next plan may raise the budget. |
| LAD-cfd-2 | in force | **A switch does not spend the ladder.** When the run moved to another engine and that engine's first mesh is sound but short of the floor, it still gets one rung - inside the run's ceiling of 3 attempts, and only when a whole attempt (the builder's per-attempt limit) still fits in the run's time (the earlier of the builder's and the pipeline's deadlines); that last check is a guard clock and is recorded as one. |
| LAD-cfd-3 | in force | **Ladder spent, mesh sound: delivered with a note.** The note names the narrowest gap, where it is, its cells across and the floor ("the gap at (x, y, z) has 8 cells across; 12 is our floor"). The share of skewed faces is a production limit (VER-2), not a failure. And a later attempt that comes out broken never throws an earlier sound mesh away: the run delivers its best sound mesh instead of the failure (`node_deliver_sound`). The best is the one with the most cells across its narrowest gap (one that meets the floor beats any that does not), the newest on a tie; and when the last attempt is sound but a rung left it coarser than an earlier sound mesh, the earlier one is delivered too. The same holds between snappyHexMesh's own passes: a sound pass is kept aside while the next pass tries for a cleaner one, and put back if that pass comes out broken. |
| LAD-cfd-4 | in force | **Over one job's cell limit is a note, never a retry.** A sound mesh bigger than one standard job is delivered at the size its sizing rules asked for, with the note; a retry could only shrink it. The worker's trial solve is not run on it (its matrix outgrows the worker); checkMesh's fatal topology and a lean count of the mesh's connected regions (from its cell graph; one per passage meshed) stand in, and a disconnected pocket still blocks. |

## PLAN-fea: the FEA meshing plan

A structural solid (purpose Structural / FEA, 3D, no face named by the user) is meshed by the
deterministic FEA driver (`engines/fea_*.py`), not the builder loop. The plan is computed from the part's
measurements (FEA build step 3: walls, hot spots, bodies) and is the same for every engine; each
engine is one adapter that meshes it (gmsh first, `engines/gmsh/fea_adapter.py`). The plan is
written to `fea_plan.json` beside the mesh, with these sentences in plain words. No model is asked
anything: AI changes to the plan are off (Areen's decision 4, 20 Oct).

| ID | Status | Rule |
|---|---|---|
| PLAN-fea-1 | in force | **Quadratic tetrahedra, solid only.** Version 1 meshes the solid with 10-node tetrahedra; thin walls are meshed as solid too (shells are version 1.1). |
| PLAN-fea-2 | retired | **The detail level set the element count** (Quick / Standard / Fine). Retired 21 Oct: one rules-based plan, sized by PLAN-fea-14 and held under the element limit by PLAN-fea-8. |
| PLAN-fea-3 | in force | **At least 2 elements through every wall.** Every face's measured wall gets elements of at most half its thickness, held through the wall. A wall is measured by rays from the face along its inward normal to where they leave the material: a face of another body, a surface closer than 1/100,000 of the part (or twice the CAD face's own tolerance), a face met at a graze, and a face meeting the ray's own at an acute edge (a taper, whose thickness runs to nothing at that edge) are not the far side of a wall; a face's wall is the thinnest reading at least 3 of its rays agree on within 25%, and a face its rays do not agree on has no wall of its own (`cad/fea_features.wall_reach`). **A wall thin only in places is refined only there** (22 Oct): when a face's thinnest agreed reading is under half its median reading, the readings under half the median are thin spots, grouped where they lie within 2 sample spacings of each other. The rest of the face keeps the wall its other readings agree on. Each spot gets its own zone: the wall rule held off the face, inside a ball round its readings plus one sample spacing (`cad/fea_features.thin_spots`; a sliver between a counterbore and a bore no longer sets the whole bore's size). |
| PLAN-fea-4 | in force | **Hot spots: 10 elements per quarter-turn** of every hole, fillet and notch or groove, and 3 through the wall beside it, held out to one radius. |
| PLAN-fea-5 | in force | **Sharp inside corners are capped** at a quarter of the wall beside them, and refined no further: their stress never settles under refinement, so more elements there buy nothing. |
| PLAN-fea-6 | in force | **Gentle growth:** element sizes grow by at most 1.3 from one element to the next away from every refined zone. |
| PLAN-fea-7 | in force | **Every curved face keeps its shape:** at least 4 elements per quarter-turn (about 16 round a full circle; quadratic elements) of every round face that is not a hot spot - a cylinder or torus by its CAD radius, and any other curved face (a swept wire, a spring's coil, a free-form blend) by its own tightest bend, read from its principal curvatures sampled over the face (`cad/fea_measure.face_radius`). A bend gentler than a quarter of the part's diagonal is left to the base size. A round bar or wire so gets at least 3 elements across its diameter (a coil spring's wire came out a 4- to 6-sided ribbon, 22 Oct). |
| PLAN-fea-8 | in force | **Under the element limit, never a stop before meshing.** The element estimate is checked before meshing against the limit: 8,000,000 elements, or fewer when the mesher could not build that many inside its time guard (PLAN-fea-13). Over it, the finest zones are made coarser - a floor size doubled from the finest until the estimate fits - but never past what keeps elements valid: at least 1 element through every wall and 3 per quarter-turn of every round. What that leaves short of the rules is named (`resolution_short`) as a note on the delivery. When even those minimums are over the limit, the part is meshed at them anyway and told so (Areen, 21 Oct); the search stops as soon as a coarser floor changes no zone. An estimate the size field stopped counting short is never zero: the volume it had not resolved is counted at the size its zones ask for (fea0221 read 0 elements, and its time guard fell to the floor). |
| PLAN-fea-16 | in force | **The plan fits in the mesh job's memory.** The element limit is also what the mesher's peak memory fits in the mesh job's memory (32 GiB) with 1.25 times to spare, by the measured model peak = 450 MB + 5,100 bytes per element (lab VMs, 22 Oct: 4.65 to 5.04 KB an element at 2.7M to 2.9M elements) - about 5.3M elements (`engines/fea_driver.memory_elements`). Over it, the plan is coarsened like any other limit (PLAN-fea-8), never past what keeps elements valid. When even the least valid mesh is over it, the part is meshed anyway with a plain note; a pass the memory limit kills is not retried (LAD-fea-7). The memory, the model and the limit are in the why record. |
| PLAN-fea-14 | in force | **The base size is the coarsest the part allows** - its diagonal over 10 - and every rule refines below it where it asks to (walls, hot spots, corner caps, rounds, growth). That is the mesh the rules recommend; there is no detail level. |
| PLAN-fea-9 | retired | **One part only** (version 1): a file of several bodies stopped before meshing. Retired 23 Oct by PLAN-fea-16: an assembly is meshed. |
| PLAN-fea-16 | in force | **An assembly is meshed, its parts joined as the user said** (`cad/fea_assembly.py`, `engines/fea_assembly_deck.py`). Each solid body is a part, Part_k in OpenCASCADE's order, named from the CAD product (else by its number); several files uploaded together are joined into one such file first (`cad/ingest/assembly_files.py`), each part where its file puts it. Every face is Part_k_Face_j (the j-th face of part k). OpenCASCADE's general fuse finds where parts TOUCH (a face both share: a contact candidate) and where they PASS INTO each other (a solid both share: interference); the stage marks both and asks one question, how the parts are connected. **Bonded** (welded or glued, the default): the fuse itself is meshed, so the parts share their nodes where they touch (one conformal mesh), and a shared piece of interference is one piece of material counted with the lower-numbered part, said in a note; should the fuse fail, the parts are meshed apart and tied (*TIE / BCTABLE IGLUE / CONTA174 bonded), never a stop. **Contact** (they can slide or separate): each part is meshed on its own and every touching or overlapping pair is a named contact pair, the smaller surface the slave (*SURFACE + *CONTACT PAIR, hard and frictionless as a default to change; Nastran BCPROP + BCBODY + BCTABLE + BCPARA with the surfaces as BSURFS; Ansys TARGE170 / CONTA174). Each part is its own element set (and PSOLID / material number), sized by its own rules - its base a tenth of the part (PLAN-fea-14 per part) - and the element limit (PLAN-fea-8) covers the whole assembly. A part touching no other is meshed and said. |
| PLAN-fea-10 | in force | **A cusp is set aside, and named.** Where two CAD edges of one face leave a vertex within 5 degrees of each other (a bore or boss just touching a straight face; a nut's chamfer meeting its flats), the material runs to nothing and no element there can be well shaped at any size. The plan finds every cusp before meshing (`cad/fea_measure.cad_cusps`); the elements touching one get straight sides (always valid: a positive Jacobian), are set aside from the quality floor, and the mesh is delivered with a concern naming each place: the material runs to nothing here, so stress at this point is not meaningful; round it off in CAD for a clean mesh. Every other element must meet the floor (Areen's decision, 21 Oct). |
| PLAN-fea-12 | in force | **A micro detail is set aside, and named.** A CAD edge shorter, or a face narrower, than 1/20 of the finest element the plan asks for (a sprocket's teeth drawn with 0.001 mm flat strips, a socket's 0.004 mm edges) pins the elements on it into bad shapes at any size the level allows. The plan finds them before meshing (`cad/fea_measure.cad_micro`); the elements on them get straight sides (always valid), are set aside from the quality floor, and the delivery names each detail and says that stress right at it is not resolved - on a gear, the contact stress at the tooth tips. Every other element must meet the floor (Areen's decision, 21 Oct). |
| PLAN-fea-13 | in force | **The time guard grows with the planned work.** Each gmsh FEA pass may take 3 times its planned time - the plan's elements at the slow fill rate of the part's measured complexity class (CAD faces, hot spots, micro details) - never less than the engine's old fixed cap, never more than the mesh job's limit (14,400 s, less a margin) or what the attempt has left; the rate, the class and the guard are in the why record. The rates (elements a second) are measured on the lab VMs: simple 1,500, moderate 1,100, complex 1,000 - each under the slowest of its class (22 Oct, 12 parts of 68k to 1.3M elements). gmsh runs on one thread: at 8 threads three of five parts meshed twice came out different. No level is planned bigger than such a guard allows. A deterministic driver's attempt may use what the builder has left, not the model loop's per-turn slice (`engines/fea_driver.planned_work`, mirroring D3's PlannedWork). |
| PLAN-fea-15 | in force | **The staged solid is read as gmsh's own B-rep.** The staging writes the metre-scaled solid twice: as STEP, and as gmsh's own B-rep file beside it (`geometry.brep`). The FEA path measures and meshes the B-rep when it is there: gmsh's STEP writer and reader together lose faces on some parts (an ANSI bolt could not be read back; an ISO 4762 screw left a face with no surface mesh). A solid the measurement still cannot read is healed (OpenCASCADE's ShapeFix) and measured again, with a note, so the part still gets a plan. |
| PLAN-fea-11 | in force | **Curving never makes an element worse.** An element that curving onto the CAD leaves under the quality floor and worse than its straight shape keeps straight sides (its midside nodes at the middle of its edges; `engines/gmsh/fea_settle.py`). |

The numbers are `engines/fea_plan.py` (the limit and the coarsening are `engines/fea_driver.choose_plan`). The estimate is read from the plan's own size field over the
part's volume (`engines/fea_size_field.py`), and the plan records it, so the plan can be worked out
again from what it recorded.

## LAD-fea: the FEA driver's fixed lever ladder

One attempt runs at most 3 passes. After a pass, the first trigger that holds pulls its lever, each
lever at most once. The passes do not use up the run's attempts.

| ID | Status | Rule |
|---|---|---|
| LAD-fea-1 | in force | **An invalid mesh** (a degenerate or inverted element): the next pass grows sizes more gently (1.2) and optimises three times instead of once. Elements under the quality note floor are a note, not a trigger. |
| LAD-fea-2 | in force | **No mesh came back** (the fill failed): the next pass uses gmsh's other 3D algorithm (HXT instead of Delaunay). |
| LAD-fea-3 | in force | **A plan rule fell short on the mesh** (through a wall, or round a hot spot): the zones that fell short are made 20% finer - never on a plan coarsened to fit the limit, which is short there by choice and says so. |
| LAD-fea-4 | in force | **More elements than the limit** after meshing (the estimate ran low): the next pass makes the finest zones coarser, aimed at 85% of the limit, never past what keeps elements valid. |
| LAD-fea-6 | in force | **No identical retry.** The surfaces a failed pass went wrong on - the ones the fill named, or the ones an invalid element touches - are meshed the other way (gmsh's MeshAdapt surface algorithm instead of the default) on the next pass: together with LAD-fea-1 or LAD-fea-2, or on its own when those were pulled already. A surface the default algorithm leaves with no mesh is meshed again with MeshAdapt at once, in the same pass. When there is no surface left to change, the ladder stops rather than run the same pass twice. |
| LAD-fea-7 | in force | **Out of memory: no retry.** A pass the mesh job's memory limit killed pulls no lever (the other 3D algorithm and gentler growth need as much memory or more); the attempt ends saying the plan needs more memory than the mesh job has. |
| LAD-fea-5 | in force | **The best pass is delivered:** a valid mesh first, then every rule met, then within the limit, then the better worst element, then the earlier pass. |

The ladder is `engines/fea_ladder.py`.

## Reserved families

These ID families are reserved. Their rules are written by the PRs that build them, and no code
may cite them before then.

| Family | For | Written by |
|---|---|---|
| `PLAN-<engine>-n` | the numbers of each engine's computed plan (`PLAN-fea` above is the FEA path's) | D4 to D8 |
| `LAD-<engine>-n` | each engine's fixed ladder of approved levers | D4 to D8 |
| `TIME-n` | planned-work limits and the guard clocks | D3 |
