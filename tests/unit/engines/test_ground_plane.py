# Responsibility: Pin the ground plane end to end: a body on the ground is admitted with one body
# wall, the domain lays the box floor under it as the wall patch "ground", a body free in the flow
# keeps all six faces far field, and the gates judge the body - not the floor.
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
from meshpipeline.engines.registry import get_spec
from meshpipeline.engines.snappy import snappy_runner as R

# the car the user uploaded: one solid, one unnamed region, sitting on z = 0
ONE_REGION = {"region_count": 1, "region_names": ["ahmed_variant_001"]}
CAR_ON_GROUND = [("car", "wall"), ("ground", "wall"), ("farfield", "farfield")]


def _ev(engine, patches, facts=None, purpose="external_cfd"):
    return AdmissionEvidence(
        engine=engine, purpose=purpose, input_kind="solid-body", dimensionality="3D",
        surface_analysis=facts, patches=tuple(PatchSummary(n, t) for n, t in patches))


def _codes(engine, patches, facts=None, purpose="external_cfd"):
    return {r.code for r in get_spec(engine).admit(_ev(engine, patches, facts, purpose))}


# the gate

class TestTheGateCountsOnlyTheBody:
    def test_car_ground_farfield_on_a_one_region_body_is_admitted(self):
        assert _codes("snappy", CAR_ON_GROUND, ONE_REGION) == set()

    def test_the_observed_refusal_no_longer_appears_for_this_shape(self):
        # 2026-09-28 on shared dev: "cannot deliver the two requested wall patches because the
        # geometry is a single unnamed region". The ground is not a region, so there is one wall.
        from meshpipeline.agents.intake.validation import preview_admission
        v = preview_admission("snappy", "external_cfd", "solid-body", "3D",
                              patches=[{"name": n, "type": t} for n, t in CAR_ON_GROUND],
                              engine_params={}, geometry_facts=ONE_REGION)
        assert v["verdict"] == "supported", v
        assert "unnamed" not in v["safe_user_message"].lower()

    def test_the_ground_does_not_hide_a_real_arity_problem(self):
        # two BODY walls on a one-region file is still a file that names nothing
        patches = [("wing", "wall"), ("fuselage", "wall"), ("ground", "wall"),
                   ("farfield", "farfield")]
        assert "multiple_wall_patches_unsupported" in _codes("snappy", patches, ONE_REGION)

    def test_capitalisation_of_the_ground_does_not_matter(self):
        patches = [("car", "wall"), ("Ground", "wall"), ("farfield", "farfield")]
        assert _codes("snappy", patches, ONE_REGION) == set()

    def test_a_second_spelling_of_the_ground_is_not_a_body_wall(self):
        # ground + Ground and no body: both are the floor, so the body wall is still missing
        patches = [("ground", "wall"), ("Ground", "wall"), ("farfield", "farfield")]
        assert "missing_wall_patch" in _codes("snappy", patches, ONE_REGION)

    def test_a_ground_with_no_body_wall_asks_for_the_body(self):
        rejections = get_spec("snappy").admit(
            _ev("snappy", [("ground", "wall"), ("farfield", "farfield")], ONE_REGION))
        missing = [r for r in rejections if r.code == "missing_wall_patch"]
        assert missing and "body" in missing[0].message

    def test_inside_a_duct_a_wall_called_ground_is_just_a_wall(self):
        # only an EXTERNAL flow has a far-field box with a floor; internal walls keep counting
        patches = [("wall", "wall"), ("ground", "wall"), ("inlet", "inlet"), ("outlet", "outlet")]
        assert "multiple_wall_patches_unsupported" in _codes(
            "snappy", patches, {"region_count": 1, "region_names": ["pipe"]},
            purpose="internal_cfd")


