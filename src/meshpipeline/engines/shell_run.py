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
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

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


#: SHELL-26: a shell made again without its short plates, because a mesh with them could not be made (SHSTAGE-9: they
#: are left out of the mesh - no solid is made beside a shell - and counted in what it leaves out)
PLATES_OUT = ("The walls short for their thickness could not be meshed with the rest, so they are left out of this "
              "shell mesh.")
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
    kept = rec.get("kept_solid") or {}
    if kept:
        # MIXED-14: the shells and the solids named apart
        c = kept.get("counts") or {}
        said = (f"Shell and solid mesh: {n:,} elements ({q:,} quads, {t:,} triangles on the mid-surface; "
                f"{int(c.get('solid_elements') or 0):,} 10-node tetrahedra in the thick regions)")
    gone = ((rec.get("quality") or {}).get("face_watch") or {}).get("words")
    if gone:
        said = f"{gone} {said}"      # SHMESH-74: faces left out are said first
    from meshpipeline.engines.shell_delivery import cover_short, cover_words

    if cover_short(rec.get("coverage")):
        said = f"{cover_words(rec.get('coverage'))} {said}"      # MESH-1c: a shell short of the part, said first
    walls = rec.get("walls_mm") or []
    if walls:
        said += f", walls {min(walls):.3g} to {max(walls):.3g} mm" if len(walls) > 1 else f", wall {walls[0]:.3g} mm"
    said += ". Files: Nastran .bdf, CalculiX and Abaqus .inp, Ansys .cdb."
    if rec.get("kept_solid"):
        said += " " + str(rec["kept_solid"].get("words") or "")       # MIXED-12
    elif rec.get("kept_solid_why"):
        said += " " + str(rec["kept_solid_why"])                       # MIXED-12: the join failed, said
    if rec.get("left_out"):
        # SHSTAGE-9: what the mesh has no elements for, in numbers, and the way on
        from meshpipeline.cad.shell_words import left_out_words

        said += " " + left_out_words(rec["left_out"])
    elif rec.get("solid_bodies"):
        said += f" {rec['solid_bodies']} part(s) or region(s) are not in this shell mesh."
    if rec.get("open_edges"):
        said += f" The plates do not meet at {rec['open_edges']} edge(s): the shell has gaps there."
    if rec.get("advice"):
        said += f" {rec['advice']}"       # SHELL-23: walls short for their thickness
    passes = ((rec.get("quality") or {}).get("quality_passes") or {}).get("words")
    if passes:
        said += f" {passes}"              # SHMESH-100: what the extra quality passes did with the time left
    if rec.get("plates_left_out"):
        said += f" {rec['plates_left_out']}"      # SHELL-26
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


def write_msh(path: Path, mesh, solid=None) -> list[dict]:
    """The shell mesh as a gmsh MSH 2.2 ASCII file in metres: 4-node quads and 3-node triangles, each mid-face its own
    surface entity and physical group (k, named Mid_k), so the viewer draws one part per mid-face. Returns the faces:
    [{"tag", "name", "wall_mm", "elements", "part", "part_name"}] in tag order - SHWRITE-1: "part" the body the
    mid-face belongs to (Part_k) and "part_name" its name as the solver files write it (the CAD's, else Part_k).
    `solid`: MIXED-14, the thick regions kept solid (engines/mixed_mesh.SolidPart, mm) - drawn by their outer skin
    (the 10-node tetrahedra's boundary faces, as 3-node triangles) in one more group, SOLID_GROUP, listed last with
    "solid": True and "elements" its tetrahedra."""
    import numpy as np

    from meshpipeline.engines.shell_writers import part_labels

    labels = part_labels(mesh)
    part_of: dict[str, str] = {}
    for m_, p_ in zip(mesh.mid, mesh.part, strict=True):
        part_of.setdefault(m_, p_)
    mids = sorted(set(mesh.mid), key=lambda m: (len(m), m))
    tag_of = {m: k for k, m in enumerate(mids, start=1)}
    walls: dict[str, list[float]] = {}
    lines = ["$MeshFormat", "2.2 0 8", "$EndMeshFormat", "$PhysicalNames", str(len(mids))]
    lines += [f'2 {tag_of[m]} "{m}"' for m in mids]
    skin = np.zeros((0, 3), dtype=np.int64)
    s_xyz = np.zeros((0, 3))
    s_tag = len(mids) + 1
    if solid is not None and len(solid.tets):
        from meshpipeline.engines.mixed_mesh import boundary_triangles

        tri = boundary_triangles(solid.tets, np.ones(solid.n_nodes + 1, dtype=bool))[:, :3]
        used = np.unique(tri)                                  # the skin's corners (1-based solid ids)
        new = np.zeros(solid.n_nodes + 1, dtype=np.int64)
        new[used] = np.arange(len(used)) + len(mesh.xyz) + 1   # numbered after the shell's nodes
        skin, s_xyz = new[tri], np.asarray(solid.xyz, dtype=float)[used - 1]
        lines.append(f'2 {s_tag} "{SOLID_GROUP}"')
    lines[4] = str(len(mids) + (1 if len(skin) else 0))
    lines += ["$EndPhysicalNames", "$Nodes", str(len(mesh.xyz) + len(s_xyz))]
    xyz = np.vstack([np.asarray(mesh.xyz, dtype=float), s_xyz]) / 1000.0
    lines += [f"{i} {x:.9g} {y:.9g} {z:.9g}" for i, (x, y, z) in enumerate(xyz, start=1)]
    lines += ["$EndNodes", "$Elements", str(mesh.n_elements + len(skin))]
    for e, conn in enumerate(mesh.elements()):
        m = mesh.mid[e]
        k = tag_of[m]
        walls.setdefault(m, []).append(float(mesh.wall[e]))
        kind = 3 if len(conn) == 4 else 2
        lines.append(f"{e + 1} {kind} 2 {k} {k} " + " ".join(str(int(n) + 1) for n in conn))
    e0 = mesh.n_elements + 1
    lines += [f"{e0 + j} 2 2 {s_tag} {s_tag} {a} {b} {c}" for j, (a, b, c) in enumerate(skin.tolist())]
    lines.append("$EndElements")
    Path(path).write_text("\n".join(lines) + "\n")
    faces = [{"tag": tag_of[m], "name": m, "elements": len(walls.get(m) or []),
              "wall_mm": round(float(np.median(walls[m])), 4) if walls.get(m) else None,
              "part": part_of[m], "part_name": labels.get(part_of[m], part_of[m])} for m in mids]
    if len(skin):
        p0 = str(mesh.part[0]) if len(mesh.part) else ""
        faces.append({"tag": s_tag, "name": SOLID_GROUP, "elements": int(len(solid.tets)), "wall_mm": None,
                      "part": p0, "part_name": labels.get(p0, p0), "solid": True})
    return faces


