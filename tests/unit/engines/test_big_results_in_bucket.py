# Responsibility: Verify RUN-7 - a big result's delivery-only files stay in the exchange bucket, the worker reads light stand-ins, and every download streams from the bucket byte for byte.
from __future__ import annotations

import asyncio
import contextlib
import gzip
import io
import json
import os
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshpipeline.engines import bucket_files as BF

# ------------------------------------------------------------------ a bucket in memory


class _Bucket:
    """Objects by (bucket, key); writers and readers as the exchange hands them out."""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[str] = []

    def writer(self, bucket, key):
        store = self

        class _W(io.BytesIO):
            def close(self):
                store.objects[(bucket, key)] = self.getvalue()
                super().close()

        return _W()

    def reader(self, bucket, key):
        return io.BytesIO(self.objects[(bucket, key)])

    def delete(self, bucket, key):
        self.deleted.append(key)
        self.objects.pop((bucket, key), None)


def _inflate(obj: bytes) -> bytes:
    import zlib
    d = zlib.decompressobj(-15)
    return d.decompress(obj) + d.flush()


def _deflate(data: bytes) -> bytes:
    import zlib
    z = zlib.compressobj(1, zlib.DEFLATED, -15)
    return z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH)


@pytest.fixture
def small_files(monkeypatch):
    monkeypatch.setattr(BF, "KEEP_FROM_BYTES", 1000)
    monkeypatch.setattr(BF, "PACK_FROM_BYTES", 600)
    monkeypatch.setattr(BF, "PACK_PIECE_BYTES", 1024)      # many gzip members per file


def _flow_ws(root: Path) -> Path:
    ws = root / "flow"
    pm = ws / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "faces").write_text("FoamFile{}\n" + "4(0 1 2 3)\n" * 300)        # 3,311 bytes: fetched
    (pm / "owner").write_text("FoamFile{}\n" + "7\n" * 400)                  # 811: fetched
    (pm / "boundary").write_text("FoamFile{}\n(\nwall{type wall;}\n)\n")      # small: in the archive
    (pm / "faces").chmod(0o640)
    (ws / "system").mkdir()
    (ws / "system" / "meshDict").write_text("maxCellSize 1;\n")
    return ws


def test_a_flow_mesh_is_packed_on_the_mesher_and_fetched_by_the_worker(tmp_path, small_files):
    ws = _flow_ws(tmp_path)
    members = ["constant/polyMesh", "system/meshDict"]
    originals = {p.relative_to(ws).as_posix(): p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    expected = _tarfile_bundle(ws, members, "openfoam_case")
    bucket = _Bucket()
    rows = {k.rel: k for k in BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)}
    # a flow mesh's polyMesh is read by the worker's checks: sent on its own, fetched - never kept
    assert sorted(rows) == ["constant/polyMesh/faces", "constant/polyMesh/owner"]
    assert all(k.fetch for k in rows.values())
    assert not (ws / "constant/polyMesh/faces").exists()            # not in the archive
    assert not (ws / BF.VIEWER_MESH_FILE).exists()                   # nothing kept: no stand-ins
    # the worker: every fetched file back in its place, byte for byte, its mode kept
    assert BF.fetch(ws, bucket.reader) == sum(k.bytes for k in rows.values())
    for rel, data in originals.items():
        assert (ws / rel).read_bytes() == data
    assert (ws / "constant/polyMesh/faces").stat().st_mode & 0o777 == 0o640
    # the bundle: the fetched members pass through as the mesher packed them - the same tar
    entries = BF.bundle_entries(ws, members, "openfoam_case")
    assert [n for n, _p, k in entries if k is not None] == ["openfoam_case/constant/polyMesh/faces",
                                                            "openfoam_case/constant/polyMesh/owner"]
    assert gzip.decompress(b"".join(BF.bundle_chunks(entries, bucket.reader))) == expected
    # a fetched file the worker changed since is packed again here, never shipped stale
    (ws / "constant/polyMesh/owner").write_text("FoamFile{}\n" + "8\n" * 400)
    entries = BF.bundle_entries(ws, members, "openfoam_case")
    assert [n for n, _p, k in entries if k is not None] == ["openfoam_case/constant/polyMesh/faces"]
    assert gzip.decompress(b"".join(BF.bundle_chunks(entries, bucket.reader))) == \
        _tarfile_bundle(ws, members, "openfoam_case")


def _fetched_flow(tmp_path):
    ws = _flow_ws(tmp_path)
    bucket = _Bucket()
    BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)
    BF.fetch(ws, bucket.reader)
    return ws, bucket


def _passed_through(ws) -> list[str]:
    return [n for n, _p, k in BF.bundle_entries(ws, ["constant/polyMesh"], "c") if k is not None]


