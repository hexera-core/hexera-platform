# Responsibility: Say what the delivered mesh IS, read from the engine's own manifest.
# Boundaries: a reader over declared facts; it opens no mesh and measures nothing itself.
from __future__ import annotations

from typing import Any, Final

from meshpipeline.contracts.mesh_units import completed_mesh_unit

# The quality figures a delivered mesh may declare, and the type each must be.
# Engines that do not compute one simply omit it.
#: CLOSED ON PURPOSE, and widened deliberately rather than opened. The viewer groups by these, so
#: a metric that is not here never reaches a reader however faithfully the mesher measured it -
#: which is how "cell types and count per type" and "min and max cell sizes" came to be measured
#: on every run and shown on none (Corintis asked for both by name, 2026-10-29). Adding a metric
#: is an edit here AND in the viewer's label table; that pairing is the point.
_INT_METRICS: Final[tuple[str, ...]] = (
    "cells", "faces", "points", "internal_faces",
    # every cell type checkMesh counts, not the four that happened to be listed
    "hexahedra", "prisms", "wedges", "pyramids", "tet_wedges", "tetrahedra", "polyhedra",
    "regions", "skew_faces",
    # cells too flat for a solver to integrate over: the count behind "poor-quality cells"
    "small_determinant_cells",
    # AND THE TWO THE EXTENDED CHECKS FOUND, which are the biggest quality findings on the first
    # customer plate and could not be drawn. `conjugate_viewer.POOR_MAX_TRIANGLES` is 400,000 and
    # the metal's concave-cell surface is 862 MB, so the highlight skips it - rightly, since
    # 3,676,061 concave cells out of 241,984,911 is the staircase rather than a spot on it. Which
    # is the argument for the number: the picture cannot carry it, so the panel must.
    "concave_cells",
    # 22 faces on that plate's metal region, and the only thing in either checkMesh report labelled
    # an Error. A reading this small is exactly the kind that a summary rounds away.
    "low_quality_tet_faces",
    # THE THIRD OF THE SIX READINGS, "number of boundary layers in the fluid". The conjugate
    # engine computes it and writes it to the manifest, and until 2026-11-14 the answer was a
    # deliberate zero. It is now whatever the plan asked for, because that engine grows layers:
    # `cfmesh_conjugate/layers.py` runs a `snappyHexMesh` addLayers-only pass after the split, and
    # the measured run on the first customer plate grew 2.84 of three asked-for layers over 95.553%
    # of a 16,619,777-face interface.
    #
    # TWO ARGUMENTS THAT USED TO BE HERE WERE BOTH WRONG. Conformality was never the obstacle, and
    # that was measured on the coupon - all 14,477 interface faces stayed matched to 4e-18 m. Nor
    # does the stack fail to FIT: what was measured as "it takes the quality to checkMesh's bar and
    # buys nothing" was a stack 109% of its own background cell, which is a dictionary defect, and
    # `layers.fit_complaint` now refuses exactly that dict. At 60.0% of a cell the same part came
    # back with 2,936 faces in error out of 102,536,327 cells.
    #
    # THE COUNT ITSELF IS A FRACTION AND IS IN `_FLOAT_METRICS` BELOW, NOT HERE. It was here, and
    # that is the defect this split exists for: on the real plate (job 0ff6f9e5, 2026-11-16) the
    # engine measured 2.25 layers of the 3 asked for, and `_as_int(2.25)` published 2. The user had
    # already been quoted 3, which was the REQUEST. 2 is neither the request nor the achievement, so
    # the one change whose whole purpose was to stop publishing an intention as a measurement would
    # have shipped a THIRD number, and one the mesh does not have.
    #
    # WHAT DOES BELONG HERE IS THE ASK, as a whole number of layers, because the achievement alone
    # is not a reading: 2.25 with nothing beside it cannot be seen to have fallen short of
    # anything, which is how 3 came to be quoted as an achievement in the first place.
    "near_wall_layers_asked",
    # THE FOURTH OF THIS ENGINE'S FOUR LAYER FLOORS TAKES TWO NUMBERS.
    # `layers.ILLEGAL_PER_MILLION_CEILING` is 200.0 faces in error per million cells ADDED, so
    # neither figure can be read alone: the plate left 8,916 over 37,247,044, which is 239 per
    # million and over the ceiling. The count by itself is a number with nothing to compare to.
    "layer_faces_in_error", "layer_cells_added",
    # HOW MANY REPAIR ITERATIONS THE MOVER RAN - 10 on the plate, and every one of them took
    # coverage DOWN, 97.2267% at extrusion to 88.5826% at the end. The final coverage says a floor
    # was missed; the iteration count beside it says the REPAIR is what missed it, which is a
    # different defect and a different fix. The ten-point curve itself stays in the manifest: a
    # series is not a figure, and this panel has one value to a row.
    "layer_iterations",
    # HOW MANY FLOORS THIS ENGINE JUDGES A LAYER PASS BY - `layers.LAYER_FLOORS`, which is four.
    # THE DENOMINATOR, and the panel had none: a run that fell under three of them showed three
    # sentences, and "three of four" was arithmetic the reader had to do from a number that was
    # nowhere on the page. It travels rather than being written into the viewer because a
    # constant with a second reader on the far side of an HTTP boundary is a constant that can
    # disagree, which is the mistake this engine has already paid for once.
    "layer_floors_total",
    # The graded levels and the cell size at the wall still travel beside the count, because a
    # layer count alone does not say how the wall was resolved.
    "near_wall_levels",
    # and the conformality itself, which is the number that says the interface is real: the same
    # face count on each side of it
    "interface_faces_fluid", "interface_faces_solid",
    # ------------------------------------------------------------------ WHERE THE STACK WENT
    # THE TWO CAUSES OF A MISSING FACE, TOLD APART, and the mechanism that names why. A single
    # coverage figure cannot do it: 90.6438% is what job f55ef23e covered, and of the 9.36 points
    # missing, 7.44 were NEVER OFFERED - the extruder declining a face where the surface turns by
    # more than featureAngle, which is the part's own shape - against 1.69 that were offered and
    # then taken back, which is the mover. The fix for each is a different one, and a reader
    # holding only the total cannot tell which they need.
    #
    # `layer_isolated_points_stopped` IS THE MECHANISM, in snappyHexMesh's own words
    # ("displacementMedialAxis : Number of isolated points extrusion stopped"). It read 2,371,211
    # on that run BEFORE the first addition, and 0 on every featureAngle-180 run of this part -
    # all of which added 100% of their ideal cells at iteration 0. It is the only quantity on this
    # panel that says WHY a stack is short rather than THAT it is, so it belongs beside the
    # figures and not in a log nobody keeps.
    #
    # THE DENOMINATORS TRAVEL WITH THEM. `layer_faces_on_patch` is what every percentage here is
    # over, and `layer_faces_extruded` is what `layers_per_extruded_face` divides by: a share with
    # no denominator on the page is a figure a reader has to take on trust.
    "layer_faces_on_patch", "layer_faces_extruded",
    "layer_faces_never_offered", "layer_faces_removed_first_iter",
    "layer_cells_added_first_iter", "layer_isolated_points_stopped",
)
_FLOAT_METRICS: Final[tuple[str, ...]] = (
    "max_non_ortho", "max_skewness", "avg_non_ortho", "skew_fraction",
    # the size RANGE, as lengths. On a two-scale part the ratio is how many octree levels the
    # delivered mesh actually spans, which is the number its budget was set by.
    "min_cell_m", "max_cell_m", "cell_size_ratio", "max_aspect_ratio",
    # the cell AT the wall, which is the near-wall resolution a reader asking about layers wants
    "near_wall_cell_m",
    # THE LAYER COUNT THE MESH ACTUALLY HAS, to the precision it was measured at. A stack that
    # covers part of a patch and was thinned over the rest does not come back a whole number -
    # `snappyHexMesh` reports a mean per patch, and on the plate that mean is 2.25. Rounding it is
    # not caution, it is a third figure: the ask was 3, the mesh has 2.25, and `_as_int` published
    # 2 to the one panel a customer reads.
    "near_wall_layers",
    # HOW WELL IT GREW, against `layers.KEPT_OF_OFFERED_FLOOR_PCT` 95.0 and
    # `THICKNESS_FLOOR_PCT` 75.0. These, the pair of counts above and the ask are every number
    # this engine draws its own four floors on, so a reader can check all four from the payload
    # without opening the manifest.
    #
    # THE FIRST FLOOR MOVED, AND THE PANEL HAD TO MOVE WITH IT. It was COVERAGE_FLOOR_PCT 92.0,
    # and coverage turned out to be two things added together: the faces `featureAngle` declined
    # to offer, which is the part's shape, and the faces offered and then lost, which is the
    # mover. Job b8f09ae9 measured the split - 91.1145% offered, 99.5641% of that kept - so of
    # 9.28 missing points 8.89 were never the pass's to grow. `layer_kept_of_offered_pct` is
    # the figure the floor is drawn on now, and `layer_offered_pct` is what makes the other two
    # readable, so BOTH travel: a panel showing only the sum is the page this engine already
    # paid for once, where a good pass and a failed one printed the same number.
    #
    # `layer_coverage_pct` IS THE ONE OF THESE WITH MORE THAN ONE PRODUCER, and it was grepped
    # before it was read: cfmesh_conjugate, cfmesh, snappy, snappy_multiregion and vmtk all write
    # it, all meaning the share of wall faces that ended up layered, and `engines/verdict_limits`
    # declares the single cross-engine Limit on it ("Wall covered by prism layers (percent)",
    # >= 80.0). So this row widens the panel for five engines and not one, deliberately: it is one
    # quantity under one name. The other ten keys in this group and the four above have exactly one
    # producer each, `cfmesh_conjugate`. What is NOT picked up is cfMesh and snappy own layer
    # counts, which travel under different names (`layers_requested`, `layers_in_meshdict`,
    # `layers_stated`, `layers_mean`) and are mostly ASKS - the field this change exists to stop
    # publishing as a result. Carrying those needs its own measurement, not this edit.
    "layer_coverage_pct", "layer_thickness_pct",
    "layer_offered_pct", "layer_kept_of_offered_pct",
    # and the stack as a LENGTH, beside `near_wall_cell_m`: 1.09e-05 m on the plate. A percentage
    # of an ask says nothing about what it resolves.
    "layer_thickness_m",
    # ------------------------------------------------------------------ THREE DEPTHS, NOT ONE
    # ONE NUMBER WAS DOING TWO JOBS AND THE PANEL SHOWED ONLY THAT NUMBER. `layers_in_mesh` is the
    # patch table's own mesh_layers column, which is cells_added / ALL patch faces: measured on
    # job f55ef23e, 33,123,053 / 16,582,198 = 1.9975, published as "2.00 layers". A pass that
    # grows three full layers on two thirds of the patch and nothing on the rest reports 2.00, and
    # so does a pass that grows exactly two layers everywhere. Those are not the same mesh, only
    # one of them is a problem, and the panel could not tell them apart.
    #
    # SO THE OTHER TWO DEPTHS TRAVEL BESIDE IT. `layers_at_iteration_0` is the depth before any
    # repair ran, and `layers_per_extruded_face` averages over the faces that KEPT a stack rather
    # than over the whole patch. On that run: 1.9975 / 2.0817 / 2.2037. Together they say the
    # stack was two cells deep before the first repair and that the repair cost 0.08 of a layer -
    # neither of which the single figure could say.
    #
    # `layers_in_mesh` IS NOT TOUCHED, and that is deliberate: LAYERS_FLOOR_FRACTION is calibrated
    # on it, and quietly redefining a number a floor is drawn on is how a bar stops meaning
    # anything. Note that all THREE depths miss the 0.75 x 3 = 2.25 floor on that run, so the
    # floor is telling the truth about a short mesh - this edit makes the panel honest about WHAT
    # is short, and does not make anything pass.
    "layers_at_iteration_0", "layers_per_extruded_face",
    # The shares the two face counts above are of the patch, so a reader does not divide by hand.
    "layer_never_offered_pct", "layer_removed_first_iter_pct",
    "layer_cells_added_first_iter_pct",
)

