# Responsibility: Hold the layer judgement open on the path the product actually takes - the
# measured figures must cross the container boundary, reach the manifest, and be said where a
# human and the reviewer both look.
# Boundaries: cfmesh_conjugate/native._as_dict, finalize.quality_of/_near_wall/_said and the
# engine's declared criteria. No meshing and no rendering.
"""THE LAYER GATE RAN AND NOTHING IT MEASURED EVER LEFT THE MACHINE.

`cfmesh_conjugate/layers` carries four floors and the functions that read a layer log against
them, and `conjugate.build` calls all of it the moment the `snappyHexMesh` stage exits - it has
to, because with OpenFOAM's shipped `minVol` a total zero-layer failure exits 0. Every delivered
conjugate mesh comes through `native._run_conjugate_local`, which calls `conjugate.build`, so the
judgement DOES run on the native path. The premise that it does not is wrong about the path and
right about everything that follows from it: `conjugate.build` put the whole reading in
`ConjugateReport.layers`, and `native._as_dict` - the one function that decides what crosses the
container boundary - did not name that field. So the report, the manifest, the report card and the
reviewer all learned nothing, and the only trace was a `logger.warning` on a VM that is now gone.

MEASURED ON THE REAL PLATE, job 0ff6f9e5 (2026-11-16), from its own preserved
`snappyHexMesh.log`, re-read through `layers.read_layer_log`:

    extrusion, iteration 0               97.2267%
    ...ten repair iterations, every one DOWN
    extrusion, iteration 9               88.5826%     against COVERAGE_FLOOR_PCT 92.0
    fluid_to_solid, layers in the mesh   2.25 of 3    LAYERS_FLOOR_FRACTION 0.75 x 3 = 2.25 exactly
    fluid_to_solid, thickness achieved   71.9%        against THICKNESS_FLOOR_PCT 75.0
    faces in error                       8,916 over 37,247,044 cells added = 239 per million
                                                      against ILLEGAL_PER_MILLION_CEILING 200.0

Three of the four floors missed - and worse than the 90.7146% run those thresholds were written to
refuse. `layers.complain` returns "" (something DID grow, so the mesh is not thrown away) and
`layers.shortfalls` returns those three lines. The delivered `conjugate_report.json` has no
`layers` key at all, and the delivered `mesh_manifest.json` published

    near_wall_layers: 3

which is the number the mesher was TOLD to grow. The mesh had 2.25, and 3 is what was quoted to
the customer off that field.

THE LAYERS SHORTFALL IS REPORTED AND NOT GATED, deliberately. `layers.shortfalls` states the
reason in its own docstring: the pass MOVES the only copy of the coolant mesh and rewrites it in
place, so refusing on a coverage miss throws away a finished mesh that nothing else has anything
against, and the mesh is the deliverable. `layers.complain` keeps the refusals that mean NOTHING
GREW and those stop the run inside the mesher. What is not acceptable is a QUIET pass, so the
shortfall is said in three places a reader reaches: the quality block, the declared `layer_short`
criterion row, and the build log line.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_conjugate_row import REPORT, case  # noqa: F401

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import layers as LY
from meshpipeline.engines.cfmesh_conjugate import native as N
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID
from meshpipeline.engines.quality_criteria import evaluate

#: THE REAL PLATE'S OWN LAYER TABLE, transcribed from job 0ff6f9e5's `snappyHexMesh.log`. The
#: whole log is 37,649 bytes of ten repair iterations; these are the lines `read_layer_log`'s own
#: regexes match, in the order it matches them, so the dict this produces is the dict that run
#: produced. The coverage curve is every `Extruding` line of the ten iterations.
REAL_LOG = """
Extruding 16122330 out of 16582198 faces (97.2267%). Removed extrusion at 334828 faces.
Added 49746594 out of 49746594 cells (100%).
Extruding 14961662 out of 16582198 faces (90.2273%). Removed extrusion at 62208 faces.
Added 37924957 out of 49746594 cells (76.2363%).
Extruding 14868608 out of 16582198 faces (89.6661%). Removed extrusion at 49102 faces.
Added 37659301 out of 49746594 cells (75.7023%).
Extruding 14804005 out of 16582198 faces (89.2765%). Removed extrusion at 19120 faces.
Added 37453435 out of 49746594 cells (75.2884%).
Extruding 14776991 out of 16582198 faces (89.1136%). Removed extrusion at 13974 faces.
Added 37395891 out of 49746594 cells (75.1728%).
Extruding 14756134 out of 16582198 faces (88.9878%). Removed extrusion at 12385 faces.
Added 37359066 out of 49746594 cells (75.0987%).
Extruding 14737324 out of 16582198 faces (88.8744%). Removed extrusion at 11582 faces.
Added 37327294 out of 49746594 cells (75.0349%).
Extruding 14718420 out of 16582198 faces (88.7604%). Removed extrusion at 11305 faces.
Added 37298157 out of 49746594 cells (74.9763%).
Extruding 14701931 out of 16582198 faces (88.6609%). Removed extrusion at 8794 faces.
Added 37268354 out of 49746594 cells (74.9164%).
Extruding 14688940 out of 16582198 faces (88.5826%). Removed extrusion at 8715 faces.
Added 37247044 out of 49746594 cells (74.8736%).

