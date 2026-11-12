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
    # engine computes it, writes it to the manifest and the answer is a deliberate zero with the
    # reason attached (see cfmesh_conjugate/finalize.NEAR_WALL_LAYERS). The reason is that a prism
    # stack does not FIT across a passage this narrow - it takes the quality to checkMesh's bar and
    # buys nothing, because across such a channel the cells across it ARE the near-wall resolution.
    # It is NOT that layers would break conformality: that was measured on the coupon and all
    # 14,477 interface faces stayed matched to 4e-18 m. The graded levels and the cell size at the
    # wall are what stands in its place, so all three travel together or none of them means
    # anything.
    "near_wall_layers", "near_wall_levels",
    # and the conformality itself, which is the number that says the interface is real: the same
    # face count on each side of it
    "interface_faces_fluid", "interface_faces_solid",
)
_FLOAT_METRICS: Final[tuple[str, ...]] = (
    "max_non_ortho", "max_skewness", "avg_non_ortho", "skew_fraction",
    # the size RANGE, as lengths. On a two-scale part the ratio is how many octree levels the
    # delivered mesh actually spans, which is the number its budget was set by.
    "min_cell_m", "max_cell_m", "cell_size_ratio", "max_aspect_ratio",
    # the cell AT the wall, which is the near-wall resolution a reader asking about layers wants
    "near_wall_cell_m",
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
