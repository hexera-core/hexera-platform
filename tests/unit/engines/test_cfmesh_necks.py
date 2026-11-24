# Responsibility: Verify INS-32 - when gap sizing cannot afford every narrow spot, the spots an opening reaches the rest of
# its passage only through are sized first, at their own width / 3, and a plan that cannot fit even those stops before
# meshing with the opening, the gap's width and what the user can change. abc 63401: a 0.25 mm slit got 0.425 mm cells
# (the half-budget fallback), cfMesh kept the larger region and two outlets came back with no faces.
from __future__ import annotations

import json

import numpy as np

import meshpipeline.engines.gap_sizing as G
from meshpipeline.cad.inside_space import opening_necks

MM = 1e-3
# a chamber (x 0-10 mm) joined to a pocket (x 12-20 mm) only by a slit 0.3 mm high (x 10-12, y 4.85-5.15 mm);
# the inlet is the chamber's x = 0 face, the outlet the pocket's x = 20 face
XS = [0.0, 10.0, 12.0, 20.0]
YS = [0.0, 4.85, 5.15, 10.0]
ZS = [0.0, 10.0]


def _filled(i: int, j: int, k: int) -> bool:
    return i != 1 or j == 1


def _surface() -> dict:
    """{patch: triangles (n, 3, 3) in metres}: the boundary of the filled lattice cells."""
    out: dict = {"wall": [], "inlet": [], "outlet": []}
    nx, ny, nz = len(XS) - 1, len(YS) - 1, len(ZS) - 1
    axes = (XS, YS, ZS)
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                if not _filled(i, j, k):
                    continue
                for d in range(3):
                    for side in (0, 1):
                        q = [i, j, k]
                        q[d] += 1 if side else -1
                        inside = (0 <= q[0] < nx and 0 <= q[1] < ny and 0 <= q[2] < nz and _filled(*q))
                        if inside:
                            continue
                        lo = [XS[i], YS[j], ZS[k]]
                        hi = [XS[i + 1], YS[j + 1], ZS[k + 1]]
                        plane = (hi if side else lo)[d]
                        a, b = [e for e in range(3) if e != d]
                        corners = []
                        for ua, ub in ((0, 0), (1, 0), (1, 1), (0, 1)):
                            p = [0.0, 0.0, 0.0]
                            p[d] = plane
                            p[a] = (hi if ua else lo)[a]
                            p[b] = (hi if ub else lo)[b]
                            corners.append(p)
                        name = ("inlet" if d == 0 and plane == XS[0] else
                                "outlet" if d == 0 and plane == XS[-1] else "wall")
                        c = np.asarray(corners) * MM
                        out[name] += [c[[0, 1, 2]], c[[0, 2, 3]]]
    return {n: np.asarray(v) for n, v in out.items()}


def _write_stl(path, solids: dict) -> None:
    with open(path, "w") as fh:
        for name, tris in solids.items():
            fh.write(f"solid {name}\n")
            for t in tris:
                fh.write("facet normal 0 0 0\nouter loop\n")
                for p in t:
                    fh.write(f"vertex {p[0]:.9g} {p[1]:.9g} {p[2]:.9g}\n")
                fh.write("endloop\nendfacet\n")
            fh.write(f"endsolid {name}\n")


SLIT_BOX = {"min": [10.0 * MM, 4.7 * MM, 0.0], "max": [12.0 * MM, 5.3 * MM, 10.0 * MM], "radius_m": 0.15 * MM}


def _hairlines(n: int = 12) -> list[dict]:
    """Hairline spots along the chamber's floor corner: narrow, but no opening depends on them."""
    out = []
    for z in np.linspace(1.0, 9.0, n):
        out.append({"min": [1.0 * MM, 0.0, (z - 0.2) * MM], "max": [9.0 * MM, 0.05 * MM, (z + 0.2) * MM],
                    "radius_m": 0.007 * MM})
    return out


