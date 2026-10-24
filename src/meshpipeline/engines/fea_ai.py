# Responsibility: The FEA builder's judgement calls, made by the AI and held by the rules: ask the builder model once per part which measured features matter for this analysis and the user's words (per-zone priority, small features kept or set aside, an element budget the user named, a joint suggestion, plain words), and on a failed pass which allowed lever to pull; turn each answer into exact sizes inside the rules' minimums; fall back to the rules' own plan whenever the answer is missing or wrong.
# Boundaries: the model only ever picks from declared choices on ids it was shown - it writes no mesher command and no size. Every size comes from the rules here; every minimum (through-wall, per quarter-turn, the corner cap, the limits) is re-applied after its choices. A model that is off, slow, wrong or down never fails a run: the plan is the rules' own, with a note.
# Collaborates with: engines/fea_driver.py (asks once per part, applies the decision to every plan, asks for a lever after a failed pass), engines/fea_plan.py (the zones and their rules), engines/fea_ladder.py (lever_options), contracts/model_inference (the builder route; contracts/decision_cache keys and replays every round), contracts/decision_record (source ai-lever).
"""AI GOVERNS, RULES GUARANTEE (design section 15, Areen 24 Oct).

The AI is shown the part as measured and the user's words, and answers with ONE `fea_decision`
tool call against a strict schema:

    {"analysis": "static" | "modal" | "buckling", "matters": "...",
     "zones": [{"id": "H3", "priority": "refine" | "standard" | "coarser", "reason": "..."}],
     "features": [{"id": "M2" | "H3", "action": "keep" | "set_aside", "reason": "..."}],
     "joints": [{"pair": "...", "suggest": "bonded" | "contact", "reason": "..."}],
     "budget_elements": 0 | n, "words": ["...", ...]}

The rules turn it into sizes (AI-fea-n, docs/RULES.md):

  AI-fea-1  refine: a hot spot 20 elements per quarter-turn and 4 through the wall beside it; a wall 3
            through; a round 10 per quarter-turn. Never finer than half the rule's own size.
  AI-fea-2  coarser: a hot spot keeps only its shape (4 per quarter-turn, 2 through the wall); a sharp
            corner is capped at half its wall. Walls and rounds are at their minimum already.
  AI-fea-3  set aside: a hot spot no larger than 2% of the part is meshed as coarser; a wall never.
            Keep: a micro detail at least a fiftieth of the base size gets a zone its own size.
  AI-fea-4  a budget the user named lowers the element limit, never raises it.
  AI-fea-5  THE MINIMUMS HOLD after every choice: 2 through every wall, 4 per quarter-turn of every
            round, the corner cap, valid elements, the element, memory and time limits, the gates.
  AI-fea-6  no answer, a wrong answer or no AI: the rules' own plan, said in one line.
  AI-fea-7  a joint suggestion is shown for the user to confirm, never applied.
  AI-fea-8  after a failed pass the AI may pick one lever from the ones the ladder allows there.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping

from meshpipeline.contracts.event_stream import StaleExecutionPublish
from meshpipeline.engines import fea_plan as P
from meshpipeline.settings.env import optional_env

logger = logging.getLogger(__name__)

#: FEA_AI=off: the rules' own plan, no model asked (AI-fea-6).
FEA_AI: bool = (optional_env("FEA_AI", "on") or "on").strip().lower() not in ("off", "false", "0", "no")

STEP = "fea_ai"
TOOL_DECISION = "fea_decision"
TOOL_LEVER = "fea_lever"
#: how long the one decision may take before the rules' own plan goes ahead (AI-fea-6)
TIMEOUT_S = 240.0
#: the most zones shown to the model (the finest first); the rest stay standard
MAX_ZONES = 300
MAX_MICRO = 60

ANALYSES = ("static", "modal", "buckling")
PRIORITIES = ("refine", "standard", "coarser")
ACTIONS = ("keep", "set_aside")
JOINTS = ("bonded", "contact")

#: AI-fea-1
REFINE_PER_QUARTER = 20
REFINE_STRESS_WALL = 4
REFINE_WALL = 3
REFINE_ROUND_PER_QUARTER = 10
REFINE_FLOOR = 0.5
#: AI-fea-2
COARSER_CORNER_DIVISIONS = 2
#: AI-fea-3
SET_ASIDE_MAX = 0.02
KEEP_MIN = 1.0 / 50.0
#: AI-fea-4: a budget under this is not an element budget anyone means
MIN_BUDGET = 10_000

_SPOTS = ("hole", "fillet", "notch")


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _r(v: float) -> float:
    return float(f"{float(v):.6g}")


def _mm(v_m: float) -> float:
    return float(f"{float(v_m) * 1e3:.4g}")


# #
# what the model is shown
# #

SYSTEM = """You are the FEA meshing engineer for ONE solid part. The mesh is quadratic tetrahedra for the user's own solver.
Fixed rules already size every zone: at least 2 elements through every wall; at each hole, fillet or notch 10 elements per
quarter-turn and 3 through the wall beside it; sharp inside corners capped at a quarter of their wall (their stress never
settles); every other round keeps its shape with 4 per quarter-turn; sizes grow by at most 1.3; an element limit.

