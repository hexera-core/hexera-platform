# Responsibility: Verify that a size asked in one named place is applied on a shell mesh and told truthfully (SHMESH-52): the parts reading ties the words to a part or an area, the shell faces of that part or area take the size (a number, "finer", "coarser"), the report row says what the elements there came out at, and what cannot be placed (a hole, a reading that did not answer, words no reading matched) is told "not applied" with why; nothing asked in one place leaves the shell byte for byte as before; with no reading handed to it (a mixed mesh's shell) the row still says it is not applied yet, and a solid keeps its part-by-part line.
# Boundaries: pure over plain data first; then the real shell run on small synthetic plates (a few seconds each), and the real FEA driver on a small T-joint with both readings of the notes stubbed (no model, no solid mesher).
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import mesh_controls as MC
from meshpipeline.engines import control_appliers as CA
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import shell_places as SP
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines.shell_mesh import ShellMesh


def _req(*local):
    """A checked set from the reader's answer: each `local` (where, what, value, quote)."""
    entries = [{"where": w, "what": what, "value": v, "unit": "mm" if what == "size" else "none", "quote": q}
               for w, what, v, q in local]
    notes = ". ".join(q for *_x, q in local)
    args = json.dumps({"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "physics": [],
                       "element_kind": [], "limits": [], "other": [], "local": entries})
    req, dropped, _na = MR.check(args, notes, route="fea")
    assert not dropped, dropped
    return req


def _global(mm: float) -> dict:
    quote = f"{mm:g} mm elements"
    return MR.check(json.dumps({"stated_unit": "none", "sizes": [{"what": "global", "value": mm, "unit": "mm",
                                                                   "quote": quote}],
                                "limits": [], "other": []}), quote)[0]


def _bracket():
    return _req(("the bracket", "size", 3, "3 mm on the bracket"))


# #
# the rows that stay as they were
# #

def test_with_no_reading_handed_to_it_a_size_in_a_named_place_is_not_applied_on_a_shell_and_is_said():
    """A mixed mesh's shell, a surface file: nothing reads the parts for them - never told as applied."""
    row = MR.elements_row("local", _bracket(), 1000, "shell")
    assert row["applied"] is False and row["delivered"].startswith("not applied - a size in one named place")
    rep = {"rows": [{**r, **MR.elements_row(r["field"], _bracket(), 1000, "shell")} for r in _bracket()["rows"]]}
    assert "1 could not be applied to a shell mesh" in MR.summary(rep, kind="shell")


def test_a_solid_still_sets_it_part_by_part():
    row = MR.elements_row("local", _bracket(), 1000, "solid")
    assert row["applied"] is True and "part by part" in row["delivered"]


# #
# the reading, the faces, the sizes
# #

def _record():
    """Two bodies: Part_1 (a plate: Mid_1 from Face_1/Face_2, Mid_2 a flange from Face_3/Face_4) and Part_2 (Mid_3)."""
    return {"mids": [{"id": "Mid_1", "body": "Part_1", "faces": ["Part_1_Face_1", "Part_1_Face_2"]},
                     {"id": "Mid_2", "body": "Part_1", "faces": ["Part_1_Face_3", "Part_1_Face_4"]},
                     {"id": "Mid_3", "body": "Part_2", "faces": ["Part_2_Face_1", "Part_2_Face_2"]}]}


def _rows():
    # one sewn face per mid-face, plus a second sewn face of Mid_1
    return [{"face": 1, "mid": "Mid_1", "wall_mm": 3.0, "size_mm": 6.0, "rule": "SHMESH-15", "floored": False},
            {"face": 2, "mid": "Mid_1", "wall_mm": 3.0, "size_mm": 6.0, "rule": "SHMESH-15", "floored": False,
             "curve_mm": 2.0, "curve_rule": "SHMESH-3"},
            {"face": 3, "mid": "Mid_2", "wall_mm": 3.0, "size_mm": 4.0, "rule": "SHMESH-4", "floored": False},
            {"face": 4, "mid": "Mid_3", "wall_mm": 2.0, "size_mm": 5.0, "rule": "SHMESH-1", "floored": False}]


OWNERS = ["Mid_1", "Mid_1", "Mid_2", "Mid_3"]
AREAS = [10000.0, 10000.0, 2000.0, 5000.0]
FLAT = [(None, 100.0), (None, 100.0), (None, 30.0), (None, 50.0)]
BENT = [(None, 100.0), (None, 100.0), (5.0, 30.0), (None, 50.0)]      # the flange is a bend of 5 mm radius


