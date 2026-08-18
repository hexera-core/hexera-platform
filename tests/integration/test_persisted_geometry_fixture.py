# Responsibility: Verify the persisted geometry fixture records a chosen unit and real bytes via the real repository.
from __future__ import annotations

import os
import uuid

import pytest
from tests._geometry_support import PersistedGeometry, persisted_geometry

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _composed(store):
    return store


# the caller must state meaning

async def test_a_unit_and_basis_must_be_chosen_explicitly(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    # matched on the argument name, so this cannot pass because of some unrelated TypeError
    with pytest.raises(TypeError, match="unit"):
        await persisted_geometry(db, tmp_path=tmp_path)               # neither stated
    with pytest.raises(TypeError, match="unit"):
        await persisted_geometry(db, tmp_path=tmp_path,               # basis alone
                                 basis=ResolutionBasis.user_confirmed)
    with pytest.raises(TypeError, match="basis"):
        await persisted_geometry(db, tmp_path=tmp_path,               # unit alone
                                 unit=LengthUnit.millimetre)


async def test_the_recorded_unit_is_the_one_asked_for(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    for unit in (LengthUnit.metre, LengthUnit.millimetre, LengthUnit.centimetre, LengthUnit.inch):
        pg = await persisted_geometry(db, tmp_path=tmp_path / unit.value, unit=unit,
                                      basis=ResolutionBasis.user_confirmed)
        assert pg.interpretation.unit is unit


async def test_scale_is_derived_from_the_unit_not_supplied(db, tmp_path):
    from meshpipeline.contracts.geometry_units import (
        SCALE_TO_METRES,
        LengthUnit,
        ResolutionBasis,
    )

    pg = await persisted_geometry(db, tmp_path=tmp_path, unit=LengthUnit.inch,
                                  basis=ResolutionBasis.file_declared)
    assert pg.interpretation.scale_to_metres == SCALE_TO_METRES[LengthUnit.inch] == 0.0254


# it agrees with the real system

async def test_the_source_is_readable_through_the_real_tenant_scoped_repository(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )

    pg = await persisted_geometry(db, tmp_path=tmp_path, owner_id="tenant-a",
                                  unit=LengthUnit.millimetre,
                                  basis=ResolutionBasis.file_declared)

    row = await GeometrySourceRepository().get_for_owner(
        db, uuid.UUID(pg.source_id), "tenant-a")
    assert row is not None
    assert row.object_key == pg.ref.object_key
    assert row.sha256 == pg.ref.sha256
    assert row.size_bytes == pg.ref.size_bytes


async def test_another_tenant_cannot_see_it(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )

    pg = await persisted_geometry(db, tmp_path=tmp_path, owner_id="tenant-a",
                                  unit=LengthUnit.millimetre,
                                  basis=ResolutionBasis.file_declared)

    assert await GeometrySourceRepository().get_for_owner(
        db, uuid.UUID(pg.source_id), "tenant-b") is None
    assert await GeometryInterpretationRepository().get_for_owner(
        db, uuid.UUID(pg.interpretation_id), "tenant-b") is None


async def test_the_object_store_really_holds_the_bytes(db, store, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    pg = await persisted_geometry(db, tmp_path=tmp_path, unit=LengthUnit.metre,
                                  basis=ResolutionBasis.user_confirmed)

    assert store.exists(object_key=pg.ref.object_key)
    assert store.get_bytes(object_key=pg.ref.object_key) == pg.payload


async def test_reference_digest_and_size_describe_the_real_bytes(db, tmp_path):
    import hashlib

    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    pg = await persisted_geometry(db, tmp_path=tmp_path, unit=LengthUnit.metre,
                                  basis=ResolutionBasis.user_confirmed)

    assert pg.ref.sha256 == hashlib.sha256(pg.payload).hexdigest()
    assert pg.ref.size_bytes == len(pg.payload)
    assert pg.local_path.read_bytes() == pg.payload


async def test_the_interpretation_is_bound_to_that_source_and_owner(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )

    pg = await persisted_geometry(db, tmp_path=tmp_path, owner_id="tenant-a",
                                  unit=LengthUnit.centimetre,
                                  basis=ResolutionBasis.user_confirmed)

    found = await GeometryInterpretationRepository().get_for_owner(
        db, uuid.UUID(pg.interpretation_id), "tenant-a")
    assert found is not None
    assert found.geometry_source_id == pg.source_id
    assert found.owner_id == "tenant-a"
    assert found.unit is LengthUnit.centimetre
    assert found.basis is ResolutionBasis.user_confirmed


async def test_re_recording_the_same_reading_is_the_same_row(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )

    pg = await persisted_geometry(db, tmp_path=tmp_path, unit=LengthUnit.millimetre,
                                  basis=ResolutionBasis.file_declared)

    again = await GeometryInterpretationRepository().record(
        db, owner_id=pg.owner_id, geometry_source_id=uuid.UUID(pg.source_id),
        unit=LengthUnit.millimetre, basis=ResolutionBasis.file_declared)
    assert again.interpretation_id == pg.interpretation_id

    # a DIFFERENT unit is a different interpretation, never an edit of this one
    other = await GeometryInterpretationRepository().record(
        db, owner_id=pg.owner_id, geometry_source_id=uuid.UUID(pg.source_id),
        unit=LengthUnit.inch, basis=ResolutionBasis.user_confirmed)
    assert other.interpretation_id != pg.interpretation_id
    assert (await GeometryInterpretationRepository().get_for_owner(
        db, uuid.UUID(pg.interpretation_id), pg.owner_id)).unit is LengthUnit.millimetre


async def test_state_round_trips_through_the_production_serializer(db, tmp_path):
    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    pg = await persisted_geometry(db, tmp_path=tmp_path, unit=LengthUnit.millimetre,
                                  basis=ResolutionBasis.file_declared)

    state = pg.state()
    assert state["ref"]["source_id"] == pg.source_id
    assert state["local_path"] == str(pg.local_path)

    back = MaterializedGeometry.from_state(state)
    assert back is not None
    assert back.ref.source_id == pg.source_id
    assert back.ref.sha256 == pg.ref.sha256


async def test_it_is_a_named_type_not_a_tuple(db, tmp_path):
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    pg = await persisted_geometry(db, tmp_path=tmp_path, unit=LengthUnit.metre,
                                  basis=ResolutionBasis.user_confirmed)

    assert isinstance(pg, PersistedGeometry)
    assert not isinstance(pg, tuple)
    with pytest.raises(TypeError):
        _a, _b = pg                                   # not unpackable, deliberately