def test_a_same_size_rewrite_inside_one_clock_tick_is_never_shipped_stale(tmp_path, small_files, monkeypatch):
    """A coarse file-system clock (whole seconds on some; one kernel tick on a fast machine): every
    write, the fetch's stamp and the worker's rewrite land in the same tick, so the rewrite leaves
    size, times and inode exactly as the fetch stamped them. The content must decide."""
    tick = 1_700_000_000 * 1_000_000_000
    real = BF._stat

    def coarse(p):
        st = real(p)
        return SimpleNamespace(st_size=st.st_size, st_ino=st.st_ino, st_mtime_ns=tick, st_ctime_ns=tick)

    monkeypatch.setattr(BF, "_stat", coarse)
    monkeypatch.setattr(BF, "time", SimpleNamespace(time_ns=lambda: tick + 1000))
    ws, bucket = _fetched_flow(tmp_path)
    owner = ws / "constant/polyMesh/owner"
    before = BF._stamp(owner)
    owner.write_text("FoamFile{}\n" + "8\n" * 400)                                   # the same 811 bytes
    assert BF._stamp(owner) == before                      # the stamp alone cannot see the change
    assert _passed_through(ws) == ["c/constant/polyMesh/faces"]
    members = ["constant/polyMesh", "system/meshDict"]
    entries = BF.bundle_entries(ws, members, "c")
    assert gzip.decompress(b"".join(BF.bundle_chunks(entries, bucket.reader))) == _tarfile_bundle(ws, members, "c")


def test_a_file_stamped_long_after_its_last_write_is_judged_by_its_stamp_alone(tmp_path, small_files, monkeypatch):
    """A multi-GB file is written long before the fetch record is taken: its stamp is proof, and the
    bundle never reads it to check. A change since - even with its time set back - still shows."""
    ws, _bucket = _fetched_flow(tmp_path)
    rec = json.loads((ws / BF.FETCHED_FILE).read_text())
    rec["at"] += 60 * 1_000_000_000                       # as if stamped a minute after the writes
    (ws / BF.FETCHED_FILE).write_text(json.dumps(rec))

    def no_read(p):
        raise AssertionError(f"{p.name} was read to check it")

    monkeypatch.setattr(BF, "_tar_crc", no_read)
    assert _passed_through(ws) == ["c/constant/polyMesh/faces", "c/constant/polyMesh/owner"]
    owner = ws / "constant/polyMesh/owner"
    st = owner.stat()
    new = owner.with_name("owner.new")
    new.write_text("FoamFile{}\n" + "8\n" * 400)                                     # the same size,
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))                              # the same time,
    new.replace(owner)                                                              # swapped in
    assert _passed_through(ws) == ["c/constant/polyMesh/faces"]


def test_a_fetch_record_of_another_form_repacks_every_file(tmp_path, small_files):
    ws, _bucket = _fetched_flow(tmp_path)
    rec = json.loads((ws / BF.FETCHED_FILE).read_text())
    (ws / BF.FETCHED_FILE).write_text(json.dumps({r: s[:2] for r, s in rec["files"].items()}))  # the old form
    assert _passed_through(ws) == []


def test_a_short_fetch_is_refused_and_leaves_nothing_half_written(tmp_path, small_files):
    ws = _flow_ws(tmp_path)
    bucket = _Bucket()
    k = {x.rel: x for x in BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)}["constant/polyMesh/faces"]
    whole = _inflate(bucket.objects[("ex", k.key)])
    bucket.objects[("ex", k.key)] = _deflate(whole[:100])
    with pytest.raises(BF.KeptFileError):
        BF.fetch(ws, bucket.reader)
    assert not (ws / "constant/polyMesh/faces").exists()
    assert not list((ws / "constant/polyMesh").glob(".*.fetching"))


def _fea_ws(root: Path, *, with_msh: bool = False) -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "fea_plan.json").write_text("{}")
    (ws / "quality.json").write_text(json.dumps({"cells": 10}))
    (ws / "gmsh_spec.json").write_text("{}")
    (ws / "mesh.inp").write_bytes(b"*NODE\n" + b"1, 0.0, 0.0, 0.0\n" * 400)      # 6,806 bytes
    (ws / "mesh.bdf").write_bytes(b"GRID    1\n" * 50)                             # 500: stays
    (ws / "mesh.cdb").write_bytes(bytes(range(256)) * 9 + b"tail")                 # 2,308: binary-ish
    if with_msh:
        (ws / "mesh.msh").write_bytes(b"$MeshFormat\n" + b"x" * 3000)
    return ws


# ------------------------------------------------------------------ the mesher keeps


def test_only_big_delivery_only_files_are_kept(tmp_path, small_files):
    ws = _fea_ws(tmp_path, with_msh=True)
    assert [p.name for p in BF.keepable(ws)] == ["mesh.inp", "mesh.msh", "mesh.cdb"]
    (ws / "fea_plan.json").unlink()
    # a flow mesh's mesh.msh is the boundary its gates read: it always comes home
    assert [p.name for p in BF.keepable(ws)] == ["mesh.inp", "mesh.cdb"]


