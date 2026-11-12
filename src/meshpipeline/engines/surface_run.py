# Responsibility: THE SHELL MESH OF A SURFACE FILE (SURF-3..6): a CAD file that is a surface, not a solid
# (cad/surface_model reads and sews it), meshed as a shell ON the surface as given - the record and surface the
# mid-surface extractor would write are built from the file itself, and from there everything is the shell path's own:
# the sizes (engines/shell_plan), the mesher and its clean-up (engines/gmsh/shell_adapter), the solver files
# (engines/shell_writers), the viewer mesh and the run's record (engines/shell_run's shell_run.json), the delivery
# (engines/shell_delivery). The wall thickness is the user's (contracts/shell_thickness); with none given the mesh is
# still delivered, sized as for a thin sheet, with the thickness plainly not set in every file. The surface is
# cleaned by rule before it is meshed (SURF-8..10, cad/surface_clean: small gaps stitched, a face written twice left
# out, pinholes filled) and the result says what was done; a stitch tolerance or a hole size in the user's notes
# (contracts/surface_stitch) is honoured. The user's element count (SHMESH-23) and the mesh requirements read from
# their notes (NOTE-7..9, engines/mesh_requirements when that reader is in the build) reach the shell mesher here
# exactly as they reach it for a mid-surface shell.
# Boundaries: deterministic, no model call, no store. Run in a process of its own (main()): OpenCASCADE and gmsh can
# crash, and a crash must cost the run a plain reason, never a hang. Writes only into the workspace it is given. Never
# raises from run(). A file that is not a surface (a solid, faces that close, a closed skin with a gap) is left alone:
# its record says which, and the FEA driver carries on exactly as before.
# The notes' mesh requirements (NOTE-7) reach it too (SURF-7, engines/surface_requirements): their sizes, limits and
# element kind are the shell mesher's, a global size with no thickness sizes the sheet itself, and a thickness in them
# is used when the word table read none.
# Collaborates with: engines/fea_driver.py (calls surface_pass, cites it), cad/surface_model.py, engines/shell_run.py,
# engines/shell_delivery.py, contracts/shell_thickness.py, engines/surface_requirements.py.
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: the surface run's own record of what the file is (beside shell_run.json, which a made shell also writes)
READING = "surface_model.json"
GATE = "fea_surface"
#: SURF-5: WITH NO THICKNESS GIVEN, the sizes follow the shape as for a thin sheet: a wall of the sheet's size (the
#: square root of its area) over SIZING_DIVISIONS. Measured on the shell lab's 72 public thin-walled parts: the
#: thinnest wall over the largest sheet's size is 1/120 at the middle and 1/300 at the lower quarter - so three real
#: parts in four are at least this thick, and their own floor (half the wall) would be no finer. The wall is used
#: for the sizes only (the floor, what counts as a panel, the sliver clean-up): no thickness is written.
SIZING_DIVISIONS = 300.0
#: ... and when the mesh sized so leaves elements past Nastran's own limits (the sliver clean-up works in walls: a
#: thin wall cleans little), the sheet is sized again at each of these multiples of that wall, and the first mesh
#: inside the limits is kept - else the one with the fewest elements past them. Measured on the lab's parts of known
#: wall: 4 of 28 left 2 to 5 triangles past the limits at the thin wall.
SIZING_AGAIN = (2.0, 4.0)
#: SURF-4: a thickness past this share of its sheet's size is said (shell theory holds to about 1/10, SHELL-2);
#: the user's number still stands
THICK_SHARE = 0.1
#: the run's time limit, seconds (sewing is seconds; the shell mesh is bounded by its own deadline, SHMESH-21)
RUN_LIMIT_S = 1800
SURFACE_LINE = "This file is a surface, not a solid. It is meshed as a shell"
#: with no thickness given: what the stage says before the run, and what the result says after it
NO_THICKNESS_YET = ('No thickness given yet. Add it to your notes (for example "1.2 mm thick"), or set it later in '
                    "the solver file.")
NO_THICKNESS = ('No thickness was given, so none is set in the files: add it to your notes (for example "1.2 mm '
                'thick") and run again, or set it on the property line in the file.')


def _mm(v: float) -> str:
    from meshpipeline.contracts.shell_thickness import words

    return words(v)


def line(thickness_mm: float | None) -> str:
    """SURF-6: THE ONE LINE the user reads, on the stage and on the result."""
    return f"{SURFACE_LINE}, {_mm(thickness_mm)} thick." if thickness_mm else f"{SURFACE_LINE}."