def test_the_opening_behind_a_slit_needs_the_slit_and_not_the_hairlines():
    spots = [*_hairlines(), dict(SLIT_BOX)]
    got = opening_necks(_surface(), "wall", spots, need_m=3.0 * 1.0 * MM)
    assert [r["box"] for r in got] == [len(spots) - 1]
    assert got[0]["openings"] == ["outlet"]
    assert abs(got[0]["width_m"] - 0.3 * MM) < 1e-9


def test_an_opening_joined_wide_needs_no_spot():
    spots = [*_hairlines(), dict(SLIT_BOX)]
    solids = {n: t for n, t in _surface().items() if n != "outlet"}
    solids["outlet"] = solids.pop("inlet")          # the only opening sits on the chamber
    assert opening_necks(solids, "wall", spots, need_m=3.0 * MM) == []


def _field():
    """(points, radius, area per point) of the wall: wide everywhere, the slit's two walls 0.15 mm from their middle,
    and hairline spots along the chamber's floor corner that cost far more than any limit."""
    pts, r = [], []
    for x in np.linspace(0.5, 19.5, 20):
        for z in np.linspace(0.5, 9.5, 10):
            if not 10.0 < x < 12.0:
                pts += [[x, 0.0, z], [x, 10.0, z]]
                r += [2.5, 2.5]
    for x in np.linspace(10.1, 11.9, 8):
        for z in np.linspace(0.2, 9.8, 25):
            pts += [[x, 4.85, z], [x, 5.15, z]]
            r += [0.15, 0.15]
    for x in np.linspace(1.0, 9.0, 20):
        for z in np.linspace(1.0, 9.0, 20):
            for y in (0.02, 1.0, 2.0, 3.0, 4.0):
                pts.append([x, y, z])
                r.append(0.007)
    p = np.asarray(pts) * MM
    return p, np.asarray(r) * MM, np.full(len(p), 0.25 * MM * MM)


def _render(ws, budget: int):
    from meshpipeline.engines.cfmesh.cfmesh_runner import render_cfmesh_case
    _write_stl(ws / "geom.stl", _surface())
    return render_cfmesh_case(
        ws, surface_file="geom.stl", wall_patch="wall", patches=[], body_bbox=((0, 0, 0), (0.02, 0.01, 0.01)),
        L=0.02, domain_min=(0, 0, 0), domain_max=(0.02, 0.01, 0.01), strategy={"n_layers": 0},
        cell_budget=budget, passage_radius={"p05": 0.0025, "median": 0.0025, "band": 0.0025},
        passage_field=_field())


def _slit_block(ws) -> float | None:
    """The cell size of the meshDict's refinement box that lies across the slit's middle, or None."""
    text = (ws / "system" / "meshDict").read_text()
    best = None
    for blk in text.split("narrowPassage")[1:]:
        def num(key):
            return float(blk.split(key)[1].split(";")[0].replace("(", " ").split()[0])
        c = [float(v) for v in blk.split("centre")[1].split("(")[1].split(")")[0].split()]
        half = [0.5 * num("lengthX"), 0.5 * num("lengthY"), 0.5 * num("lengthZ")]
        mid = (11.0 * MM, 5.0 * MM, 5.0 * MM)
        if all(abs(mid[k] - c[k]) <= half[k] for k in range(3)) and half[1] >= 0.15 * MM:
            size = num("cellSize")
            best = size if best is None else min(best, size)
    return best


def test_the_slit_gets_its_cells_the_hairlines_do_not_and_the_plan_stays_under_the_limit(tmp_path):
    ws = tmp_path / "case"
    ws.mkdir()
    out = _render(ws, 100_000)
    assert json.loads((ws / G.GAP_RECORD).read_text())["status"] == G.OVER_LIMIT
    assert "neck_stop" not in out
    size = _slit_block(ws)
    assert size is not None and size <= 0.3 * MM / 3.0 + 1e-12     # three cells across the 0.3 mm slit
    # the slit is measured as several boxes along it: each one the outlet needs, none of the hairlines
    assert out["neck_spots"] and all(s["openings"] == ["outlet"] and s["width_mm"] == 0.3
                                     for s in out["neck_spots"])
    assert all(r["cell_size"] > 0.007 * MM for r in out["narrow_regions"])   # no hairline at its own count
    assert 0 < out["planned_cells"] <= 100_000


