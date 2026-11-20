# Responsibility: Verify tier 2b against the REAL fTetWild and OCC kernels - that the tolerant
#                 rung eats what gmsh refuses, and that a tiny budget ends in time with nothing
#                 left running.
# Boundaries: the native tier. Skipped where OCP or the fTetWild binary is absent. No CAD is
#             committed: the box fixtures are built in-process and the corpus file is read from
#             HEXERA_MESH_FAIL_CORPUS when it happens to be there.
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepTools", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402

from meshpipeline.cad.repair import tolerant_probe as tp  # noqa: E402

_CORPUS = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                              "~/archductor/mesh-fail-corpus")).expanduser()


def _ftetwild() -> None:
    from meshpipeline.engines.fea_tetwild import binary

    if binary() is None:
        pytest.skip("fTetWild is not installed (native tier: the mesh image ships it)")


def _corpus(name: str) -> Path:
    path = _CORPUS / name
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    return path


def _box(x=0.0, y=0.0, z=0.0, size=10.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), size, size, size).Shape()


def _write(shape, path: Path) -> Path:
    assert BRepTools.Write_s(shape, str(path))
    return path


def _overlapping(*shapes):
    """The two solids COMPOUNDED, never fused: the interpenetration is the fixture. A boolean
    would resolve the overlap gmsh refuses, and then there would be nothing to route."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    return compound


def test_box_eats(tmp_path):
    _ftetwild()
    result = tp.tolerant_probe(_write(_box(), tmp_path / "box.brep"),
                               diagonal_mm=17.3, budget_s=30.0)
    assert result.verdict == tp.EATS, result.message
    assert result.tets > 0
    assert result.seconds < 30.0


def test_interpenetrating_boxes_eat(tmp_path):
    """THE QUESTION TIER 2B EXISTS TO ANSWER. gmsh refuses this with `overlapping_facets`; the
    tolerant rung meshes it, because an envelope does not care that two faces cross."""
    _ftetwild()
    overlap = _overlapping(_box(), _box(5.0, 5.0, 5.0))
    result = tp.tolerant_probe(_write(overlap, tmp_path / "overlap.brep"),
                               diagonal_mm=26.0, budget_s=30.0)
    assert result.verdict == tp.EATS, result.message
    assert result.tets > 0


def test_tiny_budget_returns_no_answer_in_time(tmp_path):
    """A budget too small to finish ends IN TIME and leaves no mesher behind.

    The process-group kill is the assertion that matters: fTetWild is the child's grandchild, so a
    plain `proc.kill()` would leave it burning a worker's CPU for minutes.
    """
    _ftetwild()
    overlap = _overlapping(_box(size=40.0), _box(20.0, 20.0, 20.0, size=40.0))
    path = _write(overlap, tmp_path / "overlap.brep")

    started = time.monotonic()
    result = tp.tolerant_probe(path, diagonal_mm=104.0, budget_s=0.5)
    elapsed = time.monotonic() - started

    assert result.verdict in {tp.NO_ANSWER, tp.REFUSES, tp.EATS}, result.message
    assert elapsed < 0.5 + tp.REAP_S + 1.0, elapsed
    left = subprocess.run(["pgrep", "-f", f"{tmp_path}"], capture_output=True, text=True)
    assert left.stdout.strip() == "", left.stdout


def test_corpus_overlap_file_gets_a_routing_verdict(tmp_path):
    """A real volume-stage overlap from the 2026-11-15 census. REPORTED, NOT ASSERTED: whether
    the tolerant rung eats this particular part is the evidence Task 6 collects, not a pin."""
    _ftetwild()
    from meshpipeline.cad.repair.brep import _read_shape

    source = _corpus("abc/00500400_bce216c87d2d17025db6ab36_step_000.step")
    path = _write(_read_shape(source), tmp_path / "corpus.brep")
    result = tp.tolerant_probe(path, diagonal_mm=132.3, budget_s=30.0)
    print(f"tier 2b on {source.name}: {result.verdict} "
          f"({result.tets} tets, {result.seconds:.1f} s) {result.message}")
    assert result.verdict in {tp.EATS, tp.REFUSES, tp.NO_ANSWER}
    assert result.seconds <= 35.0
