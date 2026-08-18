# Responsibility: Verify the reviewer writes exactly its allow-listed keys, holding no execution or scheduling power.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests._scan import scanned

import meshpipeline.agents.reviewer.visual as visual

REPO = Path(__file__).parents[4]
VISUAL = REPO / "src" / "meshpipeline" / "agents" / "reviewer" / "visual.py"

EXPECTED_ALLOW_LIST = frozenset({
    # the parent-mesh review's per-flag baseline: the Reviewer measured it, so the Reviewer owns it
    "dispute_flag_findings",
    "reviewer_result", "reviewer_verdict", "reviewer_feedback", "reviewer_axis_findings",
    "reviewer_rebuild_required", "reviewer_tool_calls", "api_failure",
    # v8: the Reviewer's OWN canonical accountability record. Append-only history about the
    # review's execution - it carries no downstream truth and is never read back as a verdict.
    "agent_run_records",
})

# truth owned by other stages - the Reviewer must never be able to return any of these.
FORBIDDEN_KEYS = frozenset({
    "executor_success", "executor_failed_gate", "executor_output", "mesh_manifest",
    "engine", "purpose", "input_kind", "dimensionality", "mesh_fidelity", "intake_patches",
    "approved_snapshot_id", "classifier_result", "builder_mode", "retry_count",
    "solvability_failed", "geometry_unsuitable_reason", "final_result", "outcome_message",
    "schema_version",
})

# node_reviewer's returns route through exactly these constructors (each terminates in _reviewer_return)
# `_early_nonverdict` is a thin delegate to `_nonverdict` for the one refusal issued before
# the artifact is known to exist (so before its unit can be read). It builds no state of its
# own - it forwards to the same constructor - so it belongs on this list.
_RETURN_BUILDERS = {"_reviewer_return", "_translate_outcome", "_nonverdict",
                    "_early_nonverdict", "_render_failure_result"}


# static: the production allow-list is exactly what we intend
def test_reviewer_return_keys_is_exactly_the_intended_allow_list():
    assert visual._REVIEWER_RETURN_KEYS == EXPECTED_ALLOW_LIST, (
        "the Reviewer write-surface allow-list drifted:\n"
        f"  added   : {sorted(set(visual._REVIEWER_RETURN_KEYS) - EXPECTED_ALLOW_LIST)}\n"
        f"  removed : {sorted(EXPECTED_ALLOW_LIST - set(visual._REVIEWER_RETURN_KEYS))}")


def test_allow_list_excludes_every_downstream_truth_key():
    leaked = FORBIDDEN_KEYS & set(visual._REVIEWER_RETURN_KEYS)
    assert not leaked, f"the Reviewer allow-list contains keys owned by other stages: {sorted(leaked)}"


# AST: every node_reviewer return routes through the allow-list constructors
def _node_reviewer_returns() -> list[ast.Return]:
    tree = ast.parse(VISUAL.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "node_reviewer")
    # exclude returns inside the nested `_read_txt` string helper - they are not STATE returns
    nested = {n for sub in ast.walk(fn)
              if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not fn
              for n in ast.walk(sub)}
    return [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n not in nested]


def test_every_node_reviewer_return_routes_through_the_allow_list():
    offenders = []
    for r in _node_reviewer_returns():
        v = r.value
        # the publishing constructors are awaited now; the constructor is what must be checked
        if isinstance(v, ast.Await):
            v = v.value
        ok = isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in _RETURN_BUILDERS
        if not ok:
            offenders.append((r.lineno, ast.dump(v) if v else "None"))
    assert not offenders, (
        f"node_reviewer has state return(s) bypassing the allow-list constructors: {offenders}")


def test_no_bare_return_dict_literal_in_node_reviewer():
    for r in _node_reviewer_returns():
        assert not isinstance(r.value, ast.Dict), (
            f"bare return-dict literal at visual.py:{r.lineno} - build it via _reviewer_return")


# runtime: the filter rejects unauthorized keys loudly, accepts the allow-list
def test_reviewer_return_rejects_every_forbidden_key_at_runtime():
    for key in sorted(FORBIDDEN_KEYS | {"totally_unknown_key"}):
        try:
            visual._reviewer_return(**{key: True})
        except AssertionError:
            continue
        raise AssertionError(f"_reviewer_return accepted forbidden key {key!r}")


def test_reviewer_return_accepts_the_full_allow_list():
    out = visual._reviewer_return(**dict.fromkeys(EXPECTED_ALLOW_LIST, 0))
    assert set(out) == EXPECTED_ALLOW_LIST


# authority invariants around the Reviewer
def test_reviewer_package_does_not_import_terminal_or_execution_authority():
    pkg = REPO / "src" / "meshpipeline" / "agents" / "reviewer"
    banned = ("final_result", "FinalResult", "celery", "cloud_run", "pipeline.executor")
    offenders: list[str] = []
    for py in scanned(pkg.glob("*.py"), "the Reviewer package modules"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = mod + " " + " ".join(a.name for a in node.names)
                for b in banned:
                    if b in names:
                        offenders.append(f"{py.name}: {names.strip()}")
    assert not offenders, f"Reviewer reached for terminal/execution authority: {offenders}"


def test_reviewer_tools_contain_no_code_execution_or_scheduling_authority():
    pkg = REPO / "src" / "meshpipeline" / "agents" / "reviewer"
    banned_calls = ("subprocess", "os.system", "Popen", "run_python", "apply_async", ".delay(")
    for fname in ("tools.py", "render_runtime.py", "unified.py"):
        src = (pkg / fname).read_text()
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert "subprocess" not in imported, f"{fname} imports subprocess"
        for b in banned_calls:
            assert b not in src, f"{fname} references {b!r}"


# the canonical axis-findings shape is a typed LIST with a deterministic per-axis verdict
def test_findings_dump_is_the_canonical_typed_list():
    import types

    from meshpipeline.agents.reviewer.eligibility import AxisFinding
    from meshpipeline.agents.reviewer.unified import UnifiedReviewOutcome
    from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus

    ledger = EvidenceLedger()
    bad = ledger.add_gate("rc", EvidenceStatus.FAIL, "failed", "executor")     # grounds a defect
    ok = ledger.add_gate("q", EvidenceStatus.PASS, "passed", "executor")       # passing evidence
    plan = types.SimpleNamespace(axes=(
        types.SimpleNamespace(name="quality", owner="engine:cfmesh"),
        types.SimpleNamespace(name="conformance", owner="purpose:external_cfd"),
    ))
    outcome = UnifiedReviewOutcome(verdict="FAIL", findings=(
        # `satisfied` is the reviewer's per-axis judgement; the persisted `passed` is derived
        # from it AND the evidence, by the same rule the verdict is.
        AxisFinding("quality", "skew too high", (bad,), passed=False),
        AxisFinding("conformance", "patches fine", (ok,), passed=True),
    ))
    dump = visual._findings_dump(plan, ledger, outcome, attempt=2)
    assert isinstance(dump, list) and len(dump) == 2
    by_axis = {e["axis_key"]: e for e in dump}
    assert set(by_axis["quality"]) == {"axis_key", "owner", "passed", "finding", "evidence_ids", "attempt"}
    assert by_axis["quality"]["passed"] is False and by_axis["quality"]["owner"] == "engine:cfmesh"
    assert by_axis["conformance"]["passed"] is True     # cites only passing evidence → not a defect
    assert by_axis["quality"]["attempt"] == 2 and by_axis["quality"]["evidence_ids"] == [bad]


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
