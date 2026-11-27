# Responsibility: Grow a prism stack on the conjugate interface, and say when it did not grow.
# Boundaries: it renders dictionaries, builds a shell and reads a log. It launches nothing; conjugate.build owns the chain.
# Collaborates with: cfmesh_conjugate/conjugate.py (which runs the stage) and plan.py (which carries the ask).
"""THE BOUNDARY LAYER PASS, and the reason it is a second pass rather than part of the mesh.

cfMesh cannot do this. `cartesianMesh` meshes a manifold domain and the conjugate interface does
not exist until `splitMeshRegions` has cut it, so there is no patch to extrude from while the
mesher is running. `snappyHexMesh` can grow layers on an existing patch with
`castellatedMesh false; snap false; addLayers true;`, which is what every measured run of this
part has used.

WHERE IT RUNS IN THE CHAIN. After the split and BEFORE `reconstructParMesh`, on the decomposed
mesh. That ordering is the whole economy of it: measured on the first customer plate the layer pass
is 2,521.87 s on 8 ranks against 10,038 s serial, and the reconstruct costs about 1,105 s whichever
side of it you pay.

HOW THE CASE IS BUILT, and why there is no `-region`. `snappyHexMesh` is handed a standalone
SINGLE-region case: the coolant's `polyMesh` is MOVED up out of `processor*/constant/fluid/` to
`processor*/constant/`, so `constant/polyMesh` exists and `constant/fluid/` does not. It is a
rename on one filesystem, which is why the measured chain prices this step at 2 s for 270 million
cells. Afterwards the layered mesh is moved back where `reconstructParMesh -region fluid` expects
it. THAT SECOND HALF IS UNMEASURED: the hand-built run left the coolant promoted and reconstructed
it as a standalone single-region mesh with `-noZero`, so no run has yet put a snappy-rewritten
polyMesh back under `constant/fluid/` and merged it per region. It is written with a guard at every
step for that reason.

THE ONE SETTING THAT DECIDES WHETHER ANY OF THIS WORKS IS NOT A LAYER SETTING.
`meshQualityControls/minVol` is an ABSOLUTE volume, and OpenFOAM ships it at 1e-13. A 22.0035 um
cube's face pyramids are (1/3)(22e-6)^2(11e-6) = 1.78e-15 m3, fifty-six times below that bar, so
on a microchannel mesh EVERY FACE OF A GEOMETRICALLY PERFECT MESH is flagged on length scale
alone. The break-even cell edge is about 84 um. Measured on a 20x20x10 blockMesh at 22.0 um with
addLayers only:

    faces with face pyramid volume < 1e-13 : 12800     (of 12800)
    Extruding 0 out of 1600 faces (0%)
    Added 0 out of 4800 cells (0%)
    Finished meshing with 12800 illegal faces
    checkMesh: Mesh OK
    exit code 0

A silent, total, zero-layer failure that exits 0 and passes checkMesh. It is the reason `complain`
exists and the reason no stage here may be judged by its return code. The ablation says
`minVol 1e-20` is necessary AND sufficient - adding `minTetQuality` or `minFaceWeight` changes
nothing - and that `minTetQuality -1e30` must NOT be set with it, because tet quality is
dimensionless and scale-invariant so it was never the blocker, and clearing it only switches off
inverted-cell detection.

AND ZERO LAYERS IS NOT THE ONLY WAY IT FAILS. The delivered mesh's own pass extruded 99.7653% of
the patch at iteration 0 and held 82,548 illegal faces, because the stack it was given did not fit
its background cell; ten repair iterations then dropped coverage to 90.7146% having removed 6.6%
of the failures, since thinning is the mover's only repair and thinning manufactures hairline
cells. So `complain` reads the achieved THICKNESS and the illegal-face count as well as the
coverage, and `fit_complaint` refuses that dict before anything runs.

NEVER SMOOTH THE NORMALS ON A STAIRCASE. The conjugate interface is a staircase of axis-aligned
faces, so smoothing its normals aims the extrusion diagonally across steps instead of along a
face. Raising `nSmoothSurfaceNormals` 1 to 6, `nSmoothThickness` 10 to 50 and `nSmoothNormals` 3
to 10 came out WORSE - 96.451% coverage at iteration 0 against 97.1958%. See SMOOTH_SURFACE_NORMALS
for the measurement that says ZEROING two of them is better still, and why that is not the default
here yet.

THE REGION'S OWN PORT PATCHES ARE LEFT UNNAMED. Pinning them at `nSurfaceLayers 0` measured 93.1%
coverage with 92 polyhedra and non-orthogonality 62.09; saying nothing about them and letting them
slide measured 100% and all-hex. Note that measurement was taken with the port patches ALREADY
CUT, and in this chain the ports are cut before the layer pass for exactly that reason.
"""
from __future__ import annotations

import re

#: The directory the single-region layer case is built in, beside the mesh.
LAYER_CASE = "_layers"
#: THE STAGE'S NAME, WHICH IS ALSO ITS LOG FILE, AND THAT IS WHY IT IS THE TOOL'S NAME.
#: `engines/mesher_stage` derives the line a customer is shown from the log's STEM, against a table
#: of tool names - so a stage called `addLayers` writes `addLayers.log`, matches nothing, and the
#: longest stage of the chain runs with no line at all. `snappyHexMesh` is in that table and maps
#: to "adding wall layers" already.
LAYER_STAGE = "snappyHexMesh"

#: How many prisms. Three on every measured run of this part.
N_SURFACE_LAYERS = 3

#: THE STACK GEOMETRY AS FRACTIONS OF THE BACKGROUND CELL, which is what makes it transfer to a
#: part that is not this one. The measured run used `firstLayerThickness 4.15e-06` against a
#: 25.1578 um background and `expansionRatio 1.2`, so the first layer is 0.164957 of a cell and the
#: whole stack 0.600444 of one; `geometry_for` reproduces 4.15e-06 at that cell exactly.
#: ...AND THEY ARE FRACTIONS OF THE CELL AT THE INTERFACE, NOT OF THE BACKGROUND. The layers grow
#: on the conjugate interface, whose cells are the background divided by the octree levels under
#: it - on the run plan 402.5570 / 2**4 = 25.1598 um. Sized against the background instead, the
#: first layer came out at 66.4053 um and the stack at 241.7153 um into 25.1598 um cells, and the
#: fit rule could not see it because it compared the stack against the same wrong cell: both scale
#: together, so the share was exactly 60.0% either way. The parameter is named `interface_cell_m`
#: for that reason.
FIRST_LAYER_FRACTION = 4.15e-06 / 25.1578e-06
EXPANSION_RATIO = 1.2
#: THE FLOOR UNDER THE WHOLE STACK, as a fraction of the stack. Below it snappyHexMesh unextrudes
#: the face instead of keeping what is left of it, so this is the one number standing between the
#: mover's thinning and a hairline cell - and it is in `addLayersControls`, where no relaxed
#: iteration can reach it.
#:
#: It used to be 3.0e-06 / 25.1578e-06 of a CELL - a fifth of the stack - chosen to sit just clear
#: of what `minFaceWeight 0.02` implies against a full-sized neighbour, which `fit_complaint` still
#: enforces as the lower bound. Only just legal turned out to be the problem: a fifth permits a
#: stack thinned five times over, which at expansion 1.2 is a first layer of 0.82 um in a 25 um
#: cell, and `displacementMedialAxis` reduces thickness at 177,132 nodes of this part at every
#: iteration with nothing but this underneath it.
#:
#: SEVEN TENTHS, AND AT SEVEN TENTHS THE MOVER FINISHES CLEAN. Swept on
#: two_scale_plate_ported at 2 across, 4 ranks, 3 layers, 506,298 interface faces, buffer on:
#:
#:   floor  coverage  illegal  det<0.001  concave  wt<0.05  vr<0.01  smallest cell  pass
#:   0.20   95.9350%        2      1,220   38,597    1,306        2    2.05349e-15   123s
#:   0.30   96.1035%       24        166   36,370      327       24    1.64614e-15   125s
#:   0.40   96.1341%       24         96   36,969      263       24    2.16820e-15   124s
#:   0.50   96.1501%       26         78   36,264      181       26    2.72103e-15   122s
#:   0.60   96.2431%       16         47   39,126      170       16    3.58330e-15   122s
#:   0.70   96.3032%        0         28   38,280      210        0    4.31474e-15    69s
#:   0.80   96.3006%        0         40   37,925      187        0    4.31474e-15    62s
#:
#: At 0.7 snappyHexMesh prints "Finished meshing without any errors" and its own report says
#: `faces on cells with determinant < 0.001 : 0`. The illegal-face trace runs 137, 19, 2, 0 and the
#: mover STOPS at iteration 4 of 10 with nothing left to repair. The fifth that shipped ran all ten
#: and never got below 2:
#:
#:     0.20   1608  278  162  88  94  82  74  56   2   2
#:     0.70    137   19    2   0
#:
#: That is where 44% off the layer pass comes from, and it is not a trade against quality but the
#: same cause: a floor that refuses a layer the mover cannot hold means it never has to repair one.
#: `Face volume ratio` also changes VERDICT, not just count - 0.0102547 against the 0.01 bar, 0
#: faces under it, where every lower floor failed that check.
#:
#: 0.8 is not better: the determinant count goes back up, 28 -> 40, and coverage stops improving.
#: 0.7 is a knee, not the edge of a trend.
#:
#: WHAT IT COSTS is 2,016 more concave cells, 36,264 -> 38,280. Thicker retained layers put more
#: prisms on the staircase's corners, and concave-by-face-planes is the mildest thing checkMesh
#: reports: no tolerance at all, so a prism on a stair corner is concave by construction.
#:
#: A FRACTION OF THE STACK AND NOT OF THE CELL, because the floor protects what is being grown and
#: has to scale with it; as a cell fraction it would mean a different thing at every layer count.
MIN_THICKNESS_OF_STACK = 0.8
#: RE-TUNED WITH THE OTHER TWO, not reversed. The floor sweep above chose 0.70 over 0.80 when the
#: floor was the only thing moving, and the two were within noise of each other there - 0 illegal
#: faces and 0 volume-ratio faces at both, coverage 96.3032 against 96.3006. With FEATURE_ANGLE
#: and SMOOTH_SURFACE_NORMALS in, 0.80 is what clears the determinant cells:
#:
#:     fa120 + s5, floor 0.70    concave 18,810   det 34   weight 43   FAILED 3   aspect 21.41
#:     fa120 + s5, floor 0.80    concave 26,664   det  0   weight  8   FAILED 2   aspect 16.96
#:
#: 0.70 is the concave minimum of the whole sweep and 0.80 is the better mesh: two failure modes
#: gone rather than one traded for another, interpolation-weight faces from 209 to 8, aspect ratio
#: halved, and concave still down 29% on the shipped dictionary. Concave-by-face-planes is the
#: mildest thing checkMesh reports - no tolerance at all, so a prism on a stair corner counts by
#: construction - and a cell with a bad determinant is a solver's problem.

