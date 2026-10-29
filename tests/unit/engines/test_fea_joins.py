# Responsibility: Verify PLAN-fea-18 on small assemblies of known geometry: where parts meet is measured on their surfaces (cad/fea_joins) - touches, small-gap joins, clearances left open, skins of no thickness never joining parts - the fuse is checked against it and set aside when contradicted (cad/fea_assembly.joined_interfaces), staging ties the gap joints with their reach, the decks write that reach, the stage says it all (application/fea_check), and the geometry check's task result is plain JSON.
# Boundaries: parts are built in the test (tests/fea_fixtures.anchorage, gapped_blocks); no mesher and no solver run here (tests/unit/engines/test_fea_textbook.py solves the bonded gap in CalculiX).
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.cad import fea_joins as J  # noqa: E402

BAR, BOSS1, BOSS2, BRACKET1, BRACKET2, BOLT1, BOLT2 = (f"Part_{k}" for k in range(1, 8))


def _pairs(interfaces) -> dict[tuple[str, str], dict]:
    return {tuple(i["parts"]): i for i in interfaces}


@pytest.fixture(scope="module")
def skinned():
    return A.record(fx.anchorage(skin=True), mm_per_unit=1.0)


@pytest.fixture(scope="module")
def clean():
    return A.record(fx.anchorage(skin=False), mm_per_unit=1.0)


def test_the_gap_limit_is_half_a_millimetre_or_less_on_small_parts():
    assert J.gap_limit_mm(100.0, 300.0) == pytest.approx(0.5)
    assert J.gap_limit_mm(5.0, 300.0) == pytest.approx(0.1)        # 2% of the smaller part


@pytest.mark.parametrize("which", ["clean", "skinned"])
def test_every_joint_of_the_anchorage_is_found_the_same_on_both_sides(which, request):
    rec = request.getfixturevalue(which)
    got = _pairs(rec["interfaces"])
    want = {(BOSS1, BRACKET1): "touching", (BOSS2, BRACKET2): "touching",       # bracket on its boss
            (BRACKET1, BOLT1): "touching", (BRACKET2, BOLT2): "touching",       # bolt head on its bracket
            (BAR, BOSS1): "gap", (BAR, BOSS2): "gap",                           # boss in its clearance hole
            (BOSS1, BOLT1): "gap", (BOSS2, BOLT2): "gap"}                       # bolt in its bore (thread)
    assert {k: v["kind"] for k, v in got.items()} == want
    g = fx.ANCHORAGE
    for pair, gap in (((BAR, BOSS1), g["boss_gap"]), ((BAR, BOSS2), g["boss_gap"]),
                      ((BOSS1, BOLT1), g["bore_gap"]), ((BOSS2, BOLT2), g["bore_gap"])):
        assert got[pair]["gap_mm"] == pytest.approx(gap, abs=1e-3)
        assert got[pair]["reach_mm"] >= got[pair]["gap_max_mm"]
    # MIRRORED PARTS, MIRRORED JOINTS: the same faces and areas on both sides
    for a, b in (((BOSS1, BRACKET1), (BOSS2, BRACKET2)), ((BAR, BOSS1), (BAR, BOSS2)), ((BOSS1, BOLT1), (BOSS2, BOLT2))):
        assert got[a]["area_mm2"] == pytest.approx(got[b]["area_mm2"], rel=0.02)
    assert rec["free_parts"] == []


@pytest.mark.parametrize("which", ["clean", "skinned"])
def test_a_clearance_a_millimetre_wide_stays_a_clearance(which, request):
    """The bolt sits 1 mm from its bracket's hole and the bracket 2 mm above the bar: no joint there -
    the bolt meets its bracket only under its head."""
    got = _pairs(request.getfixturevalue(which)["interfaces"])
    assert (BAR, BRACKET1) not in got and (BAR, BOLT1) not in got and (BAR, BOLT2) not in got
    head = got[(BRACKET1, BOLT1)]
    assert head["kind"] == "touching" and len(head["faces"][BOLT1]) == 1
    assert head["area_mm2"] == pytest.approx(np.pi * (4.0 ** 2 - 3.0 ** 2), rel=0.05)


