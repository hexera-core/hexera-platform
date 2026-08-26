# Planner capture identity: every real model round of one node execution gets its own op_id
# (the driver run's plan-call index), and a rebuilt BuilderDriverRun regenerates the same
# sequence - so crash-resume replays dedupe while genuine disagreements still quarantine.
# The old spelling gave drive()'s initial call and the repair loop's re-plan the SAME op_id,
# which fail-opened as a CONFLICTING replay quarantine on a healthy tugboat run.
from types import SimpleNamespace

from meshpipeline.agents.builder.driver_run import BuilderDriverRun


def _run():
    return BuilderDriverRun(job_id="j", engine="snappy", mode="initial")


def test_plan_call_index_advances_only_on_real_rounds():
    run = _run()
    assert run.plan_call_index == 1
    run.note_plan_round(None)                      # no model call happened
    assert run.plan_call_index == 1
    run.note_plan_round(_round())
    assert run.plan_call_index == 2
    run.note_plan_round(_round())
    assert run.plan_call_index == 3


def test_reexecuted_node_reproduces_identical_indices():
    seq1 = _replay()
    seq2 = _replay()                               # a rebuilt run = a re-executed node
    assert seq1 == seq2 == [1, 2, 3]


def _replay():
    run = _run()
    out = []
    for _ in range(3):
        out.append(run.plan_call_index)
        run.note_plan_round(_round())
    return out


def _round(marker=""):
    return SimpleNamespace(failure_marker=marker, assistant_text="", input_tokens=1,
                           cached_input_tokens=0, output_tokens=1, tool_calls=[],
                           finish_reason="stop", provider=SimpleNamespace(attempts=1))


async def test_failing_rounds_get_distinct_op_ids(tmp_path, monkeypatch):
    # Three failed planner rounds of one attempt must write three DISTINCT capture op_ids.
    import meshpipeline.cad.analysis as _ana
    import meshpipeline.cad.prepared_surface as _prep
    import meshpipeline.engines.snappy.planner as planner

    (tmp_path / "input.stl").write_bytes(b"solid s\nendsolid s\n")
    monkeypatch.setattr(_prep, "require_metre_surface", lambda *a, **k: "surface")
    monkeypatch.setattr(_ana, "analyze_surface", lambda s: {
        "diag": 1.0, "extent": [1.0, 1.0, 1.0], "surface_area": 1.0, "min_feature": 0.01})
    monkeypatch.setattr(_ana, "recommend_refinement", lambda a, max_cells: {
        "surface_level": 5, "feature_level": 6, "afford_level": 5})

    async def _failing_round(messages, tools=None, tool_choice="none", job_id="", **kw):  # noqa: ARG001
        return _round(marker="provider_timeout")

    monkeypatch.setattr(planner.llm_router, "call_planner_model", _failing_round)
    seen = []
    monkeypatch.setattr(planner, "_log_plan_event",
                        lambda job_id, payload, *, op_id, attempt: seen.append(op_id))

    run = _run()
    for _ in range(3):
        po = await planner.plan_with_accounting(
            workspace=tmp_path, job_id="j", request_txt="mesh it",
            attempt=2, native_attempt=1, plan_call=run.plan_call_index)
        run.note_plan_round(po.round)

    assert len(seen) == 3 and len(set(seen)) == 3, seen
    assert seen == ["planner:2:1:1", "planner:2:1:2", "planner:2:1:3"]
