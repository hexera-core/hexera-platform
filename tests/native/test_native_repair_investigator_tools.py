# Responsibility: Verify the ladder runner and the investigator's read-only tools against the
#                 real OCC kernel and the real gmsh probe.
# Boundaries: the native tier; the rules (strict args, bounds, the envelope cap) are unit-tested.
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.repair import caps, ladder, observe  # noqa: E402
from meshpipeline.cad.repair import investigator_tools as it
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity  # noqa: E402
from meshpipeline.cad.repair.localize import EntityDefect, _diagonal  # noqa: E402
from meshpipeline.cad.repair.regions import Region  # noqa: E402

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              str(Path.home() / "archductor" / "mesh-fail-corpus")))


def _corpus(name: str) -> Path:
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


def _box(dx, dy, dz, at=(0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), dx, dy, dz).Shape()


def _compound(*shapes):
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    return compound


def _workspace(shape, defects=()) -> it.Workspace:
    found = tuple(defects)
    region = Region(index=0, fingerprints=frozenset(d.fingerprint for d in found),
                    bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                    face_indices=frozenset({1, 2, 3}))
    return it.Workspace(region=region, defects=found, attempts=(), shape=shape,
                        original=caps.measure(shape), baseline_shape=shape)


def test_run_tier_0_on_a_box():
    shape = _box(10.0, 10.0, 10.0)
    out = ladder.run_tier(shape, "0", diagonal_mm=_diagonal(shape), within=None, budget_s=5.0)
    measured = caps.measure(shape)
    assert out.passed is True
    assert out.detail["faces"] == measured.faces == 6
    assert out.detail["solids"] == measured.solids == 1
    assert out.detail["valid"] is True


def test_probe_t2_on_the_overlap_fixture():
    """The real overlap fixture: gmsh refuses the volume mesh, and tier 2 says so under its cap.

    Constructed interpenetrating primitives were tried first and were NOT a fixture: at the
    probe's coarse sizing gmsh meshes two overlapping boxes without complaint, so the only honest
    tier-2 failure is a part that actually failed.
    """
    import time

    from meshpipeline.cad.repair.brep import _read_shape

    shape = _read_shape(_corpus("abc/00500400_bce216c87d2d17025db6ab36_step_000.step"))
    started = time.monotonic()
    out = ladder.run_tier(shape, "2", diagonal_mm=_diagonal(shape), within=None, budget_s=60.0)
    elapsed = time.monotonic() - started
    assert elapsed < 70.0                     # the hard cap, plus the reap
    assert out.passed is False
    assert out.defects
    assert {str(d.code.value) for d in out.defects} == {"mesh_overlapping_facets"}
    assert out.detail["scope"] == "part"
    assert out.detail["element_cap"] == 1_000_000


def test_probe_t2_budget_is_capped_at_sixty_seconds(monkeypatch):
    seen: dict = {}

    def mesh_defects(shape, *, diagonal_mm, budget_s, within=None):
        seen["budget_s"] = budget_s
        return ()
    monkeypatch.setattr(ladder.mesh_probe, "mesh_defects", mesh_defects)
    ladder.run_tier(_box(1.0, 1.0, 1.0), "2", diagonal_mm=1.7, within=None, budget_s=1_000.0)
    assert seen["budget_s"] == 60.0


def test_pair_contact_on_interpenetrating_boxes():
    shape = _compound(_box(10.0, 10.0, 10.0), _box(10.0, 10.0, 10.0, at=(5.0, 0.0, 0.0)))
    out = it.run_tool(_workspace(shape), "measure",
                      {"what": "pair_contact", "targets": ["solid:1", "solid:2"]}, budget_s=20.0)
    assert out.ok is True, out.error
    assert out.data["boxes_meet"] is True
    assert out.data["common_volume_mm3"] == pytest.approx(500.0, rel=0.02)
    assert out.data["shared_faces"] == 0


def test_pair_contact_on_apart_boxes_is_not_touching():
    shape = _compound(_box(10.0, 10.0, 10.0), _box(10.0, 10.0, 10.0, at=(50.0, 0.0, 0.0)))
    out = it.run_tool(_workspace(shape), "measure",
                      {"what": "pair_contact", "targets": ["solid:1", "solid:2"]}, budget_s=20.0)
    assert out.ok is True, out.error
    assert out.data["boxes_meet"] is False
    assert out.data["common_volume_mm3"] == pytest.approx(0.0, abs=1e-6)


def test_thickness_of_a_10mm_plate():
    shape = _box(100.0, 100.0, 10.0)
    ws = _workspace(shape)
    best = None
    for index in range(1, 7):
        out = it.run_tool(ws, "measure", {"what": "thickness", "targets": [f"face:{index}"]},
                          budget_s=20.0)
        assert out.ok is True, out.error
        assert out.data["basis"] == "estimate"
        value = out.data["thickness_mm"]
        if value is not None:
            best = value if best is None else min(best, value)
    assert best == pytest.approx(10.0, rel=0.01)


def test_distance_between_two_apart_boxes():
    shape = _compound(_box(10.0, 10.0, 10.0), _box(10.0, 10.0, 10.0, at=(30.0, 0.0, 0.0)))
    out = it.run_tool(_workspace(shape), "measure",
                      {"what": "distance", "targets": ["solid:1", "solid:2"]}, budget_s=20.0)
    assert out.ok is True, out.error
    assert out.data["min_distance_mm"] == pytest.approx(20.0, rel=1e-6)
    assert len(out.data["point_a"]) == 3


def test_face_facts_name_the_owning_solid_and_the_closed_directions():
    shape = _box(10.0, 10.0, 10.0)
    out = it.run_tool(_workspace(shape), "measure",
                      {"what": "face", "targets": ["face:1", "face:2"]}, budget_s=20.0)
    assert out.ok is True, out.error
    face = out.data["faces"][0]
    assert face["surface_type"].endswith("Plane")
    assert face["u_closed"] is False and face["v_closed"] is False
    assert face["owning_solids"] == 1
    assert face["tolerance_mm"] >= 0.0
    assert face["area_mm2"] == pytest.approx(100.0, rel=1e-6)


def test_look_matches_model_author_observation():
    shape = _box(10.0, 10.0, 10.0)
    defect = EntityDefect(
        code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
        entity_type="face", entity_index=1, message="overlapping facets",
        measurements={}, location={"bbox_min": (0.0, 0.0, 0.0), "bbox_max": (1.0, 1.0, 1.0)},
        fingerprint="fp1")
    ws = _workspace(shape, (defect,))
    out = it.run_tool(ws, "look", {}, budget_s=20.0)
    assert out.ok is True, out.error
    expected = observe.observation_of(ws.region, ws.defects, ws.attempts, ws.shape,
                                      original=ws.original, face_reader=ws.face_reader)
    assert out.data["observation"] == expected
    camera = out.data["cameras"][0]
    assert camera["extent_mm"] > 0.0
    assert len(camera["centroid_mm"]) == 3
