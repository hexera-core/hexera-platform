# Responsibility: Verify a terminal-failed run with a surviving workspace re-delivers, that nothing else does, and that its status never moves.
from __future__ import annotations

import uuid

import pytest

from meshpipeline.application import artifact_uploader as AU
from meshpipeline.application import final_result as FR
from meshpipeline.application import redeliver as RD
from meshpipeline.application import terminal_finalize as TF
from meshpipeline.persistence.models import JobStatus

JOB = str(uuid.uuid4())
ENGINE = "cfmesh_conjugate"
PURPOSE = "conjugate_heat_transfer"

#: what the job's rows carry BEFORE the re-delivery: the repair report landed, the mesh never did
BEFORE = ["repair_report"]
#: and after it: the bundle and the viewer payload the policy requires, plus the optional preview
AFTER = ["mesh", "mesh_bundle", "repair_report", "viewer_data"]


class _Log:
    def __init__(self): self.lines = []
    def info(self, *a, **k): self.lines.append(("info", a))
    def warning(self, *a, **k): self.lines.append(("warning", a))
    def error(self, *a, **k): self.lines.append(("error", a))


class _Session:
    def __init__(self, row=None): self.row = row
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): return None
    async def execute(self, _stmt):
        row = self.row

        class _Res:
            def scalar_one_or_none(self): return row
        return _Res()


def _sessions(row=None):
    def _factory():
        return _Session(row)
    return _factory


def _record(**over) -> dict:
    """The terminal record of the incident, built by the product rather than typed out here: a
    failed run whose review did not conclude, carrying the `missing_outputs` that are the lie."""
    fr = FR.build_final_result(
        job_id=JOB, owner_id="rehaan-dev", status=FR.TerminalStatus.failed, engine=ENGINE,
        purpose=PURPOSE, dimensionality="3D", approved_snapshot_id="",
        executor_success=True, reviewer_verdict="", failed_gate="",
        api_failure="reviewer_evidence_missing", attempts=1, attempts_max=5,
        required_ready=False, delivered_types=[], optional_warnings=[])
    return {**fr.to_dict(), **over}


class _Row:
    def __init__(self, *, status=JobStatus.failed, purged=False, record=None):
        self.id = JOB
        self.status = status
        self.owner_id = "rehaan-dev"
        self.execution_generation = 1
        self.current_attempt = 1
        self.workspace_purged = purged
        self.final_result = _record() if record is None else record


class _Repo:
    def __init__(self, row): self.row = row; self.calls = []; self.written = []

    async def get_internal(self, db, jid):
        self.calls.append("get_internal")
        return self.row

    async def get_final_result(self, db, jid):
        self.calls.append("get_final_result")
        return self.row.final_result

    async def set_final_result(self, db, jid, record):
        self.calls.append("set_final_result")
        self.row.final_result = record
        self.written.append(record)

    async def transition(self, db, jid, target, **kw):
        self.calls.append("transition")
        raise AssertionError("re-delivery moved a terminal job's status")


class _Pub:
    calls: list = []
    def __init__(self, job_id): self.job_id = job_id
    def note(self, msg, op_id=""): _Pub.calls.append(("note", op_id))
    def error(self, msg, op_id=""): _Pub.calls.append(("error", op_id))


async def _noop_orphans(*a, **k): return None


@pytest.fixture(autouse=True)
def _reset():
    _Pub.calls = []


@pytest.fixture
def delivery(monkeypatch):
    """The real delivery, replaced by a recorder: these tests are about the entry point's guard and
    its bookkeeping, and the uploader itself has its own suite."""
    seen: dict = {}

    async def _deliver(session_factory, **kw):
        seen.update(kw)
        return AU.RunDelivery([{"type": "mesh_bundle", "size_bytes": 10616027998}], True, None, None)

    monkeypatch.setattr(AU, "deliver_succeeded_run", _deliver)
    return seen