def test_nothing_is_kept_below_the_size(tmp_path):
    ws = _fea_ws(tmp_path)
    assert BF.keepable(ws) == []
    assert BF.keep(ws, bucket="b", output_key="ops/o.tar.gz", writer=_Bucket().writer) == []
    assert not (ws / BF.KEPT_FILE).exists()


def test_keep_moves_the_file_to_the_bucket_and_records_it(tmp_path, small_files, monkeypatch):
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = _fea_ws(tmp_path)
    original = (ws / "mesh.inp").read_bytes()
    bucket = _Bucket()
    kept = BF.keep(ws, bucket="ex", output_key="ops/abc/output.tar.gz", writer=bucket.writer)
    assert sorted(k.rel for k in kept) == ["mesh.cdb", "mesh.inp"]
    assert not (ws / "mesh.inp").exists() and (ws / "mesh.bdf").exists()
    rec = BF.read_record(ws, bucket="ex", prefix="ops/abc/output.tar.gz")
    k = rec["mesh.inp"]
    assert k.bytes == len(original) and k.key.startswith("ops/abc/output.tar.gz.kept/")
    # stored as one gzip member of the file's tar data: the bytes, then zeros to the next block
    packed = _inflate(bucket.objects[("ex", k.key)])
    assert packed[:len(original)] == original and len(packed) % 512 == 0
    assert set(packed[len(original):]) <= {0}
    assert b"".join(BF.raw_chunks(k, bucket.reader)) == original
    assert BF.present(ws, "mesh.inp") and BF.size(ws, "mesh.inp") == len(original)
    assert not BF.present(ws, "mesh.unv")


def test_a_short_or_tampered_object_is_never_delivered_as_the_file(tmp_path, small_files, monkeypatch):
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = _fea_ws(tmp_path)
    bucket = _Bucket()
    k = {x.rel: x for x in BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)}["mesh.inp"]
    whole = _inflate(bucket.objects[("ex", k.key)])
    bucket.objects[("ex", k.key)] = _deflate(whole[:100])
    with pytest.raises(BF.KeptFileError, match="short"):
        b"".join(BF.raw_chunks(k, bucket.reader))
    with pytest.raises(BF.KeptFileError, match="does not match"):
        b"".join(BF.packed_chunks(k, bucket.reader))


@pytest.mark.parametrize("row, why", [
    ({"bucket": "other"}, "another bucket"),
    ({"key": "someone/else.gz"}, "not this run"),
    ({"key": "o.kept/a/b/mesh.inp.gz"}, "not this run"),
    ({"bytes": 0}, "empty"),
])
def test_a_record_naming_anything_but_this_runs_objects_is_refused(tmp_path, row, why):
    ws = tmp_path
    good = {"bucket": "ex", "key": "o.kept/run1/mesh.inp.gz", "bytes": 5, "gz_bytes": 9,
            "gz_md5": "0" * 32, "mode": 0o644, "crc32": 0}
    (ws / BF.KEPT_FILE).write_text(json.dumps({"files": {"mesh.inp": {**good, **row}}}))
    with pytest.raises(BF.KeptFileError, match=why):
        BF.read_record(ws, bucket="ex", prefix="o")


def test_a_record_path_cannot_leave_the_workspace(tmp_path):
    row = {"bucket": "ex", "key": "o.kept/r/../x.gz", "bytes": 5, "gz_bytes": 9, "gz_md5": "0" * 32, "mode": 0, "crc32": 0}
    (tmp_path / BF.KEPT_FILE).write_text(json.dumps({"files": {"../x": row}}))
    with pytest.raises(BF.KeptFileError, match="not a workspace path"):
        BF.read_record(tmp_path)
    assert BF.kept(tmp_path) == {} and not BF.present(tmp_path, "../x")


# ------------------------------------------------------------------ the bundle, streamed


def _tarfile_bundle(ws: Path, members: list[str], prefix: str) -> bytes:
    """The bundle as the uploader builds it from local files (tarfile.add, normalised)."""
    def norm(ti):
        ti.mtime = 0
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel in sorted(members):
            if (ws / rel).exists():
                tf.add(ws / rel, arcname=f"{prefix}/{rel}", filter=norm)
    return buf.getvalue()


def test_the_streamed_bundle_is_the_same_archive_byte_for_byte(tmp_path, small_files, monkeypatch):
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = _fea_ws(tmp_path)
    (ws / "sub").mkdir()
    (ws / "sub" / "b.txt").write_text("b")
    (ws / "sub" / "a.txt").write_text("a")
    members = ["mesh.inp", "mesh.bdf", "mesh.cdb", "gmsh_spec.json", "sub", "mesh.unv"]
    expected = _tarfile_bundle(ws, members, "gmsh_case")
    bucket = _Bucket()
    BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)
    entries = BF.bundle_entries(ws, members, "gmsh_case")
    streamed = b"".join(BF.bundle_chunks(entries, bucket.reader))
    # many gzip members end to end: any gzip reader reads them as one stream - the same tar
    assert gzip.decompress(streamed) == expected
    with tarfile.open(fileobj=io.BytesIO(streamed), mode="r:gz") as tf:
        assert tf.extractfile("gmsh_case/mesh.inp").read().startswith(b"*NODE")
    # the same content gives the same bytes: a retried delivery stores the same object
    assert b"".join(BF.bundle_chunks(entries, bucket.reader)) == streamed


