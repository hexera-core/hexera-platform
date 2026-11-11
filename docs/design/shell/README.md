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

**Since SHELL-23 (10 Nov) these two figures are advice, not a gate.** A sheet that fails them is still a shell when it is plate-like (section 1.5): a shell is only ever made because the user asked for one. The figures now decide one thing: which walls get the line of advice.

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

**A solid body says why, in numbers (SHELL-2c, 30 Oct; the words are SHELL-23's since 10 Nov).** Its most plate-like wall (the widest against its thickness) is named: "no wall of it is a plate: its most plate-like wall is 30 mm thick but only 40 mm wide, 1.3 times its thickness; under 2 times, it is a bar or a block, not a plate". A thick ring says so ("18 mm thick round a tight bore, a thick ring; a curved wall is a shell when its radius is at least 2 times its thickness"). The record keeps it as `closest_wall`, with what it failed. The stage shows it (SHSTAGE-3b), and the part is meshed solid.

### 1.5 Every plate is a shell (SHELL-23, 10 Nov)

**The founder's call.** "If the user says shell, why wouldn't we convert everything into a shell?" A shell is only ever made because the user asked for one in words. Their request beats our accuracy minimums: warn, do not refuse. So the 10 walls of SHELL-2 stop being a gate.

**What showed it.** A welded plate pedestal with a rolled channel, which its users mesh all as shells: a pipe column, base and top plates, eight triangular gussets, a channel beam. Its plates and pipe came out right (mid-surfaces within 0.2 mm of the users' own shell mesh, thicknesses equal). Its eight gussets, 12.7 mm thick over a 96.5 mm face, stayed solid ("only 7.6 times its thickness; a shell needs at least 10 times") and were left out of the shell files: 8 of 14 bodies.

**The rule.** A sheet that fails SHELL-2's thin rule is still a shell when it is plate-like. All four must hold:

| it is | measured | what fails | why this line |
|---|---|---|---|
| wide against its wall | at least 2 walls wide, on its own mid-faces: the narrower of `width_of` and its widest place (the largest circle that fits in a mid-face, holes counted). A plate split by a wall standing on it (a beam's flange, either side of the web) is read on the face its pairs share. | a bar, a block, a cube, a flange ring 1.9 walls across | Narrower, the pair of faces across the width is nearly as close as the pair across the thickness: no one mid-surface. And the mesh's own rules meet there: four elements across (SHMESH-1), none under half the wall (SHMESH-2). |
| no thick ring | at least half its mid-surface is flat, or round a radius of 2 walls or more | a bush, a nut, a hollow boss (a bore under 3 walls across) | No shell stands for a wall thicker than half its radius. A tight bend rides with the flat plates it joins, as in SHELL-2. |
| metal between its faces | 90% of the points read on its mid-face lie in the body | two faces that look at each other across a gap (a channel's two flanges, across its open side) | A mid-face in air is no wall. |
| no pad on a thinner wall | neither of its faces is a side of a thinner shell wall | a pad, a boss, a land or a foot standing on a plate | It shares the plate's far face: thicker metal on that plate, not a plate of its own. It stays solid, as it always did. Two plates stacked are a lap (SHELL-8), as before. |

The thin sheets are settled first, then the laps, then the others from the thinnest up, so a pad is told by the wall under it.

Two more guards, both on the whole body:

- **The body must be mostly plate.** The plates found in a body are kept only when its shell walls (the thin ones and these) then hold at least half its metal: its sewn mid-faces times their walls, over its volume. This is MIXED-4's measure and MIXED-4's half. A plate with a lump on it is a shell with a solid region. A lump with a tab on it is a lump: a shell of the tab alone would be a scrap of the part, where the user got a whole solid mesh and the reason before. Under half, the body is left exactly as it was. The line is drawn twice: on the bare mid-faces before any join (junction metal is not in them), and on the sewn shell after. With the first line at a quarter, 7 more dev parts came in: 5 shelled well (a flag plate, a cable-chain mount, a chassis floor, a channel block, a panel clip), and 2 frames that meshed before lost their mesh (gmsh could not close a boundary where the short walls join). So both lines stay at half until those junctions mesh.
- **The plates must join.** A body whose new plates (or flat flanges, SHELL-24) leave edges open where they meet its other walls is read again without them, and the reading with fewer open edges is kept, said in a note. So a body that sewed shut before still does. At most two more passes, inside the same work budget.

**What stays solid.** Anything with no mid-surface: a block, a cube, a bar under 2 walls wide, a bolt, a shaft, a ball, a thick ring, a boss or pad on a plate. Measured on generated parts (`~/plates_lab`): a 30 mm cube, a 50 x 40 x 30 block, a 20 x 20 bar, a round bar, an M12 bolt, a stepped shaft, a block with a boss, a thick bush, a block with a pocket, a ball: all solid, each with its reason. A 100 x 50 x 25 block is the line itself (2.0 walls wide): a shell.

**What is a shell now.** Flat plates and triangular gussets from 3 to 12 walls across (24 parts): all shell, mid-surface exact, volume held 1.000. Flat bars 2, 3, 4 and 6 walls wide: shell; 1 and 1.5: solid. The look-alike pedestal (13 bodies: plates, a pipe, eight gussets, a rolled channel, a support plate 4 walls across): 13 of 13 shell, 0 left solid, 0 open edges, every thickness the plate's own (4 shell and 9 solid before).

**The advice, one line** (`cad/shell_words.py`; the record's `short_walls` and `advice`). The stage shows it in the shell fold's own muted line, and the result says it after the element count. No new control.

- one wall: "Part_6: 12.7 mm thick over 96.5 mm, 7.6 times its thickness. Shell theory is less exact under 10 times (about 2% off on a free plate this short, more where it bends from a joint)."
- several: "9 walls are short for their thickness (the shortest, Part_13: 31.8 mm thick over 126 mm, 4 times its thickness). Shell theory is less exact under 10 times (about 5% off on a free plate this short, more where it bends from a joint)."
- a wall 10 walls across but under 4 wide: "... 5 mm thick and 15 mm wide, 3 times its thickness. Shell theory is less exact under 4 times (about 2% off on a free strip this narrow, ...)."

**What a short plate costs, measured** (CalculiX 2.21 on one thread; free-free modes 7 to 12; the product's shell mesh, S4, against a C3D10 solid of the same part, three elements through the plate; the worst of the six modes, and the first):

| walls across | 12 | 10 | 9 | 7.6 | 6 | 5 | 4 | 3 |
|---|---|---|---|---|---|---|---|---|
| square plate, worst | +2.4% | +2.3% | +2.3% | +2.1% | -2.3% | -3.2% | -4.7% | -7.4% |
| square plate, first mode | +2.4% | +2.3% | +2.3% | +2.1% | +2.0% | +1.9% | +2.2% | +3.0% |
| triangular gusset, worst | +2.1% | +1.6% | +1.5% | +1.3% | +1.2% | -2.0% | +1.6% | -2.6% |

Flat bars 200 mm long, 2, 3, 4 and 6 walls wide: 3.2%, 2.2%, 2.1%, 2.0%.

So a plate ALONE loses nothing down to about 6 walls (2.1% at 7.6 walls, as at 12), and reads 3% off at 5, 5% at 4, 7% at 3. The advice quotes the figure for the wall's own ratio. On structured meshes (the accuracy lane's test: a plate clamped at one end and pushed at the other, S4 against C3D20R four through the wall) the shell's tip is -0.05% off the solid's at 7.6 walls, -0.05% at 5, -0.30% at 3: so the 2% left at every ratio above looks like the mesh, not the theory (not separated here). CalculiX expands a shell into one layer of solid elements; Nastran's plate elements were not run.

The advice also says where the real cost is: a short wall bending from a joint. A shell wall bends from the middle of its joint, and MIXED-8's box (4 mm walls reaching 5 to 6.7 walls from their joints) read +23%.

**Knock-on effects** (the lab and the dev set): section 2.4.4.

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

### 2.2.2 A rolled flange is one flat plate at its mean thickness (SHELL-24, 10 Nov)

**What showed it.** The same pedestal's rolled channel: its web was right, but only 19% of the users' flange surface lay within 2 mm of ours, and what we had carried 23.0 mm where the section table says 17.8. A rolled channel's flange is tapered (thin at the toe, thick at the web), with a root fillet and a rounded toe. On main the pair is a tapered wall (SHELL-9): judged at its thickest point (23.0), fitted over the sloped face only (short of the toe and of the web), and tilted. Three of seven look-alike channels came out "mixed" with their flanges solid.

**The rule.** A tapered wall whose two faces are both PLANES, and which is at most 10 of its mean thickness long ALONG its taper, is one flat plate:

- **Flat, and where.** Parallel to the larger of its two faces (the flange's flat outside: the datum), half the mean thickness inside it. That is the plate every section table, hand check and solver property card assumes.
- **Its thickness: the mean.** Read on the two planes at the middle of the wall's whole length along the taper: from the face of the wall it grows out of (the next flat face past its root fillet: the web's) to its own end face (past its toe round). The wall is linear between, so that is its mean over that length, which is what the tables call the flange thickness.
- **Its outline.** The datum face's own outline: it runs to the wall's true end under a rounded toe. On a side where another wall begins it stops at the smaller face's end, and SHELL-3 carries it on to that wall's mid-plane. So the flange is sewn to the web.
- **Its check.** Half its wall inside its datum face, everywhere (it is not midway between two faces that taper; the record says `flat`, its `datum` and `taper_mm`).

A longer tapered wall (a wedge, a drafted side, a printed cone) keeps SHELL-9's fitted mid-surface and its true wall at every node: it bends along its taper, where stiffness goes with the cube of the thickness (a 2-degree wedge meshed at one wall was 41 to 68% too stiff).

**Measured on rolled sections** drawn from the public shape tables with their true taper, fillet and toe round (`~/plates_lab/tools/parts.py`; AISC C, MC, S, W, L, WT, ST; EN UPN, IPN; 600 mm long). Coverage: the share of the table's flange mid-plane rectangle (web mid-plane to toe) within 0.25 mm of our surface.

| section | before: flange coverage, thickness against the table | now |
|---|---|---|
| C15x50, C6x13, MC12x50, MC18x58 | 6 to 13%, -3.3 to -4.3% (fitted, tilted) | 100%, exact |
| C10x30, MC12x31, UPN200 | 0%: flanges solid, body "mixed" | 100%, exact |
| S12x50, S8x18.4, IPN200, ST6x25 (four or two tapered half flanges) | 8 to 17%, -2.1 to -4.3% | 100%, exact |
| W8x31, W12x26, W14x90, WT6x25 (flat flanges) | 100%, exact | the same |
| W14x730 (a jumbo: 125 mm flanges 3.6 walls wide) | solid | 100%, exact (SHELL-23) |
| L4x4x1/2, L6x4x3/8, L2x2x3/8 | 100%, exact | the same |
| L8x8x1-1/8 | 95.8% | 95.8% (below) |

Every section: one body, verdict shell, 0 open edges, flanges sewn to the web. "Exact" is against the mean over the clear outstand, which is the AISC tables' flange thickness. The EN tables read a UPN's (up to 300 mm) at half the flange width and an IPN's at a quarter: 3.0% and 2.4% over the mean. Ours is the mean, so it is that much under those two tables' figure; the metal is right (the look-alikes' section areas match the tables).

**Not done: the stockiest angle.** An even wall with a rounded toe stops at the toe round's start when the straight join is kept (the general join runs it on to the skin). An L8x8x1-1/8 keeps the straight join and covers 95.8% of its legs, on main as here. Carrying every even plate to its own end changes the mid-surface of every plate with a one-sided round or bevel in the corpus; it needs its own regression run.

**Flat at the mean, or the true wall? Measured both ways** (CalculiX on one thread; free-free modes 7 to 12 against a C3D10 solid of the CAD, two elements a wall, 1.5 on the 1500 mm ones; first mode / worst of six):

| 600 mm long | flat, mean thickness (the default) | flat, true wall at each node | fitted and tilted, true wall (SHELL-9, as before) |
|---|---|---|---|
| C15x50 | -3.6% / -9.0% | -2.1% / -5.2% | -1.9% / -5.4% |
| MC12x50 | -5.8% / -16.9% | -3.4% / -6.9% | -3.7% / -6.0% |
| S12x50 | -10.2% / -15.0% | -7.5% / -8.2% | -8.0% / -8.0% |
| C10x30 | -3.5% / -5.9% | -2.3% / -5.7% | flanges solid (-15% / -50%) |
| UPN200 | -5.3% / -14.5% | not run | flanges solid (-36% / -50%) |
| C15x50, 1500 mm | -2.9% / +6.7% | -1.6% / -3.6% | -3.0% / -4.3% |
| MC12x50, 1500 mm | -6.0% / +7.4% | -4.2% / -4.2% | not run |
| S12x50, 1500 mm | -11.0% / -11.0% | -7.9% / -7.9% | -9.4% / -9.4% |

- **What the code does best:** the flat plate in its table place with the wall's true thickness at every node (`FLAT_TAPER_WALL = "true"`, one constant: the record then carries SHELL-9b's readings, taken on the two planes). It is as good as the fitted surface and keeps the flat plate's full coverage and simple join.
- **What is the default, and why:** the mean thickness (`"mean"`), as the founder leaned: it is what the users' own shell models, their property cards and their hand checks carry, so our mesh can be laid over theirs and agree. It costs accuracy on modes where the flange bends as a plate from the web: at the web the true flange is 1.4 times the mean (and the fillet's metal is in no shell model), so the mean plate is soft there. On a short stub's higher modes that is up to 17%. The users' own convention has the same error.
- The section's area, and its distance from the section's middle, are kept either way (the look-alikes' sewn faces times their walls hold 0.99 to 1.02 of the volume).

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

### 2.4.3 Open edges on real parts (SHELL-21, SHELL-22, 10 Nov)

On the sealed real-world set, open edges are the largest real defect (22 of 80 files by first failure on main). Measured first, on real parts the lab was not tuned on: 195 files drawn like the sealed set from open-hardware repositories used nowhere else (the 60-file wall set and 135 new ones: ducts, tanks, tubes, enclosures, frames, brackets). Every open edge was read by what lies past it, walking outward from the edge in its own face:

| what lies past the open edge (on main) | edges | files |
|---|---|---|
| thicker metal, no shell wall there (a boss, a rib's foot, a corner post, a block) | 73 | 12 |
| the wall's own end, under one wall on (a bevelled or rounded edge, a lip) | 44 | 9 |
| the wall going on at its own thickness, not shelled there (uneven, drafted, narrow) | 43 | 10 |
| another mid-face under the junction reach away (a junction the joins did not close) | 11 | 4 |
| another mid-face further than the reach | 14 | 7 |
| a wall in line, its mid-surface a step away (a thickness change) | 6 | 3 |

(All 20 files with open edges of the 195, 191 edges.) So on real parts the edges that stop "half a wall short at a T or an L" are the smaller part. Most open edges were not gaps in the shell at all: the wall runs into a thick region, or simply ends. And where a junction was left open, a join had given up on a whole face or a whole body for one local reason (on these parts the general join often runs past its 5 minutes, loses mid-faces, or dies inside OpenCASCADE). Two rules, each general:

**SHELL-21, what lies past an edge the joins left open is read on the part.** An edge with metal past it used to be a solid link only when a face NAMED thick or solid lay within 0.8 walls of it. But a thick region's faces beside the edge are often its own chamfers, fillets and small steps, named rim or blend by their size, and the link was not seen; and a plate whose two faces stop facing each other before the plate ends (a bevel, a rounded end) has its own metal past its mid-face's edge. The body's last census now walks outward from such an edge, at three places along it, while it stays in the metal:

- **A shell wall ahead: open, as before.** Within SHELL-3's reach the walk comes inside another sewn wall's slab (half its wall of its mid-face: a plate it runs into, a wall in line a step away, another piece of its own wall), or the edge carried straight on meets another wall's surface within half of each wall of its mid-face (a corner whose two walls both stop short). That is what SHELL-22 joins. Such an edge is a junction the joins did not close; it is never taken for a link or a free edge (a unit test holds a web stopped at its plate's face open in a mixed body).
- **The wall's own end: a free edge.** No wall ahead, the metal stops within 2 walls (SHELL-2's largest blend), and all the way it stays inside the wall's own slab (half a wall each side of the mid-face). Counted in `boundary_edges`, and apart as `rim_edges`.
- **A thick region: a solid link, in a mixed body only.** No wall ahead, and within 1.5 walls past the edge the metal reaches a whole wall off the mid-plane (half a wall past where the wall's own face would be). Counted in `solid_link_edges`, and apart as `bulk_link_edges`. A body with no thick region has nothing to link to: there the edge stays open.
- **Anything else stays open:** a wall that goes on at its own thickness and was not shelled.

It is read LAST, on the edges still open when the joins, SHELL-13 and SHELL-22 are done. Those all see every edge with metal past it as open, as they always did, so every join and strip is made exactly as before.

**SHELL-22, an open edge is carried on to the wall it stops short of.** After the joins and SHELL-13, and before SHELL-21 reads anything, each edge still open is carried on, point by point (one a wall, 8 to 128), in its own face's surface to the SURFACE of the nearest other mid-face within SHELL-3's reach; the way must be in the metal and the meeting point within half of each wall of that mid-face itself. The strip between the edge and the meeting curve is sewn in. When the way is the same at every point the far side is the edge itself, moved: the strip is the edge's own surface carried on exactly (a plane, a cylinder along its axis). An edge that meets no surface that way but lies within the reach of another mid-face, the way to it in the metal, is joined to its nearest points on it: walls in line whose mid-surfaces are a step apart (a tube whose wall steps from 2 to 1.7 mm: two cylinders 0.15 mm apart), or two pieces of one wall. Kept only when it leaves fewer open edges and no tolerance past SHELL-12, and only with time left in the work budget (SHELL-18). It works one edge at a time, so one face the straight extension cannot redraw, or a general join that crashed, ran out of time or was thrown out (SHELL-11, 12, 17), no longer leaves every junction of the body open. A T joint with both joins switched off comes out as the straight extension makes it (the same area: a unit test); so do a tube on a plate and an L.

SHELL-13 looked only for a whole edge lying alongside a whole edge, and skipped two edges closer than ten times the sewing tolerance, which the sewing (at one times it) had not joined: a stepped tube whose two mid-cylinders are 0.01 to 0.4 mm apart was open at every step size tried. SHELL-22 takes any gap past the sewing tolerance itself; a strip under a tenth of a wall wide is then dropped and sewn across by SHELL-16.

**A part with no open edge on main is untouched.** Both rules act only on an edge still open after main's own joins and strips: every record and mesh of a part those close is byte-identical to main's.

**What stays open, said so.** A wall that goes on unshelled; a junction further than the reach (walls meeting through a corner post more than 2 walls deep); a piece of a wall cut off under a boss; a rib ending at a wall beside its plate. These are still counted, shown on the stage and told in the result ("the plates do not meet at N edges").

**Results (10 Nov; one WSL machine, one process at a time).** "Fully good" is the sealed set's own measure: a shell found, every extraction check, a valid mesh inside all Nastran limits.

| set | files | fully good | files with open edges | open edges | fully good before, not after |
|---|---|---|---|---|---|
| wall set (real parts, step 1) | 60 | 34 to 36 | 3 to 1 | 13 to 1 | none |
| new set (real parts: ducts, tanks, tubes, enclosures, frames) | 135 | 70 to 76 | 17 to 9 | 178 to 53 | none |
| lab (87 parts the code was tuned on) | 87 | 69 to 71 | 4 to 2 | 10 to 3 | none |

- **Which rule did it.** 12 parts are left with no open edge. 4 needed a SHELL-22 strip (a cartridge case whose wall steps, a flange with two tube stubs, an enclosure floor split under its screw posts, one more enclosure); 8 the SHELL-21 reading alone: their open edges were links to thick regions (6 parts) or the wall's own end (2), and their mid-surface and mesh are main's own. 10 of the 12 are fully good; one is outside the Nastran limits; one fails in the mesher (below).
- **Nothing else moved.** Every part with no open edge on main has the same record as on main (261 of the 282 files have), and every mesh is byte-identical to main's but on the 11 parts that got strips.
- **Known defects, both on parts with strips.** The flange with two tube stubs: its 2 open edges are closed by strips, and gmsh then cannot recover an edge at the tilted tube's foot, where the strips meet an older SHELL-13 strip and a 1.3 mm2 sliver mid-face. On main it meshed, with its 2 gaps said; here it is delivered solid, said so. And a keycap with free-form walls meshes in 554 s where it took 67 s (still inside the limit, and now inside the Nastran limits). The other 9 parts with strips mesh as on main (one of them not at all, on main either).
- **Accuracy** (CalculiX on one thread, free-free modes 7 to 12, S4 shell against C3D10 solid of the CAD): a T joint closed only by SHELL-22 -2.5 to +2.4% (the same joint by the straight extension -2.7 to +1.1%); a tube whose wall steps 3 to 2 mm (SHELL-22 across the step; two loose tubes on main) +1.8 to +6.3%, where the plain 3 mm tube of the same size, with no junction, is +3.6 to +5.0%: about one point of it is the step, the rest that short thick tube as a shell.
- **The same twice.** The 28 quicker parts of the 32 that can differ from main were run twice on the final code: the same record and the same surface file 28 of 28, and the same mesh and the same three decks, byte for byte, on all 26 that mesh. (On the first commit 54 parts were run twice, 22 of them touched by the rules: 54 of 54 and 51 of 51.)
- **Sealed set.** Run once, on this branch's first commit (0529473e, which also held SHELL-20 and read SHELL-21 before SHELL-13): fully good 28 to 37 of 80, files with open edges 22 to 8, open edges 90 to 23; by an aggregate count the 14 files left with no open edge were all cleared by the SHELL-21 reading, none by a strip. The two commits after it (SHELL-20 out; SHELL-21 read last) were made for the lab bracket and the dev duct named in their messages and were not run on the sealed set: it is run once. On the dev sets and the lab the first commit cleared 13 parts and the final one 12 of those 13; SHELL-20 was in play in 2 sealed files. So the final commit is expected a file or two under 37; the number to quote is the next milestone run's.

**What is still open, and the next class.** Of the 54 edges still open on the real parts (10 files), 26 are a wall that goes on past the edge at its own thickness and is not shelled there (a drafted or free-form stretch, a strip under four walls wide between two features: SHELL-1 and SHELL-2 leave it out, and with the general join timed out on such parts nothing runs across it); 15 have thicker metal past them and another wall beside the way (a rib ending at a wall, a floor piece cut off under a post); 8 end under a wall short of the part's skin with another wall beside them; 5 are junctions past the reach or inside one face. The first is wall finding, not joining: step 3.

**Found, not fixed here.**
- **The straight join models a relieved gusset soft.** SHELL-20 (a plate with holes redrawn hole by hole, so the straight extension works on it) was written, measured and taken out. It closed a printed chassis of slotted plates (13 open edges to 3). But the straight join wins a tie, so five lab parts the general join already closed changed their join, and a motor bracket with two relieved gussets went from 1.8% stiff to 6.1% soft on its first elastic mode against the solid: the straight join joins each gusset to its plates over 2 mm less than the general join does (16.5 and 12.5 mm against 18.5 and 14.5). (The three tube frames among them meshed with 2 to 8 times fewer elements; the one checked kept its modes within 0.3% at half the elements.) The tie rule and the gusset's foot come first.
- **A wall through air.** A flat hollow profile with solid edges (two 1.5 mm skins 4 mm apart): its two OUTER faces face each other through the solid edges, pass as a 7 mm shell wall, and their mid-face is laid through the cavity (the body holds 2.7 times its metal; the volume check is read on shell bodies only, and this one is mixed). On main as here.
- **SHELL-13 before SHELL-22.** One duct lost its last open edge on the first commit and keeps it on the final one: main's SHELL-13 strip beside it is made first again and leaves SHELL-22 no clean way. The better strip should win, whichever rule makes it.

### 2.4.4 Every plate a shell: what moved in the lab (SHELL-23, SHELL-24, 10 Nov)

Run on a lab VM, 10 at once, extraction then mesh then the three decks: the code before (the open-edges branch, 9ad6948d) against this one. "Fully good" as in 2.4.3: a shell found, every extraction check, a valid mesh inside all Nastran limits.

| set | files | fully good | no shell found | fully good before, not after |
|---|---|---|---|---|
| lab (87) and the dev set of real parts (60) | 147 | 107 to 112 | 25 to 21 | none |
| look-alikes (plates, gussets, bars, rolled sections, the pedestal, lumps) | 64 | 25 to 52 | 39 to 12 | none |

The 12 look-alikes with no shell are the ten lumps and the two bars under 2 walls wide: all meant to stay solid.

**Every file that changed, and the call on each.** 11 files of the 147. Every other file keeps its surface file byte for byte (134 of the 145 run on the final code) and, where it meshes, its mesh and three decks byte for byte (112 files). Two files were run on the commit before only: a chassis that stops at the 30-minute limit before and after, and a cone adapter that takes 25 minutes and is unchanged.

| file | what it is | before | now | call |
|---|---|---|---|---|
| d011 | a flat strip bracket, 3 mm by 6 mm by 67 mm | solid: "6.7 times" | shell, 269 elements | Right: a flat strip. It sits on the line (2.0 walls wide). |
| d041 | an extrusion foot, a 9 mm plate 5 walls across | solid | shell holds 97%, one thick region | Right. |
| d046 | a rail section, 2.5 mm wall, 5.3 walls across | solid | shell holds 78% | Right. |
| d053 | a pivot plate, 10 mm, 9.3 walls across | solid | shell holds 80% | Right. |
| d010 | an IMU mount; its second body has 5 mm walls 3.5 to 6.7 walls across | that body solid | shell and solid regions | A shell, as asked. Weak spot: its sewn faces times their walls hold 1.18 of the body (thick short walls overlap where they join; the volume check is read on all-shell bodies only). |
| d028 | a switch enclosure | shell holds 79% | 94%: two 5 mm walls and two small 1 mm ones joined in | Better. |
| d029 | a camera cover | shell held 6% of the part | all of it: two 5 mm walls and a short drafted wall, flat at its mean | Better: the shell was a scrap before. Triangles 1% to 14%. |
| d030 | a flight-controller housing | shell holds 75%, outside the Nastran limits | 85%, inside them | Better. |
| d038 | a tube holder | a 4 mm patch of the wall was a hole in the shell | filled: all shell | Better. |
| d048 | a tube base | shell holds 57% | 89%: six 2.5 mm tabs joined in | Better. |
| sh0089 | a CubeSat frame | 1 mid-face | 11: ten 2 mm tabs joined in | Better. |

No part that was rightly solid turned into a shell. What kept them out:

- **The body share.** Five dev parts that were solid, and one body of a sixth, have a plate-like tab or lip on a thick body: their plates hold 8% to 19% of the metal. Without the share they came out "shell and solid regions" with a scrap of shell in the files; with it they are solid, as before. A skin panel whose new plates held 26% (and brought 34 open edges) keeps the shell it had. Seven more parts sit between a quarter and a half and are left as they were (above).
- **The plates must join.** A printed buoy's seventh body gained 12 short walls and 12 open edges with them; it is read again without them and keeps its sewn shell.
- **No flat plate under two walls wide.** A drafted rib's two sides are a tapered pair too. They get no flat mid-face, so a part with no new plate keeps its record's mid-face numbers, and its decks, as they were.

**Costs, said plainly.**
- Triangles. Short walls mesh with more triangles: six of the eleven are over the 5% target (5.2% to 14.9%; one of them was at 5.7% before). All are inside every Nastran limit.
- Time. Extraction is 10 to 20% slower on parts with many thick or tapered pairs (a hull part alone on the VM: 36 s to 43 s). A body read again without its plates costs up to two more passes (a skin panel 11 s to 36 s).

**The same twice.** The 145 files run twice on the final code: the same record and the same surface file 145 of 145; the same mesh and the same three decks, byte for byte, on all 123 that mesh.

### 2.4.5 Walls never found as walls (SHELL-40 to SHELL-44, 11 Nov)

The last round found that most open edges left on real parts sit beside a wall the pairing never made a wall. So
the walls were measured on the part itself, not on the extractor's pairs.

**How "wall missed" is measured.** 4,000 points over the solid's skin (by area, a fixed seed). From each, a ray
inward gives the local wall and the skin across it; the point is a wall's side when the skin across faces it back
within 12 degrees (SHELL-9's 10, plus 2 for the triangles). From the wall's middle there, a walk along the wall in 8
directions, half a wall a step, re-centred between the skins each step, while the wall keeps its thickness within
1.5 times. The skin is THIN-WALL skin when the wall is at least 4 walls wide and 10 walls long there (SHELL-2's own
1/4 and 1/10, read locally, with no faces, pairs or sheets), or when a walk from it runs into such wall of about
its thickness (a strip between cut-outs, a stretch beside a boss). It is COVERED when the delivered mid-surface
passes within half the local wall of the wall's middle. Each point not covered is put down to its two CAD faces and
to what the record says of that pair. Tools: `~/shellcamp/walls/tools/skin.py`, `classes.py`. A corner whose two
skins turn by more than 12 degrees is not counted as wall skin: that class shows in open edges, not here.

**What was missed (282 real and lab parts: the 60-file wall set, the 135 new real parts, the 87 lab parts; 225 have
thin-wall skin).** Ranked by the sum over files of each file's missed share (1.00 = one whole file):

| class | files | share | whose |
|---|---|---|---|
| a whole wall judged too thick against its span or width | 51 | 22.3 | the shell-or-solid verdict (plates stream) |
| a bore, a round or a blend beside a flat face, turned past 10 degrees | 34 | 3.4 | mostly no wall |
| a drafted or free-form wall whose outline did not close or whose overlap came back empty | 8 | 2.3 | SHELL-42 |
| a free-form wall whose fitted mid-surface was off the middle between the fitted points | 4 | 0.9 | SHELL-43 |
| a face that fills little of its parameter box: "too few points of the face could be read" | 15 | 0.7 | SHELL-40 |
| a piece of a wall judged too thick beside a shell piece of it (a step, a strip) | 8 | 0.6 | verdict |
| a drafted tube (a cone facing a cylinder) whose wall read "the two faces do not overlap" | 2 | 0.3 | SHELL-41 |
| a shell pair whose mid-face is not in the sewn surface | 3 | 0.3 | joins |
| never named a pair by the rays | 16 | 0.1 | SHELL-44 |

So the pairing's own misses are few in area against the verdict's, but they are whole walls, and each leaves a hole
in the shell beside walls that are found.

**SHELL-40, the wall read on the face itself.** The even-wall and taper tests read a 7 x 7 grid over a face's
PARAMETER BOX and keep the points inside the face. A face that fills little of its box (a frame round a cut-out, a
ring, an L, a strip lying askew in its plane's parameters) kept under 3 points and was thrown out ("too few points
of the face could be read": a 2 mm duct's flange 12 walls wide, a 9.5 mm plate's pocket floors). When fewer than 12
of the 49 points lie in the face, the grid is made twice as fine, at most three times (7, 14, 28, 56 a side), until
12 do. 12 is a quarter of 49: a face filling a quarter of its box or more is read exactly as before.

**SHELL-41, a foot on a closed face in the face's own turn.** A point projected onto a cylinder, a cone or any closed
surface comes back in the surface's first period; a face on it may run from pi to 3 pi. Read as it came, half the
feet of a drafted tube's wall lay "off" the tube and its two faces "did not overlap" (a moulded tank's cone in its
cylinder, a dust jar's drafted wall). The parameters are moved by whole periods into the face's range first, in the
taper test and in the fitted mid-face's "B lies across A".

**SHELL-42, an outline is closed as the file says it is.** The pieces of a face's outline share their end points in
the B-rep, but their parameter curves meet only within the file's tolerance (up to 7e-6 of the parameter box on the
parts measured: a cyclone's free-form walls, a duct's, an enclosure's, a bracket's drafted walls), and the wire
builder asks 1e-7: the fitted mid-face's outline came out in pieces ("the two faces do not overlap") or not closed
("not a valid face"). Each piece is made to start where the one before ends (CHAIN_GAP 1e-3 of the box keeps apart
ends that are not one point: an outline wound round a closed surface ends a whole period away). OpenCASCADE's flat
Boolean of the two outlines also found NOTHING at one tolerance where it finds the overlap either side of it
(0.358 of the box at 1e-7, 1e-5, 1e-4, 1e-3, nothing at 1e-6); an empty overlap is asked again at 10, 0.1 and 100
times. A wrong piece cannot pass: every fitted mid-face is read against both its faces (SHELL-9).

**SHELL-43, a finer fit where the first is off the middle.** A free-form wall that curves between the 17 x 17 fitted
midpoints (a blow-moulded tank's side) was up to 5% of its wall off the middle between them and thrown out
(FIDELITY_SHARE 2%). It is fitted once more through 33 a side (a cubic fit's error falls about 16 times: 5% to
0.4% on that tank) and kept only if it holds the middle. Once, not twice: 65 a side took 5 to 7 s a pair. A fitted
overlap under a tenth of a wall wide (SHELL-16's sliver) is no mid-face: read finely enough, a sliver along A's own
edge lies in the middle of its wall.

**SHELL-44, no pairing ray lost on a flat face.** VTK's line test, asked with a tolerance of exactly 0, misses a
triangle whose box has no thickness - a flat face square to an axis, the far side of the commonest wall there is -
for part of the rays that meet it. On 237 parts: 2.5% of the pairing rays read no wall that way on average, over 5%
on 23 parts, 85% on one; every ray lost left through such a triangle; on 71 parts the faces named as
partners changed. The pairing's rays now ask 1e-9 mm. The FEA wall readings (`fea_features.wall_thickness`) are left
as they were (the FEA path's regression is not this stream's); they lose the same rays.

**Tried and taken out: SHELL-45, a bend whose rounds are not concentric.** A pair turned past 10 degrees was let
through as a tapered wall when it faced squarely somewhere (within 2 degrees), turned at most 22.5 degrees and kept
its wall within 1 + tan 10 / 0.25 = 1.71 times. On the 282 parts it changed 4: an enclosure's 2 open edges closed but
its mesh then ran past 15 minutes (SHMESH-21); a fully good tank got 1 open edge at its knuckle and went 10 s to 386
s; a frame's rounded tube took 18% more metal than it has (a fitted range check took that back). The generated
sheet-metal brackets it was meant for (2 mm sheet, both rounds 4 to 6 mm, not concentric) still read MIXED with or
without it: their bends pass the taper test but are fitted only when narrower than four walls is excused, and their
thinnest reading (2.4 mm at the grid's band middles) does not join them to their 2 mm flanges' wall (SHELL-2b). That
grouping, at the bend's own edges, is the next step for bends.

**Results (11 Nov, lab VM; "fully good" as the sealed set reads it).** Skin coverage is the mean over files of the
share of thin-wall skin with shell.

| set | files | fully good | skin coverage | files 99% covered | files with open edges | open edges |
|---|---|---|---|---|---|---|
| wall set (real parts) | 60 | 36 to 36 | 87.3 to 87.4% | 25 to 26 of 40 | 1 to 1 | 1 to 1 |
| new set (real parts) | 135 | 76 to 76 | 77.2 to 78.4% | 56 to 61 of 104 | 9 to 8 | 53 to 137 |
| lab (tuned on) | 87 | 71 to 70 | 96.9 to 98.0% | 70 to 76 of 81 | 2 to 4 | 3 to 70 |

On the plates stream's head (#436) the same rules move the same files: fully good 41 / 78 / 71 to 41 / 78 / 70, skin
coverage 87.8 / 80.7 / 96.5% to 87.9 / 81.8 / 97.9%.

- **Better.** 15 files hold more of their walls (coverage up a point or more, none down): a duct bend and a reducer
  now shell whole (their bodies mixed or solid before), an octagonal chamber's 7 more bodies, a tank's drafted cone
  in its cylinder (open edge 1 to 0, now fully good), two tanks and an enclosure 7, 9 and 6 open edges to 0, 4, 4.
- **Worse by the count, judged.** Three parts whose walls are now found open new junctions the joins do not close:
  a cyclone inlet body ("fully good" before with 10% of its metal shelled; now 51%, 14 open edges; the general join
  runs to its 300 s deadline: 58 to 693 s), an enclosure plate whose pocket floors are now walls (62% of its metal to
  102%, 100 open edges at the pocket steps), a cyclone cone (outside the Nastran limits before, now one shell with 53
  open edges). The shell they had before was the wrong model (most walls missing); the open edges are joins work.
- **Run time** (base and this branch side by side under the same load, 281 files): 12,233 s to 11,326 s. Five files
  more than 1.5 times slower, each holding new walls: the cyclone inlet (the general join above), a duct reducer's mesh
  (12 to 106 s: 18,250 to 88,271 elements, its two new walls are 20-across panels), a duct bend (37 to 65 s mesh), a
  bracket and the enclosure plate (1.6 times).
- **The same twice.** All 281 records and surfaces byte-identical over two runs; meshes identical but on two files
  whose gmsh quad recombination differs run to run on the base code too.

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
- **Decided by the CalculiX cases (29 Oct, `engines/shell_links.py`, SHLINK-1..3, placeholders).** CalculiX's
  `*DISTRIBUTING` coupling needs element-face surfaces and takes a node into one coupling only, so per-node
  couplings along a shell edge cannot be written; rotations of shell nodes do not reach `*EQUATION`. What works,
  with translations only:
  - **SHLINK-1, edge link: the shell runs into the solid** by one wall (at least one element row), and each of
    its nodes there is tied (`*EQUATION`) to the mean of the solid nodes on its wall's thickness line (within half
    the wall along its normal, their foot within a quarter of the shell's element size). The moment crosses as the
    couple between the overlapped node rows. Wider bands stiffen the join (half an element: 15% stiff), so the
    solid is meshed with nodes on the overlapped strip.
  - **SHLINK-2, face link: a solid standing on a shell's face** (a thick web on a shelled plate): each solid node
    on the contact face rides on the shell point under it, moved rigidly by its offset from the mid-surface.
  - Measured (`tests/unit/engines/test_shell_textbook.py`, CI's accuracy lane):
    - half-shell, half-solid cantilever 200 x 20 x 2: tip 0.6% off the pure shell, 1.7% off PL³/3EI;
    - shell and solid sharing only their meeting line: the tip moved 10^9 times too far (a hinge);
    - T-joint in bending, web shell into a solid plate: 4.6% off the all-solid T (1.6% off the all-shell T,
      which is itself 6.3% softer than all-solid: its junction has no corner metal);
    - T-joint, plate shell under a solid web: 4.0% off the all-solid T.
  - Nastran and Ansys, and the mixed-part mesher: PR 3, section 2.6 (the same ties as MPC and CE).
- **The solid region gets at least 2 quadratic elements through the thickness** at the link, so its face can carry the linear stress through the wall that a moment puts on it.
- **Proved in CalculiX, in the accuracy lane (PR 2):**
  - a cantilever plate, half shell and half solid, tip load: the tip deflection within 2% of the pure-shell model and of beam theory (PL³/3EI);
  - a T-joint under bending (the stiffener shell, the plate solid, and the other way round), against the all-solid mesh;
  - a plate with a solid boss, against the all-solid mesh.

### 2.6 Mixed parts: shell where thin, solid where thick (PR 3, MIXED-1..6, SHLINK-4, placeholders)

A part that is mostly thin walls with some thick regions (a boss, a pad, a mounting block, a flange, cast corners)
was meshed fully solid. Now its thin walls are shelled, only its thick regions stay solid, and the two are joined so
loads and moments pass. Code: `cad/mixed_split.py`, `engines/mixed_links.py`, `engines/mixed_mesh.py`,
`engines/mixed_run.py`.

**What industry does.** Femap, HyperMesh, ANSA and Abaqus all mesh such parts mixed and join shell to solid with a
constraint that carries moment: Abaqus `*SHELL TO SOLID COUPLING` (each shell edge node spread over the solid face
nodes within half a wall of it), Nastran RBE3 or RSSCON, Ansys shell-solid assembly contact (MPC). Each says the
same thing about where the joint goes: away from the junction, where the wall's stress is a plate's again. The 3D
stress near a junction dies out within about one wall (Saint-Venant); one to two walls is the usual choice.

**MIXED-1, the thick core.** Each shell wall's slab (its sewn mid-face thickened to its wall, half each side, 2%
over) is taken from the body. What is left and holds a face the extraction called thick or solid is a thick core.
What is left and holds none is junction metal a shell model leaves out (fillets, the corner of an L); over 2% of
the body's volume of it means a wall was missed, and the split is not used.

**MIXED-2, the cut.** Each core is grown CUT_WALLS = 2 of the thickest wall running into it, in every direction (OCC's
3D offset), and the body's metal inside the grown core is the solid region. Inside a wall the grown core's side is
the wall's own cross-section, square to it: a straight edge's offset is a plane, a boss's circle a cylinder of
radius r + 2t, a tube's end a plane across the tube. Each mid-face is trimmed by the same grown core, so the shell
edge lies on the cut face, along the wall's middle. No list of shapes: a distance from the thick region.

**MIXED-3, accounted for.** The solid region's volume plus each trimmed mid-face's area times its wall must hold the
body's volume within the shell check's band (0.85 to 1.10). On the seven generated parts: 1.000 to 1.016.

**SHLINK-4, the join.** CalculiX takes no shell rotation in an `*EQUATION`: tied that way (each shell edge node's
rotation to the least-squares rigid motion of the cut face, the RBE3 / Abaqus form) a half-shell, half-solid
cantilever was still a hinge (tip 10^8 times too far), with or without a moment put on the nodes to make CalculiX
knot them. So the join is the form CalculiX solves, written the same in every solver (SHLINK-1's idea, carried to
tetrahedra):
- the shell runs one wall on past the cut face into the solid (one row of overlap quads, group `SHLINK_overlap`);
- its row on the cut face is tied, translations only, to the cut face's nodes; its inner row to the solid's nodes
  in a band round it (half a wall each way along the shell, inside the wall);
- each tie is the weighted least-squares LINEAR field through those nodes read at the shell node (weights: the
  node's area share times the row's hat function). A plain weighted mean is linear-exact only where its weights
  centre on the node: at a row's end node it sat a third of an element inward, and the in-plane bend of a
  cantilever crossed short (21% stiff). The linear fit reads any linear field exactly (a unit test holds it).

The moment crosses as the couple of the two rows, a wall apart, each spread over the wall's whole section.
Measured on a 2 mm, 200 x 20 cantilever, half C3D10 and half S4, against the all-solid C3D10 model: out-of-plane
bend 0.03% (pure shell 0.2%), in-plane bend 0.2%, tension 0.1%; the same with one tetrahedron through the wall.
The solid side keeps the FEA path's own rule of at least two quadratic elements through every wall (PLAN-fea-3),
so the stub of wall in the solid region is meshed as any wall is.

Per solver: CalculiX and Abaqus `*EQUATION`, Nastran MPC (set 1; the deck says "add MPC = 1"), Ansys CE. The solid
region is C3D10 / CTETRA (10 nodes) / SOLID187 in the same deck as the shells, numbered after them. Only CalculiX
solves for real; the others are read back and labelled "not yet tested in the real solver" as every deck is.

**MIXED-4, Auto.** Auto recommends Mixed when the part is one body the shell check calls mixed, its mid-surfaces meet
everywhere (no open edge), and its thin walls hold at least half its metal. Under that, the joins cost more than
the shell saves, and the part is meshed solid. Several bodies are meshed solid in this version.

**MIXED-6, the words.** "Thin walls meshed as shell (78% of the part, 2 mm thick); 3 thick regions kept solid and
joined to them."

**The solid region's mesh** is the FEA path's own: the region is staged in metres and measured, planned and meshed by
`fea_driver.measure_staged` / `choose_plan` and `gmsh/fea_adapter.mesh` (no AI pass and no ladder in this version).

**MIXED-1b, rims are no cores.** A left-over piece thinner than half the thinnest wall (twice its volume over its
area) is a rim the slabs left, not a thick region: a 4 mm plate whose hole bores were read thick left a 0.6 mm ring
round each hole, and growing those crashed OCC. It counts as left-over metal, as a junction's does.

**MIXED-2b, repeatable.** OCC's offset hands back the same faces, surfaces and curves (bit for bit, as sets) in a
different order each run (it orders by memory address). `canonical()` stores the grown core's solids, shells,
faces, wires and edges again in an order read from exact geometric keys (an edge's end points and middle point; a
face's edges and the surface point at its parameters' middle), so every Boolean on it and both meshes repeat.

**MIXED-2c, cores that grow.** A concave round tighter than the growth turns inside out in OCC's plain offset (no
solid). The core is then grown with the offset's complete intersection; then with its tight concave faces taken out
by OCC's defeaturing (grown that far the round is inside the grown core anyway); last with sharp joins, which reach
a little further round the core's edges, never less far. The plain offset is tried first, so every core it grew
grows as before.

**MIXED-7, mixed walls are panels.** A thick region stiffens the wall round it, so the wall's free modes bend in
half-waves a fraction of its width. In a mixed part every shell wall at least a tenth of its body's largest sheet
wide gets SHMESH-15's 20 elements across, however few walls wide it is: a 2 mm plate 18 walls wide round a boss
was meshed 7 across and its modes 7-12 came out up to 6.8% stiff of a converged solid; as a panel, within 0.9%.

**MIXED-8, walls too thick for shells stay solid (2 Nov, Areen's call).** The box and the casting were outside 5%
however fine the shell or wherever the cut: shell theory itself, on walls too short for their thickness. So each
shell wall is measured: its REACH is the farthest any point of it lies from its nearest support - a joint (an edge
where another wall meets it at an angle; a sheet's bend running on into its flange is no joint), a free edge the
body's metal goes on past (a thick region), or a stretch of the wall with metal past its faces (the wall running
through a corner column). A wall is a shell only if it reaches at least 10 of its walls (`REACH_WALLS`); fewer, and
it is kept solid with the thick regions (its mid-face marks it as a core), the rest still shell. If the walls left
as shell hold under half the part's metal (MIXED-4's share), or none is left, the part is meshed solid, said so:
"Walls too thick for shells: 5 of its 5 thin walls reach less than 10 times their thickness from the nearest joint or
thick region (the shortest, 4 mm thick, reaches 19.8 mm: 4.9 times). ..."

**Since SHELL-23 (10 Nov) MIXED-8 is advice, not a gate** (`engines/mixed_run.REACH_GATE = False`). A plate the extraction calls a shell is meshed as shell in the mixed mesh too, however short: a gusset or a rolled flange is never kept solid with the thick regions. The reach is still measured and the short walls named: "... 5 walls reach under 10 times their thickness from a joint: shell theory is less exact there." The measurements below are why the line is said; the rule as it was is one constant away. The box, the casting and the stiffened panel are meshed shell and solid again (as before 2 Nov), said so.

Why 10, and why the reach. 10 is the shell check's own 1/10 (SHELL-2: Mindlin shells hold to t / L of about 1/10;
plate texts put the thin / thick plate line at a span of 8 to 10 thicknesses). SHELL-2 reads L as the square root of
the whole sheet's area, which called the box's five walls one sheet 30 walls across. The reach is the length a wall
really bends over, and where the idealisation's error sits: a shell wall bends from the middle of its joint, half
the other wall's thickness from where the solid wall starts to bend, and the 3D stress round a joint reaches about a
wall into it - both a fixed length, so a share of the reach that grows as the reach shortens. The accuracy table
says the same: every part that read within 5% of a converged solid has all its walls at 11 walls or more (L
bracket 11 and 25, plate with a boss 26, tube 40, strip 99; failure-map plates 23 to 51); the two outside have walls
of 3.5 to 6.7 (box 4.9 to 6.7, back wall +23%; casting 3.5 to 6.7, mode 12 -6.8%). Not fitted to a part: the line is
the shell check's own.

What it costs. The stiffened panel's 3 mm walls reach 7.8 (plate, from its stiffeners) to 8.3 walls (the 25 mm
stiffeners), so it is meshed solid too, though it read within 3% mixed - even pushed sideways at a stiffener's tip,
where a short wall's bend is worst (+3.0%; its plate +0.5%). The rule is cautious there: it costs that panel the
smaller mesh (33,179 elements against 167,056), never accuracy. Training part sh0025 (a tray: 3 mm side walls
reaching 2 to 3 walls, a 4 mm one 9) had read within 3.2% on its free modes, which its floor carries; the rule keeps
the floor as shell and the walls solid, but the core grown round those walls does not grow (OCC's offset), so it is
meshed solid with both reasons said. sh0052 keeps one 5 mm flange solid (5.3 walls) and still mixes, modes within
1.2% (0.8% before). A plate with a lip too thick for shells (`plate_lip_block` in the accuracy lane: 2 mm, the 12 mm
lip kept solid with the block) reads within 0.3% on its statics.

Code: `cad/mixed_split.wall_reach` (a fixed grid over each mid-face, exact distances to points along the supports:
deterministic), `engines/mixed_run.too_thick` / `reach_of` / `kept_solid`. `recommend(record, reach)` reads MIXED-8
when it is given the reach; the stage (#377) measures it with `reach_of` before offering Mixed.

(The casting's wall / 3 truth ran past CalculiX's one-hour limit on one thread; its truth is wall / 2.5, 254,288
elements, which the product's all-solid mesh matches within 0.6% on every mode and 0.1% on the statics.)

**Proof (2 Nov, lab VM, CalculiX 2.21 on one thread).** The CI accuracy lane's parts against a CONVERGED all-solid
truth: a uniform C3D10 mesh of wall / 3 everywhere (24 a turn on curves; `MIXED_TRUTH_WALLS=3`, too big for a
runner). The product's own all-solid mesh is within 0.25% of it on every mode, so CI's comparison against it holds.
Static displacements and stress are read AT the probe point (a weighted linear fit over the directions the nodes
span, within three local node spacings), and each load is shared so it acts at the loaded face's middle (equal
nodal shares put it where a graded mesh's nodes are: the casting's all-solid mesh put the block's load 8.7 mm
nearer the clamp and read 11% stiff); modes 7-12 free-free:

| part | elements, mixed / all-solid | displacements | stress | modes 7-12 |
|---|---|---|---|---|
| cantilever strip in a block | 13,367 / 55,600 | tip -0.2%; in-plane -1.2%; stretch -0.2% | | -0.1 to +0.7% |
| plate with a thick boss | 13,320 / 55,276 | -0.2%, -0.4% | | +0.1 to +0.8% |
| L bracket with a thick pad | 22,279 / 67,691 | +0.9%, +1.0% | +0.1% | -1.2 to -0.6% |
| stiffened panel with a thick mount | 33,179 / 167,056 | +0.2%, +0.1% | +0.6% | -2.7 to -0.7% (before MIXED-8; now meshed solid, walls 7.8 to 8.3) |
| tube with a thick flange | 31,038 / 113,227 | end -2.2%, middle -3.1% | -0.7% | +0.2 to +0.5% |
| casting with ribs and a block (truth wall / 2.5) | 36,249 / 127,221 | -0.6%, +0.2% | -3.3% | -3.1 to -0.2%; mode 12 -6.8% (open) (before MIXED-8; now meshed solid, walls 3.5 to 6.7) |
| box with thick corners | 45,954 / 63,586 | column top -1.4%; back wall +23.2% (open) | +3.0% | -4.0 to -1.4%; mode 11 -5.5% (open) (before MIXED-8; now meshed solid, walls 4.9 to 6.7) |
| plate with a lip too thick for shells and a block (MIXED-8: lip kept solid; truth wall / 2, 209,256) | 55,127 / 206,155 | block -0.2%, plate -0.2%, lip top -0.2% | -0.2% | -0.8 to +0.4% |

The flanged tube's old -15.7% stress and +5.5% middle were the readings, not the mesh: the stress was a plane fit
through a fixed count of nodes, and on a 15-degree faceted wall the band keeps one line of nodes, so it reached
44 mm along the wall into the flange's junction (7.16 MPa; the shell node beside the point read 8.56, beam theory
8.55); the middle was the mean of four nodes sitting 3 mm above the box's middle, where the deflection rises 2.5%
a millimetre. Read at the point, the tube is within 3.1% everywhere.

Closed by MIXED-8 (both now meshed solid), as it was explained: the box and the casting's 12th mode. Both are as far from the converged truth as from the product's
own solid mesh, and are unchanged by a shell mesh twice as fine (-0.7%) or a cut 3 walls in (0.0%): the shell
idealisation itself. The box's 4 mm walls are 10 walls tall: a shell wall bends from the bottom's mid-plane, 2 mm
lower than the solid wall's inner corner, so it is (38 / 36)^3 = 1.18 times as flexible out of plane - the back
wall's +23.2%. The casting's ribs are 20 mm by 3 mm (6.7 walls tall), the same thick-shell regime. Keeping the junction metal solid as well (grown 2 walls) made 140,094 tetrahedra, more than the whole
all-solid mesh (63,586): such a box gains nothing from a mixed mesh.

Training parts that mix (sh0004/12/23/24/25/28/52/53/58/59/88/89) and the failure map's 20 "mixed" cases, against the
product's all-solid mesh (modes 7-12) and run twice (decks hashed): see the PR. Repeat: every mixed deck made is
byte-identical in two processes - the 7 accuracy parts and the training parts that mix.

## 3. Meshing (PR 2)

**Engine.** gmsh on the sewn mid-surface: Frontal-Delaunay (algorithm 6) plus Blossom recombination, and quad-dominant output. (Frontal-Delaunay for quads, algorithm 8, left faces of B-spline mid-surfaces up to 7 times coarser than their size, with long thin quads past Nastran's limits; QuadQS, algorithm 11, made 3 to 5 times the elements asked for.) If gmsh leaves too many triangles on a family of parts, Salome SMESH (quadrangle mapping, LGPL) is the second engine. The system recommends one, the user picks, and the plan is engine-neutral as on the FEA path.

**Element size rules** (SHMESH-1..7, placeholders). Each is general and measured:

1. **Base:** the coarsest the sheet allows. At most 1/10 of the sheet's size L, and at least 4 elements across every mid-face's narrowest width.
2. **Floor:** no element smaller than half the wall. Smaller than that, a shell is the wrong element. Where a rule asks for less, the part is meshed at the floor with a plain note.
3. **Holes and cut-outs:** 6 elements per quarter-turn (24 around), as PLAN-fea-4. Each arc is refined to its own radius and grown from there; the face's straight edges and wider arcs keep the face's size. (Refining every edge of a face to its smallest hole's size filled a 0.5 x 1.2 m bulkhead plate with 329,000 elements; it now takes 3,100 and 1 second.) Plus one ring of quads round the hole (a washer) and at least 2 element rows between a hole and a free edge.
4. **Curvature:** at most 15 degrees per element on curved mid-faces (6 per quarter-turn), and at least 3 elements across a bend.
5. **Small features:** at least 3 elements across any flange, strip or web.
6. **Growth:** 1.3 at most, as PLAN-fea-6. Held by grading fields from every finer face's edges and every refined arc, each growing up to the largest size in the part, never capped below it. gmsh's own spreading of edge sizes into a face is off: it carried a 2.5 mm bend strip's size over the whole of a 1.7 m flange (800,000 elements, over 20 minutes; now 50,000 in 25 s).
7. **Junction lines:** always meshed with shared nodes. Both sides use the same seeds.

**Sizes for modal accuracy (SHMESH-15, 30 Oct).** A free-free modal check of 14 corpus parts (CalculiX S4 shells against C3D10 solids of the CAD, modes 7 to 12) found the rules above too coarse: 5 parts were 10 to 23% too stiff. A grid study over the base divisions, the elements across, the curve sizes and a panel threshold (`~/shell_lab/modal/g1` to `g11`) showed that one thing matters: the elements across a wide face. The base divisions and the curve sizes changed nothing. 16 across everywhere put all 14 parts within 4.7%, for 2.3 times the elements. The rule kept:

- **A panel gets 20 elements across.** A panel is a face at least 20 walls wide and at least a tenth of the size of its body's largest sheet. It bends across its own width in the part's first modes.
- **Narrower faces keep 4 across.** These are bends, flanges and strips. They do not bend across in those modes.
- **Result:** all 14 parts within 4.1% of the solid (the worst, sh0055, is 4% soft at every size: the shell model's own difference, not its mesh), for 1.1 times the elements.
- **Why "a tenth of the body's largest sheet".** Without it, a corrugated roof sheet's 23 mm flanks (48 walls of 0.49 mm) were panels: 399,000 elements instead of 38,700. The 2 m sheet bends as a whole long before a flank bends across.

**Mapped bends (SHMESH-16, 30 Oct).** A narrow curved strip with four sides is meshed as a mapped grid of quads (gmsh transfinite):

- a bend, or a tube's rounded corner;
- its two long sides alike within 5%, and its ends alike;
- the long sides at least 1.5 times the ends;
- under 20 walls across.

Across, it keeps its planned size. Along, it takes the size of the faces beside it, at most 5 times the size across (Femap's aspect target). Its neighbours grade from its long sides at that size.

**Every count is even.** With an odd count on a strip's side, Blossom left hundreds of triangles in the face next to it: 485 on one sheet-metal flange, 0 with even counts. A count another strip has already set is kept; a strip that cannot agree stays free. Flat strips are not mapped: a lone plate stretched 5 times along was 5 times too coarse (sh0086: 45 elements, 11% stiff).

On the corpus, a floor-sized bend no longer fills its length with floor-sized elements:

- sh0107's 400 tube corners: 1.5 million elements and over 20 minutes before, now 213,000 elements in 201 s;
- the welded tube frames (sh0106, sh0109 to sh0113): 2 to 8 times fewer elements, and 2 to 6 times faster.

**The sliver merge never folds a corner.** SHMESH-12's guard checked only each element's mean normal. A quad can keep its normal with one corner turned back, and 3 such quads made sh0021's mesh invalid once the panels were finer. A merge is now undone when any corner of an element turns against its old normal. Cost, still open: sh0083 keeps slivers the merge used to close, and 3.6% of its elements are past a Nastran limit (0.8% before).

**Quality** (SHMESH-8, verdict by limits as VER-1..4).

- **Blocker:** a valid mesh only. No inverted or zero-area element, a positive Jacobian at every corner, and consistent normals in each sheet.
- **Notes:** everything else, judged against Nastran's GEOMCHECK defaults and the Femap or industry targets below.

| Measure | Nastran GEOMCHECK default (CQUAD4 / CTRIA3) | Femap / industry target |
|---|---|---|
| aspect ratio | IAR 100 (warn) | ≤ 5 |
| warp | Q4_WARP 0.05, the warping coefficient 2H / (D1 + D2) | ≤ 10 degrees between the two halves (notes) |
| skew | Q4_SKEW 30° min, T3_SKEW 10° min | ≥ 45° (quad) |
| taper | Q4_TAPER 0.5 | ≤ 0.5 |
| interior angle | Q4_IAMIN 30°, Q4_IAMAX 150°, T3_IAMAX 160° | quad 45 to 135°, tria 20 to 120° |
| Jacobian | (must be positive) | ≥ 0.6 |
| triangle share | | ≤ 5% of elements |

The GEOMCHECK figures are as we read MSC's Quick Reference Guide; the warping coefficient's formula is Altair's statement of Nastran's (H: the corners' height off the mean plane, D1 and D2 the diagonals). Held as an angle of 5 degrees (as first written here) it was 4.5 times stricter than the solver: a square whose corners stand 0.05 of a diagonal off its plane is 22.6 degrees.

**Every delivered element inside Nastran's own limits** (SHMESH-10..13, placeholders). After gmsh, in this order:

10. **Cut:** a quad past any GEOMCHECK limit becomes two triangles, on the diagonal whose triangles are better (fewer past a limit, then the larger smallest angle). A triangle has no warp or taper.
11. **Seams:** every closed sewn face (a full ring, a torus) is cut at its seams before meshing and fused back conformal. A face gmsh still cannot mesh as a periodic surface is re-made as a B-spline (the same surface) and meshed again.
12. **Slivers:** an element edge under the floor and either under a fifth of the longest edge of every element on it or under a fifth of the floor is a CAD sliver; its ends become one node (the one most faces meet at stays put). Run before the cut and once after it (a quad folded nearly flat cuts into two slivers along its short diagonal, which then closes).
13. **Swaps:** a triangle past a limit swaps its longest edge with the triangle across it (same face, never a face's own edge) when both come out better.
14. **Relax first:** before any quad is cut, a node inside a flat face (on no CAD edge) of an element past a limit moves to the middle of its neighbours when that leaves fewer of its elements past a limit and none turned over (a tie goes to the larger smallest angle). Nodes sharing no element move together; at most 6 passes. The middle of coplanar points stays in their plane, so the node stays on its face. This halves the cut quads on flat sheet parts.
14b. **Relax, second pass (30 Oct):** what the middle left past a limit is tried again with two more points per node: where each quad round it would be a parallelogram (b + d - c, averaged), and half-way to it. Tried in one pass with the middle, the parallelogram moves mended quads and left sliver triangles the middle had mended (sh0039: one triangle past T3_SKEW). A past triangle outweighs every quad round a node (a cut quad mostly gives good triangles; a past triangle stays past), and a quad that no cut can mend counts as a triangle. After the cut, one more pass for the triangles the cut left past, where no quad may come out past a limit.
14c. **Relax on curved faces (30 Oct, 11th helper):** the nodes inside a cylinder, cone or any curved face move too; each point tried is laid back onto the face's own surface (its closest point) before it is judged. sh0007's slotted cylinder: 11.3% to 7.2% triangles; sh0004: 5.8% to 2.7%. Every node stays on its surface (a test holds a relaxed cylinder's nodes on its radius to 1e-6 mm).
11b. **A boundary gmsh cannot recover (30 Oct, 11th helper):** gmsh's 2D mesher stopped on a ship's 3 m hull cylinder with 25 mm slots (sh0048: "impossible to recover the edge", algorithms 6, 1 and 5 alike). The same face scaled down 10 times meshed, and so did the same surface made a B-spline at full size: gmsh's handling of the large periodic analytic surface, not the face's shape. The face gmsh was meshing when it stopped (read from its log) is re-made as a B-spline and the part meshed again, as for a periodic failure (SHMESH-11). Only failing faces change, and with each one every sewn face on the same surface (the hull's four slotted halves each stopped gmsh in turn: four whole re-meshes). Still open: sh0048 meshed that way runs past an hour on the lab machine, so it does not yet finish inside the sweep's 20 minutes.
11c. **A loop gmsh cannot close (30 Oct, 12th helper):** "the 1D mesh seems not to be forming a closed loop" on an analytic or trimmed face (a tee's run, a mitred bend's pieces, a nozzle's saddle) is handled as 11b: that face is re-made as a B-spline and the part meshed again. A face gmsh already names a B-spline is not re-made again: the old guard compared "BSplineSurface" with gmsh's "BSpline surface", never matched, and re-made a failing B-spline face up to 16 times. At most 4 faces are re-made for loops in one mesh; past that it stops in words (fresh fr017, a PVC tee: its faces failed one after another, 14 were re-made, then gmsh hung; now it stops in 4 s).
11d. **Closed faces that will not fuse back:** the pieces of the closed faces are fused back exactly, then with a fuzzy value of twice the surface's own largest tolerance; if they still will not fuse (a 45 degree cone's pieces), the surface is meshed as sewn, said in the notes.
11e. **A budget on re-made faces:** no face is re-made once the meshes so far have taken 300 s; the mesh stops and says why.
11g. **Offset faces as B-splines:** faces on OCC offset surfaces (a drafted wall's mid-face) are re-made as B-splines before the closed faces are cut. Cut at the seam as offsets, a printed cone's halves came out invalid, the fuse dropped them, and the mesh had 0 elements (fresh fr018; now 338 elements, valid).
20. **A plan too big stops before gmsh (SHMESH-20):** a plan asking for over 1.5 million elements (each face's area over its size squared, summed) fails at once, in words. gmsh makes about 1,000 elements a second here, and its counts run 0.4 to 3.3 times the plan's on the corpus. sh0048 (a ship's slotted hull, about 3.2 million planned, over an hour of gmsh) now stops in seconds and the solid mesh is delivered.
21. **A deadline on every shell mesh (SHMESH-21):** the mesh runs in a forked child stopped after 15 minutes (gmsh can hang in one 2D mesh with no way back to Python). Never reached on the corpus.
Repeatability (30 Oct, 11th helper): 65 of 66 meshed training files byte-identical run to run. sh0038 is not: gmsh hands back the same nodes but its quad recombination differs between runs (2 quads, 2 triangles), and it does at the 10th helper's head too; open.
12b. **Sliver fold guard, local (30 Oct):** a merge that folds an element's corner in is undone at that element's own moved nodes, not over its whole cluster; the last round still undoes whole clusters. Undoing whole clusters (as added on 29 Oct for sh0021) kept 2,600 sliver edges of sh0083 and doubled its triangles (8% to 20%). sh0021 stays valid.
17. **Pairs (SHMESH-17):** two triangles of one face across an edge theirs alone become one quad when that quad is valid and inside every Nastran quad limit; best pairs first.
18. **Walks (SHMESH-18):** a triangle walks through at most 6 quads of its face to the nearest other triangle: each step re-cuts the triangle and the quad ahead (a pentagon) into a quad and a triangle that touches the next element; the last step joins the two triangles. A walk is kept only when every quad it leaves is valid, turns as the face does and is inside every limit; nodes never move and the outline stays.
19. **Even boundaries (SHMESH-19):** Blossom pairs every triangle of a face only when the face's boundary has an even number of segments. After the 1D mesh, each odd face gets one segment more on one of its own free curves (the most evenly divided, then the most segments; never one whose segments would come out under the floor - a 0.7 mm spline cut in 3 left slivers), else on a curve shared with another odd face. sh0019: 13.5% triangles to 0.7%.
20. **Speed for very large parts:** one size field per size, not per face; past 48 grading sizes, sizes are taken down to quarter-octave steps (finer, never coarser); gmsh integrates sizes along curves to 1e-3 (its 1e-9 held a ship's 1D mesh, 24,000 curves, past 30 minutes; counts move under 0.2%).

What was tried and dropped for the triangle share (29 Oct, 8th helper): Frontal-Delaunay-for-quads on flat faces only (better on some parts, worse on as many); QuadQS (3 to 5 times slower, no fewer triangles); gmsh's transfinite-automatic (maps wide faces from a narrow neighbour's count: 15 times the elements); mapping only floor-width four-sided strips (left skewed triangles at their ends); meshing a strip narrower than the floor as one compound with its neighbour (gmsh crashed on one part - a whole-process crash). gmsh's Laplace smoothing helped less than rule 14. (30 Oct) Mapped flat strips (no stretch) did not help either: most flat strips have more than four sides. (30 Oct, 10th helper) gmsh's packing of parallelograms (algorithm 9) on flat faces: 2 to 4% triangles on holed plates, but it ignored the sizes on one part (2.3 times the elements, sh0059), ran past 20 minutes on B-spline parts and failed boundary recovery on the ship; dropped. A tri-plus-quad re-cut for past triangles mended 2 elements corpus-wide; dropped. gmsh's full-quad recombination needs every curve even and its sub-quads were cut more; Blossom options (minimum quality, topology passes, recombine angle) changed nothing.

**Normals.** Every sheet's normals point one way. They point from face B toward face A of each pair, and outward on closed sections. Offsets and pressures in the user's solver then mean the same thing everywhere.

## 4. Properties and groups (PR 2)

- **One PSHELL per sheet thickness, per part.** Part_3 at 6 mm becomes "Part_3_t6mm". Material: exactly as today's solid decks - mesh only, no material: PSHELL k names MAT1 k (Abaqus: MATERIAL=MAT_k) and the deck header says "add MAT1 k" (decided 29 Oct). The textbook tests add their own material.
- **Groups:** one element group per original CAD face pair, named after the CAD faces (`Part_3_Face_12+Face_15`), and one per part.
- **Joints are marked:**
  - shared-node lines at sewn junctions get a node group per junction;
  - ties exist only where there is a gap, as PLAN-fea-18;
  - solid links (mixed parts) get a node group per link.

**Walls that vary, and offset mid-planes (30 Oct, 11th helper).**
- A mid-face whose wall varies (SHELL-9b) gives each of its nodes its own wall: a plane through the 8 wall readings nearest the node, kept within the readings' range. Its elements are written per corner (CQUAD4 / CTRIA3 T1 to T4, `*NODAL THICKNESS` in a section of their own, an Ansys section per element at its corners' mean). A varying mid-face with no readings is never meshed at one wall: the mesh stops and says why.
- A plate snapped onto a thicker neighbour's mid-plane (SHELL-7: steps, laps) carries its true mid-plane's distance from its nodes along each element's normal: Nastran ZOFFS, Abaqus and CalculiX `OFFSET` (in walls, the nodes off the mid-surface), Ansys `SECOFFSET,USER`. Offsets and varying walls each get their own property (`..._z0p25`, `..._varies`). A varying wall is sized by its thinnest point (SHMESH-1/2/15 read that wall). Synthetic failure map, free-free modes 7 to 12 against the solid: a 2-degree wedge +67.5% to +2.8%, a printed tapered cone +146.7% to +5.9%; steps and laps with offsets unchanged at 3 to 4%.

## 5. Export (PR 2; Ansys in PR 2 too)

**Nastran bulk data (.bdf), first-class for FEMAP.**
- Cards: GRID, CQUAD4 and CTRIA3 (T1 to T4 corner thicknesses when the wall varies), PSHELL (MID1 = MID2 = MID3, 12I/T³ = 1, TS/T = 5/6), MAT1.
- Names: Femap's own comment form for property and material names (`$ Femap Property 1 : Part_3_t6mm`). Our ingest already reads that form back (`cad/ingest/readers.py`).
- **Groups:** Nastran bulk data has no group card. We write each group as a named comment block of element ranges, and read it back in our tests.
- **The FEMAP-native route is a neutral file (.neu)**, whose group block is public. We offer it as soon as the prospect confirms the import in their FEMAP. Until then the BDF says "not yet tested in the real solver", as the solid writers do.

**Abaqus and CalculiX (.inp).**
- Elements: `*ELEMENT, TYPE=S4R` and `S3` for Abaqus; **`S4` and `S3` for CalculiX** (29 Oct). CalculiX expands shells into solids: S4R into one layer of C3D8R, which has next to no bending stiffness (the 200 x 20 x 2 cantilever bent 96 mm for 9.4); S4 into C3D8I, 0.6% off.
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
  - **What lies past an edge still open at the end, read on the part (SHELL-21, 10 Nov; section 2.4.3).** With a shell wall ahead within the reach the edge is open, always. With none: an edge whose metal stops within 2 walls, inside the wall's own slab all the way, is the wall's own end, a real boundary (`rim_edges`); in a mixed body an edge whose metal grows thicker than the wall is a solid link (`bulk_link_edges`). Read in the body's last census only: the joins and the strips are made and judged with these edges open, as before.
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
   - Mixed parts: Auto reads "Shell + solid" (shell where thin, solid where thick) only when MIXED-4 and MIXED-8 pass; otherwise Solid, with the reason (the land PR, 7 Nov).
   - The user can switch.

**A user's explicit choice is never overridden.** If they say Shell but the part has solid regions, the stage says which regions stay solid and why.

**Stage:**
- The extracted mid-surfaces are drawn over the part, coloured by thickness. We use a muted sequential map with a legend in mm.
- A list of what could not be shelled, in plain words: "Boss near the centre stays solid: it is 30 mm thick." "Two plates do not meet at 1 place: shown in red."
- Open edges show as red marks at their places.

The system recommends and the user picks. Nothing here blocks meshing. A part we cannot shell is meshed solid, with the reason.

**As built (PR 4 draft, rule numbers SHSTAGE-1..6 are placeholders until merge):**
- SHSTAGE-1, words: `application/purpose_map.mesh_as_of` is the word table; `application/mesh_as` asks the model only for words the table cannot read (a paraphrase, both kinds, a word taken back) and keeps its answer only when it is shell, solid or none. Read once, in the FEA naming step, and stored with the check's facts. Shell-model words (CQUAD4, Femap, mid-surface, plate model, shell mesh) are FEA words too, so they take the FEA path with no question.
- SHSTAGE-2, the shell check: started with the FEA measurement, run on the geometry-check queue in a child process (`cad/shell_preview`, limit 15 min). The stage opens at once and says the walls are being measured.
- SHSTAGE-3, Auto: Shell when every body is a shell, the plates meet everywhere (no open edge) and the thin walls are at least 90% of the surface. A part the extraction calls mixed is "Shell + solid" only when `engines/mixed_run.recommend(record, reach)` says so with EVERY shell wall's reach measured (`cad/shell_preview.mixed_auto`, in the check's child process): one body, no open edge, thin walls at least half the metal, and the walls too thick for shells (MIXED-8) kept solid. A reach that cannot be measured is no Mixed. Otherwise Solid, with the reason ("Walls too thick for shells: ...", "Only 42% of this part is thin wall ..."). Mixed is Auto's only: there is no fourth button, so a part Auto does not call mixed is never meshed mixed (the box and the casting, +23% and -6.8% mixed, stay solid); Shell and Solid can still be picked.
- SHSTAGE-4, the stage: the mid-surface over the part (wall colours on a muted ramp, legend in mm), open edges in red, solid regions and open places tagged (S1.., O1..) and listed.
- SHSTAGE-5, Proceed settles it: the user's pick (a click, else their words) stands; Auto takes the recommendation, or Solid while still measuring. A triangle file, or a part with no thin wall, is meshed solid with the reason. Stored in `confirmed.json` (`fea_mesh_as`) and read into the run.
- SHSTAGE-6, the run: when Auto settled Shell + solid, the FEA driver runs `engines/mixed_run` the same way (child process, the staged solid, MIXED-5 cited) and the downloads offer mixed.bdf (first), mixed.inp, mixed_abaqus.inp and mixed.cdb; a part the run's own MIXED-8 check stops makes no mixed files and says why. When Shell was settled, the FEA driver makes the shell (`engines/shell_run`, child process, extraction on the staged solid) and the downloads offer shell.bdf (first), shell.inp, shell_abaqus.inp and shell.cdb. In this draft the solid mesh is still made beside it, so the result screen, the verdict and the 3D view are unchanged; a shell-only run needs those taught about shells.

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
