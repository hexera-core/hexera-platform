# Responsibility: Fold the fluid an assembly holds (cad/fluid_volume) into the geometry check's
# proposal: its openings - the caps where the fluid meets the outside - become the openings the user
# confirms on the picture, its point inside the flow the seed, and what it found or could not find is
# said in the notes. When it cannot be sure it says what it found and leaves the reading it had.
# Boundaries: the scout's facts dict in, the same dict out (cad/scout's opening shape). No
# OpenCASCADE here (the split runs in a child process), no engine, no storage, no model call.
from __future__ import annotations

import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

#: The fluid is read off the parts for a file of several parts (an assembly: a valve's body, disc,
#: stem and seats) or of surfaces.
MIN_PARTS = 2
#: ...and for ONE solid when the measuring step found a way in and a way out through its faces. A
#: cold plate is a single block with coolant channels bored through it: the space inside it IS the
#: coolant, and `extract` finds the caps itself - it needs nothing declared. Before this, one
#: solid kept the scout's face-by-face reading and the coolant was never found at all, because
#: the split only ever ran on a file of several parts.
#:
#: Two mouths, not one: one hole is a bore or a bolt hole, and a passage needs both ends.
#:
#: THE MOUTHS ARE THE WHOLE TEST, and the flow the measuring step guessed is not consulted.
#: Measured on the corpus: a branching plate fed through one side read as EXTERNAL flow with three
#: mouths, and an embedded-silicon array with 200 channels read as external with two, yet both
#: extract their coolant cleanly - so requiring "internal" here threw away parts that work. What
#: it costs is one bounded split (CHECK_BUDGET_S) on a single solid that turns out to have no
#: cavity; a car body does not have two passage mouths through it, and if it did, the split finds
#: nothing and the reading it already had stands.
MIN_PASSAGE_MOUTHS = 2


def has_passage_mouths(facts: dict) -> bool:
    """Whether a single solid reads as one with passages through it, by the measuring step's own
    count of the mouths on its faces."""
    mouths = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
    return len(mouths) >= MIN_PASSAGE_MOUTHS


def wants_fluid_volume(facts: dict) -> bool:
    """Whether the check splits the space of a CAD file: several solids, a file of surfaces, or one
    solid with passage mouths in it."""
    n = int(facts.get("solids") or 0)
    if n >= MIN_PARTS or (n == 0 and facts.get("body_kind") == "surface"):
        return True
    return n == 1 and has_passage_mouths(facts)


def triangle_parts(source: Path) -> int:
    """How many closed parts a triangle file holds, counted on its own triangles (the scout's skin
    joins parts pressed face to face into one); 0 when it is too big to split anyway."""
    from meshpipeline.cad.fluid_volume import MAX_TRIANGLES, closed_pieces
    from meshpipeline.cad.stl_io import read_stl_triangles
    try:
        if (Path(source).stat().st_size - 84) // 50 > MAX_TRIANGLES:
            return 0
        tris = read_stl_triangles(Path(source))
        return closed_pieces(tris) if len(tris) <= MAX_TRIANGLES else 0
    except Exception:  # noqa: BLE001 - a skin it cannot read is no assembly to split
        return 0


def read_fluid_volume(facts: dict, source: Path, *, work: Path, scale: float = 1.0,
                      kind: str = "cad", budget_s: float | None = None) -> None:
    """Split the space the parts of `source` bound (a child process, time-boxed) and fold what it
    found into `facts`. Never raises: a split that fails or runs out of time is a note, and the
    scout's own reading stands."""
    from meshpipeline.cad.fluid_volume import CHECK_BUDGET_S, run_bounded

    if not (triangle_parts(source) >= MIN_PARTS if kind == "triangles" else wants_fluid_volume(facts)):
        # NOT LOOKING IS ALSO AN ANSWER, for the one shape where it is worth saying so: a thin
        # plate with fewer than two mouths in its faces is the open-top groove case, and it is the
        # most likely cold plate a customer sends (Tuckerman-Pease, Lee-Garimella and the Corintis
        # baseline are all of them).
        said = plate_with_too_few_mouths(facts)
        if said:
            facts.setdefault("notes", []).append(said)
        return
    try:
        answer = run_bounded({"task": "propose", "source": str(source), "scale": float(scale), "kind": kind},
                             Path(work) / "fluid_volume", budget_s or CHECK_BUDGET_S)
    except Exception as exc:  # noqa: BLE001 - the scout's reading stands
        answer = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    fold(facts, answer)