@pytest.fixture
def types(monkeypatch):
    """What the durable artifact rows say, read once before the delivery and once after."""
    script = {"reads": [BEFORE, AFTER]}

    async def _read(session_factory, job_id, *, jlog):
        return script["reads"].pop(0) if len(script["reads"]) > 1 else script["reads"][0]

    monkeypatch.setattr(TF, "read_delivered_types", _read)
    return script


@pytest.fixture
def workspace(tmp_path):
    """The run's own attempt directory, laid out as the builder lays it out: the job id is a path
    component, which is what makes a workspace this job's rather than another run's."""
    ws = tmp_path / JOB / "generation_1" / "attempt_1"
    ws.mkdir(parents=True)
    return ws


async def _run(repo, *, workspace, row=None):
    return await RD.redeliver_job(_sessions(row), repo, JOB, jlog=_Log(),
                                  workspace=str(workspace), publish=_Pub,
                                  persist_orphans=_noop_orphans)


# THE CASE IT EXISTS FOR

async def test_a_terminal_failed_run_whose_workspace_survived_is_delivered_again(
        workspace, delivery, types):
    repo = _Repo(_Row())
    out = await _run(repo, workspace=workspace)
    assert out.refused == "", f"the re-delivery was refused: {out.refused}"
    assert out.required_ready and out.delivered_types == AFTER
    assert out.delivered == [{"type": "mesh_bundle", "size_bytes": 10616027998}]


async def test_it_delivers_through_the_product_not_a_copy_of_it(workspace, delivery, types):
    out = await _run(_Repo(_Row()), workspace=workspace)
    assert out.refused == ""
    # the production call shape (pipeline_run.py:1171): the same arguments, the run's own
    # coordinates, and a fence for the registration
    assert delivery["job_id"] == JOB and delivery["owner_id"] == "rehaan-dev"
    assert delivery["workspace"] == str(workspace) and delivery["engine"] == ENGINE
    # 0, not 1: delivery_attempt is a retry_count, and attempt_1 is retry_count 0
    assert delivery["delivery_attempt"] == 0 and delivery["execution_generation"] == 1
    assert delivery["publish"] is _Pub and delivery["persist_orphans"] is _noop_orphans
    assert callable(delivery["fence_commit"]), "the registration was left unfenced"


async def test_the_default_collaborators_are_the_pipelines_own(workspace, delivery, types):
    import meshpipeline.application.pipeline_run as pr

    out = await RD.redeliver_job(_sessions(), _Repo(_Row()), JOB, jlog=_Log(),
                                 workspace=str(workspace))
    assert out.refused == ""
    assert delivery["publish"] is pr._pub, "re-delivery publishes through something of its own"
    assert delivery["persist_orphans"] is pr._persist_orphans


# THE GUARD. Each of these must deliver NOTHING.

async def test_a_succeeded_job_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row(status=JobStatus.succeeded)), workspace=workspace)
    assert out.refused == RD.NOT_TERMINAL_FAILED
    assert not delivery, "a job that already delivered was delivered over"


async def test_a_cancelled_job_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row(status=JobStatus.cancelled)), workspace=workspace)
    assert out.refused == RD.NOT_TERMINAL_FAILED and not delivery


async def test_a_running_job_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row(status=JobStatus.running)), workspace=workspace)
    assert out.refused == RD.NOT_TERMINAL_FAILED and not delivery


async def test_the_attempt_is_read_off_the_directory_not_the_job_row(tmp_path, delivery, types):
    # current_attempt is the number a user is shown and the uploader's contract warns against it
    # by name; what the CAS wants is the retry_count, and attempt_<n> is retry_count + 1
    ws = tmp_path / JOB / "generation_1" / "attempt_3"
    ws.mkdir(parents=True)
    row = _Row()
    row.current_attempt = 1
    out = await _run(_Repo(row), workspace=ws)
    assert out.refused == ""
    assert delivery["delivery_attempt"] == 2, (
        "attempt_3 is retry_count 2; reading current_attempt would have said 1, and a low pair "
        "registers nothing at all")


