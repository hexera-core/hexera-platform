# Responsibility: Hold the conjugate volume export's decided policy on the SUCCESS path - who
# pays for the second copy of every cell in time, in disk and in archive - and prove the half
# nothing reads is given back.
"""WHAT THE EVIDENCE ACTUALLY SAYS, read fairly on 2026-11-12.

`native._review_aids` skips the export only when `refused and cells > VOLUME_SKIP_ABOVE_CELLS`,
so on the success path a clean 270 M-cell mesh gets the full `foamToVTK -allRegions` - an
internal.vtu per region, a second copy of every cell. Two recorded attempts looked like a case
against that and are not:

  * 336,666,582 cells, killed by hand to stop it taking the box with it - but that mesh had
    ALREADY been refused by its own coolant_pieces gate, which is exactly what the skip catches;
  * 102,630,992 cells, gave up at 301 s leaving both VTK directories empty - against a 300 s
    ceiling since raised 24x (foam_exec.EXPORT_TIMEOUT_S).

Neither is evidence that a clean export of this size fails. And the reviewer NEEDS it: this engine
declares `review_requires_the_interior=True`, and the required surface is converted from the same
export's boundary tree. So the export stays and what is bounded is the time, the disk, and the
copy nobody reads.

Everything here EXERCISES the policy. The test that used to guard this feature asserted on the
source text of the condition, which is why it survived every change to what the condition means.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.adapters.mesh_execution import gce_vm as V
from meshpipeline.engines.cfmesh_conjugate import budget as B
from meshpipeline.engines.cfmesh_conjugate import native as N


def _exported(ws: Path, *, regions=("fluid", "solid"), mb: int = 1) -> None:
    """A finished `foamToVTK -allRegions` tree: a boundary patch and an internal.vtu per region."""
    for region in regions:
        time_dir = ws / "VTK" / region / "case_0"
        (time_dir / "boundary").mkdir(parents=True, exist_ok=True)
        (time_dir / "boundary" / "walls.vtp").write_bytes(b"\0" * 16)
        (time_dir / "internal.vtu").write_bytes(b"\0" * (mb * 1024 * 1024))


@pytest.fixture
def no_surface(monkeypatch):
    """The surface builder stubbed out: these tests are about the volume."""
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.review_surface.build_region_msh",
        lambda ws, **k: ({}, None))


# #
# the success path exports, at any size, and says why
# #
def test_a_clean_mesh_of_any_size_still_exports(tmp_path, monkeypatch, no_surface):
    seen: list = []

    def fake(*a, **k):
        seen.append(k)
        _exported(tmp_path)
        return "VTK/fluid/case_0/internal.vtu"

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk", fake)
    out = N._review_aids(tmp_path, bashrc="/dev/null", refused=False, cells=267_095_428)
    assert seen, "the export has to run on a mesh that passed"
    assert out["volume"] == "VTK/fluid/case_0/internal.vtu"
    assert "volume_skipped" not in out


def test_the_reason_rides_home_in_the_report(tmp_path, monkeypatch, no_surface):
    """A reader of a run must never have to infer which of these happened."""
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: None)
    out = N._review_aids(tmp_path, bashrc="/dev/null", refused=False, cells=267_095_428)
    assert out["volume_policy"] == N.VOLUME_ALWAYS_ON_SUCCESS
    assert "review inspects the interior" in out["volume_policy"]


def test_a_small_refusal_says_it_is_a_small_refusal(tmp_path, monkeypatch, no_surface):
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: None)
    out = N._review_aids(tmp_path, bashrc="/dev/null", refused=True, cells=1_000_000)
    assert "1,000,000" in out["volume_policy"]
    assert "refused" in out["volume_policy"]


# #
# TIME: the export gets what is left of the run's budget, not its own ceiling on top of it
# #
def test_the_export_is_bounded_by_what_is_left_of_the_run(tmp_path, monkeypatch, no_surface):
    seen: list = []
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: seen.append(k.get("timeout")))
    N._review_aids(tmp_path, bashrc="/dev/null", cells=1, budget_left_s=120.0)
    assert seen == [120.0]


def test_a_run_with_time_to_spare_keeps_the_export_s_own_ceiling(tmp_path, monkeypatch,
                                                                 no_surface):
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import EXPORT_TIMEOUT_S

    seen: list = []
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: seen.append(k.get("timeout")))
    N._review_aids(tmp_path, bashrc="/dev/null", cells=1, budget_left_s=99_999.0)
    assert seen == [EXPORT_TIMEOUT_S]


def test_a_spent_budget_does_not_start_the_export_and_says_which_it_was(tmp_path, monkeypatch,
                                                                       no_surface):
    """Told as the resource it is: the mesh is on disk and the run's TIME ran out, which a reader
    has to be able to tell apart from a foamToVTK that broke."""
    seen: list = []
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: seen.append(k))
    out = N._review_aids(tmp_path, bashrc="/dev/null", cells=1, budget_left_s=0.0)
    assert seen == []
    assert "budget was spent" in out["volume_skipped"]
    assert out["why"] == out["volume_skipped"]


def test_no_budget_given_means_unbounded_by_the_run(tmp_path, monkeypatch, no_surface):
    """Every caller outside the pipeline - the CLI and the tests - keeps what it had."""
    from meshpipeline.engines.cfmesh_conjugate.foam_exec import EXPORT_TIMEOUT_S

    seen: list = []
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: seen.append(k.get("timeout")))
    N._review_aids(tmp_path, bashrc="/dev/null", cells=1)
    assert seen == [EXPORT_TIMEOUT_S]


# #
# the copy nothing reads is given back
# #
def test_the_metals_cells_are_dropped_and_the_coolants_are_kept(tmp_path, monkeypatch,
                                                                no_surface):
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: (_exported(tmp_path, mb=2),
                         "VTK/fluid/case_0/internal.vtu")[1])
    out = N._review_aids(tmp_path, bashrc="/dev/null", cells=1)

    assert (tmp_path / "VTK" / "fluid" / "case_0" / "internal.vtu").is_file()
    assert not (tmp_path / "VTK" / "solid" / "case_0" / "internal.vtu").exists()
    assert out["volume_dropped_bytes"] == 2 * 1024 * 1024
    assert "dropped" in out["volume_dropped"]


def test_the_metals_boundary_survives_because_the_surface_needs_it(tmp_path, monkeypatch,
                                                                  no_surface):
    """`build_region_msh` reads BOTH regions' boundaries and refuses to publish half a surface."""
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: (_exported(tmp_path), "VTK/fluid/case_0/internal.vtu")[1])
    N._review_aids(tmp_path, bashrc="/dev/null", cells=1)
    assert (tmp_path / "VTK" / "solid" / "case_0" / "boundary" / "walls.vtp").is_file()


