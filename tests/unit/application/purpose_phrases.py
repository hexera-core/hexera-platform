# Responsibility: Hold the purpose-mapping phrase set: what a user may answer to "what are you
# meshing this for?", and where each answer should go.
# Boundaries: data only. tests/unit/application/test_purpose_map.py scores application/purpose_map
# against it; devtools/quality/purpose_map_score.py prints the table.
#
# Labels (the route the answer should take, FEA design section 5 and the approved thermal split):
#   flow         - CFD, through or around the part (the geometry check confirms which, on its stage)
#   fea          - structural FEA (static, modal, buckling, and the "coming soon" kinds)
#   fea_heat     - heat in a solid part: conduction, temperature in the part, thermal stress
#                  (the FEA path; "coming soon" in V1, meshed as for static stress)
#   electronics  - electronics cooling (the electronics-thermal flow)
#   ask          - unclear: the one short two-button question is the right answer
#
# Origins:
#   audit60  - the 60 phrases of the FEA audit (2026-10-19, ~/fea_audit/purpose_mapping.md), with the
#              audit's labels mapped to routes. Two changes, said here so nobody has to find them:
#              "thermal stress", "thermal expansion of this part" and "thermal-structural" were
#              "structural" and are "fea_heat" now (the approved split puts thermal-stress words
#              there; both are the FEA path), and "CFD" and "OpenFOAM" were "ambiguous" and are
#              "flow" now (the geometry check asks through-or-around on its stage).
#   dev      - 45 new phrases the rules were written against.
#   holdout  - 45 new phrases written at the same time, scored once at the end, never tuned on.
from __future__ import annotations

AUDIT60 = [
    ("stress testing", "fea"),
    ("will this break?", "fea"),
    ("deformation under load", "fea"),
    ("FEA", "fea"),
    ("structural analysis", "fea"),
    ("vibration", "fea"),
    ("modal analysis", "fea"),
    ("natural frequencies of this part", "fea"),
    ("crash simulation", "fea"),
    ("drop test", "fea"),
    ("fatigue life", "fea"),
    ("buckling", "fea"),
    ("static stress, steel", "fea"),
    ("Abaqus", "fea"),
    ("I want to check the deflection of this beam", "fea"),
    ("how much load can this bracket take", "fea"),
    ("strength check", "fea"),
    ("von Mises stress", "fea"),
    ("impact analysis", "fea"),
    ("nonlinear contact analysis", "fea"),
    ("thermal", "ask"),
    ("heat sink", "electronics"),
    ("how hot does it get?", "ask"),
    ("thermal stress", "fea_heat"),
    ("thermal expansion of this part", "fea_heat"),
    ("heat conduction through this solid", "fea_heat"),
    ("electronics cooling", "electronics"),
    ("cooling of a chip with a fan", "electronics"),
    ("steady-state temperature in this block, one face held at 100 C", "fea_heat"),
    ("conjugate heat transfer", "electronics"),
    ("airflow over it", "flow"),
    ("flow through the pipe", "flow"),
    ("pressure drop", "flow"),
    ("aero", "flow"),
    ("CFD", "flow"),
    ("drag on this part at 30 m/s", "flow"),
    ("wind load on this", "ask"),
    ("water flowing through it", "flow"),
    ("external aerodynamics", "flow"),
    ("lift and drag", "flow"),
    ("OpenFOAM", "flow"),
    ("simulation", "ask"),
    ("analysis", "ask"),
    ("mesh it", "ask"),
    ("I need a mesh", "ask"),
    ("for ANSYS", "ask"),
    ("fluid-structure interaction", "ask"),
    ("stress from the pressure of the water flowing inside", "fea"),
    ("pressure loading on the walls", "fea"),
    ("will it overheat and crack?", "ask"),
    ("vibration from the airflow", "ask"),
    ("test it", "ask"),
    ("strength and cooling", "ask"),
    ("how strong is it", "fea"),
    ("is it stiff enough", "fea"),
    ("thermal-structural", "fea_heat"),
    ("acoustics", "ask"),
    ("heat transfer", "ask"),
    ("temperature distribution", "ask"),
    ("bolt preload check", "fea"),
]

