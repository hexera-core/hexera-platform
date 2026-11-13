# Responsibility: Verify both dispatch sites cross the execution seam, and each backend is selected there.
from __future__ import annotations

from pathlib import Path

import pytest

import meshpipeline.settings.providers as provcfg

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"


def test_run_from_job_loads_the_snapshot_and_runs_the_same_pipeline(monkeypatch):
    import meshpipeline.application.pipeline_run as ex
    from meshpipeline.application.dispatch_contract import DISPATCH_SCHEMA_VERSION

    saved = {"job_id": "J", "owner_id": "u", "mesh_engine": "cfmesh", "purpose": "internal_cfd"}

    async def _fake_load(job_id):
        assert job_id == "J"
        return {"schema_version": DISPATCH_SCHEMA_VERSION, **saved}
    monkeypatch.setattr(ex, "_load_payload", _fake_load)

    called = {}
    monkeypatch.setattr(ex, "run_pipeline", lambda **kw: called.update(kw) or {"status": "succeeded"})

    out = ex.run_from_job("J")
    assert out["status"] == "succeeded"
    # A persisted row is replayed verbatim, plus the documented defaults a DIRECT dispatch omits
    # (the provenance id and the typed patch contract). See
    # application/dispatch_contract._OPTIONAL_DISPATCH_DEFAULTS. The envelope version itself is
    # never a run argument.
    assert {k: called[k] for k in saved} == saved, \
        "run_from_job did not pass the persisted snapshot verbatim"
    assert called == {**saved, "approved_snapshot_id": "", "approved_patch_contract": None}


def test_run_from_job_refuses_a_job_with_no_snapshot(monkeypatch):
    import meshpipeline.application.pipeline_run as ex

    async def _none(job_id):
        return None
    monkeypatch.setattr(ex, "_load_payload", _none)
    with pytest.raises(SystemExit):
        ex.run_from_job("missing")


def test_deferred_backend_persists_without_launching(monkeypatch):
    import asyncio

    from meshpipeline.adapters.pipeline_execution import deferred as deferred_pipeline

    launched = {"celery": False}
    import meshpipeline.adapters.pipeline_execution.celery as wt

    class _T:
        @staticmethod
        def apply_async(**k):
            launched["celery"] = True
    monkeypatch.setattr(wt, "run_simulation", _T)

    asyncio.run(deferred_pipeline.launch(None, "J", {"job_id": "J"}))
    assert launched["celery"] is False, "deferred backend launched Celery"


def test_the_seam_selects_each_backend_module(monkeypatch):
    import meshpipeline.runtime.composition as comp
    from meshpipeline.adapters.pipeline_execution import celery as celery_pipeline
    from meshpipeline.adapters.pipeline_execution import deferred as deferred_pipeline
    for name, mod in (("celery", celery_pipeline), ("deferred", deferred_pipeline)):
        monkeypatch.setattr(provcfg, "PIPELINE_BACKEND", name)
        assert comp.build_pipeline_launcher() is mod


#: The roots the dependency rule covers: engine, agent and pipeline code may be IMPORTED BY the
#: launch adapters, never the other way round.
_SEAM_ROOTS = ("pipeline/graph.py", "engines", "agents", "pipeline")
#: The module this side of the seam may not depend on.
_SEAM = "adapters.pipeline_execution"


