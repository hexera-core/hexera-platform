# Responsibility: Verify a setup the measured part cannot carry is said before approval - a symmetry
# plane on a whole body, the ground taken for a half model's cut, a reference length a thousand times
# off - and that the half-model rule is the engine's own.
# Boundaries: the pure checker, the engine's symmetry detector, and the submit step with stubbed stores.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from meshpipeline.agents.intake import measured_setup as ms
from meshpipeline.engines.snappy.snappy_runner import detect_symmetry_plane

# real-path files, 2026-10-28 (boxes in metres)
AIRCRAFT = ([-0.3, -4.65, -1.9], [34.08, 4.65, 2.67])        # a whole supersonic aircraft, y-symmetric
HALF_CAR = ([0.0, 0.0, 0.0], [4.2, 0.95, 1.4])                # a half car cut on y = 0, on the ground
HALF_WING = ([-0.1, -0.3, 0.0], [1.4, 0.05, 2.2])             # a half wing cut on z = 0 (root at z = 0)
TURBINE = ([-11.0, -104.9, 0.0], [11.0, 104.9, 268.2])        # stands on z = 0: the base, not a cut


def _ext(*patches, ref=None, dim="3D"):
    args = {"purpose": "external_cfd", "dimensionality": dim,
            "patches": [{"name": n, "type": t} for n, t in patches]}
    if ref is not None:
        args["reference_length_m"] = ref
    return args


def _facts(box):
    return {"bbox_min_m": box[0], "bbox_max_m": box[1]}


@pytest.mark.parametrize("box", [AIRCRAFT, HALF_CAR, HALF_WING, TURBINE,
                                 ([1.0, 1.0, 1.0], [2.0, 3.0, 4.0]), ([-2.0, -1.0, -3.0], [0.0, 1.0, 3.0])])
def test_the_half_model_rule_is_the_engines(box):
    # the engine's detector, with no end caps measured and no flow axis, finds a plane exactly when
    # the intake's rule finds a side
    L = max(h - lo for lo, h in zip(*box, strict=True))
    found = detect_symmetry_plane({"bbox_min": box[0], "bbox_max": box[1], "L": L}, "symmetry")
    assert bool(found) == bool(ms.half_model_sides(*box))


def test_a_symmetry_plane_on_a_whole_body_is_said_before_approval():
    errs = ms.setup_errors(_ext(("aircraft", "wall"), ("farfield", "farfield"), ("symmetry", "symmetry")),
                           _facts(AIRCRAFT), {})
    assert len(errs) == 1 and "whole body" in errs[0] and "'symmetry'" in errs[0]


@pytest.mark.parametrize("box,extra", [(HALF_CAR, [("ground", "wall")]), (HALF_WING, [])])
def test_a_half_model_keeps_its_symmetry_plane(box, extra):
    assert ms.setup_errors(_ext(("body", "wall"), ("farfield", "farfield"), ("symmetry", "symmetry"), *extra),
                           _facts(box), {}) == []


def test_the_ground_is_not_a_half_models_cut():
    errs = ms.setup_errors(_ext(("turbine", "wall"), ("ground", "wall"), ("farfield", "farfield"),
                                ("symmetry", "symmetry")), _facts(TURBINE), {})
    assert len(errs) == 1 and "the bottom it stands on" in errs[0]
    # the same turbine without a ground: its base can carry the plane (the engine's own call)
    assert ms.setup_errors(_ext(("turbine", "wall"), ("farfield", "farfield"), ("symmetry", "symmetry")),
                           _facts(TURBINE), {}) == []


@pytest.mark.parametrize("args,facts", [
    (_ext(("a", "wall"), ("s1", "symmetry"), ("s2", "symmetry")), _facts(AIRCRAFT)),   # a slab: the engine's
    (_ext(("a", "wall"), ("symmetry", "symmetry"), dim="2D"), _facts(AIRCRAFT)),
    (_ext(("a", "wall"), ("symmetry", "symmetry")), {}),                                # nothing measured
    ({"purpose": "internal_cfd", "patches": [{"name": "symmetry", "type": "symmetry"}]}, _facts(AIRCRAFT)),
])
def test_nothing_is_said_without_a_case_or_evidence(args, facts):
    assert ms.setup_errors(args, facts, {}) == []


@pytest.mark.parametrize("ref,said", [(0.079862, True), (79.862, False), (4.0, False), (1.2, False),
                                      (79862.0, True), (0.9, False)])
def test_a_reference_length_a_thousand_times_off_is_asked(ref, said):
    # the NASA body: 79.862 m long, confirmed on the picture as 79862 mm; a chord of 0.9 m is fine
    errs = ms.setup_errors(_ext(("body", "wall"), ("farfield", "farfield"), ref=ref), {},
                           {"size_mm": [79862.31, 17532.34, 15158.16]})
    assert bool(errs) is said
    if said:
        assert "79.8623 m" in errs[0] and "millimetres and metres" in errs[0]


@pytest.mark.asyncio
async def test_the_submit_step_says_it_and_saves_nothing(monkeypatch):
    from meshpipeline.agents.intake import executor as ex
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.contracts import object_storage as osx

    monkeypatch.setattr(gc, "read_check", lambda sid: {"facts": _facts(AIRCRAFT)})

    class _Store:
        def get_bytes(self, object_key):
            return json.dumps({"size_mm": [34380.0, 9304.0, 4569.0]}).encode()
    monkeypatch.setattr(osx, "get_object_store", lambda: _Store())
    monkeypatch.setattr(ex, "validate_submission", lambda args: [])
    tool = ex.IntakeToolExecutor.__new__(ex.IntakeToolExecutor)
    tool.state = SimpleNamespace(session_id="s-1", submit_attempts=0, recommended_this_turn=False,
                                 submit_rejections=0, val_errors_seen=[], pending={}, owner_id="o",
                                 revision="r", selection={})
    tool._trace = None
    tool._job_id = "j"
    args = _ext(("aircraft", "wall"), ("farfield", "farfield"), ("symmetry", "symmetry"), ref=34.38)
    out = await tool._do_submit_requirements(args)
    assert out.accepted is False and "whole body" in out.content
    assert tool.state.submit_rejections == 1
