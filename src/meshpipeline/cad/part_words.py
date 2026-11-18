# Responsibility: Tie the phrases of the user's notes that are about PARTS ("3-4 mm for the frame rail", "refine near the bolts", "the inlet duct is the long tube") to the real parts of the model, for every route: the vision model is shown the parts as measured (a facts table) and as drawn (pictures, each part its own colour and number) and answers with part ids, what the phrase asks (a size, finer, coarser, a name) and the user's own numbers; the rules check every entry and drop what does not hold.
# Boundaries: the model only ever picks part ids from the table and repeats numbers from a quote of the user's words - it invents no size. A model that is off, slow, unsure, wrong or down never fails a run: nothing is applied for that phrase, and it is said. What a route does with the mapping is the route's (the FEA sizes: engines/fea_plan PLAN-fea-29).
# Collaborates with: engines/fea_part_sizes.py (the FEA route), render/part_pictures.py (the pictures), contracts/model_inference (the reviewer route - the vision model the geometry check asks; contracts/decision_cache keys and replays it), contracts/user_notes (the notes, word for word).
"""THE USER'S WORDS TIED TO PARTS (NOTE-4), for any route.

The model answers with ONE `part_words` call:

    {"phrases": [{"parts": ["Part_9"], "everything_else": false, "ask": "size", "min": 3, "max": 4, "unit": "mm",
                  "quote": "3 - 4 mm for the frame rail", "why": "the only long hollow beam", "confidence": 0.9}],
     "words": ["..."]}

`ask`: "size" (an element size: min and max in the user's own numbers), "refine" (finer there), "coarsen"
(coarser there) or "name" (which part is which); only "size" carries numbers. The rules check each entry
(`check`): the quote is the user's own words (found in the notes), a size's numbers are in the quote, min <= max,
both positive, the unit one of mm / cm / m / in (converted to mm), every id in the table, the size sane for each
part (no finer than PART_FINEST of the part, no coarser than the part itself), the model sure enough
(MIN_CONFIDENCE: "unsure" is allowed and applies nothing), no part given two sizes (the first keeps it). What fails
is dropped with a plain note; the rest stands. No answer and none saved: nothing is applied, and said.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from meshpipeline.contracts.event_stream import StaleExecutionPublish

logger = logging.getLogger(__name__)

RULE = "NOTE-4"
TOOL = "part_words"
ASKS = ("size", "refine", "coarsen", "name")
#: where the pictures are kept in the workspace, and the record beside them
PICTURES_DIR = "part_pictures"
#: how long the model may take before nothing is applied (the same as the other FEA decision)
TIMEOUT_S = 180.0
MIN_WAIT_S = 20.0
#: the most parts in the table (an assembly past it: the largest by volume, the rest said)
MAX_PARTS = 120
#: an entry the model is less sure of than this is dropped (applied nothing) with a note
MIN_CONFIDENCE = 0.6
#: a size finer than the part's diagonal over this is not a size anyone means for that part (0.001 mm on a bolt)
PART_FINEST = 2000.0
UNITS_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
#: alike parts: the same size and volume within this share
ALIKE_SHARE = 0.02


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _g(v: float) -> float:
    return float(f"{float(v):.4g}")


# #
# what the parts are, measured
# #

def _part_of(name: str) -> str:
    """Part_k of an assembly's Part_k_Face_j; "Part_1" for a single part's Face_j."""
    head, sep, _tail = str(name).rpartition("_Face_")
    return head if sep else "Part_1"


def part_table(record: Mapping, surface) -> list[dict]:
    """One row per part, the facts the model ties words to: its CAD name, its box (longest first), volume,
    area, how full its box is (a hollow beam or a thin plate is far under 1), its walls, the bores and
    shafts it has (diameters of its round faces), how many alike, and which parts it touches."""
    import numpy as np

    names = list(getattr(surface, "names", []) or [])
    label = np.asarray(getattr(surface, "label", []), dtype=np.int64)
    tris = np.asarray(getattr(surface, "tris", []), dtype=np.int64).reshape(-1, 3)
    xyz = np.asarray(getattr(surface, "xyz", []), dtype=float).reshape(-1, 3)
    part_of_label = np.array([_part_of(n) for n in names] + ["?"], dtype=object)
    lab = np.where((label >= 0) & (label < len(names)), label, len(names))
    tri_part = part_of_label[lab] if len(tris) else np.zeros(0, dtype=object)
    bases = {str(b.get("id")): b for b in record.get("part_bases") or []}
    asm = record.get("assembly_plan") or {}
    touches: dict[str, set[str]] = {}
    for p in asm.get("pair_list") or []:
        ps = [str(x) for x in (p.get("parts") or []) if x]
        for a in ps:
            touches.setdefault(a, set()).update(x for x in ps if x != a)
    faces = record.get("faces") or {}
    ids = sorted({str(x) for x in tri_part if x != "?"} | set(bases), key=_part_order)
    rows: list[dict] = []
    for pid in ids:
        sel = np.flatnonzero(tri_part == pid) if len(tris) else np.zeros(0, dtype=np.int64)
        row: dict = {"id": pid, "name": str((bases.get(pid) or {}).get("name") or "")}
        if len(sel):
            t = tris[sel]
            pts = xyz[np.unique(t)]
            ext = sorted((pts.max(axis=0) - pts.min(axis=0)).tolist(), reverse=True)
            a, b, c = xyz[t[:, 0]], xyz[t[:, 1]], xyz[t[:, 2]]
            vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)
            area = float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
            box = max(ext[0] * ext[1] * ext[2], 1e-12)
            row.update(size_mm=[_g(e) for e in ext], diag_mm=_g(float(np.linalg.norm(ext))),
                       volume_mm3=_g(vol), area_mm2=_g(area), box_fill=round(min(1.0, vol / box), 3))
        own = {n: r or {} for n, r in faces.items() if _part_of(n) == pid}
        walls = sorted(float(r.get("thickness_wall_mm") or r.get("thickness_median_mm") or 0.0) for r in own.values())
        walls = [w for w in walls if w > 0]
        if walls:
            mins = [float(r["thickness_min_mm"]) for r in own.values() if _f(r.get("thickness_min_mm"))]
            row["thinnest_wall_mm"] = _g(min(mins) if mins else walls[0])
            row["typical_wall_mm"] = _g(walls[len(walls) // 2])
        bores = sorted({_g(2 * float(r["radius_mm"])) for r in own.values()
                        if r.get("kind") == "cylinder" and r.get("concave") and _f(r.get("radius_mm"))})
        shafts = sorted({_g(2 * float(r["radius_mm"])) for r in own.values()
                         if r.get("kind") == "cylinder" and not r.get("concave") and _f(r.get("radius_mm"))})
        kinds: dict[str, int] = {}
        for r in own.values():
            kinds[str(r.get("kind") or "other")] = kinds.get(str(r.get("kind") or "other"), 0) + 1
        row.update(faces=len(own), face_kinds=dict(sorted(kinds.items())))
        if bores:
            row["bore_diameters_mm"] = bores[:8]
        if shafts:
            row["shaft_diameters_mm"] = shafts[:8]
        if pid in touches:
            row["touches"] = sorted(touches[pid], key=_part_order)
        rows.append(row)
    for row in rows:
        row["alike"] = sum(1 for o in rows if _alike(row, o))
    if len(rows) > MAX_PARTS:
        rows = sorted(rows, key=lambda r: -float(r.get("volume_mm3") or 0.0))[:MAX_PARTS]
        rows.sort(key=lambda r: _part_order(r["id"]))
    return rows


def check_record(record: Mapping) -> dict:
    """The geometry check's FEA record (cad/fea_measure, an assembly's under fea_assembly.FACT_KEY) in the shape
    part_table reads off the driver's: each part's id and CAD name, and which parts touch."""
    asm = record.get("assembly") or {}
    return {"faces": record.get("faces") or {}, "hot_spots": record.get("hot_spots") or [], "bores": record.get("bores") or [],
            "part_bases": [{"id": p.get("id"), "name": p.get("name") or ""} for p in asm.get("parts") or []
                           if p.get("id")],
            "assembly_plan": {"pair_list": [{"parts": list(i.get("parts") or [])}
                                            for i in asm.get("interfaces") or []]}}


def _alike(a: Mapping, b: Mapping) -> bool:
    va, vb = _f(a.get("volume_mm3")), _f(b.get("volume_mm3"))
    sa, sb = a.get("size_mm") or [], b.get("size_mm") or []
    if not va or not vb or len(sa) != 3 or len(sb) != 3:
        return a is b
    def near(x: float, y: float) -> bool:
        return abs(x - y) <= ALIKE_SHARE * max(abs(x), abs(y), 1e-9)

    return near(va, vb) and all(near(x, y) for x, y in zip(sa, sb, strict=True))


def _part_order(pid: str) -> int:
    try:
        return int(str(pid).rsplit("_", 1)[-1])
    except ValueError:
        return 0


# #
# what the model is shown
# #

SYSTEM = """You tie an engineer's words about PARTS of the model they are meshing to the parts themselves.
You are given their notes word for word, a table of the parts as measured (id, CAD name, box size longest first,
volume, how full its box is - a hollow beam or a thin plate is far under 1 -, walls, bore and shaft diameters, how
many parts are alike, which parts each touches), and pictures: the whole model with each part in its own colour and
its id number on it, and each part alone with its id.

