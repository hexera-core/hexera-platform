"""The return gives files up only when giving them up achieves something, and against the ceiling
the caller will really use.

TWO DEFECTS, ONE RUN. Job 0ff6f9e5, 2026-11-16, a 283,410,955-cell conjugate plate. Its own
return_scope.json:

    {"ceiling_bytes": 3200000000, "archive_bytes_est": 11406088609, "measure": "exact",
     "left_out": [{"file": "VTK/fluid/cr_ws_vgcm8hv7_0/internal.vtu", "bytes": 10366611525}],
     "fits": false}

1. THE CEILING WAS NOT THE REAL ONE. `archive_ceiling` works it out from
   `result_extraction_limits`, which is built on `CELL_HARD_LIMIT` - and that is
   `optional_env("CELL_HARD_LIMIT", "8000000")`, which THE MESH VM IS NEVER PASSED. So the VM
   computed max(536870912, 8e6 x 100, min(283410955, 16e6) x 200) = 3,200,000,000 while the side
   that would actually extract it would have allowed 283410955 x 200 = 56,682,191,000. 17.7x.

2. THE PRUNE DID NOT EVEN WORK. "fits": false - the archive was still over the ceiling after the
   interior was gone - and the 11.4 GB archive was then sent and accepted anyway. The only thing
   the prune achieved was deleting the one artifact a conjugate review can reach a verdict from.

AND THE TRAP IN FIXING IT, which is why the precheck is written against `cap` and not `bar`. In
the band between HEADROOM x cap and cap, today's loop prunes, lands under cap and IS accepted. A
precheck written against bar would call that case hopeless, keep the files, and ship an archive
over cap that `sandbox/safe_extract` rejects outright - losing the polyMesh and the whole mesh
with it. HEADROOM is what the loop aims for, never the test for whether to try.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import meshpipeline.engines.return_scope as RS


def _ws(tmp_path: Path, *, volume_bytes: int = 3_000_000) -> Path:
    pm = tmp_path / "constant" / "polyMesh"
    pm.mkdir(parents=True)
    (pm / "owner").write_text("\n".join(str(i % 977) for i in range(200_000)))
    (pm / "points").write_text("\n".join(f"({i * 0.001:.6f} 0.1 0.2)" for i in range(50_000)))
    vtk = tmp_path / "VTK" / "case_0"
    (vtk / "boundary").mkdir(parents=True)
    (vtk / "internal.vtu").write_bytes(os.urandom(volume_bytes))
    (vtk / "boundary" / "wall.vtp").write_bytes(os.urandom(50_000))
    (tmp_path / "mesh_quality.json").write_text(json.dumps({"cells": 1}))
    return tmp_path


def test_a_prune_that_cannot_reach_the_ceiling_keeps_the_volume(tmp_path):
    """Job 0ff6f9e5's shape: the archive is over the ceiling with the volume and over it without.

    The volume buys nothing by going, so it stays and the run says why.
    """
    ws = _ws(tmp_path)
    vtu = ws / "VTK" / "case_0" / "internal.vtu"
    rec = RS.scope_return(ws, ceiling=1000)
    assert rec and not rec["fits"]
    assert vtu.exists(), "the volume export was deleted for a prune that could not work"
    assert rec["left_out"] == []
    assert "prune_declined" in rec and "either way" in rec["prune_declined"]
    # the mesh itself was never at risk and still is not
    assert (ws / "constant" / "polyMesh" / "owner").exists()


def test_a_prune_that_can_reach_the_ceiling_still_happens(tmp_path):
    """The band the precheck must not swallow: pruning lands the archive under cap, so it runs."""
    ws = _ws(tmp_path)
    vtu = ws / "VTK" / "case_0" / "internal.vtu"
    before = RS.estimate_archive_bytes(ws)
    share = RS.compressed_size(vtu)
    # a ceiling the archive clears ONLY once the volume is given up
    cap = int(before - share / 2)
    assert RS.HEADROOM * cap < before, "the workspace already fits: nothing to prune"
    assert before - share <= cap, "even pruning cannot reach this cap: wrong band"

    rec = RS.scope_return(ws, ceiling=cap)

    assert rec and [x["file"] for x in rec["left_out"]] == ["VTK/case_0/internal.vtu"]
    assert not vtu.exists()
    assert rec["fits"], "pruned, under the cap, and still reported as not fitting"
    assert "prune_declined" not in rec


def test_the_ceiling_the_submitting_side_carried_is_the_one_used(tmp_path):
    """The mesh VM has no CELL_HARD_LIMIT, so it must be told the ceiling rather than derive one."""
    ws = _ws(tmp_path)
    vtu = ws / "VTK" / "case_0" / "internal.vtu"
    before = RS.estimate_archive_bytes(ws)
    (ws / RS.CEILING_FILE).write_text(json.dumps({"max_archive_bytes": 50 * before}))

    rec = RS.scope_return(ws)

    assert vtu.exists(), "pruned against a computed ceiling while the real one was in the workspace"
    assert rec is None or rec["left_out"] == []
    assert RS.carried_ceiling(ws) == 50 * before


def test_a_carried_ceiling_is_recorded_so_a_reader_knows_which_one_was_used(tmp_path):
    ws = _ws(tmp_path)
    (ws / "_rescue").mkdir()                       # so a record is written at all
    (ws / RS.CEILING_FILE).write_text(json.dumps({"max_archive_bytes": 9_000_000_000}))
    rec = RS.scope_return(ws)
    assert rec["ceiling_bytes"] == 9_000_000_000
    assert rec["ceiling_source"] == RS.CEILING_FILE


def test_a_ceiling_file_that_is_nonsense_is_ignored_rather_than_obeyed(tmp_path):
    """A ceiling read as 0 would give up every optional file in the workspace, which is the exact
    damage this exists to stop."""
    ws = _ws(tmp_path)
    for junk in ('{"max_archive_bytes": 0}', '{"max_archive_bytes": "lots"}', "not json at all"):
        (ws / RS.CEILING_FILE).write_text(junk)
        assert RS.carried_ceiling(ws) is None, junk
    (ws / RS.CEILING_FILE).unlink()
    assert RS.carried_ceiling(ws) is None


def test_an_explicit_ceiling_still_beats_the_carried_one(tmp_path):
    """A caller that passes one knows better: tests and the dispatch route that measured it."""
    ws = _ws(tmp_path)
    (ws / RS.CEILING_FILE).write_text(json.dumps({"max_archive_bytes": 10**12}))
    rec = RS.scope_return(ws, ceiling=1000)
    assert rec and rec["ceiling_bytes"] == 1000
