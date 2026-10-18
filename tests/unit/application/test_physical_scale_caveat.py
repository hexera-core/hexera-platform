# Responsibility: Verify a delivered mesh that is no believable size for what it is (a car 4.7 mm
# long, an aircraft 1 mm long - both triangle files that state no unit) is delivered with the
# concern said and the likely unit named, never as a clean pass - and that a believable mesh, or
# one the executor did not validate, carries no such note.
# Boundaries: final_result's predicate and its words; the manifest is a plain dict.
from __future__ import annotations

from meshpipeline.application.final_result import (
    PHYSICAL_SCALE,
    FinalResult,
    RunOutcome,
    delivered_size_m,
    physical_scale_caveat,
    render_message,
)


def _manifest(body, flow="external"):
    x, y, z = body
    return {"mesh_units": "m", "flow_topology": flow,
            "geometry": {"box_xmin": -10, "box_xmax": 10, "box_ymin": -10, "box_ymax": 10,
                         "box_zmin": 0, "box_zmax": 10,
                         "body_box": {"xmin": 0, "xmax": x, "ymin": 0, "ymax": y, "zmin": 0, "zmax": z}}}


def test_a_car_meshed_4_7_mm_long_is_delivered_with_the_unit_named():
    oc = RunOutcome(executor_success=True, flow_topology="external",
                    body_size_m=delivered_size_m(_manifest((0.0047, 0.002, 0.0014))),
                    words="External aerodynamics around the car in air at sea level", unit="mm")
    c = physical_scale_caveat(oc)
    assert c is not None and c["kind"] == PHYSICAL_SCALE and c["other_unit"] == "m"
    assert "4.7 mm long" in c["words"] and "in metres it would be 4.7 m long" in c["words"]


def test_the_shape_speaks_when_the_request_names_nothing():
    # "the body" - no word for it; on the ground, 2.3 times as long as wide: a car-like body
    oc = RunOutcome(executor_success=True, flow_topology="external", body_size_m=(0.0047, 0.002, 0.0014),
                    words="External aerodynamics around the body", unit="mm", grounded=True)
    c = physical_scale_caveat(oc)
    assert c is not None and "car-like body on the ground" in c["words"]
    # a blended wing 1 mm long in the free stream
    oc = RunOutcome(executor_success=True, flow_topology="external", body_size_m=(0.001, 0.001, 0.00018),
                    words="", unit="mm")
    assert physical_scale_caveat(oc) is not None


def test_a_believable_mesh_or_an_unvalidated_one_carries_no_note():
    ok = RunOutcome(executor_success=True, flow_topology="external", body_size_m=(4.7, 2.0, 1.4),
                    words="air over the car", unit="m")
    assert physical_scale_caveat(ok) is None
    assert physical_scale_caveat(RunOutcome(executor_success=True, flow_topology="internal",
                                            body_size_m=(0.4, 0.1, 0.1), unit="mm")) is None
    bad = RunOutcome(executor_success=False, flow_topology="external", body_size_m=(0.0047, 0.002, 0.0014))
    assert physical_scale_caveat(bad) is None
    assert physical_scale_caveat(RunOutcome(executor_success=True, failed_gate="x",
                                            body_size_m=(0.0047, 0.002, 0.0014))) is None
    assert physical_scale_caveat(RunOutcome(executor_success=True, body_size_m=())) is None


def test_the_size_comes_from_the_body_box_or_an_internal_meshs_own_bounds():
    assert delivered_size_m(_manifest((1.0, 2.0, 3.0))) == (1.0, 2.0, 3.0)
    m = _manifest((1, 1, 1), "internal")
    del m["geometry"]["body_box"]
    assert delivered_size_m(m, "internal") == (20.0, 20.0, 10.0)
    assert delivered_size_m(m, "external") == ()           # an external mesh's bounds are its far field
    assert delivered_size_m({}, "internal") == () and delivered_size_m(None) == ()


def test_the_graph_state_carries_the_facts_the_note_needs():
    state = {"executor_success": True, "flow_topology": "external", "domain": "car aero",
             "request_txt": "External aerodynamics around the car", "mesh_manifest": _manifest((0.0047, 0.002, 0.0014)),
             "geometry": {"ref": {}, "interpretation": {"unit": "mm"}},
             "intake_patches": [{"name": "body", "type": "wall"}, {"name": "ground", "type": "wall"}]}
    oc = RunOutcome.from_graph_state(state)
    assert oc.unit == "mm" and oc.grounded and "car" in oc.words and len(oc.body_size_m) == 3
    assert physical_scale_caveat(oc) is not None


def test_the_delivery_message_says_it_first():
    from meshpipeline.application.final_result import FINAL_RESULT_SCHEMA_VERSION, TerminalStatus

    c = physical_scale_caveat(RunOutcome(executor_success=True, flow_topology="external",
                                         body_size_m=(0.0047, 0.002, 0.0014), words="the car", unit="mm"))
    fr = FinalResult(schema_version=FINAL_RESULT_SCHEMA_VERSION, job_id="j", owner_id="o",
                     status=TerminalStatus.succeeded, engine="snappy", purpose="external_cfd",
                     executor_success=True, requirement_caveats=[c])
    text = render_message(fr)
    assert text.startswith("Delivered with a concern about its size:")
    assert "4.7 mm long" in text and "say which one the file is in" in text
