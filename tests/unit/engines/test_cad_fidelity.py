# Responsibility: Verify the CAD fidelity checks tell a mesh that IS the part from one that is not: a boundary on the CAD passes, a mesh of a smaller region is refused for a rebuild, a small miss is a concern, and what cannot be measured is said, never guessed.
# Boundaries: engines/cad_fidelity.py on synthetic workspaces - a cube CAD in metres and a boundary written the way foamToVTK writes an OpenFOAM one; no mesher runs.
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")

from meshpipeline.engines import cad_fidelity as F  # noqa: E402
from meshpipeline.engines.gates import GateCtx  # noqa: E402

PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
           {"name": "wall", "type": "wall"}]


def _write_cad(path: Path, bounds=(0, 1, 0, 0.4, 0, 0.4)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pv.Box(bounds=bounds, level=6).triangulate().save(str(path))


def _box_patches(bounds, level=10, *, inward=False) -> dict:
    """A box's boundary as patches: -x face inlet, +x face outlet, the rest wall; quads, normals
    out of the box (or into it, for a body inside a fluid)."""
    box = pv.Box(bounds=bounds, level=level, quads=True).compute_normals(
        cell_normals=True, point_normals=False, auto_orient_normals=True)
    n = np.asarray(box.cell_data["Normals"])
    if inward:
        box = box.flip_faces()
    out = {}
    for name, mask in (("inlet", n[:, 0] < -0.9), ("outlet", n[:, 0] > 0.9),
                       ("wall", np.abs(n[:, 0]) <= 0.9)):
        out[name] = box.extract_cells(np.flatnonzero(mask)).extract_surface(
            algorithm="dataset_surface")
    return out


def _write_boundary(ws: Path, patches: dict) -> None:
    d = ws / "VTK" / "case_0" / "boundary"
    d.mkdir(parents=True, exist_ok=True)
    for name, surf in patches.items():
        surf.save(str(d / f"{name}.vtp"))


def _internal_ws(tmp_path: Path, mesh_bounds) -> Path:
    ws = tmp_path / "ws"
    _write_cad(ws / "_internal_stls" / "fluid_boundary.stl")
    _write_boundary(ws, _box_patches(mesh_bounds))
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"flow_topology": "internal", "patch_types": {p["name"]: p["type"] for p in PATCHES},
         "quality": {}}))
    return ws


def test_a_mesh_on_the_cad_passes_every_measure(tmp_path):
    ws = _internal_ws(tmp_path, (0, 1, 0, 0.4, 0, 0.4))
    rec = F.measure(ws, intake_patches=PATCHES, flow="internal",
                    manifest=json.loads((ws / "mesh_manifest.json").read_text()))
    assert rec["verdict"] == F.PASS, rec
    assert rec["surface"]["mesh_to_cad"]["wall_off_share"] == 0
    assert rec["surface"]["cad_to_mesh"]["cad_missed_share"] == 0
    assert abs(rec["volume"]["error"]) < 1e-6 and rec["volume"]["what"] == "fluid"
    assert F.concerns(rec) == []


def test_a_mesh_of_a_smaller_region_is_refused_for_a_rebuild(tmp_path, monkeypatch):
    # the mesh fills 0.6 of the duct's length: a third of the CAD is not in it
    ws = _internal_ws(tmp_path, (0, 0.6, 0, 0.4, 0, 0.4))
    ctx = GateCtx(workspace=ws, intake_patches=PATCHES)
    ok, feedback = F.gate_cad_fidelity(ctx)
    assert not ok
    from meshpipeline.engines.gates import cause_of
    assert cause_of(feedback) == "patch_not_captured"
    rec = json.loads((ws / "mesh_manifest.json").read_text())["quality"][F.RECORD_KEY]
    assert rec["verdicts"]["volume"] == F.REBUILD and rec["volume"]["error"] == pytest.approx(-0.4, abs=1e-3)
    assert rec["verdicts"]["cad_to_mesh"] == F.REBUILD
    said = " ".join(F.concerns(rec))
    assert "40.0% smaller" in said and "not reached by the mesh" in said
    # switched off, the same mesh passes the gate and the record still says what it measured
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "CAD_FIDELITY_RETRY_ENABLED", False)
    ok, _ = F.gate_cad_fidelity(GateCtx(workspace=ws, intake_patches=PATCHES))
    assert ok


