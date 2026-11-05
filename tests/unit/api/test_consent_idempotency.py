# Responsibility: Verify confirming an approval whose job already exists is idempotent.
import uuid
from types import SimpleNamespace

import pytest

from meshpipeline.api.v1 import chat  # noqa: E402

# The approved upload these runs carry.
_GEOMETRY_SOURCE = {'source_id': '33333333-3333-4333-8333-333333333333', 'owner_id': 'owner-1', 'object_key': 'sources/33333333-3333-4333-8333-333333333333', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'input.step', 'suffix_hint': '.step'}



@pytest.mark.asyncio
async def test_confirm_pending_approval_is_idempotent_when_job_linked(monkeypatch):
    existing_job = uuid.uuid4()
    session = SimpleNamespace(
        job_id=existing_job, geometry_source=_GEOMETRY_SOURCE, request_txt="req",
        intake_gate=None,
    )
    # If the guard fails, this would be called - make it explode to prove it isn't.
    def _boom(*a, **k):
        raise AssertionError("run_simulation must NOT be dispatched for an already-linked session")
    monkeypatch.setattr("meshpipeline.adapters.pipeline_execution.celery.run_simulation", SimpleNamespace(apply_async=_boom), raising=False)

    resp = await chat._confirm_pending_approval(session, session_repo=None, owner_id="alice",
                                                session_id=uuid.uuid4())
    assert resp.done is True
    assert resp.job_id == existing_job
