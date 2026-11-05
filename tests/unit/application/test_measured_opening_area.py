"""An opening that is no circle is declared by the area the check measured, not by its box: the
scout, the staging and the port binding then judge one opening by one number (the aorta root
measured 922 mm2 and was declared as its 37.6 x 34.3 mm box, 1,290 mm2)."""
from __future__ import annotations

from meshpipeline.application.geometry_confirmation import body_of, patches_from
from meshpipeline.cad.scout import Opening


def _opening(**kw) -> Opening:
    base = {"face_index": 0, "kind": "disc", "centroid": (0.0, 0.0, 0.0), "normal": (0.0, 0.0, 1.0),
            "area": 921.8e-6, "wh": (0.0376, 0.0343), "clear_ahead": True, "on_extremity": True}
    base.update(kw)
    return Opening(**base)


def test_the_proposal_carries_the_measured_area_of_a_mouth_that_is_no_circle():
    o = _opening().as_dict()
    assert o["shape"] == "other" and o["flow_area_mm2"] == 921.8
    assert "flow_area_mm2" not in _opening(area=3.14159e-4 * 4.0, wh=(0.04, 0.04)).as_dict()


def test_the_declaration_sends_that_area_instead_of_the_box():
    o = {**_opening().as_dict(), "id": 1, "name": "inlet", "role": "inlet"}
    out = {**_opening(centroid=(0.1, 0, 0)).as_dict(), "id": 2, "name": "outlet", "role": "outlet"}
    body = body_of({"flow": "internal", "input_kind": "fluid-domain", "openings": [o, out]})
    inlet = patches_from(body)[0]
    assert inlet["area_mm2"] == 921.8 and "width_mm" not in inlet and "diameter_mm" not in inlet
