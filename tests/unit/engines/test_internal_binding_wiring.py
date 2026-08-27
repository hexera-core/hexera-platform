# Responsibility: Pin the snappy internal path's binding seam: declared patches re-key the
# tessellation before any surface is prepared, the wall key threads through the renderer, the
# bore comes from the DECLARED inlet (never the engine's largest-opening guess), and blind
# plugs fold into the wall surface.
from __future__ import annotations

import math

import pytest

from meshpipeline.engines.snappy.drivers import _bind_declared_ports, _bore_area_m2


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


class TestTheBindingSeam:
    def test_declared_patches_rekey_the_tessellation(self):
        t, wall_key, note = _bind_declared_ports(WYE_T, WYE_PATCHES)
        assert wall_key == "pipe_wall"
        assert set(t["stls"]) == {"pipe_wall", "inlet_1", "inlet_2", "outlet"}
        assert set(t["openings"]) == {"inlet_1", "inlet_2", "outlet"}
        assert "pipe_wall" in note and "outlet" in note

    def test_no_declaration_keeps_engine_names_and_the_guess_note(self):
        t, wall_key, note = _bind_declared_ports(WYE_T, [])
        assert t is WYE_T and wall_key == "wall" and note == ""

    def test_a_bind_failure_propagates_as_binderror(self):
        from meshpipeline.engines.port_binding import BindError
        bad = [{"name": "pipe_wall", "type": "wall"},
               {"name": "feed", "type": "inlet", "diameter_mm": 200}]  # matches nothing
        with pytest.raises(BindError):
            _bind_declared_ports(WYE_T, bad)

    def test_the_bore_comes_from_the_declared_inlet_not_the_biggest_hole(self):
        t, _wall, _note = _bind_declared_ports(WYE_T, WYE_PATCHES)
        # engine guessed the 60mm collector as "inlet"; the DECLARED inlets are the 40mm feeds
        assert _bore_area_m2(t) == pytest.approx(circle_area(40))

    def test_without_a_declaration_the_bore_keeps_the_engine_guess(self):
        assert _bore_area_m2(WYE_T) == pytest.approx(circle_area(60))


class TestRingPortBoreSizing:
    # Job 11b50253 follow-up: a thin-walled duct's bound port face is ~5014 mm2 of ring
    # metal around a 497,059 mm2 bore. The resolution yardstick must follow the BORE the
    # evidence row's opening_area_m2 carries - sizing from the metal ring re-runs the
    # mesh ~10x over-refined into the cell budget. Rows without an opening (solid-disc
    # ports) must size by area_m2 exactly as before.
    def _t(self, ports):
        return {"binding": {"wall_name": "duct_wall", "ports": ports,
                            "folded_into_wall": []}}

    def test_a_ring_port_sizes_by_the_opening_its_inner_wire_encloses(self):
        t = self._t([{"name": "inlet", "role": "inlet", "engine_key": "outlet",
                      "centroid": [0.0, 0.0, 0.0],
                      "area_m2": 5014e-6, "opening_area_m2": 0.497059}])
        assert _bore_area_m2(t) == 0.497059

    def test_a_disc_port_without_an_opening_sizes_exactly_as_before(self):
        t = self._t([{"name": "inlet", "role": "inlet", "engine_key": "inlet",
                      "centroid": [0.0, 0.0, 0.0], "area_m2": 0.004}])
        assert _bore_area_m2(t) == 0.004          # the pre-ring behaviour, byte-exact

    def test_the_largest_declared_inlet_still_wins_across_mixed_rows(self):
        # a ring inlet's true bore competes against a disc inlet's face area; outlets
        # stay out of the pool while any inlet exists - both rules unchanged
        t = self._t([
            {"name": "feed_a", "role": "inlet", "engine_key": "inlet",
             "centroid": [0.0, 0.0, 0.0], "area_m2": 5014e-6,
             "opening_area_m2": 0.497059},
            {"name": "feed_b", "role": "inlet", "engine_key": "outlet_1",
             "centroid": [0.1, 0.0, 0.0], "area_m2": 0.004},
            {"name": "drain", "role": "outlet", "engine_key": "outlet_2",
             "centroid": [0.2, 0.0, 0.0], "area_m2": 0.9}])
        assert _bore_area_m2(t) == 0.497059


class TestRendererWallKeyThreading:
    def _render(self, tmp_path, wall_key, names):
        from meshpipeline.engines.snappy.snappy_runner import render_internal_case
        (tmp_path / "system").mkdir(parents=True, exist_ok=True)
        render_internal_case(
            tmp_path, names=names, features={k: f"{v}.eMesh" for k, v in names.items()},
            interior_point=[0.1, 0.0, 0.0], bbox_min=[0, 0, 0], bbox_max=[0.4, 0.1, 0.1],
            base_cell=0.01, surface_level=2, feature_level=3, n_layers=3,
            wall_key=wall_key)
        return (tmp_path / "system" / "snappyHexMeshDict").read_text()

    def test_a_user_named_wall_drives_refinement_and_layers(self, tmp_path):
        names = {"pipe_wall": "pipe_wall", "inlet_1": "inlet_1", "outlet": "outlet"}
        dict_txt = self._render(tmp_path, "pipe_wall", names)
        assert "pipe_wall { level" in dict_txt            # wall refinement entry
        assert "layers { pipe_wall {" in dict_txt          # prism layers on the user's wall
        assert "inlet_1 { level" in dict_txt               # ports refined as patches
        assert "patchInfo { type wall; } }" in dict_txt

    def test_the_default_wall_key_is_byte_compatible(self, tmp_path):
        names = {"wall": "wall", "inlet": "inlet", "outlet": "outlet"}
        dict_txt = self._render(tmp_path, "wall", names)
        assert "wall { level" in dict_txt and "layers { wall {" in dict_txt


class TestPrepMergesFoldedSurfaces:
    def _stl(self, path, n_tris):
        # a minimal ASCII STL with n degenerate-free triangles
        tris = []
        for i in range(n_tris):
            z = float(i)
            tris.append(
                f"facet normal 0 0 1\nouter loop\n"
                f"vertex 0 0 {z}\nvertex 1 0 {z}\nvertex 0 1 {z}\n"
                f"endloop\nendfacet\n")
        path.write_text("solid s\n" + "".join(tris) + "endsolid s\n")
        return path

    def test_a_list_of_sources_concatenates_into_one_patch(self, tmp_path):
        from meshpipeline.engines.snappy.snappy_runner import prepare_surface_internal
        a = self._stl(tmp_path / "wall_a.stl", 4)
        b = self._stl(tmp_path / "plug.stl", 2)
        c = self._stl(tmp_path / "inlet.stl", 3)
        prep = prepare_surface_internal(tmp_path, surfaces_src={
            "pipe_wall": [str(a), str(b)], "inlet": str(c)})
        merged = (tmp_path / "constant" / "triSurface" / "pipe_wall.stl").read_text()
        assert merged.count("facet normal") == 6           # 4 + 2 folded in
        assert prep["names"]["pipe_wall"] == "pipe_wall"
