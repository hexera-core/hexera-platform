# Responsibility: Read and write artifact rows, and decide what a repeated delivery means.
# Boundaries: currency is (generation, attempt): a superseded delivery is recorded, never allowed to overwrite.
from __future__ import annotations

import enum
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Artifact, ArtifactType


class DeliveryOutcome(str, enum.Enum):
    created = "created"          # this caller wrote the row (first delivery)
    updated = "updated"          # a newer attempt overwrote an older row
    idempotent = "idempotent"    # same attempt + same checksum already present - nothing to do
    superseded = "superseded"    # a NEWER attempt already owns this logical artifact - no-op
    conflict = "conflict"        # same attempt, DIFFERENT checksum - fail closed, do not overwrite


class ArtifactRepository:
    async def get_by_logical_key(self, db: AsyncSession, job_id: uuid.UUID,
                                 logical_key: str) -> Artifact | None:
        res = await db.execute(
            select(Artifact).where(Artifact.job_id == job_id,
                                   Artifact.logical_key == logical_key))
        return res.scalar_one_or_none()

    async def get_by_job(self, db: AsyncSession, job_id: uuid.UUID) -> list[Artifact]:
        result = await db.execute(select(Artifact).where(Artifact.job_id == job_id))
        return list(result.scalars().all())

    async def deliver_artifact(self, db: AsyncSession, *, job_id: uuid.UUID, logical_key: str,
                               artifact_type: ArtifactType, storage_key: str, size_bytes: int,
                               checksum: str | None, delivery_attempt: int,
                               execution_generation: int = 0) -> DeliveryOutcome:
        _cur_gen = Artifact.execution_generation
        _cur_att = Artifact.delivery_attempt
        stmt = (
            pg_insert(Artifact)
            .values(job_id=job_id, logical_key=logical_key, artifact_type=artifact_type,
                    storage_key=storage_key, size_bytes=size_bytes, checksum=checksum,
                    delivery_attempt=delivery_attempt, execution_generation=execution_generation)
            .on_conflict_do_update(
                constraint="uq_artifacts_job_logical",
                set_={"artifact_type": artifact_type, "storage_key": storage_key,
                      "size_bytes": size_bytes, "checksum": checksum,
                      "delivery_attempt": delivery_attempt,
                      "execution_generation": execution_generation},
                where=(
                    (_cur_gen < execution_generation)
                    | ((_cur_gen == execution_generation) & (_cur_att < delivery_attempt))
                    | ((_cur_gen == execution_generation) & (_cur_att == delivery_attempt)
                       & (Artifact.checksum.is_not_distinct_from(checksum)))
                ),
            )
            .returning(Artifact.id, (Artifact.storage_key == storage_key).label("same_key"))
        )
        res = (await db.execute(stmt)).first()
        if res is not None:
            # A row was written or updated by this statement (the CAS WHERE held). same_key True
            # means the stored key equals ours → treat as created/idempotent-owned; a differing key
            # would only happen on an update of an older row to our key.
            return DeliveryOutcome.created if res.same_key else DeliveryOutcome.updated

        # No row written: the CAS WHERE blocked it. Read the incumbent to classify precisely.
        incumbent = (await db.execute(
            select(Artifact).where(Artifact.job_id == job_id, Artifact.logical_key == logical_key)
        )).scalar_one_or_none()
        if incumbent is None:
            # A concurrent delete between the INSERT-conflict and this read: treat as a conflict
            # rather than claim a success we cannot prove.
            return DeliveryOutcome.conflict
        _mine = (execution_generation, delivery_attempt)
        _theirs = (int(incumbent.execution_generation or 0), incumbent.delivery_attempt)
        if _theirs > _mine:
            return DeliveryOutcome.superseded
        if (_theirs == _mine
                and incumbent.storage_key == storage_key and incumbent.checksum == checksum):
            return DeliveryOutcome.idempotent
        return DeliveryOutcome.conflict

    async def create(self, db: AsyncSession, job_id: uuid.UUID,
                     artifact_type: ArtifactType, storage_key: str,
                     size_bytes: int = 0, checksum: str | None = None,
                     logical_key: str | None = None, delivery_attempt: int = 0) -> Artifact:
        a = Artifact(job_id=job_id, artifact_type=artifact_type,
                     logical_key=logical_key or artifact_type.value,
                     delivery_attempt=delivery_attempt,
                     storage_key=storage_key, size_bytes=size_bytes, checksum=checksum)
        db.add(a)
        await db.flush()
        return a
