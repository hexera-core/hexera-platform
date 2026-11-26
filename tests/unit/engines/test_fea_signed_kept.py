"""TIME-12: one sizing keeps the signed distance to the whole surface per cube (the geometry does not change between
its passes, only the zones), and an assembly part's own size is not measured where a finer zone already holds the
point - with the same sizes, histograms, plans and estimates to the bit.

- the histograms of a sizing's passes are the same with the kept distances on and off, and later passes measure
  fewer cubes;
- with the cap hit, the cubes not kept are measured as before (the same histograms, never more kept than the cap);
- the sizes are those of the field measured with no shortcut for the parts' own sizes (random points, cubes deep in
  a part);
- the plan and its estimate on real parts are the same with the kept distances off.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402
from tests.unit.engines.test_fea_size_field_tiles import _many, _same, _stage  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402
from meshpipeline.engines import fea_size_field as SF  # noqa: E402

KW = {"coarsest": 4e-3, "per_h3": P.PER_H3, "stop_elements": float("inf"), "workers": 1, "max_cubes": 300_000}


def _passes(zones: list[dict]) -> list[list[dict]]:
    """Three passes of one sizing: the zones, then coarsened twice (as a coarsening search moves them)."""
    out = [zones]
    for f in (1.4, 2.0):
        out.append([{**z, "size_m": z["size_m"] * f} if z["kind"] != "part" else dict(z) for z in zones])
    return out


def _sizing(surf, passes, monkeypatch) -> tuple[list[SF.Histogram], int, int]:
    measured = [0]
    on = SF.SizeField._on

    def counted(self, pool, what, p, r):
        if what == "signed":
            measured[0] += len(p)
        return on(self, pool, what, p, r)

    monkeypatch.setattr(SF.SizeField, "_on", counted)
    with SF.sizing():
        fields = [SF.SizeField(surf, z, growth=1.25) for z in passes[:1]]
        hists = [fields[0].histogram(**KW)]
        for z in passes[1:]:
            fields.append(SF.SizeField(surf, z, growth=1.25))
            hists.append(fields[-1].histogram(**KW))
        kept = fields[-1]._signed_kept
        cubes = kept.cubes if kept is not None else 0
    monkeypatch.setattr(SF.SizeField, "_on", on)
    return hists, measured[0], cubes


def test_a_sizings_passes_read_the_kept_distances_with_the_same_histograms(monkeypatch):
    surf, zones = _many(6)
    passes = _passes(zones)
    kept, measured_kept, cubes = _sizing(surf, passes, monkeypatch)
    monkeypatch.setattr(SF, "SIGNED_KEEP_CUBES", 0)
    every, measured_every, none = _sizing(surf, passes, monkeypatch)
    for a, b in zip(kept, every, strict=True):
        _same(a, b)
    assert none == 0 and cubes > 0
    assert sum(h.cubes for h in every) == measured_every
    assert measured_kept < 0.7 * measured_every, (measured_kept, measured_every)


def test_with_the_cap_hit_the_rest_are_measured_as_before(monkeypatch):
    surf, zones = _many(6)
    passes = _passes(zones)
    monkeypatch.setattr(SF, "SIGNED_KEEP_CUBES", 5_000)
    capped, measured_capped, cubes = _sizing(surf, passes, monkeypatch)
    monkeypatch.setattr(SF, "SIGNED_KEEP_CUBES", 0)
    every, measured_every, _ = _sizing(surf, passes, monkeypatch)
    for a, b in zip(capped, every, strict=True):
        _same(a, b)
    assert cubes == 5_000
    assert measured_every - 3 * 5_000 <= measured_capped < measured_every


def test_the_kept_distances_are_the_measured_floats():
    """Every kept distance is the signed distance measured at its cube's centre, to the bit."""
    surf, zones = _many(4)
    field = SF.SizeField(surf, zones, growth=1.25)
    seen: list[tuple[np.ndarray, tuple]] = []
    at = field._signed_at

    def record(pool, centres, r, where, lo, e, n):
        seen.append((centres.copy(), where, lo, e, n))
        return at(pool, centres, r, where, lo, e, n)

    field._signed_at = record  # type: ignore[method-assign]
    field.histogram(**{**KW, "max_cubes": 100_000})
    kept = field._signed_kept
    assert kept is not None and kept.cubes > 0 and len(seen) > 3
    for centres, where, lo, e, n in seen:
        keys = kept.keys(centres, lo, e, n, where[1])
        assert keys is not None and len(np.unique(keys)) == len(keys)
        sd, miss = kept.take(where, keys)
        assert not miss.any()
        assert np.array_equal(sd, field.signed(centres))


