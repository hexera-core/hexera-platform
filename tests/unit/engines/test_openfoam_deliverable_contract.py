# Responsibility: Verify an OpenFOAM deliverable is judged on its files and manifest, not on a completion marker.
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.gates import GateCtx, run_gates
from meshpipeline.engines.registry import get_spec
from meshpipeline.pipeline.graph import route_after_executor

SINGLE_REGION = ("cfmesh", "snappy")
POLYMESH_FILES = ("owner", "neighbour", "points", "faces", "boundary")


def _manifest(**over) -> dict:
    m = {
        "schema_version": "2.1",
        "geometry": {},
        "patches": {"body": [], "farfield": []},
        "cell_count": 1000,
        "mesh_units": "m",
        "mesh_written": True,
        "domain": "d",
        "patch_types": {"body": "wall", "farfield": "farfield"},
        "patch_face_counts": {"body": 500, "farfield": 200},
        "validation": {"has_wall": True, "has_inflow": True, "has_outflow": True,
                       "patch_validation": {"body": True, "farfield": True}},
        "quality": {"fatal": [], "cells": 1000, "skew_fraction": 1e-5, "max_non_ortho": 40.0},
    }
    m.update(over)
    return m


def _polymesh(ws, *, files=POLYMESH_FILES):
    pm = ws / "constant" / "polyMesh"
    pm.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f == "boundary":
            (pm / f).write_text(
                "FoamFile{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }\n"
                "2\n(\n    body\n    {\n        type wall;\n        nFaces 500;\n"
                "        startFace 0;\n    }\n    farfield\n    {\n        type patch;\n"
                "        nFaces 200;\n        startFace 500;\n    }\n)\n")
        else:
            (pm / f).write_text("0\n(\n)\n")
    return pm


def _gate(ws, engine: str, manifest: dict | None = None, intake_patches=None):
    if manifest is not None:
        (ws / "mesh_manifest.json").write_text(json.dumps(manifest))
    ctx = GateCtx(workspace=ws, engine=engine, domain="d",
                  intake_patches=intake_patches or [], engine_params={})
    rows: list = []
    ok, failed, feedback = run_gates(
        get_spec(engine).gates, ctx,
        on_result=lambda k, o, fb: rows.append((k, bool(o), str(fb))))
    route = route_after_executor({"executor_success": bool(ok), "retry_count": 99,
                                  "solvability_failed": False})
    return {"ok": bool(ok), "failed": failed, "feedback": feedback, "rows": rows,
            "reviewer_invocations": 1 if route == "node_reviewer" else 0}


def _assert_rejected_before_review(out, engine: str):
    assert not out["ok"], f"{engine}: a defective deliverable passed the gate chain"
    assert out["reviewer_invocations"] == 0, f"{engine}: the reviewer was invoked anyway"
    assert out["failed"] in {g.key for g in get_spec(engine).gates}, (
        f"{engine}: rejected by `{out['failed']}`, which is not a declared gate")
    assert out["feedback"].strip(), f"{engine}: refusal carried no diagnostic"


# positive control

@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_complete_polymesh_passes(tmp_path, engine):
    _polymesh(tmp_path)
    out = _gate(tmp_path, engine, _manifest())
    assert out["ok"], f"{engine}: a complete deliverable was rejected by {out['failed']}: {out['feedback']}"
    assert out["reviewer_invocations"] == 1


# missing pieces

@pytest.mark.parametrize("engine", SINGLE_REGION)
@pytest.mark.parametrize("missing", POLYMESH_FILES)
def test_a_missing_polymesh_file_is_rejected(tmp_path, engine, missing):
    _polymesh(tmp_path, files=tuple(f for f in POLYMESH_FILES if f != missing))
    out = _gate(tmp_path, engine, _manifest())
    _assert_rejected_before_review(out, engine)
    assert missing in out["feedback"], (
        f"{engine}: the refusal for a missing `{missing}` does not name it: {out['feedback']}")


@pytest.mark.parametrize("engine", SINGLE_REGION)
@pytest.mark.parametrize("truncated", POLYMESH_FILES)
def test_a_zero_byte_polymesh_file_is_rejected(tmp_path, engine, truncated):
    pm = _polymesh(tmp_path)
    (pm / truncated).write_text("")
    out = _gate(tmp_path, engine, _manifest())
    _assert_rejected_before_review(out, engine)


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_missing_owner_is_rejected_and_names_the_artifact(tmp_path, engine):
    _polymesh(tmp_path, files=tuple(f for f in POLYMESH_FILES if f != "owner"))
    out = _gate(tmp_path, engine, _manifest())
    _assert_rejected_before_review(out, engine)
    assert "owner" in out["feedback"], out["feedback"]


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_an_entirely_absent_polymesh_is_rejected(tmp_path, engine):
    out = _gate(tmp_path, engine, _manifest())
    _assert_rejected_before_review(out, engine)


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_an_empty_polymesh_directory_is_rejected(tmp_path, engine):
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    out = _gate(tmp_path, engine, _manifest())
    _assert_rejected_before_review(out, engine)