#: Declared yes/no answers. A GROUP OF THEIR OWN BECAUSE A BOOL IS NOT A SMALL NUMBER: `_as_int`
#: and `_as_float` both refuse one deliberately (a True arriving as a count would read as 1), so a
#: flag put through either of them disappears - and the one flag here is the one that says whether
#: the number beside it was measured at all.
_FLAG_METRICS: Final[tuple[str, ...]] = (
    # WHETHER `near_wall_layers` IS A MEASUREMENT OR AN INTENTION. `finalize._near_wall` publishes
    # the plan's ask under that name when no pass measured anything - on every mesh this engine has
    # delivered with `layers: 0` that is the honest answer, and on a run whose layer log could not
    # be read it is the only answer there is. 3 asked and 3 grown are the same number and only one
    # of them is evidence, so the payload has to be able to tell them apart. ABSENT when the engine
    # says nothing about layers at all, which reads as neither.
    "near_wall_layers_measured",
)
#: Declared NAMES, carried as text, stripped, and dropped when empty - an empty name is not an
#: answer to "which surface".
_TEXT_METRICS: Final[tuple[str, ...]] = (
    # WHICH SURFACE THE LAYERS ARE ON. On a conjugate mesh that is the coupling itself,
    # `fluid_to_solid`, and it is the one patch the mesh exists for; a layer figure with no patch
    # beside it could be about any of the nine boundaries.
    "layer_patch",
)
#: Declared FINDINGS: a list of lines, each a sentence a reader can act on. EMPTY SURVIVES, and
#: that is the whole reason this group is not filtered on truthiness. `[]` is "this was judged and
#: it met every floor"; an ABSENT key is "nobody judged it". Collapsing the two is the failure this
#: file exists to stop - a run that fell three floors short would then read exactly like a run
#: nobody ever looked at.
#:
#: THE RULE BINDS THE PRODUCER TOO, AND FOR A DAY IT DID NOT. A reader is only entitled to read
#: `[]` as a clean pass if nothing publishes `[]` without judging, and
#: `cfmesh_conjugate/finalize._layers_published` did: a run whose layer log could not be read came
#: through as `{"layer_iterations": 0, "layer_short": []}`, because the only figure an unreadable
#: log leaves behind is an iteration count of 0 and the guard there asked whether ANY figure
#: survived. It is closed from that side (2026-11-17) - this file's own contract is unchanged,
#: which is why there is nothing to filter here.
#:
#: WHERE THAT GUARD ACTUALLY IS, CORRECTED 2026-11-18. This pointed at a helper called
#: `_floors_had_a_reading`, which had no caller anywhere in src/, ui/, apps/ or tests/ and has now
#: been removed. The guard is `len(unjudged) >= len(layers.LAYER_FLOORS)` inside
#: `finalize._layers_published` itself, which returns the two tally keys and nothing else. The
#: contract above was correct then and is correct now; only the pointer to the mechanism was
#: wrong, which is worse than no pointer - a reader who follows it looks for the guard where it
#: is not and does not find the one that runs.
_LINE_METRICS: Final[tuple[str, ...]] = (
    # WHERE THE LAYER PASS FELL UNDER THIS ENGINE'S OWN FLOORS, in `layers.shortfalls`' own words,
    # computed once inside `conjugate.build` against the pass's own `cells_added` - a number no
    # other artifact holds, so a second reader could not get the same answer. Three lines on the
    # real plate. The figures above let a reader derive the misses; these say them.
    "layer_short",
    # ...AND WHICH OF THOSE FLOORS HAD NOTHING TO BE JUDGED ON, by name
    # (`layers.floors_unjudged`). NAMES AND NOT SENTENCES, which is the one member of this group
    # that is not a finding - it is here because it needs exactly this group's three states and
    # none of the others': `[]` is "all four floors had a figure", a non-empty list is which ones
    # did not, and an ABSENT key is a run that published nothing about layers at all.
    #
    # WITHOUT IT `layer_short` CARRIED TWO OPPOSITE MEANINGS. MEASURED, SYNTHETIC, 2026-11-17: a
    # layer log holding only the Extruding and Added lines published `layer_coverage_pct` 99.9 and
    # `layer_short` `[]`, which this file's own rule defines as "judged and met every floor" -
    # while `near_wall_layers_measured` on the SAME payload said the count had not been measured.
    # One floor had been evaluated and the panel reported four. The all-four-absent case is closed
    # on the producer side by the `len(unjudged) >= len(layers.LAYER_FLOORS)` return in
    # `finalize._layers_published` (until 2026-11-18 this pointed at a helper called
    # `_floors_had_a_reading`, which had no caller and has been removed - `finalize`'s own note
    # above that function carries what it recorded); this key closes the partial case, which that
    # return by design lets through.
    #
    # AND WHAT THIS KEY ASSERTS, NARROWED 2026-11-18: which floors had no FIGURE to be drawn on,
    # inferred by `layers.floors_unjudged` from the measured figures. It is NOT a record of which
    # floors `shortfalls` reached. The two part only when `shortfalls` is called with no
    # `n_layers` AND the layer table row asks for 0, which `conjugate.build` cannot do (it builds
    # a layer report only under `layers > 0` and passes `n_layers=layers`); MEASURED, SYNTHETIC,
    # 2026-11-18, the tally is exact on every shape the producer can emit, and `finalize`'s own
    # comment carries the table and why the obvious roster patch moves the error rather than
    # closing it.
    "layer_floors_unjudged",
)