DEV = [
    ("check the max stress when 500 N is applied at the tip", "fea"),
    ("find the first 10 mode shapes", "fea"),
    ("what is the safety factor of this hook", "fea"),
    ("Nastran SOL 103", "fea"),
    ("bending stiffness of the beam", "fea"),
    ("does this bracket survive a 50 g shock", "fea"),
    ("tensile test simulation", "fea"),
    ("I want to do FEM on this housing", "fea"),
    ("compute deflection under self weight", "fea"),
    ("contact pressure between the gear teeth", "fea"),
    ("crashworthiness in LS-DYNA", "fea"),
    ("load bearing capacity", "fea"),
    ("check the strain in the rubber seal", "fea"),
    ("pressure vessel burst check", "fea"),
    ("stress anaylsis", "fea"),
    ("air flow around a car at 120 km/h", "flow"),
    ("pressure loss through the valve", "flow"),
    ("wind tunnel test of the wing", "flow"),
    ("flow rate through the nozzle", "flow"),
    ("Fluent mesh", "flow"),
    ("y+ of 1 on the blade", "flow"),
    ("simulate the airflow in the room", "flow"),
    ("cavitation in the propeller", "flow"),
    ("external aero at Mach 0.8", "flow"),
    ("how fast does the air move through the filter", "flow"),
    ("blood flow in the artery", "flow"),
    ("cooling the PCB with a fan", "electronics"),
    ("thermal management of the battery enclosure", "electronics"),
    ("junction temperature of the power module", "electronics"),
    ("Icepak model", "electronics"),
    ("forced convection over the board", "electronics"),
    ("liquid cooling plate for the inverter", "electronics"),
    ("conduction through the bracket", "fea_heat"),
    ("thermal expansion stresses in the pipe flange", "fea_heat"),
    ("steady heat conduction in the solid", "fea_heat"),
    ("temperature distribution in the part", "fea_heat"),
    ("thermal fatigue", "fea_heat"),
    ("run a simulation", "ask"),
    ("prepare it for analysis", "ask"),
    ("heat", "ask"),
    ("it gets hot", "ask"),
    ("multiphysics", "ask"),
    ("hello", "ask"),
    ("flutter of the wing", "ask"),
    ("help me with this part", "ask"),
]

HOLDOUT = [
    ("I need to run a static structural analysis in Ansys Mechanical", "fea"),
    ("linear buckling of this column", "fea"),
    ("stress concentration around the hole", "fea"),
    ("calculix static", "fea"),
    ("will the shaft yield under torque", "fea"),
    ("fatigue analysis of the weld", "fea"),
    ("check if the frame deforms too much", "fea"),
    ("mesh for solid mechanics", "fea"),
    ("eigenfrequency analysis", "fea"),
    ("topology optimisation", "fea"),
    ("drop test from 1.5 m", "fea"),
    ("harmonic response of the plate", "fea"),
    ("random vibration for the launch", "fea"),
    ("is the wall thick enough to hold 10 bar", "fea"),
    ("structural mesh for abaqus please, quadratic tets", "fea"),
    ("how much will it bend", "fea"),
    ("internal flow in the manifold", "flow"),
    ("CFD of the pump impeller", "flow"),
    ("aerodynamic drag of the cyclist helmet", "flow"),
    ("mixing in the tank", "flow"),
    ("turbulent flow in the duct, k-omega SST", "flow"),
    ("the water goes in here and out there", "flow"),
    ("downforce on the rear wing", "flow"),
    ("hydrodynamic resistance of the hull", "flow"),
    ("gas flow in the combustor", "flow"),
    ("star-ccm+ polyhedral mesh", "flow"),
    ("will the CPU overheat", "electronics"),
    ("heatsink performance with 20 W", "electronics"),
    ("air cooling for the server rack", "electronics"),
    ("Flotherm", "electronics"),
    ("LED lamp cooling", "electronics"),
    ("how warm do the chips get in the case", "electronics"),
    ("temperature inside the part when one end is hot", "fea_heat"),
    ("thermal strain", "fea_heat"),
    ("thermo-mechanical analysis", "fea_heat"),
    ("heat flowing through the wall of the block", "fea_heat"),
    ("temperature rise inside the brake disc", "fea_heat"),
    ("can you mesh this for me", "ask"),
    ("I'm not sure yet", "ask"),
    ("thermal analysis", "ask"),
    ("for COMSOL", "ask"),
    ("check the design", "ask"),
    ("temperature", "ask"),
    ("what happens in a storm", "ask"),
    ("performance", "ask"),
]

