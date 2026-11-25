"""CFDREQ-34 / CFDREQ-35: a size typed with faces picked is built on those faces, and a size in a place is told
applied only when the built mesh measures it there."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.engines import cfd_picked as CP
from meshpipeline.engines import cfd_requirements as CR

HDR = 'FoamFile {{ version 2.0; format ascii; class {cls}; object {obj}; }}\n'


def _polymesh(case, n=4, h=1e-3):
    """n x n x 1 cubes of side h; the z=0 faces are the patch 'wall' (type wall), the rest 'sides'."""
    pid = lambda i, j, k: k * (n + 1) ** 2 + j * (n + 1) + i  # noqa: E731
    pts = [(i * h, j * h, k * h) for k in range(2) for j in range(n + 1) for i in range(n + 1)]
    cid = lambda i, j: j * n + i  # noqa: E731
    internal, wall, sides = [], [], []
    for j in range(n):
        for i in range(n):
            c = cid(i, j)
            if i + 1 < n:
                internal.append(((pid(i + 1, j, 0), pid(i + 1, j + 1, 0), pid(i + 1, j + 1, 1), pid(i + 1, j, 1)), c, cid(i + 1, j)))
            if j + 1 < n:
                internal.append(((pid(i, j + 1, 0), pid(i, j + 1, 1), pid(i + 1, j + 1, 1), pid(i + 1, j + 1, 0)), c, cid(i, j + 1)))
            wall.append(((pid(i, j, 0), pid(i, j + 1, 0), pid(i + 1, j + 1, 0), pid(i + 1, j, 0)), c))
            sides.append(((pid(i, j, 1), pid(i + 1, j, 1), pid(i + 1, j + 1, 1), pid(i, j + 1, 1)), c))
            if i == 0:
                sides.append(((pid(0, j, 0), pid(0, j, 1), pid(0, j + 1, 1), pid(0, j + 1, 0)), c))
            if i == n - 1:
                sides.append(((pid(n, j, 0), pid(n, j + 1, 0), pid(n, j + 1, 1), pid(n, j, 1)), c))
            if j == 0:
                sides.append(((pid(i, 0, 0), pid(i + 1, 0, 0), pid(i + 1, 0, 1), pid(i, 0, 1)), c))
            if j == n - 1:
                sides.append(((pid(i, n, 0), pid(i, n, 1), pid(i + 1, n, 1), pid(i + 1, n, 0)), c))
    faces = [f for f, *_ in internal] + [f for f, _ in wall] + [f for f, _ in sides]
    owner = [o for _f, o, _n in internal] + [o for _f, o in wall] + [o for _f, o in sides]
    nei = [x for _f, _o, x in internal]
    pm = case / "constant" / "polyMesh"
    pm.mkdir(parents=True)

    def lst(rows):
        return f"{len(rows)}\n(\n" + "\n".join(rows) + "\n)\n"

    (pm / "points").write_text(HDR.format(cls="vectorField", obj="points") + lst([f"({x} {y} {z})" for x, y, z in pts]))
    (pm / "faces").write_text(HDR.format(cls="faceList", obj="faces") + lst([f"4({' '.join(map(str, f))})" for f in faces]))
    (pm / "owner").write_text(HDR.format(cls="labelList", obj="owner") + lst([str(o) for o in owner]))
    (pm / "neighbour").write_text(HDR.format(cls="labelList", obj="neighbour") + lst([str(o) for o in nei]))
    ni, nw = len(internal), len(wall)
    (pm / "boundary").write_text(HDR.format(cls="polyBoundaryMesh", obj="boundary") + (
        f"2\n(\nwall\n{{\n type wall;\n nFaces {nw};\n startFace {ni};\n}}\n"
        f"sides\n{{\n type patch;\n nFaces {len(sides)};\n startFace {ni + nw};\n}}\n)\n"))


def _surface():
    """Two CAD faces in millimetres: Face_1 the 4 x 4 mm square at z=0, Face_2 a square at z=1."""
    xyz = np.array([[0, 0, 0], [4, 0, 0], [4, 4, 0], [0, 4, 0], [0, 0, 1], [4, 0, 1], [4, 4, 1], [0, 4, 1]], float)
    tris = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]])
    return SimpleNamespace(xyz=xyz, tris=tris, label=np.array([0, 0, 1, 1]), names=["Face_1", "Face_2"])


SIG1 = {"area_m2": 16e-6, "centroid_m": [2e-3, 2e-3, 0.0], "normal": [0, 0, -1]}


def _req(mm=0.5, quote="0.5 mm cells on this wall"):
    return {"rows": [{"id": "r1", "field": "local", "asked": "cells of 0.5 mm at this wall", "quote": quote}],
            "local": [{"id": "r1", "where": "this wall", "what": "size", "mm": mm, "quote": quote}],
            "sizes": {}}


def _notes(quote="0.5 mm cells on this wall", sig=SIG1):
    return [{"text": f"please {quote}", "faces": [{"id": "Face_1", "label": "Face 1 · flat 4 x 4 mm", "sig": sig}]}]


def test_bind_ties_the_size_to_the_picked_face_and_a_wall_size_too():
    req = CP.bind(_req(), _notes())
    e = req["local"][0]
    assert e["faces"] == ["Face_1"] and e["sigs"][0] == SIG1 and "Face 1" in e["where"]
    wall = {"rows": [{"id": "r2", "field": "near_wall", "asked": "0.5 mm at the wall", "quote": "0.5 mm cells on this wall"}],
            "sizes": {"near_wall_mm": 0.5}, "local": []}
    CP.bind(wall, _notes())
    assert wall["sizes"]["near_wall_mm"] is None and wall["local"][0]["faces"] == ["Face_1"]
    assert wall["rows"][0]["field"] == "local"
    overall = _req(quote="2 mm overall")
    CP.bind(overall, _notes("2 mm overall"))
    assert not overall["local"][0].get("faces")


def test_resolve_writes_the_face_own_triangles_and_tells_a_changed_face(tmp_path):
    req = CP.bind(_req(), _notes())
    got = CP.resolve(tmp_path, req, "", surface=_surface())
    assert got["r1"]["file"] == "picked_1" and got["r1"]["area_m2"] == pytest.approx(16e-6)
    t = CP.triangles(tmp_path, "picked_1")
    assert t.shape == (2, 3, 3) and float(np.abs(t[..., 2]).max()) == 0.0
    moved = CP.bind(_req(), _notes(sig={**SIG1, "area_m2": 30e-6}))
    assert "gone" in CP.resolve(tmp_path, moved, "", surface=_surface())["r1"]


def test_framed_settles_the_unit_against_the_staged_box(tmp_path):
    req = CP.bind(_req(), _notes())
    CP.resolve(tmp_path, req, "", surface=_surface())
    got = CP.framed(tmp_path, ([0, 0, 0], [4.0, 4.0, 1.0]))          # the staged model is in metres: a metre file
    assert got["r1"]["unit_factor"] == 1000.0
    assert float(CP.triangles(tmp_path, "picked_1").max()) == pytest.approx(4.0)


def _ask(tmp_path, mm=0.5):
    req = CP.bind(_req(mm), _notes())
    CP.resolve(tmp_path, req, "", surface=_surface())
    return CR.Ask(req, "snappy")


def test_snappy_builds_the_picked_face_at_or_under_the_size_asked(tmp_path):
    ask = _ask(tmp_path)
    regions = CR.snappy_picked(ask, tmp_path, base_m=0.01, wall_level=2, box=([0, 0, 0], [0.004, 0.004, 0.001]))
    # 10 mm halves to 0.625 mm (over 0.5) and 0.3125 mm: the cell at or under the maximum typed
    assert regions == [{"name": "picked_1", "file": "picked_1.stl", "level": 5, "distance_m": pytest.approx(3 * 0.0003125)}]
    row = ask.rows["r1"]
    assert row["status"] == "applied" and row["picked"] == "picked_1" and row["built_m"] == pytest.approx(0.0003125)
    assert "0.625 mm, is over your size" in row["built"]


def test_cfmesh_builds_the_picked_face_as_a_surface_refinement(tmp_path):
    ask = _ask(tmp_path)
    text = CR.cfmesh_picked(ask, tmp_path, max_cell=0.008, wall_cell=0.002, floor=0.0,
                            box=([0, 0, 0], [0.004, 0.004, 0.001]))
    # nudged over the level so cfMesh stops at it (CFDREQ-36: exactly at a level it goes one finer)
    assert 'surfaceFile "constant/triSurface/picked_1.stl"; cellSize 0.0005005;' in text
    assert ask.picked_cells > 0.0
    assert ask.rows["r1"]["status"] == "applied"


def test_cfmesh_never_guesses_an_internal_place_onto_the_walls():
    req = {"rows": [{"id": "r1", "field": "local", "asked": "finer cells at the bend"}],
           "local": [{"id": "r1", "where": "the bend", "what": "finer", "mm": None, "quote": "finer around the bend"}]}
    ask = CR.Ask(req, "cfmesh")
    CR.cfmesh_local(ask, [{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                          {"name": "outlet", "type": "outlet"}], "wall", max_cell=0.008, wall_cell=0.002, floor=0.0)
    row = ask.rows["r1"]
    assert row["status"] == "not_applied" and "click the faces" in row["built"]


def test_measure_reads_the_built_wall_cells_on_a_patch_and_on_a_picked_face(tmp_path):
    _polymesh(tmp_path)
    got = CP.measure(tmp_path, patches=["wall"])
    assert got["n_faces"] == 16 and got["median_m"] == pytest.approx(1e-3)
    tri = tmp_path / "constant" / "triSurface"
    tri.mkdir(parents=True)
    from meshpipeline.cad.stl_io import _write_solid
    with (tri / "picked_1.stl").open("w") as fh:
        _write_solid(fh, "picked_1", [((0, 0, 0), (0.002, 0, 0), (0.002, 0.004, 0)), ((0, 0, 0), (0.002, 0.004, 0), (0, 0.004, 0))])
    on = CP.measure(tmp_path, picked="picked_1", size_m=1e-3)
    assert 8 <= on["n_faces"] <= 12 and on["median_m"] == pytest.approx(1e-3)


def test_a_size_set_but_not_built_is_not_applied_with_the_number_built(tmp_path):
    """The elbow's case: cfMesh was set for 0.5 mm on the wall and built ~5 mm there - told applied before."""
    _polymesh(tmp_path)
    req = {"rows": [{"id": "r1", "field": "local", "asked": "cells of 0.5 mm at the wall"}],
           "local": [{"id": "r1", "where": "the wall", "what": "size", "mm": 0.5, "quote": "0.5 mm at the wall"}]}
    p = {"status": "applied", "built": "cells of 0.5 mm on the body's walls", "built_m": 0.0005, "patches": ["wall"]}
    got = CR._place_row(tmp_path, req, req["rows"][0], p)
    assert got["applied"] is False and got["met"] is False and got["measured"] == pytest.approx(1.0)
    assert got["delivered"].startswith("not applied - asked 0.5 mm, built 1 mm")
    req["local"][0]["mm"] = 2.0
    ok = CR._place_row(tmp_path, req, req["rows"][0], {**p, "built_m": 0.002})
    assert ok["applied"] is True and ok["met"] is True


