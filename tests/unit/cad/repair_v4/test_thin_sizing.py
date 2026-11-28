"""thin_sizing: the check panel's thickness-aware local sizing (owner decision 2026-11-26). Pure parts only: the plan
(wall -> size, floor, cap), the gmsh field wiring against a recording fake, and the cache key."""

from __future__ import annotations

import math

import pytest

from meshpipeline.cad.repair_v4.verdict import thin_sizing as TS

DIAG = 200.0


def _wall(t: float, area: float = 100.0, pair: tuple[int, int] = (1, 2), n: int = 100) -> dict:
    side = {"probe": [0.0, 0.0, 0.0], "box": [0, 0, 0, 1, 1, 1], "extent_mm": 10.0, "area_mm2": area, "samples": n,
            "d_mm": [t] * n}
    return {"pair": list(pair), "t_mm": t, "sides": [side, dict(side)], "points": [[0.0, 0.0, 0.0]]}


def test_a_thin_wall_gets_the_product_wall_size_and_a_thicker_one_a_larger_size() -> None:
    plan = TS.plan([_wall(2.0, area=1.0, pair=(3, 4)), _wall(0.06, area=1.0)], DIAG)
    sizes = {w["t_mm"]: w["h_mm"] for w in plan["walls"]}
    assert sizes[0.06] == pytest.approx(0.06 / TS.WALL_ELEMENTS)
    assert sizes[2.0] == pytest.approx(2.0 / TS.WALL_ELEMENTS)
    assert sizes[0.06] < sizes[2.0] < TS.SCAN_OF_DIAG * DIAG
    assert [w["t_mm"] for w in plan["walls"]] == [0.06, 2.0]          # thinnest first
    assert not plan["capped"]


def test_no_size_goes_below_the_floor() -> None:
    plan = TS.plan([_wall(1e-5, area=1e-4)], DIAG)
    assert plan["walls"][0]["h_mm"] == pytest.approx(TS.FLOOR_OF_DIAG * DIAG)
    assert plan["floor_mm"] == pytest.approx(TS.FLOOR_OF_DIAG * DIAG)


def test_over_the_cap_sizes_scale_by_one_factor_but_never_past_the_validity_minimum() -> None:
    # 1000 mm^2 per side at t 0.2: 2 x 2.3 x 1000 / 0.1^2 = 460k triangles at the rule's size; 115k at h = t
    plan = TS.plan([_wall(0.2, area=1000.0)], DIAG)
    w = plan["walls"][0]
    assert plan["capped"] and plan["scale"] > 1.0
    assert 0.2 / TS.WALL_ELEMENTS < w["h_mm"] <= 0.2 / TS.KEEP_THROUGH + 1e-12
    assert plan["final_triangles"] <= TS.MAX_REFINED_TRIANGLES < plan["estimate_triangles"]


def test_still_over_at_the_validity_minimum_the_thickest_walls_are_dropped() -> None:
    walls = [_wall(0.05, area=400.0, pair=(1, 2)), _wall(0.1, area=4000.0, pair=(3, 4)),
             _wall(0.3, area=40000.0, pair=(5, 6))]
    plan = TS.plan(walls, DIAG)
    kept = [w["t_mm"] for w in plan["walls"]]
    assert kept and kept[0] == 0.05 and 0.3 not in kept
    assert plan["dropped"] >= 1 and plan["capped"]
    assert all(w["h_mm"] <= w["t_mm"] / TS.KEEP_THROUGH + 1e-12 for w in plan["walls"])


def test_at_most_max_walls_are_sized_thinnest_first() -> None:
    walls = [_wall(0.01 * (i + 1), area=1e-3, pair=(i, i + 1000)) for i in range(TS.MAX_WALLS + 10)]
    plan = TS.plan(walls, DIAG)
    assert len(plan["walls"]) == TS.MAX_WALLS
    assert max(w["t_mm"] for w in plan["walls"]) == pytest.approx(0.01 * TS.MAX_WALLS)
    assert plan["dropped"] == 10


def test_the_law_is_h_within_the_reach_and_grows_at_the_product_rate() -> None:
    assert TS.size_at(0.05, 0.1, 0.05, 20.0) == 0.05
    assert TS.size_at(1.1, 0.1, 0.05, 20.0) == pytest.approx(0.05 + (TS.GROWTH - 1.0) * 1.0)
    assert TS.size_at(1e6, 0.1, 0.05, 20.0) == 20.0


