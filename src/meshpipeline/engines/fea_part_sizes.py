# Responsibility: The FEA route's use of the user's words tied to parts (cad/part_words, NOTE-4): the element sizes they asked for, part by part ("3-4 mm for the frame rail, 1.5-2 mm for the bolts"), handed to the plan as checked sizes (PLAN-fea-29), with the mapping and the pictures kept in the plan for the console.
# Boundaries: asks once per run through cad/part_words and applies nothing itself; engines/fea_plan.user_sized holds the sizes. With FEA_AI off, no words, or no answer: nothing applied, said (AI-fea-6's pattern) - never a failed run.
# Collaborates with: engines/fea_driver.py (calls decide, passes per_part to every plan), cad/part_words.py (the shared reading), engines/fea_ai.py (FEA_AI).
"""SIZES PER PART FROM THE USER'S NOTES, ON THE FEA ROUTE (AI-fea-9): cad/part_words reads which parts each phrase
means and the user's own numbers; the entries that ask for a size become {part id: {"min_m", "max_m"}} for the plan."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from meshpipeline.cad import part_words as PW

RULE = "AI-fea-9"
CONSUMER = "fea_part_sizes"
PURPOSE = "FEA: quadratic tetrahedra of a solid (structural)"

section = PW.section
part_table = PW.part_table
per_part = PW.per_part
#: NOTE-17: the faces picked on the model as areas of their own, re-checked on this run's surface
with_picks = PW.with_picks


async def decide(ws: Path, record: Mapping, surface, state: Mapping, *, job_id: str = "", run=None,
                 enabled: bool | None = None, wait_s: float = PW.TIMEOUT_S) -> dict:
    """The parts' sizes the notes ask for, read once (cad/part_words.decide on this route): never raises."""
    from meshpipeline.engines.fea_ai import FEA_AI

    return await PW.decide(ws, record, surface, state, consumer="fea_part_sizes", purpose=PURPOSE, job_id=job_id, run=run,
                           enabled=FEA_AI if enabled is None else enabled, wait_s=wait_s)


def sizes_of(ps: Mapping | None) -> dict[str, dict]:
    """The plan's sizes from a reading: part id -> {"min_m", "max_m"}; an area inside a part (NOTE-6, PLAN-fea-32)
    also carries its faces and its plain name. A whole part asked "finer" or "coarser" with no number carries
    {"scale", "quote"} instead (PLAN-fea-36), turned into sizes by `resolve_scales` once the plan's own sizes are
    known. {} when nothing was read."""
    if not ps or ps.get("status") != "used":
        return {}
    entries = list(ps.get("entries") or [])
    out = per_part(entries, ps.get("features") or {})
    names = {str(r.get("id")): str(r.get("name") or "") for r in ps.get("parts") or [] if r.get("feature")}
    for k, v in out.items():
        if v.get("faces") and names.get(k):
            v["name"] = names[k]
    features = set(ps.get("features") or {})
    for e in entries:
        scale = SCALES.get(str(e.get("ask") or ""))
        if scale is None:
            continue
        for p in [*e.get("parts", []), *e.get("rest_parts", [])]:
            if p in features:
                # an area asked "finer" or "coarser" ("make the mesh finer at the fillets"): its own faces' size
                # halved or doubled (resolve_scales), held on its faces as a sized area is (PLAN-fea-32, -36)
                out.setdefault(p, {"scale": scale, "quote": e.get("quote") or "", "rest": False,
                                   "faces": list((ps.get("features") or {})[p]), "name": names.get(p) or p,
                                   # VER-9: asked on a delivered mesh ("Request a change"): against that mesh
                                   **({"change_of": str(e["change_of"])} if e.get("change_of") else {})})
            else:
                out.setdefault(p, {"scale": scale, "quote": e.get("quote") or "", "rest": p in e.get("rest_parts", [])})
    return out


#: PLAN-fea-36: "finer" or "coarser" on a whole part with no number - the part's own size halved or doubled, the
#: same one step the flow route's part levels take (engines/snappy/part_levels: one octree level)
SCALES = {"refine": 0.5, "coarsen": 2.0}


def _zone_key(z: Mapping) -> str:
    """The part a zone of the plan belongs to, keyed as engines/fea_plan.user_sized keys it."""
    if z.get("kind") == "part":
        return str(z.get("part") or "")
    head, sep, _tail = str((z.get("faces") or [""])[0]).rpartition("_Face_")
    return head if sep else "Part_1"


