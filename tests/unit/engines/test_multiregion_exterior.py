# Responsibility: Verify a multi-region case keeps only its declared regions and names its outside as the user declared.
# On main every multi-region case came back with an undeclared region `domain0` - the air around a pipe and its wall,
# which nobody asked to mesh - and the fluid's ports were interfaces to it rather than inlet/outlet patches
# (both lab CHT assemblies, every format, 2026-10-04).
from __future__ import annotations

import json

from meshpipeline.engines.snappy_multiregion import multiregion_runner as R
from meshpipeline.engines.snappy_multiregion import native as N


def test_the_background_outside_is_a_named_wall_patch():
    text = R.render_block_mesh([0, 0, 0], [1, 1, 1], 0.1)
    assert "boundary\n(\n    exterior" in text and "type wall;" in text
    assert text.count("(0 3 2 1)") == 1 and "boundary ();" not in text


def test_the_kept_cells_are_the_declared_regions_zones():
    rmap = R.region_map([{"name": "fluid", "type": "fluid", "solids": [0]},
                         {"name": "pipe", "type": "solid", "solids": [1]}])
    d = R.render_zoned_set_dict(rmap)
    assert "name zoned; type cellSet; action new; source zoneToCell; zone fluid;" in d
    assert "name zoned; type cellSet; action add; source zoneToCell; zone pipe;" in d


def _region(ws, name, patches):
    pm = ws / "constant" / name / "polyMesh"
    pm.mkdir(parents=True)
    body = "\n".join(f"    {p}\n    {{\n        type wall;\n        nFaces 10;\n        startFace 0;\n    }}"
                     for p in patches)
    (pm / "boundary").write_text(f"{len(patches)}\n(\n{body}\n)\n")


def test_the_exterior_becomes_the_declared_ports_then_the_declared_wall(tmp_path):
    ws = tmp_path
    (ws / "port_declaration.json").write_text(json.dumps([
        {"name": "inlet", "type": "inlet", "diameter_mm": 100, "near_mm": [0, 0, 0]},
        {"name": "outlet", "type": "outlet", "diameter_mm": 50, "near_mm": [500, 0, 0]},
        {"name": "pipe_wall", "type": "wall"}]))
    _region(ws, "fluid", ["fluid_to_pipe", "exterior"])
    _region(ws, "pipe", ["pipe_to_fluid", "exterior"])
    plan = dict(N.name_exterior(ws, [{"name": "fluid", "type": "fluid"},
                                      {"name": "pipe", "type": "solid"}]))
    # the fluid: its ports from the exterior faces at their declared places, then the wall
    assert [c for c, _ in plan["fluid"]] == [
        "topoSet -region fluid", "createPatch -region fluid -overwrite",
        "createPatch -region fluid -dict system/fluid/createPatchDict.wall -overwrite"]
    ts = (ws / "system" / "fluid" / "topoSetDict").read_text()
    assert "box (-0.06 -0.06 -0.06) (0.06 0.06 0.06)" in ts and "action subset" in ts
    assert "set port_outlet;" in (ws / "system" / "fluid" / "createPatchDict").read_text()
    # a solid has no ports: its whole outside is the declared wall
    assert [c for c, _ in plan["pipe"]] == [
        "createPatch -region pipe -dict system/pipe/createPatchDict.wall -overwrite"]
    assert "name pipe_wall; patchInfo { type wall; } constructFrom patches; patches (exterior);" in \
        (ws / "system" / "pipe" / "createPatchDict.wall").read_text()


def test_a_region_with_no_outside_is_left_alone(tmp_path):
    _region(tmp_path, "core", ["core_to_air"])
    assert N.name_exterior(tmp_path, [{"name": "core", "type": "solid"}]) == []