Call part_words once. For each phrase in the notes about particular parts - an element SIZE there (a length per
element: "an element size of around 3-4 mm for X", "2 mm elements on the bolts", "mesh everything else at 5 mm"):
ask "size"; finer there without a number ("refine near the mounts"): ask "refine"; coarser there: "coarsen";
only which part is which ("the long tube is the inlet duct"): "name". For each:
- parts: the ids of the parts it means, from the table only. Tie the words to parts by what they ARE: the CAD name
  when it says, else the shape - a long hollow or open beam (long box, low box_fill) is a frame member, rail or beam; a
  cylinder with a hex head and a shaft the size of a thread (M8 = 8 mm) is a bolt; a thin flat ring is a washer;
  a short thick tube welded or set into a larger part is a boss; a small plate with holes is a bracket. Count:
  "the bolts" is every alike bolt. If the words mean every part not named elsewhere ("everything else", "the
  rest"), give parts [] and everything_else true.
- min, max: for "size" only, the numbers EXACTLY as the user wrote them, in their unit (unit: mm, cm, m or in); a
  single number is min = max. Never convert, never invent, never round. Any other ask: 0 and 0.
- quote: the user's own words for this phrase, copied exactly from the notes (the numbers in it).
- why: one short plain sentence - which parts you took and what told you ("Part_9 is the only long hollow beam").
- confidence: 0 to 1. Below 0.6 when you cannot tell which parts are meant - say so in why; never guess.
Some rows are AREAS INSIDE A PART (feature: true): a hole or a group of alike holes, a bore, a notch, fillets,
bosses or shafts, thin walls (ribs, flanges), a large flat face - each with a plain name, how many, its size and the
part it is in (in_part); in the pictures each area has its own colour and an F number (F3 is Feature_3), the rest of
the part grey. Words about such an area ("1 mm at the mounting holes", "fine on the fillets", "coarser on the big
flat face") take the area's id; words about a whole part take the part's id; "elsewhere" or "everything else" means
the rest of the parts (everything_else true), never the areas.
NOT sizes: an element COUNT ("500k elements"), loads, pressures, bolt sizes alone (M8), part dimensions,
elements through a wall. If the notes say nothing about particular parts, phrases is an empty list.
words: one or two short plain sentences for the user on how you read their words. No ids-only answers: name the
parts as the user did."""


def size_tool() -> dict:
    return {"type": "function", "function": {
        "name": TOOL,
        "description": "The phrases of the user's notes about particular parts, tied to the parts' ids.",
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["phrases", "words"],
            "properties": {
                "phrases": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["parts", "everything_else", "ask", "min", "max", "unit", "quote", "why",
                                 "confidence"],
                    "properties": {
                        "parts": {"type": "array", "items": {"type": "string"}},
                        "everything_else": {"type": "boolean"},
                        "ask": {"type": "string", "enum": list(ASKS)},
                        "min": {"type": "number"}, "max": {"type": "number"},
                        "unit": {"type": "string", "enum": list(UNITS_MM)},
                        "quote": {"type": "string"}, "why": {"type": "string"},
                        "confidence": {"type": "number"}}}},
                "words": {"type": "array", "items": {"type": "string"}}}}}}


def squash(text: str) -> str:
    return " ".join(re.sub(r"[^\w.\-–]+", " ", str(text or "").casefold()).split()).strip(" .")


def sentences(text: str) -> list[str]:
    """The sentences (and lines) of a text, as written."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()]


def notes_text(state: Mapping) -> str:
    """THE USER'S OWN WORDS, and only those: what they typed in the notes box and their own chat messages as kept
    (contracts/user_notes.own_notes), word for word. Never the intake's summary of the request (request_txt, a
    model's rewrite): read beside the notes, its sentences were quoted back to the user as theirs - on the card's
    checklist (NOTE-5) and as "not applied" lines."""
    from meshpipeline.contracts.user_notes import own_notes, render
    return render(own_notes((state or {}).get("user_notes") or []))


def wanted(state: Mapping) -> bool:
    """Asked only when the user left words to read (a run without notes asks nothing)."""
    return bool(notes_text(state).strip())


def messages_for(state: Mapping, table: list[dict], pictures: list[Path], *, consumer: str,
                 purpose: str = "") -> list[dict]:
    from meshpipeline.contracts.user_notes import own_notes, prompt_block
    s = state or {}
    text = {"mesh_for": purpose,
            "user_notes": prompt_block(own_notes(s.get("user_notes") or []), consumer=consumer),
            "parts": table,
            "pictures": [p.stem for p in pictures]}
    content: list[dict] = [{"type": "text", "text": json.dumps(text, sort_keys=True, separators=(",", ":"))}]
    for p in pictures:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(Path(p).read_bytes()).decode("ascii")}})
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]


