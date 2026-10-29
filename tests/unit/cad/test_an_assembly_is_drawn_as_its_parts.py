"""A CAD file of several solids is drawn as several named parts, not one welded lump.

WHY THIS EXISTS. Rehaan, 2026-10-29: the viewer must show everything, "including separate meshes
and separate CAD files at the pre-meshing CAD stage".

A thermal model (ECXML) has always been drawn this way - `ecxml_place.placed_skin` returns one
block per part under its own name and colour, and the console already knows how to draw
`{"parts": True, "patches": [...]}`. A STEP assembly got none of it. The first customer's package
is six solids (a top lid, a board cooling circuit, an I/O splitter, a microfluidic core and two
fittings) and arrived on the stage as one grey shape, so a customer checking that we had read
their assembly correctly could not see whether we had found their parts at all.

The payload is the SAME ONE, so nothing in the console changes.

MEASURED, 2026-10-29, on a three-solid STEP of 10x8x3, 6x6x6 and 20x2x2 mm boxes: three patches,
three colours, and 240.0 / 216.0 / 80.0 mm3 - every volume exact.
"""
import tempfile
from pathlib import Path

import pytest


def _step(sizes, dest=None):
    """A STEP compound of one box per size, spaced apart so they are separate solids."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    dest = Path(dest or (Path(tempfile.mkdtemp()) / "parts.step"))
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for i, (dx, dy, dz) in enumerate(sizes):
        b.Add(comp, BRepPrimAPI_MakeBox(gp_Pnt(i * 50.0, 0.0, 0.0), dx, dy, dz).Shape())
    w = STEPControl_Writer()
    w.Transfer(comp, STEPControl_AsIs)
    assert w.Write(str(dest))
    return dest


class _Ref:
    original_filename = "parts.step"
    suffix_hint = ".step"
    owner_id = "dev-user"
    source_id = "local"
    stated_unit = None

    def to_payload(self):
        return {}


def _meshed(step: Path):
    """The shape as the stage has it: scaled to metres and tessellated to view size."""
    from meshpipeline.application.geometry_check import _prepared_coordinates
    from meshpipeline.cad.scout import read_cad, view_shape

    prepared, _note = _prepared_coordinates(step, None, _Ref())
    # view_shape rather than draw_view: this wants the shape and not a skin file beside it.
    return view_shape(step, prepared=prepared, shape=read_cad(step))


SIZES = [(10.0, 8.0, 3.0), (6.0, 6.0, 6.0), (20.0, 2.0, 2.0)]
VOLUMES = [240.0, 216.0, 80.0]


@pytest.fixture(scope="module")
def three():
    from meshpipeline.cad.solid_parts import parts_payload

    step = _step(SIZES)
    return parts_payload(_meshed(step), path=step)


#   -- the drawing -------------------------------------------------------------------------------

def test_every_solid_is_its_own_patch(three):
    assert three is not None
    assert three["part_count"] == 3
    assert len(three["patches"]) == 3


def test_it_is_the_payload_the_console_already_draws(three):
    """The same keys ecxml_place.placed_skin returns, so the console needs no new branch."""
    assert three["parts"] is True
    assert three["kind"] == "skin" and three["is_mesh"] is False
    assert three["mesh_units"] == "m"
    assert "edges" in three and three["edges"]["count"] > 0


def test_each_part_is_named_and_identified(three):
    assert [p["part_id"] for p in three["patches"]] == ["Part_1", "Part_2", "Part_3"]
    assert all(p["name"] for p in three["patches"])


def test_each_part_carries_its_own_size_and_volume(three):
    """A NAME ALONE DOES NOT ANSWER "is this my part?", which is the question the stage exists for.
    Two solids called Part_3 and Part_4 look the same in a list; 80 mm3 against 240 does not."""
    assert [p["size_mm"] for p in three["patches"]] == [list(s) for s in SIZES]
    assert [p["volume_mm3"] for p in three["patches"]] == pytest.approx(VOLUMES)


def test_the_parts_are_coloured_apart(three):
    colours = [tuple(p["color"]) for p in three["patches"]]
    assert len(set(colours)) == 3


def test_the_colours_are_the_same_ring_the_placed_drawing_uses(three):
    """One file drawn both ways must not change colour."""
    from meshpipeline.cad.ingest.ecxml_place import PART_COLOURS

    assert tuple(three["patches"][0]["color"]) == tuple(PART_COLOURS[0])


#   -- what must not change ----------------------------------------------------------------------

def test_one_solid_is_not_an_assembly_and_is_drawn_as_before():
    """Every part in the corpus is one solid. The welded single-patch skin is both what it should
    look like and the path they all take, so this must return nothing for them.

    IT BUILDS ITS OWN ONE-SOLID STEP. The first version reached for
    `devtools/semiconductor/cad/two_scale_plate_ported_solid.step`, which is GENERATED output and
    sits under a gitignored directory (.gitignore:69) - so this row passed on the machine that had
    just generated it and would have failed on any fresh checkout. A test that only passes where
    it was written is worse than no test.
    """
    from meshpipeline.cad.solid_parts import parts_payload

    step = _step([(12.0, 9.0, 4.0)])
    assert parts_payload(_meshed(step), path=step) is None


def test_a_model_of_thousands_of_solids_is_not_drawn_this_way():
    """A chip package can state two thousand (Corintis: ECXML "up to 2000 solids"), and two
    thousand patches is a payload the console unpacks one at a time before it draws anything."""
    from meshpipeline.cad import solid_parts as SP

    assert SP.MAX_DRAWN_PARTS == 64
    step = _step([(2.0, 2.0, 2.0)] * (SP.MAX_DRAWN_PARTS + 1))
    assert SP.parts_payload(_meshed(step), path=step) is None


def test_the_count_is_the_file_s_even_when_a_part_cannot_be_drawn():
    """Leaving an undrawable solid out silently shows a five-part assembly as four."""
    from meshpipeline.cad.solid_parts import part_skins

    step = _step(SIZES)
    parts = part_skins(_meshed(step), path=step)
    assert len(parts) == 3
    assert all("name" in p and "id" in p for p in parts)


#   -- the stage serves it -----------------------------------------------------------------------

def test_the_check_draws_the_parts_and_the_stage_serves_them():
    """THE GAP THIS CODEBASE HAS HAD TWICE: a renderer tested through its own front door cannot
    catch a caller that never calls it. It caught one here: the skin store is in , not in
     (which only wraps it in the progress tracker), so the first version of the wiring
    asserted against the wrong function and would have passed on a stage that served nothing."""
    import inspect

    from meshpipeline.application import geometry_check as GC

    src = inspect.getsource(GC._scout_exact)
    assert "_draw_parts" in src, "the check has to draw them"
    assert "draw_view" in src, (
        "the skin and the parts have to come off ONE tessellation, and `draw_view` is the one "
        "seam that writes the skin and hands the meshed shape back - two functions here split "
        "the seam the check's own tests fake, and the real reader then refused their fake STEP")
    run = inspect.getsource(GC._scout)
    assert "_parts_skin" in run, "the stage has to serve them"
    assert "drawn_parts or skin_payload(skin)" in run, (
        "and fall back to the welded skin when there are none")


def test_the_drawing_never_fails_the_check(tmp_path):
    """A file whose solids cannot be drawn separately is still measured, confirmed and meshed."""
    from meshpipeline.application.geometry_check import PARTS_SKIN, _draw_parts, _parts_skin

    _draw_parts(object(), Path("nope.step"), tmp_path)        # not a shape at all
    assert not (tmp_path / PARTS_SKIN).exists()
    assert _parts_skin(tmp_path) is None


def test_a_half_written_drawing_is_not_served(tmp_path):
    from meshpipeline.application.geometry_check import PARTS_SKIN, _parts_skin

    (tmp_path / PARTS_SKIN).write_text("{not json")
    assert _parts_skin(tmp_path) is None
    (tmp_path / PARTS_SKIN).write_text('{"patches": []}')
    assert _parts_skin(tmp_path) is None, "a drawing with no patches is not a drawing"