def _reading(*entries, status="used", dropped=(), unsure=()):
    """cad/part_words' section: the parts table (two parts, three areas) and the checked entries."""
    parts = [{"id": "Part_1", "name": "base plate"}, {"id": "Part_2", "name": "bracket"},
             {"id": "Feature_1", "feature": True, "kind": "flat face", "name": "a large flat face, 10,000 mm²"},
             {"id": "Feature_2", "feature": True, "kind": "hole group", "name": "4 holes, 8 mm across"},
             {"id": "Feature_3", "feature": True, "kind": "thin wall", "name": "thin walls (ribs or flanges), 3 mm thick"}]
    sec = PW.section(status, entries=list(entries), dropped=list(dropped), table=parts,
                     unsure=[{"quote": u} for u in unsure])
    sec["features"] = {"Feature_1": ["Part_1_Face_1"], "Feature_2": ["Part_1_Face_9", "Part_1_Face_10"],
                       "Feature_3": ["Part_1_Face_4"]}
    return sec


def _size(quote, parts, lo, hi=None):
    return {"quote": quote, "ask": "size", "parts": parts, "min_mm": lo, "max_mm": hi or lo}


def _plan(req, reading, curved=FLAT, planned_max=1e9):
    rows = _rows()
    got = SP.plan(rows, OWNERS, _record(), SP.from_reading(reading), req["local"], areas_mm2=AREAS, curved=curved,
                  planned_max=planned_max)
    return rows, got


def test_nothing_asked_in_one_place_reads_nothing_and_sizes_nothing():
    assert not SP.wanted(MR.empty()) and not SP.wanted(None) and not SP.wanted(_global(8))
    rows = _rows()
    assert SP.plan(rows, OWNERS, _record(), SP.from_reading(_reading()), [], areas_mm2=AREAS) is None
    assert rows == _rows()
    # the registry's loop asks nothing of the shell lane either
    assert "place_size" not in CA.asked(MR.empty()) and "place_size" not in CA.asked(_global(8))


def test_a_size_on_a_part_sizes_every_shell_face_of_that_part_and_no_other():
    rows, got = _plan(_bracket(), _reading(_size("3 mm on the bracket", ["Part_2"], 3)))
    assert got["applied"] and [r["size_mm"] for r in rows] == [6.0, 6.0, 4.0, 3.0]
    assert rows[3]["rule"].endswith("+SHMESH-52") and "SHMESH-52" not in rows[0]["rule"]
    p = got["places"][got["asks"][0]["places"][0]]
    assert (p["name"], p["mids"], p["before_mm"], p["set_mm"]) == ("bracket", ["Mid_3"], 5.0, 3.0)
    assert got["planned"] == round(2 * 10000 / 36 + 2000 / 16 + 5000 / 9)


def test_a_band_is_held_at_its_middle_and_a_curved_face_keeps_its_shape():
    req = _req(("the base plate", "size", 20, "20-30 mm on the base plate"))
    rows, got = _plan(req, _reading(_size("20-30 mm on the base plate", ["Part_1"], 20, 30)), curved=BENT)
    assert rows[0]["size_mm"] == rows[1]["size_mm"] == 25.0
    # a hole's edge keeps its own finer size; a bend: 4 a turn at most, its middle a tenth of the size off the arc
    assert rows[1]["curve_mm"] == 2.0
    assert rows[2]["size_mm"] == pytest.approx(SR.curved_size(5.0, 30.0, floor=20, rule=25, chord=2.5), rel=1e-5)
    assert rows[2]["size_mm"] < 25.0 and rows[3]["size_mm"] == 5.0
    p = got["places"][0]
    assert p["coarser"] is True and p["finer"] is False


def test_finer_halves_and_coarser_doubles_each_faces_own_size_but_a_bend_asked_coarser_keeps_its_own():
    req = _req(("the base plate", "finer", None, "finer on the base plate"))
    rows, _got = _plan(req, _reading({"quote": "finer on the base plate", "ask": "refine", "parts": ["Part_1"]}))
    assert [r["size_mm"] for r in rows] == [3.0, 3.0, 2.0, 5.0] and rows[1]["curve_mm"] == 2.0
    req = _req(("the base plate", "coarser", None, "coarser on the base plate"))
    rows, got = _plan(req, _reading({"quote": "coarser on the base plate", "ask": "coarsen", "parts": ["Part_1"]}),
                      curved=BENT)
    assert [r["size_mm"] for r in rows] == [12.0, 12.0, 4.0, 5.0]
    assert got["places"][0]["coarser"] is True


def test_an_area_is_the_shell_faces_made_from_its_cad_faces_and_wins_over_its_part():
    req = _req(("the base plate", "size", 8, "8 mm on the base plate"), ("the flanges", "size", 1, "1 mm at the flanges"))
    rows, got = _plan(req, _reading(_size("8 mm on the base plate", ["Part_1"], 8),
                                    _size("1 mm at the flanges", ["Feature_3"], 1)))
    assert [r["size_mm"] for r in rows] == [8.0, 8.0, 1.0, 5.0]
    flange = got["places"][got["asks"][1]["places"][0]]
    assert flange["area"] and flange["mids"] == ["Mid_2"] and flange["cad_faces"] == [1, 1]
    # a user's size beats our minimums: finer than half the wall is meshed as asked, and said
    assert flange["finer"] is True and rows[2]["floored"] is False