def test_a_skin_of_no_thickness_joins_nothing_and_the_fuse_it_fools_is_set_aside(skinned):
    joins = skinned["joins"]
    # the disc across the +y hole crosses the boss and the bolt there: a skin, said, and never a joint
    disc = [f for f in joins["skin_faces"] if f.startswith(f"{BAR}_")]
    assert disc, joins
    assert joins["found_by"] == "surfaces"
    assert {f["kind"] for f in joins["findings"]} <= {"skin_only", "apart", "not_inside", "missed"}
    assert any(n.startswith("Where the parts meet was measured on their surfaces") for n in skinned["notes"])


def test_a_clean_assembly_keeps_the_fuse_and_gains_its_gap_joints_after(clean):
    joins = clean["joins"]
    assert joins["found_by"] == "fuse" and joins["findings"] == [] and joins["skins"] == {}
    kinds = [i["kind"] for i in clean["interfaces"]]
    # the fuse's interfaces first, as they always were; the small-gap joints numbered after them
    assert kinds == sorted(kinds, key=lambda k: k == "gap")
    assert [i["id"] for i in clean["interfaces"]] == [f"Interface_{k}" for k in range(1, len(kinds) + 1)]
    assert set(joins["gaps"]) == {i["id"] for i in clean["interfaces"] if i["kind"] == "gap"}
    assert any("face each other across a small gap" in n for n in clean["notes"])


def test_an_assembly_with_nothing_new_is_recorded_as_before():
    """Two cubes on one face: the fuse stands, nothing is a gap or a skin - no joins key at all."""
    rec = A.record(fx.touching_blocks(10.0), mm_per_unit=1.0)
    assert "joins" not in rec
    assert [i["kind"] for i in rec["interfaces"]] == ["touching"]


def test_a_fuse_the_surfaces_contradict_is_set_aside(monkeypatch):
    """A fuse that puts the far cube "wholly inside" the near one, whose box it never reaches."""
    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(40, 0, 0, 10, 10, 10))
    parts = A.solids(shape)
    bogus = [{"id": "Interface_1", "kind": "touching", "parts": ["Part_1", "Part_2"],
              "faces": {"Part_1": ["Part_1_Face_2"], "Part_2": ["Part_2_Face_1"]}, "at_mm": [10, 5, 5], "area_mm2": 100.0},
             {"id": "Interface_2", "kind": "overlap", "parts": ["Part_1", "Part_3"], "faces": {"Part_1": [], "Part_3": []},
              "at_mm": [45, 5, 5], "overlap_share": 1.0, "area_mm2": None}]
    monkeypatch.setattr(A, "interfaces_of", lambda *_a, **_k: [dict(b) for b in bogus])
    fused = A.fuse(parts)
    vols = [A._props(s, True)[0] for s in parts]
    interfaces, info = A.joined_interfaces(parts, fused, vols, diag=60.0, mm_per_unit=1.0, scale=1.0)
    assert not info["trusted"]
    assert {f["kind"] for f in info["findings"]} == {"apart", "not_inside"}
    assert [(i["kind"], i["parts"]) for i in interfaces] == [("touching", ["Part_1", "Part_2"])]


@pytest.mark.parametrize("build,kind", [(fx.overlapping_blocks, "overlap"), (fx.doubled_block, "overlap"),
                                        (fx.touching_blocks, "touching")])
def test_interference_and_a_body_drawn_twice_agree_with_the_fuse(build, kind):
    """INTERFERENCE (two cubes passing into each other) is measured as surfaces that cross; a body drawn
    twice as wholly inside: the fuse's answer stands and the join check finds nothing to set aside."""
    rec = A.record(build(), mm_per_unit=1.0)
    assert [i["kind"] for i in rec["interfaces"]] == [kind]
    assert (rec.get("joins") or {}).get("findings", []) == []
    parts = A.solids(build())
    m = J.measure(parts, tol=1e-5, mm_per_unit=1.0)
    assert [jt.kind for jt in m.joints.values()] == [kind]


