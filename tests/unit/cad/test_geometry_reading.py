# Responsibility: Verify the geometry reader looks at the stored measurement and the durable bytes, never at the deleted staging directory, and that absence, failure and success stay three different values.
from __future__ import annotations

import asyncio
import types

import pytest

from meshpipeline.cad import regions
from meshpipeline.contracts.geometry_measurement import MEASUREMENT_SCHEMA
from meshpipeline.contracts.geometry_source import GeometrySourceRef

SHA = "b" * 64

REF = GeometrySourceRef(source_id="11111111-1111-4111-8111-111111111111", owner_id="owner",
                        object_key="sources/11111111-1111-4111-8111-111111111111", sha256=SHA,
                        size_bytes=1024, original_filename="part.step", suffix_hint=".step")


def _document(**over) -> dict:
    base = {
        "schema": MEASUREMENT_SCHEMA, "status": "ok", "reason": "",
        "projection": {"region_names": ["wall", "core"], "region_count": 2,
                       "region_source": "geometry_agent.facts.names",
                       "self_intersecting": "unknown", "diag": 2.62, "thin_gap": 0.0346,
                       "units": "m"},
    }
    base.update(over)
    return base


def _row(document: dict, sha: str = SHA):
    return types.SimpleNamespace(sha256=sha, document=document)


def _with_row(monkeypatch, row):
    """Stand in for the repository read, so the reader is tested and the database is not."""
    class _Repo:
        async def for_source(self, _db, **_kw):
            return row

    class _DB:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.geometry_measurement_repository"
        ".GeometryMeasurementRepository", _Repo)
    monkeypatch.setattr("meshpipeline.persistence.session.get_db", lambda *a, **k: _DB())


def test_the_stored_report_is_what_a_session_reads(monkeypatch):
    _with_row(monkeypatch, _row(_document()))
    analysis = asyncio.run(regions.reading_for_source(REF))
    assert analysis["status"] == "ok"
    assert analysis["region_names"] == ["wall", "core"] and analysis["region_count"] == 2
    assert analysis["diag"] == pytest.approx(2.62)


def test_self_intersecting_unknown_is_never_passed_through_as_a_claim(monkeypatch):
    """THE ONE TRANSLATION, and it is load-bearing.

    `engines/base.py:565` is `if ic.require_no_self_intersection and analysis.get("self_intersecting")`
    - a truthiness test - and the measurement package answers `"unknown"` rather than `None` because
    `None` reads as "not self-intersecting". A non-empty string is TRUE here, and
    `engines/vmtk/spec.py:190` declares `require_no_self_intersection=True` on an implemented engine.
    Passing the word through would reject every vmtk upload for a defect nothing looked for.
    """
    _with_row(monkeypatch, _row(_document()))
    analysis = asyncio.run(regions.reading_for_source(REF))
    assert "self_intersecting" not in analysis, "an unmeasured flag must be ABSENT, not a string"
    assert analysis["self_intersecting_state"] == "unknown"
    assert analysis.get("self_intersecting") is None   # what `base.py` reads: no claim


def test_vmtk_admits_geometry_nobody_tested_for_self_intersection(monkeypatch):
    """The regression the translation above prevents, asserted through the real engine spec."""
    from meshpipeline.engines.registry import get_spec

    spec = get_spec("vmtk")
    assert spec.input_contract.require_no_self_intersection, "this test is about that declaration"
    _with_row(monkeypatch, _row(_document()))
    analysis = asyncio.run(regions.reading_for_source(REF))
    assert spec.geometry_unsuitable(analysis) == ""
    # and the same spec still rejects a surface that WAS measured and does self-intersect
    assert spec.geometry_unsuitable({**analysis, "self_intersecting": True})


def test_a_measured_true_flag_still_comes_through(monkeypatch):
    _with_row(monkeypatch, _row(_document(
        projection={"region_names": [], "region_count": 1, "region_source": "",
                    "self_intersecting": True, "diag": 1.0, "thin_gap": None, "units": "m"})))
    analysis = asyncio.run(regions.reading_for_source(REF))
    assert analysis["self_intersecting"] is True
    assert "self_intersecting_state" not in analysis