#: MIXED-14: the viewer's group of the thick regions kept solid (their skin)
SOLID_GROUP = "Solid"


def name_parts(mesh, ws: Path) -> None:
    """SHWRITE-1: the mesh's parts named as the CAD names them - the STEP names the assembly staging read
    (cad/fea_assembly's assembly.json, through engines/shell_delivery.part_names: the very names the result viewer
    lists, so the files and the viewer agree); a part with none, or a run with no assembly, is Part_k as before.
    Names only: the mesh is not touched."""
    from meshpipeline.engines.shell_delivery import part_names

    try:
        names = part_names(ws)
    except Exception as exc:  # noqa: BLE001 - a courtesy: every part is still Part_k
        logger.info("shell run: the parts' names were not read (%s)", type(exc).__name__)
        names = {}
    ids = set(mesh.part)
    mesh.part_names = {k: v for k, v in names.items() if k in ids and v and v != k}


def parts_of(mesh, faces: list[dict]) -> list[dict]:
    """SHWRITE-1: the record's parts - shell_writers.parts_record with each part's shell.msh tags ("tags": the
    physical groups of its mid-faces), for the result viewer's parts list."""
    from meshpipeline.engines.shell_writers import parts_record

    tags: dict[str, list[int]] = {}
    for f in faces:
        tags.setdefault(str(f.get("part")), []).append(int(f["tag"]))
    return [{**p, "tags": tags.get(p["id"], [])} for p in parts_record(mesh)]


#: SHELL-59, A SHELL THE MESHER COULD NOT MESH ACROSS ITS DROPPED SLIVERS IS READ AGAIN WITH THEM: SHELL-16 drops a sewn
#: face narrower than a tenth of its wall and sews its neighbours across the gap, at 1.5 times the widest sliver. On a
#: hat rail with a joggle (cen_s0111) ten slivers up to 0.23 mm wide went and the whole body was sewn again at 0.34 mm,
#: and gmsh could not mesh the result ("Impossible to mesh periodic surface"); read with its slivers kept the same rail
#: meshes (7,832 elements, valid, one piece). When the mesh fails with a ValueError on a surface SHELL-16 sewed, the part
#: is extracted again with its slivers kept and meshed once more, and the record says so ("slivers_kept"). A part the
#: mesher meshes the first time is made exactly as before.
SLIVERS_AGAIN = True
SLIVERS_KEPT = ("The narrow strips the extraction would have dropped were kept: the surface sewn across them could not "
                "be meshed.")


def _again(rec: dict, record: dict) -> None:
    """The record's counts read again from a second extraction (SHELL-26, SHELL-59)."""
    from meshpipeline.cad.shell_words import left_out

    verdicts = _verdicts(record)
    rec.update(bodies=verdicts,
               open_edges=int(sum(int(r.get("open_edges") or 0) for r in record.get("sewn") or [])),
               solid_bodies=int(verdicts.get("solid", 0)) + sum(len(b.get("solid_regions") or [])
                                                                for b in record.get("bodies") or []),
               left_out=left_out(record))


#: MIXED-12, A SHELL KEEPS ITS THICK REGIONS AS SOLID: when the user asks for a shell, what can be shelled is shell and
#: the thick regions of a body of thin walls and thick regions (bosses, lugs, lips, thick flanges) are kept as solid
#: elements in the same mesh, joined to the shell (engines/mixed_run.build: the split, the shell of the trimmed walls,
#: the solid region, SHLINK-4's ties). The run's files are that one mesh, under the shell's names. A join that cannot
#: be made leaves the shell as before (its thick regions left out, SHSTAGE-9), and says why.
KEEP_THICK = True
KEPT_WORDS = ("{n} thick region{s} (bosses, lugs, lips or thick flanges) kept as solid elements in the same mesh "
              "({tets:,} 10-node tetrahedra), joined to the shell; the join is MPC set 1 in the Nastran file.")
KEPT_NOT = "The thick regions could not be kept as solid and joined to the shell ({why}), so they are left out."
KEPT_FOLDER = "kept"            # MIXED-26: the attempt's own work folder, under the shell's


def _kept_shape(record: dict) -> bool:
    """MIXED-12: the record is one body of thin walls and thick regions whose mid-surfaces meet (no open edge)."""
    bodies = record.get("bodies") or []
    return (len(bodies) == 1 and bodies[0].get("verdict") == "mixed" and bool(bodies[0].get("solid_regions"))
            and bool(record.get("surface_faces"))
            and not any(int(r.get("open_edges") or 0) for r in record.get("sewn") or []))


#: MIXED-18, KEEPING THICK REGIONS SOLID PAYS ONLY WHERE THE WALLS HOLD THE METAL: the attempt cuts every wall's slab
#: out of the body and grows each thick region back into the walls; when the walls hold little of the metal the
#: "thick region" is most of the part (a 3D-printed case whose 2 mm walls were sewn only in part: 2 to 10% of the
#: metal), the cut is the whole body's, and on the board (e398a798) every such attempt failed or ran out its 300 s
#: guard and came back with the shell made before. Measured on mixed_run.thin_share (the sewn mid-faces times their
#: walls over the volume): parts that kept their thick regions solid well read 0.35 to 1.0 (9 board parts, 14
#: development parts); parts whose attempt paid and failed read 0.016, 0.065, 0.096 and 0.22, others 0.59 to 0.85
#: (not separable on this share; MIXED-19 makes their failures cheap). Under KEPT_MIN_SHARE the attempt is not made,
#: said, and the stage does not promise it (application/shell_check reads keeps_thick).
KEPT_MIN_SHARE = 0.3
KEPT_LOW = ("its shell walls hold {p:.0f}% of its metal, and keeping thick regions solid pays only when they hold at "
            "least {m:.0f}%")