# malformed / mismatched

@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_malformed_manifest_is_rejected(tmp_path, engine):
    _polymesh(tmp_path)
    (tmp_path / "mesh_manifest.json").write_text('{"schema_version": 2.1, "geometry": {,,')
    out = _gate(tmp_path, engine, None)
    _assert_rejected_before_review(out, engine)


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_manifest_claiming_a_mesh_that_was_not_written_is_rejected(tmp_path, engine):
    _polymesh(tmp_path)
    out = _gate(tmp_path, engine, _manifest(mesh_written=False))
    _assert_rejected_before_review(out, engine)


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_patch_with_zero_faces_is_rejected(tmp_path, engine):
    _polymesh(tmp_path)
    m = _manifest()
    m["validation"]["patch_validation"]["farfield"] = False
    out = _gate(tmp_path, engine, m)
    _assert_rejected_before_review(out, engine)


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_fabricated_completion_marker_without_deliverables_is_rejected(tmp_path, engine):
    out = _gate(tmp_path, engine, _manifest(cell_count=1_000_000))
    _assert_rejected_before_review(out, engine)


# boundary typing

@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_declared_role_not_evidenced_by_the_boundary_type_is_rejected(tmp_path, engine):
    pm = _polymesh(tmp_path)
    (pm / "boundary").write_text(
        "FoamFile{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }\n"
        "1\n(\n    side\n    {\n        type patch;\n        nFaces 300;\n"
        "        startFace 0;\n    }\n)\n")
    m = _manifest()
    m["patch_types"] = {"side": "symmetry"}
    m["patches"] = {"side": []}
    m["patch_face_counts"] = {"side": 300}
    m["validation"]["patch_validation"] = {"side": True}
    out = _gate(tmp_path, engine, m, intake_patches=[{"name": "side", "type": "symmetry"}])
    _assert_rejected_before_review(out, engine)
    assert "side" in out["feedback"], out["feedback"]


# layout is engine-specific

def test_a_single_region_layout_is_not_a_multiregion_deliverable(tmp_path):
    _polymesh(tmp_path)
    out = _gate(tmp_path, "snappy_multiregion", _manifest())
    _assert_rejected_before_review(out, "snappy_multiregion")
    assert "regionProperties" in out["feedback"], out["feedback"]


@pytest.mark.parametrize("engine", SINGLE_REGION)
def test_a_multiregion_layout_is_not_a_single_region_deliverable(tmp_path, engine):
    for region in ("fluid", "solid"):
        pm = tmp_path / "constant" / region / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        for f in POLYMESH_FILES:
            (pm / f).write_text("0\n(\n)\n")
    (tmp_path / "constant" / "regionProperties").write_text("regions ( fluid ( fluid ) );\n")
    out = _gate(tmp_path, engine, _manifest())
    _assert_rejected_before_review(out, engine)


# mesh_written asks the engine what it delivers
#
# The flag is documented as "the mesh physically exists". It used to be a hardcoded
# constant/polyMesh/owner probe, which is the right question for the three OpenFOAM engines and
# structurally wrong for gmsh (mesh.inp) and vmtk (mesh.vtu): a real 35,994-tetrahedron vmtk mesh
# published mesh_written: false. Each engine already declares its own marker.
import pytest

from meshpipeline.engines.manifest import _delivered_mesh_exists

_MARKERS = {"cfmesh": "constant/polyMesh/owner", "snappy": "constant/polyMesh/owner",
            "snappy_multiregion": "constant/regionProperties",
            "gmsh": "mesh.inp", "vmtk": "mesh.vtu"}


@pytest.mark.parametrize("engine,marker", sorted(_MARKERS.items()))
def test_each_engine_is_asked_for_its_own_delivered_artifact(tmp_path, engine, marker):
    assert get_spec(engine).deliverable.marker == marker, (
        "the engine's declared marker moved; this contract follows the spec, not a copy")
    assert _delivered_mesh_exists(tmp_path, engine) is False, "an empty workspace claims a mesh"
    target = tmp_path / marker
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    assert _delivered_mesh_exists(tmp_path, engine) is True, (
        f"{engine} delivered {marker} and mesh_written still says no")


def test_an_openfoam_probe_alone_no_longer_answers_for_every_engine(tmp_path):
    # The exact regression: the OpenFOAM artifact present, the engine's own artifact absent.
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "owner").write_text("x")
    assert _delivered_mesh_exists(tmp_path, "vmtk") is False, (
        "vmtk reported a mesh because an unrelated polyMesh file existed")
    assert _delivered_mesh_exists(tmp_path, "gmsh") is False
    assert _delivered_mesh_exists(tmp_path, "cfmesh") is True


def test_an_unregistered_engine_does_not_claim_a_mesh(tmp_path):
    assert _delivered_mesh_exists(tmp_path, "not_a_real_engine") is False


