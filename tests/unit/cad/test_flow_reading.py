# Responsibility: Verify the whole-shape reading (cad/flow_reading) on parts built on paper: a
# straight round bar, a bladed rotor, a car-like body, a half wing, an annular fluid passage and a
# thin pipe, a diffuser feeding a chamber - and that the proposal reads its direction across the
# up axis (contracts.geometry_fields.axis_from_reading) and weighs it against the pictures' nose.
# Boundaries: numpy triangles only; no file, no model, no storage.
from __future__ import annotations

import math

import numpy as np

from meshpipeline.application.geometry_check import nose_sureness, weigh_flow_axis
from meshpipeline.cad import flow_reading as fr
from meshpipeline.cad.stl_io import _box_triangles
from meshpipeline.contracts.geometry_fields import axis_from_reading, external_defaults


# ------------------------------------------------------------------------- parts on paper ----
def _revolved(profile, axis: int = 0, n: int = 48) -> np.ndarray:
    """A closed body of revolution about `axis` through the origin: `profile` is (position, radius)
    pairs; ends with a radius are closed by flat discs."""
    a, b = [k for k in range(3) if k != axis]

    def pt(s, r, t):
        p = [0.0, 0.0, 0.0]
        p[axis], p[a], p[b] = s, r * math.cos(t), r * math.sin(t)
        return p

    tris = []
    for i in range(n):
        t0, t1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        for (s0, r0), (s1, r1) in zip(profile, profile[1:]):
            p00, p01, p10, p11 = pt(s0, r0, t0), pt(s0, r0, t1), pt(s1, r1, t0), pt(s1, r1, t1)
            tris += [(p00, p10, p11), (p00, p11, p01)]
        for s, r in (profile[0], profile[-1]):
            if r > 0:
                tris.append((pt(s, 0.0, 0.0), pt(s, r, t0), pt(s, r, t1)))
    return np.asarray(tris, dtype=float)


def _tube(r_in: float, r_out: float, length: float, n: int = 48) -> np.ndarray:
    """An annular solid along x: outer and inner walls and two flat ring ends."""
    tris = []
    for i in range(n):
        t0, t1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n

        def p(x, r, t):
            return [x, r * math.cos(t), r * math.sin(t)]

        for r in (r_in, r_out):
            tris += [(p(0, r, t0), p(length, r, t0), p(length, r, t1)), (p(0, r, t0), p(length, r, t1), p(0, r, t1))]
        for x in (0.0, length):
            tris += [(p(x, r_in, t0), p(x, r_out, t0), p(x, r_out, t1)), (p(x, r_in, t0), p(x, r_out, t1), p(x, r_in, t1))]
    return np.asarray(tris, dtype=float)


def _loft(root, tip, span: float) -> np.ndarray:
    """A closed solid lofted along +y from a polygon in x-z at y=0 to another at y=span."""
    tris = []
    m = len(root)
    for i in range(m):
        j = (i + 1) % m
        a0, a1 = [root[i][0], 0.0, root[i][1]], [root[j][0], 0.0, root[j][1]]
        b0, b1 = [tip[i][0], span, tip[i][1]], [tip[j][0], span, tip[j][1]]
        tris += [(a0, b0, b1), (a0, b1, a1)]
    for poly, y in ((root, 0.0), (tip, span)):
        c = [sum(p[0] for p in poly) / m, y, sum(p[1] for p in poly) / m]
        for i in range(m):
            j = (i + 1) % m
            tris.append((c, [poly[i][0], y, poly[i][1]], [poly[j][0], y, poly[j][1]]))
    return np.asarray(tris, dtype=float)


def _airfoil(chord: float, thick: float, camber: float, n: int = 40):
    """A cambered teardrop section in x-z, leading edge at x=0."""
    up, lo = [], []
    for i in range(n + 1):
        x = chord * (1 - math.cos(math.pi * i / n)) / 2
        t = thick * 5 * (0.2969 * math.sqrt(x / chord) - 0.126 * x / chord - 0.3516 * (x / chord) ** 2
                         + 0.2843 * (x / chord) ** 3 - 0.1036 * (x / chord) ** 4) * chord
        c = camber * chord * 4 * (x / chord) * (1 - x / chord)
        up.append((x, c + t))
        lo.append((x, c - t))
    return up + lo[-2:0:-1]


