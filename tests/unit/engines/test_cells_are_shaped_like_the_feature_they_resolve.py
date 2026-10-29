"""A channel that is thin in one axis and prismatic in another should not be meshed with cubes.

WHY THIS EXISTS. The first customer's microchannels are 0.098 mm wide, run tens of millimetres in
plane, and are 1.9 mm tall with a constant cross-section through that height. An octree cell is a
cube, so resolving the WIDTH to five cells also buys about ninety-seven cells through a height
where a dozen would do, and the whole field is priced at the cost of the axis that needed nothing.
At 2.23 cells across the delivered mesh was already 102,630,992 cells.

cfMesh answers this with `anisotropicSources`, which its own ship5415Octree tutorial uses. The
convention was MEASURED rather than assumed, on a 10 mm box at 1 mm cells (2026-10-29):

    plain          896 cells, max cell volume 1.256e-9
    scaleY 4.0     320 cells, max cell volume 5.142e-9    4.09x taller, 2.8x fewer cells
    scaleY 0.25  3,776 cells, max cell volume 4.344e-10   4x shorter, 4.2x more cells

so scale > 1 stretches along the axis. Guessing the sign the other way quadruples a mesh that was
meant to be quartered, which is why it is written down here beside the code that relies on it.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import plan as PL
from meshpipeline.engines.cfmesh_conjugate.conjugate import ConjugateError, render_meshdict

BLOCK, FLUID = "constant/triSurface/block.stl", "constant/triSurface/fluid.stl"

#: The smallest plan `read_plan` accepts. Spelled out so the tests below fail on the stretch zone
#: they are about and not on a field they forgot - the reader refuses a plan missing
#: expect_fluid_volume_m3 or narrowest_m, and it is right to.
_PLAN_FLOOR = {"background_cell_m": 3.5e-4, "levels": 1,
               "block_lo": [0, 0, 0], "block_hi": [1, 1, 1],
               "expect_fluid_volume_m3": 5.4e-5, "narrowest_m": 9.8e-5,
               "expect_fluid_pieces": 2, "volume_tol": 0.05}


def _dict(**kw):
    return render_meshdict(block_file=BLOCK, fluid_file=FLUID,
                           background_cell_m=3.5e-4, levels=1, **kw)


def _zone(**kw):
    z = {"name": "channels", "lo": (0.0, 0.0216, 0.016), "hi": (0.053, 0.0235, 0.068),
         "scale": (1.0, 6.0, 1.0)}
    z.update(kw)
    return z


def test_a_part_with_no_stretch_writes_no_block():
    """Most parts are not prismatic along any axis. They must get the dictionary they always got."""
    out = _dict()
    assert "anisotropicSources" not in out


def test_the_stretch_reaches_the_dictionary_the_mesher_reads():
    out = _dict(stretch_zones=[_zone()])
    assert "anisotropicSources" in out
    assert "scaleY 6;" in out
    assert "scaleX 1;" in out and "scaleZ 1;" in out
    assert "type box;" in out


def test_the_box_is_written_as_centre_and_lengths_not_corners():
    """cfMesh takes a centre and three lengths. Handing it corners writes a box somewhere else."""
    out = _dict(stretch_zones=[_zone(lo=(0.0, 0.0, 0.0), hi=(0.04, 0.002, 0.05))])
    assert "centre (0.02 0.001 0.025);" in out
    assert "lengthX 0.04;" in out and "lengthY 0.002;" in out and "lengthZ 0.05;" in out


def test_several_zones_share_one_block():
    """cfMesh reads one anisotropicSources dictionary; a second one replaces the first."""
    out = _dict(stretch_zones=[_zone(name="a"), _zone(name="b", scale=(1.0, 1.0, 3.0))])
    assert out.count("anisotropicSources") == 1
    assert "    a\n" in out and "    b\n" in out
    assert out.count("type box;") == 2


def test_a_stretch_and_a_refinement_are_different_blocks():
    """A stretch changes the SHAPE of cells; a refinement changes where the small ones go.
    Writing one as the other is the difference between a mesh and a different mesh."""
    out = _dict(stretch_zones=[_zone()],
                fine_zones=[{"name": "slots", "lo": (0.0, 0.0, 0.0),
                             "hi": (0.01, 0.01, 0.01), "cell_m": 2e-5}])
    assert "anisotropicSources" in out and "objectRefinements" in out
    assert out.index("objectRefinements") < out.index("anisotropicSources")
    assert "cellSize 2e-05;" in out and "scaleY 6;" in out


@pytest.mark.parametrize("bad, why", [
    ({"scale": (1.0, 0.0, 1.0)}, "a zero scale"),
    ({"scale": (1.0, -2.0, 1.0)}, "a negative scale"),
    ({"hi": (0.0, 0.0235, 0.068)}, "no extent in x"),
])
def test_a_zone_that_cannot_mean_anything_is_refused(bad, why):
    with pytest.raises(ConjugateError):
        _dict(stretch_zones=[_zone(**bad)])


def test_a_zone_that_scales_nothing_is_refused():
    """It would read to the next person as a deliberate choice and do nothing at all."""
    with pytest.raises(ConjugateError, match="does nothing"):
        _dict(stretch_zones=[_zone(scale=(1.0, 1.0, 1.0))])


# -- and that it survives the plan on disk --------------------------------------------------

def test_the_plan_carries_the_stretch(tmp_path):
    import json

    body = {**_PLAN_FLOOR,
            "stretch_zones": [{"name": "channels", "lo": [0, 0, 0], "hi": [0.05, 0.002, 0.05],
                               "scale": [1, 6, 1]}]}
    (tmp_path / PL.PLAN_NAME).write_text(json.dumps(body))
    got = PL.read_plan(tmp_path)
    assert len(got.stretch_zones) == 1
    z = got.stretch_zones[0]
    assert z["name"] == "channels" and z["scale"] == (1.0, 6.0, 1.0)
    assert z["lo"] == (0.0, 0.0, 0.0) and z["hi"] == (0.05, 0.002, 0.05)


@pytest.mark.parametrize("zone, match", [
    ({"lo": [0, 0, 0], "hi": [1, 1, 1]}, "needs lo, hi and scale"),
    ({"lo": [0, 0], "hi": [1, 1, 1], "scale": [1, 2, 1]}, "three axes"),
    ({"lo": [0, 0, 0], "hi": [0, 1, 1], "scale": [1, 2, 1]}, "no extent"),
    ({"lo": [0, 0, 0], "hi": [1, 1, 1], "scale": [1, 0, 1]}, "non-positive scale"),
    ({"lo": [0, 0, 0], "hi": [1, 1, 1], "scale": [1, 1, 1]}, "does nothing"),
])
def test_the_plan_refuses_a_zone_it_cannot_act_on(tmp_path, zone, match):
    import json

    body = {**_PLAN_FLOOR, "stretch_zones": [zone]}
    (tmp_path / PL.PLAN_NAME).write_text(json.dumps(body))
    with pytest.raises(ValueError, match=match):
        PL.read_plan(tmp_path)


def test_the_runner_hands_the_stretch_to_the_mesher():
    """THE BUG THIS PACKAGE HAS ALREADY HAD ONCE. `render_meshdict` took fine_zones from the day
    it was written and `build` never passed them, so a 78-minute run on a 251 GB machine rebuilt
    an identical mesh and exited 0. A renderer tested through its own front door cannot catch an
    argument the caller never supplies, so the CALLER is what this checks."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import native
    from meshpipeline.engines.cfmesh_conjugate.conjugate import build

    assert "stretch_zones" in inspect.signature(build).parameters, (
        "build has to accept it or the plan cannot reach the dictionary")
    src = inspect.getsource(native._run_conjugate_local)
    assert "stretch_zones=" in src, (
        "the local runner must pass plan.stretch_zones to build; this is exactly the gap that "
        "cost a 99,172,391-cell run")
    body = inspect.getsource(build)
    assert "stretch_zones=stretch_zones" in body, (
        "build must forward it to render_meshdict")
