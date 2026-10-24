# Responsibility: Verify several CAD files uploaded together become one assembly file: each part where its file puts it and named from the file's product or the file's name, by the files' own text when they agree on schema and unit and by OpenCASCADE otherwise; a file that is not a CAD solid is refused in plain words; and the upload route stores the one joined file.
# Boundaries: parts built in the test under tmp_path; the upload route runs with its database and store mocked (as tests/unit/api/test_upload_greeting.py does).
from __future__ import annotations

import uuid
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.cad.ingest.assembly_files import AssemblyFilesError, Member, combine  # noqa: E402
from meshpipeline.cad.scout import read_cad  # noqa: E402


def _write(shape, path: Path, unit: str = "MM") -> Path:
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    Interface_Static.SetCVal_s("write.step.unit", unit)
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(path))
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    return path


def _parts(path: Path) -> list[tuple[str, list[float], list[float]]]:
    shape = read_cad(path)
    names = A.match_body_names(A.read_body_names(path), A.solids(shape))
    rec = A.record(shape, mm_per_unit=1.0, names=names)
    return [(p["name"], p["centroid_mm"], p["box_mm"]) for p in rec["parts"]]


def test_step_files_in_one_unit_are_joined_as_their_own_text(tmp_path):
    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "flange.step")
    b = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "bolt.step")
    how = combine([Member(a, "flange"), Member(b, "bolt")], tmp_path / "joined.step")
    assert how == {"files": 2, "method": "text", "unit_known": True}
    parts = _parts(tmp_path / "joined.step")
    assert [p[0] for p in parts] == ["flange", "bolt"]                  # generic product names: the file's
    assert parts[1][1] == pytest.approx([15.0, 5.0, 5.0])                # placed by its own coordinates
    text = (tmp_path / "joined.step").read_text()
    assert text.count("END-ISO-10303-21;") == 1 and text.count("DATA;") == 1


def test_files_in_different_units_are_read_and_written_as_one_in_millimetres(tmp_path):
    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a.step")
    b = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "b_in_metres.step", unit="M")
    how = combine([Member(a, "a"), Member(b, "b_in_metres")], tmp_path / "joined.step")
    assert how["method"] == "occ"
    parts = _parts(tmp_path / "joined.step")
    assert [p[0] for p in parts] == ["a", "b_in_metres"]
    assert parts[1][1] == pytest.approx([15.0, 5.0, 5.0], abs=1e-6) and parts[1][2] == pytest.approx([10, 10, 10])
    rec = A.record(read_cad(tmp_path / "joined.step"), mm_per_unit=1.0)
    assert rec["interfaces"][0]["kind"] == "touching"                    # the two still touch


def test_a_file_that_is_not_a_cad_solid_cannot_join(tmp_path):
    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a.step")
    stl = tmp_path / "b.stl"
    stl.write_text("solid b\nendsolid b\n")
    with pytest.raises(AssemblyFilesError, match="each is a CAD solid"):
        combine([Member(a, "a"), Member(stl, "b.stl")], tmp_path / "joined.step")


# #
# the upload route
# #

_SESSION_ID = uuid.UUID("12345678-1234-4234-a234-123456789abc")


@asynccontextmanager
async def _mock_get_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    yield db