def kept_cannot_pay(record: dict) -> str:
    """MIXED-18: why keeping the thick regions solid cannot pay on this record (words), or "" when it can."""
    from meshpipeline.engines import mixed_run as mr

    share = mr.thin_share(record)
    if share < KEPT_MIN_SHARE:
        return KEPT_LOW.format(p=100 * share, m=100 * KEPT_MIN_SHARE)
    return ""


def keeps_thick(record: dict) -> bool:
    """MIXED-12: the record is one body of thin walls and thick regions whose mid-surfaces meet (no open edge) - the
    part mixed_run.build can split, mesh and join - and MIXED-18: its walls hold enough of its metal to pay."""
    return _kept_shape(record) and not kept_cannot_pay(record)


#: MIXED-12's guard: the shell-and-solid attempt gets at most KEPT_BUDGET_S, and never the time the shell itself needs
#: before the run's outer stop (HEXERA_SHELL_RUN_END less KEPT_RESERVE_S). On the 9 development parts it makes it took
#: 13 to 89 s; on a round-tube truss whose saddle joints read thick (extraction 431 s) it ran the scorer's 900 s out
#: and the shell it had made before was lost. Past the guard the attempt is stopped (it runs in a forked child) and the
#: shell is made as before, said, and the trip recorded (TIME-3 guard).
KEPT_BUDGET_S = 300.0
KEPT_RESERVE_S = 240.0
KEPT_SLOW = "it took longer than the {s:.0f} s allowed"
#: MIXED-16, A JOIN THAT CANNOT FIT ITS TIME IS NOT STARTED: the attempt redoes the extraction's kind of work on the
#: same body (Booleans and offsets of every wall's slab against the body, then of each grown thick region) and more,
#: so it costs at least KEPT_PER_EXTRACT times what the extraction cost: on the 14 development parts it kept solid it
#: took 2.05 to 15 times the extraction (3.4 to 23 s after 1.4 to 4.4 s). When that already passes the guard's
#: budget the attempt is not started and the shell is made as before, said. A round-tube truss (26 saddle joints
#: read thick, 53 tube walls, extraction 368 to 552 s) spent its 300 s in the split's first Boolean - the body less
#: every wall's slab: 47 s with 1 slab, 67 s with 8, 139 s with 16, past 12 min with all 53 - and came out as before.
KEPT_PER_EXTRACT = 2.0
KEPT_AHEAD = ("reading its walls took {e:.0f} s, and keeping thick regions solid takes at least {k:g} times as long, "
              "more than the {s:.0f} s allowed")


def _kept_budget_s() -> float:
    from meshpipeline.settings.env import optional_env

    budget = float(KEPT_BUDGET_S)
    try:
        end = float(optional_env("HEXERA_SHELL_RUN_END", "") or "nan")
    except ValueError:
        end = float("nan")
    if end == end:
        budget = min(budget, end - time.time() - KEPT_RESERVE_S)
    return budget


def kept_ahead(extract_s: float, budget_s: float) -> str:
    """MIXED-16: why the shell-and-solid attempt is not started (it cannot finish inside `budget_s` when the
    extraction it builds on took `extract_s`), or "" when it is worth starting."""
    if KEPT_PER_EXTRACT * float(extract_s) > budget_s:
        return KEPT_AHEAD.format(e=float(extract_s), k=KEPT_PER_EXTRACT, s=max(0.0, budget_s))
    return ""


def _build_kept(step, out: Path, mm_per_unit: float, requirements: dict | None, record: dict, target: dict | None):
    from meshpipeline.engines import mixed_run as mr

    sub: dict = {}
    joined = mr.build(step, out, mm_per_unit=mm_per_unit, rec=sub, requirements=requirements, record=record,
                      target=target)
    return joined, sub


#: MIXED-25, A THICK REGION'S GROWTH IS BOUNDED BY THE PART'S OWN COST: growing a core into its walls is one OCC offset
#: per core (cad/mixed_split.grown, up to 10 ways tried), which cannot be stopped from inside. Where it cannot grow it
#: runs long and then fails: cen_r0533's one core still growing after 287 s (its attempt stopped by the 300 s
#: guard), pub_0013 46 s and lab_fr003 24 s before "a thick core could not be grown". Every core grown on the parts
#: that kept thick regions solid grew in under 8 s (cen_s0190's three together 8.0 s; the 14 development parts 0.5 to
#: 1.4 s a part, on extractions of 3 to 15 s). A growth that has not finished after GROW_S (about twice the slowest
#: measured) is stopped with the attempt (the child reports when a growth starts), and the shell is made as before,
#: said. Scaling it with the extraction's seconds was weighed and not taken: no part that kept its regions grew
#: slower on a slower extraction, and on cen_r0533 (68 s extraction) it would have paid 53 s more for nothing.
GROW_S = 15.0
KEPT_GROW = "growing a thick region into its walls had not finished after {s:.0f} s"


