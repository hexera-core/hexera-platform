# Responsibility: Verify MIXED-5 in the FEA run: when the stage's Auto settled "shell + solid" (state fea_mesh_as
# "mixed"), the FEA driver runs engines/mixed_run on the staged solid (metres, as the builder stages it) in a process
# of its own, cites it, and the downloads offer the mixed files; a part whose walls are too thick for shells makes no
# mixed files and says why, and the solid path goes on. Shell and Solid are unchanged.
# Boundaries: a plate with a thick boss and the box with thick corners, meshed in a child process (about a minute);
# no solver run here (the CalculiX accuracy cases are test_mixed_textbook.py / test_mixed_families.py).
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from tests import shell_fixtures as sf  # noqa: E402
from tests.unit.engines.test_shell_run import _Run, _stage  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import mixed_run  # noqa: E402


def test_mesh_as_reads_mixed_only_as_settled():
    assert FD.mesh_as_of({"fea_mesh_as": "mixed"}) == "mixed"
    assert FD.mesh_as_of({"fea_mesh_as": "MIXED "}) == "mixed"
    assert FD.mesh_as_of({"fea_mesh_as": "beam"}) == "solid"
    # a mixed part is a 3D part: asked as 2D, only a shell keeps the FEA driver
    assert FD.applies({"purpose": "structural", "dimensionality": "2D", "fea_mesh_as": "mixed"}) is False


def test_the_driver_makes_the_mixed_mesh_when_auto_settled_it(tmp_path, monkeypatch):
    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    ws = _stage(sf.plate_with_boss(), tmp_path / "ws")
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "mixed"}, job_id="j", run=_Run()))
    assert rec["made"] is True, rec.get("why")
    assert rec["recommended"] is True and rec["too_thick"] == []
    for f in mixed_run.FILES:
        assert (ws / f).stat().st_size > 0, f
    assert not (ws / "shell.bdf").exists()
    (entry,) = cited
    assert entry["rule"] == "MIXED-5" and entry["stage"] == FD.MIXED_GATE and entry["decided"] == "mixed mesh"
    assert entry["words"] == rec["words"] and entry["words"].startswith("Shell-and-solid mesh: ")
    c = rec["counts"]
    assert entry["value"] == c["shell_elements"] + c["solid_elements"] and c["links"] > 0
    # staged in metres, read at its own scale: the deck's nodes are in metres (the plate is 200 mm wide)
    assert json.loads((ws / mixed_run.RECORD).read_text())["mm_per_unit"] == pytest.approx(1000.0)


def test_walls_too_thick_for_shells_make_no_mixed_files_and_say_why(tmp_path, monkeypatch):
    from tests.unit.engines import test_mixed_families as fam

    cited: list = []
    monkeypatch.setattr(FD, "_cite", lambda job_id, entries: cited.extend(entries))
    ws = _stage(fam._family("box_corners")[0], tmp_path / "ws")
    rec = asyncio.run(FD.shell_pass(ws, {"fea_mesh_as": "mixed"}, job_id="j", run=_Run()))
    assert rec["made"] is False and rec["why"].startswith("Walls too thick for shells: ")
    assert not any((ws / f).exists() for f in mixed_run.FILES)
    assert cited[0]["decided"] == "no mixed mesh" and cited[0]["words"].startswith("No shell-and-solid mesh was made")


def test_the_downloads_offer_the_mixed_files_nastran_first():
    from meshpipeline.engines.fea_writers import EXPORT_ARTIFACTS
    from meshpipeline.engines.registry import get_spec

    files = [f for _k, f, _l in EXPORT_ARTIFACTS]
    mixed = [f for f in files if f.startswith("mixed")]
    assert mixed == ["mixed.bdf", "mixed.inp", "mixed_abaqus.inp", "mixed.cdb"]
    assert set(mixed) == set(mixed_run.FILES) and files.index("mixed.bdf") < files.index("mesh.inp")
    members = {m.path for m in get_spec("gmsh").deliverable.members}
    assert set(mixed_run.FILES) | {mixed_run.RECORD} <= members