@pytest.mark.asyncio
async def test_the_upload_joins_several_files_into_the_one_source_it_stores(tmp_path):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    import meshpipeline.api.v1.upload as upload_mod

    app = FastAPI()
    app.include_router(upload_mod.router, prefix="/api/v1/upload")
    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "flange.step").read_bytes()
    b = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "bolt.step").read_bytes()
    svc = MagicMock()
    svc.check_quotas = AsyncMock()
    svc.create_session = AsyncMock(return_value=_SESSION_ID)
    stored: dict = {}
    store = MagicMock()
    store.upload_file = MagicMock(side_effect=lambda local_path, object_key: stored.update(
        bytes=Path(local_path).read_bytes()))
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    with ExitStack() as stack:
        for p in (patch("meshpipeline.persistence.session.get_db", _mock_get_db),
                  patch("meshpipeline.application.job_service.JobService", return_value=svc),
                  patch("meshpipeline.contracts.object_storage.get_object_store", return_value=store),
                  patch.object(upload_mod, "_JOBS_DIR", jobs),
                  patch.object(upload_mod, "_enqueue_geometry_check", lambda *a, **k: None),
                  # the unit the joined file declares is recorded by the real repository: not here
                  patch.object(upload_mod, "_declared_unit_evidence", lambda _p: None)):
            stack.enter_context(p)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/upload/step-file", files=[
                ("file", ("flange.step", a, "application/octet-stream")),
                ("parts", ("bolt.step", b, "application/octet-stream"))])
            assert resp.status_code == 200, resp.text
            assert resp.json()["step_filename"] == "flange_assembly_of_2.step"
            joined = tmp_path / "stored.step"
            joined.write_bytes(stored["bytes"])
            assert [p[0] for p in _parts(joined)] == ["flange", "bolt"]
            bad = await c.post("/api/v1/upload/step-file", files=[
                ("file", ("flange.step", a, "application/octet-stream")),
                ("parts", ("notes.stl", b"solid x\nendsolid x\n", "application/octet-stream"))])
            assert bad.status_code == 422 and "each is a CAD solid" in bad.json()["detail"]


def _write_named(shapes_names, path: Path) -> Path:
    """A STEP file whose bodies carry product names (as an assembly export writes them)."""
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("t"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for shape, name in shapes_names:
        TDataStd_Name.Set_s(tool.AddShape(shape, False), TCollection_ExtendedString(name))
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.Transfer(doc, STEPControl_AsIs)
    w.Write(str(path))
    return path


def test_the_kernel_join_keeps_each_files_own_body_names(tmp_path):
    a = _write_named([(fx._box(0, 0, 0, 10, 10, 10), "Flange"), (fx._box(10, 0, 0, 10, 10, 10), "Gasket")],
                     tmp_path / "flange_set.step")
    b = _write(fx._box(20, 0, 0, 10, 10, 10), tmp_path / "bolt_in_metres.step", unit="M")
    how = combine([Member(a, "flange_set"), Member(b, "bolt_in_metres")], tmp_path / "joined.step")
    assert how["method"] == "occ"
    assert [p[0] for p in _parts(tmp_path / "joined.step")] == ["Flange", "Gasket", "bolt_in_metres"]


def test_a_join_past_its_guard_is_refused_in_plain_words(tmp_path):
    from meshpipeline.cad.ingest.assembly_files import combine_guarded

    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a.step")
    b = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "b.step", unit="M")
    with pytest.raises(AssemblyFilesError, match="export the assembly as one STEP file|Export the assembly"):
        combine_guarded([Member(a, "a"), Member(b, "b")], tmp_path / "joined.step", memory_mb=64)


def _brep(shape, path: Path) -> Path:
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return path


def test_a_brep_takes_the_unit_the_other_files_state(tmp_path):
    # the STEP is in metres; the BREP (no unit of its own) is drawn in the same metres beside it
    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a_in_metres.step", unit="M")
    b = _brep(fx._box(0.01, 0, 0, 0.01, 0.01, 0.01), tmp_path / "b.brep")
    how = combine([Member(a, "a"), Member(b, "b")], tmp_path / "joined.step")
    assert how["method"] == "occ" and how["unit_known"] is True
    parts = _parts(tmp_path / "joined.step")
    assert parts[1][1] == pytest.approx([15.0, 5.0, 5.0], abs=1e-6) and parts[1][2] == pytest.approx([10, 10, 10])


