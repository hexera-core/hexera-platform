# Responsibility: Verify a resumed thread continues from its checkpoint, refreshing geometry without resetting position.
from __future__ import annotations

import pytest

from meshpipeline.application.fenced_checkpointer import (
    Continuation,
    disposition_of,
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


def test_an_unstarted_thread_starts_fresh_from_this_process_state():
    # The thread holds only a DEAD process's copy of the input, with that process's local
    # geometry path in it. This process's state, with the geometry it fetched, is the input.
    c = plan_continuation("unstarted", STATE, _Materialized())
    assert c.is_fresh and c.graph_input == STATE and not c.refresh_geometry


# reading the thread

class _Snap:
    def __init__(self, *, source, next_=(), created_at="2026-09-28T00:00:00+00:00"):
        self.metadata = {"source": source, "step": -1 if source == "input" else 3}
        self.next = next_
        self.created_at = created_at


def test_no_checkpoint_is_absent():
    assert disposition_of(None) == "absent"


def test_a_snapshot_with_no_saved_checkpoint_is_absent():
    assert disposition_of(_Snap(source="loop", next_=("b",), created_at=None)) == "absent"


@pytest.mark.parametrize("next_", [("__start__",), ()], ids=["nothing_else_saved",
                                                               "start_writes_saved"])
def test_only_the_input_saved_is_unstarted_whatever_next_says(next_):
    # The two shapes a crash between the input checkpoint and the first loop checkpoint leaves.
    # ('__start__',) used to read as pending - and the geometry refresh then raised "Ambiguous
    # update" - and () used to read as COMPLETE, so the restart ran on the dead process's file.
    assert disposition_of(_Snap(source="input", next_=next_)) == "unstarted"


def test_a_saved_step_with_work_left_is_pending():
    assert disposition_of(_Snap(source="loop", next_=("b",))) == "pending"


def test_a_geometry_refresh_is_still_a_pending_position():
    # enter_graph's own refresh writes an `update` checkpoint; a crash right after it must still
    # resume from the saved position rather than start again.
    assert disposition_of(_Snap(source="update", next_=("b",))) == "pending"


def test_a_finished_graph_is_complete():
    assert disposition_of(_Snap(source="loop", next_=())) == "complete"


def test_a_snapshot_without_metadata_is_read_by_its_position():
    snap = _Snap(source="loop", next_=("b",))
    snap.metadata = None
    assert disposition_of(snap) == "pending"


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


async def test_an_unstarted_thread_starts_again_without_touching_the_checkpoint():
    # NO geometry refresh here: aupdate_state on an input-only thread is exactly the call that
    # raised "Ambiguous update" - the new input carries this process's geometry instead.
    g, log = _Graph(), _Log()
    mat = _Materialized()
    out = await enter_graph(g, CFG, plan_continuation("unstarted", STATE, mat), mat,
                            job_id="j", generation=4, jlog=log)
    assert out == STATE and g.updates == []
    assert any(4 in a for a in log.infos), "the start-again did not record its generation"


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
