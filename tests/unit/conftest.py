# Responsibility: Give the unit tier its fixtures, doubles and durable-execution stand-ins.
# Boundaries: the tier's shared seams; a test needing a real service belongs in the integration tier.
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

# The API mounts /ui and /static at IMPORT time from STATIC_DIR (api/app.py). Point it at the
# repo's shipped frontend BEFORE any meshpipeline import so the whole-app HTTP smoke can
# exercise those routes on the real assembled app. setdefault: an explicit override still wins,
# and this is a no-op wherever STATIC_DIR is already the shipped UI (e.g. /srv/ui in-image).
os.environ.setdefault("STATIC_DIR", str(Path(__file__).resolve().parents[2] / "ui"))

# THE RUNTIME DATA ROOTS, OFF THE CHECKOUT. settings/runtime.py defaults these to CWD-relative
# ./data and ./workspaces, and `api/v1/upload.py` binds AND mkdirs its jobs dir at IMPORT time -
# so merely importing the app under pytest created data/jobs/ in the repository, and the suite
# then left ~2000 job traces and mesh workspaces behind it. Set before any meshpipeline import
# so the import-time binding never sees the checkout. The per-test fixture below narrows this
# further; this level exists for whatever runs at import/collection.
_RUNTIME_TMP = Path(tempfile.mkdtemp(prefix="meshpipeline-unit-"))
os.environ.setdefault("DATA_ROOT", str(_RUNTIME_TMP / "data"))
os.environ.setdefault("JOBS_DIR", str(_RUNTIME_TMP / "data" / "jobs"))
os.environ.setdefault("CORPUS_DIR", str(_RUNTIME_TMP / "data" / "corpus"))
os.environ.setdefault("WORKSPACE_BASE", str(_RUNTIME_TMP / "workspaces"))

# THE OBJECT-STORE SETTINGS THIS TIER RUNS UNDER. Pinned, not `setdefault`ed: these deliberately
# OVERRIDE whatever the developer's shell exports, because the unit tier owns its own
# configuration and must not inherit a pointer to somebody's real bucket.
# Ambient values used to decide what a unit test talked to. After driving the integration stack -
# the ordinary state of a working shell - MINIO_* variables
# turned two upload tests into external-service tests that returned 503, and a malformed ambient
# bucket name failed the whole tier at import. Both are false signals: one fails a correct change,
# the other could pass a broken one against a store no assertion mentions.
# These values are syntactically valid so settings validation is deterministic, and they are never
# dialled: the store composed below is in-memory. Integration is unaffected - it exports its own
# isolated endpoint and composes the genuine adapter.
os.environ["MINIO_ENDPOINT"] = "unit-tier.invalid:9000"
os.environ["MINIO_ACCESS_KEY"] = "unit-tier-access"
os.environ["MINIO_SECRET_KEY"] = "unit-tier-secret"
os.environ["MINIO_BUCKET"] = "unit-tier-bucket"

# Tests import the product exclusively through the installed `meshpipeline` distribution - the
# package dir is NOT placed on sys.path (that would expose internal packages like `engines` as
# bogus top-level modules and defeat the src-layout). Only the LangGraph/Celery heavy-dep stubs
# below are set up here.


class _FakeStateGraph:
    def __init__(self, *a, **k): pass
    def add_node(self, *a, **k): pass
    def add_edge(self, *a, **k): pass
    def add_conditional_edges(self, *a, **k): pass
    def compile(self, *a, **k): return self

    async def aget_state(self, config=None):
        # A compiled graph answers this, and the execution entry asks BEFORE deciding whether it
        # needs geometry at all. The hermetic tier has no durable store, so the truthful answer is
        # an EMPTY thread - `created_at=None`, nothing pending. Omitting the method made every
        # hermetic run look like an unreadable checkpoint, which is a disposition no real empty
        # thread ever reports.
        return types.SimpleNamespace(next=(), values={}, tasks=(), created_at=None, config={})

class _FakeMemorySaver:
    pass

_lg               = types.ModuleType("langgraph")
_lg_graph         = types.ModuleType("langgraph.graph")
_lg_graph.StateGraph = _FakeStateGraph
# The REAL langgraph sentinel values. A stub that invents its own ("START"/"END") would let
# production code return the wrong sentinel and still pass the hermetic tier.
_lg_graph.START      = "__start__"
_lg_graph.END        = "__end__"
_lg_graph_msg        = types.ModuleType("langgraph.graph.message")
_lg_graph_msg.add_messages = lambda *a, **k: list(a[0]) if a else []
_lg_checkpoint       = types.ModuleType("langgraph.checkpoint")
# The REAL base class is what LangGraph's `compile()` type-checks against, and the product's
# ownership-fencing checkpointer subclasses it - so the stub has to provide it too, or the fenced
# wrapper cannot even be imported in the hermetic tier.
_lg_checkpoint_base  = types.ModuleType("langgraph.checkpoint.base")
class _FakeBaseCheckpointSaver:
    def __init__(self, *a, **k): self.serde = k.get("serde")
    @property
    def config_specs(self): return []