def test_report_measures_a_place_row(tmp_path, monkeypatch):
    _polymesh(tmp_path)
    req = {"rows": [{"id": "r1", "field": "local", "asked": "cells of 0.5 mm at the wall"}],
           "local": [{"id": "r1", "where": "the wall", "what": "size", "mm": 0.5, "quote": "0.5 mm at the wall"}]}
    monkeypatch.setattr(CR, "requirements_for", lambda ws: req)
    monkeypatch.setattr(CR, "_plan", lambda ws: {"rows": {"r1": {"status": "applied", "built": "cells of 0.5 mm",
                                                                 "built_m": 0.0005, "patches": ["wall"]}}})
    monkeypatch.setattr(CR.MR, "read", lambda ws: {})
    rep = CR.report(tmp_path, {"cells": 16}, "cfmesh")
    row = next(r for r in rep["rows"] if r["id"] == "r1")
    assert row["applied"] is False and row["measured"] == pytest.approx(1.0)
    json.dumps(rep)


def test_framed_keeps_a_small_fluid_inside_a_big_solid(tmp_path):
    """An internal flow's staged fluid may be far smaller than the solid around it: the face is still framed."""
    req = CP.bind(_req(), _notes())
    CP.resolve(tmp_path, req, "", surface=_surface())
    got = CP.framed(tmp_path, ([0, 0, 0], [0.0041, 0.0041, 0.0002]))     # a thin fluid slab under the 4 x 4 x 1 mm solid
    assert got["r1"]["unit_factor"] == 1.0


