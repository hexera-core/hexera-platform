# Responsibility: Verify only a validated mesh reaches the reviewer, and a contract failure terminates honestly.
from __future__ import annotations

from langgraph.graph import END

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.pipeline.graph import route_after_executor


def test_a_failed_executor_never_routes_to_the_reviewer():
    # a contract failure is an executor-gate failure → executor_success=False
    for attempt in range(0, bcfg.MAX_BUILDER_RETRIES + 1):
        dest = route_after_executor({"executor_success": False, "retry_count": attempt,
                                     "job_id": "j"})
        assert dest != "node_reviewer", "an un-validated mesh must never reach the reviewer"
        assert dest == "node_classifier"   # rebuild while attempts remain


def test_exhausted_contract_failure_terminates_honestly_bypassing_the_reviewer():
    dest = route_after_executor({"executor_success": False,
                                 "retry_count": bcfg.MAX_BUILDER_RETRIES + 1, "job_id": "j"})
    assert dest == END, "exhausted attempts report failure, not a reviewer PASS"


def test_the_executor_flags_a_multiregion_patch_contract_failure_as_contract_failed():
    import inspect

    import meshpipeline.pipeline.executor as ex
    src = inspect.getsource(ex.node_executor)
    assert 'contract_failed = (_gate_key == "patch_contract")' in src
    # the multiregion gate is keyed 'patch_contract', so this flag fires for it too
    from meshpipeline.engines.registry import get_spec
    assert any(g.key == "patch_contract" for g in get_spec("snappy_multiregion").gates)


def test_only_a_validated_mesh_reaches_the_reviewer():
    assert route_after_executor({"executor_success": True, "retry_count": 0}) == "node_reviewer"
