# Responsibility: Verify the files a user attached in the chat or notes box (NOTE-16) are in their account export and are erased: the request takes the names they gave them and keeps only the object keys, and the sweep deletes every stored file and read text by those keys.
# Boundaries: no database and no store: a fake session answers the sweep's queries, a dict-like store records the deletes; the request's SQL half is covered against real PostgreSQL in tests/integration/test_privacy_lifecycle_postgres.py.
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy.sql import Select

from meshpipeline.application import chat_attachments as ca
from meshpipeline.application import privacy_service as ps
from meshpipeline.persistence.models import ChatSession

PIC, PDF = "a" * 16, "b" * 16
SID = uuid.uuid4()


def _index() -> dict:
    return {
        PIC: {"kind": "image", "name": "bearing bore.png", "mime": "image/png", "bytes": 1200,
              "object_key": ca.object_key(SID, PIC, "file"), "sha256": "c" * 64, "at": "2026-11-23T10:00:00+00:00",
              "width": 800, "height": 600},
        PDF: {"kind": "text", "name": "acme spec.pdf", "mime": "application/pdf", "bytes": 9000,
              "object_key": ca.object_key(SID, PDF, "file"), "text_key": ca.object_key(SID, PDF, "text.txt"),
              "sha256": "d" * 64, "at": "2026-11-23T10:01:00+00:00", "pages": 3, "chars": 4000, "cut": False},
    }


def test_the_request_keeps_only_where_the_bytes_are():
    left = ca.erased_index(_index())
    assert left == {PIC: {"object_key": ca.object_key(SID, PIC, "file")},
                    PDF: {"object_key": ca.object_key(SID, PDF, "file"),
                          "text_key": ca.object_key(SID, PDF, "text.txt")}}
    assert "bearing" not in repr(left) and "acme" not in repr(left)
    # the sweep still finds every key in what the request left
    assert sorted(ca.object_keys(SimpleNamespace(attachments=left))) == sorted(
        ca.object_keys(SimpleNamespace(attachments=_index())))
    assert ca.erased_index(None) is None and ca.erased_index({}) is None


def test_the_export_lists_each_file_without_its_keys_or_text():
    rows = ca.exported(_index())
    assert [r["name"] for r in rows] == ["bearing bore.png", "acme spec.pdf"]
    assert rows[1]["pages"] == 3 and rows[0]["at"].startswith("2026-11-23")
    assert not any(k in r for r in rows for k in ("object_key", "text_key", "sha256", "text"))
    assert ca.exported(None) == [] and ca.exported({"../x": {"name": "x"}}) == []


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _Db:
    """Answers the sweep's selects: the one session for ChatSession, nothing for the rest."""

    def __init__(self, session):
        self.session, self.deleted = session, []

    async def execute(self, stmt):
        if isinstance(stmt, Select) and stmt.column_descriptions[0].get("entity") is ChatSession:
            return _Result([self.session])
        return _Result([])

    async def delete(self, row):
        self.deleted.append(row)

    async def flush(self):
        return None


class _Store:
    def __init__(self):
        self.gone: list[str] = []

    def exists(self, *, object_key):
        return True

    def get_bytes(self, *, object_key):
        raise FileNotFoundError(object_key)

    def delete_object(self, *, object_key):
        self.gone.append(object_key)


def test_the_sweep_deletes_every_attached_file_and_its_read_text_then_the_row(monkeypatch):
    from meshpipeline.application import part_matches

    monkeypatch.setattr(part_matches, "picture_keys", lambda _sid: [])
    session = SimpleNamespace(id=SID, attachments=ca.erased_index(_index()))
    db, store = _Db(session), _Store()
    asyncio.run(ps._erase_bytes(db, store, uuid.uuid4(), datetime.now(UTC)))
    for key in (ca.object_key(SID, PIC, "file"), ca.object_key(SID, PDF, "file"),
                ca.object_key(SID, PDF, "text.txt")):
        assert key in store.gone
    assert db.deleted == [session]


def test_a_session_with_no_files_is_swept_as_before(monkeypatch):
    from meshpipeline.application import part_matches

    monkeypatch.setattr(part_matches, "picture_keys", lambda _sid: [])
    session = SimpleNamespace(id=SID, attachments=None)
    db, store = _Db(session), _Store()
    asyncio.run(ps._erase_bytes(db, store, uuid.uuid4(), datetime.now(UTC)))
    assert not any("/attachments/" in k for k in store.gone) and db.deleted == [session]
