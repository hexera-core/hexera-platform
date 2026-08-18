# Responsibility: Verify a replayed logical event publishes once, while a genuine re-execution still publishes.
from __future__ import annotations

import contextlib
import json
import os
import uuid

import pytest

if not os.getenv("REDIS_URL"):
    pytest.skip("a real Redis endpoint is required", allow_module_level=True)


def _log(job: str) -> list[dict]:
    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.events.channels import log_key_for
    return [json.loads(x) for x in sync_client().lrange(log_key_for(job), 0, -1)]


def _event():
    import meshpipeline.events as E
    return E.UiEvent(type="note", stage="geometry_admission",
                     data={"text": "prepared", "tone": "info"})


def _pub(job):
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    return JobPublisher(job, agent="geometry_admission")


def test_a_replayed_logical_event_is_published_once():
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    for _ in range(3):                       # the node ran three times; one logical event
        pub._emit_once(ev, op_key="admission:prepared:gen1")

    assert len(_log(job)) == 1


def test_a_distinct_logical_operation_still_publishes():
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    pub._emit_once(ev, op_key="admission:prepared:gen1")
    pub._emit_once(ev, op_key="admission:prepared:gen2")

    assert len(_log(job)) == 2


def test_unkeyed_progress_stays_at_least_once_by_design():
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    pub._emit_once(ev)
    pub._emit_once(ev)

    assert len(_log(job)) == 2


def test_sequence_stays_monotonic_across_suppressed_replays():
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    pub._emit_once(ev, op_key="a")
    pub._emit_once(ev, op_key="a")           # suppressed
    pub._emit_once(ev, op_key="b")

    seqs = [r["seq"] for r in _log(job)]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), seqs


def test_a_suppressed_replay_reaches_the_replay_backlog_once():
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    for _ in range(4):
        pub._emit_once(ev, op_key="terminal:done")

    backlog = _log(job)
    assert len([r for r in backlog if r.get("text") == "prepared"]) == 1, backlog


def test_published_events_carry_no_storage_identity():
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    pub._emit_once(ev, op_key="k")
    blob = json.dumps(_log(job))
    for leak in ("sources/", "sha256", "bucket", "minio", "accesskey", "arn:"):
        assert leak.lower() not in blob.lower(), leak


# production wiring

def test_a_redelivered_terminal_event_publishes_once():
    from meshpipeline.persistence.repositories.terminal_outbox_repository import dedup_key_for
    job = str(uuid.uuid4())
    pub = _pub(job)
    key = dedup_key_for(job)

    for _ in range(3):                                  # the outbox row delivered three times
        pub.publish_terminal("Mesh generation finished.", key)

    log = _log(job)
    closings = [r for r in log if r.get("type") == "closing"]
    assert len(closings) == 1, closings


def test_a_later_generation_terminal_is_a_distinct_event():
    job = str(uuid.uuid4())
    pub = _pub(job)
    pub.publish_terminal("Mesh generation finished.", f"terminal:{job}")
    pub.publish_terminal("Mesh generation finished.", f"terminal:{job}:g2")

    assert len([r for r in _log(job) if r.get("type") == "closing"]) == 2


def test_the_operation_claim_expires_with_the_event_history():
    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.events.channels import log_key_for, opkey_set_for
    job = str(uuid.uuid4())
    _pub(job)._emit_once(_event(), op_key="k1")

    r = sync_client()
    log_ttl = r.ttl(log_key_for(job))
    ops_ttl = r.ttl(opkey_set_for(job))
    assert log_ttl > 0 and ops_ttl > 0, (log_ttl, ops_ttl)
    assert ops_ttl >= log_ttl - 2, (ops_ttl, log_ttl)   # claims live at least as long as history


def test_a_suppressed_replay_leaves_the_sequence_unconsumed():
    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.events.channels import seq_key_for
    job = str(uuid.uuid4())
    pub, ev = _pub(job), _event()
    pub._emit_once(ev, op_key="only")
    after_first = int(sync_client().get(seq_key_for(job)))
    pub._emit_once(ev, op_key="only")                   # suppressed
    assert int(sync_client().get(seq_key_for(job))) == after_first


# the public progress surface

@contextlib.contextmanager
def _generation(n: int):
    from meshpipeline.application import execution_fence as fence
    from meshpipeline.persistence.lease import ExecutionOwnership
    own = ExecutionOwnership(job_id=uuid.uuid4(), execution_generation=n,
                             worker_token=uuid.uuid4(), backend="test",
                             pipeline_deadline_at=None)
    with fence.execution_ownership(own):
        yield