#: MIXED-26, THE KEPT-SOLID ATTEMPT IS BOUNDED BY THE ALL-SHELL MESH'S OWN COST: the all-shell mesh is made beside the
#: attempt (the attempt in its forked child, in a work folder of its own; the all-shell mesh in the run), so it is
#: always in hand. Once it is made, the attempt gets KEPT_PER_PLAIN times the seconds the all-shell mesh took, and
#: never less than KEPT_FLOOR_S (both counted from the attempt's start; never past MIXED-12's guard). Past that clock
#: the attempt is stopped, the all-shell mesh is delivered and the user is told (KEPT_CLOCK). Measured (no clock,
#: testlab-2): the keepers' attempt over the all-shell mesh's seconds read 0.79 to 95 - cen_s0190 181 s on 25.9 s
#: (6.98), lab_sh0023 2.0, the development parts 2.6 to 69 but all under 48 s. The floor is 1.25 x the slowest keeper
#: attempt on a small all-shell mesh (48 s on 0.7 s); the multiple about 1.4 x the largest ratio past the floor (6.98).
#: Made beside the all-shell mesh, an attempt that fails before it is done costs about nothing on top.
#: MIXED-27: counted in CPU seconds, both are 1.5 x the wall-second values above (10 and 60): the attempt's process tree
#: works on more than one core where it has them (CPU over wall seconds 1.17 lab_sh0023, 1.42 cen_s0186, 1.43 pub_0020;
#: the all-shell mesh 1.0 to 1.4), and at 10 and 60 cen_s0184 and cen_s0190 lost their kept regions to the clock.
KEPT_PER_PLAIN = 15.0
KEPT_FLOOR_S = 90.0
KEPT_CLOCK = "keeping them solid did not finish in the {s:.0f} s it was given, {k:g} times what the all-shell mesh took"


def kept_clock_s(plain_s: float) -> float:
    """MIXED-26: the seconds the kept-solid attempt is given, from the all-shell mesh's own measured seconds."""
    return max(float(KEPT_FLOOR_S), float(KEPT_PER_PLAIN) * max(0.0, float(plain_s)))


#: MIXED-27, THE KEPT-SOLID CLOCKS COUNT WORK, NOT THE MACHINE'S LOAD: MIXED-26's clock (and its floor) and MIXED-25's
#: growth bound are counted in CPU seconds - the all-shell mesh's own (the run's process and the children it reaped),
#: the attempt's own (its forked child and every process of its group, read from /proc) - so a worker with one core,
#: or one shared with other work, decides as a quiet one does: an attempt waiting for the core is not stopped for
#: waiting. (The plate with a 30 mm boss, testlab-2: the attempt 15-16 s of wall on one core or six, its all-shell mesh
#: 0.2-0.5 s, so MIXED-26's 60 s floor; the unit shard's failures were MIXED-28's hang, not this.) MIXED-12's 300 s
#: guard stays in wall seconds, the last stop. Without /proc the clocks fall back to wall seconds, as before.
KEPT_BY_CPU = True
WAIT_POLL_S = 0.5               # how often the attempt's CPU seconds are read while it runs


def _cpu_self() -> float:
    """CPU seconds of this process (all its threads) and of the children it has reaped."""
    t = os.times()
    return float(t.user + t.system + t.children_user + t.children_system)


def _cpu_group(pid: int) -> float | None:
    """MIXED-27: CPU seconds of process `pid` and every live process under it (each with the children it reaped), or
    None when /proc cannot tell."""
    tick = float(os.sysconf("SC_CLK_TCK"))
    total, todo, seen = 0, [int(pid)], set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            with open(f"/proc/{p}/stat", "rb") as fh:
                raw = fh.read()
            f = raw[raw.rfind(b")") + 2:].split()
            total += int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])     # utime stime cutime cstime
            for tid in os.listdir(f"/proc/{p}/task"):
                with open(f"/proc/{p}/task/{tid}/children", "rb") as fh:
                    todo.extend(int(c) for c in fh.read().split())
        except (OSError, ValueError, IndexError):
            if p == pid:
                return None
    return total / tick


#: MIXED-28, THE KEPT-SOLID ATTEMPT STARTS IN A CLEAN PROCESS WHEN THE RUN'S PROCESS HOLDS OTHER THREADS: a fork copies
#: only the forking thread, so a library's thread pool left in the run's process (OpenBLAS, VTK, OCC, OpenMP: 12 to 39
#: threads seen) is a pool with no threads in the child, and the child waits on it for ever. Measured at 79ad5509
#: (testlab-2, the unit shard that failed): the attempt forked from a process with 32 or 12 threads hung in its first
#: gmsh OCC call (`gmsh.model.occ.synchronize`, fea_driver._metre_box) until MIXED-26's 60 s clock or the 600 s guard
#: stopped it, and the thick region was left out; forked from a process with one thread it finished in 15 s. So when
#: this process runs more than one thread (/proc/self/task), the attempt is started by "spawn" (a fresh interpreter,
#: no inherited pools); with one thread it is forked as before. A callable that cannot be handed to a fresh
#: interpreter (not picklable) is forked as before. KEPT_START "fork" / "spawn" forces one way.
KEPT_START = "auto"


def _threads() -> int:
    """The threads this process runs (the OS's count, which sees native pools; Python's where /proc cannot tell)."""
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        import threading

        return threading.active_count()


def _start_kind(fn) -> str:
    """MIXED-28: "fork" or "spawn" for the attempt `fn`."""
    if KEPT_START in ("fork", "spawn"):
        return KEPT_START
    if _threads() <= 1:
        return "fork"
    import pickle

    try:
        pickle.dumps(fn)
    except Exception:  # noqa: BLE001 - a closure: forked as before
        return "fork"
    return "spawn"


def _child_main(tx, fn) -> None:
    """The attempt's child: fn() run in its own process group, its steps and result sent through `tx`."""
    from meshpipeline.cad import mixed_split as mx
    from meshpipeline.engines import mixed_run as mr

    os.setsid()            # its own group: a stop takes the solid mesher's workers with it
    mx.STEP = lambda name: tx.send(("step", name, time.time()))
    try:
        tx.send(("ok", fn(), time.time()))         # the time read before the result is handed over
    except BaseException as exc:  # noqa: BLE001 - handed to the parent
        tx.send(("err", exc if isinstance(exc, mr._Stop) else RuntimeError(f"{type(exc).__name__}: {exc}"[:300]),
                 time.time()))