def test_parts_further_apart_than_the_gap_limit_are_not_joined():
    """Two cubes 1 mm apart (more than 2% of a 10 mm cube): no joint, and nothing for the fuse to miss."""
    rec = A.record(fx.gapped_blocks(10.0, 1.0), mm_per_unit=1.0)
    assert rec["interfaces"] == [] and "joins" not in rec


def test_cubes_a_tenth_of_a_millimetre_apart_are_joined_across_the_gap():
    rec = A.record(fx.gapped_blocks(10.0, 0.1), mm_per_unit=1.0)
    (itf,) = rec["interfaces"]
    assert itf["kind"] == "gap" and itf["gap_mm"] == pytest.approx(0.1, abs=1e-4)
    assert itf["area_mm2"] == pytest.approx(100.0, rel=0.05)


def test_a_touch_the_fuse_missed_is_added_and_tied_while_the_fuse_stands(monkeypatch, tmp_path):
    """A MISSED TOUCH (the corpus: pins whose cylinders the fuse did not merge, wires and fins resting
    along a line): an omission, not a wrong answer - the fuse's own joints stand, conformal, and the
    touch is added and tied."""
    shape = fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(0, 10, 0, 10, 10, 10))
    # (Part_2 and Part_3 meet along an edge too: a line joint the fuse has no face for, added as well)
    real = A.interfaces_of

    def without_the_second(*a, **k):
        return [i for i in real(*a, **k) if i["parts"] != ["Part_1", "Part_3"]]

    monkeypatch.setattr(A, "interfaces_of", without_the_second)
    rec = A.record(shape, mm_per_unit=1.0)
    assert rec["joins"]["found_by"] == "fuse" and {f["kind"] for f in rec["joins"]["findings"]} == {"missed"}
    added = {tuple(i["parts"]): i for i in rec["interfaces"] if i.get("added")}
    assert added[("Part_1", "Part_3")]["kind"] == "touching" and not added[("Part_1", "Part_3")].get("line")
    assert added[("Part_2", "Part_3")].get("line")
    side = A.stage(shape, A.BONDED, tmp_path, mm_per_unit=1.0)
    assert side["mode"] == A.PAIR_BONDED
    kinds = {tuple(p["parts"]): p["kind"] for p in side["pairs"]}
    assert kinds[(1, 2)] == A.PAIR_BONDED and kinds[(1, 3)] == A.PAIR_TIE


def test_the_pairs_measured_in_parallel_give_the_same_answer(monkeypatch):
    """The pairs shared among worker processes: the same joints, skins and order as one by one."""
    parts = A.solids(fx.anchorage(skin=True))
    real, ran = J._spawned, []

    def spy(*a, **k):
        got = real(*a, **k)
        ran.append(got is not None)
        return got

    monkeypatch.setattr(J, "WORKER_PAIRS", 1)
    monkeypatch.setattr(J, "_spawned", spy)
    forked = J.measure(parts, tol=3e-4, mm_per_unit=1.0)
    assert ran == [True], "the workers did not run"
    monkeypatch.setattr(J, "MAX_WORKERS", 1)
    serial = J.measure(parts, tol=3e-4, mm_per_unit=1.0)
    assert list(forked.joints) == list(serial.joints)
    for k in serial.joints:
        a, b = forked.joints[k], serial.joints[k]
        assert (a.kind, a.faces_a, a.faces_b) == (b.kind, b.faces_a, b.faces_b)
        assert a.area == pytest.approx(b.area) and (a.gap or 0.0) == pytest.approx(b.gap or 0.0)
    assert forked.skins == serial.skins and forked.near == serial.near


@pytest.fixture(scope="module")
def one_by_one():
    parts = A.solids(fx.anchorage(skin=True))
    keep = J.MAX_WORKERS
    J.MAX_WORKERS = 1
    try:
        return J.measure(parts, tol=3e-4, mm_per_unit=1.0)
    finally:
        J.MAX_WORKERS = keep


