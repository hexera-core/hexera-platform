# Responsibility: Verify the user's attachments are checked by their bytes, pictures re-encoded with no metadata, text and PDF text read under caps, kept per session, and only the owner's ids resolved (NOTE-16).
from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from meshpipeline.api.schemas.chat import MAX_ATTACHMENTS
from meshpipeline.application import chat_attachments as ca

SID = uuid.UUID("abab1111-2222-4222-b222-abababababab")


def _png(w=40, h=30, alpha=False) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", (w, h), (200, 10, 10, 128) if alpha else (200, 10, 10)).save(out, "PNG")
    return out.getvalue()


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (64, 48), (10, 200, 10))
    exif = Image.Exif()
    exif[0x010F] = "SpyCam"          # Make
    exif[0x0112] = 6                 # Orientation: rotate 90
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def _pdf(text: str | None = "Bore cell size 2 mm", pages: int = 1, password: str | None = None) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    for _ in range(pages):
        page = w.add_blank_page(200, 200)
        if text is not None:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = w._add_object(stream)
    if password:
        w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_the_limit_per_message_is_the_schemas():
    assert ca.MAX_PER_MESSAGE == MAX_ATTACHMENTS


@pytest.mark.parametrize("data,what", [
    (b"\x89PNG\r\n\x1a\n....", "png"), (b"\xff\xd8\xff\xe0", "jpeg"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
    (b"%PDF-1.7\n", "pdf"), (b"diameter,size\n20,2\n", "text"), ("Ø20 bore".encode(), "text"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None), (b"  <!DOCTYPE html><html>", None),
    (b"<?xml version='1.0'?><svg/>", None), (b"MZ\x90\x00\x03\x00\x00\x00", None), (b"PK\x03\x04", None),
    (b"\xff\xfe\x00b", None)])
def test_files_are_known_by_their_bytes(data, what):
    assert ca.sniff(data) == what


def test_a_picture_is_re_encoded_upright_with_no_metadata():
    got = ca.read(_jpeg_with_exif())
    assert got.kind == "image" and got.mime == "image/jpeg"
    with Image.open(io.BytesIO(got.data)) as img:
        assert img.getexif() == {} and "exif" not in img.info
        assert img.size == (48, 64), "the EXIF orientation was applied before it was dropped"


def test_a_picture_with_transparency_stays_png_and_is_downscaled():
    got = ca.read(_png(3000, 1000, alpha=True))
    assert got.mime == "image/png" and (got.width, got.height) == (ca.LONG_SIDE, 523)


def test_a_picture_too_many_pixels_is_refused_before_decoding(monkeypatch):
    monkeypatch.setattr(ca, "MAX_PIXELS_IN", 100)
    with pytest.raises(ca.Refused) as e:
        ca.read(_png(20, 20))
    assert e.value.status == 413 and e.value.line == ca.TOO_MANY_PIXELS


def test_a_broken_picture_is_told():
    with pytest.raises(ca.Refused) as e:
        ca.read(_png()[:60])
    assert e.value.line == ca.BAD_IMAGE


def test_sizes_are_capped(monkeypatch):
    monkeypatch.setattr(ca, "IMAGE_MAX_BYTES", 10)
    with pytest.raises(ca.Refused) as e:
        ca.read(_png())
    assert e.value.status == 413 and "10 MB" in e.value.line
    monkeypatch.setattr(ca, "TEXT_MAX_BYTES", 5)
    with pytest.raises(ca.Refused) as e:
        ca.read(b"hello world")
    assert "20 MB" in e.value.line


def test_svg_named_png_is_refused():
    with pytest.raises(ca.Refused) as e:
        ca.read(b"<svg onload='alert(1)'/>")
    assert e.value.status == 415 and e.value.line == ca.NOT_ACCEPTED


def test_text_is_read_and_cut(monkeypatch):
    monkeypatch.setattr(ca, "MAX_CHARS", 5)
    got = ca.read("\ufeffbore 2 mm".encode())
    assert got.kind == "text" and got.text == "bore " and got.cut


def test_a_pdf_text_is_read():
    got = ca.read(_pdf("Bore cell size 2 mm", pages=2))
    assert got.mime == "application/pdf" and got.pages == 2 and not got.cut
    assert "Bore cell size 2 mm" in got.text and "--- page 2 ---" in got.text


def test_a_long_pdf_reads_its_first_pages_and_says_so(monkeypatch):
    monkeypatch.setattr(ca, "PDF_MAX_PAGES", 2)
    got = ca.read(_pdf("x 1", pages=4))
    assert got.pages == 4 and got.cut and "--- page 3 ---" not in got.text


def test_a_scanned_pdf_and_a_locked_pdf_are_told():
    with pytest.raises(ca.Refused) as e:
        ca.read(_pdf(None))
    assert e.value.line == ca.SCANNED_PDF
    with pytest.raises(ca.Refused) as e:
        ca.read(_pdf("secret", password="pw"))
    assert e.value.line == ca.LOCKED_PDF


def test_a_slow_pdf_is_stopped():
    with pytest.raises(ca.Refused) as e:
        ca.read_pdf(_pdf("x", pages=2), seconds=-1)
    assert e.value.line == ca.SLOW_PDF


def test_garbage_after_the_pdf_mark_is_told():
    with pytest.raises(ca.Refused) as e:
        ca.read(b"%PDF-1.4\n garbage garbage")
    assert e.value.line in (ca.BAD_PDF, ca.SCANNED_PDF)


def test_names_are_cleaned():
    assert ca.clean_name("C:\\Users\\me\\spec\x00.pdf") == "spec.pdf"
    assert ca.clean_name("", "pasted.png") == "pasted.png"
    assert len(ca.clean_name("a" * 500)) == 120


def test_resolve_only_the_sessions_own_ids_and_never_keys():
    entry = ca.entry_for("0123456789abcdef", "bore.png", ca.read(_png()), session_id=SID)
    session = SimpleNamespace(attachments={"0123456789abcdef": entry})
    rows = ca.resolve(session, ["0123456789abcdef", "fedcba9876543210", "../../x", "0123456789abcdef"])
    assert [r["id"] for r in rows] == ["0123456789abcdef"]
    assert "object_key" not in rows[0] and "sha256" not in rows[0] and rows[0]["name"] == "bore.png"
    assert ca.object_keys(session) == [f"sessions/{SID}/attachments/0123456789abcdef/file"]


def test_served_never_as_something_a_browser_runs():
    assert ca.served_type({"mime": "text/html"}) == "text/plain; charset=utf-8"
    h = ca.served_headers({"mime": "application/pdf", "name": 'a"b.pdf'})
    assert h["X-Content-Type-Options"] == "nosniff" and h["Content-Disposition"] == 'attachment; filename="a_b.pdf"'
    assert "sandbox" in h["Content-Security-Policy"]
    assert ca.served_headers({"mime": "image/png", "name": "x.png"})["Content-Disposition"].startswith("inline")


# #
# keeping, under the session lock
# #

class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
        self.objects[object_key] = local_path.read_bytes()

    def get_bytes(self, *, object_key):
        from meshpipeline.contracts.object_storage import ObjectNotFound
        if object_key not in self.objects:
            raise ObjectNotFound(object_key)
        return self.objects[object_key]

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)