def test_a_step_whose_unit_is_not_named_is_never_scaled_again(tmp_path, monkeypatch):
    """OpenCASCADE converts every STEP to millimetres: one whose unit we cannot name keeps its size and
    leaves the unit to the user, only a BREP is scaled."""
    from meshpipeline.cad.ingest import assembly_files as AF

    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a_in_metres.step", unit="M")
    b = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "b_unnamed.step")
    real = AF._stated_metres
    monkeypatch.setattr(AF, "_stated_metres", lambda p: None if p.name == "b_unnamed.step" else real(p))
    how = combine([Member(a, "a"), Member(b, "b")], tmp_path / "joined.step", guarded=False)
    assert how["method"] == "occ" and how["unit_known"] is False
    assert _parts(tmp_path / "joined.step")[1][1] == pytest.approx([15.0, 5.0, 5.0], abs=1e-6)


def test_breps_alone_leave_the_unit_to_the_user(tmp_path):
    a = _brep(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a.brep")
    b = _brep(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "b.brep")
    how = combine([Member(a, "a"), Member(b, "b")], tmp_path / "joined.step")
    assert how["unit_known"] is False
    assert _parts(tmp_path / "joined.step")[1][1] == pytest.approx([15.0, 5.0, 5.0], abs=1e-6)


def test_a_brep_beside_files_in_different_units_is_refused_in_words(tmp_path):
    a = _write(fx._box(0, 0, 0, 10, 10, 10), tmp_path / "a.step")
    b = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "b.step", unit="M")
    c = _brep(fx._box(20, 0, 0, 10, 10, 10), tmp_path / "c.brep")
    with pytest.raises(AssemblyFilesError, match="does not state its unit"):
        combine([Member(a, "a"), Member(b, "b"), Member(c, "c")], tmp_path / "joined.step")


def test_section_words_inside_a_name_or_comment_do_not_cut_the_text(tmp_path):
    from meshpipeline.cad.ingest.assembly_files import _sections

    text = (b"ISO-10303-21;\nHEADER;\nFILE_NAME('x');\nENDSEC;\nDATA;\n"
            b"#1=PRODUCT('ENDSEC;','DATA;','',());\n/* ENDSEC; */\n#2=X(#1);\nENDSEC;\nEND-ISO-10303-21;\n")
    head, body = _sections(text)
    assert head.endswith(b"DATA;") and b"#2=X(#1);" in body and body.count(b"ENDSEC;") == 2


def test_unclosed_comments_are_scanned_in_linear_time():
    import time

    from meshpipeline.cad.ingest.assembly_files import _renumbered, _sections

    body = b"#1=X(#2);" + b"/*x" * 200_000
    t0 = time.monotonic()
    out, top = _renumbered(body, 10)
    with pytest.raises(AssemblyFilesError):
        _sections(b"HEADER;ENDSEC;DATA;" + body)
    assert time.monotonic() - t0 < 2.0
    assert out.startswith(b"#11=X(#12);") and top == 2


def test_an_extra_file_named_wrongly_never_replaces_the_first(tmp_path):
    """A STEP file named .iges is moved to the staged STEP name: in its own folder, never onto the first."""
    import asyncio

    import meshpipeline.api.v1.upload as upload_mod
    from meshpipeline.contracts.intake_formats import staged_name_for

    session = tmp_path / "s"
    session.mkdir()
    first = _write(fx._box(0, 0, 0, 10, 10, 10), session / staged_name_for(".step"))
    other = _write(fx._box(10, 0, 0, 10, 10, 10), tmp_path / "bolt.step").read_bytes()

    class _Up:
        filename = "bolt.iges"

        def __init__(self, data):
            self.data = [data, b""]

        async def read(self, _n):
            return self.data.pop(0)

    joined, suffix, name, known = asyncio.run(upload_mod._joined_assembly(
        first, ".step", "flange.step", [_Up(other)], session, first.stat().st_size))
    assert suffix == ".step" and known is True
    assert [round(p[1][0]) for p in _parts(joined)] == [5, 15]
