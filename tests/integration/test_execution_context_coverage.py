# Responsibility: Measure which canonical execution publication contexts real runs actually reach.
# Boundaries: instrumentation and its controls - it reports coverage and never claims completeness.
from __future__ import annotations

import asyncio
import collections
import json
import os
import time

import pytest
from tests.integration import execution_context_attribution as A

import meshpipeline.settings.providers as provcfg

# No module-level `asyncio` mark: the attribution controls are synchronous by design - they drive
# one publication and inspect the frame it was attributed to. The tier runs in asyncio auto mode.
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)
if A.unavailable_reason():
    # The canonical contexts and their ordinals belong to the authority scanner, which is a
    # development tool. Run this tier from the repository checkout, not from a runtime image.
    pytest.skip(A.unavailable_reason(), allow_module_level=True)

SRC = A.SRC

#: Where the ownership gate lives. It is transport, so the measurement must never name it - which
#: makes it exactly the identity the naive control is expected to produce.
GATE = "application/execution_publisher.py"


# the target set comes from the manifest, and from nowhere else


def test_the_targets_are_the_manifests_execution_owned_records():
    manifest = json.loads(A.MANIFEST.read_text())
    expected = {f"{r['path']}::{r['qualname']}::{r['semantic']}#{r['ordinal']}"
                for r in manifest if r["lifecycle"] == "execution-owned"}
    assert set(A.targets()) == expected, "the target set is not the manifest's"
    assert expected, "the manifest declares no execution-owned context at all"
    for ident, r in A.targets().items():
        assert r["authority"] == "ExecutionEventPublisher", ident
        assert r["gated_required"] is True, ident


def test_repeated_sites_in_one_definition_get_distinct_ordinals():
    checks = [c for c in A.targets()
              if c.startswith("pipeline/executor.py::node_executor::check#")]
    assert len(checks) > 1, "no repeated check site here, so this proves nothing about ordinals"
    assert len({c.rsplit("#", 1)[1] for c in checks}) == len(checks), \
        "two check sites in one definition share an ordinal"
    # and the runtime index can tell them apart, by line, in the scanner's own AST order
    index = A.lines()
    keys = [k for k in index if k[:3] == ("pipeline/executor.py", "node_executor", "check")]
    assert len({k[3] for k in keys}) == 6, "the six sites are not distinguishable at runtime"
    assert {index[k] for k in keys} == {1, 2, 3, 4, 5, 6}


# attribution controls


def _fire(monkeypatch, seen: list, *, naive: bool = False, intervening: bool = True) -> None:
    # A REAL production publication site, driven through a stand-in transport so no service is
    # needed: what is under test is which frame the observer attributes the call to.
    import meshpipeline.application.execution_publisher as mod
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher
    from meshpipeline.contracts import rationale as R

    monkeypatch.setattr(JobPublisher, "_emit_once", lambda self, event, op_key="": None)
    A.observe(monkeypatch, seen, naive=naive)

    if intervening:
        # another suite's recorder, installed on top of ours exactly as one really would be
        original = JobPublisher.rationale

        def another_suites_observer(self, *a, **k):
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, "rationale", another_suites_observer)

    class _Own:
        job_id, execution_generation, worker_token = "j", 1, "t"

    async def _is_current_owner():
        return True

    monkeypatch.setattr(mod._fence, "current_ownership", lambda: _Own())
    monkeypatch.setattr(mod._fence, "is_current_owner", _is_current_owner)

    async def go():
        await R.abuilder_mesh_ready(OwnershipCheckedPublisher(JobPublisher("j", "builder")),
                                    cells=1)

    asyncio.run(go())


def test_attribution_skips_the_gate_the_adapter_and_another_suites_observer(monkeypatch):
    seen: list = []
    _fire(monkeypatch, seen)
    assert seen, "the publication was never observed"
    ctx = seen[0]["context"]
    assert ctx == "contracts/rationale.py::_asay::rationale#1", ctx
    assert GATE not in ctx and "redis.py" not in ctx, "transport was named as the decider"
    assert "another_suites_observer" not in ctx, "another suite's recorder was named as the decider"
    assert ctx in A.targets(), "the attributed identity is not a manifest context"