#: What fraction of a background cell the stack may occupy. 0.600444 on the measured run, which
#: left a 10.052 um remnant and a 2.50x volume jump to the first full cell; the delivered run was
#: at 1.093 of a cell and spent ten iterations failing to recover from it.
MAX_STACK_FRACTION = 0.75

#: THE MOST LAYERS THAT CAN EVER FIT, derived from the two constants above rather than asserted:
#: at EXPANSION_RATIO 1.2 the stack is 0.165, 0.363, 0.600, 0.885 of a cell for one to four layers,
#: so four is over MAX_STACK_FRACTION whatever the part. A deployment asking for more is a typo and
#: `determinism.conjugate_layers` refuses it at startup rather than once per job.
def max_layers() -> int:
    """How many layers could fit a cell at this expansion ratio, at most."""
    n = 0
    while n < 64:
        if (stack_thickness_m(first_layer_m=FIRST_LAYER_FRACTION, expansion=EXPANSION_RATIO,
                              n_layers=n + 1) > MAX_STACK_FRACTION):
            return n
        n += 1
    return n


#: HOW MUCH MEMORY HEADROOM A LAYER PASS BUYS, AND IT IS A GUESS THAT SAYS SO.
#:
#: The cell count is priced (`cells_multiplier`), but the PASS's own transient peak is not.
#: `budget.PEAK_BYTES_PER_CELL` was fitted on three cfMesh builds with no layer stage in them, and
#: the only run of this pass that exists was on a 755 GB machine, so it has never been near a
#: limit. What it is protecting against is the worst failure in this project's record: when the
#: kernel OOM-kills ONE rank of an mpirun the others do not exit - they wait in a collective that
#: will never complete, and OpenMPI busy-waits, so each survivor spins at full CPU holding its
#: whole share of memory until something reaps it. On 2026-11-07 that left 16 processes and 381.9
#: GB resident for six hours and killed an unrelated stage three stages later with rc=137.
#:
#: 1.25 is one tier of the machine ladder at the sizes this part lands on, which is the smallest
#: increment that buys anything. DROP IT once a layer pass has been measured on a right-sized box
#: - and the first run of this stage is what measures it.
LAYER_PEAK_MARGIN = 1.25

#: `meshQualityControls/minVol` AS A FRACTION OF THE INTERFACE CELL, and the module docstring is
#: about this one line.
#:
#: It is an ABSOLUTE volume and OpenFOAM ships it at 1e-13, which on a 25 um mesh condemns every
#: face of a geometrically perfect mesh - that is the silent total failure the docstring describes,
#: and it is why this carried a flat 1e-20 instead. But 1e-20 IS NOT A SMALLER FLOOR, IT IS NO
#: FLOOR. The thinnest legitimate face pyramid in a three-layer stack on a 25.1578 um cell is
#: (1/3)(25.1578e-6 x 4.15e-6)(25.1578e-6 / 2) = 4.38e-16 m3, four orders of magnitude above it.
#: The 155 M plate's smallest cell came back at 3.74e-17 m3 - a layer 0.059 um thick in a 25 um
#: cell - and nothing in the dictionary objected to it.
#:
#: A thousandth of the interface cell's own volume is about 30x under the thinnest legitimate
#: pyramid and about 170x over a hairline, and it reproduces the shipped behaviour where the
#: shipped behaviour was right: on the 22.0035 um blockMesh of the ablation it is 1.07e-17 against
#: a real pyramid of 1.78e-15, so layers still grow there. Scaling it is what makes it safe to
#: enforce during the relaxed iterations as well as the strict ones.
MIN_VOL_FRACTION = 1.0e-3


def min_vol_for(interface_cell_m: float) -> float:
    """The `minVol` floor for a mesh whose cells at the interface are this big."""
    cell = float(interface_cell_m)
    if not cell > 0:
        raise LayerError("minVol scales with the interface cell, so the cell has to be positive")
    return MIN_VOL_FRACTION * cell ** 3
#: Left at OpenFOAM's own value deliberately: tet quality is dimensionless, so it was never what
#: blocked the extrusion, and relaxing it would only switch off inverted-cell detection.
MIN_TET_QUALITY = 1e-30
#: CAPPED AT 2^31-1 BY THE LABEL SIZE, WHICH BINDS THE DICT ENTRY AND NOT ONLY THE MESH. The first
#: attempt set 4e9 and OpenFOAM exited with "Expected integral (int32), value out-of-range:
#: double 4e+09" after 689 s, 665 of them already spent reading the mesh.
MAX_GLOBAL_CELLS = 2_000_000_000
MAX_LOCAL_CELLS = 400_000_000

#: HOW FAR BACK FROM A TERMINATION THE MOVER STOPS EXTRUDING, and it is worth a tenth of the
#: bad cells.
#:
#: Where a layer stops is where the mesh is worst. Both of the mover's repairs - removing a layer,
#: thinning one - leave a face carrying three prisms against a face carrying none, and at 0 that
#: step is one face wide: a fifth-height cell against a full one is a volume ratio of five and an
#: interpolation weight at the floor. A buffer of 1 stops the extrusion one cell earlier all round
#: a termination, so the step is spread over two faces.
#:
#: Measured on two_scale_plate_ported at 2 across, 4 ranks, 3 layers, 506,298 interface faces,
#: against the 0 this shipped with:
#:
#:     coverage                           95.9121% -> 95.9350%
#:     illegal faces at the end                  8 -> 2
#:     concave cells                        42,702 -> 38,597
#:     faces under interpolation 0.05        1,452 -> 1,306
#:     worst interpolation weight         0.020620 -> 0.022364
#:     worst cell determinant          3.90492e-05 -> 4.94893e-05
#:     cells under determinant 0.001         1,256 -> 1,220
#:     layer pass                          123.7 s -> 123.0 s
#:
#: Better on every measure that moved, and not slower. `engines/snappy` carries 0 for the same
#: entry on a different kind of part and is not changed by this.
BUFFER_CELLS_NO_EXTRUDE = 1

