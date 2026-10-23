# Responsibility: Verify FEA ASSEMBLIES end to end on small parts of known geometry: which parts touch or pass into each other (cad/fea_assembly), how they are staged for each connection, and the REAL FEA driver meshing them - bonded as one conformal mesh with shared nodes, contact as separate meshes with named contact pairs - with every part its own set and every face of every part named in each solver file.
# Boundaries: parts are built in the test (tests/fea_fixtures.py) and meshed in-process by the real driver; no solver runs here (tests/unit/engines/test_fea_textbook.py solves the textbook assembly cases in CalculiX).
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.contracts import decision_record as dr  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_writers  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter  # noqa: E402


def _stage(shape, ws: Path) -> Path:
    """Stage a part as the builder does for gmsh: the upload read in mm, written in metres, as STEP
    and as gmsh's own B-rep."""
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.write(str(ws / "geometry.brep"))
    finally:
        gmsh.finalize()
    return ws


class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _drive(ws: Path, monkeypatch, connection: str = ""):
    from meshpipeline.engines import runtime

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    state = {"purpose": "structural", **({"fea_connection": connection} if connection else {})}
    return asyncio.run(FD.drive(ws, state, job_id=f"t-{ws.name}", publish=None, run=_Run(),
                                source_path=str(ws / "upload.step")))


def _elsets(text: str) -> dict[str, set[int]]:
    """Each *ELEMENT block's elset -> the node numbers its elements use."""
    out: dict[str, set[int]] = {}
    for m in re.finditer(r"\*ELEMENT, TYPE=C3D10, ELSET=(\w+)\n((?:[^*][^\n]*\n)*)", text):
        out[m.group(1)] = {int(v) for ln in m.group(2).splitlines() for v in ln.split(",")[1:]}
    return out


# #
# what the assembly is
# #

def test_touching_parts_share_a_face_and_overlapping_parts_a_piece():
    rec = A.record(fx.touching_blocks(), mm_per_unit=1.0)
    assert [p["id"] for p in rec["parts"]] == ["Part_1", "Part_2"]
    (itf,) = rec["interfaces"]
    assert itf["kind"] == "touching" and itf["parts"] == ["Part_1", "Part_2"]
    assert itf["area_mm2"] == pytest.approx(100.0, rel=1e-6)
    assert itf["at_mm"] == pytest.approx([10.0, 5.0, 5.0], abs=1e-6)
    assert len(itf["faces"]["Part_1"]) == 1 and len(itf["faces"]["Part_2"]) == 1
    assert rec["free_parts"] == [] and rec["fused"]

    over = A.record(fx.overlapping_blocks(), mm_per_unit=1.0)
    (itf,) = over["interfaces"]
    assert itf["kind"] == "overlap" and itf["overlap_share"] == pytest.approx(0.2, rel=1e-6)
    assert any("pass into each other" in n for n in over["notes"])

    apart = A.record(fx.two_blocks(), mm_per_unit=1.0)
    assert apart["interfaces"] == [] and apart["free_parts"] == ["Part_1", "Part_2"]


def test_every_face_is_named_by_its_part_and_its_place_in_it():
    shape = fx.touching_blocks()
    names = A.global_face_names(shape)
    assert names == [f"Part_1_Face_{j}" for j in range(1, 7)] + [f"Part_2_Face_{j}" for j in range(1, 7)]
    rec = {"faces": {"Face_1": {}, "Face_7": {}}, "hot_spots": [{"faces": ["Face_8"]}],
           "thickness": {"face": "Face_2"}}
    out = A.rename_record(rec, names)
    assert set(out["faces"]) == {"Part_1_Face_1", "Part_2_Face_1"}
    assert out["hot_spots"][0]["faces"] == ["Part_2_Face_2"] and out["thickness"]["face"] == "Part_1_Face_2"


@pytest.mark.parametrize("raw, said", [("SOLID", ""), ("Part1", ""), ("3", ""), ("=>[0:1:1:2]", ""),
                                       ("Open CASCADE STEP translator 7.9 1", ""), ("Valve body", "Valve body"),
                                       ("TornilloM3", "TornilloM3")])
def test_a_generic_body_name_is_no_name(raw, said):
    assert A.readable_body_name(raw) == said


