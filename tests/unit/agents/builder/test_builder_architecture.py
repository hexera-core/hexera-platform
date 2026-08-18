# Responsibility: Verify the builder's dispatchable tool surface is exactly the bounded set, changing no intent.
from __future__ import annotations

import ast
from pathlib import Path

from tests._scan import scanned

import meshpipeline.agents.builder.tools as tools


def _ctx(workspace, *, engine="cfmesh", job_id="", geometry=None):
    from pathlib import Path

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    return BuilderToolContext(workspace=Path(workspace), geometry=geometry,
                              job_id=job_id, engine=engine)

REPO = Path(__file__).parents[4]
BUILDER_PKG = REPO / "src" / "meshpipeline" / "agents" / "builder"

# The Builder's ENTIRE dispatchable tool surface. Bounded, enumerated, and asserted so a new tool
# cannot ship silently - every name here maps to a bounded, workspace-confined action.
EXPECTED_DISPATCH_TOOLS = frozenset({
    "write_file", "read_file", "list_directory", "web_search", "geometry_report",
    "measure_scales", "configure_mesh", "run_mesh", "run_python", "submit_mesh",
})


def _dispatch_tool_names() -> set[str]:
    from meshpipeline.agents.builder import tools as _T

    return set(_T._ROUTES)


def test_builder_dispatch_tool_surface_is_exactly_the_bounded_set():
    assert _dispatch_tool_names() == EXPECTED_DISPATCH_TOOLS, (
        "the Builder's dispatchable tool surface drifted:\n"
        f"  added   : {sorted(_dispatch_tool_names() - EXPECTED_DISPATCH_TOOLS)}\n"
        f"  removed : {sorted(EXPECTED_DISPATCH_TOOLS - _dispatch_tool_names())}")


def test_builder_retains_run_mesh_it_is_not_a_spec_only_stub():
    assert "run_mesh" in EXPECTED_DISPATCH_TOOLS
    assert "run_mesh" in _dispatch_tool_names()
    assert hasattr(tools, "_tool_run_mesh")


def test_unknown_tool_name_is_contained_not_executed(tmp_path):
    import json
    for hostile in ("set_executor_success", "schedule_cloud_run_job", "celery_delay",
                    "set_final_result", "change_engine", "approve_patches"):
        out = json.loads(tools._dispatch_tool(_ctx(tmp_path, engine="cfmesh"), hostile, {}))
        assert out == {"error": f"Unknown tool: {hostile}"}, out


def test_builder_has_no_tool_to_change_approved_intent():
    forbidden_fragments = ("engine", "purpose", "patch", "intent", "dimension", "approve",
                           "final", "verdict", "executor", "schedule", "deploy", "celery", "cloud_run")
    for name in EXPECTED_DISPATCH_TOOLS:
        low = name.lower()
        # the legitimate tools are about geometry/mesh authoring + execution, never approval/terminal
        assert not any(frag in low for frag in forbidden_fragments), name


def test_builder_package_does_not_import_terminal_or_scheduling_authority():
    banned = ("final_result", "FinalResult", "celery", "apply_async", ".delay(")
    offenders: list[str] = []
    for py in scanned(BUILDER_PKG.glob("*.py"), "the Builder package modules"):
        text = py.read_text()
        tree = ast.parse(text)
        # only flag IMPORTS / call sites, not prose in comments/docstrings
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = mod + " " + " ".join(a.name for a in node.names)
                for b in ("final_result", "FinalResult", "celery"):
                    if b in names:
                        offenders.append(f"{py.name}: import {names.strip()}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("delay", "apply_async"):
                    offenders.append(f"{py.name}: .{node.func.attr}() call")
    assert not offenders, f"Builder reached for terminal/scheduling authority: {offenders}"


def test_active_tools_are_a_subset_of_the_dispatchable_set():
    from meshpipeline.engines.registry import all_engine_names
    for engine in all_engine_names():
        for t in tools._active_tools(engine):
            name = t.get("function", {}).get("name")
            # configure_mesh's engine palette IS configure_mesh (same dispatch name)
            assert name in EXPECTED_DISPATCH_TOOLS or name == "configure_mesh", (engine, name)
