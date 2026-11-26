# Responsibility: Hold SHELL-59 (engines/shell_run.run): a surface the mesher could not mesh, sewn across slivers the
# extraction dropped (SHELL-16), is extracted again with its slivers kept and meshed once more, said in the record;
# a surface without dropped slivers, or one the mesher meshes, is made as before (no second extraction).
# Boundaries: the extraction and the mesher are replaced by stand-ins that record their calls; no OpenCASCADE run, no
# gmsh, no files but the run's own record.
from __future__ import annotations

import json

import pytest

from meshpipeline.cad import midsurface as ms
from meshpipeline.engines import shell_run
from meshpipeline.engines.gmsh import shell_adapter as sa

SLIVER_NOTE = "Part_1: 3 sliver face(s) under a tenth of a wall wide dropped and their neighbours sewn across them (SHELL-16)."


def _record(notes: list[str]) -> dict:
    return {"surface_faces": ["Mid_1"], "notes": notes, "bodies": [{"body": "Part_1", "verdict": "shell"}],
            "sewn": [{"open_edges": 0}]}


@pytest.fixture()
def calls(monkeypatch):
    seen: dict = {"extract": [], "mesh": 0}

    def extract_file(step, out, *, mm_per_unit, plates=True, slivers=True, **_k):
        seen["extract"].append({"plates": plates, "slivers": slivers})
        return _record([] if not slivers else seen["notes"])

    def mesh_run(out, **_k):
        seen["mesh"] += 1
        raise ValueError("gmsh could not mesh 3 face(s)")

    monkeypatch.setattr(ms, "extract_file", extract_file)
    monkeypatch.setattr(sa, "mesh_run", mesh_run)
    return seen


def test_a_surface_sewn_across_slivers_the_mesher_failed_is_read_again_with_its_slivers(tmp_path, calls):
    calls["notes"] = [SLIVER_NOTE]
    rec = shell_run.run(tmp_path, tmp_path / "part.step", mm_per_unit=1.0)
    assert calls["extract"] == [{"plates": True, "slivers": True}, {"plates": True, "slivers": False}]
    assert calls["mesh"] == 2
    assert rec["slivers_kept"] == shell_run.SLIVERS_KEPT and "gmsh could not mesh" in rec["slivers_mesh_error"]
    assert json.loads((tmp_path / shell_run.RECORD).read_text())["slivers_kept"]


def test_a_surface_without_dropped_slivers_is_not_read_again(tmp_path, calls):
    calls["notes"] = []
    rec = shell_run.run(tmp_path, tmp_path / "part.step", mm_per_unit=1.0)
    assert calls["extract"] == [{"plates": True, "slivers": True}] and calls["mesh"] == 1
    assert "slivers_kept" not in rec and rec["made"] is False


def test_with_the_rule_off_the_run_is_as_before(tmp_path, calls, monkeypatch):
    calls["notes"] = [SLIVER_NOTE]
    monkeypatch.setattr(shell_run, "SLIVERS_AGAIN", False)
    rec = shell_run.run(tmp_path, tmp_path / "part.step", mm_per_unit=1.0)
    assert len(calls["extract"]) == 1 and calls["mesh"] == 1 and "slivers_kept" not in rec


def test_has_slivers_reads_the_shell_16_note():
    assert ms.has_slivers(_record([SLIVER_NOTE])) and not ms.has_slivers(_record(["something else"]))
