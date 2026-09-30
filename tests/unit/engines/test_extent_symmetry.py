# Responsibility: Pin the far-field extent check for boxes whose faces lie on the body by design: a half model on its symmetry plane, a slab between two, a car on the ground - and that a box which really touches the body, or a symmetry plane on the wrong face, is still refused.
# Boundaries: the typed extent verdict, the legacy prose gate, the snappy pre-flight and box builder, and the manifest record the post-mesh gate reads. No mesher runs.
from __future__ import annotations

import json

import pytest

from meshpipeline.contracts.failure_cause import FailureCause, describe
from meshpipeline.engines.domain_extent_gate import (
    check_domain_extents,
    evaluate_domain_extents,
    manifest_symmetry_faces,
)
from meshpipeline.engines.preflight import check_domain
from meshpipeline.engines.snappy import snappy_runner as R

# The NASA CRM high-lift half model as the pipeline staged it (crm_highlift_takeoff.step, inches,
# to metres): the fuselage is cut on y = 0 - tessellation leaves it 0.65 micrometres below - and
# the wing reaches +y. Job 011e1fe0 asked 5 / 10 / 5 / 5 reference lengths of 64.593 m, flow +x.
CRM = {"bbox_min": [2.3495, -6.488056e-07, 0.4617192],
       "bbox_max": [66.69876, 29.46196, 18.50279],
       "extent": [64.34926, 29.46196, 18.04107], "L": 64.34926, "diag": 73.03639,
       "axis_caps": [{"min": 0.0, "max": 0.0},
                     {"min": 0.242872, "max": 6.76e-06},
                     {"min": 0.000187, "max": 1.28e-06}]}
RULER = 64.593
ASKED = {"upstream": 5.0, "downstream": 10.0, "lateral": 5.0, "vertical": 5.0}
MARGINS = {"domain_margin": {"up": 5.0, "down": 10.0, "side": 5.0, "vert": 5.0}}
CUT = [{"patch": "symmetry", "axis": "y", "side": "min"}]


def _box(dmin, dmax) -> dict:
    return {f"{n}{s}": float(v[i]) for s, v in (("min", dmin), ("max", dmax))
            for i, n in enumerate("xyz")}


def _manifest(dmin, dmax, body_min, body_max, *, symmetry_faces=None, patch_types=None,
              topology="external") -> dict:
    geom = {"domain_box": _box(dmin, dmax), "body_box": _box(body_min, body_max),
            "reference_length": RULER}
    if symmetry_faces is not None:
        geom["symmetry_faces"] = symmetry_faces
    return {"flow_topology": topology, "geometry": geom,
            "patch_types": patch_types or {"aircraft": "wall", "symmetry": "symmetry",
                                           "farfield": "farfield"}}


def _crm_box():
    sym = R.detect_symmetry_plane(CRM, "symmetry")
    dmin, dmax = R.domain_from_strategy(CRM, MARGINS, sym, flow_axis="+x", ruler_m=RULER)
    return sym, dmin, dmax


# the snappy box builder puts the plane on the cut and the far field everywhere else

