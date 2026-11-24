# Responsibility: Verify NOTE-17 on snappyHexMesh: a finer size typed with a face picked on the model becomes a distance refinement on that face's own triangles (engines/snappy/part_levels.picked_surfaces + cfd_requirements.snappy_levels) at the level the size gives; the face is re-checked against the check's face map through the contract seam and told when it is gone; a coarser size or layers on one face are told with the next best; and on a single body a size in a place that cannot be located is told as not located, never spread over the whole body.
# Boundaries: plain requirement sets and one plate's face map built in the test (OCP, skipped without it); no snappyHexMesh run (that needs OpenFOAM: a cloud run).
from __future__ import annotations

import json

import pytest

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines.snappy import part_levels as PL

LOCAL = "1 mm here"


def _req(where: str, what: str = "size", value: float | None = 1, quote: str = LOCAL, faces=None) -> dict:
    args = json.dumps({"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "physics": [],
                       "element_kind": [], "limits": [], "other": [],
                       "local": [{"where": where, "what": what, "value": value, "unit": "mm" if value else "none",
                                  "quote": quote}]})
    req, dropped, _na = MR.check(args, quote, route="flow")
    assert not dropped, dropped
    if faces:
        req = MR.bind_picks(req, [{"text": quote, "faces": faces}], cells=True)
    return req


BORE = {"id": "Face_7", "label": "Face 7 · round Ø15 mm",
        "sig": {"area_m2": 1e-4, "centroid_m": [0.06, 0.03, 0.002], "normal": [0.0, 0.0, 0.0]}}


def _levels(req, regions=(), picked=None):
    ask = CR.Ask(req, "snappy")
    if picked is not None:
        ask.picked = picked
    got = CR.snappy_levels(ask, 0.032, floor=3, level=4, flevel=4, regions=list(regions), part_levels=False)
    return ask, got


def test_a_finer_size_on_a_picked_face_is_a_distance_region_at_its_own_level():
    req = _req("here", faces=[BORE])
    rid = req["local"][0]["id"]
    ask, (_cap, lo, hi, _f) = _levels(req, picked={rid: {"file": "picked_1", "where": req["local"][0]["where"]}})
    # 32 mm background: 1 mm is level 5 (1 mm cells); the walls keep their own level 4 (2 mm)
    assert ask.picked_levels == {"picked_1": 5} and lo == hi == 4
    row = ask.rows[rid]
    assert row["status"] == "applied" and row["built"] == f"cells of 1 mm on {req['local'][0]['where']}", row
    geom, refine = PL.picked_refinement(ask.picked_levels, base_m=0.032, have=lambda f: True)
    assert geom == " picked_1.stl { type triSurfaceMesh; name picked_1; }"
    assert refine == " picked_1 { mode distance; levels ((0.003 5)); }"           # 3 of its own cells deep
    # a file that was not written is no entry at all
    assert PL.picked_refinement(ask.picked_levels, base_m=0.032, have=lambda f: False) == ("", "")


def test_finer_with_no_number_is_one_level_over_the_walls():
    req = _req("these", what="finer", value=None, quote="finer on these", faces=[BORE])
    rid = req["local"][0]["id"]
    ask, _got = _levels(req, picked={rid: {"file": "picked_1", "where": "w"}})
    assert ask.picked_levels == {"picked_1": 5} and ask.rows[rid]["status"] == "applied"


@pytest.mark.parametrize("what,value,quote,said", [
    ("coarser", None, "coarser here", "coarser cells on one face need that face as its own surface"),
    ("size", 8, "8 mm here", "is not finer than the cells the walls already have"),
])
def test_coarser_on_a_picked_face_is_told_with_the_next_best(what, value, quote, said):
    req = _req("here", what=what, value=value, quote=quote, faces=[BORE])
    rid = req["local"][0]["id"]
    ask, _got = _levels(req, picked={rid: {"file": "picked_1", "where": "w"}})
    assert ask.picked_levels == {}
    row = ask.rows[rid]
    assert row["status"] == "not_applied" and said in row["built"] and "next best" in row["built"], row


