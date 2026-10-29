"""A conjugate mesher fills one manifold volume, and the easy one is the box round the part.

Every cell between the part and that box is paid for at the background size. The engine says what
that costs, beside the patch name a shell domain would use:

    Measured on the first customer cold plate: the cuboid is 846,543 mm3 and metal plus coolant is
    274,807 mm3, so 67.5% of a box-domain mesh's cells are air.

Until now there was nothing to switch to: `domain_solid` fused the metal with each passage, the
fuse is wrong on that assembly whatever glue mode it gets, and `write_domain` refused what came
out - so the box stood. The domain comes out of the split now, measured on
`ColdPlateAssembly_CLEAN.step`:

    ok              true
    solid_name      wall_outer
    triangles       27,696
    volume_mm3      276,372.39
    box_mm3         750,014.89
    share_of_box    0.3685          against BOX_WORTH_KEEPING 0.60
    metal_mm3       222,661.97
    fluid_mm3       53,661.79

These pin the SWITCH: when the skin is worth building, that it is used; when it is not, that
nothing moves. The consumer side was already written - `OUTER_PATCH`, `is_domain_face`, and
`mouths.shell_stubs`, which was documented against this exact assembly and called by nothing.
"""
from __future__ import annotations

import inspect

import pytest

from meshpipeline.cad.fluid_volume import BOX_WORTH_KEEPING
from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh_conjugate import driver as D

#: the record `write_domain` left on Corintis's own plate
CORINTIS = {"domain": {"ok": True, "share_of_box": 0.3685, "volume_mm3": 276372.39,
                       "box_mm3": 750014.89, "triangles": 27696}}


def test_the_customers_plate_is_worth_shelling():
    assert C.use_shell_domain(CORINTIS) is True
    assert CORINTIS["domain"]["share_of_box"] < BOX_WORTH_KEEPING


def test_a_part_that_fills_its_own_box_keeps_the_box():
    """The negative control, and the reason the threshold exists: on a plain cuboid the box IS
    the domain and the box is the simpler surface."""
    assert C.use_shell_domain({"domain": {"ok": True, "share_of_box": 1.0}}) is False


@pytest.mark.parametrize("share", [BOX_WORTH_KEEPING, BOX_WORTH_KEEPING + 1e-9, 0.75])
def test_at_or_above_the_threshold_the_box_stands(share):
    assert C.use_shell_domain({"domain": {"ok": True, "share_of_box": share}}) is False


@pytest.mark.parametrize("bad", [
    None, {}, {"domain": None}, {"domain": {}}, {"domain": {"ok": False, "share_of_box": 0.1}},
    {"domain": {"ok": True}}, {"domain": {"ok": True, "share_of_box": None}},
    {"domain": {"ok": True, "share_of_box": "0.3"}}, {"domain": {"ok": True, "share_of_box": 0.0}},
    {"domain": {"ok": True, "share_of_box": -1.0}},
])
def test_anything_unmeasured_keeps_the_box(bad):
    """A domain that did not come out, a record with no share, a share that is not a number: the
    box stands. This is an improvement on a path that already worked, so it may not be the thing
    that breaks it."""
    assert C.use_shell_domain(bad) is False


def test_the_driver_asks_the_engine_rather_than_redoing_the_arithmetic():
    src = inspect.getsource(D.drive) if hasattr(D, "drive") else inspect.getsource(D)
    assert "use_shell_domain" in src


def test_the_skin_is_what_the_mesher_is_handed_when_it_is_used():
    """`block.stl` is the surface cfMesh fills, and everything downstream reads that one name, so
    the domain is copied over it rather than threaded through as a second path."""
    src = inspect.getsource(D)
    assert "PL.DOMAIN_STL" in src
    # the WRITE, not the log line that also opens with `if shell:`
    at_write = src.index("(ws / PL.BLOCK_STL).write_bytes")
    assert "PL.DOMAIN_STL" in src[at_write:at_write + 200]
    assert src.rindex("if shell:", 0, at_write) > 0, "the copy is under the shell branch"
    assert "block_surface_stl" in src, "the box is still built when the skin is not used"


def test_a_shell_cuts_its_ports_where_they_already_are():
    """On a box the mouths are inside it and have to be stubbed out to a face first. On a shell
    they are already ON the surface, so each is a patch cut where it is - which is exactly what
    `mouths.shell_stubs` does, and it had no callers."""
    from meshpipeline.engines.cfmesh_conjugate import mouths as MO

    src = inspect.getsource(D)
    assert "MO.shell_stubs(rec[\"ports\"])" in src
    assert "MO.stubs_for(rec[\"ports\"], lo, hi)" in src, "the box path must still exist"
    stubs = MO.shell_stubs([{"centroid_m": (0.1, 0.05, 0.04), "normal": (0, 1, 0),
                             "diameter_m": 0.0114, "passage": 0}])
    assert len(stubs) == 1
    assert stubs[0].face == C.OUTER_PATCH
    assert stubs[0].length == 0.0, "a shell mouth has nothing to reach out to"
    assert C.is_domain_face(stubs[0].face), "a port must be cuttable out of it"


def test_the_domain_is_asked_for_on_the_extraction_already_being_paid_for():
    """One extraction, two answers. Asking twice would double the slowest step in the build."""
    src = inspect.getsource(D)
    assert "domain_path=ws / PL.DOMAIN_STL" in src
    assert src.count("surface_bounded") <= 2, "the coolant is extracted once"