def _fork(fn) -> dict:
    """fn() started in a child of its own process group (forked, or spawned clean: MIXED-28); the handle _wait reads."""
    import multiprocessing as mp

    kind = _start_kind(fn)
    ctx: Any = mp.get_context(kind)            # a ForkContext or a SpawnContext
    rx, tx = ctx.Pipe(duplex=False)
    t0 = time.time()
    p = ctx.Process(target=_child_main, args=(tx, fn))     # not a daemon: the solid mesher starts processes of its own
    p.start()
    tx.close()
    # MIXED-27: its clocks count its CPU seconds where /proc can tell them, else wall seconds as before
    by_cpu = bool(KEPT_BY_CPU and p.pid and _cpu_group(p.pid) is not None)
    return {"p": p, "rx": rx, "t0": t0, "done_s": None, "done_cpu_s": None, "by_cpu": by_cpu, "start": kind}


def _guarded(fn, budget_s: float, ws: Path, grow_s: float | None = None):
    """fn() in a forked child, stopped past `budget_s`: its result, or its exception raised here (a _Stop with
    KEPT_SLOW when the guard trips). `grow_s`: MIXED-25, one core's growth is stopped past it (KEPT_GROW)."""
    from meshpipeline.engines import mixed_run as mr

    if budget_s <= 0:
        raise mr._Stop(KEPT_SLOW.format(s=0.0))
    return _wait(_fork(fn), budget_s, ws, grow_s=grow_s)


def _wait(h: dict, budget_s: float, ws: Path, grow_s: float | None = None, clock_s: float | None = None):
    """The forked attempt `h`'s result, or its exception raised here; stopped past `budget_s` (MIXED-12, KEPT_SLOW),
    past `clock_s` (MIXED-26, KEPT_CLOCK), or a growth past `grow_s` (MIXED-25, KEPT_GROW) - all counted from its
    start. h["done_s"]: the seconds it took, when it finished."""
    from meshpipeline.cad import mixed_split as mx
    from meshpipeline.engines import mixed_run as mr

    p, rx = h["p"], h["rx"]
    end = h["t0"] + budget_s                        # MIXED-12: wall seconds, the last stop

    def used() -> float:
        # MIXED-27: the clocks count the attempt's own CPU seconds (wall seconds where /proc cannot tell)
        cpu = _cpu_group(p.pid) if p.pid and h.get("by_cpu") else None
        return time.time() - h["t0"] if cpu is None else cpu

    got, growing, slow, spent = None, None, "", 0.0
    while got is None:
        spent = max(spent, used())
        left = [end - time.time()]
        if clock_s is not None:
            left.append(float(clock_s) - spent)
        if growing is not None and grow_s is not None:
            left.append(growing + float(grow_s) - spent)
        if min(left) <= 0.0:
            slow = ("guard" if left[0] <= 0.0 else "grow" if growing is not None and grow_s is not None
                    and growing + float(grow_s) - spent <= 0.0 else "clock")
            break
        if not rx.poll(min(min(left), WAIT_POLL_S)):
            continue
        spent = max(spent, used())
        msg = rx.recv()
        if msg[0] == "step":
            growing = spent if msg[1] == mx.GROW_STEP else None
            continue
        got = msg
        h["done_s"] = round(float(msg[2]) - h["t0"], 1)
        h["done_cpu_s"] = round(spent, 1) if h.get("by_cpu") else None
    p.join(5.0 if got is not None else 0.0)        # a stopped child is not waited for (it would cost 5 s more)
    if p.is_alive():
        import os
        import signal

        try:
            if not p.pid:
                raise OSError("no child")
            os.killpg(p.pid, signal.SIGKILL)       # its own group (setsid), never ours
        except OSError:
            p.kill()
        p.join()
    if got is None:
        from meshpipeline.engines.determinism import note_guard_trip

        h["stopped_cpu_s"] = round(spent, 1) if h.get("by_cpu") else None
        try:
            note_guard_trip("shell_thick_kept", workspace=ws,
                            rule={"grow": "MIXED-25", "clock": "MIXED-26"}.get(slow, "MIXED-12"),
                            budget_s=float(grow_s if slow == "grow" and grow_s is not None
                                           else clock_s if slow == "clock" and clock_s is not None else budget_s),
                            wall_s=round(time.time() - h["t0"], 1), cpu_s=h["stopped_cpu_s"])
        except Exception:  # noqa: BLE001 - the record says it all the same
            pass
        if slow == "grow":
            raise mr._Stop(KEPT_GROW.format(s=grow_s))
        if slow == "clock":
            raise mr._Stop(KEPT_CLOCK.format(s=float(clock_s or 0.0), k=float(KEPT_PER_PLAIN)))
        raise mr._Stop(KEPT_SLOW.format(s=max(0.0, budget_s)))
    kind, value = got[0], got[1]
    if kind == "err":
        raise value
    return value


#: MIXED-23, KEEPING A REGION SOLID NEVER COSTS NASTRAN'S LIMITS: the product ranks a mesh valid first, then inside
#: Nastran's own limits, then the user's shape lines, structure, edge lengths, triangles. When the mesh with its thick
#: regions kept solid still has shell elements past Nastran's limits (after MIXED-22's cut of the overlap quads), it is
#: held, and the all-shell mesh is made as for an attempt that failed: inside the limits (and valid) it is delivered,
#: said (KEPT_PAST); past them too, the two are level on that rank and the kept one is delivered (more of the part's
#: structure), said (KEPT_OVER). It costs the all-shell mesh only on such parts.
KEPT_PAST = "its shell elements were past Nastran's limits at the junction, and the all-shell mesh is inside them"
KEPT_OVER = (" Its shell elements are past Nastran's limits at the junction; the all-shell mesh was past them too, "
             "so the mesh with its thick regions kept solid is delivered.")


def _plain_inside(rec: dict) -> bool:
    """MIXED-23: the all-shell mesh made, valid and inside Nastran's limits."""
    q = rec.get("quality") or {}
    return bool(rec.get("made")) and q.get("valid") is not False and q.get("within_nastran") is True