def part_of_faces(faces) -> str:
    """The part an area's faces lie in, keyed as the plan's sizes are (Part_1 for a single part)."""
    return _zone_key({"faces": sorted(str(f) for f in faces)[:1]})


def resolve_scales(sizes: Mapping[str, Mapping], zones, *, base_m: float,
                   whole: Mapping[str, Mapping] | None = None) -> dict[str, dict]:
    """PLAN-fea-36: each part asked "finer" or "coarser" with no number as a size band the plan holds
    (engines/fea_plan.user_sized): the part's own size - the whole mesh's size when the notes gave one (`whole`, the
    PLAN-fea-33 entry for that part), else the rules' size for that part - times the scale, and nothing in the part
    raised: what the rules make finer there (walls, rounds, hot spots) stays as fine, so "coarser" never coarsens past
    the accuracy minimums and "finer" never coarsens a feature. Unchanged with no scale."""
    out = {k: dict(v) for k, v in (sizes or {}).items()}
    zs = list(zones or [])
    for k, v in list(out.items()):
        scale = v.get("scale")
        if scale is None:
            continue
        faces = {str(f) for f in v.get("faces") or []}
        if faces:
            # an area (NOTE-6): its own size is the size the plan gives its faces - the finest zone on them (a round's,
            # a hole's), else its part's - halved or doubled; nothing on it raised for "finer"
            own = [z for z in zs if z.get("kind") != "part" and faces & {str(f) for f in z.get("faces") or []}]
            in_part = _zone_key({"faces": sorted(faces)[:1]})
            part = next((float(z["size_m"]) for z in zs if z.get("kind") == "part" and _zone_key(z) == in_part), None)
            given = (whole or {}).get(in_part)
            whole_m = (0.5 * (float(given["min_m"]) + float(given["max_m"])) if given and "min_m" in given
                       else float(part if part is not None else base_m))
            if v.get("change_of"):
                # VER-9: asked on the DELIVERED mesh ("Request a change"): finer or coarser than what that mesh had
                # at the marked faces - never the mesh round them (reading A is the plan step's)
                out[k] = _changed_area(v, faces, zs, whole or {}, in_part=in_part, whole_m=whole_m,
                                       scale=float(scale), yours=bool(given and "min_m" in given))
                continue
            if float(scale) < 1.0:
                # "finer" at an area (Areen, 25 Nov, reading A): finer than the mesh AROUND it - half the size the
                # plan has round the area - never under what the accuracy rules already give it
                out[k] = _finer_area(v, faces, own, around_m=whole_m, scale=float(scale),
                                     yours=bool(given and "min_m" in given))
                continue
            base = min([float(z["size_m"]) for z in own] or [whole_m])
        else:
            own = [z for z in zs if _zone_key(z) == k]
            part = next((float(z["size_m"]) for z in own if z.get("kind") == "part"), None)
            given = (whole or {}).get(k)
            base = (0.5 * (float(given["min_m"]) + float(given["max_m"])) if given and "min_m" in given
                    else float(part if part is not None else base_m))
        target = base * float(scale)
        finest = min([float(z["size_m"]) for z in own] + [target])
        low = min(finest, target)
        out[k] = {"min_m": float(f"{low:.6g}"), "max_m": float(f"{2.0 * target - low:.6g}"), "scale": float(scale),
                  "rule_m": float(f"{base:.6g}"), "quote": v.get("quote") or "", "rest": bool(v.get("rest")),
                  **({"faces": sorted(faces), "name": v.get("name") or k} if faces else {})}
    return out


