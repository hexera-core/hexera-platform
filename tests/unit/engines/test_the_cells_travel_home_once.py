# Responsibility: Verify the mesher packs the customer's bundle itself, that only what the bundle
# carries is left behind, and that the one home-side reader of the per-rank trees still refuses a
# half-written region once they are gone.
# Boundaries: the packing, the record and the gate. Nothing here meshes and nothing here uploads.
#
# WHAT THIS IS FOR, measured on job 606af55e (2026-11-27) - the first run of this plate to pass every
# gate, at 2:15:39 of which the VM leg was 7,705 s. The mesher's own beacon in the exchange bucket
# (progress.json under this job's operation key f6e000d0f577ba...) is stamped 10:02:08 with
# "returning: sending the mesh back", and the home side logged "rc=0 timed_out=False" at 10:19:57:
#
#     6,636 s   VM created -> "returning"
#     1,069 s   "returning" -> the result collected at home
#
# The workspace that came home is 124 GB, of which 123 GB is the eight `processor*` trees at 15-16 GB
# each. Until 4558f0cc the delivered bundle held no cells at all, so packing it at home was cheap;
# with the cells in it the home side has to compress those 123 GB through a 4.0-CPU quota, which at
# the measured level-6 rate of 26.8 MB/s a thread is about nineteen minutes added to every run.
#
# THE ONE READER THAT HAD TO BE DEALT WITH is `gates.meshed_regions`, which proves a region's cells
# exist by looking for `processor*/constant/<region>/polyMesh/owner` on EVERY rank and which runs at
# home. Its guarantee is kept rather than weakened: the record is taken from the VERIFIED archive's
# own member names, and it is believed only while the bundle it describes is still there at its
# recorded size.
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from meshpipeline.engines import bundled_delivery as BD
from meshpipeline.engines.cfmesh_conjugate import gates as G
from meshpipeline.engines.registry import get_spec

SPEC = get_spec("cfmesh_conjugate")
REGIONS = ("fluid", "solid")
#: What OpenFOAM reads a mesh out of. `boundary` alone is a patch table, which is what used to ship.
CELLS = ("points", "faces", "owner", "neighbour")


def _case(root: Path, *, ranks: int = 8, cells_in_constant: bool = False,
          skip: tuple[str, ...] = ()) -> Path:
    """A conjugate workspace shaped like a real one: the dicts, the report, the plan, the patch
    tables and viewer sidecars - with the CELLS either in the per-rank trees (a decomposed delivery)
    or in `constant` (a merged one). `skip` names ranks that hold no cells for the solid region,
    which is the half-written state the gate exists to refuse.
    """
    (root / "system").mkdir(parents=True, exist_ok=True)
    (root / "system" / "fvSchemes").write_text("x")
    (root / "constant").mkdir(parents=True, exist_ok=True)
    (root / "constant" / "regionProperties").write_text("regions (fluid solid);")
    for r in REGIONS:
        pm = root / "constant" / r / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        (pm / "boundary").write_text("0()")              # the published patch table
        (pm / "boundaryIndex").mkdir(exist_ok=True)      # the viewer sidecar
        (pm / "boundaryIndex" / "meta.json").write_text("{}")
        if cells_in_constant:
            for f in CELLS:
                (pm / f).write_text("0()")
    for k in range(ranks):
        for r in REGIONS:
            if r == "solid" and f"processor{k}" in skip:
                continue
            pm = root / f"processor{k}" / "constant" / r / "polyMesh"
            pm.mkdir(parents=True, exist_ok=True)
            for f in CELLS:
                (pm / f).write_text("0()")
    (root / "conjugate_report.json").write_text("{}")
    (root / "conjugate_plan.json").write_text("{}")
    return root


# ---------------------------------------------------------------------------------------------
# THE PACKING


