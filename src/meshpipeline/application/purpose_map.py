# Responsibility: Read what the user said the mesh is for and say which path it takes - CFD, FEA,
# electronics cooling, or FEA "heat in a solid part" - and, when the words do not say, the one
# short question that settles it. Also the FEA path's one required question ("What will you
# analyze?") and the reading of its answer.
# Owns: the cue words, the routing rule, the two purpose questions and their buttons, the FEA
# analysis types (V1 and "coming soon") and the hold line an FEA part is checked under.
# Boundaries: pure. It reads words and returns a reading; it stores nothing, asks no model and
# never changes what a user declared. An ECXML upload is routed by its format before any of this
# is read (contracts/intake_formats check_mode "thermal").
# Collaborates with: agents/intake/message.py (asks the question, holds for the check),
# application/geometry_check.py (an FEA part is not named by the picture model), api/v1/geometry.py
# (which stage the part opens on) and api/v1/chat.py (the buttons under a question).
from __future__ import annotations

import re
from dataclasses import dataclass

#: THE PATHS. "flow": CFD through or around the part, the geometry check confirms which on its
#: stage. "fea": the part's own solid mesh for structural FEA. "electronics": electronics cooling
#: (an ECXML model's own flow; for any other file, a cooling case through the flow check).
ROUTE_FLOW, ROUTE_FEA, ROUTE_ELECTRONICS = "flow", "fea", "electronics"
ROUTES = (ROUTE_FLOW, ROUTE_FEA, ROUTE_ELECTRONICS)
#: THE TWO QUESTIONS, by what is unclear: "thermal" (cooling something, or heat in a solid part)
#: and "domain" (a flow, or the part's strength).
ASK_THERMAL, ASK_DOMAIN = "thermal", "domain"

#: The thermal question, in the approved wording (Documents/HEXERA_THERMAL_SPLIT_PROPOSAL.md).
THERMAL_QUESTION = ("Is this about cooling something with air or liquid, or about heat moving "
                    "through a solid part?")
#: The question for words that name neither a flow nor the part's strength (or both at once).
DOMAIN_QUESTION = ("Is this about a fluid flowing through or around the part (CFD), or about the "
                   "part's own strength, stress or vibration (FEA)?")
#: Each question's two buttons: (what the button says, what a click sends as the user's words).
#: The words sent are clear answers themselves, so a click reads exactly as typing them would.
THERMAL_CHOICES = (("Cooling with air or liquid", "Cooling with air or liquid"),
                   ("Heat moving through a solid part", "Heat moving through a solid part"))
DOMAIN_CHOICES = (("A fluid flow (CFD)", "A fluid flowing through or around the part (CFD)"),
                  ("Strength, stress or vibration (FEA)", "The part's strength, stress or vibration (FEA)"))
QUESTIONS = {ASK_THERMAL: THERMAL_QUESTION, ASK_DOMAIN: DOMAIN_QUESTION}
_CHOICES = {ASK_THERMAL: THERMAL_CHOICES, ASK_DOMAIN: DOMAIN_CHOICES}


@dataclass(frozen=True)
class PurposeReading:
    """What the words say. `route` is "" when they settle nothing; `ask` then says which question
    settles it. `heat_in_solid` marks the FEA path taken for heat in a solid part (V1: coming soon,
    meshed as for static stress). `topology` is "internal" / "external" when a flow's words say
    which, else "". `analysis` is the FEA analysis type the words name (ANALYSIS_KEYS), else ""."""

    route: str = ""
    ask: str = ""
    heat_in_solid: bool = False
    topology: str = ""
    analysis: str = ""
    #: the cue words that decided, for the log and the score table
    why: str = ""

    @property
    def settled(self) -> bool:
        return self.route in ROUTES


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE)