#: The smoothing, and it is load-bearing at these values.
#:
#: THE TWO SMOOTHING KNOBS ARE NOT ONE KNOB, and the experiment they were set by moved both at
#: once. This block used to say "NEVER SMOOTH THE NORMALS ON A STAIRCASE" on the strength of a
#: variant that turned `nSmoothSurfaceNormals` AND `nSmoothNormals` off together and came back
#: better on six figures - faces in error 2,936 -> 2,724, interpolation weights < 0.02 405 -> 298,
#: max non-orthogonality 76.6676 -> 76.0512, max aspect ratio 322.161 -> 241.68, coverage 95.553%
#: -> 95.7253%, layer pass 2,522 s -> 2,353 s. Every one of those still stands. NONE OF THEM IS
#: THE CONCAVE COUNT, which is the largest thing the layer pass produces.
#:
#: Swept one knob at a time on the two-across plate, 2026-11-22, with the pinch repair in the
#: chain. `nSmoothNormals` does NOTHING: 37,651 concave cells against the baseline's 37,677, a
#: tenth of a per cent, so it was never the one doing the damage. `nSmoothSurfaceNormals` is the
#: strongest single effect in the sweep and it has an optimum at 5:
#:
#:     nSmoothSurfaceNormals   0        2        3        5        8       10
#:     concave cells      37,677   32,105   27,480   20,046   23,270   23,409
#:     weight faces < 0.05   209      116       85       66       71       92
#:
#: WHY IT WORKS IS THE STAIRCASE. `surfaceToCell useSurfaceOrientation` classifies by cell centre,
#: so every neighbouring interface face meets its neighbour at 90 degrees; with no smoothing each
#: prism is extruded along its own face's normal and two prisms at a step are driven into one
#: another, which is a concave cell by the face-plane test. The SOLID settles that these are the
#: layer pass and not the voxel cut: it carries the same staircase, gets no layers at all (the
#: dict below names `fluid_to_solid` only), and checkMesh says "Concave cell check OK" on it in
#: every one of the fifteen configurations swept, while the coolant has 36,000 to 43,000 in all
#: of them.
#:
#: WHAT IT COSTS ON ITS OWN is 8 to 12 faces with a small volume ratio, a failed check the
#: baseline does not have. The thickness floor does not pay those off - `s5` with the floor at
#: 0.80 still has 10 - and FEATURE_ANGLE does, which is why the two below are set together.
#: SWEPT AGAIN ON A PRODUCTION-DENSITY RIG, 2026-11-26, AND THE ANSWER IS 0. The table above was
#: measured on the TWO-ACROSS plate, which its own heading says, and two across is the only channel
#: width at which 5 wins anything. A 45-degree diagonal lattice at this engine's own pitch and cell
#: - 201.3 um pitch, 25.1578 um cells, the 3x y-stretch, one knob off this dict - swept over width:
#:
#:     cells across    nSmoothSurfaceNormals 0          nSmoothSurfaceNormals 5
#:                     cover layers thick concave       cover layers thick concave
#:          2           100   2.81   97.6   1234        99.96  2.81   97.6    772
#:          3           100   1.83   81.2    480        75.18  0.784  45.3    358
#:          4           100   3.00  100.0      0        75.79  0.808  46.4    332
#:          6           100   3.00  100.0      0        76.52  0.822  47.1    284
#:         10           100   3.00  100.0      0        76.43  0.825  47.1    168
#:
#: Read DOWN the three columns that are the deliverable - coverage, layers kept of three, thickness
#: achieved - and 0 is never worse at any width. From four cells across it is PERFECT and 5 throws
#: away a quarter of the interface and over half the stack, at every width, forever. At two across
#: the two are identical on all three and differ only in concave cells, which is what the old sweep
#: optimised: job 22ac7054's part is 3.91 cells across its thin passages and 10.50 across its broad
#: ones, so hardly any of its interface is in the regime the 5 was fitted in.
#:
#: WHY, AND IT IS THE OPPOSITE OF WHAT THIS BLOCK USED TO ARGUE. Neighbouring interface faces meet
#: at 90 degrees. Unsmoothed, the prism off a step's top face goes up and the prism off its side
#: face goes sideways: they DIVERGE. Smoothing averages the two normals toward the diagonal and the
#: same two prisms then CONVERGE and collide - which is the concave cell AND the reason the mover
#: un-extrudes, which is the coverage and the thickness. Smoothing is the cause, not the cure.
#:
#: AND THE COUNT IT WAS CHOSEN BY IS A DIAGNOSTIC. `checkConcaveCells` sits inside checkMesh's
#: `if (allGeometry)` block, tests face CENTRES against face PLANES at a 1e-6 coplanarity
#: tolerance, and its own source comment reads "Concave or planar face". The checks that decide
#: whether a cell works in the FV scheme - checkFacePyramids, cell volumes, face areas - pass on
#: job 22ac7054 in both regions.
#:
#: AT 0 THIS ONE ENTRY DOMINATES THE DICT: `nLayerIter 4`, `slipFeatureAngle 90`,
#: `featureAngle 180` and `nSmoothThickness 0` each give byte-identical output, because there is no
#: displacement field left to smooth and no collision left to terminate.
#: AND THEN IT WAS MEASURED ON THE PART ITSELF, WHICH REFUTED THE SWEEP. Jobs 22ac7054 and
#: b8f09ae9 are the same Corintis cold plate on the same plan, with this constant as the ONLY
#: difference between them - the solid region came out at 192,087,711 cells in both, to the cell.
#: Read off both runs' own conjugate_report.json, which is the production parser's reading and not
#: a log scraped by hand:
#:
#:     nSmoothSurfaceNormals          5            0
#:     coverage                90.6422%     90.7174%
#:     layers kept of 3            2.00         2.00
#:     thickness achieved         79.8%        79.9%
#:     illegal faces                  8            8
#:     fluid concave cells    1,340,185    2,341,980
#:     fluid det < 0.001         71,529       79,295
#:     fluid max skewness       3.53913        3.782
#:     fluid max non-ortho      61.0276      61.5883
#:     fluid max aspect         108.362      104.101
#:     layer pass              2579.65 s    2717.94 s
#:
#: The three columns the sweep above was read down DID NOT MOVE: 0.075 of a coverage point, 0.1 of
#: a thickness point, layers kept identical to two decimal places. What moved was a million extra
#: concave cells, 7,766 more cells under the determinant floor, 6.9% more skewness and 138 extra
#: seconds. So it is back at 5. The sweep is kept rather than deleted because it is now the thing
#: that has to be explained.
#:
#: WHY THE RIG DID NOT TRANSFER, and this is the part to carry forward. b8f09ae9 instrumented what
#: the extruder was ever OFFERED, which the earlier run did not record: it offered 91.1145% of the
#: interface and kept 99.5641% of what it offered. Of the 9.28 missing points, 8.89 were declined
#: up front by `featureAngle 120` and only 0.40 were offered and then lost over ten iterations of
#: repair. A SMOOTHER ONLY ACTS ON A FACE THAT WAS OFFERED, so on this part no setting of these
#: three can reach more than 0.40 of a point, whatever the sweep says. The rig - a clean 45-degree
#: lattice with no port cuts, no mouths and no pinch-bridged metal - had almost nothing declined,
#: so it put the whole of its coverage difference in the one place this part keeps 4% of it. The
#: rig is wrong about this part in the other direction too: its 5-column predicts 75.18% coverage
#: and 0.784 layers at three cells across, and the part measured 90.6422% and 2.00 at 3.91 across.
#: THE COVERAGE LEVER ON THIS PART IS FEATURE_ANGLE, NOT ANY SMOOTHER.
SMOOTH_SURFACE_NORMALS = 5
SMOOTH_THICKNESS = 10
SMOOTH_NORMALS = 0

#: WHERE THE MOVER IS ALLOWED TO STOP. `featureAngle` is the angle above which a surface is not
#: extruded, and 180 - which every tutorial writes and this module carried - means never: the
#: mover is required to carry a layer around every 90-degree step of the staircase, including the
#: steps where it cannot. Declining to extrude 1,796 faces of 506,322, which is 0.35% of the
#: interface, is the whole difference between 180 and 120, and those 1,796 faces were producing
#: the mesh's worst cells:
#:
#:     featureAngle          180      150      120       90
#:     max aspect ratio    37.49    19.30    19.30    19.30
#:     max non-ortho       59.30    47.43    47.43    47.43
#:     weight faces < 0.05   209       45       34       34
#:     volume-ratio faces      0        8        0        0
#:     concave cells      37,677   37,406   37,590   62,700
#:
#: 150 and 120 buy the same maxima and 150 brings volume-ratio faces with it; 90 excludes 5,480
#: more faces for nothing but a concave count that doubles, because an unextruded face beside an
#: extruded one is a step in the layer itself. The bound is not abstract: the delivered 155 M
#: plate came back at 64.8123 degrees against this engine's bar of 65, which is 0.19 degrees.
#: SETTABLE PER RUN, and 120 unless it is set. The table above was measured on a 506,322-face
#: coupon where 120 declined 0.35% of the interface. On job cba7ac69's 16,582,198-face plate
#: it declined 9.14%, which is 26 times the share, and that refusal - not the mover - is what
#: fails the coverage floor there: the pass kept 99.7599% of every face it was given. Whether
#: those faces can carry prisms at acceptable cell quality is a question about THAT part, and
#: the coupon cannot answer it. This exists so the answer can be measured rather than argued,
#: on the real geometry, without a deploy between each attempt.
#:
#: ABSENT OR UNREADABLE, THE SHIPPED VALUE STANDS. An out-of-range or non-numeric setting is
#: ignored rather than clamped: a run that silently meshed at an angle nobody chose would be
#: worse than one that meshed at the angle the module documents.
LAYER_FEATURE_ANGLE_ENV = "HEXERA_LAYER_FEATURE_ANGLE"
FEATURE_ANGLE_DEFAULT = 120


def _feature_angle() -> int:
    import os

    raw = (os.environ.get(LAYER_FEATURE_ANGLE_ENV) or "").strip()
    if not raw:
        return FEATURE_ANGLE_DEFAULT
    try:
        # OverflowError as well as ValueError: float("1e999") is inf, and int(inf) raises
        # OverflowError, so a value that is merely huge would otherwise take the whole mesher
        # down at import rather than being ignored like every other unusable setting.
        got = int(float(raw))
    except (ValueError, OverflowError):
        return FEATURE_ANGLE_DEFAULT
    # OpenFOAM reads this as degrees between two face normals: outside 0..180 it means nothing.
    return got if 0 <= got <= 180 else FEATURE_ANGLE_DEFAULT


FEATURE_ANGLE = _feature_angle()