def _texts(job: str) -> list[str]:
    return [e.get("text", e["type"]) for e in _log(job)]


def test_a_replayed_node_announcement_reaches_the_user_once():
    job = str(uuid.uuid4())
    pub = _pub(job)
    for _ in range(3):
        pub.stage(op_id="check:0")
        pub.note("Checking the mesh", op_id="check:0")

    assert _texts(job) == ["stage", "Checking the mesh"]


def test_successive_attempts_are_distinct_operations_despite_identical_words():
    job = str(uuid.uuid4())
    pub = _pub(job)
    for attempt in (1, 2, 3):
        pub.note("Carving the body out of the background mesh", op_id=f"snappy:carving:{attempt}")

    assert len(_log(job)) == 3


def test_a_genuine_re_execution_is_not_mistaken_for_a_replay():
    job = str(uuid.uuid4())
    with _generation(1):
        _pub(job).note("Designing the mesh", op_id="designing:0")
    with _generation(2):
        _pub(job).note("Designing the mesh", op_id="designing:0")

    assert len(_log(job)) == 2


def test_a_same_generation_takeover_suppresses_the_replayed_announcement():
    job = str(uuid.uuid4())
    for _ in range(2):
        with _generation(7):
            _pub(job).note("Designing the mesh", op_id="designing:0")

    assert len(_log(job)) == 1


def test_two_stages_may_use_the_same_phase_name_without_colliding():
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    job = str(uuid.uuid4())
    JobPublisher(job, agent="executor").note("Every check passed", op_id="outcome:0")
    JobPublisher(job, agent="reviewer").note("Every check passed", op_id="outcome:0")

    assert len(_log(job)) == 2


def test_the_attempt_counter_identifies_itself_without_the_caller_helping():
    job = str(uuid.uuid4())
    pub = _pub(job)
    for _ in range(2):
        pub.attempt(1, 4)          # replay of attempt 1
    pub.attempt(2, 4)              # a real second attempt

    assert [e["n"] for e in _log(job)] == [1, 2]


def test_a_replayed_inspection_render_is_not_shown_twice():
    job = str(uuid.uuid4())
    pub = _pub(job)
    for _ in range(2):
        pub.screenshot("aGVsbG8=", op_id="opening-render:0")

    assert len(_log(job)) == 1


def test_a_reconnecting_viewer_replays_each_operation_exactly_once():
    job = str(uuid.uuid4())

    def _worker():                                  # the same node body, run twice
        pub = _pub(job)
        pub.stage(op_id="check:0")
        pub.note("Checking the mesh", op_id="check:0")
        pub.note("Trying a solver run on it", op_id="solvability:0")

    with _generation(3):
        _worker()                                   # crashed after the third publication
        _worker()                                   # takeover re-ran the node

    assert _texts(job) == ["stage", "Checking the mesh", "Trying a solver run on it"]


def test_suppressed_replays_consume_no_sequence_numbers():
    job = str(uuid.uuid4())
    pub = _pub(job)
    with _generation(1):
        pub.note("a", op_id="p1")
        pub.note("a", op_id="p1")                   # replay
        pub.note("b", op_id="p2")

    assert [e["seq"] for e in _log(job)] == [1, 2]


# The three builder progress events used to have no way for the caller to name the occurrence, so a
# replayed builder node republished them verbatim: two "Meshed 1000 cells" lines in one run's
# history, two entries in the retained backlog, and two live frames. Their sibling in the same
# block (`awarn(op_id=f"defects:{_n}")`) was already replay-safe, and `_pub_seq` exists precisely
# so a rebuilt executor recounts onto the same identities.

def test_a_replayed_mesh_completion_reaches_the_user_once():
    job = str(uuid.uuid4())
    for _ in range(2):                       # the node runs, crashes pre-checkpoint, runs again
        _pub(job).meshed(1000, op_id="meshed:1")
    assert len(_log(job)) == 1


def test_a_genuine_second_mesh_is_still_its_own_event():
    job = str(uuid.uuid4())
    _pub(job).meshed(1000, op_id="meshed:1")
    _pub(job).meshed(2400, op_id="meshed:2")
    assert len(_log(job)) == 2


def test_a_replayed_written_file_notice_reaches_the_user_once():
    job = str(uuid.uuid4())
    for _ in range(2):
        _pub(job).file("case/system/blockMeshDict", 812, "created", op_id="file:3")
    assert len(_log(job)) == 1


def test_a_replayed_meshing_announcement_reaches_the_user_once():
    job = str(uuid.uuid4())
    for _ in range(2):
        _pub(job).meshing("cfmesh", 600, None, op_id="meshing:1")
    assert len(_log(job)) == 1
