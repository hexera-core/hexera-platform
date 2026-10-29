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

import math
import re
from types import SimpleNamespace
from typing import Any

#: How the confirmation announces itself in the conversation; the intake prompt block names it.
CONFIRMED_MARK = "GEOMETRY CHECK (confirmed by the user):"
#: How the declaration says why the openings have their roles, when the uploaded file declares them;
#: the brief reads it back from here (application/intake_brief).
ROLES_MARK = "Roles as the file declares them:"
_ROLES_RE = re.compile(re.escape(ROLES_MARK) + r" ([^.]+)\.")


def declared_reason_kept(facts: dict, openings) -> str:
    """The check's reason for its openings (geometry_check.declared_openings_reason), when the user
    confirmed the file's openings with the file's roles - '' when the file declared nothing, or
    the user changed a role, removed one or added one: the reason would then claim the file said
    what the user decided."""
    reason = str((facts or {}).get("openings_reason") or "").strip()
    if not reason:
        return ""
    declared = {int(o["id"]): o.get("role") for o in (facts.get("openings") or [])
                if isinstance(o, dict) and "id" in o}
    confirmed = {int(getattr(o, "id", -1)): getattr(o, "role", None) for o in (openings or [])}
    return reason if declared and confirmed == declared else ""


_OPENINGS_RE = re.compile(r"Openings: ([^\n]*?)\.(?: |$)")
_ROW_RE = re.compile(r"([^\s;(]+) \((inlet|outlet|wall)\)")


def roles_reason_of(messages, assignments=None) -> str:
    """The reason the latest declaration gives for its openings' roles, or ''. With `assignments`
    (the brief's [{name, role}]), only while every declared opening is still there with its
    declared role - a role changed in the chat afterwards is the user's, not the file's."""
    for m in reversed(list(messages or [])):
        if isinstance(m, dict) and _is_declaration(m):
            text = str(m.get("content") or "")
            found = _ROLES_RE.search(text)
            if not found:
                return ""
            if assignments is not None:
                rows = _OPENINGS_RE.search(text)
                declared = dict(_ROW_RE.findall(rows.group(1))) if rows else {}
                now = {str(a.get("name")): str(a.get("role") or a.get("type") or "")
                       for a in assignments or [] if isinstance(a, dict)}
                if not declared or any(now.get(n) != r for n, r in declared.items()):
                    return ""
            return found.group(1).strip()
    return ""


def renamed_openings_sentence(before, after) -> str:
    """What the confirmation says when openings were named apart: "Openings shared a name, so
    opening 7 is called Outlet_2 ...". Empty when nothing was renamed."""
    moved = [(a.id, a.name) for b, a in zip(before, after) if a.name != b.name]
    if not moved:
        return ""
    said = ", ".join(f"opening {i} is called {n}" for i, n in moved)
    # the chat shows the whole confirmation to the user, so this says it to them, not to the model
    return (f"Openings shared a name (with each other or with the walls), so {said}; any of them "
            "can be renamed on the picture.")


def distinct_opening_names(openings) -> list:
    """The openings with every real opening's name its own. Two that would reach a mesher as one
    name ("inlet" and "Inlet", "outlet 1" and "outlet_1") keep the first and number the rest
    ("inlet_2", "inlet_3") - what patch_names does for a declaration - instead of refusing the
    confirmation. The built-in wall an internal flow adds (patches_from) is taken before any
    sticker, so a sticker called "wall" or "Wall" becomes wall_2 here, where it is said, rather
    than a second "wall" the intake has to ask about. A sticker marked not an opening never
    becomes a patch, so its name is left as it is and compared with nothing."""
    from meshpipeline.contracts.patch_names import same_patch_name

    longest = 40                                      # the stage form's name box (ConfirmedOpening)
    out = []
    kept: list[str] = [BUILT_IN_WALL]
    for o in openings:
        if getattr(o, "role", None) == "not_an_opening":
            out.append(o)
            continue
        name = str(o.name)
        if any(same_patch_name(name, k) for k in kept):
            n = 2
            while True:
                suffix = f"_{n}"
                candidate = f"{name[:longest - len(suffix)]}{suffix}"
                if not any(same_patch_name(candidate, k) for k in kept):
                    break
                n += 1
            o = o.model_copy(update={"name": candidate}) if hasattr(o, "model_copy") else {**o, "name": candidate}
            name = candidate
        kept.append(name)
        out.append(o)
    return out


