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
- **Tapered walls (SHELL-9, 29 Oct).** A pair that is not even, but whose faces turn from each other by at most 10 degrees where they face each other (the points of A whose foot lies on B itself, at least 6 of them), is a tapered wall: a draft, a wedge, a printed funnel's drafted side, a twisted strap. Whether it is a shell is judged at its thickest point. Its mid-surface is fitted (section 2.2). Steeper than 10 degrees, the two faces are two sides of a solid region and stay so.

### 1.2 Sheets and the shell-or-solid rule (SHELL-2)

Pairs are grouped into **sheets**. A sheet is the pairs of one body and one thickness (within 2%) whose faces are the same or meet at a CAD edge. A bent sheet's flanges and bends form one sheet. So do a deck plate's pieces between stiffeners.

A sheet is a shell when both of these hold:

- **t ≤ L / 10.** L is the sheet's size: the square root of its mid-surface area. Mindlin shells (the S4R and CQUAD4 kind) hold to about t / L = 1/10, where L is a typical structural size.
- **t ≤ width / 4,** so a shell element there can be a few walls wide.

Why per sheet and not per pair: a 2 mm sheet's 3 mm bend is only 5.7 mm wide. Judged alone, it reads as thick. Judged with its flanges, it is clearly a shell. The same goes for a flat-bar stiffener web, 8 mm thick and 50 mm deep.

**The width of a split wall (SHELL-2b, 30 Oct, 12th helper).** A sheet's width is read on its CAD faces as one region: an edge two of its faces share is inside it, not its outline. Its mid-faces share no edge, so their outlines added up: a printed cone split lengthwise into strips read as wide as one strip and stayed solid. The same width decides whether a tapered pair is fitted at all (a strip of a wide wall is fitted). Failure map: the split printed cones (cone_taper_split, 2 cases) solid to shell; training sh0012's 5.4 mm duct body mixed to shell.

**Brackets and frames (30 Oct, 12th helper).** The sealed set's "no shell found" is mostly brackets and frames. 38 generated brackets and frames (angle, gusseted, cast corner, strap, U, pocketed, flat-bar, angle and tube frames, T-slot extrusion; walls 2 to 8 mm) were judged against the plate rule (shell when span over about 10 walls, solid under 6, either between): 38 of 38 right on the code as it was. The call is not too strict; such brackets are thick. What was missing is the reason (SHELL-2c, section 1.4).

### 1.3 Every face gets a role (SHELL-2)

| Role | What it is | Rule |
|---|---|---|
| paired | one side of a shell wall | in a shell sheet's pair |
| rim | a plate's edge face, or a hole's bore through the plate | strip width (2 × area / perimeter) ≤ 1.5 × the wall it touches |
| blend | a fillet or chamfer where walls meet | round with radius ≤ 2 × wall and a strip (never a cylinder or cone that closes round its axis: that is a boss, a pin or a bore), or flat and ≤ 2 × wall wide with no parallel face across the metal |
| thick | a side only of walls too thick to be a shell | in pairs, none of them shell |
| solid | anything else | |

Rims become the mid-surface's free edges. Blends are bridged when the mid-faces are extended to meet (section 2.3).

### 1.4 Body verdicts

- **shell:** every face is paired, rim or blend.
- **mixed:** shell sheets plus thick or solid faces. The thick and solid faces are grouped into named solid regions, so the stage can say "this boss stays solid".
- **solid:** no shell sheet.

The verdict is a recommendation. The user picks (section 7).

**A solid body says why, in numbers (SHELL-2c, 30 Oct).** Its most plate-like wall (the pair closest to passing both rules) is named with its thickness, its span and width, each in walls: "no wall is thin against its size: its most plate-like wall is 8 mm thick and spans about 42 mm, only 5.3 times its thickness; a shell needs at least 10 times". A wall too narrow says so ("only 2.8 mm wide, 1.5 times its thickness; a shell needs at least 4 times (narrower, it is a bar or a beam)"). The record keeps it as `closest_wall`. The stage shows it (SHSTAGE-3b), and Auto meshes the part solid.

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