def test_a_hole_is_only_an_edge_on_a_shell_so_its_size_is_not_applied_and_the_reason_is_told():
    req = _req(("the holes", "size", 1, "1 mm at the holes"))
    rows, got = _plan(req, _reading(_size("1 mm at the holes", ["Feature_2"], 1)))
    assert rows == _rows() and got["applied"] is False
    row = SP.row(req["local"][0]["id"], got, _mesh())
    assert row["applied"] is False and row["met"] is None
    assert row["delivered"].startswith("not applied - 4 holes, 8 mm across: a hole is only an edge on a shell")


@pytest.mark.parametrize(("reading", "said"), [
    (_reading(status="failed"), "did not answer"),
    (_reading(status="none"), "that step is off"),
    (_reading(dropped=['"3 mm on the bracket": it was not clear which parts you meant (two brackets), so nothing was '
                       'applied for it']), "not applied - it was not clear which parts you meant (two brackets)"),
    (_reading(_size("10 mm on the frame", ["Part_1"], 10)), "no part or area of your CAD was matched"),
])
def test_an_ask_with_no_reading_to_apply_is_told_not_applied_with_why_and_changes_nothing(reading, said):
    rows, got = _plan(_bracket(), reading)
    assert rows == _rows() and got["applied"] is False
    row = SP.row(_bracket()["local"][0]["id"], got, _mesh())
    assert row["applied"] is False and said in row["delivered"], row


def test_only_a_reading_of_the_same_words_is_applied():
    assert SP.same_words("3 mm on the bracket", "use 3 mm on the bracket please")
    assert SP.same_words("5 mm elements on the base plate", "5 mm elements on base plate")
    assert not SP.same_words("3 mm on the bracket", "10 mm on the frame")
    assert not SP.same_words("3 mm on the bracket", "3 mm on the frame")


def test_a_plan_too_big_to_build_steps_the_places_to_the_nearest_that_builds_and_says_so():
    rows, got = _plan(_bracket(), _reading(_size("3 mm on the bracket", ["Part_2"], 0.05)), planned_max=2000)
    assert got["held"] and rows[3]["size_mm"] > 0.05 and [r["size_mm"] for r in rows[:3]] == [6.0, 6.0, 4.0]
    assert got["planned"] <= 2000


# #
# asked against delivered
# #

def _grid(x0, n, h, mid):
    """n x n quads of side h from (x0, 0, 0)."""
    xyz = [(x0 + i * h, j * h, 0.0) for j in range(n + 1) for i in range(n + 1)]
    quads = [(j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i)
             for j in range(n) for i in range(n)]
    return np.array(xyz, float), np.array(quads, dtype=np.int64), [mid] * len(quads)


def _mesh():
    """Mid_1 and Mid_2 (Part_1) at 6 mm, Mid_3 (Part_2) at 3 mm."""
    xyz, quads, mid, off = [], [], [], 0
    for x, q, m in (_grid(0, 4, 6.0, "Mid_1"), _grid(100, 2, 6.0, "Mid_2"), _grid(200, 5, 3.0, "Mid_3")):
        xyz.append(x)
        quads.append(q + off)
        mid += m
        off += len(x)
    body = {"Mid_1": "Part_1", "Mid_2": "Part_1", "Mid_3": "Part_2"}
    return ShellMesh(xyz=np.vstack(xyz), quads=np.vstack(quads), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.full(len(mid), 3.0), mid=mid, part=[body[m] for m in mid])


def test_the_row_says_what_the_elements_there_came_out_at_against_what_was_asked():
    req = _bracket()
    _rows_, got = _plan(req, _reading(_size("3 mm on the bracket", ["Part_2"], 3)))
    rep = SR.report(_mesh(), req, None, places=got)
    row = rep["rows"][0]
    assert row["field"] == "local" and row["applied"] is True and row["met"] is True and row["measured"] == 3.0
    assert row["delivered"] == "bracket: elements are about 3 mm (25 elements)"
    assert MR.line(row) == "Elements of 3 mm at the bracket: met - bracket: elements are about 3 mm (25 elements)."
    assert "could not be applied" not in SR.summary(rep)
    # asked 1.5 mm and delivered 3 mm: told as not fully met, never as met
    req = _req(("the bracket", "size", 1.5, "1.5 mm on the bracket"))
    _rows_, got = _plan(req, _reading(_size("1.5 mm on the bracket", ["Part_2"], 1.5)))
    row = SR.report(_mesh(), req, None, places=got)["rows"][0]
    assert row["met"] is False and MR.line(row).startswith("Elements of 1.5 mm at the bracket: not fully met - ")