def test_when_not_even_the_slit_fits_the_plan_stops_naming_the_opening_and_the_gap(tmp_path):
    ws = tmp_path / "case"
    ws.mkdir()
    out = _render(ws, 8_000)
    stop = out.get("neck_stop") or ""
    assert stop.startswith("outlet reaches the rest of its passage only through a gap about 0.3 mm wide")
    assert "Widen or close that gap in the CAD, allow more cells, or drop that opening." in stop


# --- INS-32 (E11): the needed spot is priced tight round the gap, and coarsened to 2 across before a stop ---
# gap sizing groups spots in boxes much larger than the gap (abc 63401: 8 boxes at y 156-169 mm round a slit,
# 15.9 M cells at 0.033 mm against 8 M). This box holds the slit and a wide share of both chambers.
GROUPED_BOX = {"min": [6.0 * MM, 2.0 * MM, 0.0], "max": [16.0 * MM, 8.0 * MM, 10.0 * MM], "radius_m": 0.15 * MM}


def _volume(b: dict) -> float:
    return float(np.prod([b["max"][k] - b["min"][k] for k in range(3)]))


def test_a_needed_spot_reads_the_gap_s_own_extent_not_its_grouped_box():
    got = opening_necks(_surface(), "wall", [dict(GROUPED_BOX)], need_m=3.0 * MM)
    assert [r["box"] for r in got] == [0]
    lo, hi = got[0]["gap_min"], got[0]["gap_max"]
    # the slit runs x 10-12, y 4.85-5.15 mm, the whole z depth: its extent, within three grid cubes (0.75 mm here)
    assert 8.5 * MM <= lo[0] and hi[0] <= 13.5 * MM
    assert 2.9 * MM <= lo[1] and hi[1] <= 7.1 * MM
    assert lo[2] <= 1.0 * MM and hi[2] >= 9.0 * MM


def test_the_needed_spot_is_boxed_tight_round_the_gap_and_fits_where_its_grouped_box_did_not():
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_plan
    from meshpipeline.engines.gap_sizing import box_cost
    grouped_cost = _volume(GROUPED_BOX) / (0.1 * MM) ** 3       # the whole grouped box at width / 3: 600 k
    boxes, stop = neck_plan([_surface()], "wall", [dict(GROUPED_BOX)], wall_cell=1.0 * MM, base=0.0,
                            limit=200_000)
    assert stop == "" and len(boxes) == 1
    b = boxes[0]
    assert b["tight"] and b["cells_across"] == 3.0 and abs(b["cell_m"] - 0.1 * MM) < 1e-12
    cost = box_cost(boxes, 1.0 * MM, whole_box=True)
    assert cost <= 200_000 < grouped_cost
    # the box still covers the slit, padded by two of its cells
    assert b["min"][0] <= 10.0 * MM - 0.2 * MM and b["max"][0] >= 12.0 * MM + 0.2 * MM
    assert b["min"][1] <= 4.85 * MM - 0.2 * MM and b["max"][1] >= 5.15 * MM + 0.2 * MM