def test_a_picked_face_no_wall_lies_on_is_not_applied(tmp_path, monkeypatch):
    """Lab 00139975: the picked cone was not wetted - no wall face lay on it - and the row said applied."""
    _polymesh(tmp_path)
    tri = tmp_path / "constant" / "triSurface"
    tri.mkdir(parents=True)
    from meshpipeline.cad.stl_io import _write_solid
    with (tri / "picked_1.stl").open("w") as fh:
        _write_solid(fh, "picked_1", [((0, 0, 0.5), (0.004, 0, 0.5), (0.004, 0.004, 0.5))])
    req = {"rows": [{"id": "r1", "field": "local", "asked": "cells of 0.3 mm at the face you picked"}],
           "local": [{"id": "r1", "where": "the face you picked", "what": "size", "mm": 0.3, "faces": ["Face_9"]}]}
    monkeypatch.setattr(CR, "requirements_for", lambda ws: req)
    monkeypatch.setattr(CR, "_plan", lambda ws: {"rows": {"r1": {"status": "applied", "built": "cells of 0.25 mm",
                                                                 "built_m": 0.00025, "picked": "picked_1"}}})
    monkeypatch.setattr(CR.MR, "read", lambda ws: {})
    row = next(r for r in CR.report(tmp_path, {"cells": 16}, "snappy")["rows"] if r["id"] == "r1")
    assert row["applied"] is False and "no wall of the built mesh" in row["delivered"]


