# Responsibility: Verify a valid FEA mesh is never lost after meshing (Areen, 24 Oct: only a broken mesh fails): its return is sized by what the mesher measured, its 3D view and any one solver file are notes when missing, the CFD non-orthogonality row never gates it, and a mesh the measured limits refuse is told as the check it failed.
from __future__ import annotations

import dataclasses
import io
import random
import tarfile
from pathlib import Path

import pytest

import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
import meshpipeline.settings.policy as polcfg
from meshpipeline.sandbox import safe_extract as se

# #
# the return: sized by what the mesher measured
# #

def _tar(files: dict[str, int]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, n in files.items():
            data = random.Random(n).randbytes(n)        # does not compress: the archive is as big as the files
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def small_ceilings(monkeypatch):
    """The generic limits shrunk a thousand-fold, so a test archive of a few MB stands for one of a
    few GB: the arithmetic is the same."""
    base = se.default_limits()
    small = dataclasses.replace(base, max_archive_bytes=1_000_000, max_total_uncompressed_bytes=2_000_000,
                                max_file_bytes=1_000_000)
    monkeypatch.setattr(se, "default_limits", lambda: small)
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 1_000)
    monkeypatch.setattr(crc, "RESULT_BYTES_PER_CELL", 100)
    return small


def test_a_big_fea_mesh_crosses_the_old_ceiling_and_comes_home(tmp_path, small_ceilings):
    # an FEA workspace as the mesher sends it: the deck, the Nastran and Ansys files, gmsh's mesh
    files = {"mesh.inp": 1_200_000, "mesh.bdf": 1_500_000, "mesh.cdb": 1_800_000, "mesh.msh": 900_000,
             "quality.json": 100}
    archive = tmp_path / "result.tar.gz"
    archive.write_bytes(_tar(files))
    raw, largest = sum(files.values()), max(files.values())
    cells = 1_500                                    # few elements: the flow allowance per cell is far short
    flow = crc.result_extraction_limits({"rc": 0, "quality": {"cells": cells}})
    assert largest > flow.max_file_bytes and raw > flow.max_total_uncompressed_bytes   # the OLD ceiling
    with pytest.raises(se.WorkspaceExtractionError):
        se.safe_extract_tar(path=archive, dest=str(tmp_path / "old"), limits=flow)
    fea = crc.result_extraction_limits({"rc": 0, "quality": {"cells": cells, "quality_bar": "fea"},
                                        "returned_bytes": {"raw": raw, "largest": largest, "fea": True}})
    se.safe_extract_tar(path=archive, dest=str(tmp_path / "new"), limits=fea)
    assert (tmp_path / "new" / "mesh.cdb").stat().st_size == files["mesh.cdb"]


def test_an_fea_result_without_a_measurement_still_gets_the_fea_allowance(monkeypatch):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 8_000_000)
    lim = crc.result_extraction_limits({"rc": 0, "quality": {"cells": 3_000_000, "quality_bar": "fea"}})
    assert lim.max_file_bytes >= 3_000_000 * crc.FEA_RESULT_FILE_BYTES_PER_ELEMENT
    assert lim.max_total_uncompressed_bytes >= 3_000_000 * crc.FEA_RESULT_RAW_BYTES_PER_ELEMENT


def test_no_fea_document_lifts_the_limits_without_end(small_ceilings):
    lim = crc.result_extraction_limits({"quality": {"cells": 10 ** 12, "quality_bar": "fea"},
                                        "returned_bytes": {"raw": 10 ** 15, "largest": 10 ** 15, "fea": True}})
    assert max(lim.max_archive_bytes, lim.max_total_uncompressed_bytes, lim.max_file_bytes) <= crc.FEA_RESULT_MAX_BYTES


def test_a_flow_result_is_sized_exactly_as_before(monkeypatch):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 8_000_000)
    doc = {"rc": 0, "quality": {"cells": 6_707_503}}
    with_bytes = {**doc, "returned_bytes": {"raw": 9 * 1024 ** 3, "largest": 3 * 1024 ** 3, "fea": False}}
    assert crc.result_extraction_limits(with_bytes) == crc.result_extraction_limits(doc)
    assert crc.result_extraction_limits(doc).max_archive_bytes == max(
        8_000_000 * crc.RESULT_BYTES_PER_CELL, 6_707_503 * crc.RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL)


def test_the_mesher_measures_what_it_sends_home(tmp_path):
    from meshpipeline.engines.dispatch import returned_bytes

    (tmp_path / "fea_plan.json").write_text("{}")
    (tmp_path / "mesh.inp").write_bytes(b"x" * 500)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "big.bdf").write_bytes(b"y" * 2_000)
    got = returned_bytes(tmp_path)
    assert got == {"raw": 2_502, "largest": 2_000, "fea": True}


