"""The mesher prunes against the ceiling the SUBMITTING side will extract under, not its own.

THE READER HAS BEEN IN THE TREE SINCE 2026-11-16 AND NOTHING EVER TOLD IT. `return_scope` says in
its own text why the budget has to be carried: the ceiling is `result_extraction_limits`, which is
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

IT RIDES ON THE INVOCATION CHANNEL AND NOT IN THE ARCHIVE, and the first version of this test
pinned the wrong carrier. The budget was written into the submission tar as `return_ceiling.json`
at archive time. That tar is the case plus the facts the workspace declares, and the SAME
enumerator (`contracts/mesh_execution.submission_payload_files`) scopes both it and the claim's
payload digest - so a synthesised member put bytes in the upload that the submission's identity
never saw, and tests/unit/platform/test_submission_payload_scope said so in two places at once
("the archive is the case plus the facts", "the digest scope is the tar scope by construction").
The budget is this side's CONFIGURATION rather than part of the case, so it goes where this side's
other per-run configuration already goes: one more container override, beside TIMEOUT.
"""
from __future__ import annotations

import io
import tarfile

from meshpipeline.adapters.mesh_execution import cloud_run_client as CRC
from meshpipeline.adapters.mesh_execution import gce_vm as VM
from meshpipeline.contracts.mesh_execution import RETURN_BUDGET_ENV
from meshpipeline.sandbox import result_limits as RL

PLATE = {"quality": {"cells": 283_410_955}}


def _overrides() -> dict[str, str]:
    return {e["name"]: e["value"] for e in CRC.return_budget_overrides()}


def test_the_submission_carries_the_budget_the_caller_will_extract_under(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 141_705_477)

    assert _overrides() == {RETURN_BUDGET_ENV: "141705477"}


def test_the_budget_rides_beside_the_case_and_never_inside_it(tmp_path):
    """THE ARCHIVE IS THE CASE AND THE FACTS. Nothing this side's configuration knows about may
    appear in it: the payload declaration scopes the tar and the claim's digest together, and a
    member neither of them named breaks that by construction."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "case.txt").write_text("a case")
    with tarfile.open(fileobj=io.BytesIO(CRC._tar_dir(ws))) as tf:
        names = sorted(tf.getnames())

    assert names == ["case.txt"], names
    assert not any("ceiling" in n for n in names)


def test_both_dispatch_routes_carry_it(monkeypatch):
    """The Cloud Run job through `containerOverrides`, a VM through its instance metadata and the
    same `-e`. The plate that lost its interior ran on the VM route, so carrying it on one of the
    two would have fixed nothing."""
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 141_705_477)

    body = VM.instance_body(name="m", zone="z", machine="n2-highmem-64", image="img",
                            input_uri="gs://i", output_uri="gs://o", engine="cfmesh_conjugate",
                            timeout=600, disk_gb=50, account="", workspace_gib=10)
    meta = {item["key"]: item["value"] for item in body["metadata"]["items"]}

    assert meta["return-cell-budget"] == "141705477"
    assert "-e HEXERA_RETURN_CELL_BUDGET=" in VM.STARTUP_SCRIPT
    assert RETURN_BUDGET_ENV == "HEXERA_RETURN_CELL_BUDGET", (
        "the startup script spells the name literally - it is a shell file, not Python")


def test_a_budget_this_side_cannot_read_is_simply_not_carried(monkeypatch):
    """NON-FATAL: a submission is never lost over a setting. Without it the mesher computes its
    own ceiling exactly as it did before."""
    import meshpipeline.settings.policy as polcfg
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", 0)
    assert CRC.return_budget_overrides() == []
    monkeypatch.setattr(polcfg, "CELL_HARD_LIMIT", "not a number")
    assert CRC.return_budget_overrides() == []


def test_the_mesher_finishes_the_sum_with_its_own_measured_cell_count(monkeypatch):
    """The budget is the one input the VM is missing; the cell count is the one the caller is
    missing. Each side has half, so the carried half is what makes the two agree."""
    from meshpipeline.engines import return_scope as RS
    monkeypatch.setenv(RETURN_BUDGET_ENV, "141705477")

    got = RS.carried_ceiling(PLATE)
    home = RL.result_extraction_limits(PLATE, cell_hard_limit=141_705_477).max_archive_bytes

    assert got == home
    assert got > 800_000_000, "the floor alone is the number that destroyed an interior"


def test_nonsense_falls_back_to_a_computed_ceiling(monkeypatch):
    """A ceiling read as 0 would give up every optional file in the workspace, which is the exact
    damage this exists to stop."""
    from meshpipeline.engines import return_scope as RS
    for junk in ("0", "-1", "lots", "", "1e8", "8000000.0"):
        monkeypatch.setenv(RETURN_BUDGET_ENV, junk)
        assert RS.carried_ceiling(PLATE) is None, junk
    monkeypatch.delenv(RETURN_BUDGET_ENV, raising=False)
    assert RS.carried_ceiling(PLATE) is None


def test_the_caller_s_own_budget_is_what_is_used_when_nothing_is_carried():
    """And this is the 17.7x: the same mesh, priced by a side that was never told the budget."""
    vm = RL.result_extraction_limits(PLATE, cell_hard_limit=8_000_000).max_archive_bytes
    home = RL.result_extraction_limits(PLATE, cell_hard_limit=141_705_477).max_archive_bytes
    assert vm == 3_200_000_000, vm
    assert home / vm > 17.0, (home, vm)
