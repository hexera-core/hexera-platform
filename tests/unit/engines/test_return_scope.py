# Responsibility: Verify what rides home with a mesh: scratch never does, and the optional volume export is left out
# only when the archive would not fit the extraction ceiling - never the mesh, its boundary or its measurements.
from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import meshpipeline.engines.return_scope as RS
from meshpipeline.adapters.mesh_execution.gcs_exchange import RESULT_ARCHIVE_LEVEL as ARCHIVE_LEVEL


def _ws(tmp_path: Path, *, volume_bytes: int = 3_000_000) -> Path:
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    # ASCII mesh files compress well; the volume export (binary) barely does
    (pm / "owner").write_text("\n".join(str(i % 977) for i in range(200_000)))
    (pm / "points").write_text("\n".join(f"({i * 0.001:.6f} 0.1 0.2)" for i in range(50_000)))
    vtk = tmp_path / "VTK" / "case_0"
    (vtk / "boundary").mkdir(parents=True)
    (vtk / "internal.vtu").write_bytes(os.urandom(volume_bytes))
    (vtk / "boundary" / "wall.vtp").write_bytes(os.urandom(50_000))
    (tmp_path / "mesh_quality.json").write_text(json.dumps({"cells": 1}))
    return tmp_path


def _archive_bytes(ws: Path) -> int:
    """The archive the mesher really sends, at the level it really sends it
    (gcs_exchange.RESULT_ARCHIVE_LEVEL). Measuring against tarfile's level-9 default instead was
    measuring against an archive nothing writes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=ARCHIVE_LEVEL) as tf:
        for child in sorted(ws.iterdir()):
            tf.add(str(child), arcname=child.name)
    return len(buf.getvalue())


def test_the_measure_matches_the_real_archive_and_never_reads_low(tmp_path):
    ws = _ws(tmp_path, volume_bytes=6_000_000)
    est, real = RS.estimate_archive_bytes(ws), _archive_bytes(ws)
    # NOT `0.99 * real`, which is what this allowed: the measure reading 1% low is the whole
    # failure, and it did read low here (6,192,083 against 6,193,573) until frame_bytes counted
    # the members the files' own bytes do not. On the coupon's real workspace the margin was
    # 19,881 bytes in 244,704,856 - 0.008% - so there was never room to spend on a tolerance.
    assert real <= est <= 1.15 * real, (est, real)


def test_a_file_that_compresses_unevenly_is_measured_whole(tmp_path):
    # compressible at its start, middle and end, random between: a sample of three would read it
    # as almost nothing
    rnd = os.urandom(3_000_000)
    p = tmp_path / "uneven.bin"
    p.write_bytes(b"\0" * 1_100_000 + rnd + b"\0" * 1_100_000 + rnd + b"\0" * 1_100_000)
    real = len(gzip.compress(p.read_bytes(), 9))
    assert real <= RS.compressed_size(p) <= 1.05 * real + 1024


def test_nothing_is_left_out_of_an_archive_that_fits(tmp_path):
    ws = _ws(tmp_path)
    assert RS.scope_return(ws, ceiling=512 * 2**20) is None
    assert (ws / "VTK" / "case_0" / "internal.vtu").exists()
    assert not (ws / RS.RETURN_SCOPE_FILE).exists()


def test_the_volume_export_is_left_out_when_the_archive_would_not_fit(tmp_path):
    ws = _ws(tmp_path)
    before = RS.estimate_archive_bytes(ws)
    rec = RS.scope_return(ws, ceiling=int(before * 0.6))
    assert rec and rec["fits"]
    assert [x["file"] for x in rec["left_out"]] == ["VTK/case_0/internal.vtu"]
    assert not (ws / "VTK" / "case_0" / "internal.vtu").exists()
    # the mesh, its boundary and its measurements all stay
    for keep in ("constant/polyMesh/owner", "constant/polyMesh/points",
                 "VTK/case_0/boundary/wall.vtp", "mesh_quality.json"):
        assert (ws / keep).exists(), keep
    assert "volume export" in rec["note"]
    assert json.loads((ws / RS.RETURN_SCOPE_FILE).read_text())["left_out"]


def test_a_mesh_too_big_even_without_its_volume_says_so(tmp_path):
    ws = _ws(tmp_path)
    rec = RS.scope_return(ws, ceiling=1000)
    assert rec and not rec["fits"] and (ws / "constant" / "polyMesh" / "owner").exists()


def test_scratch_never_rides_home(tmp_path):
    ws = _ws(tmp_path)
    (ws / "_rescue" / "as_built").mkdir(parents=True)
    (ws / "_rescue" / "as_built" / "owner").write_text("x")
    rec = RS.scope_return(ws, ceiling=512 * 2**20)
    assert rec and rec["scratch_removed"] == ["_rescue/"] and not (ws / "_rescue").exists()
    assert (ws / "VTK" / "case_0" / "internal.vtu").exists()


def test_every_engine_run_is_scoped_before_it_goes_home(tmp_path, monkeypatch):
    import meshpipeline.engines.dispatch as D
    ws = _ws(tmp_path)
    (ws / "_rescue").mkdir()

    def _runner(workspace, *, bashrc, timeout):
        return {"rc": 0, "timed_out": False}
    monkeypatch.setattr(D, "engine_runners", lambda: {"gmsh": _runner})
    out = D.run_engine_local(ws, engine="gmsh", timeout=10)
    assert out["rc"] == 0 and out["return_scope"]["scratch_removed"] == ["_rescue/"]


def test_an_empty_file_still_costs_its_archive_header(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert 512 <= RS.compressed_size(p) <= 600
    assert RS.compressed_size(Path(gzip.__file__)) > 600
# #
# WHAT THE MEASURE COSTS. Every byte is compressed here to decide and again by the archiver to
# send, so the measure is paid at the cheapest level that can still answer - and the cheap level
# has to read HIGH, or a built mesh is lost on the way back.
# #
#: Measured on the two-scale coupon's own workspace (1,120,883,923 bytes over 105 files, the mesh
#: of job 575f76e6): the fast measure read 303,084,615 bytes in 26.0 s where the exact one read
#: 244,724,737 in 114.1 s - 1.2385 of it, and 4.4 times as fast.
COUPON_FAST_OVER_EXACT = 1.2385
#: ...but level 1 is not guaranteed to read high FILE BY FILE. Over the same workspace's 62 files
#: of 4 KiB or more, one read LOW against level 6 - a boundary .vtp, at this fraction of its
#: level-6 size. FAST_TRUST is the margin that covers it.
COUPON_WORST_FAST_FILE = 0.9937


def test_the_order_of_the_levels_is_what_makes_the_measure_safe():
    """The measure may only be compressed HARDER than the archive, never softer. zlib's output
    shrinks as the level rises, so a measure at a level above the archiver's would read LOW - and
    reading low is the one failure that loses a mesh. Lower the archiver to level 1 and leave the
    measure at 6 and this is what says so."""
    assert RS.FAST_LEVEL <= RS.MEASURE_LEVEL <= ARCHIVE_LEVEL


def test_the_fast_measure_reads_high_against_the_real_archive(tmp_path):
    """The whole fast path rests on this. Pinned on the coupon's measured figures above."""
    ws = _ws(tmp_path, volume_bytes=6_000_000)
    fast = RS.estimate_archive_bytes(ws, level=RS.FAST_LEVEL)
    exact = RS.estimate_archive_bytes(ws, level=RS.MEASURE_LEVEL)
    real = _archive_bytes(ws)
    assert fast >= real, (fast, real)
    assert exact >= real, (exact, real)
    assert fast >= exact