# #
# the 3D view is a note for FEA
# #

def test_an_fea_run_needs_its_bundle_not_its_view():
    from meshpipeline.application import artifact_policy as ap

    assert ap.required_output_classes("gmsh", "structural") == [ap.REQUIRED_BUNDLE_CLASS]
    assert ap.required_ready("gmsh", [ap.REQUIRED_BUNDLE_CLASS], "structural")
    # a flow run on the same engine still needs both
    assert ap.required_output_classes("gmsh", "internal_cfd") == [ap.REQUIRED_BUNDLE_CLASS, ap.REQUIRED_VIEWER_CLASS]
    assert not ap.required_ready("gmsh", [ap.REQUIRED_BUNDLE_CLASS], "internal_cfd")
    assert not ap.is_required_class("gmsh", ap.REQUIRED_VIEWER_CLASS, "structural")


def test_an_fea_view_that_cannot_be_built_still_opens_the_result_screen(tmp_path, monkeypatch):
    from meshpipeline.application import artifact_uploader as au
    from meshpipeline.engines import fea_results

    monkeypatch.setattr(fea_results, "results", lambda ws: {"title": "FEA mesh", "solvers": []})
    payload = au.fea_viewer_fallback(tmp_path, "MemoryError: the surface")
    assert payload["surface"]["patches"] == [] and payload["surface"]["fea"]["title"] == "FEA mesh"
    assert payload["surface"]["fea"]["view_note"] == fea_results.FEA_VIEW_MISSING


def test_the_gmsh_view_of_an_fea_mesh_falls_back_when_the_mesh_cannot_be_read(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_results
    from meshpipeline.engines.gmsh import spec as gspec
    from meshpipeline.engines.gmsh import viewer_surface as vs

    (tmp_path / "fea_plan.json").write_text("{}")
    monkeypatch.setattr(vs, "read_msh", lambda *a, **k: (_ for _ in ()).throw(MemoryError("too big")))
    monkeypatch.setattr(fea_results, "results", lambda ws: {"title": "FEA mesh"})
    resp = gspec._viewer_surface()(tmp_path, roles={}, units="m")
    assert resp["patches"] == [] and resp["fea"]["view_note"] == fea_results.FEA_VIEW_MISSING


# #
# one solver file failing is a note
# #

def test_a_solver_writer_that_fails_leaves_the_others(tmp_path, monkeypatch):
    from meshpipeline.engines import fea_writers as fw
    from meshpipeline.engines.fea_results import FILE_MISSING, _ready

    monkeypatch.setattr(fw, "prepare", lambda mesh: object())
    monkeypatch.setattr(fw, "write_bdf", lambda path, prep, **k: (Path(path).write_text("BDF"), {"file": "mesh.bdf"})[1])

    def broken(path, prep, **k):
        Path(path).write_text("half")
        raise MemoryError("cdb")

    monkeypatch.setattr(fw, "write_cdb", broken)
    out = fw.write_all(tmp_path, mesh=None)
    assert out["nastran"] == {"file": "mesh.bdf"} and "MemoryError" in out["ansys"]["error"]
    assert (tmp_path / "mesh.bdf").exists() and not (tmp_path / "mesh.cdb").exists()
    line = _ready(tmp_path, {"file": "mesh.cdb"})
    assert line["file_ready"] is False and line["file_note"] == FILE_MISSING


# #
# the verdict never gates an FEA mesh on a CFD measure; a refusal is told as its check
# #

def _fea_manifest(**q) -> dict:
    quality = {"fea": {"plan": True}, "element_order": 2, "fatal": [], "elements_under_floor": 0,
               "min_sicn": 0.2}
    quality.update(q)
    return {"quality": quality}


def test_the_cfd_non_orthogonality_row_never_gates_an_fea_mesh():
    from meshpipeline.engines import verdict_limits as vl

    keys = {r["key"] for r in vl.gating_rows("gmsh", _fea_manifest(max_non_ortho=89.0))}
    assert "max_non_ortho" not in keys and "fatal" in keys
    flow = {r["key"] for r in vl.gating_rows("gmsh", {"quality": {"max_non_ortho": 89.0, "fatal": []}})}
    assert "max_non_ortho" in flow


def test_a_mesh_the_limits_refuse_is_told_as_the_check_it_failed():
    from meshpipeline.application import final_result as fr

    checks = fr.limits_failure_checks({"inputs": {
        "valid": True, "gating": [{"key": "fatal", "label": "No degenerate elements", "op": "empty",
                                   "limit": None, "measured": ["degenerate elements (SICN <= 0)"]}],
        "limits": []}})
    assert checks and checks[0]["label"] == "No degenerate elements"
    res = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="gmsh", purpose="structural",
        dimensionality="3D", approved_snapshot_id="", executor_success=True, reviewer_verdict="PASS",
        failed_gate=fr.MEASURED_LIMITS_GATE, api_failure="", attempts=0, attempts_max=3,
        required_ready=False, delivered_types=[], optional_warnings=[], failure_cause="mesh_quality",
        failure_facts={"checks": checks})
    assert res.failure_category == fr.FailureCategory.gate_failed
    msg = fr.render_message(res)
    assert "could not be stored" not in msg and "No degenerate elements" in msg


