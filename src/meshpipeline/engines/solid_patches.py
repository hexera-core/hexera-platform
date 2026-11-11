# Responsibility: THE SOLID PATCHES OF A SHELL RUN (MIXED-9): when the user asked for a shell and a region of the part
# (a boss, a block, a nozzle root) or a whole body cannot be made into a shell, that region is meshed SOLID and tied to
# the shell round it, in the same solver files, instead of being left out of them. The split, the solid mesh and the
# join are the mixed route's own, already proved against all-solid meshes (cad/mixed_split MIXED-1..3, engines/
# mixed_run's solid path, engines/mixed_mesh SHLINK-4).
# Boundaries: deterministic, no model call, no store. Reads the extraction's record and sewn surface, writes only
# into the folder it is given. Raises Stop with the reason when no patch can be made: the shell run then makes
# today's shell and says which regions stay out and why. Never used when the notes ask for a shell only.
# Collaborates with: engines/shell_run.py (calls build, writes the decks and the viewer mesh), cad/mixed_split.py,
# engines/mixed_run.py (_solid_mesh), engines/mixed_mesh.py (join, the decks), engines/gmsh/shell_adapter.py.
"""MIXED-9: SHELL WHERE YOU CAN, SOLID PATCHES WHERE YOU MUST, TIED TOGETHER.

The founder's decision (11 Nov): on a shell request, a region the extraction keeps solid is meshed as solid and joined
to the shell round it, never left out of the solver files. Industry users accept this.

MIXED-9a, WHAT IS PATCHED: every thick region of a body that has shell walls (the record's solid_regions of a
"mixed" body), and every body that is solid as a whole when another body of the file has a shell. A file with no
shell at all is meshed solid as before.

MIXED-9b, HOW: a mixed body is split as the mixed route splits it (MIXED-1..3: each thick core grown CUT_WALLS of
its thickest wall into the walls round it, the body's metal inside it the solid region); the sewn mid-surface of the
whole part is cut by those regions in one Boolean, so every shell edge left there lies on a cut face, and meshed as a
shell run meshes it; each region is meshed by the FEA path's own plan and adapter (quadratic tetrahedra); a body solid
as a whole is meshed whole, its faces the faces a shell edge may end on. The join is SHLINK-4: each shell edge on a
solid face runs one wall on into the solid, and both node rows are tied, translations only, so moments cross.

MIXED-9c, HELD TO: the shell mesh valid; every solid patch valid; every link node tied; every patch tied to the shell
at least once - a patch that meets no shell edge (a block standing apart, or only lying on a plate's face) is left
out and said. A mixed body whose split cannot be made keeps today's shell, its regions said as left out.

MIXED-9d, SHELL ONLY: when the user's notes say so ("shell only", "no solid", "leave out what can't be shelled":
engines/mesh_requirements, kind "shell_only"), nothing is patched: today's shell, said as asked.

MIXED-9e, THE WORDS: one line, "2 regions are too thick for a shell and are meshed as solid, tied to the shell
(Part_3: 2 regions; Part_7: whole part)." - the list only for a file of several bodies. None when nothing is solid.
"""
from __future__ import annotations

import copy
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

RULE = "MIXED-9"
#: the element kind the notes reader gives for "shell only" (engines/mesh_requirements KINDS)
SHELL_ONLY = "shell_only"
#: the shell trimmed round the regions, as written for the mesher
TRIMMED_FILE = "shell_patched.brep"


class Stop(Exception):
    """No solid patch can be made, with the reason in plain words."""


@dataclass
class Region:
    """One solid patch before it is meshed: the body it is of, whether it is the whole body, how many of the
    record's regions it holds, its solid (the part's units) and the faces a shell edge may end on."""
    body: str
    index: int
    whole: bool
    count: int
    shape: object
    cut_faces: list
    name: str = ""


@dataclass
class Patched:
    """The shell run's mesh with its solid patches: the joined mesh (engines/mixed_mesh.MixedMesh, its `regions`
    set), the patches as the record says them, what was left out and why, the shell mesher's applied plan, and each
    patch's solid quality."""
    joined: object
    patches: list[dict]
    left: list[dict]
    applied: dict
    solid_quality: list[dict] = field(default_factory=list)
    seconds: dict = field(default_factory=dict)


def shell_only(requirements: dict | None) -> bool:
    """MIXED-9d: whether the notes asked for a shell only (no solid anywhere)."""
    return bool(((requirements or {}).get("kind") or {}).get(SHELL_ONLY))