### 2.2.1 The fitted mid-face (SHELL-9, 29 Oct)

For a tapered wall, and for an even wall whose two half-offsets do not overlap (two free-form faces whose offsets are not quite the same surface: a twisted strap):

1. Over a 17 x 17 grid of face A's parameters, each point of A is paired with its foot on B's surface. Their midpoint is moved along the line between them until it lies as far from A's surface as from B's (3 steps).
2. A cubic B-spline surface interpolates the grid of midpoints, its parameters A's own scaled to 0..1, so A's outline maps onto it exactly (an affine map of B-spline poles). An approximation of higher degree waved between the points: a twisted strap's mid-surface came out 14 times its area.
3. When B does not lie across the whole of A, B's outline is projected onto A's surface too, and the mid-face is where the two outlines overlap. The overlap is worked out flat, in the plane of the parameters, and laid back onto the surface: a Boolean on a fitted surface closed round a cylinder's seam found nothing.
4. A wall closed round its axis (a drafted tube) has its two seam sides sewn into one edge.
5. The thickness at a point is its distance to A plus its distance to B; the record keeps the thinnest and thickest (`thickness_range_mm`), and PR 2 writes it per node (CQUAD4 T1 to T4, `*NODAL THICKNESS`). **SHELL-9b (30 Oct):** a fitted mid-face whose wall varies by more than 1% carries its wall read at points over it (`wall_samples_mm`: a 9 x 9 grid inside it and 9 points along each edge, distance to A plus distance to B). Meshed at the pair's one (thickest) wall, a 2-degree wedge came out 41 to 68% too stiff and a printed cone 49 to 147%, with every check passing.

A fitted mid-face is kept only when:
- every point read on it lies as far from A as from B, within 2% of the local thickness (|dA - dB| <= 0.02 (dA + dB));
- it is itself at least four walls wide (as a shell sheet is). A narrower one is a local feature of the wall beside it, which that wall's own mid-face runs across when it is joined (a flat dimple 1.4 walls wide in a printed cylinder's side).

Otherwise the pair stays a variable wall, with the reason.

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
- **Laps (SHELL-8):** two shell plates laid one on the other and fused (a lap joint, a doubler, a bracket flange on a plate). The stacked region is a pair whose two faces are each a side of a shell pair and whose wall is the two walls added up (within 2%). It is a few walls wide, so it fails the width rule on its own; it is shelled with the plates it is made of, and SHELL-7 snaps both plates onto its mid-plane with their offsets: one flat shell.
- **Plates of different thickness meeting in-plane (a butt step):** the mid-planes are parallel and offset, so nothing above joins them. PR 3 snaps the thinner plate onto the thicker plate's mid-plane and records the offset per element (ZOFFS in Nastran, OFFSET in Abaqus and CalculiX). Stiffness and mass stay right, and the user sees one flat shell. Until then the step shows as an open edge.
- **Cut-outs and holes:** the overlap keeps them. Their edges are real free edges (the outside lies just past them). Holes smaller than about one element are named for defeaturing later. They are never filled silently.
- **Fillets and chamfers:** up to 2 walls, absorbed (blend role). Larger rounds with an even wall are their own sheet (a sheet-metal bend). Others are solid and named.
- **Tapers:** fitted mid-faces (section 2.2.1), joined like any other mid-face.
- **Bends whose outside is not concentric (SHELL-10, 29 Oct).** A CAD model that rounds both corners of a sheet alike (inside and outside radius the same) gives a bend whose wall is t at the flanges and about t x 1.41 mid-bend: a tapered wall. Its fitted mid-face ends where its two faces stop facing each other, short of the flanges' mid-planes, which run on almost tangent to it, so neither the extension nor the general fuse meets them (a fan housing's 1 mm duct, sh0021 Part_33: 16 open edges). The rule: a free edge of a fitted mid-face that runs alongside a free edge of another mid-face of the body - every one of 5 points along each edge within one wall (the thicker) of the other edge, further apart than the sewing tolerance, the strip between them in the material - is joined to it by the ruled strip between the two edges, which is part of the fitted mid-face. Part_33: 0 open edges, volume 1.002.
- **Cylinder ends on plates (tube frames, nozzles):** PR 3. The same rule, with the cylinder carried along its axis to the target and cut by it.

