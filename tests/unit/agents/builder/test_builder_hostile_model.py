# Responsibility: Verify a hostile model cannot escape the workspace, overwrite approved context, or change intent.
from __future__ import annotations

import json
from pathlib import Path

import pytest

import meshpipeline.agents.builder.tools as tools
from meshpipeline.sandbox.foam_case_guard import scan_case_dicts


def _ctx(workspace, *, engine="cfmesh", job_id="", geometry=None):
    from pathlib import Path

    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    return BuilderToolContext(workspace=Path(workspace), geometry=geometry,
                              job_id=job_id, engine=engine)

# application-authored, user-approved context the model must never be able to rewrite.
APPROVED_CONTEXT = ["request.txt", "review_brief.txt", "patches_contract.txt",
                    "engine_params.json", "flow_topology", "dimensionality", "purpose"]


def _dispatch(ws: Path, name: str, args: dict) -> dict:
    return json.loads(tools._dispatch_tool(_ctx(ws, engine="cfmesh"), name, args))


# write_file: cannot overwrite approved context, cannot escape the workspace
@pytest.mark.parametrize("protected", APPROVED_CONTEXT)
def test_model_cannot_overwrite_approved_context_via_write_file(tmp_path, protected):
    original = b"APPROVED-CONTENT"
    (tmp_path / protected).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / protected).write_bytes(original)
    out = _dispatch(tmp_path, "write_file", {"path": protected, "content": "HOSTILE OVERWRITE"})
    assert "error" in out and "protected" in out["error"].lower()
    assert (tmp_path / protected).read_bytes() == original


@pytest.mark.parametrize("escape", ["../escape.txt", "/etc/passwd", "sub/../../escape",
                                    "system/../../escape"])
def test_model_cannot_write_outside_workspace(tmp_path, escape):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = _dispatch(ws, "write_file", {"path": escape, "content": "x"})
    assert "error" in out and "escape" in out["error"].lower()


# the model MAY write case dicts (its job) but the guard refuses to EXECUTE a poisoned one
def test_model_can_author_a_dict_but_guard_refuses_to_execute_a_poisoned_one(tmp_path):
    (tmp_path / "system").mkdir()
    out = _dispatch(tmp_path, "write_file",
                    {"path": "system/meshDict", "content": '#codeStream { code "exfiltrate"; }\n'})
    assert "written" in out                       # authoring a file is allowed …
    assert scan_case_dicts(tmp_path) is not None   # … but the pre-execution guard blocks the run


def test_no_dispatchable_tool_changes_approved_intent(tmp_path):
    for name in ("set_engine", "set_purpose", "override_patches", "set_dimensionality",
                 "set_executor_success", "set_final_result", "approve"):
        out = _dispatch(tmp_path, name, {"value": "anything"})
        assert out == {"error": f"Unknown tool: {name}"}


# Run_python cannot PERSIST a mutation to approved context
def test_restore_protected_reverts_overwrite_and_delete(tmp_path):
    (tmp_path / "request.txt").write_bytes(b"ORIG")
    (tmp_path / "purpose").write_bytes(b"external_aero")
    snap = {"request.txt": b"ORIG", "purpose": b"external_aero", "engine_params.json": None}
    # child mutates request.txt, deletes purpose, and creates engine_params.json (was absent)
    (tmp_path / "request.txt").write_bytes(b"TAMPERED")
    (tmp_path / "purpose").unlink()
    (tmp_path / "engine_params.json").write_bytes(b"{}")
    reverted = tools._restore_protected(tmp_path, snap)
    assert reverted == {"request.txt", "purpose", "engine_params.json"}
    assert (tmp_path / "request.txt").read_bytes() == b"ORIG"
    assert (tmp_path / "purpose").read_bytes() == b"external_aero"
    assert not (tmp_path / "engine_params.json").exists()   # was absent in snapshot → removed


def test_restore_protected_leaves_untouched_files_alone(tmp_path):
    (tmp_path / "request.txt").write_bytes(b"ORIG")
    snap = {"request.txt": b"ORIG"}
    assert tools._restore_protected(tmp_path, snap) == set()
    assert (tmp_path / "request.txt").read_bytes() == b"ORIG"


def test_restore_protected_reverts_symlink_planted_over_a_file(tmp_path):
    import os
    (tmp_path / "patches_contract.txt").write_bytes(b"CONTRACT")
    secret = tmp_path.parent / "secret"
    secret.write_bytes(b"SECRET")
    snap = {"patches_contract.txt": b"CONTRACT"}
    (tmp_path / "patches_contract.txt").unlink()
    os.symlink(secret, tmp_path / "patches_contract.txt")
    reverted = tools._restore_protected(tmp_path, snap)
    assert reverted == {"patches_contract.txt"}
    p = tmp_path / "patches_contract.txt"
    assert not p.is_symlink() and p.read_bytes() == b"CONTRACT"


def test_run_python_reverts_protected_mutation_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "request.txt").write_bytes(b"APPROVED BRIEF")
    # neutralise host-dependent gates so the restore wiring - not seccomp availability - is under test
    monkeypatch.setattr(tools.research.polcfg, "MESH_SCRIPT_SCAN_ENABLED", False, raising=False)
    monkeypatch.setattr(tools.research.polcfg, "RUN_PYTHON_REQUIRE_SANDBOX", False, raising=False)

    class _Proc:
        stdout, stderr, returncode = "", "", 0

    def _fake_run(cmd, *a, **k):
        # simulate the jailed child overwriting an approved context file
        (tmp_path / "request.txt").write_bytes(b"HOSTILE REWRITE")
        return _Proc()

    monkeypatch.setattr(tools.research.subprocess, "run", _fake_run)
    out = tools.research.run_python(tmp_path, "print('hi')")
    assert "error" in out and "REVERTED" in out["error"]
    assert (tmp_path / "request.txt").read_bytes() == b"APPROVED BRIEF"