class TestAnEngineWithoutAFloorRefusesWithAWayOn:
    def test_cfmesh_refuses_the_ground_up_front(self):
        rejections = get_spec("cfmesh").admit(_ev("cfmesh", CAR_ON_GROUND, ONE_REGION))
        ground = [r for r in rejections if r.code == "ground_plane_unsupported"]
        assert ground and ground[0].phase == "declared"
        assert "drop 'ground'" in ground[0].message
        assert "engine that builds a ground plane" in ground[0].message

    def test_the_refusal_is_a_hard_impossibility_at_intake(self):
        from meshpipeline.agents.intake.validation import preview_admission
        v = preview_admission("cfmesh", "external_cfd", "solid-body", "3D",
                              patches=[{"name": n, "type": t} for n, t in CAR_ON_GROUND],
                              engine_params={}, geometry_facts=ONE_REGION)
        assert v["verdict"] == "impossible"
        assert "ground_plane_unsupported" in v["blocking_rule_codes"]

    def test_snappy_declares_it_can_lay_the_floor(self):
        assert get_spec("snappy").supports_ground_plane is True

    def test_cfmesh_builder_path_stops_before_a_build(self, tmp_path):
        from meshpipeline.engines.cfmesh import cfmesh_runner as C
        (tmp_path / "flow_topology").write_text("external")
        out = C.configure_mesh(tmp_path, geometry_file="input.stl", strategy={},
                               wall_patch="car",
                               contract_patches=[{"name": n, "type": t} for n, t in CAR_ON_GROUND],
                               args={}, cell_budget=1_000_000)
        assert out["success"] is False and "ground" in out["error"]


# intake validation

def _submission(**over):
    base = {
        "domain": "car on the ground", "engine_params": {},
        "request_txt": "Air over a car body at 40 m/s; the car sits on the ground. " * 3,
        "review_brief_txt": "The car is captured, the ground is a wall, the far field is open. " * 2,
        "mesh_engine": "snappy", "engine_source": "user_direct", "purpose": "external_cfd",
        "input_kind": "solid-body", "dimensionality": "3D", "flow_axis": "+x",
        "patches": [{"name": n, "type": t} for n, t in CAR_ON_GROUND],
    }
    base.update(over)
    from meshpipeline.agents.intake.validation import validate_submission
    return validate_submission(base)


class TestIntakeValidation:
    def test_a_grounded_car_submission_has_no_patch_errors(self):
        errs = _submission()
        assert not any("patch" in e.lower() or "ground" in e.lower() for e in errs), errs

    def test_a_ground_that_is_not_a_wall_is_refused(self):
        errs = _submission(patches=[{"name": "car", "type": "wall"},
                                    {"name": "ground", "type": "farfield"},
                                    {"name": "farfield", "type": "farfield"}])
        assert any("floor of the far-field box" in e for e in errs), errs

    def test_a_ground_under_a_flow_along_z_is_refused(self):
        errs = _submission(flow_axis="-z")
        assert any("cannot also travel along z" in e for e in errs), errs

    def test_the_ground_is_declared_once(self):
        errs = _submission(patches=[{"name": "car", "type": "wall"},
                                    {"name": "ground", "type": "wall"},
                                    {"name": "Ground", "type": "wall"},
                                    {"name": "farfield", "type": "farfield"}])
        assert any("declare it once" in e for e in errs), errs


# the domain builder

ANALYSIS = {"bbox_min": [0.0, -0.2, 0.0], "bbox_max": [1.0, 0.2, 0.3], "L": 1.0,
            "extent": [1.0, 0.4, 0.3]}
STRATEGY = {"domain_margin": {"up": 5, "down": 10, "side": 5, "vert": 5}}
REC = {"base_cell": 0.1, "surface_level": [4, 4], "afford_level": 4, "feature_level": 5,
       "distance_bands": [(0.2, 3), (0.8, 2)], "resolve_feature_angle": 30}