#: HOW MANY TIMES THE MOVER MAY REPAIR WHAT IT GREW, and until now this was a bare literal in the
#: renderer with no name, no comment and no override - the only number in this dictionary that
#: could not be measured on the real part without a deploy.
#:
#: FOUR, NOT TEN. Every figure below is labelled with the job it came from, because two runs of
#: this plate differ by twelve thousand cells and their numbers have been mixed up before.
#:
#:   JOB b8f09ae9 (279,294,127 cells), its own snappyHexMesh.log, ten iterations:
#:     iteration   0        1       2       3       4       5       6       7       8       9
#:     illegal  372431   14094      79      48      38      30      26      26       9       8
#:     coverage 91.1145 90.7214 90.7198 90.7191 90.7186 90.7183 90.7180 90.7178 90.7175 90.7174
#:
#: Iterations 4 to 9 take illegal faces from 38 to 8 and coverage DOWN by 0.0017 of a point, for
#: six tenths of the pass. At four iterations this run would have finished on 48 illegal faces -
#: 0.17 per million against `ILLEGAL_PER_MILLION_CEILING` 200.0, which is 0.09% of the only
#: ceiling this module judges the count by - and at a coverage 0.0017 points HIGHER than it did.
#:
#: REPAIR ONLY EVER REMOVES LAYERS. That is the part that makes this a straight win rather than a
#: trade. Coverage falls monotonically from iteration 0 in all twelve variants recorded in
#: runs/layer_sweep-two_scale_plate_ported-2a-L3.json; for `fa120_s5`, which is the shipped pair,
#: the curve is 96.0322 95.6901 95.6640 95.6356 95.6119 95.5850 95.5645 95.5471 95.5424 95.5376,
#: so truncating at four FINISHES 0.098 of a point higher. The iterations buy exactly one thing,
#: a lower illegal count, and they pay for it in layers.
#:
#: AND IT CANNOT REACH THE COVERAGE FLOOR ANYWAY. `COVERAGE_FLOOR_PCT` is 92.0 and both runs of
#: this plate finish at 90.64 / 90.72, because 8.89 of the 9.28 missing points are declined up
#: front by `featureAngle` and never offered to the mover at all (job b8f09ae9: offered 91.1145%,
#: kept 99.5641% of what it offered). No number of repair iterations can recover a face that was
#: never extruded.
#:
#: THE DEFERRAL THAT HELD THIS AT TEN IS SPENT. Commit 47a5293e left it at ten on purpose and said
#: why: "the time lever waits for a production layer-pass time measured with the smoothing off,
#: rather than two variables moving at once". 82c39ff2 put `SMOOTH_SURFACE_NORMALS` back to 5
#: after the part refuted the rig, so there is one variable again, and the production time at 5 is
#: on record - job 22ac7054's layer pass at 2579.65 s, of which 47a5293e measures 25 of 44.9
#: minutes as the relaxed phase.
#:
#: WHAT THIS DOES NOT CLAIM. The saving is an estimate, not a measurement of a four-iteration run.
#: Iterations are not equal cost: the floor sweep at :111-123 measures four at 69 s against ten at
#: 122 s, and runs/layer_sweep-two_scale_plate_ported-2a-L3.json gives five at 51.81 s against ten
#: at 85.5-87.1 s, so the marginal iteration is about half the average one and the six dropped
#: iterations are 43-56% of the pass rather than 60%.
#:
#: IT IS VERIFIABLE WITHOUT RE-MESHING, and that is why four is safe to ship. `nLayerIter` appears
#: in snappyLayerDriver.C only as the loop bound - `for (label iteration = 0; iteration <
#: nLayerIter; iteration++)` - the relaxed switch reads only `iteration` and `nRelaxedIter`, and
#: the single `break` reads only `nTotChanged`. So iterations 0-3 of a ten-iteration log ARE the
#: four-iteration run, exactly, and the table above is that run's result on the real part.
LAYER_ITER_ENV = "HEXERA_LAYER_ITER"
LAYER_ITER_DEFAULT = 4

#: WHEN THE STRICT QUALITY BAR GIVES WAY TO `relaxed`. Promoted out of the renderer beside
#: `LAYER_ITER` because the two are read against each other and must never be set apart.
N_RELAXED_ITER = 3


def _layer_iter() -> int:
    import os

    raw = (os.environ.get(LAYER_ITER_ENV) or "").strip()
    if not raw:
        return LAYER_ITER_DEFAULT
    try:
        # OverflowError as well as ValueError, for the same reason as `_feature_angle`.
        got = int(float(raw))
    except (ValueError, OverflowError):
        return LAYER_ITER_DEFAULT
    # AT OR BELOW `nRelaxedIter` THE RELAXED PHASE NEVER RUNS, AND NOTHING SAYS SO.
    # snappyLayerDriver picks the quality bar with
    # `iteration < nRelaxedIter ? motionDict : motionDict.subDict("relaxed")`, so nLayerIter <=
    # nRelaxedIter means that ternary never selects the relaxed sub-dictionary and `RELAXED_SHAPE`
    # below becomes dead text that is still written to disk. layerParameters.C validates neither
    # against the other and prints no warning. Three is therefore NOT one less than four here; it
    # is a different dictionary, and the clamp refuses it.
    return got if N_RELAXED_ITER < got <= 10 else LAYER_ITER_DEFAULT


LAYER_ITER = _layer_iter()

#: HOW MUCH OF THE INTERFACE HAS TO END UP LAYERED. The measured run finished at 95.553% after ten
#: outer iterations; the DEFECTIVE delivered run finished at 90.7146%, so a floor under that would
#: admit the run this module exists to refuse.
COVERAGE_FLOOR_PCT = 92.0
#: ...AND HOW MUCH OF THE ASKED STACK HAS TO SURVIVE. The measured run achieved 2.84 of 3 layers at
#: 94.6% of the requested thickness. Thinning is the mover's only repair and it manufactures
#: hairline cells, so a stack that came back much thinner than it was asked for is the failure that
#: coverage alone cannot see.
THICKNESS_FLOOR_PCT = 75.0
LAYERS_FLOOR_FRACTION = 0.75
#: Faces in error per million cells, above which the pass did more harm than good. The measured run
#: is 2,888 illegal faces over 102,536,327 cells, which is 28.2 per million; the delivered run's
#: iteration 0 held 82,548 over a smaller mesh.
ILLEGAL_PER_MILLION_CEILING = 200.0

#: THE FOUR FLOORS THIS MODULE JUDGES A LAYER PASS BY, each with the figure(s) `measured()` must
#: have read for that floor to have been drawn on anything at all - NAME FIRST, so a reader can be
#: told WHICH of the four nobody judged.
#:
#: THIS ROSTER EXISTS BECAUSE `shortfalls` RETURNS AN EMPTY LIST FOR TWO OPPOSITE REASONS. Each of
#: its four tests is guarded on its own input being present, so a log that yielded one figure and a
#: log that yielded all four both come back `[]` when nothing fell short - and
#: `render/mesh_facts._LINE_METRICS` defines `[]` as "this was judged and it met every floor".
#: MEASURED, SYNTHETIC, 2026-11-17: a log holding only the Extruding and Added lines published
#: `layer_coverage_pct` 99.9 with `layer_short` `[]`, so ONE judged floor was reported as four, on
#: the same panel that said the layer count had not been measured at all. `floors_unjudged` is how
#: the three that were never evaluated get named instead of implied.
#:
#: NO THRESHOLD IS RESTATED HERE and none can be: these are the INPUTS. Whether a floor was MET is
#: `shortfalls`' own business, computed once, against the pass's own `cells_added`.
LAYER_FLOORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("coverage", ("layer_coverage_pct",)),                 # COVERAGE_FLOOR_PCT
    ("thickness", ("layer_thickness_pct",)),               # THICKNESS_FLOOR_PCT
    ("layers kept", ("layers_in_mesh",)),                  # LAYERS_FLOOR_FRACTION
    # BOTH NUMBERS, because `shortfalls` skips this one on `int(cells) > 0` and a face count with
    # nothing to divide it by was never judged either - which is why `layer_cells_added` is in
    # `_FLOORS_NEED_A_POSITIVE_READING` below rather than merely required to be present.
    ("faces in error", ("layer_faces_in_error", "layer_cells_added")),
)

#: THE ONE READING A FLOOR NEEDS MORE THAN THE PRESENCE OF, and that was the defect. `shortfalls`
#: guards the faces-in-error floor on `int(cells) > 0`, so a pass whose `cells_added` is 0 never
#: reaches that comparison at all - while the presence test here was `is not None`, which 0
#: satisfies. MEASURED, SYNTHETIC, 2026-11-17: a zero-layer log (table row
#: `fluid_to_solid 16582198 3 0 0 0`, `Added 0 out of 0 cells (0%)`, 90,000 illegal faces) came
#: back from `floors_unjudged` as `()` beside THREE shortfall sentences, so the panel's own tally
#: read "4 of the 4 floors this engine judges a layer pass by had a figure in the layer log" over
#: a verdict on three. The face count was there; the number to divide it by was not.
#:
#: A LIST OF ONE, AND NOT A RULE ABOUT ZEROES. Every other figure these floors are drawn on is
#: judged AT zero and must stay judged there: 0.0000% coverage, 0.0% thickness and 0.00 layers
#: kept are the largest misses those three floors can have, and they are exactly the misses that
#: same zero-layer pass does report. Only this one is a DENOMINATOR.
_FLOORS_NEED_A_POSITIVE_READING: frozenset[str] = frozenset({"layer_cells_added"})


def _judged_on(got: dict, key: str) -> bool:
    """Whether `got[key]` is the reading `shortfalls` needed to have evaluated a floor on it.

    A NUMBER, WHERE A NUMBER IS DIVIDED BY, and a bool is not one here: `measured()`'s dict is
    built from JSON written on the far side of a container boundary, and `True` would otherwise
    pass `float(v) > 0.0` as a cell count of one.
    """
    v = got.get(key)
    if v is None:
        return False
    if key in _FLOORS_NEED_A_POSITIVE_READING:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 0.0
    return True


