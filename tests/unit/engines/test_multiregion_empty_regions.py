# A region that gets no cells is said, with its cause, before splitMeshRegions crashes on it
# (diffuser_plenum_004_cht: signal 11, "regionProperties is missing").
from __future__ import annotations

from meshpipeline.engines.snappy_multiregion import native as N


def test_empty_regions_are_read_off_the_toposet_log(tmp_path):
    (tmp_path / "log.topoSet").write_text(
        "    Using zone fluid with 0 cells\n    Using zone wall_solid with 0 cells\n")
    (tmp_path / "log.surfaceFeatureExtract").write_text("        open edges         : 14\n")
    assert N.empty_regions(tmp_path) == ["fluid", "wall_solid"]
    why = N.empty_regions_reason(tmp_path, ["fluid", "wall_solid"])
    assert "fluid, wall_solid" in why and "14 open edge" in why


def test_regions_with_cells_say_nothing(tmp_path):
    (tmp_path / "log.topoSet").write_text("    Using zone fluid with 208395 cells\n")
    assert N.empty_regions(tmp_path) == []
    assert N.empty_regions(tmp_path / "missing") == []
