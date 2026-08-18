# Responsibility: Verify the Lua boundary refuses a write whose claim Redis no longer holds.
# Boundaries: the atomic fence check against a real Redis - the two-worker race is certification.
from __future__ import annotations

import hashlib
import os
import uuid

import pytest

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.event_stream import fence as F
from meshpipeline.contracts.event_stream import StaleExecutionPublish, expecting_fence
from meshpipeline.events.channels import (
    fence_key_for,
    log_key_for,
    opkey_set_for,
    seq_key_for,
)

if not os.getenv("REDIS_URL") and not provcfg.REDIS_URL:
    pytest.skip("a real Redis endpoint is required", allow_module_level=True)

OWN_GEN, OWN_TOKEN = 2, uuid.UUID(int=99)


def _client():
    import redis as _redis
    return _redis.from_url(provcfg.REDIS_URL)


def _keys(job_id: str) -> list[str]:
    return [seq_key_for(job_id), log_key_for(job_id), opkey_set_for(job_id),
            fence_key_for(job_id)]


def _snapshot(job_id: str) -> dict:
    r = _client()
    try:
        out = {}
        for key in _keys(job_id):
            raw = r.dump(key)
            out[key] = {"exists": bool(r.exists(key)),
                        "type": r.type(key).decode() if r.exists(key) else "none",
                        "pttl": r.pttl(key),
                        "sha256": hashlib.sha256(raw).hexdigest() if raw else ""}
        return out
    finally:
        r.close()


def _publisher(job_id: str):
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    return JobPublisher(job_id, agent="builder")


def _fp(job_id: str) -> str:
    return F.fingerprint(job_id, OWN_GEN, OWN_TOKEN)


@pytest.fixture()
def job():
    job_id = f"fence-{uuid.uuid4().hex[:12]}"
    yield job_id
    r = _client()
    try:
        for key in _keys(job_id):
            r.delete(key)
    finally:
        r.close()


# authorized


def test_a_matching_fence_publishes_normally(job):
    F.install(job, _fp(job), 60)
    with expecting_fence(_fp(job)):
        _publisher(job).note("hello", op_id="a:1")
    r = _client()
    try:
        assert r.llen(log_key_for(job)) == 1 and int(r.get(seq_key_for(job))) == 1
    finally:
        r.close()


def test_an_authorized_replay_is_still_suppressed(job):
    F.install(job, _fp(job), 60)
    with expecting_fence(_fp(job)):
        _publisher(job).note("hello", op_id="same")
        _publisher(job).note("hello", op_id="same")
    r = _client()
    try:
        assert r.llen(log_key_for(job)) == 1, "the replay was written a second time"
    finally:
        r.close()


def test_plain_terminal_publication_needs_no_fence(job):
    _publisher(job).publish_terminal("the run ended", "t:1")
    r = _client()
    try:
        assert r.llen(log_key_for(job)) == 1
        assert not r.exists(fence_key_for(job)), "terminal publication created a fence"
    finally:
        r.close()


# refused


@pytest.mark.parametrize("case", ["missing", "wrong generation", "wrong token", "revoked"])
def test_a_write_without_the_matching_fence_is_refused_and_mutates_nothing(job, case):
    F.install(job, _fp(job), 60)
    with expecting_fence(_fp(job)):
        _publisher(job).note("first", op_id="a:1")

    if case == "missing":
        _client().delete(fence_key_for(job))
        expectation = _fp(job)
    elif case == "wrong generation":
        expectation = F.fingerprint(job, OWN_GEN + 1, OWN_TOKEN)
    elif case == "wrong token":
        expectation = F.fingerprint(job, OWN_GEN, uuid.UUID(int=1234))
    else:
        F.revoke(job, _fp(job))
        expectation = _fp(job)

    before = _snapshot(job)
    with pytest.raises(StaleExecutionPublish), expecting_fence(expectation):
        _publisher(job).note("second", op_id="a:2")
    after = _snapshot(job)

    for key, was in before.items():
        for field in ("exists", "type", "sha256"):
            assert after[key][field] == was[field], (
                f"{case}: a refused write changed {key}.{field}")
        # a live TTL counts down on its own; what must never happen is a REFRESH
        assert after[key]["pttl"] <= was["pttl"], (
            f"{case}: a refused write extended the TTL on {key}")


def test_a_stale_replay_is_refused_rather_than_treated_as_idempotent(job):
    F.install(job, _fp(job), 60)
    with expecting_fence(_fp(job)):
        _publisher(job).note("hello", op_id="same")
    F.revoke(job, _fp(job))

    before = _snapshot(job)
    with pytest.raises(StaleExecutionPublish), expecting_fence(_fp(job)):
        _publisher(job).note("hello", op_id="same")     # the SAME op key
    after = _snapshot(job)
    for key, was in before.items():
        assert after[key]["sha256"] == was["sha256"] and after[key]["exists"] == was["exists"]
        assert after[key]["pttl"] <= was["pttl"]


def test_publication_never_touches_the_fence_key(job):
    F.install(job, _fp(job), 60)
    before = _snapshot(job)[fence_key_for(job)]
    with expecting_fence(_fp(job)):
        _publisher(job).note("hello", op_id="a:1")
    after = _snapshot(job)[fence_key_for(job)]
    assert before["sha256"] == after["sha256"] and before["exists"] == after["exists"]


# fence operations


def test_refresh_extends_only_a_matching_fence(job):
    F.install(job, _fp(job), 60)
    assert F.refresh(job, _fp(job), 120) is True
    assert _client().ttl(fence_key_for(job)) > 60
    assert F.refresh(job, "0" * 32, 300) is False, "a foreign fingerprint refreshed the fence"


def test_refresh_cannot_recreate_a_revoked_fence(job):
    F.install(job, _fp(job), 60)
    F.revoke(job, _fp(job))
    assert F.refresh(job, _fp(job), 60) is False
    assert not _client().exists(fence_key_for(job)), "a delayed renewal resurrected the fence"


def test_revoke_leaves_another_workers_fence_alone(job):
    F.install(job, _fp(job), 60)
    assert F.revoke(job, "0" * 32) is False
    assert F.current(job) == _fp(job), "a mismatched release deleted the current fence"


def test_the_installed_ttl_is_bounded(job):
    F.install(job, _fp(job), 30)
    assert 0 < _client().ttl(fence_key_for(job)) <= 30