def floors_unjudged(grown: dict) -> tuple[str, ...]:
    """The names of the floors in `LAYER_FLOORS` `shortfalls` did not evaluate on this pass.

    `grown` is `measured()`'s own dict, which is the one spelling of these figures. `()` means all
    four WERE judged; a tuple as long as `LAYER_FLOORS` means nothing about this pass was judged
    at all - which is NOT the same answer as "it met every floor", and is the whole reason this
    function exists.

    EVALUATED, NOT KEYED, and the difference is a whole floor. A floor is counted as judged here
    when `shortfalls`' own guard for it would have fired: the presence of its figure for three of
    them, and a POSITIVE `layer_cells_added` for the faces-in-error one, whose test is a ratio
    (see `_FLOORS_NEED_A_POSITIVE_READING`, which carries the measurement). Asking only whether a
    key was present reported a zero-layer pass as judged by four floors when three had been.

    `layer_iterations` and `layer_seconds` are deliberately not floors. Ten repair iterations is a
    reading of what the pass did; zero of them is the absence of a pass, and the pass's seconds are
    a cost. Neither is a figure any floor is drawn on, so neither can make a judgement exist.
    """
    got = dict(grown or {})
    return tuple(name for name, keys in LAYER_FLOORS
                 if not all(_judged_on(got, k) for k in keys))


_EXTRUDING = re.compile(r"Extruding\s+(\d+)\s+out of\s+(\d+)\s+faces\s+\(([\d.]+)%\)")
_ADDED = re.compile(r"Added\s+(\d+)\s+out of\s+(\d+)\s+cells\s+\(([\d.]+)%\)")
_LAYER_ROW = re.compile(r"^\s*(\S+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.eE+-]+)\s+([\d.]+)\s*$")
_ILLEGAL = re.compile(r"Finished meshing with (\d+) illegal faces")
_SECONDS = re.compile(r"Layers added in = ([\d.]+) s")


class LayerError(ValueError):
    """An ask about layers that cannot be satisfied as stated."""


def interface_cell_m(background_cell_m: float, levels: int) -> float:
    """The cell at the conjugate interface: the background halved once per octree level under it.

    ON A TWO-SCALE PART THE LEVELS ARE THE FINE SURFACE'S, not the coolant's own. The run plan for
    the first customer plate is `levels 1` with a `fine_surfaces` entry at levels 4, and the
    interface is where that fine surface is - so the cell there is background / 2**4, which is
    25.1598 um against a 402.5570 um background. `finalize._near_wall` takes the deepest level for
    the same reason.
    """
    bg = float(background_cell_m)
    if bg <= 0.0:
        raise LayerError(f"the background cell must be positive, got {background_cell_m}")
    if int(levels) < 0:
        raise LayerError(f"octree levels cannot be negative, got {levels}")
    return bg / (2 ** int(levels))


def geometry_for(interface_cell_m: float, n_layers: int = N_SURFACE_LAYERS) -> dict:
    """The stack geometry for a mesh whose cells AT THE INTERFACE are this big.

    NOT THE BACKGROUND CELL. The layers grow on the conjugate interface and the three thicknesses
    only mean anything against the cell they sit in; given the background instead they come out
    2**levels too thick, which on the run plan is sixteen times, and the fit rule cannot see it
    because it scales the same way. Use `interface_cell_m()` to get the number.
    """
    bg = float(interface_cell_m)
    if bg <= 0.0:
        raise LayerError(f"the interface cell must be positive, got {interface_cell_m}")
    n = int(n_layers)
    if n <= 0:
        raise LayerError(f"a stack has to have at least one layer, got {n_layers}")
    first = bg * FIRST_LAYER_FRACTION
    # HALF OF THIS STACK, not half of a three-layer one. Sized against N_SURFACE_LAYERS while the
    # caller overwrote n_layers afterwards, the floor at one layer came out at 182% of the stack:
    # every face fails a bar it cannot reach, nothing extrudes, and the run exits 0 saying
    # "Mesh OK". See MIN_THICKNESS_OF_STACK.
    stack = stack_thickness_m(first_layer_m=first, expansion=EXPANSION_RATIO, n_layers=n)
    return {"n_layers": n,
            "first_layer_m": first,
            "expansion": EXPANSION_RATIO,
            "min_thickness_m": MIN_THICKNESS_OF_STACK * stack}


def stack_thickness_m(*, first_layer_m: float, expansion: float, n_layers: int) -> float:
    """How deep the whole stack is: first * (1 + r + r^2 + ... + r^(n-1))."""
    r = float(expansion)
    n = int(n_layers)
    if n < 1:
        raise LayerError(f"a stack needs at least one layer, got {n_layers}")
    if r <= 0.0:
        raise LayerError(f"the expansion ratio must be positive, got {expansion}")
    growth = n if abs(r - 1.0) < 1e-12 else (r ** n - 1.0) / (r - 1.0)
    return float(first_layer_m) * growth


#: HOW MUCH BIGGER THE WHOLE MESH GETS PER LAYER, measured. The pass grows the COOLANT
#: 55,303,441 cells to 102,536,327, which is 1.854x - but the coolant is a quarter of this part and
#: the metal around it does not move, so the whole two-region mesh goes 223,191,421 to 270,424,307,
#: which is 1.212x. Per layer that is 0.0707 of the unlayered mesh.
#:
#: IT IS THE WHOLE-MESH FIGURE BECAUSE THAT IS WHAT IS BEING PRICED. The first version of this used
#: the coolant's own 1.854 against a whole-mesh budget, which over-priced the run by 53% and
#: refused, at the limit already configured, the very plan the hand-built mesh was built from. The
#: error showed up against the mesh that exists: raw estimate x factor predicted 381,405,488 cells
#: where the real layered mesh is 270,424,307.
WHOLE_MESH_PER_LAYER = (270_424_307 / 223_191_421 - 1.0) / 3.0
#: ...and the coolant's own, which is what the layer pass's RUN TIME scales with.
COOLANT_PER_LAYER = (102_536_327 / 55_303_441 - 1.0) / 3.0


def cells_multiplier(n_layers: int) -> float:
    """How much bigger the WHOLE two-region mesh gets when this stack is grown on its interface.

    IT IS HERE BECAUSE THE HOST PRICES THE RUN. The machine, the disk, the VM's lifetime and the
    cell budget are all chosen from an estimate taken before this stage existed, and
    `gates._gate_manifest_valid` re-checks the DELIVERED count against the same limit - so a plan
    admitted at 0.85 of it delivers over and is refused AFTER the whole chain, which is the entire
    run thrown away for arithmetic that costs nothing.

    Linear in the layer count because nothing finer has been measured.
    """
    if int(n_layers) <= 0:
        return 1.0
    return 1.0 + WHOLE_MESH_PER_LAYER * int(n_layers)


def coolant_multiplier(n_layers: int) -> float:
    """How much bigger the COOLANT REGION gets - a different number, and not the one to price with.

    1.854x at three layers against the whole mesh's 1.212x, because the coolant is a quarter of
    this part and the metal around it does not move. It is here because the layer pass's own cost
    scales with the coolant, not with the mesh.
    """
    if int(n_layers) <= 0:
        return 1.0
    return 1.0 + COOLANT_PER_LAYER * int(n_layers)


def seconds_estimate(n_layers: int, *, coolant_cells: float) -> float:
    """Roughly how long the pass takes, for the run deadline.

    2,521.87 s for three layers on a 55.3 million cell coolant at eight ranks, which is 45.6 us per
    unlayered coolant cell. Linear in the layer count because nothing else has been measured, and
    it is used only to keep a deadline from cutting the stage off - never to promise anything.
    """
    if int(n_layers) <= 0:
        return 0.0
    return 45.6e-06 * float(coolant_cells) * (int(n_layers) / 3.0)


def fit_complaint(*, first_layer_m: float, expansion: float, n_layers: int,
                  min_thickness_m: float, interface_cell_m: float) -> str:
    """Empty when this stack fits inside one cell AT THE INTERFACE, otherwise what is wrong.

    THE RULE THAT WAS BROKEN ON THE DELIVERED MESH. A stack deeper than the cell it grows into
    cannot be placed, and snappyHexMesh's only repair is to thin it, which manufactures hairline
    cells and loses coverage it never gets back.

    THE CELL HAS TO BE THE ONE AT THE INTERFACE. Given the background instead, this check compares
    a stack that is 2**levels too thick against a cell that is 2**levels too big and passes - the
    error cancels exactly, which is how a 241.7153 um stack into 25.1598 um cells read as 60.0% of
    its cell.
    """
    bg = float(interface_cell_m)
    if bg <= 0.0:
        return f"the interface cell must be positive, got {interface_cell_m}"
    stack = stack_thickness_m(first_layer_m=first_layer_m, expansion=expansion,
                              n_layers=n_layers)
    share = stack / bg
    if share > MAX_STACK_FRACTION:
        return (f"a {n_layers}-layer stack of {stack * 1e6:.3f} um is {share * 100:.1f}% of the "
                f"{bg * 1e6:.4f} um cell at the interface, over the "
                f"{MAX_STACK_FRACTION * 100:.0f}% "
                f"this engine allows. It does not fit, and snappyHexMesh's only repair is to thin "
                f"it, which manufactures hairline cells: the delivered mesh asked for 109% of a "
                f"cell and spent ten iterations dropping coverage from 99.77% to 90.71%")
    # A FLOOR THE STACK CANNOT REACH IS A ZERO-LAYER RUN THAT EXITS 0. snappyHexMesh unextrudes
    # every face whose stack comes in under `minThickness`, so a floor at or above the stack
    # unextrudes all of them and still prints "Finished meshing without any errors". This is the
    # bound in the opposite direction from the `minFaceWeight` one below, and it is the one that
    # fails silently.
    if float(min_thickness_m) >= stack:
        return (f"minThickness {float(min_thickness_m) * 1e6:.4f} um is at or above the whole "
                f"{n_layers}-layer stack of {stack * 1e6:.4f} um, so every face would fail a bar "
                f"it cannot reach: snappyHexMesh would unextrude all of them, report no errors "
                f"and exit 0 with no layers grown")
    weight_floor = 0.02 * bg
    if float(min_thickness_m) < weight_floor:
        return (f"minThickness {float(min_thickness_m) * 1e6:.4f} um permits a layer thinner than "
                f"minFaceWeight 0.02 against a {bg * 1e6:.4f} um neighbour allows "
                f"({weight_floor * 1e6:.4f} um), so the mover would make illegal faces by "
                f"construction - which is what the delivered dict did at 0.400 um")
    return ""


