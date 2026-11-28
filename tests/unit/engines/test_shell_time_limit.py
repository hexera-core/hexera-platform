# Responsibility: Hold the shell mesh's time limit to "the best mesh so far, never nothing" (SHMESH-21b) and the
# stamped stream's optional meshes to the time left (SHMESH-21c): a run stopped at its limit delivers the best whole
# mesh it had made, told; an optional mesh that the time left would not cover is not started.
# Boundaries: the forked run and its folder in this process tree; made-up meshes and records, a fake clock; no gmsh.
from __future__ import annotations

import time

import numpy as np

from meshpipeline.engines.gmsh import shell_adapter as sa
from meshpipeline.engines.shell_mesh import ShellMesh


def _mesh(n_trias: int = 0) -> ShellMesh:
    xyz = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0], [2.0, 0.0, 0.0]])
    trias = np.array([[1, 4, 2]] * n_trias, dtype=np.int64).reshape(-1, 3)
    return ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3]], dtype=np.int64), trias=trias,
                     wall=np.ones(1 + n_trias), mid=["Mid_1"] * (1 + n_trias), part=["Part_1"] * (1 + n_trias))


def _applied(past: int = 0, valid: bool = True, beyond: int = 0) -> dict:
    return {"quality": {"valid": valid, "nastran_past": {"taper": past} if past else {}},
            "requirements": {"rows": [{"id": "aspect", "field": "limit:aspect", "beyond": beyond, "met": not beyond,
                                       "worst": 6.0 if beyond else 4.0}]}}


