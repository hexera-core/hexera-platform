# Responsibility: Verify the resolution floor sets aside narrow passages the flow can go round, and still fails the passages it must go through.
# Boundaries: engines/passage_flow on castellated boxes built cell by cell (the boundary a snappy fill has), and the gates, messages and note that read its verdict.
"""Job 02ed0d14 (shell-and-tube exchanger, shell side, 2026-10-01): a sound 4.2 M-cell mesh was
refused twice because its narrowest wall - the 20.4 mm gaps between neighbouring tubes, 6% of the
wall - held about 8 to 10 cells across against a floor of 12, while the flow's main way round the
bundle held more. The flow can go round those gaps, so they are a side passage: the mesh is
delivered with a note that says so with the figures. A throat the flow must go through, or a main
passage meshed too coarsely, still fails exactly as before.

The fixtures are fluid regions built from cubic cells, so the boundary is what a castellated fill
gives a snappy mesh: square faces of one size, wound outward, ports at the two ends."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines import passage_flow as PF
from meshpipeline.engines.gates import GateCtx

H = 0.003                                    # the cell edge of every fixture: 3 mm


def _boundary(mask: np.ndarray, h: float = H):
    """(points, quads, kind) of the boundary of a fluid made of cubic cells: quads wound out of the
    fluid; kind 0 = the port at x-min, 1 = the port at x-max, -1 = wall."""
    nx, ny, nz = mask.shape
    pad = np.pad(mask, 1)
    node = {}
    pts: list[tuple[float, float, float]] = []

    def p(i, j, k):
        key = (i, j, k)
        if key not in node:
            node[key] = len(pts)
            pts.append((i * h, j * h, k * h))
        return node[key]

    quads, kind = [], []
    for axis in range(3):
        for sign in (-1, 1):
            nb = np.roll(pad, -sign, axis=axis)[1:-1, 1:-1, 1:-1]
            for i, j, k in np.argwhere(mask & ~nb):
                if axis == 0:
                    x = i + (1 if sign > 0 else 0)
                    q = [p(x, j, k), p(x, j + 1, k), p(x, j + 1, k + 1), p(x, j, k + 1)]
                    kd = 1 if (sign > 0 and i == nx - 1) else 0 if (sign < 0 and i == 0) else -1
                elif axis == 1:
                    y = j + (1 if sign > 0 else 0)
                    q = [p(i, y, k), p(i, y, k + 1), p(i + 1, y, k + 1), p(i + 1, y, k)]
                    kd = -1
                else:
                    z = k + (1 if sign > 0 else 0)
                    q = [p(i, j, z), p(i + 1, j, z), p(i + 1, j + 1, z), p(i, j + 1, z)]
                    kd = -1
                quads.append(q if sign > 0 else q[::-1])
                kind.append(kd)
    return np.asarray(pts, float), np.asarray(quads, np.int64), np.asarray(kind, np.int64)


def _triangles(quads, kind):
    tris = np.concatenate([quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]])
    return tris, np.concatenate([kind, kind])


def _duct(n_long=50, n_side=26) -> np.ndarray:
    return np.ones((n_long, n_side, n_side), dtype=bool)


def _duct_with_side_gap() -> np.ndarray:
    """A 78 x 78 mm duct, 150 mm long, with a solid block in its middle third that leaves a 6 mm
    gap (two cells) under it and a 54 mm way over it: the gap is a side passage."""
    m = _duct()
    m[17:33, 2:8, :] = False
    return m


def _duct_with_throat() -> np.ndarray:
    """The same duct pinched to a 12 mm slot (four cells) over its middle third: every bit of the
    flow goes through the slot - it is the main way, not a side passage."""
    m = _duct()
    m[17:33, :8, :] = False
    m[17:33, 12:, :] = False
    return m


def _evidence(mask, monkeypatch, **kw):
    monkeypatch.setattr(PF, "MAX_VOXELS", 1_500_000)
    pts, quads, kind = _boundary(mask)
    tris, port = _triangles(quads, kind)
    return PF.flow_evidence(pts, tris, port, sample_points=4000, **kw)


def _quality(p05: float, flow: dict | None = None) -> dict:
    q = {"passage_cells_across_local": {"median": 14.0, "p05": p05, "min": p05}}
    if flow is not None:
        q["passage_flow"] = flow
    return q


# #
# the reading
# #

def test_the_main_way_is_a_lower_bound_within_a_few_voxels(monkeypatch):
    pts, quads, kind = _boundary(_duct())
    tris, port = _triangles(quads, kind)
    way = PF.main_way_radius(pts, tris, port, narrow_width=0.03, max_voxels=400_000)
    radius, h = way.radius, way.voxel
    assert 0.039 - (PF.VOXEL_SLACK + 1.5) * h <= radius <= 0.039, (radius, h)
    assert way.read - radius == pytest.approx(PF.VOXEL_SLACK * h)


def test_the_main_way_needs_two_ports():
    pts, quads, kind = _boundary(_duct())
    tris, port = _triangles(quads, np.where(kind == 1, -1, kind))
    assert PF.main_way_radius(pts, tris, port, narrow_width=0.03, max_voxels=200_000) is None


def test_a_gap_beside_a_wider_way_is_a_side_passage(monkeypatch):
    ev = _evidence(_duct_with_side_gap(), monkeypatch)
    side, main = ev["side"], ev["main"]
    assert side["under_floor_share"] > 0.0
    # the gap is 6 mm across and its walls are 3 mm cells: two cells across it, read against the
    # mean edge at the point with the faces' diagonals in it - engines/passage.measure_passage's
    # convention, so a little under two
    assert side["width_m"] == pytest.approx(0.006, abs=1e-6)
    assert 1.5 <= side["cells_across"]["p05"] <= 2.0
    # the way over the block is 54 mm: the main way reads that, low by a voxel or so
    assert 0.048 <= ev["main_way"]["width_m"] <= 0.054 + 1e-9
    assert main["p05"] >= 12.0


def test_a_throat_the_flow_must_go_through_is_no_side_passage(monkeypatch):
    ev = _evidence(_duct_with_throat(), monkeypatch)
    # the slot IS the main way: nothing narrower than it to set aside (and when the copy cannot
    # read so narrow a way at all, nothing is set aside either)
    assert not (ev.get("side") or {}).get("under_floor_share")
    assert ev["main_way"] is None or ev["main_way"]["width_m"] <= 0.012


# #
# the verdict
# #

def test_side_passages_under_the_floor_pass_with_the_main_way_judged(monkeypatch):
    ev = _evidence(_duct_with_side_gap(), monkeypatch)
    v = PF.judge_resolution(_quality(2.0, ev))
    assert v.ok and v.scope == "main"
    assert v.cells_across == ev["main"]["p05"] and v.side["width_m"] == ev["side"]["width_m"]


def test_a_throat_still_fails_on_the_figure_measured_beside_the_mesh(monkeypatch):
    ev = _evidence(_duct_with_throat(), monkeypatch)
    v = PF.judge_resolution(_quality(4.0, ev))
    assert not v.ok and v.scope == "narrowest" and v.cells_across == 4.0


def test_a_short_throat_beside_a_side_gap_still_fails_at_the_neck(monkeypatch):
    # the side gap of the fixture above, and downstream a 9 mm long slot only 12 mm across that
    # every bit of the flow goes through: too short to reach the main way's 5th percentile, so
    # the neck - the main way's under-resolved walls whose passages part the ports when shut -
    # is judged on its own
    m = _duct_with_side_gap()
    m[40:43, :, :] = False
    m[40:43, 11:15, :] = True
    ev = _evidence(m, monkeypatch)
    assert ev["side"]["under_floor_share"] > 0.0
    assert ev["neck"]["points"] >= PF.NECK_MIN_POINTS and ev["neck"]["p05"] < 12.0
    v = PF.judge_resolution(_quality(2.0, ev))
    assert not v.ok and v.scope == "main" and v.cells_across == ev["neck"]["p05"]


def test_only_a_passage_the_flow_cannot_go_round_parts_the_ports():
    m = _duct_with_side_gap()
    m[40:43, :, :] = False
    m[40:43, 11:15, :] = True
    pts, quads, kind = _boundary(m)
    tris, port = _triangles(quads, kind)
    copy = PF.fluid_copy(pts, tris, port, narrow_width=0.012, max_voxels=1_500_000)
    # the 12 mm slot (x 120..129 mm, y 33..45 mm), shut by balls along its middle: the ports part
    slot = np.asarray([[0.1245, 0.039, z] for z in np.arange(0.003, 0.078, 0.004)])
    # the 6 mm gap under the block (x 51..99 mm, y 0..6 mm), shut the same way: they stay joined
    gap = np.asarray([[x, 0.003, z] for x in np.arange(0.054, 0.099, 0.004)
                      for z in np.arange(0.003, 0.078, 0.004)])
    assert copy.must_cross(slot, np.full(len(slot), 0.006)).all()
    assert not copy.must_cross(gap, np.full(len(gap), 0.003)).any()


def test_a_coarse_main_way_still_fails_with_its_own_figure(monkeypatch):
    # 6 mm cells: the 60 mm duct has ten cells across it, under the floor, beside a 6 mm gap
    m = np.ones((26, 10, 10), dtype=bool)
    m[9:17, 1:4, :] = False
    monkeypatch.setattr(PF, "MAX_VOXELS", 1_500_000)
    pts, quads, kind = _boundary(m, h=0.006)
    tris, port = _triangles(quads, kind)
    ev = PF.flow_evidence(pts, tris, port, sample_points=4000)
    assert ev["side"]["under_floor_share"] > 0.0
    v = PF.judge_resolution(_quality(1.0, ev))
    assert not v.ok and v.scope == "main"
    assert v.cells_across == ev["main"]["p05"] < 12.0


def test_the_verdict_is_unchanged_without_the_evidence():
    assert PF.judge_resolution({}).scope == "unmeasured" and PF.judge_resolution({}).ok
    assert PF.judge_resolution(_quality(13.0)).ok
    v = PF.judge_resolution(_quality(9.8))
    assert not v.ok and v.scope == "narrowest" and v.cells_across == 9.8
    # evidence that could not find the main way, or no side passage under the floor, changes nothing
    assert not PF.judge_resolution(_quality(9.8, {"main_way": None, "side": {}})).ok
    no_side = {"main_way": {"width_m": 0.03}, "main": {"p05": 20.0},
               "side": {"under_floor_share": 0.0}}
    assert not PF.judge_resolution(_quality(9.8, no_side)).ok


def test_side_passages_lining_much_of_the_wall_do_not_excuse_it():
    ev = {"main_way": {"width_m": 0.03}, "main": {"p05": 20.0, "median": 30.0},
          "side": {"under_floor_share": PF.SIDE_SHARE_MAX + 0.05, "width_m": 0.004}}
    v = PF.judge_resolution(_quality(3.0, ev))
    assert not v.ok and v.scope == "narrowest"


# #
# the polyMesh it reads, and when
# #

def _foam(path: Path, cls: str, obj: str, records: list[str]) -> None:
    path.write_text(
        "FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
        f"    class       {cls};\n    object      {obj};\n}}\n"
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
        f"{len(records)}\n(\n" + "\n".join(records) + "\n)\n")


def _write_boundary_polymesh(pm: Path, mask: np.ndarray) -> None:
    """The boundary of a cell-built fluid as a polyMesh (no internal faces): inlet, outlet, wall."""
    pts, quads, kind = _boundary(mask)
    pm.mkdir(parents=True, exist_ok=True)
    order = [np.flatnonzero(kind == 0), np.flatnonzero(kind == 1), np.flatnonzero(kind < 0)]
    faces = [quads[i] for sel in order for i in sel]
    _foam(pm / "points", "vectorField", "points", [f"({x} {y} {z})" for x, y, z in pts])
    _foam(pm / "faces", "faceList", "faces",
          [f"4({' '.join(str(int(v)) for v in f)})" for f in faces])
    _foam(pm / "owner", "labelList", "owner", ["0"] * len(faces))
    start, blocks = 0, []
    for name, typ, sel in zip(("inlet", "outlet", "wall"), ("patch", "patch", "wall"), order):
        blocks.append(f"    {name}\n    {{\n        type {typ};\n        nFaces {len(sel)};\n"
                      f"        startFace {start};\n    }}")
        start += len(sel)
    (pm / "boundary").write_text(
        "FoamFile\n{\n    version 2.0;\n    format ascii;\n    class polyBoundaryMesh;\n"
        "    object boundary;\n}\n// * * //\n\n3\n(\n" + "\n".join(blocks) + "\n)\n")


def test_the_reading_comes_from_the_polymesh_and_only_when_the_floor_failed(tmp_path,
                                                                            monkeypatch):
    monkeypatch.setattr(PF, "MAX_VOXELS", 1_500_000)
    monkeypatch.setattr(PF, "SAMPLE_POINTS", 4000)
    _write_boundary_polymesh(tmp_path / "constant" / "polyMesh", _duct_with_side_gap())
    assert PF.passage_flow_of_polymesh(tmp_path, _quality(13.0)) == {}, "a pass is not re-read"
    assert PF.passage_flow_of_polymesh(tmp_path, {}) == {}, "no measure, nothing to re-read"
    out = PF.passage_flow_of_polymesh(tmp_path, _quality(2.0))
    ev = out["passage_flow"]
    assert ev["side"]["width_m"] == pytest.approx(0.006, abs=1e-6)
    assert PF.judge_resolution({**_quality(2.0), **out}).ok
    # a polyMesh that is not there, or a budget that runs out, is no evidence - never an error
    assert PF.passage_flow_of_polymesh(tmp_path / "nowhere", _quality(2.0)) == {}
    assert PF.passage_flow_of_polymesh(tmp_path, _quality(2.0), budget_s=1e-9) == {}


# #
# the gates, the message and the note
# #

def _ctx(manifest: dict) -> GateCtx:
    c = GateCtx(workspace=Path("."), engine="snappy", domain="", intake_patches=[],
                engine_params={})
    c.manifest_or_load = lambda: manifest  # type: ignore[method-assign]
    return c


SIDE_EVIDENCE = {"version": 1, "main_way": {"width_m": 0.0324, "voxel_m": 0.0023},
                 "main": {"p05": 13.5, "median": 24.1, "min": 8.5, "points": 9970},
                 "side": {"share": 0.0645, "under_floor_share": 0.0645, "points": 689,
                          "under_floor_points": 689,
                          "cells_across": {"p05": 7.1, "median": 8.0, "min": 6.7},
                          "width_m": 0.021016, "narrowest_width_m": 0.020477}}


def test_both_flow_gates_pass_a_mesh_whose_only_shortfall_is_a_side_passage():
    from meshpipeline.engines.cfmesh import flow_gates as CG
    from meshpipeline.engines.snappy import flow_gates as SG
    m = {"quality": _quality(7.1, SIDE_EVIDENCE), "cell_count": 4_178_590}
    assert SG._gate_resolution_floor(_ctx(m)) == (True, "")
    assert CG._gate_resolution_floor(_ctx(m)) == (True, "")


def test_the_snappy_gate_names_the_lever_that_moves_it_and_what_a_rebuild_costs():
    from meshpipeline.engines.gates import cause_of, facts_of
    from meshpipeline.engines.snappy import flow_gates as SG
    ok, fb = SG._gate_resolution_floor(_ctx({"quality": _quality(8.3), "cell_count": 4_178_590}))
    assert not ok and cause_of(fb) == "under_resolved"
    assert "cells_across_diameter" in fb and "surface_level alone does not change it" in fb
    assert "8.3 cells across the passage at the narrowest wall" in fb
    f = facts_of(fb)
    assert f["cells_across"] == 8.3 and f["needed"] == 12 and f["scope"] == "narrowest"
    assert f["rebuild_cells"] == int(4_178_590 * (12 / 8.3) ** 2) and f["cell_limit"] > 0


def test_a_main_way_failure_says_so_with_its_own_figures():
    from meshpipeline.contracts.failure_cause import describe
    from meshpipeline.engines.gates import facts_of
    from meshpipeline.engines.snappy import flow_gates as SG
    ev = {**SIDE_EVIDENCE, "main": {**SIDE_EVIDENCE["main"], "p05": 9.4}}
    ok, fb = SG._gate_resolution_floor(_ctx({"quality": _quality(7.1, ev)}))
    assert not ok and "passages the flow must go through" in fb and "9.4" in fb
    assert "21 mm" in fb and "6.5% of the wall" in fb
    what, _ = describe("under_resolved", facts_of(fb))
    assert what == ("The mesh is too coarse in the passages the flow must go through: about "
                    "9.4 cells across them at the narrowest, and they need at least 12.")


def test_no_rebuild_is_started_when_even_the_cheapest_one_is_over_the_limit():
    from meshpipeline.contracts.failure_cause import describe, retry_can_help
    over = {"cells_across": 6.0, "needed": 12, "cells": 4_000_000, "cell_limit": 8_000_000,
            "rebuild_cells": 16_000_000}
    assert not retry_can_help("under_resolved", over)
    assert retry_can_help("under_resolved", {**over, "rebuild_cells": 7_000_000})
    assert retry_can_help("under_resolved", {"cells_across": 6.0, "needed": 12}), \
        "without the figures the retry stays"
    what, nxt = describe("under_resolved", over)
    assert "at least about 16 million cells, more than the 8 million one job may build" in what
    assert "another mesher" in nxt and "run it again" not in nxt


def test_the_delivered_note_states_the_gaps_and_the_way_round_them():
    from meshpipeline.application.final_result import (
        NARROW_PASSAGES,
        RunOutcome,
        _narrow_passage_lines,
        narrow_passage_caveat,
    )
    outcome = RunOutcome(executor_success=True, quality=_quality(7.1, SIDE_EVIDENCE))
    c = narrow_passage_caveat(outcome)
    assert c is not None and c["kind"] == NARROW_PASSAGES
    assert c["cells_across"] == 7.1 and c["main_cells_across"] == 13.5
    text = "\n".join(_narrow_passage_lines(c))
    assert "about 21 mm across" in text and "about 6.5% of the wall" in text
    assert "about 7.1 cells across them. Our floor is 12" in text
    assert "at least 32 mm across, with about 13.5 or more cells across it" in text
    # no note on a mesh that failed a gate, held the floor anyway, or has no evidence
    assert narrow_passage_caveat(RunOutcome(executor_success=True, failed_gate="x",
                                            quality=_quality(7.1, SIDE_EVIDENCE))) is None
    assert narrow_passage_caveat(RunOutcome(executor_success=True,
                                            quality=_quality(13.0, SIDE_EVIDENCE))) is None
    assert narrow_passage_caveat(RunOutcome(executor_success=True,
                                            quality=_quality(7.1))) is None


def test_a_delivered_mesh_with_the_note_reads_it_first():
    from meshpipeline.application.final_result import (
        RunOutcome,
        TerminalStatus,
        build_final_result,
        narrow_passage_caveat,
        render_message,
    )
    note = narrow_passage_caveat(RunOutcome(executor_success=True,
                                            quality=_quality(7.1, SIDE_EVIDENCE)))
    fr = build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.succeeded, engine="snappy",
        purpose="internal_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=3, required_ready=True, delivered_types=["mesh"],
        optional_warnings=[], requirement_caveats=[note])
    msg = render_message(fr)
    assert msg.startswith("Delivered with a note on its narrowest gaps:")
    assert "near-wall layer coverage" not in msg, "the layer closing is not this note's"
    assert "Mesh generation completed successfully." in msg
