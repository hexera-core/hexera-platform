# Responsibility: Verify the direct upload - begin, the client's PUT, finish - refuses what the multipart upload refuses and keeps its guarantees.
# Boundaries: the routes over an in-memory store and an in-memory intent table; the SQL scoping is checked on the rendered query.
from __future__ import annotations

import hashlib
import uuid
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.api.v1.upload as upload_mod
import meshpipeline.api.v1.upload_direct as direct_mod
from meshpipeline.api.security import org_dep, owner_dep, plan_dep
from meshpipeline.contracts import object_storage
from meshpipeline.contracts.geometry_source import source_object_key
from meshpipeline.persistence.models import GeometrySource, ReconciliationState

_VALID_STEP = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('t'),'2;1');\nENDSEC;\n"
               b"DATA;\nENDSEC;\nEND-ISO-10303-21;\n")
_ALICE, _BOB = "alice", "bob"
_ORG_A, _ORG_B = str(uuid.uuid4()), str(uuid.uuid4())


# an in-memory world: intents, committed sources and the sessions they opened

class _World:
    def __init__(self) -> None:
        self.intents: dict[str, SimpleNamespace] = {}
        self.sources: list[GeometrySource] = []
        self.session_for_source: dict[uuid.UUID, uuid.UUID] = {}
        self.greetings: list[tuple] = []
        self.quota_error: str | None = None
        self.fail_commit = False
        self.checks: list[dict] = []


class _FakeDb:
    """Changes apply on commit and vanish otherwise - the transaction the real code relies on."""

    def __init__(self, world: _World) -> None:
        self.world = world
        self.pending: list = []
        self.added: list = []
        self.sessions: list[uuid.UUID] = []

    async def execute(self, _stmt):
        return MagicMock()

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        if self.world.fail_commit:
            raise RuntimeError("the database went away")
        for apply in self.pending:
            apply()
        for row in self.added:
            self.world.sources.append(row)
            if self.sessions:
                self.world.session_for_source[row.id] = self.sessions[-1]
        self.pending, self.added = [], []


def _visible(row, *, owner_id: str, organization_id: str) -> bool:
    # tenant_scope.scope: the organisation when there is one, else the owner - and the actor too
    tenant_ok = (row.organization_id == organization_id) if organization_id else True
    return tenant_ok and row.owner_id == owner_id


def _fake_repo_class(world: _World):
    class _Repo:
        async def record_intent(self, db, *, owner_id, source_id, object_key,
                                organization_id="", hold=None):
            def apply():
                world.intents.setdefault(object_key, SimpleNamespace(
                    owner_id=owner_id, organization_id=organization_id, source_id=source_id,
                    object_key=object_key, state=ReconciliationState.pending, hold=hold,
                    detail=None))
            db.pending.append(apply)

        async def resolve(self, db, *, object_key, state, detail=None):
            def apply():
                row = world.intents.get(object_key)
                if row is not None:
                    row.state, row.detail = state, detail
            db.pending.append(apply)

        async def get_upload_for_owner(self, db, *, source_id, object_key, owner_id,
                                       organization_id=""):
            row = world.intents.get(object_key)
            if row is None or row.source_id != source_id:
                return None
            return row if _visible(row, owner_id=owner_id,
                                   organization_id=organization_id) else None

        lock_upload_for_owner = get_upload_for_owner

    return _Repo


def _fake_job_service(world: _World):
    svc = MagicMock()

    async def check_quotas(db, owner_id, *, plan=""):
        if world.quota_error:
            raise ValueError(world.quota_error)

    async def create_session(db, owner_id, *, organization_id=""):
        sid = uuid.uuid4()
        db.sessions.append(sid)
        return sid

    svc.check_quotas = check_quotas
    svc.create_session = create_session
    return svc


def _fake_session_repo(world: _World):
    class _Sessions:
        async def get_for_owner_by_geometry_source(self, db, source_id, owner_id, *,
                                                   organization_id=""):
            sid = world.session_for_source.get(source_id)
            return SimpleNamespace(id=sid) if sid else None

        async def append_message(self, db, session_id, role, content):
            world.greetings.append((session_id, role, content))

    return _Sessions


@pytest.fixture()
def staging(tmp_path):
    d = tmp_path / "upload-staging"
    d.mkdir()
    return d