def test_a_piece_of_a_part_that_is_no_face_of_its_own_is_set_on_the_whole_part_and_told_as_the_nearest():
    """The L plate (one flat face) asked "1.5 mm elements on the vertical leg": the reader ties it to the part and
    says the words mean only a piece of it; the row says so, never plain "met"."""
    req = _req(("the vertical leg", "size", 3.0, "3 mm elements on the vertical leg"))
    entry = {**_size("3 mm elements on the vertical leg", ["Part_2"], 3.0), "piece": True}
    _rows_, got = _plan(req, _reading(entry))
    assert got["places"][0]["piece"] is True
    rep = SR.report(_mesh(), req, None, places=got)
    row = rep["rows"][0]
    assert row["met"] is None and row["nearest"] is True and row["applied"] is True
    assert row["delivered"] == ("applied to the whole part, the nearest there is - bracket: elements are about 3 mm "
                                "(25 elements)")
    assert MR.line(row) == ("Elements of 3 mm at the vertical leg: applied to the whole part, the nearest there is - "
                            "bracket: elements are about 3 mm (25 elements).")
    assert MR.summary(rep) == "Of the 1 thing you asked for: 0 met; 1 nearest there is."
    # the checked reading keeps the flag only on a part, never on an area (an area is the place itself)
    table = [{"id": "Part_1", "name": "plate"}, {"id": "Feature_1", "feature": True, "kind": "hole", "name": "a hole"}]
    phrase = {"everything_else": False, "ask": "size", "min": 3, "max": 3, "unit": "mm", "why": "the plate",
              "confidence": 0.9, "piece": True}
    args = json.dumps({"phrases": [{**phrase, "parts": ["Part_1"], "quote": "3 mm on the leg"},
                                   {**phrase, "parts": ["Feature_1"], "quote": "3 mm at the hole"}], "words": []})
    entries, _dropped, _w = PW.check(args, table, "3 mm on the leg. 3 mm at the hole.")
    assert [e.get("piece") for e in entries] == [True, None]


def test_the_coarser_warning_is_told_from_the_built_elements_and_a_smaller_mesh_says_why():
    """Val4's flanges, asked 3 mm over our 2.5 mm, came out about 1.3 mm and were still told "coarser than our
    accuracy rules": the warning now comes from the elements built there, and a place built well under its size
    says what held it there."""
    req = _req(("the bracket", "size", 6.0, "6 mm on the bracket"))
    _rows_, got = _plan(req, _reading(_size("6 mm on the bracket", ["Part_2"], 6.0)))
    p = got["places"][got["asks"][0]["places"][0]]
    assert p["coarser"] is True and p["before_mm"] < 6.0
    row = SR.report(_mesh(), req, None, places=got)["rows"][0]        # built at 3 mm there
    assert row["delivered"].startswith("bracket: elements are about 3 mm (25 elements); smaller near holes, bends "
                                       "and finer faces beside it, so shapes stay true and sizes change smoothly")
    said = f"coarser than the {SP._g(p['before_mm'])} mm our accuracy rules use there"
    assert said not in row["delivered"] and p["before_mm"] == 5.0
    # built at the size asked, over our own 5 mm: the warning names both
    xyz, quads, mid = _grid(200, 3, 6.0, "Mid_3")
    at6 = ShellMesh(xyz=xyz, quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(len(mid), 2.0),
                    mid=mid, part=["Part_2"] * len(mid))
    row = SR.report(at6, req, None, places=got)["rows"][0]
    assert row["delivered"] == ("bracket: elements are about 6 mm (9 elements); its largest are about 6 mm, coarser "
                                "than the 5 mm our accuracy rules use there, so results may be less exact")


def test_finer_is_told_with_the_size_it_had_and_the_size_it_got():
    req = _req(("the base plate", "finer", None, "finer on the base plate"))
    _rows_, got = _plan(req, _reading({"quote": "finer on the base plate", "ask": "refine", "parts": ["Part_1"]}))
    row = SR.report(_mesh(), req, None, places=got)["rows"][0]
    assert row["delivered"].startswith("base plate: its own size halved, from 6 to 3 mm; elements are about 6 mm")
    assert row["met"] is False                      # (this mesh was not made from that plan: 6 mm is not about 3)


def test_the_registry_declares_the_control_and_the_shell_lane_applies_it():
    c = MC.CONTROLS["place_size"]
    assert c.field == "local" and c.rule == "SHMESH-52" and c not in MC.read_by_reader()
    assert c.lanes["shell"] == "meshpipeline.engines.control_appliers.shell_place_sizes"
    req = _bracket()
    assert CA.asked(req)["place_size"]["value"][0]["where"] == "the bracket"
    rows = _rows()
    target = {"rows": rows, "owners": OWNERS, "record": _record(), "areas_mm2": AREAS, "curved": FLAT,
              "places": SP.from_reading(_reading(_size("3 mm on the bracket", ["Part_2"], 3)))}
    got = CA.apply("shell", req, target)
    assert got["place_size"] == {"id": req["local"][0]["id"], "status": "applied", "built": "sized 1 place on the shell"}
    assert rows[3]["size_mm"] == 3.0 and target["applied"]["applied"] is True
    # every other lane already decides such a row in its own code: the one loop leaves it to them
    assert CA.apply("fea_solid", req, {}) == {} and CA.apply("snappy", req, {}) == {}


