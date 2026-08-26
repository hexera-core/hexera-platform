# The publish seam cannot tell "the mirror lapsed while the claim held" from a real takeover
# on its own - the decorator gives every gated method ONE recovery (re-verify under the row
# lock, heal, retry once) and a second refusal fails closed exactly as before.
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meshpipeline.application.execution_fence as _fence
from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher
from meshpipeline.contracts.event_stream import StaleExecutionPublish


class _Inner:
    def __init__(self, job_id, fail_times=0):
        self.job_id = job_id
        self.calls = 0
        self._fail_times = fail_times

    def note(self, text, tone="info", op_id=""):  # noqa: ARG002
        self.calls += 1
        if self.calls <= self._fail_times:
            raise StaleExecutionPublish("fence gate refused (-2)")


def _own(job_id):
    return SimpleNamespace(job_id=job_id, execution_generation=1,
                           worker_token=uuid.uuid4())


@pytest.fixture
def job_id():
    return uuid.uuid4()


@pytest.fixture
def bound(monkeypatch, job_id):
    monkeypatch.setattr(_fence, "current_ownership", lambda: _own(job_id))

    async def _is_owner(**kwargs):  # noqa: ARG001
        return True

    monkeypatch.setattr(_fence, "is_current_owner", _is_owner)
    return job_id


async def test_a_lapsed_fence_heals_once_and_the_event_publishes(monkeypatch, bound):
    heal = AsyncMock()
    monkeypatch.setattr(_fence, "reverify_and_heal_fence", heal)
    inner = _Inner(bound, fail_times=1)
    await OwnershipCheckedPublisher(inner).anote("hello")
    assert inner.calls == 2                  # refused once, healed, published
    heal.assert_awaited_once()


async def test_a_second_refusal_fails_closed(monkeypatch, bound):
    heal = AsyncMock()
    monkeypatch.setattr(_fence, "reverify_and_heal_fence", heal)
    inner = _Inner(bound, fail_times=99)
    with pytest.raises(StaleExecutionPublish):
        await OwnershipCheckedPublisher(inner).anote("hello")
    assert inner.calls == 2                  # exactly one retry, never a loop
    heal.assert_awaited_once()


async def test_a_failed_recovery_propagates_the_refusal(monkeypatch, bound):
    heal = AsyncMock(side_effect=StaleExecutionPublish("claim no longer holds"))
    monkeypatch.setattr(_fence, "reverify_and_heal_fence", heal)
    inner = _Inner(bound, fail_times=1)
    with pytest.raises(StaleExecutionPublish):
        await OwnershipCheckedPublisher(inner).anote("hello")
    assert inner.calls == 1                  # no retry after a refused recovery


async def test_no_bound_ownership_is_refused_without_recovery(monkeypatch, job_id):
    monkeypatch.setattr(_fence, "current_ownership", lambda: None)
    heal = AsyncMock()
    monkeypatch.setattr(_fence, "reverify_and_heal_fence", heal)
    inner = _Inner(job_id)
    with pytest.raises(StaleExecutionPublish):
        await OwnershipCheckedPublisher(inner).anote("hello")
    heal.assert_not_awaited()
    assert inner.calls == 0


async def test_another_jobs_ownership_is_refused_without_recovery(monkeypatch, job_id):
    monkeypatch.setattr(_fence, "current_ownership", lambda: _own(uuid.uuid4()))
    heal = AsyncMock()
    monkeypatch.setattr(_fence, "reverify_and_heal_fence", heal)
    inner = _Inner(job_id)
    with pytest.raises(StaleExecutionPublish):
        await OwnershipCheckedPublisher(inner).anote("hello")
    heal.assert_not_awaited()