# #
# the answer, checked
# #

_DASHES = re.compile(r"[‐-―−]")
_NUM = re.compile(r"\d+(?:[.,]\d+)?|\.\d+")


def _norm(text: str) -> str:
    return " ".join(_DASHES.sub("-", str(text or "")).casefold().split())


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", ".")) for m in _NUM.findall(_DASHES.sub("-", str(text or "")))]


def check(arguments: str, table: list[dict], notes: str,
          unsure: list[dict] | None = None) -> tuple[list[dict], list[str], list[str]]:
    """(the entries that hold, in millimetres; what was dropped, in plain words; the model's words). Never raises.
    `unsure`, when given, is told each phrase dropped only because it was not clear which parts it means (its
    index, quote, ask and sizes): the console offers those for the user to pick the parts."""
    try:
        return _check(arguments, table, notes, unsure)
    except Exception as exc:  # noqa: BLE001 - a malformed answer is the model's, never the run's
        return [], [f"the answer did not fit the schema ({type(exc).__name__}): nothing applied"], []


def _check(arguments: str, table: list[dict], notes: str,
           unsure: list[dict] | None = None) -> tuple[list[dict], list[str], list[str]]:
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return [], ["the answer was not JSON: nothing applied"], []
    if not isinstance(raw, dict) or not isinstance(raw.get("phrases") or raw.get("sizes") or [], list):
        return [], ["the answer was not a list of phrases: nothing applied"], []
    rows = {str(r["id"]): r for r in table}
    said = _norm(notes)
    out: list[dict] = []
    dropped: list[str] = []
    taken: set[str] = set()
    rest_entry: dict | None = None
    for i, e in enumerate(raw.get("phrases") or raw.get("sizes") or []):
        if not isinstance(e, dict):
            dropped.append("an entry that was not an object: dropped")
            continue
        quote = " ".join(str(e.get("quote") or "").split())[:300]
        tag = f'"{quote}"' if quote else "an entry with no quote"
        ask = str(e.get("ask") or "size")
        lo, hi = _f(e.get("min")), _f(e.get("max"))
        unit = str(e.get("unit") or "mm").strip().lower()
        conf = _f(e.get("confidence"))
        why = " ".join(str(e.get("why") or "").split())[:300]
        if not quote or _norm(quote) not in said:
            dropped.append(f"{tag}: not found word for word in your notes, so nothing was applied for it")
            continue
        if ask not in ASKS:
            dropped.append(f"{tag}: {ask!r} is not something the mesh can do with a part, so nothing was applied")
            continue
        if ask != "size":
            lo = hi = 1.0          # no numbers to check; the part ids and the sureness still are
            unit = "mm"
        elif lo is None or hi is None or lo <= 0 or hi <= 0:
            dropped.append(f"{tag}: the sizes were not positive numbers, so nothing was applied for it")
            continue
        if lo > hi:
            dropped.append(f"{tag}: the smaller size is over the larger, so nothing was applied for it")
            continue
        nums = _numbers(quote)
        if ask == "size" and not all(any(abs(v - n) <= 1e-9 * max(1.0, abs(n)) for n in nums) for v in (lo, hi)):
            dropped.append(f"{tag}: the sizes are not the numbers you wrote, so nothing was applied for it")
            continue
        if unit not in UNITS_MM:
            dropped.append(f"{tag}: the unit {unit!r} is not a length unit, so nothing was applied for it")
            continue
        if conf is None or conf < MIN_CONFIDENCE:
            if unsure is not None:
                unsure.append({"phrase": i, "quote": quote, "ask": ask, "why": why,
                               **({"min_mm": _g(lo * UNITS_MM[unit]), "max_mm": _g(hi * UNITS_MM[unit])}
                                  if ask == "size" else {})})
            dropped.append(f"{tag}: it was not clear which parts you meant ({why or 'no reason given'}), so "
                           "nothing was applied for it")
            continue
        k = UNITS_MM[unit]
        lo_mm, hi_mm = lo * k, hi * k
        rest = bool(e.get("everything_else"))
        ids = [str(p) for p in e.get("parts") or [] if str(p)]
        unknown = [p for p in ids if p not in rows]
        if unknown:
            dropped.append(f"{tag}: {', '.join(unknown[:5])} is not a part of this model, so nothing was applied "
                           "for it")
            continue
        if not ids and not rest:
            dropped.append(f"{tag}: it named no part, so nothing was applied for it")
            continue
        entry: dict[str, Any] = {"phrase": i, "parts": [], "everything_else": rest, "ask": ask, "quote": quote, "why": why,
                 "confidence": round(float(conf), 2),
                 **({"min_mm": _g(lo_mm), "max_mm": _g(hi_mm), "unit": unit, "asked": [lo, hi]} if ask == "size" else {})}
        if ask != "size":
            entry["parts"] = ids
            if entry["parts"] or rest:
                out.append(entry)
            continue
        for p in ids:
            d = _f((rows[p] or {}).get("diag_mm"))
            if p in taken:
                dropped.append(f"{tag}: {p} was already sized by an earlier phrase; that one stands")
                continue
            if d and hi_mm > d:
                dropped.append(f"{tag}: {_mm(hi_mm)} is coarser than {p} itself ({_mm(d)} across), so it was not "
                               f"applied to {p}")
                continue
            if d and lo_mm < d / PART_FINEST:
                dropped.append(f"{tag}: {_mm(lo_mm)} is far finer than any mesh of {p} ({_mm(d)} across) could "
                               f"hold, so it was not applied to {p}")
                continue
            entry["parts"].append(p)
            taken.add(p)
        if rest:
            if rest_entry is not None:
                dropped.append(f"{tag}: \"everything else\" was already sized by an earlier phrase; that one stands")
                entry["everything_else"] = False
            else:
                rest_entry = entry
        if entry["parts"] or entry["everything_else"]:
            out.append(entry)
    if rest_entry is not None:
        # "everything else": every part no other phrase sized, each checked against its own size
        for p, row in rows.items():
            # "everything else" is the rest of the PARTS: an area inside a part (NOTE-6) is sized only when named
            if p in taken or row.get("feature"):
                continue
            d = _f(row.get("diag_mm"))
            if d and (rest_entry["max_mm"] > d or rest_entry["min_mm"] < d / PART_FINEST):
                dropped.append(f"\"{rest_entry.get('quote')}\": not applied to {p} ({_mm(d)} across) - "
                               + ("coarser than the part itself" if rest_entry["max_mm"] > d else "far too fine for it"))
                continue
            rest_entry.setdefault("rest_parts", []).append(p)
            taken.add(p)
    words = [" ".join(str(w).split())[:300] for w in raw.get("words") or [] if str(w).strip()][:3]
    return out, dropped, words