def _two_faces(picked_mm2: float, rest_mm2: float):
    """Two square CAD faces in millimetres: Face_1 (the one picked, `picked_mm2`) at z=0 and the rest of the model
    (`rest_mm2`) at z=1."""
    a, b = picked_mm2 ** 0.5, rest_mm2 ** 0.5
    xyz = np.array([[0, 0, 0], [a, 0, 0], [a, a, 0], [0, a, 0], [0, 0, 1], [b, 0, 1], [b, b, 1], [0, b, 1]], float)
    return SimpleNamespace(xyz=xyz, tris=np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]]),
                           label=np.array([0, 0, 1, 1]), names=["Face_1", "Face_2"])


def _priced_ask(tmp_path, picked_mm2: float, rest_mm2: float, mm: float, cells: dict | None = None):
    a = picked_mm2 ** 0.5
    sig = {"area_m2": picked_mm2 * 1e-6, "centroid_m": [a / 2e3, a / 2e3, 0.0], "normal": [0, 0, -1]}
    req = CP.bind(dict(_req(mm), **({"cells": cells} if cells else {})), _notes(sig=sig))
    got = CP.resolve(tmp_path, req, "", surface=_two_faces(picked_mm2, rest_mm2))
    assert got["r1"]["model_share"] == pytest.approx(picked_mm2 / (picked_mm2 + rest_mm2))
    b = max(a, rest_mm2 ** 0.5) / 1e3
    return CR.Ask(req, "snappy"), ([0, 0, 0], [b, b, 0.001])


@pytest.fixture
def limit_8m(monkeypatch):
    from meshpipeline.settings import policy as pol
    monkeypatch.setattr(pol, "CELL_HARD_LIMIT", 8_000_000)


def test_a_picked_size_that_cannot_fit_the_limit_is_built_at_the_finest_that_fits_and_said(tmp_path, limit_8m):
    # CFDREQ-36, the elbow's shape: the picked face is 0.58 m2 of a 3.12 m2 model, walls at 6.15 mm, 16 layers,
    # 0.5 mm asked. 0.384 mm there is ~77 M cells, 0.77 mm ~20 M: past the 8 M limit; 1.54 mm (~6 M) fits.
    ask, box = _priced_ask(tmp_path, 583_561.0, 2_536_387.0, 0.5)
    regions = CR.snappy_picked(ask, tmp_path, base_m=0.0492, wall_level=3, box=box, layers=16)
    assert [r["level"] for r in regions] == [5]
    row = ask.rows["r1"]
    assert row["status"] == "nearest" and row["rule"] == CR.RULE_PICKED_PRICE
    assert row["built_m"] == pytest.approx(0.0492 / 32)
    assert row["cell_limit"] == 8_000_000 and row["needs_cells"] > 50_000_000
    assert "0.5 mm on that face needs about" in row["built"] and "this run's limit is 8 million" in row["built"]
    assert "Give a higher cell count to get 0.5 mm" in row["built"]


def test_a_picked_size_is_built_as_asked_when_the_count_typed_holds_it(tmp_path, limit_8m):
    ask, box = _priced_ask(tmp_path, 583_561.0, 2_536_387.0, 0.5, cells={"ceiling": 100_000_000})
    regions = CR.snappy_picked(ask, tmp_path, base_m=0.0492, wall_level=3, box=box, layers=16)
    assert [r["level"] for r in regions] == [7]                      # 0.384 mm: at or under the 0.5 mm asked
    assert ask.rows["r1"]["status"] == "applied" and "needs_cells" not in ask.rows["r1"]


def test_a_count_typed_too_small_is_named_as_theirs(tmp_path, limit_8m):
    ask, box = _priced_ask(tmp_path, 583_561.0, 2_536_387.0, 0.5, cells={"ceiling": 25_000_000})
    regions = CR.snappy_picked(ask, tmp_path, base_m=0.0492, wall_level=3, box=box, layers=16)
    assert [r["level"] for r in regions] == [6]                      # 0.77 mm (~20 M) fits 25 M; 0.384 mm does not
    assert "the count you gave is 25 million" in ask.rows["r1"]["built"]