def test_the_mesher_packs_the_bundle_and_the_ranks_stay_behind(tmp_path):
    """THE CHANGE. After this the workspace carries the deliverable and not the cells twice."""
    ws = _case(tmp_path / "ws")
    before = sorted(p.name for p in ws.glob("processor*"))
    assert len(before) == 8, before

    rec = BD.build_bundle(ws, SPEC)
    assert rec is not None
    bundle = ws / SPEC.deliverable.bundle
    assert bundle.is_file() and bundle.stat().st_size == rec["bytes"]
    assert not list(ws.glob("*.part")), "a part file must never be left in the workspace"

    gone = BD.narrow(ws, SPEC)
    assert sorted(gone) == before, gone
    assert not list(ws.glob("processor*")), "the cells are in the bundle; they must not travel too"
    # AND EVERYTHING ELSE IS UNTOUCHED, which is what the home side still reads: the patch tables,
    # the viewer sidecars, the dicts, the report and the plan.
    for r in REGIONS:
        pm = ws / "constant" / r / "polyMesh"
        assert (pm / "boundary").is_file()
        assert (pm / "boundaryIndex" / "meta.json").is_file()
    assert (ws / "system" / "fvSchemes").is_file()
    assert (ws / "conjugate_report.json").is_file()


def test_the_bundle_the_mesher_wrote_carries_the_cells(tmp_path):
    """It is the deliverable and not a transport artifact, so it has to satisfy the engine's own
    content rule - which is `verify_archive`'s job and is run inside `build_bundle`."""
    ws = _case(tmp_path / "ws", ranks=3)
    BD.build_bundle(ws, SPEC)
    names = tarfile.open(ws / SPEC.deliverable.bundle).getnames()
    pre = SPEC.deliverable.prefix
    for k in range(3):
        for r in REGIONS:
            for f in CELLS:
                assert f"{pre}/processor{k}/constant/{r}/polyMesh/{f}" in names, (k, r, f)


def test_a_refused_build_leaves_nothing_behind_to_travel(tmp_path):
    """AN ECONOMY THAT CANNOT COMPLETE MUST COST NOTHING AT ALL.

    The caller treats a failed build as "send the workspace as it is", so anything left here
    TRAVELS - and the half-written archive is not nothing. MEASURED on the shared dispatch test:
    a refused build left a 428,873-byte `.part` in a workspace whose largest file had been 288,889,
    and `returned_bytes["largest"]` is the figure the extractor sizes itself from
    (`result_extraction_limits`). So a refusal moved the bound the delivery is checked against.
    """
    ws = _case(tmp_path / "ws", ranks=0)              # no cells anywhere: the build is refused
    before = {p.name for p in ws.iterdir()}
    with pytest.raises(Exception):
        BD.build_bundle(ws, SPEC)
    assert {p.name for p in ws.iterdir()} == before, (
        "the refused build left a file in a workspace that is about to be archived")
    assert not list(ws.glob("*.part")), list(ws.glob("*.part"))


def test_the_mesher_only_packs_for_an_engine_that_gains_by_it(tmp_path):
    """`dispatch.run_engine_local` IS SHARED BY EVERY ENGINE, and the other five gain nothing here:
    they would pay a second compression pass over the workspace and leave nothing behind for it.
    `test_the_return_watch_changes_nothing_else_a_non_conjugate_run_returns` holds that function to
    changing nothing about a cfmesh, gmsh or snappy_multiregion run, and it is right to.
    """
    from meshpipeline.engines.registry import ENGINE_CATALOG

    assert BD.worth_building(SPEC), "the engine this was measured on has to be in"
    named = [n for n, s in ENGINE_CATALOG.items()
             if s.implemented and s.deliverable and BD.worth_building(s)]
    assert named == ["cfmesh_conjugate"], (
        f"these engines would pack on the mesher without having been measured for it: {named}")