def fold(facts: dict, answer: dict) -> None:
    """Fold the split's answer (fluid_volume.Extraction.as_dict, or {"error"}) into the facts."""
    facts["fluid_volume"] = _summary(answer)
    notes = facts.setdefault("notes", [])
    if answer.get("error") or not answer.get("found"):
        why = answer.get("error") or answer.get("reason") or "no space inside the parts"
        if "do not touch" in why:
            return
        if answer.get("error"):
            # A SPLIT THAT DID NOT FINISH SAYS NOTHING ABOUT THE PART, and it used to say nothing
            # at all. why_no_coolant refuses an error on purpose ("a crash or a timeout is not the
            # geometry's fault") and the note below needs internal flow and fewer than two
            # openings, so a customer's six-solid cold plate whose split ran out of time was told
            # nothing, and every reader downstream saw the same `found` false a measured "there is
            # no coolant here" produces. One of those two is the geometry's answer and the other
            # is ours; they are not the same sentence and must not read as one.
            facts["fluid_volume"]["unfinished"] = True
            # "COULD NOT BE FOUND" IS KEPT WORD FOR WORD from the note this replaces, which
            # tests/unit/cad/test_fluid_volume pins. The sentence it is part of is new - a reader
            # has to be able to tell "we gave up" from "there is nothing in there" - but the
            # phrase a user and a test both look for is not mine to change on the way past.
            notes.append(f"the fluid inside the parts could not be found ({why}); this is a limit "
                         "of the check and not something measured about the part, so the openings "
                         "shown are read from the parts' flat faces - add or move them on the "
                         "picture")
            return
        said = why_no_coolant(answer, facts)
        if said:
            notes.append(said)
            return
        ops = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
        if len(ops) < 2 and facts.get("flow") == "internal":
            notes.append(f"the fluid inside the parts could not be found ({why}); the openings shown "
                         "are read from the parts' flat faces - add or move them on the picture")
        return
    passages = [p for p in answer.get("passages") or [] if any(o.get("proposed") for o in p["openings"])]
    proposed = [(k, o) for k, p in enumerate(passages) for o in p["openings"] if o.get("proposed")]
    if len(proposed) < 2:
        return                         # one opening is no flow through: the reading it had stands
    openings, space_of = _openings(proposed)
    if int(facts.get("solids") or 0) == 1:
        # ONE SOLID: THE SPLIT ADDS WHAT IT LEARNED AND OVERRULES NOTHING. It replaces the
        # openings for an ASSEMBLY because the scout has no good reading there to start from - it
        # sees dozens of flat faces spread over several parts and cannot tell which bound the
        # fluid. For one solid it has measured that solid's own faces directly, and a pipe tee
        # showed what the override costs: it had a correct pipe_wall reading with its three ports
        # and the split replaced it with "the space inside the parts".
        #
        # What the split is still worth here is the volume, the caps and a point inside the
        # coolant - the things a conjugate case needs and the part's faces cannot give - and all
        # three travel in facts["fluid_volume"] rather than over the top of the reading. The
        # top-level seed_point stays the scout's for the same reason the openings do. Measured:
        # six of the eleven cold plates yield their coolant this way, four exact to the CAD.
        notes.append(f"the space inside the part is {_litres(_volume_l(passages))}, open at "
                     f"{len(openings)} place{'s' if len(openings) != 1 else ''}")
        # ...BUT A READING THAT CONTRADICTS IT IS NOT SERVED AS THE DEFAULT. Overruling nothing
        # is about the OPENINGS and the input kind, which is what the pipe tee lost. The FLOW is
        # a different question and the split has just answered it: it cut the coolant out of this
        # solid and found it open at two or more places, which is a part the fluid goes THROUGH.
        #
        # WHAT IT COST TO LEAVE THIS ALONE, measured end to end on 2026-10-29. A cold plate's
        # proposal came back with two openings named inlet and outlet, their diameters and their
        # positions - and `flow: external`, because the scout reads a 24 x 16 x 3 mm plate as a
        # box in a flow (flat_share 1.32, fill 0.64). Nothing asked the user about it:
        # flow_question returns "" the moment there are two ports. Confirming what was drawn then
        # produced "No openings: the fluid flows around the whole body" - the openings dropped by
        # patches_from, which returns none for external flow - and the intake, correctly, refused
        # to mesh a conjugate case on a geometry with no coolant in it. Two openings called inlet
        # and outlet beside "the fluid flows around the whole body" is not a reading a user should
        # be asked to notice is wrong.
        #
        # solid-assembly is the kind for it, and purposes.py says so in as many words: "ONE solid
        # with coolant passages through it (a cold plate, a chip package, a heat-exchanger core),
        # where the solid is the part uploaded and the fluid is the space found inside it".
        #
        # ...FOR A SOLID BODY, AND NOT FOR A WALL. A part the scout read as `pipe_wall` or
        # `hollow_wall` is metal AROUND a fluid, and its ordinary case is the fluid alone -
        # internal CFD, nothing conjugate about it. Reading a tee as solid-assembly would impose
        # a two-region case on the commonest internal-flow part there is, and the stage already
        # OFFERS the cooled-solid reading for it when mouths were found (`_kinds_on_offer`), which
        # is the right strength for a part that could be either.
        #
        # `single_solid` is the cold plate: a solid body, with coolant found inside it. That one is
        # a conjugate case whatever else is true, and it is the one that was being read as a car.
        #
        # The first version of this set both for every one-solid part and
        # test_one_solid_keeps_the_reading_it_had caught the tee immediately.
        if str(facts.get("body_kind") or "") == "single_solid":
            facts["flow"] = "internal"
            facts["input_kind"] = "solid-assembly"
            facts.setdefault("confidence", {})["input_kind"] = 0.85
        return
    facts["openings"] = openings
    facts["openings_from"] = "fluid_volume"
    seed = passages[0].get("seed_mm")
    if seed:
        facts["seed_point_mm"] = seed
        facts["seed_point_m"] = [round(v / 1000.0, 6) for v in seed]
    if facts.get("flow") == "internal" or facts.get("body_kind") == "surface":
        # the space inside the parts IS the passage: a pipe wall's skin, an open sheet's inside
        facts["flow"], facts["input_kind"] = "internal", "body-surface"
        facts["body_kind"] = "assembly_cavity"
        facts.setdefault("confidence", {})["input_kind"] = 0.85
    litres = _volume_l(passages)
    parts = int(answer.get("parts") or 0)
    notes.append(f"the fluid is the space inside the {parts} part{'s' if parts != 1 else ''}, closed off "
                 f"at {len(openings)} opening{'s' if len(openings) != 1 else ''} ({_litres(litres)})")
    if len(passages) > 1:
        rows = "; ".join(f"space {k + 1}: openings "
                         + ", ".join(str(o["id"]) for o in openings if space_of[o["id"]] == k)
                         for k in range(len(passages)))
        notes.append(f"the parts close it into {len(passages)} spaces that do not connect ({rows}) - a "
                     "valve drawn shut, or a part sealing the passage; each space is meshed on its own")
    blind = [o for o in openings if not o.get("clear_ahead")]
    for o in blind:
        notes.append(f"opening {o['id']} opens into another part whose inside reaches the outside some "
                     "other way (a gap in it?): check it on the picture")
    gaps = [g for p in passages for g in p["openings"] if g.get("gap")]
    if gaps:
        widest = max(g["area_mm2"] / max(math.pi * math.sqrt(4.0 * g["hole_mm2"] / math.pi), 1e-9) for g in gaps)
        notes.append(f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''} between parts (up to {widest:.2f} mm "
                     "wide) where a lid does not quite meet its body: closed into the wall")
    small = sum(1 for p in passages for o in p["openings"] if not o.get("proposed") and not o.get("gap"))
    if small:
        notes.append(f"{small} small opening{'s' if small != 1 else ''} of the space (taps, vents) "
                     "sealed into the wall: add one on the picture to make it an opening")


