# Responsibility: Verify the native-submission recorder is durable and the real cfMesh path reaches it once.
# Boundaries: the recorder and the production dispatch boundary; crash and replay behaviour are another suite's.
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from tests.integration.native_submission_recorder import (
    NativeSubmissionRecorder,
    RecordingMeshExecutor,
    cfmesh_context,
    preparation_preconditions,
    required_files,
    workspace_digest,
)

from meshpipeline.persistence.repositories import native_submission_repository as claims

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required for the claim", allow_module_level=True)

ENGINE = "cfmesh"


@pytest.fixture(autouse=True)
def _composed_executor_is_restored():
    # THE INVARIANT, ENFORCED RATHER THAN HOPED FOR. `installed` restores the composed port on
    # teardown, but one test below calls set_mesh_executor directly and never removes it - so the
    # global composition leaked into whatever ran next. In declaration order the only thing that ran
    # next was the assertion that nothing is installed, so the leak read as a pass; under a
    # randomised order it failed, which is the same leak seen from the other side.
    from meshpipeline.contracts import mesh_execution

    yield
    mesh_execution.set_mesh_executor(None)


@pytest.fixture()
def recorder(tmp_path):
    made = NativeSubmissionRecorder(tmp_path / "recorder").start()
    yield made
    made.stop()


# the recorder's own contract


async def test_a_request_is_durable_before_the_client_is_answered(recorder, tmp_path):
    # THE property the crash window depends on: the record exists while the client is still
    # blocked, so a client that dies here leaves proof it submitted.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "system").mkdir()
    (workspace / "system" / "meshDict").write_text("x")
    client = RecordingMeshExecutor(recorder.socket_path, job_id="j-1", execution_generation=3)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.run, workspace, engine=ENGINE, timeout=420)
        accepted = recorder.wait_accepted()
        assert accepted["attempt"] == 1
        # durable NOW, while the caller has had no answer
        records = recorder.records()
        assert len(records) == 1, records
        assert not pending.done(), "the recorder answered before the parent released it"
        recorder.allow_response()
        result = pending.result(timeout=30)

    assert result["rc"] == 0
    record = recorder.records()[0]
    assert record["job_id"] == "j-1" and record["execution_generation"] == 3
    assert record["engine"] == ENGINE and record["timeout"] == 420
    assert record["payload_digest"] == workspace_digest(workspace)
    assert len(record["payload_digest"]) == 64


async def test_two_requests_are_two_distinguishable_records(recorder, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a").write_text("1")
    client = RecordingMeshExecutor(recorder.socket_path, job_id="j-2", execution_generation=1)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        for expected in (1, 2):
            pending = pool.submit(client.run, workspace, engine=ENGINE, timeout=60)
            assert recorder.wait_accepted()["attempt"] == expected
            recorder.allow_response()
            pending.result(timeout=30)

    records = recorder.records()
    assert [r["attempt"] for r in records] == [1, 2], records
    assert records[0]["monotonic"] <= records[1]["monotonic"]


async def test_the_record_survives_the_death_of_the_client_process(recorder, tmp_path):
    # A CLIENT KILLED between acceptance and response. This is the shape of the crash window, in
    # miniature: the process that submitted is gone and the submission is still on record.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a").write_text("1")
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0, sys.argv[1]);"
         "from tests.integration.native_submission_recorder import RecordingMeshExecutor as R;"
         "R(sys.argv[2], job_id='j-killed', execution_generation=7)"
         ".run(sys.argv[3], engine='cfmesh', timeout=60)",
         str(Path(__file__).parents[2]), str(recorder.socket_path), str(workspace)])
    try:
        recorder.wait_accepted()
        assert recorder.records()[0]["job_id"] == "j-killed"
        child.kill()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    recorder.allow_response()          # nobody is listening; the recorder must not wedge
    assert len(recorder.records()) == 1
    assert recorder.records()[0]["execution_generation"] == 7


async def test_the_recorder_shuts_down_and_leaves_nothing(tmp_path):
    made = NativeSubmissionRecorder(tmp_path / "r2").start()
    socket_path, accepted, release = made.socket_path, made.accepted_fifo, made.release_fifo
    assert socket_path.exists()
    made.stop()
    assert not socket_path.exists() and not accepted.exists() and not release.exists()
    with pytest.raises(OSError):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(socket_path))
        finally:
            s.close()


# the real cfMesh preparation path


