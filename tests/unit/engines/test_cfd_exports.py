# Responsibility: Verify a flow mesh's solver files (engines/cfd_exports.py): the three downloads and their labels, how each is written beside the mesh and checked, that a file is offered only with the very mesh it was written from, that a big one stays in the exchange bucket and one too big to send home is left out, and that each becomes its own download in the order the menu shows.
# Boundaries: OpenFOAM's converters are stood in for by two small scripts on PATH, so this runs anywhere; the real converters and independent readers are run on real meshes by the lab (~/cfdexport_lab/verify.py).
from __future__ import annotations

import json
import os
import stat
import uuid
import zipfile
from pathlib import Path

import pytest

from meshpipeline.engines import cfd_exports as CE

_OWNER = """FoamFile
{{
    version     2.0;
    format      ascii;
    class       labelList;
    note        "nPoints:{p}  nCells:{c}  nFaces:{f}  nInternalFaces:{i}";
    location    "constant/polyMesh";
    object      owner;
}}
0
"""
_BOUNDARY = """FoamFile { version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
2
(
    inlet { type patch; nFaces 4; startFace 20; }
    walls { type wall; nFaces 10; startFace 24; }
)
"""


def _case(root: Path, *, points=12, cells=2, faces=34, internal=20) -> Path:
    pm = root / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text(_OWNER.format(p=points, c=cells, f=faces, i=internal))
    for n in ("points", "faces", "neighbour"):
        (pm / n).write_text(f"{n}\n")
    (pm / "boundary").write_text(_BOUNDARY)
    (root / "constant" / "triSurface").mkdir()
    (root / "constant" / "triSurface" / "body.stl").write_text("solid body\nendsolid body\n")
    (root / "system").mkdir()
    (root / "system" / "controlDict").write_text("application none;\n")
    return root