def test_attribution_selects_a_frame_under_the_production_package(monkeypatch):
    seen: list = []
    _fire(monkeypatch, seen)
    resolved = (SRC / seen[0]["path"]).resolve()
    assert resolved.is_relative_to(SRC), f"{resolved} is outside the production package"
    assert resolved.exists(), f"{resolved} does not exist"


def test_nested_definitions_are_normalized_to_the_scanner_spelling(monkeypatch):
    # the scanner names a nested function `outer.inner`; the interpreter says `outer.<locals>.inner`
    nested = [c for c in A.targets() if "._on_round::" in c or "._announce_engine::" in c]
    assert nested, "no nested-definition context in the manifest to calibrate against"
    assert not any("<locals>" in c for c in A.targets()), "a target carries the interpreter spelling"

    class _F:
        class f_code:
            co_qualname = "_run_tool_loop.<locals>._on_round"

    assert A._qualname(_F) == "_run_tool_loop._on_round"


def test_the_naive_control_reproduces_the_wrapper_attribution_error(monkeypatch):
    seen: list = []
    _fire(monkeypatch, seen, naive=True, intervening=False)
    ctx = seen[0]["context"]
    assert ctx.startswith(f"{GATE}::OwnershipCheckedPublisher.arationale::"), (
        f"the control no longer reproduces the wrapper attribution it exists to catch: {ctx}")
    assert ctx not in A.targets(), "a transport identity was accepted as a manifest context"
    with pytest.raises(A.AttributionUnsound):
        A.assert_measurement_sound(A.measure(seen))


def test_measurement_refuses_an_identity_outside_the_manifest():
    result = A.measure([{"context": "some/other.py::somewhere::note#1"}])
    assert result["unexpected"] == ["some/other.py::somewhere::note#1"]
    with pytest.raises(A.AttributionUnsound, match="outside the manifest"):
        A.assert_measurement_sound(result)


def test_measurement_refuses_a_call_it_could_not_place():
    result = A.measure([{"context": "engines/snappy/drivers.py::_build_snappy::note#0"}])
    assert result["ambiguous"], "a call that resolved to no site was accepted"
    with pytest.raises(A.AttributionUnsound, match="could not be attributed"):
        A.assert_measurement_sound(result)


def test_measurement_reports_missing_contexts_explicitly():
    result = A.measure([])
    assert len(result["missing"]) == result["target_total"] and result["observed_total"] == 0
    A.assert_measurement_sound(result)          # a sound instrument, and empty coverage


def test_certification_rejects_an_incomplete_observed_set():
    with pytest.raises(AssertionError, match="never behaviourally executed"):
        A.assert_every_context_certified(A.measure([]))


# the combined measurement, over the committed behavioural scenarios


def _delete_job_keys(job_ids: list) -> None:
    # by recorded name, never by pattern
    import redis as _redis

    from meshpipeline.events.channels import (
        fence_key_for,
        log_key_for,
        opkey_set_for,
        seq_key_for,
    )

    r = _redis.from_url(provcfg.REDIS_URL)
    try:
        for job_id in job_ids:
            for key in (seq_key_for(str(job_id)), log_key_for(str(job_id)),
                        opkey_set_for(str(job_id)), fence_key_for(str(job_id))):
                r.delete(key)
    finally:
        r.close()