def test_a_bundle_with_no_cells_in_it_is_never_recorded_and_nothing_is_dropped(tmp_path):
    """THE ORDER IS THE SAFETY. `build_bundle` verifies before it records, and `narrow` drops
    nothing without a record - so the archive that job b8f09ae9 shipped (patch tables and viewer
    sidecars, zero cells) cannot become a reason to delete the cells it does not contain."""
    ws = _case(tmp_path / "ws", ranks=0)          # patch tables and sidecars only
    with pytest.raises(Exception) as e:
        BD.build_bundle(ws, SPEC)
    assert "no mesh inside it" in str(e.value), str(e.value)
    assert not BD.record_path(ws).exists()
    assert BD.narrow(ws, SPEC) == []
    assert BD.bundled_file(ws, SPEC) is None


def test_a_second_pass_neither_rebuilds_nor_re_drops(tmp_path):
    """A retry on the same workspace must not rebuild tens of gigabytes, and must not mistake an
    already-narrowed workspace for one whose cells went missing."""
    ws = _case(tmp_path / "ws", ranks=2)
    first = BD.build_bundle(ws, SPEC)
    BD.narrow(ws, SPEC)
    mtime = (ws / SPEC.deliverable.bundle).stat().st_mtime_ns
    again = BD.build_bundle(ws, SPEC)
    assert again == first
    assert (ws / SPEC.deliverable.bundle).stat().st_mtime_ns == mtime
    assert BD.narrow(ws, SPEC) == [], "there is nothing left to drop and nothing to complain about"


def test_an_engine_that_declares_nothing_packed_only_loses_nothing(tmp_path):
    """`narrow` is engine-declared. Every engine that has not said which of its members only the
    bundle needed keeps its whole workspace, which is every engine but this one."""
    ws = _case(tmp_path / "ws", ranks=2)
    BD.build_bundle(ws, SPEC)
    stripped = type(SPEC.deliverable)(
        **{**{f: getattr(SPEC.deliverable, f) for f in SPEC.deliverable.__dataclass_fields__},
           "packed_only": ()})
    assert BD.narrow(ws, type("S", (), {"name": SPEC.name, "deliverable": stripped})) == []
    assert len(list(ws.glob("processor*"))) == 2


# ---------------------------------------------------------------------------------------------
# THE RECORD, AND WHAT IT IS ALLOWED TO SPEAK FOR


def test_the_record_is_read_off_the_archive_and_not_off_the_directories(tmp_path):
    """Which is why it can be trusted after the directories are gone."""
    ws = _case(tmp_path / "ws", ranks=4)
    rec = BD.build_bundle(ws, SPEC)
    assert rec["cells"] == {r: [f"processor{k}" for k in range(4)] for r in REGIONS}, rec["cells"]
    BD.narrow(ws, SPEC)
    assert BD.cells_delivered(ws) == rec["cells"], "the record outlives the trees; that is the point"


def test_a_merged_delivery_records_one_rank(tmp_path):
    """No processor directories at all, cells in `constant` - the other real path."""
    ws = _case(tmp_path / "ws", ranks=0, cells_in_constant=True)
    rec = BD.build_bundle(ws, SPEC)
    assert rec["cells"] == {r: [""] for r in REGIONS}, rec["cells"]


def test_a_record_whose_bundle_is_gone_or_changed_speaks_for_nothing(tmp_path):
    """The record's only job is to speak for bytes that are no longer on this disk, so it is tied to
    them: the named file must be there, at the recorded size."""
    ws = _case(tmp_path / "ws", ranks=2)
    BD.build_bundle(ws, SPEC)
    BD.narrow(ws, SPEC)
    bundle = ws / SPEC.deliverable.bundle
    keep = bundle.read_bytes()

    bundle.unlink()
    assert BD.cells_delivered(ws) is None and BD.bundled_file(ws, SPEC) is None

    bundle.write_bytes(keep[:-64])
    assert BD.cells_delivered(ws) is None, "a truncated bundle is not the bundle that was recorded"

    bundle.write_bytes(keep)
    assert BD.cells_delivered(ws) is not None

    BD.record_path(ws).write_text("{not json")
    assert BD.cells_delivered(ws) is None


