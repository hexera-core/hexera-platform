"""The binder property the announced role correction rests on, pinned where the binder lives.

`agents/intake/executor._apply_the_corrections` expresses "this mouth is not a port, it is wall" by REMOVING
the declared port, because `bind_ports` refuses any declaration carrying more than one wall patch - retyping
the port to `wall` would kill the job pre-mesh instead of meshing the mouth as a wall. The correction is only
honest if removing that one declaration does two things and nothing else: the mouth folds into the wall, and
every other port binds exactly where it bound before.

Both are properties of `engines/port_binding.py`, so they are asserted against it and not against a copy of
it. If either stops holding, a confirmation that says "I have set it to wall" starts lying again, and the
test that fails is this one - on the file that changed.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.port_binding import BindError, bind_intake, bind_ports
from meshpipeline.engines.port_binding import DeclaredPatch as DP

#: Three mouths of a shell: two real bores and the end face of an inner body between them.
_T = {"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (1.2, 0.5, 0.5),
      "stls": {"wall": "w.stl", "p_o1": "a.stl", "p_o4": "b.stl", "p_o8": "c.stl"},
      "openings": {"p_o1": {"area": 1256.0e-6, "centroid": (0.0, 0.0, 0.0)},
                   "p_o4": {"area": 2827.0e-6, "centroid": (1.0, 0.0, 0.0)},
                   "p_o8": {"area": 2827.0e-6, "centroid": (0.5, 0.4, 0.0)}}}


def _port(name: str, role: str, area_mm2: float, near_mm: list[float]) -> dict:
    """A submitted port as `_locate_named_ports` leaves it: the measured area and the measured centroid."""
    return {"name": name, "type": role, "area_mm2": area_mm2, "near_mm": near_mm}


_IN = _port("in", "inlet", 1256.0, [0.0, 0.0, 0.0])
_OUT = _port("out", "outlet", 2827.0, [1000.0, 0.0, 0.0])
_OUT2 = _port("out2", "outlet", 2827.0, [500.0, 400.0, 0.0])
_WALL = {"name": "shell", "type": "wall"}


def test_the_mouth_whose_port_was_withdrawn_folds_into_the_wall():
    out, wall, _note = bind_intake(_T, [_IN, _OUT, _WALL])
    assert out["binding"]["folded_into_wall"] == ["p_o8"]
    assert out["folded_stls"] == {"p_o8": "c.stl"}
    assert wall == "shell"


def test_withdrawing_one_port_moves_no_other_binding():
    """The correction's whole claim about the rest of the payload. Same openings, one declaration fewer."""
    before, _w, _n = bind_intake(_T, [_IN, _OUT, _OUT2, _WALL])
    after, _w2, _n2 = bind_intake(_T, [_IN, _OUT, _WALL])
    kept = {"in", "out"}
    assert {k: v for k, v in before["openings"].items() if k in kept} == after["openings"]
    assert [r for r in before["binding"]["ports"] if r["name"] in kept] == after["binding"]["ports"]


def test_the_mouth_is_still_declared_a_port_when_nothing_withdraws_it():
    """The defect's own shape, so this file can fail differently from the two above: with the port still in
    the declaration the mouth carries an outlet boundary condition, which is what the screen used to
    contradict."""
    out, _w, _n = bind_intake(_T, [_IN, _OUT, _OUT2, _WALL])
    assert out["binding"]["folded_into_wall"] == []
    assert {r["name"]: r["engine_key"] for r in out["binding"]["ports"]}["out2"] == "p_o8"


def test_a_second_wall_patch_is_refused_which_is_why_the_port_is_withdrawn_instead():
    """Naming the alternative and proving it is not one. `_apply_the_corrections` cites this refusal."""
    with pytest.raises(BindError) as exc:
        bind_ports([DP.from_intake(_IN), DP.from_intake(_OUT),
                    DP.from_intake({**_OUT2, "type": "wall"}), DP.from_intake(_WALL)], _T)
    assert "exactly one wall patch" in str(exc.value)


@pytest.mark.parametrize("role", ["symmetry", "farfield", "closed_end"])
def test_the_roles_a_payload_cannot_carry_are_refused_outright(role: str):
    """Why a plan that re-reads a mouth as one of these is withdrawn rather than submitted: the binder would
    refuse the whole job pre-mesh, which is a worse outcome than a correction nobody was promised."""
    with pytest.raises(BindError) as exc:
        bind_ports([DP.from_intake(_IN), DP.from_intake(_OUT),
                    DP.from_intake({**_OUT2, "type": role}), DP.from_intake(_WALL)], _T)
    assert "unknown roles" in str(exc.value)


def test_a_retyped_port_binds_to_the_same_mouth_with_the_other_boundary_condition():
    """The other half of the correction: the patch keeps its name, size and location, so only the role moves."""
    out, _w, _n = bind_intake(_T, [_IN, {**_OUT, "type": "inlet"}, _OUT2, _WALL])
    rows = {r["name"]: (r["role"], r["engine_key"]) for r in out["binding"]["ports"]}
    assert rows["out"] == ("inlet", "p_o4")
    assert rows["in"] == ("inlet", "p_o1") and rows["out2"] == ("outlet", "p_o8")