def solid_rows(record: dict) -> list[dict]:
    """MIXED-9a: what stays solid in the extraction's record and could be a patch: each body's thick regions (a
    body with shell walls) and each body solid as a whole when the part has a shell somewhere -
    [{"body", "whole", "count"}], in body order."""
    bodies = record.get("bodies") or []
    has_shell = any(str(b.get("verdict")) in ("shell", "mixed") for b in bodies) and bool(record.get("surface_faces"))
    out = []
    for b in bodies:
        v = str(b.get("verdict") or "solid")
        regs = b.get("solid_regions") or []
        if v == "mixed" and regs:
            out.append({"body": str(b.get("body")), "whole": False, "count": len(regs)})
        elif v == "solid" and has_shell:
            out.append({"body": str(b.get("body")), "whole": True, "count": 1})
    return out


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def words(patches: list[dict], *, bodies: int = 1) -> str:
    """MIXED-9e: the one line said on the stage and in the result; "" with no patch."""
    n = sum(int(p.get("count") or 1) for p in patches)
    if not n:
        return ""
    said = (f"{n} {_plural(n, 'region is', 'regions are')} too thick for a shell and {_plural(n, 'is', 'are')} "
            "meshed as solid, tied to the shell")
    if bodies > 1:
        items = [f"{p['body']}: whole part" if p.get("whole")
                 else f"{p['body']}: {int(p.get('count') or 1)} {_plural(int(p.get('count') or 1), 'region', 'regions')}"
                 for p in patches]
        said += f" ({'; '.join(items)})"
    return said + "."


def left_words(left: list[dict]) -> str:
    """What stays out of the shell files after all (a split that could not be made, a block that meets no shell):
    one sentence, with the first reason."""
    n = sum(int(x.get("count") or 1) for x in left)
    if not n:
        return ""
    why = next((str(x.get("why")) for x in left if x.get("why")), "")
    return (f"{n} {_plural(n, 'region stays', 'regions stay')} solid and {_plural(n, 'is', 'are')} not in the shell "
            f"files" + (f": {why}" if why else "") + ".")


# ------------------------------------------------------------------------------------------------ the build ----
def _scaled(shape, k: float):
    if k == 1.0:
        return shape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf

    tr = gp_Trsf()
    tr.SetScale(gp_Pnt(0, 0, 0), float(k))
    return BRepBuilderAPI_Transform(shape, tr, True).Shape()


def trim(sewn: list, owners: list[str], tools: list) -> tuple[list, list[str]]:
    """MIXED-9b: the sewn mid-surface less the solid regions, cut as ONE shape (the faces keep the edges they share),
    each piece owned by the sewn face it lies on (the nearest, among those whose box holds it). No tool: as it is."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx

    if not tools:
        return list(sewn), list(owners)
    pieces = ms._faces(mx._bop("cut", ms._compound(list(sewn)), list(tools)))
    boxes = [ms._box(f) for f in sewn]
    out, own = [], []
    for p in pieces:
        q = ms._inner_point(p)
        if q is None:
            continue
        q = np.asarray(q, float)
        near = [i for i, (lo, hi) in enumerate(boxes) if np.all(q >= lo - 1e-6 * (1 + np.abs(lo)))
                and np.all(q <= hi + 1e-6 * (1 + np.abs(hi)))] or list(range(len(sewn)))
        k = min(near, key=lambda i: (ms._dist(ms._vertex(q), sewn[i]), i))
        out.append(p)
        own.append(owners[k])
    return out, own


def _met_faces(shell, faces: list, wall_share: float) -> list:
    """The faces of a whole solid body that a shell free edge ends on (both its nodes within `wall_share` of the
    edge's wall of the face): the faces the join looks at, so a big block's other faces cost nothing."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines.shell_mesh import edge_users

    users = edge_users(shell)
    free = sorted((k, u[0][0]) for k, u in users.items() if len(u) == 1)
    if not free:
        return []
    nodes = sorted({n for (a, b), _e in free for n in (a, b)})
    wall = {}
    for (a, b), e in free:
        for n in (a, b):
            wall[n] = max(wall.get(n, 0.0), float(shell.wall[e]))
    xyz = np.asarray(shell.xyz, float)
    out = []
    for f in faces:
        lo, hi = ms._box(f)
        pad = max(wall.values()) * wall_share + 1e-6
        cand = [n for n in nodes if np.all(xyz[n] >= lo - pad) and np.all(xyz[n] <= hi + pad)]
        hit = 0
        for n in cand:
            d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, xyz[n]))).Vertex(), f)
            if d.IsDone() and d.Value() <= wall_share * wall[n] + 1e-6:
                hit += 1
                if hit >= 2:
                    out.append(f)
                    break
    return out


