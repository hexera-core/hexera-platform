# Responsibility: Name, in one place, every fact the geometry check can fill in for the user - what
# it is called, what kind of answer it takes, when it applies, and how it reads once confirmed -
# so the model's answer form, the confirm endpoint, the console's form and the intake's
# declaration cannot drift apart.
# Boundaries: a registry and pure helpers. It reads no file, calls no model and stores nothing.
from __future__ import annotations

from dataclasses import dataclass, field

AXES = ("+x", "-x", "+y", "-y", "+z", "-z")
#: WHICH WAY IS UP: the axis of the part that points to the sky in the real world, in the order
#: the console lists them. +z is what CAD means by up and what a part is shown with when nothing
#: says otherwise; a car drawn upside down is "-z". It turns the views, never the file.
UP_AXES = ("+z", "-z", "+y", "-y", "+x", "-x")
DEFAULT_UP = "+z"
#: Far-field margins in body lengths when nobody has said otherwise: a bluff body's wake needs
#: room behind it, the rest a little less.
DEFAULT_EXTENTS = {"upstream": 5.0, "downstream": 10.0, "lateral": 5.0, "vertical": 5.0}


@dataclass(frozen=True)
class GeometryField:
    key: str
    label: str
    kind: str                      # select | number | bool | text | openings | point | size
    applies: str = "both"          # internal | external | both
    options: tuple = ()            # (value, label) pairs for a select
    editable: bool = True
    help: str = ""


#: THE REGISTRY. Order is display order. Adding a fact the check can fill in starts here; the
#: tests hold the confirm model and the console form to this list.
FIELDS: tuple[GeometryField, ...] = (
    GeometryField("part", "The part", "text", help="what the part is, in a few words"),
    GeometryField("input_kind", "The file is", "select", options=(
        ("body-surface", "the part's wall, hollow inside for the fluid"),
        ("fluid-domain", "the fluid volume itself"),
        ("solid-body", "a solid body the fluid flows around"))),
    GeometryField("flow", "The fluid flows", "select", options=(
        ("internal", "through the part"), ("external", "around the part"))),
    GeometryField("up_axis", "Up", "select", options=tuple((a, a.upper()) for a in UP_AXES),
                  help="the axis of the part that points up in the real world; the 3D views "
                       "open with it"),
    GeometryField("openings", "Openings", "openings", applies="internal",
                  help="one row per sticker: name and role; size and position are measured"),
    GeometryField("seed_point_mm", "A point inside the flow", "point", applies="internal", editable=False),
    GeometryField("flow_axis", "The fluid travels along", "select", applies="external",
                  options=tuple((a, a) for a in AXES) + (("unknown", "not sure"),),
                  help="the direction the flow moves past the part"),
    GeometryField("reference_length_mm", "Reference length (mm)", "number", applies="external",
                  help="the part's length along the flow; the margins below are multiples of it"),
    GeometryField("extents", "Far-field margins (body lengths)", "extents", applies="external",
                  help="upstream, downstream, to each side, above"),
    GeometryField("grounded", "The part stands on the ground", "bool", applies="external"),
    GeometryField("size_mm", "Overall size", "size", editable=False),
)

FIELD_KEYS = tuple(f.key for f in FIELDS)
EXTERNAL_KEYS = tuple(f.key for f in FIELDS if f.applies == "external")


def form_spec() -> list[dict]:
    """The registry as the console reads it: one entry per field, in display order."""
    return [{"key": f.key, "label": f.label, "kind": f.kind, "applies": f.applies,
             "options": [list(o) for o in f.options], "editable": f.editable, "help": f.help}
            for f in FIELDS]


def up_or_default(up_axis) -> str:
    """The up axis when it is one of the six, else +z."""
    return up_axis if up_axis in UP_AXES else DEFAULT_UP


def axis_of_longest_side(size_mm, up_axis: str = DEFAULT_UP) -> str:
    """The code's guess at where the flow goes for a body in a flow: along its longest horizontal
    side - the longer of the two axes across the one that points up - positive sign; the sign is
    for the pictures and the user to settle."""
    up = "xyz".index(up_or_default(up_axis)[1])
    k = max((i for i in range(3) if i != up), key=lambda i: float(size_mm[i]))   # ties: the first
    return "+" + "xyz"[k]