class _Clock:
    """A fake clock for shell_adapter alone: monotonic and wall time move only when a pass says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def time_ns(self) -> int:
        return int(self.now * 1e9)

    def sleep(self, s: float) -> None:
        self.now += s


def test_a_run_stopped_at_its_limit_delivers_its_best_mesh_so_far_and_says_so(monkeypatch):
    # SHMESH-21b: the forked run makes two whole meshes (the second better: none past Nastran's limits), its third
    # pass runs past the limit and the child is stopped - pass 2's mesh is delivered, the record and a note say so
    req = {"limits": [{"id": "aspect", "op": "<=", "target": 5.0, "outer": 5.5}]}

    def run(*_a, requirements=None, **_k):
        sa._so_far(_mesh(2), _applied(past=3), 3, requirements, name="pass 1")
        sa._so_far(_mesh(2), _applied(past=0), 0, requirements, name="pass 2")
        time.sleep(30)                      # pass 3: would cross the limit
        raise AssertionError("pass 3 finished")

    monkeypatch.setattr(sa, "_mesh_shell", run)
    monkeypatch.setattr(sa, "FACE_WATCH", False)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    started = time.monotonic()
    mesh, applied = sa.mesh_shell("x.brep", {}, requirements=req)
    assert time.monotonic() - started < 15.0
    tl = applied["quality"]["time_limit"]
    assert tl["reached"] and tl["meshes_made"] == 2 and tl["delivered_mesh"] == 2 and tl["delivered"] == "pass 2"
    assert any("SHMESH-21b" in n and "after 2 whole mesh(es)" in n for n in mesh.notes)
    assert sa._SO_FAR["dir"] is None


def test_a_run_stopped_before_any_valid_mesh_ends_with_none_as_before(monkeypatch):
    # SHMESH-21b: only an invalid whole mesh was made - the stop says so in words, as SHMESH-21 always did
    import pytest

    def run(*_a, requirements=None, **_k):
        sa._so_far(_mesh(), _applied(valid=False), 0, requirements, name="pass 1")
        time.sleep(30)

    monkeypatch.setattr(sa, "_mesh_shell", run)
    monkeypatch.setattr(sa, "FACE_WATCH", False)
    monkeypatch.setattr(sa, "MESH_DEADLINE_S", 2.0)
    with pytest.raises(ValueError, match="SHMESH-21"):
        sa.mesh_shell("x.brep", {}, requirements={})


def test_a_mesh_delivered_at_the_limit_is_not_aimed_again_at_the_users_count(tmp_path, monkeypatch):
    # SHMESH-21b: SHMESH-23's second aim is a whole shell mesh more - not after the run reached its limit
    import json

    (tmp_path / "shell.json").write_text(json.dumps({"surface_faces": []}))
    calls = []

    def shell(*_a, **_k):
        calls.append(1)
        applied = _applied()
        applied["quality"]["time_limit"] = {"reached": True}
        applied["target"] = {"asked": 1000, "planned": 1000, "correction": 1.0}
        return _mesh(), applied

    monkeypatch.setattr(sa, "mesh_shell", shell)
    _m, applied = sa.mesh_run(tmp_path, target={"elements": 1000})
    assert len(calls) == 1 and "SHMESH-21b" in applied["target"]["not_reaimed"]


def test_the_best_so_far_is_ranked_as_the_mesher_ranks_its_candidates():
    # SHMESH-21b: valid first, then inside Nastran's limits, then the user's lines, then fewer triangles; a tie keeps
    # the mesh made first
    req = {"limits": [{"id": "aspect", "op": "<=", "target": 5.0, "outer": 5.5}]}

    def v(mesh, applied, n):
        return sa._so_far_view(mesh, applied, n)

    good = v(_mesh(2), _applied(), 0)
    assert sa._so_far_beats(good, v(_mesh(0), _applied(valid=False), 0), req)
    assert sa._so_far_beats(good, v(_mesh(0), _applied(past=1), 1), req)
    assert sa._so_far_beats(good, v(_mesh(0), _applied(beyond=2), 0), req)
    assert sa._so_far_beats(v(_mesh(1), _applied(), 0), good, req)
    assert not sa._so_far_beats(v(_mesh(2), _applied(), 0), good, req)


def test_an_optional_stamped_mesh_the_time_left_does_not_cover_is_not_started(monkeypatch):
    # SHMESH-21c (fake clock): each whole path takes 300 s of a 900 s run. The first path and SHMESH-76's first
    # candidate fit; the third whole mesh (without the steps and the fit-mapping) would end past the limit - it is not
    # started, the record and a note say so, and pass 2's mesh (the better) is delivered
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    calls = []

    def path(*_a, **_k):
        calls.append(1)
        clock.sleep(300.0)
        mesh = _mesh(4 if len(calls) == 1 else 2)
        applied = _applied()
        applied["quality"].update(strip_yield=True, fit_mapped=3)
        return mesh, applied, 0

    monkeypatch.setattr(sa, "_narrow_kept_path", path)
    monkeypatch.setattr(sa, "FACE_KEEP", False)
    monkeypatch.setattr(sa, "FIT_PER_FACE", False)
    monkeypatch.setattr(sa, "STEP_PER_STRIP", False)
    monkeypatch.setitem(sa._RUN, "end", clock.now + 900.0)
    monkeypatch.setitem(sa._RUN, "cut", False)
    mesh, applied, _n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert len(calls) == 2
    kept = applied["quality"]["strip_steps_kept"]
    assert kept["kept"] == "without the steps" and len(mesh.trias) == 2
    assert [s["mesh"] for s in kept["skipped"]] == ["without the steps and the fit-mapping"]
    assert "SHMESH-21c" in kept["skipped"][0]["why"]
    assert any("SHMESH-21c" in n for n in mesh.notes)


def _choosing_path(clock, calls, spot_s):
    """A fake candidate path whose spot pass is decided as _mesh_path decides it (SHMESH-21d / 31h): the first path
    keeps back the first meshes owed after it, a spot pass that the time left less that cannot afford (priced at
    _spot_price) waits, one that runs past its bound is stopped and waits; a spot pass made clears the elements past
    the solver's limits. Each call is recorded as (its name, TRANSITIONS, whether its spot pass waited)."""

    def path(*_a, **_k):
        label, trans = sa._SO_FAR["label"], sa.TRANSITIONS
        clock.sleep(50.0 if not calls else 10.0)
        if sa._RUN.get("first_s") is None:
            sa._RUN["first_s"] = 50.0
        applied = _applied(past=3)
        applied["quality"].update(strip_yield=True, fit_mapped=3)
        reserve = sa._choose_reserve(applied)
        waited = bool(sa._CHOOSE.get("defer"))
        if not waited and not sa._affords(sa._spot_price() + reserve):
            assert reserve and sa._affords(reserve)
            sa._choose_start("the spot pass and the meshes to compare would not fit")
            waited = True
        if not waited:
            bound = sa._limit() - reserve
            if spot_s > bound:
                clock.sleep(bound)
                sa._SPOT_COST["s"] = bound
                sa._choose_start("the spot pass was stopped")
                waited = True
            else:
                clock.sleep(spot_s)
                sa._SPOT_COST["s"] = max(sa._SPOT_COST["s"] or 0.0, spot_s)
        calls.append((label, trans, waited))
        past = 3 if waited else 0
        applied["quality"]["nastran_past"] = {"taper": past} if past else {}
        if waited:
            applied["quality"]["own_size_deferred"] = True
        return _mesh(4 if trans else 2), applied, past

    return path