def test_a_small_volume_miss_is_a_concern_not_a_rebuild(tmp_path):
    # 5% short of the far end: more than the 3% internal concern bar, far under the rebuild bar
    ws = _internal_ws(tmp_path, (0, 0.95, 0, 0.4, 0, 0.4))
    rec = F.measure(ws, intake_patches=PATCHES, flow="internal")
    assert rec["verdicts"]["volume"] == F.CONCERN
    assert rec["verdict"] == F.CONCERN
    ok, _ = F.gate_cad_fidelity(GateCtx(workspace=ws, intake_patches=PATCHES))
    assert ok


def test_an_external_body_is_judged_by_the_volume_it_displaces(tmp_path):
    ws = tmp_path / "ws"
    _write_cad(ws / "input.stl", bounds=(0, 1, 0, 0.5, 0, 0.5))
    body = _box_patches((0, 1, 0, 0.5, 0, 0.5), inward=True)
    far = pv.Box(bounds=(-5, 6, -5, 5.5, -5, 5.5), level=2, quads=True).extract_surface(
        algorithm="dataset_surface")
    _write_boundary(ws, {"body": pv.merge(list(body.values())).extract_surface(
        algorithm="dataset_surface"), "farfield": far})
    roles = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]
    rec = F.measure(ws, intake_patches=roles, flow="external")
    assert rec["patches_compared"] == ["body"]
    assert rec["volume"]["what"] == "body"
    assert rec["volume"]["error"] == pytest.approx(0.0, abs=1e-6)
    assert rec["verdict"] == F.PASS


def test_a_body_on_the_ground_has_no_closed_volume_and_says_so(tmp_path):
    ws = tmp_path / "ws"
    _write_cad(ws / "input.stl", bounds=(0, 1, 0, 0.5, 0, 0.5))
    body = _box_patches((0, 1, 0, 0.5, 0, 0.5), inward=True)
    walls = pv.merge(list(body.values())).extract_surface(algorithm="dataset_surface")
    n = np.asarray(walls.compute_normals(cell_normals=True, point_normals=False).cell_data["Normals"])
    open_body = walls.extract_cells(np.flatnonzero(n[:, 2] < 0.9)).extract_surface(
        algorithm="dataset_surface")   # the face that stands on the ground is not a wall
    _write_boundary(ws, {"car": open_body,
                         "farfield": pv.Box(bounds=(-5, 6, -5, 5.5, 0, 5.5), level=2,
                                            quads=True).extract_surface(algorithm="dataset_surface")})
    rec = F.measure(ws, intake_patches=[{"name": "car", "type": "wall"},
                                        {"name": "farfield", "type": "farfield"}], flow="external")
    assert "volume" not in rec["verdicts"]
    assert "not closed" in rec["volume"]["why"]


def test_what_cannot_be_measured_is_said_and_never_refused(tmp_path):
    ws = tmp_path / "ws"
    _write_boundary(ws, _box_patches((0, 1, 0, 0.4, 0, 0.4)))
    rec = F.measure(ws, intake_patches=PATCHES, flow="internal")
    assert rec["verdict"] == F.UNMEASURED and "no CAD surface" in rec["why"]
    ok, _ = F.gate_cad_fidelity(GateCtx(workspace=ws, intake_patches=PATCHES))
    assert ok


def test_an_opening_off_its_confirmed_size_is_a_concern(tmp_path):
    ws = _internal_ws(tmp_path, (0, 1, 0, 0.4, 0, 0.4))
    manifest = {"quality": {"patch_areas_m2": {"inlet": 0.16 * 1.25, "outlet": 0.16},
                            "port_openings_m2": {"inlet": 0.16, "outlet": 0.16}}}
    rec = F.measure(ws, intake_patches=PATCHES, flow="internal", manifest=manifest)
    assert rec["openings"]["inlet"]["ratio"] == pytest.approx(1.25)
    assert rec["verdicts"]["openings"] == F.CONCERN
    assert any("'inlet' patch is 1.25 times" in s for s in F.concerns(rec))


