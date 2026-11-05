# Responsibility: Decide, for every pair of parts of an FEA assembly that meet, how that one pair is joined - Bonded, Contact or Off (PLAN-fea-19) - from the measured meeting and the stage's whole-assembly answer, with the user's own choice for any one pair over it; and, where two parts pass into each other, which part keeps the shared piece (PLAN-fea-20: the smaller body keeps its full shape). Each decision carries its reason, in plain words.
# Boundaries: pure over plain data (the interfaces cad/fea_assembly records, the parts' volumes, the stage's answers); no geometry kernel, no persistence, no model call. It meshes nothing and stages nothing: cad/fea_assembly.stage applies the choices, application/fea_check shows them, engines/fea_driver cites them.
# Collaborates with: cad/fea_assembly.py (stage() reads choose()), application/fea_check.py (the stage's per-pair rows and their defaults), api/v1/geometry.py (cleans what the stage sent), engines/fea_driver.py (cites decisions()), engines/fea_assembly_deck.py (writes each pair as its type).
"""EACH PAIR'S JOINT, AND WHO KEEPS AN OVERLAP.

A pair is two parts that meet (one interface of cad/fea_assembly's record: touching, across a small
gap, or passing into each other). Its key is the two part ids in number order, "Part_1|Part_3":
the same whatever order the interfaces are numbered in, so a choice made on the stage finds its
pair again when the run stages the assembly.

THE TYPE (PLAN-fea-19). Bonded (welded or glued: tied, or one shared mesh), Contact (a contact pair:
the parts can slide or separate) or Off (not joined: the parts are left unconnected there). By
default a pair takes the stage's whole-assembly answer; a pair across a small gap takes it too
while the small-gap switch is on, and is Off when it is switched off. A choice the user made for
that one pair stands over the default.

THE OWNER (PLAN-fea-20). Where two parts pass into each other and are meshed as one piece of
material there, the SMALLER body (by volume) keeps its full shape and the larger gives way - the
rule SpaceClaim's interference tool publishes (it cuts the overlap out of the larger body), so a
pin keeps its whole section inside the bracket it is pressed into. Two parts the same size (within
SAME_SIZE: a body drawn twice) leave it with the lower-numbered one. The user can name the other.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

BONDED, CONTACT, OFF = "bonded", "contact", "off"
TYPES = (BONDED, CONTACT, OFF)
TYPE_LABELS = {BONDED: "Bonded", CONTACT: "Contact", OFF: "Off"}
TYPE_WORDS = {BONDED: "welded or glued: tied, or one mesh where they meet",
              CONTACT: "a contact pair: they can slide or separate",
              OFF: "not joined: the mesh leaves them unconnected there"}
#: The interface kinds cad/fea_assembly records.
TOUCHING, GAP, OVERLAP = "touching", "gap", "overlap"
#: Two parts whose volumes differ by no more than this share are the same size (a body drawn twice).
SAME_SIZE = 1e-3
#: A reach the user may set for one pair: more than nothing, and at most this (millimetres).
MAX_REACH_MM = 1000.0
#: The rules.
TYPE_RULE = "PLAN-fea-19"
OWNER_RULE = "PLAN-fea-20"
#: The open bonded pair (engines/fea_assembly_deck.open_joints): said, never a clean pass.
OPEN_RULE = "PLAN-fea-21"
#: The why record cites each pair one by one up to this many; the rest in one summary entry.
CITE_EACH = 200


def part_no(pid: Any) -> int:
    m = re.match(r"^Part_(\d+)$", str(pid))
    return int(m.group(1)) if m else 0


def pair_key(parts: Iterable[Any]) -> str:
    """The pair's key: its two part ids in number order, "Part_1|Part_3"."""
    return "|".join(sorted((str(p) for p in parts), key=part_no))


def _mm(v: Any) -> str:
    try:
        g = float(v)
    except (TypeError, ValueError):
        return "a small gap"
    return f"{g:.2g} mm" if g < 1 else f"{g:.3g} mm"