def reference_length_along_the_flow(body, stored_size_mm=None, proposed_mm=None) -> tuple[Any, str]:
    """The confirmation with its reference length along the confirmed flow axis, and the sentence
    that says so - or the body as it came and "" when nothing needed changing.

    The check fills the reference length with the part's length along the axis it guessed. A
    console (or a caller) that sent the user's corrected axis with the guessed axis's length
    untouched would size the far field on the wrong length: the NASA CRM read along +y and turned
    to +x kept its 30.4 m span instead of its 64.6 m length, a far field half the size.

    Only the check's own guess is ever changed. A length the user typed (`reference_length_typed`)
    is theirs; so is one that is none of the lengths the check proposed (`proposed_mm`, in the
    body's scale: the measuring step's, and the naming's that replaced it - a form drawn before
    the naming landed still holds the first) - a caller that chose the car's width on purpose
    sent a number the check never offered; and so is one that is no extent of the part at all.
    With no proposal to compare with, the length being another axis's extent decides.
    `stored_size_mm` is the part's size, in the body's scale, for a body that did not carry its
    own. Never refuses: it corrects and says so."""
    if getattr(body, "flow", None) != "external" or getattr(body, "reference_length_typed", False):
        return body, ""
    from meshpipeline.contracts.geometry_fields import length_of_another_axis, same_length

    ref = getattr(body, "reference_length_mm", None)
    offered = [float(p) for p in (proposed_mm or ())]
    if offered and ref is not None and not any(same_length(float(ref), p) for p in offered):
        return body, ""
    size = getattr(body, "size_mm", None)
    size = size if size and len(size) == 3 else stored_size_mm
    found = length_of_another_axis(ref, getattr(body, "flow_axis", None), size)
    if found is None:
        return body, ""
    along, other = found
    was = float(body.reference_length_mm)
    # the chat shows the whole confirmation to the user, so this says it to them, not to the model
    note = (f"The reference length on the form, {was:.0f} mm, was the part's length along {other} - "
            "left from the check's first guess of the flow axis - so it is now the part's length "
            f"along the flow, {along:.0f} mm, and the far-field margins are multiples of that. "
            "Another reference length can be given in the chat.")
    return body.model_copy(update={"reference_length_mm": along}), note


def stored_lengths(stored_check: dict | None, scale_to_m: float) -> tuple[list[float] | None, tuple[float, ...]]:
    """The part's size and every reference length the check proposed, from the stored check (read
    under the scout's own scale), re-read in `scale_to_m` - what a confirmation is compared with.
    The lengths are the stored proposal's and the measuring step's own guess, which the naming's
    proposal replaced in the store but a form drawn before it landed still shows. The size is
    None, and the lengths empty, when the check holds none."""
    facts = (stored_check or {}).get("facts") or {}
    proposal = (stored_check or {}).get("proposal") or {}
    try:
        was = float(facts.get("scale_to_m") or 0.001)     # what the scout and the form assume
        k = float(scale_to_m) / was
    except (TypeError, ValueError, ZeroDivisionError):
        return None, ()
    try:
        sizes: list[float] | None = [round(float(v) * k, 6) for v in (facts.get("size_mm") or proposal.get("size_mm") or [])]
    except (TypeError, ValueError):
        sizes = None
    offered = []
    for length in (proposal.get("reference_length_mm"), _measuring_steps_guess(facts)):
        try:
            if length:
                offered.append(round(float(length) * k, 6))
        except (TypeError, ValueError):
            continue
    return (sizes if sizes and len(sizes) == 3 else None), tuple(offered)