@pytest.fixture()
def world(staging):
    w = _World()

    @asynccontextmanager
    async def _get_db():
        yield _FakeDb(w)

    with ExitStack() as stack:
        for p in (
            patch("meshpipeline.persistence.session.get_db", _get_db),
            patch("meshpipeline.application.job_service.JobService",
                  return_value=_fake_job_service(w)),
            patch("meshpipeline.persistence.repositories.source_cleanup_repository"
                  ".SourceCleanupRepository", _fake_repo_class(w)),
            patch("meshpipeline.persistence.repositories.session_repository.SessionRepository",
                  _fake_session_repo(w)),
            patch.object(upload_mod, "_JOBS_DIR", staging),
            patch.object(upload_mod.icfg, "INTAKE_GREETING_ON_UPLOAD", True),
            patch.object(upload_mod, "_enqueue_geometry_check",
                         lambda sid, owner, **kw: w.checks.append({"session": sid, **kw})),
        ):
            stack.enter_context(p)
        yield w


def _app(owner: str = _ALICE, org: str = _ORG_A) -> FastAPI:
    app = FastAPI()
    app.include_router(direct_mod.router, prefix="/api/v1/upload")
    app.dependency_overrides[owner_dep] = lambda: owner
    app.dependency_overrides[org_dep] = lambda: org
    app.dependency_overrides[plan_dep] = lambda: ""
    return app


async def _post(path: str, body: dict, *, owner: str = _ALICE, org: str = _ORG_A):
    async with AsyncClient(transport=ASGITransport(app=_app(owner, org)),
                           base_url="http://test") as c:
        return await c.post(path, json=body)


async def _begin(name="wing.step", size=len(_VALID_STEP), **who):
    return await _post("/api/v1/upload/direct", {"filename": name, "size_bytes": size}, **who)


async def _finish(upload_id: str, name="wing.step", **who):
    return await _post(f"/api/v1/upload/direct/{upload_id}/finalize", {"filename": name}, **who)


def _store():
    return object_storage.get_object_store()


def _put(upload_id: str, payload: bytes) -> None:
    """What a client holding the signed URL can do: write any bytes at all under that key."""
    _store().put_bytes(object_key=source_object_key(upload_id), payload=payload)


# begin

async def test_begin_hands_out_a_url_for_one_key_behind_a_held_intent(world):
    resp = await _begin()
    assert resp.status_code == 200, resp.text
    out = resp.json()
    key = source_object_key(out["upload_id"])
    assert out["method"] == "PUT" and key in out["upload_url"]
    assert out["headers"] == {"Content-Type": "application/octet-stream"}
    assert out["finalize_url"] == f"/api/v1/upload/direct/{out['upload_id']}/finalize"
    assert out["max_bytes"] == upload_mod._MAX_FILE_BYTES
    # write-ahead: the intent names the key before any byte can exist, and it is held
    intent = world.intents[key]
    assert intent.state is ReconciliationState.pending
    assert intent.hold == direct_mod.UPLOAD_HOLD and intent.hold >= timedelta(hours=1)
    assert intent.owner_id == _ALICE and intent.organization_id == _ORG_A
    assert _store().keys() == []                       # nothing stored yet


@pytest.mark.parametrize("name", ["model.obj", "model", "notes.txt", "../../etc/passwd"])
async def test_begin_refuses_a_suffix_the_product_does_not_accept(world, name):
    resp = await _begin(name=name)
    assert resp.status_code == 422
    assert "Accepted geometry formats" in resp.json()["detail"]
    assert world.intents == {}


@pytest.mark.parametrize("name,tool", [
    ("fuselage.SLDASM", "SolidWorks"), ("wing.CATProduct", "CATIA"), ("duct.asm.4", "Creo"),
])
async def test_begin_refuses_native_cad_before_a_byte_moves_and_says_how_to_export(world, name, tool):
    # a 300 MB native assembly is exactly the file that takes the direct route; it must hear
    # which export to upload instead before it spends minutes travelling to storage
    resp = await _begin(name=name, size=300 * 1024 * 1024)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith("Hexera can't open ") and tool in detail and "STEP" in detail
    assert world.intents == {}


