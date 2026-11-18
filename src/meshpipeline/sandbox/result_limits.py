# Responsibility: Size the extraction limits a RETURNED mesh workspace is opened under, from the cell budget and from what the mesh turned out to be.
# Owns: the per-cell allowances, the reading of a result document's cell count, and result_extraction_limits.
# Boundaries: it decides what a returned archive MAY be, never how it travels; it opens nothing and it knows no transport.
# Collaborates with: sandbox/safe_extract (the limits it widens), settings/policy (CELL_HARD_LIMIT), adapters/mesh_execution/cloud_run_client (extracts with it, and carries the budget to the mesher), engines/return_scope and engines/cfmesh_conjugate/driver (price against it before a mesh is built and before one is sent home).
"""THE CEILING A RETURNED MESH IS OPENED UNDER, and why it is not in the transport.

This is a POLICY the SUBMITTING side owns: how large a workspace it will accept back, given the
cell budget it set and the mesh the runner says it built. Three places have to agree on it -

  * `adapters/mesh_execution/cloud_run_client`, which extracts the archive with it;
  * `engines/return_scope`, which decides on the MESHER's side what to leave out so the archive
    fits (and, when the budget was carried to it, finishes the same sum with the measured count);
  * `engines/cfmesh_conjugate/driver`, which refuses to start a mesh that could not come home.

The last two are product code. They used to reach into the Cloud Run transport module for it -
`from meshpipeline.adapters.mesh_execution.cloud_run_client import result_extraction_limits` -
which is an engine asking the pipe what the caller's rules are, and
`tests/unit/hygiene/test_architecture_boundaries::test_product_and_application_import_no_concrete_adapters`
names it for what it is. The dependency inverts here instead: the rule lives beside
`sandbox/safe_extract`, whose `ExtractionLimits` it returns and whose `default_limits()` is its
floor, and every side reads it from there. The adapter keeps the names it has always exported, so
nothing that extracts or tests through `cloud_run_client` changes spelling.

Nothing in this module imports a transport, a provider SDK or an engine. It is arithmetic over a
result document and two settings.
"""
from __future__ import annotations

#: Compressed bytes a returned workspace carries per mesh cell, with margin. Measured: a 7 M-cell
#: snappy passage came back as 557 MB (80 B a cell, job 3cd77f85), and VMTK's Fluent aorta as
#: 560 MB (lab, 2026-10-04) - both past the generic 512 MiB extraction ceiling, so meshes the cell
#: budget allows were built, paid for and lost on the way back.
RESULT_BYTES_PER_CELL = 100
#: ...and per cell the RESULT DOCUMENT says the mesh has, measured on what came back: compressed,
#: the whole workspace; raw, all of it and its largest single file. A 6.7 M-cell cfMesh polyMesh is
#: 1,236 MB raw (its faces file 699 MB) and 218 MB gzipped (cf_lab tw011_v3); the volume export
#: beside it is most of the rest - the verify-7fix layered lumens came back at 566-696 MB. Each
#: with room: these bound a mesh the run has already built and paid for, not a guess.
RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL = 200
RESULT_RAW_BYTES_PER_MEASURED_CELL = 800
#: AN FEA MESH comes home as a quadratic deck AND its Nastran and Ansys files AND gmsh's own mesh
#: (mesh once, export many): about 1 KB raw an element (SC8UU on dev-areen, 1.5 M elements: .inp
#: 290 MB, .bdf 405 MB, .cdb about 500 MB). Past the flow-mesh allowance above, a valid mesh of a
#: few million elements was built and then refused on the way back. When the mesher measured what
#: it sent (dispatch.returned_bytes) that measurement sizes the limits, with margin; else these.
FEA_RESULT_ARCHIVE_BYTES_PER_ELEMENT = 700
FEA_RESULT_RAW_BYTES_PER_ELEMENT = 1600
FEA_RESULT_FILE_BYTES_PER_ELEMENT = 600
#: The margin on a measured size, and the ceiling no result document can lift the limits past.
FEA_RESULT_MARGIN = 1.15
FEA_RESULT_MAX_BYTES = 48 * 1024 ** 3
RESULT_FILE_BYTES_PER_MEASURED_CELL = 400


def _reported_cells(result: dict | None) -> int:
    """The cell count a mesh runner puts at the TOP of its result, or 0.

    WHERE THE NUMBER ACTUALLY IS. A live conjugate run on Cloud Run reported

        mesh complete: {"rc": 0, ..., "cells": 6582471, "reproducibility": {...},
                        "return_scope": {"ceiling_bytes": 800000000,
                                         "archive_bytes_est": 689484779,
                                         "left_out": [{"file": "VTK/solid/.../internal.vtu",
                                                       "bytes": 330346080}]}}

    6,582,471 is the two regions added up (2,520,586 and 4,061,885) and the runner had already
    put it there. 800,000,000 is CELL_HARD_LIMIT x RESULT_BYTES_PER_CELL - the floor - so the
    count was never read, and the volume export the reviewer slices was dropped to get under it.
    At 6,582,471 cells the ceiling is 1,316,494,200 bytes and the whole workspace, export
    included, is about 791 MB.

    I LOOKED IN TWO PLACES THE RESULT DOES NOT HAVE. `quality.cells` is a single-region engine's
    shape, and `regions[*].cells` is the conjugate RUNNER's shape - but by the time dispatch hands
    the result to return_scope it has been flattened, and the live run above is what says so.
    """
    if not isinstance(result, dict):
        return 0
    try:
        return max(0, int(result.get("cells") or 0))
    except (TypeError, ValueError):
        return 0


