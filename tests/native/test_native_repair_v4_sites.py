# Responsibility: Prove failure sites on the REAL kernel and the REAL mesher: the sites_* kernel
#                 functions measure what they claim on boxes built here, and the ABC overlapping-
#                 facets file (a real gmsh failure) yields a site naming real face ids.
# Boundaries: the native tier. The clustering/observation rules are unit-tested; this module only
#             checks the geometry and the end-to-end path. The corpus test skips if the file is absent.
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")
pytest.importorskip("gmsh", reason="gmsh is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402

from meshpipeline.cad.repair.mesh_probe import faces_for_tags, probe_file  # noqa: E402
from meshpipeline.cad.repair_v4 import (
    kernel_calls,  # noqa: E402,F401  (first: it wires the kernel modules)
    render,  # noqa: E402
)
from meshpipeline.cad.repair_v4.sites import find_sites, observation  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS", "~/archductor/mesh-fail-corpus")).expanduser()
_ABC = "abc/00500400_bce216c87d2d17025db6ab36_step_000.step"


class Located:
    def __init__(self, entity, xyz, mesher_tag):
        self.entity, self.xyz, self.mesher_tag = entity, xyz, mesher_tag


class Member:
    def __init__(self, name, status, error_kind, message, located):
        self.name, self.status, self.error_kind = name, status, error_kind
        self.message, self.located = message, located
        self.min_sicn, self.elements, self.seconds = None, None, 0.0


class Verdict:
    def __init__(self, valid, panel):
        self.valid, self.panel, self.faithful, self.clean, self.tier = valid, panel, None, False, "fail"


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=180)
    try:
        yield p
    finally:
        p.close()