class TestTheHalfModelBox:
    def test_the_cut_is_found_on_y_min(self):
        sym, _dmin, _dmax = _crm_box()
        # laid on the cut face itself (0.65 micrometres under y = 0), not an idealised 0
        assert sym == {"axis": 1, "pos": CRM["bbox_min"][1], "side": "min", "name": "symmetry"}
        assert R.symmetry_box_faces(sym) == CUT

    def test_the_symmetry_face_is_the_cut_plane_and_every_other_face_is_far(self):
        _sym, dmin, dmax = _crm_box()
        lo, hi = CRM["bbox_min"], CRM["bbox_max"]
        assert dmin[1] == lo[1] == pytest.approx(0.0, abs=1e-6)   # on the cut, y = 0
        assert dmax[1] == pytest.approx(hi[1] + 5 * RULER)        # 5 lengths beyond the tip
        assert dmin[0] == pytest.approx(lo[0] - 5 * RULER)       # upstream
        assert dmax[0] == pytest.approx(hi[0] + 10 * RULER)      # downstream
        assert dmin[2] == pytest.approx(lo[2] - 5 * RULER)       # below
        assert dmax[2] == pytest.approx(hi[2] + 5 * RULER)       # above

    def test_a_cut_a_hair_off_zero_gets_its_plane_on_the_cut_not_through_the_body(self):
        # a 2 m half body whose cut came out 0.4 mm below y = 0: a plane at 0 would slice it
        a = {"bbox_min": [0.0, -0.0004, -0.5], "bbox_max": [2.0, 0.8, 0.5], "L": 2.0}
        sym = R.detect_symmetry_plane(a, "symmetry", flow_axis="+x")
        assert sym is not None and sym["pos"] == -0.0004
        dmin, dmax = R.domain_from_strategy(a, MARGINS, sym, flow_axis="+x", ruler_m=1.0)
        assert dmin[1] == -0.0004
        assert check_domain(requested=ASKED, reference_length_m=1.0, strict=True, flow_axis="+x",
                            body_min=a["bbox_min"], body_max=a["bbox_max"], domain_min=dmin,
                            domain_max=dmax, grounded=False,
                            symmetry_faces=R.symmetry_box_faces(sym)) is None

    def test_the_flow_axis_is_never_taken_for_the_cut(self):
        # a half car: its flat rear base sits on x = 0 (the first axis tried), its cut on y = 0
        car = {"bbox_min": [-1.044, 0.0, 0.05], "bbox_max": [0.0, 0.1945, 0.338], "L": 1.044,
               "axis_caps": [{"min": 0.0, "max": 0.08}, {"min": 0.3, "max": 0.0},
                             {"min": 0.0, "max": 0.1}]}
        assert R.detect_symmetry_plane(car, "symmetry")["axis"] == 0     # blind: the base
        sym = R.detect_symmetry_plane(car, "symmetry", flow_axis="+x")
        assert sym == {"axis": 1, "pos": 0.0, "side": "min", "name": "symmetry"}

    def test_a_slab_claims_both_ends_of_its_sweep(self):
        slab = {"slab": True, "axis": 2, "pos_lo": 0.0, "pos_hi": 1.0,
                "lo_name": "front", "hi_name": "back"}
        assert R.symmetry_box_faces(slab) == [{"patch": "front", "axis": "z", "side": "min"},
                                              {"patch": "back", "axis": "z", "side": "max"}]
        assert R.symmetry_box_faces(None) == []


# the gate: a half model on its symmetry plane passes

class TestAHalfModelOnItsSymmetryPlane:
    def test_the_crm_box_passes_the_pre_flight_that_refused_it(self):
        sym, dmin, dmax = _crm_box()
        common = {"requested": ASKED, "reference_length_m": RULER, "strict": True,
                  "flow_axis": "+x", "body_min": CRM["bbox_min"], "body_max": CRM["bbox_max"],
                  "domain_min": dmin, "domain_max": dmax, "grounded": False}
        assert check_domain(**common, symmetry_faces=R.symmetry_box_faces(sym)) is None
        # without the plane it is the refusal job 011e1fe0 got on both attempts
        before = check_domain(**common)
        assert before is not None and before.cause == FailureCause.DOMAIN_EXTENT
        assert before.facts["misses"][0]["direction"] == "lateral"
        assert before.facts["misses"][0]["face"] == "y-min"

    def test_the_built_mesh_passes_on_the_plane_its_manifest_records(self):
        _sym, dmin, dmax = _crm_box()
        m = _manifest(dmin, dmax, CRM["bbox_min"], CRM["bbox_max"], symmetry_faces=CUT)
        v = evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x")
        assert v.status == "pass", v.detail
        assert manifest_symmetry_faces(m) == CUT

    def test_the_room_on_the_open_side_is_still_judged(self):
        _sym, dmin, dmax = _crm_box()
        dmax = [dmax[0], CRM["bbox_max"][1] + 1.0 * RULER, dmax[2]]   # 1 length beyond the tip
        m = _manifest(dmin, dmax, CRM["bbox_min"], CRM["bbox_max"], symmetry_faces=CUT)
        v = evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x")
        assert v.status == "block" and "lateral" in v.detail
        assert v.misses == [{"direction": "lateral", "requested": 5.0, "measured": 1.0}]

    def test_a_half_model_cut_on_the_other_side_passes_too(self):
        lo, hi = [0.0, -3.0, 0.0], [10.0, 0.0, 2.0]             # wing reaches -y, cut on y-max
        m = _manifest([-5 * RULER, -3.0 - 5 * RULER, -5 * RULER],
                      [10.0 + 10 * RULER, 0.0, 2 + 5 * RULER],
                      lo, hi, symmetry_faces=[{"patch": "symmetry", "axis": "y", "side": "max"}])
        assert evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x").status == "pass"

    def test_the_legacy_prose_gate_reads_the_room_away_from_the_cut(self):
        body = {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 0.4, "zmin": 0.0, "zmax": 0.2}
        box = {"xmin": -20.0, "xmax": 31.0, "ymin": 0.0, "ymax": 20.4, "zmin": -20.0,
               "zmax": 20.2}
        geom = {"chord": 1.0, "body_box": body, "domain_box": box}
        asked = {"upstream": 20, "downstream": 30, "lateral": 20}
        assert check_domain_extents(asked, {"geometry": {**geom, "symmetry_faces": CUT}}) == \
            (True, "")
        ok, diag = check_domain_extents(asked, {"geometry": geom})
        assert not ok and "lateral: requested 20c, mesh has 0c" in diag


