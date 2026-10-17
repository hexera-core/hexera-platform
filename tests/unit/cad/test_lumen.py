# Responsibility: Verify a closed body that is the fluid inside tubes - a lumen - is read as one, with
# its tube ends as the openings, whether the ends are flat lids or rounded caps; that bodies which only
# look tube-like (a straight capsule, a coarse extruded wing, a blob) are not; that a body read as one
# the fluid flows around still keeps the openings it would have, so a flip to "through" has some; and
# that the staging cuts a rounded end to the opening the scout proposed there.
# Boundaries: numpy meshes written as STL, and OpenCASCADE for the capped tree (skipped where it is not
# installed); no model, no storage.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.lumen import read_lumen
from meshpipeline.cad.open_ends import surface
from meshpipeline.cad.scout_mesh import MAX_LUMEN_ENDS, scout_mesh
from meshpipeline.cad.stl_io import write_stl_binary

R = 0.02          # the tubes' radius, metres


def _frames(path: np.ndarray):
    """A frame along a centreline that does not twist (parallel transport)."""
    t = np.gradient(path, axis=0)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    u = np.cross(t[0], [0.0, 0.0, 1.0] if abs(t[0][2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    us = [u]
    for k in range(1, len(path)):
        u = us[-1] - (us[-1] @ t[k]) * t[k]
        us.append(u / np.linalg.norm(u))
    us = np.asarray(us)
    return t, us, np.cross(t, us)


def _swept(path, r=R, n=24, ends="round", start=None):
    """The closed skin of a tube of radius r swept along `path`, its two ends closed by rounded caps
    (a quarter turn of rings shrinking to a point) or flat lids - `start` closes the first end
    another way than the last."""
    path = np.asarray(path, dtype=float)
    t, u, w = _frames(path)
    ang = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)

    def ring(c, tt, uu, ww, rr):
        return [c + rr * (math.cos(a) * uu + math.sin(a) * ww) for a in ang]

    rings = [ring(path[k], t[k], u[k], w[k], r) for k in range(len(path))]
    tris = []

    def band(a, b):
        for i in range(n):
            j = (i + 1) % n
            tris.append([a[i], a[j], b[j]])
            tris.append([a[i], b[j], b[i]])

    for a, b in zip(rings, rings[1:]):
        band(a, b)

    def close(k, sign, how):
        c, tt, uu, ww = path[k], t[k] * sign, u[k], w[k]
        last = rings[k]
        if how == "round":
            steps = 8
            for s in range(1, steps):
                th = 0.5 * math.pi * s / steps
                nxt = ring(c + tt * r * math.sin(th), tt, uu, ww, r * math.cos(th))
                band(last, nxt) if sign > 0 else band(nxt, last)
                last = nxt
            tip = c + tt * r
        else:
            tip = c
        for i in range(n):
            j = (i + 1) % n
            tris.append([last[i], last[j], tip] if sign > 0 else [last[j], last[i], tip])

    close(len(path) - 1, 1.0, ends)
    close(0, -1.0, start or ends)
    return [[list(map(float, p)) for p in tri] for tri in tris]


def _u_bend(radius=0.3, step=0.008):
    n = int(math.pi * radius / step)
    a = np.linspace(0.0, math.pi, n)
    return np.stack([radius * np.cos(a), radius * np.sin(a), np.zeros_like(a)], axis=1)


def _straight(length=0.6, step=0.008):
    z = np.linspace(0.0, length, int(length / step))
    return np.stack([np.zeros_like(z), np.zeros_like(z), z], axis=1)


@pytest.fixture
def stl(tmp_path):
    def _write(name, tris):
        p = tmp_path / f"{name}.stl"
        write_stl_binary(p, tris)
        return p
    return _write


# -------------------------------------------------------------------------- rounded ends ----
def test_a_bent_tube_closed_by_rounded_caps_is_a_lumen_opening_at_its_two_ends(stl):
    """The fluid of a U-bend exported as a closed volume with rounded ends (a segmented vessel's
    shape): no lid, no hole - and still the water in a pipe, its ends the openings."""
    res = scout_mesh(stl("ubend", _swept(_u_bend())), scale_to_m=1.0)
    assert (res.flow, res.input_kind) == ("internal", "fluid-domain")
    assert len(res.openings) == 2 and {o.kind for o in res.openings} == {"end"}
    assert {o.role for o in res.openings} == {"inlet", "outlet"}
    tips = [np.array([0.3, -R, 0.0]), np.array([-0.3, -R, 0.0])]
    for o in res.openings:
        assert min(np.linalg.norm(np.asarray(o.centroid) - p) for p in tips) < 0.3 * R    # on the cap
        assert o.equivalent_diameter == pytest.approx(2 * R, rel=0.15)                    # the tube's
        assert o.normal[1] < -0.9                                                          # out along it
    assert res.seed_point is not None
    assert any("tube ends are proposed" in n for n in res.notes)


def test_a_straight_capsule_is_left_a_body_but_keeps_its_ends_for_a_flip(stl):
    """A straight rod with rounded ends is as likely a strut in a crossflow as the water in a pipe:
    it stays a body - and its two ends are kept, so saying it is a passage leaves openings to confirm."""
    res = scout_mesh(stl("capsule", _swept(_straight())), scale_to_m=1.0)
    assert (res.flow, res.input_kind) == ("external", "solid-body")
    assert res.seed_point is None
    assert len(res.openings) == 2 and {o.kind for o in res.openings} == {"end"}
    assert {o.role for o in res.openings} == {"inlet", "outlet"}


def test_a_tube_with_one_lid_and_one_rounded_cap_keeps_both_ends_for_a_flip(stl):
    """A body reading keeps every opening a flip would need: the flat lid AND the rounded end no lid
    closes - not the lid alone."""
    res = scout_mesh(stl("half_capped", _swept(_straight(), start="flat")), scale_to_m=1.0)
    assert res.flow == "external" and res.seed_point is None
    assert sorted(o.kind for o in res.openings) == ["disc", "end"]


def test_a_blob_and_a_coarse_extruded_wing_are_not_lumens():
    u, v = np.meshgrid(np.linspace(0, np.pi, 40), np.linspace(0, 2 * np.pi, 60))
    P = np.stack([0.6 * np.sin(u) * np.cos(v), 0.2 * np.sin(u) * np.sin(v), 0.3 * np.cos(u)], axis=-1)
    blob = []
    for i in range(59):
        for j in range(39):
            a, b, c, d = P[i, j], P[i + 1, j], P[i, j + 1], P[i + 1, j + 1]
            blob += [(a, d, b), (a, c, d)]
    reading = read_lumen(surface(np.asarray(blob)))
    assert reading is None or not reading.lumen
    # a wing drawn as one strip of triangles across its span has no rings to read at all
    x = np.linspace(0.0, 1.0, 60)
    half = 0.6 * (0.2969 * np.sqrt(x) - 0.126 * x - 0.3516 * x ** 2 + 0.2843 * x ** 3 - 0.1036 * x ** 4) + 1e-4
    loop = np.r_[np.c_[x, half], np.c_[x[::-1], -half[::-1]]]
    wing = []
    for k in range(len(loop)):
        a, b = loop[k], loop[(k + 1) % len(loop)]
        p0, p1, q0, q1 = [a[0], 0.0, a[1]], [b[0], 0.0, b[1]], [a[0], 1.0, a[1]], [b[0], 1.0, b[1]]
        wing += [(p0, p1, q1), (p0, q1, q0)]
    c0, c1 = [0.4, 0.0, 0.0], [0.4, 1.0, 0.0]
    for k in range(len(loop)):
        a, b = loop[k], loop[(k + 1) % len(loop)]
        wing += [(c0, [b[0], 0.0, b[1]], [a[0], 0.0, a[1]]), (c1, [a[0], 1.0, a[1]], [b[0], 1.0, b[1]])]
    reading = read_lumen(surface(np.asarray(wing)))
    assert reading is None or not reading.lumen


# --------------------------------------------------------------------------- capped trees ----
def _tree(branches: int):
    """A trunk with `branches` capped side branches round it, as a CAD export draws it."""
    from tests.unit.cad.test_scout_mesh import _cylinders_fused

    ways = [(1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0)]
    cyl = [((0, 0, 0), (0, 0, 1), 0.05, 1.4)]
    for k in range(branches):
        cyl.append(((0, 0, 0.1 + 0.075 * k), ways[k % 4], 0.015, 0.2))
    return _cylinders_fused(*cyl)


def test_a_tree_of_capped_tubes_is_a_lumen_with_every_end_an_opening(stl):
    """A pulmonary tree has two dozen flat-capped outlets: more lids than a passage has mouths, which
    reads as a machined part - unless every lid closes a tube of its own size, they point every way
    and the body is sparse. Then it is the fluid of a tube tree, and every end is an opening."""
    pytest.importorskip("OCP")
    res = scout_mesh(stl("tree", _tree(17)), scale_to_m=1.0)
    assert (res.flow, res.input_kind) == ("internal", "fluid-domain")
    assert len(res.openings) == 19                            # 17 branches and the trunk's two ends
    assert len(res.openings) <= MAX_LUMEN_ENDS
    assert any("capped tubes" in n for n in res.notes)


def test_a_connector_with_its_pins_all_one_way_stays_a_machined_body(stl):
    """The same count of round flat-ended pins, all standing one way off a bar, fill the box: a
    connector, not a tube tree."""
    pytest.importorskip("OCP")
    from tests.unit.cad.test_scout_mesh import _cylinders_fused

    pins = [((0, 0, 0.06 + 0.067 * k), (1, 0, 0), 0.006, 0.08) for k in range(13)]
    res = scout_mesh(stl("connector", _cylinders_fused(((0, 0, 0), (0, 0, 1), 0.05, 1.0), *pins)), scale_to_m=1.0)
    assert res.flow == "external" and res.seed_point is None
    assert any("more than a passage has mouths" in n for n in res.notes)


# --------------------------------------------------------- the staging cuts a rounded end ----
def test_the_staging_cuts_a_rounded_end_to_the_proposed_opening(stl):
    """What the scout proposes for a rounded end, the staging binds: the cap's tip cut square to the
    tube, to the proposed opening's own area (what the port binder holds it to)."""
    from meshpipeline.application.geometry_confirmation import body_of, patches_from
    from meshpipeline.cad.internal_surface import stage_triangles

    tris = _swept(_u_bend())
    res = scout_mesh(stl("ubend", tris), scale_to_m=1.0)
    body = body_of({"flow": "internal", "input_kind": res.input_kind,
                    "openings": [o.as_dict() for o in res.openings]})
    staged = stage_triangles(np.asarray(tris), intake_patches=patches_from(body), input_kind=res.input_kind)
    assert set(staged.ports) == {"inlet", "outlet"}
    for o in res.openings:
        assert staged.ports[o.name].area == pytest.approx(o.area, rel=0.25)