class TestTheDomainLaysTheFloor:
    @pytest.mark.parametrize("flow_axis", [None, "+x", "-y"])
    def test_the_floor_sits_at_the_bodys_lowest_point(self, flow_axis):
        dmin, dmax = R.domain_from_strategy(ANALYSIS, STRATEGY, flow_axis=flow_axis, ground=True)
        assert dmin[2] == ANALYSIS["bbox_min"][2]

    @pytest.mark.parametrize("flow_axis", [None, "+x", "-y"])
    def test_every_other_margin_is_the_ungrounded_one(self, flow_axis):
        free = R.domain_from_strategy(ANALYSIS, STRATEGY, flow_axis=flow_axis)
        grounded = R.domain_from_strategy(ANALYSIS, STRATEGY, flow_axis=flow_axis, ground=True)
        assert grounded[1] == free[1]                       # every max face, the top included
        assert grounded[0][:2] == free[0][:2]               # both horizontal min faces
        assert free[0][2] < ANALYSIS["bbox_min"][2]         # the free body keeps room below

    def test_a_flow_along_z_cannot_share_the_floors_axis(self):
        with pytest.raises(ValueError):
            R.domain_from_strategy(ANALYSIS, STRATEGY, flow_axis="+z", ground=True)


def _render(tmp_path, *, ground=None, symmetry=None, flow_axis="+x"):
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    dmin, dmax = R.domain_from_strategy(ANALYSIS, STRATEGY, symmetry, flow_axis=flow_axis,
                                        ground=bool(ground))
    summary = R.render_snappy_case(
        tmp_path, surface_name="car", feature_file="car.eMesh", analysis=ANALYSIS,
        recommendation=REC, domain_min=dmin, domain_max=dmax, strategy={},
        dimensionality="3D", symmetry=symmetry, ground=ground)
    return summary, (tmp_path / "system" / "blockMeshDict").read_text()


def _farfield_faces(bm: str) -> list[str]:
    ff = re.search(r"farfield \{ type patch; faces \((.*?)\); \}", bm).group(1)
    return re.findall(r"\([0-9 ]+\)", ff)


class TestTheBlockMeshBoundary:
    def test_the_floor_is_the_wall_patch_ground(self, tmp_path):
        summary, bm = _render(tmp_path, ground="ground")
        assert "ground { type wall; faces ((0 3 2 1)); }" in bm       # the z-min face
        assert summary["ground"] == {"patch": "ground", "floor_z": 0.0}

    def test_the_other_five_faces_stay_farfield(self, tmp_path):
        _summary, bm = _render(tmp_path, ground="ground")
        faces = _farfield_faces(bm)
        assert len(faces) == 5 and "(0 3 2 1)" not in faces

    def test_an_ungrounded_body_keeps_all_six_faces_farfield(self, tmp_path):
        summary, bm = _render(tmp_path)
        assert len(_farfield_faces(bm)) == 6
        assert "type wall" not in bm and "ground" not in summary

    def test_the_ungrounded_box_is_byte_for_byte_the_old_one(self, tmp_path):
        _summary, bm = _render(tmp_path)
        assert ("boundary (farfield { type patch; faces "
                "((0 3 2 1)(4 5 6 7)(0 1 5 4)(2 3 7 6)(1 2 6 5)(0 4 7 3)); });") in bm

    def test_a_half_car_on_the_ground_carries_both_planes(self, tmp_path):
        half = {**ANALYSIS, "bbox_min": [0.0, 0.0, 0.0]}
        sym = {"axis": 1, "pos": 0.0, "side": "min", "name": "symmetry"}   # cut on y = 0
        (tmp_path / "system").mkdir(parents=True, exist_ok=True)
        dmin, dmax = R.domain_from_strategy(half, STRATEGY, sym, flow_axis="+x", ground=True)
        R.render_snappy_case(tmp_path, surface_name="car", feature_file="car.eMesh",
                             analysis=half, recommendation=REC, domain_min=dmin,
                             domain_max=dmax, strategy={}, symmetry=sym, ground="ground")
        bm = (tmp_path / "system" / "blockMeshDict").read_text()
        assert "symmetry { type symmetryPlane; faces ((0 1 5 4)); }" in bm
        assert "ground { type wall; faces ((0 3 2 1)); }" in bm
        assert len(_farfield_faces(bm)) == 4

    def test_a_symmetry_plane_on_the_floor_cannot_also_be_the_ground(self, tmp_path):
        sym = {"axis": 2, "pos": 0.0, "side": "min", "name": "symmetry"}
        with pytest.raises(ValueError):
            _render(tmp_path, ground="ground", symmetry=sym)

    def test_the_seed_point_stays_in_the_fluid_above_the_floor(self, tmp_path):
        summary, _bm = _render(tmp_path, ground="ground")
        loc = summary["location_in_mesh"]
        assert loc[2] > ANALYSIS["bbox_min"][2]
        assert loc[0] < ANALYSIS["bbox_min"][0]          # upstream of the car, clear of it


