# The external-ANSYS validation of delivered Fluent meshes found three exporter defects:
# inlets typed pressure-outlet (with a THIRD wrong answer, bc code 4, in the face headers),
# zone ids that follow OpenFOAM patch order, and boundary faces wound inward. These tests
# drive devtools/validation/fluent_repair.py over a synthetic single-hex mesh built with all
# three defects and assert each is corrected without touching anything else.
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).parents[3]
sys.path.insert(0, str(REPO / "devtools" / "validation"))
import fluent_repair  # noqa: E402


def _defective_cube(tmp_path: Path) -> Path:
    """One hex cell; 6 boundary quads ALL wound inward; foamMeshToFluent's exact defect
    set: inlet+outlet both bc code 4 in (13) and both 'pressure-outlet' in (39); ids in
    patch order (wall first)."""
    mesh = textwrap.dedent("""\
        (0 "OpenFOAM to Fluent Mesh File")
        (0 "Dimension:")
        (2 3)
        (10 (0 1 8 0 3))
        (10 (1 1 8 1 3)
        (
            0 0 0
            1 0 0
            1 1 0
            0 1 0
            0 0 1
            1 0 1
            1 1 1
            0 1 1
        ))
        (12 (0 1 1 0 0))
        (12 (1 1 1 1 4))
        (13 (0 1 6 0 0))
        (13 (a 1 4 3 0)
        (
        4 1 2 3 4 1 0
        4 5 8 7 6 1 0
        4 1 5 6 2 1 0
        4 3 7 8 4 1 0
        ))
        (13 (b 5 5 4 0)
        (
        4 1 4 8 5 1 0
        ))
        (13 (c 6 6 4 0)
        (
        4 2 6 7 3 1 0
        ))
        (39 (1 fluid fluid-1)())
        (39 (10 wall wall)())
        (39 (11 pressure-outlet inlet)())
        (39 (12 pressure-outlet outlet)())
    """)
    p = tmp_path / "cube.msh"
    p.write_text(mesh)
    return p


def _zone(report: dict, name: str) -> dict:
    return next(z for z in report["zones"] if z["name"] == name)


def test_check_mode_detects_all_three_defects(tmp_path):
    src = _defective_cube(tmp_path)
    report = fluent_repair.repair(src, src.with_suffix(".out.msh"), check_only=True,
                                  overrides={}, n_samples=40, report_path=None)
    inlet = _zone(report, "inlet")
    assert inlet["new_type"] == "velocity-inlet" and inlet["new_bc_code"] == 10
    assert _zone(report, "outlet")["new_bc_code"] == 5
    assert _zone(report, "wall")["new_bc_code"] == 3
    # every zone was wound inward, and detection is unanimous, not majority-of-noise
    for z in report["zones"]:
        assert z["outward_fraction"] == 0.0
        assert "INVERTED" in z["normals"]
    # deterministic ids: role priority puts the inlet first from 10
    assert (inlet["new_id"], _zone(report, "outlet")["new_id"],
            _zone(report, "wall")["new_id"]) == (10, 11, 12)


def test_repair_fixes_everything_and_output_verifies_clean(tmp_path):
    src = _defective_cube(tmp_path)
    out = tmp_path / "cube.fixed.msh"
    fluent_repair.repair(src, out, check_only=False, overrides={}, n_samples=40,
                         report_path=None)
    verify = fluent_repair.repair(out, tmp_path / "unused.msh", check_only=True,
                                  overrides={}, n_samples=40, report_path=None)
    for z in verify["zones"]:
        assert z["outward_fraction"] == 1.0, z
        assert z["normals"] == "ok"
    assert _zone(verify, "inlet")["old_bc_code"] == 10       # velocity-inlet persisted
    assert _zone(verify, "outlet")["old_bc_code"] == 5
    assert _zone(verify, "wall")["old_bc_code"] == 3
    text = out.read_text()
    assert "(39 (10 velocity-inlet inlet)())" in text
    assert "(39 (11 pressure-outlet outlet)())" in text
    assert "(39 (12 wall wall)())" in text
    # the flip must be a pure node reversal: same owner cell, neighbour stays 0
    assert "4 5 8 4 1 1 0" in text                            # inlet quad 1 4 8 5 reversed
    # interior declarations and the cell zone are untouched
    assert "(12 (1 1 1 1 4))" in text
    assert "(13 (0 1 6 0 0))" in text


def test_name_overrides_and_unknown_names_are_conservative(tmp_path):
    src = _defective_cube(tmp_path)
    report = fluent_repair.repair(
        src, src.with_suffix(".o.msh"), check_only=True,
        overrides={"inlet": "mass-flow-inlet"}, n_samples=40, report_path=None)
    assert _zone(report, "inlet")["new_bc_code"] == 20
    # a zone whose name matches no rule keeps its type and raises a warning instead
    mesh = src.read_text().replace("wall wall", "wall mystery_patch")
    src2 = src.with_name("cube2.msh")
    src2.write_text(mesh)
    report2 = fluent_repair.repair(src2, src2.with_suffix(".o.msh"), check_only=True,
                                   overrides={}, n_samples=40, report_path=None)
    assert any("mystery_patch" in w for w in report2["warnings"])
    assert _zone(report2, "mystery_patch")["new_type"] == "wall"