def test_needed_spots_that_do_not_fit_are_coarsened_towards_two_across_before_the_plan_stops():
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_plan
    from meshpipeline.engines.gap_sizing import box_cost
    at3, _ = neck_plan([_surface()], "wall", [dict(GROUPED_BOX)], wall_cell=1.0 * MM, base=0.0, limit=1e12)
    c3 = box_cost(at3, 1.0 * MM, whole_box=True)
    boxes, stop = neck_plan([_surface()], "wall", [dict(GROUPED_BOX)], wall_cell=1.0 * MM, base=0.0,
                            limit=0.6 * c3)
    assert stop == ""
    across = boxes[0]["cells_across"]
    assert 2.0 <= across < 3.0 and abs(boxes[0]["cell_m"] - 0.3 * MM / across) < 1e-12
    assert box_cost(boxes, 1.0 * MM, whole_box=True) <= 0.6 * c3
    # not even 2 across fits: the plan stops, saying so, and never goes under 2 across
    boxes, stop = neck_plan([_surface()], "wall", [dict(GROUPED_BOX)], wall_cell=1.0 * MM, base=0.0,
                            limit=0.05 * c3)
    assert boxes[0]["cells_across"] == 2.0
    assert stop.startswith("outlet reaches the rest of its passage only through a gap about 0.3 mm wide")
    assert "even at 2 cells across the gap" in stop


def test_the_plan_stop_reaches_the_user_s_result_with_the_opening_the_gap_and_the_three_ways_out(tmp_path):
    # E10 wrote the stop as a PATCH_NOT_CAPTURED refusal with no words of its own: the user read "A boundary you
    # declared came out of the mesher with no faces ... run it again" from a plan that never started a mesher
    from meshpipeline.application.final_result import _cause_account
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_stop_refusal
    from meshpipeline.engines.gates import cause_of, facts_of
    from meshpipeline.engines.preflight import read_refusal
    ws = tmp_path / "case"
    ws.mkdir()
    stop = _render(ws, 8_000)["neck_stop"]
    neck_stop_refusal(stop).write(ws)
    fb = read_refusal(ws).as_feedback()
    assert facts_of(fb)["port_region_dropped"] and facts_of(fb)["before_meshing"]   # still no wrap (LAD-cfd-12)
    cause, what, next_step = _cause_account("finalize", cause_of(fb), facts_of(fb), "cfmesh")
    assert cause == "patch_not_captured"
    assert what.startswith("The plan stopped before meshing: outlet reaches the rest of its passage only through "
                           "a gap about 0.3 mm wide")
    assert "came out of the mesher" not in what and "run it again" not in next_step
    assert next_step == "Widen or close that gap in the CAD, allow more cells, or drop that opening."


def _two_chambers_one_with_no_opening() -> dict:
    """The same chamber + slit + pocket, but both openings on the chamber (its x = 0 face split in two) and the
    pocket's x = 20 face wall: the pocket is a sizeable passage piece no opening is on (abc 99768's kind: cfMesh
    keeps the largest region, '61 unconnected regions')."""
    s = _surface()
    inlet = np.asarray(s.pop("inlet"))
    s["wall"] = np.concatenate([s["wall"], s.pop("outlet")])
    low = inlet[:, :, 1].mean(axis=1) < 5.0 * MM            # the x = 0 face split at y = 5 mm
    s["inlet"], s["outlet"] = inlet[low], inlet[~low]
    return s


def test_a_passage_piece_cut_off_by_a_slit_needs_the_slit_with_no_opening_named():
    spots = [*_hairlines(), dict(SLIT_BOX)]
    got = opening_necks(_two_chambers_one_with_no_opening(), "wall", spots, need_m=3.0 * MM)
    assert [r["box"] for r in got] == [len(spots) - 1]
    assert got[0]["openings"] == [None]                     # the piece, not an opening
    assert abs(got[0]["width_m"] - 0.3 * MM) < 1e-9


def test_a_passage_piece_the_limit_cannot_reach_stops_the_plan_saying_a_piece_of_the_passage():
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_plan
    spots = [*_hairlines(), dict(SLIT_BOX)]
    boxes, stop = neck_plan([_two_chambers_one_with_no_opening()], "wall", spots, wall_cell=1.0 * MM, base=0.0,
                            limit=1_000)
    assert [b["neck_of"] for b in boxes] == [[None]]
    assert stop.startswith("A piece of the passage reaches the rest of its passage only through a gap about 0.3 mm")


