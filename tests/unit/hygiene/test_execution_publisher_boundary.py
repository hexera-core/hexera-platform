# Responsibility: Verify execution events are published only through the awaited, ownership-checked seam.
# Boundaries: the publication boundary itself; what each mode then emits is the event and fencing suites.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "meshpipeline"
#: Derived from the event vocabulary rather than pinned, so completing the gated surface does not
#: require editing this guard - and so a NEW event method cannot appear without its `a` form here.
def _async_methods() -> set[str]:
    from meshpipeline.contracts.event_stream import EventPublisher

    return {f"a{n}" for n, f in vars(EventPublisher).items()
            if callable(f) and not n.startswith("_")
            and n not in ("emit", "publish_terminal")}


ASYNC_METHODS = _async_methods()
#: Publication that answers to a NON-execution authority - maintenance, terminal finalization,
#: the outbox - and therefore legitimately keeps the synchronous publisher.
SYNC_AUTHORITIES = {"application/maintenance/cleanup.py", "application/terminal_finalize.py",
                    "application/outbox_publisher.py", "adapters/event_stream/redis.py",
                    "application/pipeline_run.py"}   # terminal closings only; asserted below

#: MIGRATION DEBT, not approved authorities. Every module here still publishes execution-scoped
#: events synchronously and must be converted. This set must be EMPTY before the global
#: stale-event defect can be called closed.
#: Empty: every execution-owned publication now goes through the ownership-checked contract.
#: The remaining synchronous publishers are declared authorities, not migration debt.
PENDING_EXECUTION_MIGRATIONS: set[str] = set()


def _tree(rel: str) -> ast.AST:
    return ast.parse((SRC / rel).read_text())


def _all_py() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_every_execution_publisher_call_is_awaited():
    offenders = []
    for path in _all_py():
        tree = ast.parse(path.read_text())
        awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ASYNC_METHODS and id(node) not in awaited):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {node.func.attr}")
    assert offenders == [], f"un-awaited execution publications: {offenders}"


def test_the_execution_protocol_exposes_no_synchronous_emitter():
    from meshpipeline.contracts.event_stream import ExecutionEventPublisher
    exposed = {m for m in dir(ExecutionEventPublisher) if not m.startswith("_")}
    assert exposed == ASYNC_METHODS, exposed
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher
    for sync_name in ("note", "warn", "error", "stage", "attempt", "emit"):
        assert not hasattr(OwnershipCheckedPublisher, sync_name), sync_name


@pytest.mark.parametrize("bridge", ["asyncio.run", "run_coroutine_threadsafe",
                                    "new_event_loop", "ensure_future", "create_task"])
def test_no_sync_to_async_bridge_or_fire_and_forget_in_the_publication_path(bridge):
    for rel in ("application/execution_publisher.py", "contracts/event_stream.py"):
        assert bridge not in (SRC / rel).read_text(), f"{rel} uses {bridge}"


def test_the_ownership_checked_publisher_cannot_fall_back_to_generation_zero():
    src = (SRC / "application" / "execution_publisher.py").read_text()
    assert "_current_generation" not in src, "the exception-swallowing fallback is reachable"
    tree = _tree("application/execution_publisher.py")
    # the generation comes from the VERIFIED ownership, never a literal
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return) and n.value is not None]
    assert not any(isinstance(r.value, ast.Constant) and r.value.value == 0 for r in returns)


def test_direct_publisher_construction_stays_in_composition():
    offenders = []
    for path in _all_py():
        rel = path.relative_to(SRC).as_posix()
        if rel in ("runtime/composition.py", "adapters/event_stream/redis.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "JobPublisher":
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"JobPublisher constructed outside composition: {offenders}"


def test_pipeline_run_publishes_execution_events_only_through_the_async_seam():
    tree = _tree("application/pipeline_run.py")
    sync_emit = {"note", "warn", "error", "stage", "attempt"}
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in sync_emit and isinstance(node.func.value, ast.Name)
                and node.func.value.id.startswith("_")
                and node.func.value.id not in ("_log", "_jlog")):
            bad.append(f"{node.lineno}: {node.func.value.id}.{node.func.attr}")
    assert bad == [], f"pipeline_run still emits execution events synchronously: {bad}"


def test_the_contracts_layer_imports_no_application_implementation():
    tree = _tree("contracts/event_stream.py")
    for node in ast.walk(tree):
        mod = getattr(node, "module", "") or ""
        assert not mod.startswith("meshpipeline.application"), mod


def test_every_sync_publisher_use_is_an_authority_or_declared_migration_debt():
    users = set()
    for path in _all_py():
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text()
        if "event_stream import publisher" in text or "event_stream.publisher(" in text:
            users.add(rel)
    known = SYNC_AUTHORITIES | PENDING_EXECUTION_MIGRATIONS | {"application/execution_publisher.py"}
    assert users <= known, (
        "an unclassified synchronous publisher appeared - it is either a non-execution "
        f"authority or migration debt, and must be declared: {sorted(users - known)}")
    # a pending module dropped from the inventory while it still publishes synchronously
    stale_inventory = {m for m in PENDING_EXECUTION_MIGRATIONS if m not in users}
    assert stale_inventory == set(), (
        f"these are recorded as pending but no longer use the sync publisher - remove them "
        f"from the inventory: {sorted(stale_inventory)}")
    assert not (SYNC_AUTHORITIES & PENDING_EXECUTION_MIGRATIONS), \
        "a pending execution migration is being described as an approved authority"


def test_pipeline_runs_remaining_sync_publication_is_terminal_authorized():
    # Its only synchronous uses are terminal closings, whose identity is the terminal dedup key -
    # that authority owns them, not execution ownership.
    tree = _tree("application/pipeline_run.py")
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Call)
             and isinstance(n.func.value.func, ast.Name) and n.func.value.func.id == "_pub"]
    assert calls, "the terminal publication path vanished"
    assert {c.func.attr for c in calls} == {"closing"}, \
        f"pipeline_run uses the synchronous publisher for more than terminal closings: " \
        f"{sorted({c.func.attr for c in calls})}"
