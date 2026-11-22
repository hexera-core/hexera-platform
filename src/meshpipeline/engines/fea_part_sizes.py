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
                                   "faces": list((ps.get("features") or {})[p]), "name": names.get(p) or p})
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


__all__ = ["CONSUMER", "RULE", "SCALES", "decide", "decisions", "part_table", "per_part", "resolve_scales", "section",
           "sizes_of", "told", "words"]
