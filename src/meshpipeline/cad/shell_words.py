# Responsibility: THE ONE LINE OF ADVICE for walls that are a shell only because the user asked for one (SHELL-23):
# under ten of their thicknesses across, or under four wide. Plain words with the wall's own numbers and the measured
# cost, from the rows the extraction keeps (cad/midsurface "short_walls").
# Boundaries: pure - no geometry, no OpenCASCADE, no numpy - so the stage (application/shell_check) can say the line
# again after the user corrects the unit, without loading the extraction.
# Collaborates with: cad/midsurface.py (writes the rows and the record's "advice"), cad/shell_pairs.py (the two
# ratios; a test holds these equal to them), application/shell_check.py (the stage's line), engines/shell_run.py
# (the result's words), docs/design/shell/README.md (1.5: where the numbers were measured).
"""SHELL-23's advice.

One wall:      "Part_6: 12.7 mm thick over 96.5 mm, 7.6 times its thickness. Shell theory is less exact under 10
                times (about 2% off on a free plate this short, more where it bends from a joint)."
Several walls: "9 walls are short for their thickness (the shortest, Part_13: 31.8 mm thick over 126 mm, 4 times its
                thickness). Shell theory is less exact under 10 times (about 5% off ...)."
"""
from __future__ import annotations

from collections.abc import Mapping

#: SHELL-2's thin rule, in walls: a sheet at least SPAN_WALLS of its thickness across (the root of its mid-surface
#: area) and WIDTH_WALLS wide is a thin shell and gets no advice (1 / shell_pairs.SHELL_RATIO, 1 / SHELL_WIDTH_RATIO)
SPAN_WALLS = 10.0
WIDTH_WALLS = 4.0
#: WHAT A SHORT PLATE COSTS, MEASURED (10 Nov, CalculiX on one thread, free-free modes 7 to 12, the product's shell
#: mesh against a C3D10 solid mesh of the same plate, three elements through its thickness). A free square plate:
#: 12 walls across 2.4%, 10 2.3%, 9 2.3%, 7.6 2.1%, 6 2.3%, 5 3.2%, 4 4.7%, 3 7.4% (the worst of the six modes) -
#: a plate alone is no worse at 6 walls than at 12, and only under about 5 does it lose. (span under, about % off)
SPAN_OFF = ((3.5, 7), (4.5, 5), (5.5, 3), (float("inf"), 2))
#: ... and a free flat bar 200 mm long: 2 walls wide 3.2%, 3 2.2%, 4 2.1%, 6 2.0%. (width under, about % off)
WIDTH_OFF = ((2.5, 3), (float("inf"), 2))
#: ... the plate ALONE. A short wall bending from a joint is another matter: a shell wall bends from the middle of
#: its joint, and a box's 4 mm walls reaching 5 to 6.7 walls from theirs read +23% (design note 2.6, MIXED-8).
JOINT_WORDS = "more where it bends from a joint"


def num(x: float) -> str:
    """A length in plain digits, three significant (7000, 40100, 2.78 - never 7e+03)."""
    return f"{float(f'{float(x):.3g}'):g}"


def _off(table: tuple, walls: float) -> int:
    return next(pct for under, pct in table if walls < under)


def short_wall_words(rows: list[dict]) -> str:
    """The advice for the walls under SHELL-2's thin rule (`rows`: body, thickness_mm, span_mm, width_mm, span_walls,
    width_walls each), "" when there is none: the wall named - the shortest when there are several - with its
    thickness, its span or width, how many times its thickness that is, and what a free plate that short costs."""
    if not rows:
        return ""

    def key(r: dict):
        return (min(float(r["span_walls"]) / SPAN_WALLS, float(r["width_walls"]) / WIDTH_WALLS), str(r["body"]),
                str(r.get("sheet")))

    w = min(rows, key=key)
    if float(w["span_walls"]) < SPAN_WALLS:
        one = (f"{num(w['thickness_mm'])} mm thick over {num(w['span_mm'])} mm, {float(w['span_walls']):g} times its "
               "thickness")
        rule = (f"Shell theory is less exact under {SPAN_WALLS:g} times (about {_off(SPAN_OFF, float(w['span_walls']))}% "
                f"off on a free plate this short, {JOINT_WORDS}).")
    else:
        one = (f"{num(w['thickness_mm'])} mm thick and {num(w['width_mm'])} mm wide, {float(w['width_walls']):g} times "
               "its thickness")
        rule = (f"Shell theory is less exact under {WIDTH_WALLS:g} times (about "
                f"{_off(WIDTH_OFF, float(w['width_walls']))}% off on a free strip this narrow, {JOINT_WORDS}).")
    if len(rows) == 1:
        return f"{w['body']}: {one}. {rule}"
    return f"{len(rows)} walls are short for their thickness (the shortest, {w['body']}: {one}). {rule}"