def _rotor(blades: int = 5) -> np.ndarray:
    """A hub along x with pitched flat blades round it."""
    parts = [_revolved([(-0.03, 0.0), (-0.03, 0.06), (0.03, 0.06), (0.03, 0.0)])]
    blade = np.asarray(_box_triangles((-0.004, 0.05, -0.03), (0.004, 0.32, 0.03)), dtype=float)
    pitch = math.radians(35)
    rp = np.array([[math.cos(pitch), 0, math.sin(pitch)], [0, 1, 0], [-math.sin(pitch), 0, math.cos(pitch)]])
    for k in range(blades):
        t = 2 * math.pi * k / blades
        rx = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])
        parts.append(blade @ rp.T @ rx.T)
    return np.concatenate(parts)


def _car() -> np.ndarray:
    """A car-like block: a side profile with a sloped nose and a straight back, extruded across y."""
    side = [(0.0, 0.1), (0.3, 0.1), (0.5, 0.35), (1.2, 0.4), (1.5, 0.3), (1.5, 0.1), (1.5, 0.0), (0.0, 0.0)]
    body = _loft(side, side, 0.6)
    body[..., 1] -= 0.3
    return body


def _facts(flow="internal", kind="fluid-domain", body="single_solid", openings=()):
    return {"flow": flow, "input_kind": kind, "body_kind": body, "openings": [dict(o) for o in openings],
            "size_mm": [0, 0, 0], "notes": [], "confidence": {"input_kind": 0.7},
            "seed_point_m": None, "seed_point_mm": None}


def _mouth(i, centre, normal, d_mm, kind="disc", role="inlet"):
    return {"id": i, "kind": kind, "role": role, "name": role, "centroid_m": list(centre),
            "centroid_mm": [1000 * v for v in centre], "normal": list(normal), "diameter_mm": d_mm, "shape": "circle"}


def _read(tris, facts):
    reading = fr.read_flow(tris, facts)
    fr.apply_reading(facts, reading)
    return reading


# ------------------------------------------------------------------------------- the tests ----
def test_a_straight_round_bar_is_a_body_in_cross_flow_not_a_pipe_full_of_fluid():
    bar = _revolved([(0.0, 0.0), (0.0, 0.1), (0.6, 0.1), (0.6, 0.0)], axis=1)       # along y
    facts = _facts(openings=[_mouth(1, (0, 0.0, 0), (0, -1, 0), 200), _mouth(2, (0, 0.6, 0), (0, 1, 0), 200, role="outlet")])
    reading = _read(bar, facts)
    assert reading["symmetry"]["revolution"][1] and reading["symmetry"]["extruded"][1]
    assert facts["flow"] == "external" and facts["input_kind"] == "solid-body" and facts["openings"] == []
    assert any("straight convex bar" in n for n in facts["notes"])
    assert axis_from_reading(facts["flow_axis_reading"], "+z")[0] == "+x"        # across its span


def test_blades_round_a_hub_turn_the_flow_along_their_axis_and_are_no_passage():
    rotor = _rotor(5)
    hub_bore = [_mouth(1, (-0.03, 0, 0), (-1, 0, 0), 40, kind="ring"),
                _mouth(2, (0.03, 0, 0), (1, 0, 0), 40, kind="ring", role="outlet")]
    facts = _facts(kind="body-surface", body="pipe_wall", openings=hub_bore)
    reading = _read(rotor, facts)
    assert reading["symmetry"]["order"][0] == 5 and not any(reading["symmetry"]["revolution"])
    assert facts["flow"] == "external" and facts["body_kind"] == "rotor"
    axis, said = axis_from_reading(facts["flow_axis_reading"], "+z")
    assert axis == "+x" and said["confidence"] >= 0.7 and "5-fold" in said["reason"]


def test_a_car_like_body_flows_in_its_mirror_plane():
    reading = fr.read_flow(_car(), _facts(flow="external", kind="solid-body", openings=()))
    assert reading["symmetry"]["mirror"] == [False, True, False]
    assert axis_from_reading(reading["direction"], "+z")[0] == "+x"
    assert "kind" not in reading                                     # already a body: nothing overruled


def test_a_half_wing_flows_along_its_root_across_its_span():
    root = _airfoil(0.3, 0.12, 0.04)
    tip = [(0.08 + 0.6 * x, 0.6 * z) for x, z in root]
    wing = _loft(root, tip, 1.0)
    reading = fr.read_flow(wing, _facts(flow="external", kind="solid-body"))
    assert not any(reading["symmetry"]["mirror"])                   # cambered, swept, half: no mirror
    assert axis_from_reading(reading["direction"], "+z")[0] == "+x"  # the longest side would say +y


