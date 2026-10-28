"""The refinement zones reach the dictionary the mesher actually reads.

WHY THIS TEST EXISTS. render_meshdict has taken `fine_zones` since it was written, and
test_declared_port_names and test_cfmesh_conjugate both call it directly and prove it renders the
block correctly. None of that touched the question that mattered: whether the one caller in the
package that writes a meshDict passes the argument. It did not.

Measured, 2026-10-28. A zoned run of a Corintis cold plate on a 251 GB machine returned
99,172,391 cells at a 74.61 GB peak - the same cell count and the same peak, to the byte, as the
un-zoned run of the same part. cfMesh read a dictionary with no objectRefinements block, meshed
uniformly, and exited 0. 78 minutes of a 251 GB machine produced a mesh that already existed.

So these tests go through build() and read the file off the disk, which is the only place the
answer is not a function's own return value.
"""
import json
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import plan as PL

ZONE = {"name": "microchannel", "lo": (0.001, 0.002, 0.003), "hi": (0.004, 0.005, 0.006),
        "cell_m": 4.63e-05}


def _meshdict_from_build(tmp_path, monkeypatch, **kw):
    """Run build() far enough to write its dictionaries, then stop it.

    The guard scan is the first thing after the write, so refusing there returns control with the
    meshDict on disk and without OpenFOAM. The point is to read the file build() wrote.
    """
    monkeypatch.setattr(C, "scan_case_dicts", lambda ws: "stopped after the dictionaries")
    with pytest.raises(C.ConjugateError):
        C.build(tmp_path, background_cell_m=0.0007577, **kw)
    return (Path(tmp_path) / "system" / "meshDict").read_text()


def test_a_zone_build_was_given_lands_in_the_meshdict(tmp_path, monkeypatch):
    out = _meshdict_from_build(tmp_path, monkeypatch, fine_zones=[ZONE])
    assert "objectRefinements" in out, (
        "build() wrote a meshDict with no objectRefinements block, so cfMesh will mesh uniformly "
        "and report success - which is exactly how a 78-minute run rebuilt an existing mesh")
    assert "microchannel" in out
    # the cell size is the zone's own, not the background's
    assert "4.63e-05" in out


def test_without_zones_the_meshdict_is_unchanged(tmp_path, monkeypatch):
    """A part with one scale must not acquire an empty block it did not ask for."""
    out = _meshdict_from_build(tmp_path, monkeypatch)
    assert "objectRefinements" not in out


def test_a_zone_survives_the_plan_written_to_disk(tmp_path):
    """The host writes the plan and the container reads it, so the zone crosses as JSON."""
    plan = PL.ConjugatePlan(
        background_cell_m=0.0007577, levels=4, expect_fluid_volume_m3=1e-6,
        expect_fluid_pieces=2, volume_tol=0.05, narrowest_m=9.8e-05,
        fine_zones=(ZONE,))
    PL.write_plan(tmp_path, plan)
    # it really is JSON on the disk, not an object passed in memory
    assert json.loads((Path(tmp_path) / PL.PLAN_NAME).read_text())["fine_zones"][0]["cell_m"]
    back = PL.read_plan(tmp_path)
    assert len(back.fine_zones) == 1
    got = back.fine_zones[0]
    assert got["name"] == "microchannel"
    assert got["cell_m"] == pytest.approx(4.63e-05)
    assert got["lo"] == (0.001, 0.002, 0.003)


@pytest.mark.parametrize("bad, why", [
    ({"lo": (0, 0, 0), "hi": (1, 1, 1), "cell_m": 0.0}, "cell size"),
    ({"lo": (0, 0, 0), "hi": (1, 1, 1), "cell_m": -1.0}, "cell size"),
    ({"lo": (0, 0, 0), "hi": (0, 1, 1), "cell_m": 0.1}, "extent"),
    ({"lo": (0, 0), "hi": (1, 1), "cell_m": 0.1}, "three axes"),
    ({"lo": (0, 0, 0), "cell_m": 0.1}, "needs lo, hi and cell_m"),
])
def test_a_zone_that_is_not_a_zone_is_refused_at_the_boundary(tmp_path, bad, why):
    """A resolution nobody could have chosen is refused where it crosses, not meshed."""
    plan = PL.ConjugatePlan(background_cell_m=0.0007577, levels=4, expect_fluid_volume_m3=1e-6,
                            expect_fluid_pieces=2, volume_tol=0.05, narrowest_m=9.8e-05)
    PL.write_plan(tmp_path, plan)
    p = Path(tmp_path) / PL.PLAN_NAME
    data = json.loads(p.read_text())
    data["fine_zones"] = [bad]
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=why):
        PL.read_plan(tmp_path)
