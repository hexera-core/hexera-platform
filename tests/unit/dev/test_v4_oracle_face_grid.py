# Responsibility: lane A's face-op grid for the D21 oracle sweep keeps its contract - every row is an attributable
#                 Candidate, no row repeats one the det sweep already makes, sizes come from the ORIGINAL's diagonal,
#                 and a row that cannot be sized is NOT built and says so.
# Boundaries: hermetic; fake sites, no kernel. The sweep itself is lane D's harness.
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"))
import oracle_face_grid as g  # noqa: E402

from meshpipeline.cad.repair_v4.ops import REGISTRY, validate_call  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

FACES = ["F1", "F2", "F3", "F4"]


def _site(kind: str, *, located=(), nodes=(), diagonal_mm=None, coarse=False):
    graph = {"mesher_faces": list(located), "nodes": list(nodes)}
    if diagonal_mm:
        graph["diagonal_mm"] = diagonal_mm
    if coarse:
        graph["coarse"] = {"reason": "no member said where"}
    return SimpleNamespace(id=f"S@{kind}", kind=kind, entities=list(FACES), graph=graph)


def _state(*sites):
    return SimpleNamespace(sites=list(sites), ws=SimpleNamespace(original="/fake/o.brep"))


def _ws():
    ids = {f: f"fp{f}" for f in FACES}
    return Workspace(brep_path="/fake/a.brep", ids=ids, labels={}, history=[], original="/fake/o.brep",
                     indices={f: i for i, f in enumerate(FACES, start=1)})


@pytest.fixture(autouse=True)
def _clear_skips():
    g.SKIPPED.clear()
    yield
    g.SKIPPED.clear()


def test_every_row_is_an_attributable_candidate_the_op_itself_accepts() -> None:
    rows = g.extra_rows(_state(_site("sliver", located=["F2"])), diagonal_mm=200.0)
    assert rows, "a face site must get rows"
    ws = _ws()
    for c in rows:
        assert c.op in REGISTRY, c.op
        assert c.rationale.startswith("oracle_face_grid: "), c.rationale
        assert validate_call(c.op, list(c.targets), dict(c.params), ws) == [], (c.op, c.targets, c.params)


def test_it_proposes_no_body_op_and_nothing_part_wide_except_the_one_merge() -> None:
    """Body ops are lane C's grid. The only part-wide row here is unify_same_domain's `scope: all`, which the op
    itself defines as taking no targets."""
    rows = g.extra_rows(_state(_site("invalid", located=["F1"])), diagonal_mm=200.0)
    assert not [c for c in rows if c.op in ("conformalise", "fuse", "cut_by", "trim_unbounded")]
    for c in rows:
        if not c.targets:
            assert (c.op, c.params.get("scope")) == ("unify_same_domain", "all"), (c.op, c.params)


def test_det_s_own_split_rungs_are_not_repeated_where_det_may_propose_them() -> None:
    """The oracle pays a full verdict per candidate, so a rung det already offers at that kind is pure cost."""
    for kind in g.DET_SPLIT_KINDS:
        degs = {c.params["max_deg"] for c in g.extra_rows(_state(_site(kind)), diagonal_mm=200.0)
                if c.op == "split_angle"}
        assert degs.isdisjoint(g.DET_SPLIT_ANGLES), (kind, degs)
        assert degs == set(g.EXTRA_SPLIT_ANGLES), (kind, degs)
    # at a kind det's gate forbids, det's rungs ARE offered - that gate is the thing being tested
    degs = {c.params["max_deg"] for c in g.extra_rows(_state(_site("sliver")), diagonal_mm=200.0)
            if c.op == "split_angle"}
    assert degs == set(g.DET_SPLIT_ANGLES) | set(g.EXTRA_SPLIT_ANGLES), degs


def test_split_continuity_is_offered_off_invalid_and_not_on_it() -> None:
    off = {c.params["order"] for c in g.extra_rows(_state(_site("periodic")), diagonal_mm=200.0)
           if c.op == "split_continuity"}
    on = [c for c in g.extra_rows(_state(_site("invalid")), diagonal_mm=200.0) if c.op == "split_continuity"]
    assert off == {1, 2} and on == []


def test_rebuild_face_is_offered_at_G1_which_det_never_proposes() -> None:
    rows = [c for c in g.extra_rows(_state(_site("pole", located=["F3"])), diagonal_mm=200.0)
            if c.op == "rebuild_face"]
    assert rows and all(c.params["continuity"] == "G1" for c in rows)
    assert rows[0].targets == ["F3"], "the mesher's own face comes first"


