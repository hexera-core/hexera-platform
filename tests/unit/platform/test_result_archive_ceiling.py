# Responsibility: Verify a returned mesh workspace may be as large as a mesh the cell budget allows, and no other extraction bound moves.
# A 7 M-cell snappy passage (557 MB) and VMTK's Fluent aorta (560 MB, lab 2026-10-04) were built and paid for, then
# refused on the way back by the generic 512 MiB extraction ceiling.
from __future__ import annotations

import dataclasses

import meshpipeline.adapters.mesh_execution.cloud_run_client as crc
import meshpipeline.settings.policy as polcfg
from meshpipeline.sandbox.safe_extract import default_limits


def test_the_result_ceiling_is_the_cell_budget_s_worth(monkeypatch):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 8_000_000)
    lim = crc.result_extraction_limits()
    assert lim.max_archive_bytes == 8_000_000 * crc.RESULT_BYTES_PER_CELL > 560_000_000
    base = default_limits()
    for f in dataclasses.fields(base):
        if f.name != "max_archive_bytes":
            assert getattr(lim, f.name) == getattr(base, f.name), f.name


def test_the_ceiling_never_drops_below_the_generic_one(monkeypatch):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 1_000)
    assert crc.result_extraction_limits().max_archive_bytes == default_limits().max_archive_bytes


def test_the_mesh_the_result_document_reports_sizes_the_limits(monkeypatch):
    # a 6.7 M-cell cfMesh mesh: its faces file alone is 699 MB raw, its polyMesh 1,236 MB
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 8_000_000)
    lim = crc.result_extraction_limits({"rc": 0, "quality": {"cells": 6_707_503}})
    assert lim.max_archive_bytes == max(8_000_000 * crc.RESULT_BYTES_PER_CELL,
                                        6_707_503 * crc.RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL)
    assert lim.max_file_bytes >= 2 * 699_000_000
    assert lim.max_total_uncompressed_bytes >= 4 * 1_236_000_000
    base = default_limits()
    assert (lim.max_entries, lim.max_ratio, lim.max_depth) == (
        base.max_entries, base.max_ratio, base.max_depth)


def test_no_result_document_can_lift_the_limits_without_end(monkeypatch):
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 8_000_000)
    huge = crc.result_extraction_limits({"quality": {"cells": 10 ** 12}})
    assert huge.max_archive_bytes == 2 * 8_000_000 * crc.RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL
    for junk in ({"quality": {"cells": "many"}}, {"quality": None}, {"quality": {"cells": -5}}):
        assert crc.result_extraction_limits(junk) == crc.result_extraction_limits()


def test_an_archive_refused_for_its_size_names_its_largest_files(tmp_path):
    import io
    import os
    import tarfile

    import pytest

    from meshpipeline.sandbox.safe_extract import WorkspaceExtractionError, safe_extract_tar
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, n in (("VTK/case_0/internal.vtu", 400_000), ("constant/polyMesh/faces", 200_000),
                        ("mesh_quality.json", 10)):
            data = os.urandom(n)
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    lim = dataclasses.replace(default_limits(), max_archive_bytes=1000)
    with pytest.raises(WorkspaceExtractionError) as err:
        safe_extract_tar(fileobj=buf, dest=tmp_path / "out", limits=lim)
    msg = str(err.value)
    assert "over the 1000 cap" in msg and "largest files" in msg
    assert msg.index("VTK/case_0/internal.vtu") < msg.index("constant/polyMesh/faces")
    assert buf.tell() == 0, "the archive is left where it was"


def test_naming_the_files_stops_on_time_inside_a_huge_member(tmp_path):
    # one highly compressed member: skipping it means inflating it, and the budget holds there too
    import io
    import tarfile
    import time

    from meshpipeline.sandbox.safe_extract import largest_members
    path = tmp_path / "bomb.tar.gz"
    with tarfile.open(path, mode="w:gz") as tf:
        ti = tarfile.TarInfo("zeros.bin")
        ti.size = 300_000_000
        tf.addfile(ti, io.BytesIO(b"\0" * ti.size))
        ti2 = tarfile.TarInfo("after.bin")
        ti2.size = 3
        tf.addfile(ti2, io.BytesIO(b"abc"))
    t = time.monotonic()
    out = largest_members(path=path, budget_s=0.05)
    assert time.monotonic() - t < 2.0
    assert "after.bin" not in out
    assert largest_members(path=path, budget_s=60.0).count("MB") >= 2