def _held_delivered(ws: Path, held: dict, record: dict, rec: dict, before: dict, *, source: str,
                    requirements: dict | None, over: bool = True) -> None:
    """The kept-solid mesh delivered over the all-shell one (MIXED-26: made beside it), its files written over the
    all-shell one's, the record as the attempt left it. `over`: MIXED-23, the kept mesh is past Nastran's limits and
    the all-shell mesh no better on them (or not made), said."""
    rec.clear()
    rec.update(before)
    rec.pop("kept_solid_why", None)
    _deliver_kept(ws, held["joined"], held["sub"], record, rec, source=source, t1=float(held["t1"]),
                  requirements=requirements)
    if over:
        rec["kept_solid"]["words"] = str(rec["kept_solid"].get("words") or "") + KEPT_OVER


def _kept_start(ws: Path, out: Path, step, record: dict, rec: dict, *, mm_per_unit: float, target: dict | None,
                requirements: dict | None) -> dict | None:
    """MIXED-12: the shell with its thick regions kept as solid, started in a forked child (MIXED-26: in a work folder
    of its own, beside the all-shell mesh the run makes meanwhile). The handle _kept_finish reads, or None with
    rec["kept_solid_why"] when it is not started (MIXED-16)."""
    from meshpipeline.engines import mixed_run as mr

    try:
        budget = _kept_budget_s()
        ahead = kept_ahead(float(rec.get("extract_s") or 0.0), budget) if budget > 0 else ""
        if ahead:
            # MIXED-16: it cannot finish in its time, so it is not started (a TIME-3 guard, recorded as the trip is)
            from meshpipeline.engines.determinism import note_guard_trip

            note_guard_trip("shell_thick_kept", workspace=ws, rule="MIXED-16", budget_s=float(budget),
                            extract_s=float(rec.get("extract_s") or 0.0))
            raise mr._Stop(ahead)
        if budget <= 0:
            raise mr._Stop(KEPT_SLOW.format(s=0.0))
        own = out / KEPT_FOLDER             # MIXED-26: the all-shell mesh writes into `out` meanwhile
        own.mkdir(parents=True, exist_ok=True)
        for f in out.iterdir():
            if f.is_file():
                shutil.copyfile(f, own / f.name)
        import functools

        # a partial of a module function, so it can be handed to a clean process (MIXED-28)
        h = _fork(functools.partial(_build_kept, step, own, mm_per_unit, requirements, record, target))
    except Exception as exc:  # noqa: BLE001 - the shell is still made, without its thick regions, said
        _kept_not(rec, exc)
        return None
    h["budget"] = budget
    return h


def _kept_not(rec: dict, exc: BaseException) -> None:
    from meshpipeline.engines import mixed_run as mr

    why = str(exc) if isinstance(exc, mr._Stop) else f"{type(exc).__name__}: {str(exc)[:200]}"
    rec["kept_solid_why"] = KEPT_NOT.format(why=why.rstrip(". ") or "the join could not be made")
    logger.info("shell run: thick regions not kept solid (%s)", why[:300])


def _kept_finish(ws: Path, h: dict, record: dict, rec: dict, before: dict, *, plain_s: float | None, source: str,
                 requirements: dict | None, plain_cpu_s: float | None = None) -> bool:
    """MIXED-26: the attempt `h` waited for - KEPT_PER_PLAIN x the all-shell mesh's `plain_s` (KEPT_FLOOR_S at least;
    None: the all-shell mesh was not made, MIXED-12's guard alone) - and, made, delivered over the all-shell mesh
    unless MIXED-23 keeps that one. True when the kept-solid mesh is delivered; False with rec["kept_solid_why"]."""
    from meshpipeline.engines import mixed_run as mr

    # MIXED-27: counted in CPU seconds - the all-shell mesh's and the attempt's own - where the attempt's can be read
    plain = plain_cpu_s if h.get("by_cpu") and plain_cpu_s is not None else plain_s
    clock = kept_clock_s(plain) if plain is not None else None
    timing = {"plain_s": None if plain_s is None else round(float(plain_s), 1),
              "plain_cpu_s": None if plain_cpu_s is None else round(float(plain_cpu_s), 1),
              "by_cpu": bool(h.get("by_cpu")), "start": h.get("start"),
              "allowed_s": None if clock is None else round(clock, 1),
              "guard_s": round(float(h["budget"]), 1)}

    def told(kept: bool) -> dict:
        return {**timing, "attempt_s": h.get("done_s"), "attempt_cpu_s": h.get("done_cpu_s"),
                "stopped_cpu_s": h.get("stopped_cpu_s"), "kept": kept}

    try:
        joined, sub = _wait(h, float(h["budget"]), ws, grow_s=GROW_S, clock_s=clock)
        sq = sub.get("solid_quality") or {}
        if sq.get("valid") is False or sq.get("fatal"):
            raise mr._Stop("the solid mesh of the thick regions is not valid")
    except Exception as exc:  # noqa: BLE001 - the all-shell mesh is delivered, said
        _kept_not(rec, exc)
        rec["kept_clock"] = told(False)
        return False
    past = (sub.get("shell_quality") or {}).get("within_nastran") is False
    if past and _plain_inside(rec):
        rec["kept_solid_why"] = KEPT_NOT.format(why=KEPT_PAST)       # MIXED-23: the all-shell mesh is delivered
        rec["kept_clock"] = told(False)
        return False
    _held_delivered(ws, {"joined": joined, "sub": sub, "t1": h["t0"]}, record, rec, before, source=source,
                    requirements=requirements, over=past)
    rec["kept_clock"] = told(True)
    return True


