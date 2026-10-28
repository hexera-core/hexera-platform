# Shell meshing: design note

Status: draft 1, 28 Oct 2026. PR 1 is being built from this note (branch `feat/shell-midsurface`).
Rule numbers (SHELL-n, SHMESH-n) are placeholders until they get rulebook numbers at merge.

## 0. The goal

You upload a 3D part. We find its thin walls, pull out their mid-surfaces, and mesh them with shell elements.
It must be deterministic and general. Nothing is tuned to one part.

Why now:

- A marine-engineering prospect wants automated 2D (shell) pre-processing of marine structures for FEMAP and Nastran.
- Our thin-sheet assemblies blow solid plans up to 10 to 125M elements, and onto big machines. Examples: a cyclone filter, sheet-metal enclosures, CAx-IF S1, and 0.1 to 0.85 mm films. A shell mesh of the same parts is about 10 to 100 times smaller.

The shell path reuses the FEA path's parts:

- the CAD reading and the `Face_k` / `Part_k_Face_j` names;
- the wall rays (`cad/fea_features.wall_reach`);
- the joints (`cad/fea_joins.py`);
- the deck writers;
- the verdict by limits, the plan, the driver and the ladder.

## 1. What is a shell

### 1.1 Wall pairs (SHELL-1)

A wall pair is two faces of one body that face each other across an even wall.

- **Found by the rays we already fire for FEA sizing.** A ray fired inward from face A leaves the material through face B. B is A's partner when at least 3 rays, and at least 10% of A's rays, leave through it. The ray walk now also says which face each ray left by (`exit_label`); its readings are unchanged.
- **Confirmed on the exact CAD faces.** Points spread over the smaller face are projected onto the other face's surface. The pair is an even wall when two things hold:
  - the distances agree within 2%;
  - the normals are opposite within 2 degrees.
- **The surface type does not matter.** Two planes, two coaxial cylinders or cones, two concentric spheres and two offset free-form faces all pass the same test. There is no list of shapes.
- **A face can have several partners.** A deck plate's bottom face pairs with each top face between the stiffeners.
- **Variable walls are named, not shelled yet** (tapers, wedges, cast walls). They stay solid until PR 3 (section 2.5).

### 1.2 Sheets and the shell-or-solid rule (SHELL-2)

Pairs are grouped into **sheets**. A sheet is the pairs of one body and one thickness (within 2%) whose faces are the same or meet at a CAD edge. A bent sheet's flanges and bends form one sheet. So do a deck plate's pieces between stiffeners.

A sheet is a shell when both of these hold:

- **t ≤ L / 10.** L is the sheet's size: the square root of its mid-surface area. Mindlin shells (the S4R and CQUAD4 kind) hold to about t / L = 1/10, where L is a typical structural size.
- **t ≤ width / 4,** so a shell element there can be a few walls wide.

Why per sheet and not per pair: a 2 mm sheet's 3 mm bend is only 5.7 mm wide. Judged alone, it reads as thick. Judged with its flanges, it is clearly a shell. The same goes for a flat-bar stiffener web, 8 mm thick and 50 mm deep.

### 1.3 Every face gets a role (SHELL-2)

| Role | What it is | Rule |
|---|---|---|
| paired | one side of a shell wall | in a shell sheet's pair |
| rim | a plate's edge face, or a hole's bore through the plate | strip width (2 × area / perimeter) ≤ 1.5 × the wall it touches |
| blend | a fillet or chamfer where walls meet | round with radius ≤ 2 × wall, or flat and ≤ 2 × wall wide |
| thick | a side only of walls too thick to be a shell | in pairs, none of them shell |
| solid | anything else | |

Rims become the mid-surface's free edges. Blends are bridged when the mid-faces are extended to meet (section 2.3).

### 1.4 Body verdicts

- **shell:** every face is paired, rim or blend.
- **mixed:** shell sheets plus thick or solid faces. The thick and solid faces are grouped into named solid regions, so the stage can say "this boss stays solid".
- **solid:** no shell sheet.

The verdict is a recommendation. The user picks (section 7).

## 2. Mid-surface from the B-rep

### 2.1 The approaches we looked at

