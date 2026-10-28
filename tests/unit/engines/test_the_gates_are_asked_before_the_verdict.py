"""A mesh that came out is handed to the gates, not judged before they are asked.

WHY THIS TEST EXISTS. pipeline/executor.py runs an engine's declared gate row inside
`if executor_success:`, and executor_success is whatever that engine's finalize returns as
`success`. This engine returned its ENTIRE mesh_ok verdict there, so on any mesh it had already
rejected the gate row never executed at all.

MEASURED on the first real customer cold plate, 2026-10-28. The build completed: cartesianMesh,
topoSet, splitMeshRegions and both per-region checkMesh runs all finished, leaving 99,172,391 cells
(fluid 40,099,581 / solid 59,072,810) with a conformal 7,224,348-face interface, and checkMesh
reported Mesh OK for both regions with no fatal classes. At 0.0947 mm cells the part's 0.098 mm
microchannel slots got 1.03 cells across, so the coolant came out in 367 disconnected pieces.
mesh_ok went False - and not one of the eight gates ran. They did not fail; they never executed.
The run was then reported as a mesher that "stopped before it finished writing the mesh", which is
the one thing that had not happened.

cfmesh, snappy, gmsh and snappy_multiregion all return the narrow "a mesh came out and checkMesh
found nothing fatal" (cfmesh/finalize.py: `success = not fatal`). This engine now does the same.
"""
import json
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import gates as G
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_REGION, SOLID_REGION

from test_conjugate_row import REPORT, _Ctx, _manifest, case  # noqa: F401


def _cut_into_pieces(n: int) -> dict:
    """The customer run's shape: a complete mesh whose coolant is in n pieces, not 1."""
    report = json.loads(json.dumps(REPORT))
    report["regions"][FLUID_REGION]["pieces"] = n
    report["ok"] = False
    report["problems"] = [f"fluid is in {n} disconnected piece(s), expected 1"]
    return report


def test_a_mesh_cut_into_pieces_is_still_handed_to_the_gates(case):  # noqa: F811
    """`success` is what switches the gate row on, so it must say a mesh came out - nothing more."""
    q = F.quality_of(case, _cut_into_pieces(367))
    assert q["mesh_ok"] is False, "the engine still knows the mesh is wrong"
    assert not q["fatal"], "checkMesh found nothing fatal on this mesh - that is the whole point"
    assert q["cells"] > 0 and q["regions"], "a mesh was delivered"
    # THE CODE'S OWN ANSWER, not this test's restatement of it. a_mesh_came_out is what
    # finalize returns as `success`, and `success` is what the executor reads to decide whether the
    # gate row runs. An earlier version of this assertion recomputed the expression here, which
    # would have passed whatever the code did.
    assert F.a_mesh_came_out(q) is True


def test_the_pieces_gate_is_the_one_that_names_the_defect(case):  # noqa: F811
    """And it says it in the engineer's language, where the boolean said nothing at all."""
    man = _manifest(case)
    man["quality"]["coolant_pieces"] = 367
    man["quality"]["coolant_pieces_expected"] = 2
    (case / "mesh_manifest.json").write_text(json.dumps(man))
    gate = next(g for g in G.CONJUGATE_GATES if g.key == "coolant_pieces")
    ok, said = gate.check(_Ctx(case, man))
    assert not ok, "the gate that exists for exactly this defect has to refuse it"
    assert "367" in said and "2" in said
    assert "REGION_SPLIT_FAILED" in said


def test_a_workspace_with_no_mesh_is_not_handed_to_the_gates(case):  # noqa: F811
    """The narrow reading still has to keep an empty workspace OUT of the row, so the executor's
    no-mesh path can say which of the four things happened instead of a gate crashing on nothing."""
    report = json.loads(json.dumps(REPORT))
    report["regions"] = {}
    assert F.a_mesh_came_out(F.quality_of(case, report)) is False


def test_a_fatal_checkmesh_class_still_fails_before_the_gates(case):  # noqa: F811
    """What `success` keeps: a fatal class means the mesh is not fit to be measured further, which
    is the same line cfmesh draws."""
    report = json.loads(json.dumps(REPORT))
    report["regions"][SOLID_REGION]["fatal"] = ["negative-volume cells"]
    q = F.quality_of(case, report)
    assert q["fatal"], "a fatal class has to reach the quality block"
    assert F.a_mesh_came_out(q) is False


def test_every_conjunct_of_the_old_verdict_has_a_gate():
    """The reason narrowing `success` loses no safety: each thing mesh_ok folded in is checked by a
    named gate that carries its own FailureCause, where the boolean carried none."""
    keys = {g.key for g in G.CONJUGATE_GATES}
    for needed in ("coolant_pieces", "regions_split", "interface", "named_patches",
                   "quality_floor"):
        assert needed in keys, f"nothing would check {needed} once success stops folding it in"


@pytest.mark.parametrize("engine", ["cfmesh", "snappy"])
def test_the_other_engines_already_read_success_narrowly(engine):
    """Not a new idea - the engines that were not affected by this already do it this way."""
    src = Path(F.__file__).parent.parent / engine / "finalize.py"
    body = src.read_text(encoding="utf-8")
    assert '"success": not fatal' in body, (
        f"{engine} was the precedent for the narrow reading; if it has changed, this engine's "
        "reasoning needs revisiting")