@pytest.mark.parametrize("memory", ["none free", "used up while running"])
def test_the_workers_read_only_their_own_parts_and_fit_the_memory(monkeypatch, one_by_one, memory):
    """Each worker reads the parts of its own pairs only (never the whole assembly); no more start at once
    than the job's free memory holds (none free: one at a time), and when the running ones use it up the
    newest is stopped and run again later - the same answer as one by one either way."""
    import subprocess

    parts = A.solids(fx.anchorage(skin=True))
    real_popen, specs, live, most = subprocess.Popen, [], [], [0]

    def popen(cmd, *a, **k):
        specs.append(json.loads(open(cmd[-2]).read()))
        live[:] = [p for p in live if p.poll() is None]
        proc = real_popen(cmd, *a, **k)
        live.append(proc)
        most[0] = max(most[0], len(live))
        return proc

    reads = []

    def free():
        reads.append(1)
        return 64.0 * 2**30 if len(reads) == 1 else 0.0

    real_spawned, answered = J._spawned, []

    def spy(*a, **k):
        got = real_spawned(*a, **k)
        answered.append(got is not None and got[3])
        return got

    monkeypatch.setattr(J, "WORKER_PAIRS", 1)
    monkeypatch.setattr(J, "_memory_free", (lambda: 0.0) if memory == "none free" else free)
    monkeypatch.setattr(J, "_spawned", spy)
    monkeypatch.setattr(subprocess, "Popen", popen)
    spawned = J.measure(parts, tol=3e-4, mm_per_unit=1.0)
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    # the workers' own answer, whole - never the one-by-one fallback in the caller hiding a failure
    assert answered == [True], "the workers did not answer"
    jobs = {s["brep"] for s in specs}
    assert len(jobs) > 1, "the workers did not run"
    if memory == "none free":
        assert most[0] == 1 and len(specs) == len(jobs), "more workers ran at once than the memory holds"
    else:
        assert most[0] > 1 and len(specs) > len(jobs), "no worker was stopped and run again"
    for spec in specs:
        assert len(spec["index"]) < len(parts), "a worker read the whole assembly"
        assert {p for pair in spec["pairs"] for p in pair} == set(spec["index"])
    assert list(spawned.joints) == list(one_by_one.joints)
    for k in one_by_one.joints:
        a, b = spawned.joints[k], one_by_one.joints[k]
        assert (a.a, a.b, a.kind, a.faces_a, a.faces_b) == (b.a, b.b, b.kind, b.faces_a, b.faces_b)
        assert a.area == pytest.approx(b.area) and (a.gap or 0.0) == pytest.approx(b.gap or 0.0)
    assert spawned.skins == one_by_one.skins and spawned.near == one_by_one.near


def test_runs_started_late_get_what_is_left_and_finished_runs_are_kept(monkeypatch, one_by_one):
    """Memory lets one run at a time, and the budget runs out after the first: that run's answer is
    kept (complete False), the runs not started are not measured - never every finished run thrown
    away for a measurement started again with no time left (the code review, P1)."""
    import subprocess
    import time as real_time
    from types import SimpleNamespace

    parts = A.solids(fx.anchorage(skin=True))
    pairs = J._near_pairs(parts, tol=3e-4, mm=1.0)
    budget, clock, specs = 600.0, [0.0], []
    real_popen = subprocess.Popen

    def popen(cmd, *a, **k):
        specs.append(json.loads(open(cmd[-2]).read()))
        proc = real_popen(cmd, *a, **k)
        clock[0] = budget          # the first run uses up the budget (within the grace)
        return proc

    monkeypatch.setattr(J, "time", SimpleNamespace(time=lambda: real_time.time() + clock[0], sleep=real_time.sleep))
    monkeypatch.setattr(J, "_memory_free", lambda: 0.0)
    monkeypatch.setattr(subprocess, "Popen", popen)
    t0 = real_time.time()
    got = J._spawned(parts, pairs, 3, tol=3e-4, mm=1.0, budget_s=budget, t0=t0)
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    assert got is not None, "the finished run was thrown away"
    joints, near, skins, complete, unmeasured = got
    assert complete is False and len(specs) == 1 and specs[0]["budget_s"] <= budget
    first = {tuple(x) for x in specs[0]["pairs"]}
    assert set(joints) <= first
    assert set(joints) == {k for k in one_by_one.joints if k in first}


