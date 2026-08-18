# Responsibility: Verify an OpenFOAM dict carrying a code directive is refused before any subprocess starts.
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh.cfmesh_runner import (  # noqa: E402
    _foam_env,
    run_cartesian_mesh,
    scan_case_dicts,
)
from meshpipeline.engines.snappy import snappy_runner  # noqa: E402


def _case(tmp_path: Path, meshdict: str) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_text(meshdict)
    return tmp_path


def test_clean_dicts_pass(tmp_path):
    ws = _case(tmp_path, "maxCellSize 0.1;\nsurfaceFile \"geom.fms\";\n")
    assert scan_case_dicts(ws) is None


def test_no_system_dir_passes(tmp_path):
    assert scan_case_dicts(tmp_path) is None


@pytest.mark.parametrize("directive", ["#codeStream", "#calc", "#system"])
def test_code_directives_rejected(tmp_path, directive):
    ws = _case(tmp_path, f"maxCellSize {directive} {{ code \"exit\"; }};\n")
    reason = scan_case_dicts(ws)
    assert reason is not None
    # the hardened guard names the OFFENDING PATH but not the external construct's contents
    assert "meshDict" in reason


def test_directive_in_any_system_file_rejected(tmp_path):
    ws = _case(tmp_path, "maxCellSize 0.1;\n")
    (ws / "system" / "controlDict").write_text('startTime #calc "0+0";\n')
    assert scan_case_dicts(ws) is not None


def test_run_cartesian_mesh_rejects_before_any_subprocess(tmp_path, monkeypatch):
    import subprocess as _sp
    def _boom(*a, **k):
        raise AssertionError("subprocess must not run on a rejected case")
    monkeypatch.setattr(_sp, "run", _boom)
    ws = _case(tmp_path, '#codeStream { code "exfiltrate"; };\n')
    result = run_cartesian_mesh(ws, timeout=5)
    assert result["rc"] == -2 and not result["timed_out"]
    assert "REJECTED" in result["log_tail"]


def test_run_snappy_rejects_before_any_subprocess(tmp_path, monkeypatch):
    import subprocess as _sp
    def _boom(*a, **k):
        raise AssertionError("subprocess must not run on a rejected case")
    monkeypatch.setattr(_sp, "run", _boom)
    ws = _case(tmp_path, "unused\n")
    (ws / "system" / "snappyHexMeshDict").write_text('#system "id";\n')
    result = snappy_runner.run_snappy(ws, timeout=5)
    assert result["rc"] == -2 and "REJECTED" in result["log_tail"]
    assert result["layer_coverage"] == 0.0


def test_foam_env_strips_secrets_keeps_runtime_vars(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "sk-secret")
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _foam_env()
    assert "DEEPINFRA_API_KEY" not in env
    assert "POSTGRES_PASSWORD" not in env
    assert env.get("PATH") == "/usr/bin"


def test_geometry_file_path_escape_blocked(tmp_path):
    from meshpipeline.agents.builder.tools import _confined
    assert _confined(tmp_path, "../outside.stl") is None
    assert _confined(tmp_path, "/etc/passwd") is None
    inside = _confined(tmp_path, "sub/geom.stl")
    assert inside is not None and inside.is_relative_to(tmp_path.resolve())


# The guard also scans constant/, and EVERY OpenFOAM engine refuses before its native
# binary - a scanner rejection must NEVER surface as an executor success
def _no_subprocess(monkeypatch):
    import subprocess as _sp

    def _boom(*a, **k):
        raise AssertionError("subprocess must not run on a rejected case")

    monkeypatch.setattr(_sp, "run", _boom)
    # multiregion runs foam steps via run_guarded (Popen), not subprocess.run - block that too,
    # at both its definition site and the name bound into the multiregion runner module.
    import meshpipeline.sandbox.safe_exec as _se
    monkeypatch.setattr(_se, "run_guarded", _boom, raising=False)
    from meshpipeline.engines.snappy_multiregion import multiregion_runner as _mr
    monkeypatch.setattr(_mr, "run_guarded", _boom, raising=False)


def test_directive_in_constant_dir_is_scanned(tmp_path):
    ws = _case(tmp_path, "maxCellSize 0.1;\n")
    (ws / "constant").mkdir()
    (ws / "constant" / "transportProperties").write_text('nu #calc "1e-5";\n')
    assert scan_case_dicts(ws) is not None


def test_cfmesh_refuses_constant_dir_directive_before_subprocess(tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    ws = _case(tmp_path, "maxCellSize 0.1;\n")
    (ws / "constant").mkdir()
    (ws / "constant" / "meshQualityDict").write_text('libs ("libEvil.so");\n')
    result = run_cartesian_mesh(ws, timeout=5)
    assert result["rc"] != 0 and not result.get("timed_out")
    assert "REJECTED" in result["log_tail"]


def test_snappy_refuses_constant_dir_directive_before_subprocess(tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    ws = _case(tmp_path, "maxCellSize 0.1;\n")
    (ws / "system" / "snappyHexMeshDict").write_text("castellatedMesh true;\n")
    (ws / "constant").mkdir()
    (ws / "constant" / "dynamicMeshDict").write_text('#includeEtc "caseDicts/x"\n')
    result = snappy_runner.run_snappy(ws, timeout=5)
    assert result["rc"] != 0 and "REJECTED" in result["log_tail"]


def test_snappy_multiregion_refuses_directive_before_subprocess(tmp_path, monkeypatch):
    _no_subprocess(monkeypatch)
    from meshpipeline.engines.snappy_multiregion import multiregion_runner
    ws = _case(tmp_path, "maxCellSize 0.1;\n")
    (ws / "constant").mkdir()
    (ws / "constant" / "regionProperties").write_text('dynamicCode { code #{ #}; }\n')
    result = multiregion_runner._run_snappy_multiregion_local(ws, timeout=5)
    # a clean, NON-success failure (never rc==0, never a produced mesh)
    assert result["rc"] != 0 and not result.get("timed_out")
    assert "rejected" in result["log_tail"].lower()