def _two_boxes(tmp_path: Path, gap: float = 0.5) -> str:
    """Box A [0,10]^3 and box B [10+gap, 20+gap] x [0,10]^2 in one compound: faces facing across `gap`."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    for x in (0.0, 10.0 + gap):
        builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 10.0, 10.0, 10.0).Shape())
    path = tmp_path / "two.brep"
    assert BRepTools.Write_s(compound, str(path))
    return str(path)


def _faces_at_x(pool: WorkerPool, brep: str, x: float) -> list[int]:
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep

    shape = read_brep(brep)
    faces = _maps(shape)["face"]
    out = []
    for i in range(1, faces.Extent() + 1):
        from meshpipeline.cad.repair_v4.sites_kernel import _box

        box = _box(faces.FindKey(i))
        assert box is not None
        if abs(box[0] - x) < 1e-6 and abs(box[3] - x) < 1e-6:
            out.append(i)
    return out


def test_distance_proximity_and_locate_on_real_boxes(pool: WorkerPool, tmp_path: Path) -> None:
    brep = _two_boxes(tmp_path)
    (a,) = _faces_at_x(pool, brep, 10.0)
    (b,) = _faces_at_x(pool, brep, 10.5)
    got = pool.call("sites_distance", {"brep_path": brep, "pairs": [
        {"a": {"type": "face", "index": a}, "b": {"type": "face", "index": b}}]}, timeout_s=120)
    assert got.ok, got.error
    assert got.value is not None
    (d,) = got.value["distances"]
    assert d["dist"] == pytest.approx(0.5) and d["adjacent"] is False

    prox = pool.call("sites_proximity", {"brep_path": brep, "max_dist": 1.0, "max_pairs": 50}, timeout_s=120)
    assert prox.ok and prox.value is not None
    # the facing pair, plus the box sides that are coplanar across the same 0.5 gap; all 0.5 apart
    pairs = {(p["a"], p["b"]): p["dist"] for p in prox.value["pairs"]}
    assert pairs[tuple(sorted((a, b)))] == pytest.approx(0.5)
    assert all(d == pytest.approx(0.5) for d in pairs.values())
    assert not any(x in (a, b) and y in (a, b) and (x, y) != tuple(sorted((a, b))) for x, y in pairs)
    far = pool.call("sites_proximity", {"brep_path": brep, "max_dist": 0.1, "max_pairs": 8}, timeout_s=120)
    assert far.ok and far.value is not None and far.value["pairs"] == []

    located = pool.call("sites_locate_points", {"brep_path": brep, "points": [[10.0, 5.0, 5.0], [50, 50, 50]],
                                                "radius": 0.2}, timeout_s=120)
    assert located.ok and located.value is not None
    assert located.value["faces"][0][0] == a and located.value["faces"][1] == []


def test_two_hop_graph_of_a_box_face(pool: WorkerPool, tmp_path: Path) -> None:
    brep = _two_boxes(tmp_path)
    (a,) = _faces_at_x(pool, brep, 10.0)
    got = pool.call("sites_graph", {"brep_path": brep, "k": 2, "sites": [
        {"id": "S1", "seeds": [{"id": "F", "type": "face", "index": a}]}]}, timeout_s=120)
    assert got.ok, got.error
    assert got.value is not None
    graph = got.value["graphs"]["S1"]
    hops = {n["id"]: n["hop"] for n in graph["nodes"]}
    assert hops[f"F{a}"] == 0 and sorted(hops.values()).count(1) == 4 and max(hops.values()) == 2
    assert len(graph["nodes"]) == 6  # one box: the seed, its 4 neighbours, the opposite face
    node = next(n for n in graph["nodes"] if n["id"] == f"F{a}")
    assert node["surface"] == "plane" and node["area"] == pytest.approx(100.0)
    assert node["min_width"] == pytest.approx(10.0, rel=1e-3) and node["angular_span_deg"] is None
    edge = graph["edges"][0]
    assert edge["dihedral_deg"] == pytest.approx(90.0, abs=1e-3)
    assert edge["continuity"] == "C0" and edge["gap"] == pytest.approx(0.0, abs=1e-6)
    assert edge["tolerance"] > 0


def test_free_edges_of_an_open_shell_are_in_the_graph(pool: WorkerPool, tmp_path: Path) -> None:
    """For an open site the free edges ARE the defect: a lone square face has four, and a closed
    box has none."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln

    path = tmp_path / "square.brep"
    assert BRepTools.Write_s(BRepBuilderAPI_MakeFace(gp_Pln(), 0.0, 10.0, 0.0, 10.0).Face(), str(path))
    got = pool.call("sites_graph", {"brep_path": str(path), "k": 2, "sites": [
        {"id": "S", "seeds": [{"id": "F", "type": "face", "index": 1}]}]}, timeout_s=120)
    assert got.ok and got.value is not None, got.error
    edges = got.value["graphs"]["S"]["edges"]
    assert sorted(e["kind"] for e in edges) == ["free"] * 4
    assert all(e["faces"] == ["F1"] and e["tolerance"] > 0 for e in edges)
    closed = pool.call("sites_graph", {"brep_path": _two_boxes(tmp_path), "k": 2, "sites": [
        {"id": "S", "seeds": [{"id": "F", "type": "face", "index": 1}]}]}, timeout_s=120)
    assert closed.ok and closed.value is not None
    assert not [e for e in closed.value["graphs"]["S"]["edges"] if "kind" in e]


def test_proximity_ties_are_broken_by_place_not_face_order(pool: WorkerPool, tmp_path: Path) -> None:
    brep = _two_boxes(tmp_path)
    full = pool.call("sites_proximity", {"brep_path": brep, "max_dist": 1.0, "max_pairs": 50}, timeout_s=120)
    assert full.ok and full.value is not None
    keys = [(round(p["dist"], 4), [round(c, 3) for c in p["point_a"]]) for p in full.value["pairs"]]
    assert keys == sorted(keys) and all(p["point_a"] and p["point_b"] for p in full.value["pairs"])


