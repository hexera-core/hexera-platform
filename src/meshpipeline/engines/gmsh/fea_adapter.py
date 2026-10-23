# Responsibility: gmsh as an FEA engine adapter: mesh the staged CAD solid exactly as the FEA plan (fea_plan.json) says - its base size, its zones as gmsh size fields, its lever settings - and write the deck, the quality record and the plan's rules as measured on the mesh.
# Boundaries: plan in, mesh out. It decides no size and pulls no lever (engines/fea_plan.py and engines/fea_ladder.py do); it runs inside the mesh runner, called by engines/gmsh/driver.main when the workspace holds a plan.
# Collaborates with: engines/fea_size_field.group_zones (the same zone groups the estimate read), engines/fea_checks.py (the rules measured), engines/gmsh/driver.py (deck writer, quality helpers), engines/gmsh/abaqus_deck.py.
"""THE PLAN, MESHED BY GMSH.

Each zone group becomes a Distance field (to its CAD faces, or to the edge two faces share for a
capped corner) and a Threshold on it: the group's size out to its reach, then growing at the
plan's rate to the base size. The smallest of them is the background field, and nothing else
sizes the mesh (no curvature sizing, no sizes carried in from points or boundaries), so the mesh
is the plan's. One thread and a fixed seed, as every gmsh run here: the same plan gives the same
mesh.

Face_k in the plan is the k-th CAD face (cad/fea_measure numbers them in OpenCASCADE's order),
which is gmsh's surface tag k for the staged solid, and Face_k in the deck.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

#: 3D algorithms the plan may name (gmsh's Mesh.Algorithm3D numbers).
ALGORITHMS_3D = {"delaunay": 1, "hxt": 10}
#: gmsh's MeshAdapt 2D algorithm (Mesh.Algorithm 1): the other way to mesh a surface, for the
#: surfaces a pass went wrong on (LAD-fea-6) - the default (Frontal-Delaunay) left them empty or bad
MESH_ADAPT = 1
#: at most this many invalid elements are traced back to the surfaces they touch
TRACE_BAD = 500
#: the line the driver reads the surfaces a failed pass went wrong on from (LAD-fea-6)
BAD_SURFACES = "[GMSH-FEA] BAD SURFACES:"
#: Distance sampling per face, per direction (gmsh's "Sampling"): enough points that the spacing is
#: about half the group's size, within these bounds.
SAMPLING_MIN, SAMPLING_MAX = 20, 300
#: The most points one Distance field samples (faces x Sampling^2). gmsh samples every face of a
#: field at the one Sampling the field's largest face needs: a perforated plate's 1,467 hole and
#: plate faces at 300 x 300 came to 132M points and ran the mesh job out of memory before any
#: surface was meshed (fea0260). Past this, the faces are split into fields sampled at their own
#: size (the same sizes: the smallest of thresholds over parts is the threshold over the whole).
MAX_FIELD_POINTS = 2_000_000


def _tag(name: str) -> int | None:
    try:
        return int(str(name).rsplit("_", 1)[-1])
    except ValueError:
        return None


def _shared_curves(gmsh, a: int, b: int) -> list[int]:
    ca = {abs(t) for _d, t in gmsh.model.getBoundary([(2, a)], oriented=False)}
    cb = {abs(t) for _d, t in gmsh.model.getBoundary([(2, b)], oriented=False)}
    return sorted(ca & cb)


def _sampling(gmsh, tags: list[int], size: float) -> int:
    longest = 0.0
    for t in tags:
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, t)
        longest = max(longest, x1 - x0, y1 - y0, z1 - z0)
    n = math.ceil(longest / max(size / 2.0, 1e-12)) if longest > 0 else SAMPLING_MIN
    return int(min(SAMPLING_MAX, max(SAMPLING_MIN, n)))


def sampled_parts(gmsh, tags: list[int], size: float) -> list[tuple[list[int], int]]:
    """(faces, Sampling) for each Distance field a zone group's faces need: one field as before when
    it stays within MAX_FIELD_POINTS; else each face at its own Sampling, in fields of at most
    MAX_FIELD_POINTS points."""
    n = _sampling(gmsh, tags, size)
    if len(tags) * n * n <= MAX_FIELD_POINTS:
        return [(tags, n)]
    by: dict[int, list[int]] = {}
    for t in tags:
        by.setdefault(_sampling(gmsh, [t], size), []).append(t)
    out: list[tuple[list[int], int]] = []
    for k in sorted(by):
        per = max(1, MAX_FIELD_POINTS // (k * k))
        out.extend((by[k][i:i + per], k) for i in range(0, len(by[k]), per))
    return out


def _micro_place(m: dict) -> dict:
    """A micro detail as the delivery names it: where (its middle point, mm), what and how small."""
    pts = m.get("points_m") or [[0.0, 0.0, 0.0]]
    mid = pts[len(pts) // 2]
    return {"at_mm": [round(float(v) * 1000.0, 2) for v in mid], "kind": m.get("kind"),
            "size_mm": m.get("size_mm"), "faces": list(m.get("faces") or [])}


def size_fields(gmsh, plan: dict, surfaces: set[int]) -> int | None:
    """The plan's zones as gmsh fields; returns the background field (None when no zone binds)."""
    from meshpipeline.engines.fea_size_field import group_zones

    base = float(plan["base_size_m"])
    g1 = float(plan.get("growth") or 1.3) - 1.0
    ids = []
    for g in group_zones(plan.get("zones") or []):
        size = float(g["size"])
        if size >= base:
            continue
        if g.get("balls"):
            # PLAN-fea-3's thin spots: the wall's Threshold off its face, held only inside the spots'
            # balls (each its size inside, growing at the plan's rate outside) - the larger of the two
            tags = [t for f in g["faces"] if (t := _tag(f)) in surfaces]
            if not tags:
                continue
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "SurfacesList", tags)
            gmsh.model.mesh.field.setNumber(dist, "Sampling", _sampling(gmsh, tags, size))
            th = gmsh.model.mesh.field.add("Threshold")
            reach = float(g["reach"])
            for key, val in (("InField", dist), ("SizeMin", size), ("SizeMax", base), ("DistMin", reach),
                             ("DistMax", reach + (base - size) / max(g1, 1e-6))):
                gmsh.model.mesh.field.setNumber(th, key, float(val))
            balls = []
            for x, y, z, r in g["balls"]:
                ball = gmsh.model.mesh.field.add("Ball")
                for key, val in (("XCenter", x), ("YCenter", y), ("ZCenter", z), ("Radius", r), ("VIn", size),
                                 ("VOut", base), ("Thickness", (base - size) / max(g1, 1e-6))):
                    gmsh.model.mesh.field.setNumber(ball, key, float(val))
                balls.append(ball)
            near = balls[0]
            if len(balls) > 1:
                near = gmsh.model.mesh.field.add("Min")
                gmsh.model.mesh.field.setNumbers(near, "FieldsList", balls)
            both = gmsh.model.mesh.field.add("Max")
            gmsh.model.mesh.field.setNumbers(both, "FieldsList", [th, near])
            ids.append(both)
            continue
        dists = []
        if g["edge"]:
            curves = sorted({c for a, b in (fs for fs in g["edges"] if len(fs) == 2)
                             if (ta := _tag(a)) in surfaces and (tb := _tag(b)) in surfaces
                             for c in _shared_curves(gmsh, ta, tb)})
            if not curves:
                continue
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "CurvesList", curves)
            gmsh.model.mesh.field.setNumber(dist, "Sampling", SAMPLING_MAX)
            dists.append(dist)
        else:
            tags = [t for f in g["faces"] if (t := _tag(f)) in surfaces]
            if not tags:
                continue
            for part, n in sampled_parts(gmsh, tags, size):
                dist = gmsh.model.mesh.field.add("Distance")
                gmsh.model.mesh.field.setNumbers(dist, "SurfacesList", part)
                gmsh.model.mesh.field.setNumber(dist, "Sampling", n)
                dists.append(dist)
        reach = float(g["reach"])
        for dist in dists:
            th = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(th, "InField", dist)
            gmsh.model.mesh.field.setNumber(th, "SizeMin", size)
            gmsh.model.mesh.field.setNumber(th, "SizeMax", base)
            gmsh.model.mesh.field.setNumber(th, "DistMin", reach)
            gmsh.model.mesh.field.setNumber(th, "DistMax", reach + (base - size) / max(g1, 1e-6))
            ids.append(th)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    m = gmsh.model.mesh.field.add("Min")
    gmsh.model.mesh.field.setNumbers(m, "FieldsList", ids)
    return m