def _merge(parts: list) -> tuple[object, list[tuple[int, int]], list[tuple[int, int]]]:
    """The solid patches' meshes as one SolidPart (one numbering), with each patch's node and tet ranges."""
    from meshpipeline.engines import mixed_mesh as mm

    xyz, tets, faces = [], [], {}
    nodes_at, tets_at = [], []
    n0 = t0 = 0
    many = len(parts) > 1
    for i, p in enumerate(parts, start=1):
        xyz.append(p.xyz)
        tets.append(np.asarray(p.tets, dtype=np.int64) + n0)
        for name, ids in p.faces.items():
            faces[f"P{i}_{name}" if many else name] = np.asarray(ids, dtype=np.int64) + n0
        nodes_at.append((n0, n0 + p.n_nodes))
        tets_at.append((t0, t0 + len(p.tets)))
        n0 += p.n_nodes
        t0 += len(p.tets)
    return (mm.SolidPart(xyz=np.vstack(xyz), tets=np.vstack(tets), faces=faces), nodes_at, tets_at)


def _tied(joined, nodes_at: list[tuple[int, int]]) -> list[int]:
    """How many tie terms reach each patch's nodes (deck numbers: shell nodes first, then the solid's)."""
    base = len(joined.shell.xyz)
    got = np.zeros(len(nodes_at), dtype=np.int64)
    starts = np.array([a for a, _b in nodes_at])
    for t in joined.ties:
        for n, _d, _c in t.terms:
            j = n - base - 1
            if j >= 0:
                got[int(np.searchsorted(starts, j, side="right") - 1)] += 1
    return got.tolist()


