# Responsibility: Verify a verified unit declaration is recorded at upload, and an unverifiable one is left to the user.
from __future__ import annotations

import os
import uuid

import pytest
from tests.cad_fixtures import write_iges, write_step

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("real PostgreSQL and MinIO endpoints are required", allow_module_level=True)

pytest.importorskip("OCP.STEPControl")

pytestmark = pytest.mark.asyncio

_OWNER = "tenant-upload-interpretation"


class _Upload:

    def __init__(self, filename: str, payload: bytes) -> None:
        self.filename = filename
        self._payload = payload
        self._sent = False

    async def read(self, _n: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self._payload


async def _upload(path, store):
    from meshpipeline.api.v1 import upload as upload_mod
    return await upload_mod.upload_step_file(
        file=_Upload(path.name, path.read_bytes()), owner_id=_OWNER)


async def _session_scale(session_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        row = (await db.execute(text(
            "select i.unit, i.basis, i.scale_to_metres, i.evidence "
            "from chat_sessions s "
            "left join geometry_interpretations i on i.id = s.geometry_interpretation_id "
            "where s.id = :sid"), {"sid": uuid.UUID(session_id)})).first()
    return row


@pytest.fixture(autouse=True)
def _not_gated_by_another_suites_jobs(monkeypatch):
    # MAX_CONCURRENT_JOBS is a WHOLE-SYSTEM quota and this tier shares one database, so jobs left
    # active by unrelated suites refuse these uploads for capacity. Measured under a randomised
    # order: 28 active against a limit of 20, failing eight tests here for a reason that has
    # nothing to do with unit interpretation. Only the system-wide limit is raised; the per-owner
    # one is scoped to this suite's own tenants, and quotas are proven in their own suite.
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "MAX_CONCURRENT_JOBS", 1_000_000, raising=False)


@pytest.mark.parametrize("declared,expected_unit,expected_scale", [
    ("MM", "mm", 1e-3),
    ("CM", "cm", 1e-2),
    ("INCH", "in", 0.0254),
])
async def test_a_verified_declaration_is_recorded_at_upload(store, tmp_path, declared,
                                                            expected_unit, expected_scale):
    out = await _upload(write_step(tmp_path / f"{declared}.step", declared), store)

    row = await _session_scale(out.session_id)
    assert row is not None
    unit, basis, scale, evidence = row
    assert unit == expected_unit
    assert basis == "file_declared"          # never user_confirmed - nobody was asked
    assert scale == pytest.approx(expected_scale)
    assert evidence, "a recorded interpretation must say why the unit is believed"


async def test_a_format_carrying_no_unit_records_nothing(store, tmp_path):
    stl = tmp_path / "surface.stl"
    stl.write_text("solid s\nfacet normal 0 0 1\nouter loop\n"
                   "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
                   "endloop\nendfacet\nendsolid s\n")

    out = await _upload(stl, store)

    unit, basis, scale, _ = await _session_scale(out.session_id)
    assert unit is None and basis is None and scale is None, (
        "an STL has no declared unit; recording one would be a guess presented as a fact")


async def test_a_step_declaring_metres_is_left_for_the_user_to_confirm(store, tmp_path):
    out = await _upload(write_step(tmp_path / "metre.step", "M"), store)

    unit, basis, _, _ = await _session_scale(out.session_id)
    assert unit is None and basis is None, (
        "a STEP declaration of metres collides with the parser's failure default and must be "
        "confirmed, not trusted")


async def test_a_damaged_unit_context_is_left_for_the_user_to_confirm(store, tmp_path):
    from tests.cad_fixtures import corrupt_step_unit

    good = write_step(tmp_path / "good.step", "MM")
    broken = corrupt_step_unit(good, tmp_path / "broken.step", "malformed")

    out = await _upload(broken, store)

    unit, basis, _, _ = await _session_scale(out.session_id)
    assert unit is None and basis is None


async def test_an_iges_declaration_is_recorded(store, tmp_path):
    out = await _upload(write_iges(tmp_path / "part.igs", "MM"), store)

    row = await _session_scale(out.session_id)
    assert row is not None
    unit, basis, scale, _ = row
    assert (unit, basis) == ("mm", "file_declared")
    assert scale == pytest.approx(1e-3)


async def test_the_interpretation_belongs_to_the_uploaded_source(store, tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import get_db

    out = await _upload(write_step(tmp_path / "bound.step", "INCH"), store)

    async with get_db() as db:
        row = (await db.execute(text(
            "select s.geometry_source_id, i.geometry_source_id, i.owner_id "
            "from chat_sessions s "
            "join geometry_interpretations i on i.id = s.geometry_interpretation_id "
            "where s.id = :sid"), {"sid": uuid.UUID(out.session_id)})).first()

    session_source, interpretation_source, owner = row
    assert interpretation_source == session_source
    assert owner == _OWNER


async def test_an_unreadable_file_still_uploads(store, tmp_path):
    from unittest.mock import patch

    with patch("meshpipeline.cad.unit_evidence.read_declared_unit",
               side_effect=RuntimeError("parser exploded")):
        out = await _upload(write_step(tmp_path / "fine.step", "MM"), store)

    assert out.session_id
    unit, basis, _, _ = await _session_scale(out.session_id)
    assert unit is None and basis is None
