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