def _openings(proposed: list) -> tuple[list[dict], dict[int, int]]:
    """The scout's opening dicts (cad/scout.Opening.as_dict) for the proposed caps, named the scout's
    way (two: the lower along their axis is the inlet; more: the widest), and which space each
    sticker opens into."""
    from meshpipeline.cad.scout import Opening, _name_openings

    pool, where = [], {}
    for k, o in proposed:
        hole = float(o["hole_mm2"]) * 1e-6
        area = float(o["area_mm2"]) * 1e-6
        w, h = (float(v) * 1e-3 for v in (o.get("wh_mm") or (0.0, 0.0)))
        wh = (w, h)
        op = Opening(face_index=-1, kind="ring", centroid=_xyz([v * 1e-3 for v in o["centroid_mm"]]),
                     normal=_xyz(o["normal"]), area=hole, wh=wh, clear_ahead=bool(o.get("clear_ahead", True)),
                     on_extremity=bool(o.get("on_extremity")), outer_wh=wh,
                     filled=max(hole - area, 0.0) if hole - area > 0.01 * hole else 0.0)
        pool.append(op)
        where[id(op)] = k
    pool.sort(key=lambda q: -(q.area - q.filled))
    named = _name_openings(pool)
    out, space_of = [], {}
    for n, op in enumerate(named, start=1):
        op.sticker = n
        op.confidence = 0.85 if (op.on_extremity and op.clear_ahead) else 0.7 if op.clear_ahead else 0.5
        out.append(op.as_dict())
        space_of[n] = where[id(op)]
    return out, space_of


