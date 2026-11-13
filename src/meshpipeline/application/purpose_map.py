# Responsibility: Read what the user said the mesh is for and say which path it takes - CFD, FEA,
# electronics cooling, or FEA "heat in a solid part" - and, when the words do not say, the one
# short question that settles it. Also each declared path's one required question - the FEA path's
# ("What will you analyze?") and a cooled solid's ("What is the coolant, and how much of it
# flows?") - and the reading of its answer.
# Owns: the cue words, the routing rule, the two purpose questions and their buttons, the FEA
# analysis types (V1 and "coming soon"), the coolants a cooled solid is offered, and the hold line
# an FEA part is checked under.
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
    # Reliability analysis is structural work. Without this, "solder joint reliability of a BGA"
    # read as cooling - the only wrong ROUTE the semiconductor holdout found, and the reason it is
    # fixed here rather than left: an "ask" costs the user one click, a wrong route costs them the
    # whole job. The other eight misses are left exactly as the holdout scored them.
    # Reliability of a JOINT or a PART, not the word on its own: bare "reliability" turned "flow
    # reliability in the manifold" into a question about which physics was meant.
    r"\breliability (analysis|test\w*|of (the |this |a )?(solder|joint|weld|bond|package|die|bga))",
    r"(solder|joint|weld|bond|package|bga|die)[- ]?\w* reliability\b",
)
#: WORDS FOR A FLUID: what CFD answers.
_FLOW = _rx(
    # \baero as a PREFIX matches AEROSPACE, so "aerospace bracket, static stress" read as
    # mechanics AND flow and asked which domain, on a sentence with no fluid in it at all.
    #
    # ANCHORING IT DROPPED THE TWO AERO WORDS THAT ARE NOT "AERO". Measured against main on 70
    # phrases, 2026-11-12 - origin/main's reading first, then HEAD 42d8a731's, which is what the
    # two patterns below put back (this file reads all of these as origin/main does):
    #
    #     mesh this aerofoil                   flow external  ->  ask domain, why "no cue"
    #     aeroacoustics of the side mirror     flow external  ->  ask domain, why "no cue"
    #
    # The prefix had been carrying them, so the anchor took them with it, and the reason string
    # then reported no cue on a sentence whose only noun is an aerofoil. Named here instead of
    # loosening the anchor back: each of these can only mean a fluid moving past a body, which
    # "aerospace" cannot.
    #
    # THE AMERICAN SPELLING IS DELIBERATELY NOT HERE. `\baero` never matched "airfoil" either, so
    # adding it would not restore a reading - it would change one. Measured 2026-11-12 side by
    # side with origin/main and HEAD 42d8a731: "mesh this airfoil" asks the domain question on all
    # three, and twelve mechanics frames ("stress in this airfoil" and the rest) read fea on all
    # three. Adding it would move the first and leave the second where it is, because these two
    # patterns are read as a body's NAME below (_A_BODYS_NAME) and a body's name loses to a
    # mechanics word. Restoring what the anchor took is still the whole job.
    r"\bcfd\b", r"\bfluid\b(?![- ]structure)", r"\bflow", r"\bairflow", r"\baero\b",
    r"\baerodynamic", r"\baerofoil", r"\baeroacoustic", r"\bdrag\b",
    r"\blift\b", r"\bdownforce", r"\bwind\b", r"pressure (drop|loss)", r"\bhead loss", r"\bturbulen",
    r"\bopenfoam\b", r"\bfluent\b", r"star-?ccm", r"\bcfx\b", r"\bvelocity", r"\b\d+\s?(m/s|km/h|mph)",
    r"\bmach\b", r"\breynolds", r"\by\+", r"\byplus", r"boundary layer", r"\binlet", r"\boutlet",
    r"\bnozzle", r"\bduct\b", r"\bmanifold", r"\bwater\b", r"\bgas\b", r"\bcombust", r"\bmixing\b",
    r"\bwake\b", r"\bswirl", r"\bhydrodynamic", r"\bcavitat", r"\bimpeller", r"\bventilat", r"\bhvac\b",
    r"\bslosh", r"\bwind tunnel", r"\bair (move|moves|around|over|through)", r"goes in .* out",
    r"\bblood\b", r"\bk-?(omega|epsilon)\b", r"\bpolyhedral\b",
    # HOW AN AERODYNAMICIST ACTUALLY WRITES IT. "cd of this ahmed body" settled nothing and asked
    # the domain question, which is a poor showing for the half of the platform that has
    # customers: Cd is the drag coefficient, and the Ahmed body and the DrivAer are the two
    # reference cars every automotive CFD group runs. Found by writing the test, not by a report.
    r"\bc_?d\b", r"\bahmed\b", r"\bdrivaer\b",
)
#: Words that put a flow INSIDE the part, and AROUND it.
_INTERNAL = _rx(r"\bthrough\b", r"\binside\b", r"\binternal", r"\bpipe", r"\bduct", r"\bmanifold",
                r"\bvalve", r"\bnozzle", r"pressure (drop|loss)", r"\bfilter\b", r"\bgoes in\b",
                r"\bartery", r"\bimpeller", r"\bmixing\b", r"\bin the (room|tank|combustor)")