def _sizes_ref(field: SF.SizeField, p: np.ndarray, r: float) -> tuple[np.ndarray, np.ndarray]:
    """Round 3's sizes: every point in a part's box measured against the part's own surface."""
    h = np.full(len(p), np.inf)
    need = np.full(len(p), np.inf)
    g1 = field.growth - 1.0
    for g in field.groups:
        if g.inside:
            sd = np.linalg.norm(np.maximum(0.0, np.maximum(g.lo - p, p - g.hi)), axis=1)
            box = sd <= 0.0
            if box.any():
                sd[box] = g.faces(p[box])
            hg = g.size + g1 * np.maximum(0.0, np.maximum(0.0, sd) - g.reach)
            h = np.minimum(h, hg)
            need = np.minimum(need, np.where(sd <= -r, np.inf, hg))
        else:
            hs, ns = field._sizes_each(p, r, h, need, (g,))
            h, need = hs, ns
    return h, need


@pytest.mark.parametrize("r", [0.0, 0.4e-3])
def test_a_parts_own_size_is_skipped_only_where_it_changes_nothing(r):
    surf, zones = _many()
    field = SF.SizeField(surf, zones, growth=1.25)
    assert sum(g.inside for g in field.groups) > 10
    rng = np.random.default_rng(11)
    lo, hi = field.xyz.min(axis=0), field.xyz.max(axis=0)
    span = hi - lo
    near = field.xyz[rng.integers(0, len(field.xyz), 20_000)] + rng.normal(0.0, 0.3e-3, (20_000, 3))
    for p in (lo - 0.1 * span + rng.random((30_000, 3)) * 1.2 * span, near):
        ref = _sizes_ref(field, p, r)
        for got in (field.sizes(p, r), field._sizes_each(p, r)):
            assert np.array_equal(got[0], ref[0]) and np.array_equal(got[1], ref[1])


@pytest.mark.parametrize("name", ["plate_with_hole", "bracket", "touching_blocks", "drilled_plates"])
def test_the_plan_and_its_estimate_are_the_same_with_the_kept_distances_off(name, tmp_path, monkeypatch):
    shape = fx.drilled_plates(4) if name == "drilled_plates" else getattr(fx, name)()
    record, surface, diag = FD.measure_staged(_stage(shape, tmp_path / name))
    monkeypatch.setattr(SF, "plan_workers", lambda: 1)       # in this process, so the switch below reaches it
    full = FD.make_plan(record, surface, diag)
    cap = max(1000, int(full["estimate"]["elements"] / 3)) if isinstance(full.get("estimate"), dict) else None
    kept = FD.choose_plan(record, surface, diag, cap=cap)
    monkeypatch.setattr(SF, "SIGNED_KEEP_CUBES", 0)
    every = FD.choose_plan(record, surface, diag, cap=cap)
    assert kept == every
    assert FD.make_plan(record, surface, diag) == full


def test_one_sizing_keeps_one_set_of_distances(monkeypatch):
    surf, zones = _many(4)
    with SF.sizing():
        a = SF.SizeField(surf, zones, growth=1.25)
        a.histogram(**{**KW, "max_cubes": 50_000})
        b = SF.SizeField(surf, zones[3:], growth=1.25)
        assert b._signed_kept is a._signed_kept and a._signed_kept is not None
        assert a._signed_kept.cubes > 0
    assert SF._scope().dists is None
    alone = SF.SizeField(surf, zones, growth=1.25)
    assert alone._signed_kept is not a._signed_kept