def _xyz(v) -> tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


#: A part is PLATE-LIKE when its thinnest side is under this share of its longest: the shape a cold
#: plate is, and a shape a car, a housing or a rotor is not. Measured: the corpus plates run 0.006
#: to 0.034, the Ahmed reference body 0.276, a sphere 1.0.
PLATE_ASPECT = 0.1
#: ...and a passage needs a way in and a way out, so fewer mouths than this on a plate is worth
#: saying something about.
BOTH_ENDS = 2

#: WHAT TO SAY FOR EACH WAY THE COOLANT CAN FAIL TO BE FOUND. Measured on the eleven-part corpus,
#: where five parts refuse for four different reasons, and every one of them used to arrive at the
#: stage with no coolant and no reason at all - which leaves the first option on the stage (the
#: part's wall, hollow inside for the fluid) as the plausible pick, and the metal thrown away.
NO_HOLE = ("no coolant was found: there is no hole in any face for it to enter by. If the passages "
           "are grooves machined into the surface they are open at the top, and a groove has no "
           "hole to close off - send the part with its lid on, or the lid as a second solid")
SEALED = ("the space inside the part is sealed, so there is nothing to flow through it. If it "
          "should have an inlet and an outlet, they are not in this file")
LEAKING = ("the space inside the part reaches the outside somewhere other than a hole in a face, "
           "so it could not be closed off - often a gap where two surfaces should meet. The clean "
           "check above says whether the file has one")
MANY_HOLES = ("the space inside the part could not be closed off, though {n} possible openings "
              "were found in its faces. A coolant space that is open across a whole face rather "
              "than running as channels with two ends - a pin-fin or pin-array plate - is not "
              "handled yet")
FEW_MOUTHS = ("{found} in this plate's faces, and coolant needs a way in and a way out. If the "
              "channels are grooves open at the top, send the part with its lid on, or the lid as "
              "a second solid")
#: ...said so it reads as a sentence either way round, which "only 0 holes was found" did not.
_MOUTHS_FOUND = {0: "no hole was found", 1: "only one hole was found"}


def plate_like(facts: dict) -> bool:
    """Whether this is one solid shaped like something that ought to have passages through it.

    NOTHING IS SAID ABOUT A PART THAT IS NOT. The first version of these sentences told the Ahmed
    reference body that "no coolant was found: there is no hole in any face for it to enter by -
    send the part with its lid on", which is nonsense for a car and would be nonsense for a
    housing, a hull or a pressure vessel. Having no cavity is the normal state of a solid body;
    only on a plate is it worth remarking on.

    Measured thinnest-over-longest: the corpus plates run 0.006 to 0.034, the Ahmed body 0.276, a
    sphere 1.0. The size is read in millimetres, which is the key the measuring step writes
    (scout.as_dict) - reading a key it does not write is why this said nothing at all at first.
    """
    size = [abs(float(v)) for v in (facts.get("size_mm") or facts.get("size") or []) if v]
    return (len(size) == 3 and int(facts.get("solids") or 0) == 1
            and min(size) <= PLATE_ASPECT * max(size))