def _mm(v: float) -> str:
    return f"{float(v):.3g} mm"


def per_part(entries: list[dict], faces_of: Mapping[str, list[str]] | None = None) -> dict[str, dict]:
    """Part id (or area id, NOTE-6) -> {"min_m", "max_m", "quote"} from the checked entries (metres, the plan's
    unit); an area also carries its "faces" (`faces_of`), where its size holds."""
    out: dict[str, dict] = {}
    faces_of = faces_of or {}
    for e in entries:
        if e.get("ask", "size") != "size":
            continue
        for p in [*e.get("parts", []), *e.get("rest_parts", [])]:
            out.setdefault(p, {"min_m": float(e["min_mm"]) * 1e-3, "max_m": float(e["max_mm"]) * 1e-3,
                               "quote": e.get("quote") or "", "rest": p in e.get("rest_parts", []),
                               **({"faces": list(faces_of[p])} if p in faces_of else {})})
    return out


# #
# asking
# #

UNREAD_WORDS = ("If your notes asked for something on particular parts, it was not applied this run: the AI step "
                "that ties your words to the parts did not answer, so those parts are meshed by the rules.")


def section(status: str, *, entries=None, dropped=None, words=None, table=None, pictures=None, note: str = "",
            key: str = "", source: str = "", phrases=None, unsure=None) -> dict:
    """`phrases`: the model's own answer (kept so a part the user picks on the stage is checked by the same rules);
    `unsure`: the phrases dropped only because it was not clear which parts they mean."""
    return {"status": status, "rule": RULE, "entries": list(entries or []), "dropped": list(dropped or []),
            "words": list(words or []), "parts": list(table or []), "pictures": [str(p) for p in pictures or []],
            "note": note, "key": key, "source": source, "phrases": list(phrases or []), "unsure": list(unsure or [])}


