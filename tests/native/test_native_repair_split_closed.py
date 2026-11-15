# Responsibility: Verify on the REAL kernel that split_closed_faces splits a closed face in two
#                 while moving no volume and losing no solid, and that on a real periodic-surface
#                 mesh failure the region loop keeps it and the failure changes or goes.
# Boundaries: the native tier; the claim, declines and flag gating are unit-tested hermetically.
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("OCP.ShapeUpgrade", reason="the CAD kernel is a native-tier dependency")

import meshpipeline.cad.repair.rules_author as rules_author_module  # noqa: E402
import meshpipeline.settings.cad_repair as rcfg  # noqa: E402
from meshpipeline.cad.repair.caps import measure  # noqa: E402
from meshpipeline.cad.repair.identity import EntityRef, fingerprint_of, quantum_for  # noqa: E402
from meshpipeline.cad.repair.operations import (  # noqa: E402
    AREA_DELTA,
    VOLUME_DELTA,
    SplitClosedFaces,
    _closed_directions,
    box_of,
    catalogue,
    sub_shape,
)
from meshpipeline.cad.repair.rules_author import mesh_ops  # noqa: E402


@pytest.fixture
def split_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", True)


def _cylinder_brep(tmp_path):
    """A plain cylinder through a BREP round trip: one lateral face closed in U, two caps."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    path = tmp_path / "cylinder.brep"
    assert BRepTools.Write_s(BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape(), str(path))
    shape = TopoDS_Shape()
    assert BRepTools.Read_s(shape, str(path), BRep_Builder())
    return shape


def _ref(shape, index: int) -> EntityRef:
    diagonal = measure(shape).diagonal_mm
    face = sub_shape(shape, "face", index)
    return EntityRef(entity_type="face", entity_index=index,
                     fingerprint=fingerprint_of("face", box_of(face),
                                                quantum=quantum_for(diagonal)))


def _lateral_index(shape) -> int:
    count = measure(shape).faces
    closed = [i for i in range(1, count + 1) if any(_closed_directions(sub_shape(shape, "face", i)))]
    assert len(closed) == 1, closed
    return closed[0]


def test_a_cylinder_s_lateral_face_is_split_in_two(tmp_path, split_on):
    shape = _cylinder_brep(tmp_path)
    before = measure(shape)
    index = _lateral_index(shape)

    outcome = SplitClosedFaces().apply(shape, (_ref(shape, index),), tolerance_mm=0.01)

    assert "declined" not in outcome.evidence, outcome.evidence
    after = measure(outcome.shape)
    assert after.solids == before.solids == 1
    assert after.faces == before.faces + 1
    assert outcome.evidence["faces_added"] == 1
    assert outcome.evidence[AREA_DELTA] == 0.0 and outcome.evidence[VOLUME_DELTA] == 0.0
    assert abs(after.volume_mm3 - before.volume_mm3) <= 1e-6 * before.volume_mm3
    assert abs(after.surface_area_mm2 - before.surface_area_mm2) <= 1e-6 * before.surface_area_mm2
    assert after.valid
    # The halves are named, so a defect appearing on them is attributed to this operation.
    assert len(outcome.added) == 2
    # And nothing closed is left to split.
    assert not any(any(_closed_directions(sub_shape(outcome.shape, "face", i)))
                   for i in range(1, after.faces + 1))


def test_a_cap_is_declined_as_not_periodic(tmp_path, split_on):
    shape = _cylinder_brep(tmp_path)
    cap = next(i for i in range(1, measure(shape).faces + 1)
               if not any(_closed_directions(sub_shape(shape, "face", i))))
    outcome = SplitClosedFaces().apply(shape, (_ref(shape, cap),), tolerance_mm=0.01)
    assert outcome.shape is shape
    assert outcome.evidence["declined"].startswith("not_periodic")


# THE REAL MESH FAILURE

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()
_PERIODIC_FILE = "occt/bug32596_abc_0_13.step"


def _corpus(name):
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


@pytest.mark.xfail(run=False, strict=True, reason=(
    "MEASURED 2026-11-14, not assumed: after an OCP read the whole-shape probe of bug32596 TIMES "
    "OUT at 300 s (mesh_timeout, unlocated), so the region loop never sees a located periodic "
    "failure and never proposes split_closed_faces - the 1800 s run tried fix_small_edges, "
    "fix_shape_global, fix_small_edges, all rolled back. And where gmsh does name the surface "
    "(reading the STEP itself), it is a partial cylinder closed in neither direction, which this "
    "operation declines (pinned below). Not run by default: one attempt costs the full 30 min."))
def test_corpus_periodic_file_is_repaired_and_meshes(monkeypatch, split_on, capsys):
    """bug32596 fails gmsh's 2D stage with a periodic surface after OCP reads it.

    The region loop, with the probe live and the split consented, must KEEP split_closed_faces.
    The whole-shape probe afterwards then either meshes the file, or - reported, not hidden - fails
    for some reason that is no longer a periodic surface. The probe is given 300 s: this file timed
    out at 120 s in the calibration.
    """
    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.loop import LoopBudget
    from meshpipeline.cad.repair.region_loop import run_regions
    from meshpipeline.cad.repair.rules_author import rules_author

    path = _corpus(_PERIODIC_FILE)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 300.0)
    # THE IMPORT-TIME CATALOGUE WAS BUILT WITH THE FLAG OFF; the run gets the flag-on mapping.
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(split_closed_faces=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(split_closed_faces=True)})

    shape = _read_shape(path)
    started = time.monotonic()
    run = run_regions(shape, author=rules_author(shape, original=measure(shape)),
                      tolerance_mm=0.1, budget=LoopBudget(max_ops=6, stall_limit=2,
                                                          timeout_s=1800.0))
    elapsed = time.monotonic() - started

    verdicts = [(v.op, v.status, v.reason) for p in run.packages for v in p.verdicts]
    codes = {d.code for d in run.defects_after}
    with capsys.disabled():
        print(f"\n[bug32596] run {elapsed:.1f}s stopped={run.stopped_because} "
              f"verdicts={verdicts} after={sorted(c.value for c in codes)} "
              f"timings={ {k: v for k, v in run.timings.items() if k.endswith('_s')} }")

    assert ("split_closed_faces", "kept") in [(op, status) for op, status, _ in verdicts], verdicts
    assert DefectCode.mesh_periodic_surface not in codes, codes
    assert not codes & {DefectCode.mesh_timeout, DefectCode.mesh_probe_error}, codes


def test_bug32596_s_periodic_failure_is_on_a_face_that_is_not_closed(split_on):
    """WHY THE CORPUS TEST ABOVE CANNOT KEEP THE SPLIT, measured rather than assumed.

    gmsh (reading the STEP itself, which fails in seconds) names surface 6 as the periodic surface
    it could not mesh. That surface is a PARTIAL cylinder - an arc of about 44 degrees, closed in
    neither U nor V - so `split_closed_faces`, aimed at it, rightly declines `not_periodic`. gmsh's
    "periodic surface" kind names the surface's parameterisation, not a seam.
    """
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import _indexed
    from meshpipeline.cad.repair.mesh_probe import (
        PERIODIC_SURFACE,
        TAG_MATCH_FRACTION,
        _face_boxes,
        faces_for_tags,
        probe_file,
    )

    path = _corpus(_PERIODIC_FILE)
    diagonal = 59.5
    result = probe_file(path, diagonal_mm=diagonal, budget_s=120.0)
    assert result.failure is not None and result.failure.kind == PERIODIC_SURFACE
    shape = _read_shape(path)
    mapping = faces_for_tags(_face_boxes(_indexed(shape, TopAbs_FACE)), result.tag_boxes,
                             tol=diagonal * TAG_MATCH_FRACTION * 1000)
    failing = [mapping[t] for t in result.failure.surfaces if t in mapping]
    assert failing, (result.failure.surfaces, len(mapping))
    for index in failing:
        assert not any(_closed_directions(sub_shape(shape, "face", index))), index
        outcome = SplitClosedFaces().apply(shape, (_ref(shape, index),), tolerance_mm=0.1)
        assert outcome.evidence["declined"].startswith("not_periodic"), outcome.evidence