### 2.4 Sewing (SHELL-4)

The extended mid-faces of each body are run through OCC's general fuse (`BOPAlgo_Builder`, one thread):

- the fuzzy tolerance is 1/1000 of the thinnest wall;
- edges that meet become one edge;
- a face that another face's edge lies on is split along it.

The result is a conformal shell: shared nodes along every junction once meshed.

**Speed (29 Oct).** No corpus file may take more than 10 minutes. The same answers, read faster:
- An offset mid-face (a drafted or free-form wall) is re-made as a B-spline before the join, only when every point read stays within 1/10,000 of the wall. One fuse of an offset against a plane ran past 40 minutes; the B-spline takes 0.01 s. The checks still read the offset.
- Inside-or-outside: a point well clear of the skin is told by the body's own closed triangulation (three rays that must agree). Near the skin, the exact classifier answers.
- Faces that cannot touch are fused apart. The part's own faces go into the general join as one argument, so they are not intersected with each other.
- Readers are built once per face (projectors, surface analysers), and each point's inside state is kept.
- The taper test stops at the first point turned past 10 degrees; a pair whose two faces are both narrower than four walls (1.5 margin) is not fitted (29 Oct: a fan housing 1,055 to 832 s alone). Each general-join variant past the first (mid-faces in beside their patches; no re-making) runs only when an open edge is left within a grown patch's reach of a wall it treats differently (free-form; offset), measured to the wall itself: a fan housing's free-form wall spans its body, so its box held every open edge (sh0020 / sh0021 833 / 851 to 528 / 536 s at 8 at once).
- **A free-form mid-face's grown patch goes into the general join alone** (29 Oct). The patch is the mid-face's parameter box widened, so it lies over the mid-face itself, on a surface the fuse does not merge with the mid-face's own. With both in, the wall was there twice: a twisted strap held 29% more metal than it has, and its join left 4 open edges. Its pieces inside the mid-face are the mid-face's own, read by geometry. The general join is also run with the mid-face in beside its patch, and the one leaving fewer open edges is kept (on a tie, the patch alone): on a cyclone of re-made offset walls (sh0011) the patch alone left 29 open edges where both left 5.
- **A deadline on each general join (SHELL-11, 29 Oct).** OpenCASCADE gives its general fuse no time budget, and on a cone meeting a ring (fresh-set fr018) one fuse ran past 40 minutes. The general join now runs in a forked child process, stopped at 5 minutes (no join of the training corpus comes near it); the joins already made stand, and the record says so. The child hands back the fused surface as a binary BRep file (every number exact) and each mid-face's faces by index, so the record is the same as a join made in-process (a unit test holds this). Where the system cannot fork, the join runs in-process, unbounded.
- **No join with a widened tolerance (SHELL-12, 30 Oct).** The general fuse can close a gap by widening a vertex's tolerance until it covers it: on a funnel's cone (sh0011) one vertex came back with 39 mm on 5 mm walls. Its edges then stand that far off their faces, gmsh cannot close the face's loop, and the mesh failed. A general join that leaves any tolerance over half the body's thinnest wall is not used; the straight join stands, and the record says so. Corpus: only the four files whose sewn surface carried such a tolerance can change. sh0011 now meshes (its open edges, 5 before, are 29: the 5 were closed only by the widened vertices); sh0048's open edges go 206 to 272 for the same reason; sh0015 and fr018 are byte-identical (their wide tolerances come from the straight join, which meshes).