# #
# the real shell run
# #

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.engines import shell_run  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = sf.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


FILES = ("shell.bdf", "shell.inp", "shell_abaqus.inp", "shell.cdb", "shell.msh")


def _sha(ws: Path) -> dict:
    return {n: hashlib.sha256((ws / n).read_bytes()).hexdigest() for n in FILES}


def _copy(src: Path, ws: Path) -> Path:
    ws.mkdir(parents=True)
    (ws / "geometry.step").write_bytes((src / "geometry.step").read_bytes())
    return ws


def _run(ws: Path, req: dict | None = None, reading: dict | None = None) -> dict:
    if req is not None:
        MR.write(ws, MR.section("used", requirements=req))
    if reading is not None:
        SP.write(ws, SP.from_reading(reading))
    assert shell_run.main([str(ws), str(ws / "geometry.step"), "staged"]) == 0
    return shell_run.read(ws)


@pytest.fixture(scope="module")
def plates(tmp_path_factory):
    """Two separate plates (two bodies): Part_1 100 x 100 x 3 at z 0, Part_2 100 x 50 x 3 at z 20; and their shell
    as our rules make it."""
    ws = _stage(sf.two_plates_assembly(), tmp_path_factory.mktemp("plates"))
    assert _run(_copy(ws, ws / "rules"))["made"]
    return ws


def _elements_of(ws: Path, part: str) -> int:
    return sum(int(f["elements"]) for f in shell_run.read(ws)["faces"] if f["part"] == part)


def _top():
    return _req(("the top plate", "size", 2.5, "2.5 mm elements on the top plate"))


def _top_reading():
    sec = PW.section("used", entries=[_size("2.5 mm elements on the top plate", ["Part_2"], 2.5)],
                     table=[{"id": "Part_1", "name": ""}, {"id": "Part_2", "name": "top plate"}])
    sec["features"] = {}
    return sec


def test_a_size_on_one_part_is_meshed_there_and_told_asked_against_delivered(tmp_path, plates):
    rec = _run(_copy(plates, tmp_path / "ws"), _top(), _top_reading())
    assert rec["made"] and rec["quality"]["valid"] is True
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["applied"] is True and row["met"] is True, row
    assert 0.8 * 2.5 <= row["measured"] <= 1.25 * 2.5
    assert row["delivered"].startswith("top plate: elements are about ")
    # the top plate's 5,000 mm2 at about 2.5 mm, far finer than our rules made it
    built, rules = _elements_of(tmp_path / "ws", "Part_2"), _elements_of(plates / "rules", "Part_2")
    assert built >= 0.6 * 5000 / 2.5 ** 2 and built > 2 * rules
    assert rec["places"]["places"][0]["faces"] >= 1
    assert "could not be applied" not in SR.summary(rec["requirements"])


def test_nothing_asked_in_one_place_leaves_the_shell_byte_for_byte_as_before(tmp_path, plates):
    """A reading carried in the workspace is never read unless the notes ask a size in one place: the rules' shell,
    and a shell under other requirements, are the very files they were."""
    a = _run(_copy(plates, tmp_path / "a"), reading=_top_reading())
    assert _sha(tmp_path / "a") == _sha(plates / "rules") and "places" not in a
    b = _run(_copy(plates, tmp_path / "b"), _global(8))
    c = _run(_copy(plates, tmp_path / "c"), _global(8), _top_reading())
    assert _sha(tmp_path / "b") == _sha(tmp_path / "c") and b["requirements"] == c["requirements"]
    assert "places" not in c


def test_an_area_inside_a_part_takes_its_size_and_a_hole_says_why_it_cannot(tmp_path):
    ws = _stage(sf.plate_with_hole(), tmp_path / "src")
    rules = _run(_copy(ws, tmp_path / "rules"))
    shell = json.loads((tmp_path / "rules" / "shell.json").read_text())
    plate_faces = next(m for m in shell["mids"] if m["id"] in shell["surface_faces"])["faces"]   # the meshed plate
    bore = [f["face"] for f in shell["faces"] if f.get("kind") == "cylinder"]
    assert bore and not set(bore) & {x for m in shell["mids"] for x in m["faces"]}, shell["faces"]
    req = _req(("the big flat face", "size", 3, "3 mm on the big flat face"), ("the hole", "size", 1, "1 mm at the hole"))
    reading = PW.section("used", entries=[_size("3 mm on the big flat face", ["Feature_2"], 3),
                                          _size("1 mm at the hole", ["Feature_1"], 1)],
                         table=[{"id": "Part_1", "name": ""},
                                {"id": "Feature_1", "feature": True, "kind": "hole", "name": "a hole, 20 mm across"},
                                {"id": "Feature_2", "feature": True, "kind": "flat face", "name": "a large flat face"}])
    reading["features"] = {"Feature_1": bore, "Feature_2": [plate_faces[0]]}
    rec = _run(_copy(ws, tmp_path / "ws"), req, reading)
    rows = {r["asked"]: r for r in rec["requirements"]["rows"] if r["field"] == "local"}
    flat = rows["elements of 3 mm at the big flat face"]
    assert flat["applied"] is True and flat["met"] is True and 0.8 * 3 <= flat["measured"] <= 1.25 * 3, flat
    assert rec["elements"] > 1.5 * rules["elements"]
    hole = rows["elements of 1 mm at the hole"]
    assert hole["applied"] is False and "a hole is only an edge on a shell" in hole["delivered"]
    assert "1 could not be applied to a shell mesh" in SR.summary(rec["requirements"])