def test_every_flow_engine_runs_the_gate_last():
    from meshpipeline.engines.cfmesh.flow_gates import FLOW_GATES as CF
    from meshpipeline.engines.gmsh.gates import GMSH_GATES
    from meshpipeline.engines.snappy.flow_gates import FLOW_GATES as SN
    for gates in (CF, SN, GMSH_GATES):
        assert gates[-1].key == "cad_fidelity" and gates[-1].check is F.gate_cad_fidelity


def test_staged_parts_that_also_bound_the_metal_hold_only_the_wall_to_the_cad(tmp_path):
    # the CAD path stages the part surface and the lids, not the fluid boundary alone, and no
    # metre surface is there to stage it from: the wall is held to the CAD, nothing more is claimed
    ws = tmp_path / "ws"
    _write_cad(ws / "_internal_stls" / "wall.stl")
    _write_boundary(ws, _box_patches((0, 1, 0, 0.4, 0, 0.4)))
    rec = F.measure(ws, intake_patches=PATCHES, flow="internal")
    assert rec["cad_source"] == "staged part surface and lids"
    assert rec["verdicts"] == {"mesh_to_cad": F.PASS}
    assert "not the fluid" in rec["surface"]["cad_to_mesh_why"]
    assert "not the fluid" in rec["volume"]["why"]


def test_a_slab_open_at_two_opposite_symmetry_planes_has_no_volume_to_read():
    # no end caps: the two missing faces cancel in any sum over the faces, so only the open edges
    # tell that this surface encloses nothing
    box = pv.Box(bounds=(0, 1, 0, 0.4, 0, 0.4), level=4, quads=True).compute_normals(
        cell_normals=True, point_normals=False, auto_orient_normals=True)
    n = np.asarray(box.cell_data["Normals"])
    sides = box.extract_cells(np.flatnonzero(np.abs(n[:, 0]) <= 0.9)).extract_surface(
        algorithm="dataset_surface")
    s = F._from_pyvista(sides)
    assert F.outward_volume(s) is None and F.enclosed_volume(s) is None
    assert F.is_closed(F._from_pyvista(box))


def test_a_case_of_several_passages_counts_each_passage_once(tmp_path):
    ws = tmp_path / "ws"
    _write_cad(ws / "geom.stl", bounds=(0, 1, 0, 0.4, 0, 0.4))
    _write_cad(ws / "_passages" / "p1" / "geom.stl", bounds=(0, 1, 1, 1.4, 0, 0.4))
    # the whole staged boundary holds both passages: it must not be added on top of them
    _write_cad(ws / "_internal_stls" / "fluid_boundary.stl", bounds=(0, 1, 0, 1.4, 0, 0.4))
    (ws / "passages.json").write_text(json.dumps({"runs": ["_passages/p1"]}))
    from meshpipeline.cad.fluid_passages import PASSAGES_FILE
    if PASSAGES_FILE != "passages.json":
        (ws / PASSAGES_FILE).parent.mkdir(parents=True, exist_ok=True)
        (ws / PASSAGES_FILE).write_text(json.dumps({"runs": ["_passages/p1"]}))
    cad, source, fluid_side = F.cad_reference(ws, "internal")
    assert fluid_side and source.endswith("every passage")
    assert F.enclosed_volume(cad) == pytest.approx(2 * 0.4 * 0.4, rel=1e-6)


# # an external body made of several solids: only the skin the outside air touches is the CAD the
# # mesh must reach - faces where the solids touch or overlap are inside the body

BODY_ROLES = [{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}]


def _solids(ws: Path, *bounds) -> None:
    """input.stl = several closed boxes in one file, each its own solid."""
    ws.mkdir(parents=True, exist_ok=True)
    pv.merge([pv.Box(bounds=b, level=6).triangulate() for b in bounds]).extract_surface(
        algorithm="dataset_surface").save(str(ws / "input.stl"))


def _external_mesh(ws: Path, body_bounds) -> None:
    """An external mesh's boundary: the body's skin (faces into the body) and a far-field box."""
    body = _box_patches(body_bounds, inward=True)
    far = pv.Box(bounds=(-5, 6, -5, 5.5, -5, 5.5), level=2, quads=True).extract_surface(
        algorithm="dataset_surface")
    _write_boundary(ws, {"body": pv.merge(list(body.values())).extract_surface(
        algorithm="dataset_surface"), "farfield": far})
    (ws / "mesh_manifest.json").write_text(json.dumps(
        {"flow_topology": "external", "patch_types": {"body": "wall", "farfield": "patch"},
         "quality": {}}))