async def run_committed_scenarios(mp, tmp, jobs: list, seen: list | None = None,
                                  spans: list | None = None) -> None:
    # The COMMITTED behavioural scenarios, each driven exactly as its own suite drives it. None is
    # duplicated to raise the count: the branches differ in the production path above the
    # publication, which is what makes each one reach a different context.
    #
    # `spans` records which scenario produced which publications, so every certified context can
    # name the executed scenario that reached it rather than being justified by a total.
    from tests.integration import test_builder_execution_certification as B
    from tests.integration import test_snappy_driver_ownership as S

    async def step(name: str, awaitable):
        start = len(seen) if seen is not None else 0
        result = await awaitable
        if spans is not None:
            spans.append({"scenario": name, "start": start,
                          "end": len(seen) if seen is not None else 0})
        return result

    for engine in B.LOOP_ENGINES:
        jobs.append((await step(f"builder-loop:{engine}",
                                B._drive(mp, tmp / engine, engine=engine)))["job_id"])
    jobs.append((await step("builder-driver:snappy-retry",
                            B._drive(mp, tmp / "retry", engine="snappy",
                                     mode="retry")))["job_id"])
    jobs.append((await step("builder-budget:exhausted-before-start",
                            B._drive(mp, tmp / "budget", engine="cfmesh",
                                     deadline_epoch=time.time() - 5.0)))["job_id"])
    jobs.append((await step("builder-no-progress:identical-failure-twice",
                            B._drive_no_progress(mp, tmp / "noprog")))["job_id"])
    for name, kw in (("reject", {}),
                     ("accept", {"body": S._half_model, "native_double": False}),
                     ("exhaust", {"body": S._half_model, "native_double": True}),
                     ("internal-accept", {"internal": True, "native_double": False}),
                     ("internal-exhaust", {"internal": True, "native_double": True}),
                     ("internal-refuse", {"internal": True, "native_double": False,
                                          "tessellate_fail": True}),
                     ("internal-refuse-binding", {"internal": True, "native_double": False,
                                                  "unbindable_patches": True})):
        jobs.append((await step(f"snappy-driver:{name}",
                                S._run(mp, tmp / f"snappy-{name}", **kw)))[0])

    # The pipeline executor's own scenarios: five outcomes, because reaching all eleven of its
    # canonical sites means REACHING THE OUTCOMES, not calling the publisher eleven times.
    from tests.integration import test_pipeline_executor_ownership as X

    (tmp / "executor").mkdir(parents=True, exist_ok=True)
    workspace = X._ws(tmp / "executor")
    for _name, kw in X.SCENARIOS:
        jobs.append(await step(f"pipeline-executor:{_name}",
                               X._run(mp, workspace, **kw)))

    # Engine selection and geometry admission, each through its own production boundary.
    from tests.integration import test_pipeline_node_ownership as N

    from meshpipeline.pipeline.engine_select import node_engine_select
    from meshpipeline.pipeline.geometry_admission import node_geometry_admission

    nodes: list = []
    for label, state in (("internal-topology",
                          lambda j: {"job_id": str(j), "purpose": "internal_cfd"}),
                         ("deterministic-default", lambda j: {"job_id": str(j)})):
        jobs.append((await step(f"engine-select:{label}",
                                N._owned(mp, nodes, node_engine_select,
                                         state)))["job_id"])
    (tmp / "admission").mkdir(parents=True, exist_ok=True)
    geometry = N._self_intersecting_geometry(tmp / "admission")
    jobs.append((await step("geometry-admission:measured-rejection",
                            N._owned(mp, nodes, node_geometry_admission,
                                     lambda j: {**N._VMTK, "job_id": str(j),
                                                "geometry": geometry})))["job_id"])

    # The builder policy's own close-out: two valid meshes, no submission, so the loop ends with
    # the policy making the submission and announcing it.
    jobs.append((await step("builder-policy:auto-submit-close-out",
                            B._drive(mp, tmp / "close-out", engine="cfmesh",
                                     script=B.AUTO_SUBMIT_SCRIPT,
                                     mesh_result=B.VALID_MESH)))["job_id"])

    # The pipeline run's own orchestration announcements, before the graph starts.
    from tests.integration import test_pipeline_run_announcements as R

    (tmp / "run").mkdir(parents=True, exist_ok=True)
    jobs.append((await step("pipeline-run:pinned-engine-and-dispute",
                            R._run(mp, tmp / "run", [], [])))["job_id"])

    # The reviewer, both ways: a completed review that renders, judges and publishes its verdict,
    # and the refusal path that warns instead. Together they reach all five of its sites.
    from tests.integration import test_reviewer_visual_ownership as V

    (tmp / "review").mkdir(parents=True, exist_ok=True)
    jobs.append((await step("reviewer:completed-review-with-verdict",
                            V._review(mp, tmp / "review", [])))["job_id"])
    jobs.append((await step("reviewer:unvalidated-execution-refusal",
                            V._review(mp, tmp / "review-refused", [],
                                      executor_success=False)))["job_id"])