def _round_hole_nodes(ws: Path, cx: float, cy: float, r: float) -> int:
    """The shell's nodes on the edge of a round hole at (cx, cy) of radius r (mm; the mesh is in metres)."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(ws / "shell.msh"))
        _tags, xyz, _p = gmsh.model.mesh.getNodes()
    finally:
        gmsh.finalize()
    xyz = 1000.0 * np.asarray(xyz).reshape(-1, 3)
    return int((np.abs(np.hypot(xyz[:, 0] - cx, xyz[:, 1] - cy) - r) < 0.02).sum())


def test_a_coarse_size_in_a_place_wins_round_its_holes_which_keep_eight_a_turn(tmp_path):
    """Val4's flanges asked 3 mm came out about 1.3 mm: our arc sizes (12 a turn round each 4 mm hole) graded the
    whole flange down. Asked coarser in a place, its holes are sized as under a size for the whole mesh: the size
    asked, but never under SR.HOLE_PER_TURN round a hole."""
    ws = _stage(sf.plate_with_hole(r=4.0), tmp_path / "src")
    rules = _run(_copy(ws, tmp_path / "rules"))
    assert rules["made"] and _round_hole_nodes(tmp_path / "rules", 100, 50, 4.0) >= 12
    req = _req(("the plate", "size", 12, "12 mm elements on the plate"))
    reading = PW.section("used", entries=[_size("12 mm elements on the plate", ["Part_1"], 12)],
                         table=[{"id": "Part_1", "name": "plate"}])
    reading["features"] = {}
    rec = _run(_copy(ws, tmp_path / "ws"), req, reading)
    assert rec["made"] and rec["quality"]["valid"] is True
    assert SR.HOLE_PER_TURN <= _round_hole_nodes(tmp_path / "ws", 100, 50, 4.0) < 12
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["applied"] is True and row["met"] is True, row


# #
# the real FEA driver: the parts are read before the shell only when a size is asked in one place
# #

class _Run:
    deadline_s = None

    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _drive(ws: Path, monkeypatch, req: dict, notes: str, part_reading=None) -> list[str]:
    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime

    calls: list[str] = []

    async def ai(*_a, **_k):
        return {"decision": {}}

    async def decide(_state, **_kw):
        return MR.section("used", requirements=req, source="model")

    async def parts(ws_, _record, _surface, _state, **_kw):
        calls.append("after the shell" if (Path(ws_) / shell_run.RECORD).exists() else "before the shell")
        if part_reading is None:
            raise AssertionError("the parts were read with no size asked in one place")
        return part_reading

    monkeypatch.setattr(FD.FA, "decide", ai)
    monkeypatch.setattr(FD, "element_target", lambda _state, _ai: None)
    monkeypatch.setattr(MR, "decide", decide)
    monkeypatch.setattr(FD.PS, "decide", parts)
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: None)
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: (_ for _ in ()).throw(AssertionError("the solid mesher was called"))))
    state = {"purpose": "structural", "fea_mesh_as": "shell", "user_notes": [{"id": "n1", "text": notes}]}
    ok, _marker, _o = asyncio.run(FD.drive(ws, state, job_id="t-place", publish=None, run=_Run()))
    assert ok is True
    return calls


def test_the_driver_reads_the_parts_before_the_shell_and_the_shell_applies_them(tmp_path, monkeypatch):
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    req = _req(("the part", "finer", None, "finer on the part"))
    reading = PW.section("used", entries=[{"quote": "finer on the part", "ask": "refine", "parts": ["Part_1"]}],
                         table=[{"id": "Part_1", "name": ""}])
    reading["features"] = {}
    assert _drive(ws, monkeypatch, req, "finer on the part", reading) == ["before the shell"]
    rec = shell_run.read(ws)
    assert rec["made"] is True
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["applied"] is True and row["delivered"].startswith("Part_1: its own size halved"), row
    assert json.loads((ws / SP.FILE).read_text())["entries"][0]["ask"] == "refine"


def test_the_driver_reads_no_parts_before_a_shell_with_no_size_asked_in_one_place(tmp_path, monkeypatch):
    ws = _stage(sf.t_joint(), tmp_path / "ws")
    assert _drive(ws, monkeypatch, _global(8), "8 mm elements") == []
    assert not (ws / SP.FILE).exists() and shell_run.read(ws)["made"] is True


def test_the_vertical_leg_of_a_bent_bracket_is_its_upright_plate_on_the_shell(tmp_path):
    """A leg, flange, web or base plate that is a plate of its own is a place on a shell: the parts reader is told how
    each wall or large flat face lies in its pictures (z up) - the bent L's upright leg and its level base - so "the vertical
    leg" is tied to the upright face and sized on that leg's mid-face alone, met there, never the whole part."""
    from meshpipeline.cad import part_features as PF
    from meshpipeline.engines import fea_driver as FD
    ws = _stage(sf.l_angle(), tmp_path / "src")
    record, surface, _diag = FD.measure_staged(ws)
    rows, faces_of = PF.features(record, surface)
    # (its plates are thin walls beside its 100 mm ends: one area per way they lie)
    lie = {r["id"]: r.get("lie") for r in rows if r.get("lie")}
    upright = [k for k, v in lie.items() if v == "upright"]
    assert upright and any(str(v).startswith("level") for v in lie.values()), rows
    assert all("upright" in r["name"] for r in rows if r["id"] in upright)
    req = _req(("the vertical leg", "size", 3, "3 mm elements on the vertical leg"))
    reading = PW.section("used", entries=[_size("3 mm elements on the vertical leg", upright[:1], 3)],
                         table=[{"id": "Part_1", "name": ""}, *rows])
    reading["features"] = {k: list(v) for k, v in faces_of.items()}
    rec = _run(_copy(ws, tmp_path / "ws"), req, reading)
    p = rec["places"]["places"][0]
    shell = json.loads((tmp_path / "ws" / "shell.json").read_text())
    assert len(p["mids"]) == 1 and len(shell["mids"]) >= 2 and not p.get("piece")
    row = next(r for r in rec["requirements"]["rows"] if r["field"] == "local")
    assert row["met"] is True and 0.8 * 3 <= row["measured"] <= 1.25 * 3, row