_EXTERNAL = _rx(r"\baround\b", r"\bover\b", r"\bexternal", r"\baero", r"\bdrag\b", r"\blift\b",
                r"\bdownforce", r"\bwind\b", r"\bhull\b", r"\bwake\b")
#: ...and the words that can ONLY mean flow past a body, which is what may overrule a piece of
#: thermal hardware. It began as a subset of _EXTERNAL and is a set of its own now - two lessons
#: apart, below. "around" and "over" are in _EXTERNAL above because they are how a user says
#: which way the fluid goes, and they are right for that - but they are hopeless as a veto, and
#: using the whole of _EXTERNAL for one sent these to plain car-style CFD:
#:
#:     airflow over the PCB                 electronics -> flow external
#:     airflow around the GPU               electronics -> flow external
#:     air over the IGBT module             electronics -> flow external
#:     pressure drop over the cold plate    electronics -> flow
#:
#: with "pressure drop ACROSS the cold plate" still reaching cooling, so one preposition decided
#: the mesher. "airflow over the heat sink" survived only because "heat" is itself a heat word and
#: short-circuited the veto before it ran.
#:
#: ANCHORED, and measured. `\baero` as a prefix matches AEROSPACE, so "aerospace power module cold
#: plate, coolant at 2 L/min" vetoed its own coolant and went to car-style CFD (why="flow: aero"),
#: as did "aerospace cold plate thermal analysis". A whole industry's name is not a question about
#: air flowing past a body.
#:
#: AND IT IS THE QUANTITY ASKED FOR, NOT WHERE THE AIR IS. `\bexternal` and `\bwind` were in this
#: set too, and they say where the air is rather than what is being asked about it, which is how
#: a user says a part is AIR-COOLED. Measured against main on 70 phrases, 2026-11-12 -
#: origin/main's reading first, then HEAD 42d8a731's, which is what taking the two words out of
#: this set put back (this file reads all four as origin/main does):
#:
#:     wind tunnel test of a heat sink                electronics  ->  flow external
#:     heat sink in a wind tunnel at 5 m/s            electronics  ->  flow external
#:     cooling the brake disc with external airflow   electronics  ->  flow external
#:     turbine blade cooling with external flow       electronics  ->  flow external
#:
#: Four pieces of genuinely air-cooled thermal work sent to car-style CFD, where the metal is
#: never meshed. A wind tunnel is where you measure a heat sink; "with external airflow" names
#: the coolant. What is left here is the aerodynamic QUANTITY or PHENOMENON of a body in a free
#: stream, and every veto the set was built for rests on one of those: "external drag on an
#: electronics enclosure" (drag), "wake behind the chip conveyor" (wake), "downforce on this
#: cooling vent" (downforce).
#:
#: AND A BODY'S NAME IS NOT A QUANTITY EITHER, which is why `aerofoil` and `aeroacoustic` are NOT
#: here. They were, on the argument that each names a body in a free stream - but this set is read
#: as a VETO over heat, and a turbine blade is an aerofoil whose whole job is heat. Measured
#: 2026-11-12 on 192 generated turbomachinery and acoustics phrases - the 12 bodies x 16 frames
#: of tests/unit/application/purpose_phrases.TURBO_BODIES and TURBO_FRAMES - through four
#: purpose_maps: origin/main, HEAD 42d8a731, this file with the two patterns in the veto, and this
#: file with them out of it. 96 phrases change, and in ALL 96 the reading with them OUT matches
#: both origin/main AND HEAD, while the reading with them in matches NEITHER on any of the 96; 94
#: of the 96 are a settled route under the veto and not an ask. Both baselines first, then the
#: reading with the two patterns in the veto:
#:
#:     conduction through the turbine blade aerofoil            fea_heat     ->  flow
#:     internal cooling of the aerofoil                         electronics  ->  flow
#:     cooling the nozzle guide vane aerofoil with film holes    electronics  ->  flow
#:     conduction through the aeroacoustic liner                 fea_heat     ->  flow
#:     thermal stress in the compressor aerofoil                 fea_heat     ->  fea
#:
#: Four of those are a WRONG ROUTE and not an ask - the cost this file's own doctrine says must
#: never be paid - and the fifth keeps the FEA path but loses the heat in the solid, so a thermal
#: stress reads as a static one. The customer is a gas-turbine one, not a semiconductor one. The set's own
#: corpus labels "turbine blade cooling with external flow" electronics, and with the two patterns
#: in the veto "turbine blade aerofoil cooling" read flow.
#:
#: AND ON NONE OF THE 192 DOES THIS FILE MATCH NEITHER BASELINE: 59 of them restore origin/main's
#: reading (which HEAD's `\baero\b` anchor had cost), 10 keep HEAD's, and on the other 123 the two
#: baselines agree and so does this file.
#:
#: THE REMOVAL COSTS NOTHING AND KEEPING THEM BOUGHT NOTHING. Measured the same day on the shared
#: set, 251 phrases: 242/251 with them in the veto and 242/251 with them out, the same nine
#: misses either way (misses only with them out: none; misses only with them in: none). The four
#: phrases they were added for - "mesh this aerofoil", "aeroacoustics of the side mirror",
#: "aerofoil at 2 degrees incidence", "aeroacoustic noise from the wing" - read flow/external on
#: origin/main, with them in the veto AND with them out, because `\baerofoil` and `\baeroacoustic`
#: in _FLOW already carry them. They stay in _FLOW; they are out of the veto.
#:
#: `external` AND `wind` DID NOT STOP BEING EVIDENCE, THEY STOPPED BEING THIS SET'S KIND OF
#: EVIDENCE, and they are read at the gate's weaker half now (_WHERE_THE_AIR_IS, below). The measurement
#: that used to sit here was "wind tunnel test of this enclosure", and it was the wrong phrase: a
#: bare enclosure is not in _THERMAL_HARDWARE, so the gate cannot open on it and that sentence
#: reads as flow whether these two words are in this set or not. Add the one word that makes it a
#: real customer sentence - electronics - and it reverses. Measured 2026-11-12 with HEAD 42d8a731's
#: purpose_map and this one loaded side by side as standalone modules, HEAD's reading first:
#:
#:     wind tunnel test of this enclosure                      flow external  ->  flow external
#:     wind tunnel test of this electronics enclosure          flow external  ->  electronics
#:     wind tunnel test of an electronics enclosure at 20 m/s  flow external  ->  electronics
#:     wind pressure on the electronics enclosure              flow external  ->  electronics
#:     external flow over the inverter cabinet with its fans   flow external  ->  electronics
#:
#: Four ways of asking for the wind loading or the wind-tunnel aero of an outdoor electronics
#: cabinet, all four taken down the conjugate-cooling path, and the last says "external flow"
#: outright. Nothing was left to veto them: `enclosure` and `fans?` are themselves the
#: corroboration _COOLING_AT_IT accepts.
_PAST_A_BODY = _rx(r"\baero\b", r"\baerodynamic",
                   r"\bdrag\b", r"\blift\b", r"\bdownforce", r"\bhull\b", r"\bwake\b")