def test_bonded_staging_shares_the_touching_face_and_contact_keeps_both(tmp_path):
    bonded = A.stage(fx.touching_blocks(), A.BONDED, tmp_path / "b")
    assert bonded["mode"] == A.PAIR_BONDED and len(bonded["volumes"]) == 2
    shared = [s for s in bonded["surfaces"] if len(s["names"]) == 2]
    assert len(shared) == 1 and len(bonded["surfaces"]) == 11
    assert bonded["pairs"][0]["kind"] == A.PAIR_BONDED
    contact = A.stage(fx.touching_blocks(), A.CONTACT, tmp_path / "c")
    assert contact["mode"] == A.PAIR_CONTACT and len(contact["surfaces"]) == 12
    (pair,) = contact["pairs"]
    assert pair["kind"] == A.PAIR_CONTACT and pair["faces"] == {"1": ["Part_1_Face_2"], "2": ["Part_2_Face_1"]}
    assert (tmp_path / "c" / A.STAGED).exists() and (tmp_path / "c" / A.SIDECAR).exists()


# #
# the real driver
# #

def test_a_bonded_assembly_is_one_conformal_mesh_with_every_part_and_face_named(tmp_path, monkeypatch):
    ws = _stage(fx.touching_blocks(), tmp_path / "bonded")
    ok, marker, _o = _drive(ws, monkeypatch)                  # no answer: bonded, the default
    assert ok, marker
    cited = {e["rule"] for e in dr.cited_for(f"t-{ws.name}")}
    assert "PLAN-fea-17" in cited
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert plan["assembly"]["mode"] == "bonded" and plan["assembly"]["pairs"] == 1
    assert {z["id"] for z in plan["zones"] if z["kind"] == "part"} == {"P1", "P2"}
    assert any("An assembly of 2 parts" in w for w in plan["words"])
    text = (ws / "mesh.inp").read_text()
    sets = _elsets(text)
    assert set(sets) == {"Part_1", "Part_2"}
    # CONFORMAL: the two parts share their nodes on the face they touch on
    shared = sets["Part_1"] & sets["Part_2"]
    assert len(shared) > 20
    names = set(re.findall(r"\*SURFACE, NAME=(\S+), TYPE=ELEMENT", text))
    assert {f"Part_{p}_Face_{j}" for p in (1, 2) for j in range(1, 7)} <= names
    assert {"Bond_1_Part_1", "Bond_1_Part_2"} <= names and "*CONTACT PAIR" not in text
    q = json.loads((ws / "quality.json").read_text())
    asm = q["fea"]["assembly"]
    assert [p["id"] for p in asm["parts"]] == ["Part_1", "Part_2"] and asm["mode"] == "bonded"
    # the Nastran and Ansys files: a property / material per part, every face named, read back whole
    bdf = fea_writers.read_bdf(ws / "mesh.bdf")
    assert len(bdf.elements) == q["cells"]
    assert {f"Face_{k}" for k in range(1, 13)} <= set(bdf.face_nodes)
    assert "PSOLID         2       2" in (ws / "mesh.bdf").read_text()
    cdb = (ws / "mesh.cdb").read_text()
    assert "CMBLOCK,PART_1,ELEM" in cdb and "CMBLOCK,PART_2_FACE_6,NODE" in cdb
    assert len(fea_writers.read_cdb(ws / "mesh.cdb").elements) == q["cells"]
    from meshpipeline.engines.fea_results import results
    from meshpipeline.engines.fea_review import facts

    res = results(ws)
    assert res["assembly"]["parts"][1]["elements"] > 0 and len(res["faces"]) == 12
    check = facts(ws)["assembly"]
    assert check["parts"]["ok"] and check["joints"]["ok"] and check["faces"]["ok"]


