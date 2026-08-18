# Responsibility: Verify builder tool dispatch gates submission, turns a tool crash into a result, and caps timeout.
from __future__ import annotations

from pathlib import Path

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"

from meshpipeline.agents.builder.tools import _tool_submit_mesh  # noqa: E402
from meshpipeline.engines.registry import engine_names, get_spec  # noqa: E402


def _ctx(workspace, *, engine="cfmesh", job_id="", geometry=None, with_geometry=False,
         loop_deadline=None):
    from pathlib import Path

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    if with_geometry and geometry is None:
        from tests._geometry_support import materialized
        geometry = materialized(Path(workspace) / "_geom")
    return BuilderToolContext(workspace=Path(workspace), geometry=geometry,
                              job_id=job_id, engine=engine, loop_deadline=loop_deadline)


def test_submit_mesh_gates_on_the_named_engines_marker(tmp_path):
    for name in engine_names():
        marker = get_spec(name).run_policy.submit_marker
        ws = tmp_path / name
        ws.mkdir()
        assert _tool_submit_mesh(_ctx(ws, engine=name))["success"] is False, \
            f"{name}: submit must fail without its deliverable ({marker})"
        target = ws / marker
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        assert _tool_submit_mesh(_ctx(ws, engine=name))["success"] is True, \
            f"{name}: submit must succeed once {marker} exists"


# tool crashes are model feedback, never node death (live aorta run died on this)

def test_list_directory_on_a_file_returns_an_error_not_an_exception(tmp_path):
    from meshpipeline.agents.builder.tools import _tool_list_directory
    (tmp_path / "flow_topology").write_text("internal")
    out = _tool_list_directory(tmp_path, "flow_topology")
    assert "error" in out and "Not a directory" in out["error"]


def test_dispatch_tool_converts_any_tool_crash_into_an_error_result(tmp_path, monkeypatch):
    import meshpipeline.agents.builder.tools as T
    def _boom(ws, path):
        raise NotADirectoryError(20, "Not a directory", str(ws / path))
    monkeypatch.setattr(T.workspace, "list_directory", _boom)
    import json as _json
    out = _json.loads(T._dispatch_tool(_ctx(tmp_path, engine="vmtk"), "list_directory", {"path": "x"}))
    assert "error" in out and "NotADirectoryError" in out["error"]


# run_mesh budget must FIT the remaining loop budget (forensic: 4 attempts killed
# at exactly BUILDER_LOOP_TIMEOUT with zero remote returns)

def test_run_mesh_refuses_dispatch_when_loop_budget_too_small(tmp_path, monkeypatch):
    import time

    from meshpipeline.agents.builder import tools as T
    from meshpipeline.engines.registry import get_spec
    for f in get_spec("snappy_multiregion").run_policy.required_files:
        p = tmp_path / f; p.parent.mkdir(parents=True, exist_ok=True); p.write_text("{}")
    def _never(*a, **k):
        raise AssertionError("must not dispatch with insufficient budget")
    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine",
                        lambda n="": type("E", (), {"name": "snappy_multiregion",
                                                    "run_cartesian_mesh": _never})())
    out = T._tool_run_mesh(_ctx(tmp_path, engine="snappy_multiregion", with_geometry=True,
                                loop_deadline=time.monotonic() + 200))   # < reserve+min
    assert out["error"] == "not_enough_loop_budget_for_remote_run"
    assert out["required_reserve_seconds"] == T._RUN_MESH_FEEDBACK_RESERVE
    assert "guidance" in out


def test_run_mesh_caps_dispatch_timeout_to_remaining_budget(tmp_path, monkeypatch):
    import time

    from meshpipeline.agents.builder import tools as T
    from meshpipeline.engines.registry import get_spec
    for f in get_spec("snappy_multiregion").run_policy.required_files:
        p = tmp_path / f; p.parent.mkdir(parents=True, exist_ok=True); p.write_text("{}")
    seen = {}
    class _E:
        name = "snappy_multiregion"
        def run_cartesian_mesh(self, ws, *, timeout, context=None):
            seen["timeout"] = timeout
            return {"timed_out": True, "rc": -1, "log_tail": ""}
    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine", lambda n="": _E())
    remaining = 1000
    out = T._tool_run_mesh(_ctx(tmp_path, engine="snappy_multiregion", with_geometry=True,
                                loop_deadline=time.monotonic() + remaining))
    # engine wants 3000s; only remaining-reserve fits - and the TIMEOUT VERDICT returns
    assert seen["timeout"] <= remaining - T._RUN_MESH_FEEDBACK_RESERVE
    assert out["timed_out"] is True and "guidance" in out