def words(rec: dict) -> str:
    """What the surface run made, in plain words, for the decision record and the result."""
    s = rec.get("surface") or {}
    if not rec.get("made"):
        return (f"This file is a surface, not a solid, so it can only be meshed as a shell - and the shell mesh could "
                f"not be made: {rec.get('why') or 'it could not be made'}.")
    n, q, t = int(rec.get("elements") or 0), int(rec.get("quads") or 0), int(rec.get("trias") or 0)
    said = [line(s.get("thickness_mm")),
            f"{n:,} elements ({q:,} quads, {t:,} triangles). Files: Nastran .bdf, CalculiX and Abaqus .inp, Ansys .cdb."]
    if not s.get("thickness_mm"):
        said.append(NO_THICKNESS)
    sheets = int(s.get("sheets") or 1)
    if sheets > 1:
        said.append(f"The file's {sheets} separate sheets are {sheets} parts, each with its own property.")
    closing = rec.get("mesh_close") if isinstance(rec.get("mesh_close"), dict) else None
    said += clean_words(s.get("cleaned"), closing)
    if _told_by_place(s.get("cleaned"), closing):
        # SURF-12 / SURF-13: one sentence per place - what was done there, by how much, where - then what is open
        said += place_words(s.get("cleaned"), closing)
        n_open = int(rec.get("open_edges") or 0)
        if n_open:
            said.append(f"The surfaces still do not meet at {_count(n_open, 'place')}: the shell has gaps there.")
    else:
        said += gap_words(int(rec.get("open_edges") or 0), s.get("cleaned"))
    if s.get("others_mm"):
        said.append("Your notes also name " + ", ".join(_mm(v) for v in s["others_mm"]) + ": one thickness is used for "
                    "the whole file; change a part's on its own property line.")
    if s.get("unread"):
        said.append(f"{str(s['unread'])[:1].upper()}{str(s['unread'])[1:]} was not read as a thickness: say it in mm.")
    if s.get("thick_for_mm"):
        said.append(f"At {_mm(s['thickness_mm'])} the sheet is thick for its size ({s['thick_for_mm']:.3g} mm): a "
                    "shell is less exact for it.")
    from meshpipeline.engines.shell_plan import count_words

    counted = count_words(rec.get("target"))
    if counted:
        said.append(counted)
    if rec.get("count_set_aside"):
        said.append(f"Your element count ({int(rec['count_set_aside']):,}) was not used: the global size you gave "
                    "decides the sizes.")
    return " ".join(said)


def _gap(mm: float) -> str:
    """A gap as it is said: "0.12 mm", "0.004 mm"."""
    return f"{float(mm):.2g} mm"


#: SURF-13: where the reach came from, as the words say it
REACH_WORDS = {"notes": "the gap your notes allow", "asked": "the element size you asked for",
               "run": "the element size of this mesh", "part": "1/100 of the part's size"}
#: the most places told one by one (the rest are in the run's record)
MAX_TOLD = 12


def _told_by_place(cleaned: dict | None, closing: dict | None) -> bool:
    """Whether the places are told one by one (SURF-12/13: the clean-up ran and named its places, and the user did not
    ask for the surfaces to be kept as drawn - then only stitching ran, and it is told as before)."""
    c = cleaned or {}
    return "places" in c and c.get("stitch") != "off" and not (c.get("mend") or {}).get("off")


def _at(at) -> str:
    return "(" + ", ".join(f"{round(float(v), 1) + 0.0:g}" for v in list(at or [])[:3]) + ") mm"


def _between(faces: list) -> str:
    """ "Face_3 and Face_7", "Face_3 and its 3 neighbours" (a face lifted off all its neighbours)."""
    pairs = [list(f) for f in faces or [] if len(f) == 2]
    if not pairs:
        return "two faces"
    if len(pairs) == 1:
        return f"{pairs[0][0]} and {pairs[0][1]}"
    names = [n for f in pairs for n in f]
    common = max(sorted(set(names)), key=names.count)
    others = sorted({n for f in pairs for n in f if n != common})
    return f"{common} and its {len(others)} neighbours" if names.count(common) == len(pairs) else \
        " and ".join(sorted(set(names)))


def place_words(cleaned: dict | None, closing: dict | None) -> list[str]:
    """SURF-12 / SURF-13: ONE PLAIN SENTENCE PER PLACE: what was done there, by how much and where - a face extended,
    trimmed or its edge moved (the surfaces changed), the faces stitched, the gap closed in the mesh - or why it is
    left open. At most MAX_TOLD; the rest are counted and are in the run's record."""
    c = cleaned or {}
    m = c.get("mend") or {}
    out: list[str] = []
    for r in m.get("changes") or []:
        a, b = (list(r.get("faces") or []) + ["a face", "its neighbour"])[:2]
        by = _gap(float(r.get("moved_mm") or r.get("gap_mm") or 0.0))
        how = str(r.get("how") or "")
        if how.startswith("extended"):
            did = f"{a} was extended {by} along its own surface to meet {b}"
        elif how.startswith("trimmed"):
            did = f"{a} was trimmed back {by} to the edge of {b}"
        else:
            did = f"the edge of {a} was moved {by} onto {b}"
        out.append(f"At {_at(r.get('at_mm'))}, {did}.")
    for p in c.get("stitched_places") or []:
        out.append(f"At {_at(p.get('at_mm'))}, the faces were stitched together across a {_gap(float(p.get('gap_mm') or 0.0))} gap.")
    reach = float((closing or {}).get("reach_mm") or 0.0)
    rows = (closing or {}).get("places")
    if rows is None:
        # nothing went to the mesh (no mesh made, or nothing left): the places still open, as the reading has them
        rows = [{"at_mm": p.get("at_mm"), "gap_mm": p.get("gap_mm"), "faces": p.get("faces"), "closed": False,
                 "why": ""} for p in c.get("places") or []]
    for r in rows:
        gap = _gap(float(r.get("gap_mm") or 0.0))
        where = f"At {_at(r.get('at_mm'))}, the {gap} gap between {_between(r.get('faces') or [])}"
        if r.get("closed"):
            n = int(r.get("joined") or 0) + int(r.get("put_in") or 0)
            out.append(f"{where} was closed in the mesh: {n} nodes joined across it, none moved more than "
                       f"{_gap(float(r.get('moved_mm') or 0.0))}.")
            continue
        why = str(r.get("why") or "")
        if why == "wider than the reach":
            why = (f"it is wider than {REACH_WORDS.get(str((closing or {}).get('reach_from')), 'the reach')} "
                   f"({_gap(reach)})")
        out.append(f"{where} is left open{': ' + why if why else ''}.")
    if len(out) > MAX_TOLD:
        extra = len(out) - (MAX_TOLD - 1)
        out = out[:MAX_TOLD - 1] + [f"{extra} more places are listed in the run's record."]
    return out