def _region_cells(result: dict | None) -> int:
    """The cell count of a result that reports its mesh REGION BY REGION, or 0.

    A conjugate result has no `quality` block at all: the cfmesh_conjugate runner returns
    {"ok", "problems", "notes", "regions": {"fluid": {"cells": ...}, "solid": {"cells": ...}}},
    because a two-region mesh has no single set of quality numbers. This reads that shape, and
    `_reported_cells` reads the shape the result has by the time it reaches return_scope - which
    is the one that was actually failing. So the archive ceiling fell back to its floor on exactly
    the engine that produces the biggest workspaces, and a live run said so:

        return scope: The mesh was too large to send back whole within the transfer limit
        (763 MiB), so the volume export the reviewer slices was left out (330 MB)

    763 MiB is CELL_HARD_LIMIT x RESULT_BYTES_PER_CELL with the count unread. That mesh had
    6,707,503 cells across its two regions, which at RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL is
    1,279 MiB - and the 330 MB export is what a conclusive review is built from, so the figure
    mattered. Same bound as the `quality` path: the count is the remote's word, and the caller
    clamps it to twice CELL_HARD_LIMIT.
    """
    regions = (result or {}).get("regions") if isinstance(result, dict) else None
    if not isinstance(regions, dict):
        return 0
    total = 0
    for one in regions.values():
        if not isinstance(one, dict):
            continue
        try:
            total += max(0, int(one.get("cells") or 0))
        except (TypeError, ValueError):
            continue
    return total


def result_extraction_limits(result: dict | None = None, *, cell_hard_limit: int | None = None):
    """The extraction limits for a RETURNED mesh workspace: the generic limits, with the archive
    ceiling sized from the cell budget - CELL_HARD_LIMIT cells at RESULT_BYTES_PER_CELL (800 MB
    at the default 8 M) - and, when the remote's result document says how many cells the mesh
    has, from that count too (the archive, the expanded total and the largest file each at their
    RESULT_*_PER_MEASURED_CELL). Never below the generic limits; entries, depth, ratio and member
    types are unchanged.

    `cell_hard_limit` overrides this process's own budget, and exists for ONE caller: the mesh VM,
    which runs `engines/return_scope` against the budget the submitting side carried to it as a
    per-run container override (contracts/mesh_execution.RETURN_BUDGET_ENV). Without it that side
    computes this whole thing off the 8,000,000
    default while the process that will really do the extracting uses the configured number, and
    the two disagree by whatever the operator set - measured at 17.7x on job 0ff6f9e5, which cost
    that run its fluid interior. Omitted everywhere else, which is this process's own budget."""
    from dataclasses import replace

    import meshpipeline.settings.policy as polcfg
    from meshpipeline.sandbox.safe_extract import default_limits
    base = default_limits()
    hard = int(cell_hard_limit or 0) or int(polcfg.CELL_HARD_LIMIT)
    by_budget = hard * RESULT_BYTES_PER_CELL
    q = (result or {}).get("quality") if isinstance(result, dict) else None
    try:
        cells = max(0, int((q or {}).get("cells") or 0)) if isinstance(q, dict) else 0
    except (TypeError, ValueError):
        cells = 0
    cells = cells or _reported_cells(result) or _region_cells(result)
    # the count is the remote's word: bounded, so no document can lift the limits without end
    cells = min(cells, 2 * hard)
    archive = max(base.max_archive_bytes, by_budget, cells * RESULT_ARCHIVE_BYTES_PER_MEASURED_CELL)
    raw = max(base.max_total_uncompressed_bytes, cells * RESULT_RAW_BYTES_PER_MEASURED_CELL)
    largest = max(base.max_file_bytes, cells * RESULT_FILE_BYTES_PER_MEASURED_CELL)
    measured = (result or {}).get("returned_bytes") if isinstance(result, dict) else None
    fea = (isinstance(measured, dict) and bool(measured.get("fea"))) or (
        isinstance(q, dict) and (str(q.get("quality_bar") or "") == "fea" or isinstance(q.get("fea"), dict)))
    if fea:
        # AN FEA MESH IS NEVER LOST TO SIZE (Areen: only a broken mesh fails): its own allowance, and
        # the mesher's measurement of what it sent when there is one - bounded, so no document can
        # lift the limits without end
        archive = max(archive, cells * FEA_RESULT_ARCHIVE_BYTES_PER_ELEMENT)
        raw = max(raw, cells * FEA_RESULT_RAW_BYTES_PER_ELEMENT)
        largest = max(largest, cells * FEA_RESULT_FILE_BYTES_PER_ELEMENT)
        if isinstance(measured, dict):
            try:
                m_raw, m_big = int(measured.get("raw") or 0), int(measured.get("largest") or 0)
            except (TypeError, ValueError):
                m_raw = m_big = 0
            raw = max(raw, int(m_raw * FEA_RESULT_MARGIN))
            archive = max(archive, int(m_raw * FEA_RESULT_MARGIN))      # gzip never grows text much
            largest = max(largest, int(m_big * FEA_RESULT_MARGIN))
        archive, raw, largest = (min(v, FEA_RESULT_MAX_BYTES) for v in (archive, raw, largest))
    return replace(base, max_archive_bytes=archive, max_total_uncompressed_bytes=raw,
                   max_file_bytes=largest)