def _seam_importers() -> list[str]:
    """Every place under `_SEAM_ROOTS` that really imports `_SEAM`, as file:line.

    THE RULE IS ABOUT THE IMPORT GRAPH, NOT ABOUT PROSE, and asking the text could not tell the
    two apart. The previous form ran `grep -rln adapters.pipeline_execution` over whole file
    CONTENT, which read three things as dependencies that are not:

      * a docstring in `engines/cfmesh_conjugate/driver.py` citing the launch adapter's celery
        task as the authority for that task's 86,400 s soft limit - grep's unescaped `.` matches
        the `/` in the path too, so the citation matched;
      * the same citation in a comment in `engines/cfmesh_conjugate/spec.py`;
      * `engines/cfmesh_conjugate/__pycache__/driver.cpython-312.pyc`, a build artifact carrying
        that docstring, which is not source at all.

    Naming the file a number came from is what this repo asks for everywhere else, so a guard that
    forbids the citation while permitting the dependency is the wrong way round. This parses each
    module and reads its Import and ImportFrom nodes, so every form is caught - a plain import, a
    from-import of the module, a from-import of its name out of the package, and the deferred ones
    inside a function - while a comment, a docstring and a `.pyc` are all invisible to it.
    """
    import ast

    files: list[Path] = []
    for rel in _SEAM_ROOTS:
        root = APP / rel
        files.extend([root] if root.is_file() else sorted(root.rglob("*.py")))

    hits: list[str] = []
    for path in sorted(set(files)):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                named = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                named = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            else:
                continue
            if any(_SEAM in name for name in named):
                hits.append(f"{path.relative_to(APP)}:{node.lineno}")
    return hits


def _imports_the_seam(source: str) -> bool:
    """Whether `source` really imports the seam, by the same reading `_seam_importers` uses."""
    import ast

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            named = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            named = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        if any(_SEAM in name for name in named):
            return True
    return False


#: Real dependencies on the seam, one per import form the language offers, including the deferred
#: form this repo uses inside functions. Every one of these MUST be caught.
_REAL_IMPORTS = (
    "import meshpipeline.adapters.pipeline_execution",
    "from meshpipeline.adapters.pipeline_execution import celery as c",
    "from meshpipeline.adapters import pipeline_execution",
    """
def f():
    from meshpipeline.adapters.pipeline_execution import celery
""",
)

#: Citations of the same path in prose. None of these is an edge in the import graph, and all three
#: are shapes that really occur in this tree - they are what the old text-matching guard failed on.
_CITATIONS = (
    "# see adapters/pipeline_execution/celery.py:44 for the soft limit",
    '"""the figure is cited in adapters/pipeline_execution/celery.py:44-45."""',
    "WALL_S = 86_400  # adapters.pipeline_execution.celery",
)


def test_the_pipeline_code_does_not_import_the_execution_seam():
    hits = _seam_importers()
    assert not hits, f"pipeline code imports the launch adapters (wrong direction): {hits}"


@pytest.mark.parametrize("source", _REAL_IMPORTS)
def test_the_seam_guard_still_catches_a_real_import(source):
    """The guard above went from matching text to matching the import graph. Prove it did not go
    blind on the way: every real import form must still be found."""
    assert _imports_the_seam(source), f"the guard missed a real import: {source!r}"


@pytest.mark.parametrize("source", _CITATIONS)
def test_the_seam_guard_does_not_read_a_citation_as_a_dependency(source):
    """And prove it fixed the thing it was changed for: naming the file a number came from is not
    a dependency on it."""
    assert not _imports_the_seam(source), f"the guard read a citation as a dependency: {source!r}"


def test_both_dispatch_sites_go_through_the_seam_not_apply_async_directly():
    for f in ("api/v1/chat.py", "api/v1/simulation.py"):
        src = (APP / f).read_text()
        assert "run_simulation.apply_async" not in src, (
            f"{f} still calls apply_async directly - the job would have no dispatch_payload")
        # Both routes delegate the dispatch itself, so the seam is asserted at the authority that
        # now performs it: the chat route hands the whole approval transaction to
        # agents/intake/approval, and the dispute route hands dispatch-and-its-failure to
        # application/dispute_operation, which is what makes a refused launch recoverable.
        if f == "api/v1/chat.py":
            src = (APP / "agents" / "intake" / "approval.py").read_text()
        if f == "api/v1/simulation.py":
            src = (APP / "application" / "dispute_operation.py").read_text()
        assert "dispatch_pipeline" in src or "execution.pipeline" in src, (
            f"{f} does not dispatch through the execution seam")
