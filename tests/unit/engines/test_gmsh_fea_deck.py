# Responsibility: Verify the gmsh FEA deck holds only 3D elements and names every CAD face as an element-face surface whose nodes lie on that face.
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.gmsh.abaqus_deck import (
    SolidMesh,
    deck_label,
    element_faces,
    face_nodes,
    face_sets,
    readable_face_name,
    write_inp,
)

SRC = Path(__file__).parents[3] / "src"
CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""


def _run_driver(ws: Path) -> subprocess.CompletedProcess:
    # the driver runs as its own process, as in the mesh image, by its full module path; this
    # checkout's package first
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(SRC), *filter(None, [os.environ.get("PYTHONPATH")])])}
    return subprocess.run([sys.executable, "-m", "meshpipeline.engines.gmsh.driver", str(ws)],
                          env=env, capture_output=True, text=True, timeout=600)


# ---------------------------------------------------------------- pure mesh data (no gmsh)

def _two_tets() -> SolidMesh:
    # two tets sharing the face 2-3-4; node 1 below it, node 5 above it
    xyz = np.array([[0, 0, -1], [1, 0, 0], [0, 1, 0], [0, 0, 0], [0.3, 0.3, 1]], dtype=float)
    els = np.array([[1, 2, 3, 4], [5, 3, 2, 4]])
    return SolidMesh(node_ids=np.arange(1, 6), xyz=xyz, elements=els,
                     element_volume=np.array([1, 1]),
                     faces={1: np.array([[2, 1, 3]]), 2: np.array([[4, 5, 3]])})


def test_element_faces_finds_the_label_whatever_the_triangle_orientation():
    m = _two_tets()
    # S1 = 1-2-3, S2 = 1-4-2, S3 = 2-4-3, S4 = 3-4-1 (corners, solver order)
    e, lab = element_faces(m.elements, np.array([[3, 2, 1], [1, 2, 4], [4, 3, 2], [3, 4, 1]]))
    assert e.tolist() == [0, 0, 0, 0]
    assert lab.tolist() == [1, 2, 3, 4]
    # a face between two elements is taken on the lower element, every time
    e, lab = element_faces(m.elements, np.array([[4, 2, 3]]))
    assert e.tolist() == [0] and lab.tolist() == [3]
    with pytest.raises(ValueError, match="not the face of any element"):
        element_faces(m.elements, np.array([[1, 2, 5]]))


def test_deck_labels_are_solver_safe():
    assert deck_label("fixed") == "fixed"
    assert deck_label("load face.1") == "load_face_1"
    assert deck_label("2nd bolt") == "G_2nd_bolt"
    assert len(deck_label("x" * 200)) == 80


def test_an_alias_never_takes_a_face_name(tmp_path):
    out = write_inp(tmp_path / "m.inp", _two_tets(), {"Face_1": [1], "solid": [2], "load": [2]})
    assert set(out["face_sets"]) == {"Face_1", "Face_2"}
    assert out["face_aliases"] == {"Face_1_group": ["Face_1"], "solid_group": ["Face_2"],
                                   "load": ["Face_2"]}


def test_readable_cad_names_ride_beside_face_k(tmp_path):
    assert readable_face_name("Face 12") == "" and readable_face_name("face<3>") == ""
    assert readable_face_name("  Bore   A ") == "Bore A"
    out = write_inp(tmp_path / "m.inp", _two_tets(), {"load": [2]},
                    face_names={1: "Mounting face", 2: "load"})
    # a readable name over the faces a plan group already names is written once, not twice
    assert out["cad_face_names"] == {"Face_1": "Mounting_face", "Face_2": "load"}
    assert out["face_aliases"] == {"load": ["Face_2"], "Mounting_face": ["Face_1"]}
    text = (tmp_path / "m.inp").read_text()
    assert "Face_1 = CAD face 1 (Mounting_face)" in text
    assert text.count("*SURFACE, NAME=load,") == 1
    # a name the CAD gives a face the plan names differently is kept apart
    out = write_inp(tmp_path / "n.inp", _two_tets(), {"load": [2]}, face_names={1: "load"})
    assert out["face_aliases"] == {"load": ["Face_2"], "load_cad": ["Face_1"]}