# Part kinds. Closed: the viewer groups by these, so a new kind is a deliberate
# addition on both sides rather than a string that appears one day.
BOUNDARY: Final = "boundary"
REGION: Final = "region"
CELL_GROUP: Final = "cellGroup"
INSPECTION: Final = "inspection"
#: The cells checkMesh would not pass, drawn as their own surface. A kind of their own because
#: the viewer groups by kind and these were arriving as ordinary boundary patches: the sixth of
#: the six readings the customer asked for is "highlight of poor-quality cells", and a highlight
#: listed between ymin and zmax in the same palette colour as a wall is not one.
POOR_CELLS: Final = "poorCells"
KINDS: Final[frozenset[str]] = frozenset({BOUNDARY, REGION, CELL_GROUP, INSPECTION, POOR_CELLS})

VISIBLE: Final = "visible"
HIDDEN: Final = "hidden"

# Boundary ROLES that enclose the model. Shown solid they are a wall of colour
# with the body trapped inside, so they open hidden. Role-driven, never
# name-driven: a patch called "farfield" that is a wall is a wall.
_ENCLOSING_ROLES: Final[frozenset[str]] = frozenset({
    "farfield", "freestream", "external", "atmosphere",
})


def far_side_interfaces(rendered: tuple[str, ...],
                        enclosing: tuple[str, ...] = ()) -> dict[str, str]:
    """{drawn surface: the drawn surface it duplicates} for one side of every interface
    DRAWN TWICE.

    A conjugate interface is ONE surface with two sides, and a payload that carries both hands
    the renderer two coincident sheets: measured on this plate, `fluid/fluid_to_solid` and
    `solid/solid_to_fluid` are 16,619,777 faces each, the same faces, and what a reader sees
    across every channel wall is not detail but a speckle of two materials, worse the closer they
    look. So one of the two opens HIDDEN. Not dropped: the metal is 63% of this mesh and the
    channel walls carved into it are the only thing in it worth looking at, so a reader who
    isolates the metal has to be able to reach them - and when the coolant is off screen there is
    nothing left for that sheet to fight with.

    THE PAIRING IS THE NAME, which is the convention `splitMeshRegions` writes: region `A` calls
    its side of the coupling to `B` `A_to_B`, so the drawn `A/A_to_B` is paired with `B/B_to_A`
    and with nothing else. A patch whose partner is not drawn is not in here at all.

    WHICH SIDE IS THE FAR ONE is decided by `enclosing` and not by a region name. The region that
    also owns the outside of the block - the one whose other patches wrap everything, which on a
    cooled solid is the metal - is the outer body, so its side is the far side. That is the same
    reasoning `cfmesh_conjugate.viewer._skip_for` states for dropping the solid's side, derived
    from the payload instead of from the word "solid". When neither region owns an enclosing
    surface the two are still the same surface, so the pick only decides which NAME a reader
    turns on: the one later in `rendered` is taken, deterministically and for no deeper reason.
    """
    drawn = [str(s) for s in rendered if isinstance(s, str) and "/" in s]
    at = {s: i for i, s in enumerate(drawn)}
    outer = {s.split("/", 1)[0] for s in enclosing if isinstance(s, str) and "/" in s}
    out: dict[str, str] = {}
    for s in drawn:
        region, bare = s.split("/", 1)
        if not bare.startswith(region + "_to_"):
            continue
        other = bare[len(region) + 4:]
        mate = f"{other}/{other}_to_{region}"
        if mate not in at or mate in out or s in out:
            continue
        # the outer body's side, or - when that does not decide it - the later of the two
        far = (s if region in outer and other not in outer
               else mate if other in outer and region not in outer
               else (s if at[s] > at[mate] else mate))
        out[far] = mate if far == s else s
    return out


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i >= 0 else None