#: THE FOUR LIMITS A RELAXED ITERATION MAY LOOSEN, and they are all about SHAPE.
#:
#: A relaxed iteration is the mover saying "I cannot fix this, let me accept a worse cell". A
#: worse-shaped cell is a fair thing to accept: a bit more non-orthogonality or skew costs the
#: solver accuracy and nothing else. A THINNER cell is not, because thin has no floor of its own -
#: it goes to zero, and a cell of zero thickness is not a worse answer, it is not an answer.
RELAXED_SHAPE: dict[str, str] = {
    "maxNonOrtho": "75",
    "maxBoundarySkewness": "25",
    "maxInternalSkewness": "6",
    "maxConcave": "85",
}


def _quality_lines(*, min_vol: float, relaxed: bool = False) -> list[str]:
    """Every `meshQualityControls` entry, as `key value;` lines. THE ONLY PLACE A FLOOR IS WRITTEN.

    THE RELAXED BLOCK IS NOT A PATCH ON THE STRICT ONE - OpenFOAM READS IT ALONE. `nRelaxedIter`
    swaps `meshQualityControls` for its `relaxed` sub-dictionary wholesale, and that sub-dictionary
    is read as a dictionary in its own right: every key it does not contain is defaulted away, not
    inherited. The one-line `relaxed { maxNonOrtho 75; }` that the tutorials write - and that this
    module wrote - therefore switches OFF `minVol`, `minTetQuality`, `minDeterminant`,
    `minFaceWeight`, `minVolRatio` and `minTwist` for every relaxed iteration.

    On the delivered 155 M plate that was seven of ten iterations, and the mesh came back with a
    cell determinant of 0 against this dict's own `minDeterminant 0.001`, an interpolation weight
    of 0.0107 against its `minFaceWeight 0.02`, a volume ratio of 0.0065 against its
    `minVolRatio 0.01` and a smallest cell of 3.74e-17 m3 - a layer 0.059 um thick. The mover
    reported 133 illegal faces, because with the floors defaulted away almost nothing was illegal.

    So both blocks come from here, and they differ only by `RELAXED_SHAPE`. A floor cannot be
    dropped from one of them by editing the other.
    """
    entry = {
        "maxNonOrtho": "65",
        "maxBoundarySkewness": "20",
        "maxInternalSkewness": "4",
        "maxConcave": "80",
        "minVol": f"{float(min_vol):g}",
        "minTetQuality": f"{MIN_TET_QUALITY:g}",
        "minArea": "-1",
        "minTwist": "0.02",
        "minDeterminant": "0.001",
        "minFaceWeight": "0.02",
        "minVolRatio": "0.01",
        "minTriangleTwist": "-1",
    }
    if relaxed:
        entry.update(RELAXED_SHAPE)
    return [f"{k:<22}{v};" for k, v in entry.items()]


def render_mesh_quality_dict(interface_cell_m: float) -> str:
    """`system/meshQualityDict`, which `checkMesh -meshQuality` finds by name and cannot be given
    with `-dict` because checkMesh has no such option.

    It takes the cell for the same reason `render_layers_dict` does: `minVol` is an absolute
    volume, so the only honest floor is one derived from the mesh it is judging.
    """
    return "\n".join([
        "FoamFile { version 2.0; format ascii; class dictionary; object meshQualityDict; }",
        *_quality_lines(min_vol=min_vol_for(interface_cell_m)),
        "nSmoothScale          4;",
        "errorReduction        0.75;",
        "",
    ])


def render_layers_dict(*, interface_patch: str, interface_cell_m: float, n_layers: int,
                       first_layer_m: float, expansion: float, min_thickness_m: float) -> str:
    """`system/snappyHexMeshDict` for an addLayers-only pass.

    IT TAKES THE CELL AT THE INTERFACE. The first draft recovered it from `first_layer_m /
    FIRST_LAYER_FRACTION`, which makes the fit check self-referential: the share came out at
    0.600450 for a first layer of 1e-9 and of 1e-3 alike, so the only guard inside the renderer
    could not fire. The second took the BACKGROUND cell, which is 2**levels too big and cancelled
    the error exactly. The cell is the caller's to supply, and it is the one the layers grow in.

    `meshQualityControls` IS WRITTEN OUT IN FULL, never `#include "meshQualityDict"`. The measured
    dict used the include and the product cannot: `sandbox/foam_case_guard` refuses any active
    preprocessor directive, and that refusal fires again inside `foam_exec.check_mesh` - so a
    finished mesh of hundreds of millions of cells would be thrown away on a dictionary, after
    every stage had succeeded. The engine that already does it inline is `snappy`.

    `castellatedMeshControls` EXISTS THOUGH castellatedMesh IS FALSE, because snappyHexMesh
    requires the block to be present and exits without it. Nothing in it is used, `locationInMesh`
    included - which is why it is a fixed origin with a comment saying so, rather than a point
    computed by reading the whole coolant surface into memory for a value nothing reads.
    """
    if not str(interface_patch).strip():
        raise LayerError("the patch to grow layers on has to be named")
    bad = fit_complaint(first_layer_m=first_layer_m, expansion=expansion, n_layers=n_layers,
                        min_thickness_m=min_thickness_m, interface_cell_m=interface_cell_m)
    if bad:
        raise LayerError(bad)
    return "\n".join([
        "FoamFile { version 2.0; format ascii; class dictionary; object snappyHexMeshDict; }",
        "castellatedMesh false;",
        "snap            false;",
        "addLayers       true;",
        "geometry        {}",
        "castellatedMeshControls",
        "{",
        f"    maxLocalCells        {MAX_LOCAL_CELLS};",
        f"    maxGlobalCells      {MAX_GLOBAL_CELLS};",
        "    minRefinementCells           0;",
        "    maxLoadUnbalance          0.10;",
        "    nCellsBetweenLevels          1;",
        "    features ();",
        "    refinementSurfaces {}",
        "    resolveFeatureAngle 30;",
        "    refinementRegions {}",
        "    // not read: castellatedMesh is false, so the step that uses this never runs",
        "    locationInMesh (0 0 0);",
        "    allowFreeStandingZoneFaces false;",
        "}",
        "snapControls { nSmoothPatch 3; tolerance 2.0; nSolveIter 30; nRelaxIter 5; }",
        "addLayersControls",
        "{",
        "    relativeSizes false;",
        f"    layers {{ {interface_patch} {{ nSurfaceLayers {int(n_layers)}; }} }}",
        f"    expansionRatio          {float(expansion):g};",
        f"    firstLayerThickness     {float(first_layer_m):.6g};",
        f"    minThickness            {float(min_thickness_m):.6g};",
        "    nGrow 0;",
        f"    featureAngle              {int(FEATURE_ANGLE)};",
        "    maxFaceThicknessRatio     0.5;",
        f"    nSmoothSurfaceNormals       {SMOOTH_SURFACE_NORMALS};",
        f"    nSmoothThickness           {SMOOTH_THICKNESS};",
        "    minMedialAxisAngle         90;",
        "    maxThicknessToMedialRatio 0.6;",
        f"    nSmoothNormals              {SMOOTH_NORMALS};",
        "    slipFeatureAngle           30;",
        "    nRelaxIter                  5;",
        # A LAYER THAT STOPS, STOPS GRADUALLY. See BUFFER_CELLS_NO_EXTRUDE.
        f"    nBufferCellsNoExtrude       {BUFFER_CELLS_NO_EXTRUDE};",
        f"    nLayerIter                 {LAYER_ITER:>2};",
        f"    nRelaxedIter                {N_RELAXED_ITER};",
        "}",
        "meshQualityControls",
        "{",
        *[f"    {line}" for line in _quality_lines(min_vol=min_vol_for(interface_cell_m))],
        # WRITTEN OUT IN FULL, because OpenFOAM reads this sub-dictionary alone and defaults away
        # every key it does not find. See `_quality_lines`.
        "    relaxed",
        "    {",
        *[f"        {line}" for line in
          _quality_lines(min_vol=min_vol_for(interface_cell_m), relaxed=True)],
        "    }",
        "    nSmoothScale 4;",
        "    errorReduction 0.75;",
        "}",
        "writeFlags ();",
        "mergeTolerance 1e-6;",
        "",
    ])


