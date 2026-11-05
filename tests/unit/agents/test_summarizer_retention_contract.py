# Responsibility: Verify the search summarizer has its own route and deadline, writes no graph state, degrades safely.
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import meshpipeline.agent_tools.shared.settings as scfg
import meshpipeline.agent_tools.shared.web_search as web_search


def test_summarizer_has_its_own_route_and_aggregate_deadline():
    assert scfg.SUMMARIZER_ROUTE.primary.circuit_group == "summarizer"
    # its own aggregate/route budget (not shared with builder/intake)
    assert scfg.SUMMARIZER_ROUTE.total_deadline_s and scfg.SUMMARIZER_ROUTE.total_deadline_s > 0


def test_web_search_writes_no_graph_or_terminal_state():
    src = Path(web_search.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (getattr(node, "module", "") or "") + " " + " ".join(a.name for a in node.names)
            for banned in ("PipelineState", "final_result", "pipeline_run", "job_repository",
                           "artifact"):
                assert banned not in names, f"web_search imports {banned!r} - it must stay advisory"
    for banned in ("executor_success", "reviewer_verdict", "final_result", "JobStatus",
                   "set_final_result", "transition("):
        assert banned not in src, f"web_search references {banned!r} - it must write no state"


def test_web_search_degrades_gracefully_not_fatally():
    src = inspect.getsource(web_search)
    assert "undistilled" in src or "proceed on your own knowledge" in src


def test_distill_prompt_frames_snippets_as_untrusted():
    assert "untrusted" in web_search._DISTILL_SYSTEM.lower()
    assert "do not invent" in web_search._DISTILL_SYSTEM.lower()