def test_a_file_over_8_gib_gets_the_header_tarfile_would_write():
    ti = tarfile.TarInfo("gmsh_case/mesh.inp")
    ti.size, ti.mode = 9 * 1024 ** 3, 0o644
    head = BF._header(BF._norm(ti))
    back = tarfile.TarInfo.frombuf(head[:512], "utf-8", "surrogateescape")
    assert back.type == tarfile.XHDTYPE        # pax: ustar holds no size past 8 GiB
    assert len(head) % 512 == 0


# ------------------------------------------------------------------ the worker


def test_the_deck_counts_as_written_when_it_is_kept(tmp_path, small_files, monkeypatch):
    from meshpipeline.engines.fea_results import _ready
    from meshpipeline.engines.gmsh.gmsh_runner import finalize
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = _fea_ws(tmp_path)
    BF.keep(ws, bucket="ex", output_key="o", writer=_Bucket().writer)
    assert _ready(ws, {"file": "mesh.inp"})["file_ready"]
    assert _ready(ws, {"file": "mesh.cdb"})["file_ready"]
    assert not _ready(ws, {"file": "mesh.unv"})["file_ready"]
    # the engine's own "no deck" refusal does not fire on a kept deck
    out = finalize(str(ws), [], "gmsh")
    assert "no mesh.inp" not in str(out.get("output") or "")


def test_clear_drops_a_previous_results_record_and_stand_ins(tmp_path):
    for name in BF.STAND_INS:
        (tmp_path / name).write_text("x")
    BF.clear(tmp_path)
    assert not any((tmp_path / n).exists() for n in BF.STAND_INS)


def test_release_deletes_each_kept_object_once_delivered(tmp_path, small_files, monkeypatch):
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = _fea_ws(tmp_path)
    bucket = _Bucket()
    kept = BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)
    assert BF.release(ws, bucket.delete) == len(kept)
    assert sorted(bucket.deleted) == sorted(k.key for k in kept)


# ------------------------------------------------------------------ gmsh stand-ins


def _gmsh_fea_msh(path: Path) -> None:
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("box")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        for _d, tag in gmsh.model.getEntities(2):
            gmsh.model.addPhysicalGroup(2, [tag], name=f"Face_{tag}")
        gmsh.model.addPhysicalGroup(3, [1], name="solid")
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.4)
        gmsh.model.mesh.generate(3)
        gmsh.model.mesh.setOrder(2)
        gmsh.write(str(path))
    finally:
        gmsh.finalize()


def test_the_viewer_reads_the_stand_in_exactly_as_it_read_the_mesh(tmp_path):
    from meshpipeline.engines.gmsh.viewer_surface import read_msh, surface_patches_of
    ws = tmp_path
    (ws / "fea_plan.json").write_text("{}")
    _gmsh_fea_msh(ws / "mesh.msh")
    before = surface_patches_of(read_msh(ws, by_face=True))
    made = BF.write_stand_ins(ws)
    assert made == [BF.VIEWER_MESH_FILE, BF.REVIEW_SURFACE_FILE]
    (ws / "mesh.msh").unlink()
    after = surface_patches_of(read_msh(ws, by_face=True))
    assert list(after) == list(before) == [f"Face_{k}" for k in range(1, 7)]
    import numpy as np
    for name in before:
        assert np.allclose(np.asarray(after[name]), np.asarray(before[name]), rtol=0, atol=0)
    assert read_msh(ws) is None          # a flow view never reads an FEA stand-in


