# Responsibility: Let the builder read and write files inside its own workspace.
# Boundaries: every path is confined to the attempt workspace; approved context is restored if model code mutates it.
from __future__ import annotations

import logging
import os
from pathlib import Path

#: THE staged surface every engine analyses. A DERIVED workspace artefact - written upstream by the
#: engine's own tessellator - not the source geometry and not an identity. It carries no unit,
#: which is precisely why a tool that measures it must also hold the interpretation.
STAGED_SURFACE = "input.stl"

logger = logging.getLogger(__name__)


# Every confinement rule here exists because a specific escape was possible: a path resolving
# outside the workspace, or model-authored code replacing the very context files its review is
# conducted against.

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file inside the job workspace. "
                "Creates parent directories automatically. "
                "Use relative paths from the workspace root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from workspace root, e.g. 'notes.txt' or the engine's spec file",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the job workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from workspace root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories at a path inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from workspace root. Use '.' for root."},
                },
                "required": ["path"],
            },
        },
    },
]

ACTIONS: dict[str, str] = {
    "write_file":      "Writing the mesh spec",
    "read_file":       "Reading the brief",
    "list_directory":  "Checking the workspace",
}

_PROTECTED_PATHS = frozenset({
    "input.stl",
    "geom.stl", "geom.fms", "geometry.step",   # staged input geometry (also carried across retries)
    "system/controlDict",
    "system/fvSchemes",
    "system/fvSolution",
    # application-authored, user-approved briefing/context (workspace.py::_write_workspace_context_files)
    "request.txt",
    "review_brief.txt",
    # The mesh manifest is written by the engine's finalize (engines/manifest.write_manifest)
    # and read by the REVIEW renderer. Leaving it writable let the component under review
    # reach the conditions its own review is conducted under; the renderer now ignores its
    # presentation fields, and this closes the channel outright.
    "mesh_manifest.json",
    "patches_contract.txt",
    "engine_params.json",
    "flow_topology",
    "dimensionality",
    "purpose",
})



# What each tool means to the PERSON watching. Tool names are the LLM's vocabulary;
# the user is owed the action, not the API. This lives beside the tool declarations

def _confined(workspace: Path, rel: str) -> Path | None:
    target = (workspace / rel).resolve()
    return target if target.is_relative_to(workspace.resolve()) else None


def write_file(workspace: Path, path: str, content: str) -> dict:
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        return {"error": f"Path escape attempt blocked: {path}"}
    relative = str(target.relative_to(workspace.resolve()))
    if relative in _PROTECTED_PATHS:
        return {"error": f"Write blocked: {relative} is a protected file and cannot be overwritten"}
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"written": relative, "bytes": len(content.encode()),
            "operation": "updated" if existed else "created"}


def read_file(workspace: Path, path: str) -> dict:
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        return {"error": f"Path escape attempt blocked: {path}"}
    if not target.exists():
        return {"error": f"File not found: {path}"}
    content = target.read_text(encoding="utf-8", errors="replace")
    return {"content": content}


def list_directory(workspace: Path, path: str) -> dict:
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace.resolve()):
        return {"error": f"Path escape attempt blocked: {path}"}
    if not target.exists():
        return {"error": f"Directory not found: {path}"}
    if not target.is_dir():
        # a live run crashed here: the model listed the flow_topology FILE
        return {"error": f"Not a directory: {path} (it is a file - use read_file)"}
    entries = sorted(os.listdir(target))
    return {"entries": entries}

def _restore_protected(workspace: Path, snap: dict) -> set:
    reverted: set = set()
    for _rel, _orig in snap.items():
        _pp = workspace / _rel
        try:
            _now = _pp.read_bytes() if _pp.is_file() else None
        except OSError:
            _now = None
        if _now == _orig:
            continue
        reverted.add(_rel)
        try:
            if _orig is None:
                if _pp.exists() or _pp.is_symlink():
                    _pp.unlink()
            else:
                if _pp.is_symlink():           # a symlink planted over the path - remove, rewrite real bytes
                    _pp.unlink()
                _pp.parent.mkdir(parents=True, exist_ok=True)
                _pp.write_bytes(_orig)
        except OSError:
            pass
    return reverted