def test_a_whole_wing_wider_than_long_flows_in_its_mirror_plane():
    root = _airfoil(0.3, 0.12, 0.04)
    tip = [(0.08 + 0.6 * x, 0.6 * z) for x, z in root]
    half = _loft(root, tip, 1.0)
    other = half.copy()
    other[..., 1] *= -1.0
    reading = fr.read_flow(np.concatenate([half, other[:, ::-1]]), _facts(flow="external", kind="solid-body"))
    assert reading["symmetry"]["mirror"][1]                        # mirrored across its 2 m span, its longest side
    assert axis_from_reading(reading["direction"], "+z")[0] == "+x"


def test_a_part_mirrored_across_its_own_length_says_nothing_about_the_flow():
    connector = np.asarray(_box_triangles((-0.02, -0.006, 0.0), (0.02, 0.006, 0.012)), dtype=float)
    pin = np.asarray(_box_triangles((-0.001, 0.006, 0.002), (0.001, 0.009, 0.004)), dtype=float)
    reading = fr.read_flow(np.concatenate([connector, pin]), _facts(flow="external", kind="solid-body"))
    assert reading["symmetry"]["mirror"][0]
    assert not any(d["rule"] == "across" and d["axes"] == ["x"] and "mirror" in d["reason"] for d in reading["direction"])


def test_an_annulus_as_thick_as_a_blade_row_is_the_fluid_and_its_mouths_are_the_ring_faces():
    hole = [_mouth(1, (0, 0, 0), (-1, 0, 0), 240, kind="ring"), _mouth(2, (0.2, 0, 0), (1, 0, 0), 240, kind="ring", role="outlet")]
    facts = _facts(kind="body-surface", body="pipe_wall", openings=hole)
    _read(_tube(0.12, 0.2, 0.2), facts)
    assert facts["input_kind"] == "fluid-domain" and facts["flow"] == "internal"
    for o in facts["openings"]:
        assert o["kind"] == "annulus" and abs(o["diameter_mm"] - 400) < 25 and o["inner_diameter_mm"] == 240
    assert facts["seed_point_mm"] is not None and 120 < math.hypot(*facts["seed_point_mm"][1:]) < 200


def test_a_short_annulus_is_read_inside_its_own_length():
    hole = [_mouth(1, (0, 0, 0), (-1, 0, 0), 240, kind="ring"), _mouth(2, (0.04, 0, 0), (1, 0, 0), 240, kind="ring", role="outlet")]
    facts = _facts(kind="body-surface", body="pipe_wall", openings=hole)
    _read(_tube(0.12, 0.2, 0.04), facts)
    assert facts["input_kind"] == "fluid-domain"
    assert all(abs(o["diameter_mm"] - 400) < 25 for o in facts["openings"])


def test_a_thin_pipe_wall_stays_a_pipe_wall():
    hole = [_mouth(1, (0, 0, 0), (-1, 0, 0), 240, kind="ring"), _mouth(2, (0.6, 0, 0), (1, 0, 0), 240, kind="ring", role="outlet")]
    facts = _facts(kind="body-surface", body="pipe_wall", openings=hole)
    reading = _read(_tube(0.12, 0.13, 0.6), facts)
    assert "annulus" not in reading and "kind" not in reading
    assert facts["input_kind"] == "body-surface" and [o["diameter_mm"] for o in facts["openings"]] == [240, 240]


def test_the_mouth_a_diffuser_widens_from_into_a_chamber_is_the_inlet():
    # outlet pipe at the low end, then the chamber, then a cone narrowing to the inlet pipe
    body = _revolved([(0.0, 0.0), (0.0, 0.05), (0.2, 0.05), (0.2, 0.3), (0.5, 0.3), (0.5, 0.08),
                      (0.65, 0.05), (0.75, 0.05), (0.75, 0.0)])
    ends = [_mouth(1, (0, 0, 0), (-1, 0, 0), 100), _mouth(2, (0.75, 0, 0), (1, 0, 0), 100, role="outlet")]
    facts = _facts(openings=ends)
    reading = _read(body, facts)
    assert reading["roles"]["inlet"] == 2
    assert {o["id"]: o["role"] for o in facts["openings"]} == {1: "outlet", 2: "inlet"}


def test_a_diffuser_is_a_gentle_cone_not_a_branch_or_a_chamber_wall():
    assert fr.diffuser_behind([1.0, 1.06, 1.12, 1.18, 1.25, 2.4, 3.5])
    assert not fr.diffuser_behind([1.0, 1.0, 1.0, 1.43, 1.73, 1.87, 1.87])                     # a branch into a header
    assert not fr.diffuser_behind([1.0, 1.0, 1.0, 2.5, 3.0])                                   # a pipe into a chamber
    assert not fr.diffuser_behind([1.0, 1.0, 1.0, 1.06, 1.28, 1.51])                           # into a chamber aslant
    assert fr.narrows_behind([1.0, 0.98, 0.92, 0.82, 0.68, 0.5])                                # a reducing bend's wide end