def test_a_big_surface_is_thinned_face_by_face_keeping_every_outline(tmp_path, monkeypatch):
    import numpy as np

    import meshpipeline.engines.gmsh.viewer_surface as vs
    _gmsh_fea_msh(tmp_path / "mesh.msh")
    mesh = vs.read_msh(tmp_path, by_face=True)
    total = sum(len(p) for _n, p in mesh["patches"])
    light = vs.downsample(mesh, max_triangles=total // 4)
    shown = sum(len(p) for _n, p in light["patches"])
    assert [n for n, _p in light["patches"]] == [n for n, _p in mesh["patches"]]
    assert shown < total / 2 and light["downsampled"] == {"triangles": total, "shown": shown}
    # the same box: every corner and edge (each face's border) is kept
    assert np.allclose(light["points"].min(0), mesh["points"].min(0))
    assert np.allclose(light["points"].max(0), mesh["points"].max(0))
    assert vs.downsample(mesh, max_triangles=total) is mesh
    monkeypatch.setattr(vs, "VIEW_MAX_TRIANGLES", total // 4)
    vs.save_viewer_mesh(tmp_path / BF.VIEWER_MESH_FILE, mesh)
    (tmp_path / "mesh.msh").unlink()
    back = vs.load_viewer_mesh(tmp_path)
    assert sum(len(p) for _n, p in back["patches"]) == shown


_FOAM_HEAD = ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n    class       labelList;\n"
              "    note        \"nCells:9\";\n    object      neighbour;\n}\n\n")


def test_a_label_list_is_read_in_pieces_and_checked_against_its_count(tmp_path, monkeypatch):
    import numpy as np

    from meshpipeline.engines import foam_labels as FL
    labels = list(range(1, 5000))
    p = tmp_path / "neighbour"
    p.write_text(_FOAM_HEAD + f"{len(labels)}\n(\n" + "\n".join(map(str, labels)) + "\n)\n")
    monkeypatch.setattr(FL, "PIECE_BYTES", 97)       # many pieces, each cut at a line end
    got = FL.read_foam_labels(p)
    assert got is not None and np.array_equal(got, np.asarray(labels))
    # a list that is not what it says it is is never used: short, or with a negative cell
    p.write_text(_FOAM_HEAD + "5\n(\n1\n2\n3\n)\n")
    assert FL.read_foam_labels(p) is None
    p.write_text(_FOAM_HEAD + "3\n(\n1\n-1\n3\n)\n")
    assert FL.read_foam_labels(p) is None
    # cfMesh's neighbour list, the length of every face, -1 for each boundary face after the
    # internal ones (job 6cb2d606's 107 M-cell mesh): the internal faces are the list
    p.write_text(_FOAM_HEAD + "5\n(\n4\n7\n9\n-1\n-1\n)\n")
    assert FL.read_foam_labels(p).tolist() == [4, 7, 9]
    owner = tmp_path / "owner"
    owner.write_text(_FOAM_HEAD + "5\n(\n4\n7\n9\n-1\n-1\n)\n")
    assert FL.read_foam_labels(owner) is None          # an owner has no padding
    p.write_text(_FOAM_HEAD.replace("ascii", "binary") + "3\n(\n1\n2\n3\n)\n")
    assert FL.read_foam_labels(p) is None


def test_the_kept_pass_shares_its_mesh_files_and_copies_its_records(tmp_path):
    from meshpipeline.engines import fea_driver as FD
    ws = tmp_path
    for name in ("mesh.inp", "mesh.msh", "quality.json", "fea_plan.json"):
        (ws / name).write_text(name)
    FD._keep_pass(ws, 1)
    d = ws / FD.PASS_FOLDER.format(n=1)
    assert (d / "mesh.inp").stat().st_ino == (ws / "mesh.inp").stat().st_ino      # linked: no bytes copied
    assert (d / "quality.json").stat().st_ino != (ws / "quality.json").stat().st_ino
    # the next pass clears its outputs and writes new ones: the kept pass keeps its own bytes
    FD._clear_outputs(ws)
    (ws / "mesh.inp").write_text("pass 2")
    (ws / "fea_plan.json").write_text("plan 2")
    assert (d / "mesh.inp").read_text() == "mesh.inp" and (d / "fea_plan.json").read_text() == "fea_plan.json"
    FD._restore_pass(ws, 1)
    assert (ws / "mesh.inp").read_text() == "mesh.inp" and (ws / "fea_plan.json").read_text() == "fea_plan.json"


def _pass_result(ws: Path, n: int, *, in_bucket: bool = False) -> None:
    """The workspace as pass `n` left it: its records, and its deck here or (a big result) in the bucket."""
    from meshpipeline.engines import fea_driver as FD
    FD._clear_outputs(ws)
    (ws / "quality.json").write_text(f"quality {n}")
    (ws / "fea_plan.json").write_text(f"plan {n}")
    if in_bucket:
        BF.write_record(ws, [BF.Kept(rel="mesh.inp", bucket="ex", key=f"o.kept/run{n}/mesh.inp.gz", bytes=9,
                                     gz_bytes=5, gz_md5="0" * 32, mode=0o644, crc32=1)])
    else:
        (ws / "mesh.inp").write_text(f"deck {n}")


def _not_kept_cited(job: str) -> list[dict]:
    from meshpipeline.contracts import decision_record as dr
    return [e for e in dr.cited_for(job) if e["decided"] == "delivered pass not kept"]


def test_a_kept_pass_is_one_whose_deck_is_in_its_folder_or_in_its_own_bucket_record(tmp_path):
    from meshpipeline.engines import fea_driver as FD
    ws = tmp_path
    _pass_result(ws, 1)
    FD._keep_pass(ws, 1)
    _pass_result(ws, 2, in_bucket=True)
    FD._keep_pass(ws, 2)
    _pass_result(ws, 3)
    FD._keep_pass_log(ws, 3)                 # its log and quality only: the folder is there, the mesh is not
    assert FD._pass_kept(ws, 1) and FD._pass_kept(ws, 2)
    assert (ws / FD.PASS_FOLDER.format(n=3)).is_dir() and not FD._pass_kept(ws, 3) and not FD._pass_kept(ws, 4)


def test_a_chosen_pass_never_kept_is_delivered_from_the_workspace_it_still_holds(tmp_path):
    """Job d03f1bf6, 10 Nov: the chosen pass's decks were in the bucket and its folder held only its log;
    restoring from that folder deleted the record of the kept decks, and the run had no mesh."""
    from meshpipeline.engines import fea_driver as FD
    ws = tmp_path
    _pass_result(ws, 1)
    FD._keep_pass(ws, 1)
    _pass_result(ws, 2, in_bucket=True)
    FD._keep_pass_log(ws, 2)
    passes = [{"pass": 1, "valid": True, "deliverable": False}, {"pass": 2, "valid": True}]
    assert FD._deliver_pass(ws, 2, passes, "t-in-hand") == 2
    assert BF.present(ws, "mesh.inp") and BF.kept(ws)["mesh.inp"].key == "o.kept/run2/mesh.inp.gz"
    assert (ws / "fea_plan.json").read_text() == "plan 2" and (ws / "quality.json").read_text() == "quality 2"
    said = _not_kept_cited("t-in-hand")
    assert len(said) == 1 and said[0]["value"] == 2 and said[0]["inputs"]["kept"] == [1]


def test_a_chosen_pass_never_kept_and_no_longer_in_the_workspace_falls_back_to_the_best_kept_pass(tmp_path):
    from meshpipeline.engines import fea_driver as FD
    ws = tmp_path
    _pass_result(ws, 1)
    FD._keep_pass(ws, 1)
    _pass_result(ws, 2, in_bucket=True)
    FD._keep_pass_log(ws, 2)
    _pass_result(ws, 3)                      # a later pass ran: the workspace is no longer pass 2's
    passes = [{"pass": 1, "valid": True, "short": ["W1"], "over": False, "min_sicn": 0.2, "deliverable": False},
              {"pass": 2, "valid": True, "short": [], "over": False, "min_sicn": 0.3},
              {"pass": 3, "valid": False, "short": [], "over": False, "min_sicn": None}]
    assert FD._deliver_pass(ws, 2, passes, "t-fall-back") == 1
    assert (ws / "mesh.inp").read_text() == "deck 1" and (ws / "fea_plan.json").read_text() == "plan 1"
    assert passes[1]["not_kept"] and passes[1]["deliverable"] is False and "deliverable" not in passes[0]
    said = _not_kept_cited("t-fall-back")
    assert len(said) == 1 and said[0]["value"] == 1 and "pass 1" in said[0]["words"]


def test_no_other_passes_mesh_goes_out_under_the_name_of_a_pass_that_left_none(tmp_path):
    from meshpipeline.engines import fea_driver as FD
    ws = tmp_path
    _pass_result(ws, 1)
    (ws / "mesh.inp").unlink()               # read as valid from its quality record, but no deck anywhere
    FD._keep_pass_log(ws, 1)
    _pass_result(ws, 2)                      # a later, invalid pass left its own deck in the workspace
    passes = [{"pass": 1, "valid": True}, {"pass": 2, "valid": False}]
    assert FD._deliver_pass(ws, 1, passes, "t-none") == 1
    assert not BF.present(ws, "mesh.inp")
    assert len(_not_kept_cited("t-none")) == 1


def test_the_reviewer_surface_has_the_faces_and_no_volume(tmp_path):
    gmsh = pytest.importorskip("gmsh")
    _gmsh_fea_msh(tmp_path / "mesh.msh")
    from meshpipeline.engines.gmsh.viewer_surface import write_surface_only
    assert write_surface_only(tmp_path / "mesh.msh", tmp_path / BF.REVIEW_SURFACE_FILE)
    assert (tmp_path / BF.REVIEW_SURFACE_FILE).stat().st_size < (tmp_path / "mesh.msh").stat().st_size
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(tmp_path / BF.REVIEW_SURFACE_FILE))
        assert len(gmsh.model.getPhysicalGroups(2)) == 6
        assert gmsh.model.getPhysicalGroups(3) == []
        assert all(len(t) == 0 for t in gmsh.model.mesh.getElements(3)[1])
    finally:
        gmsh.finalize()