def _empty_surfaces(gmsh, surfaces: set[int]) -> list[int]:
    out: list[int] = []
    for s in sorted(surfaces):
        _types, tags, _nodes = gmsh.model.mesh.getElements(2, s)
        if not any(len(t) for t in tags):
            out.append(s)
    return out


def surface_mesh(gmsh, surfaces: set[int], remesh: set[int]) -> list[int]:
    """The surface mesh, LAD-fea-6's surfaces on MeshAdapt; a surface the default algorithm leaves
    with no mesh is meshed again the other way at once, in the same pass. Returns the surfaces
    still left with no mesh."""
    for s in sorted(remesh & surfaces):
        gmsh.model.mesh.setAlgorithm(2, s, MESH_ADAPT)
    gmsh.model.mesh.generate(2)
    empty = _empty_surfaces(gmsh, surfaces)
    again = [s for s in empty if s not in remesh]
    if again:
        print(f"[GMSH-FEA] surface(s) {again} left with no mesh - meshed again with MeshAdapt (LAD-fea-6)",
              file=sys.stderr)
        for s in again:
            gmsh.model.mesh.setAlgorithm(2, s, MESH_ADAPT)
        gmsh.model.mesh.clear()
        gmsh.model.mesh.generate(2)
        empty = _empty_surfaces(gmsh, surfaces)
    return empty