# INS-34: the walk reads as many narrow spots in a row as the opening needs - abc 63401's outlet_3 needed nine and a
# cap of eight left the ninth (a 1.6 mm gap) unboxed at 1.9 cells across; cfMesh cut it and dropped outlet_3

def _chain_xs(n: int) -> list[float]:
    """A chamber (x 0-30 mm) then `n` slits (2 mm long) each into a small pocket (3.6 mm) - the outlet on the last."""
    xs = [0.0, 30.0]
    for _ in range(n):
        xs += [xs[-1] + 2.0, xs[-1] + 5.6]
    return xs


def test_an_opening_behind_nine_slits_in_a_row_needs_all_nine(monkeypatch):
    import sys
    n = 9
    xs = _chain_xs(n)
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "XS", xs)
    monkeypatch.setattr(mod, "_filled", lambda i, j, k: i % 2 == 0 or j == 1)   # odd columns: the slit only
    spots = [{"min": [xs[2 * s + 1] * MM, 4.7 * MM, 0.0], "max": [xs[2 * s + 2] * MM, 5.3 * MM, 10.0 * MM],
              "radius_m": 0.15 * MM} for s in range(n)]
    got = opening_necks(_surface(), "wall", spots, need_m=3.0 * MM)
    assert sorted(r["box"] for r in got) == list(range(n))           # 8 on the old fixed cap of 8 steps
    assert all(r["openings"] == ["outlet"] for r in got)


def test_two_spots_the_walk_reads_as_touching_leave_no_hole_in_the_mesh_boxes():
    # INS-38: the slit (x 10-12 mm) measured as two spots 0.5 mm apart, less than one of the walk's cubes (0.75 mm
    # at need 3 mm). The walk reads them as one chain; the mesh boxes must chain too, or plain cells sit across the
    # slit in the hole (abc 63401: 0.1 mm slit boxes 0.72 mm apart along y, outlet_3 came out with no faces).
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_plan
    spots = [{"min": [10.0 * MM, 4.7 * MM, 0.0], "max": [10.75 * MM, 5.3 * MM, 10.0 * MM], "radius_m": 0.15 * MM},
             {"min": [11.25 * MM, 4.7 * MM, 0.0], "max": [12.0 * MM, 5.3 * MM, 10.0 * MM], "radius_m": 0.15 * MM}]
    got = opening_necks(_surface(), "wall", spots, need_m=3.0 * MM)
    assert sorted(r["box"] for r in got) == [0, 1]
    boxes, stop = neck_plan([_surface()], "wall", spots, wall_cell=1.0 * MM, base=0.0, limit=1e12)
    assert not stop
    # along the slit (x 10-12 mm) the boxes leave no stretch of plain cells: was a 0.1 mm hole at x 10.95-11.05 mm
    covered = 10.0 * MM
    for bx in sorted(boxes, key=lambda q: q["min"][0]):
        if bx["min"][0] <= covered + 1e-12 and bx["min"][1] <= 4.85 * MM and bx["max"][1] >= 5.15 * MM:
            covered = max(covered, bx["max"][0])
    assert covered >= 12.0 * MM
    bridge = [bx for bx in boxes if bx.get("bridge")]
    assert len(bridge) == 1 and bridge[0]["rule"] == "INS-38" and bridge[0]["neck_of"] == ["outlet"]
    assert bridge[0]["max"][0] - bridge[0]["min"][0] < 1.0 * MM    # the hole and its padding, not the whole slit
    assert abs(bridge[0]["cell_m"] - 0.1 * MM) < 1e-12               # at the gap's own cell (0.3 mm / 3)