def render_support_dicts() -> dict[str, str]:
    """The rest of `system/` the layer case needs. snappyHexMesh opens all of these.

    `writeFormat ascii` IS NOT A PREFERENCE. Binary is 1.3 to 1.8x slower on every stage and makes
    the mesh unreadable to the boundary index, the viewer and the quality sidecar - and those
    readers refuse it by design, so the refusal lands only after the whole chain has succeeded.
    `fvSolution` carries no real solver: snappyHexMesh needs the file to exist and reads nothing
    from it, and the measured case's own was `solvers {}`.
    """
    return {
        "controlDict": "\n".join([
            "FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }",
            "application     chtMultiRegionFoam;",
            "startFrom       startTime;",
            "startTime       0;",
            "stopAt          endTime;",
            "endTime         1;",
            "deltaT          1;",
            "writeControl    timeStep;",
            "writeInterval   1;",
            "writeFormat     ascii;",
            "writeCompression off;",
            "timeFormat      general;",
            "timePrecision   6;",
            "runTimeModifiable true;",
            "",
        ]),
        "fvSchemes": "\n".join([
            "FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }",
            "ddtSchemes { default steadyState; }",
            "gradSchemes { default Gauss linear; }",
            "divSchemes { default none; }",
            "laplacianSchemes { default Gauss linear corrected; }",
            "interpolationSchemes { default linear; }",
            "snGradSchemes { default corrected; }",
            "",
        ]),
        "fvSolution": "\n".join([
            "FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }",
            "solvers {}",
            "",
        ]),
    }


def render_decompose_dict(ranks: int) -> str:
    """`system/decomposeParDict` for the layer case.

    `method scotch` WITH NO COEFFICIENTS. A dict copied from a 16-rank case keeps
    `method hierarchical; coeffs { n (4 2 2); }`, and 4x2x2 is 16 exactly - so in an 8-rank case it
    is a hard inconsistency. Every driver that produced a layered mesh used scotch.
    """
    if int(ranks) < 1:
        raise LayerError(f"a decomposition needs at least one process, got {ranks}")
    return "\n".join([
        "FoamFile { version 2.0; format ascii; class dictionary; object decomposeParDict; }",
        f"numberOfSubdomains {int(ranks)};",
        "method scotch;",
        "",
    ])


def serial_complaint(ranks: int) -> str:
    """Empty when a layer pass may be attempted on this decomposition, otherwise why not.

    PARALLEL ONLY, AND DELIBERATELY. Every layer pass that has produced a mesh of this part ran on
    eight ranks over a decomposed case, and the promotion it depends on is a rename of
    `processor*/constant/<region>/polyMesh`. A serial case keeps its regions at
    `constant/<region>/polyMesh` with no processor directories at all, which is a different
    promotion that nothing has ever run.

    A PART THAT PLANS SERIAL IS SKIPPED, NOT REFUSED - see `conjugate.build` and the driver.
    `ranks_for` returns 1 for everything at or below the per-process octree ceiling, which is every
    part in the corpus and every mesh so far delivered, so refusing on this would turn one
    deployment-wide setting into a corpus-wide outage.
    """
    if int(ranks) > 1:
        return ""
    return ("a boundary layer pass has only ever been measured on a decomposed case - 2,522 s on "
            "eight ranks against 10,038 s serial on the same part - and the promotion it needs is "
            "a rename inside the processor directories, which a serial case does not have")


def case_script(*, ranks: int, region: str, dict_dir: str, mpi: str) -> str:
    """The shell the stage runs: promote the region, layer it, put it back.

    EVERY MOVE IS GUARDED, because the failures here are silent.

    THE PROMOTION IS A RENAME, so after it the ONLY copy of the coolant mesh is inside the layer
    case. A first draft began with `rm -rf` on that directory, which meant a rerun of the stage on
    a workspace whose previous pass had failed DELETED THE MESH and then reported a missing file.
    So a layer case that already holds a mesh is refused, and the caller has to say what to do
    about it.

    THE DEMOTION OVERWRITES NOTHING. `mv src dst` where dst is an existing directory moves src
    INSIDE it, so a stale `constant/<region>/polyMesh` turned into
    `constant/<region>/polyMesh/polyMesh` - and the guard that followed, which tested for
    `polyMesh/owner`, passed on the stale mesh. The reconstruct then merged the UNLAYERED mesh and
    the delivery reported layers it did not have. Both halves are tested for explicitly now.
    """
    case = LAYER_CASE
    return "; ".join([
        "set -e",
        # a layer case holding a mesh is the remains of a failed pass, and the mesh in it is the
        # only one there is
        f'if [ -e "{case}" ]; then '
        f'if find "{case}" -name owner -print -quit 2>/dev/null | grep -q .; then '
        f'echo "FATAL: {case} already holds a mesh from an earlier pass; move it back under '
        f'constant/{region}/ before retrying"; exit 2; fi; rm -rf "{case}"; fi',
        f"mkdir -p {case}/system",
        f"cp -a {dict_dir}/. {case}/system/",
        # THE PROMOTION, per processor directory. The glob is tested first, because an unmatched
        # glob stays literal and would otherwise make a directory called `processor*`.
        'ls -d processor0 >/dev/null 2>&1 || '
        '{ echo "FATAL: no processor directories to layer"; exit 2; }',
        f'for p in processor*; do mkdir -p "{case}/$p/constant"; '
        f'test -d "$p/constant/{region}/polyMesh" || '
        f'{{ echo "FATAL: $p has no {region} mesh to promote"; exit 2; }}; '
        f'mv "$p/constant/{region}/polyMesh" "{case}/$p/constant/polyMesh"; done',
        f'test -f "{case}/processor0/constant/polyMesh/owner" || '
        f'{{ echo "FATAL: the promoted case has no mesh"; exit 2; }}',
        f"cd {case}",
        mpi,
        "cd ..",
        # ...AND BACK, refusing to nest if anything is in the way.
        f'for p in processor*; do mkdir -p "$p/constant/{region}"; '
        f'test ! -e "$p/constant/{region}/polyMesh" || '
        f'{{ echo "FATAL: $p/constant/{region}/polyMesh is in the way of the layered mesh"; '
        f'exit 2; }}; '
        f'mv "{case}/$p/constant/polyMesh" "$p/constant/{region}/polyMesh"; done',
        f'test -f "processor0/constant/{region}/polyMesh/owner" || '
        f'{{ echo "FATAL: the layered mesh was not put back"; exit 2; }}',
        f'find "{case}" -name owner -print -quit 2>/dev/null | grep -q . && '
        f'{{ echo "FATAL: a mesh is still in {case}, so not every rank was put back"; exit 2; }}; '
        f'rm -rf "{case}"',
    ])


def read_layer_log(text: str) -> dict:
    """What a layer pass actually did, read off its own log.

    Everything is optional, because the point of this is to be able to say "it reported nothing
    about layers at all".
    """
    body = str(text or "")
    extruding = [(int(a), int(b), float(c)) for a, b, c in _EXTRUDING.findall(body)]
    added = [(int(a), int(b), float(c)) for a, b, c in _ADDED.findall(body)]
    table = {}
    for line in body.splitlines():
        m = _LAYER_ROW.match(line)
        if not m:
            continue
        name = m.group(1)
        if name in ("patch", "-----"):
            continue
        table[name] = {"faces": int(m.group(2)), "target_layers": int(m.group(3)),
                       "mesh_layers": float(m.group(4)), "thickness_m": float(m.group(5)),
                       "thickness_pct": float(m.group(6))}
    illegal = _ILLEGAL.findall(body)
    seconds = _SECONDS.findall(body)
    return {
        "iterations": len(extruding),
        "coverage_pct": (extruding[-1][2] if extruding else None),
        # WHAT THE MOVER WAS EVER OFFERED. The first `Extruding N out of M` line is the decision
        # the extruder makes BEFORE it moves anything: every face where the surface turns by more
        # than `featureAngle` is refused up front. The last line is what survived ten iterations
        # of repair. The two are different questions and only the second was ever published, so a
        # coverage shortfall read as "the mover failed" when it was mostly "the engine declined".
        # MEASURED on job cba7ac69's 16,582,198-face interface: offered 90.8603% on iteration 1,
        # finished at 90.6422% on iteration 10 - the whole of the 9.14% shortfall was the refusal
        # and 0.22 points was the repair.
        "offered_pct": (extruding[0][2] if extruding else None),
        # WHAT THE MOVER DID WITH WHAT IT WAS GIVEN, which is the only part of coverage it
        # controls. MEASURED on job cba7ac69: it kept 15,030,462 of the 15,066,630 faces it was
        # offered - 99.7599% - while the raw coverage read 90.6422% against a 92% floor. The two
        # numbers answer different questions and only the raw one was published, so a near
        # perfect layer pass on a part whose interface mostly cannot carry prisms read exactly
        # like a layer pass that had failed.
        "kept_of_offered_pct": ((100.0 * float(extruding[-1][0]) / float(extruding[0][0]))
                                if extruding and float(extruding[0][0]) else None),
        "coverage_by_iteration": [c for _a, _b, c in extruding],
        "faces_extruded": (extruding[-1][0] if extruding else None),
        "faces_offered": (extruding[-1][1] if extruding else None),
        "cells_added": (added[-1][0] if added else None),
        "cells_added_pct": (added[-1][2] if added else None),
        "patches": table,
        "illegal_faces": (int(illegal[-1]) if illegal else None),
        "seconds": (float(seconds[-1]) if seconds else None),
    }