#: A BODY'S NAME, AND NOT A QUESTION ABOUT A FLUID - the two flow cues this branch added, held to
#: the same weak-evidence rule a thermal hardware noun is held to at the heat gate.
#:
#: `\baerofoil` and `\baeroacoustic` are in _FLOW because an aerofoil is a body in a free stream
#: and nothing else (see _FLOW). But they name the PART, the way `cold plate` and `chip` do, and
#: the heat gate already settles what a part's name is worth: it opens on a hardware noun only
#: where "the words name no mechanics" (`not fea`, in read_purpose below). The flow side had no
#: such rule, so adding these two patterns created a tie that HEAD 42d8a731 did not have and
#: turned a stress analysis of an aerofoil into a question about which physics was meant.
#:
#: Measured 2026-11-12, HEAD 42d8a731's purpose_map and this one loaded side by side as standalone
#: modules, over twelve mechanics frames x two British-spelt bodies - 24 phrases, HEAD's reading
#: first, and HEAD's reading on all 24 is "fea":
#:
#:     stress in this aerofoil                   fea  ->  ask domain, why "both: stress, aerofoil"
#:     vibration of this aerofoil                fea  ->  ask domain
#:     modal analysis of this aerofoil           fea  ->  ask domain
#:     FEA of this aerofoil                      fea  ->  ask domain
#:     is this aerofoil strong enough            fea  ->  ask domain
#:     fatigue of this aerofoil                  fea  ->  ask domain
#:     buckling of this aerofoil                 fea  ->  ask domain
#:     ...and the same twelve again for "aeroacoustic liner"
#:
#: A stress analysis of an aerofoil is FEA. The rule below reads it as FEA again: where EVERY flow
#: cue in the sentence is one of these body names and the words also name mechanics, the mechanics
#: decides. One aerodynamic quantity in the sentence ("drag on the aerofoil") and the set is no
#: longer the whole of the flow evidence, so the tie stands and the domain question is asked.
#:
#: THE AMERICAN SPELLING IS STILL NOT IN _FLOW, and this rule is why it need not be: adding
#: `\bairfoil` would no longer cost "stress in this airfoil" its mechanics reading, but it would
#: still MOVE one - "mesh this airfoil" asks the domain question on origin/main, on HEAD and here -
#: and no finding asks for that. Restoring what the `\baero\b` anchor took is still the whole job.
_A_BODYS_NAME = _rx(r"\baerofoil", r"\baeroacoustic")
#: WHERE THE AIR IS - the weaker half of the same argument, and it is read in ONE place: whether a
#: bare electronics noun may open the heat gate. It must not beat a heat word, because "cooling the
#: brake disc with external airflow" and "wind tunnel test of a heat sink" are air-cooled thermal
#: work that names its coolant with exactly these two words. It must beat a hardware noun that has
#: nothing behind it but an enclosure or a fan, because that is the four readings above. The
#: strength is the difference between the two sets, not the words in them.
#:
#: TWO WORDS, AND NOT THE WHOLE OF _EXTERNAL. "around" and "over" are the rest of that set and they
#: are hopeless as a veto at any strength, which is the lesson _PAST_A_BODY was split out of
#: _EXTERNAL to record. Measured 2026-11-12 on the 251-phrase shared set: these two words score
#: 242/251, and the whole of _EXTERNAL substituted in their place scores 237/251. The five it loses
#: are "airflow over the PCB", "airflow around the GPU", "air over the IGBT module", "air around
#: the chip package" and "fan blowing over the processor" - four of them back on the car-style
#: path where the metal is never meshed, and the fifth asking which physics was meant. "pressure
#: drop over the cold plate" survives even that, because a cold plate is a coolant said outright
#: and the coolant beats both vetoes.
#:
#: AND IT REACHES WELL BEYOND COLD PLATES - recorded here because the decision is kept and its
#: breadth was not. `external` used to veto the heat gate outright; at this weaker strength it
#: only vetoes a BARE hardware noun, so every sentence whose heat word is its own ("external
#: thermal resistance of ...") now opens the gate where it did not. Measured 2026-11-12 over 490
#: generated phrases - the 35 ordinary parts x 14 "external ..." frames of
#: tests/unit/application/purpose_phrases.EXTERNAL_BREADTH_PARTS and EXTERNAL_BREADTH_FRAMES -
#: HEAD 42d8a731 and this file side by side as standalone modules: 291 of the 490 move, in seven
#: shapes:
#:
#:     ask domain     ->  electronics  140
#:     ask domain     ->  ask thermal  110
#:     flow           ->  electronics   25
#:     fea            ->  ask domain     5
#:     flow external  ->  electronics    5
#:     fea            ->  ask thermal    4
#:     ask domain     ->  fea_heat       2
#:
#: "external thermal resistance of the <part>" routes to electronics cooling for 34 of the 35
#: parts, a car body, an engine block, a cylinder head, a bridge deck, a ship and a solar panel
#: among them, and six of those were flow on HEAD (the exhaust manifold, the duct, the nozzle, the
#: mixing tank, the wind turbine and the impeller). The one exception is the weldment, and it is
#: also where every one of the nine fea rows above is: `weld` is a mechanics cue, so HEAD read
#: "external thermal resistance of the weldment" as the FEA path outright (route fea, analysis
#: thermal, why="mechanics: weld") and this file asks which physics was meant - which is what
#: origin/main does with it too. Only FOUR of the 490
#: appear in the 251-phrase shared set (the first frame on the engine block, the car body, the duct
#: and the nozzle), so the score says almost nothing about any of this;
#: tests/unit/application/purpose_phrases.py carries six rows as its `semi_breadth` origin and
#: test_the_word_external_reaches_past_cold_plates runs the whole 490, so the next reader finds
#: this scored rather than argued.
#:
#: It is kept because "external thermal resistance" is how the trade states a cooling problem and
#: `thermal resistance` is in _THERMAL_HARDWARE for that reason. The cost is a thermal reading on
#: an engine block where HEAD asked which physics was meant - an ask traded for a thermal route,
#: not a wrong route traded for a right one.
_WHERE_THE_AIR_IS = _rx(r"\bexternal", r"\bwind\b")