# the gate: a symmetry plane on the wrong face is refused, and says so

class TestAPlaneOnTheWrongFace:
    WRONG = [{"patch": "symmetry", "axis": "y", "side": "max"}]

    def _verdict(self):
        # the body lies on y-min (its cut) and the box is flush there, but the plane was put on
        # y-max, out in the far field: y-min is a far-field face touching the body
        _sym, dmin, dmax = _crm_box()
        m = _manifest(dmin, dmax, CRM["bbox_min"], CRM["bbox_max"], symmetry_faces=self.WRONG)
        return evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x")

    def test_it_is_refused_naming_both_faces(self):
        v = self._verdict()
        assert v.status == "block"
        assert "touches or clips the body on its y-min face" in v.detail
        assert "'symmetry' was put on the y-max face" in v.detail
        assert "no far-field margin can change that" in v.detail      # the planner is told
        assert v.misses[0]["face"] == "y-min"
        assert v.misses[0]["symmetry_patch"] == "symmetry"
        assert v.misses[0]["symmetry_face"] == "y-max"

    def test_the_user_is_told_where_the_plane_has_to_go(self):
        v = self._verdict()
        what, nxt = describe(FailureCause.DOMAIN_EXTENT, {"misses": v.misses,
                                                          "before_meshing": True})
        assert ("the box touches the body on the lateral side (y-min), but the symmetry plane "
                "'symmetry' is on the y-max face - it has to be on the face the model was cut "
                "on") in what
        assert "which face the model was cut on" in nxt and "without a symmetry plane" in nxt

    def test_the_pre_flight_refuses_it_before_meshing(self):
        _sym, dmin, dmax = _crm_box()
        r = check_domain(requested=ASKED, reference_length_m=RULER, strict=False,
                         flow_axis="+x", body_min=CRM["bbox_min"], body_max=CRM["bbox_max"],
                         domain_min=dmin, domain_max=dmax, grounded=False,
                         symmetry_faces=self.WRONG)
        assert r is not None and r.facts["misses"][0]["symmetry_face"] == "y-max"

    def test_a_body_that_crosses_its_plane_is_refused(self):
        lo, hi = [0.0, -2.0, 0.0], [10.0, 8.0, 2.0]              # 2 m on the far side of y = 0
        m = _manifest([-5 * RULER, 0.0, -5 * RULER], [10 + 10 * RULER, 8 + 5 * RULER, 2 + 5 * RULER],
                      lo, hi, symmetry_faces=CUT)
        v = evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x")
        assert v.status == "block" and "crosses the symmetry plane 'symmetry'" in v.detail
        what, _ = describe(FailureCause.DOMAIN_EXTENT, {"misses": v.misses})
        assert "crosses the symmetry plane 'symmetry' on the lateral side" in what

    def test_a_body_a_hair_through_its_plane_is_refused_not_meshed_short(self):
        # the CRM reaching 0.1 m through y = 0: inside the detector's 0.005 L, but the box would
        # slice 0.1 m off the fuselage. Float noise (the real file's 0.65 micrometres) passes.
        _sym, dmin, dmax = _crm_box()
        dmin = [dmin[0], 0.0, dmin[2]]
        lo = [CRM["bbox_min"][0], -0.1, CRM["bbox_min"][2]]
        m = _manifest(dmin, dmax, lo, CRM["bbox_max"], symmetry_faces=CUT)
        v = evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x")
        assert v.status == "block" and "crosses the symmetry plane 'symmetry'" in v.detail
        noise = [CRM["bbox_min"][0], -6.5e-7, CRM["bbox_min"][2]]
        m = _manifest(dmin, dmax, noise, CRM["bbox_max"], symmetry_faces=CUT)
        assert evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x").status == "pass"

    @pytest.mark.parametrize("asked", [ASKED, {"lateral": 5.0, "vertical": 5.0}])
    def test_a_plane_across_the_flow_is_never_excused(self, asked):
        # a half model is cut along the flow: a symmetry plane on the upstream face is wrong,
        # whether or not the approval asked for upstream room
        lo, hi = [0.0, -1.0, -1.0], [10.0, 1.0, 1.0]
        m = _manifest([0.0, -5 * RULER, -5 * RULER], [10 + 10 * RULER, 1 + 5 * RULER, 1 + 5 * RULER],
                      lo, hi, symmetry_faces=[{"patch": "symmetry", "axis": "x", "side": "min"}])
        v = evaluate_domain_extents(asked, RULER, m, flow_axis="+x")
        assert v.status == "block" and "upstream" in v.detail and "across the flow" in v.detail
        what, nxt = describe(FailureCause.DOMAIN_EXTENT, {"misses": v.misses})
        assert "'symmetry' was put across the flow, on the upstream side" in what
        assert "which face the model was cut on" in nxt


