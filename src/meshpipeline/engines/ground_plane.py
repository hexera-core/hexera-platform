# Responsibility: Say, in one place, what a ground plane is: the floor of the far-field box, made
# by the domain as a wall patch named "ground" - never a region of the geometry.
# Boundaries: pure helpers over declared patches and a manifest's patch roles. It meshes nothing,
# reads no file and knows no engine.
# Collaborates with: engines/base.py (the admission gate), agents/intake/validation.py, the snappy
# domain builder, and the extent gate - each asks here instead of spelling the rule again.
from __future__ import annotations

#: The name the domain's floor carries. In an external flow this name is the ground plane, so the
#: geometry check tells the intake to declare it and every box builder reads it from here.
GROUND_PATCH = "ground"
#: The role it plays: a car on a road has a wall under it, not open air.
GROUND_ROLE = "wall"
#: The axis the ground lies across when nothing says which way is up: a file drawn +z up, whose
#: floor is the box's z-min face.
VERTICAL_AXIS = 2
#: The rule the floor's place answers to (docs/RULES.md).
FLOOR_RULE = "INS-9"
#: The floor of a +z-up file: (axis, side) of the far-field box face the ground is.
DEFAULT_FLOOR = (VERTICAL_AXIS, "min")


def floor_of(up_axis) -> tuple[int, str]:
    """THE GROUND FOLLOWS THE UP AXIS (INS-9). The far-field box face under a body on the ground,
    as (axis, side), for the up axis the user confirmed on the stage: +z up lays it on z-min, +y up
    on y-min, -y up on y-max. A CAD export drawn Y-up is as valid as a Z-up one, and a flow along z
    over a Y-up floor is a flow along the ground. Anything unreadable is +z up, as before."""
    text = str(up_axis or "").strip().lower()
    if len(text) == 2 and text[0] in "+-" and text[1] in "xyz":
        return "xyz".index(text[1]), ("min" if text[0] == "+" else "max")
    return DEFAULT_FLOOR


def up_word(floor: tuple[int, str]) -> str:
    """The up axis a floor stands for, as the stage writes it: (1, "max") is "-y"."""
    return ("+" if floor[1] == "min" else "-") + "xyz"[int(floor[0])]


def manifest_floor(manifest: dict | None) -> tuple[int, str]:
    """The floor face a built mesh's manifest records (the snappy renderer writes it); a manifest
    from before the floor followed the up axis is +z up."""
    rec = (manifest or {}).get("ground_floor") or {}
    try:
        ax, side = int(rec["axis"]), str(rec["side"])
        if ax in (0, 1, 2) and side in ("min", "max"):
            return ax, side
    except (KeyError, TypeError, ValueError):
        pass
    return DEFAULT_FLOOR


def to_up_frame(points, floor: tuple[int, str]):
    """Points (..., 3) in a frame whose +z is up for this floor: the up axis moved to z and, for a
    floor on the max side, turned over - so code that measures a body on a z-min floor measures
    it on any floor. A right-handed swap of axes; `from_up_frame` undoes it exactly."""
    import numpy as np

    p = np.asarray(points, dtype=float)
    ax, side = int(floor[0]), floor[1]
    order = [(ax + 1) % 3, (ax + 2) % 3, ax]
    out = p[..., order].copy()
    if side == "max":
        out[..., 2] *= -1.0
        out[..., 0] *= -1.0                     # keep it a rotation, not a mirror
    return out


def from_up_frame(points, floor: tuple[int, str]):
    import numpy as np

    p = np.asarray(points, dtype=float).copy()
    ax, side = int(floor[0]), floor[1]
    if side == "max":
        p[..., 2] *= -1.0
        p[..., 0] *= -1.0
    out = np.empty_like(p)
    out[..., (ax + 1) % 3], out[..., (ax + 2) % 3], out[..., ax] = p[..., 0], p[..., 1], p[..., 2]
    return out


def _name_role(p) -> tuple[str, str]:
    if isinstance(p, dict):
        return (str(p.get("name") or "").strip(),
                str(p.get("type") or p.get("role") or "").strip())
    return (str(getattr(p, "name", "") or "").strip(),
            str(getattr(p, "type", "") or "").strip())


def is_ground(name, role) -> bool:
    """A declared patch is the ground plane when it is a wall named ground (any capitalisation)."""
    return (str(name or "").strip().casefold() == GROUND_PATCH
            and str(role or "").strip().lower() == GROUND_ROLE)


def ground_patch_name(patches) -> str | None:
    """The declared ground patch under the user's own spelling, or None when there is none.
    Accepts intake dicts ({name, type}) and admission PatchSummary rows alike."""
    for p in patches or ():
        name, role = _name_role(p)
        if is_ground(name, role):
            return name
    return None


def ground_rule():
    """The rule the intake applies where a declaration enters, for an EXTERNAL flow: a wall named
    or typed with any ground word ("ground plane", "floor", "road") is this floor, and carries its
    one name - so no layer has to recognise a synonym, and "ground plane" never becomes a second
    body wall with zero faces."""
    from meshpipeline.contracts.patch_names import GroundRule
    return GroundRule(name=GROUND_PATCH)


def is_ground_word(name) -> bool:
    """Whether a name is one of the words people use for the ground (ground, floor, road ...)."""
    from meshpipeline.contracts.patch_names import is_ground_word as _word
    return _word(name)


def body_walls(patches) -> list[str]:
    """The declared wall patches the GEOMETRY has to supply: every wall except the ground, which
    the domain builds. This is the count the wall-arity rule measures against the file's regions."""
    out: list[str] = []
    for p in patches or ():
        name, role = _name_role(p)
        if role == "wall" and not is_ground(name, role):
            out.append(name)
    return out


def manifest_is_grounded(manifest: dict | None) -> bool:
    """Whether a built mesh carries a ground plane: an external mesh whose patch roles include
    the ground wall. Read from the manifest, so a gate judges the mesh that was actually built."""
    m = manifest or {}
    if str(m.get("flow_topology") or "") == "internal":
        return False
    return any(is_ground(n, r) for n, r in (m.get("patch_types") or {}).items())


__all__ = ["DEFAULT_FLOOR", "FLOOR_RULE", "GROUND_PATCH", "GROUND_ROLE", "VERTICAL_AXIS", "body_walls", "floor_of", "from_up_frame", "ground_patch_name", "manifest_floor", "to_up_frame", "up_word",
           "ground_rule", "is_ground", "is_ground_word", "manifest_is_grounded"]
