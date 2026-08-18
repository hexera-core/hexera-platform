# Responsibility: Verify each tool family stays inside the workspace, returning a crash as feedback, never raising.
from __future__ import annotations

import json

import pytest
from tests._geometry_support import materialized

from meshpipeline.agents.builder import tools as T
from meshpipeline.agents.builder.tool_context import BuilderToolContext
from meshpipeline.engines.registry import engine_names, get_spec

ENGINES = sorted(engine_names())


def _ctx(tmp_path, *, engine="cfmesh", geometry=True, job_id=""):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    geo = materialized(tmp_path / "mat") if geometry else None
    return BuilderToolContext(workspace=ws, geometry=geo, job_id=job_id, execution_id="exec-1",
                              execution_generation=1, engine=engine, mesh_fidelity="standard",
                              loop_deadline=None)


def _call(ctx, name, **args) -> dict:
    return json.loads(T._dispatch_tool(ctx, name, args))


# workspace family

def test_write_then_read_round_trips_through_the_dispatcher(tmp_path):
    ctx = _ctx(tmp_path)
    out = _call(ctx, "write_file", path="notes/sizing.txt", content="cell = 0.05")
    assert out == {"written": "notes/sizing.txt", "bytes": 11, "operation": "created"}
    assert _call(ctx, "read_file", path="notes/sizing.txt")["content"] == "cell = 0.05"
    again = _call(ctx, "write_file", path="notes/sizing.txt", content="cell = 0.02")
    assert again["operation"] == "updated"


def test_a_protected_file_cannot_be_overwritten_through_the_dispatcher(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.workspace / "request.txt").write_text("the approved brief")
    out = _call(ctx, "write_file", path="request.txt", content="rewritten by the model")
    assert "protected" in out["error"]
    assert (ctx.workspace / "request.txt").read_text() == "the approved brief", (
        "an approved context file was overwritten")


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "../escape.txt", "content": "x"}),
    ("read_file", {"path": "../../etc/passwd"}),
    ("list_directory", {"path": "../.."}),
])
def test_the_workspace_confinement_holds_on_every_workspace_tool(tmp_path, tool, args):
    out = _call(_ctx(tmp_path), tool, **args)
    assert "escape" in out["error"].lower(), out


def test_listing_a_file_is_an_error_result_not_a_crash(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.workspace / "flow_topology").write_text("external")
    out = _call(ctx, "list_directory", path="flow_topology")
    assert "not a directory" in out["error"].lower() and "read_file" in out["error"]


def test_list_directory_defaults_to_the_workspace_root(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.workspace / "a.txt").write_text("a")
    assert "a.txt" in _call(ctx, "list_directory")["entries"]


# geometry family

def test_a_geometric_tool_refuses_a_run_that_carries_no_geometry(tmp_path):
    ctx = _ctx(tmp_path, geometry=False)
    (ctx.workspace / "input.stl").write_text("solid decoy\nendsolid decoy\n")
    for tool in ("geometry_report", "measure_scales"):
        out = _call(ctx, tool, **({} if tool == "geometry_report" else {}))
        assert "error" in out, f"{tool} answered from the workspace with no verified geometry"


def test_measure_scales_confines_its_geometry_file_argument(tmp_path):
    out = _call(_ctx(tmp_path), "measure_scales", geometry_file="../../etc/passwd")
    assert "escape" in out["error"].lower()


def test_measure_scales_reports_a_missing_surface_as_feedback(tmp_path):
    out = _call(_ctx(tmp_path), "measure_scales")
    assert "not found" in out["error"]


@pytest.mark.parametrize("engine", ENGINES)
def test_geometry_report_reaches_the_engines_own_inspector(tmp_path, engine, monkeypatch):
    import meshpipeline.engines.runtime as runtime
    seen = {}
    real = runtime.get_engine

    def _spy(name, *_a, **_k):
        eng = real(name)

        class _Proxy:
            def __getattr__(self, attr): return getattr(eng, attr)

            def inspect_stl(self, workspace, *, context=None):
                seen["engine"] = name
                seen["context"] = context
                return {"ok": True}
        return _Proxy()
    monkeypatch.setattr(runtime, "get_engine", _spy)

    ctx = _ctx(tmp_path, engine=engine)
    assert _call(ctx, "geometry_report") == {"ok": True}
    assert seen["engine"] == engine, f"geometry_report consulted {seen['engine']}, not {engine}"
    assert seen["context"] is ctx, "the verified geometry context did not reach the engine"


# meshing family

