# Responsibility: Verify the builder writes exactly its allow-listed keys, and never a downstream truth key.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import meshpipeline.agents.builder.agent as agent

REPO = Path(__file__).parents[4]
BUILDER_AGENT = REPO / "src" / "meshpipeline" / "agents" / "builder" / "agent.py"
EXECUTOR = REPO / "src" / "meshpipeline" / "pipeline" / "executor.py"

# The exact, intended Builder write surface. Bookkeeping + authoring outputs only - NO downstream
# truth. Kept as a literal here (not imported) so a change to the production allow-list must be made
# CONSCIOUSLY in two places, and this test is the second reviewer of that change.
EXPECTED_ALLOW_LIST = frozenset({
    # what the builder declares it changed per engineer-flagged region (a claim the review verifies)
    "builder_flag_responses",
    "retry_count", "builder_noop_count", "openfoam_workspace",
    "request_txt", "review_brief_txt", "api_failure", "builder_deadline_epoch",
})

# Truth owned by other pipeline stages - the Builder must never be able to return any of these.
FORBIDDEN_KEYS = frozenset({
    "executor_success", "executor_failed_gate", "executor_output", "mesh_manifest",
    "reviewer_verdict", "reviewer_result", "reviewer_feedback", "reviewer_axis_findings",
    "reviewer_rebuild_required", "reviewer_patch_checks",
    "engine", "purpose", "input_kind", "dimensionality", "intake_patches",
    "flow_topology", "engine_params",
    "outcome_message", "solvability_failed", "geometry_unsuitable_reason",
    "schema_version", "final_result",
})