def test_the_margin_on_the_fast_measure_covers_the_worst_file_measured():
    """FAST_TRUST is not a round number someone liked: it is the room left for a file the cheap
    level reads low on, and it has to stay several times wider than the worst one measured."""
    assert (1 - RS.FAST_TRUST) >= 5 * (1 - COUPON_WORST_FAST_FILE)


def _levels_used(monkeypatch, record):
    real = RS.compressed_size

    def spy(path, level=RS.MEASURE_LEVEL):
        record.append((Path(path), level))
        return real(path, level)
    monkeypatch.setattr(RS, "compressed_size", spy)


def test_a_workspace_the_fast_measure_clears_is_never_measured_exactly(tmp_path, monkeypatch):
    """THE 25 MINUTES. A workspace whose raw bytes do not fit but whose archive is plainly clear
    of the bar used to be compressed whole at the exact level to find that out, and then compressed
    whole again to be sent. The cheap level answers it."""
    ws = _ws(tmp_path, volume_bytes=200_000)
    (ws / "_rescue").mkdir()          # so there is a record to read the decision off
    fast, raw = RS.estimate_archive_bytes(ws, level=RS.FAST_LEVEL), RS.raw_bytes(ws)
    cap = int(fast / RS.FAST_TRUST * 1.1 / RS.HEADROOM)
    assert RS.HEADROOM * cap < raw, "the raw path would settle this, so it proves nothing"

    used = []
    _levels_used(monkeypatch, used)
    rec = RS.scope_return(ws, ceiling=cap)

    assert rec["measure"] == "fast" and rec["left_out"] == []
    assert {lv for _, lv in used} == {RS.FAST_LEVEL}, "the exact measure was paid for anyway"
    assert (ws / "VTK" / "case_0" / "internal.vtu").exists()