def _measuring_steps_guess(facts: dict) -> float | None:
    """The reference length the measuring step proposed before the naming answered: the part's
    length along its own guess of the flow axis, across the up axis the shape alone decides -
    what the scouted form showed. None when the facts cannot say."""
    try:
        from meshpipeline.application.geometry_check import decide_up
        from meshpipeline.contracts.geometry_fields import external_defaults

        up = decide_up(str(facts.get("flow") or ""), facts.get("up_evidence"), None)["up_axis"]
        return float(external_defaults(facts, None, up)["reference_length_mm"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


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
    said = f"The file is in {UNIT_WORDS[unit]}"
    said += f": the part is {length_words(longest / 1000.0)} long." if longest > 0 else "."
    if getattr(body, "unit_fixed", False):
        # the converter wrote the geometry in this unit: asking would invite the SOURCE's unit
        # ("metres", for an ECXML model) as an answer about the converted geometry. The chat shows
        # these words to the user, so they state a fact; the intake prompt block says what follows.
        said += " That unit is fixed by the conversion of the uploaded file, so it is settled"
        # ...and WHY, in the format's own words: a user who knows an ECXML model is in metres
        # read "the file is in millimetres" as a mistake and corrected it at the approval
        # (dev-areen set_top_box, 2026-10-11) - both are true, and this says so
        reason = str(getattr(body, "unit_fixed_reason", "") or "").strip().rstrip(".")
        said += f": {reason}." if reason else "."
    if longest > 0:
        said += " The sizes above are already converted to millimetres; use them as they stand."
    return said


#: How each kind reads in the declaration.
_KIND_WORDS = {"body-surface": "the part's wall, hollow inside for the fluid",
               "fluid-domain": "the fluid volume itself",
               "solid-body": "a solid body",
               "solid-assembly": "an assembly of solid parts, each its own region"}
#: ...and the same kind said for ONE channelled solid. The kind covers two shapes of file and the
#: confirmation knows which it has, so each customer is told about their own part: a cold plate's
#: owner reads "an assembly of solid parts" as a denial, and a thermal model's owner would read
#: "coolant passages" as the wrong part. The STAGE's dropdown cannot branch - the user picks from
#: a fixed list before anything is settled - so there the one label covers both.
_COOLED_SOLID_WORDS = "a solid part with coolant passages through it"


def kind_words(body) -> str:
    """How the confirmation names what the file is, in the user's own terms."""
    if str(getattr(body, "input_kind", "")) == "solid-assembly" \
            and not [n for n in (getattr(body, "regions", None) or []) if str(n).strip()] \
            and _coolant_is_inside(body):
        return _COOLED_SOLID_WORDS
    return _KIND_WORDS[body.input_kind]
#: What the declaration says of a whole thermal model's air box (check_mode "thermal"): its sides
#: are the file's, so no boundary is declared and none is asked about. The chat shows it.
THERMAL_SIDES = ("The sides of the air box are the file's own: open to the room unless the file "
                 "puts a fan, a vent or a wall there, and kept under the file's names. Nothing about "
                 "them needs declaring.")
#: The detail levels a thermal model's panel offers, as the panel names them, by the mesher's
#: fidelity words (the intake's mesh_fidelity).
DETAIL_WORDS = {"draft": "Quick look", "standard": "Standard", "max": "Fine"}


def detail_sentence(body) -> str:
    """What the declaration says of the detail level the user picked on the panel: the user's own
    choice of mesh_fidelity, in the panel's word and the intake's. "" when none was picked."""
    level = str(getattr(body, "detail", "") or "")
    if level not in DETAIL_WORDS:
        return ""
    return (f"Detail level: {DETAIL_WORDS[level]}, chosen by the user on the panel "
            f"(mesh_fidelity {level}).")


#: Region names spelled out in an assembly's declaration; the rest are counted.
_REGIONS_NAMED = 24


def _coolant_is_inside(body) -> bool:
    """Whether the file is a COOLED SOLID: the coolant is the space inside it.

    THE MEASUREMENT AND NOT THE SOLID COUNT. This branched on solids == 1, and a brazed or
    diffusion-bonded cold plate is base plus lid - two solids, and the commonest way one is made -
    so it fell through to "each solid part is its own solid region and the fluid around them is
    one more", which is the air-box wording and describes the opposite of the part. Three solids
    (base, lid, manifold) is the same case again.

    The extraction's answer is the right evidence: it found an enclosed space inside the parts that
    reaches the outside. Where the check did not run or found nothing, the solid count still
    stands in - a single solid confirmed as a cooled solid is one.
    """
    found = getattr(body, "coolant_inside", None)
    if found is not None:
        return bool(found)
    return int(getattr(body, "solids", 0) or 0) == 1


def _cooled_solid_words(body) -> str:
    """"The part is one solid with coolant passages through it", or the same for a bonded stack."""
    n = int(getattr(body, "solids", 0) or 0)
    if n >= 2:
        return (f"The part is {n} solids bonded into one piece, with coolant passages through it "
                "(a base and a lid, as a brazed or diffusion-bonded cold plate is made)")
    return "The part is one solid with coolant passages through it"


def assembly_sentence(body) -> str:
    """What a multi-region input is, for the intake: the solid and the fluid each their own
    region, meshed as they stand - the conjugate heat-transfer input.

    IT IS SAID TWO WAYS, because the kind covers two shapes of file. A file of several parts has
    the fluid AROUND them, which is what a thermal model's air box is. One channelled solid - a
    cold plate, a chip package, a heat-exchanger core - has the coolant INSIDE it, and telling
    that customer about "the fluid around them" describes the opposite of their part.
    """
    names = [str(n) for n in (getattr(body, "regions", None) or []) if str(n).strip()]
    if not names and _coolant_is_inside(body):
        return (f"{_cooled_solid_words(body)}, so it is two regions at once: the metal carries "
                "the heat and the coolant inside it takes the heat away. Both are meshed, sharing "
                "every face between them, as a conjugate heat-transfer case - nothing is built "
                "around the part.")
    said = ("Each solid part is its own solid region and the fluid around them is one more region; "
            "together they fill the domain, so the file is meshed as it stands, as a multi-region "
            "(conjugate heat transfer) case, with no fluid domain built around it.")
    if names:
        shown = ", ".join(names[:_REGIONS_NAMED])
        more = f", and {len(names) - _REGIONS_NAMED} more" if len(names) > _REGIONS_NAMED else ""
        said += f" Its {len(names)} regions, named as in the file: {shown}{more}."
    return said


#: HOW A THERMAL MODEL'S DECLARATION BEGINS, after the mark: the console says it was confirmed on
#: the thermal model panel (render/geometry_form.js displayText mirrors this), and the intake reads
#: the case from it (`declared_case`, `thermal_declared`).
THERMAL_LEAD = "the thermal model"
_THERMAL_CASE = "input_kind solid-assembly, for a conjugate heat-transfer solve."


def thermal_confirmation_message(body) -> str:
    """The declaration of a whole thermal model (check_mode "thermal"), in an electronics-cooling
    engineer's words: the model by the file's name, its parts and the air among them as regions,
    a conjugate heat-transfer solve, the air box's sides as the file has them, the detail level
    picked on the panel, and the unit the conversion fixed. Nothing in it speaks of openings, of
    which way a fluid goes, or of a picture: the panel showed the file's own model."""
    title = str(getattr(body, "part", "") or "").strip()
    parts = [f"{CONFIRMED_MARK} {THERMAL_LEAD}" + (f" “{title}”" if title else "")
             + ", read from the file: its parts and the air among them, each its own region, "
             + _THERMAL_CASE]
    names = [str(n) for n in (getattr(body, "regions", None) or []) if str(n).strip()]
    said = ("Each part is its own solid region and the air among them is one more; together they "
            "fill the air box, so the model is meshed as it stands.")
    if names:
        shown = ", ".join(names[:_REGIONS_NAMED])
        more = f", and {len(names) - _REGIONS_NAMED} more" if len(names) > _REGIONS_NAMED else ""
        said += f" Its {len(names)} regions, named as in the file: {shown}{more}."
    parts.append(said)
    parts.append(THERMAL_SIDES)
    if detail_sentence(body):
        parts.append(detail_sentence(body))
    size = getattr(body, "size_mm", None)
    if size and len(size) == 3:
        parts.append(f"Air box: {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm.")
    if getattr(body, "unit", None):
        # the converter wrote the model in millimetres; the file's own lengths are metres. Both are
        # true, and the intake prompt reads "fixed by the conversion" as never to be asked again
        reason = str(getattr(body, "unit_fixed_reason", "") or "").strip().rstrip(".")
        parts.append("Lengths are in millimetres, fixed by the conversion of the uploaded file"
                     + (f": {reason}." if reason else "."))
    return " ".join(parts)


#: HOW AN FEA PART'S DECLARATION BEGINS, after the mark: the part itself for structural analysis
#: (application/purpose_map routed the words to the FEA path). The console says it was confirmed on
#: the stage (render/geometry_form.js displayText), and the intake reads the case from it.
FEA_LEAD = "a solid part for structural analysis (FEA)"
#: The check mode a confirmation carries for an FEA part (set by the server, never the form).
FEA_CHECK = "fea"
_FEA_CASE = "input_kind solid-body: the part itself is meshed, with no fluid around it."


#: How an assembly's parts are joined, said in its declaration (the stage's one question).
FEA_CONNECTION_WORDS = {
    "bonded": ("Parts connected: bonded (welded or glued) - one mesh, the parts share their nodes where they "
               "touch."),
    "contact": ("Parts connected: contact (they can slide or separate) - each part meshed on its own, with "
                "named contact pairs where they touch."),
}


def fea_confirmation_message(body) -> str:
    """The declaration of a part confirmed on the FEA stage: the part, meshed as a solid for
    structural analysis, how many bodies the file has, what the clean check found, its size and
    its unit. Nothing in it speaks of a fluid, openings, a flow axis or a far field."""
    part = str(getattr(body, "part", "") or "").strip()
    parts = [f"{CONFIRMED_MARK} {FEA_LEAD}" + (f" ({part})" if part else "") + f", {_FEA_CASE}"]
    bodies = getattr(body, "bodies", None)
    if isinstance(bodies, int) and bodies > 0:
        parts.append("One body." if bodies == 1 else f"{bodies} bodies.")
    # AN ASSEMBLY: its parts, meshed together, joined as the user said on the stage
    said_parts = str(getattr(body, "parts_summary", "") or "").strip().rstrip(".")
    connection = str(getattr(body, "fea_connection", "") or "").strip()
    if connection:
        parts.append(f"An assembly of {said_parts}, meshed together as solids." if said_parts
                     else "An assembly of several parts, meshed together as solids.")
        parts.append(FEA_CONNECTION_WORDS.get(connection, FEA_CONNECTION_WORDS["bonded"]))
    clean = str(getattr(body, "clean_summary", "") or "").strip().rstrip(".")
    if clean:
        parts.append(f"Clean check: {clean}.")
    size = getattr(body, "size_mm", None)
    if size and len(size) == 3:
        parts.append(f"Part size: {size[0]:.0f} x {size[1]:.0f} x {size[2]:.0f} mm.")
    said = unit_sentence(body)
    if said:
        parts.append(said)
    # the chat shows the declaration to the user, so it speaks to them
    parts.append("Every face of the part is kept as a named group in the delivered file, so you set "
                 "supports and loads in your own solver.")
    return " ".join(parts)


def fea_declared(messages: list[dict] | None) -> bool:
    """Whether the conversation's declaration (its latest one) is an FEA part's."""
    for m in reversed(list(messages or [])):
        if isinstance(m, dict) and _is_declaration(m):
            return str(m.get("content", "")).startswith(f"{CONFIRMED_MARK} {FEA_LEAD}")
    return False


def thermal_declared(messages: list[dict] | None) -> bool:
    """Whether the conversation's declaration is a whole thermal model's (its latest one)."""
    for m in reversed(list(messages or [])):
        if isinstance(m, dict) and _is_declaration(m):
            return str(m.get("content", "")).startswith(f"{CONFIRMED_MARK} {THERMAL_LEAD}")
    return False


def confirmation_message(body) -> str:
    """The sentence the intake reads: everything the user confirmed, in plain words, with the
    numbers the builder needs. Its opening phrase is the one the intake prompt block names."""
    if getattr(body, "check_mode", "") == "thermal":
        return thermal_confirmation_message(body)
    if getattr(body, "check_mode", "") == FEA_CHECK:
        return fea_confirmation_message(body)
    kind = kind_words(body)
    through = "through it" if body.flow == "internal" else "around it"
    # The kind is stated verbatim, in the intake's own enum, and it is the same word the session
    # stores. A solid body the fluid flows around stays "solid-body": the engine gate reads that as
    # a body surface for any fluid purpose (engines/purposes.kinds_admitted_as), and a later
    # structural request on the same solid still finds the kind gmsh needs.
    parts = [f"{CONFIRMED_MARK} the file is {kind}"
             + (f" ({body.part})" if body.part else "")
             + f", input_kind {body.input_kind}; the fluid flows {through}."]
    if body.input_kind == "solid-assembly":
        parts.append(assembly_sentence(body))
    ports = [o for o in body.openings if o.role != "not_an_opening"]
    if body.flow == "external":
        from meshpipeline.contracts.geometry_fields import external_declaration
        parts.append("No openings: the fluid flows around the whole body.")
        parts.extend(external_declaration(body))
    elif ports:
        rows = []
        for o in ports:
            inner = _centre_body_mm(o)
            size = (f"an annulus {_mm(o.diameter_mm)} mm across round a {_mm(inner)} mm centre body"
                    if inner else f"{_mm(o.diameter_mm)} mm across" if o.diameter_mm
                    else f"{_mm(o.width_mm)} x {_mm(o.height_mm)} mm"
                    if o.width_mm and o.height_mm else "")
            flow_area = _num_or_none(getattr(o, "flow_area_mm2", None))
            if flow_area and (inner or not o.diameter_mm):
                size += f" ({_mm2(flow_area)} mm2 of flow)"
            at = (f" at ({_mm(o.centroid_mm[0])}, {_mm(o.centroid_mm[1])}, "
                  f"{_mm(o.centroid_mm[2])}) mm"
                  if o.centroid_mm and len(o.centroid_mm) == 3 else "")
            rows.append(f"{o.name} ({o.role}){', ' + size if size else ''}{at}")
        parts.append("Openings: " + "; ".join(rows) + ".")
        why = str(getattr(body, "openings_reason", "") or "").strip().rstrip(".")
        if why:
            parts.append(f"{ROLES_MARK} {why}.")
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


_DECLARED = re.compile(
    r"input_kind (body-surface|fluid-domain|solid-body|solid-assembly); the fluid flows (through|around) it\.")
#: A thermal model's case, as `thermal_confirmation_message` states it.
_THERMAL_DECLARED = re.compile(re.escape(_THERMAL_CASE))


#: A user message that says what the file is or how the fluid moves - after the stage, a correction
#: the stage's sentence no longer speaks for.
_RESTATES = re.compile(
    r"fluid[ -]?(?:volume|domain|region)|body[ -]?surface|solid[ -]?body|input[ _-]?kind|"
    r"\b(?:file|part|geometry|model|it|this)(?:'s| is| was)\s+(?:actually\s+|really\s+)?(?:the |a |an |just )?"
    r"(?:wall|walls|solid|surface|skin|shell|fluid|hollow|filled)\b|"
    r"\bnot the (?:fluid|wall|solid|surface)|\b(?:internal|external) (?:flow|cfd)\b|"
    r"flows? (?:through|around|over|past|inside)", re.IGNORECASE)


def declared_case(messages: list[dict] | None) -> tuple[str, str] | None:
    """(purpose, input kind) as the user confirmed them on the stage - read back from this module's
    own sentence: a fluid flowing through the part is internal CFD, around it external CFD, and an
    assembly of solid parts with the fluid among them is conjugate heat transfer - the one purpose
    a multi-region input is meshed for. None
    when nothing was confirmed there, or when a later message of the user's speaks about what the
    file is or how the fluid moves: the chat may have corrected the stage, and then the stage's
    sentence is not the last word. What an engine the model proposes on its own must mesh."""
    msgs = list(messages or [])
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and _is_declaration(m):
            text = str(m.get("content", ""))
            found = _DECLARED.search(text)
            # a whole thermal model says its case in its own words: an assembly of parts with the
            # air among them, for a conjugate heat-transfer solve
            thermal = found is None and text.startswith(f"{CONFIRMED_MARK} {THERMAL_LEAD}") \
                and _THERMAL_DECLARED.search(text) is not None
            # an FEA part says its case in its own words too: the solid part, for structural work
            fea = found is None and text.startswith(f"{CONFIRMED_MARK} {FEA_LEAD}") and _FEA_CASE in text
            if not found and not thermal and not fea:
                return None
            if any(isinstance(later, dict) and later.get("role") == "user"
                   and _RESTATES.search(str(later.get("content", ""))) for later in msgs[i + 1:]):
                return None
            if thermal:
                return "conjugate_heat_transfer", "solid-assembly"
            if fea:
                return "structural", "solid-body"
            assert found is not None
            kind, flow = found.groups()
            if kind == "solid-assembly":
                return "conjugate_heat_transfer", kind
            return ("internal_cfd" if flow == "through" else "external_cfd"), kind
    return None


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


def confirmed_opening_names(session_id) -> tuple[str, ...]:
    """The boundary patches the uploaded file declares, as the user confirmed them on the geometry
    stage (the stored confirmation's openings, "not an opening" left out). Empty when nothing was
    confirmed or the store cannot be read - and then nothing is judged against it."""
    import json

    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(
            object_key=check_object_key(str(session_id), "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown, never a refusal
        return ()
    return tuple(str(o.get("name")) for o in (record.get("openings") or [])
                 if isinstance(o, dict) and o.get("name") and o.get("role") != "not_an_opening")


#: WHAT A PORT CARRIES THAT ONLY THE STAGE MEASURED. Exactly the fields
#: `admission_token.approved_intent_canonical` puts in its `port_declaration`, because those are
#: the ones an approval is fingerprinted on and an engine binds ports with.
PORT_DETAIL_FIELDS: tuple[str, ...] = (
    "near_mm", "diameter_mm", "inner_diameter_mm", "outer_diameter_mm",
    "width_mm", "height_mm", "area_mm2", "interchangeable_with",
)


def confirmed_port_details(session_id) -> dict[str, dict]:
    """Each confirmed port's MEASURED fields, by name, from the stored confirmation.

    The stage measured these off the part and drew them as stickers the user pressed Proceed on,
    and `confirmed.json` is written before the session takes the declaration ("the stored copy
    goes first"), so it is the durable record of them. Empty when nothing was confirmed or the
    store cannot be read, and then nothing is restored from it.
    """
    import json

    from meshpipeline.application.geometry_check import check_object_key
    from meshpipeline.contracts.object_storage import get_object_store

    try:
        record = json.loads(get_object_store().get_bytes(
            object_key=check_object_key(str(session_id), "confirmed.json")))
    except Exception:  # noqa: BLE001 - unknown, never a refusal
        return {}
    out: dict[str, dict] = {}
    for q in (record.get("patches") or []):
        if not isinstance(q, dict):
            continue
        name = str(q.get("name") or "").strip()
        role = str(q.get("type") or q.get("role") or "").strip().lower()
        if not name or role not in ("inlet", "outlet"):
            continue
        have = {k: q[k] for k in PORT_DETAIL_FIELDS
                if q.get(k) is not None and q.get(k) != []}
        if have:
            out[name] = have
    return out


def restore_port_details(patches, confirmed: dict) -> tuple[list, list[str]]:
    """Put back the measured fields the stage confirmed, on the ports that lost them.

    WHY THIS EXISTS. The stage measures where each port is, the user confirms it on a picture, and
    `confirmed.json` records it - and then the submission's patch list is produced by a MODEL,
    which is asked in prose to copy those numbers across. Measured live, same file, same path,
    twice:

        run A  patches [{inlet, near_mm [6,5,2], diameter_mm 0.9}, {outlet, near_mm [2,1,2], ...}]
        run B  patches [{inlet}, {outlet}]

    Run B's mesh built correctly and was then refused: with no `near_mm` the conjugate engine
    cannot bind the declared ports to the coolant's mouths, so it named them `coolant_in` and
    `coolant_out`, and the patch contract rejected the mesh for not carrying the names the user
    approved. 29 minutes of meshing, no retry ("a cause another attempt cannot change").
    `mouths.declared_for` already carries the scar of this failing once before.

    WHAT THIS DOES AND DOES NOT DO. It fills a measured field that is ABSENT, from the
    confirmation of this same session, on a port of the same name. It never overwrites a value the
    model did state - a user who corrects a bore in the conversation is still right - never adds or
    removes a patch, and never touches a name or a role. The model keeps every judgement; it just
    stops being the courier for numbers it did not measure.

    Returns the patches and the names actually restored, so the change can be said out loud.
    """
    rows, restored = [], []
    for p in (patches or []):
        if not isinstance(p, dict):
            rows.append(p)
            continue
        name = str(p.get("name") or "").strip()
        role = str(p.get("type") or p.get("role") or "").strip().lower()
        have = confirmed.get(name) or {}
        missing = {k: v for k, v in have.items()
                   if role in ("inlet", "outlet") and (p.get(k) is None or p.get(k) == [])}
        if missing:
            rows.append({**p, **missing})
            restored.append(name)
        else:
            rows.append(p)
    return rows, restored


#: The one wall an internal flow's confirmation declares for every surface that is not an opening.
BUILT_IN_WALL = "wall"

#: THE FLOW KIND BY RULE (INS-3, docs/RULES.md). A flow through the part needs an inside with a way
#: in and a way out. When the user says the fluid flows through a part whose inside-space map
#: (cad/inside_space) finds no passage with two mouths, and fewer than two openings are confirmed
#: on it, the stage asks one plain question - with the one-click way out of flowing around it -
#: instead of a job that can only fail.
FLOW_KIND_RULE = "INS-3"
NO_INSIDE_QUESTION = "This part has no openings or inside for fluid. Did you mean flow around it?"
ONE_MOUTH_QUESTION = ("The space inside this part has only one opening, so the fluid has no way through it. "
                      "Did you mean flow around it?")


#: INS-7: an internal-flow part with no way through whose surface is open or in loose pieces is
#: told it needs CAD repair, never offered flow around it (a broken surface is no part to flow
#: round either). The repair stage closes it; this stage only says what it found.
CAD_REPAIR_MESSAGE = ("This part's surface is not closed in one piece ({what}), so the space the fluid "
                      "fills cannot be found. It needs CAD repair first.")


def needs_cad_repair(surface: dict | None) -> str:
    """INS-7's words for a surface with open edges that edge no opening, or loose pieces, else ""."""
    if not isinstance(surface, dict):
        return ""
    what = []
    stray, pieces = int(surface.get("open_edges_not_openings") or 0), int(surface.get("pieces") or 1)
    if stray:
        what.append(f"{stray} open edge{'s' if stray != 1 else ''} that edge no opening")
    if pieces > 1:
        what.append(f"{pieces} loose pieces")
    return CAD_REPAIR_MESSAGE.format(what=" and ".join(what)) if what else ""


def flow_question(inside: dict | None, openings, surface: dict | None = None) -> str:
    """The stage's question when "the fluid flows through it" cannot be meshed as said, else "".
    `inside` is the check's map facts (scout `inside`); `openings` the openings as confirmed (dicts
    or rows with a `role`). A part the map could not read is never questioned: the intake asks."""
    def role(o) -> str:
        return str((o.get("role") if isinstance(o, dict) else getattr(o, "role", "")) or "")
    ports = [o for o in openings or () if role(o) in ("inlet", "outlet")]
    if len(ports) >= 2 or not isinstance(inside, dict) or inside.get("mapped") is False:
        return ""
    if inside.get("has_inside"):
        return ""                 # there is a way through: the openings are the user's to place
    broken = needs_cad_repair(surface)
    if broken:
        return broken             # INS-7: said, never healed; no "flow around it" for a broken surface
    mouths = max([int(n) for n in inside.get("mouths_per_passage") or []] or [0])
    return ONE_MOUTH_QUESTION if mouths == 1 else NO_INSIDE_QUESTION


def patches_from(body) -> list[dict]:
    """The session's declared patches, in the shape the intake's submit tool already takes:
    name and role, plus the size and location fields the port binding reads. The openings come
    named apart from the built-in wall (distinct_opening_names, at the stage); a caller that did
    not name them apart still never gets two boundaries one mesher name - the wall then takes the
    next free name instead."""
    patches: list[dict] = []
    if body.flow == "external":
        return patches
    for o in body.openings:
        if o.role == "not_an_opening":
            continue
        entry: dict = {"name": o.name, "type": o.role}
        inner = _centre_body_mm(o)
        flow_area = _num_or_none(getattr(o, "flow_area_mm2", None))
        # AN ANNULUS - a centre rod or hub standing in the bore - is declared as the bore and the
        # body in it, which every reader of a declaration takes as the ring between them (the
        # port binding, the port gate, the hydraulic diameter D - d the mesh is sized by). The
        # scout measured both, so pi/4 (D^2 - d^2) is the flow area it measured. A ring of
        # another shape round a body states that area alone (one size form per port).
        if inner:
            entry["diameter_mm"], entry["inner_diameter_mm"] = float(o.diameter_mm), inner
        elif flow_area and o.width_mm and o.height_mm:
            entry["area_mm2"] = flow_area
        # a rectangle keeps its sides: the form sends width and height only for a rectangular
        # opening (with its equal-area diameter beside them, which would turn it into a circle)
        elif o.width_mm and o.height_mm:
            entry["width_mm"], entry["height_mm"] = float(o.width_mm), float(o.height_mm)
        elif o.diameter_mm:
            entry["diameter_mm"] = float(o.diameter_mm)
        if o.centroid_mm and len(o.centroid_mm) == 3:
            entry["near_mm"] = [float(v) for v in o.centroid_mm]
        patches.append(entry)
    if body.flow == "internal" and patches:
        from meshpipeline.contracts.patch_names import same_patch_name

        wall, n = BUILT_IN_WALL, 2
        while any(same_patch_name(wall, p["name"]) for p in patches):
            wall, n = f"{BUILT_IN_WALL}_{n}", n + 1
        patches.append({"name": wall, "type": "wall"})
    return patches


def _num_or_none(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


#: How many places a length in millimetres is worth printing to. THREE SIGNIFICANT FIGURES, and
#: the reason is a measurement: every size in this sentence used to print with :.0f, so a
#: 2.0 x 0.25 mm port read "2 x 0 mm", a 0.25 mm microchannel read "0 x 0 mm", and a 0.8 mm bore
#: at (5.0, 0.4, 0.6) read "1 mm across at (5, 0, 1) mm" - the diameter up a quarter and the
#: centroid moved 0.4 mm on a part whose channel is 0.8 mm wide. This is the text the owner
#: confirms and the text the case is read back out of, and the numbers behind it were right all
#: along. A cold plate's channels live between 0.05 and 1 mm, so whole millimetres cannot describe
#: one at all.
_PLACES = 3


def _mm(value: float) -> str:
    """A length in millimetres, to three significant figures, with no trailing zeros.

    10.58 -> "10.6",  2.0 -> "2",  0.25 -> "0.25",  0.0457 -> "0.0457",  0.0 -> "0".
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    if v == 0.0:
        return "0"
    import math
    from decimal import Decimal
    # _PLACES SIGNIFICANT FIGURES, BUT NEVER COARSER THAN A WHOLE MILLIMETRE. Three significant
    # figures alone turns a 1234.5 mm part into "1230 mm", which is a centimetre of invented
    # slack on the one part big enough for it to matter.
    places = max(0, _PLACES - 1 - int(math.floor(math.log10(abs(v)))))
    text = format(Decimal(repr(round(v, places))).normalize(), "f")
    return text.rstrip(".") if "." in text else text


def _mm2(value: float) -> str:
    """An AREA in square millimetres: grouped above a thousand, significant figures below it.

    An annulus of flow is thousands of square millimetres and reads as "2,199"; a microchannel's
    cross-section is a fraction of one and read as "0" while every size here printed whole.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0"
    return f"{v:,.0f}" if abs(v) >= 1000.0 else _mm(v)


def _centre_body_mm(o) -> float | None:
    """The diameter of the centre body standing in a round opening's bore (the scout's
    inner_diameter_mm), or None for a plain bore - and for a bore said to be no wider than the
    body in it, which no annulus can be (a diameter retyped below it on the form)."""
    if o.width_mm and o.height_mm:
        return None
    inner, bore = _num_or_none(getattr(o, "inner_diameter_mm", None)), _num_or_none(o.diameter_mm)
    return inner if inner and bore and inner < bore else None


#: The fields of a stored confirmation that are not the user's body: when it was stored, by whom,
#: and what was derived from it.
_RECORD_ONLY = ("confirmed_at", "owner_id", "message", "patches", "reread")
_OPENING_DEFAULTS: dict[str, object] = {"centroid_mm": None, "diameter_mm": None, "width_mm": None,
                                         "height_mm": None, "inner_diameter_mm": None,
                                         "flow_area_mm2": None}
_BODY_DEFAULTS: dict[str, object] = {"openings": [], "seed_point_mm": None, "size_mm": None, "part": "",
                                     "coolant_inside": None,
                                     "flow_axis": None, "reference_length_mm": None, "extents": None,
                                     "grounded": False, "up_axis": None, "scale_to_m": None, "unit": None,
                                     "unit_fixed": False, "unit_fixed_reason": "", "regions": None, "openings_reason": "",
                                     "check_mode": "", "detail": None, "solids": None}


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
_OPENING_LENGTHS = ("centroid_mm", "diameter_mm", "width_mm", "height_mm", "inner_diameter_mm",
                    "flow_area_mm2")
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


__all__ = ["BUILT_IN_WALL", "CONFIRMED_MARK", "assembly_sentence", "body_of", "confirmation_message", "declared_case", "kind_words", "distinct_opening_names",
           "renamed_openings_sentence",
           "patches_from", "replace_declaration",
           "reread_record", "sizeless_record", "thermal_confirmation_message", "thermal_declared",
           "unit_sentence", "with_declaration"]
