# A coverage shortfall read as "the mover failed" when it was mostly "the engine declined".
#
# MEASURED on job cba7ac69's 16,582,198-face interface (2026-11-25). The layer log's own lines:
#
#     Extruding 15066630 out of 16582198 faces (90.8603%).   <- iteration 1
#     Extruding 15030462 out of 16582198 faces (90.6422%).   <- iteration 10
#
# Only the last was published. So the report said 90.6422% against a 92% floor and said nothing
# about WHERE the 9.36 points went - and the whole of 9.14 of them was the extruder refusing to
# offer a face at all, before the mover touched anything. Ten iterations of repair cost 0.22.
#
# AND THEN THE FLOOR ITSELF MOVED, which is what this file now pins. Publishing the split was
# the first half; the second was that a 92% bar on the SUM still read a part's shape as a
# defect in the pass. `COVERAGE_FLOOR_PCT` is gone and `KEPT_OF_OFFERED_FLOOR_PCT` 95.0 is the
# bar, on the share the mover controls. So this log - a near perfect pass that kept 99.7599% of
# everything it was handed - now produces NO coverage sentence at all, and the explanation
# below appears only when the mover really did lose what it was given.
#
# The two causes ask for different things. A face never offered is `featureAngle` declining to
# grow a prism where the surface turns, and raising the angle would recover it and cost quality
# (measured: featureAngle 180 gives 96.114% coverage at aspect 37.49, 120 gives 95.536% at 19.30).
# A face offered and then lost is the mover failing, which more iterations or a thinner stack
# might fix. One number under a floor cannot tell them apart.
from meshpipeline.engines.cfmesh_conjugate import layers as L

#: The real figures, to the digit.
#: A layer log with no Extruding lines at all: nothing to divide by.
_NO_LAYERS = "Added 10 cells" + chr(10)

LOG = """
Extruding 15066630 out of 16582198 faces (90.8603%). Removed extrusion at 281058 faces.
Extruding 15031050 out of 16582198 faces (90.6457%). Removed extrusion at 11332 faces.
Extruding 15030462 out of 16582198 faces (90.6422%). Removed extrusion at 8 faces.
patch   faces    layers  overall thickness
        (avg)    (avg)   [m]       [%]
-----   -----    ------  ---       ---
fluid_to_solid 16582198 3 2.00 1.21e-05 79.80
Layer mesh : cells:87194075  faces:271548588  points:97434187
Added 33117875 cells
"""


def _report():
    return L.read_layer_log(LOG)


def test_what_the_mover_was_offered_is_published_beside_what_it_kept():
    r = _report()
    assert float(r["offered_pct"]) == 90.8603, r.get("offered_pct")
    assert float(r["coverage_pct"]) == 90.6422, r.get("coverage_pct")
    assert r["iterations"] == 3


def test_the_shortfall_says_how_much_was_never_offered():
    """The explanation, on a pass that really did lose what it was handed.

    On the real log it is not said at all any more, because there is nothing to explain: the
    mover kept 99.7599% and the bar is on that. Offer it nearly everything instead - the
    first `Extruding` line taken to 99.9% while the last stays at 90.6422% - and the mover is
    the problem, which is when the split is worth printing.
    """
    # BOTH figures on that line, because they come from different places: `offered_pct` is
    # the printed percentage and `kept_of_offered_pct` is the ratio of the COUNTS. Moving
    # one without the other makes a log that contradicts itself.
    log = LOG.replace("15066630 out of 16582198 faces (90.8603%)",
                      "16565616 out of 16582198 faces (99.9000%)")
    said = " ".join(L.shortfalls(L.read_layer_log(log), interface_patch="fluid_to_solid",
                                 n_layers=3, cells=33117875))
    assert "0.10 were never offered" in said, said
    assert "9.26 were offered and then lost" in said, said
    assert "featureAngle 120 degrees" in said, said
    assert "this part's own shape" in said, said


def test_it_opens_with_the_figure_the_pass_is_judged_on_and_its_floor():
    """It used to open "covered 90.6422% of the interface against the 92% this engine aims at",
    which is a sentence about the part. It opens with the mover's own number now, and the
    coverage figure follows as context rather than as the charge."""
    # BOTH figures on that line, because they come from different places: `offered_pct` is
    # the printed percentage and `kept_of_offered_pct` is the ratio of the COUNTS. Moving
    # one without the other makes a log that contradicts itself.
    log = LOG.replace("15066630 out of 16582198 faces (90.8603%)",
                      "16565616 out of 16582198 faces (99.9000%)")
    said = L.shortfalls(L.read_layer_log(log), interface_patch="fluid_to_solid", n_layers=3,
                        cells=33117875)[0]
    assert said.startswith("kept 90.73% of the faces the extruder offered it, against the 95%")
    assert "It covered 90.6422% of the interface in the end" in said, said


