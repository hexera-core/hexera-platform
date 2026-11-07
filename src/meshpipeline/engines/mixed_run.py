# Responsibility: THE MIXED MESH OF AN FEA RUN (MIXED-4..6, placeholders): whether a part is recommended "Mixed"
# (thin walls as shell, thick regions as solid, joined), and, when the stage settles Mixed, the run itself - the
# part's mid-surface (cad/midsurface), the split (cad/mixed_split), the shell mesh of the trimmed mid-faces
# (engines/gmsh/shell_adapter), the solid region meshed by the FEA path's own plan and adapter (engines/fea_driver,
# engines/gmsh/fea_adapter), the join (engines/mixed_mesh) and every solver's file - with a short record of what was
# made (mixed_run.json). Run in a process of its own (main()), as engines/shell_run is: a crash costs the mixed
# files, never the run.
# Boundaries: deterministic, no model call, no store. Writes only into the workspace it is given, under mixed_*
# names and its own folder. Never raises from run(): a mixed mesh that cannot be made says why, and the solid mesh
# is delivered.
# Collaborates with: application/shell_check.py and engines/shell_run.py (the stage's Auto / Shell / Solid, design
# PR 4 #377, which adds Mixed by calling recommend() and run()), cad/mixed_split.py, engines/mixed_mesh.py.
"""MIXED: SHELL WHERE THIN, SOLID WHERE THICK.

MIXED-4, AUTO RECOMMENDS MIXED when the part is one body the shell check calls mixed (thin walls and thick regions,
cad/midsurface 1.4), its mid-surfaces meet everywhere (no open edge), and its thin walls hold at least
MIXED_AUTO_SHARE of its metal (the sewn mid-faces times their walls, over its volume): the shell then takes most of
the part, and the mesh is many times smaller than a solid one. A part under that share is meshed solid: the joins
would cost more than the shell saves. Several bodies are meshed solid in this version (a mixed assembly's bonded
joins across bodies are PR 3's next step), said so.

MIXED-5, THE RUN: the split (MIXED-1..3) must hold; the shell mesh must be valid; every link node must be tied
(SHLINK-4). Any of those failing, no mixed files are written and the record says why.

MIXED-6, THE WORDS: "Thin walls meshed as shell (78% of the part, 2 mm thick); 3 thick regions kept solid and
joined to them."

MIXED-8, WALLS TOO THICK FOR SHELLS: a shell wall is meshed as shell only where shell theory holds - its farthest
point at least cad/mixed_split.REACH_WALLS (10) of its walls from its nearest joint or thick region (wall_reach). A
shell wall bends from the middle of its joint, half the other wall's thickness from where the solid wall starts to
bend, and the 3D stress round a joint reaches about a wall into it; over a short reach both are a large share of the
length the wall bends over. A wall that reaches less is kept solid with the thick regions; when the walls left hold
under MIXED_AUTO_SHARE of the part's metal, the part is meshed solid: "Walls too thick for shells: ...". Measured
(2 Nov, against a converged solid): a 4 mm box reaching 5 to 6.7 walls read its back wall +23%, a casting's 3 mm
ribs reaching 6.7 walls a mode -6.8%; the L bracket's plate, reaching 11 walls, +0.9%.
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RECORD = "mixed_run.json"
FOLDER = "_mixed"
#: the files a mixed run leaves in the workspace, Nastran first
FILES = ("mixed.bdf", "mixed.inp", "mixed_abaqus.inp", "mixed.cdb")
#: MIXED-4: the least share of the part's metal its thin walls must hold for Auto to recommend Mixed
MIXED_AUTO_SHARE = 0.5
#: MIXED-7: in a mixed part every shell wall at least a tenth of its body's largest sheet wide is a panel
#: (engines/shell_plan SHMESH-15, PANEL_ACROSS elements across), however few walls wide it is. A thick region
#: stiffens the wall round it, so its free modes bend between the region and the wall's edges, in half-waves a
#: fraction of the wall's width: a 2 mm plate 18 walls wide round a boss (just short of a panel) was meshed 7 across
#: and its modes 7-12 came out up to 6.8% stiff of a converged solid mesh
MIXED_PANEL_WALLS = 0.0
MIXED = "mixed"
WHY = "Thin walls meshed as shell ({share}% of the part, {thin}); {n} thick region{s} kept solid and joined to them."
WHY_LOW = ("Only {share}% of this part is thin wall; the rest is thick, so a shell-and-solid mesh saves little. "
           "It is meshed as a solid.")
WHY_OPEN = "Its mid-surfaces do not meet at {n} place{s}, so the shell would have gaps. It is meshed as a solid."
WHY_BODIES = "It has {n} bodies; a shell-and-solid mesh is made for one body for now. It is meshed as a solid."
#: MIXED-8 in words: too little shell is left once the walls too thick for shells are kept solid
WHY_THICK = ("Walls too thick for shells: {n} of its {m} thin walls reach less than {k:g} times their thickness from "
             "the nearest joint or thick region (the shortest, {t} thick, reaches {r}: {x:.1f} times). A shell that "
             "short bends too much at its joint, so the part is meshed as a solid.")
#: MIXED-8 in words, when enough shell is left: those walls are kept solid with the thick regions
WHY_KEPT = " {n} wall{s} too thick for shells (under {k:g} times {its} thickness from a joint) kept solid with them."
#: MIXED-8 in words, when the walls kept solid leave a split that cannot be made
WHY_THICK_SPLIT = ("Walls too thick for shells: {n} of its {m} thin walls reach less than {k:g} times their thickness "
                   "from the nearest joint or thick region, and with them kept solid the rest could not be cut apart "
                   "({why}). It is meshed as a solid.")


def _mm(v: float) -> str:
    return f"{v:.2g} mm" if v < 1 else f"{v:.3g} mm"


def thin_share(record: dict) -> float:
    """The share of the part's metal its thin walls hold: the sewn mid-faces times their walls over its volume
    (cad/midsurface's mid_volume_ratio, a body's), the bodies together."""
    vol = sum(float(b.get("volume_mm3") or 0.0) for b in record.get("bodies") or [])
    held = sum(float(b.get("mid_volume_ratio") or 0.0) * float(b.get("volume_mm3") or 0.0)
               for b in record.get("bodies") or [])
    return max(0.0, min(1.0, held / vol)) if vol > 0 else 0.0


def too_thick(record: dict, reach: dict) -> tuple[list[str], float, str]:
    """MIXED-8: (the shell walls too thick for shells, the share of the part's metal the other shell walls hold, the
    stop in words - "" when enough shell is left). `reach`: each wall's reach in mm (reach_of). A wall is a shell
    where shell theory holds: reaching at least REACH_WALLS of its walls from its nearest joint or thick region
    (cad/mixed_split.wall_reach). Too thick, it is kept solid with the thick regions; when the walls left as shell
    hold under MIXED_AUTO_SHARE of the part (or none is left), the part is meshed solid."""
    from meshpipeline.cad import mixed_split as mx

    shell = [m for m in record.get("mids") or [] if m.get("shell") and m["id"] in reach]
    bad = sorted((m for m in shell if float(reach[m["id"]]) < mx.REACH_WALLS * float(m["thickness_mm"])),
                 key=lambda m: (float(reach[m["id"]]) / float(m["thickness_mm"]), m["id"]))
    metal = {m["id"]: float(m.get("area_mm2") or 0.0) * float(m["thickness_mm"]) for m in shell}
    held = sum(metal.values())
    left = held - sum(metal[m["id"]] for m in bad)
    share = thin_share(record) * (left / held) if held > 0 else 0.0
    if not bad or (share >= MIXED_AUTO_SHARE and left > 0):
        return [m["id"] for m in bad], share, ""
    w = bad[0]
    t, r = float(w["thickness_mm"]), float(reach[w["id"]])
    return [m["id"] for m in bad], share, WHY_THICK.format(n=len(bad), m=len(shell), k=mx.REACH_WALLS, t=_mm(t),
                                                           r=_mm(r), x=r / t)


def recommend(record: dict, reach: dict | None = None) -> tuple[bool, str]:
    """MIXED-4 / MIXED-6 / MIXED-8: (Auto recommends Mixed, the reason in plain words). False with "" when the part is
    not mixed at all (the shell / solid rules speak for it). `reach`: each shell wall's reach in mm (reach_of); MIXED-8
    is read only when it is given."""
    bodies = record.get("bodies") or []
    verdicts = [str(b.get("verdict") or "solid") for b in bodies]
    if MIXED not in verdicts:
        return False, ""
    if len(bodies) > 1:
        return False, WHY_BODIES.format(n=len(bodies))
    opened = sum(int(r.get("open_edges") or 0) for r in record.get("sewn") or [])
    if opened:
        return False, WHY_OPEN.format(n=opened, s="" if opened == 1 else "s")
    share = thin_share(record)
    bad: list[str] = []
    if reach is not None:
        # walls too thick for shells said first: the reason the user can see in the part
        bad, kept_share, stop = too_thick(record, reach)
        if stop:
            return False, stop
        share = kept_share if bad else share
    pct = max(0, min(100, int(round(100 * share))))
    if share < MIXED_AUTO_SHARE:
        return False, WHY_LOW.format(share=pct)
    walls = sorted({float(m["thickness_mm"]) for m in record.get("mids") or [] if m.get("shell") and m.get("id") not in bad})
    thin = (f"{_mm(walls[0])} thick" if len(walls) == 1 or walls[-1] - walls[0] <= 0.02 * walls[-1]
            else f"{_mm(walls[0])} to {_mm(walls[-1])} thick") if walls else "thin"
    n = len(bodies[0].get("solid_regions") or []) or 1
    said = WHY.format(share=pct, thin=thin, n=n, s="" if n == 1 else "s")
    if bad:
        from meshpipeline.cad import mixed_split as mx

        said += WHY_KEPT.format(n=len(bad), s="" if len(bad) == 1 else "s", k=mx.REACH_WALLS,
                                its="its" if len(bad) == 1 else "their")
    return True, said


def kept_solid(record: dict, kept: list, thick: list, bad: list[str]) -> tuple[list[int], list, list]:
    """MIXED-8: the split's input with the walls too thick for shells (`bad`, mid ids) kept solid: (the sewn faces
    still shell, by index; those faces as cad/mixed_split takes them; the faces that mark thick regions - the thick
    faces and the too-thick walls' own mid-faces, which lie in their metal). With none too thick, as it was."""
    owners = record["surface_faces"]
    order = [k for k, o in enumerate(owners) if o not in bad]
    return order, [kept[k] for k in order], list(thick) + [kept[k][0] for k, o in enumerate(owners) if o in bad]