def test_the_pairs_are_cut_into_runs_of_neighbours():
    pairs = [(0, 1), (0, 2), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]
    runs = J._chunks(pairs, 3)
    assert [len(r) for r in runs] == [3, 2, 2] and sum(runs, []) == pairs
    assert J._chunks(pairs[:2], 8) == [[(0, 1)], [(0, 2)]]


def test_the_gap_switch_off_joins_no_gap(clean):
    parts = A.solids(fx.anchorage(skin=False))
    vols = [A._props(s, True)[0] for s in parts]
    interfaces, info = A.joined_interfaces(parts, A.fuse(parts, fuzzy=1e-6 * 300), vols, diag=300.0,
                                           mm_per_unit=1.0, scale=1.0, gap_joins=False)
    assert info["gaps"] == [] and all(i["kind"] != "gap" for i in interfaces)


@pytest.mark.parametrize("connection,kind", [(A.BONDED, A.PAIR_TIE), (A.CONTACT, A.PAIR_CONTACT)])
def test_staging_joins_each_gap_with_its_reach(connection, kind, tmp_path):
    side = A.stage(fx.anchorage(skin=False), connection, tmp_path, mm_per_unit=1.0)
    gaps = [p for p in side["pairs"] if p.get("gap")]
    assert len(gaps) == 4 and all(p["kind"] == kind for p in gaps)
    for p in gaps:
        assert p["reach"] >= p["gap_max"] >= p["gap"] > 0
        assert all(p["faces"][k] for k in p["faces"])
    # bonded: the touching parts stay one conformal mesh; only the gaps are tied
    if connection == A.BONDED:
        assert side["mode"] == A.PAIR_BONDED
        assert {p["kind"] for p in side["pairs"] if not p.get("gap")} == {A.PAIR_BONDED}
    # the faces a gap joint joins, for the sizing to read (the gap is joined, never meshed across)
    on_disk = json.loads((tmp_path / A.SIDECAR).read_text())
    assert on_disk["joins"]["gap_faces"] and on_disk["joins"]["found_by"] == "fuse"


def test_staging_with_the_gap_switch_off_says_so(tmp_path):
    side = A.stage(fx.anchorage(skin=False), A.BONDED, tmp_path, mm_per_unit=1.0, gap_joins=False)
    assert side["gap_joins"] is False and not [p for p in side["pairs"] if p.get("gap")]


# #
# the decks: each gap joint's reach in each solver's words
# #

def _prep(kind: str, reach):
    from meshpipeline.engines import fea_assembly_deck as AD

    els = np.array([[1, 2, 3, 4], [5, 6, 7, 8]])
    xyz = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
                    [0, 0, 1.0002], [1, 0, 1.0002], [0, 1, 1.0002], [0, 0, 2]], dtype=float) * 1e-3
    a = AD.FaceSet(name="Tie1_Part_1", tag=0, elements=np.array([1]), labels=np.array([1]), nodes=np.array([1, 2, 3]))
    b = AD.FaceSet(name="Tie1_Part_2", tag=0, elements=np.array([2]), labels=np.array([1]), nodes=np.array([5, 6, 7]))
    j = AD.Joint(name="Tie1", kind=kind, parts=(1, 2), sides=(a, b), reach=reach)
    parts = [{"n": 1, "id": "Part_1", "name": "", "first": 1, "last": 1}, {"n": 2, "id": "Part_2", "name": "", "first": 2, "last": 2}]
    return AD.Prepared(node_ids=np.arange(1, 9), xyz=xyz, elements=els, element_part=np.array([1, 2]), parts=parts,
                       faces=[], joints=[j])