def test_nothing_is_dropped_when_no_volume_was_written(tmp_path, monkeypatch, no_surface):
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: None)
    out = N._review_aids(tmp_path, bashrc="/dev/null", cells=1)
    assert "volume_dropped" not in out


def test_the_metals_cells_are_given_back_even_when_the_export_WAS_KILLED_PARTWAY(tmp_path,
                                                                                monkeypatch,
                                                                                no_surface):
    """THE ONE CASE WHERE THE DISK IS MOST AT RISK, and the one that used to reclaim nothing.

    `export_volume_vtk` returns None when `run_guarded` raises on a timeout, and
    `newest()`/`prefer_finished` then reject an unmarked time directory - so the volume path is
    FALSY exactly when foamToVTK was killed partway, which is the moment there is most unreadable
    .vtu on the disk. The drop sat inside `if out["volume"]:`, so at the plate's size up to about
    62 GiB of it survived, went to `return_scope`, and was compressed byte by byte to decide
    whether the archive fits.

    The fixture is that shape: files on disk from a part-written export, and a None return.
    """
    def killed_partway(*_a, **_k):
        _exported(tmp_path, mb=3)        # what foamToVTK had already written
        return None                      # ...and then it was stopped

    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk", killed_partway)
    out = N._review_aids(tmp_path, bashrc="/dev/null", cells=1)

    assert out["volume"] is None, "the export did not finish, so there is no artifact"
    assert not (tmp_path / "VTK" / "solid" / "case_0" / "internal.vtu").exists()
    assert out["volume_dropped_bytes"] == 3 * 1024 * 1024
    # the coolant's half stays: a later run or a reader may still want what landed
    assert (tmp_path / "VTK" / "fluid" / "case_0" / "internal.vtu").is_file()


def test_the_region_dropped_is_the_one_nothing_reads():
    """`review_volume` returns the FLUID region's cells by name and only ever the fluid's, so the
    region given back must be the other one."""
    from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION

    assert N.UNREAD_VOLUME_REGION != FLUID_REGION


def test_return_scope_would_have_dropped_the_same_file_later():
    """This is not a new policy, it is the same one made earlier and on the disk rather than on
    the archive."""
    from meshpipeline.engines.return_scope import OPTIONAL_GLOBS

    assert OPTIONAL_GLOBS == ("VTK/**/internal.vtu",)


def test_the_free_disk_is_recorded_before_the_export(tmp_path, monkeypatch, no_surface):
    """`export_volume_vtk` never looks at foamToVTK's exit code - it globs for what landed - so an
    export stopped by ENOSPC and one stopped by anything else arrive looking identical. Running
    out of disk is what forced the 336 M-cell export to be killed by hand."""
    monkeypatch.setattr(
        "meshpipeline.engines.cfmesh_conjugate.foam_exec.export_volume_vtk",
        lambda *a, **k: None)
    out = N._review_aids(tmp_path, bashrc="/dev/null", cells=1)
    assert out["disk_free_bytes_before_export"] > 0


# #
# DISK: priced for what actually gets written
# #
def test_the_disk_holds_the_mesh_and_everything_written_beside_it():
    """Worked out at the Corintis plate's margined 333,869,285 cells (gce_vm's own table): the
    mesh is 565 GiB by the engine's own figure and what is written beside it - foamToVTK's second
    copy, the boundary .vtp tree, checkMesh's fields, the review msh and the archive - comes to
    about 670 GiB all told."""
    cells = 333_869_285
    workspace_gib = B.written_gib(cells)
    assert 560 < workspace_gib < 570, workspace_gib
    disk_gib = V.disk_gb_for(workspace_gib) * 1000.0**3 / 2.0**30
    assert disk_gib > 670 * 1.5, (disk_gib, "no headroom over what the run writes")


def test_the_disk_price_is_a_function_of_the_workspace():
    assert V.disk_gb_for(0.0) == V.MIN_DISK_GB
    assert V.disk_gb_for(1_000.0) == 1_000 * V.DISK_PER_WORKSPACE_GIB + V.DISK_FIXED_GB
    # monotone, so a bigger mesh never asks for a smaller disk
    sizes = [V.disk_gb_for(g) for g in (0, 10, 100, 1_000)]
    assert sizes == sorted(sizes)


def test_the_vm_body_carries_the_priced_disk():
    body = V.instance_body(name="n", zone="z", machine="n2-highmem-8", image="i", account="",
                           input_uri="gs://i", output_uri="gs://o", engine="cfmesh_conjugate",
                           timeout=600, disk_gb=V.disk_gb_for(565.2))
    got = body["disks"][0]["initializeParams"]["diskSizeGb"]
    assert got == str(V.disk_gb_for(565.2))
    assert int(got) > 2_000