def test_the_node_set_is_every_node_of_the_surface_midside_included():
    # one 10-node tet; its S1 face (1-2-3) carries corners 1, 2, 3 and midsides 5, 6, 7
    el = np.array([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]])
    m = SolidMesh(node_ids=np.arange(1, 11), xyz=np.zeros((10, 3)), elements=el,
                  element_volume=np.array([1]), faces={7: np.array([[3, 1, 2]])})
    (f,) = face_sets(m)
    assert f.name == "Face_1" and f.tag == 7 and f.labels.tolist() == [1]
    assert f.nodes.tolist() == [1, 2, 3, 5, 6, 7]


# ---------------------------------------------------------------- the driver, end to end

def _read_deck(path: Path) -> dict:
    """A small Abaqus reader: element blocks, element sets, surfaces and node sets."""
    blocks: list[tuple[str, list[str]]] = []
    for line in path.read_text().splitlines():
        if line.startswith("**") or not line.strip():
            continue
        if line.startswith("*"):
            blocks.append((line, []))
        else:
            blocks[-1][1].append(line)
    deck: dict = {"nodes": {}, "elements": {}, "types": set(), "elsets": {}, "surfaces": {},
                  "nsets": {}}
    for head, rows in blocks:
        key = head.split(",")[0].strip().upper()
        opts = dict(re.findall(r"(\w+)=([^,\s]+)", head))
        if key == "*NODE":
            for r in rows:
                f = r.split(",")
                deck["nodes"][int(f[0])] = [float(v) for v in f[1:4]]
        elif key == "*ELEMENT":
            deck["types"].add(opts["TYPE"])
            for r in rows:
                f = [int(v) for v in r.split(",")]
                deck["elements"][f[0]] = f[1:]
                deck["elsets"].setdefault(opts["ELSET"], []).append(f[0])
        elif key == "*ELSET":
            ids = [int(v) for r in rows for v in r.split(",") if v.strip()]
            if "GENERATE" in head.upper():
                ids = [i for a, b, s in zip(ids[0::3], ids[1::3], ids[2::3], strict=True)
                       for i in range(a, b + 1, s)]
            deck["elsets"][opts["ELSET"]] = ids
        elif key == "*SURFACE":
            assert opts["TYPE"] == "ELEMENT"
            deck["surfaces"][opts["NAME"]] = [tuple(p.strip() for p in r.split(",")) for r in rows]
        elif key == "*NSET":
            deck["nsets"][opts["NSET"]] = [int(v) for r in rows for v in r.split(",") if v.strip()]
    return deck


def _surface_faces(deck: dict, name: str) -> list[tuple[int, int]]:
    return [(e, int(lab[1:])) for elset, lab in deck["surfaces"][name]
            for e in deck["elsets"][elset]]


@pytest.fixture(scope="module")
def bracket_ws(tmp_path_factory):
    """A block with a through hole and a fillet-free notch, meshed by the real driver."""
    gmsh = pytest.importorskip("gmsh")
    ws = tmp_path_factory.mktemp("fea_deck")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        box = occ.addBox(0, 0, 0, 0.06, 0.04, 0.03)
        hole = occ.addCylinder(0.03, 0.02, -0.01, 0, 0, 0.05, 0.008)
        occ.cut([(3, box)], [(3, hole)])
        occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    # name two faces the way a plan would; everything else lands in "free"
    (ws / "gmsh_spec.json").write_text(json.dumps({
        "element_order": 2, "size": {"mode": "factor", "value": 0.08},
        "groups": [{"name": "fixed end", "role": "fixed", "surface_tags": [1]},
                   {"name": "load", "role": "load", "surface_tags": [2]}],
        "default_group": "free", "optimize": True}))
    proc = _run_driver(ws)
    assert proc.returncode == 0, proc.stderr[-1500:]
    return ws


