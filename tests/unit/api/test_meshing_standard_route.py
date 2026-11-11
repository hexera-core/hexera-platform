# tests/unit/api/test_meshing_standard_route.py
# Responsibility: Verify the organisation's meshing standard routes (GET / PUT /organization/meshing-standard): a caller reads and writes ONLY their own organisation's standard (another organisation's words are never read, shown or changed), the words are read once on save and the reading reused while they stay the same, a model that is down still saves the words, empty words clear the standard, a key cannot change it, and an approved run is dispatched with the standard as saved.
# Boundaries: the routes and the service over in-memory repositories and a fake reader. No database, no model.
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.api.v1 import organization
from meshpipeline.application import meshing_standard_service as MSS
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.engines import meshing_standard as MS

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
RIVAL = "rival@elsewhere.test"
OWN_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
RIVAL_WORDS = "Rival Dynamics standard: Global Mesh Size = 2 mm"


@dataclass
class _Row:
    organization_id: uuid.UUID
    text: str
    reading: dict | None
    updated_by: str
    updated_at: datetime


class _Store:
    """organization_meshing_standards in memory, with ANOTHER organisation's row really in it: a refusal must be
    the code's, not an empty fake's."""

    def __init__(self):
        self.rows = {OTHER_ORG: _Row(OTHER_ORG, RIVAL_WORDS, None, RIVAL, NOW)}
        self.commits = 0

    async def get_for_organization(self, db, organization_id):
        return self.rows.get(organization_id)

    async def put(self, db, *, organization_id, text, reading, updated_by):
        self.rows[organization_id] = _Row(organization_id, text, reading, updated_by, NOW)
        return self.rows[organization_id]

    async def clear(self, db, *, organization_id):
        self.rows.pop(organization_id, None)


class _Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.store.commits += 1


class _Reader:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    async def __call__(self, text, *, user_id=""):
        self.calls.append((text, user_id))
        if self.answer is None:
            return None                                  # the model was down: saved unread
        return {"key": MS.key_of(text), "arguments": json.dumps(self.answer), "read_at": NOW.isoformat()}


@pytest.fixture
def store(monkeypatch):
    s = _Store()

    async def org_for_email(db, email):
        return {OWNER: OWN_ORG, RIVAL: OTHER_ORG}.get(email)

    monkeypatch.setattr(MSS.membership_repo, "organization_id_for_email", org_for_email)
    for name in ("get_for_organization", "put", "clear"):
        monkeypatch.setattr(MSS.standard_repo, name, getattr(s, name))
    monkeypatch.setattr(organization, "get_db", lambda: _Session(s))
    return s


@pytest.fixture
def reader(monkeypatch):
    r = _Reader(criteria_answer())
    monkeypatch.setattr(MS, "read_at_save", r)
    return r


def _console(org=OWN_ORG) -> Principal:
    return Principal(owner_id=OWNER, organization_id=str(org), credential=Credential.signed_header)


async def _put(text: str, principal: Principal | None = None):
    return await organization.save_meshing_standard(organization.MeshingStandardIn(text=text),
                                                    principal or _console(), owner_id=OWNER)


async def test_an_organisation_with_no_standard_reads_empty(store):
    got = await organization.read_meshing_standard(owner_id=OWNER, organization_id=str(OWN_ORG))
    assert got == {"text": "", "lines": [], "status": "empty", "updated_by": None, "updated_at": None}


async def test_saving_reads_the_words_once_and_shows_what_was_understood(store, reader):
    got = await _put(CRITERIA)
    assert reader.calls == [(CRITERIA, OWNER)]
    assert got["status"] == "read" and got["text"] == CRITERIA and got["updated_by"] == OWNER
    assert got["lines"][0] == "Global element size 10 mm." and len(got["lines"]) == 10
    assert store.rows[OWN_ORG].reading["key"] == MS.key_of(CRITERIA)
    # read back: the same view, from the reading kept with the words
    again = await organization.read_meshing_standard(owner_id=OWNER, organization_id=str(OWN_ORG))
    assert again == got
    # the same words saved again are not read again; changed words are
    await _put(CRITERIA)
    assert len(reader.calls) == 1
    await _put(CRITERIA + "\nGrowth rate 1.2")
    assert len(reader.calls) == 2


