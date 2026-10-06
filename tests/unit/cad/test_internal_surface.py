# Responsibility: Verify the one internal-flow staging of a triangle surface (cad/internal_surface)
# on the forms a fluid passage arrives in: a thin wall open at its ends, a thick wall whose ends are
# bore mouths, a closed fluid body with capped mouths, a ring-shaped mouth around a centre rod,
# ends cut on a slant, an opening the user left out, a loose sheet beside the part - and that what
# it writes is what every engine reads (the binder accepts it under the user's names).
# Boundaries: numpy meshes built here; no OpenCASCADE, no engine, no files beyond tmp_path.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.internal_surface import (
    InternalSurfaceError,
    _winding,
    stage_internal_surface,
    stage_triangles,
    write_staged,
)
from meshpipeline.cad.lids import frame, lid, ring
from meshpipeline.engines.port_binding import BindError, bind_intake

SIDES = 32


def _tube(r_out, r_in=None, length=0.3, rows=4, end0=None, end1=None, x0=0.0):
    """A straight tube along +x: a thin wall open at both ends, or with r_in a thick wall closed by
    an end face at each end. end0/end1 move each end along the axis by f(angle)."""
    f0 = end0 or (lambda a: 0.0)
    f1 = end1 or (lambda a: 0.0)

    def at(t, r, k):
        a = 2 * math.pi * k / SIDES
        x = x0 + (1 - t) * f0(a) + t * (length + f1(a))
        return (x, r * math.cos(a), r * math.sin(a))

    tris = []
    for k in range(SIDES):
        for j in range(rows):
            t0, t1 = j / rows, (j + 1) / rows
            a0, a1, b0, b1 = at(t0, r_out, k), at(t0, r_out, k + 1), at(t1, r_out, k), at(t1, r_out, k + 1)
            tris += [(a0, b1, b0), (a0, a1, b1)]
            if r_in is not None:
                c0, c1, d0, d1 = at(t0, r_in, k), at(t0, r_in, k + 1), at(t1, r_in, k), at(t1, r_in, k + 1)
                tris += [(c0, d0, d1), (c0, d1, c1)]
        if r_in is not None:
            a0, a1, b0, b1 = at(0, r_out, k), at(0, r_out, k + 1), at(1, r_out, k), at(1, r_out, k + 1)
            c0, c1, d0, d1 = at(0, r_in, k), at(0, r_in, k + 1), at(1, r_in, k), at(1, r_in, k + 1)
            tris += [(a0, c0, c1), (a0, c1, a1), (b0, d1, d0), (b0, b1, d1)]
    return np.asarray(tris, dtype=float)


def _capped(r=0.05, length=0.3):
    tris = list(_tube(r, None, length))
    for x, s in ((0.0, -1), (length, 1)):
        for k in range(SIDES):
            a0, a1 = 2 * math.pi * k / SIDES, 2 * math.pi * (k + 1) / SIDES
            p0, p1 = (x, r * math.cos(a0), r * math.sin(a0)), (x, r * math.cos(a1), r * math.sin(a1))
            tris.append(((x, 0, 0), p1, p0) if s < 0 else ((x, 0, 0), p0, p1))
    return np.asarray(tris)


def _ports(d_mm=100.0, length_mm=300.0, names=("feed", "exit"), wall="pipe"):
    return [{"name": names[0], "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": d_mm},
            {"name": names[1], "type": "outlet", "near_mm": [length_mm, 0, 0], "diameter_mm": d_mm},
            {"name": wall, "type": "wall"}]


def _closed_and_named(st, names):
    f = st.facts
    assert f["watertight"] and f["manifold"] and f["inconsistent_edges"] == 0, f
    assert f["lid_crossings"] == 0
    assert f["one_patch_per_opening"]
    assert set(st.ports) == set(names)
    assert f["volume_m3"] > 0, "the boundary must face out of the fluid"


def _inside(st, p) -> float:
    return float(_winding(st.verts[st.faces], np.asarray([p]))[0])


# ------------------------------------------------------------------------------- the forms ----
def test_a_thin_wall_open_at_both_ends_is_closed_by_lids_on_its_rims():
    st = stage_triangles(_tube(0.05), intake_patches=_ports())
    _closed_and_named(st, {"feed", "exit"})
    assert {o.kind for o in st.ports.values()} == {"rim"}
    assert abs(st.facts["volume_m3"] - math.pi * 0.05 ** 2 * 0.3) < 0.03 * math.pi * 0.05 ** 2 * 0.3
    assert abs(_inside(st, st.seed) - 1.0) < 1e-6