def why_no_coolant(answer: dict, facts: dict) -> str:
    """Which of the reasons the split failed for, in words the user can act on ("" for none).

    Read off what the split MEASURED rather than its wording: no cap candidate at all is an
    open-top groove, a leak is a gap where two surfaces should meet, and a pocket with no passage
    is a sealed cavity. Three different things to do about it, which is why they are told apart.
    """
    if answer.get("error"):
        return ""                            # a crash or a timeout is not the geometry's fault
    if not plate_like(facts):
        return ""
    # MOST SPECIFIC FIRST. A sealed pocket was measured and is a definite finding; "no cap
    # candidate" is the absence of one and must not get in front of it. Measured: the branching
    # plate has caps 0 AND pockets 1, and with the checks the other way round it was told its
    # grooves were open at the top when what it actually has is a closed network inside it.
    if int(answer.get("pockets_left_out") or 0) > 0:
        return SEALED
    if answer.get("leaks_mm"):
        return LEAKING
    caps = int(answer.get("caps") or 0)
    if caps == 0:
        return NO_HOLE
    # Holes WERE found and the space still was not closed off by them. Measured: the pin-fin plate
    # has 526 of them, because every pin's end is a candidate; its coolant is one wide chamber
    # rather than channels with two ends.
    return MANY_HOLES.format(n=caps)


def plate_with_too_few_mouths(facts: dict) -> str:
    """What to say about a plate-like solid whose faces carry fewer than two mouths ("" for
    anything else), which is why the split was never run on it at all."""
    if not plate_like(facts):
        return ""
    mouths = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
    if len(mouths) >= BOTH_ENDS:
        return ""
    return FEW_MOUTHS.format(found=_MOUTHS_FOUND[len(mouths)])


def _summary(answer: dict) -> dict:
    # leaks_mm and caps travel because the reason the coolant was not found is read off them
    # (why_no_coolant), and a reason the user can act on is the whole point of carrying them.
    keep = ("found", "reason", "error", "parts", "caps", "regions", "seconds", "pockets_left_out",
            "leaks_mm")
    out = {k: answer[k] for k in keep if k in answer}
    # seed_mm travels here because a single solid's split does not write over the top-level
    # seed_point, and a conjugate case still needs a point inside the coolant.
    out["passages"] = [{"volume_l": p.get("volume_l"), "volume_mm3": p.get("volume_mm3"),
                        "seed_mm": p.get("seed_mm"),
                        "openings": len(p.get("openings") or []),
                        "proposed": sum(1 for o in p.get("openings") or [] if o.get("proposed"))}
                       for p in answer.get("passages") or []]
    return out


def _volume_l(passages: list) -> float:
    return sum(float(p.get("volume_l") or 0.0) for p in passages)


def _litres(v: float) -> str:
    """The volume in the unit a person would use for it. A cold plate's coolant is 2.6 mm3, which
    read as "0 ml" while litres were the smallest unit on offer."""
    if v >= 1.0:
        return f"{v:.1f} litres"
    if v >= 0.001:                       # a millilitre or more
        return f"{v * 1000.0:.0f} ml"
    return f"{v * 1e6:.3g} mm3"          # 1 ml is 1000 mm3; a chip's channels live here


__all__ = ["BOTH_ENDS", "FEW_MOUTHS", "LEAKING", "MANY_HOLES", "MIN_PARTS", "MIN_PASSAGE_MOUTHS", "NO_HOLE",
           "PLATE_ASPECT", "SEALED", "fold", "has_passage_mouths", "plate_like", "plate_with_too_few_mouths",
           "read_fluid_volume", "triangle_parts", "wants_fluid_volume", "why_no_coolant"]