def test_the_deck_holds_only_3d_elements(bracket_ws):
    deck = _read_deck(bracket_ws / "mesh.inp")
    assert deck["types"] == {"C3D10"}
    assert all(len(c) == 10 for c in deck["elements"].values())
    assert sorted(deck["elsets"]["solid"]) == sorted(deck["elements"])
    text = (bracket_ws / "mesh.inp").read_text()
    assert "CPS" not in text and "S3R" not in text
    assert "** UNITS: lengths in metres (m)" in text
    meshio = pytest.importorskip("meshio")
    m = meshio.read(bracket_ws / "mesh.inp", file_format="abaqus")
    assert [c.type for c in m.cells] == ["tetra10"]


def test_every_cad_face_is_a_named_surface_with_one_element_face_per_triangle(bracket_ws):
    gmsh = pytest.importorskip("gmsh")
    deck = _read_deck(bracket_ws / "mesh.inp")
    q = json.loads((bracket_ws / "quality.json").read_text())
    names = q["deck"]["face_sets"]
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(bracket_ws / "mesh.msh"))
        tags = sorted(t for _, t in gmsh.model.getEntities(2))
        assert sorted(names.values()) == tags                    # every CAD face, once
        assert list(names) == [f"Face_{k}" for k in range(1, len(tags) + 1)]
        for name, tag in names.items():
            _t, etags, _n = gmsh.model.mesh.getElements(2, tag)
            n_tri = sum(len(e) for e in etags)
            faces = _surface_faces(deck, name)
            assert len(faces) == n_tri > 0, name
            assert len(set(faces)) == len(faces)
            assert deck["nsets"][name]
    finally:
        gmsh.finalize()
    # the plan's names ride along as aliases over the same element faces; Face_k stays
    aliases = q["deck"]["face_aliases"]
    assert aliases["fixed_end"] == ["Face_1"] and aliases["load"] == ["Face_2"]
    assert sorted(aliases["free"]) == sorted(f"Face_{k}" for k in range(3, len(names) + 1))
    assert set(_surface_faces(deck, "fixed_end")) == set(_surface_faces(deck, "Face_1"))
    assert set(deck["nsets"]["load"]) == set(deck["nsets"]["Face_2"])


