"""The review must open the mesh, not the blank fields written beside it.

A live conjugate run delivered a mesh that passed every gate and then reviewed as inconclusive,
because `foamToVTK -constant` exported TWO times: `constant`, which is the mesh, and `0`, which
`solver_case` writes with <<HEXERA_FILL_THIS_IN>> where every physical value belongs. It died on
the blank field, leaving a half-written time directory - last .vtp truncated mid-base64, no .vtm
beside it - and the review picked that one as "the newest time". pv.read raised "Error parsing
input file", the patch was skipped, and the coolant region vanished from a review of a conjugate
mesh.

Measured in the mesh image, 2026-10-29, on a 2x2x2 blockMesh with one volScalarField at time 0:

    -constant            readable field    rc 0    tc_0  tc_0.vtm  tc_1  tc_1.vtm
    -constant -noZero    readable field    rc 0    tc_0  tc_0.vtm
    -constant            BLANK field       rc 1    tc_0  tc_0.vtm  tc_1        <- no .vtm
    -constant -noZero    BLANK field       rc 0    tc_0  tc_0.vtm

Two things follow, and this file holds both. The export does not ask for the field time, and the
reader does not believe a time directory that has no .vtm.
"""
from __future__ import annotations

import inspect
import logging

import pytest

from meshpipeline.engines.cfmesh_conjugate import foam_exec as FE
from meshpipeline.engines.cfmesh_conjugate import review_surface as RS

#: The two fluid times a real run left on disk, with their real sizes. `_0` is `constant` and
#: complete; `_1` is `0` and stops mid-base64.
CASE = "cr_ws_tghpvgh4"
COMPLETE, TRUNCATED = f"{CASE}_0", f"{CASE}_1"
FLUID_PATCHES = ("fluid_to_solid", "inlet", "outlet")


def _lay_out_the_run(root, *, with_truncated=True):
    """The exact tree the delivered workspace had: VTK/fluid/{_0 + _0.vtm, _1 (no .vtm)}."""
    region = root / "VTK" / "fluid"
    for time_dir, has_vtm in ((COMPLETE, True), (TRUNCATED, False)):
        if time_dir == TRUNCATED and not with_truncated:
            continue
        boundary = region / time_dir / "boundary"
        boundary.mkdir(parents=True)
        for patch in FLUID_PATCHES:
            (boundary / f"{patch}.vtp").write_text("<VTKFile>")
        (region / time_dir / "internal.vtu").write_text("<VTKFile>")
        if has_vtm:
            (region / f"{time_dir}.vtm").write_text("<VTKFile type='vtkMultiBlockDataSet'>")
    return region


def test_the_export_does_not_ask_for_the_field_time():
    """-noZero, because the blank fields at time 0 are what killed the export."""
    assert FE.NO_FIELD_TIME == "-noZero"
    src = inspect.getsource(FE.export_volume_vtk)
    assert "NO_FIELD_TIME" in src, "the flag has to be on the command, not only named"
    assert "-constant" in src


def test_a_time_directory_without_its_vtm_is_not_finished(tmp_path):
    """foamToVTK writes <case>_<n>.vtm only once that time is fully written."""
    region = _lay_out_the_run(tmp_path)
    assert FE.finished_export(region / COMPLETE)
    assert not FE.finished_export(region / TRUNCATED)


def test_a_missing_directory_is_not_finished_either(tmp_path):
    """A .vtm with no directory beside it is not an export, and neither is nothing at all."""
    assert not FE.finished_export(tmp_path / "nope_0")
    (tmp_path / "ghost_0.vtm").write_text("x")
    assert not FE.finished_export(tmp_path / "ghost_0")


def test_the_reader_skips_the_truncated_newest_and_takes_the_mesh(tmp_path):
    """THE BUG. _1 sorts after _0 and is the one that cannot be parsed."""
    region = _lay_out_the_run(tmp_path)
    assert RS._newest_time(region) == region / COMPLETE


def test_with_nothing_finished_the_rule_gets_out_of_the_way(tmp_path):
    """A PREFERENCE, NOT A PRECONDITION. The .vtm breaks the tie between a complete export and a
    half-written one; made a requirement it would do the opposite of its job, because a readable
    export whose marker is missing for any reason would leave the review with no surface where
    today it has one. Two existing tests build exactly that tree and were right to fail the
    strict version.
    """
    region = tmp_path / "VTK" / "fluid"
    (region / TRUNCATED / "boundary").mkdir(parents=True)
    (region / TRUNCATED / "boundary" / "inlet.vtp").write_text("<VTKFile>")
    assert RS._newest_time(region) == region / TRUNCATED
    assert FE.prefer_finished([region / TRUNCATED]) == [region / TRUNCATED]