_lg_checkpoint_base.BaseCheckpointSaver = _FakeBaseCheckpointSaver
_lg_errors           = types.ModuleType("langgraph.errors")
class _FakeGraphInterrupt(Exception): pass
_lg_errors.GraphInterrupt = _FakeGraphInterrupt
_lg_checkpoint_mem   = types.ModuleType("langgraph.checkpoint.memory")
_lg_checkpoint_mem.MemorySaver = _FakeMemorySaver
_lg_checkpoint_pg    = types.ModuleType("langgraph.checkpoint.postgres")
_lg_checkpoint_pg_aio = types.ModuleType("langgraph.checkpoint.postgres.aio")
class _FakeAsyncPostgresSaver:
    # `from_conn_string` is an async context manager on the real saver, and production enters it
    # both to classify a thread and to run the graph. A double that returns a bare instance makes
    # the classification look like an unreadable checkpoint.
    @classmethod
    def from_conn_string(cls, *a, **k): return cls()
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def setup(self): pass
_lg_checkpoint_pg_aio.AsyncPostgresSaver = _FakeAsyncPostgresSaver

for _name, _mod in [
    ("langgraph",                          _lg),
    ("langgraph.graph",                    _lg_graph),
    ("langgraph.graph.message",            _lg_graph_msg),
    ("langgraph.checkpoint",               _lg_checkpoint),
    ("langgraph.checkpoint.base",          _lg_checkpoint_base),
    ("langgraph.errors",                   _lg_errors),
    ("langgraph.checkpoint.memory",        _lg_checkpoint_mem),
    ("langgraph.checkpoint.postgres",      _lg_checkpoint_pg),
    ("langgraph.checkpoint.postgres.aio",  _lg_checkpoint_pg_aio),
]:
    sys.modules.setdefault(_name, _mod)


# #
# RENDER DEPENDENCIES (pyvista / PIL) - THE ONE PLACE THEY MAY BE SUBSTITUTED.
# Modules under test import these at import time, and the app image does not carry them, so the
# tier needs a stand-in WHERE THEY ARE GENUINELY ABSENT. Eight test modules used to install that
# stand-in themselves, each at import time, each guarded only by `if "pyvista" not in
# sys.modules`. Seven of them tried the real import first; one did not - so whenever it was
# imported before anything had pulled in the real package, it replaced an INSTALLED pyvista with a
# five-attribute stub for the rest of the process. `test_consolidated_matrix.py` then failed 12
# tests with `module 'pyvista' has no attribute 'Cube'`, and which module won the race depended on
# collection order.
# The substitution belongs here, once, ahead of every test module, and it must never shadow a real
# package: a stub is installed ONLY when importing the real one raises ImportError.
# #
def _stub_if_absent(name: str, build) -> bool:
    import importlib

    try:
        importlib.import_module(name)
        return False
    except ImportError:
        for mod_name, mod in build().items():
            sys.modules.setdefault(mod_name, mod)
        return True


def _fake_pyvista() -> dict:
    pv = types.ModuleType("pyvista")
    pv.PolyData = object
    pv.Plotter = lambda **k: None
    pv.global_theme = types.SimpleNamespace(allow_empty_mesh=None)
    pv.Actor = object
    return {"pyvista": pv}


def _fake_pil() -> dict:
    pil = types.ModuleType("PIL")
    image = types.ModuleType("PIL.Image")
    image.fromarray = lambda *a, **k: types.SimpleNamespace(save=lambda *a, **k: None)
    pil.Image = image
    return {"PIL": pil, "PIL.Image": image}


#: Which render dependencies this process is running against - real or stubbed. Read by
#: tests/unit/infra/test_render_dependency_isolation.py, which is the control proving a stub can
#: never shadow an installed package.
RENDER_STUBS_INSTALLED = {
    "pyvista": _stub_if_absent("pyvista", _fake_pyvista),
    "PIL": _stub_if_absent("PIL", _fake_pil),
}

class _FakeCelery:
    def __init__(self, *a, **k):
        self.conf = types.SimpleNamespace(update=lambda **_: None)
    def task(self, *a, **k):
        def _decorator(fn):
            return fn
        return _decorator

class _FakeSignal:
    def connect(self, fn=None, **k):
        if fn is None:
            return lambda f: f
        return fn