def clean_words(cleaned: dict | None, closing: dict | None = None) -> list[str]:
    """SURF-8..10: WHAT THE CLEAN-UP DID, one short sentence each, nothing when it did nothing. `closing`: what the
    mesh closed (SURF-13), counted with the rest."""
    c = cleaned or {}
    out = []
    by_place = _told_by_place(c, closing)
    if c.get("duplicates"):
        n = int(c["duplicates"])
        out.append(f"{_count(n, 'face was', 'faces were')} in the file twice: the second copy of {'it' if n == 1 else 'each'} is left out.")
    in_mesh = int((closing or {}).get("closed") or 0)
    if c.get("closed_places") or in_mesh:
        # SURF-8: said in PLACES, the count the record and gap_words use: places before - closed = places after.
        # (Joins inside a place that stays open - part of its length - are not a place closed.)
        asked = (f", as your notes asked (up to {_gap(c['limit_mm'])})"
                 if c.get("limit_from") == "notes" and not c.get("fell_back") else "")
        widest = float(c.get("closed_places_widest_mm") or 0.0)
        k = int(c.get("closed_places") or 0) + in_mesh
        if by_place:
            out.append(f"Closed {_closed_of(k, int(c.get('places_before') or 0))} where the surfaces did not meet"
                       f"{asked}.")
        else:
            out.append(f"Closed {_closed_of(k, int(c.get('places_before') or 0))} where "
                       f"the surfaces did not meet{f', the widest {_gap(widest)}' if widest > 0 else ''}{asked}.")
    if c.get("pinholes"):
        asked = ", as your notes asked" if c.get("holes_from") == "notes" else ""
        k = int(c["pinholes"])
        what = _count(k, "hole") if c.get("holes_from") == "notes" else _count(k, "pinhole")
        out.append(f"Filled {what}, {'' if k == 1 else 'the largest '}{_gap(c.get('pinhole_widest_mm') or 0.0)} "
                   f"across{asked}.")
    m = c.get("mend") or {}
    if m.get("closed") and not by_place:
        how = [f"{int(m[k])} {w}" for k, w in (("moved", "moved onto a neighbour"),
                                                ("extended", "extended along their own surface"),
                                                ("trimmed", "trimmed back to a neighbour's edge")) if m.get(k)]
        out.append(f"To make the surfaces meet, {', '.join(how)} - by up to {_gap(m.get('widest_mm') or 0.0)}"
                   f" (each change is listed in the run's record).")
    if m.get("off"):
        out.append("No surface was changed, as your notes asked.")
    if c.get("fell_back"):
        out.append(f"Stitching up to {_gap(c['limit_mm'])}, as your notes asked, would have left more open than the "
                   f"usual {_gap(c.get('rule_mm') or 0.0)}, so the usual result is kept.")
    if c.get("stitch") == "off":
        out.append("Nothing was stitched, as your notes asked.")
    return out


def gap_words(n: int, cleaned: dict | None) -> list[str]:
    """SURF-2 / SURF-8: WHERE THE SURFACES DO NOT MEET, by the one count the record holds (cad/surface_model.
    open_places: n places in all, the same whatever tolerance was asked). Those within what was stitched are said
    with why they were left, and no tolerance is offered for them (a larger one would not close them either); those
    wider are said with a tolerance only when it was tried on this file and closed them."""
    if not n:
        return []
    c = cleaned or {}
    if "left_open" not in c:
        return [f"The surfaces do not meet at {_count(n, 'place')}: the shell has gaps there."]
    out = []
    left, wide = int(c.get("left_open") or 0), int(c.get("wider_gaps") or 0)
    if c.get("stitch") == "off" or c.get("applied_mm") is None:
        return [f"The surfaces do not meet at {_count(n, 'place')}: the shell has gaps there (the widest "
                f"{_gap(max(float(c.get('left_widest_mm') or 0.0), float(c.get('wider_widest_mm') or 0.0)))})."]
    applied = float(c["applied_mm"])
    out.append(f"The surfaces do not meet at {_count(n, 'place')}: the shell has gaps there.")
    # every sentence stands alone: it names what was stitched, and its counts add up to the n above
    if left:
        said = (f"At {_of(left, n)}, the gap is narrower than the {_gap(applied)} stitched here (the widest "
                f"{_gap(float(c.get('left_widest_mm') or 0.0))}) but stitching does not join "
                f"{'it' if left == 1 else 'them'}")
        said += f", because {c['refused']}." if c.get("refused") else ": an edge lies over its neighbour's face, or a corner is off its neighbour's corner."
        out.append(said)
    if wide:
        lo, hi = float(c.get("wider_narrowest_mm") or 0.0), float(c.get("wider_widest_mm") or 0.0)
        span = _gap(hi) if abs(hi - lo) <= 1e-9 * max(hi, 1.0) else f"{_gap(lo)[:-3]} to {_gap(hi)}"
        said = (f"At {_of(wide, n)}, the surfaces run side by side {span} apart, wider than the {_gap(applied)} "
                "stitched here.")
        if c.get("advice_mm"):
            said += f' If they should meet, add to your notes: "stitch gaps up to {float(c["advice_mm"]):g} mm".'
        else:
            said += f" Stitching does not close {'it' if wide == 1 else 'them'} cleanly, so {'it is' if wide == 1 else 'they are'} left as drawn."
        out.append(said)
    m = c.get("mend") or {}
    rows = [r for r in m.get("left_places") or [] if r.get("why")]
    if rows and n:
        # SURF-12: why the surfaces were not changed where they still do not meet (the commonest reason first)
        whys = [str(r["why"]) for r in rows]
        top = max(sorted(set(whys)), key=whys.count)
        k = min(whys.count(top), n)
        out.append(f"At {_of(k, n)}, the surfaces were not changed: {top}.")
    return out


