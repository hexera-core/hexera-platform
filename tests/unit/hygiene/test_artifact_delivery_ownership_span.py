# Responsibility: Verify artifact delivery runs inside an execution-ownership span the orchestrator owns.
# Boundaries: the shape of the call site - whether ownership is really bound at runtime is the integration tier.
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"
ORCHESTRATOR = SRC / "application" / "pipeline_run.py"
UPLOADER = SRC / "application" / "artifact_uploader.py"

DELIVER = "deliver_succeeded_run"
BINDER = "execution_ownership"
OWNER_CHECK = "is_current_owner"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        got = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if got == name:
            out.append(n)
    return out


def _binding_spans(tree: ast.Module) -> list[ast.With]:
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.With) and any(_calls(item.context_expr, BINDER) for item in n.items)]


def _delivery_span(tree: ast.Module) -> ast.With:
    spans = [w for w in _binding_spans(tree) if _calls(w, DELIVER)]
    assert len(spans) == 1, (
        f"expected exactly one execution_ownership span around {DELIVER}; found {len(spans)}")
    return spans[0]


# the delivery has one orchestration caller, and it binds


def test_the_uploader_delivery_has_exactly_one_production_caller():
    callers = [p for p in sorted(SRC.rglob("*.py"))
               if p != UPLOADER and _calls(_tree(p), DELIVER)]
    assert [p.name for p in callers] == ["pipeline_run.py"], (
        "artifact delivery is invoked from somewhere other than the orchestrator; each caller "
        f"would need its own ownership span. Found: {[p.name for p in callers]}")


def test_the_delivery_call_is_inside_an_execution_ownership_span():
    tree = _tree(ORCHESTRATOR)
    span = _delivery_span(tree)
    assert len(_calls(span, DELIVER)) == 1, "the span wraps more than one delivery call"
    # every delivery call in the module is the one inside the span
    assert len(_calls(tree, DELIVER)) == 1


def test_the_span_binds_the_claimed_ownership_and_the_existing_session_authority():
    span = _delivery_span(_tree(ORCHESTRATOR))
    binder = _calls(span.items[0].context_expr, BINDER)[0]
    assert [ast.unparse(a) for a in binder.args] == ["ownership"], (
        "the span binds something other than the ownership the claim returned; it must not "
        "reconstruct, re-query or fabricate one")
    kwargs = {k.arg: ast.unparse(k.value) for k in binder.keywords}
    assert kwargs.get("session_factory") == "AsyncSessionLocal", \
        "the span does not use the orchestrator's existing session authority"


# ordering: the current-owner check dominates the binding


def test_the_current_owner_check_precedes_the_binding():
    tree = _tree(ORCHESTRATOR)
    span = _delivery_span(tree)
    checks = _calls(tree, OWNER_CHECK)
    assert checks, f"the {OWNER_CHECK} authority call is gone"
    assert min(c.lineno for c in checks) < span.lineno, (
        f"the ownership span opens before {OWNER_CHECK}. The binding is context propagation, not "
        "authorization - a superseded worker must be refused BEFORE it can deliver anything.")


def test_the_owner_check_is_not_itself_inside_the_span():
    span = _delivery_span(_tree(ORCHESTRATOR))
    assert not _calls(span, OWNER_CHECK), \
        "the current-owner check moved inside the span it is supposed to gate"


# the span stays narrow


def test_terminal_and_outbox_work_stays_outside_the_span():
    span = _delivery_span(_tree(ORCHESTRATOR))
    inside = ast.unparse(span)
    for forbidden in ("finalize_", "terminal_finalize", "publish_terminal", "outbox",
                      "apply_delivery", "closing("):
        assert forbidden not in inside, (
            f"{forbidden!r} is inside the artifact-delivery ownership span. Terminal authority "
            "must be able to speak when the execution no longer owns the job.")


def test_the_span_contains_only_the_delivery_call():
    span = _delivery_span(_tree(ORCHESTRATOR))
    assert len(span.body) == 1, f"the span has {len(span.body)} statements; it must wrap only delivery"
    stmt = span.body[0]
    assert isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Await), \
        "the span's single statement is not the awaited delivery"


# the uploader stays neutral


def test_the_uploader_never_touches_the_ownership_context():
    src = UPLOADER.read_text()
    tree = _tree(UPLOADER)
    for banned in (BINDER, "current_ownership", "ExecutionOwnership", "claim_execution",
                   "claim_delivery", "LeaseRepository"):
        assert banned not in src, (
            f"artifact_uploader references {banned!r}. It is a reusable delivery module: the "
            "orchestration call site owns the execution lifetime, the uploader stays neutral.")
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and "execution_fence" in n.module:
            raise AssertionError("artifact_uploader imports the execution fence")
        assert not (isinstance(n, ast.With)
                    and any(_calls(i.context_expr, BINDER) for i in n.items)), \
            "artifact_uploader opens an ownership span of its own"


def test_the_uploader_still_publishes_through_the_injected_callback():
    tree = _tree(UPLOADER)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == DELIVER)
    assert "publish" in [a.arg for a in fn.args.kwonlyargs], \
        "the uploader no longer takes its publisher from the caller"