patch          faces        layers        overall thickness
                        target   mesh     [m]       [%]
-----          -----    -----    ----     ---       ---
fluid_to_solid 16582198 3        2.25     1.09e-05  71.9

Layers added in = 2537.74 s.
Finished meshing with 8916 illegal faces (concave, zero area or negative cell pyramid volume)
"""

#: A pass that met every floor, so the shortfall row can be shown to come back EMPTY rather than
#: absent. These are the hand-built run's own figures (layers.py's docstring): 95.553% coverage,
#: 2.84 of 3 layers at 94.6% thickness, 2,888 faces in error over the cells it added.
GOOD_LOG = """
Extruding 16619777 out of 16619777 faces (95.553%). Removed extrusion at 0 faces.
Added 47232886 out of 47232886 cells (100%).

patch          faces        layers        overall thickness
                        target   mesh     [m]       [%]
-----          -----    -----    ----     ---       ---
fluid_to_solid 16619777 3        2.84     1.43e-05  94.6

Layers added in = 2352.61 s.
Finished meshing with 2888 illegal faces (concave, zero area or negative cell pyramid volume)
"""


def _judged(log: str, *, asked: int = 3) -> dict:
    """The layers block exactly as `conjugate.build` builds it: the log read, then the shortfalls
    it computed against the pass's OWN cells_added written back onto the same dict."""
    raw = LY.read_layer_log(log)
    cells = int(raw.get("cells_added") or 0)
    assert not LY.complain(raw, interface_patch=FLUID_TO_SOLID, n_layers=asked, cells=cells), (
        "this fixture is a pass that GREW something; complain is for the ones that grew nothing")
    raw["shortfalls"] = list(LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID,
                                           n_layers=asked, cells=cells))
    return raw


def _report(log: str | None, *, asked: int = 3) -> dict:
    """The run's report with the plan asking for `asked` layers, and the pass's own reading on it."""
    r = json.loads(json.dumps(REPORT))
    r["plan"]["layers"] = asked
    if log is not None:
        r["layers"] = _judged(log, asked=asked)
    return r


# #
# the reading itself, on the real plate's own log
# #
def test_the_real_plates_own_log_misses_three_of_this_engines_four_floors():
    """The measurement the rest of this file is about, taken from the shipped parser."""
    raw = _judged(REAL_LOG)
    assert raw["coverage_pct"] == pytest.approx(88.5826)
    assert raw["coverage_by_iteration"][0] == pytest.approx(97.2267), "it started HIGHER"
    assert raw["coverage_by_iteration"] == sorted(raw["coverage_by_iteration"], reverse=True), (
        "every one of the ten repair iterations took coverage DOWN")
    row = raw["patches"][FLUID_TO_SOLID]
    assert row["mesh_layers"] == pytest.approx(2.25)
    assert row["target_layers"] == 3
    assert row["thickness_pct"] == pytest.approx(71.9)
    assert raw["illegal_faces"] == 8916
    assert raw["cells_added"] == 37247044
    assert len(raw["shortfalls"]) == 3, raw["shortfalls"]
    # ...and the one floor it did NOT miss, because 2.25 is 0.75 x 3 exactly and the rule is `<`
    assert not any("layers of the 3 asked for" in s for s in raw["shortfalls"]), (
        "2.25 is LAYERS_FLOOR_FRACTION x 3 to the digit, so that floor is met at the boundary")