def test_one_finished_export_is_enough_to_rule_the_rest_out(tmp_path):
    """The tie-break itself: as soon as ONE export is complete, the incomplete ones are ignored.

    This is the delivered tree. Before the fix it returned TRUNCATED.
    """
    region = _lay_out_the_run(tmp_path)
    assert FE.prefer_finished([region / COMPLETE, region / TRUNCATED]) == [region / COMPLETE]


def test_the_newest_finished_time_still_wins(tmp_path):
    """The rule it replaces was right about everything except completeness - keep that part.

    _10 after _9, which is why the index is read numerically rather than as a string.
    """
    region = tmp_path / "VTK" / "fluid"
    for n in (9, 10):
        (region / f"{CASE}_{n}" / "boundary").mkdir(parents=True)
        (region / f"{CASE}_{n}.vtm").write_text("x")
    assert RS._newest_time(region) == region / f"{CASE}_10"


def test_the_patches_come_from_the_mesh_time(tmp_path):
    """What the review actually opens: three fluid patches, from constant, none from time 0."""
    region = _lay_out_the_run(tmp_path)
    got = RS._patch_files(tmp_path, "fluid")
    assert [p.name for p in got] == ["fluid_to_solid.vtp", "inlet.vtp", "outlet.vtp"]
    assert all(p.parent.parent.name == COMPLETE for p in got), "time 0 is not the mesh"
    assert not any(p.parent.parent.name == TRUNCATED for p in got)


def test_the_volume_lookup_skips_an_unfinished_export():
    """The same guard on the file the reviewer slices, not only on the patches it draws."""
    src = inspect.getsource(FE.export_volume_vtk)
    assert "prefer_finished" in src, (
        "internal.vtu from a half-written time is as unparseable as the .vtp beside it")


def test_a_region_that_contributes_nothing_says_so_in_words(tmp_path, caplog):
    """The silent `continue` is why nothing in the log an operator reads said "fluid".

    EXERCISED, NOT GREPPED, AND NOT SELF-LOGGED. This asserted the source text of the message and
    then LOGGED THAT MESSAGE ITSELF before checking the log, so it would have passed on a function
    that logged nothing at all - and it passed through a 198-line rewrite of `build_region_msh`
    without executing a line of it. Now the real builder is run on a real workspace whose fluid
    export cannot be parsed, and the log is what the builder put there.
    """
    pytest.importorskip("pyvista")
    for region in ("fluid", "solid"):
        pm = tmp_path / "constant" / region / "polyMesh"
        pm.mkdir(parents=True)
        (pm / "boundary").write_text("0()")
        t = tmp_path / "VTK" / region / CASE
        (t / "boundary").mkdir(parents=True)
        for name in ("fluid_to_solid", "inlet", "outlet"):
            # not a .vtp: pv.read raises, _read_patch logs and returns None
            (t / "boundary" / f"{name}.vtp").write_text("not XML at all")
        (tmp_path / "VTK" / region / f"{CASE}.vtm").write_text("<VTKFile/>")

    with caplog.at_level(logging.ERROR, logger=RS.logger.name):
        groups, bbox = RS.build_region_msh(tmp_path)

    assert groups == {} and bbox is None
    said = caplog.text
    assert "contributed no readable surface" in said, said
    assert "'fluid'" in said and "3 boundary file(s)" in said, said
    assert "outlet.vtp" in said, "the files it tried have to be named"


def test_the_two_fixes_are_not_the_same_fix():
    """NO_FIELD_TIME stops the bad export; finished_export stops it being believed.

    Either alone leaves a real hole. Without the flag, any foam utility run by hand in the
    delivered case writes the same truncated directory again. Without the check, anything else
    that kills an export mid-write - a timeout, a full disk, a SIGKILL - puts a half-written time
    in front of the reader and it reads as the mesh.
    """
    assert "{NO_FIELD_TIME}" in inspect.getsource(FE.export_volume_vtk), (
        "the flag goes on the command foamToVTK is given")
    assert "prefer_finished" in inspect.getsource(RS._newest_time)
