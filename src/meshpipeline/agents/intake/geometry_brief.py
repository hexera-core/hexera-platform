# Responsibility: Turn a stored geometry measurement into what intake may say and what it no longer has to ask.
# Owns: the opening table shown to the model, the Surveyor's questions as intake reads them, how long the chain's two slow turns are said to take, and the binding of a declared patch to a measured opening.
# Boundaries: it renders and it binds; it writes no question of its own, blocks nothing and changes no tool schema.
# Collaborates with: cad/regions.py for the document and contracts/rationale.py for what the customer is told.
from __future__ import annotations

import logging
from typing import Any

from meshpipeline.contracts.geometry_agent_block import platform_cell_ceiling, runnable_cell_estimates

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
    "  - WHEN THEY HAND IT TO YOU, TAKE IT - INCLUDING ON THESE QUESTIONS. \"best practice\", \"you decide\",",
    "    \"go ahead with what you decided\", \"I don't know\" are instructions to proceed on your own",
    "    judgement, not questions to bounce back. That applies to the Surveyor's questions too: call",
    "    answer_survey_question with the best-supported option and THEIR deferral as",
    "    customer_words_verbatim - \"you decide\" is their own message and the application accepts it.",
    "    Then say in one line which reading you took and why, and move on.",
    "  - ONE ASKING, THEN DECIDE. Put a Surveyor question once. If what comes back does not answer it -",
    "    a \"yes\" to an either/or, a shrug, a reply about something else - do NOT put it again. Record the",
    "    best-supported option with answer_survey_question (took_default, or their own words where they",
    "    gave any), say in one line which way you went and what would change it, and carry on. Asking an",
    "    eighth time is how a real conversation died without ever reaching a mesh.",
    "  - NEVER SAY YOU CANNOT DECIDE. Not \"only you can answer this\", not \"I'd be flipping a coin\", not",
    "    \"reply with one word: fluid or solid\". A real conversation was told that nine times running and",
    "    never reached a mesh. If a reading is genuinely uncertain, say which way you are going and WHY,",
    "    name what would change your mind, and go: \"Going with the file being the metal and the fluid",
    "    being the cavity inside - that is what the passage measures like. Say 'it is the fluid' if it",
    "    is the other way and I will switch it.\" A customer can correct one sentence. They cannot",
    "    correct a refusal.",
    "  - IF WHAT THEY SAY DISAGREES WITH WHAT WAS MEASURED, SAY SO IN ONE LINE AND TAKE THEIRS. \"You said",
    "    500 mm; I measured that mouth at 194 mm - going with yours, tell me if that is wrong.\" They are",
    "    the authority on their own part. A silent override is a wrong mesh nobody was warned about, and",
    "    a refusal is a wall - naming the difference is neither.",
)


