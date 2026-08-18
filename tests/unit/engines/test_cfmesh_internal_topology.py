# Responsibility: Verify flow topology is read from its own file rather than inferred, and drives internal preparation.
from __future__ import annotations

import json
from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.agents.builder.workspace import (  # noqa: E402
    _write_workspace_context_files,
    read_engine_params,
)
from meshpipeline.engines.registry import get_spec  # noqa: E402


def _surface(workspace, name="input.stl", **kw):
    from pathlib import Path

    from tests._geometry_support import prepared_surface
    return prepared_surface(Path(workspace) / name, **kw)

# the declaration reaches the engine

def test_engine_params_are_written_to_the_workspace(tmp_path):
    # engine_params.json holds engine-NATIVE knobs only (topology is NOT one - it rides the
    # neutral flow_topology file); this exercises the round-trip with a stand-in knob.
    _write_workspace_context_files(tmp_path, "/x/input.step", request_txt="r",
                                   engine_params={"wall_layers": "on"})
    assert json.loads((tmp_path / "engine_params.json").read_text()) == {"wall_layers": "on"}
    assert read_engine_params(tmp_path) == {"wall_layers": "on"}


def test_flow_topology_is_written_to_its_own_neutral_file_not_engine_params(tmp_path):
    from meshpipeline.agents.builder.workspace import read_flow_topology
    _write_workspace_context_files(tmp_path, "/x/input.step", request_txt="r",
                                   engine_params={"wall_layers": "on"}, flow_topology="internal")
    assert read_flow_topology(tmp_path) == "internal"
    assert "topology" not in read_engine_params(tmp_path)   # kept OUT of engine_params


def test_read_engine_params_is_empty_when_none_declared(tmp_path):
    assert read_engine_params(tmp_path) == {}
    _write_workspace_context_files(tmp_path, "/x/input.step", request_txt="r")
    assert not (tmp_path / "engine_params.json").exists()
    assert read_engine_params(tmp_path) == {}


def test_read_engine_params_survives_a_corrupt_file(tmp_path):
    (tmp_path / "engine_params.json").write_text("{not json")
    assert read_engine_params(tmp_path) == {}


# the spec

def test_cfmesh_admits_both_topologies():
    caps = get_spec("cfmesh").capabilities
    assert set(caps[0].topologies) == {"external", "internal"}
    assert "topology" not in [p.key for p in get_spec("cfmesh").intake_params]


def test_cfmesh_descriptor_does_not_claim_external_only():
    d = get_spec("cfmesh").descriptor.lower()
    assert "internal" in d and "external" in d
    for lie in ("only topology", "external only", "not for: internal"):
        assert lie not in d


def test_no_engine_gates_a_capability_on_our_validation_status():
    from meshpipeline.engines.registry import engine_names
    for name in engine_names():
        for p in (get_spec(name).intake_params or ()):
            ask = (p.ask or "").lower()
            assert "validated" not in ask, (
                f"{name}.{p.key} justifies its choices by our validation status: {p.ask!r}")


# configure_mesh routes on the DECLARED topology

def test_configure_mesh_reads_topology_it_does_not_infer_it(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    seen = {}

    def _fake_internal(ws, **kw):
        seen["internal"] = True
        return {"success": True}

    monkeypatch.setattr(R, "_configure_internal", _fake_internal)
    (tmp_path / "flow_topology").write_text("internal")   # neutral fact, not an engine_param
    out = R.configure_mesh(tmp_path, geometry_file="input.stl", surface=_surface(tmp_path), strategy={},
                           wall_patch="wall",
                           contract_patches=[{"name": "wall", "type": "wall"}],
                           args={}, cell_budget=1_000_000)
    assert out["success"] and seen.get("internal")


def test_configure_mesh_defaults_to_external_when_nothing_declared(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_configure_internal",
                        lambda ws, **kw: pytest.fail("must not take the internal path"))
    monkeypatch.setattr(R, "prepare_surface",
                        lambda ws, **kw: {"surface_file": "geom.stl",
                                          "body_bbox": [[0, 0, 0], [1, 1, 1]]})
    monkeypatch.setattr(R, "render_cfmesh_case", lambda ws, **kw: {"max_cell_size": 0.1})
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda p: {"bbox_min": [0, 0, 0], "bbox_max": [1, 1, 1], "L": 1.0})
    out = R.configure_mesh(tmp_path, geometry_file="input.stl", surface=_surface(tmp_path), strategy={},
                           wall_patch="body", contract_patches=[], args={},
                           cell_budget=1_000_000)
    assert out["topology"] == "external"