def test_the_manifest_points_the_reviewer_at_the_stand_in(tmp_path):
    from meshpipeline.engines.manifest import _review_surface
    (tmp_path / BF.REVIEW_SURFACE_FILE).write_text("x")
    assert _review_surface(tmp_path).endswith(BF.REVIEW_SURFACE_FILE)
    (tmp_path / "mesh.msh").write_text("x")
    assert _review_surface(tmp_path).endswith("mesh.msh")


# ------------------------------------------------------------------ delivery, end to end


class _StreamStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
        from meshpipeline.contracts.object_storage import StoredObject
        data = Path(local_path).read_bytes()
        self.objects[object_key] = data
        return StoredObject(object_key=object_key, size_bytes=len(data), checksum=None)

    def upload_stream(self, *, chunks, object_key, content_type=None):
        import hashlib

        from meshpipeline.contracts.object_storage import StoredObject
        data = b"".join(chunks)
        self.objects[object_key] = data
        return StoredObject(object_key=object_key, size_bytes=len(data),
                            checksum=hashlib.md5(data).hexdigest())  # noqa: S324

    def stat_object(self, *, object_key):
        from meshpipeline.contracts.object_storage import StoredObject
        return StoredObject(object_key=object_key, size_bytes=len(self.objects[object_key]))

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)


