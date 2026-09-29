# Responsibility: Turn what the user confirmed on the geometry check into what the intake reads -
# the declaration message in the conversation and the session's declared patches - and re-read a
# stored confirmation in another unit when the user changes the file's unit afterwards.
# Owns: the declaration's words (and the mark the intake prompt names), the patch shape the port
# binding reads, and the one rule for re-reading a confirmation's lengths.
# Boundaries: pure. It takes the confirmation as the route validated it (or as it was stored) and
# returns words, patches and records; it stores nothing and asks nothing.
# Collaborates with: api/v1/geometry.py (the confirm route), application/unit_change.py (a unit
# changed later, in the chat or on the stage).
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

#: How the confirmation announces itself in the conversation; the intake prompt block names it.
CONFIRMED_MARK = "GEOMETRY CHECK (confirmed by the user):"


def unit_sentence(body) -> str:
    """The file's unit as the user confirmed it, and the part's length in it - so the intake is
    told the scale in words instead of working it out from millimetres. Empty when the
    confirmation carries no unit."""
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.contracts.unit_plausibility import UNIT_WORDS, length_words

    raw = getattr(body, "unit", None)
    try:
        unit = LengthUnit(raw) if raw else None
    except ValueError:
        unit = None
    if unit is None:
        return ""
    size = getattr(body, "size_mm", None)
    longest = max((float(v) for v in size), default=0.0) if size and len(size) == 3 else 0.0
    if not longest > 0:
        return f"The file is in {UNIT_WORDS[unit]}."
    return (f"The file is in {UNIT_WORDS[unit]}: the part is {length_words(longest / 1000.0)} long. "
            "The sizes above are already converted to millimetres; use them as they stand.")


def confirmation_message(body) -> str:
    """The sentence the intake reads: everything the user confirmed, in plain words, with the
    numbers the builder needs. Its opening phrase is the one the intake prompt block names."""
    kind = {"body-surface": "the part's wall, hollow inside for the fluid",
            "fluid-domain": "the fluid volume itself",
            "solid-body": "a solid body"}[body.input_kind]
    through = "through it" if body.flow == "internal" else "around it"
    # The kind is stated verbatim, in the intake's own enum, and it is the same word the session
    # stores. A solid body the fluid flows around stays "solid-body": the engine gate reads that as
    # a body surface for any fluid purpose (engines/purposes.kinds_admitted_as), and a later
    # structural request on the same solid still finds the kind gmsh needs.
    parts = [f"{CONFIRMED_MARK} the file is {kind}"
             + (f" ({body.part})" if body.part else "")
             + f", input_kind {body.input_kind}; the fluid flows {through}."]
    ports = [o for o in body.openings if o.role != "not_an_opening"]
    if body.flow == "external":
        from meshpipeline.contracts.geometry_fields import external_declaration
        parts.append("No openings: the fluid flows around the whole body.")
        parts.extend(external_declaration(body))
    elif ports:
        rows = []
        for o in ports:
            size = (f"{o.diameter_mm:.0f} mm across" if o.diameter_mm
                    else f"{o.width_mm:.0f} x {o.height_mm:.0f} mm" if o.width_mm and o.height_mm else "")
            at = (f" at ({o.centroid_mm[0]:.0f}, {o.centroid_mm[1]:.0f}, {o.centroid_mm[2]:.0f}) mm"
                  if o.centroid_mm and len(o.centroid_mm) == 3 else "")
            rows.append(f"{o.name} ({o.role}){', ' + size if size else ''}{at}")
        parts.append("Openings: " + "; ".join(rows) + ".")
        skipped = [str(o.id) for o in body.openings if o.role == "not_an_opening"]
        if skipped:
            parts.append(f"Sticker{'s' if len(skipped) > 1 else ''} {', '.join(skipped)}: not an opening (a hole or a face the fluid does not pass).")
    else:
        # the fluid flows through the part but no port was confirmed: the intake must ask
        parts.append("No openings were confirmed on the picture"
                     + (" (every sticker was marked not an opening)" if body.openings else "")
                     + "; ask the user where the fluid enters and leaves.")
    if body.seed_point_mm and len(body.seed_point_mm) == 3:
        p = body.seed_point_mm
        parts.append(f"A point inside the flow: ({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f}) mm.")
    if body.size_mm and len(body.size_mm) == 3:
        s = body.size_mm
        parts.append(f"Part size: {s[0]:.0f} x {s[1]:.0f} x {s[2]:.0f} mm.")
    said = unit_sentence(body)
    if said:
        parts.append(said)
    return " ".join(parts)


def _is_declaration(m: dict) -> bool:
    return m.get("role") == "assistant" and str(m.get("content", "")).startswith(CONFIRMED_MARK)


def with_declaration(messages: list[dict] | None, message: str) -> list[dict]:
    """The conversation with this confirmation as its only one: an earlier confirmation is
    replaced, not joined, so a retry or a change of mind leaves one declaration for the intake."""
    kept = [m for m in (messages or []) if not _is_declaration(m)]
    return [*kept, {"role": "assistant", "content": message}]