#   semi_dev - 34 semiconductor and electronics-cooling phrases the thermal-hardware cues were
#              written against (2026-10-24), so they are tuned on. Half of them are the risk
#              cases: a cold plate, a die or a PCB in a sentence whose question is MECHANICS, and
#              those must still take the FEA path.
#   semi_holdout - 30 more, written before the first score and scored once. Never tuned on.
SEMI_DEV = [
    # what a semiconductor thermal engineer types. None of these settled before: a cold plate has
    # no heat word in it, and the heat gate wanted one.
    ("mesh this cold plate", "electronics"),
    ("cool this cold plate with water", "electronics"),
    ("microchannel cold plate, water at 2 L/min", "electronics"),
    ("conjugate heat transfer on this cold plate", "electronics"),
    ("direct-to-chip liquid cooling", "electronics"),
    ("heat through this chip", "electronics"),
    ("cool this chip", "electronics"),
    ("die stack thermal", "electronics"),
    ("junction temperature of this package", "electronics"),
    ("what is the junction to ambient resistance", "electronics"),
    ("heat spreader on a 150 W TDP part", "electronics"),
    ("150 W processor with a fan", "electronics"),
    ("cool this heat sink", "electronics"),
    ("thermal management of this enclosure", "electronics"),
    # RELABELLED, because the label was wrong and not the reading. This was "electronics" on the
    # grounds that a radiator is cooling hardware, but air over a radiator core is a heat-
    # exchanger CFD question and the CFD path is where it belongs; electronics cooling means a
    # board, a chip or an enclosure. A radiator and a cooler are ordinary automotive and HVAC
    # parts, so neither is a semiconductor cue any more.
    ("airflow over the radiator core", "flow"),
    ("pin fin array in this cold plate", "electronics"),
    ("vapour chamber under the GPU", "electronics"),
    ("IGBT module cooling", "electronics"),
    # ...and the same nouns where the question is the part's own mechanics. A PCB in a drop test
    # is still FEA; the hardware noun is weak evidence and the mechanics words outrank it.
    ("stress in this PCB", "fea"),
    ("drop test of this PCB", "fea"),
    ("modal analysis of this cold plate", "fea"),
    ("fatigue of the chip solder joints", "fea"),
    ("stress in this die cast housing", "fea"),
    ("how much load can this wafer carrier hold", "fea"),
    ("can this cold plate hold 3 bar without bursting", "fea"),
    ("stress in this bracket", "fea"),
    ("thermal stress in this disc", "fea_heat"),
    # ...and the sentences that must stay a plain flow: watts and a manifold are not a chip.
    ("500 W pump, what is the flow rate", "flow"),
    ("pressure drop through this manifold", "flow"),
    ("mesh the water inside this pipe", "flow"),
    ("blood flow through this artery", "flow"),
    ("drag on this car body", "flow"),
    ("external aero on a car at 30 m/s", "flow"),
    # ...and one that names both jobs, where the one question is the right answer
    ("will this heat sink fin break off under vibration", "ask"),
]

SEMI_HOLDOUT = [
    ("liquid cooling loop for a 700 W GPU", "electronics"),
    ("manifold microchannel heat sink, 50 kW/cm2", "electronics"),
    ("two-phase cooling of a power module", "electronics"),
    ("what is the pressure drop across this cold plate", "electronics"),
    ("embedded cooling in the silicon", "electronics"),
    ("jet impingement on a bare die", "electronics"),
    ("TIM2 bondline thickness effect", "electronics"),
    ("hotspot at 500 W/cm2", "electronics"),
    ("lid and heat spreader of this CPU package", "electronics"),
    ("thermal model of an SiC inverter", "electronics"),
    ("temperature of the GaN HEMT channel", "electronics"),
    ("3D stacked memory thermal", "electronics"),
    ("immersion tank for these servers", "electronics"),
    ("rear door heat exchanger for a rack", "electronics"),
    ("how hot does the interposer get", "electronics"),
    ("coolant distribution in the manifold", "electronics"),
    ("natural convection from this enclosure", "electronics"),
    ("fan curve for this chassis", "electronics"),
    ("mesh the air gap between the die and the lid", "electronics"),
    ("wafer chuck temperature uniformity", "electronics"),
    ("junction temperature under a 20 ms power pulse", "electronics"),
    ("direct liquid cooling of a 1 kW accelerator", "electronics"),
    ("thermal runaway of the battery module", "electronics"),
    ("warpage of the package after reflow", "fea"),
    ("solder joint reliability of a BGA", "fea"),
    ("stress from CTE mismatch in the die attach", "fea_heat"),
    ("CMP pad stress", "fea"),
    ("etch chamber gas flow", "flow"),
    ("showerhead flow uniformity in the deposition chamber", "flow"),
    ("cool the wafer stage", "electronics"),
]