def test_a_kept_fea_result_is_delivered_whole_from_the_bucket(tmp_path, small_files, monkeypatch):
    from tests.engine_workspaces import build_workspace
    from tests.unit.application.test_artifact_delivery_contract import _Db, _Repo

    import meshpipeline.application.artifact_uploader as au
    import meshpipeline.contracts.object_storage as osmod
    from meshpipeline.contracts import kept_objects
    from meshpipeline.persistence.models import ArtifactType

    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = build_workspace(tmp_path, "gmsh")
    (ws / "fea_plan.json").write_text("{}")
    deck = b"*NODE\n" + b"1, 0.0, 0.0, 0.0\n" * 800
    (ws / "mesh.inp").write_bytes(deck)
    (ws / "mesh.cdb").write_bytes(b"N," * 900)
    members = [m.path for m in __import__(
        "meshpipeline.engines.registry", fromlist=["get_spec"]).get_spec("gmsh").deliverable.members]
    # the workspace as it was, for the archive the uploader would have packed from local files
    import shutil
    orig = tmp_path / "as_written"
    shutil.copytree(ws, orig)
    bucket = _Bucket()
    BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)
    assert not (ws / "mesh.inp").exists()

    store, repo = _StreamStore(), _Repo()
    prev = osmod._store
    osmod.set_object_store(store)
    kept_objects.set_kept_objects(bucket.reader, bucket.delete)
    try:
        monkeypatch.setattr(au, "ArtifactRepository", lambda: repo)
        job = uuid.uuid4()
        report = asyncio.run(au.upload_job_artifacts(db=_Db(), job_id=job, workspace=ws, engine="gmsh"))
        assert report.required_all_delivered() and not report.optional_failures
        bundle = store.objects[f"jobs/{job}/gmsh_case.tar.gz"]
        # the Mesh report the uploader wrote goes in the Everything bundle with the rest
        from meshpipeline.render.mesh_report import REPORT_FILE
        shutil.copy(ws / REPORT_FILE, orig / REPORT_FILE)
        expected_tar = _tarfile_bundle(orig, [*members, REPORT_FILE], "gmsh_case")
        assert gzip.decompress(bundle) == expected_tar
        assert store.objects[f"jobs/{job}/exports/mesh.inp"] == deck
        assert store.objects[f"jobs/{job}/exports/mesh.cdb"] == b"N," * 900
        assert {r.artifact_type for r in repo.rows} >= {ArtifactType.mesh_bundle, ArtifactType.viewer_data}
        # nothing streamed touched the disk
        assert not list(ws.glob("bundle_*")) and not list(ws.glob("stream_*"))
        assert au.release_kept(ws) == 2 and bucket.deleted
    finally:
        osmod.set_object_store(prev)
        kept_objects.set_kept_objects(None, None)


def test_a_store_that_cannot_stream_is_given_a_file(tmp_path):
    import meshpipeline.application.artifact_uploader as au
    from meshpipeline.persistence.models import ArtifactType

    class _Plain:
        def upload_file(self, *, local_path, object_key, content_type=None, metadata=None):
            from meshpipeline.contracts.object_storage import StoredObject
            self.data = Path(local_path).read_bytes()
            return StoredObject(object_key=object_key, size_bytes=len(self.data))

    p = au._Planned(ArtifactType.mesh, "x", "k", "text/plain", tmp_path / "x", False,
                    chunks=lambda: iter([b"ab", b"cd"]), expect_bytes=4)
    store = _Plain()
    got = au._upload_streamed(store, p, scratch_dir=tmp_path)
    assert store.data == b"abcd" and got.size_bytes == 4 and len(got.checksum) == 32
    assert not list(tmp_path.glob("stream_*"))


# ------------------------------------------------------------------ collection limits


def test_a_flow_result_is_collected_by_what_the_mesher_measured():
    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    # the 107 M-cell cfMesh run of 28 Oct: 3.4 GB packed, 22 GB raw, lost to the cell-capped limits
    doc = {"rc": 0, "quality": {"cells": 107_000_000},
           "returned_bytes": {"raw": 22 * 10 ** 9, "largest": 9 * 10 ** 9, "fea": False}}
    lim = crc.result_extraction_limits(doc)
    assert lim.max_total_uncompressed_bytes >= 22 * 10 ** 9 and lim.max_archive_bytes >= 3.4e9
    assert lim.max_file_bytes >= 9 * 10 ** 9
    huge = {**doc, "returned_bytes": {"raw": 10 ** 15, "largest": 10 ** 15}}
    assert crc.result_extraction_limits(huge).max_total_uncompressed_bytes == crc.RESULT_MAX_BYTES