You make the JUDGEMENT calls only, by calling fea_decision once. You never give sizes; the rules turn your choices into
sizes and keep every minimum, whatever you choose.
- analysis: the analysis the user's words ask for (static stress, modal = vibration, buckling). If they do not say, static.
- matters: one sentence - which measured features matter for THIS analysis and THIS user, and why.
- zones: only zones whose priority should differ from standard, by the id shown.
  refine = the stress or the behaviour there matters to this user (a feature they named, a load path they mentioned, the
  stress raisers that decide a static strength check). coarser = it does not matter for this analysis. In a modal or
  buckling run stiffness and mass decide, not local stress peaks: the 10-per-quarter-turn refinement at holes, fillets
  and notches buys nothing there, so mark those coarser unless the user named them or they sit in a thin wall that sets
  the stiffness. Be selective: refining everything costs elements and time for nothing. Leave a zone out to keep it
  standard. Walls (W, T) and rounds (R, kind round) are already at the minimum the rules allow: coarser or set_aside
  does nothing to them.
- features: small features to set_aside (cosmetic chamfers, engraved text, tiny fillets: hot spots of radius at most 2%
  of the part) or micro details (ids M..) to keep because the user cares about them. Leave out what the rules decide.
- joints: only when the part is an assembly with touching pairs: bonded (welded, glued, press-fit, one piece in effect)
  or contact (parts that can slide or separate: bolted flanges, a pin in a hole, parts that only rest on each other)
  per pair id, as a suggestion the user confirms; the user's own choice on the stage is shown. Otherwise an empty list.
- budget_elements: an element budget only if the user stated one in their words, else 0.
- words: two to five short plain sentences for the user: what you prioritised and why. No jargon, no ids, no numbers you
  were not given.
Reasons are short and concrete (what the user said, or what the analysis needs)."""

LEVER_SYSTEM = """You are the FEA meshing engineer. A meshing pass failed. Pick the ONE next lever from the options given,
by its id, calling fea_lever once. Each option says what it changes. Prefer the one most likely to give a valid mesh for
the failure described; the first option is the fixed rules' default."""


def decision_tool() -> dict:
    reason = {"type": "string", "description": "short, concrete"}
    return {"type": "function", "function": {
        "name": TOOL_DECISION,
        "description": "The judgement calls for this part's FEA mesh. The rules turn them into sizes.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["analysis", "matters", "zones", "features", "joints", "budget_elements", "words"],
            "properties": {
                "analysis": {"type": "string", "enum": list(ANALYSES)},
                "matters": {"type": "string"},
                "zones": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "priority", "reason"],
                    "properties": {"id": {"type": "string"}, "priority": {"type": "string", "enum": list(PRIORITIES)},
                                   "reason": reason}}},
                "features": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "action", "reason"],
                    "properties": {"id": {"type": "string"}, "action": {"type": "string", "enum": list(ACTIONS)},
                                   "reason": reason}}},
                "joints": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["pair", "suggest", "reason"],
                    "properties": {"pair": {"type": "string"}, "suggest": {"type": "string", "enum": list(JOINTS)},
                                   "reason": reason}}},
                "budget_elements": {"type": "integer"},
                "words": {"type": "array", "items": {"type": "string"}},
            }}}}


def lever_tool(ids: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": TOOL_LEVER, "description": "The next lever, from the options given.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["lever", "reason"],
                       "properties": {"lever": {"type": "string", "enum": list(ids)},
                                      "reason": {"type": "string"}}}}}


