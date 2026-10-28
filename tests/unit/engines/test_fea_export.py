# Responsibility: Verify FEA build step 6 - the Nastran and Ansys files written from the same mesh as the Abaqus / CalculiX deck: read back, they carry the deck's nodes, elements and named faces exactly; each keeps its format's rules; every solver gets its own pass / warnings / fail line; and each file is its own download, labelled with whether that solver has really run it.
# Boundaries: a small fixture part meshed in-process. Third-party readers (meshio, pyNastran, ansys-mapdl-reader) are used when installed; the lab (~/fea5_lab/validate.py) runs all of them on the audit parts.
from __future__ import annotations

import re
import uuid

import numpy as np
import pytest

from meshpipeline.engines import fea_solvers as FS
from meshpipeline.engines import fea_writers as FW


def test_every_download_says_whether_its_solver_has_run_it():
    labels = {k: lab for k, _f, lab in FW.EXPORT_ARTIFACTS}
    assert "solved in CalculiX" in labels["export_calculix_abaqus"]
    assert "Abaqus not yet tested in the real solver" in labels["export_calculix_abaqus"]
    assert labels["export_nastran"].endswith(FW.NOT_TESTED)
    assert labels["export_ansys"].endswith(FW.NOT_TESTED)
    tested = {k: t for k, _l, _f, _e, t in FW.SOLVERS}
    assert tested == {"calculix": True, "abaqus": False, "nastran": False, "ansys": False}


def test_ansys_component_names_follow_mapdl_rules():
    assert FW.ansys_name("Face_3") == "FACE_3"
    assert FW.ansys_name("3 mounting holes (M6)") == "C_3_MOUNTING_HOLES__M6_"
    assert len(FW.ansys_name("x" * 80)) == 32
    # two long CAD names that share their first 32 characters stay distinct, and the search ends
    taken = {FW.ansys_name("A" * 30 + "_C_left")}
    second = FW.unique_ansys_name("A" * 30 + "_C_right", taken)
    assert second not in taken and len(second) <= 32
    taken.add(second)
    assert FW.unique_ansys_name("A" * 30 + "_C_up", taken) not in taken


GOOD = {"fea_inverted": 0, "fea_max_edge_ratio": 3.0, "fea_min_face_angle_deg": 15.0, "fea_max_face_angle_deg": 130.0,
        "fea_max_aspect_raw": 6.0, "fea_max_midside_offset": 0.0, "fea_max_midside_bend_deg": 4.0,
        "fea_max_jacobian_ratio": 1.3}


def _outcomes(fig):
    return {line["solver"]: line["outcome"] for line in FS.judge(fig)}


def test_each_solver_judges_by_its_own_limits():
    assert set(_outcomes(GOOD).values()) == {FS.PASS}
    # an edge ratio of 12: Abaqus Verify warns, nobody else reads it
    assert _outcomes({**GOOD, "fea_max_edge_ratio": 12.0}) == {
        "calculix": FS.PASS, "abaqus": FS.WARNINGS, "nastran": FS.PASS, "ansys": FS.PASS}
    # a Jacobian ratio of 40: Ansys warns; of 2000 it fails
    assert _outcomes({**GOOD, "fea_max_jacobian_ratio": 40.0})["ansys"] == FS.WARNINGS
    assert _outcomes({**GOOD, "fea_max_jacobian_ratio": 2000.0})["ansys"] == FS.FAIL
    # an inverted element fails every solver
    assert set(_outcomes({**GOOD, "fea_inverted": 3}).values()) == {FS.FAIL}
    # a figure not measured is never a pass
    assert _outcomes({k: v for k, v in GOOD.items() if k != "fea_max_midside_bend_deg"})["nastran"] == FS.UNMEASURED
    line = next(x for x in FS.judge(GOOD) if x["solver"] == "nastran")
    assert line["tested_words"] == FW.NOT_TESTED and line["file"] == "mesh.bdf"


# #
# the files, on a meshed part
# #



@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    # the CAD stack only here: the pure checks above run without it
    pytest.importorskip("OCP.BRepPrimAPI")
    gmsh = pytest.importorskip("gmsh")
    pytest.importorskip("vtkmodules.vtkFiltersCore")
    from tests import fea_fixtures as fx

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines.gmsh import fea_adapter

    ws = tmp_path_factory.mktemp("fea_export")
    src = fx.write_step(fx.plate_with_hole(), ws / "upload.step")
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
    record, surface, diag = FD.measure_staged(ws)
    FD.write_plan(ws, FD.make_plan(record, surface, diag))
    assert fea_adapter.mesh(ws) == 0
    return ws


def _deck(ws):
    """The Abaqus deck read plainly: nodes, elements, Face_k node sets and element faces."""
    text = (ws / "mesh.inp").read_text()
    nodes = {}
    for ln in text.split("*NODE\n", 1)[1].split("*", 1)[0].strip().splitlines():
        p = ln.split(",")
        nodes[int(p[0])] = tuple(float(v) for v in p[1:4])
    els = {}
    for ln in text.split("*ELEMENT, TYPE=C3D10, ELSET=solid\n", 1)[1].split("*", 1)[0].strip().splitlines():
        p = [int(v) for v in ln.split(",")]
        els[p[0]] = tuple(p[1:])
    nsets = {m.group(1): {int(v) for v in re.findall(r"\d+", m.group(2))}
             for m in re.finditer(r"\*NSET, NSET=(Face_\d+)\n((?:[^*][^\n]*\n)*)", text)}
    elsets = {m.group(1): {int(v) for v in re.findall(r"\d+", m.group(2))}
              for m in re.finditer(r"\*ELSET, ELSET=(Face_\d+_S\d)\n((?:[^*][^\n]*\n)*)", text)}
    faces: dict[str, set] = {}
    for name, ids in elsets.items():
        face, lab = name.rsplit("_S", 1)
        faces.setdefault(face, set()).update((e, int(lab)) for e in ids)
    return nodes, els, nsets, faces