async def test_a_model_that_is_down_still_saves_the_words_unread(store, monkeypatch):
    monkeypatch.setattr(MS, "read_at_save", _Reader(None))
    got = await _put(CRITERIA)
    assert got["status"] == "unread" and got["lines"] == [] and store.rows[OWN_ORG].text == CRITERIA
    assert store.rows[OWN_ORG].reading is None


async def test_empty_words_clear_the_standard(store, reader):
    await _put(CRITERIA)
    got = await _put("   \n ")
    assert got["status"] == "empty" and OWN_ORG not in store.rows and len(reader.calls) == 1


async def test_another_organisations_standard_is_never_read_or_changed(store, reader):
    # the caller names an organisation they do not belong to: refused the same as no organisation at all
    for org in (str(OTHER_ORG), "", "not-a-uuid"):
        with pytest.raises(HTTPException) as e:
            await organization.read_meshing_standard(owner_id=OWNER, organization_id=org)
        assert e.value.status_code == 404 and "Rival" not in str(e.value.detail)
        with pytest.raises(HTTPException) as e:
            await _put("Global Mesh Size = 50 mm", Principal(owner_id=OWNER, organization_id=org,
                                                             credential=Credential.signed_header))
        assert e.value.status_code == 404
    assert store.rows[OTHER_ORG].text == RIVAL_WORDS and store.rows[OTHER_ORG].updated_by == RIVAL
    assert reader.calls == [] and OWN_ORG not in store.rows
    # and the rival, in their own organisation, sees their own words and never this caller's
    await _put(CRITERIA)
    theirs = await organization.read_meshing_standard(owner_id=RIVAL, organization_id=str(OTHER_ORG))
    assert theirs["text"] == RIVAL_WORDS and CRITERIA not in repr(theirs) and OWNER not in repr(theirs)


async def test_a_key_cannot_change_the_standard(store, reader):
    key = Principal(owner_id=OWNER, organization_id=str(OWN_ORG), credential=Credential.api_key,
                    key_id=str(uuid.uuid4()))
    with pytest.raises(HTTPException) as e:
        await _put(CRITERIA, key)
    assert e.value.status_code == 403 and "Sign in to the console" in str(e.value.detail)
    assert OWN_ORG not in store.rows and reader.calls == []


async def test_the_words_are_at_most_what_the_reader_reads(store, reader):
    with pytest.raises(ValidationError):
        organization.MeshingStandardIn(text="x" * (MSS.MAX_CHARS + 1))
    with pytest.raises(MSS.TooLong):
        await MSS.save(owner_id=OWNER, organization_id=str(OWN_ORG), text="x" * (MSS.MAX_CHARS + 1),
                       sessions=lambda: _Session(store))


async def test_an_approved_run_is_dispatched_with_the_standard_as_saved(store, reader):
    assert await MSS.for_dispatch(None, organization_id=str(OWN_ORG)) is None
    await _put(CRITERIA)
    snap = await MSS.for_dispatch(None, organization_id=str(OWN_ORG))
    assert snap["text"] == CRITERIA and snap["updated_by"] == OWNER
    assert snap["reading"]["key"] == MS.key_of(CRITERIA) and "read_at" not in snap["reading"]
    assert MS.kept_reading(snap) == snap["reading"]["arguments"]
    assert await MSS.for_dispatch(None, organization_id="") is None


async def test_the_approval_carries_the_standard_only_when_there_is_one(store, reader):
    import logging

    from meshpipeline.agents.intake import approval

    assert await approval._meshing_standard(lambda: _Session(store), str(OWN_ORG),
                                            logger=logging.getLogger("t")) is None
    await _put(CRITERIA)
    got = await approval._meshing_standard(lambda: _Session(store), str(OWN_ORG), logger=logging.getLogger("t"))
    assert got["text"] == CRITERIA
    assert await approval._meshing_standard(lambda: _Session(store), "", logger=logging.getLogger("t")) is None

    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("database down")

        async def __aexit__(self, *exc):
            return False

    # a standard that cannot be read never fails the approval: the run goes without it
    assert await approval._meshing_standard(lambda: _Broken(), str(OWN_ORG), logger=logging.getLogger("t")) is None
