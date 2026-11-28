"""SHMESH-101 (amended 91): a strip narrower than the shortest edge whose closing was refused is meshed open, its
elements along it inside the aspect target; a typed shortest edge holds, our own default may give way."""
import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines import shell_requirements as SR  # noqa: E402
from meshpipeline.engines.gmsh import shell_adapter as SA  # noqa: E402


@pytest.fixture(autouse=True)
def _shmesh_101_on(monkeypatch):
    # (SHMESH-101 is off by default since round 13; these tests cover what it does when on)
    monkeypatch.setattr(SA, "STRIP_AIM", True)
    monkeypatch.setattr(SA, "STRIP_OPEN", True)


def _thin(width: float, sizing: dict, *, reopen: bool, rule: str = "SHMESH-2") -> tuple[dict, dict, dict]:
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("thin")
        s = gmsh.model.occ.addRectangle(0, 0, 0, 100.0, width)
        gmsh.model.occ.synchronize()
        rows = [{"size_mm": 10.0, "rule": rule, "wall_mm": 2.0}]
        aim: dict = {}
        thin: dict = {}
        out = SA._map_strips(gmsh, {s: 0}, rows, 1.0, sizing, aim_out=aim, narrow_out=thin,
                             keep_open=frozenset({s}) if reopen else frozenset())
        return dict(out), aim, thin
    finally:
        gmsh.finalize()


def _sizing(low: float, typed: bool) -> dict:
    return {"stretch": SR.ASPECT_SHARE * 4.0, "low_mm": low, "high_mm": 15.0, "global_mm": 10.0, "low_typed": typed}


def test_a_thin_strip_planned_to_close_keeps_the_neighbours_size_as_before():
    out, aim, thin = _thin(3.0, _sizing(5.0, True), reopen=False)
    assert len(thin) == 1 and not aim
    assert next(iter(out.values()))[1] == pytest.approx(10.0)


def test_a_strip_left_open_is_meshed_along_inside_the_aspect_target():
    out, aim, thin = _thin(3.0, _sizing(5.0, True), reopen=True)
    assert not thin                                  # (not to be closed)
    along = next(iter(out.values()))[1]
    assert 5.0 - 1e-6 <= along <= SA.STRIP_AIM_SHARE * 4.0 * 3.0 + 1e-6
    assert aim["open"] == 1 and aim["added"] > 0 and not aim.get("typed_blocks")


def test_a_typed_shortest_edge_holds_and_the_blocked_elements_are_told():
    out, aim, _thin_ = _thin(1.0, _sizing(5.0, True), reopen=True)
    along = next(iter(out.values()))[1]
    assert along >= 5.0 - 1e-6                       # (the user's edge wins)
    assert aim["typed_blocks"] == 20 and aim["typed_blocks_narrowest_mm"] == pytest.approx(1.0)
    assert aim["typed_blocks_target"] == pytest.approx(4.0) and not aim.get("under_default")


def test_our_own_default_shortest_edge_gives_way_down_to_the_floor():
    out, aim, _thin_ = _thin(1.0, _sizing(5.0, False), reopen=True)
    along = next(iter(out.values()))[1]
    assert along == pytest.approx(100.0 / 42)        # 0.6 x 4 x 1 mm = 2.4 mm aim, over the 2 mm floor
    assert along < 5.0 and along >= SA.STRIP_FLOOR_SHARE * 10.0
    assert aim["under_default"] == 1 and aim["floor_mm"] == pytest.approx(2.0) and not aim.get("floor_blocks")


def test_a_face_sized_in_a_named_place_is_not_reopened():
    out, aim, thin = _thin(3.0, _sizing(5.0, True), reopen=True, rule="SHMESH-52")
    assert len(thin) == 1 and not aim.get("open")


def test_the_shortest_edge_is_marked_typed_only_when_the_user_typed_it():
    def rows_():
        return [{"size_mm": 10.0, "_rule_size": 10.0, "_rule_curve": None, "wall_mm": 2.0, "rule": "SHMESH-2"}]

    got = SR.size_plan(rows_(), {"sizes": {"global_mm": 10.0}}, [100.0], planned_max=1e9)
    assert got is not None and got["low_typed"] is False and got["low_mm"] == pytest.approx(5.0)
    got = SR.size_plan(rows_(), {"sizes": {"global_mm": 10.0, "min_edge_mm": 4.0}}, [100.0], planned_max=1e9)
    assert got is not None and got["low_typed"] is True
    assert SA._strip_edge_floor({"low_mm": 5.0, "global_mm": 10.0, "low_typed": False}) == (2.0, False)
    assert SA._strip_edge_floor({"low_mm": 4.0, "global_mm": 10.0, "low_typed": True}) == (4.0, True)
