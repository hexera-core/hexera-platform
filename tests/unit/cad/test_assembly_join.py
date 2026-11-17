# Responsibility: Verify PLAN-fea-35 on assemblies built with the CAD kernel: only parts that touch are fused, group by group, and the result is the single fuse's; each fuse is time-boxed in a child process, a group past its limit is joined pair by pair, and what still cannot be fused is left apart and tied where it meets, said in words - never a wait without a bound.
# Boundaries: parts are built here (tests/fea_fixtures.py and the perforated blocks below); fuses run in real child processes (cad/bounded_steps); no mesher runs here (tests/unit/engines/test_fea_assembly.py meshes assemblies).
from __future__ import annotations

import time
from collections import Counter

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import assembly_join as AJ  # noqa: E402
from meshpipeline.cad import bounded_steps as BS  # noqa: E402
from meshpipeline.cad import fea_assembly as A  # noqa: E402

FUZZY = 1e-5


def _perforated(z0: float, n: int = 12, off: float = 0.0):
    """A 10 mm cube from z0, with an n x n grid of round holes through it along z: two of them stacked with their
    grids offset make a pair whose fuse takes a second or more (every hole's edge crosses its neighbours')."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.TopTools import TopTools_ListOfShape

    tools = TopTools_ListOfShape()
    for i in range(n):
        for j in range(n):
            tools.Append(fx._cyl((i + 0.5 + off) * 10 / n, (j + 0.5 + off) * 10 / n, z0 - 1, 10 / n / 2.5, 12))
    args = TopTools_ListOfShape()
    args.Append(fx._box(0, 0, z0, 10, 10, 10))
    cut = BRepAlgoAPI_Cut()
    cut.SetArguments(args)
    cut.SetTools(tools)
    cut.Build()
    return cut.Shape()


def _faces_of(fused) -> Counter:
    """The fuse's faces as (their origins, area): what a stage names and shares, whatever the order."""
    out: Counter = Counter()
    for f, o in zip(A._faces(fused.shape), fused.face_origins, strict=True):
        out[(tuple(sorted(o)), round(A._props(f, False)[0], 6))] += 1
    return out


def _solids_of(fused) -> Counter:
    return Counter((tuple(sorted(o)), round(A._props(s, True)[0], 6))
                   for s, o in zip(A.solids(fused.shape), fused.solid_parts, strict=True))


def _chain_and_one_apart():
    """Three 10 mm cubes touching in a row (x 0..30), the third passing 2 mm into a fourth, and a fifth cube
    50 mm away that touches nothing."""
    return [fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 10, 10, 10),
            fx._box(28, 0, 0, 10, 10, 10), fx._box(90, 0, 0, 10, 10, 10)]


def test_parts_that_touch_in_a_chain_are_one_group_and_the_join_is_the_single_fuse():
    parts = _chain_and_one_apart()
    joined = AJ.join(parts, fuzzy=FUZZY)
    single = A.fuse(parts, fuzzy=FUZZY)
    assert not joined.error and joined.unfused == []
    # ONE GROUP of the four that touch; the fifth is never in a fuse
    assert [g["parts"] for g in joined.join["groups"]] == [[1, 2, 3, 4]]
    assert joined.join["groups"][0]["outcome"] == AJ.FUSED
    # THE SAME CONFORMAL ASSEMBLY: the same faces from the same faces of the same parts, the same solids
    assert _faces_of(joined) == _faces_of(single)
    assert _solids_of(joined) == _solids_of(single)
    # and the same interfaces, kind for kind and face for face
    vols = [A._props(s, True)[0] for s in parts]

    def said(itfs):
        return sorted((i["kind"], tuple(i["parts"]), tuple(tuple(v) for v in i["faces"].values())) for i in itfs)

    assert said(A.interfaces_of(joined, vols)) == said(A.interfaces_of(single, vols))


def test_a_part_that_touches_nothing_is_left_as_it_is():
    parts = _chain_and_one_apart()
    joined = AJ.join(parts, fuzzy=FUZZY)
    alone = [o for o in joined.solid_parts if o == [5]]
    assert alone == [[5]]
    fifth = [s for s, o in zip(A.solids(joined.shape), joined.solid_parts, strict=True) if o == [5]][0]
    assert fifth.IsSame(parts[4])                                   # the very solid, not a copy through a fuse
    two = AJ.join([fx._box(0, 0, 0, 10, 10, 10), fx._box(30, 0, 0, 10, 10, 10)], fuzzy=FUZZY)
    assert two.join["groups"] == [] and two.join["touch"]["touching"] == 0


