# Responsibility: Verify the intake measures the user's upload on a real upload, where nothing stays staged.
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import shutil
from pathlib import Path

import pytest

import meshpipeline.application.geometry_materializer as gm
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.agents.intake import measured_choice
from meshpipeline.contracts.geometry_source import GeometrySourceRef


class _Store:
    """The object store's download, over a dict of key -> bytes."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.downloads = 0

    def download_file(self, *, object_key: str, destination: Path) -> None:
        self.downloads += 1
        Path(destination).write_bytes(self.objects[object_key])


def _ref(data: bytes, *, key: str = "uploads/u1/src1", suffix: str = ".stl",
         sha: str | None = None) -> GeometrySourceRef:
    return GeometrySourceRef(source_id="src1", owner_id="u1", object_key=key,
                             sha256=sha or hashlib.sha256(data).hexdigest(),
                             size_bytes=len(data), original_filename=f"part{suffix}",
                             suffix_hint=suffix)


_STL = b"solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n" \
       b"endloop\nendfacet\nendsolid t\n"


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(rtcfg, "JOBS_DIR", str(tmp_path))
    return tmp_path


def test_a_real_upload_leaves_nothing_staged_so_the_stored_source_is_fetched(jobs, monkeypatch):
    # The upload route deletes JOBS_DIR/<session>/input.<ext> once the object is stored, and a
    # direct upload never stages there: the intake used to read '' on every real upload.
    store = _Store({"uploads/u1/src1": _STL})
    monkeypatch.setattr(gm, "get_object_store", lambda: store)
    path = measured_choice.staged_upload("sess", _ref(_STL))
    assert path and Path(path).read_bytes() == _STL
    assert Path(path).parent == jobs / "sess"
    # kept for the next turn: no second fetch
    assert measured_choice.staged_upload("sess", _ref(_STL)) == path
    assert store.downloads == 1


def test_a_file_still_staged_beside_the_session_is_read_as_before(jobs, monkeypatch):
    monkeypatch.setattr(gm, "get_object_store", lambda: pytest.fail("fetched a staged upload"))
    (jobs / "sess").mkdir()
    (jobs / "sess" / "input.stl").write_bytes(_STL)
    assert measured_choice.staged_upload("sess", _ref(_STL)) == str(jobs / "sess" / "input.stl")


def test_bytes_that_do_not_match_the_source_record_are_never_measured(jobs, monkeypatch):
    store = _Store({"uploads/u1/src1": _STL + b"tampered"})
    monkeypatch.setattr(gm, "get_object_store", lambda: store)
    assert measured_choice.staged_upload("sess", _ref(_STL)) == ""
    assert not [p for p in (jobs / "sess").iterdir() if p.is_file()]


def test_no_source_too_big_or_an_unreachable_store_answers_nothing(jobs, monkeypatch):
    assert measured_choice.staged_upload("sess", None) == ""
    big = _ref(_STL)
    big = dataclasses.replace(big, size_bytes=gm.INTAKE_COPY_MAX_BYTES + 1)
    monkeypatch.setattr(gm, "get_object_store", lambda: pytest.fail("fetched an oversized upload"))
    assert measured_choice.staged_upload("sess", big) == ""

    class _Down:
        def download_file(self, **_kw):
            raise OSError("store unreachable")
    monkeypatch.setattr(gm, "get_object_store", lambda: _Down())
    assert measured_choice.staged_upload("sess", _ref(_STL)) == ""


def test_the_copies_on_one_host_stay_inside_their_total_bound(jobs, monkeypatch):
    # a hosted API's disk is memory: the oldest copies go first
    monkeypatch.setattr(gm, "INTAKE_COPY_TOTAL_BYTES", 2 * len(_STL))
    objects = {f"k{i}": _STL for i in range(4)}
    monkeypatch.setattr(gm, "get_object_store", lambda: _Store(objects))
    for i in range(4):
        assert measured_choice.staged_upload(f"s{i}", _ref(_STL, key=f"k{i}"))
    left = sorted(p.parent.name for p in jobs.glob("*/intake_copy.*"))
    assert left == ["s2", "s3"]


def test_the_measured_recommendation_runs_for_a_real_upload(jobs, monkeypatch):
    # end to end through the turn's own entry: no staged file, the bytes only in the store
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    import meshpipeline.agents.intake.agent as intake
    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 10.0,
                                        200.0).Shape(), STEPControl_AsIs)
    scratch = jobs / "scratch"
    scratch.mkdir()
    w.Write(str(scratch / "pipe.step"))
    data = (scratch / "pipe.step").read_bytes()
    shutil.rmtree(scratch)
    monkeypatch.setattr(gm, "get_object_store", lambda: _Store({"uploads/u1/src1": data}))
    state = {"session_id": "sess", "purpose": "internal_cfd", "input_kind": "fluid-domain",
             "geometry": {"ref": _ref(data, suffix=".step").to_payload()},
             "intake_patches": [{"name": "inlet", "type": "inlet", "diameter_mm": 20.0},
                                {"name": "outlet", "type": "outlet", "diameter_mm": 20.0},
                                {"name": "wall", "type": "wall"}]}
    rec = asyncio.run(intake._measured_recommendation(state, []))
    assert rec is not None and rec.engine in rec.ranking