async def test_a_workspace_with_no_attempt_number_is_refused(tmp_path, delivery, types):
    # guessing low is the dangerous direction: the CAS reads it as superseded and silently
    # registers nothing, which is this whole incident again
    ws = tmp_path / JOB / "generation_1" / "somewhere_else"
    ws.mkdir(parents=True)
    out = await _run(_Repo(_Row()), workspace=ws)
    assert out.refused == RD.UNNUMBERED_WORKSPACE and not delivery


async def test_a_purged_workspace_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row(purged=True)), workspace=workspace)
    assert out.refused == RD.WORKSPACE_PURGED
    assert not delivery, "a purged job was delivered from whatever is at the path now"


async def test_a_workspace_that_is_not_on_disk_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row()), workspace=workspace / "attempt_2")
    assert out.refused == RD.NO_WORKSPACE and not delivery


async def test_another_runs_workspace_is_refused(workspace, tmp_path, delivery, types):
    # the deliverable contract cannot catch this: another run of the SAME engine satisfies every
    # check it makes, and the result would be one customer holding another's mesh
    other = tmp_path / str(uuid.uuid4()) / "generation_1" / "attempt_1"
    other.mkdir(parents=True)
    out = await _run(_Repo(_Row()), workspace=other)
    assert out.refused == RD.FOREIGN_WORKSPACE
    assert not delivery, "another run's mesh was delivered under this job's id"


async def test_a_job_whose_required_outputs_are_already_ready_is_refused(workspace, delivery, types):
    types["reads"] = [["mesh_bundle", "viewer_data"]]
    out = await _run(_Repo(_Row()), workspace=workspace)
    assert out.refused == RD.ALREADY_DELIVERED
    assert not delivery, "a complete delivery was uploaded over"


async def test_readiness_is_decided_on_the_rows_not_on_the_record(workspace, delivery, types):
    # A record claiming the deliverables are ready does NOT block a job whose rows say otherwise.
    # The record is the thing this incident proved can be wrong about delivery, so the guard asks
    # the artifact rows - and this is the direction that would have hidden the whole failure.
    row = _Row(record=_record(required_ready=True,
                              delivered_types=["mesh_bundle", "viewer_data"], missing_outputs=[]))
    out = await _run(_Repo(row), workspace=workspace, row=row)
    assert out.refused == "", "a record's own claim of readiness blocked a real re-delivery"
    assert delivery["engine"] == ENGINE


async def test_a_job_with_no_terminal_record_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row(record={})), workspace=workspace)
    assert out.refused == RD.NO_TERMINAL_RECORD
    assert not delivery, "a job was delivered without knowing what it promised"


async def test_a_record_that_names_no_engine_is_refused(workspace, delivery, types):
    out = await _run(_Repo(_Row(record=_record(engine=""))), workspace=workspace)
    assert out.refused == RD.NO_TERMINAL_RECORD and not delivery


# THE STATUS

async def test_the_job_status_is_never_changed(workspace, delivery, types):
    row = _Row()
    repo = _Repo(row)
    out = await _run(repo, workspace=workspace, row=row)
    assert out.refused == ""
    assert row.status is JobStatus.failed, "the re-delivery moved the run out of failed"
    assert "transition" not in repo.calls
    assert set(repo.calls) <= {"get_internal", "get_final_result", "set_final_result"}, (
        f"the re-delivery wrote something other than the terminal record: {repo.calls}")


# THE RECORD

async def test_the_record_stops_claiming_the_outputs_are_missing(workspace, delivery, types):
    row = _Row()
    repo = _Repo(row)
    before = dict(row.final_result)
    assert before["missing_outputs"] == ["mesh_bundle", "viewer_data"]
    assert before["required_ready"] is False and before["delivered_types"] == []

    out = await _run(repo, workspace=workspace, row=row)
    assert out.refused == "" and out.restated

    after = repo.written[-1]
    assert after["required_ready"] is True
    assert after["delivered_types"] == AFTER
    assert after["missing_outputs"] == []