def _deliver_kept(ws: Path, joined, sub: dict, record: dict, rec: dict, *, source: str, t1: float,
                  requirements: dict | None) -> None:
    """MIXED-12: the joined mesh written under the shell's names, rec filled as a shell run's, with "kept_solid"."""
    from meshpipeline.engines import mixed_mesh as mm

    rec["mesh_s"] = round(time.time() - t1, 1)
    shell = joined.shell
    name_parts(shell, ws)
    exports = mm.write_all(ws, joined, units="m", source=source, stem="shell")
    (ws / "shell_quality.json").write_text(json.dumps({**(sub.get("shell_plan") or {}),
                                                      "quality": sub.get("shell_quality"),
                                                      "solid_quality": sub.get("solid_quality")},
                                                     indent=1, sort_keys=True, default=float))
    faces = write_msh(ws / MSH, shell, solid=joined.solid)      # MIXED-14: the whole mesh
    xyz_m = shell.xyz / 1000.0
    told = float((rec.get("left_out") or {}).get("area_share") or 0.0)
    c = joined.counts
    n = len(record["bodies"][0].get("solid_regions") or []) or 1
    rec.update(nodes=int(c["shell_nodes"] + c["solid_nodes"]), faces=faces, parts=parts_of(shell, faces),
               bounds_m=[float(v) for v in (*xyz_m.min(axis=0), *xyz_m.max(axis=0))] if len(xyz_m) else None,
               elements=int(shell.n_elements + c["solid_elements"]), quads=int(len(shell.quads)),
               trias=int(len(shell.trias)), walls_mm=sorted({round(float(w), 3) for w in shell.wall}),
               quality=sub.get("shell_quality"), exports={k: v for k, v in exports.items() if isinstance(v, dict)},
               left_out={}, solid_bodies=0, kept_solid={"regions": n, "counts": c, "split": sub.get("split"),
                                        "words": KEPT_WORDS.format(n=n, s="" if n == 1 else "s",
                                                                   tets=int(c["solid_elements"]))},
               made=all((ws / f).exists() and (ws / f).stat().st_size > 0 for f in FILES))
    if sub.get("requirements"):
        rec["requirements"] = sub["requirements"]
    # MIXED-13: the shell part's count, sizing and places told as a shell-only run tells them
    for key, val in (sub.get("shell_plan") or {}).items():
        if key == "target" or requirements:
            rec[key] = val
    from meshpipeline.engines import shell_delivery as SD

    cov = SD.coverage(shell, part_mm2=sum(float(b.get("area_mm2") or 0.0) for b in record.get("bodies") or []),
                      mid=True, kept=SD.kept_share(ws, source), told_share=told)
    if cov:
        rec["coverage"] = cov
    if not rec["made"]:
        rec["why"] = "the solver files could not all be written"