def test_cylinder_reports_its_angular_span(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    path = tmp_path / "cyl.brep"
    assert BRepTools.Write_s(BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape(), str(path))
    got = pool.call("sites_graph", {"brep_path": str(path), "k": 1, "sites": [
        {"id": "S1", "seeds": [{"id": "F", "type": "face", "index": i} for i in (1, 2, 3)]}]}, timeout_s=120)
    assert got.ok and got.value is not None
    spans = [n["angular_span_deg"] for n in got.value["graphs"]["S1"]["nodes"] if n["surface"] == "cylinder"]
    assert spans and spans[0] == pytest.approx(360.0)


def test_render_view_is_a_512px_png(pool: WorkerPool, tmp_path: Path) -> None:
    pytest.importorskip("pyvista")
    brep = _two_boxes(tmp_path)
    (a,) = _faces_at_x(pool, brep, 10.0)
    ws = Workspace(brep_path=brep, ids={"F1": "x"}, labels={}, history=[], original=brep, indices={"F1": a})
    from meshpipeline.cad.repair_v4.types import Site

    site = Site("S1", "proximity", ["F1"], [(10.0, 5.0, 5.0)], ["gmsh"], None, {}, None)
    png = render.view(pool, ws, site)
    assert png is not None and site.view_png == png
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 0
    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(png)).size == (512, 512)


def test_abc_overlapping_facets_file_yields_a_site_with_real_face_ids(pool: WorkerPool, tmp_path: Path) -> None:
    source = _CORPUS / _ABC
    if not source.is_file():
        pytest.skip(f"mesh-failure corpus file {source} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    # The corpus test meshes this file at diagonal 132.3 and expects failure; in the current mesh
    # image gmsh MESHES it there (pre-existing, identical on the base branch: test_native_repair_mesh_probe
    # fails the same way). A coarser size (diagonal 264 -> h 6.6 mm) reproduces overlapping_facets.
    diagonal = 264.0
    read = pool.call("read_step_xcaf", {"path": str(source), "brep_out": str(tmp_path / "abc.brep")}, timeout_s=300)
    assert read.ok and read.value is not None, read.error
    ws = Workspace.from_brep(pool, read.value["brep"], timeout_s=300)

    probe = probe_file(source, diagonal_mm=diagonal)
    assert probe.failure is not None and probe.failure.surfaces
    boxes = pool.call("sites_geometry", {"brep_path": ws.brep_path, "entities": [
        {"id": sid, "type": "face", "index": ws.indices[sid]} for sid in ws.ids if sid[0] == "F"]}, timeout_s=300)
    assert boxes.ok and boxes.value is not None
    face_boxes = {ws.indices[sid]: tuple(g["box"]) for sid, g in boxes.value["geometry"].items()}
    mapping = faces_for_tags(face_boxes, {t: probe.tag_boxes[t] for t in probe.failure.surfaces},
                             tol=1e-6 * diagonal)
    by_index = {idx: sid for sid, idx in ws.indices.items() if sid[0] == "F"}
    located = [Located(by_index[mapping[t]], None, f"surface {t}") for t in probe.failure.surfaces if t in mapping]
    assert located, "the failing gmsh surfaces must map to OCC faces for this test to mean anything"

    member = Member("gmsh", "fail", probe.failure.kind, probe.failure.message, located)
    valid = ValidReport(True, [], [], "mm", 1, True)
    verdict = Verdict(valid, [member])
    sites = find_sites(pool, ws, verdict, h_mm=diagonal / 40.0)
    assert sites, "a real overlapping-facets failure must yield a site"
    named = {e for s in sites for e in s.entities}
    assert named & {loc.entity for loc in located}
    assert all(e in ws.ids for e in named)
    assert any(s.kind in ("proximity", "self_overlap") for s in sites)
    assert any(s.graph.get("nodes") for s in sites), "the k=2 graph must have been measured"
    obs = observation(ws, verdict, sites, {"usd_left": 1.0})
    assert len(json.dumps(obs).encode()) < 60_000
