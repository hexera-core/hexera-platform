# Responsibility: Let the builder look something up, and compute values in a jail.
# Boundaries: run_python executes model-authored code under seccomp and Landlock, with no network.
# Collaborates with: sandbox/sandbox_exec.py.
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import meshpipeline.settings.policy as polcfg

#: THE staged surface every engine analyses. A DERIVED workspace artefact - written upstream by the
#: engine's own tessellator - not the source geometry and not an identity. It carries no unit,
#: which is precisely why a tool that measures it must also hold the interpretation.
STAGED_SURFACE = "input.stl"

logger = logging.getLogger(__name__)


# `run_python` and `web_search` are one family because the Builder's role prompt pairs them as
# one discipline (prompts/builder/system.txt), so a change to it changes both. Both are strictly
# advisory: neither may alter the mesh or the workspace contract.
from meshpipeline.agents.builder.tools.workspace import (
    _PROTECTED_PATHS,
    _restore_protected,
)

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for meshing guidance you don't already know - mesh "
                "spec syntax, less-common boundary/region setups, or physics for an "
                "unfamiliar geometry/regime. A search "
                "sub-agent retrieves and DISTILLS the results, returning a short answer "
                "(not raw pages). Prefer your own knowledge first; search only when "
                "genuinely uncertain. Do NOT search for the standard external-aero "
                "thresholds already in your system prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A specific, self-contained question (include the mesher/tool context).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute a short Python 3 snippet in the workspace and return its stdout. Use this to "
                "COMPUTE numeric values precisely - never do arithmetic in your head. E.g. derive domain "
                "corners from the body extents and a requested multiple, a first-layer thickness, "
                "or a cell-count estimate. print() whatever you need back. No network; ~15s limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python 3 source; print() the values you need back."},
                },
                "required": ["code"],
            },
        },
    },
]

ACTIONS: dict[str, str] = {
    "run_python":      "Computing sizes",
    "web_search":      "Looking up guidance",
}

def web_search(query: str, job_id: str = "") -> dict:
    # Runs inside asyncio.to_thread (off the main loop), so a fresh event loop
    # via asyncio.run is safe here.
    import asyncio as _asyncio
    try:
        from meshpipeline.agent_tools.shared.web_search import web_search
        result = _asyncio.run(web_search(query, job_id=job_id))
        return {"result": result or "No answer returned."}
    except Exception as exc:
        logger.warning("Builder web_search tool failed: %s", exc)
        return {"error": f"web_search failed: {exc}"}

def run_python(workspace: Path, code: str) -> dict:
    # Model-authored code. Defence in DEPTH:
    #   1. static scan rejects obvious network/subprocess/dynamic-import/env attempts;
    #   2. secret-scrubbed env - no provider/DB/MinIO creds reachable;
    #   3. OS JAIL (sandbox.sandbox_exec): seccomp blocks the socket syscall (NO
    #      network - kernel-enforced, unbypassable by python tricks) + rlimits.
    if polcfg.MESH_SCRIPT_SCAN_ENABLED:
        from meshpipeline.sandbox.safe_exec import scan_python_source
        _reason = scan_python_source(code, filename="run_python")
        if _reason:
            return {"error": (
                f"run_python rejected by safety scan: {_reason}. This tool is for "
                "local numeric computation only - no network I/O, no subprocess, and "
                "no reading process environment variables.")}
    from meshpipeline.sandbox.safe_exec import scrubbed_subprocess_env
    from meshpipeline.sandbox.sandbox_exec import jail_preexec, seccomp_supported
    if polcfg.RUN_PYTHON_REQUIRE_SANDBOX and not seccomp_supported():
        return {"error": ("run_python sandbox unavailable on this host (seccomp could "
                          "not be initialised) and RUN_PYTHON_REQUIRE_SANDBOX is on - "
                          "refusing to run un-jailed model code.")}
    import os as _os
    _ws_fd = None
    # PROTECTED-FILE INTEGRITY: the jail confines the child to the workspace (rw), so the snippet
    # *could* open/rename/delete an application-authored context file (request.txt, the patch
    # contract, …). Landlock grants can't carve a read-only hole under a read-write root, so we
    # snapshot the protected files' bytes before the run and RESTORE any that the child changed or
    # deleted afterwards - the child can never persist a mutation to approved context.
    _snap: dict[str, bytes | None] = {}
    for _rel in _PROTECTED_PATHS:
        _pp = (workspace / _rel)
        try:
            _snap[_rel] = _pp.read_bytes() if _pp.is_file() else None
        except OSError:
            _snap[_rel] = None
    try:
        f = workspace / ".builder_calc.py"
        f.write_text(code, encoding="utf-8")
        # O_PATH dir fd → Landlock confines the child to THIS workspace (rw) + RO
        # system paths, so it can't read sibling job workspaces.
        try:
            _ws_fd = _os.open(str(workspace), _os.O_PATH | _os.O_CLOEXEC | _os.O_DIRECTORY)
        except OSError:
            _ws_fd = None
        proc = subprocess.run(
            ["python3", str(f)], cwd=str(workspace),
            capture_output=True, text=True, timeout=15,
            env=scrubbed_subprocess_env(),
            preexec_fn=jail_preexec(require_seccomp=polcfg.RUN_PYTHON_REQUIRE_SANDBOX,
                                    workspace_fd=_ws_fd),
        )
        _reverted = _restore_protected(workspace, _snap)
        if _reverted:
            return {"error": ("run_python attempted to modify application-authored, approved context "
                              f"file(s) {sorted(_reverted)} - the change was REVERTED. Those files are "
                              "immutable; write derived content to a different filename."),
                    "stdout": (proc.stdout or "")[-1000:]}
        return {
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-1500:],
            "exit_code": proc.returncode,
            "guidance": "" if proc.returncode == 0 else "Python raised an error - fix the snippet and retry.",
        }
    except subprocess.TimeoutExpired:
        return {"error": "run_python timed out (15s). Keep it to a quick calculation."}
    except Exception as exc:
        return {"error": f"run_python failed: {exc}"}
    finally:
        # ALWAYS restore protected context, even on timeout/crash - a mutation must never persist
        # into the next tool call. Idempotent: a no-op if the normal path already restored.
        _restore_protected(workspace, _snap)
        if _ws_fd is not None:
            try: _os.close(_ws_fd)
            except OSError: pass