def test_a_picked_face_gone_from_the_model_is_told_and_never_guessed():
    req = _req("here", faces=[BORE])
    rid = req["local"][0]["id"]
    ask, _got = _levels(req, picked={rid: {"gone": "the face you picked (Face 7 · round Ø15 mm)"}})
    assert ask.picked_levels == {}
    assert ask.rows[rid]["status"] == "not_applied"
    assert "no longer on the part" in ask.rows[rid]["built"] and "no other face was guessed" in ask.rows[rid]["built"]
    # no face map reached the run at all: told, not spread to the walls
    ask, (_c, lo, _h, _f) = _levels(req)
    assert lo == 4 and ask.rows[rid]["status"] == "not_applied"


def test_on_a_single_body_a_place_that_cannot_be_located_is_told_never_the_whole_body():
    """A real bug for every user: "refine the leading edge" on one body refined the whole body."""
    req = _req("the leading edge", quote="1 mm on the leading edge")
    rid = req["local"][0]["id"]
    ask, (_cap, lo, hi, _f) = _levels(req)
    assert lo == hi == 4                                             # the walls keep their own size
    row = ask.rows[rid]
    assert row["status"] == "not_applied" and "the leading edge" in row["built"] and "next best" in row["built"], row
    req = _req("the upper surface", quote="1 mm on the upper surface")              # a piece of it: not located
    ask, (_cap, lo, _hi, _f) = _levels(req)
    assert lo == 4 and ask.rows[req["local"][0]["id"]]["status"] == "not_applied"
    # words that mean the whole body still size the whole body
    for where in ("the body", "the walls", "the surface", "the model", "everywhere on the part"):
        req = _req(where, quote=f"1 mm on {where}")
        ask, (_cap, lo, _hi, _f) = _levels(req)
        assert lo == 5 and ask.rows[req["local"][0]["id"]]["status"] == "applied", where
    # a single named region by its own name still takes it
    req = _req("the wing", quote="1 mm on the wing")
    ask, _got = _levels(req, regions=["wing"])
    assert ask.region_levels == {"wing": (5, 5)}


def test_the_picked_faces_triangles_are_written_and_a_moved_face_is_told(tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.cad import face_map as FM
    from meshpipeline.cad.stl_io import read_stl_solids

    fm = FM.from_cad(fx.plate_with_hole(), mm_per_unit=1.0, scale_to_m=0.001)
    k = FM.face_at(fm, [0.0675, 0.030, 0.002], [-1.0, 0.0, 0.0])
    face = {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}
    req = _req("here", faces=[face])
    rid = req["local"][0]["id"]
    staged = [[0.0, 0.0, 0.0], [0.12, 0.06, 0.004]]                  # the staged surface's box (metres)
    got = PL.picked_surfaces(tmp_path, req["local"], (fm, 1.0), box=staged)
    assert got == {rid: {"file": "picked_1", "where": req["local"][0]["where"]}}
    solids = read_stl_solids(tmp_path / "constant" / "triSurface" / "picked_1.stl")
    tris = list(solids.values())[0]
    assert len(tris) == int((fm.label == k).sum())
    moved = {**face, "sig": {**face["sig"], "centroid_m": [0.01, 0.01, 0.002]}}
    req = _req("here", faces=[moved])
    got = PL.picked_surfaces(tmp_path / "b", req["local"], (fm, 1.0), box=staged)
    assert got == {req["local"][0]["id"]: {"gone": f"the face you picked ({face['label']})"}}
    # a face map in another frame than the staged surface is not used (told as gone, never put somewhere else)
    got = PL.picked_surfaces(tmp_path / "c", _req("here", faces=[face])["local"], (fm, 1.0),
                             box=[[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]])
    assert list(got.values())[0].get("gone")
    # no face map at all (the seam unset): every pick told
    assert list(PL.picked_surfaces(tmp_path / "d", req["local"], (None, 1.0), box=staged).values())[0].get("gone")


def test_the_face_map_reaches_an_engine_only_through_the_contract_seam(monkeypatch):
    from meshpipeline.contracts import geometry_check as GC

    monkeypatch.setattr(GC, "_face_map_reader", None)
    assert GC.face_map("s1") == (None, 1.0)                        # nothing configured: every pick is told
    GC.set_face_map_reader(lambda sid: ("MAP-" + sid, 2.0))
    assert GC.face_map("s1") == ("MAP-s1", 2.0) and GC.face_map("") == (None, 1.0)

    def broken(_sid):
        raise OSError("store down")

    GC.set_face_map_reader(broken)
    assert GC.face_map("s1") == (None, 1.0)                        # never raises
    GC.set_face_map_reader(None)
