# Responsibility: Verify a protected workspace file cannot be written, and the refusal is a structured result.
from pathlib import Path

import pytest

from meshpipeline.agents.builder.agent import _PROTECTED_PATHS, _tool_write_file


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path



def test_protected_set_covers_all_expected_files():
    assert "input.stl"           in _PROTECTED_PATHS
    assert "system/controlDict"  in _PROTECTED_PATHS
    assert "system/fvSchemes"    in _PROTECTED_PATHS
    assert "system/fvSolution"   in _PROTECTED_PATHS
    assert "mesh_manifest.json"  in _PROTECTED_PATHS



def test_blocks_input_stl(ws):
    result = _tool_write_file(ws, "input.stl", "solid body\nendsolid body\n")
    assert "error" in result
    assert "input.stl" in result["error"]
    assert not (ws / "input.stl").exists()


def test_blocks_control_dict(ws):
    result = _tool_write_file(ws, "system/controlDict", "FoamFile{}")
    assert "error" in result
    assert "controlDict" in result["error"]
    assert not (ws / "system" / "controlDict").exists()


def test_blocks_fvSchemes(ws):
    result = _tool_write_file(ws, "system/fvSchemes", "FoamFile{}")
    assert "error" in result
    assert "fvSchemes" in result["error"]
    assert not (ws / "system" / "fvSchemes").exists()


def test_blocks_fvSolution(ws):
    result = _tool_write_file(ws, "system/fvSolution", "FoamFile{}")
    assert "error" in result
    assert "fvSolution" in result["error"]
    assert not (ws / "system" / "fvSolution").exists()


def test_blocked_write_returns_structured_error_not_exception(ws):
    result = _tool_write_file(ws, "input.stl", "content")
    assert isinstance(result, dict)
    assert "error" in result
    assert "written" not in result



def test_allows_mesh_gen_py(ws):
    result = _tool_write_file(ws, "mesh_gen.py", "# generated")
    assert "written" in result
    assert result["written"] == "mesh_gen.py"
    assert (ws / "mesh_gen.py").read_text() == "# generated"


def test_allows_nested_output_file(ws):
    result = _tool_write_file(ws, "results/summary.txt", "ok")
    assert "written" in result
    assert (ws / "results" / "summary.txt").exists()


def test_blocks_mesh_manifest(ws):
    # OWNERSHIP, not merely safety. mesh_manifest.json is written by the engine's finalize
    # (engines/manifest.write_manifest) and read by the REVIEW renderer - patch roles, the
    # reviewer's per-patch view presets, inspection regions. The builder is the component
    # whose mesh that review judges, so it must not be able to reach the conditions the
    # review is conducted under. It remains READABLE (agents/builder/context.py).
    result = _tool_write_file(ws, "mesh_manifest.json", "{}")
    assert "error" in result
    assert "mesh_manifest.json" in result["error"]
    assert not (ws / "mesh_manifest.json").exists()


def test_write_creates_missing_parent_dirs(ws):
    result = _tool_write_file(ws, "deep/nested/dir/file.txt", "data")
    assert "written" in result
    assert (ws / "deep" / "nested" / "dir" / "file.txt").exists()


def test_write_returns_correct_byte_count(ws):
    content = "hello world"
    result = _tool_write_file(ws, "test.txt", content)
    assert result["bytes"] == len(content.encode("utf-8"))


def test_write_returns_relative_path(ws):
    result = _tool_write_file(ws, "subdir/file.py", "x = 1")
    assert not result["written"].startswith("/")
    assert "subdir" in result["written"]



def test_blocks_parent_traversal(ws):
    result = _tool_write_file(ws, "../evil.py", "malicious")
    assert "error" in result
    assert "blocked" in result["error"].lower()
    assert not (ws.parent / "evil.py").exists()


def test_blocks_absolute_like_traversal(ws):
    result = _tool_write_file(ws, "../../etc/passwd", "INJECTED")
    assert "error" in result
    assert "blocked" in result["error"].lower()
