# Responsibility: Verify a pinned engine is written before the graph and never rewritten by a resume or a purpose.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

import pytest

from meshpipeline.engines.registry import engine_names
from meshpipeline.pipeline.state_factory import pin_selected_engine


class _Log:
    def __init__(self): self.warnings = []
    def warning(self, *a, **k): self.warnings.append(a)
    def info(self, *a, **k): pass


# the pin

@pytest.mark.parametrize("engine", sorted(engine_names()))
async def test_every_catalog_engine_can_be_selected(engine):
    state: dict = {}
    told: list = []
    assert await pin_selected_engine(state, engine, jlog=_Log(), publish=_acollect(told)) == engine
    assert state["engine"] == engine
    assert told == [engine], "the user was not told which engine their run uses"



async def _anoop(_v=None) -> None:
    return None


def _acollect(sink):
    async def _c(v):
        sink.append(v)
    return _c


async def test_no_selection_leaves_the_engine_unpinned():
    state: dict = {}
    assert await pin_selected_engine(state, "", jlog=_Log(), publish=_anoop) == ""
    assert "engine" not in state


async def test_an_unknown_engine_is_refused_not_substituted():
    state: dict = {}
    told: list = []
    log = _Log()
    assert await pin_selected_engine(state, "notamesher", jlog=log, publish=_acollect(told)) == ""
    assert "engine" not in state, "an unknown engine name was pinned anyway"
    assert told == [], "the user was told about an engine that does not exist"
    assert log.warnings, "an unknown engine selection was accepted silently"


async def test_the_pin_never_defaults_to_the_default_engine():
    from meshpipeline.engines.registry import default_engine

    state: dict = {}
    await pin_selected_engine(state, "", jlog=_Log(), publish=_anoop)
    assert state.get("engine") != default_engine(), (
        "an absent selection silently became the default engine")


# selection honours it

async def test_a_pinned_engine_is_honoured_and_nothing_is_classified():
    from meshpipeline.pipeline.engine_select import node_engine_select

    out = await node_engine_select({"job_id": "j", "engine": "gmsh", "purpose": "external_cfd"})
    assert out == {}, "a pinned engine was overwritten by selection"


async def test_a_pin_that_names_no_engine_fails_loudly_rather_than_meshing():
    from meshpipeline.pipeline.engine_select import node_engine_select

    with pytest.raises(Exception):
        await node_engine_select({"job_id": "j", "engine": "notamesher"})


async def test_the_purpose_alone_never_pins_an_engine():
    state: dict = {"job_id": "j", "purpose": "external_cfd"}
    await pin_selected_engine(state, "", jlog=_Log(), publish=_anoop)
    assert "engine" not in state, "the declared purpose selected an engine on the user's behalf"


# ordering and continuation

def test_the_pin_is_written_before_the_graph_exists():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert src.index("pin_selected_engine") < src.index("build_graph("), (
        "the engine is pinned after the graph is built - selection would already have run")


def test_a_dispute_rebuild_adopts_the_parent_engine_after_the_pin():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert src.index("pin_selected_engine") < src.index("seed_dispute_run"), (
        "dispute seeding runs before the user pin, so a dispute rebuild could use the wrong mesher")


def test_a_continuation_carries_the_engine_in_its_durable_state():
    from meshpipeline.contracts.pipeline_state import PipelineState

    assert "engine" in PipelineState.__annotations__, (
        "engine is not part of the durable pipeline state - a continuation could not preserve it")


def test_a_resume_never_rewrites_the_engine():
    import inspect

    from meshpipeline.application import fenced_checkpointer as fc

    src = inspect.getsource(fc.enter_graph)
    assert '{"geometry":' in src
    assert '"engine"' not in src, "the resume writes the engine key and could overwrite the pin"


# mutation guard

def test_the_orchestrator_no_longer_validates_the_engine_itself():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "engine_names()" not in src, "the run validates the engine catalog itself again"
    assert "pin_selected_engine" in src