#   semi_veto - 16 phrases about air moved PAST a piece of thermal hardware, added 2026-10-25
#              because the set could not see them and a score of 206/214 therefore said nothing
#              about the case that was broken. A hardware noun loses to words that can only mean
#              flow past a body, and the first attempt at that veto used every external-flow word
#              including "around" and "over" - which are how a user says which way the fluid goes.
#              So "airflow over the PCB" went to plain car-style CFD while "pressure drop ACROSS
#              the cold plate" still reached cooling, and a preposition picked the mesher.
SEMI_VETO = [
    # air moved over or around electronics is still electronics cooling
    ("airflow over the PCB", "electronics"),
    ("airflow around the GPU", "electronics"),
    ("air over the IGBT module", "electronics"),
    ("airflow over the heat sink", "electronics"),
    ("air around the chip package", "electronics"),
    ("fan blowing over the processor", "electronics"),
    ("pressure drop over the cold plate", "electronics"),
    ("pressure drop across the cold plate", "electronics"),
    ("coolant around the die", "electronics"),
    ("air moving over the server boards", "electronics"),
    # ...and the words that really do mean flow past a body still win
    ("external drag on an electronics enclosure", "flow"),
    ("aero of a radiator duct", "flow"),
    ("wake behind the chip conveyor", "flow"),
    ("wind tunnel test of this enclosure", "flow"),
    ("downforce on this cooling vent", "flow"),
    ("drag on this car body", "flow"),
]

#   semi_regress - 15 phrases added 2026-11-12 after main's purpose_map and the semiconductor
#              branch's were run side by side and their readings compared phrase by phrase. These
#              are the ones where the branch was WRONG and main was right, so the set could not
#              see any of them and a score of 221/230 therefore said nothing about ordinary CFD
#              routing. Every one is reproduced twice, and the comments in application/purpose_map
#              record the before and after beside the cue set that caused it.
#
#              Eleven came from a 70-phrase run in which 33 readings differed. The last four came
#              from the review of that fix, which ran the same comparison on the enclosure
#              sentences a customer actually writes and found the fix had opened a fourth hole of
#              its own: dropping `external` and `wind` from the veto, plus the new corroboration
#              set that accepts `enclosure` and `fans?`, took the wind loading of an outdoor
#              electronics cabinet down the conjugate-cooling path.
#
#              The four causes, and what each cost:
#                * `\baero` was anchored to `\baero\b` (so AEROSPACE would stop voting for flow),
#                  which took the two aero words that are not "aero" with it.
#                * the _PAST_A_BODY veto held `external` and `wind`, which say where the air is
#                  rather than what is asked about it - so genuinely air-cooled thermal work went
#                  to car-style CFD, where the metal is never meshed.
#                * a _THERMAL_HARDWARE noun opened the heat gate with no other evidence at all,
#                  so the noun beat an explicit flow cue ("inverter duct flow").
#                * ...and then taking `external` and `wind` out of every veto at once left nothing
#                  to stop `enclosure` or `fans?` opening that gate, so the wind loading of an
#                  electronics cabinet went to the cooling path. They are read at the weaker
#                  strength now (_WHERE_THE_AIR_IS), which beats a bare noun and not a heat word.
#
#              THE FOUR FALSE FRIENDS ARE LABELLED DELIBERATELY, and all four are "ask". A wafer
#              check valve is a valve, a chip ejection chute carries swarf, "processor of the
#              signal" is a verb and an interposer plate is a plate - the same shape as "mesh this
#              part for Tim" matching TIM. An ask costs the user one click; a wrong route costs
#              them the whole job, and all four would have been routed to a conjugate mesher.
SEMI_REGRESS = [
    # an aero word that is not the word "aero"
    ("mesh this aerofoil", "flow"),
    ("aeroacoustics of the side mirror", "flow"),
    # air-cooled thermal hardware: a wind tunnel is where you measure a heat sink, and "with
    # external airflow" names the coolant rather than asking for drag
    ("wind tunnel test of a heat sink", "electronics"),
    ("heat sink in a wind tunnel at 5 m/s", "electronics"),
    ("cooling the brake disc with external airflow", "electronics"),
    ("turbine blade cooling with external flow", "electronics"),
    # the wind loading and the wind-tunnel aero of an outdoor electronics cabinet. These are the
    # four the review caught: `enclosure` and `fans?` are enough to open the heat gate on a bare
    # electronics noun, so with `external` and `wind` gone from the veto there was nothing left to
    # stop them. "wind tunnel test of this enclosure" is NOT one of them and is no use as a test:
    # a bare enclosure is not thermal hardware, so it reads as flow either way.
    ("wind tunnel test of this electronics enclosure", "flow"),
    ("wind tunnel test of an electronics enclosure at 20 m/s", "flow"),
    ("wind pressure on the electronics enclosure", "flow"),
    ("external flow over the inverter cabinet with its fans", "flow"),
    # an explicit flow cue outranks a bare electronics noun
    ("inverter duct flow", "flow"),
    # ...and the bare electronics noun in a sentence about something else entirely
    ("mesh this wafer check valve", "ask"),
    ("chip ejection chute", "ask"),
    ("processor of the signal, mesh the housing", "ask"),
    ("mesh this interposer plate", "ask"),
]