@pytest.mark.parametrize("halves", [
    # two halves joined on a plane: each carries a cut face on it, inside the body
    ((0, 0.5, 0, 0.5, 0, 0.5), (0.5, 1, 0, 0.5, 0, 0.5)),
    # two solids pushed into each other: each has faces inside the other
    ((0, 0.65, 0, 0.5, 0, 0.5), (0.35, 1, 0, 0.5, 0, 0.5)),
])
def test_faces_where_solids_touch_or_overlap_are_not_missed_by_an_external_mesh(tmp_path, halves):
    ws = tmp_path / "ws"
    _solids(ws, *halves)
    _external_mesh(ws, (0, 1, 0, 0.5, 0, 0.5))      # the mesh follows the body's outer skin
    rec = F.measure(ws, intake_patches=BODY_ROLES, flow="external")
    c2m = rec["surface"]["cad_to_mesh"]
    assert c2m["cad_missed_share"] < F.CAD_MISSED_CONCERN, rec
    assert c2m["cad_hidden_share"] > 0.05               # the inside faces were set aside, said so
    assert rec["verdicts"]["cad_to_mesh"] == F.PASS
    # summed solids are not the volume the body displaces: not read, and said why
    assert "volume" not in rec["verdicts"] and "touch or overlap" in rec["volume"]["why"]
    assert rec["verdict"] == F.PASS
    assert F.gate_cad_fidelity(GateCtx(workspace=ws, intake_patches=BODY_ROLES))[0]


def test_a_fin_the_mesh_dropped_is_still_refused_when_it_is_its_own_solid(tmp_path):
    # the fin is a second solid whose root is pushed into the body: its root is hidden, but the
    # fin standing in the air is skin, and a mesh of the body alone does not reach it
    ws = tmp_path / "ws"
    _solids(ws, (0, 1, 0, 0.5, 0, 0.5), (0.4, 0.6, 0.2, 0.3, 0.3, 1.5))
    _external_mesh(ws, (0, 1, 0, 0.5, 0, 0.5))
    rec = F.measure(ws, intake_patches=BODY_ROLES, flow="external")
    assert rec["verdicts"]["cad_to_mesh"] == F.REBUILD, rec
    assert rec["surface"]["cad_to_mesh"]["cad_missed_share"] > F.CAD_MISSED_REBUILD
    ok, feedback = F.gate_cad_fidelity(GateCtx(workspace=ws, intake_patches=BODY_ROLES))
    assert not ok and "not reached by the mesh" in feedback


def test_a_lost_part_beside_the_body_is_still_refused(tmp_path):
    # a separate part standing free in the air (a pod beside the body) that the mesh lost
    ws = tmp_path / "ws"
    _solids(ws, (0, 1, 0, 0.5, 0, 0.5), (0, 1, 0.8, 1.3, 0, 0.5))
    _external_mesh(ws, (0, 1, 0, 0.5, 0, 0.5))
    rec = F.measure(ws, intake_patches=BODY_ROLES, flow="external")
    assert rec["verdicts"]["cad_to_mesh"] == F.REBUILD, rec
    assert rec["surface"]["cad_to_mesh"]["cad_hidden_share"] < 0.01   # nothing of it is hidden


def test_a_surface_with_a_hole_keeps_every_face_counted():
    # air gets in through the hole: the skin finder sets nothing aside, the old reading stands
    box = pv.Box(bounds=(0, 1, 0, 0.5, 0, 0.5), level=6, quads=True).compute_normals(
        cell_normals=True, point_normals=False, auto_orient_normals=True)
    n = np.asarray(box.cell_data["Normals"])
    open_box = F._from_pyvista(box.extract_cells(np.flatnonzero(n[:, 0] < 0.9)).extract_surface(
        algorithm="dataset_surface"))
    inner = F._from_pyvista(pv.Box(bounds=(0.3, 0.6, 0.1, 0.4, 0.1, 0.4), level=2))
    cad = F.join([open_box, inner])
    skin = F.outer_skin(cad, 0.05)
    assert skin is not None and skin(cad.centroids()).all()