_celery_mod  = types.ModuleType("celery")
_celery_mod.Celery = _FakeCelery
_celery_signals = types.ModuleType("celery.signals")
_celery_signals.worker_process_init     = _FakeSignal()
_celery_signals.setup_logging           = _FakeSignal()
_celery_signals.worker_init             = _FakeSignal()
_celery_signals.worker_ready            = _FakeSignal()
_celery_signals.worker_process_shutdown = _FakeSignal()
_celery_exc = types.ModuleType("celery.exceptions")
class _FakeSoftTimeLimit(Exception): pass
class _FakeTimeLimit(Exception): pass
_celery_exc.SoftTimeLimitExceeded = _FakeSoftTimeLimit
_celery_exc.TimeLimitExceeded     = _FakeTimeLimit
for _n, _m in [
    ("celery",            _celery_mod),
    ("celery.signals",    _celery_signals),
    ("celery.exceptions", _celery_exc),
]:
    sys.modules.setdefault(_n, _m)

os.environ.setdefault("DEEPSEEK_API_KEY",   "sk-test")
os.environ.setdefault("DEEPINFRA_API_KEY",   "sk-test")
os.environ.setdefault("POSTGRES_PASSWORD",   "test-password")
os.environ.setdefault("MESH_API_KEY",     "")

# LOUD on purpose: if worker.tasks stops importing under the stubs, every test
# session should fail here, not silently skip (a swallowed ImportError once
# masked a broken celery.signals stub for the whole local suite).
import pytest as _pytest

import meshpipeline.adapters.pipeline_execution.celery  # noqa: F401  side-effect: register `worker` package for tests

_MISSING = object()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_RUNTIME_TMP, ignore_errors=True)


@_pytest.fixture(autouse=True)
def _runtime_paths_are_test_owned(tmp_path):
    from meshpipeline.settings import runtime as rtcfg

    root = tmp_path / "_runtime"
    jobs, corpus, workspaces = root / "data" / "jobs", root / "data" / "corpus", root / "workspaces"
    for d in (jobs, corpus, workspaces):
        d.mkdir(parents=True, exist_ok=True)

    targets: list[tuple[object, str, object]] = [
        (rtcfg, "DATA_ROOT", root / "data"),
        (rtcfg, "JOBS_DIR", jobs),
        (rtcfg, "CORPUS_DIR", str(corpus)),
        (rtcfg, "WORKSPACE_BASE", workspaces),
    ]
    try:
        from meshpipeline.api.v1 import upload as _upload
        targets.append((_upload, "_JOBS_DIR", jobs))
    except Exception:
        pass

    saved = [(mod, name, getattr(mod, name, _MISSING)) for mod, name, _ in targets]
    for mod, name, value in targets:
        setattr(mod, name, value)
    try:
        yield
    finally:
        for mod, name, old in saved:
            if old is _MISSING:
                delattr(mod, name)
            else:
                setattr(mod, name, old)


@_pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    try:
        from meshpipeline.adapters._shared import resilience
        resilience.reset_breakers()
    except Exception:
        pass
    yield
    try:
        from meshpipeline.adapters._shared import resilience
        resilience.reset_breakers()
    except Exception:
        pass



# the user stream
class FakePublisher:

    def __init__(self, job_id: str = "", agent: str = "") -> None:
        self.job_id, self.agent, self.events = job_id, agent, []

    def emit(self, event) -> None:
        self.events.append(event)

    def _rec(self, type_: str, **data) -> None:
        self.events.append({"type": type_, "stage": self.agent, **data})

    def stage(self, op_id="") -> None:              self._rec("stage", op_id=op_id)
    def attempt(self, n, of, op_id="") -> None:     self._rec("attempt", n=n, of=of, op_id=op_id)
    def note(self, text, tone="info", op_id="") -> None:
        self._rec("note", text=text, tone=tone, op_id=op_id)
    def warn(self, text, op_id="") -> None:         self._rec("note", text=text, tone="warn",
                                                              op_id=op_id)
    def error(self, text, op_id="") -> None:        self._rec("note", text=text, tone="error",
                                                              op_id=op_id)
    def check(self, statement, *, ok) -> None:      self._rec("check", statement=statement, ok=ok)
    def action(self, actions) -> None:              self._rec("action", actions=actions)
    def reasoning(self, text) -> None:              self._rec("reasoning", text=text)
    def search(self, query) -> None:                self._rec("search", query=query)
    def screenshot(self, image_b64, op_id="") -> None:
        self._rec("screenshot", op_id=op_id)
    def meshing(self, engine, budget_s) -> None:    self._rec("meshing", engine=engine, budget_s=budget_s)
    def meshed(self, cells=None) -> None:           self._rec("meshed", cells=cells)
    def verdict(self, verdict, summary="") -> None: self._rec("verdict", verdict=verdict)
    def closing(self, text, event_id="") -> None:
        # production passes the terminal identity so an early close and a later
        # outbox delivery are one event; the double must accept it
        self._rec("closing", text=text, event_id=event_id)