def test_the_touch_check_measures_the_solids_not_their_boxes():
    # a cube inside a hollow box, 2 mm clear of it all round: the boxes meet, the parts do not
    hollow = fx._cut(fx._box(0, 0, 0, 20, 20, 20), fx._box(2, 2, 2, 16, 16, 16))
    inner = fx._box(4, 4, 4, 12, 12, 12)
    assert AJ.touches(hollow, inner, 1e-4)["touch"] is False
    # a cube wholly inside another (a body drawn inside a boss): no faces near, still touching
    got = AJ.touches(fx._box(0, 0, 0, 20, 20, 20), fx._box(5, 5, 5, 4, 4, 4), 1e-4)
    assert got == {"touch": True, "how": "inside", "distance": 0.0}
    # faces a hair apart, inside the reach, touch; a tenth of a millimetre apart, they do not
    assert AJ.touches(fx._box(0, 0, 0, 10, 10, 10), fx._box(10.00001, 0, 0, 10, 10, 10), 1e-4)["touch"] is True
    assert AJ.touches(fx._box(0, 0, 0, 10, 10, 10), fx._box(10.1, 0, 0, 10, 10, 10), 1e-4)["touch"] is False
    joined = AJ.join([hollow, inner], fuzzy=FUZZY)
    assert joined.join["touch"] == {**joined.join["touch"], "box_pairs": 1, "touching": 0}
    assert joined.join["groups"] == []


def _slow_chain():
    """Part 1 and Part 2 perforated and stacked (their fuse takes a second or more), Part 3 a plain cube on top
    of Part 2 (x 0..10, z 20..30): one group of three."""
    return [_perforated(0.0), _perforated(10.0, off=0.37), fx._box(0, 0, 20, 10, 10, 10)]


def test_a_group_past_its_limit_is_joined_pair_by_pair_and_the_slow_pair_left_apart(monkeypatch):
    parts = _slow_chain()
    faces = [len(A._faces(s)) for s in parts]
    slow = faces[0] + faces[1]
    # every fuse that holds both perforated blocks gets a limit far under its time; any other a usual one
    monkeypatch.setattr(AJ, "budget_s", lambda n: 0.05 if n >= slow else 120.0)
    t0 = time.time()
    joined = AJ.join(parts, fuzzy=FUZZY)
    assert time.time() - t0 < 60
    rec = joined.join
    assert [(g["parts"], g["outcome"]) for g in rec["groups"]] == [([1, 2, 3], BS.TIME)]
    assert sorted((p["parts"], p["outcome"]) for p in rec["pairs"]) == [([1, 2], BS.TIME), ([2, 3], AJ.FUSED)]
    # Part 2 and Part 3 are one conformal piece (they share their face); Part 1 is left as it is
    assert sorted(sorted(o) for o in joined.solid_parts) == [[1], [2], [3]]
    shared = [o for o in joined.face_origins if len({p for p, _j in o}) > 1]
    assert shared and all(sorted({p for p, _j in o}) == [2, 3] for o in shared)
    # the pair no fuse joined, with no fuse of its own to say where it meets (the surfaces will)
    assert [u["parts"] for u in joined.unfused] == [[1, 2]] and joined.unfused[0]["pair"] is None
    assert rec["unfused"] == [[1, 2]]
    assert AJ.words(rec, lambda p: {1: "Base plate"}.get(p, "")) == [
        "Two parts could not be joined in the CAD in the time set for it, so they are meshed apart and tied where "
        "they meet: Base plate (Part_1) and Part_2."]


def _fuses_past_their_limit(monkeypatch, fails):
    """Every fuse step whose parts (0-based) `fails` says runs past its limit (a box fuse is too quick to time out
    on its own)."""
    real = BS.run

    def run(module, payload, steps, budgets, **kw):
        got = real(module, payload, steps, budgets, **kw)
        for s, st in zip(got, steps, strict=True):
            if st.get("op") == "fuse" and fails(set(st["parts"])):
                s.ok, s.why, s.value = False, BS.TIME, None
        return got

    monkeypatch.setattr(BS, "run", run)


def test_pairs_that_make_the_whole_group_again_are_not_fused_again_but_tied(monkeypatch):
    parts = [fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 10, 10, 10)]
    _fuses_past_their_limit(monkeypatch, lambda members: len(members) > 2)
    joined = AJ.join(parts, fuzzy=FUZZY)
    assert [g["outcome"] for g in joined.join["groups"]] == [BS.TIME]
    # both pairs fuse, and together they are the group that ran out: that fuse is not run a second time
    assert [r["outcome"] for r in joined.join["regroups"]] == ["not tried again"]
    assert sorted(u["parts"] for u in joined.unfused) == [[1, 2], [2, 3]]
    # each pair's own fuse says where it meets: one shared face between the two
    for u in joined.unfused:
        itf = A.interfaces_of(u["pair"], [1000.0] * 3)
        assert [i["kind"] for i in itf] == ["touching"] and itf[0]["parts"] == [f"Part_{p}" for p in u["parts"]]


