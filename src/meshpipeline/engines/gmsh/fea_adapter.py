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
#: Distance sampling per face, per direction (gmsh's "Sampling"): enough points that the spacing is
#: about half the group's size, within these bounds.
SAMPLING_MIN, SAMPLING_MAX = 20, 300


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
        dist = gmsh.model.mesh.field.add("Distance")
        if g["edge"]:
            curves = sorted({c for a, b in (fs for fs in g["edges"] if len(fs) == 2)
                             if (ta := _tag(a)) in surfaces and (tb := _tag(b)) in surfaces
                             for c in _shared_curves(gmsh, ta, tb)})
            if not curves:
                gmsh.model.mesh.field.remove(dist)
                continue
            gmsh.model.mesh.field.setNumbers(dist, "CurvesList", curves)
            gmsh.model.mesh.field.setNumber(dist, "Sampling", SAMPLING_MAX)
        else:
            tags = [t for f in g["faces"] if (t := _tag(f)) in surfaces]
            if not tags:
                gmsh.model.mesh.field.remove(dist)
                continue
            gmsh.model.mesh.field.setNumbers(dist, "SurfacesList", tags)
            gmsh.model.mesh.field.setNumber(dist, "Sampling", _sampling(gmsh, tags, size))
        th = gmsh.model.mesh.field.add("Threshold")
        reach = float(g["reach"])
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


def mesh(workspace) -> int:
    """Mesh the staged solid by the plan. Exit codes as the driver's: 0 a valid mesh, 2 no
    geometry, 3 no solid, 4 fatal defects."""
    from meshpipeline.engines import fea_checks as checks
    from meshpipeline.engines.fea_plan import PLAN_FILE
    from meshpipeline.engines.gmsh import driver as D

    ws = Path(workspace)
    plan = json.loads((ws / PLAN_FILE).read_text())
    geom = ws / "geometry.step"
    if not geom.exists():
        print(f"[GMSH-FEA] geometry.step missing in {ws}", file=sys.stderr)
        return 2
    run = plan.get("mesh") or {}
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 1)
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
        gmsh.model.mesh.generate(3)
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
        low = int(sum(1 for q in held if q < D.SICN_FLOOR))
        aside_q = [q for t, q in zip(all_tags, qualities, strict=True) if int(t) in aside]
        fatal = []
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
            "fatal": fatal, "size_h": base, "quality_bar": "fea", "sicn_floor": D.SICN_FLOOR,
            "elements_under_floor": low,
            "bounds": D._final_node_bounds(gmsh),
            "groups": {"free": "free"}, "default_group": "free", "default_group_used": True,
            "deck": deck,
            # the worst figures, top level, where the production limits read them
            **{k: shape.get(k) for k in ("fea_min_scaled_jacobian", "fea_max_jacobian_ratio",
                                          "fea_max_aspect_ratio", "fea_min_dihedral_deg",
                                          "fea_max_dihedral_deg", "fea_inverted") if k in shape},
            "fea_rules_short": len(rules.get("short") or []) if rules else None,
            # THE PLAN'S RULES AS MET (engines/fea_checks.py): the gate reads the through-wall
            # count in place of the flow floor (engines/gmsh/gates.py)
            "fea": {"plan": True, "detail": (plan.get("detail") or {}).get("key"),
                    "estimate": (plan.get("estimate") or {}).get("elements"),
                    # PLAN-fea-8: how the level was chosen, and what a best-possible plan leaves short
                    **({"auto_level": plan["auto_level"]} if plan.get("auto_level") else {}),
                    **({"best_possible": plan["best_possible"],
                        "resolution_short": list(plan.get("resolution_short") or [])}
                       if plan.get("best_possible") else {}),
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


__all__ = ["ALGORITHMS_3D", "mesh", "size_fields"]
