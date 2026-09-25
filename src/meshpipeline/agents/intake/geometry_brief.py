# Responsibility: Turn a stored geometry measurement into what intake may say and what it no longer has to ask.
# Owns: the opening table shown to the model, the Surveyor's questions as intake reads them, and the binding of a declared patch to a measured opening.
# Boundaries: it renders and it binds; it writes no question of its own, blocks nothing and changes no tool schema.
# Collaborates with: cad/regions.py for the document and contracts/rationale.py for what the customer is told.
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# WHY THIS IS A PROMPT BLOCK AND NOT A TOOL.
#
# Across the 287 stored conversations intake asks 601 questions of its own, and 321 of them name
# something the file measures: a bore, a port, an opening, an axis, a bounding box, a coordinate.
# It asks them because it cannot see the file - `cad/regions.py:135-151` listed a directory
# `api/v1/upload.py:253` had already emptied - so every one of those 321 is a question with an
# answer already on disk.
#
# The narrowest possible fix is a prompt block. A tool would need a new schema, a new call the model
# has to remember to make, and a new failure mode when it does not; a block is text the model is
# already holding when it writes its first sentence. Nothing in `INTAKE_TOOLS` changes, no
# validator changes, and `submit_requirements` takes exactly the fields it took before. This is
# Rehaan's production conversation and a regression here is worse than a missed improvement.
#
# THE SURVEY IS THE ONE EXCEPTION, and it is behind its own gate. The chain needs two structured
# answers back from the conversation: what the part is for, because the measurement is composed for
# it, and what the customer said to each question, with who said it. Prose cannot carry either, so
# `agents/intake/agent.py` offers `SURVEY_TOOLS` beside `INTAKE_TOOLS` when the survey is armed and
# never otherwise. Everything this module renders for them is below `survey_lines`.

#: How many opening rows the table carries. A 400-body part has hundreds of openings and a table
#: that long buries the three that are ports. Ordered widest first, so the cut takes the least
#: significant. The corpus's largest declared port count is well inside this.
MAX_TABLE_ROWS = 12

#: Below this fraction of the widest opening, an opening is summarised in a sentence rather than
#: given a row. 619 openings across the corpus are named by no brief, and 247 of them on 95 cases
#: are at least a quarter of the smallest declared bore: those are worth a sentence, and the rest
#: are meshed as wall in silence today and stay that way.
MINOR_OPENING_FRACTION = 0.25