def _write(tmp_path, engine, **kw):
    from meshpipeline.engines.manifest import write_manifest
    write_manifest(tmp_path, patch_types={}, patch_entities={}, quality={"cells": 12},
                   bbox=[0.0, 0.0, 0.0, 1.0, 1.0, 1.0], mesh_mode=engine, mesh_units="m", **kw)
    return json.loads((tmp_path / "mesh_manifest.json").read_text())


def test_the_manifest_field_itself_follows_the_engine_not_openfoam(tmp_path):
    # The production seam: the published mesh_written, for a non-OpenFOAM engine whose real
    # artifact is present. This is the exact shape of the vmtk result that reported false.
    (tmp_path / "mesh.vtu").write_text("x")
    assert _write(tmp_path, "vmtk")["mesh_written"] is True, (
        "vmtk delivered mesh.vtu and the manifest still published mesh_written: false")


def test_the_manifest_field_is_false_when_the_engine_delivered_nothing(tmp_path):
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "owner").write_text("x")
    assert _write(tmp_path, "vmtk")["mesh_written"] is False, (
        "an unrelated polyMesh file made vmtk claim a mesh")
    assert _write(tmp_path, "cfmesh")["mesh_written"] is True


# geometry.box_* is the ACTUAL meshed extent, for every engine
#
# The schema defines it that way. snappy, cfMesh and multi-region already passed final measured
# bounds; gmsh passed the OCC model hull (which on curved BSpline faces overstates the real extent
# - the elbow read 0.193 x 0.368 x 0.075 m as a hull against a mesh occupying 0.175 x 0.350 x
# 0.050) and vmtk passed the staged input surface. Both now measure the finished mesh.

_STAGED = (0.0, 0.0, 0.0, 2.0, 2.0, 2.0)      # prepared geometry: deliberately larger
_FINAL = (0.1, 0.2, 0.3, 1.1, 1.2, 1.3)       # what the mesh actually occupies


def _manifest_with(tmp_path, engine, *, mesh_bounds, bbox=_STAGED):
    from meshpipeline.engines.manifest import write_manifest
    marker = get_spec(engine).deliverable.marker
    t = tmp_path / marker
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("x")
    write_manifest(tmp_path, patch_types={}, patch_entities={}, quality={"cells": 5},
                   bbox=bbox, mesh_bounds=mesh_bounds, mesh_mode=engine, mesh_units="m")
    return json.loads((tmp_path / "mesh_manifest.json").read_text())["geometry"]


@pytest.mark.parametrize("engine", ["gmsh", "vmtk", "cfmesh", "snappy", "snappy_multiregion"])
def test_every_engine_publishes_the_supplied_final_mesh_bounds(tmp_path, engine):
    g = _manifest_with(tmp_path, engine, mesh_bounds=_FINAL)
    got = (g["box_xmin"], g["box_ymin"], g["box_zmin"], g["box_xmax"], g["box_ymax"], g["box_zmax"])
    assert got == pytest.approx(_FINAL), f"{engine} published {got}, not the final mesh bounds"
    assert got != pytest.approx(_STAGED), f"{engine} published the prepared geometry instead"


def test_the_published_span_is_the_final_span_not_the_prepared_one(tmp_path):
    # The elbow shape of the defect: a prepared hull strictly larger than the mesh.
    g = _manifest_with(tmp_path, "gmsh", mesh_bounds=_FINAL)
    assert g["box_xmax"] - g["box_xmin"] == pytest.approx(1.0)
    assert g["box_xmax"] - g["box_xmin"] != pytest.approx(2.0), "the prepared hull span survived"


def test_the_gmsh_driver_measures_bounds_on_the_mesh_not_the_cad_model(tmp_path):
    # Runtime behaviour of the helper the driver publishes from: a model whose CAD hull is larger
    # than its nodes must report the nodes.
    import numpy as np

    from meshpipeline.engines.gmsh.driver import _final_node_bounds

    class _FakeMesh:
        def getNodes(self):
            return ([1, 2], np.array([0.1, 0.2, 0.3, 1.1, 1.2, 1.3]), [])

    class _FakeModel:
        mesh = _FakeMesh()

        def getBoundingBox(self, *_a):
            return (-9.0, -9.0, -9.0, 9.0, 9.0, 9.0)     # the tolerant CAD hull

    class _FakeGmsh:
        model = _FakeModel()

    assert _final_node_bounds(_FakeGmsh()) == pytest.approx(list(_FINAL))


def test_an_empty_gmsh_mesh_reports_no_extent_rather_than_a_cad_hull(tmp_path):
    import numpy as np

    from meshpipeline.engines.gmsh.driver import _final_node_bounds

    class _FakeGmsh:
        class model:
            class mesh:
                @staticmethod
                def getNodes():
                    return ([], np.array([]), [])

    assert _final_node_bounds(_FakeGmsh()) == [0.0] * 6