#: WORDS FOR THE PART'S OWN MECHANICS: what FEA answers.
_FEA = _rx(
    r"\bfea\b", r"\bfem\b", r"finite[- ]element", r"\bstructur", r"\bstress", r"\bstrain", r"\bdeform",
    r"\bdeflect", r"\bdisplacement", r"\bbuckl", r"\bfatigue", r"\bmodal\b", r"\bmode shapes?\b",
    r"natural freq", r"\beigen", r"\bresonan", r"\bvibrat", r"\bcrash", r"\bimpact\b", r"\bdrop[- ]test",
    r"\bvon[- ]?mises", r"\byield", r"\bbreak", r"\bbroken\b", r"\bcrack", r"\bfractur", r"\bfail",
    r"\bstrength\b", r"\bstrong\b", r"\bstiff", r"\bload(s|ed|ing)?\b", r"\bbend", r"\btorsion",
    r"\btorque\b", r"\btensile\b", r"\bcompressi", r"\bpreload", r"\bweld", r"safety factor",
    r"factor of safety", r"\babaqus\b", r"\bnastran\b", r"\bcalculix\b", r"\bls-?dyna\b",
    r"\boptistruct\b", r"code[_ ]aster", r"\bmechanical\b", r"\bsolid mechanics\b", r"\bburst",
    r"\bsurviv", r"\bwithstand", r"\bshock\b", r"harmonic response", r"\bcreep\b", r"\bplastic(ity)?\b",
    r"\belastic", r"\bcontact (analysis|pressure)", r"\btopology optimi[sz]", r"\b\d+(\.\d+)?\s?k?n\b",
    r"\bsag\b", r"how much (load|weight|force)", r"\bhold \d", r"\bthick enough\b", r"\bsol ?1\d\d\b",
    r"\bcrashworth",
)
#: WORDS FOR A FLUID: what CFD answers.
_FLOW = _rx(
    r"\bcfd\b", r"\bfluid\b(?![- ]structure)", r"\bflow", r"\bairflow", r"\baero", r"\bdrag\b",
    r"\blift\b", r"\bdownforce", r"\bwind\b", r"pressure (drop|loss)", r"\bhead loss", r"\bturbulen",
    r"\bopenfoam\b", r"\bfluent\b", r"star-?ccm", r"\bcfx\b", r"\bvelocity", r"\b\d+\s?(m/s|km/h|mph)",
    r"\bmach\b", r"\breynolds", r"\by\+", r"\byplus", r"boundary layer", r"\binlet", r"\boutlet",
    r"\bnozzle", r"\bduct\b", r"\bmanifold", r"\bwater\b", r"\bgas\b", r"\bcombust", r"\bmixing\b",
    r"\bwake\b", r"\bswirl", r"\bhydrodynamic", r"\bcavitat", r"\bimpeller", r"\bventilat", r"\bhvac\b",
    r"\bslosh", r"\bwind tunnel", r"\bair (move|moves|around|over|through)", r"goes in .* out",
    r"\bblood\b", r"\bk-?(omega|epsilon)\b", r"\bpolyhedral\b",
)
#: Words that put a flow INSIDE the part, and AROUND it.
_INTERNAL = _rx(r"\bthrough\b", r"\binside\b", r"\binternal", r"\bpipe", r"\bduct", r"\bmanifold",
                r"\bvalve", r"\bnozzle", r"pressure (drop|loss)", r"\bfilter\b", r"\bgoes in\b",
                r"\bartery", r"\bimpeller", r"\bmixing\b", r"\bin the (room|tank|combustor)")
_EXTERNAL = _rx(r"\baround\b", r"\bover\b", r"\bexternal", r"\baero", r"\bdrag\b", r"\blift\b",
                r"\bdownforce", r"\bwind\b", r"\bhull\b", r"\bwake\b")

#: WORDS THAT PUT HEAT IN THE QUESTION. Any one of them and the thermal split decides.
_HEAT = _rx(r"\bthermal", r"\bheat", r"\bhot\b", r"\bwarm", r"\btemperature", r"\bcool", r"\boverheat",
            r"\bconduct", r"\bconvect", r"junction temp", r"\bdissipat", r"\bconjugate\b", r"\bcht\b",
            r"\bthermo", r"\bflotherm", r"\bicepak", r"\becxml\b")