def _mm(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    return f"{float(value) * 1000.0:.1f} mm"


def _file(value: Any) -> str:
    """A length in the file's own numbers, said as what it is.

    SIGNIFICANT FIGURES, not decimal places. A file with no declared unit may be authored in metres
    (a 0.052 bore), in millimetres (52.0) or in inches (2.05), and a fixed `.1f` prints the first of
    those as "0.1" and the ports as "0.0" - which destroys the ratios that are the only thing a
    unitless table has to offer.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    return f"{float(value):.4g}"


def _point_mm(value: Any) -> str:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return "unknown"
    def _one(c) -> str:
        # A centroid on the origin measures as -5.9e-10 m, which rounds to the string "-0". The
        # sign is real arithmetic and meaningless at this precision, and a customer reading a minus
        # on a coordinate reasonably asks what it means, so anything under half the last printed
        # digit is written as zero.
        mm = float(c) * 1000.0
        return f"{0.0 if abs(mm) < 0.5 else mm:.0f}"

    try:
        return "(" + ", ".join(_one(c) for c in value) + ")"
    except (TypeError, ValueError):
        return "unknown"


def _point_file(value: Any) -> str:
    """A centroid in the file's own numbers, for a file whose unit nobody has confirmed."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return "unknown"
    try:
        # Four significant figures for the same reason as `_file`, and an exact zero for a coordinate
        # that is a rounding artefact of the origin rather than a real offset.
        return "(" + ", ".join(
            "0" if abs(float(c)) < 1e-9 else f"{float(c):.4g}" for c in value) + ")"
    except (TypeError, ValueError):
        return "unknown"


def _facing(row: dict) -> str:
    """Where the mouth points, in the words a person would use, and never as a claim about a role."""
    normal = row.get("normal")
    tilt = row.get("tilt_deg")
    axis = ""
    if isinstance(normal, (list, tuple)) and len(normal) == 3:
        try:
            values = [float(c) for c in normal]
        except (TypeError, ValueError):
            values = []
        if values:
            i = max(range(3), key=lambda k: abs(values[k]))
            axis = f"along {'-+'[values[i] >= 0]}{'XYZ'[i]}"
    if isinstance(tilt, (int, float)) and float(tilt) >= 1.0:
        off = f"{float(tilt):.1f} degrees off the grid"
        return f"{axis}, {off}" if axis else off
    side = row.get("bbox_side")
    if axis and side:
        return f"{axis}, flat on the {side} face"
    return axis or "unknown"


def _port_size(row: dict) -> Any:
    """The bore, and the bore is what a person means by the size of a port.

    `bore_diameter` is the hole the fluid goes through; `hydraulic_diameter` on a ring is computed
    over the flange disc and is a different number. 596 of 602 declared bores in the corpus match
    `bore_diameter` within 6 percent, so this is the field that agrees with what customers write.
    """
    return _first_positive(row, ("bore_diameter_m", "min_dimension_m", "hydraulic_diameter_m"))


def _port_size_file(row: dict) -> Any:
    """The same bore in the FILE'S OWN numbers, for showing only, never for binding.

    Kept apart from `_port_size` on purpose. A declared `diameter_mm` is millimetres, and comparing
    it against a number whose unit nobody has stated is the 1,000x mistake this whole path is built
    to avoid - so the binding authority above stays metres-only and returns None when there is no
    scale, while the table below can still say what it measured.
    """
    return _first_positive(row, ("bore_diameter_file", "min_dimension_file",
                                 "hydraulic_diameter_file"))


def _first_positive(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return value
    return None


def opening_rows(document: dict | None) -> list[dict]:
    """The measured openings, widest first, each already in the words the table uses."""
    if not isinstance(document, dict):
        return []
    rows = []
    for row in document.get("openings") or []:
        if not isinstance(row, dict):
            continue
        rows.append({"id": str(row.get("id") or ""), "size_m": _port_size(row),
                     #: The same measurement without a unit. An STL declares none, so on those files
                     #: this is the only size there is - and it is a real one: the RATIOS between
                     #: openings are exactly what the conversation needs, and they are scale-free.
                     "size_file": _port_size_file(row),
                     "where": _point_mm(row.get("centroid_m")),
                     "where_file": _point_file(row.get("centroid_file")),
                     "facing": _facing(row),
                     "planar": bool(row.get("planar")), "kind": str(row.get("kind") or "")})
    # Widest first, by whichever of the two is present. Both are the same quantity and the order is
    # the same either way, so a file with no confirmed unit still gets its ports at the top.
    rows.sort(key=lambda r: -float(r["size_m"] or r["size_file"] or 0.0))
    return rows


#: What intake still asks for itself, with the survey off. Byte for byte what shipped before it.
_UNSURVEYED_ASKS = (
    "  - What the file CANNOT say, and what is therefore still worth asking: what is flowing and how",
    "    fast; what they want to learn; their cell budget; the engine, by the ENGINE FIRST policy",
    "    above, which this block does not modify; and WHICH of two openings of the same size and",
    "    class is the inlet. That last one is a real refusal, not a courtesy: twin feeds carrying hot",
    "    and cold streams are identical here and no check downstream could catch a swap.",
    "  - You may ask TWO questions in one message when one of them is the port identity above and the",
    "    other is the fluid. Holding a measurement, a second question costs a round and risks nothing.",
)

#: With the survey on, the port question is not intake's to compose. It is the Surveyor's, and it is
#: below, in the Surveyor's words, with the options the measurement allows.
_SURVEYED_ASKS = (
    "  - What the file CANNOT say, and what is therefore still worth asking: what is flowing and how",
    "    fast; what they want to learn; their cell budget; the engine, by the ENGINE FIRST policy",
    "    above, which this block does not modify. WHICH opening is which is NOT your question to",
    "    compose: it is the Surveyor's, below, and you put it in the Surveyor's words.",
    "  - PUT THE WHOLE SETUP IN ONE MESSAGE, AS DECISIONS, NOT AS QUESTIONS. Do not ask the Surveyor's",
    "    questions one at a time. State your best answer to EVERY open one at once - which mouth is the",
    "    inlet, which are outlets, which side the fluid is on, the fluid, the engine - each with a short",
    "    reason from the measurement, and close with one question: anything to change, or shall I go?",
    "    A customer answering eleven questions in eleven messages is this product failing, not working.",
    "  - ONE YES SETTLES THE WHOLE BLOCK. When they agree to a setup you have just LISTED IN FULL, record",
    "    EVERY item in it, quoting their agreement - \"go\", \"yes\", \"looks good\" - as the words for each.",
    "    That is their confirmation of a specific proposal they could read, which is exactly what the",
    "    record is for. Do NOT make them restate the items back to you.",
    "  - ONLY WHAT YOU ACTUALLY SHOWED THEM. An item you did not list in the message they are answering",
    "    was not confirmed by that answer, and recording it would be putting a value in their mouth.",
    "    Show it, then record it - never the other way round.",
    "  - A CORRECTION CHANGES ONE LINE. If they change one item, change that item, restate the block in",
    "    two lines, and ask once. Never restart the questioning.",
    "  - WHEN THEY HAND IT TO YOU, TAKE IT. \"best practice\", \"you decide\", \"go ahead with what you",
    "    decided\" are instructions to proceed on your own judgement, not questions to bounce back. Use",
    "    the measurement and standard practice, say in one line what you chose, and move.",
    "  - IF WHAT THEY SAY DISAGREES WITH WHAT WAS MEASURED, SAY SO IN ONE LINE AND TAKE THEIRS. \"You said",
    "    500 mm; I measured that mouth at 194 mm - going with yours, tell me if that is wrong.\" They are",
    "    the authority on their own part. A silent override is a wrong mesh nobody was warned about, and",
    "    a refusal is a wall - naming the difference is neither.",
)


def render_block(document: dict | None, *, survey: dict | None = None, armed: bool = False) -> str:
    """The measurement, as the model's own knowledge of the part. Empty string when there is none.

    Empty is the whole fail-open contract at this boundary: with no block the system prompt is
    character-for-character what it is today, and intake asks what it has always asked.

    `armed` is the survey switched on for this conversation, and `survey` the stored survey state when
    one has been composed. Unarmed, this is byte for byte the block it was before the survey existed.
    Armed, three things change and nothing else: the representation line says what the measurement
    composed FOR THE CUSTOMER'S PURPOSE, and is left out until they have said one; intake is told the
    port question belongs to the Surveyor rather than to it; and the Surveyor's own questions follow.
    """
    if not isinstance(document, dict) or document.get("status") != "ok":
        return ""
    try:
        if not armed:
            return _render(document)
        composed = dict((survey or {}).get("composed_for") or {})
        shown = {k: v for k, v in document.items() if k != "representation"}
        if composed.get("representation"):
            shown["representation"] = composed["representation"]
        surveyor = survey_lines(survey)
        return _render(shown, surveyed=True) + ("\n" + "\n".join(surveyor) if surveyor else "")
    except Exception as exc:                       # noqa: BLE001 - a table is never worth a turn
        logger.warning("intake geometry brief: the table could not be rendered (%s)", exc)
        return ""


def _render(document: dict, *, surveyed: bool = False) -> str:
    rows = opening_rows(document)
    coords = document.get("coordinates") or {}
    bbox = document.get("bbox") or {}
    bodies = document.get("bodies") or {}
    unit = coords.get("unit")
    # WHETHER THE NUMBERS HAVE A UNIT, and it is a different question from whether they exist. An
    # STL declares no unit, so `extent_mm`, `centroid_m` and every `_m` field of the document are
    # null - but the geometry was measured, and the file's own numbers are real. They are shown as
    # what they are, in the file's units, because the ratios between them are scale-free and are
    # most of what the conversation needs; the absolute sizes are the one thing still to ask for.
    scaled = bool(coords.get("scale_to_metres"))
    extent = bbox.get("extent_mm") if scaled else bbox.get("extent_file")
    _key = "size_m" if scaled else "size_file"

    def _size_of(row: dict) -> float:
        value = row.get(_key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    widest = _size_of(rows[0]) if rows else 0.0
    major = [r for r in rows if r["planar"] and _size_of(r) >= widest * MINOR_OPENING_FRACTION]
    minor = [r for r in rows if r not in major]

    lines = ["\n\n## WHAT THE FILE IS - measured from the customer's own upload, before they said anything",
             "These numbers came off THEIR file. They are not a guess and not something the customer told you.",
             "State them; do not ask for them. The customer may still correct any of them and their word wins.",
             ""]
    if unit:
        basis = "declared by the file header" if coords.get("basis") == "occ_transfer" else str(coords.get("basis") or "")
        lines.append(f"  units:          {unit}{f' ({basis})' if basis else ''}")
    else:
        # The one size question that is still real. An STL declares no unit at all.
        lines.append("  units:          NOT DECLARED by this file - the coordinates mean nothing until "
                     "the customer says what they are. Ask.")
    if isinstance(extent, (list, tuple)) and len(extent) == 3:
        _extent_unit = " mm" if scaled else " in the file's own units"
        lines.append(f"  bounding box:   {extent[0]:.2f} x {extent[1]:.2f} x {extent[2]:.2f}{_extent_unit}")
    lines.append(f"  bodies:         {bodies.get('count', '?')}, "
                 f"{'watertight' if bodies.get('watertight') else 'not watertight'}")
    # Only when there is a sentence to say. The measurement answers `unknown` for a part it cannot
    # classify, and printing the word back tells the model a measurement produced something it
    # cannot read, which is worse than saying nothing: the representation is then simply one more
    # thing the file did not settle.
    _representation = _REPRESENTATION_WORDS.get(document.get("representation") or "")
    if _representation:
        lines.append(f"  the file holds: {_representation}")
    names = document.get("names") or []
    if names:
        lines.append(f"  named regions:  {', '.join(str(n) for n in names[:12])}")
    lines.append("")

    if major:
        lines.append(f"  {len(rows)} openings measured. The candidate ports:")
        lines.append("")
        _where = "where (mm)" if scaled else "where (file units)"
        _size = "size" if scaled else "size (file units)"
        lines.append(f"  | id | {_where} | {_size} | facing |")
        lines.append("  |----|------------|------|--------|")
        for row in major[:MAX_TABLE_ROWS]:
            where = row["where"] if scaled else row["where_file"]
            size = _mm(row["size_m"]) if scaled else _file(row["size_file"])
            lines.append(f"  | {row['id']} | {where} | {size} | {row['facing']} |")
        lines.append("")
    if minor:
        lines.append(f"  {len(minor)} further opening(s) are small or irregular. They are meshed as wall "
                     "unless the customer says otherwise - say so once, do not list them.")
        lines.append("")
    if not rows:
        # SAID OUT LOUD, because zero is a measurement and the list below tells the model not to ask
        # for an opening count "written above". On a closed body there is nothing written, and the
        # fact itself is worth having: no ports means no inlet, no outlet and an external domain.
        lines.append("  NO openings were measured. This is a closed body with no ports, so there is no inlet "
                     "and no outlet to identify: it is meshed inside a far-field domain, or it is the wrong "
                     "file. Say so, and let the customer correct you if it is the wrong file.")
        lines.append("")

    lines.extend([
        "HOW TO USE THIS - it changes what you ASK, never what you SUBMIT:",
        "  - CONFIRM, do not interrogate. Open by telling the customer what you measured, then ask only",
        "    what the file cannot answer. Do not ask for a bore, a coordinate, a bounding box, an axis,",
        "    an opening count or a body count that is written above.",
        *([] if scaled else [
            "  - THE SIZES ABOVE HAVE NO UNIT. This file declares none, so every number in the table is in",
            "    the file's own coordinates: the SHAPE, the opening count, the positions relative to each",
            "    other and the ratios between the bores are all measured and true, and not one absolute",
            "    length is. So the size question is still open and you must ask it - state the ratios you",
            "    can see (\"the widest opening is about three times the others\") and ask what unit the file",
            "    is in, or what one real dimension actually measures. Until they answer, put NO length in a",
            "    patch: a millimetre field filled from an unconfirmed file is wrong by a factor of 1,000",
            "    exactly as often as it is right.",
        ]),
        *(_SURVEYED_ASKS if surveyed else _UNSURVEYED_ASKS),
        "  - The table is MEASUREMENT, not identity. It says how wide an opening is and where it sits. It",
        "    does not say which is the inlet, what the part is for, or what flows through it.",
        "  - submit_requirements is UNCHANGED. Every rule about it still holds, including: never invent a",
        "    dimension, location or interchangeability the customer did not state. A measured number is",
        "    not invented and it is not theirs either - so you may SAY it and ask them to confirm it, and",
        "    you may put it in a patch only once they have confirmed it in their own words.",
        "  - AND THEN ACTUALLY PUT IT THERE. Confirming a size in conversation does not submit it. The",
        "    moment the customer confirms a port's measured size or position, WRITE it into that patch",
        "    (diameter_mm, or area_mm2, or width_mm+height_mm, or near_mm) in the payload you submit.",
        "    A submission refused for a port with \"no size or location\" after they have confirmed one",
        "    is you failing to carry the number across - not them failing to answer. Do not ask again.",
    ])
    lines.extend(look_lines(document))
    return "\n".join(lines)


# -------------------------------------------------------------------------------------------------
# WHAT THE PART LOOKS LIKE
#
# A second, separate block, and separate is the point. Everything above is a measurement of the file.
# This is a vision model's words about rendered views of it: no number, nothing that can be checked,
# and a per-field record of how often each thing it says has been right.
#
# THE TIERS ARE NOT THIS MODULE'S. The measurement package measured them over 322 parts and carries
# them in the stored block; `geometry_agent.vision.trust.for_model` is what strips the fields that only
# ever lied before anything here can see them. So `defects` cannot reach a customer through this block
# - it is not in the dict that arrives - and nor can a guessed inlet, which is the field that would do
# the most damage in a conversation whose whole job is to settle which opening is which.
# -------------------------------------------------------------------------------------------------

#: How many phrases of one candidate list are worth showing. A model that lists nine internal features
#: from one look is listing wordings, not findings (Jaccard 0.31 to 0.46 on a repeat).
MAX_LOOK_PHRASES = 4


def look_at_it(document: dict | None) -> dict | None:
    """What the look may say to a model, or None. Never raises.

    None is the fail-open at this boundary, and it is the answer to all of: the vision setting off, a
    look that was never attempted, a look that failed, a row written before the look existed, and an
    image without the measurement package. In each case the block below is not rendered and the system
    prompt is character-for-character the prompt with no look.
    """
    if not isinstance(document, dict):
        return None
    look = document.get("look")
    if not isinstance(look, dict) or look.get("status") != "ok":
        return None
    block = (document.get("planner_block") or {})
    if isinstance(block, dict) and isinstance(block.get("look"), dict):
        # Composed once, at measurement time, by the package that owns the tiers. Preferred over
        # anything recomposed here, for the same reason the planner's block is.
        return block["look"]
    try:
        from geometry_agent.vision.trust import for_model
        return for_model(look.get("impression"), model=str(look.get("model") or ""))
    except Exception as exc:                       # noqa: BLE001 - a description is never worth a turn
        logger.info("intake geometry brief: the look could not be read (%s)", exc)
        return None


def _phrases(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "; ".join(items[:MAX_LOOK_PHRASES])
    if isinstance(value, dict):
        return "; ".join(f"{k} {v}" for k, v in list(value.items())[:MAX_LOOK_PHRASES])
    return str(value or "").strip()


#: One label column for both groups, so the two read as one table and a reader sees at a glance which
#: half a line is in.
_LABEL_WIDTH = 22


def _row(label: str, value: str) -> str:
    return f"  {label + ':':<{_LABEL_WIDTH}}{value}"


def look_lines(document: dict | None) -> list[str]:
    """The look, as lines appended under the measured table. Empty list when there is no look."""
    try:
        seen = look_at_it(document)
    except Exception:                              # noqa: BLE001 - never worth a turn
        return []
    if not seen:
        return []
    relied = seen.get("relied_on") or {}
    candidates = seen.get("candidates") or {}
    lines = ["",
             "## WHAT IT LOOKS LIKE - a vision model's words from rendered views of the same file",
             "This is NOT a measurement and it holds no number: every digit was removed before you saw it.",
             "The two groups below are not a tone, they are a measurement of how often each was right.",
             ""]
    strong: list[str] = []
    if relied.get("attachments"):
        strong.append(_row("flanges and fittings", _phrases(relied["attachments"])))
    if relied.get("openings_seen"):
        strong.append(_row("openings it can see", _phrases(relied["openings_seen"])))
    if relied.get("inside_is_plain"):
        strong.append(_row("inside the passage", "plain, nothing across it"))
    elif relied.get("internal_features"):
        strong.append(_row("inside the passage", _phrases(relied["internal_features"])))
    if strong:
        lines.append("  YOU MAY STATE THESE (measured to be right often enough to say out loud):")
        lines.extend(strong)
        lines.append("")
    weak: list[str] = []
    if candidates.get("looks_like"):
        weak.append(_row("it looks like", str(candidates["looks_like"])))
    for key, label in (("internal_features", "inside the passage"),
                       ("sharp_edges", "sharp edges and lips"),
                       ("thin_parts", "thin walls and plates"),
                       ("opening_mouths", "how the mouths sit")):
        if candidates.get(key):
            weak.append(_row(label, _phrases(candidates[key])))
    if weak:
        lines.append("  ASK, NEVER ASSERT (right often enough to be worth raising, not to be stated):")
        lines.extend(weak)
        lines.append("")
    lines.extend(placed_lines(seen))
    lines.extend([
        "  HOW TO USE IT: it changes what you can OFFER, never what you submit. Say what it saw and invite",
        "  a correction (\"it looks like a manifold with a flange at each end - is that right?\"). The",
        "  customer's word wins over it instantly and without argument. It does NOT tell you which opening",
        "  is the inlet, which way the part faces, whether it is symmetric or whether the file is sound:",
        "  the measured table above has the first three exactly, and the picture has been wrong about all",
        "  of them. Put nothing from here in a patch.",
    ])
    return lines


#: How many placed findings are shown. A draw places six to eight letters and up to four passage ends
#: on a part; this carries every one of them and stops a runaway reply from burying the table.
MAX_PLACED_LINES = 12


def placed_lines(seen: dict | None) -> list[str]:
    """What the look said AT a measured place: `at_places`, which the typed block has carried since
    round seven and this conversation never showed. Empty list when there are none.

    Each row joins the model's words to a place the MEASUREMENT chose and drew before the model saw
    the picture (`geometry_agent.vision.place`): a violet letter the renderer put at a point, or the
    end of a passage the stop detector found. So the place is a measurement and the words are not.
    The coordinate is the renderer's or the opening's centroid, in metres, and is shown in
    millimetres; a row with no coordinate says so rather than inventing one. A place the look declined
    to read is an answer, and it is said once for all of them.
    """
    rows = [r for r in ((seen or {}).get("at_places") or []) if isinstance(r, dict)]
    if not rows:
        return []
    said = [r for r in rows if not r.get("declined") and str(r.get("the_look_says") or "").strip()]
    declined = [str(r.get("at") or "") for r in rows if r.get("declined")]
    lines = ["  WHAT IT SAW AT MEASURED PLACES (the place was measured and drawn; the words are the model's):"]
    for row in said[:MAX_PLACED_LINES]:
        where = _point_mm(row.get("where_m"))
        where = f"{where} mm" if where != "unknown" else "no coordinate for this file"
        kind = "passage end" if row.get("kind") == "passage_end" else "marked place"
        lines.append(_row(f"{kind} {row.get('at')}", f"{str(row['the_look_says']).strip()} [{where}]"))
    if len(said) > MAX_PLACED_LINES:
        lines.append(f"  and {len(said) - MAX_PLACED_LINES} more places the planner receives in full.")
    if declined:
        lines.append(_row("could not read", ", ".join(d for d in declined if d)))
    lines.extend(["  Raise one of these only as a question about that place, never as a finding.", ""])
    return lines


# -------------------------------------------------------------------------------------------------
# THE SURVEYOR'S QUESTIONS
#
# With the survey on, the port question is not intake's to compose. The measurement and the look
# could not settle it, and their abstention IS the question, already formed by the measurement
# package with the options the measurement allows (`geometry_agent.contract.asking`). Intake puts it
# and posts the answer back with the customer's own words. This renders the stored survey state from
# `application/geometry_survey.py`; it writes no question of its own.
# -------------------------------------------------------------------------------------------------

#: How many open questions are shown at once. The chain puts the step-4 questions first and the
#: budget trade only after them, so this is never where a question is lost: an unshown one is shown
#: on the next turn.
MAX_SURVEY_QUESTIONS = 4


def survey_lines(state: dict | None) -> list[str]:
    """The Surveyor's block for the system prompt. Always a block when armed: before the survey
    exists it says how to get one, which is step 1 of the chain."""
    if not isinstance(state, dict) or not state.get("survey"):
        return ["", "## BEFORE YOU ASK ABOUT THE GEOMETRY - the Surveyor",
                "The measurement above was taken from the bytes before the customer said anything. What it",
                "MEANS depends on what the part is for, so as soon as the customer has said what the analysis",
                "is for, call survey_the_part with that purpose, their own words that said it, and any ports",
                "they named. It returns the questions the measurement and the look could not settle. Until",
                "then ask nothing about which opening is which: that is the Surveyor's question, not yours."]
    try:
        from meshpipeline.application import geometry_survey as gs

        views = gs.question_views(state)
        now = gs.open_now(state)
        confirmed = gs.confirmed_roles(state)
    except Exception as exc:                       # noqa: BLE001 - a question list is never worth a turn
        logger.warning("intake geometry brief: the survey could not be read (%s)", exc)
        return []
    lines = ["", "## THE SURVEYOR'S QUESTIONS - the only questions you ask about the geometry"]
    if confirmed:
        lines.append("  Already settled by the customer's own answers: "
                     + "; ".join(f"{oid} is the {role}" for oid, role in sorted(confirmed.items())) + ".")
    unsettled = [v for v in views if v["route"] in ("intake", "trade", "late")
                 and v["status"] in ("skipped", "defaulted")]
    if unsettled:
        lines.append("  Put and NOT settled (a default that stood is not an answer): "
                     + ", ".join(v["id"] for v in unsettled) + ".")
    if not now:
        lines.extend([
            "  Nothing is left to ask about the geometry. Put exactly the confirmed roles on exactly those",
            "  mouths when you submit: submit_requirements refuses a port whose role the customer did not",
            "  confirm, and a port that does not bind to the mouth they named."])
        return lines
    trade = now[0]["route"] == "trade"
    late = now[0]["route"] == "late"
    lines.extend([
        "  " + ("ONE MORE, and only this one: the cost of resolving the part against the budget they stated."
                if trade else
                "ONE MORE, and only this one, now that the geometry agent has planned the part: what resolving "
                "the places its plan names costs against the budget they stated. Put it once."
                if late else
                "The measurement and the look could not settle these. Put them in these words or close to them."),
        "  When the customer answers, call answer_survey_question with the id, the option EXACTLY as listed,",
        "  and their own words quoted exactly. If they decline, record it with skipped. If they tell you to",
        "  take the default, record took_default: that is NOT an answer, and the question stays open.",
        ""])
    for v in now[:MAX_SURVEY_QUESTIONS]:
        lines.append(f"  [{v['id']}] {v['text']}")
        lines.append(f"      options: {', '.join(v['options'])}")
        if v["about"] == "opening.role":
            # THE TWO SHAPES A ROLE QUESTION COMES IN, said in the words the tool takes. The step-4 finder puts
            # ONE question naming every unplaced mouth whose options are the ROLES, so the model has to say
            # which mouth as well as which role; the older shape lists the mouths as its options instead.
            roles = set(v["options"]) & {"inlet", "outlet", "wall", "closed for this run"}
            lines.append("      the table above has each mouth's position, size and facing. "
                         + (f"The mouths this asks about are {', '.join(v['subjects'])}: call the tool once per "
                            "mouth with `option` the role and `mouth` its id, and it is not settled until every "
                            "one of them has a role" if roles else
                            "The options are the mouth ids; give the role for each mouth they name"))
    return lines


#: What each representation means in a sentence a customer would recognise. The word itself is the
#: measurement package's; these are how it is said out loud.
_REPRESENTATION_WORDS = {
    "wall_shell": "the solid WALL of a passage - the fluid is the cavity inside it, which the mesher carves",
    "fluid_domain": "the FLUID volume itself, already carved",
    "annular_fluid": "an ANNULAR fluid passage between an inner body and an outer wall",
    "external": "a solid BODY to be meshed inside a far-field box",
}


# -------------------------------------------------------------------------------------------------
# BINDING A DECLARED PATCH TO A MEASURED OPENING
#
# This is what makes `contracts/rationale.py`'s sentence true. It has told every customer since the
# product shipped that their boundary assignments "were checked against the geometry"; nothing
# checked them, because the checker read the emptied staging directory. The foundations phase made
# the sentence say what happened instead. This makes it able to say the true version again, and only
# when it IS true: every declared inlet and outlet resolved to an opening that was actually measured.
# -------------------------------------------------------------------------------------------------

#: How close a declared coordinate has to be to a measured centroid, as a fraction of the body's
#: diagonal. 678 of 678 declared ports in the corpus bind to a measured centroid within 0.0083 mm,
#: so this is loose by orders of magnitude on purpose: it is here to catch a port on the wrong end of
#: the part, not to adjudicate a rounding difference.
NEAR_TOLERANCE_OF_DIAGONAL = 0.02

#: How far a declared size may be from the measured bore and still be the same port. 596 of 602
#: declared bores match `bore_diameter` within 6 percent; this is that figure, doubled, for the same
#: reason as above.
SIZE_TOLERANCE = 0.12


def bind_patches(document: dict | None, patches: Any) -> dict:
    """Which declared flow boundaries resolved to a measured opening.

    Returns `{"checked": bool, "bound": [...], "unbound": [...], "openings": int}`. `checked` is the
    only thing a caller should act on and it is true only when there was a measurement, there was at
    least one declared inlet or outlet, and every one of them bound. Anything else is false, because
    a partial check is not a check and the sentence it would license is the one this exists to stop.
    """
    result: dict[str, Any] = {"checked": False, "bound": [], "unbound": [], "openings": 0}
    if not isinstance(document, dict) or document.get("status") != "ok":
        return result
    rows = [r for r in (document.get("openings") or []) if isinstance(r, dict)]
    result["openings"] = len(rows)
    declared = [p for p in (patches or [])
                if isinstance(p, dict) and str(p.get("type", "")).strip().lower() in ("inlet", "outlet")]
    if not rows or not declared:
        return result
    diagonal = (document.get("bbox") or {}).get("diagonal_m")
    for patch in declared:
        match = _match(patch, rows, diagonal)
        (result["bound"] if match else result["unbound"]).append(
            {"name": str(patch.get("name") or ""), "opening_id": match})
    result["checked"] = not result["unbound"]
    return result


def _stated_point(patch: dict) -> list[float] | None:
    """The location the customer stated, in metres, or None when they stated none this can read.

    None means "no position was given", which is what lets `_match` fall back to the bore. A
    `near_mm` that is present but unreadable is the same thing: nothing usable was stated.
    """
    near = patch.get("near_mm")
    if not isinstance(near, (list, tuple)) or len(near) != 3:
        return None
    try:
        return [float(c) / 1000.0 for c in near]
    except (TypeError, ValueError):
        return None


def _match(patch: dict, rows: list[dict], diagonal: Any) -> str | None:
    """The opening a declared patch names, by position when one is stated and by size when none is.

    POSITION IS NOT A PREFERENCE, IT IS THE ANSWER. Where a customer states `near_mm` the question
    "which hole did they mean" is settled by that point and by nothing else, so a stated position
    that resolves to no opening is an UNBOUND patch and never a patch rebound by its bore.

    That distinction is the whole value of this function. `agents/intake/validation.py:275-282`
    requires a size on every declared port, so every real patch carries one; a fall-through to size
    would therefore mean a port declared 9 metres from anything still bound - to the one opening of
    that bore - and `contracts/rationale.py` would tell the customer their assignments "were checked
    against the measured geometry". A check that cannot fail is the false sentence this phase exists
    to remove, one level further down.

    Size is the answer only for a patch that states no position at all, and then only when exactly
    one opening fits: two identical ports are the case no measurement can settle.
    """
    # NAMED BEATS INFERRED. When the patch says which measured opening it is, that IS the answer and
    # nothing is matched by bore or by distance. Everything below exists because this field did not:
    # size-matching cannot separate two 439 mm mouths, so the product interrogated the customer about
    # which was which and then refused the submission when neither could be told apart.
    named = str(patch.get("opening_id") or "").strip()
    if named:
        return named if any(str(r.get("id") or "").strip() == named for r in rows) else None
    point = _stated_point(patch)
    if point is not None:
        # A tolerance needs a body to be a fraction OF. Without one there is no check to make, and
        # an unchecked nearest is a guess wearing a verdict's clothes.
        if not (isinstance(diagonal, (int, float)) and not isinstance(diagonal, bool) and diagonal > 0):
            return None
        limit = float(diagonal) * NEAR_TOLERANCE_OF_DIAGONAL
        best, best_d = None, None
        for row in rows:
            centroid = row.get("centroid_m")
            if not isinstance(centroid, (list, tuple)) or len(centroid) != 3:
                continue
            try:
                d = sum((float(a) - b) ** 2 for a, b in zip(centroid, point, strict=True)) ** 0.5
            except (TypeError, ValueError):
                continue
            if best_d is None or d < best_d:
                best, best_d = str(row.get("id") or ""), d
        # An opening with no id cannot be named in a binding, so it is not one: `best` has to
        # carry an id, not merely be the nearest row.
        if best and best_d is not None and best_d <= limit:
            return best
        return None
    diameter = patch.get("diameter_mm")
    if isinstance(diameter, (int, float)) and not isinstance(diameter, bool) and diameter > 0:
        target = float(diameter) / 1000.0
        sized = [(r, _port_size(r)) for r in rows]
        candidates = [r for r, size in sized
                      if isinstance(size, (int, float))
                      and abs(float(size) - target) <= target * SIZE_TOLERANCE]
        # One candidate is a binding. Two identical ports are exactly the case no measurement can
        # settle, so this refuses rather than picking the first, which is what the builder's own
        # `engines/port_binding.py` already does for the same reason.
        if len(candidates) == 1:
            return str(candidates[0].get("id") or "")
    return None


__all__ = ["MAX_LOOK_PHRASES", "MAX_PLACED_LINES", "MAX_SURVEY_QUESTIONS", "MAX_TABLE_ROWS",
           "MINOR_OPENING_FRACTION", "NEAR_TOLERANCE_OF_DIAGONAL", "SIZE_TOLERANCE", "bind_patches",
           "look_at_it", "look_lines", "opening_rows", "placed_lines", "render_block", "survey_lines"]