# #
# the container boundary
# #
def test_the_measured_layer_reading_crosses_the_container_boundary(case):  # noqa: F811
    """`native._as_dict` is the only thing that decides what finalize can read on the far side.
    `conjugate.build` already put the reading on the report; this is whether it travels."""
    from meshpipeline.engines.cfmesh_conjugate import plan as PL
    from meshpipeline.engines.cfmesh_conjugate.conjugate import (
        FLUID_REGION,
        SOLID_REGION,
        ConjugateReport,
        RegionReport,
    )

    # THE REPORT'S OWN DATACLASSES, so this test cannot pass on a shape _as_dict does not read.
    rep = ConjugateReport(
        fluid=RegionReport(name=FLUID_REGION, cells=166400, volume_m3=2.44473e-9, mesh_ok=True),
        solid=RegionReport(name=SOLID_REGION, cells=495376, volume_m3=6.00527e-9, mesh_ok=True),
        interface_faces_fluid=93600, interface_faces_solid=93600,
        layers=_judged(REAL_LOG))
    plan = PL.ConjugatePlan(background_cell_m=2e-4, levels=3, expect_fluid_volume_m3=2.44e-9,
                            expect_fluid_pieces=1, volume_tol=0.1989, narrowest_m=1e-4,
                            layers=3)
    body = N._as_dict(rep, plan)
    assert body.get("layers"), (
        "the report that crosses the container boundary carries no reading of the layer pass, so "
        "the judgement conjugate.build already made reaches no artifact, no manifest and no "
        "reviewer")
    assert body["layers"]["coverage_pct"] == pytest.approx(88.5826)
    assert body["layers"]["shortfalls"], "the shortfalls travel with the figures or not at all"
    assert body["plan"]["layers"] == 3, "the ASK is still published, beside the reading"


# #
# the manifest's quality block
# #
def test_the_layer_count_published_is_the_one_the_mesh_has(case):  # noqa: F811
    """THE FIELD THE USER QUOTED. It published the plan's ask - 3 on job 0ff6f9e5 - while the mesh
    carried 2.25."""
    q = F.quality_of(case, _report(REAL_LOG))
    assert q["near_wall_layers"] == pytest.approx(2.25), (
        "near_wall_layers is the number the mesher was TOLD to grow, not the number it grew")
    assert q["near_wall_layers_asked"] == 3, "and the ask is beside it, never instead of it"
    assert q["near_wall_layers_measured"] is True


def test_a_run_that_grew_no_layers_still_publishes_the_ask(case):  # noqa: F811
    """Unchanged for every mesh this engine has delivered: `layers: 0` and no pass. A reading
    nobody took must not read as a measurement of nothing."""
    q = F.quality_of(case, _report(None, asked=0))
    assert q["near_wall_layers"] == 0
    assert q.get("near_wall_layers_asked") == 0, (
        "the ask is not published under its own name, so a reader cannot tell a layer count that "
        "was asked for from one that was measured")
    assert q.get("near_wall_layers_measured") is False, (
        "nothing says whether near_wall_layers is a measurement or an intention")
    assert "layers_in_mesh" not in q, "no pass ran, so there is no measured count to publish"
    assert "layer_short" not in q, (
        "an absent shortfall list means nobody judged a layer pass; an empty one would claim one "
        "was judged and met every floor")


def test_the_measured_layer_figures_reach_the_manifest(case):  # noqa: F811
    """Every figure `layers.measured` names, under the names it names them, plus the curve."""
    q = F.quality_of(case, _report(REAL_LOG))
    want = {"layer_coverage_pct": 88.5826, "layer_thickness_pct": 71.9, "layers_in_mesh": 2.25,
            "layers_asked": 3, "layer_faces_in_error": 8916, "layer_cells_added": 37247044,
            "layer_iterations": 10, "layer_patch": FLUID_TO_SOLID}
    got = {k: q.get(k) for k in want}
    assert got == pytest.approx(want), (
        "the manifest's quality block does not carry what the layer pass measured, so no reader "
        f"downstream of it can see what grew: {got!r}")
    assert (q.get("layer_coverage_by_iteration") or [0.0])[0] == pytest.approx(97.2267), (
        "the coverage curve is not published, and the final figure alone says a floor was missed "
        "where the curve says the REPAIR is what missed it")