# ------------------------------------------------------------------------------- what a shell leaves out ----
#: SHSTAGE-9, A SHELL SAYS WHAT IT LEAVES OUT: a shell mesh is plates on the mid-surface and nothing else - no solid
#: is made beside it (SHSTAGE-7) - so a mixed body's thick regions and a body with no shell have NO elements in it.
#: The stage, the result, the decks and the reviewer said they "stay solid". They now say, in two short sentences,
#: what is not in the mesh (with the one region's size, and the share of the part's volume, cad/midsurface SHELL-60)
#: and the way on: "1 thick region (about 30 x 30 x 30 mm, 26% of the part's volume) is not in this shell mesh. For a
#: mesh that includes it, ask for a solid mesh." Until a real shell + solid mesh is offered, the way on is a solid mesh.


def pct(share: float) -> str:
    """A share in plain digits: "25%", "3.5%", "0.4%", "under 0.1%"."""
    p = 100.0 * float(share)
    if p < 0.1:
        return "under 0.1%"
    if p < 9.95:
        return f"{p:.1f}".rstrip("0").rstrip(".") + "%"
    return f"{p:.0f}%"


def dims(size_mm) -> str:
    """A box's sides, largest first: "30 x 30 x 4"."""
    return " x ".join(num(x) for x in size_mm)


def left_out(record: dict | None) -> dict:
    """What a shell mesh of the shell record (cad/midsurface) has no element for: the thick regions of its mixed bodies
    and its bodies with no shell. {} when nothing is left out, or when no body is a shell (no shell mesh is made then).

    {"thick": n thick regions, "whole": [names of the bodies with no shell], "bodies": n bodies,
     "area_share", "volume_share" (None when a region's metal was not measured), "largest_mm" (the largest thick
     region's size, largest side first, or None),
     "regions": [{"body", "whole", "faces", "area_share", "volume_share", "size_mm"}] - the record's order}"""
    bodies = list((record or {}).get("bodies") or [])
    if not any(b.get("verdict") in ("shell", "mixed") for b in bodies):
        return {}
    area = sum(float(b.get("area_mm2") or 0.0) for b in bodies)
    volume = sum(float(b.get("volume_mm3") or 0.0) for b in bodies)
    rows: list[dict] = []
    for b in bodies:
        if b.get("verdict") == "solid":
            rows.append({"body": b.get("body"), "whole": True, "faces": None, "area": b.get("area_mm2"),
                         "volume": b.get("volume_mm3"), "size_mm": b.get("size_mm")})
        for g in b.get("solid_regions") or []:
            rows.append({"body": b.get("body"), "whole": False, "faces": len(g.get("faces") or []),
                         "area": g.get("area_mm2"), "volume": g.get("volume_mm3"), "size_mm": g.get("size_mm")})
    if not rows:
        return {}

    def share(v, of: float):
        return None if v is None or of <= 0 else float(v) / of

    regions = [{"body": r["body"], "whole": r["whole"], "faces": r["faces"], "area_share": share(r["area"], area),
                "volume_share": share(r["volume"], volume), "size_mm": r["size_mm"]} for r in rows]
    known = all(r["volume"] is not None for r in rows) and volume > 0
    thick = [r for r in rows if not r["whole"]]
    big = max(thick, key=lambda r: (float(r["volume"] or 0.0), float(r["area"] or 0.0)), default=None)
    return {"thick": len(thick), "whole": [str(r["body"]) for r in rows if r["whole"]], "bodies": len(bodies),
            "area_share": share(sum(float(r["area"] or 0.0) for r in rows), area),
            "volume_share": share(sum(float(r["volume"]) for r in rows), volume) if known else None,
            "largest_mm": (big or {}).get("size_mm"), "regions": regions}