def replace_declaration(messages: list[dict] | None, message: str) -> list[dict]:
    """The conversation with its declaration re-worded WHERE IT STANDS - for a unit changed after
    the confirmation, whose sizes are re-read but whose place in the conversation is unchanged.
    Appended when there was none."""
    out, done = [], False
    for m in messages or []:
        if _is_declaration(m):
            if not done:
                out.append({"role": "assistant", "content": message})
                done = True
            continue
        out.append(m)
    return out if done else [*out, {"role": "assistant", "content": message}]


def patches_from(body) -> list[dict]:
    """The session's declared patches, in the shape the intake's submit tool already takes:
    name and role, plus the size and location fields the port binding reads."""
    patches: list[dict] = []
    if body.flow == "external":
        return patches
    for o in body.openings:
        if o.role == "not_an_opening":
            continue
        entry: dict = {"name": o.name, "type": o.role}
        if o.diameter_mm:
            entry["diameter_mm"] = float(o.diameter_mm)
        elif o.width_mm and o.height_mm:
            entry["width_mm"], entry["height_mm"] = float(o.width_mm), float(o.height_mm)
        if o.centroid_mm and len(o.centroid_mm) == 3:
            entry["near_mm"] = [float(v) for v in o.centroid_mm]
        patches.append(entry)
    if body.flow == "internal" and patches:
        patches.append({"name": "wall", "type": "wall"})
    return patches


#: The fields of a stored confirmation that are not the user's body: when it was stored, by whom,
#: and what was derived from it.
_RECORD_ONLY = ("confirmed_at", "owner_id", "message", "patches", "reread")
_OPENING_DEFAULTS: dict[str, object] = {"centroid_mm": None, "diameter_mm": None, "width_mm": None,
                                         "height_mm": None}
_BODY_DEFAULTS: dict[str, object] = {"openings": [], "seed_point_mm": None, "size_mm": None, "part": "",
                                     "flow_axis": None, "reference_length_mm": None, "extents": None,
                                     "grounded": False, "scale_to_m": None, "unit": None}


def body_of(record: dict) -> SimpleNamespace:
    """A stored confirmation as the body the words and patches are made from."""
    plain = {k: v for k, v in record.items() if k not in _RECORD_ONLY}
    body: dict[str, Any] = {**_BODY_DEFAULTS, **plain}
    body["openings"] = [SimpleNamespace(**{**_OPENING_DEFAULTS, **dict(o)}) for o in body.get("openings") or []]
    return SimpleNamespace(**body)


def reread_record(record: dict, *, scale_to_metres: float, unit: str,
                  confirmed_scale: float | None = None) -> dict | None:
    """A stored confirmation re-read in another unit: every length scaled by the ratio of the new
    scale to the one it was confirmed under (areas by its square; far-field margins are body
    lengths and stay), the words and patches made again from the result. The scale is the
    record's own; `confirmed_scale` stands in for a record stored without one (the unit in force
    when it was confirmed). None when neither says, so nothing can be re-read safely."""
    from meshpipeline.application.geometry_check import rescaled_lengths

    was = record.get("scale_to_m") or confirmed_scale
    try:
        was_f = float(was) if was is not None else 0.0
    except (TypeError, ValueError):
        return None
    if not was_f > 0:
        return None
    k = float(scale_to_metres) / was_f
    plain = {key: v for key, v in record.items() if key not in _RECORD_ONLY and key != "scale_to_m"}
    reread = rescaled_lengths(plain, k) if abs(k - 1.0) > 1e-12 else dict(plain)
    reread.update(scale_to_m=float(scale_to_metres), unit=unit)
    body = body_of(reread)
    return {**{key: record[key] for key in ("confirmed_at", "owner_id") if key in record},
            **reread, "message": confirmation_message(body), "patches": patches_from(body),
            "reread": {"from_scale_to_m": was_f, "factor": k}}


#: The lengths a confirmation carries, dropped when they cannot be re-read in a new unit.
_OPENING_LENGTHS = ("centroid_mm", "diameter_mm", "width_mm", "height_mm")
_BODY_LENGTHS = ("seed_point_mm", "size_mm", "reference_length_mm")


def sizeless_record(record: dict, *, scale_to_metres: float, unit: str) -> dict:
    """A stored confirmation with every length taken out and the rest kept - the kind, the flow,
    each opening's name and role, the flow axis, the far-field margins (body lengths, unit-free).
    For a confirmation that does not say what unit its sizes are in: rather than refuse a change
    of unit, or carry sizes in a unit nobody can name, the sizes go and the intake asks for them
    in the new unit."""
    plain = {key: v for key, v in record.items() if key not in _RECORD_ONLY and key not in _BODY_LENGTHS}
    plain["openings"] = [{k: v for k, v in dict(o).items() if k not in _OPENING_LENGTHS}
                         for o in plain.get("openings") or []]
    plain.update(scale_to_m=float(scale_to_metres), unit=unit)
    body = body_of(plain)
    message = confirmation_message(body)
    message += (" The sizes confirmed on the picture could not be carried over to the new unit: ask "
                "the user for each opening's size and the reference length, in that unit.")
    return {**{key: record[key] for key in ("confirmed_at", "owner_id") if key in record},
            **plain, "message": message, "patches": patches_from(body), "reread": {"sizes_dropped": True}}


__all__ = ["CONFIRMED_MARK", "body_of", "confirmation_message", "patches_from", "replace_declaration",
           "reread_record", "sizeless_record", "unit_sentence", "with_declaration"]
