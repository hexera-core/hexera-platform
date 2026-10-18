# Responsibility: Say what an approved patch declaration asks a case writer to name - the body walls, the far field, the symmetry planes, the ground - and stage a surface's named parts under those names.
# Boundaries: pure functions over declared patches and triangle groups; it writes no file and knows no engine.
# Collaborates with: engines/ground_plane.py, contracts/patch_names.py, and the snappy and cfMesh case writers.
"""What the declaration asks the case writer to name.

Every case writer used to read the approved patches its own way: snappy's box wrote its far field as
`farfield` whatever the user had called it, a surface's named solids were written under the CAD
file's spellings (or `<surface>_<solid>`) rather than the declared ones, and a single declared wall
on a multi-solid file came out as several patches none of which carried the approved name. Each of
those meshed for the whole run and then failed the manifest check on a patch with zero faces.

The names come from here instead, once, for every writer.
"""
from __future__ import annotations

from meshpipeline.engines.ground_plane import body_walls as _body_walls
from meshpipeline.engines.ground_plane import ground_patch_name

#: The far field's name when the declaration names none (a direct, contract-less dispatch).
DEFAULT_FARFIELD = "farfield"


def _name_role(p) -> tuple[str, str]:
    if isinstance(p, dict):
        return (str(p.get("name") or "").strip(),
                str(p.get("type") or p.get("role") or "").strip())
    return (str(getattr(p, "name", "") or "").strip(), str(getattr(p, "type", "") or "").strip())


def body_walls(patches) -> list[str]:
    """The declared walls the GEOMETRY supplies: every wall except the ground the domain lays."""
    return _body_walls(patches)


def farfield_name(patches, default: str = DEFAULT_FARFIELD) -> str:
    """The declared far-field patch's name - the one name a far-field box is written under."""
    for p in patches or ():
        name, role = _name_role(p)
        if role == "farfield" and name:
            return name
    return default


def symmetry_names(patches) -> list[str]:
    return [n for n, r in map(_name_role, patches or ()) if r == "symmetry" and n]


def ground_name(patches) -> str | None:
    return ground_patch_name(patches)


def stage_regions(regions: dict, walls: list[str]) -> dict:
    """A surface's named parts, staged under the DECLARED wall names.

    `regions` maps each named solid of the input surface to its triangles. With one declared body
    wall (or none) the parts are one wall - the user approved one - so nothing is kept apart and
    the caller writes a single solid under that wall's name ({} is returned). With several, each
    part is written under the declared wall it matches (mesh-safe and without regard to capitals,
    the rule admission matched them by), parts that match the same wall are merged, and a part
    that matches none keeps its own mesh-safe name - the pre-flight then names it before any mesh
    runs, rather than the manifest check after one."""
    from meshpipeline.contracts.patch_names import mesh_safe, same_patch_name

    if len(regions or {}) <= 1 or len(walls or []) <= 1:
        return {}
    out: dict = {}
    for name, tris in regions.items():
        target = next((w for w in walls if same_patch_name(w, name)), None) or mesh_safe(name)
        out.setdefault(target, [])
        out[target] = list(out[target]) + list(tris)
    return out


__all__ = ["DEFAULT_FARFIELD", "body_walls", "farfield_name", "ground_name", "stage_regions",
           "symmetry_names"]