# the gates that judge the built mesh

def _manifest(box_zmin: float, patch_types: dict, topology="external") -> dict:
    return {"flow_topology": topology, "patch_types": patch_types,
            "geometry": {"domain_box": {"xmin": -5.0, "xmax": 11.0, "ymin": -5.2, "ymax": 5.2,
                                        "zmin": box_zmin, "zmax": 5.3},
                         "body_bbox": {"xmin": 0.0, "xmax": 1.0, "ymin": -0.2, "ymax": 0.2,
                                       "zmin": 0.0, "zmax": 0.3}}}


class TestTheExtentGateMeasuresTheRoomAbove:
    ASKED = {"upstream": 5.0, "downstream": 10.0, "lateral": 5.0, "vertical": 5.0}
    GROUNDED = {"car": "wall", "ground": "wall", "farfield": "farfield"}

    def test_a_grounded_box_passes_on_the_room_above(self):
        from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
        v = evaluate_domain_extents(self.ASKED, 1.0, _manifest(0.0, self.GROUNDED),
                                    flow_axis="+x")
        assert v.status == "pass", v.detail

    def test_the_same_box_without_a_ground_touches_the_body(self):
        from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
        v = evaluate_domain_extents(self.ASKED, 1.0,
                                    _manifest(0.0, {"car": "wall", "farfield": "farfield"}),
                                    flow_axis="+x")
        assert v.status == "block" and "vertical" in v.detail

    def test_the_room_above_is_still_judged(self):
        from meshpipeline.engines.domain_extent_gate import evaluate_domain_extents
        m = _manifest(0.0, self.GROUNDED)
        m["geometry"]["domain_box"]["zmax"] = 1.3              # 1 length above, 5 asked
        v = evaluate_domain_extents(self.ASKED, 1.0, m, flow_axis="+x")
        assert v.status == "block" and "vertical" in v.detail