def _phrases_of(arguments: str) -> list[dict]:
    try:
        raw = json.loads(arguments or "")
    except ValueError:
        return []
    got = raw.get("phrases") if isinstance(raw, dict) else None
    return [e for e in got if isinstance(e, dict)] if isinstance(got, list) else []


def picked(ps: Mapping, picks: Mapping[str, list[str]], table: list[dict], notes: str) -> dict:
    """The reading `ps` with the parts the user picked on the stage (`picks`: phrase index -> part ids), checked
    again by the same rules: a picked phrase is the user's own (sure), its quote and numbers still the notes', its
    sizes still sane for each part. A pick of no parts drops the phrase. Pure; never raises."""
    phrases = [dict(e) for e in ps.get("phrases") or []]
    if not phrases or not picks:
        return dict(ps)
    for k, ids in picks.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(phrases):
            # no parts picked: the phrase is back to "which parts?" (unsure), never lost
            phrases[i].update(parts=[str(p) for p in ids or []], everything_else=False,
                              confidence=1.0 if ids else 0.0, why="picked by you" if ids else "no part picked")
    args = json.dumps({"phrases": phrases, "words": ps.get("words") or []})
    unsure: list[dict] = []
    entries, dropped, words = check(args, table, notes, unsure)
    out = dict(ps)
    out.update(entries=entries, dropped=dropped, words=words, phrases=phrases, unsure=unsure, picked=True)
    return out