def _said(lo: Mapping) -> tuple[str, bool]:
    """What is left out, as the sentence's subject, and whether it is more than one."""
    whole = list(lo.get("whole") or [])
    n = int(lo.get("thick") or 0)
    bits = []
    if len(whole) == 1 and not n:
        bits.append(whole[0])
    elif whole:
        names = ", ".join(whole[:3]) + (" and more" if len(whole) > 3 else "")
        bits.append(f"{len(whole)} of the {lo.get('bodies')} parts ({names})")
    if n:
        bits.append(f"{n} thick region{'s' if n != 1 else ''}")
    return " and ".join(bits), len(whole) + n > 1


def left_out_short(lo: Mapping | None) -> str:
    """The first sentence alone: what is not in this shell mesh, with the one region's size and the share of the
    part's volume ("" when nothing is left out)."""
    if not lo or not lo.get("regions"):
        return ""
    what, many = _said(lo)
    regions = list(lo.get("regions") or [])
    bits = []
    size = (regions[0].get("size_mm") or lo.get("largest_mm")) if len(regions) == 1 else None
    if size:
        bits.append(f"about {dims(size)} mm")
    if lo.get("volume_share") is not None:
        bits.append(f"{pct(lo['volume_share'])} of the part's volume")
    told = f"{what} ({', '.join(bits)})" if bits else what
    told = told[:1].upper() + told[1:]
    return f"{told} {'are' if many else 'is'} not in this shell mesh."


def left_out_words(lo: Mapping | None) -> str:
    """The two short sentences: what is not in this shell mesh, and the way on ("" when nothing is left out)."""
    first = left_out_short(lo)
    if not first:
        return ""
    many = _said(lo or {})[1]
    return f"{first} For a mesh that includes {'them' if many else 'it'}, ask for a solid mesh."


def kept_short(lo: Mapping | None) -> str:
    """MIXED-15: the first sentence when a shell run will keep the thick regions as solid (engines/shell_run
    .keeps_thick): what will be kept, with the one region's size and the share of the part's volume ("" when
    nothing would be left out)."""
    first = left_out_short(lo)
    if not first:
        return ""
    many = _said(lo or {})[1]
    return first[: -len(" not in this shell mesh.")].rsplit(" ", 1)[0] + (
        f" {'are' if many else 'is'} kept as solid elements in the same mesh.")


def kept_words(lo: Mapping | None) -> str:
    """MIXED-15: the two short sentences: what will be kept as solid, and what happens if the join cannot be made."""
    first = kept_short(lo)
    if not first:
        return ""
    many = _said(lo or {})[1]
    return f"{first} If {'they' if many else 'it'} cannot be joined to the shell, {'they are' if many else 'it is'} " \
           "left out and the result says so."


def left_out_lines(lo: Mapping | None, *, most: int = 20) -> list[str]:
    """The decks' header lines (engines/shell_writers `notes`): the sentence, then one line a region."""
    if not lo or not lo.get("regions"):
        return []
    first, way = left_out_short(lo), left_out_words(lo)[len(left_out_short(lo)) + 1:]
    lines = [f"NOT IN THIS MESH: {first}", way]
    k = 0
    for r in lo["regions"][:most]:
        bits = []
        if r.get("whole"):
            head = f"- {r['body']}: the whole part"
        else:
            k += 1
            head = f"- {r['body']}, thick region {k}: {r.get('faces')} face{'s' if r.get('faces') != 1 else ''}"
        if r.get("size_mm"):
            bits.append(f"about {dims(r['size_mm'])} mm")
        if r.get("volume_share") is not None:
            bits.append(f"about {pct(r['volume_share'])} of the part's volume")
        elif r.get("area_share") is not None:
            bits.append(f"{pct(r['area_share'])} of the part's surface")
        lines.append(", ".join([head, *bits]))
    if len(lo["regions"]) > most:
        lines.append(f"- and {len(lo['regions']) - most} more")
    return lines


__all__ = ["kept_short", "kept_words", "JOINT_WORDS", "SPAN_OFF", "SPAN_WALLS", "WIDTH_OFF", "WIDTH_WALLS", "dims",
           "left_out", "left_out_lines", "left_out_short", "left_out_words", "num", "pct", "short_wall_words"]
