# Responsibility: Verify NOTE-17 on snappyHexMesh, built by CFDREQ-34 (engines/cfd_picked + cfd_requirements.snappy_picked): a picked face is sized ONCE, by snappy_picked, never also by snappy_levels; a face the uploaded CAD cannot name (a triangle file's Region_k) is found on the check's face map through the contract seam, its own triangles written and refined; a moved face, a face map in another frame, or no face map is told and never guessed; and on a single body a size in a place that cannot be located is told as not located, never spread over the whole body.
# Boundaries: plain requirement sets and one box's face map built in the test (numpy only; the plate case needs OCP, skipped without it); no snappyHexMesh run (that needs OpenFOAM: a cloud run).
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.cad import face_map as FM
from meshpipeline.contracts import geometry_check as GC
from meshpipeline.engines import cfd_picked as CP
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR

LOCAL = "1 mm here"
#: the staged surface's box (metres) of the 40 x 20 x 10 mm box below
STAGED = ([0.0, 0.0, 0.0], [0.04, 0.02, 0.01])


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


def _box_map() -> FM.FaceMap:
    """A triangle file's face map: a 40 x 20 x 10 mm box, its six sides Region_k."""
    v = np.array([[0, 0, 0], [40, 0, 0], [40, 20, 0], [0, 20, 0], [0, 0, 10], [40, 0, 10], [40, 20, 10], [0, 20, 10]],
                 float)
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 4, 7, 3)]
    tris = [[v[a], v[b], v[c]] for q in quads for a, b, c in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))]
    return FM.from_triangles(tris, scale_to_m=0.001)


def _top(fm: FM.FaceMap) -> dict:
    k = int(np.argmax(fm.centroid_m[:, 2]))
    return {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}


@pytest.fixture
def seam():
    yield GC.set_face_map_reader
    GC.set_face_map_reader(None)


def _sec(req: dict) -> dict:
    return {"status": "used", "requirements": req}


def test_a_picked_face_is_sized_once_by_snappy_picked_never_by_snappy_levels(tmp_path, seam):
    fm = _box_map()
    face = _top(fm)
    assert face["id"].startswith("Region_")                  # a triangle file: the CAD re-read has no faces to name
    req = _req("here", faces=[face])
    rid = req["local"][0]["id"]
    seam(lambda sid: (fm, 1.0) if sid == "s1" else (None, 1.0))
    CR._with_picks(tmp_path, _sec(req), {"session_id": "s1"})
    got = CP.read(tmp_path)
    assert got[rid]["file"] == "picked_1" and got[rid]["faces"] == [face["id"]]
    tris = CP.triangles(tmp_path, "picked_1")
    assert tris is not None and len(tris) == int((fm.label == fm.names.index(face["id"])).sum())
    ask = CR.Ask(req, "snappy")
    # snappy_levels leaves the picked size alone: the walls keep their level, no row is written for it
    _cap, lo, hi, _f = CR.snappy_levels(ask, 0.032, floor=3, level=4, flevel=4, regions=[], part_levels=False)
    assert lo == hi == 4 and rid not in ask.rows and ask.region_levels == {}
    regions = CR.snappy_picked(ask, tmp_path, base_m=0.032, wall_level=4, box=STAGED)
    # 32 mm background: 1 mm is level 5; one distance refinement on the face's own surface, and only one
    assert [(r["name"], r["level"]) for r in regions] == [("picked_1", 5)]
    assert ask.rows[rid]["status"] == "applied" and ask.rows[rid]["picked"] == "picked_1"


def test_a_moved_face_on_the_face_map_is_told_and_never_guessed(tmp_path, seam):
    fm = _box_map()
    face = _top(fm)
    moved = {**face, "sig": {**face["sig"], "centroid_m": [0.001, 0.001, 0.0]}}
    req = _req("here", faces=[moved])
    rid = req["local"][0]["id"]
    seam(lambda sid: (fm, 1.0))
    CR._with_picks(tmp_path, _sec(req), {"session_id": "s1"})
    assert "gone" in CP.read(tmp_path)[rid]
    ask = CR.Ask(req, "snappy")
    assert CR.snappy_picked(ask, tmp_path, base_m=0.032, wall_level=4, box=STAGED) == []
    assert ask.rows[rid]["status"] == "not_applied"


def test_with_no_face_map_a_pick_the_cad_cannot_name_is_told(tmp_path, seam):
    face = _top(_box_map())
    req = _req("here", faces=[face])
    rid = req["local"][0]["id"]
    seam(None)
    assert CP.face_map_surface(None) is None
    CR._with_picks(tmp_path, _sec(req), {"session_id": "s1"})
    assert "gone" in CP.read(tmp_path)[rid]
    ask = CR.Ask(req, "snappy")
    assert CR.snappy_picked(ask, tmp_path, base_m=0.032, wall_level=4, box=STAGED) == []
    assert ask.rows[rid]["status"] == "not_applied"