def test_a_thick_wall_keeps_only_the_bore_and_lids_its_mouths():
    st = stage_triangles(_tube(0.05, 0.04), intake_patches=_ports(d_mm=80.0))
    _closed_and_named(st, {"feed", "exit"})
    assert {o.kind for o in st.ports.values()} == {"bore"}
    # the fluid is the bore (r = 40 mm), never the metal envelope (r = 50 mm)
    bore = math.pi * 0.04 ** 2 * 0.3
    assert abs(st.facts["volume_m3"] - bore) < 0.03 * bore
    assert np.linalg.norm(st.seed[1:]) < 0.04


def test_a_closed_fluid_body_takes_its_capped_faces_as_the_openings():
    st = stage_triangles(_capped(), intake_patches=_ports(), input_kind="fluid-domain")
    _closed_and_named(st, {"feed", "exit"})
    assert {o.kind for o in st.ports.values()} == {"cap"}
    assert st.facts["lids"] == [], "a capped mouth already has its face; no second lid"


def _capsule(r=0.05, length=0.3, rings=12):
    """A closed fluid body with rounded ends: no crease marks where a mouth would be."""
    tris = list(_tube(r, None, length))
    for x0, sgn in ((0.0, -1.0), (length, 1.0)):
        prev = [(x0, r * math.cos(2 * math.pi * k / SIDES), r * math.sin(2 * math.pi * k / SIDES)) for k in range(SIDES)]
        for j in range(1, rings + 1):
            th = 0.5 * math.pi * j / rings
            if j == rings:
                tip = (x0 + sgn * r, 0.0, 0.0)
                for k in range(SIDES):
                    a, b = prev[k], prev[(k + 1) % SIDES]
                    tris.append((a, tip, b) if sgn < 0 else (a, b, tip))
                break
            rr, xx = r * math.cos(th), x0 + sgn * r * math.sin(th)
            cur = [(xx, rr * math.cos(2 * math.pi * k / SIDES), rr * math.sin(2 * math.pi * k / SIDES)) for k in range(SIDES)]
            for k in range(SIDES):
                a0, a1, b0, b1 = prev[k], prev[(k + 1) % SIDES], cur[k], cur[(k + 1) % SIDES]
                tris += [(a0, b0, b1), (a0, b1, a1)] if sgn < 0 else [(a0, b1, b0), (a0, a1, b1)]
            prev = cur
    return np.asarray(tris)


def test_a_mouth_traced_on_a_rounded_end_takes_the_faces_around_it():
    """A closed body whose ends are domes (no crease): an opening placed on each pole, 50 mm
    across, becomes the faces turned its way within its radius - one patch each, no lid."""
    patches = [{"name": "feed", "type": "inlet", "near_mm": [-50, 0, 0], "diameter_mm": 50.0},
               {"name": "exit", "type": "outlet", "near_mm": [350, 0, 0], "diameter_mm": 50.0},
               {"name": "pipe", "type": "wall"}]
    st = stage_triangles(_capsule(), intake_patches=patches, input_kind="fluid-domain")
    _closed_and_named(st, {"feed", "exit"})
    assert {o.kind for o in st.ports.values()} == {"cap"}
    a = st.ports["feed"].area
    assert 0.5 < a / (math.pi * 0.025 ** 2) < 2.0


def test_ends_cut_on_a_slant_are_closed_by_lids_that_cross_nothing():
    tris = _tube(0.05, None, end0=lambda a: 0.03 * math.cos(a), end1=lambda a: 0.02 * math.sin(2 * a))
    st = stage_triangles(tris, intake_patches=_ports())
    _closed_and_named(st, {"feed", "exit"})


def test_a_ring_shaped_mouth_around_a_centre_rod_is_lidded_as_a_ring():
    """An annular passage as an open surface: the outer pipe and the rod both end in the same plane
    at each mouth. The opening is the ring between them, and the rod is part of the wall."""
    tris = np.vstack([_tube(0.05), _tube(0.03)])
    ring_area = math.pi * (100.0 ** 2 - 60.0 ** 2) / 4.0
    patches = [{"name": "feed", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 100.0,
                "inner_diameter_mm": 60.0},
               {"name": "exit", "type": "outlet", "near_mm": [300, 0, 0], "diameter_mm": 100.0,
                "inner_diameter_mm": 60.0},
               {"name": "pipe", "type": "wall"}]
    st = stage_triangles(tris, intake_patches=patches)
    _closed_and_named(st, {"feed", "exit"})
    assert all(o.inner is not None for o in st.ports.values())
    assert abs(st.ports["feed"].area * 1e6 - ring_area) < 0.05 * ring_area
    r = float(np.linalg.norm(st.seed[1:]))
    assert 0.03 < r < 0.05, "the seed must be in the gap, not in the rod"


