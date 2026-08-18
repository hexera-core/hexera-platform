# Responsibility: Verify an interpretation for different bytes or another tenant never reaches materialisation.
from __future__ import annotations

import uuid

import pytest
from tests._geometry_support import interpretation_ref, source_ref

from meshpipeline.application.geometry_materializer import materialize_for_job
from meshpipeline.contracts.geometry_source import GeometrySourceError

OWNER = "owner-1"


def _pair(*, same_source: bool = True):
    src = source_ref(owner_id=OWNER, filename="part.step",
                     source_id="22222222-2222-4222-8222-222222222222")
    other = "99999999-9999-4999-8999-999999999999"
    interp = interpretation_ref(
        geometry_source_id=src.source_id if same_source else other,
        interpretation_id="33333333-3333-4333-8333-333333333333")
    return src, interp


class _Repo:

    def __init__(self, interp):
        self._interp = interp

    async def get_for_owner(self, _db, interpretation_id, owner_id):
        from meshpipeline.contracts.geometry_units import (
            GeometryInterpretation,
            LengthUnit,
            ResolutionBasis,
        )
        if str(interpretation_id) != self._interp.interpretation_id or owner_id != OWNER:
            return None
        return GeometryInterpretation(
            interpretation_id=self._interp.interpretation_id, owner_id=OWNER,
            geometry_source_id=self._interp.geometry_source_id,
            unit=LengthUnit(self._interp.unit),
            scale_to_metres=float(self._interp.scale_to_metres),
            basis=ResolutionBasis(self._interp.basis), evidence=self._interp.evidence)


@pytest.fixture()
def _resolved(monkeypatch):
    async def _row_ref(_db, ref):
        return ref
    monkeypatch.setattr("meshpipeline.application.geometry_materializer.resolve_row_ref", _row_ref)
    return monkeypatch


async def test_a_matched_pair_reaches_materialisation(_resolved, tmp_path):
    src, interp = _pair(same_source=True)
    _resolved.setattr(
        "meshpipeline.persistence.repositories.geometry_interpretation_repository"
        ".GeometryInterpretationRepository", lambda: _Repo(interp))
    seen: dict = {}

    def _materialize(ref, verified, **kw):
        seen["source_id"] = ref.source_id
        seen["interpretation_source_id"] = verified.geometry_source_id
        return "materialized"

    _resolved.setattr("meshpipeline.application.geometry_materializer.materialize", _materialize)
    out = await materialize_for_job(None, src, interp, workspace=tmp_path, job_id="j")
    assert out == "materialized"
    assert seen["source_id"] == seen["interpretation_source_id"]


async def test_an_interpretation_for_DIFFERENT_bytes_is_refused(_resolved, tmp_path):
    src, interp = _pair(same_source=False)
    _resolved.setattr(
        "meshpipeline.persistence.repositories.geometry_interpretation_repository"
        ".GeometryInterpretationRepository", lambda: _Repo(interp))

    def _must_not_run(*a, **k):
        raise AssertionError("materialisation ran on a mismatched pair")

    _resolved.setattr("meshpipeline.application.geometry_materializer.materialize", _must_not_run)
    with pytest.raises(GeometrySourceError, match="belongs to a different geometry source"):
        await materialize_for_job(None, src, interp, workspace=tmp_path, job_id="j")


async def test_the_mismatch_is_classified_as_data_integrity(_resolved, tmp_path):
    from meshpipeline.errors import FailureClass
    src, interp = _pair(same_source=False)
    _resolved.setattr(
        "meshpipeline.persistence.repositories.geometry_interpretation_repository"
        ".GeometryInterpretationRepository", lambda: _Repo(interp))
    def _must_not_run(*a, **k):
        raise AssertionError("materialisation ran on a mismatched pair")

    _resolved.setattr("meshpipeline.application.geometry_materializer.materialize", _must_not_run)
    with pytest.raises(GeometrySourceError) as exc:
        await materialize_for_job(None, src, interp, workspace=tmp_path, job_id="j")
    # both the identity AND the classification, so an unrelated raise cannot satisfy this
    assert "belongs to a different geometry source" in str(exc.value)
    assert exc.value.failure_class is FailureClass.DATA_INTEGRITY


async def test_a_foreign_tenants_interpretation_is_absent_not_applied(_resolved, tmp_path):
    src, _ = _pair()
    foreign = interpretation_ref(geometry_source_id=src.source_id,
                                 interpretation_id=str(uuid.uuid4()))
    _resolved.setattr(
        "meshpipeline.persistence.repositories.geometry_interpretation_repository"
        ".GeometryInterpretationRepository",
        lambda: _Repo(interpretation_ref(geometry_source_id=src.source_id,
                                         interpretation_id=str(uuid.uuid4()))))
    with pytest.raises(GeometrySourceError, match="no longer available"):
        await materialize_for_job(None, src, foreign, workspace=tmp_path, job_id="j")


async def test_the_session_resolver_refuses_a_foreign_interpretation():
    from types import SimpleNamespace

    from meshpipeline.application.geometry_materializer import interpretation_ref_for_session
    from meshpipeline.errors import FailureClass

    class _Empty:
        async def get_for_owner(self, _db, _iid, _owner):
            return None                      # tenant-scoped query finds no row

    import meshpipeline.persistence.repositories.geometry_interpretation_repository as mod
    prior = mod.GeometryInterpretationRepository
    mod.GeometryInterpretationRepository = _Empty
    try:
        session = SimpleNamespace(geometry_interpretation_id=uuid.uuid4())
        with pytest.raises(GeometrySourceError) as exc:
            await interpretation_ref_for_session(None, session, "someone-else")
        assert exc.value.failure_class is FailureClass.NOT_AUTHORIZED
    finally:
        mod.GeometryInterpretationRepository = prior


async def test_a_session_without_an_interpretation_resolves_to_none_not_a_default():
    from types import SimpleNamespace

    from meshpipeline.application.geometry_materializer import interpretation_ref_for_session
    session = SimpleNamespace(geometry_interpretation_id=None)
    assert await interpretation_ref_for_session(None, session, OWNER) is None