def test_a_piece_told_as_the_nearest_still_has_its_row_in_the_report():
    """Round 4 G on a shell: the L plate (one flat face) asked "1.5 mm on the vertical leg" was told "applied to the
    whole part, the nearest there is" in the tally, but its 'You asked' row was dropped from the report (met None)."""
    from meshpipeline.engines import shell_delivery as SD
    row = {"id": "r1", "field": "local", "asked": "elements of 1.5 mm at the vertical leg", "applied": True,
           "met": None, "nearest": True, "measured": 1.35,
           "delivered": "applied to the whole part, the nearest there is - Part_1: elements are about 1.35 mm"}
    got = SD.asked_rows({"requirements": {"rows": [row]}})
    assert [r["label"] for r in got] == ["You asked: elements of 1.5 mm at the vertical leg"]
    assert got[0]["said"].startswith("Elements of 1.5 mm at the vertical leg: applied to the whole part, the nearest")


def test_a_part_that_is_one_plate_offers_no_flat_face_as_a_piece_of_it(tmp_path):
    """The L-shaped plate's two big faces (its two sides) were offered as areas: "the vertical leg" was tied to one in
    one run and told the nearest in another. A part that is one plate offers none - the part itself is the place."""
    from meshpipeline.cad import part_features as PF
    from meshpipeline.engines import fea_driver as FD
    ws = _stage(sf.flat_plate(), tmp_path / "plate")
    rows, _faces = PF.features(*FD.measure_staged(ws)[:2])
    assert not [r for r in rows if r["kind"] in ("flat face", "thin wall")], rows


def test_a_nearest_place_row_is_never_shown_as_met():
    """6b, round 11: the tally said "1 applied to the whole part, the nearest there is" while its report row read
    "Met". The row is told, not judged: "Nearest there is"."""
    from meshpipeline.engines import shell_delivery as SD
    from meshpipeline.render import mesh_report as RP
    row = {"id": "r1", "field": "local", "asked": "elements of 1.5 mm at the vertical leg", "applied": True,
           "met": None, "nearest": True, "delivered": "applied to the whole part, the nearest there is - Part_1: ..."}
    got = SD.asked_rows({"requirements": {"rows": [row]}})
    assert got[0]["told"] == "nearest"
    html = RP._fea_limits({"check": {"rows": [{"label": got[0]["label"], "measured": 0, "op": "==", "limit": 0,
                                               "ok": None, "told": "nearest"}]}})
    assert "Nearest there is" in html and ">Met<" not in html


# #
# whole part or a portion of it (m483 round 12, 6c): geometry first, then the reader's extent - never a word list
# #

def _tied(quote: str, parts: list[str], table: list[dict], **reading) -> dict:
    """One phrase of the parts reader, checked (cad/part_words.check) against `table`."""
    phrase = {"everything_else": False, "ask": "size", "min": 3, "max": 3, "unit": "mm", "why": "the part",
              "confidence": 0.9, "parts": parts, "quote": quote, **reading}
    entries, dropped, _w = PW.check(json.dumps({"phrases": [phrase], "words": []}), table, quote)
    assert not dropped, dropped
    return entries[0]


