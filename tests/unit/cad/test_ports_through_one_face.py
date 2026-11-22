# Responsibility: Verify INS-25 (cad_tessellate.held_to_declaration): a located declared port that no staged flat-face
# opening comes within NO_FACE_NEAR_WIDTHS of its own width - several ports through one flat face, staged as one
# opening at most - raises FlatFacesMissPort so the part's surface is staged; two openings both near a port are still
# the binder's to settle, and a port with an opening of its own binds as before.
# Boundaries: the staged-openings records only (no CAD, no mesher).
from __future__ import annotations

import pytest

from meshpipeline.cad import cad_tessellate
from meshpipeline.cad.cad_tessellate import FlatFacesMissPort, held_to_declaration

NO_FACE_NEAR_WIDTHS = getattr(cad_tessellate, "NO_FACE_NEAR_WIDTHS", 2.0)
BOX = ([-0.1, -0.03, 0.0], [0.1, 0.03, 0.05])


def _rec(area_mm2, c_mm):
    return {"area": area_mm2 * 1e-6, "centroid": [v / 1000.0 for v in c_mm]}


def _port(name, d_mm, c_mm):
    import math
    return {"name": name, "area_m2": math.pi / 4.0 * d_mm * d_mm * 1e-6, "d_m": d_mm / 1000.0,
            "near_m": tuple(v / 1000.0 for v in c_mm)}


def _manifold_openings():
    # what the B-rep staging found on a manifold block (70969's kind): the side inlet, two of the top holes (one per
    # face it took) and the two end holes - not the other two holes of the top face
    return {"inlet": _rec(400.0, (0, 0, 0)), "outlet_1": _rec(78.5, (-20, -20, 50)),
            "outlet_2": _rec(78.5, (20, 20, 50)), "outlet_3": _rec(78.5, (-60, 0, 25)),
            "outlet_4": _rec(78.5, (60, 0, 25))}


def test_a_port_through_a_face_already_taken_stages_the_surface():
    """outlet_3's hole is in the top face beside outlet_1's: the nearest staged opening left is an end hole 51 mm
    (5 of its widths) away and the next 86 mm - not decisive, and nothing near. Old code returned quietly and the
    binder refused 'does not decisively pick one opening'."""
    declared = [_port("inlet", 22.6, (0, 0, 0)), _port("outlet_1", 10.0, (-20, -20, 50)),
                _port("outlet_2", 10.0, (20, 20, 50)), _port("outlet_3", 10.0, (-20, 20, 50)),
                _port("outlet_4", 10.0, (20, -20, 50))]
    with pytest.raises(FlatFacesMissPort, match="INS-25"):
        held_to_declaration(_manifold_openings(), declared, *BOX)


def test_two_openings_near_a_port_are_the_binders_to_settle():
    """A port standing between two staged openings 8 mm apart (within NO_FACE_NEAR_WIDTHS of its width): not
    decisive, but something is near - the binder says it, unchanged."""
    openings = {"inlet": _rec(400.0, (0, 0, 0)), "outlet_1": _rec(78.5, (-4, 0, 50)),
                "outlet_2": _rec(78.5, (4, 0, 50))}
    declared = [_port("inlet", 22.6, (0, 0, 0)), _port("outlet_1", 10.0, (0, 0, 50)),
                _port("outlet_2", 10.0, (0, 0, 50))]
    assert 4.0 < NO_FACE_NEAR_WIDTHS * 10.0
    held_to_declaration(openings, declared, *BOX)       # no raise


def test_ports_with_openings_of_their_own_bind_as_before():
    declared = [_port("inlet", 22.6, (0, 0, 0)), _port("outlet_1", 10.0, (-20, -20, 50)),
                _port("outlet_2", 10.0, (20, 20, 50)), _port("outlet_3", 10.0, (-60, 0, 25)),
                _port("outlet_4", 10.0, (60, 0, 25))]
    held_to_declaration(_manifold_openings(), declared, *BOX)