def surveyor_panel(document: dict | None, survey: dict | None) -> str:
    """What the Surveyor saw, found and suggests, for the CUSTOMER, once the look has landed.

    THE SURVEYOR WAS INVISIBLE. It measured every upload and read seventeen rendered views of it,
    and the customer saw none of that - only questions, which read as an interrogation rather than
    as the output of something that had already done most of the work. This is the receipt.

    Three sections, and the separation is the point. MEASURED is arithmetic on their bytes and is
    simply true. SEEN is a vision model's words about pictures: it is labelled as a reading, never
    as a measurement, because that is exactly what the confidence cap in the plan checker is about.
    SUGGESTS is what the two together imply, offered for correction rather than stated as fact.

    Empty until there is a look worth showing, so a conversation where nothing was looked at reads
    exactly as it did before this existed.
    """
    if not isinstance(document, dict) or document.get("status") != "ok":
        return ""
    look = document.get("look") if isinstance(document.get("look"), dict) else {}
    if str(look.get("status") or "") != "ok":
        return ""
    imp = look.get("impression") if isinstance(look.get("impression"), dict) else {}
    rows = [r for r in (document.get("openings") or []) if isinstance(r, dict)]
    out = ["", "- - - THE SURVEYOR - - -", ""]

    out.append("MEASURED (arithmetic on your file, not an opinion)")
    ext = ((document.get("bbox") or {}).get("extent_m")) or []
    declared = str((document.get("unit") or {}).get("declared") or "").strip()
    if declared and len(ext) == 3:
        out.append("  {:,.0f} x {:,.0f} x {:,.0f} mm".format(*[float(x) * 1000.0 for x in ext]))
    out.append("  {} opening(s): {}".format(len(rows), ", ".join(str(r.get("id")) for r in rows) or "none"))

    seconds = look.get("seconds")
    head = "SEEN (a reading of 17 rendered views - words, never numbers"
    head += f", {float(seconds):.0f}s)" if isinstance(seconds, (int, float)) else ")"
    out.append("")
    out.append(head)
    if imp.get("looks_like"):
        out.append("  looks like   {}".format(imp["looks_like"]))
    if imp.get("confidence"):
        out.append("  its own confidence  {}".format(imp["confidence"]))
    for edge in (imp.get("sharp_edges") or [])[:3]:
        out.append(f"  sharp        {edge}")
    notes = str(imp.get("notes") or "").strip()
    if notes:
        out.append(f"  notes        {notes[:260]}")

    roles = [o for o in (imp.get("openings_seen") or []) if isinstance(o, dict)]
    not_ports = [str(o.get("id")) for o in roles if str(o.get("likely_role") or "") == "not a port"]
    out.append("")
    out.append("SUGGESTS (my reading - correct any of it and I will use yours)")
    purpose = str(((survey or {}).get("composed_for") or {}).get("purpose") or "")
    # ONLY A FLOW CASE HAS FLOW IN IT. "no duct mouth, so this reads as flow AROUND the body" is a
    # useful sentence for CFD and a nonsense one for a structural or thermal job, and the purpose
    # enum has both. What the openings ARE is worth saying either way; what a fluid does is not.
    is_flow = purpose in ("internal_cfd", "external_cfd", "conjugate_heat_transfer")
    rep = str(((survey or {}).get("composed_for") or {}).get("representation") or "").replace("_", " ")
    # "this is      unknown" is what the customer actually read on a real upload. It is the absence of
    # a reading printed as though it were one, in the section headed "my reading", under a panel whose
    # whole claim is that the Surveyor had already done the work. A row we cannot fill is a row we do
    # not print.
    if rep and rep != "unknown":
        out.append(f"  this is      {rep}")
    if not_ports and len(not_ports) == len(rows) and rows:
        line = f"  the {len(not_ports)} opening(s) above do NOT look like ports - no duct mouth behind them"
        if is_flow:
            line += ", so this reads as flow AROUND the body rather than through it"
        out.append(line)
    elif not_ports:
        out.append("  {} of the openings do not look like ports: {}".format(len(not_ports), ", ".join(not_ports)))
    elif rows and not roles:
        # THE READING DISAGREED WITH ITSELF AND ONLY THE HALF NOBODY CHECKED WAS BELIEVED.
        #
        # MEASURED on ahmed_variant_001, an external-flow bluff body. The look's own prose said "solid
        # material throughout, with no visible internal flow passage, cavity, or obstruction. The
        # labelled openings do not resolve as visible mouths in these views and may be topology or
        # open-face detections." Its `openings_seen` came back empty. The gate below reads
        # `likely_role == "not a port"` and nothing else, so neither line printed, that sentence went
        # past as a `notes` row, and the next thing the customer was asked was which of the four
        # mouths is the inlet - with o4 proposed as inlet and o1..o3 as outlets, on a solid block.
        #
        # The panel reporting the reading had the same blind spot as the reading. That is the shape
        # that keeps recurring here, and the check has to be able to fail differently from the thing
        # it checks - so this one asks the measurement, not the look: there are openings and the look
        # returned no role for any of them.
        #
        # AND IT SAYS "COULD NOT CONFIRM", NEVER "NOT A PORT". An empty field is not a denial, and
        # spending it as one would be a fact that lies - the thing this codebase is most careful about.
        # Unconfirmed is true, it is the customer's to correct, and it is the sentence that would have
        # stopped four ports being proposed on a solid block.
        line = (f"  the look could not confirm any of the {len(rows)} opening(s) above as a duct mouth"
                )
        if is_flow:
            line += " - say if the flow really does go through one of them"
        out.append(line)
    # HOW BIG THE MESH WILL BE, SAID RATHER THAN ASKED. The measurement forecasts a cell count per
    # tier, and the customer never saw any of it: the intake is forbidden to ask for a cell count
    # ("a QUALITATIVE preference, never a cell count"), which is right - a number pulled out of a
    # customer is not a budget - but the consequence was that mesh size was never discussed at all
    # and they found out afterwards. Stating the forecast and naming the two words that change it
    # gives them the decision without the interrogation.
    #
    # AND ONLY THE TIERS THIS PLATFORM WILL ACTUALLY RUN. The forecast is the measurement's, sized from
    # the part, and it knows nothing of the compute ceiling: on a part with fine features `max` came out
    # at eight figures against a CELL_HARD_LIMIT of 8,000,000. Offering it read as a choice, and it was
    # not one - the clamp in the snappy driver cuts the budget down silently, downstream of the customer
    # saying yes. So a tier over the ceiling is not offered, and when the tier we would otherwise quote
    # is itself over it, the ceiling is what gets quoted, named as a ceiling.
    all_tiers = [e for e in ((document.get("facts") or {}).get("cell_estimates") or []) if isinstance(e, dict)]
    tiers, beyond = runnable_cell_estimates(all_tiers)
    by_tier = {str(e.get("tier")): e.get("cells") for e in tiers if e.get("cells")}
    if by_tier.get("standard"):
        line = "  mesh size    about {:,.0f} cells at standard".format(float(by_tier["standard"]))
        spare = [f"{t} about {float(by_tier[t]):,.0f}" for t in ("draft", "max") if by_tier.get(t)]
        if spare:
            line += " ({}) - say {} to change it".format(
                "; ".join(spare), " or ".join(t for t in ("draft", "max") if by_tier.get(t)))
        out.append(line)
    elif beyond:
        # Every tier is over the ceiling, `standard` among them. Saying nothing here would leave the
        # customer to discover the size of their mesh from the result, which is the thing this block
        # exists to stop.
        out.append("  mesh size    this part forecasts about {:,.0f} cells at its coarsest, over the "
                   "{:,.0f} I can mesh - I will hold it at {:,.0f} and resolve the largest features "
                   "first".format(min(float(e["cells"]) for e in beyond if e.get("cells")),
                                  float(platform_cell_ceiling()), float(platform_cell_ceiling())))

    out.append("")
    return chr(10).join(out)