def _choose_setup(monkeypatch, left_s, spot_s):
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    calls: list = []
    monkeypatch.setattr(sa, "_narrow_kept_path", _choosing_path(clock, calls, spot_s))
    for flag in ("FACE_KEEP", "FIT_PER_FACE", "STEP_PER_STRIP"):
        monkeypatch.setattr(sa, flag, False)
    monkeypatch.setitem(sa._RUN, "end", clock.now + left_s)
    monkeypatch.setitem(sa._RUN, "first_s", None)
    monkeypatch.setitem(sa._RUN, "cut", False)
    monkeypatch.setitem(sa._SPOT_COST, "s", None)
    return calls


def test_when_time_is_short_the_candidates_are_chosen_first_and_the_one_kept_repaired_last(monkeypatch):
    # SHMESH-21d (fake clock): 210 s; the first mesh 50 s, two meshes to compare owed (110 s kept back): its spot pass
    # (priced at 50 s) and they would not fit, so it waits; both candidates are made as first meshes (10 s each), the
    # better (fewer triangles) is kept and made again with its spot pass - delivered with none past the solver's limits
    calls = _choose_setup(monkeypatch, 210.0, 50.0)
    mesh, applied, n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert [c[2] for c in calls] == [True, True, True, False]          # three first meshes, then the repair
    assert calls[-1] == ("without the steps, repaired", False, False)
    chose = applied["quality"]["chose_first"]
    assert chose["kept"] == "without the steps" and chose["repaired"] == "kept"
    assert n == 0 and len(mesh.trias) == 2
    assert sa._CHOOSE["tight"] is False and sa._CHOOSE["defer"] is False


def test_a_spot_pass_that_runs_into_the_meshes_to_compare_is_stopped_and_they_are_made(monkeypatch):
    # SHMESH-21d (fake clock), s33's shape: 900 s; the first mesh 50 s, its spot pass would take 750 s - it is stopped
    # at the time left less the two first meshes owed (820 s - 110 s), both are made, the best first mesh is kept; no
    # time is left for its repair (priced at the spot pass measured): told, nothing cut
    calls = _choose_setup(monkeypatch, 900.0, 750.0)
    mesh, applied, n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert [c[2] for c in calls] == [True, True, True]
    chose = applied["quality"]["chose_first"]
    assert chose["kept"] == "without the steps" and chose["repaired"].startswith("not started")
    assert len(mesh.trias) == 2 and n == 3 and not sa._RUN["cut"]
    assert any("SHMESH-21d" in t for t in mesh.notes)