def test_the_shortfall_against_this_engines_own_floors_is_published(case):  # noqa: F811
    q = F.quality_of(case, _report(REAL_LOG))
    short = q.get("layer_short")
    assert short and len(short) == 3, f"three floors were missed on the real plate: {short!r}"
    joined = " ".join(short)
    assert "of the interface against" not in joined, joined
    assert "71.9%" in joined and f"{LY.THICKNESS_FLOOR_PCT:g}%" in joined
    assert "239 per million" in joined and f"{LY.ILLEGAL_PER_MILLION_CEILING:g}" in joined


def test_a_pass_that_met_every_floor_publishes_an_empty_shortfall(case):  # noqa: F811
    """EMPTY IS NOT ABSENT. The criterion is op="empty", so it has to be able to tell "the pass met
    every floor" from "nobody judged it"."""
    q = F.quality_of(case, _report(GOOD_LOG))
    assert q.get("layer_short") == [], (
        "a pass that met every floor publishes no shortfall list at all, so the criterion that "
        f"reads it cannot tell that from an unjudged pass: {q.get('layer_short')!r}")
    assert q.get("layers_in_mesh") == pytest.approx(2.84)
    assert q["near_wall_layers"] == pytest.approx(2.84)


# #
# where a human and the reviewer see it
# #
def test_the_shortfall_is_a_declared_criterion_row(case):  # noqa: F811
    """The report card and the reviewer's rubric are built from these rows. Without one, a mesh
    that missed three of this engine's own floors passes every declared bar in silence."""
    q = F.quality_of(case, _report(REAL_LOG))
    rows = {r["key"]: r for r in evaluate("cfmesh_conjugate", q)}
    assert "layer_short" in rows, (
        "no declared criterion reads the layer shortfall, so a pass under this engine's own "
        "coverage and thickness floors is shipped with nothing said about it")
    row = rows["layer_short"]
    assert row["passed"] is False, row
    assert row["gating"] is False, (
        "gating would throw away a finished mesh over a coverage miss; layers.shortfalls says why")
    assert len(row["measured"]) == 3


def test_an_unjudged_layer_pass_never_reads_as_a_pass(case):  # noqa: F811
    q = F.quality_of(case, _report(None, asked=0))
    rows = {r["key"]: r for r in evaluate("cfmesh_conjugate", q)}
    row = rows.get("layer_short")
    assert row is not None and row["passed"] is None, (
        "absent must be unmeasured, not a pass: a run whose layer log could not be read would "
        f"otherwise be reported as having met every floor. row={row!r}")


def test_the_build_log_line_says_what_grew_and_where_it_fell_short(case):  # noqa: F811
    """The line a reader of the build log gets. It said nothing about layers at all."""
    said = F._said(F.quality_of(case, _report(REAL_LOG)))
    assert "layers=2.25 of 3 asked" in said, said
    assert "88.5826%" in said, said
    assert "LAYER SHORTFALL" in said, (
        "the build log line carries no shortfall, so the only human-readable trace of a mesh "
        "under this engine's own floors is absent")
    assert said.count("LAYER SHORTFALL") == 3, said


def test_the_build_log_line_is_unchanged_for_a_mesh_with_no_layers(case):  # noqa: F811
    said = F._said(F.quality_of(case, _report(None, asked=0)))
    assert "layers=" not in said and "LAYER SHORTFALL" not in said, said


# #
# the floors are stated once
# #
def test_the_floors_are_the_layers_modules_own(case):  # noqa: F811
    """One spelling. This engine has already paid for one quantity with two readers that
    disagreed - 5% in a criterion against 19.89% in a gate - so the criterion carries no number of
    its own and the shortfall text is `layers.shortfalls`' own."""
    from meshpipeline.engines.cfmesh_conjugate.criteria import CRITERIA_ROWS

    rationale = next((c.rationale for c in CRITERIA_ROWS if c.key == "layer_short"), "")
    assert rationale, "no layer_short criterion is declared, so no row names the floors at all"
    for bar in (f"{LY.KEPT_OF_OFFERED_FLOOR_PCT:g}", f"{LY.THICKNESS_FLOOR_PCT:g}",
                f"{LY.ILLEGAL_PER_MILLION_CEILING:g}"):
        assert bar in rationale, (bar, rationale)
    src = Path(F.__file__).read_text()
    assert "KEPT_OF_OFFERED_FLOOR_PCT =" not in src and "THICKNESS_FLOOR_PCT =" not in src, (
        "finalize has restated a floor that lives in layers.py")