#: COOLANT INSIDE METAL, SAID OUTRIGHT - and these beat the veto above.
#:
#: The veto is there because aerodynamics vocabulary is not what anyone reaches for about coolant
#: inside metal. These words are the other half of that argument: they are not what anyone reaches
#: for about air flowing past a body. Measured, every one of these went to car-style CFD:
#:
#:     aerospace power module cold plate, coolant at 2 L/min   flow: aero
#:     pin fin cold plate, I need the wake behind each pin     flow: wake
#:     externally finned cold plate, water cooling             flow: water
#:     external thermal resistance of this cold plate          ask domain, why "no cue"
#:
#: The last is the worst of the four: the veto suppressed every cue and the reason string then
#: denied they were there.
_COOLANT_INSIDE = _rx(r"\bcoolant\b", r"\bcold[- ]plate", r"\bwater[- ]?block", r"\bmicrochannel",
                      r"\bmicro[- ]channel", r"\bwater[- ]cool", r"\bliquid[- ]cool",
                      r"\bl\s*/\s*min\b", r"\blpm\b", r"\bgpm\b")

#: WORDS THAT PUT HEAT IN THE QUESTION. Any one of them and the thermal split decides.
_HEAT = _rx(r"\bthermal", r"\bheat", r"\bhot\b", r"\bwarm", r"\btemperature", r"\bcool", r"\boverheat",
            r"\bconduct", r"\bconvect", r"junction temp", r"\bdissipat", r"\bconjugate\b", r"\bcht\b",
            r"\bthermo", r"\bflotherm", r"\bicepak", r"\becxml\b",
            # CTE is the coefficient of thermal expansion. "stress from CTE mismatch in the die
            # attach" named no heat at all without it and went to plain structural work.
            r"\bcte\b")
#: ...cooling something: electronics cooling (the approved split's words).
_COOLING = _rx(r"\bcool", r"\bheat ?sinks?\b", r"\bfans?\b", r"\bchips?\b", r"\bpcbs?\b", r"circuit board",
               r"\bboards?\b", r"\belectronic", r"\benclosure", r"\bcpus?\b", r"\bgpus?\b", r"\bprocessor",
               r"\bserver", r"\brack\b", r"power (module|electronics)", r"\binverter", r"\bleds?\b",
               r"junction temp", r"thermal management", r"\becxml\b", r"\bflotherm", r"\bicepak",
               r"6sigma", r"\bconjugate\b", r"\bcht\b", r"\bconvect", r"\b\d+(\.\d+)?\s?w\b", r"\bairflow")