def test_a_flow_result_too_big_for_the_worker_is_refused_and_kept(tmp_path, monkeypatch):
    import shutil

    from tests.unit.platform.test_fea_valid_mesh_never_lost import _Bucket as _SizedBucket
    from tests.unit.platform.test_fea_valid_mesh_never_lost import _usage

    import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _usage(20 * 10 ** 9))
    doc = {"quality": {"cells": 107_000_000}, "returned_bytes": {"raw": 22 * 10 ** 9, "largest": 1}}
    got = crc.worker_room(_SizedBucket(4 * 10 ** 9), "out", doc, tmp_path)
    assert got["rc"] == crc.RC_INFRASTRUCTURE and got["too_big_for_worker"]["need_bytes"] == 30 * 10 ** 9


def test_a_big_cfmesh_plan_sizes_its_machine_by_the_files_it_will_write(tmp_path):
    from meshpipeline.contracts import mesh_machine as MM
    from meshpipeline.engines.cfmesh.cfmesh_runner import machine_for_plan
    got = machine_for_plan(tmp_path, {"estimated_cells": 30_000_000, "estimated_seconds": 60})
    assert got["workspace_gib"] == pytest.approx(30_000_000 * MM.FLOW_FILE_BYTES_PER_CELL / 2 ** 30, abs=0.01)
    small = machine_for_plan(tmp_path, {"estimated_cells": 1_000_000, "estimated_seconds": 60})
    assert small["where"] == "cloud_run"


# ------------------------------------------------------------------ the mesher side, wired


def test_the_mesher_keeps_then_measures_what_it_really_sends(tmp_path, small_files, monkeypatch):
    from meshpipeline.adapters.mesh_execution import gcs_exchange as gx
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    bucket = _Bucket()
    monkeypatch.setattr(gx, "kept_writer", bucket.writer)
    ws = _fea_ws(tmp_path)
    result: dict = {"rc": 0}
    gx.keep_big_files("gs://ex/ops/k/output.tar.gz", str(ws), result)
    rb = result["returned_bytes"]
    assert rb["kept_files"] == 2 and rb["kept"] > 0
    assert rb["raw"] == sum(p.stat().st_size for p in ws.rglob("*") if p.is_file())
    assert BF.read_record(ws, bucket="ex", prefix="ops/k/output.tar.gz")


def test_a_failed_keep_sends_the_workspace_home_whole(tmp_path, small_files, monkeypatch):
    from meshpipeline.adapters.mesh_execution import gcs_exchange as gx
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])

    @contextlib.contextmanager
    def broken(bucket, key):
        raise OSError("bucket down")
        yield  # pragma: no cover

    monkeypatch.setattr(gx, "kept_writer", broken)
    ws = _fea_ws(tmp_path)
    with pytest.raises(OSError):
        gx.keep_big_files("gs://ex/o", str(ws), {"rc": 0})
    assert (ws / "mesh.inp").exists() and not (ws / BF.KEPT_FILE).exists()


def test_the_bundle_is_one_gzip_member_every_reader_reads(tmp_path, small_files, monkeypatch):
    # Python's stream reader ("r|gz") refuses a file of several gzip members; the bundle is one
    monkeypatch.setattr(BF, "write_stand_ins", lambda ws: [])
    ws = _fea_ws(tmp_path)
    members = ["mesh.inp", "mesh.bdf", "mesh.cdb", "gmsh_spec.json"]
    expected = _tarfile_bundle(ws, members, "gmsh_case")
    bucket = _Bucket()
    BF.keep(ws, bucket="ex", output_key="o", writer=bucket.writer)
    streamed = b"".join(BF.bundle_chunks(BF.bundle_entries(ws, members, "gmsh_case"), bucket.reader))
    assert streamed.count(b"\x1f\x8b\x08") >= 1 and streamed[:3] == b"\x1f\x8b\x08"
    import zlib
    d = zlib.decompressobj(31)
    assert d.decompress(streamed) == expected and d.eof and d.unused_data == b""   # one member, trailer checked
    with tarfile.open(fileobj=io.BytesIO(streamed), mode="r|gz") as tf:
        names = [m.name for m in tf]
    assert "gmsh_case/mesh.inp" in names


def test_crc32_combine_is_zlibs():
    import random
    import zlib
    rng = random.Random(7)
    for _ in range(20):
        a = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 3000)))
        b = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 3000)))
        assert BF.crc32_combine(zlib.crc32(a), zlib.crc32(b), len(b)) == zlib.crc32(a + b)