def test_a_face_map_in_another_frame_than_the_staged_surface_is_not_used(tmp_path, seam):
    fm = _box_map()
    req = _req("here", faces=[_top(fm)])
    rid = req["local"][0]["id"]
    seam(lambda sid: (fm, 1.0))
    CR._with_picks(tmp_path, _sec(req), {"session_id": "s1"})
    ask = CR.Ask(req, "snappy")
    assert CR.snappy_picked(ask, tmp_path, base_m=0.032, wall_level=4, box=([1.0, 1.0, 1.0], [2.0, 2.0, 2.0])) == []
    assert ask.rows[rid]["status"] == "not_applied" and "another frame" in ask.rows[rid]["built"]


def test_the_plate_s_bore_picked_on_the_cad_face_map_is_written_as_its_own_surface(tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    fm = FM.from_cad(fx.plate_with_hole(), mm_per_unit=1.0, scale_to_m=0.001)
    k = FM.face_at(fm, [0.0675, 0.030, 0.002], [-1.0, 0.0, 0.0])
    face = {"id": fm.names[k], "label": FM.label(fm, k), "sig": FM.signature(fm, k)}
    req = _req("here", faces=[face])
    rid = req["local"][0]["id"]
    got = CP.resolve(tmp_path, req, "", surface=CP.face_map_surface(fm, 1.0))
    assert got[rid]["file"] == "picked_1"
    assert len(CP.triangles(tmp_path, "picked_1")) == int((fm.label == k).sum())


def test_on_a_single_body_a_place_that_cannot_be_located_is_told_never_the_whole_body():
    """A real bug for every user: "refine the leading edge" on one body refined the whole body."""

    def levels(req, regions=()):
        ask = CR.Ask(req, "snappy")
        return ask, CR.snappy_levels(ask, 0.032, floor=3, level=4, flevel=4, regions=list(regions), part_levels=False)

    req = _req("the leading edge", quote="1 mm on the leading edge")
    rid = req["local"][0]["id"]
    ask, (_cap, lo, hi, _f) = levels(req)
    assert lo == hi == 4                                             # the walls keep their own size
    row = ask.rows[rid]
    assert row["status"] == "not_applied" and "the leading edge" in row["built"] and "next best" in row["built"], row
    req = _req("the upper surface", quote="1 mm on the upper surface")              # a piece of it: not located
    ask, (_cap, lo, _hi, _f) = levels(req)
    assert lo == 4 and ask.rows[req["local"][0]["id"]]["status"] == "not_applied"
    # words that mean the whole body still size the whole body
    for where in ("the body", "the walls", "the surface", "the model", "everywhere on the part"):
        req = _req(where, quote=f"1 mm on {where}")
        ask, (_cap, lo, _hi, _f) = levels(req)
        assert lo == 5 and ask.rows[req["local"][0]["id"]]["status"] == "applied", where
    # around the body itself is the whole body (CFDREQ-35's "finer around the car"); around a piece of it is not
    req = _req("around the car", what="finer", value=None, quote="finer around the car")
    ask, (_cap, lo, _hi, _f) = levels(req)
    assert lo == 5 and ask.rows[req["local"][0]["id"]]["status"] == "applied"
    req = _req("around the leading edge", what="finer", value=None, quote="finer around the leading edge")
    ask, (_cap, lo, _hi, _f) = levels(req)
    assert lo == 4 and ask.rows[req["local"][0]["id"]]["status"] == "not_applied"
    # a single named region by its own name still takes it
    req = _req("the wing", quote="1 mm on the wing")
    ask, _got = levels(req, regions=["wing"])
    assert ask.region_levels == {"wing": (5, 5)}


def test_the_face_map_reaches_an_engine_only_through_the_contract_seam(monkeypatch):
    monkeypatch.setattr(GC, "_face_map_reader", None)
    assert GC.face_map("s1") == (None, 1.0)                        # nothing configured: every pick is told
    GC.set_face_map_reader(lambda sid: ("MAP-" + sid, 2.0))
    assert GC.face_map("s1") == ("MAP-s1", 2.0) and GC.face_map("") == (None, 1.0)

    def broken(_sid):
        raise OSError("store down")

    GC.set_face_map_reader(broken)
    assert GC.face_map("s1") == (None, 1.0)                        # never raises
    GC.set_face_map_reader(None)