def _zone_row(z: Mapping, spots: Mapping[str, Mapping]) -> dict:
    row: dict = {"id": z["id"], "kind": z["kind"], "faces": list(z.get("faces") or [])[:6],
                 "rule_size_mm": _mm(z["size_m"])}
    if z.get("thickness_m"):
        row["wall_mm"] = _mm(z["thickness_m"])
    if z.get("radius_m"):
        row["radius_mm"] = _mm(z["radius_m"])
    h = spots.get(str(z.get("spot") or ""))
    if h:
        for k in ("shape", "depth_mm", "length_mm", "angle_deg"):
            if h.get(k) is not None:
                row[k] = h[k] if not isinstance(h[k], float) else float(f"{h[k]:.4g}")
        at = h.get("at_mm")
        if isinstance(at, (list, tuple)) and len(at) == 3:
            row["at_mm"] = [float(f"{float(v):.4g}") for v in at]
    if z.get("at_m"):
        row["at_mm"] = [_mm(v) for v in z["at_m"]]
    return row


def brief(record: Mapping, state: Mapping, zone_list: list[dict], *, diag_m: float, limit: int) -> dict:
    """Everything the model is shown, as plain data with a fixed order (the decision cache keys the
    round by it: the same part and the same words give the same message)."""
    base = diag_m / P.MIN_DIVISIONS
    spots = {str(h.get("id")): h for h in record.get("hot_spots") or []}
    ranked = sorted(zone_list, key=lambda z: (float(z["size_m"]), str(z["id"])))
    binding = [z for z in ranked if float(z["size_m"]) < base or z["kind"] in ("hole", "fillet", "notch", "corner")]
    shown = binding[:MAX_ZONES]
    micro = list(record.get("micro") or [])[:MAX_MICRO]
    s = state or {}
    out: dict = {
        "user_request": str(s.get("request_txt") or "").strip()[:4000],
        "user_brief_for_review": str(s.get("review_brief_txt") or "").strip()[:2000],
        "mesh_detail_preference": str(s.get("effective_mesh_fidelity") or ""),
        "part": {"diagonal_mm": _mm(diag_m), "base_size_mm": _mm(base), "faces": len(record.get("faces") or {}),
                 "bodies": record.get("bodies"),
                 "thinnest_wall_mm": (record.get("thickness") or {}).get("min_mm"),
                 "hot_spots": len(spots), "element_limit": int(limit)},
        "zones": [_zone_row(z, spots) for z in sorted(shown, key=lambda z: str(z["id"]))],
        "zones_not_shown": max(0, len(zone_list) - len(shown)),
        "micro_details": [{"id": f"M{i}", "kind": m.get("kind"), "size_mm": m.get("size_mm"),
                           "faces": list(m.get("faces") or [])[:4]} for i, m in enumerate(micro, start=1)],
    }
    # AN ASSEMBLY (PLAN-fea-17, engines/fea_driver.prepare_assembly): its parts and where they meet,
    # for the joint suggestion (AI-fea-7) - the user's own answer on the stage is shown too
    asm = record.get("assembly_plan") or {}
    pairs = [p for p in asm.get("pair_list") or [] if isinstance(p, dict) and p.get("pair")]
    if pairs:
        out["assembly"] = {
            "connection_user_chose": asm.get("connection"),
            "parts": [{"id": p.get("id"), "name": p.get("name")} if isinstance(p, dict) else {"id": str(p)}
                      for p in asm.get("parts") or []],
            "touching_pairs": [{"pair": str(p["pair"]), "kind": p.get("kind"), "parts": list(p.get("parts") or []),
                                "area_mm2": p.get("area_mm2")} for p in pairs][:100]}
    return out


def messages_for(b: Mapping) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(b, sort_keys=True, separators=(",", ":"))}]


# #
# the answer, checked
# #

def validate(arguments: str, b: Mapping) -> tuple[dict | None, list[str]]:
    """(the decision, what was dropped from it) - None when the answer is not a decision at all.
    Never raises: an answer of any other shape is refused (AI-fea-6)."""
    try:
        return _validate(arguments, b)
    except Exception as exc:  # noqa: BLE001 - a malformed answer is the model's, never the run's
        return None, [f"the answer did not fit the schema ({type(exc).__name__})"]


def _list_of_objects(raw: Mapping, key: str) -> list[dict]:
    v = raw.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise TypeError(f"{key} is not a list of objects")
    return v


