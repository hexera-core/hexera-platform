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


#: This process standing in for a mesh VM: the 8,000,000-cell default, because CELL_HARD_LIMIT is
#: read from the SUBMITTING side's settings and the VM is never passed it.
VM_DEFAULT_BUDGET = 8_000_000
#: ...and what the operator really set, which is the number the side that extracts will use.
HOME_BUDGET = 400_000_000


def _as_a_mesh_vm(monkeypatch, *, carried: str | None) -> None:
    """Run the body as the mesh VM runs it: this process's own budget is the bare default, and
    `carried` is what the submitting side told it (None: nothing was carried)."""
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.contracts.mesh_execution import RETURN_BUDGET_ENV

    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", VM_DEFAULT_BUDGET)
    if carried is None:
        monkeypatch.delenv(RETURN_BUDGET_ENV, raising=False)
    else:
        monkeypatch.setenv(RETURN_BUDGET_ENV, carried)


def test_the_budget_the_submitting_side_carried_is_the_one_priced_against(tmp_path, monkeypatch):
    """The mesh VM has no CELL_HARD_LIMIT of its own, so it must be TOLD the budget rather than
    derive a ceiling from a default that is 50x too small."""
    from meshpipeline.contracts.mesh_execution import RETURN_BUDGET_ENV
    ws = _ws(tmp_path)
    (ws / "_rescue").mkdir()                       # so a record is written at all
    _as_a_mesh_vm(monkeypatch, carried=str(HOME_BUDGET))

    rec = RS.scope_return(ws)

    assert rec["ceiling_bytes"] == RS.archive_ceiling(None, cell_hard_limit=HOME_BUDGET)
    assert rec["ceiling_bytes"] > RS.archive_ceiling(None), (
        "the carried budget changed nothing - this is the 17.7x that destroyed an interior")
    # WHERE THE CEILING CAME FROM, so a reader never has to guess which number this run pruned
    # against.
    assert rec["ceiling_source"] == RETURN_BUDGET_ENV


def test_nothing_carried_leaves_the_prune_on_this_side_s_own_number(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    (ws / "_rescue").mkdir()
    _as_a_mesh_vm(monkeypatch, carried=None)

    rec = RS.scope_return(ws)

    assert rec["ceiling_bytes"] == RS.archive_ceiling(None)
    assert "no " in rec["ceiling_source"] and "was carried" in rec["ceiling_source"]


def test_a_carried_budget_that_is_nonsense_is_ignored_rather_than_obeyed(tmp_path, monkeypatch):
    """A budget read as 0 prices the ceiling at its floor and gives up every optional file in the
    workspace, which is the exact damage this exists to stop."""
    ws = _ws(tmp_path)
    for junk in ("0", "-1", "lots", "", "8e6"):
        _as_a_mesh_vm(monkeypatch, carried=junk)
        assert RS.carried_ceiling() is None, junk
    _as_a_mesh_vm(monkeypatch, carried=None)
    assert RS.carried_ceiling() is None
    assert ws.is_dir()


def test_an_explicit_ceiling_still_beats_the_carried_one(tmp_path, monkeypatch):
    """A caller that passes one knows better: tests and the dispatch route that measured it."""
    ws = _ws(tmp_path)
    _as_a_mesh_vm(monkeypatch, carried=str(HOME_BUDGET))
    rec = RS.scope_return(ws, ceiling=1000)
    assert rec and rec["ceiling_bytes"] == 1000