def _as_flag(v: Any) -> bool | None:
    """A declared yes/no. A `bool` AND NOTHING ELSE: a 1 or a "true" arriving here is a reader
    guessing at what an engine meant, and this group's one member decides whether the figure beside
    it is evidence or an intention."""
    return v if isinstance(v, bool) else None


def _as_text(v: Any) -> str | None:
    """A declared name. Stripped; blank is not a name and is dropped like an absent key."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _as_lines(v: Any) -> list[str] | None:
    """A declared list of findings, EMPTY INCLUDED - `[]` is a judgement that found nothing and
    `None` is no judgement. A list or a tuple only: a bare string here would be published as a list
    of its characters."""
    if not isinstance(v, (list, tuple)):
        return None
    return [t for t in (str(x).strip() for x in v) if t]


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN and infinity are not measurements
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def quality(manifest: dict | None) -> dict[str, Any]:
    man: dict = manifest or {}
    _q = man.get("quality")
    q: dict = _q if isinstance(_q, dict) else {}
    out: dict[str, Any] = {}
    for k in _INT_METRICS:
        iv = _as_int(q.get(k))
        if iv is not None:
            out[k] = iv
    for k in _FLOAT_METRICS:
        fv = _as_float(q.get(k))
        if fv is not None:
            out[k] = fv
    # EVERY GROUP IS TESTED AGAINST None AND NEVER FOR TRUTH. `0`, `0.0`, `False` and `[]` are all
    # readings - no faces in error, a flag that says the count was not measured, a judgement that
    # found no shortfall - and a truthiness filter here would delete exactly the four answers a
    # reader most needs, leaving the key absent, which this file's own rule says means "nobody
    # looked".
    for k in _FLAG_METRICS:
        bv = _as_flag(q.get(k))
        if bv is not None:
            out[k] = bv
    for k in _TEXT_METRICS:
        sv = _as_text(q.get(k))
        if sv is not None:
            out[k] = sv
    for k in _LINE_METRICS:
        lv = _as_lines(q.get(k))
        if lv is not None:
            out[k] = lv
    named = _region_rows(q)
    if named:                      # a multi-region engine lists its regions; the count is theirs
        out["regions"] = len(named)
    authoritative = _as_int(man.get("cell_count"))
    if authoritative:
        out["cells"] = authoritative
    # Not "if it looks like a string, keep it". A quality block describes a DELIVERED mesh, so an
    # absent or non-metre unit is refused rather than silently omitted - dropping the key left the
    # viewer showing dimensionless numbers, which is the same failure as showing the wrong unit.
    out["units"] = completed_mesh_unit(man).value
    engine = man.get("mesh_mode")
    if isinstance(engine, str) and engine.strip():
        out["engine"] = engine.strip()
    return out


def _part(pid: str, label: str, kind: str, source: str, *,
          visibility: str = VISIBLE, selectable: bool = True,
          renderable: bool = False, metadata: dict | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a mesh part kind")
    return {"id": pid, "label": label, "kind": kind, "source": source,
            "default_visibility": visibility, "selectable": selectable,
            "renderable": renderable, "metadata": metadata or {}}


def _region_rows(q: dict) -> list[dict]:
    """The named regions a multi-region engine lists in its quality block (`regions` as rows with
    a name, a type and a cell count), in its order; [] when it gives a count or nothing."""
    rows = q.get("regions")
    if not isinstance(rows, list):
        return []
    out, seen = [], set()
    for r in rows:
        name = str(r.get("name") or "").strip() if isinstance(r, dict) else ""
        if name and name not in seen:
            seen.add(name)
            out.append(r)
    return out


def parts(manifest: dict | None, *, rendered: tuple[str, ...] = (),
          enclosing: tuple[str, ...] = (), list_drawn: bool = False,
          poor: tuple[str, ...] = (), coincident: dict[str, str] | None = None,
          ) -> list[dict[str, Any]]:
    """The mesh parts the viewer lists. `rendered` names the surfaces the payload draws;
    `enclosing` those of them the engine says wrap everything else (they open hidden, like an
    enclosing role); `poor` those that are sets of cells that failed a check. `list_drawn` - a
    DELIVERED mesh - also lists every drawn surface no declared part names, so nothing the user
    sees is a surface they cannot pick.

    `coincident` maps a drawn surface onto the one it DUPLICATES - the far side of an interface
    the payload carries both sides of (`far_side_interfaces` derives it). Those open hidden, so
    the first sight of the mesh is one sheet per surface and not two fighting, and `coincident_with`
    on the part says which surface it is the other side of. It is a SEPARATE reason from
    `encloses_domain` because the two want different things once the part is shown: an enclosing
    wall comes back as a shell you can see through, where the far side of an interface is the
    surface a reader asked for and comes back solid.
    """
    man: dict = manifest or {}
    rendered_set = set(rendered)
    enclosing_set = set(enclosing)
    poor_set = set(poor)
    twin: dict[str, str] = dict(coincident or {})
    out: list[dict[str, Any]] = []

    # boundary patches, in manifest order
    _roles = man.get("patch_types")
    roles: dict = _roles if isinstance(_roles, dict) else {}
    _faces = man.get("patch_face_counts")
    faces: dict = _faces if isinstance(_faces, dict) else {}
    names: list[str] = []
    for src in (roles, faces):
        for n in src:
            if isinstance(n, str) and n and n not in names:
                names.append(n)
    # WHICH DRAWN SURFACES CARRY EACH DECLARED PATCH.
    # A multi-region engine draws "<region>/<patch>" because its regions own identically named
    # block faces and one would otherwise replace the other in this roster. The manifest keeps the
    # BARE name, deliberately: that is the name the user asked for at intake and the name their
    # boundary condition will carry. Without this map the two never met - the declared `ymin` was
    # listed un-renderable and `solid/ymin` was appended beside it as a separate part, so a
    # conjugate mesh offered 15 entries for 9 boundaries and picking the wrong one did nothing.
    # One declared patch may legitimately be drawn by SEVERAL surfaces (both regions reach ymin),
    # which is why this is a list and not a rename.
    drawn_by: dict[str, list[str]] = {}
    for drawn in rendered:
        if isinstance(drawn, str) and "/" in drawn:
            drawn_by.setdefault(drawn.split("/", 1)[1], []).append(drawn)

    for n in names:
        role = str(roles.get(n) or "").strip()
        bmeta: dict[str, Any] = {"name": n}
        if role:
            bmeta["role"] = role
        fc = _as_int(faces.get(n))
        if fc is not None:
            bmeta["faces"] = fc
        surfaces = drawn_by.get(n, [])
        if surfaces:
            bmeta["drawn_as"] = list(surfaces)
        # enclosing when the role says so, when the engine named this patch, or when every
        # surface that draws it is enclosing - a boundary half of which wraps the domain is not
        # one to open hidden
        encloses = (role.lower() in _ENCLOSING_ROLES or n in enclosing_set
                    or (bool(surfaces) and all(s in enclosing_set for s in surfaces)))
        if encloses:
            bmeta["encloses_domain"] = True
        # the far side of an interface drawn from both sides: every surface that draws this patch
        # is already on screen under another name, so it opens hidden
        far = bool(surfaces) and all(s in twin for s in surfaces)
        if far:
            bmeta["coincident_with"] = twin[surfaces[0]]
        out.append(_part(f"{BOUNDARY}:{n}", n, BOUNDARY, "patch_types",
                         visibility=HIDDEN if (encloses or far) else VISIBLE,
                         renderable=n in rendered_set or bool(surfaces), metadata=bmeta))

    # mesh regions
    _q2 = man.get("quality")
    q: dict = _q2 if isinstance(_q2, dict) else {}
    n_regions = _as_int(q.get("regions"))
    # a multi-region mesh NAMES its regions (the file's parts, its air spaces): each is a part
    # under its own name, drawn when the engine drew a surface for it
    for r in _region_rows(q):
        name = str(r["name"]).strip()
        nmeta: dict[str, Any] = {"name": name}
        rtype = str(r.get("type") or "").strip()
        if rtype:
            nmeta["role"] = rtype
        cells = _as_int(r.get("cells"))
        if cells is not None:
            nmeta["cells"] = cells
        # WHICH DRAWN SURFACES ARE THIS REGION'S. A multi-region engine draws "<region>/<patch>"
        # because its regions own identically named block faces, so a region's own surfaces are
        # exactly the ones under its prefix. Without this the test was `name in rendered_set` -
        # is a drawn surface called "fluid" - which is never true on the engine that has regions,
        # so every region was listed un-renderable and the inspector said the case ships no
        # geometry for it. "Show me the coolant on its own" is the question a reader of a
        # conjugate mesh always asks, and the roster offered it and could not answer it.
        #
        # A POOR-CELL SET IS NOT CLAIMED HERE. It wears the same prefix so the two regions' sets
        # stay apart, but it is a part in its own right below; binding it to the region would
        # take it out of `rendered` for that loop and the sixth reading with it.
        mine = [s for s in rendered if s not in poor_set and s.startswith(f"{name}/")]
        if mine:
            nmeta["drawn_as"] = mine
        encloses = name in enclosing_set
        if encloses:
            nmeta["encloses_domain"] = True
        out.append(_part(f"{REGION}:{name}", name, REGION, "quality.regions",
                         visibility=HIDDEN if encloses else VISIBLE,
                         renderable=name in rendered_set or bool(mine), metadata=nmeta))
    if n_regions:
        for i in range(n_regions):
            rmeta: dict[str, Any] = {"index": i}
            if n_regions == 1:
                # the whole mesh: its totals ARE the region's totals
                for k in ("cells", "faces"):
                    tv = _as_int(q.get(k))
                    if tv is not None:
                        rmeta[k] = tv
                cc = _as_int(man.get("cell_count"))
                if cc:
                    rmeta["cells"] = cc
            out.append(_part(f"{REGION}:{i}", f"region {i}", REGION, "quality.regions",
                             selectable=True, metadata=rmeta))

    # cell-type groups
    for k in ("hexahedra", "polyhedra", "prisms", "pyramids", "tetrahedra"):
        gv = _as_int(q.get(k))
        if gv:
            out.append(_part(f"{CELL_GROUP}:{k}", k, CELL_GROUP, f"quality.{k}",
                             metadata={"cells": gv, "shape": k}))

    # inspection structures the engine declared
    regions = man.get("inspection_regions")
    if isinstance(regions, list):
        for r in regions:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "").strip()
            if not name:
                continue
            imeta: dict[str, Any] = {"name": name}
            kind_ = str(r.get("kind") or "").strip()
            if kind_:
                imeta["structure"] = kind_
            for key in ("normal", "origin"):
                seq = r.get(key)
                if isinstance(seq, list) and all(isinstance(x, (int, float)) for x in seq):
                    imeta[key] = [float(x) for x in seq]
            out.append(_part(f"{INSPECTION}:{name}", name, INSPECTION,
                             "inspection_regions", metadata=imeta))

    # a drawn surface no declared part names is still a part: shown and unpickable it is a wall
    # the user cannot move out of the way (an engine's region surface the manifest never listed)
    if list_drawn:
        named = {p["metadata"].get("name") for p in out if p["renderable"]}
        # a surface already bound to a declared patch above is that patch, not a second part.
        # A BOUNDARY'S CLAIM ONLY. A region also names the surfaces that draw it, and that is a
        # different relationship: the declared `ymin` and the drawn `solid/ymin` are one part
        # under two names, while the metal region merely contains its six sides and the coolant
        # contains its interface. Counting a region's claim here deleted `fluid/fluid_to_solid`
        # from the roster - the one surface on a conjugate mesh that matters most and the one the
        # manifest can never declare, because it does not exist until the block is cut.
        bound = {s for p in out if p["kind"] == BOUNDARY
                 for s in (p["metadata"].get("drawn_as") or ())}
        for n in rendered:
            if not isinstance(n, str) or not n or n in named or n in bound:
                continue
            named.add(n)
            dmeta: dict[str, Any] = {"name": n}
            # a poor-cell set is never an enclosing surface and never opens hidden: it exists to
            # be seen, and a reader who has to find it in a list to turn it on has not been shown
            # anything
            if n in poor_set:
                out.append(_part(f"{POOR_CELLS}:{n}", n, POOR_CELLS, "surface",
                                 renderable=True, metadata=dmeta))
                continue
            encloses = n in enclosing_set
            if encloses:
                dmeta["encloses_domain"] = True
            far = n in twin
            if far:
                dmeta["coincident_with"] = twin[n]
            out.append(_part(f"{BOUNDARY}:{n}", n, BOUNDARY, "surface",
                             visibility=HIDDEN if (encloses or far) else VISIBLE,
                             renderable=True, metadata=dmeta))

    return out
