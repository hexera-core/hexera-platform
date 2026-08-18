# Responsibility: Verify two concurrent jobs receive disjoint workspaces and displayless renders that cannot collide.
from __future__ import annotations

import threading
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.agents.builder.workspace import _setup_workspace


def test_two_jobs_get_disjoint_workspaces_under_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", tmp_path)
    results: dict[str, Path] = {}
    errors: list[tuple[str, Exception]] = []

    def _run(job_id: str) -> None:
        try:
            ws = _setup_workspace(job_id, 1)
            # a distinctive meshDict per job - a cross-write would corrupt a neighbour
            (ws / "system" / "meshDict").write_text(f"job={job_id}\n")
            results[job_id] = ws
        except Exception as exc:  # noqa: BLE001
            errors.append((job_id, exc))

    jobs = [f"job-{i:08d}-aaaa-bbbb-cccc-dddddddddddd" for i in range(2)]
    threads = [threading.Thread(target=_run, args=(j,)) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    a, b = results[jobs[0]], results[jobs[1]]
    # disjoint subtrees, each rooted at its OWN job id
    assert a != b and jobs[0] in str(a) and jobs[1] in str(b)
    assert a not in b.parents and b not in a.parents
    # no cross-contamination: each meshDict holds only its own job id
    assert (a / "system" / "meshDict").read_text().strip() == f"job={jobs[0]}"
    assert (b / "system" / "meshDict").read_text().strip() == f"job={jobs[1]}"
    # the case skeleton was laid down independently in each
    assert (a / "system" / "controlDict").exists()
    assert (b / "system" / "controlDict").exists()


def test_reviewer_render_is_displayless_so_concurrent_reviews_cannot_collide():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "sandbox" / "backend.py").read_text()
    assert 'environ.pop("DISPLAY"' in src
    assert '"egl"' in src.lower() or "VTK_DEFAULT_RENDER_BACKEND" in src
    assert "off_screen=True" in src
