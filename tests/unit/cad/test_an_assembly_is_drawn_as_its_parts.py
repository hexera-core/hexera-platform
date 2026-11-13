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
import base64
import json
import struct
import tempfile
from pathlib import Path

import pytest


def b64f32(b: str) -> list[float]:
    """The float32 block the console reads with its own b64f32 (ui/js/viewer/geometry_stage.js)."""
    raw = base64.b64decode(b)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


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
#
# WHAT WAS HERE BEFORE, and why it is gone. One test called `inspect.getsource(GC._scout)` and
# grepped it for "_parts_skin" and "drawn_parts or skin_payload(skin)". Both strings were there,
# the row was green for a fortnight, and the branch holding them was DEAD: on the real path
# `preview` is always a _Preview whose publish() has set `skin_key`, so `if skin_key is None:`
# never opened and `_parts_skin` - whose only call site is inside it - was never called once.
# Measured 2026-11-12: skin.json held one welded patch on a six-solid cold plate. The rows below
# RUN the check and read what it stored. A text assertion cannot tell a live branch from a dead
# one, and this file's own docstring warned about exactly that before it happened.

SID = "abcdef12-parts"


class _Store:
    """Uploads and reads, like the object store - and keeps every version of the skin, because
    the thing under test is that the SECOND one replaces the first at the same key."""

    def __init__(self):
        self.written: dict[str, bytes] = {}
        self.checks: list[dict] = []
        self.skins: list[dict] = []
        #: every get_bytes, in order - a stored skin comes back whole, so reading one to look at a
        #: single float is tens of MB on the wire for the files this path exists for
        self.reads: list[str] = []

    def upload_file(self, *, local_path, object_key, **_):
        data = Path(local_path).read_bytes()
        self.written[object_key] = data
        if object_key.endswith("scout.json"):
            self.checks.append(json.loads(data))
        if object_key.endswith("skin.json"):
            self.skins.append(json.loads(data))

    def get_bytes(self, *, object_key):
        from meshpipeline.contracts.object_storage import ObjectNotFound

        self.reads.append(object_key)
        if object_key not in self.written:
            raise ObjectNotFound(object_key)
        return self.written[object_key]

    def delete_object(self, *, object_key):
        self.written.pop(object_key, None)


def _run_check(monkeypatch, tmp_path, sizes, sid):
    """The real check over a STEP of these solids, wired as the worker wires it - the preview and
    all (run_geometry_check -> _run_scout -> _Preview). Only the upload fetch and the pictures are
    stood in for; the CAD reading, the drawing and the parts drawing are the product's own."""
    import tempfile as T

    import meshpipeline.render.scout_snapshots as snaps
    from meshpipeline.application import geometry_check as GC
    from meshpipeline.contracts import object_storage

    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    monkeypatch.setattr(T, "tempdir", str(tmp_path))
    step = _step(sizes, tmp_path / "parts.step")

    def fetch(_ref, work):
        dest = Path(work, "geometry.step")
        dest.write_bytes(step.read_bytes())
        return dest
    monkeypatch.setattr(GC, "_fetch", fetch)
    monkeypatch.setattr(snaps, "render_snapshots", lambda skin, openings, out: [])
    monkeypatch.setattr(snaps, "render_upright_sheet",
                        lambda skin, out, **k: (_ for _ in ()).throw(RuntimeError("not drawn here")))
    GC.write_status(sid, "pending")
    result = GC.run_geometry_check(session_id=sid, owner_id="o1", source={
        "source_id": "s1", "owner_id": "o1", "object_key": "uploads/s1/parts.step",
        "sha256": "0" * 64, "size_bytes": step.stat().st_size,
        "original_filename": "parts.step", "suffix_hint": ".step"})
    return store, result


@pytest.fixture()
def assembly(monkeypatch, tmp_path):
    return _run_check(monkeypatch, tmp_path, SIZES, SID)


