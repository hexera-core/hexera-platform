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
# Collaborates with: engines/fea_driver.py (calls surface_pass, cites it), cad/surface_model.py, engines/shell_run.py,
# engines/shell_delivery.py, contracts/shell_thickness.py.
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
    said += clean_words(s.get("cleaned"))
    if rec.get("open_edges"):
        said.append(open_words(int(rec["open_edges"]), s.get("cleaned")))
    if wider_words(s.get("cleaned")):
        said.append(wider_words(s.get("cleaned")))
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
    return " ".join(said)


def _gap(mm: float) -> str:
    """A gap as it is said: "0.12 mm", "0.004 mm"."""
    return f"{float(mm):.2g} mm"


def clean_words(cleaned: dict | None) -> list[str]:
    """SURF-8..10: WHAT THE CLEAN-UP DID, one short sentence each, nothing when it did nothing."""
    c = cleaned or {}
    out = []
    if c.get("duplicates"):
        n = int(c["duplicates"])
        out.append(f"{n} face(s) were in the file twice: the second copy of each is left out.")
    if c.get("closed_edges"):
        asked = f", as your notes asked (up to {_gap(c['limit_mm'])})" if c.get("limit_from") == "notes" else ""
        out.append(f"Closed {int(c['closed_edges'])} gap(s) between the surfaces, the widest "
                   f"{_gap(c.get('closed_widest_mm') or 0.0)}{asked}.")
    if c.get("pinholes"):
        asked = ", as your notes asked" if c.get("holes_from") == "notes" else ""
        what = "hole(s)" if c.get("holes_from") == "notes" else "pinhole(s)"
        out.append(f"Filled {int(c['pinholes'])} {what}, the largest {_gap(c.get('pinhole_widest_mm') or 0.0)} "
                   f"across{asked}.")
    if c.get("stitch") == "off":
        out.append("Nothing was stitched, as your notes asked.")
    return out


def open_words(n: int, cleaned: dict | None) -> str:
    """SURF-2 / SURF-8: the gaps LEFT OPEN, said with their size and the way to close them."""
    c = cleaned or {}
    said = f"The surfaces do not meet at {n} edge(s): the shell has gaps there"
    widest = float(c.get("left_widest_mm") or 0.0)
    if widest > 0:
        said += f" (the widest {_gap(widest)})"
    said += "."
    if c.get("refused"):
        said += f" They were left open because {c['refused']}."
    elif c.get("stitch") != "off" and widest > 0 and not c.get("wider_gaps"):
        said += f' To close them, add to your notes: "stitch gaps up to {_ask(widest):g} mm".'
    return said


def _ask(widest_mm: float) -> float:
    """The tolerance that closes a gap this wide: a tenth over it, rounded up to two digits."""
    import math

    v = 1.1 * float(widest_mm)
    if v <= 0:
        return 0.0
    step = 10.0 ** (math.floor(math.log10(v)) - 1)
    return float(f"{math.ceil(v / step) * step:.2g}")


def wider_words(cleaned: dict | None) -> str:
    """SURF-8: edges that run side by side WIDER APART than the gaps closed by rule: found, not closed, and said -
    with the note that closes them (they may be meant to be apart: only the user knows)."""
    c = cleaned or {}
    n = int(c.get("wider_gaps") or 0)
    if not n or c.get("stitch") == "off":
        return ""
    widest = max(float(c.get("wider_widest_mm") or 0.0), float(c.get("left_widest_mm") or 0.0))
    return (f"{n} pair(s) of edges run side by side up to {_gap(float(c.get('wider_widest_mm') or 0.0))} apart, wider "
            f"than the {_gap(float(c.get('limit_mm') or 0.0))} closed here. If they should meet, add to your notes: "
            f'"stitch gaps up to {_ask(widest):g} mm".')


def stitch_for(state):
    """SURF-8 / SURF-10: what the user's words say about the clean-up (contracts/surface_stitch): a stitch tolerance,
    "do not stitch", a hole size - the latest word standing, a note over the intake's summary."""
    from meshpipeline.contracts import shell_thickness as st
    from meshpipeline.contracts import surface_stitch as ss

    return ss.read(st.texts_of(state))


def requirements_in(workspace: Path | str) -> dict | None:
    """NOTE-7..9: the mesh requirements the FEA driver read from the user's notes and left in the workspace (a global
    size, edge lengths, quality limits, the element kind), for the shell mesher. None when nothing was asked - or
    when the notes' requirements reader (engines/mesh_requirements) is not in this build."""
    try:
        from meshpipeline.engines import mesh_requirements as MR
    except ImportError:
        return None
    try:
        return MR.requirements_of(MR.read(workspace))
    except Exception as exc:  # noqa: BLE001 - no requirements read is no requirements
        logger.warning("surface run: the notes' mesh requirements were not read (%s)", type(exc).__name__)
        return None


