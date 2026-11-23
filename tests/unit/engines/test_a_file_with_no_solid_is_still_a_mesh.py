# Responsibility: A model with no solid region meshes as air, and says so, instead of raising.
# Boundaries: mesh_ecxml itself, writing a real polyMesh; no OpenFOAM and no checkMesh.
"""The mesher handled "the solids fill the whole domain, so there is no air". It did not handle the
mirror case: a file with no solid at all.

Three real files land there. A set of 2D plates with no thickness - every one of them an opening or
a baffle, none of them a solid. A file whose <geometry> is empty, which the schema permits. A file
whose every object is inactive. In all three the zone array is AIR throughout, so nothing was ever
going to select a part; but `np.where` evaluates both of its branches, and indexing an empty part
table raises before the `where` is reached:

    cell_zone = np.where(zone >= 0, part_zone[np.maximum(zone, 0)], airs)
    IndexError: index 0 is out of bounds for axis 0 with size 0

The fix skips the indexing rather than merely not selecting it, and the build report says what kind
of mesh the file asked for. These tests mesh the three files for real and read the result.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.snapgrid.grid import GridPlan
from meshpipeline.engines.snapgrid.mesher import mesh_ecxml

from tests.unit.cad.ecxml_models import Ecxml, standard_materials

MM = 1e-03


def _plates_only() -> Ecxml:
    """Six plates, one on each plane, none with a thickness: no solid anywhere."""
    d = standard_materials(Ecxml("plates only", "FloTHERM"))
    d.domain((0, 0, 0), (60 * MM, 60 * MM, 60 * MM))
    for i, (pl, sz) in enumerate((("+xy", (10 * MM, 10 * MM, 0)),
                                  ("-xy", (10 * MM, 10 * MM, 0)),
                                  ("+yz", (0, 10 * MM, 10 * MM)),
                                  ("-yz", (0, 10 * MM, 10 * MM)),
                                  ("+xz", (10 * MM, 0, 10 * MM)),
                                  ("-xz", (10 * MM, 0, 10 * MM)))):
        d.plate(f"plate {pl}", (5 * MM + i * 8 * MM, 5 * MM, 10 * MM), sz, pl,
                "Aluminium 6063", 0.0)
    return d


def _empty() -> Ecxml:
    """A solution domain and an empty <geometry>: the schema permits it."""
    d = Ecxml("empty", "FloTHERM")
    d.domain((0, 0, 0), (10 * MM, 10 * MM, 10 * MM))
    return d


def _all_inactive() -> Ecxml:
    """Every object switched off. There is geometry in the file and no solid to mesh."""
    d = standard_materials(Ecxml("all off", "FloTHERM"))
    d.domain((0, 0, 0), (30 * MM, 20 * MM, 20 * MM))
    d.block("off a", (2 * MM, 2 * MM, 2 * MM), (6 * MM, 6 * MM, 6 * MM), "Silicon", 1.0,
            active=False)
    with d.assembly("off group", active=False):
        d.block("off b", (14 * MM, 2 * MM, 2 * MM), (6 * MM, 6 * MM, 6 * MM), "Silicon", 1.0)
    return d


CASES = {"plates only": _plates_only, "empty geometry": _empty, "all inactive": _all_inactive}


def _mesh(tmp_path, which: str):
    src = tmp_path / "m.ecxml"
    src.write_bytes(CASES[which]().xml())
    return mesh_ecxml(src, tmp_path / "case", GridPlan(max_cells=200_000), binary=False)


@pytest.mark.parametrize("which", sorted(CASES))
def test_a_model_with_no_solid_meshes_instead_of_raising(tmp_path, which):
    """The regression. Before the fix every one of these three raised IndexError from
    mesher.py's zone arithmetic, which is a mesher bug and not a model fault."""
    got = _mesh(tmp_path, which)
    assert got.report["polymesh"]["cells"] > 0, (
        "the domain is meshed as air, so there are cells in it")


@pytest.mark.parametrize("which", sorted(CASES))
def test_the_only_region_is_air_and_it_holds_every_cell(tmp_path, which):
    """No solid region is invented to stand in for the missing one, and no cell is left out."""
    got = _mesh(tmp_path, which)
    kinds = sorted({r["type"] for r in got.regions})
    assert kinds == ["fluid"], f"a file with no solid gets no solid region, got {kinds}"
    assert sum(r["cells"] for r in got.regions) == got.report["polymesh"]["cells"]


@pytest.mark.parametrize("which", sorted(CASES))
def test_the_report_says_the_file_asked_for_a_flow_mesh(tmp_path, which):
    """Silence here would be the worse failure: a customer who uploads a board and gets back an
    empty box of air has to be told that is what the file said."""
    got = _mesh(tmp_path, which)
    said = " ".join(got.report["build_report"])
    assert "No solids" in said, said[:400]
    assert "flow mesh" in said


def test_a_model_that_does_have_a_solid_still_gets_one(tmp_path):
    """The guard must not switch off the ordinary path: one block, one solid region, one air."""
    d = standard_materials(Ecxml("one block", "FloTHERM"))
    d.domain((0, 0, 0), (30 * MM, 20 * MM, 20 * MM))
    d.block("die", (10 * MM, 6 * MM, 6 * MM), (8 * MM, 8 * MM, 8 * MM), "Silicon", 4.0)
    src = tmp_path / "one.ecxml"
    src.write_bytes(d.xml())
    got = mesh_ecxml(src, tmp_path / "case", GridPlan(max_cells=200_000), binary=False)
    kinds = sorted(r["type"] for r in got.regions)
    assert kinds == ["fluid", "solid"]
    said = " ".join(got.report["build_report"])
    assert "No solids" not in said