| Approach | Good | Bad | Verdict |
|---|---|---|---|
| Offset the whole solid inward by t/2 (OCC `BRepOffsetAPI_MakeThickSolid` / `MakeOffsetShape` on the solid) | one call | fails at thickness changes and self-intersections; gives a solid, not a mid-surface | no |
| OCC medial axis | exact in theory | OCC has only a 2D one (`BRepMAT2d`); no 3D medial axis | no |
| **Face pairs: mid-face per pair, extend to meet, trim, sew** | exact B-rep output; keeps each mid-face tied to its CAD faces (names, groups); what Femap, NX, ANSA and HyperMesh do | needs good pairs; only even walls; junction logic to get right | **chosen** |
| Voxel or distance-field skeleton | works on anything (cast, organic) | faceted and approximate; noisy branches at corners; loses the CAD face names; voxels must be about t/4, so a 20 m hull with 10 mm plate needs about 10^12 voxels | fallback only, later |
| Triangle midsurface (pair triangles by rays, average) | general; reuses our rays | faceted; needs remeshing; weaker names | fallback for variable walls (PR 3) |

**Chosen:** face pairs on the exact B-rep, with the ray pairing as the evidence layer. Regions this cannot do stay solid (mixed part), are named on the stage, and are never dropped silently.

### 2.2 The mid-face of a pair

1. Offset each face of the pair halfway into the material. This is OCC's simple offset (`PerformBySimple`, −t/2 along the face's outward normal).
2. Take the overlap of the two offsets (a Boolean common, one thread). The mid-face covers only where the two faces really face each other.

This is exact for every even wall. The prototype got exact results:

- plate: 20,000.0 mm² for 20,000;
- tube: 30,159.3 mm² for 2π · 24 · 200;
- bend: 314.16 mm² for a quarter-cylinder of radius 4, 50 mm deep.

### 2.3 Junctions: extend to meet (SHELL-3)

At a junction the mid-faces stop short of each other.

- At a T, the stiffener's mid-face stops at the plate's top face, half a plate thickness short of the plate's mid-plane.
- At an L, each leg stops at the other leg's inside face.

The rule: a straight edge of a planar mid-face is carried on to the mid-plane it runs into when all of these hold:

- all three sample points along the edge, moved outward in the face's own plane, meet the same target plane;
- they meet it within the reach: half the target's wall plus 2 × the thicker wall, so a fillet up to 2 walls at the junction is bridged;
- the whole way there is inside the part's material;
- the target is a mid-face of the same body, not within 5 degrees of parallel.