def test_the_record_cannot_name_a_file_outside_the_workspace(tmp_path):
    """It is read back from a file inside a workspace that travelled, so its `bundle` name is
    treated as data: a path rather than a name is refused."""
    ws = _case(tmp_path / "ws", ranks=1)
    BD.build_bundle(ws, SPEC)
    rec = json.loads(BD.record_path(ws).read_text())
    for bad in ("../outside.tar.gz", "sub/dir.tar.gz", "/etc/passwd"):
        BD.record_path(ws).write_text(json.dumps({**rec, "bundle": bad}))
        assert BD.bundle_record(ws) is None, bad


def test_a_bundle_from_another_engine_is_not_this_engine_s_deliverable(tmp_path):
    """Delivering it would hand a customer the wrong format under the right label."""
    ws = _case(tmp_path / "ws", ranks=1)
    BD.build_bundle(ws, SPEC)
    rec = json.loads(BD.record_path(ws).read_text())
    BD.record_path(ws).write_text(json.dumps({**rec, "engine": "gmsh"}))
    assert BD.bundled_file(ws, SPEC) is None


# ---------------------------------------------------------------------------------------------
# THE GATE, WHICH WAS THE ONLY HOME-SIDE READER OF THE TREES


def test_the_gate_still_names_both_regions_once_the_trees_are_gone(tmp_path):
    """The guarantee it makes is kept, from the bundle instead of from the directories."""
    ws = _case(tmp_path / "ws", ranks=8)
    assert G.meshed_regions(ws) == ["fluid", "solid"]
    BD.build_bundle(ws, SPEC)
    BD.narrow(ws, SPEC)
    assert not list(ws.glob("processor*"))
    assert G.meshed_regions(ws) == ["fluid", "solid"], (
        "with the cells in the bundle and the trees gone, the gate has to read the record")


def test_the_gate_still_refuses_a_region_missing_from_one_rank(tmp_path):
    """EVERY RANK, NOT ANY - the half-written-region failure, after narrowing.

    The solid region is absent from processor5 only. On the filesystem that is already refused; it
    must stay refused when the only evidence left is what the bundle carries, or narrowing would
    have bought speed by giving up the gate.
    """
    ws = _case(tmp_path / "ws", ranks=8, skip=("processor5",))
    assert G.meshed_regions(ws) == ["fluid"], "the filesystem check must refuse it to begin with"
    BD.build_bundle(ws, SPEC)
    BD.narrow(ws, SPEC)
    assert G.meshed_regions(ws) == ["fluid"], (
        "solid is in the bundle for seven of the eight ranks and must not read as meshed")


def test_the_gate_reads_the_filesystem_whenever_the_cells_are_on_it(tmp_path):
    """A workspace that still has its own cells is judged on them, and nothing about this moves:
    `cells_delivered` is None for every such workspace, so the record is never consulted."""
    ws = _case(tmp_path / "ws", ranks=8)
    BD.build_bundle(ws, SPEC)                      # a bundle exists, but the trees are still here
    assert BD.cells_delivered(ws) is not None
    (ws / "processor3" / "constant" / "solid" / "polyMesh" / "owner").unlink()
    assert G.meshed_regions(ws) == ["fluid"], (
        "the record says solid is on all eight ranks; the disk says otherwise and the disk wins")


def test_a_workspace_with_neither_trees_nor_a_record_is_refused(tmp_path):
    """Which is the state a lost or half-extracted return leaves, and it must not read as meshed."""
    ws = _case(tmp_path / "ws", ranks=0)
    assert G.meshed_regions(ws) == []


# ---------------------------------------------------------------------------------------------
# AND THE TWO DELIVERY PATHS HAVE TO BE ONE DELIVERY