def test_no_file_is_given_up_on_the_fast_measure_s_word(tmp_path, monkeypatch):
    """The cheap level reads high, so it can say "might not fit" about a workspace that does. It
    is never allowed to be the reason a file is dropped: the exact measure settles that."""
    ws = _ws(tmp_path, volume_bytes=200_000)
    (ws / "_rescue").mkdir()
    fast = RS.estimate_archive_bytes(ws, level=RS.FAST_LEVEL)
    exact = RS.estimate_archive_bytes(ws, level=RS.MEASURE_LEVEL)
    assert exact < fast / RS.FAST_TRUST, "no band between the two measures: nothing to test"
    cap = int((exact + fast / RS.FAST_TRUST) / 2 / RS.HEADROOM)

    used = []
    _levels_used(monkeypatch, used)
    rec = RS.scope_return(ws, ceiling=cap)

    assert rec["measure"] == "exact", "the fast measure decided a case it was not clear on"
    assert rec["left_out"] == [] and (ws / "VTK" / "case_0" / "internal.vtu").exists()
    assert RS.MEASURE_LEVEL in {lv for _, lv in used}


def test_a_file_given_up_is_not_compressed_a_second_time_to_price_it(tmp_path, monkeypatch):
    """It was compressed by the walk that decided, and then again to be given up - a third pass
    over the heaviest file in the workspace, for a number already in hand."""
    ws = _ws(tmp_path)
    before = RS.estimate_archive_bytes(ws)
    vtu = ws / "VTK" / "case_0" / "internal.vtu"

    used = []
    _levels_used(monkeypatch, used)
    rec = RS.scope_return(ws, ceiling=int(before * 0.6))

    assert rec and [x["file"] for x in rec["left_out"]] == ["VTK/case_0/internal.vtu"]
    exactly = [p for p, lv in used if p == vtu and lv == RS.MEASURE_LEVEL]
    assert len(exactly) == 1, "the volume export was compressed twice at the exact level"
def test_a_sum_of_file_sizes_is_not_an_upper_bound_on_the_archive(tmp_path):
    """WHY THE FRAMING IS COUNTED. Two hundred one-byte cellSets are 200 bytes of content and far
    more archive than that: every member carries a 512-byte header its own bytes know nothing
    about. The raw-bytes path settles a workspace without compressing anything, so it has to
    bound the archive, and a sum of file sizes does not."""
    d = tmp_path / "constant" / "polyMesh" / "sets"
    d.mkdir(parents=True)
    for i in range(200):
        (d / f"cellSet_{i}").write_bytes(os.urandom(1))
    real = _archive_bytes(tmp_path)
    assert RS.raw_bytes(tmp_path) < real, "a sum of file sizes cannot bound this archive"
    assert RS.raw_bytes(tmp_path) + RS.frame_bytes(tmp_path) >= real
    assert RS.estimate_archive_bytes(tmp_path) >= real