def run(workspace: Path | str, step: Path | str, *, mm_per_unit: float, source: str = "",
        target: dict | None = None, requirements: dict | None = None) -> dict:
    """Extract, mesh and write the shell of `step` (coordinates `mm_per_unit` mm each) into `workspace`. `target`: the
    element count the user asked for (engines/fea_driver.element_target), which the shell's sizes follow (SHMESH-23).
    `requirements`: the mesh requirements read from the user's notes (engines/mesh_requirements): their sizes, limits
    and element kind are applied (SHMESH-24, 25) and each is told asked against delivered (SHMESH-26, the record's
    "requirements"). None: the rules' own shell, exactly as before. SHWRITE-1: the parts are named in every file as
    the assembly staging named them (name_parts)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines.gmsh import shell_adapter as sa

    ws = Path(workspace)
    started = time.time()
    rec: dict = {"made": False, "mm_per_unit": float(mm_per_unit)}
    pending: dict | None = None     # MIXED-26: the kept-solid attempt, made beside the all-shell mesh
    before: dict = {}
    kept_record: dict = {}
    try:
        out = ws / FOLDER
        out.mkdir(parents=True, exist_ok=True)
        record = ms.extract_file(step, out, mm_per_unit=mm_per_unit)
        rec.update(extract_s=round(time.time() - started, 1), advice=str(record.get("advice") or ""))
        _again(rec, record)
        kept_record = record
        if not record.get("surface_faces"):
            rec["why"] = no_shell_why(record)
        else:
            if KEEP_THICK and keeps_thick(record):
                # MIXED-12: the shell with its thick regions kept solid, started beside the all-shell mesh (MIXED-26)
                pending = _kept_start(ws, out, step, record, rec, mm_per_unit=mm_per_unit, target=target,
                                      requirements=requirements)
            elif KEEP_THICK and _kept_shape(record) and kept_cannot_pay(record):
                rec["kept_solid_why"] = KEPT_NOT.format(why=kept_cannot_pay(record))      # MIXED-18, said
            before.update(rec)      # as they were, should the kept-solid mesh be delivered
            t1, c1 = time.time(), _cpu_self()
            try:
                mesh, applied = sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
            except ValueError as exc:
                if not (ms.has_plates(record) or (SLIVERS_AGAIN and ms.has_slivers(record))):
                    raise
                again = None
                plates = True
                if ms.has_plates(record):
                    # SHELL-26: a shell whose short plates the mesher could not close is made as before them, said
                    plates = False
                    record = ms.extract_file(step, out, mm_per_unit=mm_per_unit, plates=False)
                    rec.update(plates_left_out=PLATES_OUT, plates_mesh_error=str(exc)[:300], advice="")
                    _again(rec, record)
                    if not record.get("surface_faces"):
                        raise
                    try:
                        again = sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
                    except ValueError as exc2:
                        if not (SLIVERS_AGAIN and ms.has_slivers(record)):
                            raise
                        exc = exc2
                if again is None:
                    # SHELL-59: the surface sewn across its dropped slivers (SHELL-16) the mesher could not mesh: the
                    # part is read again with its slivers kept, and meshed once more, said
                    record = ms.extract_file(step, out, mm_per_unit=mm_per_unit, plates=plates, slivers=False)
                    rec.update(slivers_kept=SLIVERS_KEPT, slivers_mesh_error=str(exc)[:300])
                    _again(rec, record)
                    if not record.get("surface_faces"):
                        raise
                    again = sa.mesh_run(out, mm_per_unit=mm_per_unit, target=target, requirements=requirements)
                mesh, applied = again
            rec["mesh_s"] = round(time.time() - t1, 1)
            name_parts(mesh, ws)
            from meshpipeline.cad.shell_words import left_out_lines

            # SHSTAGE-9: every deck's header says what this mesh has no elements for (nothing when nothing is)
            exports = sw.write_all(ws, mesh, units="m", source=source, notes=left_out_lines(rec.get("left_out")))
            # THE SHELL IS THE RESULT (engines/shell_delivery): the viewer's picture of it, one group per mid-face
            faces = write_msh(ws / MSH, mesh)
            xyz_m = mesh.xyz / 1000.0
            rec.update(nodes=int(len(mesh.xyz)), faces=faces, parts=parts_of(mesh, faces),
                       bounds_m=[float(v) for v in (*xyz_m.min(axis=0), *xyz_m.max(axis=0))] if len(xyz_m) else None)
            q = applied.get("quality") if isinstance(applied, dict) else None
            (ws / "shell_quality.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
            rec.update(made=all((ws / f).exists() and (ws / f).stat().st_size > 0 for f in FILES),
                       elements=int(mesh.n_elements), quads=int(len(mesh.quads)), trias=int(len(mesh.trias)),
                       walls_mm=sorted({round(float(w), 3) for w in mesh.wall}),
                       quality=q if isinstance(q, dict) else None, exports={k: v for k, v in exports.items()
                                                                            if isinstance(v, dict)})
            # MESH-1c: how much of the part the shell covers (its mid-surface: half the solid's face area, less the
            # regions told as left out), against the upload when the driver staged less of it
            from meshpipeline.engines import shell_delivery as SD

            cov = SD.coverage(mesh, part_mm2=sum(float(b.get("area_mm2") or 0.0) for b in record.get("bodies") or []),
                              mid=True, kept=SD.kept_share(ws, source),
                              told_share=float((rec.get("left_out") or {}).get("area_share") or 0.0))
            if cov:
                rec["coverage"] = cov
            if isinstance(applied, dict) and isinstance(applied.get("target"), dict):
                rec["target"] = applied["target"]
            if isinstance(applied, dict) and requirements:
                for key in ("requirements", "sizing", "count_set_aside", "places"):
                    if applied.get(key):
                        rec[key] = applied[key]
            if not rec["made"]:
                rec["why"] = "the solver files could not all be written"
            if pending is not None:
                # MIXED-26: the all-shell mesh is in hand; the attempt gets a clock set from what it cost
                h, pending = pending, None
                _kept_finish(ws, h, kept_record, rec, before, plain_s=time.time() - t1, source=source,
                             requirements=requirements, plain_cpu_s=_cpu_self() - c1)
        for name in (ms.RECORD_FILE, ms.SURFACE_FILE):
            if (out / name).exists():
                shutil.copyfile(out / name, ws / (name if name != ms.RECORD_FILE else "shell.json"))
    except Exception as exc:  # noqa: BLE001 - the solid mesh is still delivered; the reason is kept
        logger.warning("shell run: no shell mesh (%s: %s)", type(exc).__name__, str(exc)[:300])
        from meshpipeline.engines.gmsh.shell_adapter import said_words

        kept = False
        if pending is not None:
            # MIXED-26: the all-shell mesh could not be made, so the attempt is waited for under MIXED-12's guard alone
            h, pending = pending, None
            try:
                kept = _kept_finish(ws, h, kept_record, rec, before, plain_s=None, source=source,
                                    requirements=requirements)
            except Exception as exc2:  # noqa: BLE001 - the shell's own reason is kept
                logger.warning("shell run: kept-solid mesh not delivered (%s)", str(exc2)[:300])
        if not kept:
            rec.update(made=False, why=said_words(exc) or f"the shell mesh could not be made ({type(exc).__name__})")
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


def with_places(workspace: Path | str, requirements: dict | None) -> dict | None:
    """SHMESH-52: the requirements with the parts reading the FEA driver left for a size asked in one named place
    (engines/shell_places.FILE) - only when the requirements ask one; otherwise the requirements as they are."""
    from meshpipeline.engines import shell_places as SP

    if requirements is None or not SP.wanted(requirements):
        return requirements
    places = SP.read(workspace)
    return {**requirements, "places": places} if places is not None else requirements


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
    step = args[1]
    if args[2] == "staged" and abs(mm - 1.0) > 1e-9:
        # SHELL-18m: the staged solid (metres) is handed to the extraction and the mesher in millimetres
        try:
            step, mm = str(in_millimetres(Path(args[0]), Path(args[1]), mm)), 1.0
        except Exception as exc:  # noqa: BLE001 - meshed in the staged unit, as before
            logger.warning("shell run: the staged solid was not put in millimetres (%s)", type(exc).__name__)
    run(args[0], step, mm_per_unit=mm, source=Path(args[1]).name, target=target,
        requirements=with_places(args[0], requirements_in(args[0])))
    return 0


#: SHELL-18m: the staged solid in millimetres, beside it in the workspace
MM_COPY = "staged_mm.brep"


def in_millimetres(ws: Path, step: Path, mm_per_unit: float) -> Path:
    """SHELL-18m: THE SHELL IS MADE ON THE PART IN MILLIMETRES, whatever unit it was staged in. The mid-surface
    extraction and the shell mesher are built and proven on millimetre geometry (every lab and scoreboard run), and
    the same part staged in metres came out otherwise: a thin casting's mid-surface kept 0.18 mm gaps and 0.22 mm
    edges the millimetre reading has not, 2,296 elements where the lab made 3,815. Writes MM_COPY (the staged shape
    scaled by `mm_per_unit`, its faces in the same order) and returns its path."""
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad.fea_assembly import _scaled
    from meshpipeline.cad.scout import read_cad

    out = Path(ws) / MM_COPY
    BRepTools.Write_s(_scaled(read_cad(step), float(mm_per_unit)), str(out))
    return out


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FILES", "MM_COPY", "MSH", "RECORD", "in_millimetres", "main", "name_parts", "parts_of", "read", "requirements_in", "run", "with_places",
           "words", "write_msh"]
