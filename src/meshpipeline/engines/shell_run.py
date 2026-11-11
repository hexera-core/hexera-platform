# Responsibility: THE SHELL MESH OF AN FEA RUN (SHSTAGE-6), when the stage settled "Mesh as: Shell": the staged
# solid's mid-surface extracted (cad/midsurface), meshed (engines/gmsh/shell_adapter.mesh_run) and written for
# every solver (engines/shell_writers: Nastran .bdf, CalculiX and Abaqus .inp, Ansys .cdb) beside the solid deck,
# with a short record of what was made (shell_run.json). Run in a process of its own (main()): gmsh and
# OpenCASCADE can crash, and a crash costs the shell files, never the run.
# Boundaries: deterministic, no model call, no store. Writes only into the workspace it is given, under shell_*
# names the solid path never touches. Never raises from run(): a shell that cannot be made says why.
# Collaborates with: engines/fea_driver.py (runs it, cites it), engines/fea_writers.EXPORT_ARTIFACTS (offers the
# files as downloads), engines/gmsh/spec.py (puts them in the bundle).
from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RECORD = "shell_run.json"
FOLDER = "_shell"
#: the files a shell run leaves in the workspace, in the order the download offers them (Nastran first)
FILES = ("shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb")


def _verdicts(record: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in record.get("bodies") or []:
        v = str(b.get("verdict") or "solid")
        out[v] = out.get(v, 0) + 1
    return out


#: what a shell run with no shell says when the extraction gave no measured reason
NO_WALL_WHY = "the part has no plate or thin wall to make a shell from"


def no_shell_why(record: dict) -> str:
    """SHSTAGE-6b: why no shell was made, in numbers when the extraction measured them (cad/midsurface SHELL-2c:
    "its most plate-like wall is 60 mm thick and spans about 11 mm, ..."; SHELL-18: "... used its 540 s work
    budget ..."): each solid body's reason, the same reason told once, at most three."""
    whys: list[str] = []
    for b in record.get("bodies") or []:
        w = str(b.get("why") or "").strip()
        if str(b.get("verdict") or "solid") != "solid" or not w:
            continue
        w = w.split(": ", 1)[1].strip() if w.startswith(("no wall of it is a plate: ", "no wall is thin against its size: ")) else w
        if w not in whys:
            whys.append(w)
    if not whys:
        return NO_WALL_WHY
    if len(whys) == 1:
        return whys[0]
    return "; ".join(whys[:3]) + ("; and more" if len(whys) > 3 else "")


def words(rec: dict) -> str:
    """What the shell run made, in plain words, for the decision record and the result."""
    if not rec.get("made"):
        return (f"No shell mesh was made: {rec.get('why') or 'it could not be made'}. A solid mesh is delivered "
                "instead.")
    n, q, t = int(rec.get("elements") or 0), int(rec.get("quads") or 0), int(rec.get("trias") or 0)
    said = f"Shell mesh: {n:,} elements ({q:,} quads, {t:,} triangles) on the mid-surface"
    walls = rec.get("walls_mm") or []
    if walls:
        said += f", walls {min(walls):.3g} to {max(walls):.3g} mm" if len(walls) > 1 else f", wall {walls[0]:.3g} mm"
    said += ". Files: Nastran .bdf, CalculiX and Abaqus .inp, Ansys .cdb."
    if rec.get("patches"):
        # MIXED-9: the regions that cannot be a shell are in the files as solid, tied to the shell
        from meshpipeline.engines.solid_patches import left_words

        said += f" {rec['patch_words']}"
        if rec.get("left"):
            said += f" {left_words(rec['left'])}"
    elif rec.get("solid_bodies") and rec.get("shell_only"):
        said += (f" {rec['solid_bodies']} part(s) or region(s) stay solid and are not in the shell files: your notes "
                 "asked for a shell only.")
    elif rec.get("solid_bodies") and rec.get("patch_why"):
        said += (f" {rec['solid_bodies']} part(s) or region(s) stay solid and are not in the shell files: they could "
                 f"not be meshed as solid and tied to the shell ({rec['patch_why']}).")
    elif rec.get("solid_bodies"):
        said += f" {rec['solid_bodies']} part(s) or region(s) stay solid and are not in the shell files."
    if rec.get("open_edges"):
        said += f" The plates do not meet at {rec['open_edges']} edge(s): the shell has gaps there."
    if rec.get("advice"):
        said += f" {rec['advice']}"       # SHELL-23: walls short for their thickness
    from meshpipeline.engines.shell_plan import count_words

    counted = count_words(rec.get("target"))
    if counted:
        said += " " + counted
    if rec.get("count_set_aside"):
        said += (f" Your element count ({int(rec['count_set_aside']):,}) was not used: the global size you gave "
                 "decides the sizes.")
    return said


#: the shell mesh as the result viewer reads it (gmsh MSH 2.2, metres, one physical group per mid-face)
MSH = "shell.msh"


def write_msh(path: Path, mesh) -> list[dict]:
    """The shell mesh as a gmsh MSH 2.2 ASCII file in metres: 4-node quads and 3-node triangles, each mid-face its own
    surface entity and physical group (k, named Mid_k), so the viewer draws one part per mid-face. Returns the faces:
    [{"tag", "name", "wall_mm", "elements"}] in tag order."""
    import numpy as np

    mids = sorted(set(mesh.mid), key=lambda m: (len(m), m))
    tag_of = {m: k for k, m in enumerate(mids, start=1)}
    walls: dict[str, list[float]] = {}
    lines = ["$MeshFormat", "2.2 0 8", "$EndMeshFormat", "$PhysicalNames", str(len(mids))]
    lines += [f'2 {tag_of[m]} "{m}"' for m in mids]
    lines += ["$EndPhysicalNames", "$Nodes", str(len(mesh.xyz))]
    xyz = np.asarray(mesh.xyz, dtype=float) / 1000.0
    lines += [f"{i} {x:.9g} {y:.9g} {z:.9g}" for i, (x, y, z) in enumerate(xyz, start=1)]
    lines += ["$EndNodes", "$Elements", str(mesh.n_elements)]
    for e, conn in enumerate(mesh.elements()):
        m = mesh.mid[e]
        k = tag_of[m]
        walls.setdefault(m, []).append(float(mesh.wall[e]))
        kind = 3 if len(conn) == 4 else 2
        lines.append(f"{e + 1} {kind} 2 {k} {k} " + " ".join(str(int(n) + 1) for n in conn))
    lines.append("$EndElements")
    Path(path).write_text("\n".join(lines) + "\n")
    return [{"tag": tag_of[m], "name": m, "elements": len(walls.get(m) or []),
             "wall_mm": round(float(np.median(walls[m])), 4) if walls.get(m) else None} for m in mids]


def _patched(ws: Path, out: Path, step, record: dict, rec: dict, *, mm_per_unit: float, target: dict | None,
             requirements: dict | None):
    """MIXED-9: the shell with its solid patches (engines/solid_patches), or None - nothing to patch, the notes ask
    for a shell only (said: rec["shell_only"]), or the patches could not be made (said: rec["patch_why"]); the
    shell is then made as before."""
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines import solid_patches as SP

    if not SP.solid_rows(record):
        return None
    if SP.shell_only(requirements):
        rec["shell_only"] = True
        return None
    t1 = time.time()
    try:
        return SP.build(step, out, record, mm_per_unit=mm_per_unit, requirements=requirements, target=target,
                        requirements_file=ws / MR.FILE)
    except SP.Stop as stop:
        rec["patch_why"] = str(stop)
    except Exception as exc:  # noqa: BLE001 - the shell is still made, as before; the reason is kept
        logger.warning("shell run: no solid patches (%s: %s)", type(exc).__name__, str(exc)[:300])
        rec["patch_why"] = f"the solid patches could not be made ({type(exc).__name__})"
    finally:
        rec["patch_s"] = round(time.time() - t1, 1)
    return None


def run(workspace: Path | str, step: Path | str, *, mm_per_unit: float, source: str = "",
        target: dict | None = None, requirements: dict | None = None) -> dict:
    """Extract, mesh and write the shell of `step` (coordinates `mm_per_unit` mm each) into `workspace`. `target`: the
    element count the user asked for (engines/fea_driver.element_target), which the shell's sizes follow (SHMESH-23).
    `requirements`: the mesh requirements read from the user's notes (engines/mesh_requirements): their sizes, limits
    and element kind are applied (SHMESH-24, 25) and each is told asked against delivered (SHMESH-26, the record's
    "requirements"). None: the rules' own shell, exactly as before."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines.gmsh import shell_adapter as sa

    ws = Path(workspace)
    started = time.time()
    rec: dict = {"made": False, "mm_per_unit": float(mm_per_unit)}
    try:
        out = ws / FOLDER
        out.mkdir(parents=True, exist_ok=True)
        record = ms.extract_file(step, out, mm_per_unit=mm_per_unit)
        verdicts = _verdicts(record)
        rec.update(bodies=verdicts, extract_s=round(time.time() - started, 1), advice=str(record.get("advice") or ""),
                   open_edges=int(sum(int(r.get("open_edges") or 0) for r in record.get("sewn") or [])),
                   solid_bodies=int(verdicts.get("solid", 0)) + sum(len(b.get("solid_regions") or [])
                                                                    for b in record.get("bodies") or []))
        if not record.get("surface_faces"):
            rec["why"] = no_shell_why(record)
        else:
            t1 = time.time()
            patched = _patched(ws, out, step, record, rec, mm_per_unit=mm_per_unit, target=target,
                               requirements=requirements) if rec["solid_bodies"] else None
            if patched is not None:
                # MIXED-9: the shell and its solid patches, tied, in the shell's own files
                from meshpipeline.engines import mixed_mesh as mm
                from meshpipeline.engines import shell_quality as SQ
                from meshpipeline.engines import solid_patches as SP

                mesh, applied = patched.joined.shell, patched.applied
                (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
                exports = mm.write_all(ws, patched.joined, units="m", source=source, stem="shell")
                faces = SP.write_msh(ws / MSH, patched.joined, patched.patches)
                # the shell's figures on the delivered shell elements, the overlap rows of the join among them
                applied = {**applied, "quality": {**dict(applied.get("quality") or {}), **SQ.measure(mesh)}}
                rec.update(SP.record_of(patched, bodies=len(record.get("bodies") or [])))
                rec["mesh_s"] = round(time.time() - t1, 1)
                import numpy as np

                xyz_m = np.vstack([mesh.xyz, patched.joined.solid.xyz]) / 1000.0
            else:
                t1 = time.time()
                mesh, applied = sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
                rec["mesh_s"] = round(time.time() - t1, 1)
                exports = sw.write_all(ws, mesh, units="m", source=source)
                # THE SHELL IS THE RESULT (engines/shell_delivery): the viewer's picture of it, one group per mid-face
                faces = write_msh(ws / MSH, mesh)
                xyz_m = mesh.xyz / 1000.0
            rec.update(nodes=int(len(xyz_m)), faces=faces,
                       bounds_m=[float(v) for v in (*xyz_m.min(axis=0), *xyz_m.max(axis=0))] if len(xyz_m) else None)
            q = applied.get("quality") if isinstance(applied, dict) else None
            (ws / "shell_quality.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
            rec.update(made=all((ws / f).exists() and (ws / f).stat().st_size > 0 for f in FILES),
                       elements=int(mesh.n_elements), quads=int(len(mesh.quads)), trias=int(len(mesh.trias)),
                       walls_mm=sorted({round(float(w), 3) for w in mesh.wall}),
                       quality=q if isinstance(q, dict) else None, exports={k: v for k, v in exports.items()
                                                                            if isinstance(v, dict)})
            if isinstance(applied, dict) and isinstance(applied.get("target"), dict):
                rec["target"] = applied["target"]
            if isinstance(applied, dict) and requirements:
                for key in ("requirements", "sizing", "count_set_aside"):
                    if applied.get(key):
                        rec[key] = applied[key]
            if not rec["made"]:
                rec["why"] = "the solver files could not all be written"
        for name in (ms.RECORD_FILE, ms.SURFACE_FILE):
            if (out / name).exists():
                shutil.copyfile(out / name, ws / (name if name != ms.RECORD_FILE else "shell.json"))
    except Exception as exc:  # noqa: BLE001 - the solid mesh is still delivered; the reason is kept
        logger.warning("shell run: no shell mesh (%s: %s)", type(exc).__name__, str(exc)[:300])
        rec.update(made=False, why=f"the shell mesh could not be made ({type(exc).__name__})")
    rec["seconds"] = round(time.time() - started, 1)
    rec["words"] = words(rec)
    (ws / RECORD).write_text(json.dumps(rec, indent=1, sort_keys=True, default=float))
    return rec


def read(workspace: Path | str) -> dict:
    """The shell run's record, {} when none ran."""
    try:
        got = json.loads((Path(workspace) / RECORD).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def requirements_in(workspace: Path | str) -> dict | None:
    """The mesh requirements the FEA driver read from the user's notes and left in the workspace
    (engines/mesh_requirements.FILE): the checked set, or None when there is none (no notes, nothing asked)."""
    from meshpipeline.engines import mesh_requirements as MR

    return MR.requirements_of(MR.read(workspace))


def main(argv: list[str] | None = None) -> int:
    """python -m meshpipeline.engines.shell_run <workspace> <staged step> <mm per unit | staged> [<count JSON>]
    ("staged": read as the FEA driver reads the staged solid, engines/fea_driver.staged_mm_per_unit; the count is
    fea_driver.element_target's, SHMESH-23; the requirements read from the notes are the workspace's own file)"""
    args = list(sys.argv[1:] if argv is None else argv)
    target = None
    if len(args) == 4:
        try:
            got = json.loads(args.pop())
            target = got if isinstance(got, dict) else None
        except ValueError:
            target = None
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
            return 0
    else:
        mm = float(args[2])
    run(args[0], args[1], mm_per_unit=mm, source=Path(args[1]).name, target=target,
        requirements=requirements_in(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FILES", "MSH", "RECORD", "main", "read", "requirements_in", "run", "words", "write_msh"]