def test_a_taper_is_passed_either_way_so_its_cone_names_no_inlet():
    # a straight reducer: the small end widens gently, the big end narrows gently
    body = _revolved([(0.0, 0.0), (0.0, 0.05), (0.15, 0.05), (0.45, 0.1), (0.6, 0.1), (0.6, 0.0)])
    ends = [_mouth(1, (0, 0, 0), (-1, 0, 0), 100), _mouth(2, (0.6, 0, 0), (1, 0, 0), 200, role="outlet")]
    facts = _facts(openings=ends)
    reading = _read(body, facts)
    assert reading["roles"]["inlet"] is None
    assert {o["id"]: o["role"] for o in facts["openings"]} == {1: "inlet", 2: "outlet"}        # the scout's own


def test_of_equally_wide_openings_the_first_along_their_run_is_the_feed():
    tee = [_mouth(1, (1.0, 0, 0), (1, 0, 0), 100), _mouth(2, (0.0, 0, 0), (-1, 0, 0), 100, role="outlet"),
           _mouth(3, (0.5, 0, 0.4), (0, 0, 1), 80, role="outlet")]
    for o, a in zip(tee, (7854.0, 7854.0, 5027.0)):
        o["area_mm2"] = a
    assert fr.widest_tie(tee) == 2
    tee[0]["area_mm2"] = 7700.0                               # 2% wider: the widest on its own
    assert fr.widest_tie(tee) is None


def test_the_reading_settles_the_direction_across_the_up_axis_surest_first():
    mirror_and_flat_bottom = [{"rule": "across", "axes": ["z"], "confidence": 0.7, "reason": "mirror"},
                              {"rule": "across", "axes": ["y"], "confidence": 0.6, "reason": "cut"}]
    assert axis_from_reading(mirror_and_flat_bottom, "+z")[0] == "+x"     # across the up axis says nothing
    assert axis_from_reading([{"rule": "across", "axes": ["x", "y"]}], "+z") == (None, {})
    assert axis_from_reading({"rule": "along", "axes": ["z"]}, "+z")[0] == "+z"
    out = external_defaults({"size_mm": [100.0, 600.0, 100.0],
                             "flow_axis_reading": [{"rule": "across", "axes": ["y"], "confidence": 0.6, "reason": "a bar"}]}, None)
    assert out["flow_axis"] == "+x" and out["flow_axis_guessed"] and out["flow_axis_confidence"] == 0.6
    assert out["reference_length_mm"] == 100.0
    plain = external_defaults({"size_mm": [100.0, 600.0, 100.0]}, None)
    assert plain["flow_axis"] == "+y" and plain["flow_axis_confidence"] == 0.3


def test_a_sure_shape_axis_stands_against_the_pictures_nose_but_never_against_the_user():
    facts = {"flow_axis_reading": [{"rule": "along", "axes": ["x"], "confidence": 0.75, "reason": "rotor"}]}
    assert weigh_flow_axis(facts, {"flow_from": "the nose"}, "-y", "+z")[0] is None
    assert weigh_flow_axis(facts, {"flow_from": "the nose"}, "-x", "+z") == ("-x", "")    # the nose gives the sign
    assert weigh_flow_axis(facts, {"flow_from": "the user's words"}, "-y", "+z") == ("-y", "")
    unsure = {"flow_axis_reading": [{"rule": "across", "axes": ["y"], "confidence": 0.6, "reason": "cut"}]}
    assert weigh_flow_axis(unsure, {"flow_from": "the nose"}, "+y", "+z") == ("+y", "")


def test_a_nose_from_the_pictures_says_where_it_came_from_and_how_sure_it_is():
    rotor = {"flow_axis_reading": [{"rule": "along", "axes": ["x"], "confidence": 0.75, "reason": "rotor"}]}
    agree = nose_sureness(rotor, "-x", "+z")
    assert agree["flow_axis_confidence"] == 0.75 and "along the shape's own axis (rotor)" in agree["flow_axis_from"]
    alone = nose_sureness({}, "-y", "+z")
    assert alone == {"flow_axis_confidence": 0.6, "flow_axis_from": "the nose in the pictures"}


def test_a_shape_too_small_to_read_says_so():
    assert fr.read_flow(np.zeros((2, 3, 3)), _facts()) == {
        "read": False, "reason": "the skin is too small or too large to read quickly"}