class TestTheBodyIsJudgedNotTheFloor:
    def test_ground_faces_are_not_body_faces(self):
        from meshpipeline.engines.quality_criteria import measurements_from_manifest
        m = {"flow_topology": "external", "quality": {},
             "patch_types": {"car": "wall", "ground": "wall", "farfield": "farfield"},
             "patch_face_counts": {"car": 0, "ground": 900, "farfield": 300}}
        assert measurements_from_manifest(m)["wall_faces"] is None     # the body was lost

    def test_body_faces_still_count(self):
        from meshpipeline.engines.quality_criteria import measurements_from_manifest
        m = {"flow_topology": "external", "quality": {},
             "patch_types": {"car": "wall", "ground": "wall", "farfield": "farfield"},
             "patch_face_counts": {"car": 500, "ground": 900, "farfield": 300}}
        assert measurements_from_manifest(m)["wall_faces"] == 500

    @pytest.mark.parametrize("order", [["ground", "car"], ["car", "ground"]])
    def test_the_contract_wall_is_the_body_whichever_order(self, tmp_path, order):
        from meshpipeline.engines.workspace_facts import contract_wall_patch
        lines = [f"  • {n}  →  type wall" for n in order] + ["  • farfield  →  type farfield"]
        (tmp_path / "patches_contract.txt").write_text("\n".join(lines) + "\n")
        assert contract_wall_patch(tmp_path) == "car"

    def test_surface_capture_compares_the_body_not_the_floor(self, tmp_path):
        vtk = tmp_path / "VTK" / "case_1" / "boundary"
        vtk.mkdir(parents=True)
        for n in ("car", "ground"):
            (vtk / f"{n}.vtp").write_text("")
        (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")
        pair = R.surface_capture_reference(tmp_path, {"ground": "wall", "car": "wall"})
        assert pair is not None and pair[0].name == "car.vtp"


# the words the intake reads

class TestTheIntakeIsToldTheGroundIsTheDomains:
    def test_the_confirmation_names_the_ground_as_domain_built(self):
        from types import SimpleNamespace

        from meshpipeline.contracts.geometry_fields import external_declaration
        lines = external_declaration(SimpleNamespace(flow_axis="+x", grounded=True,
                                                     reference_length_mm=1044, extents={}))
        said = " ".join(lines)
        assert "stands on the ground" in said
        assert "wall patch named ground" in said and "domain builds" in said

    def test_the_geometry_check_block_says_it_is_never_a_geometry_patch(self):
        from meshpipeline.agents.intake.agent import _block_geometry_check
        block = _block_geometry_check()
        assert "the ground is produced by the domain" in block
        assert "`ground` with type wall" in block
        assert "never a region of the geometry" in block


# the driver

class _Publish:
    # The gated contract spelled out, no catch-all: the rationale path probes the publisher with
    # getattr, and a stand-in that answers every name would defeat it.
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name, a, k):
        self.calls.append((name, a, k))

    async def anote(self, *a, **k): self._rec("anote", a, k)
    async def awarn(self, *a, **k): self._rec("awarn", a, k)
    async def aerror(self, *a, **k): self._rec("aerror", a, k)
    async def astage(self, *a, **k): self._rec("astage", a, k)
    async def aattempt(self, *a, **k): self._rec("aattempt", a, k)
    async def acheck(self, *a, **k): self._rec("acheck", a, k)
    async def aaction(self, *a, **k): self._rec("aaction", a, k)
    async def arationale(self, *a, **k): self._rec("arationale", a, k)
    async def areasoning(self, *a, **k): self._rec("areasoning", a, k)
    async def atool_call(self, *a, **k): self._rec("atool_call", a, k)
    async def atool_result(self, *a, **k): self._rec("atool_result", a, k)
    async def ameshing(self, *a, **k): self._rec("ameshing", a, k)
    async def ameshed(self, *a, **k): self._rec("ameshed", a, k)
    async def averdict(self, *a, **k): self._rec("averdict", a, k)
    async def aclosing(self, *a, **k): self._rec("aclosing", a, k)


