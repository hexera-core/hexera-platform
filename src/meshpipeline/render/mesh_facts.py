# Responsibility: Say what the delivered mesh IS, read from the engine's own manifest.
# Boundaries: a reader over declared facts; it opens no mesh and measures nothing itself.
from __future__ import annotations

from typing import Any, Final

from meshpipeline.contracts.mesh_units import completed_mesh_unit

# The quality figures a delivered mesh may declare, and the type each must be.
# Engines that do not compute one simply omit it.
_INT_METRICS: Final[tuple[str, ...]] = (
    "cells", "faces", "hexahedra", "polyhedra", "prisms", "pyramids",
    "tetrahedra", "regions", "skew_faces",
)
_FLOAT_METRICS: Final[tuple[str, ...]] = (
    "max_non_ortho", "max_skewness", "avg_non_ortho", "skew_fraction",
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


def parts(manifest: dict | None, *, rendered: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    man: dict = manifest or {}
    rendered_set = set(rendered)
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
    for n in names:
        role = str(roles.get(n) or "").strip()
        bmeta: dict[str, Any] = {"name": n}
        if role:
            bmeta["role"] = role
        fc = _as_int(faces.get(n))
        if fc is not None:
            bmeta["faces"] = fc
        encloses = role.lower() in _ENCLOSING_ROLES
        if encloses:
            bmeta["encloses_domain"] = True
        out.append(_part(f"{BOUNDARY}:{n}", n, BOUNDARY, "patch_types",
                         visibility=HIDDEN if encloses else VISIBLE,
                         renderable=n in rendered_set, metadata=bmeta))

    # mesh regions
    _q2 = man.get("quality")
    q: dict = _q2 if isinstance(_q2, dict) else {}
    n_regions = _as_int(q.get("regions"))
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

    return out
