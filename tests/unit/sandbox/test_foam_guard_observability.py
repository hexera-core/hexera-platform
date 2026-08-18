# Responsibility: Verify a guard refusal names its stage and category while leaking no file content or secret.
from __future__ import annotations

import json
from pathlib import Path

import meshpipeline.agents.builder.tools as tools
import meshpipeline.capture.trace as trace
from meshpipeline.sandbox.foam_case_guard import scan_case_dicts


def _ctx(workspace, *, engine="cfmesh", job_id="", geometry=None):
    from pathlib import Path

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    return BuilderToolContext(workspace=Path(workspace), geometry=geometry,
                              job_id=job_id, engine=engine)


def _poisoned(tmp_path: Path, target: str) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text(f'#include "{target}"\nmaxCellSize 0.1;\n')
    return tmp_path


def test_refusal_reason_records_stage_and_construct_category(tmp_path):
    reason = scan_case_dicts(_poisoned(tmp_path, "secret_target"))
    assert "meshDict" in reason
    assert "directive" in reason.lower()


def test_refusal_reason_leaks_neither_file_contents_nor_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "sk-VERY-SECRET-XYZ")
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2-secret")
    target = "EXFIL_TARGET_PATH_abc"
    reason = scan_case_dicts(_poisoned(tmp_path, target))
    assert reason is not None
    assert target not in reason                    # not the included-file target
    assert "sk-VERY-SECRET-XYZ" not in reason       # not a provider secret
    assert "hunter2-secret" not in reason           # not a DB secret


def test_runner_rejection_log_tail_is_leak_free(tmp_path, monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no subprocess on a rejected case")))
    from meshpipeline.engines.cfmesh.cfmesh_runner import run_cartesian_mesh
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text('#codeStream { code "EXFIL_abc"; }\n')
    res = run_cartesian_mesh(tmp_path, timeout=5)
    assert res["rc"] == -2
    assert "EXFIL_abc" not in res["log_tail"]
    assert "REJECTED" in res["log_tail"]


# dispatch tolerates a raising tracer: a refusal is still returned, never a crash/success
def test_dispatch_returns_refusal_even_when_tracer_raises(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("tracer exploded")

    monkeypatch.setattr(trace, "add_event", _boom)

    # isolate the dispatch trace-tolerance: the tool itself returns the guard's refusal shape
    monkeypatch.setattr(tools.meshing, "run_mesh",
                        lambda *a, **k: {"success": False, "rc": -2, "log_tail": "REJECTED: bad dict"})
    out = json.loads(tools._dispatch_tool(_ctx(tmp_path, engine="cfmesh", job_id="j"), "run_mesh", {}))
    assert out.get("success") is False and out.get("rc") == -2


def test_guard_module_imports_no_model_or_network_dependency():
    import ast

    # tools became a package in, so its __file__ sits one level deeper.
    import meshpipeline
    src = (Path(meshpipeline.__file__).parent
           / "sandbox" / "foam_case_guard.py").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    banned_roots = ("requests", "httpx", "socket", "subprocess", "urllib",
                    "openai", "meshpipeline.capture")
    for mod in imported:
        assert not any(mod == b or mod.startswith(b + ".") for b in banned_roots), (
            f"foam_case_guard imports {mod!r} - it must stay a pure lexical pass")
    # concretely: it imports only stdlib re + pathlib
    assert imported <= {"re", "pathlib", "__future__"}, imported