def complain(report: dict, *, interface_patch: str, n_layers: int = 0,
             cells: int = 0) -> str:
    """Empty when this pass really grew usable layers, otherwise what it actually did.

    NEVER JUDGED BY THE RETURN CODE, and never by coverage alone. With OpenFOAM's shipped `minVol`
    every face of a good microchannel mesh is flagged on length scale, nothing is extruded, and the
    run prints "Finished meshing without any errors" and exits 0. And the delivered mesh's own pass
    extruded 99.7653% of the patch while holding 82,548 illegal faces, because the stack did not
    fit the cell - a failure that shows up in the achieved THICKNESS and the illegal-face count and
    nowhere in the coverage.
    """
    got = dict(report or {})
    if not got.get("iterations"):
        return ("the layer pass reported no extrusion at all, so no layers were added. With "
                "OpenFOAM's shipped minVol of 1e-13 this is what a scaled-floor case looks "
                "like: "
                "every face of a geometrically correct microchannel mesh is flagged on length "
                "scale alone, nothing is extruded, and the run still exits 0")
    cover = got.get("coverage_pct")
    if cover is None or float(cover) <= 0.0:
        return "the layer pass extruded 0% of the interface, so no layers were added"
    row = (got.get("patches") or {}).get(str(interface_patch))
    if row is None:
        return (f"the layer pass wrote no layer table row for {interface_patch!r}, so there is "
                f"nothing that says layers were grown on the conjugate interface; it reported "
                f"{sorted(got.get('patches') or {})}")
    if float(row.get("mesh_layers") or 0.0) <= 0.0:
        return (f"{interface_patch} came back with {row.get('mesh_layers')} layers in the mesh "
                f"against {int(n_layers or row.get('target_layers') or 0)} asked for")
    if not got.get("cells_added"):
        return "the layer pass added no cells, so the mesh it returned is the one it was given"
    return ""


def shortfalls(report: dict, *, interface_patch: str, n_layers: int = 0,
               cells: int = 0) -> list[str]:
    """Where this pass did WORSE than the one that was measured - said, never raised.

    WHY THESE ARE NOT REFUSALS. Every threshold here comes from one hand-built run of one part on
    2026-11-08: 95.553% coverage, 94.6% of the requested thickness, 2.84 of three layers, 2,888
    faces in error over 102,536,327 cells. A different part has its own staircase and its own port
    cuts, and the layer pass MOVES the only copy of the coolant mesh and rewrites it in place - so
    refusing on a three-point coverage miss would throw away a finished mesh that nothing else had
    anything against, and the mesh is what the judgement should be made on.

    `complain` keeps the refusals that mean NOTHING GREW. These mean it grew less well than
    somewhere else, which is a reading and not a verdict.
    """
    got = dict(report or {})
    row = (got.get("patches") or {}).get(str(interface_patch)) or {}
    out: list[str] = []
    cover = got.get("coverage_pct")
    if cover is not None and float(cover) < COVERAGE_FLOOR_PCT:
        said = (f"covered {float(cover):.4f}% of the interface against the "
                f"{COVERAGE_FLOOR_PCT:g}% this engine aims at; the measured run of the other part "
                f"reached 95.553% and the run that was refused for not fitting its cell reached "
                f"90.7146%")
        # WHERE THE SHORTFALL CAME FROM, because the two causes ask for different things. A face
        # the extruder never offered is `featureAngle` declining to grow a prism where the surface
        # turns - raising the angle would recover it and cost quality. A face it offered and then
        # lost is the mover failing, which more iterations or a thinner stack might fix. Without
        # this the reader sees one number under a floor and cannot tell which.
        offered = got.get("offered_pct")
        if offered is not None:
            never = 100.0 - float(offered)
            lost = float(offered) - float(cover)
            said += (f". Of the {100.0 - float(cover):.2f} points missing, {never:.2f} were never "
                     f"offered to the layer pass at all - the extruder declines a face where the "
                     f"surface turns by more than featureAngle {int(FEATURE_ANGLE)} degrees - and "
                     f"{lost:.2f} were offered and then lost over "
                     f"{int(got.get('iterations') or 0)} iteration(s) of repair")
            keep = got.get("kept_of_offered_pct")
            if keep is not None:
                said += (f", so the pass kept {float(keep):.2f}% of every face it was given and "
                         f"the rest of the gap is this part's own shape")
        out.append(said)
    thick = row.get("thickness_pct")
    if thick is not None and float(thick) < THICKNESS_FLOOR_PCT:
        out.append(f"{interface_patch} came back at {float(thick):.1f}% of the thickness it was "
                   f"asked for against {THICKNESS_FLOOR_PCT:g}%; thinning is the mover's only "
                   f"repair and it manufactures hairline cells, and the measured run achieved 94.6%")
    asked = int(n_layers or row.get("target_layers") or 0)
    # PRESENCE IS `is not None`, NOT `> 0.0`, and that was the defect. This read
    # `float(row.get("mesh_layers") or 0.0)` and then guarded on `0.0 < mesh_layers`, so a pass
    # that grew NOTHING - the largest miss this floor can have - fell through the one test that
    # exists to catch it. MEASURED, SYNTHETIC, 2026-11-17: a layer table row reading
    # `fluid_to_solid 16582198 3 0 0 0` with 90,000 illegal faces missed all FOUR floors and came
    # back with THREE sentences; the one it was silent about was the layer count itself.
    # `complain` does stop such a run inside the mesher, which is why this was only ever a defect
    # in the RECORD - and the record is what a customer reads.
    #
    # A MISSING ROW IS STILL MISSING. `row` is `{}` when the log had no layer table, so
    # `mesh_layers` is None there and this floor is skipped for the honest reason - which is the
    # same reading `floors_unjudged` reports under "layers kept".
    mesh_layers = row.get("mesh_layers")
    # STRICTLY LESS THAN, AT THE BOUNDARY, ON PURPOSE. The floor is "at least 75% of the ask", so
    # exactly 75% MEETS it and is not a shortfall. That is not a rounding accident: the real plate
    # kept 2.25 of 3 and `LAYERS_FLOOR_FRACTION * 3` is 2.25 EXACTLY - 0.75 is 3/4 and 2.25 is 9/4,
    # both exact in binary - so this comparison decides that case on the rule and not on a float
    # tail. The plate's other three floors are missed by real margins (88.5826 vs 92.0, 71.9 vs
    # 75.0, 239 vs 200) and this one is met on the nose, which is why the delivered record says
    # three and not four.
    if asked and mesh_layers is not None and float(mesh_layers) < LAYERS_FLOOR_FRACTION * asked:
        out.append(f"{interface_patch} kept {float(mesh_layers):.2f} layers of the {asked} asked "
                   f"for, under {LAYERS_FLOOR_FRACTION * 100:.0f}%; the measured run kept 2.84 "
                   f"of 3")
    bad = got.get("illegal_faces")
    if bad is not None and int(cells) > 0:
        per_m = float(bad) * 1e6 / float(cells)
        if per_m > ILLEGAL_PER_MILLION_CEILING:
            out.append(f"left {int(bad):,} faces in error over {int(cells):,} cells, "
                       f"{per_m:.0f} per million against {ILLEGAL_PER_MILLION_CEILING:g}; the "
                       f"measured run left 2,888 over 102,536,327, which is 28")
    return out


def measured(report: dict, *, interface_patch: str) -> dict:
    """The figures a delivery should publish about its layers, from the pass's own log.

    The ASKED count is in the plan; these are what was ACHIEVED, and they are not the same number -
    the measured run asked for 3 and got 2.84 over 94.6% of the thickness on 95.553% of the
    interface. A report that publishes only the ask is publishing an intention.
    """
    got = dict(report or {})
    row = (got.get("patches") or {}).get(str(interface_patch)) or {}
    out = {
        "layer_coverage_pct": got.get("coverage_pct"),
        "layer_cells_added": got.get("cells_added"),
        "layer_faces_in_error": got.get("illegal_faces"),
        "layer_seconds": got.get("seconds"),
        "layer_iterations": got.get("iterations"),
        "layers_asked": row.get("target_layers"),
        "layers_in_mesh": row.get("mesh_layers"),
        "layer_thickness_m": row.get("thickness_m"),
        "layer_thickness_pct": row.get("thickness_pct"),
        "layer_patch": str(interface_patch) if row else "",
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


__all__ = ["LAYER_CASE", "LAYER_STAGE", "LayerError", "MAX_GLOBAL_CELLS", "MAX_LOCAL_CELLS",
           "MIN_VOL_FRACTION", "min_vol_for", "RELAXED_SHAPE", "MIN_TET_QUALITY",
           "MIN_THICKNESS_OF_STACK",
           "BUFFER_CELLS_NO_EXTRUDE", "N_SURFACE_LAYERS", "EXPANSION_RATIO", "FEATURE_ANGLE",
           "FIRST_LAYER_FRACTION", "MAX_STACK_FRACTION",
           "SMOOTH_SURFACE_NORMALS", "SMOOTH_THICKNESS", "SMOOTH_NORMALS", "COVERAGE_FLOOR_PCT",
           "LAYER_ITER", "LAYER_ITER_DEFAULT", "LAYER_ITER_ENV", "N_RELAXED_ITER",
           "THICKNESS_FLOOR_PCT", "LAYERS_FLOOR_FRACTION", "ILLEGAL_PER_MILLION_CEILING",
           "LAYER_FLOORS", "floors_unjudged",
           "LAYER_PEAK_MARGIN", "max_layers", "interface_cell_m", "geometry_for", "stack_thickness_m", "cells_multiplier", "coolant_multiplier", "WHOLE_MESH_PER_LAYER",
           "COOLANT_PER_LAYER",
           "seconds_estimate", "fit_complaint", "render_mesh_quality_dict", "render_layers_dict",
           "render_support_dicts", "render_decompose_dict", "serial_complaint", "case_script",
           "read_layer_log", "complain", "shortfalls", "measured"]