#: HARDWARE THAT IS THERMAL BY ITS NATURE. Nobody meshes a cold plate, a heat sink or a die for
#: any reason but heat, so these nouns put heat in the question on their own - the words need not
#: say "thermal", "heat" or "cool" at all. Measured: without this, "mesh this cold plate" and
#: "150 W processor with a fan" settled nothing and the domain question was asked, because the
#: heat gate wanted a heat word and a cold plate has none ("cold" is not one). "die stack thermal"
#: reached the gate and then matched neither cooling nor heat-in-a-solid, so the thermal question
#: was asked about a die stack.
#:
#: THE GATE ONLY OPENS ON THESE WHERE THE WORDS NAME NO MECHANICS. "stress in this PCB" is a
#: board-level drop test and goes to FEA as it always did; it is the hardware noun that is weak
#: evidence here, not the heat.
_THERMAL_HARDWARE = _rx(
    # the cooling hardware itself
    r"\bcold ?plates?\b", r"\bheat ?sinks?\b", r"\bheat ?spreaders?\b", r"\bheat ?pipes?\b",
    r"\bvapou?r chamber", r"\bmicro ?channel", r"\bmini ?channel", r"\bpin ?fins?\b",
    # NOT a bare "tim": TIM is the thermal interface material, and "mesh this part for Tim" went
    # to electronics cooling on a colleague's name. Not "radiator" or "cooler" either - both are
    # ordinary automotive and HVAC parts, and "race car radiator duct" and "airflow through a
    # radiator core" are CFD questions, not semiconductor ones.
    r"\bcoolants?\b", r"\btim ?[0-9]\b", r"thermal interface",
    r"thermal interface material", r"\bimmersion cool", r"\bdirect[- ]to[- ]chip\b", r"\bd2c\b",
    # the thing being cooled
    # A DIE IS ONLY A SILICON DIE WHEN THE WORDS SAY SO. A bare "die" sent an extrusion die, a
    # stamping die and a spinneret die to electronics cooling; excluding "die cast" was not
    # nearly enough, because every other kind of die is a tool too.
    r"\bchips?\b", r"\bdie ?(stack|attach|shadow)",
    r"\b(silicon|bare|packaged?|flip[- ]?chip|logic|memory) dies?\b", r"\bwafers?\b",
    r"\bcpus?\b", r"\bgpus?\b", r"\bprocessors?\b", r"\basics?\b", r"\bsocs?\b",
    r"\bigbts?\b", r"\bmosfets?\b", r"\bsemiconductor", r"\binterposer",
    r"\b(ic|chip|semiconductor|electronic)[- ]packages?\b", r"\bbgas?\b", r"\bflip[- ]?chip",
    r"\bqfns?\b", r"\blgas?\b", r"\bpcbs?\b", r"circuit board", r"\belectronics?\b",
    r"power (module|electronics)", r"\binverters?\b",
    # how the trade states the problem
    r"\btdp\b", r"\bhot ?spots?\b", r"junction[- ]to[- ]ambient", r"junction temp",
    r"thermal resistance", r"\btheta[- ]?ja\b",
)
#: ...AND THE HALF OF THEM THAT SPEAKS FOR ITSELF: the nouns that name heat, coolant, a cooling
#: device or silicon outright. Nobody says "cold plate", "heat sink", "TIM2", "junction to ambient"
#: or "bare die" about anything but heat, so one of these opens the gate on its own.
#:
#: THE OTHER HALF IS A PIECE OF ELECTRONICS WITH AN EVERYDAY HOMONYM - a chip, a wafer, a
#: processor, an interposer, an inverter - and on its own it is no evidence of heat at all.
#: Measured against main on 70 phrases, 2026-11-12 - origin/main's reading first, then HEAD
#: 42d8a731's, which is what splitting this set off put back (this file reads all five as
#: origin/main does):
#:
#:     inverter duct flow                         flow internal  ->  electronics
#:     mesh this wafer check valve                ask domain     ->  electronics
#:     chip ejection chute                        ask domain     ->  electronics
#:     processor of the signal, mesh the housing  ask domain     ->  electronics
#:     mesh this interposer plate                 ask domain     ->  electronics
#:
#: A wafer check valve is a valve, a chip ejection chute carries swarf, "processor of the signal"
#: is a verb and an interposer plate is a plate. The first is the worst of the five: a hardware
#: noun beat "duct flow", which says outright what the user wants. This is the same trap as "mesh
#: this part for Tim" matching TIM, which the file already names; these five are it with a noun
#: that happens to be a real semiconductor word as well.
_THERMAL_BY_NAME = _rx(
    # the cooling hardware itself, and the coolant
    r"\bcold ?plates?\b", r"\bheat ?sinks?\b", r"\bheat ?spreaders?\b", r"\bheat ?pipes?\b",
    r"\bvapou?r chamber", r"\bmicro ?channel", r"\bmini ?channel", r"\bpin ?fins?\b",
    r"\bcoolants?\b", r"\btim ?[0-9]\b", r"thermal interface", r"\bimmersion cool",
    r"\bdirect[- ]to[- ]chip\b", r"\bd2c\b",
    # silicon said outright - the qualified die the hardware set already insists on
    r"\bdie ?(stack|attach|shadow)",
    r"\b(silicon|bare|packaged?|flip[- ]?chip|logic|memory) dies?\b",
    # how the trade states a THERMAL problem: these are heat quantities, not part names
    r"\btdp\b", r"\bhot ?spots?\b", r"junction[- ]to[- ]ambient", r"junction temp",
    r"thermal resistance", r"\btheta[- ]?ja\b",
)
#: WHAT TURNS A BARE ELECTRONICS NOUN INTO A QUESTION ABOUT HEAT: something cooling it, or air
#: being moved at it. These are the `_COOLING` words that are neither heat words themselves (a
#: heat word opens the gate before any of this is read) nor hardware nouns, so they are the
#: independent evidence a weak noun needs. Every phrase in the set that rests on a weak noun is
#: carried by one of them: "150 W processor with a fan" (a wattage, a fan), "airflow over the PCB"
#: and "airflow around the GPU" (airflow), "fan blowing over the processor" (a fan), "air over the
#: IGBT module" and "air around the chip package" (air moved at it).
_COOLING_AT_IT = _rx(r"\bfans?\b", r"\bairflow", r"\bair (move|moves|moving|around|over|through)",
                     r"\bboards?\b", r"\benclosure", r"\bserver", r"\brack\b", r"\bleds?\b",
                     r"\b\d+(\.\d+)?\s?w\b", r"6sigma")