def test_a_contact_assembly_is_meshed_apart_with_a_named_pair_for_every_solver(tmp_path, monkeypatch):
    ws = _stage(fx.touching_blocks(), tmp_path / "contact")
    ok, marker, _o = _drive(ws, monkeypatch, connection="contact")
    assert ok, marker
    text = (ws / "mesh.inp").read_text()
    sets = _elsets(text)
    assert not (sets["Part_1"] & sets["Part_2"])               # separate meshes: no shared node
    assert "*SURFACE INTERACTION, NAME=Contact_hard_frictionless" in text
    pair = re.search(r"\*CONTACT PAIR, INTERACTION=\w+, TYPE=SURFACE TO SURFACE\n(\S+), (\S+)\n", text)
    assert pair and {pair.group(1), pair.group(2)} == {"Contact_1_Part_1", "Contact_1_Part_2"}
    bdf = (ws / "mesh.bdf").read_text()
    assert re.search(r"^BCTABLE\s+1", bdf, re.M) and re.search(r"^BCBODY\s+2\s*3D\s+DEFORM", bdf, re.M)
    assert re.search(r"^BSURFS\s+100001", bdf, re.M) and re.search(r"^BSURFS\s+100002", bdf, re.M)
    cdb = (ws / "mesh.cdb").read_text()
    assert "ET,2,170" in cdb and "ET,3,174" in cdb and "KEYOPT,3,12,0" in cdb
    # one CONTA174 per slave element face, one TARGE170 per master element face
    rows = [ln for ln in cdb.split("EBLOCK", 1)[1].splitlines()[2:] if len(ln) >= 99]
    types = [int(ln[9:18]) for ln in rows if ln[:9].strip().isdigit()]
    assert types.count(2) > 0 and types.count(3) > 0
    q = json.loads((ws / "quality.json").read_text())
    (joint,) = q["fea"]["assembly"]["joints"]
    assert joint["kind"] == "contact" and joint["name"] == "Contact_1"


def test_parts_that_pass_into_each_other_are_fused_where_bonded_and_said(tmp_path, monkeypatch):
    ws = _stage(fx.overlapping_blocks(), tmp_path / "overlap")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    q = json.loads((ws / "quality.json").read_text())
    asm = q["fea"]["assembly"]
    assert any("pass into each other" in n and "one piece" in n for n in asm["notes"])
    # three solids (Part_1 alone, the shared slab, Part_2 alone): the slab counts with Part_1
    side = json.loads((ws / "assembly.json").read_text())
    assert sorted(v["parts"] for v in side["volumes"]) == [[1], [1, 2], [2]]
    assert all(p["elements"] > 0 for p in asm["parts"])


def test_parts_that_do_not_touch_are_still_meshed_and_said(tmp_path, monkeypatch):
    ws = _stage(fx.two_blocks(), tmp_path / "apart")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    q = json.loads((ws / "quality.json").read_text())
    assert any("touch no other part" in n for n in q["fea"]["assembly"]["notes"])


def test_the_connection_is_bonded_unless_the_user_said_contact():
    assert FD.connection_of({}) == "bonded"
    assert FD.connection_of({"fea_connection": "contact"}) == "contact"
    assert FD.connection_of({"fea_connection": "glued"}) == "bonded"


def test_the_ansys_contact_faces_point_out_of_their_element():
    from meshpipeline.engines.fea_assembly_deck import _oriented

    xyz = {1: np.array([0.0, 0, 0]), 2: np.array([1.0, 0, 0]), 3: np.array([0.0, 1, 0]), 4: np.array([0.0, 0, 1])}
    conn = np.array([1, 2, 3, 4])
    for lab in range(1, 5):
        a, b, c = (xyz[n] for n in _oriented(conn, lab, lambda ids: np.array([xyz[int(i)] for i in ids])))
        other = ({1, 2, 3, 4} - set(_oriented(conn, lab, lambda ids: np.array([xyz[int(i)] for i in ids])))).pop()
        assert float(np.dot(np.cross(b - a, c - a), xyz[other] - a)) < 0


def test_the_stage_record_names_every_face_by_its_part_and_finds_the_joint(tmp_path):
    from meshpipeline.cad.fea_measure import measure_cad
    from meshpipeline.cad.scout import read_cad

    path = fx.write_step(fx.touching_blocks(), tmp_path / "blocks.step")
    shape = read_cad(path)
    out = A.measure_assembly(shape, measure_cad(shape, mm_per_unit=1.0), source=path, mm_per_unit=1.0)
    assert len(out["faces"]) == 12 and all(k.startswith("Part_") for k in out["faces"])
    assert out["face_names"] == "Part_k_Face_j"
    (itf,) = out[A.FACT_KEY]["interfaces"]
    assert itf["kind"] == "touching" and itf["area_mm2"] == pytest.approx(100.0, rel=1e-6)