@pytest.mark.parametrize("engine", ENGINES)
def test_run_mesh_refuses_before_the_engines_required_files_exist(tmp_path, engine):
    required = get_spec(engine).run_policy.required_files
    if not required:
        pytest.skip(f"{engine} declares no required files")
    out = _call(_ctx(tmp_path, engine=engine), "run_mesh")
    assert "error" in out
    assert any(r.split("/")[-1] in out["error"] for r in required), out


@pytest.mark.parametrize("engine", ENGINES)
def test_configure_mesh_is_offered_only_where_the_engine_declares_it(tmp_path, engine):
    declared = "configure_mesh" in set(get_spec(engine).tool_names)
    offered = any(t["function"]["name"] == "configure_mesh" for t in T._active_tools(engine))
    assert offered == declared


def test_submit_mesh_reaches_the_meshing_family(tmp_path, monkeypatch):
    import meshpipeline.agents.builder.tools.meshing as M
    called = {}

    def _spy(ctx, args=None):
        # submit_mesh now declares parameters (the per-flag declaration a dispute requires), so the
        # route passes the call's arguments through to it like every other parameterised tool.
        called["ctx"], called["args"] = ctx, args
        return {"ok": 1}
    monkeypatch.setattr(M, "submit_mesh", _spy)
    ctx = _ctx(tmp_path)
    assert _call(ctx, "submit_mesh") == {"ok": 1}
    assert called["ctx"] is ctx, "submit_mesh did not reach the meshing family"


# research family

def test_run_python_computes_and_returns_stdout(tmp_path):
    out = _call(_ctx(tmp_path), "run_python", code="print(round(2.0 ** 0.5, 4))")
    assert out.get("exit_code") == 0, out
    assert "1.4142" in out["stdout"]


def test_run_python_reverts_a_write_to_approved_context(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.workspace / "request.txt").write_text("the approved brief")
    out = _call(ctx, "run_python",
                code="open('request.txt','w').write('rewritten by model code')")
    assert (ctx.workspace / "request.txt").read_text() == "the approved brief", (
        "model code persisted a mutation to approved context")
    assert "REVERTED" in out.get("error", ""), out


def test_run_python_rejects_network_and_subprocess_statically(tmp_path):
    for code in ("import socket", "import subprocess", "import os; os.environ['DATABASE_URL']"):
        out = _call(_ctx(tmp_path), "run_python", code=code)
        assert "error" in out, f"the safety scan admitted {code!r}"


def test_web_search_failure_is_model_feedback_not_a_crash(tmp_path, monkeypatch):
    import meshpipeline.agent_tools.shared.web_search as ws

    async def _boom(query, job_id=""):
        raise RuntimeError("provider down")
    monkeypatch.setattr(ws, "web_search", _boom)
    out = _call(_ctx(tmp_path), "web_search", query="cfMesh boundary layer guidance")
    assert "error" in out and "web_search failed" in out["error"]


# cross-cutting dispatch

def test_an_unknown_tool_is_an_error_result(tmp_path):
    assert "Unknown tool" in _call(_ctx(tmp_path), "definitely_not_a_tool")["error"]


def test_a_missing_required_argument_names_the_argument(tmp_path):
    out = _call(_ctx(tmp_path), "write_file", path="x.txt")   # no content
    assert "Missing required argument for write_file" in out["error"]
    assert "content" in out["error"]


def test_a_crashing_tool_is_returned_as_feedback_never_killing_the_node(tmp_path, monkeypatch):
    import meshpipeline.agents.builder.tools.workspace as W

    def _boom(*a, **k):
        raise NotADirectoryError("not a directory")
    monkeypatch.setattr(W, "list_directory", _boom)
    out = _call(_ctx(tmp_path), "list_directory", path=".")
    assert "list_directory failed: NotADirectoryError" in out["error"]


def test_an_oversized_tool_result_is_capped_with_a_notice(tmp_path, monkeypatch):
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "MAX_TOOL_OUTPUT_CHARS", 200)
    ctx = _ctx(tmp_path)
    (ctx.workspace / "big.txt").write_text("x" * 5000)
    out = _call(ctx, "read_file", path="big.txt")
    assert "too large to return" in out["error"] and out["output_chars"] > 200


def test_a_broken_tracer_never_breaks_a_tool_call(tmp_path, monkeypatch):
    import meshpipeline.capture.trace as trace

    def _boom(*a, **k):
        raise RuntimeError("collector down")
    monkeypatch.setattr(trace, "add_event", _boom)
    out = _call(_ctx(tmp_path, job_id="job-9"), "write_file", path="a.txt", content="hi")
    assert out["written"] == "a.txt"