def _closed_of(k: int, n: int) -> str:
    """ "the 1 place", "both places", "all 3 places", "2 of the 5 places"."""
    if n <= 1:
        return "the 1 place"
    if k >= n:
        return "both places" if n == 2 else f"all {n} places"
    return f"{k} of the {n} places"


def _count(k: int, noun: str, nouns: str | None = None) -> str:
    """ "1 place", "3 places" ("1 face was", "2 faces were" when the plural is given)."""
    return f"{k} {noun}" if k == 1 else f"{k} {nouns or noun + 's'}"


def _of(k: int, n: int) -> str:
    """ "that place", "both places", "all 3 places", "2 of these 4 places"."""
    if n == 1:
        return "that place"
    if k == n:
        return "both places" if n == 2 else f"all {n} places"
    return f"{k} of these {n} places"


def stitch_for(state):
    """SURF-8 / SURF-10: what the user's words say about the clean-up (contracts/surface_stitch): a stitch tolerance,
    "do not stitch", a hole size - the latest word standing, a note over the intake's summary."""
    from meshpipeline.contracts import shell_thickness as st
    from meshpipeline.contracts import surface_stitch as ss

    return ss.read(st.texts_of(state))


def requirements_in(workspace: Path | str) -> dict | None:
    """NOTE-7..9: the mesh requirements the FEA driver read from the user's notes and left in the workspace (a global
    size, edge lengths, quality limits, the element kind), for the shell mesher: the same reading engines/shell_run
    takes (SURF-7). None when nothing was asked."""
    from meshpipeline.engines import shell_run

    try:
        return shell_run.requirements_in(workspace)
    except Exception as exc:  # noqa: BLE001 - no requirements read is no requirements
        logger.warning("surface run: the notes' mesh requirements were not read (%s)", type(exc).__name__)
        return None


def _global_mm(requirements: dict | None) -> float | None:
    """SURF-12: the element size the user asked for everywhere (the notes' mesh requirements), in mm: a gap under one
    element is one the surfaces may be changed by."""
    try:
        v = ((requirements or {}).get("sizes") or {}).get("global_mm")
        return float(v) if v else None
    except (TypeError, ValueError):
        return None


def _close_in_mesh(mesh, applied, reading: dict, *, requirements: dict | None, sew_mm: float | None,
                   wall_mm: float | None):
    """SURF-13: the places the surfaces still do not meet at, closed in the shell mesh where the gap is narrower than
    the reach (the notes' "close gaps up to ...", else one element - the user's size, else the run's - never past
    1/100 of the part). Returns (the mesh, the record) - (mesh, None) when nothing was to be closed, or the user said
    not to close gaps ("do not stitch") or to keep the surfaces as drawn (then only stitching closes anything). Never
    raises: a failure leaves the mesh as made, every place told open."""
    from meshpipeline.cad import surface_mend as smd
    from meshpipeline.engines import surface_mesh_close as smc

    c = reading.get("cleaned") or {}
    places = [p for p in c.get("places") or [] if p.get("faces")]
    if not places or c.get("stitch") == "off" or (c.get("mend") or {}).get("off"):
        return mesh, None
    m = c.get("mend") or {}
    if m.get("reach_mm"):
        reach, reach_from = float(m["reach_mm"]), str(m.get("reach_from") or "part")
    else:
        size = _global_mm(requirements)
        size_from = "asked"
        if not size:
            size, size_from = smd.run_element_mm(reading.get("sheets") or [], wall_mm), "run"
        reach, reach_from = smd.reach_of(float(reading.get("diag_mm") or 0.0), limit_mm=sew_mm, element_mm=size,
                                         element_from=size_from)
    rows = [dict(p, gap_mm=max(float(p.get("gap_mm") or 0.0), float(p.get("apart_mm") or 0.0)),
                 drawn_gap_mm=p.get("gap_mm")) for p in places]
    try:
        got, record = smc.close(mesh, rows, reach_mm=reach, reach_from=reach_from)
    except Exception as exc:  # noqa: BLE001 - the mesh as made; every place is told open
        logger.warning("surface run: the gaps were not closed in the mesh (%s: %s)", type(exc).__name__, str(exc)[:200])
        return mesh, {"rule": smc.RULE, "error": type(exc).__name__, "closed": 0, "left": len(rows), "places": [],
                      "reach_mm": reach, "reach_from": reach_from}
    if record.get("closed") and isinstance(applied, dict):
        # the quality as delivered: measured again on the closed mesh (the steps' own counts kept)
        from meshpipeline.engines import shell_quality
        from meshpipeline.engines.shell_mesh import edge_census

        before: dict = applied["quality"] if isinstance(applied.get("quality"), dict) else {}
        q = shell_quality.measure(got)
        q["edges"] = {k: v for k, v in edge_census(got).items() if k != "free_edges"}
        applied["quality"] = {**before, **q}
    return got, record