@pytest.fixture
def driver(monkeypatch, tmp_path):
    from tests._geometry_support import geometry_state

    import meshpipeline.engines.snappy.drivers as drv
    import meshpipeline.engines.snappy.planner as planner
    from meshpipeline.agents.builder.driver_run import BuilderDriverRun
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo

    seen: dict = {"faces": {"car": 500, "ground": 900, "farfield": 300}}
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")

    def _domain(*a, **k):
        seen["domain"] = k
        return ([-5.0, -5.2, 0.0], [11.0, 5.2, 5.3])

    def _render(*a, **k):
        seen["render"] = k
        return {"surface_level": 2, "n_layers": 3}

    for name, fn in {
        "detect_symmetry_plane": lambda *a, **k: None,
        "domain_from_strategy": _domain,
        "prepare_surface": lambda *a, **k: {"surface_name": "car", "feature_file": "car.eMesh"},
        "render_snappy_case": _render,
        "run_snappy": lambda *a, **k: {"rc": 0},
        "check_mesh": lambda *a, **k: {"cells": 1000, "fatal": [], "skew_fraction": 0.0,
                                       "skew_faces": 0},
        "_patch_face_counts": lambda *a, **k: dict(seen["faces"]),
    }.items():
        monkeypatch.setattr(R, name, fn, raising=False)
    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda *a, **k: {"diag": 1.0, "extent": [1.0, 0.4, 0.3],
                                         "bbox_min": [0.0, -0.2, 0.0], "bbox_max": [1.0, 0.2, 0.3],
                                         "surface_area": 1.0, "min_feature": 0.01, "L": 1.0})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda *a, **k: {"surface_level": 2, "feature_level": 3,
                                         "afford_level": 2})
    monkeypatch.setattr("meshpipeline.engines.workspace_facts.contract_wall_patch",
                        lambda *a, **k: "car")
    monkeypatch.setattr(drv, "read_purpose", lambda *a, **k: "external_cfd")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded", lambda **kw: {})

    async def _plan(**_kw):
        rr = ModelRoundResult(tool_calls=(), assistant_text="{}", finish_reason="stop",
                              provider=ProviderAttemptInfo(1, "p", "m"),
                              input_tokens=1, output_tokens=1)
        return planner.PlanOutcome({"approach": "a", "max_cells": 100000}, rr)

    monkeypatch.setattr(drv, "plan_with_accounting", _plan, raising=False)
    monkeypatch.setattr(planner, "plan_with_accounting", _plan)
    monkeypatch.setattr(planner, "clamp_cell_budget", lambda v, ceiling=None: v or 100000)

    def run(flow_axis="+x", patches=CAR_ON_GROUND):
        builder_run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial",
                                       deadline_s=60.0)

        async def _fence(*_a, **_k):
            return None
        monkeypatch.setattr(builder_run, "fence", _fence)
        pub = _Publish()
        state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
                 "intake_patches": [{"name": n, "type": t} for n, t in patches],
                 "dimensionality": "3D", "flow_topology": "external", "flow_axis": flow_axis,
                 "geometry": geometry_state(Path(tmp_path) / "_src", unit=LengthUnit.metre,
                                            filename="body.stl")}
        ok, _value, _outcome = asyncio.run(drv.drive(Path(tmp_path), state, job_id="j",
                                                     publish=pub, run=builder_run))
        return ok, pub

    return run, seen


class TestTheDriver:
    def test_a_grounded_case_lays_the_floor_and_names_it(self, driver):
        run, seen = driver
        ok, pub = run()
        assert ok is True
        assert seen["domain"]["ground"] is True
        assert seen["render"]["ground"] == "ground"
        notes = [a[0] for name, a, _k in pub.calls if name == "anote" and a]
        assert any("stands on the ground" in n and "'ground'" in n for n in notes), notes

    def test_a_free_body_asks_for_no_floor(self, driver):
        run, seen = driver
        seen["faces"] = {"car": 500, "farfield": 300}
        ok, _pub = run(patches=[("car", "wall"), ("farfield", "farfield")])
        assert ok is True
        assert seen["domain"]["ground"] is False and seen["render"]["ground"] is None

    def test_floor_faces_never_stand_in_for_a_lost_body(self, driver):
        run, seen = driver
        seen["faces"] = {"car": 0, "ground": 900, "farfield": 300}
        ok, _pub = run()
        assert ok is False

    def test_symmetry_faces_never_stand_in_for_a_lost_body(self, driver, monkeypatch):
        # a half car on the ground: the cut and the floor are both box faces, neither the body
        run, seen = driver
        monkeypatch.setattr(R, "detect_symmetry_plane", lambda *a, **k: {
            "axis": 1, "pos": 0.0, "side": "min", "name": "symmetry"})
        half = [*CAR_ON_GROUND, ("symmetry", "symmetry")]
        seen["faces"] = {"car": 0, "ground": 900, "symmetry": 400, "farfield": 300}
        ok, _pub = run(patches=half)
        assert ok is False
        seen["faces"] = {"car": 500, "ground": 900, "symmetry": 400, "farfield": 300}
        ok, _pub = run(patches=half)
        assert ok is True

    def test_a_flow_along_z_is_refused_before_any_build(self, driver):
        run, seen = driver
        ok, pub = run(flow_axis="+z")
        assert ok is False and "render" not in seen
        errors = [(a, k) for name, a, k in pub.calls if name == "aerror"]
        assert errors and errors[0][1]["op_id"] == "snappy:ground-unusable"
        assert "horizontal axis" in errors[0][0][0]