async def run_stale_attempts(mp, tmp, jobs: list) -> list:
    # REFUSED publications, kept apart from the measurement above on purpose: a stale attempt is
    # evidence that the boundary held, never evidence that a context was covered.
    from tests.integration import test_pipeline_node_ownership as N

    from meshpipeline.application.execution_fence import StaleWorkerFenced
    from meshpipeline.contracts.event_stream import StaleExecutionPublish
    from meshpipeline.pipeline.engine_select import node_engine_select
    from meshpipeline.pipeline.executor import node_executor
    from meshpipeline.pipeline.geometry_admission import node_geometry_admission

    (tmp / "stale").mkdir(parents=True, exist_ok=True)
    geometry = N._self_intersecting_geometry(tmp / "stale")
    attempts = []
    for root, node, state, expected in (
            ("pipeline/engine_select.py", node_engine_select,
             lambda j: {"job_id": str(j), "purpose": "internal_cfd"}, StaleExecutionPublish),
            ("pipeline/geometry_admission.py", node_geometry_admission,
             lambda j: {**N._VMTK, "job_id": str(j), "geometry": geometry},
             StaleExecutionPublish),
            ("pipeline/executor.py", node_executor,
             lambda j: {"job_id": str(j), "openfoam_workspace": str(tmp / "stale"),
                        "engine": "gmsh", "retry_count": 0}, StaleWorkerFenced)):
        result = await N._stale_refusal(mp, node, state, expected=expected)
        jobs.append(result["job_id"])
        attempts.append({
            "root": root, "refused_with": expected.__name__,
            "published": [(r["module"], r["fn"], r["method"]) for r in result["seen"]],
            "sequence_moved": result["after"]["seq"] != result["before"]["seq"],
            "backlog_moved": result["after"]["backlog"] != result["before"]["backlog"],
            "fence_disturbed": result["after"]["fence"] != result["before"]["fence"],
            "fence_present": bool(result["after"]["fence"]),
        })
    return attempts


def measure_committed_scenarios(mp, tmp, jobs: list) -> tuple[dict, list]:
    # `jobs` is filled as each run is created, so a failure part-way through still leaves the
    # caller the exact key names to clean up. The stale attempts run under the SAME observer:
    # if a refused publication ever reached the adapter, it would show up in the measurement.
    seen: list = []
    A.observe(mp, seen)

    spans: list = []

    async def go():
        await run_committed_scenarios(mp, tmp, jobs, seen, spans)
        accepted = len(seen)
        stale = await run_stale_attempts(mp, tmp, jobs)
        return accepted, stale

    accepted, stale = asyncio.run(go())
    result = A.measure(seen)
    result["stale_attempts"] = stale
    # WHICH executed scenario first reached each certified context.
    proof: dict = {}
    for span in spans:
        for record in seen[span["start"]:span["end"]]:
            proof.setdefault(record["context"], span["scenario"])
    result["proof_scenarios"] = proof
    result["scenario_order"] = [span["scenario"] for span in spans]
    result["publications_after_stale_attempts"] = len(seen) - accepted
    return result, seen


@pytest.fixture(scope="module")
def coverage(request, tmp_path_factory):
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    request.addfinalizer(mp.undo)
    jobs: list = []
    request.addfinalizer(lambda: _delete_job_keys(jobs))

    return measure_committed_scenarios(mp, tmp_path_factory.mktemp("ctx-coverage"), jobs)


async def test_the_instrument_is_sound_over_every_committed_scenario(coverage):
    result, _seen = coverage
    A.assert_measurement_sound(result)


