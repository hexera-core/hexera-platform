"""The mesher prunes against the ceiling the SUBMITTING side will extract under, not its own.

THE READER HAS BEEN IN THE TREE SINCE 2026-11-16 AND NOTHING EVER WROTE THE FILE. `return_scope`
says in its own text why the ceiling has to be carried: it is `result_extraction_limits`, which is
CELL_HARD_LIMIT cells at a rate, and CELL_HARD_LIMIT is read from the SUBMITTING side's settings.
The mesh VM is never passed it, so the VM runs the whole calculation off the 8,000,000 default
while the side that will really do the extracting runs off whatever the operator set.

MEASURED, job 0ff6f9e5, 2026-11-16, a 283,410,955-cell plate:

    max(536870912, 8e6 x 100, min(283410955, 16e6) x 200) = 3,200,000,000   on the VM
    283410955 x 200                                       = 56,682,191,000  at home

17.7 times larger. The VM gave up the 10,366,611,525-byte fluid interior to get under a ceiling
that was never the real one, recorded "fits": false because the prune did not even work, and the
11.4 GB archive was sent and accepted anyway.

AND WHAT IS CARRIED IS THE CELL BUDGET, NOT A FINISHED CEILING. The real number depends on how
many cells the mesh HAS, which only the mesher knows when it is done - so a number computed at
submission time is the 800 MB floor and nothing else.
"""
from __future__ import annotations

import io
import json
import tarfile

from meshpipeline.adapters.mesh_execution import cloud_run_client as CRC
from meshpipeline.engines import return_scope as RS

PLATE = {"quality": {"cells": 283_410_955}}


def _carried(tmp_path):
    (tmp_path / "case.txt").write_text("a case")
    with tarfile.open(fileobj=io.BytesIO(CRC._tar_dir(tmp_path))) as tf:
        names = sorted(tf.getnames())
        body = json.loads(tf.extractfile(RS.CEILING_FILE).read())
    return names, body


def test_the_submission_carries_the_ceiling_the_caller_will_extract_under(tmp_path):
    names, body = _carried(tmp_path)
    assert RS.CEILING_FILE in names, names
    assert "case.txt" in names, "the case still travels"
    assert body["cell_hard_limit"] > 0
    assert body["max_archive_bytes"] > 0
    assert body["why"]


def test_the_mesher_finishes_the_sum_with_its_own_measured_cell_count(tmp_path):
    """The budget is the one input the VM is missing; the cell count is the one the caller is
    missing. Each side has half, so the carried half is what makes the two agree."""
    (tmp_path / RS.CEILING_FILE).write_text(json.dumps(
        {"cell_hard_limit": 141_705_477, "max_archive_bytes": 800_000_000}))
    got = RS.carried_ceiling(tmp_path, result=PLATE)
    home = CRC.result_extraction_limits(PLATE, cell_hard_limit=141_705_477).max_archive_bytes
    assert got == home
    assert got > 800_000_000, "the floor alone is the number that destroyed an interior"


def test_a_file_that_carries_only_a_number_still_works(tmp_path):
    """The shape the reader was written for. Nothing ever wrote it, but a hand-written one still
    has to be honoured rather than ignored."""
    (tmp_path / RS.CEILING_FILE).write_text(json.dumps({"max_archive_bytes": 9_000_000_000}))
    assert RS.carried_ceiling(tmp_path, result=PLATE) == 9_000_000_000


def test_nonsense_falls_back_to_a_computed_ceiling(tmp_path):
    """A ceiling read as 0 would give up every optional file in the workspace, which is the exact
    damage this exists to stop."""
    for junk in ('{"max_archive_bytes": 0}', '{"max_archive_bytes": "lots"}', "not json", "{}",
                 '{"cell_hard_limit": 0}'):
        (tmp_path / RS.CEILING_FILE).write_text(junk)
        assert RS.carried_ceiling(tmp_path, result=PLATE) is None, junk
    (tmp_path / RS.CEILING_FILE).unlink()
    assert RS.carried_ceiling(tmp_path, result=PLATE) is None


def test_the_caller_s_own_budget_is_what_is_used_when_nothing_is_carried():
    """And this is the 17.7x: the same mesh, priced by a side that was never told the budget."""
    vm = CRC.result_extraction_limits(PLATE, cell_hard_limit=8_000_000).max_archive_bytes
    home = CRC.result_extraction_limits(PLATE, cell_hard_limit=141_705_477).max_archive_bytes
    assert vm == 3_200_000_000, vm
    assert home / vm > 17.0, (home, vm)
