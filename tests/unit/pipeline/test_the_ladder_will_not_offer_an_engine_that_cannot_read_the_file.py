"""An engine that cannot read the upload is not a rung on the ladder.

WHY THIS EXISTS, measured on the coupon plate end to end on 2026-10-29. The conjugate engine
crashed, and the fallback said:

    node_engine_fallback: cfmesh_conjugate could not mesh this shape (engine: engine_crashed) -
    attempt 2 moves to snapgrid, which delivers the same approved mesh

snapgrid reads ECXML and nothing else - it meshes the model the file describes, its parts and
their materials and positions - and the upload was a STEP. Attempt 2 ended the job with

    ValueError: no ECXML model beside source.step - the snap-grid mesher reads the ECXML file
    itself

`EngineSpec.admit` has refused this since snapgrid was written, and `ladder` calls it. The check
only fires when the facts NAME the upload's format, and that name lived in `surface_analysis`,
which comes from STAGING the surface - and staging is precisely what the wrong format breaks.
snapgrid's `tessellate_to_stl` threw inside `prepare_surface`; `geometry_admission` swallowed it
("a staging hiccup is a system issue - defer, never reject"); the run reached the builder and
threw again, as a crash rather than a refusal.

The format is a property of the FILE. `source_format` reads it off the snapshot's own name, so
the check fires before anything is staged.
"""
import pytest

from meshpipeline.pipeline.engine_fallback import ladder, source_format

REF = {"source_id": "s1", "owner_id": "dev-user", "interpretation_id": "unconfirmed",
       "original_filename": "plate.step", "suffix_hint": ".step"}


def _state(**geom):
    s = {
        "purpose": "conjugate_heat_transfer",
        "engine": "cfmesh_conjugate",
        "approved_engine": "cfmesh_conjugate",
        "input_kind": "solid-assembly",
        "dimensionality": "3D",
        "intake_patches": [{"name": "inlet", "type": "inlet"},
                           {"name": "outlet", "type": "outlet"}],
        "geometry": {"path": "/srv/workspaces/x/source.step", "ref": dict(REF)},
        "request_txt": "a cold plate, conjugate heat transfer",
    }
    s["geometry"].update(geom)
    return s


def _engines(state):
    return [r.engine for r in ladder(state)]


#   -- the format, read off the file's own name -------------------------------------------------

def test_the_format_comes_off_the_upload_s_name():
    assert source_format(_state()) == "step"


def test_the_suffix_hint_is_read_when_the_name_has_none():
    s = _state(ref={**REF, "original_filename": "plate", "suffix_hint": ".ecxml"})
    assert source_format(s) == "ecxml"


def test_the_materialised_path_is_the_last_resort():
    s = _state(ref={**REF, "original_filename": "", "suffix_hint": ""})
    assert source_format(s) == "step", "the staged path still ends .step"


@pytest.mark.parametrize("geom", [
    {}, {"geometry": None}, {"ref": None}, {"ref": {}}, {"path": ""},
])
def test_an_unreadable_snapshot_says_nothing_rather_than_raising(geom):
    """NOTHING HERE MAY RAISE. This runs after an engine has already failed, so an exception out
    of it takes the whole fallback with it - and GeometrySourceRef.from_payload raises
    GeometrySourceError on a partial snapshot ("missing 'object_key'"), which is why the name is
    read off the raw mapping instead of through the contract objects.
    """
    s = _state()
    if "geometry" in geom:
        s["geometry"] = geom["geometry"]
    else:
        s["geometry"].update(geom)
    assert isinstance(source_format(s), str)


def test_a_state_with_no_geometry_at_all_says_nothing():
    s = _state()
    s.pop("geometry")
    assert source_format(s) == ""


#   -- and the ladder uses it -------------------------------------------------------------------

def test_snapgrid_is_not_a_rung_for_a_step():
    got = _engines(_state())
    assert "cfmesh_conjugate" in got, "the approved engine is always the first rung"
    assert "snapgrid" not in got, "it reads only ECXML"


def test_snapgrid_is_still_a_rung_for_an_ecxml():
    """The check must not simply remove it: an ECXML model is the file it is for."""
    s = _state(ref={**REF, "original_filename": "chip.ecxml", "suffix_hint": ".ecxml"})
    assert "snapgrid" in _engines(s)


def test_an_engine_that_reads_every_format_is_untouched():
    """Only engines that DECLARE a format list are judged on it; the rest say nothing, which is
    every engine but the snap grid."""
    assert "snappy_multiregion" in _engines(_state())


def test_an_unknown_format_changes_nothing():
    """An older run whose snapshot carries no readable name must ladder exactly as it did."""
    s = _state(path="", ref={**REF, "original_filename": "", "suffix_hint": ""})
    assert source_format(s) == ""
    assert "snapgrid" in _engines(s), "nothing is refused on a fact nobody has"


def test_the_approved_engine_is_a_rung_whatever_the_format():
    """The user approved it. A ladder that dropped its own first rung would have nothing to run."""
    s = _state()
    s["approved_engine"] = "snapgrid"
    s["engine"] = "snapgrid"
    assert _engines(s)[0] == "snapgrid"