def _finer_area(v: Mapping, faces: set[str], own: list, *, around_m: float, scale: float, yours: bool) -> dict:
    """PLAN-fea-36, "finer" at an area with no number (reading A, Areen 25 Nov: "finer than the mesh around it"): the
    target is `scale` (half) times the size round the area (`around_m`: the user's own size for that part or the whole
    mesh, else the plan's size for the part). The size already there is, face by face, the finest the accuracy rules
    put on it (a hot spot's, a round's, a wall's), else the size round it. Every face of the area already at or under
    the target: the area KEEPS the rules' sizes (`kept`) - nothing is refined, by this rule or by the AI
    (fea_ai.apply_zones) - and the user is told so. Else the area gets the target, and what is already finer on it
    keeps its size (engines/fea_plan.user_sized). Only a number the user types goes under the rules' sizes."""
    target = around_m * scale
    per_face = [min([float(z["size_m"]) for z in own if f in {str(x) for x in z.get("faces") or []}] + [around_m])
                for f in sorted(faces)]
    there, there_low = max(per_face), min(per_face)
    kept = there <= target * (1 + 1e-6)
    low = min([float(z["size_m"]) for z in own] + [target])
    return {"min_m": float(f"{low:.6g}"), "max_m": float(f"{(target if kept else 2.0 * target - low):.6g}"),
            "scale": scale, "rule_m": float(f"{there:.6g}"), "target_m": float(f"{target:.6g}"),
            "there_m": float(f"{there:.6g}"), "there_low_m": float(f"{there_low:.6g}"),
            "around_m": float(f"{around_m:.6g}"), "yours": yours, "kept": kept,
            "quote": v.get("quote") or "", "rest": bool(v.get("rest")), "faces": sorted(faces),
            "name": v.get("name") or ""}


def _changed_area(v: Mapping, faces: set[str], zones: list, whole: Mapping[str, Mapping], *, in_part: str,
                  whole_m: float, scale: float, yours: bool) -> dict:
    """PLAN-fea-36 ON A CHANGE REQUEST (VER-9): the person is looking at the delivered mesh and asks for it finer (or
    coarser) THERE, so the reference is the size the finished run had at the marked faces - its plan there, rebuilt
    from the same payload: the rules' zones held to the user's other sizes as that run held them
    (engines/fea_plan.user_sized), face by face the finest zone on it, else its part's size. The target is that size
    times `scale` (half for "finer", twice for "coarser") at the coarsest marked face; a zone on the area finer than
    the target keeps its size. Nothing else in the plan changes. The accuracy rules set no finer bound of their own;
    the plan's element limit (PLAN-fea-8) is the only one, and the result says what was built there."""
    from meshpipeline.engines import fea_plan as P

    planned = P.user_sized([dict(z) for z in zones], whole) if whole else [dict(z) for z in zones]
    part_m = next((float(z["size_m"]) for z in planned if z.get("kind") == "part" and _zone_key(z) == in_part),
                  whole_m)
    per_face = [min([float(z["size_m"]) for z in planned
                     if z.get("kind") != "part" and f in {str(x) for x in z.get("faces") or []}] + [part_m])
                for f in sorted(faces)]
    last, last_low = max(per_face), min(per_face)
    target = last * scale
    t = float(f"{target:.6g}")
    return {"min_m": t, "max_m": t, "scale": scale, "change_of": str(v.get("change_of") or ""),
            "rule_m": float(f"{last:.6g}"), "target_m": t, "there_m": float(f"{last:.6g}"),
            "there_low_m": float(f"{last_low:.6g}"), "around_m": float(f"{whole_m:.6g}"), "yours": yours,
            "kept": False, "quote": v.get("quote") or "", "rest": bool(v.get("rest")), "faces": sorted(faces),
            "name": v.get("name") or ""}


def _mm(v_m: float, digits: int = 2) -> str:
    return f"{float(v_m) * 1e3:.{digits}g}"


def finer_outcomes(scaled: Mapping[str, Mapping] | None) -> list[dict]:
    """PLAN-fea-36: what came of each area asked "finer" with no number, from resolve_scales' entries - one row per
    ask: {"id", "name", "quote", "faces", "target_mm" (half the size round it), "there_mm" (the size the accuracy
    rules already give it; "there_low_mm" its finest), "around_mm", "yours" (the size round it is the user's own),
    "changed" (False: it keeps the rules' sizes), "words"}. For the plan's words, the "From your notes" line, the
    asked-against-built row and the plan step's chat. [] with none."""
    out = []
    for k, v in sorted((scaled or {}).items()):
        if not (v.get("faces") and "target_m" in v):
            continue
        row = {"id": k, "name": str(v.get("name") or k), "quote": str(v.get("quote") or ""),
               "faces": list(v.get("faces") or []), "target_mm": float(f"{float(v['target_m']) * 1e3:.4g}"),
               "there_mm": float(f"{float(v['there_m']) * 1e3:.4g}"),
               "there_low_mm": float(f"{float(v['there_low_m']) * 1e3:.4g}"),
               "around_mm": float(f"{float(v['around_m']) * 1e3:.4g}"), "yours": bool(v.get("yours")),
               "changed": not v.get("kept"), "change": bool(v.get("change_of")),
               "coarser": float(v.get("scale") or 0.5) > 1.0}
        row["words"] = finer_words(row)
        out.append(row)
    return out