async def test_the_fixture_satisfies_every_current_cfmesh_precondition(tmp_path):
    from meshpipeline.agents.builder.tools.meshing import PreparedMeshRun

    ctx, workspace = cfmesh_context(tmp_path, job_id=str(uuid.uuid4()), generation=1)
    checks = preparation_preconditions(ctx, workspace)

    assert checks["input_contract_rejection"] == "", \
        f"the geometry was rejected by the engine's input contract: {checks}"
    assert checks["missing_required_files"] == [], checks
    assert checks["mesh_ok_present"] is False, "the fixture pre-declares a production mesh"
    assert isinstance(checks["prepared"], PreparedMeshRun), checks
    assert checks["refusal"] is None, f"preparation refused: {checks['refusal']}"
    assert checks["cap"] > 0, checks


async def test_a_missing_required_file_refuses_before_anything_is_submitted(recorder, tmp_path):
    from meshpipeline.agents.builder.tools.meshing import prepare_mesh_run

    ctx, _ = cfmesh_context(tmp_path, job_id=str(uuid.uuid4()), generation=1,
                             omit=required_files()[0])
    prepared = prepare_mesh_run(ctx)
    assert prepared.refusal is not None, "a workspace missing a required file was prepared anyway"
    assert required_files()[0] in prepared.refusal["error"]
    assert recorder.records() == [], "a refused preparation reached the provider"


# the normal production path, end to end


@pytest.fixture()
def installed(recorder):
    # ONLY the composed port is replaced. `set_mesh_executor` is the production seam; everything
    # above it - preparation, dispatch, ownership, fencing - stays production code.
    from meshpipeline.contracts import mesh_execution

    def install(job_id: str, generation: int):
        mesh_execution.set_mesh_executor(
            RecordingMeshExecutor(recorder.socket_path, job_id=job_id,
                                  execution_generation=generation))
    yield install
    mesh_execution.set_mesh_executor(None)


async def test_the_production_dispatch_path_reaches_the_recorder_exactly_once(
        recorder, installed, tmp_path):
    # prepare_mesh_run -> dispatch_prepared_mesh -> execute_prepared_mesh_run
    #   -> run_cartesian_mesh -> contracts.mesh_execution.run_mesh -> the installed executor
    from concurrent.futures import ThreadPoolExecutor

    from meshpipeline.agents.builder.tools import dispatch_prepared_mesh
    from meshpipeline.agents.builder.tools.meshing import prepare_mesh_run

    job_id, generation = str(uuid.uuid4()), 4
    ctx, workspace = cfmesh_context(tmp_path, job_id=job_id, generation=generation)
    installed(job_id, generation)
    prepared = prepare_mesh_run(ctx)
    assert prepared.refusal is None

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(dispatch_prepared_mesh, ctx, prepared)
        accepted = recorder.wait_accepted()
        assert accepted["attempt"] == 1
        assert not pending.done(), "dispatch returned before the provider answered"
        recorder.allow_response()
        raw = pending.result(timeout=120)

    records = recorder.records()
    assert len(records) == 1, f"the production path submitted {len(records)} times: {records}"
    assert records[0]["engine"] == ENGINE
    assert records[0]["execution_generation"] == generation
    assert records[0]["job_id"] == job_id
    assert json.loads(raw), "dispatch returned no serialised result"
    # cfMesh writes no production-grade marker - the only writer of `.mesh_ok` is the snappy
    # driver, so nothing on this path records that a submission happened.
    assert not (workspace / ".mesh_ok").exists()


async def test_an_unavailable_recorder_fails_dispatch_safely(installed, tmp_path):
    from meshpipeline.agents.builder.tools import dispatch_prepared_mesh
    from meshpipeline.agents.builder.tools.meshing import prepare_mesh_run
    from meshpipeline.contracts import mesh_execution

    job_id = str(uuid.uuid4())
    ctx, _ = cfmesh_context(tmp_path, job_id=job_id, generation=1)
    prepared = prepare_mesh_run(ctx)
    assert prepared.refusal is None
    mesh_execution.set_mesh_executor(
        RecordingMeshExecutor(tmp_path / "no-such-socket", job_id=job_id, execution_generation=1))

    raw = dispatch_prepared_mesh(ctx, prepared)
    result = json.loads(raw)
    # a tool crash is model feedback, never a node death - the established safe outcome
    assert "error" in json.dumps(result).lower(), result


async def test_the_composed_executor_is_restored_after_each_test():
    from meshpipeline.contracts import mesh_execution

    with pytest.raises(Exception, match="no MeshExecutor configured"):
        mesh_execution.run_mesh(Path("/tmp"), engine=ENGINE, timeout=1)