# a 2.5D slab: a symmetry plane on each end of the sweep

class TestASlabBetweenTwoPlanes:
    # a wing section swept along z, chord 1 m on x, thickness 0.12 m on y
    SEC = {"bbox_min": [0.0, -0.06, 0.0], "bbox_max": [1.0, 0.06, 0.5],
           "extent": [1.0, 0.12, 0.5], "L": 1.0,
           "axis_caps": [{"min": 0.0, "max": 0.0}, {"min": 0.0, "max": 0.0},
                         {"min": 0.05, "max": 0.05}]}
    ENDS = [{"patch": "front", "axis": "z", "side": "min"},
            {"patch": "back", "axis": "z", "side": "max"}]

    def test_the_slab_box_passes_with_no_margin_owed_along_the_sweep(self):
        slab = R.detect_slab_symmetry(self.SEC, "front", "back")
        assert slab is not None and slab["axis"] == 2
        dmin, dmax = R.domain_from_strategy(self.SEC, MARGINS, slab, flow_axis="+x", ruler_m=1.0)
        assert (dmin[2], dmax[2]) == (0.0, 0.5)                  # the planes are the end caps
        faces = R.symmetry_box_faces(slab)
        assert faces == self.ENDS
        r = check_domain(requested=ASKED, reference_length_m=1.0, strict=True, flow_axis="+x",
                         body_min=self.SEC["bbox_min"], body_max=self.SEC["bbox_max"],
                         domain_min=dmin, domain_max=dmax, grounded=False, symmetry_faces=faces)
        assert r is None
        m = _manifest(dmin, dmax, self.SEC["bbox_min"], self.SEC["bbox_max"],
                      symmetry_faces=faces,
                      patch_types={"wing": "wall", "front": "symmetry", "back": "symmetry",
                                   "farfield": "farfield"})
        assert evaluate_domain_extents(ASKED, 1.0, m, flow_axis="+x").status == "pass"
        # without the planes the same box "touches the body" on its vertical sides
        assert evaluate_domain_extents(ASKED, 1.0, {**m, "geometry": {
            k: v for k, v in m["geometry"].items() if k != "symmetry_faces"}},
            flow_axis="+x").status == "block"

    def test_the_section_itself_still_needs_its_room(self):
        dmin, dmax = [-5.0, -0.1, 0.0], [11.0, 5.06, 0.5]      # 0.04 below the section
        r = check_domain(requested=ASKED, reference_length_m=1.0, strict=False, flow_axis="+x",
                         body_min=self.SEC["bbox_min"], body_max=self.SEC["bbox_max"],
                         domain_min=dmin, domain_max=dmax, grounded=False,
                         symmetry_faces=self.ENDS)
        assert r is not None and r.facts["misses"][0]["direction"] == "lateral"


# a car on the ground is judged exactly as before