def _built_without(ws, counts: dict, spots: list) -> None:
    """A case built with every passage's openings, `counts` its boundary faces, `spots` the plan's needed spots."""
    from meshpipeline.cad.fluid_passages import PASSAGES_FILE
    from meshpipeline.engines.cfmesh.cfmesh_runner import NECK_SPOTS_FILE
    pm = ws / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    body = "".join(f"    {n}\n    {{\n        type patch;\n        nFaces {c};\n        startFace 0;\n    }}\n"
                   for n, c in counts.items())
    (pm / "boundary").write_text(f"FoamFile\n{{\n    format ascii;\n    class polyBoundaryMesh;\n}}\n"
                                 f"{len(counts)}\n(\n{body})\n")
    (ws / "system").mkdir()
    (ws / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"],
                                                "ports": [["inlet", "outlet_1"], ["outlet_2", "outlet_3"]]}))
    (ws / NECK_SPOTS_FILE).write_text(json.dumps(spots))


def test_a_built_mesh_that_lost_an_opening_ends_in_the_plan_stop_s_words_not_a_manifest_error(tmp_path):
    """INS-36 (abc 63401, c2e13_p34c): the mesh was built (8.19 M cells), outlet_3 had zero faces, and the run ended
    "[MANIFEST_VALIDATION_FAILED] ... zero faces: ['outlet_3']" - no gap, no ways out. Now: the plan-time stop's words
    (the opening, the narrowest gap on its way and where, what WAS built, the three ways out)."""
    from meshpipeline.application.final_result import _cause_account
    from meshpipeline.engines.cfmesh.cfmesh_runner import NECK_WAYS_OUT, after_build_refusal, neck_words
    from meshpipeline.engines.gates import cause_of, facts_of
    from meshpipeline.engines.preflight import read_refusal
    ws = tmp_path / "case"
    ws.mkdir()
    _built_without(ws, {"inlet": 18932, "outlet_1": 11548, "outlet_2": 6392, "outlet_3": 0, "wall": 9000},
                   [{"width_m": 1.6e-3, "where_m": [0.0, 0.01, 0.0], "openings": ["inlet"]},
                    {"width_m": 1.0e-4, "where_m": [-0.00998, 0.15628, -0.00802], "openings": ["outlet_3"]},
                    {"width_m": 1.63e-3, "where_m": [-0.0088, 0.1521, -0.0123], "openings": ["outlet_3"]}])
    ref = after_build_refusal(ws)
    assert ref is not None
    ref.write(ws)
    fb = read_refusal(ws).as_feedback()
    facts = facts_of(fb)
    assert facts["port_region_dropped"] and facts["after_build"] and not facts["before_meshing"]
    cause, what, next_step = _cause_account("finalize", cause_of(fb), facts, "cfmesh")
    assert cause == "patch_not_captured"
    head = neck_words(["outlet_3"], 1.0e-4, [-0.00998, 0.15628, -0.00802])
    assert what.startswith("The mesh was not delivered: " + head)
    assert "0.1 mm wide (near x -10.0, y 156.3, z -8.0 mm)" in what
    assert "In the mesh with its faces: inlet, outlet_1, outlet_2" in what
    assert "manifest" not in what.lower() and next_step == NECK_WAYS_OUT
    # every opening with faces: nothing to refuse
    ws2 = tmp_path / "all"
    ws2.mkdir()
    _built_without(ws2, {"inlet": 1, "outlet_1": 1, "outlet_2": 1, "outlet_3": 1}, [])
    assert after_build_refusal(ws2) is None


def test_the_plan_time_stop_and_the_after_build_refusal_share_one_sentence():
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_stop_refusal, neck_words
    words = neck_words(["outlet_3"], 1e-4)
    assert words == "outlet_3 reaches the rest of its passage only through a gap about 0.1 mm wide"
    plan, built = neck_stop_refusal(words + "."), neck_stop_refusal(words + ".", after_build=True)
    assert plan.facts["plan_stop"] == built.facts["plan_stop"] and plan.facts["ways_out"] == built.facts["ways_out"]
    assert plan.facts["before_meshing"] and not built.facts["before_meshing"]


