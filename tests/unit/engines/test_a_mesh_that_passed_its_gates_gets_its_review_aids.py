"""A mesh is "refused" for the review aids only when it is not a mesh (cfmesh_conjugate.native).

THE RUN THAT PAID FOR THIS. Job 57b613d7, 2026-11-15, ColdPlateAssembly_CLEAN.step: 283,410,955
cells, fluid 91,323,244 in 2 pieces and solid 192,087,711, a conformal 16,582,198-face interface,
`gate_declared_criteria` PASSED, every declared gating criterion passed, and the executor recorded
`success=True`. The reviewer then STARTED for the first time in eleven jobs and immediately refused:

    Reviewer: visual verification unavailable (reviewer_evidence_missing):
              required artifact 'mesh_paths.surface' does not exist

because `_review_aids` had been called with `refused=bool(report.problems)`, and `report.problems`
held the two lines that gate nothing - "fluid: checkMesh did not report Mesh OK" and the same for
the solid. Above VOLUME_SKIP_ABOVE_CELLS a refused mesh writes no volume, the surface is built from
that export, and the manifest then names a mesh.msh nobody wrote. The engine's own recorded reason
says it in as many words: "the mesh was refused and has 283,410,955 cells, so the volume export ...
is not written".

NEITHER REGION OF A GOOD CONJUGATE MESH REPORTS MESH OK. Both fail the same five -allGeometry
checks, the solid is a zero-layer control arm that fails them too, and there is no `mesh_ok`
criterion in this engine's CRITERIA_ROWS. So that reading can never be the one that decides whether
a mesh deserves a picture of itself.
"""
from __future__ import annotations

import inspect

from meshpipeline.engines.cfmesh_conjugate import native as N


def _src() -> str:
    return inspect.getsource(N)


def test_the_two_mesh_ok_lines_do_not_decide_whether_the_aids_are_built():
    """The whole defect in one assertion: `report.problems` must not be the test."""
    src = _src()
    assert "refused=bool(report.problems)" not in src, (
        "a mesh that passes every gate carries those two lines and is not refused")
    assert "_review_aids(ws, bashrc=bashrc, refused=_no_mesh or _fatal" in src


def test_refused_means_there_is_no_mesh_to_review():
    """The same test `finalize.a_mesh_came_out` uses, which is the line this engine already drew
    once for the run's success and did not draw here."""
    src = _src()
    at = src.index("_no_mesh = ")
    block = src[at:at + 400]
    assert "report.fluid.cells" in block and "report.solid.cells" in block
    assert "report.fluid.fatal" in block and "report.solid.fatal" in block
    # and nothing else sneaks back in
    assert "problems" not in block


def test_the_success_rule_the_sizes_guard_states_is_the_one_applied():
    """`VOLUME_ALWAYS_ON_SUCCESS` says "the mesh passed its own gates, so the volume export is
    written at any size". It was never reached for a 283M-cell mesh that did pass them."""
    assert "passed its own gates" in N.VOLUME_ALWAYS_ON_SUCCESS
    assert N.VOLUME_SKIP_ABOVE_CELLS == 40_000_000


def test_a_mesh_with_a_fatal_class_is_still_refused():
    """The guard that stays: a fatal checkMesh class means there is nothing worth exporting, and
    that is the same bar `finalize.a_mesh_came_out` keeps."""
    src = _src()
    at = src.index("_fatal = ")
    assert "report.fluid.fatal" in src[at:at + 120]


def test_an_empty_region_is_refused_too():
    src = _src()
    at = src.index("_no_mesh = ")
    assert "not (" in src[at:at + 120], "no cells in either region is not a mesh"