def default_type(kind: str, connection: str, gap_joins: bool = True, gap_mm: Any = None) -> tuple[str, str]:
    """(the pair's type by rule, why) - PLAN-fea-19: the whole-assembly answer; a small-gap pair
    takes it while the small-gap switch is on and is Off when it is off."""
    conn = CONTACT if str(connection) == CONTACT else BONDED
    said = TYPE_LABELS[conn]
    if kind == GAP:
        if not gap_joins:
            return OFF, f"they face each other across {_mm(gap_mm)}, and joining across small gaps is switched off"
        return conn, (f"they face each other across {_mm(gap_mm)}, inside the small-gap limit: "
                      f"{said}, the answer for the whole assembly")
    if kind == OVERLAP:
        return conn, f"they pass into each other: {said}, the answer for the whole assembly"
    return conn, f"they touch: {said}, the answer for the whole assembly"


def default_keep(parts: list[str], volume_of: Mapping[str, float]) -> tuple[str, str]:
    """(the part that keeps the shared piece, why) - PLAN-fea-20: the smaller body by volume; the
    lower-numbered when the two are the same size."""
    a, b = sorted((str(p) for p in parts), key=part_no)[:2]
    va, vb = float(volume_of.get(a) or 0.0), float(volume_of.get(b) or 0.0)
    if abs(va - vb) <= SAME_SIZE * max(va, vb, 1e-300):
        return a, (f"{a} and {b} are the same size (drawn twice?): the shared piece stays with {a}, "
                   "the lower-numbered")
    small, big = (a, b) if va < vb else (b, a)
    return small, (f"{small} is the smaller body: it keeps its full shape and {big} gives way there "
                   "(SpaceClaim's interference rule)")


def _reach(v: Any) -> float | None:
    try:
        r = float(v)
    except (TypeError, ValueError):
        return None
    return r if math.isfinite(r) and 0.0 < r <= MAX_REACH_MM else None


def clean_overrides(raw: Any, keys: Iterable[str] | None = None) -> dict[str, dict]:
    """What the stage sent for single pairs, checked: {pair key: {"type"?, "keep"?, "reach_mm"?}}.
    Takes a mapping by key or a list of rows ({"key"} or {"parts"}); anything it cannot read is
    left out, never a refusal. `keys`: the pairs the assembly has (others are dropped)."""
    rows: list[tuple[str, Mapping]] = []
    if isinstance(raw, Mapping):
        rows = [(str(k), v) for k, v in raw.items() if isinstance(v, Mapping)]
    elif isinstance(raw, list):
        for r in raw:
            if isinstance(r, Mapping):
                k = str(r.get("key") or "") or (pair_key(r["parts"]) if isinstance(r.get("parts"), list)
                                                and len(r["parts"]) == 2 else "")
                if k:
                    rows.append((k, r))
    allowed = set(keys) if keys is not None else None
    out: dict[str, dict] = {}
    for k, r in rows:
        parts = k.split("|")
        if len(parts) != 2 or not all(part_no(p) for p in parts):
            continue
        k = pair_key(parts)
        if allowed is not None and k not in allowed:
            continue
        row: dict[str, Any] = {}
        t = str(r.get("type") or "").strip().lower()
        if t in TYPES:
            row["type"] = t
        keep = str(r.get("keep") or "").strip()
        if keep in parts:
            row["keep"] = keep
        reach = _reach(r.get("reach_mm"))
        if reach is not None:
            row["reach_mm"] = reach
        if row:
            out[k] = row
    return out


def choose(interfaces: Sequence[Mapping], *, connection: str, gap_joins: bool = True,
           volume_of: Mapping[str, float] | None = None, overrides: Mapping[str, Mapping] | None = None) -> list[dict]:
    """Every interface's joint, in the interfaces' order:

        {"id", "key", "parts", "kind", "type", "default", "source" ("rule" | "user"), "why",
         "keep", "keep_default", "keep_source", "keep_why"   (an overlap only),
         "reach_mm"                                           (the user's reach for this pair, if any)}
    """
    vols = dict(volume_of or {})
    said = clean_overrides(overrides or {})
    out = []
    for itf in interfaces:
        parts = [str(p) for p in itf.get("parts") or []][:2]
        if len(parts) != 2:
            continue
        key = pair_key(parts)
        kind = str(itf.get("kind") or TOUCHING)
        default, why = default_type(kind, connection, gap_joins, itf.get("gap_mm"))
        mine = said.get(key) or {}
        t = mine.get("type") or default
        row: dict[str, Any] = {"id": itf.get("id"), "key": key, "parts": sorted(parts, key=part_no), "kind": kind,
                               "type": t, "default": default, "source": "user" if t != default else "rule",
                               "why": why if t == default else f"set to {TYPE_LABELS[t]} on the stage, for this pair"}
        if kind == OVERLAP:
            keep, kwhy = default_keep(parts, vols)
            got = mine.get("keep") or keep
            row.update(keep=got, keep_default=keep, keep_source="user" if got != keep else "rule",
                       keep_why=kwhy if got == keep else f"{got} keeps its full shape there, as set on the stage")
        if mine.get("reach_mm") and t != OFF:
            row["reach_mm"] = float(mine["reach_mm"])
        out.append(row)
    return out