def _return_dict_keys(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    return keys


# static: the production allow-list is exactly what we intend
def test_builder_return_keys_is_exactly_the_intended_allow_list():
    assert agent._BUILDER_RETURN_KEYS == EXPECTED_ALLOW_LIST, (
        "the Builder's production write-surface allow-list drifted from the intended set:\n"
        f"  added   : {sorted(set(agent._BUILDER_RETURN_KEYS) - EXPECTED_ALLOW_LIST)}\n"
        f"  removed : {sorted(EXPECTED_ALLOW_LIST - set(agent._BUILDER_RETURN_KEYS))}\n"
        "If this change is intentional, update EXPECTED_ALLOW_LIST too - deliberately.")


def test_builder_allow_list_excludes_every_downstream_truth_key():
    leaked = FORBIDDEN_KEYS & set(agent._BUILDER_RETURN_KEYS)
    assert not leaked, (
        f"the Builder allow-list contains keys owned by other stages: {sorted(leaked)}. "
        "Execution/gate/reviewer/approved-intent/terminal truth is never the Builder's to write.")


# AST: no return in node_builder can bypass the runtime filter
def _node_builder_returns() -> list[ast.Return]:
    tree = ast.parse(BUILDER_AGENT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "node_builder":
            return [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    raise AssertionError("node_builder not found in agent.py")


def test_every_node_builder_return_goes_through_the_one_patch_path():
    returns = _node_builder_returns()
    assert returns, "node_builder has no return statements - the guard would be vacuous"
    def _ok(value) -> bool:
        if not isinstance(value, ast.Call):
            return False
        # an exit: `_patch(...)`
        if isinstance(value.func, ast.Name) and value.func.id == "_patch":
            return True
        # the single construction site itself: `TurnPatch(...).state()` inside `_patch`
        return isinstance(value.func, ast.Attribute) and value.func.attr == "state"

    offenders = [(r.lineno, ast.unparse(r.value)[:60]) for r in returns if not _ok(r.value)]
    assert not offenders, (
        "node_builder has return(s) that bypass the single patch path (so the runtime allow-list "
        f"cannot filter them): {offenders}.")


def test_the_single_patch_path_reaches_the_allow_list_guard():
    import inspect

    from meshpipeline.agents.builder import turn_patch

    node = inspect.getsource(agent.node_builder)
    assert "TurnPatch(" in node and ".state()" in node, (
        "the node's `_patch` no longer constructs a TurnPatch")
    assert "builder_state(" in inspect.getsource(turn_patch.TurnPatch.state), (
        "TurnPatch.state no longer routes through the allow-list guard")
    assert agent._builder_return is turn_patch.builder_state


def test_every_exit_carries_the_fields_that_must_survive_it():
    from meshpipeline.agents.builder.turn_patch import TurnPatch

    carried = {"openfoam_workspace", "request_txt", "review_brief_txt", "builder_deadline_epoch",
               "retry_count"}
    for api_failure in ("", "openai: 500"):
        state = TurnPatch(workspace="/w", request_txt="r", review_brief_txt="b",
                          deadline_epoch=123.0, retry_count=2,
                          api_failure=api_failure).state()
        assert carried <= set(state), (
            f"an exit with api_failure={api_failure!r} dropped {sorted(carried - set(state))}")


def test_builder_agent_has_no_bare_return_dict_literals_in_node_builder():
    for r in _node_builder_returns():
        assert not isinstance(r.value, ast.Dict), (
            f"bare return-dict literal at agent.py:{r.lineno} inside node_builder - construct it "
            "through _builder_return so the write-surface allow-list applies")


# runtime: the filter rejects unauthorized keys loudly
def test_builder_return_rejects_forbidden_keys_at_runtime():
    for key in sorted(FORBIDDEN_KEYS):
        try:
            agent._builder_return(**{key: True})
        except AssertionError:
            continue
        raise AssertionError(
            f"_builder_return accepted forbidden key {key!r} - the Builder can leak {key} into "
            "global pipeline state")


def test_builder_return_accepts_the_full_allow_list():
    out = agent._builder_return(**dict.fromkeys(EXPECTED_ALLOW_LIST, 0))
    assert set(out) == EXPECTED_ALLOW_LIST


def test_executor_is_a_writer_of_executor_success():
    assert "executor_success" in _return_dict_keys(EXECUTOR), (
        "node_executor no longer writes executor_success - execution truth has no author")


def test_builder_assigns_no_variable_named_executor_success():
    tree = ast.parse(BUILDER_AGENT.read_text())
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    assigned.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
    assert "executor_success" not in assigned, (
        "the Builder assigns a local `executor_success` - rename it to the authoring fact it "
        "really is, so it can never be mistaken for execution truth")


def test_node_builder_return_carries_only_allow_list_keys_at_runtime():
    import asyncio
    import types

    async def _fake_driver(workspace, state, *, job_id, publish, source_path):
        return True, "authored"

    class _Spec:
        build_driver = staticmethod(_fake_driver)

    import meshpipeline.agents.builder.attempt_capture as attempt_capture
    import meshpipeline.agents.builder.invoke as invoke

    orig_tl = attempt_capture.TrainingLogger
    invoke.get_spec = lambda *_a, **_k: _Spec()
    attempt_capture.TrainingLogger = lambda *a, **k: types.SimpleNamespace(
        log=lambda *a, **k: None)
    try:
        state = {
            "job_id": "j", "engine": "cfmesh", "retry_count": 0, "builder_mode": "initial",
            "geometry": {}, "request_txt": "r", "review_brief_txt": "",
            "intake_patches": [], "openfoam_workspace": "",
        }
        out = asyncio.run(agent.node_builder(state))
    finally:
        del invoke.get_spec
        attempt_capture.TrainingLogger = orig_tl

    assert isinstance(out, dict)
    extra = set(out) - EXPECTED_ALLOW_LIST
    assert not extra, f"node_builder returned keys outside its write surface at runtime: {sorted(extra)}"
    assert "executor_success" not in out
    # it DID author a spec and carries its own honest fields
    assert "openfoam_workspace" in out


# The builder node now publishes through the ownership-checked port. These tests exercise the
# node's own contract, not Redis or PostgreSQL, so the port is stood in for; nothing about
# ownership is asserted here - the gated publisher has its own suites.
@pytest.fixture(autouse=True)
def _execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install
    return install(monkeypatch, agent)