def reach_of(record: dict, body, surface, mm_per_unit: float) -> dict:
    """MIXED-8: each shell wall's reach in mm, {mid id: mm} (cad/mixed_split.wall_reach): `body` the part's one solid,
    `surface` the extraction's sewn mid-surface (cad/midsurface SURFACE_FILE), both in the part's own units."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    mids = {m["id"]: m for m in record.get("mids") or []}
    faces = ms._faces(surface)
    owners = list(record.get("surface_faces") or [])
    if not faces or len(faces) != len(owners):
        return {}
    walls = [float(mids[o]["thickness_mm"]) / mm_per_unit for o in owners]
    offsets = [float(mids[o].get("offset_mm", 0.0)) / mm_per_unit for o in owners]
    got = mx.wall_reach(faces, owners, walls, body, offsets)
    return {o: float(r) * mm_per_unit for o, r in sorted(got.items())}


def words(rec: dict) -> str:
    """What the mixed run made, in plain words."""
    if not rec.get("made"):
        return f"No shell-and-solid mesh was made: {rec.get('why') or 'it could not be made'}. The solid mesh is delivered."
    c = rec.get("counts") or {}
    said = (f"Shell-and-solid mesh: {int(c.get('shell_elements', 0)):,} shell elements on the thin walls and "
            f"{int(c.get('solid_elements', 0)):,} quadratic tetrahedra in {rec.get('regions', 1)} thick "
            f"region{'s' if rec.get('regions', 1) != 1 else ''}, joined by {int(c.get('links', 0))} moment-carrying "
            "link(s). Files: Nastran .bdf, CalculiX and Abaqus .inp, Ansys .cdb.")
    n = len(rec.get("too_thick") or [])
    if n:
        said += f" {n} wall{'' if n == 1 else 's'} too thick for shells {'is' if n == 1 else 'are'} in the solid part."
    return said


def _solid_mesh(shape, ws: Path):
    """The solid region through the FEA path's own measure, plan and adapter (no AI pass, no ladder): mesh.inp."""
    import gmsh
    from OCP.BRepTools import BRepTools

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines.gmsh import fea_adapter

    ws.mkdir(parents=True, exist_ok=True)
    BRepTools.Write_s(shape, str(ws / "region.brep"))
    mine = not gmsh.isInitialized()
    if mine:
        gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("mixed_region")
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)       # staged in metres, as a run stages it
        gmsh.model.occ.importShapes(str(ws / "region.brep"))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.model.remove()
    finally:
        if mine:
            gmsh.finalize()
    record, surface, diag_m = FD.measure_staged(ws)
    plan = FD.choose_plan(record, surface, diag_m, levers=[], ceiling_s=FD.ceiling_for(None))
    FD.write_plan(ws, plan)
    rc = fea_adapter.mesh(ws)
    if rc != 0 or not (ws / "mesh.inp").exists():
        raise ValueError(f"the solid region could not be meshed (exit {rc})")
    return mm.read_solid_deck(ws / "mesh.inp"), json.loads((ws / "quality.json").read_text())