class _FakeGmsh:
    """Records the fields gmsh_fields adds. Surface 1 is the probe's face at z = 0, surface 2 the wall's other face at
    z = t, surface 3 a face elsewhere."""

    def __init__(self, t: float) -> None:
        self.t = t
        self.fields: dict[int, dict] = {}
        self.background: int | None = None
        outer = self

        class Field:
            def add(self, kind: str) -> int:
                tag = len(outer.fields) + 1
                outer.fields[tag] = {"kind": kind}
                return tag

            def setNumber(self, tag: int, key: str, value: float) -> None:  # noqa: N802 - gmsh's own name
                outer.fields[tag][key] = value

            def setNumbers(self, tag: int, key: str, values: list) -> None:  # noqa: N802
                outer.fields[tag][key] = list(values)

            def setAsBackgroundMesh(self, tag: int) -> None:  # noqa: N802
                outer.background = tag

        class Model:
            class mesh:  # noqa: N801
                field = Field()

            @staticmethod
            def getClosestPoint(dim: int, tag: int, xyz: list) -> tuple[list, list]:  # noqa: N802
                z = {1: 0.0, 2: outer.t, 3: 50.0}[tag]
                return [xyz[0], xyz[1], z], [0.0, 0.0]

            @staticmethod
            def getEntities(dim: int) -> list[tuple[int, int]]:  # noqa: N802
                return [(dim, 1), (dim, 2), (dim, 3)]

        self.model = Model()


def _sizing(t: float, h: float) -> dict:
    sides = [{"probe": [0.5, 0.5, 0.0], "box": [0, 0, 0, 1, 1, 0], "extent_mm": 1.4, "area_mm2": 1.0, "samples": 1,
              "d_mm": [t]},
             {"probe": [0.5, 0.5, t], "box": [0, 0, t, 1, 1, t], "extent_mm": 1.4, "area_mm2": 1.0, "samples": 1,
              "d_mm": [t]}]
    return {"status": "measured", "walls": [{"pair": [1, 2], "t_mm": t, "h_mm": h, "sides": sides}]}


def test_gmsh_fields_size_both_faces_of_the_wall_and_set_the_background() -> None:
    t = 0.05
    gmsh = _FakeGmsh(t)
    boxes = {1: (0, 0, 0, 1, 1, 0), 2: (0, 0, t, 1, 1, t), 3: (0, 0, 50, 1, 1, 50)}
    got = TS.gmsh_fields(gmsh, _sizing(t, t / 2), 1.0, 20.0, boxes)
    assert got["matched"] == 1 and got["fields"] == 1
    kinds = [f["kind"] for f in gmsh.fields.values()]
    assert kinds.count("Distance") == 2 and kinds.count("Threshold") == 2 and "Max" in kinds
    surfaces = sorted(f["SurfacesList"][0] for f in gmsh.fields.values() if f["kind"] == "Distance")
    assert surfaces == [1, 2]                                          # the wall's two faces, never surface 3
    for th in (f for f in gmsh.fields.values() if f["kind"] == "Threshold"):
        assert th["SizeMin"] == pytest.approx(t / 2) and th["SizeMax"] == 20.0
        assert th["DistMin"] >= t
    assert gmsh.fields[gmsh.background]["kind"] == "Min"               # held in the volume too: the product's law


def test_surface_only_wraps_the_walls_in_a_restrict_to_curves_and_surfaces(monkeypatch) -> None:
    t = 0.05
    monkeypatch.setattr(TS, "SURFACE_ONLY", True)
    gmsh = _FakeGmsh(t)
    TS.gmsh_fields(gmsh, _sizing(t, t / 2), 1.0, 20.0, {1: (0, 0, 0, 1, 1, 0), 2: (0, 0, t, 1, 1, t)})
    restrict = gmsh.fields[gmsh.background]
    assert restrict["kind"] == "Restrict" and gmsh.fields[restrict["InField"]]["kind"] == "Min"
    assert restrict["SurfacesList"] == [1, 2, 3] and "VolumesList" not in restrict


