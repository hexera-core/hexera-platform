# Responsibility: Verify the case guard refuses a prohibited construct or an escaping symlink, and fails closed.
from __future__ import annotations

import os
from pathlib import Path

import pytest

from meshpipeline.sandbox.foam_case_guard import (
    FOAM_GUARD_REJECT_RC,
    scan_case_dicts,
)

# every construct the guard must refuse - the parse-time code/include/library vectors.
PROHIBITED = [
    "#include \"other\"",
    "# include \"other\"",          # whitespace-obfuscated: '#', space, directive
    "#includeEtc \"caseDicts/x\"",
    "#sinclude \"maybe\"",
    "#includeFunc residuals",
    "#codeStream { code \"exit\"; }",
    "#calc \"1+1\"",
    "#system \"id\"",
    "#remove x",
    "libs ( \"libFoo.so\" )",
    "dynamicCode { codeInclude #{ #} ; }",
    "codedFixedValue;",
    "codedSource;",
]


def _write(ws: Path, rel: str, text: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# clean cases pass; empty case passes
def test_clean_case_passes(tmp_path):
    _write(tmp_path, "system/meshDict", "maxCellSize 0.1;\nsurfaceFile \"geom.fms\";\n")
    _write(tmp_path, "constant/transportProperties", "nu [0 2 -1 0 0 0 0] 1e-5;\n")
    assert scan_case_dicts(tmp_path) is None


def test_empty_workspace_passes(tmp_path):
    assert scan_case_dicts(tmp_path) is None


def test_comment_only_mention_passes(tmp_path):
    _write(tmp_path, "system/meshDict",
           "// this used to use #include but no longer does\n"
           "/* #codeStream { code \"x\"; } - historical note */\n"
           "maxCellSize 0.1;\n")
    assert scan_case_dicts(tmp_path) is None


# MUTATION NET: every prohibited construct, in BOTH system/ and constant/
@pytest.mark.parametrize("construct", PROHIBITED)
@pytest.mark.parametrize("root", ["system", "constant"])
def test_prohibited_construct_rejected_in_both_dirs(tmp_path, construct, root):
    _write(tmp_path, f"{root}/meshDict", f"maxCellSize 0.1;\n{construct}\n")
    reason = scan_case_dicts(tmp_path)
    assert reason is not None, f"guard permitted {construct!r} in {root}/"
    assert "meshDict" in reason


def test_directive_in_constant_polymesh_dict_is_scanned(tmp_path):
    _write(tmp_path, "system/meshDict", "maxCellSize 0.1;\n")
    _write(tmp_path, "constant/dynamicMeshDict", '#include "$FOAM_CASE/evil"\n')
    assert scan_case_dicts(tmp_path) is not None


# symlink escape
def test_symlink_file_escaping_workspace_rejected(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("nu 1;\n")
    ws = tmp_path / "ws"
    (ws / "system").mkdir(parents=True)
    (ws / "system" / "meshDict").write_text("maxCellSize 0.1;\n")
    os.symlink(outside, ws / "system" / "linked")
    reason = scan_case_dicts(ws)
    assert reason is not None and "outside the workspace" in reason


def test_symlinked_directory_escaping_workspace_rejected(tmp_path):
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir()
    (outside / "meshDict").write_text('#calc "1"\n')
    ws = tmp_path / "ws"
    (ws / "system").mkdir(parents=True)
    os.symlink(outside, ws / "constant", target_is_directory=True)
    assert scan_case_dicts(ws) is not None


# fail CLOSED on non-dictionary content where a dictionary is expected
def test_binary_dictionary_fails_closed(tmp_path):
    _write(tmp_path, "system/meshDict", "ok;\n")
    (tmp_path / "system" / "meshDict").write_bytes(b"maxCellSize 0.1;\x00\x01\x02")
    reason = scan_case_dicts(tmp_path)
    assert reason is not None and "binary" in reason


def test_undecodable_dictionary_fails_closed(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_bytes(b"\xff\xfe not utf8 \xfa")
    reason = scan_case_dicts(tmp_path)
    assert reason is not None


def test_oversize_dictionary_refused(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "meshDict").write_bytes(b"a" * (8 * 1024 * 1024 + 1))
    reason = scan_case_dicts(tmp_path)
    assert reason is not None and "cap" in reason


# geometry/mesh DATA is skipped (binary content is legitimate there)
def test_binary_geometry_data_is_not_scanned(tmp_path):
    _write(tmp_path, "system/meshDict", "maxCellSize 0.1;\n")
    (tmp_path / "constant" / "triSurface").mkdir(parents=True)
    (tmp_path / "constant" / "triSurface" / "geom.stl").write_bytes(b"solid\x00\x01binary")
    (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
    (tmp_path / "constant" / "polyMesh" / "points").write_bytes(b"\x00\x01\x02 binary points")
    assert scan_case_dicts(tmp_path) is None


# non-leakage: the reason names the construct + path, never the file's contents
def test_reason_never_leaks_scanned_file_contents(tmp_path):
    secret = "SUPER_SECRET_TOKEN_abc123"
    _write(tmp_path, "system/meshDict", f'#include "{secret}"\nmaxCellSize 0.1;\n')
    reason = scan_case_dicts(tmp_path)
    assert reason is not None
    assert secret not in reason, "the refusal reason leaked the included-file target/contents"


def test_reject_rc_is_negative_safe_sentinel():
    assert FOAM_GUARD_REJECT_RC == -2
    assert FOAM_GUARD_REJECT_RC < 0


# ONE implementation, shared by every OpenFOAM engine (no drift)
def test_all_openfoam_engines_share_the_one_guard_object():
    import meshpipeline.sandbox.foam_case_guard as canonical
    from meshpipeline.engines.cfmesh import foam_exec as cf
    from meshpipeline.engines.snappy import foam_exec as sn
    from meshpipeline.engines.snappy_multiregion import foam_exec as mr
    assert cf.scan_case_dicts is canonical.scan_case_dicts
    assert sn.scan_case_dicts is canonical.scan_case_dicts
    assert mr.scan_case_dicts is canonical.scan_case_dicts
