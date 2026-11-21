# Responsibility: Verify a wall treatment the layers did not reach everywhere is said plainly beside the delivered mesh:
# which treatment, how much of the wall, and the first cell where the layers did not grow (rulebook 75).
# Boundaries: the verdict inputs built from a manifest's quality record; no mesh is read.
from __future__ import annotations

from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import verdict_limits as vl


def _quality(coverage: float, delivered: str) -> dict:
    return {"layer_coverage_pct": coverage, "max_non_ortho": 60.0, "skew_fraction": 0.0, "regions": 1,
            "surface_deviation": {"cell_size_m": 0.0049},
            "requirements": {"rows": [{"id": "c1", "field": "layers:y_plus", "asked": "y+ of about 1",
                                       "applied": True, "met": True, "delivered": delivered}]}}


def _row(quality: dict) -> dict:
    inputs = vl.inputs_for(engine="snappy", topology="external", manifest={"quality": quality}, valid=True,
                           layers_planned=True)
    return next(r for r in inputs["limits"] if r["key"] == "layer_coverage_pct")


def test_a_resolved_wall_with_partial_layers_is_said_plainly():
    q = _quality(13.4, "a resolved wall: first layer 0.03382 mm high, 18 layers growing 1.2, so the first "
                       "cell's centre sits at y+ about 0.5")
    row = _row(q)
    assert vl.words(row) == ("The resolved wall (y+ about 1) has its layers on 13% of the wall; elsewhere the "
                             "first cell is the wall's own cell, about 4.9 mm, so y+ there is well above 1 (we aim "
                             "for layers on at least 80%)")
    inputs = vl.inputs_for(engine="snappy", topology="external", manifest={"quality": q}, valid=True,
                           layers_planned=True)
    assert dr.verdict(inputs)[0] == "concerns", "partial layers are never a clean pass"


def test_wall_functions_with_partial_layers_get_their_own_line_and_full_coverage_none():
    q = _quality(40.0, "wall functions: first layer 1.482 mm high, 1 layer")
    assert vl.words(_row(q)).startswith("The wall-function layers are on 40% of the wall; elsewhere the first "
                                        "cell is the wall's own cell, about 4.9 mm")
    assert "said" not in _row(_quality(96.0, "a resolved wall: first layer 0.03 mm"))
    # no wall treatment on record: the generic row stands
    q = _quality(13.4, "")
    q["requirements"] = {}
    assert "said" not in _row(q)