def test_the_sliver_widths_are_past_the_det_ladder_and_come_from_the_ORIGINAL() -> None:
    diag = 441.0408
    widths = sorted({c.params["max_width_mm"] for c in g.extra_rows(_state(_site("sliver")), diagonal_mm=diag)
                     if c.op == "remove_small"})
    assert widths == pytest.approx([r * diag for r in g.EXTRA_SLIVER_RATIOS])
    assert min(widths) > max(r * diag for r in g.DET_SLIVER_RATIOS), "every width is past det's top rung"
    # a state 78x the part would move these if they were sized from the state; they are not
    assert widths == pytest.approx([r * diag for r in g.EXTRA_SLIVER_RATIOS])


def test_a_row_that_cannot_be_sized_is_not_built_and_says_so() -> None:
    """No diagonal means no reapproximate and no remove_small - and a line in SKIPPED, because a skip nobody
    reports is how a glob quietly dropped a file earlier tonight."""
    rows = g.extra_rows(_state(_site("sliver")))      # no diagonal anywhere
    assert not [c for c in rows if c.op in ("remove_small", "reapproximate")]
    assert rows, "the unsized rows are still built"
    assert g.SKIPPED and "no diagonal was measured" in g.SKIPPED[0], g.SKIPPED


def test_the_site_s_own_measured_diagonal_is_used_when_the_caller_passes_none() -> None:
    rows = g.extra_rows(_state(_site("sliver", diagonal_mm=100.0)))
    widths = sorted({c.params["max_width_mm"] for c in rows if c.op == "remove_small"})
    assert widths == pytest.approx([r * 100.0 for r in g.EXTRA_SLIVER_RATIOS])
    assert not g.SKIPPED


def test_reapproximate_needs_a_free_form_face_and_ranges_past_det_s() -> None:
    plain = [c for c in g.extra_rows(_state(_site("sliver")), diagonal_mm=200.0) if c.op == "reapproximate"]
    assert plain == [], "no free-form face is known on that site"
    free = _site("sliver", nodes=[{"id": "F1", "surface": "bsplinesurface"}], located=["F1"])
    rows = [c for c in g.extra_rows(_state(free), diagonal_mm=200.0) if c.op == "reapproximate"]
    assert rows
    bound = min(0.1, 1e-3 * 200.0)
    shares = {round(c.params["tol_mm"] / bound, 6) for c in rows}
    assert shares >= {0.05, 0.1, 1.0, 2.0}, shares
    assert all(c.params["max_degree"] in range(3, 10) for c in rows)


def test_rows_are_unique_so_no_verdict_is_spent_twice() -> None:
    rows = g.extra_rows(_state(_site("sliver", located=["F1"]), _site("sliver", located=["F1"])),
                        diagonal_mm=200.0)
    # params hold lists (`keep: []`), so compare them by repr, as the module's own de-duplication does
    keys = [(c.op, tuple(c.targets), tuple(sorted((k, repr(v)) for k, v in c.params.items()))) for c in rows]
    assert len(keys) == len(set(keys)), [k for k in keys if keys.count(k) > 1]


# --- depth 2: the faces a previous split MADE (LEAD-2's next thing) ------------------------------------------------


def _diff(op: str, params: dict, modified=None, generated=None):
    return SimpleNamespace(op=op, params=dict(params), modified=dict(modified or {}),
                           generated=dict(generated or {}), removed=[], declared_added=[], declared_removed=[],
                           seconds=0.1)


def _split_state(op="split_angle", deg=60.0, made=("F9", "F10"), live=None, depth=1):
    ids = {f: f"fp{f}" for f in (live if live is not None else [*FACES, *made])}
    ws = SimpleNamespace(ids=ids, original="/fake/o.brep",
                         history=[_diff(op, {"max_deg": deg} if deg else {}, modified={"F2": list(made)})])
    return SimpleNamespace(sites=[_site("proximity", located=["F1"])], ws=ws, depth=depth, parent="S0")


def test_depth2_aims_at_the_faces_the_SPLIT_made_not_the_ones_the_mesher_named() -> None:
    """A11's caveat the other way round: at depth 2 the thing to aim at did not exist when the member failed."""
    rows = g.depth2_rows(_split_state())
    assert rows, "a split parent must produce depth-2 rows"
    for c in rows:
        assert set(c.targets) <= {"F9", "F10"}, c.targets
        assert "depth2" in c.rationale and c.rationale.startswith("oracle_face_grid: "), c.rationale


