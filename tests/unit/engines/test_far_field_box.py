# Responsibility: Verify an engine-built far-field box passes the domain-extent gate that judges it, for any flow axis.
# cfMesh multiplied its margins by the body's LARGEST extent along a fixed +x; the gate measures them
# in the stated reference length, else the body's extent along the flow. A rotor came back with 47.8
# lengths downstream where 8 were asked, and a bluff body with 25.3 (2026-10-04 lab baseline).
from __future__ import annotations

import json

import pytest

from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
from meshpipeline.engines.far_field import far_field_box, margins_from, ruler_of

BODY_MIN, BODY_MAX = [0.0, -1.5, 0.0], [0.5, 1.5, 0.3]      # a disc: thin along x, wide in y


def _manifest(dmin, dmax):
    names = "xyz"
    return {"geometry": {
        "domain_box": {f"{names[i]}{s}": float(v[i]) for s, v in (("min", dmin), ("max", dmax))
                       for i in range(3)},
        "body_box": {f"{names[i]}{s}": float(v[i]) for s, v in (("min", BODY_MIN), ("max", BODY_MAX))
                     for i in range(3)}}}


@pytest.mark.parametrize("axis", ["+x", "-x", "+y", "-y", "+z", "-z", None])
@pytest.mark.parametrize("ref", [None, 0.25])
def test_the_box_is_built_in_the_unit_the_gate_judges(axis, ref):
    requested = {"upstream": 5.0, "downstream": 8.0, "lateral": 5.0, "vertical": 4.0}
    dmin, dmax = far_field_box(BODY_MIN, BODY_MAX, margins_from(None, requested),
                               flow_axis=axis, reference_length_m=ref)
    ruler = ref or ruler_of(BODY_MIN, BODY_MAX, axis, None)
    v = evaluate_domain_extents(requested, ruler, _manifest(dmin, dmax), flow_axis=axis)
    assert v.status == "pass", (axis, ref, v.detail, v.misses)


def test_the_ruler_is_the_streamwise_extent_not_the_largest():
    assert ruler_of(BODY_MIN, BODY_MAX, "+x", None) == pytest.approx(0.5)
    assert ruler_of(BODY_MIN, BODY_MAX, "+y", None) == pytest.approx(3.0)
    assert ruler_of(BODY_MIN, BODY_MAX, "+x", 0.2) == pytest.approx(0.2)
    dmin, dmax = far_field_box(BODY_MIN, BODY_MAX, {"up": 5, "down": 8, "side": 5, "vert": 5})
    assert dmin[0] == pytest.approx(-2.5) and dmax[0] == pytest.approx(0.5 + 4.0)


def test_downstream_follows_the_declared_direction():
    dmin, dmax = far_field_box(BODY_MIN, BODY_MAX, {"up": 1, "down": 3, "side": 0, "vert": 0},
                               flow_axis="-y")
    assert dmax[1] == pytest.approx(1.5 + 1 * 3.0), "upstream is +y for a flow along -y"
    assert dmin[1] == pytest.approx(-1.5 - 3 * 3.0)


def test_the_strategy_overrides_the_request_which_overrides_the_defaults():
    m = margins_from({"up": 2}, {"upstream": 7, "downstream": 9})
    assert m["up"] == 2 and m["down"] == 9 and m["side"] == 10.0 and m["vert"] == 10.0


def test_cfmesh_sizes_its_box_from_the_approved_request(tmp_path, monkeypatch):
    import pyvista as pv
    from tests.native._native_geometry import prepared_surface_for

    from meshpipeline.engines.cfmesh import cfmesh_runner as R
    monkeypatch.setattr(R, "_to_fms", lambda ws, angle, bashrc: "geom.fms")
    body = pv.Cube(center=(0.25, 0.0, 0.15), x_length=0.5, y_length=3.0, z_length=0.3).triangulate()
    body.save(str(tmp_path / "input.stl"))
    (tmp_path / "flow_topology").write_text("external\n")
    (tmp_path / "far_field_request.json").write_text(json.dumps({
        "requested_extents": {"upstream": 5, "downstream": 8, "lateral": 5, "vertical": 4},
        "flow_axis": "+x"}))
    out = R.configure_mesh(tmp_path, geometry_file="input.stl",
                           surface=prepared_surface_for(tmp_path / "input.stl"), strategy={},
                           wall_patch="body",
                           contract_patches=[{"name": "body", "type": "wall"},
                                             {"name": "farfield", "type": "farfield"}],
                           args={}, cell_budget=2_000_000)
    assert out.get("success"), out
    box = json.loads((tmp_path / "geom_box.json").read_text())
    # 5 and 8 streamwise extents (0.5 m) up- and downstream, not 5 and 8 spans (3 m)
    assert box["domain_min"][0] == pytest.approx(-2.5, abs=1e-6)
    assert box["domain_max"][0] == pytest.approx(0.5 + 4.0, abs=1e-6)
    assert box["domain_min"][2] == pytest.approx(0.0 - 4 * 0.5, abs=1e-6)