def test_the_skin_the_stage_fetches_is_the_file_s_own_solids(assembly):
    """The check RAN. What it left at sessions/<id>/geometry_check/skin.json - the one object
    api/v1/geometry serves to the stage - is the three parts, named, identified and coloured
    apart, not one welded patch."""
    store, result = assembly
    assert result["status"] == "scouted", result
    skin = json.loads(store.written[f"sessions/{SID}/geometry_check/skin.json"])
    assert skin.get("parts") is True, "the console's parts branch is chosen by this flag"
    assert len(skin["patches"]) == 3
    assert [p["part_id"] for p in skin["patches"]] == ["Part_1", "Part_2", "Part_3"]
    assert all(p.get("name") for p in skin["patches"])
    assert len({tuple(p["color"]) for p in skin["patches"]}) == 3, "coloured apart"
    assert [p["volume_mm3"] for p in skin["patches"]] == pytest.approx(VOLUMES)


def test_the_preview_is_shown_first_and_then_replaced_by_the_named_solids(assembly):
    """THE TWO HALVES OF THE DEFECT, in one measurement. The preview stores the welded lump within
    seconds so something is on screen; the full reading REPLACES it at the same key with the named
    solids. Before the fix there was exactly one skin version, the lump, for the whole session."""
    store, _result = assembly
    assert len(store.skins) == 2, [len(s.get("patches") or []) for s in store.skins]
    first, second = store.skins
    assert len(first["patches"]) == 1 and not first.get("parts"), "the preview is the quick drawing"
    assert len(second["patches"]) == 3 and second["parts"] is True
    # ...and the check says so where the stage reads it: the preview is marked while it stands,
    # and the record that carries the parts is no preview, which is the client's signal to re-fetch
    assert [c.get("preview") for c in store.checks] == [None, "running", None]
    assert [p["name"] for p in _result_parts(_result)] == [p["name"] for p in second["patches"]]


def _result_parts(result):
    return result["facts"]["parts_drawn"]


def test_both_drawings_come_off_one_tessellation(assembly):
    """MESHED ONCE, DRAWN TWICE. A big STEP takes 10-25 s to tessellate and doing it twice would
    double the slowest step of the check for nothing, so the parts are read off the triangulation
    the welded skin was written from - measurable as the same triangle count, 36 for three boxes."""
    store, _result = assembly
    welded, parts = store.skins
    assert sum(p["triangles"] for p in parts["patches"]) == welded["patches"][0]["tri_count"] == 36


def test_a_one_solid_file_keeps_the_preview_s_drawing_and_is_not_stored_twice(monkeypatch, tmp_path):
    """Every part in the corpus is one solid: the welded single-patch skin is what they should look
    like, and the fix must not make the scout upload a second, identical copy of it for them."""
    store, result = _run_check(monkeypatch, tmp_path, [(12.0, 9.0, 4.0)], "abcdef12-single")
    assert result["status"] == "scouted", result
    assert len(store.skins) == 1 and len(store.skins[0]["patches"]) == 1
    assert "parts" not in store.skins[0] and "parts_drawn" not in result["facts"]


def _states_no_unit(monkeypatch):
    """Make the scout read the STEP as a file that DECLARES NO UNIT. That is the whole precondition
    of a re-read skin: `_name` rescales the stored skin only under `facts["unit_assumed"]`, and it
    is the only caller of `_rescale_skin`, so a file that states its own unit - every STEP OCP
    writes, which is why this has to be forced - can never have had one."""
    from meshpipeline.application import geometry_check as GC

    real = GC._prepared_coordinates

    def no_unit(path, interp_ref, ref):
        prepared, _note = real(path, interp_ref, ref)
        return prepared, "the file does not say its unit, so I assume millimetres"
    monkeypatch.setattr(GC, "_prepared_coordinates", no_unit)