def test_an_open_end_the_user_did_not_declare_is_sealed_into_the_wall():
    st = stage_triangles(_tube(0.05), intake_patches=_ports()[:1] + _ports()[2:])
    _closed_and_named(st, {"feed"})
    assert len(st.facts["sealed"]) == 1


def test_a_loose_open_sheet_beside_the_passage_is_left_out():
    sheet = np.asarray([((1.0, 0, 0), (1.1, 0, 0), (1.0, 0.1, 0)), ((1.1, 0, 0), (1.1, 0.1, 0), (1.0, 0.1, 0))])
    st = stage_triangles(np.vstack([_tube(0.05), sheet]), intake_patches=_ports())
    _closed_and_named(st, {"feed", "exit"})
    assert st.facts["loose_open_pieces_left_out"] == [{"faces": 2}]


def test_with_nothing_declared_the_holes_are_the_openings_largest_first():
    st = stage_triangles(_tube(0.05, None, end1=lambda a: 0.0), intake_patches=[])
    assert set(st.ports) == {"inlet", "outlet"}
    _closed_and_named(st, {"inlet", "outlet"})


def test_a_declared_opening_that_matches_nothing_is_refused_with_what_was_measured():
    bad = _ports()
    bad[1]["near_mm"] = [150, 0, 0]               # the middle of the pipe: no hole there
    with pytest.raises(BindError, match="matches no open end"):
        stage_triangles(_tube(0.05), intake_patches=bad)


def test_openings_that_leave_the_fluid_open_are_refused_in_plain_words():
    # a thick wall with an undeclared bore through its side cannot leak: the bore is sealed.
    # A surface whose declared openings close nothing (a flat sheet) has no inside at all.
    sheet = np.asarray([((0, 0, 0), (1, 0, 0), (0, 1, 0)), ((1, 0, 0), (1, 1, 0), (0, 1, 0)),
                        ((0, 0, 0.01), (1, 0, 0.01), (0, 1, 0.01)), ((2, 2, 2), (2.1, 2, 2), (2, 2.1, 2))])
    with pytest.raises((InternalSurfaceError, BindError)):
        stage_triangles(sheet, intake_patches=_ports())


# ------------------------------------------------------------------------------ the record ----
def test_the_written_record_is_what_the_engines_bind_under_the_users_names(tmp_path):
    from meshpipeline.cad.stl_io import read_stl_solids, write_stl_binary
    write_stl_binary(tmp_path / "input.stl", [tuple(map(list, t)) for t in _tube(0.05)])
    t = stage_internal_surface(tmp_path / "input.stl", tmp_path / "out", intake_patches=_ports())
    assert set(t["stls"]) == {"wall", "feed", "exit"}
    assert set(read_stl_solids(tmp_path / "out" / "fluid_boundary.stl")) == {"pipe", "feed", "exit"}
    bound, wall, note = bind_intake(t, _ports())
    assert wall == "pipe" and set(bound["stls"]) == {"pipe", "feed", "exit"}
    assert "feed" in note
    assert len(t["interior_point"]) == 3 and t["facts"]["seed"]["odd_rays"] >= 4


def test_write_staged_round_trips_float32_points_so_the_patches_stay_joined(tmp_path):
    from pathlib import Path

    from meshpipeline.cad.open_ends import skin_faces
    from meshpipeline.cad.stl_io import read_stl_triangles
    far = [{**p, "near_mm": [p["near_mm"][0] + 612345.6, 0, 0]} if "near_mm" in p else p
           for p in _ports()]                               # a part drawn 612 m from the origin
    st = stage_triangles(_tube(0.05, x0=612.3456), intake_patches=far)
    rec = write_staged(st, tmp_path)
    tris = [t for p in rec["stls"].values() for t in read_stl_triangles(Path(p))]
    v, f = skin_faces(np.asarray(tris))
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    assert np.all(cnt == 2), "the written patches must meet edge to edge, far from the origin too"