def _validate(arguments: str, b: Mapping) -> tuple[dict | None, list[str]]:
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return None, ["the answer was not JSON"]
    if not isinstance(raw, dict):
        return None, ["the answer was not an object"]
    notes: list[str] = []
    analysis = str(raw.get("analysis") or "")
    if analysis not in ANALYSES:
        return None, [f"the analysis {analysis!r} is not one of {ANALYSES}"]
    known_zones = {str(z["id"]) for z in b.get("zones") or []}
    known_features = known_zones | {str(m["id"]) for m in b.get("micro_details") or []}
    zones: dict[str, dict] = {}
    for z in _list_of_objects(raw, "zones"):
        zid, pr = str(z.get("id") or ""), str(z.get("priority") or "")
        if zid not in known_zones:
            notes.append(f"zone {zid!r} was not shown: ignored")
            continue
        if pr not in PRIORITIES:
            notes.append(f"zone {zid}: priority {pr!r} is not a choice: ignored")
            continue
        zones[zid] = {"priority": pr, "reason": str(z.get("reason") or "")[:300]}
    features: dict[str, dict] = {}
    for f in _list_of_objects(raw, "features"):
        fid, act = str(f.get("id") or ""), str(f.get("action") or "")
        if fid not in known_features or act not in ACTIONS:
            notes.append(f"feature {fid!r} ({act!r}) is not a choice: ignored")
            continue
        features[fid] = {"action": act, "reason": str(f.get("reason") or "")[:300]}
    joints = []
    shown_pairs = {str(p.get("pair")) for p in (b.get("assembly") or {}).get("touching_pairs") or []
                   if isinstance(p, dict)}
    for j in _list_of_objects(raw, "joints"):
        if str(j.get("pair")) in shown_pairs and j.get("suggest") in JOINTS:
            joints.append({"pair": str(j["pair"]), "suggest": str(j["suggest"]), "reason": str(j.get("reason") or "")[:300]})
    try:
        budget = int(raw.get("budget_elements") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget and budget < MIN_BUDGET:
        notes.append(f"a budget of {budget} elements is not one anyone means: ignored")
        budget = 0
    said = raw.get("words") or []
    if not isinstance(said, list):
        raise TypeError("words is not a list")
    words = [" ".join(str(w).split())[:400] for w in said if str(w).strip()][:5]
    return {"analysis": analysis, "matters": " ".join(str(raw.get("matters") or "").split())[:500],
            "zones": zones, "features": features, "joints": joints, "budget_elements": max(0, budget),
            "words": words}, notes


# #
# the rules applied to it
# #

def _floor_size(z: Mapping) -> float | None:
    """AI-fea-5: the coarsest size the rules allow this zone, whatever the AI chose (2 through the
    wall, 4 per quarter-turn of a round, the corner cap at half its wall)."""
    sizes = []
    if z["kind"] == "corner":
        if z.get("thickness_m"):
            sizes.append(float(z["thickness_m"]) / COARSER_CORNER_DIVISIONS)
    elif z.get("thickness_m"):
        sizes.append(float(z["thickness_m"]) / P.WALL_ELEMENTS)
    if z["kind"] in (*_SPOTS, "round") and z.get("radius_m"):
        sizes.append(P.QUARTER * float(z["radius_m"]) / P.ROUND_PER_QUARTER)
    return min(sizes) if sizes else None


def _targets(z: Mapping, size: float) -> dict:
    """What a refined zone's own size gives, as the measured checks read it (engines/fea_checks):
    elements per quarter-turn of a hot spot, elements through a wall."""
    out: dict = {}
    t, r = _f(z.get("thickness_m")), _f(z.get("radius_m"))
    if z["kind"] in _SPOTS and r:
        out["per_quarter"] = float(f"{min(REFINE_PER_QUARTER, P.QUARTER * r / size):.3g}")
        if t:
            out["wall_elements"] = float(f"{min(REFINE_STRESS_WALL, t / size):.3g}")
    elif z["kind"] in ("wall", "wall_spot") and t:
        out["wall_elements"] = float(f"{min(REFINE_WALL, t / size):.3g}")
    return out


def _refined(z: Mapping) -> tuple[float, str] | None:
    k, size = z["kind"], float(z["size_m"])
    t, r = _f(z.get("thickness_m")), _f(z.get("radius_m"))
    if k in _SPOTS and r:
        new = P.QUARTER * r / REFINE_PER_QUARTER
        if t:
            new = min(new, t / REFINE_STRESS_WALL)
        how = f"{REFINE_PER_QUARTER} elements per quarter-turn" + (f" and {REFINE_STRESS_WALL} through the wall" if t else "")
    elif k in ("wall", "wall_spot") and t:
        new, how = t / REFINE_WALL, f"{REFINE_WALL} elements through the wall"
    elif k == "round" and r:
        new, how = P.QUARTER * r / REFINE_ROUND_PER_QUARTER, f"{REFINE_ROUND_PER_QUARTER} elements per quarter-turn"
    else:
        return None
    return max(new, REFINE_FLOOR * size), how


def _coarser(z: Mapping) -> tuple[dict, str] | None:
    k, t, r = z["kind"], _f(z.get("thickness_m")), _f(z.get("radius_m"))
    if k in _SPOTS and r:
        size = P.QUARTER * r / P.ROUND_PER_QUARTER
        if t:
            size = min(size, t / P.WALL_ELEMENTS)
        out = {**z, "kind": "round", "rule": "PLAN-fea-7", "size_m": max(size, float(z["size_m"])),
               "was_kind": k, "reach_m": float(z.get("reach_m") or 0.0)}
        out.pop("wall_elements", None)
        return out, f"keeps only its shape: {P.ROUND_PER_QUARTER} elements per quarter-turn"
    if k == "corner" and t:
        return ({**z, "size_m": max(float(z["size_m"]), t / COARSER_CORNER_DIVISIONS)},
                f"capped at 1/{COARSER_CORNER_DIVISIONS} of its wall")
    return None


def apply_zones(zone_list: list[dict], decision: Mapping | None, record: Mapping, *,
                diag_m: float) -> tuple[list[dict], list[dict]]:
    """(the zones with the AI's choices made exact by the rules, one row per choice saying what the
    rules did with it). Every zone ends at or under its floor (AI-fea-5)."""
    if not decision:
        return [dict(z) for z in zone_list], []
    base = diag_m / P.MIN_DIVISIONS
    pri = dict(decision.get("zones") or {})
    feats = dict(decision.get("features") or {})
    applied: list[dict] = []
    out: list[dict] = []
    for z in zone_list:
        z = dict(z)
        zid = str(z["id"])
        before = float(z["size_m"])
        choice, why = None, ""
        f = feats.get(zid)
        if f and f["action"] == "set_aside":
            r = _f(z.get("radius_m"))
            if z["kind"] in _SPOTS and r and r <= SET_ASIDE_MAX * diag_m:
                choice, why = "set_aside", f["reason"]
            else:
                applied.append({"id": zid, "choice": "set_aside", "done": False, "reason": f["reason"],
                                "rule": "AI-fea-3", "words": f"{zid} is not a small detail (or is a wall): kept as the rules size it"})
        p = pri.get(zid)
        if choice is None and p and p["priority"] != "standard":
            choice, why = p["priority"], p["reason"]
        if choice == "refine":
            got = _refined(z)
            if got is None:
                applied.append({"id": zid, "choice": "refine", "done": False, "reason": why, "rule": "AI-fea-1",
                                "words": (f"{zid}: a sharp corner's stress never settles, so refining it would only add elements"
                                          if z["kind"] == "corner" else f"{zid}: nothing to refine")})
            else:
                z["size_m"], how = _r(min(before, got[0])), got[1]
                z["ai"] = "refine"
                z.update(_targets(z, float(z["size_m"])))
                applied.append({"id": zid, "choice": "refine", "done": True, "reason": why, "rule": "AI-fea-1",
                                "before_m": before, "after_m": z["size_m"], "words": f"{zid}: refined to {how}"})
        elif choice in ("coarser", "set_aside"):
            got2 = _coarser(z)
            if got2 is None:
                applied.append({"id": zid, "choice": choice, "done": False, "reason": why, "rule": "AI-fea-2",
                                "words": f"{zid}: already at the rules' minimum, unchanged"})
            else:
                z, how = got2
                z["size_m"] = _r(z["size_m"])
                z["ai"] = choice
                applied.append({"id": zid, "choice": choice, "done": True, "reason": why,
                                "rule": "AI-fea-3" if choice == "set_aside" else "AI-fea-2",
                                "before_m": before, "after_m": z["size_m"], "words": f"{zid}: {how}"})
        floor = _floor_size(z) if z.get("ai") else None    # a zone the AI did not touch is the rules' own
        if floor is not None and float(z["size_m"]) > floor * (1 + 1e-9):
            z["size_m"] = float(floor)               # AI-fea-5: never past the rules' minimum (unrounded: never over it)
        out.append(z)
    micro = list(record.get("micro") or [])[:MAX_MICRO]
    for i, m in enumerate(micro, start=1):
        f = feats.get(f"M{i}")
        if not f or f["action"] != "keep":
            continue
        size = _f(m.get("size_mm"))
        faces = [str(x) for x in m.get("faces") or []]
        if not size or not faces or size * 1e-3 < KEEP_MIN * base:
            applied.append({"id": f"M{i}", "choice": "keep", "done": False, "reason": f["reason"], "rule": "AI-fea-3",
                            "words": f"M{i}: too small for any mesh to follow ({size} mm): still set aside"})
            continue
        out.append({"id": f"K{i}", "kind": "detail", "faces": faces, "size_m": _r(size * 1e-3),
                    "reach_m": _r(size * 1e-3), "rule": "AI-fea-3", "capped": False, "ai": "keep",
                    "why": f"a {size:.3g} mm detail the user cares about: kept, with elements its own size"})
        applied.append({"id": f"M{i}", "choice": "keep", "done": True, "reason": f["reason"], "rule": "AI-fea-3",
                        "after_m": _r(size * 1e-3), "words": f"M{i}: kept, elements its own size"})
    return out, applied


def refines(decision: Mapping | None) -> bool:
    """Whether the decision asks for any refinement (AI-fea-1) or keeps a micro detail (AI-fea-3)."""
    d = decision or {}
    return (any(z.get("priority") == "refine" for z in (d.get("zones") or {}).values())
            or any(f.get("action") == "keep" for f in (d.get("features") or {}).values()))


def without_refines(ai: Mapping) -> dict:
    """AI-fea-5: the `ai` section with its refinements (and kept micro details) left out - its
    other choices stand - and the words saying why."""
    d = dict(ai.get("decision") or {})
    dropped = sorted([i for i, z in (d.get("zones") or {}).items() if z.get("priority") == "refine"]
                     + [i for i, f in (d.get("features") or {}).items() if f.get("action") == "keep"])
    d["zones"] = {i: z for i, z in (d.get("zones") or {}).items() if z.get("priority") != "refine"}
    d["features"] = {i: f for i, f in (d.get("features") or {}).items() if f.get("action") != "keep"}
    return {**ai, "decision": d, "refines_dropped": dropped}


def budget_limit(limit: int, decision: Mapping | None) -> tuple[int, str]:
    """AI-fea-4: the element limit with the user's own budget, which only ever lowers it."""
    b = int((decision or {}).get("budget_elements") or 0)
    if b and b < int(limit):
        return b, f"the user's own budget of {b:,} elements"
    return int(limit), ""


def section(decision: Mapping | None, applied: list[dict], *, status: str, note: str = "",
            key: str = "", problems: list[str] | None = None) -> dict:
    """The `ai` section of fea_plan.json: what was asked, what came back, what the rules did."""
    return {"status": status, "note": note, "key": key, "decision": dict(decision or {}),
            "applied": list(applied), "dropped": list(problems or [])}


def words(ai: Mapping | None, plan: Mapping | None = None) -> list[str]:
    """The AI's part of the plan's words (design 3.8), the rules' own numbers stay in the rules' lines.
    `plan`: the final plan, so the words say the limit it holds and whether it had to coarsen."""
    if not ai:
        return []
    if ai.get("status") != "used":
        return [str(ai.get("note") or "")] if ai.get("note") else []
    d = ai.get("decision") or {}
    out = [f"What mattered for this {d.get('analysis', 'static')} analysis: {d.get('matters')}".strip()]
    out += [str(w) for w in d.get("words") or []]
    done = [a for a in ai.get("applied") or [] if a.get("done")]
    if done:
        by: dict[str, int] = {}
        for a in done:
            by[a["choice"]] = by.get(a["choice"], 0) + 1
        said = {"refine": "refined", "coarser": "kept at their shape only", "set_aside": "set aside as small details",
                "keep": "small details kept"}
        coarsened = bool((plan or {}).get("coarsened"))
        out.append("From those choices the rules made " + ", ".join(f"{n} zone(s) {said[c]}" for c, n in by.items())
                   + ("; no choice went below 2 elements through a wall or 4 per quarter-turn of a round, and the "
                      "element limit then made the finest zones coarser, as it does for any plan (named below)."
                      if coarsened else
                      "; every wall still has at least 2 elements through it and every round at least 4 per quarter-turn."))
    if ai.get("refines_dropped"):
        out.append(f"The {len(ai['refines_dropped'])} refinement(s) asked for would take the mesh over its element "
                   "limit, so they were left out: the limit never costs the rules' own minimums for an extra.")
    asked = int(d.get("budget_elements") or 0)
    if asked:
        held = int(((plan or {}).get("limit") or {}).get("elements") or asked)
        out.append(f"Your element budget, {asked:,}, is the limit this mesh is held to." if held >= asked else
                   f"You asked for at most {asked:,} elements; the mesh job's own limit, {held:,}, is lower, so "
                   "the mesh is held to that.")
    for j in d.get("joints") or []:
        out.append(f"Suggested for {j['pair']}: {j['suggest']} ({j['reason']}). Please confirm.")
    return [w for w in out if w]


def decisions(ai: Mapping | None) -> list[dict]:
    """The why-record entries (contracts/decision_record.cite shape, source ai-lever)."""
    if not ai:
        return []
    from meshpipeline.contracts.decision_record import SOURCE_AI_LEVER

    if ai.get("status") != "used":
        return [{"stage": "fea_ai", "decided": "rules only", "value": ai.get("status"), "rule": "AI-fea-6",
                 "inputs": {"note": ai.get("note"), "dropped": ai.get("dropped") or []},
                 "words": str(ai.get("note") or "The rules' own plan.")}]
    d = ai.get("decision") or {}
    out = [{"stage": "fea_ai", "decided": "analysis", "value": d.get("analysis"), "rule": "AI-fea-1",
            "source": SOURCE_AI_LEVER, "inputs": {"key": ai.get("key"), "matters": d.get("matters"),
                                                 "dropped": ai.get("dropped") or []},
            "words": str(d.get("matters") or "")}]
    for a in ai.get("applied") or []:
        out.append({"stage": "fea_ai", "decided": f"{a['choice']} {a['id']}", "value": bool(a.get("done")),
                    "rule": a.get("rule") or "AI-fea-5", "source": SOURCE_AI_LEVER,
                    "inputs": {k: a.get(k) for k in ("reason", "before_m", "after_m") if a.get(k) is not None},
                    "words": str(a.get("words") or "")})
    if ai.get("refines_dropped"):
        out.append({"stage": "fea_ai", "decided": "refinements left out", "value": len(ai["refines_dropped"]),
                    "rule": "AI-fea-5", "source": SOURCE_AI_LEVER, "inputs": {"zones": ai["refines_dropped"]},
                    "words": "The refinements would take the plan over the element limit: left out."})
    if int(d.get("budget_elements") or 0):
        out.append({"stage": "fea_ai", "decided": "budget", "value": int(d["budget_elements"]), "rule": "AI-fea-4",
                    "source": SOURCE_AI_LEVER, "inputs": {}, "words": "The user's element budget lowers the limit."})
    for j in d.get("joints") or []:
        out.append({"stage": "fea_ai", "decided": f"joint {j['pair']}", "value": j["suggest"], "rule": "AI-fea-7",
                    "source": SOURCE_AI_LEVER, "inputs": {"reason": j["reason"]},
                    "words": "A suggestion for the user to confirm; not applied."})
    return out


# #
# asking
# #

def _args_of(round_result, tool: str) -> str | None:
    for c in getattr(round_result, "tool_calls", ()) or ():
        if getattr(c, "name", "") == tool:
            return str(getattr(c, "arguments", "") or "")
    return None


#: the least time worth asking the model in; with less left, the rules' own choice at once
MIN_WAIT_S = 20.0


async def _ask(messages: list[dict], tool: dict, *, job_id: str, user_id: str, wait_s: float = TIMEOUT_S):
    from meshpipeline.contracts import model_inference as llm_router

    return await asyncio.wait_for(llm_router.call_builder_model(
        messages, tools=[tool], tool_choice={"type": "function", "function": {"name": tool["function"]["name"]}},
        job_id=job_id, user_id=user_id), timeout=min(TIMEOUT_S, float(wait_s)))


def _account(run, round_result) -> None:
    """The round counted against the builder run, when the run counts rounds."""
    note = getattr(run, "note_plan_round", None)
    if callable(note):
        note(round_result)


def _key(messages: list[dict], tool: dict) -> str:
    import hashlib

    return hashlib.sha256(json.dumps({"m": messages, "t": tool}, sort_keys=True).encode()).hexdigest()[:16]


async def decide(record: Mapping, state: Mapping, zone_list: list[dict], *, diag_m: float, limit: int,
                 job_id: str = "", run=None, enabled: bool | None = None, wait_s: float = TIMEOUT_S) -> dict:
    """The `ai` section for this part: the checked decision ("used"), or why the rules' own plan
    goes ahead ("off", "no_time", "failed", "invalid") - never an exception (AI-fea-6). `wait_s`:
    the most the attempt can give the model, after the meshing it still has to do."""
    if not (FEA_AI if enabled is None else enabled):
        return section(None, [], status="off", note="")
    if wait_s < MIN_WAIT_S:
        return section(None, [], status="no_time",
                       note="The AI was not asked (the attempt had no time to spare); this is the rules' own mesh.")
    b = brief(record, state, zone_list, diag_m=diag_m, limit=limit)
    msgs = messages_for(b)
    tool = decision_tool()
    key = _key(msgs, tool)
    from meshpipeline.contracts import execution_guard as _fence

    # FENCE: a fenced worker must not spend provider budget on a part a newer generation owns
    await _fence.assert_still_owner("fea ai decision")
    try:
        rr = await _ask(msgs, tool, job_id=job_id, user_id=str((state or {}).get("user_id") or ""), wait_s=wait_s)
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - AI-fea-6: the AI never fails a run
        logger.warning("fea ai: no decision (%s) - the rules' own plan - job_id=%s", type(exc).__name__, job_id)
        return section(None, [], status="failed", key=key,
                       note=f"The AI's choices were not used ({type(exc).__name__}); this is the rules' own mesh.")
    _account(run, rr)
    if getattr(rr, "failure_marker", ""):
        return section(None, [], status="failed", key=key,
                       note=f"The AI's choices were not used ({rr.failure_marker}); this is the rules' own mesh.")
    args = _args_of(rr, TOOL_DECISION)
    if args is None:
        return section(None, [], status="invalid", key=key,
                       note="The AI's choices were not used (it gave no decision); this is the rules' own mesh.")
    decision, problems = validate(args, b)
    if decision is None:
        return section(None, [], status="invalid", key=key, problems=problems,
                       note=f"The AI's choices were not used ({problems[0]}); this is the rules' own mesh.")
    return section(decision, [], status="used", key=key, problems=problems)


async def choose_lever(options: list[dict], facts: Mapping, *, ai: Mapping | None, state: Mapping,
                       job_id: str = "", run=None, wait_s: float = TIMEOUT_S) -> tuple[dict | None, dict]:
    """AI-fea-8: (the lever to pull, the record of the choice) - the rules' first option when there is
    one choice only, the AI is not in use, or its answer is not one of the options."""
    if not options:
        return None, {}
    default = options[0]
    if len(options) == 1 or not ai or ai.get("status") != "used" or wait_s < MIN_WAIT_S:
        return default, {}
    ids = [o["lever"] for o in options]
    shown = {"failure": {k: facts.get(k) for k in ("pass", "built", "valid", "min_sicn", "cells", "bad_surfaces", "rc")},
             "options": [{"id": o["lever"], "what": o.get("words")} for o in options],
             "analysis": (ai.get("decision") or {}).get("analysis")}
    msgs = [{"role": "system", "content": LEVER_SYSTEM},
            {"role": "user", "content": json.dumps(shown, sort_keys=True, separators=(",", ":"), default=str)}]
    tool = lever_tool(ids)
    try:
        rr = await _ask(msgs, tool, job_id=job_id, user_id=str((state or {}).get("user_id") or ""), wait_s=wait_s)
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - AI-fea-6
        return default, {"status": "failed", "note": type(exc).__name__, "picked": default["lever"]}
    _account(run, rr)
    try:
        raw = json.loads(_args_of(rr, TOOL_LEVER) or "")
        pick = str(raw.get("lever") or "")
        reason = str(raw.get("reason") or "")[:300]
    except (ValueError, AttributeError):
        pick, reason = "", ""
    chosen = next((o for o in options if o["lever"] == pick), None)
    if chosen is None:
        return default, {"status": "invalid", "note": f"{pick!r} is not an option", "picked": default["lever"]}
    return chosen, {"status": "used", "picked": pick, "reason": reason, "options": ids}


__all__ = ["ANALYSES", "FEA_AI", "KEEP_MIN", "REFINE_FLOOR", "SET_ASIDE_MAX", "apply_zones", "brief",
           "budget_limit", "refines", "without_refines", "choose_lever", "decide", "decision_tool", "decisions", "lever_tool", "messages_for",
           "section", "validate", "words"]
