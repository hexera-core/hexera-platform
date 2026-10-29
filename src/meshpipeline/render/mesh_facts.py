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
)
_FLOAT_METRICS: Final[tuple[str, ...]] = (
    "max_non_ortho", "max_skewness", "avg_non_ortho", "skew_fraction",
    # the size RANGE, as lengths. On a two-scale part the ratio is how many octree levels the
    # delivered mesh actually spans, which is the number its budget was set by.
    "min_cell_m", "max_cell_m", "cell_size_ratio", "max_aspect_ratio",
)

# Part kinds. Closed: the viewer groups by these, so a new kind is a deliberate
# addition on both sides rather than a string that appears one day.
BOUNDARY: Final = "boundary"
REGION: Final = "region"
CELL_GROUP: Final = "cellGroup"
INSPECTION: Final = "inspection"
KINDS: Final[frozenset[str]] = frozenset({BOUNDARY, REGION, CELL_GROUP, INSPECTION})

VISIBLE: Final = "visible"
HIDDEN: Final = "hidden"

# Boundary ROLES that enclose the model. Shown solid they are a wall of colour
# with the body trapped inside, so they open hidden. Role-driven, never
# name-driven: a patch called "farfield" that is a wall is a wall.
_ENCLOSING_ROLES: Final[frozenset[str]] = frozenset({
    "farfield", "freestream", "external", "atmosphere",
})


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
          enclosing: tuple[str, ...] = (), list_drawn: bool = False) -> list[dict[str, Any]]:
    """The mesh parts the viewer lists. `rendered` names the surfaces the payload draws;
    `enclosing` those of them the engine says wrap everything else (they open hidden, like an
    enclosing role). `list_drawn` - a DELIVERED mesh - also lists every drawn surface no
    declared part names, so nothing the user sees is a surface they cannot pick."""
    man: dict = manifest or {}
    rendered_set = set(rendered)
    enclosing_set = set(enclosing)
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
        out.append(_part(f"{BOUNDARY}:{n}", n, BOUNDARY, "patch_types",
                         visibility=HIDDEN if encloses else VISIBLE,
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
        encloses = name in enclosing_set
        if encloses:
            nmeta["encloses_domain"] = True
        out.append(_part(f"{REGION}:{name}", name, REGION, "quality.regions",
                         visibility=HIDDEN if encloses else VISIBLE,
                         renderable=name in rendered_set, metadata=nmeta))
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
        # a surface already bound to a declared patch above is that patch, not a second part
        bound = {s for p in out for s in (p["metadata"].get("drawn_as") or ())}
        for n in rendered:
            if not isinstance(n, str) or not n or n in named or n in bound:
                continue
            named.add(n)
            dmeta: dict[str, Any] = {"name": n}
            encloses = n in enclosing_set
            if encloses:
                dmeta["encloses_domain"] = True
            out.append(_part(f"{BOUNDARY}:{n}", n, BOUNDARY, "surface",
                             visibility=HIDDEN if encloses else VISIBLE,
                             renderable=True, metadata=dmeta))

    return out
