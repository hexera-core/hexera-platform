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
    also carries its faces and its plain name. {} when nothing was read."""
    if not ps or ps.get("status") != "used":
        return {}
    out = per_part(list(ps.get("entries") or []), ps.get("features") or {})
    names = {str(r.get("id")): str(r.get("name") or "") for r in ps.get("parts") or [] if r.get("feature")}
    for k, v in out.items():
        if v.get("faces") and names.get(k):
            v["name"] = names[k]
    return out


def words(ps: Mapping | None) -> list[str]:
    return PW.words(ps)


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


__all__ = ["CONSUMER", "RULE", "decide", "decisions", "part_table", "per_part", "section", "sizes_of", "words"]