def test_cfmesh_builds_the_finest_picked_size_inside_the_limit(tmp_path, limit_8m):
    ask, box = _priced_ask(tmp_path, 583_561.0, 2_536_387.0, 0.5)
    text = CR.cfmesh_picked(ask, tmp_path, max_cell=0.03966, wall_cell=0.0049575, floor=0.0, box=box, layers=4)
    # 0.31 / 0.62 mm are past 8 M with the octree band cfMesh really makes; 1.24 mm fits (written nudged)
    assert "cellSize 0.00124061;" in text
    est = CR.picked_estimate(asked_m=0.03966 / 32, wall_cell_m=0.0049575, layers=4, limit=8e6,
                             area_m2=0.583561, share=583_561.0 / 3_119_948.0,
                             band_cells=CR.CFMESH_PICKED_BAND_CELLS)
    assert ask.picked_cells == pytest.approx(est["cells_added"], rel=1e-3)
    assert ask.rows["r1"]["rule"] == "CFDREQ-36" and ask.rows["r1"]["status"] == "nearest"


def test_a_small_picked_face_still_gets_the_size_asked(tmp_path, limit_8m):
    # 00165872's shape: 1510 mm2 of a 4378 mm2 part, walls at 1.49 mm, 0.3 mm asked -> 0.186 mm
    ask, box = _priced_ask(tmp_path, 1510.0, 2868.0, 0.3)
    regions = CR.snappy_picked(ask, tmp_path, base_m=0.005961, wall_level=2, box=box, layers=16)
    assert [r["level"] for r in regions] == [5]
    assert ask.rows["r1"]["status"] == "applied" and ask.rows["r1"]["rule"] == "CFDREQ-34"


def test_picked_estimate_is_the_added_faces_with_their_layers_and_band():
    # the elbow, measured on snappy (16 layers): 856,183 -> 5,200,609 cells at 1.534 mm, -> 1,736,762 at 3.083 mm
    e = CR.picked_estimate(asked_m=1.534e-3, wall_cell_m=6.15e-3, layers=16, limit=8e6, area_m2=0.5836,
                           plan_cells=856_183)
    assert e["rule"] == CR.RULE_PICKED_PRICE and e["fits"] is True
    assert e["cells_added"] == pytest.approx(5_200_609 - 856_183, rel=0.1)
    e = CR.picked_estimate(asked_m=3.083e-3, wall_cell_m=6.15e-3, layers=16, limit=8e6, area_m2=0.5836,
                           plan_cells=856_183)
    assert e["cells_added"] == pytest.approx(1_736_762 - 856_183, rel=0.1)
    assert CR.picked_faces_added(1.0, 0.001, 0.002) == pytest.approx(1e6 - 2.5e5)


def test_picked_estimate_takes_the_share_and_prices_the_plan_from_it():
    by_area = CR.picked_estimate(asked_m=1e-3, wall_cell_m=4e-3, layers=5, limit=1e6, area_m2=0.25, share=0.25)
    by_share = CR.picked_estimate(asked_m=1e-3, wall_cell_m=4e-3, layers=5, limit=1e6, share=0.25, model_area_m2=1.0)
    assert by_area == by_share
    assert by_area["plan_cells"] == pytest.approx(1.0 / 16e-6 * (5 + CR.PICKED_BAND_CELLS))
    assert by_area["total"] == pytest.approx(by_area["plan_cells"] + by_area["cells_added"])
    assert by_area["fits"] is (by_area["total"] <= 1e6)
    roomy = CR.picked_estimate(asked_m=1e-3, wall_cell_m=4e-3, layers=5, limit=1e9, share=0.25, model_area_m2=1.0)
    assert roomy["fits"] is True and roomy["cells_added"] == by_area["cells_added"]


def test_cfmesh_plan_carries_the_picked_octree_band_the_brake_measures():
    # the kept elbow case: picked 0.58 m2 at 4.957 mm under a 9.91 mm wall, 3 layers. Octree measured with the
    # entry at that level 219,559 cells, 85,092 without it; the plan (131,773 without the face) must hold the
    # with-entry octree inside the size brake's margin
    from meshpipeline.engines.size_brake import SIZE_BRAKE_MARGIN
    e = CR.picked_estimate(asked_m=0.0049573, wall_cell_m=0.0099146, layers=3, limit=8e6, area_m2=0.5836,
                           plan_cells=131_773, band_cells=CR.CFMESH_PICKED_BAND_CELLS)
    assert 219_559 <= SIZE_BRAKE_MARGIN * e["total"]
    assert e["cells_added"] >= 219_559 - 85_092
