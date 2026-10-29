"""The domain a conjugate mesher fills is metal UNION coolant, not the box around the part.

cfMesh fills ONE manifold volume and the conjugate engine cuts the two regions out of it
afterwards, so the easy manifold has always been the cuboid round the part. Every cell of air
between the part and that cuboid is paid for at the background size. Measured on the first
customer cold plate, 2026-10-29:

    bounding box      846,543 mm3     177.51 x 47.21 x 101.01 mm
    the six solids    222,662 mm3     26.3% of it
    the coolant        52,145 mm3     one circuit, two ports
    metal + coolant   274,807 mm3     32.5% of it

Two 33 mm fittings standing off a 7-9 mm plate stack put the other 67.5% there, and none of it is
part.

AND THE PART ON ITS OWN IS NOT THE DOMAIN EITHER. A cold plate's boundary is one closed surface
that runs over the outside, dives in at the inlet, winds through the channels and comes back out
at the outlet. A mesher given it fills the METAL and leaves the coolant outside the domain
altogether: measured on a part skin handed to cfMesh, the fluid region came back at 256 cells
against 495,506. Fusing the coolant back in caps the mouths with the coolant's own end faces and
leaves exactly the part's outer skin, enclosing metal and coolant together - which is the volume
the split is going to cut in two.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from meshpipeline.cad import fluid_volume as FV

#: A plate with two straight channels through it, 2 x 4 x 1 mm. Measured on it: one solid of
#: 7.04 mm3 and two passages of 0.48 mm3 each, so the domain is 8.00 mm3 - which is also exactly
#: its bounding box, because the plate is a plain cuboid. The negative control, and the reason
#: BOX_WORTH_KEEPING exists: on a part that fills its own box the box IS the domain and there is
#: nothing to build.
FIXTURE = Path("tests/fixtures/geometry/cold_plate_2ch.step")
METAL_MM3, FLUID_MM3, DOMAIN_MM3 = 7.04, 0.96, 8.0


@pytest.fixture(scope="module")
def plate():
    """The fixture loaded in metres, with its fluid extracted (0.1 s on this part)."""
    pytest.importorskip("OCP")
    if not FIXTURE.is_file():
        pytest.skip(f"{FIXTURE} is not in this checkout")
    shape = FV._load({"source": str(FIXTURE), "scale": 1e-3, "kind": "cad"})
    solids = FV._solids(shape)
    return solids, FV.extract(shape)


def test_the_part_and_its_coolant_are_what_went_in(plate):
    """Stated so a later change to the fixture shows up here and not three tests down."""
    solids, x = plate
    assert len(solids) == 1
    assert round(sum(FV._volume(s) for s in solids) * 1e9, 2) == METAL_MM3
    assert len(x.passages) == 2
    assert round(sum(p.volume for p in x.passages) * 1e9, 2) == FLUID_MM3


def test_the_domain_is_the_two_added_together(plate):
    """7.04 of metal and 0.96 of coolant make 8.00, as ONE solid. Not 7.04 (the part, which
    leaves the coolant outside the domain) and not two pieces (the fuse not taking)."""
    solids, x = plate
    dom = FV.domain_solid(solids, x)
    assert dom is not None
    pieces = FV._solids(dom)
    assert len(pieces) == 1
    assert round(sum(FV._volume(s) for s in pieces) * 1e9, 2) == DOMAIN_MM3


def test_it_comes_out_as_one_closed_surface(plate, tmp_path):
    """cfMesh fills what the surface encloses, so an open surface encloses the universe."""
    solids, x = plate
    rec = FV.write_domain(solids, x, tmp_path / "domain.stl")
    assert rec["ok"], rec.get("reason")
    assert rec["solids"] == 1
    assert rec["triangles"] > 0
    assert Path(rec["path"]).is_file()
    tris = __import__("meshpipeline.cad.stl_io", fromlist=["x"]).read_stl_triangles(rec["path"])
    assert FV.closed_pieces(tris) == 1
    assert round(FV.surface_volume(tris) * 1e9, 2) == DOMAIN_MM3


def test_the_record_says_what_the_box_was_worth(plate, tmp_path):
    """The caller decides whether to build the shell, so the record has to carry the share."""
    solids, x = plate
    rec = FV.write_domain(solids, x, tmp_path / "domain.stl")
    assert rec["metal_mm3"] == METAL_MM3
    assert rec["fluid_mm3"] == FLUID_MM3
    assert rec["volume_mm3"] == DOMAIN_MM3
    assert rec["share_of_box"] == 1.0, "a plain cuboid plate fills its own box"
    assert rec["box_mm3"] == DOMAIN_MM3
    assert rec["lo_m"] and rec["hi_m"]


def test_a_part_that_fills_its_box_is_left_with_the_box(plate, tmp_path):
    """THE NEGATIVE CONTROL. This plate gains nothing: its share is 1.0, far above
    BOX_WORTH_KEEPING, and the cuboid is the simpler surface. The customer plate's 0.325 is what
    the threshold is for."""
    solids, x = plate
    rec = FV.write_domain(solids, x, tmp_path / "domain.stl")
    assert rec["share_of_box"] > FV.BOX_WORTH_KEEPING
    assert 0.325 < FV.BOX_WORTH_KEEPING, "the customer plate has to come out under it"


def test_nothing_to_fuse_is_not_a_domain(plate, tmp_path):
    """Both halves are needed. No solids is not a part; no passage is a part with no coolant, and
    a conjugate mesh of it has no second region to cut."""
    solids, x = plate
    assert FV.domain_solid([], x) is None
    empty = FV.Extraction(passages=[], parts=1, caps=0, regions=1, seconds=0.0)
    assert FV.domain_solid(solids, empty) is None
    said = FV.write_domain(solids, empty, tmp_path / "domain.stl")
    assert said["ok"] is False
    assert "would not fuse" in said["reason"]
    assert not (tmp_path / "domain.stl").exists()


def test_the_reason_travels_in_the_answer_and_not_only_the_log():
    """THE DIAGNOSIS WAS LOST. This runs in a time-boxed child process whose stdout is read and
    thrown away unless it times out (run_bounded), so on the first customer assembly it came back
    `"domain": null` after 1,001 s with nothing anywhere saying why. Every way out carries a
    reason now, because the answer is the only thing that crosses the process boundary."""
    import inspect

    src = inspect.getsource(FV.write_domain)
    assert "return None" not in src, "a bare None loses the reason"
    assert src.count('"reason"') >= 5
    assert '"ok": True' in src
    # and the caller says it rather than falling back in silence
    said = inspect.getsource(FV.surface_bounded)
    assert "the box stands" in said
    assert 'dom.get("ok")' in said


def test_the_fuse_rides_on_the_extraction_already_paid_for():
    """The split is the expensive step - 382 s on the customer assembly - so the domain is built
    in the same child process, on the same Extraction, or not at all."""
    src = inspect.getsource(FV._child)
    assert "domain_path" in src
    assert "write_domain(" in src
    at_extract = src.index("x = extract(shape)")
    assert src.index("write_domain(") > at_extract, "it must reuse x, not extract again"


def test_the_caller_has_to_ask_for_it():
    """`surface_bounded` is called by engines that have no use for a domain; the work and the file
    only happen when a caller passes a path for it."""
    src = inspect.getsource(FV.surface_bounded)
    assert "domain_path" in src
    assert 'if domain_path else {}' in src


def test_a_domain_that_cannot_be_built_is_not_a_failure():
    """The box is the answer when this is not: the engine has always used it and still can."""
    src = inspect.getsource(FV._child)
    assert "write_domain(" in src
    assert "except Exception" in src, "a raise in here must not cost the coolant surface"
    assert inspect.getsource(FV.write_domain).count("return") >= 6, "no raise out of here"