ONE_BODY = [{"id": "Part_2", "name": "Part_2"}]


def test_the_name_of_a_one_plate_part_is_the_whole_part_and_its_row_is_met():
    """6c: the NEMA17 mount is one flat plate, so it offers no flat face (the one-plate rule removes only the "which
    side" question). "2 mm elements on the base plate" names the part itself: the row is judged on the whole part's
    elements and is met when they are the size asked - never "the nearest there is"."""
    entry = _tied("3 mm elements on the base plate", ["Part_2"], ONE_BODY, extent="whole",
                  extent_words="the base plate")
    assert "piece" not in entry
    req = _req(("the base plate", "size", 3.0, "3 mm elements on the base plate"))
    _rows_, got = _plan(req, _reading(entry))
    assert not got["places"][0].get("piece")
    row = SR.report(_mesh(), req, None, places=got)["rows"][0]
    assert row["met"] is True and not row.get("nearest")
    assert MR.summary(SR.report(_mesh(), req, None, places=got)) == "Of the 1 thing you asked for: 1 met."


def test_a_portion_with_no_face_of_its_own_is_the_nearest_there_is():
    """6b: "the vertical leg" of an L plate that is one flat face is a portion of the part no row is."""
    entry = _tied("3 mm elements on the vertical leg", ["Part_2"], ONE_BODY, extent="portion",
                  extent_words="the vertical leg")
    assert entry["piece"] is True
    req = _req(("the vertical leg", "size", 3.0, "3 mm elements on the vertical leg"))
    _rows_, got = _plan(req, _reading(entry))
    row = SR.report(_mesh(), req, None, places=got)["rows"][0]
    assert row["met"] is None and row["nearest"] is True


@pytest.mark.parametrize("words", ["the bracket", "the mount", "the part", "the body"])
def test_a_one_body_part_called_by_any_name_is_the_whole_part(words):
    entry = _tied(f"3 mm elements on {words}", ["Part_2"], ONE_BODY, extent="whole", extent_words=words)
    assert "piece" not in entry


@pytest.mark.parametrize("reading", [{"extent": "unsure", "extent_words": "the mount"},
                                     {"extent": "whole", "extent_words": "the frame"},     # not the user's words
                                     {"extent": "whole", "extent_words": ""},
                                     {}])                                                 # no extent at all
def test_unsure_missing_or_unquoted_is_a_portion_told_as_the_nearest(reading):
    entry = _tied("3 mm elements on the mount", ["Part_2"], ONE_BODY, **reading)
    assert entry["piece"] is True


def test_a_two_body_model_where_the_plate_is_one_body_is_that_body():
    table = [{"id": "Part_1", "name": "plate"}, {"id": "Part_2", "name": "frame"}]
    entry = _tied("3 mm elements on the plate", ["Part_1"], table, extent="whole", extent_words="the plate")
    assert entry["parts"] == ["Part_1"] and "piece" not in entry


def test_an_area_that_is_the_whole_of_its_part_is_the_part_whatever_the_reading_says():
    """Geometry first: an area every other face of whose part is an opening or an edge IS the part - the reader's
    "portion" does not make it a piece; an area that is a real portion stays that area."""
    table = [{"id": "Part_1", "name": "Part_1"},
             {"id": "Feature_1", "feature": True, "kind": "flat face", "name": "a large flat face", "in_part": "Part_1",
              "whole_part": True},
             {"id": "Feature_2", "feature": True, "kind": "flat face", "name": "a large flat face", "in_part": "Part_1"}]
    whole = _tied("3 mm elements on the top", ["Feature_1"], table, extent="portion", extent_words="the top")
    assert whole["parts"] == ["Part_1"] and "piece" not in whole
    area = _tied("3 mm elements on the top", ["Feature_2"], table, extent="portion", extent_words="the top")
    assert area["parts"] == ["Feature_2"] and "piece" not in area


def test_the_share_of_the_part_an_area_covers_decides_whether_it_is_the_whole_part():
    from meshpipeline.cad import part_features as PF
    faces = {"Part_1_Face_1": {"area_mm2": 900.0}, "Part_1_Face_2": {"area_mm2": 20.0},     # an edge strip
             "Part_1_Face_3": {"area_mm2": 60.0},                                             # a hole
             "Part_1_Face_4": {"area_mm2": 20.0}}                                             # another edge
    names = list(faces)
    assert PF.whole_of_part(["Part_1_Face_1"], names, faces, {"Part_1_Face_3"})
    # the other side of a plate is no edge: one side is a portion
    faces["Part_1_Face_2"] = {"area_mm2": 880.0}
    assert not PF.whole_of_part(["Part_1_Face_1"], names, faces, {"Part_1_Face_3"})