#: ...cooling something: electronics cooling (the approved split's words).
_COOLING = _rx(r"\bcool", r"\bheat ?sinks?\b", r"\bfans?\b", r"\bchips?\b", r"\bpcbs?\b", r"circuit board",
               r"\bboards?\b", r"\belectronic", r"\benclosure", r"\bcpus?\b", r"\bgpus?\b", r"\bprocessor",
               r"\bserver", r"\brack\b", r"power (module|electronics)", r"\binverter", r"\bleds?\b",
               r"junction temp", r"thermal management", r"\becxml\b", r"\bflotherm", r"\bicepak",
               r"6sigma", r"\bconjugate\b", r"\bcht\b", r"\bconvect", r"\b\d+(\.\d+)?\s?w\b", r"\bairflow")
#: ...heat in a solid part: conduction, the temperature inside the part, thermal stress.
_SOLID_HEAT = _rx(
    r"\bconduct", r"thermal[- ]?(stress|strain|expansion|fatigue|structural|mechanical|deformation)",
    r"thermo[- ]?(mechanical|elastic|structural)",
    r"temperature (rise |field |distribution )?(in|inside|of|within|through|across) (the |this |a )?"
    r"(part|solid|block|body|wall|bracket|disc|disk|plate|component|beam|shaft)",
    r"heat (moving |flowing |going |travel\w* )?(through|in|inside|into) (the |this |a )?"
    r"(solid|part|wall|block|body)", r"\bin (the|this) solid\b", r"\bsolid part\b", r"\bexpansion",
)
#: A FLUID AS THE CAUSE, the part's mechanics as the question: "stress from the pressure of the
#: water". The flow is the load; the answer is FEA.
_FLOW_AS_LOAD = _rx(r"(stress|strain|deform\w*|displacement|deflect\w*|break\w*|crack\w*)\s+"
                    r"(\w+\s+){0,2}(from|due to|caused by|because of|under)\s+(the\s+)?"
                    r"(pressure|flow|water|air|wind|fluid)",
                    r"pressure load", r"\bload\w* on the walls")
#: Words that name BOTH physics as one question - a coupled problem.
_COUPLED = _rx(r"fluid[- ]structure", r"\bfsi\b", r"\bflutter", r"aero[- ]?elastic", r"multiphysics",
               r"\bstorm\b")

#: THE FEA ANALYSIS TYPES: (key, button label, in V1). The "coming soon" ones are shown greyed
#: and, if picked, the part is meshed as for static stress, with a plain note (design 3.2).
ANALYSES = (
    ("static", "Static stress", True),
    ("modal", "Vibration (modal)", True),
    ("buckling", "Buckling", True),
    ("thermal", "Thermal or thermal stress", False),
    ("nonlinear", "Nonlinear or contact", False),
    ("impact", "Impact or crash", False),
    ("fatigue", "Fatigue", False),
)
ANALYSIS_KEYS = tuple(k for k, _l, _v in ANALYSES)
V1_ANALYSES = tuple(k for k, _l, v1 in ANALYSES if v1)
ANALYSIS_LABELS = {k: label for k, label, _v in ANALYSES}
#: The FEA path's one required question (design 3.2, Areen's words).
ANALYSIS_QUESTION = "What will you analyze?"
#: A "coming soon" pick, said plainly: what it is, and what happens instead.
COMING_SOON = ("{label} is coming soon. For now I'll mesh the part as for static stress: the same "
               "solid mesh, with every face named, so you can set up the rest in your solver.")

#: Which analysis the words name, most specific first: buckling before stress, fatigue before a
#: crack, a thermal stress before a stress.
_ANALYSIS_RX = (
    ("buckling", _rx(r"\bbuckl")),
    ("modal", _rx(r"\bmodal\b", r"\bvibrat", r"natural freq", r"\bmode shapes?\b", r"\beigen", r"\bresonan",
                  r"harmonic response", r"\bsol ?103\b")),
    ("fatigue", _rx(r"\bfatigue", r"\bendurance")),
    ("impact", _rx(r"\bimpact", r"\bcrash", r"\bdrop[- ]?test", r"\bexplicit\b", r"\bls-?dyna\b", r"\bshock\b")),
    ("nonlinear", _rx(r"non-?linear", r"\bcontact\b", r"\bplastic", r"hyperelastic", r"\bpreload")),
    ("thermal", _rx(r"\bthermal", r"\bheat", r"\btemperature", r"\bconduct", r"\bthermo")),
    ("static", _rx(r"\bstatic", r"\bstress", r"\bstrength", r"\bstrong\b", r"\bdeflect", r"\bdeform",
                   r"\bdisplacement", r"\bload", r"\bstiff", r"\bbreak", r"\byield", r"von[- ]?mises",
                   r"safety factor", r"\bbend", r"\btorque", r"\bburst", r"\bstrain", r"\btensile")),
)