def _after_close(out: Path, record: dict, applied: dict, closing: dict) -> None:
    """SURF-13: the record and the quality as the closed mesh has them (shell.json's notes and its connected check,
    shell_plan.json's quality)."""
    from meshpipeline.cad import midsurface as ms

    k, left = int(closing.get("closed") or 0), int(closing.get("left") or 0)
    record.setdefault("notes", []).append(
        f"{k} place(s) where the surfaces did not meet were closed in the mesh: the boundary nodes facing each other "
        f"across the gap joined, none moved farther than the gap (SURF-13).")
    if not left and not any(int(c.get("open_edges") or 0) > k for c in record.get("sewn") or []):
        record.setdefault("checks", {})["connected"] = True
    (out / ms.RECORD_FILE).write_text(json.dumps(record, indent=1, sort_keys=True))
    (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))


def _mesh(out: Path, *, mm_per_unit: float, target: dict | None, requirements: dict | None):
    """The shell mesher on the record in `out`, with the user's count and (when given) their notes' requirements."""
    from meshpipeline.engines.gmsh import shell_adapter as sa

    # with no requirement the mesher is called exactly as before (no new keyword)
    more: dict = {"requirements": requirements} if requirements else {}
    return sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target, **more)


def deck_notes(thickness_mm: float | None) -> list[str]:
    """The lines every solver file carries at its top about where the shell came from and its thickness."""
    out = ["Made from a surface file: the elements lie on the surface as given."]
    if thickness_mm:
        out.append(f"Wall thickness {_mm(thickness_mm)}, as the user gave it.")
    else:
        out += ["WALL THICKNESS NOT SET: the file is a surface and no thickness was given.",
                "Every shell property below carries 0: set the real thickness on each before solving."]
    return out


def _past(applied: dict) -> int:
    """How many elements of a mesh are folded or past Nastran's own limits (the plan as applied, shell_adapter)."""
    q = applied.get("quality") if isinstance(applied, dict) else None
    if not isinstance(q, dict):
        return 0
    return int(q.get("invalid") or 0) + sum(int(v) for v in (q.get("nastran_past") or {}).values())


def sizing_wall(size_mm: float) -> float:
    """SURF-5: the wall a sheet is sized with when no thickness was given (mm)."""
    return float(f"{float(size_mm) / SIZING_DIVISIONS:.6g}")


