"""SHMESH-101: a mapped strip one element across aims its elements inside the user's aspect target."""
import math

import pytest

gmsh = pytest.importorskip("gmsh")

from meshpipeline.engines import shell_requirements as SR  # noqa: E402
from meshpipeline.engines.gmsh import shell_adapter as SA  # noqa: E402


@pytest.fixture(autouse=True)
def _shmesh_101_on(monkeypatch):
    # (SHMESH-101 is off by default since round 13; these tests cover what it does when on)
    monkeypatch.setattr(SA, "STRIP_AIM", True)
    monkeypatch.setattr(SA, "STRIP_OPEN", True)


def _strip(width: float, sizing: dict, monkeypatch=None, **patch) -> tuple[dict, dict]:
    if monkeypatch is not None:
        for k, v in patch.items():
            monkeypatch.setattr(SA, k, v)
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("strip")
        s = gmsh.model.occ.addRectangle(0, 0, 0, 100.0, width)
        gmsh.model.occ.synchronize()
        rows = [{"size_mm": 10.0, "rule": "SHMESH-2", "wall_mm": 2.0}]
        aim: dict = {}
        out = SA._map_strips(gmsh, {s: 0}, rows, 1.0, sizing, aim_out=aim)
        return dict(out), aim
    finally:
        gmsh.finalize()


def test_a_narrow_strip_is_meshed_along_inside_the_aspect_target():
    # a 100 x 3 mm strip under a 10 mm size, aspect target 4 (stretch 3.6): one quad across, 10.8 mm along before
    sizing = {"stretch": SR.ASPECT_SHARE * 4.0, "low_mm": 5.0, "high_mm": 15.0}
    out, aim = _strip(3.0, sizing)
    assert len(out) == 1
    along = next(iter(out.values()))[1]
    assert along <= SA.STRIP_AIM_SHARE * 4.0 * 3.0 + 1e-6 and along >= 5.0
    assert aim["strips"] == 1 and aim["added"] > 0 and aim["finest_mm"] == pytest.approx(7.2)


def test_the_users_shortest_edge_wins_over_the_aim():
    sizing = {"stretch": SR.ASPECT_SHARE * 4.0, "low_mm": 9.0, "high_mm": 15.0}
    out, _aim = _strip(3.0, sizing)
    along = next(iter(out.values()))[1]
    assert along >= 9.0 - 1e-6


def test_the_elements_added_are_capped_and_the_rest_keep_the_plan(monkeypatch):
    sizing = {"stretch": SR.ASPECT_SHARE * 4.0, "low_mm": 5.0, "high_mm": 15.0}
    out, aim = _strip(3.0, sizing, monkeypatch, STRIP_AIM_ADD_MAX=0)
    along = next(iter(out.values()))[1]
    assert aim.get("over") == 1 and not aim.get("strips")
    assert along == pytest.approx(100.0 / math.ceil(100.0 / (SR.ASPECT_SHARE * 4.0 * 3.0)))


def test_shmesh_101_is_off_by_default() -> None:
    # round 13: off (candidate) - 3 parts better and 2 worse of 173 for a whole second mesh on 42 parts
    # (read from the source: the autouse fixture has turned it on in this module)
    from pathlib import Path

    src = Path(SA.__file__).read_text().splitlines()
    assert "STRIP_AIM = False" in src and "STRIP_OPEN = False" in src