async def test_the_snappy_scenarios_contribute_their_driver_contexts(coverage):
    result, _seen = coverage
    driver = sorted(c for c in result["observed"] if c.startswith("engines/snappy/drivers.py::"))
    assert len(driver) >= 12, (
        f"the committed Snappy scenarios reached {len(driver)} driver contexts, "
        "fewer than the 12 that suite already drives:\n  " + "\n  ".join(driver))


async def test_the_pipeline_batch_reaches_every_one_of_its_contexts(coverage):
    # The batch, derived from the manifest rather than listed here: every execution-owned context
    # in these three modules, plus the builder's relocated meshing announcement.
    result, _seen = coverage
    batch = sorted(c for c in A.targets()
                   if c.split("::")[0] in ("pipeline/executor.py", "pipeline/engine_select.py",
                                           "pipeline/geometry_admission.py")
                   or c.endswith("_run_announced_mesh::meshing#1"))
    missing = [c for c in batch if c not in set(result["observed"])]
    assert missing == [], (
        f"{len(missing)} of the {len(batch)} contexts in this batch were never executed:\n  "
        + "\n  ".join(missing))


async def test_no_previously_covered_context_regressed(coverage):
    # The 35 the last measurement reached must still be reached: this batch adds, never trades.
    result, _seen = coverage
    observed = set(result["observed"])
    batch_paths = ("pipeline/executor.py", "pipeline/engine_select.py",
                   "pipeline/geometry_admission.py")
    previous = [c for c in observed | set(result["missing"])
                if c.split("::")[0] not in batch_paths
                and not c.endswith("_run_announced_mesh::meshing#1")]
    assert len(observed & set(previous)) >= 35, (
        f"only {len(observed & set(previous))} of the previously covered contexts remain")


async def test_a_stale_attempt_covers_nothing_and_disturbs_nothing(coverage):
    result, _seen = coverage
    attempts = result["stale_attempts"]
    assert len(attempts) == 3, f"{len(attempts)} stale controls ran, not three"
    for a in attempts:
        assert a["published"] == [], f"{a['root']} published {a['published']} while superseded"
        assert not a["sequence_moved"], f"{a['root']}: a stale attempt advanced the sequence"
        assert not a["backlog_moved"], f"{a['root']}: a stale attempt reached the backlog"
        assert not a["fence_disturbed"], f"{a['root']}: a stale attempt disturbed the fence"
        assert a["fence_present"], f"{a['root']}: the current worker's fence is gone"
    assert result["publications_after_stale_attempts"] == 0, \
        "a refused publication still reached the adapter"


async def test_the_measured_coverage_is_reported_in_full(coverage):
    result, _seen = coverage
    print(f"\nEXECUTION CONTEXT COVERAGE  {result['observed_total']}/{result['target_total']}")
    print("\n  stale attempts (refused, and counted nowhere above)")
    for a in result["stale_attempts"]:
        print(f"    {a['root']:<36} refused with {a['refused_with']}, "
              f"published {len(a['published'])}, fence intact {not a['fence_disturbed']}")
    print("\n  executed, by path")
    for path, n in sorted(result["by_path"].items()):
        print(f"    {n:>3}  {path}")
    if result["missing"]:
        print("\n  missing, by path")
        for path, n in sorted(collections.Counter(
                c.split("::")[0] for c in result["missing"]).items()):
            print(f"    {n:>3}  {path}")
        print("\n  missing identities")
        for c in result["missing"]:
            print(f"    {c}")
    assert result["observed_total"] > 0, "no committed scenario reached any execution context"


async def test_every_execution_owned_publication_context_is_behaviorally_certified(coverage):
    # Every canonical execution-owned context in the committed manifest was reached by a real
    # production frame, under a real claim, through the real gate and fence. It fails if any
    # identity disappears from execution - which is why it runs against real services rather
    # than asserting a number.
    result, _seen = coverage
    A.assert_measurement_sound(result)
    A.assert_every_context_certified(result)
    assert result["observed_total"] == result["target_total"], (
        f"{result['observed_total']}/{result['target_total']} certified")
    assert set(result["observed"]) == set(result["targets"]), \
        "the certified set is not the manifest's execution-owned set"