def length_along(size_mm, axis: str) -> float:
    idx = {"x": 0, "y": 1, "z": 2}.get(axis[-1:], 0)
    return float(size_mm[idx])


def external_defaults(facts: dict, flow_axis: str | None, up_axis: str = DEFAULT_UP) -> dict:
    """The external-flow facts a proposal carries, whatever the flow turns out to be: the axis the
    model chose when it is one of the six, else the code's guess across the up axis; the reference
    length along it; the default margins; whether the part stands on the ground when the
    measuring step could tell. The measuring step looks for the ground under the file's lowest z,
    so on a part drawn another way up it has nothing to say. Internal cases carry them too,
    unused, so the form never meets a missing key."""
    size = facts.get("size_mm") or [0.0, 0.0, 0.0]
    upright = up_or_default(up_axis) == DEFAULT_UP
    guess = (facts.get("flow_axis_guess") if upright else None) or axis_of_longest_side(size, up_axis)
    axis = flow_axis if flow_axis in AXES else guess
    return {
        "flow_axis": axis,
        "flow_axis_guessed": flow_axis not in AXES,
        "reference_length_mm": round(length_along(size, axis), 2),
        "extents": dict(DEFAULT_EXTENTS),
        "grounded": bool(facts.get("grounded", False)) and upright,
    }


#: WHY A GROUNDED PART DRAWN ANOTHER WAY UP IS MESHED FREE IN THE FLOW. The mesher lays the ground
#: under the file's lowest z (engines/ground_plane.VERTICAL_AXIS); on a car drawn upside down that
#: is its roof. Until the ground follows the up axis, the part is meshed without one - never with
#: the ground on its roof - and the user is told how to get it.
GROUND_NEEDS_Z_UP = (
    "The part stands on the ground, but this file is drawn with {up} up, and the ground plane can "
    "only be laid under the file's lowest z, which on this file is not under the part. So it is "
    "meshed free in the flow: every far-field face is open, and no ground patch is declared. Tell "
    "the user this, and that re-exporting the file with +z up lets it stand on the ground.")


def external_declaration(body) -> list[str]:
    """The sentences the intake reads for a body in a flow, from what the user confirmed."""
    axis = getattr(body, "flow_axis", None) or ""
    if axis not in AXES:
        return ["The flow direction was not settled on the picture; ask for it."]
    lines = [f"The fluid travels along {axis}."]
    ref = getattr(body, "reference_length_mm", None)
    ext = getattr(body, "extents", None) or {}
    if ref:
        lines.append(f"Reference length: {float(ref):.0f} mm along the flow.")
    if ext:
        lines.append("Far-field margins in reference lengths: "
                     f"{float(ext.get('upstream', 0)):g} upstream, {float(ext.get('downstream', 0)):g} downstream, "
                     f"{float(ext.get('lateral', 0)):g} to each side, {float(ext.get('vertical', 0)):g} above.")
    up = up_or_default(getattr(body, "up_axis", None))
    if not getattr(body, "grounded", False):
        lines.append("The part is free in the flow; every far-field face is open.")
    elif up != DEFAULT_UP:
        lines.append(GROUND_NEEDS_Z_UP.format(up=up))
    else:
        lines.append("The part stands on the ground: the floor of the far-field box, at the part's "
                     "lowest z, is a wall patch named ground, which the domain builds - it is not a "
                     "region of the geometry.")
    return lines


__all__ = ["AXES", "DEFAULT_EXTENTS", "DEFAULT_UP", "EXTERNAL_KEYS", "FIELD_KEYS", "FIELDS",
           "GROUND_NEEDS_Z_UP", "GeometryField", "UP_AXES", "axis_of_longest_side",
           "external_declaration", "external_defaults", "form_spec", "length_along", "up_or_default"]
_ = field  # dataclasses.field is imported for future registry entries with defaults