# the pre-durable crash window


def _await_marker(lines, name, timeout=120):
    # An event, polled only because the child's stdout is the only channel it has.
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(f'"marker": "{name}"' in line for line in list(lines)):
            return True
        time.sleep(0.05)
    raise AssertionError(f"the child never reached {name}: {list(lines)}")


def _submit_child(recorder, root, job_id, generation, lines, worker_token, *, hold=False):
    import threading

    from tests.integration.native_submission_recorder import SUBMITTING_CHILD

    proc = subprocess.Popen(
        [sys.executable, "-c", SUBMITTING_CHILD, str(recorder.socket_path), str(root),
         str(job_id), str(generation), str(worker_token), str(Path(__file__).parents[2])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, **({"HOLD_AFTER_DISPATCH": "1"} if hold else {})})
    threading.Thread(target=lambda: [lines.append(ln.rstrip()) for ln in proc.stdout],
                     daemon=True).start()
    return proc


@pytest.fixture()
async def crash_window(recorder, tmp_path):
    # THE WINDOW: a submission the provider has durably accepted, a response withheld, the node
    # not returned, and the worker killed there. Then the SAME generation runs the same
    # production path again - under a REAL claim, because the repair lives in the claim.
    from tests.integration import execution_ownership_support as ownership
    from tests.integration.native_submission_recorder import bounded, markers

    backend_execution = f"be-{uuid.uuid4().hex[:8]}"
    job_id, own_a, sessions, engine = await ownership.seeded_claim(
        "replay", backend_execution_id=backend_execution)
    generation = own_a.execution_generation
    a_lines: list = []
    b_lines: list = []
    a = b = None
    try:
        a = _submit_child(recorder, tmp_path / "a", job_id, generation, a_lines,
                          own_a.worker_token, hold=True)
        bounded("process A recorder acceptance", recorder.wait_accepted)
        # THE SUBMISSION SUCCEEDS - that is the premise. A learns the operation reference and
        # records the acceptance durably, then dies before the graph checkpoint.
        bounded("release of process A's response", recorder.allow_response)
        bounded("process A reaching the un-checkpointed gap",
                lambda: _await_marker(a_lines, "holding_before_checkpoint"))
        state = {
            "job_id": str(job_id), "generation": generation,
            "records_at_kill": recorder.records(),
            "mesh_ok_at_kill": any(p.name == ".mesh_ok" for p in (tmp_path / "a").rglob("*")),
            "process_a_alive_at_kill": a.poll() is None,
            "process_a_markers": markers(a_lines),
        }
        a.kill()
        a.wait(timeout=60)
        state["process_a_returncode"] = a.returncode
        state["claim_after_kill"] = claims.current(job_id=job_id,
                                                   execution_generation=generation)

        # The killed worker's lease still stands - the anti-duplicate guard refuses a second
        # runner while it does, which is correct. Expiring it is what a dead worker's lease does
        # on its own; doing it here is the only way to reach the replay without waiting it out.
        async with sessions() as db:
            from sqlalchemy import text
            await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                                  "interval '1 second' where id = :j"), {"j": job_id})
            await db.commit()

        # THE REDELIVERY, as this platform actually performs it: the same backend execution comes
        # back, so the generation is preserved and only the worker token rotates. A replacement
        # that got a new generation would be a new run and would prove nothing about a replay.
        own_b = await ownership.claim(sessions, job_id, backend_execution_id=backend_execution)
        state["generation_b"] = own_b.execution_generation
        state["token_rotated"] = own_b.worker_token != own_a.worker_token

        b = _submit_child(recorder, tmp_path / "b", job_id, generation, b_lines,
                          own_b.worker_token)
        b.wait(timeout=180)
        state["process_b_returncode"] = b.returncode
        state["process_b_markers"] = markers(b_lines)
        state["records_final"] = recorder.records()
        state["collections"] = recorder.collections()
        state["claim_final"] = claims.current(job_id=job_id, execution_generation=generation)
        yield state
    finally:
        for proc in (a, b):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=60)
        await engine.dispose()


