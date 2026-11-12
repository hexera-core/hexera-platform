"""What the budget decides about two scales is the shape the mesher can actually read.

WHY THIS EXISTS, AND IT IS THE SAME LESSON TWICE. The renderer took refinement zones from the day
it was written, its unit tests handed them to it directly, and the one caller that writes a meshDict
left the argument off - so a 78-minute run on a 251 GB machine rebuilt an identical 99,172,391-cell
mesh and exited 0. Nothing failed. Each piece was tested through its own front door and the seam
between them was not.

So these tests walk the whole seam with no mesher: budget decides, the driver's shape is built from
that decision, the plan carries it to disk and back, and the renderer turns it into the dictionary
cfMesh reads. Any one of those links changing shape breaks a test here rather than a run.
"""
import json
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import plan as PL

LO = (-0.06157654, 0.02011018, -0.008590401)
HI = (0.1159235, 0.06194618, 0.0924096)
#: a part with the customer plate's broad side and a tenth of its thin area, so a plan exists
SCALES = {
    "split_m": 4.883e-4,
    "broad": {"width_m": 2.1123723e-3, "area_m2": 38813.50e-6},
    "fine": {"width_m": 0.0982e-3, "area_m2": 2171.267e-6},
}


@pytest.fixture
def two():
    plan, said = B.plan_two_scales(block_lo=LO, block_hi=HI, scales=SCALES,
                                   limit=700_000_000, asked_across=12)
    assert plan is not None, f"the fixture part has to be plannable: {said}"
    return plan


def _fine_surface(two):
    """Exactly what driver.drive builds from a two-scale plan. Kept in step with it by
    test_the_driver_builds_this_shape below."""
    return {"name": "microchannels", "file": PL.FLUID_FINE_STL,
            "levels": int(two["fine_levels"]), "thickness_m": 4.0 * float(two["fine_cell_m"])}


def test_the_budget_decision_renders_into_the_dictionary(two):
    """The seam that was missed last time: budget's numbers, the renderer's output, no mesher."""
    out = C.render_meshdict(
        block_file=PL.BLOCK_STL, fluid_file=PL.FLUID_STL,
        background_cell_m=two["background_m"], levels=two["broad_levels"],
        fine_surfaces=[_fine_surface(two)])
    assert out.count("surfaceMeshRefinement") == 1, "one block, two entries inside it"
    assert "coolant" in out and "microchannels" in out
    assert PL.FLUID_FINE_STL in out, "the thin surface is not named in the dictionary"
    assert f"additionalRefinementLevels {two['fine_levels']}" in out
    assert f"additionalRefinementLevels {two['broad_levels']}" in out
    assert out.count("{") == out.count("}")


def test_the_thin_surface_is_finer_than_the_coolant_surface(two):
    """A fine surface no finer than the coolant itself buys nothing, and the renderer refuses it
    rather than writing a dictionary that reads as a deliberate choice to a later reader."""
    assert two["fine_levels"] > two["broad_levels"]
    with pytest.raises(C.ConjugateError, match="has to be finer"):
        C.render_meshdict(block_file=PL.BLOCK_STL, fluid_file=PL.FLUID_STL,
                          background_cell_m=two["background_m"], levels=two["broad_levels"],
                          fine_surfaces=[{**_fine_surface(two),
                                          "levels": two["broad_levels"]}])


def test_a_fine_surface_with_no_file_is_refused(two):
    with pytest.raises(C.ConjugateError, match="no surface file"):
        C.render_meshdict(block_file=PL.BLOCK_STL, fluid_file=PL.FLUID_STL,
                          background_cell_m=two["background_m"], levels=two["broad_levels"],
                          fine_surfaces=[{**_fine_surface(two), "file": ""}])


def test_the_decision_survives_the_plan_on_disk(tmp_path, two):
    """The host writes the plan and the container reads it, so this crosses as JSON."""
    plan = PL.ConjugatePlan(
        background_cell_m=two["background_m"], levels=two["broad_levels"],
        expect_fluid_volume_m3=5.3661786893e-05, expect_fluid_pieces=2, volume_tol=0.05,
        narrowest_m=SCALES["broad"]["width_m"], fine_surfaces=(_fine_surface(two),))
    PL.write_plan(tmp_path, plan)
    raw = json.loads((Path(tmp_path) / PL.PLAN_NAME).read_text())
    assert raw["fine_surfaces"][0]["file"] == PL.FLUID_FINE_STL
    back = PL.read_plan(tmp_path)
    assert len(back.fine_surfaces) == 1
    got = back.fine_surfaces[0]
    assert got["levels"] == two["fine_levels"]
    assert got["name"] == "microchannels"
    # and it still renders from what came back off the disk
    out = C.render_meshdict(block_file=PL.BLOCK_STL, fluid_file=PL.FLUID_STL,
                            background_cell_m=back.background_cell_m, levels=back.levels,
                            fine_surfaces=[dict(s) for s in back.fine_surfaces])
    assert PL.FLUID_FINE_STL in out


