# Responsibility: Verify a user's engine choice is direct input that pins state, and selection never calls a model.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import json

# graph → agents.reviewer.visual → sandbox.sandbox needs a renderer; stub if absent
# (same pattern as test_dispute_flow.py / test_graph_real.py).
import sys as _sys
import uuid as _uuid
from pathlib import Path
from pathlib import Path as _Path
from types import SimpleNamespace

import pytest
from conftest import FakePublisher, install_durable_execution_fakes  # noqa: E402
from tests.execution_publisher_double import install as _install_pub

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.agents.intake.agent import INTAKE_TOOLS  # noqa: E402
from meshpipeline.engines.registry import engine_names  # noqa: E402
from meshpipeline.persistence.job_state import TransitionResult

_sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))

_GEOMETRY_SOURCE = {'source_id': '22222222-2222-4222-8222-222222222222', 'owner_id': 'owner-1', 'object_key': 'sources/22222222-2222-4222-8222-222222222222', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'input.step', 'suffix_hint': '.step'}



def _submit_schema() -> dict:
    for t in INTAKE_TOOLS:
        if t["function"]["name"] == "submit_requirements":
            return t["function"]
    raise AssertionError("submit_requirements tool not found")


# intake contract  #

def test_schema_engine_enum_tracks_the_catalog():
    props = _submit_schema()["parameters"]["properties"]
    assert "mesh_engine" in props
    # EXACTLY the implemented engines - no 'auto': every submission carries a
    # concrete user-confirmed engine (2026-07-05 routing redo)
    # Intake is offered DISPLAY NAMES, never registry keys - and each one maps back to exactly
    # one implemented engine (see agents/intake/vocabulary.py).
    from meshpipeline.agents.intake import vocabulary as _vocab
    assert set(props["mesh_engine"]["enum"]) == set(_vocab.engine_choices())
    assert {_vocab.to_key(_vocab.ENGINE, n) for n in props["mesh_engine"]["enum"]} \
        == set(engine_names())
    assert "mesh_engine" in _submit_schema()["parameters"]["required"]
    assert set(props["engine_source"]["enum"]) == {"user_direct", "suggested_confirmed"}
    assert "engine_source" in _submit_schema()["parameters"]["required"]


def test_schema_says_engine_is_direct_user_input():
    desc = _submit_schema()["parameters"]["properties"]["mesh_engine"]["description"]
    assert "DIRECT USER INPUT" in desc and "CONFIRMED" in desc.upper()


def test_intake_prompt_is_engine_first():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "agents" / "intake" / "agent.py").read_text()
    assert "ENGINE FIRST" in src
    assert "FIRST substantive question" in src   # engine question opens the intake
    assert "AUTHORITATIVE" in src                # a named engine is never second-guessed
    assert "never silently applied" in src       # fallback = propose + confirm
    assert "intake_guidance" in src or "_native_qs" in src  # engine-native follow-ups
    assert "catalog_menu" in src                 # options from the single catalog source


# worker seeding  #