def test_each_face_label_puts_its_nodes_on_that_cad_face(bracket_ws):
    gmsh = pytest.importorskip("gmsh")
    deck = _read_deck(bracket_ws / "mesh.inp")
    names = json.loads((bracket_ws / "quality.json").read_text())["deck"]["face_sets"]
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(bracket_ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        for name, tag in names.items():
            faces = _surface_faces(deck, name)
            ids = np.unique(np.concatenate([face_nodes(np.array(deck["elements"][e]), lab)
                                            for e, lab in faces]))
            assert set(ids) == set(deck["nsets"][name]), name   # the node set is the same face
            pts = np.array([deck["nodes"][int(i)] for i in ids])
            on = np.asarray(gmsh.model.getClosestPoint(2, tag, pts.ravel())[0]).reshape(-1, 3)
            gap = float(np.abs(on - pts).max())
            assert gap < 1e-7, f"{name}: a node sits {gap:.3g} m off CAD face {tag}"
            # and the element's fourth corner is not on the face: the label is the right side
            for e, lab in faces[:50]:
                el = np.array(deck["elements"][e])
                off = (set(el[:4].tolist()) - set(face_nodes(el, lab)[:3].tolist())).pop()
                p = np.array(deck["nodes"][int(off)])
                q = np.asarray(gmsh.model.getClosestPoint(2, tag, p)[0])
                assert np.linalg.norm(q - p) > 1e-6, f"{name}: element {e} S{lab} is flat"
    finally:
        gmsh.finalize()


def test_the_nastran_deck_carries_no_boundary_triangles(bracket_ws, tmp_path):
    import shutil
    ws = tmp_path / "bdf"
    ws.mkdir()
    shutil.copy2(bracket_ws / "geometry.step", ws / "geometry.step")
    spec = json.loads((bracket_ws / "gmsh_spec.json").read_text())
    (ws / "gmsh_spec.json").write_text(json.dumps({**spec, "element_order": 1,
                                                   "extra_exports": ["bdf"]}))
    proc = _run_driver(ws)
    assert proc.returncode == 0, proc.stderr[-1500:]
    cards = {line[:8].strip().rstrip("*") for line in (ws / "mesh.bdf").read_text().splitlines()
             if line[:1].isalpha()}
    assert "CTETRA" in cards and not cards & {"CTRIA3", "CTRIA6"}
    # a first-order deck is C3D4 with the same named faces
    deck = _read_deck(ws / "mesh.inp")
    assert deck["types"] == {"C3D4"} and "Face_1" in deck["surfaces"]
    q = json.loads((ws / "quality.json").read_text())
    assert set(q["groups"]) == {"fixed end", "load", "free"}


#: The keywords a Hexera mesh deck may hold - all in the subset Abaqus and CalculiX share.
_DECK_KEYWORDS = {"*NODE", "*ELEMENT", "*ELSET", "*SURFACE", "*NSET"}


def test_the_deck_keeps_the_abaqus_input_rules(bracket_ws):
    """A reader's-eye check of the file against the Abaqus input rules a solver enforces: known
    keywords, data lines under 256 characters, at most 16 ids on a set line, set names a solver
    accepts, every id defined once, every set and node it points at defined before use."""
    nodes: set[int] = set()
    elements: set[int] = set()
    elsets: set[str] = set()
    names: set[str] = set()
    key = ""
    for n, line in enumerate((bracket_ws / "mesh.inp").read_text().splitlines(), start=1):
        assert len(line) <= 256, f"line {n} is {len(line)} characters"
        if line.startswith("**"):
            continue
        if line.startswith("*"):
            key = line.split(",")[0].strip().upper()
            assert key in _DECK_KEYWORDS, f"line {n}: {key}"
            opts = dict(re.findall(r"(\w+)=([^,\s]+)", line))
            for o in ("ELSET", "NSET", "NAME"):
                if o in opts:
                    v = opts[o]
                    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{0,79}", v), f"line {n}: {v}"
                    assert (key, v.lower()) not in names, f"line {n}: {v} defined twice"
                    names.add((key, v.lower()))
            if key == "*ELSET" or (key == "*ELEMENT" and "ELSET" in opts):
                elsets.add(opts["ELSET"].lower())
            continue
        f = [v.strip() for v in line.split(",")]
        if key == "*NODE":
            assert len(f) == 4 and int(f[0]) > 0 and int(f[0]) not in nodes
            nodes.add(int(f[0]))
        elif key == "*ELEMENT":
            assert int(f[0]) > 0 and int(f[0]) not in elements
            assert set(map(int, f[1:])) <= nodes, f"line {n}: an element node is undefined"
            elements.add(int(f[0]))
        elif key == "*ELSET":
            assert len(f) <= 16 and set(map(int, f)) <= elements
        elif key == "*NSET":
            assert len(f) <= 16 and set(map(int, f)) <= nodes
        elif key == "*SURFACE":
            assert len(f) == 2 and f[0].lower() in elsets and f[1] in ("S1", "S2", "S3", "S4")


@pytest.mark.skipif(not (CCX and Path(CCX).exists()),
                    reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
def test_calculix_solves_the_deck_as_delivered(bracket_ws, tmp_path):
    """The delivered deck with only a material, a section, a support and a load appended - on
    its own named faces - runs in CalculiX: no gen3delem refusal, displacements come back."""
    job = tmp_path / "bracket.inp"
    job.write_text((bracket_ws / "mesh.inp").read_text() + """
*MATERIAL, NAME=STEEL
*ELASTIC
210e9, 0.3
*SOLID SECTION, ELSET=solid, MATERIAL=STEEL
*BOUNDARY
fixed_end, 1, 3, 0.0
*STEP
*STATIC
*DSLOAD
load, P, 1.0e6
*NODE PRINT, NSET=Face_3
U
*END STEP
""")
    proc = subprocess.run([CCX, "bracket"], cwd=tmp_path, capture_output=True, text=True,
                          timeout=1200, env={**os.environ, "OMP_NUM_THREADS": "2"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "*ERROR" not in out, out[-1500:]
    u = [float(v) for ln in (tmp_path / "bracket.dat").read_text().splitlines()
         if len(p := ln.split()) == 4 and p[0].isdigit() for v in p[1:]]
    assert u and max(abs(v) for v in u) > 0


def test_names_a_cad_file_gives_its_faces_reach_the_deck(tmp_path):
    """A STEP that names two faces, staged in another unit as a run stages it: the deck writes
    both names over the right Face_k, and every other face stays Face_k alone."""
    gmsh = pytest.importorskip("gmsh")
    pytest.importorskip("OCP")
    from meshpipeline.cad.face_names import RECORD, read_named_faces, record_for_staging

    src, ws = tmp_path / "part.step", tmp_path / "ws"
    ws.mkdir()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        box = occ.addBox(0, 0, 0, 60, 40, 30)                      # millimetres
        occ.cut([(3, box)], [(3, occ.addCylinder(30, 20, -10, 0, 0, 50, 8))])
        occ.synchronize()
        gmsh.write(str(src))
    finally:
        gmsh.finalize()
    text = src.read_text()
    for name in ("Mounting face", "Bore A"):
        text = text.replace("ADVANCED_FACE('',", f"ADVANCED_FACE('{name}',", 1)
    src.write_text(text)
    named, _frame = read_named_faces(src)
    assert sorted(n for n, _c, _a in named) == ["Bore A", "Mounting face"]

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)       # staged in metres
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        assert record_for_staging(gmsh, src, ws) == 2
    finally:
        gmsh.finalize()
    assert (ws / RECORD).exists()
    (ws / "gmsh_spec.json").write_text(json.dumps({
        "element_order": 1, "size": {"mode": "factor", "value": 0.15}, "optimize": False}))
    proc = _run_driver(ws)
    assert proc.returncode == 0, proc.stderr[-1500:]
    deck = json.loads((ws / "quality.json").read_text())["deck"]
    assert sorted(deck["cad_face_names"].values()) == ["Bore_A", "Mounting_face"]
    assert len(deck["face_sets"]) == 7
    # each name sits on the face it was given: the face's centre, back in millimetres
    by_name = {n: c for n, c, _a in named}
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        for face, name in deck["cad_face_names"].items():
            tag = deck["face_sets"][face]
            c = np.asarray(gmsh.model.occ.getCenterOfMass(2, tag)) * 1000.0
            assert np.allclose(c, by_name[name.replace("_", " ")], atol=1e-6), (face, name)
    finally:
        gmsh.finalize()
    inp = (ws / "mesh.inp").read_text()
    assert "*SURFACE, NAME=Bore_A, TYPE=ELEMENT" in inp


def test_a_file_without_face_names_writes_no_record(tmp_path):
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.cad.face_names import RECORD, record_for_staging
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.write(str(tmp_path / "plain.step"))
        (tmp_path / RECORD).write_text("[]")              # a stale record from an earlier stage
        assert record_for_staging(gmsh, tmp_path / "plain.step", tmp_path) == 0
    finally:
        gmsh.finalize()
    assert not (tmp_path / RECORD).exists()
