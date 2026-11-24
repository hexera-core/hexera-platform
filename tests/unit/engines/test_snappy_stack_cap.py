# Responsibility: Verify a resolved wall's stack is capped at its wall cell and a share of the narrowest gap, from the asked first height (CFDREQ-9, rulebook 75), and that a stack snappy cut short is said as that.
# Boundaries: the dictionary render_snappy_case writes for a small staged body, the cap's arithmetic, and the log reading; no OpenFOAM run.
from __future__ import annotations

import re

from tests.unit.engines.test_snappy_external_layer_bridge import ANALYSIS, REC, _ask, _box_stl

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines.snappy import snappy_runner as R
from meshpipeline.engines.snappy_hexmesh import parse_layer_coverage
from meshpipeline.engines.verdict_limits import coverage_words


def test_the_cap_keeps_the_first_height_and_shortens_the_stack():
    # the rotor's numbers: a 0.0292 mm first layer under 3.8 mm wall cells, a 4.43 mm bridged stack
    n, g, stack, last = CR.capped_stack(0.0292e-3, 3.8e-3, 4.43e-3, 3.8e-3, 1.2)
    assert stack <= 3.8e-3 and n >= 10
    # the growth rises no further than the step into the core needs, and never past 1.3
    assert 1.2 <= g <= CR.MAX_CAPPED_GROWTH
    first_built = stack * (g - 1) / (g ** n - 1)
    assert abs(first_built - 0.0292e-3) / 0.0292e-3 < 1e-6
    assert 0 < last < 1


def test_a_stack_that_fits_is_left_alone():
    assert CR.capped_stack(0.03e-3, 4e-3, 2e-3, 4e-3, 1.2)[0] == 0


def test_the_cap_is_the_wall_cell_or_a_share_of_the_narrowest_gap():
    assert R.stack_cap(4e-3, None) == 4e-3
    assert R.stack_cap(4e-3, 5.5e-3) == CR.STACK_GAP_SHARE * 5.5e-3
    assert R.stack_cap(4e-3, 1.0) == 4e-3


def test_a_capped_stack_reaches_the_dictionary_and_its_row(tmp_path):
    ws = tmp_path
    (ws / "system").mkdir()
    _box_stl(ws / "constant" / "triSurface" / "body.stl")
    ask = _ask(0.034)
    R.render_snappy_case(ws, surface_name="body", feature_file="body.eMesh", analysis=ANALYSIS,
                         recommendation=REC, domain_min=[-4.0, -3.0, -3.0], domain_max=[7.0, 4.0, 4.0],
                         strategy={"n_layers": 5, "last_layer_rel": 0.35, "max_cells": 2_000_000},
                         requirements=ask)
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    first = float(re.search(r"firstLayerThickness\s+([-+0-9.eE]+)", text).group(1))
    first *= float(re.search(r"plainWallCell\s+([-+0-9.eE]+)", text).group(1)) if "relativeSizes true" in text else 1.0  # CFDREQ-33: a relative stack, read as its height
    g = float(re.search(r"expansionRatio\s+([-+0-9.eE]+)", text).group(1))
    n = int(re.search(r"nSurfaceLayers\s+(\d+)", text).group(1))
    assert abs(first - 0.034e-3) < 1e-9, "the first height asked is never traded away"
    wall_cell = REC["base_cell"] / 2 ** REC["surface_level"][1]
    assert first * (g ** n - 1) / (g - 1) <= wall_cell * (1 + 1e-6), "the stack is no thicker than the wall cell"
    assert "a thicker stack is not kept where the surface curves" in (ask.layer_note or "")


SNAPPY_TAIL = """
Extruding 83213 out of 88989 faces (93.5093%). Removed extrusion at 1146 faces.
Extruding 78624 out of 88989 faces (88.3525%). Removed extrusion at 0 faces.
Added 480736 out of 1690791 cells (28.4326%).
"""


def test_a_stack_cut_short_is_said_as_that():
    lc = parse_layer_coverage(SNAPPY_TAIL)
    assert lc["faces_extruded_pct"] == 88.3525 and lc["overall_pct"] == 28.4326
    q = {"layer_faces_extruded_pct": 88.3525,
         "per_patch_layers": {"body": {"layers": 5.4, "target": 19, "coverage_pct": 59.6}}}
    said = coverage_words(q, 28.4326, 80)
    assert "grew on 88% of the wall faces, but only 5.4 of the 19 layers asked" in said
    assert "cut short" in said
    # faces and cells that agree: no such line
    assert coverage_words({"layer_faces_extruded_pct": 30.0}, 28.0, 80) == ""
