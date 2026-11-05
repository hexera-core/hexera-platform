# Responsibility: Verify a route is transport - it opens no transaction and reinterprets no gate policy.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
ROUTES = sorted((SRC / "api").rglob("*.py"))

#: Primitives no route may perform itself: the approval-transaction primitives, and the
#: intake-gate primitives now that `agents/intake/message` owns them. Deliberately NOT included:
#: `simulation.py` builds its own dispatch payload, which is a Builder boundary.
FORBIDDEN_IN_ROUTES = {
    # approval transaction
    "get_for_update":            "session row locking",
    "link_job":                  "job linking",
    "mark_launch_failed":        "durable launch-failure recording",
    "approved_intent_canonical": "approved-intent binding",
    "builder_payload":           "approved-snapshot payload extraction",
    "purged_at":                 "retention-expiry policy",
    # intake-gate transitions /
    "set_intake_gate":           "intake-gate mutation",
    "bind_geometry_interpretation": "durable unit binding",
    "append_intake_event":       "intake event-channel writes",
}


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("route", ROUTES, ids=lambda p: p.name)
def test_a_route_performs_no_application_transaction(route):
    src = _src(route)
    found = [f"{tok} ({why})" for tok, why in FORBIDDEN_IN_ROUTES.items() if tok in src]
    assert not found, (
        f"{route.relative_to(SRC)} performs application transaction work itself: "
        + ", ".join(found))


def test_the_approval_transaction_has_exactly_one_implementation():
    owners = [p for p in sorted(SRC.rglob("*.py")) if "get_for_update" in _src(p)]
    names = {p.relative_to(SRC).as_posix() for p in owners}
    # the message authority takes the SAME lock, deliberately - a message and a
    # confirmation must not interleave, and that is only true if they contend for one row.
    assert names <= {"agents/intake/approval.py", "agents/intake/message.py",
                     "persistence/repositories/session_repository.py"}, (
        f"the session row lock is taken outside the intake authority and its repository: "
        f"{sorted(names)}")


def test_no_second_approval_service_was_created():
    assert not (SRC / "application" / "approval.py").exists(), (
        "application/approval.py exists - the audit established agents/intake/approval as the "
        "single approval authority")


def test_the_intake_approval_authority_imports_no_transport():
    src = _src(SRC / "agents" / "intake" / "approval.py")
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = [m for m in imported
              if m.startswith(("fastapi", "starlette", "meshpipeline.api"))]
    assert not banned, f"the approval authority imports transport: {banned}"


def test_the_route_module_does_not_import_the_persistence_repositories_for_approval():
    src = _src(SRC / "api" / "v1" / "chat.py")
    assert "JobRepository" not in src, "the chat route reaches for the job repository again"
    assert "GeometrySourceRepository" not in src, (
        "the chat route reads the geometry source rows again")


def test_no_deferred_import_hides_the_approval_transaction():
    src = _src(SRC / "api" / "v1" / "chat.py")
    import inspect

    from meshpipeline.api.v1 import chat

    route = inspect.getsource(chat._confirm_pending_approval)
    for banned in ("meshpipeline.persistence", "meshpipeline.application"):
        assert banned not in route, (
            f"the approval route defers a {banned} import to hide a transaction")
    assert "get_db" not in route, "the approval route opens its own session again"
    assert src.count("get_for_update") == 0



GATE_WRITERS = {"set_intake_gate", "bind_geometry_interpretation", "append_intake_event"}


def test_no_api_module_writes_an_intake_gate():
    offenders = []
    for route in ROUTES:
        src = _src(route)
        hits = sorted(w for w in GATE_WRITERS if w in src)
        if hits:
            offenders.append(f"{route.relative_to(SRC)}: {', '.join(hits)}")
    assert not offenders, "API modules mutate the intake gate: " + "; ".join(offenders)


def test_no_api_module_imports_a_persistence_repository_at_module_scope():
    for route in ROUTES:
        tree = ast.parse(_src(route))
        for node in tree.body:
            mod = (node.module if isinstance(node, ast.ImportFrom) else None) or ""
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else [])
            assert not mod.startswith("meshpipeline.persistence.repositories"), (
                f"{route.relative_to(SRC)} imports a repository at module scope: {mod}")
            assert not any(n.startswith("meshpipeline.persistence.repositories") for n in names), (
                f"{route.relative_to(SRC)} imports a repository at module scope")


def test_message_driven_gate_transitions_have_exactly_one_authority():
    owners = [p for p in sorted(SRC.rglob("*.py"))
              if "set_intake_gate" in _src(p) and p.name != "session_repository.py"]
    names = {p.relative_to(SRC).as_posix() for p in owners}
    assert names <= {"agents/intake/message.py", "agents/intake/approval.py"}, (
        f"the intake gate is written outside the intake authority: {sorted(names)}")


def test_the_approval_primitives_stayed_with_the_approval_authority():
    approval = _src(SRC / "agents" / "intake" / "approval.py")
    for owned in ("def verify", "def create", "def invalidate", "def defer",
                  "def builder_payload", "async def confirm_pending_approval"):
        assert owned in approval, f"approval policy left its authority: {owned}"
    message = _src(SRC / "agents" / "intake" / "message.py")
    for not_owned in ("def verify(", "def create(", "def builder_payload(",
                      "check_quotas", "link_job"):
        assert not_owned not in message, (
            f"the message authority re-implements approval policy: {not_owned}")


def test_the_intake_message_authority_imports_no_transport():
    tree = ast.parse(_src(SRC / "agents" / "intake" / "message.py"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = [m for m in imported
              if m.startswith(("fastapi", "starlette", "meshpipeline.api"))]
    assert not banned, f"the intake message authority imports transport: {banned}"


def test_no_second_intake_message_service_was_created():
    assert not (SRC / "application" / "chat_service.py").exists()
    assert not (SRC / "application" / "intake_message.py").exists(), (
        "a competing generic application service was created; agents/intake/message is the "
        "message authority")


#: Gate-policy functions the route must never CALL. Matched on the call graph, not on text: the
#: route's own docstring explains that it does not invalidate or defer anything, and a rule that
#: fails on its own explanation is a rule people delete.
GATE_POLICY_CALLS = {"is_live", "classify", "invalidate", "defer", "needs_confirmation",
                     "already_asked", "asked", "answered", "record", "set_intake_gate"}


def test_the_route_maps_typed_outcomes_and_reinterprets_no_gate_policy():
    import inspect

    from meshpipeline.api.v1 import chat

    tree = ast.parse(inspect.getsource(chat.chat_message))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    leaked = sorted(called & GATE_POLICY_CALLS)
    assert not leaked, f"the chat route decides gate policy again: {leaked}"
    assert {"accept", "persist_turn"} <= called, (
        "the route no longer delegates to the intake message authority")