def finer_words(row: Mapping) -> str:
    """One area's "finer" outcome in plain words (finer_outcomes' row)."""
    if row.get("change"):
        # VER-9: against the delivered mesh the person was looking at
        hi, lo = float(row["there_mm"]), float(row["there_low_mm"])
        last = f"{_mm(lo * 1e-3)}-{_mm(hi * 1e-3)}" if lo < 0.9 * hi and _mm(lo * 1e-3) != _mm(hi * 1e-3) \
            else _mm(hi * 1e-3)
        return (f"You asked for {'coarser' if row.get('coarser') else 'finer'} elements at {row['name']}: the last "
                f"mesh had about {last} mm there; this one has about {_mm(float(row['target_mm']) * 1e-3)} mm.")
    around = (f"your {float(row['around_mm']):.3g} mm" if row.get("yours")
              else f"the part's {float(row['around_mm']):.3g} mm")
    if row.get("changed"):
        return (f"You asked for finer elements at {row['name']}: the mesh there is now about "
                f"{_mm(float(row['target_mm']) * 1e-3)} mm, half of {around}; what was already finer there keeps its "
                "size.")
    hi, lo = float(row["there_mm"]), float(row["there_low_mm"])
    there = f"{_mm(lo * 1e-3)}-{_mm(hi * 1e-3)}" if lo < 0.9 * hi and _mm(lo * 1e-3) != _mm(hi * 1e-3) else _mm(hi * 1e-3)
    return (f"You asked for finer elements at {row['name']}: the mesh there is already about {there} mm, finer than "
            f"half of {around}, so it keeps that size. Give a size if you want it finer still.")


def settled(scaled: Mapping[str, Mapping] | None) -> dict[str, dict]:
    """AI-fea-1's guard (fea_ai.apply_zones, under fea_ai.SETTLED_KEY in the record): the areas asked "finer" with no
    number, whose sizes this rule settles in code - area id -> {"faces", "words"}. {} with none."""
    return {r["id"]: {"faces": r["faces"], "kept": not r["changed"], "words": r["words"]}
            for r in finer_outcomes(scaled)}


def words(ps: Mapping | None) -> list[str]:
    return PW.words(ps)


def told(ps: Mapping | None, req: Mapping | None = None) -> list[str]:
    """What the reading has to tell the user: its words when it tied a phrase to a part, set one aside or was
    unsure of one (or could not be read), or when the notes ask a size in one named place (`req`'s local rows, whose
    row points to these lines) - [] when it read the notes and found no part named in them. Its own words then only
    explain why there was nothing for it, beside the rules' line on the asks they applied ("I found only an
    instruction for 5 elements through the thickness, ..." next to "1 met", fea0162 round 3)."""
    local = any(str(r.get("field") or "") == "local" for r in (req or {}).get("rows") or [] if isinstance(r, Mapping))
    if (ps and ps.get("status") == "used" and not local
            and not (ps.get("entries") or ps.get("dropped") or ps.get("unsure"))):
        return []
    return words(ps)


def decisions(ps: Mapping | None) -> list[dict]:
    """The why-record entries: the shared reading (NOTE-4) and, when sizes came of it, this route's use (AI-fea-9)."""
    out = PW.decisions(ps, stage="fea_ai")
    sized = per_part(list((ps or {}).get("entries") or []))
    if sized:
        out.append({"stage": "fea_ai", "decided": "sizes per part", "value": len(sized), "rule": RULE,
                    "inputs": {"parts": {k: {"min_m": v["min_m"], "max_m": v["max_m"]} for k, v in sized.items()}},
                    "words": "The element sizes the user asked for, part by part; the plan holds every size in those "
                             "parts inside them (PLAN-fea-29)."})
    return out


__all__ = ["CONSUMER", "RULE", "SCALES", "decide", "decisions", "finer_outcomes", "finer_words", "part_of_faces", "part_table",
           "per_part", "resolve_scales", "section", "settled", "sizes_of", "told", "with_picks", "words"]