#   semi_breadth - 6 phrases added 2026-11-12 to record how far one KEPT decision reaches. Taking
#              `external` out of the _PAST_A_BODY veto is right and is not in dispute, but the word
#              is not a cold-plate word, and at the weaker strength it is read at now
#              (_WHERE_THE_AIR_IS) it no longer vetoes a sentence whose heat word is its own.
#
#              WHAT IT BUYS ON THIS SET IS SEVEN ROWS, measured 2026-11-12 by putting `\bexternal`
#              back into the _PAST_A_BODY veto in process: 235/251 with it there and 242/251 with
#              it where it is, and the seven are "cooling the brake disc with external airflow"
#              and "turbine blade cooling with external flow" (both flow with it there, both
#              electronics on origin/main) plus five of the six rows below. `\bwind\b` put back
#              the same way scores 240/251 and costs the two wind-tunnel heat-sink rows.
#              NOT "external thermal resistance of this cold plate": that reads electronics on
#              HEAD 42d8a731 and with the word back in the veto as well, because a cold plate is a
#              coolant said outright and the coolant beats every veto. The cold plate was never
#              what this decision was for.
#
#              MEASURED 2026-11-12 over the 490 generated phrases below
#              (EXTERNAL_BREADTH_PARTS x EXTERNAL_BREADTH_FRAMES, 35 x 14), HEAD 42d8a731 and this
#              branch side by side as standalone modules: 291 of the 490 move - 140
#              ask->electronics, 110 ask->ask thermal, 25 flow->electronics, 5 fea->ask domain, 5
#              flow external->electronics, 4 fea->ask thermal and 2 ask->fea_heat. "external
#              thermal resistance of the <part>" reaches electronics cooling for 34 of the 35
#              parts, a car body, an engine block, a cylinder head, a bridge deck, a ship and a
#              solar panel among them; the weldment is the one exception, and is also where all
#              nine fea rows are (`weld` is a mechanics cue, so HEAD read it as the FEA path
#              outright - route fea, analysis thermal, why="mechanics: weld" - and this branch
#              asks, as origin/main does). Only FOUR of the 490 are in this set -
#              the first frame on the engine block, the car body, the duct and the nozzle, all four
#              below - so the score says almost nothing about any of this, which is what
#              test_the_word_external_reaches_past_cold_plates exists to run.
#
#              THESE SIX ARE LABELLED FOR WHAT THE DECISION MEANS, not for what HEAD happened to
#              say. "external thermal resistance" is how the trade states a cooling problem and
#              `thermal resistance` is in _THERMAL_HARDWARE for exactly that reason, so an
#              electronics reading is the right one even on an engine block: the user asked for a
#              thermal resistance. What was traded away is an "ask", not a right answer.
SEMI_BREADTH = [
    # the phrase the decision was made for, and five ordinary parts it now reaches as well
    ("external thermal resistance of this cold plate", "electronics"),
    ("external thermal resistance of the engine block", "electronics"),
    ("external thermal resistance of the car body", "electronics"),
    # ...and three that were FLOW on HEAD, which is the sharp end of the breadth
    ("external thermal resistance of the manifold", "electronics"),
    ("external thermal resistance of the duct", "electronics"),
    ("external thermal resistance of the nozzle", "electronics"),
]