def _hits(rx: re.Pattern, text: str) -> list[str]:
    return [m.group(0).strip().lower() for m in rx.finditer(text)]


def analysis_of(text: str) -> str:
    """The FEA analysis type the words name, or "" (then the FEA path asks "What will you analyze?")."""
    said = str(text or "")
    for key, rx in _ANALYSIS_RX:
        if rx.search(said):
            return key
    return ""


def _topology(text: str) -> str:
    inside, outside = bool(_INTERNAL.search(text)), bool(_EXTERNAL.search(text))
    return "internal" if inside and not outside else "external" if outside and not inside else ""


def read_purpose(text: str) -> PurposeReading:
    """Where these words send the job. The rules, in order:
    1. Words that name both physics as one coupled problem (FSI, flutter) ask the domain question.
    2. Heat in the words: the thermal split. Cooling words go to electronics cooling; words for
       heat in a solid part (conduction, the temperature inside it, thermal stress) to the FEA
       path; both, or neither, ask the thermal question - except words plainly about a flow and
       nothing else (a hot gas flow is a flow).
    3. No heat: the part's mechanics go to FEA, a fluid to CFD; a fluid named as the load on the
       part ("stress from the pressure of the water") is FEA; both otherwise, or neither, ask the
       domain question."""
    said = " ".join(str(text or "").split())
    fea, flow = _hits(_FEA, said), _hits(_FLOW, said)
    if _COUPLED.search(said):
        return PurposeReading(ask=ASK_DOMAIN, why="coupled: " + ", ".join(_hits(_COUPLED, said)))
    if _HEAT.search(said):
        cooling, solid = _hits(_COOLING, said), _hits(_SOLID_HEAT, said)
        if cooling and fea and not solid:
            # cooling AND the part's strength ("strength and cooling"): two jobs - which one?
            return PurposeReading(ask=ASK_DOMAIN, why="cooling and mechanics: " + ", ".join(cooling + fea))
        if cooling and not solid:
            return PurposeReading(route=ROUTE_ELECTRONICS, why="cooling: " + ", ".join(cooling))
        if solid and not cooling:
            return PurposeReading(route=ROUTE_FEA, heat_in_solid=True, analysis="thermal",
                                  why="heat in a solid: " + ", ".join(solid))
        if not cooling and not solid and flow and not fea:
            return PurposeReading(route=ROUTE_FLOW, topology=_topology(said),
                                  why="heat in a flow: " + ", ".join(flow))
        return PurposeReading(ask=ASK_THERMAL, why="heat, unclear: " + ", ".join(
            _hits(_HEAT, said) + cooling + solid + fea))
    if fea and flow:
        if _FLOW_AS_LOAD.search(said):
            return PurposeReading(route=ROUTE_FEA, analysis=analysis_of(said) or "static",
                                  why="a fluid as the load: " + ", ".join(fea))
        return PurposeReading(ask=ASK_DOMAIN, why="both: " + ", ".join(fea + flow))
    if fea:
        return PurposeReading(route=ROUTE_FEA, analysis=analysis_of(said), why="mechanics: " + ", ".join(fea))
    if flow:
        return PurposeReading(route=ROUTE_FLOW, topology=_topology(said), why="flow: " + ", ".join(flow))
    return PurposeReading(ask=ASK_DOMAIN, why="no cue")


def question_asked(messages) -> str:
    """Which purpose question the application already asked in this conversation ("" when none)."""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = str(m.get("content") or "").strip()
            for kind, question in QUESTIONS.items():
                if content == question:
                    return kind
    return ""


def _user_texts(messages) -> list[str]:
    return [str(m.get("content") or "").strip() for m in messages or []
            if isinstance(m, dict) and m.get("role") == "user" and str(m.get("content") or "").strip()]