@pytest.mark.parametrize("bad, why", [
    ({"file": None}, "needs file and levels"),
    ({"levels": None}, "needs file and levels"),
    ({"levels": 0}, "asks for 0 levels"),
    ({"levels": -2}, "asks for -2 levels"),
])
def test_a_fine_surface_that_is_not_one_is_refused_at_the_boundary(tmp_path, two, bad, why):
    """Checked where it crosses into the container, as the zones are."""
    plan = PL.ConjugatePlan(background_cell_m=two["background_m"], levels=two["broad_levels"],
                            expect_fluid_volume_m3=1e-6, expect_fluid_pieces=2, volume_tol=0.05,
                            narrowest_m=SCALES["broad"]["width_m"])
    PL.write_plan(tmp_path, plan)
    p = Path(tmp_path) / PL.PLAN_NAME
    raw = json.loads(p.read_text())
    raw["fine_surfaces"] = [{**_fine_surface(two), **bad}]
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=why):
        PL.read_plan(tmp_path)


# ---------------------------------------------------------------- the seams the source holds

def _driver_source() -> str:
    from meshpipeline.engines.cfmesh_conjugate import driver

    return Path(driver.__file__).read_text(encoding="utf-8")


def test_the_driver_builds_this_shape():
    """_fine_surface above has to be what the driver really builds, or these tests prove nothing
    about the driver. Asserted on the source because building it for real means an async run with
    a workspace, a mesher and a model behind it."""
    src = _driver_source()
    for key in ('"name": "microchannels"', '"file": PL.FLUID_FINE_STL',
                '"levels": int(two["fine_levels"])', '4.0 * float(two["fine_cell_m"])'):
        assert key in src, f"the driver no longer builds {key}; this file is out of step with it"


def test_the_thin_surface_travels_in_the_native_payload():
    """THE EXACT OMISSION THAT COST A RUN, in its other form. A surface the container cannot open
    is a refinement that silently does not happen, and the mesher reports success either way."""
    src = _driver_source()
    assert "payload.append(PL.FLUID_FINE_STL)" in src, (
        "the thin surface is not added to the native payload, so the container would mesh without "
        "it and report success")
    assert "note_native_payload(ws, payload)" in src


def test_the_plan_carries_it_onto_the_built_plan():
    """plan_for does not know about fine surfaces, so the driver has to graft it on - the same way
    it already grafts the declared port names."""
    src = _driver_source()
    assert "dataclasses.replace(plan, fine_surfaces=(fine_surface,))" in src


def test_the_mesher_ceiling_is_actually_checked():
    """A GUARD NOBODY CALLS IS WORSE THAN NO GUARD: it reads as cover that is not there.

    budget.refuse_for_octree was reachable from nowhere in src when it was written. The driver
    prices everything against polcfg.CELL_HARD_LIMIT, which defaults to 8,000,000 - twelve times
    below the mesher's own ceiling - so the guard could not fire at the default, and would not have
    fired at a raised one either because nothing asked it. Raising that limit is exactly what a run
    on a big machine does, which is when the ceiling starts to matter.

    AND IT HAS TO BE ASKED WITH THE RANK COUNT, which is the second half of the same mistake. The
    ceiling is per PROCESS, so a gate that asks it without saying how many processes there will be
    answers a question nobody asked: the sizing step would approve a two-rank plan and this would
    then decline it 70 lines later, writing "on one process" about a run that was never going to
    be one process. Both gates take the same count, decided once.
    """
    src = _driver_source()
    assert "B.refuse_for_octree(estimate, ranks=ranks)" in src, (
        "nothing calls the mesher's own ceiling, so a raised cell limit turns a refusal into a "
        "five-minute abort reported as a mesher that wrote nothing")
    assert "if octree_said:" in src
    # the two gates must agree about how many processes there are, or one of them is answering
    # about a run that will not happen
    assert "ranks=ranks)" in src and src.count("ranks=ranks)") >= 2, (
        "the rank count reaches only one of the two size gates")
    assert "det.conjugate_ranks()" in src, (
        "the rank count is not read from the pinned setting, so it could vary between two runs "
        "of the same job - and parallel changes the mesh")


def test_the_two_limits_are_different_questions():
    """The deployment's limit is about the machine; the mesher's ceiling is not. Checking only one
    of them is how a part gets started that cannot finish."""
    from meshpipeline.settings import policy as polcfg

    assert int(polcfg.CELL_HARD_LIMIT) != B.octree_ceiling_cells(), (
        "if these are ever equal, one of the two checks is redundant and the comment in the driver "
        "explaining why there are two is wrong")
    # the ceiling binds above the limit, which is the case the guard exists for
    assert B.refuse_for_octree(B.octree_ceiling_cells()) == ""
    assert B.refuse_for_octree(B.octree_ceiling_cells() + 1)
