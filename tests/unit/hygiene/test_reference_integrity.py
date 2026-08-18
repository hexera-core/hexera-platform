# Responsibility: Verify shipped code names no module it cannot import, and a pinned task name stays deliberate.
from __future__ import annotations

import re
from pathlib import Path

from tests._scan import scanned

REPO = Path(__file__).parents[3]
SRC = REPO / "src" / "meshpipeline"


def test_the_celery_task_name_is_kept_deliberately_not_by_accident():
    src = (SRC / "adapters" / "pipeline_execution" / "celery.py").read_text()
    assert 'name="worker.tasks.run_simulation"' in src, "the task identifier changed"
    assert re.search(r"compatib|preserv|identifier|queue", src, re.I), (
        "the preserved task name is not explained - the next reader will 'fix' it")


def test_shipped_code_names_no_module_it_cannot_import():
    import importlib.util

    bad: list[str] = []
    for p in scanned(SRC.rglob("*.py"), "the shipped source tree"):
        if "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'"-m",\s*"([\w.]+)"', text):
            mod = m.group(1)
            try:
                found = importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                bad.append(f"{p.relative_to(REPO)}: -m {mod}")
    assert not bad, f"shipped code launches a module that does not resolve: {bad}"