def from_stored(stored: Mapping | None, table: list[dict]) -> dict | None:
    """A reading made before approval (the stage's checklist, maybe corrected by the user), for this run's own parts
    table: every entry kept only on the parts this run has; None when nothing usable was stored."""
    if not stored or stored.get("status") != "used":
        return None
    # a part is the same part only when its id is, and its name too where both readings have one (the check and the
    # run may number the parts of a file in their own order)
    then = {str(r.get("id")): str(r.get("name") or "") for r in stored.get("parts") or []}
    ids = {str(r["id"]) for r in table
           if not (then.get(str(r["id"])) and r.get("name") and then[str(r["id"])] != str(r["name"]))}
    entries = []
    gone: list[str] = []
    for e in stored.get("entries") or []:
        e = dict(e)
        for f in ("parts", "rest_parts"):
            kept = [p for p in e.get(f) or [] if p in ids]
            gone.extend(p for p in e.get(f) or [] if p not in ids)
            if f in e:
                e[f] = kept
        if e.get("parts") or e.get("rest_parts") or (e.get("everything_else") and e.get("ask") != "size"):
            entries.append(e)
    if stored.get("entries") and not entries:
        return None                 # nothing of it is on the parts this run has: the route reads the notes itself
    dropped = list(stored.get("dropped") or [])
    if gone:
        dropped.append(f"{', '.join(sorted(set(gone))[:5])} is not a part of the model as meshed, so nothing was "
                       "applied there")
    return section("used", entries=entries, dropped=dropped, words=stored.get("words") or [], table=table,
                   pictures=[], key=str(stored.get("key") or ""), source="checked before approval",
                   phrases=stored.get("phrases") or [], unsure=stored.get("unsure") or [])