class TestAGroundedCarIsUnchanged:
    BODY = ([0.0, -0.2, 0.0], [1.0, 0.2, 0.3])
    GROUNDED = {"car": "wall", "ground": "wall", "farfield": "farfield"}

    def test_the_floor_owes_no_margin_and_the_room_above_is_judged(self):
        good = _manifest([-5.0, -5.2, 0.0], [11.0, 5.2, 5.3], *self.BODY,
                         symmetry_faces=[], patch_types=self.GROUNDED)
        v = evaluate_domain_extents(ASKED, 1.0, good, flow_axis="+x")
        assert v.status == "pass", v.detail
        low = _manifest([-5.0, -5.2, 0.0], [11.0, 5.2, 1.3], *self.BODY,
                        symmetry_faces=[], patch_types=self.GROUNDED)
        v = evaluate_domain_extents(ASKED, 1.0, low, flow_axis="+x")
        assert v.status == "block" and v.misses[0]["direction"] == "vertical"
        assert v.misses[0]["measured"] == 1.0

    def test_a_half_car_on_the_ground_owes_neither_its_floor_nor_its_cut(self):
        half = ([0.0, 0.0, 0.0], [1.0, 0.2, 0.3])
        m = _manifest([-5.0, 0.0, 0.0], [11.0, 5.2, 5.3], *half, symmetry_faces=CUT,
                      patch_types={**self.GROUNDED, "symmetry": "symmetry"})
        assert evaluate_domain_extents(ASKED, 1.0, m, flow_axis="+x").status == "pass"


# a full model that really touches the box is still refused

class TestAFullModelThatTouchesIsRefused:
    def test_a_box_flush_with_a_full_body_blocks(self):
        body = ([0.0, -15.0, 0.0], [64.0, 15.0, 18.0])
        m = _manifest([-5 * RULER, -15.0, -5 * RULER], [64 + 10 * RULER, 15 + 5 * RULER,
                                                          18 + 5 * RULER],
                      *body, symmetry_faces=[],
                      patch_types={"aircraft": "wall", "farfield": "farfield"})
        v = evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x")
        assert v.status == "block"
        assert "lateral: the domain box touches or clips the body on its y-min face" in v.detail
        what, nxt = describe(FailureCause.DOMAIN_EXTENT, {"misses": v.misses})
        assert "the box touches the body on the lateral side" in what
        assert "symmetry" not in what and "run it again" in nxt

    def test_a_symmetry_record_on_a_face_the_body_is_not_on_excuses_nothing(self):
        # a full model whose recorded plane stands off the body: that face is measured like any
        # other, and the face the body does touch is still refused
        body = ([0.0, -15.0, 0.0], [64.0, 15.0, 18.0])
        m = _manifest([-5 * RULER, -15.0, -5 * RULER], [64 + 10 * RULER, 15 + 5 * RULER,
                                                          18 + 5 * RULER],
                      *body, symmetry_faces=[{"patch": "s", "axis": "z", "side": "min"}])
        assert evaluate_domain_extents(ASKED, RULER, m, flow_axis="+x").status == "block"

    def test_an_internal_manifest_carries_no_symmetry_seats(self):
        m = _manifest([0, 0, 0], [1, 1, 1], [0, 0, 0], [1, 1, 1], symmetry_faces=CUT,
                      topology="internal")
        assert manifest_symmetry_faces(m) == []


# the record the post-mesh gate reads is written where the box is

class TestTheRecordTravelsWithTheBox:
    def test_prepare_surface_writes_the_planes_beside_the_box(self, tmp_path):
        (tmp_path / "input.stl").write_text(
            "solid b\n"
            "facet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
            "endloop\nendfacet\n"
            "facet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 0 1 0\nvertex 0 0 1\n"
            "endloop\nendfacet\nendsolid b\n")
        R.prepare_surface(tmp_path, geometry_file="input.stl", domain_min=[-5, 0, -5],
                          domain_max=[6, 6, 6], wall_patch="aircraft", symmetry_faces=CUT)
        rec = json.loads((tmp_path / "geom_box.json").read_text())
        assert rec["symmetry_faces"] == CUT

    def test_the_manifest_carries_them_and_an_all_far_box_carries_none(self, tmp_path):
        from meshpipeline.engines.manifest import write_manifest
        (tmp_path / "constant" / "polyMesh").mkdir(parents=True)
        (tmp_path / "constant" / "polyMesh" / "boundary").write_text(
            "aircraft { type wall; nFaces 100; startFace 0; }\n"
            "symmetry { type symmetryPlane; nFaces 10; startFace 100; }\n")
        kw = {"patch_types": {"aircraft": "wall", "symmetry": "symmetry"},
              "patch_entities": {}, "bbox": (0, 0, 0, 1, 1, 1), "quality": {"cells": 1},
              "mesh_units": "m", "mesh_mode": "snappy"}
        assert write_manifest(tmp_path, symmetry_faces=CUT, **kw)["geometry"][
            "symmetry_faces"] == CUT
        assert write_manifest(tmp_path, **kw)["geometry"]["symmetry_faces"] == []