def render_block(document: dict | None, *, survey: dict | None = None, armed: bool = False) -> str:
    """The measurement, as the model's own knowledge of the part. Empty string when there is none.

    Empty is the whole fail-open contract at this boundary: with no block the system prompt is
    character-for-character what it is today, and intake asks what it has always asked.

    `armed` is the survey switched on for this conversation, and `survey` the stored survey state when
    one has been composed. Unarmed, this is byte for byte the block it was before the survey existed.
    Armed, four things change and nothing else: the representation line says what the measurement
    composed FOR THE CUSTOMER'S PURPOSE, and is left out until they have said one; intake is told the
    port question belongs to the Surveyor rather than to it; the Surveyor's own questions follow; and
    `waiting_lines` names the chain's two slow turns, which exist only when it is armed.
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
        # `waiting_lines` is added, never substituted: `survey_lines` returns [] when the stored
        # survey cannot be read, and the two waits are still real on that turn - the look still
        # takes half a minute whether or not its questions could be rendered. So it is appended
        # outside the survey's own success, which is the same fail-open shape as everything here.
        surveyor = survey_lines(survey) + waiting_lines()
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
# WHAT THE CUSTOMER IS WAITING FOR
#
# Two turns of an armed conversation are slow, both of them deliberately, and until this block
# existed neither was ever mentioned to the customer - not before, not while, not after. Driving the
# real stack twice against ahmed_variant_001.step: the turn that calls `survey_the_part` took 33s
# both times (`the look landed after 35s`, and 28s on the second run), and the turn that calls
# `submit_requirements` took 82s and 90s (`geometry_agent run.end seconds=78.1`, and 61.9s).
#
# NEITHER WAIT IS A BUG AND THIS BLOCK DOES NOT SHORTEN EITHER. The look is queued BY the survey
# call, so it cannot start earlier, and waiting for it is what stops a survey being composed from a
# part nobody has looked at - the defect that threw the plan away run after run. The geometry agent
# plans the whole part before compute is spent, which is the point. What this block changes is the
# only thing left that can change: whether the customer knows the wait is coming, and whether the
# turns AROUND it were worth spending. On the measured run eleven customer messages reached one
# mesh in three minutes, and three of those turns only re-asked a question that had already been
# put once - 24 seconds of round trips that bought nothing, which is the part that actually feels
# slow. A wait you were told about and that comes back with the whole setup does not.
# -------------------------------------------------------------------------------------------------

#: How long the two waits are SAID to be. Rounded up to the measured worst case, and phrased rather
#: than printed as a number, because a prompt figure is a claim about this system and the first house
#: law covers it exactly as it covers a bore: a figure that lies is worse than no figure. The look
#: measured 26s, 28s and 35s; the plan 61.9s and 78.1s inside turns of 82s and 90s. They live here,
#: in one place, beside what measured them, and the model is told to quote them and never to invent
#: one of its own - a model promising "just a second" for half a minute is the lie this prevents.
LOOK_WAIT_SAID = "about half a minute"
PLAN_WAIT_SAID = "up to a minute and a half"

#: THE TWO WAITS IN ONE TURN, which is now the ordinary shape of a delegated conversation: the survey
#: call queues and waits for the look, and where the customer has already handed over the decisions the
#: same turn settles every question, submits, and plans. MEASURED on ahmed_variant_001 across the two
#: turns it used to take: 29.9s + 81.4s and 34.5s + 74.3s, so 111.3s and 108.8s of the same work with a
#: round trip taken out of the middle. Rounded up to the worst of those, and phrased rather than printed
#: for the reason above.
BOTH_WAITS_SAID = "about two minutes"


def waiting_lines() -> list[str]:
    """The two slow turns and when to mention them, as lines for the system prompt.

    Armed conversations only, and that is not a hedge: both waits belong to the survey chain and
    exist only when it is on. With the survey off `survey_the_part` is not offered, nothing queues a
    look, `geometry_step` is off and the submit turn is the submit turn it always was - so a prompt
    that named these waits there would be naming waits that never happen, which is the first house
    law with the sign flipped.
    """
    return [
        "",
        "## WHAT THE CUSTOMER IS WAITING FOR - say it BEFORE it happens, never after",
        "Two turns here are slow on purpose and neither can be made faster from where you sit. What you",
        "control is whether the wait was announced and whether the turns around it were worth spending.",
        f"  - READING THE PART: the turn you call survey_the_part on waits {LOOK_WAIT_SAID} while 17 rendered",
        "    views are read. It is queued by that call, so it cannot start any earlier.",
        f"  - PLANNING IT: the turn you call submit_requirements on takes {PLAN_WAIT_SAID}, because the",
        "    geometry agent plans the whole part before any compute is spent. Its findings come back with",
        "    the confirmation, so that wait is also the most valuable message in the conversation.",
        f"  - THOSE ARE ONE TURN WHENEVER THEY HAVE HANDED YOU THE DECISIONS, and then it is "
        f"{BOTH_WAITS_SAID}: you",
        "    survey, settle every Surveyor question with accepted_proposal, preview and submit, all in the",
        "    turn their delegation arrives in. That is the shape to aim for - the customer gets the reading,",
        "    the setup, what the geometry agent found and what it flagged, and one ask, in one message. Two",
        "    turns of the same work cost the same seconds plus a round trip, and put the plan's warnings a",
        "    message after the reading they are about.",
        "  - USE THOSE WORDS FOR THE LENGTHS. Do not invent a figure, do not say \"a moment\" or \"just a",
        "    second\" for something that takes half a minute, and never promise faster than this says.",
        # THE TENSE IS A FACT AND IT WAS WRONG ON THE FIRST RUN THAT WARNED AT ALL. The model wrote "I've
        # gone ahead and read your part. It takes about half a minute to finish the look ... While that
        # runs, here's everything the-". Nothing was running: the look had finished inside the tool call
        # before a word of that message was written, and nothing the model writes reaches the customer
        # until the whole turn is over. So the customer read a message telling them to wait while
        # something happened that had already happened, which is the first house law broken about this
        # system rather than about their file.
        "  - A WAIT YOU HAVE ALREADY SERVED IS NEVER DESCRIBED AS RUNNING. Nothing you write reaches the",
        "    customer until your turn is completely finished, so there is no \"while that runs\", no \"one",
        "    moment\", no \"I'll come back when it lands\" - by the time they read the sentence, it landed. A",
        "    wait that is behind you is spoken of in the past and only if it is worth a clause (\"read the",
        "    shape - here is what it says\"); a wait that is ahead of you, in a LATER turn, is the only kind",
        "    you announce. Getting that tense wrong tells the customer to sit still for something that is",
        "    already done.",
        "  - SAY IT IN THE TURN BEFORE. \"Tell me which and I'll read the shape - about half a minute - then",
        "    come back with the whole setup\" costs nothing and turns a blank screen into a wait for",
        "    something. A customer who was told is not waiting; one who was told nothing is wondering",
        "    whether it broke.",
        "  - NEVER SPEND A TURN ONLY TO WARN. Put it inside a turn you were already spending on a question",
        "    you actually need. A round trip added to announce a round trip is worse than the silence.",
        "  - ASK EARLY WHATEVER THE SLOW STEP DOES NOT NEED. The look needs the purpose and nothing else:",
        "    what is flowing, how fast, what they want to learn, their budget, and whether they already",
        "    work in a particular mesher are ALL free to ask before it. Asked before, their answer arrives",
        "    with the slow reply; asked after, each one costs another round trip.",
        "  - WHERE YOU DO STILL HAVE TO ASK BEFORE SUBMITTING - something only they can give - SAY WHAT GO",
        f"    DOES: it runs the plan, so the next message takes {PLAN_WAIT_SAID} to come back and carries",
        "    what the plan found. Tell them that when you ask, not while they are sitting through it.",
        "  - A WASTED TURN COSTS MORE THAN A SLOW ONE. Three seconds spent re-asking something already",
        "    answered is worse than thirty spent reading their part, because the thirty bought something.",
        "    Every rule above about asking once, deciding on a deferral, and putting the whole setup in one",
        "    message is a latency rule as much as a manners one.",
        # THE MEASURED SHAPE OF THE WASTE, and it is not the model forgetting to decide - it decides,
        # and then spends a whole turn announcing the decision and asking whether the decision is
        # right. Turn 3 of the run after the warning landed: "So I'm going with external flow ... Does
        # that match what you're doing?" That is the same either/or with the question mark moved, it
        # invites another "yes" that settles nothing, and it cost a round trip. Turn 6 of the same run
        # refused outright - "two items in my last message were actual questions ... I'd be guessing
        # where I shouldn't" - which is the one thing the rules above forbid in capitals. Both are
        # cured by the same mechanical test, which is why it is written as a test and not as advice.
        "  - THE TEST FOR A TURN YOU ARE ABOUT TO WASTE: if your LAST message ended in a question and what",
        "    came back does not answer it - a \"yes\" to an either/or, a shrug, a reply about something else",
        "    - then your next message contains NO question mark about that item. None. Not the same",
        "    question, and not \"does that match what you're doing?\", which is the same question with the",
        "    question mark moved and invites another yes that settles nothing.",
        "  - AND THE DECISION DOES NOT GET A TURN OF ITS OWN EITHER. Do not send a message whose only",
        "    content is which way you went. Fold that line into the next thing you were going to send -",
        "    the full setup, the next open question, the submission - so the decision costs no round trip",
        "    at all: \"Going with external flow, since nothing sits behind those mouths. Here is the whole",
        "    setup: ...\". Announcing a decision and then asking for the setup separately is two waits for",
        "    one message.",
        "  - YOU ARE NOT GUESSING, SO DO NOT SAY YOU WOULD BE. \"I'd be guessing where I shouldn't\" is not a",
        "    reason to put a question again: you are holding a measurement of their file and a reading of",
        "    17 views of it, so taking the best-supported option and naming it out loud is the opposite of",
        "    a guess. Guessing is putting a value nobody confirmed into a patch - which the tools refuse",
        "    for you, and which is why deciding in conversation is safe and costs the customer nothing.",
    ]


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
                "MEANS depends on what the part is for, so as soon as the customer has said which ONE of the",
                "purposes this is for, call survey_the_part with that purpose, their own words that said it,",
                "and any ports they named. It returns the questions the measurement and the look could not",
                "settle - and ONE of the purposes is what it takes, not merely a field they named. Until",
                "then ask nothing about which opening is which: that is the Surveyor's question, not yours.",
                # THIS CALL IS THE FIRST THING THE CUSTOMER EVER WAITS FOR, and on the two measured
                # conversations against ahmed_variant_001.step it was 33 seconds of blank screen with
                # nothing said about it either before or after. The wait itself is correct and stays:
                # the call QUEUES the look, so it cannot have started sooner, and a survey composed
                # before the look is a survey of a part nobody has seen. What was wrong is that the
                # turn immediately before it - "cfd" does not say internal or external, so there IS
                # one - was spent on a single question and told the customer nothing about what came
                # next. That turn is free: it is already being spent, and every question the look
                # does not need can ride in it.
                f"CALLING IT IS THE SLOW TURN: it queues the look and waits {LOOK_WAIT_SAID} for it, and that",
                "is the first thing the customer ever waits for. So if their words PIN ONE purpose - \"stress\"",
                "pins structural, \"flow through the manifold\" pins internal - call it NOW and do not spend a",
                "turn warning about a wait you could have started instead.",
                f"AND IF THEY HAVE ALSO HANDED YOU THE DECISIONS, THAT TURN GOES ALL THE WAY: {BOTH_WAITS_SAID},",
                "because you survey, settle every question the Surveyor raises, and submit in it, so the",
                "geometry agent plans the part there too and the customer gets the reading, the setup, what it",
                f"found and what it flagged, and one ask, in one message. Say \"{BOTH_WAITS_SAID}\" in the turn",
                "BEFORE that one, and quote that length rather than the half-minute.",
                # WHAT "HAS SAID WHAT IT IS FOR" ACTUALLY MEANS, and a real run needed it spelled out. Told
                # to survey as soon as the analysis was stated, the model surveyed on the bare word "cfd"
                # and then wrote the customer this: "'cfd' alone doesn't tell me internal vs external, and
                # I've gone ahead and read your part." It had composed the whole survey for internal_cfd on
                # a purpose it said in the same sentence that nobody had chosen. The quote check on
                # `customer_words_verbatim` cannot catch that - it verifies the words are theirs, never that
                # the words say THIS purpose - so it is the same blind spot as the thing it checks, and the
                # only place the distinction can be made is here, before the call.
                "BUT \"cfd\" ON ITS OWN PINS NOTHING: internal_cfd and external_cfd are different purposes, the",
                "measurement is composed FOR one of them, and the quote you pass cannot tell the application",
                "which one their words meant - it only checks that they said them. So never resolve that",
                "yourself and never survey on a guess. Where their words leave two purposes open, asking which",
                "is a real question with a real consequence, you are spending this turn on it regardless, and",
                "that turn is where the rest of it belongs: say that reading the shape takes about that long",
                "and comes back with the whole setup, and ask in the same breath for everything the look does",
                "NOT need. Their answer then arrives WITH the slow reply instead of costing another round trip",
                "after it.",
                # THE FIRST RUN AFTER THE WARNING LANDED STILL SPENT THAT TURN ON ONE QUESTION. It said
                # the half-minute out loud, which was the point, and then asked internal-or-external and
                # nothing else - so the fluid, the speed and the goal were still asked on later turns,
                # one per turn, exactly as before. Naming the list is the difference between a rule the
                # model agrees with and a turn that carries it.
                "PUT THIS WHOLE LIST IN THAT ONE MESSAGE, not one item per turn: which side of the wall the",
                "fluid is on (this is the one the purpose enum needs); what the fluid is and roughly how fast;",
                "what they want to learn from the run; and whether they already work in a particular mesher or",
                "would rather you picked. Four short lines and one closing question, each with your own best",
                "answer where you have one so they can correct rather than compose. Every one of them is an",
                "input you will need before you can submit and not one of them is an input to the look, so a",
                "turn that carries only the first has thrown the other three away."]
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
        # THE PROPOSAL IS THE APPLICATION'S AND IT IS ALREADY WRITTEN, so a customer who has handed you the
        # decision does not have to be asked. This is the owner's own design for the whole chain: "we anyway
        # confirm it with the user so only what passes there is used to make the plan". What went wrong without
        # it: the model proposed the setup, asked "anything to change, or shall I go?", the customer said "good
        # to go", and THEN the port roles were asked - because `role_problems` refuses any role the customer
        # did not confirm and a "go" was not something the model could record as one.
        "  IF THEY HAVE HANDED YOU THE DECISION, OR AGREE TO THE SETUP YOU LISTED, do not ask any of these.",
        "  STATE the `proposes` line below as your decision, in your own setup, and record it with",
        "  accepted_proposal: the application then records ITS OWN proposal as their answer and the question",
        "  is SETTLED - one call for the whole question, no option and no mouth. Never took_default on a role:",
        "  a default that stood is nobody answering, and the submission refuses a role nobody confirmed. A",
        "  question with no `proposes` line naming a place takes the option you read instead, as usual.",
        "  AND THEN FINISH THE JOB IN THIS SAME TURN: propose_engine_selection (a delegation accepts it, so",
        "  there is no engine question), preview_selected_admission, submit_requirements. Everything the plan",
        "  needs is now on the row, so the geometry agent plans HERE and its findings and warnings land beside",
        "  the reading they are about, under the setup you just wrote, with the one ask at the bottom. Stopping",
        "  to ask \"anything to change, or shall I go?\" first costs a round trip and then asks again.",
        ""])
    for v in now[:MAX_SURVEY_QUESTIONS]:
        lines.append(f"  [{v['id']}] {v['text']}")
        lines.append(f"      options: {', '.join(v['options'])}")
        # WHAT THE APPLICATION WOULD DO UNASKED, which the model could not see at all. It was inventing its
        # own reading of the mouths for the setup it proposed, so the sentence the customer read and the value
        # `accepted_proposal` records had no reason to agree. Shown, they are the same proposal.
        if v.get("default"):
            lines.append(f"      proposes: {v['default']}")
        if v["about"] == "opening.role":
            # THE TWO SHAPES A ROLE QUESTION COMES IN, said in the words the tool takes. The step-4 finder puts
            # ONE question naming every unplaced mouth whose options are the ROLES, so the model has to say
            # which mouth as well as which role; the older shape lists the mouths as its options instead.
            roles = set(v["options"]) & {"inlet", "outlet", "wall", "closed for this run"}
            lines.append("      the table above has each mouth's position, size and facing. "
                         + (f"The mouths this asks about are {', '.join(v['subjects'])}: call the tool once per "
                            "mouth with `option` the role and `mouth` its id, and it is not settled until every "
                            "one of them has a role" if roles else
                            "The options are the mouth ids; give the role for each mouth they name")
                         + (" - or once with accepted_proposal and no option at all, which places every one of "
                            "them the way `proposes` says" if v.get("proposal") else ""))
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


__all__ = ["LOOK_WAIT_SAID", "MAX_LOOK_PHRASES", "MAX_PLACED_LINES", "MAX_SURVEY_QUESTIONS",
           "MAX_TABLE_ROWS", "MINOR_OPENING_FRACTION", "NEAR_TOLERANCE_OF_DIAGONAL",
           "PLAN_WAIT_SAID", "SIZE_TOLERANCE", "bind_patches", "look_at_it", "look_lines",
           "opening_rows", "placed_lines", "render_block", "survey_lines", "surveyor_panel",
           "waiting_lines"]
