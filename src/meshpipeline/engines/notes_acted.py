# Responsibility: Say what the user's notes asked that the product ACTED ON outside the requirements reading - the engine the notes name, the flow conditions the user confirmed, the kind of mesh, the unit of the notes' own numbers - so none of it is ever told back as "not applied" or counted as such, whatever the model's reading made of it.
# Boundaries: rules only, no model call. It reads the notes word for word and the run's own facts (the engine this attempt meshes with, the ladder's attempts, the purpose, the conditions accepted at intake); it never changes what is meshed. Never raises.
# Collaborates with: engines/mesh_requirements (the section it corrects; words() says its lines), engines/cfd_requirements (applies it once per attempt, reports the engine row), engines/fea_driver (applies it to the solid's reading), application/part_matches (the request card's lines), agents/intake/engine_selection (what "chooses an engine" means, the same rule the typed override acts on), contracts/flow_conditions.
"""NOTE-15: WHAT THE NOTES SAID THAT THE PRODUCT ACTED ON (docs/RULES.md NOTE-15).

The notes' reader (NOTE-7..13) fills the mesh settings; whatever fits none of them it tells as "Not applied: ... no
mesh setting of ours matches it yet" and counts in the closing tally. But some of what the notes say is acted on
outside that reading, and the reading cannot know it:

- the ENGINE the notes name ("use snappyHexMesh"): the typed override acts on it (engine_selection.note_chooses_engine)
  and the run meshes with it. Recognised here from the notes and from the engine the attempt used - never from the
  reading: the line is "From your notes: meshed with snappyHexMesh, as you asked." and it is an ask of the report,
  met; one that could not be used says why and what made the mesh instead.
- the FLOW CONDITIONS and the wall treatment the user confirmed at intake ("water at 2 m/s", "wall functions").
- the KIND of mesh ("internal flow mesh"), when it is the kind this run makes.
- the UNIT of the notes' own numbers ("units: mm").

A quote the reading left as not applied, as no mesh setting, or set aside (the engine's), that is only one of these
(the act's own words, with nothing else asked in it but what a row of the reading already holds), is taken out of
those lists and told by the act's line instead - so it never makes a "Not applied" line or a count.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

RULE = "NOTE-15"
#: the section's key: one entry per act, {"act", "quotes", "line", ...}
KEY = "acted"
#: the report row's field for the engine the notes name
ENGINE_FIELD = "engine"

#: the small words around an ask that ask nothing of their own
_FILLER = frozenset("""
    a an the it its this that these those please use used using uses with by via for of to in on as is are be been
    we i id i'd im i'm you your our my me us want wanted wants would like should must need needs let lets just only
    also and or so all mesh meshed meshing mesher engine run case part model geometry here make made do go
    kindly can could will shall try ok okay
    """.split())
_TOKENS = re.compile(r"[a-z0-9+]+(?:\.[0-9]+)?")
_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+")

#: the words of the kind of mesh, by the run's flow topology ("" is every flow)
_KIND_WORDS = {
    "internal": frozenset("internal inside interior through duct ducts pipe pipes passage passages channel bore "
                          "flow fluid cfd volume 3d".split()),
    "external": frozenset("external outside exterior around aero aerodynamic aerodynamics freestream wind tunnel "
                          "flow fluid cfd volume 3d".split()),
    "fea": frozenset("fea structural stress static".split()),       # shell or solid is the stage's (SHSTAGE)
}
_KIND_SAID = {"internal": "an internal flow mesh", "external": "an external flow mesh", "fea": "a structural (FEA) mesh"}
#: the words that make a quote about the kind at all (not only "flow")
_KIND_CORE = frozenset("internal inside interior through external outside exterior around aero aerodynamic "
                       "aerodynamics freestream fea structural cfd".split())

#: the unit of the notes' own numbers ("units: mm", "all dimensions in inches")
_UNIT_WORDS = frozenset("units unit dimensions dimension lengths length numbers number values sizes given "
                        "mm millimetre millimetres millimeter millimeters cm centimetre centimetres centimeter "
                        "centimeters m metre metres meter meters in inch inches um micron microns".split())
_UNIT_LEAD = frozenset("units unit dimensions dimension lengths length numbers number values".split())
_UNIT_NAME = frozenset("mm millimetre millimetres millimeter millimeters cm centimetre centimetres centimeter "
                       "centimeters m metre metres meter meters in inch inches um micron microns".split())

#: the words of the flow conditions and the wall treatment
_FLOW_WORDS = frozenset("flow fluid speed velocity inlet at conditions condition wall walls functions function "
                        "resolved treatment y y+ yplus m/s s m km h kph mph knots knot l min lpm kg".split())


# #
# the run's facts
# #

def facts_of(state: Mapping | None, engine: str = "") -> dict:
    """What the product acted on for this attempt: the engine it meshes with, the ladder's attempts so far, the flow
    topology of the purpose, the conditions the user confirmed. Never raises."""
    s = state if isinstance(state, Mapping) else {}
    out: dict = {"engine": str(engine or s.get("engine") or "").strip().lower(), "attempts": [], "topology": "",
                 "conditions": None}
    try:
        ladder = s.get("engine_ladder") if isinstance(s.get("engine_ladder"), Mapping) else {}
        out["attempts"] = [dict(a) for a in (ladder or {}).get("attempts") or [] if isinstance(a, Mapping)]
        from meshpipeline.engines.fea_review import is_fea
        from meshpipeline.engines.purposes import topology_of
        purpose = str(s.get("purpose") or "")
        out["topology"] = "fea" if purpose and is_fea(purpose) else topology_of(purpose)
        from meshpipeline.contracts import flow_conditions as FC
        out["conditions"] = FC.read(str(s.get("request_txt") or ""), s.get("intake_patches") or [])
    except Exception:  # noqa: BLE001 - a fact not read is an act not claimed
        logger.debug("notes acted: facts not read", exc_info=True)
    return out


# #
# the engine the notes name
# #

def sentences(notes: str) -> list[str]:
    """The notes' sentences, as written (the notes block's own labels taken out)."""
    from meshpipeline.engines.mesh_requirements import _unmarked
    return [s.strip(" \t.;!") for s in _SPLIT.split(_unmarked(notes)) if s.strip(" \t.;!")]


def engine_asked(notes: str) -> tuple[str, str]:
    """(the engine the notes choose, by registry key; the sentence that chooses it) - ("", "") for none. One sentence
    that names ONE engine and plainly chooses it, by the typed override's own rule (engine_selection.engine_chosen_in:
    no question, no refusal), "mesh it with VMTK" too; the latest such sentence wins (NOTE-7's latest written)."""
    from meshpipeline.agents.intake.engine_selection import engine_chosen_in
    got = ("", "")
    for s in sentences(notes):
        engine = engine_chosen_in(s, with_words=True)
        if engine:
            got = (engine, s)
    return got


def _label(engine: str) -> str:
    from meshpipeline.engines.registry import engine_label
    return engine_label(engine)


def same_engine(asked: str, used: str) -> bool:
    """Whether the engine used is the one asked: the same, or the one whose name the asked one shortens
    ("snappyHexMesh" for snappyHexMesh multi-region)."""
    if not asked or not used:
        return False
    if asked == used:
        return True
    from meshpipeline.agents.intake.engine_selection import short_siblings
    return asked in short_siblings(used)


def why_not(asked: str, used: str, facts: Mapping) -> str:
    """Why the engine asked did not make this mesh, in plain words."""
    label = _label(asked)
    topo = str(facts.get("topology") or "")
    if topo in ("internal", "external"):
        from meshpipeline.engines.registry import engines_producing_topology
        if asked not in engines_producing_topology(topo):
            return f"{label} cannot make {_KIND_SAID[topo]}"
    for a in facts.get("attempts") or []:
        if str(a.get("engine") or "") == asked and str(a.get("kind") or "") != "passed":
            return f"{label} did not make a usable mesh"
    return f"{label} was not the engine this run was set to use"


def _engine_entry(asked: str, quote: str, facts: Mapping) -> dict:
    used = str(facts.get("engine") or "")
    label = _label(asked)
    entry: dict[str, Any] = {"act": "engine", "engine": asked, "used": used, "quotes": [quote],
                             "asked": f"meshed with {label}"}
    if not used or same_engine(asked, used):
        shown = _label(used) if used else label
        entry.update(met=True if used else None, line=f"From your notes: meshed with {shown}, as you asked.",
                     delivered="")
    else:
        why = why_not(asked, used, facts)
        entry.update(met=False, why=why,
                     line=f"From your notes: you asked for {label}; {why}, so this mesh was made with {_label(used)}.",
                     delivered=f"{why}, so it was made with {_label(used)}")
    return entry


# #
# is a quote only one of the acts?
# #

def _rest(quote: str, spellings: list[str], covered: list[str]) -> list[str]:
    """The words of `quote` left once the act's own words, what a row of the reading holds and the small words are
    taken out."""
    text = " " + " ".join(str(quote or "").lower().split()) + " "
    for c in sorted({" ".join(str(c).lower().split()) for c in covered if str(c).strip()}, key=len, reverse=True):
        if c and c != text.strip():
            text = text.replace(c, " ")
    for s in sorted({str(s).lower() for s in spellings if str(s).strip()}, key=len, reverse=True):
        text = text.replace(s, " ")
    return [t for t in _TOKENS.findall(text.replace("m/s", " ").replace("y+", " yplus ")) if t not in _FILLER]


def _engine_spellings(engine: str) -> list[str]:
    return [engine, _label(engine), _label(engine).replace(" ", "")]


def _only_engine(quote: str, asked: str, covered: list[str]) -> bool:
    from meshpipeline.agents.intake.vocabulary import engines_named_in
    if engines_named_in(quote, "") != [asked]:
        return False
    return not _rest(quote, _engine_spellings(asked), covered)


def _conditions_line(quote: str, c: Mapping) -> str:
    from meshpipeline.engines.mesh_requirements import _short
    return (f'From your notes: "{_short(quote)}" - taken as the flow conditions you confirmed '
            f"({c.get('fluid_words')} at {c.get('speed_words')}).")


def _only_conditions(quote: str, c: Mapping | None, covered: list[str]) -> bool:
    if not c:
        return False
    fluid = str(c.get("fluid_words") or "").lower()
    speed = str(c.get("speed_words") or "").lower()
    own = [w for w in _TOKENS.findall(f"{fluid} {speed}") if w not in _FILLER]
    words = _TOKENS.findall(str(quote).lower().replace("m/s", " "))
    if not any(w in own for w in words) and not re.search(r"\bwall\s+(?:functions?|treatment)|\bresolved\b",
                                                         str(quote), re.I):
        return False                       # it names nothing of the conditions
    rest = [w for w in _rest(quote, [fluid, speed], covered) if w not in _FLOW_WORDS and w not in own]
    return not rest


def _only_kind(quote: str, topology: str, covered: list[str]) -> bool:
    vocab = _KIND_WORDS.get(topology)
    if not vocab:
        return False
    rest = _rest(quote, [], covered)
    return bool(rest) and any(w in _KIND_CORE for w in rest) and all(w in vocab for w in rest)


def _only_unit(quote: str, covered: list[str]) -> bool:
    rest = _rest(quote, [], covered)
    return (bool(rest) and any(w in _UNIT_LEAD for w in rest) and any(w in _UNIT_NAME for w in rest)
            and all(w in _UNIT_WORDS for w in rest))


#: THE WALL TREATMENT IN WORDS (CFDREQ-8's two: a resolved wall, or wall functions) - the same control as a y+
_RESOLVED_WORDS = re.compile(r"\bresolv\w*\b[^.;]{0,30}?\b(?:wall|walls|boundary\s+layers?|near[- ]wall)\b"
                             r"|\bwall[- ]resolv\w*|\b(?:no|without)\s+wall[- ]functions?\b|\blow[- ]re(?:ynolds)?\b",
                             re.I)
_FUNCTIONS_WORDS = re.compile(r"\bwall[- ]functions?\b", re.I)
#: ...and the words around it that ask nothing more ("resolve the wall - y+ around 1", "wall functions are fine")
_WALL_WORDS = frozenset("resolve resolved resolving wall walls boundary layer layers near functions function no "
                        "without low re reynolds fine good y yplus around about approximately roughly at on".split())
#: a y+ at or under this is a resolved wall, at or over WALL_FN_YPLUS wall functions (CFDREQ-8: y+ 1, and 30-300)
RESOLVED_YPLUS, WALL_FN_YPLUS = 5.0, 20.0


def wall_said(quote: str) -> str:
    """The wall treatment a quote names: "resolved", "functions" or ""."""
    if _RESOLVED_WORDS.search(str(quote or "")):
        return "resolved"
    return "functions" if _FUNCTIONS_WORDS.search(str(quote or "")) else ""


def wall_built(sec: Mapping, facts: Mapping) -> str:
    """The wall treatment the reading and the confirmed conditions build: a y+ of the rows first, then the treatment
    the user chose at intake ("" when neither says)."""
    lay = ((sec.get("requirements") or {}).get("layers") or {}) if isinstance(sec, Mapping) else {}
    yp = lay.get("y_plus")
    if isinstance(yp, (int, float)):
        return "resolved" if yp <= RESOLVED_YPLUS else "functions" if yp >= WALL_FN_YPLUS else ""
    c = facts.get("conditions") if isinstance(facts, Mapping) else None
    return str(c.get("wall") or "") if isinstance(c, Mapping) and c.get("wall_said", True) else ""


def _wall_act(quote: str, sec: dict, facts: Mapping) -> bool:
    """CFDREQ-8 x NOTE-15: words that only name the wall treatment ("resolve the wall", "wall-resolved", "no wall
    functions", "wall functions are fine") are the same ask as the y+ that builds it - met with it, never a second
    ask "not applied". Alone, "resolve the wall" is that ask itself: y+ about 1 at the walls (a row of the reading,
    sized from the confirmed fluid and speed). Words naming the OTHER treatment than the one built stay told."""
    said = wall_said(quote)
    if not said:
        return False
    rest = [w for w in _TOKENS.findall(str(quote).lower().replace("y+", " yplus "))
            if w not in _FILLER and w not in _WALL_WORDS and not re.fullmatch(r"[\d.]+", w)]
    if rest:
        return False
    built = wall_built(sec, facts)
    if built:
        return built == said
    if said != "resolved" or sec.get("status") != "used":
        return False
    import copy

    from meshpipeline.engines import mesh_requirements as MR
    req = copy.deepcopy(dict(sec.get("requirements") or MR.empty()))
    req.setdefault("layers", {})["y_plus"] = 1.0
    ph = req.setdefault("physics", {})
    c = facts.get("conditions") if isinstance(facts, Mapping) else None
    if isinstance(c, Mapping):
        from meshpipeline.contracts import flow_conditions as FC
        if ph.get("velocity_ms") is None and c.get("velocity_ms") is not None:
            ph["velocity_ms"] = float(c["velocity_ms"])
        if ph.get("fluid") is None and c.get("fluid") in FC.NU:
            ph["fluid"] = c["fluid"]
    req.setdefault("rows", []).append({"id": "w1", "field": "layers:y_plus", "quote": " ".join(str(quote).split()),
                                       "asked": "y+ of about 1 at the walls (a resolved wall)"})
    sec["requirements"] = req
    return True


def _acts_together(quote: str, sec: Mapping, facts: Mapping, covered: list[str]) -> bool:
    """NOTE-15: several acts said in one breath ("air at 10 m/s with a resolved boundary layer (y+ of 1)") are still
    only those acts - the confirmed flow conditions, the words a row of the reading holds, and the wall treatment
    that is built. Nothing is left of the quote once each is taken out, so it is no further ask "not applied".
    The OTHER wall treatment than the one built, or any word beyond these, keeps the quote told."""
    said = wall_said(quote)
    if not said or wall_built(sec, facts) != said:
        return False
    c = facts.get("conditions") if isinstance(facts, Mapping) else None
    fluid = str(c.get("fluid_words") or "").lower() if isinstance(c, Mapping) else ""
    speed = str(c.get("speed_words") or "").lower() if isinstance(c, Mapping) else ""
    own = {w for w in _TOKENS.findall(f"{fluid} {speed}") if w not in _FILLER}
    rest = [w for w in _rest(quote, [fluid, speed], covered)
            if w not in _FLOW_WORDS and w not in _WALL_WORDS and w not in own]
    return not rest


def _stem(word: str) -> str:
    return re.sub(r"(?:ing|ed|es|e|s)$", "", word) if len(word) > 4 else word


def _covered_by_rows(quote: str, sec: Mapping) -> bool:
    """NOTE-15's general guard: words whose every word an applied row of the reading already holds (its quote or its
    plain words) mean that row's ask - told by the row, never again as "not applied"."""
    words = {_stem(w) for w in _TOKENS.findall(str(quote).lower()) if w not in _FILLER}
    if not words:
        return False
    for r in ((sec.get("requirements") or {}).get("rows") or []) if isinstance(sec, Mapping) else []:
        if not isinstance(r, Mapping):
            continue
        meant = {_stem(w) for w in _TOKENS.findall(str(r.get("asked") or "").lower()) if w not in _FILLER}
        held = meant | {_stem(w) for w in _TOKENS.findall(str(r.get("quote") or "").lower())}
        # (and the words name what the row asks, not only share its sentence: "keep the cells cubic at the outlet"
        # inside a y+ row's quote is still an ask of its own)
        if words <= held and words & meant:
            return True
    return False


def _act_of(quote: str, facts: Mapping, covered: list[str], asked: str) -> tuple[str, str]:
    """(the act a quote is only one of, its line - "" for the engine's own entry); ("", "") for none."""
    from meshpipeline.engines.mesh_requirements import _short
    if asked and _only_engine(quote, asked, covered):
        return "engine", ""
    if _only_conditions(quote, facts.get("conditions"), covered):
        return "conditions", _conditions_line(quote, facts["conditions"])
    topo = str(facts.get("topology") or "")
    if _only_kind(quote, topo, covered):
        return "kind", f'From your notes: "{_short(quote)}" - meshed as {_KIND_SAID[topo]}, as you said.'
    if _only_unit(quote, covered):
        return "unit", f'From your notes: "{_short(quote)}" - read as the unit of the lengths in your notes.'
    return "", ""


# #
# the section, corrected
# #

def _covered(sec: Mapping) -> list[str]:
    req = sec.get("requirements") if isinstance(sec.get("requirements"), Mapping) else {}
    return [str(r.get("quote") or "") for r in (req or {}).get("rows") or [] if isinstance(r, Mapping)]


def _dropped_quote(d: str) -> str:
    """The user's words of an ask the checks set aside ('"<their words>": <why>: not applied')."""
    head = str(d).partition(": ")[0].strip()
    return head.strip('"') if head.startswith('"') else head


def apply(sec: Mapping | None, notes: str, facts: Mapping | None) -> dict:
    """The section with every quote that is only an act of the product taken out of `not_applied`, the notes'
    `not_mesh` and (the engine's) `dropped`, and the acts said in `acted` - one entry each. The engine the notes
    choose is an entry whenever they choose one, whatever the reading made of the sentence. The section as given
    when nothing applies; never raises."""
    if not isinstance(sec, Mapping):
        return sec  # type: ignore[return-value]
    try:
        return _apply(dict(sec), str(notes or ""), facts or {})
    except Exception:  # noqa: BLE001 - the reading as it was
        logger.warning("notes acted: not applied", exc_info=True)
        return dict(sec)


def _apply(sec: dict, notes: str, facts: Mapping) -> dict:
    import copy

    from meshpipeline.engines import mesh_requirements as MR
    if sec.get("status") == "none" or not notes.strip():
        return sec
    asked, quote = engine_asked(notes)
    entries: list[dict] = []
    engine_entry = _engine_entry(asked, quote, facts) if asked else None
    if engine_entry is not None:
        entries.append(engine_entry)
    covered = _covered(sec)
    gone_lines: list[str] = []

    def absorbed(q: str, *, engine_only: bool = False) -> bool:
        if not engine_only and (_wall_act(q, sec, facts) or _covered_by_rows(q, sec)
                                or _acts_together(q, sec, facts, covered)):
            # the wall treatment the rows build, or words an applied row already holds the meaning of: one ask,
            # told by its row - never a second one "not applied"
            entries.append({"act": "covered", "quotes": [q], "line": ""})
            return True
        act, line = (("engine", "") if asked and _only_engine(q, asked, covered) else ("", "")) if engine_only \
            else _act_of(q, facts, covered, asked)
        if not act:
            return False
        if act == "engine" and engine_entry is not None:
            if q not in engine_entry["quotes"]:
                engine_entry["quotes"].append(q)
        elif not any(e.get("line") == line for e in entries):
            entries.append({"act": act, "quotes": [q], "line": line})
        return True

    kept_not = []
    for q in sec.get("not_applied") or []:
        if absorbed(str(q)):
            gone_lines.append(f'Not applied: "{MR._short(str(q))}" - no mesh setting of ours matches it yet.')
        else:
            kept_not.append(q)
    kept_drop = []
    for d in sec.get("dropped") or []:
        if absorbed(_dropped_quote(str(d)), engine_only=True):
            gone_lines.append(f"Not applied: {d}.")
        else:
            kept_drop.append(d)
    req = sec.get("requirements") if isinstance(sec.get("requirements"), Mapping) else None
    kept_mesh = []
    for e in (req or {}).get("not_mesh") or []:
        if isinstance(e, Mapping) and absorbed(str(e.get("quote") or "")):
            gone_lines += MR.not_mesh_words([e])
        else:
            kept_mesh.append(e)
    if not entries:
        return sec
    sec["not_applied"], sec["dropped"] = kept_not, kept_drop
    if req is not None and req.get("not_mesh") is not None:
        # (the set as it stands now: a wall treatment's y+ may have been added to it above)
        req = copy.deepcopy(dict(sec.get("requirements") or req))
        if kept_mesh:
            req["not_mesh"] = kept_mesh
        else:
            req.pop("not_mesh", None)
        sec["requirements"] = req
    sec[KEY] = entries
    if isinstance(sec.get("lines"), list):
        # merged with the team's meshing standard: its lines, the acts' said where the notes' "Not applied" start
        kept = [w for w in sec["lines"] if w not in gone_lines]
        at = next((i for i, w in enumerate(kept) if str(w).startswith(("Not applied", MR.NOT_MESH_LEAD))), len(kept))
        sec["lines"] = [*kept[:at], *[e["line"] for e in entries], *kept[at:]]
        if sec.get("status") != "used":
            sec["note"] = " ".join(sec["lines"])
    return sec


def lines(sec: Mapping | None) -> list[str]:
    """The acts' lines, one each ([] with none)."""
    return [str(e.get("line")) for e in ((sec or {}).get(KEY) or []) if isinstance(e, Mapping) and e.get("line")]


def engine_entry(sec: Mapping | None) -> dict | None:
    """The engine the notes chose, as the section keeps it (None with none)."""
    for e in (sec or {}).get(KEY) or []:
        if isinstance(e, Mapping) and e.get("act") == "engine":
            return dict(e)
    return None


def report_row(sec: Mapping | None, engine: str) -> dict | None:
    """The engine the notes chose as a row of the requirement report, against the engine that built THIS mesh:
    met when it is the one asked, else not met with why and what made it. None when the notes chose none."""
    e = engine_entry(sec)
    if e is None:
        return None
    used = str(engine or e.get("used") or "").strip().lower()
    if used and used != e.get("used"):
        e = _engine_entry(str(e["engine"]), str((e.get("quotes") or [""])[0]), {"engine": used})
    met = e.get("met")
    return {"id": "e1", "field": ENGINE_FIELD, "asked": e["asked"], "applied": True,
            "met": met if met is not None else True, "excess": 0 if met is not False else 1,
            "delivered": str(e.get("delivered") or "")}


__all__ = ["ENGINE_FIELD", "KEY", "RULE", "apply", "engine_asked", "engine_entry", "facts_of", "lines", "report_row",
           "same_engine", "sentences", "why_not"]