async def test_the_crash_window_is_actually_reached(crash_window):
    # This must hold for the xfail below to mean anything: if the window is not reached, the
    # equality assertion would be measuring something else entirely.
    state = crash_window
    assert [m["marker"] for m in state["process_a_markers"]][-1] == "holding_before_checkpoint", (
        f"process A did not die in the un-checkpointed gap: {state['process_a_markers']}")
    assert state["claim_after_kill"]["disposition"] == "accepted", (
        "the submission was not durably accepted before the kill, so this is a different window")
    assert len(state["records_at_kill"]) == 1, state["records_at_kill"]
    assert state["process_a_alive_at_kill"] is True, "process A had already exited"
    assert state["process_a_returncode"] in (-9, 137), state["process_a_returncode"]
    assert state["mesh_ok_at_kill"] is False, "a marker existed, so this is not the open window"
    # the replacement really did run the production path to completion
    assert [m["marker"] for m in state["process_b_markers"]][-1] == "child_exited"


async def test_both_submissions_carry_one_generation_and_one_payload(crash_window):
    records = crash_window["records_final"]
    assert len({r["job_id"] for r in records}) == 1, records
    assert len({r["execution_generation"] for r in records}) == 1, (
        "the replacement ran under a different generation - that is a new run, not a replay")
    assert len({r["payload_digest"] for r in records}) == 1, (
        "the two requests carried different workspaces")
    assert all(r["engine"] == ENGINE for r in records)


async def test_a_same_generation_replay_submits_the_native_work_once(crash_window):
    # THE REPAIRED DEFECT. Process A's submission was durably accepted and process A died before
    # the graph checkpoint; process B ran the same node again under the same generation. The
    # provider must have been contacted exactly once.
    state = crash_window
    records = state["records_final"]
    assert len(records) == 1, (
        f"the same logical mesh run was submitted {len(records)} times to the provider: "
        f"{[(r['attempt'], r['execution_generation']) for r in records]}")

    # ... and the replacement RESUMED rather than simply doing nothing: it read the accepted
    # operation back from the same namespace. Absence of a second submission is not, on its own,
    # evidence of that.
    assert len(state["collections"]) == 1, state["collections"]
    assert state["collections"][0]["operation_key"] == records[0]["operation_key"] != "", (
        "the replacement resumed under a different exchange namespace")


async def test_the_replacement_used_the_first_worker_s_exchange_namespace(crash_window):
    # The operation key is the exchange id, so this is the GCS namespace: same key, same objects.
    state = crash_window
    expected = claims.operation_key(state["job_id"], state["generation"])
    assert state["records_final"][0]["operation_key"] == expected
    assert state["collections"][0]["operation_key"] == expected


async def test_the_accepted_operation_is_durable_and_names_the_provider_reference(crash_window):
    claim = crash_window["claim_final"]
    assert claim is not None, "no claim survived the crash, so nothing could be resumed"
    assert claim["disposition"] == "accepted", claim
    assert claim["provider_reference"].startswith("projects/"), claim
    # THE INTEGRITY CHECK: the digest stored with the claim describes the bundle that was
    # actually uploaded, so a replay carrying different workspace content cannot reuse it.
    assert claim["payload_digest"] == crash_window["records_final"][0]["payload_digest"] != ""
    assert claim["engine"] == ENGINE


async def test_the_replacement_ran_under_a_rotated_token_and_one_generation(crash_window):
    state = crash_window
    assert state["generation_b"] == state["generation"], (
        "the replacement got a new generation - that is a new run, not a replay")
    assert state["token_rotated"] is True, "the worker token did not rotate across the takeover"


# the two windows on either side of the accepted submission