async def test_begin_refuses_an_empty_file(world):
    resp = await _begin(size=0)
    assert resp.status_code == 422 and "empty" in resp.json()["detail"].lower()
    assert world.intents == {}


async def test_begin_refuses_an_oversized_file_in_plain_words_before_a_byte_moves(world):
    resp = await _begin(size=upload_mod._MAX_FILE_BYTES + 1)
    assert resp.status_code == 413
    detail = resp.json()["detail"]
    assert "500 MB" in detail and "export only the bodies" in detail
    assert world.intents == {}


async def test_begin_gives_the_quota_refusal_the_multipart_upload_gives(world):
    world.quota_error = "You already have 2 active job(s)."
    resp = await _begin()
    assert resp.status_code == 429 and "active job" in resp.json()["detail"]
    assert world.intents == {}


async def test_a_url_that_cannot_be_signed_settles_its_intent(world, monkeypatch):
    def refuse(**_kw):
        raise object_storage.StorageError("signing failed")
    monkeypatch.setattr(_store(), "create_upload_url", refuse)
    resp = await _begin()
    assert resp.status_code == 503
    (intent,) = world.intents.values()
    assert intent.state is ReconciliationState.resolved_deleted


# finish: the happy path, and repeating it

async def test_finish_checks_the_stored_bytes_and_answers_what_step_file_answers(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    resp = await _finish(upload_id)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert set(out) == {"session_id", "step_filename", "intake_greeting", "trace"}
    assert out["step_filename"] == "wing.step"
    assert out["intake_greeting"] == upload_mod.UPLOAD_ACKNOWLEDGEMENT

    (source,) = world.sources
    assert str(source.id) == upload_id
    assert source.object_key == source_object_key(upload_id)
    assert source.sha256 == hashlib.sha256(_VALID_STEP).hexdigest()
    assert source.size_bytes == len(_VALID_STEP) and source.suffix_hint == ".step"
    # the committed source never has an open intent
    assert world.intents[source.object_key].state is ReconciliationState.resolved_adopted
    assert _store().keys() == [source.object_key]      # the object is kept
    assert world.checks and world.checks[0]["source_payload"]["sha256"] == source.sha256
    assert world.greetings == [(uuid.UUID(out["session_id"]), "assistant",
                                upload_mod.UPLOAD_ACKNOWLEDGEMENT)]


async def test_a_declared_unit_is_recorded_with_the_source_as_the_multipart_upload_does(world):
    from meshpipeline.cad.unit_evidence import UnitEvidence
    from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

    recorded: list[dict] = []

    class _Interpretations:
        async def record(self, db, **kw):
            recorded.append(kw)
            return SimpleNamespace(interpretation_id=str(uuid.uuid4()),
                                   geometry_source_id=kw["geometry_source_id"], unit=kw["unit"],
                                   scale_to_metres=0.001, basis=kw["basis"], evidence=kw["evidence"])

    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    with (patch.object(upload_mod, "_declared_unit_evidence",
                       lambda _p: UnitEvidence(True, LengthUnit.millimetre, "declared in the file")),
          patch("meshpipeline.persistence.repositories.geometry_interpretation_repository"
                ".GeometryInterpretationRepository", _Interpretations)):
        resp = await _finish(upload_id)
    assert resp.status_code == 200, resp.text
    (kw,) = recorded
    assert kw["unit"] is LengthUnit.millimetre and kw["basis"] is ResolutionBasis.file_declared
    assert str(kw["geometry_source_id"]) == upload_id and kw["organization_id"] == _ORG_A
    assert world.checks[0]["interpretation_payload"]["unit"] == LengthUnit.millimetre.value


async def test_finishing_twice_answers_with_the_same_session_and_changes_nothing(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    first = (await _finish(upload_id)).json()
    again = await _finish(upload_id)
    assert again.status_code == 200, again.text
    assert again.json()["session_id"] == first["session_id"]
    assert len(world.sources) == 1 and len(world.greetings) == 1


@pytest.mark.parametrize("name,payload", [
    ("part.stl", b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n"
                 b"vertex 0 1 0\nendloop\nendfacet\nendsolid t\n"),
    ("PART.STP", _VALID_STEP),
])
async def test_other_accepted_formats_finish_the_same_way(world, name, payload):
    upload_id = (await _begin(name=name, size=len(payload))).json()["upload_id"]
    _put(upload_id, payload)
    resp = await _finish(upload_id, name=name)
    assert resp.status_code == 200, resp.text
    assert world.sources[0].suffix_hint == name[name.rindex("."):].lower()


# finish: what a client can get wrong, or try

async def test_a_spoofed_step_file_is_refused_and_its_object_removed(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, b"PK\x03\x04 this is a zip, not a STEP file")
    resp = await _finish(upload_id)
    assert resp.status_code == 400
    assert "ISO-10303-21" in resp.json()["detail"]
    assert _store().keys() == []                       # the exact key is gone
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.resolved_deleted
    assert world.sources == []


async def test_an_object_larger_than_the_cap_is_refused_whatever_was_declared(world, monkeypatch):
    upload_id = (await _begin(size=10)).json()["upload_id"]      # declared small
    monkeypatch.setattr(upload_mod, "_MAX_FILE_BYTES", 64)
    _put(upload_id, _VALID_STEP)                                  # arrived larger than the cap
    resp = await _finish(upload_id)
    assert resp.status_code == 413
    assert _store().keys() == []
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.resolved_deleted
    assert world.sources == []


async def test_an_empty_object_is_refused_and_removed(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, b"")
    resp = await _finish(upload_id)
    assert resp.status_code == 422 and "empty" in resp.json()["detail"].lower()
    assert _store().keys() == []


async def test_finish_refuses_a_suffix_the_product_does_not_accept(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    resp = await _finish(upload_id, name="wing.exe")
    assert resp.status_code == 422
    # nothing was touched: the upload can still be finished under its real name
    assert _store().keys() == [source_object_key(upload_id)]
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.pending


async def test_finishing_before_the_bytes_arrive_keeps_the_upload_open(world):
    upload_id = (await _begin()).json()["upload_id"]
    resp = await _finish(upload_id)
    assert resp.status_code == 409 and "has not reached storage" in resp.json()["detail"]
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.pending
    _put(upload_id, _VALID_STEP)
    assert (await _finish(upload_id)).status_code == 200


@pytest.mark.parametrize("who", [
    {"owner": _BOB, "org": _ORG_B},        # another tenant
    {"owner": _BOB, "org": _ORG_A},        # another person in the same organisation
])
async def test_another_persons_upload_reads_as_one_that_never_existed(world, who):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    resp = await _finish(upload_id, **who)
    assert resp.status_code == 404
    assert "No upload with that id" in resp.json()["detail"]
    # untouched: still Alice's to finish
    assert _store().keys() == [source_object_key(upload_id)]
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.pending
    assert world.sources == []
    assert (await _finish(upload_id)).status_code == 200


@pytest.mark.parametrize("upload_id", ["not-a-uuid", "..%2F..%2Fsecrets", str(uuid.uuid4())])
async def test_an_unknown_or_malformed_id_is_not_found(world, upload_id):
    resp = await _finish(upload_id)
    assert resp.status_code == 404


async def test_an_upload_the_sweep_already_reclaimed_says_so(world):
    upload_id = (await _begin()).json()["upload_id"]
    world.intents[source_object_key(upload_id)].state = ReconciliationState.resolved_deleted
    resp = await _finish(upload_id)
    assert resp.status_code == 410 and "Upload the file again" in resp.json()["detail"]


async def test_a_quota_refusal_at_finish_keeps_the_bytes_for_a_later_finish(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    world.quota_error = "You already have 2 active job(s)."
    resp = await _finish(upload_id)
    assert resp.status_code == 429
    assert _store().keys() == [source_object_key(upload_id)]
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.pending
    assert world.sources == []
    world.quota_error = None
    assert (await _finish(upload_id)).status_code == 200


async def test_a_database_failure_at_adoption_commits_nothing_and_keeps_the_file(world):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, _VALID_STEP)
    world.fail_commit = True
    resp = await _finish(upload_id)
    assert resp.status_code == 503
    assert "does not need to be sent again" in resp.json()["detail"]
    # no source, no adopted intent - and the object is still named by its pending intent
    assert world.sources == []
    assert world.intents[source_object_key(upload_id)].state is ReconciliationState.pending
    assert _store().keys() == [source_object_key(upload_id)]
    world.fail_commit = False
    assert (await _finish(upload_id)).status_code == 200


@pytest.mark.parametrize("payload,status", [(_VALID_STEP, 200), (b"not a step", 400)])
async def test_the_staging_copy_never_outlives_the_finish(world, staging, payload, status):
    upload_id = (await _begin()).json()["upload_id"]
    _put(upload_id, payload)
    assert (await _finish(upload_id)).status_code == status
    assert not any(staging.iterdir()), "the read-back copy was left on the API's disk"


# the scoping is in the SQL, not only in the fake above

def _rendered(stmt) -> str:
    from sqlalchemy.dialects import postgresql
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_the_upload_read_is_scoped_to_the_tenant_and_the_person():
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )

    sql = _rendered(SourceCleanupRepository._upload_query(
        source_id=uuid.uuid4(), object_key="sources/x", owner_id=_ALICE,
        organization_id=_ORG_A))
    where = sql.split("WHERE", 1)[1]
    for column in ("object_key", "source_id", "owner_id", "organization_id"):
        assert f"source_object_cleanups.{column} =" in where, f"{column} is not in the scope"

    ownerless = _rendered(SourceCleanupRepository._upload_query(
        source_id=uuid.uuid4(), object_key="sources/x", owner_id=_ALICE, organization_id=""))
    assert "source_object_cleanups.owner_id =" in ownerless.split("WHERE", 1)[1]


def test_the_session_lookup_for_a_repeated_finish_is_tenant_scoped():
    import inspect

    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    src = inspect.getsource(SessionRepository.get_for_owner_by_geometry_source)
    assert "tenant_scope.scope(ChatSession" in src and "geometry_source_id" in src


def test_the_multipart_route_and_the_direct_route_share_one_set_of_checks():
    import inspect

    src = inspect.getsource(direct_mod)
    for shared in ("sanitised_upload_name", "has_step_header", "STEP_HEADER_REFUSAL",
                   "_declared_unit_evidence", "_MAX_FILE_BYTES", "UPLOAD_ACKNOWLEDGEMENT"):
        assert f"_multipart.{shared}" in src, f"the direct upload has its own {shared}"
    assert "ACCEPTED_SUFFIXES" in src


# the console sends a large file the direct way

def _ui(rel: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[3] / "ui" / "js" / rel).read_text(encoding="utf-8")


def test_the_console_sends_a_large_file_straight_to_storage_with_progress():
    import re

    api = _ui("api/endpoints.js")
    threshold = re.search(r"DIRECT_UPLOAD_FROM_BYTES = (\d+) \* 1024 \* 1024", api)
    assert threshold, "the console no longer decides which files go direct"
    # below Cloud Run's 32 MiB request cap, with room for the multipart envelope
    assert int(threshold.group(1)) < 32
    assert 'apiFetch("/api/v1/upload/direct"' in api and "begun.finalize_url" in api
    put = api[api.index("export function putToStorage"):api.index("function storageRefusal")]
    assert "XMLHttpRequest" in put and "upload.onprogress" in put
    # the object store is not the API: no identity header, and a 403 there is not a sign-out
    assert "apiFetch" not in put and "headers()" not in put
    composer = _ui("shell/composer.js")
    assert "uploadGeometry(file, { onProgress: progress })" in composer
    assert "Checking the file" in composer and "Upload failed: " in composer


def test_a_huge_iges_is_not_handed_to_opencascade_at_upload(tmp_path, monkeypatch):
    # OCC loads the whole IGES model to report its unit - several times the file's size in memory,
    # on an API process whose disk is memory too. Above the bound the unit is confirmed in
    # conversation instead, which is what any file that states no unit already gets.
    import meshpipeline.cad.unit_evidence as ue

    def never(_path):
        raise AssertionError("the IGES reader ran on a file over the bound")

    monkeypatch.setattr(ue, "read_declared_unit", never)
    big = tmp_path / "input.iges"
    with open(big, "wb") as fh:                       # sparse: no real 65 MB is written
        fh.truncate(upload_mod._IGES_UNIT_READ_MAX_BYTES + 1)
    evidence = upload_mod._declared_unit_evidence(big)
    assert evidence is not None and not evidence.resolved
    assert "too large" in evidence.detail