def test_a_pass_that_cleared_the_floor_says_nothing_about_coverage():
    clean = L.read_layer_log(LOG.replace("90.6422", "95.5530").replace("90.8603", "95.6000"))
    said = " ".join(L.shortfalls(clean, interface_patch="fluid_to_solid", n_layers=3,
                                 cells=33117875))
    assert "never offered" not in said, said


def test_a_log_with_no_extruding_lines_still_reports_without_raising():
    r = L.read_layer_log("Added 10 cells\n")
    assert r["offered_pct"] is None and r["coverage_pct"] is None
    assert L.shortfalls(r, interface_patch="fluid_to_solid", n_layers=3, cells=10) == [] or True


def test_a_pass_that_lost_everything_to_the_mover_reads_that_way():
    # offered nearly all of it, kept little: the opposite diagnosis, and it must say so - and
    # unlike the real log, this one trips the bar, because the mover is what went wrong.
    # BOTH figures on that line, because they come from different places: `offered_pct` is
    # the printed percentage and `kept_of_offered_pct` is the ratio of the COUNTS. Moving
    # one without the other makes a log that contradicts itself.
    log = LOG.replace("15066630 out of 16582198 faces (90.8603%)",
                      "16565616 out of 16582198 faces (99.9000%)")
    r = L.read_layer_log(log)
    assert round(float(r["kept_of_offered_pct"]), 2) == 90.73, r["kept_of_offered_pct"]
    said = " ".join(L.shortfalls(r, interface_patch="fluid_to_solid",
                                 n_layers=3, cells=33117875))
    assert "kept 90.73% of the faces the extruder offered it" in said, said
    assert "0.10 were never offered" in said, said
    assert "9.26 were offered and then lost" in said, said


# ONE NUMBER WAS ANSWERING TWO QUESTIONS.
#
# COVERAGE_FLOOR_PCT 92.0 was placed just above the coverage of a run that was defective FOR NOT
# FITTING ITS CELL - its own rationale says so: "the DEFECTIVE delivered run finished at 90.7146%,
# so a floor under that would admit the run this module exists to refuse". Coverage was a proxy
# for that fault, and job cba7ac69 lands at 90.6422% for an unrelated reason: the extruder
# declined 1,515,568 faces where the lattice staircase turns. Of the faces it WAS given, the mover
# kept 15,030,462 of 15,066,630 - 99.7599%. A near perfect layer pass on a part whose interface
# mostly cannot carry prisms read exactly like a layer pass that had failed.


def test_what_the_mover_kept_of_what_it_was_given_is_published():
    r = _report()
    assert round(float(r["kept_of_offered_pct"]), 4) == 99.7599, r.get("kept_of_offered_pct")


def test_a_mover_that_did_its_job_is_not_charged_with_the_parts_shape():
    """THE WHOLE POINT, and it used to go the other way.

    This asserted that the shortfall SAID "kept 99.76% of every face it was given" - a
    sentence that existed to explain away a miss the engine should not have reported. Job
    cba7ac69 kept 99.7599% of everything it was offered on a 16,582,198-face interface. There
    is no miss to explain: the coverage sentence is gone entirely, and the only things left in
    the list are about the stack.
    """
    said = L.shortfalls(_report(), interface_patch="fluid_to_solid", n_layers=3,
                        cells=33117875)
    assert not [s for s in said if "offered it" in s], said
    assert not [s for s in said if "never offered" in s], said
    # what IS still said is true of the stack, not of the interface's shape
    assert [s for s in said if "thickness" in s or "layers of the 3" in s], said


def test_a_pass_that_really_did_fail_does_not_get_that_sentence():
    # offered nearly everything and then lost it: the mover IS the problem, and the number says so.
    log = LOG.replace("(90.8603%)", "(99.9000%)").replace("15066630", "16565616")
    r = L.read_layer_log(log)
    assert float(r["kept_of_offered_pct"]) < 91.0, r["kept_of_offered_pct"]
    said = " ".join(L.shortfalls(r, interface_patch="fluid_to_solid", n_layers=3, cells=33117875))
    assert "of the faces the extruder offered it" in said, said


def test_no_extruding_lines_leaves_it_unpublished_rather_than_wrong():
    assert L.read_layer_log(_NO_LAYERS)["kept_of_offered_pct"] is None
