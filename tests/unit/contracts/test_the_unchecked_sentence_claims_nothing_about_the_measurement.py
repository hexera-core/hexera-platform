# Responsibility: Verify the intake completion sentence never claims the geometry went unmeasured.
# Boundaries: the sentence only. Whether a binding happened is agents/intake/geometry_brief.py's.
from __future__ import annotations

import ast
import pathlib

from meshpipeline.agents.intake import geometry_brief as gb
from meshpipeline.contracts import rationale as R

SOURCE = pathlib.Path(R.__file__).read_text(encoding="utf-8")

# The measured tee-wye from the corpus, in the shape the stored document carries. It is here to make
# the point of the whole file: this measurement SUCCEEDED, so nothing downstream of it may tell the
# customer their geometry was not measured.
DOCUMENT = {
    "schema": "geometry_agent.measurement.v1", "status": "ok", "reason": "",
    "coordinates": {"unit": "mm", "basis": "occ_transfer", "scale_to_metres": 0.001},
    "bbox": {"extent_mm": [2069.69, 1565.33, 388.14], "diagonal_m": 2.6237},
    "bodies": {"count": 1, "watertight": True},
    "openings": [
        {"id": "o1", "kind": "ring", "shape": "circle", "planar": True,
         "bore_diameter_m": 0.35694, "min_dimension_m": 0.35694,
         "centroid_m": [0.0, 0.0, 0.0], "normal": [-1.0, 0.0, 0.0], "tilt_deg": 0.0},
        {"id": "o2", "kind": "ring", "shape": "circle", "planar": True,
         "bore_diameter_m": 0.30113, "min_dimension_m": 0.30113,
         "centroid_m": [1.99521, -0.63415, 0.0], "normal": [0.894, -0.448, 0.0],
         "tilt_deg": 26.636},
    ],
}


def _because(*, patches, document=DOCUMENT) -> str:
    """The sentence the customer reads, through the real binding rather than a hand-set bool."""
    said: list = []

    class _Recorder:
        def rationale(self, conclusion, because=""):
            said.append((conclusion, because))

    bound = gb.bind_patches(document, patches)
    R.intake_requirements_finalized(_Recorder(), patches=len(patches), dimensionality="3D",
                                    geometry_checked=bool(bound.get("checked")))
    assert len(said) == 1
    return said[0][1]


def test_the_farfield_job_is_not_told_its_geometry_went_unmeasured():
    """THE DEFECT. An external-CFD farfield case declares no inlet and no outlet - the body sits in
    a wind tunnel and the farfield is constructed, not declared - so `bind_patches` returns
    `checked=False` with a perfectly good measurement in hand. The sentence said "the geometry
    itself was not measured" on every one of those jobs, while the opening table from that same
    measurement was in the same conversation."""
    because = _because(patches=[{"name": "body", "type": "wall"},
                                {"name": "farfield", "type": "wall"}])
    assert "was not measured" not in because
    assert "not measured" not in because
    # what it may say instead: it was not CONFIRMED against the measurement, which is true here
    assert "not confirmed against the measured geometry" in because
    assert "the ones you stated" in because


def test_no_declared_port_bound_and_the_sentence_still_blames_no_one():
    """A declared port 9 metres from any opening binds to nothing, so this is the same `False` from
    a different cause. The sentence may not name the cause it guessed - it is handed a bool."""
    because = _because(patches=[{"name": "inlet", "type": "inlet",
                                 "near_mm": [9000.0, 9000.0, 9000.0], "diameter_mm": 357.24}])
    assert "not measured" not in because
    assert "not confirmed against the measured geometry" in because


def test_a_partly_bound_brief_is_not_told_that_nothing_matched():
    """THE FIX IN THE OTHER DIRECTION IS ALSO A LIE. Here the inlet DOES bind and one outlet does
    not, and `bind_patches` refuses the whole check because a partial check is not a check. A
    sentence that said "no port roles were declared", or that none matched, would be false on this
    job - so the claim stays set-level."""
    because = _because(patches=[
        {"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0], "diameter_mm": 357.24},
        {"name": "outlet_far", "type": "outlet", "near_mm": [9000.0, 0.0, 0.0],
         "diameter_mm": 301.38}])
    for lie in ("not measured", "no port roles", "none of them", "no boundary assignment",
                "you declared no"):
        assert lie not in because, because


def test_the_true_sentence_still_arrives_when_every_declared_port_bound():
    because = _because(patches=[
        {"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0], "diameter_mm": 357.24},
        {"name": "outlet", "type": "outlet", "near_mm": [1995.21, -634.15, 0.0],
         "diameter_mm": 301.38}])
    assert "checked against the measured geometry and the selected engine" in because


def test_no_string_in_this_module_makes_a_claim_about_the_measurement_running():
    """The blind spot this file exists for: the module is handed one bool and four situations
    collapse into it, so no sentence here may report WHICH of them happened - not the one template
    the tests above render, and not a second one added next to it.

    IT READS THE PARSED CONSTANTS, NOT THE SOURCE TEXT, and the first version of this check did the
    opposite and passed against the defect it was written for. The sentence was spelled
    `"...the geometry itself was not "` `"measured, so..."` across two source lines, so the substring
    "was not measured" was nowhere in the file while the string the customer read contained it.
    Python joins adjacent literals into ONE ast.Constant, so the parsed value is the sentence as
    published; the long comment above, which quotes the old lie deliberately, is not a Constant and
    is correctly ignored."""
    offenders = []
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        low = node.value.lower()
        for lie in ("was not measured", "not measured", "nothing was measured",
                    "no measurement was"):
            if lie in low:
                offenders.append((node.lineno, node.value))
    assert offenders == [], offenders