def changes_only(overrides: Mapping[str, Mapping], defaults: Sequence[Mapping]) -> dict[str, dict]:
    """The overrides that differ from the rule's choice (`defaults`: choose() with no overrides):
    a pair the user left as the rule had it follows the rule when the run stages it."""
    by_key = {str(d["key"]): d for d in defaults}
    out: dict[str, dict] = {}
    for k, r in clean_overrides(overrides, by_key).items():
        d = by_key[k]
        row = {}
        if r.get("type") and r["type"] != d["type"]:
            row["type"] = r["type"]
        if r.get("keep") and d.get("kind") == OVERLAP and r["keep"] != d.get("keep"):
            row["keep"] = r["keep"]
        if r.get("reach_mm") and (r.get("type") or d["type"]) != OFF:
            row["reach_mm"] = r["reach_mm"]
        if row:
            out[k] = row
    return out


def pair_words(choice: Mapping, label: Mapping[str, str] | None = None) -> str:
    """One pair as the card says it: "Bolt (Part_6) and Boss (Part_2): Bonded - they face each other
    across 0.1 mm ...", and for an overlap who keeps it."""
    lab = dict(label or {})
    a, b = choice["parts"]
    who = f"{lab.get(a) or a} and {lab.get(b) or b}"
    s = f"{who}: {TYPE_LABELS.get(str(choice.get('type')), str(choice.get('type')))} - {choice.get('why')}"
    if choice.get("kind") == OVERLAP and choice.get("keep") and choice.get("type") != OFF:
        s += f". {choice.get('keep_why')}"
    return s


def decisions(choices: Sequence[Mapping], *, connection: str, gap_joins: bool) -> list[dict]:
    """The why-record entries (contracts/decision_record.cite shape): each pair's type (PLAN-fea-19)
    and each overlap's owner (PLAN-fea-20), with the reason; past CITE_EACH pairs, the rest in one."""
    out: list[dict] = []
    for c in choices[:CITE_EACH]:
        out.append({"stage": "fea_plan", "decided": f"joint {c['key']}", "value": c["type"], "rule": TYPE_RULE,
                    "source": c["source"], "alternatives": list(TYPES),
                    "inputs": {"kind": c["kind"], "default": c["default"], "connection": connection,
                               "gap_joins": bool(gap_joins), **({"reach_mm": c["reach_mm"]} if c.get("reach_mm") else {})},
                    "words": pair_words(c)})
        if c.get("kind") == OVERLAP and c.get("keep"):
            out.append({"stage": "fea_plan", "decided": f"overlap {c['key']}", "value": c["keep"], "rule": OWNER_RULE,
                        "source": c["keep_source"], "alternatives": list(c["parts"]),
                        "inputs": {"default": c["keep_default"], "type": c["type"]}, "words": str(c["keep_why"])})
    rest = choices[CITE_EACH:]
    if rest:
        n = {t: sum(1 for c in rest if c["type"] == t) for t in TYPES}
        out.append({"stage": "fea_plan", "decided": "joints (the rest)", "value": len(rest), "rule": TYPE_RULE,
                    "source": "user" if any(c["source"] == "user" for c in rest) else "rule",
                    "inputs": {**n, "connection": connection, "gap_joins": bool(gap_joins)},
                    "words": f"{len(rest)} more pair(s): {n[BONDED]} bonded, {n[CONTACT]} contact, {n[OFF]} off."})
    return out


