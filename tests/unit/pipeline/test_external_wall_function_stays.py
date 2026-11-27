"""CFDREQ-27 (rulebook 98): the wall-function layer gate never moves an EXTERNAL run to another engine. Once cfMesh
refined the external wake (CFDREQ-28) it counted as the same external mesh, and the gate sent abc 00000646_002 to it
for 3.8 minutes and the same mesh delivered. The layer stack's own gate (CFDREQ-28) still moves an external run, and
an internal run still moves on CFDREQ-27."""
from __future__ import annotations

from tests.unit.pipeline.test_engine_fallback_ladder import _external

from meshpipeline.pipeline import engine_fallback as lad


def _refused(gate: str, **kw) -> dict:
    return _external(engine="snappy", retry_count=0, executor_failed_gate=gate,
                     executor_failure_cause="mesh_quality", **kw)


def test_the_wall_function_gate_keeps_an_external_run_on_its_engine():
    st = _refused("wall_function_layer", executor_failure_facts={"under_jump_pct": 71.1})
    assert lad.stays_on_external(st)
    d = lad.decide(st, record=lad.with_attempt(st, lad.classify(st)))
    assert not d.switch_to
    assert not lad.another_engine_takes_over(st)


def test_the_layer_stack_gate_still_moves_an_external_run_to_cfmesh():
    st = _refused("layer_stack", executor_failure_facts={"coverage_pct": 61.8, "layers": 10})
    assert not lad.stays_on_external(st)
    d = lad.decide(st, record=lad.with_attempt(st, lad.classify(st)))
    assert d.switch_to == "cfmesh"


def test_an_internal_run_is_not_held_by_the_external_rule():
    st = _refused("wall_function_layer", purpose="internal_cfd")
    assert not lad.stays_on_external(st)
    assert not lad.stays_on_external({**_refused("wall_function_layer"), "executor_success": True})