def _tool(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_foam(tmp_path) -> str:
    """A bashrc whose foamMeshToFluent writes a header declaring the case's counts (FLUENT_POINTS etc.
    override them) and whose foamToEnsight writes a case file and its geometry - as the real ones lay
    them out."""
    bin_dir = tmp_path / "fakefoam"
    bin_dir.mkdir()
    _tool(bin_dir, "foamMeshToFluent", r"""
mkdir -p fluentInterface
c=$(basename "$PWD")
printf '(0 "foamMeshToFluent")\n(2 3)\n(10 (0 1 %x 0 3))\n(13 (0 1 %x 0 0))\n(12 (0 1 %x 0 0))\n' \
  "${FLUENT_POINTS:-12}" "${FLUENT_FACES:-34}" "${FLUENT_CELLS:-2}" > "fluentInterface/$c.msh"
printf '(12 (1 1 2 1 0)(\n 7 7\n))\n(39 (1 fluid fluid-1)())\n(39 (2 interior interior-1)())\n' >> "fluentInterface/$c.msh"
printf '(39 (10 pressure-outlet inlet)())\n(39 (11 wall walls)())\n' >> "fluentInterface/$c.msh"
""")
    _tool(bin_dir, "foamToEnsight", r"""
c=$(basename "$PWD")
mkdir -p EnSight/data/constant
printf 'FORMAT\ntype: ensight gold\nGEOMETRY\nmodel: data/constant/geometry\n' > "EnSight/$c.case"
printf 'geometry' > EnSight/data/constant/geometry
""")
    rc = tmp_path / "bashrc"
    rc.write_text(f"export PATH={bin_dir}:$PATH\n")
    return str(rc)


def test_three_downloads_openfoam_first_and_the_converted_ones_say_they_are_untested():
    assert [k for k, _f, _l in CE.EXPORT_ARTIFACTS] == ["export_openfoam_case", "export_fluent", "export_ensight"]
    labels = {k: lab for k, _f, lab in CE.EXPORT_ARTIFACTS}
    assert labels["export_openfoam_case"] == "OpenFOAM case (.zip)"
    # the result screen shows the part after " - " as an "untested" tag (ui/js/viewer/result_panel.fileLabel)
    assert labels["export_fluent"] == "Ansys Fluent mesh (.msh) - not yet tested in the real solver"
    assert labels["export_ensight"].startswith("EnSight case (.case) - not yet tested")
    from meshpipeline.engines.bucket_files import FLOW_EXPORTS
    assert tuple(FLOW_EXPORTS) == CE.FILES


def test_the_counts_come_from_the_owner_note_and_the_fluent_header(tmp_path):
    pm = _case(tmp_path / "c", points=3_400_001, cells=3_190_000, faces=9_800_002, internal=9_400_000)
    assert CE.polymesh_counts(pm / "constant" / "polyMesh") == {
        "points": 3_400_001, "cells": 3_190_000, "faces": 9_800_002, "internal_faces": 9_400_000}
    msh = tmp_path / "m.msh"
    msh.write_text('(0 "x")\n(2 3)\n(10 (0 1 33e141 0 3))\n(13 (0 1 958a82 0 0))\n(12 (0 1 30ad30 0 0))\n'
                   "(10 (5 1 33e141 1 3)(\n")
    assert CE.fluent_counts(msh) == {"points": 0x33E141, "faces": 0x958A82, "cells": 0x30AD30}


def test_a_zone_name_fluent_cannot_read_is_made_safe_and_the_rest_of_the_file_is_untouched(tmp_path):
    # the OpenFOAM motorBike's snappy regions ("motorBike_frame:016%16") stopped fluent3DMeshToFoam at
    # the "%" on a 3.2 M-cell mesh (lab, 2026-11-14)
    body = b'(0 "x")\n(10 (0 1 3 0 3))\n' + b"(10 (1 1 3 1 3)(\n" + b"1 2 3\n" * 3 + b"))\n"
    names = (b"(39 (1 fluid fluid-1)())\n(39 (2 interior interior-1)())\n(39 (10 wall motorBike_frame:016%16)())\n"
             b"(39 (11 wall motorBike_frame:016_16)())\n(39 (12 symmetry front)())\n")
    msh = tmp_path / "mesh.msh"
    msh.write_bytes(body + names)
    n, renamed = CE._zip_fluent(msh, tmp_path / "f.zip", "mesh.msh")
    with zipfile.ZipFile(tmp_path / "f.zip") as zf:
        out = zf.read("mesh.msh")
    assert out.startswith(body) and n == (tmp_path / "f.zip").stat().st_size
    # the name that was already safe keeps it; the unsafe one takes the next free spelling
    assert b"(39 (10 wall motorBike_frame:016_16_2)())" in out and b"(39 (11 wall motorBike_frame:016_16)())" in out
    assert b"(39 (12 symmetry front)())" in out and b"%" not in out
    assert renamed == {"motorBike_frame:016%16": "motorBike_frame:016_16_2"}
    assert CE.fluent_zone_name("a b/c") == "a_b_c" and CE.fluent_zone_name("wall:001-x.y") == "wall:001-x.y"


def test_a_mesh_without_the_owner_note_is_counted_from_its_lists(tmp_path):
    # cfMesh writes its polyMesh with no note in the owner header (and a neighbour entry for every
    # face, -1 on the boundary): the Fluent check went without counts and dropped a good file (lab, 2026-11-14)
    import numpy as np
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    hdr = "FoamFile\n{{\n    version 2.0;\n    format {fmt};\n    class labelList;\n    object {obj};\n}}\n// * * //\n\n"
    (pm / "points").write_text(hdr.format(fmt="ascii", obj="points") + "4\n(\n(0 0 0)\n(1 0 0)\n(0 1 0)\n(0 0 1)\n)\n")
    (pm / "owner").write_text(hdr.format(fmt="ascii", obj="owner") + "5\n(\n0\n0\n1\n1\n2\n)\n")
    nb = np.array([1, 2, -1, -1, -1], dtype=np.int32).tobytes()
    (pm / "neighbour").write_bytes(hdr.format(fmt="binary", obj="neighbour").encode() + b"5\n(" + nb + b")\n")
    assert CE.polymesh_counts(pm) == {"points": 4, "faces": 5, "cells": 3, "internal_faces": 2}


def test_a_single_region_mesh_gets_all_three_files(tmp_path, fake_foam):
    ws = _case(tmp_path / "ws")
    rec = CE.write_exports(ws, bashrc=fake_foam)
    assert sorted(rec["files"]) == sorted(CE.FILES) and rec["left_out"] == {}
    with zipfile.ZipFile(ws / CE.OPENFOAM_ZIP) as zf:
        names = set(zf.namelist())
    assert {"openfoam_case/constant/polyMesh/owner", "openfoam_case/constant/polyMesh/boundary",
            "openfoam_case/constant/triSurface/body.stl", "openfoam_case/system/controlDict"} <= names
    with zipfile.ZipFile(ws / CE.FLUENT_ZIP) as zf:
        assert zf.namelist() == ["mesh.msh"]
    with zipfile.ZipFile(ws / CE.ENSIGHT_ZIP) as zf:
        assert "ensight/mesh.case" in zf.namelist()
    # nothing the converters wrote is left in the delivered case or the workspace
    assert not (ws / CE.SCRATCH).exists() and not (ws / "fluentInterface").exists()
    assert sorted(p.name for p in (ws / "constant" / "polyMesh").iterdir()) == [
        "boundary", "faces", "neighbour", "owner", "points"]
    assert json.loads((ws / CE.EXPORTS_FILE).read_text())["files"].keys() == rec["files"].keys()
    assert [k for k, _f, _l in CE.offered(ws)] == ["export_openfoam_case", "export_fluent", "export_ensight"]


def test_a_fluent_file_that_does_not_declare_the_mesh_s_counts_is_not_offered(tmp_path, fake_foam, monkeypatch):
    monkeypatch.setenv("FLUENT_CELLS", "3")
    ws = _case(tmp_path / "ws")
    rec = CE.write_exports(ws, bashrc=fake_foam)
    assert CE.FLUENT_ZIP not in rec["files"] and "polyMesh has" in rec["left_out"][CE.FLUENT_ZIP]
    assert not (ws / CE.FLUENT_ZIP).exists()
    assert [k for k, _f, _l in CE.offered(ws)] == ["export_openfoam_case", "export_ensight"]


def test_a_converter_that_fails_costs_only_its_own_file(tmp_path):
    rc = tmp_path / "bashrc"
    rc.write_text("foamMeshToFluent() { return 1; }\nfoamToEnsight() { return 1; }\n"
                  "export -f foamMeshToFluent foamToEnsight\n")
    ws = _case(tmp_path / "ws")
    rec = CE.write_exports(ws, bashrc=str(rc))
    assert list(rec["files"]) == [CE.OPENFOAM_ZIP]
    assert set(rec["left_out"]) == {CE.FLUENT_ZIP, CE.ENSIGHT_ZIP}


def test_vmtk_s_case_is_found_under_openfoam_case(tmp_path, fake_foam):
    ws = tmp_path / "ws"
    _case(ws / "openfoam_case")
    (ws / "mesh.vtu").write_text("vtu")
    assert CE.case_root(ws) == ws / "openfoam_case"
    rec = CE.write_exports(ws, bashrc=fake_foam)
    assert sorted(rec["files"]) == sorted(CE.FILES)


def test_a_multi_region_mesh_is_offered_as_its_openfoam_case_only(tmp_path, fake_foam):
    ws = tmp_path / "ws"
    for region in ("fluid", "chip"):
        _case(ws / "tmp" / region)
        (ws / "constant" / region).mkdir(parents=True, exist_ok=True)
        os.replace(ws / "tmp" / region / "constant" / "polyMesh", ws / "constant" / region / "polyMesh")
    (ws / "constant" / "regionProperties").write_text("regions (fluid (fluid) solid (chip));\n")
    (ws / "system").mkdir()
    (ws / "system" / "controlDict").write_text("application none;\n")
    rec = CE.write_exports(ws, bashrc=fake_foam)
    assert list(rec["files"]) == [CE.OPENFOAM_ZIP]
    assert "multi-region" in rec["left_out"][CE.FLUENT_ZIP]
    with zipfile.ZipFile(ws / CE.OPENFOAM_ZIP) as zf:
        names = set(zf.namelist())
    assert {"openfoam_case/constant/regionProperties", "openfoam_case/constant/fluid/polyMesh/owner",
            "openfoam_case/constant/chip/polyMesh/owner"} <= names
    assert [k for k, _f, _l in CE.offered(ws)] == ["export_openfoam_case"]


def test_files_written_for_another_mesh_are_never_offered(tmp_path, fake_foam):
    ws = _case(tmp_path / "ws")
    CE.write_exports(ws, bashrc=fake_foam)
    # an earlier pass's mesh put back (engines/snappy/drivers._restore_sound_mesh): another boundary
    (ws / "constant" / "polyMesh" / "boundary").write_text(_BOUNDARY.replace("nFaces 4", "nFaces 5"))
    assert CE.offered(ws) == []


def test_a_file_is_offered_only_as_the_size_it_was_written(tmp_path, fake_foam):
    from meshpipeline.engines.bucket_files import Kept
    ws = _case(tmp_path / "ws")
    rec = CE.write_exports(ws, bashrc=fake_foam)
    big = rec["files"][CE.FLUENT_ZIP]["bytes"]
    (ws / CE.FLUENT_ZIP).unlink()                         # sent on its own and kept in the bucket
    held = {CE.FLUENT_ZIP: Kept(rel=CE.FLUENT_ZIP, bucket="b", key="k", bytes=big, gz_bytes=1, gz_md5="", mode=0o644)}
    assert "export_fluent" in [k for k, _f, _l in CE.offered(ws, held)]
    other = {CE.FLUENT_ZIP: Kept(rel=CE.FLUENT_ZIP, bucket="b", key="k", bytes=big + 1, gz_bytes=1, gz_md5="", mode=0o644)}
    assert "export_fluent" not in [k for k, _f, _l in CE.offered(ws, other)]
    (ws / CE.ENSIGHT_ZIP).write_bytes(b"changed")
    assert "export_ensight" not in [k for k, _f, _l in CE.offered(ws)]


def test_the_hook_writes_only_for_a_flow_mesh_that_was_built_with_time_left(tmp_path, fake_foam):
    ws = _case(tmp_path / "ws")
    assert CE.after_run(ws, engine="gmsh", result={"rc": 0}, bashrc=fake_foam) is None
    assert not (ws / CE.OPENFOAM_ZIP).exists()
    assert CE.after_run(ws, engine="snappy", result={"rc": 0}, bashrc=fake_foam, left_s=3600)["files"]
    # the next pass failed: its predecessor's files go with it
    assert CE.after_run(ws, engine="snappy", result={"rc": 1}, bashrc=fake_foam) is None
    assert not any((ws / f).exists() for f in (*CE.FILES, CE.EXPORTS_FILE))
    CE.after_run(ws, engine="cfmesh", result={"rc": 0}, bashrc=fake_foam)
    # too little of the run's wall left: nothing is started, nothing stale stays
    assert CE.after_run(ws, engine="cfmesh", result={"rc": 0}, bashrc=fake_foam, left_s=30) is None
    assert not (ws / CE.OPENFOAM_ZIP).exists()


def test_the_zips_stop_at_the_exports_deadline_and_leave_no_half_written_file(tmp_path, fake_foam):
    # the converters ran under the deadline but the zips did not: a big case near the end of the run's
    # wall went on compressing past it, into the time that is the finished mesh's own way home
    ws = _case(tmp_path / "ws")
    (ws / "constant" / "triSurface" / "big.stl").write_bytes(os.urandom(96 * 2**20))
    t = __import__("time").monotonic()
    rec = CE.write_exports(ws, bashrc=fake_foam, guard_s=0.2)
    took = __import__("time").monotonic() - t
    assert CE.OPENFOAM_ZIP not in rec["files"] and "out of time" in rec["left_out"][CE.OPENFOAM_ZIP]
    # nothing after the deadline is started either, and nothing half written is left
    assert set(rec["left_out"]) == set(CE.FILES) and rec["files"] == {}
    assert not list(ws.glob("*.part")) and not any((ws / f).exists() for f in CE.FILES)
    assert took < 0.2 + 1.5, took
    assert CE.offered(ws) == []


def test_a_zip_past_its_deadline_is_abandoned_whole(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(os.urandom(1024))
    msh = tmp_path / "mesh.msh"
    msh.write_bytes(b'(0 "x")\n(39 (1 fluid fluid-1)())\n')
    past = __import__("time").monotonic() - 1.0
    with pytest.raises(CE.OutOfTime):
        CE._zip(tmp_path / "a.zip", [(src, "a.bin")], deadline=past)
    with pytest.raises(CE.OutOfTime):
        CE._zip_fluent(msh, tmp_path / "f.zip", "mesh.msh", deadline=past)
    assert not [p.name for p in tmp_path.iterdir() if p.suffix in (".zip", ".part")]
    # inside the deadline the zip is the same as before: every entry, deflated
    n = CE._zip(tmp_path / "a.zip", [(src, "a.bin")], deadline=past + 3600)
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        assert zf.read("a.bin") == src.read_bytes() and zf.getinfo("a.bin").compress_type == zipfile.ZIP_DEFLATED
    assert n == (tmp_path / "a.zip").stat().st_size


def test_the_time_the_exports_need_is_read_from_the_mesh_s_size(tmp_path, fake_foam, monkeypatch):
    # the lab's 3.2 M-cell motorBike (a 392 MB polyMesh) took 34 s quiet, 97 s busy for both converters
    # and the three zips: a flat two minutes is a guess, too much for a small mesh, too little for a big one
    assert CE.export_need_s(0) == CE.MIN_LEFT_S
    need_3m = CE.export_need_s(392e6)
    assert need_3m >= 2 * 97 + CE.MIN_LEFT_S and need_3m >= 2 * CE.export_need_s(0)
    assert CE.export_send_s(392e6) >= 97
    ws = _case(tmp_path / "ws")
    (ws / "constant" / "polyMesh" / "points").write_bytes(b"p" * 2_000_000)      # a 2 MB polyMesh
    mesh = CE.mesh_bytes(ws)
    assert 2_000_000 <= mesh <= 2_010_000
    seen = {}

    def _write(workspace, *, bashrc, guard_s):
        seen["guard_s"] = guard_s
        return {"files": {}}
    monkeypatch.setattr(CE, "write_exports", _write)
    # just enough for the exports AND their own way home: started, bounded to what is left less that way home
    left = CE.export_need_s(mesh) + CE.export_send_s(mesh) + 1
    assert CE.after_run(ws, engine="snappy", result={"rc": 0}, bashrc=fake_foam, left_s=left) == {"files": {}}
    assert abs(seen.pop("guard_s") - (left - CE.export_send_s(mesh))) < 1e-6
    # a little less: not started, and nothing stale stays
    (ws / CE.OPENFOAM_ZIP).write_bytes(b"stale")
    assert CE.after_run(ws, engine="snappy", result={"rc": 0}, bashrc=fake_foam, left_s=left - 2) is None
    assert "guard_s" not in seen and not (ws / CE.OPENFOAM_ZIP).exists()
    # a mesh too big for the stuck-converter guard is given what it needs, not cut at the guard
    monkeypatch.setattr(CE, "BUSY_S_PER_MB", 1000.0)
    assert CE.export_need_s(mesh) > CE.GUARD_S
    CE.after_run(ws, engine="snappy", result={"rc": 0}, bashrc=fake_foam, left_s=None)
    assert seen["guard_s"] >= CE.export_need_s(mesh)


def test_every_engine_run_writes_its_exports_before_it_goes_home(tmp_path, monkeypatch, fake_foam):
    import meshpipeline.engines.cfmesh.foam_exec as FX
    import meshpipeline.engines.dispatch as D
    ws = _case(tmp_path / "ws")
    seen = {}

    def _runner(workspace, *, bashrc, timeout):
        return {"rc": 0, "timed_out": False}

    def _hook(workspace, *, engine, result, bashrc, left_s=None):
        seen.update(engine=engine, left_s=left_s)
        return {"files": {"x": {}}}
    monkeypatch.setattr(D, "engine_runners", lambda: {"netgen": _runner})
    monkeypatch.setattr(CE, "after_run", _hook)
    monkeypatch.setattr(FX, "_DEFAULT_BASHRC", fake_foam)
    out = D.run_engine_local(ws, engine="netgen", timeout=600)
    assert out["cfd_exports"] == {"files": {"x": {}}}
    assert seen["engine"] == "netgen" and 590 < seen["left_s"] <= 600


def test_a_big_export_stays_in_the_bucket_and_one_too_big_to_send_home_is_left_out(tmp_path, monkeypatch):
    import meshpipeline.engines.bucket_files as BF
    import meshpipeline.engines.return_scope as RS
    ws = _case(tmp_path / "ws")
    (ws / CE.FLUENT_ZIP).write_bytes(os.urandom(4096))
    (ws / "VTK" / "c_0").mkdir(parents=True)
    (ws / "VTK" / "c_0" / "internal.vtu").write_bytes(os.urandom(4096))
    monkeypatch.setattr(BF, "KEEP_FROM_BYTES", 1024)
    assert ws / CE.FLUENT_ZIP in BF.keepable(ws)
    (ws / CE.ENSIGHT_ZIP).write_bytes(os.urandom(2048))
    rec = RS.scope_return(ws, ceiling=4096)
    # the volume export goes first, then the solver files; the mesh never
    assert [x["file"] for x in rec["left_out"]][:2] == ["VTK/c_0/internal.vtu", CE.ENSIGHT_ZIP]
    assert "solver files" in rec["note"] and (ws / "constant" / "polyMesh" / "owner").exists()


def test_each_file_is_its_own_download_and_the_viewer_s_surface_is_not_one(tmp_path, fake_foam):
    from meshpipeline.application.artifact_uploader import cfd_export_plan
    ws = _case(tmp_path / "ws")
    CE.write_exports(ws, bashrc=fake_foam)
    job = uuid.uuid4()
    plan = cfd_export_plan(job, ws)
    assert [p.logical_key for p in plan] == ["export_openfoam_case", "export_fluent", "export_ensight"]
    assert all(not p.required and p.content_type == "application/zip"
               and p.object_key.startswith(f"jobs/{job}/exports/") for p in plan)
    (ws / "fea_plan.json").write_text("{}")
    assert cfd_export_plan(job, ws) == []


def test_the_delivery_offers_the_solver_files_of_the_mesh_delivered_and_no_surface_preview(tmp_path, fake_foam):
    import tarfile

    from tests.engine_workspaces import build_workspace

    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.engines.surface_deliverable import SURFACE_MSH
    from meshpipeline.persistence.models import ArtifactType

    ws = build_workspace(tmp_path, "snappy", include_optional=False)
    pm = ws / "constant" / "polyMesh"
    (pm / "owner").write_text(_OWNER.format(p=12, c=2, f=34, i=20))
    for n in ("points", "faces", "neighbour"):
        (pm / n).write_text(f"{n}\n")
    (pm / "boundary").write_text(_BOUNDARY)
    (ws / SURFACE_MSH).write_text("the boundary the viewer draws")
    CE.write_exports(ws, bashrc=fake_foam)

    def bundle_names(plan):
        b = next(p for p in plan if p.artifact_type is ArtifactType.mesh_bundle)
        with tarfile.open(b.local_path) as tf:
            return {n.split("/", 1)[1] for n in tf.getnames()}

    plan = au._build_plan(uuid.uuid4(), ws, [], "snappy")
    assert [p.logical_key for p in plan if p.artifact_type is ArtifactType.mesh] == [
        "export_openfoam_case", "export_fluent", "export_ensight"], "no Surface preview row for a flow mesh"
    # Everything carries the solver files too, as an FEA mesh's does (not the case zip: the case is in it)
    names = bundle_names(plan)
    assert {CE.FLUENT_ZIP, CE.ENSIGHT_ZIP} <= names and CE.OPENFOAM_ZIP not in names
    # an earlier pass's mesh put back: its files are offered nowhere, the bundle included
    (pm / "boundary").write_text(_BOUNDARY.replace("nFaces 10", "nFaces 11"))
    plan = au._build_plan(uuid.uuid4(), ws, [], "snappy")
    assert [p.logical_key for p in plan if p.artifact_type is ArtifactType.mesh] == []
    assert not ({CE.FLUENT_ZIP, CE.ENSIGHT_ZIP} & bundle_names(plan))