#: PLAN-fea-21: the way on from an open bonded pair, said with it.
OPEN_WAY_OUT = ("switch this pair to Contact or Off on the stage, or set its reach to at least {needed} so the "
                "solver ties those nodes")
#: PLAN-fea-21 for two parts that PASS INTO each other and are tied apart: a tie joins them only where their
#: surfaces cross, and no reach mends that (the faces inside the other part lie deep in its material).
OPEN_OVERLAP_WAY_OUT = ("they pass into each other, and meshed apart a tie joins them only where their surfaces "
                        "cross - remove the overlap in the CAD, or switch this pair to Contact or Off on the stage; "
                        "with every pair that touches left Bonded the two are meshed as one piece instead")

#: PLAN-fea-21 for a tie whose nodes all lie BESIDE the other part's surface (settle_ties ties a node only where
#: it lies over a face): no reach mends that.
OPEN_BESIDE_WAY_OUT = ("its nearest nodes lie beside the other part's faces, not over them, and no reach ties a node "
                       "beside a face - switch this pair to Contact or Off on the stage, or mend where the two meet "
                       "in the CAD")


def open_words(row: Mapping) -> str:
    """One OPEN bonded pair (engines/fea_assembly_deck.open_joints) in plain words, with the way on:
    "Tie3 (Part_2 and Part_5): Part_5_Face_7 has no mesh node within the 0.25 mm reach (nearest
    0.41 mm) - switch this pair to Contact or Off on the stage, or set its reach to at least 0.42 mm"."""
    parts = list(row.get("parts") or ["?", "?"])
    reach_mm = 1000.0 * float(row.get("reach_m") or 0.0)
    need_mm = 1000.0 * float(row.get("needed_m") or 0.0)
    faces = [f for f in row.get("faces") or [] if isinstance(f, Mapping)]
    if faces:
        named = ", ".join(f"{f.get('face')} (nearest {_mm(1000.0 * float(f.get('nearest_m') or 0.0))})"
                          for f in faces[:4]) + (f" and {len(faces) - 4} more" if len(faces) > 4 else "")
        what = f"{named} {'has' if len(faces) == 1 else 'have'} no mesh node within the {_mm(reach_mm)} reach"
    elif row.get("beside"):
        what = f"no node of it lies over the other part's surface within the {_mm(reach_mm)} reach"
    else:
        what = f"no node of it lies within the {_mm(reach_mm)} reach"
    src = {"solver default": " (the solvers' default)", "set": " (set on the stage)"}.get(str(row.get("reach_from")), "")
    if row.get("overlap"):
        return f"{row.get('name')} ({parts[0]} and {parts[1]}): {what}{src} - " + OPEN_OVERLAP_WAY_OUT
    if row.get("beside"):
        return f"{row.get('name')} ({parts[0]} and {parts[1]}): {what}{src} - " + OPEN_BESIDE_WAY_OUT
    # a little past the farthest node, so every one is inside it
    return (f"{row.get('name')} ({parts[0]} and {parts[1]}): {what}{src} - "
            + OPEN_WAY_OUT.format(needed=_mm(need_mm * 1.05 + 1e-6)))


def counts(choices: Sequence[Mapping]) -> dict:
    """How many pairs of each type, and how many the user set (its type, its owner or its reach)."""
    return {**{t: sum(1 for c in choices if c.get("type") == t) for t in TYPES},
            "edited": sum(1 for c in choices if c.get("source") == "user" or c.get("keep_source") == "user"
                          or c.get("reach_mm"))}


__all__ = ["BONDED", "CITE_EACH", "CONTACT", "MAX_REACH_MM", "OFF", "OPEN_BESIDE_WAY_OUT", "OPEN_OVERLAP_WAY_OUT", "OPEN_RULE", "OPEN_WAY_OUT", "OWNER_RULE", "SAME_SIZE",
           "TYPES", "TYPE_LABELS", "TYPE_RULE", "TYPE_WORDS", "changes_only", "choose", "clean_overrides", "counts",
           "decisions", "default_keep", "default_type", "open_words", "pair_key", "pair_words", "part_no"]
