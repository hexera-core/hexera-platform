# Responsibility: Hold the shell decks (engines/shell_writers.py) to plain ASCII whatever the part, group and source
# names hold: a part read from a file whose name has an accented letter is still delivered.
# Boundaries: pure numpy; no OpenCASCADE, no gmsh.
from __future__ import annotations

import numpy as np

from meshpipeline.engines import shell_writers as sw
from meshpipeline.engines.shell_mesh import ShellMesh


def test_accents_become_plain_letters_and_other_letters_underscores():
    assert sw.ascii_text("sisärakenne v21.step") == "sisarakenne v21.step"
    assert sw.ascii_text("Straße 外壳") == "Stra_e __"
    assert sw.ascii_text("Part_1_t2mm") == "Part_1_t2mm"


def test_a_mesh_whose_names_are_not_ascii_is_written_in_all_three_decks(tmp_path):
    xyz = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 10.0, 0.0]])
    mesh = ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3]]), trias=np.zeros((0, 3), dtype=int), wall=np.array([2.0]),
                     mid=["Mid_1"], part=["Part_1"], groups={"Part_1_sisä_Face_1+Face_2": np.array([0])})
    sw.write_all(tmp_path, mesh, units="mm")
    for name in ("shell.bdf", "shell.inp", "shell.cdb"):
        text = (tmp_path / name).read_bytes()
        assert text.isascii()
    back = sw.read_bdf(tmp_path / "shell.bdf")
    assert len(back.elements) == 1 and any("sisa" in g for g in back.groups)
