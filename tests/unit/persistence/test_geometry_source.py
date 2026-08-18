# Responsibility: Verify a geometry source is identified by content and ownership, never by a path.
from __future__ import annotations

import hashlib
import uuid

import pytest

from meshpipeline.persistence.models import GeometrySource
from meshpipeline.persistence.repositories.geometry_source_repository import (
    GeometrySourceRepository,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Session:

    def __init__(self):
        self.added: list = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


# the row's own shape

def test_a_source_carries_content_identity_not_a_path():
    declared = {c.name for c in GeometrySource.__table__.columns}
    lineage = {"id", "owner_id", "original_filename", "suffix_hint",
               "object_key", "sha256", "size_bytes", "created_at"}
    # Retention state, which says whether the BYTES still exist. The row itself is durable
    # lineage and is never deleted to satisfy a retention window, so this is state about the
    # object, not a second identity for it.
    retention = {"purged_at", "purge_claim_id", "purge_claimed_at"}
    assert lineage | retention == declared
    # the point of the entity: no path column may reappear as a second source of truth
    assert not [c for c in declared if "path" in c]


def test_lineage_columns_survive_a_purge_by_construction():
    nullable = {c.name for c in GeometrySource.__table__.columns if c.nullable}
    for col in ("sha256", "size_bytes", "original_filename", "object_key", "owner_id"):
        assert col not in nullable, f"{col} is lineage and must always be present"
    for col in ("purged_at", "purge_claim_id", "purge_claimed_at"):
        assert col in nullable, f"{col} is retention state and must start empty"


def test_the_object_key_is_unique_so_a_source_cannot_be_repointed():
    uniques = {tuple(sorted(c.columns.keys()))
               for c in GeometrySource.__table__.constraints
               if c.__class__.__name__ == "UniqueConstraint"}
    assert ("object_key",) in uniques


def test_the_checksum_is_indexed_for_identity_lookups():
    indexed = {tuple(c.name for c in ix.columns) for ix in GeometrySource.__table__.indexes}
    assert ("sha256",) in indexed
    assert ("owner_id", "created_at") in indexed


# what the repository refuses

@pytest.mark.asyncio
async def test_a_wellformed_source_is_created_with_a_normalized_digest():
    payload = b"solid demo\nendsolid demo\n"
    repo, db = GeometrySourceRepository(), _Session()
    row = await repo.create(
        db, owner_id="owner-a", original_filename="Part One.STEP", suffix_hint=".STEP",
        object_key=f"sources/{uuid.uuid4()}.step", sha256=_digest(payload).upper(),
        size_bytes=len(payload))
    assert row.sha256 == _digest(payload)          # upper-case in, canonical lower-case stored
    assert row.suffix_hint == ".step"              # hint is normalised, still untrusted
    assert row.original_filename == "Part One.STEP"
    assert row.size_bytes == len(payload)
    assert db.added == [row]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "",                       # absent
    "abc",                    # too short
    "g" * 64,                 # not hex
    _digest(b"x")[:-1],       # truncated by one
])
async def test_a_digest_that_cannot_prove_anything_is_refused(bad):
    repo, db = GeometrySourceRepository(), _Session()
    with pytest.raises(ValueError, match="sha256"):
        await repo.create(db, owner_id="o", original_filename="f.step", suffix_hint=".step",
                          object_key="sources/k", sha256=bad, size_bytes=10)
    assert db.added == []


@pytest.mark.asyncio
async def test_an_empty_upload_is_not_a_source():
    repo, db = GeometrySourceRepository(), _Session()
    with pytest.raises(ValueError, match="size_bytes"):
        await repo.create(db, owner_id="o", original_filename="f.step", suffix_hint=".step",
                          object_key="sources/k", sha256=_digest(b""), size_bytes=0)
    assert db.added == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,match", [
    ("owner_id", "  ", "owner_id"),
    ("object_key", "", "object_key"),
])
async def test_a_source_is_always_owned_and_always_locatable(field, value, match):
    kwargs = {"owner_id": "o", "original_filename": "f.step", "suffix_hint": ".step",
              "object_key": "sources/k", "sha256": _digest(b"x"), "size_bytes": 1}
    kwargs[field] = value
    repo, db = GeometrySourceRepository(), _Session()
    with pytest.raises(ValueError, match=match):
        await repo.create(db, **kwargs)
    assert db.added == []