def test_a_skin_the_naming_re_read_in_another_unit_is_replaced_in_that_unit(monkeypatch, tmp_path):
    """THE INTERLEAVING THE REPLACEMENT HAD TO ANSWER. A file that states no unit is drawn in the
    scout's assumed millimetres; when the user settles metres in the chat the naming re-reads the
    stored skin a thousand times larger (_rescale_skin) and places the stage's pins on it. A
    replacement written in the old unit would draw the part at a thousandth of its own pins.

    The naming running between the preview and the full reading is simulated at the one seam where
    it matters - `_parts_skin` is the last thing the scout reads before it stores - and the real
    carry code then runs. MEASURED here: the replacement's points come back 1000x, and the skin
    still says `rescaled_by: 1000.0` for whatever is placed on it next."""
    from meshpipeline.application import geometry_check as GC

    _states_no_unit(monkeypatch)
    real = GC._parts_skin

    def rescaled_meanwhile(work):
        GC._rescale_skin("abcdef12-unit", 1000.0)      # the naming, landing before the scout stores
        return real(work)
    monkeypatch.setattr(GC, "_parts_skin", rescaled_meanwhile)
    store, _result = _run_check(monkeypatch, tmp_path, SIZES, "abcdef12-unit")
    final = json.loads(store.written["sessions/abcdef12-unit/geometry_check/skin.json"])
    assert final["rescaled_by"] == pytest.approx(1000.0)
    pts = b64f32(final["patches"][0]["points_b64"])
    assert max(abs(v) for v in pts) == pytest.approx(10.0, rel=1e-4), (
        "the first box is 10 mm, so its points stand at 10.0 after the metre re-read, not 0.01")
    # AND SO DOES THE PATCH'S OWN MEASUREMENT. It used to be frozen here as [10.0, 8.0, 3.0] while
    # the same patch's points had just been multiplied by a thousand: a drawing saying 10.0 mm
    # about a part its own points put 10 m across. Nothing renders it yet (partsLegend draws the
    # name and the colour), which is the only reason the stale number was harmless.
    assert final["patches"][0]["size_mm"] == [10000.0, 8000.0, 3000.0]
    assert final["patches"][0]["volume_mm3"] == pytest.approx(240.0 * 1000 ** 3)


def test_a_file_that_states_its_unit_never_reads_the_stored_skin_back(monkeypatch, tmp_path):
    """WHAT THE CARRY MUST NOT COST. The carry above used to be read by fetching the whole stored
    skin.json back through the object store, on EVERY multi-solid CAD scout, for one float that is
    1.0 unless the unit was re-settled. ColdPlateAssembly_CLEAN.step is 15,883,539 bytes and 9,226
    faces and its skin is base64 float32 of all of it.

    MEASURED: 0 reads of skin.json across the whole scout for a file that states its unit, against
    1 for the file that does not (the row above), and the replacement still happens."""
    store, result = _run_check(monkeypatch, tmp_path, SIZES, "abcdef12-noread")
    assert result["status"] == "scouted", result
    skin_key = "sessions/abcdef12-noread/geometry_check/skin.json"
    assert [k for k in store.reads if k == skin_key] == []
    assert len(store.skins) == 2 and len(store.skins[1]["patches"]) == 3, "still replaced"


def test_a_solid_that_cannot_be_drawn_keeps_the_whole_part_on_the_stage(monkeypatch, tmp_path):
    """A DRAWING MISSING A SOLID IS NOT SERVED. parts_payload leaves out any solid it could not
    triangulate and names it in `parts_not_drawn`; the welded skin draw_view wrote covers every
    solid in the file. Replacing one with the other showed a three-solid file as two - under a
    camera framed on three-solid bounds, since setSkin does not resetCamera - which is a worse
    answer to "did you read my package" than one shape of everything.

    The undrawable solid is made by dropping one part's triangles in `part_skins`, so the real
    parts_payload computes its own `parts_not_drawn` and the real scout decides on it."""
    from meshpipeline.cad import solid_parts as SP

    real = SP.part_skins

    def one_undrawable(shape, *, path=None):
        parts = real(shape, path=path)
        parts[-1] = {**parts[-1], "triangles": None}
        return parts
    monkeypatch.setattr(SP, "part_skins", one_undrawable)
    store, result = _run_check(monkeypatch, tmp_path, SIZES, "abcdef12-undrawn")
    assert result["status"] == "scouted", result
    # ONE skin version, the preview's welded lump, and it is what the stage fetches
    assert len(store.skins) == 1, [len(s.get("patches") or []) for s in store.skins]
    final = json.loads(store.written["sessions/abcdef12-undrawn/geometry_check/skin.json"])
    assert len(final["patches"]) == 1 and not final.get("parts")
    # ...and nothing claims a parts drawing that is not there, while the note still names the solid
    assert "parts_drawn" not in result["facts"]
    said = [n for n in result["facts"]["notes"] if "could not be drawn separately" in n]
    assert len(said) == 1, result["facts"]["notes"]
    assert "the file holds 3 solids and 1 of them" in said[0], said


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
