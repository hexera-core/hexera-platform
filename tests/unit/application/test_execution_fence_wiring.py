# Responsibility: Verify a write is authorized by the exact claim Redis still holds, not a stale check.
# Boundaries: the fence mechanism - the two-worker race and the full snapshot matrix are certification.
from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest

from meshpipeline.adapters.event_stream import fence as F
from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts.event_stream import expected_fence, expecting_fence
from meshpipeline.events.channels import fence_key_for


class _Own:
    def __init__(self, job_id="job-1", generation=2, token=None):
        self.job_id = job_id
        self.execution_generation = generation
        self.worker_token = token or uuid.UUID(int=7)


class _Inner:
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.seen: list = []

    def note(self, *a, **k):
        self.seen.append(("note", expected_fence()))

    def closing(self, *a, **k):
        self.seen.append(("closing", expected_fence()))


def _bind(monkeypatch, *, own, current=True):
    import meshpipeline.application.execution_publisher as mod

    async def is_current_owner():
        return current

    monkeypatch.setattr(mod._fence, "current_ownership", lambda: own)
    monkeypatch.setattr(mod._fence, "is_current_owner", is_current_owner)


# identity


def test_the_fingerprint_is_the_exact_claim_and_carries_no_token():
    own = _Own()
    got = F.fingerprint(own.job_id, own.execution_generation, own.worker_token)
    assert got == hashlib.sha256(
        f"{own.job_id}:{own.execution_generation}:{own.worker_token}".encode()).hexdigest()[:32]
    assert len(got) == 32
    assert str(own.worker_token) not in got, "the raw worker token reached the fingerprint"


@pytest.mark.parametrize("changed", ["job", "generation", "token"])
def test_a_different_claim_is_a_different_fingerprint(changed):
    base = F.fingerprint("job-1", 2, uuid.UUID(int=7))
    other = F.fingerprint("job-2" if changed == "job" else "job-1",
                          3 if changed == "generation" else 2,
                          uuid.UUID(int=8) if changed == "token" else uuid.UUID(int=7))
    assert base != other


def test_the_fence_key_is_per_job_and_namespaced():
    assert fence_key_for("j") == "jobs:j:eventfence"
    assert fence_key_for("a") != fence_key_for("b")


# the context-local expectation


def test_the_gate_sets_the_expectation_only_around_its_delegation(monkeypatch):
    inner = _Inner()
    own = _Own(job_id=inner.job_id)
    _bind(monkeypatch, own=own)
    assert expected_fence() == ""
    asyncio.run(OwnershipCheckedPublisher(inner).anote("hello"))
    assert expected_fence() == "", "the expectation leaked out of the delegation"
    assert inner.seen[0][1] == F.fingerprint(own.job_id, own.execution_generation,
                                             own.worker_token)


def test_the_expectation_resets_after_a_transport_failure(monkeypatch):
    class _Broken(_Inner):
        def note(self, *a, **k):
            raise RuntimeError("redis is down")

    _bind(monkeypatch, own=_Own())
    with pytest.raises(RuntimeError):
        asyncio.run(OwnershipCheckedPublisher(_Broken()).anote("hello"))
    assert expected_fence() == ""


def test_the_expectation_is_never_set_when_ownership_is_refused(monkeypatch):
    inner = _Inner()
    _bind(monkeypatch, own=None)
    with pytest.raises(StaleExecutionPublish):
        asyncio.run(OwnershipCheckedPublisher(inner).anote("hello"))
    assert expected_fence() == "" and inner.seen == []


def test_two_concurrent_tasks_cannot_see_each_others_expectation():
    seen: dict = {}

    async def one(name, value):
        with expecting_fence(value):
            await asyncio.sleep(0)
            seen[name] = expected_fence()

    async def both():
        await asyncio.gather(one("a", "aaaa"), one("b", "bbbb"))

    asyncio.run(both())
    assert seen == {"a": "aaaa", "b": "bbbb"}


def test_a_nested_expectation_restores_the_previous_one():
    with expecting_fence("outer"):
        with expecting_fence("inner"):
            assert expected_fence() == "inner"
        assert expected_fence() == "outer"
    assert expected_fence() == ""


