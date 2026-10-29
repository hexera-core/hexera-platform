"""The domain is metal plus coolant, and the split already knew it without a boolean.

`domain_solid` fused the metal with each passage. The fuse is right on the fixture and WRONG on
the first customer assembly, whatever glue mode it is given - 1,118 mm3 of bounding box out of a
part whose own box is 846,543 - so `write_domain` refused it and the engine kept the cuboid.

The split that found the coolant in the first place classifies every region of space as outside,
metal or space, so the domain is "every region that is not outside" and its boundary is every face
of those regions that two of them do not share: the part's outer skin with each port mouth closed
by its own cap. No boolean, no tolerance.

MEASURED ON `ColdPlateAssembly_CLEAN.step` (Corintis, 6 solids, 2 passages, 70 regions), the part
the fuse cannot do:

    domain faces   348
    sewn           2 shells: one degenerate (1 free edge, 0 mm3), one CLOSED
    closed shell   276,372.4 mm3
    the cuboid     750,014.9 mm3      what the engine meshes today
    ratio          2.71x less volume to fill

276,372.4 against the 276,324 the fuse produces where it does work is the same answer to four
figures, so this is the same domain by a route that does not need it.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("OCP")

from meshpipeline.cad import fluid_volume as FV  # noqa: E402

FIXTURE = Path("tests/fixtures/geometry/cold_plate_2ch.step")
DOMAIN_MM3 = 8.0


@pytest.fixture(scope="module")
def plate():
    if not FIXTURE.is_file():
        pytest.skip(f"{FIXTURE} is not in this checkout")
    shape = FV._load({"source": str(FIXTURE), "scale": 1e-3, "kind": "cad"})
    # ASKED FOR, because it is not free: the walk covers every region that is not outside, which
    # on the customer assembly is 11.0 s of a 359 s split for 348 faces the geometry check has no
    # use for. Only a build that is going to write the domain asks.
    return FV._solids(shape), FV.extract(shape, want_domain=True)


def test_the_split_carries_the_domain_it_already_knew(plate):
    """It was computed and thrown away: `kind[i]` says outside, metal or space for every region,
    and nothing downstream could see it."""
    _solids, x = plate
    assert x.domain_boundary, "the split produced no domain boundary"
    assert len(x.domain_boundary) > 5


def test_the_domain_from_the_split_is_the_two_added_together(plate):
    """7.04 of metal and 0.96 of coolant, with no fuse anywhere in the path."""
    _solids, x = plate
    dom = FV._domain_from_split(x)
    assert dom is not None, "the split's own domain did not close"
    pieces = FV._solids(dom)
    assert len(pieces) == 1
    assert round(sum(FV._volume(s) for s in pieces) * 1e9, 2) == DOMAIN_MM3


def test_it_is_the_route_domain_solid_takes_first(plate):
    """The fuse is the FALLBACK now. Ordered in the source, because a run that silently fell back
    would be slow and right rather than fast and right, and would look identical."""
    solids, x = plate
    src = inspect.getsource(FV.domain_solid)
    at_split = src.index("_domain_from_split")
    at_fuse = src.index("fluids = []")
    assert at_split < at_fuse, "the fuse is reached before the split's own answer"
    assert round(sum(FV._volume(s) for s in FV._solids(FV.domain_solid(solids, x))) * 1e9, 2) \
        == DOMAIN_MM3


def test_a_split_that_carries_nothing_still_fuses(plate):
    """Every extraction made before this existed, and any split that classified no region: the
    fuse is still there and still right where it works."""
    solids, x = plate
    bare = FV.Extraction(x.passages, x.parts, x.caps, x.regions, x.seconds, diag=x.diag)
    assert not bare.domain_boundary
    assert FV._domain_from_split(bare) is None
    dom = FV.domain_solid(solids, bare)
    assert dom is not None, "the fallback did not take"
    assert round(sum(FV._volume(s) for s in FV._solids(dom)) * 1e9, 2) == DOMAIN_MM3


def test_a_degenerate_shell_does_not_become_the_domain(plate):
    """Sewing faces off a split leaves the odd sliver that closes nothing - two shells came out of
    the customer assembly, one of them 0 mm3 with a free edge. The largest CLOSED one wins, which
    is why this takes a maximum rather than the first that closes."""
    src = inspect.getsource(FV._domain_from_split)
    assert "_free_edges(sh)" in src, "an open shell bounds nothing and must be skipped"
    assert "best_v" in src and "> best_v" in src, "it must take the largest, not the first"
    assert "if best is None or best_v <= 0.0:" in src


def test_it_never_turns_a_working_run_into_a_broken_one(plate):
    """A sew that raises, a shell that will not close: the answer is None and the fuse runs. This
    is an improvement on a path that already worked, so it may not be the thing that fails."""
    src = inspect.getsource(FV._domain_from_split)
    assert "except Exception" in src
    assert src.rstrip().endswith("return None")
    assert "falling back to the fuse" in src


def test_nobody_pays_for_it_unless_they_asked():
    """The geometry check reads passages, caps and volumes and never a domain - and it is the one
    caller with a wall-clock budget. 11.0 s of a 359 s split, against a budget that was 300."""
    if not FIXTURE.is_file():
        pytest.skip(f"{FIXTURE} is not in this checkout")
    shape = FV._load({"source": str(FIXTURE), "scale": 1e-3, "kind": "cad"})
    assert FV.extract(shape).domain_boundary == [], "the check paid for a domain it never wanted"
    assert FV.extract(shape, want_domain=True).domain_boundary, "asking for it got nothing"