- **An open edge bridged to the junction beside it (SHELL-13, 30 Oct).** After the join, an edge still open that runs alongside an edge of another mid-face of the body (a junction line, or another free edge) is joined to it by the ruled strip between the two: every one of 5 points of each edge within SHELL-3's reach of the other (half the other wall plus 2 walls), further apart than the sewing tolerance, the strip's middle in the material; the nearest such edge. The surface is kept only when it leaves fewer open edges. An extruded profile's curved (fitted) wall stopped 0.7 mm short of the corner of its two flat walls (fresh fr013: 1 open edge to 0); a funnel's cone (sh0011) went from 29 open edges to 2.
- **Offsets carry their direction (SHELL-7, 30 Oct).** A snapped plate's record gives the direction its true mid-plane lies in (`offset_normal`, with `offset_mm`), so the mesh writes it per element (Nastran ZOFFS, Abaqus and CalculiX OFFSET, Ansys SECOFFSET).

- **A grown face is one face where it can be (SHELL-14, 30 Oct, 12th helper).** A mid-face grown only along directions that do not close (a tube past its ends, a cone past its rims, a cap past its edge) is grown as one face on its own surface, not its own face plus a band each side. The bands' edges stayed in the joined surface half a wall from the junction, as slivers whose two long edges were nearly one: gmsh's 1D mesh "not a closed loop" (an equal tee, a 45 degree lateral, a mitred bend), and open edges where a curved cap meets a crease (a dome on a flange ring, a dished end, a nozzle on a torispherical head: 1 open edge each, now 0). Faces that close in a grown direction still grow as tiles (a torus).
- **Slivers dropped (SHELL-16, 30 Oct).** After the join, a sewn face under a tenth of its wall wide is dropped and its neighbours sewn across it at 1.5 times its width (never past SHELL-12). Kept only when no edge opens and every face stays valid (a face a join handed back twice is counted once).
- **No join holding more metal than the part (SHELL-17, 30 Oct).** On a shell body, a general join whose sewn faces times their walls hold over 1.10 times the body's volume, and 10% of it more than the straight join, is not used: it kept grown pieces that are no wall. A ring frame on a curved skin held 5.8 times the metal and a pipe saddle 1.27 (gmsh then failed their loops); with the straight join and SHELL-13 both mesh, 0 open edges, 1.01 and 1.00. A mixed body is left out: its mid-faces run on into its thick regions (a PVC tee's sockets, fresh fr017, 1.2 with every junction met).

### 2.4.1 IGES and other loose surfaces (SHELL-15, 30 Oct, 12th helper)

An IGES file is trimmed surfaces with no topology, read as loose faces in no solid; none was shelled (fresh-set IGES copies: 0 of 20). Now:

