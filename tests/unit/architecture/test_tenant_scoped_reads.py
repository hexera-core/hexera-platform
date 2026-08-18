# Responsibility: Verify request-facing code reads through the tenant-scoped seam, never the internal one.
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests._scan import scanned

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"

#: Modules that serve HTTP/WebSocket requests. These have an authenticated owner and must scope.
REQUEST_FACING = (SRC / "api", SRC / "application" / "job_service.py")

INTERNAL_READ = "get_internal"
SCOPED_READ = "get_for_owner"


def _python_files(target: Path):
    return sorted(target.rglob("*.py")) if target.is_dir() else [target]


def _calls_named(path: Path, attr: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


def test_no_request_facing_module_calls_the_internal_read():
    offenders = [f"{p.relative_to(SRC.parent.parent)}:{ln}"
                 for target in REQUEST_FACING
                 for p in _python_files(target)
                 for ln in _calls_named(p, INTERNAL_READ)]
    assert not offenders, (
        "request-facing code must scope with get_for_owner:\n  " + "\n  ".join(offenders))


def test_request_facing_code_actually_uses_the_scoped_read():
    found = [f"{p.relative_to(SRC.parent.parent)}:{ln}"
             for target in REQUEST_FACING
             for p in _python_files(target)
             for ln in _calls_named(p, SCOPED_READ)]
    assert len(found) >= 5, f"expected several scoped reads, found {found}"


@pytest.mark.parametrize("module", ["api/v1/chat.py", "api/v1/ws.py",
                                    "api/v1/simulation.py", "application/job_service.py"])
def test_each_request_facing_module_is_free_of_the_internal_read(module):
    assert not _calls_named(SRC / module, INTERNAL_READ)


def test_no_ambiguous_get_alias_was_restored():
    repos = SRC / "persistence" / "repositories"
    for p in scanned(sorted(repos.glob("*.py")), "the persistence repositories"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "get":
                raise AssertionError(f"{p.name} reintroduced an ambiguous `get`")


def test_the_worker_still_has_an_internal_seam():
    owners = [m for m in sorted((SRC / "application").rglob("*.py"))
              if _calls_named(m, INTERNAL_READ)]
    assert owners, "the worker lost its explicit internal read"