def build(step, out: Path, record: dict, *, mm_per_unit: float, requirements: dict | None = None,
          target: dict | None = None, requirements_file: Path | None = None) -> Patched:
    """MIXED-9: the shell of `step` with its solid patches, joined (all lengths mm). `out`: the extraction's folder
    (its record and sewn surface are read there; the patches' work goes there too). Raises Stop with the reason
    when no patch can be made."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import mixed_split as mx
    from meshpipeline.cad.scout import read_cad
    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines import mixed_run
    from meshpipeline.engines.gmsh import shell_adapter as sa

    t0 = time.time()
    k = float(mm_per_unit)
    rows = solid_rows(record)
    if not rows:
        raise Stop("nothing stays solid")
    shape = ms.solidify(read_cad(Path(step)), k)[0]
    solids = mx._solids(shape)
    bodies = record.get("bodies") or []
    if len(solids) != len(bodies):
        raise Stop(f"the part reads as {len(solids)} solids, not the {len(bodies)} bodies the shell was made of")
    surf = TopoDS_Shape()
    BRepTools.Read_s(surf, str(Path(out) / ms.SURFACE_FILE), BRep_Builder())
    sewn = ms._faces(surf)
    owners = list(record.get("surface_faces") or [])
    if len(sewn) != len(owners):
        raise Stop("the mid-surface does not match its record")
    mids = {m["id"]: m for m in record.get("mids") or []}
    faces = ms._faces(shape)
    roles = record.get("faces") or []
    index = {str(b.get("body")): i for i, b in enumerate(bodies)}
    regions: list[Region] = []
    left: list[dict] = []
    split_notes: list[dict] = []
    for row in rows:
        b = index[row["body"]]
        if row["whole"]:
            regions.append(Region(row["body"], b, True, 1, solids[b], ms._faces(solids[b])))
            continue
        ks = [i for i, o in enumerate(owners) if str(mids.get(o, {}).get("body")) == row["body"]]
        kept = [(sewn[i], float(mids[owners[i]]["thickness_mm"]) / k, float(mids[owners[i]].get("offset_mm", 0.0)) / k)
                for i in ks]
        thick = [faces[j] for j, r in enumerate(roles) if j < len(faces) and r.get("role") in ("thick", "solid")
                 and str(r.get("body")) == row["body"]]
        if not kept or not thick:
            left.append({**row, "why": "its thick regions could not be found again"})
            continue
        split = mx.split_body(solids[b], kept, thick, scale=k)
        split_notes.append({"body": row["body"], "ok": split.ok, "cores": split.cores,
                            "cut_mm": [round(d, 4) for d in split.cut_distance], "why": split.why})
        if not split.ok:
            left.append({**row, "why": f"its thick regions could not be cut out of {row['body']} ({split.why})"})
            continue
        regions.append(Region(row["body"], b, False, row["count"], split.solid, list(split.cut_faces)))
    if not regions:
        raise Stop(left[0]["why"] if left else "no region could be made solid")
    seconds = {"split_s": round(time.time() - t0, 1)}

    # the shell: the sewn surface less the regions of the mixed bodies (a body solid as a whole is not cut into: the
    # shell ends on its faces as it was made)
    t1 = time.time()
    pieces, own = trim(sewn, owners, [r.shape for r in regions if not r.whole])
    if not pieces:
        raise Stop("no shell is left once the solid regions are cut out")
    BRepTools.Write_s(ms._compound(pieces), str(Path(out) / TRIMMED_FILE))
    rec2 = copy.deepcopy(record)
    rec2["surface_faces"] = own
    shell, applied = sa.mesh_shell(Path(out) / TRIMMED_FILE, rec2, mm_per_unit=k, target=target,
                                   requirements=requirements)
    q = applied.get("quality") if isinstance(applied, dict) else None
    if isinstance(q, dict) and q.get("valid") is False:
        raise Stop("the shell mesh round the solid regions is not valid")
    seconds["shell_mesh_s"] = round(time.time() - t1, 1)

    # the solid patches, each meshed by the FEA path; a whole body's faces cut down to those a shell edge ends on
    t1 = time.time()
    from meshpipeline.engines.mixed_mesh import LOOSE_SHARE

    parts, sqs, cut_all, kept_regions = [], [], [], []
    for i, r in enumerate(regions, start=1):
        cut = [_scaled(f, k) for f in r.cut_faces]
        if r.whole:
            cut = _met_faces(shell, cut, LOOSE_SHARE)
            if not cut:
                left.append({"body": r.body, "whole": True, "count": 1,
                             "why": f"{r.body} does not meet the shell at any plate edge"})
                continue
        ws_i = Path(out) / f"solid_{i}"
        ws_i.mkdir(parents=True, exist_ok=True)
        if requirements_file is not None and Path(requirements_file).exists():
            # the notes' requirements are measured on the solid part too (engines/gmsh/fea_adapter PLAN-fea-34)
            shutil.copyfile(requirements_file, ws_i / Path(requirements_file).name)
        try:
            part, sq = mixed_run._solid_mesh(_scaled(r.shape, k), ws_i, requirements)
        except Exception as exc:  # noqa: BLE001 - this patch is left out, said; the others go on
            logger.warning("solid patch %s: not meshed (%s)", r.body, exc)
            left.append({"body": r.body, "whole": r.whole, "count": r.count,
                         "why": f"the solid mesh of {r.body}'s {'body' if r.whole else 'thick region'} could not be made"})
            continue
        if sq.get("fatal") or int(sq.get("fea_inverted") or 0) > 0:
            left.append({"body": r.body, "whole": r.whole, "count": r.count,
                         "why": f"the solid mesh of {r.body}'s {'body' if r.whole else 'thick region'} is not valid"})
            continue
        parts.append(part)
        sqs.append(sq)
        cut_all.append(cut)
        kept_regions.append(r)
    seconds["solid_mesh_s"] = round(time.time() - t1, 1)
    if not parts:
        raise Stop(left[-1]["why"] if left else "no solid patch could be meshed")

    # the join (SHLINK-4); a patch no shell edge reaches is left out and the rest joined again
    t1 = time.time()
    while True:
        solid, nodes_at, tets_at = _merge(parts)
        try:
            joined = mm.join(shell, solid, [f for c, r in zip(cut_all, kept_regions, strict=True) if not r.whole for f in c],
                             part=kept_regions[0].body,
                             loose_faces=[f for c, r in zip(cut_all, kept_regions, strict=True) if r.whole for f in c])
        except ValueError as exc:
            raise Stop(f"the shell meets none of the solid regions ({exc})") from exc
        reach = _tied(joined, nodes_at)
        loose = [i for i, n in enumerate(reach) if n == 0]
        if not loose:
            break
        for i in reversed(loose):
            r = kept_regions.pop(i)
            left.append({"body": r.body, "whole": r.whole, "count": r.count,
                         "why": f"{r.body}'s {'body' if r.whole else 'thick region'} does not meet the shell at a "
                                "plate edge"})
            del parts[i], sqs[i], cut_all[i]
        if not parts:
            raise Stop(left[-1]["why"])
    if joined.notes:
        raise Stop("; ".join(joined.notes))
    seconds["join_s"] = round(time.time() - t1, 1)
    patches = []
    joined.regions = []
    for j, (r, (a, b), sq) in enumerate(zip(kept_regions, tets_at, sqs, strict=True), start=1):
        r.name = f"{j}_{r.body}"
        joined.regions.append(mm.SolidRegion(name=r.name, part=r.body, first=a, count=b - a))
        patches.append({"name": f"Solid_{j}", "set": f"SOLID_{r.name}", "body": r.body, "whole": r.whole,
                        "count": r.count, "elements": b - a,
                        "quality": {key: sq.get(key) for key in ("cells", "fatal", "fea_inverted", "min_sicn",
                                                                 "sicn_floor", "elements_under_floor", "requirements")
                                    if key in sq}})
    applied = dict(applied) if isinstance(applied, dict) else {}
    applied["patches_split"] = split_notes
    return Patched(joined=joined, patches=patches, left=left, applied=applied,
                   solid_quality=[p["quality"] for p in patches], seconds=seconds)


# ------------------------------------------------------------------------------------------- the viewer ----
def write_msh(path: Path, joined, patches: list[dict]) -> list[dict]:
    """The shell run's viewer mesh with its solid patches (gmsh MSH 2.2, metres): the shell's mid-faces as
    engines/shell_run.write_msh writes them, then each solid patch's outer skin (its boundary triangles' corners) as
    a surface group of its own, named Solid_k. Returns the groups: the mid-faces' rows, then {"tag", "name", "body",
    "elements", "solid": True} per patch."""
    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines.shell_run import write_msh as shell_msh

    shell = joined.shell
    rows = shell_msh(path, shell)
    text = Path(path).read_text().splitlines()
    tag0 = max((int(r["tag"]) for r in rows), default=0)
    base = len(shell.xyz)
    solid = joined.solid
    tri_rows, out = [], []
    for j, (reg, p) in enumerate(zip(joined.regions or [], patches, strict=True), start=1):
        tets = solid.tets[reg.first:reg.first + reg.count]
        on = np.ones(solid.n_nodes + 1, dtype=bool)
        tri = mm.boundary_triangles(tets, on)[:, :3]
        tri_rows.append((tag0 + j, tri))
        out.append({"tag": tag0 + j, "name": p["name"], "body": p["body"], "elements": int(reg.count), "solid": True})
    # splice: names, nodes, elements
    i_names = text.index("$PhysicalNames")
    n_names = int(text[i_names + 1])
    names = [f'2 {t} "{r["name"]}"' for (t, _tri), r in zip(tri_rows, out, strict=True)]
    text[i_names + 1] = str(n_names + len(names))
    text[i_names + 2 + n_names:i_names + 2 + n_names] = names
    i_nodes = text.index("$Nodes")
    n_nodes = int(text[i_nodes + 1])
    text[i_nodes + 1] = str(n_nodes + solid.n_nodes)
    xyz = np.asarray(solid.xyz, dtype=float) / 1000.0
    at = i_nodes + 2 + n_nodes
    text[at:at] = [f"{base + j + 1} {x:.9g} {y:.9g} {z:.9g}" for j, (x, y, z) in enumerate(xyz)]
    i_el = text.index("$Elements")
    n_el = int(text[i_el + 1])
    extra = []
    e = n_el
    for t, tri in tri_rows:
        for row in tri.tolist():
            e += 1
            extra.append(f"{e} 2 2 {t} {t} " + " ".join(str(base + int(v)) for v in row))
    text[i_el + 1] = str(n_el + len(extra))
    end = text.index("$EndElements")
    text[end:end] = extra
    Path(path).write_text("\n".join(text) + "\n")
    return rows + out


def record_of(p: Patched, *, bodies: int) -> dict:
    """What the shell run's record keeps of the patches."""
    c = p.joined.counts
    return {"patches": p.patches, "patched_regions": sum(int(x.get("count") or 1) for x in p.patches),
            "solid_elements": int(c.get("solid_elements") or 0), "solid_nodes": int(c.get("solid_nodes") or 0),
            "overlap_elements": int(c.get("overlap_elements") or 0), "ties": int(c.get("ties") or 0),
            "links": int(c.get("links") or 0), "patch_words": words(p.patches, bodies=bodies),
            "patch_seconds": p.seconds, **({"left": p.left} if p.left else {})}


__all__ = ["RULE", "SHELL_ONLY", "Patched", "Region", "Stop", "build", "left_words", "record_of", "shell_only",
           "solid_rows", "trim", "words", "write_msh"]