def reading_for(messages) -> PurposeReading:
    """THE CONVERSATION'S PURPOSE, as the application routes it. The answer to a purpose question
    the application asked speaks first (it answers exactly that); otherwise everything the user
    has said, read together. After the one question, words that still settle nothing take the
    generic path (a flow, whose stage asks through-or-around) - never a second question."""
    texts = _user_texts(messages)
    asked = question_asked(messages)
    if asked and texts:
        answer = read_purpose(f"heat: {texts[-1]}" if asked == ASK_THERMAL else texts[-1])
        if answer.settled:
            return answer
    reading = read_purpose("\n".join(texts))
    if reading.settled or not asked:
        return reading
    return PurposeReading(route=ROUTE_FLOW, why=f"still unclear after the {asked} question: the generic path")


def question_for(reading: PurposeReading, messages) -> str:
    """The question to ask now, or "" - the reading is settled, or a purpose question was asked
    already (one question, never two)."""
    if reading.settled or question_asked(messages):
        return ""
    return QUESTIONS.get(reading.ask, "")


def choices_for(reply: str) -> list[dict]:
    """The buttons under an application question, by its exact words; [] for any other reply.
    `send` is what a click sends as the user's message; `soon` greys a "coming soon" choice
    (still clickable: it is meshed as for static stress, with a note)."""
    text = str(reply or "").strip()
    for kind, question in QUESTIONS.items():
        if text == question:
            return [{"label": label, "send": send, "soon": False} for label, send in _CHOICES[kind]]
    if text == ANALYSIS_QUESTION:
        return [{"label": label + ("" if v1 else " (coming soon)"),
                 "send": label if v1 else f"{label} (coming soon)", "soon": not v1}
                for _k, label, v1 in ANALYSES]
    return []


#: THE HOLD LINE an FEA part is checked under (after the drawing mark, which the intake never
#: reads as declared and the console holds the chat box on).
FEA_HOLD = ("thanks - I'm checking your part now. It will appear beside this chat with its size, "
            "its unit and the result of the clean check. Check them and press Proceed.")
#: Said with the hold for heat in a solid part, which version 1 does not mesh as its own kind yet.
HEAT_IN_SOLID_NOTE = ("Heat moving through a solid part is coming soon. For now I'll mesh it as for "
                      "static stress: the same solid mesh, with every face named.")
#: Said with the hold for electronics cooling from a file that is not an ECXML thermal model.
ELECTRONICS_NOTE = ("For the full electronics-cooling setup (parts, powers and fans read from the "
                    "file), upload the ECXML model exported from your thermal tool. For this file "
                    "I'll set up the cooling air.")


def coming_soon_note(analysis: str) -> str:
    """The plain note for a "coming soon" analysis type, "" for a V1 one (or none named)."""
    if not analysis or analysis in V1_ANALYSES:
        return ""
    return COMING_SOON.format(label=ANALYSIS_LABELS.get(analysis, analysis))


def fea_hold_text(reading: PurposeReading) -> str:
    """What the chat says while an FEA part is checked, after the drawing mark: the hold line, and
    a plain note when what the words ask for is not in V1 yet."""
    note = HEAT_IN_SOLID_NOTE if reading.heat_in_solid else coming_soon_note(reading.analysis)
    return FEA_HOLD + (f" {note}" if note else "")


__all__ = ["ANALYSES", "ANALYSIS_KEYS", "ANALYSIS_LABELS", "ANALYSIS_QUESTION", "ASK_DOMAIN",
           "ASK_THERMAL", "COMING_SOON", "DOMAIN_QUESTION", "ELECTRONICS_NOTE", "FEA_HOLD",
           "HEAT_IN_SOLID_NOTE", "PurposeReading", "ROUTES", "ROUTE_ELECTRONICS", "ROUTE_FEA",
           "ROUTE_FLOW", "THERMAL_QUESTION", "V1_ANALYSES", "analysis_of", "choices_for",
           "coming_soon_note", "fea_hold_text", "question_asked", "question_for", "read_purpose",
           "reading_for"]