def test_one_entry_point_routes_a_cad_solid_to_the_brep_and_anything_else_to_the_surface(tmp_path, monkeypatch):
    """stage_internal is the one call an engine makes: a STEP goes through the B-rep path as
    before; any other upload is the staged input.stl; and a B-rep that cannot be separated falls
    back to its own staged surface instead of refusing."""
    from meshpipeline.cad import cad_tessellate
    from meshpipeline.cad.internal_surface import stage_internal
    from meshpipeline.cad.stl_io import write_stl_binary
    write_stl_binary(tmp_path / "input.stl", [tuple(map(list, t)) for t in _tube(0.05)])
    calls = []

    def fake_brep(src, out, **kw):
        calls.append(src)
        if "flat" in str(src):
            return {"stls": {}, "openings": {}, "facts": {}}
        raise RuntimeError("internal-flow geometry needs >=2 flat openings")
    monkeypatch.setattr(cad_tessellate, "tessellate_internal", fake_brep)
    assert stage_internal(tmp_path / "flat.step", tmp_path, prepared=None,
                          intake_patches=_ports())["stls"] == {}
    t = stage_internal(tmp_path / "curved.step", tmp_path, prepared=None, intake_patches=_ports())
    assert set(t["stls"]) == {"wall", "feed", "exit"} and "flat openings" in t["facts"]["cad_path_failed"]
    t = stage_internal(tmp_path / "upload.obj", tmp_path, prepared=None, intake_patches=_ports())
    assert t["source"] == "surface" and len(calls) == 2


# -------------------------------------------------------------------------------- the lids ----
def test_a_flat_loop_is_lidded_in_its_plane_with_no_triangle_crossing_another():
    a = np.linspace(0, 2 * math.pi, 200, endpoint=False)
    P = np.c_[np.zeros_like(a), 0.7 * np.cos(a) + 0.2 * np.cos(3 * a), np.sin(a)]   # not convex
    extra, tris, info = lid(P)
    assert info["method"] == "planar" and len(tris) == len(P) - 2 + 2 * len(extra)
    assert len(extra) > 0, "a wide lid gets points inside it, not a fan of slivers"
    _, n, area, _ = frame(P)
    assert np.allclose((np.vstack([P, extra]) - P.mean(axis=0)) @ n, 0.0, atol=1e-12), "a flat lid stays flat"
    pts = np.vstack([P, extra])
    t = pts[tris]
    longest = np.linalg.norm(pts[tris[:, [1, 2, 0]]] - t, axis=2).max(axis=1)
    cr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    # a triangle's area over its longest edge squared: 0.43 equilateral, -> 0 for a sliver
    assert np.median(0.5 * np.linalg.norm(cr, axis=1) / longest ** 2) > 0.15, "the lid is not a fan of slivers"
    assert np.all(cr @ n > 0), "every triangle winds with the loop"
    assert abs(0.5 * np.linalg.norm(cr, axis=1).sum() - area) < 1e-9 * max(area, 1.0) + 1e-9


def test_a_saddle_loop_gets_a_lid_no_larger_than_its_flat_projection_allows():
    a = np.linspace(0, 2 * math.pi, 64, endpoint=False)
    P = np.c_[np.cos(a), np.sin(a), 0.3 * np.cos(2 * a)]
    extra, tris, info = lid(P)
    assert info["method"] in ("planar", "projected") and len(tris) == len(P) - 2 + 2 * len(extra)
    t = np.vstack([P, extra])[tris]
    proj = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])[:, 2]
    assert np.all(proj > 0), "seen along its axis, no lid triangle folds back over another"


def test_a_ring_lid_fills_exactly_the_gap_between_two_loops():
    a = np.linspace(0, 2 * math.pi, 40, endpoint=False)
    b = np.linspace(0, 2 * math.pi, 23, endpoint=False) + 0.1
    P = np.c_[np.zeros_like(a), np.cos(a), np.sin(a)]
    Q = np.c_[np.zeros_like(b), 0.5 * np.cos(b), 0.5 * np.sin(b)]
    tris = ring(P, Q)
    pts = np.vstack([P, Q])
    t = pts[tris]
    cr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    assert len(tris) == len(P) + len(Q) and np.all(cr[:, 0] > 0)
    assert abs(0.5 * np.linalg.norm(cr, axis=1).sum() - (frame(P)[2] - frame(Q)[2])) < 1e-9