class _Repo:
    def __init__(self, session):
        self.session = session

    async def get_for_owner(self, db, sid, owner, organization_id=""):
        return self.session if owner == self.session.owner_id else None

    async def add_attachment(self, db, sid, owner, aid, entry, *, most):
        s = self.session
        if owner != s.owner_id:
            return "not_found"
        if s.job_id:
            return "running"
        if len(s.attachments or {}) >= most:
            return "full"
        s.attachments = {**(s.attachments or {}), aid: entry}
        return "added"


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        pass


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr("meshpipeline.contracts.object_storage.get_object_store", lambda: s)
    return s


async def test_accept_keeps_a_pdf_and_its_text(store):
    session = SimpleNamespace(owner_id="alice", job_id=None, attachments=None)
    row = await ca.accept(SID, "alice", name="spec.pdf", data=_pdf(), session_repo=_Repo(session), db_factory=_Db)
    assert row["kind"] == "text" and row["pages"] == 1 and row["name"] == "spec.pdf"
    entry = session.attachments[row["id"]]
    assert store.objects[entry["text_key"]].decode().count("Bore cell size 2 mm") == 1
    got = await ca.open_for_owner(SID, row["id"], "alice", session_repo=_Repo(session), db_factory=_Db)
    assert got is not None and got[0].startswith(b"%PDF-")
    assert await ca.open_for_owner(SID, row["id"], "mallory", session_repo=_Repo(session), db_factory=_Db) is None


async def test_accept_refuses_another_owner_a_started_run_and_a_full_session(store, monkeypatch):
    session = SimpleNamespace(owner_id="alice", job_id=None, attachments=None)
    with pytest.raises(LookupError):
        await ca.accept(SID, "mallory", name="x.png", data=_png(), session_repo=_Repo(session), db_factory=_Db)
    session.job_id = uuid.uuid4()
    with pytest.raises(ca.Refused) as e:
        await ca.accept(SID, "alice", name="x.png", data=_png(), session_repo=_Repo(session), db_factory=_Db)
    assert e.value.status == 409
    session.job_id = None
    # the run starts while the file is being read: refused under the lock, its bytes dropped
    repo = _Repo(session)
    real = repo.get_for_owner

    async def started_after_the_check(*a, **kw):
        got = await real(*a, **kw)
        session.job_id = uuid.uuid4()
        return got
    repo.get_for_owner = started_after_the_check
    with pytest.raises(ca.Refused) as e:
        await ca.accept(SID, "alice", name="x.png", data=_png(), session_repo=repo, db_factory=_Db)
    assert e.value.status == 409 and store.objects == {}
    session.job_id = None
    monkeypatch.setattr(ca, "MAX_PER_SESSION", 1)
    await ca.accept(SID, "alice", name="x.png", data=_png(), session_repo=_Repo(session), db_factory=_Db)
    with pytest.raises(ca.Refused):
        await ca.accept(SID, "alice", name="y.png", data=_png(), session_repo=_Repo(session), db_factory=_Db)
    assert len(store.objects) == 1, "a refused file leaves no bytes behind"
