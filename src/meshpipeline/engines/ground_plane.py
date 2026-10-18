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
#: The axis the ground lies across. The geometry check calls a part "on the ground" when a flat
#: face lies at its lowest z (cad/scout_mesh.py), so the floor is the box's z-min face.
VERTICAL_AXIS = 2


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


__all__ = ["GROUND_PATCH", "GROUND_ROLE", "VERTICAL_AXIS", "body_walls", "ground_patch_name",
           "ground_rule", "is_ground", "is_ground_word", "manifest_is_grounded"]
