# Responsibility: Pin cfmesh's side of the binding parity: the declaration survives as a
# workspace fact with its sizes intact (the text contract loses them), and the shared
# bind_intake seam gives cfmesh the same binding snappy gets.
from __future__ import annotations

import json
import math


def circle_area(d_mm: float) -> float:
    return math.pi * (d_mm / 2.0) ** 2 * 1e-6


WYE_T = {
    "stls": {"wall": "/w/wall.stl", "inlet": "/w/inlet.stl",
             "outlet_1": "/w/outlet_1.stl", "outlet_2": "/w/outlet_2.stl"},
    "interior_point": [0.1, 0.0, 0.0],
    "bbox_min": [-0.2, -0.2, -0.05], "bbox_max": [0.3, 0.2, 0.05],
    "openings": {
        "inlet": {"area": circle_area(60), "centroid": [0.25, 0.0, 0.0]},
        "outlet_1": {"area": circle_area(40), "centroid": [-0.173, 0.1, 0.0]},
        "outlet_2": {"area": circle_area(40), "centroid": [-0.173, -0.1, 0.0]},
    },
    "n_wall_faces": 1000,
}

WYE_PATCHES = [
    {"name": "pipe_wall", "type": "wall"},
    {"name": "inlet_1", "type": "inlet", "diameter_mm": 40,
     "interchangeable_with": ["inlet_2"]},
    {"name": "inlet_2", "type": "inlet", "diameter_mm": 40,
     "interchangeable_with": ["inlet_1"]},
    {"name": "outlet", "type": "outlet", "diameter_mm": 60},
]


class TestTheDeclarationSurvivesAsAWorkspaceFact:
    def test_roundtrip_keeps_the_sizes_the_text_contract_loses(self, tmp_path):
        from meshpipeline.engines.workspace_facts import port_declaration
        (tmp_path / "port_declaration.json").write_text(json.dumps(WYE_PATCHES))
        out = port_declaration(tmp_path)
        assert out == WYE_PATCHES
        assert out[1]["diameter_mm"] == 40      # the field contract_patches cannot carry

    def test_absence_means_undeclared_not_an_error(self, tmp_path):
        from meshpipeline.engines.workspace_facts import port_declaration
        assert port_declaration(tmp_path) == []

    def test_corruption_means_undeclared_not_an_error(self, tmp_path):
        from meshpipeline.engines.workspace_facts import port_declaration
        (tmp_path / "port_declaration.json").write_text("{broken")
        assert port_declaration(tmp_path) == []


class TestTheSharedSeamBindsForCfmesh:
    def test_bind_intake_rekeys_exactly_like_the_snappy_path(self):
        from meshpipeline.engines.port_binding import bind_intake
        from meshpipeline.engines.snappy.drivers import _bind_declared_ports
        via_shared = bind_intake(WYE_T, WYE_PATCHES)
        via_snappy = _bind_declared_ports(WYE_T, WYE_PATCHES)
        assert via_shared[0]["stls"].keys() == via_snappy[0]["stls"].keys()
        assert via_shared[1] == via_snappy[1] == "pipe_wall"

    def test_cfmesh_helper_is_the_shared_seam(self):
        from meshpipeline.engines.cfmesh.cfmesh_runner import _bind_intake_shared
        t, wall_key, note = _bind_intake_shared(WYE_T, WYE_PATCHES)
        assert wall_key == "pipe_wall"
        assert set(t["stls"]) == {"pipe_wall", "inlet_1", "inlet_2", "outlet"}
        assert "bound to your declared ports" in note

    def test_no_declaration_is_engine_canonical(self):
        from meshpipeline.engines.cfmesh.cfmesh_runner import _bind_intake_shared
        t, wall_key, note = _bind_intake_shared(WYE_T, [])
        assert t is WYE_T and wall_key == "wall" and note == ""