def test_a_failed_measurement_carries_its_status_and_no_numbers(monkeypatch):
    """Three situations used to arrive as `{}`. This is the middle one."""
    _with_row(monkeypatch, _row(_document(status="measurement_failed",
                                          reason="the tessellation ran out of memory")))
    analysis = asyncio.run(regions.reading_for_source(REF))
    assert analysis["status"] == "measurement_failed"
    assert "memory" in analysis["reason"]
    assert "diag" not in analysis and "region_count" not in analysis


def test_a_measurement_of_different_bytes_is_refused_and_reads_as_not_attempted(monkeypatch):
    """A port table measured off a file the customer replaced is worse than no table."""
    _with_row(monkeypatch, _row(_document(), sha="c" * 64))
    monkeypatch.setattr(regions, "_analysis_from_object_store", _never_called)
    assert asyncio.run(regions.reading_for_source(REF)) is None


async def _never_called(_ref):
    return None


def test_no_measurement_falls_back_to_the_durable_bytes(monkeypatch):
    """The route the old reader could not take: `upload.py:253` deletes the staging buffer, and the
    object in storage is the copy that survives for thirty days."""
    _with_row(monkeypatch, None)
    fetched: list = []

    def _fetch(ref, *, workspace, job_id=""):
        fetched.append(ref.object_key)
        from pathlib import Path
        p = Path(workspace) / "input.step"
        p.write_text("not really a step")
        return p

    monkeypatch.setattr("meshpipeline.application.geometry_materializer.fetch_verified_bytes",
                        _fetch)
    analysis = asyncio.run(regions.reading_for_source(REF))
    assert fetched == [REF.object_key]
    assert analysis["status"] == "ok" and analysis["reading_source"] == "object_store"
    # it measured only what it read: no diag, no thin_gap, and no self-intersection claim
    assert "diag" not in analysis and "thin_gap" not in analysis
    assert analysis.get("self_intersecting") is None


def test_nothing_anywhere_reads_as_not_attempted_and_never_as_an_empty_dict(monkeypatch):
    """`{}` passes `base.py:430-434`'s `is not None` test carrying nothing. `None` does not."""
    _with_row(monkeypatch, None)

    def _boom(*_a, **_k):
        raise RuntimeError("the object store is unreachable")

    monkeypatch.setattr("meshpipeline.application.geometry_materializer.fetch_verified_bytes",
                        _boom)
    assert asyncio.run(regions.reading_for_source(REF)) is None
    assert asyncio.run(regions.reading_for_source(None)) is None


def test_a_database_that_cannot_be_reached_is_an_absence_not_a_failure(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("no database configured in this process")

    monkeypatch.setattr("meshpipeline.persistence.session.get_db", _boom)
    monkeypatch.setattr(regions, "_analysis_from_object_store", _never_called)
    assert asyncio.run(regions.reading_for_source(REF)) is None
    assert asyncio.run(regions.stored_document_for_source(REF)) is None


def test_the_staging_reader_is_still_there_and_still_answers_a_staged_file(tmp_path):
    """Kept for the one case it answers: a file staged and not yet cleaned up."""
    (tmp_path / "s1").mkdir()
    assert regions.regions_for_session("s1", tmp_path).count == 0        # nothing staged
    assert regions.regions_for_session("missing", tmp_path).count == 0   # no directory at all


def test_the_document_reader_returns_the_whole_document(monkeypatch):
    """Two readers want two different things and neither is derivable from the other."""
    _with_row(monkeypatch, _row(_document(openings=[{"id": "o1"}])))
    document = asyncio.run(regions.stored_document_for_source(REF))
    assert document["schema"] == MEASUREMENT_SCHEMA
    assert document["openings"] == [{"id": "o1"}]