def _stub_infra(monkeypatch, wt, captured, tmp_path=None):
    # These tests are about ENGINE resolution, not about obtaining geometry, so the execution
    # entry's materialisation is stubbed with a real verified file rather than a database and an
    # object store. Materialisation itself is exercised in the geometry suites.
    if tmp_path is not None:
        from unittest.mock import AsyncMock

        from tests._geometry_support import materialized as _mk

        import meshpipeline.application.geometry_materializer as _gm
        monkeypatch.setattr(_gm, "prepare_execution_geometry",
                            AsyncMock(return_value=_mk(tmp_path / "materialised")))

    class FakeGraph:

        async def aget_state(self, config=None):

            # A compiled graph always answers this; the entry asks before deciding fresh vs

            # resume. An empty thread is the right answer for a double that never checkpoints.

            from types import SimpleNamespace

            return SimpleNamespace(next=(), values={}, tasks=(), created_at=None, config={})

        async def ainvoke(self, state, config=None):
            captured.update(state)
            return {**state, "reviewer_verdict": "FAIL", "outcome_message": "done"}

    import meshpipeline.pipeline.graph as graph_module
    monkeypatch.setattr(graph_module, "build_graph", lambda checkpointer=None: FakeGraph())

    import sqlalchemy.ext.asyncio as sa_aio

    async def _noop(): pass

    class FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): pass
        async def execute(self, *a, **k): pass
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_noop))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: FakeSession)

    import meshpipeline.persistence.repositories.job_repository as jr

    class FakeRepo:
        row = SimpleNamespace(owner_id="dev-user", status=None, failed_reason=None, created_at=None, ended_at=None)
        async def get_internal(self, db, job_id): return self.row
        async def get_for_owner(self, db, job_id, owner_id): return self.row
        async def transition(self, db, job_id, target, *, allow=None):
            self.row.status = target
            return TransitionResult.applied
        async def update_current_attempt(self, db, job_id, n): pass
        async def set_final_result(self, db, job_id, fr): pass
    monkeypatch.setattr(jr, "JobRepository", lambda: FakeRepo())
    monkeypatch.setattr(wt, "_pub", lambda job_id, stage="outcome": FakePublisher(job_id, stage))
    monkeypatch.setattr(wt, "_incr_delivery_count", lambda job_id: 1)
    install_durable_execution_fakes(monkeypatch, wt) # durable lease + outbox seams


async def test_user_engine_pins_state(monkeypatch, tmp_path):
    import meshpipeline.application.pipeline_run as wt
    _install_pub(monkeypatch, wt)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)
    captured: dict = {}
    _stub_infra(monkeypatch, wt, captured, tmp_path)
    await wt._run_async(wt.JobRequest(
        job_id=str(_uuid.uuid4()), geometry_source=_GEOMETRY_SOURCE,
        request_txt="mesh it", mesh_engine="snappy"))
    assert captured["engine"] == "snappy"          # resolution keeps the pin


async def test_unknown_user_engine_left_to_deterministic_resolution(monkeypatch, tmp_path):
    import meshpipeline.application.pipeline_run as wt
    _install_pub(monkeypatch, wt)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)
    captured: dict = {}
    _stub_infra(monkeypatch, wt, captured, tmp_path)
    await wt._run_async(wt.JobRequest(
        job_id=str(_uuid.uuid4()), geometry_source=_GEOMETRY_SOURCE,
        request_txt="mesh it", mesh_engine="pointwise"))  # not in the catalog
    assert not captured.get("engine")              # unset → node resolves deterministically


async def test_dispute_pin_beats_user_engine(monkeypatch, tmp_path):
    import meshpipeline.application.pipeline_run as wt
    _install_pub(monkeypatch, wt)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)
    parent = "parent-x"
    ws = tmp_path / parent / "generation_1" / "attempt_1"
    ws.mkdir(parents=True)
    (ws / "mesh_manifest.json").write_text(json.dumps({"mesh_mode": "snappy"}))
    (ws / "request.txt").write_text("orig")
    captured: dict = {}
    _stub_infra(monkeypatch, wt, captured, tmp_path)
    await wt._run_async(wt.JobRequest(
        job_id=str(_uuid.uuid4()), mesh_engine="cfmesh",
        user_dispute={"of_job_id": parent, "flags": [], "comment": "finer"}))
    assert captured["engine"] == "snappy"          # parent's engine, not the pref


# selector defers  #

async def test_engine_select_never_calls_a_model(monkeypatch, tmp_path):

    monkeypatch.setattr(rtcfg, "CORPUS_DIR", str(tmp_path))
    import meshpipeline.adapters.model_inference.router as r

    async def _boom(*a, **k):
        raise AssertionError("engine resolution must never call a model")
    monkeypatch.setattr(r, "call_classifier_model", _boom, raising=False)
    from meshpipeline.pipeline import engine_select as es
    out = await es.node_engine_select({"engine": "cfmesh", "job_id": "t"})
    assert out == {}                               # pin untouched, no model call


@pytest.fixture(autouse=True)
def _execution_fence(monkeypatch):
    # This suite claims delivery with no Redis. The mirror is stood in for at the two seams
    # production resolves; ownership checks, claim transitions and the transaction stay real.
    from tests.execution_fence_double import install
    return install(monkeypatch)
