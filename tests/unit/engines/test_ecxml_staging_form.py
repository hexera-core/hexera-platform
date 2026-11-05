# Responsibility: Pin that a run judges an ECXML upload's form as the upload it is, not as the placed-parts staging the snap-grid mesher gets.
# Boundaries: pure reads of the capability helpers and the declared form gate; nothing is staged or meshed.
from __future__ import annotations

from meshpipeline.engines.capability import geometry_form_of_state
from meshpipeline.engines.registry import get_spec
from meshpipeline.pipeline.geometry_admission import _form_refusal

# THE DEFECT. The snap-grid mesher reads the ECXML itself, so the run stages the upload as its
# PLACED parts, written as geometry.stl. The form gate read that staging and called the job "a
# surface mesh (STL or a similar triangle file)", which snapgrid's own spec (multi-region from a CAD
# solid) refuses - every snap-grid job failed in seconds, before anything was built (dev-areen
# set_top_box job 7922fe65, 2026-10-11).


def _state(local_path: str, suffix: str, name: str, purpose: str = "conjugate_heat_transfer") -> dict:
    return {"purpose": purpose, "input_kind": "solid-assembly",
            "geometry": {"local_path": local_path,
                         "ref": {"suffix_hint": suffix, "original_filename": name}}}


def test_a_placed_ecxml_staging_is_the_cad_model_it_came_from():
    st = _state("/srv/workspaces/j/geometry/geometry.stl", ".ecxml", "set_top_box.ecxml")
    assert geometry_form_of_state(st) == "cad"


def test_the_fused_staging_other_engines_get_is_still_cad():
    st = _state("/srv/workspaces/j/geometry/geometry.step", ".ecxml", "set_top_box.ecxml")
    assert geometry_form_of_state(st) == "cad"


def test_a_triangle_upload_is_still_the_surface_it_is():
    assert geometry_form_of_state(_state("/w/geometry.stl", ".stl", "box.stl")) == "surface"
    assert geometry_form_of_state(_state("/w/geometry.step", ".step", "box.step")) == "cad"


def test_snapgrid_takes_its_own_placed_staging():
    st = _state("/srv/workspaces/j/geometry/geometry.stl", ".ecxml", "set_top_box.ecxml")
    assert _form_refusal(st, get_spec("snapgrid")) is None


def test_snapgrid_still_refuses_a_triangle_upload_by_its_form():
    st = _state("/w/geometry.stl", ".stl", "board.stl")
    refusal = _form_refusal(st, get_spec("snapgrid"))
    assert refusal is not None and refusal.code == "geometry_form_unsupported"