async def _bound_child(recorder, root, job_id, generation, token, lines, *, script=None):
    import threading

    from tests.integration.native_submission_recorder import SUBMITTING_CHILD
    proc = subprocess.Popen(
        [sys.executable, "-c", script or SUBMITTING_CHILD, str(recorder.socket_path), str(root),
         str(job_id), str(generation), str(token), str(Path(__file__).parents[2])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [lines.append(ln.rstrip()) for ln in proc.stdout],
                     daemon=True).start()
    return proc


async def test_a_worker_killed_after_claiming_and_before_the_provider_call_never_fabricates_success(
        recorder, tmp_path):
    # THE OTHER SIDE of the window: the claim exists, nothing was submitted, and the truth is
    # that nobody knows. The replacement must neither submit nor report success.
    from tests.integration import execution_ownership_support as ownership
    from tests.integration.native_submission_recorder import markers

    backend_execution = f"be-{uuid.uuid4().hex[:8]}"
    job_id, own_a, sessions, engine = await ownership.seeded_claim(
        "preclaim", backend_execution_id=backend_execution)
    generation = own_a.execution_generation
    lines: list = []
    proc = None
    try:
        # A claims and dies before ever reaching the provider - written directly, because a
        # process killed between two statements cannot be timed reliably from outside.
        claimed = claims.claim(job_id=job_id, execution_generation=generation,
                               worker_token=own_a.worker_token, engine=ENGINE,
                               payload_digest="c" * 64)
        assert claimed.may_submit
        assert recorder.records() == [], "something was submitted before the provider was called"

        async with sessions() as db:
            from sqlalchemy import text
            await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                                  "interval '1 second' where id = :j"), {"j": job_id})
            await db.commit()
        own_b = await ownership.claim(sessions, job_id, backend_execution_id=backend_execution)

        proc = await _bound_child(recorder, tmp_path / "b", job_id, generation,
                                  own_b.worker_token, lines)
        proc.wait(timeout=180)

        assert recorder.records() == [], (
            "the replacement submitted work under a claim it did not hold")
        assert recorder.collections() == [], "it collected a result that was never submitted"
        body = [m for m in markers(lines) if m["marker"] == "dispatch_returned"]
        assert body, f"the replacement did not complete the production path: {markers(lines)}"
        # the SAFE infrastructure failure, not a mesh verdict and not a success
        assert "CLOUD_RUN_FAILED" in body[0]["body"], body[0]["body"]
        assert claims.current(job_id=job_id, execution_generation=generation)["disposition"] \
            == "claimed", "an unresolved claim was moved by a worker that never submitted"
    finally:
        for child in (proc, locals().get("second")):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=60)
        await engine.dispose()


#: A child whose provider connection is lost after the request was sent - the acknowledgement the
#: worker never receives. The submission itself is real and durable at the recorder.
LOSING_CHILD_PATCH = '''
from meshpipeline.contracts.mesh_execution import SubmissionIndeterminate
from tests.integration.native_submission_recorder import RecordingMeshExecutor as _R
_real = _R.run
def _lossy(self, workspace, *, engine, timeout, operation_key=""):
    _real(self, workspace, engine=engine, timeout=timeout, operation_key=operation_key)
    raise SubmissionIndeterminate("the acknowledgement was lost in transit")
_R.run = _lossy
'''


async def test_a_lost_acknowledgement_after_acceptance_is_recorded_as_indeterminate(
        recorder, tmp_path):
    from tests.integration import execution_ownership_support as ownership
    from tests.integration.native_submission_recorder import SUBMITTING_CHILD, markers

    lossy = SUBMITTING_CHILD.replace("ctx, ws = cfmesh_context",
                                     LOSING_CHILD_PATCH + "\nctx, ws = cfmesh_context")
    job_id, own, sessions, engine = await ownership.seeded_claim("lost")
    lines: list = []
    proc = None
    try:
        proc = await _bound_child(recorder, tmp_path / "a", job_id, own.execution_generation,
                                  own.worker_token, lines, script=lossy)
        recorder.wait_accepted()
        recorder.allow_response()
        proc.wait(timeout=180)

        # The provider really was contacted, once, and the worker really did not learn the answer.
        assert len(recorder.records()) == 1, recorder.records()
        claim = claims.current(job_id=job_id, execution_generation=own.execution_generation)
        assert claim["disposition"] == "indeterminate", claim
        assert not claim["provider_reference"], "an unacknowledged call was recorded as accepted"

        body = [m for m in markers(lines) if m["marker"] == "dispatch_returned"]
        assert body and "CLOUD_RUN_FAILED" in body[0]["body"], markers(lines)

        # AND IT IS NOT RETRIED - measured by running the node a SECOND time, not by asking the
        # repository. The redelivery is the real one: same generation, rotated token.
        async with sessions() as db:
            from sqlalchemy import text
            await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                                  "interval '1 second' where id = :j"), {"j": job_id})
            await db.commit()
        own_b = await ownership.claim(sessions, job_id)
        second_lines: list = []
        second = await _bound_child(recorder, tmp_path / "b", job_id,
                                    own.execution_generation, own_b.worker_token, second_lines)
        second.wait(timeout=180)
        assert len(recorder.records()) == 1, (
            "the indeterminate operation was resubmitted by the replacement")
        assert recorder.collections() == [], (
            "an unacknowledged submission was resumed as though it had been accepted")
        replayed = [m for m in markers(second_lines) if m["marker"] == "dispatch_returned"]
        assert replayed and "CLOUD_RUN_FAILED" in replayed[0]["body"], markers(second_lines)
        assert claims.current(job_id=job_id, execution_generation=own.execution_generation)[
            "disposition"] == "indeterminate", "the replacement moved an ambiguous outcome"
    finally:
        for child in (proc, locals().get("second")):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=60)
        await engine.dispose()