def test_each_deck_carries_a_gap_joints_reach(tmp_path):
    from meshpipeline.engines import fea_assembly_deck as AD

    reach = 2.5e-4
    AD.write_inp(tmp_path / "m.inp", _prep("tie", reach))
    inp = (tmp_path / "m.inp").read_text()
    assert "*TIE, NAME=Tie1, POSITION TOLERANCE=" in inp and ", ADJUST=NO" in inp
    tol = float(inp.split("POSITION TOLERANCE=", 1)[1].split(",", 1)[0])
    assert tol == pytest.approx(reach)
    AD.write_bdf(tmp_path / "m.bdf", _prep("tie", reach))
    slave = next(ln for ln in (tmp_path / "m.bdf").read_text().splitlines() if ln[8:16].strip() == "SLAVE")
    assert float(slave[24:32].replace("-", "E-").replace("+", "E+").lstrip("E")) == pytest.approx(reach, rel=1e-3)
    AD.write_cdb(tmp_path / "m.cdb", _prep("tie", reach))
    real = next(ln for ln in (tmp_path / "m.cdb").read_text().splitlines() if ln.startswith("R,2"))
    assert float(real.split(",")[7]) == pytest.approx(reach)          # PINB, the 6th real constant


def test_a_tie_never_moves_a_node(tmp_path):
    # ADJUST=NO on every tie, touching or across a gap: CalculiX's default moves slave corners onto the
    # master without their edge midpoints, and elements turn inside out (asm_fw_case: 8,804 "nonpositive
    # jacobian"); unsettled (no tolerance of its own), a touching tie leaves the reach to the solver
    from meshpipeline.engines import fea_assembly_deck as AD

    AD.write_inp(tmp_path / "m.inp", _prep("tie", None))
    assert "*TIE, NAME=Tie1, ADJUST=NO\nTie1_Part_1_Tied, Tie1_Part_2\n" in (tmp_path / "m.inp").read_text()
    AD.write_cdb(tmp_path / "m.cdb", _prep("tie", None))
    cdb = (tmp_path / "m.cdb").read_text()
    assert "R,2\n" in cdb and "KEYOPT,5,9,1" in cdb


# #
# the stage
# #

def test_the_stage_says_not_yet_measured_rather_than_no_touch():
    from meshpipeline.application import fea_check as F

    view = F.assembly_view({"solids": 3})
    assert view["measured"] is False and view["interfaces"] == []


def test_the_stage_proposes_each_gap_joint_in_plain_words(clean):
    from meshpipeline.application import fea_check as F
    from meshpipeline.cad.fea_measure import FACT_KEY

    view = F.assembly_view({"solids": 7, FACT_KEY: {"measured": True, A.FACT_KEY: clean}})
    assert view["measured"] is True and view["gap_question"] == F.GAP_JOIN_QUESTION and view["gap_default"] is True
    words = [g["words"] for g in view["gap_joins"]]
    assert len(words) == 4 and all(" sits 0." in w and "joined across the gap" in w for w in words)
    assert "4 across a small gap" in view["summary"]
    assert F.gap_joins_answer(False) is False and F.gap_joins_answer(None) is True


# #
# the task's result is plain JSON
# #

def test_the_stored_result_comes_back_as_plain_json(monkeypatch):
    """26 Oct: one numpy integer in the clean check's body numbers ended the scout task with Celery's
    EncodeError after its record was stored. The task's result is plain JSON, as the stored record is."""
    from meshpipeline.application import geometry_check as G

    monkeypatch.setattr(G, "write_status", lambda *a, **k: None)
    got = G.store_status("s", "scouted", "scout", facts={"validity": {"places": [{"bodies": [np.int64(1), np.int64(7)]}]}})
    json.dumps(got)
    assert type(got["facts"]["validity"]["places"][0]["bodies"][0]) is int


# #
# nothing OpenCASCADE throws fails the staging (an industrial STEP: BRepExtrema's StdFail_NotDone, 27 Oct)
# #

def test_a_distance_opencascade_cannot_give_is_unknown_never_a_crash():
    from OCP.TopoDS import TopoDS_Shape

    assert np.isnan(J._dist(TopoDS_Shape(), fx._box(0, 0, 0, 1, 1, 1)))


def _three_blocks():
    """Three cubes in a row: the middle one touches each end one over a face; the ends are apart."""
    return fx._compound(fx._box(0, 0, 0, 10, 10, 10), fx._box(10, 0, 0, 10, 10, 10), fx._box(-10, 0, 0, 10, 10, 10))