The new edge is the line where the two mid-planes meet. Corners are re-cut as the meeting points of the new lines. An edge that already touches another mid-face (a bend's flange and its bend cylinder) is left alone. An edge that cannot be redrawn keeps its own shape and shows up as an open edge.

Each junction type:

- **T (stiffener on plate):** the stiffener goes down to the plate's mid-plane. The plate's two pieces each go across to the stiffener's mid-plane. Three faces share one line.
- **L corner:** each leg goes to the other's mid-plane, and they meet on one line.
- **Cross (two webs through a plate):** the same as two Ts.
- **Plates of different thickness meeting in-plane (a butt step):** the mid-planes are parallel and offset, so nothing above joins them. PR 3 snaps the thinner plate onto the thicker plate's mid-plane and records the offset per element (ZOFFS in Nastran, OFFSET in Abaqus and CalculiX). Stiffness and mass stay right, and the user sees one flat shell. Until then the step shows as an open edge.
- **Cut-outs and holes:** the overlap keeps them. Their edges are real free edges (the outside lies just past them). Holes smaller than about one element are named for defeaturing later. They are never filled silently.
- **Fillets and chamfers:** up to 2 walls, absorbed (blend role). Larger rounds with an even wall are their own sheet (a sheet-metal bend). Others are solid and named.
- **Tapers:** named as variable walls and left solid until PR 3. PR 3's approach:
  - sample the midpoints between the two faces;
  - fit a B-spline mid-surface (`GeomAPI_PointsToBSplineSurface`);
  - store the thickness per node (CQUAD4 T1 to T4, or `*NODAL THICKNESS`).
- **Cylinder ends on plates (tube frames, nozzles):** PR 3. The same rule, with the cylinder carried along its axis to the target and cut by it.

### 2.4 Sewing (SHELL-4)

The extended mid-faces of each body are run through OCC's general fuse (`BOPAlgo_Builder`, one thread):

- the fuzzy tolerance is 1/1000 of the thinnest wall;
- edges that meet become one edge;
- a face that another face's edge lies on is split along it.

The result is a conformal shell: shared nodes along every junction once meshed.

### 2.5 Assemblies and mixed parts (PR 3)

**Bonded plates in different bodies:**
- The same extension rule, aimed across bodies that `cad/fea_joins` says touch. Touching plates share nodes.
- Where a gap exists (PLAN-fea-18's small-gap rule), a tie is used, never shared nodes:
  - Nastran: RBE3 or BSURF/BCONTACT glue;
  - Abaqus and CalculiX: `*TIE`;
  - Ansys: bonded contact.

**Mixed part (shell meets solid):**
- The shell's edge on the solid region is a "solid link" in the record, with its place.
- The connection, per solver:
  - Abaqus: `*SHELL TO SOLID COUPLING`;
  - Nastran: RBE3 from the shell edge nodes to the solid face nodes;
  - Ansys: an MPC184 or contact-based shell-solid assembly;
  - CalculiX has no shell-to-solid keyword, so it gets the overlap method: the shell edge runs one wall deep into the solid and shares its nodes there.
- Proved on a textbook mixed case: a plate with a solid boss, against the all-solid mesh.

## 3. Meshing (PR 2)

**Engine.** gmsh on the sewn mid-surface: Frontal-Delaunay for quads (algorithm 8) plus Blossom recombination, and quad-dominant output. If gmsh leaves too many triangles on a family of parts, Salome SMESH (quadrangle mapping, LGPL) is the second engine. The system recommends one, the user picks, and the plan is engine-neutral as on the FEA path.

**Element size rules** (SHMESH-1..7, placeholders). Each is general and measured:

1. **Base:** the coarsest the sheet allows. At most 1/10 of the sheet's size L, and at least 4 elements across every mid-face's narrowest width.
2. **Floor:** no element smaller than half the wall. Smaller than that, a shell is the wrong element. Where a rule asks for less, the part is meshed at the floor with a plain note.
3. **Holes and cut-outs:** 6 elements per quarter-turn (24 around), as PLAN-fea-4. Plus one ring of quads round the hole (a washer) and at least 2 element rows between a hole and a free edge.
4. **Curvature:** at most 15 degrees per element on curved mid-faces (6 per quarter-turn), and at least 3 elements across a bend.
5. **Small features:** at least 3 elements across any flange, strip or web.
6. **Growth:** 1.3 at most, as PLAN-fea-6.
7. **Junction lines:** always meshed with shared nodes. Both sides use the same seeds.

**Quality** (SHMESH-8, verdict by limits as VER-1..4).

- **Blocker:** a valid mesh only. No inverted or zero-area element, a positive Jacobian at every corner, and consistent normals in each sheet.
- **Notes:** everything else, judged against Nastran's GEOMCHECK defaults and the Femap or industry targets below.

| Measure | Nastran GEOMCHECK default (CQUAD4 / CTRIA3) | Femap / industry target |
|---|---|---|
| aspect ratio | IAR 100 (warn) | ≤ 5 |
| warp | Q4_WARP 0.05 | ≤ 5 degrees (≤ 10 notes) |
| skew | Q4_SKEW 30° min, T3_SKEW 10° min | ≥ 45° (quad) |
| taper | Q4_TAPER 0.5 | ≤ 0.5 |
| interior angle | Q4_IAMIN 30°, Q4_IAMAX 150°, T3_IAMAX 160° | quad 45 to 135°, tria 20 to 120° |
| Jacobian | (must be positive) | ≥ 0.6 |
| triangle share | | ≤ 5% of elements |

The GEOMCHECK figures are as we read MSC's Quick Reference Guide. PR 2 checks them against the guide and cites the page, as `fea_solvers.py` does.

**Normals.** Every sheet's normals point one way. They point from face B toward face A of each pair, and outward on closed sections. Offsets and pressures in the user's solver then mean the same thing everywhere.

## 4. Properties and groups (PR 2)

- **One PSHELL per sheet thickness, per part.** Part_3 at 6 mm becomes "Part_3_t6mm". MAT1 comes from the part's material when given. Otherwise it is a placeholder steel with a note, so the deck runs.
- **Groups:** one element group per original CAD face pair, named after the CAD faces (`Part_3_Face_12+Face_15`), and one per part.
- **Joints are marked:**
  - shared-node lines at sewn junctions get a node group per junction;
  - ties exist only where there is a gap, as PLAN-fea-18;
  - solid links (mixed parts) get a node group per link.

## 5. Export (PR 2; Ansys in PR 2 too)

**Nastran bulk data (.bdf), first-class for FEMAP.**
- Cards: GRID, CQUAD4 and CTRIA3 (T1 to T4 corner thicknesses when the wall varies), PSHELL (MID1 = MID2 = MID3, 12I/T³ = 1, TS/T = 5/6), MAT1.
- Names: Femap's own comment form for property and material names (`$ Femap Property 1 : Part_3_t6mm`). Our ingest already reads that form back (`cad/ingest/readers.py`).
- **Groups:** Nastran bulk data has no group card. We write each group as a named comment block of element ranges, and read it back in our tests.
- **The FEMAP-native route is a neutral file (.neu)**, whose group block is public. We offer it as soon as the prospect confirms the import in their FEMAP. Until then the BDF says "not yet tested in the real solver", as the solid writers do.

**Abaqus and CalculiX (.inp).**
- Elements: `*ELEMENT, TYPE=S4R` and `S3`.
- Sections: `*SHELL SECTION, ELSET=…, MATERIAL=…` with the thickness, or `*NODAL THICKNESS`.
- Sets: an ELSET per group and an NSET per junction.
- CalculiX solves for real. It expands shells into solid elements, and we check it against the textbook cases.

**Ansys MAPDL (.cdb).**
- Elements: SHELL181 (ET; SECTYPE/SECDATA per thickness), CMBLOCK components per group.
- Checked with readers and round trips only (no licence), labelled as such.

Every writer is engine-neutral. They read one internal shell mesh (nodes, quads and triangles, a thickness per element or node, groups). Each is read back in a round trip, as the solid writers are.

## 6. Checks

**Per mid-face and per body, in PR 1:**
- **Fidelity:** every point read on a mid-face lies half the wall from both of its faces, within 2% of the wall.
- **Volume:** a shell body's mid-faces times their walls hold its volume within −15% to +10%. Rims, blends and junction corners are counted once or not at all.
- **Every face accounted for:** each face of a shell body is paired, rim or blend. Nothing is lost.
- **Connectivity:** every free edge of the sewn surface is either a real boundary (outside just past it) or, on a mixed part, a solid link. An OPEN edge is material past a free edge with no solid region there. It is a junction we did not reach, and it is listed with its place.

**On the mesh, from PR 2:**
- the same connectivity count on elements: free element edges must lie on free mid-surface edges;
- the quality figures above;
- every original face accounted for in a group;
- the deck read back.

**Textbook accuracy in CalculiX (PR 2),** each with a closed form, and each also run on our solid FEA mesh as a cross-check:

1. Cantilever plate, tip load. Tip deflection PL³/3EI, root stress.
2. Plate with a hole in tension. Kt from Peterson (finite width, Howland).
3. Pressurised thin cylinder with closed ends. Hoop stress pr/t, axial stress pr/2t, radial growth.
4. Simply supported plate:
   - modal: f_mn from the Kirchhoff plate formula;
   - buckling: σcr = 4π²E/(12(1−ν²)) · (t/b)²;
   - then the stiffened panel modal case against our fine solid mesh.

## 7. Intake and stage

**Intake.** Today's FEA intake asks one thing, the analysis type. Shell adds no new question. It adds one choice on the FEA stage: **"Mesh as: Auto (recommended) / Shell / Solid"**.

- **Auto** shells the shell bodies and keeps solid bodies solid. Mixed parts get shell where thin and solid where thick (once PR 3 is in; until then, mixed parts are recommended solid with the reason).
- The recommendation always shows its measured reason, for example: "Shell recommended: walls 2 to 6 mm on a part 1.2 m long."

**Stage:**
- The extracted mid-surfaces are drawn over the part, coloured by thickness. We use a muted sequential map with a legend in mm.
- A list of what could not be shelled, in plain words: "Boss near the centre stays solid: it is 30 mm thick." "Two plates do not meet at 1 place: shown in red."
- Open edges show as red marks at their places.

The system recommends and the user picks. Nothing here blocks meshing. A part we cannot shell is meshed solid, with the reason.

## 8. Plan: five PRs, each to the deep-test bar

The deep-test bar for every PR:
- end-to-end on the shell corpus, not plan-only;
- the FEA regression set byte-identical, or every difference explained;
- a CalculiX accuracy check for anything that changes a delivered mesh;
- real-path testing (dev-areen) for anything a user sees.

| PR | Scope | Proof |
|---|---|---|
| 1 | Wall pairs, sheets, shell or solid, mid-faces, T and L extension, sewing, the checks, `shell.json` + `midsurface.brep`. No user-facing change. | Unit tests on known parts. Corpus sweep of extraction with the judge (fidelity, connectivity, accounting, against each file's truth notes). FEA regression unchanged (the ray change adds an output only). |
| 2 | Shell mesh (gmsh quads), the SHMESH rules, quality, normals, PSHELL and groups, Nastran, CalculiX and Ansys writers, textbook cases. | CalculiX textbook cases. Corpus meshed end to end. Deck validity (our read-back plus pyNastran in the lab). A CalculiX free-free modal solve on every corpus part, compared with our solid mesh's modes. |
| 3 | Thickness steps (offsets), tapers and variable walls, cylinder-to-plate and tube frames, bonded assemblies across bodies, ties at gaps, mixed shell-solid links per solver. | The same, plus the mixed textbook case. |
| 4 | Stage and intake: the Auto/Shell/Solid choice, mid-surface overlay, not-shelled list, downloads (BDF, INP, CDB, NEU). | Clicked through in the browser, and run once on dev-areen end to end. |
| 5 | Corpus proof: the full training corpus and, at the milestone, the sealed holdout. | Pass rates with 95% ranges. |

## 9. Test corpus and judge (Areen, 28 Oct)

**Corpus.** Real engineering models only, from public sources with their licences recorded, never NDA files. The categories:

- ship hull blocks and stiffened panels;
- offshore structures;
- sheet-metal enclosures and brackets;
- automotive body-in-white style panels;
- ducts and tanks;
- cyclone and hopper plate structures;
- aircraft skin-stringer panels;
- tube frames.

Also:
- As many CAD exporters as we can find, mostly STEP.
- Plus our own sheet-metal and thin parts from the FEA hunt and the assembly corpus.
- Lab: `~/shell_lab` (corpus, manifest, tools, runs).

**Truth notes for every file:** the expected plates and thicknesses, the junction types, and the holes. The judge holds each run against them.

**The judge, end to end on each file:**
- mid-surface fidelity;
- connectivity (no open edges);
- every face accounted for;
- thicknesses found against the truth notes;
- element quality (PR 2);
- Nastran deck validity (PR 2);
- a CalculiX solve on each (PR 2). A free-free modal solve needs no supports, so it runs on any part. Its modes are compared with the solid mesh's.

Pass rates are reported with 95% ranges (Wilson).

**Sealed holdout.** About 30 untouched files, kept apart from the start. Aggregate-only, never fixed on, run only at milestones.

## 10. Open questions for Areen

1. FEMAP: can the prospect send one sample model, or confirm that our .neu groups import? Until then the BDF and NEU say "not yet tested in the real solver".
2. Is placeholder steel (E 210 GPa, ν 0.3, ρ 7850) right when no material is given? Or should the deck have no material, so the user must set one?
3. Mixed parts in Auto before PR 3: recommend solid with the reason (this note's choice), or shell the thin bodies only?