def test_plain_publication_sees_an_empty_expectation():
    inner = _Inner()
    inner.closing("the run ended")
    assert inner.seen == [("closing", "")], (
        "a terminal publication carried a fence expectation; it publishes after the claim is "
        "gone and would be refused")


# the Lua boundary


def test_the_emit_script_checks_the_fence_before_touching_anything():
    from meshpipeline.adapters.event_stream.redis import _EMIT_LUA

    body = _EMIT_LUA.strip().splitlines()
    guard = "\n".join(body[:3])
    assert "KEYS[5]" in guard and "ARGV[5]" in guard and "-2" in guard, guard
    for mutator in ("SADD", "INCR", "RPUSH", "EXPIRE", "PUBLISH"):
        assert mutator not in guard, f"{mutator} runs before the fence is checked"
    assert "KEYS[5]" not in "\n".join(body[3:]), "publication modifies the fence key"


def test_the_emitter_passes_five_keys_and_the_expectation():
    import inspect

    from meshpipeline.adapters.event_stream import redis as R

    src = inspect.getsource(R.JobPublisher._emit_once)
    assert "_EMIT_LUA, 5" in src, "the emitter still invokes the script with four keys"
    assert "fence_key_for(self.job_id)" in src and "expected_fence()" in src


def test_a_refused_write_raises_stale_rather_than_looking_like_transport():
    import inspect

    from meshpipeline.adapters.event_stream import redis as R

    src = inspect.getsource(R.JobPublisher._emit_once)
    assert "seq == -2" in src and "StaleExecutionPublish" in src
    assert src.index("seq == -2") < src.index("seq == -1"), (
        "a stale replay must be refused before replay handling can absorb it")


def test_the_guarded_emitter_propagates_a_refusal_instead_of_warning(monkeypatch):
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    pub = JobPublisher.__new__(JobPublisher)
    pub.job_id, pub._redis = "job-1", object()

    def _boom(event, op_key=""):
        raise StaleExecutionPublish("the claim is gone")

    monkeypatch.setattr(pub, "_emit_once", _boom)
    with pytest.raises(StaleExecutionPublish):
        pub._emit_guarded(object(), op_key="k")


def test_the_best_effort_emitter_propagates_a_refusal_too(monkeypatch):
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    pub = JobPublisher.__new__(JobPublisher)
    pub.job_id, pub._redis, pub._last_warn_ts = "job-1", object(), 0.0

    def _boom(event, op_key=""):
        raise StaleExecutionPublish("the claim is gone")

    monkeypatch.setattr(pub, "_emit_once", _boom)
    with pytest.raises(StaleExecutionPublish):
        pub.emit(object())


# claim lifecycle ordering


def test_a_takeover_revokes_the_outgoing_fence_while_the_row_is_locked():
    import inspect
    import re

    from meshpipeline.persistence.lease import LeaseRepository

    lines = inspect.getsource(LeaseRepository.claim_execution).splitlines()
    revoked = next(i for i, ln in enumerate(lines) if ".revoke(" in ln)
    # the FIRST write that exposes the new owner, wherever it is
    rotated = next(i for i, ln in enumerate(lines)
                   if re.search(r"row\.active_worker_token\s*=(?!=)", ln))
    assert revoked < rotated, (
        f"the outgoing fence is revoked at line {revoked} but the new owner is installed at "
        f"{rotated}: a superseded worker's mirror can still authorize a write")
    assert "FenceUnavailable" in "\n".join(lines), "a failed revocation does not fail closed"


def test_renewal_extends_postgresql_before_it_refreshes_the_mirror():
    import inspect

    from meshpipeline.persistence.lease import LeaseRepository

    src = inspect.getsource(LeaseRepository.heartbeat)
    assert src.index("row.lease_expires_at = now") < src.index("refresh"), (
        "the mirror is refreshed before the authoritative lease is extended")
    # `install` is an unconditional SET and would overwrite a newer generation's fence, handing a
    # superseded worker the authority to publish over the current one. Renewal may restore a
    # mirror that has LAPSED - see heal() - but it may never displace one that is present.
    assert "install" not in src, "renewal may not install a fence - it would clobber a live one"


