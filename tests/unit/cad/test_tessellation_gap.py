# Responsibility: Verify INS-22 - a gap in a CAD solid's drawn skin (a face its tessellation left bare) is never
# proposed as an opening; on a triangle file, where an open rim can be a real open end, it is read as before.
# Boundaries: a real OpenCASCADE tube tessellated to STL with two triangles taken out of its outer wall; the geometry
# check's own skin reading (geometry_check.skin_holes); no mesher.
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_flat_holes import _cyl, _write
from tests.unit.cad.test_scout import _prepared

from meshpipeline.cad.cad_tessellate import tessellate_to_stl
from meshpipeline.cad.stl_io import read_stl_triangles, write_stl_binary


def _gapped_skin(tmp: Path) -> Path:
    """A 20 mm tube (bore 15 mm, 100 mm long) as its tessellation draws it, with two neighbouring triangles of the
    outer wall half way along left out - the hole OpenCASCADE leaves round a face it cannot triangulate."""
    step = _write(BRepAlgoAPI_Cut(_cyl(10.0, 100.0), _cyl(7.5, 100.0)).Shape(), tmp / "tube.step")
    tessellate_to_stl(step, tmp / "full.stl", prepared=_prepared(step))
    tris = np.asarray(read_stl_triangles(tmp / "full.stl"), dtype=float).reshape(-1, 3, 3)
    c = tris.mean(axis=1)
    r = np.hypot(c[:, 0], c[:, 1]) / np.hypot(c[:, 0], c[:, 1]).max()
    z = c[:, 2] / c[:, 2].max()
    outer = np.flatnonzero((r > 0.95) & (abs(z - 0.5) < 0.2))
    drop = set(outer[np.argsort(abs(z[outer] - 0.5))[:2]].tolist())
    keep = [tuple(map(tuple, t)) for i, t in enumerate(tris) if i not in drop]
    write_stl_binary(tmp / "gap.stl", keep)
    return tmp / "gap.stl"


def test_a_gap_in_a_cad_solids_skin_is_no_opening(tmp_path):
    from meshpipeline.application.geometry_check import skin_holes
    skin = _gapped_skin(tmp_path)
    as_triangles = skin_holes(skin, {"solids": 0})
    assert any(h.get("kind") == "rim" for h in as_triangles), as_triangles
    facts = {"solids": 1}
    as_cad = skin_holes(skin, facts)
    assert not any(h.get("kind") == "rim" for h in as_cad), as_cad
    assert any("INS-22" in n for n in facts.get("notes") or [])