def run(workspace: Path | str, step: Path | str, *, mm_per_unit: float, source: str = "") -> dict:
    """Extract, split, mesh, join and write the mixed mesh of `step` (coordinates `mm_per_unit` mm each)."""
    from meshpipeline.engines import mixed_mesh as mm

    ws = Path(workspace)
    started = time.time()
    rec: dict = {"made": False, "mm_per_unit": float(mm_per_unit)}
    try:
        joined = build(step, ws / FOLDER, mm_per_unit=mm_per_unit, rec=rec)
        exports = mm.write_all(ws, joined, units="m", source=source)
        rec.update(made=all((ws / f).exists() and (ws / f).stat().st_size > 0 for f in FILES),
                   counts=joined.counts, exports={k: v for k, v in exports.items() if isinstance(v, dict)})
    except _Stop as stop:
        rec.update(made=False, why=str(stop) or "the part is not one body of thin walls and thick regions")
    except Exception as exc:  # noqa: BLE001 - the solid mesh is still delivered; the reason is kept
        logger.warning("mixed run: no mixed mesh (%s: %s)", type(exc).__name__, str(exc)[:300])
        rec.update(made=False, why=f"the shell-and-solid mesh could not be made ({type(exc).__name__}: {str(exc)[:200]})")
    rec["seconds"] = round(time.time() - started, 1)
    rec["words"] = words(rec)
    (ws / RECORD).write_text(json.dumps(rec, indent=1, sort_keys=True, default=float))
    return rec