def test_renewal_can_restore_a_lapsed_mirror_but_only_by_filling_a_hole():
    """The invariant is 'never displace', not 'never write'.

    Asserting the absence of one function name was a proxy for that, and a weak one: it passes on
    a rename. What actually matters is that the only write renewal can make is one that loses
    every race for an occupied key. Without SOME restore, a single lapse is permanent - refresh
    no-ops forever, PostgreSQL reports perfect health, and the next fenced publish is refused for
    a supersession that never happened.
    """
    import inspect

    from meshpipeline.persistence.lease import LeaseRepository

    src = inspect.getsource(LeaseRepository.heartbeat)
    assert "heal" in src, "a lapsed mirror can never come back: refresh alone cannot restore it"
    assert src.index("refresh") < src.index("heal"), (
        "renewal must try to extend its own fence before restoring a missing one")

    heal_src = inspect.getsource(F.heal)
    assert "nx=True" in heal_src, (
        "heal must be NX - anything else can displace the fence of a newer generation")
    assert "GET" not in heal_src, "heal must be one atomic write, not a check-then-set"


def test_refresh_never_creates_a_missing_fence():
    assert "'SET'" not in F._REFRESH_LUA and "SET " not in F._REFRESH_LUA
    assert F._REFRESH_LUA.index("GET") < F._REFRESH_LUA.index("EXPIRE")


def test_revoke_only_deletes_a_matching_fence():
    assert F._REVOKE_LUA.index("GET") < F._REVOKE_LUA.index("DEL")


def test_release_revokes_before_it_clears_ownership():
    import inspect

    from meshpipeline.persistence.lease import LeaseRepository

    src = inspect.getsource(LeaseRepository.release)
    assert src.index("revoke") < src.index("row.active_worker_token = None"), (
        "ownership is relinquished before the mirror is revoked, leaving a window in which a "
        "pre-checked worker can still publish")


def test_the_ttl_cannot_outlive_the_authoritative_lease():
    import datetime as dt

    from meshpipeline.persistence.lease import LeaseRepository

    now = dt.datetime.now(dt.UTC)

    class _Row:
        lease_expires_at = now + dt.timedelta(seconds=30)

    assert LeaseRepository.fence_ttl_seconds(_Row(), now) <= 30
    assert LeaseRepository.fence_ttl_seconds(_Row(), now + dt.timedelta(seconds=10)) <= 20

    class _Expired:
        lease_expires_at = now - dt.timedelta(seconds=5)

    assert LeaseRepository.fence_ttl_seconds(_Expired(), now) == 1


# the corrected seam


def test_the_installer_is_a_module_level_application_seam():
    from meshpipeline.application import execution_fence as EF

    assert callable(EF.install_execution_fence)
    import inspect
    src = inspect.getsource(EF.install_execution_fence)
    assert "install_fence_value" in src, (
        "the installer must reach the adapter through the claim authority - `application`\n"
        "may not import a concrete adapter")


def test_the_repository_carries_no_redis_installation_method():
    from meshpipeline.persistence.lease import LeaseRepository

    assert not hasattr(LeaseRepository, "install_fence"), (
        "fence installation drifted back onto the substitutable repository; a suite supplying "
        "its own repository double would then have to implement Redis orchestration")
    assert hasattr(LeaseRepository, "fence_ttl_seconds"), (
        "the TTL is pure row-derived logic and belongs on the repository")


def test_the_claim_installs_after_commit_and_before_ownership_is_announced():
    import inspect

    from meshpipeline.application import execution_fence as EF

    src = inspect.getsource(EF.claim_delivery)
    assert "install_execution_fence" in src, "the claim never installs a fence"
    assert src.index("await db.commit()") < src.index("install_execution_fence"), (
        "the fence is installed before the claim is committed")
    assert "fence_unavailable" in src, "a failed installation still delivers execution"
    assert src.index("install_execution_fence") < src.index('jlog.info("Claimed execution'), (
        "ownership is announced before the fence exists")


def test_the_ttl_handed_to_the_seam_comes_from_the_claim_unchanged():
    import inspect

    from meshpipeline.application import execution_fence as EF

    src = inspect.getsource(EF.claim_delivery)
    assert "ownership.fence_ttl_seconds" in src, (
        "the installer re-derives the TTL instead of using the one the claim recorded")