def _key(messages: list[dict], tool: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps({"m": messages, "t": tool}, sort_keys=True).encode()).hexdigest()[:16]


def _args_of(rr) -> str | None:
    for c in getattr(rr, "tool_calls", ()) or ():
        if getattr(c, "name", "") == TOOL:
            return str(getattr(c, "arguments", "") or "")
    return None


def draw(ws: Path, surface, table: list[dict]) -> list[Path]:
    """The pictures for the model (and kept for the console): [] when they cannot be drawn."""
    try:
        from meshpipeline.render.part_pictures import render_parts
        return render_parts(surface, [r["id"] for r in table], Path(ws) / PICTURES_DIR)
    except Exception as exc:  # noqa: BLE001 - fewer pictures, never a failed run
        logger.warning("part words: the pictures were not drawn (%s)", exc)
        return []


def table_of(record: Mapping, surface) -> tuple[list[dict], dict[str, list[str]], Any]:
    """The rows the words are tied to - the parts (part_table) and, where the record names its faces, the areas
    inside them (NOTE-6, cad/part_features: holes, fillets, bosses, thin walls, large flat faces) - their faces, and
    the surface the pictures are drawn on (each area its own colour; on one part, the rest of it grey)."""
    table = part_table(record, surface)
    faces_of: dict[str, list[str]] = {}
    shown = surface
    try:
        from meshpipeline.cad import part_features as PF
        feats, faces_of = PF.features(record, surface)
        if feats:
            table = [*table, *feats]
            shown = PF.labelled(surface, faces_of, parts=True)
    except Exception as exc:  # noqa: BLE001 - the parts alone
        logger.warning("part words: the areas inside the parts were not read (%s)", type(exc).__name__)
    return table, faces_of, shown


async def decide(ws: Path, record: Mapping, surface, state: Mapping, *, consumer: str, purpose: str = "",
                 job_id: str = "", run=None, enabled: bool = True, wait_s: float = TIMEOUT_S) -> dict:
    """NOTE-4's section for this run: "none" (no words to read, or the AI off), "used" (the checked entries,
    maybe none), or why nothing was applied ("failed", "invalid", "no_time"). `record`: the measurement record
    (its face rows and assembly, when the route has them; {} reads the surface alone). `consumer`: the route's
    user-notes consumer. Never raises (but a fenced worker's stale publish)."""
    if not enabled or not wanted(state):
        return section("none")
    table, faces_of, shown = table_of(record, surface)
    if not table:
        return section("none", note="no parts could be measured")
    # THE READING MADE BEFORE APPROVAL (the stage's checklist, as the user left it) is the one used: the user saw
    # it and approved it. The model is asked again only when there is none.
    stored = from_stored((state or {}).get("part_matches"), table)
    if stored is not None:
        stored["features"] = dict(faces_of)
        return stored
    pictures = await asyncio.to_thread(draw, ws, shown, table)
    got = await ask(ws, table, pictures, state, consumer=consumer, purpose=purpose, job_id=job_id, run=run,
                    wait_s=wait_s)
    got["features"] = dict(faces_of)
    return got