def test_all_three_files_are_written_and_recorded(plate):
    import json

    q = json.loads((plate / "quality.json").read_text())
    assert set(q["fea"]["exports"]) == {"nastran", "ansys"}
    for f in ("mesh.inp", "mesh.bdf", "mesh.cdb"):
        assert (plate / f).stat().st_size > 0


def test_the_nastran_file_reads_back_as_the_deck(plate):
    nodes, els, nsets, faces = _deck(plate)
    rb = FW.read_bdf(plate / "mesh.bdf")
    assert rb.elements == els
    assert rb.nodes.keys() == nodes.keys()
    # 16-character large-field coordinates: ten significant figures (well under a micron on a part)
    assert max(np.max(np.abs(np.subtract(rb.nodes[i], nodes[i]))) for i in nodes) < 1e-9 * 0.2
    assert rb.face_nodes == nsets
    assert rb.face_elements == faces


def test_the_ansys_file_reads_back_as_the_deck(plate):
    nodes, els, nsets, _faces = _deck(plate)
    rb = FW.read_cdb(plate / "mesh.cdb")
    assert rb.elements == els
    assert max(np.max(np.abs(np.subtract(rb.nodes[i], nodes[i]))) for i in nodes) < 1e-13
    assert rb.face_nodes == nsets


def test_each_file_keeps_its_format(plate):
    bdf = (plate / "mesh.bdf").read_text().splitlines()
    assert all(len(ln) <= 80 for ln in bdf)
    assert not any(ln.startswith(("BEGIN BULK", "ENDDATA")) for ln in bdf)      # an INCLUDE file
    assert any(ln.startswith("PSOLID") for ln in bdf)
    assert bdf[0].startswith("$") and "not yet tested in the real solver" in bdf[1].lower()
    cdb = (plate / "mesh.cdb").read_text().splitlines()
    assert cdb[cdb.index("/PREP7") + 1] == "ET,1,187"
    assert cdb[-1] == "FINISH"
    assert all(len(ln.split(",")[1]) <= 32 for ln in cdb if ln.startswith("CMBLOCK"))
    assert "metres" in "\n".join(cdb[:6]) and "metres" in "\n".join(bdf[:6])


def test_meshio_reads_the_deck_and_the_nastran_file(plate, tmp_path):
    meshio = pytest.importorskip("meshio")
    _nodes, els, _ns, _f = _deck(plate)
    m = meshio.read(plate / "mesh.inp")
    assert sum(len(c.data) for c in m.cells if c.type == "tetra10") == len(els)
    run = tmp_path / "run.bdf"
    run.write_text("SOL 101\nCEND\nBEGIN BULK\n" + (plate / "mesh.bdf").read_text() + "ENDDATA\n")
    m2 = meshio.read(run, file_format="nastran")
    assert sum(len(c.data) for c in m2.cells if c.type == "tetra10") == len(els)


def test_the_result_screen_has_a_line_per_solver(plate):
    from meshpipeline.engines.fea_results import results

    f = results(plate)
    assert [s["solver"] for s in f["solvers"]] == ["calculix", "abaqus", "nastran", "ansys"]
    assert all(s["outcome"] == FS.PASS and s["file_ready"] for s in f["solvers"])
    assert [c["file"] for c in f["download"]["contains"]][:3] == ["mesh.inp", "mesh.bdf", "mesh.cdb"]


def test_each_solver_file_is_its_own_download(plate):
    from meshpipeline.application.artifact_uploader import fea_export_plan

    job = uuid.uuid4()
    plan = fea_export_plan(job, plate)
    assert [p.logical_key for p in plan] == ["export_calculix_abaqus", "export_nastran", "export_ansys"]
    assert all(not p.required and p.object_key.startswith(f"jobs/{job}/exports/") for p in plan)


def test_a_cfd_workspace_offers_no_solver_files(tmp_path):
    from meshpipeline.application.artifact_uploader import fea_export_plan

    (tmp_path / "mesh.bdf").write_text("x")
    assert fea_export_plan(uuid.uuid4(), tmp_path) == []


def test_every_deck_number_fits_calculix_s_twenty_character_field(plate):
    # CalculiX reads a data line's numbers as Fortran f20.0 fields: a longer one is cut silently
    # ("-5.76226301486252e-18" read as -0.576 turned elements inside out, FEA step 7)
    text = (plate / "mesh.inp").read_text()
    block = text.split("*NODE\n", 1)[1].split("*", 1)[0]
    assert all(len(f.strip()) <= 20 for ln in block.strip().splitlines() for f in ln.split(","))


def test_a_tiny_coordinate_is_written_short():
    from meshpipeline.engines.gmsh.abaqus_deck import _coord

    assert len(_coord(-5.76226301486252e-18)) <= 20 and len(_coord(-1.234567890123456e-100)) <= 20
    assert _coord(-0.0) == "0"