def test_the_mesher_s_bundle_is_byte_identical_to_the_one_home_would_have_built(tmp_path):
    """THE EQUIVALENCE THIS RESTS ON. The bundle's checksum is load-bearing - `_norm` zeroes
    mtime/uid/gid precisely so a same-attempt retry is recognised as the same content rather than
    tripping the conflict guard - so moving WHERE it is packed may not change WHAT is packed.

    Both sides go through `artifact_uploader`'s own writer at `BUNDLE_ARCHIVE_LEVEL` over
    `Deliverable.expand`, which is why this holds; the test is here because "it is the same code"
    is a claim and this is the measurement of it.
    """
    import uuid

    from tests.engine_workspaces import build_workspace

    import meshpipeline.application.artifact_uploader as au

    home = build_workspace(tmp_path / "home", "cfmesh_conjugate")
    mesher = build_workspace(tmp_path / "mesher", "cfmesh_conjugate")

    at_home = next(p for p in au._build_plan(uuid.uuid4(), home, [], "cfmesh_conjugate")
                   if p.logical_key == "mesh_bundle")
    BD.build_bundle(mesher, SPEC)
    on_vm = mesher / SPEC.deliverable.bundle

    assert on_vm.read_bytes() == at_home.local_path.read_bytes(), (
        "the mesher's bundle differs from the one the uploader would have written")


def test_the_uploader_delivers_the_mesher_s_bundle_rather_than_packing_a_second_one(tmp_path):
    """And it is still verified here, by the same `verify_archive`: a file found in a workspace is
    evidence, not authority, and this is the side that answers to the customer."""
    import uuid

    from tests.engine_workspaces import build_workspace

    import meshpipeline.application.artifact_uploader as au

    ws = build_workspace(tmp_path, "cfmesh_conjugate")
    BD.build_bundle(ws, SPEC)
    BD.narrow(ws, SPEC)
    ready = ws / SPEC.deliverable.bundle
    stamp = (ready.stat().st_size, ready.stat().st_mtime_ns)

    plan = au._build_plan(uuid.uuid4(), ws, [], "cfmesh_conjugate")
    bundle = [p for p in plan if p.logical_key == "mesh_bundle"]
    assert len(bundle) == 1
    assert bundle[0].local_path == ready, "it planned a different file than the one already here"
    assert (ready.stat().st_size, ready.stat().st_mtime_ns) == stamp, "it repacked it anyway"
    assert not list(ws.glob("bundle_*.tar.gz")), "a second bundle was written beside the first"
    # THE VIEWER STILL TRAVELS ON THIS PATH. A mesh delivered without a view is the failure mode of
    # having two paths at all, which is why `_viewer_plan` is shared rather than copied.
    assert len([p for p in plan if p.logical_key == "viewer_data"]) == 1
    assert [p.logical_key for p in plan] == [
        p.logical_key for p in au._build_plan(uuid.uuid4(),
                                              build_workspace(tmp_path / "other",
                                                              "cfmesh_conjugate"),
                                              [], "cfmesh_conjugate")], (
        "the two paths must plan the same artifacts in the same order")


def test_a_bundle_the_record_does_not_vouch_for_is_packed_here_as_before(tmp_path):
    """A stray archive in a workspace is not a delivery. With no record - or a record that does not
    describe it - the uploader packs its own, which is every run before this change."""
    import uuid

    from tests.engine_workspaces import build_workspace

    import meshpipeline.application.artifact_uploader as au

    ws = build_workspace(tmp_path, "cfmesh_conjugate")
    (ws / SPEC.deliverable.bundle).write_bytes(b"not an archive at all")
    plan = au._build_plan(uuid.uuid4(), ws, [], "cfmesh_conjugate")
    bundle = next(p for p in plan if p.logical_key == "mesh_bundle")
    assert bundle.local_path.name.startswith("bundle_"), (
        f"it delivered the stray file instead of packing one: {bundle.local_path.name}")
    with tarfile.open(bundle.local_path) as tf:
        assert tf.getnames(), "and what it packed is a real archive"