async def ask(ws: Path | None, table: list[dict], pictures: list[Path], state: Mapping, *, consumer: str,
              purpose: str = "", job_id: str = "", run=None, wait_s: float = TIMEOUT_S) -> dict:
    """The model asked on a parts table and its pictures, already made (decide's second half; the geometry check's
    step before approval calls it on what it measured). Never raises (but a fenced worker's stale publish)."""
    msgs = messages_for(state, table, pictures, consumer=consumer, purpose=purpose)
    tool = size_tool()
    key = _key(msgs, tool)
    notes = notes_text(state)
    user_id = str((state or {}).get("user_id") or "")
    rel = [p.relative_to(ws) if ws is not None and Path(p).is_relative_to(ws) else p for p in pictures]

    def answered(rr, source: str) -> dict | None:
        args = _args_of(rr) if rr is not None else None
        if args is None:
            return None
        unsure: list[dict] = []
        entries, dropped, words = check(args, table, notes, unsure)
        return section("used", entries=entries, dropped=dropped, words=words, table=table, pictures=rel, key=key,
                       source=source, phrases=_phrases_of(args), unsure=unsure)

    def without(status: str, why: str) -> dict:
        from meshpipeline.contracts.decision_cache import recorded_answer
        saved = answered(recorded_answer("reviewer", messages=msgs, tools=[tool], tool_choice="auto", job_id=job_id,
                                         user_id=user_id), "saved")
        if saved is not None:
            saved["note"] = f"The AI was not available just now ({why}); its saved reading of these same words was used."
            return saved
        logger.warning("part words: no answer (%s) and none saved - nothing applied - job_id=%s", why, job_id)
        return section(status, table=table, pictures=rel, key=key, source="rules (nothing saved)",
                       note=UNREAD_WORDS)

    if wait_s < MIN_WAIT_S:
        return without("no_time", "no time to spare")
    from meshpipeline.contracts import execution_guard as _fence
    from meshpipeline.contracts import model_inference as llm_router

    await _fence.assert_still_owner("part words")
    try:
        rr = await asyncio.wait_for(llm_router.call_reviewer_with_tools(msgs, [tool], job_id=job_id, user_id=user_id),
                                    timeout=min(TIMEOUT_S, float(wait_s)))
    except StaleExecutionPublish:
        raise
    except Exception as exc:  # noqa: BLE001 - AI-fea-6: the AI never fails a run
        return without("failed", type(exc).__name__)
    note = getattr(run, "note_plan_round", None)
    if callable(note):
        note(rr)
    if getattr(rr, "failure_marker", ""):
        return without("failed", str(rr.failure_marker))
    got = answered(rr, "model")
    return got if got is not None else without("invalid", "it gave no answer")


def words(ps: Mapping | None) -> list[str]:
    """What became of the sizes in the notes, in plain words (the plan's own lines say what each did)."""
    if not ps or ps.get("status") == "none":
        return []
    if ps.get("status") != "used":
        return [str(ps.get("note") or UNREAD_WORDS)]
    out = [str(w) for w in ps.get("words") or []]
    names = {str(r["id"]): (r.get("name") or "") for r in ps.get("parts") or []}
    for e in ps.get("entries") or []:
        ids = [*e.get("parts", []), *e.get("rest_parts", [])]
        shown = ", ".join(f"{p} ({names[p]})" if names.get(p) else p for p in ids[:8])
        if len(ids) > 8:
            shown += f" and {len(ids) - 8} more"
        why = f": {str(e.get('why')).rstrip('.')}" if e.get("why") else ""
        what = {"size": "", "refine": " (finer there)", "coarsen": " (coarser there)", "name": ""}.get(str(e.get("ask")), "")
        out.append(f"\"{e.get('quote')}\" was read as {shown}{what}{why}.")
    if ps.get("source") == "saved" and ps.get("note"):
        out.append(str(ps["note"]))
    out.extend(f"Not applied: {d}." for d in ps.get("dropped") or [])
    return out


def decisions(ps: Mapping | None, stage: str = "notes") -> list[dict]:
    """The why-record entries (source ai-lever)."""
    if not ps or ps.get("status") == "none":
        return []
    from meshpipeline.contracts.decision_record import SOURCE_AI_LEVER
    if ps.get("status") != "used":
        return [{"stage": stage, "decided": "notes about parts not read", "value": ps.get("status"), "rule": RULE,
                 "inputs": {"note": ps.get("note"), "saved": ps.get("source") or ""}, "words": UNREAD_WORDS}]
    return [{"stage": stage, "decided": "notes tied to parts", "value": len(ps.get("entries") or []), "rule": RULE,
             "source": SOURCE_AI_LEVER,
             "inputs": {"entries": [{k: e.get(k) for k in ("parts", "rest_parts", "ask", "min_mm", "max_mm", "quote", "why",
                                                           "confidence")} for e in ps.get("entries") or []],
                        "dropped": ps.get("dropped") or [], "key": ps.get("key"), "from": ps.get("source")},
             "words": " ".join(words(ps)[:3])}]


__all__ = ["ASKS", "MIN_CONFIDENCE", "PART_FINEST", "PICTURES_DIR", "RULE", "SYSTEM", "TOOL", "UNREAD_WORDS", "UNITS_MM",
           "ask", "check", "check_record", "decide", "decisions", "draw", "from_stored", "messages_for", "notes_text",
           "part_table", "per_part", "picked", "section", "size_tool", "wanted", "words"]
