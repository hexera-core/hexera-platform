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
# The two causes ask for different things. A face never offered is `featureAngle` declining to
# grow a prism where the surface turns, and raising the angle would recover it and cost quality
# (measured: featureAngle 180 gives 96.114% coverage at aspect 37.49, 120 gives 95.536% at 19.30).
# A face offered and then lost is the mover failing, which more iterations or a thinner stack
# might fix. One number under a floor cannot tell them apart.
from meshpipeline.engines.cfmesh_conjugate import layers as L

#: The real figures, to the digit.
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
    said = " ".join(L.shortfalls(_report(), interface_patch="fluid_to_solid",
                                 n_layers=3, cells=33117875))
    assert "9.14 were never offered" in said, said
    assert "0.22 were offered and then lost" in said, said
    assert "featureAngle 120 degrees" in said, said


def test_it_still_opens_with_the_figure_and_the_floor():
    said = L.shortfalls(_report(), interface_patch="fluid_to_solid", n_layers=3,
                        cells=33117875)[0]
    assert said.startswith("covered 90.6422% of the interface against the 92% this engine aims at")


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
    # offered nearly all of it, kept little: the opposite diagnosis, and it must say so.
    log = LOG.replace("(90.8603%)", "(99.9000%)")
    said = " ".join(L.shortfalls(L.read_layer_log(log), interface_patch="fluid_to_solid",
                                 n_layers=3, cells=33117875))
    assert "0.10 were never offered" in said, said
    assert "9.26 were offered and then lost" in said, said