def _mesh(out: Path, *, mm_per_unit: float, target: dict | None, requirements: dict | None):
    """The shell mesher on the record in `out`, with the user's count and (when given) their notes' requirements."""
    from meshpipeline.engines.gmsh import shell_adapter as sa

    if requirements:
        return sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
    return sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target)


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
        requirements: dict | None = None) -> dict:
    """Read `step` (coordinates `mm_per_unit` mm each); when it is a surface, sew it, mesh it as a shell and write
    every file a shell run writes into `workspace`. Returns the run's record, shell_run.json's shape plus "surface":
    {"kind", "why", "thickness_mm", "thickness_set", "sizing_wall_mm", "sheets", "faces", "open_share", ...}.
    `thickness_mm`: the user's (None: not given); `said_shell`: their words asked for a shell (SURF-1). `stitch`
    False, `sew_mm`, `holes_mm`: what their notes say of the clean-up (SURF-8, SURF-10). `target`: the element count
    they asked for (SHMESH-23); `requirements`: the mesh requirements read from their notes (NOTE-7..9)."""
    import numpy as np

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm
    from meshpipeline.cad.scout import read_cad
    from meshpipeline.engines import shell_run
    from meshpipeline.engines import shell_writers as sw

    ws = Path(workspace)
    started = time.time()
    rec: dict = {"made": False, "mm_per_unit": float(mm_per_unit), "surface": {"kind": "unread"}}
    try:
        wall = float(thickness_mm or 0.0)
        given = wall > 0
        reading, sewn = sm.read(read_cad(Path(step)), mm_per_unit=mm_per_unit, said_shell=said_shell or given,
                                stitch=stitch, sew_mm=sew_mm, holes_mm=holes_mm)
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
            walls = {s["part"]: (wall if given else sizing_wall(s["size_mm"])) for s in sheets}
            if not given:
                rec["surface"]["sizing_wall_mm"] = sorted(set(walls.values()))
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
            if not given and _past(applied):
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
            rec["mesh_s"] = round(time.time() - t1, 1)
            if not given:
                # SURF-5: the sizing wall sized the mesh; it is no thickness, and none is written
                mesh.wall = np.zeros(mesh.n_elements, dtype=float)
                mesh.node_wall, mesh.varies = None, None
            exports = sw.write_all(ws, mesh, units="m", source=source, notes=deck_notes(wall if given else None))
            faces = shell_run.write_msh(ws / shell_run.MSH, mesh)
            xyz_m = mesh.xyz / 1000.0
            q = applied.get("quality") if isinstance(applied, dict) else None
            (ws / "shell_quality.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
            rec.update(nodes=int(len(mesh.xyz)), faces=faces,
                       bounds_m=[float(v) for v in (*xyz_m.min(axis=0), *xyz_m.max(axis=0))] if len(xyz_m) else None,
                       made=all((ws / f).exists() and (ws / f).stat().st_size > 0 for f in shell_run.FILES),
                       elements=int(mesh.n_elements), quads=int(len(mesh.quads)), trias=int(len(mesh.trias)),
                       walls_mm=sorted({round(float(w), 3) for w in mesh.wall}) if given else [],
                       quality=q if isinstance(q, dict) else None,
                       exports={k: v for k, v in exports.items() if isinstance(v, dict)})
            if isinstance(applied, dict) and isinstance(applied.get("target"), dict):
                rec["target"] = applied["target"]
            if isinstance(applied, dict) and requirements:
                # NOTE-8: what was asked against what was delivered, as the shell run keeps it
                for key in ("requirements", "sizing", "count_set_aside"):
                    if applied.get(key) is not None:
                        rec[key] = applied[key]
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
    got = thickness_for(state)
    sewing = stitch_for(state)
    args = {"thickness_mm": got.mm, "others_mm": list(got.others_mm), "unread": got.unread,
            "said_shell": said_shell(state), "target": {k: target[k] for k in ("elements", "mode", "low", "high")
                                                         if k in target} if target and target.get("elements") else None,
            "stitch": not sewing.off, "sew_mm": sewing.mm, "holes_mm": sewing.holes_mm}
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
                                  "sizing_wall_mm": s.get("sizing_wall_mm")},
                       "words": (f"The wall thickness is the user's: {_mm(s['thickness_mm'])} (\"{got.quote}\")."
                                 if s.get("thickness_mm") else
                                 "No thickness was given: the mesh is sized as for a thin sheet and no thickness is "
                                 "written (SURF-5).")},
                      *_clean_cites(s.get("cleaned"), sewing),
                      {"stage": GATE, "decided": "shell mesh" if rec.get("made") else "no shell mesh",
                       "value": rec.get("elements"), "rule": "SURF-3",
                       "inputs": {k: rec.get(k) for k in ("open_edges", "free_edges", "seconds", "why", "target")},
                       "words": rec["words"]}])
    return rec


def _clean_cites(cleaned: dict | None, sewing) -> list[dict]:
    """SURF-8..10: the clean-up in the decision record - one entry, only when it did or was asked something."""
    c = cleaned or {}
    did = clean_words(c)
    if not did and not c.get("left_open") and not getattr(sewing, "given", False):
        return []
    return [{"stage": GATE, "decided": "surface cleaned", "value": int(c.get("closed_edges") or 0), "rule": "SURF-8",
             "inputs": {"limit_mm": c.get("limit_mm"), "limit_from": c.get("limit_from"), "rung_mm": c.get("rung_mm"),
                        "closed_widest_mm": c.get("closed_widest_mm"), "duplicates": c.get("duplicates"),
                        "pinholes": c.get("pinholes"), "left_open": c.get("left_open"),
                        "left_widest_mm": c.get("left_widest_mm"), "refused": c.get("refused"),
                        "quote": getattr(sewing, "quote", ""), "holes_quote": getattr(sewing, "holes_quote", "")},
             "words": " ".join(did) or "Nothing in the surface needed closing."}]


def main(argv: list[str] | None = None) -> int:
    """python -m meshpipeline.engines.surface_run <workspace> <staged file> <mm per unit | staged> [<JSON: thickness_mm,
    others_mm, unread, said_shell, target, stitch, sew_mm, holes_mm>]
    (the mesh requirements read from the notes are the workspace's own file, as for engines/shell_run)"""
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
        requirements=requirements_in(ws))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["GATE", "NO_THICKNESS", "NO_THICKNESS_YET", "READING", "SIZING_AGAIN", "SIZING_DIVISIONS", "clean_words",
           "deck_notes", "is_surface", "line", "main", "open_words", "read", "requirements_in", "run", "said_shell",
           "sizing_wall", "stitch_for", "surface_pass", "thickness_for", "words"]