def install_durable_execution_fakes(monkeypatch, run_module, *, published=None,
                                    owner=True, generation=1):
    import uuid as _uuid

    from meshpipeline.persistence import lease as _lease_mod
    from meshpipeline.persistence.repositories import (
        terminal_outbox_repository as _outbox_mod,
    )

    rows: list[dict] = []
    own = _lease_mod.ExecutionOwnership(
        job_id=_uuid.uuid4(), execution_generation=generation, worker_token=_uuid.uuid4(),
        backend="direct", pipeline_deadline_at=None)

    class _FakeLease:
        async def claim_execution(self, db, job_id, **kw):
            return (_lease_mod.ClaimResult.acquired_new_generation,
                    _lease_mod.ExecutionOwnership(
                        job_id=job_id, execution_generation=generation,
                        worker_token=own.worker_token, backend="direct",
                        pipeline_deadline_at=None))
        async def heartbeat(self, db, ownership, **kw):        return owner
        async def is_current_owner(self, db, ownership):       return owner
        async def lock_current_owner(self, db, ownership):
            return object() if owner else None                 # None = FENCED (writes nothing)
        async def release(self, db, ownership, **kw):          return None

    class _FakeOutbox:
        async def enqueue(self, db, *, job_id, execution_generation, terminal_status,
                          final_result_schema_version, event_payload):
            if any(r["job_id"] == str(job_id) for r in rows):
                return False                                   # dedup: one terminal event per job
            rows.append({"job_id": str(job_id), "payload": event_payload, "published": False})
            return True

    monkeypatch.setattr(_lease_mod, "LeaseRepository", _FakeLease)
    monkeypatch.setattr(_outbox_mod, "TerminalOutboxRepository", _FakeOutbox)
    # terminal_finalize binds these at import, so patch its namespace too - otherwise the atomic
    # finalizer would still construct the REAL outbox repository against the faked session.
    from meshpipeline.application import terminal_finalize as _tf_mod
    monkeypatch.setattr(_tf_mod, "TerminalOutboxRepository", _FakeOutbox)
    monkeypatch.setattr(_tf_mod, "LeaseRepository", _FakeLease)

    async def _deliver(session_factory, job_id, **kw):
        for r in rows:
            if r["job_id"] == str(job_id) and not r["published"]:
                run_module._pub(str(job_id)).closing(str(r["payload"].get("closing_message") or ""))
                r["published"] = True
        return None

    from meshpipeline.application import outbox_publisher as _obp
    monkeypatch.setattr(_obp, "deliver_own_terminal_event", _deliver)
    return _pytest_ns(outbox_rows=rows, ownership=own, published=published)


def _pytest_ns(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(**kw)


@_pytest.fixture(autouse=True)
def _compose_test_adapters():
    from meshpipeline.adapters.ws_ticket.memory import MemoryWsTicketStore
    from meshpipeline.contracts import (
        event_stream,
        model_inference,
        object_storage,
        search,
        training_export,
        ws_ticket,
    )

    # WS connection tickets: an in-memory single-use store (no Redis), so the ticket flow is
    # exercisable in the hermetic unit tier; ticket-specific tests re-inject their own.
    ws_ticket.set_ws_ticket_store(MemoryWsTicketStore())

    # LLM router: inject the real module so existing router monkeypatches still take effect.
    try:
        from meshpipeline.adapters.model_inference import router
        model_inference.set_model_router(router)
    except Exception:
        model_inference.set_model_router(None)

    # user event stream: the recording double (no Redis); event-inspecting tests hold their own.
    event_stream.set_publisher_factory(FakePublisher)

    # object store: an IN-MEMORY double, never the settings-selected concrete adapter. Composing
    # the concrete one made storage behaviour a function of the ambient environment - see the
    # pinned settings at the top of this file. A fresh instance per test, so nothing an earlier
    # test uploaded is visible to the next. Storage-specific tests still inject their own.
    from tests.object_store_double import InMemoryObjectStore

    from meshpipeline.adapters.object_storage.factory import _reset_for_tests as _rs_store
    _rs_store()
    object_storage.set_object_store(InMemoryObjectStore())

    # web search: the settings-selected concrete, constructed lazily so no network happens.
    try:
        from meshpipeline.adapters.search.factory import build_web_search_provider
        search.set_web_search_provider(build_web_search_provider())
    except Exception:
        search.set_web_search_provider(None)

    # training export: no-op by default; export-behaviour tests inject a capturing enqueuer.
    training_export.set_export_enqueuer(lambda *a, **k: None)
    yield


from tests.capture_authority import CAPTURE_OWNER  # re-exported for the suites


@_pytest.fixture()
def capture_authority(monkeypatch):
    from tests.capture_authority import InMemoryAuthority

    from meshpipeline.capture import scope as _scope
    auth = InMemoryAuthority().install(monkeypatch)
    with _scope.capture_scope(CAPTURE_OWNER, ""):
        yield auth
