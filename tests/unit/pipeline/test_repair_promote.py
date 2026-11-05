# Responsibility: Verify repaired geometry becomes the run's geometry only after it provably stages, and never without recording what it replaced.
from __future__ import annotations

import uuid

import pytest
from tests._geometry_support import geometry_state, source_ref

from meshpipeline.contracts.geometry_source import (
    GeometryInterpretationRef,
    MaterializedGeometry,
)
from meshpipeline.pipeline.repair_promote import (
    PromotionRefused,
    derived_interpretation,
    promote,
)


def _original(tmp_path):
    from meshpipeline.pipeline.geometry_state import materialized
    state = {"job_id": "job-1", "engine": "snappy", "geometry": geometry_state(tmp_path)}
    return state, materialized(state)


def _repaired(tmp_path, original, *, marker="repaired", unit=None, scale=None, filename="fixed.step"):
    """A handle to repaired bytes, stored as their own source - which is what promotion requires.

    A repair produces different bytes, so it is a different source: the digest is the identity,
    and minting a handle whose digest did not match its object would make every later provenance
    claim a lie.
    """
    ref = source_ref(tmp_path=tmp_path, filename=filename, marker=marker,
                     owner_id=original.ref.owner_id)
    interpretation = derived_interpretation(original, source_id=ref.source_id)
    if unit is not None:
        interpretation = GeometryInterpretationRef(
            interpretation_id=interpretation.interpretation_id,
            geometry_source_id=ref.source_id, unit=unit, scale_to_metres=scale,
            basis=interpretation.basis, evidence=interpretation.evidence)
    return MaterializedGeometry(ref=ref, interpretation=interpretation,
                                local_path=tmp_path / filename)


@pytest.fixture
def staging_ok(monkeypatch):
    seen: list = []

    def _prepare(geometry, destination, *, engine=""):
        seen.append((geometry.ref.sha256, engine))
        from pathlib import Path

        from tests._geometry_support import prepared_surface
        Path(destination).write_text("")
        return prepared_surface(destination)

    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _prepare)
    return seen


@pytest.fixture
def staging_fails(monkeypatch):
    def _prepare(geometry, destination, *, engine=""):
        raise RuntimeError("OpenCASCADE produced an empty shape")

    monkeypatch.setattr("meshpipeline.cad.staging.prepare_surface", _prepare)


# THE PROMOTION


def test_repaired_geometry_that_stages_becomes_the_geometry_the_run_meshes(tmp_path, staging_ok):
    state, original = _original(tmp_path)
    repaired = _repaired(tmp_path, original)

    update = promote(state, repaired=repaired, engine="snappy", attempt=2)

    # ONE HANDLE, and it is the approved one. No consumer has to choose between two candidates.
    assert update["geometry"]["ref"]["sha256"] == repaired.ref.sha256
    assert "effective_geometry" not in update
    # and it was proved against the engine that has to mesh it
    assert staging_ok == [(repaired.ref.sha256, "snappy")]


def test_what_it_replaced_is_recorded_beside_it(tmp_path, staging_ok):
    state, original = _original(tmp_path)
    repaired = _repaired(tmp_path, original)

    lineage = promote(state, repaired=repaired, engine="snappy", attempt=3)["repair_lineage"]

    # the ORIGINAL stays identifiable: the upload is immutable and still in the catalogue, so the
    # customer's bytes remain retrievable and provable after the run has moved on
    assert lineage["original"]["sha256"] == original.ref.sha256
    assert lineage["original"]["source_id"] == original.ref.source_id
    assert lineage["repaired"]["sha256"] == repaired.ref.sha256
    assert lineage["engine_staged_for"] == "snappy"
    assert lineage["attempt"] == 3


def test_repaired_geometry_that_does_not_stage_is_refused(tmp_path, staging_fails):
    state, original = _original(tmp_path)
    repaired = _repaired(tmp_path, original)

    with pytest.raises(PromotionRefused, match="does not stage"):
        promote(state, repaired=repaired, engine="snappy")

    # THE RUN STAYS ON THE ORIGINAL. Promoting geometry the mesher cannot stage would hide a
    # failed repair behind a mesh attempt that was always going to fail the same way.
    assert state["geometry"]["ref"]["sha256"] == original.ref.sha256


def test_the_refusal_names_the_engine_and_the_kernels_own_words(tmp_path, staging_fails):
    state, original = _original(tmp_path)
    with pytest.raises(PromotionRefused) as refused:
        promote(state, repaired=_repaired(tmp_path, original), engine="vmtk")
    assert "vmtk" in str(refused.value)
    assert "empty shape" in str(refused.value)


def test_a_repair_that_changed_nothing_is_not_promoted(tmp_path, staging_ok):
    state, original = _original(tmp_path)
    # byte-identical: same marker, so the same digest
    same = MaterializedGeometry(
        ref=original.ref,
        interpretation=original.interpretation,
        local_path=original.local_path)

    with pytest.raises(PromotionRefused, match="byte-identical"):
        promote(state, repaired=same, engine="snappy")


def test_a_repair_may_not_reinterpret_the_unit(tmp_path, staging_ok):
    state, original = _original(tmp_path)
    # the same part, now claiming to be metres: a factor of a thousand, silently
    rescaled = _repaired(tmp_path, original, unit="m", scale=1.0)

    with pytest.raises(PromotionRefused, match="does not reinterpret scale"):
        promote(state, repaired=rescaled, engine="snappy")


def test_the_unit_is_carried_across_rather_than_resolved_again(tmp_path):
    _, original = _original(tmp_path)
    new_source = str(uuid.uuid4())

    derived = derived_interpretation(original, source_id=new_source)

    # same meaning, new owner-of-record: MaterializedGeometry refuses an interpretation that
    # names another upload, and repair must not decide a millimetre part was metres all along
    assert derived.unit == original.interpretation.unit
    assert derived.scale_to_metres == original.interpretation.scale_to_metres
    assert derived.basis == original.interpretation.basis
    assert derived.interpretation_id == original.interpretation.interpretation_id
    assert derived.geometry_source_id == new_source


def test_a_run_carrying_no_geometry_has_nothing_to_replace(tmp_path, staging_ok):
    _, original = _original(tmp_path)
    with pytest.raises(PromotionRefused, match="nothing for a repair to replace"):
        promote({"job_id": "job-1"}, repaired=_repaired(tmp_path, original), engine="snappy")


def test_offering_no_repair_is_refused_rather_than_silently_ignored(tmp_path, staging_ok):
    state, _ = _original(tmp_path)
    with pytest.raises(PromotionRefused, match="no repaired geometry"):
        promote(state, repaired=None, engine="snappy")


def test_the_staging_proof_is_thrown_away(tmp_path, staging_ok):
    # it asks a question; the builder stages again for real from whatever this decided on
    state, original = _original(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}

    promote(state, repaired=_repaired(tmp_path, original), engine="snappy")

    leaked = {p.name for p in tmp_path.iterdir()} - before - {"fixed.step"}
    assert leaked == set()