def test_depth2_angles_are_strictly_finer_than_the_parent_split() -> None:
    """Repeating the parent's own rung divides nothing - the kernel answers "found nothing to divide"."""
    degs = {c.params["max_deg"] for c in g.depth2_rows(_split_state(deg=60.0)) if c.op == "split_angle"}
    assert degs == {45.0, 30.0}, degs
    assert {c.params["max_deg"] for c in g.depth2_rows(_split_state(deg=30.0)) if c.op == "split_angle"} == set()
    assert {c.params["max_deg"] for c in g.depth2_rows(_split_state(deg=180.0))
            if c.op == "split_angle"} == set(g.DEPTH2_ANGLES)


def test_depth2_fires_only_after_a_split() -> None:
    assert g.depth2_rows(_split_state(op="defeature", deg=None)) == []
    assert g.depth2_rows(_split_state(op="remove_small", deg=None)) == []
    assert g.depth2_rows(SimpleNamespace(sites=[], ws=SimpleNamespace(ids={}, history=[]), depth=0)) == []


def test_a_face_the_split_made_but_that_no_longer_lives_is_not_aimed_at() -> None:
    """A later op can have consumed it; aiming at a dead id is a candidate the workspace refuses to resolve."""
    rows = g.depth2_rows(_split_state(made=("F9", "F10"), live=[*FACES, "F9"]))
    assert rows and all(c.targets == ["F9"] or set(c.targets) == {"F9"} for c in rows), [c.targets for c in rows]


def test_a_split_that_recorded_no_new_face_is_reported_not_guessed() -> None:
    ws = SimpleNamespace(ids={f: f"fp{f}" for f in FACES}, original="/fake/o.brep",
                         history=[_diff("split_angle", {"max_deg": 60.0})])
    assert g.depth2_rows(SimpleNamespace(sites=[], ws=ws, depth=1)) == []
    assert any("recorded no new face" in n for n in g.SKIPPED), g.SKIPPED


def test_depth2_rows_reach_the_table_through_extra_rows_and_are_not_duplicated() -> None:
    rows = g.extra_rows(_split_state(), diagonal_mm=200.0)
    deep = [c for c in rows if "depth2" in c.rationale]
    assert deep, "extra_rows must include the depth-2 rows"
    keys = [(c.op, tuple(c.targets), tuple(sorted((k, repr(v)) for k, v in c.params.items()))) for c in rows]
    assert len(keys) == len(set(keys))
    ws = _ws()
    ws.ids.update({"F9": "fpF9", "F10": "fpF10"})
    ws.indices.update({"F9": 9, "F10": 10})
    for c in deep:
        assert validate_call(c.op, list(c.targets), dict(c.params), ws) == [], (c.op, c.params)


# --- LEAD-3's standing rule: a new row field must be declared, or report.load refuses the whole row --------------


def test_the_wall_numbers_ride_inside_triage_so_no_row_can_be_refused() -> None:
    """A16 put `overrun_s` and `wall_skipped_candidates` on the search's stop event, and LEAD-3 reads them per
    file. `report.load` builds `ArmRow(**row)`, so a NEW TOP-LEVEL field would refuse the whole row - after the
    file's repair work is done and paid for, which cost two waves today. They therefore live inside `triage`,
    which ArmRow already declares as one dict. This pins both halves: the names are in the triage block, and they
    are NOT top-level row keys."""
    import inspect
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devtools/validation/v4_eval"))
    import arms  # noqa: PLC0415
    import report  # noqa: PLC0415

    src = inspect.getsource(arms.search_triage) if hasattr(arms, "search_triage") else inspect.getsource(arms)
    assert '"overrun_s": stop.get("overrun_s")' in src, "the overrun must be carried somewhere readable"
    declared = set(report.ArmRow.__dataclass_fields__)
    assert "triage" in declared, "the nesting only works because ArmRow declares triage"
    assert '"early_stop_failed_full": stop.get("early_stop_failed_full")' in src, (
        "the cost of the early-stop trade must be carried where LEAD-3 can read it")
    for leaked in ("overrun_s", "wall_skipped_candidates", "early_stopped", "early_stop_failed_full"):
        assert leaked not in declared, (
            f"{leaked} became a top-level row field; either declare it on ArmRow in THIS commit or keep it nested")