async def test_the_rest_of_the_record_is_left_exactly_as_the_run_wrote_it(workspace, delivery, types):
    row = _Row()
    repo = _Repo(row)
    before = dict(row.final_result)
    await _run(repo, workspace=workspace, row=row)
    after = repo.written[-1]
    for key in ("status", "outcome_code", "failure_category", "failure_cause", "failure_detail",
                "failure_next_step", "finalized_at", "executor_success", "review_execution",
                "attempts", "engine", "purpose"):
        assert after[key] == before[key], f"the re-delivery rewrote {key}"
    assert after["status"] == "failed"


async def test_the_closing_message_stops_saying_there_is_nothing_to_download(workspace, delivery,
                                                                             types):
    """The message MUST move, and only in what it says about the shelf.

    This test used to assert the opposite - that the closing message is identical before and
    after - on the reasoning that the renderer read `required_ready` only inside its succeeded
    branch, so re-stating it on a failed record could not turn a failure into a delivery. The
    first half was true and the conclusion was wrong: the renderer did not read the flag, so it
    went on asserting "No downloadable mesh deliverable is available." and "run it again ...
    nothing needs to change" beside a panel that was offering three downloads and drawing
    283,410,955 cells. A message that cannot change is not safe, it is just wrong for longer.

    What must still not move is the verdict on the run, and that is asserted here too: this is
    not told as a success, and the status, category and cause are checked by the test above.
    """
    row = _Row()
    repo = _Repo(row)
    before = FR.render_message(FR.FinalResult.from_dict(row.final_result))
    assert "No downloadable mesh deliverable is available." in before
    await _run(repo, workspace=workspace, row=row)
    after = FR.render_message(FR.FinalResult.from_dict(repo.written[-1]))
    assert after != before
    assert "No downloadable mesh deliverable is available." not in after
    assert "ready to download" in after
    # the run still failed, and the message still says so
    assert "Mesh generation completed successfully." not in after
    assert "still recorded as failed" in after


async def test_the_renderer_itself_cannot_say_a_failed_run_delivered():
    # WHY the re-statement above is written out instead of going through build_terminal_result:
    # the failure branch hardcodes the delivery fields, so the renderer reproduces the very lie
    # this entry point exists to correct, however it is called.
    fr = FR.build_final_result(
        job_id=JOB, owner_id="rehaan-dev", status=FR.TerminalStatus.failed, engine=ENGINE,
        purpose=PURPOSE, dimensionality="3D", approved_snapshot_id="", executor_success=True,
        reviewer_verdict="", failed_gate="", api_failure="reviewer_evidence_missing",
        attempts=1, attempts_max=5,
        required_ready=True, delivered_types=["mesh_bundle", "viewer_data"], optional_warnings=[])
    assert fr.required_ready is False and fr.delivered_types == []
    assert fr.missing_outputs == ["mesh_bundle", "viewer_data"]


# THE FENCE AND THE FAILURE PATH

async def test_the_registration_is_refused_if_the_job_stopped_being_what_was_guarded():
    from meshpipeline.application.execution_fence import StaleWorkerFenced

    fence = RD._terminal_fence(uuid.UUID(JOB), JobStatus.failed)
    await fence(_Session(_Row()))  # the job is still the terminal-failed run: no refusal

    for row in (None, _Row(status=JobStatus.pending), _Row(status=JobStatus.succeeded),
                _Row(purged=True)):
        with pytest.raises(StaleWorkerFenced):
            await fence(_Session(row))


async def test_a_delivery_that_fails_leaves_the_record_alone(workspace, monkeypatch, types):
    async def _deliver(session_factory, **kw):
        return AU.RunDelivery([], False, None, RuntimeError("store down"))

    monkeypatch.setattr(AU, "deliver_succeeded_run", _deliver)
    row = _Row()
    repo = _Repo(row)
    out = await _run(repo, workspace=workspace, row=row)
    assert out.refused == RD.DELIVERY_FAILED and not out.required_ready
    assert repo.written == [], "a failed delivery was written up as a delivered one"
    assert row.status is JobStatus.failed
