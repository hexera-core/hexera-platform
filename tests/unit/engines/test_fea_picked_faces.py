# Responsibility: Verify NOTE-17 on the solid FEA route: a size typed with a face picked on the model sizes THAT face in the built mesh (cad/part_words.with_picks: a Pick_n area re-checked on the run's own surface, held by the plan as any sized area, PLAN-fea-32) - the faces far from it keep the global size, the result's row says applied with the size measured on the picked face - and a picked face that is no longer on the model is told, never guessed.
# Boundaries: one plate with a bore built in the test and meshed for real (gmsh + OCP, skipped without them); the model is a fake router that answers the mesh-requirements question only.
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import fea_fixtures as fx  # noqa: E402
from tests.unit.engines.test_fea_driver import _stage  # noqa: E402
from tests.unit.engines.test_fea_requirements import _drive, _ToolRouter  # noqa: E402

from meshpipeline.cad import face_map as FM  # noqa: E402
from meshpipeline.cad import part_words as PW  # noqa: E402
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest  # noqa: E402
from meshpipeline.engines import fea_requirements as FR  # noqa: E402
from meshpipeline.engines import mesh_requirements as MR  # noqa: E402

GLOBAL, LOCAL = "Global mesh size = 8 mm", "1 mm here"


def _block():
    """A 120 x 60 x 30 mm block with a 15 mm bore through it (thick enough that the global size holds far off)."""
    return fx._cut(fx._box(0, 0, 0, 120, 60, 30), fx._cyl(60, 30, -1, 7.5, 32))


def _bore(ws):
    """The bore face as the geometry check's face map names it, read off the upload (mm), with its label and
    signature: what the console sends with the note."""
    from meshpipeline.cad.scout import read_cad

    fm = FM.from_cad(read_cad(ws / "upload.step"), mm_per_unit=1.0, scale_to_m=0.001)
    k = FM.face_at(fm, [0.060 + 0.0075, 0.030, 0.010], [-1.0, 0.0, 0.0])
    assert k is not None and "round Ø15 mm" in FM.label(fm, k)
    return {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}


def _router(local: bool = True):
    answer = {"stated_unit": "none", "element_kind": [], "other": [], "limits": [],
              "sizes": [{"what": "global", "value": 8, "unit": "mm", "quote": GLOBAL}],
              "local": [{"where": "here", "what": "size", "value": 1, "unit": "mm", "quote": LOCAL}] if local else []}
    return _ToolRouter(ModelRoundResult(tool_calls=(ToolCallRequest(
        id="call_abcdef123", name=MR.TOOL, arguments=json.dumps(answer)),)))


def _sizes_on(ws, names):
    """The element size measured on each named face of the built mesh (mm)."""
    from meshpipeline.engines.gmsh.abaqus_deck import from_gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(ws / "mesh.msh"))
        solid = from_gmsh(gmsh, [t for _d, t in gmsh.model.getEntities(3)], by_tag=True)
    finally:
        gmsh.finalize()
    return {n: FR.face_size_mm(solid, [n]) for n in names}


