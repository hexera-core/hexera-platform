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

#: The fluid is read off the parts only for a file of several parts (an assembly: a valve's body,
#: disc, stem and seats) or of surfaces; one solid keeps the scout's own face-by-face reading.
MIN_PARTS = 2


def wants_fluid_volume(facts: dict) -> bool:
    """Whether the check splits the space of a CAD file: several solids, or a file of surfaces."""
    n = int(facts.get("solids") or 0)
    return n >= MIN_PARTS or (n == 0 and facts.get("body_kind") == "surface")


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
    litres = sum(float(p.get("volume_l") or 0.0) for p in passages)
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


def _summary(answer: dict) -> dict:
    keep = ("found", "reason", "error", "parts", "caps", "regions", "seconds", "pockets_left_out")
    out = {k: answer[k] for k in keep if k in answer}
    out["passages"] = [{"volume_l": p.get("volume_l"), "openings": len(p.get("openings") or []),
                        "proposed": sum(1 for o in p.get("openings") or [] if o.get("proposed"))}
                       for p in answer.get("passages") or []]
    return out


def _litres(v: float) -> str:
    if v >= 1.0:
        return f"{v:.1f} litres"
    return f"{v * 1000.0:.0f} ml"


__all__ = ["MIN_PARTS", "fold", "read_fluid_volume", "triangle_parts", "wants_fluid_volume"]