1. **Analytic again (SHELL-15c).** A face whose surface is a plane, cylinder, cone, sphere or torus written as a surface of revolution or a B-spline is re-made on that surface (OCC's `GeomConvert_SurfToAnaSurf`, within the first sewing tolerance), its own edges kept. A bend written as a surface of revolution had its mid-face 1.1 mm off the middle of a 2 mm wall: its offset is no exact offset.
2. **Sewn at a measured tolerance (SHELL-15).** First at a millionth of the part. Then each free edge's gap to the nearest other free edge, read as the farthest of 5 points along it (two edges that meet at a corner and part along their length are that far apart, not 0), times 1.5, never over a tenth of the narrowest face: a tolerance that reached across a rim would sew a wall's two sides together. Closed shells become solids, oriented.
3. **Touching bodies (SHELL-15b).** Where sewing makes no valid solid or leaves faces in no body (a frame's tubes end to end, a duct and its insulation), the faces are read as the volumes they bound (OCC's volume maker): each volume whose largest faces' own normals point out of it is material.

Results (OpenCASCADE IGES copies of the fresh set's 20 STEPs): the judge passed 0 of 20 before, 15 of 20 now (the STEPs: 19); 14 of 20 give the same verdicts, walls and open edges as their STEP. Still apart: bodies that touch (fr011's four tubes, one volume lost; fr014; fr015's duct and insulation read as four solid bodies) and the printed cones fr018 and fr019 (no body closes). On the training set's IGES copies, single bodies mostly match their STEP and touching multi-body assemblies mostly do not (state line).

More IGES rules (30 Oct, 13th helper):

4. **Each body owns its faces (SHELL-15d).** The volume maker hands two touching bodies ONE face where they meet. A face is read as one body's only, so the other lost a side of its walls: a bent duct's 52 faces read as 35, both bodies "solid" with 250 mm walls. Each volume is now copied, so each owns its faces, as in the STEP.
5. **A shared face does not vote (SHELL-15e).** A face made from two of the file's faces (a duct's outside and its insulation's inside) is turned one way in one body and the other way in the other. It no longer votes on "material or cavity"; the next largest faces do.
6. **A seam written twice (SHELL-15f).** A closed face (a bend's cylinder) whose seam the file wrote as two edges of its one wire: sewing never joins a face to itself, both stayed free and the body never closed (a sheet-metal part with 129 faces: 2 free edges, gap 0). Only such a face is split in two across its closed direction; the seam is then sewn.
7. **Re-made the right way round (SHELL-15g).** The analytic surface can run the other way from the surface of revolution it replaces; the file's wire then turns against it and the face reads invalid, so it was kept as written (13 of a part's 26 bends, its spheres and tori). It is made again with its wires reversed (a general mend made the complement: a 62 mm2 torus face came back as 1,747). The material side is read from the two surfaces' normals. A re-made face that will not tessellate (a sphere whose wire now crosses its seam) is kept as written: with no triangles, no ray found it and its wall went unpaired.
8. **Voids (SHELL-15h).** A closed shell sewn inside another is that one's inner void (a closed box with a sealed cavity: one body, two shells, as in the STEP), not a second body the size of the cavity. Checked: the volume must come out as the outer less the voids.

9. **Bodies lying on each other along edges (SHELL-15i, 1 Nov, 14th helper).** A frame whose short tube lies on a longer one over its length (sh0102): sewing pairs the edges where four faces meet as the faces come, so it made one closed shell that is no valid solid - and, in another face order, one valid solid made of both tubes' faces. The faces are now sewn letting an edge hold more than two, and taken round each such edge in the order of the angle at which they leave it:
   - each face at its own angle (bodies touching along the edge): the two faces either side of each wedge of material are one body's. A wedge is material when a ray from a point in it crosses an odd number of the file's faces (the most of three rays);
   - two pairs of faces lying on each other (one body on another): each body holds one face of each pair. The two faces of a pair are in different bodies; the bodies are two-coloured by that.

   Each group of faces is then sewn alone and must close into one valid solid, or nothing changes. sh0102's IGES copy now reads as its STEP: two tubes, 394058 and 1105222 mm3 (STEP 394058 and 1105220).

Still open: bodies that touch over PART of a face (sh0013's duct and its insulation). There an edge of one body lies inside a face of the other, so the faces round it are neither each at their own angle nor in two pairs (one edge of sh0013 has six faces in four clusters: 2, 1, 1, 2). Reading them needs the larger face split along the other body's edge first.

### 2.4.2 The work budget (SHELL-18, 30 Oct, 13th helper)

An open-hardware part ran its extraction past 30 minutes. The extraction now plans its work against a work budget (1500 s; first 540 s, see SHELL-18c below):
- the stages before the joins check the clock as they go and stop, in words, when it is spent ("the mid-surface extraction used its 540 s work budget while making the mid-faces ...");
- each general join gets at most what is left (less 60 s kept for the checks after it), and none starts with under 30 s left: the straight join then stands and the notes say so;
- the whole extraction runs in a child process stopped at 1800 s (first 780 s; see SHELL-18c below), because one OpenCASCADE call cannot be stopped from inside.

A part out of time is meshed solid, told why: never a wait with no end. The child must not be a daemon: a daemon may not fork, and the general joins (SHELL-11) fork. The first try made it one, every general join failed quietly, and the training set fell from 61 to 48 passes; a test now holds a general join inside the bounded extraction.

When the deadline stops the extraction, the general join it forked is stopped with it (SHELL-18b): killed alone, the join would run on, orphaned, holding a core and its memory until its OpenCASCADE call ends. Its processes are read from /proc and killed first.

Two corrections from the 14th helper's lab-VM sweep (1 Nov):
- **SHELL-18c, the budget sits far above any part that finishes.** The clock is the machine's. At 540 / 780 s, on a lab VM running eight at once (about as slow per part as a worker), five training parts that shell in 290-410 s on a laptop (sh0020, sh0021, sh0049, sh0083, sh0105) ran out and were thrown back solid whole. On that VM the code before these rules takes up to 1244 s (sh0105). The budget is now 1500 s and the hard stop 1800 s; and running out while joining no longer throws the part back: a body keeps its straight join, as when a general join is skipped.
- **SHELL-18d, a new process, not a fork.** A fork copies the caller's locks but not its threads. Once a parallel OpenCASCADE call has started its thread pool (the intake and stage meshes run parallel), a forked child waits for pool workers that are not there: in a long test run a 2 s extraction hung to the deadline, twice. The bounded extraction now starts a new Python process that reads the file itself, with the caller's import path; a test holds it after a parallel mesh.

### 2.5 Assemblies and mixed parts (PR 3)

**Bonded plates in different bodies:**
- The same extension rule, aimed across bodies that `cad/fea_joins` says touch. Touching plates share nodes.
- Where a gap exists (PLAN-fea-18's small-gap rule), a tie is used, never shared nodes:
  - Nastran: RBE3 or BSURF/BCONTACT glue;
  - Abaqus and CalculiX: `*TIE`;
  - Ansys: bonded contact.

**Mixed part (shell meets solid):**
- The shell's edge on the solid region is a "solid link" in the record, with its place. The same holds for every SHELL-6 fallback region (an open edge meshed solid).
- **The join must carry BENDING MOMENTS, not just forces (Areen, 29 Oct).** Shell nodes have rotations; solid nodes have none. A plain node tie or shared nodes lets the shell hinge at the joint, which no check on forces will show. So never a plain tie; the connection, per solver:
  - Abaqus: `*SHELL TO SOLID COUPLING` (or a distributing coupling over the solid face across the shell's thickness band);
  - CalculiX: it has no `*SHELL TO SOLID COUPLING`. Candidates, decided by the textbook cases below: the overlap method (the shell edge runs one wall deep into the solid and shares its nodes there, so the moment passes as a force couple), or a `*COUPLING` / `*DISTRIBUTING` over the solid face's thickness band with the shell edge node as the reference node;
  - Nastran: RBE3 from each shell edge node (all six DOFs) to the solid face nodes across the thickness band, or glued shell-to-solid edge contact (BSURF/BCONTACT);
  - Ansys: MPC184 or shell-solid assembly contact (CONTA175 / TARGE170 with the shell-solid KEYOPT).
- **The solid region gets at least 2 quadratic elements through the thickness** at the link, so its face can carry the linear stress through the wall that a moment puts on it.
- **Proved in CalculiX, in the accuracy lane (PR 2):**
  - a cantilever plate, half shell and half solid, tip load: the tip deflection within 2% of the pure-shell model and of beam theory (PL³/3EI);
  - a T-joint under bending (the stiffener shell, the plate solid, and the other way round), against the all-solid mesh;
  - a plate with a solid boss, against the all-solid mesh.

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

- **One PSHELL per sheet thickness, per part.** Part_3 at 6 mm becomes "Part_3_t6mm". Material: exactly as today's solid decks - mesh only, no material: PSHELL k names MAT1 k (Abaqus: MATERIAL=MAT_k) and the deck header says "add MAT1 k" (decided 29 Oct). The textbook tests add their own material.
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
- **Volume:** a shell body's sewn faces (the mid-faces as extended to their junctions) times their walls hold its volume within −15% to +10%. Junction corners are counted once (a short-legged angle's corner is a fifth of its metal).
- **Every face accounted for:** each face of a shell body is paired, rim or blend. Nothing is lost.
- **Connectivity:** every free edge of the sewn surface is either a real boundary (outside just past it) or, on a mixed part, a solid link. An OPEN edge is material past a free edge with no solid region there. It is a junction we did not reach, and it is listed with its place.
  - The outside is read at 0.05, 0.15 and 0.3 walls past the edge: outside anywhere along the way is a real cut, however narrow (a lanced slit).
  - **A link across a blend or into a thick wall (SHELL-6b, 30 Oct).** The material past an edge also meets a thick region when a blend that rounds into that region lies within the plain test's reach (a pad whose foot is all fillet and round: the plate's edge stops where the fillet starts), or when the point just past the edge lies in the wall of a thick pair (its distances to the pair's two faces add up to that wall: a plate edge inside a pad's slab). Such links are counted apart (`blend_link_edges`) and the joins are chosen as if they were open, so a join that meets the walls themselves is still preferred. Fresh fr003 (a bracket's pad, 4 open edges), fr010 (a plate meeting a 10 mm boss, 1) and fr019 (3) now have none.
  - An edge ENDS IN A JUNCTION, not open, when it runs along a thicker wall inside it, or when both its ends lie inside sewn walls and one of them on a sewn mid-face (a gusset's slanted edge carried into the plate it stands on).
  - An edge shorter than half its wall is below the shell mesh's floor (SHMESH-2) and is counted apart, not as open.
  - A join must keep every mid-face whole: one whose sewn faces hold less than 99% of the mid-faces' own area is not used (a join that loses its mid-faces has no free edge left to count).

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
5. Shell-to-solid moment transfer (section 2.5): the half-shell, half-solid cantilever (tip within 2% of pure shell and of PL³/3EI) and the T-joint in bending against all-solid.

## 7. Intake and stage

**Intake: two routes into shell (Areen, 29 Oct).** Shell adds no new question.

1. **The user says it.** At the first question ("what are you meshing this for?"), shell words set **Mesh as: Shell** with no follow-up question:
   - shell words: "shell", "2D", "mid-surface" / "midsurface", "plate model", "Femap shell", "CQUAD4", "sheet-metal shell" and the like;
   - "solid", "3D", "tets" or "hex" set **Solid** the same way.

   This uses the intake's existing deterministic mapping plus the AI guard-rail pattern: a fixed word table first, then the model only for paraphrases, its answer checked against the allowed values (shell, solid, or nothing said). Intake tests cover the word table and a set of paraphrases.
2. **The user says nothing.** After the check measures the part, the FEA stage shows **"Mesh as: Auto (recommended) / Shell / Solid"**:
   - **Auto** recommends Shell when the measured thin-wall share is high. The reason is in plain words, for example: "90% of this part is plate under 6 mm thick, so a shell mesh is about 100× smaller, and it is what engineers use for sheet metal."
   - Mixed parts are recommended Solid until PR 3 lands, then shell where thin and solid where thick (the coordinator's answer, 29 Oct).
   - The user can switch.

**A user's explicit choice is never overridden.** If they say Shell but the part has solid regions, the stage says which regions stay solid and why.

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
2. Decided 29 Oct: no material in the deck, exactly as the solid decks (mesh only, "add MAT1").
3. Answered 29 Oct: mixed parts in Auto are recommended Solid until PR 3 lands.