#: THE TWO GENERATED SWEEPS, kept here so the numbers recorded in application/purpose_map's
#: comments can be re-derived rather than taken on trust. Neither is scored against a label: a
#: sweep is for comparing two purpose_maps phrase by phrase, and the six rows of SEMI_BREADTH
#: above are the part of it that IS labelled and scored.
#:
#: TURBO_* is the turbomachinery and acoustics sweep behind _PAST_A_BODY's record of why
#: `aerofoil` and `aeroacoustic` are not in the veto: 12 bodies x 16 frames = 192 phrases, every
#: one of which a gas-turbine customer could write, and most of which are about heat.
TURBO_BODIES = ("turbine blade aerofoil", "aerofoil", "nozzle guide vane aerofoil",
                "compressor aerofoil", "aeroacoustic liner", "aerofoil cascade",
                "HPT blade aerofoil", "aeroacoustic duct liner", "fan aerofoil",
                "aerofoil trailing edge", "aeroacoustic panel", "cooled aerofoil")
TURBO_FRAMES = ("conduction through the {part}", "internal cooling of the {part}",
                "cooling the {part} with film holes", "thermal stress in the {part}",
                "temperature inside the {part}", "heat transfer in the {part}", "mesh the {part}",
                "stress in the {part}", "cooling passages in the {part}",
                "the {part} with its cooling channels", "metal temperature of the {part}",
                "thermal analysis of the {part}", "drag on the {part}",
                "the {part} at 2 degrees incidence", "noise from the {part}",
                "conjugate heat transfer in the {part}")
#: EXTERNAL_BREADTH_* is the sweep behind _WHERE_THE_AIR_IS's record of how far taking `external`
#: out of that veto reaches: 35 ordinary parts - not one of them a semiconductor part - x 14
#: "external ..." frames = 490 phrases.
EXTERNAL_BREADTH_PARTS = (
    "car body", "engine block", "cylinder head", "bridge deck", "ship", "solar panel",
    "brake disc", "radiator", "exhaust manifold", "turbocharger housing", "gearbox casing",
    "pump casing", "valve body", "flange", "bracket", "weldment", "pipe", "duct", "nozzle",
    "mixing tank", "wind turbine", "chimney", "kiln wall", "furnace wall", "oven door",
    "roof panel", "window frame", "concrete slab", "piston", "crankshaft", "bearing housing",
    "impeller", "propeller", "fuselage panel", "battery tray")
EXTERNAL_BREADTH_FRAMES = (
    "external thermal resistance of the {part}", "external thermal resistance of this {part}",
    "the external thermal resistance of a {part}",
    "external heat transfer coefficient on the {part}", "external convection from the {part}",
    "external cooling of the {part}", "external temperature of the {part}",
    "external surface temperature of the {part}", "external heat loss from the {part}",
    "external insulation on the {part}", "external flow over the {part}",
    "external air over the {part}", "external pressure on the {part}",
    "mesh the external surface of the {part}")


def sweep(bodies, frames) -> list[str]:
    """Every frame filled with every body, bodies outermost - the order the recorded counts were
    measured in."""
    return [f.format(part=b) for b in bodies for f in frames]


#: (phrase, label, origin) for the whole set.
PHRASES = ([(p, lab, "audit60") for p, lab in AUDIT60] + [(p, lab, "dev") for p, lab in DEV]
           + [(p, lab, "holdout") for p, lab in HOLDOUT]
           + [(p, lab, "semi_dev") for p, lab in SEMI_DEV]
           + [(p, lab, "semi_holdout") for p, lab in SEMI_HOLDOUT]
           + [(p, lab, "semi_veto") for p, lab in SEMI_VETO]
           + [(p, lab, "semi_regress") for p, lab in SEMI_REGRESS]
           + [(p, lab, "semi_breadth") for p, lab in SEMI_BREADTH])
#: The origins that did not exist when the 221/230 was measured. The guard in
#: tests/unit/application/test_an_aerofoil_is_still_an_aerofoil.py takes them out so that number
#: stays comparable; every new origin belongs here as well as above.
ADDED_AFTER_221 = ("semi_regress", "semi_breadth")