def test_a_fuse_that_fails_ties_the_parts_found_pair_by_pair_never_a_stop(tmp_path, monkeypatch):
    real = A.fuse

    def fails_all_at_once(parts, *, fuzzy=0.0):
        return A.Fused(error="the parts could not be fused") if len(parts) > 2 else real(parts, fuzzy=fuzzy)

    monkeypatch.setattr(A, "fuse", fails_all_at_once)
    three = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 10, 10, 10))
    ws = _stage(three, tmp_path / "tied")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    text = (ws / "mesh.inp").read_text()
    assert text.count("*TIE, NAME=") == 2 and "*CONTACT PAIR" not in text
    q = json.loads((ws / "quality.json").read_text())
    asm = q["fea"]["assembly"]
    assert asm["mode"] == "tie" and [j["kind"] for j in asm["joints"]] == ["tie", "tie"]
    assert any("tied where they touch" in n for n in asm["notes"])
    cdb = (ws / "mesh.cdb").read_text()
    assert "KEYOPT,5,12,5" in cdb
    # Nastran: every pair in the contact table is glued (IGLUE, the 7th field of its SLAVE row, is 1)
    rows = re.findall(r"^\s+SLAVE\s+\d+(?:\s+0\.){4}\s+(\d)\s+0\s*$", (ws / "mesh.bdf").read_text(), re.M)
    assert rows == ["1", "1"]


def test_the_viewer_names_an_assemblys_faces_as_the_files_do(tmp_path, monkeypatch):
    from meshpipeline.engines.fea_results import viewer_face_names
    from meshpipeline.engines.gmsh.viewer_surface import read_msh

    ws = _stage(fx.touching_blocks(), tmp_path / "viewer")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    names = viewer_face_names(ws)
    mesh = read_msh(ws, by_face=True, face_names=names)
    shown = [n for n, _polys in mesh["patches"]]
    # 11 surfaces (the shared one once, under Part_1's name): every face of Part_1, five of Part_2
    assert shown == [f"Part_1_Face_{j}" for j in range(1, 7)] + [f"Part_2_Face_{j}" for j in range(2, 7)]


def test_a_body_drawn_twice_is_one_piece_when_bonded_and_said(tmp_path, monkeypatch):
    from meshpipeline.engines.fea_review import facts

    ws = _stage(fx.doubled_block(), tmp_path / "doubled")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    asm = json.loads((ws / "quality.json").read_text())["fea"]["assembly"]
    assert [p["id"] for p in asm["parts"]] == ["Part_1"] and asm["absorbed"] == ["Part_2"]
    assert any("Part_2 lies wholly inside Part_1" in n for n in asm["notes"])
    assert facts(ws)["assembly"]["parts"]["ok"]


def test_cad_names_reach_the_solver_files_as_plain_ascii(tmp_path, monkeypatch):
    from meshpipeline.engines.fea_assembly_deck import ascii_name

    assert ascii_name("Gehäuse") == "Gehause" and ascii_name("Bolzen Ø8") == "Bolzen 8"
    monkeypatch.setattr(A, "match_body_names", lambda _named, parts: ["Gehäuse", "Bolzen Ø8"][:len(parts)])
    ws = _stage(fx.touching_blocks(), tmp_path / "names")
    ok, marker, _o = _drive(ws, monkeypatch)
    assert ok, marker
    text = (ws / "mesh.inp").read_text(encoding="ascii")
    assert "Part_1 (Gehause)" in text and "Part_2 (Bolzen 8)" in text
    assert "Gehause" in (ws / "mesh.bdf").read_text(encoding="ascii")


def test_three_bodies_on_one_place_give_every_pair_its_interface():
    tripled = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(0, 0, 0, 10, 10, 10), fx._box(0, 0, 0, 10, 10, 10))
    rec = A.record(tripled, mm_per_unit=1.0)
    assert sorted(tuple(i["parts"]) for i in rec["interfaces"]) == [
        ("Part_1", "Part_2"), ("Part_1", "Part_3"), ("Part_2", "Part_3")]
    assert rec["free_parts"] == []


def test_parts_meshed_apart_never_share_a_face_the_file_already_shared(tmp_path):
    shared = A.fuse(A.solids(fx.touching_blocks())).shape          # two solids sharing their face
    assert len(A.global_face_names(shared)) == 11
    side = A.stage(shared, A.CONTACT, tmp_path / "c")
    assert len(side["surfaces"]) == 12 and all(len(s["names"]) == 1 for s in side["surfaces"])
    (pair,) = side["pairs"]
    assert pair["faces"]["1"] and pair["faces"]["2"]
