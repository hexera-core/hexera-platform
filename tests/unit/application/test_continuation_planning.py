# Responsibility: Verify a resumed thread continues from its checkpoint, refreshing geometry without resetting position.
from __future__ import annotations

import pytest

from meshpipeline.application.fenced_checkpointer import (
    Continuation,
    enter_graph,
    plan_continuation,
)


class _Log:
    def __init__(self): self.infos = []
    def info(self, *a, **k): self.infos.append(a)
    def warning(self, *a, **k): pass


class _Materialized:
    def to_state(self): return {"local_path": "/w/geom.step", "source_id": "src-1"}


class _Graph:
    def __init__(self): self.updates = []
    async def aupdate_state(self, cfg, values): self.updates.append((cfg, values))


STATE = {"job_id": "j", "engine": "cfmesh"}
CFG = {"configurable": {"thread_id": "j:s1:g2"}}


# the decision

def test_an_absent_thread_starts_fresh_from_the_initial_state():
    c = plan_continuation("absent", STATE, None)
    assert c.is_fresh and c.graph_input == STATE and not c.refresh_geometry


@pytest.mark.parametrize("disposition", ["pending", "complete"])
def test_a_durable_thread_never_receives_the_initial_state(disposition):
    c = plan_continuation(disposition, STATE, _Materialized())
    assert c.graph_input is None, (
        f"a {disposition} thread was handed the initial state - the graph would restart from START")
    assert not c.is_fresh


def test_a_pending_thread_refreshes_a_process_local_geometry_handle():
    c = plan_continuation("pending", STATE, _Materialized())
    assert c.refresh_geometry is True


def test_a_pending_thread_without_new_geometry_refreshes_nothing():
    assert plan_continuation("pending", STATE, None).refresh_geometry is False


def test_a_complete_thread_never_refreshes_geometry():
    assert plan_continuation("complete", STATE, _Materialized()).refresh_geometry is False


def test_an_unknown_disposition_is_treated_as_a_continuation_not_a_restart():
    assert plan_continuation("something-new", STATE, None).graph_input is None


# applying it

async def test_a_fresh_run_hands_over_the_state_and_touches_no_checkpoint():
    g = _Graph()
    out = await enter_graph(g, CFG, plan_continuation("absent", STATE, None), None,
                            job_id="j", generation=2, jlog=_Log())
    assert out == STATE and g.updates == [], "a fresh run wrote to the checkpoint"


async def test_a_pending_resume_substitutes_geometry_without_resetting_position():
    g = _Graph()
    mat = _Materialized()
    out = await enter_graph(g, CFG, plan_continuation("pending", STATE, mat), mat,
                            job_id="j", generation=2, jlog=_Log())
    assert out is None, "the resume was turned into a restart"
    assert len(g.updates) == 1
    cfg, values = g.updates[0]
    assert cfg == CFG, "the update did not target the run's own thread"
    assert set(values) == {"geometry"}, (
        f"the resume wrote more than the geometry handle: {sorted(values)} - the saved channel "
        f"state and pending task list must survive untouched")


async def test_a_complete_thread_runs_no_node_and_writes_nothing():
    g = _Graph()
    out = await enter_graph(g, CFG, plan_continuation("complete", STATE, _Materialized()),
                            _Materialized(), job_id="j", generation=2, jlog=_Log())
    assert out is None and g.updates == []


async def test_a_pending_resume_without_geometry_writes_nothing():
    g = _Graph()
    out = await enter_graph(g, CFG, plan_continuation("pending", STATE, None), None,
                            job_id="j", generation=2, jlog=_Log())
    assert out is None and g.updates == []


async def test_the_resume_is_logged_with_its_generation():
    log = _Log()
    await enter_graph(_Graph(), CFG, plan_continuation("pending", STATE, None), None,
                      job_id="j", generation=7, jlog=log)
    assert any(7 in a for a in log.infos), "the resume did not record which generation resumed"


# the fenced write

def test_the_geometry_refresh_travels_through_the_fenced_checkpointer():
    import inspect

    from meshpipeline.application import fenced_checkpointer as fc

    assert "aupdate_state" in inspect.getsource(fc.enter_graph)
    assert hasattr(fc, "FencedCheckpointer")
    # the run wires the graph with the fenced saver, so aupdate_state is fenced by construction
    import meshpipeline.application.pipeline_run as pr
    assert "FencedCheckpointer" in inspect.getsource(pr._run_async)


def test_the_orchestrator_no_longer_interprets_the_disposition():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert '"absent"' not in src and '"pending"' not in src, (
        "the run interprets the checkpoint disposition itself again")
    assert "aupdate_state" not in src, "the run writes the geometry substitution itself again"
    assert "plan_continuation" in src and "enter_graph" in src


def test_a_continuation_is_an_immutable_value():
    c = plan_continuation("pending", STATE, None)
    assert isinstance(c, tuple)
    with pytest.raises(AttributeError):
        c.graph_input = STATE          # type: ignore[misc]
    assert isinstance(Continuation(None, False, "x"), tuple)