def test_a_size_typed_with_a_picked_bore_sizes_that_bore_and_only_it(tmp_path, monkeypatch):
    ws = _stage(_block(), tmp_path / "block")
    bore = _bore(ws)
    state = {"purpose": "structural", "user_id": "u1",
             "user_notes": [{"text": GLOBAL, "source": "console"},
                            {"text": LOCAL, "source": "console", "faces": [bore]}]}
    ok, marker, _cited, _said = _drive(ws, monkeypatch, state, _router())
    assert ok, marker
    plan = json.loads((ws / "fea_plan.json").read_text())
    picked = [z for z in plan["zones"] if z.get("user") == "Pick_1"]
    assert picked and picked[-1]["faces"] == [bore["id"]] and picked[-1]["size_m"] == pytest.approx(0.001)
    rows = json.loads((ws / "quality.json").read_text())["requirements"]["rows"]
    row = next(r for r in rows if r["field"] == "local")
    assert row["applied"] is True and row["met"] is True, row
    assert row["delivered"].startswith(f"elements on the face you picked ({bore['label']}) are about")
    assert 0.5 <= row["measured"] <= 1.5
    # far from the bore the mesh is the one the global size alone makes: the plate's two end faces (x = 0, 120 mm)
    from meshpipeline.engines import fea_driver as FD

    _record, surface, _diag = FD.measure_staged(ws)
    fm = FM.from_surface(surface, scale_to_m=1.0)
    ends = [fm.names[k] for k in range(len(fm.names))
            if abs(abs(float(fm.normal[k][0])) - 1.0) < 1e-6 and float(fm.area_m2[k]) > 1e-3]
    assert len(ends) == 2
    got = _sizes_on(ws, [bore["id"], *ends])
    assert 0.5 <= got[bore["id"]] <= 1.5
    alone = tmp_path / "global_only"
    _stage(_block(), alone)
    ok, marker, _c, _s = _drive(alone, monkeypatch, {**state, "user_notes": state["user_notes"][:1]},
                                _router(local=False))
    assert ok, marker
    base = _sizes_on(alone, [bore["id"], *ends])
    for name in ends:
        assert got[name] == pytest.approx(base[name], rel=0.1), (got, base)
        assert got[name] >= 4.0, got                    # inside the global 8 mm band (4-12 mm), not the bore's 1 mm
    assert base[bore["id"]] > 2.0 * got[bore["id"]]             # the bore was finer only because it was picked
    print("MEASURED picked run", got, "global only", base)


def test_a_picked_face_no_longer_on_the_model_is_told_and_nothing_is_guessed(tmp_path):
    ws = _stage(_block(), tmp_path / "block")
    bore = _bore(ws)
    from meshpipeline.engines import fea_driver as FD

    _record, surface, _diag = FD.measure_staged(ws)
    moved = {**bore, "sig": {**bore["sig"], "centroid_m": [0.010, 0.010, 0.002]}}      # the bore was moved away
    req = {"local": [{"id": "r2", "where": "the face you picked (x)", "what": "size", "mm": 1.0, "quote": LOCAL,
                      "faces": [moved["id"]], "labels": [moved["label"]], "sigs": [moved["sig"]], "rule": "NOTE-17"}]}
    ps = PW.with_picks({"status": "failed", "note": "down"}, req, surface)
    assert ps["status"] == "used" and ps["entries"] == [] and "Pick_1" not in ps["features"]
    assert ps["dropped"] == [f'"{LOCAL}": the face you picked ({bore["label"]}) {PW.GONE_WORDS}']
    # the same face, unmoved, is kept: one area of its own, an entry with the user's own numbers
    req["local"][0]["sigs"] = [bore["sig"]]
    ps = PW.with_picks(None, req, surface)
    assert ps["features"] == {"Pick_1": [bore["id"]]} and ps["dropped"] == []
    (e,) = ps["entries"]
    assert e["parts"] == ["Pick_1"] and e["min_mm"] == e["max_mm"] == 1.0 and e["rule"] == "NOTE-17"
    # the report row of a pick that was not kept says why, in the reader's words
    rep = FR.report(_one_tet(), {"rows": [{"id": "r2", "field": "local", "asked": "1 mm on x", "quote": LOCAL}],
                                 "local": [{**req["local"][0], "sigs": [moved["sig"]]}]},
                    places=PW.with_picks(None, {"local": [{**req["local"][0], "sigs": [moved["sig"]]}]}, surface))
    (r,) = rep["rows"]
    assert r["applied"] is False and "is no longer on the model as meshed" in r["delivered"]
    assert "no other face was guessed" in r["delivered"]


def test_a_unit_typed_with_the_pick_is_read_back_in_its_own_unit():
    assert PW._unit_of(2.54, "0.1 in here") == pytest.approx((0.1, "in"))
    assert PW._unit_of(2.0, "2 mm here") == (2.0, "mm")


def _one_tet():
    from meshpipeline.engines.gmsh.abaqus_deck import SolidMesh

    pts = [(0, 0, 0), (10, 0, 0), (5, 8.660254, 0), (5, 2.886751, 8.164966)]
    return SolidMesh(node_ids=np.arange(1, 5), xyz=np.array(pts, dtype=float) * 1e-3,
                     elements=np.array([[1, 2, 3, 4]], dtype=np.int64), element_volume=np.ones(1, dtype=np.int64))
