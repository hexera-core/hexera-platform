# Responsibility: Mesh a part's sewn mid-surface (cad/midsurface: midsurface.brep + shell.json) with gmsh as a
# quad-dominant shell mesh - each face sized by the shell plan (engines/shell_plan.py), conformal along every
# junction because the faces share their edges - and hand back the engine-neutral ShellMesh: its elements'
# walls, mid-faces, parts and groups, the normals of each sheet turned one way, and its measured quality.
# Boundaries: runs gmsh in this process (the caller decides which process); reads the two files it is given
# and writes nothing. Deterministic: one thread, fixed options.
# Collaborates with: cad/midsurface.py, engines/shell_plan.py, engines/shell_mesh.py, engines/shell_quality.py,
# engines/shell_writers.py.
from __future__ import annotations

import bisect
import contextlib
import functools
import json
import logging
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from meshpipeline.engines import shell_mesh, shell_quality, shell_repair
from meshpipeline.engines import shell_plan as plan_rules
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines import shell_structure as SSM
from meshpipeline.engines.gmsh import shell_structured as ST
from meshpipeline.engines.shell_mesh import ShellMesh, edge_census, orient

logger = logging.getLogger(__name__)

#: gmsh: Frontal-Delaunay, then Blossom recombination into quads; quad-dominant (a few triangles where quads cannot
#: fit). Frontal-Delaunay-for-quads (8) left faces of B-spline mid-surfaces 7 times coarser than their size and
#: their quads past Nastran limits; QuadQS (11) made 3 to 5 times the elements asked for
ALGORITHM = 6
RECOMBINATION = 1
#: SHMESH-42: the 2D algorithm for flat faces left free (None: ALGORITHM)
PLANE_ALGORITHM: int | None = None
#: SHMESH-21g: gmsh's Blossom recombination grows far faster than the elements on ONE big face of algorithm 6's
#: triangles (gmsh 4.15, the 1200 x 800 plate's face alone, one core: 23k quads 10 s, 69k 603 s, 138k 357 s; the
#: product's 200k plate made no mesh in 900 s, all of it in gmsh's 2D mesh); on Frontal-Delaunay-for-quads' (8)
#: right triangles the same Blossom stays near the elements (21k 7 s, 63k 33 s, 125k 102 s, 250k 219 s) with every
#: element a quad and a better mean quality (0.92 against 0.85). A flat face left free planned past BIG_FLAT_FACE
#: elements is meshed by BIG_FLAT_ALGORITHM; under it every face meshes exactly as before. Flat faces only: 8 left
#: B-spline mid-surfaces 7 times coarser than their size (above). 0: off
BIG_FLAT_FACE = 30_000
BIG_FLAT_ALGORITHM = 8
#: SHMESH-49k: gmsh's algorithm for a round plate's paved band (None: the default)
POLAR_BAND_ALGORITHM: int | None = None
#: SHMESH-10: cut every quad past Nastran's own limits into two triangles
SPLIT_PAST = True
#: SHMESH-48e: a gridded face's free boxes may carry one triangle each (inside the tria angle lines) while its
#: triangles stay under this share of its elements
BOX_TRIA_SHARE = 0.01
#: SHMESH-11: cut every closed sewn face at its seams before meshing; a face gmsh still cannot mesh as a periodic
#: surface is re-made as a B-spline (the same surface, exactly) and the mesh made again - at most RESPLINE_MAX faces
SPLIT_CLOSED = True
#: SHMESH-11e: a mesh that fails with the closed faces cut at their seams is made again once with them whole, when the
#: failure came within WHOLE_RETRY_S: on cen_r0430 (a galvanised fitting, 23 cylinders and 6 spheres) and lab_fr017 (a
#: pipe tee) every mid-face is valid and plain gmsh meshes them, but the cut faces gave "the 1D mesh seems not to be
#: forming a closed loop" and no mesh; whole, both mesh valid and inside Nastran's limits
WHOLE_RETRY = True
WHOLE_RETRY_S = 300.0
_WHOLE_NOW = [False]
#: SHMESH-14: before any quad is cut, the inner nodes of flat faces round a quad past a limit are moved towards their
#: neighbours' middle when that brings fewer elements past a limit (at most RELAX_ROUNDS passes)
RELAX_ROUNDS = 6
#: SHMESH-14c: the nodes inside curved faces relax too, each tried point laid back onto the face's surface
RELAX_CURVED = True
#: SHMESH-82: one projector per curved face (its search grid built once) and each point projected once
PROJECT_ONCE = True
PROJECT_SEEN_MAX = 2_000_000
#: SHMESH-82b: the projector searches with OCC's tree of the face's sample points (Extrema_ExtAlgo_Tree) - about 7 times
#: faster than its gradient search, the same closest point to under a micrometre (most to 1e-12 mm)
PROJECT_TREE = True
#: SHMESH-82c: a tried point is laid on its node's face from the node's own place on the surface (a local search from
#: its UV), not by the global search alone: both global searches can settle on a far minimum for a point 1 mm off a
#: B-spline skin (5 to 7 mm from the true closest point on 23 of 1,500 tries on lab_sh0087). A local result further
#: from the node than LOCAL_REACH times the move asked falls back to the global search's solution nearest the node
PROJECT_LOCAL = True
LOCAL_REACH = 1.5
#: SHMESH-13: swap the longest edge of a triangle past a limit with its neighbour's when both come out better
FLIP_PAST = True
#: SHMESH-12: merge the ends of sliver edges (under the floor and a fifth of their elements' longest edges)
COLLAPSE_SHORT = True
RESPLINE_MAX = 16
#: SHMESH-71: ONE FACE NEVER COSTS A WHOLE SHELL MESH - a watchdog on gmsh's 2D mesh, face by face (SHMESH-21's forked
#: child): gmsh says which surface it meshes and how deep its boundary recovery goes; a surface held past
#: FACE_WATCH_S, or whose recovery reaches FACE_WATCH_LEVEL ("splitting those edges and trying again - level N"),
#: stops the child and the mesh is made again with that face one step down FACE_LADDER: meshed by Delaunay, then by
#: MeshAdapt, then at a coarser size, then at a finer one, then re-made as a B-spline (SHMESH-11) - each step under the
#: same watchdog - and a face that stops it past the last step is left out of the mesh, and said (SHMESH-74). Stamped
#: lab parts sat at levels 9 and 10 on one B-spline fillet until the 15-minute stop and got no mesh. At most
#: FACE_WATCH_TRIPS trips in a shell mesh (then it runs unwatched, as before)
FACE_WATCH = True
#: (None: no time trip - a large face meshed slowly is no stuck face: on the flat-plate bar a 69,000 mm2 plate face
#: held past 90 s under load was left out, then the part given up; only gmsh's own recovery levels trip)
FACE_WATCH_S: float | None = None
FACE_WATCH_LEVEL = 6
FACE_WATCH_TRIPS = 10
FACE_WATCH_POLL = 2.0
FACE_LADDER = ("curve", "delaunay", "meshadapt", "coarser", "finer", "bspline")
#: ... the curve rung: a curve gmsh named meshed CURVE_FINER times finer (each further trip naming it, again)
CURVE_FINER = 2.0
#: ... OFF: meshed finer by a transfinite count, the named curve's other face failed in turn (s22: the dome after
#: its fillet; the test sheet: a 236 mm2 face beside the held one) - to be made as a local size, not a count
CURVE_RUNG = False
CURVE_MIN = 8
LADDER_ALGO = {"delaunay": 5, "meshadapt": 1}
LADDER_SIZE = {"coarser": 1.5, "finer": 0.67}
#: SHMESH-72: a face SHMESH-11 re-made as a B-spline that stops gmsh again is not re-made again, and the re-making is
#: held to one budget for the whole shell mesh (RESPLINE_BUDGET_S, every guarded path included; a re-make whose fuse
#: back runs past REBUILD_WATCH_S stops the child): past either, the failing face is left out of the mesh and said,
#: at most FACE_OUT_MAX faces (then the mesh is given up, as before). A curved stamped lab panel re-made 49, 35, 74,
#: 70, 47 (49 again, its area moved 1.2% by the fuse) until the 15-minute stop
REMADE_ONCE = True
#: ... and a face re-made once that stops gmsh again is left out at once (False: re-made again, as before, until the
#: limits SHMESH-11c/11e set; only past them is it left out. On: 2 flat-plate bar faces and 4 of 28 board parts
#: that meshed before were left out and given up)
REMADE_EARLY = False
#: ... the re-making budget for the whole shell mesh (False: each guarded path's own, as before)
REMAKE_RUN_BUDGET = False
REBUILD_WATCH_S = 45.0
FACE_OUT_MAX = 8
#: SHMESH-74: a mesh with faces left out is never told as a plain pass: past FACE_OUT_SHARE of the part's area or
#: FACE_OUT_MANY faces it is "meshed with faces missing" (ours to fix)
FACE_OUT_SHARE = 0.02
#: ... and past FACE_OUT_GIVE_UP of the part's area left out the shell mesh is given up (said), as before the guards
FACE_OUT_GIVE_UP = 0.25
FACE_OUT_MANY = 2
#: this shell mesh's faces treated otherwise (keys: centre, area): "level" [(key, rung)], "out" [key], "remade" [key]
_FACE_FATE: dict = {"level": [], "out": [], "remade": [], "t0": None, "remake_s": 0.0, "ladder_spent": False, "out_path": [], "curves": []}
_WATCH_DIR: list = [None]
#: SHMESH-11b: an analytic face (a cylinder, a cone) whose boundary gmsh's 2D mesher cannot recover ("impossible to
#: recover the edge") is re-made as a B-spline (the same surface, exactly, without its period) and the mesh made
#: again. Seen on a ship's 3 m hull cylinder with 25 mm slots (sh0048): the same face scaled down by 10 meshed, and
#: so did its B-spline at full size - gmsh's handling of the large periodic analytic surface, not the face's shape
RESPLINE_UNRECOVERED = True
#: SHMESH-11c: a face whose 1D mesh gmsh reads as not a closed loop is re-made as a B-spline too
RESPLINE_LOOP = True
#: ... at most this many faces in one mesh (a duct whose faces failed one after another, 14 re-made, then hung gmsh)
RESPLINE_LOOP_MAX = 4
#: SHMESH-11e: no face is re-made once the meshes so far have taken this long (each re-made face meshes it all again)
RESPLINE_BUDGET_S = 300.0
#: SHMESH-11g: faces on offset surfaces are meshed as B-splines
OFFSETS_AS_SPLINES = True
#: SHMESH-20: the most elements a shell plan may ask for (area / size^2 summed over its faces; gmsh makes about 1,000
#: a second here, and its counts run 0.4 to 3.3 times the plan's on the corpus)
PLANNED_MAX = 1.5e6
#: SHMESH-21: a shell mesh is stopped after this long (0: unbounded) - since rulebook 80 the FLOOR of its limit: a mesh
#: whose work is priced longer (SHMESH-21e) is given its price
MESH_DEADLINE_S = 900.0
#: SHMESH-21e: THE LIMIT FOLLOWS THE WORK - a shell mesh's work is priced at PRICE_FACTOR times its planned elements at
#: MESH_RATE a second (SHMESH-20's measure), or PRICE_FACTOR times its first whole mesh's measured seconds once one is
#: made (this part's own rate and count), whichever is longer (TIME-2: a guard is 3x the cost); a price past
#: MESH_DEADLINE_S is the mesh's limit, from its start, never past the run's hard cap (SHELL_CAP_ENV). A plan of 1.5M
#: elements (PLANNED_MAX) is priced at 7,500 s: under the old 900 s it could not finish one mesh.
#: The book rate is the slowest whole-path rate measured on the big plate after SHMESH-21g (test VM, one core, first
#: whole mesh: 20k 1,700 / 56k 1,300 / 108k 1,380 / 215k 930 / 473k 620 elements a second), rounded down to 600
#: (1,000 before: the 473k plate took 764 s against a 1,350 s price, a margin of 1.8 where TIME-2 asks 3)
MESH_RATE = 600.0
PRICE_FACTOR = 3.0
#: SHMESH-21f: A STUCK FACE IS STILL CAUGHT - while a limit runs past MESH_DEADLINE_S, a face gmsh's output shows it
#: on (the parent reads "Meshing surface N" in the watched child's output: a new surface is progress) for longer than
#: PRICE_FACTOR times its own planned elements at MESH_RATE, and never under FACE_HELD_FLOOR_S, trips SHMESH-71's
#: watchdog (its ladder, then left out, said). A watched child only: an unwatched one is held to the floor's limit
FACE_HELD_FLOOR_S = 300.0
#: SHMESH-17: two triangles of a face that together make a quad inside Nastran's own limits are joined into it
PAIR_TRIAS = True
#: SHMESH-18: a triangle walked through at most WALK_DEPTH quads of its face to meet another (0: off)
WALK_DEPTH = 6
#: SHMESH-22: a cap triangle past a limit taken into the element across its longest edge, or dropped from a free edge
ABSORB_CAPS = True
#: SHMESH-19: every face's boundary given an even number of segments before the 2D mesh (Blossom's condition)
EVEN_FACES = True
#: at most GROW_GROUPS grading sizes; past that, sizes taken down to steps of GROW_STEP (a quarter octave)
GROW_GROUPS = 48
GROW_STEP = 2.0 ** 0.25
#: how closely gmsh integrates the size field along a curve to place its nodes: gmsh's own 1e-9 kept a ship's 1D mesh
#: (24,000 curves, sh0048) going for over 30 minutes; 1e-3 places nodes to a thousandth (counts within 0.2%)
LC_PRECISION = 1e-3
#: SHMESH-16: narrow four-sided strips meshed as mapped grids, stretched along them up to STRETCH_MAX times their
#: size across (Femap's aspect target: 5); a strip's opposite sides alike within STRIP_ALIKE, its long sides at
#: least STRIP_LONG times its ends
MAP_STRIPS = True
STRETCH_MAX = 5.0
MAP_FLAT = False
STRETCH_FLAT = 1.0
STRIP_ALIKE = 0.05
STRIP_LONG = 1.5
#: SHMESH-38: SHMESH-12 reads a mapped strip's edges as slivers only under SHORT_SHARE of the floor (walls stream,
#: census DEV 124 -> 130 fully good: ~/shellcamp/walls/REPORT.md, run walls1m)
MAPPED_SLIVER_SHARE = True
EVEN = True
#: SHMESH-3/4 through gmsh's own curvature sizing: elements per full turn of every curve and curved face
PER_TURN = 4 * plan_rules.CURVE_PER_QUARTER
#: SHMESH-51o78: the one-ring trial starts only with time left for itself and every spot pass the mesh may take
TRIAL_OWES_ALL_SPOTS = True
#: SHMESH-27 (under the user's own sizes only): the size beside a CAD edge shorter than the size, and on a face
#: narrower than it, is held at shell_plan.FEATURE_RATIO times the feature and grown from there
FEATURE_SIZES = True
FEATURE_EDGES = True
#: SHMESH-51o63 (lab, off): under a shortest edge the user typed, a face narrower than it has one element across and,
#: along it, elements as long as the user's aspect target allows (the plan's stretch times its width, never over their
#: longest edge); a CAD edge shorter than it is graded from the plan's stretch times its length, not FEATURE_RATIO times
NARROW_ALONG = False
#: SHMESH-51o65: with NARROW_ALONG, a face keeps its stretched size only where it and the faces sharing an edge with it
#: are no worse on any line than with today's sizing (elements past an outer line, past a target beyond the line's
#: allowance, triangles, past the solver's limits): one mesh with today's sizing to compare, the faces that are worse
#: sent back together, NARROW_ROUNDS at most
NARROW_KEEP = True
NARROW_ROUNDS = 1
#: (the mid-faces, by name, whose stretched size went back: set by _narrow_kept_path for one mesh)
NARROW_SKIP: frozenset[str] = frozenset()
#: (faces: measured on the 80 lab parts, free-meshed narrow faces gave 4,300 more triangles and 3 parts past Nastran's
#: limits for the same parts inside the list; off until narrow faces of more than four sides are mapped)
FEATURE_FACES = False
#: SHMESH-37: a sheet drawn as many CAD patches is sized by the sheet (shell_plan.SHEET_SMOOTH_DEG) - on surface
#: files (record "source": "surface", PR #434's route, not on main); on extracted mid-surfaces too only with
#: SHEET_REGIONS_ALL (a change to the rules' own mesh: off until signed off)
SHEET_REGIONS = True
SHEET_REGIONS_ALL = False
#: SHMESH-31: how many times faces holding elements past the solver's own limits under the user's sizes are meshed
#: again at our own (finer) sizes there
OWN_SIZE_RETRIES = 2
#: SHMESH-31c: the passes the steps may add (half, a quarter, an eighth before h0)
SPOT_STEP_PASSES = 2
#: SHMESH-31b: past SPOTS_MAX boxes the worst spots are kept (by how far their elements are past the solver's limits),
#: the rest left to the clean-up and told - a face's boxes were joined into one box the size of the face: on a flange
#: (fr014) 305 elements past made 77 boxes, joined to 8 face-wide ones meshed at 0.5 mm (global 3.2): 875,000 elements
#: planned in the boxes against a mesh of 51,800, and gmsh never came back
SPOTS_MAX = 64
#: SHMESH-31d: a spot's box covers its elements past the solver's limits and one element of the new size round them
#: (SPOT_PAD_H times h0), no more: the new size is what must fit round them; the box padded by the old element's
#: longest edge (SPOT_PAD 1, as before) made fr014's 61 boxes ~250 x 7 x 7 mm strips and took the mesh 52,643 ->
#: 158,109 elements. Measured on fr014 side by side (every variant inside Nastran): pad 1 / 0.5 / 0.25 x the longest
#: edge 158,109 / 120,186 / 105,741; none + 1 / 2 new sizes 102,498 / 107,649; 0.25 + 1 new size 110,923
SPOT_PAD = 0.0
SPOT_PAD_H = 1.0
#: SHMESH-31e: a box whose pass leaves an element past within its reach (its elements' longest edge round it, the
#: padding before SHMESH-31d) is grown to that reach for the next pass - a tight box that cleared its own elements
#: but moved the fault to the next element along the same short curve (r0091: two slivers at the ends of a small
#: corner arc; the tight boxes left one in the middle, the old padding covered the arc)
SPOT_GROW = True
#: SHMESH-31d (lab): the grading out of a box (gmsh's Box Thickness) at this share of the distance the growth needs.
#: 0.6 on fr014: 157,409 against 158,109 (no gain)
SPOT_GRADE = 1.0
#: SHMESH-31d (lab): each box held to its own face by a Restrict field (False: everywhere it reaches; "faces": its
#: surfaces; "curves": and their curves) - its grading kept off the neighbours. Measured on fr014 (pad none + 1 new
#: size): 101,201 elements against 102,498, but 5,710 triangles against 3,250 (the neighbours meet the fine edge
#: abruptly); off
SPOT_OWN: bool | str = False
#: SHMESH-31b: the spot plan is priced before gmsh (each box's two largest extents over its size squared) and kept to
#: this many times the mesh it re-makes, worst spots first
SPOT_PLAN_MULT = 3.0
#: SHMESH-31b (lab): a spot's size never under the user's shortest edge less its tolerance. Off: the solver's limits
#: come before the user's edge lines, and fr014's elements past them sit on strips and fillets that only the face's
#: own size (0.5 mm against a 1.28 mm floor) clears
SPOT_FLOOR = False
#: SHMESH-31b: a spot pass (and a trial of SHMESH-36 / SHMESH-45) runs under the time left to the shell mesh's
#: deadline (MESH_DEADLINE_S) less this margin for what must follow it (the record, the mesh sent back); past it the
#: best mesh so far is kept and told. A fixed 40% of the deadline (360 s) stopped fr014's spot pass, which takes
#: 374-388 s on C15 and would have finished
TIME_MARGIN_S = 30.0
#: SHMESH-31c: a spot is refined in steps - its first pass at half the size there, a spot still past after it at a
#: quarter, and so on down to the face's own rules' size (h0) - never straight to h0: on a flange meshed at 3.2 mm
#: (fr014) h0 is 0.5 mm (half a 1 mm wall, SHMESH-2's own size), 6.4 times finer, and the spot pass tripled the mesh
#: (52,600 -> 158,100 elements) to clear 890 elements past Nastran's limits. Off: every spot at h0, as before.
#: Off (lab only) while a spot pass re-meshes the whole part: each step is a whole mesh more (fr014 330 s), and a
#: step that clears nothing costs one more pass where h0 straight away gave the same mesh (sh0010: 2 passes a path
#: for 1, the same 2,060 elements; sh0011: 4 for 2)
SPOT_STEPS = False
#: SHMESH-36: a mesh left past a limit is set against #435's steps' mesh, the better kept; FORCE_CLASSIC (lab only):
#: #435's steps alone. With #437 in the tree "#435's steps" are #435's plus #437's local fuse-back (SHMESH-30,
#: LOCAL_REBUILD, left on by _classic): the mesh the tree makes without this PR's steps
CLASSIC_CHECK = True
FORCE_CLASSIC = False
#: SHMESH-33: a count coarser than our rules coarsens gmsh's curvature sizing too
COUNT_CURVATURE = True
#: SHMESH-35: the walls of beads, darts and steps keep shell_plan.FLANK_ACROSS elements across under a coarser size
FLANKS = True
FLANK_SHARED = 2.0
FLANK_BENDS = 2
#: SHMESH-27/31: a face narrower than the floor takes the floor under the user's size (as with no notes) - every
#: such face when True; else only those holding elements a first mesh left past the solver's own limits (SHMESH-31).
#: All of them: fr014 inside Nastran's limits, but the welded tube frames' 0.5 mm corner faces at the floor left 16
#: quads past Q4_IAMAX/IAMIN/TAPER on each of sh0109, sh0110, sh0113 (none before)
SLIVER_FLOOR = False
#: SHMESH-34: under the user's sizes, how far element centres stand off curved faces is measured and told (at most
#: CHORD_SAMPLES elements a face are read)
CHORD_REPORT = True
CHORD_SAMPLES = 200
#: SHMESH-32: gmsh's own fallback when its 2D mesher reports a face failed - gmsh by default meshes that face again
#: with MeshAdapt - is off (0), and the face keeps its Frontal-Delaunay mesh, checked by every step after as any other.
#: MeshAdapt hung over an hour on one 5,000-element face (d2 t0057; 19 s, valid and inside Nastran's limits with the
#: fallback off) and took 134 s of a 2,030-element part's 135 (d2 t0044). None: gmsh's own default
ALGO_SWITCH = 0
#: SHMESH-28: the last clean-up (shell_quality.mend) under the user's limits; then relax and mend again, at most
#: MEND_CYCLES times
MEND = True
MEND_CYCLES = 3
#: ... in which an edge over the user's longest edge (under their shortest) costs as an element in a band does
MEND_LONG = True
MEND_SHORT = False
#: SHMESH-28e: under the user's shortest edge (on a mid-surface extracted from a solid: the stamped steps' SHORT_FIRST
#: and SHORT_SUPPRESS hold a surface file), after the mend under the user's lines, our own elements under it are
#: mended in a pass of their own (shell_quality.mend with a ShortGate): the part's own short edges skipped (both ends
#: on CAD edges - across two, or along one shorter than the shortest edge or a hole too small for HOLE_PER_TURN of
#: it); each change kept only where, face by face, no count gets worse - valid, the solver's limits, every user line
#: and its outer limit, the edge lines, the triangles - and the elements under the shortest edge fall; no merge or
#: re-cut on a grid, a mapped strip or a ring's face; each cycle priced on the run's one clock (SHMESH-31g / 31f).
#: MEND_SHORT (the whole mend weighing the shortest edge) put 5,024 more merges on 136 board parts and +1,122
#: triangles where the passes were the same; 54% of the triangles added landed on faces whose every short element is
#: forced, 9% on faces that came to meet the line
SHORT_MEND = True
#: SHMESH-28e: where the pass may add triangles - "none" (no face takes a triangle more), "met" (a face may, when it
#: ends with none of our own elements under the shortest edge; tried with triangles everywhere, then again with the
#: faces that did not end so held to none, until none is left)
SHORT_MEND_TRI = "none"
SHORT_MEND_TRI_TRIES = 3
#: ... and a node of an element outside a line may move to where that element is well shaped (shell_quality.mend)
MEND_MOVES = True
#: SHMESH-14d (under the user's limits): the nodes on a straight CAD edge relax too, sliding along their edge, never
#: nearer its ends than SLIDE_ENDS of its length
SLIDE_EDGES = True
SLIDE_ENDS = 0.01
#: ... but not at the corners of a mapped strip (a mapped grid cannot grade), and no strip is left free for one
FEATURE_AT_STRIPS = True
FEATURE_UNMAP = False
#: SHMESH-60 (under the user's shortest edge): a four-sided strip narrower than the user's shortest edge (a stamped
#: part's fillet, a narrow flank, a run-out's sliver) is mapped with one row across - its long sides alike within
#: NARROW_ALIKE (a fillet round a bead's end has unequal arcs), its ends no longer than its long sides - and then
#: CLOSED to its middle line: each pair of nodes across it becomes one node on the strip's surface, so the faces either
#: side meet there and no element keeps an edge across the strip. Strips are closed narrowest first, each while the
#: row it would leave (its own width plus half of every closed neighbour's) is under the shortest edge.
NARROW_COLLAPSE = True
#: SHMESH-61: SHMESH-35's flank count held over the user's shortest edge
FLANK_LOW = True
#: SHMESH-62: under the user's shortest edge, the CAD edges shorter than it are not graded to below it, and the mend
#: may merge their ends (SHMESH-29's "ignore edges under X" with X their shortest edge)
SHORT_SUPPRESS = True
#: ... and with it every element edge under the shortest edge costs in the mend as when the user asks to ignore edges
SHORT_COST = True
#: SHMESH-64: under the user's limits, the triangles left after the mend are paired and walked once more
REPAIR_AFTER_MEND = True
#: SHMESH-65: under the user's sizes a strip one element across is mapped with its long sides alike within
#: NARROW_ALIKE (not STRIP_ALIKE)
ONE_ROW_ALIKE = True
#: ... a strip whose ends are under FEW_ROWS times its size (at most a few rows across)
FEW_ROWS = 2.5
#: ... and with long sides more unequal than NARROW_ALIKE, when one count fits both inside the user's edges
UNEQUAL_FEASIBLE = True
#: SHMESH-69: a strip tapering to a point is mapped as a three-cornered transfinite face. OFF: on a domed run-out
#: lab part (s21) the tapers came out as quads but the flats beside them took the tapers' counts and paved with
#: triangles (7.4% -> 12.4%)
TAPERS = False
#: SHMESH-70: the mend never makes an edge over the user's longest edge (times 1 + LONG_BAND; 0: as written)
LONG_GUARD = True
LONG_BAND = 0.0
#: SHMESH-73: a few-row strip whose long sides cannot share one count steps between two counts (rows of the coarse
#: count, the last row three-to-one steps), in place of paving
TRANSITIONS = True
#: SHMESH-90: THE BETTER STEP - a stepped strip's transition row is made of two-to-one steps (two coarse segments
#: meeting four, six quads) as well as three-to-one ones, placed where the coarse and fine sides' nodes stay in
#: step, as placed or smoothed; the best judged is kept (off: SHMESH-73's three-to-one steps spread by count)
STEP_PATTERNS = True
#: SHMESH-90: a step whose own edges come out under the user's shortest edge is refused (off: a mapped strip's
#: structure stands above the user's edge targets; on: the edge targets are held, the strip paved)
STEP_OWN_LOW = False
#: SHMESH-90 per strip (SHMESH-76's split, as SHMESH-77's per face): the steps kept on the strips where they and
#: their neighbours carry no more triangles than without the steps
STEP_PER_STRIP = True
#: SHMESH-91: THE PER-FACE KEEP - across SHMESH-76 candidates, each face takes its best mesh where its boundary nodes agree
#: (within POS_TOL mm); the stitched mesh is kept only when it stands better than the one SHMESH-76 kept
FACE_KEEP = True
POS_TOL = 1e-6
#: SHMESH-40's freeing of every grid beside a free face with an element past the solver's limits (off as a stamped
#: step, lab trial: STAMPED_SWITCHES puts it back on with the rest off)
GRIDS_FREED_BESIDE = True
#: SHMESH-75: a strip's count yields to what a few-row neighbour sharing its side can take (one count or a step),
#: when a count inside its own edges does; the mesh is made both ways and the better kept (_standing, then fewer
#: triangles). A stamped panel's fillet (long sides 28 and 10.5 mm, 2 to 6 mm edges: 5 and 7 a step) stayed paved
#: because the strip beside its 28 mm side had counted it 6
YIELD_COUNTS = True
#: SHMESH-76: the newer strip steps (SHMESH-73, SHMESH-75) are kept only where the mesh with them stands no worse
NO_WORSE = True
#: SHMESH-73: a planned step that cannot be made sends the pass round again with that face free (no step counts).
#: OFF: on the lab the paving to a failed step's counts was better than the face free (s01 38 -> 48 triangles,
#: s10 5 -> 54 short-edge elements); SHMESH-76 already drops the steps where they make a line worse
STEP_RETRY = False
#: SHMESH-78: a face of more than four curves that turns at exactly four vertices (CORNER_TURN or more) and runs on
#: smoothly at the rest (under SMOOTH_TURN) is mapped on those four corners
CORNER_MERGE = True
CORNER_TURN = 45.0
SMOOTH_TURN = 20.0
_CORNER_MERGED = [0]
#: SHMESH-77 per face: with and without the fit made, the fit is kept only on the faces where it and its neighbours
#: carry no more triangles than without it (one mesh more)
FIT_PER_FACE = True
#: SHMESH-79: a three-cornered face is three quad blocks from a centre point (counts on its sides of even sum)
TRI_BLOCKS = True
#: SHMESH-79: a degenerate curve (no end vertices) is a point of its face's loop, so a four-curve face with one is
#: three-sided and reaches the three-block rule (before: "its two other sides share a corner", paved)
DEGENERATE_CORNER = True
_TRI_PLAN: dict = {}          # surface tag -> (three chains of curves, their counts, the three corner vertices)
_STEP_FAILED: list = []     # (centre, area) of the faces whose step failed in this shell mesh
#: SHMESH-77: under the user's sizes a four-sided face is mapped whenever one count per pair of opposite sides keeps
#: every segment inside the user's edges, the shortest edge given LOW_BAND (whatever the ratio of the sides)
FIT_MAP = True
#: SHMESH-93: in the one-count tests (SHMESH-65's unequal strips, SHMESH-77's fit), a side shorter than the user's
#: shortest edge fits one segment (_most_segments): any mesh of the face has it as one edge
SHORT_SIDE_ONE = True
LOW_BAND = 0.0
_FIT_MAPPED = [0]
_FIT_FACES: list = []         # SHMESH-77: the sewn faces (fact index) this mesh fit-mapped
_FIT_NEAR: dict = {}          # SHMESH-77: fact index -> the fact indices of it and its neighbours
_FIT_EXCLUDE: list = []       # SHMESH-77: (centre, area) of faces not to fit-map in this try (their own lines worse)
_STEP_EXCLUDE: list = []      # SHMESH-90: (centre, area) of strips not to step in this try (they cost triangles)
_STEP_NEAR: dict = {}         # SHMESH-90: stepped strip's fact index -> the fact indices of it and its neighbours
#: the user's quad targets in force for this mesh (shell_quality.InForce.aims rows), for SHMESH-77's shape check
_AIMS_NOW: list = [()]
_YIELDED = [0]
#: SHMESH-75: this mesh's few-row strips in a count conflict and what became of them (sizes and counts only)
_STRIP_LOG: list = []
#: the record of why each four-sided face was left free by _map_strips (quality.free_why; sizes, counts, angles)
_FREE_WHY: dict = {}
_YIELD_OFF = [False]
#: SHMESH-60..69 on extracted mid-surfaces too (off: on the 80-part flat-plate bar they lost 11 lines on 6 parts and
#: ran 2 to 5 times slower on a few; they are on for surface files, where stamped sheets come)
STAMPED_ALL = False
#: SHMESH-60: a strip to be closed is sized as the faces it joins (no grading from it)
CLOSING_SIZE = True
#: SHMESH-66: under the user's shortest edge the relaxing aims at it too
RELAX_SHORT = False
#: SHMESH-67: the short edges mended first with no new triangle; then the mend weighs the user's lines alone
SHORT_FIRST = True
#: SHMESH-68: a strip is closed only where no element round it comes out past a limit of the user's list (solver
#: limits, outer limits, longest edge) it was inside
CLOSE_GUARD = True
#: SHMESH-94: ... where a quad round the strip that comes out past a solver limit is one SHMESH-10's cut mends (two
#: triangles inside every solver limit and outer line), the strip is closed
CLOSE_CUT_MENDS = True
#: SHMESH-63: a mapped strip never takes more segments than leave each over the user's shortest edge
STRIP_LOW = True
NARROW_ALIKE = 0.5
CLOSE_ROUNDS = 4


class _Periodic(Exception):
    """gmsh could not mesh sewn face `face` (0-based, in the surface's own order) as a periodic surface."""

    def __init__(self, face: int, *, kin: bool = False, loop: bool = False) -> None:
        super().__init__(f"face {face}")
        self.face = face
        self.kin = kin      # re-make every sewn face on the same surface with it (SHMESH-11b)
        self.loop = loop    # gmsh read the face's 1D mesh as not a closed loop (SHMESH-11c)


class _UnderLow(Exception):
    """SHMESH-51o58: the face comparison of the rings under the shortest edge wants another mesh: keys to add to or
    take from the faces refused, and holes to send to one ring."""

    def __init__(self, add: set[int] | None = None, remove: set[int] | None = None, sent: list | None = None,
                 why: str = "") -> None:
        super().__init__(why or "rings under the shortest edge compared")
        self.add, self.remove, self.sent, self.why = set(add or ()), set(remove or ()), list(sent or ()), why


class _UnderLowKeep(Exception):
    """SHMESH-51o59: no face is worse with its rings under the shortest edge: the mesh kept before the one-ring mesh
    is delivered (`keys`: the one-ring keys to take back)."""

    def __init__(self, keys: set[int]) -> None:
        super().__init__("rings under the shortest edge kept")
        self.keys = set(keys)


class _NoCut(Exception):
    """SHMESH-47b: gridded faces (sewn face indices) whose lines cut a joined face into triangles or elements past the
    solver's own limits: the mesh is made again with those faces left ungridded (the neighbours whole)."""

    def __init__(self, faces: set[int], why: dict[int, dict] | None = None, holes: list | None = None,
                 hole_faces: list | None = None, hole_why: list | None = None) -> None:
        super().__init__(f"{len(faces)} gridded face(s)")
        self.faces = faces
        self.why = why or {}
        self.holes = holes or []        # (SHMESH-51c: bolt holes whose two-ring box failed in a grid: one ring there)
        self.hole_faces = hole_faces or []  # (SHMESH-51f: each such hole's sewn face: its rounds are its face's own)
        self.hole_why = hole_why or []  # (SHMESH-51h: why each takes one ring)
        self.unmap: list = []           # (SHMESH-51f: grid cells of other faces found past the limits on the same pass)


class _Regrid(Exception):
    """SHMESH-48x: faces (sewn face indices) refused for their short edges or triangles, now meshed free and no better
    there: the mesh is made again with their grids back (kept, not judged on those two again)."""

    def __init__(self, faces: set[int], why: dict[int, dict], wanted: list | None = None) -> None:
        super().__init__(f"{len(faces)} grid(s) back")
        self.faces = faces
        self.why = why
        # (SHMESH-48l2: [(centre, sewn face)] - the holes this pass's rings asked to be drawn again as one circle)
        self.wanted = wanted or []


class _Redraw(Exception):
    """SHMESH-48l2: holes drawn again (SHMESH-45k one circle, 49c O-grid seams) as soon as the rings were built, on a
    pass that could end no other way: the clean-up of a mesh the redraw throws away is not made."""


class _Restep(Exception):
    """SHMESH-73: planned steps that could not be made: the pass is made again with those faces free."""


class _Unmap(Exception):
    """SHMESH-40: mapped faces (gmsh surface tags) whose grid came out with a quad past the solver's own limits: the
    mesh is made again with them free."""

    def __init__(self, faces: set[int], keys: list | None = None, told: list | None = None) -> None:
        super().__init__(f"{len(faces)} mapped face(s)")
        self.faces = faces
        self.keys = keys or []          # (SHMESH-51f: the same cells by keys that outlive gmsh's tags, _cell_keys)
        self.told = told or []          # (SHMESH-51o27: SHMESH-51o24's cells freed for a fault, in words)


#: SHMESH-51o27: the fields of a refusal the run's record keeps by pass, and how many entries
REFUSAL_KEYS = ("kind", "why", "then", "triangles", "elements", "line", "worst", "limit", "offender")
REFUSAL_LOG_MAX = 60
#: SHMESH-51o39: a free cell's chain opposite a two-ring box side takes the box side's count where it fits
CHAIN_TAKES_BOX = True
#: SHMESH-51o44: ... with counted pieces in the chain kept, and uncounted pieces shared with another free cell taken too
CHAIN_SHARED = True
#: SHMESH-51o46: a free four-sided cell of a gridded face whose opposite sides disagree is evened - its uncounted
#: pieces, or a whole band of grid rows through it, take the other side's count where every edge stays in the band
CELL_EVEN = True
#: SHMESH-51o48: ... and never with segments coarser than the size the rules gave the faces (within EVEN_SIZE_TOL)
EVEN_NO_COARSER = True
EVEN_SIZE_TOL = 0.05
#: SHMESH-51o46: the most curves one band of grid rows may hold to be re-counted as one
BAND_CHAIN_MAX = 400


def _band_chain(gmsh, c: int, grids, stop: set[int], limit: int = BAND_CHAIN_MAX) -> list[int] | None:
    """SHMESH-51o46: the band of grid rows a grid-counted curve lies on - the curve, the side opposite it in each
    mapped cell it bounds, and so on - so the band is re-counted as one and every cell on it stays a grid. None when
    a cell on it is not four-sided, a side on it is in pieces, the band reaches a curve of `stop` (a box's or a ring's
    face), or it is longer than `limit`."""
    chain, seen, todo = [c], {c}, [c]
    while todo:
        x = todo.pop()
        if any(int(f) in stop for f in gmsh.model.getAdjacencies(1, x)[0]):
            return None
        for g in (int(f) for f in gmsh.model.getAdjacencies(1, x)[0] if int(f) in grids):
            got = ST._four_sided(gmsh, g)
            if got is None:
                return None
            sides = got[0]
            at = [i for i, sd in enumerate(sides) if x in sd]
            if len(at) != 1 or len(sides[at[0]]) != 1 or len(sides[(at[0] + 2) % 4]) != 1:
                return None
            y = sides[(at[0] + 2) % 4][0]
            if y not in seen:
                seen.add(y)
                chain.append(y)
                todo.append(y)
                if len(chain) > limit:
                    return None
    return chain
#: SHMESH-51o31: triangles under the user's triangle angle target polished by nudging their free nodes
TRIA_POLISH = True
TRIA_POLISH_ROUNDS = 4
TRIA_POLISH_MAX = 200
#: SHMESH-51o32: ... then re-cut with a quad beside them; and told in the record (at most TRIA_TOLD_MAX)
TRIA_RECUT = True
TRIA_TOLD_MAX = 40
#: SHMESH-51o29: THE USER'S EDGE LENGTHS ARE TARGETS unless they say hard (the customer, 19 Nov: "it's okay to go
#: below this in some places ... it doesn't need to be a hard min"; their own accepted mesh of the same structure has
#: 3.3% of its elements under the shortest edge and 1.1% over the longest): an edge within EDGE_TOL outside the target
#: is no fault, and a face is refused for its edge lengths only when more than EDGE_SHARE of its elements are outside
#: that band. The counts stay in per_face and the report.
EDGE_TOL = 0.2
EDGE_SHARE = 0.05
#: SHMESH-40: how many times a mesh is made again with its bad grids free
UNMAP_ROUNDS = 2
#: SHMESH-51o24: before SHMESH-51b, the mapped or transition cells a face's faults lie in are meshed free (while fewer
#: than CELL_FIRST_MAX cells freed in all)
CELL_FIRST = True
CELL_FIRST_MAX = 40
#: SHMESH-51o27: the mark on the keys of the cells SHMESH-51o24 freed (only they are kept from being transition cells:
#: skipping every freed cell's key un-made all of a real column's transition cells, 6 cells and 12 triangles)
CELL_FIRST_MARK = "51o24"

#: SHMESH-51f: a pass gathers its faults before it starts again (the grid cells past the limits found with a refusal
#: or a bolt hole's one ring), keeps the cells it frees by keys that outlive gmsh's tags (sewn face, centre, area) and
#: carries them across a redraw or a start again; each face counts its own bolt-hole rounds, and a face's fault
#: never stops another face's holes from taking one ring alone
GATHER = True
#: (SHMESH-51f: how near a cell's centre, mm, and its area, relative, must be to a kept key's to be that cell)
KEY_MM = 0.01
KEY_AREA = 1e-4
#: SHMESH-51g: a gridded face refused after meshing for elements past the solver's limits or over the user's longest
#: edge, all in or beside two-ring bolt boxes, sends those holes alone to one ring (else SHMESH-51b, the whole face);
#: beside = within BOX_BLAME_REACH box half widths of the box's centre (one box width past its side)
BOX_BLAME = True
BOX_BLAME_REACH = 3.0
#: SHMESH-51h: with GATHER, how many rounds of one ring a face may have (each round sends every hole a fault is put on
#: at once; the rounds go on while they send holes - the fixed 3 had thrown away a web's 41 made bands for 5 holes)
HOLE_ROUNDS_MAX = 8
#: SHMESH-51o: a mesh whose faces with two rings round bolt holes carry triangles is made again with one ring round
#: those faces' bolt holes (SHMESH-51b's way); the one with fewer triangles is kept, unless it is worse on the solver's
#: limits or the user's lines. Then, when two rings still stand and triangles are left anywhere, the same with one
#: ring round every bolt hole: the mesh never has more triangles than one ring a hole would give
BOLT_TRIA_GUARD = True
#: ... a trial starts only while the run has used under GUARD_START of the shell mesh's time (MESH_DEADLINE_S), and is
#: let go (the mesh before it stands) once the run passes GUARD_TIME of it
GUARD_START = 0.4
GUARD_TIME = 0.65
#: SHMESH-51o2: in that trial the faces' bolt holes, now one ring each, still own their boxes (no line ends on them, each
#: side one curve at its band's count): taken back to one ring by SHMESH-51b's way alone, the other holes' lines ended
#: on their boxes and cut each side 31.7 + 13.7 mm, the box cells were paved (8 triangles a box) and the real column's
#: trial came out with 108 triangles where a6cdf476's one ring had 71
GUARD_OWN = True
#: SHMESH-51o4: the first trial is the face with the most triangles of its own, alone (a 12-hole plate whose bands were
#: all made went to one ring with the long plate beside it, whose two rings left the triangles)
GUARD_PER_FACE = True
GUARD_MAPPED = 0.99
#: SHMESH-51o4: a trial starts with the holes drawn again so far still drawn (a hole's circle is the CAD's whatever its
#: rings) instead of from the CAD - off (SHMESH-51o5): on the real column the long plate's trial alone came out with 44
#: triangles this way and 38 from the CAD (the freed cells' keys a redraw lets go stayed kept)
GUARD_CARRY = False
#: ... and the other bodies' grids left out and back kept so (off: on a lab part it changed the trial's own result -
#: bodies of a column are joined, a face's rings change the counts through its joints)
GUARD_CARRY_GRIDS = False
#: SHMESH-51o5: a trial switches to one ring only faces whose own triangles drop with it
GUARD_OWN_GAIN = True
#: ... a face is a candidate only with triangles of its own within GUARD_NEAR two-ring outer radii of its two-ring holes
GUARD_NEAR = 4.5
#: SHMESH-48x3: a grid refused for its share under the user's shortest edge comes back when it has no triangle, its
#: free mesh has triangles and the shortest-edge line is no worse with it (the user's choice, 18 Nov)
GRID_ZERO_TRIA = True
#: SHMESH-51o21: ... and on a face with holes such a grid is not meshed free to find that out
GRID_ZERO_KEEP = True


def _tria_faces(mesh, applied: dict, owners: list[str], everywhere: bool = False) -> list[int]:
    """SHMESH-51o: the sewn faces of the mid-faces that hold two-ring bolt holes - those with triangles of their own
    (`everywhere`: all of them, when the mesh has triangles anywhere)."""
    lines = ((applied.get("quality") or {}).get("structure") or {}).get("hole_list") or []
    two = {str(h.get("face")) for h in lines if h.get("bolt") and int(h.get("rings") or 0) >= ST.BOLT_RINGS}
    if not two or not len(mesh.trias):
        return []
    q = len(mesh.quads)
    with_t = {str(m) for m in mesh.mid[q:]}
    if GUARD_NEAR and not everywhere:
        # (SHMESH-51o5: triangles of its own near its two-ring holes - a face whose triangles lie away from its rings
        # cannot lose them to one ring)
        near = _near_trias(mesh, applied)
        with_t = {m for m in with_t if near.get(m, 0)}
    return sorted(k for k, o in enumerate(owners) if str(o) in two and (everywhere or str(o) in with_t))


def _near_trias(mesh, applied: dict) -> Counter:
    """SHMESH-51o5: per mid-face, its triangles within GUARD_NEAR two-ring outer radii of one of its two-ring bolt
    holes (about three box half widths: in or beside the hole's box)."""
    lines = ((applied.get("quality") or {}).get("structure") or {}).get("hole_list") or []
    holes: dict[str, list[tuple[np.ndarray, float]]] = {}
    for h in lines:
        if h.get("bolt") and int(h.get("rings") or 0) >= ST.BOLT_RINGS and h.get("at_mm") is not None:
            r_out = 0.5 * float(h.get("diameter_mm") or 0.0) + float(h.get("planned_width_mm") or 0.0)
            holes.setdefault(str(h.get("face")), []).append((np.asarray(h["at_mm"], dtype=float), GUARD_NEAR * r_out))
    out: Counter = Counter()
    q = len(mesh.quads)
    if not len(mesh.trias):
        return out
    cen = np.asarray(mesh.xyz)[np.asarray(mesh.trias)].mean(axis=1)
    for i, m in enumerate(mesh.mid[q:]):
        hs = holes.get(str(m))
        if hs and any(float(np.linalg.norm(cen[i] - c)) <= reach for c, reach in hs):
            out[str(m)] += 1
    return out


def _grid_back(g_c: dict, free_: dict) -> tuple[bool, str]:
    """SHMESH-48x: whether a grid refused for its short edges or triangles comes back against its free mesh (each
    {elements, triangles, short}), and (SHMESH-48x3) the words when it comes back only for having no triangle."""
    # (SHMESH-48x2: short edges refuse a grid only over GRID_SHORT AND over GRID_SHORT_RATIO x the free mesh's share -
    # a channel's web at 3.37% against its free mesh's 2.66% keeps its straight rows)
    g_share = g_c["short"] / max(g_c["elements"], 1)
    f_share = free_["short"] / max(free_["elements"], 1)
    short_ok = g_share <= ST.GRID_SHORT or g_share <= ST.GRID_SHORT_RATIO * f_share
    tri_ok = g_c["triangles"] <= max(ST.GRID_TRIAS, ST.GRID_TRIA_SHARE * g_c["elements"], 2 * free_["triangles"])
    if short_ok and tri_ok:
        return True, ""
    # SHMESH-48x3: a grid with no triangle comes back when its free mesh has triangles and the user's shortest-edge
    # line is no worse with it (a target, met only with no element under it: the free mesh has some too, or the grid
    # none) - the user's choice, 18 Nov
    if GRID_ZERO_TRIA and g_c["triangles"] == 0 and free_["triangles"] > 0 and (g_c["short"] == 0 or free_["short"] > 0):
        return True, (f"no triangle in the grid against {free_['triangles']} in its free mesh; {g_c['short']} of its "
                      f"{g_c['elements']} elements under your shortest edge (the free mesh: {free_['short']} of "
                      f"{free_['elements']})")
    return False, ""


def _first_trial(mesh, owners: list[str], faces: list[int], applied: dict | None = None) -> list[int]:
    """SHMESH-51o4: the faces of the guard's first trial - those of the mid-face with the most triangles of its own
    among these (all of them when none has a triangle of its own)."""
    own_t = Counter(str(m) for m in mesh.mid[len(mesh.quads):])
    if applied is not None and GUARD_NEAR:
        own_t = _near_trias(mesh, applied)
    top = max(sorted({str(owners[k]) for k in faces}), key=lambda m_: (own_t.get(m_, 0), m_))
    return [k for k in faces if str(owners[k]) == top] if own_t.get(top, 0) else list(faces)


#: SHMESH-51o18: two rings round bolt holes are what the user asked for: the guard trades a face's second rings for
#: triangles only where one ring takes off at least this share of the face's own triangles (34 to 2 on a 50-hole plate,
#: 18 to 0 on the real column's), not a few of many (37 to 23 had cost a 50-hole look-alike all 50 of its two rings)
GUARD_SHARE = 0.75
#: SHMESH-51o30: ... replaced: two rings round bolt holes are what the user asked for, and a few triangles are fine
#: (the customer, 19 Nov; their own accepted mesh of the same structure is 0.90% triangles). A face gives up its
#: second rings for triangles only when its own triangles with two rings are over its share of its own elements -
#: the user's ("tria_share", from their notes) else GUARD_TRIA_SHARE, at least GUARD_TRIA_FLOOR triangles. On the
#: real column a 50-hole plate of 15,000 elements had gone to one ring for 29 triangles (0.19%).
GUARD_BY_SHARE = True
GUARD_TRIA_SHARE = 0.01
GUARD_TRIA_FLOOR = 4


def _own_gain(t_two: Counter, t_one: Counter, mids: list[str], e_two: Counter | None = None,
              share: float | None = None) -> tuple[list[str], bool]:
    """SHMESH-51o5: (the trial's mid-faces whose own triangles drop with one ring, whether every one of them does).
    SHMESH-51o30: a face's two rings stand while its own triangles are within its share of its elements (`share`: the
    user's, else GUARD_TRIA_SHARE; at least GUARD_TRIA_FLOOR) - only past that does one ring gain."""
    sh = GUARD_TRIA_SHARE if share is None else float(share)
    gain = [m_ for m_ in mids if t_one.get(m_, 0) < t_two.get(m_, 0)
            and (not GUARD_BY_SHARE or e_two is None
                 or t_two.get(m_, 0) > max(GUARD_TRIA_FLOOR, sh * e_two.get(m_, 0)))
            and (GUARD_BY_SHARE or t_one.get(m_, 0) <= (1.0 - GUARD_SHARE) * t_two.get(m_, 0) + 1e-9)]
    return gain, (not GUARD_OWN_GAIN or len(gain) == len(mids))


def _guard_carry(sn: dict, faces: list[int], keys: set[int], body_of) -> tuple[set, dict, dict, set, set]:
    """SHMESH-51o4: what a one-ring trial of the guard starts from (no_cut, refused, compare, kept, unmap): the
    run's state when the mesh before it was made, less what touches the bodies of the faces it changes. With
    GUARD_CARRY the other bodies' grids left out, grids back and their figures are kept (each restart had found them
    again: 8 to 9 passes, about 130 s on a real column); without, only the bolt-ring keys, as SHMESH-51b starts again."""
    hit = {body_of(int(f)) for f in faces}

    def away(k: int) -> bool:
        f_ = _face_of_key(int(k))
        b_ = body_of(f_)
        return GUARD_CARRY_GRIDS and b_ is not None and None not in hit and b_ not in hit

    no_cut = {f for f in set(sn["no_cut"]) if f < 0 or away(f)} | set(keys)
    refused = {k: v for k, v in dict(sn["refused"]).items() if k < 0 or away(k)}
    compare = {k: dict(v) for k, v in dict(sn.get("compare") or {}).items() if away(k) and k in no_cut}
    kept = {k for k in set(sn.get("kept") or ()) if away(k)}
    unmap = _keys_kept(set(sn["unmap"]), set(faces), body_of) if GATHER else set()
    return no_cut, refused, compare, kept, unmap


def _worse_why(applied: dict, past_n: int, other: dict, other_n: int) -> str:
    """SHMESH-51o11: which test of _not_worse a mesh fails against another ("" when none)."""
    if _past(applied)[0] > _past(other)[0]:
        return "not valid"
    if _past(applied)[1] > _past(other)[1]:
        return f"past Nastran's limits ({_past(applied)[1]} against {_past(other)[1]})"
    if past_n > other_n:
        return f"past the solver's own limits ({past_n} against {other_n})"
    a, b = _standing(applied, past_n), _standing(other, other_n)
    if a[2] > b[2]:
        return f"past the user's outer limits ({a[2]} against {b[2]})"
    if a[3] > b[3]:
        return f"fewer of the user's lines met ({-a[3]} against {-b[3]})"
    m_a = int(((applied.get("quality") or {}).get("structure") or {}).get("mapped_faces") or 0)
    m_b = int(((other.get("quality") or {}).get("structure") or {}).get("mapped_faces") or 0)
    if m_a < GUARD_MAPPED * m_b:
        return f"fewer mapped faces ({m_a} against {m_b})"
    return ""


def _not_worse(applied: dict, past_n: int, other: dict, other_n: int) -> bool:
    """SHMESH-51o: a mesh no worse than another on the solver's limits (valid, past Nastran's, past the solver's own)
    and on the user's lines (outside their outer limits, lines met), and (SHMESH-51o3) with its grids: no fewer mapped
    faces than GUARD_MAPPED of the other's (a one-ring trial that took 4 triangles off a channel's flange lost the web's
    grid, 448 mapped faces to 119, 2 elements under the shortest edge to 500)."""
    if _past(applied) > _past(other) or past_n > other_n:
        return False
    a, b = _standing(applied, past_n), _standing(other, other_n)
    m_a = int(((applied.get("quality") or {}).get("structure") or {}).get("mapped_faces") or 0)
    m_b = int(((other.get("quality") or {}).get("structure") or {}).get("mapped_faces") or 0)
    return a[2] <= b[2] and a[3] <= b[3] and m_a >= GUARD_MAPPED * m_b


def big_flat_faces(gmsh, tag_of: dict[int, int], facts: list[dict], rows: list[dict], mm: float,
                   skip: set[int] | frozenset = frozenset()) -> dict[int, int]:
    """SHMESH-21g: the flat faces (gmsh tag -> fact index) not in `skip` (grids, mapped) planned past BIG_FLAT_FACE
    elements (area over size squared, as face_planned.json): meshed by BIG_FLAT_ALGORITHM. None under the gate."""
    out: dict[int, int] = {}
    for t in sorted(tag_of):
        k = tag_of[t]
        size = float(rows[k].get("size_mm") or 0.0)
        if t in skip or size <= 0.0 or float(facts[k]["area"]) * mm * mm / size ** 2 <= BIG_FLAT_FACE:
            continue
        if gmsh.model.getType(2, t) == "Plane":
            out[t] = k
    return out


def _generate(gmsh, tag_of: dict[int, int]) -> None:
    """Mesh in 2D; a face gmsh cannot mesh as a periodic surface raises _Periodic for it (SHMESH-11), and so does
    an analytic face whose boundary gmsh's 2D mesher cannot recover (SHMESH-11b): the face gmsh was meshing when
    it stopped, read from its log."""
    import re

    gmsh.logger.start()
    try:
        gmsh.model.mesh.generate(2)
    except Exception as exc:
        m = re.search(r"periodic surface (\d+)", str(exc))
        if m and int(m.group(1)) in tag_of:
            raise _Periodic(tag_of[int(m.group(1))]) from exc
        text = str(exc).lower()
        # SHMESH-11c: and so does a face whose boundary's 1D mesh gmsh reads as "not a closed loop" (an analytic or
        # trimmed face on a closed surface whose loop runs across the surface's seam in its parameters: a tee's run,
        # a mitred bend's pieces; fresh fr015/fr017) - as a B-spline it has no seam. A face already a B-spline is
        # not re-made again (gmsh names it "BSpline surface"): the error stands
        if RESPLINE_UNRECOVERED and ("recover" in text or (RESPLINE_LOOP and "closed loop" in text)):
            at = [int(x) for line in gmsh.logger.get() for x in re.findall(r"Meshing surface (\d+)", line)]
            kind = gmsh.model.getType(2, at[-1]).lower() if at else ""
            if at and at[-1] in tag_of and "spline" not in kind and "bezier" not in kind:
                raise _Periodic(tag_of[at[-1]], kin="recover" in text, loop="recover" not in text) from exc
        raise


def _even_faces(gmsh, tag_of: dict[int, int], fixed: set[int], floor: dict[int, float]) -> int:
    """SHMESH-19: Blossom can pair every triangle of a face into quads only when the face's boundary has an even
    number of segments; with an odd number gmsh fell back and left hundreds of triangles (sh0019: 687 on one plate).
    After the 1D mesh, each face with an odd count gets one segment more on one of its own free curves (a rim no
    other face meshes), the most evenly divided one (a hole's circle before a graded edge), the longest count on a
    tie; a face with no free curve takes it on a curve it shares with one other odd face, which evens both. Curves
    in `fixed` (already set: mapped strips) are left, and so is a curve whose segments would come out under the floor
    of a face it bounds (`floor`, per surface; a 0.7 mm spline cut in 3 left slivers). The most evenly divided means
    to a tenth; then the most segments (the smallest change).
    The 1D mesh is then cleared, so the caller meshes again.
    Returns how many curves were given a segment more."""
    gmsh.model.mesh.generate(1)
    seg: dict[int, int] = {}
    spread: dict[int, float] = {}
    length: dict[int, float] = {}
    for _d, c in gmsh.model.getEntities(1):
        _t, _tags, nodes = gmsh.model.mesh.getElements(1, c)
        conn = np.concatenate([np.asarray(x, dtype=np.int64) for x in nodes]).reshape(-1, 2) if nodes else np.zeros((0, 2), dtype=np.int64)
        seg[c] = len(conn)
        if len(conn):
            tags, xyz, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            at = {int(t): i for i, t in enumerate(tags)}
            p = np.asarray(xyz, dtype=float).reshape(-1, 3)
            ln = np.array([np.linalg.norm(p[at[int(a)]] - p[at[int(b)]]) for a, b in conn])
            spread[c] = round(float(ln.max() / max(ln.min(), 1e-300)), 1)
            length[c] = float(ln.sum())
    faces_of: dict[int, list[int]] = {}
    bound: dict[int, list[int]] = {}
    for t in sorted(tag_of):
        bound[t] = sorted(abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False))
        for c in bound[t]:
            faces_of.setdefault(c, []).append(t)
    odd = {t for t in bound if sum(seg.get(c, 0) for c in bound[t]) % 2}
    bumped: set[int] = set()

    def ok(c: int) -> bool:
        return (c not in fixed and c not in bumped and seg.get(c, 0) > 0
                and length[c] / (seg[c] + 1) >= max(floor.get(t, 0.0) for t in faces_of.get(c, [])))

    for t in sorted(odd):
        if t not in odd:
            continue
        free = [c for c in bound[t] if ok(c) and faces_of.get(c) == [t]]
        if not free:
            continue
        c = min(free, key=lambda c: (spread.get(c, 1.0), -seg[c], c))
        bumped.add(c)
        odd.discard(t)
    for t in sorted(odd):
        if t not in odd:
            continue
        shared = [c for c in bound[t] if ok(c) and len(faces_of.get(c, [])) == 2
                  and [o for o in faces_of[c] if o != t][0] in odd]
        if not shared:
            continue
        c = min(shared, key=lambda c: (spread.get(c, 1.0), -seg[c], c))
        bumped.add(c)
        odd -= set(faces_of[c])
    for c in sorted(bumped):
        gmsh.model.mesh.setTransfiniteCurve(c, seg[c] + 2)
    if bumped:
        # (clearing only the curves given a segment more left their end nodes behind: gmsh found open loops)
        gmsh.model.mesh.clear()
    return len(bumped)


def _arc_radius(gmsh, curve: int) -> float | None:
    """The radius (model units) of a gmsh curve that is a circle or a circular arc; None for any other curve."""
    if gmsh.model.getType(1, curve) != "Circle":
        return None
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    k = gmsh.model.getCurvature(1, curve, [0.5 * (lo[0] + hi[0])])
    return 1.0 / float(k[0]) if len(k) and float(k[0]) > 0 else None


#: SHMESH-51o16: a narrow strip one cell from a two-ring bolt box's side takes the box's count along it (the real
#: column's 12-hole plate: four end boxes' 56 mm sides sat in a class held at 6 by such a strip's count, their bands
#: came out with 3 degree corners and were refused, and the four holes fell to one ring). OFF: on the real column the
#: strip holding 6 (47 x 4.2 mm) stood two and three cells out, so it never fired there, and on a 50-hole look-alike
#: the strips it did change sent holes to one ring five rounds running (92 to 188 s, the same mesh)
STRIP_TAKES_BOX = False


def _box_facing(gmsh, wish: dict[int, int], long: set[int]) -> dict[int, int]:
    """SHMESH-51o16: {curve: count} - each single curve facing a two-ring bolt box's side across one four-sided cell,
    with the box's count for that side (a curve two boxes face with two counts is left out)."""
    out: dict[int, set[int]] = {}
    for c in sorted(long & set(wish)):
        for s_ in (int(x) for x in gmsh.model.getAdjacencies(1, c)[0]):
            got = ST._four_sided(gmsh, s_)
            if got is None:
                continue
            sides = got[0]
            for i in range(4):
                if sides[i] == [c] and len(sides[(i + 2) % 4]) == 1:
                    out.setdefault(sides[(i + 2) % 4][0], set()).add(int(wish[c]))
    return {c: next(iter(v)) for c, v in out.items() if len(v) == 1}


def _free_other(gmsh, t: int, mm: float, size: float) -> None:
    """free_why for a face that is not four-sided: its sides (mm, in loop order where gmsh gives it), the turn at each
    corner, its smallest radius of curvature and its size - which rule could map it (corners from the curves)."""
    bnd = list(dict.fromkeys(abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=True)))
    sides = sorted(set(bnd))
    try:
        turns = _corner_turns(gmsh, t, sides)
    except Exception:  # noqa: BLE001 - a face whose corners cannot be read is listed without them
        turns = []
    prior = _FREE_WHY.get(t)
    _FREE_WHY[t] = {**({"corner_rule": prior} if prior else {}), "why": f"not four-sided ({len(bnd)} sides)", "sides_mm": [round(float(gmsh.model.occ.getMass(1, c)) * mm, 2)
                                                                     for c in bnd],
                    "turns_deg": turns, "corners_over_45": sum(1 for x_ in turns if x_ > 45.0),
                    "radius_mm": _face_radius(gmsh, t, mm), "size_mm": round(size * mm, 2)}


def _face_radius(gmsh, t: int, mm: float) -> float | None:
    """free_why: a face's smallest radius of curvature over a 3 x 3 sample (mm; None for a flat face)."""
    lo, hi = gmsh.model.getParametrizationBounds(2, t)
    kmax = 0.0
    for a_ in (0.25, 0.5, 0.75):
        for b_ in (0.25, 0.5, 0.75):
            try:
                k1, k2, _d1, _d2 = gmsh.model.getPrincipalCurvatures(
                    t, [lo[0] + a_ * (hi[0] - lo[0]), lo[1] + b_ * (hi[1] - lo[1])])
                kmax = max(kmax, abs(k1[0]), abs(k2[0]))
            except Exception:  # noqa: BLE001
                pass
    return round(1.0 / kmax * mm, 2) if kmax > 1e-12 else None


def _degenerate(gmsh, c: int) -> bool:
    """SHMESH-79: a curve of no length and no end vertices - a side of its face's patch shrunk to a point (gmsh meshes
    it with no node of its own); a closed curve (a circle: no end vertices either) is not one."""
    return (not gmsh.model.getBoundary([(1, int(c))], oriented=False)
            and float(gmsh.model.occ.getMass(1, int(c))) <= 1e-9)


def _no_step(tag_of: dict[int, int], facts: list[dict], keys: list | None = None) -> frozenset:
    """SHMESH-73: the surface tags of this pass whose step failed before (by centre and area, within a millionth);
    with `keys`, those faces (SHMESH-77's faces not to fit-map)."""
    got = set()
    for t, k in tag_of.items():
        c = np.asarray(facts[k]["centroid"], dtype=float)
        a = float(facts[k]["area"])
        for c_, a_ in (_STEP_FAILED if keys is None else keys):
            if (float(np.linalg.norm(c - c_)) <= 1e-6 * max(1.0, float(np.abs(c).max()))
                    and abs(a - a_) <= 1e-6 * max(a, a_, 1e-12)):
                got.add(t)
                break
    return frozenset(got)


def _most_segments(length_mm: float, low_mm: float) -> int:
    """The most segments a curve takes with none under the user's shortest edge - and, SHMESH-93, one for a curve
    shorter than that edge itself: every mesh of the face has that curve as one edge at least, so one segment there
    costs the edge line nothing a free mesh would not (a fillet's inner arc of 0.79 mm under a 1.9 mm shortest edge
    read 0, no count fitted, and the face was paved: 4 to 9 triangles on each of a dart part's corner fillets)."""
    n = int(math.floor(length_mm / low_mm + 1e-9))
    return max(n, 1) if SHORT_SIDE_ONE else n


def _corner_turns(gmsh, t: int, sides) -> list[float]:
    """free_why: the turn at each corner of a four-sided face, degrees (0: the sides run straight on; 90: a square
    corner), from the sides' tangents at the corner."""
    turns = []
    pts = sorted({abs(p) for c in sides for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)})
    for p in pts:
        at = [c for c in sides if p in {abs(q) for _d, q in gmsh.model.getBoundary([(1, c)], oriented=False)}]
        if len(at) != 2:
            continue
        xyz = np.array(gmsh.model.getValue(0, p, []))
        tans = []
        for c in at:
            lo, hi = gmsh.model.getParametrizationBounds(1, c)
            u0 = lo[0] if np.linalg.norm(np.array(gmsh.model.getValue(1, c, [lo[0]])) - xyz) <= np.linalg.norm(
                np.array(gmsh.model.getValue(1, c, [hi[0]])) - xyz) else hi[0]
            d = np.array(gmsh.model.getDerivative(1, c, [u0]))
            n = float(np.linalg.norm(d))
            tans.append(d / n * (1.0 if u0 == lo[0] else -1.0) if n > 0 else d)
        cosang = float(np.clip(np.dot(tans[0], tans[1]), -1.0, 1.0))
        turns.append(round(180.0 - math.degrees(math.acos(cosang)), 1))
    return turns


def _map_strips(gmsh, tag_of: dict[int, int], rows: list[dict], mm: float,
                sizing: dict | None = None, fine_at: dict[int, float] | None = None,
                counts_out: dict[int, int] | None = None, held: set[int] | frozenset = frozenset(),
                prefer: dict[int, int] | None = None,
                narrow_out: dict | None = None,
                not_thin: set[int] | frozenset = frozenset(),
                transitions_out: dict | None = None,
                no_step: set[int] | frozenset = frozenset(),
                no_fit: set[int] | frozenset = frozenset()) -> dict[int, tuple[tuple[int, ...], float]]:
    """SHMESH-16: every narrow four-sided strip (a bend, a flange, a strip between holes: its two long sides alike in
    length, its two ends alike, the long sides at least STRIP_LONG times the ends) is meshed as a mapped grid of
    quads - its planned size across it, and along it the size of the faces beside its long sides, at most
    STRETCH_MAX times the size across. A floor-sized bend no longer fills its length with floor-sized elements, and
    a mapped grid leaves no triangles. Returns {strip surface: (its long sides, the size along them)}. Counts are set once per curve: a
    strip whose curve another strip has already counted differently stays free (meshed as before).
    `sizing` (SHMESH-24, the user's sizes): the stretch is held under their aspect ratio and their longest edge,
    and a count is not made even when that would put its segments under their shortest edge.
    `narrow_out` (SHMESH-60, with the user's shortest edge): strips narrower than it are mapped one row across and
    listed there, {strip: (long sides, ends, width mm)}, to be closed after meshing (_narrow_pairs)."""
    count: dict[int, int] = {}
    low: Any = (sizing or {}).get("low_mm")
    high: Any = (sizing or {}).get("high_mm")
    nout: dict = narrow_out if narrow_out is not None else {}

    def even(n: int, length: float) -> int:
        if not n % 2 or (low is not None and length * mm / (n + 1) < low):
            return n
        return n + 1

    out: dict[int, tuple[tuple[int, ...], float]] = {}
    size_of = {t: rows[k]["size_mm"] / mm for t, k in tag_of.items()}

    def chain(t: int) -> bool:
        """SHMESH-60: a narrow face that is no plain four-sided strip (its ends chains of short curves - a fillet
        ending in a run-out's slivers -, a needle of three sides, a side of no length): its two long sides take one
        count, every curve of its ends one segment; it is meshed free and closed after."""
        if t in not_thin:
            return False
        got = _thin_chain(gmsh, t, low / mm)
        if got is None:
            return False
        a, ends_c, width = got
        beside = [size_of[s_] for c in a for s_ in gmsh.model.getAdjacencies(1, c)[0] if s_ != t and s_ in size_of]
        along_t = min(beside) if beside else size_of[t]
        if high is not None:
            along_t = min(along_t, 0.98 * high / mm)
        la = [float(gmsh.model.occ.getMass(1, c)) for c in a]
        n_a = max(1, math.ceil(max(la) / along_t - 1e-9))
        if EVEN:
            n_a = even(n_a, max(la))
        n_a = count.get(a[0], count.get(a[1], n_a))
        if any(count.get(c, n_a) != n_a for c in a) or any(count.get(c, 1) != 1 for c in ends_c):
            return False
        for c in a:
            count[c] = n_a
        for c in ends_c:
            count[c] = 1
        nout[t] = (tuple(a), tuple(ends_c), float(width * mm), "free")
        return True

    chains = narrow_out is not None and low is not None
    corner_tags: dict[int, list[int]] = {}
    can_take: dict[int, dict[int, set[int]]] = {}    # SHMESH-75: curve -> {few-row strip: the counts it can map with}
    if YIELD_COUNTS and not _YIELD_OFF[0] and ONE_ROW_ALIKE and sizing is not None and low is not None \
            and high is not None:
        for t_y, k_y in sorted(tag_of.items()):
            if rows[k_y]["rule"] == "SHMESH-15" or t_y in not_thin:
                continue
            sd = sorted({abs(c) for _d, c in gmsh.model.getBoundary([(2, t_y)], oriented=False)})
            if len(sd) != 4:
                continue
            en = {c: {abs(p_) for _d, p_ in gmsh.model.getBoundary([(1, c)], oriented=False)} for c in sd}
            op = [c for c in sd[1:] if not (en[c] & en[sd[0]])]
            if len(op) != 1:
                continue
            a_y = (sd[0], op[0])
            b_y = tuple(c for c in sd[1:] if c != op[0])
            ln = {c: float(gmsh.model.occ.getMass(1, c)) for c in sd}
            if max(ln[c] for c in a_y) < max(ln[c] for c in b_y):
                a_y, b_y = b_y, a_y
            la_y, lb_y = [ln[c] for c in a_y], [ln[c] for c in b_y]
            if not (max(lb_y) < FEW_ROWS * size_of[t_y] and min(la_y) >= STRIP_LONG * max(lb_y)
                    and min(lb_y) * mm >= low and abs(lb_y[0] - lb_y[1]) <= NARROW_ALIKE * max(lb_y)):
                continue
            rg = [(max(1, math.ceil(L * mm / (0.98 * high) - 1e-9)), int(math.floor(L * mm / low + 1e-9)))
                  for L in la_y]
            one = set(range(max(r_[0] for r_ in rg), min(r_[1] for r_ in rg) + 1))
            took = [set(one), set(one)]
            if TRANSITIONS:
                i_c = 0 if la_y[0] <= la_y[1] else 1
                for n_c in range(rg[i_c][0], rg[i_c][1] + 1):
                    for n_f in range(max(rg[1 - i_c][0], n_c + 2), min(rg[1 - i_c][1], 3 * n_c) + 1):
                        if (n_f - n_c) % 2 == 0:
                            took[i_c].add(n_c)
                            took[1 - i_c].add(n_f)
            for c, got_y in zip(a_y, took, strict=True):
                if got_y:
                    can_take.setdefault(c, {})[t_y] = got_y

    def yielded(t: int, a: tuple, la: tuple, n: int, least: int) -> int:
        """SHMESH-75: the count for strip t's long sides a: n, or the nearest count inside the user's edges (and at
        least `least`, its own stretch) that every few-row neighbour sharing a side of a can take."""
        if not can_take or any(c in count for c in a):
            return n
        want = [v_ for c in a for o_, v_ in can_take.get(c, {}).items() if o_ != t]
        if not want:
            return n
        row_ = {"strip": int(t), "long_mm": [round(x_ * mm, 2) for x_ in la], "count": int(n),
                "neighbour_takes": [sorted(v_)[:12] for v_ in want]}
        _STRIP_LOG.append(row_)
        if all(n in v_ for v_ in want):
            row_["outcome"] = "its count is one the neighbour can take"
            return n
        lo_n = max(least, math.ceil(max(la) * mm / (0.98 * high) - 1e-9))
        hi_n = int(math.floor(min(la) * mm / low + 1e-9))
        ok = [m for m in range(lo_n, hi_n + 1) if all(m in v_ for v_ in want)]
        if not ok:
            row_["outcome"] = f"no count {lo_n}..{hi_n} inside its own edges is one the neighbour can take"
            return n
        _YIELDED[0] += 1
        row_["outcome"] = f"yielded to {min(ok, key=lambda m: (abs(m - n), -m))}"
        return min(ok, key=lambda m: (abs(m - n), -m))
    refused: list[tuple] = []     # SHMESH-73: few-row strips one count could not map

    def taper(t: int, k: int) -> bool:
        """SHMESH-69: a strip that tapers to a point (a fillet running out at a run-out: three corners, its other
        vertices smooth, its short side under FEW_ROWS of its size, its two long sides alike within NARROW_ALIKE and
        at least twice the short side) is one or a few rows of quads along it, closing in one triangle a row at
        its tip (gmsh's three-cornered transfinite face), not paved."""
        got = _taper_sides(gmsh, t)
        if got is None:
            return False
        tip, l1, l2, end = got
        len_of = {c: float(gmsh.model.occ.getMass(1, c)) for c in (*l1, *l2, *end)}
        L1, L2, E = sum(len_of[c] for c in l1), sum(len_of[c] for c in l2), sum(len_of[c] for c in end)
        across = size_of[t]
        if (E >= FEW_ROWS * across or min(L1, L2) < 2.0 * E
                or abs(L1 - L2) > NARROW_ALIKE * max(L1, L2) or len(end) != 1):
            return False
        beside = [size_of[s_] for c in (*l1, *l2) for s_ in gmsh.model.getAdjacencies(1, c)[0]
                  if s_ != t and s_ in size_of]
        along = min(beside) if beside else across
        if high is not None:
            along = min(along, 0.98 * high / mm)
        n_a = max(1, math.ceil(max(L1, L2) / along - 1e-9))
        n_b = max(1, math.ceil(E / across - 1e-9))
        if low is not None:
            n_b = min(n_b, max(1, int(math.floor(E * mm / low + 1e-9))))
        spreads = []
        for chain_ in (l1, l2):
            sp = ST._spread(n_a, [len_of[c] for c in chain_]) if len(chain_) > 1 else [n_a]
            if sp is None:
                return False
            spreads.append(sp)
        want = {c: m for chain_, sp in zip((l1, l2), spreads, strict=True) for c, m in zip(chain_, sp, strict=True)}
        want[end[0]] = n_b
        if any(count.get(c, m) != m for c, m in want.items()):
            return False
        count.update(want)
        pts = [abs(int(p_)) for _d, p_ in gmsh.model.getBoundary([(1, end[0])], oriented=False)]
        corner_tags[t] = [tip, *pts]
        out[t] = (tuple(l1 + l2), max(L1, L2) / n_a)
        return True
    def tri_plan(t: int, cl: list, at: list, verts: list, turns_: list) -> bool:
        """SHMESH-79: counts for a three-cornered face's sides (even sum, each under the other two), inside the edges."""
        m_ = len(cl)
        inner_ = [180.0 - turns_[i] for i in at]
        for shape_, key_, _op, target_, _o, _s in _AIMS_NOW[0]:
            # (each block's quad at a corner has that corner's angle: inside the user's quad targets, or no blocks)
            if shape_ == "quad" and ((key_ == "min_angle_quad" and min(inner_) < target_)
                                     or (key_ == "max_angle_quad" and max(inner_) > target_)):
                _FREE_WHY[t] = {"why": "three corners: a corner outside the user's quad angles",
                                "corners_deg": [round(x_, 1) for x_ in inner_]}
                return False
        chains_ = []
        for q in range(3):
            i0, i1 = at[q], at[(q + 1) % 3]
            chains_.append([cl[(i0 + 1 + j) % m_] for j in range((i1 - i0) % m_)])
        lens = {c: float(gmsh.model.occ.getMass(1, c)) for c in cl}
        L = [sum(lens[c] for c in ch) for ch in chains_]
        rng_ = []
        for ch, L_ in zip(chains_, L, strict=True):
            lo_ = max(1, math.ceil(L_ * mm / (0.98 * high) - 1e-9))
            hi_ = math.floor(L_ * mm / low + 1e-9)
            held_ = {count[c] for c in ch if c in count} if len(ch) == 1 else set()
            if held_:
                lo_ = hi_ = held_.pop()
            rng_.append((lo_, hi_))
        want_ = [min(max(r_[0], math.ceil(L_ / size_of[t] - 1e-9)), r_[1]) for r_, L_ in zip(rng_, L, strict=True)]
        best_ = None
        for d0 in (0, 1, -1):
            for d1 in (0, 1, -1):
                for d2 in (0, 1, -1):
                    n_ = [want_[0] + d0, want_[1] + d1, want_[2] + d2]
                    if any(not (r_[0] <= x_ <= r_[1]) for x_, r_ in zip(n_, rng_, strict=True)):
                        continue
                    if sum(n_) % 2 or any(2 * x_ >= sum(n_) for x_ in n_):
                        continue
                    cost = sum(abs(d_) for d_ in (d0, d1, d2))
                    if best_ is None or cost < best_[0]:
                        best_ = (cost, n_)
        if best_ is None:
            _FREE_WHY[t] = {"why": "three corners: no counts of even sum fit its sides", "sides_mm": [round(x_ * mm, 2)
                                                                                                   for x_ in L],
                            "ranges": rng_}
            return False
        n_ = best_[1]
        w_, u_, v_ = (n_[0] + n_[1] - n_[2]) // 2, (n_[1] + n_[2] - n_[0]) // 2, (n_[0] + n_[2] - n_[1]) // 2
        if min(w_, u_, v_) < 1 or 3 * min(w_, u_, v_) < max(w_, u_, v_):
            _FREE_WHY[t] = {"why": "three corners: its sides too unequal for blocks of like size", "counts": n_}
            return False
        plan: dict[int, int] = {}
        for ch, k_ in zip(chains_, n_, strict=True):
            sp = ST._spread(k_, [lens[c] for c in ch]) if len(ch) > 1 else [k_]
            if sp is None or any(lens[c] * mm / x_ < low and not (x_ == 1 and lens[c] * mm < low)
                                 for c, x_ in zip(ch, sp, strict=True)):
                _FREE_WHY[t] = {"why": "three corners: a curve too short for its share", "sides_mm": [round(x_ * mm, 2)
                                                                                                 for x_ in L]}
                return False
            for c, x_ in zip(ch, sp, strict=True):
                if count.get(c, x_) != x_:
                    _FREE_WHY[t] = {"why": "three corners: a curve already counted otherwise",
                                    "sides_mm": [round(x_ * mm, 2) for x_ in L]}
                    return False
                plan[c] = x_
        count.update(plan)
        _TRI_PLAN[t] = (chains_, n_, [verts[i] for i in at])
        return True

    def corner_four(t: int) -> bool:
        """SHMESH-78: map a face of more than four curves on its four real corners (see CORNER_MERGE)."""
        def refuse(text: str, **nums) -> bool:
            _FREE_WHY[t] = {"why": "corners from the curves: " + text, **nums}
            return False

        _l, loops = ST._curve_loops(gmsh, t)
        if len(loops) != 1:
            return refuse(f"{len(loops)} curve loops (a face with holes)")
        cl = [abs(int(c)) for c in loops[0]]
        if DEGENERATE_CORNER and len(cl) == 4:
            # SHMESH-79: a degenerate curve (no end vertices: a side of the face's patch shrunk to a point, as at a
            # bead crossing's corner blend) is a point of the loop, not a side - a four-curve face with one is
            # three-sided. (Only four-curve faces: on longer loops it let SHMESH-78 map two faces of a domed run-out
            # on four corners, +20 triangles on the integration tree, -11 on the stream alone - not judged here.)
            cl = [c for c in cl if not _degenerate(gmsh, c)]
        m_ = len(cl)
        if len(set(cl)) != m_ or m_ < 3 or (m_ == 4 and not TRI_BLOCKS):
            return refuse("a curve twice in its loop (a seam)" if len(set(cl)) != m_ else "four sides or fewer")
        ends_ = {c: [abs(int(q)) for _d, q in gmsh.model.getBoundary([(1, c)], oriented=False)] for c in cl}
        # the vertex between curve i and curve i+1, and the turn there
        verts, turns_ = [], []
        for i in range(m_):
            c1, c2 = cl[i], cl[(i + 1) % m_]
            shared = set(ends_[c1]) & set(ends_[c2])
            if len(shared) != 1:
                return refuse("two curves of its loop meet at more or fewer than one vertex")
            p_ = shared.pop()
            verts.append(p_)
            got_ = _corner_turns(gmsh, t, [c1, c2])
            if len(got_) != 1:
                return refuse("a corner's turn could not be read")
            turns_.append(got_[0])
        at = [i for i in range(m_) if turns_[i] >= CORNER_TURN]
        if TRI_BLOCKS and len(at) == 3:
            # (three corners: the rest of its loop runs on as its three sides, the blocks' quads checked as built)
            return tri_plan(t, cl, at, verts, turns_)
        if len(at) != 4 or any(SMOOTH_TURN <= turns_[i] < CORNER_TURN for i in range(m_)):
            return refuse(f"{len(at)} corners of {CORNER_TURN:.0f} degrees or more" if len(at) != 4
                          else f"a joint between smooth and a corner ({SMOOTH_TURN:.0f} to {CORNER_TURN:.0f} degrees)",
                          turns_deg=[round(x_, 1) for x_ in turns_])
        # the four chains: the curves after corner a up to and including the curve ending at the next corner
        chains_ = []
        for q in range(4):
            i0, i1 = at[q], at[(q + 1) % 4]
            idx = [(i0 + 1 + j) % m_ for j in range((i1 - i0) % m_)]
            chains_.append([cl[j] for j in idx])
        L = [sum(float(gmsh.model.occ.getMass(1, c)) for c in ch) for ch in chains_]
        lens = {c: float(gmsh.model.occ.getMass(1, c)) for c in cl}
        plan: dict[int, int] = {}
        ns = []
        for pa, pb in ((0, 2), (1, 3)):
            lo_ = math.ceil(max(L[pa], L[pb]) * mm / (0.98 * high) - 1e-9)
            hi_ = math.floor(min(L[pa], L[pb]) * mm / low + 1e-9)
            held_ = {count[c] for ch in (chains_[pa], chains_[pb]) for c in ch if c in count and len(ch) == 1}
            want_ = min(max(lo_, math.ceil(max(L[pa], L[pb]) / size_of[t] - 1e-9)), hi_)
            cands = sorted(held_) if held_ else [want_]
            if len(cands) != 1 or not (lo_ <= cands[0] <= hi_):
                _FREE_WHY[t] = {"why": "corners from the curves: no count fits a pair of its sides",
                                "sides_mm": [round(x_ * mm, 2) for x_ in L], "range": [lo_, hi_], "held": sorted(held_)}
                return False
            n_ = cands[0]
            for ch in (chains_[pa], chains_[pb]):
                sp = ST._spread(n_, [lens[c] for c in ch]) if len(ch) > 1 else [n_]
                # (a curve under the shortest edge itself takes one segment: its two ends are nodes of any mesh -
                # the CAD's vertices, shared with the faces beside -, so the face free has that edge too)
                if sp is None or any(lens[c] * mm / k_ < low and not (k_ == 1 and lens[c] * mm < low)
                                     for c, k_ in zip(ch, sp, strict=True)):
                    _FREE_WHY[t] = {"why": "corners from the curves: a curve too short for its share of the count",
                                    "sides_mm": [round(x_ * mm, 2) for x_ in L], "count": n_,
                                    "chain_mm": [round(lens[c] * mm, 2) for c in ch], "spread": sp, "low_mm": low}
                    return False
                for c, k_ in zip(ch, sp, strict=True):
                    if count.get(c, k_) != k_ or plan.get(c, k_) != k_:
                        _FREE_WHY[t] = {"why": "corners from the curves: a curve already counted otherwise",
                                        "sides_mm": [round(x_ * mm, 2) for x_ in L], "count": n_,
                                        "curve_mm": round(lens[c] * mm, 2), "wants": k_,
                                        "held": count.get(c, plan.get(c))}
                        return False
                    plan[c] = k_
            ns.append(n_)
        inner = [180.0 - turns_[i] for i in at]
        seg = [L[0] / ns[0], L[2] / ns[0], L[1] / ns[1], L[3] / ns[1]]
        aspect_ = max(max(seg[:2]) / min(seg[2:]), max(seg[2:]) / min(seg[:2]))
        for shape_, key_, _op, target_, _o, _s in _AIMS_NOW[0]:
            if shape_ != "quad":
                continue
            if ((key_ == "aspect" and aspect_ > target_) or (key_ == "min_angle_quad" and min(inner) < target_)
                    or (key_ == "max_angle_quad" and max(inner) > target_)):
                _FREE_WHY[t] = {"why": "corners from the curves: its quads would be past the user's targets",
                                "sides_mm": [round(x_ * mm, 2) for x_ in L], "aspect": round(aspect_, 2),
                                "corners_deg": [round(x_, 1) for x_ in inner]}
                return False
        count.update(plan)
        corner_tags[t] = [verts[i] for i in at]
        out[t] = (tuple(chains_[0] + chains_[2]), max(L[0], L[2]) / ns[0])
        _CORNER_MERGED[0] += 1
        return True

    for t, k in sorted(tag_of.items()):
        bd_t = gmsh.model.getBoundary([(2, t)], oriented=False)
        degen_t = (TRI_BLOCKS and DEGENERATE_CORNER and len(bd_t) == 4
                   and any(_degenerate(gmsh, abs(c_)) for _d, c_ in bd_t))
        if (CORNER_MERGE and sizing is not None and low is not None and high is not None
                and rows[k]["rule"] != "SHMESH-15" and t not in not_thin
                and (len(bd_t) > 4 or (TRI_BLOCKS and len(bd_t) == 3) or degen_t)
                and corner_four(t)):
            continue
        if degen_t and t in _FREE_WHY:
            continue        # (SHMESH-79: a three-sided face; its corner rule's refusal is its record)
        if chains and rows[k]["rule"] != "SHMESH-15" and len(gmsh.model.getBoundary([(2, t)], oriented=False)) != 4:
            _free_other(gmsh, t, mm, size_of[t])
            chain(t)
            continue
        flat = gmsh.model.getType(2, t) == "Plane"
        # SHMESH-27: under the user's sizes a flat strip narrower than its size (one element across) is mapped too
        user_flat = flat and sizing is not None and FEATURE_SIZES
        n_bd = len(gmsh.model.getBoundary([(2, t)], oriented=False))
        if (rows[k]["rule"] == "SHMESH-15" or (flat and not MAP_FLAT and not user_flat) or n_bd != 4):
            if n_bd == 4:
                _FREE_WHY[t] = {"why": "a plate's own face (SHMESH-15)" if rows[k]["rule"] == "SHMESH-15"
                                else "flat, and flat faces are not strip-mapped here"}
            elif rows[k]["rule"] != "SHMESH-15" and not flat:
                _free_other(gmsh, t, mm, size_of[t])
            continue
        sides = sorted({abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)})
        side_mm = [round(float(gmsh.model.occ.getMass(1, c)) * mm, 2) for c in sides]
        if len(sides) != 4 or (held and any(c in held for c in sides)):
            # (SHMESH-51e: a strip along a two-ring bolt box's side is the grid's (map_faces): the box's count holds it)
            _FREE_WHY[t] = {"why": "a curve used twice (a seam)" if len(sides) != 4 else "a side held by a bolt box",
                            "sides_mm": side_mm}
            continue
        ends = {c: {abs(p) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)} for c in sides}
        # the side sharing no corner with the first is its opposite; the other two face each other
        opposite = [c for c in sides[1:] if not (ends[c] & ends[sides[0]])]
        if len(opposite) != 1:
            _FREE_WHY[t] = {"why": "no single opposite side (corners shared oddly)", "sides_mm": side_mm,
                            "opposites": len(opposite)}
            continue
        a = (sides[0], opposite[0])
        b = tuple(c for c in sides[1:] if c != opposite[0])
        if ends[b[0]] & ends[b[1]]:
            _FREE_WHY[t] = {"why": "its two other sides share a corner", "sides_mm": side_mm}
            continue
        la = tuple(float(gmsh.model.occ.getMass(1, c)) for c in a)
        lb = tuple(float(gmsh.model.occ.getMass(1, c)) for c in b)

        def why_(text: str, **nums) -> None:
            # (called in this iteration only: the face's sides as they stand when it is refused)
            _FREE_WHY[t] = {"why": text, "long_mm": [round(x_ * mm, 2) for x_ in la],  # noqa: B023
                            "ends_mm": [round(x_ * mm, 2) for x_ in lb],  # noqa: B023
                            "size_mm": round(size_of[t] * mm, 2),  # noqa: B023
                            "counted": [count.get(c) for c in (*a, *b)],  # noqa: B023
                            "turns_deg": _corner_turns(gmsh, t, sides),  # noqa: B023
                            "radius_mm": _face_radius(gmsh, t, mm),  # noqa: B023
                            **nums}
        thin = None
        if narrow_out is not None and low is not None and t not in not_thin:
            # SHMESH-60: a strip narrower than the user's shortest edge: one row across, closed after meshing
            for a_, b_, la_, lb_ in ((a, b, la, lb), (b, a, lb, la)):
                if (max(lb_) * mm < low and min(la_) >= max(lb_)
                        and abs(la_[0] - la_[1]) <= NARROW_ALIKE * max(la_)):
                    thin = (a_, b_, la_, lb_)
                    break
        if thin is not None:
            a, b, la, lb = thin
        else:
            if max(la) < max(lb):
                a, b, la, lb = b, a, lb, la
            alike = STRIP_ALIKE
            if (ONE_ROW_ALIKE and sizing is not None and low is not None
                    and max(lb) < FEW_ROWS * size_of[t] and min(la) >= STRIP_LONG * max(lb)):
                # SHMESH-65: under the user's sizes a strip a few elements across (a fillet round a dart's crest, its
                # long sides 7 and 11 mm; a stamped panel's B-spline fillet 4.4 mm wide whose long sides curve and
                # whose ends run out at a slant) is rows of trapezoids however unequal its long sides: mapped, not
                # paved (paved, half its elements were triangles)
                alike = NARROW_ALIKE
                if (UNEQUAL_FEASIBLE and high is not None and abs(la[0] - la[1]) > alike * max(la)
                        and abs(lb[0] - lb[1]) <= alike * max(lb)
                        and math.ceil(max(la) * mm / (0.98 * high) - 1e-9)
                        <= _most_segments(min(la) * mm, low)):
                    # SHMESH-65: long sides more unequal still (a fillet between fillets, 28 and 10.5 mm, its ends
                    # running on smoothly) are one count while one count keeps the long side's segments under the
                    # user's longest edge and the short side's over their shortest
                    alike = 1.0
            fits = False
            if FIT_MAP and sizing is not None and low is not None and high is not None and t not in no_fit:
                # SHMESH-77: one count per pair inside the user's edges (shortest with LOW_BAND's give) maps the face
                low_b = low * (1.0 - LOW_BAND)
                fits = all(math.ceil(max(p_) * mm / (0.98 * high) - 1e-9) <= _most_segments(min(p_) * mm, low_b)
                           for p_ in (la, lb))
                plain_ok = (abs(la[0] - la[1]) <= alike * max(la) and abs(lb[0] - lb[1]) <= alike * max(lb)
                            and min(la) >= STRIP_LONG * max(lb))
                if fits and plain_ok:
                    fits = False             # (a strip the plain tests map: as before, no shape check)
                elif fits:
                    # (and its quads, as the counts would make them, inside the user's quad targets: their stretch
                    # from the segments, their corners from the face's own - a fit-mapped trapezoid more stretched
                    # or more skewed than the free mesh it replaces is no gain)
                    def n_fit(p_, _t=t, _lb=low_b):
                        lo_ = math.ceil(max(p_) * mm / (0.98 * high) - 1e-9)
                        hi_ = _most_segments(min(p_) * mm, _lb)
                        return min(max(lo_, math.ceil(max(p_) / size_of[_t] - 1e-9)), hi_)

                    na_, nb_ = n_fit(la), n_fit(lb)
                    seg_a, seg_b = (min(la) / na_, max(la) / na_), (min(lb) / nb_, max(lb) / nb_)
                    aspect_ = max(seg_a[1] / seg_b[0], seg_b[1] / seg_a[0])
                    inner = [180.0 - x_ for x_ in _corner_turns(gmsh, t, sides)]
                    shape_past = []
                    for shape_, key_, _op, target_, _o, _s in _AIMS_NOW[0]:
                        if shape_ != "quad":
                            continue
                        if key_ == "aspect" and aspect_ > target_:
                            shape_past.append(f"aspect {aspect_:.2f}")
                        elif key_ == "min_angle_quad" and inner and min(inner) < target_:
                            shape_past.append(f"corner {min(inner):.0f} deg")
                        elif key_ == "max_angle_quad" and inner and max(inner) > target_:
                            shape_past.append(f"corner {max(inner):.0f} deg")
                    if shape_past:
                        fits = False
                        why_("one count fits, but its quads would be past the user's targets", past=shape_past,
                             counts=[na_, nb_])
                        continue
                    alike = max(alike, 1.0)
            if abs(la[0] - la[1]) > alike * max(la) or abs(lb[0] - lb[1]) > alike * max(lb):
                why_("long sides or ends not alike", alike=alike,
                     long_unequal=round(abs(la[0] - la[1]) / max(la), 3), ends_unequal=round(abs(lb[0] - lb[1]) / max(lb), 3),
                     few_rows=bool(max(lb) < FEW_ROWS * size_of[t]), long_enough=bool(min(la) >= STRIP_LONG * max(lb)),
                     one_count_fits=(None if high is None or low is None else
                                     [math.ceil(max(la) * mm / (0.98 * high) - 1e-9), math.floor(min(la) * mm / low + 1e-9)]))
                if alike > STRIP_ALIKE:
                    refused.append((t, a, b))
                continue
            if (min(la) < STRIP_LONG * max(lb) and not fits) or max(lb) * mm >= plan_rules.PANEL_WALLS * rows[k]["wall_mm"]:
                why_("not long enough for its width" if min(la) < STRIP_LONG * max(lb) else "wider than a panel wall",
                     strip_long=STRIP_LONG, panel_mm=round(plan_rules.PANEL_WALLS * rows[k]["wall_mm"], 2))
                continue
        across = size_of[t]
        if user_flat and not MAP_FLAT and max(lb) >= across and thin is None:
            why_("flat and wider than its size")
            continue
        beside = [size_of[s] for c in a for s in gmsh.model.getAdjacencies(1, c)[0] if s != t and s in size_of]
        if thin is not None:
            # (closed after meshing, so its elements along are the size of the faces it joins, under the user's
            # longest edge - not held to an aspect of its own width)
            along_t = min(beside) if beside else size_of[t]
            if high is not None:
                along_t = min(along_t, 0.98 * high / mm)
            if fine_at:
                near = min((fine_at.get(p, math.inf) for c in sides for p in ends[c]), default=math.inf)
                if near * plan_rules.GROWTH < along_t * mm:
                    continue
            n_a = max(1, math.ceil(max(la) / along_t - 1e-9))
            if EVEN:
                n_a = even(n_a, max(la))
            n_a = count.get(a[0], count.get(a[1], n_a))
            if any(count.get(c, n_a) != n_a for c in a) or any(count.get(c, 1) != 1 for c in b):
                continue
            for c in a:
                count[c] = n_a
            for c in b:
                count[c] = 1
            out[t] = (tuple(a), max(la) / n_a)
            nout[t] = (tuple(a), tuple(b), float(max(lb) * mm))
            continue
        stretch = STRETCH_FLAT if flat and not user_flat else STRETCH_MAX
        if sizing is not None and sizing.get("stretch") is not None:
            stretch = min(stretch, max(1.0, float(sizing["stretch"])))
        along = min([stretch * across] + beside)
        if sizing is None:
            along = max(along, across)
        else:
            # SHMESH-24: a strip narrower than the user's size has one element across it, as wide as the strip:
            # its length is held to the user's aspect ratio of THAT width, and under their longest edge
            seg = max(lb) / max(1, math.ceil(max(lb) / across - 1e-9))
            if sizing.get("stretch") is not None and SR.STRIP_ASPECT:
                along = min(along, max(1.0, float(sizing["stretch"])) * seg)
            if high is not None:
                along = min(along, 0.98 * high / mm)
            along = max(along, min(across, seg))
        if fine_at:
            # SHMESH-27: a mapped grid cannot grade. A strip with a smaller piece of the CAD at one of its corners
            # (sized more than one growth step under the strip's own elements) stays free, and grades down to it:
            # mapped, a tube corner's 10 mm elements fanned out of a 0.5 mm face at its end (sh0110)
            near = min((fine_at.get(p, math.inf) for c in sides for p in ends[c]), default=math.inf)
            if near * plan_rules.GROWTH < min(along, max(lb) / max(1, math.ceil(max(lb) / across - 1e-9))) * mm:
                why_("a finer piece of the CAD at a corner (a grid cannot grade)", corner_size_mm=round(near, 3))
                continue
        n_b = max(1, math.ceil(max(lb) / across - 1e-9))
        n_a = max(1, math.ceil(max(la) / along - 1e-9))
        if low is not None and STRIP_LOW:
            # SHMESH-63: never more segments than leave each over the user's shortest edge where fewer can (a 1.5 mm
            # bend's 2.36 mm arc held to a tenth of a 4 mm size off the arc took 2 rows of 1.18 mm; one row stands
            # 0.44 mm off it, 0.04 mm past the chord's hold)
            n_b = min(n_b, max(1, int(math.floor(min(lb) * mm / low + 1e-9))))
            n_a = min(n_a, max(1, int(math.floor(min(la) * mm / low + 1e-9))))
            if high is not None and UNEQUAL_FEASIBLE:
                # (and never so few that the long side's segments pass the user's longest edge)
                n_a = max(n_a, int(math.ceil(max(la) * mm / (0.98 * high) - 1e-9)))
        if EVEN:
            n_a, n_b = even(n_a, min(la) if UNEQUAL_FEASIBLE else max(la)), even(n_b, min(lb) if UNEQUAL_FEASIBLE else max(lb))
        k_p = {prefer[c] for c in a if c in prefer} if prefer else set()
        if len(k_p) == 1:
            # SHMESH-51o16: a strip whose long side faces a two-ring bolt box's side across one cell takes the box's
            # count along it where that count keeps its segments inside the user's edges and its stretch
            k_ = k_p.pop()
            if ((high is None or max(la) * mm / k_ <= 0.98 * high) and (low is None or min(la) * mm / k_ >= low)
                    and max(la) / k_ <= stretch * across + 1e-12):
                n_a = k_
        n_a = yielded(t, a, la, n_a, max(1, math.ceil(max(la) / along - 1e-9)))
        n_a = count.get(a[0], count.get(a[1], n_a))
        n_b = count.get(b[0], count.get(b[1], n_b))
        if any(count.get(c, n_a) != n_a for c in a) or any(count.get(c, n_b) != n_b for c in b):
            why_("a side already counted otherwise", wants=[n_a, n_b])
            if thin is None and alike > STRIP_ALIKE:
                refused.append((t, a, b))
            continue
        low_seg = low * (1.0 - LOW_BAND) if (low is not None and FIT_MAP) else low
        if alike > STRIP_ALIKE and sizing is not None and (
                (high is not None and max(max(la) / n_a, max(lb) / n_b) * mm > 0.98 * high)
                or (low is not None and any(
                    side / n_ * mm < low_seg and not (SHORT_SIDE_ONE and n_ == 1)   # (SHMESH-93: one is forced)
                    for side, n_ in ((min(la), n_a), (min(lb), n_b))))):
            # SHMESH-65: a strip mapped with unequal sides only where every segment of it stays inside the user's
            # edges (mapped regardless, a few-row strip's long side came out 4.7 mm against a 4.5 mm longest edge)
            why_("a segment would pass the user's edges", wants=[n_a, n_b],
                 seg_mm=[round(max(la) / n_a * mm, 2), round(min(la) / n_a * mm, 2), round(max(lb) / n_b * mm, 2),
                         round(min(lb) / n_b * mm, 2)], low_mm=low, high_mm=high)
            refused.append((t, a, b))
            continue
        for c in a:
            count[c] = n_a
        for c in b:
            count[c] = n_b
        out[t] = (tuple(a), max(la) / n_a)
        _FREE_WHY.pop(t, None)
        if thin is None and FIT_MAP and sizing is not None and (
                abs(la[0] - la[1]) > NARROW_ALIKE * max(la) or abs(lb[0] - lb[1]) > NARROW_ALIKE * max(lb)
                or min(la) < STRIP_LONG * max(lb)):
            _FIT_MAPPED[0] += 1
            _FIT_FACES.append(int(tag_of[t]))
    if TRANSITIONS and transitions_out is not None and low is not None and high is not None:
        # SHMESH-73: a few-row strip whose long sides cannot share one count inside the user's edges (28 and 10.5 mm
        # between 2 and 6 mm; or a neighbour strip already holds its long side at a count the other cannot take)
        # gets a count of its own on each long side, two apart or more, and its rows step from one to the other
        for t, a, b in refused:
            if t in out or t in transitions_out or t in not_thin or len(a) != 2 or len(b) != 2:
                continue
            la_ = tuple(float(gmsh.model.occ.getMass(1, c)) for c in a)
            lb_ = tuple(float(gmsh.model.occ.getMass(1, c)) for c in b)
            log_ = {"strip": int(t), "long_mm": [round(x_ * mm, 2) for x_ in la_],
                    "ends_mm": [round(x_ * mm, 2) for x_ in lb_],
                    "counted": [count.get(c) for c in (*a, *b)], "outcome": "stepped"}
            _STRIP_LOG.append(log_)
            if t in no_step:
                log_["outcome"] = "not stepped: its step could not be made in an earlier pass"
                continue
            if min(lb_) * mm < low or abs(lb_[0] - lb_[1]) > NARROW_ALIKE * max(lb_):
                # (its ends alike and each over the shortest edge: a strip tapering to a sliver end is no step's)
                log_["outcome"] = "not stepped: its ends unequal or under the shortest edge"
                continue
            rng = []
            for c, L in zip(a, la_, strict=True):
                lo_n = max(1, int(math.ceil(L * mm / (0.98 * high) - 1e-9)))
                hi_n = int(math.floor(L * mm / low + 1e-9))
                if c in count:
                    lo_n = hi_n = count[c]
                rng.append((lo_n, hi_n))
            if any(lo_n > hi_n for lo_n, hi_n in rng):
                log_["outcome"] = f"not stepped: a long side's count is held outside its edges {rng}"
                continue
            # the coarse side as few as its edges allow, the fine side the nearest count of the same parity above
            i_c = 0 if la_[0] <= la_[1] else 1
            lo_f, hi_f = rng[1 - i_c]

            def steps_to(n_c_: int, _a=a, _i=i_c, _lo=lo_f, _hi=hi_f) -> list[int]:
                got_ = [f_ for f_ in range(max(_lo, n_c_ + 2), _hi + 1)
                        if (f_ - n_c_) % 2 == 0 and f_ - n_c_ <= 2 * n_c_]
                return [f_ for f_ in got_ if f_ == count[_a[1 - _i]]] if _a[1 - _i] in count else got_

            n_c = rng[i_c][0] if a[i_c] not in count else count[a[i_c]]
            if a[i_c] not in count and a[1 - i_c] in count and not steps_to(n_c):
                # (when the fewest cannot step to the fine side's held count: the finest coarse count that can)
                n_c = next((m_ for m_ in range(rng[i_c][1], rng[i_c][0] - 1, -1) if steps_to(m_)), n_c)
            cand_f = steps_to(n_c)
            if not cand_f:
                log_["outcome"] = f"not stepped: no fine count for coarse {n_c} in {lo_f}..{hi_f}" + (
                    f" (held at {count[a[1 - i_c]]})" if a[1 - i_c] in count else "")
                continue
            n_f = cand_f[0]
            m_ = max(1, int(math.ceil(max(lb_) / size_of[t] - 1e-9)))
            m_ = min(m_, max(1, int(math.floor(min(lb_) * mm / low + 1e-9))))
            if any(count.get(c, m_) != m_ for c in b):
                log_["outcome"] = f"not stepped: its ends are counted {[count.get(c) for c in b]}, it needs {m_}"
                continue
            log_["outcome"] = f"stepped {n_c} to {n_f}"
            count[a[i_c]], count[a[1 - i_c]] = n_c, n_f
            for c in b:
                count[c] = m_
            transitions_out[t] = (a[i_c], a[1 - i_c], tuple(b))
    if chains:
        for t, k in sorted(tag_of.items()):
            if t not in out and t not in nout and rows[k]["rule"] != "SHMESH-15" and t not in not_thin:
                if not (TAPERS and taper(t, k)):
                    chain(t)
    for t_ in (*out, *(narrow_out or {}), *(transitions_out or {})):
        _FREE_WHY.pop(t_, None)
    for c, n in sorted(count.items()):
        gmsh.model.mesh.setTransfiniteCurve(c, n + 1)
    if counts_out is not None:
        counts_out.update({c: count[c] for t in out for c in
                           {abs(int(x)) for _d, x in gmsh.model.getBoundary([(2, t)], oriented=False)} if c in count})
        if narrow_out:
            # (SHMESH-60: a narrow face meshed free keeps its counts against the grids beside it)
            counts_out.update({c: count[c] for v_ in narrow_out.values() for c in (*v_[0], *v_[1]) if c in count})
        if transitions_out:
            counts_out.update({c: count[c] for v_ in transitions_out.values() for c in (v_[0], v_[1], *v_[2])
                               if c in count})
    for t in sorted(out):
        if t in corner_tags:
            gmsh.model.mesh.setTransfiniteSurface(t, cornerTags=corner_tags[t])
        else:
            gmsh.model.mesh.setTransfiniteSurface(t)
        gmsh.model.mesh.setRecombine(2, t)
    return out


def _rebuild(brep: Path, owners: list[str], out: Path, change) -> list[str]:
    """The sewn surface with some faces replaced: change(k, face) gives the faces that stand for face k (None:
    unchanged). The new faces and the rest are fused back into one conformal surface (a neighbour's edge is cut
    where a new corner lies on it), written to `out`; returns the owners, one per face of it."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    before = ms._faces(shape)
    if len(before) != len(owners):
        raise ValueError(f"the mid-surface has {len(before)} faces but the record names {len(owners)}")
    pieces: list[tuple[object, str]] = []
    changed = False
    moved: set[int] = set()
    for k, (f, own) in enumerate(zip(before, owners, strict=True)):
        got = change(k, f)
        changed = changed or got is not None
        if got is not None:
            moved.add(k)
        pieces += [(g, own) for g in (got if got is not None else [f])]
    if not changed:
        BRepTools.Write_s(shape, str(out))
        return list(owners)
    if len(pieces) == 1:
        BRepTools.Write_s(pieces[0][0], str(out))
        return [pieces[0][1]]
    if LOCAL_REBUILD:
        local = _rebuild_local(shape, before, owners, change, moved, out)
        if local is not None:
            return local

    def fuse(fuzzy: float):
        b = BOPAlgo_Builder()
        for g, _own in pieces:
            b.AddArgument(g)
        b.SetRunParallel(False)
        b.SetNonDestructive(True)
        if fuzzy > 0:
            b.SetFuzzyValue(fuzzy)
        b.Perform()
        return b

    bop = fuse(0.0)
    if bop.HasErrors():
        # SHMESH-11d: once more with a fuzzy value of twice the surface's own largest tolerance (its sewn edges'
        # gaps, measured): an exact fuse of a duct's pieces failed outright (fresh fr015)
        bop = fuse(2.0 * max(ms._max_tol(shape), 1e-7))
    if bop.HasErrors():
        raise ValueError("the re-made faces of the mid-surface could not be fused back")
    fused = bop.Shape()
    after = ms._faces(fused)
    index = TopTools_IndexedMapOfShape()
    for f in after:
        index.Add(f)
    owner_of: dict[int, str] = {}
    for g, own in pieces:
        images = [TopoDS.Face_s(x) for x in bop.Modified(g)] or ([] if bop.IsDeleted(g) else [g])
        for x in images:
            k = index.FindIndex(x)
            if k > 0:
                owner_of.setdefault(k, own)
    if len(owner_of) != len(after):
        raise ValueError("a re-made face of the mid-surface could not be traced to its mid-face")
    BRepTools.Write_s(fused, str(out))
    return [owner_of[k] for k in range(1, len(after) + 1)]


#: SHMESH-30, ONLY THE RE-MADE FACES AND THEIR NEIGHBOURS ARE FUSED BACK: SHMESH-11 re-makes a few faces (cut at their
#: seams, offsets as B-splines) and fused the WHOLE surface again to join them, every pair of faces whose boxes meet
#: intersected anew. On a printed keycap's free-form walls (dev t0044: 4 of its 53 faces re-made) that fuse took over
#: 700 s of a 704 s mesh; gmsh itself 3.6 s. Now the re-made faces are fused with the faces whose boxes meet theirs
#: only (the only pairs the general fuse intersects that involve a re-made face), and the rest are put back as they
#: were: they keep their edges, which the neighbours still share (a fuse that does not cut a face hands back its own
#: edges). Kept only when the surface comes back with no more free
#: edge than it had (to a millionth) and every face is traced to its mid-face; else the whole surface is fused, as
#: before.
LOCAL_REBUILD = True


def _rebuild_local(shape, before: list, owners: list[str], change, moved: set[int], out: Path) -> list[str] | None:
    """SHMESH-30: _rebuild with only the re-made faces (`moved`) and their vertex neighbours fused; None when that
    surface is not as joined as the one it replaces (the caller then fuses the whole)."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    # the faces a re-made face can touch: its box and theirs, each grown by the surface's largest tolerance, meet (the
    # general fuse intersects no other pair; two faces neither of which is re-made already meet as sewn)
    grow = 2.0 * max(ms._max_tol(shape), 1e-7)
    boxes = [ms._box(f) for f in before]
    near = set(moved)
    for k, (lo, hi) in enumerate(boxes):
        if k in near:
            continue
        for m in moved:
            mlo, mhi = boxes[m]
            if np.all(lo - grow <= mhi + grow) and np.all(mlo - grow <= hi + grow):
                near.add(k)
                break
    if len(near) >= len(before):
        return None
    args: list[tuple[object, str]] = []
    for k in sorted(near):
        got = change(k, before[k]) if k in moved else None
        args += [(g, owners[k]) for g in (got if got is not None else [before[k]])]
    b = BOPAlgo_Builder()
    for g, _own in args:
        b.AddArgument(g)
    b.SetRunParallel(False)
    b.SetNonDestructive(True)
    b.Perform()
    if b.HasErrors():
        return None
    faces: list = []
    names: list[str] = []
    index = TopTools_IndexedMapOfShape()
    for g, own in args:
        for x in [TopoDS.Face_s(y) for y in b.Modified(g)] or ([] if b.IsDeleted(g) else [g]):
            if not index.Contains(x):
                index.Add(x)
                faces.append(x)
                names.append(own)
    if len(ms._faces(b.Shape())) != len(faces):
        return None
    for k, f in enumerate(before):
        if k not in near:
            faces.append(f)
            names.append(owners[k])
    whole = ms._compound(faces)
    free = lambda sh: sum(ms._length(e) for e in ms._free_edge_list(sh))  # noqa: E731
    if free(whole) > free(shape) * (1.0 + 1e-6) + 1e-9:
        return None
    # each face of the surface as the mesher will read it (its own order), with its mid-face
    mine = TopTools_IndexedMapOfShape()
    for f in faces:
        mine.Add(f)
    order = [mine.FindIndex(f) for f in ms._faces(whole)]
    if len(order) != len(faces) or min(order) < 1:
        return None
    BRepTools.Write_s(whole, str(out))
    return [names[k - 1] for k in order]


#: SHMESH-11h: an OPEN offset face (no seam) is re-made as a B-spline only while its base surface has at most this
#: many poles; past it, it goes to gmsh as it is (a stamped panel's mid-faces, offsets of 153 x 103-pole skins, took
#: minutes each to convert - the whole shell mesh sat in it until its 15-minute stop - and were never cut)
OFFSET_OPEN_POLES = 2500


def _offset_worth(f) -> bool:
    """SHMESH-11h: whether an offset face is re-made as a B-spline before its seams are cut: always when it has a seam
    or lies on a closed or periodic surface (it is cut there, SHMESH-11g); an open one only while its base surface is
    small (OFFSET_OPEN_POLES)."""
    from OCP.BRep import BRep_Tool
    from OCP.Geom import Geom_BSplineSurface
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    ex = TopExp_Explorer(f, TopAbs_EDGE)
    while ex.More():
        if BRep_Tool.IsClosed_s(TopoDS.Edge_s(ex.Current()), f):
            return True
        ex.Next()
    surf = BRep_Tool.Surface_s(f)
    if surf.IsUClosed() or surf.IsVClosed() or surf.IsUPeriodic() or surf.IsVPeriodic():
        return True
    base = surf.BasisSurface()
    if isinstance(base, Geom_BSplineSurface):
        return base.NbUPoles() * base.NbVPoles() <= OFFSET_OPEN_POLES
    return True


def split_closed(brep: Path, owners: list[str], out: Path) -> list[str]:
    """SHMESH-11: every sewn face closed on itself (a full ring, a torus, a sphere: a seam in it) is cut at its seams
    into open pieces. gmsh cannot mesh some closed faces ("impossible to mesh periodic surface")."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
    from OCP.Geom import Geom_OffsetSurface
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideClosed

    from meshpipeline.cad import midsurface as ms

    def cut(_k, f):
        # SHMESH-11g: a face on an offset surface (a drafted wall's mid-face) is re-made as a B-spline first: cut at
        # its seam as an offset it came apart into invalid halves that the fuse dropped (fresh fr018: the whole cone
        # gone, a mesh of 0 elements), and gmsh reads offset surfaces as "Unknown" anyway
        remade = None
        if OFFSETS_AS_SPLINES and isinstance(BRep_Tool.Surface_s(f), Geom_OffsetSurface) and _offset_worth(f):
            got = ms._faces(BRepBuilderAPI_NurbsConvert(f, True).Shape())
            if len(got) == 1:
                remade = f = got[0]
        tool = ShapeUpgrade_ShapeDivideClosed(f)
        tool.SetNbSplitPoints(1)
        tool.Perform()
        got = ms._faces(tool.Result())
        return got if len(got) > 1 else ([remade] if remade is not None else None)

    return _rebuild(brep, owners, out, cut)


def respline(brep: Path, owners: list[str], face: int, out: Path, *, kin: bool = False,
             fixed: list | None = None) -> tuple[list[str], int]:
    """SHMESH-11: sewn face `face` re-made as a B-spline surface (exactly the same surface, without its period);
    with `kin`, every sewn face that lies on the same surface too (SHMESH-11b: a hull cylinder's four slotted halves
    each stopped gmsh in turn, a whole mesh each). Returns (the owners, how many faces were re-made); each face whose
    wires SHMESH-11i's fix changed is added to `fixed`."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
    from OCP.BRepTools import BRepTools
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from OCP.ShapeExtend import ShapeExtend_Status
    from OCP.ShapeFix import ShapeFix_Face
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad import midsurface as ms

    def _on(surf, p, tol):
        pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(p[0]), float(p[1]), float(p[2])), surf)
        return pr.NbPoints() > 0 and pr.LowerDistance() <= tol

    picked = {face}
    if kin:
        shape = TopoDS_Shape()
        BRepTools.Read_s(shape, str(brep), BRep_Builder())
        faces = ms._faces(shape)
        base = faces[face]
        kind = _surface_type(base)
        surf = BRep_Tool.Surface_s(base)
        tol = max(1e-6, 1e-6 * float(np.linalg.norm(np.subtract(*ms._box(base)))))
        for k, f in enumerate(faces):
            if k in picked or _surface_type(f) != kind:
                continue
            # on the same surface: points read inside the face lie on the failed face's surface
            pts = [ms._np(q) for q in _probe_points(f)]
            if pts and all(_on(surf, p, tol) for p in pts):
                picked.add(k)

    def convert(k, f):
        if k not in picked:
            return None
        if REMAKE_FIXED:
            # SHMESH-11i: its wires fixed first (a seam-cut sphere half's own wire read 23,552 mm2 as a B-spline
            # instead of its 20,735)
            fix = ShapeFix_Face(f)
            fix.Perform()
            if fixed is not None and fix.Status(ShapeExtend_Status.ShapeExtend_DONE):
                fixed.append(k)     # (the run record counts where the fix changed a face)
            f = fix.Face()
        nc = BRepBuilderAPI_NurbsConvert(f, True)
        return ms._faces(nc.Shape())

    new_owners = _rebuild(brep, owners, out, convert)
    if REMAKE_FIXED:
        # SHMESH-11i: a re-made surface that lost area (a face fused back with a broken trim: gmsh meshes nothing
        # there and says nothing) is not used
        before, after = _surface_area(brep), _surface_area(out)
        if abs(after - before) > REMAKE_AREA_TOL * before:
            raise _NotRemade(f"re-made as a B-spline, the surface read {after:.0f} of its {before:.0f} "
                             "(model units squared)")
    return new_owners, len(picked)


#: SHMESH-11i, A FACE IS RE-MADE FROM ITS FIXED WIRES, AND A RE-MAKE THAT LOSES AREA IS NOT USED: SHMESH-11 re-made a
#: face that stopped gmsh as a B-spline straight from its wires; on a stamped dome (s21/s27) the seam-cut sphere half
#: (an invalid face: its wire not closed on the surface) came out with the wrong trim and, fused back, both halves read
#: negative area - gmsh meshed neither and said nothing (41,470 of 69,794 mm2 delivered bare). Now the face's wires are
#: fixed first (ShapeFix_Face), and a re-made surface whose face areas sum other than the surface's before (past
#: REMAKE_AREA_TOL of it) raises _NotRemade: the face is left out and said (SHMESH-72).
REMAKE_FIXED = True
REMAKE_AREA_TOL = 0.01


class _NotRemade(ValueError):
    """SHMESH-11i: the face could not be re-made as a B-spline without losing the surface's area."""


def _surface_area(brep: Path) -> float:
    """The sewn faces' areas summed, each as it reads (a face whose trim came out turned inside out reads its small
    remainder, not its surface: s21's dome halves 248 and 2,562 of 20,735 each), model units squared."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    return sum(ms._area(f) for f in ms._faces(shape))


def _surface_type(face):
    """The face's surface type (OpenCASCADE GeomAbs)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return BRepAdaptor_Surface(face).GetType()


def _probe_points(face) -> list:
    """Three points of the face: its centre of mass laid on it, and two of its vertices."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list = []
    ex = TopExp_Explorer(face, TopAbs_VERTEX)
    while ex.More() and len(out) < 3:
        out.append(BRep_Tool.Pnt_s(TopoDS.Vertex_s(ex.Current())))
        ex.Next()
    return out


def _faces_and_facts(brep: Path, mm: float):
    """The sewn faces (OpenCASCADE MapShapes order), each with its centroid, width (mm) and curve radii (mm)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape

    from meshpipeline.cad import fea_measure as fm
    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    out = []
    for f in ms._faces(shape):
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        radii = []
        ex = TopExp_Explorer(f, TopAbs_EDGE)
        while ex.More():
            e = TopoDS.Edge_s(ex.Current())
            a = BRepAdaptor_Curve(e)
            if a.GetType() == GeomAbs_Circle:
                radii.append(a.Circle().Radius() * mm)
            ex.Next()
        out.append({"centroid": np.array([c.X(), c.Y(), c.Z()]), "area": g.Mass(), "width": ms.strip_width(f) * mm,
                    "box_width": ms.width_of(f) * mm, "radii": radii, "bend": fm.face_radius(f, mm),
                    "normal_face": f})
    return out


def _sheet_widths(brep: Path, facts: list[dict], owners: list[str], record: dict, mm: float) -> dict:
    """SHMESH-37: each sewn face's region (faces of one wall joined where the surface runs on smoothly,
    shell_plan.smooth_link) and that region's width, measured as one face's is (mm: its oriented box's middle side
    or twice its area over its own boundary, the narrower). The 400 x 240 mm panel of the patch study: 1,979 elements
    as 1 patch, and 2,005 / 2,178 / 2,513 as 4 / 12 / 30 (patch by patch: 7,937 / 22,468 / 2,523). Returns
    {"widths": per face (0 for a face alone), "size": faces in its region}."""
    from OCP.Bnd import Bnd_OBB
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    faces = ms._faces(shape)
    fmap = ms._map(shape, TopAbs_FACE)          # the same order as `faces` (1-based)
    mids = {m["id"]: m for m in record["mids"]}
    pairs = {p["id"]: p for p in record["pairs"]}

    def wall_of(k: int) -> float:
        return float(pairs[mids[owners[k]]["pair"]]["thickness_mm"])

    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    links: list[tuple[int, int]] = []
    for i in range(1, emap.Extent() + 1):
        ks = sorted({fmap.FindIndex(x) - 1 for x in emap.FindFromIndex(i)} - {-1})
        if len(ks) != 2:
            continue
        a, b = ks
        same = abs(wall_of(a) - wall_of(b)) <= 1e-6 * max(1.0, wall_of(a)) and (
            mids[owners[a]]["pair"] == mids[owners[b]]["pair"] or mids[owners[a]].get("body") == mids[owners[b]].get("body"))
        if not same:
            continue
        c = BRepAdaptor_Curve(TopoDS.Edge_s(emap.FindKey(i)))
        p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
        na = ms._face_normal_at(faces[a], (p.X(), p.Y(), p.Z()))
        nb = ms._face_normal_at(faces[b], (p.X(), p.Y(), p.Z()))
        if na is None or nb is None:
            continue
        cosv = abs(float(np.dot(na, nb))) / max(float(np.linalg.norm(na) * np.linalg.norm(nb)), 1e-300)
        angle = math.degrees(math.acos(min(1.0, cosv)))
        if plan_rules.smooth_link(angle, same_wall=True, bends=(facts[a]["bend"], facts[b]["bend"]),
                                  wall=min(wall_of(a), wall_of(b))):
            links.append((a, b))
    region = plan_rules.sheet_regions(len(faces), links)
    members: dict[int, list[int]] = {}
    for k, r in enumerate(region):
        members.setdefault(r, []).append(k)
    widths = [0.0] * len(faces)
    size = [1] * len(faces)
    # each edge's faces and length, for the regions' own boundaries
    edge_faces = []
    for i in range(1, emap.Extent() + 1):
        ks = sorted({fmap.FindIndex(x) - 1 for x in emap.FindFromIndex(i)} - {-1})
        edge_faces.append((ks, ms._length(emap.FindKey(i))))
    for ks in members.values():
        if len(ks) < 2:
            continue
        # the region measured as one face is: the narrower of its oriented box's middle side and twice its area
        # over its own boundary (the edges only one of its faces holds, holes included) - midsurface.strip_width
        inside = set(ks)
        area = sum(ms._area(faces[k]) for k in ks)
        rim = sum(length for fs, length in edge_faces if sum(1 for f in fs if f in inside) == 1)
        obb = Bnd_OBB()
        BRepBndLib.AddOBB_s(ms._compound([faces[k] for k in ks]), obb, True, True, False)
        w = sorted(2.0 * x for x in (obb.XHSize(), obb.YHSize(), obb.ZHSize()))[1]
        if rim > 0:
            w = min(w, 2.0 * area / rim)
        for k in ks:
            widths[k], size[k] = w * mm, len(ks)
    return {"widths": widths, "size": size}


def _reference_normal(face) -> np.ndarray | None:
    """The face's oriented normal at its middle (the side its sheet's normals are turned to)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    from meshpipeline.cad import midsurface as ms

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    c = g.CentreOfMass()
    n = ms._face_normal_at(face, np.array([c.X(), c.Y(), c.Z()]))
    if n is None:
        return None
    from OCP.TopAbs import TopAbs_REVERSED

    return -n if face.Orientation() == TopAbs_REVERSED else n


def _kind_words(kind: str) -> str:
    """SHMESH-74: a gmsh surface type in plain words."""
    k = str(kind).lower()
    return {"bspline surface": "B-spline surface", "plane": "flat", "cylinder": "cylinder", "cone": "cone",
            "sphere": "sphere", "torus": "torus (a fillet round a bend)"}.get(k, "surface" if k in ("", "unknown") else k)


def _fate_key(fact: dict) -> tuple[np.ndarray, float]:
    return np.asarray(fact["centroid"], dtype=float), float(fact["area"])


def _same_face(k1: tuple, k2: tuple, loose: bool = False) -> bool:
    """SHMESH-71/72: two keys (centre, area) name the same face - to a millionth, or (`loose`: a face re-made and fused
    back, which moves a little) within 5% of its area and a twentieth of its size."""
    (c1, a1), (c2, a2) = k1, k2
    if loose:
        size = max(a1, a2, 1e-300) ** 0.5
        return float(np.linalg.norm(c1 - c2)) <= 0.05 * size and abs(a1 - a2) <= 0.05 * max(a1, a2)
    scale = 1e-6 * max(1.0, float(np.abs(c1).max()))
    return float(np.linalg.norm(c1 - c2)) <= scale and abs(a1 - a2) <= 1e-6 * max(a1, a2, 1e-12)


def _curves_finer(gmsh, mm: float, size_mm: dict[int, float]) -> dict[int, int]:
    """SHMESH-71: the curves of this model a trip named (by centre and length, within a millionth), each with its
    segments: CURVE_FINER times those its length takes at the smaller size of its faces, per trip naming it, at least
    CURVE_MIN."""
    got: dict[int, int] = {}
    for _d, c in gmsh.model.getEntities(1):
        L = float(gmsh.model.occ.getMass(1, c))
        key = (np.asarray(gmsh.model.occ.getCenterOfMass(1, c), dtype=float), L)
        times = sum(1 for k_ in _FACE_FATE["curves"] if _same_face(key, k_))
        if times:
            sz = min((size_mm[s_] for s_ in gmsh.model.getAdjacencies(1, c)[0] if s_ in size_mm), default=None)
            base = max(1, int(np.ceil(L * mm / sz))) if sz else CURVE_MIN
            got[int(c)] = max(CURVE_MIN, int(np.ceil(base * CURVE_FINER ** times)))
    return got


def _fate_rung(fact: dict) -> str | None:
    """SHMESH-71: the ladder rung a face is on in this shell mesh (None: as the rules plan it)."""
    key = _fate_key(fact)
    got = [r_ for k_, r_ in _FACE_FATE["level"] if _same_face(key, k_, loose=True)]
    return FACE_LADDER[got[-1] - 1] if got and 0 < got[-1] <= len(FACE_LADDER) else None


def _fate_out(fact: dict) -> bool:
    key = _fate_key(fact)
    return any(_same_face(key, k_, loose=True) for k_ in (*_FACE_FATE["out"], *_FACE_FATE["out_path"]))


def _fate_save() -> None:
    """SHMESH-72: in a watched child, what it learned (faces left out, faces re-made, re-making seconds), for the
    parent to carry into the next child when this one is stopped."""
    d = _WATCH_DIR[0]
    if d is None:
        return
    row = {"out": [[list(map(float, c)), float(a)] for c, a in _FACE_FATE["out"]],
           "remade": [[list(map(float, c)), float(a)] for c, a in _FACE_FATE["remade"]],
           "remake_s": float(_FACE_FATE["remake_s"])}
    tmp_ = Path(d) / "fate.json.tmp"
    tmp_.write_text(json.dumps(row))
    tmp_.replace(Path(d) / "fate.json")


def _fate_load(d: Path) -> None:
    """SHMESH-72: the parent takes in what a stopped child learned (_fate_save)."""
    try:
        row = json.loads((Path(d) / "fate.json").read_text())
    except (OSError, ValueError):
        return
    for name in ("out", "remade"):
        for c, a in row.get(name) or []:
            key = (np.asarray(c, dtype=float), float(a))
            if not any(_same_face(key, k_) for k_ in _FACE_FATE[name]):
                _FACE_FATE[name].append(key)
    _FACE_FATE["remake_s"] = max(float(_FACE_FATE["remake_s"]), float(row.get("remake_s") or 0.0))


def _watch_tags(tag_of: dict[int, int], facts: list[dict], planned: dict[int, float] | None = None) -> None:
    """SHMESH-71: in a watched child, the face behind each gmsh surface tag (written before every 2D mesh: tags change
    from pass to pass), for the parent to name the face gmsh stops on. SHMESH-21f: `planned`, each tag's planned
    elements, for the parent to price how long gmsh may stay on it."""
    d = _WATCH_DIR[0]
    if d is None:
        return
    if planned is not None:
        (Path(d) / "face_planned.json").write_text(json.dumps({int(t): float(n) for t, n in planned.items()}))
    rows = {int(t): [list(map(float, facts[k]["centroid"])), float(facts[k]["area"])] for t, k in tag_of.items()}
    (Path(d) / "tags.json").write_text(json.dumps(rows))
    try:
        import gmsh

        curves = {int(c): [list(map(float, gmsh.model.occ.getCenterOfMass(1, c))), float(gmsh.model.occ.getMass(1, c))]
                  for _d, c in gmsh.model.getEntities(1)}
        (Path(d) / "curves.json").write_text(json.dumps(curves))
    except Exception:  # noqa: BLE001 - the face is named without its curves
        pass


def _watch_read(d: Path, seen: dict) -> tuple[int, float] | None:
    """SHMESH-71: (the surface gmsh is meshing, seconds on it) from the child's gmsh output; the deepest recovery
    level on it is kept in `seen` (the reading between calls). None while nothing was read."""
    import re

    try:
        with open(Path(d) / "gmsh.out", "rb") as fh:
            fh.seek(seen.get("pos", 0))
            chunk = fh.read()
            seen["pos"] = fh.tell()
    except OSError:
        return None
    for line in chunk.decode("utf-8", "replace").splitlines():
        m = re.search(r"Meshing surface (\d+)", line)
        if m:
            if int(m.group(1)) != seen.get("tag"):
                if seen.get("tag") is not None:
                    # (SHMESH-21e: the surfaces gmsh moved past - this mesh's measured progress)
                    seen.setdefault("done", set()).add(int(seen["tag"]))
                seen.update(tag=int(m.group(1)), since=time.monotonic(), level=0, curves=set())
            continue
        m = re.search(r"in the 1D mesh \(curves ([\d ]+)\)", line) or re.search(r"on curve (\d+)", line)
        if m and seen.get("tag") is not None:
            seen.setdefault("curves", set()).update(int(x) for x in m.group(1).split())
        m = re.search(r"trying again - level (\d+)", line)
        if m and seen.get("tag") is not None:
            seen["level"] = max(int(seen.get("level", 0)), int(m.group(1)))
    if seen.get("tag") is None:
        return None
    return int(seen["tag"]), time.monotonic() - float(seen["since"])


def _step_reset() -> None:
    _STEP_FAILED.clear()


def mesh_shell(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
               target: dict | None = None, correction: float = 1.0,
               requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """Mesh the sewn mid-surface: (the shell mesh in millimetres, the plan as applied: one row per face).
    `target`: the user's element count (SHMESH-23, fea_driver.element_target); `correction`: the built-over-planned a
    first mesh measured (mesh_run aims again with it). `requirements`: the mesh requirements read from the user's
    notes (engines/mesh_requirements; SHMESH-24..26) - None: the rules' own mesh, exactly as before.

    SHMESH-21: run in a forked child stopped after MESH_DEADLINE_S (ValueError, in words): gmsh can hang on a face
    with no way back to Python (fresh fr017's re-made faces: over 20 minutes on one 2D mesh), and a hung mesh must
    fail fast for the solid mesh to be delivered. Without fork (not Linux), or with no deadline, it runs here.
    SHMESH-21b: at the stop, the best whole mesh the run made so far is delivered, told; none only when it made no
    valid one (the ValueError as before)."""
    _step_reset()
    import multiprocessing as mp
    import tempfile

    _FACE_FATE.update(level=[], out=[], remade=[], t0=time.monotonic(), remake_s=0.0, ladder_spent=False, out_path=[], curves=[])
    if MESH_DEADLINE_S <= 0 or "fork" not in mp.get_all_start_methods():
        return _mesh_shell(brep, record, mm_per_unit=mm_per_unit, log=log, target=target, correction=correction,
                           requirements=requirements)
    allowed = _deadline_s()                     # (SHMESH-31g: what is left of the run's outer stop, at most 900 s)
    # SHMESH-21e: the clock the forked runs share (they inherit it at the fork): it moves out to the work's price
    _CLOCK.update(t0=time.monotonic(), base=allowed, allowed=allowed, price=None, planned=None, first_s=None,
                  built=None, capped=False, rate_seen=None, outer=Path(brep).parent / CLOCK_FILE)
    # SHMESH-21b: the whole meshes the forked runs make are kept in hand here; the best is delivered at the stop
    with tempfile.TemporaryDirectory(prefix="shell_best_") as bdir:
        _SO_FAR.update(dir=bdir if BEST_SO_FAR else None, mine=None, file=None, label="the first path", t0=time.time())
        try:
            out = _mesh_shell_watched(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                      correction=correction, requirements=requirements, allowed=allowed)
            out[1].setdefault("quality", {})["clock"] = _clock_view()
            if _CLOCK.get("first_s") and int(_CLOCK.get("built") or 0) >= RATE_MIN_ELEMENTS:
                # (SHMESH-21e: this part's measured rate prices its next mesh in this run)
                rate = float(_CLOCK["built"]) / max(float(_CLOCK["first_s"]), 1e-3)
                _RATE_SEEN["rate"] = min(rate, float(_RATE_SEEN.get("rate") or rate))
            return out
        except _PastLimit as exc:
            got = _so_far_deliver(bdir, requirements, float(_CLOCK["allowed"]), str(exc)) if BEST_SO_FAR else None
            if got is None:
                raise
            got[1].setdefault("quality", {})["clock"] = _clock_view()
            return got
        finally:
            _SO_FAR.update(dir=None, mine=None, file=None, label="the first path")
            _CLOCK.update(t0=None, base=None, allowed=None, outer=None)


def _mesh_shell_watched(brep, record, *, mm_per_unit, log, target, correction, requirements,
                        allowed: float) -> tuple[ShellMesh, dict]:
    """SHMESH-21/71: mesh_shell's forked runs, one after another while a face's watchdog trips (SHMESH-71/72), all
    under `allowed` seconds from now (_PastLimit past it)."""
    import tempfile

    trips = 0
    started = time.monotonic()
    if _CLOCK.get("allowed") is None:
        _CLOCK.update(t0=started, base=allowed, allowed=allowed)
    while True:
        watch = FACE_WATCH and trips < FACE_WATCH_TRIPS
        t_child = time.monotonic()
        if not watch:
            # SHMESH-21f: an unwatched child has no sign of progress to read: held to the floor's limit
            _CLOCK["allowed"] = float(_CLOCK["base"])
        allowed = float(_CLOCK["allowed"])      # (SHMESH-21e: moved out by the work's price, read at each child)
        with tempfile.TemporaryDirectory(prefix="shell_watch_") as wdir:
            back = _mesh_shell_child(brep, record, mm_per_unit=mm_per_unit, target=target, correction=correction,
                                     requirements=requirements, watch_dir=wdir if watch else None,
                                     deadline=max(1.0, allowed - (time.monotonic() - started)))
            if back[0] == "trip":
                _fate_load(Path(wdir))
        if back[0] == "trip":
            # SHMESH-71/72: a face held gmsh (or a re-make of it ran on): one rung down its ladder, or left out
            kind, (c_, a_) = back[1][:2]
            key = (np.asarray(c_, dtype=float), float(a_))
            named = back[1][2] if len(back[1]) > 2 else []
            trips += 1
            if kind == "rebuild":
                _FACE_FATE["out"].append(key)
                continue
            now = [r_ for k_, r_ in _FACE_FATE["level"] if _same_face(key, k_, loose=True)]
            rung = (now[-1] if now else 0) + 1
            if rung == 1 and not (named and CURVE_RUNG):
                rung = 2                    # (no curve named: the curve rung has nothing to mesh finer)
            if named and CURVE_RUNG and rung <= 2:
                # SHMESH-71: the curves gmsh named are meshed finer in the next child (finer again if named again)
                for cc, ll in named:
                    ck = (np.asarray(cc, dtype=float), float(ll))
                    _FACE_FATE["curves"].append(ck)
                rung = 1 if not now else 2
            took = time.monotonic() - t_child
            left = float(_CLOCK["allowed"]) - (time.monotonic() - started)
            if rung > len(FACE_LADDER) or _FACE_FATE["ladder_spent"] or left < 2.0 * took:
                # (past its last rung; or no rung made a face this run; or no time for a rung and the mesh after it)
                if rung > len(FACE_LADDER):
                    _FACE_FATE["ladder_spent"] = True
                _FACE_FATE["out"].append(key)
            else:
                _FACE_FATE["level"].append((key, rung))
            continue
        status, out, got = back
        if status != "ok":
            raise ValueError(out)
        if log is not None:
            log.extend(got)
        if trips:
            out[1]["quality"].setdefault("face_watch", {})["trips"] = trips
        return out


def _mesh_shell_child(brep, record, *, mm_per_unit, target, correction, requirements, watch_dir, deadline):
    """SHMESH-21/71: one forked run of _mesh_shell: ("ok", (mesh, applied), log) or ("error", words, None); with
    `watch_dir`, gmsh's own output goes to a file there and a face it holds past the watchdog (or a re-make whose fuse
    runs on) stops the child: ("trip", ("gmsh" | "rebuild", (that face's centre, area)))."""
    import multiprocessing as mp
    import os

    recv, send = mp.get_context("fork").Pipe(duplex=False)

    def child() -> None:
        try:
            if watch_dir is not None:
                # (gmsh's own words to a file the parent reads: which surface, which recovery level)
                fd = os.open(str(Path(watch_dir) / "gmsh.out"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                os.dup2(fd, 1)
                _WATCH_DIR[0] = watch_dir
                gmsh_mod = __import__("gmsh")
                orig_set = gmsh_mod.option.setNumber

                def set_number(name, value, _o=orig_set):
                    _o(name, value)
                    if name == "General.Terminal":
                        _o("General.Terminal", 1)
                        _o("General.Verbosity", 5)
                gmsh_mod.option.setNumber = set_number
            got: list = []
            mesh, applied = _mesh_shell(brep, record, mm_per_unit=mm_per_unit, log=got, target=target,
                                        correction=correction, requirements=requirements)
            send.send(("ok", (mesh, applied), got))
        except BaseException as exc:  # noqa: BLE001 - told to the parent, which raises it there
            send.send(("error", f"{type(exc).__name__}: {exc}", None))
        finally:
            send.close()
            os._exit(0)

    proc = mp.get_context("fork").Process(target=child, daemon=True)
    proc.start()
    send.close()
    t0 = time.monotonic()
    seen: dict = {}
    try:
        while True:
            if watch_dir is not None:
                # SHMESH-21e: the child priced its work: the deadline moves out by what the clock gained
                deadline += _clock_take(Path(watch_dir))
            left = deadline - (time.monotonic() - t0)
            if left <= 0:
                raise _PastLimit(past_limit_words(float(_CLOCK.get("allowed") or deadline), _CLOCK))
            if recv.poll(min(left, FACE_WATCH_POLL) if watch_dir is not None else left):
                if watch_dir is not None:
                    _clock_take(Path(watch_dir))        # (SHMESH-21e: the work it was priced on, for the record)
                return recv.recv()
            if watch_dir is None:
                continue
            try:
                # SHMESH-72: a re-make whose fuse back runs past REBUILD_WATCH_S
                rb = json.loads((Path(watch_dir) / "rebuild.json").read_text())
                if time.time() - float(rb["since"]) > REBUILD_WATCH_S:
                    logger.info("shell mesh: a face's re-make ran past %.0f s; stopped (SHMESH-72)", REBUILD_WATCH_S)
                    return ("trip", ("rebuild", (rb["centre"], rb["area"])))
            except (OSError, ValueError, KeyError):
                pass
            now = _watch_read(Path(watch_dir), seen)
            if now is None:
                continue
            # SHMESH-21e: the rate this part's faces are measured to mesh at prices the work again
            deadline += _progress_take(Path(watch_dir), seen)
            tag, held = now
            if ((FACE_WATCH_S is not None and held > FACE_WATCH_S) or int(seen.get("level", 0)) >= FACE_WATCH_LEVEL
                    or _face_stuck(Path(watch_dir), tag, held)):
                try:
                    rows = json.loads((Path(watch_dir) / "tags.json").read_text())
                except (OSError, ValueError):
                    continue
                if str(tag) not in rows:
                    continue
                logger.info("shell mesh: gmsh held surface %s for %.0f s (recovery level %s); stopped (SHMESH-71)",
                            tag, held, seen.get("level", 0))
                try:
                    cmap = json.loads((Path(watch_dir) / "curves.json").read_text())
                except (OSError, ValueError):
                    cmap = {}
                named = [cmap[str(c)] for c in sorted(seen.get("curves") or ()) if str(c) in cmap]
                return ("trip", ("gmsh", tuple(rows[str(tag)]), named))
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join()
        recv.close()


#: SHMESH-31b: the run's clock - when the shell mesh must be done (monotonic; None: no deadline), how long its first
#: path took to its first whole mesh (what any trial is priced at), and whether a spot pass was cut off. Set by
#: _mesh_shell (a shell mesh runs one at a time in its process); a bounded child sees its own end
_RUN: dict[str, Any] = {"end": None, "first_s": None, "cut": False}

#: SHMESH-31g: ONE CLOCK - the process that runs the shell mesh (fea_driver.shell_pass's shell_run, or any runner with
#: an outer stop) tells it when that stop comes, as seconds since the epoch in this environment variable; the shell
#: mesh then takes what is left before it less END_MARGIN_S (the decks still to write), at most MESH_DEADLINE_S, so no
#: pass starts that cannot finish before the outer stop. On the integration board a part extracted for 620-730 s
#: then meshed under its own 900 s from there, and the run was cut from outside with no mesh. The name is declared
#: in the settings inventory (Platform-supplied: the parent process sets it) and read through the settings loader
SHELL_END_ENV = "HEXERA_SHELL_RUN_END"
END_MARGIN_S = 60.0


#: SHMESH-21e: the run's HARD CAP (seconds since the epoch, set beside SHELL_END_ENV by the runner): what the job can
#: give the shell run at most (fea_driver.shell_pass: half of what the attempt has left, the machine's wall; the
#: surface run: its share). SHELL_END_ENV is the run's usual stop; a mesh priced past it is given its price up to
#: this cap, and the runner's stop follows (run_following). Unset: no cap but the price
SHELL_CAP_ENV = "HEXERA_SHELL_RUN_CAP"
#: SHMESH-21e: where a shell mesh says when it must now be done (beside its surface file), for the runner's stop
CLOCK_FILE = "shell_clock.json"
#: SHMESH-21e: the shell mesh's clock in this process: its start (monotonic), today's limit (`base`, SHMESH-21/31g),
#: the limit in force (`allowed`, seconds from the start), and the work it was priced on
_CLOCK: dict[str, Any] = {"t0": None, "base": None, "allowed": None, "price": None, "planned": None, "first_s": None,
                          "built": None, "capped": False, "outer": None}


def _env_end(name: str) -> float | None:
    """SHMESH-31g/21e: a runner's stop (seconds since the epoch) from the environment; None when unset or unread."""
    from meshpipeline.settings.env import optional_env

    # (each name read as written, for the settings inventory's check)
    raw = (optional_env("HEXERA_SHELL_RUN_CAP", "") if name == SHELL_CAP_ENV
           else optional_env("HEXERA_SHELL_RUN_END", ""))
    try:
        end = float(raw or "nan")
    except ValueError:
        return None
    return end if end == end else None


def _deadline_s() -> float:
    """SHMESH-31g: the seconds this shell mesh may take from now - MESH_DEADLINE_S, or less where the run's outer stop
    (SHELL_END_ENV) comes sooner (never under 1 s). SHMESH-21e moves it out once the work is priced (_priced_total)."""
    allowed = float(MESH_DEADLINE_S)
    end = _env_end(SHELL_END_ENV)
    if end is not None:
        allowed = min(allowed, end - time.time() - END_MARGIN_S)
    return max(1.0, allowed)


def mesh_price_s(planned: float | None, first_s: float | None = None, rate: float | None = None) -> float:
    """SHMESH-21e: the seconds a shell mesh's work is priced at - PRICE_FACTOR x its planned elements at MESH_RATE,
    or PRICE_FACTOR x its first whole mesh's measured seconds where that is longer. `rate`: elements a second
    measured on this part (its faces as gmsh meshes them, or a whole mesh this run made before, _RATE_SEEN): the
    planned elements are priced at it where it is slower than MESH_RATE."""
    seen = [r for r in (rate, _RATE_SEEN.get("rate")) if r]
    at = min([MESH_RATE, *seen])
    price = PRICE_FACTOR * max(float(planned or 0.0), 0.0) / at
    if first_s:
        price = max(price, PRICE_FACTOR * float(first_s))
    return price


#: SHMESH-21e: the slowest rate (elements a second) a whole shell mesh made in THIS process measured - a shell or
#: surface run's own process meshes one part (its retries, its re-aim, SURF-5's re-sizing), so the next mesh of the
#: part is priced at what this part was measured to do, not only at MESH_RATE
_RATE_SEEN: dict[str, float | None] = {"rate": None}
#: ... from a mesh of at least this many elements (a small mesh's seconds are mostly its set-up, not its rate)
RATE_MIN_ELEMENTS = 20_000
#: SHMESH-21e: the faces gmsh is seen to finish price the mesh while it runs, once PROGRESS_SHARE of the planned
#: elements are done and PROGRESS_MIN_S have passed: rate = the planned elements of the faces finished over the
#: seconds from the first of them to the start of the face now on (a face gmsh stays on never lowers it - a stuck
#: face is SHMESH-21f's)
PROGRESS_SHARE = 0.05
PROGRESS_MIN_S = 30.0


def _clock_outer() -> None:
    """SHMESH-21e: the runner told when the shell mesh must now be done (CLOCK_FILE beside the surface file)."""
    outer = _CLOCK.get("outer")
    if outer is None or _CLOCK.get("t0") is None:
        return
    try:
        end = time.time() + float(_CLOCK["allowed"]) - (time.monotonic() - float(_CLOCK["t0"]))
        Path(outer).write_text(json.dumps({**_clock_view(), "end": end}))
    except OSError:
        pass


def _progress_take(watch_dir: Path, seen: dict) -> float:
    """SHMESH-21e: in the parent, the seconds the watched child's measured progress adds to the limit (0 when none):
    the rate its finished faces meshed at (face_planned.json, the surfaces gmsh moved past in `seen`) prices the
    whole plan again (mesh_price_s); a longer price moves the limit out (never past the cap), told to the runner."""
    if _CLOCK.get("allowed") is None or _CLOCK.get("t0") is None:
        return 0.0
    p = watch_dir / "face_planned.json"
    try:
        mtime = p.stat().st_mtime
        if mtime != seen.get("fp_mtime"):
            # (a new 2D mesh: tags change from pass to pass - its faces are counted from the face now on)
            seen.update(fp_mtime=mtime, fp=json.loads(p.read_text()), done=set(), first_at=seen.get("since"))
    except (OSError, ValueError):
        return 0.0
    fp, done = seen.get("fp") or {}, seen.get("done") or set()
    total = sum(float(v) for v in fp.values())
    n_done = sum(float(fp.get(str(t), 0.0)) for t in done)
    span = float(seen.get("since") or 0.0) - float(seen.get("first_at") or seen.get("since") or 0.0)
    if total <= 0 or n_done < PROGRESS_SHARE * total or span < PROGRESS_MIN_S:
        return 0.0
    rate = n_done / span
    planned = max(float(_CLOCK.get("planned") or 0.0), total)
    price = mesh_price_s(planned, _CLOCK.get("first_s"), rate=rate)
    total_s, capped = _priced_total(price, float(_CLOCK["base"]), time.monotonic() - float(_CLOCK["t0"]))
    _CLOCK["rate_seen"] = round(rate, 1)
    gain = total_s - float(_CLOCK["allowed"])
    if gain <= 0:
        return 0.0
    _CLOCK.update(allowed=total_s, price=price, capped=capped)
    logger.info("shell mesh: faces measured at %.0f elements a second; the limit is now %.0f s (SHMESH-21e)",
                rate, total_s)
    _clock_outer()
    return gain


def _priced_total(price: float, base: float, since_s: float) -> tuple[float, bool]:
    """SHMESH-21e: (the seconds from the shell mesh's start it may take, whether the run's hard cap held it): `base`
    (today's limit) unless the price runs past MESH_DEADLINE_S - then the price, never past SHELL_CAP_ENV less
    END_MARGIN_S (`since_s`: seconds since the mesh started), never under `base`."""
    if MESH_DEADLINE_S <= 0 or price <= max(float(base), float(MESH_DEADLINE_S)):
        return float(base), False
    total, capped = float(price), False
    cap = _env_end(SHELL_CAP_ENV)
    if cap is not None and cap - time.time() - END_MARGIN_S + since_s < total:
        total, capped = cap - time.time() - END_MARGIN_S + since_s, True
    return max(float(base), total), capped


def _reprice(planned: float | None = None, first_s: float | None = None, built: int | None = None) -> None:
    """SHMESH-21e: in a watched shell mesh child, its work priced again - on the largest plan made so far and, once a
    first whole mesh stands, its measured seconds - and the run's clock (_RUN's end) moved out to it; the parent reads
    the new limit from the watch folder (_clock_take). Nothing in an unwatched child (SHMESH-21f) or with no clock."""
    if _CLOCK.get("t0") is None or _WATCH_DIR[0] is None or _RUN.get("end") is None:
        return
    if planned is not None:
        _CLOCK["planned"] = max(float(_CLOCK.get("planned") or 0.0), float(planned))
    if first_s is not None and _CLOCK.get("first_s") is None:
        _CLOCK.update(first_s=float(first_s), built=int(built or 0) or None)
    price = mesh_price_s(_CLOCK.get("planned"), _CLOCK.get("first_s"))
    since = time.monotonic() - float(_CLOCK["t0"])
    total, capped = _priced_total(price, float(_CLOCK["base"]), since)
    _CLOCK.update(price=price, capped=capped)
    if total > float(_CLOCK["allowed"]):
        _CLOCK["allowed"] = total
        _RUN["end"] = max(float(_RUN["end"]), float(_CLOCK["t0"]) + total)
    try:
        (Path(_WATCH_DIR[0]) / CLOCK_FILE).write_text(json.dumps(_clock_view()))
    except OSError:
        pass


def _clock_view() -> dict:
    """SHMESH-21e: the clock as the record keeps it (seconds)."""
    c = _CLOCK
    return {"rule": "SHMESH-21e", "floor_s": float(MESH_DEADLINE_S), "base_s": c.get("base"),
            "limit_s": None if c.get("allowed") is None else round(float(c["allowed"]), 1),
            "price_s": None if c.get("price") is None else round(float(c["price"]), 1),
            "planned": None if c.get("planned") is None else int(c["planned"]),
            "first_s": None if c.get("first_s") is None else round(float(c["first_s"]), 1),
            "built": c.get("built"), "capped": bool(c.get("capped")), "rate": MESH_RATE, "factor": PRICE_FACTOR,
            "rate_seen": c.get("rate_seen"), "rate_before": _RATE_SEEN.get("rate")}


def _clock_take(watch_dir: Path) -> float:
    """SHMESH-21e: in the parent, the seconds a watched child's priced clock gained since last read (0 when none);
    the new limit is kept (_CLOCK), the work it was priced on with it, and the runner is told when the shell mesh
    must now be done (CLOCK_FILE beside the surface file, run_following)."""
    try:
        got = json.loads((watch_dir / CLOCK_FILE).read_text())
    except (OSError, ValueError):
        return 0.0
    if _CLOCK.get("allowed") is None or got.get("limit_s") is None:
        return 0.0
    price = max(float(got.get("price_s") or 0.0), float(_CLOCK.get("price") or 0.0)) or None
    _CLOCK.update(price=price, planned=got.get("planned"), first_s=got.get("first_s"),
                  built=got.get("built"), capped=bool(got.get("capped")) or bool(_CLOCK.get("capped")))
    gain = float(got["limit_s"]) - float(_CLOCK["allowed"])
    if gain <= 0:
        return 0.0
    _CLOCK["allowed"] = float(got["limit_s"])
    _clock_outer()
    return gain


def _face_stuck(watch_dir: Path, tag: int, held: float) -> bool:
    """SHMESH-21f: while the limit runs past MESH_DEADLINE_S, whether gmsh has been on surface `tag` longer than its own
    price (PRICE_FACTOR x its planned elements at MESH_RATE, at least FACE_HELD_FLOOR_S)."""
    if _CLOCK.get("allowed") is None or float(_CLOCK["allowed"]) <= max(float(_CLOCK["base"]), MESH_DEADLINE_S):
        return False
    if held <= FACE_HELD_FLOOR_S:
        return False
    try:
        n = float(json.loads((watch_dir / "face_planned.json").read_text()).get(str(tag)) or 0.0)
    except (OSError, ValueError, AttributeError):
        n = 0.0
    return held > max(FACE_HELD_FLOOR_S, PRICE_FACTOR * n / MESH_RATE)


def run_env(base_s: float, cap_s: float, now: float | None = None) -> dict[str, str]:
    """SHMESH-31g/21e: the two stops a runner gives its shell run - its usual one and the hard cap."""
    t = time.time() if now is None else now
    return {SHELL_END_ENV: f"{t + base_s:.3f}", SHELL_CAP_ENV: f"{t + max(base_s, cap_s):.3f}"}


def outer_end(started: float, base_s: float, cap_s: float, folder: Path | str) -> float:
    """SHMESH-21e: when a runner stops its shell run (seconds since the epoch): `base_s` after `started`, or later where
    a shell mesh in `folder` (any CLOCK_FILE in it or one level down) must be done later - plus END_MARGIN_S for what
    follows the mesh, as SHMESH-31g keeps it - never past `cap_s`."""
    end = started + base_s
    root = Path(folder)
    for p in (root / CLOCK_FILE, *root.glob(f"*/{CLOCK_FILE}")):
        try:
            got = float(json.loads(p.read_text()).get("end") or 0.0)
        except (OSError, ValueError, AttributeError):
            continue
        end = max(end, got + END_MARGIN_S)
    return min(end, started + max(base_s, cap_s))


def run_following(argv: list[str], *, base_s: float, cap_s: float, folder: Path | str,
                  env: dict | None = None, poll_s: float = 5.0):
    """SHMESH-21e: a shell run in its own process, stopped at outer_end - its usual stop, moved out when the shell
    mesh's limit follows its work, never past the hard cap: the runner's stop never cuts below the mesh's own limit.
    Returns (the finished process's CompletedProcess, the seconds it was allowed); past the stop, the process is
    killed and subprocess.TimeoutExpired raised with `timeout` the seconds it was allowed. `env`: the whole
    environment the run gets (the caller forwards its own), the two stops added (run_env); None: this process's
    environment as it is, no stops told (the shell mesh's own limit only)."""
    import subprocess

    started = time.time()
    for p in (Path(folder) / CLOCK_FILE, *Path(folder).glob(f"*/{CLOCK_FILE}")):
        try:
            p.unlink()                          # (a clock left by an earlier run says nothing of this one)
        except OSError:
            pass
    full = None if env is None else {**env, **run_env(base_s, cap_s, started)}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=full)
    while True:
        end = outer_end(started, base_s, cap_s, folder)
        wait = min(poll_s, end - time.time())
        try:
            out, err = proc.communicate(timeout=max(0.01, wait))
            return subprocess.CompletedProcess(argv, proc.returncode, out, err), end - started
        except subprocess.TimeoutExpired:
            if time.time() < end:
                continue
            proc.kill()
            proc.communicate()
            raise subprocess.TimeoutExpired(argv, end - started) from None


def run_stopped_words(allowed_s: float, folder: Path | str) -> str:
    """SHMESH-21e: what the user reads when a runner stopped its shell run from outside (run_following): how long it
    was given, the shell mesh's plan when one was priced (CLOCK_FILE), and the way on."""
    planned, capped = None, False
    root = Path(folder)
    for p in (root / CLOCK_FILE, *root.glob(f"*/{CLOCK_FILE}")):
        try:
            got = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        planned, capped = got.get("planned") or planned, bool(got.get("capped")) or capped
    mesh = f"its shell mesh, about {int(planned):,} elements planned" if planned else "its shell mesh"
    held = ", all the time this job had left for it" if capped else ""
    return (f"it took longer than the {_minutes(allowed_s)} allowed{held} (reading the part, then {mesh}) and was "
            "stopped; a larger element size makes a smaller mesh that finishes sooner")


def _minutes(s: float) -> str:
    return f"{s / 60:.0f} minutes" if s >= 60 else f"{s:.0f} s"


def past_limit_words(allowed: float, clock: dict | None = None) -> str:
    """SHMESH-21/21e: what the user reads when the shell mesh is stopped with no whole mesh made: how long it ran, the
    work it was priced on, and the way on (a larger element size). The neighbouring words' rule id kept."""
    c = clock or {}
    planned = c.get("planned")
    asked = f" (about {int(planned):,} elements planned)" if planned else ""
    held = (" - all the time this job had left for it" if c.get("capped") else "")
    # (SHMESH-12b's marker: the runs tell these words, not the error's class name)
    return (f"{SAID}the shell mesh{asked} ran for its {_minutes(allowed)}{held} and was stopped before it made a whole mesh; "
            "a larger element size makes a smaller mesh that finishes sooner (SHMESH-21)")


def _limit() -> float | None:
    """SHMESH-31b: the seconds a spot pass or a trial may run - the time left to the deadline less TIME_MARGIN_S for
    what must follow it; None: no deadline."""
    end = _RUN.get("end")
    return None if end is None else end - time.monotonic() - TIME_MARGIN_S


def _affords(price: float | None) -> bool:
    """SHMESH-31b: whether a pass priced at `price` seconds (the first mesh's own time) fits in _limit()."""
    lim = _limit()
    return lim is None or (lim > 0 and lim >= float(price or 0.0))


#: SHMESH-31f: what an optional whole mesh for a lower-ranked aim (a ring or grid trial, a line trial, narrow-along, a
#: step comparison) is told when it is not started
TRIAL_KEPT_WHY = "not tried: time kept for the solver-limit pass (SHMESH-31f)"


def _affords_trial(past_n: int, requirements: dict | None = None) -> bool:
    """SHMESH-31f: whether an optional whole mesh for an aim ranked under the solver's limits (structure, the user's
    lines, edge lengths) may start - the time still owed to higher-ranked work is set aside first: with elements past
    the solver's limits in the mesh as it stands (`past_n`, under the user's requirements, where SHMESH-31 makes a spot
    pass), a spot pass's price (the first mesh's own time, its clean-up in it) is reserved, and the trial (priced the
    same) starts only if it fits in what is left. Every stream's optional trial calls this before it starts; one that
    does not start records TRIAL_KEPT_WHY. On s0054 a bolt-ring trial took the last whole mesh's worth of time and the
    spot pass that brings the mesh inside Nastran's limits was not started: the part was delivered past them."""
    price = float(_RUN.get("first_s") or 0.0)
    owed = price if (past_n and requirements and OWN_SIZE_RETRIES) else 0.0
    lim = _limit()
    return lim is None or (lim > 0 and lim >= price + owed)


def _affords_step(price: float, past_n: int, requirements: dict | None = None) -> bool:
    """SHMESH-31f for a step inside one mesh (ranked under the solver's limits): whether a step priced at `price`
    seconds fits in what is left of the run's clock once the spot pass owed to `past_n` elements past the solver's
    limits is set aside (priced at the first mesh's own time, as _affords_trial)."""
    owed = float(_RUN.get("first_s") or 0.0) if (past_n and requirements and OWN_SIZE_RETRIES) else 0.0
    lim = _limit()
    return lim is None or (lim > 0 and lim >= float(price) + owed)


#: SHMESH-21b: THE BEST MESH SO FAR AT THE LIMIT - every whole mesh a shell mesh makes (each path's mesh, #435's mesh,
#: the per-face keep's) is left in hand: written with its record to a folder the forked run's parent owns, whenever it
#: stands better (_so_far_beats) than the best this process wrote before it. When SHMESH-21's stop (or the run's one
#: clock, SHMESH-31g) ends the run, the parent delivers the best of them and the record and a note say so; only a run
#: with no valid whole mesh ends with none, as before. A stamped lab part (s33, crests r45 across) worked through the
#: stamped stream's candidates to 910 s and was stopped with no mesh, its first path's mesh long made. Writing a mesh
#: changes no mesh. False: the stop delivers nothing, as before
BEST_SO_FAR = True
#: (the folder for this shell mesh's forked runs, set by mesh_shell; the best this process wrote - its standing figures
#: and file; what the mesh being made is called in the record; when the shell mesh started, seconds since the epoch)
_SO_FAR: dict[str, Any] = {"dir": None, "mine": None, "file": None, "label": "the first path", "t0": 0.0}


#: SHMESH-12b: a failure's words written for the user (the runs' records say them as they stand, with no
#: exception name): what follows this mark, through every re-raise and the forked child's "Type: message"
SAID = "[said] "


def said_words(exc: BaseException | str) -> str | None:
    """SHMESH-12b: the user's words a failure carries (after SAID), else None."""
    text = str(exc)
    return text.split(SAID, 1)[1].strip() if SAID in text else None


def too_small_words(box: tuple | list, mm: float, size_mm: float) -> str:
    """SHMESH-12b: a part smaller than the size it is meshed at, said with the way on. `box`: gmsh's bounding box
    in model units; `mm`: mm per unit; `size_mm`: the size every face was measured against."""
    span = max(float(box[3]) - float(box[0]), float(box[4]) - float(box[1]), float(box[5]) - float(box[2])) * mm
    fits = max(span / 10.0, 1e-6)
    return (f"the part is only {span:.3g} mm across and every face of it is smaller than one {size_mm:.3g} mm "
            f"element: ask for an element size under about {fits:.2g} mm (a tenth of the part), or, if the part "
            "should be larger, check the unit the file was read in")


class _PastLimit(ValueError):
    """SHMESH-21: the forked shell mesh ran past its deadline and was stopped (the words as before)."""


def _so_far_view(mesh: ShellMesh, applied: dict, past_n: int) -> dict:
    """SHMESH-21b: what _so_far_beats reads of a whole mesh - kept apart from the record, which goes on changing."""
    import copy

    q = applied.get("quality") or {}
    rows = (applied.get("requirements") or {}).get("rows") or []
    return {"past": _past(applied), "gone": float((q.get("face_watch") or {}).get("left_out_share") or 0.0),
            "applied": {"requirements": {"rows": copy.deepcopy(rows)}}, "past_n": int(past_n),
            "trias": int(len(mesh.trias)), "elements": int(mesh.n_elements)}


def _so_far_beats(a: dict, b: dict, requirements: dict | None) -> bool:
    """SHMESH-21b: whether whole mesh `a` stands better than `b` (_so_far_view), in the order the mesher keeps one
    candidate over another: valid, then inside Nastran's limits (_past), then less of the part left out (SHMESH-72),
    then SHMESH-36's standing against each other (the user's lines; SHMESH-36b where each is worse on some line), then
    fewer triangles, then fewer elements. A tie keeps `b` (the mesh made first)."""
    head_a, head_b = (a["past"][0], a["past"][1], a["gone"]), (b["past"][0], b["past"][1], b["gone"])
    if head_a != head_b:
        return head_a < head_b
    s_a = _standing(a["applied"], a["past_n"], b["applied"], requirements)
    s_b = _standing(b["applied"], b["past_n"], a["applied"], requirements)
    if MIXED_BY_SHARE and s_a[0] == s_b[0] and _each_worse(a["applied"], b["applied"]):
        m_a, m_b = _mixed_key(a["applied"], requirements), _mixed_key(b["applied"], requirements)
        if m_a != m_b:
            return m_a < m_b
    elif s_a != s_b:
        return s_a < s_b
    return (a["trias"], a["elements"]) < (b["trias"], b["elements"])


@contextlib.contextmanager
def _so_far_named(name: str):
    """SHMESH-21b: what the whole meshes made inside the block are called in the record, put back after."""
    saved = _SO_FAR.get("label")
    _SO_FAR["label"] = name
    try:
        yield
    finally:
        _SO_FAR["label"] = saved


def _so_far(mesh: ShellMesh, applied: dict, past_n: int, requirements: dict | None, name: str | None = None) -> None:
    """SHMESH-21b: one whole mesh made - counted, and written to the run's folder when it stands better than the best
    this process wrote (then that one's file goes). Never raises, never changes the mesh or its record."""
    import os
    import pickle

    d = _SO_FAR.get("dir")
    if not (BEST_SO_FAR and d):
        return
    try:
        folder = Path(d)
        pid = os.getpid()
        (folder / f"made_{time.time_ns()}_{pid}").touch()
        n = len(list(folder.glob("made_*")))
        view = _so_far_view(mesh, applied, past_n)
        mine = _SO_FAR.get("mine")
        if mine is not None and not _so_far_beats(view, mine, requirements):
            return
        label = name or str(_SO_FAR.get("label") or "a whole mesh")
        data = pickle.dumps({"name": label, "n": n, "at_s": round(time.time() - float(_SO_FAR.get("t0") or 0.0), 1),
                             "mesh": mesh, "applied": applied, "past_n": int(past_n)})
        part = folder / f".best_{n:04d}_{pid}.part"
        part.write_bytes(data)
        out = folder / f"best_{n:04d}_{pid}.pkl"
        os.replace(part, out)
        old = _SO_FAR.get("file")
        if old is not None and old[0] == pid:
            Path(old[1]).unlink(missing_ok=True)
        _SO_FAR.update(mine=view, file=(pid, str(out)))
    except Exception as exc:  # noqa: BLE001 - a mesh not kept in hand costs nothing but the hand
        logger.info("shell mesh: a whole mesh was not kept in hand (%s)", exc)


def _so_far_best(folder: Path | str, requirements: dict | None) -> tuple[int, dict | None]:
    """SHMESH-21b: (the whole meshes the run made, the best valid one written - its name, number, seconds, mesh,
    record and elements past the solver's limits; None when none was valid)."""
    import pickle

    d = Path(folder)
    made = len(list(d.glob("made_*")))
    best: tuple[dict, dict] | None = None
    for f in sorted(d.glob("best_*.pkl")):
        try:
            c = pickle.loads(f.read_bytes())  # noqa: S301 - our own forked children's meshes, in our own folder
        except Exception as exc:  # noqa: BLE001 - a file cut off as the child was stopped
            logger.info("shell mesh: a kept mesh was not read back (%s)", exc)
            continue
        v = _so_far_view(c["mesh"], c["applied"], c["past_n"])
        if v["past"][0]:
            continue
        if best is None or _so_far_beats(v, best[0], requirements):
            best = (v, c)
    return made, (best[1] if best is not None else None)


def _so_far_deliver(folder: Path | str, requirements: dict | None, allowed: float,
                    stopped: str) -> tuple[ShellMesh, dict] | None:
    """SHMESH-21b: at the stop, the best whole mesh made so far, told (quality.time_limit, a note, the run's
    time_limited); None when no valid whole mesh was made."""
    made, c = _so_far_best(folder, requirements)
    if c is None:
        return None
    mesh, applied = c["mesh"], c["applied"]
    minutes = f"{allowed / 60:.0f}" if allowed >= 60 else f"{allowed:.0f} s"
    unit = " minutes" if allowed >= 60 else ""
    applied.setdefault("quality", {})["time_limit"] = {
        "rule": "SHMESH-21b", "reached": True, "limit_s": round(float(allowed), 1), "meshes_made": int(made),
        "delivered": c["name"], "delivered_mesh": int(c["n"]), "made_at_s": c["at_s"], "stopped": stopped[:200],
        "why": f"the shell mesh reached its time limit after {made} whole mesh(es); the best made so far is delivered"}
    # SHMESH-21e: what was asked (the planned elements), how long it ran, what is delivered, and the way on
    planned = _CLOCK.get("planned")
    asked = f" for about {int(planned):,} planned elements" if planned else ""
    held = " (all the time this job had left for it)" if _CLOCK.get("capped") else ""
    applied["quality"]["time_limit"].update(planned=None if not planned else int(planned),
                                            capped=bool(_CLOCK.get("capped")))
    mesh.notes.append(f"The shell mesh reached its time limit ({minutes}{unit}{asked}){held} after {made} whole "
                      f"mesh(es) and was stopped; the best mesh made so far (mesh {c['n']} of {made}: {c['name']}, "
                      f"{int(mesh.n_elements):,} elements) is delivered. A larger element size makes a smaller mesh "
                      "that finishes sooner (SHMESH-21b).")
    logger.warning("shell mesh: stopped at its time limit after %d whole meshes; mesh %d (%s) delivered (SHMESH-21b)",
                   made, c["n"], c["name"])
    try:
        from meshpipeline.engines.determinism import note_time_limit

        note_time_limit("shell_mesh", "the shell mesh reached its time limit: the best mesh made so far is delivered",
                        limit_s=float(allowed), meshes_made=int(made), delivered_mesh=int(c["n"]))
    except Exception as exc:  # noqa: BLE001 - the record and the note still say it
        logger.debug("shell mesh: the time limit was not recorded (%s)", type(exc).__name__)
    return mesh, applied


#: SHMESH-21c: the stamped stream's optional whole meshes (SHMESH-76's meshes without the steps or the fit, SHMESH-77's
#: fit kept where it pays, SHMESH-90's steps kept where they pay; SHMESH-91's per-face keep) start only when the time
#: left (_limit: the clock less TIME_MARGIN_S) covers what the last one of their kind cost, plus PRICE_MARGIN of it;
#: one not started is recorded (strip_steps_kept.skipped) and told, and the run ends inside its limit with the best it
#: has. The rules that judge them are unchanged. False: started whatever the time left, as before
STAMPED_PRICED = True
PRICE_MARGIN = 0.1
#: SHMESH-21c: the per-face keep has no earlier one of its kind in a run: it is priced at this (it stitches meshes
#: made, no gmsh; measured on the stamped lab, see quality.strip_steps_kept.face_keep_s)
FACE_KEEP_PRICE_S = 10.0
#: (what a stamped optional mesh not started is told)
PRICED_WHY = "not started: the time left would not cover one more (SHMESH-21c)"


def _priced_affords(price: float | None) -> bool:
    """SHMESH-21c: whether an optional stamped mesh (or the per-face keep) priced at `price` seconds - the last of its
    kind - may start: no once a spot pass was cut off at the deadline; else when the time left covers it and its
    margin."""
    if not STAMPED_PRICED:
        return True
    if _RUN.get("cut"):
        return False
    return _affords(float(price or 0.0) * (1.0 + PRICE_MARGIN))


#: SHMESH-21d: CHOOSE FIRST, REPAIR LAST - the first path keeps back the time of the first mesh of each of SHMESH-76's
#: meshes its mesh sets going; its spot passes (SHMESH-31, priced at _spot_price) and its bolt-ring trial start only
#: with that kept back, and a spot pass runs bounded to the time left less it. Where they do not fit (or a spot pass is
#: stopped at that bound, or the next candidate fits only as a first mesh), the run chooses first: each candidate is
#: made as its first mesh only (priced at the last first mesh's seconds), they are ranked as they stand (SHMESH-76's
#: order: SHMESH-36's standing, then triangles, then elements), and only the one kept is made again with its spot
#: passes, with the time that is left (none when even that cannot fit: told). Where the time holds it all, nothing
#: changes: the candidates are made whole, one after another, as before. On s33 the first path's spot pass took 610 s
#: of 900 and was then dropped (it cleared nothing), and the two meshes it was to be set against were never made.
#: False: every path makes its spot passes at once, as before
CHOOSE_FIRST = True
#: (armed: the first path's first spot pass decides; first: the first path is being made; reserve: the seconds it
#: keeps back; tight: the run chooses first; defer: the spot passes of the paths made now wait; owed: SHMESH-76's
#: meshes the first path's mesh sets going)
_CHOOSE: dict[str, Any] = {"armed": False, "first": False, "reserve": 0.0, "tight": False, "defer": False,
                           "price": None, "owed": 0, "why": ""}


def _choose_owed(q: dict) -> int:
    """SHMESH-21d: how many of SHMESH-76's three meshes a first mesh (its quality record) sets going, read as
    _yield_both reads them."""
    steps = bool(q.get("strip_yield")) or bool((q.get("strip_steps") or {}).get("planned"))
    return int(steps) + int(bool(q.get("fit_bad"))) + int(steps or bool(q.get("fit_mapped")))


def _choose_reserve(applied: dict) -> float:
    """SHMESH-21d: at the first path's spot passes (once a run, the path armed by _yield_both): the seconds kept back
    for the first mesh of each of SHMESH-76's meshes the first path's mesh sets going (each at the first mesh's own
    time, with PRICE_MARGIN) - decided at its first spot pass, held for the rest of that path (its bolt-ring trial and
    the trial's spot passes); 0 for any other path."""
    if not CHOOSE_FIRST:
        return 0.0
    if not _CHOOSE.get("armed"):
        return float(_CHOOSE.get("reserve") or 0.0) if _CHOOSE.get("first") else 0.0
    _CHOOSE["armed"] = False
    owed = _choose_owed(applied.get("quality") or {})
    # (SHMESH-76p2: and the plain mesh's, owed whenever this first mesh is past the solver's limits)
    reserve = (owed + int(bool(_CHOOSE.get("plain")))) * float(_RUN.get("first_s") or 0.0) * (1.0 + PRICE_MARGIN)
    _CHOOSE.update(owed=owed, reserve=reserve)
    return reserve


def _choose_trial_waits(past: list) -> bool:
    """SHMESH-21d: whether the first path's bolt-ring trial (SHMESH-51o: a first mesh again, and its spot pass where
    elements are past the solver's limits) waits: it does not fit in the time left with the first meshes owed after
    the path kept back."""
    reserve = float(_CHOOSE.get("reserve") or 0.0) if (CHOOSE_FIRST and _CHOOSE.get("first")) else 0.0
    if not reserve:
        return False
    return not _affords(float(_RUN.get("first_s") or 0.0) + (_spot_price() if past else 0.0) + reserve)


def _choose_start(why: str) -> None:
    """SHMESH-21d: from now on the run chooses first - the paths made now wait with their spot passes."""
    _CHOOSE.update(tight=True, defer=True, why=why)


#: SHMESH-31i: a path whose spot pass made nothing that was kept (it left as many past the solver's limits or more)
#: runs no spot pass again in its bolt-ring trial: on s33 the trial's second spot pass took 445 s more and was dropped
#: too. False: the trial makes its spot passes again, as before
SPOT_NOT_AGAIN = True
#: SHMESH-76p: THE PLAIN MESH JOINS THE POOL - on a stamped part (the stamped steps on) where every candidate SHMESH-76
#: made stands past the solver's limits (or not valid), the mesh with every STAMPED_SWITCHES member off (what the
#: mesher makes of a part it does not treat as stamped) is made as a first mesh, ranked with them in SHMESH-76's order,
#: and repaired last if it is kept (SHMESH-21d). A part with any candidate inside the solver's limits never makes it.
#: s33: every stamped candidate 115 past the solver's limits as a first mesh; the plain mesh 45 / 48 by kind. False:
#: the pool is the stamped candidates only, as before
PLAIN_JOINS = True
PLAIN_NAME = "the plain mesh (every stamped step off)"


def _plain_applies(record: dict) -> bool:
    """SHMESH-76p: whether a plain mesh can differ from this part's stamped one - the stamped steps are on for it (a
    surface file, or STAMPED_ALL) and some STAMPED_SWITCHES member is on."""
    g_ = globals()
    return bool(PLAIN_JOINS and (STAMPED_ALL or (record or {}).get("source") == "surface") and not _STAMPED_OFF_NOW[0]
                and any(g_[n_] for n_ in STAMPED_SWITCHES))
#: SHMESH-31h: the longest spot pass this shell mesh measured, seconds (None: none yet); reset with the run's clock
_SPOT_COST: dict[str, float | None] = {"s": None}


def _spot_price() -> float:
    """SHMESH-31h: what a spot pass is priced at - the first mesh's own time (SHMESH-31b), or the longest spot pass
    this shell mesh has measured when that is longer: a spot pass re-meshes the whole part with finer boxes, and its
    clean-up can cost many times the first mesh's (s33: 610 s against a first mesh of 91 s; most parts 0.9 to 2.4
    times). Its work cannot be read before it runs (s33 had 10 boxes; fr014, 64 boxes and twice the elements, took 1.1
    times its first mesh), so the first spot pass of a run is priced at the first mesh and held to its bound."""
    return max(float(_RUN.get("first_s") or 0.0), float(_SPOT_COST.get("s") or 0.0))


def _short_mend(xyz, Q, T, q_face, t_face, node_kind, node_edge, vertex_edges, lim, low: float,
                high: float | None, forced_curves: set[int], movable, project, hard_long, price: float,
                requirements: dict | None, tri_ok: bool = True):
    """SHMESH-28e: the short-edge mend (SHORT_MEND), cycle by cycle while the run's clock affords one (each priced at
    the last cycle's time, the first at the mend's before it). Returns (xyz, quads, triangles, their faces, what was
    done: merged / recut / moved, cycles, seconds, and why it stopped when the clock stopped it)."""
    t0 = time.monotonic()
    m_ = shell_quality.quad_measures(xyz, Q) if len(Q) else None
    t_ = shell_quality.tria_measures(xyz, T) if len(T) else None
    past_n = (int(shell_quality._quad_past(m_).sum()) if m_ is not None else 0) + (
        int(shell_quality._tria_past(t_).sum()) if t_ is not None else 0)
    start = (xyz, Q, T, list(q_face), list(t_face))
    stopped: list[str] = []

    def changed(X0, Q0, T0, X1, Q1, T1) -> np.ndarray:
        """The nodes a round changed (moved, or on an element that came or went) and the nodes one ring round them."""
        ch = np.zeros(len(X0), dtype=bool)
        ch[np.flatnonzero(np.any(X1[: len(X0)] != X0, axis=1))] = True
        for a_, b_ in ((Q0, Q1), (T0, T1)):
            sa = {tuple(c) for c in a_.tolist()}
            sb = {tuple(c) for c in b_.tolist()}
            for c in sa ^ sb:
                ch[list(c)] = True
        ring = np.zeros(len(X0), dtype=bool)
        for els in (Q1, T1):
            if len(els):
                ring[els[ch[els].any(axis=1)].ravel()] = True
        return ch | ring

    def run(gate, price_):
        X, Q_, T_, qf, tf = start
        Q_ = np.asarray(Q_, dtype=np.int64).reshape(-1, 4)
        T_ = np.asarray(T_, dtype=np.int64).reshape(-1, 3)
        got_all = {"merged": 0, "recut": 0, "moved": 0}
        rounds = 0
        focus = None
        # (one round at a time; after the first, only round what the last round changed: elsewhere nothing changed,
        # and a change judged before is judged the same)
        for _ in range(MEND_CYCLES * shell_quality.MEND_ROUNDS):
            if not _affords_step(price_, past_n, requirements):
                stopped.append(TRIAL_KEPT_WHY)
                break
            t1 = time.monotonic()
            X1, Q1, T1, qf, tf, got = shell_quality.mend_short_local(
                X, Q_, T_, qf, tf, node_kind, node_edge, vertex_edges, gate=gate, focus=focus, rounds=1, limits=lim,
                quads_too=True, triangles_too=True, edges=(low, high), movable=movable, project=project,
                hard_long=hard_long)
            price_ = time.monotonic() - t1
            rounds += 1
            if not (got["merged"] or got["recut"] or got["moved"]):
                X, Q_, T_ = X1, Q1, T1
                break
            got_all = {k: got_all[k] + got[k] for k in got_all}
            focus = changed(X, Q_, T_, X1, Q1, T1)
            X, Q_, T_ = X1, Q1, T1
        return X, Q_, T_, qf, tf, got_all, rounds, price_

    forced = frozenset(int(c) for c in forced_curves)
    if SHORT_MEND_TRI == "met" and tri_ok:
        # (a face may take triangles only where it ends with none of ours under the shortest edge)
        n_f = 1 + max(list(q_face) + list(t_face) + [-1])
        allowed = set(range(n_f))
        tri0 = np.bincount(np.asarray(t_face, dtype=np.int64), minlength=n_f) if len(t_face) else np.zeros(n_f, int)
        out = None
        for _try in range(SHORT_MEND_TRI_TRIES):
            gate = shell_quality.ShortGate(low=low, forced_curves=forced, tri_faces=frozenset(allowed))
            out = run(gate, price)
            X, Q_, T_, qf, tf = out[:5]
            tri1 = np.bincount(np.asarray(tf, dtype=np.int64), minlength=n_f) if len(tf) else np.zeros(n_f, int)
            ours = shell_quality.short_ours(X, Q_, T_, node_kind, node_edge, vertex_edges, gate)
            f_all = np.asarray(list(qf) + list(tf), dtype=np.int64)
            ours_f = np.bincount(f_all[ours], minlength=n_f) if len(f_all) else np.zeros(n_f, int)
            held = {f for f in allowed if tri1[f] > tri0[f] and ours_f[f] > 0}
            if not held:
                break
            allowed -= held
            out = None
        if out is None:
            out = run(shell_quality.ShortGate(low=low, forced_curves=forced, tri_faces=frozenset()), price)
    else:
        out = run(shell_quality.ShortGate(low=low, forced_curves=forced, tri_faces=frozenset()), price)
    X, Q_, T_, qf, tf, got_all, rounds, _p = out
    done = {**got_all, "rounds": rounds, "seconds": round(time.monotonic() - t0, 3)}
    if stopped:
        done["stopped"] = stopped[0]
    return X, Q_, T_, qf, tf, done


def _bounded(fn, seconds: float | None):
    """SHMESH-31b: fn() in a forked child stopped after `seconds` (ValueError "stopped"); in this process with no
    limit or no fork. A plain fork: the shell mesh itself already runs in a daemonic child (SHMESH-21), which
    multiprocessing does not let have children. In the child the run's end is the bound's own (_RUN), so a pass
    inside it is held to that."""
    import os
    import pickle
    import select
    import signal

    if seconds is None or not hasattr(os, "fork"):
        return fn()
    if seconds <= 0:
        raise ValueError("stopped: no time left for it (SHMESH-31b)")
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:                                        # the child: run, send, leave
        os.close(r_fd)
        _RUN["end"] = time.monotonic() + seconds
        try:
            try:
                data = pickle.dumps(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001 - told to the parent, which raises it there
                data = pickle.dumps(("error", f"{type(exc).__name__}: {exc}"))
            with os.fdopen(w_fd, "wb") as fh:
                fh.write(data)
        finally:
            os._exit(0)
    os.close(w_fd)
    chunks: list[bytes] = []
    try:
        deadline = time.monotonic() + seconds
        with os.fdopen(r_fd, "rb", buffering=0) as fh:
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fh], [], [], left)[0]:
                    raise ValueError(f"stopped after {seconds:.0f} s, the time left before the deadline (SHMESH-31b)")
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
    if not chunks:
        raise ValueError("the spot pass ended without a mesh (SHMESH-31b)")
    status, out = pickle.loads(b"".join(chunks))  # noqa: S301 - our own forked child's answer, through our own pipe
    if status != "ok":
        raise ValueError(out)
    return out


def _mesh_shell(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
                target: dict | None = None, correction: float = 1.0,
                requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """mesh_shell, unbounded, in this process. SHMESH-36: a mesh under the user's limits left with an element past
    the solver's limits or past one of the user's outer limits is set against the mesh #435's steps alone make
    (_classic: the whole path again, from the sewn surface, exactly as #435 meshed it), and the better is kept -
    fewer past the solver's limits, then fewer lines on which it is past an outer limit where the other is not (or
    further past), then fewer past an outer limit, then more lines met. Both candidates and the choice are written
    into the record ("kept").

    SHMESH-31b: the run's clock starts here (and stops when it returns); #435's mesh is made only while it fits in
    the time left (priced at the first mesh's own time, and bounded), and never once a spot pass was cut off - the
    mesh made stands, told."""
    end = time.monotonic() + _deadline_s() if MESH_DEADLINE_S > 0 else None
    if end is not None and _CLOCK.get("t0") is not None and _CLOCK.get("allowed") is not None:
        # (SHMESH-21e: a forked run of mesh_shell keeps the parent's one clock - its start, the limit in force)
        end = float(_CLOCK["t0"]) + float(_CLOCK["allowed"])
    _RUN.update(end=end, first_s=None, cut=False)
    _SPOT_COST["s"] = None
    try:
        return _mesh_shell_timed(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                 correction=correction, requirements=requirements)
    finally:
        _RUN.update(end=None, first_s=None, cut=False)
        _SPOT_COST["s"] = None


def _mesh_shell_timed(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
                      target: dict | None = None, correction: float = 1.0,
                      requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """_mesh_shell under the run's clock (_RUN)."""
    if FORCE_CLASSIC:
        with _classic():
            mesh, applied, _n = _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                           correction=correction, requirements=requirements)
        return mesh, applied
    mesh, applied, past_n = _yield_both(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                        correction=correction, requirements=requirements)
    standing = _standing(applied, past_n)
    if not (requirements and CLASSIC_CHECK and (standing[0] or standing[2])):
        _record_short_edge(brep, mm_per_unit, mesh, applied)
        return mesh, applied
    this = _candidate("this PR's steps (SHMESH-27..35)", mesh, applied, past_n)
    if _RUN.get("cut") or not _affords(_RUN.get("first_s")):
        # SHMESH-31b: no trial that cannot finish before the deadline (or after a spot pass was cut off)
        applied["kept"] = {"rule": "SHMESH-36", "candidates": [this], "kept": this["mesh"],
                           "why": "#435's steps were not tried: no time left for a whole mesh more (SHMESH-31b)"}
        return mesh, applied

    def classic_path():
        with _classic(), _so_far_named("#435's steps (SHMESH-36)"):
            return _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=None, target=target, correction=correction,
                              requirements=requirements)

    try:
        old, applied_old, old_n = _bounded(classic_path, _limit())
    except (ValueError, _Periodic) as exc:
        logger.info("shell mesh: #435's steps did not make a mesh to compare (%s)", exc)
        applied["kept"] = {"rule": "SHMESH-36", "candidates": [this], "kept": this["mesh"],
                           "why": f"#435's steps made no mesh ({type(exc).__name__}: {str(exc)[:200]})"}
        return mesh, applied
    that = _candidate("#435's steps", old, applied_old, old_n)
    s_new = _standing(applied, past_n, applied_old, requirements)
    s_old = _standing(applied_old, old_n, applied, requirements)
    keep_old = s_old < s_new
    why = next((name for name, a, b in zip(STANDING_WORDS, s_new, s_old, strict=True) if a != b), "a tie: this PR's")
    if MIXED_BY_SHARE and s_new[0] == s_old[0] and _each_worse(applied, applied_old):
        # SHMESH-36b: each mesh worse on some line - the fewer elements past the outer limits in total, then fewer
        # lines past, then the smaller worst overshoot as a share of its band
        m_new, m_old = _mixed_key(applied, requirements), _mixed_key(applied_old, requirements)
        keep_old = m_old < m_new
        why = "each mesh worse on some line: " + next(
            (name for name, a, b in zip(MIXED_WORDS, m_new, m_old, strict=True) if a != b), "a tie: this PR's")
    kept = {"rule": "SHMESH-36", "candidates": [this, that], "kept": (that if keep_old else this)["mesh"],
            "why": why, "standing": {this["mesh"]: list(s_new), that["mesh"]: list(s_old)},
            "by_line": _by_line(this, that)}
    if keep_old:
        mesh, applied = old, applied_old
        applied["quality"]["classic"] = True
    applied["kept"] = kept
    _record_short_edge(brep, mm_per_unit, mesh, applied)
    return mesh, applied


#: SHMESH-48l2: a pass whose only outcome is a redraw stops once its rings are built (False: made whole, as before)
EARLY_REDRAW = True

#: SHMESH-45c: at most this many holes are listed one by one in the record (quality.structure.hole_list)
HOLE_LINES = 500
#: SHMESH-45 (washer guard, whole step): a structured mesh that fails, or comes out invalid or past Nastran's own limits,
#: is set against the mesh with the structured steps off ("free mesh": exactly the mesh before them), and the free mesh
#: is kept when it is valid where the other is not, or has fewer elements past Nastran's limits
STRUCTURED_GUARD = True


def _structured_on(requirements: dict | None) -> bool:
    return bool((ST.WASHERS or ST.MAPPED) and SR.structure_of(requirements) != "free")


@contextlib.contextmanager
def _structured_off():
    """The structured steps (washers, mapped and gridded faces, the flat-face algorithm) off for one mesh: the mesh is
    then the one "free mesh" in the notes gives, byte for byte the mesh before them."""
    g = globals()
    saved = (ST.WASHERS, ST.MAPPED, ST.BLOCKS, g["PLANE_ALGORITHM"])
    try:
        ST.WASHERS = ST.MAPPED = ST.BLOCKS = False
        g["PLANE_ALGORITHM"] = None
        yield
    finally:
        ST.WASHERS, ST.MAPPED, ST.BLOCKS, g["PLANE_ALGORITHM"] = saved


#: SHMESH-60..69: the stamped-sheet steps' switches, all put off by _stamped_off
STAMPED_SWITCHES = ("NARROW_COLLAPSE", "FLANK_LOW", "SHORT_SUPPRESS", "STRIP_LOW", "REPAIR_AFTER_MEND",
                    "ONE_ROW_ALIKE", "YIELD_COUNTS", "FIT_MAP", "CORNER_MERGE", "TRI_BLOCKS", "CLOSING_SIZE", "RELAX_SHORT", "SHORT_FIRST", "TAPERS", "UNEQUAL_FEASIBLE",
                    "CLOSE_GUARD", "CLOSE_CUT_MENDS", "LONG_GUARD", "TRANSITIONS")
_STAMPED_OFF_NOW = [False]


@contextlib.contextmanager
def _stamped_off():
    """SHMESH-60..69 off for one mesh: the rules' own mesh, byte for byte the mesh before them."""
    g = globals()
    saved = {n: g[n] for n in STAMPED_SWITCHES}
    try:
        g.update(dict.fromkeys(STAMPED_SWITCHES, False))
        _STAMPED_OFF_NOW[0] = True
        yield
    finally:
        g.update(saved)
        _STAMPED_OFF_NOW[0] = False


@contextlib.contextmanager
def _rings_only():
    """SHMESH-45g: the guard's middle step - round washers' rings in free faces only (no gridded faces, no square
    rings, no mapped faces): what a part keeps when the fuller steps fail on it, before it falls back to the free
    mesh."""
    saved = (ST.MAPPED, ST.BLOCKS, ST.SQUARES)
    try:
        ST.MAPPED = ST.BLOCKS = ST.SQUARES = False
        yield
    finally:
        ST.MAPPED, ST.BLOCKS, ST.SQUARES = saved


def _past(applied: dict) -> tuple[int, int]:
    """(invalid, elements past Nastran's own limits) of a mesh, from its record - lower is better."""
    q = applied.get("quality") or {}
    return int(not q.get("valid", True)), int(sum(int(v) for v in (q.get("nastran_past") or {}).values()))


def _yield_both(brep, record, **kw):
    """SHMESH-75: _guarded_path; when a strip's count yielded to its neighbour, the mesh without yielding is made too
    and the better kept (_standing against each other, then fewer triangles, then fewer elements)."""
    # (every argument passed on whole: a parameter another step adds to _guarded_path reaches it)
    requirements = kw.get("requirements")
    t_c = time.monotonic()
    # (SHMESH-21d: the first path's spot passes decide whether this run chooses first and repairs last)
    choose = bool(CHOOSE_FIRST and NO_WORSE and not _YIELD_OFF[0] and requirements and _RUN.get("end") is not None)
    _CHOOSE.update(armed=choose, first=choose, reserve=0.0, tight=False, defer=False, price=None, owed=0, why="",
                   plain=bool(choose and _plain_applies(record)))
    try:
        with _so_far_named("all on (the first path)"):
            mesh, applied, past_n = _narrow_kept_path(brep, record, **kw)
    finally:
        _CHOOSE.update(armed=False, first=False, reserve=0.0)
    tight = bool(_CHOOSE.get("tight"))
    if _YIELD_OFF[0] or not NO_WORSE:
        _CHOOSE.update(tight=False, defer=False)
        return mesh, applied, past_n
    try:
        return _yield_rest(brep, record, kw, mesh, applied, past_n, time.monotonic() - t_c, tight)
    finally:
        _CHOOSE.update(tight=False, defer=False)


def _yield_remake(brep, record, kw: dict, recipe: tuple, name: str):
    """SHMESH-21d: one of _yield_both's meshes made again from its recipe (the switches off, _YIELD_OFF, the fit
    faces and strips set aside), exactly as it was set up the first time (SHMESH-76p: the plain mesh under
    _stamped_off)."""
    off, yield_off, fit_ex, step_ex, plain = recipe
    g_ = globals()
    saved = {n_: g_[n_] for n_ in off}
    if yield_off:
        _YIELD_OFF[0] = True
    if fit_ex is not None:
        _FIT_EXCLUDE[:] = list(fit_ex)
    if step_ex is not None:
        _STEP_EXCLUDE[:] = list(step_ex)
    try:
        g_.update(off)
        with _so_far_named(name), (_stamped_off() if plain else contextlib.nullcontext()):
            return _narrow_kept_path(brep, record, **{**kw, "log": None})
    finally:
        if yield_off:
            _YIELD_OFF[0] = False
        g_.update(saved)
        if fit_ex is not None:
            _FIT_EXCLUDE.clear()
        if step_ex is not None:
            _STEP_EXCLUDE.clear()


def _yield_rest(brep, record, kw: dict, mesh, applied, past_n, last_s: float, tight: bool):
    """_yield_both after its first path: SHMESH-76/77/90's meshes and SHMESH-91's per-face keep; SHMESH-21d: with
    `tight`, the meshes are first meshes only and the one kept is repaired last (_yield_remake)."""
    requirements = kw.get("requirements")
    # (SHMESH-21d: how each mesh is made again, and its first mesh's seconds; the last first mesh's seconds)
    recipes: dict[str, tuple] = {"all on": ({}, False, None, None, False)}
    last_first = float(_RUN.get("first_s") or last_s)
    first_secs: dict[str, float] = {"all on": last_first}
    can_choose = bool(CHOOSE_FIRST and requirements and _RUN.get("end") is not None)
    other: ShellMesh | None
    # (SHMESH-21c: each optional mesh below priced at the last whole mesh's seconds; those not started)
    skipped: list[dict] = []
    plain_rec: dict | None = None
    plain_got: tuple | None = None
    if _plain_applies(record) and (_past(applied)[0] or past_n):
        # SHMESH-76p2: the first stamped mesh is past the solver's limits - the plain mesh is the next thing made, before
        # any optional mesh (its time was kept back by the first path, SHMESH-21d), as a first mesh; ranked below with
        # the others in SHMESH-76's order
        plain_rec = {"rule": "SHMESH-76p", "why": "the first stamped mesh is past the solver's limits",
                     "price_s": round(last_first, 1)}
        if not _priced_affords(last_first):
            plain_rec["made"] = "not started: " + PRICED_WHY
        else:
            defer_was = _CHOOSE.get("defer")
            _CHOOSE["defer"] = bool(can_choose) or bool(defer_was)
            t_c = time.monotonic()
            try:
                plain_got = _yield_remake(brep, record, kw, ({}, False, None, None, True), PLAIN_NAME)
            except (ValueError, _Periodic) as exc:
                logger.info("shell mesh: no plain mesh to compare (%s)", exc)
                plain_rec["made"] = f"failed ({type(exc).__name__})"
            finally:
                _CHOOSE["defer"] = defer_was
            plain_s = time.monotonic() - t_c
            if plain_got is not None:
                plain_rec["made"] = round(plain_s, 1)
                first_secs[PLAIN_NAME] = float(plain_s)
                recipes[PLAIN_NAME] = ({}, False, None, None, True)
                last_first = float(plain_s) if tight else last_first

    def may_start(name_: str) -> bool:
        """SHMESH-21c/21d: whether the next mesh starts - whole, when the time affords the last whole mesh's seconds
        (and the run is not choosing first); else as a first mesh, when it affords the last first mesh's (the run
        chooses first from here on); else not (recorded)."""
        nonlocal tight
        if not tight and _priced_affords(last_s):
            return True
        if can_choose and _priced_affords(last_first):
            if not tight:
                tight = True
                _choose_start(f"{name_}: a whole mesh ({last_s:.0f} s) would not fit in the time left, its first mesh "
                              f"({last_first:.0f} s) does")
            return True
        skipped.append({"mesh": name_, "price_s": round(last_first if tight else last_s, 1), "why": PRICED_WHY})
        return False

    face_keep_s: float | None = None
    best = ("all on", mesh, applied, past_n)
    pool = [best]                 # (SHMESH-91: every candidate made, for the per-face keep)
    tried = []
    seen = [applied["quality"]]
    g_ = globals()
    # SHMESH-76: the newer steps, dropped one group at a time while the mesh before acted; a candidate replaces the
    # one kept only when it stands better against it (a line past its outer limit where the other has none loses),
    # then on fewer triangles
    for name, off in (("without the steps", {"TRANSITIONS": False}),
                      ("without the steps, the fit-mapping off on its faces past a line", {"TRANSITIONS": False}),
                      ("without the steps and the fit-mapping", {"TRANSITIONS": False, "FIT_MAP": False})):
        q_ = best[2]["quality"]
        per_face = "on its faces" in name
        if per_face:
            # (SHMESH-77 judged per face: the fit faces whose own elements were past a line, free; the rest kept)
            acted = bool(q_.get("fit_bad"))
        else:
            acted = (bool(q_.get("strip_yield")) or bool((q_.get("strip_steps") or {}).get("planned"))
                     or (bool(q_.get("fit_mapped")) and "FIT_MAP" in off))
        if not acted:
            continue
        if not may_start(name):
            continue
        saved = {n_: g_[n_] for n_ in off}
        _YIELD_OFF[0] = True
        _fit_ex = [(np.asarray(c_, dtype=float), float(a_)) for c_, a_ in q_["fit_bad"]] if per_face else []
        if per_face:
            _FIT_EXCLUDE[:] = list(_fit_ex)
        t_c = time.monotonic()
        try:
            g_.update(off)
            with _so_far_named(name):
                other, applied_o, other_n = _narrow_kept_path(brep, record, **{**kw, "log": None})
        except (ValueError, _Periodic) as exc:
            logger.info("shell mesh: no mesh %s to compare (%s)", name, exc)
            continue
        finally:
            _YIELD_OFF[0] = False
            g_.update(saved)
            _FIT_EXCLUDE.clear()
            last_s = time.monotonic() - t_c
            if tight:
                last_first = last_s
        _n, m_b, a_b, p_b = best
        mine = (*_standing(a_b, p_b, applied_o, requirements), len(m_b.trias), int(m_b.n_elements))
        theirs = (*_standing(applied_o, other_n, a_b, requirements), len(other.trias), int(other.n_elements))
        def did(q__):
            return {"fit_mapped": int(q__.get("fit_mapped") or 0), "fit_bad": len(q__.get("fit_bad") or []),
                    "corner_mapped": int(q__.get("corner_mapped") or 0),
                    "steps_planned": int((q__.get("strip_steps") or {}).get("planned") or 0),
                    **({"steps": q__["strip_steps"]} if q__.get("strip_steps") else {})}

        pool.append((name, other, applied_o, other_n))
        recipes[name] = (dict(off), True, list(_fit_ex) if per_face else None, None, False)
        first_secs[name] = float(last_s)
        tried.append({"mesh": name, "against": best[0], "kept": theirs < mine, "this": list(theirs),
                      "that": list(mine), "this_did": did(applied_o["quality"]), "that_did": did(a_b["quality"]),
                      "seconds": round(last_s, 1)})
        seen.append(applied_o["quality"])
        if theirs < mine:
            applied_o["quality"]["strip_log"] = applied_o["quality"].get("strip_log") or q_.get("strip_log")
            best = (name, other, applied_o, other_n)
    seen_q = [q_c for q_c in seen if q_c is not None]
    with_fit = next((q_c for q_c in reversed(seen_q) if q_c.get("fit_faces")), None)
    no_fit = next((q_c for q_c in reversed(seen_q) if not q_c.get("fit_mapped") and "face_trias" in q_c), None)
    per_face_rec: dict = {}
    if FIT_PER_FACE and (with_fit is None or no_fit is None):
        per_face_rec = {"split": "not tried: " + ("no mesh with fit-mapped faces" if with_fit is None
                                                    else "no mesh without the fit to set against")}
    if FIT_PER_FACE and with_fit is not None and no_fit is not None:
        # SHMESH-77 per face, in triangles: a fit face is freed when it and the faces sharing a curve with it carry more
        # triangles with the fit than without it; the mesh with the fit kept on the others is made and compared
        ff = with_fit["fit_faces"]
        tw, tn = with_fit.get("face_trias") or {}, no_fit.get("face_trias") or {}
        worse = [k_ for k_, v_ in ff.items()
                 if sum(int(tw.get(str(n_), 0)) for n_ in v_["near"]) > sum(int(tn.get(str(n_), 0)) for n_ in v_["near"])]
        per_face_rec = {"faces": [{"face": int(k_), "near": len(v_["near"]),
                                    "trias_with_fit": sum(int(tw.get(str(n_), 0)) for n_ in v_["near"]),
                                    "trias_without": sum(int(tn.get(str(n_), 0)) for n_ in v_["near"]),
                                    "verdict": "freed" if k_ in worse else "kept"} for k_, v_ in ff.items()]}
        per_face_rec["split"] = ("tried" if worse and len(worse) < len(ff) else
                                  "not tried: every fit face costs triangles (the mesh without the fit is that split)"
                                  if worse else "not tried: no fit face costs triangles")
        name = f"the fit kept where it pays ({len(ff) - len(worse)} of {len(ff)} faces)"
        go = bool(worse and len(worse) < len(ff)) and may_start(name)
        if worse and len(worse) < len(ff) and not go:
            # (SHMESH-21c: not started - the time left would not cover it)
            per_face_rec["split"] = "not tried: " + PRICED_WHY
        elif go:
            _FIT_EXCLUDE[:] = [(np.asarray(ff[k_]["key"][0], dtype=float), float(ff[k_]["key"][1])) for k_ in worse]
            saved = {"TRANSITIONS": g_["TRANSITIONS"]}
            _YIELD_OFF[0] = True
            t_c = time.monotonic()
            try:
                g_["TRANSITIONS"] = False
                with _so_far_named(name):
                    other, applied_o, other_n = _narrow_kept_path(brep, record, **{**kw, "log": None})
            except (ValueError, _Periodic) as exc:
                logger.info("shell mesh: no mesh with the fit kept where it pays (%s)", exc)
                other = None
            finally:
                _YIELD_OFF[0] = False
                g_.update(saved)
                _FIT_EXCLUDE.clear()
                last_s = time.monotonic() - t_c
                if tight:
                    last_first = last_s
            if other is not None:
                _n, m_b, a_b, p_b = best
                mine = (*_standing(a_b, p_b, applied_o, requirements), len(m_b.trias), int(m_b.n_elements))
                theirs = (*_standing(applied_o, other_n, a_b, requirements), len(other.trias), int(other.n_elements))
                pool.append((name, other, applied_o, other_n))
                recipes[name] = ({"TRANSITIONS": False}, True,
                                 [(np.asarray(ff[k_]["key"][0], dtype=float), float(ff[k_]["key"][1])) for k_ in worse],
                                 None, False)
                first_secs[name] = float(last_s)
                tried.append({"mesh": name, "against": best[0], "kept": theirs < mine, "this": list(theirs),
                              "that": list(mine), "seconds": round(last_s, 1)})
                if theirs < mine:
                    best = (name, other, applied_o, other_n)
    with_st = next((q_c for q_c in seen_q if q_c.get("step_faces")), None)
    no_st = next((q_c for q_c in seen_q if not (q_c.get("strip_steps") or {}).get("made")
                  and "face_trias" in q_c), None)
    if STEP_PER_STRIP and with_st is not None and no_st is not None:
        # SHMESH-90 per strip, in triangles (as SHMESH-77's fit per face): a stepped strip is left unstepped when it
        # and the faces sharing a curve with it carry more triangles with the steps than without; the mesh with the
        # other steps kept is made and compared - the clean-up after the steps (the last mend, on the user's lines
        # alone) cut some stepped strips' quads to triangles and not others
        sf = with_st["step_faces"]
        tw, tn = with_st.get("face_trias") or {}, no_st.get("face_trias") or {}

        def near_tri(t_: dict, v_: dict) -> int:
            return sum(int(t_.get(str(n_), 0)) for n_ in v_["near"])
        worse = [k_ for k_, v_ in sf.items() if near_tri(tw, v_) > near_tri(tn, v_)]
        name = f"the steps kept where they pay ({len(sf) - len(worse)} of {len(sf)} strips)"
        if worse and len(worse) < len(sf) and may_start(name):
            _STEP_EXCLUDE[:] = [(np.asarray(sf[k_]["key"][0], dtype=float), float(sf[k_]["key"][1])) for k_ in worse]
            t_c = time.monotonic()
            try:
                with _so_far_named(name):
                    other, applied_o, other_n = _narrow_kept_path(brep, record, **{**kw, "log": None})
            except (ValueError, _Periodic) as exc:
                logger.info("shell mesh: no mesh with the steps kept where they pay (%s)", exc)
                other = None
            finally:
                _STEP_EXCLUDE.clear()
                last_s = time.monotonic() - t_c
                if tight:
                    last_first = last_s
            if other is not None:
                _n, m_b, a_b, p_b = best
                mine = (*_standing(a_b, p_b, applied_o, requirements), len(m_b.trias), int(m_b.n_elements))
                theirs = (*_standing(applied_o, other_n, a_b, requirements), len(other.trias), int(other.n_elements))
                pool.append((name, other, applied_o, other_n))
                recipes[name] = ({}, False, None,
                                 [(np.asarray(sf[k_]["key"][0], dtype=float), float(sf[k_]["key"][1])) for k_ in worse],
                                 False)
                first_secs[name] = float(last_s)
                tried.append({"mesh": name, "against": best[0], "kept": theirs < mine, "this": list(theirs),
                              "that": list(mine), "seconds": round(last_s, 1)})
                if theirs < mine:
                    best = (name, other, applied_o, other_n)
    if plain_got is not None and plain_rec is not None:
        # SHMESH-76p: the plain mesh made first is ranked here, after the stamped candidates (their own order kept)
        other, applied_o, other_n = plain_got
        _n, m_b, a_b, p_b = best
        mine = (*_standing(a_b, p_b, applied_o, requirements), len(m_b.trias), int(m_b.n_elements))
        theirs = (*_standing(applied_o, other_n, a_b, requirements), len(other.trias), int(other.n_elements))
        pool.append((PLAIN_NAME, other, applied_o, other_n))
        plain_rec.update(kept=theirs < mine, this=list(theirs), that=list(mine), against=best[0])
        tried.append({"mesh": PLAIN_NAME, "against": best[0], "kept": theirs < mine, "this": list(theirs),
                      "that": list(mine), "seconds": plain_rec.get("made")})
        if theirs < mine:
            best = (PLAIN_NAME, other, applied_o, other_n)
    chose: dict | None = None
    if tight or (plain_rec is not None and best[0] == PLAIN_NAME):
        # SHMESH-21d: the candidates stood as first meshes; the one kept is made again with its spot passes, with the
        # time that is left (its first mesh again, then the spot pass at its price)
        _CHOOSE["defer"] = False
        name_b = best[0]
        price_b = (first_secs.get(name_b, last_first) + _spot_price()) * (1.0 + PRICE_MARGIN)
        chose = {"rule": "SHMESH-21d", "why": str(_CHOOSE.get("why") or ""), "kept": name_b,
                 "first_meshes": {k_: round(v_, 1) for k_, v_ in first_secs.items()}, "repair_price_s": round(price_b, 1)}
        if name_b not in recipes or not best[3] or not best[2]["quality"].get("own_size_deferred"):
            chose["repaired"] = "no repair: " + ("nothing past the solver's limits" if not best[3] else
                                                 "its spot passes were made" if name_b in recipes else "no recipe")
        elif _RUN.get("cut") or not _affords(price_b):
            chose["repaired"] = "not started: the time left would not cover it"
        else:
            t_c = time.monotonic()
            try:
                other, applied_o, other_n = _yield_remake(brep, record, kw, recipes[name_b], name_b + ", repaired")
            except (ValueError, _Periodic) as exc:
                logger.info("shell mesh: the mesh kept was not repaired (%s)", exc)
                chose["repaired"] = f"failed ({type(exc).__name__})"
            else:
                _n, m_b, a_b, p_b = best
                mine = (*_standing(a_b, p_b, applied_o, requirements), len(m_b.trias), int(m_b.n_elements))
                theirs = (*_standing(applied_o, other_n, a_b, requirements), len(other.trias), int(other.n_elements))
                chose.update(repaired="kept" if not mine < theirs else "worse: the first mesh stands",
                             repair_s=round(time.monotonic() - t_c, 1), this=list(theirs), that=list(mine))
                if not mine < theirs:
                    pool = [c_ for c_ in pool if c_[0] != name_b] + [(name_b, other, applied_o, other_n)]
                    best = (name_b, other, applied_o, other_n)
        if chose["repaired"] != "kept" and best[3]:
            best[1].notes.append("The spots past the solver's own limits were not meshed finer: the time left went to "
                                 "choosing the better mesh first (SHMESH-21d).")
    if plain_rec is not None:
        best[2]["quality"]["plain_joined"] = plain_rec
    if FACE_KEEP and len(pool) > 1 and not _priced_affords(FACE_KEEP_PRICE_S):
        # (SHMESH-21c: the per-face keep not started - the time left would not cover it)
        skipped.append({"mesh": "the per-face keep (SHMESH-91)", "price_s": FACE_KEEP_PRICE_S, "why": PRICED_WHY})
    elif FACE_KEEP and len(pool) > 1:
        # SHMESH-91: the delivered mesh takes, face by face, the best of each face across the candidates whose nodes
        # on that face's boundary are the same (kept only when the whole stands no worse)
        t_c = time.monotonic()
        got_k = _face_keep(best, pool, requirements)
        face_keep_s = round(time.monotonic() - t_c, 1)
        if got_k is not None:
            best = got_k
            _so_far(best[1], best[2], best[3], requirements, name=best[0])
    _n, mesh, applied, past_n = best
    if chose is not None:
        applied["quality"]["chose_first"] = chose
    if plain_rec is not None and str(best[0]).startswith(PLAIN_NAME):
        mesh.notes.append("The stamped-sheet steps were set aside on this part: every mesh made with them had elements "
                          "past the solver's own limits, the mesh without them fewer (SHMESH-76p).")
    if skipped:
        mesh.notes.append(f"{len(skipped)} optional stamped-sheet mesh(es) were not started: the time left before the "
                          "shell mesh's limit would not cover one more; the best mesh made is delivered (SHMESH-21c).")
    if tried or skipped:
        applied["quality"]["strip_steps_kept"] = {"kept": best[0], "tried": tried,
                                                  **({"skipped": skipped} if skipped else {}),
                                                  **({"face_keep_s": face_keep_s} if face_keep_s is not None else {})}
    if per_face_rec:
        applied["quality"]["fit_per_face"] = per_face_rec
    return mesh, applied, past_n


def _face_mesh_score(mesh: ShellMesh, lim) -> np.ndarray:
    """SHMESH-91: per element, (not valid, past the solver's own limits, past a user's outer limit, a triangle, between
    a target and its outer limit) as 0/1 columns - the order a face's meshes are judged in (a quad in a band is within
    the line's allowance; it decides only between meshes of as many triangles: judged before the triangles, the
    steps' quads in a band lost s14's stepped strips to their paving, 28 -> 44 triangles, refused as a whole)."""
    nq = len(mesh.quads)
    invalid = _min_jac(mesh) <= 0
    past = shell_quality.nastran_past(mesh)
    outer = np.zeros(mesh.n_elements, dtype=bool)
    band = np.zeros(mesh.n_elements, dtype=bool)
    if getattr(lim, "aims", ()):
        for shape, el, off in (("quad", mesh.quads, 0), ("tria", mesh.trias, nq)):
            if not len(el):
                continue
            me = (shell_quality.quad_measures if shape == "quad" else shell_quality.tria_measures)(mesh.xyz, el,
                                                                                                    extra=True)
            sc = shell_quality.aim_score(me, shape, lim, len(el))
            outer[off:off + len(el)] = sc >= shell_quality.AIM_OUTER
            band[off:off + len(el)] = sc == shell_quality.AIM_BAND
    tri = np.zeros(mesh.n_elements, dtype=bool)
    tri[nq:] = True
    return np.stack([invalid, past, outer, tri, band], axis=1).astype(np.int64)


def _face_patches(mesh: ShellMesh, tol: float) -> dict:
    """SHMESH-91: per sewn face, (its element indices, the positions (rounded to `tol`) of the nodes on its patch's
    boundary - the edges its elements use once - as a frozenset)."""
    fo = np.asarray(mesh.face_of)  # type: ignore[attr-defined]
    nq = len(mesh.quads)
    out: dict = {}
    for k in np.unique(fo).tolist():
        els = np.flatnonzero(fo == k)
        cnt: dict = {}
        for e in els.tolist():
            nodes = (mesh.quads[e] if e < nq else mesh.trias[e - nq]).tolist()
            for i in range(len(nodes)):
                a, b = nodes[i], nodes[(i + 1) % len(nodes)]
                key = (min(a, b), max(a, b))
                cnt[key] = cnt.get(key, 0) + 1
        bnodes = {v for (a, b), c in cnt.items() if c == 1 for v in (a, b)}
        pos = frozenset(tuple(np.round(mesh.xyz[v] / tol).astype(np.int64).tolist()) for v in bnodes)
        out[int(k)] = (els, pos, len(bnodes))
    return out


def _face_keep(best: tuple, pool: list, requirements) -> tuple | None:
    """SHMESH-91: THE PER-FACE KEEP. `best` (name, mesh, applied, past n) is the candidate SHMESH-76 kept; `pool` every
    candidate made. For each sewn face, among the candidates whose nodes on that face's boundary stand where the kept
    mesh's do (the same positions, within POS_TOL mm), the face's own elements are judged - fewest not valid, then past
    the solver's limits, past an outer line, triangles, quads in a band - and the best taken, stitched on the shared
    boundary nodes (its inner nodes added, the kept mesh's there dropped). The stitched mesh replaces the kept one only
    when it stands better against it (SHMESH-36's standing, then fewer triangles, then fewer elements): never worse.
    None when no face changes or the stitched mesh does not stand better."""
    import copy

    _n0, m0, a0, p0 = best
    if getattr(m0, "face_of", None) is None or len(m0.face_of) != m0.n_elements:
        return None
    others = [c for c in pool if c[1] is not m0 and getattr(c[1], "face_of", None) is not None
              and len(c[1].face_of) == c[1].n_elements]
    if not others:
        return None
    lim = SR.limits_in_force(requirements)
    p_m0 = _face_patches(m0, POS_TOL)
    s_m0 = _face_mesh_score(m0, lim)
    pick: dict[int, tuple] = {}
    for name, m1, _a1, _p1 in others:
        p_m1 = _face_patches(m1, POS_TOL)
        s_m1 = _face_mesh_score(m1, lim)
        for k, (els0, pos0, nb0) in p_m0.items():
            got1 = p_m1.get(k)
            if got1 is None or got1[2] != nb0 or got1[1] != pos0 or len(pos0) != nb0:
                continue
            sc1 = tuple(s_m1[got1[0]].sum(axis=0).tolist()) + (len(got1[0]),)
            cur = pick.get(k, (None, tuple(s_m0[els0].sum(axis=0).tolist()) + (len(els0),)))[1]
            if sc1 < cur:
                pick[k] = ((name, m1, got1[0]), sc1)
    if not pick:
        return None
    # stitch: the kept mesh's elements off the picked faces, the picked faces' elements from their meshes
    nq0 = len(m0.quads)
    swapped = set(pick)
    keep0 = np.flatnonzero(~np.isin(m0.face_of, list(swapped)))
    xyz = [m0.xyz]
    nw = [m0.node_wall] if m0.node_wall is not None else None
    n_nodes = len(m0.xyz)
    at0 = {tuple(np.round(m0.xyz[v] / POS_TOL).astype(np.int64).tolist()): v for v in range(len(m0.xyz))}
    quads, trias, qa, ta = [], [], [], []      # (nodes, (source mesh, element index))
    for e in keep0.tolist():
        if e < nq0:
            quads.append(m0.quads[e].tolist())
            qa.append((m0, e))
        else:
            trias.append(m0.trias[e - nq0].tolist())
            ta.append((m0, e))
    for k, ((_name, m1, els1), _sc) in sorted(pick.items()):
        nq1 = len(m1.quads)
        remap: dict[int, int] = {}
        used = {int(v) for e in els1.tolist() for v in (m1.quads[e] if e < nq1 else m1.trias[e - nq1]).tolist()}
        bpos = p_m0[k][1]
        for v in sorted(used):
            key = tuple(np.round(m1.xyz[v] / POS_TOL).astype(np.int64).tolist())
            if key in bpos:
                remap[v] = at0[key]
            else:
                remap[v] = n_nodes
                n_nodes += 1
                xyz.append(m1.xyz[v][None, :])
                if nw is not None:
                    nw.append(np.asarray([(m1.node_wall[v] if m1.node_wall is not None else m0.wall[0])]))
        for e in els1.tolist():
            if e < nq1:
                quads.append([remap[int(v)] for v in m1.quads[e].tolist()])
                qa.append((m1, e))
            else:
                trias.append([remap[int(v)] for v in m1.trias[e - nq1].tolist()])
                ta.append((m1, e))
    X = np.vstack(xyz)
    Q = np.array(quads, dtype=np.int64).reshape(-1, 4)
    T = np.array(trias, dtype=np.int64).reshape(-1, 3)
    src = qa + ta
    # (the nodes no element uses - the kept mesh's inner nodes of the faces taken - are dropped)
    used_all = np.zeros(len(X), dtype=bool)
    used_all[Q.ravel()] = True
    used_all[T.ravel()] = True
    new_id = np.cumsum(used_all) - 1
    X = X[used_all]
    Q, T = new_id[Q], new_id[T]
    inv: dict = {}
    for name_g, members in m0.groups.items():
        for e in np.asarray(members).tolist():
            inv.setdefault((id(m0), int(e)), []).append(name_g)
    for _n1, m1, _a1, _p1 in others:
        if any(m_ is m1 for m_, _e in src):
            for name_g, members in m1.groups.items():
                for e in np.asarray(members).tolist():
                    inv.setdefault((id(m1), int(e)), []).append(name_g)
    groups: dict[str, list[int]] = {g: [] for g in m0.groups}
    for i, (m_, e) in enumerate(src):
        for g in inv.get((id(m_), int(e)), []):
            groups.setdefault(g, []).append(i)
    mesh = ShellMesh(xyz=X, quads=Q, trias=T, wall=np.array([float(m_.wall[e]) for m_, e in src]),
                     mid=[m_.mid[e] for m_, e in src], part=[m_.part[e] for m_, e in src],
                     groups={g: np.asarray(v, dtype=np.int64) for g, v in groups.items()},
                     node_wall=(np.concatenate(nw)[used_all] if nw is not None else None),
                     varies=(np.array([bool(m_.varies[e]) if m_.varies is not None else False for m_, e in src])
                             if m0.varies is not None else None),
                     zoffs=(np.array([float(m_.zoffs[e]) if m_.zoffs is not None else 0.0 for m_, e in src])
                            if m0.zoffs is not None else None),
                     notes=list(m0.notes), part_names=dict(m0.part_names))
    mesh.face_of = np.array([int(m_.face_of[e]) for m_, e in src], dtype=np.int64)  # type: ignore[attr-defined]
    applied = copy.deepcopy(a0)
    q = applied.setdefault("quality", {})
    q.update(shell_quality.measure(mesh))
    q["per_face"] = shell_quality.per_face(mesh)
    if "face_trias" in q:
        fo_t = np.asarray(mesh.face_of[len(Q):], dtype=np.int64)  # type: ignore[attr-defined]
        k_t, n_t = np.unique(fo_t, return_counts=True)
        q["face_trias"] = {str(int(k_)): int(n_) for k_, n_ in zip(k_t.tolist(), n_t.tolist(), strict=True)}
    asked = SR.report(mesh, requirements, applied.get("sizing"), places=applied.get("places"))
    if asked is not None:
        applied["requirements"] = asked
    past = shell_quality.nastran_past(mesh) | (_min_jac(mesh) <= 0)
    applied["_past_n"] = int(past.sum())
    past_n = int(past.sum())
    mine = (*_standing(a0, p0, applied, requirements), len(m0.trias), int(m0.n_elements))
    theirs = (*_standing(applied, past_n, a0, requirements), len(mesh.trias), int(mesh.n_elements))
    rec = {"faces": len(pick), "from": sorted({str(v[0][0]) for v in pick.values()}), "this": list(theirs),
           "that": list(mine), "kept": theirs < mine}
    a0.setdefault("quality", {})["face_keep"] = rec
    if not theirs < mine:
        return None
    q["face_keep"] = rec
    return (f"{best[0]}, faces kept best of {len(pool)}", mesh, applied, past_n)


def _guarded_path(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
                  target: dict | None = None, correction: float = 1.0,
                  requirements: dict | None = None) -> tuple[ShellMesh, dict, int]:
    """SHMESH-45: _mesh_path, with the structured steps never costing the mesh. When they raise, the mesh is made
    again with them off; when their mesh is invalid or has elements past Nastran's own limits, the mesh with them off
    is made too and kept if it is better (valid where the other is not, else fewer past). Either way the record says
    so (quality.structure.guard) and a note tells the user."""
    if not (STRUCTURED_GUARD and _structured_on(requirements)):
        return _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=log, target=target, correction=correction,
                          requirements=requirements)
    try:
        mesh, applied, past_n = _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                           correction=correction, requirements=requirements)
    except Exception as exc:  # noqa: BLE001 - a washer must never cost the mesh: fewer steps are tried
        why = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.info("shell mesh: the structured steps failed (%s); tried with round rings only", exc)
        try:
            # SHMESH-45g: round rings only first - the part keeps its washers when only the fuller steps fail
            with _rings_only():
                mesh, applied, past_n = _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                                   correction=correction, requirements=requirements)
            if _past(applied)[0] == 0:
                applied["quality"].setdefault("structure", {})["guard"] = {
                    "rule": "SHMESH-45g", "kept": "round rings only",
                    "why": f"the gridded, square and mapped steps failed ({why})"}
                mesh.notes.append("Mapped quads could not be made on this part; its round holes keep their washers "
                                  "(SHMESH-45g).")
                return mesh, applied, past_n
        except Exception as exc2:  # noqa: BLE001 - then the free mesh
            logger.info("shell mesh: round rings only failed too (%s); meshed without them", exc2)
        with _structured_off():
            mesh, applied, past_n = _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=log, target=target,
                                               correction=correction, requirements=requirements)
        applied["quality"].setdefault("structure", {})["guard"] = {"rule": "SHMESH-45", "kept": "free mesh",
                                                                    "why": f"the structured steps failed ({why})"}
        mesh.notes.append("Washers and mapped quads could not be made on this part; it is meshed without them, as "
                          "before (SHMESH-45).")
        return mesh, applied, past_n
    mine = _past(applied)
    gone = float((applied["quality"].get("face_watch") or {}).get("left_out_share") or 0.0)
    if gone > 0:
        # SHMESH-72: faces left out with the structured steps on: the mesh without them may make those faces
        try:
            with _structured_off():
                plain, applied_plain, plain_n = _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=None,
                                                           target=target, correction=correction,
                                                           requirements=requirements)
        except Exception as exc:  # noqa: BLE001 - the structured mesh stands
            logger.info("shell mesh: no mesh without the structured steps to compare (%s)", exc)
        else:
            gone_plain = float((applied_plain["quality"].get("face_watch") or {}).get("left_out_share") or 0.0)
            if (gone_plain, *_past(applied_plain)) < (gone, *mine):
                applied_plain["quality"].setdefault("structure", {})["guard"] = {
                    "rule": "SHMESH-72", "kept": "free mesh",
                    "why": f"with washers and mapped quads {100 * gone:.1f}% of the part was left out; without them "
                           f"{100 * gone_plain:.1f}%"}
                return plain, applied_plain, plain_n
    if mine == (0, 0):
        return mesh, applied, past_n
    if _RUN.get("cut") or not _affords(_RUN.get("first_s")):
        # SHMESH-31b: no trial that cannot finish before the deadline (or after a spot pass was cut off)
        applied["quality"].setdefault("structure", {})["guard"] = {
            "rule": "SHMESH-45", "kept": "structured",
            "why": "the mesh without the structured steps was not tried: no time left for a whole mesh more "
                   "(SHMESH-31b)"}
        return mesh, applied, past_n

    def plain_path():
        with _structured_off():
            return _mesh_path(brep, record, mm_per_unit=mm_per_unit, log=None, target=target, correction=correction,
                              requirements=requirements)

    try:
        plain, applied_plain, plain_n = _bounded(plain_path, _limit())
    except Exception as exc:  # noqa: BLE001 - the structured mesh stands
        logger.info("shell mesh: no mesh without the structured steps to compare (%s)", exc)
        return mesh, applied, past_n
    theirs = _past(applied_plain)
    if theirs < mine:
        applied_plain["quality"].setdefault("structure", {})["guard"] = {
            "rule": "SHMESH-45", "kept": "free mesh",
            "why": f"with washers and mapped quads the mesh had {mine[1]} element(s) past Nastran's limits"
                   + (" and was not valid" if mine[0] else "") + f"; without them {theirs[1]}"}
        plain.notes.append("Washers and mapped quads were left out on this part: the mesh without them is better "
                           "inside Nastran's limits (SHMESH-45).")
        return plain, applied_plain, plain_n
    applied["quality"].setdefault("structure", {})["guard"] = {"rule": "SHMESH-45", "kept": "structured",
                                                                "why": "no better without the structured steps"}
    return mesh, applied, past_n


#: SHMESH-36: what each figure of _standing compares, in order (the first that differs decides)
STANDING_WORDS_OLD = ("fewer elements past the solver's own limits",
                      "fewer lines past an outer limit where the other mesh is inside it (or less far past)",
                      "fewer elements past the user's outer limits", "more lines met")
#: SHMESH-51o61: how far one element is past an outer line decides last, after the counts
STANDING_BY_LINES = True
STANDING_WORDS = (("fewer elements past the solver's own limits",
                   "fewer lines past an outer limit where the other mesh is inside it",
                   "fewer elements past the user's outer limits", "more lines met",
                   "fewer lines further past an outer limit than the other mesh") if STANDING_BY_LINES
                  else STANDING_WORDS_OLD)


#: SHMESH-51o62: SHMESH-31's finer pass is kept in the ranked order - fewer elements past the solver's limits; with
#: those equal, fewer elements past the user's outer limits, or (those equal too) a user line met that the coarser
#: pass missed, none it met lost. False: fewer past the solver's limits alone, as before
FINER_RANKED = True


def _line_state(applied: dict) -> tuple[int, set]:
    """(elements past the user's outer limits summed over the limit lines, the ids of the lines met)."""
    rows = (applied.get("requirements") or {}).get("rows") or []
    beyond = sum(int(r.get("beyond") or 0) for r in rows if str(r.get("field", "")).startswith("limit:"))
    return beyond, {r.get("id") for r in rows if r.get("met") is True}


def _finer_kept(finer: dict, n_finer: int, coarser: dict, n_coarser: int) -> bool:
    """SHMESH-31 / 51o62: whether the finer pass is kept over the coarser one. On lab_fr003 the finer pass left as
    many elements past the solver's limits and was dropped, the mesh then missing the quad corner line by two
    elements (30 of 2842 between 30 and 45 degrees, 1% allowed)."""
    if n_finer != n_coarser or not FINER_RANKED:
        return n_finer < n_coarser
    (b_f, met_f), (b_c, met_c) = _line_state(finer), _line_state(coarser)
    if b_f != b_c:
        return b_f < b_c
    return bool(met_f - met_c) and met_c <= met_f


def _by_line(this: dict, that: dict) -> dict:
    """SHMESH-51o62: the two candidates line by line - elements past the outer limit on each, and whether the case is
    mixed (each mesh has fewer past on some line): the standing sums them, the record shows them apart."""
    theirs = {ln["id"]: ln for ln in that["lines"]}
    lines, this_better, that_better = [], 0, 0
    for ln in this["lines"]:
        o = theirs.get(ln["id"]) or {}
        a, b = int(ln.get("beyond") or 0), int(o.get("beyond") or 0)
        this_better += a < b
        that_better += b < a
        lines.append({"id": ln["id"], "past_outer": {this["mesh"]: a, that["mesh"]: b},
                      "met": {this["mesh"]: ln.get("met"), that["mesh"]: o.get("met")}})
    share = {}
    for c in (this, that):
        past = sum(int(x.get("beyond") or 0) for x in c["lines"])
        share[c["mesh"]] = float(f"{past / max(int(c['elements']), 1):.4g}")
    return {"lines": lines, "mixed": bool(this_better and that_better), "past_outer_share": share}


@contextlib.contextmanager
def _narrow_set(on: bool, skip: frozenset[str] = frozenset()):
    """SHMESH-51o65: NARROW_ALONG and NARROW_SKIP for one mesh, put back after."""
    g = globals()
    saved = (g["NARROW_ALONG"], g["NARROW_SKIP"])
    g["NARROW_ALONG"], g["NARROW_SKIP"] = on, frozenset(skip)
    try:
        yield
    finally:
        g["NARROW_ALONG"], g["NARROW_SKIP"] = saved


def _mid_neighbours(mesh: ShellMesh) -> dict[str, set[str]]:
    """SHMESH-51o65: per mid-face, the mid-faces sharing a mesh edge with it."""
    owner: dict[tuple[int, int], set[str]] = {}
    for c, m in zip(list(mesh.quads) + list(mesh.trias), mesh.mid, strict=True):
        k = len(c)
        for i in range(k):
            a, b = int(c[i]), int(c[(i + 1) % k])
            owner.setdefault((a, b) if a < b else (b, a), set()).add(str(m))
    out: dict[str, set[str]] = {}
    for ms_ in owner.values():
        for m in ms_:
            out.setdefault(m, set()).update(ms_ - {m})
    return out


def _group_lines(mesh: ShellMesh, group: set[str], lim) -> dict:
    """SHMESH-51o65: the elements of the mid-faces `group` on each line - triangles, past the solver's limits, and per
    user line (key, op, target) those past its target and past its outer line, quads and triangles together."""
    mids = np.array([str(m) for m in mesh.mid])
    nq = len(mesh.quads)
    sel = np.isin(mids, sorted(group))
    qs, ts = sel[:nq], sel[nq:]
    Q, T = mesh.quads[qs], mesh.trias[ts]
    out: dict = {"trias": int(len(T))}
    mq = shell_quality.quad_measures(mesh.xyz, Q, extra=lim.extra) if len(Q) else {}
    mt = shell_quality.tria_measures(mesh.xyz, T, extra=lim.extra) if len(T) else {}
    out["solver"] = int((shell_quality._quad_past(mq).sum() if len(Q) else 0)
                        + (shell_quality._tria_past(mt).sum() if len(T) else 0))
    for row_shape, key, op, target, outer, _share in lim.aims:
        m = mq if row_shape == "quad" else mt
        if key not in m:
            continue
        v = np.asarray(m[key])
        for name, lv in (("in", target), ("out", outer)):
            if lv is None:
                continue
            n_ = int(((v < lv) if op == ">=" else (v > lv)).sum())
            out[(key, op, target, name)] = out.get((key, op, target, name), 0) + n_
    return out


def _whole_met(whole: dict, n_all: int, lim) -> dict:
    """SHMESH-51o65: per user line (key, op, target), whether the whole mesh meets it - nothing past its outer line and
    at most its allowance past its target."""
    out = {}
    for _s, key, op, target, _o, sh in lim.aims:
        k = (key, op, target)
        # (the band keys are k + ("out",) / k + ("in",): past the outer line, past the target only)
        out[k] = whole.get(k + ("out",), 0) == 0 and whole.get(k + ("in",), 0) <= float(sh or 0.0) * n_all
    return out


def _narrow_worse(a: dict, b: dict, met_a: dict, met_b: dict) -> tuple[str, str] | None:
    """SHMESH-51o65, in the ranked order: (the step, why) on which the stretched group `a` is worse than the same group
    at today's sizing `b`, or None (no worse, or better at a step before it):
    a - more elements past the solver's limits (fewer: better, the comparison stops);
    b - more past an outer line, summed over the lines (fewer: better, stops - whatever the in-band counts do);
    c - more past a target on a line the whole mesh meets at today's sizing and misses stretched;
    d - more triangles."""
    if a.get("solver", 0) != b.get("solver", 0):
        return ("a", "more past the solver's limits") if a.get("solver", 0) > b.get("solver", 0) else None
    out_a = sum(v for k, v in a.items() if isinstance(k, tuple) and k[3] == "out")
    out_b = sum(v for k, v in b.items() if isinstance(k, tuple) and k[3] == "out")
    if out_a != out_b:
        return ("b", f"more past an outer line ({out_b} -> {out_a})") if out_a > out_b else None
    for k in sorted((x for x in a if isinstance(x, tuple) and x[3] == "in"), key=str):
        if a.get(k, 0) > b.get(k, 0) and met_b.get(k[:3]) and not met_a.get(k[:3]):
            return "c", f"{k[0]} line met with today's sizing, missed stretched"
    if a.get("trias", 0) > b.get("trias", 0):
        return "d", f"more triangles ({b.get('trias', 0)} -> {a.get('trias', 0)})"
    return None


#: SHMESH-51o65: no comparison mesh where the stretched faces hold under this share of the elements
NARROW_KEEP_SHARE = 0.01


def _narrow_kept_path(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
                      target: dict | None = None, correction: float = 1.0,
                      requirements: dict | None = None) -> tuple[ShellMesh, dict, int]:
    """SHMESH-51o65: _guarded_path; with NARROW_ALONG and NARROW_KEEP, the mesh with faces stretched along is set face
    by face against one mesh with today's sizing (none where the stretched faces hold under NARROW_KEEP_SHARE of the
    elements). Each stretched face's group - it and the faces sharing an edge with it - is judged in the ranked order
    (_narrow_worse). Faces worse at steps a-c go back to today's sizing together in one more mesh (those worse on
    triangles alone with them); where every face is, today's mesh stands. Faces worse on triangles alone earn no
    mesh of their own: the stretched mesh stands where its triangles are not more than today's, else today's."""
    kw: dict[str, Any] = {"mm_per_unit": mm_per_unit, "log": log, "target": target, "correction": correction,
                          "requirements": requirements}
    mesh, applied, past_n = _guarded_path(brep, record, **kw)
    along_rec = (applied.get("sizing") or {}).get("narrow_along") or {}
    faces = list(along_rec.get("faces") or [])
    if not (NARROW_ALONG and NARROW_KEEP and faces and requirements):
        return mesh, applied, past_n
    # (the share counted on the faces narrower than the shortest edge: a face beside a short CAD edge is stretched
    # only round that edge)
    held = int(np.isin(np.array([str(m) for m in mesh.mid]), list(along_rec.get("narrow_faces") or [])).sum())
    rec: dict[str, Any] = {"rule": "SHMESH-51o65", "stretched": len(faces), "elements_stretched": held}
    if held < NARROW_KEEP_SHARE * mesh.n_elements:
        applied["quality"]["narrow_kept"] = {**rec, "kept": "stretched", "meshes": 1,
                                             "why": "the stretched faces hold too few elements to compare"}
        return mesh, applied, past_n
    lim = SR.limits_in_force(requirements)
    with _narrow_set(False):
        base, applied_b, past_b = _guarded_path(brep, record, **{**kw, "log": None})
    nb = _mid_neighbours(mesh)
    for m, v in _mid_neighbours(base).items():
        nb.setdefault(m, set()).update(v)
    whole_a = _group_lines(mesh, {str(m) for m in mesh.mid}, lim)
    whole_b = _group_lines(base, {str(m) for m in base.mid}, lim)
    met_a, met_b = _whole_met(whole_a, mesh.n_elements, lim), _whole_met(whole_b, base.n_elements, lim)
    back: dict[str, str] = {}
    tri_only: dict[str, str] = {}
    for f in faces:
        group = {f} | nb.get(f, set())
        got = _narrow_worse(_group_lines(mesh, group, lim), _group_lines(base, group, lim), met_a, met_b)
        if got is not None:
            (tri_only if got[0] == "d" else back)[f] = f"{got[0]}: {got[1]}"
    rec.update(back=len(back), triangles_only=len(tri_only), why=dict(sorted({**back, **tri_only}.items())[:40]),
               meshes=2)
    if back and len(back) + len(tri_only) == len(faces):
        out = (base, applied_b, past_b)
        rec["kept"] = "today's sizing"
    elif back:
        with _narrow_set(True, frozenset(back) | frozenset(tri_only)):
            out = _guarded_path(brep, record, **{**kw, "log": None})
        rec["kept"] = "stretched, some faces back"
        rec["meshes"] = 3
    elif tri_only and whole_a["trias"] > whole_b["trias"]:
        out = (base, applied_b, past_b)
        rec["kept"] = "today's sizing (more triangles stretched)"
    else:
        out = (mesh, applied, past_n)
        rec["kept"] = "stretched"
    out[1]["quality"]["narrow_kept"] = rec
    return out


#: SHMESH-36b: when each mesh has more elements past an outer limit than the other on some line of the user's, the
#: one meeting more of the user's lines (every row, the edge lines too) is kept, then the one with fewer elements past
#: the outer limits in total, then the one with fewer lines past, then the one whose worst overshoot is the smaller
#: share of its line's band; the solver's limits come first, unchanged. Counted line by line first, one triangle 0.13
#: degrees past the 140-degree line handed fuselage_01 to #435's mesh with 74 past the triangle-min line (against 7)
#: and 2 elements over the longest edge. The total alone first handed sh0114 to #435's mesh (15 against 18 past),
#: which misses two lines this PR's mesh meets (aspect, the longest edge)
MIXED_BY_SHARE = True
MIXED_WORDS = ("more of the user's lines met", "fewer elements past the user's outer limits in total",
               "fewer lines past an outer limit", "the smaller worst overshoot (a share of its band)")


def _each_worse(a: dict, b: dict) -> bool:
    """SHMESH-36b: whether each candidate has more elements past the outer limit than the other on some line."""
    def beyond(x: dict) -> dict:
        return {r.get("id"): int(r.get("beyond") or 0) for r in ((x.get("requirements") or {}).get("rows") or [])
                if str(r.get("field", "")).startswith("limit:")}
    ba, bb = beyond(a), beyond(b)
    ids = set(ba) | set(bb)
    return any(ba.get(i, 0) > bb.get(i, 0) for i in ids) and any(bb.get(i, 0) > ba.get(i, 0) for i in ids)


def _mixed_key(applied: dict, requirements: dict | None) -> tuple[int, int, int, float]:
    """SHMESH-36b: (minus the user's lines met - every row, the edge lines too -, elements past the user's outer
    limits, lines with one past, the largest overshoot over a line's band width - target to outer limit) of a
    candidate; lower is better."""
    every = (applied.get("requirements") or {}).get("rows") or []
    met = sum(1 for r in every if r.get("met") is True)
    rows = [r for r in every if str(r.get("field", "")).startswith("limit:")]
    lims = {lim.get("id"): lim for lim in (requirements or {}).get("limits") or []}
    beyond = sum(int(r.get("beyond") or 0) for r in rows)
    lines = sum(1 for r in rows if int(r.get("beyond") or 0))
    worst = 0.0
    for r in rows:
        lim = lims.get(r.get("id")) or {}
        if not int(r.get("beyond") or 0) or r.get("worst") is None or lim.get("outer") is None:
            continue
        outer, target = float(lim["outer"]), float(lim.get("target", lim["outer"]))
        over = (outer - float(r["worst"])) if str(lim.get("op")) == ">=" else (float(r["worst"]) - outer)
        worst = max(worst, over / max(abs(outer - target), 1e-12))
    return -met, beyond, lines, float(f"{worst:.6g}")


def _candidate(name: str, mesh: ShellMesh, applied: dict, past_n: int) -> dict:
    """SHMESH-36: one candidate mesh as the record tells it: its elements, those past the solver's own limits, and
    per line of the user's: outside, past the outer limit, the worst, met."""
    rows = (applied.get("requirements") or {}).get("rows") or []
    return {"mesh": name, "elements": int(mesh.n_elements), "trias": int(len(mesh.trias)),
            "past_solver": int(past_n), "nastran_past": dict(applied["quality"].get("nastran_past") or {}),
            "lines": [{"id": r.get("id"), "asked": r.get("asked"), "outside": r.get("outside"),
                       "beyond": r.get("beyond"), "worst": r.get("worst"), "met": r.get("met")}
                      for r in rows if str(r.get("field", "")).startswith("limit:")]}


def _mesh_path(brep: Path | str, record: dict, **kw) -> tuple[ShellMesh, dict, int]:
    """_mesh_path_made (every argument passed on whole); SHMESH-21b: its mesh is kept in hand (_so_far) for the stop."""
    mesh, applied, past_n = _mesh_path_made(brep, record, **kw)
    _so_far(mesh, applied, past_n, kw.get("requirements"))
    return mesh, applied, past_n


def _mesh_path_made(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
                    target: dict | None = None, correction: float = 1.0,
                    requirements: dict | None = None) -> tuple[ShellMesh, dict, int]:
    """The whole path of one shell mesh: closed faces cut, faces gmsh cannot mesh re-made, SHMESH-31's local
    re-mesh. Returns (the mesh, the plan as applied, its elements past the solver's own limits - with requirements
    only)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="shell_mesh_") as tmp:
        owners = list(record["surface_faces"])
        split_note = ""
        if SPLIT_CLOSED and not _WHOLE_NOW[0]:
            cut = Path(tmp) / "cut.brep"
            try:
                owners = split_closed(Path(brep), owners, cut)
                brep = cut
            except ValueError as exc:
                # SHMESH-11d: the closed faces' pieces would not fuse back with the rest (a 45 degree cone's pieces,
                # exact or fuzzy): the surface is meshed as sewn, and a closed face gmsh stops on is re-made below
                split_note = f"the closed faces were meshed whole ({exc})"
        reseated = 0
        redrawn: list = []
        # SHMESH-45b/45k: a hole is drawn again as one circle only where a first mesh's ring needed it (its nodes
        # uneven, or the ring not built): drawing every fitted or split hole again changed gmsh's paving round holes
        # that take no washer (sh0028: a longest edge 17% past 1.5 G, 75 mm from the one hole drawn again)
        unseated = brep
        redraw_pass = not (ST.RESEAT and ST.WASHERS and SR.structure_of(requirements) not in ("free", "no_washers"))
        redraw_off = redraw_pass
        resplined = loops = 0
        wires_fixed: list[int] = []     # (SHMESH-11i: faces whose wires were fixed on their re-make)
        _FACE_FATE["out_path"] = []         # (SHMESH-72: faces left out in this guarded path only)
        started = time.monotonic()
        unmap: set[int] = set()
        unmapped = 0
        no_cut: set[int] = set()
        mid_body = {m["id"]: m.get("body") for m in record.get("mids", [])}

        def body_of(k: int):
            # (SHMESH-51f: the body a sewn face is of)
            return mid_body.get(owners[k]) if 0 <= k < len(owners) else None

        hole_rounds: int | dict[int, int] = {} if GATHER else 0    # (SHMESH-51f: with GATHER, rounds per sewn face)
        one_ring_at: list = []              # SHMESH-51c: bolt holes back to one ring (their box failed)
        gridded_ever: set[int] = set()      # SHMESH-51j: the faces cut into a grid on any pass of this run
        refused: dict[int, dict] = {}
        compare: dict[int, dict] = {}       # SHMESH-48x: refused grids' figures, to set against their free meshes
        kept: set[int] = set()
        before_guard: tuple | None = None   # (what was meshed free and how many rounds, when a guard first refused)
        passes: list[list] = []             # SHMESH-48l: each whole mesh made - what followed it, its seconds
        # SHMESH-48l2: every hole a redraw asked for, as ("w" one circle / "s" O-grid seam, its entry, its sewn face),
        # and those drawn in the brep meshed now
        asked: list[tuple[str, Any, int]] = []
        seated_now: list[tuple[str, Any, int]] = []
        n_seat = 0
        # SHMESH-51o: the mesh made before one ring was tried on faces whose two rings left triangles, those faces per
        # trial, and what each trial came to
        guard_best: tuple | None = None
        guard_faces: list[list[int]] = []
        guard_log: list[dict] = []
        guard_snap: dict | None = None      # (SHMESH-51o4: the run's state when the mesh before a trial was made)
        guard_part_tried = False            # (SHMESH-51o5: the faces that gained of a trial tried alone, once a run)
        regrid_log: list[dict] = []         # (SHMESH-48x3: grids back with no triangle, and their short elements)
        refusal_log: list[dict] = []        # (SHMESH-51o27: every pass's refusals and cells freed first, in words)

        def redraw(wanted: list, seam_wanted: list, w_owner: list, s_owner: list) -> bool:
            """SHMESH-45k/49c: one more pass for both: the holes whose ring needed them drawn again as one circle, and
            the O-grid holes drawn again to start where their O-grid cuts (a seam inside a quarter makes it two
            curves, counted unevenly). Whether any was drawn."""
            nonlocal redraw_pass, brep, reseated, redrawn, asked, seated_now, unmap, unmapped
            redraw_pass = True
            # (SHMESH-48l2: after a refusal, with the holes still drawn - the new ones join them)
            had = list(seated_now)
            wanted = [e for k_, e, _o in had if k_ in ("w", "W")] + wanted
            seam_wanted = [e for k_, e, _o in had if k_ == "s"] + seam_wanted
            w_owner = [o for k_, _e, o in had if k_ in ("w", "W")] + w_owner
            s_owner = [o for k_, _e, o in had if k_ == "s"] + s_owner
            seated = Path(tmp) / ("reseated.brep" if not had else f"reseated_more_{n_seat}.brep")
            try:
                n2, red2 = ST.reseat_holes(Path(unseated), seated,
                                           only=wanted + [e[0] if len(e) == 2 else e for e in seam_wanted],
                                           seam=seam_wanted)
            except Exception as exc:  # noqa: BLE001 - the first mesh stands
                logger.info("shell mesh: holes not drawn again (%s)", exc)
                n2, red2 = 0, []
            if not n2:
                return False
            brep, reseated, redrawn = seated, n2, red2
            asked = [("w", e, int(o)) for e, o in zip(wanted, w_owner, strict=True)] + [
                ("s", e, int(o)) for e, o in zip(seam_wanted, s_owner, strict=True)]
            seated_now = list(asked)
            # (SHMESH-51f: the cells freed are kept by their keys, but for those whose freeing came from a face whose
            # holes are drawn again)
            unmap, unmapped = (_keys_kept(unmap, {o for _k, _e, o in asked}) if GATHER else set()), 0
            return True

        def seat_kept() -> tuple[Path | str, int, list, bool, list]:
            """SHMESH-48l2/49g: the refused faces' holes go back to the CAD's own; every other hole drawn again stays
            drawn (the CAD's brep when none is left). Returns (brep, holes drawn, their list, whether a redraw was
            made, the entries drawn)."""
            nonlocal n_seat
            keep = [x for x in asked if x[0] == "W" or x[2] not in no_cut]
            if keep == seated_now:
                return brep, reseated, redrawn, redraw_pass, seated_now
            if keep:
                n_seat += 1
                out_ = Path(tmp) / f"reseated_kept_{n_seat}.brep"
                only_ = [e for k_, e, _o in keep if k_ in ("w", "W")]
                seam_ = [e for k_, e, _o in keep if k_ == "s"]
                try:
                    n3, red3 = ST.reseat_holes(Path(unseated), out_, only=only_ + [e[0] if len(e) == 2 else e
                                                                                   for e in seam_], seam=seam_)
                except Exception as exc:  # noqa: BLE001 - then the CAD's own holes, as before
                    logger.info("shell mesh: the kept holes were not drawn again (%s)", exc)
                    n3, red3 = 0, []
                if n3:
                    return out_, n3, red3, True, keep
            return unseated, 0, [], redraw_off, []

        ul_state: dict = {}         # (SHMESH-51o58: the faces' one-ring counts, across the passes)
        spots_dropped = False       # (SHMESH-31i: a spot pass of this path was not kept)
        while True:
            t_pass = time.monotonic()
            if guard_best is not None and MESH_DEADLINE_S > 0 and t_pass - started > GUARD_TIME * MESH_DEADLINE_S:
                # (SHMESH-51o: a trial of one ring that runs long is let go: the mesh before it stands)
                guard_log.append({"one_ring_faces": guard_faces[-1], "kept": "two rings", "why": "out of time"})
                mesh, applied, past_n = guard_best
                applied["quality"]["structure"]["bolt_guard"] = list(guard_log)
                return mesh, applied, past_n
            try:
                mesh, applied = _mesh_once(Path(brep), record, owners, float(mm_per_unit), log, target=target,
                                           hole_before=tuple((tuple(c), e) for c, e in redrawn),
                                           correction=correction, requirements=requirements, unmap=frozenset(unmap),
                                           no_cut=frozenset(no_cut), check_maps=unmapped < UNMAP_ROUNDS,
                                           judge_faces=True, compare=compare, grids_kept=frozenset(kept),
                                           one_ring_at=tuple(one_ring_at), hole_rounds=hole_rounds,
                                           gridded_ever=gridded_ever, own_bolt=frozenset(k for f_ in guard_faces for k in f_)
                                           if GUARD_OWN else frozenset(), ul_state=ul_state,
                                           redraw_now=redraw if EARLY_REDRAW and not redraw_pass and not resplined
                                           else None)
                passes.append(["made", round(time.monotonic() - t_pass, 1)])
                if _RUN.get("first_s") is None:
                    # (SHMESH-31b: what a whole mesh more costs - this path's time to its first mesh)
                    _RUN["first_s"] = time.monotonic() - started
                    # (SHMESH-21e: and the limit follows this part's own measured mesh)
                    _reprice(first_s=_RUN["first_s"], built=int(mesh.n_elements))
            except _Redraw:
                passes.append(["holes drawn again", round(time.monotonic() - t_pass, 1)])
                continue
            except _UnderLow as ul:
                # SHMESH-51o58: the face comparison of the rings under the shortest edge - another mesh from the start
                passes.append([f"rings under the shortest edge: {ul.why}", round(time.monotonic() - t_pass, 1)])
                no_cut = (set(no_cut) | ul.add) - ul.remove
                one_ring_at.extend(ul.sent)
                unmapped, compare, kept, before_guard = 0, {}, set(), None
                brep, reseated, redrawn, redraw_pass, asked, seated_now = unseated, 0, [], redraw_off, [], []
                continue
            except _NoCut as nc:
                held_ = ul_state.get("stash_base")
                if held_ is not None and not _trial_ok(held_[1].get("_past_n", 0), requirements):
                    # SHMESH-31f: the trial would mesh again; the solver-limit pass is owed first - the one-ring
                    # mesh held to fall back on stands
                    passes.append([f"rings under the shortest edge: the one-ring mesh held stands, {_trial_why()}",
                                   round(time.monotonic() - t_pass, 1)])
                    mesh, applied = ul_state.pop("stash_base")
                    ul_state.pop("decision", None)
                else:
                    refusal_log.extend({"pass": len(passes) + 1, "face": _owner_of_key(k_, owners),
                                        **{a_: b_ for a_, b_ in dict(v_).items() if a_ in REFUSAL_KEYS}}
                                       for k_, v_ in sorted(nc.why.items()))
                    if nc.holes:
                        refusal_log.append({"pass": len(passes) + 1, "holes_to_one_ring": len(nc.holes),
                                            "why": sorted(set(nc.hole_why or []))[:6]})
                    if nc.holes and all(f < 0 and -f - 1 >= UNDER_LOW_KEY for f in nc.faces) and not resplined:
                        # SHMESH-51c: bolt holes whose two-ring box failed in their grid take one ring - start again
                        passes.append(["one ring round some bolt holes so a grid stays (start again)",
                                       round(time.monotonic() - t_pass, 1)])
                        one_ring_at.extend(nc.holes)
                        if isinstance(hole_rounds, dict):
                            # (SHMESH-51f: a round spent by each face whose holes these are, not by the others)
                            for f_h in sorted(set(nc.hole_faces)):
                                hole_rounds[f_h] = hole_rounds.get(f_h, 0) + 1
                        else:
                            hole_rounds += 1
                        no_cut = {f for f in no_cut if f < 0} | set(nc.faces)
                        refused = {k: v for k, v in {**refused, **nc.why}.items() if k < 0}
                        # (SHMESH-51f: the grid cells freed so far stay free, by their keys, with those this pass found - but
                        # for those whose freeing came from a face whose holes now take one ring)
                        unmap = _keys_kept(unmap | set(nc.unmap), nc.hole_faces, body_of) if GATHER else set()
                        unmapped, compare, kept, before_guard = 0, {}, set(), None
                        brep, reseated, redrawn, redraw_pass, asked, seated_now = unseated, 0, [], redraw_off, [], []
                        continue
                    if ST.BOLT_GRID_FIRST and not resplined and any(f < 0 and -f - 1 >= UNDER_LOW_KEY for f in nc.faces):
                        # SHMESH-51b: a face's bolt holes back to one ring so it keeps its grid - the run starts again with
                        # only that, so the face goes through the passes as it would with one ring from the start (the
                        # rounds and comparisons a refusal costs are not spent on it)
                        passes.append(["one ring so a grid stays (start again)", round(time.monotonic() - t_pass, 1)])
                        no_cut = {f for f in no_cut | set(nc.faces) if f < 0}
                        refused = {k: v for k, v in {**refused, **nc.why}.items() if k < 0}
                        unmap = _keys_kept(unmap, {_face_of_key(f) for f in nc.faces if f < 0}, body_of) if GATHER else set()
                        unmapped, compare, kept, before_guard = 0, {}, set(), None
                        brep, reseated, redrawn, redraw_pass, asked, seated_now = unseated, 0, [], redraw_off, [], []
                        continue
                    passes.append(["grids left out", round(time.monotonic() - t_pass, 1)])
                    # SHMESH-47b: those faces' grids left out (their joined faces whole); counts as a round of SHMESH-40
                    if before_guard is None and any("grid_figures" in v for v in nc.why.values()):
                        before_guard = (set(unmap), unmapped)
                    no_cut |= nc.faces
                    if GATHER:
                        # (SHMESH-51f: the cells whose freeing came from a face now left ungridded are let go - its mesh
                        # changes all over)
                        unmap = _keys_kept(unmap | set(nc.unmap), {_face_of_key(f) for f in nc.faces})
                    refused.update(nc.why)
                    compare.update({k: dict(v["grid_figures"]) for k, v in nc.why.items() if "grid_figures" in v})
                    unmapped += 1
                    if brep != unseated and not resplined:
                        # their holes drawn again for the O-grids go back to the CAD's own (SHMESH-49g); SHMESH-48l2: only
                        # theirs - the other faces' holes stay drawn again (going back to the CAD's brep for all of them
                        # cost a whole round of passes to draw the same holes again: 75 s on a column). A hole on a
                        # refused face its free ring asks for is drawn on the next pass (one more redraw, merged)
                        brep, reseated, redrawn, redraw_pass, seated_now = seat_kept()
                        if seated_now:
                            redraw_pass = False
                    continue
            except _Regrid as rg:
                passes.append(["grids back (no better free)", round(time.monotonic() - t_pass, 1)])
                # (SHMESH-48x3: a grid back with no triangle against its free mesh's: told with its short elements)
                regrid_log.extend({"face": _owner_of_key(k, owners), "why": str(v["why_back"])}
                                  for k, v in sorted(rg.why.items()) if "why_back" in v)
                no_cut -= rg.faces
                kept |= rg.faces
                if before_guard is not None:
                    # (the grid comes back as it was judged: the cells meshed free since belong to the free pass's
                    # own gmsh faces)
                    unmap, unmapped = set(before_guard[0]), before_guard[1]
                    before_guard = None
                for k in rg.faces:
                    refused.pop(k, None)
                    compare.pop(k, None)
                if asked and not resplined:
                    # SHMESH-48l2: the faces whose grids come back take their holes drawn again back too; and the holes
                    # this pass's free rings asked for (on the faces that stay free) are drawn with them now - the
                    # redraw the next pass would ask for, without that pass
                    # (a hole already asked for and still drawn: not twice - an O-grid seam of a face now free is not
                    # drawn any more, its free ring may ask for one circle there)
                    have = [np.asarray(e if len(e) != 2 else e[0], dtype=float) for k_, e, o_ in asked
                            if k_ == "W" or o_ not in no_cut]
                    extra = [("W", c, o) for c, o in rg.wanted if o not in rg.faces
                             and not any(float(np.linalg.norm(np.asarray(c) - h_)) <= 1e-6 * max(1.0, float(
                                 np.linalg.norm(h_))) for h_ in have)]
                    asked = asked[:]
                    for x in extra:
                        # (each in its place: the one-circle holes before the O-grid seams, as a redraw lists them)
                        at = max((i + 1 for i, y in enumerate(asked) if y[0] in ("w", "W")), default=0)
                        asked.insert(at, x)
                    brep, reseated, redrawn, redraw_pass, seated_now = seat_kept()
                    if seated_now and extra and any(x in seated_now for x in extra):
                        # (drawn as a redraw draws: its grids found again from none; SHMESH-51f: by keys, kept but for
                        # those whose freeing came from a face with holes drawn now)
                        unmap, unmapped = (_keys_kept(unmap, {o for _k, _e, o in seated_now}) if GATHER else set()), 0
                        redraw_pass = True
                    elif seated_now:
                        redraw_pass = False
                continue
            except _Restep:
                passes.append(["steps freed", round(time.monotonic() - t_pass, 1)])
                continue
            except _Unmap as u:
                refusal_log.extend({"pass": len(passes) + 1, "cell_freed_first": True, **t_}
                                   for t_ in getattr(u, "told", None) or [])
                passes.append(["grids meshed free", round(time.monotonic() - t_pass, 1)])
                # SHMESH-40: a grid that came out past the solver's limits is meshed free (the rest mapped again)
                unmap |= set(u.keys) if GATHER else u.faces
                unmapped += 1
                continue
            except _Periodic as p:
                if guard_best is not None:
                    # (SHMESH-51o: a trial of one ring that gmsh stops on is let go: the mesh before it stands)
                    guard_log.append({"one_ring_faces": guard_faces[-1], "kept": "two rings",
                                      "why": "gmsh stopped on a face"})
                    mesh, applied, past_n = guard_best
                    applied["quality"]["structure"]["bolt_guard"] = list(guard_log)
                    return mesh, applied, past_n
                key_p = _fate_key(_faces_and_facts(Path(brep), float(mm_per_unit))[p.face]) if REMADE_ONCE else None
                why_out = None
                if (REMADE_EARLY and key_p is not None
                        and any(_same_face(key_p, k_, loose=True) for k_ in _FACE_FATE["remade"])):
                    why_out = "re-made once, it stopped gmsh again"
                if resplined >= RESPLINE_MAX:
                    if not REMADE_ONCE:
                        raise ValueError(f"gmsh could not mesh {resplined + 1} periodic faces") from p
                    why_out = why_out or "the most faces a shell mesh re-makes were re-made"
                loops += p.loop
                if loops > RESPLINE_LOOP_MAX and why_out is None:
                    if not REMADE_ONCE:
                        # SHMESH-11c: a surface whose faces fail one after another is not a seam to take out: it stops
                        raise ValueError(f"gmsh could not close the boundary of {loops} faces in turn, each re-made as a "
                                         "B-spline; the shell mesh is given up (SHMESH-11c)") from p
                    why_out = "faces failed one after another"
                # SHMESH-11e: each re-made face costs a whole mesh again; past the budget the mesh stops and says why
                # (SHMESH-72: one budget for the whole shell mesh, every guarded path included)
                if REMADE_ONCE:
                    # (SHMESH-72: the re-making's own time - this failed attempt, and each fuse back - over the run)
                    _FACE_FATE["remake_s"] += time.monotonic() - t_pass
                spent = float(_FACE_FATE["remake_s"]) if REMADE_ONCE and REMAKE_RUN_BUDGET else (
                    time.monotonic() - started)
                if spent > RESPLINE_BUDGET_S and why_out is None:
                    if not REMADE_ONCE:
                        raise ValueError(f"gmsh stopped on a face again after {resplined} face(s) re-made and "
                                         f"{spent / 60:.0f} minutes; the shell mesh is given up (SHMESH-11e)") from p
                    why_out = "the time for re-making faces was spent"
                if why_out is not None:
                    # SHMESH-72: the failing face is left out (said), not re-made again; past FACE_OUT_MAX, given up
                    if len(_FACE_FATE["out"]) + len(_FACE_FATE["out_path"]) >= FACE_OUT_MAX:
                        raise ValueError(f"gmsh stopped on {len(_FACE_FATE['out']) + len(_FACE_FATE['out_path']) + 1} "
                                         "faces; the shell mesh is given up (SHMESH-72)") from p
                    _FACE_FATE["out_path"].append(key_p)
                    logger.info("shell mesh: a face left out (%s; SHMESH-72)", why_out)
                    continue
                if key_p is not None:
                    _FACE_FATE["remade"].append(key_p)
                    _fate_save()
                t_fuse = time.monotonic()
                nxt = Path(tmp) / f"respline_{resplined}.brep"
                marker = Path(_WATCH_DIR[0]) / "rebuild.json" if _WATCH_DIR[0] and key_p is not None else None
                if marker is not None and key_p is not None:
                    # (SHMESH-72: the parent watches the fuse back)
                    marker.write_text(json.dumps({"since": time.time(), "centre": list(map(float, key_p[0])),
                                                  "area": float(key_p[1])}))
                try:
                    owners, made = respline(Path(brep), owners, p.face, nxt, kin=p.kin, fixed=wires_fixed)
                except _NotRemade as nr:
                    if marker is not None:
                        marker.unlink(missing_ok=True)
                    if key_p is None or not REMADE_ONCE:
                        raise ValueError(f"gmsh stopped on a face and it could not be re-made: {nr}") from p
                    # SHMESH-11i: a face that cannot be re-made without losing area is left out (said, SHMESH-72)
                    if len(_FACE_FATE["out"]) + len(_FACE_FATE["out_path"]) >= FACE_OUT_MAX:
                        raise ValueError(f"gmsh stopped on {len(_FACE_FATE['out']) + len(_FACE_FATE['out_path']) + 1} "
                                         "faces; the shell mesh is given up (SHMESH-72)") from p
                    _FACE_FATE["out_path"].append(key_p)
                    logger.info("shell mesh: a face left out (it could not be re-made; SHMESH-11i)")
                    continue
                if marker is not None:
                    marker.unlink(missing_ok=True)
                if REMADE_ONCE:
                    _FACE_FATE["remake_s"] += time.monotonic() - t_fuse
                    _fate_save()
                brep = nxt
                resplined += made
                continue
            except Exception as exc:  # noqa: BLE001 - only while one ring is tried: the mesh before it stands
                if guard_best is None:
                    raise
                guard_log.append({"one_ring_faces": guard_faces[-1], "kept": "two rings",
                                  "why": f"the trial failed ({type(exc).__name__})"})
                mesh, applied, past_n = guard_best
                applied["quality"]["structure"]["bolt_guard"] = list(guard_log)
                return mesh, applied, past_n
            dec_ = ul_state.pop("decision", None)
            if dec_ is not None:
                # SHMESH-51o59/60: what the one-ring mesh decided
                kind_d, keys_d, sent_d = dec_
                if kind_d == "keep":
                    # (no face worse with its rings: the mesh kept before stands, no third mesh)
                    passes[-1][0] = "rings under the shortest edge: no line worse, the mesh before kept"
                    no_cut = set(no_cut) - keys_d
                    mesh, applied = ul_state.pop("stash")
                elif kind_d == "base":
                    # (every such hole back to one ring: this one-ring mesh is the mesh)
                    passes[-1][0] = "rings under the shortest edge: all back to one ring"
                    one_ring_at.extend(sent_d)
                elif kind_d == "restore":
                    passes[-1][0] = "rings under the shortest edge: all back to one ring (the one-ring mesh held)"
                    mesh, applied = ul_state.pop("stash_base")
                elif not _trial_ok(applied.get("_past_n", 0), requirements):
                    # SHMESH-31f: some holes would go back - a third mesh; the solver-limit pass is owed first:
                    # this one-ring mesh stands
                    passes[-1][0] = f"rings under the shortest edge: {len(sent_d)} hole(s) back {_trial_why()}"
                else:
                    # (some back: this one-ring mesh held to fall back on, and the face meshed again)
                    passes[-1][0] = f"rings under the shortest edge: {len(sent_d)} hole(s) back"
                    ul_state["stash_base"] = (mesh, applied)
                    no_cut = set(no_cut) - keys_d
                    one_ring_at.extend(sent_d)
                    unmapped, compare, kept, before_guard = 0, {}, set(), None
                    brep, reseated, redrawn, redraw_pass, asked, seated_now = unseated, 0, [], redraw_off, [], []
                    continue
            if ul_state.get("want") and not _trial_ok(applied.get("_past_n", 0), requirements):
                # SHMESH-31f: the one-ring trial is not started - the solver-limit pass is owed first
                ul_state.pop("want")
                passes.append([f"rings under the shortest edge: {_trial_why()}", 0.0])
            if ul_state.get("want"):
                # SHMESH-51o59: faces with rings under the shortest edge not yet set against their one-ring mesh - this
                # mesh is kept, and ONE mesh with all of them on one ring follows
                want_ = ul_state.pop("want")
                ul_state["stash"] = (mesh, applied)
                ul_state.setdefault("waiting", set()).update(want_)
                no_cut = set(no_cut) | {-(f_ + UNDER_LOW_KEY) - 1 for f_ in want_}
                passes.append(["rings under the shortest edge: their one-ring mesh wanted", 0.0])
                unmapped, compare, kept, before_guard = 0, {}, set(), None
                brep, reseated, redrawn, redraw_pass, asked, seated_now = unseated, 0, [], redraw_off, [], []
                continue
            wanted = applied.pop("_redraw_wanted", [])
            seam_wanted = applied.pop("_seam_wanted", [])
            w_owner = applied.pop("_redraw_owners", [])
            s_owner = applied.pop("_seam_owners", [])
            if (wanted or seam_wanted) and not redraw_pass and not resplined:
                if redraw(wanted, seam_wanted, w_owner, s_owner):
                    passes[-1][0] = "holes drawn again"
                    continue
            past = applied.pop("_past_spots", [])
            past_n = applied.pop("_past_n", 0)
            left = applied.pop("_spots_left", 0)
            applied.pop("_spots_trimmed", None)
            own: list = []
            spot_left = spot_trimmed = 0
            spot_stopped = spot_late = False
            first_n = float(mesh.n_elements)            # (SHMESH-31b: the runaway guard against the first mesh)
            readd, went_h0 = True, False
            reserve, deferred = 0.0, False
            if past and requirements and OWN_SIZE_RETRIES:
                # SHMESH-21d: the first path keeps back the time of the first meshes owed after it; while the run
                # chooses first, the spot passes wait for the mesh kept
                reserve = _choose_reserve(applied)
                deferred = bool(_CHOOSE.get("defer"))
            # (SHMESH-36's #435 steps have no spot pass, OWN_SIZE_RETRIES 0: the steps add none to them either)
            for step_ in range(1, 1 + (OWN_SIZE_RETRIES + (SPOT_STEP_PASSES if SPOT_STEPS and OWN_SIZE_RETRIES else 0))
                               if requirements else 0):
                # SHMESH-31: elements past the solver's own limits under the user's sizes: round each of them the
                # mesh is made again at our rules' own size for its face (finer), graded back to the user's; the
                # mesh with fewer past is kept
                if not past or deferred:
                    break
                if spots_dropped and SPOT_NOT_AGAIN:
                    # SHMESH-31i: a spot pass this path dropped is not run again in its bolt-ring trial
                    passes.append(["finer at spots: not again, dropped once in this path (SHMESH-31i)", 0.0])
                    break
                # (SHMESH-31c: each spot carries its step - the spots found now take this pass's, the earlier keep theirs)
                # (SHMESH-31e: a box whose reach holds an element still past grows to it; the new spots join tight)
                if readd:
                    own = _grown(own, past) + [tuple(b[:3]) + (step_ if SPOT_STEPS else 0,
                                                               float(b[3]) if len(b) > 3 else 0.0, False) for b in past]
                readd = True
                t_pass = time.monotonic()
                # SHMESH-31b: the plan kept to SPOT_PLAN_MULT times the mesh it re-makes, and the pass to the time
                # left before the deadline less what must follow (past it, the best mesh so far is kept); a pass that
                # cannot finish in it (priced at the first mesh's own time) is not started
                budget = SPOT_PLAN_MULT * first_n
                # (SHMESH-31h: priced at the first mesh, or the longest spot pass measured; SHMESH-21d: with the
                # first meshes owed after this path kept back)
                if not _affords(_spot_price() + reserve):
                    if reserve and _affords(reserve):
                        # SHMESH-21d: the meshes to compare still fit, the spot pass with them does not: choose first
                        _choose_start(f"the spot pass ({_spot_price():.0f} s) and the {_CHOOSE.get('owed')} "
                                      f"mesh(es) to compare ({reserve:.0f} s) would not fit in the time left")
                        deferred = True
                        passes.append(["finer at spots: waits, the meshes to compare first (SHMESH-21d)", 0.0])
                        break
                    spot_late = True
                    break
                lim_s = _limit()
                try:
                    again, applied2 = _bounded(functools.partial(
                        _mesh_once, Path(brep), record, owners, float(mm_per_unit), None,
                        hole_before=tuple((tuple(c), e) for c, e in redrawn), target=target, correction=correction,
                        requirements=requirements, own_spots=tuple(own), unmap=frozenset(unmap),
                        no_cut=frozenset(no_cut), check_maps=False, one_ring_at=tuple(one_ring_at),
                        spot_budget=budget), lim_s - reserve if (lim_s is not None and reserve) else lim_s)
                    passes.append(["finer at spots past the solver's limits", round(time.monotonic() - t_pass, 1)])
                    _SPOT_COST["s"] = max(float(_SPOT_COST.get("s") or 0.0), time.monotonic() - t_pass)
                except (ValueError, _Periodic) as exc:
                    logger.info("shell mesh: the spots past the solver's limits were not meshed again (%s)", exc)
                    spot_stopped = "stopped" in str(exc)
                    if spot_stopped:
                        _SPOT_COST["s"] = max(float(_SPOT_COST.get("s") or 0.0), time.monotonic() - t_pass)
                    if spot_stopped and reserve:
                        # SHMESH-21d: stopped where the meshes to compare begin - no cut: the run chooses first
                        spot_stopped = False
                        _choose_start(f"the spot pass ran past {time.monotonic() - t_pass:.0f} s, the time left less "
                                      f"the {_CHOOSE.get('owed')} mesh(es) to compare ({reserve:.0f} s)")
                        deferred = True
                        passes.append(["finer at spots: stopped, the meshes to compare first (SHMESH-21d)",
                                       round(time.monotonic() - t_pass, 1)])
                    elif spot_stopped:
                        _RUN["cut"] = True              # (SHMESH-31b: then no whole mesh more is started)
                        passes.append(["finer at spots: stopped at the deadline", round(time.monotonic() - t_pass, 1)])
                    break
                for key_ in ("_redraw_wanted", "_seam_wanted", "_redraw_owners", "_seam_owners"):
                    applied2.pop(key_, None)
                left2 = applied2.pop("_spots_left", 0)
                trimmed2 = applied2.pop("_spots_trimmed", 0)
                past2 = applied2.pop("_past_spots", [])
                n2 = applied2.pop("_past_n", 0)
                if not _finer_kept(applied2, n2, applied, past_n):
                    if SPOT_STEPS and not went_h0:
                        # SHMESH-31c: a step that cleared nothing: every spot goes to the face's own size, as before
                        own = [tuple(b[:3]) + (0,) + tuple(b[4:]) for b in own]
                        readd, went_h0 = False, True
                        continue
                    if step_ == 1:
                        spots_dropped = True            # (SHMESH-31i: nothing this spot pass made was kept)
                    break
                # SHMESH-49g: a spot re-meshed across a rings-and-strips plate must not put it past POLAR_TRIAS
                # triangles (the plate was judged on the mesh before: the record and the check say the same)
                if any(v > ST.POLAR_TRIAS and v > applied.get("_polar_trias", {}).get(k, 0)
                       for k, v in applied2.get("_polar_trias", {}).items()):
                    break
                mesh, applied, past, past_n = again, applied2, past2, n2
                applied["quality"]["own_size_spots"] = len(own) - trimmed2
                spot_left, spot_trimmed = left2, trimmed2
            if deferred and past:
                applied["quality"]["own_size_deferred"] = True     # (SHMESH-21d: repaired last, if it is kept)
            if applied["quality"].get("own_size_spots"):
                mesh.notes.append(f"In {applied['quality']['own_size_spots']} spot(s) the mesh is finer than the size "
                                  "you asked for: at your size some elements there were past the solver's own limits "
                                  "(SHMESH-31).")
            # SHMESH-31b: what the spot plan left to the clean-up, told
            applied["quality"]["own_size_left"] = int(left + spot_left + spot_trimmed)
            applied["quality"]["own_size_stopped"] = bool(spot_stopped or spot_late)
            if spot_stopped or spot_late:
                mesh.notes.append("Meshing finer at the spots past the solver's own limits would have run past the "
                                  f"shell mesh's {_minutes(float(_CLOCK.get('allowed') or MESH_DEADLINE_S))}, so it was "
                                  + ("stopped" if spot_stopped else "not started")
                                  + " and the best mesh made before it is delivered (SHMESH-31b).")
            elif left + spot_left + spot_trimmed:
                mesh.notes.append(f"{left + spot_left + spot_trimmed} spot(s) past the solver's own limits were not "
                                  "meshed finer (the plan would have been too large); the clean-up took them as they "
                                  "were (SHMESH-31b).")
            applied["quality"]["resplined_faces"] = resplined
            applied["quality"]["resplined_wires_fixed"] = len(wires_fixed)  # (SHMESH-11i: fixed on their re-make)
            # SHMESH-48l: every whole mesh this path made, in order (what followed it; its seconds apart, in passes_s,
            # so a record without timings stays the same from run to run)
            applied["quality"]["passes"] = [str(x[0]) for x in passes]
            applied["quality"]["passes_s"] = [x[1] for x in passes]
            applied["quality"].setdefault("structure", {})["grids_freed"] = len(unmap)
            applied["quality"]["structure"]["holes_redrawn"] = reseated
            # SHMESH-48f/49g: every face a check refused (its grid or its rings and strips), why, with its numbers
            for k_c, v_c in compare.items():
                if k_c in refused and "free_figures" in v_c:
                    refused[k_c]["free_figures"] = v_c["free_figures"]
            if regrid_log:
                applied["quality"]["structure"]["grids_back"] = list(regrid_log)
            if refusal_log:
                applied["quality"]["structure"]["refusals_by_pass"] = list(refusal_log[-REFUSAL_LOG_MAX:])
            applied["quality"]["structure"]["refused"] = [
                {"face": _owner_of_key(k, owners), **v} for k, v in sorted(refused.items())]
            # SHMESH-49g: each round plate meshed in rings and strips, with its triangles in the mesh delivered
            tri_at = applied.pop("_polar_tri_at", {})
            was_judged = applied.pop("_polar_judged", None)
            applied["quality"]["structure"]["rings_and_strips"] = [
                {"face": str(k), "triangles": int(n), "triangles_at_mm": tri_at.get(k, []),
                 "judged_by_the_check": was_judged}
                for k, n in sorted(applied.pop("_polar_trias", {}).items())]
            if split_note:
                mesh.notes.append(f"{split_note[0].upper()}{split_note[1:]} (SHMESH-11d).")
            if resplined:
                mesh.notes.append(f"{resplined} face(s) gmsh could not mesh (a periodic surface, or a boundary it "
                                  "could not recover) were re-made as B-splines (the same surface) and meshed "
                                  "(SHMESH-11).")
            if guard_best is not None:
                # SHMESH-51o: the mesh with one ring on the trial's faces against the one before it - fewer triangles,
                # no worse on the limits and the grids, wins; else the run goes on from the one before (its state)
                b_mesh, b_applied, b_n = guard_best
                guard_best = None
                q_a, q_b = len(b_mesh.quads), len(mesh.quads)
                t_a = Counter(str(m) for m in b_mesh.mid[q_a:])
                t_b = Counter(str(m) for m in mesh.mid[q_b:])
                mids = sorted({str(owners[k]) for k in guard_faces[-1]})
                # SHMESH-51o5: a face takes one ring only where its OWN triangles drop with it; a trial is kept only
                # when every face it switches gains so (the real column's second trial, one ring round every other bolt
                # hole, took 8 triangles off the run and none off its own faces - 0 to 0, 10 to 10 - and cost a 12-hole
                # plate its two rings); where some gain and some do not, those that gain are tried alone (once, while
                # time allows); the triangles that went elsewhere are told (`others`), never taken as the face's
                gain, own_ok = _own_gain(t_a, t_b, mids, Counter(str(m) for m in b_mesh.mid),
                                         _tria_share_asked(requirements))
                won = own_ok and len(mesh.trias) < len(b_mesh.trias) and _not_worse(applied, past_n, b_applied, b_n)
                why_not = ("" if won else "no face of it gains on its own" if not gain else
                           "not every face of it gains on its own" if not own_ok else
                           "no fewer triangles" if len(mesh.trias) >= len(b_mesh.trias) else
                           _worse_why(applied, past_n, b_applied, b_n))
                others = {m_: [int(t_a.get(m_, 0)), int(t_b.get(m_, 0))] for m_ in sorted(set(t_a) | set(t_b))
                          if m_ not in mids and t_a.get(m_, 0) != t_b.get(m_, 0)}
                guard_log.append({"one_ring_faces": guard_faces[-1], "triangles_two": int(len(b_mesh.trias)),
                                  "triangles_one": int(len(mesh.trias)),
                                  "per_face": {m_: [int(t_a.get(m_, 0)), int(t_b.get(m_, 0))] for m_ in mids},
                                  "others": others, "why_not": why_not,
                                  "kept": "one ring" if won else ("two rings: no face of it gains on its own"
                                                                   if not gain else "two rings")})
                part = [k for k in guard_faces[-1] if str(owners[k]) in gain]
                if (GUARD_OWN_GAIN and gain and not own_ok and not guard_part_tried and guard_snap is not None
                        and (MESH_DEADLINE_S <= 0 or time.monotonic() - started < GUARD_TIME * MESH_DEADLINE_S)):
                    # (only the faces that gained: from the mesh before the trial)
                    guard_part_tried = True
                    guard_log[-1]["kept"] = "only the faces that gained, tried alone"
                    guard_best = (b_mesh, b_applied, b_n)
                    sn = guard_snap
                    guard_faces[-1] = part
                    passes[-1][0] = "one ring better on some faces only: one ring there (start again)"
                    keys_g = {-(k + BOLT_GRID_KEY) - 1 for k in part}
                    no_cut, refused, compare, kept, unmap = _guard_carry(sn, part, keys_g, body_of)
                    refused.update({k: {"kind": "bolt rings", "why": "its own triangles fewer with one ring",
                                        "then": "one ring on this face"} for k in keys_g})
                    unmapped, before_guard = 0, None
                    if GUARD_CARRY:
                        brep, reseated, redrawn, redraw_pass, asked, seated_now = sn["seat"]
                    else:
                        brep, reseated, redrawn, redraw_pass, asked, seated_now = (unseated, 0, [], redraw_off, [],
                                                                                   [])
                    continue
                if not won:
                    mesh, applied, past_n = b_mesh, b_applied, b_n
                    sn = guard_snap or {}
                    no_cut, refused, unmap = set(sn["no_cut"]), dict(sn["refused"]), set(sn["unmap"])
                    compare, kept = {k_: dict(v_) for k_, v_ in sn["compare"].items()}, set(sn["kept"])
                    brep, reseated, redrawn, redraw_pass, asked, seated_now = sn["seat"]
                applied["quality"]["passes"] = [str(x[0]) for x in passes]
                applied["quality"]["passes_s"] = [x[1] for x in passes]
                applied["quality"]["structure"]["bolt_guard"] = list(guard_log)
                if won:
                    mesh.notes.append(f"One ring round the bolt holes of {len(guard_faces[-1])} face(s): with two "
                                      f"rings the mesh had {guard_log[-1]['triangles_two']} triangle(s), with one "
                                      f"{guard_log[-1]['triangles_one']} (SHMESH-51o).")
            trial_may = (BOLT_TRIA_GUARD and ST.BOLTS and not resplined and len(guard_faces) < 2
                         and (MESH_DEADLINE_S <= 0 or time.monotonic() - started < GUARD_START * MESH_DEADLINE_S))
            if trial_may and (deferred or _choose_trial_waits(past)):
                # SHMESH-21d: not while the run chooses first, nor where it would take the time kept back for the
                # meshes to compare - the trial waits with the spot passes, for the mesh kept
                trial_may = False
                applied["quality"].setdefault("structure", {})["bolt_trial_waited"] = "SHMESH-21d"
            if trial_may:
                # SHMESH-51o: two rings never cost triangles one ring a hole would not have. SHMESH-51o4: first the
                # face with the most triangles of its own among those with two-ring holes, alone (another face's
                # triangles may be its doing: a 12-hole plate's bands were made and its 12 triangles went when the
                # long plate beside it took one ring); then, while time allows and triangles are left anywhere, one
                # ring round every bolt hole - the mesh never ends with more triangles than that one, when it is tried
                faces_g: list[int] = []
                for every in ((False, True) if not guard_faces and not GUARD_OWN_GAIN else
                              (False,) if GUARD_OWN_GAIN else (True,)):
                    own_t_g = Counter(str(m) for m in mesh.mid[len(mesh.quads):])
                    own_e_g = Counter(str(m) for m in mesh.mid)
                    sh_g = _tria_share_asked(requirements)
                    sh_g = GUARD_TRIA_SHARE if sh_g is None else float(sh_g)
                    faces_g = [k for k in _tria_faces(mesh, applied, owners, everywhere=every)
                               if -(k + BOLT_GRID_KEY) - 1 not in no_cut
                               and not (GUARD_OWN_GAIN and any(k in f_ for f_ in guard_faces))
                               # (SHMESH-51o30: no trial for a face whose triangles are within its share)
                               and (not GUARD_BY_SHARE or own_t_g.get(str(owners[k]), 0)
                                    > max(GUARD_TRIA_FLOOR, sh_g * own_e_g.get(str(owners[k]), 0)))]
                    if faces_g:
                        break
                if faces_g and GUARD_PER_FACE and not guard_faces:
                    faces_g = _first_trial(mesh, owners, faces_g, applied)
                if faces_g:
                    guard_best = (mesh, applied, past_n)
                    guard_snap = {"no_cut": set(no_cut), "refused": dict(refused), "unmap": set(unmap),
                                  "compare": {k_: dict(v_) for k_, v_ in compare.items()}, "kept": set(kept),
                                  "seat": (brep, reseated, list(redrawn), redraw_pass, list(asked), list(seated_now))}
                    guard_faces.append(faces_g)
                    passes[-1][0] = "two rings left triangles: one ring on their faces (start again)"
                    keys_g = {-(k + BOLT_GRID_KEY) - 1 for k in faces_g}
                    no_cut, refused, compare, kept, unmap = _guard_carry(guard_snap, faces_g, keys_g, body_of)
                    refused.update({k: {"kind": "bolt rings", "why": "its two rings left triangles",
                                        "then": "one ring tried, the mesh with fewer triangles kept"} for k in keys_g})
                    unmapped, before_guard = 0, None
                    if not GUARD_CARRY:
                        brep, reseated, redrawn, redraw_pass, asked, seated_now = (unseated, 0, [], redraw_off, [],
                                                                                   [])
                    continue
            return mesh, applied, past_n


def _standing(applied: dict, past_n: int, other: dict | None = None,
              requirements: dict | None = None) -> tuple[int, ...]:
    """SHMESH-36: how a mesh under the user's limits stands, lower is better: (elements past the solver's own
    limits, the lines of the user's on which it has an element past the outer limit and `other` has none, elements
    past the user's outer limits, minus the lines met, the lines on which both are past the outer limit and its
    worst is further past). Without `other` the second and last figures are 0.
    SHMESH-51o61: how far one element is past comes last, after how many are past and how many lines are met: on
    lab_fr004 #435's mesh (3824 elements) won for its worst triangle on the 30 degree line being less far past (both
    past that line), though it had more elements past an outer limit (4 against 3) and missed the quad corner lines
    this mesh met (6 lines met against 7)."""
    rows = (applied.get("requirements") or {}).get("rows") or []
    limit_rows = [r for r in rows if str(r.get("field", "")).startswith("limit:")]
    beyond = sum(int(r.get("beyond") or 0) for r in limit_rows)
    met = sum(1 for r in rows if r.get("met") is True)
    worse = 0
    further_n = 0
    if other is not None:
        theirs = {r.get("id"): r for r in ((other.get("requirements") or {}).get("rows") or [])}
        ops = {lim.get("id"): str(lim.get("op")) for lim in (requirements or {}).get("limits") or []}
        for r in limit_rows:
            if not int(r.get("beyond") or 0):
                continue
            o = theirs.get(r.get("id")) or {}
            if not int(o.get("beyond") or 0):
                worse += 1
            elif r.get("worst") is not None and o.get("worst") is not None:
                further = (float(r["worst"]) < float(o["worst"])) if ops.get(r.get("id")) == ">=" else (
                    float(r["worst"]) > float(o["worst"]))
                if STANDING_BY_LINES:
                    further_n += int(further)
                else:
                    worse += int(further)
    if STANDING_BY_LINES:
        return int(past_n), worse, beyond, -met, further_n
    return int(past_n), worse, beyond, -met


@contextlib.contextmanager
def _classic():
    """SHMESH-36: #435's steps exactly, for one mesh: every switch this PR added is put at #435's value - no sizes
    beside small pieces of the CAD (27), no mend (28), no sliding on straight CAD edges (14d), no sheet regions (37),
    no local re-mesh (31), gmsh's own fallback (32: the earlier head kept 32 on, and on a part where gmsh falls back
    that alone gave a different mesh), gmsh's 24 a turn under a count (33), 4 a turn round holes (33), no chord hold
    or report (34); the comparison itself off. The constants are put back after (a shell mesh runs one at a time in
    its process). #437's local fuse-back (SHMESH-30, LOCAL_REBUILD) is left as it is: it is not one of this PR's
    switches, so with #437 in the tree the mesh compared is #435's steps plus #437's - exactly the mesh the tree
    makes without SHMESH-27..29 and 31..37."""
    g = globals()
    names = ("FEATURE_SIZES", "MEND", "SLIDE_EDGES", "SHEET_REGIONS", "SHEET_REGIONS_ALL", "OWN_SIZE_RETRIES",
             "ALGO_SWITCH", "COUNT_CURVATURE", "CHORD_REPORT", "CLASSIC_CHECK", "FORCE_CLASSIC")
    saved = {n: g[n] for n in names}
    saved_sr = (SR.HOLE_PER_TURN, SR.CHORD_SHARE)
    saved_st = (ST.WASHERS, ST.MAPPED, ST.BLOCKS, PLANE_ALGORITHM)
    try:
        # SHMESH-39..42 are not #435's steps either: the candidate is the mesh without them
        ST.WASHERS = ST.MAPPED = ST.BLOCKS = False
        g["PLANE_ALGORITHM"] = None
        g.update(FEATURE_SIZES=False, MEND=False, SLIDE_EDGES=False, SHEET_REGIONS=False, SHEET_REGIONS_ALL=False,
                 OWN_SIZE_RETRIES=0, ALGO_SWITCH=None, COUNT_CURVATURE=False, CHORD_REPORT=False,
                 CLASSIC_CHECK=False, FORCE_CLASSIC=False)
        SR.HOLE_PER_TURN, SR.CHORD_SHARE = SR.SHAPE_PER_TURN, math.inf
        yield
    finally:
        g.update(saved)
        SR.HOLE_PER_TURN, SR.CHORD_SHARE = saved_sr
        ST.WASHERS, ST.MAPPED, ST.BLOCKS, g["PLANE_ALGORITHM"] = saved_st


def _face_figures(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, q_face, t_face, k: int,
                  low_mm: float | None) -> dict:
    """SHMESH-48x: a sewn face's elements, triangles and elements with an edge under the user's shortest edge."""
    qk, tk = np.asarray(q_face, dtype=np.int64), np.asarray(t_face, dtype=np.int64)
    n_short = 0
    for els, fk in ((Q, qk), (T, tk)):
        mine = els[fk == k] if len(els) else els
        if low_mm is not None and len(mine):
            e_min = np.linalg.norm(xyz[mine] - xyz[np.roll(mine, -1, axis=1)], axis=2).min(axis=1)
            n_short += int((e_min < float(low_mm)).sum())
    return {"elements": int((qk == k).sum() + (tk == k).sum()), "triangles": int((tk == k).sum()), "short": n_short}


def _mesh_once(*args, **kwargs) -> tuple[ShellMesh, dict]:
    """One mesh (_mesh_once_core, every argument passed on as given). SHMESH-60..69: the stamped-sheet steps are on
    for surface files (a stamped sheet comes as its skin); on a mid-surface extracted from a solid they are off - the
    mesh is the rules' own, as before (STAMPED_ALL). Forwarded whole, never argument by argument: a parameter another
    step adds to _mesh_once_core reaches it (a named list here once dropped the bolt rings' ul_state)."""
    record = kwargs["record"] if "record" in kwargs else args[1]
    if not STAMPED_ALL and record.get("source") != "surface" and not _STAMPED_OFF_NOW[0]:
        with _stamped_off():
            return _mesh_once_core(*args, **kwargs)
    return _mesh_once_core(*args, **kwargs)


def _mesh_once_core(brep: Path, record: dict, owners: list[str], mm: float, log: list | None, *,
               target: dict | None = None, correction: float = 1.0,
               requirements: dict | None = None, own_spots: tuple = (), unmap: frozenset = frozenset(),
               hole_before: tuple = (), no_cut: frozenset = frozenset(), judge_faces: bool = False,
               check_maps: bool = False, compare: dict | None = None, grids_kept: frozenset = frozenset(),
               redraw_now=None, one_ring_at: tuple = (), hole_rounds: int | dict = 0,
               gridded_ever: set | None = None, own_bolt: frozenset = frozenset(),
               ul_state: dict | None = None, spot_budget: float | None = None) -> tuple[ShellMesh, dict]:
    """One mesh. `unmap` (SHMESH-40): gmsh surfaces left free (their grid came out past the solver's limits once);
    `check_maps`: raise _Unmap when a mapped face's grid comes out past them. `own_spots` (SHMESH-31): (sewn face, box low corner, box high corner - mm) round elements a mesh
    under the user's sizes left past the solver's own limits: inside each box the face's own rules' size, where
    finer than the user's, graded back out. `spot_budget` (SHMESH-31b): the elements the boxes may plan, worst first."""
    _YIELDED[0] = 0
    _STRIP_LOG.clear()
    _FREE_WHY.clear()
    _FIT_MAPPED[0] = 0
    _FIT_FACES.clear()
    _FIT_NEAR.clear()
    _STEP_NEAR.clear()
    _CORNER_MERGED[0] = 0
    _TRI_PLAN.clear()
    import gmsh

    spots_trimmed = 0

    facts = _faces_and_facts(Path(brep), mm)
    if len(owners) != len(facts):
        raise ValueError(f"the mid-surface has {len(facts)} faces but the record names {len(owners)}")
    rows = []
    widths = [min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"] for f in facts]
    sheet = None
    if SHEET_REGIONS_ALL or (SHEET_REGIONS and record.get("source") == "surface"):
        # SHMESH-37: a sheet drawn as many patches is sized by the sheet
        sheet = _sheet_widths(Path(brep), facts, owners, record, mm)
        widths = [max(w, s) for w, s in zip(widths, sheet["widths"], strict=True)]
    for k, f in enumerate(facts):
        rows.append({"face": k + 1, **plan_rules.face_plan(record, owners[k], width=widths[k], radii=f["radii"],
                                                         bend=f["bend"])})
        if sheet is not None and sheet["size"][k] > 1:
            rows[-1]["sheet_mm"] = float(f"{sheet['widths'][k]:.6g}")
    walls = [r["wall_mm"] for r in rows]
    # SHMESH-20: a plan far past what a shell mesh is made in within the time allowed stops before gmsh starts, and
    # says so (a ship's 3 m slotted hull, sh0048: about 3 million planned, over an hour of gmsh)
    planned = sum(f["area"] * mm * mm / r["size_mm"] ** 2 for f, r in zip(facts, rows, strict=True))
    # SHMESH-23: THE USER'S ELEMENT COUNT scales every size by one factor (their count beats our minimums: said, never
    # refused), aimed on the built-over-planned a first mesh measured
    counted, sz = _count_plan(rows, planned, target, correction)
    if counted is not None:
        planned = counted["planned"]
    # SHMESH-24..26: THE USER'S REQUIREMENTS from their notes (engines/shell_requirements). Their sizes replace the
    # plan's (their size beats our minimums: said, never refused; only a plan too big to build is stepped to the
    # nearest that builds), their limits join the limits in force, their element kind is made. None: nothing here
    # changes a number - `lim` is shell_quality.IN_FORCE, `sizing` and `kind` are None
    lim = SR.limits_in_force(requirements)
    _AIMS_NOW[0] = tuple(lim.aims or ())
    kind = SR.kind_of(requirements)
    # SHMESH-43: the user's "free mesh" / "no washers" (NOTE-7's element kind) turns the structured steps off
    structure = SR.structure_of(requirements)
    rule_size = {b[0]: min(rows[b[0]]["size_mm"], rows[b[0]].get("curve_mm", rows[b[0]]["size_mm"])) for b in own_spots}
    sizing = SR.size_plan(rows, requirements, [f["area"] * mm * mm for f in facts], planned_max=PLANNED_MAX,
                          curved=[(f["bend"], min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"])
                                  for f in facts])
    if sizing is not None:
        planned = sizing["planned"]
    # SHMESH-52: A SIZE IN ONE NAMED PLACE (a part, an area inside one) on the faces the parts reading ties it to,
    # through the registry's shell lane (NOTE-13). Nothing asked in one place: nothing here runs
    places = None
    if requirements and requirements.get("places"):
        places = _place_sizes(rows, owners, record, requirements, facts, mm)
    if places is not None and places.get("applied"):
        planned = places["planned"]
        if (sizing is not None and sizing.get("low_mm") is not None and places.get("finest_mm")
                and ((requirements or {}).get("sizes") or {}).get("min_edge_mm") is None):
            # SHMESH-52: the low end of a global size's band (half of it) is no shortest edge the user typed: a place
            # asked finer is meshed at what was asked, never held at the band's low end (a picked face asked 2 mm
            # under a global 8 mm came out 4.08 mm, NOTE-17)
            sizing["low_mm"] = min(float(sizing["low_mm"]), 0.5 * float(places["finest_mm"]))
    # SHMESH-27 (the user's sizes only): a face narrower than their size is sized to its own width
    # (SHMESH-51o65: the faces whose stretched size went back keep today's sizing)
    along_skip = frozenset(k for k, o in enumerate(owners) if o in NARROW_SKIP)
    bent = [f["bend"] is not None and 0 < f["bend"] < plan_rules.PANEL_WALLS * r["wall_mm"]
            for f, r in zip(facts, rows, strict=True)]
    narrow = (_narrow_faces(rows, facts, mm, planned, _interior_faces(Path(brep), bent) if FLANKS else None,
                            slivers={b[0] for b in own_spots},
                            low_mm=(sizing or {}).get("low_mm") if FLANK_LOW else None,
                            along=_along(sizing, record), along_skip=along_skip)
              if sizing is not None and FEATURE_SIZES else None)
    if narrow is not None:
        planned = narrow["planned"]
    for k_l, f_l in enumerate(facts):
        rung = _fate_rung(f_l)
        if rung in LADDER_SIZE:
            # SHMESH-71: a face that held gmsh's 2D mesh, a rung down its ladder: meshed at another size
            rows[k_l]["size_mm"] = float(rows[k_l]["size_mm"]) * LADDER_SIZE[rung]
            if "curve_mm" in rows[k_l]:
                rows[k_l]["curve_mm"] = float(rows[k_l]["curve_mm"]) * LADDER_SIZE[rung]
    finest = (sizing or {}).get("floor_mm")
    growth = float((sizing or {}).get("growth") or plan_rules.GROWTH)
    if planned > PLANNED_MAX:
        raise ValueError(f"the shell mesh plan asks for about {planned / 1e6:.1f} million elements over {len(facts)} "
                         f"faces, past the {PLANNED_MAX / 1e6:.1f} million a shell mesh is made of in the time allowed "
                         "(SHMESH-20)")
    _reprice(planned=planned)               # (SHMESH-21e: the limit follows the plan)
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Geometry.OCCParallel", 0)
        gmsh.option.setNumber("Mesh.Algorithm", ALGORITHM)
        gmsh.option.setNumber("Mesh.RecombineAll", 0 if kind == "triangles" else 1)
        gmsh.option.setNumber("Mesh.RecombinationAlgorithm", RECOMBINATION)
        # (with the user's own sizes, SHMESH-24, every curved face and arc is sized by the plan itself, between
        # the user's shortest edge and the shape it must keep: gmsh's own curvature sizing would refine past both)
        # SHMESH-33: a count the user asked for that makes the sizes coarser than ours (SHMESH-23, sz > 1) coarsens
        # the elements round every curve too, by the same factor, never under SHAPE_PER_TURN a turn: gmsh's own
        # 24 a turn held every 12 mm hole and every curved panel at our size (a bracket asked 221 elements got 503,
        # a curved panel asked 1,263 got 21,256)
        per_turn = (PER_TURN if sz <= 1.0 or not COUNT_CURVATURE
                    else max(SR.HOLE_PER_TURN, int(round(PER_TURN / sz))))
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", per_turn if finest is None else 0)
        # SHMESH-6 is held by the grading fields below; gmsh's own spread of edge sizes into a face carried a
        # narrow neighbour's size over the whole of a wide face (one 2.5 mm strip: 800,000 elements)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        size_floor = plan_rules.FLOOR_WALLS * min(walls) * min(sz, 1.0)
        if places is not None and places.get("finest_mm"):
            # SHMESH-52: a place asked finer than half its wall is meshed at what was asked (said in its row)
            size_floor = min(size_floor, 0.5 * float(places["finest_mm"]))
        gmsh.option.setNumber("Mesh.MeshSizeMin", size_floor / mm)
        gmsh.option.setNumber("Mesh.MeshSizeMax", max(r["size_mm"] for r in rows) / mm)
        gmsh.option.setNumber("Mesh.Smoothing", 5)
        gmsh.option.setNumber("Mesh.LcIntegrationPrecision", LC_PRECISION)
        if ALGO_SWITCH is not None:
            gmsh.option.setNumber("Mesh.AlgorithmSwitchOnFailure", ALGO_SWITCH)
        gmsh.model.add("shell")
        gmsh.model.occ.importShapes(str(brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        surfaces = [t for _d, t in gmsh.model.getEntities(2)]
        if len(surfaces) != len(facts):
            raise ValueError(f"gmsh read {len(surfaces)} faces of the mid-surface's {len(facts)}")
        # each gmsh surface to its sewn face, by centroid and area (never by trusting the order); faces alike in
        # both (a cone's apex slivers of 1e-6 mm2, all at one point) go in order, each sewn face once
        cents = np.array([f["centroid"] for f in facts])
        areas = np.array([f["area"] for f in facts])
        scale = 1e-9 * max(1.0, float(np.abs(cents).max()) if len(cents) else 1.0)
        tag_of: dict[int, int] = {}
        free = np.ones(len(facts), dtype=bool)
        for t in sorted(surfaces):
            c = np.array(gmsh.model.occ.getCenterOfMass(2, t))
            a = float(gmsh.model.occ.getMass(2, t))
            dist = np.where(free, np.linalg.norm(cents - c, axis=1), np.inf)
            best = float(dist.min())
            if not np.isfinite(best):
                raise ValueError("gmsh read more faces than the mid-surface has")
            near = np.flatnonzero(dist <= best + scale)
            k = int(near[np.argmin(np.abs(areas[near] - a))])
            free[k] = False
            tag_of[t] = k
        # SHMESH-12: a sewn face smaller than a square a fifth of the floor across (a cone's apex slivers of 1e-6
        # mm2) is no wall: it is taken out before meshing (gmsh stopped at one, "the 1D mesh seems not to be
        # forming a closed loop"); its edges stay, so its neighbours mesh up to them
        tiny = sorted(t for t, k in tag_of.items()
                      if facts[k]["area"] * mm * mm < (shell_mesh.SHORT_SHARE * plan_rules.FLOOR_WALLS
                                                       * rows[k]["wall_mm"]) ** 2)
        # SHMESH-71/72: the faces that stopped gmsh past their last chance are left out (their edges stay)
        left_out = sorted(t for t, k in tag_of.items() if t not in tiny and _fate_out(facts[k]))
        left_areas = [float(facts[tag_of[t]]["area"]) * mm * mm for t in left_out]
        left_kinds = [_kind_words(gmsh.model.getType(2, t)) for t in left_out]
        which = ", ".join(f"{a_:.0f} mm2 ({k_})" for a_, k_ in sorted(zip(left_areas, left_kinds), reverse=True)[:6])
        if left_out and sum(left_areas) > FACE_OUT_GIVE_UP * float(sum(f_["area"] for f_ in facts)) * mm * mm:
            raise ValueError(f"gmsh could not mesh {len(left_out)} face(s) - {which} - more than "
                             f"{100 * FACE_OUT_GIVE_UP:.0f}% of the part; the shell mesh is given up. Those faces of the "
                             "CAD are the ones to look at (SHMESH-72)")
        if tag_of and len(tiny) == len(tag_of):
            # SHMESH-12b: EVERY face is under the smallest face the asked size meshes - the size is larger than the
            # part (a 2 mm part asked 20 mm elements): said in plain words with the way on, never "max() arg is an
            # empty sequence" from the sizing below
            raise ValueError(SAID + too_small_words(gmsh.model.getBoundingBox(-1, -1), mm,
                                                    max(float(rows[k]["wall_mm"]) for k in tag_of.values())))
        if tiny or left_out:
            gmsh.model.occ.remove([(2, t) for t in tiny + left_out])
            gmsh.model.occ.synchronize()
            for t in tiny + left_out:
                del tag_of[t]
        slivers = len(tiny)
        rungs = {t: _fate_rung(facts[k]) for t, k in tag_of.items()}
        fine_curves = (_curves_finer(gmsh, mm, {t: float(rows[k]["size_mm"]) for t, k in tag_of.items()})
                       if _FACE_FATE["curves"] else {})
        for t, rung in sorted(rungs.items()):
            if rung == "bspline" and "spline" not in gmsh.model.getType(2, t).lower():
                # SHMESH-71: the ladder's B-spline rung - the face re-made (SHMESH-11's path)
                raise _Periodic(tag_of[t])
        # SHMESH-39: WASHERS - a ring of quads round each round hole of a flat face (engines/gmsh/shell_structured.py),
        # drawn on the face before anything is sized; off under the user's "free mesh" / "no washers"
        mesh_min = plan_rules.FLOOR_WALLS * min(walls) * min(sz, 1.0)

        def hole_size(r_mm: float, k: int) -> float:
            # the element length the plan puts round a hole of radius r_mm (as the arc sizes below do)
            h0, _rule = plan_rules.curve_size(r_mm, wall=rows[k]["wall_mm"])
            h0 *= sz
            if finest is not None and sizing is not None:
                h0 = SR.curved_size(r_mm, 0.0, floor=finest, rule=(
                    rows[k]["size_mm"] if sizing.get("global_mm") is not None else h0), per_turn=SR.HOLE_PER_TURN)
            elif "place" in rows[k]:
                # SHMESH-52: a place's own size wins round its holes too, at least HOLE_PER_TURN round each
                h0 = SR.curved_size(r_mm, 0.0, floor=0.0, rule=rows[k]["size_mm"], per_turn=SR.HOLE_PER_TURN)
            elif per_turn:
                h0 = min(h0, max(2 * math.pi * r_mm / per_turn, mesh_min))
            return min(h0, rows[k]["size_mm"])
        washers: list = []
        washers_planned: list = []          # SHMESH-45k: every washer planned, built or not
        washer_skipped: dict[str, int] = {}
        block_skipped: dict[str, int] = {}
        gridded: list = []                  # O-grid washers inside a gridded face (SHMESH-41): mapped with it
        cut_of: dict[int, set[int]] = {}    # SHMESH-47b: gridded face -> the joined faces its lines cut
        join_of: dict[int, set[int]] = {}   # SHMESH-47c: joined face -> the strips and faces cut with it (no own-cell rule)
        polar_faces_made: set[int] = set()  # SHMESH-49g: the round plates cut into rings and strips
        bands: list = []                    # SHMESH-49l: their bands round a central opening, meshed in rings
        skipped_where: dict[str, list[int]] = {}  # SHMESH-48f: which face each not-gridded reason came from
        band_known: dict[int, int] = {}
        grid_faces_made: set[int] = set()   # SHMESH-48f: the faces cut into a grid
        boxed: list = []                    # washers in a gridded face's free boxes: their own ring
        box_wish: dict[int, int] = {}       # SHMESH-48q: the counts free boxes' sides ask for (their two-to-one row)
        box_wish_long: set[int] = set()     # SHMESH-51c: the sides of two-ring bolt boxes
        squares: list = []                  # SHMESH-45f: the square holes' washers
        rings: list = []                    # washers in a free face: their own ring mesh (ST.ring_mesh)
        block_preset: dict[int, int] = {}
        block_holes: set[int] = set()
        cut_curves: set[int] = set()
        square_cells: set[int] = set()      # SHMESH-45f: the cells a square ring's strips came out as        # curves SHMESH-41 drew (no CAD edge)
        grid_cells: set[int] = set()        # the cells SHMESH-41 cut faces into
        evened_curves: set[int] = set()     # SHMESH-51o47: the curves SHMESH-51o39/44/46 counted for a free cell
        hole_lines: list[dict] = []         # SHMESH-45c: one line per hole of a flat face, in the record
        if ST.WASHERS and kind != "triangles" and structure not in ("free", "no_washers"):
            mids_of = {m["id"]: m for m in record["mids"]}
            names = {k: (owners[k], str(mids_of.get(owners[k], {}).get("body", ""))) for k in set(tag_of.values())}
            washers, washer_skipped, hole_lines = ST.plan_washers(
                gmsh, tag_of, {t: float(rows[k]["size_mm"]) for t, k in tag_of.items()}, hole_size, mm,
                names=names, before=list(hole_before), no_bolt=_bolt_refused(no_cut), one_ring_at=list(one_ring_at),
                quad_aims=tuple(lim.aims or ()),
                across=ST.WASHER_ACROSS_USER if (sizing or {}).get("global_mm") is not None else ST.WASHER_ACROSS,
                low_mm=(sizing or {}).get("low_mm"), high_mm=(sizing or {}).get("high_mm"),
                low_soft=not _edge_hard(requirements)[0], low_soft_off=_under_low_off(no_cut))
            washers_planned = list(washers)
            if places is not None and places.get("applied") and any(p.get("edge") for p in places.get("places") or []):
                # SHMESH-52b: a hole the user asked a size at is meshed free, that size graded out from its edge (a
                # ring holds its own count round the hole: a bore asked 1 mm under 8 mm came out 4.56 mm)
                asked_at = {c for cs in _edge_place_curves(gmsh, places, (requirements or {}).get("places"), mm, float(
                    np.median([r["wall_mm"] for r in rows]))).values() for c in cs}
                for w in [w for w in washers if asked_at & {abs(int(c)) for c in w.hole}]:
                    w.info.update(washer=False, why="a size you asked is set at this hole")
                    washer_skipped[w.info["why"]] = washer_skipped.get(w.info["why"], 0) + 1
                    washers.remove(w)
            # SHMESH-45f: square holes take a square ring (mapped below); never gridded nor polar
            squares = [w for w in washers if w.corners] if ST.MAPPED and ST.SQUARES else []
            for w in washers:
                if w.corners and w not in squares:
                    w.info.update(washer=False, why="square rings are off on this mesh")
                    washer_skipped["square rings are off on this mesh"] = (
                        washer_skipped.get("square rings are off on this mesh", 0) + 1)
            washers = [w for w in washers if not w.corners]
            polar_made: list = []
            polar_o: list = []
            polar_why: dict[str, int] = {}
            polar_preset: dict[int, int] = {}
            polar_holes: set[int] = set()
            washers_all = washers
            if ST.POLAR and ST.BLOCKS and ST.MAPPED and structure != "free":
                # SHMESH-49: a round plate with its holes on one bolt circle in rings and spokes
                polar_made, polar_why, polar_o, polar_preset, polar_holes = ST.polar_faces(
                    gmsh, washers, tag_of, {t: float(rows[k]["size_mm"]) for t, k in tag_of.items()}, mm,
                    cuts=cut_curves, cells_out=grid_cells, hole_size=hole_size,
                    low_mm=(sizing or {}).get("low_mm"), high_mm=(sizing or {}).get("high_mm"), no_polar=no_cut,
                    made_out=polar_faces_made, bands_out=bands, why_where=skipped_where)
                washers = [w for w in washers if w not in polar_made and w not in polar_o]
            if ST.BLOCKS and ST.MAPPED and structure != "free":
                # SHMESH-41: a face whose holes all have washers is cut into a grid of cells, mapped below
                # (SHMESH-48h: square rings go in too - each takes a free box of the grid, its strips drawn there)
                gridded, boxed, block_preset, block_holes, block_skipped = ST.block_faces(
                    gmsh, washers + squares, tag_of, {t: float(rows[k]["size_mm"]) for t, k in tag_of.items()}, hole_size, mm,
                    cuts=cut_curves, low_mm=(sizing or {}).get("low_mm"), high_mm=(sizing or {}).get("high_mm"),
                    cells_out=grid_cells, no_cut=no_cut, cut_out=cut_of, made_out=grid_faces_made, join_out=join_of,
                    why_where=skipped_where, wish_out=box_wish, wish_long=box_wish_long, quad_aims=tuple(lim.aims or ()),
                    own_bolt=own_bolt)
                if ST.BOLT_GRID_FIRST:
                    # SHMESH-51b: a face refused a grid while it holds two-ring bolt holes is tried again with one
                    # ring round them; where it still is refused its holes keep their two rings in its free mesh
                    refused_k = {k_ for ks_ in skipped_where.values() for k_ in ks_}
                    # (SHMESH-51o11: and the square bolt holes' second rows of a face whose round ones it takes - left
                    # at two rows they kept the face from its grid, and its round holes then took their two rings back
                    # in a free face: a real column's plate went from 51b's one ring to two rings, 8 more triangles;
                    # a face with square bolt holes only keeps their two rows)
                    back_b = [w for w in washers if w.boxed2 and int(w.owner) in refused_k and w.face in tag_of
                              and w not in gridded and w not in boxed]
                    back_b += [w for w in squares if w.corners and w.rows == 2 and w.square_one
                               and w.face in {o.face for o in back_b} and w not in gridded and w not in boxed]
                    saved = {id(w): (w.n, w.rows, w.R, list(w.radii), w.out_mm, w.shrunk, dict(w.info), w.kept,
                                     w.square_one) for w in back_b}
                    back_b = [w for w in back_b if ST.one_ring(w, mm, "one ring so its face keeps its grid")]
                    if back_b:
                        faces_b = {w.face for w in back_b}
                        where_b: dict[str, list[int]] = {}
                        made_before = set(grid_faces_made)
                        g2, b2, p2, h2, _s2 = ST.block_faces(
                            gmsh, [w for w in washers + squares if w.face in faces_b], tag_of,
                            {t: float(rows[k]["size_mm"]) for t, k in tag_of.items()}, hole_size, mm,
                            cuts=cut_curves, low_mm=(sizing or {}).get("low_mm"),
                            high_mm=(sizing or {}).get("high_mm"), cells_out=grid_cells, no_cut=no_cut,
                            cut_out=cut_of, made_out=grid_faces_made, join_out=join_of, why_where=where_b,
                            wish_out=box_wish, wish_long=box_wish_long, quad_aims=tuple(lim.aims or ()),
                            own_bolt=own_bolt)
                        gridded, boxed = gridded + g2, boxed + b2
                        block_preset.update(p2)
                        block_holes = block_holes | h2
                        now_made = set(grid_faces_made) - made_before
                        for why_k in list(skipped_where):
                            gone = [k_ for k_ in skipped_where[why_k] if k_ in now_made]
                            if gone:
                                skipped_where[why_k] = [k_ for k_ in skipped_where[why_k] if k_ not in now_made]
                                block_skipped[why_k] = block_skipped.get(why_k, 0) - len(gone)
                                if not skipped_where[why_k]:
                                    del skipped_where[why_k]
                                if block_skipped.get(why_k, 0) <= 0:
                                    block_skipped.pop(why_k, None)
                        for w in back_b:
                            if w not in g2 and w not in b2 and w.face in tag_of:
                                # (its face stays free: its two rings come back_b)
                                w.n, w.rows, w.R, w.radii, w.out_mm, w.shrunk = saved[id(w)][:6]
                                w.kept, w.square_one = saved[id(w)][7], saved[id(w)][8]
                                w.info.clear()
                                w.info.update(saved[id(w)][6])
            if ST.KEEP_BOX and gridded_ever is not None and ST.BOLT_GRID_FIRST:
                # SHMESH-51j: a face cut into a grid on an earlier pass of this run that comes out uncut after a round of
                # one ring on its holes is refused, not left free without a word: SHMESH-51b takes it (one ring round
                # every bolt hole, its plan by size) - a face left free at planning has no check after meshing, and its
                # joined faces were then refused one after another beside it (the real column's web)
                sent_k = {int(w.owner) for w in washers_planned
                          if any(float(np.linalg.norm(np.asarray(c_[:3], dtype=float) - w.centre)) <= 0.25 * w.r
                                 for c_ in one_ring_at)}
                lost_k = sorted(k_ for k_ in (gridded_ever & sent_k) - set(grid_faces_made)
                                if k_ not in no_cut and -(k_ + BOLT_GRID_KEY) - 1 not in no_cut)
                if lost_k:
                    raise _NoCut({-(k_ + BOLT_GRID_KEY) - 1 for k_ in lost_k},
                                 {-(k_ + BOLT_GRID_KEY) - 1: {
                                     "kind": "bolt rings in a grid", "then": "one ring so its face keeps its grid",
                                     "why": "its grid was not drawn after some of its bolt holes took one ring",
                                     "not_gridded": sorted(r_ for r_, ks_ in skipped_where.items() if k_ in ks_)}
                                  for k_ in lost_k})
                gridded_ever |= set(grid_faces_made)
            for w in gridded:
                w.info["kind"] = "o-grid in a gridded face"
            for w in boxed:
                w.info["kind"] = "free box in a gridded face"
            gridded = gridded + polar_o
            boxed = boxed + polar_made
            block_preset.update(polar_preset)
            block_holes = block_holes | polar_holes
            for key, val in polar_why.items():
                block_skipped[key] = block_skipped.get(key, 0) + val
            washers = washers_all
            rings = [w for w in washers if w not in gridded and w not in boxed]
            # (SHMESH-48k: a square ring made of the grid's own cells is drawn already)
            to_draw = rings + [w for w in squares if not w.in_grid]
            drawn = ST.imprint(gmsh, to_draw, tag_of, cuts=cut_curves, preset=block_preset,
                               square_cells=square_cells)
            lost = [w for w in to_draw if w not in drawn]
            for w in lost:
                reason = str(w.info.get("why") or "its ring was not drawn")
                washer_skipped[reason] = washer_skipped.get(reason, 0) + 1
            washers = [w for w in washers + squares if w not in lost]
            for w in drawn:
                w.info.setdefault("kind", "ring in a free face")
            rings = [w for w in drawn if not w.corners] + boxed
        washer_holes = {c for w in rings for c in w.hole_after} | block_holes
        # SHMESH-1/2: a constant size per face; SHMESH-3/4/6: curves sized by curvature, grown at GROWTH
        fields = []
        top = max(rows[k]["size_mm"] for k in tag_of.values()) / mm

        def grow(curves: list[int], h0: float) -> None:
            """SHMESH-6: size h0 on these curves, growing by GROWTH per element up to the largest size in the
            part - never capped at a smaller one, which would hold every coarser face to it."""
            if h0 >= top or not curves:
                return
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "CurvesList", sorted(set(curves)))
            gmsh.model.mesh.field.setNumber(dist, "Sampling", 50)
            thr = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(thr, "InField", dist)
            gmsh.model.mesh.field.setNumber(thr, "SizeMin", h0)
            gmsh.model.mesh.field.setNumber(thr, "SizeMax", top)
            gmsh.model.mesh.field.setNumber(thr, "DistMin", 0.0)
            gmsh.model.mesh.field.setNumber(thr, "DistMax", (top - h0) / (growth - 1.0))
            fields.append(thr)

        # SHMESH-27: an edge of the CAD shorter than the size of the faces it bounds is graded down to
        short_edges = (_short_edges(gmsh, tag_of, rows, mm,
                                    low_mm=(sizing or {}).get("low_mm") if SHORT_SUPPRESS else None,
                                    along=_along(sizing, record), along_skip=along_skip)
                       if narrow is not None else None)
        # SHMESH-16: narrow four-sided strips (bends, flanges) are mapped, their elements stretched along them
        strip_counts: dict[int, int] = {}
        thin_strips: dict[int, tuple] = {}
        strip_steps: dict[int, tuple] = {}     # SHMESH-73: strips whose rows step between two counts
        mapped = (_map_strips(gmsh, tag_of, rows, mm, sizing,
                              fine_at=short_edges["fine_at"] if short_edges and FEATURE_UNMAP else None,
                              counts_out=strip_counts,
                              held=frozenset(box_wish_long & set(box_wish)) if ST.BOX_HOLDS else frozenset(),
                              prefer=_box_facing(gmsh, box_wish, box_wish_long) if STRIP_TAKES_BOX else None,
                              narrow_out=thin_strips if (NARROW_COLLAPSE and sizing is not None
                                                         and sizing.get("low_mm") is not None) else None,
                              not_thin=_structured_faces(gmsh, tag_of, rings, grid_cells, cut_curves,
                                                         grid_faces_made | polar_faces_made),
                              transitions_out=strip_steps,
                              no_step=((_no_step(tag_of, facts) if _STEP_FAILED else frozenset())
                                       | (_no_step(tag_of, facts, _STEP_EXCLUDE) if _STEP_EXCLUDE else frozenset())),
                              no_fit=_no_step(tag_of, facts, _FIT_EXCLUDE) if _FIT_EXCLUDE else frozenset())
                  if MAP_STRIPS and kind != "triangles" else {})
        # SHMESH-60: the strips narrower than the user's shortest edge that are closed after meshing; their ends are
        # not graded to (they close to a point)
        closing = _narrow_choose(gmsh, thin_strips, float((sizing or {})["low_mm"])) if thin_strips else []
        if closing and short_edges is not None:
            gone_c = {c for t in closing for c in thin_strips[t][1]}
            for h in list(short_edges["by_size"]):
                short_edges["by_size"][h] = [c for c in short_edges["by_size"][h] if c not in gone_c]
                if not short_edges["by_size"][h]:
                    del short_edges["by_size"][h]
        if closing and CLOSING_SIZE:
            # SHMESH-60: a strip to be closed grades nothing: its own (curvature) size and its arcs' sizes are the
            # size of the faces it joins, so the faces beside it are not sized down from a strip that goes
            for t in closing:
                k = tag_of[t]
                near_s = [float(rows[tag_of[int(s_)]]["size_mm"]) for c in thin_strips[t][0]
                        for s_ in gmsh.model.getAdjacencies(1, c)[0] if int(s_) != t and int(s_) in tag_of
                        and int(s_) not in closing]
                if near_s:
                    rows[k]["size_mm"] = max(float(rows[k]["size_mm"]), min(near_s))
                rows[k].pop("curve_mm", None)
        # SHMESH-40: every other four-sided face is a mapped grid too (counts shared through the curves they share)
        grids: dict[int, list[int]] = {}
        if ST.MAPPED and kind != "triangles" and structure != "free":
            def arc_mm(c: int, t: int) -> float | None:
                r = _arc_radius(gmsh, c)
                return None if r is None else hole_size(r * mm, tag_of[t])

            grids, _grid_counts = ST.map_faces(
                gmsh, tag_of, {t: float(rows[k]["size_mm"]) for t, k in tag_of.items()}, mm, arc_mm=arc_mm,
                skip=set(mapped) | set(thin_strips) | set(strip_steps) | {w.annulus for w in rings} | (
                    _cells_of_keys(gmsh, tag_of, unmap, mm) if GATHER else set(unmap)),
                preset={**strip_counts, **block_preset},
                low_mm=(sizing or {}).get("low_mm"), high_mm=(sizing or {}).get("high_mm"),
                fine_at=short_edges["fine_at"] if short_edges else None,
                growth=growth, wish=box_wish, wish_long=box_wish_long)
            # SHMESH-48c: a grid line's piece that bounds free cells only (between neighbouring washers' free boxes,
            # a column of them in rows of bolt holes) is counted by no mapped face, and gmsh's own count there left a
            # triangle in each box. Each such piece takes the count nearest its length over the size, one more where
            # that makes a box beside it even all round (its other sides' counts and its ring's): a box with one
            # piece left uncounted sets it, so a column of boxes is counted from its ends in
            known = {**_grid_counts, **block_preset, **{w.ring: w.n for w in boxed if w.ring}}
            band_known = known
            in_grid = {abs(int(c)) for t in grids for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)}
            open_ = {c for c in cut_curves - in_grid - set(known)
                     if any(int(x) in tag_of for x in gmsh.model.getAdjacencies(1, c)[0])}
            # (and a curve of the CAD's that only a free cell of a gridded face or round plate bounds - a central
            # opening inside a bolt circle's paved band: its count makes that band even all round, SHMESH-49j)
            annuli = {w.annulus for w in rings}
            for f_ in sorted(set(grid_cells) - set(grids) - annuli):
                if f_ not in tag_of:
                    continue
                for _d, x in gmsh.model.getBoundary([(2, f_)], oriented=False):
                    ax_ = abs(int(x))
                    if ax_ not in known and ax_ not in in_grid and len(gmsh.model.getAdjacencies(1, ax_)[0]) == 1:
                        open_.add(ax_)
            bound: dict[int, list[int]] = {}
            for c_piece in sorted(open_):
                for f_ in (int(x) for x in gmsh.model.getAdjacencies(1, c_piece)[0] if int(x) in tag_of):
                    bound.setdefault(f_, [abs(int(x)) for _d, x in gmsh.model.getBoundary([(2, f_)], oriented=False)])
            # SHMESH-51c: a two-ring bolt box's side that no mapped face counts (it lies between two boxes' free cells,
            # so no cell has it as its one open side) takes its band's count at once (gmsh's own count, 6 a side of
            # 56 mm, left no band and paved the boxes with triangles)
            for c_w in sorted(open_ & box_wish_long):
                known[c_w] = box_wish[c_w]
                open_.discard(c_w)
                gmsh.model.mesh.setTransfiniteCurve(c_w, box_wish[c_w] + 1)
            if CHAIN_TAKES_BOX:
                # SHMESH-51o39: a free four-sided cell with a two-ring bolt box's side on one side and a chain of
                # uncounted pieces opposite gives the chain the box side's count, spread by length, where each piece
                # stays inside the user's edges with their flexible band and the two other sides already agree - the
                # cell then meshes as rows (on the real column a 12-hole plate's 59.7 x 17.1 mm cells beside its end
                # boxes: a 56 mm side of 4 against 17.7 + 42.0 mm, which gmsh sized 2 + 4, paved with 12 triangles)
                hi_t = _high_tolerant(sizing, requirements)
                lo_t = _low_tolerant(sizing, requirements)
                for f_ in sorted(bound):
                    got_f = ST._four_sided(gmsh, f_)
                    if got_f is None:
                        continue
                    sides_f = got_f[0]
                    for i_ in range(4):
                        one, many = sides_f[i_], sides_f[(i_ + 2) % 4]
                        if len(one) != 1 or one[0] not in box_wish_long or one[0] not in known:
                            continue
                        # (SHMESH-51o44: pieces of the chain a grid or box already counts keep their counts; the
                        # others - open cut pieces, or uncounted edges no grid bounds, of this cell alone or shared with
                        # another free cell - take what is left of the box side's count. On the real column a 56 mm
                        # box side of 4 faced 17.9 + 20.2 + 17.9 mm, the middle piece a grid's 2 and the outer two
                        # left to gmsh, which gave them 3 each: 8 against 4, paved; and the slot strips beside, whose
                        # 17.9 mm ends stood against 14.0 mm sides of 1, were paved for the same pieces)
                        free_p = [x for x in many if x not in known]
                        if not many or not free_p or not all(x in open_ or (x not in in_grid and (
                                CHAIN_SHARED or len(gmsh.model.getAdjacencies(1, x)[0]) == 1)) for x in free_p):
                            continue        # (open cut pieces, or uncounted edges no grid bounds)
                        if not CHAIN_SHARED and len(free_p) != len(many):
                            continue
                        o1, o2 = sides_f[(i_ + 1) % 4], sides_f[(i_ + 3) % 4]
                        if not all(x in known for x in o1 + o2) or sum(known[x] for x in o1) != sum(known[x] for x in o2):
                            continue
                        left_n = int(known[one[0]]) - sum(int(known[x]) for x in many if x in known)
                        if left_n < len(free_p):
                            continue
                        lens = [float(gmsh.model.occ.getMass(1, x)) * mm for x in free_p]
                        got_s = ST._spread(left_n, lens)
                        if got_s is None or any(
                                (hi_t is not None and ln / n_c > hi_t) or (lo_t is not None and ln / n_c < lo_t)
                                for ln, n_c in zip(lens, got_s, strict=True)):
                            continue
                        for x, n_c in zip(free_p, got_s, strict=True):
                            known[x] = n_c
                            open_.discard(x)
                            gmsh.model.mesh.setTransfiniteCurve(x, n_c + 1)
                            evened_curves.add(x)
                        break
            if CELL_EVEN:
                # SHMESH-51o46: a free four-sided cell of a gridded face whose opposite sides disagree, so that gmsh
                # paves it (an odd count round it forces a triangle): the side that can move takes the other's count -
                # its uncounted pieces (no grid bounds them; shared with another free cell or not) spread by length,
                # or, when it is one grid-counted curve, the whole band of grid rows it lies on (_band_chain), every
                # cell on the band staying a grid. Where both sides can move, the longer keeps the count its length
                # over the size gives. Each new edge stays inside the user's edges. (On the
                # real column a 56 x 29.2 mm cell had grid rows of 3 on one side and 4 on the other, two bands of the
                # same 29.2 mm height counted apart; and a 25.4 mm cell on the 6-hole plate 3 against 4)
                # (inside the user's edges themselves, not their band: evening a cell puts no new element outside an
                # edge target - edges come before triangles)
                hi_e = (sizing or {}).get("high_mm")
                lo_e = (sizing or {}).get("low_mm")
                stop_f = {w.box for w in rings if w.box} | annuli
                ring_c = {w.ring for w in rings if w.ring}

                def ok_len(ln: float, n_: int, x: int | None = None) -> bool:
                    """Inside the user's edges, and (SHMESH-51o48) no coarser than the size the rules gave the faces
                    the curve bounds: on the real column bands of 56 mm taken from 6 to 4 (14 mm) and 55 mm from 6 to 4
                    evened cells of C but left a plate beside them and the 6-hole plates two triangles each."""
                    if n_ < 1 or (hi_e is not None and ln / n_ > hi_e) or (lo_e is not None and ln / n_ < lo_e):
                        return False
                    if x is not None and EVEN_NO_COARSER:
                        sz_ = [float(rows[tag_of[int(f)]]["size_mm"]) for f in gmsh.model.getAdjacencies(1, x)[0]
                               if int(f) in tag_of]
                        if sz_ and ln / n_ > max(sz_) * (1 + EVEN_SIZE_TOL):
                            return False
                    return True

                # (a planned count - map_faces' or a block's preset - is not set until its cell is a grid: only the grids'
                # own curves, the rings', the curves 51c / 51o39 / 51o44 / 51o46 set, are held)
                held_c = in_grid | ring_c | evened_curves | (set(box_wish_long) & set(known) - set(_grid_counts))

                def held(x: int) -> bool:
                    """SHMESH-51o47: whether a curve's count is set for the mesh - a grid's, a ring's, a box's, or one
                    counted to even a cell - and not only planned (a count map_faces planned for a cell it then left
                    free is not set: gmsh chose another, and a cell evened against it came out odd)."""
                    return x in held_c and x in known

                def alone(x: int) -> bool:
                    """Whether a new count on this curve changes no other free cell: every face it bounds is this
                    cell or a grid (a free cell beside it would take the count too, and its own count round could
                    turn odd - on the lab channels a cell evened that way left its neighbour a triangle)."""
                    return all(int(f) == f_ or int(f) in grids for f in gmsh.model.getAdjacencies(1, x)[0])

                def mover(sd: list[int]):
                    """('free', pieces) / ('band', chain) / None: what on this side may take a new count."""
                    free_s = [x for x in sd if not held(x) and x not in in_grid and x not in ring_c and alone(x)
                              and not any(int(f) in stop_f for f in gmsh.model.getAdjacencies(1, x)[0])]
                    if free_s and all(held(x) or x in free_s for x in sd):
                        return "free", free_s
                    if len(sd) == 1 and sd[0] in known and sd[0] in in_grid and sd[0] not in ring_c \
                            and sd[0] not in box_wish:
                        ch = _band_chain(gmsh, sd[0], grids, stop_f)
                        if ch and not any(x in ring_c or x in box_wish or not alone(x) for x in ch):
                            return "band", ch
                    return None

                for f_ in sorted(set(grid_cells) - set(grids) - annuli - stop_f):
                    if f_ not in tag_of:
                        continue
                    got_f = ST._four_sided(gmsh, f_)
                    if got_f is None:
                        continue
                    sides_f = got_f[0]
                    size_f = float(rows[tag_of[f_]]["size_mm"])
                    for i_ in range(2):
                        sa, sb = sides_f[i_], sides_f[i_ + 2]
                        if all(held(x) for x in sa + sb) and sum(known[x] for x in sa) == sum(known[x] for x in sb):
                            continue
                        ma, mb = mover(sa), mover(sb)
                        fixed_a = all(held(x) for x in sa) and ma is None
                        fixed_b = all(held(x) for x in sb) and mb is None
                        if fixed_a and fixed_b:
                            continue
                        len_ = {x: float(gmsh.model.occ.getMass(1, x)) * mm for x in sa + sb}
                        if fixed_a or fixed_b:
                            tgt = sum(known[x] for x in (sa if fixed_a else sb))
                            moves = [(sb, mb)] if fixed_a else [(sa, ma)]
                        elif ma is None or mb is None:
                            continue
                        else:
                            longer = sa if sum(len_[x] for x in sa) >= sum(len_[x] for x in sb) else sb
                            tgt = max(1, round(sum(len_[x] for x in longer) / size_f))
                            moves = [(sa, ma), (sb, mb)]
                        plan_: dict[int, int] = {}
                        for sd, mv in moves:
                            if mv is None:
                                plan_ = {}
                                break
                            kind_m, items = mv
                            if kind_m == "free":
                                left_ = tgt - sum(known[x] for x in sd if x not in items)   # (the others held)
                                ls_ = [len_[x] for x in items]
                                got_s = ST._spread(left_, ls_) if left_ >= len(items) else None
                                if got_s is None or not all(ok_len(ln, n_, x) for ln, n_, x in zip(ls_, got_s, items,
                                                                                                    strict=True)):
                                    plan_ = {}
                                    break
                                plan_.update(zip(items, got_s, strict=True))
                            else:
                                if not all(ok_len(float(gmsh.model.occ.getMass(1, x)) * mm, tgt, x) for x in items):
                                    plan_ = {}
                                    break
                                plan_.update(dict.fromkeys(items, tgt))
                        if not plan_:
                            continue
                        was_ = {x: known.get(x) for x in plan_}
                        for x, n_c in sorted(plan_.items()):
                            known[x] = n_c
                            open_.discard(x)
                            gmsh.model.mesh.setTransfiniteCurve(x, n_c + 1)
                            evened_curves.add(x)
                        # (what was evened, in sizes and counts: the cell's face and size, the moves' kinds, each
                        # curve's length, count before and after, and the faces it bounds - so a record shows where)
                        bb_ = gmsh.model.getBoundingBox(2, f_)
                        logger.info("shell mesh: a free cell evened (SHMESH-51o46): face %s cell %s; %s; %s",
                                    tag_of[f_], "x".join(f"{v * mm:.1f}" for v in sorted(
                                        np.subtract(bb_[3:], bb_[:3]))[1:][::-1]),
                                    [mv[0] + f" {len(mv[1])}" for _sd, mv in moves if mv],
                                    [(round(float(gmsh.model.occ.getMass(1, x)) * mm, 1), was_[x], n_c,
                                      sorted({int(tag_of[int(f)]) for f in gmsh.model.getAdjacencies(1, x)[0]
                                              if int(f) in tag_of}))
                                     for x, n_c in sorted(plan_.items())])
            settled = True
            while settled:
                settled = False
                for f_, curves_f in sorted(bound.items()):
                    left = [x for x in curves_f if x in open_]
                    if len(left) != 1 or any(x not in known and x not in open_ for x in curves_f):
                        continue
                    c_open = left[0]
                    h_c = float(rows[tag_of[f_]]["size_mm"]) / mm
                    n_c = max(1, round(float(gmsh.model.occ.getMass(1, c_open)) / h_c))
                    # (SHMESH-48q: a free box's side its own two-to-one row asks for, BAND_SEG sizes long)
                    n_c = box_wish.get(c_open, n_c)
                    if (sum(known[x] for x in curves_f if x != c_open) + n_c) % 2:
                        n_c = _even_count(float(gmsh.model.occ.getMass(1, c_open)) * mm, n_c,
                                          (sizing or {}).get("low_mm"), (sizing or {}).get("high_mm"))
                    known[c_open] = n_c
                    open_.discard(c_open)
                    gmsh.model.mesh.setTransfiniteCurve(c_open, n_c + 1)
                    settled = True
        if short_edges is not None and narrow is not None and mapped and not FEATURE_AT_STRIPS:
            _spare_strip_corners(gmsh, tag_of, rows, mapped, narrow, short_edges)
        # SHMESH-6: a face finer than the coarsest grades its neighbours from its own edges (a mapped strip's long
        # sides from its size along them)
        rims: dict[float, list[int]] = {}
        for t, k in sorted(tag_of.items()):
            long_sides, along = mapped.get(t, ((), 0.0))
            rims.setdefault(float(rows[k]["size_mm"]), []).extend(
                abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False) if abs(c) not in long_sides)
            if long_sides:
                rims.setdefault(float(f"{along * mm:.6g}"), []).extend(long_sides)
        # one field per size, not per face: gmsh reads every field of the Min at every point it sizes, and a ship's
        # 5,392 faces made 5,392 constant fields (sh0048: its 1D mesh alone ran past 30 minutes). The same sizes.
        const_by: dict[float, list[int]] = {}
        arcs_by: dict[float, list[int]] = {}
        for t, k in sorted(tag_of.items()):
            const_by.setdefault(float(rows[k]["size_mm"]), []).append(t)
            if "curve_mm" in rows[k]:
                # SHMESH-3: each arc of the face refined to its OWN radius's size, then grown; its straight edges
                # and wider arcs keep the face's size (one small hole no longer refines the whole rim)
                for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False):
                    r = _arc_radius(gmsh, abs(c))
                    if r is None or abs(c) in washer_holes:
                        continue
                    h0, _rule = plan_rules.curve_size(r * mm, wall=rows[k]["wall_mm"])
                    h0 *= sz
                    if finest is not None and sizing is not None:
                        # SHMESH-24: the user's global size (else no finer than their shortest edge allows) - but a
                        # hole keeps its shape
                        # hole keeps its shape: SHMESH-33, at least HOLE_PER_TURN edges round it
                        h0 = SR.curved_size(r * mm, 0.0, floor=finest, rule=(
                            rows[k]["size_mm"] if sizing.get("global_mm") is not None else h0),
                            per_turn=SR.HOLE_PER_TURN)
                    elif "place" in rows[k]:
                        # SHMESH-52: the size asked in a place wins over our arc sizes there, coarser or finer; a
                        # hole keeps its shape (HOLE_PER_TURN a turn), as under the user's size for the whole mesh
                        h0 = SR.curved_size(r * mm, 0.0, floor=0.0, rule=rows[k]["size_mm"], per_turn=SR.HOLE_PER_TURN)
                    if h0 < rows[k]["size_mm"]:
                        arcs_by.setdefault(float(f"{h0:.6g}"), []).append(abs(c))
        if places is not None and places.get("applied"):
            # SHMESH-52b: a place the shell makes an edge of (a hole's bore, the plate's edge wall) is sized on the
            # curves lying on it, graded out as an arc's size is
            for h, curves in _edge_place_curves(gmsh, places, (requirements or {}).get("places"), mm,
                                                float(np.median([r["wall_mm"] for r in rows]))).items():
                arcs_by.setdefault(h, []).extend(curves)
        if short_edges is not None:
            for h, curves in short_edges["by_size"].items():
                arcs_by.setdefault(h, []).extend(curves)
        for w in rings:
            # SHMESH-39: the field grows out from the ring at the ring's own element length
            if w.out_mm < rows[w.owner]["size_mm"]:
                arcs_by.setdefault(float(f"{w.out_mm:.6g}"), []).append(w.ring)
        for h_mm, surfs in sorted(const_by.items()):
            fid = gmsh.model.mesh.field.add("Constant")
            gmsh.model.mesh.field.setNumber(fid, "VIn", h_mm / mm)
            gmsh.model.mesh.field.setNumbers(fid, "SurfacesList", surfs)
            gmsh.model.mesh.field.setNumber(fid, "IncludeBoundary", 1)
            fields.append(fid)
        grading: dict[float, list[int]] = {}
        for h, curves in list(rims.items()) + list(arcs_by.items()):
            grading.setdefault(h, []).extend(curves)
        if len(grading) > GROW_GROUPS:
            # gmsh reads every grading field at every point it sizes: a ship's 221 of them held its 1D mesh past 30
            # minutes. Past GROW_GROUPS sizes, each size is taken down to its step of GROW_STEP (finer, never coarser)
            binned: dict[float, list[int]] = {}
            for h, curves in grading.items():
                b = float(f"{GROW_STEP ** math.floor(math.log(h) / math.log(GROW_STEP) + 1e-9):.6g}")
                binned.setdefault(b, []).extend(curves)
            grading = binned
        for h, curves in sorted(grading.items()):
            grow(curves, h / mm)
        spot_floor = _low_tolerant(sizing, requirements) if SPOT_FLOOR else None
        priced = 0.0
        for spot in own_spots:
            # SHMESH-31: the face's own rules' size inside a box round elements past the solver's limits, graded out
            k, lo, hi = spot[0], spot[1], spot[2]
            h0 = rule_size[k]
            if spot_floor is not None:
                h0 = max(h0, float(spot_floor))          # (SHMESH-31b: never under the user's shortest edge allowed)
            here = min(rows[k]["size_mm"], rows[k].get("curve_mm", rows[k]["size_mm"]))
            if len(spot) > 3 and spot[3]:
                h0 = max(h0, here / 2.0 ** int(spot[3]))  # (SHMESH-31c: its step - half, a quarter, ... down to h0)
            if h0 >= here:
                continue
            # SHMESH-31d: one new size round the elements; SHMESH-31e: a grown box its elements' longest edge
            pad_ = max(SPOT_PAD_H * h0, float(spot[4])) if len(spot) > 5 and spot[5] else SPOT_PAD_H * h0
            if spot_budget is not None:
                # SHMESH-31b: the plan priced before gmsh, worst spots first; past the budget the rest are left
                grow_ = 2.0 * pad_ if len(spot) > 5 and spot[5] else 0.0
                ext = sorted(abs(float(b) - float(a)) + grow_ for a, b in zip(lo, hi, strict=True))
                cost = ext[1] * ext[2] / (h0 * h0)
                if priced + cost > spot_budget:
                    spots_trimmed += 1
                    continue
                priced += cost
            box = gmsh.model.mesh.field.add("Box")
            gmsh.model.mesh.field.setNumber(box, "VIn", h0 / mm)
            gmsh.model.mesh.field.setNumber(box, "VOut", top)
            for name, v in zip(("XMin", "YMin", "ZMin"), lo, strict=True):
                gmsh.model.mesh.field.setNumber(box, name, (float(v) - pad_) / mm)
            for name, v in zip(("XMax", "YMax", "ZMax"), hi, strict=True):
                gmsh.model.mesh.field.setNumber(box, name, (float(v) + pad_) / mm)
            gmsh.model.mesh.field.setNumber(box, "Thickness", SPOT_GRADE * (here - h0) / (growth - 1.0) / mm)
            if SPOT_OWN:
                # SHMESH-31 (lab): the box reads on its own face only (its gmsh surfaces, and their curves with
                # SPOT_OWN == "curves"): its grading cannot spill into the neighbours
                own_s = sorted(t for t, kk in tag_of.items() if kk == k)
                rst = gmsh.model.mesh.field.add("Restrict")
                gmsh.model.mesh.field.setNumber(rst, "InField", box)
                gmsh.model.mesh.field.setNumbers(rst, "SurfacesList", own_s)
                if SPOT_OWN == "curves":
                    gmsh.model.mesh.field.setNumbers(rst, "CurvesList", sorted(
                        {abs(c) for _d, c in gmsh.model.getBoundary([(2, t) for t in own_s], combined=False,
                                                                      oriented=False)}))
                box = rst
            fields.append(box)
        mn =gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(mn, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(mn)
        evened = 0
        washer_curves = ST.set_counts(gmsh, rings)
        band_of: dict[int, ST.Band] = {}
        for bd in bands:
            if bd.face in tag_of and bd.face not in grids and ST.band_counts(
                    gmsh, bd, {**_grid_counts, **block_preset, **band_known},
                    float(rows[tag_of[bd.face]]["size_mm"]), mm) is not None:
                band_of[bd.face] = bd
        if EVEN_FACES and kind != "triangles":
            fixed = {abs(c) for t in mapped for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)}
            # SHMESH-60: a narrow face's counts stand (its long sides pair node for node)
            fixed |= {c for v_ in thin_strips.values() for c in (*v_[0], *v_[1])}
            fixed |= {c for v_ in strip_steps.values() for c in (v_[0], v_[1], *v_[2])}
            fixed |= {c for bd in band_of.values() for c in bd.inner}  # SHMESH-49l: the band's count stands
            fixed |= washer_curves
            fixed |= {abs(c) for t in grids for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)}
            # SHMESH-51o47: nor a curve counted to even a free cell (51o39/44/46) - a segment more there, to even a
            # face beside it, turned the cell odd again (on the lab channel a cell evened to 3 + 1 + 3 + 1 took a
            # fourth on its far side and was paved with a triangle)
            fixed |= evened_curves
            floors = {t: plan_rules.FLOOR_WALLS * rows[k]["wall_mm"] * min(sz, 1.0) / mm for t, k in tag_of.items()}
            if sizing is not None and sizing.get("low_mm") is not None:
                # SHMESH-24: nor under the user's shortest edge
                low = float(sizing["low_mm"])
                floors = {t: max(v, low / mm) for t, v in floors.items()}
            if places is not None and places.get("applied"):
                # SHMESH-52: nor over half the size a place was asked for (finer than half its wall: as asked)
                floors = {t: min(v, 0.5 * float(rows[tag_of[t]]["size_mm"]) / mm) if "place" in rows[tag_of[t]]
                          else v for t, v in floors.items()}
            evened = _even_faces(gmsh, tag_of, fixed, floors)
        if PLANE_ALGORITHM is not None and structure != "free":
            # SHMESH-42: flat faces meshed free by the quad-aligned algorithm (rows along their edges)
            for t in sorted(tag_of):
                if gmsh.model.getType(2, t) == "Plane" and t not in grids and t not in mapped:
                    gmsh.model.mesh.setAlgorithm(2, t, PLANE_ALGORITHM)
        if BIG_FLAT_FACE and kind != "triangles":
            # SHMESH-21g: a big flat face left free is meshed by the quad-aligned algorithm (Blossom scales on it)
            for t, k in big_flat_faces(gmsh, tag_of, facts, rows, mm, skip=set(grids) | set(mapped) | {w.annulus for w in rings}).items():
                gmsh.model.mesh.setAlgorithm(2, t, BIG_FLAT_ALGORITHM)
                rows[k]["big_flat"] = BIG_FLAT_ALGORITHM
        if POLAR_BAND_ALGORITHM is not None and polar_faces_made:
            # SHMESH-49k: a round plate's free cells (the band inside its bolt circle) paved by this algorithm
            for t in sorted(tag_of):
                if tag_of[t] in polar_faces_made and t not in grids and t not in {w.annulus for w in rings}:
                    gmsh.model.mesh.setAlgorithm(2, t, POLAR_BAND_ALGORITHM)
        for t, rung in sorted(rungs.items()):
            if rung in LADDER_ALGO and t in tag_of:
                # SHMESH-71: another 2D algorithm for a face that held gmsh past the watchdog
                gmsh.model.mesh.setAlgorithm(2, t, LADDER_ALGO[rung])
        for c_f, n_f in sorted(fine_curves.items()):
            # SHMESH-71: the curve rung - a curve gmsh named meshed finer (its two faces mesh to it)
            gmsh.model.mesh.setTransfiniteCurve(c_f, n_f + 1)
        _watch_tags(tag_of, facts, {t: float(facts[k]["area"]) * mm * mm / float(rows[k]["size_mm"]) ** 2
                                    for t, k in tag_of.items() if float(rows[k].get("size_mm") or 0) > 0})
        _generate(gmsh, tag_of)
        node_tags, coords, _p = gmsh.model.mesh.getNodes()
        tags = np.asarray(node_tags, dtype=np.int64)
        index = np.full(int(tags.max()) + 1 if len(tags) else 1, -1, dtype=np.int64)
        index[tags] = np.arange(len(tags))
        xyz = np.asarray(coords, dtype=float).reshape(-1, 3) * mm
        ring_faces = {w.annulus for w in rings}
        high = (sizing or {}).get("high_mm")
        pending: _NoCut | None = None       # (SHMESH-51f: this pass's refusal, raised once the grids are checked too)
        zero_kept: list[dict] = []          # (SHMESH-51o21: grids with no triangle kept unset against a free mesh)
        fixable: list[tuple] = []           # (SHMESH-51o24: mapped and transition cells: face, outline box, key)
        tria_told: list[dict] = []          # (SHMESH-51o32: triangles under the user's angle target, in words)
        cells_told: list[tuple] = []        # (SHMESH-51o24: box cells, as fixable, never freed)
        if check_maps and (cut_of or join_of):
            # SHMESH-47b: a gridded face whose lines cut a joined face into triangles or elements past the solver's
            # own limits - there, or in its own free cells - is left ungridded on the next pass (its neighbours whole)
            hurt: set[int] = set()
            why_cut: dict[int, dict] = {}
            # the tria angle lines a free box's one triangle must meet: the user's, else 30 and 120 degrees
            angle_aims = [(key, op, target) for shape, key, op, target, *_r in lim.aims
                          if shape == "tria" and key in ("min_angle_tria", "max_angle_tria")]
            if not angle_aims:
                angle_aims = [("min_angle_tria", ">=", 30.0), ("max_angle_tria", "<=", 120.0)]

            def elements_of(s_: int) -> list[tuple[int, np.ndarray]]:
                types, _tags, conns = gmsh.model.mesh.getElements(2, s_)
                return [(int(et), index[np.asarray(conn, dtype=np.int64)]) for et, conn in zip(types, conns, strict=True)]

            # (SHMESH-47c: a joined face's strips are held to the same; its own cells as any gridded face's, SHMESH-48f)
            either = {k_: cut_of.get(k_, set()) | join_of.get(k_, set()) for k_ in set(cut_of) | set(join_of)}
            tri_cells: dict[int, dict[int, int]] = {}   # (SHMESH-51c: triangles per free cell)
            # SHMESH-51e: a free box's cell whose own band will be made (SHMESH-48q) is judged on that band, not on
            # gmsh's paving that the band replaces (a paved two-to-one box left quads past the taper limit, and a
            # face whose bands were all good lost its grid for elements it never delivers); the band keeps its own
            # checks (the solver's limits, the user's quad lines and longest edge) and the face is judged again on
            # what it delivers (SHMESH-48f)
            banded: set[int] = set()
            past_cells: set[int] = set()        # (SHMESH-51e: free cells with a quad past the solver's limits)
            if ST.FREE_BAND and ST.BAND_JUDGED and structure != "free":
                for w in rings:
                    if not w.box or w.box not in tag_of or tag_of[w.box] not in cut_of:
                        continue
                    # (tried as they are made below - its ring first, which may slide the ring's nodes - on a copy:
                    # nothing here changes the mesh or the washer)
                    info_, xyz_ = dict(w.info), xyz.copy()
                    try:
                        ST.ring_mesh(gmsh, w, index, xyz_, mm)
                        ST.box_band(gmsh, w, index, xyz_, mm, lim, high)
                    except ValueError:
                        continue
                    finally:
                        w.info.clear()
                        w.info.update(info_)
                    banded.add(w.box)
            if ST.TRANSITION_CELLS and ST.FREE_BAND and structure != "free":
                # SHMESH-51o17: a free cell its own transition rows will mesh is judged on them (tried on a copy)
                for s_t in _transition_cells(gmsh, tag_of, _cut_faces(cut_of, join_of), grids, rings, banded,
                                             _cells_of_keys(gmsh, tag_of, {k_ for k_ in unmap if k_[-1:] == (CELL_FIRST_MARK,)}, mm) if GATHER and CELL_FIRST
                                             else set()):
                    try:
                        ST.transition_cell(gmsh, s_t, index, xyz, mm, lim, high, (sizing or {}).get("low_mm"))
                    except ValueError:
                        continue
                    banded.add(s_t)
            for kg, nbrs in sorted(either.items()):
                # the joined faces its lines cut: no triangle, no quad past the solver's limits
                for s_ in sorted(x for x, kx in tag_of.items() if kx in nbrs):
                    if s_ in banded:
                        continue        # (SHMESH-51o17: its own rows replace this paving)
                    for et, c_ in elements_of(s_):
                        if et == 2 or (et == 3 and shell_quality._quad_past(
                                shell_quality.quad_measures(xyz, c_.reshape(-1, 4))).any()):
                            hurt.add(kg)
                            why_cut[kg] = {"kind": "grid", "why": "its lines cut a joined face into triangles or "
                                                                  "elements past the solver's limits",
                                           "at_mm": [round(float(x), 1) for x in xyz[c_[:4]].mean(axis=0)]}
                    if kg in hurt:
                        break
                if kg in hurt or kg not in cut_of:
                    continue
                # SHMESH-48e: its own free cells (a washer's free box): no quad past the solver's limits, and at most
                # one triangle each, inside the tria angle lines, the face's triangles under BOX_TRIA_SHARE of its
                # elements (a box between rows of bolt holes leaves one where its ring nearly meets the shared line)
                n_face, n_tri = 0, 0
                for s_ in sorted(x for x, kx in tag_of.items() if kx == kg):
                    tri_here = 0
                    tri_cell = tri_cells.setdefault(kg, {})
                    for et, c_ in elements_of(s_):
                        if s_ in banded:
                            n_face += len(c_) // (4 if et == 3 else 3)      # (its band replaces this paving)
                            continue
                        if et == 3:
                            quads_ = c_.reshape(-1, 4)
                            n_face += len(quads_)
                            if s_ not in grids and shell_quality._quad_past(
                                    shell_quality.quad_measures(xyz, quads_)).any():
                                hurt.add(kg)
                                past_cells.add(s_)
                        elif et == 2:
                            tris_ = c_.reshape(-1, 3)
                            n_face += len(tris_)
                            tri_here += len(tris_)
                            m_ = shell_quality.tria_measures(xyz, tris_)
                            for key, op, target in angle_aims:
                                bad_ = (m_[key] < target) if op.startswith(">") else (m_[key] > target)
                                if bad_.any():
                                    hurt.add(kg)
                    n_tri += tri_here
                    tri_cell[s_] = tri_here
                    if tri_here > (1 if s_ not in grids else 0):
                        hurt.add(kg)
                if n_tri > BOX_TRIA_SHARE * max(n_face, 1):
                    hurt.add(kg)
                if kg in hurt and kg not in why_cut:
                    why_cut[kg] = {"kind": "grid", "why": "its free cells: a box with more than one triangle, a triangle "
                                                          "past the tria angle lines, a quad past the solver's limits "
                                                          "or triangles over BOX_TRIA_SHARE",
                                   "triangles": n_tri, "elements": n_face}
            if hurt and ST.BOLT_BAND_LONG:
                # SHMESH-51c: where the face's only fault is in two-ring bolt holes' boxes (their band not made, paved
                # with triangles), those holes take one ring and the face keeps its grid and its other holes' rings
                bad_holes = []
                for kg_ in sorted(hurt):
                    two_ = [w for w in boxed if w.radii and int(w.owner) == kg_]
                    # (SHMESH-51e: or its box cell, its band not made, paved with a quad past the solver's limits)
                    # (... or a free cell beside its box, paved so: the cell a box side's count could not map)
                    past_sides = {abs(int(c)) for s_p in past_cells if s_p in tag_of and tag_of[s_p] == kg_
                                  for _d, c in gmsh.model.getBoundary([(2, s_p)], oriented=False)}
                    for w_p in two_:
                        if not w_p.box or w_p.box not in tag_of or w_p in bad_holes:
                            continue
                        sides_p = {abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, w_p.box)], oriented=False)
                                   } - {w_p.ring}
                        if w_p.box in past_cells or (ST.BOX_OWNS_SIDES and sides_p & past_sides
                                                      and _beside_ok(hole_rounds, kg_)):
                            bad_holes.append(w_p)
                    for s_c, n_c in sorted(tri_cells.get(kg_, {}).items()):
                        if not n_c or not two_:
                            continue
                        # (the cell's own two-ring hole: the nearest, its box cell round it - a cell's tag is the
                        # mesh's, not the one the box was noted under)
                        at_c = np.asarray(gmsh.model.occ.getCenterOfMass(2, s_c), dtype=float)
                        w_c = min(two_, key=lambda w_: float(np.linalg.norm(w_.centre - at_c)))
                        if float(np.linalg.norm(w_c.centre - at_c)) <= (
                                2.5 if _beside_ok(hole_rounds, kg_) else 0.5) * w_c.R \
                                and w_c not in bad_holes:
                            bad_holes.append(w_c)
                if GATHER:
                    # SHMESH-51f: face by face - the holes of each face whose own fault is its free cells, while that
                    # face has rounds left (another face's refusal on this pass no longer sends this one's whole grid
                    # to one ring: that face is judged again on the next pass)
                    go_ = [w for w in bad_holes if _round_left(hole_rounds, int(w.owner))
                           and why_cut.get(int(w.owner), {}).get("why", "").startswith("its free cells")]
                else:
                    go_ = bad_holes if isinstance(hole_rounds, int) and hole_rounds < ST.BOLT_HOLE_ROUNDS and bad_holes and all(why_cut.get(kg, {}).get("why", "").startswith("its free cells") for kg in hurt) else []  # noqa: E501
                if go_:
                    pending = _NoCut(set(), {}, holes=[[float(x) for x in w.centre] + (
                        [1.0] if ST.KEEP_BOX and KEEP_BOX_BAND else []) for w in go_],
                                     hole_faces=[int(w.owner) for w in go_])
            if hurt and pending is None:
                _grid_first(hurt, why_cut, _two_faces(washers, boxed + gridded), set(grid_faces_made),
                            no_cut, _one_for_grid(washers), _under_faces(washers))
                pending = _NoCut(hurt, why_cut)
            if pending is not None and not GATHER:
                raise pending
        if check_maps and grids:
            # SHMESH-40: a grid with a quad past the solver's own limits (a face mapped where its shape bends the
            # rows too far) is meshed free on the next pass
            bad: set[int] = set()
            free_of: dict[int, set[int]] = {}   # (SHMESH-51f: a grid freed beside a free face: that face's sewn face)
            for t in sorted(grids):
                types, _tags, conns = gmsh.model.mesh.getElements(2, t)
                for et, conn in zip(types, conns, strict=True):
                    if et != 3:
                        bad.add(t)
                        continue
                    cq = index[np.asarray(conn, dtype=np.int64)].reshape(-1, 4)
                    if len(cq) and shell_quality._quad_past(shell_quality.quad_measures(xyz, cq)).any():
                        bad.add(t)
                    elif len(cq) and high is not None and float(np.linalg.norm(
                            xyz[cq] - xyz[np.roll(cq, -1, axis=1)], axis=2).max()) > high:
                        bad.add(t)      # ... or with an edge over the user's longest edge (a chain's short curve)
            # ... and so is every grid beside a free face that came out with an element past them (the grid's counts
            # on their shared curves did not suit that face)
            # (a gridded face's own free cells - a washer's box - are left to the clean-up: their transition is ours)
            # (SHMESH-60: a narrow strip to be closed is no free face: its own elements go)
            for t in sorted(set(tag_of) - set(grids) - set(ring_faces) - grid_cells - set(closing)):
                if not GRIDS_FREED_BESIDE:
                    break
                beside = {int(x) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)
                          for x in gmsh.model.getAdjacencies(1, abs(c))[0] if int(x) in grids} - bad
                if not beside:
                    continue
                past_nodes: set[int] = set()
                types, _tags, conns = gmsh.model.mesh.getElements(2, t)
                for et, conn in zip(types, conns, strict=True):
                    c_ = index[np.asarray(conn, dtype=np.int64)]
                    if et == 3:
                        el = c_.reshape(-1, 4)
                        hit_ = shell_quality._quad_past(shell_quality.quad_measures(xyz, el))
                    elif et == 2:
                        el = c_.reshape(-1, 3)
                        hit_ = shell_quality._tria_past(shell_quality.tria_measures(xyz, el))
                    else:
                        continue
                    past_nodes |= set(el[hit_].ravel().tolist())
                if not past_nodes:
                    continue
                # (a square ring's strips only where such an element stands on their shared curve, SHMESH-45f - one
                # poor element across the face freed all of a ring's strips; every other grid beside it, as before)
                strips = beside & square_cells
                bad |= beside - strips
                for g_b in beside - strips:
                    free_of.setdefault(g_b, set()).add(int(tag_of.get(t, -1)))
                for g_grid in sorted(strips):
                    shared_c = ({abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)}
                                & {abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, g_grid)], oriented=False)})
                    for c_shared in shared_c:
                        on_c, _x, _p = gmsh.model.mesh.getNodes(1, c_shared, includeBoundary=True)
                        if past_nodes & set(index[np.asarray(on_c, dtype=np.int64)].tolist()):
                            bad.add(g_grid)
                            free_of.setdefault(g_grid, set()).add(int(tag_of.get(t, -1)))
                            break
            if bad and pending is not None and pending.holes:
                # SHMESH-51f: gathered with this pass's holes sent to one ring (the run starts again: what it found
                # would be found again) - the cells of faces it does not name, nor beside one (a grid beside a face
                # now with one ring may come out well). Not with grids left out: a face left ungridded changes the
                # counts through the classes it shared, and grids away from it came out well again (lab: two parts
                # of the 80 kept two cells fewer mapped)
                blame = {_face_of_key(k) for k in pending.faces} | set(pending.hole_faces)
                pending.unmap = _cell_keys(gmsh, tag_of, {
                    t for t in bad if tag_of.get(t) not in blame and not any(
                        tag_of.get(int(x)) in blame for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)
                        for x in gmsh.model.getAdjacencies(1, abs(c))[0])}, mm, free_of)
            elif bad and pending is None:
                raise _Unmap(bad, keys=_cell_keys(gmsh, tag_of, bad, mm, free_of) if GATHER else None)
        if pending is not None:
            raise pending
        quads, trias, q_face, t_face = [], [], [], []
        n_made = len(xyz)       # (SHMESH-83: nodes from here on are a ring's, a band's or a cell's own)
        # SHMESH-39: each washer's annulus takes the washer's own ring of quads in place of gmsh's mesh there
        ring_of = {}
        ring_nodes: set[int] = set()        # (SHMESH-62: the rings' nodes, never merged for a short edge)
        for w in rings:
            try:
                pts, ring = ST.ring_mesh(gmsh, w, index, xyz, mm)
            except ValueError as exc:
                # SHMESH-45 (washer guard, per washer): a ring that cannot be built keeps gmsh's own mesh of its annulus
                # (conformal: gmsh meshed it with the face), and the record says why
                logger.info("shell mesh: a washer's own ring was not made (%s)", exc)
                w.info.update(washer=False, why=f"its ring could not be built: {exc}"[:300])
                washer_skipped["its ring could not be built (gmsh's mesh kept there)"] = (
                    washer_skipped.get("its ring could not be built (gmsh's mesh kept there)", 0) + 1)
                continue
            ring_of[w.annulus] = w
            xyz = np.vstack([xyz, pts])
            ring_nodes.update(int(x) for x in np.unique(ring))
            quads.append(ring)
            q_face += [w.owner] * len(ring)
        box_done: set[int] = set()
        band_nodes: set[int] = set()        # SHMESH-51k: every node of a band made (its ring's, its box's, its own)
        band_quads: list = []               # SHMESH-51l: the bands' quads, and their nodes where the band put them
        if ST.FREE_BAND and structure != "free":
            # SHMESH-48q: a free box's cell is our own two-to-one row of quads where its counts and shapes suit (gmsh's
            # paving stays where they do not)
            for w in rings:
                if not w.box or w.annulus not in ring_of or w.box not in tag_of:
                    continue
                try:
                    pts_x, quads_x = ST.box_band(gmsh, w, index, xyz, mm, lim, high)
                except ValueError as exc:
                    w.info["box_band"] = str(exc)[:200]
                    continue
                w.info["box_band"] = "made"
                box_done.add(w.box)
                band_nodes.update(int(x) for x in np.unique(quads_x))
                xyz = np.vstack([xyz, pts_x])
                band_quads.append(np.asarray(quads_x, dtype=np.int64))
                quads.append(quads_x)
                q_face += [w.owner] * len(quads_x)
        trans_done: set[int] = set()
        n_trans = 0
        n_trans_paved = 0
        if ST.TRANSITION_CELLS and ST.FREE_BAND and structure != "free":
            # SHMESH-51o17: a free four-sided cell of a gridded face with sides of n and n + 2k segments: rows of the
            # coarse count and k three-to-one steps, in place of gmsh's paving
            for t_t in _transition_cells(gmsh, tag_of, _cut_faces(cut_of, join_of), grids, rings, box_done,
                                         _cells_of_keys(gmsh, tag_of, {k_ for k_ in unmap if k_[-1:] == (CELL_FIRST_MARK,)}, mm) if GATHER and CELL_FIRST else set()):
                try:
                    pts_t, quads_t = ST.transition_cell(gmsh, t_t, index, xyz, mm, lim, high,
                                                        (sizing or {}).get("low_mm"))
                except ValueError:
                    continue
                if TRANSITION_BOTH_WAYS:
                    kept_t, counts_t = _transition_kept(gmsh, t_t, index, np.vstack([xyz, pts_t]), quads_t, lim,
                                                        (sizing or {}).get("low_mm"), high, _edge_hard(requirements))
                    if not kept_t:
                        n_trans_paved += 1
                        logger.info("shell mesh: a transition cell was left paved (SHMESH-51o42: %s)", counts_t)
                        continue
                trans_done.add(t_t)
                xyz = np.vstack([xyz, pts_t])
                quads.append(quads_t)
                q_face += [tag_of[t_t]] * len(quads_t)
                # (SHMESH-51o23: its nodes pinned and its quads guarded as a band's - checked where it was made, its
                # cut-line nodes were then moved by the relaxing: on the real column an edge came out 15.085 mm, over
                # the user's 15, and a 50-hole plate was sent to one ring for it)
                band_nodes.update(int(x) for x in np.unique(quads_t))
                band_quads.append(np.asarray(quads_t, dtype=np.int64))
                n_trans += 1
        if CELL_FIRST and structure != "free":
            # SHMESH-51o24: each mapped or transition cell of this pass, its key and its outline box (mm), so a fault the
            # face check finds after gmsh is gone can be put on the cell it lies in
            for t_f in sorted(set(grids) | trans_done):
                if t_f in tag_of:
                    b_f = gmsh.model.getBoundingBox(2, t_f)
                    fixable.append((int(tag_of[t_f]), np.asarray(b_f[:3], dtype=float) * mm,
                                    np.asarray(b_f[3:], dtype=float) * mm, _cell_keys(gmsh, tag_of, [t_f], mm)[0],
                                    "transition cell" if t_f in trans_done else "grid cell",
                                    _cell_sides(gmsh, t_f, index, mm)))
        if structure != "free":
            # SHMESH-51o24: the box cells too (not freed for a fault: only told where one lies)
            for w_f in rings:
                if w_f.box and w_f.box in tag_of:
                    b_f = gmsh.model.getBoundingBox(2, w_f.box)
                    cells_told.append((int(tag_of[w_f.box]), np.asarray(b_f[:3], dtype=float) * mm,
                                       np.asarray(b_f[3:], dtype=float) * mm, None,
                                       "band (box cell, " + ("made" if w_f.box in box_done else "paved") + ")",
                                       _cell_sides(gmsh, w_f.box, index, mm)))
        tri_made: list[int] = []
        for t_r, plan_r in sorted(_TRI_PLAN.items()):
            # SHMESH-79: a three-cornered face's own three blocks of quads, in place of gmsh's paving
            if t_r not in tag_of:
                continue
            try:
                pts_r, quads_r = _tri_blocks(gmsh, t_r, plan_r, index, xyz, facts[tag_of[t_r]], mm, lim, high)
            except ValueError as exc:
                logger.info("shell mesh: a three-cornered face kept gmsh's paving (%s)", exc)
                continue
            trans_done.add(t_r)
            tri_made.append(t_r)
            xyz = np.vstack([xyz, pts_r])
            quads.append(quads_r)
            q_face += [tag_of[t_r]] * len(quads_r)
        steps_made: list[int] = []
        steps_failed: list[int] = []
        step_why: list[dict] = []
        for t_s, sides_s in sorted(strip_steps.items()):
            # SHMESH-73: the stepped strips' own rows, in place of gmsh's paving (kept where they will not do)
            try:
                pts_s, quads_s, kept_s = _strip_steps(gmsh, t_s, sides_s, index, xyz, facts[tag_of[t_s]], mm, lim,
                                                      high, (sizing or {}).get("low_mm"))
            except ValueError as exc:
                logger.info("shell mesh: a stepped strip kept gmsh's paving (%s)", exc)
                steps_failed.append(t_s)
                step_why.append({"strip": int(t_s), "made": False, "why": str(exc)[:120]})
                continue
            step_why.append({"strip": int(t_s), "made": True, "quads": int(len(quads_s)), **kept_s})
            trans_done.add(t_s)
            steps_made.append(t_s)
            xyz = np.vstack([xyz, pts_s])
            quads.append(quads_s)
            q_face += [tag_of[t_s]] * len(quads_s)
        if STEP_RETRY and steps_failed:
            # (its paving to the step's counts is worse than its free mesh: the pass again, those faces free)
            for t_s in steps_failed:
                f_s = facts[tag_of[t_s]]
                _STEP_FAILED.append((np.asarray(f_s["centroid"], dtype=float), float(f_s["area"])))
            raise _Restep()
        band_done: set[int] = set()
        for t_b, bd in sorted(band_of.items()):
            try:
                pts_b, quads_b = ST.band_mesh(gmsh, bd, index, xyz, mm, float(rows[tag_of[t_b]]["size_mm"]))
                # (its transition quads are tidied with the rest; the plate is judged on what is delivered)
            except ValueError as exc:
                logger.info("shell mesh: a round plate's band was paved (%s)", exc)
                continue
            band_done.add(t_b)
            xyz = np.vstack([xyz, pts_b])
            quads.append(quads_b)
            q_face += [tag_of[t_b]] * len(quads_b)
        for t, k in sorted(tag_of.items()):
            if t in ring_of or t in band_done or t in box_done or t in trans_done:
                continue
            types, _tags, conns = gmsh.model.mesh.getElements(2, t)
            for et, conn in zip(types, conns, strict=True):
                c = index[np.asarray(conn, dtype=np.int64)]
                if et == 3:
                    c = c.reshape(-1, 4)
                    quads.append(c)
                    q_face += [k] * len(c)
                elif et == 2:
                    c = c.reshape(-1, 3)
                    trias.append(c)
                    t_face += [k] * len(c)
                else:
                    raise ValueError(f"gmsh made an element of type {et} on a shell face")
        # SHMESH-60: the node pairs across the narrow strips to be closed
        close_pairs = _narrow_pairs(gmsh, closing, index, thin_strips, tag_of, xyz) if closing else []
        # SHMESH-14: the nodes inside a flat face (on no CAD edge) may move within its plane; SHMESH-14c: inside a
        # curved face too, laid back onto its surface
        movable = np.zeros(len(xyz), dtype=bool)
        on_face = np.full(len(xyz), -1, dtype=np.int64)
        for t in sorted(tag_of):
            plane = gmsh.model.getType(2, t) == "Plane"
            if t in ring_of:
                continue        # SHMESH-39: a washer's own nodes stay where the ring put them
            if plane or RELAX_CURVED:
                inner, _c, _p = gmsh.model.mesh.getNodes(2, t, includeBoundary=False)
                if len(inner):
                    at = index[np.asarray(inner, dtype=np.int64)]
                    movable[at] = True
                    if not plane:
                        on_face[at] = tag_of[t]
        # SHMESH-28: which nodes lie on a CAD edge (and on which) or at a CAD vertex - those the mend never takes off
        node_kind = np.zeros(len(xyz), dtype=np.int8)
        node_edge = np.full(len(xyz), -1, dtype=np.int64)
        vertex_edges: dict[int, set[int]] = {}
        # SHMESH-28e: the user's shortest edge and the CAD edges that force edges under it
        low_sm = (sizing or {}).get("low_mm")
        low_sm = float(low_sm) if low_sm is not None else None
        forced_curves: set[int] = set()
        # SHMESH-14d: the nodes on a straight CAD edge may slide along it (between its two ends)
        on_line = np.zeros(len(xyz), dtype=bool)
        line_a = np.zeros((len(xyz), 3))
        line_b = np.zeros((len(xyz), 3))
        if MEND and lim.aims:
            for _d, c in gmsh.model.getEntities(1):
                inner, _c, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=False)
                at = index[np.asarray(inner, dtype=np.int64)]
                node_kind[at] = shell_quality.ON_EDGE
                node_edge[at] = int(c)
                points = [abs(int(p)) for _dd, p in gmsh.model.getBoundary([(1, c)], oriented=False)]
                for p in points:
                    ends, _c, _p = gmsh.model.mesh.getNodes(0, p)
                    for v in index[np.asarray(ends, dtype=np.int64)].tolist():
                        node_kind[v] = shell_quality.AT_VERTEX
                        vertex_edges.setdefault(int(v), set()).add(int(c))
                if SHORT_MEND and low_sm is not None:
                    # SHMESH-28e: the CAD edges along which an edge under the shortest edge is the part's own doing
                    len_c = float(gmsh.model.occ.getMass(1, c)) * mm
                    r_c = _arc_radius(gmsh, int(c))
                    if ((len(points) == 2 and len_c < low_sm)
                            or (r_c is not None and 2.0 * math.pi * r_c * mm / SR.HOLE_PER_TURN < low_sm)
                            or (len(points) < 2 and len_c / SR.HOLE_PER_TURN < low_sm)):
                        forced_curves.add(int(c))
                if SLIDE_EDGES and len(at) and len(points) == 2 and gmsh.model.getType(1, c) == "Line":
                    on_line[at] = True
                    line_a[at] = np.asarray(gmsh.model.getValue(0, points[0], []), dtype=float) * mm
                    line_b[at] = np.asarray(gmsh.model.getValue(0, points[1], []), dtype=float) * mm
        if cut_curves:
            # SHMESH-41: the nodes on the lines the grid was cut along are inside the CAD face: they relax and mend as
            # the face's own inside nodes do (in its plane); the ends of a cut on a CAD edge stay on it
            have = {int(x) for _d, x in gmsh.model.getEntities(1)}
            on_cad: set[int] = set()
            for cv in sorted(have - cut_curves):
                along, _c, _p = gmsh.model.mesh.getNodes(1, cv, includeBoundary=True)
                on_cad |= set(index[np.asarray(along, dtype=np.int64)].tolist())
            for cv in sorted(cut_curves & have):
                along, _c, _p = gmsh.model.mesh.getNodes(1, cv, includeBoundary=True)
                for v in index[np.asarray(along, dtype=np.int64)].tolist():
                    if v in on_cad:
                        continue
                    movable[v] = True
                    node_kind[v] = 0
                    node_edge[v] = -1
                    on_line[v] = False
                    vertex_edges.pop(int(v), None)
        band_made = ((np.vstack(band_quads), np.array(xyz)) if BAND_GUARD and band_quads else None)
        xyz_made = np.array(xyz) if EDGE_GUARD else None  # (SHMESH-51o26: every node where the mesh was made)
        if BAND_PINNED and band_nodes:
            # SHMESH-51k: a band's nodes stay where the band put them (as a ring's do): it was checked there against
            # the solver's limits, the quad lines and the user's longest edge, and the relaxing and mending after it
            # moved its box's side nodes (cut lines, free to move in the face's plane) - on the real column a corner
            # kite of a 56 mm box made at 14.4 mm came out 16.02 mm, over the 15 mm line, and its hole was sent to one
            # ring for it
            at_b = np.fromiter(sorted(band_nodes), dtype=np.int64)
            movable[at_b] = False
            on_line[at_b] = False
            node_kind[at_b] = shell_quality.AT_VERTEX
            node_edge[at_b] = -1
        if log is not None:
            log.extend(gmsh.logger.get())
        if _FIT_FACES:
            # (SHMESH-77: each fit-mapped face's neighbours, read while the model is here)
            tag_by_k = {k_: t_ for t_, k_ in tag_of.items()}
            for kf in sorted(set(_FIT_FACES)):
                t_fit = tag_by_k.get(kf)
                near_f = {kf}
                if t_fit is not None:
                    for _d, c_ in gmsh.model.getBoundary([(2, t_fit)], oriented=False):
                        near_f |= {tag_of[s_] for s_ in gmsh.model.getAdjacencies(1, abs(c_))[0] if s_ in tag_of}
                _FIT_NEAR[kf] = near_f
        if STEP_PER_STRIP and steps_made:
            # (SHMESH-90: each stepped strip's neighbours, read while the model is here)
            for t_st in steps_made:
                if t_st not in tag_of:
                    continue
                near_st: set[int] = {tag_of[t_st]}
                for _d, c_ in gmsh.model.getBoundary([(2, t_st)], oriented=False):
                    near_st |= {tag_of[s_] for s_ in gmsh.model.getAdjacencies(1, abs(c_))[0] if s_ in tag_of}
                _STEP_NEAR[tag_of[t_st]] = near_st
    finally:
        gmsh.finalize()
    if redraw_now is not None:
        # SHMESH-48l2: holes are to be drawn again and nothing after this could end the pass otherwise (no face is
        # judged before its O-grid holes are drawn again, no grid waits for its free mesh): the redraw is made now,
        # with the same lists the whole pass would give, and the pass stops here (none drawn: it goes on whole)
        drawn_e = [np.asarray(c, dtype=float) for c, _e in hole_before]
        want_e = [w for w in washers_planned if _wants_redraw(w, drawn_e)]
        seam_e = [w for w in gridded if not any(float(np.linalg.norm(c - w.centre)) <= 0.25 * w.r for c in drawn_e)]
        judged_e = polar_faces_made | (grid_faces_made if ST.GRID_CHECK else set())
        judged_now_e = bool(judge_faces and ST.POLAR_CHECK and judged_e and not (ST.RESEAT and seam_e))
        comparing = bool(compare and judge_faces and any(k not in grid_faces_made and "free_figures" not in g
                                                         for k, g in compare.items()))
        if (want_e or seam_e) and not judged_now_e and not comparing and redraw_now(
                [[float(x) for x in w.centre] for w in want_e], [([float(x) for x in w.centre], list(w.seam))
                                                                 if w.seam else [float(x) for x in w.centre]
                                                                 for w in seam_e],
                [int(w.owner) for w in want_e], [int(w.owner) for w in seam_e]):
            raise _Redraw()
    Q = np.vstack(quads) if quads else np.zeros((0, 4), dtype=np.int64)
    T = np.vstack(trias) if trias else np.zeros((0, 3), dtype=np.int64)
    # SHMESH-80: an element gmsh wrote twice (two faces' meshes folded onto each other) is left once or not at all
    Q, T, q_face, t_face, _twins = shell_quality.drop_twins(Q, T, q_face, t_face)
    narrow_closed: list[int] = []
    if close_pairs:
        # SHMESH-60: the strips narrower than the user's shortest edge closed to their middle lines; the nodes made
        # there stay put (as a CAD vertex's: the relax and mend never move them)
        xyz, Q, T, q_face, t_face, new_nodes, narrow_closed = _close_narrow(
            xyz, Q, T, q_face, t_face, close_pairs, facts, mm, limits=lim, high_mm=(sizing or {}).get("high_mm"))
        if new_nodes:
            at_n = np.asarray(new_nodes, dtype=np.int64)
            movable[at_n] = False
            on_face[at_n] = -1
            on_line[at_n] = False
            node_kind[at_n] = shell_quality.AT_VERTEX
            node_edge[at_n] = -1
    # SHMESH-84: the user's edge lines go with the limits in force (the relaxing and the mend count them apart)
    lim = shell_quality.with_edges(lim, ((sizing or {}).get("low_mm"), _low_tolerant(sizing, requirements),
                                         (sizing or {}).get("high_mm"), _high_tolerant(sizing, requirements)))
    # SHMESH-83: the nodes no repair moves or cuts round (rings, bands, grid cells, mapped strips, transition cells)
    structured = {tag_of[t] for t in set(grids) | set(mapped) if t in tag_of}
    pinned = shell_repair.pinned_nodes(len(xyz), Q, q_face, n_made, band_nodes, structured)
    # SHMESH-86b: and the mend spends no allowance on those faces, nor on a washer's or band's own
    held_faces = structured | ({q_face[i] for i in np.flatnonzero(pinned[Q].any(axis=1))} if len(Q) else set())
    held_q = np.isin(np.asarray(q_face, dtype=np.int64), np.fromiter(sorted(held_faces), dtype=np.int64)) if len(Q) \
        else np.zeros(0, dtype=bool)
    lim = shell_quality.with_protect(lim, held_faces, np.unique(Q[held_q]).tolist() if len(Q) else ())
    repaired = {"moved": 0, "repaired": 0}
    merged = 0
    if COLLAPSE_SHORT:
        # SHMESH-12: an edge under the floor, far shorter than its elements' others, is a CAD sliver: one node
        limit = np.array([plan_rules.FLOOR_WALLS * r["wall_mm"] * min(sz, 1.0) / mm for r in rows])
        if MAPPED_SLIVER_SHARE:
            # SHMESH-38: a mapped strip's edges (SHMESH-16) are slivers only under SHORT_SHARE of the floor. Its
            # elements are stretched up to STRETCH_MAX = 1 / SHORT_SHARE times along it, so the plain test met them
            # exactly: every edge across a rounded corner was merged, pulling the walls' nodes into the hollow
            for t_m in mapped:
                limit[tag_of[t_m]] *= shell_mesh.SHORT_SHARE
            if STEP_PATTERNS:
                # (SHMESH-90: a stepped strip's own rows likewise - its edges are inside the user's edges)
                for t_m in steps_made:
                    limit[tag_of[t_m]] *= shell_mesh.SHORT_SHARE
        Q, T, q_face, t_face, merged = shell_mesh.collapse_short(xyz / mm, Q, T, q_face, t_face, limit)
    relaxed = 0
    project = _projector(facts, on_face, mm) if RELAX_CURVED and (on_face >= 0).any() else None
    if on_line.any():
        # SHMESH-14d (under the user's limits): the nodes on straight CAD edges relax too, each kept on its edge
        project = _slider(project, on_line, line_a, line_b)
        movable = movable | on_line
    lim_relax = lim
    if RELAX_SHORT and lim.aims and (sizing or {}).get("low_mm") is not None:
        # SHMESH-66: the relaxing aims at the user's shortest edge too (an element with an edge under it weighs as one
        # in a band): a node moves to lengthen a short edge where nothing gets worse - no element is cut or merged
        # for it (the mend's merges turned quads into triangles to do the same)
        low_r = float((sizing or {})["low_mm"])
        lim_relax = shell_quality.InForce(hard=lim.hard, aims=tuple(lim.aims) + (
            ("quad", "min_edge", ">=", low_r, None, 0.0), ("tria", "min_edge", ">=", low_r, None, 0.0)))
    if RELAX_ROUNDS:
        # the neighbours' middle first; then also where each quad round a node would be a parallelogram (SHMESH-14b),
        # for what the middle left past a limit (tried at once, the parallelogram moves mended a quad and left a
        # sliver triangle beside it that the middle had mended)
        xyz, relaxed = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, parallelogram=False,
                                                project=project, limits=lim_relax)
        if shell_quality.RELAX_PARALLELOGRAM:
            xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, parallelogram=True,
                                                  project=project, limits=lim_relax)
            relaxed += again
    split = 0
    if SPLIT_PAST:
        # SHMESH-83: a quad past Nastran's own limits is repaired first where a repair leaves no line worse
        xyz, Q, T, q_face, t_face, got_r = shell_repair.repair_quads(
            xyz, Q, T, q_face, t_face, movable, pinned, shell_repair.Lines(lim), stage="past", project=project,
            on_face=on_face, on_line=on_line)
        repaired = {k: repaired[k] + got_r[k] for k in repaired}
        # SHMESH-10: a quad past Nastran's own limits becomes two triangles
        keep, cut, came = shell_quality.split_past_quads(xyz, Q, lim)
        if len(cut):
            qf = np.array(q_face, dtype=np.int64)
            T = np.vstack([T, cut])
            t_face = t_face + qf[came].tolist()
            Q, q_face = Q[keep], qf[keep].tolist()
            split = len(came) // 2
        if COLLAPSE_SHORT and split:
            # a quad folded nearly flat (two opposite corners a sliver apart) cuts into two slivers along that short
            # diagonal: it closes up, its edges zipped pairwise (SHMESH-12 on the cut)
            Q, T, q_face, t_face, again = shell_mesh.collapse_short(xyz / mm, Q, T, q_face, t_face, limit)
            merged += again
            if again:
                # the merge can leave a neighbouring quad past a limit (a corner pulled onto the merged node):
                # SHMESH-10 once more on what it left
                keep, cut, came = shell_quality.split_past_quads(xyz, Q, lim)
                if len(cut):
                    qf = np.array(q_face, dtype=np.int64)
                    T = np.vstack([T, cut])
                    t_face = t_face + qf[came].tolist()
                    Q, q_face = Q[keep], qf[keep].tolist()
                    split += len(came) // 2
        # SHMESH-80: two quads round a node only they share, cut along the same diagonal, write one triangle twice
        Q, T, q_face, t_face, _twins = shell_quality.drop_twins(Q, T, q_face, t_face)
    aimed = 0
    if lim.aims and kind != "quads" and len(Q):
        # SHMESH-25: a quad past one of the USER's limits is cut only when both triangles are inside every limit,
        # and only as far as the share the user allows needs (never when they asked for quads); SHMESH-83: repaired
        # first where a repair leaves no line worse
        xyz, Q, T, q_face, t_face, got_r = shell_repair.repair_quads(
            xyz, Q, T, q_face, t_face, movable, pinned, shell_repair.Lines(lim), stage="aimed", project=project,
            on_face=on_face, on_line=on_line)
        repaired = {k: repaired[k] + got_r[k] for k in repaired}
        keep, cut, came = shell_quality.split_aimed_quads(xyz, Q, len(Q) + len(T), lim)
        if len(cut):
            qf = np.array(q_face, dtype=np.int64)
            T = np.vstack([T, cut])
            t_face = t_face + qf[came].tolist()
            Q, q_face = Q[keep], qf[keep].tolist()
            aimed = len(came) // 2
    flipped = 0
    if FLIP_PAST and len(T):
        # SHMESH-13: a triangle past a limit swaps its longest edge with its neighbour's when both come out better
        T, flipped = shell_quality.flip_past_trias(xyz, T, t_face, limits=lim)
    if RELAX_ROUNDS and (split or aimed) and len(T):
        # SHMESH-14 once more, after the cut, for the triangles it left past a limit; no quad may come out past one
        # (a quad past a limit outweighs every triangle round a node)
        xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, weights=(64, 1),
                                              project=project, limits=lim)
        relaxed += again
    paired = 0
    if PAIR_TRIAS and len(T) > 1 and kind != "triangles":
        # SHMESH-17: two triangles of a face that make a quad inside Nastran's limits become that quad
        new_q, kept, came = shell_quality.pair_trias(xyz, Q, T, t_face, lim)
        if len(new_q):
            tf = np.array(t_face, dtype=np.int64)
            Q = np.vstack([Q, new_q])
            q_face = q_face + tf[came].tolist()
            T, t_face = T[kept], tf[kept].tolist()
            paired = len(new_q)
    walked = 0
    if WALK_DEPTH and len(T) > 1 and kind != "triangles":
        # SHMESH-18: a triangle walked through its face's quads to the nearest other triangle, the two made one quad
        Q, T, q_face, t_face, walked = shell_quality.walk_trias(xyz, Q, T, q_face, t_face, depth=WALK_DEPTH,
                                                                limits=lim)
    capped = 0
    if ABSORB_CAPS and len(T):
        # SHMESH-22: a cap left past a limit (all its corners on CAD edges) taken into the element across its longest
        # edge and cut again inside the limits, or dropped from a free edge
        face_wall = np.array([float(r["wall_mm"]) for r in rows])
        Q, T, q_face, t_face, capped = shell_quality.absorb_caps(xyz, Q, T, q_face, t_face, face_wall, limits=lim,
                                                                 quads_too=kind != "triangles")
    mended = {"merged": 0, "recut": 0, "moved": 0}
    short_mended = None
    if MEND and lim.aims:
        # SHMESH-28: what is still past a hard limit or outside one of the user's limits is mended by merging a
        # node away or cutting two neighbours' outline again; the nodes round a change relax once more, and what
        # that opens is mended again
        # SHMESH-29: "ignore edges under X mm" - only when the user asks: the ends of a CAD edge shorter than X may be
        # merged too, and every element edge under X counts as outside
        ignore = ((requirements or {}).get("sizes") or {}).get("ignore_edges_mm")
        ignore = float(ignore) if ignore else None
        asked_ignore = ignore
        if ignore is None and SHORT_SUPPRESS and (sizing or {}).get("low_mm") is not None:
            # SHMESH-62: a CAD edge shorter than the user's shortest edge is one the mesh may step over (as if they
            # had asked to ignore edges under it): no element can keep it and stay over their shortest edge
            ignore = float((sizing or {})["low_mm"])
        short_cost = SHORT_COST
        # SHMESH-70: under the user's longest edge (stamped steps on), no mend change makes an edge over it
        long_hard = None
        if LONG_GUARD and (sizing or {}).get("high_mm") is not None:
            long_hard = float((sizing or {})["high_mm"]) * (1.0 + LONG_BAND)
        kinds_short = node_kind
        if ignore and not asked_ignore and (ring_nodes or band_nodes):
            # SHMESH-62: a washer's ring and a band are drawn to the user's edges already: no node of theirs is
            # merged away for an edge under the shortest edge
            kinds_short = node_kind.copy()
            kinds_short[np.fromiter(sorted(ring_nodes | band_nodes), dtype=np.int64)] = shell_quality.AT_VERTEX
        if SHORT_FIRST and ignore and not asked_ignore and kind != "triangles":
            # SHMESH-67: the short edges are mended first by changes that leave no more triangles (a merge that takes
            # a triangle's corner, a re-cut, a move); the mend after that weighs the user's lines alone, so a quad is
            # not turned into a triangle for an edge under the shortest edge
            for _ in range(MEND_CYCLES):
                xyz, Q, T, q_face, t_face, got = shell_quality.mend(
                    xyz, Q, T, q_face, t_face, kinds_short, node_edge, vertex_edges, limits=lim, quads_too=True,
                    triangles_too=False, edges=(ignore, (sizing or {}).get("high_mm") if MEND_LONG else None),
                    suppress=ignore, movable=movable if MEND_MOVES else None, project=project,
                    hard_long=long_hard)
                if not (got["merged"] or got["recut"] or got["moved"]):
                    break
                mended = {k: mended[k] + got[k] for k in mended}
                if RELAX_ROUNDS:
                    xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, weights=(64, 1),
                                                          project=project, limits=lim_relax)
                    relaxed += again
            short_cost = False
        t_mend = time.monotonic()
        for _ in range(MEND_CYCLES):
            xyz, Q, T, q_face, t_face, got = shell_quality.mend(
                xyz, Q, T, q_face, t_face, kinds_short if (short_cost and not asked_ignore) else node_kind,
                node_edge, vertex_edges, limits=lim,
                quads_too=kind != "triangles", triangles_too=kind != "quads",
                edges=((ignore if (asked_ignore or short_cost) else None) if ignore
                       else ((sizing or {}).get("low_mm") if MEND_SHORT else None),
                       (sizing or {}).get("high_mm") if MEND_LONG else None),
                suppress=ignore if (asked_ignore or short_cost) else None,
                movable=movable if MEND_MOVES else None, project=project, hard_long=long_hard)
            if not (got["merged"] or got["recut"] or got["moved"]):
                break
            mended = {k: mended[k] + got[k] for k in mended}
            if RELAX_ROUNDS:
                xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, weights=(64, 1),
                                                      project=project, limits=lim_relax)
                relaxed += again
        t_mend = time.monotonic() - t_mend
        if SHORT_MEND and low_sm is not None and not ignore and kind != "triangles":
            # SHMESH-28e: the short-edge mend, after the mend under the user's lines (its mesh is where this starts)
            xyz, Q, T, q_face, t_face, short_mended = _short_mend(
                xyz, Q, T, q_face, t_face, node_kind, node_edge, vertex_edges, lim, low_sm,
                (sizing or {}).get("high_mm") if MEND_LONG else None, forced_curves,
                movable if MEND_MOVES else None, project, long_hard, t_mend, requirements, tri_ok=kind != "quads")
        if REPAIR_AFTER_MEND and len(T) > 1 and kind != "triangles":
            # SHMESH-64: the triangles the mend left (a quad that lost a corner to a merge) are paired and walked once
            # more, as SHMESH-17/18 pair gmsh's: two of a face into a quad no worse under every line
            new_q, kept, came = shell_quality.pair_trias(xyz, Q, T, t_face, lim)
            if len(new_q):
                tf = np.array(t_face, dtype=np.int64)
                Q = np.vstack([Q, new_q])
                q_face = q_face + tf[came].tolist()
                T, t_face = T[kept], tf[kept].tolist()
                paired += len(new_q)
            if WALK_DEPTH and len(T) > 1:
                Q, T, q_face, t_face, w_ = shell_quality.walk_trias(xyz, Q, T, q_face, t_face, depth=WALK_DEPTH,
                                                                    limits=lim)
                walked += w_
    if BAND_GUARD and band_made is not None:
        # SHMESH-51l: a band was checked where it was made; a node of it the relaxing or mending moved goes back there
        # wherever a quad of the band now comes out past the solver's limits or over the user's longest edge (on the
        # real column a corner kite made at 14.4 mm came out 16.02 mm: its box's side node had moved 1.6 mm outward)
        bq_, at_made = band_made
        high_g = (sizing or {}).get("high_mm")
        low_g = (sizing or {}).get("low_mm")
        for _ in range(BAND_GUARD_ROUNDS):
            bad_ = _band_bad(xyz, bq_, at_made, high_g, low_g)
            if not bad_.any():
                break
            nodes_ = np.unique(bq_[bad_])
            nodes_ = nodes_[np.linalg.norm(xyz[nodes_] - at_made[nodes_], axis=1) > 0.0]
            if not len(nodes_):
                break
            xyz = np.array(xyz)
            xyz[nodes_] = at_made[nodes_]
    high_e = (sizing or {}).get("high_mm")
    if EDGE_GUARD and xyz_made is not None and high_e is not None:
        # SHMESH-51o26: the user's longest edge on every delivered element - an element the relaxing and mending left
        # with an edge over it has its moved nodes put back where the mesh made them, one node at a time, where that
        # brings no element past the solver's limits (an independent read of the real column's deck found two
        # elements to 17 mm on faces no check judged)
        for _ in range(EDGE_GUARD_ROUNDS):
            put = 0
            for els in (Q, T):
                if not len(els):
                    continue
                ed = np.linalg.norm(xyz[els] - xyz[np.roll(els, -1, axis=1)], axis=2).max(axis=1)
                for e in np.flatnonzero(ed > float(high_e)):
                    for v_ in els[e]:
                        v_ = int(v_)
                        if v_ >= len(xyz_made) or not np.linalg.norm(xyz[v_] - xyz_made[v_]) > 0.0:
                            continue
                        near_q = Q[(Q == v_).any(axis=1)] if len(Q) else Q
                        near_t = T[(T == v_).any(axis=1)] if len(T) else T
                        before = (int(shell_quality._quad_past(shell_quality.quad_measures(xyz, near_q)).sum())
                                  if len(near_q) else 0) + (int(shell_quality._tria_past(
                                      shell_quality.tria_measures(xyz, near_t)).sum()) if len(near_t) else 0)
                        keep = xyz[v_].copy()
                        xyz = np.array(xyz)
                        xyz[v_] = xyz_made[v_]
                        after = (int(shell_quality._quad_past(shell_quality.quad_measures(xyz, near_q)).sum())
                                 if len(near_q) else 0) + (int(shell_quality._tria_past(
                                     shell_quality.tria_measures(xyz, near_t)).sum()) if len(near_t) else 0)
                        if after > before:
                            xyz[v_] = keep
                        else:
                            put += 1
            if not put:
                break
    # SHMESH-80: and once more on what every step left
    Q, T, q_face, t_face, _twins = shell_quality.drop_twins(Q, T, q_face, t_face)
    # SHMESH-51o34: why each node may or may not move, kept through the renumbering (the record tells it per triangle
    # under the user's angle target, on the mesh as delivered)
    node_why = _node_why(len(xyz), movable, on_face, node_kind, band_nodes)
    # keep only the nodes the elements use, renumbered in order
    used = np.unique(np.concatenate([Q.ravel(), T.ravel()]))
    remap = np.full(len(xyz), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    Q, T, xyz = remap[Q], remap[T], xyz[used]
    node_why = [node_why[int(u)] for u in used]
    face_of = np.array(q_face + t_face, dtype=np.int64)
    seam_due = ST.RESEAT and any(not any(float(np.linalg.norm(np.asarray(c, dtype=float) - w.centre)) <= 0.25 * w.r
                                         for c, _e in hole_before) for w in gridded)
    if compare and judge_faces:
        # SHMESH-48x: each grid refused for its short edges or triangles, now meshed free: its grid comes back unless
        # the free mesh is better on that face (short: the grid's share over GRID_SHORT and over GRID_SHORT_RATIO x the
        # free mesh's; triangles: the grid's over max(GRID_TRIAS, GRID_TRIA_SHARE, 2 x the free mesh's))
        back: dict[int, dict] = {}
        for k_c, g_c in sorted(compare.items()):
            if k_c in grid_faces_made or "free_figures" in g_c:
                continue
            free_ = _face_figures(xyz, Q, T, q_face, t_face, k_c, _low_tolerant(sizing, requirements))
            g_c["free_figures"] = free_
            if not free_["elements"]:
                continue
            ok_, why_ = _grid_back(g_c, free_)
            if ok_:
                back[k_c] = {"grid_figures": dict(g_c), "free_figures": free_}
                if why_:
                    back[k_c]["why_back"] = why_
        if back:
            drawn_ = [np.asarray(c, dtype=float) for c, _e in hole_before]
            raise _Regrid(set(back), back, [([float(x) for x in w.centre], int(w.owner)) for w in washers_planned
                                            if _wants_redraw(w, drawn_)])
    judged = polar_faces_made | (grid_faces_made if ST.GRID_CHECK else set())
    judged_now = bool(judge_faces and ST.POLAR_CHECK and judged and not seam_due)
    if judged_now:
        # (judged on every main pass, not only while SHMESH-40 has rounds left: a part with many grids spent them,
        # the last pass went unjudged and a real base plate kept 10 triangles; each face is refused at most once)
        # (judged only once its O-grid holes start where they are cut: the pass before is drawn again anyway)
        # SHMESH-49g: a round plate whose rings and strips end with more than POLAR_TRIAS triangles on it (the
        # clean-up split quads past the limits there) is meshed on the next pass with its free-face washers
        on_plate = Counter(int(k) for k in t_face if int(k) in judged)
        n_on = Counter(int(k) for k in list(q_face) + list(t_face) if int(k) in judged)
        # (a round plate: at most POLAR_TRIAS triangles; a gridded face is held to the lines only - its free
        # boxes' triangles were there before it was judged)
        allowed_p = {k: max(ST.POLAR_TRIAS, int(ST.POLAR_TRIA_SHARE * n_on.get(k, 0))) for k in polar_faces_made}
        hurt_p = {k for k, n in on_plate.items() if k in polar_faces_made and n > allowed_p[k]}
        # ... and every triangle on it inside the tria angle lines (the user's, or 30 and 120 degrees)
        angle_lines = [(key, op, target) for shape, key, op, target, *_r in lim.aims
                       if shape == "tria" and key in ("min_angle_tria", "max_angle_tria")] or [
            ("min_angle_tria", ">=", 30.0), ("max_angle_tria", "<=", 120.0)]
        tk_ = np.asarray(t_face, dtype=np.int64)
        for k in polar_faces_made - hurt_p:
            mine_t = T[tk_ == k]
            if len(mine_t):
                m_t = shell_quality.tria_measures(xyz, mine_t)
                if any(((m_t[key] < target) if op.startswith(">") else (m_t[key] > target)).any()
                       for key, op, target in angle_lines):
                    hurt_p.add(k)
        # SHMESH-49g/48f: why each face is refused, with its numbers (the record lists them)
        why_p: dict[int, dict] = {}
        tfk = np.asarray(t_face, dtype=np.int64)
        for k in judged:
            at_ = [[round(float(x), 1) for x in xyz[T[j]].mean(axis=0)] for j in np.nonzero(tfk == k)[0][:20]]
            why_p[k] = {"kind": "rings and strips" if k in polar_faces_made else "grid",
                        "triangles": int(on_plate.get(k, 0)), "triangles_at_mm": at_,
                        "triangles_allowed": allowed_p.get(k) if k in polar_faces_made else None,
                        "elements": int(n_on.get(k, 0))}
            if k in hurt_p:
                why_p[k]["why"] = ("too many triangles" if on_plate.get(k, 0) > allowed_p.get(k, 0)
                                   else "a triangle past the tria angle lines")
        # ... or with an element on it past the solver's own limits, or an edge over the user's longest edge (a line
        # the report holds to every element): the plate's free-face washers met them
        high_p = (sizing or {}).get("high_mm")
        # SHMESH-51o29: the user's edge lengths are targets unless they said hard - an edge within EDGE_TOL outside is
        # no fault, and a face is refused for edge length only past EDGE_SHARE of its elements outside that band
        hard_lo, hard_hi = _edge_hard(requirements)
        tol_e = 0.0 if hard_hi else EDGE_TOL
        high_f = float(high_p) * (1 + tol_e) if high_p is not None else None
        low_f = _low_tolerant(sizing, requirements)
        share_e = 0.0 if hard_hi else EDGE_SHARE           # (the longest edge's share)
        share_lo = 0.0 if hard_lo else EDGE_SHARE          # (the shortest edge's)
        for els, faces_, measure, past_fn in ((Q, q_face, shell_quality.quad_measures, shell_quality._quad_past),
                                              (T, t_face, shell_quality.tria_measures, shell_quality._tria_past)):
            if not len(els):
                continue
            fk = np.asarray(faces_, dtype=np.int64)
            for kp in judged - hurt_p:
                mine = els[fk == kp]
                if not len(mine):
                    continue
                longest = np.linalg.norm(xyz[mine] - xyz[np.roll(mine, -1, axis=1)], axis=2).max()
                past_ = past_fn(measure(xyz, mine))
                if past_.any():
                    hurt_p.add(kp)
                    why_p[kp].update(why="elements past the solver's own limits", past=int(past_.sum()),
                                     past_at_mm=[[round(float(x), 1) for x in xyz[e].mean(axis=0)]
                                                 for e in mine[past_][:10]])
                elif high_f is not None and float(longest) > high_f and int((np.linalg.norm(
                        xyz[mine] - xyz[np.roll(mine, -1, axis=1)], axis=2).max(axis=1) > high_f).sum()) > share_e * int(
                        n_on.get(kp, len(mine))):
                    hurt_p.add(kp)
                    ed_ = np.linalg.norm(xyz[mine] - xyz[np.roll(mine, -1, axis=1)], axis=2).max(axis=1)
                    why_p[kp].update(why="an edge over the user's longest edge", line="max_edge",
                                     worst=round(float(longest), 3), limit=float(high_p or 0.0), tolerance=tol_e,
                                     offender=_offender(xyz[mine[int(np.argmax(ed_))]], kp, fixable + cells_told,
                                                        "quad" if mine.shape[1] == 4 else "triangle"))
        # SHMESH-48x: ... or a gridded face over GRID_SHORT of its elements under the user's shortest edge, or with
        # more triangles than max(GRID_TRIAS, GRID_TRIA_SHARE of its elements): meshed free on the next pass and
        # compared there (a grid that left 105 triangles on a plate its free mesh gave 6 is no expert's mesh)
        if ST.GRID_GUARD:
            for kp in sorted(set(grid_faces_made) & judged - hurt_p - set(grids_kept)):
                figs = _face_figures(xyz, Q, T, q_face, t_face, kp, low_f)
                over_short = (figs["short"] > (max(ST.GRID_SHORT, share_lo) if not hard_lo else ST.GRID_SHORT)
                              * figs["elements"] and low_f is not None)
                over_tri = figs["triangles"] > max(ST.GRID_TRIAS, ST.GRID_TRIA_SHARE * figs["elements"])
                if (GRID_ZERO_KEEP and GRID_ZERO_TRIA and over_short and not over_tri and figs["triangles"] == 0
                        and any(int(w.owner) == kp and not w.corners for w in washers_planned)):
                    # SHMESH-51o21: a grid with no triangle refused for its short edges alone, on a face with holes,
                    # is not meshed free to be set against its free mesh: that comes back with triangles round the
                    # holes and the grid back (SHMESH-48x3) - a pass and a pass back for the same mesh (about 65 s on
                    # the real column; every such trial in the lab bars ended with the grid back)
                    zero_kept.append({"face": _owner_of_key(kp, owners), "grid_figures": figs})
                    continue
                if over_short or over_tri:
                    hurt_p.add(kp)
                    why_p[kp].update(why=("too many elements under the user's shortest edge" if over_short
                                          else "more triangles than a grid may keep") + " (set against its free mesh)",
                                     grid_figures=figs)
        if requirements and requirements.get("limits"):
            # ... or a quality line of the user's not met on the plate's own elements (the report's own rule)
            import types as _types

            qk, tk = np.asarray(q_face, dtype=np.int64), np.asarray(t_face, dtype=np.int64)
            for kp in judged - hurt_p:
                plate_q: dict = shell_quality.quad_measures(xyz, Q[qk == kp], extra=True) if (qk == kp).any() else {}
                plate_t: dict = shell_quality.tria_measures(xyz, T[tk == kp], extra=True) if (tk == kp).any() else {}
                sub = _types.SimpleNamespace(n_elements=int((qk == kp).sum() + (tk == kp).sum()))
                for lim_ in requirements["limits"]:
                    row_ = SR._limit_row(lim_, sub, plate_q, plate_t)
                    if row_.get("applied") and row_.get("met") is not True:
                        hurt_p.add(kp)
                        why_p[kp].update(why="a quality line not met on its own elements",
                                         line=str(lim_.get("measure")), outside=row_.get("outside"),
                                         outside_pct=row_.get("outside_pct"), worst=row_.get("worst"))
                        break
        # SHMESH-51b: a gridded face holding two-ring bolt holes keeps its grid and gives up the second ring first
        if hurt_p and ST.BOLT_BAND_LONG and (GATHER or (isinstance(hole_rounds, int) and hole_rounds < ST.BOLT_HOLE_ROUNDS)):
            # SHMESH-51d: on a gridded face refused while some of its two-ring bolt holes' bands were not made (their box
            # cells paved), those holes alone take one ring and the face is meshed again; its other holes keep two
            # (SHMESH-51f: each face while it has rounds of its own left)
            bad_b = [w for w in boxed if w.radii and int(w.owner) in hurt_p and int(w.owner) in grid_faces_made
                     and w.info.get("box_band") != "made" and _round_left(hole_rounds, int(w.owner))]
            why_b = {id(w): "its band was not made (" + str(w.info.get("box_band"))[:80] + ")" for w in bad_b}
            # (SHMESH-51j: the holes that keep their box - a fault in their rings; with KEEP_BOX_BAND a band not made too:
            # its box cell is then paved round one ring, its sides and every line of its face as they were)
            ring_b: set[int] = {id(w) for w in bad_b} if ST.KEEP_BOX and KEEP_BOX_BAND else set()
            cell_keys_b: list = []          # (SHMESH-51o28: grid cells beside boxes freed first)
            cell_told_b: list[dict] = []
            if GATHER and BOX_BLAME:
                # SHMESH-51g/51h: in the same round, every element the face was refused for that lies in or beside a
                # two-ring bolt hole's box (a cell there paved, its counts not meeting the box's) puts that hole to one
                # ring too: elements past the solver's limits or over the user's longest edge, and for a face refused
                # against its free mesh its triangles and its edges under the user's shortest edge. The face keeps its
                # grid and its other rings; a fault on no hole's box is judged on the next pass (SHMESH-51b if alone)
                for kp in sorted(hurt_p & set(grid_faces_made)):
                    why_k = str(why_p.get(kp, {}).get("why"))
                    guard = why_k.endswith("(set against its free mesh)")
                    if why_k not in ("elements past the solver's own limits", "an edge over the user's longest edge") \
                            and not guard:
                        continue
                    two_k = [w for w in boxed if w.radii and int(w.owner) == kp and len(w.box_corners) == 4
                             and _round_left(hole_rounds, kp)]
                    at_k = []
                    for els, faces_, measure, past_fn in ((Q, q_face, shell_quality.quad_measures,
                                                           shell_quality._quad_past),
                                                          (T, t_face, shell_quality.tria_measures,
                                                           shell_quality._tria_past)):
                        mine = els[np.asarray(faces_, dtype=np.int64) == kp] if len(els) and two_k else els[:0]
                        if len(mine):
                            ed = np.linalg.norm(xyz[mine] - xyz[np.roll(mine, -1, axis=1)], axis=2)
                            if guard:
                                # (SHMESH-51i: its triangles only - a paved cell's; short edges stand in mapped,
                                # clean cells too, and a hole whose box and cells round it are mapped is not to blame)
                                flag = np.full(len(mine), mine.shape[1] == 3)
                            else:
                                flag = past_fn(measure(xyz, mine))
                                if high_f is not None:
                                    flag = flag | (ed.max(axis=1) > high_f)
                            at_k.extend(xyz[mine[flag]].mean(axis=1))
                    at_a = np.asarray(at_k, dtype=float).reshape(-1, 3)
                    if ST.KEEP_BOX and not _beside_ok(hole_rounds, kp):
                        # SHMESH-51j: only a fault inside a hole's rings is its rings' (one ring in the box it keeps cures
                        # it); one in its band or beside its box stays the face's: fixed where it starts, else SHMESH-51b.
                        # (Where a hole of the face took one ring in a box of its own - its band was not made - the
                        # cells beside that box are its doing, and the holes beside it go as before, SHMESH-51g.)
                        for w in two_k:
                            if w not in bad_b and len(at_a) and bool(
                                    (np.linalg.norm(at_a - w.centre * mm, axis=1) <= w.R * mm * (1 + 1e-6)).any()):
                                bad_b.append(w)
                                ring_b.add(id(w))
                                why_b[id(w)] = f"{why_k}: in its rings"
                        continue
                    if CELL_FIRST and len(at_a) and fixable and len(unmap) < CELL_FIRST_MAX:
                        # SHMESH-51o28: a fault beside a box that lies in a mapped grid cell has that cell meshed free
                        # first - no hole goes to one ring for it (on the real column two of the 50-hole plate's holes
                        # went to one ring, each a start again, for edges over the longest edge in such cells)
                        rest_a = []
                        for c_ in at_a:
                            inside = [f_ for f_ in fixable if f_[0] == kp and f_[4] == "grid cell"
                                      and bool((c_ >= f_[1] - 1e-6).all()) and bool((c_ <= f_[2] + 1e-6).all())]
                            if not inside:
                                rest_a.append(c_)
                                continue
                            f_in = min(inside, key=lambda f_: f_[3][4])
                            key_ = (*f_in[3][:6], CELL_FIRST_MARK)
                            if key_ not in unmap and key_ not in cell_keys_b:
                                cell_keys_b.append(key_)
                                cell_told_b.append({"face": _owner_of_key(kp, owners), "why": f"{why_k}: beside a box",
                                                    "cell": f_in[4], "cell_sides_mm_segments": f_in[5]})
                        at_a = np.asarray(rest_a, dtype=float).reshape(-1, 3)
                    for w in _boxes_at(at_a, two_k, mm, partial=True) or [] if len(at_a) else []:
                        if w not in bad_b:
                            bad_b.append(w)
                            why_b[id(w)] = f"{why_k}: in or beside its box"
            if cell_keys_b:
                raise _Unmap(set(), keys=cell_keys_b, told=cell_told_b)
            if bad_b:
                extra_k: set[int] = set()
                why_x: dict[int, dict] = {}
                if BATCH_BOLT_KEYS:
                    # SHMESH-51o19: the other faces refused on this pass whose bolt holes SHMESH-51b would put to one
                    # ring go with these holes, in the same start again (on the real column they came one start
                    # again later, about 95 s each)
                    rest = {k for k in hurt_p if k >= 0 and k not in {int(w.owner) for w in bad_b}}
                    why_x = {k: dict(why_p[k]) for k in rest}
                    _grid_first(rest, why_x, _two_faces(washers, boxed + gridded), set(grid_faces_made), no_cut,
                                _one_for_grid(washers), _under_faces(washers))
                    extra_k = {k for k in rest if k < 0 and -k - 1 >= UNDER_LOW_KEY}
                raise _NoCut(extra_k, {k: why_x[k] for k in extra_k},
                             holes=[[float(x) for x in w.centre] + ([1.0] if id(w) in ring_b else []) for w in bad_b],
                             hole_faces=[int(w.owner) for w in bad_b], hole_why=[why_b[id(w)] for w in bad_b])
        if CELL_FIRST and fixable and len(unmap) < CELL_FIRST_MAX:
            # SHMESH-51o24: one ring is the last answer - a face holding two-ring bolt holes refused for elements past
            # the solver's limits or over the user's longest edge has the mapped or transition cells those elements lie
            # in meshed free first (on the real column on GF23 one edge 15.085 mm long against the user's 15 sent a
            # 50-hole plate's 48 two-ring holes to one ring); where they lie in no such cell, SHMESH-51b as before
            two_f = _two_faces(washers, boxed + gridded)
            keys_f: list = []
            told_f: list[dict] = []
            for kp in sorted(hurt_p & two_f):
                if str(why_p.get(kp, {}).get("why")) not in ("elements past the solver's own limits",
                                                             "an edge over the user's longest edge"):
                    continue
                at_f = []
                for els, faces_, measure, past_fn in ((Q, q_face, shell_quality.quad_measures,
                                                       shell_quality._quad_past),
                                                      (T, t_face, shell_quality.tria_measures,
                                                       shell_quality._tria_past)):
                    mine = els[np.asarray(faces_, dtype=np.int64) == kp] if len(els) else els[:0]
                    if not len(mine):
                        continue
                    flag = past_fn(measure(xyz, mine))
                    if high_f is not None:
                        ed = np.linalg.norm(xyz[mine] - xyz[np.roll(mine, -1, axis=1)], axis=2)
                        flag = flag | (ed.max(axis=1) > high_f)
                    at_f.extend(xyz[mine[flag]].mean(axis=1))
                for c_ in at_f:
                    inside = [f_ for f_ in fixable if f_[0] == kp and bool((c_ >= f_[1] - 1e-6).all())
                              and bool((c_ <= f_[2] + 1e-6).all())]
                    if inside:
                        f_in = min(inside, key=lambda f_: f_[3][4])          # (the smallest cell round it)
                        key_ = (*f_in[3][:6], CELL_FIRST_MARK)
                        if key_ not in unmap and key_ not in keys_f:
                            keys_f.append(key_)
                            told_f.append({"face": _owner_of_key(kp, owners), "why": str(why_p[kp].get("why")),
                                           "cell": f_in[4], "cell_sides_mm_segments": f_in[5]})
            if keys_f:
                raise _Unmap(set(), keys=keys_f, told=told_f)
        _grid_first(hurt_p, why_p, _two_faces(washers, boxed + gridded), set(grid_faces_made), no_cut,
                    _one_for_grid(washers), _under_faces(washers))
        if hurt_p:
            raise _NoCut(hurt_p, {k: why_p[k] for k in hurt_p})
    if judge_faces and UNDER_LOW_CHECK and not seam_due and lim.aims:
        # SHMESH-51o57: a hole whose two rings were planned under the user's flexible shortest edge keeps them only
        # where every element of its rings is inside every one of the user's targets and the solver's limits - else
        # that hole alone goes back to one ring (on fr020 such rings took quads outside the corner targets, 4 to 27
        # and 0 to 26 on the part); its alike holes keep theirs
        under_w = [w for w in rings if w.radii and w.info.get("rings_under_shortest_edge")]
        if under_w:
            qc_ = xyz[Q].mean(axis=1) if len(Q) else np.zeros((0, 3))
            tc_ = xyz[T].mean(axis=1) if len(T) else np.zeros((0, 3))
            qk_u, tk_u = np.asarray(q_face, dtype=np.int64), np.asarray(t_face, dtype=np.int64)
            bad_u = []
            for w in under_w:
                sel_q = (qk_u == int(w.owner)) & (np.linalg.norm(qc_ - w.centre, axis=1) < UNDER_LOW_REACH * w.R) if len(Q) else []
                sel_t = (tk_u == int(w.owner)) & (np.linalg.norm(tc_ - w.centre, axis=1) < UNDER_LOW_REACH * w.R) if len(T) else []
                out_ = 0
                if len(Q) and np.any(sel_q):
                    mq_ = shell_quality.quad_measures(xyz, Q[sel_q], extra=lim.extra)
                    out_ += int((shell_quality.aim_score(mq_, "quad", lim, int(np.sum(sel_q))) > 0).sum())
                    out_ += int(shell_quality._quad_past(mq_).sum())
                if len(T) and np.any(sel_t):
                    mt_ = shell_quality.tria_measures(xyz, T[sel_t], extra=lim.extra)
                    out_ += int((shell_quality.aim_score(mt_, "tria", lim, int(np.sum(sel_t))) > 0).sum()) + int(
                        np.sum(sel_t))       # (a triangle in a ring is no ring)
                if out_:
                    bad_u.append(w)
            if bad_u:
                raise _NoCut(set(), {}, holes=[[float(x) for x in w.centre] for w in bad_u],
                             hole_faces=[int(w.owner) for w in bad_u],
                             hole_why=["its rings under the shortest edge left elements outside a target"] * len(bad_u))
    if (judge_faces and UNDER_LOW_COMPARE and ul_state is not None and not seam_due and lim.aims
            and not _edge_hard(requirements)[0]):
        # (only with the shortest edge flexible: a hard one plans no ring under it, SHMESH-51o52)
        # SHMESH-51o58: the rule held everywhere - no line worse. A face with rings under the shortest edge is set
        # against the same face with those holes on one ring; where a line of it is worse, the holes nearest the
        # elements that make it go back to one ring, until no line is worse or none of those holes is left.
        # SHMESH-51o59, the cost: this pass finishes as it is and is kept; ONE mesh with every such face on one ring
        # follows (key -k-1-UNDER_LOW_KEY for all of them), measured, and where no face is worse the kept mesh is
        # delivered as it was (no third mesh); the holes sent back go as a set; UNDER_LOW_ROUNDS at most a face
        # (the edge lengths left out: these rings are let under the flexible shortest edge)
        qk_c, tk_c = np.asarray(q_face, dtype=np.int64), np.asarray(t_face, dtype=np.int64)
        base_ = ul_state.setdefault("base", {})
        settled_ = ul_state.setdefault("settled", set())
        waiting_ = ul_state.setdefault("waiting", set())
        with_ = ul_state.setdefault("with", {})
        rounds_ = ul_state.setdefault("rounds", {})
        under_now: dict[int, list] = {}
        for w in rings:
            if w.radii and w.info.get("rings_under_shortest_edge"):
                under_now.setdefault(int(w.owner), []).append(w)

        def figures(f_c: int) -> tuple[np.ndarray, list[np.ndarray]]:
            """(the face's counts per line, the centres of the elements making each count)."""
            vec_, masks_ = _face_lines(xyz, Q, T, qk_c == f_c, tk_c == f_c, lim, None)
            qi_c, ti_c = np.flatnonzero(qk_c == f_c), np.flatnonzero(tk_c == f_c)
            pts_c = [np.vstack([xyz[Q[qi_c[mq_]]].mean(axis=1) if mq_.any() else np.zeros((0, 3)),
                                xyz[T[ti_c[mt_]]].mean(axis=1) if mt_.any() else np.zeros((0, 3))])
                     for mq_, mt_ in masks_]
            return vec_, pts_c

        whole_cache_: list = []

        def whole_now() -> tuple[np.ndarray, int]:
            """(SHMESH-51o74: this mesh's counts per line over all its elements, and how many it has; once a pass)."""
            if not whole_cache_:
                whole_cache_.append((_face_lines(xyz, Q, T, np.ones(len(Q), bool), np.ones(len(T), bool), lim,
                                                 None)[0], int(len(Q) + len(T))))
            return whole_cache_[0]

        def picks_of(vec_: np.ndarray, pts_c: list[np.ndarray], base_vec: np.ndarray, cen_: list,
                     whole: tuple[np.ndarray, int] | None = None) -> list:
            """The hole centres to send back: every one nearest to an element on a worse line (all of them past
            UNDER_LOW_ROUNDS); [] where no line is worse. SHMESH-51o74 (UNDER_LOW_RANKED, with the two-ring mesh's
            whole counts): worse in the ranked order (_ul_worse), not on any count."""
            worse_ = (np.asarray(_ul_worse(vec_, base_vec, whole[0], whole[1], lim), dtype=np.int64)
                      if UNDER_LOW_RANKED and whole is not None else np.flatnonzero(vec_ > base_vec))
            if not len(worse_):
                return []
            pts_ = np.vstack([pts_c[j] for j in worse_]) if len(worse_) else np.zeros((0, 3))
            cen_a = np.asarray(cen_, dtype=float)
            if not len(pts_):
                return list(cen_)
            near_ = np.argmin(np.linalg.norm(pts_[:, None, :] - cen_a[None, :, :], axis=2), axis=1)
            return [cen_[int(i)] for i in sorted(set(near_.tolist()))]

        if waiting_ and not (waiting_ & set(under_now)):
            # the one-ring mesh: each waiting face measured and set against the mesh kept. SHMESH-51o60: this pass
            # runs to its end either way - kept as the one-ring mesh where every such hole goes back (delivered as it
            # is, or held to fall back on), the mesh before it delivered where none does
            sent_all: list = []
            all_back = True
            for f_c in sorted(waiting_):
                base_[f_c] = figures(f_c)[0]
                vec_w, pts_w, cen_w = with_.pop(f_c)
                rounds_[f_c] = rounds_.get(f_c, 0) + 1
                got_p = picks_of(vec_w, pts_w, base_[f_c], cen_w, ul_state.get("whole_with"))
                if got_p and rounds_[f_c] > UNDER_LOW_ROUNDS:
                    got_p = list(cen_w)
                if not got_p or len(got_p) == len(cen_w):
                    settled_.add(f_c)
                all_back = all_back and len(got_p) == len(cen_w)
                sent_all += got_p
            keys_ = {-(f_c + UNDER_LOW_KEY) - 1 for f_c in waiting_}
            waiting_.clear()
            if not sent_all:
                ul_state["decision"] = ("keep", keys_, [])
            elif all_back:
                ul_state["decision"] = ("base", keys_, sent_all)
            else:
                ul_state["decision"] = ("some", keys_, sent_all)
        for f_c, ws_c in sorted(under_now.items()):
            if f_c in settled_:
                continue
            vec_n, pts_n, cen_n = *figures(f_c), [[float(x) for x in w.centre] for w in ws_c]
            if f_c not in base_ and not vec_n.any():
                settled_.add(f_c)        # (nothing on any line: no one-ring mesh can be better - none is made)
                continue
            if f_c not in base_:
                with_[f_c] = (vec_n, pts_n, cen_n)
                if UNDER_LOW_RANKED:
                    ul_state["whole_with"] = whole_now()     # (SHMESH-51o74: the two-ring mesh, whole)
                ul_state.setdefault("want", set()).add(f_c)
                continue
            rounds_[f_c] = rounds_.get(f_c, 0) + 1
            got_p = picks_of(vec_n, pts_n, base_[f_c], cen_n, whole_now() if UNDER_LOW_RANKED else None)
            if not got_p:
                settled_.add(f_c)
                continue
            if rounds_[f_c] > UNDER_LOW_ROUNDS and "stash_base" in ul_state:
                # (past the rounds, every such hole goes back: the one-ring mesh held is that mesh - no new one)
                settled_.add(f_c)
                ul_state["decision"] = ("restore", set(), [])
                continue
            if rounds_[f_c] > UNDER_LOW_ROUNDS:
                got_p = cen_n
            if len(got_p) == len(cen_n):
                settled_.add(f_c)
            raise _UnderLow(sent=got_p, why=f"a line of its face worse than with one ring ({len(got_p)} hole(s) back)")
    if judge_faces and ST.BOLT_CHECK and not seam_due and requirements and requirements.get("limits"):
        # SHMESH-51: a face whose bolt holes took two rings and that leaves a quality line of the user's unmet on its
        # own elements is meshed again with one ring round them (as their size gives): -k-1 in the faces refused
        import types as _types

        qk_b, tk_b = np.asarray(q_face, dtype=np.int64), np.asarray(t_face, dtype=np.int64)
        hurt_b: dict[int, dict] = {}
        whole_rows: dict[int, dict] = {}

        def whole_row(j: int, lim_: dict) -> dict:
            # (SHMESH-51o67: the line on the whole mesh, read once)
            if j not in whole_rows:
                if -1 not in whole_rows:
                    whole_rows[-1] = {"q": shell_quality.quad_measures(xyz, Q, extra=True) if len(Q) else {},
                                      "t": shell_quality.tria_measures(xyz, T, extra=True) if len(T) else {}}
                whole_rows[j] = SR._limit_row(lim_, _types.SimpleNamespace(n_elements=int(len(Q) + len(T))),
                                              whole_rows[-1]["q"], whole_rows[-1]["t"])
            return whole_rows[j]

        for kp in sorted({int(w.owner) for w in rings if w.boxed2}):
            plate_q = shell_quality.quad_measures(xyz, Q[qk_b == kp], extra=True) if (qk_b == kp).any() else {}
            plate_t = shell_quality.tria_measures(xyz, T[tk_b == kp], extra=True) if (tk_b == kp).any() else {}
            sub_b = _types.SimpleNamespace(n_elements=int((qk_b == kp).sum() + (tk_b == kp).sum()))
            if BOLT_CHECK_RANKED:
                # SHMESH-51o67, the ranked order: the rings go back for an element of the face past the solver's
                # limits or past an outer line of the user's; for elements between a target and its outer line only
                # where the whole mesh misses that line (its allowance is the whole mesh's, not the face's: on the
                # real column two faces' rings went back for a 149.9 degree quad against a 135 target, 150 outer)
                solver_b = int((shell_quality._quad_past(plate_q).sum() if len(plate_q) else 0)
                               + (shell_quality._tria_past(plate_t).sum() if len(plate_t) else 0))
                if solver_b:
                    hurt_b[-kp - 1] = {"kind": "bolt rings", "why": "an element of its own past the solver's limits",
                                       "elements": solver_b}
                    continue
            for j_, lim_ in enumerate(requirements["limits"]):
                row_ = SR._limit_row(lim_, sub_b, plate_q, plate_t)
                if not row_.get("applied") or row_.get("met") is True:
                    continue
                if BOLT_CHECK_RANKED:
                    if int(row_.get("beyond") or 0):
                        why_ = "an element of its own past an outer line"
                    elif whole_row(j_, lim_).get("met") is not True:
                        why_ = "a quality line the whole mesh misses, not met on its own elements"
                    else:
                        continue
                else:
                    why_ = "a quality line not met on its own elements"
                hurt_b[-kp - 1] = {"kind": "bolt rings", "why": why_, "line": str(lim_.get("measure")),
                                   "worst": row_.get("worst")}
                break
        if hurt_b:
            raise _NoCut(set(hurt_b), hurt_b)
    if TRIA_POLISH and len(T) and lim.aims:
        # SHMESH-51o31: a triangle under the user's triangle angle target has its free nodes (flat faces) nudged where
        # that raises its smallest angle and leaves no element round the node worse on the solver's limits or the
        # user's lines (on the real column 4 of 63 triangles stood between the 20 and 30 degree lines). SHMESH-51o34:
        # after the band guard, which may put nodes back. SHMESH-51o43: and after every face check of the pass, on
        # the mesh as it will be delivered - polished before them, the checks judged a different mesh and took other
        # turns (a grid kept or freed, a face refused): on the 80 lab parts 18 parts came out with more elements
        # outside a target, and one with warped quads past the outer line, though each move raised no count round it
        # (SHMESH-51o36: with the user's longest edge hard, no move or cut may put an element over it)
        hi_hard = (sizing or {}).get("high_mm") if _edge_hard(requirements)[1] else None
        mov_u, on_u = movable[used], on_face[used]
        # (SHMESH-51o45: the user's edges and their bands are lines too)
        edges_p = ((sizing or {}).get("low_mm"), _low_tolerant(sizing, requirements), (sizing or {}).get("high_mm"),
                   _high_tolerant(sizing, requirements))
        xyz = _polish_trias(xyz, Q, T, mov_u, on_u, lim, hi_hard, edges_p)
        if TRIA_RECUT:
            # SHMESH-51o32: ... then a triangle still under it and the quad beside it re-cut the other ways (the
            # pentagon they make, a quad and a triangle), where the triangle's smallest angle rises and the quad stays
            # inside the solver's limits and no worse on the user's lines - no node moves
            Q, T, q_face, t_face, _recut = _recut_trias(xyz, Q, T, q_face, t_face, lim, mov_u, hi_hard, edges_p)
            # (SHMESH-80: and on what the re-cut left)
            Q, T, q_face, t_face, _twins = shell_quality.drop_twins(Q, T, q_face, t_face)
            face_of = np.array(q_face + t_face, dtype=np.int64)
    fit_bad: list = []
    if _FIT_FACES and lim.aims:
        # SHMESH-77: the fit-mapped faces whose own elements, or a neighbour's (a face sharing a curve: the fit's counts
        # are its counts too), are past an outer line of the user's or the solver's limits
        bad_k: set[int] = set()
        if len(Q):
            mq = shell_quality.quad_measures(xyz, Q, extra=True)
            hit = (shell_quality.aim_score(mq, "quad", lim, len(Q)) >= shell_quality.AIM_OUTER) | shell_quality._quad_past(mq, lim)
            bad_k |= {int(x) for x in np.asarray(q_face, dtype=np.int64)[hit]}
        if len(T):
            mt = shell_quality.tria_measures(xyz, T, extra=True)
            hit = (shell_quality.aim_score(mt, "tria", lim, len(T)) >= shell_quality.AIM_OUTER) | shell_quality._tria_past(mt, lim)
            bad_k |= {int(x) for x in np.asarray(t_face, dtype=np.int64)[hit]}
        for kf in sorted(set(_FIT_FACES)):
            near = _FIT_NEAR.get(kf, {kf})
            if near & bad_k:
                fit_bad.append([list(map(float, facts[kf]["centroid"])), float(facts[kf]["area"])])
    step_lines: list = []
    if strip_steps and lim.aims:
        # (SHMESH-73: each stepped strip as delivered - its triangles, and its elements past a target or an outer line)
        qk_s, tk_s = np.asarray(q_face, dtype=np.int64), np.asarray(t_face, dtype=np.int64)
        for t_s in sorted(strip_steps):
            if t_s not in tag_of:
                continue
            k_s = tag_of[t_s]
            row_s = {"strip": int(t_s), "face": int(k_s), "made": t_s in steps_made,
                     "trias": int((tk_s == k_s).sum()), "quads": int((qk_s == k_s).sum())}
            for shape_s, el_s, fk_s in (("quad", Q, qk_s), ("tria", T, tk_s)):
                sel_s = fk_s == k_s
                if not sel_s.any():
                    continue
                me_s = (shell_quality.quad_measures if shape_s == "quad" else shell_quality.tria_measures)(
                    xyz, el_s[sel_s], extra=True)
                sc_s = shell_quality.aim_score(me_s, shape_s, lim, int(sel_s.sum()))
                row_s[shape_s + "_band"] = int((sc_s == shell_quality.AIM_BAND).sum())
                row_s[shape_s + "_beyond"] = int((sc_s >= shell_quality.AIM_OUTER).sum())
            step_lines.append(row_s)
    mids = {m["id"]: m for m in record["mids"]}
    pairs = {p["id"]: p for p in record["pairs"]}
    mid_of = [owners[k] for k in face_of.tolist()]
    wall = np.array([float(pairs[mids[m]["pair"]]["thickness_mm"]) for m in mid_of])
    part = [mids[m]["body"] for m in mid_of]
    mesh = ShellMesh(xyz=xyz, quads=Q, trias=T, wall=wall, mid=mid_of, part=part)
    # (SHMESH-91: each element's sewn face, for the per-face keep across candidate meshes)
    mesh.face_of = np.asarray(face_of, dtype=np.int64).copy()  # type: ignore[attr-defined]
    # groups: one per wall pair (its CAD faces), one per part
    for m in sorted(set(mid_of), key=lambda s: int(s.split("_")[1])):
        name = "+".join(mids[m]["faces"])
        mesh.groups.setdefault(name, np.zeros(0, dtype=np.int64))
        mesh.groups[name] = np.concatenate([mesh.groups[name], np.flatnonzero(np.array(mid_of) == m)])
    for p in sorted(set(part)):
        mesh.groups[p] = np.flatnonzero(np.array(part) == p)
    reference: dict[str, np.ndarray] = {}
    for k, f in enumerate(facts):
        n = _reference_normal(f["normal_face"])
        if n is not None:
            reference.setdefault(owners[k], n)
    turned = orient(mesh, reference)
    varied = node_walls(mesh, mids)
    offset = element_offsets(mesh, mids)
    census = edge_census(mesh)
    quality = shell_quality.measure(mesh)
    quality["per_face"] = shell_quality.per_face(mesh, (sizing or {}).get("low_mm"), (sizing or {}).get("high_mm"),
                                                 aims=lim.aims)
    tria_told = _trias_told(mesh, lim, node_why)
    quality["edges"] = {k: v for k, v in census.items() if k != "free_edges"}
    quality["turned"] = turned
    quality["varied_wall_elements"] = varied
    quality["offset_elements"] = offset
    quality["split"] = split
    quality["repaired"] = repaired          # (SHMESH-83)
    quality["relaxed_nodes"] = relaxed
    quality["merged_nodes"] = merged
    stepped = {r_: n_ for r_ in FACE_LADDER if (n_ := sum(1 for v_ in rungs.values() if v_ == r_))}
    if left_out or stepped:
        # SHMESH-71/72/74: the faces treated otherwise so that one face never costs the whole part
        total = float(sum(f_["area"] for f_ in facts)) * mm * mm
        quality["face_watch"] = {"left_out": len(left_out), "left_out_mm2": [round(a_, 2) for a_ in left_areas],
                                 "left_out_share": round(sum(left_areas) / max(total, 1e-300), 5),
                                 "rungs": stepped}
    if narrow_closed:
        # SHMESH-60: the narrow strips closed to their middle lines
        quality["narrow_closed"] = len(narrow_closed)
    if strip_steps:
        # SHMESH-73: the stepped strips planned and made
        quality["strip_steps"] = {"planned": len(strip_steps), "made": len(steps_made), "each": step_why[:40],
                                   "delivered": step_lines[:40]}
    if _YIELDED[0]:
        quality["strip_yield"] = int(_YIELDED[0])
    if _STRIP_LOG:
        quality["strip_log"] = [dict(r_) for r_ in _STRIP_LOG[:60]]
    if _FIT_MAPPED[0]:
        quality["fit_mapped"] = int(_FIT_MAPPED[0])
    if fit_bad:
        quality["fit_bad"] = fit_bad
    if sizing is not None:
        # (SHMESH-77 per face: each sewn face's triangles, the fit faces, their neighbours and keys - for SHMESH-76;
        # written with the fit off too - that mesh is the one each fit face is set against)
        quality["face_trias"] = {str(k_): int(n_) for k_, n_ in
                                  zip(*np.unique(np.asarray(t_face, dtype=np.int64), return_counts=True))} if len(t_face) else {}
        if _FIT_FACES:
            quality["fit_faces"] = {str(k_): {"near": sorted(int(x_) for x_ in _FIT_NEAR.get(k_, {k_})),
                                              "key": [list(map(float, facts[k_]["centroid"])), float(facts[k_]["area"])]}
                                     for k_ in sorted(set(_FIT_FACES))}
        if _STEP_NEAR:
            # (SHMESH-90 per strip: each stepped strip's neighbours and key - for SHMESH-76's split)
            quality["step_faces"] = {str(k_): {"near": sorted(int(x_) for x_ in v_),
                                               "key": [list(map(float, facts[k_]["centroid"])), float(facts[k_]["area"])]}
                                     for k_, v_ in sorted(_STEP_NEAR.items())}
    if _CORNER_MERGED[0]:
        quality["corner_mapped"] = int(_CORNER_MERGED[0])
    if _TRI_PLAN:
        quality["tri_blocks"] = {"planned": len(_TRI_PLAN), "made": len(tri_made)}
    if _FREE_WHY:
        # (why each four-sided face was not strip-mapped; sizes, counts, angles only)
        quality["free_why"] = [{"face": int(t_), **v_} for t_, v_ in sorted(_FREE_WHY.items())][:200]
    quality["flipped"] = flipped
    quality["paired"] = paired
    quality["walked"] = walked
    quality["caps_absorbed"] = capped
    quality["evened_curves"] = evened
    quality["sliver_faces"] = slivers
    unbuilt = [w for w in rings if w.annulus not in ring_of]
    washers = [w for w in washers if w not in unbuilt]
    holes_seen = len(washers) + sum(washer_skipped.values())
    quality["structure"] = {"washers": len(washers), "holes": holes_seen,
                            "mapped_faces": len(grids) + len(mapped), "faces": len(tag_of) - len(rings),
                            "washers_shrunk": sum(1 for w in washers if w.shrunk),
                            "no_washer": dict(sorted(washer_skipped.items())),
                            "around": dict(sorted(Counter(w.n for w in washers).items())),
                            "washers_ogrid": len(gridded), "washers_boxed": len(boxed),
                            "not_gridded": dict(sorted(block_skipped.items())),
                            "not_gridded_faces": {why_: sorted({owners[k] if 0 <= k < len(owners) else str(k)
                                                                for k in ks})
                                                  for why_, ks in sorted(skipped_where.items())},
                            "switched_off": structure,
                            "hole_list": hole_lines[:HOLE_LINES]}
    if zero_kept:
        quality["structure"]["grids_kept_no_triangle"] = zero_kept
    quality["structure"]["transition_cells"] = int(n_trans)   # (SHMESH-51o28: cells made transition cells)
    quality["structure"]["transition_cells_paved"] = int(n_trans_paved)   # (SHMESH-51o42: worse than their paving)
    if tria_told:
        quality["structure"]["trias_under_target"] = tria_told[:TRIA_TOLD_MAX]
    if grids or mapped:
        mesh.notes.append(f"Mapped quads (a regular grid) on {len(grids) + len(mapped)} of {len(tag_of) - len(rings)} "
                          "face(s): every face with four sides (SHMESH-40).")
    under_h = [h for h in hole_lines if h.get("rings_under_shortest_edge") and h.get("planned_rows", 0) >= 2]
    if under_h and (sizing or {}).get("low_mm") is not None:
        # SHMESH-51o59: the two rings let under the user's flexible shortest edge, and the short edges they bring
        low_u = float((sizing or {})["low_mm"])
        cq_ = mesh.xyz[mesh.quads].mean(axis=1) if len(mesh.quads) else np.zeros((0, 3))
        near_u = np.zeros(len(cq_), dtype=bool)
        for h in under_h:
            r_out = h["diameter_mm"] / 2 + float(h.get("planned_width_mm") or 0)
            near_u |= np.linalg.norm(cq_ - np.asarray(h["at_mm"], dtype=float), axis=1) < r_out * 1.05
        e_u = (np.linalg.norm(mesh.xyz[mesh.quads] - mesh.xyz[np.roll(mesh.quads, -1, axis=1)], axis=2).min(axis=1)
               < low_u) if len(mesh.quads) else np.zeros(0, dtype=bool)
        n_short = int((near_u & e_u).sum())
        quality["structure"]["rings_under_shortest_edge"] = {
            "rule": "SHMESH-51o52", "holes": len(under_h), "elements_under_shortest_edge": n_short,
            "share_pct": round(100.0 * n_short / max(mesh.n_elements, 1), 2)}
        mesh.notes.append(f"{len(under_h)} bolt hole(s) took two rings under your shortest edge of {low_u:g} mm "
                          f"(flexible: rings first); {n_short} of their ring elements have an edge under it "
                          f"({100.0 * n_short / max(mesh.n_elements, 1):.2g}% of the elements) (SHMESH-51o52).")
    bolt_lines = _bolt_rings(mesh, hole_lines) if any(h.get("bolt") for h in hole_lines) else []
    if bolt_lines:
        quality["structure"]["bolt_holes"] = len(bolt_lines)
        quality["structure"]["bolt_rings"] = dict(sorted(Counter(min(int(h.get("rings") or 0), ST.BOLT_RINGS)
                                                                 for h in bolt_lines).items()))
        mesh.notes += _bolt_words(bolt_lines)
    if washers:
        why = "; ".join(f"{n} {reason}" for reason, n in sorted(washer_skipped.items()))
        mesh.notes.append(f"Washers on {len(washers)} of {holes_seen} round hole(s): a ring of quads round each, "
                          f"{'/'.join(str(n) for n in sorted({w.n for w in washers}))} elements round"
                          + (f" ({sum(1 for w in washers if w.shrunk)} ring(s) narrowed to fit)" if any(w.shrunk for w in washers) else "")
                          + (f"; none on {why}" if why else "") + " (SHMESH-39).")
    if left_out:
        # SHMESH-74: said first, plainly: an opening in the mesh is never a quiet pass
        share = 100.0 * sum(left_areas) / max(float(sum(f_["area"] for f_ in facts)) * mm * mm, 1e-300)
        said_out = (f"{len(left_out)} face(s) could not be meshed and were left out: {sum(left_areas):.0f} mm2, "
                    f"{share:.1f}% of the part (the faces: {which}). The mesh has an opening there (free edges round "
                    "it). This is ours to fix (SHMESH-71/72); those faces of the CAD are where to look.")
        mesh.notes.insert(0, said_out)
        quality.setdefault("face_watch", {})["words"] = said_out
    if stepped:
        mesh.notes.append("Faces that held gmsh's surface mesher too long were meshed another way: "
                          + ", ".join(f"{n_} by {r_}" for r_, n_ in stepped.items()) + " (SHMESH-71).")
    if slivers:
        mesh.notes.append(f"{slivers} sewn face(s) under a fifth of the floor across (CAD slivers) carry no "
                          "elements (SHMESH-12).")
    if merged:
        mesh.notes.append(f"{merged} node(s) at the ends of sliver edges (under the floor, a fifth of their elements' longest) "
                          "were merged (SHMESH-12).")
    if capped:
        mesh.notes.append(f"{capped} flat triangle(s) with every corner on a CAD edge were taken into the element "
                          "beside them, or dropped from a free edge (SHMESH-22).")
    if split:
        mesh.notes.append(f"{split} quad(s) past Nastran's own limits were cut into two triangles each (SHMESH-10).")
    floored = [r["face"] for r in rows if r["floored"]]
    if floored:
        mesh.notes.append(f"{len(floored)} face(s) asked for elements finer than half their wall; they are meshed at "
                          "half the wall (SHMESH-2).")
    out = {"faces": rows, "quality": quality, "per_turn": PER_TURN, "growth": growth}
    if counted is not None:
        out["target"] = counted
    if requirements:
        # SHMESH-26: every requirement of the user's, asked against delivered, measured on this mesh
        quality["aimed_split"] = aimed
        quality["mended"] = mended
        if short_mended is not None:
            quality["short_mended"] = short_mended
        if aimed:
            mesh.notes.append(f"{aimed} quad(s) past a limit you asked for were cut into two triangles each, both "
                              "inside it (SHMESH-25).")
        if sizing is not None:
            out["sizing"] = sizing
            if narrow is not None and short_edges is not None and (narrow["widths"] or short_edges["edges"]):
                smallest = float(f"{min([*narrow['widths'].values(), *short_edges['lengths'].values()]):.4g}")
                sizing["features"] = {"rule": "SHMESH-27", "faces": len(narrow["widths"]),
                                      "edges": short_edges["edges"], "smallest_mm": smallest}
                mesh.notes.append(
                    f"Your CAD has {short_edges['edges']} edge(s) and {len(narrow['widths'])} face(s) smaller than "
                    f"the element size you asked for (the smallest is {smallest:g} mm). The mesh is finer around "
                    "them, so the elements there keep a good shape (SHMESH-27).")
            if NARROW_ALONG and _stamped_owns(record) and sizing.get("low_mm") is not None:
                # SHMESH-51o68: the stamped strip rules size the narrow faces here
                sizing["narrow_along"] = {"rule": "SHMESH-51o63", "faces": [], "narrow_faces": [],
                                          "stood_aside": "the stamped-sheet strip rules (SHMESH-60..69) size the "
                                                         "narrow faces of this part"}
            if _along(sizing, record) is not None and narrow is not None and short_edges is not None:
                # SHMESH-51o63/65: the mid-faces sized as long along as the aspect target allows (a face narrower than
                # the shortest edge, or one beside a CAD edge shorter than it), and those that went back
                along_k = (set(narrow.get("along_faces") or ()) | set(short_edges.get("along_faces") or ()))
                sizing["narrow_along"] = {"rule": "SHMESH-51o63",
                                          "faces": sorted({str(owners[k]) for k in along_k if k < len(owners)}),
                                          "narrow_faces": sorted({str(owners[k]) for k in narrow.get("along_faces")
                                                                  or () if k < len(owners)}),
                                          # (SHMESH-51o68: which rule sized each face narrower than the shortest edge)
                                          "sized_by": {str(owners[k]): "SHMESH-51o63 (one across, stretched along)"
                                                       for k in sorted(narrow.get("along_faces") or ())
                                                       if k < len(owners)},
                                          "back": sorted(NARROW_SKIP)}
            flanks = sorted((narrow or {}).get("flank_faces") or [])
            if flanks:
                sizing["flanks"] = {"rule": "SHMESH-35", "faces": len(flanks), "across": plan_rules.FLANK_ACROSS}
                mesh.notes.append(f"Kept finer at {len(flanks)} narrow wall(s) of beads, darts or steps: "
                                  f"{plan_rules.FLANK_ACROSS} elements across each, so they keep their shape "
                                  "(SHMESH-35).")
            chord = _chord(mesh, facts, face_of, mm) if CHORD_REPORT else None
            if chord is not None:
                # SHMESH-34: how far the elements stand off the CAD on curved faces, told against the size
                g = sizing.get("global_mm") or float(np.median([r["size_mm"] for r in rows]))
                sizing["chord"] = {"rule": "SHMESH-34", **chord, "share": float(f"{chord['max_mm'] / g:.3g}")}
                mesh.notes.append(f"On curved faces element centres lie within {chord['max_mm']:.3g} mm of your "
                                  f"CAD ({100 * chord['max_mm'] / g:.2g}% of the element size) (SHMESH-34).")
        if places is not None:
            out["places"] = places
            sized_places = [p for p in places.get("places") or [] if p.get("faces")]
            if sized_places:
                mesh.notes.append(f"Sized {len(sized_places)} place(s) your notes name, each to the size asked there "
                                  "(SHMESH-52).")
        asked = SR.report(mesh, requirements, sizing, places=places)
        if asked is not None:
            out["requirements"] = asked
        # SHMESH-31: where elements past the solver's own limits are (taken off by _mesh_shell)
        past = shell_quality.nastran_past(mesh) | (_min_jac(mesh) <= 0)
        out["_past_spots"], out["_spots_left"] = _spots(mesh, face_of, past)
        out["_spots_trimmed"] = spots_trimmed
        out["_past_n"] = int(past.sum())
    # SHMESH-45k: the holes whose ring had to follow uneven hole nodes, or could not be built or closed: _mesh_path
    # draws them again as one circle and meshes once more
    drawn_at = [np.asarray(c, dtype=float) for c, _e in hole_before]
    want_w = [w for w in washers_planned if _wants_redraw(w, drawn_at)]
    out["_redraw_wanted"] = [[float(x) for x in w.centre] for w in want_w]
    out["_redraw_owners"] = [int(w.owner) for w in want_w]     # (SHMESH-48l2: each hole's sewn face)
    # SHMESH-49c: the O-grid holes this mesh did not draw again (to start where their O-grid cuts)
    if polar_faces_made:
        tri_k = Counter(int(k) for k in t_face)
        out["_polar_trias"] = {int(k): int(tri_k.get(k, 0)) for k in polar_faces_made}
        tf_ = np.asarray(t_face, dtype=np.int64)
        out["_polar_tri_at"] = {int(k): [[round(float(x), 1) for x in xyz[T[j]].mean(axis=0)]
                                         for j in np.nonzero(tf_ == k)[0][:20]] for k in polar_faces_made}
        out["_polar_judged"] = judged_now
    seam_w = [w for w in gridded if not any(float(np.linalg.norm(c - w.centre)) <= 0.25 * w.r for c in drawn_at)]
    out["_seam_wanted"] = [([float(x) for x in w.centre], list(w.seam)) if w.seam else [float(x) for x in w.centre]
                           for w in seam_w]
    out["_seam_owners"] = [int(w.owner) for w in seam_w]
    return mesh, out


#: SHMESH-51: a refused face's bolt rings are kept in the faces refused as -k-1 (a quality line unmet), or as
#: SHMESH-51k: a band's nodes are not moved after it is made (relaxing, mending): it was checked where it was made -
#: off since SHMESH-51l: held fast, its box's side nodes could not even out the cells beside it, and on the real column
#: a 12-hole plate's next passes went otherwise and 6 of its 12 bands came out past the solver's limits
BAND_PINNED = False
#: SHMESH-51o82: a free cell's last open curve evens its count (an all-quad cell needs an even sum) by one segment
#: more - unless that takes its segments under the user's shortest edge: then one fewer where that stays inside their
#: longest edge, else the count stays (gmsh closes the cell with one triangle). Traced on the look-alike plate w7_0:
#: 51 cut curves of 8.5 mm (the strip between box sides of holes out of step) went from 1 segment to 2 of 4.26 mm
#: under the 5 mm line, and the strip's 472 quads came out under it
EVEN_KEEPS_LOW = True


def _even_count(length_mm: float, n: int, low_mm: float | None, high_mm: float | None) -> int:
    """SHMESH-51o82: the count a free cell's last open curve takes to even its cell's sum (n is odd for it)."""
    if not EVEN_KEEPS_LOW or low_mm is None or length_mm / (n + 1) >= float(low_mm) * (1 - 1e-9):
        return n + 1
    if n - 1 >= 1 and (high_mm is None or length_mm / (n - 1) <= 0.98 * float(high_mm)):
        return n - 1
    return n


#: SHMESH-51l: a band's nodes move with the relaxing and mending as before, and go back to where the band put them
#: wherever a quad of the band would come out past the solver's limits or over the user's longest edge
BAND_GUARD = True
#: SHMESH-51o79: ... and wherever a band quad made at or over the user's shortest edge comes out under it: on the real
#: column 196 ring-to-box edges sat between 3.0 and 5.0 mm under its 5 mm line, the box side nodes moved in after the
#: band was made
BAND_LOW_GUARD = True


def _min_edges(xyz: np.ndarray, quads: np.ndarray) -> np.ndarray:
    """(quads,) the shortest edge of each quad (mm)."""
    p = xyz[quads]
    return np.linalg.norm(p - np.roll(p, -1, axis=1), axis=2).min(axis=1)


def _band_bad(xyz: np.ndarray, quads: np.ndarray, at_made: np.ndarray, high_mm: float | None,
              low_mm: float | None) -> np.ndarray:
    """(quads,) bool: SHMESH-51l - a band quad now past the solver's limits or over the user's longest edge;
    SHMESH-51o79 - or made with every edge at or over the user's shortest edge and now with one under it."""
    m_ = shell_quality.quad_measures(xyz, quads, extra=True)
    bad = shell_quality._quad_past(m_)
    if high_mm is not None:
        bad = bad | (np.asarray(m_["max_edge"]) > float(high_mm))
    if BAND_LOW_GUARD and low_mm is not None:
        lo = float(low_mm) - 1e-6
        bad = bad | ((_min_edges(at_made, quads) >= lo) & (_min_edges(xyz, quads) < lo))
    return bad
BAND_GUARD_ROUNDS = 8
#: SHMESH-51o26: every delivered element held to the user's longest edge by putting back nodes the tidying moved
EDGE_GUARD = False
EDGE_GUARD_ROUNDS = 4
#: SHMESH-51j: a two-ring bolt hole whose band was not made keeps its box too (one ring inside, its cell paved) - off:
#: on the lab's webY8 the paved box cell came out past the solver's limits and SHMESH-51b lost the web's grid
KEEP_BOX_BAND = False
#: -k-1-BOLT_GRID_KEY (one ring so the face keeps its grid, SHMESH-51b)
BOLT_GRID_KEY = 1 << 30
#: SHMESH-51o67: SHMESH-51's bolt-ring check in the ranked order - rings go back for their face's elements past the
#: solver's limits or an outer line, for in-band elements only where the whole mesh misses the line
BOLT_CHECK_RANKED = True
#: SHMESH-51o57: ... and -k-1-UNDER_LOW_KEY (the face's rings planned under the user's flexible shortest edge go back
#: to the edges, before any other hole of the face gives up its second ring)
UNDER_LOW_KEY = 1 << 29
#: SHMESH-51o57: each hole with rings under the shortest edge checked on its own ring elements
UNDER_LOW_CHECK = False  # (SHMESH-51o60: the face comparison alone - the filter cost fr020 two passes)
#: ... over the rings and the row of elements beside them (on fr020 the quads outside the corner targets sat just out
#: of the rings: 27 of 30 between 1 and 1.5 ring radii)
UNDER_LOW_REACH = 1.5
#: SHMESH-51o58: the final word - a face keeps its rings under the shortest edge only where no line of it is worse
#: than the same face with those holes on one ring
UNDER_LOW_COMPARE = True
#: SHMESH-51o74: ... "worse" in the ranked order the lines are held to (as SHMESH-51o66's per-face keep): elements past
#: the solver's limits first, then elements past an outer line (one where the one-ring face has none, then the sum),
#: then a rise inside a line's band only where it takes a line the whole mesh meets with one ring to missed, then
#: triangles. Any count at all sent holes back: on s0054 (with the clean-up's a644bb45) the long plate's two rings
#: were 5 in a band against 4 on one line - met either way - while better on triangles (26 against 48), on another
#: line's band (4 against 14) and on its outer lines (0 against 2); two holes went back, the third mesh's spot pass left
#: 5 elements past Nastran's limits, where the two-ring mesh's spot pass clears them all
UNDER_LOW_RANKED = True
#: SHMESH-51o59: the rounds a face's comparison may take before every such hole of it goes back
UNDER_LOW_ROUNDS = 1


def _face_lines(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, qsel: np.ndarray, tsel: np.ndarray, lim,
                edges) -> tuple[np.ndarray, list[tuple[np.ndarray, np.ndarray]]]:
    """SHMESH-51o58: one face's counts per line - triangles, elements past the solver's limits, then each of the
    user's lines (outside its target, past its outer line) and edges (under / over the user's edges and their
    bands), quads and triangles apart - and per count which of the face's quads and triangles make it."""
    q_i, t_i = np.flatnonzero(qsel), np.flatnonzero(tsel)
    nq, nt = len(q_i), len(t_i)
    masks: list[tuple[np.ndarray, np.ndarray]] = [(np.zeros(nq, bool), np.ones(nt, bool))]
    mq = shell_quality.quad_measures(xyz, Q[q_i], extra=lim.extra) if nq else {}
    mt = shell_quality.tria_measures(xyz, T[t_i], extra=lim.extra) if nt else {}
    masks.append((shell_quality._quad_past(mq) if nq else np.zeros(0, bool),
                  shell_quality._tria_past(mt) if nt else np.zeros(0, bool)))
    for shape, m, n_ in (("quad", mq, nq), ("tria", mt, nt)):
        for row_shape, key, op, target, outer, _share in lim.aims:
            if row_shape != shape:
                continue
            # (the same counts in the same order whatever the face holds: a face with no triangle still counts 0 on
            # the triangle lines - its one-ring mesh may have some)
            v = np.asarray(m[key]) if key in m else np.zeros(n_)
            for lim_v in (target, outer):
                bad = np.zeros(n_, bool) if (lim_v is None or key not in m) else (
                    (v < lim_v) if op == ">=" else (v > lim_v))
                masks.append((bad, np.zeros(nt, bool)) if shape == "quad" else (np.zeros(nq, bool), bad))
        els = Q[q_i] if shape == "quad" else T[t_i]
        if edges is not None:
            ed = np.linalg.norm(xyz[els] - xyz[np.roll(els, -1, axis=1)], axis=2) if n_ else np.zeros((0, 3))
            for lim_v, side in zip(edges, ("lo", "lo", "hi", "hi"), strict=True):
                bad = np.zeros(n_, bool) if (lim_v is None or not n_) else (
                    (ed.min(axis=1) < float(lim_v)) if side == "lo" else (ed.max(axis=1) > float(lim_v)))
                masks.append((bad, np.zeros(nt, bool)) if shape == "quad" else (np.zeros(nq, bool), bad))
    vec = np.array([int(a.sum()) + int(b.sum()) for a, b in masks], dtype=np.int64)
    return vec, masks


def _aim_slots(lim) -> list[tuple[int, int, float]]:
    """SHMESH-51o74: per user line, where _face_lines (no edges) counts it - (index outside its target, index past its
    outer line, its allowance share) - quads' lines first, then triangles', as _face_lines orders them."""
    out, i = [], 2
    for shape in ("quad", "tria"):
        for row_shape, _key, _op, _target, _outer, share in lim.aims:
            if row_shape == shape:
                out.append((i, i + 1, float(share or 0.0)))
                i += 2
    return out


def _ul_worse(vw: np.ndarray, vb: np.ndarray, whole: np.ndarray, n_all: int, lim) -> list[int]:
    """SHMESH-51o74: the counts (_face_lines indices) on which a face with two rings (`vw`) is worse than with one
    (`vb`), in the ranked order - [] where it is no worse (or better at an earlier step). `whole`: the two-ring mesh's
    counts over all its elements (n_all of them), to tell whether a band rise takes a met line to missed."""
    if vw[1] != vb[1]:
        return [1] if vw[1] > vb[1] else []
    slots = _aim_slots(lim)
    outs = [o for _t, o, _s in slots if o < len(vw)]
    new_out = [o for o in outs if vw[o] > 0 and vb[o] == 0]
    if new_out:
        return new_out
    so_w, so_b = int(sum(int(vw[o]) for o in outs)), int(sum(int(vb[o]) for o in outs))
    if so_w != so_b:
        return [o for o in outs if vw[o] > vb[o]] if so_w > so_b else []
    alt = whole - vw + vb
    lost = []
    for t, o, sh in slots:
        if t < len(vw) and vw[t] > vb[t]:
            miss_w = whole[o] > 0 or whole[t] > sh * n_all
            miss_b = alt[o] > 0 or alt[t] > sh * n_all
            if miss_w and not miss_b:
                lost.append(t)
    if lost:
        return lost
    return [0] if vw[0] > vb[0] else []


def _trial_ok(past_n: int, requirements: dict | None) -> bool:
    """SHMESH-31f by name: whether an optional whole mesh of the one-ring trial may start - the clean-up stream's
    _affords_trial (the time owed to the solver-limit pass set aside first) where the tree has it; else True."""
    f = globals().get("_affords_trial")
    if not callable(f):
        return True
    if not f(int(past_n or 0), requirements):
        return False
    lim_f, run = globals().get("_limit"), globals().get("_RUN")
    if TRIAL_OWES_ALL_SPOTS and past_n and requirements and callable(lim_f) and isinstance(run, dict):
        # SHMESH-51o78: every spot pass the mesh may still take (OWN_SIZE_RETRIES) is set aside before the trial, not
        # one: on the portal frame s0054 the one-ring trial (256 s) took the time of the second spot pass, and one pass
        # left 5 triangles past Nastran's limits
        lim = lim_f()
        price = float(run.get("first_s") or 0.0)
        if lim is not None and lim < price * (1 + max(1, int(OWN_SIZE_RETRIES))):
            return False
    return True


def _trial_why() -> str:
    """SHMESH-31f by name: what a trial not started records."""
    return str(globals().get("TRIAL_KEPT_WHY", "not tried: time kept for the solver-limit pass (SHMESH-31f)"))


def _under_faces(washers: list) -> set[int]:
    """SHMESH-51o57: the sewn faces with bolt holes whose two rings were planned under the shortest edge."""
    return {int(w.owner) for w in washers if w.info.get("rings_under_shortest_edge") and w.radii}


def _under_low_off(no_cut) -> set[int]:
    """SHMESH-51o57: the sewn faces whose rings under the shortest edge were taken back."""
    return {(-x - 1) - UNDER_LOW_KEY for x in no_cut if x < 0 and UNDER_LOW_KEY <= -x - 1 < BOLT_GRID_KEY}


def _boxes_at(at: np.ndarray, ws: list, mm: float, partial: bool = False) -> list | None:
    """SHMESH-51g: the two-ring bolt holes (of `ws`, each with its box's four corners) whose box holds each point of
    `at` (mm), or has it beside it (within BOX_BLAME_REACH of the box's half widths from its centre: one box width
    past its side); None when a point is beside none of them (the fault is not the rings'). `partial` (SHMESH-51h): the
    holes of the points that are, the others left out."""
    if not len(at):
        return None
    reach = np.full(len(at), np.inf)
    who = np.full(len(at), -1)
    for i, w in enumerate(ws):
        bc = np.asarray(w.box_corners, dtype=float) * mm
        u, v = bc[1] - bc[0], bc[3] - bc[0]
        d = at - np.asarray(w.centre, dtype=float) * mm
        f = np.maximum(2 * np.abs(d @ u) / max(float(u @ u), 1e-300), 2 * np.abs(d @ v) / max(float(v @ v), 1e-300))
        better = f < reach
        reach[better], who[better] = f[better], i
    if (reach > BOX_BLAME_REACH).any() and not partial:
        return None
    return [ws[i] for i in sorted(set(who[reach <= BOX_BLAME_REACH].tolist()))]


def _round_left(hole_rounds, k: int) -> bool:
    """SHMESH-51h: whether a face may send bolt holes to one ring again: with GATHER while it has had fewer than
    HOLE_ROUNDS_MAX rounds (each round sends one hole or more, so it ends), else BOLT_HOLE_ROUNDS rounds in all."""
    return _rounds(hole_rounds, k) < (HOLE_ROUNDS_MAX if isinstance(hole_rounds, dict) else ST.BOLT_HOLE_ROUNDS)


def _rounds(hole_rounds, k: int) -> int:
    """SHMESH-51c/51f: the bolt-hole rounds spent (a count for the whole model, or with GATHER one per sewn face)."""
    return int(hole_rounds.get(k, 0)) if isinstance(hole_rounds, dict) else int(hole_rounds)


def _face_of_key(k: int) -> int:
    """The sewn face a refused key names (a face, or a face's bolt rings, SHMESH-51/51b)."""
    if k >= 0:
        return k
    y = -k - 1
    return y - BOLT_GRID_KEY if y >= BOLT_GRID_KEY else (y - UNDER_LOW_KEY if y >= UNDER_LOW_KEY else y)


def _cell_keys(gmsh, tag_of: dict[int, int], tags, mm: float, free_of: dict | None = None) -> list[tuple]:
    """SHMESH-51f: each gmsh surface's key that outlives gmsh's tags from pass to pass: (its sewn face, its centre in
    mm, its area in mm2, the sewn faces its freeing came from - its own and those of the free faces beside it whose
    elements were past the limits)."""
    out = []
    for t in sorted(tags):
        if t in tag_of:
            c = gmsh.model.occ.getCenterOfMass(2, t)
            out.append((int(tag_of[t]), *(round(float(x) * mm, 4) for x in c),
                        round(float(gmsh.model.occ.getMass(2, t)) * mm * mm, 4),
                        tuple(sorted({int(tag_of[t])} | set((free_of or {}).get(t, ()))))))
    return out


def _keys_kept(keys, faces, body_of=None) -> set:
    """SHMESH-51f: the freed cells' keys that outlive a change to these sewn faces (their holes drawn again, their
    rings changed): those whose freeing came from none of them - with `body_of` (sewn face -> its body), from no face
    of their bodies (a face's rings change the counts through the faces joined to it)."""
    faces = {int(f) for f in faces}
    if body_of is not None:
        hit = {body_of(f) for f in faces}
        return {k for k in keys if not ({body_of(f) for f in (k[5] if len(k) > 5 else k[:1])} & hit)}
    return {k for k in keys if not (set(k[5] if len(k) > 5 else k[:1]) & faces)}


def _cells_of_keys(gmsh, tag_of: dict[int, int], keys, mm: float) -> set[int]:
    """SHMESH-51f: the gmsh surfaces of this pass that the keys name (same sewn face, centre within KEY_MM, area within
    KEY_AREA); a key whose cell is not drawn this pass (its face cut otherwise) names none."""
    if not keys:
        return set()
    kk = np.array([k[1:5] for k in keys], dtype=float)
    kf = np.array([k[0] for k in keys], dtype=np.int64)
    got: set[int] = set()
    faces = set(kf.tolist())
    for t in sorted(t for t, k in tag_of.items() if k in faces):
        (k_t, *c_t, a_t, _f), = _cell_keys(gmsh, tag_of, [t], mm)
        hit = (kf == k_t) & (np.abs(kk[:, :3] - np.asarray(c_t)).max(axis=1) <= KEY_MM) & (
            np.abs(kk[:, 3] - a_t) <= KEY_AREA * np.maximum(kk[:, 3], 1.0))
        if hit.any():
            got.add(t)
    return got


#: SHMESH-51j: faults beside two-ring boxes are put on their holes (one ring in a box of their own, SHMESH-51g) in a face's
#: first BESIDE_ROUNDS rounds only; after that such a fault goes to SHMESH-51b (one decision for the face). On the real
#: column every round of that kind moved boxes and broke the next ones (50 to 45 to 28 to the grid lost); the lab's
#: webreal needs one such round (54 of 70 holes keep two rings), webY8 two (a band not made, then a cell beside it)
BESIDE_ROUNDS = 2


def _beside_ok(hole_rounds, k: int) -> bool:
    """SHMESH-51j: whether a fault beside a box of face k may still be put on its holes."""
    return not ST.KEEP_BOX or _rounds(hole_rounds, k) < BESIDE_ROUNDS


#: SHMESH-51o12: SHMESH-51b's one ring takes every face refused its grid that holds two-ring bolt holes, wherever they
#: sit (in its grid, in a free box, or round a ring left in a cell its grid let free): a real column's plate (six round
#: and five square bolt holes) was refused its grid after meshing with its round holes' two rings in free cells, was
#: no face 51b named, and kept two rings and 8 more triangles where on dddf4d64 it took one ring and kept its grid
BOLT_FIRST_ANY = True


def _cell_sides(gmsh, t: int, index: np.ndarray, mm: float) -> list | None:
    """SHMESH-51o24: a four-sided cell's sides (mm) and their segments, for the record; None for another cell."""
    got = ST._four_sided(gmsh, t)
    if got is None:
        return None
    out = []
    for sd in got[0]:
        n_ = 0
        for c in sd:
            tags_, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            n_ += max(len(tags_) - 1, 0)
        out.append([round(sum(float(gmsh.model.occ.getMass(1, c)) for c in sd) * mm, 1), int(n_)])
    return out


def _offender(pts: np.ndarray, k: int, cells: list, shape: str) -> dict:
    """SHMESH-51o24: what an element of face k lies in (the smallest kept cell whose outline box holds its centre: a
    grid cell, a transition cell, a band's box cell; else a paved cell), with its longest edge and that cell's sides."""
    c_ = pts.mean(axis=0)
    ed = float(np.linalg.norm(pts - np.roll(pts, -1, axis=0), axis=1).max())
    inside = [f_ for f_ in cells if f_[0] == k and bool((c_ >= f_[1] - 1e-6).all()) and bool((c_ <= f_[2] + 1e-6).all())]
    if not inside:
        return {"element": shape, "longest_edge_mm": round(ed, 3), "lies_in": "a paved cell (no grid, band or transition)"}
    f_ = min(inside, key=lambda x: float(np.prod(np.maximum(x[2] - x[1], 1e-9)[:2])))
    return {"element": shape, "longest_edge_mm": round(ed, 3), "lies_in": f_[4], "cell_sides_mm_segments": f_[5]}


def _edge_hard(requirements: dict | None) -> tuple[bool, bool]:
    """SHMESH-51o29/51o35: (the shortest edge is hard, the longest edge is hard) - THE ONE PLACE the user's word on
    their edge lengths is read. A row of `requirements["rows"]` for min_edge / max_edge says it as `strictness`
    ("hard" / "flexible", the notes reader's; mesh_requirements.strictness(row) where that exists, whose default for
    these rows is "flexible"); the older whole-mesh `edge_hard` switch makes both hard."""
    from meshpipeline.engines import mesh_requirements as MR_

    req = requirements or {}
    both = bool(req.get("edge_hard"))
    hard = {"min_edge": both, "max_edge": both}
    read = getattr(MR_, "strictness", None)
    for row in req.get("rows") or []:
        f_ = str(row.get("field"))
        if f_ in hard:
            word = read(row) if read is not None else str(row.get("strictness") or "flexible")
            hard[f_] = hard[f_] or word == "hard"
    return hard["min_edge"], hard["max_edge"]


def _edges_hard(requirements: dict | None) -> bool:
    """SHMESH-51o29: whether both edge lengths are hard (kept for its callers; _edge_hard reads each side)."""
    lo, hi = _edge_hard(requirements)
    return lo and hi


def _high_tolerant(sizing: dict | None, requirements: dict | None) -> float | None:
    """SHMESH-51o39: the user's longest edge plus EDGE_TOL when it is flexible; itself when hard."""
    high = (sizing or {}).get("high_mm")
    if high is None:
        return None
    return float(high) * (1 + (0.0 if _edge_hard(requirements)[1] else EDGE_TOL))


def _low_tolerant(sizing: dict | None, requirements: dict | None) -> float | None:
    """SHMESH-51o29: the user's shortest edge less EDGE_TOL when it is flexible (the edge counted short in a face's
    figures); itself when hard."""
    low = (sizing or {}).get("low_mm")
    if low is None:
        return None
    return float(low) * (1 - (0.0 if _edge_hard(requirements)[0] else EDGE_TOL))


def _polish_trias(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, movable: np.ndarray, on_face: np.ndarray,
                  lim, high_hard: float | None = None, edges=None) -> np.ndarray:
    """SHMESH-51o31: each triangle under the user's smallest-angle target for triangles, its movable nodes on flat
    faces tried in small steps in the plane of the elements round them; a step is kept where the triangle's smallest
    angle rises and no element round the node comes out past the solver's limits or worse on the user's lines."""
    tg = [a for a in lim.aims if a[0] == "tria" and a[1] == "min_angle_tria" and a[2] == ">="]
    if not tg:
        return xyz
    target = float(max(a[3] for a in tg))
    m_t = shell_quality.tria_measures(xyz, T)
    bad = np.flatnonzero(m_t["min_angle_tria"] < target)
    # (SHMESH-51o41: at most TRIA_POLISH_MAX of them, the sharpest first - a triangle-heavy part costs no more)
    bad = bad[np.argsort(m_t["min_angle_tria"][bad], kind="stable")][:TRIA_POLISH_MAX]
    if not len(bad):
        return xyz
    xyz = np.array(xyz)
    q_of: dict[int, list[int]] = {}
    t_of: dict[int, list[int]] = {}
    for i_, e_ in enumerate(Q.tolist() if len(Q) else []):
        for v_ in e_:
            q_of.setdefault(int(v_), []).append(i_)
    for i_, e_ in enumerate(T.tolist()):
        for v_ in e_:
            t_of.setdefault(int(v_), []).append(i_)

    def longest(els: np.ndarray) -> float:
        return float(np.linalg.norm(xyz[els] - xyz[np.roll(els, -1, axis=1)], axis=2).max()) if len(els) else 0.0

    def state(v_: int) -> np.ndarray:
        """SHMESH-51o40/51o45: round the node, the elements past the solver's limits, then per line of the user's
        (_line_counts, quads and triangles) - every element, the other triangles under the target too (a move that
        took one triangle up took another past the user's 20 degree outer line, and the part kept its free mesh
        without washers)."""
        qs, ts = q_of.get(v_, []), t_of.get(v_, [])
        past = 0
        if high_hard is not None:
            # (SHMESH-51o36: a hard longest edge counts as a limit: an element over it is past)
            over = max(longest(Q[qs]) if qs else 0.0, longest(T[ts]) if ts else 0.0) > float(high_hard)
            past += int(over)
        if qs:
            mq = shell_quality.quad_measures(xyz, Q[qs], extra=lim.extra)
            past += int(shell_quality._quad_past(mq).sum())
        mt = shell_quality.tria_measures(xyz, T[ts], extra=lim.extra)
        past += int(shell_quality._tria_past(mt).sum())
        lines = (_line_counts(xyz, Q[qs] if qs else np.zeros((0, 4), np.int64), "quad", lim, edges)
                 + _line_counts(xyz, T[ts], "tria", lim, edges))
        return np.concatenate([[past], lines])

    for t_i in bad.tolist():
        for _round in range(TRIA_POLISH_ROUNDS):
            moved = False
            for v_ in T[t_i].tolist():
                if v_ >= len(movable) or not movable[v_] or on_face[v_] >= 0:
                    continue
                if not _flat_round(xyz, Q, T, q_of.get(v_, []), t_of.get(v_, [])):
                    continue        # (SHMESH-51o45: a node on a fold stays)
                p0 = xyz[v_].copy()
                tri = xyz[T[t_i]]
                nrm = np.cross(tri[1] - tri[0], tri[2] - tri[0])
                nn = float(np.linalg.norm(nrm))
                if nn <= 0:
                    continue
                nrm /= nn
                e1 = tri[1] - tri[0]
                e1 /= max(float(np.linalg.norm(e1)), 1e-300)
                e2 = np.cross(nrm, e1)
                h_ = float(np.linalg.norm(tri - np.roll(tri, -1, axis=0), axis=1).mean())
                base_ang = float(shell_quality.tria_measures(xyz, T[[t_i]])["min_angle_tria"][0])
                base_st = state(v_)
                best = (base_ang, p0)
                for step in (0.15, 0.08, 0.04):
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                        xyz[v_] = p0 + step * h_ * (dx * e1 + dy * e2)
                        ang = float(shell_quality.tria_measures(xyz, T[[t_i]])["min_angle_tria"][0])
                        if ang > best[0] + 1e-6:
                            st = state(v_)
                            if bool((st <= base_st).all()):
                                best = (ang, xyz[v_].copy())
                xyz[v_] = best[1]
                moved = moved or best[0] > base_ang + 1e-6
            if not moved or float(shell_quality.tria_measures(xyz, T[[t_i]])["min_angle_tria"][0]) >= target:
                break
    return xyz


def _line_counts(xyz: np.ndarray, els: np.ndarray, shape: str, lim, edges=None) -> np.ndarray:
    """SHMESH-51o45: of these elements of one shape, per line - each of the user's limits (outside its target, past
    its outer line), then the edges (under the user's shortest edge, under its band; over the longest, over its band)
    - the counts, as one array. A move or a cut is kept only where no entry rises: one line's gain never pays for
    another's loss (summed, a move that lifted two triangles from past the 20 degree line put two quads past the
    30 degree warp line, on sh0042). (SHMESH-84: the one counting helper lives in shell_quality.line_counts.)"""
    return shell_quality.line_counts(xyz, els, shape, lim, edges)


def _flat_round(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, qs: list[int], ts: list[int]) -> bool:
    """SHMESH-51o45: whether the elements round a node lie in one plane (within 1 degree): a node on a fold - two
    faces' elements meeting at it, as a merged seam can leave a face's own node - never moves in a plane."""
    nrm = []
    for els in (Q[qs] if qs else np.zeros((0, 4), np.int64), T[ts] if ts else np.zeros((0, 3), np.int64)):
        for e in els:
            p_ = xyz[e]
            n_ = np.cross(p_[2] - p_[0], p_[-1] - p_[1]) if len(e) == 4 else np.cross(p_[1] - p_[0], p_[2] - p_[0])
            nn = float(np.linalg.norm(n_))
            if nn > 0:
                nrm.append(n_ / nn)
    return all(abs(float(a @ nrm[0])) >= math.cos(math.radians(1.0)) for a in nrm)


def _tria_target(lim) -> float | None:
    """SHMESH-51o32: the user's smallest-angle target for triangles, if any."""
    tg = [float(a[3]) for a in lim.aims if a[0] == "tria" and a[1] == "min_angle_tria" and a[2] == ">="]
    return max(tg) if tg else None


def _recut_trias(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, q_face: list, t_face: list, lim,
                 movable: np.ndarray | None = None, high_hard: float | None = None, edges=None):
    """SHMESH-51o32: each triangle under the user's angle target and a quad of its face sharing an edge with it, cut
    again as the pentagon they make allows (a quad and a triangle), the best where the triangle's smallest angle rises,
    the quad passes the solver's limits and neither is worse on the user's lines. Returns (Q, T, q_face, t_face, n)."""
    target = _tria_target(lim)
    if target is None or not len(T) or not len(Q):
        return Q, T, q_face, t_face, 0
    Q, T = np.array(Q), np.array(T)
    q_face, t_face = list(q_face), list(t_face)
    edge_q: dict[tuple, list[int]] = {}
    for i_, e_ in enumerate(Q.tolist()):
        for k_ in range(4):
            edge_q.setdefault(tuple(sorted((e_[k_], e_[(k_ + 1) % 4]))), []).append(i_)
    n_done = 0
    # (SHMESH-51o53: the elements' node sets, so a cut never makes one that is already there)
    have: set[tuple] = {tuple(sorted(e_)) for e_ in Q.tolist()} | {tuple(sorted(e_)) for e_ in T.tolist()}
    mt = shell_quality.tria_measures(xyz, T)
    under = np.flatnonzero(mt["min_angle_tria"] < target)
    # (SHMESH-51o41: the sharpest TRIA_POLISH_MAX, as the polish)
    for t_i in under[np.argsort(mt["min_angle_tria"][under], kind="stable")][:TRIA_POLISH_MAX].tolist():
        tri = T[t_i].tolist()
        base = shell_quality._least_angle(xyz, tri)
        best = None
        for k_ in range(3):
            a, b = tri[k_], tri[(k_ + 1) % 3]
            c = tri[(k_ + 2) % 3]
            for q_i in edge_q.get(tuple(sorted((a, b))), []):
                if q_face[q_i] != t_face[t_i]:
                    continue
                q = Q[q_i].tolist()
                if movable is not None and any(v_ not in (a, b) and (v_ >= len(movable) or not movable[v_])
                                               for v_ in q):
                    continue        # (a ring's, a band's or a grid's quad stays as it was made)
                # the pentagon: the quad's corners from b round to a, then the triangle's far corner
                j = q.index(b)
                ring = q[j:] + q[:j]
                if ring[-1] != a:
                    ring = ring[::-1]
                    j = ring.index(b)
                    ring = ring[j:] + ring[:j]
                    if ring[-1] != a:
                        continue
                poly = ring + [c]
                # (SHMESH-51o45: the two elements' counts per line, as they stand)
                base_l = (_line_counts(xyz, np.array([q]), "quad", lim, edges)
                          + _line_counts(xyz, np.array([tri]), "tria", lim, edges))
                for split in shell_quality._splits(poly):
                    if len(split) != 2:
                        continue
                    nq = [e for e in split if len(e) == 4]
                    nt = [e for e in split if len(e) == 3]
                    if len(nq) != 1 or len(nt) != 1 or sorted(nq[0]) == sorted(q):
                        continue
                    # SHMESH-51o53: never an element with a corner twice, nor one whose corners another element
                    # already has (on the board 4 parts came out with duplicate elements - invalid decks - from the
                    # re-cut alone: their elements and triangles otherwise as the deployed build's)
                    if (len(set(nq[0])) != 4 or len(set(nt[0])) != 3
                            or tuple(sorted(nq[0])) in have - {tuple(sorted(q))}
                            or tuple(sorted(nt[0])) in have - {tuple(sorted(tri))}):
                        continue
                    if shell_quality._past_one(xyz, nq[0], lim) or shell_quality._past_one(xyz, nt[0], lim):
                        continue
                    if high_hard is not None and any(
                            float(np.linalg.norm(xyz[e] - xyz[np.roll(e, -1)], axis=1).max()) > float(high_hard)
                            for e in (np.array(nq[0]), np.array(nt[0]))):
                        continue        # (SHMESH-51o36: a hard longest edge holds)
                    # (SHMESH-51o40/51o45: no line's count rises - outside a target, past an outer line, the edges)
                    new_l = (_line_counts(xyz, np.array([nq[0]]), "quad", lim, edges)
                             + _line_counts(xyz, np.array([nt[0]]), "tria", lim, edges))
                    if not bool((new_l <= base_l).all()):
                        continue
                    ang = shell_quality._least_angle(xyz, nt[0])
                    if ang > base + 1e-6 and (best is None or ang > best[0]):
                        best = (ang, q_i, nq[0], nt[0])
        if best is not None:
            _a, q_i, nq_b, nt_b = best
            # (wound as the elements they replace)
            nrm_old = shell_quality._elem_normal(xyz, Q[q_i].tolist())
            if float(shell_quality._elem_normal(xyz, nq_b) @ nrm_old) < 0:
                nq_b = nq_b[::-1]
            if float(shell_quality._elem_normal(xyz, nt_b) @ nrm_old) < 0:
                nt_b = nt_b[::-1]
            have -= {tuple(sorted(Q[q_i].tolist())), tuple(sorted(T[t_i].tolist()))}
            old_q = Q[q_i].tolist()
            Q[q_i] = nq_b
            T[t_i] = nt_b
            have |= {tuple(sorted(nq_b)), tuple(sorted(nt_b))}
            n_done += 1
            # (SHMESH-28c: the edge table kept as a rebuild over every quad makes it - quad numbers in order - by
            # taking this quad off its old edges and onto its new ones; rebuilt whole after each re-cut, it took 96 s
            # of a 102,000-element spot pass)
            for k_ in range(4):
                key_ = tuple(sorted((old_q[k_], old_q[(k_ + 1) % 4])))
                if key_ in edge_q:
                    edge_q[key_] = [x_ for x_ in edge_q[key_] if x_ != q_i]
                    if not edge_q[key_]:
                        del edge_q[key_]
            new_e = Q[q_i].tolist()
            for k_ in range(4):
                bisect.insort(edge_q.setdefault(tuple(sorted((new_e[k_], new_e[(k_ + 1) % 4]))), []), q_i)
    return Q, T, q_face, t_face, n_done


def _node_why(n: int, movable: np.ndarray, on_face: np.ndarray, node_kind: np.ndarray, pinned: set) -> list[str]:
    """SHMESH-51o34: for each node, why it may or may not move (pinned band or transition node, CAD vertex, CAD edge,
    curved face, a ring's or grid's node, free)."""
    out = []
    for v_ in range(n):
        if v_ in pinned:
            out.append("a band's or transition cell's node (pinned)")
        elif v_ < len(node_kind) and int(node_kind[v_]) == shell_quality.AT_VERTEX:
            out.append("at a CAD vertex")
        elif v_ < len(node_kind) and int(node_kind[v_]) == shell_quality.ON_EDGE:
            out.append("on a CAD edge")
        elif v_ < len(on_face) and on_face[v_] >= 0:
            out.append("on a curved face (not moved by the polish)")
        elif v_ >= len(movable) or not movable[v_]:
            out.append("not movable (a ring's or a mapped grid's)")
        else:
            out.append("free")
    return out


def _trias_told(mesh, lim, node_why: list[str]) -> list[dict]:
    """SHMESH-51o32/51o34: each delivered triangle under the user's angle target: its mid-face, its three corner angles
    (from its three nodes as delivered) and, for each node, why it may or may not move."""
    target = _tria_target(lim)
    if target is None or not len(mesh.trias):
        return []
    ang = shell_quality._angles(mesh.xyz[mesh.trias])
    nq = len(mesh.quads)
    out = []
    for t_i in np.flatnonzero(ang.min(axis=1) < target).tolist():
        out.append({"face": str(mesh.mid[nq + t_i]), "angles": [round(float(a), 1) for a in ang[t_i]],
                    "nodes": [node_why[int(v_)] if int(v_) < len(node_why) else "?" for v_ in mesh.trias[t_i]]})
    return out


def _tria_share_asked(requirements: dict | None) -> float | None:
    """SHMESH-51o33: the triangle share the user asked for ("at most 5% triangles": kind.tria_percent_max), as a
    fraction; None when they gave none."""
    p_ = ((requirements or {}).get("kind") or {}).get("tria_percent_max")
    if p_ is None:
        p_ = (requirements or {}).get("tria_share")
        return None if p_ is None else float(p_)
    return float(p_) / 100.0


def _cut_faces(cut_of: dict, join_of: dict) -> set[int]:
    """SHMESH-51o22: the gridded faces and every face their lines cut (a joined face beside a grid, cut into cells by
    the lines carried across the joint, is where a held count's transition cell lies - on the real column the 12-hole
    plate's freed cells beside a strip were in such a face, never tried, and paved: 12 triangles)."""
    out = set(cut_of) | set(join_of)
    for v in list(cut_of.values()) + list(join_of.values()):
        out |= {int(x) for x in v}
    return out


def _transition_cells(gmsh, tag_of: dict[int, int], faces: set[int], grids, rings: list, done,
                      freed: set[int] | frozenset = frozenset()) -> list[int]:
    """SHMESH-51o17: the free cells of these sewn faces that gmsh paved with triangles (no mapped cell, no washer's
    annulus or box, none already replaced)."""
    skip = set(grids) | set(done) | set(freed) | {w.annulus for w in rings} | {w.box for w in rings if w.box}
    out = []
    for t, k in sorted(tag_of.items()):
        if k not in faces or t in skip:
            continue
        types, _tags, _conns = gmsh.model.mesh.getElements(2, t)
        if any(int(et) == 2 for et in types):
            out.append(t)
    return out


#: SHMESH-51o42: a transition cell is kept only where it is no worse than gmsh's paving of the same cell
TRANSITION_BOTH_WAYS = True


def _cell_counts(xyz: np.ndarray, quads: np.ndarray, trias: np.ndarray, lim, low: float | None, high: float | None,
                 hard: tuple[bool, bool]) -> tuple[int, int, int]:
    """SHMESH-51o42: of one cell's elements, (past the solver's limits, past a user's outer limit or outside the edge
    band, outside a user's target or edge length). The band: the user's edges with EDGE_TOL either side where they
    are flexible, the edges themselves where hard."""
    past = outer = outside = 0
    lo_b = None if low is None else float(low) * (1 - (0.0 if hard[0] else EDGE_TOL))
    hi_b = None if high is None else float(high) * (1 + (0.0 if hard[1] else EDGE_TOL))
    for els, shape in ((quads, "quad"), (trias, "tria")):
        if not len(els):
            continue
        els = np.asarray(els, dtype=np.int64)
        m = (shell_quality.quad_measures if shape == "quad" else shell_quality.tria_measures)(xyz, els,
                                                                                             extra=lim.extra)
        past += int((shell_quality._quad_past(m) if shape == "quad" else shell_quality._tria_past(m)).sum())
        sc = shell_quality.aim_score(m, shape, lim, len(els))
        ed = np.linalg.norm(xyz[els] - xyz[np.roll(els, -1, axis=1)], axis=2)
        e_lo, e_hi = ed.min(axis=1), ed.max(axis=1)
        out_band = np.zeros(len(els), dtype=bool)
        out_tg = sc > 0
        if low is not None:
            out_band |= e_lo < lo_b
            out_tg |= e_lo < float(low)
        if high is not None:
            out_band |= e_hi > hi_b
            out_tg |= e_hi > float(high)
        outer += int(((sc >= shell_quality.AIM_OUTER) | out_band).sum())
        outside += int(out_tg.sum())
    return past, outer, outside


def _transition_kept(gmsh, t: int, index: np.ndarray, xyz: np.ndarray, quads_t: np.ndarray, lim,
                     low: float | None, high: float | None, hard: tuple[bool, bool]) -> tuple[bool, dict]:
    """SHMESH-51o42: the cell built both ways - gmsh's paving (as meshed) and the transition rows - and the
    transition kept only where it has no more elements past the solver's limits, none more past an outer limit or
    the edge band, and none more outside a user target or edge length than the paving; triangles count only after
    that (the transition has none). Its boundary nodes are the paving's, so the cells beside it are the same either
    way. Returns (kept, the two counts)."""
    types, _tags, conns = gmsh.model.mesh.getElements(2, t)
    pq, pt = [], []
    for et, conn in zip(types, conns, strict=True):
        c = index[np.asarray(conn, dtype=np.int64)]
        if int(et) == 3:
            pq.append(c.reshape(-1, 4))
        elif int(et) == 2:
            pt.append(c.reshape(-1, 3))
    paved = _cell_counts(xyz, np.vstack(pq) if pq else np.zeros((0, 4), np.int64),
                         np.vstack(pt) if pt else np.zeros((0, 3), np.int64), lim, low, high, hard)
    made = _cell_counts(xyz, quads_t, np.zeros((0, 3), np.int64), lim, low, high, hard)
    kept = all(m_ <= p_ for m_, p_ in zip(made, paved, strict=True))
    return kept, {"paved": list(paved), "transition": list(made)}


#: SHMESH-51o19: one start again for a pass's bolt holes and its faces' SHMESH-51b one ring together
BATCH_BOLT_KEYS = True


def _two_faces(washers: list, placed: list) -> set[int]:
    """SHMESH-51b/51o12: the sewn faces holding two-ring bolt holes (51b gives up their second rings first)."""
    return {int(w.owner) for w in (washers if BOLT_FIRST_ANY else placed) if w.boxed2}


#: SHMESH-51o14: a face whose bolt holes were held to one ring for its grid on this pass (SHMESH-51b's retry at
#: planning) and whose grid is refused after meshing all the same keeps them at one ring in its free mesh (its bolt
#: key with its face's), as a face 51b named after meshing does: the real column's plate E (and its twin) was refused
#: its grid with one ring round its holes, took two rings back in its free face on the next pass and ended with 12
#: triangles where one ring had 10 (its twin 12 to 6)
#: SHMESH-51o51: OFF - the owner's order puts two rings (structure) before the triangle count: a face whose grid is
#: refused with its holes at one ring now meshes free with two rings where they fit, as any free face does (on the
#: real column the two 6-hole plates kept one ring on all 22 holes for 2 triangles each)
ONE_KEPT_FREE = False    # (SHMESH-51o51: on again with SHMESH-51o56)


def _one_for_grid(washers: list) -> set[int]:
    """SHMESH-51o14: the sewn faces with bolt holes held to one ring for their grid on this pass."""
    return {int(w.owner) for w in washers if w.info.get("one_ring") == "one ring so its face keeps its grid"
            and w.info.get("bolt")} if ONE_KEPT_FREE else set()


def _grid_first(hurt: set[int], why: dict[int, dict], two: set[int], made: set[int], no_cut,
                one_kept: set[int] | None = None, under: set[int] | None = None) -> None:
    """SHMESH-51b: in a set of faces refused their grids, each gridded face holding two-ring bolt holes gives up the
    second ring first (its key -k-1-BOLT_GRID_KEY in place of k), unless it already has."""
    if not ST.BOLT_GRID_FIRST:
        return
    for kp in sorted(hurt & two & made):
        ku_ = -(kp + UNDER_LOW_KEY) - 1
        if kp in (under or set()) and ku_ not in no_cut:
            # SHMESH-51o57: a face with rings planned under the user's flexible shortest edge gives those back first -
            # its other holes keep their second rings (on the web look-alike 8 such holes sent all 70 of the face to
            # one ring)
            hurt.discard(kp)
            hurt.add(ku_)
            why[ku_] = {k_: v_ for k_, v_ in why.get(kp, {}).items() if k_ != "grid_figures"}
            why[ku_].update(kind="bolt rings in a grid", then="the rings under the shortest edge back to the edges")
            continue
        kb_ = -(kp + BOLT_GRID_KEY) - 1
        if kb_ not in no_cut:
            hurt.discard(kp)
            hurt.add(kb_)
            why[kb_] = {k_: v_ for k_, v_ in why.get(kp, {}).items() if k_ != "grid_figures"}
            why[kb_].update(kind="bolt rings in a grid", then="one ring so its face keeps its grid")
    for kp in sorted(hurt & (one_kept or set()) & made):
        # (SHMESH-51o14: its holes already on one ring for this grid: the face goes free, its holes keep one ring)
        kb_ = -(kp + BOLT_GRID_KEY) - 1
        if kb_ not in no_cut and kb_ not in hurt:
            hurt.add(kb_)
            why[kb_] = {k_: v_ for k_, v_ in why.get(kp, {}).items() if k_ != "grid_figures"}
            why[kb_].update(kind="bolt rings in a grid", then="one ring kept in its free mesh (held for its grid)")


def _bolt_refused(no_cut) -> dict[int, str]:
    """SHMESH-51/51b: {sewn face: why its bolt holes keep one ring} from the faces refused."""
    out: dict[int, str] = {}
    for x in sorted(no_cut):
        if x < 0:
            y = -x - 1
            if y >= BOLT_GRID_KEY:
                # (kept when the grid is refused with one ring too: its free mesh is then set against the grid as
                # the grid was meshed, one ring a hole - a second ring never wins a face from its grid)
                out[y - BOLT_GRID_KEY] = "one ring so its face keeps its grid"
            elif y >= UNDER_LOW_KEY:
                continue        # (SHMESH-51o57: only the rings under the shortest edge go; the face keeps its others)
            else:
                out.setdefault(y, "two rings left a quality line unmet on its face")
    return out


def _owner_of_key(k: int, owners: list[str]) -> str:
    """The sewn face a refused key names (a face, or a face's bolt rings)."""
    if 0 <= k < len(owners):
        return owners[k]
    y = -k - 1
    y = y - BOLT_GRID_KEY if y >= BOLT_GRID_KEY else (y - UNDER_LOW_KEY if y >= UNDER_LOW_KEY else y)
    return owners[y] if 0 <= y < len(owners) else str(k)


def _bolt_rings(mesh: ShellMesh, hole_lines: list[dict]) -> list[dict]:
    """SHMESH-51: the bolt holes' lines, each with the whole rings of quads round it in the mesh as built (`rings`,
    measured: the hole found by its centre)."""
    measured = SSM.holes(mesh)
    out = []
    for h in hole_lines:
        if not h.get("bolt"):
            continue
        at = np.asarray(h.get("at_mm") or [math.inf] * 3, dtype=float)
        tol = 0.25 * float(h.get("diameter_mm") or 0.0)
        got = [m for m in measured if float(np.linalg.norm(np.asarray(m["centre"], dtype=float) - at)) <= tol
               and abs(float(m["diameter_mm"]) - float(h["diameter_mm"])) <= 0.1 * float(h["diameter_mm"])]
        h["rings"] = max((int(m.get("rings") or 0) for m in got), default=0)
        if h.get("shape") == "square":
            # (SHMESH-51o7: a square bolt hole's rows of its square ring, as planned and drawn - the measure above
            # finds round holes only)
            h["rings"] = int(h.get("planned_rows") or 0) if h.get("washer") else 0
            h["rings_from"] = "plan"
        if h["rings"] >= ST.BOLT_RINGS and h.get("one_ring"):
            # (the line says what was built: a hole planned with one ring that has two or more round it in the mesh - an
            # O-grid's rows to its box, a round plate's square - keeps no "one ring" reason)
            h["planned_one_ring"] = h.pop("one_ring")
        out.append(h)
    return out


def _bolt_words(lines: list[dict]) -> list[str]:
    """SHMESH-51: what the user reads - one line for which holes were taken as bolt holes and why, one for their
    rings as built (plain words, counts)."""
    kinds = Counter(str(h["bolt"]).split(" (")[0] for h in lines)
    short = {"it lines up with a hole in another plate": "line up with a hole in another plate",
             "its plate is joined to another plate": "are in a plate joined to another plate"}
    parts = []
    for k, n in sorted(kinds.items(), key=lambda x: (-x[1], x[0])):
        parts.append(f"{n} " + (short.get(k) or ("are one of a pattern of alike holes" if k.startswith("one of")
                                                 else k)))
    out = [f"{len(lines)} round hole(s) taken as bolt holes, so they get two rings of elements where they fit: "
           + "; ".join(parts) + "."]
    two = [h for h in lines if int(h.get("rings") or 0) >= ST.BOLT_RINGS]
    one = [h for h in lines if int(h.get("rings") or 0) == 1]
    none = [h for h in lines if int(h.get("rings") or 0) == 0]
    words = f"Two rings round {len(two)} of the {len(lines)} bolt holes" if two else \
        f"No bolt hole has two rings ({len(lines)} found)"
    if one:
        said = {"no room": "there was no room for a second",
                "the edge lengths": "your edge lengths leave no room for a second",
                "two rings left": "a second left one of your quality lines unmet",
                "one ring so its face keeps its grid": "a second would break the plate's straight grid"}
        why_one = Counter(next((v for k_, v in said.items() if str(h.get("one_ring", "")).startswith(k_)), "")
                          for h in one)
        words += f"; one ring round {len(one)}" + (
            f" ({', '.join(f'{n} where {w}' if len(why_one) > 1 else f'where {w}' for w, n in sorted(why_one.items()) if w)})"
            if any(why_one) else "")
    if none:
        words += f"; no ring round {len(none)} (too near an edge or another hole)"
    out.append(words + ".")
    return out


def _wants_redraw(w, drawn_at: list) -> bool:
    """SHMESH-45k: a hole whose ring had to follow uneven hole nodes, or could not be built or closed, and that is not
    drawn again yet: _mesh_path draws it again as one circle."""
    return bool((w.info.get("followed") or str(w.info.get("why", "")).startswith(("its ring could not be built",
                                                                                   "its ring did not close")))
                and not any(float(np.linalg.norm(c - w.centre)) <= 0.25 * w.r for c in drawn_at))


def _spots(mesh: ShellMesh, face_of: np.ndarray, past: np.ndarray) -> tuple[list[tuple], int]:
    """SHMESH-31: boxes (sewn face, low corner, high corner; mm) round the elements `past`, each padded by SPOT_PAD
    times its element's longest edge (SHMESH-31d: none; _mesh_once adds SPOT_PAD_H new sizes); boxes of one face that
    overlap are joined. SHMESH-31b: worst first (the summed distance
    past the solver's limits of their elements, shell_quality.solver_distance), at most SPOTS_MAX of them. Returns
    (the boxes, how many were left out); each box carries its elements' longest edge (SHMESH-31e: its reach)."""
    nq = len(mesh.quads)
    far = np.zeros(mesh.n_elements)
    if nq:
        far[:nq] = shell_quality.solver_distance(shell_quality.quad_measures(mesh.xyz, mesh.quads), "quad",
                                                 shell_quality.IN_FORCE, nq)
    if len(mesh.trias):
        far[nq:] = shell_quality.solver_distance(shell_quality.tria_measures(mesh.xyz, mesh.trias), "tria",
                                                 shell_quality.IN_FORCE, len(mesh.trias))
    boxes: list[list] = []
    for e in np.flatnonzero(past).tolist():
        p = mesh.xyz[mesh.element_nodes(int(e))]
        longest = float(np.linalg.norm(p - np.roll(p, 1, axis=0), axis=1).max())
        pad = SPOT_PAD * longest
        boxes.append([int(face_of[e]), p.min(axis=0) - pad, p.max(axis=0) + pad, float(far[e]) + 1e-9, longest])
    out: list[list] = []
    for b in sorted(boxes, key=lambda x: (x[0], *x[1].tolist())):
        for o in out:
            if o[0] == b[0] and np.all(o[1] <= b[2]) and np.all(b[1] <= o[2]):
                o[1], o[2], o[3] = np.minimum(o[1], b[1]), np.maximum(o[2], b[2]), o[3] + b[3]
                o[4] = max(o[4], b[4])
                break
        else:
            out.append([b[0], b[1].copy(), b[2].copy(), b[3], b[4]])
    out.sort(key=lambda x: (-x[3], x[0], *x[1].tolist()))
    kept = out[:SPOTS_MAX]
    return ([(k, tuple(float(f"{v:.6g}") for v in lo), tuple(float(f"{v:.6g}") for v in hi), float(f"{ln:.6g}"))
             for k, lo, hi, _f, ln in kept], len(out) - len(kept))


def _grown(own: list, past: list) -> list:
    """SHMESH-31e: the spot boxes `own` after a pass that left the elements `past` (their boxes): a box whose reach
    (its elements' longest edge round it, the padding before SHMESH-31d) holds an element still past is grown to that
    reach for the next pass; the others stand. Each box: (sewn face, low, high, step, reach, grown)."""
    out = []
    for b in own:
        reach = float(b[4]) if len(b) > 4 else 0.0
        grown = bool(b[5]) if len(b) > 5 else False
        if SPOT_GROW and not grown and reach > 0.0:
            lo, hi = np.asarray(b[1], dtype=float) - reach, np.asarray(b[2], dtype=float) + reach
            grown = any(np.all(np.asarray(p[1], dtype=float) <= hi) and np.all(lo <= np.asarray(p[2], dtype=float))
                        for p in past)
        out.append((b[0], b[1], b[2], b[3] if len(b) > 3 else 0, reach, grown))
    return out


def _min_jac(mesh: ShellMesh) -> np.ndarray:
    """Each element's smallest corner Jacobian (quads, then triangles)."""
    q = shell_quality.quad_measures(mesh.xyz, mesh.quads)["min_jacobian"] if len(mesh.quads) else np.zeros(0)
    t = shell_quality.tria_measures(mesh.xyz, mesh.trias)["min_jacobian"] if len(mesh.trias) else np.zeros(0)
    return np.concatenate([q, t])


def _stamped_owns(record: dict | None) -> bool:
    """SHMESH-51o68: whether the stamped-sheet strip rules (SHMESH-60..69: a strip narrower than the shortest edge
    mapped one row across and closed) size this part's narrow faces - a surface file, or every part with STAMPED_ALL
    on. Where they do, narrow-along stands aside."""
    return bool((record or {}).get("source") == "surface" or globals().get("STAMPED_ALL") is True)


def _along(sizing: dict | None, record: dict | None = None) -> tuple[float, float, float] | None:
    """SHMESH-51o63: (the plan's stretch, the user's shortest edge, their longest edge, mm) when NARROW_ALONG is on and
    the user typed both; else None (SHMESH-51o68: and None where the stamped strip rules own the narrow faces)."""
    if not NARROW_ALONG or not sizing or _stamped_owns(record):
        return None
    st, lo, hi = sizing.get("stretch"), sizing.get("low_mm"), sizing.get("high_mm")
    if st is None or lo is None or hi is None:
        return None
    return float(st), float(lo), float(hi)


def _narrow_faces(rows: list[dict], facts: list[dict], mm: float, planned: float,
                  interior: set[int] | None = None, slivers: frozenset[int] | set[int] = frozenset(),
                  low_mm: float | None = None,
                  along: tuple[float, float, float] | None = None,
                  along_skip: frozenset[int] | set[int] = frozenset()) -> dict | None:
    """SHMESH-27, faces: under the user's sizes every face narrower than 1 / FEATURE_FACE_RATIO of its size (its
    strip width: twice its area over its perimeter) is sized to FEATURE_FACE_RATIO times its own width, never under
    the floor, in place - a 0.3 mm step strip at a 3 mm size was one row of elements ten times as long as wide. With
    FEATURE_FACES off, only a face narrower than the floor itself (half its wall: a sliver strip) is - every one with
    SLIVER_FLOOR, else those in `slivers` (SHMESH-31: faces a first mesh left elements past the solver's limits on):
    it takes the floor, as our own rules size it, so the sliver merge (SHMESH-12) closes it as it does with no notes
    (fr014's 0.28 mm step strips at 3.2 mm: 137 triangles past Nastran's T3_IAMAX; none with no notes). Returns
    {"was": the rows changed as they were, "widths": their widths, "planned", "flank_faces"}; None (rows untouched)
    when the finer plan would pass SHMESH-20's most elements.
    SHMESH-35: a FLANK - a face narrower than its size held along its sides by other faces (`interior`,
    _interior_faces: a bead's or a dart's wall, a step, a rib) that is not itself a tight bend - keeps
    plan_rules.FLANK_ACROSS elements across it (never under the floor), so a bead is not washed out under a coarse
    size. None: no flank rule."""
    new: dict[int, float] = {}
    widths: dict[int, float] = {}
    flank_faces: set[int] = set()
    along_faces: set[int] = set()
    for k, (f, r) in enumerate(zip(facts, rows, strict=True)):
        width = min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"]
        if along is not None and 0 < width < along[1] and k not in along_skip:
            # SHMESH-51o63: narrower than the user's shortest edge - one element across, as long along as their
            # aspect target allows (stretch, low, high = along)
            h = min(along[0] * width, 0.98 * along[2], float(r["size_mm"]))
            h = max(h, plan_rules.FLOOR_WALLS * r["wall_mm"])
            if h < float(r["size_mm"]):
                new[k], widths[k] = plan_rules._r(h), width
            along_faces.add(k)
            continue
        if interior is not None and k in interior and not (
                f["bend"] is not None and 0 < f["bend"] < plan_rules.PANEL_WALLS * r["wall_mm"]):
            # SHMESH-61: never so many across that an element comes out under the user's shortest edge (2 across a
            # 3.4 mm flank at a 2 mm shortest edge made 1.7 mm edges along the whole bead); one across is the size
            across = plan_rules.FLANK_ACROSS
            if low_mm is not None:
                across = min(across, int(math.floor(width / float(low_mm) + 1e-9)))
            h = (plan_rules.flank_size(width, size=r["size_mm"], wall=r["wall_mm"], across=across)
                 if across >= 2 else None)
            if h is not None:
                new[k], widths[k] = h, width
                flank_faces.add(k)
                continue
        if not FEATURE_FACES and (not (SLIVER_FLOOR or k in slivers)
                                  or width >= plan_rules.FLOOR_WALLS * r["wall_mm"]):
            continue
        h = plan_rules.feature_size(width, size=r["size_mm"], wall=r["wall_mm"], ratio=plan_rules.FEATURE_FACE_RATIO)
        if h is not None:
            new[k] = h
            widths[k] = width
    extra = sum(facts[k]["area"] * mm * mm * (1.0 / h ** 2 - 1.0 / rows[k]["size_mm"] ** 2) for k, h in new.items())
    if planned + extra > PLANNED_MAX:
        return None
    was = {k: dict(rows[k]) for k in new}
    for k, h in new.items():
        rows[k]["size_mm"] = h
        if rows[k].get("curve_mm", 0.0) >= h:
            rows[k].pop("curve_mm", None)
        rows[k]["rule"] = f"{rows[k]['rule']}+SHMESH-27"
    return {"was": was, "widths": widths, "planned": planned + extra, "flank_faces": flank_faces,
            "along_faces": along_faces}


#: SHMESH-51o64: under a shortest edge the user typed, the record says how many elements are under it and why - a
#: small hole, a short CAD edge, a gap or strip narrower than it (forced by the part), or our sizing (chosen)
SHORT_EDGE_RECORD = True
#: (a pair of CAD edges further apart than this many shortest edges is not measured exactly)
SHORT_EDGE_PAIR_REACH = 2.0
#: SHMESH-51o64b: the record by engines/gmsh/shell_edge_causes.py (any edge of an element under the shortest edge
#: decides it; rings and mapped faces apart; the longest edge too); off: SHMESH-51o64's record as before
EDGE_CAUSES = True


def _record_short_edge(brep: Path | str, mm_per_unit: float, mesh: ShellMesh, applied: dict) -> None:
    """SHMESH-51o64: quality.short_edge on the mesh delivered, when the user typed a shortest edge and some element
    has an edge under it. Never raises: a failure leaves the record without it."""
    sz = applied.get("sizing") or {}
    low = sz.get("low_mm")
    if EDGE_CAUSES and SHORT_EDGE_RECORD:
        # SHMESH-51o64b: every edge of an element read, structure apart, the longest edge too (shell_edge_causes)
        from meshpipeline.engines.gmsh import shell_edge_causes

        shell_edge_causes.record(brep, float(mm_per_unit), mesh, applied)
        return
    if not SHORT_EDGE_RECORD or low is None or not mesh.n_elements:
        return
    try:
        rec = _short_edge_causes(Path(brep), float(mm_per_unit), mesh, float(low))
    except Exception as exc:  # noqa: BLE001 - a record figure must never cost the mesh
        logger.info("shell mesh: the short-edge causes were not measured (%s)", exc)
        return
    if rec is not None:
        applied["quality"]["short_edge"] = rec


def _short_edge_causes(brep: Path, mm: float, mesh: ShellMesh, low: float) -> dict | None:
    """SHMESH-51o64: per element with an edge under `low` (mm), the first cause that holds - small_hole: its shortest
    edge on, or it touching, an arc whose turn cannot take HOLE_PER_TURN segments of `low`; short_cad_edge: the same
    with an open CAD edge shorter than `low`; narrow: its distances to two CAD edges of one face that do not meet
    add up to less than `low`; chosen: none (beside_forced: within 2 shortest edges of a CAD edge that forces one;
    beside_narrow: the gap there under twice `low`). None when no element is under `low`."""
    import gmsh
    from scipy.spatial import cKDTree

    els = [np.asarray(c) for c in mesh.quads] + [np.asarray(c) for c in mesh.trias]
    short_e: list[tuple[int, float, int]] = []
    for i, c in enumerate(els):
        pts = mesh.xyz[c]
        ed = np.linalg.norm(pts - np.roll(pts, -1, axis=0), axis=1)
        m = int(ed.argmin())
        if ed[m] < low:
            short_e.append((i, float(ed[m]), m))
    band = low * (1.0 - EDGE_TOL)
    if not short_e:
        return None
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("short_edge")
        gmsh.model.occ.importShapes(str(brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        curves: dict[int, dict] = {}
        for _d, c in gmsh.model.getEntities(1):
            length = float(gmsh.model.occ.getMass(1, c)) * mm
            ends = {abs(int(q)) for _dd, q in gmsh.model.getBoundary([(1, c)], oriented=False)}
            lo, hi = gmsh.model.getParametrizationBounds(1, c)
            n = min(4000, max(8, int(length / max(low / 10, 1e-9)) + 1))
            ts = np.linspace(lo[0], hi[0], n)
            pts = np.array(gmsh.model.getValue(1, c, ts.tolist())).reshape(-1, 3) * mm
            closed = len(ends) <= 1
            radius = None
            if gmsh.model.getType(1, c) in ("Circle", "Ellipse") or closed:
                k_ = gmsh.model.getCurvature(1, c, [0.5 * (lo[0] + hi[0])])
                radius = 1.0 / (k_[0] / mm) if k_ and k_[0] > 1e-12 else None
            curves[int(c)] = {"len": length, "closed": closed, "pts": pts, "ends": ends,
                              "hole": radius is not None and 2 * math.pi * radius / SR.HOLE_PER_TURN < low,
                              "diam": 2 * radius if radius is not None else None}
        trees = {k: cKDTree(v["pts"]) for k, v in curves.items()}
        pairs: set[tuple[int, int]] = set()
        narrow_faces: set[int] = set()
        narrowest = math.inf
        for _d, t in gmsh.model.getEntities(2):
            cs = sorted({abs(int(c)) for _dd, c in gmsh.model.getBoundary([(2, t)], oriented=False, combined=False)})
            for i, a in enumerate(cs):
                for b in cs[i + 1:]:
                    if a not in curves or b not in curves or curves[a]["ends"] & curves[b]["ends"]:
                        continue
                    if trees[a].query(curves[b]["pts"])[0].min() > SHORT_EDGE_PAIR_REACH * low:
                        continue
                    d = float(gmsh.model.occ.getDistance(1, a, 1, b)[0]) * mm
                    if d < low:
                        pairs.add((a, b))
                        narrow_faces.add(int(t))
                        narrowest = min(narrowest, d)
    finally:
        gmsh.finalize()
    allp = np.vstack([v["pts"] for v in curves.values()])
    owner = np.concatenate([[k] * len(v["pts"]) for k, v in curves.items()])
    tree = cKDTree(allp)
    tol = max(0.06 * low, 1e-6)
    on_cache: dict[int, set[int]] = {}

    def on(v: int) -> set[int]:
        if v not in on_cache:
            on_cache[v] = {int(owner[j]) for j in tree.query_ball_point(mesh.xyz[v], r=tol)}
        return on_cache[v]

    forcing = {k for k, v in curves.items() if v["hole"] or v["len"] < low} | {c for pr in pairs for c in pr}
    ftree = cKDTree(np.vstack([curves[k]["pts"] for k in forcing])) if forcing else None
    causes = ("small_hole", "short_cad_edge", "narrow", "chosen")
    out: dict[str, dict] = {k: {"under": 0, "outside_band": 0} for k in causes}
    out["chosen"].update(beside_forced=0, beside_narrow=0)
    holes_hit: set[int] = set()
    edges_hit: set[int] = set()
    for i, e_len, m in short_e:
        c = els[i]
        a, b = int(c[m]), int(c[(m + 1) % len(c)])
        common = on(a) & on(b)
        touched = set().union(*[on(int(v)) for v in c])
        cause = "chosen"
        if any(curves[k]["hole"] for k in touched):
            cause = "small_hole"
            holes_hit |= {k for k in touched if curves[k]["hole"]}
        elif any(curves[k]["len"] < low and not curves[k]["closed"] for k in common | touched):
            cause = "short_cad_edge"
            edges_hit |= {k for k in common | touched if curves[k]["len"] < low}
        else:
            cen = mesh.xyz[c].mean(axis=0)
            close = sorted({int(owner[j]) for j in tree.query_ball_point(cen, r=low)})
            gaps = [trees[c1].query(cen)[0] + trees[c2].query(cen)[0]
                    for j, c1 in enumerate(close) for c2 in close[j + 1:] if (c1, c2) in pairs]
            if gaps and min(gaps) < low:
                cause = "narrow"
            elif gaps:
                out["chosen"]["beside_narrow"] += 1
            if cause == "chosen" and ftree is not None and ftree.query(cen)[0] < 2 * low:
                out["chosen"]["beside_forced"] += 1
        out[cause]["under"] += 1
        out[cause]["outside_band"] += int(e_len < band)
    out["small_hole"]["holes"] = len(holes_hit)
    diams = [curves[k]["diam"] for k in holes_hit if curves[k]["diam"]]
    if diams:
        out["small_hole"]["smallest_mm"] = float(f"{min(diams):.4g}")
    out["short_cad_edge"]["edges"] = len(edges_hit)
    if edges_hit:
        out["short_cad_edge"]["shortest_mm"] = float(f"{min(curves[k]['len'] for k in edges_hit):.4g}")
    out["narrow"]["faces"] = len(narrow_faces)
    if narrowest < math.inf:
        out["narrow"]["narrowest_mm"] = float(f"{narrowest:.4g}")
    under = len(short_e)
    forced = under - out["chosen"]["under"]
    return {"rule": "SHMESH-51o64", "shortest_mm": float(f"{low:.6g}"), "band_mm": float(f"{band:.6g}"),
            "elements": int(mesh.n_elements), "under": under,
            "outside_band": int(sum(v["outside_band"] for v in out.values())),
            "by_cause": out, "forced_share": float(f"{forced / under:.4g}")}


def _chord(mesh: ShellMesh, facts: list[dict], face_of: np.ndarray, mm: float) -> dict | None:
    """SHMESH-34: on the curved faces (those with a radius), the largest distance (mm) of an element's centre from the
    face's own surface, over at most CHORD_SAMPLES elements a face (every n-th, in order); None with no curved face.
    The nearest point is read within the surface's own bounds (ShapeAnalysis_Surface, as the mid-surface reads it):
    an orthogonal projection alone found far points beside a patch's rim (6 mm off on a 20 mm radius at 6 mm)."""
    from OCP.gp import gp_Pnt

    from meshpipeline.cad import midsurface as ms

    worst, faces = 0.0, 0
    order = np.argsort(face_of, kind="stable")
    bounds = np.searchsorted(face_of[order], np.arange(len(facts) + 1))
    for k, f in enumerate(facts):
        if f["bend"] is None:
            continue
        els = order[bounds[k]:bounds[k + 1]]
        if not len(els):
            continue
        faces += 1
        _surf, sa = ms._analyser(f["normal_face"])
        for e in els[::max(1, len(els) // CHORD_SAMPLES)].tolist():
            c = mesh.xyz[mesh.element_nodes(int(e))].mean(axis=0) / mm
            sa.ValueOfUV(gp_Pnt(float(c[0]), float(c[1]), float(c[2])), 1e-7)
            worst = max(worst, float(sa.Gap()) * mm)
    return {"max_mm": float(f"{worst:.4g}"), "faces": faces} if faces else None


def _interior_faces(brep: Path, bent: list[bool] | None = None) -> set[int]:
    """SHMESH-35: the sewn faces (0-based) held along their sides by other faces: the length of their edges another
    face shares at least FLANK_SHARED times the length of their free edges (a bead's wall running out to the sheet's
    rim has only its two short ends free; a strip along the rim has a whole long side free). With `bent` (per face:
    is it a tight bend, a fillet), also met by at least FLANK_BENDS such faces: a bead's or a dart's wall runs between
    its fillets; a step or a joint strip in an extracted mid-surface meets flat faces at a sharp edge (on the 80 lab
    parts the walls without this test cost 154,000 elements and 20,000 triangles for no line of the list)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    fmap = ms._map(shape, TopAbs_FACE)
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    free = np.zeros(fmap.Extent())
    shared = np.zeros(fmap.Extent())
    bends: list[set[int]] = [set() for _ in range(fmap.Extent())]
    for i in range(1, emap.Extent() + 1):
        ks = {fmap.FindIndex(x) - 1 for x in emap.FindFromIndex(i)} - {-1}
        length = ms._length(emap.FindKey(i))
        for k in ks:
            if len(ks) == 1:
                free[k] += length
            else:
                shared[k] += length
                if bent is not None:
                    bends[k] |= {j for j in ks if j != k and bent[j]}
    return {k for k in range(fmap.Extent()) if shared[k] > 0 and shared[k] >= FLANK_SHARED * free[k]
            and (bent is None or len(bends[k]) >= FLANK_BENDS)}


def _spare_strip_corners(gmsh, tag_of: dict[int, int], rows: list[dict], mapped: dict, narrow: dict,
                         short_edges: dict) -> None:
    """SHMESH-27: a mapped grid cannot grade, so a smaller piece of the CAD at a mapped strip's corner is left at the
    size it had: a short edge ending there is not graded to, a narrow face (not itself mapped) with a corner there
    takes its size back. Graded, a tube corner's 10 mm mapped elements fanned out of a 0.5 mm face at its end
    (sh0110: 30 triangles under 20 degrees became 168); left free, the strips lost their grids (899)."""
    def points(c: int) -> set[int]:
        return {abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)}

    def curves(t: int) -> list[int]:
        return [abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)]

    corners: set[int] = set()
    for t in mapped:
        for c in curves(t):
            corners |= points(c)
    for h in list(short_edges["by_size"]):
        keep = [c for c in short_edges["by_size"][h] if not (points(c) & corners)]
        for c in short_edges["by_size"][h]:
            if c not in keep:
                short_edges["lengths"].pop(c, None)
        short_edges["edges"] = len(short_edges["lengths"])
        if keep:
            short_edges["by_size"][h] = keep
        else:
            del short_edges["by_size"][h]
    face_tag = {k: t for t, k in tag_of.items()}
    for k in sorted(narrow["was"]):
        t = face_tag.get(k)
        if t is None or t in mapped:
            continue
        if any(points(c) & corners for c in curves(t)):
            rows[k].clear()
            rows[k].update(narrow["was"].pop(k))
            narrow["widths"].pop(k)
            narrow["flank_faces"].discard(k)


def _narrow_choose(gmsh, thin: dict, low_mm: float) -> list[int]:
    """SHMESH-60: which of the strips narrower than the user's shortest edge (`thin`: {strip: (long sides, ends, width
    mm)}) are closed to their middle lines. Narrowest first, a strip is closed while the row it stands for (its own
    width, plus half of every closed strip it shares a long side with, as those halves came to it) is under
    `low_mm`; closing it hands half of that row to each strip beside it. Two touching 0.8 mm fillets under a 2 mm
    edge both close (one line); two of 1.5 mm close one (the other keeps a 2.25 mm row). Deterministic."""
    beside: dict[int, set[int]] = {t: set() for t in thin}
    by_curve: dict[int, list[int]] = {}
    for t, v_ in thin.items():
        for c in v_[0]:
            by_curve.setdefault(c, []).append(t)
    for ts in by_curve.values():
        for t in ts:
            beside[t] |= {s for s in ts if s != t}
    eff = {t: float(v_[2]) for t, v_ in thin.items()}
    closed: list[int] = []
    open_ = set(thin)
    while True:
        cand = [t for t in open_ if eff[t] < low_mm]
        if not cand:
            return sorted(closed)
        t = min(cand, key=lambda s: (eff[s], s))
        open_.discard(t)
        closed.append(t)
        for s in beside[t] & open_:
            eff[s] += 0.5 * eff[t]


def _structured_faces(gmsh, tag_of: dict[int, int], rings: list, grid_cells: set[int], cut_curves: set[int],
                       made: set[int]) -> set[int]:
    """SHMESH-60: the faces the structured steps drew or own (washer annuli, grid and polar cells, any face with a
    drawn line for an edge, the faces cut into grids or rings): a narrow one is theirs, never closed."""
    out = {int(w.annulus) for w in rings if getattr(w, "annulus", None)} | {int(t) for t in grid_cells}
    for t, k in tag_of.items():
        if k in made or any(abs(int(c)) in cut_curves for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)):
            out.add(int(t))
    return out


def _taper_sides(gmsh, t: int) -> tuple[int, list[int], list[int], list[int]] | None:
    """SHMESH-69: a face of one loop with exactly three corners (the loop turns by at least MAP_CORNER[0] there; at
    every other vertex it runs on within ST.SMOOTH_DEG): (its tip - the corner between its two longest sides -, the
    curves of one long side from the tip, of the other, of the short side). None otherwise."""
    _l, loops = ST._curve_loops(gmsh, t)
    if len(loops) != 1:
        return None
    cl = [abs(int(c)) for c in loops[0]]
    m = len(cl)
    if m < 3 or len(set(cl)) != m:
        return None
    pts = {c: [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=True)] for c in cl}
    if any(len(v) != 2 or v[0] == v[1] for v in pts.values()):
        return None
    corner_at: list[int | None] = []
    for j in range(m):
        a, b = cl[j], cl[(j + 1) % m]
        sh = set(pts[a]) & set(pts[b])
        if len(sh) != 1:
            return None
        p = sh.pop()
        xp = np.asarray(gmsh.model.getValue(0, p, []), dtype=float)
        ea, eb = ST._ends(gmsh, a), ST._ends(gmsh, b)
        da = ea[2] if np.linalg.norm(ea[0] - xp) <= np.linalg.norm(ea[1] - xp) else ea[3]
        db = eb[2] if np.linalg.norm(eb[0] - xp) <= np.linalg.norm(eb[1] - xp) else eb[3]
        cos = float(da @ db) / max(float(np.linalg.norm(da) * np.linalg.norm(db)), 1e-300)
        turn = 180.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        if turn <= ST.SMOOTH_DEG:
            corner_at.append(None)
        elif turn >= ST.MAP_CORNER[0]:
            corner_at.append(p)
        else:
            return None
    at = [j for j in range(m) if corner_at[j] is not None]
    if len(at) != 3:
        return None
    sides = []
    for i in range(3):
        start, stop = at[i] + 1, at[(i + 1) % 3] + 1
        sides.append(([cl[(start + q) % m] for q in range((stop - start) % m or m)], corner_at[at[i]],
                      corner_at[at[(i + 1) % 3]]))
    length = [sum(float(gmsh.model.occ.getMass(1, c)) for c in sd[0]) for sd in sides]
    e = int(np.argmin(length))
    end, (l1, l2) = sides[e], (sides[(e + 1) % 3], sides[(e + 2) % 3])
    # the tip: the corner the two long sides share (l1 starts at end's last corner, l2 ends at end's first)
    tip = l1[2]
    if tip is None or tip != l2[1]:
        return None
    return int(tip), list(reversed(l1[0])), list(l2[0]), list(end[0])


def _tri_blocks(gmsh, t: int, plan: tuple, index: np.ndarray, xyz: np.ndarray, fact: dict, mm: float, limits,
                high_mm: float | None) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-79: a three-cornered face's quads - a centre point and three blocks, each filled by transfinite
    interpolation from its four sides, each new node laid on the face's surface. `plan`: (three chains of curves, their
    counts a, b, c, the three corners C0, C1, C2; chain q runs from Cq to Cq+1). Side q is split at w, u, v segments
    from its first corner (w = (a+b-c)/2, u = (b+c-a)/2, v = (a+c-b)/2); the spokes from those points to the centre
    have u, v, w segments. ValueError when it cannot be made or a quad would be past the solver's limits or the
    user's outer limits, or over their longest edge."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt

    chains, counts, corners = plan
    corner_node = [int(index[gmsh.model.mesh.getNodes(0, int(cv))[0][0]]) for cv in corners]
    sides = []
    for q, ch in enumerate(chains):
        seq = [corner_node[q]]
        for c in ch:
            nodes = _side_nodes(gmsh, c, index)
            if nodes[0] != seq[-1]:
                nodes = nodes[::-1]
            if nodes[0] != seq[-1]:
                raise ValueError("a side's curves do not chain")
            seq += nodes[1:]
        if seq[-1] != corner_node[(q + 1) % 3]:
            raise ValueError("a side does not end at the next corner")
        sides.append(seq)
    a_, b_, c_ = [len(sd) - 1 for sd in sides]
    if [a_, b_, c_] != list(counts) or (a_ + b_ + c_) % 2:
        raise ValueError("its sides' counts are not as planned")
    w, u, v = (a_ + b_ - c_) // 2, (b_ + c_ - a_) // 2, (a_ + c_ - b_) // 2
    if min(w, u, v) < 1 or 3 * min(w, u, v) < max(w, u, v):
        raise ValueError("its counts do not split into blocks of like size")
    surf = BRep_Tool.Surface_s(fact["normal_face"])

    def on_face(p_: np.ndarray) -> np.ndarray:
        q_ = p_ / mm
        pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(q_[0]), float(q_[1]), float(q_[2])), surf)
        if pr.NbPoints() > 0:
            r_ = pr.NearestPoint()
            return np.array([r_.X(), r_.Y(), r_.Z()]) * mm
        return p_

    base = len(xyz)
    new: list[np.ndarray] = []

    def add(p_: np.ndarray) -> int:
        new.append(on_face(p_))
        return base + len(new) - 1

    def P(i: int) -> np.ndarray:
        return xyz[i] if i < base else new[i - base]

    s0, s1, s2 = sides
    m0, m1, m2 = s0[w], s1[u], s2[v]
    centre = add((P(m0) + P(m1) + P(m2)) / 3.0)

    def spoke(m: int, k: int) -> list[int]:
        return [m] + [add(P(m) + (P(centre) - P(m)) * (j / k)) for j in range(1, k)] + [centre]

    sp0, sp1, sp2 = spoke(m0, u), spoke(m1, v), spoke(m2, w)
    quads: list[list[int]] = []

    def block(bottom: list[int], right: list[int], top: list[int], left: list[int]) -> None:
        # bottom and top run from the left side to the right; left and right from the bottom to the top
        nb, nl = len(bottom) - 1, len(left) - 1
        if (len(top) - 1 != nb or len(right) - 1 != nl or bottom[0] != left[0] or bottom[-1] != right[0]
                or top[0] != left[-1] or top[-1] != right[-1]):
            raise ValueError("a block's sides do not meet")
        Xb, Xt = np.array([P(i) for i in bottom]), np.array([P(i) for i in top])
        Xl, Xr = np.array([P(i) for i in left]), np.array([P(i) for i in right])
        grid = [list(bottom)]
        for j in range(1, nl):
            vv = j / nl
            row = [left[j]]
            for i in range(1, nb):
                uu = i / nb
                p_ = ((1 - vv) * Xb[i] + vv * Xt[i] + (1 - uu) * Xl[j] + uu * Xr[j]
                      - ((1 - uu) * (1 - vv) * Xb[0] + uu * (1 - vv) * Xb[-1] + (1 - uu) * vv * Xt[0]
                         + uu * vv * Xt[-1]))
                row.append(add(p_))
            row.append(right[j])
            grid.append(row)
        grid.append(list(top))
        for j in range(nl):
            for i in range(nb):
                quads.append([grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i]])

    block(s0[: w + 1], sp0, sp2, s2[v:][::-1])           # at C0: C0-m0 (w), m0-O (u), m2-O (w), C0-m2 (u)
    block(s1[: u + 1], sp1, sp0, s0[w:][::-1])           # at C1: C1-m1 (u), m1-O (v), m0-O (u), C1-m0 (v)
    block(s2[: v + 1], sp2, sp1, s1[u:][::-1])           # at C2: C2-m2 (v), m2-O (w), m1-O (v), C2-m1 (w)
    Qa = np.array(quads, dtype=np.int64)
    X = np.vstack([xyz, np.array(new)]) if new else xyz.copy()
    types, _tags, conns = gmsh.model.mesh.getElements(2, t)
    ref = None
    for et, conn in zip(types, conns, strict=True):
        e_ = index[np.asarray(conn, dtype=np.int64)][: (4 if et == 3 else 3)]
        if len(e_) >= 3:
            ref = np.cross(xyz[e_[1]] - xyz[e_[0]], xyz[e_[2]] - xyz[e_[0]])
            break
    if ref is None:
        raise ValueError("no element of gmsh's to wind by")
    # (one winding for all: the blocks are built alike; turned as gmsh's own elements nearest each quad - a fillet's
    # normal turns through its arc, so one reference normal for the whole face would not do)
    cen_g, nor_g = [], []
    for et, conn in zip(types, conns, strict=True):
        k_ = 4 if et == 3 else 3
        if et not in (2, 3):
            continue
        e_ = index[np.asarray(conn, dtype=np.int64)].reshape(-1, k_)
        cen_g.append(xyz[e_].mean(1))
        nor_g.append(np.cross(xyz[e_[:, 1]] - xyz[e_[:, 0]], xyz[e_[:, 2]] - xyz[e_[:, 0]]))
    cen_a, nor_a = np.vstack(cen_g), np.vstack(nor_g)
    cq = X[Qa].mean(1)
    near = np.argmin(((cq[:, None, :] - cen_a[None, :, :]) ** 2).sum(-1), axis=1)
    sgn = np.einsum("ij,ij->i", np.cross(X[Qa[:, 1]] - X[Qa[:, 0]], X[Qa[:, 3]] - X[Qa[:, 0]]), nor_a[near])
    if (sgn < 0).sum() > (sgn > 0).sum():
        Qa = Qa[:, ::-1]
    meas = shell_quality.quad_measures(X, Qa, extra=True)
    if (meas["min_jacobian"] <= 0).any() or shell_quality._quad_past(meas, limits).any():
        raise ValueError("a quad past the solver's own limits")
    if (shell_quality.aim_score(meas, "quad", limits, len(Qa)) >= shell_quality.AIM_OUTER).any():
        raise ValueError("a quad past an outer limit of the list")
    if high_mm is not None and float(meas["max_edge"].max()) > float(high_mm):
        raise ValueError("an edge over the longest edge asked")
    return X[base:], Qa


def _step_tokens(fb: np.ndarray, ft: np.ndarray, n: int, f: int, mode: str) -> list[str] | None:
    """SHMESH-90: how a stepped strip's transition row meets its fine side, one token per coarse segment - "P" one
    fine segment, "T3" three (the three-to-one quad step, four quads), "T2a"/"T2b" a pair of coarse segments meeting
    four (the two-to-one quad step, six quads). `fb`, `ft`: the arc-length fractions of the coarse and fine sides'
    nodes. mode "spread": SHMESH-73's own - k three-to-one steps spread evenly by count; "t3": three-to-one steps
    only, "gentle": as few three-to-one steps as the counts allow (two-to-one ones first), "even": steps of either
    kind; the last three placed where the coarse and fine nodes' fractions stay closest (the largest mismatch at a
    node smallest, then the fewest three-to-one steps, then the sum of squares). None when the counts cannot step."""
    k = (f - n) // 2
    if f <= n or (f - n) % 2 or k > n:
        return None
    if mode == "spread":
        steps = sorted({min(n - 1, int((q + 0.5) * n / k)) for q in range(k)})
        if len(steps) != k:
            return None
        return ["T3" if i in steps else "P" for i in range(n)]
    # (i coarse nodes walked, s steps made) -> (cost, previous state, tokens of the last move)
    best: dict[tuple[int, int], tuple] = {(0, 0): ((0, 0.0, 0, 0.0), None, (), 0.0, 0, 0.0)}
    moves: list[tuple[int, int, tuple[str, ...]]] = [(1, 0, ("P",)), (1, 1, ("T3",))]
    if mode != "t3":
        moves.append((2, 1, ("T2a", "T2b")))
    for i in range(n):
        for s in range(k + 1):
            got = best.get((i, s))
            if got is None:
                continue
            _c0, _p, _t, mx, n3, sq = got
            for di, ds, toks in moves:
                i2, s2 = i + di, s + ds
                if i2 > n or s2 > k:
                    continue
                j2 = i2 + 2 * s2
                if j2 > f or (f - j2) < (n - i2):
                    continue
                e = abs(float(fb[i2]) - float(ft[j2]))
                if di == 2:
                    e = max(e, abs(float(fb[i + 1]) - float(ft[i + 2 * s + 2])))
                m3 = n3 + (1 if toks == ("T3",) else 0)
                mx2 = max(mx, e)
                sq2 = sq + e * e
                cost = (m3, round(mx2, 9), 0, sq2) if mode == "gentle" else (0, round(mx2, 9), m3, sq2)
                old = best.get((i2, s2))
                if old is None or cost < old[0]:
                    best[(i2, s2)] = (cost, (i, s), toks, mx2, m3, sq2)
    end = best.get((n, k))
    if end is None:
        return None
    out: list[str] = []
    st: tuple[int, int] | None = (n, k)
    while st is not None and st != (0, 0):
        _c, prev, toks = best[st][:3]
        out = list(toks) + out
        st = prev
    return out


def _step_build(bottom: list[int], top: list[int], left: list[int], right: list[int], xyz: np.ndarray,
                tokens: list[str], on_face, smooth: int = 0, tall: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-73/90: a stepped strip's quads - rows of the coarse count from the coarse side (`bottom`), their nodes
    walking from the coarse side's fractions to the fine side's ones, and a transition row meeting the fine side
    (`top`) as `tokens` say (_step_tokens). `left` and `right` run from the coarse side's ends to the fine side's;
    `on_face` lays a point on the face. `smooth`: rounds of laying each new node at the mean of its neighbours (back
    on the face each round). `tall` (two rows or more): the share of the strip's width the transition row takes away
    from its ends (its rows' lines bowed to it from one coarse segment in; 0: every row alike) - its quads hold nodes
    half-way across it. Returns (the new nodes; the quads, node indices - new nodes numbered from len(xyz))."""
    n, f, m = len(bottom) - 1, len(top) - 1, len(left) - 1
    Xb, Xt, Xl, Xr = xyz[bottom], xyz[top], xyz[left], xyz[right]
    ub, ut, vl, vr = ST._along(Xb), ST._along(Xt), ST._along(Xl), ST._along(Xr)
    meet = [0]
    for tok in tokens:
        meet.append(meet[-1] + {"P": 1, "T3": 3, "T2a": 2, "T2b": 2}[tok])
    if len(tokens) != n or meet[-1] != f:
        raise ValueError("its steps do not close")
    new: list[np.ndarray] = []
    base = len(xyz)

    def add(p_: np.ndarray) -> int:
        new.append(on_face(p_))
        return base + len(new) - 1

    def at(i_: int) -> np.ndarray:
        return xyz[i_] if i_ < base else new[i_ - base]

    lines: list[list[int]] = [list(bottom)]
    for j in range(1, m):
        row = [int(left[j])]
        s_j = j / max(m - 1, 1) if m > 1 else 1.0
        for i in range(1, n):
            u = (1 - s_j) * float(ub[i]) + s_j * float(ut[meet[i]])
            v = (1 - u) * float(vl[j]) + u * float(vr[j])
            if tall > 0 and m > 1:
                bow = min(1.0, float(min(i, n - i)))
                v += bow * ((1.0 - tall) * j / (m - 1) - v)
                p_ = ((1 - v) * ST._at(Xb, ub, u) + v * ST._at(Xt, ut, u) + (1 - u) * ST._at(Xl, vl, v)
                      + u * ST._at(Xr, vr, v)
                      - ((1 - u) * (1 - v) * Xb[0] + u * (1 - v) * Xb[-1] + (1 - u) * v * Xt[0] + u * v * Xt[-1]))
            else:
                p_ = ((1 - v) * ST._at(Xb, ub, u) + v * ST._at(Xt, ut, u) + (1 - u) * Xl[j] + u * Xr[j]
                      - ((1 - u) * (1 - v) * Xb[0] + u * (1 - v) * Xb[-1] + (1 - u) * v * Xt[0] + u * v * Xt[-1]))
            row.append(add(p_))
        row.append(int(right[j]))
        lines.append(row)
    quads: list[list[int]] = []
    for j in range(m - 1):
        for i in range(n):
            quads.append([lines[j][i], lines[j][i + 1], lines[j + 1][i + 1], lines[j + 1][i]])
    A = lines[m - 1]
    F = [int(x) for x in top]
    a_ = 0
    i = 0
    while i < n:
        tok = tokens[i]
        if tok == "P":
            quads.append([A[i], A[i + 1], F[a_ + 1], F[a_]])
            a_ += 1
            i += 1
        elif tok == "T3":
            q0, q1 = at(A[i]), at(A[i + 1])
            c1 = add(0.5 * ((2 * q0 + q1) / 3) + 0.5 * xyz[F[a_ + 1]])
            c2 = add(0.5 * ((q0 + 2 * q1) / 3) + 0.5 * xyz[F[a_ + 2]])
            quads += [[A[i], c1, F[a_ + 1], F[a_]], [c1, c2, F[a_ + 2], F[a_ + 1]],
                      [c2, A[i + 1], F[a_ + 3], F[a_ + 2]], [A[i], A[i + 1], c2, c1]]
            a_ += 3
            i += 1
        elif tok == "T2a" and i + 1 < n and tokens[i + 1] == "T2b":
            q0, q1, q2 = at(A[i]), at(A[i + 1]), at(A[i + 2])
            p1 = add(0.5 * (0.5 * (q0 + q1)) + 0.5 * xyz[F[a_ + 1]])
            p2 = add(0.5 * q1 + 0.5 * xyz[F[a_ + 2]])
            p3 = add(0.5 * (0.5 * (q1 + q2)) + 0.5 * xyz[F[a_ + 3]])
            quads += [[A[i], p1, F[a_ + 1], F[a_]], [p1, p2, F[a_ + 2], F[a_ + 1]], [p2, p3, F[a_ + 3], F[a_ + 2]],
                      [p3, A[i + 2], F[a_ + 4], F[a_ + 3]], [A[i], A[i + 1], p2, p1], [A[i + 1], A[i + 2], p3, p2]]
            a_ += 4
            i += 2
        else:
            raise ValueError("its steps do not pair")
    if a_ != f:
        raise ValueError("its last row did not close")
    Qa = np.array(quads, dtype=np.int64)
    X = np.vstack([xyz, np.array(new)]) if new else xyz.copy()
    if smooth and new:
        X = _step_relax(X, Qa, list(range(base, len(X))), on_face, smooth)
    return X[base:], Qa


def _step_relax(X: np.ndarray, Qa: np.ndarray, free: list[int], on_face, rounds: int) -> np.ndarray:
    """SHMESH-90: the step's own nodes moved, one at a time, to where the quads round each stand furthest inside the
    solver's own limits (the worst figure as a share of its limit smallest), no quad turned over - trying a move each
    way along the face, the move a quarter of its edges' mean at first and halved every other round; each move laid
    back on the face. (Weighing the user's quad targets too was tried: no triangle fewer on the 24 lab parts, 16
    short-edge elements more on one.)"""
    X = X.copy()
    round_ = {v_: np.flatnonzero((Qa == v_).any(axis=1)) for v_ in free}
    rows = [(k_, op_, lim_) for sh_, k_, op_, lim_, _c in shell_quality.NASTRAN_LIMITS if sh_ == "quad"]

    def cost(qs: np.ndarray) -> float:
        p = X[Qa[qs]]
        nrm = np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1])
        cr = np.cross(p[:, [1, 2, 3, 0]] - p, p[:, [3, 0, 1, 2]] - p)
        if (np.einsum("mki,mi->mk", cr, nrm) <= 0).any():
            return 1e9
        # (the worst of the solver's own figures as a share of its limit: corner angles, skew, taper, warp)
        meas = shell_quality.quad_measures(X, Qa[qs])
        worst = 0.0
        for key, op, limit in rows:
            if key in meas:
                v = meas[key]
                worst = max(worst, float(limit / max(float(v.min()), 1e-300)) if op == ">="
                            else float(v.max()) / max(limit, 1e-300))
        return worst

    steps = {}
    for v_ in free:
        qs = round_[v_]
        nb = {int(w_) for q in Qa[qs] for w_ in q} - {v_}
        steps[v_] = 0.25 * float(np.mean([np.linalg.norm(X[w_] - X[v_]) for w_ in nb])) if nb else 0.0
    for r_ in range(rounds):
        scale = 0.5 ** (r_ // 2)
        for v_ in free:
            qs = round_[v_]
            if not len(qs) or steps[v_] <= 0:
                continue
            p = X[Qa[qs]]
            nrm = np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1]).sum(axis=0)
            nrm /= max(float(np.linalg.norm(nrm)), 1e-300)
            e_ = X[[int(w_) for w_ in Qa[qs[0]] if w_ != v_][0]] - X[v_]
            t1 = e_ - (e_ @ nrm) * nrm
            t1 /= max(float(np.linalg.norm(t1)), 1e-300)
            t2 = np.cross(nrm, t1)
            here = X[v_].copy()
            best_c, best_p = cost(qs), here
            for d_ in (t1, -t1, t2, -t2):
                X[v_] = on_face(here + scale * steps[v_] * d_)
                c_ = cost(qs)
                if c_ < best_c - 1e-9:
                    best_c, best_p = c_, X[v_].copy()
            X[v_] = best_p
    return X


def _step_grade(X: np.ndarray, Qa: np.ndarray, limits, high_mm: float | None, low_mm: float | None,
                own: set | None = None) -> tuple | str:
    """SHMESH-90: a stepped strip's quads judged - the reason when one is past the solver's own limits, past an outer
    limit of the user's list or over their longest edge (with `own`, the strip's side segments as node pairs: an edge
    of its own making - its sides' segments are as the curves were counted, the paving's too), or, with STEP_OWN_LOW,
    when an edge of its own making is under the user's shortest edge (a transition in a strip under twice it across
    makes such edges); else (quads between a target and its outer limit, quads with an edge under the user's
    shortest edge, the worst of the solver's own figures as a share of its limit)."""
    long_own = 0.0
    if own is not None:
        e0 = np.concatenate([Qa[:, [0, 1]], Qa[:, [1, 2]], Qa[:, [2, 3]], Qa[:, [3, 0]]])
        ln_ = np.linalg.norm(X[e0[:, 0]] - X[e0[:, 1]], axis=1)
        mine = np.array([(min(u_, w_), max(u_, w_)) not in own for u_, w_ in e0.tolist()], dtype=bool)
        long_own = float(ln_[mine].max()) if mine.any() else 0.0
        if STEP_OWN_LOW and low_mm is not None and mine.any() and float(ln_[mine].min()) < float(low_mm) * (1 - 1e-9):
            return "an edge of its own under the shortest edge asked"
    meas = shell_quality.quad_measures(X, Qa, extra=True)
    if (meas["min_jacobian"] <= 0).any() or shell_quality._quad_past(meas, limits).any():
        return "a quad past the solver's own limits"
    aim = shell_quality.aim_score(meas, "quad", limits, len(Qa))
    if (aim >= shell_quality.AIM_OUTER).any():
        return "a quad past an outer limit of the list"
    if high_mm is not None and (long_own if own is not None else float(meas["max_edge"].max())) > float(high_mm):
        return "an edge over the longest edge asked"
    worst = 0.0
    for shape, key, op, limit, _card in getattr(limits, "hard", ()):
        if shape == "quad" and key in meas and len(meas[key]):
            v = meas[key]
            r_ = float(limit / max(float(v.min()), 1e-300)) if op == ">=" else float(v.max()) / max(limit, 1e-300)
            worst = max(worst, r_)
    short = int((meas["min_edge"] < float(low_mm) * (1 - 1e-9)).sum()) if low_mm is not None else 0
    return int((aim >= shell_quality.AIM_BAND).sum()), short, round(worst, 3)


#: SHMESH-90: the step modes tried (_step_tokens) and the rounds of smoothing tried for each (0: as placed)
STEP_MODES = ("gentle", "even", "t3", "spread")
STEP_SMOOTH = (0, 8)


def _step_best(bottom, top, left, right, xyz, on_face, limits, high_mm, low_mm, modes=None, smooth=None
               ) -> tuple[np.ndarray, np.ndarray, dict]:
    """SHMESH-90: every step mode and smoothing tried on a stepped strip; the best judged (_step_grade: fewest quads
    in a user's band, then fewest short-edge quads, then the solver's figures furthest inside) kept. Returns (the new
    nodes, the quads, what was kept); ValueError (the reason of the first refused) when none can be made."""
    n, f, m = len(bottom) - 1, len(top) - 1, len(left) - 1
    fb, ft = ST._along(xyz[bottom]), ST._along(xyz[top])
    own: set | None = None
    talls = [0.0]
    if STEP_PATTERNS:
        own = {(min(int(p_[q]), int(p_[q + 1])), max(int(p_[q]), int(p_[q + 1])))
               for p_ in (bottom, top, left, right) for q in range(len(p_) - 1)}
        if m > 1:
            # (the transition row as two rows' share of the width: its nodes half-way across it as far from each side
            # as the other rows' lines are apart)
            talls.append(2.0 / (m + 1))
    best: tuple | None = None
    why: str | None = None
    seen: set[tuple] = set()
    for mode in (modes if modes is not None else (STEP_MODES if STEP_PATTERNS else ("spread",))):
        tokens = _step_tokens(fb, ft, n, f, mode)
        if tokens is None:
            why = why or "its counts do not step"
            continue
        for sm in (smooth if smooth is not None else (STEP_SMOOTH if STEP_PATTERNS else (0,))):
            if (tuple(tokens), sm) in seen:
                continue
            seen.add((tuple(tokens), sm))
            for tall in talls:
                try:
                    P_, Qa = _step_build(bottom, top, left, right, xyz, tokens, on_face, sm, tall)
                except ValueError as exc:
                    why = why or str(exc)
                    continue
                X = np.vstack([xyz, P_]) if len(P_) else xyz
                g = _step_grade(X, Qa, limits, high_mm, low_mm, own)
                if isinstance(g, str):
                    why = why or g
                    continue
                if best is None or g < best[0]:
                    best = (g, P_, Qa, {"mode": mode, "smooth": sm, "tall": round(tall, 3),
                                        "t2": tokens.count("T2a"), "t3": tokens.count("T3"), "grade": list(g)})
    if best is None:
        raise ValueError(why or "its counts do not step")
    return best[1], best[2], best[3]


def _strip_steps(gmsh, t: int, sides: tuple, index: np.ndarray, xyz: np.ndarray, fact: dict, mm: float, limits,
                 high_mm: float | None, low_mm: float | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """SHMESH-73: a stepped strip's own quads - rows of the coarse side's count from the coarse side, the last row
    meeting the fine side in three-to-one steps spread along it (SHMESH-51o17's cell, on a curved face: each new node
    laid on the face's surface). `sides`: (coarse long side, fine long side, (its two ends)). Returns (the new nodes,
    mm; the quads, node indices), wound as gmsh's own elements there; ValueError when it cannot be made or a quad
    would come out past the solver's limits or the user's outer limits, or over their longest edge. SHMESH-90: the
    transition row's steps two-to-one or three-to-one, placed where the sides' nodes stay in step, smoothed or
    not - the best judged kept (_step_best); also returns what was kept."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt

    coarse, fine, (e1, e2) = sides
    bottom = _side_nodes(gmsh, coarse, index)
    top = _side_nodes(gmsh, fine, index)
    left = _side_nodes(gmsh, e1, index)
    right = _side_nodes(gmsh, e2, index)
    # orient: left runs from bottom[0] to top's end at it, right from bottom[-1]
    if bottom[0] not in (left[0], left[-1]):
        left, right = right, left
    if bottom[0] not in (left[0], left[-1]) or bottom[-1] not in (right[0], right[-1]):
        raise ValueError("its sides do not meet as a four-sided strip")
    if left[0] != bottom[0]:
        left = left[::-1]
    if right[0] != bottom[-1]:
        right = right[::-1]
    if top[0] != left[-1]:
        top = top[::-1]
    if top[0] != left[-1] or top[-1] != right[-1]:
        raise ValueError("its long sides do not face each other")
    n, f, m = len(bottom) - 1, len(top) - 1, len(left) - 1
    if len(right) - 1 != m or f <= n or (f - n) % 2:
        raise ValueError(f"its counts ({n}, {f}, rows {m}) do not step")
    k = (f - n) // 2
    if k > n:
        raise ValueError("more steps than coarse segments")
    surf = BRep_Tool.Surface_s(fact["normal_face"])

    def on_face(p_: np.ndarray) -> np.ndarray:
        q = p_ / mm
        pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(q[0]), float(q[1]), float(q[2])), surf)
        if pr.NbPoints() > 0:
            r_ = pr.NearestPoint()
            return np.array([r_.X(), r_.Y(), r_.Z()]) * mm
        return p_

    pts, Qa, kept = _step_best(bottom, top, left, right, xyz, on_face, limits, high_mm, low_mm)
    X = np.vstack([xyz, pts]) if len(pts) else xyz.copy()
    types, _tags, conns = gmsh.model.mesh.getElements(2, t)
    ref = None
    for et, conn in zip(types, conns, strict=True):
        e_ = index[np.asarray(conn, dtype=np.int64)][: (4 if et == 3 else 3)]
        if len(e_) >= 3:
            ref = np.cross(xyz[e_[1]] - xyz[e_[0]], xyz[e_[2]] - xyz[e_[0]])
            break
    if ref is None:
        raise ValueError("no element of gmsh's to wind by")
    sgn = np.cross(X[Qa[:, 1]] - X[Qa[:, 0]], X[Qa[:, 3]] - X[Qa[:, 0]]) @ ref
    Qa[sgn < 0] = Qa[sgn < 0][:, ::-1]
    return pts, Qa, kept


def _thin_chain(gmsh, t: int, low: float) -> tuple[list[int], list[int], float] | None:
    """SHMESH-60: a face that is a narrow strip other than a plain four-sided one: its two longest curves (its long
    sides) are alike within NARROW_ALIKE, each at least as long as either way round between them (its ends: chains
    of curves), not neighbours unless the face has three sides (a needle: they meet at its tip), and its width (area
    over the long sides' mean) is under `low` (model units). Returns ([its long sides], [its end curves], width) or
    None."""
    _l, loops = ST._curve_loops(gmsh, t)
    if len(loops) != 1:
        return None
    cl = [abs(int(c)) for c in loops[0]]
    m = len(cl)
    if m < 3 or len(set(cl)) != m:
        return None
    ln = [float(gmsh.model.occ.getMass(1, c)) for c in cl]
    i, j = sorted(sorted(range(m), key=lambda q: (-ln[q], q))[:2])
    if j - i in (1, m - 1) and m > 3:
        return None
    ends = [cl[i + 1:j], cl[j + 1:] + cl[:i]]
    e_len = [sum(ln[cl.index(c)] for c in e) for e in ends]
    if not any(ends) or min(ln[i], ln[j]) < max(e_len):
        return None
    if abs(ln[i] - ln[j]) > NARROW_ALIKE * max(ln[i], ln[j]):
        return None
    width = float(gmsh.model.occ.getMass(2, t)) / max(0.5 * (ln[i] + ln[j]), 1e-300)
    if width >= low:
        return None
    return [cl[i], cl[j]], ends[0] + ends[1], width


def _side_nodes(gmsh, c: int, index: np.ndarray) -> list[int]:
    """The mesh nodes along a curve in its own order, its two end vertices included."""
    inner, _x, par = gmsh.model.mesh.getNodes(1, c, includeBoundary=False)
    ends = [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=True)]
    first = gmsh.model.mesh.getNodes(0, ends[0])[0]
    last = gmsh.model.mesh.getNodes(0, ends[-1])[0]
    order = np.argsort(np.asarray(par, dtype=float)) if len(inner) else np.zeros(0, dtype=np.int64)
    seq = [int(first[0])] + [int(inner[q]) for q in order.tolist()] + [int(last[0])]
    return [int(index[v]) for v in seq]


def _narrow_pairs(gmsh, strips: list[int], index: np.ndarray, thin: dict, tag_of: dict[int, int],
                  xyz: np.ndarray | None = None) -> list[tuple]:
    """SHMESH-60: the node pairs across each strip to be closed - the element edges of its one row of quads that join
    its two long sides - as (node, node, sewn face). A strip meshed otherwise (a triangle in it, a row that is not
    one quad across) gives none and stays. A narrow face with chains for ends (meshed free, `thin` marks it "free"):
    its long sides' nodes paired in order, and every other node of it (inside it, on its ends) with the nearest node
    of its long sides; none when its sides came out with different counts."""
    out: list[tuple] = []
    for t in strips:
        if len(thin[t]) > 3 and thin[t][3] == "free":
            if xyz is None:
                continue
            a_s, b_s = (_side_nodes(gmsh, c, index) for c in thin[t][0])
            if len(a_s) != len(b_s) or len(a_s) < 2:
                continue
            if (np.linalg.norm(xyz[a_s[0]] - xyz[b_s[0]]) + np.linalg.norm(xyz[a_s[-1]] - xyz[b_s[-1]])
                    > np.linalg.norm(xyz[a_s[0]] - xyz[b_s[-1]]) + np.linalg.norm(xyz[a_s[-1]] - xyz[b_s[0]])):
                b_s = b_s[::-1]
            k = tag_of[t]
            out.extend((u, v, k) for u, v in zip(a_s, b_s, strict=True) if u != v)
            sides = np.array(a_s + b_s, dtype=np.int64)
            rest: set[int] = set()
            inner, _x, _p = gmsh.model.mesh.getNodes(2, t, includeBoundary=False)
            rest |= {int(v) for v in index[np.asarray(inner, dtype=np.int64)].tolist()}
            for c in thin[t][1]:
                got, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
                rest |= {int(v) for v in index[np.asarray(got, dtype=np.int64)].tolist()}
            rest -= set(sides.tolist())
            for v in sorted(rest):
                near = int(sides[np.argmin(np.linalg.norm(xyz[sides] - xyz[v], axis=1))])
                out.append((min(v, near), max(v, near), k))
            continue
        a = thin[t][0]
        side_sets: list[set[int]] = []
        for c in a:
            got, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            side_sets.append({int(v) for v in index[np.asarray(got, dtype=np.int64)].tolist()})
        types, _tags, conns = gmsh.model.mesh.getElements(2, t)
        if any(int(et) != 3 for et in types) or not len(types):
            continue
        quads = index[np.asarray(conns[0], dtype=np.int64)].reshape(-1, 4)
        got_pairs: set[tuple[int, int]] = set()
        ok = True
        for q in quads.tolist():
            across = [(q[i], q[(i + 1) % 4]) for i in range(4)
                      if (q[i] in side_sets[0] and q[(i + 1) % 4] in side_sets[1])
                      or (q[i] in side_sets[1] and q[(i + 1) % 4] in side_sets[0])]
            if len(across) != 2:
                ok = False
                break
            got_pairs |= {(min(u, v), max(u, v)) for u, v in across}
        if ok:
            out.extend((u, v, tag_of[t]) for u, v in sorted(got_pairs))
    return out


def _clean_cut(x: np.ndarray, c: list[int], limits) -> bool:
    """SHMESH-94: whether quad c has a cut into two triangles that are both inside every solver limit and every
    outer line of the user's list (the triangles SHMESH-10 would leave: no line lost)."""
    q = np.array([c], dtype=np.int64)
    outer = [(k, op, o) for sh, k, op, _t, o, _s in getattr(limits, "aims", ()) if sh == "tria" and o is not None]
    for cut in (q[:, [0, 1, 2, 0, 2, 3]], q[:, [0, 1, 3, 1, 2, 3]]):
        t = cut.reshape(-1, 3)
        m = shell_quality.tria_measures(x, t, extra=True)
        if shell_quality._tria_past(m, limits).any():
            continue
        if any(k in m and ((m[k] < o) if op == ">=" else (m[k] > o)).any() for k, op, o in outer):
            continue
        return True
    return False


def _beyond_after(xyz0: np.ndarray, conn0: list[int], xyz1: np.ndarray, conn1: list[int], limits) -> bool:
    """SHMESH-94: whether an element comes out past an outer line of the user's list - any line, the angle lines too
    - that it was inside (judged on a strip SHMESH-94 lets close: what the guard did not weigh there before)."""
    def past(x: np.ndarray, c: list[int]) -> set:
        a = np.array([c], dtype=np.int64)
        shape = "quad" if len(c) == 4 else "tria"
        m = (shell_quality.quad_measures(x, a, extra=True) if shape == "quad"
             else shell_quality.tria_measures(x, a, extra=True))
        return {k for sh, k, op, _t, o, _s in getattr(limits, "aims", ()) if sh == shape and o is not None and k in m
                and ((float(m[k][0]) < o) if op == ">=" else (float(m[k][0]) > o))}
    return bool(past(xyz1, conn1) - past(xyz0, conn0))


def _worse_after(xyz0: np.ndarray, conn0: list[int], xyz1: np.ndarray, conn1: list[int], limits, high_mm,
                 mends: bool = False) -> bool:
    """SHMESH-68: whether an element (conn0 at xyz0) becomes worse after a clean-up step made it conn1 at xyz1, in a
    way the steps after cannot mend: past a solver limit, longer than the user's longest edge, or past the user's
    outer limit on aspect or on a triangle's largest angle (a needle or a flat triangle: what a closing left on a real
    part) where it was not before. The angle and Jacobian lines the relaxing and mending after it mend are not judged
    here (judged here they refused the closing of every fillet of a plain beaded sheet)."""
    keys = {"aspect", "max_angle_tria"}
    outer = {(sh, k): o for sh, k, _op, _t, o, _s in getattr(limits, "aims", ()) if k in keys and o is not None}

    def grade(x: np.ndarray, c: list[int]) -> tuple[bool, bool, bool]:
        a = np.array([c], dtype=np.int64)
        shape = "quad" if len(c) == 4 else "tria"
        if shape == "quad":
            m = shell_quality.quad_measures(x, a, extra=True)
            hard = bool(shell_quality._quad_past(m, limits)[0])
            if hard and mends and float(m["min_jacobian"][0]) > 0 and _clean_cut(x, c, limits):
                # SHMESH-94: a quad past a solver limit that SHMESH-10's cut mends - two triangles inside every
                # solver limit and every outer line - costs no line (two triangles); refused for it, a strip stayed
                # open and its own quads were merged into triangles beyond the triangle-min line (s15: 17.6 deg)
                hard = False
        else:
            m = shell_quality.tria_measures(x, a, extra=True)
            hard = bool(shell_quality._tria_past(m, limits)[0])
        past = any(float(m[k][0]) > float(o) for (sh, k), o in outer.items() if sh == shape and k in m)
        long_ = high_mm is not None and float(m["max_edge"][0]) > float(high_mm)
        return hard, past, long_
    g0 = grade(xyz0, conn0)
    g1 = grade(xyz1, conn1)
    return any(b and not a for a, b in zip(g0, g1, strict=True))


def _close_narrow(xyz: np.ndarray, Q: np.ndarray, T: np.ndarray, q_face: list[int], t_face: list[int],
                  pairs: list[tuple], facts: list[dict], mm: float, limits=None, high_mm: float | None = None
                  ) -> tuple:
    """SHMESH-60: close the strips: each pair's two nodes become one, at their middle laid on the strip's own surface
    (a chain of pairs - touching strips closed together - is one node at its middle). An element left with fewer than
    three corners goes (the strip's own row); one left with three is a triangle. A strip whose closing would turn an
    element of the faces beside it over, or fold a corner in, is not closed (tried again without it, at most
    CLOSE_ROUNDS times). Returns (xyz, Q, T, q_face, t_face, the new nodes, strips closed)."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt

    n = len(xyz)
    faces_of_pairs = sorted({k for _u, _v, k in pairs})
    skip: set[int] = set()
    elems = [(list(q), f) for q, f in zip(Q.tolist(), q_face, strict=True)] + \
            [(list(t), f) for t, f in zip(T.tolist(), t_face, strict=True)]
    for _round in range(CLOSE_ROUNDS):
        parent = list(range(n))

        def root(a: int, parent: list[int] = parent) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        face_of_root: dict[int, int] = {}
        for u, v, k in pairs:
            if k in skip:
                continue
            ru, rv = root(u), root(v)
            if ru != rv:
                parent[max(ru, rv)] = min(ru, rv)
        to = np.array([root(a) for a in range(n)], dtype=np.int64)
        for u, _v, k in pairs:
            if k not in skip:
                face_of_root.setdefault(int(to[u]), k)
        moved = np.flatnonzero(to != np.arange(n))
        roots = sorted(set(to[moved].tolist()))
        new_xyz = xyz.copy()
        surfaces: dict[int, Any] = {}
        for r_ in roots:
            members = np.flatnonzero(to == r_)
            p = xyz[members].mean(axis=0)
            k = face_of_root.get(r_)
            if k is not None:
                if k not in surfaces:
                    surfaces[k] = BRep_Tool.Surface_s(facts[k]["normal_face"])
                q = p / mm
                pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(q[0]), float(q[1]), float(q[2])), surfaces[k])
                if pr.NbPoints() > 0:
                    np_ = pr.NearestPoint()
                    p = np.array([np_.X(), np_.Y(), np_.Z()]) * mm
            new_xyz[r_] = p
        bad: set[int] = set()
        rescued: set[int] = set()
        beyond: set[int] = set()
        touched = set(moved.tolist()) | set(roots)
        root_face: dict[int, set[int]] = {}
        for u, _v, k in pairs:
            if k not in skip:
                root_face.setdefault(int(to[u]), set()).add(k)
        for conn, _f in elems:
            if not touched & set(conn):
                continue
            c = [int(to[a]) for a in conn]
            kept = [c[i] for i in range(len(c)) if c[i] != c[(i + 1) % len(c)]]
            if len(kept) < 3 or len(set(kept)) != len(kept):
                continue
            before = shell_mesh._normal(xyz, np.array(conn))
            after = shell_mesh._normal(new_xyz, np.array(kept))
            guard = CLOSE_GUARD and limits is not None
            worse = guard and _worse_after(xyz, list(conn), new_xyz, kept, limits, high_mm)
            if worse and CLOSE_CUT_MENDS and not _worse_after(xyz, list(conn), new_xyz, kept, limits, high_mm,
                                                             mends=True):
                # (SHMESH-94: worse only for a quad SHMESH-10's cut mends: the strip may close, judged on every line)
                worse = False
                for a in kept:
                    rescued |= root_face.get(a, set())
            if guard and CLOSE_CUT_MENDS and _beyond_after(xyz, list(conn), new_xyz, kept, limits):
                for a in kept:
                    beyond |= root_face.get(a, set())
            if float(np.dot(after, before)) <= 0.0 or shell_mesh._corners_turn(new_xyz, kept, before) or worse:
                # (SHMESH-68: nor one that would leave an element past a limit of the list it was inside)
                for a in kept:
                    bad |= root_face.get(a, set())
        bad |= rescued & beyond      # (SHMESH-94: a strip let through closes only where no line is newly lost)
        if not bad:
            break
        skip |= bad
    else:
        return xyz, Q, T, list(q_face), list(t_face), [], []
    out_q, out_t, fq, ft = [], [], [], []
    for conn, f in elems:
        c = [int(to[a]) for a in conn]
        kept = [c[i] for i in range(len(c)) if c[i] != c[(i + 1) % len(c)]]
        if len(set(kept)) != len(kept) or len(kept) < 3:
            continue
        if len(kept) == 4:
            out_q.append(kept)
            fq.append(f)
        else:
            out_t.append(kept)
            ft.append(f)
    closed = [k for k in faces_of_pairs if k not in skip]
    return (new_xyz, np.array(out_q, dtype=np.int64).reshape(-1, 4), np.array(out_t, dtype=np.int64).reshape(-1, 3),
            fq, ft, roots, closed)


def _short_edges(gmsh, tag_of: dict[int, int], rows: list[dict], mm: float, low_mm: float | None = None,
                 along: tuple[float, float, float] | None = None,
                 along_skip: frozenset[int] | set[int] = frozenset()) -> dict:
    """SHMESH-27, edges: every edge of the CAD shorter than 1 / FEATURE_RATIO of the size of the faces it bounds
    takes FEATURE_RATIO times its own length as the size beside it (grown from there by the grading fields). The
    edge itself still gets one segment. Returns {"by_size": {size mm: [curves]}, "edges", "lengths": {curve: mm},
    "fine_at": {CAD vertex: the finest size (mm) a feature gives beside it - a short edge ending there, a narrow
    face (one SHMESH-27 sized) it is a corner of}}."""
    by_size: dict[float, list[int]] = {}
    fine_at: dict[int, float] = {}
    lengths: dict[int, float] = {}
    along_faces: set[int] = set()

    def ends(c: int) -> list[int]:
        return [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)]

    for _d, c in gmsh.model.getEntities(1):
        faces = [int(t) for t in gmsh.model.getAdjacencies(1, c)[0] if int(t) in tag_of]
        if not faces or not FEATURE_EDGES:
            continue
        for t in faces:
            if "SHMESH-27" in rows[tag_of[t]]["rule"]:
                for p in ends(int(c)):
                    fine_at[p] = min(fine_at.get(p, math.inf), float(rows[tag_of[t]]["size_mm"]))
        size = min(rows[tag_of[t]]["size_mm"] for t in faces)
        wall = min(rows[tag_of[t]]["wall_mm"] for t in faces)
        length = float(gmsh.model.occ.getMass(1, c)) * mm
        h = plan_rules.feature_size(length, size=size, wall=wall, ratio=plan_rules.FEATURE_RATIO)
        if along is not None and length < along[1] and not any(tag_of[t] in along_skip for t in faces):
            # SHMESH-51o63: one segment on the edge, the elements beside it as long as the aspect target allows
            h_a = plan_rules.feature_size(length, size=min(size, 0.98 * along[2]), wall=wall, ratio=along[0])
            if h_a != h:
                along_faces |= {tag_of[t] for t in faces}
            h = h_a
        if h is not None and low_mm is not None:
            # SHMESH-62: never graded under the user's shortest edge (the edge itself is stepped over in the mend)
            h = None if low_mm >= size else max(h, float(f"{low_mm:.6g}"))
        if h is None:
            continue
        by_size.setdefault(float(f"{h:.6g}"), []).append(int(c))
        for p in ends(int(c)):
            fine_at[p] = min(fine_at.get(p, math.inf), h)
        lengths[int(c)] = length
    return {"by_size": by_size, "edges": len(lengths), "fine_at": fine_at, "lengths": lengths,
            "along_faces": along_faces}


def _count_plan(rows: list[dict], planned: float, target: dict | None, correction: float) -> tuple[dict | None, float]:
    """SHMESH-23: the plan's rows scaled in place to the user's element count: (what was done, the factor on every
    size); (None, 1.0) with no count asked. `planned` is the rules' own estimate, `correction` the built-over-planned
    measured on a first mesh (1.0 before one)."""
    if not target or int(target.get("elements") or 0) <= 0:
        return None, 1.0
    corr = float(correction) if correction and correction > 0 else 1.0
    rules = planned * corr
    aim = plan_rules.count_aim(rules, target)
    out = {"asked": int(target["elements"]), "mode": str(target.get("mode") or "about"),
           "rules_elements": int(round(rules)), "correction": round(corr, 4)}
    if aim is None:
        out.update(kept_rules=True, scale=1.0, planned=int(round(planned)))
        return out, 1.0
    sz = plan_rules.count_scale(rules, aim)
    held = sz in plan_rules.SCALE_RANGE
    # past what a shell mesh is made of in the time allowed (SHMESH-20), the count is held there, and said
    if planned / sz ** 2 > PLANNED_MAX:
        sz, held = math.sqrt(planned / (0.95 * PLANNED_MAX)), True
    under = False
    for r in rows:
        r["size_mm"] = float(f"{r['size_mm'] * sz:.6g}")
        if "curve_mm" in r:
            r["curve_mm"] = float(f"{r['curve_mm'] * sz:.6g}")
        r["floored"] = False
        finest = min(r["size_mm"], r.get("curve_mm", r["size_mm"]))
        under = under or finest < plan_rules.FLOOR_WALLS * r["wall_mm"] * (1 - 1e-6)
        r["rule"] = f"{r['rule']}+SHMESH-23"
    out.update(scale=round(sz, 4), planned=int(round(planned / sz ** 2)), under_floor=under, held=held)
    return out, sz


def _place_sizes(rows: list[dict], owners: list[str], record: dict, requirements: dict, facts: list[dict],
                 mm: float) -> dict | None:
    """SHMESH-52: the plan's rows sized in place for each size the notes ask in one named place - the registry's shell
    lane (contracts/mesh_controls `place_size`, NOTE-13's one loop) on the parts reading the shell run was handed
    (requirements["places"], engines/shell_places). What was done, or None; the rows are as they were when nothing
    could be placed (or our own step failed)."""
    import copy

    from meshpipeline.engines import control_appliers as CA

    kept = copy.deepcopy(rows)
    target: dict[str, Any] = {
        "rows": rows, "owners": owners, "record": record, "places": requirements.get("places"),
        "areas_mm2": [f["area"] * mm * mm for f in facts], "planned_max": PLANNED_MAX,
        "curved": [(f["bend"], min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"]) for f in facts]}
    CA.apply("shell", requirements, target)
    got: dict | None = target.get("applied")
    if not (got or {}).get("applied"):
        rows[:] = kept
    return got


def _edge_place_curves(gmsh, applied: dict, source: dict | None, mm: float, wall_mm: float) -> dict[float, list[int]]:
    """SHMESH-52b: {size (mm): curves} - for each edge place of `applied` (engines/shell_places.plan), the model's curves
    lying on its CAD face (its triangles kept in `source`, the reading), within shell_places.EDGE_TOL_WALL of the
    wall. Each place is told what was found (shell_places.edge_found): its curves' points, or not applied, with why."""
    from meshpipeline.engines import shell_places as SP

    out: dict[float, list[int]] = {}
    edge = [(j, p) for j, p in enumerate(applied.get("places") or []) if p.get("edge")]
    if not edge:
        return out
    pts: dict[int, np.ndarray] = {}
    for _d, c in gmsh.model.getEntities(1):
        lo, hi = gmsh.model.getParametrizationBounds(1, c)
        if not len(lo) or hi[0] <= lo[0]:
            continue
        xyz = gmsh.model.getValue(1, c, list(np.linspace(lo[0], hi[0], SP.EDGE_SAMPLES)))
        pts[int(c)] = np.asarray(xyz, dtype=float).reshape(-1, 3) * mm
    for j, p in edge:
        t = ((source or {}).get("entries") or [])[p["entry"]]["targets"][p["target"]]
        curves = SP.edge_curves(pts, t.get("tris_mm") or [], SP.EDGE_TOL_WALL * wall_mm)
        SP.edge_found(applied, j, [pts[c] for c in curves], t)
        if curves:
            out.setdefault(float(f"{float(p['set_mm']):.6g}"), []).extend(curves)
    return out


def _projector(facts: list[dict], on_face: np.ndarray, mm: float):
    """SHMESH-14c: project(nodes, points) - each point laid onto its node's own curved face's surface (the closest
    point; mm in and out). Nodes of flat faces (on_face -1) are left as given (the middle of coplanar points stays
    in their plane)."""
    from OCP.BRep import BRep_Tool
    from OCP.Extrema import Extrema_ExtAlgo
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from OCP.Precision import Precision

    surfaces: dict[int, Any] = {}
    # SHMESH-82: one projector per face, its search grid built once (the projector made afresh for every point built
    # it again each time: a B-spline face of a stamped panel spent the 15 minutes there), set up exactly as a fresh one
    # is (the surface's bounds, Precision::Confusion) so every answer is the same to the last bit; and every point
    # asked once (the relaxing asks the same middle again round after round)
    seen: dict[tuple[int, float, float, float], tuple[float, float, float] | None] = {}
    seen_local: dict[tuple, tuple[float, float, float] | None] = {}

    from OCP.ShapeAnalysis import ShapeAnalysis_Surface

    analysers: dict[int, Any] = {}
    uv_of: dict[tuple[int, float, float, float], Any] = {}

    def face_of(k: int):
        if k not in surfaces:
            surf = BRep_Tool.Surface_s(facts[k]["normal_face"])
            pr_k = None
            if PROJECT_ONCE:
                pr_k = GeomAPI_ProjectPointOnSurf()
                u1, u2, v1, v2 = surf.Bounds()
                pr_k.Init(surf, u1, u2, v1, v2, Precision.Confusion_s(),
                          Extrema_ExtAlgo.Extrema_ExtAlgo_Tree if PROJECT_TREE
                          else Extrema_ExtAlgo.Extrema_ExtAlgo_Grad)
            surfaces[k] = (surf, pr_k)
        return surfaces[k]

    def global_(k: int, p: tuple[float, float, float], near: np.ndarray | None):
        """The closest point by the face's global search; with `near` (the node, model units) the solution found
        nearest the node rather than nearest the point (SHMESH-82c: the fallback)."""
        surf, pr_k = face_of(k)
        if pr_k is not None:
            pr_k.Perform(gp_Pnt(*p))
            pr = pr_k
        else:
            pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(*p), surf)
        if pr.NbPoints() <= 0:
            return None
        if near is None:
            q = pr.NearestPoint()
            return (q.X(), q.Y(), q.Z())
        sols = [pr.Point(j) for j in range(1, pr.NbPoints() + 1)]
        q = min(sols, key=lambda r: (r.X() - near[0]) ** 2 + (r.Y() - near[1]) ** 2 + (r.Z() - near[2]) ** 2)
        return (q.X(), q.Y(), q.Z())

    def local(k: int, p: tuple[float, float, float], here: np.ndarray):
        """SHMESH-82c: the closest point found from the node's own place on its surface (its UV, the node itself
        projected - on the surface, so every search agrees), or None when that fails or lands further from the node
        than LOCAL_REACH times the move asked."""
        surf, _pr = face_of(k)
        if k not in analysers:
            analysers[k] = ShapeAnalysis_Surface(surf)
        sas = analysers[k]
        hk = (k, float(here[0]), float(here[1]), float(here[2]))
        if hk not in uv_of:
            if len(uv_of) > PROJECT_SEEN_MAX:
                uv_of.clear()
            uv_of[hk] = sas.ValueOfUV(gp_Pnt(*hk[1:]), Precision.Confusion_s())
        uv = sas.NextValueOfUV(uv_of[hk], gp_Pnt(*p), Precision.Confusion_s(), -1.0)
        q = surf.Value(uv.X(), uv.Y())
        got = np.array([q.X(), q.Y(), q.Z()])
        asked = float(np.linalg.norm(np.asarray(p) - here))
        if not np.isfinite(got).all() or float(np.linalg.norm(got - here)) > LOCAL_REACH * max(asked, 1e-12):
            return None
        return (float(got[0]), float(got[1]), float(got[2]))

    def project(nodes: np.ndarray, pts: np.ndarray, here: np.ndarray | None = None) -> np.ndarray:
        """Each point laid on its node's face. With `here` (the nodes where they stand, mm) and PROJECT_LOCAL, from
        the node's own place on the surface (SHMESH-82c)."""
        out = pts.copy()
        for i, v in enumerate(nodes.tolist()):
            k = int(on_face[v])
            if k < 0:
                continue
            p = pts[i] / mm
            key = (k, float(p[0]), float(p[1]), float(p[2]))
            if PROJECT_LOCAL and here is not None:
                h = here[i] / mm
                lk = (key, float(h[0]), float(h[1]), float(h[2]))
                if PROJECT_ONCE and lk in seen_local:
                    got = seen_local[lk]
                else:
                    got = local(k, key[1:], h)
                    if got is None:
                        got = global_(k, key[1:], h)
                    if PROJECT_ONCE:
                        if len(seen_local) > PROJECT_SEEN_MAX:
                            seen_local.clear()
                        seen_local[lk] = got
            elif PROJECT_ONCE and key in seen:
                got = seen[key]
            else:
                got = global_(k, key[1:], None)
                if PROJECT_ONCE:
                    if len(seen) > PROJECT_SEEN_MAX:
                        seen.clear()
                    seen[key] = got
            if got is not None:
                out[i] = np.array(got) * mm
        return out

    return project


def _slider(project, on_line: np.ndarray, line_a: np.ndarray, line_b: np.ndarray):
    """SHMESH-14d: project(nodes, points) - a node on a straight CAD edge is laid back onto that edge (the closest
    point of the line through the edge's two ends, kept between them: SLIDE_ENDS of its length from either end);
    every other node goes through `project` (the curved faces' own, or none)."""
    def both(nodes: np.ndarray, pts: np.ndarray, here: np.ndarray | None = None) -> np.ndarray:
        out = project(nodes, pts, here) if project is not None else pts.copy()
        sel = on_line[nodes]
        if sel.any():
            a, b = line_a[nodes[sel]], line_b[nodes[sel]]
            d = b - a
            t = np.einsum("mi,mi->m", pts[sel] - a, d) / np.maximum(np.einsum("mi,mi->m", d, d), 1e-300)
            out[sel] = a + np.clip(t, SLIDE_ENDS, 1.0 - SLIDE_ENDS)[:, None] * d
        return out

    return both


#: SHELL-9b: a node's wall is read from the WALL_NEAREST wall readings of its mid-face nearest it (a plane through
#: them, the nearer weighing more, so a wall that varies evenly - a taper, a draft - is read exactly), kept within
#: their thinnest and thickest. The 8 nearest were all one row of a narrow strip's readings: no slope along it
WALL_NEAREST = 24
#: a mid-face's wall varies when its thickest reading is more than WALL_VARIES thicker than its thinnest
WALL_VARIES = 0.01


def _wall_at(points: np.ndarray, samples: np.ndarray) -> np.ndarray:
    """The wall at each point (mm): a least-squares plane through the WALL_NEAREST readings nearest it
    (x, y, z, wall rows of `samples`), clipped to the readings' range."""
    xyz, w = samples[:, :3], samples[:, 3]
    k = min(WALL_NEAREST, len(samples))
    out = np.empty(len(points))
    for i, p in enumerate(points):
        d = np.linalg.norm(xyz - p, axis=1)
        near = np.argsort(d, kind="stable")[:k]
        rel = xyz[near] - p
        # the readings lie on a surface: fit over its two widest directions there (a 3D fit is near-singular)
        _u, sv, vt = np.linalg.svd(rel - rel.mean(axis=0), full_matrices=False)
        A = np.column_stack([np.ones(k), rel @ vt[:2].T])
        # nearer readings weigh more (a wall that varies unevenly is read locally)
        wt = 1.0 / (d[near] + float(np.median(d[near])) + 1e-300)
        coef, *_rest = np.linalg.lstsq(A * wt[:, None], w[near] * wt, rcond=1e-6)
        out[i] = coef[0]
    return np.clip(out, float(w.min()), float(w.max()))


def node_walls(mesh: ShellMesh, mids: dict[str, dict]) -> int:
    """SHELL-9b: every node of a mid-face whose wall varies (a tapered or drafted wall, fitted) gets its own wall,
    read from the record's wall readings; its elements are written per corner (CQUAD4 T1..T4, *NODAL
    THICKNESS). A varying mid-face without readings is never meshed at one wall: it stops here. Returns how many
    elements carry corner walls."""
    mid_of = np.array(mesh.mid)
    varies = np.zeros(mesh.n_elements, dtype=bool)
    total = np.zeros(len(mesh.xyz))
    count = np.zeros(len(mesh.xyz))
    for m in sorted(set(mesh.mid), key=lambda x: int(x.split("_")[1])):
        row = mids[m]
        lo, hi = row.get("thickness_range_mm", [row.get("thickness_mm"), row.get("thickness_mm")])
        if not (lo and hi and hi > lo * (1.0 + WALL_VARIES)):
            continue
        samples = np.asarray(row.get("wall_samples_mm") or [], dtype=float).reshape(-1, 4)
        if len(samples) < 3:
            raise ValueError(f"{m}'s wall varies ({lo:g} to {hi:g} mm) but the record holds no wall readings: it "
                             "cannot be meshed at one wall (SHELL-9b)")
        els = np.flatnonzero(mid_of == m)
        nodes = np.unique(np.concatenate([mesh.element_nodes(int(e)) for e in els]))
        total[nodes] += _wall_at(mesh.xyz[nodes], samples)
        count[nodes] += 1.0
        varies[els] = True
    if not varies.any():
        return 0
    mesh.node_wall = np.where(count > 0, total / np.maximum(count, 1.0), 0.0)
    mesh.varies = varies
    used = np.unique(np.concatenate([mesh.element_nodes(int(e)) for e in np.flatnonzero(varies)]))
    if np.any(mesh.node_wall[used] <= 0):
        raise ValueError("a node of a varying wall came out with no wall (SHELL-9b)")
    return int(varies.sum())


def element_offsets(mesh: ShellMesh, mids: dict[str, dict]) -> int:
    """SHELL-7: an element of a mid-face snapped onto a thicker neighbour's mid-plane (a thickness step, a lap)
    carries the distance from its nodes' plane to its own true mid-plane, along its own normal (Nastran ZOFFS,
    Abaqus / CalculiX OFFSET, Ansys SECOFFSET). Returns how many elements are offset."""
    from meshpipeline.engines.shell_mesh import _normal

    z = np.zeros(mesh.n_elements)
    for e, m in enumerate(mesh.mid):
        row = mids[m]
        off = float(row.get("offset_mm") or 0.0)
        if not off:
            continue
        n_o = np.asarray(row.get("offset_normal") or [0.0, 0.0, 0.0], dtype=float)
        if not np.linalg.norm(n_o):
            raise ValueError(f"{m} is offset {off:g} mm but the record gives no direction (SHELL-7)")
        n_e = _normal(mesh.xyz, mesh.element_nodes(e))
        z[e] = off * float(np.dot(n_o, n_e / max(float(np.linalg.norm(n_e)), 1e-300)))
    if not np.any(z):
        return 0
    mesh.zoffs = z
    return int(np.count_nonzero(z))


def mesh_run(out_dir: Path | str, *, mm_per_unit: float = 1.0, target: dict | None = None,
             requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """Mesh the mid-surface cad/midsurface wrote into `out_dir`, and write shell_plan.json there. `target`: the
    user's element count (SHMESH-23), met on the built mesh - aimed again once when the first mesh missed it.
    `requirements`: the mesh requirements read from the user's notes (SHMESH-24..26); a global size among them
    decides the sizes, so a count asked beside it is set aside (and said: applied["count_set_aside"])."""
    from meshpipeline.cad import midsurface as ms

    out = Path(out_dir)
    record = json.loads((out / ms.RECORD_FILE).read_text())
    set_aside = None
    if requirements and ((requirements.get("sizes") or {}).get("global_mm") is not None) and target \
            and int(target.get("elements") or 0) > 0:
        set_aside, target = int(target["elements"]), None
    t_first = time.monotonic()
    try:
        mesh, applied = mesh_shell(out / ms.SURFACE_FILE, record, mm_per_unit=mm_per_unit, target=target,
                                   requirements=requirements)
    except (ValueError, EOFError) as exc:
        # SHMESH-11e: the closed faces cut at their seams (SHMESH-11) and the mesh failed - meshed again with the
        # closed faces whole, once, where the failure came fast (the run's clock still holds the second try)
        if not (WHOLE_RETRY and SPLIT_CLOSED and time.monotonic() - t_first < WHOLE_RETRY_S):
            raise
        logger.info("shell mesh: failed with the closed faces cut (%s); meshed again whole (SHMESH-11e)", exc)
        _WHOLE_NOW[0] = True
        try:
            mesh, applied = mesh_shell(out / ms.SURFACE_FILE, record, mm_per_unit=mm_per_unit, target=target,
                                       requirements=requirements)
        except (ValueError, EOFError):
            raise ValueError(str(exc) or type(exc).__name__) from exc
        finally:
            _WHOLE_NOW[0] = False
        why = f"{type(exc).__name__}: {exc}"[:200]
        applied["split_whole"] = {"rule": "SHMESH-11e", "cut_failed": why}
        mesh.notes.append("The closed faces were meshed whole: cut at their seams, the mesh failed "
                          f"({why}) (SHMESH-11e).")
    if set_aside:
        applied["count_set_aside"] = set_aside
    t = applied.get("target")
    if t and ((applied.get("quality") or {}).get("time_limit") or {}).get("reached"):
        # SHMESH-21b: a mesh delivered at the time limit is not aimed again - that would be a whole shell mesh more
        t["not_reaimed"] = "the shell mesh reached its time limit (SHMESH-21b)"
    elif t and not t.get("kept_rules") and not plan_rules.count_met(mesh.n_elements, target):
        # SHMESH-23: the built count missed the user's: aimed again once on the built-over-planned it measured, and
        # the closer of the two kept
        corr = float(t.get("correction") or 1.0) * mesh.n_elements / max(float(t.get("planned") or 1), 1.0)
        try:
            again, applied2 = mesh_shell(out / ms.SURFACE_FILE, record, mm_per_unit=mm_per_unit, target=target,
                                         correction=corr, requirements=requirements)
        except ValueError as exc:
            logger.info("shell mesh: the count was not aimed again (%s)", exc)
        else:
            aim = float(t["asked"])

            def gap(n: int) -> float:
                return abs(math.log(max(n, 1) / aim))

            if gap(again.n_elements) < gap(mesh.n_elements):
                mesh, applied = again, applied2
            applied["target"]["reaimed"] = True
    if applied.get("target"):
        tt = applied["target"]
        tt["built"] = int(mesh.n_elements)
        tt["held"] = bool(tt.get("held")) or (not tt.get("kept_rules") and not plan_rules.count_met(mesh.n_elements, target))
    (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
    return mesh, applied


def element_turn_deg(radius_mm: float, size_mm: float) -> float:
    """How far one element turns on a curve (degrees): the SHMESH-4 check."""
    return math.degrees(size_mm / radius_mm) if radius_mm > 0 else 0.0


__all__ = ["MESH_RATE", "PRICE_FACTOR", "FACE_HELD_FLOOR_S", "SHELL_CAP_ENV", "CLOCK_FILE", "mesh_price_s", "run_env", "outer_end", "run_following", "run_stopped_words", "past_limit_words", "ALGORITHM", "PER_TURN", "mesh_run", "mesh_shell"]