def build(step: Path | str, out: Path, *, mm_per_unit: float, rec: dict):
    """MIXED-5 up to the join: extract, split, mesh the shell and the solid region, join them (a MixedMesh, mm).
    Fills `rec` with what it read; raises _Stop with the reason when the rules make no mixed mesh."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx
    from meshpipeline.cad.scout import read_cad
    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines.gmsh import shell_adapter as sa

    started = time.time()
    out.mkdir(parents=True, exist_ok=True)
    record = ms.extract_file(step, out, mm_per_unit=mm_per_unit)
    ok, why = recommend(record)
    rec.update(extract_s=round(time.time() - started, 1), thin_share=round(thin_share(record), 4),
               recommended=ok, recommend_why=why)
    bodies = record.get("bodies") or []
    if len(bodies) != 1 or bodies[0].get("verdict") != MIXED:
        raise _Stop(why or "the part is not one body of thin walls and thick regions")
    if any(int(r.get("open_edges") or 0) for r in record.get("sewn") or []):
        raise _Stop(why)
    # the shape the record speaks of: an IGES file's loose surfaces sewn into its solid as the extraction sewed them
    # (SHELL-15; read raw, an IGES frame had no solid to split and the run stopped on an empty list)
    shape = ms.solidify(read_cad(step), float(mm_per_unit))[0]
    surf = TopoDS_Shape()
    BRepTools.Read_s(surf, str(out / ms.SURFACE_FILE), BRep_Builder())
    mids = {m["id"]: m for m in record["mids"]}
    kept = [(f, float(mids[o]["thickness_mm"]) / mm_per_unit, float(mids[o].get("offset_mm", 0.0)) / mm_per_unit)
            for f, o in zip(ms._faces(surf), record["surface_faces"], strict=True)]
    faces = ms._faces(shape)
    thick = [faces[k] for k, r in enumerate(record["faces"]) if r["role"] in ("thick", "solid")]
    solids = mx._solids(shape)
    if len(solids) != 1:
        raise _Stop(f"the part reads as {len(solids)} solids, not one closed body")
    # MIXED-8: the walls too thick for shells are kept solid, as thick regions (their mid-faces mark them); with
    # too little shell left, the part is meshed solid
    t1 = time.time()
    reach = reach_of(record, solids[0], surf, mm_per_unit)
    bad, _share, stop = too_thick(record, reach)
    ok, why = recommend(record, reach)
    rec.update(reach_s=round(time.time() - t1, 1), recommended=ok, recommend_why=why, too_thick=bad,
               reach_mm={o: round(r, 4) for o, r in reach.items()})
    if stop:
        raise _Stop(stop)
    order, shell_walls, marks = kept_solid(record, kept, thick, bad)
    split = mx.split_body(solids[0], shell_walls, marks, scale=mm_per_unit)
    rec["split"] = {"cores": split.cores, "cut_mm": [round(d, 4) for d in split.cut_distance],
                    "solid_share": round(split.solid_volume / split.body_volume, 4) if split.body_volume else None,
                    "notes": split.notes}
    if not split.ok:
        if bad:
            raise _Stop(WHY_THICK_SPLIT.format(n=len(bad), m=len(reach), k=mx.REACH_WALLS, why=split.why))
        raise _Stop(split.why)
    rec2 = copy.deepcopy(record)
    rec2["surface_faces"] = [record["surface_faces"][order[k]] for k in split.shell_from]
    rec2["panel_walls"] = MIXED_PANEL_WALLS
    BRepTools.Write_s(ms._compound(split.shell_faces), str(out / "shell_trimmed.brep"))
    t1 = time.time()
    shell, applied = sa.mesh_shell(out / "shell_trimmed.brep", rec2, mm_per_unit=mm_per_unit)
    rec["shell_mesh_s"] = round(time.time() - t1, 1)
    q = applied.get("quality") if isinstance(applied, dict) else {}
    if isinstance(q, dict) and q.get("valid") is False:
        raise _Stop("the shell mesh of the thin walls is not valid")
    t1 = time.time()
    solid_shape = split.solid
    if mm_per_unit != 1.0:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Pnt, gp_Trsf

        tr = gp_Trsf()
        tr.SetScale(gp_Pnt(0, 0, 0), float(mm_per_unit))
        solid_shape = BRepBuilderAPI_Transform(split.solid, tr, True).Shape()
        cut = [BRepBuilderAPI_Transform(f, tr, True).Shape() for f in split.cut_faces]
    else:
        cut = list(split.cut_faces)
    solid, sq = _solid_mesh(solid_shape, out / "solid")
    rec["solid_mesh_s"] = round(time.time() - t1, 1)
    joined = mm.join(shell, solid, cut, part=str(bodies[0]["body"]))
    if joined.notes:
        raise _Stop("; ".join(joined.notes))
    rec.update(regions=split.cores, shell_quality=q if isinstance(q, dict) else None,
               solid_quality={k: sq.get(k) for k in ("elements", "valid", "fatal") if k in sq})
    return joined


class _Stop(Exception):
    """A mixed mesh the rules do not make, with the reason in words."""


def read(workspace: Path | str) -> dict:
    """The mixed run's record, {} when none ran."""
    try:
        got = json.loads((Path(workspace) / RECORD).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    """python -m meshpipeline.engines.mixed_run <workspace> <step> <mm per unit | staged>
    ("staged": read as the FEA driver reads the staged solid, engines/fea_driver.staged_mm_per_unit, as shell_run)"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(main.__doc__, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING)
    if args[2] == "staged":
        from meshpipeline.engines.fea_driver import staged_mm_per_unit

        try:
            mm = staged_mm_per_unit(Path(args[1]))
        except Exception as exc:  # noqa: BLE001 - said in the record, the solid is still delivered
            rec = {"made": False, "why": f"the staged part could not be read ({type(exc).__name__})"}
            rec["words"] = words(rec)
            (Path(args[0]) / RECORD).write_text(json.dumps(rec, indent=1))
            return 1
    else:
        mm = float(args[2])
    rec = run(args[0], args[1], mm_per_unit=mm, source=Path(args[1]).name)
    print(rec["words"])
    return 0 if rec.get("made") else 1


__all__ = ["FILES", "MIXED_AUTO_SHARE", "RECORD", "main", "reach_of", "read", "recommend", "run", "thin_share", "too_thick",
           "words"]

if __name__ == "__main__":
    raise SystemExit(main())
