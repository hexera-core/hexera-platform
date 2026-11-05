# Responsibility: Verify a dispute inherits its parent's resolved attempt, and refuses a missing or traversing parent.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

import json
import uuid

import pytest

from meshpipeline.application import job_service as js
from meshpipeline.errors import SystemFailure

JOB = str(uuid.uuid4())
PARENT = str(uuid.uuid4())


class _Log:
    def __init__(self): self.warnings = []; self.infos = []
    def warning(self, *a, **k): self.warnings.append(a)
    def info(self, *a, **k): self.infos.append(a)
    def error(self, *a, **k): pass


@pytest.fixture
def parent(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    ws = tmp_path / PARENT / "generation_1" / "attempt_2"
    ws.mkdir(parents=True)
    (ws / "mesh_manifest.json").write_text(json.dumps({
        "mesh_mode": "gmsh", "engine_params": {"element_order": "2"},
        "flow_topology": "External", "cell_count": 1000}))
    (ws / "request.txt").write_text("mesh the wing")
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))
    return ws



async def _anoop(_v=None) -> None:
    return None


def _acollect(sink):
    async def _c(v):
        sink.append(v)
    return _c


def _dispute(**kw):
    d = {"of_job_id": PARENT, "flags": [{"region": "wing"}, {"region": "tip"}],
         "comment": "the trailing edge is coarse"}
    d.update(kw)
    return d


# resolution

def test_a_valid_dispute_resolves_the_parents_final_attempt(parent):
    seed = js.resolve_dispute_seed(_dispute(), jlog=_Log())
    assert seed.parent_job_id == PARENT
    assert seed.parent_workspace == parent
    assert seed.parent_engine == "gmsh"
    assert seed.flags == 2


def test_a_missing_parent_workspace_refuses(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))
    with pytest.raises(SystemFailure) as ei:
        js.resolve_dispute_seed(_dispute(), jlog=_Log())
    assert "no reviewable workspace" in str(ei.value)


def test_a_dispute_with_no_parent_id_refuses(parent):
    with pytest.raises(SystemFailure):
        js.resolve_dispute_seed(_dispute(of_job_id=""), jlog=_Log())


def test_an_unreadable_parent_manifest_refuses(parent):
    (parent / "mesh_manifest.json").write_text("{not json")
    with pytest.raises(SystemFailure) as ei:
        js.resolve_dispute_seed(_dispute(), jlog=_Log())
    assert "manifest unreadable" in str(ei.value)


def test_a_parent_without_engine_params_is_warned_not_refused(parent):
    (parent / "mesh_manifest.json").write_text(json.dumps({"mesh_mode": "cfmesh"}))
    log = _Log()
    seed = js.resolve_dispute_seed(_dispute(), jlog=log)
    assert seed.parent_engine == "cfmesh" and log.warnings


# seeding

async def test_the_state_inherits_the_parents_mesh_engine_and_regime(parent):
    state: dict = {}
    await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert state["openfoam_workspace"] == str(parent)
    assert state["engine"] == "gmsh"
    assert state["mesh_manifest"]["cell_count"] == 1000
    assert state["flow_topology"] == "external", "the regime was not normalised"
    # resolve_engine_params keeps only the params the ENGINE declares - a rebuild
    # cannot inherit a knob its mesher does not have.
    assert state["engine_params"]["element_order"] == "2"
    assert state["user_dispute"]["of_job_id"] == PARENT


async def test_the_parent_mesh_is_marked_as_having_passed_its_gates(parent):
    state: dict = {}
    await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert state["executor_success"] is True


async def test_the_parents_request_is_inherited_only_when_absent(parent):
    fresh: dict = {}
    await js.seed_dispute_run(fresh, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert fresh["request_txt"] == "mesh the wing"

    kept: dict = {"request_txt": "the user's own new words"}
    await js.seed_dispute_run(kept, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert kept["request_txt"] == "the user's own new words"


async def test_the_workspace_pointer_stays_inside_the_workspace_root(parent, tmp_path):
    state: dict = {}
    await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    from pathlib import Path
    assert Path(state["openfoam_workspace"]).resolve().is_relative_to(tmp_path.resolve())


def test_a_parent_id_that_tries_to_traverse_resolves_to_nothing(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    with pytest.raises(SystemFailure):
        js.resolve_dispute_seed(_dispute(of_job_id="../../etc"), jlog=_Log())


# capture

async def test_the_capture_is_keyed_so_a_replay_does_not_duplicate_it(parent, monkeypatch):
    logged: list = []
    import meshpipeline.capture.logger as cl

    class _TL:
        def __init__(self, job_id): pass
        def log(self, event, *, op_id, payload): logged.append((event, op_id, payload))
    monkeypatch.setattr(cl, "TrainingLogger", _TL)

    for _ in range(2):
        await js.seed_dispute_run({}, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert [e[1] for e in logged] == ["dispute-context", "dispute-context"], (
        "the capture is not keyed by a stable operation id, so a replay writes a new record")
    assert logged[0][2]["parent_engine"] == "gmsh"
    assert logged[0][2]["n_flags"] == 2


async def test_no_capture_is_written_when_the_parent_cannot_be_reviewed(tmp_path, monkeypatch):
    logged: list = []
    import meshpipeline.capture.logger as cl
    import meshpipeline.settings.runtime as rtcfg

    class _TL:
        def __init__(self, job_id): pass
        def log(self, *a, **k): logged.append(a)
    monkeypatch.setattr(cl, "TrainingLogger", _TL)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))

    with pytest.raises(SystemFailure):
        await js.seed_dispute_run({}, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert logged == [], "a capture recorded a dispute that never resolved"


async def test_a_failed_resolution_writes_no_state_key(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(tmp_path))
    state: dict = {}
    with pytest.raises(SystemFailure):
        await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=_Log(), publish=_anoop)
    assert state == {}, "a half-seeded state was left behind for a dispute that did not resolve"


async def test_a_capture_failure_never_stops_the_dispute(parent, monkeypatch):
    import meshpipeline.capture.logger as cl

    class _Boom:
        def __init__(self, job_id): raise RuntimeError("capture down")
    monkeypatch.setattr(cl, "TrainingLogger", _Boom)
    state: dict = {}
    log = _Log()
    await js.seed_dispute_run(state, _dispute(), job_id=JOB, jlog=log, publish=_anoop)
    assert state["engine"] == "gmsh" and log.warnings


async def test_the_user_is_told_how_many_regions_were_flagged(parent):
    announced: list = []
    await js.seed_dispute_run({}, _dispute(), job_id=JOB, jlog=_Log(), publish=_acollect(announced))
    assert announced == [2]


# mutation guard

def test_the_orchestrator_no_longer_seeds_the_dispute_itself():
    import inspect

    import meshpipeline.application.pipeline_run as pr

    src = inspect.getsource(pr._run_async)
    assert "final_attempt_workspace" not in src, "the run resolves the parent itself again"
    assert "dispute_context" not in src, "the run writes the dispute capture again"
    assert "mesh_mode" not in src, "the run reads the parent manifest itself again"
    assert "seed_dispute_run" in src