def run(workspace: Path | str, step: Path | str, *, mm_per_unit: float, thickness_mm: float | None = None,
        source: str = "", target: dict | None = None, said_shell: bool = False, others_mm: list | None = None,
        unread: str = "", stitch: bool = True, sew_mm: float | None = None, holes_mm: float | None = None,
        requirements: dict | None = None, mend: bool = True,
        thickness_how: dict | None = None) -> dict:
    """Read `step` (coordinates `mm_per_unit` mm each); when it is a surface, sew it, mesh it as a shell and write
    every file a shell run writes into `workspace`. Returns the run's record, shell_run.json's shape plus "surface":
    {"kind", "why", "thickness_mm", "thickness_set", "sizing_wall_mm", "sheets", "faces", "open_share", ...}.
    `thickness_mm`: the user's (None: not given); `said_shell`: their words asked for a shell (SURF-1). `stitch`
    False, `sew_mm`, `holes_mm`: what their notes say of the clean-up (SURF-8, SURF-10). `target`: the element count
    they asked for (SHMESH-23). `requirements`: the mesh requirements read from the user's notes
    (engines/mesh_requirements) - applied as on any shell (SHMESH-24..26) and told asked against delivered; a global
    size among them sizes a sheet with no thickness (SURF-7). `thickness_how`: where the thickness came from
    (engines/surface_requirements.thickness). None: the rules' own surface shell, exactly as before."""
    import numpy as np

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm
    from meshpipeline.cad.scout import read_cad
    from meshpipeline.engines import shell_run
    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines import surface_requirements as srq

    ws = Path(workspace)
    started = time.time()
    rec: dict = {"made": False, "mm_per_unit": float(mm_per_unit), "surface": {"kind": "unread"}}
    try:
        wall = float(thickness_mm or 0.0)
        given = wall > 0
        reading, sewn = sm.read(read_cad(Path(step)), mm_per_unit=mm_per_unit, said_shell=said_shell or given,
                                stitch=stitch, sew_mm=sew_mm, holes_mm=holes_mm, mend=mend,
                                element_mm=_global_mm(requirements), wall_mm=wall if given else None)
        (ws / READING).write_text(json.dumps(reading, indent=1, sort_keys=True, default=float))
        rec["surface"] = {"kind": reading["kind"], "why": reading.get("why"), "faces": reading.get("faces"),
                          "open_share": reading.get("open_share"), "sheets": len(reading.get("sheets") or []),
                          "sew_tolerance_mm": reading.get("sew_tolerance_mm"),
                          "thickness_mm": wall if given else None, "thickness_set": given,
                          "others_mm": [float(v) for v in others_mm or []] if given else [],
                          "unread": str(unread or ""), "cleaned": reading.get("cleaned")}
        if reading["kind"] != "surface" or sewn is None:
            rec["why"] = f"the file is not a surface part ({reading.get('why')})"
        else:
            sheets = reading["sheets"]
            # SURF-7: a global size in the notes and no thickness - the sheet is worked at the size asked, never at
            # the thin-sheet guess (SURF-5), and it is not sized again
            by_size = None if given else srq.working_wall(requirements)
            walls = {s["part"]: (wall if given else by_size if by_size is not None else sizing_wall(s["size_mm"]))
                     for s in sheets}
            if not given:
                rec["surface"]["sizing_wall_mm"] = sorted(set(walls.values()))
                if by_size is not None:
                    rec["surface"]["sizing_by"] = "global size"
            else:
                thick = [s["size_mm"] for s in sheets if wall > THICK_SHARE * s["size_mm"]]
                if thick:
                    rec["surface"]["thick_for_mm"] = float(min(thick))
            record, surface = sm.record(reading, sewn, mm_per_unit=mm_per_unit, walls_mm=walls,
                                        given=dict.fromkeys(walls, given))
            out = ws / shell_run.FOLDER
            sm.write(record, surface, out)
            rec.update(bodies={"shell": len(sheets)}, extract_s=round(time.time() - started, 1), solid_bodies=0,
                       open_edges=int(sum(int(r.get("open_edges") or 0) for r in record["sewn"])),
                       free_edges=int(sum(s["free_edges"] for s in sheets)))
            t1 = time.time()
            mesh, applied = _mesh(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
            if not given and by_size is None and _past(applied):
                # SURF-5: sized again for a thicker sheet while elements are left past Nastran's own limits
                best = (_past(applied), mesh, applied, record, walls)
                for factor in SIZING_AGAIN:
                    walls_k = {p: float(f"{w * factor:.6g}") for p, w in walls.items()}
                    record_k, surface_k = sm.record(reading, sewn, mm_per_unit=mm_per_unit, walls_mm=walls_k,
                                                    given=dict.fromkeys(walls_k, False))
                    sm.write(record_k, surface_k, out)
                    try:
                        mesh_k, applied_k = _mesh(out, mm_per_unit=mm_per_unit, target=target,
                                                  requirements=requirements)
                    except ValueError:
                        continue
                    if _past(applied_k) < best[0]:
                        best = (_past(applied_k), mesh_k, applied_k, record_k, walls_k)
                    if not best[0]:
                        break
                _n, mesh, applied, record, walls = best
                sm.write(record, surface, out)
                (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
                rec["surface"]["sizing_wall_mm"] = sorted(set(walls.values()))
            # SURF-13: what is still open, narrower than the reach, closed in the mesh
            mesh, closing = _close_in_mesh(mesh, applied, reading, requirements=requirements, sew_mm=sew_mm,
                                           wall_mm=wall if given else None)
            if closing is not None:
                rec["mesh_close"] = closing
                if closing.get("closed"):
                    _after_close(out, record, applied, closing)
                    rec["open_edges"] = max(0, int(rec["open_edges"]) - int(closing["closed"]))
            rec["mesh_s"] = round(time.time() - t1, 1)
            if requirements and isinstance(applied, dict) and applied.get("requirements"):
                # SURF-7: asked against delivered, said as a surface file's (its thickness is the user's, applied)
                # (a report with no rows to say is kept as the mesher gave it)
                applied["requirements"] = srq.told(applied["requirements"], elements=int(mesh.n_elements),
                                                   thickness_mm=wall if given else None, how=thickness_how,
                                                   unread=str(unread or "")) or applied["requirements"]
                (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
            if not given:
                # SURF-5: the sizing wall sized the mesh; it is no thickness, and none is written
                mesh.wall = np.zeros(mesh.n_elements, dtype=float)
                mesh.node_wall, mesh.varies = None, None
            # SHWRITE-1: each sheet named as a shell run names its parts (Part_k where the staging named none)
            shell_run.name_parts(mesh, ws)
            exports = sw.write_all(ws, mesh, units="m", source=source, notes=deck_notes(wall if given else None))
            faces = shell_run.write_msh(ws / shell_run.MSH, mesh)
            xyz_m = mesh.xyz / 1000.0
            q = applied.get("quality") if isinstance(applied, dict) else None
            (ws / "shell_quality.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
            rec.update(nodes=int(len(mesh.xyz)), faces=faces, parts=shell_run.parts_of(mesh, faces),
                       bounds_m=[float(v) for v in (*xyz_m.min(axis=0), *xyz_m.max(axis=0))] if len(xyz_m) else None,
                       made=all((ws / f).exists() and (ws / f).stat().st_size > 0 for f in shell_run.FILES),
                       elements=int(mesh.n_elements), quads=int(len(mesh.quads)), trias=int(len(mesh.trias)),
                       walls_mm=sorted({round(float(w), 3) for w in mesh.wall}) if given else [],
                       quality=q if isinstance(q, dict) else None,
                       exports={k: v for k, v in exports.items() if isinstance(v, dict)})
            if isinstance(applied, dict) and isinstance(applied.get("target"), dict):
                rec["target"] = applied["target"]
            if isinstance(applied, dict) and requirements:
                for key in ("requirements", "sizing", "count_set_aside"):
                    if applied.get(key):
                        rec[key] = applied[key]
                rec["surface"]["notes"] = srq.words(thickness_how, requirements, given=given)
                if thickness_how and thickness_how.get("by"):
                    rec["surface"]["thickness_by"] = thickness_how["by"]
            if not rec["made"]:
                rec["why"] = "the solver files could not all be written"
            for name in (ms.RECORD_FILE, ms.SURFACE_FILE):
                if (out / name).exists():
                    import shutil

                    shutil.copyfile(out / name, ws / name)
    except Exception as exc:  # noqa: BLE001 - said in the record; the driver tells the user
        logger.warning("surface run: no shell mesh (%s: %s)", type(exc).__name__, str(exc)[:300])
        rec.update(made=False, why=f"the shell mesh could not be made ({type(exc).__name__}: {str(exc)[:200]})")
    rec["seconds"] = round(time.time() - started, 1)
    if (rec.get("surface") or {}).get("kind") == "surface":
        rec["words"] = words(rec)
        (ws / shell_run.RECORD).write_text(json.dumps(rec, indent=1, sort_keys=True, default=float))
    (ws / "surface_run.json").write_text(json.dumps(rec, indent=1, sort_keys=True, default=float))
    return rec


def read(workspace: Path | str) -> dict:
    """The surface run's record, {} when none ran."""
    try:
        got = json.loads((Path(workspace) / "surface_run.json").read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def is_surface(rec: dict | None) -> bool:
    """Whether a run's record (shell_run.json or surface_run.json) is a surface file's."""
    return isinstance(rec, dict) and (rec.get("surface") or {}).get("kind") == "surface"


def said_shell(state) -> bool:
    """Whether a shell was asked for: the choice settled on the stage from the user's words (SHSTAGE-5's
    fea_mesh_as), which SURF-1 reads for a file whose faces nearly close."""
    return str((state or {}).get("fea_mesh_as") or "").strip().lower() == "shell"


def thickness_for(state):
    """SURF-4: THE RUN'S THICKNESS, the latest word standing: a thickness in the user's notes (kept word for word,
    a note can be added after the stage), else the one settled on the stage at Proceed from their messages (state
    fea_shell_thickness_mm), else one in the intake's summary. A contracts/shell_thickness.ThicknessReading."""
    from meshpipeline.contracts import shell_thickness as st

    s = state or {}
    notes = st.read([str(n.get("text") or "") for n in s.get("user_notes") or [] if isinstance(n, dict)])
    if notes.given:
        return notes
    try:
        settled = float(s.get("fea_shell_thickness_mm") or 0.0)
    except (TypeError, ValueError):
        settled = 0.0
    if st.MIN_MM <= settled <= st.MAX_MM:
        return st.ThicknessReading(mm=settled, quote="as said before the run", unread=notes.unread)
    summary = st.read([str(s.get("request_txt") or "")])
    return summary if summary.given or not notes.unread else st.ThicknessReading(unread=notes.unread)


async def surface_pass(ws: Path, state, *, job_id: str, run=None, target: dict | None = None, cite=None,
                       left_s: float | None = None) -> dict | None:
    """SURF-3: the FEA driver's step for a file with no solid whose faces do not close. Reads the user's thickness
    (SURF-4), runs the surface run in a process of its own and cites what it found. Returns the run's record when
    the file IS a surface part (made or not - there is no solid to fall back to), None when it is not (the driver
    carries on as before)."""
    import asyncio
    import subprocess

    from meshpipeline.engines.fea_plan import STAGED_SOLIDS

    step = next((ws / n for n in STAGED_SOLIDS if (ws / n).exists()), None)
    if step is None:
        return None
    # SURF-7: the notes' mesh requirements, as the driver left them in the workspace (None: nothing asked), and
    # the ONE thickness - the word table's, else the one in the notes' typed set; SURF-8..10: what the notes say of
    # the clean-up
    from meshpipeline.engines import surface_requirements as srq

    req = requirements_in(ws)
    got, how = srq.thickness(thickness_for(state), req)
    sewing = stitch_for(state)
    args = {"thickness_mm": got.mm, "others_mm": list(got.others_mm), "unread": got.unread,
            **({"thickness_how": how} if req else {}),
            "said_shell": said_shell(state), "target": {k: target[k] for k in ("elements", "mode", "low", "high")
                                                         if k in target} if target and target.get("elements") else None,
            "stitch": not sewing.off, "sew_mm": sewing.mm, "holes_mm": sewing.holes_mm,
            "mend": not sewing.keep_surfaces}
    limit = float(RUN_LIMIT_S)
    if left_s is not None:
        limit = max(120.0, min(limit, 0.8 * left_s))
    try:
        proc = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", __name__, str(ws), str(step), "staged", json.dumps(args)],
            capture_output=True, text=True, timeout=limit, check=False)
        rec = read(ws)
        if not rec:
            rec = {"made": False, "surface": {"kind": "unread"},
                   "why": f"the surface run stopped (exit {proc.returncode})"}
    except subprocess.TimeoutExpired:
        rec = {"made": False, "surface": {"kind": "surface"},
               "why": f"it took longer than the {int(limit) // 60} minutes allowed"}
    except Exception as exc:  # noqa: BLE001 - not a surface run: the driver carries on as before
        logger.warning("surface run: not started (%s)", type(exc).__name__)
        return None
    if not is_surface(rec):
        return None
    if not rec.get("words"):
        rec["words"] = words(rec)
    s = rec.get("surface") or {}
    if cite is not None:
        cite(job_id, [{"stage": GATE, "decided": "surface part", "value": s.get("sheets"), "rule": "SURF-1",
                       "inputs": {k: s.get(k) for k in ("why", "faces", "open_share", "sheets", "sew_tolerance_mm")},
                       "words": f"The file is a surface, not a solid: {s.get('why')}."},
                      {"stage": GATE, "decided": "thickness", "value": s.get("thickness_mm"), "rule": "SURF-4",
                       "inputs": {"quote": got.quote, "others_mm": list(got.others_mm), "unread": got.unread,
                                  "sizing_wall_mm": s.get("sizing_wall_mm"),
                                  **({"read_by": how["by"], "sizing_by": s.get("sizing_by")} if req else {})},
                       "words": (f"The wall thickness is the user's: {_mm(s['thickness_mm'])} (\"{got.quote}\")."
                                 if s.get("thickness_mm") else
                                 "No thickness was given: the mesh is sized at the global size the notes ask for "
                                 "and no thickness is written (SURF-7)." if s.get("sizing_by") else
                                 "No thickness was given: the mesh is sized as for a thin sheet and no thickness is "
                                 "written (SURF-5).")},
                      *_clean_cites(s.get("cleaned"), sewing),
                      {"stage": GATE, "decided": "shell mesh" if rec.get("made") else "no shell mesh",
                       "value": rec.get("elements"), "rule": "SURF-3",
                       "inputs": {k: rec.get(k) for k in ("open_edges", "free_edges", "seconds", "why", "target")},
                       "words": rec["words"]}]
             + ([{"stage": GATE, "decided": "notes applied", "value": len(req.get("rows") or []),
                  "rule": srq.RULE,
                  "inputs": {"sizes": req.get("sizes"), "thickness_by": how["by"], "thickness_set_mm": how["set_mm"],
                             "sizing_by": s.get("sizing_by")},
                  "words": s.get("notes") or srq.words(how, req, given=bool(s.get("thickness_mm")))}]
                if req and rec.get("made") else []))
    return rec


def _clean_cites(cleaned: dict | None, sewing) -> list[dict]:
    """SURF-8..10: the clean-up in the decision record - one entry, only when it did or was asked something."""
    c = cleaned or {}
    did = clean_words(c)
    if not did and not c.get("left_open") and not getattr(sewing, "given", False):
        return []
    return [{"stage": GATE, "decided": "surface cleaned", "value": int(c.get("closed_places") or 0), "rule": "SURF-8",
             "inputs": {"limit_mm": c.get("limit_mm"), "limit_from": c.get("limit_from"), "rung_mm": c.get("rung_mm"),
                        "places_before": c.get("places_before"), "joined_edge_pairs": c.get("closed_edges"),
                        "closed_widest_mm": c.get("closed_widest_mm"), "duplicates": c.get("duplicates"),
                        "pinholes": c.get("pinholes"), "left_open": c.get("left_open"),
                        "left_widest_mm": c.get("left_widest_mm"), "refused": c.get("refused"),
                        "quote": getattr(sewing, "quote", ""), "holes_quote": getattr(sewing, "holes_quote", "")},
             "words": " ".join(did) or "Nothing in the surface needed closing."}]


def main(argv: list[str] | None = None) -> int:
    """python -m meshpipeline.engines.surface_run <workspace> <staged file> <mm per unit | staged> [<JSON: thickness_mm,
    others_mm, unread, said_shell, target, thickness_how, stitch, sew_mm, holes_mm>] (the mesh requirements read from
    the notes are the workspace's own file, as for engines/shell_run)"""
    args = list(sys.argv[1:] if argv is None else argv)
    more: dict = {}
    if len(args) == 4:
        try:
            got = json.loads(args.pop())
            more = got if isinstance(got, dict) else {}
        except ValueError:
            more = {}
    if len(args) != 3:
        print(main.__doc__, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING)
    ws = Path(args[0])
    if args[2] == "staged":
        from meshpipeline.engines.fea_driver import staged_mm_per_unit

        try:
            mm = staged_mm_per_unit(Path(args[1]))
        except Exception as exc:  # noqa: BLE001 - said in the record
            rec = {"made": False, "surface": {"kind": "unread"},
                   "why": f"the staged file could not be read ({type(exc).__name__})"}
            (ws / "surface_run.json").write_text(json.dumps(rec, indent=1))
            return 0
    else:
        mm = float(args[2])
    t = more.get("thickness_mm")
    sew_mm, holes_mm = more.get("sew_mm"), more.get("holes_mm")
    run(ws, args[1], mm_per_unit=mm, thickness_mm=float(t) if t else None, source=Path(args[1]).name,
        target=more.get("target") if isinstance(more.get("target"), dict) else None,
        said_shell=bool(more.get("said_shell")), others_mm=list(more.get("others_mm") or []),
        unread=str(more.get("unread") or ""), stitch=more.get("stitch") is not False,
        sew_mm=float(sew_mm) if sew_mm else None, holes_mm=float(holes_mm) if holes_mm else None,
        requirements=requirements_in(ws), mend=more.get("mend") is not False,
        thickness_how=more.get("thickness_how") if isinstance(more.get("thickness_how"), dict) else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["GATE", "NO_THICKNESS", "NO_THICKNESS_YET", "READING", "SIZING_AGAIN", "SIZING_DIVISIONS", "clean_words",
           "deck_notes", "gap_words", "is_surface", "line", "main", "read", "requirements_in", "run", "said_shell",
           "sizing_wall", "stitch_for", "surface_pass", "thickness_for", "words"]