def test_a_real_storage_failure_still_says_so():
    from meshpipeline.application import final_result as fr

    res = fr.build_final_result(
        job_id="j", owner_id="o", status=fr.TerminalStatus.failed, engine="gmsh", purpose="structural",
        dimensionality="3D", approved_snapshot_id="", executor_success=True, reviewer_verdict="PASS",
        failed_gate="", api_failure="", attempts=0, attempts_max=3, required_ready=False,
        delivered_types=[], optional_warnings=[])
    assert res.failure_category == fr.FailureCategory.delivery_failed


# #
# room on the worker: a result too big for its disk is refused in words and kept
# #

class _Blob:
    def __init__(self, size):
        self.size = size

    def reload(self):
        pass


class _Bucket:
    def __init__(self, size):
        self._size = size

    def blob(self, key):
        return _Blob(self._size)


def _usage(free):
    import collections
    return collections.namedtuple("u", "total used free")(free * 2, free, free)


def test_an_fea_result_that_fits_is_collected(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(80 * 10 ** 9))
    doc = {"quality": {"quality_bar": "fea", "cells": 8_000_000},
           "returned_bytes": {"raw": 9 * 10 ** 9, "largest": 3 * 10 ** 9, "fea": True}}
    assert crc.worker_room(_Bucket(3 * 10 ** 9), "out", doc, tmp_path) is None     # 15 GB of 80


def test_an_fea_result_too_big_for_the_worker_is_refused_and_kept(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(20 * 10 ** 9))
    doc = {"quality": {"quality_bar": "fea"}, "returned_bytes": {"raw": 30 * 10 ** 9, "largest": 9 * 10 ** 9, "fea": True}}
    got = crc.worker_room(_Bucket(10 * 10 ** 9), "out", doc, tmp_path)
    assert got["rc"] == crc.RC_INFRASTRUCTURE and got["too_big_for_worker"]["need_bytes"] == 50 * 10 ** 9
    assert crc.TOO_BIG_MARKER in got["log_tail"] and "kept" in got["log_tail"]


def test_a_flow_result_is_not_measured_for_room(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(1))
    assert crc.worker_room(_Bucket(10 ** 12), "out", {"quality": {"cells": 5}}, tmp_path) is None


def test_the_user_reads_how_much_disk_it_needed():
    from meshpipeline.contracts.failure_cause import FailureCause, describe

    what, nxt = describe(FailureCause.RUN_INFRASTRUCTURE,
                         {"result_uncollected": True,
                          "too_big_for_worker": {"need_bytes": 50e9, "free_bytes": 20e9}})
    assert "too big for the computer that collects it" in what and "50 GB" in what and "20 GB" in what
    assert "kept" in what and "run it again" in nxt.lower()


def test_the_fea_driver_ends_the_attempt_on_a_result_too_big_for_the_worker(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    from types import SimpleNamespace

    pytest.importorskip("gmsh")
    from tests import fea_fixtures as fx
    from tests.unit.engines import test_fea_driver as TD

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime

    ws = TD._stage(fx.plate_with_hole(), tmp_path / "big") if hasattr(TD, "_stage") else None
    if ws is None:
        pytest.skip("no staging helper")
    calls = []

    def mesher(w, timeout):
        calls.append(1)
        return {"rc": crc.RC_INFRASTRUCTURE, "timed_out": False,
                "too_big_for_worker": {"need_bytes": 50e9, "free_bytes": 20e9}, "log_tail": crc.TOO_BIG_MARKER}

    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(run_cartesian_mesh=mesher))
    ok, marker, _o = asyncio.run(FD.drive(ws, {"purpose": "structural"}, job_id="t-big", publish=None,
                                          run=TD._Run(), source_path=str(ws / "upload.step")))
    assert not ok and marker == FD.TOO_BIG and calls == [1]
    ref = _json.loads((ws / "preflight_refusal.json").read_text())
    assert ref["cause"] == "run_infrastructure" and ref["facts"]["too_big_for_worker"]["need_bytes"] == 50e9