def test_a_pair_opencascade_throws_on_is_unmeasured_and_the_fuse_stands_there(monkeypatch, tmp_path):
    real, calls = J._facing, []

    def throws_first(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("StdFail_NotDone: BRepExtrema_DistShapeShape::Value")
        return real(*a, **k)

    monkeypatch.setattr(J, "_facing", throws_first)
    rec = A.record(_three_blocks(), mm_per_unit=1.0)
    # every touch is still there: the first pair's from the fuse, the second measured
    assert sorted(tuple(i["parts"]) for i in rec["interfaces"]) == [("Part_1", "Part_2"), ("Part_1", "Part_3")]
    (u,) = rec["joins"]["unmeasured"]
    assert u["parts"] == ["Part_1", "Part_2"] and "StdFail_NotDone" in u["why"]
    assert any("could not be measured on their surfaces" in n and "the fuse's answer stands" in n for n in rec["notes"])
    calls.clear()
    side = A.stage(_three_blocks(), A.BONDED, tmp_path, mm_per_unit=1.0)
    assert side["mode"] == A.PAIR_BONDED and len(side["pairs"]) == 2


def test_a_measurement_that_fails_outright_leaves_the_fuse_whole(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("StdFail_NotDone")

    monkeypatch.setattr(J, "measure", boom)
    rec = A.record(_three_blocks(), mm_per_unit=1.0)
    assert [i["kind"] for i in rec["interfaces"]] == ["touching", "touching"]
    assert rec["joins"]["found_by"] == "fuse"
    assert any("could not be measured" in n for n in rec["notes"])
    side = A.stage(_three_blocks(), A.BONDED, tmp_path, mm_per_unit=1.0)
    assert len(side["pairs"]) == 2


# #
# parts that meet only along a line, an edge or at a point (the industrial hunt, 27 Oct: a kart's plates on
# its frame, a ring in a countersink, corners a hair apart)
# #

def _tube_on_plate():
    return fx._compound(fx._box(0, 0, -5, 100, 40, 5), fx._cyl(10, 20, 10, 10, 80, axis=(1, 0, 0)))


def test_a_tube_lying_on_a_plate_is_joined_along_the_line():
    rec = A.record(_tube_on_plate(), mm_per_unit=1.0)
    (itf,) = rec["interfaces"]
    assert itf["parts"] == ["Part_1", "Part_2"] and itf["kind"] == "touching" and itf.get("line")
    assert itf["reach_mm"] >= itf["gap_limit_mm"]          # the solver ties a band along the line
    assert any("meet only along a line" in n for n in rec["notes"])


@pytest.mark.parametrize("apart,joined", [(0.0, True), (0.3, True), (0.6, False)])
def test_corners_and_edges_a_hair_apart_are_joined_within_the_gap_limit(apart, joined):
    """Two 30 mm cubes meeting along one edge (apart = 0), or with their edges `apart` mm away diagonally."""
    d = apart / 2 ** 0.5
    rec = A.record(fx._compound(fx._box(0, 0, 0, 30, 30, 30), fx._box(30 + d, 30 + d, 0, 30, 30, 30)), mm_per_unit=1.0)
    assert bool(rec["interfaces"]) is joined
    if joined:
        (itf,) = rec["interfaces"]
        assert itf.get("line") and itf["gap_mm"] == pytest.approx(apart, abs=1e-6)


def test_a_line_contact_is_tied_with_its_reach_in_a_bonded_mesh(tmp_path):
    side = A.stage(_tube_on_plate(), A.BONDED, tmp_path, mm_per_unit=1.0)
    (pair,) = side["pairs"]
    assert pair["kind"] == A.PAIR_TIE and pair["reach"] > 0 and all(pair["faces"].values())


def test_the_stage_says_a_line_contact_in_words():
    from meshpipeline.application import fea_check as F

    assert F.gap_words("Plate", "Frame", 0.0, line=True) == "Plate meets Frame only along a line or an edge - joined there"