def test_the_first_paths_bolt_ring_trial_waits_where_it_would_take_the_time_kept_back(monkeypatch):
    # SHMESH-21d: on s33 the first path's spot pass finished, then its bolt-ring trial (a first mesh and a spot pass
    # again) took the rest of the run; with 110 s kept back for two meshes to compare, a trial priced at 50 + 300 s
    # does not start in 400 s left; it does where 500 s are left, or with no spot pass to price
    monkeypatch.setitem(sa._RUN, "first_s", 50.0)
    monkeypatch.setitem(sa._SPOT_COST, "s", 300.0)
    monkeypatch.setattr(sa, "_CHOOSE", {**sa._CHOOSE, "first": True, "reserve": 110.0})
    monkeypatch.setitem(sa._RUN, "end", time.monotonic() + 430.0)        # 400 s after the margin
    assert sa._choose_trial_waits([("spot",)])
    assert not sa._choose_trial_waits([])                              # no spot pass to price: 160 s fit
    monkeypatch.setitem(sa._RUN, "end", time.monotonic() + 530.0)
    assert not sa._choose_trial_waits([("spot",)])
    sa._CHOOSE["first"] = False
    monkeypatch.setitem(sa._RUN, "end", time.monotonic() + 40.0)
    assert not sa._choose_trial_waits([("spot",)])                     # any other path: as before


def _plain_setup(monkeypatch, stamped_past: int):
    """A stamped part (a surface file) whose stamped candidates stand `stamped_past` past the solver's limits whether
    or not their spot passes are made; the plain mesh (every stamped step off) 1 past as a first mesh, 0 repaired."""
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    calls: list = []

    def path(*_a, **_k):
        plain, waited = bool(sa._STAMPED_OFF_NOW[0]), bool(sa._CHOOSE.get("defer"))
        calls.append((sa._SO_FAR["label"], plain, waited))
        clock.sleep(30.0 if not waited else 20.0)
        if sa._RUN.get("first_s") is None:
            sa._RUN["first_s"] = 20.0
        past = (1 if waited else 0) if plain else stamped_past
        applied = _applied(past=past)
        if not plain:
            applied["quality"].update(strip_yield=True)
        if waited and past:
            applied["quality"]["own_size_deferred"] = True
        return _mesh(0 if plain else 2), applied, past

    monkeypatch.setattr(sa, "_narrow_kept_path", path)
    for flag in ("FACE_KEEP", "FIT_PER_FACE", "STEP_PER_STRIP"):
        monkeypatch.setattr(sa, flag, False)
    monkeypatch.setitem(sa._RUN, "end", clock.now + 900.0)
    monkeypatch.setitem(sa._RUN, "first_s", None)
    monkeypatch.setitem(sa._RUN, "cut", False)
    monkeypatch.setitem(sa._SPOT_COST, "s", None)
    return calls


def test_where_every_stamped_mesh_is_past_the_solvers_limits_the_plain_mesh_joins_and_wins(monkeypatch):
    # SHMESH-76p2: the first stamped mesh 3 past the solver's limits: the plain mesh is the next mesh made, as a first
    # mesh (1 past), before the stamped candidates (3 past each); ranked after them in SHMESH-76's order, kept, then
    # repaired last (0 past) and told
    calls = _plain_setup(monkeypatch, 3)
    mesh, applied, n = sa._yield_both("x.brep", {"source": "surface"}, requirements={"limits": []})
    assert [c[1:] for c in calls] == [(False, False), (True, True), (False, False), (False, False), (True, False)]
    assert calls[-1][0] == sa.PLAIN_NAME + ", repaired"
    assert n == 0 and applied["quality"]["plain_joined"]["kept"] is True
    assert applied["quality"]["chose_first"]["repaired"] == "kept"
    assert any("SHMESH-76p" in t for t in mesh.notes)


def test_the_first_paths_spot_pass_keeps_back_the_plain_meshs_time(monkeypatch):
    # SHMESH-76p2: on a stamped part the first path keeps back the plain mesh's first mesh too (2 meshes to compare
    # and the plain one, each at the first mesh's 50 s, +10%)
    monkeypatch.setitem(sa._RUN, "first_s", 50.0)
    applied = _applied(past=3)
    applied["quality"].update(strip_yield=True)
    monkeypatch.setattr(sa, "_CHOOSE", {**sa._CHOOSE, "armed": True, "first": True, "plain": True})
    assert abs(sa._choose_reserve(applied) - 3 * 50.0 * 1.1) < 1e-9
    monkeypatch.setattr(sa, "_CHOOSE", {**sa._CHOOSE, "armed": True, "first": True, "plain": False})
    assert abs(sa._choose_reserve(applied) - 2 * 50.0 * 1.1) < 1e-9