def test_gmsh_fields_scale_to_the_members_units_and_skip_a_wall_no_finer_than_the_member() -> None:
    t = 0.05
    gmsh = _FakeGmsh(t * 1e-3)
    boxes = {1: (0, 0, 0, 1e-3, 1e-3, 0), 2: (0, 0, t * 1e-3, 1e-3, 1e-3, t * 1e-3)}
    TS.gmsh_fields(gmsh, _sizing(t, t / 2), 1e-3, 0.02, boxes)          # gmsh_prod meshes in metres
    th = next(f for f in gmsh.fields.values() if f["kind"] == "Threshold")
    assert th["SizeMin"] == pytest.approx(t / 2 * 1e-3)
    coarse = _FakeGmsh(t)
    assert TS.gmsh_fields(coarse, _sizing(t, 30.0), 1.0, 20.0, boxes)["fields"] == 0
    assert coarse.background is None


def test_an_unreadable_file_is_unmeasured_not_an_error(tmp_path) -> None:
    plan = TS.plan_for(str(tmp_path / "missing.step"))
    assert plan["status"].startswith("unmeasured") and plan["walls"] == []


def test_every_member_keys_its_cache_on_the_sizing_and_a_change_moves_the_key(monkeypatch) -> None:
    from meshpipeline.cad.repair_v4.verdict import cache, gmsh_members, mesh_child, netgen_member

    members = [gmsh_members.GmshProd(), gmsh_members.GmshDefault(), gmsh_members.GmshAlt(), netgen_member.NetgenOCC()]
    for m in members:
        assert m.config["local_sizing"] == TS.config()
    before = cache.panel_config_hash(members, 300.0, {"gmsh": "4.15.2"})
    monkeypatch.setattr(TS, "FLOOR_OF_DIAG", TS.FLOOR_OF_DIAG / 2)
    after = cache.panel_config_hash(members, 300.0, {"gmsh": "4.15.2"})
    assert before != after
    assert cache.VERDICT_CODE_VERSION == "crs-v4.verdict.10"
    _ = mesh_child


@pytest.mark.parametrize("profile", ["prod", "default", "alt"])
def test_each_gmsh_profile_keeps_its_algorithms_and_gets_the_floor_only_where_walls_are_sized(profile: str) -> None:
    from meshpipeline.cad.repair_v4.verdict import mesh_child
    from meshpipeline.engines import fea_mesh_config

    diag = 0.2 if profile == "prod" else 200.0
    _scale, options = mesh_child._profile(profile, diag)
    assert options["Mesh.MeshSizeFromCurvature"] == TS.CURVATURE_POINTS
    assert mesh_child.algorithms(profile) == {"prod": (6, 10), "default": (6, 1), "alt": (1, 4)}[profile]
    if profile == "alt":
        assert options["Mesh.MeshSizeMax"] == pytest.approx(diag / mesh_child.ALT_SIZE_DIVISOR)
    if profile != "prod":   # a file with no wall meshes exactly as before: no floor, gmsh's own precision
        assert "Mesh.MeshSizeMin" not in options and "Mesh.LcIntegrationPrecision" not in options
    walls = mesh_child.wall_options(diag)
    assert walls["Mesh.MeshSizeMin"] == pytest.approx(TS.FLOOR_OF_DIAG * diag)
    assert walls["Mesh.LcIntegrationPrecision"] == fea_mesh_config.LC_INTEGRATION_PRECISION
    assert math.isfinite(walls["Mesh.LcIntegrationPrecision"])


def test_the_check_and_the_product_mesher_size_from_curvature_alike() -> None:
    """Owner 2026-11-27: the clean check's meshers and the product mesher stay matched."""
    from meshpipeline.cad.repair_v4.verdict import mesh_child
    from meshpipeline.engines import fea_mesh_config

    assert TS.CURVATURE_POINTS == fea_mesh_config.CURVATURE_POINTS
    assert fea_mesh_config.fixed_options()["Mesh.MeshSizeFromCurvature"] == fea_mesh_config.CURVATURE_POINTS
    for profile in ("prod", "default", "alt"):
        _scale, options = mesh_child._profile(profile, 1.0)
        assert options["Mesh.MeshSizeFromCurvature"] == fea_mesh_config.CURVATURE_POINTS