def test_internal_without_the_cad_solid_fails_with_an_actionable_error(tmp_path):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    (tmp_path / "flow_topology").write_text("internal")   # neutral fact, not an engine_param
    out = R.configure_mesh(tmp_path, geometry_file="input.stl", surface=_surface(tmp_path), strategy={},
                           wall_patch="wall", contract_patches=[], args={},
                           cell_budget=1_000_000)
    assert out["success"] is False
    assert "geometry.step" in out["error"]
    assert "external topology" in out["next"]


# the internal surface: named solids, and NO bounding box

def _tri(z):
    return [((0, 0, z), (1, 0, z), (0, 1, z))]


def test_prepare_surface_internal_writes_named_solids_and_no_box(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws, fa, br: "geom.stl")
    boxed = []
    monkeypatch.setattr(R, "_box_triangles", lambda *a: boxed.append(a) or [])

    srcs = {}
    for i, patch in enumerate(("wall", "inlet", "outlet")):
        p = tmp_path / f"{patch}.stl"
        with p.open("w") as fh:
            R._write_solid(fh, patch, _tri(float(i)))
        srcs[patch] = str(p)

    out = R.prepare_surface_internal(tmp_path, surfaces_src=srcs)

    assert out["patch_names"] == ["wall", "inlet", "outlet"]
    text = (tmp_path / "geom.stl").read_text()
    for patch in ("wall", "inlet", "outlet"):
        assert f"solid {patch}" in text
    # THE point of the internal path: no far-field box is bolted on
    assert not boxed, "internal topology must not construct a bounding box"
    # ...and no requested far-field means no geom_box.json for the extent gate to measure
    assert not (tmp_path / "geom_box.json").exists()


def test_prepare_surface_internal_bbox_spans_all_patches(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws, fa, br: "geom.stl")
    srcs = {}
    for i, patch in enumerate(("wall", "inlet")):
        p = tmp_path / f"{patch}.stl"
        with p.open("w") as fh:
            R._write_solid(fh, patch, _tri(float(i * 5)))
        srcs[patch] = str(p)
    bb_min, bb_max = R.prepare_surface_internal(tmp_path, surfaces_src=srcs)["body_bbox"]
    assert bb_min[2] == 0.0 and bb_max[2] == 5.0


def test_prepare_surface_internal_rejects_empty_input(tmp_path):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    with pytest.raises(ValueError, match="named boundary surfaces"):
        R.prepare_surface_internal(tmp_path, surfaces_src={})


def test_prepare_surface_internal_rejects_a_degenerate_patch(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws, fa, br: "geom.stl")
    degenerate = tmp_path / "inlet.stl"
    with degenerate.open("w") as fh:                 # all three vertices coincident
        R._write_solid(fh, "inlet", [((0, 0, 0), (0, 0, 0), (0, 0, 0))])
    with pytest.raises(ValueError, match="zero triangles"):
        R.prepare_surface_internal(tmp_path, surfaces_src={"inlet": str(degenerate)})


# staging: the CAD solid is kept for the internal path

def test_cfmesh_tessellate_stages_the_cad_solid(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    src = tmp_path / "up.step"
    src.write_text("ISO-10303-21;")
    monkeypatch.setattr(R, "_cad_tessellate_to_stl",
                        lambda g, o, *, prepared=None: Path(o))
    ws = tmp_path / "ws"
    ws.mkdir()
    R.tessellate_to_stl(src, ws / "input.stl")
    assert (ws / "geometry.step").exists(), "internal topology needs the B-rep staged"


def test_cfmesh_tessellate_of_an_stl_stages_no_solid(tmp_path, monkeypatch):
    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    src = tmp_path / "up.stl"
    src.write_text("solid x\nendsolid x\n")
    monkeypatch.setattr(R, "_cad_tessellate_to_stl",
                        lambda g, o, *, prepared=None: Path(o))
    ws = tmp_path / "ws"
    ws.mkdir()
    R.tessellate_to_stl(src, ws / "input.stl")
    assert not (ws / "geometry.step").exists()