def test_where_a_stamped_mesh_is_inside_the_solvers_limits_no_plain_mesh_is_made(monkeypatch):
    # SHMESH-76p: a part whose stamped meshes stand inside the solver's limits is meshed exactly as before
    calls = _plain_setup(monkeypatch, 0)
    _m, applied, n = sa._yield_both("x.brep", {"source": "surface"}, requirements={"limits": []})
    assert all(not c[1] for c in calls) and "plain_joined" not in applied["quality"] and n == 0


def test_with_time_for_everything_each_candidate_is_made_whole_as_before(monkeypatch):
    # SHMESH-21d: 900 s and spot passes of 50 s - nothing waits, nothing is chosen first, no repair pass
    calls = _choose_setup(monkeypatch, 900.0, 50.0)
    _m, applied, n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert [c[2] for c in calls] == [False, False, False] and "chose_first" not in applied["quality"]
    assert n == 0


def test_with_time_to_spare_every_optional_mesh_starts_as_before(monkeypatch):
    # SHMESH-21c: a run far inside its limit starts each optional mesh exactly as before
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    calls = []

    def path(*_a, **_k):
        calls.append(1)
        clock.sleep(10.0)
        applied = _applied()
        applied["quality"].update(strip_yield=True, fit_mapped=3)
        return _mesh(2), applied, 0

    monkeypatch.setattr(sa, "_narrow_kept_path", path)
    monkeypatch.setattr(sa, "FACE_KEEP", False)
    monkeypatch.setattr(sa, "FIT_PER_FACE", False)
    monkeypatch.setattr(sa, "STEP_PER_STRIP", False)
    monkeypatch.setitem(sa._RUN, "end", clock.now + 900.0)
    monkeypatch.setitem(sa._RUN, "cut", False)
    _mesh_, applied, _n = sa._yield_both("x.brep", {}, requirements={"limits": []})
    assert len(calls) == 3
    assert "skipped" not in applied["quality"]["strip_steps_kept"]


def test_a_later_spot_pass_is_priced_with_the_growth_the_last_one_made(monkeypatch):
    # SHMESH-31j (fake clock), cen_s0195's numbers: the first mesh 305 s; the first spot pass took 271 s and grew the
    # mesh 52,951 -> 62,509 elements; 307 s left. SHMESH-31h's price (305 s) fits, and the second pass ran to the
    # deadline for nothing; priced with the growth it does not fit, so it is not started. With the time, it is.
    clock = _Clock()
    monkeypatch.setattr(sa, "time", clock)
    monkeypatch.setitem(sa._RUN, "first_s", 305.0)
    monkeypatch.setitem(sa._SPOT_COST, "s", 271.0)
    monkeypatch.setitem(sa._RUN, "end", clock.now + 307.0 + sa.TIME_MARGIN_S)
    grown = 62509 / 52951
    assert sa._affords(sa._spot_price())
    assert abs(sa._later_spot_price(grown) - 305.0 * grown) < 1e-9
    assert not sa._affords(sa._later_spot_price(grown))
    # a pass that made the mesh smaller: still PRICE_MARGIN above the price measured
    assert abs(sa._later_spot_price(0.9) - 305.0 * (1.0 + sa.PRICE_MARGIN)) < 1e-9
    monkeypatch.setitem(sa._RUN, "end", clock.now + 400.0 + sa.TIME_MARGIN_S)
    assert sa._affords(sa._later_spot_price(grown))
    monkeypatch.setattr(sa, "SPOT_LATER_PRICED", False)
    assert sa._later_spot_price(grown) == sa._spot_price()