def test_a_smaller_group_the_pairs_make_is_fused_again_and_only_the_slow_pair_left_apart(monkeypatch):
    parts = [fx._box(10 * k, 0, 0, 10, 10, 10) for k in range(4)]
    # the four together and the last pair run past their limits; every other fuse finishes
    _fuses_past_their_limit(monkeypatch, lambda members: len(members) == 4 or {2, 3} <= members)
    joined = AJ.join(parts, fuzzy=FUZZY)
    assert [(r["parts"], r["outcome"]) for r in joined.join["regroups"]] == [([1, 2, 3], AJ.FUSED)]
    assert sorted(sorted(o) for o in joined.solid_parts) == [[1], [2], [3], [4]]
    shared = sorted(tuple(sorted({p for p, _j in o})) for o in joined.face_origins if len({p for p, _j in o}) > 1)
    assert shared == [(1, 2), (2, 3)]                              # one conformal piece of the first three
    assert [u["parts"] for u in joined.unfused] == [[3, 4]] and joined.unfused[0]["pair"] is None


def test_the_whole_join_is_held_to_its_total_time():
    parts = _slow_chain()
    t0 = time.time()
    joined = AJ.join(parts, fuzzy=FUZZY, total_s=0.5)
    assert time.time() - t0 < 30
    # nothing fused in time: every part as it is, every touching pair left to be tied
    assert sorted(sorted(o) for o in joined.solid_parts) == [[1], [2], [3]]
    assert {tuple(u["parts"]) for u in joined.unfused} >= {(1, 2), (2, 3)}


def test_a_fuse_past_its_limit_is_stopped_and_the_next_step_still_runs(tmp_path):
    parts = _slow_chain()
    brep = tmp_path / "parts.brep"
    from OCP.BRepTools import BRepTools

    assert BRepTools.Write_s(A.compound(parts), str(brep))
    steps = [{"op": "fuse", "parts": [0, 1], "fuzzy": FUZZY, "mm_per_unit": 1.0},
             {"op": "fuse", "parts": [1, 2], "fuzzy": FUZZY, "mm_per_unit": 1.0}]
    t0 = time.time()
    got = BS.run(AJ.MODULE, {"brep": str(brep)}, steps, [0.05, 120.0], workdir=tmp_path / "w")
    assert time.time() - t0 < 60
    assert got[0].why == BS.TIME and not got[0].ok
    assert got[1].ok and got[1].value["error"] == "" and (tmp_path / "w" / "1.brep").exists()


def test_a_bonded_stage_ties_the_pair_no_fuse_joined_and_says_so(tmp_path, monkeypatch):
    _fuses_past_their_limit(monkeypatch, lambda members: {0, 1} <= members)
    three = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(20, 0, 0, 10, 10, 10))
    sidecar = A.stage(three, A.BONDED, tmp_path, names=["Base plate", "", ""], mm_per_unit=1.0)
    assert sidecar["mode"] == A.PAIR_BONDED
    kinds = {tuple(p["parts"]): p["kind"] for p in sidecar["pairs"]}
    # Part 2 and Part 3 share their nodes; Part 1 and Part 2 are tied where they touch (found on their surfaces)
    assert kinds == {(2, 3): A.PAIR_BONDED, (1, 2): A.PAIR_TIE}
    tied = [p for p in sidecar["pairs"] if p["kind"] == A.PAIR_TIE][0]
    assert tied["faces"] == {"1": ["Part_1_Face_2"], "2": ["Part_2_Face_1"]}
    assert sidecar["join"]["unfused"] == [[1, 2]] and sidecar["join"]["rule"] == "PLAN-fea-35"
    assert any(n.startswith("Two parts could not be joined in the CAD") and "Base plate (Part_1) and Part_2" in n
               for n in sidecar["notes"])


def test_a_bonded_stage_where_no_fuse_finished_is_meshed_apart_and_tied(tmp_path, monkeypatch):
    _fuses_past_their_limit(monkeypatch, lambda members: {0, 1} <= members)
    sidecar = A.stage(fx.touching_blocks(), A.BONDED, tmp_path, mm_per_unit=1.0)
    assert sidecar["mode"] == A.PAIR_TIE
    assert [(p["kind"], p["parts"]) for p in sidecar["pairs"]] == [(A.PAIR_TIE, [1, 2])]
    assert any("could not be joined in the CAD" in n for n in sidecar["notes"])


def test_the_budget_grows_with_the_faces_and_is_capped():
    assert AJ.budget_s(0) == AJ.FUSE_FLOOR_S
    assert AJ.budget_s(100) < AJ.budget_s(1000) <= AJ.FUSE_CAP_S
    assert AJ.budget_s(10**7) == AJ.FUSE_CAP_S
