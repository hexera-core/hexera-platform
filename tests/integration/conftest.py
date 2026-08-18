# Responsibility: Give the integration tier its real database and object store.
# Boundaries: fixtures over provisioned services; this tier deliberately uses no doubles.
import sys
import types

# A heavy dependency present as a bare ModuleType with no __file__ is the unit-suite stub, not
# the real package. If any is here, the unit conftest ran in this process first.
_STUBBED = [
    name for name in ("langgraph", "celery", "pyvista", "vtkmodules")
    if isinstance(sys.modules.get(name), types.ModuleType)
    and getattr(sys.modules[name], "__file__", None) is None
]
if _STUBBED:
    raise RuntimeError(
        "Integration tests are running in a process that already loaded the UNIT-suite stubs "
        f"for {_STUBBED}. The tiers must run as SEPARATE pytest sessions - a stubbed dependency "
        "here makes an integration test compare against a fake and skip silently, which is a "
        "false 'green'. Run `make test-integration` (its own session), never "
        "`pytest tests/unit tests/integration` together. See docs/development/overview.md."
    )


import os  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402


# Refuse BEFORE collection when the configured database is not this run's disposable one. This is
# the choke point: every destructive fixture below, and every helper they reach, runs only after
# this has proven - from the live server, not from the URL text - that the database was
# provisioned for this run. A developer's DATABASE_URL pointing at the shared development database
# stops here, having executed two read-only identity queries and nothing else.
def pytest_collection(session):
    url = os.getenv("DATABASE_URL")
    if not url:
        return
    from tests import disposable_database as dd
    from tests import harness_provisioning as hp

    try:
        hp.set_session_authority(dd.authorize(url))
    except dd.NotDisposableError as exc:
        raise pytest.UsageError(
            f"the integration tier refuses to run against this database.\n\n{exc}\n\n"
            "The integration suite drops and rebuilds the public schema. It may only ever do that "
            "to a database provisioned for the current run. Use `make test-integration` (which "
            "provisions one on the local stack) or `make test-container-integration` (which "
            "provisions a whole throwaway stack). Pointing pytest at a long-lived database by "
            "exporting DATABASE_URL destroys it.") from None


# The tier writes real files through a scratch root the runner supplies. Without a usable one the
# suites that need it fail one at a time on a bare KeyError, which says nothing about what is
# missing; this fails the session once, naming the coordinate and who owns it.
def pytest_collection_modifyitems(config, items):
    if not os.getenv("DATABASE_URL"):
        return

    def refuse(problem: str) -> None:
        raise pytest.UsageError(
            f"API_ROOT {problem}. The integration tier materialises real files through it, so it "
            "must be an existing writable directory inside this container. `make test-integration` "
            "creates one per run and removes it afterwards; run the tier that way.")

    root = os.getenv("API_ROOT")
    if not root:
        refuse("is not set")
    path = Path(root)
    if not path.exists():
        refuse(f"={root!r} does not exist")
    if not path.is_dir():
        refuse(f"={root!r} is not a directory")
    if not os.access(path, os.W_OK):
        refuse(f"={root!r} is not writable")


@pytest.fixture(scope="session", autouse=True)
def _provisioned_stack():
    if not os.getenv("DATABASE_URL"):
        yield
        return

    import asyncio

    from tests import harness_provisioning as hp

    asyncio.run(hp.ensure_schema(hp.dsn()))

    if os.getenv("MINIO_ENDPOINT"):
        hp.ensure_bucket()
    yield


@pytest.fixture(autouse=True)
async def _schema_present():
    if not os.getenv("DATABASE_URL"):
        yield
        return

    from tests import harness_provisioning as hp
    await hp.ensure_schema(hp.dsn())
    yield


@pytest.fixture(autouse=True)
async def _engine_per_loop():
    yield
    from meshpipeline.persistence.session import dispose_engine
    await dispose_engine()


@pytest.fixture()
async def db():
    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as session:
        yield session
    await dispose_engine()


@pytest.fixture()
def store():
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.contracts import object_storage
    st = build_object_store()
    object_storage.set_object_store(st)
    yield st
    object_storage.set_object_store(None)


@pytest.fixture()
def collection_enabled(monkeypatch):
    # An OPT-IN enabled process for the suites whose subject is capture. Retention is a product
    # mode, decided once per process by settings/modes.py and frozen onto polcfg.MODES; production
    # never re-reads it, and nothing here makes it do so. This replaces the frozen object with one
    # built the same way, differing in the single field the test needs, and monkeypatch puts the
    # original back.
    #
    # It is deliberately NOT autouse and NOT session-scoped. The shipped default stays disabled -
    # a run that keeps nothing is the privacy-preserving default - so only a test that asks for
    # this fixture sees collection at all.
    #
    # Writing `polcfg.DATA_COLLECTION_ENABLED = True` is what the capture suites used to do. That
    # name does not exist on the policy module: the value lives on MODES, so the assignment created
    # an attribute nobody reads and the process stayed disabled. Every capture assertion then found
    # zero durable operations.
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.settings.modes import ProductModes

    monkeypatch.setattr(
        polcfg, "MODES",
        ProductModes(data_collection_enabled=True,
                     trace_disclosure=polcfg.MODES.trace_disclosure))
    return polcfg.MODES