def test_a_pass_is_built_from_what_the_last_one_found(monkeypatch):
    """Each face's triangles are the ones a pass over every triangle finds; a capped corner's sampled edges, its
    segments and spacing alike, are the last pass's own; and the children are sent the surface once a sizing."""
    surf, zones = _many(4)
    monkeypatch.setattr(SF, "PARALLEL_MIN_POINTS", 2000)
    kw = {**KW, "workers": 2, "max_cubes": 50_000}
    with SF.sizing():
        a = SF.SizeField(surf, zones, growth=1.25)
        a.histogram(**kw)
        for faces in (["Part_1_Face_1"], ["Part_3_Face_2", "Part_2_Face_6", "Part_3_Face_2"], ["nothing"]):
            ks = [a.index[f] for f in faces if f in a.index]
            assert np.array_equal(a._tris_of(faces), np.flatnonzero(np.isin(a.label, ks)))
        pool = SF._scope().pool
        assert pool is not None and pool.init_message(a._spec)[1][0] is None
        b = SF.SizeField(surf, zones, growth=1.25)
        edges = [g.edges for g in a.groups if g.edges is not None]
        assert edges and {id(e) for e in edges} == {id(g.edges) for g in b.groups if g.edges is not None}
        again = b.histogram(**kw)
    _same(again, SF.SizeField(surf, zones, growth=1.25).histogram(**{**kw, "workers": 1}))


@pytest.mark.parametrize("r", [0.0, 0.4e-3])
def test_a_corners_edges_searched_only_as_far_as_they_matter_give_the_same_sizes(r, monkeypatch):
    surf, zones = _many()
    field = SF.SizeField(surf, zones, growth=1.25)
    assert sum(g.edges is not None for g in field.groups) >= 5
    cut = [0]
    call = SF._EdgeDistance.__call__

    def counted(self, p, bound=float("inf")):
        d = call(self, p, bound)
        cut[0] += int(np.isinf(d).sum())
        return d

    monkeypatch.setattr(SF._EdgeDistance, "__call__", counted)
    rng = np.random.default_rng(5)
    lo, hi = field.xyz.min(axis=0), field.xyz.max(axis=0)
    span = hi - lo
    near = field.xyz[rng.integers(0, len(field.xyz), 20_000)] + rng.normal(0.0, 0.3e-3, (20_000, 3))
    for p in (lo - 0.1 * span + rng.random((30_000, 3)) * 1.2 * span, near):
        bounded = field.sizes(p, r)
        monkeypatch.setattr(SF, "EDGE_MARGIN_M", None)
        every = field.sizes(p, r)
        monkeypatch.setattr(SF, "EDGE_MARGIN_M", 1e-9)
        assert np.array_equal(bounded[0], every[0]) and np.array_equal(bounded[1], every[1])
    assert cut[0] > 1000                        # the limit did cut searches short


@pytest.mark.parametrize("r", [0.0, 0.4e-3])
def test_a_thin_spots_balls_first_give_the_same_sizes(r, monkeypatch):
    surf, zones = _many()
    field = SF.SizeField(surf, zones, growth=1.25)
    spots = [g for g in field.groups if g.balls is not None]
    assert len(spots) > 10
    measured = [0]
    for g in spots:
        faces = g.faces

        def counted(p, faces=faces):
            measured[0] += len(p)
            return faces(p)

        g.faces = counted
    rng = np.random.default_rng(3)
    lo, hi = field.xyz.min(axis=0), field.xyz.max(axis=0)
    span = hi - lo
    near = field.xyz[rng.integers(0, len(field.xyz), 20_000)] + rng.normal(0.0, 0.3e-3, (20_000, 3))
    for p in (lo - 0.1 * span + rng.random((30_000, 3)) * 1.2 * span, near):
        before = measured[0]
        first = field.sizes(p, r)
        firsts = measured[0] - before
        monkeypatch.setattr(SF, "BALLS_FIRST", False)
        before = measured[0]
        face = field.sizes(p, r)
        faces_first = measured[0] - before
        monkeypatch.setattr(SF, "BALLS_FIRST", True)
        assert np.array_equal(first[0], face[0]) and np.array_equal(first[1], face[1])
        assert firsts < faces_first