def test_the_after_build_refusal_stands_over_the_mesh_on_disk_and_a_plan_stop_still_does_not():
    """INS-36: judged again, the mesh that lost outlet_3 ended as a manifest error; the refusal is the verdict."""
    from meshpipeline.engines.cfmesh.cfmesh_runner import neck_stop_refusal
    from meshpipeline.pipeline.executor import refusal_stands_over_mesh
    assert refusal_stands_over_mesh(neck_stop_refusal("x reaches ...", after_build=True))
    assert not refusal_stands_over_mesh(neck_stop_refusal("x reaches ..."))
    assert not refusal_stands_over_mesh(None)


def _single_passage(ws, counts: dict, types: dict, spots: list):
    from meshpipeline.engines.cfmesh.cfmesh_runner import NECK_SPOTS_FILE
    ws.mkdir()
    (ws / "system").mkdir()
    (ws / NECK_SPOTS_FILE).write_text(json.dumps(spots))
    return {"patch_types": types}, {n: c > 0 for n, c in counts.items()}


def test_a_single_passage_mesh_that_lost_an_opening_is_told_in_the_same_words(tmp_path):
    """INS-37: E14's INS-36 words reached only a case with a passages file; a single-passage cfMesh mesh that lost an
    opening still ended "these patches have zero faces". Same words, same functions; no gap invented when none was
    measured; the zero-face check's cause and facts (and so its retries) are unchanged."""
    from meshpipeline.application.final_result import _cause_account
    from meshpipeline.engines.cfmesh import flow_gates as fg
    from meshpipeline.engines.cfmesh.cfmesh_runner import LOST_OPENING_WAYS_OUT, NECK_WAYS_OUT, neck_words
    from meshpipeline.engines.gates import cause_of, facts_of, refuse

    def zero(empty):
        return refuse(f"mesh_manifest.json: these patches have zero faces: {empty}", "patch_not_captured",
                      patches=list(empty))
    types = {"inlet": "inlet", "outlet_1": "outlet", "outlet_2": "outlet", "wall": "wall"}
    counts = {"inlet": 10, "outlet_1": 0, "outlet_2": 5, "wall": 99}
    # no narrow spot measured: the opening and what is in the mesh, no gap named
    ws = tmp_path / "a"
    manifest, val = _single_passage(ws, counts, types, [])
    fb = fg._lost_openings_said(ws, manifest, val, ["outlet_1"], zero(["outlet_1"]))
    facts = facts_of(fb)
    assert facts["patches"] == ["outlet_1"] and cause_of(fb) == "patch_not_captured"
    assert "port_region_dropped" not in facts and "these patches have zero faces: ['outlet_1']" in str(fb)
    _, what, nxt = _cause_account("manifest_valid", cause_of(fb), facts, "cfmesh")
    assert what.startswith("The mesh was not delivered: the mesh was built, but outlet_1 came out with no faces")
    assert "gap" not in what and "In the mesh with its faces: inlet, outlet_2" in what and nxt == LOST_OPENING_WAYS_OUT
    assert "zero faces" not in what
    # a narrow spot measured on its way: neck_words, as INS-36
    ws = tmp_path / "b"
    manifest, val = _single_passage(ws, counts, types, [{"width_m": 1e-4, "where_m": [0, 0, 0],
                                                         "openings": ["outlet_1"]}])
    fb = fg._lost_openings_said(ws, manifest, val, ["outlet_1"], zero(["outlet_1"]))
    _, what, nxt = _cause_account("manifest_valid", cause_of(fb), facts_of(fb), "cfmesh")
    assert neck_words(["outlet_1"], 1e-4, [0, 0, 0]) in what and nxt == NECK_WAYS_OUT
    # a wall with no faces is not an opening: the zero-face check's own words stand
    fb0 = zero(["wall"])
    assert fg._lost_openings_said(ws, manifest, {**val, "wall": False}, ["wall"], fb0) is fb0