def surfaces_named(text: str) -> list[int]:
    """The surface tags a gmsh error names ("... on surface 8", "No elements in surface 9")."""
    import re

    return sorted({int(m) for m in re.findall(r"surface (\d+)", text or "")})


def bad_surfaces(gmsh, tags, qualities) -> list[int]:
    """The CAD surfaces the invalid elements (quality not above 0) touch: their nodes on a surface,
    or on a curve or corner of one - where LAD-fea-6 meshes the next pass the other way."""
    out: set[int] = set()
    bad = [int(t) for t, q in zip(tags, qualities, strict=True) if q <= 0.0][:TRACE_BAD]
    for t in bad:
        _etype, nodes, _dim, _tag = gmsh.model.mesh.getElement(t)
        for n in nodes:
            _xyz, _par, dim, ent = gmsh.model.mesh.getNode(int(n))
            if dim == 2:
                out.add(int(ent))
            elif dim in (0, 1):
                up = [int(c) for c in gmsh.model.getAdjacencies(dim, int(ent))[0]]
                if dim == 0:
                    up = [int(s) for c in up for s in gmsh.model.getAdjacencies(1, c)[0]]
                out.update(up)
    return sorted(out)


def _peak_rss_mb() -> float | None:
    """This process's peak resident memory (MB), or None where the platform does not say. Read as
    /proc's VmHWM, which starts afresh at exec: getrusage's ru_maxrss carries the parent's peak
    across fork and exec, so a mesher started by a planner that peaked at 10 GB read 10 GB."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return round(float(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError):
        return None
    return None


def mesh(workspace) -> int:
    """Mesh the staged solid by the plan. Exit codes as the driver's: 0 a valid mesh, 2 no
    geometry, 3 no solid, 4 fatal defects."""
    from meshpipeline.engines import fea_checks as checks
    from meshpipeline.engines.fea_plan import PLAN_FILE, STAGED_SOLIDS
    from meshpipeline.engines.gmsh import driver as D

    ws = Path(workspace)
    plan = json.loads((ws / PLAN_FILE).read_text())
    # PLAN-fea-15: the staged solid the plan was measured on (gmsh's own B-rep first)
    geom = next((ws / n for n in STAGED_SOLIDS if (ws / n).exists()), ws / "geometry.step")
    if not geom.exists():
        print(f"[GMSH-FEA] geometry.step missing in {ws}", file=sys.stderr)
        return 2
    run = plan.get("mesh") or {}
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 1)
        # ONE THREAD, KEPT (fix pack, 22 Oct): at 8 threads three of five lab parts meshed twice came out
        # different (cell counts too) - gmsh's parallel curve and surface meshing is not repeatable,
        # and the 3D fill and the optimiser are serial anyway
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Mesh.RandomSeed", 1)
        gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
        gmsh.model.add("fea")
        gmsh.model.occ.importShapes(str(geom))
        gmsh.model.occ.synchronize()
        vols = [t for _, t in gmsh.model.getEntities(3)]
        if not vols:
            print("[GMSH-FEA] no solid volume in geometry.step - an FEA mesh needs a closed solid",
                  file=sys.stderr)
            return 3
        surfaces = {t for _, t in gmsh.model.getEntities(2)}
        gmsh.model.addPhysicalGroup(3, vols, name="solid")
        gmsh.model.addPhysicalGroup(2, sorted(surfaces), name="free")
        base = float(plan["base_size_m"])
        gmsh.option.setNumber("Mesh.MeshSizeMax", base)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.Algorithm3D", ALGORITHMS_3D.get(str(run.get("algorithm3d") or "delaunay"), 1))
        bg = size_fields(gmsh, plan, surfaces)
        if bg is not None:
            gmsh.model.mesh.field.setAsBackgroundMesh(bg)
        remesh = {int(s) for s in run.get("remesh_surfaces") or []}
        empty = surface_mesh(gmsh, surfaces, remesh)
        try:
            gmsh.model.mesh.generate(3)
        except Exception as exc:
            # the surfaces the fill failed on, for the next pass (LAD-fea-6): the driver reads them
            # from this line
            named = sorted(set(empty) | {s for s in surfaces_named(str(exc)) if s in surfaces})
            print(f"{BAD_SURFACES} {','.join(str(s) for s in named)}", file=sys.stderr)
            raise
        for _ in range(int(run.get("optimise_passes") or 1)):
            gmsh.model.mesh.optimize("Netgen")
        order = int((plan.get("element") or {}).get("order") or 2)
        settled: dict = {"straightened": 0, "set_aside": [], "cusp_places_m": [], "cusp_elements": 0,
                         "micro_used": [], "micro_elements": 0}
        if order > 1:
            from meshpipeline.engines.gmsh import fea_settle

            # each element's straight shape, before curving: what PLAN-fea-11 compares against
            st_tags, st_q = fea_settle.qualities(gmsh)
            straight = dict(zip(st_tags.tolist(), st_q.tolist(), strict=True))
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)
            gmsh.model.mesh.setOrder(order)
            # PLAN-fea-11 (never worse than straight) and PLAN-fea-10 (the cusps set aside)
            settled = fea_settle.settle(gmsh, straight, [c["at_m"] for c in plan.get("cusps") or []],
                                        D.SICN_FLOOR, micro=plan.get("micro") or [])

        etypes, etags, _ = gmsh.model.mesh.getElements(3)
        all_tags = [t for arr in etags for t in arr]
        qualities = gmsh.model.mesh.getElementQualities(all_tags, "minSICN") if all_tags else []
        n_elem = len(all_tags)
        n_nodes = len(gmsh.model.mesh.getNodes()[0])
        aside = set(settled["set_aside"])
        held = [q for t, q in zip(all_tags, qualities, strict=True) if int(t) not in aside]
        # THE FLOOR holds every element but the ones set aside at a cusp (PLAN-fea-10); validity
        # (no degenerate element) holds every element without exception
        min_sicn = float(min(held)) if len(held) else (float(min(qualities)) if len(qualities) else 0.0)
        min_all = float(min(qualities)) if len(qualities) else 0.0
        # THE NOTE FLOOR: SICN 0.1 for straight elements, QUADRATIC_SCALED_JACOBIAN (0.01) for curved
        # ones - a note either way; validity (above 0) is the only blocker
        from meshpipeline.engines.fea_quality import QUADRATIC_SCALED_JACOBIAN
        note_floor = QUADRATIC_SCALED_JACOBIAN if order > 1 else D.SICN_FLOOR
        low = int(sum(1 for q in held if q < note_floor))
        aside_q = [q for t, q in zip(all_tags, qualities, strict=True) if int(t) in aside]
        fatal = []
        # LAD-fea-6: where the invalid elements are, for the next pass
        traced = bad_surfaces(gmsh, all_tags, qualities) if min_all <= 0.0 else []
        if n_elem == 0:
            fatal.append("no volume elements generated")
        if min_all <= 0.0:
            fatal.append("degenerate elements (SICN <= 0)")
        deck = D._write_deck(gmsh, ws, discrete=False)
        gmsh.write(str(ws / "mesh.msh"))
        rules: dict = {}
        shape: dict = {}
        solid = None
        try:
            from meshpipeline.engines import fea_quality
            from meshpipeline.engines.gmsh.abaqus_deck import from_gmsh
            solid = from_gmsh(gmsh, sorted(vols))
            rules = checks.measure(solid, plan)
            # THE ELEMENTS AS THE SOLVERS JUDGE THEM (engines/fea_quality.py): the gate refuses an
            # inverted element (VER-1); the verdict holds the rest to the production limits (VER-2),
            # the elements set aside at a cusp apart (PLAN-fea-10: reported on their own)
            shape = fea_quality.measure(solid, set_aside=sorted(aside))
        except Exception as exc:  # noqa: BLE001 - evidence; an unmeasured row decides as unmeasured
            print(f"[GMSH-FEA] plan rules or element quality not measured: {exc}", file=sys.stderr)
        # MESH ONCE, EXPORT MANY (design 8): the Nastran and Ansys files from the same mesh, with the
        # deck's numbers and face names; the user picks one at download
        exports: dict = {}
        try:
            from meshpipeline.contracts.mesh_units import COMPLETED_MESH_UNIT
            from meshpipeline.engines import fea_writers
            if solid is None:
                raise ValueError("no internal mesh was read")
            exports = fea_writers.write_all(ws, solid, cad_names=deck.get("cad_face_names") or {},
                                            units=COMPLETED_MESH_UNIT.value, source="Hexera gmsh engine")
        except Exception as exc:  # noqa: BLE001 - the deck stands; a missing file is offered as missing
            print(f"[GMSH-FEA] Nastran / Ansys files not written: {exc}", file=sys.stderr)
        if shape.get("fea_inverted"):
            fatal.append(f"{shape['fea_inverted']} inverted element(s) (Jacobian not above zero)")
        tw = (rules.get("through_wall") or {}).get("min")
        (ws / "quality.json").write_text(json.dumps({
            "cells": n_elem, "nodes": n_nodes, "element_order": order,
            "min_sicn": round(min_sicn, 4),
            # PLAN-fea-11 / PLAN-fea-10: elements given straight sides, and those set aside at a cusp
            "fea_straightened": int(settled["straightened"]),
            "fea_set_aside": {"rule": "PLAN-fea-10/PLAN-fea-12", "elements": len(aside),
                              "cusp_elements": int(settled["cusp_elements"]),
                              "micro_elements": int(settled["micro_elements"]),
                              "min_sicn": round(float(min(aside_q)), 4) if aside_q else None,
                              "places_mm": [[round(v * 1000.0, 2) for v in p] for p in settled["cusp_places_m"]],
                              "micro": [_micro_place(plan["micro"][i]) for i in settled["micro_used"]]},
            "sicn_low_fraction": round(low / n_elem, 6) if n_elem else 1.0,
            "fatal": fatal, "size_h": base, "quality_bar": "fea", "sicn_floor": note_floor,
            "elements_under_floor": low,
            "bounds": D._final_node_bounds(gmsh),
            "groups": {"free": "free"}, "default_group": "free", "default_group_used": True,
            "deck": deck,
            # the worst figures, top level, where the production limits read them
            **{k: shape.get(k) for k in ("fea_min_scaled_jacobian", "fea_max_jacobian_ratio",
                                          "fea_max_aspect_ratio", "fea_min_dihedral_deg",
                                          "fea_max_dihedral_deg", "fea_inverted") if k in shape},
            "fea_rules_short": len(rules.get("short") or []) if rules else None,
            "fea_bad_surfaces": traced,
            # PLAN-fea-16: the mesher's peak memory, for the bytes-per-element model
            "peak_rss_mb": _peak_rss_mb(),
            # THE PLAN'S RULES AS MET (engines/fea_checks.py): the gate reads the through-wall
            # count in place of the flow floor (engines/gmsh/gates.py)
            "fea": {"plan": True, "limit": (plan.get("limit") or {}).get("elements"),
                    "estimate": (plan.get("estimate") or {}).get("elements"),
                    # PLAN-fea-8: what a plan coarsened to fit leaves short
                    **({"coarsened": plan["coarsened"],
                        "resolution_short": list(plan.get("resolution_short") or [])}
                       if plan.get("coarsened") else {}),
                    "through_wall_min": tw, "rules": rules, "quality": shape, "exports": exports},
        }, indent=1))
        print(f"[GMSH-FEA] elements={n_elem} (planned ~{(plan.get('estimate') or {}).get('elements')}) "
              f"nodes={n_nodes} order={order} min_sicn={min_sicn:.3f} through_wall_min={tw} "
              f"short={rules.get('short')} worst_sj={shape.get('fea_min_scaled_jacobian')} "
              f"jac_ratio={shape.get('fea_max_jacobian_ratio')} aspect={shape.get('fea_max_aspect_ratio')} "
              f"dihedral={shape.get('fea_min_dihedral_deg')}-{shape.get('fea_max_dihedral_deg')}")
        return 0 if not fatal else 4
    finally:
        gmsh.finalize()


__all__ = ["ALGORITHMS_3D", "BAD_SURFACES", "MESH_ADAPT", "bad_surfaces", "mesh", "size_fields", "surface_mesh",
           "surfaces_named"]