#: ...heat in a solid part: conduction, the temperature inside the part, thermal stress.
_SOLID_HEAT = _rx(
    r"\bconduct", r"thermal[- ]?(stress|strain|expansion|fatigue|structural|mechanical|deformation)",
    r"thermo[- ]?(mechanical|elastic|structural)",
    r"temperature (rise |field |distribution )?(in|inside|of|within|through|across) (the |this |a )?"
    r"(part|solid|block|body|wall|bracket|disc|disk|plate|component|beam|shaft)",
    r"heat (moving |flowing |going |travel\w* )?(through|in|inside|into) (the |this |a )?"
    r"(solid|part|wall|block|body)", r"\bin (the|this) solid\b", r"\bsolid part\b", r"\bexpansion",
    # A CTE mismatch IS differential thermal expansion, and it is how the packaging trade says it.
    r"\bcte\b",
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

#: THE COOLED SOLID PATH'S ONE REQUIRED QUESTION, and it is the FEA path's mechanism exactly.
#:
#: An FEA part has one thing the application asks for itself rather than hoping the model does:
#: the analysis type (ANALYSIS_QUESTION above, asked at agents/intake/message.analysis_question_due
#: once the part is confirmed on its stage). A cooled solid - a cold plate, a water block, a cooled
#: package - has one too, and it was prompt-only: the intake prompt block tells the model to ask
#: "what it is (water, a water-glycol mix, a dielectric) and how much of it flows", and nothing
#: made it. A prompt is a hope; this is the same question asked by the application, with buttons,
#: and only when the user's own words have not already named the coolant.
#:
#: BOTH HALVES IN ONE QUESTION, because that is what the prompt block asks for and because a
#: second question about the same passage is a nag. The buttons answer the fluid half; the rate is
#: typed, and a reply that re-asks the fluid is caught by the loop's repeat-question rule, which
#: reads the application's own questions.
COOLANTS = (
    ("water", "Water"),
    ("glycol", "A water-glycol mix"),
    ("dielectric", "A dielectric fluid"),
)
COOLANT_KEYS = tuple(k for k, _l in COOLANTS)
COOLANT_LABELS = dict(COOLANTS)
COOLANT_QUESTION = "What is the coolant, and how much of it flows?"
#: The buttons under it: the words sent are clear answers themselves, so a click reads exactly as
#: typing them would - the same rule THERMAL_CHOICES and DOMAIN_CHOICES are written to.
COOLANT_CHOICES = tuple((label, label) for _k, label in COOLANTS)

#: Which coolant the words name, most specific first: a glycol mix before the water in it, and a
#: named dielectric before the general word. "" when the words name none, and the question is then
#: the one the application asks.
_COOLANT_RX = (
    ("glycol", _rx(r"\bglycol", r"\bpgw\b", r"\begw\b", r"\bwater[- ]?glycol", r"\bantifreeze",
                   r"\bcoolanol")),
    ("dielectric", _rx(r"\bdielectric", r"\bnovec\b", r"\bfluorinert\b", r"\bfc-?\d+\b",
                       r"\bmineral oil\b", r"\btransformer oil\b", r"\bsingle[- ]phase oil\b",
                       r"\bimmersion fluid\b")),
    ("water", _rx(r"\bwater\b", r"\bdeionised water\b", r"\bdeionized water\b", r"\bdi water\b")),
)

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


def _dedupe(words: list[str]) -> list[str]:
    """The cue words in the order they were found, each once - two cue sets overlap (a chip is
    cooling hardware and a cooling word) and `why` should not say it twice."""
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def analysis_of(text: str) -> str:
    """The FEA analysis type the words name, or "" (then the FEA path asks "What will you analyze?")."""
    said = str(text or "")
    for key, rx in _ANALYSIS_RX:
        if rx.search(said):
            return key
    return ""


def coolant_of(text: str) -> str:
    """The coolant the words name, or "" (then the cooled-solid path asks COOLANT_QUESTION).

    `analysis_of` for the other path's one required question, and read the same way: the first
    pattern that matches wins, most specific first."""
    said = str(text or "")
    for key, rx in _COOLANT_RX:
        if rx.search(said):
            return key
    return ""


def _topology(text: str) -> str:
    inside, outside = bool(_INTERNAL.search(text)), bool(_EXTERNAL.search(text))
    return "internal" if inside and not outside else "external" if outside and not inside else ""


def read_purpose(text: str) -> PurposeReading:
    """Where these words send the job. The rules, in order:
    1. Words that name both physics as one coupled problem (FSI, flutter) ask the domain question.
    2. Heat in the words - a heat word, or hardware that is thermal by its nature (a cold plate,
       a die, a heat sink) where the words name no mechanics: the thermal split. Cooling words go
       to electronics cooling; words for
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
    hardware = _hits(_THERMAL_HARDWARE, said)
    # A HARDWARE NOUN ALSO LOSES TO FLOW-PAST-A-BODY WORDS, for the same reason it loses to
    # mechanics: it is the weak evidence. "external drag on an electronics enclosure" went to
    # electronics cooling on "electronic" and "enclosure" while plainly saying what it wanted.
    #
    # _PAST_A_BODY AND NOT _EXTERNAL, which is the whole lesson of this veto. The first version
    # used _EXTERNAL, whose "around" and "over" are how a user says which way the fluid goes - and
    # that sent "airflow over the PCB" to plain CFD while "pressure drop ACROSS the cold plate"
    # still reached cooling, so a preposition picked the mesher.
    #
    # Internal-flow words are deliberately not in the veto at all: a cold plate's coolant IS an
    # internal flow, and "microchannel cold plate, water at 2 L/min" has to keep reaching cooling.
    # AN AERODYNAMIC QUANTITY BEATS A HEAT WORD TOO, not just a hardware noun. Drag, lift,
    # downforce, aero, wake and hull are not words anyone reaches for about coolant inside metal,
    # and the heat word is often in the PART'S NAME rather than the question: "downforce on this
    # cooling vent" went to electronics cooling on the "cool" in "cooling", which is the vent's
    # name and not what was asked about it.
    #
    # `wind` IS NOT IN THAT LIST ANY MORE, and neither is `external`. This comment sat 240 lines
    # below the paragraph that had already taken `wind` out of the set and still said "and wind",
    # so the file carried two recorded measurements that contradicted each other about the same
    # word, and the lower one sat directly above the code it misdescribes. Both words are read at
    # the weaker strength now (_WHERE_THE_AIR_IS), which beats a bare hardware noun and loses to a
    # heat word - by design, because "cooling the brake disc with external airflow" names its
    # coolant with exactly those words. Measured 2026-11-12 in this tree, one word apart:
    #
    #     wind over this heat sink     electronics       (where-the-air-is loses to the heat word)
    #     drag on this heat sink       flow              (the quantity wins)
    #
    # `aerofoil` and `aeroacoustic` are not in the list either: they name a BODY rather than a
    # quantity, and the veto over heat is the wrong place for them - see _A_BODYS_NAME above.
    # ...AND THE COOLANT BEATS THE VETO. A part named as a cold plate, or a coolant with a flow
    # rate, is the one case where aerodynamics vocabulary sits in a question that is not about air
    # past a body: "externally finned cold plate" is a cold plate with fins on the outside.
    inside = _hits(_COOLANT_INSIDE, said)
    past_a_body = None if inside else _PAST_A_BODY.search(said)
    # ...AND A HARDWARE NOUN ONLY OPENS THE GATE WHERE SOMETHING SAYS THE QUESTION IS HEAT. That
    # is the noun itself when it names heat, coolant or silicon outright (_THERMAL_BY_NAME), the
    # coolant said outright, or something cooling it (_COOLING_AT_IT). A bare electronics noun -
    # a wafer, a chip, a processor, an interposer, an inverter - says nothing on its own, and five
    # phrases measured against main show what it cost when it did (see _THERMAL_BY_NAME).
    heat_at_it = bool(inside) or bool(_THERMAL_BY_NAME.search(said)) \
        or bool(_COOLING_AT_IT.search(said))
    # ...AND THE WEAK NOUN ALSO LOSES TO WHERE-THE-AIR-IS, which a heat word does not. Measured
    # 2026-11-12 against HEAD 42d8a731: without this clause "wind tunnel test of this electronics
    # enclosure", "wind pressure on the electronics enclosure" and "external flow over the inverter
    # cabinet with its fans" all read as electronics instead of flow/external, because `enclosure`
    # and `fans?` are the corroboration _COOLING_AT_IT accepts and `external` and `wind` had been
    # taken out of the veto above. A coolant said outright still beats it, the same as above.
    air_outside = None if inside else _WHERE_THE_AIR_IS.search(said)
    weak_noun = bool(hardware) and heat_at_it and not fea and not air_outside
    if not past_a_body and (_HEAT.search(said) or weak_noun):
        solid = _hits(_SOLID_HEAT, said)
        # The hardware noun is weak evidence, and heat-in-a-solid words outrank it the same way
        # mechanics words do at the gate: a die attach whose CTE mismatch is the question is heat
        # moving through a solid, not cooling, so the nouns do not vote for cooling here. The
        # cooling words proper still do, which is why "thermal stress in this PCB" still asks.
        cooling = _dedupe(_hits(_COOLING, said) + ([] if solid else hardware))
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
    # A BODY'S NAME IS THE WEAK EVIDENCE, exactly as a thermal hardware noun is at the heat gate
    # above ("not fea"): where the WHOLE of the flow evidence is the name of a body in a free
    # stream and the words also name mechanics, the mechanics decides. A stress analysis of an
    # aerofoil is FEA. The sentence is then read as if the name were not a flow cue at all, which
    # is exactly what HEAD 42d8a731 did before `\baerofoil` and `\baeroacoustic` joined _FLOW - so
    # the route, the analysis type AND the reason string all come out of the mechanics branch
    # below, byte-identical to HEAD on all 24 measured phrases. One aerodynamic quantity in the
    # sentence ("drag on the aerofoil") and this is no longer the whole of the flow evidence, so
    # the tie stands and the domain question is asked. See _A_BODYS_NAME.
    if fea and flow and set(flow) <= set(_hits(_A_BODYS_NAME, said)):
        flow = []
    if fea and flow:
        if _FLOW_AS_LOAD.search(said):
            return PurposeReading(route=ROUTE_FEA, analysis=analysis_of(said) or "static",
                                  why="a fluid as the load: " + ", ".join(fea))
        return PurposeReading(ask=ASK_DOMAIN, why="both: " + ", ".join(fea + flow))
    if fea:
        return PurposeReading(route=ROUTE_FEA, analysis=analysis_of(said), why="mechanics: " + ", ".join(fea))
    if flow:
        return PurposeReading(route=ROUTE_FLOW, topology=_topology(said), why="flow: " + ", ".join(flow))
    # THE VETO SAYS SO WHEN IT IS WHY NOTHING MATCHED. "external thermal resistance of this cold
    # plate" asked which domain with why="no cue", on a sentence carrying both a heat word and a
    # part that is nothing but coolant in metal: the veto had suppressed them and the reason then
    # denied they existed. A reason that contradicts the sentence it read is worse than no reason.
    if _PAST_A_BODY.search(said) and (_HEAT.search(said) or _hits(_THERMAL_HARDWARE, said)):
        return PurposeReading(ask=ASK_DOMAIN, why="flow past a body, and heat: " + ", ".join(
            _hits(_PAST_A_BODY, said) + _hits(_HEAT, said)))
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
    if text == COOLANT_QUESTION:
        # every coolant this V1 meshes is meshed the same way: no "coming soon" among them
        return [{"label": label, "send": send, "soon": False} for label, send in COOLANT_CHOICES]
    return []


#: THE HOLD LINE an FEA part is checked under (after the drawing mark, which the intake never
#: reads as declared and the console holds the chat box on).
FEA_HOLD = ("thanks - I'm checking your part now. It will appear beside this chat with its size, "
            "its unit and the result of the clean check. Check them and press Proceed.")
#: Said with the hold for heat in a solid part, which version 1 does not mesh as its own kind yet.
HEAT_IN_SOLID_NOTE = ("Heat moving through a solid part is coming soon. For now I'll mesh it as for "
                      "static stress: the same solid mesh, with every face named.")
#: Said with the hold for electronics cooling from a file that is not an ECXML thermal model.
#: IT DOES NOT SEND THEM AWAY ANY MORE. This used to read "upload the ECXML model exported from
#: your thermal tool. For this file I'll set up the cooling air", which answers "mesh this cold
#: plate" by naming a file the customer does not have and a coolant that is not theirs: a cold
#: plate's coolant is water inside metal, not air around a board. The ECXML route is still the
#: fuller one where a thermal tool is in play, so it stays - as an offer rather than an errand.
ELECTRONICS_NOTE = ("I'll set the cooling up from this file. If the part has coolant passages "
                    "through it, they'll be shown on the stage and the metal and the coolant get "
                    "meshed together. For a whole electronics-cooling model instead - every part, "
                    "its power and the fans read from the file - an ECXML export from a thermal "
                    "tool carries all of that.")


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
           "ASK_THERMAL", "COMING_SOON", "COOLANTS", "COOLANT_CHOICES", "COOLANT_KEYS",
           "COOLANT_LABELS", "COOLANT_QUESTION", "DOMAIN_QUESTION", "ELECTRONICS_NOTE", "FEA_HOLD",
           "HEAT_IN_SOLID_NOTE", "PurposeReading", "ROUTES", "ROUTE_ELECTRONICS", "ROUTE_FEA",
           "ROUTE_FLOW", "THERMAL_QUESTION", "V1_ANALYSES", "analysis_of", "choices_for",
           "coming_soon_note", "coolant_of", "fea_hold_text", "question_asked", "question_for",
           "read_purpose", "reading_for"]
